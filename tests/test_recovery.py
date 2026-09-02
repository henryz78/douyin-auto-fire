from app.recovery import auto_retry_allowed, manual_retry_allowed, retry_policy


def test_unconfirmed_is_never_auto_retried() -> None:
    policy = retry_policy("send_unconfirmed")

    assert policy.auto_retry_allowed is False
    assert policy.manual_retry_allowed is True
    assert auto_retry_allowed("send_unconfirmed", 1) is False


def test_account_level_failures_abort_current_account() -> None:
    assert retry_policy("login_required").abort_account is True
    assert retry_policy("risk_control").abort_account is True
    assert retry_policy("rate_limited").cooldown_required is True


def test_retry_limit_applies_to_manual_and_auto_retry() -> None:
    assert manual_retry_allowed("transient_network", 2, limit=3) is True
    assert auto_retry_allowed("transient_network", 2, limit=3) is True
    assert manual_retry_allowed("transient_network", 3, limit=3) is False
    assert auto_retry_allowed("transient_network", 3, limit=3) is False
