from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import FailureCategory, TargetStatus
from app.recovery import DEFAULT_RETRY_LIMIT, manual_retry_allowed


HISTORY_SCHEMA_VERSION = 2


class AlreadyRunningError(RuntimeError):
    pass


class History:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = self._load()

    def run_date(self, timezone: str) -> str:
        try:
            return datetime.now(ZoneInfo(timezone)).date().isoformat()
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区: {timezone}，请安装 tzdata 或修正配置") from exc

    def key(self, task_id: str, run_date: str, target: str, message_id: str) -> str:
        return f"{task_id}:{run_date}:{target}:{message_id}"

    def contains(self, key: str) -> bool:
        return key in self.entries

    def entry(self, key: str) -> dict | None:
        value = self.entries.get(key)
        return dict(value) if isinstance(value, dict) else None

    def reserve(
        self,
        key: str,
        *,
        target_key: str | None = None,
        display_name: str | None = None,
        message_id: str | None = None,
        allow_success_override: bool = False,
    ) -> bool:
        previous = self.entry(key) or {}
        if previous.get("status") == "success" and not allow_success_override:
            return False
        now = _now()
        attempt_count = _attempt_count(previous) + 1
        self.entries[key] = {
            "status": "pending",
            "target_key": target_key,
            "display_name": display_name,
            "message_id": message_id,
            "attempt_count": attempt_count,
            "first_attempt_at": previous.get("first_attempt_at") or previous.get("started_at") or now,
            "last_attempt_at": now,
            "started_at": now,
            "failure_category": None,
            "error": None,
        }
        self._save()
        return True

    def mark_success(self, key: str) -> None:
        self._transition(key, "success")

    def mark_failed(self, key: str, category: FailureCategory, error: str | None = None) -> None:
        if category == "send_unconfirmed":
            raise ValueError("send_unconfirmed 必须写入 unconfirmed 状态")
        self._transition(key, "failed", category=category, error=error)

    def mark_unconfirmed(self, key: str, error: str | None = None) -> None:
        self._transition(key, "unconfirmed", category="send_unconfirmed", error=error)

    def mark_skipped(self, key: str, error: str | None = None) -> None:
        self._transition(key, "skipped", error=error)

    def mark_duplicate(self, key: str) -> None:
        # The durable success/failed/unconfirmed record must remain authoritative.
        # Duplicate is a run result, not a replacement for the stored send state.
        if key not in self.entries:
            self._transition(key, "duplicate")

    def retryable_failed_keys(self, limit: int = DEFAULT_RETRY_LIMIT) -> set[str]:
        result: set[str] = set()
        for key, entry in self.entries.items():
            if not isinstance(entry, dict) or entry.get("status") != "failed":
                continue
            category = entry.get("failure_category")
            if manual_retry_allowed(category, _attempt_count(entry), limit):
                result.add(key)
        return result

    def unconfirmed_keys(self) -> set[str]:
        return {
            key
            for key, entry in self.entries.items()
            if isinstance(entry, dict) and entry.get("status") == "unconfirmed"
        }

    def _transition(
        self,
        key: str,
        status: TargetStatus,
        *,
        category: FailureCategory | None = None,
        error: str | None = None,
    ) -> None:
        previous = self.entry(key) or {}
        now = _now()
        self.entries[key] = {
            **previous,
            "status": status,
            "failure_category": category,
            "error": error,
            "attempt_count": max(1, _attempt_count(previous)),
            "first_attempt_at": previous.get("first_attempt_at") or previous.get("started_at") or now,
            "last_attempt_at": previous.get("last_attempt_at") or previous.get("started_at") or now,
            "finished_at": now,
        }
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        payload = {"schema_version": HISTORY_SCHEMA_VERSION, "entries": self.entries}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            if "schema_version" in value:
                version = value.get("schema_version")
                entries = value.get("entries")
                if version != HISTORY_SCHEMA_VERSION or not isinstance(entries, dict):
                    raise ValueError(f"不支持的发送历史 schema_version: {version}")
                return {key: _normalize_entry(entry) for key, entry in entries.items()}
            # Legacy v1 was a bare key -> entry mapping. Unknown meant that a
            # send may already have happened, so migrate it to fail-closed
            # unconfirmed rather than a retryable failure.
            return {key: _normalize_entry(entry) for key, entry in value.items()}
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"发送历史损坏，为避免重复发送，任务已停止: {self.path}") from exc


def _normalize_entry(value: object) -> dict:
    if not isinstance(value, dict):
        return {"status": "unconfirmed", "failure_category": "send_unconfirmed", "attempt_count": 1}
    entry = dict(value)
    status = entry.get("status")
    if status in {"unknown", "pending"} or status not in {
        "success",
        "failed",
        "unconfirmed",
        "skipped",
        "duplicate",
    }:
        entry["status"] = "unconfirmed"
        entry["failure_category"] = "send_unconfirmed"
    elif entry.get("failure_category") not in {
        None,
        "transient_network",
        "navigation_timeout",
        "browser_startup",
        "selector_not_ready",
        "friend_not_found",
        "login_required",
        "risk_control",
        "rate_limited",
        "send_unconfirmed",
        "send_rejected",
        "non_retryable",
    }:
        entry["failure_category"] = "non_retryable"
    entry["attempt_count"] = max(1, _attempt_count(entry))
    return entry


def _attempt_count(entry: dict) -> int:
    try:
        return max(0, int(entry.get("attempt_count") or 0))
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now().astimezone().isoformat()


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AlreadyRunningError(f"已有任务正在运行；如确认没有进程，请删除 {path}") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
