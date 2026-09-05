import os
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.account_runner as runner_module
from app.accounts import Account
from app.config import ConfigError


def _env_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _account(account_id: str, env_file: Path) -> Account:
    return Account(id=account_id, enabled=True, env_file=env_file)


# ---------- account_env ----------


def test_account_env_applies_and_restores(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DOUYIN_COOKIE", raising=False)
    monkeypatch.delenv("ARTIFACTS_DIR", raising=False)
    env_file = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\nARTIFACTS_DIR=artifacts/a\n")

    with runner_module.account_env(env_file):
        assert os.environ["DOUYIN_COOKIE"] == "cookie-a"
        assert os.environ["ARTIFACTS_DIR"] == "artifacts/a"

    assert "DOUYIN_COOKIE" not in os.environ
    assert "ARTIFACTS_DIR" not in os.environ


def test_account_env_restores_overwritten_process_values(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLESS", "true")
    env_file = _env_file(tmp_path, ".env.a", "HEADLESS=false\nDOUYIN_COOKIE=cookie-a\n")

    with runner_module.account_env(env_file):
        assert os.environ["HEADLESS"] == "false"

    assert os.environ["HEADLESS"] == "true"
    assert "DOUYIN_COOKIE" not in os.environ


def test_account_env_inherits_unset_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLESS", "true")
    env_file = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")

    with runner_module.account_env(env_file):
        assert os.environ["HEADLESS"] == "true"


def test_account_env_applies_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ARTIFACTS_DIR", raising=False)
    monkeypatch.delenv("DOUYIN_ACCOUNT_ID", raising=False)
    env_file = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")

    with runner_module.account_env(
        env_file,
        defaults={"ARTIFACTS_DIR": "artifacts/account1", "DOUYIN_ACCOUNT_ID": "account1"},
    ):
        assert os.environ["ARTIFACTS_DIR"] == "artifacts/account1"
        assert os.environ["DOUYIN_ACCOUNT_ID"] == "account1"

    assert "DOUYIN_ACCOUNT_ID" not in os.environ


def test_account_env_explicit_value_beats_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ARTIFACTS_DIR", raising=False)
    env_file = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\nARTIFACTS_DIR=my/own/dir\n")

    with runner_module.account_env(env_file, defaults={"ARTIFACTS_DIR": "artifacts/account1"}):
        assert os.environ["ARTIFACTS_DIR"] == "my/own/dir"


def test_account_env_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="环境文件不存在"):
        with runner_module.account_env(tmp_path / ".env.missing"):
            pass


# ---------- run_all_accounts ----------


def test_run_all_accounts_account_failure_does_not_block_others(monkeypatch, tmp_path: Path) -> None:
    env_a = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")
    env_b = _env_file(tmp_path, ".env.b", "DOUYIN_COOKIE=cookie-b\n")
    seen: list[str] = []

    async def fake_run(dry_run: bool = False, env_file: str | None = None, run_id: str | None = None) -> int:
        cookie = os.environ["DOUYIN_COOKIE"]
        seen.append(cookie)
        if cookie == "cookie-a":
            raise RuntimeError("账号A的Cookie失效")
        return 0

    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a), _account("b", env_b)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", lambda _env=None: SimpleNamespace(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    result = runner_module.run_all_accounts()

    assert result == 1
    assert seen == ["cookie-a", "cookie-b"]


def test_run_all_accounts_legacy_env_cleared_for_each_account(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOUYIN_COOKIE", "legacy-cookie")
    env_a = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")
    seen: list[str] = []

    async def fake_run(dry_run: bool = False, env_file: str | None = None, run_id: str | None = None) -> int:
        seen.append(os.environ["DOUYIN_COOKIE"])
        return 0

    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", lambda _env=None: SimpleNamespace(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    assert runner_module.run_all_accounts() == 0
    assert seen == ["cookie-a"]


def test_run_all_accounts_all_success_returns_zero(monkeypatch, tmp_path: Path) -> None:
    env_a = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")
    env_b = _env_file(tmp_path, ".env.b", "DOUYIN_COOKIE=cookie-b\n")
    calls = []

    async def fake_run(dry_run: bool = False, env_file: str | None = None, run_id: str | None = None) -> int:
        calls.append(os.environ["DOUYIN_COOKIE"])
        return 0

    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a), _account("b", env_b)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", lambda _env=None: SimpleNamespace(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    assert runner_module.run_all_accounts() == 0
    assert calls == ["cookie-a", "cookie-b"]


@pytest.mark.parametrize(
    "flag,expected_mode",
    [("retry_failed", "failed"), ("retry_unconfirmed", "unconfirmed")],
)
def test_run_all_accounts_forwards_explicit_retry_mode_per_account(
    monkeypatch, tmp_path: Path, flag: str, expected_mode: str
) -> None:
    env_a = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")
    env_b = _env_file(tmp_path, ".env.b", "DOUYIN_COOKIE=cookie-b\n")
    calls: list[tuple[str, dict]] = []

    async def fake_run(**kwargs) -> int:
        calls.append((os.environ["DOUYIN_COOKIE"], kwargs))
        return 0

    args = SimpleNamespace(dry_run=False, env_file=None, retry_failed=False, retry_unconfirmed=False)
    setattr(args, flag, True)
    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a), _account("b", env_b)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: args)
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", lambda _env=None: SimpleNamespace(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    assert runner_module.run_all_accounts() == 0
    assert [cookie for cookie, _kwargs in calls] == ["cookie-a", "cookie-b"]
    assert [kwargs["retry_mode"] for _cookie, kwargs in calls] == [expected_mode, expected_mode]


def test_run_all_accounts_forwards_manual_account_state_reset(monkeypatch, tmp_path: Path) -> None:
    env_a = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")
    env_b = _env_file(tmp_path, ".env.b", "DOUYIN_COOKIE=cookie-b\n")
    calls: list[tuple[str, dict]] = []

    async def fake_run(**kwargs) -> int:
        calls.append((os.environ["DOUYIN_COOKIE"], kwargs))
        return 0

    args = SimpleNamespace(dry_run=False, env_file=None, reset_account_state=True)
    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a), _account("b", env_b)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: args)
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", lambda _env=None: SimpleNamespace(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    assert runner_module.run_all_accounts() == 0
    assert [kwargs["reset_account_state"] for _cookie, kwargs in calls] == [True, True]


def test_run_all_accounts_all_failed_returns_one(monkeypatch, tmp_path: Path) -> None:
    env_a = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")

    async def fake_run(dry_run: bool = False, env_file: str | None = None, run_id: str | None = None) -> int:
        raise RuntimeError("全部失败")

    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", lambda _env=None: SimpleNamespace(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    assert runner_module.run_all_accounts() == 1


def test_run_all_accounts_no_enabled_accounts_skips(monkeypatch) -> None:
    calls = []

    async def fake_run(dry_run: bool = False, env_file: str | None = None, run_id: str | None = None) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(runner_module, "load_accounts", lambda: [])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(runner_module, "run", fake_run)

    assert runner_module.run_all_accounts() == 0
    assert calls == []


def test_run_all_accounts_missing_env_file_fails_only_that_account(monkeypatch, tmp_path: Path) -> None:
    env_a = tmp_path / ".env.missing"
    env_b = _env_file(tmp_path, ".env.b", "DOUYIN_COOKIE=cookie-b\n")
    seen: list[str] = []

    async def fake_run(dry_run: bool = False, env_file: str | None = None, run_id: str | None = None) -> int:
        seen.append(os.environ["DOUYIN_COOKIE"])
        return 0

    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a), _account("b", env_b)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", lambda _env=None: SimpleNamespace(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    assert runner_module.run_all_accounts() == 1
    assert seen == ["cookie-b"]


def test_run_all_accounts_uses_per_account_artifacts_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    env_a = _env_file(tmp_path, ".env.a", "DOUYIN_COOKIE=cookie-a\n")
    lock_dirs: list[Path] = []
    run_ids: list[str] = []

    async def fake_run(dry_run: bool = False, env_file: str | None = None, run_id: str | None = None) -> int:
        payload = json.loads((Path(os.environ["ARTIFACTS_DIR"]) / "run.lock").read_text(encoding="utf-8"))
        assert payload["run_id"] == run_id
        assert payload["account_id"] == "a"
        run_ids.append(run_id or "")
        return 0

    def fake_load_settings(env_file=None):
        settings = SimpleNamespace(artifacts_dir=Path(os.environ["ARTIFACTS_DIR"]))
        lock_dirs.append(settings.artifacts_dir)
        return settings

    monkeypatch.setattr(runner_module, "load_accounts", lambda: [_account("a", env_a)])
    monkeypatch.setattr(runner_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "load_settings", fake_load_settings)
    monkeypatch.setattr(runner_module, "_configure_logging", lambda *args, **kwargs: None)

    assert runner_module.run_all_accounts() == 0
    assert lock_dirs == [Path("artifacts") / "a"]
    assert len(run_ids) == 1 and run_ids[0]
    assert (tmp_path / "artifacts" / "a").is_dir()
    assert not (tmp_path / "artifacts" / "a" / "run.lock").exists()
