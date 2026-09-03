from pathlib import Path

import pytest

import app.history as history_module
from app.history import HISTORY_SCHEMA_VERSION, AlreadyRunningError, History, run_lock


def test_history_persists_success(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    history = History(path)
    key = history.key("task", history.run_date("Asia/Shanghai"), "好友A", "0-abc")

    history.mark_success(key)

    assert History(path).contains(key)
    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == HISTORY_SCHEMA_VERSION
    assert payload["entries"][key]["status"] == "success"


def test_legacy_unknown_history_migrates_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_text('{"task:2026-09-02:friend:message": {"status": "unknown"}}', encoding="utf-8")

    history = History(path)
    entry = history.entry("task:2026-09-02:friend:message")

    assert entry["status"] == "unconfirmed"
    assert entry["failure_category"] == "send_unconfirmed"
    assert history.unconfirmed_keys() == {"task:2026-09-02:friend:message"}
    assert history.retryable_failed_keys() == set()


def test_pending_from_interrupted_process_reloads_as_unconfirmed(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    history = History(path)
    history.reserve("task:date:friend:message")

    reloaded = History(path)

    assert reloaded.entry("task:date:friend:message")["status"] == "unconfirmed"
    assert reloaded.retryable_failed_keys() == set()


def test_failed_and_unconfirmed_are_distinct(tmp_path: Path) -> None:
    history = History(tmp_path / "history.json")
    failed_key = "task:date:failed:message"
    uncertain_key = "task:date:uncertain:message"
    history.reserve(failed_key)
    history.mark_failed(failed_key, "friend_not_found", "not found")
    history.reserve(uncertain_key)
    history.mark_unconfirmed(uncertain_key, "send result unknown")

    assert history.entry(failed_key)["status"] == "failed"
    assert failed_key in history.retryable_failed_keys()
    assert history.entry(uncertain_key)["status"] == "unconfirmed"
    assert uncertain_key not in history.retryable_failed_keys()


def test_malformed_failed_unconfirmed_record_is_normalized_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    key = "task:date:friend:message"
    path.write_text(
        __import__("json").dumps({key: {"status": "failed", "failure_category": "send_unconfirmed"}}),
        encoding="utf-8",
    )

    history = History(path)

    assert history.entry(key)["status"] == "unconfirmed"
    assert key not in history.retryable_failed_keys()


def test_retry_limit_is_enforced_centrally(tmp_path: Path) -> None:
    history = History(tmp_path / "history.json")
    key = "task:date:friend:message"
    for _ in range(3):
        history.reserve(key, allow_success_override=True)
        history.mark_failed(key, "transient_network")

    assert history.entry(key)["attempt_count"] == 3
    assert history.retryable_failed_keys(limit=3) == set()


def test_reserve_does_not_overwrite_success_without_explicit_override(tmp_path: Path) -> None:
    history = History(tmp_path / "history.json")
    key = "task:date:friend:message"
    history.reserve(key)
    history.mark_success(key)

    assert history.reserve(key) is False
    assert history.entry(key)["status"] == "success"


def test_corrupt_history_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="发送历史损坏"):
        History(path)


def test_run_lock_rejects_second_process(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"

    with run_lock(path):
        with pytest.raises(AlreadyRunningError):
            with run_lock(path):
                pass

    assert not path.exists()


def test_run_lock_contains_auditable_owner_metadata(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"

    with run_lock(path, run_id="run-1", account_id="account1") as owner:
        payload = __import__("json").loads(path.read_text(encoding="utf-8"))
        assert payload["pid"] == __import__("os").getpid()
        assert payload["run_id"] == "run-1"
        assert payload["account_id"] == "account1"
        assert payload["started_at"]
        assert owner == payload

    assert not path.exists()


def test_current_process_identity_is_live_and_has_start_token() -> None:
    alive, process_started_at = history_module._process_identity(__import__("os").getpid())

    assert alive is True
    assert process_started_at


def test_run_lock_recovers_confirmed_dead_pid(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    path.write_text('{"pid": 999999, "process_started_at": "old", "run_id": "old"}', encoding="utf-8")
    original = history_module._process_identity
    monkeypatch.setattr(
        history_module,
        "_process_identity",
        lambda pid: (False, None) if pid == 999999 else original(pid),
    )

    with run_lock(path, run_id="new"):
        assert __import__("json").loads(path.read_text(encoding="utf-8"))["run_id"] == "new"


def test_run_lock_recovers_pid_reuse(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    path.write_text('{"pid": 123, "process_started_at": "old", "run_id": "old"}', encoding="utf-8")
    original = history_module._process_identity
    monkeypatch.setattr(
        history_module,
        "_process_identity",
        lambda pid: (True, "new-process") if pid == 123 else original(pid),
    )

    with run_lock(path, run_id="new"):
        assert __import__("json").loads(path.read_text(encoding="utf-8"))["run_id"] == "new"


def test_run_lock_corrupt_or_unverifiable_lock_fails_closed(monkeypatch, tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.lock"
    corrupt.write_text("broken", encoding="utf-8")
    with pytest.raises(AlreadyRunningError, match="损坏"):
        with run_lock(corrupt):
            pass

    unknown = tmp_path / "unknown.lock"
    unknown.write_text("123", encoding="utf-8")
    monkeypatch.setattr(history_module, "_process_identity", lambda _pid: (None, None))
    with pytest.raises(AlreadyRunningError, match="无法可靠判断"):
        with run_lock(unknown):
            pass
