from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import hashlib
import os
import uuid
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.account_state import AccountCooldownError, AccountState
from app.browser import AuthenticationError, RateLimitedError, RiskControlError, SearchBoxNotReadyError, open_douyin, open_private_messages, save_trace, verify_login
from app.config import ConfigError, load_settings, load_task
from app.douyin import DouyinChat
from app.history import AlreadyRunningError, History, run_lock
from app.models import Settings, TargetResult
from app.notifier import send_dingtalk_notification, send_telegram_notification
from app.privacy import RedactingFormatter, build_target_aliases, redact_text, target_alias
from app.recovery import classify_failure, manual_retry_allowed, retry_policy
from app.sender import send_message


LOGGER = logging.getLogger("douyin_sender")


RetryMode = Literal["failed", "unconfirmed"]


async def run(
    dry_run: bool = False,
    env_file: str | None = None,
    retry_mode: RetryMode | None = None,
) -> int:
    settings = load_settings(env_file)
    task = load_task(settings)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    aliases = build_target_aliases(task.targets)
    _configure_logging(settings.artifacts_dir, aliases)
    run_id = uuid.uuid4().hex
    started_at = datetime.now().astimezone().isoformat()

    if not settings.storage_state and not settings.cookie:
        raise ConfigError("必须配置 DOUYIN_STORAGE_STATE 或 DOUYIN_COOKIE")

    history = History(settings.artifacts_dir / "history.json")
    account_state = AccountState(
        settings.artifacts_dir / "account-state.json",
        settings.account_id or "default",
    )
    run_date = history.run_date(task.timezone)
    if retry_mode == "unconfirmed":
        LOGGER.warning("显式重试未确认消息：这些消息可能已经发送，存在重复发送风险")
    results: list[TargetResult] = []
    screenshots: list[Path] = []
    fatal_error: Exception | None = None
    account_failure_category = None
    try:
        account_state.ensure_runnable()
        async with open_douyin(settings) as session:
            page = session.page
            trace_saved = False
            try:
                await open_private_messages(page)
            except Exception as exc:
                category = classify_failure(exc, stage="navigation")
                if retry_policy(category).abort_account:
                    account_state.mark_failure(category)
                    account_failure_category = category
                LOGGER.exception("打开抖音私信页面失败")
                screenshot = await _screenshot(page, settings.artifacts_dir, "login")
                if screenshot:
                    screenshots.append(screenshot)
                if settings.trace and not trace_saved:
                    try:
                        await save_trace(session, _trace_path(settings.artifacts_dir))
                        trace_saved = True
                    except Exception:
                        LOGGER.exception("保存 trace 失败")
                label = "登录检查" if isinstance(exc, (AuthenticationError, RiskControlError, RateLimitedError)) else "运行检查"
                results.append(TargetResult(target=label, status="failed", error=str(exc), failure_category=category))
                fatal_error = exc

            if fatal_error is None:
                chat = DouyinChat(page, timeout_ms=int(task.target_open_timeout_seconds * 1000))
                for index, target in enumerate(task.targets):
                    sent = 0
                    alias = target_alias(index)
                    plans = _message_plans(history, task.task_id, run_date, target)
                    selected, duplicate_count = _select_message_plans(
                        history,
                        plans,
                        dry_run=dry_run,
                        prevent_duplicates=task.prevent_duplicates,
                        retry_mode=retry_mode,
                    )
                    if not dry_run and not selected:
                        status = "duplicate" if duplicate_count else "skipped"
                        results.append(TargetResult(target=target.name, status=status, target_alias=alias, identity=target.identity_key))
                        continue
                    current_key: str | None = None
                    opened = False
                    try:
                        LOGGER.info("处理好友: %s", alias)
                        target_ref = target if target.search_candidates() != (target.name,) else target.name
                        await chat.open_target(target_ref, retries=task.target_open_retries)
                        opened = True
                        if not dry_run:
                            for selected_index, (message_index, message, message_id, key) in enumerate(selected):
                                await verify_login(page, timeout_ms=3_000)
                                current_key = key
                                began = history.reserve(
                                    key,
                                    target_key=target.identity_key,
                                    display_name=target.name,
                                    message_id=message_id,
                                    allow_success_override=retry_mode is None and not task.prevent_duplicates,
                                )
                                if not began:
                                    duplicate_count += 1
                                    current_key = None
                                    continue
                                await send_message(page, chat, message, task.stickers)
                                history.mark_success(key)
                                current_key = None
                                sent += 1
                                if selected_index < len(selected) - 1:
                                    await asyncio.sleep(random.uniform(task.interval_min, task.interval_max))
                        status = "success" if sent or dry_run else "duplicate"
                        results.append(TargetResult(target=target.name, status=status, sent=sent, target_alias=alias, identity=target.identity_key))
                    except (AuthenticationError, RiskControlError, RateLimitedError) as exc:
                        category = classify_failure(exc, stage="send" if opened else "target_open")
                        _persist_target_failure(
                            history,
                            selected,
                            current_key=current_key,
                            category=category,
                            error=redact_text(str(exc), aliases),
                            target_key=target.identity_key,
                            display_name=target.name,
                            opened=opened,
                        )
                        account_state.mark_failure(category)
                        account_failure_category = category
                        LOGGER.exception("处理好友时发生账号级故障: %s", alias)
                        screenshot = await _screenshot(page, settings.artifacts_dir, alias)
                        if screenshot:
                            screenshots.append(screenshot)
                        if settings.trace and not trace_saved:
                            try:
                                await save_trace(session, _trace_path(settings.artifacts_dir))
                                trace_saved = True
                            except Exception:
                                LOGGER.exception("保存 trace 失败")
                        results.append(
                            TargetResult(
                                target=target.name,
                                status="failed",
                                sent=sent,
                                error=str(exc),
                                target_alias=alias,
                                failure_category=category,
                                identity=target.identity_key,
                            )
                        )
                        fatal_error = exc
                        break
                    except Exception as exc:
                        category = classify_failure(exc, stage="send" if opened else "target_open")
                        if current_key is not None and category in {"transient_network", "navigation_timeout", "non_retryable"}:
                            category = "send_unconfirmed"
                        _persist_target_failure(
                            history,
                            selected,
                            current_key=current_key,
                            category=category,
                            error=redact_text(str(exc), aliases),
                            target_key=target.identity_key,
                            display_name=target.name,
                            opened=opened,
                        )
                        LOGGER.exception("好友处理失败: %s", alias)
                        screenshot = await _screenshot(page, settings.artifacts_dir, alias)
                        if screenshot:
                            screenshots.append(screenshot)
                        if settings.trace and not trace_saved:
                            try:
                                await save_trace(session, _trace_path(settings.artifacts_dir))
                                trace_saved = True
                            except Exception:
                                LOGGER.exception("保存 trace 失败")
                        status = "unconfirmed" if category == "send_unconfirmed" else "failed"
                        results.append(
                            TargetResult(
                                target=target.name,
                                status=status,
                                sent=sent,
                                error=str(exc),
                                target_alias=alias,
                                failure_category=category,
                                identity=target.identity_key,
                            )
                        )
                        if retry_policy(category).abort_account:
                            account_state.mark_failure(category)
                            account_failure_category = category
                            fatal_error = exc
                            break
                        if not task.continue_on_error and category not in {"friend_not_found", "send_unconfirmed"}:
                            break
                    if index < len(task.targets) - 1 and not dry_run:
                        await asyncio.sleep(random.uniform(task.interval_min, task.interval_max))

            if settings.trace and not trace_saved:
                try:
                    await session.context.tracing.stop()
                except Exception as exc:
                    LOGGER.exception("停止 trace 失败")
                    if fatal_error is None:
                        fatal_error = exc
                        results.append(TargetResult(target="运行收尾", status="failed", error=str(exc)))
    except AccountCooldownError as exc:
        if fatal_error is None:
            fatal_error = exc
            account_failure_category = exc.failure_category
            results.append(TargetResult(target="账号检查", status="failed", error=str(exc), failure_category=exc.failure_category))
    except Exception as exc:
        if fatal_error is None:
            category = classify_failure(exc, stage="browser_startup")
            if retry_policy(category).abort_account:
                account_state.mark_failure(category)
                account_failure_category = category
            fatal_error = exc
            results.append(TargetResult(target="运行检查", status="failed", error=str(exc), failure_category=category))

    if account_failure_category is None:
        account_state.mark_ready()

    results = _enrich_results(results, task, history, run_date, settings.artifacts_dir, screenshots)
    finished_at = datetime.now().astimezone().isoformat()
    _write_results(
        settings.artifacts_dir,
        task.task_id,
        dry_run,
        results,
        aliases,
        run_id=run_id,
        account_id=settings.account_id,
        started_at=started_at,
        finished_at=finished_at,
        retry_mode=retry_mode,
        screenshots=screenshots,
    )
    await _notify_dingtalk(settings, task.task_id, dry_run, results, screenshots, retry_mode=retry_mode)
    await _notify_telegram(settings, task.task_id, dry_run, results, retry_mode=retry_mode)
    succeeded = sum(result.status in {"success", "duplicate", "skipped"} for result in results)
    failed = sum(result.status in {"failed", "unconfirmed"} for result in results)
    LOGGER.info("执行结束: 成功/跳过 %d，失败或未确认 %d", succeeded, failed)
    if fatal_error is not None:
        raise fatal_error
    return 1 if failed else 0


def main() -> int:
    args = _parse_cli_args()
    try:
        settings = load_settings(args.env_file)
        with run_lock(settings.artifacts_dir / "run.lock", account_id=settings.account_id):
            return asyncio.run(
                run(
                    dry_run=args.dry_run,
                    env_file=args.env_file,
                    retry_mode=_retry_mode(args),
                )
            )
    except (ConfigError, AuthenticationError, RiskControlError, RateLimitedError, AccountCooldownError, SearchBoxNotReadyError, AlreadyRunningError) as exc:
        print(f"错误: {exc}")
        return 2
    except KeyboardInterrupt:
        print("任务已取消")
        return 130


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="向多个抖音好友发送配置的消息")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只验证登录和好友，不发送消息")
    mode.add_argument("--retry-failed", action="store_true", help="只重试当天明确失败且允许重试的消息")
    mode.add_argument(
        "--retry-unconfirmed",
        action="store_true",
        help="人工重试当天未确认消息；可能重复发送，自动流程不得使用",
    )
    parser.add_argument("--env-file", help="指定 .env 文件路径")
    return parser.parse_args()


def _retry_mode(args: argparse.Namespace) -> RetryMode | None:
    if getattr(args, "retry_failed", False):
        return "failed"
    if getattr(args, "retry_unconfirmed", False):
        return "unconfirmed"
    return None


def _message_plans(history: History, task_id: str, run_date: str, target) -> list[tuple[int, object, str, str]]:
    plans = []
    for message_index, message in enumerate(target.messages):
        message_id = _message_id(message_index, message)
        key = history.key(task_id, run_date, target.identity_key, message_id)
        plans.append((message_index, message, message_id, key))
    return plans


def _select_message_plans(
    history: History,
    plans: list[tuple[int, object, str, str]],
    *,
    dry_run: bool,
    prevent_duplicates: bool,
    retry_mode: RetryMode | None,
) -> tuple[list[tuple[int, object, str, str]], int]:
    if dry_run:
        return plans, 0
    if retry_mode == "failed":
        allowed = history.retryable_failed_keys()
        return [plan for plan in plans if plan[3] in allowed], 0
    if retry_mode == "unconfirmed":
        allowed = history.unconfirmed_keys()
        return [plan for plan in plans if plan[3] in allowed], 0
    if not prevent_duplicates:
        return plans, 0
    selected = [plan for plan in plans if not history.contains(plan[3])]
    return selected, len(plans) - len(selected)


def _persist_target_failure(
    history: History,
    selected: list[tuple[int, object, str, str]],
    *,
    current_key: str | None,
    category,
    error: str,
    target_key: str,
    display_name: str,
    opened: bool,
) -> None:
    if retry_policy(category).abort_account and current_key is None:
        return
    keys = [current_key] if current_key else ([] if opened else [plan[3] for plan in selected])
    plan_by_key = {plan[3]: plan for plan in selected}
    for key in keys:
        if key is None:
            continue
        entry = history.entry(key)
        if entry is None:
            plan = plan_by_key[key]
            began = history.reserve(
                key,
                target_key=target_key,
                display_name=display_name,
                message_id=plan[2],
            )
            if not began:
                continue
        if category == "send_unconfirmed":
            history.mark_unconfirmed(key, error)
        else:
            history.mark_failed(key, category, error)


def _configure_logging(
    artifacts_dir: Path,
    aliases: dict[str, str] | None = None,
    *,
    label: str | None = None,
    reset: bool = False,
) -> None:
    if reset or not LOGGER.handlers:
        for handler in list(LOGGER.handlers):
            LOGGER.removeHandler(handler)
            if isinstance(handler, logging.FileHandler):
                handler.close()
        LOGGER.setLevel(logging.INFO)
        pattern = "%(asctime)s %(levelname)s %(message)s"
        if label:
            pattern = pattern.replace(" %(message)s", f" [{label}] %(message)s")
        formatter = RedactingFormatter(pattern, aliases=aliases)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(artifacts_dir / "run.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
        LOGGER.addHandler(stream_handler)
        return
    # 已有 handler（多账号模式下 run() 内部会再次调用）：只更新脱敏别名。
    for handler in LOGGER.handlers:
        if isinstance(handler.formatter, RedactingFormatter):
            handler.formatter.aliases = dict(aliases or {})


async def _screenshot(page, artifacts_dir: Path, label: str) -> Path | None:
    safe_label = re.sub(r"[^A-Za-z0-9_.\-一-鿿]+", "_", label).strip("_")
    suffix = hashlib.sha1(label.encode("utf-8")).hexdigest()[:6]
    safe_label = f"{safe_label}-{suffix}" if safe_label else f"failure-{suffix}"
    directory = artifacts_dir / "screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{safe_label}.png"
    try:
        await page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        LOGGER.exception("保存截图失败")
        return None


def _write_results(
    artifacts_dir: Path,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    aliases: dict[str, str] | None = None,
    *,
    run_id: str | None = None,
    account_id: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    retry_mode: RetryMode | None = None,
    screenshots: list[Path] | None = None,
) -> None:
    finished = finished_at or datetime.now().astimezone().isoformat()
    payload = {
        "schema_version": 1,
        "run_id": run_id or uuid.uuid4().hex,
        "account_id": account_id,
        "started_at": started_at or finished,
        "finished_at": finished,
        "overall_status": _overall_status(results),
        "task_id": task_id,
        "dry_run": dry_run,
        "retry_mode": retry_mode,
        "results": [_redacted_result(result, aliases) for result in results],
        "targets": [_redacted_result(result, aliases) for result in results],
        "artifacts": [_artifact_reference(path, artifacts_dir) for path in (screenshots or [])],
    }
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    temporary = artifacts_dir / f"result.json.{os.getpid()}.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, artifacts_dir / "result.json")


def _redacted_result(result: TargetResult, aliases: dict[str, str] | None = None) -> dict:
    aliases = dict(aliases or {})
    aliases[result.target] = result.target_alias or aliases.get(result.target, result.target)
    return {
        "target": aliases[result.target],
        "identity": _identity_reference(result.identity),
        "display_label": aliases[result.target],
        "status": result.status,
        "sent": result.sent,
        "error": redact_text(result.error, aliases) if result.error else None,
        "failure_category": result.failure_category,
        "attempt_count": result.attempt_count,
        "first_attempt_at": result.first_attempt_at,
        "last_attempt_at": result.last_attempt_at,
        "retryable": result.retryable,
        "artifacts": list(result.artifacts),
    }


def _overall_status(results: list[TargetResult]) -> str:
    if any(result.failure_category in {"login_required", "risk_control", "rate_limited", "browser_startup"} for result in results):
        return "account_failure"
    if any(result.status == "unconfirmed" for result in results):
        return "unconfirmed"
    if any(result.status == "failed" for result in results):
        return "partial_success" if any(result.status == "success" for result in results) else "failed"
    return "success"


def _enrich_results(results, task, history, run_date: str, artifacts_dir: Path, screenshots: list[Path]):
    enriched = []
    for result in results:
        target = next((item for item in task.targets if item.name == result.target), None)
        if target is None:
            enriched.append(result)
            continue
        entries = []
        for _, _, _, key in _message_plans(history, task.task_id, run_date, target):
            entry = history.entry(key)
            if isinstance(entry, dict):
                entries.append(entry)
        attempts = max((_safe_int(entry.get("attempt_count")) for entry in entries), default=0)
        first = min((entry.get("first_attempt_at") for entry in entries if entry.get("first_attempt_at")), default=None)
        last = max((entry.get("last_attempt_at") for entry in entries if entry.get("last_attempt_at")), default=None)
        category = result.failure_category or next((entry.get("failure_category") for entry in entries if entry.get("failure_category")), None)
        matching_screenshots = [
            path for path in screenshots if result.target_alias and result.target_alias in path.name
        ]
        artifact_refs = tuple(_artifact_reference(path, artifacts_dir) for path in matching_screenshots)
        enriched.append(replace(
            result,
            identity=result.identity or target.identity_key,
            failure_category=category,
            attempt_count=result.attempt_count or attempts,
            first_attempt_at=result.first_attempt_at or first,
            last_attempt_at=result.last_attempt_at or last,
            retryable=result.retryable or (category is not None and manual_retry_allowed(category, attempts or 0)),
            artifacts=result.artifacts or artifact_refs,
        ))
    return enriched


def _safe_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _identity_reference(identity: str | None) -> str | None:
    if not identity:
        return None
    return f"sha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def _artifact_reference(path: Path, artifacts_dir: Path) -> str:
    try:
        return str(path.relative_to(artifacts_dir))
    except ValueError:
        return path.name


async def _notify_dingtalk(
    settings: Settings,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
    retry_mode: RetryMode | None = None,
) -> None:
    if not settings.dingtalk_webhook or not settings.dingtalk_secret:
        return
    try:
        await send_dingtalk_notification(
            settings.dingtalk_webhook,
            settings.dingtalk_secret,
            task_id,
            dry_run,
            results,
            screenshots,
            retry_mode=retry_mode,
        )
        LOGGER.info("钉钉通知发送成功")
    except Exception:
        LOGGER.exception("钉钉通知发送失败，不影响本次任务结果")


async def _notify_telegram(
    settings: Settings,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    retry_mode: RetryMode | None = None,
) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        await send_telegram_notification(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            task_id,
            dry_run,
            results,
            account_id=settings.account_id,
            retry_mode=retry_mode,
        )
        LOGGER.info("Telegram 通知发送成功")
    except Exception:
        # Bot Token 位于 Telegram API URL 中，避免记录异常对象或 traceback，
        # 防止网络错误文本意外回显 Token。
        LOGGER.error("Telegram 通知发送失败，不影响本次任务结果")


def _trace_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "traces" / f"{datetime.now():%Y%m%d-%H%M%S}.zip"


def _message_id(index, message) -> str:
    payload = json.dumps(asdict(message), ensure_ascii=False, sort_keys=True, default=str)
    return f"{index}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"
