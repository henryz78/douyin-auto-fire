import base64
import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import ConfigError, load_settings
from app.models import TargetResult
from app.notifier import (
    _signed_webhook_url,
    build_dingtalk_markdown,
    build_telegram_message,
    send_telegram_notification,
    split_telegram_message,
)


def test_signed_webhook_url_uses_dingtalk_hmac() -> None:
    timestamp = 1700000000123
    secret = "SEC-test-secret"
    url = _signed_webhook_url(
        "https://oapi.dingtalk.com/robot/send?access_token=token",
        secret,
        timestamp_ms=timestamp,
    )

    query = parse_qs(urlsplit(url).query)
    expected = base64.b64encode(
        hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()
    ).decode()
    assert query["access_token"] == ["token"]
    assert query["timestamp"] == [str(timestamp)]
    assert query["sign"] == [expected]


def test_markdown_lists_successes_failures_and_screenshots(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    results = [
        TargetResult(target="好友A", status="success", sent=2),
        TargetResult(target="好友B", status="failed", sent=1, error="发送失败\n请重试"),
    ]

    title, markdown = build_dingtalk_markdown(
        "daily-streak",
        False,
        results,
        [Path("artifacts/screenshots/friend-b.png")],
        finished_at=datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc),
    )

    assert title == "抖音自动发送：存在失败"
    assert "完成时间**：2026-08-09 16:00:00 +0800" in markdown
    assert "成功名单（1）" in markdown
    assert "**好友A** - 已发送 2 条" in markdown
    assert "失败名单（1）" in markdown
    assert "**好友B**，已发送 1 条" in markdown
    assert "发送失败 请重试" in markdown
    assert "`friend-b.png`" in markdown
    assert "https://github.com/owner/repo/actions/runs/123" in markdown


def test_dingtalk_webhook_and_secret_must_be_configured_together(monkeypatch) -> None:
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=token")
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)

    with pytest.raises(ConfigError, match="必须同时配置"):
        load_settings()


def test_markdown_shows_real_name_even_with_alias() -> None:
    results = [
        TargetResult(target="张三", status="success", sent=1, target_alias="好友01"),
        TargetResult(target="李四", status="failed", sent=0, error="搜索不到目标好友", target_alias="好友02"),
    ]

    _, markdown = build_dingtalk_markdown("daily-streak", False, results, [])

    assert "张三" in markdown
    assert "李四" in markdown
    assert "好友01" not in markdown
    assert "好友02" not in markdown


def test_markdown_escapes_dynamic_text_and_limits_large_lists() -> None:
    results = [
        TargetResult(target=f"好友*[{index}]", status="failed", error="`失败`" * 200)
        for index in range(100)
    ]

    _, markdown = build_dingtalk_markdown("task_*", False, results, [])

    assert r"task\_\*" in markdown
    assert r"好友\*\[0\]" in markdown
    assert "其余 85 人已省略" in markdown
    assert len(markdown.encode("utf-8")) <= 18_000


def test_telegram_message_lists_successes_failures_and_account() -> None:
    results = [
        TargetResult(target="好友A", status="success", sent=2),
        TargetResult(target="好友B", status="failed", sent=1, error="发送失败\n请重试"),
    ]

    message = build_telegram_message(
        "daily-streak",
        False,
        results,
        account_id="account1",
        finished_at=datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc),
    )

    assert "❌ 抖音任务存在失败" in message
    assert "任务：daily-streak" in message
    assert "账号：account1" in message
    assert "模式：正式运行" in message
    assert "成功好友（1）：" in message
    assert "- 好友A（已发送 2 条）" in message
    assert "失败好友（1）：" in message
    assert "- 好友B（已发送 1 条）：发送失败 请重试" in message


def test_telegram_dry_run_message_uses_verification_detail() -> None:
    message = build_telegram_message(
        "daily-streak",
        True,
        [TargetResult(target="好友A", status="success")],
    )

    assert "✅ 抖音任务成功" in message
    assert "模式：Dry Run（未发送消息）" in message
    assert "好友A（验证通过）" in message


def test_telegram_message_redacts_configured_secrets(monkeypatch) -> None:
    monkeypatch.setenv("DOUYIN_COOKIE", "COOKIE_SECRET")
    monkeypatch.setenv("DOUYIN_PROXY_PASSWORD", "PROXY_SECRET")
    message = build_telegram_message(
        "daily-streak",
        False,
        [TargetResult(target="好友A", status="failed", error="COOKIE_SECRET PROXY_SECRET")],
    )

    assert "COOKIE_SECRET" not in message
    assert "PROXY_SECRET" not in message
    assert message.count("[已隐藏]") == 2


def test_split_telegram_message_preserves_content_and_limit() -> None:
    text = f"{'x' * 99}\n下一段\n" + "\n".join(f"好友{index}: 失败" for index in range(100))

    chunks = split_telegram_message(text, max_chars=100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_send_telegram_notification_sends_all_chunks(monkeypatch) -> None:
    results = [TargetResult(target=f"好友{index}", status="failed", error="发送失败") for index in range(100)]
    sent: list[tuple[str, str, str]] = []

    def fake_post(bot_token: str, chat_id: str, text: str) -> None:
        sent.append((bot_token, chat_id, text))

    monkeypatch.setattr("app.notifier._post_telegram_message", fake_post)

    await send_telegram_notification("123:BOT_TOKEN", "-100", "daily-streak", False, results)

    assert len(sent) > 1
    assert all(bot_token == "123:BOT_TOKEN" and chat_id == "-100" for bot_token, chat_id, _ in sent)
    assert all(len(text) <= 3_900 for _, _, text in sent)
    assert "好友99" in "\n".join(text for _, _, text in sent)


def test_telegram_bot_token_and_chat_id_must_be_configured_together(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(ConfigError, match="必须同时配置"):
        load_settings()
