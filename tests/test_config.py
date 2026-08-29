import json
from pathlib import Path

import pytest

from app.config import ConfigError, load_proxy_settings, load_task
from app.models import Settings


def settings_for(path: Path) -> Settings:
    return Settings(
        task_config_path=path,
        storage_state='{"cookies": [], "origins": []}',
        cookie=None,
        headless=True,
        browser_path=None,
        artifacts_dir=path.parent / "artifacts",
        trace=True,
    )


def write_config(tmp_path: Path, payload: dict) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "tasks.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _clear_proxy_env(monkeypatch) -> None:
    for name in ("DOUYIN_PROXY_SERVER", "DOUYIN_PROXY_USERNAME", "DOUYIN_PROXY_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_proxy_is_disabled_when_server_is_missing(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)

    assert load_proxy_settings() is None


def test_loads_authenticated_http_proxy(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("DOUYIN_PROXY_SERVER", "http://proxy.example.com:3128")
    monkeypatch.setenv("DOUYIN_PROXY_USERNAME", "proxy-user")
    monkeypatch.setenv("DOUYIN_PROXY_PASSWORD", "proxy-password")

    proxy = load_proxy_settings()

    assert proxy is not None
    assert proxy.as_playwright() == {
        "server": "http://proxy.example.com:3128",
        "username": "proxy-user",
        "password": "proxy-password",
    }


def test_loads_unauthenticated_socks5_proxy(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("DOUYIN_PROXY_SERVER", "socks5://proxy.example.com:1080")

    proxy = load_proxy_settings()

    assert proxy is not None
    assert proxy.as_playwright() == {"server": "socks5://proxy.example.com:1080"}


def test_rejects_authenticated_socks5_proxy(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("DOUYIN_PROXY_SERVER", "socks5://proxy.example.com:1080")
    monkeypatch.setenv("DOUYIN_PROXY_USERNAME", "proxy-user")
    monkeypatch.setenv("DOUYIN_PROXY_PASSWORD", "proxy-password")

    with pytest.raises(ConfigError, match="不支持带用户名/密码认证的 SOCKS5"):
        load_proxy_settings()


def test_rejects_partial_proxy_credentials(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("DOUYIN_PROXY_SERVER", "http://proxy.example.com:3128")
    monkeypatch.setenv("DOUYIN_PROXY_USERNAME", "proxy-user")

    with pytest.raises(ConfigError, match="必须同时配置"):
        load_proxy_settings()


def test_rejects_proxy_credentials_without_server(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("DOUYIN_PROXY_USERNAME", "proxy-user")
    monkeypatch.setenv("DOUYIN_PROXY_PASSWORD", "proxy-password")

    with pytest.raises(ConfigError, match="必须同时配置 DOUYIN_PROXY_SERVER"):
        load_proxy_settings()


def test_rejects_credentials_embedded_in_proxy_url(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("DOUYIN_PROXY_SERVER", "http://user:password@proxy.example.com:3128")

    with pytest.raises(ConfigError, match="请分别使用"):
        load_proxy_settings()


def test_loads_multiple_targets_and_text(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "targets": [
                {"name": "好友A", "messages": [{"type": "text", "content": "你好"}]},
                {"name": "好友B", "messages": [{"type": "text", "content": "早上好"}]},
            ]
        },
    )

    task = load_task(settings_for(path))

    assert [target.name for target in task.targets] == ["好友A", "好友B"]
    assert task.targets[0].messages[0].content == "你好"


def test_rejects_empty_targets(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"targets": []})

    with pytest.raises(ConfigError, match="targets 必须是非空数组"):
        load_task(settings_for(path))


def test_rejects_missing_image(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"targets": [{"name": "好友A", "messages": [{"type": "image", "path": "data/missing.png"}]}]},
    )

    with pytest.raises(ConfigError, match="文件不存在"):
        load_task(settings_for(path))


def test_requires_sticker_mapping(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"targets": [{"name": "好友A", "messages": [{"type": "douyin_sticker", "sticker": "未知"}]}]},
    )

    with pytest.raises(ConfigError, match="原生表情未在"):
        load_task(settings_for(path))


def test_loads_sticker_mapping(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"targets": [{"name": "好友A", "messages": [{"type": "douyin_sticker", "sticker": "比心"}]}]},
    )
    (path.parent / "stickers.json").write_text(
        json.dumps({"比心": {"accessible_name": "比心", "fallback_index": 2}}, ensure_ascii=False),
        encoding="utf-8",
    )

    task = load_task(settings_for(path))

    assert task.stickers["比心"].fallback_index == 2


def test_loads_simple_config(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "friends": ["好友A", "好友B"],
            "messages": [{"type": "text", "value": "你好"}],
        },
    )

    task = load_task(settings_for(path))

    assert len(task.targets) == 2
    assert task.targets[1].messages[0].content == "你好"


def test_loads_target_open_retries_and_timeout(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "targets": [{"name": "好友A", "messages": [{"type": "text", "content": "你好"}]}],
            "target_open_retries": 3,
            "target_open_timeout_seconds": 20,
        },
    )

    task = load_task(settings_for(path))

    assert task.target_open_retries == 3
    assert task.target_open_timeout_seconds == 20


def test_defaults_target_open_retries_and_timeout(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"targets": [{"name": "好友A", "messages": [{"type": "text", "content": "你好"}]}]})

    task = load_task(settings_for(path))

    assert task.target_open_retries == 1
    assert task.target_open_timeout_seconds == 15.0


def test_rejects_negative_target_open_retries(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "targets": [{"name": "好友A", "messages": [{"type": "text", "content": "你好"}]}],
            "target_open_retries": -1,
        },
    )

    with pytest.raises(ConfigError, match="target_open_retries"):
        load_task(settings_for(path))


def test_rejects_non_positive_target_open_timeout(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "targets": [{"name": "好友A", "messages": [{"type": "text", "content": "你好"}]}],
            "target_open_timeout_seconds": 0,
        },
    )

    with pytest.raises(ConfigError, match="target_open_timeout_seconds"):
        load_task(settings_for(path))
