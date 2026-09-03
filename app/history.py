from __future__ import annotations

import json
import os
import uuid
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
        # Keep the argument for source compatibility with early callers, but
        # never permit a strong success record to be reopened. A retry command
        # must prove that the durable state is failed/unconfirmed before it
        # reaches this method; overriding success would reintroduce duplicates.
        if previous.get("status") == "success":
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
    elif entry.get("status") == "failed" and entry.get("failure_category") == "send_unconfirmed":
        # A legacy or partially-written record may have combined the old
        # failure label with an uncertain send outcome. Preserve the
        # fail-closed meaning instead of exposing it to --retry-failed.
        entry["status"] = "unconfirmed"
    elif entry.get("status") == "unconfirmed":
        # The status itself is the safety boundary; never let a stale or
        # malformed category make an uncertain send look retryable.
        entry["failure_category"] = "send_unconfirmed"
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
def run_lock(
    path: Path,
    *,
    run_id: str | None = None,
    account_id: str | None = None,
) -> Iterator[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = {
        "schema_version": 1,
        "pid": os.getpid(),
        "process_started_at": _process_identity(os.getpid())[1],
        "started_at": _now(),
        "run_id": run_id or uuid.uuid4().hex,
        "account_id": account_id,
    }
    descriptor = _create_lock(path, owner)
    try:
        yield dict(owner)
    finally:
        os.close(descriptor)
        _remove_owned_lock(path, owner["run_id"])


def _create_lock(path: Path, owner: dict) -> int:
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            if not _existing_lock_is_stale(path):
                raise AlreadyRunningError(f"已有任务正在运行，锁文件: {path}") from exc
            _remove_confirmed_stale_lock(path)
            continue
        try:
            payload = json.dumps(owner, ensure_ascii=False, indent=2).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise


def _existing_lock_is_stale(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise AlreadyRunningError(f"无法安全读取现有锁，拒绝并发运行: {path}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Compatibility with the legacy lock format that contained only a PID.
        try:
            pid = int(raw)
        except ValueError as exc:
            raise AlreadyRunningError(f"锁文件损坏，无法可靠判断是否失效: {path}") from exc
        stored_identity = None
    else:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("pid"), int)
            or isinstance(value.get("pid"), bool)
        ):
            raise AlreadyRunningError(f"锁文件损坏，无法可靠判断是否失效: {path}")
        pid = value["pid"]
        stored_identity = value.get("process_started_at")
        if stored_identity is not None and (
            not isinstance(stored_identity, str) or not stored_identity
        ):
            raise AlreadyRunningError(f"锁文件损坏，无法可靠判断是否失效: {path}")

    alive, current_identity = _process_identity(pid)
    if alive is False:
        return True
    if alive is None:
        raise AlreadyRunningError(f"无法可靠判断锁持有进程是否存在: pid={pid}")
    if stored_identity is not None and current_identity is not None and stored_identity != current_identity:
        # The PID is alive but belongs to another process, so the original lock
        # owner has exited and the PID has been reused.
        return True
    return False


def _remove_confirmed_stale_lock(path: Path) -> None:
    # Re-evaluate immediately before deletion. If another process replaced the
    # lock, the second check will fail closed instead of deleting its live lock.
    if not _existing_lock_is_stale(path):
        raise AlreadyRunningError(f"锁状态在恢复期间发生变化，拒绝并发运行: {path}")
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_owned_lock(path: Path, run_id: str) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(value, dict) and value.get("run_id") == run_id:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _process_identity(pid: int) -> tuple[bool | None, str | None]:
    if pid <= 0:
        return False, None
    if os.name == "nt":
        return _windows_process_identity(pid)
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        stat = proc_stat.read_text(encoding="ascii")
    except FileNotFoundError:
        return False, None
    except OSError:
        stat = None
    if stat:
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) >= 20:
            return True, fields[19]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return None, None
    except OSError:
        return None, None
    return True, None


def _windows_process_identity(pid: int) -> tuple[bool | None, str | None]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        error = ctypes.get_last_error()
        if error in {87, 1168}:
            return False, None
        return None, None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            process,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None, None
        token = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return True, str(token)
    finally:
        kernel32.CloseHandle(process)
