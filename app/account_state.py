from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from app.models import FailureCategory
from app.recovery import RETRY_POLICIES, retry_policy


ACCOUNT_STATE_SCHEMA_VERSION = 1
DEFAULT_ACCOUNT_COOLDOWN = timedelta(hours=1)


def build_credential_fingerprint(
    storage_state: str | None,
    cookie: str | None,
) -> str | None:
    """Return a non-sensitive fingerprint for the credential in use.

    Storage State is the browser's preferred credential source, so it wins
    when both values are configured.  A file is fingerprinted by its bytes;
    inline JSON is fingerprinted as supplied.  Only the SHA-256 digest is
    persisted, never the credential itself.
    """

    if storage_state:
        return _credential_fingerprint("storage_state", storage_state)
    if cookie:
        return _credential_fingerprint("cookie", cookie)
    return None


def _credential_fingerprint(kind: str, value: str) -> str:
    try:
        candidate = Path(value).expanduser()
        if candidate.is_file():
            payload = candidate.read_bytes()
        else:
            payload = value.encode("utf-8")
    except (OSError, UnicodeError, ValueError):
        # An inline JSON value can be too long to be a valid Windows path.
        # Hashing the value still gives it a stable version without exposing
        # it in state or logs; browser validation will report malformed input.
        payload = value.encode("utf-8")
    material = kind.encode("ascii") + b"\0" + payload
    return hashlib.sha256(material).hexdigest()


def _valid_fingerprint(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


class AccountCooldownError(RuntimeError):
    def __init__(self, category: FailureCategory, cooldown_until: str) -> None:
        self.failure_category = category
        self.cooldown_until = cooldown_until
        super().__init__(f"账号仍处于 {category} 冷却期，截止 {cooldown_until}")


class AccountLoginRequiredError(RuntimeError):
    failure_category = "login_required"

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"账号登录态已失效，请更新凭证后重试；若凭证未改变，请手动重置账号状态: {path}")


class AccountState:
    def __init__(
        self,
        path: Path,
        account_id: str,
        *,
        credential_fingerprint: str | None = None,
    ) -> None:
        if credential_fingerprint is not None and not _valid_fingerprint(credential_fingerprint):
            raise ValueError("凭证指纹无效")
        self.path = path
        self.account_id = account_id
        self.credential_fingerprint = credential_fingerprint
        self.data = self._load()

    def ensure_runnable(self, now: datetime | None = None) -> None:
        status = self.data.get("status")
        category = self.data.get("failure_category")
        cooldown_until = self.data.get("cooldown_until")
        if status == "login_required":
            stored_fingerprint = self.data.get("credential_fingerprint")
            if (
                self.credential_fingerprint is not None
                and (
                    stored_fingerprint is None
                    or not hmac.compare_digest(stored_fingerprint, self.credential_fingerprint)
                )
            ):
                # A changed credential gets one fresh browser validation.  We
                # do not clear the state here: if validation fails, mark_failure
                # records the new fingerprint and the account is locked again.
                return
            # Do not repeatedly launch a known-invalid account when the same
            # credential is still configured.  Manual reset remains available
            # for an operator who has confirmed the login state independently.
            raise AccountLoginRequiredError(self.path)
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
        if self.credential_fingerprint is not None:
            self.data["credential_fingerprint"] = self.credential_fingerprint
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
        if self.credential_fingerprint is not None:
            self.data["credential_fingerprint"] = self.credential_fingerprint
        self._save()

    def reset_login_required(self) -> bool:
        """Explicitly clear only a persisted login-required state.

        This is an operator action for a manually verified account.  Risk
        control and rate-limit blocks are intentionally left untouched so a
        generic reset cannot bypass their cooldown or safety boundary.
        """
        if self.data.get("status") != "login_required":
            return False
        self.mark_ready()
        return True

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
        stored_fingerprint = value.get("credential_fingerprint")
        if stored_fingerprint is not None and not _valid_fingerprint(stored_fingerprint):
            raise ValueError(f"账号状态 credential_fingerprint 无效: {self.path}")
        if value.get("status") not in {"ready", "login_required", "blocked"}:
            raise ValueError(f"账号状态 status 无效: {self.path}")
        status = value["status"]
        category = value.get("failure_category")
        if category is not None and category not in RETRY_POLICIES:
            raise ValueError(f"账号状态 failure_category 无效: {self.path}")
        cooldown_until = value.get("cooldown_until")
        if status == "ready":
            if category is not None or cooldown_until is not None:
                raise ValueError(f"账号状态 status/failure_category 组合无效: {self.path}")
        elif status == "login_required":
            if category != "login_required" or cooldown_until is not None:
                raise ValueError(f"账号状态 status/failure_category 组合无效: {self.path}")
        elif category not in {"browser_startup", "risk_control", "rate_limited"}:
            raise ValueError(f"账号状态 status/failure_category 组合无效: {self.path}")
        elif category == "browser_startup" and cooldown_until is not None:
            raise ValueError(f"账号状态 cooldown_until 组合无效: {self.path}")
        return value

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
