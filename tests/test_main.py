from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browser import AuthenticationError
from app.errors import SafePreSendRetryError
from app.models import Message, Settings, Target, TaskConfig
import app.main as main_module


def _settings(tmp_path) -> Settings:
    return Settings(
        task_config_path=tmp_path / "config.json",
        storage_state=None,
        cookie="[]",
        headless=True,
        browser_path=None,
        artifacts_dir=tmp_path / "artifacts",
        trace=False,
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=token",
        dingtalk_secret="SEC-secret",
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


@pytest.mark.asyncio
async def test_authentication_failure_stops_remaining_targets_and_notifies(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    page = MagicMock()
    session = SimpleNamespace(page=page, context=MagicMock())

    @asynccontextmanager
    async def fake_open_douyin(_settings):
        yield session

    history = MagicMock()
    history.run_date.return_value = "2026-08-09"
    chat = MagicMock()
    chat.open_target = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(main_module, "load_settings", lambda _env=None: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _settings: task)
    monkeypatch.setattr(main_module, "History", MagicMock(return_value=history))
    monkeypatch.setattr(main_module, "open_douyin", fake_open_douyin)
    monkeypatch.setattr(main_module, "open_private_messages", AsyncMock())
    monkeypatch.setattr(main_module, "DouyinChat", MagicMock(return_value=chat))
    monkeypatch.setattr(main_module, "verify_login", AsyncMock(side_effect=AuthenticationError("登录失效")))
    monkeypatch.setattr(main_module, "_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(main_module, "_write_results", MagicMock())
    monkeypatch.setattr(main_module, "_notify_dingtalk", notify)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _path, _aliases=None: None)

    with pytest.raises(AuthenticationError, match="登录失效"):
        await main_module.run()

    # 新的实现使用 _open_target_with_retry() 包装器，内部调用时 retries=0
    chat.open_target.assert_awaited_once_with("好友A", retries=0)
    results = notify.await_args.args[3]
    assert [(result.target, result.status) for result in results] == [("好友A", "failed")]


@pytest.mark.asyncio
async def test_browser_start_failure_still_notifies(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)

    @asynccontextmanager
    async def broken_open_douyin(_settings):
        raise RuntimeError("浏览器启动失败")
        yield

    history = MagicMock()
    history.run_date.return_value = "2026-08-09"
    notify = AsyncMock()
    monkeypatch.setattr(main_module, "load_settings", lambda _env=None: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _settings: _task())
    monkeypatch.setattr(main_module, "History", MagicMock(return_value=history))
    monkeypatch.setattr(main_module, "open_douyin", broken_open_douyin)
    monkeypatch.setattr(main_module, "_write_results", MagicMock())
    monkeypatch.setattr(main_module, "_notify_dingtalk", notify)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _path, _aliases=None: None)

    with pytest.raises(SafePreSendRetryError, match="发送尚未开始"):
        await main_module.run()

    results = notify.await_args.args[3]
    assert [(result.target, result.status) for result in results] == [("运行检查", "failed")]


@pytest.mark.asyncio
async def test_target_open_retries_transient_error_with_safe_delays(monkeypatch) -> None:
    chat = SimpleNamespace(
        open_target=AsyncMock(side_effect=[RuntimeError("network timeout"), None])
    )
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    await main_module._open_target_with_retry(chat, "好友A", 3)

    assert chat.open_target.await_count == 2
    assert sleeps == [30.0]


@pytest.mark.asyncio
async def test_waits_between_consecutive_messages_for_same_friend(monkeypatch, tmp_path) -> None:
    settings = Settings(
        **{
            **_settings(tmp_path).__dict__,
            "storage_state_output": str(tmp_path / "storage-state.next.json"),
        }
    )
    messages = (Message(type="text", content="一"), Message(type="text", content="二"))
    task = TaskConfig(
        task_id="daily-streak",
        timezone="Asia/Shanghai",
        targets=(Target(name="好友A", messages=messages),),
        stickers={},
        interval_min=0.5,
        interval_max=0.5,
        continue_on_error=True,
        prevent_duplicates=False,
    )
    page = MagicMock()
    session = SimpleNamespace(page=page, context=MagicMock())

    @asynccontextmanager
    async def fake_open_douyin(_settings):
        yield session

    history = MagicMock()
    history.run_date.return_value = "2026-08-09"
    chat = MagicMock()
    chat.open_target = AsyncMock()
    send_message = AsyncMock()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main_module, "load_settings", lambda _env=None: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _settings: task)
    monkeypatch.setattr(main_module, "History", MagicMock(return_value=history))
    monkeypatch.setattr(main_module, "open_douyin", fake_open_douyin)
    monkeypatch.setattr(main_module, "open_private_messages", AsyncMock())
    monkeypatch.setattr(main_module, "DouyinChat", MagicMock(return_value=chat))
    monkeypatch.setattr(main_module, "verify_login", AsyncMock())
    monkeypatch.setattr(main_module, "send_message", send_message)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    monkeypatch.setattr(main_module, "_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(main_module, "_write_results", MagicMock())
    monkeypatch.setattr(main_module, "_notify_dingtalk", AsyncMock())
    monkeypatch.setattr(main_module, "_configure_logging", lambda _path, _aliases=None: None)
    save_state = AsyncMock()
    monkeypatch.setattr(main_module, "save_storage_state", save_state)

    assert await main_module.run() == 0
    assert send_message.await_count == 2
    assert sleeps == [0.5]
    save_state.assert_awaited_once_with(session, str(tmp_path / "storage-state.next.json"))


@pytest.mark.asyncio
async def test_telegram_notification_failure_does_not_fail_run(monkeypatch, tmp_path, caplog) -> None:
    settings = _settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "telegram_bot_token": "123:SECRET_TOKEN",
            "telegram_chat_id": "-100",
        }
    )
    monkeypatch.setattr(
        main_module,
        "send_telegram_notification",
        AsyncMock(side_effect=RuntimeError("https://api.telegram.org/bot123:SECRET_TOKEN/sendMessage")),
    )

    await main_module._notify_telegram(
        settings,
        "daily-streak",
        False,
        [main_module.TargetResult(target="好友A", status="success", sent=1)],
        [],
    )

    assert "Telegram 通知发送失败" in caplog.text
    assert "SECRET_TOKEN" not in caplog.text
