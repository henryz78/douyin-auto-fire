from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from app.models import FailureCategory
from app.recovery import RETRY_POLICIES, retry_policy


ACCOUNT_STATE_SCHEMA_VERSION = 1
DEFAULT_ACCOUNT_COOLDOWN = timedelta(hours=1)


class AccountCooldownError(RuntimeError):
    def __init__(self, category: FailureCategory, cooldown_until: str) -> None:
        self.failure_category = category
        self.cooldown_until = cooldown_until
        super().__init__(f"账号仍处于 {category} 冷却期，截止 {cooldown_until}")


class AccountState:
    def __init__(self, path: Path, account_id: str) -> None:
        self.path = path
        self.account_id = account_id
        self.data = self._load()

    def ensure_runnable(self, now: datetime | None = None) -> None:
        category = self.data.get("failure_category")
        cooldown_until = self.data.get("cooldown_until")
        if category not in {"risk_control", "rate_limited"}:
            return
        if not isinstance(cooldown_until, str):
            raise ValueError(f"账号状态 cooldown_until 缺失，为避免绕过冷却限制，任务已停止: {self.path}")
        try:
            deadline = datetime.fromisoformat(cooldown_until)
        except ValueError as exc:
            raise ValueError(f"账号状态 cooldown_until 无效: {self.path}") from exc
        if deadline.tzinfo is None:
            raise ValueError(f"账号状态 cooldown_until 必须包含时区: {self.path}")
        current = now or datetime.now().astimezone()
        if current < deadline:
            raise AccountCooldownError(category, cooldown_until)

    def mark_failure(
        self,
        category: FailureCategory,
        *,
        now: datetime | None = None,
        cooldown: timedelta = DEFAULT_ACCOUNT_COOLDOWN,
    ) -> None:
        policy = retry_policy(category)
        if not policy.abort_account:
            return
        current = now or datetime.now().astimezone()
        cooldown_until = (current + cooldown).isoformat() if policy.cooldown_required else None
        status = "login_required" if category == "login_required" else "blocked"
        self.data = {
            "schema_version": ACCOUNT_STATE_SCHEMA_VERSION,
            "account_id": self.account_id,
            "status": status,
            "failure_category": category,
            "last_failure_at": current.isoformat(),
            "cooldown_until": cooldown_until,
        }
        self._save()

    def mark_ready(self) -> None:
        self.data = {
            "schema_version": ACCOUNT_STATE_SCHEMA_VERSION,
            "account_id": self.account_id,
            "status": "ready",
            "failure_category": None,
            "last_failure_at": self.data.get("last_failure_at"),
            "cooldown_until": None,
        }
        self._save()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "schema_version": ACCOUNT_STATE_SCHEMA_VERSION,
                "account_id": self.account_id,
                "status": "ready",
                "failure_category": None,
                "last_failure_at": None,
                "cooldown_until": None,
            }
        except json.JSONDecodeError as exc:
            raise ValueError(f"账号状态文件损坏，为避免绕过冷却限制，任务已停止: {self.path}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != ACCOUNT_STATE_SCHEMA_VERSION:
            raise ValueError(f"不支持的账号状态 schema_version: {value.get('schema_version') if isinstance(value, dict) else None}")
        if value.get("account_id") != self.account_id:
            raise ValueError(f"账号状态文件与当前账号不匹配: {self.path}")
        if value.get("status") not in {"ready", "login_required", "blocked"}:
            raise ValueError(f"账号状态 status 无效: {self.path}")
        category = value.get("failure_category")
        if category is not None and category not in RETRY_POLICIES:
            raise ValueError(f"账号状态 failure_category 无效: {self.path}")
        return value

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
