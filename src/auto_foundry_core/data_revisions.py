"""Run-owned immutable data revisions.

This module is deliberately a small persistence boundary.  A revision records
one fully materialized ZIP archive and the canonical physical catalog produced
by :class:`~auto_foundry_core.workbench.DataRoom`.  Revision manifests and the
single current pointer are canonical, hash-bound JSON.  Archive and catalog
bytes are write-once; only the pointer is atomically replaced.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from types import MappingProxyType
from typing import Any, Mapping
import uuid

from .contracts import DataAssetRef
from .lifecycle import RunLifecycle
from .workbench import DataRoom, DataRoomCatalogEntry, DataRoomMember, _catalog_identity_key
from .workspace import AllowedRootError, RunContext


DATA_REVISION_SCHEMA_VERSION = "auto_foundry.data_revision.v1"
DATA_REVISION_POINTER_SCHEMA_VERSION = "auto_foundry.data_revision_pointer.v1"
DATA_ROOM_ROOT = Path("data_room")
REVISION_ROOT = DATA_ROOM_ROOT / "revisions"
CURRENT_POINTER_PATH = DATA_ROOM_ROOT / "current_revision.json"
PENDING_DATA_REFRESH_PATH = DATA_ROOM_ROOT / "pending_data_refresh.json"
PENDING_DATA_REFRESH_ARCHIVE_ROOT = DATA_ROOM_ROOT / "pending_data_refresh_applied"
REVISION_TRANSACTION_PATH = DATA_ROOM_ROOT / "revision_transaction.json"
REVISION_MANIFEST_FILENAME = "revision_manifest.json"
REVISION_ARCHIVE_FILENAME = "archive.zip"
REVISION_CATALOG_FILENAME = "catalog.json"
_REVISION_ID_RE = re.compile(r"^D-(\d{4,})$")
_GENERATION_ID_RE = re.compile(r"^G-(\d{4,})$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PENDING_DATA_REFRESH_SCHEMA_VERSION = "auto_foundry.data_refresh_admission.v1"
REVISION_TRANSACTION_SCHEMA_VERSION = "auto_foundry.data_revision_transaction.v1"

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "revision_id",
        "ordinal",
        "parent_revision_id",
        "parent_manifest_hash",
        "archive_path",
        "archive_sha256",
        "archive_size_bytes",
        "archive_alias",
        "archive_source_stat",
        "central_directory_fingerprint",
        "catalog_path",
        "catalog_key",
        "catalog_sha256",
        "catalog_schema_version",
        "catalog_source_hash",
        "catalog_core_version",
        "physical_inventory",
        "member_inventory_hash",
        "manifest_hash",
    }
)
_POINTER_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "revision_id",
        "ordinal",
        "manifest_path",
        "manifest_hash",
        "pointer_hash",
    }
)
_CATALOG_FIELDS = frozenset(
    {
        "catalog_schema_version",
        "catalog_key",
        "source_hash",
        "core_version",
        "archive",
        "counts",
        "entries",
    }
)
_PENDING_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "data_revision",
        "data_revision_ref",
        "data_revision_id",
        "data_revision_manifest_hash",
        "data_revision_archive_sha256",
        "plan",
        "plan_hash",
        "reopened_item_ids",
        "expected_parent_generation_id",
        "expected_parent_state_hash",
        "expected_parent_plan_hash",
        "original_parent_generation_id",
        "original_parent_state_hash",
        "original_parent_plan_hash",
        "launch_draft_id",
        "launch_fingerprint",
        "created_at",
        "intent_hash",
        "state",
        "applied_generation_id",
    }
)
_TRANSACTION_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "revision_id",
        "revision_manifest_hash",
        "revision_archive_sha256",
        "parent_revision_id",
        "parent_manifest_hash",
        "launch_draft_id",
        "launch_fingerprint",
        "created_at",
        "phase",
        "data_revision",
        "data_revision_ref",
        "plan",
        "reopened_item_ids",
        "expected_parent_generation_id",
        "expected_parent_state_hash",
        "expected_parent_plan_hash",
        "transaction_hash",
    }
)


class DataRevisionError(ValueError):
    """Base error for invalid or conflicting revision state."""


class RevisionConflictError(DataRevisionError):
    """Raised when an occupied revision contains different immutable bytes."""


class RevisionCASMismatch(DataRevisionError):
    """Raised when an append's expected current revision is stale."""


class PendingDataRefreshConflict(DataRevisionError):
    """Raised when a canonical pending refresh cannot be coalesced safely."""


@dataclass(frozen=True)
class DataRevisionTransaction:
    """Durable write-ahead handoff for a newly published current revision."""

    run_id: str
    revision_id: str
    revision_manifest_hash: str
    revision_archive_sha256: str
    parent_revision_id: str | None
    parent_manifest_hash: str | None
    launch_draft_id: str
    launch_fingerprint: str
    created_at: str
    phase: str = "revision_published"
    data_revision: Mapping[str, Any] | None = None
    data_revision_ref: str | None = None
    plan: Mapping[str, Any] | None = None
    reopened_item_ids: tuple[str, ...] = ()
    expected_parent_generation_id: str | None = None
    expected_parent_state_hash: str | None = None
    expected_parent_plan_hash: str | None = None
    transaction_hash: str = ""

    def __post_init__(self) -> None:
        revision_id, ordinal = _parse_revision_id(self.revision_id, label="transaction revision_id")
        if not isinstance(self.run_id, str) or not self.run_id or Path(self.run_id).name != self.run_id:
            raise DataRevisionError("transaction run_id is invalid")
        _hash_value(self.revision_manifest_hash, label="transaction revision_manifest_hash")
        _hash_value(self.revision_archive_sha256, label="transaction revision_archive_sha256")
        if ordinal == 1:
            if self.parent_revision_id is not None or self.parent_manifest_hash is not None:
                raise DataRevisionError("D-0001 transaction cannot have a parent")
        else:
            if self.parent_revision_id is None or self.parent_manifest_hash is None:
                raise DataRevisionError("transaction parent identity is required")
            _parse_revision_id(self.parent_revision_id, label="transaction parent_revision_id")
            _hash_value(self.parent_manifest_hash, label="transaction parent_manifest_hash")
        if not isinstance(self.launch_draft_id, str) or not self.launch_draft_id or Path(self.launch_draft_id).name != self.launch_draft_id:
            raise DataRevisionError("transaction launch_draft_id is invalid")
        _hash_value(self.launch_fingerprint, label="transaction launch_fingerprint")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise DataRevisionError("transaction created_at is invalid")
        if self.phase != "revision_published":
            raise DataRevisionError("transaction phase is invalid")
        handoff_values = (
            self.data_revision,
            self.data_revision_ref,
            self.plan,
            self.expected_parent_generation_id,
            self.expected_parent_state_hash,
            self.expected_parent_plan_hash,
        )
        if any(value is not None for value in handoff_values):
            if any(value is None for value in handoff_values):
                raise DataRevisionError("transaction handoff is incomplete")
            if not isinstance(self.data_revision, Mapping) or self.data_revision.get("revision_id") != revision_id:
                raise DataRevisionError("transaction data revision payload is invalid")
            if not isinstance(self.data_revision_ref, str) or not self.data_revision_ref or Path(self.data_revision_ref).is_absolute():
                raise DataRevisionError("transaction data revision reference is invalid")
            if not isinstance(self.plan, Mapping):
                raise DataRevisionError("transaction handoff plan is invalid")
            try:
                canonical_plan = self.plan
                from .requirement_planning import RequirementExecutionPlan

                canonical_plan = RequirementExecutionPlan.from_dict(dict(self.plan)).to_dict()
            except (KeyError, TypeError, ValueError) as exc:
                raise DataRevisionError("transaction handoff plan is invalid") from exc
            if _jsonable(canonical_plan) != _jsonable(self.plan):
                raise DataRevisionError("transaction handoff plan is not canonical")
            if isinstance(self.reopened_item_ids, (str, bytes)):
                raise DataRevisionError("transaction reopened_item_ids are invalid")
            reopened = tuple(str(value) for value in self.reopened_item_ids)
            if len(reopened) != len(set(reopened)) or any(
                not value or Path(value).name != value or value in {".", ".."} for value in reopened
            ):
                raise DataRevisionError("transaction reopened_item_ids are invalid")
            _parse_generation_id(self.expected_parent_generation_id, label="transaction expected_parent_generation_id")
            _hash_value(self.expected_parent_state_hash, label="transaction expected_parent_state_hash")
            _hash_value(self.expected_parent_plan_hash, label="transaction expected_parent_plan_hash")
            object.__setattr__(self, "data_revision", _freeze(self.data_revision))
            object.__setattr__(self, "plan", _freeze(self.plan))
            object.__setattr__(self, "reopened_item_ids", reopened)
        else:
            object.__setattr__(self, "reopened_item_ids", tuple(self.reopened_item_ids or ()))
        unsigned = {
            "schema_version": REVISION_TRANSACTION_SCHEMA_VERSION,
            "kind": "data_revision_transaction",
            "run_id": self.run_id,
            "revision_id": revision_id,
            "revision_manifest_hash": self.revision_manifest_hash,
            "revision_archive_sha256": self.revision_archive_sha256,
            "parent_revision_id": self.parent_revision_id,
            "parent_manifest_hash": self.parent_manifest_hash,
            "launch_draft_id": self.launch_draft_id,
            "launch_fingerprint": self.launch_fingerprint,
            "created_at": self.created_at,
            "phase": self.phase,
            "data_revision": _jsonable(self.data_revision),
            "data_revision_ref": self.data_revision_ref,
            "plan": _jsonable(self.plan),
            "reopened_item_ids": list(self.reopened_item_ids),
            "expected_parent_generation_id": self.expected_parent_generation_id,
            "expected_parent_state_hash": self.expected_parent_state_hash,
            "expected_parent_plan_hash": self.expected_parent_plan_hash,
        }
        expected_hash = _hash_json(unsigned)
        if self.transaction_hash and self.transaction_hash != expected_hash:
            raise DataRevisionError("transaction hash does not match content")
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "transaction_hash", expected_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REVISION_TRANSACTION_SCHEMA_VERSION,
            "kind": "data_revision_transaction",
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "revision_manifest_hash": self.revision_manifest_hash,
            "revision_archive_sha256": self.revision_archive_sha256,
            "parent_revision_id": self.parent_revision_id,
            "parent_manifest_hash": self.parent_manifest_hash,
            "launch_draft_id": self.launch_draft_id,
            "launch_fingerprint": self.launch_fingerprint,
            "created_at": self.created_at,
            "phase": self.phase,
            "data_revision": _jsonable(self.data_revision),
            "data_revision_ref": self.data_revision_ref,
            "plan": _jsonable(self.plan),
            "reopened_item_ids": list(self.reopened_item_ids),
            "expected_parent_generation_id": self.expected_parent_generation_id,
            "expected_parent_state_hash": self.expected_parent_state_hash,
            "expected_parent_plan_hash": self.expected_parent_plan_hash,
            "transaction_hash": self.transaction_hash,
        }

    @property
    def has_handoff(self) -> bool:
        return self.data_revision is not None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_freeze(item) for item in sorted(value, key=str))
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DataRevisionError("data revision value is not canonical JSON") from exc
    return (encoded + "\n").encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DataRevisionError(f"cannot hash archive: {path}") from exc
    return digest.hexdigest()


def _files_equal(left: Path, right: Path) -> bool:
    """Compare regular files in bounded chunks without materialising either."""

    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as lhs, right.open("rb") as rhs:
            while True:
                left_chunk = lhs.read(1024 * 1024)
                right_chunk = rhs.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def _hash_json(value: Any) -> str:
    return _hash_bytes(_canonical_bytes(value))


def _workbench_catalog_bytes(value: Any) -> bytes:
    """Canonical byte form emitted by the existing DataRoom machinery."""

    try:
        encoded = json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DataRevisionError("catalog value is not canonical JSON") from exc
    return (encoded + "\n").encode("utf-8")


def _hash_value(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise DataRevisionError(f"{label} must be a lowercase SHA-256 hash")
    return value


def _stat_signature(path: Path) -> dict[str, int]:
    try:
        result = path.stat()
    except OSError as exc:
        raise DataRevisionError(f"cannot stat archive: {path}") from exc
    return {
        "device": int(result.st_dev),
        "inode": int(result.st_ino),
        "size_bytes": int(result.st_size),
        "mtime_ns": int(result.st_mtime_ns),
    }


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _assert_path(path: Path, *, root: Path, label: str, regular: bool = False, directory: bool = False) -> Path:
    """Validate containment and reject symlink/non-regular path components."""

    root = Path(root)
    if root.is_symlink():
        raise AllowedRootError(f"{label} root cannot be a symlink: {root}")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AllowedRootError(f"{label} escapes bound root: {path}") from exc
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        raise AllowedRootError(f"{label} escapes bound root: {path}")
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError(f"{label} cannot use symlink components: {current}")
    if regular and (path.is_symlink() or not path.is_file()):
        raise DataRevisionError(f"{label} must be a regular file: {path}")
    if directory and (path.is_symlink() or not path.is_dir()):
        raise DataRevisionError(f"{label} must be a regular directory: {path}")
    return path


def _ensure_directory(path: Path, *, root: Path, label: str) -> Path:
    _assert_path(path, root=root, label=label)
    if path.exists() and path.is_symlink():
        raise AllowedRootError(f"{label} cannot be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise DataRevisionError(f"{label} must be a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    _assert_path(path, root=root, label=label, directory=True)
    return path


def _atomic_replace(path: Path, data: bytes, *, root: Path, label: str) -> None:
    """Atomically replace one canonical file after fsyncing its bytes."""

    _assert_path(path.parent, root=root, label=f"{label} directory")
    _ensure_directory(path.parent, root=root, label=f"{label} directory")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DataRevisionError(f"{label} is not replaceable: {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", prefix=f".{path.name}.", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise DataRevisionError(f"cannot atomically publish {label}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_once(path: Path, data: bytes, *, root: Path, label: str) -> None:
    """Publish immutable bytes, accepting only an exact existing retry."""

    _assert_path(path.parent, root=root, label=f"{label} directory")
    _ensure_directory(path.parent, root=root, label=f"{label} directory")
    if path.exists() or path.is_symlink():
        _assert_path(path, root=root, label=label, regular=True)
        existing = path.read_bytes()
        if existing != data:
            raise RevisionConflictError(f"conflicting {label} bytes already exist")
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", prefix=f".{path.name}.", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            _assert_path(path, root=root, label=label, regular=True)
            if path.read_bytes() != data:
                raise RevisionConflictError(f"conflicting {label} bytes already exist")
        else:
            os.replace(temporary, path)
            temporary = None
            _fsync_directory(path.parent)
    except OSError as exc:
        raise DataRevisionError(f"cannot publish immutable {label}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_once(source: Path, destination: Path, *, root: Path, label: str) -> None:
    """Copy a materialized archive to write-once destination with fsync."""

    _assert_path(source.parent, root=source.parent, label="source directory")
    if source.is_symlink() or not source.is_file():
        raise DataRevisionError(f"{label} source must be a regular file: {source}")
    _assert_path(destination.parent, root=root, label=f"{label} directory")
    _ensure_directory(destination.parent, root=root, label=f"{label} directory")
    if destination.exists() or destination.is_symlink():
        _assert_path(destination, root=root, label=label, regular=True)
        if not _files_equal(destination, source):
            raise RevisionConflictError(f"conflicting {label} bytes already exist")
        return
    temporary: Path | None = None
    before_hash = _hash_file(source)
    try:
        with tempfile.NamedTemporaryFile("wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, stream, length=1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        if _hash_file(source) != before_hash:
            raise DataRevisionError(f"archive changed while staging: {source}")
        if _hash_file(temporary) != before_hash:
            raise DataRevisionError(f"staged archive hash does not match source: {source}")
        if destination.exists() or destination.is_symlink():
            _assert_path(destination, root=root, label=label, regular=True)
            if not _files_equal(destination, temporary):
                raise RevisionConflictError(f"conflicting {label} bytes already exist")
        else:
            os.replace(temporary, destination)
            temporary = None
            _fsync_directory(destination.parent)
    except OSError as exc:
        raise DataRevisionError(f"cannot stage immutable {label}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _revision_id(ordinal: int) -> str:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise DataRevisionError("revision ordinal must be a positive integer")
    return f"D-{ordinal:04d}"


def _parse_revision_id(value: Any, *, label: str = "revision_id") -> tuple[str, int]:
    if not isinstance(value, str):
        raise DataRevisionError(f"{label} is invalid")
    match = _REVISION_ID_RE.fullmatch(value)
    if match is None:
        raise DataRevisionError(f"{label} is invalid")
    ordinal = int(match.group(1))
    canonical = _revision_id(ordinal)
    if canonical != value:
        raise DataRevisionError(f"{label} is not canonical")
    return value, ordinal


def _parse_generation_id(value: Any, *, label: str = "generation_id") -> tuple[str, int]:
    if not isinstance(value, str) or _GENERATION_ID_RE.fullmatch(value) is None:
        raise DataRevisionError(f"{label} is invalid")
    ordinal = int(value[2:])
    if f"G-{ordinal:04d}" != value:
        raise DataRevisionError(f"{label} is not canonical")
    return value, ordinal


def _inventory_bytes(inventory: Any) -> bytes:
    return _canonical_bytes(inventory)


def _inventory_hash(inventory: Any) -> str:
    return _hash_bytes(_inventory_bytes(inventory))


def _resolve_archive_candidate(context: RunContext, value: str | Path) -> Path:
    """Resolve a candidate under one declared input root or the run root."""

    raw = Path(value).expanduser()
    if raw.is_absolute():
        lexical = raw
    else:
        lexical = None
        for root in context.read_roots:
            candidate = root / raw
            if candidate.exists() or candidate.is_symlink():
                lexical = candidate
                break
        if lexical is None:
            lexical = context.resolve_input(raw)
    resolved = lexical.resolve(strict=False)
    allowed = False
    for root in context.read_roots:
        root_resolved = root.resolve(strict=False)
        _assert_path(root_resolved, root=root_resolved, label="input root")
        if resolved == root_resolved or root_resolved in resolved.parents:
            allowed = True
            # ``/var`` is a system symlink to ``/private/var`` on macOS, so
            # validate the resolved path against the resolved input root while
            # still rejecting an explicitly symlinked archive leaf.
            _assert_path(resolved, root=root_resolved, label="archive")
            if lexical.is_symlink():
                raise AllowedRootError(f"archive cannot be a symlink: {lexical}")
            break
    if not allowed:
        raise AllowedRootError(f"archive escapes declared input/run roots: {value}")
    if lexical.is_symlink() or not lexical.is_file():
        raise DataRevisionError(f"archive must be a regular file: {lexical}")
    return resolved.resolve(strict=True)


def _path_reference(context: RunContext, path: Path) -> str:
    try:
        return str(path.relative_to(context.run_root))
    except ValueError:
        return str(path)


def _resolve_manifest_path(context: RunContext, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DataRevisionError(f"{label} is invalid")
    raw = Path(value)
    if raw.is_absolute():
        return _resolve_archive_candidate(context, raw)
    # Preserve lexical components for symlink rejection.  RunContext's
    # resolver intentionally returns a resolved path, which would hide an
    # in-root symlink pointing to another in-root file.
    return context.run_root / raw


@dataclass(frozen=True)
class DataRevision:
    """Frozen typed metadata for one published immutable revision."""

    run_id: str
    revision_id: str
    ordinal: int
    parent_revision_id: str | None
    parent_manifest_hash: str | None
    archive_path: Path
    archive_sha256: str
    archive_size_bytes: int
    archive_alias: bool
    archive_source_stat: Mapping[str, int]
    central_directory_fingerprint: Mapping[str, Any]
    catalog_path: Path
    catalog_key: str
    catalog_sha256: str
    catalog_schema_version: str
    catalog_source_hash: str
    catalog_core_version: str
    physical_inventory: tuple[Mapping[str, Any], ...]
    member_inventory_hash: str
    manifest_path: Path
    manifest_hash: str

    def __post_init__(self) -> None:
        revision_id, ordinal = _parse_revision_id(self.revision_id)
        if ordinal != self.ordinal:
            raise DataRevisionError("revision ordinal does not match revision ID")
        if not self.run_id or Path(self.run_id).name != self.run_id or self.run_id in {".", ".."}:
            raise DataRevisionError("run_id is invalid")
        if self.parent_revision_id is not None:
            _parse_revision_id(self.parent_revision_id, label="parent_revision_id")
        if self.parent_manifest_hash is not None:
            _hash_value(self.parent_manifest_hash, label="parent_manifest_hash")
        _hash_value(self.archive_sha256, label="archive_sha256")
        _hash_value(self.catalog_sha256, label="catalog_sha256")
        _hash_value(self.catalog_source_hash, label="catalog_source_hash")
        _hash_value(self.member_inventory_hash, label="member_inventory_hash")
        _hash_value(self.manifest_hash, label="manifest_hash")
        if isinstance(self.archive_size_bytes, bool) or not isinstance(self.archive_size_bytes, int) or self.archive_size_bytes < 0:
            raise DataRevisionError("archive_size_bytes is invalid")
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "archive_path", Path(self.archive_path))
        object.__setattr__(self, "catalog_path", Path(self.catalog_path))
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        object.__setattr__(self, "archive_source_stat", _freeze(self.archive_source_stat))
        object.__setattr__(self, "central_directory_fingerprint", _freeze(self.central_directory_fingerprint))
        object.__setattr__(self, "physical_inventory", tuple(_freeze(item) for item in self.physical_inventory))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "ordinal": self.ordinal,
            "parent_revision_id": self.parent_revision_id,
            "parent_manifest_hash": self.parent_manifest_hash,
            "archive_path": str(self.archive_path),
            "archive_sha256": self.archive_sha256,
            "archive_size_bytes": self.archive_size_bytes,
            "archive_alias": self.archive_alias,
            "archive_source_stat": dict(self.archive_source_stat),
            "central_directory_fingerprint": dict(self.central_directory_fingerprint),
            "catalog_path": str(self.catalog_path),
            "catalog_key": self.catalog_key,
            "catalog_sha256": self.catalog_sha256,
            "catalog_schema_version": self.catalog_schema_version,
            "catalog_source_hash": self.catalog_source_hash,
            "catalog_core_version": self.catalog_core_version,
            "physical_inventory": [_jsonable(item) for item in self.physical_inventory],
            "member_inventory_hash": self.member_inventory_hash,
            "manifest_path": str(self.manifest_path),
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True)
class PendingDataRefresh:
    """Immutable canonical admission for one not-yet-safe data refresh."""

    run_id: str
    data_revision: Mapping[str, Any]
    data_revision_ref: str
    data_revision_id: str
    data_revision_manifest_hash: str
    data_revision_archive_sha256: str
    plan: Mapping[str, Any]
    plan_hash: str
    reopened_item_ids: tuple[str, ...]
    expected_parent_generation_id: str
    expected_parent_state_hash: str
    expected_parent_plan_hash: str
    original_parent_generation_id: str
    original_parent_state_hash: str
    original_parent_plan_hash: str
    launch_draft_id: str
    launch_fingerprint: str
    created_at: str
    intent_hash: str
    state: str = "pending"
    applied_generation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id or Path(self.run_id).name != self.run_id:
            raise DataRevisionError("pending refresh run_id is invalid")
        revision_id, _ = _parse_revision_id(self.data_revision_id, label="pending data_revision_id")
        _hash_value(self.data_revision_manifest_hash, label="pending data_revision_manifest_hash")
        _hash_value(self.data_revision_archive_sha256, label="pending data_revision_archive_sha256")
        _hash_value(self.plan_hash, label="pending plan_hash")
        _hash_value(self.expected_parent_state_hash, label="pending expected_parent_state_hash")
        _hash_value(self.expected_parent_plan_hash, label="pending expected_parent_plan_hash")
        _parse_generation_id(self.original_parent_generation_id, label="pending original_parent_generation_id")
        _hash_value(self.original_parent_state_hash, label="pending original_parent_state_hash")
        _hash_value(self.original_parent_plan_hash, label="pending original_parent_plan_hash")
        _hash_value(self.launch_fingerprint, label="pending launch_fingerprint")
        _hash_value(self.intent_hash, label="pending intent_hash")
        if not isinstance(self.data_revision_ref, str) or not self.data_revision_ref or Path(self.data_revision_ref).is_absolute():
            raise DataRevisionError("pending data_revision_ref is invalid")
        match = _GENERATION_ID_RE.fullmatch(self.expected_parent_generation_id)
        if match is None or f"G-{int(match.group(1)):04d}" != self.expected_parent_generation_id:
            raise DataRevisionError("pending expected_parent_generation_id is invalid")
        if not isinstance(self.launch_draft_id, str) or not self.launch_draft_id or Path(self.launch_draft_id).name != self.launch_draft_id:
            raise DataRevisionError("pending launch_draft_id is invalid")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise DataRevisionError("pending created_at is invalid")
        if self.state not in {"pending", "applied"}:
            raise DataRevisionError("pending refresh state is invalid")
        if self.applied_generation_id is not None:
            match = _GENERATION_ID_RE.fullmatch(self.applied_generation_id)
            if match is None or f"G-{int(match.group(1)):04d}" != self.applied_generation_id:
                raise DataRevisionError("pending applied_generation_id is invalid")
        if isinstance(self.reopened_item_ids, (str, bytes)):
            raise DataRevisionError("pending reopened_item_ids are invalid")
        reopened = tuple(str(value) for value in self.reopened_item_ids)
        if len(reopened) != len(set(reopened)) or any(
            not value or Path(value).name != value or value in {".", ".."} for value in reopened
        ):
            raise DataRevisionError("pending reopened_item_ids are invalid")
        if not isinstance(self.data_revision, Mapping) or self.data_revision.get("revision_id") != revision_id:
            raise DataRevisionError("pending data revision payload is invalid")
        if not isinstance(self.plan, Mapping) or _hash_json(self.plan) != self.plan_hash:
            raise DataRevisionError("pending plan hash does not match payload")
        object.__setattr__(self, "data_revision", _freeze(self.data_revision))
        object.__setattr__(self, "plan", _freeze(self.plan))
        object.__setattr__(self, "reopened_item_ids", reopened)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PENDING_DATA_REFRESH_SCHEMA_VERSION,
            "kind": "data_refresh_admission",
            "run_id": self.run_id,
            "data_revision": _jsonable(self.data_revision),
            "data_revision_ref": self.data_revision_ref,
            "data_revision_id": self.data_revision_id,
            "data_revision_manifest_hash": self.data_revision_manifest_hash,
            "data_revision_archive_sha256": self.data_revision_archive_sha256,
            "plan": _jsonable(self.plan),
            "plan_hash": self.plan_hash,
            "reopened_item_ids": list(self.reopened_item_ids),
            "expected_parent_generation_id": self.expected_parent_generation_id,
            "expected_parent_state_hash": self.expected_parent_state_hash,
            "expected_parent_plan_hash": self.expected_parent_plan_hash,
            "original_parent_generation_id": self.original_parent_generation_id,
            "original_parent_state_hash": self.original_parent_state_hash,
            "original_parent_plan_hash": self.original_parent_plan_hash,
            "launch_draft_id": self.launch_draft_id,
            "launch_fingerprint": self.launch_fingerprint,
            "created_at": self.created_at,
            "intent_hash": self.intent_hash,
            "state": self.state,
            "applied_generation_id": self.applied_generation_id,
        }


@dataclass(frozen=True)
class _Candidate:
    archive_sha256: str
    archive_size_bytes: int
    archive_source_stat: Mapping[str, int]
    central_directory_fingerprint: Mapping[str, Any]
    physical_inventory: tuple[Mapping[str, Any], ...]
    member_inventory_hash: str
    catalog_bytes: bytes
    catalog_template: Mapping[str, Any]
    catalog_key: str
    catalog_schema_version: str
    catalog_source_hash: str
    catalog_core_version: str
    catalog_sha256: str


@dataclass(frozen=True)
class _RevisionIdentity:
    """Cheap pointer/manifest identity used while holding the run lock."""

    revision_id: str
    ordinal: int
    manifest_hash: str
    parent_revision_id: str | None = None
    parent_manifest_hash: str | None = None


@dataclass(frozen=True)
class _RevisionManifestBinding:
    """Hash-bound manifest fields needed for lineage selection.

    The resolver uses this lightweight view while scanning historical applied
    receipts.  Full :meth:`DataRevisionStore.load` validation (ZIP inventory,
    catalog and physical hashes) is intentionally reserved for the selected
    revision so a long receipt history does not become a hot-path replay.
    """

    revision_id: str
    ordinal: int
    manifest_hash: str
    parent_revision_id: str | None
    parent_manifest_hash: str | None
    archive_path: Path
    archive_sha256: str
    archive_size_bytes: int


@dataclass(frozen=True)
class _MaterializedCandidate:
    """Verified immutable candidate staged before authoritative CAS."""

    stage_root: Path
    archive_path: Path
    candidate: _Candidate


def _catalog_payload(raw: bytes, *, label: str = "catalog") -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataRevisionError(f"{label} is unreadable") from exc
    if not isinstance(value, Mapping):
        raise DataRevisionError(f"{label} must be an object")
    if raw not in {_canonical_bytes(value), _workbench_catalog_bytes(value)}:
        raise DataRevisionError(f"{label} bytes are not canonical")
    if set(value) != _CATALOG_FIELDS:
        raise DataRevisionError(f"{label} fields are invalid")
    return value


class DataRevisionStore:
    """Append-only immutable data revisions for one :class:`RunContext`."""

    def __init__(self, context: RunContext):
        if not isinstance(context, RunContext):
            raise TypeError("DataRevisionStore requires a RunContext")
        self.context = context

    @property
    def root(self) -> Path:
        # Keep this lexical path (rather than RunContext's resolved return) so
        # symlinked components remain observable and are rejected explicitly.
        return self.context.run_root / DATA_ROOM_ROOT

    @property
    def revisions_root(self) -> Path:
        return self.context.run_root / REVISION_ROOT

    @property
    def pointer_path(self) -> Path:
        return self.context.run_root / CURRENT_POINTER_PATH

    @contextmanager
    def _locked(self):
        with RunLifecycle._run_lock(self.context):
            self._ensure_roots()
            yield

    def _ensure_roots(self) -> None:
        _ensure_directory(self.root, root=self.context.run_root, label="data-room root")
        _ensure_directory(self.revisions_root, root=self.context.run_root, label="data-room revisions")

    def _revision_directory(self, revision_id: str) -> Path:
        _parse_revision_id(revision_id)
        path = self.revisions_root / revision_id
        return _assert_path(path, root=self.context.run_root, label="data revision directory")

    def _manifest_path(self, revision_id: str) -> Path:
        return self._revision_directory(revision_id) / REVISION_MANIFEST_FILENAME

    def _canonical_manifest_path(self, revision_id: str) -> str:
        return str(REVISION_ROOT / revision_id / REVISION_MANIFEST_FILENAME)

    def _canonical_catalog_path(self, revision_id: str) -> str:
        return str(REVISION_ROOT / revision_id / REVISION_CATALOG_FILENAME)

    def _canonical_archive_path(self, revision_id: str) -> str:
        return str(REVISION_ROOT / revision_id / REVISION_ARCHIVE_FILENAME)

    def _read_json_file(self, path: Path, *, label: str) -> tuple[Mapping[str, Any], bytes]:
        _assert_path(path, root=self.context.run_root, label=label, regular=True)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise DataRevisionError(f"{label} is unreadable") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataRevisionError(f"{label} is unreadable") from exc
        if not isinstance(value, Mapping):
            raise DataRevisionError(f"{label} must be an object")
        if raw != _canonical_bytes(value):
            raise DataRevisionError(f"{label} bytes are not canonical")
        return value, raw

    def _read_pointer(self) -> tuple[str, str] | None:
        if not self.pointer_path.exists() and not self.pointer_path.is_symlink():
            return None
        value, _raw = self._read_json_file(self.pointer_path, label="current revision pointer")
        if set(value) != _POINTER_FIELDS:
            raise DataRevisionError("current revision pointer fields are invalid")
        if value.get("schema_version") != DATA_REVISION_POINTER_SCHEMA_VERSION:
            raise DataRevisionError("current revision pointer schema is unsupported")
        if value.get("run_id") != self.context.run_id:
            raise DataRevisionError("current revision pointer run identity does not match")
        revision_id, ordinal = _parse_revision_id(value.get("revision_id"))
        if value.get("ordinal") != ordinal or isinstance(value.get("ordinal"), bool):
            raise DataRevisionError("current revision pointer ordinal is invalid")
        manifest_hash = _hash_value(value.get("manifest_hash"), label="current revision manifest_hash")
        expected_path = self._canonical_manifest_path(revision_id)
        if value.get("manifest_path") != expected_path:
            raise DataRevisionError("current revision pointer manifest path is not canonical")
        unsigned = dict(value)
        pointer_hash = _hash_value(unsigned.pop("pointer_hash", None), label="current revision pointer hash")
        if _hash_json(unsigned) != pointer_hash:
            raise DataRevisionError("current revision pointer hash does not match")
        return revision_id, manifest_hash

    def _pointer_bytes(self, revision: DataRevision) -> bytes:
        unsigned = {
            "schema_version": DATA_REVISION_POINTER_SCHEMA_VERSION,
            "run_id": self.context.run_id,
            "revision_id": revision.revision_id,
            "ordinal": revision.ordinal,
            "manifest_path": self._canonical_manifest_path(revision.revision_id),
            "manifest_hash": revision.manifest_hash,
        }
        return _canonical_bytes({**unsigned, "pointer_hash": _hash_json(unsigned)})

    def _before_pointer_swap(self, revision: DataRevision) -> None:
        """Testing hook immediately before replacing ``current_revision.json``."""

        self._failpoint("before_pointer_swap")

    def _failpoint(self, name: str) -> None:
        """No-op failpoint hook; tests may replace it without changing APIs."""

        del name

    def _publish_pointer(self, revision: DataRevision) -> None:
        self._before_pointer_swap(revision)
        _atomic_replace(self.pointer_path, self._pointer_bytes(revision), root=self.context.run_root, label="current revision pointer")

    def _validate_revision_directory_contents(self, revision_id: str, *, archive_alias: bool) -> None:
        directory = self._revision_directory(revision_id)
        _assert_path(directory, root=self.context.run_root, label="data revision directory", directory=True)
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            raise DataRevisionError("data revision directory is unreadable") from exc
        expected = {REVISION_MANIFEST_FILENAME, REVISION_CATALOG_FILENAME}
        if not archive_alias:
            expected.add(REVISION_ARCHIVE_FILENAME)
        if {entry.name for entry in entries} != expected:
            raise DataRevisionError("data revision directory contents are invalid")
        for entry in entries:
            _assert_path(entry, root=self.context.run_root, label="data revision artifact", regular=True)

    def _validate_catalog_for_room(
        self,
        room: DataRoom,
        raw: bytes,
        *,
        expected_key: str,
        expected_source_hash: str,
        expected_core_version: str,
        label: str,
    ) -> Mapping[str, Any]:
        value = _catalog_payload(raw, label=label)
        if value.get("catalog_schema_version") != room.catalog_schema_version:
            raise DataRevisionError("catalog schema does not match DataRoom")
        if value.get("source_hash") != expected_source_hash or value.get("source_hash") != room.archive_ref.content_hash:
            raise DataRevisionError("catalog source hash does not match archive")
        if value.get("core_version") != expected_core_version:
            raise DataRevisionError("catalog core version does not match manifest")
        expected_catalog_key = _catalog_identity_key(
            str(value.get("source_hash", "")),
            str(value.get("core_version", "")),
        )
        if value.get("catalog_key") != expected_key or value.get("catalog_key") != expected_catalog_key:
            raise DataRevisionError("catalog key does not match manifest")
        archive = value.get("archive")
        if not isinstance(archive, Mapping):
            raise DataRevisionError("catalog archive identity is invalid")
        try:
            archive_ref = DataAssetRef.from_dict(archive)
        except (TypeError, ValueError, KeyError) as exc:
            raise DataRevisionError("catalog archive identity is invalid") from exc
        if archive_ref.content_hash != room.archive_ref.content_hash or archive_ref.size_bytes != room.archive_ref.size_bytes:
            raise DataRevisionError("catalog archive identity does not match archive")
        entries_raw = value.get("entries")
        if not isinstance(entries_raw, list):
            raise DataRevisionError("catalog entries are invalid")
        try:
            entries = tuple(DataRoomCatalogEntry.from_dict(item) for item in entries_raw)
        except (TypeError, KeyError, ValueError) as exc:
            raise DataRevisionError("catalog entries are invalid") from exc
        if any(entry.sample_values or entry.sample_rows for entry in entries):
            raise DataRevisionError("catalog contains derived sample data")
        for entry in entries:
            room._resolve_member(entry)
        counts = value.get("counts")
        if not isinstance(counts, Mapping) or dict(counts) != room.catalog_counts(entries).to_dict():
            raise DataRevisionError("catalog counts do not match physical inventory")
        return value

    def _normalise_catalog(self, room: DataRoom, raw: bytes, *, final_archive_path: Path) -> tuple[bytes, Mapping[str, Any]]:
        """Make DataRoom's canonical catalog deterministic for a revision path."""

        value = dict(_catalog_payload(raw, label="canonical DataRoom catalog"))
        archive = dict(value["archive"])
        archive["uri"] = str(final_archive_path)
        metadata = dict(archive.get("metadata", {}))
        # Source stat contains an inode/mtime for the staging file.  It is
        # intentionally carried by the revision manifest instead of changing
        # canonical catalog identity on every exact retry.
        metadata.pop("source_stat", None)
        archive["metadata"] = metadata
        value["archive"] = archive
        normalised = _workbench_catalog_bytes(value)
        self._validate_catalog_for_room(
            room,
            normalised,
            expected_key=room.catalog_key,
            expected_source_hash=room.archive_ref.content_hash or "",
            expected_core_version=self.context.core_version,
            label="normalised DataRoom catalog",
        )
        return normalised, value

    def _candidate_from_archive(self, archive_path: Path, *, final_archive_path: Path) -> _Candidate:
        room = DataRoom.open(self.context, archive_path)
        archive_hash = room.verify_source_full()
        archive_size = archive_path.stat().st_size
        members = room.members()
        inventory = tuple(member.to_dict() for member in members)
        inventory_hash = _inventory_hash(inventory)
        _assert_path(room.catalog_path.parent, root=self.context.run_root, label="DataRoom catalog directory")
        if room.catalog_path.is_symlink():
            raise AllowedRootError(f"DataRoom catalog cannot be a symlink: {room.catalog_path}")
        catalog_entries = room.build_catalog()
        del catalog_entries
        _assert_path(room.catalog_path, root=self.context.run_root, label="DataRoom canonical catalog", regular=True)
        raw_catalog = room.catalog_path.read_bytes()
        catalog_bytes, catalog_template = self._normalise_catalog(room, raw_catalog, final_archive_path=final_archive_path)
        return _Candidate(
            archive_sha256=archive_hash,
            archive_size_bytes=int(archive_size),
            archive_source_stat=_stat_signature(archive_path),
            central_directory_fingerprint=dict(room.central_directory_fingerprint),
            physical_inventory=inventory,
            member_inventory_hash=inventory_hash,
            catalog_bytes=catalog_bytes,
            catalog_template=catalog_template,
            catalog_key=room.catalog_key,
            catalog_schema_version=room.catalog_schema_version,
            catalog_source_hash=archive_hash,
            catalog_core_version=self.context.core_version,
            catalog_sha256=_hash_bytes(catalog_bytes),
        )

    def _candidate_for_target(self, candidate: _Candidate, *, final_archive_path: Path) -> _Candidate:
        """Bind a staged catalog template to its deterministic final URI."""

        value = dict(candidate.catalog_template)
        archive = dict(value["archive"])
        archive["uri"] = str(final_archive_path)
        metadata = dict(archive.get("metadata", {}))
        metadata.pop("source_stat", None)
        archive["metadata"] = metadata
        value["archive"] = archive
        catalog_bytes = _workbench_catalog_bytes(value)
        if _catalog_payload(catalog_bytes, label="staged data revision catalog") != value:
            raise DataRevisionError("staged data revision catalog is not canonical")
        return replace(candidate, catalog_bytes=catalog_bytes, catalog_sha256=_hash_bytes(catalog_bytes))

    def _candidate_build_hook(self, stage_archive: Path) -> None:
        """No-op hook used by focused tests to pause candidate construction."""

        del stage_archive

    def _materialize_candidate(self, candidate_archive: str | Path) -> _MaterializedCandidate:
        """Copy/hash/catalog a candidate before taking the lifecycle lock."""

        archive_path = _resolve_archive_candidate(self.context, candidate_archive)
        self._ensure_roots()
        stage_root = self.revisions_root / f".candidate-{uuid.uuid4().hex}"
        _ensure_directory(stage_root, root=self.context.run_root, label="candidate staging directory")
        try:
            stage_archive = stage_root / REVISION_ARCHIVE_FILENAME
            _copy_once(archive_path, stage_archive, root=self.context.run_root, label="candidate archive")
            self._candidate_build_hook(stage_archive)
            candidate = self._candidate_from_archive(stage_archive, final_archive_path=stage_archive)
            return _MaterializedCandidate(stage_root, stage_archive, candidate)
        except Exception:
            shutil.rmtree(stage_root, ignore_errors=True)
            raise

    def _read_revision(
        self,
        revision_id: str,
        *,
        seen: set[str] | None = None,
        record_instrumentation: bool = True,
    ) -> DataRevision:
        """Strictly validate one immutable revision.

        ``record_instrumentation`` is disabled only by read-only Control
        Center projections.  It keeps the byte/hash/path validation identical
        while preventing a status poll from mutating the run's observational
        telemetry file.  Ordinary store callers retain the instrumented
        default.
        """
        revision_id, ordinal = _parse_revision_id(revision_id)
        if seen is None:
            seen = set()
        if revision_id in seen:
            raise DataRevisionError("data revision parent chain contains a cycle")
        seen.add(revision_id)
        directory = self._revision_directory(revision_id)
        manifest_path = directory / REVISION_MANIFEST_FILENAME
        value, _raw_manifest = self._read_json_file(manifest_path, label="data revision manifest")
        if set(value) != _MANIFEST_FIELDS:
            raise DataRevisionError("data revision manifest fields are invalid")
        if value.get("schema_version") != DATA_REVISION_SCHEMA_VERSION:
            raise DataRevisionError("data revision manifest schema is unsupported")
        if value.get("run_id") != self.context.run_id:
            raise DataRevisionError("data revision manifest run identity does not match")
        manifest_revision, manifest_ordinal = _parse_revision_id(value.get("revision_id"))
        if manifest_revision != revision_id or manifest_ordinal != ordinal or value.get("ordinal") != ordinal or isinstance(value.get("ordinal"), bool):
            raise DataRevisionError("data revision manifest ordinal is invalid")
        unsigned = dict(value)
        manifest_hash = _hash_value(unsigned.pop("manifest_hash", None), label="data revision manifest_hash")
        if _hash_bytes(_canonical_bytes(unsigned)) != manifest_hash:
            raise DataRevisionError("data revision manifest hash does not match content")
        expected_manifest_path = self._canonical_manifest_path(revision_id)
        if str(manifest_path.relative_to(self.context.run_root)) != expected_manifest_path:
            raise DataRevisionError("data revision manifest path is not canonical")

        parent_id = value.get("parent_revision_id")
        parent_hash = value.get("parent_manifest_hash")
        if ordinal == 1:
            if parent_id is not None or parent_hash is not None:
                raise DataRevisionError("D-0001 cannot have a parent revision")
        else:
            if parent_id is None or parent_hash is None:
                raise DataRevisionError("later data revision parent identity is missing")
            parent_id, parent_ordinal = _parse_revision_id(parent_id, label="parent_revision_id")
            if parent_ordinal != ordinal - 1:
                raise DataRevisionError("data revision parent ordinal is not contiguous")
            parent_hash = _hash_value(parent_hash, label="parent_manifest_hash")
            parent = self._read_revision(
                parent_id,
                seen=seen,
                record_instrumentation=record_instrumentation,
            )
            if parent.manifest_hash != parent_hash:
                raise DataRevisionError("data revision parent manifest hash does not match")

        archive_alias = value.get("archive_alias")
        if not isinstance(archive_alias, bool):
            raise DataRevisionError("data revision archive_alias is invalid")
        if ordinal == 1 and not archive_alias:
            raise DataRevisionError("D-0001 must reference a legacy archive alias")
        if ordinal > 1 and archive_alias:
            raise DataRevisionError("later data revisions cannot alias the legacy archive")
        archive_path = _resolve_manifest_path(self.context, value.get("archive_path"), label="data revision archive")
        if archive_alias:
            if value.get("archive_path") == self._canonical_archive_path(revision_id):
                raise DataRevisionError("legacy archive alias cannot point at revision archive")
        elif value.get("archive_path") != self._canonical_archive_path(revision_id):
            raise DataRevisionError("data revision archive path is not canonical")
        _assert_path(archive_path, root=archive_path.parent, label="data revision archive", regular=True)
        archive_hash = _hash_value(value.get("archive_sha256"), label="archive_sha256")
        archive_size = value.get("archive_size_bytes")
        if isinstance(archive_size, bool) or not isinstance(archive_size, int) or archive_size < 0:
            raise DataRevisionError("archive_size_bytes is invalid")
        if archive_path.stat().st_size != archive_size or _hash_file(archive_path) != archive_hash:
            raise DataRevisionError("data revision archive hash or size does not match manifest")
        source_stat = value.get("archive_source_stat")
        if not isinstance(source_stat, Mapping) or {str(key) for key in source_stat} != {"device", "inode", "size_bytes", "mtime_ns"}:
            raise DataRevisionError("archive source stat is invalid")
        source_stat = {str(key): source_stat[key] for key in source_stat}
        if _stat_signature(archive_path) != {key: int(val) for key, val in source_stat.items()}:
            raise DataRevisionError("archive source stat does not match manifest")
        central = value.get("central_directory_fingerprint")
        if not isinstance(central, Mapping):
            raise DataRevisionError("central-directory fingerprint is invalid")
        central = dict(central)

        catalog_path_value = value.get("catalog_path")
        if catalog_path_value != self._canonical_catalog_path(revision_id):
            raise DataRevisionError("data revision catalog path is not canonical")
        catalog_path = self.context.run_root / Path(catalog_path_value)
        _assert_path(catalog_path, root=self.context.run_root, label="data revision catalog", regular=True)
        raw_catalog = catalog_path.read_bytes()
        catalog_hash = _hash_value(value.get("catalog_sha256"), label="catalog_sha256")
        if _hash_bytes(raw_catalog) != catalog_hash:
            raise DataRevisionError("data revision catalog hash does not match manifest")
        catalog_value = _catalog_payload(raw_catalog, label="data revision catalog")
        catalog_key = catalog_value.get("catalog_key")
        if not isinstance(catalog_key, str) or value.get("catalog_key") != catalog_key:
            raise DataRevisionError("catalog key does not match manifest")
        catalog_schema = catalog_value.get("catalog_schema_version")
        if value.get("catalog_schema_version") != catalog_schema:
            raise DataRevisionError("catalog schema does not match manifest")
        source_hash = _hash_value(value.get("catalog_source_hash"), label="catalog_source_hash")
        if catalog_value.get("source_hash") != source_hash or source_hash != archive_hash:
            raise DataRevisionError("catalog source hash does not match archive")
        core_version = value.get("catalog_core_version")
        if not isinstance(core_version, str) or not core_version or catalog_value.get("core_version") != core_version:
            raise DataRevisionError("catalog core version does not match manifest")
        inventory = value.get("physical_inventory")
        if not isinstance(inventory, list):
            raise DataRevisionError("physical inventory is invalid")
        try:
            members = tuple(DataRoomMember.from_dict(item) for item in inventory)
        except (TypeError, KeyError, ValueError) as exc:
            raise DataRevisionError("physical inventory is invalid") from exc
        inventory_hash = _hash_value(value.get("member_inventory_hash"), label="member_inventory_hash")
        if _inventory_hash(inventory) != inventory_hash:
            raise DataRevisionError("member inventory hash does not match")
        room = DataRoom.open(
            self.context,
            archive_path,
            bound_members=members,
            bound_archive_hash=archive_hash,
            bound_source_stat=source_stat,
            bound_central_directory_fingerprint=central,
            record_instrumentation=record_instrumentation,
        )
        if tuple(member.to_dict() for member in room.members()) != tuple(inventory):
            raise DataRevisionError("physical inventory does not match archive")
        room.verify_source_full(record_instrumentation=record_instrumentation)
        self._validate_catalog_for_room(
            room,
            raw_catalog,
            expected_key=catalog_key,
            expected_source_hash=source_hash,
            expected_core_version=core_version,
            label="data revision catalog",
        )
        self._validate_revision_directory_contents(revision_id, archive_alias=archive_alias)
        return DataRevision(
            run_id=self.context.run_id,
            revision_id=revision_id,
            ordinal=ordinal,
            parent_revision_id=parent_id,
            parent_manifest_hash=parent_hash,
            archive_path=archive_path,
            archive_sha256=archive_hash,
            archive_size_bytes=archive_size,
            archive_alias=archive_alias,
            archive_source_stat=source_stat,
            central_directory_fingerprint=central,
            catalog_path=catalog_path,
            catalog_key=catalog_key,
            catalog_sha256=catalog_hash,
            catalog_schema_version=catalog_schema,
            catalog_source_hash=source_hash,
            catalog_core_version=core_version,
            physical_inventory=tuple(inventory),
            member_inventory_hash=inventory_hash,
            manifest_path=manifest_path,
            manifest_hash=manifest_hash,
        )

    def load(self, revision_id: str) -> DataRevision:
        """Strictly load and validate one immutable revision."""

        self._ensure_roots()
        return self._read_revision(revision_id)

    def current(self) -> DataRevision | None:
        """Return the pointer-authoritative latest revision, or ``None``."""

        self._ensure_roots()
        return self._current_unlocked()

    def active_generation_revision(
        self,
        generation_id: str | None = None,
        *,
        generation_metadata: Any | None = None,
    ) -> DataRevision | None:
        """Resolve the immutable data revision bound to an active generation.

        This resolver deliberately does not consult ``current()``: a newly
        uploaded but not-yet-admitted D revision must never become the active
        analytical binding.  A generation's direct manifest reference wins;
        otherwise the latest validated applied refresh receipt at or before
        that generation supplies the lineage, with immutable D-0001 as the
        final legacy fallback.
        """

        self._ensure_roots()

        def field(value: Any, name: str) -> Any:
            if isinstance(value, Mapping):
                return value.get(name)
            return getattr(value, name, None)

        def canonical_generation(value: Any, *, label: str) -> tuple[str, int]:
            if not isinstance(value, str) or _GENERATION_ID_RE.fullmatch(value) is None:
                raise DataRevisionError(f"{label} is invalid")
            ordinal = int(value[2:])
            if f"G-{ordinal:04d}" != value:
                raise DataRevisionError(f"{label} is not canonical")
            return value, ordinal

        # The resolver is for the active lifecycle generation.  Loading the
        # lifecycle is metadata-only and never reads the mutable D pointer.
        lifecycle = RunLifecycle.load(self.context)
        active_metadata = lifecycle.generation_metadata
        active_id = active_metadata.generation_id if active_metadata is not None else lifecycle.generation_id
        active_id, active_ordinal = canonical_generation(active_id, label="active generation_id")
        if generation_metadata is not None:
            supplied_id, supplied_ordinal = canonical_generation(
                field(generation_metadata, "generation_id"),
                label="generation_metadata.generation_id",
            )
            if supplied_id != active_id:
                raise DataRevisionError("generation metadata is not the active generation")
            if generation_id is not None and generation_id != supplied_id:
                raise DataRevisionError("generation_id does not match generation metadata")
            target_id, target_ordinal = supplied_id, supplied_ordinal
            metadata = generation_metadata
        else:
            if generation_id is not None:
                target_id, target_ordinal = canonical_generation(generation_id, label="generation_id")
                if target_id != active_id:
                    raise DataRevisionError("generation_id is not the active generation")
            else:
                target_id, target_ordinal = active_id, active_ordinal
            metadata = active_metadata

        direct_ref = field(metadata, "data_revision_ref") if metadata is not None else None
        direct_hash = field(metadata, "data_revision_hash") if metadata is not None else None
        if direct_ref is not None:
            if (
                not isinstance(direct_ref, str)
                or direct_ref != Path(direct_ref).as_posix()
                or Path(direct_ref).is_absolute()
            ):
                raise DataRevisionError("active generation data revision reference is invalid")
            parts = Path(direct_ref).parts
            if (
                len(parts) != 4
                or parts[:2] != ("data_room", "revisions")
                or parts[3] != REVISION_MANIFEST_FILENAME
            ):
                raise DataRevisionError("active generation data revision reference is invalid")
            revision_id, _ = _parse_revision_id(parts[2], label="active generation data revision")
            manifest_path = self._manifest_path(revision_id)
            if direct_ref != self._canonical_manifest_path(revision_id) or manifest_path.is_symlink():
                raise DataRevisionError("active generation data revision reference is not canonical")
            direct_hash = _hash_value(direct_hash, label="active generation data revision hash")
            revision = self.load(revision_id)
            if revision.manifest_hash != direct_hash:
                raise DataRevisionError("active generation data revision hash does not match manifest")
            return revision
        if direct_hash is not None:
            raise DataRevisionError("active generation data revision hash requires a reference")

        # No direct binding: inspect only canonical applied receipts.  Audit
        # names outside the exact intent-hash filename shape are unrelated and
        # deliberately ignored; matching names are strict and fail closed.
        applied_root = self.pending_data_refresh_archive_root
        receipts: list[tuple[int, PendingDataRefresh, _RevisionManifestBinding]] = []
        by_generation: dict[str, tuple[str, str]] = {}
        if applied_root.exists() or applied_root.is_symlink():
            if applied_root.is_symlink() or not applied_root.is_dir():
                raise DataRevisionError("applied data refresh receipt root is not a regular directory")
            for path in sorted(applied_root.iterdir(), key=lambda value: value.name):
                match = re.fullmatch(r"([0-9a-f]{64})\.json", path.name)
                if match is None:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise DataRevisionError("applied data refresh receipt is not a regular file")
                receipt = self._read_pending_file(path, applied=True)
                if receipt.intent_hash != match.group(1):
                    raise DataRevisionError("applied data refresh receipt filename is not hash-bound")
                applied_id, applied_ordinal = canonical_generation(
                    receipt.applied_generation_id,
                    label="applied data refresh generation_id",
                )
                binding = self._read_revision_manifest_binding(receipt.data_revision_id)
                if (
                    binding.manifest_hash != receipt.data_revision_manifest_hash
                    or binding.archive_sha256 != receipt.data_revision_archive_sha256
                ):
                    raise DataRevisionError("applied data refresh receipt data revision hash is stale")
                manifest_ref = self._canonical_manifest_path(binding.revision_id)
                archive_ref: str | None = None
                try:
                    archive_ref = binding.archive_path.relative_to(self.context.run_root).as_posix()
                except ValueError:
                    pass
                if receipt.data_revision_ref not in {manifest_ref, archive_ref}:
                    raise DataRevisionError("applied data refresh receipt data revision reference is stale")
                identity = (binding.revision_id, binding.manifest_hash)
                prior = by_generation.get(applied_id)
                if prior is not None and prior != identity:
                    raise DataRevisionError("applied data refresh receipts conflict for one generation")
                by_generation[applied_id] = identity
                receipts.append((applied_ordinal, receipt, binding))

        eligible = [value for value in receipts if value[0] <= target_ordinal]
        if eligible:
            _ordinal, _receipt, binding = max(eligible, key=lambda value: value[0])
            # Perform full immutable archive/catalog/inventory validation only
            # for the selected lineage winner.
            return self.load(binding.revision_id)

        # D-0001 is immutable and may be an alias to the legacy input archive;
        # never synthesize it here and never fall back to the mutable pointer.
        first_directory = self._revision_directory("D-0001")
        if not first_directory.exists() and not first_directory.is_symlink():
            return None
        return self.load("D-0001")

    @property
    def pending_data_refresh_path(self) -> Path:
        return self.context.run_root / PENDING_DATA_REFRESH_PATH

    @property
    def pending_data_refresh_archive_root(self) -> Path:
        return self.context.run_root / PENDING_DATA_REFRESH_ARCHIVE_ROOT

    @property
    def revision_transaction_path(self) -> Path:
        return self.context.run_root / REVISION_TRANSACTION_PATH

    def _transaction_from_value(self, value: Mapping[str, Any]) -> DataRevisionTransaction:
        if set(value) != _TRANSACTION_FIELDS:
            raise DataRevisionError("data revision transaction fields are invalid")
        if value.get("schema_version") != REVISION_TRANSACTION_SCHEMA_VERSION or value.get("kind") != "data_revision_transaction":
            raise DataRevisionError("data revision transaction schema is unsupported")
        if value.get("run_id") != self.context.run_id:
            raise DataRevisionError("data revision transaction run identity does not match")
        transaction_hash = _hash_value(value.get("transaction_hash"), label="transaction_hash")
        unsigned = dict(value)
        unsigned.pop("transaction_hash", None)
        if _hash_json(unsigned) != transaction_hash:
            raise DataRevisionError("data revision transaction hash does not match")
        return DataRevisionTransaction(
            run_id=self.context.run_id,
            revision_id=str(value.get("revision_id")),
            revision_manifest_hash=str(value.get("revision_manifest_hash")),
            revision_archive_sha256=str(value.get("revision_archive_sha256")),
            parent_revision_id=value.get("parent_revision_id"),
            parent_manifest_hash=value.get("parent_manifest_hash"),
            launch_draft_id=str(value.get("launch_draft_id")),
            launch_fingerprint=str(value.get("launch_fingerprint")),
            created_at=str(value.get("created_at")),
            phase=str(value.get("phase")),
            data_revision=value.get("data_revision"),
            data_revision_ref=value.get("data_revision_ref"),
            plan=value.get("plan"),
            reopened_item_ids=tuple(value.get("reopened_item_ids") or ()),
            expected_parent_generation_id=value.get("expected_parent_generation_id"),
            expected_parent_state_hash=value.get("expected_parent_state_hash"),
            expected_parent_plan_hash=value.get("expected_parent_plan_hash"),
            transaction_hash=transaction_hash,
        )

    def _read_revision_transaction(self) -> DataRevisionTransaction | None:
        path = self.revision_transaction_path
        if not path.exists() and not path.is_symlink():
            return None
        value, _raw = self._read_json_file(path, label="data revision transaction")
        return self._transaction_from_value(value)

    def revision_transaction(self) -> DataRevisionTransaction | None:
        """Return the strict run-local append/binding journal, if present."""

        self._ensure_roots()
        return self._read_revision_transaction()

    def recover_revision_transaction(self) -> PendingDataRefresh | None:
        """Materialize a complete journal handoff into canonical pending.

        This is the launch/coordinator recovery entrypoint.  It is idempotent;
        incomplete low-level metadata-only transactions remain untouched.
        """

        with self._locked():
            transaction = self._read_revision_transaction()
            if transaction is not None and transaction.has_handoff:
                return self._recover_revision_transaction_unlocked(transaction)
            path = self.pending_data_refresh_path
            if path.exists() or path.is_symlink():
                return self._read_pending_file(path)
            return None

    @staticmethod
    def _transaction_metadata(value: Mapping[str, Any]) -> tuple[str, str, str]:
        try:
            draft_id = str(value["launch_draft_id"])
            fingerprint = str(value["launch_fingerprint"])
            created_at = str(value["created_at"])
        except (KeyError, TypeError) as exc:
            raise DataRevisionError("revision transaction metadata is incomplete") from exc
        return draft_id, fingerprint, created_at

    def _transaction_for_revision(
        self,
        revision: DataRevision,
        *,
        parent_revision_id: str | None,
        parent_manifest_hash: str | None,
        metadata: Mapping[str, Any],
    ) -> DataRevisionTransaction:
        draft_id, fingerprint, created_at = self._transaction_metadata(metadata)
        plan = metadata.get("plan")
        complete_handoff = isinstance(plan, Mapping)
        if complete_handoff:
            data_revision = revision.to_dict()
            data_revision_ref = str(metadata.get("data_revision_ref") or self._canonical_manifest_path(revision.revision_id))
            reopened_item_ids = tuple(metadata.get("reopened_item_ids") or ())
            expected_parent_generation_id = metadata.get("expected_parent_generation_id")
            expected_parent_state_hash = metadata.get("expected_parent_state_hash")
            expected_parent_plan_hash = metadata.get("expected_parent_plan_hash")
        else:
            # Metadata-only transactions remain useful for low-level callers
            # that are not publishing a refresh handoff.  Launch publication
            # always supplies the complete fields above.
            data_revision = None
            data_revision_ref = None
            reopened_item_ids = ()
            expected_parent_generation_id = None
            expected_parent_state_hash = None
            expected_parent_plan_hash = None
        return DataRevisionTransaction(
            run_id=self.context.run_id,
            revision_id=revision.revision_id,
            revision_manifest_hash=revision.manifest_hash,
            revision_archive_sha256=revision.archive_sha256,
            parent_revision_id=parent_revision_id,
            parent_manifest_hash=parent_manifest_hash,
            launch_draft_id=draft_id,
            launch_fingerprint=fingerprint,
            created_at=created_at,
            data_revision=data_revision,
            data_revision_ref=data_revision_ref,
            plan=plan,
            reopened_item_ids=reopened_item_ids,
            expected_parent_generation_id=expected_parent_generation_id,
            expected_parent_state_hash=expected_parent_state_hash,
            expected_parent_plan_hash=expected_parent_plan_hash,
        )

    def _recover_revision_transaction_unlocked(
        self,
        transaction: DataRevisionTransaction,
    ) -> PendingDataRefresh | None:
        """Recover one complete append handoff into canonical pending bytes.

        The caller holds ``RunLifecycle._run_lock``.  This deliberately uses
        the same pending schema/coalescing rules as normal admission, but does
        not clear or replace the journal; the caller decides when its exact
        owner has durably taken over.
        """

        if not transaction.has_handoff:
            return None
        current = self._current_unlocked()
        if (
            current is None
            or current.revision_id != transaction.revision_id
            or current.manifest_hash != transaction.revision_manifest_hash
            or self._read_revision_identity(transaction.revision_id).manifest_hash != transaction.revision_manifest_hash
        ):
            raise RevisionCASMismatch("revision transaction handoff is not current")
        revision_value = transaction.data_revision
        if not isinstance(revision_value, Mapping):
            raise DataRevisionError("revision transaction data revision is invalid")
        if (
            revision_value.get("revision_id") != transaction.revision_id
            or revision_value.get("manifest_hash") != transaction.revision_manifest_hash
            or revision_value.get("archive_sha256") != transaction.revision_archive_sha256
        ):
            raise DataRevisionError("revision transaction data revision hash is stale")
        revision = self.load(transaction.revision_id)
        if _jsonable(revision.to_dict()) != _jsonable(revision_value):
            raise DataRevisionError("revision transaction data revision payload is not authoritative")
        plan_value = self._pending_plan(transaction.plan)
        reopened = self._pending_ids(transaction.reopened_item_ids)
        expected_generation = transaction.expected_parent_generation_id
        expected_state_hash = transaction.expected_parent_state_hash
        expected_plan_hash = transaction.expected_parent_plan_hash
        if expected_generation is None or expected_state_hash is None or expected_plan_hash is None:
            raise DataRevisionError("revision transaction parent CAS is incomplete")
        _parse_generation_id(expected_generation, label="revision transaction expected parent generation")
        _hash_value(expected_state_hash, label="revision transaction expected parent state hash")
        _hash_value(expected_plan_hash, label="revision transaction expected parent plan hash")
        ref = transaction.data_revision_ref
        if not isinstance(ref, str) or not ref or Path(ref).is_absolute() or "\x00" in ref:
            raise DataRevisionError("revision transaction data revision reference is invalid")
        _assert_path(self.context.run_root / ref, root=self.context.run_root, label="revision transaction data revision reference")
        body: dict[str, Any] = {
            "schema_version": PENDING_DATA_REFRESH_SCHEMA_VERSION,
            "kind": "data_refresh_admission",
            "run_id": self.context.run_id,
            "data_revision": _jsonable(revision_value),
            "data_revision_ref": ref,
            "data_revision_id": transaction.revision_id,
            "data_revision_manifest_hash": transaction.revision_manifest_hash,
            "data_revision_archive_sha256": transaction.revision_archive_sha256,
            "plan": plan_value,
            "plan_hash": _hash_json(plan_value),
            "reopened_item_ids": list(reopened),
            "expected_parent_generation_id": expected_generation,
            "expected_parent_state_hash": expected_state_hash,
            "expected_parent_plan_hash": expected_plan_hash,
            "original_parent_generation_id": expected_generation,
            "original_parent_state_hash": expected_state_hash,
            "original_parent_plan_hash": expected_plan_hash,
            "launch_draft_id": transaction.launch_draft_id,
            "launch_fingerprint": transaction.launch_fingerprint,
            "created_at": transaction.created_at,
            "state": "pending",
            "applied_generation_id": None,
        }
        pending_path = self.pending_data_refresh_path
        existing = None
        if pending_path.exists() or pending_path.is_symlink():
            existing = self._read_pending_file(pending_path)
            if (
                existing.expected_parent_generation_id != expected_generation
                or existing.expected_parent_state_hash != expected_state_hash
                or existing.expected_parent_plan_hash != expected_plan_hash
            ):
                raise PendingDataRefreshConflict("pending refresh parent CAS differs from revision transaction handoff")
            merged_plan = self._coalesce_plans(existing.plan, plan_value)
            merged_reopened = tuple(dict.fromkeys((*existing.reopened_item_ids, *reopened)))
            body.update(
                {
                    "plan": merged_plan,
                    "plan_hash": _hash_json(merged_plan),
                    "reopened_item_ids": list(merged_reopened),
                    "original_parent_generation_id": existing.original_parent_generation_id,
                    "original_parent_state_hash": existing.original_parent_state_hash,
                    "original_parent_plan_hash": existing.original_parent_plan_hash,
                }
            )
        body["intent_hash"] = _hash_json(body)
        proposed = self._pending_from_value(body)
        if existing is not None and existing.intent_hash == proposed.intent_hash:
            return existing
        _atomic_replace(
            pending_path,
            _canonical_bytes(proposed.to_dict()),
            root=self.context.run_root,
            label="pending data refresh",
        )
        return proposed

    def _publish_revision_transaction(self, transaction: DataRevisionTransaction) -> None:
        existing = self._read_revision_transaction()
        if existing is not None and existing.transaction_hash != transaction.transaction_hash:
            if existing.has_handoff:
                self._recover_revision_transaction_unlocked(existing)
            if existing.revision_id == transaction.revision_id:
                # A same-D retry may legitimately take over after the prior
                # handoff has been recovered into canonical pending.  The
                # immutable revision bytes remain occupied and exact retries
                # are still checked by ``append`` before this point.
                if not existing.has_handoff:
                    raise RevisionConflictError("conflicting data revision transaction already exists")
        self._failpoint("before_revision_transaction")
        _atomic_replace(
            self.revision_transaction_path,
            _canonical_bytes(transaction.to_dict()),
            root=self.context.run_root,
            label="data revision transaction",
        )

    def _clear_revision_transaction(
        self,
        *,
        revision_id: str,
        revision_manifest_hash: str,
    ) -> None:
        transaction = self._read_revision_transaction()
        if transaction is None:
            return
        if transaction.revision_id != revision_id or transaction.revision_manifest_hash != revision_manifest_hash:
            return
        self._failpoint("before_revision_transaction_clear")
        path = self.revision_transaction_path
        if path.is_symlink() or not path.is_file():
            raise DataRevisionError("data revision transaction is not a regular file")
        path.unlink()
        _fsync_directory(path.parent)

    def _admission_proves_revision_unlocked(self, revision: DataRevision) -> bool:
        """Return whether canonical admission bytes bind ``revision``.

        A launch draft may recover a journal after another draft has already
        written the one canonical pending admission.  That admission is the
        durable proof that the current D is no longer merely an unbound
        pointer, so its original draft ownership need not be reused to clear
        the transaction.  Audit files outside the canonical hash-shaped
        applied-receipt names are deliberately ignored.
        """

        def matches(value: PendingDataRefresh) -> bool:
            return (
                value.data_revision_id == revision.revision_id
                and value.data_revision_manifest_hash == revision.manifest_hash
                and value.data_revision_archive_sha256 == revision.archive_sha256
            )

        pending_path = self.pending_data_refresh_path
        if pending_path.exists() or pending_path.is_symlink():
            if matches(self._read_pending_file(pending_path)):
                return True
        archive_root = self.pending_data_refresh_archive_root
        if archive_root.exists() or archive_root.is_symlink():
            if archive_root.is_symlink() or not archive_root.is_dir():
                raise DataRevisionError("pending refresh applied receipt root is not a regular directory")
            for path in sorted(archive_root.iterdir(), key=lambda value: value.name):
                if re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise DataRevisionError("pending refresh applied receipt is not a regular file")
                if matches(self._read_pending_file(path, applied=True)):
                    return True
        return False

    def begin_revision_transaction(
        self,
        revision: DataRevision,
        *,
        parent_revision_id: str | None,
        parent_manifest_hash: str | None,
        launch_draft_id: str,
        launch_fingerprint: str,
        created_at: str,
    ) -> DataRevisionTransaction:
        """Persist a binding journal for an already-current revision."""

        metadata = {
            "launch_draft_id": launch_draft_id,
            "launch_fingerprint": launch_fingerprint,
            "created_at": created_at,
        }
        with self._locked():
            current = self._current_unlocked()
            if current is None or current.revision_id != revision.revision_id or current.manifest_hash != revision.manifest_hash:
                raise RevisionCASMismatch("revision transaction target is not current")
            transaction = self._transaction_for_revision(
                revision,
                parent_revision_id=parent_revision_id,
                parent_manifest_hash=parent_manifest_hash,
                metadata=metadata,
            )
            existing = self._read_revision_transaction()
            if existing is not None and existing.transaction_hash == transaction.transaction_hash:
                return existing
            self._publish_revision_transaction(transaction)
            return transaction

    def complete_revision_transaction(
        self,
        revision: DataRevision,
        *,
        launch_draft_id: str | None = None,
        launch_fingerprint: str | None = None,
    ) -> None:
        """Clear a binding journal after its successful no-op continuation.

        Clearing an unadmitted revision is ownership-sensitive.  The exact
        launch draft/fingerprint that created the journal may clear it, or a
        canonical pending/applied admission for the same immutable D may prove
        that another draft has already taken ownership of the handoff.
        """

        with self._locked():
            current = self._current_unlocked()
            if current is None or current.revision_id != revision.revision_id or current.manifest_hash != revision.manifest_hash:
                raise RevisionCASMismatch("revision transaction target is not current")
            transaction = self._read_revision_transaction()
            if transaction is None:
                return
            if (
                transaction.revision_id != revision.revision_id
                or transaction.revision_manifest_hash != revision.manifest_hash
                or transaction.revision_archive_sha256 != revision.archive_sha256
            ):
                raise RevisionConflictError("revision transaction targets another immutable revision")
            owner_matches = (
                launch_draft_id is not None
                and launch_fingerprint is not None
                and transaction.launch_draft_id == launch_draft_id
                and transaction.launch_fingerprint == launch_fingerprint
            )
            if not owner_matches and not self._admission_proves_revision_unlocked(revision):
                raise RevisionConflictError("revision transaction ownership does not match")
            self._clear_revision_transaction(
                revision_id=revision.revision_id,
                revision_manifest_hash=revision.manifest_hash,
            )

    @staticmethod
    def _pending_plan(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise DataRevisionError("pending refresh plan must be an object")
        try:
            from .requirement_planning import RequirementExecutionPlan

            plan = RequirementExecutionPlan.from_dict(value)
        except (TypeError, ValueError, KeyError) as exc:
            raise DataRevisionError("pending refresh plan is invalid") from exc
        result = plan.to_dict()
        if _jsonable(result) != _jsonable(value):
            raise DataRevisionError("pending refresh plan is not canonical")
        return result

    @staticmethod
    def _pending_ids(values: Any) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
            raise DataRevisionError("pending reopened_item_ids must be a list")
        result = tuple(str(value) for value in values)
        if len(result) != len(set(result)) or any(
            not value or Path(value).name != value or value in {".", ".."} for value in result
        ):
            raise DataRevisionError("pending reopened_item_ids are invalid")
        return result

    @staticmethod
    def _pending_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(value)
        body.pop("intent_hash", None)
        # ``intent_hash`` is the identity of the admission, not of its applied
        # receipt.  Applied receipts retain the original hash while changing
        # only the state marker and generation binding.
        body["state"] = "pending"
        body["applied_generation_id"] = None
        return body

    def _pending_from_value(self, value: Mapping[str, Any], *, applied: bool = False) -> PendingDataRefresh:
        if set(value) != _PENDING_FIELDS:
            raise DataRevisionError("pending refresh fields are invalid")
        if value.get("schema_version") != PENDING_DATA_REFRESH_SCHEMA_VERSION or value.get("kind") != "data_refresh_admission":
            raise DataRevisionError("pending refresh schema is unsupported")
        if value.get("run_id") != self.context.run_id:
            raise DataRevisionError("pending refresh run identity does not match")
        intent_hash = _hash_value(value.get("intent_hash"), label="pending intent_hash")
        if _hash_json(self._pending_unsigned(value)) != intent_hash:
            raise DataRevisionError("pending refresh intent hash does not match")
        plan = self._pending_plan(value.get("plan"))
        if _hash_json(plan) != value.get("plan_hash"):
            raise DataRevisionError("pending refresh plan hash does not match")
        data_revision = value.get("data_revision")
        if not isinstance(data_revision, Mapping):
            raise DataRevisionError("pending refresh data revision is invalid")
        revision_id, _ = _parse_revision_id(value.get("data_revision_id"), label="pending data_revision_id")
        if data_revision.get("revision_id") != revision_id:
            raise DataRevisionError("pending refresh data revision identity is invalid")
        revision_manifest_hash = _hash_value(value.get("data_revision_manifest_hash"), label="pending data revision manifest hash")
        archive_hash = _hash_value(value.get("data_revision_archive_sha256"), label="pending data revision archive hash")
        if data_revision.get("manifest_hash") != revision_manifest_hash or data_revision.get("archive_sha256") != archive_hash:
            raise DataRevisionError("pending refresh data revision hash binding is invalid")
        ref = value.get("data_revision_ref")
        if not isinstance(ref, str) or not ref or Path(ref).is_absolute() or "\x00" in ref:
            raise DataRevisionError("pending refresh data revision reference is invalid")
        _assert_path(self.context.run_root / ref, root=self.context.run_root, label="pending data revision reference")
        state = value.get("state")
        if state not in {"pending", "applied"} or (applied and state != "applied") or (not applied and state != "pending"):
            raise DataRevisionError("pending refresh state is invalid")
        applied_generation_id = value.get("applied_generation_id")
        if state == "pending" and applied_generation_id is not None:
            raise DataRevisionError("pending refresh applied generation is invalid")
        if state == "applied":
            if not isinstance(applied_generation_id, str) or _GENERATION_ID_RE.fullmatch(applied_generation_id) is None:
                raise DataRevisionError("pending refresh applied generation is invalid")
        return PendingDataRefresh(
            run_id=self.context.run_id,
            data_revision=data_revision,
            data_revision_ref=ref,
            data_revision_id=revision_id,
            data_revision_manifest_hash=revision_manifest_hash,
            data_revision_archive_sha256=archive_hash,
            plan=plan,
            plan_hash=str(value["plan_hash"]),
            reopened_item_ids=self._pending_ids(value.get("reopened_item_ids")),
            expected_parent_generation_id=str(value.get("expected_parent_generation_id")),
            expected_parent_state_hash=str(value.get("expected_parent_state_hash")),
            expected_parent_plan_hash=str(value.get("expected_parent_plan_hash")),
            original_parent_generation_id=str(value.get("original_parent_generation_id")),
            original_parent_state_hash=str(value.get("original_parent_state_hash")),
            original_parent_plan_hash=str(value.get("original_parent_plan_hash")),
            launch_draft_id=str(value.get("launch_draft_id")),
            launch_fingerprint=str(value.get("launch_fingerprint")),
            created_at=str(value.get("created_at")),
            intent_hash=intent_hash,
            state=str(state),
            applied_generation_id=applied_generation_id,
        )

    def _read_pending_file(self, path: Path, *, applied: bool = False) -> PendingDataRefresh:
        value, _raw = self._read_json_file(path, label="pending data refresh")
        return self._pending_from_value(value, applied=applied)

    def _pending_archive_path(self, intent_hash: str) -> Path:
        _hash_value(intent_hash, label="pending intent_hash")
        return self.pending_data_refresh_archive_root / f"{intent_hash}.json"

    def _read_applied(self, intent_hash: str) -> PendingDataRefresh | None:
        path = self._pending_archive_path(intent_hash)
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            raise DataRevisionError("pending refresh applied receipt is not a regular file")
        return self._read_pending_file(path, applied=True)

    @staticmethod
    def _coalesce_plans(old_value: Mapping[str, Any], new_value: Mapping[str, Any]) -> dict[str, Any]:
        """Merge two candidate plans without guessing business semantics."""

        from .requirement_planning import RequirementExecutionGroup, RequirementExecutionPlan

        old_plan = RequirementExecutionPlan.from_dict(old_value)
        new_plan = RequirementExecutionPlan.from_dict(new_value)
        old_records = {record.requirement_id: record for record in old_plan.input_records}
        new_records = {record.requirement_id: record for record in new_plan.input_records}
        for item_id in set(old_records) & set(new_records):
            # Plans read from an immutable pending receipt are frozen with
            # tuples/mapping proxies, while a transaction handoff is parsed
            # from canonical JSON lists/dicts.  Compare their canonical
            # record payloads rather than Python container identity so an
            # exact recovery is truly idempotent across either form.
            if old_records[item_id].to_dict() != new_records[item_id].to_dict():
                raise PendingDataRefreshConflict(f"pending requirement record conflicts: {item_id}")
        records: list[Any] = list(old_plan.input_records)
        records.extend(record for record in new_plan.input_records if record.requirement_id not in old_records)
        present = {
            requirement_id
            for group in new_plan.groups
            for requirement_id in group.requirement_ids
        }
        groups = list(new_plan.groups)
        for group in old_plan.groups:
            missing = tuple(item_id for item_id in group.requirement_ids if item_id not in present)
            if missing:
                groups.append(
                    RequirementExecutionGroup(
                        requirement_ids=missing,
                        rationale=group.rationale,
                        shared_analysis_intent=group.shared_analysis_intent,
                        suggested_specialists=group.suggested_specialists,
                    )
                )
                present.update(missing)
        merged = RequirementExecutionPlan(
            input_records=tuple(records),
            groups=tuple(groups),
            planner_ref=new_plan.planner_ref,
            portfolio_strategy=new_plan.portfolio_strategy,
            revision=max(old_plan.revision, new_plan.revision),
        )
        return merged.to_dict()

    def pending_data_refresh(self, *, allow_stale: bool = False) -> PendingDataRefresh | None:
        """Return the canonical pending admission.

        Normal callers receive a strict current-pointer CAS check.  The
        coordinator may opt into ``allow_stale`` while recovering a crash in
        the append/admit handoff: it must be able to inspect the old immutable
        admission and project a categorical recovery state until the exact
        successor admission coalesces onto the new current D.
        """

        self._ensure_roots()
        path = self.pending_data_refresh_path
        if not path.exists() and not path.is_symlink():
            return None
        pending = self._read_pending_file(path)
        current = self._current_unlocked()
        if current is None or current.revision_id != pending.data_revision_id or current.manifest_hash != pending.data_revision_manifest_hash or current.archive_sha256 != pending.data_revision_archive_sha256:
            if allow_stale:
                return pending
            raise RevisionCASMismatch("pending data refresh is not bound to the current data revision")
        return pending

    def admit_pending_data_refresh(
        self,
        *,
        data_revision: DataRevision | Mapping[str, Any],
        plan: Mapping[str, Any],
        reopened_item_ids: Any,
        expected_parent_generation_id: str,
        expected_parent_state_hash: str,
        expected_parent_plan_hash: str,
        launch_draft_id: str,
        launch_fingerprint: str,
        created_at: str,
        data_revision_ref: str | None = None,
    ) -> PendingDataRefresh:
        """Atomically admit or structurally coalesce one pending refresh."""

        revision_value = data_revision.to_dict() if isinstance(data_revision, DataRevision) else data_revision
        if not isinstance(revision_value, Mapping):
            raise TypeError("data_revision must be a DataRevision or mapping")
        plan_value = self._pending_plan(plan)
        reopened = self._pending_ids(reopened_item_ids)
        if not isinstance(expected_parent_generation_id, str) or _GENERATION_ID_RE.fullmatch(expected_parent_generation_id) is None:
            raise DataRevisionError("expected_parent_generation_id is invalid")
        for value, label in (
            (expected_parent_state_hash, "expected_parent_state_hash"),
            (expected_parent_plan_hash, "expected_parent_plan_hash"),
            (launch_fingerprint, "launch_fingerprint"),
        ):
            _hash_value(value, label=label)
        if not isinstance(launch_draft_id, str) or not launch_draft_id or Path(launch_draft_id).name != launch_draft_id:
            raise DataRevisionError("launch_draft_id is invalid")
        if not isinstance(created_at, str) or not created_at:
            raise DataRevisionError("created_at is invalid")
        revision_id, _ = _parse_revision_id(revision_value.get("revision_id"), label="data_revision_id")
        revision_manifest_hash = _hash_value(revision_value.get("manifest_hash"), label="data_revision manifest_hash")
        archive_hash = _hash_value(revision_value.get("archive_sha256"), label="data_revision archive_sha256")
        ref = data_revision_ref or revision_value.get("archive_path") or revision_value.get("manifest_path")
        if isinstance(ref, str) and Path(ref).is_absolute():
            try:
                ref = Path(ref).relative_to(self.context.run_root).as_posix()
            except ValueError:
                # A legacy D-0001 archive may intentionally live in a
                # declared input root outside the run root.  The immutable
                # manifest remains the confined reference for the admission.
                manifest_ref = revision_value.get("manifest_path")
                if isinstance(manifest_ref, str) and Path(manifest_ref).is_absolute():
                    try:
                        ref = Path(manifest_ref).relative_to(self.context.run_root).as_posix()
                    except ValueError:
                        ref = None
                elif isinstance(manifest_ref, str):
                    ref = manifest_ref
        if not isinstance(ref, str) or not ref or Path(ref).is_absolute() or "\x00" in ref:
            raise DataRevisionError("data_revision_ref is invalid")
        _assert_path(self.context.run_root / ref, root=self.context.run_root, label="pending data revision reference")
        with self._locked():
            current = self._current_unlocked()
            if current is None or current.revision_id != revision_id or current.manifest_hash != revision_manifest_hash or current.archive_sha256 != archive_hash:
                raise RevisionCASMismatch("pending refresh data revision is stale")
            transaction = self._read_revision_transaction()
            if transaction is not None:
                if transaction.revision_id != current.revision_id:
                    if transaction.has_handoff:
                        raise RevisionConflictError("revision transaction targets another current revision")
                else:
                    self._recover_revision_transaction_unlocked(transaction)
            body: dict[str, Any] = {
                "schema_version": PENDING_DATA_REFRESH_SCHEMA_VERSION,
                "kind": "data_refresh_admission",
                "run_id": self.context.run_id,
                "data_revision": _jsonable(revision_value),
                "data_revision_ref": ref,
                "data_revision_id": revision_id,
                "data_revision_manifest_hash": revision_manifest_hash,
                "data_revision_archive_sha256": archive_hash,
                "plan": plan_value,
                "plan_hash": _hash_json(plan_value),
                "reopened_item_ids": list(reopened),
                "expected_parent_generation_id": expected_parent_generation_id,
                "expected_parent_state_hash": expected_parent_state_hash,
                "expected_parent_plan_hash": expected_parent_plan_hash,
                "original_parent_generation_id": expected_parent_generation_id,
                "original_parent_state_hash": expected_parent_state_hash,
                "original_parent_plan_hash": expected_parent_plan_hash,
                "launch_draft_id": launch_draft_id,
                "launch_fingerprint": launch_fingerprint,
                "created_at": created_at,
                "state": "pending",
                "applied_generation_id": None,
            }
            body["intent_hash"] = _hash_json(body)
            proposed = self._pending_from_value(body)
            applied = self._read_applied(proposed.intent_hash)
            if applied is not None:
                self._clear_revision_transaction(
                    revision_id=revision_id,
                    revision_manifest_hash=revision_manifest_hash,
                )
                return applied
            path = self.pending_data_refresh_path
            if path.exists() or path.is_symlink():
                existing = self._read_pending_file(path)
                if (
                    existing.expected_parent_generation_id != proposed.expected_parent_generation_id
                    or existing.expected_parent_state_hash != proposed.expected_parent_state_hash
                    or existing.expected_parent_plan_hash != proposed.expected_parent_plan_hash
                ):
                    raise PendingDataRefreshConflict("pending refresh parent CAS differs")
                merged_plan = self._coalesce_plans(existing.plan, proposed.plan)
                merged_reopened = tuple(dict.fromkeys((*existing.reopened_item_ids, *proposed.reopened_item_ids)))
                body.update(
                    {
                        "data_revision": _jsonable(revision_value),
                        "data_revision_ref": ref,
                        "data_revision_id": revision_id,
                        "data_revision_manifest_hash": revision_manifest_hash,
                        "data_revision_archive_sha256": archive_hash,
                        "plan": merged_plan,
                        "plan_hash": _hash_json(merged_plan),
                        "reopened_item_ids": list(merged_reopened),
                        # Preserve the first admission's parent CAS as
                        # immutable provenance while the canonical pending
                        # target structurally advances to a newer D.
                        "original_parent_generation_id": existing.original_parent_generation_id,
                        "original_parent_state_hash": existing.original_parent_state_hash,
                        "original_parent_plan_hash": existing.original_parent_plan_hash,
                        "launch_draft_id": launch_draft_id,
                        "launch_fingerprint": launch_fingerprint,
                        "created_at": created_at,
                        "state": "pending",
                        "applied_generation_id": None,
                    }
                )
                body.pop("intent_hash", None)
                body["intent_hash"] = _hash_json(body)
                proposed = self._pending_from_value(body)
                applied = self._read_applied(proposed.intent_hash)
                if applied is not None:
                    self._clear_revision_transaction(
                        revision_id=revision_id,
                        revision_manifest_hash=revision_manifest_hash,
                    )
                    return applied
            _atomic_replace(path, _canonical_bytes(proposed.to_dict()), root=self.context.run_root, label="pending data refresh")
            self._clear_revision_transaction(
                revision_id=revision_id,
                revision_manifest_hash=revision_manifest_hash,
            )
            return proposed

    def mark_pending_data_refresh_applied(
        self,
        intent_hash: str,
        *,
        generation_id: str,
    ) -> PendingDataRefresh | None:
        """Archive one admitted intent and atomically clear its current pointer."""

        _hash_value(intent_hash, label="pending intent_hash")
        if not isinstance(generation_id, str) or _GENERATION_ID_RE.fullmatch(generation_id) is None:
            raise DataRevisionError("applied generation_id is invalid")
        if f"G-{int(generation_id[2:]):04d}" != generation_id:
            raise DataRevisionError("applied generation_id is not canonical")
        with self._locked():
            path = self.pending_data_refresh_path
            if not path.exists() and not path.is_symlink():
                return self._read_applied(intent_hash)
            pending = self._read_pending_file(path)
            if pending.intent_hash != intent_hash:
                # A successor D admission may have replaced the canonical
                # pending bytes while this generation was being published.
                # Preserve that successor for the next safe boundary instead
                # of turning the successful older generation into a user
                # visible conflict.  The upload may have observed either the
                # generation's old parent (before publication) or the just-
                # published generation itself.  Unrelated parent CAS remains
                # fail-closed.
                current = self._current_unlocked()
                applied_ordinal = int(generation_id[2:])
                successor_parent = pending.expected_parent_generation_id
                old_parent = f"G-{applied_ordinal - 1:04d}"
                successor_is_later = (
                    current is not None
                    and current.revision_id == pending.data_revision_id
                    and current.manifest_hash == pending.data_revision_manifest_hash
                    and current.archive_sha256 == pending.data_revision_archive_sha256
                    and successor_parent in {old_parent, generation_id}
                )
                if successor_is_later:
                    return pending
                raise PendingDataRefreshConflict("pending refresh intent hash differs")
            applied_body = pending.to_dict()
            applied_body["state"] = "applied"
            applied_body["applied_generation_id"] = generation_id
            applied = self._pending_from_value(applied_body, applied=True)
            receipt_path = self._pending_archive_path(intent_hash)
            self._failpoint("before_pending_archive")
            _write_once(receipt_path, _canonical_bytes(applied.to_dict()), root=self.context.run_root, label="pending refresh applied receipt")
            self._failpoint("after_pending_archive")
            if path.is_symlink() or not path.is_file():
                raise DataRevisionError("pending data refresh pointer is not a regular file")
            self._failpoint("before_pending_clear")
            path.unlink()
            _fsync_directory(path.parent)
            return applied

    def rebase_pending_data_refresh(
        self,
        intent_hash: str,
        *,
        expected_parent_generation_id: str,
        expected_parent_state_hash: str,
        expected_parent_plan_hash: str,
        plan: Mapping[str, Any] | None = None,
    ) -> PendingDataRefresh:
        """Move an admission onto the current parent without losing provenance."""

        _hash_value(intent_hash, label="pending intent_hash")
        if not isinstance(expected_parent_generation_id, str) or _GENERATION_ID_RE.fullmatch(expected_parent_generation_id) is None:
            raise DataRevisionError("expected_parent_generation_id is invalid")
        for value, label in (
            (expected_parent_state_hash, "expected_parent_state_hash"),
            (expected_parent_plan_hash, "expected_parent_plan_hash"),
        ):
            _hash_value(value, label=label)
        plan_value = None if plan is None else self._pending_plan(plan)
        with self._locked():
            path = self.pending_data_refresh_path
            if not path.exists() and not path.is_symlink():
                applied = self._read_applied(intent_hash)
                if applied is not None:
                    return applied
                raise PendingDataRefreshConflict("pending refresh is unavailable")
            pending = self._read_pending_file(path)
            if pending.intent_hash != intent_hash:
                raise PendingDataRefreshConflict("pending refresh intent hash differs")
            body = pending.to_dict()
            if plan_value is not None:
                merged_plan = self._coalesce_plans(pending.plan, plan_value)
                body["plan"] = merged_plan
                body["plan_hash"] = _hash_json(merged_plan)
            body.update(
                {
                    "expected_parent_generation_id": expected_parent_generation_id,
                    "expected_parent_state_hash": expected_parent_state_hash,
                    "expected_parent_plan_hash": expected_parent_plan_hash,
                    "state": "pending",
                    "applied_generation_id": None,
                }
            )
            body.pop("intent_hash", None)
            body["intent_hash"] = _hash_json(body)
            rebased = self._pending_from_value(body)
            _atomic_replace(path, _canonical_bytes(rebased.to_dict()), root=self.context.run_root, label="pending data refresh")
            return rebased

    def _manifest_bytes(
        self,
        *,
        revision_id: str,
        parent: DataRevision | _RevisionIdentity | None,
        candidate: _Candidate,
        archive_alias: bool,
        archive_path: Path,
    ) -> tuple[bytes, str]:
        unsigned = {
            "schema_version": DATA_REVISION_SCHEMA_VERSION,
            "run_id": self.context.run_id,
            "revision_id": revision_id,
            "ordinal": int(revision_id.split("-", 1)[1]),
            "parent_revision_id": parent.revision_id if parent is not None else None,
            "parent_manifest_hash": parent.manifest_hash if parent is not None else None,
            "archive_path": _path_reference(self.context, archive_path) if archive_alias else self._canonical_archive_path(revision_id),
            "archive_sha256": candidate.archive_sha256,
            "archive_size_bytes": candidate.archive_size_bytes,
            "archive_alias": archive_alias,
            "archive_source_stat": dict(candidate.archive_source_stat),
            "central_directory_fingerprint": dict(candidate.central_directory_fingerprint),
            "catalog_path": self._canonical_catalog_path(revision_id),
            "catalog_key": candidate.catalog_key,
            "catalog_sha256": candidate.catalog_sha256,
            "catalog_schema_version": candidate.catalog_schema_version,
            "catalog_source_hash": candidate.catalog_source_hash,
            "catalog_core_version": candidate.catalog_core_version,
            "physical_inventory": [_jsonable(item) for item in candidate.physical_inventory],
            "member_inventory_hash": candidate.member_inventory_hash,
        }
        manifest_hash = _hash_json(unsigned)
        return _canonical_bytes({**unsigned, "manifest_hash": manifest_hash}), manifest_hash

    def _manifest_matches_candidate(
        self,
        revision: DataRevision,
        candidate: _Candidate,
        *,
        expected_parent: DataRevision | _RevisionIdentity | None,
    ) -> bool:
        return (
            revision.archive_sha256 == candidate.archive_sha256
            and revision.archive_size_bytes == candidate.archive_size_bytes
            and revision.catalog_key == candidate.catalog_key
            and revision.catalog_schema_version == candidate.catalog_schema_version
            and revision.catalog_source_hash == candidate.catalog_source_hash
            and revision.catalog_core_version == candidate.catalog_core_version
            and revision.catalog_sha256 == candidate.catalog_sha256
            and revision.member_inventory_hash == candidate.member_inventory_hash
            and tuple(_jsonable(item) for item in revision.physical_inventory) == tuple(_jsonable(item) for item in candidate.physical_inventory)
            and dict(revision.central_directory_fingerprint) == dict(candidate.central_directory_fingerprint)
            and ((expected_parent is None and revision.parent_revision_id is None) or (expected_parent is not None and revision.parent_revision_id == expected_parent.revision_id and revision.parent_manifest_hash == expected_parent.manifest_hash))
        )

    def _existing_revision_or_conflict(
        self,
        revision_id: str,
        candidate: _Candidate,
        *,
        expected_parent: DataRevision | _RevisionIdentity | None,
    ) -> DataRevision | None:
        directory = self._revision_directory(revision_id)
        if not directory.exists() and not directory.is_symlink():
            return None
        if directory.is_symlink() or not directory.is_dir():
            raise DataRevisionError("occupied data revision path is not a regular directory")
        manifest_path = directory / REVISION_MANIFEST_FILENAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise DataRevisionError("occupied data revision is incomplete")
        revision = self._read_revision(revision_id)
        if not self._manifest_matches_candidate(revision, candidate, expected_parent=expected_parent):
            raise RevisionConflictError(f"conflicting bytes already occupy {revision_id}")
        return revision

    def initialize_legacy(
        self,
        legacy_archive: str | Path | None = None,
        *,
        transaction: Mapping[str, Any] | None = None,
    ) -> DataRevision:
        """Publish/recover D-0001 as an alias to an existing legacy archive."""

        candidate_value = legacy_archive if legacy_archive is not None else "data_room.zip"
        if transaction is not None and not isinstance(transaction, Mapping):
            raise TypeError("transaction must be a mapping")
        with self._locked():
            current = self._current_unlocked()
            archive_path = _resolve_archive_candidate(self.context, candidate_value)
            if current is not None:
                first = self._read_revision("D-0001")
                if first.archive_path != archive_path.resolve(strict=True) or not first.archive_alias:
                    raise RevisionConflictError("legacy archive conflicts with published D-0001")
                if transaction is not None:
                    bound_transaction = self._transaction_for_revision(
                        first,
                        parent_revision_id=None,
                        parent_manifest_hash=None,
                        metadata=transaction,
                    )
                    self._publish_revision_transaction(bound_transaction)
                return current
            revision_id = "D-0001"
            revision_directory = self._revision_directory(revision_id)
            candidate = self._candidate_from_archive(archive_path, final_archive_path=archive_path)
            existing = self._existing_revision_or_conflict(revision_id, candidate, expected_parent=None)
            if existing is not None:
                if transaction is not None:
                    bound_transaction = self._transaction_for_revision(
                        existing,
                        parent_revision_id=None,
                        parent_manifest_hash=None,
                        metadata=transaction,
                    )
                    # Preserve the binding journal before recovering the
                    # pointer when a prior attempt left only the immutable
                    # D-0001 directory behind.
                    self._publish_revision_transaction(bound_transaction)
                self._publish_pointer(existing)
                return existing
            _ensure_directory(self.revisions_root, root=self.context.run_root, label="data-room revisions")
            stage_root = self.revisions_root / f".{revision_id}.stage-{uuid.uuid4().hex}"
            _ensure_directory(stage_root, root=self.context.run_root, label="data revision staging directory")
            try:
                stage_catalog = stage_root / REVISION_CATALOG_FILENAME
                _write_once(stage_catalog, candidate.catalog_bytes, root=self.context.run_root, label="data revision catalog")
                manifest_bytes, manifest_hash = self._manifest_bytes(
                    revision_id=revision_id,
                    parent=None,
                    candidate=candidate,
                    archive_alias=True,
                    archive_path=archive_path,
                )
                _write_once(stage_root / REVISION_MANIFEST_FILENAME, manifest_bytes, root=self.context.run_root, label="data revision manifest")
                _fsync_directory(stage_root)
                if revision_directory.exists() or revision_directory.is_symlink():
                    raise RevisionConflictError("D-0001 revision directory became occupied")
                os.replace(stage_root, revision_directory)
                stage_root = Path()
                _fsync_directory(self.revisions_root)
                revision = self._read_revision(revision_id)
                if revision.manifest_hash != manifest_hash:
                    raise DataRevisionError("published D-0001 manifest hash changed")
                if transaction is not None:
                    bound_transaction = self._transaction_for_revision(
                        revision,
                        parent_revision_id=None,
                        parent_manifest_hash=None,
                        metadata=transaction,
                    )
                    self._publish_revision_transaction(bound_transaction)
                self._publish_pointer(revision)
                return revision
            finally:
                if str(stage_root) not in {"", "."}:
                    shutil.rmtree(stage_root, ignore_errors=True)

    def _current_unlocked(self) -> DataRevision | None:
        pointer = self._read_pointer()
        if pointer is None:
            return None
        revision_id, manifest_hash = pointer
        revision = self._read_revision(revision_id)
        if revision.manifest_hash != manifest_hash:
            raise DataRevisionError("current pointer manifest hash does not match revision")
        return revision

    def _read_revision_identity(self, revision_id: str) -> _RevisionIdentity:
        """Read only the hash-bound manifest identity needed for CAS."""

        revision_id, ordinal = _parse_revision_id(revision_id)
        manifest_path = self._manifest_path(revision_id)
        value, _raw = self._read_json_file(manifest_path, label="data revision manifest")
        if set(value) != _MANIFEST_FIELDS or value.get("schema_version") != DATA_REVISION_SCHEMA_VERSION:
            raise DataRevisionError("data revision manifest identity is invalid")
        if value.get("run_id") != self.context.run_id or value.get("revision_id") != revision_id or value.get("ordinal") != ordinal:
            raise DataRevisionError("data revision manifest identity does not match run")
        unsigned = dict(value)
        manifest_hash = _hash_value(unsigned.pop("manifest_hash", None), label="data revision manifest_hash")
        if _hash_bytes(_canonical_bytes(unsigned)) != manifest_hash:
            raise DataRevisionError("data revision manifest hash does not match content")
        parent_id = value.get("parent_revision_id")
        parent_hash = value.get("parent_manifest_hash")
        if parent_id is not None:
            _parse_revision_id(parent_id, label="parent_revision_id")
            parent_hash = _hash_value(parent_hash, label="parent_manifest_hash")
        elif parent_hash is not None:
            raise DataRevisionError("data revision parent identity is invalid")
        return _RevisionIdentity(revision_id, ordinal, manifest_hash, parent_id, parent_hash)

    def _read_revision_manifest_binding(self, revision_id: str) -> _RevisionManifestBinding:
        """Read the manifest fields needed to compare an applied receipt.

        This deliberately avoids opening the archive/catalog or constructing a
        ``DataRoom``.  The winning revision is still passed through ``load``
        before it is returned by ``active_generation_revision``; historical
        candidates only need their canonical, hash-bound manifest identity for
        selection and conflict detection.
        """

        revision_id, ordinal = _parse_revision_id(revision_id)
        manifest_path = self._manifest_path(revision_id)
        value, _raw = self._read_json_file(manifest_path, label="data revision manifest")
        if set(value) != _MANIFEST_FIELDS or value.get("schema_version") != DATA_REVISION_SCHEMA_VERSION:
            raise DataRevisionError("data revision manifest identity is invalid")
        if value.get("run_id") != self.context.run_id or value.get("revision_id") != revision_id:
            raise DataRevisionError("data revision manifest identity does not match run")
        if value.get("ordinal") != ordinal or isinstance(value.get("ordinal"), bool):
            raise DataRevisionError("data revision manifest ordinal is invalid")
        unsigned = dict(value)
        manifest_hash = _hash_value(unsigned.pop("manifest_hash", None), label="data revision manifest_hash")
        if _hash_bytes(_canonical_bytes(unsigned)) != manifest_hash:
            raise DataRevisionError("data revision manifest hash does not match content")

        parent_id = value.get("parent_revision_id")
        parent_hash = value.get("parent_manifest_hash")
        if ordinal == 1:
            if parent_id is not None or parent_hash is not None:
                raise DataRevisionError("D-0001 cannot have a parent")
        else:
            if parent_id is None or parent_hash is None:
                raise DataRevisionError("data revision parent identity is missing")
            parent_id, parent_ordinal = _parse_revision_id(parent_id, label="parent_revision_id")
            if parent_ordinal != ordinal - 1:
                raise DataRevisionError("data revision parent ordinal is not contiguous")
            parent_hash = _hash_value(parent_hash, label="parent_manifest_hash")

        archive_alias = value.get("archive_alias")
        if not isinstance(archive_alias, bool):
            raise DataRevisionError("data revision archive_alias is invalid")
        if ordinal == 1 and not archive_alias:
            raise DataRevisionError("D-0001 must reference a legacy archive alias")
        if ordinal > 1 and archive_alias:
            raise DataRevisionError("later data revisions cannot alias the legacy archive")
        archive_value = value.get("archive_path")
        archive_path = _resolve_manifest_path(self.context, archive_value, label="data revision archive")
        if archive_alias:
            if archive_value == self._canonical_archive_path(revision_id):
                raise DataRevisionError("legacy archive alias cannot point at revision archive")
            _assert_path(archive_path, root=archive_path.parent, label="data revision archive", regular=True)
        else:
            if archive_value != self._canonical_archive_path(revision_id):
                raise DataRevisionError("data revision archive path is not canonical")
            _assert_path(archive_path, root=self.context.run_root, label="data revision archive", regular=True)
        archive_hash = _hash_value(value.get("archive_sha256"), label="archive_sha256")
        archive_size = value.get("archive_size_bytes")
        if isinstance(archive_size, bool) or not isinstance(archive_size, int) or archive_size < 0:
            raise DataRevisionError("archive_size_bytes is invalid")
        catalog_path = value.get("catalog_path")
        if catalog_path != self._canonical_catalog_path(revision_id):
            raise DataRevisionError("data revision catalog path is not canonical")
        _hash_value(value.get("catalog_sha256"), label="catalog_sha256")
        _hash_value(value.get("catalog_source_hash"), label="catalog_source_hash")
        _hash_value(value.get("member_inventory_hash"), label="member_inventory_hash")
        return _RevisionManifestBinding(
            revision_id=revision_id,
            ordinal=ordinal,
            manifest_hash=manifest_hash,
            parent_revision_id=parent_id,
            parent_manifest_hash=parent_hash,
            archive_path=archive_path,
            archive_sha256=archive_hash,
            archive_size_bytes=archive_size,
        )

    def _current_identity_unlocked(self) -> _RevisionIdentity | None:
        pointer = self._read_pointer()
        if pointer is None:
            return None
        revision_id, manifest_hash = pointer
        identity = self._read_revision_identity(revision_id)
        if identity.manifest_hash != manifest_hash:
            raise DataRevisionError("current pointer manifest hash does not match revision")
        return identity

    def _revision_from_candidate(
        self,
        revision_id: str,
        candidate: _Candidate,
        *,
        parent: DataRevision | _RevisionIdentity,
        manifest_hash: str,
    ) -> DataRevision:
        revision_id, ordinal = _parse_revision_id(revision_id)
        archive_path = self._revision_directory(revision_id) / REVISION_ARCHIVE_FILENAME
        catalog_path = self._revision_directory(revision_id) / REVISION_CATALOG_FILENAME
        manifest_path = self._manifest_path(revision_id)
        return DataRevision(
            run_id=self.context.run_id,
            revision_id=revision_id,
            ordinal=ordinal,
            parent_revision_id=parent.revision_id,
            parent_manifest_hash=parent.manifest_hash,
            archive_path=archive_path,
            archive_sha256=candidate.archive_sha256,
            archive_size_bytes=candidate.archive_size_bytes,
            archive_alias=False,
            archive_source_stat=candidate.archive_source_stat,
            central_directory_fingerprint=candidate.central_directory_fingerprint,
            catalog_path=catalog_path,
            catalog_key=candidate.catalog_key,
            catalog_sha256=candidate.catalog_sha256,
            catalog_schema_version=candidate.catalog_schema_version,
            catalog_source_hash=candidate.catalog_source_hash,
            catalog_core_version=candidate.catalog_core_version,
            physical_inventory=candidate.physical_inventory,
            member_inventory_hash=candidate.member_inventory_hash,
            manifest_path=manifest_path,
            manifest_hash=manifest_hash,
        )

    def append(
        self,
        candidate_archive: str | Path,
        *,
        expected_current_revision_id: str | None = None,
        expected_current_manifest_hash: str | None = None,
        transaction: Mapping[str, Any] | None = None,
    ) -> DataRevision:
        """Append a fully materialized archive under an expected-current CAS."""

        if expected_current_revision_id is not None:
            expected_current_revision_id, _expected_ordinal = _parse_revision_id(
                expected_current_revision_id,
                label="expected_current_revision_id",
            )
        if expected_current_manifest_hash is not None:
            _hash_value(expected_current_manifest_hash, label="expected_current_manifest_hash")
        if transaction is not None and not isinstance(transaction, Mapping):
            raise TypeError("transaction must be a mapping")

        materialized = self._materialize_candidate(candidate_archive)
        stage_root: Path | None = materialized.stage_root
        try:
            with self._locked():
                # Only pointer/manifest identity is read here.  Archive
                # inventory, cataloging, and hashing happened before lock
                # acquisition against the immutable staged archive.
                current = self._current_identity_unlocked()
                if current is None:
                    raise RevisionCASMismatch("cannot append without an initialized current revision")
                proposal_parent: DataRevision | _RevisionIdentity = current
                if expected_current_revision_id is not None:
                    proposal_parent = self._read_revision_identity(expected_current_revision_id)
                    if expected_current_manifest_hash is not None and proposal_parent.manifest_hash != expected_current_manifest_hash:
                        raise RevisionCASMismatch("expected current manifest hash does not match revision")
                target_revision_id = _revision_id(proposal_parent.ordinal + 1)
                final_directory = self._revision_directory(target_revision_id)
                final_archive_path = final_directory / REVISION_ARCHIVE_FILENAME
                candidate = self._candidate_for_target(materialized.candidate, final_archive_path=final_archive_path)

                # Reject a stale CAS before interpreting a different candidate
                # as an occupied-ordinal conflict.  The only exception is an
                # exact retry whose current pointer already names the target.
                if expected_current_revision_id is not None and current.revision_id not in {
                    expected_current_revision_id,
                    target_revision_id,
                }:
                    raise RevisionCASMismatch("expected current revision is stale")
                if expected_current_manifest_hash is not None and current.revision_id != target_revision_id and current.manifest_hash != expected_current_manifest_hash:
                    raise RevisionCASMismatch("expected current manifest hash is stale")

                existing = self._existing_revision_or_conflict(target_revision_id, candidate, expected_parent=proposal_parent)
                if existing is not None:
                    if current.revision_id not in {proposal_parent.revision_id, target_revision_id}:
                        raise RevisionCASMismatch("expected current revision is stale")
                    if current.revision_id == target_revision_id:
                        if transaction is not None:
                            bound_transaction = self._transaction_for_revision(
                                existing,
                                parent_revision_id=proposal_parent.revision_id,
                                parent_manifest_hash=proposal_parent.manifest_hash,
                                metadata=transaction,
                            )
                            self._publish_revision_transaction(bound_transaction)
                        return existing
                    if transaction is not None:
                        bound_transaction = self._transaction_for_revision(
                            existing,
                            parent_revision_id=proposal_parent.revision_id,
                            parent_manifest_hash=proposal_parent.manifest_hash,
                            metadata=transaction,
                        )
                        self._publish_revision_transaction(bound_transaction)
                    self._publish_pointer(existing)
                    return existing
                if expected_current_revision_id is not None and current.revision_id != expected_current_revision_id:
                    raise RevisionCASMismatch("expected current revision is stale")
                if expected_current_manifest_hash is not None and current.manifest_hash != expected_current_manifest_hash:
                    raise RevisionCASMismatch("expected current manifest hash is stale")
                if final_directory.exists() or final_directory.is_symlink():
                    raise RevisionConflictError(f"conflicting bytes already occupy {target_revision_id}")

                # The staged archive is already verified and is moved as a
                # complete directory; no mutable caller input is reopened.
                _write_once(stage_root / REVISION_CATALOG_FILENAME, candidate.catalog_bytes, root=self.context.run_root, label="data revision catalog")
                manifest_bytes, manifest_hash = self._manifest_bytes(
                    revision_id=target_revision_id,
                    parent=proposal_parent,
                    candidate=candidate,
                    archive_alias=False,
                    archive_path=final_archive_path,
                )
                _write_once(stage_root / REVISION_MANIFEST_FILENAME, manifest_bytes, root=self.context.run_root, label="data revision manifest")
                _fsync_directory(stage_root)
                os.replace(stage_root, final_directory)
                stage_root = None
                _fsync_directory(self.revisions_root)
                revision = self._revision_from_candidate(
                    target_revision_id,
                    candidate,
                    parent=proposal_parent,
                    manifest_hash=manifest_hash,
                )
                if transaction is not None:
                    bound_transaction = self._transaction_for_revision(
                        revision,
                        parent_revision_id=proposal_parent.revision_id,
                        parent_manifest_hash=proposal_parent.manifest_hash,
                        metadata=transaction,
                    )
                    # The journal is durable before the current pointer
                    # advances.  A crash or failpoint at either boundary is
                    # therefore recoverable without a pointer-only D state.
                    self._publish_revision_transaction(bound_transaction)
                self._publish_pointer(revision)
                return revision
        finally:
            if stage_root is not None:
                shutil.rmtree(stage_root, ignore_errors=True)


__all__ = [
    "CURRENT_POINTER_PATH",
    "PENDING_DATA_REFRESH_ARCHIVE_ROOT",
    "PENDING_DATA_REFRESH_PATH",
    "PENDING_DATA_REFRESH_SCHEMA_VERSION",
    "REVISION_TRANSACTION_PATH",
    "REVISION_TRANSACTION_SCHEMA_VERSION",
    "DATA_REVISION_POINTER_SCHEMA_VERSION",
    "DATA_REVISION_SCHEMA_VERSION",
    "DATA_ROOM_ROOT",
    "DataRevision",
    "DataRevisionError",
    "DataRevisionTransaction",
    "DataRevisionStore",
    "PendingDataRefresh",
    "PendingDataRefreshConflict",
    "RevisionCASMismatch",
    "RevisionConflictError",
    "REVISION_ARCHIVE_FILENAME",
    "REVISION_CATALOG_FILENAME",
    "REVISION_MANIFEST_FILENAME",
    "REVISION_ROOT",
]
