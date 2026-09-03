from __future__ import annotations

from dataclasses import dataclass

from app.models import FailureCategory


DEFAULT_RETRY_LIMIT = 3


@dataclass(frozen=True)
class RetryPolicy:
    auto_retry_allowed: bool
    manual_retry_allowed: bool
    abort_account: bool
    cooldown_required: bool


RETRY_POLICIES: dict[FailureCategory, RetryPolicy] = {
    "transient_network": RetryPolicy(True, True, False, False),
    "navigation_timeout": RetryPolicy(True, True, False, False),
    "browser_startup": RetryPolicy(True, True, True, False),
    "selector_not_ready": RetryPolicy(False, True, False, False),
    "friend_not_found": RetryPolicy(False, True, False, False),
    "login_required": RetryPolicy(False, False, True, False),
    "risk_control": RetryPolicy(False, False, True, True),
    "rate_limited": RetryPolicy(False, False, True, True),
    "send_unconfirmed": RetryPolicy(False, True, False, False),
    "send_rejected": RetryPolicy(False, True, False, False),
    "non_retryable": RetryPolicy(False, False, False, False),
}


def retry_policy(category: FailureCategory | None) -> RetryPolicy:
    if category is None:
        return RETRY_POLICIES["non_retryable"]
    return RETRY_POLICIES.get(category, RETRY_POLICIES["non_retryable"])


def manual_retry_allowed(category: FailureCategory | None, attempt_count: int, limit: int = DEFAULT_RETRY_LIMIT) -> bool:
    return attempt_count < limit and retry_policy(category).manual_retry_allowed


def auto_retry_allowed(category: FailureCategory | None, attempt_count: int, limit: int = DEFAULT_RETRY_LIMIT) -> bool:
    return attempt_count < limit and retry_policy(category).auto_retry_allowed


def classify_failure(exc: Exception, *, stage: str) -> FailureCategory:
    name = type(exc).__name__
    message = str(exc)
    lowered = message.lower()
    uppered = message.upper()

    if name == "AuthenticationError":
        return "login_required"
    if name == "RiskControlError":
        return "risk_control"
    if name == "RateLimitedError":
        return "rate_limited"
    explicit_category = getattr(exc, "failure_category", None)
    if explicit_category in RETRY_POLICIES:
        return explicit_category
    if name == "SearchBoxNotReadyError":
        return "selector_not_ready"
    if stage == "browser_startup":
        return "browser_startup"
    if "搜索不到目标好友" in message:
        return "friend_not_found"
    if any(
        marker in message
        for marker in (
            "无法确认是否发送成功",
            "发送状态未能确认",
            "没有检测到新的已发送消息",
            "没有检测到新的消息",
        )
    ):
        return "send_unconfirmed"
    if "发送失败" in message and "重试" in message:
        return "send_rejected"
    if "找不到" in message or "未能写入" in message or "未就绪" in message:
        return "selector_not_ready"
    transient_markers = (
        "ERR_CONNECTION_CLOSED",
        "ERR_CONNECTION_RESET",
        "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_ABORTED",
        "ERR_NETWORK_CHANGED",
        "ERR_NETWORK_ACCESS_DENIED",
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_INTERNET_DISCONNECTED",
        "ERR_NAME_NOT_RESOLVED",
    )
    if any(marker in uppered for marker in transient_markers):
        return "transient_network"
    if "ERR_TIMED_OUT" in uppered or "timeout" in lowered or "超时" in message:
        return "navigation_timeout" if stage in {"navigation", "target_open"} else "transient_network"
    return "non_retryable"
