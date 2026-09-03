from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


STATE_ARCHIVE_SCHEMA_VERSION = 1
STATE_WORKFLOW = "send.yml"
STATE_FILENAMES = frozenset({"history.json", "account-state.json"})
MAX_OUTER_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_STATE_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_STATE_FILE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 1 * 1024 * 1024


class StateArchiveError(ValueError):
    """Raised when a persistent state archive is missing or unsafe."""


class NoStateFilesError(StateArchiveError):
    """Raised when there is no state to persist after a failed run."""


def prepare_archive(
    artifacts_dir: Path,
    output: Path,
    *,
    branch: str,
    run_id: str,
    run_attempt: str,
) -> tuple[PurePosixPath, ...]:
    """Create an atomic, allow-listed state archive.

    Only the two durable state filenames are copied.  Logs, screenshots,
    traces, result.json, credentials, and arbitrary files under ``artifacts``
    are deliberately excluded.
    """

    files = tuple(_state_files(artifacts_dir))
    if not files:
        raise NoStateFilesError(f"未找到可持久化的状态文件: {artifacts_dir}")

    records: dict[str, dict[str, int | str]] = {}
    contents: list[tuple[PurePosixPath, bytes]] = []
    total_size = 0
    for relative in files:
        path = artifacts_dir / Path(*relative.parts)
        data = _read_and_validate_state(path, relative)
        total_size += len(data)
        if total_size > MAX_STATE_ARCHIVE_BYTES:
            raise StateArchiveError("持久化状态归档过大")
        key = relative.as_posix()
        records[key] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        contents.append((relative, data))

    manifest = {
        "schema_version": STATE_ARCHIVE_SCHEMA_VERSION,
        "workflow": STATE_WORKFLOW,
        "branch": _required_text(branch, "branch"),
        "run_id": _required_text(run_id, "run_id"),
        "run_attempt": _required_text(run_attempt, "run_attempt"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": records,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tarfile.open(temporary, mode="w:gz") as archive:
            _add_bytes(archive, PurePosixPath("manifest.json"), manifest_bytes)
            for relative, data in contents:
                _add_bytes(archive, PurePosixPath("state") / relative, data)
        # Windows rejects fsync on a read-only descriptor; reopening the
        # completed archive read/write still avoids changing its contents and
        # lets the durability barrier work on every supported platform.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return files


def extract_state_archive(zip_path: Path, output: Path) -> Path:
    """Extract exactly one state tarball from an Actions artifact ZIP.

    ``actions/upload-artifact`` wraps the uploaded file in a ZIP.  Do not use
    a general-purpose extractor here: the ZIP is downloaded from the API and
    must be treated as untrusted input before the inner tar manifest is read.
    """

    zip_path = Path(zip_path)
    try:
        with zipfile.ZipFile(zip_path, mode="r") as archive:
            candidates: list[zipfile.ZipInfo] = []
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative = _zip_relative(member.filename)
                if relative.name != "state.tar.gz":
                    raise StateArchiveError(f"Artifact ZIP 包含未允许的文件: {member.filename}")
                if _zip_is_symlink(member):
                    raise StateArchiveError(f"Artifact ZIP 包含符号链接: {member.filename}")
                candidates.append(member)
            if len(candidates) != 1:
                raise StateArchiveError("Artifact ZIP 必须包含唯一的 state.tar.gz")
            member = candidates[0]
            if member.file_size < 0 or member.file_size > MAX_OUTER_ARCHIVE_BYTES:
                raise StateArchiveError("Artifact ZIP 中的状态归档过大")
            data = archive.read(member)
    except StateArchiveError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise StateArchiveError(f"Artifact ZIP 无法安全读取: {zip_path}") from exc

    _atomic_write_bytes(Path(output), data)
    return Path(output)


def restore_archive(
    archive_path: Path,
    artifacts_dir: Path,
    *,
    branch: str,
    workflow: str = STATE_WORKFLOW,
) -> tuple[PurePosixPath, ...]:
    """Validate and restore a state archive without trusting its contents."""

    archive_path = Path(archive_path)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            manifest_member = _find_unique_member(members, "manifest.json")
            if manifest_member.size < 0 or manifest_member.size > MAX_MANIFEST_BYTES:
                raise StateArchiveError("持久化状态 manifest 过大")
            manifest_raw = archive.extractfile(manifest_member)
            if manifest_raw is None:
                raise StateArchiveError("持久化状态归档缺少 manifest.json")
            manifest = _load_manifest(manifest_raw.read(MAX_MANIFEST_BYTES + 1), branch=branch, workflow=workflow)

            state_members: dict[PurePosixPath, tarfile.TarInfo] = {}
            for member in members:
                if member.name in {"manifest.json", "state", "state/"}:
                    continue
                relative = _archive_relative(member.name)
                if relative in state_members:
                    raise StateArchiveError(f"持久化状态归档包含重复文件: {relative}")
                if not member.isfile():
                    raise StateArchiveError(f"持久化状态归档包含非普通文件: {member.name}")
                state_members[relative] = member

            records = manifest["files"]
            if set(records) != {relative.as_posix() for relative in state_members}:
                raise StateArchiveError("持久化状态归档文件清单与内容不一致")

            contents: dict[PurePosixPath, bytes] = {}
            total_size = 0
            for relative, member in state_members.items():
                if member.size < 0 or member.size > MAX_STATE_FILE_BYTES:
                    raise StateArchiveError(f"持久化状态文件过大: {relative}")
                total_size += member.size
                if total_size > MAX_STATE_ARCHIVE_BYTES:
                    raise StateArchiveError("持久化状态归档解压后过大")
                stream = archive.extractfile(member)
                if stream is None:
                    raise StateArchiveError(f"无法读取持久化状态文件: {relative}")
                data = stream.read(MAX_STATE_FILE_BYTES + 1)
                if len(data) > MAX_STATE_FILE_BYTES:
                    raise StateArchiveError(f"持久化状态文件过大: {relative}")
                record = records[relative.as_posix()]
                if record["size"] != len(data) or record["sha256"] != hashlib.sha256(data).hexdigest():
                    raise StateArchiveError(f"持久化状态文件校验失败: {relative}")
                _validate_state_bytes(data, relative)
                contents[relative] = data
    except StateArchiveError:
        raise
    except (tarfile.TarError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateArchiveError(f"持久化状态归档无法读取: {archive_path}") from exc

    if not contents:
        raise StateArchiveError("持久化状态归档不包含 history.json 或 account-state.json")
    _validate_complete_state_set(contents)

    artifacts_dir = Path(artifacts_dir)
    if artifacts_dir.exists() and artifacts_dir.is_symlink():
        raise StateArchiveError(f"状态目录不能是符号链接: {artifacts_dir}")
    # Keep the quarantine path short: GitHub runners are Linux, but this
    # helper is also exercised on Windows where a long repository checkout
    # plus a temporary path can otherwise hit MAX_PATH.
    staging = artifacts_dir.parent / f".state-restore-{uuid.uuid4().hex[:12]}"
    try:
        for relative, data in contents.items():
            target = staging / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)

        for relative in sorted(contents, key=lambda value: len(value.parts)):
            source = staging / Path(*relative.parts)
            destination = artifacts_dir / Path(*relative.parts)
            _assert_safe_destination(artifacts_dir, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
    except (OSError, ValueError) as exc:
        raise StateArchiveError(f"持久化状态恢复失败: {artifacts_dir}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return tuple(sorted(contents))


def _assert_safe_destination(root: Path, relative: PurePosixPath) -> None:
    """Reject pre-existing symlinked directories before restoring state."""

    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise StateArchiveError(f"状态目录不能包含符号链接: {current}")


def _state_files(artifacts_dir: Path) -> Iterable[PurePosixPath]:
    artifacts_dir = Path(artifacts_dir)
    if artifacts_dir.is_symlink():
        raise StateArchiveError(f"状态目录不能是符号链接: {artifacts_dir}")
    if not artifacts_dir.is_dir():
        return ()
    result: list[PurePosixPath] = []
    for path in artifacts_dir.rglob("*"):
        if path.is_symlink():
            if path.name in STATE_FILENAMES:
                raise StateArchiveError(f"状态文件不能是符号链接: {path}")
            continue
        if not path.is_file() or path.name not in STATE_FILENAMES:
            continue
        relative = PurePosixPath(path.relative_to(artifacts_dir).as_posix())
        if (
            len(relative.parts) > 2
            or ".." in relative.parts
            or any("\\" in part or ":" in part for part in relative.parts)
        ):
            raise StateArchiveError(f"状态文件路径不受支持: {relative}")
        result.append(relative)
    _validate_complete_state_set(result)
    return tuple(sorted(result))


def _validate_complete_state_set(paths: Iterable[PurePosixPath]) -> None:
    """Require history and account state to travel as an inseparable pair."""

    by_directory: dict[str, set[str]] = {}
    for relative in paths:
        directory = relative.parent.as_posix()
        by_directory.setdefault(directory, set()).add(relative.name)
    for directory, names in by_directory.items():
        if names != STATE_FILENAMES:
            label = "artifacts" if directory == "." else f"artifacts/{directory}"
            raise StateArchiveError(f"状态文件必须同时包含 history.json 和 account-state.json: {label}")


def validate_state_layout(
    artifacts_dir: Path,
    *,
    account_ids: Iterable[str] | None = None,
    allow_empty: bool = False,
) -> tuple[PurePosixPath, ...]:
    """Ensure restored state matches the current single/multi-account mode."""

    files = tuple(_state_files(artifacts_dir))
    if not files:
        if allow_empty:
            return ()
        raise StateArchiveError(f"状态布局缺少 history.json 和 account-state.json: {artifacts_dir}")

    if account_ids is None:
        expected = {PurePosixPath("history.json"), PurePosixPath("account-state.json")}
    else:
        identifiers = tuple(account_ids)
        expected = set()
        seen: set[str] = set()
        for account_id in identifiers:
            relative = _account_relative_path(account_id)
            normalized = relative.as_posix()
            if normalized in seen:
                raise StateArchiveError("当前账号配置包含重复 id")
            seen.add(normalized)
            expected.update({relative / "history.json", relative / "account-state.json"})
    actual = set(files)
    if actual != expected:
        raise StateArchiveError("持久化状态布局与当前单/多账号配置不匹配")
    return files


def _account_relative_path(account_id: str) -> PurePosixPath:
    if not isinstance(account_id, str) or not account_id.strip():
        raise StateArchiveError("账号 id 无效，无法校验状态布局")
    normalized = account_id.strip()
    relative = PurePosixPath(normalized)
    if (
        relative.is_absolute()
        or relative.parts != (normalized,)
        or "/" in normalized
        or "\\" in normalized
        or ":" in normalized
        or normalized in {".", ".."}
    ):
        raise StateArchiveError(f"账号 id 不能包含路径分隔符: {account_id}")
    return relative


def _load_account_ids(path: Path) -> tuple[str, ...]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateArchiveError(f"多账号配置无法读取，无法校验状态布局: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("accounts"), list):
        raise StateArchiveError(f"多账号配置缺少 accounts 数组: {path}")
    result: list[str] = []
    for item in value["accounts"]:
        if not isinstance(item, dict) or item.get("enabled", True) is not True:
            continue
        result.append(item.get("id"))
    return tuple(result)


def _read_and_validate_state(path: Path, relative: PurePosixPath) -> bytes:
    try:
        if path.stat().st_size > MAX_STATE_FILE_BYTES:
            raise StateArchiveError(f"持久化状态文件过大: {relative}")
        with path.open("rb") as handle:
            data = handle.read(MAX_STATE_FILE_BYTES + 1)
    except OSError as exc:
        raise StateArchiveError(f"无法读取状态文件: {path}") from exc
    if len(data) > MAX_STATE_FILE_BYTES:
        raise StateArchiveError(f"持久化状态文件过大: {relative}")
    _validate_state_bytes(data, relative)
    return data


def _validate_state_bytes(data: bytes, relative: PurePosixPath) -> None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateArchiveError(f"状态文件不是有效 JSON: {relative}") from exc
    if not isinstance(value, dict):
        raise StateArchiveError(f"状态文件必须是 JSON 对象: {relative}")
    if relative.name == "history.json":
        if "schema_version" in value:
            if value.get("schema_version") != 2 or not isinstance(value.get("entries"), dict):
                raise StateArchiveError(f"history.json schema_version 无效: {relative}")
        return
    if value.get("schema_version") != 1 or not isinstance(value.get("account_id"), str):
        raise StateArchiveError(f"account-state.json schema_version 无效: {relative}")


def _add_bytes(archive: tarfile.TarFile, name: PurePosixPath, data: bytes) -> None:
    info = tarfile.TarInfo(name.as_posix())
    info.size = len(data)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(data))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _find_unique_member(members: list[tarfile.TarInfo], name: str) -> tarfile.TarInfo:
    matches = [member for member in members if member.name == name]
    if len(matches) != 1 or not matches[0].isfile():
        raise StateArchiveError(f"持久化状态归档缺少唯一的 {name}")
    return matches[0]


def _archive_relative(name: str) -> PurePosixPath:
    if not name.startswith("state/"):
        raise StateArchiveError(f"持久化状态归档包含未允许的文件: {name}")
    relative = PurePosixPath(name.removeprefix("state/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or len(relative.parts) > 2
        or relative.name not in STATE_FILENAMES
        or any("\\" in part or ":" in part for part in relative.parts)
    ):
        raise StateArchiveError(f"持久化状态归档路径无效: {name}")
    return relative


def _zip_relative(name: str) -> PurePosixPath:
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
    ):
        raise StateArchiveError(f"Artifact ZIP 路径无效: {name}")
    if relative.name != "state.tar.gz":
        raise StateArchiveError(f"Artifact ZIP 包含未允许的文件: {name}")
    return relative


def _zip_is_symlink(member: zipfile.ZipInfo) -> bool:
    mode = (member.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _load_manifest(data: bytes, *, branch: str, workflow: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateArchiveError("持久化状态 manifest 无效") from exc
    if not isinstance(value, dict):
        raise StateArchiveError("持久化状态 manifest 必须是 JSON 对象")
    if value.get("schema_version") != STATE_ARCHIVE_SCHEMA_VERSION:
        raise StateArchiveError("不支持的持久化状态 archive schema_version")
    if value.get("workflow") != workflow or value.get("branch") != branch:
        raise StateArchiveError("持久化状态归档来源不匹配")
    for field in ("run_id", "run_attempt", "created_at"):
        _required_text(value.get(field), field)
    records = value.get("files")
    if not isinstance(records, dict) or not records:
        raise StateArchiveError("持久化状态 manifest 缺少文件清单")
    normalized: dict[str, dict[str, int | str]] = {}
    for raw_path, record in records.items():
        relative = _archive_relative(f"state/{raw_path}")
        if relative.as_posix() != raw_path:
            raise StateArchiveError("持久化状态 manifest 路径无效")
        if not isinstance(record, dict):
            raise StateArchiveError("持久化状态 manifest 文件记录无效")
        digest = record.get("sha256")
        size = record.get("size")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise StateArchiveError("持久化状态 manifest 校验值无效")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise StateArchiveError("持久化状态 manifest 文件大小无效")
        normalized[raw_path] = {"sha256": digest, "size": size}
    value["files"] = normalized
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateArchiveError(f"持久化状态 {field} 无效")
    return value.strip()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare or restore GitHub Actions send state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--artifacts-dir", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    prepare.add_argument("--branch", required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--run-attempt", required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", required=True, type=Path)
    restore.add_argument("--artifacts-dir", required=True, type=Path)
    restore.add_argument("--branch", required=True)
    restore.add_argument("--workflow", default=STATE_WORKFLOW)

    extract = subparsers.add_parser("extract-zip")
    extract.add_argument("--zip", required=True, dest="zip_path", type=Path)
    extract.add_argument("--output", required=True, type=Path)

    layout = subparsers.add_parser("validate-layout")
    layout.add_argument("--artifacts-dir", required=True, type=Path)
    layout.add_argument("--accounts-file", type=Path)
    layout.add_argument("--allow-empty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_archive(
                args.artifacts_dir,
                args.output,
                branch=args.branch,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
            )
        elif args.command == "restore":
            restore_archive(args.archive, args.artifacts_dir, branch=args.branch, workflow=args.workflow)
        elif args.command == "extract-zip":
            extract_state_archive(args.zip_path, args.output)
        else:
            account_ids = _load_account_ids(args.accounts_file) if args.accounts_file else None
            validate_state_layout(args.artifacts_dir, account_ids=account_ids, allow_empty=args.allow_empty)
    except NoStateFilesError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except StateArchiveError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
