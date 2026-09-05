import hashlib
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest
import app.actions_state as actions_state_module

from app.actions_state import (
    NoStateFilesError,
    StateArchiveError,
    extract_state_archive,
    main,
    prepare_archive,
    restore_archive,
    validate_state_layout,
)


def _history(*, legacy: bool = False) -> dict:
    if legacy:
        return {"task:date:friend:message": {"status": "unknown"}}
    return {"schema_version": 2, "entries": {}}


def _account_state(account_id: str = "account1") -> dict:
    return {
        "schema_version": 1,
        "account_id": account_id,
        "status": "ready",
        "failure_category": None,
        "last_failure_at": None,
        "cooldown_until": None,
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _make_state_tree(root: Path, *, legacy: bool = False) -> None:
    _write_json(root / "account1" / "history.json", _history(legacy=legacy))
    _write_json(root / "account1" / "account-state.json", _account_state())
    (root / "run.log").write_text("friend name and diagnostics", encoding="utf-8")
    _write_json(root / "result.json", {"status": "success"})
    (root / "screenshots").mkdir(parents=True)
    (root / "screenshots" / "friend.png").write_bytes(b"not a state file")


def test_prepare_allowlists_state_and_writes_atomic_archive(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    archive = tmp_path / "state.tar.gz"
    _make_state_tree(artifacts)

    included = prepare_archive(
        artifacts,
        archive,
        branch="main",
        run_id="123",
        run_attempt="1",
    )

    assert included == (
        PurePosixPath("account1/account-state.json"),
        PurePosixPath("account1/history.json"),
    )
    assert archive.is_file()
    assert not list(tmp_path.glob("state.tar.gz.*.tmp"))
    with tarfile.open(archive, "r:gz") as handle:
        names = {member.name for member in handle.getmembers()}
    assert names == {"manifest.json", "state/account1/history.json", "state/account1/account-state.json"}


def test_prepare_accepts_legacy_history_without_making_it_retryable(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _make_state_tree(artifacts, legacy=True)
    archive = tmp_path / "state.tar.gz"

    prepare_archive(artifacts, archive, branch="main", run_id="123", run_attempt="1")

    restored = tmp_path / "restored"
    restore_archive(archive, restored, branch="main")
    assert json.loads((restored / "account1" / "history.json").read_text(encoding="utf-8")) == _history(legacy=True)


def test_restore_round_trip_and_cleans_staging(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_state_tree(source)
    archive = tmp_path / "state.tar.gz"
    prepare_archive(source, archive, branch="main", run_id="123", run_attempt="1")

    destination = tmp_path / "artifacts"
    restored = restore_archive(archive, destination, branch="main")

    assert restored == (
        PurePosixPath("account1/account-state.json"),
        PurePosixPath("account1/history.json"),
    )
    assert (destination / "account1" / "history.json").read_bytes() == (
        source / "account1" / "history.json"
    ).read_bytes()
    assert (destination / "account1" / "account-state.json").read_bytes() == (
        source / "account1" / "account-state.json"
    ).read_bytes()
    assert not list(tmp_path.glob(".state-restore-*"))


def test_prepare_requires_state_and_cli_reports_distinct_exit_code(tmp_path: Path) -> None:
    with pytest.raises(NoStateFilesError):
        prepare_archive(
            tmp_path / "missing",
            tmp_path / "state.tar.gz",
            branch="main",
            run_id="123",
            run_attempt="1",
        )

    assert (
        main(
            [
                "prepare",
                "--artifacts-dir",
                str(tmp_path / "missing"),
                "--output",
                str(tmp_path / "state.tar.gz"),
                "--branch",
                "main",
                "--run-id",
                "123",
                "--run-attempt",
                "1",
            ]
        )
        == 3
    )


def test_restore_rejects_wrong_branch_without_writing_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_state_tree(source)
    archive = tmp_path / "state.tar.gz"
    prepare_archive(source, archive, branch="main", run_id="123", run_attempt="1")

    destination = tmp_path / "artifacts"
    with pytest.raises(StateArchiveError, match="来源不匹配"):
        restore_archive(archive, destination, branch="release")
    assert not destination.exists()


def test_restore_rejects_tampered_state_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_state_tree(source)
    archive = tmp_path / "state.tar.gz"
    prepare_archive(source, archive, branch="main", run_id="123", run_attempt="1")

    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as original, tarfile.open(tampered, "w:gz") as replacement:
        for member in original.getmembers():
            data = original.extractfile(member).read() if member.isfile() else None
            if member.name == "state/account1/history.json":
                data = b'{"schema_version":2,"entries":{"unexpected":{}}}'
            replacement.addfile(member, io.BytesIO(data) if data is not None else None)

    destination = tmp_path / "artifacts"
    with pytest.raises(StateArchiveError, match="校验失败"):
        restore_archive(tampered, destination, branch="main")
    assert not destination.exists()


def test_restore_rejects_non_regular_and_unallowlisted_members(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "workflow": "send.yml",
        "branch": "main",
        "run_id": "123",
        "run_attempt": "1",
        "created_at": "2026-09-03T00:00:00+00:00",
        "files": {"history.json": {"sha256": "0" * 64, "size": 0}},
    }
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        manifest_info.size = len(manifest_bytes)
        handle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        link = tarfile.TarInfo("state/history.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        handle.addfile(link)

    with pytest.raises(StateArchiveError, match="非普通文件"):
        restore_archive(archive, tmp_path / "artifacts", branch="main")


def test_restore_rejects_windows_drive_path_in_state_member(tmp_path: Path) -> None:
    data = json.dumps(_history()).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "workflow": "send.yml",
        "branch": "main",
        "run_id": "123",
        "run_attempt": "1",
        "created_at": "2026-09-03T00:00:00+00:00",
        "files": {"C:\\outside/history.json": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}},
    }
    archive = tmp_path / "windows-path.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        handle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        state_info = tarfile.TarInfo(r"state/C:\outside/history.json")
        state_info.size = len(data)
        handle.addfile(state_info, io.BytesIO(data))

    with pytest.raises(StateArchiveError, match="路径无效"):
        restore_archive(archive, tmp_path / "artifacts", branch="main")


def test_prepare_rejects_unsupported_nested_state_path(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write_json(artifacts / "account1" / "nested" / "history.json", _history())

    with pytest.raises(StateArchiveError, match="路径不受支持"):
        prepare_archive(artifacts, tmp_path / "state.tar.gz", branch="main", run_id="123", run_attempt="1")


def test_prepare_rejects_symlinked_artifacts_root(tmp_path: Path) -> None:
    real = tmp_path / "real-artifacts"
    real.mkdir()
    link = tmp_path / "artifacts"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前 Windows 环境不允许创建目录符号链接")

    with pytest.raises(StateArchiveError, match="符号链接"):
        prepare_archive(link, tmp_path / "state.tar.gz", branch="main", run_id="123", run_attempt="1")


@pytest.mark.parametrize(
    "missing",
    ["history.json", "account-state.json"],
)
def test_prepare_rejects_incomplete_state_pair(tmp_path: Path, missing: str) -> None:
    artifacts = tmp_path / "artifacts"
    _write_json(artifacts / "history.json", _history())
    _write_json(artifacts / "account-state.json", _account_state("default"))
    (artifacts / missing).unlink()

    with pytest.raises(StateArchiveError, match="必须同时包含"):
        prepare_archive(artifacts, tmp_path / "state.tar.gz", branch="main", run_id="123", run_attempt="1")


def test_prepare_supports_single_and_multi_account_state_pairs(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write_json(artifacts / "history.json", _history())
    _write_json(artifacts / "account-state.json", _account_state("default"))
    _write_json(artifacts / "account1" / "history.json", _history())
    _write_json(artifacts / "account1" / "account-state.json", _account_state())

    included = prepare_archive(artifacts, tmp_path / "state.tar.gz", branch="main", run_id="123", run_attempt="1")

    assert set(included) == {
        PurePosixPath("history.json"),
        PurePosixPath("account-state.json"),
        PurePosixPath("account1/history.json"),
        PurePosixPath("account1/account-state.json"),
    }


def test_validate_state_layout_accepts_matching_single_and_multi_account_modes(tmp_path: Path) -> None:
    single = tmp_path / "single"
    _write_json(single / "history.json", _history())
    _write_json(single / "account-state.json", _account_state("default"))
    assert validate_state_layout(single) == (
        PurePosixPath("account-state.json"),
        PurePosixPath("history.json"),
    )

    multi = tmp_path / "multi"
    for account_id in ("account1", "account2"):
        _write_json(multi / account_id / "history.json", _history())
        _write_json(multi / account_id / "account-state.json", _account_state(account_id))
    assert set(validate_state_layout(multi, account_ids=("account1", "account2"))) == {
        PurePosixPath("account1/account-state.json"),
        PurePosixPath("account1/history.json"),
        PurePosixPath("account2/account-state.json"),
        PurePosixPath("account2/history.json"),
    }


def test_validate_state_layout_rejects_single_multi_switch_and_invalid_account_ids(tmp_path: Path) -> None:
    single = tmp_path / "single"
    _write_json(single / "history.json", _history())
    _write_json(single / "account-state.json", _account_state("default"))
    with pytest.raises(StateArchiveError, match="布局与当前单/多账号配置不匹配"):
        validate_state_layout(single, account_ids=("account1",))

    multi = tmp_path / "multi"
    _write_json(multi / "account1" / "history.json", _history())
    _write_json(multi / "account1" / "account-state.json", _account_state("account1"))
    with pytest.raises(StateArchiveError, match="布局与当前单/多账号配置不匹配"):
        validate_state_layout(multi)
    with pytest.raises(StateArchiveError, match="路径分隔符"):
        validate_state_layout(single, account_ids=("account/1",))


def test_validate_state_layout_allows_explicit_empty_bootstrap(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    with pytest.raises(StateArchiveError, match="缺少 history.json"):
        validate_state_layout(artifacts)
    assert validate_state_layout(artifacts, allow_empty=True) == ()


def test_validate_state_layout_cli_uses_enabled_account_ids_and_rejects_bad_ids(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write_json(artifacts / "account1" / "history.json", _history())
    _write_json(artifacts / "account1" / "account-state.json", _account_state("account1"))
    accounts = tmp_path / "accounts.json"
    _write_json(
        accounts,
        {"accounts": [{"id": "account1", "enabled": True}, {"id": "account2", "enabled": False}]},
    )
    assert main(
        [
            "validate-layout",
            "--artifacts-dir",
            str(artifacts),
            "--accounts-file",
            str(accounts),
        ]
    ) == 0

    _write_json(accounts, {"accounts": [{"id": ["not-a-string"], "enabled": True}]})
    assert main(
        [
            "validate-layout",
            "--artifacts-dir",
            str(artifacts),
            "--accounts-file",
            str(accounts),
        ]
    ) == 2


def test_restore_rejects_oversized_state_member_before_reading_payload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(actions_state_module, "MAX_STATE_FILE_BYTES", 4)
    oversized = 5
    manifest = {
        "schema_version": 1,
        "workflow": "send.yml",
        "branch": "main",
        "run_id": "123",
        "run_attempt": "1",
        "created_at": "2026-09-03T00:00:00+00:00",
        "files": {"history.json": {"sha256": "0" * 64, "size": oversized}},
    }
    archive = tmp_path / "oversized.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        handle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        state_info = tarfile.TarInfo("state/history.json")
        state_info.size = oversized
        handle.addfile(state_info, io.BytesIO(b"xxxxx"))

    with pytest.raises(StateArchiveError, match="状态文件过大"):
        restore_archive(archive, tmp_path / "artifacts", branch="main")


def test_prepare_rejects_oversized_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(actions_state_module, "MAX_MANIFEST_BYTES", 1)
    source = tmp_path / "source"
    _make_state_tree(source)

    with pytest.raises(StateArchiveError, match="manifest 过大"):
        prepare_archive(source, tmp_path / "state.tar.gz", branch="main", run_id="123", run_attempt="1")


def test_restore_rejects_too_many_inner_tar_members(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    _make_state_tree(source)
    archive = tmp_path / "state.tar.gz"
    prepare_archive(source, archive, branch="main", run_id="123", run_attempt="1")
    monkeypatch.setattr(actions_state_module, "MAX_TAR_MEMBERS", 1)

    with pytest.raises(StateArchiveError, match="包含过多文件"):
        restore_archive(archive, tmp_path / "artifacts", branch="main")


def test_restore_rejects_archive_with_only_one_state_file(tmp_path: Path) -> None:
    data = json.dumps(_history()).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "workflow": "send.yml",
        "branch": "main",
        "run_id": "123",
        "run_attempt": "1",
        "created_at": "2026-09-03T00:00:00+00:00",
        "files": {"history.json": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}},
    }
    archive = tmp_path / "incomplete.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        handle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        state_info = tarfile.TarInfo("state/history.json")
        state_info.size = len(data)
        handle.addfile(state_info, io.BytesIO(data))

    with pytest.raises(StateArchiveError, match="必须同时包含"):
        restore_archive(archive, tmp_path / "artifacts", branch="main")


def test_restore_rejects_invalid_state_json(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "workflow": "send.yml",
        "branch": "main",
        "run_id": "123",
        "run_attempt": "1",
        "created_at": "2026-09-03T00:00:00+00:00",
        "files": {"history.json": {"sha256": "0" * 64, "size": 7}},
    }
    archive = tmp_path / "invalid-state.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        handle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        state_info = tarfile.TarInfo("state/history.json")
        state_info.size = 7
        handle.addfile(state_info, io.BytesIO(b"{bad!!!"))

    with pytest.raises(StateArchiveError, match="校验失败"):
        restore_archive(archive, tmp_path / "artifacts", branch="main")


def test_extract_state_archive_round_trip_is_allowlisted_and_atomic(tmp_path: Path) -> None:
    inner = tmp_path / "state.tar.gz"
    source = tmp_path / "source"
    _make_state_tree(source)
    prepare_archive(source, inner, branch="main", run_id="123", run_attempt="1")
    outer = tmp_path / "artifact.zip"
    with zipfile.ZipFile(outer, "w") as handle:
        handle.write(inner, "state.tar.gz")

    output = tmp_path / "restored-state.tar.gz"
    assert extract_state_archive(outer, output) == output
    assert output.read_bytes() == inner.read_bytes()
    assert not list(tmp_path.glob("restored-state.tar.gz.*.tmp"))


def test_extract_state_archive_rejects_path_traversal_and_extra_files(tmp_path: Path) -> None:
    outer = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(outer, "w") as handle:
        handle.writestr("../../state.tar.gz", b"not an archive")

    with pytest.raises(StateArchiveError, match="路径无效"):
        extract_state_archive(outer, tmp_path / "state.tar.gz")

    with zipfile.ZipFile(outer, "w") as handle:
        handle.writestr("state.tar.gz", b"not an archive")
        handle.writestr("unexpected.txt", b"should not be extracted")

    with pytest.raises(StateArchiveError, match="未允许的文件"):
        extract_state_archive(outer, tmp_path / "state.tar.gz")


def test_extract_state_archive_rejects_oversized_zip_and_excess_members(tmp_path: Path, monkeypatch) -> None:
    inner = tmp_path / "state.tar.gz"
    source = tmp_path / "source"
    _make_state_tree(source)
    prepare_archive(source, inner, branch="main", run_id="123", run_attempt="1")
    outer = tmp_path / "artifact.zip"
    with zipfile.ZipFile(outer, "w") as handle:
        handle.write(inner, "state.tar.gz")
        handle.writestr("metadata/", b"")

    monkeypatch.setattr(actions_state_module, "MAX_ZIP_MEMBERS", 1)
    with pytest.raises(StateArchiveError, match="包含过多文件"):
        extract_state_archive(outer, tmp_path / "restored.tar.gz")

    monkeypatch.setattr(actions_state_module, "MAX_ZIP_MEMBERS", 16)
    monkeypatch.setattr(actions_state_module, "MAX_OUTER_ARCHIVE_BYTES", 1)
    with pytest.raises(StateArchiveError, match="归档过大"):
        extract_state_archive(outer, tmp_path / "restored.tar.gz")


def test_extract_state_archive_rejects_duplicate_state_files(tmp_path: Path) -> None:
    outer = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(outer, "w") as handle:
        handle.writestr("state.tar.gz", b"one")
        duplicate = zipfile.ZipInfo("nested/state.tar.gz")
        handle.writestr(duplicate, b"two")

    with pytest.raises(StateArchiveError, match="唯一"):
        extract_state_archive(outer, tmp_path / "state.tar.gz")


def test_extract_state_archive_rejects_symlink_member(tmp_path: Path) -> None:
    outer = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("state.tar.gz")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(outer, "w") as handle:
        handle.writestr(info, "../../outside")

    with pytest.raises(StateArchiveError, match="符号链接"):
        extract_state_archive(outer, tmp_path / "state.tar.gz")


def test_extract_state_archive_cli_reports_corrupt_zip(tmp_path: Path) -> None:
    outer = tmp_path / "corrupt.zip"
    outer.write_bytes(b"not a zip")
    assert (
        main(
            [
                "extract-zip",
                "--zip",
                str(outer),
                "--output",
                str(tmp_path / "state.tar.gz"),
            ]
        )
        == 2
    )


def test_send_workflow_restores_and_uploads_only_non_dry_run_state() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "send.yml").read_text(encoding="utf-8")

    assert "actions: read" in workflow
    assert "bootstrap_state:" in workflow
    assert "reset_account_state:" in workflow
    assert "Compute state artifact key" in workflow
    assert "sha256" in workflow
    assert "page=${page}" in workflow
    assert "pending_prefix=\"douyin-state-pending-${STATE_BRANCH_KEY}-\"" in workflow
    assert "os.environ.get(f\"COOKIE_{i}\") or os.environ.get(f\"CONFIG_{i}\")" in workflow
    assert "Upload send attempt marker" in workflow
    assert 'expected="${prefix}${suffix}"' in workflow
    assert "sort_by(.created_at)" in workflow
    assert "page=$((page + 1))" in workflow
    assert "bootstrap_state 不能与 Dry Run 同时使用" in workflow
    assert "reset_account_state 不能与 Dry Run 同时使用" in workflow
    assert "python run.py --reset-account-state" in workflow
    assert "python -m app.actions_state extract-zip" in workflow
    assert "python -m app.actions_state restore" in workflow
    assert "python -m app.actions_state prepare" in workflow
    assert "name: Upload persistent send state" in workflow
    assert "retention-days: 7" in workflow
    assert 'if: ${{ always() && inputs.dry_run == false }}' in workflow
    assert "Dry Run：不恢复或写入跨运行状态" in workflow
    assert "Validate restored state layout" in workflow
    assert "validate-layout" in workflow
    assert "--output \"$RUNNER_TEMP/state.tar.gz\"" in workflow
    assert "--archive \"$RUNNER_TEMP/state.tar.gz\"" in workflow
    assert "path: ${{ runner.temp }}/state.tar.gz" in workflow
    assert "douyin-state.tar.gz" not in workflow
    # The daily schedule must remain disabled until state bootstrap and the
    # post-persistence review are complete.
    assert "# schedule:" in workflow
