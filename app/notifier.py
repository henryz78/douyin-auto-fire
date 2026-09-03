from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from app.models import TargetResult


MAX_RESULTS_PER_SECTION = 15
MAX_MARKDOWN_BYTES = 18_000
TELEGRAM_MAX_MESSAGE_CHARS = 3_900
NOTIFY_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


async def send_dingtalk_notification(
    webhook: str,
    secret: str,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
    retry_mode: str | None = None,
) -> None:
    title, markdown = build_dingtalk_markdown(task_id, dry_run, results, screenshots, retry_mode=retry_mode)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
        "at": {"isAtAll": False},
    }
    await asyncio.to_thread(_post_json, _signed_webhook_url(webhook, secret), payload)


async def send_telegram_notification(
    bot_token: str,
    chat_id: str,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    account_id: str | None = None,
    finished_at: datetime | None = None,
    retry_mode: str | None = None,
) -> None:
    message = build_telegram_message(task_id, dry_run, results, account_id, finished_at, retry_mode)
    for chunk in split_telegram_message(message):
        await asyncio.to_thread(_post_telegram_message, bot_token, chat_id, chunk)


def build_telegram_message(
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    account_id: str | None = None,
    finished_at: datetime | None = None,
    retry_mode: str | None = None,
) -> str:
    successes = [result for result in results if result.status == "success"]
    skipped = [result for result in results if result.status in {"duplicate", "skipped"}]
    unconfirmed = [result for result in results if result.status == "unconfirmed"]
    account_failures = [result for result in results if _is_account_failure(result)]
    failures = [result for result in results if result.status == "failed" and result not in account_failures]
    status = _telegram_status(successes, failures, unconfirmed, account_failures, retry_mode, skipped=skipped)
    mode = "Dry Run（未发送消息）" if dry_run else "正式运行"
    finished = (finished_at or datetime.now(timezone.utc)).astimezone(NOTIFY_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S %z"
    )
    lines = [status, f"任务：{_telegram_text(task_id, limit=100)}"]
    if account_id:
        lines.append(f"账号：{_telegram_text(account_id, limit=100)}")
    lines.extend(
        [
            f"模式：{mode}",
            f"完成时间：{finished}",
            f"结果：成功 {len(successes)}，失败 {len(failures)}，未确认 {len(unconfirmed)}，账号故障 {len(account_failures)}，跳过 {len(skipped)}",
            "",
            f"成功好友（{len(successes)}）：",
        ]
    )
    if successes:
        for result in successes:
            detail = "验证通过" if dry_run else f"已发送 {result.sent} 条"
            lines.append(f"- {_telegram_text(result.target, limit=100)}（{detail}）")
    else:
        lines.append("- 无")
    if skipped:
        lines.extend(["", f"跳过目标（{len(skipped)}，没有新发送）："])
        for result in skipped:
            lines.append(f"- {_telegram_text(result.target, limit=100)}")

    lines.extend(["", f"失败目标（{len(failures)}）："])
    if failures:
        for result in failures:
            error = _telegram_text(result.error or "未知错误", limit=300)
            sent = f"（已发送 {result.sent} 条）" if result.sent else ""
            lines.append(f"- {_telegram_text(result.target, limit=100)}{sent}：{error}")
    else:
        lines.append("- 无")
    if unconfirmed:
        lines.extend(["", f"未确认（{len(unconfirmed)}，不会自动重发）："])
        for result in unconfirmed:
            lines.append(f"- {_telegram_text(result.target, limit=100)}：{_telegram_text(result.error or '发送结果未确认', limit=300)}")
    if account_failures:
        lines.extend(["", f"账号级故障（{len(account_failures)}）："])
        for result in account_failures:
            lines.append(f"- {_telegram_text(result.failure_category or 'unknown', limit=100)}：{_telegram_text(result.error or '账号已停止', limit=300)}")
    return "\n".join(lines)


def split_telegram_message(text: str, max_chars: int = TELEGRAM_MAX_MESSAGE_CHARS) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        else:
            cut += 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    chunks.append(remaining)
    return chunks


def build_dingtalk_markdown(
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
    finished_at: datetime | None = None,
    retry_mode: str | None = None,
) -> tuple[str, str]:
    successes = [result for result in results if result.status == "success"]
    skipped = [result for result in results if result.status in {"duplicate", "skipped"}]
    unconfirmed = [result for result in results if result.status == "unconfirmed"]
    account_failures = [result for result in results if _is_account_failure(result)]
    failures = [result for result in results if result.status == "failed" and result not in account_failures]
    status = _dingtalk_status(successes, failures, unconfirmed, account_failures, retry_mode, skipped=skipped)
    mode = "检查模式（未发送消息）" if dry_run else "正式发送"
    finished = (finished_at or datetime.now(timezone.utc)).astimezone(NOTIFY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")
    title = f"抖音自动发送：{status}"
    lines = [
        f"### {title}",
        "",
        f"> **任务**：{_markdown_text(task_id, limit=100)}  ",
        f"> **模式**：{mode}  ",
        f"> **完成时间**：{finished}  ",
        f"> **结果**：成功 {len(successes)}，失败 {len(failures)}，未确认 {len(unconfirmed)}，账号故障 {len(account_failures)}，跳过 {len(skipped)}",
        "",
        f"#### 成功名单（{len(successes)}）",
    ]
    if successes:
        for index, result in enumerate(successes[:MAX_RESULTS_PER_SECTION], 1):
            detail = "验证通过" if dry_run else f"已发送 {result.sent} 条"
            lines.append(f"{index}. **{_markdown_text(result.target, limit=100)}** - {detail}")
        if len(successes) > MAX_RESULTS_PER_SECTION:
            lines.append(f"- 其余 {len(successes) - MAX_RESULTS_PER_SECTION} 人已省略")
    else:
        lines.append("无")

    if skipped:
        lines.extend(["", f"#### 跳过目标（{len(skipped)}，没有新发送）"])
        for index, result in enumerate(skipped[:MAX_RESULTS_PER_SECTION], 1):
            lines.append(f"{index}. **{_markdown_text(result.target, limit=100)}**")
        if len(skipped) > MAX_RESULTS_PER_SECTION:
            lines.append(f"- 其余 {len(skipped) - MAX_RESULTS_PER_SECTION} 人已省略")

    if unconfirmed:
        lines.extend(["", f"#### 未确认（{len(unconfirmed)}，不会自动重发）"])
        for index, result in enumerate(unconfirmed[:MAX_RESULTS_PER_SECTION], 1):
            lines.append(f"{index}. **{_markdown_text(result.target, limit=100)}**")
            lines.append(f"   - 原因：{_markdown_text(result.error or '发送结果未确认', limit=300)}")

    if account_failures:
        lines.extend(["", f"#### 账号级故障（{len(account_failures)}）"])
        for result in account_failures[:MAX_RESULTS_PER_SECTION]:
            lines.append(f"- **{_markdown_text(result.failure_category or 'unknown')}**：{_markdown_text(result.error or '账号已停止', limit=300)}")

    lines.extend(["", f"#### 失败名单（{len(failures)}）"])
    if failures:
        for index, result in enumerate(failures[:MAX_RESULTS_PER_SECTION], 1):
            error = _markdown_text(result.error or "未知错误", limit=300)
            sent = f"，已发送 {result.sent} 条" if result.sent else ""
            lines.append(f"{index}. **{_markdown_text(result.target, limit=100)}**{sent}")
            lines.append(f"   - 原因：{error}")
        if len(failures) > MAX_RESULTS_PER_SECTION:
            lines.append(f"- 其余 {len(failures) - MAX_RESULTS_PER_SECTION} 人已省略")
    else:
        lines.append("无")

    if screenshots:
        lines.extend(["", "#### 失败截图"])
        lines.extend(f"- `{_markdown_text(path.name, limit=100)}`" for path in screenshots[:MAX_RESULTS_PER_SECTION])
        run_url = _github_run_url()
        if run_url:
            lines.extend(
                [
                    "",
                    f"[打开本次 GitHub Actions 运行并下载截图]({run_url})",
                    "",
                    "> 截图将在任务结束后出现在该次运行底部的 Artifacts 中。",
                ]
            )

    return title, _truncate_utf8("\n".join(lines), MAX_MARKDOWN_BYTES)


def _is_account_failure(result: TargetResult) -> bool:
    return result.failure_category in {"login_required", "risk_control", "rate_limited", "browser_startup"}


def _telegram_status(successes, failures, unconfirmed, account_failures, retry_mode: str | None, skipped=None) -> str:
    skipped = skipped or []
    if account_failures:
        return "⛔ 抖音任务发生账号级故障"
    if unconfirmed:
        return "⚠️ 抖音任务存在未确认发送"
    if failures and successes:
        return "⚠️ 抖音任务部分成功"
    if failures:
        return "❌ 抖音任务存在失败"
    if retry_mode and successes:
        return "✅ 抖音任务重试后恢复成功"
    if skipped and not successes:
        return "⚪ 抖音任务无新发送（已跳过）"
    return "✅ 抖音任务成功"


def _dingtalk_status(successes, failures, unconfirmed, account_failures, retry_mode: str | None, skipped=None) -> str:
    skipped = skipped or []
    if account_failures:
        return "账号级故障"
    if unconfirmed:
        return "存在未确认发送"
    if failures and successes:
        return "部分成功"
    if failures:
        return "存在失败"
    if retry_mode and successes:
        return "重试后恢复成功"
    if skipped and not successes:
        return "无新发送（已跳过）"
    return "全部成功"


def _signed_webhook_url(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    signature = base64.b64encode(hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()).decode()
    parsed = urlsplit(webhook)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((('timestamp', str(timestamp)), ('sign', signature)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _post_json(url: str, payload: dict) -> None:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
        result = json.loads(body)
    except Exception:
        # The request URL contains the signed webhook and the response body can
        # echo request details.  Keep the exception safe for all callers,
        # including code outside main._notify_dingtalk.
        raise RuntimeError("钉钉机器人请求失败") from None
    if not isinstance(result, dict) or result.get("errcode") != 0:
        raise RuntimeError("钉钉机器人返回错误")


def _post_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
        result = json.loads(body)
    except Exception:
        # Bot Token 位于请求 URL 中，绝不把底层网络异常原文向上透传。
        raise RuntimeError("Telegram Bot API 请求失败") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram Bot API 返回错误")


def _github_run_url() -> str | None:
    server = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if not server or not repository or not run_id:
        return None
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"


def _markdown_text(value: str, limit: int | None = None) -> str:
    text = _mask_configured_secrets(" ".join(str(value).splitlines()).strip())
    if limit is not None and len(text) > limit:
        text = f"{text[:limit - 3]}..."
    for character in ("\\", "`", "*", "_", "[", "]", "#", ">", "|"):
        text = text.replace(character, f"\\{character}")
    return text


def _telegram_text(value: str, limit: int | None = None) -> str:
    text = _mask_configured_secrets(" ".join(str(value).splitlines()).strip())
    if limit is not None and len(text) > limit:
        text = f"{text[: max(0, limit - 3)]}..."
    return text


def _mask_configured_secrets(text: str) -> str:
    for name in (
        "DOUYIN_COOKIE",
        "DOUYIN_STORAGE_STATE",
        "DOUYIN_PROXY_SERVER",
        "DOUYIN_PROXY_USERNAME",
        "DOUYIN_PROXY_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DINGTALK_WEBHOOK",
        "DINGTALK_SECRET",
    ):
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix = "\n\n> 通知内容过长，部分内容已省略。"
    available = max_bytes - len(suffix.encode("utf-8"))
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode("utf-8")) <= available:
            low = middle
        else:
            high = middle - 1
    return f"{text[:low]}{suffix}"
