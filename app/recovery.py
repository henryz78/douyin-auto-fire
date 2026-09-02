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
    return RETRY_POLICIES[category]


def manual_retry_allowed(category: FailureCategory | None, attempt_count: int, limit: int = DEFAULT_RETRY_LIMIT) -> bool:
    return attempt_count < limit and retry_policy(category).manual_retry_allowed


def auto_retry_allowed(category: FailureCategory | None, attempt_count: int, limit: int = DEFAULT_RETRY_LIMIT) -> bool:
    return attempt_count < limit and retry_policy(category).auto_retry_allowed
