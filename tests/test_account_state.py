import json
from datetime import datetime, timedelta, timezone

import pytest

from app.account_state import (
    ACCOUNT_STATE_SCHEMA_VERSION,
    AccountCooldownError,
    AccountLoginRequiredError,
    AccountState,
)


def test_account_failure_state_is_atomic_and_schema_versioned(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    state = AccountState(path, "account1")
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)

    state.mark_failure("login_required", now=now)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == ACCOUNT_STATE_SCHEMA_VERSION
    assert payload["account_id"] == "account1"
    assert payload["status"] == "login_required"
    assert payload["failure_category"] == "login_required"
    assert list(tmp_path.glob("*.tmp")) == []


def test_rate_limit_blocks_only_until_cooldown_expires(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    state = AccountState(path, "account1")
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    state.mark_failure("rate_limited", now=now, cooldown=timedelta(hours=1))

    with pytest.raises(AccountCooldownError):
        state.ensure_runnable(now=now + timedelta(minutes=30))

    state.ensure_runnable(now=now + timedelta(hours=1))


def test_account_state_isolated_by_artifact_directory(tmp_path) -> None:
    first = AccountState(tmp_path / "a" / "account-state.json", "a")
    second = AccountState(tmp_path / "b" / "account-state.json", "b")

    first.mark_failure("risk_control")
    second.mark_ready()

    assert first.data["failure_category"] == "risk_control"
    assert second.data["status"] == "ready"


def test_corrupt_account_state_fails_closed(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="账号状态文件损坏"):
        AccountState(path, "account1")


def test_invalid_cooldown_state_fails_closed(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": ACCOUNT_STATE_SCHEMA_VERSION,
                "account_id": "account1",
                "status": "blocked",
                "failure_category": "rate_limited",
                "last_failure_at": None,
                "cooldown_until": None,
            }
        ),
        encoding="utf-8",
    )

    state = AccountState(path, "account1")
    with pytest.raises(ValueError, match="cooldown_until 缺失"):
        state.ensure_runnable()


@pytest.mark.parametrize(
    "status,category,cooldown",
    [
        ("ready", "friend_not_found", None),
        ("login_required", "rate_limited", None),
        ("blocked", "friend_not_found", None),
        ("blocked", "browser_startup", "2026-09-02T10:00:00+00:00"),
    ],
)
def test_invalid_status_category_combinations_fail_closed(
    tmp_path, status: str, category: str, cooldown: str | None
) -> None:
    path = tmp_path / "account-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": ACCOUNT_STATE_SCHEMA_VERSION,
                "account_id": "account1",
                "status": status,
                "failure_category": category,
                "last_failure_at": None,
                "cooldown_until": cooldown,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="组合无效"):
        AccountState(path, "account1")


def test_login_required_state_blocks_automatic_rerun_until_manual_reset(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    state = AccountState(path, "account1")
    state.mark_failure("login_required")

    with pytest.raises(AccountLoginRequiredError, match="手动重置"):
        state.ensure_runnable()

    state.mark_ready()
    state.ensure_runnable()
