from pathlib import Path

import pytest

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
