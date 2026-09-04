import io
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import Target, TargetResult
from app.privacy import RedactingFormatter, build_target_aliases, redact_text, target_alias


def test_target_alias_zero_based() -> None:
    assert target_alias(0) == "好友01"
    assert target_alias(1) == "好友02"
    assert target_alias(9) == "好友10"


def test_build_target_aliases_preserves_order_and_does_not_mutate() -> None:
    targets = [Target(name="张三", messages=()), Target(name="李四", messages=())]

    aliases = build_target_aliases(targets)

    assert aliases == {"张三": "好友01", "李四": "好友02"}
    # 原始 target.name 不被修改
    assert targets[0].name == "张三"
    assert targets[1].name == "李四"


def test_build_target_aliases_covers_identity_candidates() -> None:
    targets = [
        Target(
            name="张三",
            remark_name="备注张三",
            nickname="昵称张三",
            unique_id="uid-1",
            short_id="short-1",
            sec_uid="sec-1",
            messages=(),
        ),
        Target(name="李四", remark_name="备注李四", nickname="昵称李四", messages=()),
    ]

    aliases = build_target_aliases(targets)

    assert aliases["张三"] == "好友01"
    assert aliases["备注张三"] == "好友01"
    assert aliases["昵称张三"] == "好友01"
    assert aliases["uid-1"] == "好友01"
    assert aliases["short-1"] == "好友01"
    assert aliases["sec-1"] == "好友01"
    assert aliases["备注李四"] == "好友02"
    assert aliases["昵称李四"] == "好友02"


def test_redact_text_handles_nested_names_longest_first() -> None:
    aliases = {"小明": "好友01", "小明同学": "好友02"}

    assert redact_text("搜索不到好友 小明同学", aliases) == "搜索不到好友 好友02"
    assert redact_text("搜索不到好友 小明", aliases) == "搜索不到好友 好友01"


def test_redact_text_replaces_locator_snippets() -> None:
    aliases = {"李四": "好友02"}

    assert redact_text('locator(... has_text="李四")', aliases) == 'locator(... has_text="好友02")'
    assert redact_text("PageOperationError: 搜索不到好友: 李四", aliases) == "PageOperationError: 搜索不到好友: 好友02"


def test_redacting_formatter_redacts_message_and_traceback() -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(
        RedactingFormatter("%(levelname)s %(message)s", aliases={"张三": "好友01"})
    )
    logger = logging.getLogger("privacy_test")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    try:
        raise RuntimeError("搜索不到好友: 张三")
    except RuntimeError:
        logger.exception("处理好友: 张三")

    output = handler.stream.getvalue()
    assert "好友01" in output
    assert "张三" not in output


def test_redacting_formatter_redacts_traceback_locator_text() -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(
        RedactingFormatter("%(levelname)s %(message)s", aliases={"李四": "好友02"})
    )
    logger = logging.getLogger("privacy_traceback_test")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    try:
        raise RuntimeError('locator(... has_text="李四")')
    except RuntimeError:
        logger.exception("失败")

    output = handler.stream.getvalue()
    assert 'has_text="好友02"' in output
    assert "李四" not in output


def test_result_json_redacts_target_and_error(tmp_path: Path) -> None:
    import app.main as main_module

    result = TargetResult(target="张三", status="failed", sent=0, error="搜索不到好友: 张三", target_alias="好友01")
    redacted = main_module._redacted_result(result, {"张三": "好友01"})

    assert redacted["target"] == "好友01"
    assert redacted["error"] == "搜索不到好友: 好友01"
    payload = json.dumps(redacted, ensure_ascii=False)
    assert "张三" not in payload


def test_write_results_json_is_redacted(tmp_path: Path) -> None:
    import app.main as main_module

    results = [
        TargetResult(target="张三", status="success", sent=1, target_alias="好友01"),
        TargetResult(target="李四", status="failed", sent=0, error="搜索不到好友: 李四", target_alias="好友02"),
    ]
    main_module._write_results(tmp_path, "daily-streak", False, results, {"张三": "好友01", "李四": "好友02"})

    content = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "好友01" in content
    assert "好友02" in content
    assert "张三" not in content
    assert "李四" not in content

    payload = json.loads(content)
    assert payload["schema_version"] == 1
    assert payload["run_id"]
    assert payload["started_at"]
    assert payload["finished_at"]
    assert payload["overall_status"] == "partial_success"
    assert payload["targets"] == payload["results"]
    assert list(tmp_path.glob("result.json.*.tmp")) == []


def test_result_json_redacts_all_identity_candidates(tmp_path: Path) -> None:
    import app.main as main_module

    targets = [
        Target(name="张三", remark_name="备注张三", nickname="昵称张三", messages=()),
        Target(name="李四", remark_name="备注李四", nickname="昵称李四", messages=()),
    ]
    aliases = build_target_aliases(targets)
    result = TargetResult(
        target="张三",
        status="failed",
        error="备注张三失败；页面还提到昵称李四",
        target_alias=aliases["张三"],
    )

    main_module._write_results(tmp_path, "task", False, [result], aliases)

    content = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "备注张三" not in content
    assert "昵称李四" not in content
    assert "好友01" in content
    assert "好友02" in content


def test_result_identity_is_stable_but_not_exposed(tmp_path: Path) -> None:
    import app.main as main_module

    result = TargetResult(target="张三", identity="sec_uid_secret", status="success", target_alias="好友01")
    main_module._write_results(tmp_path, "task", False, [result], {"张三": "好友01"})

    content = (tmp_path / "result.json").read_text(encoding="utf-8")
    payload = json.loads(content)
    assert "sec_uid_secret" not in content
    assert payload["targets"][0]["identity"].startswith("sha256:")


def test_screenshot_filename_uses_alias_not_real_name(tmp_path: Path) -> None:
    import asyncio

    import app.main as main_module

    page = MagicMock()
    page.screenshot = AsyncMock()
    path = asyncio.run(main_module._screenshot(page, tmp_path, "好友01"))

    assert path is not None
    assert "张三" not in path.name
    assert "好友01" in path.name


def test_redacting_formatter_handles_logging_args() -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(
        RedactingFormatter("%(levelname)s %(message)s", aliases={"张三": "好友01"})
    )
    logger = logging.getLogger("privacy_args_test")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    logger.info("处理好友: %s", "张三")

    output = handler.stream.getvalue()
    assert "处理好友: 好友01" in output
    assert "张三" not in output
