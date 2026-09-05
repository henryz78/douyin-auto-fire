import json
from datetime import datetime, timedelta, timezone

import pytest

from app.account_state import (
    ACCOUNT_STATE_SCHEMA_VERSION,
    AccountCooldownError,
    AccountLoginRequiredError,
    AccountState,
    build_credential_fingerprint,
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


def test_manual_reset_clears_only_login_required_state(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    state = AccountState(path, "account1")
    state.mark_failure("login_required")

    assert state.reset_login_required() is True
    assert state.data["status"] == "ready"
    state.ensure_runnable()


def test_manual_reset_does_not_bypass_rate_limit_state(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    state = AccountState(path, "account1")
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    state.mark_failure("rate_limited", now=now, cooldown=timedelta(hours=1))

    assert state.reset_login_required() is False
    with pytest.raises(AccountCooldownError):
        state.ensure_runnable(now=now + timedelta(minutes=30))


def test_credential_fingerprint_prefers_storage_state_and_never_exposes_value(tmp_path) -> None:
    storage_path = tmp_path / "storage-state.json"
    storage_path.write_text('{"cookies":[{"name":"sid","value":"secret"}]}', encoding="utf-8")
    cookie = '[{"name":"sid","value":"other"}]'

    fingerprint = build_credential_fingerprint(str(storage_path), cookie)

    assert fingerprint is not None
    assert len(fingerprint) == 64
    assert "secret" not in fingerprint
    assert fingerprint == build_credential_fingerprint(str(storage_path), cookie)
    assert fingerprint != build_credential_fingerprint(None, cookie)


def test_changed_credential_allows_one_login_revalidation_then_locks_new_version(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    old_fingerprint = build_credential_fingerprint(None, '[{"value":"old"}]')
    new_fingerprint = build_credential_fingerprint(None, '[{"value":"new"}]')
    assert old_fingerprint is not None and new_fingerprint is not None

    old = AccountState(path, "account1", credential_fingerprint=old_fingerprint)
    old.mark_failure("login_required")
    with pytest.raises(AccountLoginRequiredError):
        AccountState(path, "account1", credential_fingerprint=old_fingerprint).ensure_runnable()

    changed = AccountState(path, "account1", credential_fingerprint=new_fingerprint)
    changed.ensure_runnable()
    assert changed.data["credential_fingerprint"] == old_fingerprint

    changed.mark_failure("login_required")
    assert json.loads(path.read_text(encoding="utf-8"))["credential_fingerprint"] == new_fingerprint
    with pytest.raises(AccountLoginRequiredError):
        AccountState(path, "account1", credential_fingerprint=new_fingerprint).ensure_runnable()


def test_legacy_login_required_state_with_new_credential_gets_one_validation_attempt(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    legacy = AccountState(path, "account1")
    legacy.mark_failure("login_required")
    assert "credential_fingerprint" not in json.loads(path.read_text(encoding="utf-8"))

    fingerprint = build_credential_fingerprint(None, '[{"value":"new"}]')
    assert fingerprint is not None
    AccountState(path, "account1", credential_fingerprint=fingerprint).ensure_runnable()


def test_ready_state_records_current_credential_fingerprint(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    fingerprint = build_credential_fingerprint(None, '[{"value":"current"}]')
    assert fingerprint is not None

    state = AccountState(path, "account1", credential_fingerprint=fingerprint)
    state.mark_ready()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["credential_fingerprint"] == fingerprint


def test_invalid_persisted_credential_fingerprint_fails_closed(tmp_path) -> None:
    path = tmp_path / "account-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": ACCOUNT_STATE_SCHEMA_VERSION,
                "account_id": "account1",
                "status": "ready",
                "failure_category": None,
                "last_failure_at": None,
                "cooldown_until": None,
                "credential_fingerprint": "not-a-sha256",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="credential_fingerprint 无效"):
        AccountState(path, "account1")
