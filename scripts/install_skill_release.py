#!/usr/bin/env python3
"""Validate and atomically install one Auto Foundry skill ZIP.

The installer is deliberately local and stdlib-only.  It validates a complete
release, stages it outside every Codex discovery root, and swaps directories
under a cross-process lock.  A small fsynced intent journal makes the swap
recoverable after a process dies between renames.  The journal, lock, stages,
and archives are all outside the active ``skills`` discovery directory.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from typing import Callable, Iterator, Mapping
import uuid
import zipfile


SKILL_NAME = "auto-foundry-agentic-e2e"
SKILL_VERSION = "0.7.2"
CORE_VERSION = "0.8.1"
RELEASE_SLUG = "universal-data-room-ingestion"
# Provisioning updates this tracked value after the deterministic package is
# built. API callers may inject an expected hash/count for self-contained
# tests; the production CLI has no such override.
PRODUCTION_PACKAGE_SHA256 = "ab73f92778616f40908120cf0f711781417e6af5595a1c1f7d081dbd58c3e30b"
PRODUCTION_FILE_COUNT = 30

_TRANSACTION_DIRNAME = f".{SKILL_NAME}-installer"
_LOCK_FILENAME = "install.lock"
_INTENT_FILENAME = "swap.intent.json"
_STAGE_DIRNAME = "stages"
_INTENT_SCHEMA = 1
# macOS presents these administrator-owned aliases on the normal temporary
# directory path. They are not user-controlled discovery aliases; all other
# symlink components remain fail-closed.
_SYSTEM_PATH_ALIASES = {"/var", "/tmp", "/etc"}


class ReleaseInstallError(ValueError):
    """Raised when a package or swap cannot be admitted safely."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _raw_path(path: Path | str) -> Path:
    """Return an absolute lexical path without following any symlink."""

    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _reject_symlink_components(path: Path | str, *, allow_missing_leaf: bool = True) -> Path:
    """Reject raw symlink components before any call to ``resolve``.

    A missing leaf (and missing descendants after it) is allowed for paths we
    are about to create. Existing ancestors are always lstat'ed, so a
    discovery root cannot be reached through a symlink alias.
    """

    raw = _raw_path(path)
    current = Path(raw.anchor or os.sep)
    parts = raw.parts[1:] if raw.anchor else raw.parts
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if allow_missing_leaf:
                break
            raise ReleaseInstallError(f"path component does not exist: {current}")
        except OSError as exc:
            raise ReleaseInstallError(f"cannot inspect path component: {current}") from exc
        if stat.S_ISLNK(mode) and str(current) not in _SYSTEM_PATH_ALIASES:
            raise ReleaseInstallError(f"symlink path component is not allowed: {current}")
    return raw


def _require_existing_dir(path: Path | str, description: str) -> Path:
    raw = _reject_symlink_components(path, allow_missing_leaf=False)
    if not raw.is_dir() or raw.is_symlink():
        raise ReleaseInstallError(f"{description} is not a regular directory: {raw}")
    resolved = raw.resolve(strict=True)
    if not resolved.is_dir():
        raise ReleaseInstallError(f"{description} is not canonical: {raw}")
    return raw


def _require_existing_file(path: Path | str, description: str) -> Path:
    raw = _reject_symlink_components(path, allow_missing_leaf=False)
    if raw.is_symlink() or not raw.is_file():
        raise ReleaseInstallError(f"{description} is not a regular file: {raw}")
    resolved = raw.resolve(strict=True)
    if not resolved.is_file():
        raise ReleaseInstallError(f"{description} is not canonical: {raw}")
    return raw


def _mkdir_checked(path: Path, description: str) -> Path:
    raw = _reject_symlink_components(path, allow_missing_leaf=True)
    if raw.exists():
        if raw.is_symlink() or not raw.is_dir():
            raise ReleaseInstallError(f"{description} is not a regular directory: {raw}")
        return raw
    try:
        raw.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # Another process may establish the stable transaction parent just
        # before this one. Re-lstat and accept it only when it is still a
        # regular directory; never follow a replacement symlink.
        _reject_symlink_components(raw, allow_missing_leaf=False)
        if raw.is_symlink() or not raw.is_dir():
            raise ReleaseInstallError(f"{description} is not a regular directory: {raw}")
        return raw
    _fsync_directory(raw.parent)
    return raw


def _skill_files(root: Path) -> dict[str, bytes]:
    root = _require_existing_dir(root, "skill tree")
    result: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseInstallError(f"skill tree contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            continue
        result[path.relative_to(root).as_posix()] = path.read_bytes()
    return dict(sorted(result.items()))


def _deterministic_zip_bytes(root: Path) -> bytes:
    import io

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, content in _skill_files(root).items():
            info = zipfile.ZipInfo(f"{SKILL_NAME}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return payload.getvalue()


def _validate_frontmatter(skill_text: str) -> None:
    if not skill_text.startswith("---\n"):
        raise ReleaseInstallError("SKILL.md frontmatter is missing")
    parts = skill_text.split("---\n", 2)
    if len(parts) != 3:
        raise ReleaseInstallError("SKILL.md frontmatter is incomplete")
    frontmatter = {line.strip() for line in parts[1].splitlines() if line.strip()}
    required = {
        f"name: {SKILL_NAME}",
        f'version: "{SKILL_VERSION}"',
        "core_name: auto_foundry_core",
        f'core_version: "{CORE_VERSION}"',
        f"release: {RELEASE_SLUG}",
    }
    if not required.issubset(frontmatter):
        raise ReleaseInstallError("SKILL.md frontmatter/version markers are invalid")
    if f"skill_version: {SKILL_VERSION}" not in skill_text or f"core_version: {CORE_VERSION}" not in skill_text:
        raise ReleaseInstallError("SKILL.md runtime version markers are invalid")


def inspect_release(
    zip_path: Path,
    *,
    expected_sha256: str = PRODUCTION_PACKAGE_SHA256,
    expected_file_count: int = PRODUCTION_FILE_COUNT,
) -> dict[str, object]:
    """Validate one complete deterministic release archive without extracting it."""

    zip_path = _require_existing_file(zip_path, "release ZIP")
    actual_sha = _sha256_file(zip_path)
    if expected_sha256 and actual_sha != expected_sha256:
        raise ReleaseInstallError("release ZIP SHA-256 does not match the production manifest")
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            bad_crc = archive.testzip()
            if bad_crc is not None:
                raise ReleaseInstallError(f"release ZIP CRC validation failed: {bad_crc}")
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ReleaseInstallError("release ZIP contains duplicate paths")
            if len(names) != expected_file_count:
                raise ReleaseInstallError(
                    f"release ZIP file count mismatch: {len(names)} != {expected_file_count}"
                )
            files: dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                if not _safe_member(name) or not name.startswith(f"{SKILL_NAME}/"):
                    raise ReleaseInstallError(f"release ZIP contains an unsafe or unexpected path: {name}")
                if name.endswith("/") or _is_zip_symlink(info):
                    raise ReleaseInstallError(f"release ZIP contains a directory or symlink entry: {name}")
                relative = name.removeprefix(f"{SKILL_NAME}/")
                if not relative or relative.startswith("/") or not _safe_member(relative):
                    raise ReleaseInstallError(f"release ZIP member path is unsafe: {name}")
                files[relative] = archive.read(info)
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise ReleaseInstallError(f"release ZIP cannot be validated: {exc}") from exc
    if "SKILL.md" not in files:
        raise ReleaseInstallError("release ZIP does not contain SKILL.md")
    try:
        _validate_frontmatter(files["SKILL.md"].decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ReleaseInstallError("SKILL.md is not valid UTF-8") from exc
    return {
        "zip": str(zip_path),
        "zip_sha256": actual_sha,
        "file_count": len(files),
        "crc": "PASS",
        "frontmatter": "PASS",
        "members": tuple(sorted(files)),
        "member_sha256": {name: _sha256_bytes(content) for name, content in sorted(files.items())},
    }


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    _reject_symlink_components(path, allow_missing_leaf=True)
    if path.is_symlink():
        raise ReleaseInstallError(f"intent journal cannot be a symlink: {path}")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _reject_symlink_components(temporary, allow_missing_leaf=True)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink(missing_ok=True)


@contextmanager
def _installer_lock(lock_path: Path) -> Iterator[None]:
    """Take a stable POSIX advisory lock outside Codex discovery."""

    lock_path = _reject_symlink_components(lock_path, allow_missing_leaf=True)
    _mkdir_checked(lock_path.parent, "installer lock parent")
    if lock_path.is_symlink():
        raise ReleaseInstallError("installer lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ReleaseInstallError(f"cannot open installer lock: {lock_path}") from exc
    stream = os.fdopen(descriptor, "r+b", closefd=True)
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _safe_extract(zip_path: Path, destination: Path, members: tuple[str, ...]) -> None:
    destination = _raw_path(destination)
    _reject_symlink_components(destination, allow_missing_leaf=True)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(zip_path, "r") as archive:
        for relative in members:
            if not _safe_member(relative):
                raise ReleaseInstallError(f"release ZIP extraction path is unsafe: {relative}")
            target = destination / relative
            _reject_symlink_components(target.parent, allow_missing_leaf=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ReleaseInstallError(f"release ZIP extraction path collision: {relative}")
            resolved_parent = target.parent.resolve(strict=False)
            resolved_destination = destination.resolve(strict=True)
            if resolved_parent != resolved_destination and resolved_destination not in resolved_parent.parents:
                raise ReleaseInstallError(f"release ZIP extraction escaped staging root: {relative}")
            info = archive.getinfo(f"{SKILL_NAME}/{relative}")
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(target, 0o644)
    _fsync_directory(destination)


def _archive_destination(archive_root: Path, label: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return archive_root / f"{label}-{stamp}-{uuid.uuid4().hex[:12]}"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _backup_candidates(skills_root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for candidate in sorted(skills_root.iterdir(), key=lambda item: item.name):
        if not candidate.name.startswith(f"{SKILL_NAME}."):
            continue
        if candidate.is_symlink():
            raise ReleaseInstallError(f"skill backup entry is a symlink: {candidate}")
        if not candidate.is_dir():
            continue
        files = _skill_files(candidate)
        if "SKILL.md" in files:
            result.append(candidate)
    return tuple(result)


def _tree_hash(path: Path) -> str:
    return _sha256_bytes(_deterministic_zip_bytes(path))


def _intent_path(transaction_root: Path) -> Path:
    return transaction_root / _INTENT_FILENAME


def _read_intent(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    _reject_symlink_components(path, allow_missing_leaf=False)
    if path.is_symlink() or not path.is_file():
        raise ReleaseInstallError("installer intent journal is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReleaseInstallError("installer intent journal is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != _INTENT_SCHEMA:
        raise ReleaseInstallError("installer intent journal schema is invalid")
    if value.get("state") not in {"staged", "active_archived", "activated"}:
        raise ReleaseInstallError("installer intent journal state is invalid")
    return value


def _unlink_intent(path: Path) -> None:
    _reject_symlink_components(path, allow_missing_leaf=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ReleaseInstallError("refusing to remove a non-regular installer intent")
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _intent_path_value(intent: Mapping[str, object], key: str) -> Path:
    value = intent.get(key)
    if not isinstance(value, str) or not value.startswith(os.sep):
        raise ReleaseInstallError(f"installer intent {key} is not an absolute path")
    return _reject_symlink_components(Path(value), allow_missing_leaf=True)


def _move_records(intent: Mapping[str, object]) -> list[tuple[Path, Path]]:
    raw = intent.get("moves")
    if not isinstance(raw, list):
        raise ReleaseInstallError("installer intent moves are invalid")
    moves: list[tuple[Path, Path]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ReleaseInstallError("installer intent move is invalid")
        original = _intent_path_value(item, "original")
        archived = _intent_path_value(item, "archived")
        moves.append((original, archived))
    return moves


def _validate_intent(
    intent: Mapping[str, object],
    transaction_root: Path,
    skills_root: Path,
    *,
    approved_archive_root: Path | None = None,
) -> None:
    if approved_archive_root is None:
        approved_archive_root = _raw_path(skills_root.parent / "skill-archives")
    expected_transaction = _require_existing_dir(transaction_root, "installer transaction root")
    recorded_transaction = _intent_path_value(intent, "transaction_root")
    if recorded_transaction != expected_transaction:
        raise ReleaseInstallError("installer intent transaction root does not match this invocation")
    recorded_skills = _intent_path_value(intent, "skills_root")
    if recorded_skills != skills_root:
        raise ReleaseInstallError("installer intent discovery root does not match this invocation")
    archive_root = _intent_path_value(intent, "archive_root")
    if approved_archive_root is not None and archive_root != approved_archive_root:
        raise ReleaseInstallError("installer intent archive root is not approved for this invocation")
    if _is_under(archive_root, skills_root):
        raise ReleaseInstallError("installer intent archive root is inside discovery")
    _reject_symlink_components(archive_root, allow_missing_leaf=True)
    active = _intent_path_value(intent, "active")
    if active != skills_root / SKILL_NAME:
        raise ReleaseInstallError("installer intent active path is invalid")
    staged = _intent_path_value(intent, "staged")
    if staged.parent.parent != expected_transaction / _STAGE_DIRNAME:
        raise ReleaseInstallError("installer intent stage path is invalid")
    for original, archived in _move_records(intent):
        if original.parent != skills_root or not _is_under(archived, archive_root):
            raise ReleaseInstallError("installer intent move path is invalid")
        _reject_symlink_components(original, allow_missing_leaf=True)
        _reject_symlink_components(archived, allow_missing_leaf=True)
    zip_path = _intent_path_value(intent, "zip")
    if zip_path.exists():
        _require_existing_file(zip_path, "installer intent release ZIP")


def _guarded_replace(source: Path, target: Path, *, source_description: str) -> None:
    _reject_symlink_components(source, allow_missing_leaf=False)
    _reject_symlink_components(target, allow_missing_leaf=True)
    if source.is_symlink() or not source.exists():
        raise ReleaseInstallError(f"{source_description} disappeared before swap: {source}")
    if target.exists() or target.is_symlink():
        raise ReleaseInstallError(f"refusing to overwrite existing swap target: {target}")
    os.replace(source, target)
    _fsync_directory(source.parent)
    if target.parent != source.parent:
        _fsync_directory(target.parent)


def _validate_staged(staged: Path, intent: Mapping[str, object]) -> None:
    members = intent.get("members")
    member_sha = intent.get("member_sha256")
    expected_zip = intent.get("zip_sha256")
    if not isinstance(members, list) or not isinstance(member_sha, Mapping) or not isinstance(expected_zip, str):
        raise ReleaseInstallError("installer intent staged package metadata is invalid")
    files = _skill_files(staged)
    if set(files) != {str(item) for item in members}:
        raise ReleaseInstallError("installer staged member set drifted")
    if any(_sha256_bytes(files[name]) != str(member_sha[name]) for name in files):
        raise ReleaseInstallError("installer staged member hash drifted")
    if _tree_hash(staged) != expected_zip:
        raise ReleaseInstallError("installer staged package hash drifted")


def _reconcile_moves(
    moves: list[tuple[Path, Path]],
    *,
    rollback: bool,
    expected_hashes: Mapping[str, str] | None = None,
) -> None:
    sequence = reversed(moves) if rollback else moves
    expected_hashes = expected_hashes or {}
    for original, archived in sequence:
        original_exists = original.exists() or original.is_symlink()
        archived_exists = archived.exists() or archived.is_symlink()
        expected = expected_hashes.get(str(original))
        if expected and original_exists and _tree_hash(original) != expected:
            raise ReleaseInstallError(f"installer original path changed: {original}")
        if expected and archived_exists and _tree_hash(archived) != expected:
            raise ReleaseInstallError(f"installer archive path changed: {archived}")
        if original_exists and archived_exists:
            raise ReleaseInstallError(f"installer move has two live paths: {original} and {archived}")
        if rollback and archived_exists:
            _guarded_replace(archived, original, source_description="archived skill")
        elif not rollback and not archived_exists:
            _guarded_replace(original, archived, source_description="skill tree")
        elif not rollback and archived_exists:
            continue
        elif not rollback and original_exists:
            raise ReleaseInstallError(f"installer move was not completed: {original}")
        elif not rollback:
            raise ReleaseInstallError(f"installer move lost both paths: {original}")


def _move_hashes(intent: Mapping[str, object], moves: list[tuple[Path, Path]]) -> dict[str, str]:
    raw = intent.get("move_hashes")
    if not isinstance(raw, Mapping):
        raise ReleaseInstallError("installer intent move hashes are invalid")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ReleaseInstallError("installer intent move hash is invalid")
        result[key] = value
    missing = [str(original) for original, _ in moves if str(original) not in result]
    if missing:
        raise ReleaseInstallError("installer intent move hash is missing")
    return result


def _recover_pending(
    transaction_root: Path,
    skills_root: Path,
    *,
    approved_archive_root: Path | None = None,
) -> None:
    intent_path = _intent_path(transaction_root)
    intent = _read_intent(intent_path)
    if intent is None:
        return
    _validate_intent(
        intent,
        transaction_root,
        skills_root,
        approved_archive_root=approved_archive_root,
    )
    state = str(intent["state"])
    staged = _intent_path_value(intent, "staged")
    active = _intent_path_value(intent, "active")
    moves = _move_records(intent)
    move_hashes = _move_hashes(intent, moves)
    expected_zip = intent.get("zip_sha256")
    if not isinstance(expected_zip, str):
        raise ReleaseInstallError("installer intent package hash is invalid")
    if state == "staged":
        _reconcile_moves(moves, rollback=True, expected_hashes=move_hashes)
        if staged.exists() or staged.is_symlink():
            _validate_staged(staged, intent)
            shutil.rmtree(staged)
            _fsync_directory(staged.parent)
        _unlink_intent(intent_path)
        return
    if state == "active_archived":
        active_is_new = active.exists() and not active.is_symlink() and active.is_dir() and _tree_hash(active) == expected_zip
        if active.exists() or active.is_symlink():
            if not active_is_new:
                raise ReleaseInstallError("installer active path conflicts during recovery")
            # The activation rename may have completed just before a process
            # died, while the journal still says ``active_archived``. The
            # active move's original path is now intentionally occupied by
            # the new tree; reconcile every other archive move only.
            active_moves = [(original, archived) for original, archived in moves if original != active]
            active_hashes = {key: value for key, value in move_hashes.items() if key != str(active)}
            _reconcile_moves(active_moves, rollback=False, expected_hashes=active_hashes)
            if staged.exists() or staged.is_symlink():
                _validate_staged(staged, intent)
                shutil.rmtree(staged)
                _fsync_directory(staged.parent)
        else:
            _reconcile_moves(moves, rollback=False, expected_hashes=move_hashes)
            _validate_staged(staged, intent)
            _guarded_replace(staged, active, source_description="staged skill")
        intent = dict(intent)
        intent["state"] = "activated"
        _atomic_json(intent_path, intent)
        _unlink_intent(intent_path)
        return
    if not active.exists() or active.is_symlink() or not active.is_dir() or _tree_hash(active) != expected_zip:
        raise ReleaseInstallError("installer activated state does not match the production package")
    if staged.exists() or staged.is_symlink():
        _validate_staged(staged, intent)
        shutil.rmtree(staged)
        _fsync_directory(staged.parent)
    _unlink_intent(intent_path)


def _make_intent(
    *,
    transaction_root: Path,
    skills_root: Path,
    archive_root: Path,
    zip_path: Path,
    staged: Path,
    active: Path,
    moves: list[tuple[Path, Path]],
    inspection: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": _INTENT_SCHEMA,
        "transaction_id": uuid.uuid4().hex,
        "state": "staged",
        "transaction_root": str(transaction_root),
        "skills_root": str(skills_root),
        "archive_root": str(archive_root),
        "zip": str(zip_path),
        "zip_sha256": str(inspection["zip_sha256"]),
        "file_count": int(inspection["file_count"]),
        "members": [str(value) for value in inspection["members"]],
        "member_sha256": dict(inspection["member_sha256"]),
        "active": str(active),
        "staged": str(staged),
        "moves": [{"original": str(original), "archived": str(archived)} for original, archived in moves],
        "move_hashes": {str(original): _tree_hash(original) for original, _ in moves},
    }


def install_skill_release(
    zip_path: Path,
    skills_root: Path,
    *,
    archive_root: Path | None = None,
    expected_sha256: str = PRODUCTION_PACKAGE_SHA256,
    expected_file_count: int = PRODUCTION_FILE_COUNT,
    dry_run: bool = False,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Stage, validate, and atomically replace one active skill directory."""

    skills_root = _require_existing_dir(skills_root, "skills discovery root")
    archive_root = _raw_path(archive_root or skills_root.parent / "skill-archives")
    _reject_symlink_components(archive_root, allow_missing_leaf=True)
    if _is_under(archive_root, skills_root):
        raise ReleaseInstallError("skill archive root must be outside the discovery root")
    transaction_root = _raw_path(skills_root.parent / _TRANSACTION_DIRNAME)
    _reject_symlink_components(transaction_root, allow_missing_leaf=True)
    if _is_under(transaction_root, skills_root):
        raise ReleaseInstallError("installer transaction root must be outside discovery")
    _mkdir_checked(transaction_root, "installer transaction root")
    _mkdir_checked(transaction_root / _STAGE_DIRNAME, "installer stage root")
    lock_path = transaction_root / _LOCK_FILENAME
    intent_path = _intent_path(transaction_root)

    with _installer_lock(lock_path):
        _recover_pending(
            transaction_root,
            skills_root,
            approved_archive_root=archive_root,
        )
        zip_path = _require_existing_file(zip_path, "release ZIP")
        inspection = inspect_release(zip_path, expected_sha256=expected_sha256, expected_file_count=expected_file_count)
        active = skills_root / SKILL_NAME
        _reject_symlink_components(active, allow_missing_leaf=True)
        if active.is_symlink() or (active.exists() and not active.is_dir()):
            raise ReleaseInstallError("active skill path is not a regular directory")
        backups = _backup_candidates(skills_root)
        result: dict[str, object] = {
            **{key: value for key, value in inspection.items() if key != "members"},
            "skills_root": str(skills_root),
            "active": str(active),
            "dry_run": bool(dry_run),
            "backup_count": len(backups),
            "archive_root": str(archive_root),
        }
        if active.exists() and _tree_hash(active) == str(inspection["zip_sha256"]):
            result.update({"installed": True, "already_current": True, "archive": None})
            return result
        if dry_run:
            result["installed"] = False
            return result
        _mkdir_checked(archive_root, "skill archive root")
        if _is_under(archive_root, skills_root):
            raise ReleaseInstallError("skill archive root must be outside the discovery root")

        stage_parent = Path(tempfile.mkdtemp(prefix="stage-", dir=str(transaction_root / _STAGE_DIRNAME)))
        _reject_symlink_components(stage_parent, allow_missing_leaf=False)
        staged = stage_parent / SKILL_NAME
        moves: list[tuple[Path, Path]] = []
        try:
            members = tuple(str(value) for value in inspection["members"])
            _safe_extract(zip_path, staged, members)
            staged_files = _skill_files(staged)
            expected_member_sha = dict(inspection["member_sha256"])
            if set(staged_files) != set(expected_member_sha) or any(
                _sha256_bytes(staged_files[name]) != str(expected_member_sha[name]) for name in staged_files
            ):
                raise ReleaseInstallError("staged skill per-file hashes do not match the release package")
            if _tree_hash(staged) != str(inspection["zip_sha256"]):
                raise ReleaseInstallError("staged skill bytes do not match the release ZIP hash")
            for backup in backups:
                moves.append((backup, _archive_destination(archive_root, backup.name)))
            if active.exists():
                moves.append((active, _archive_destination(archive_root, SKILL_NAME)))
            intent = _make_intent(
                transaction_root=transaction_root,
                skills_root=skills_root,
                archive_root=archive_root,
                zip_path=zip_path,
                staged=staged,
                active=active,
                moves=moves,
                inspection=inspection,
            )
            _atomic_json(intent_path, intent)
            if failpoint is not None:
                failpoint("after_stage")
            for original, archived in moves:
                _guarded_replace(original, archived, source_description="skill tree")
                if failpoint is not None:
                    failpoint("after_active_archive" if original == active else "after_backup_archive")
            _fsync_directory(skills_root)
            intent["state"] = "active_archived"
            _atomic_json(intent_path, intent)
            if failpoint is not None:
                failpoint("after_archive")
            _guarded_replace(staged, active, source_description="staged skill")
            _fsync_directory(skills_root)
            if failpoint is not None:
                failpoint("after_activate_rename")
            intent["state"] = "activated"
            _atomic_json(intent_path, intent)
            if failpoint is not None:
                failpoint("after_activate")
            _unlink_intent(intent_path)
            result.update(
                {
                    "archive": str(moves[-1][1]) if moves and moves[-1][0] == active else None,
                    "installed": True,
                    "already_current": False,
                }
            )
            return result
        except BaseException as exc:
            # Normal exceptions are rolled back while the lock is held. A
            # process-death failpoint (os._exit) bypasses this block and leaves
            # the durable intent for the next invocation to recover.
            try:
                intent_value = _read_intent(intent_path)
                if intent_value is not None:
                    _validate_intent(
                        intent_value,
                        transaction_root,
                        skills_root,
                        approved_archive_root=archive_root,
                    )
                    if (
                        intent_value.get("state") == "activated"
                        and active.exists()
                        and not active.is_symlink()
                        and active.is_dir()
                        and _tree_hash(active) == str(inspection["zip_sha256"])
                    ):
                        shutil.rmtree(active)
                        _fsync_directory(skills_root)
                    rollback_moves = _move_records(intent_value)
                    _reconcile_moves(
                        rollback_moves,
                        rollback=True,
                        expected_hashes=_move_hashes(intent_value, rollback_moves),
                    )
                    staged_path = _intent_path_value(intent_value, "staged")
                    if staged_path.exists() or staged_path.is_symlink():
                        _validate_staged(staged_path, intent_value)
                        shutil.rmtree(staged_path)
                        _fsync_directory(staged_path.parent)
                    _unlink_intent(intent_path)
            except BaseException:
                pass
            raise ReleaseInstallError(f"skill release swap failed and was rolled back: {exc}") from exc
        finally:
            if stage_parent.exists() or stage_parent.is_symlink():
                try:
                    shutil.rmtree(stage_parent)
                except OSError:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", dest="zip_path", type=Path, required=True)
    parser.add_argument("--skills-root", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install_skill_release(
            args.zip_path,
            args.skills_root,
            archive_root=args.archive_root,
            dry_run=args.dry_run,
        )
    except (OSError, ReleaseInstallError) as exc:
        print(f"skill install: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=list))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
