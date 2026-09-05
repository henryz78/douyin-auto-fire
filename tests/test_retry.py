from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.main as main_module
from app.douyin import PageOperationError
from app.history import History
from app.models import Message, Settings, Target, TaskConfig


def _settings(tmp_path) -> Settings:
    return Settings(
        task_config_path=tmp_path / "config.json",
        storage_state=None,
        cookie="[]",
        headless=True,
        browser_path=None,
        artifacts_dir=tmp_path / "artifacts",
        trace=False,
    )


def _task() -> TaskConfig:
    message = Message(type="text", content="测试")
    return TaskConfig(
        task_id="daily-streak",
        timezone="Asia/Shanghai",
        targets=(Target(name="好友A", messages=(message,)), Target(name="好友B", messages=(message,))),
        stickers={},
        interval_min=0,
        interval_max=0,
        continue_on_error=True,
        prevent_duplicates=False,
    )


def _prepare_history(settings: Settings, task: TaskConfig) -> tuple[History, str, str]:
    history = History(settings.artifacts_dir / "history.json")
    run_date = history.run_date(task.timezone)
    keys = []
    for target in task.targets:
        message = target.messages[0]
        message_id = main_module._message_id(0, message)
        keys.append(history.key(task.task_id, run_date, target.name, message_id))
    return history, keys[0], keys[1]


def test_message_plan_uses_stable_identity_key(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(
        name="新昵称",
        sec_uid="sec_1",
        messages=(Message(type="text", content="你好"),),
    )

    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    assert ":sec_1:" in plans[0][3]
    assert ":新昵称:" not in plans[0][3]


def test_stable_identity_honors_legacy_name_keyed_success(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(
        name="好友A",
        sec_uid="sec_1",
        messages=(Message(type="text", content="你好"),),
    )
    message_id = main_module._message_id(0, target.messages[0])
    legacy_key = history.key("task", "2026-09-02", target.name, message_id)
    history.reserve(legacy_key)
    history.mark_success(legacy_key)
    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    selected, duplicates = main_module._exclude_legacy_terminal_states(
        history, "task", "2026-09-02", target, plans, retry_mode=None
    )

    assert selected == []
    assert duplicates == 1


def test_stable_identity_honors_legacy_nickname_success_after_rename(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(
        name="新昵称",
        nickname="旧昵称",
        sec_uid="sec_1",
        messages=(Message(type="text", content="你好"),),
    )
    message_id = main_module._message_id(0, target.messages[0])
    legacy_key = history.key("task", "2026-09-02", "旧昵称", message_id)
    history.reserve(legacy_key)
    history.mark_success(legacy_key)
    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    selected, duplicates = main_module._exclude_legacy_terminal_states(
        history, "task", "2026-09-02", target, plans, retry_mode=None
    )

    assert selected == []
    assert duplicates == 1


def test_legacy_nickname_failure_never_authorizes_retry_after_rename(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(
        name="新昵称",
        nickname="旧昵称",
        sec_uid="sec_1",
        messages=(Message(type="text", content="你好"),),
    )
    message_id = main_module._message_id(0, target.messages[0])
    legacy_key = history.key("task", "2026-09-02", "旧昵称", message_id)
    history.reserve(legacy_key)
    history.mark_failed(legacy_key, "friend_not_found")
    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    selected = main_module._include_legacy_retry_plans(
        history,
        "task",
        "2026-09-02",
        target,
        plans,
        [],
        retry_mode="failed",
    )

    assert selected == []


@pytest.mark.asyncio
async def test_normal_run_does_not_retry_unconfirmed_or_success_when_duplicates_disabled(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    history, success_key, uncertain_key = _prepare_history(settings, task)
    history.reserve(success_key)
    history.mark_success(success_key)
    history.reserve(uncertain_key)
    history.mark_unconfirmed(uncertain_key)
    chat, send = _mock_runtime(monkeypatch, settings, task)

    assert main_module._select_message_plans(
        history,
        main_module._message_plans(history, task.task_id, history.run_date(task.timezone), task.targets[0]),
        dry_run=False,
        prevent_duplicates=False,
        retry_mode=None,
    )[0] == []
    assert await main_module.run() == 1
    chat.open_target.assert_not_awaited()
    send.assert_not_awaited()


def test_legacy_unconfirmed_is_only_exposed_to_explicit_manual_retry(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(
        name="好友A",
        sec_uid="sec_1",
        messages=(Message(type="text", content="你好"),),
    )
    message_id = main_module._message_id(0, target.messages[0])
    legacy_key = history.key("task", "2026-09-02", target.name, message_id)
    history.reserve(legacy_key)
    history.mark_unconfirmed(legacy_key)
    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    normal = main_module._exclude_legacy_terminal_states(
        history, "task", "2026-09-02", target, plans, retry_mode=None
    )
    manual = main_module._include_legacy_retry_plans(
        history, "task", "2026-09-02", target, plans, [], retry_mode="unconfirmed"
    )

    assert normal[0] == []
    assert manual[0][3] == legacy_key


def test_identity_aliases_never_create_two_retry_plans(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(
        name="好友A",
        sec_uid="sec_1",
        messages=(Message(type="text", content="你好"),),
    )
    message_id = main_module._message_id(0, target.messages[0])
    current_key = history.key("task", "2026-09-02", target.identity_key, message_id)
    legacy_key = history.key("task", "2026-09-02", target.name, message_id)
    history.reserve(current_key)
    history.mark_unconfirmed(current_key)
    history.reserve(legacy_key)
    history.mark_unconfirmed(legacy_key)
    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    selected, _ = main_module._select_message_plans(
        history,
        plans,
        dry_run=False,
        prevent_duplicates=False,
        retry_mode="unconfirmed",
    )
    selected = main_module._include_legacy_retry_plans(
        history,
        "task",
        "2026-09-02",
        target,
        plans,
        selected,
        retry_mode="unconfirmed",
    )
    selected, _ = main_module._exclude_legacy_terminal_states(
        history,
        "task",
        "2026-09-02",
        target,
        selected,
        retry_mode="unconfirmed",
    )

    assert [plan[3] for plan in selected] == [current_key]


def test_stable_success_blocks_legacy_failed_retry(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(
        name="好友A",
        sec_uid="sec_1",
        messages=(Message(type="text", content="你好"),),
    )
    message_id = main_module._message_id(0, target.messages[0])
    current_key = history.key("task", "2026-09-02", target.identity_key, message_id)
    legacy_key = history.key("task", "2026-09-02", target.name, message_id)
    history.reserve(current_key)
    history.mark_success(current_key)
    history.reserve(legacy_key)
    history.mark_failed(legacy_key, "friend_not_found")
    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    selected = main_module._include_legacy_retry_plans(
        history,
        "task",
        "2026-09-02",
        target,
        plans,
        [],
        retry_mode="failed",
    )

    assert selected == []


def test_normal_flow_only_auto_retries_policy_allowed_failures(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(name="好友A", messages=(Message(type="text", content="你好"),))
    plans = main_module._message_plans(history, "task", "2026-09-02", target)

    history.reserve(plans[0][3])
    history.mark_failed(plans[0][3], "friend_not_found")
    selected, _ = main_module._select_message_plans(
        history,
        plans,
        dry_run=False,
        prevent_duplicates=False,
        retry_mode=None,
    )

    assert selected == []

    history.reserve(plans[0][3], allow_success_override=True)
    history.mark_failed(plans[0][3], "transient_network")
    selected, _ = main_module._select_message_plans(
        history,
        plans,
        dry_run=False,
        prevent_duplicates=False,
        retry_mode=None,
    )

    assert selected == plans


def test_target_open_failure_increments_existing_failed_attempt(tmp_path) -> None:
    history = History(tmp_path / "history.json")
    target = Target(name="好友A", messages=(Message(type="text", content="你好"),))
    plans = main_module._message_plans(history, "task", "2026-09-02", target)
    key = plans[0][3]
    history.reserve(key)
    history.mark_failed(key, "transient_network", "首次打开失败")

    main_module._persist_target_failure(
        history,
        plans,
        current_key=None,
        category="transient_network",
        error="再次打开失败",
        target_key=target.identity_key,
        display_name=target.name,
        opened=False,
    )

    entry = history.entry(key)
    assert entry["status"] == "failed"
    assert entry["attempt_count"] == 2
    assert entry["error"] == "再次打开失败"


def _mock_runtime(monkeypatch, settings, task):
    page = MagicMock()
    session = SimpleNamespace(page=page, context=MagicMock())

    @asynccontextmanager
    async def fake_open_douyin(_settings):
        yield session

    chat = MagicMock()
    chat.open_target = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(main_module, "load_settings", lambda _env=None: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _settings: task)
    monkeypatch.setattr(main_module, "open_douyin", fake_open_douyin)
    monkeypatch.setattr(main_module, "open_private_messages", AsyncMock())
    monkeypatch.setattr(main_module, "DouyinChat", MagicMock(return_value=chat))
    monkeypatch.setattr(main_module, "verify_login", AsyncMock())
    monkeypatch.setattr(main_module, "send_message", send)
    monkeypatch.setattr(main_module, "_write_results", MagicMock())
    monkeypatch.setattr(main_module, "_notify_dingtalk", AsyncMock())
    monkeypatch.setattr(main_module, "_notify_telegram", AsyncMock())
    monkeypatch.setattr(main_module, "_configure_logging", lambda *_args, **_kwargs: None)
    return chat, send


@pytest.mark.asyncio
async def test_retry_failed_only_processes_retryable_failed_and_never_success(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    history, success_key, failed_key = _prepare_history(settings, task)
    history.reserve(success_key)
    history.mark_success(success_key)
    history.reserve(failed_key)
    history.mark_failed(failed_key, "friend_not_found")
    chat, send = _mock_runtime(monkeypatch, settings, task)

    assert await main_module.run(retry_mode="failed") == 0

    chat.open_target.assert_awaited_once_with("好友B", retries=1)
    assert send.await_count == 1
    reloaded = History(settings.artifacts_dir / "history.json")
    assert reloaded.entry(success_key)["status"] == "success"
    assert reloaded.entry(failed_key)["status"] == "success"


@pytest.mark.asyncio
async def test_retry_failed_never_selects_unconfirmed(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    history, uncertain_key, _ = _prepare_history(settings, task)
    history.reserve(uncertain_key)
    history.mark_unconfirmed(uncertain_key)
    chat, send = _mock_runtime(monkeypatch, settings, task)

    assert await main_module.run(retry_mode="failed") == 0

    chat.open_target.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_target_failure_does_not_change_history_or_success(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    history, success_key, _ = _prepare_history(settings, task)
    history.reserve(success_key)
    history.mark_success(success_key)
    before = history.entry(success_key)
    chat, send = _mock_runtime(monkeypatch, settings, task)
    chat.open_target.side_effect = PageOperationError("搜索不到目标好友")

    assert await main_module.run(dry_run=True) == 1

    reloaded = History(settings.artifacts_dir / "history.json")
    assert reloaded.entry(success_key) == before
    assert len(reloaded.entries) == 1
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_unconfirmed_requires_explicit_mode_and_warns(monkeypatch, tmp_path, caplog) -> None:
    settings = _settings(tmp_path)
    task = _task()
    history, uncertain_key, _ = _prepare_history(settings, task)
    history.reserve(uncertain_key)
    history.mark_unconfirmed(uncertain_key)
    chat, send = _mock_runtime(monkeypatch, settings, task)

    assert await main_module.run(retry_mode="unconfirmed") == 0

    chat.open_target.assert_awaited_once_with("好友A", retries=1)
    assert send.await_count == 1
    assert "存在重复发送风险" in caplog.text


def test_retry_cli_modes_are_mutually_exclusive(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["run.py", "--retry-failed", "--retry-unconfirmed"])

    with pytest.raises(SystemExit):
        main_module._parse_cli_args()


def test_reset_account_state_cli_flag_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["run.py", "--reset-account-state"])

    args = main_module._parse_cli_args()

    assert args.reset_account_state is True
    assert args.dry_run is False
    assert args.retry_failed is False
    assert args.retry_unconfirmed is False


@pytest.mark.asyncio
async def test_uncertain_send_is_fail_closed_and_not_retryable_failed(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    _, uncertain_key, _ = _prepare_history(settings, task)
    _, send = _mock_runtime(monkeypatch, settings, task)
    send.side_effect = PageOperationError("文字发送状态未能确认，为避免重复不会自动重试")

    assert await main_module.run() == 1

    history = History(settings.artifacts_dir / "history.json")
    assert history.entry(uncertain_key)["status"] == "unconfirmed"
    assert uncertain_key not in history.retryable_failed_keys()


@pytest.mark.asyncio
async def test_explicit_send_rejection_is_failed_and_retryable(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    _, failed_key, _ = _prepare_history(settings, task)
    _, send = _mock_runtime(monkeypatch, settings, task)
    send.side_effect = PageOperationError("文字发送失败，页面提示可以重试")

    assert await main_module.run() == 1

    history = History(settings.artifacts_dir / "history.json")
    assert history.entry(failed_key)["status"] == "failed"
    assert history.entry(failed_key)["failure_category"] == "send_rejected"
    assert failed_key in history.retryable_failed_keys()
