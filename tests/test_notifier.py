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
    TELEGRAM_MAX_MESSAGE_CHARS,
    _signed_webhook_url,
    build_dingtalk_markdown,
    build_telegram_message,
    _post_json,
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

    assert title == "抖音自动发送：部分成功"
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


def test_markdown_uses_public_alias_instead_of_real_name() -> None:
    results = [
        TargetResult(target="张三", status="success", sent=1, target_alias="好友01"),
        TargetResult(target="李四", status="failed", sent=0, error="搜索不到目标好友", target_alias="好友02"),
    ]

    _, markdown = build_dingtalk_markdown("daily-streak", False, results, [])

    assert "好友01" in markdown
    assert "好友02" in markdown
    assert "张三" not in markdown
    assert "李四" not in markdown


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

    assert "⚠️ 抖音任务部分成功" in message
    assert "任务：daily-streak" in message
    assert "账号：account1" in message
    assert "模式：正式运行" in message
    assert "成功好友（1）：" in message
    assert "- 好友A（已发送 2 条）" in message
    assert "失败目标（1）：" in message
    assert "- 好友B（已发送 1 条）：发送失败 请重试" in message


def test_telegram_uses_public_alias_and_redacts_target_from_error() -> None:
    message = build_telegram_message(
        "daily-streak",
        False,
        [
            TargetResult(
                target="张三",
                status="failed",
                error="张三发送失败",
                target_alias="好友01",
            )
        ],
    )

    assert "好友01" in message
    assert "张三" not in message


def test_notifications_distinguish_unconfirmed_account_failure_and_recovery() -> None:
    unconfirmed = build_telegram_message(
        "task",
        False,
        [TargetResult(target="好友A", status="unconfirmed", failure_category="send_unconfirmed")],
    )
    account_failure = build_telegram_message(
        "task",
        False,
        [TargetResult(target="账号检查", status="failed", failure_category="rate_limited")],
    )
    recovered = build_telegram_message(
        "task",
        False,
        [TargetResult(target="好友A", status="success", sent=1)],
        retry_mode="failed",
    )

    assert "未确认发送" in unconfirmed
    assert "不会自动重发" in unconfirmed
    assert "账号级故障" in account_failure
    assert "重试后恢复成功" in recovered


def test_notifications_distinguish_all_skipped_from_success() -> None:
    results = [
        TargetResult(target="好友A", status="duplicate"),
        TargetResult(target="好友B", status="skipped"),
    ]

    telegram = build_telegram_message("task", False, results)
    title, markdown = build_dingtalk_markdown("task", False, results, [])

    assert "无新发送（已跳过）" in telegram
    assert "跳过目标（2" in telegram
    assert title == "抖音自动发送：无新发送（已跳过）"
    assert "跳过目标（2" in markdown


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


def test_dingtalk_markdown_redacts_configured_secrets(monkeypatch) -> None:
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://example.test/robot?access_token=WEBHOOK_SECRET")
    monkeypatch.setenv("DINGTALK_SECRET", "SIGN_SECRET")
    _, markdown = build_dingtalk_markdown(
        "task",
        False,
        [TargetResult(target="好友A", status="failed", error="WEBHOOK_SECRET SIGN_SECRET")],
        [],
    )

    assert "WEBHOOK_SECRET" not in markdown
    assert "SIGN_SECRET" not in markdown


def test_dingtalk_http_failure_does_not_expose_url_or_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.notifier.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("https://example.test?access_token=WEBHOOK_SECRET")
        ),
    )

    with pytest.raises(RuntimeError, match="钉钉机器人请求失败") as exc_info:
        _post_json("https://example.test?access_token=WEBHOOK_SECRET", {})

    assert "WEBHOOK_SECRET" not in str(exc_info.value)


def test_split_telegram_message_preserves_content_and_limit() -> None:
    text = f"{'x' * 99}\n下一段\n" + "\n".join(f"好友{index}: 失败" for index in range(100))

    chunks = split_telegram_message(text, max_chars=100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_send_telegram_notification_sends_all_chunks(monkeypatch) -> None:
    results = [TargetResult(target=f"好友{index}", status="failed", error="发送失败") for index in range(500)]
    sent: list[tuple[str, str, str]] = []

    def fake_post(bot_token: str, chat_id: str, text: str) -> None:
        sent.append((bot_token, chat_id, text))

    monkeypatch.setattr("app.notifier._post_telegram_message", fake_post)

    message = build_telegram_message("daily-streak", False, results)
    assert len(message) > TELEGRAM_MAX_MESSAGE_CHARS

    await send_telegram_notification("123:BOT_TOKEN", "-100", "daily-streak", False, results)

    assert len(sent) > 1
    assert all(bot_token == "123:BOT_TOKEN" and chat_id == "-100" for bot_token, chat_id, _ in sent)
    assert all(len(text) <= TELEGRAM_MAX_MESSAGE_CHARS for _, _, text in sent)
    assert "好友499" in "".join(text for _, _, text in sent)


def test_telegram_bot_token_and_chat_id_must_be_configured_together(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(ConfigError, match="必须同时配置"):
        load_settings()
