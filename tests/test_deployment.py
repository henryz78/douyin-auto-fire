import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.global.yml")
RUNTIME_ENVIRONMENT = (
    "DOUYIN_COOKIE",
    "DOUYIN_STORAGE_STATE",
    "DOUYIN_PROXY_SERVER",
    "DOUYIN_PROXY_USERNAME",
    "DOUYIN_PROXY_PASSWORD",
    "DINGTALK_WEBHOOK",
    "DINGTALK_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


@pytest.mark.parametrize("compose_name", COMPOSE_FILES)
def test_compose_exposes_supported_runtime_settings_and_storage_mount(compose_name: str) -> None:
    # Keep this a dependency-free contract test: the production image does not
    # need PyYAML just to validate that Compose passes settings through.
    compose = (ROOT / compose_name).read_text(encoding="utf-8")

    for key in RUNTIME_ENVIRONMENT:
        pattern = rf"^\s+{re.escape(key)}:\s+\$\{{{re.escape(key)}:-"
        assert re.search(pattern, compose, re.MULTILINE), f"{compose_name} 缺少 {key}"
    assert "- ./storage_state:/data/storage_state:ro" in compose
