from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


MessageType = Literal["text", "image", "douyin_sticker", "random"]
TargetStatus = Literal["pending", "success", "failed", "unconfirmed", "skipped", "duplicate"]
FailureCategory = Literal[
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
]


@dataclass(frozen=True)
class ProxySettings:
    server: str
    username: str | None = None
    password: str | None = None

    def as_playwright(self) -> dict[str, str]:
        proxy = {"server": self.server}
        if self.username is not None:
            proxy["username"] = self.username
        if self.password is not None:
            proxy["password"] = self.password
        return proxy


@dataclass(frozen=True)
class Message:
    type: MessageType
    content: str | None = None
    path: Path | None = None
    sticker: str | None = None
    choices: tuple["Message", ...] = ()


@dataclass(frozen=True)
class Target:
    name: str
    messages: tuple[Message, ...]
    remark_name: str | None = None
    nickname: str | None = None
    unique_id: str | None = None
    short_id: str | None = None
    sec_uid: str | None = None

    @property
    def display_name(self) -> str:
        return self.name

    @property
    def identity_key(self) -> str:
        return self.sec_uid or self.unique_id or self.short_id or self.name

    def search_candidates(self) -> tuple[str, ...]:
        values = (self.sec_uid, self.unique_id, self.short_id, self.remark_name, self.nickname, self.name)
        return tuple(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))

    def confirmation_names(self) -> tuple[str, ...]:
        values = (self.remark_name, self.nickname, self.name)
        return tuple(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


@dataclass(frozen=True)
class Sticker:
    name: str
    category: str | None = None
    accessible_name: str | None = None
    fallback_index: int | None = None


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    timezone: str
    targets: tuple[Target, ...]
    stickers: dict[str, Sticker]
    interval_min: float
    interval_max: float
    continue_on_error: bool
    prevent_duplicates: bool
    target_open_retries: int = 1
    target_open_timeout_seconds: float = 15.0


@dataclass(frozen=True)
class Settings:
    task_config_path: Path
    storage_state: str | None
    cookie: str | None
    headless: bool
    browser_path: str | None
    artifacts_dir: Path
    trace: bool
    dingtalk_webhook: str | None = None
    dingtalk_secret: str | None = None
    proxy: ProxySettings | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class TargetResult:
    target: str
    status: TargetStatus
    sent: int = 0
    error: str | None = None
    target_alias: str | None = None
    failure_category: FailureCategory | None = None
    attempt_count: int = 0
    first_attempt_at: str | None = None
    last_attempt_at: str | None = None
    retryable: bool = False
    artifacts: tuple[str, ...] = ()
