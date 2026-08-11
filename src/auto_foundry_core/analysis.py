"""Program-bound analysis context and bounded script execution.

This module is the narrow boundary between an analyst-authored calculation and
the run-owned workbench.  Scripts receive the context through the
``AUTO_FOUNDRY_ANALYSIS_CONTEXT`` environment variable and can load it with
``load_bound_analysis_context``; they do not need (or get encouraged) to copy
dataset paths, run paths, or source hashes into source code.

The runner provides process, path, timeout, and output bounds.  It is *not* a
security sandbox: hostile code still requires operating-system/container
isolation outside this package.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
import uuid

try:  # pragma: no cover - POSIX hosts provide advisory flock
    import fcntl
except ImportError:  # pragma: no cover - defensive non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

from .contracts import DataAssetRef
from .durable import ItemWorkspace
from .prepared import PreparedAssetRegistry
from .workbench import CatalogCounts, DataRoomCatalogEntry, DataRoomMember, DataRoomWorkbench
from .workspace import AllowedRootError, RunContext


ANALYSIS_CONTEXT_SCHEMA_VERSION = "2"
ANALYSIS_CONTEXT_ENV = "AUTO_FOUNDRY_ANALYSIS_CONTEXT"
ANALYSIS_PHASE_ENV = "AUTO_FOUNDRY_ANALYSIS_PHASE"
ANALYSIS_SAMPLE_LIMIT_ENV = "AUTO_FOUNDRY_SAMPLE_LIMIT"
ANALYSIS_OUTPUT_ROOT_ENV = "AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT"
_MANIFEST_FILENAME = "analysis_context.json"
_RECEIPT_DIR = Path("script_receipts")
_TRANSITION_AUDIT_FILENAME = "analysis_context_transitions.jsonl"
_TRANSITION_STATE_FILENAME = "analysis_context_transition_state.json"
_TRANSITION_INTENT_FILENAME = "analysis_context_transition_intent.json"
_TRANSITION_LOCK_FILENAME = "analysis_context_transition.lock"
_TRANSITION_AUDIT_FIELDS = frozenset(
    {
        "record_kind",
        "transition_id",
        "run_id",
        "item_id",
        "before_manifest_hash",
        "after_manifest_hash",
        "old_core_version",
        "new_core_version",
        "old_skill_version",
        "new_skill_version",
        "old_sha",
        "new_sha",
        "old_tree",
        "new_tree",
        "previous_hash",
        "record_hash",
    }
)
_TRANSITION_STATE_FIELDS = frozenset(
    {
        "record_kind",
        "run_id",
        "item_id",
        "audit_path",
        "audit_count",
        "audit_head",
        "manifest_file_hash",
        "state_hash",
    }
)
_TRANSITION_INTENT_FIELDS = frozenset(
    {
        "record_kind",
        "operation_id",
        "run_id",
        "item_id",
        "audit_path",
        "state_path",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_audit_count",
        "before_audit_head",
        "expected_audit",
        "candidate_manifest",
        "phase",
        "intent_hash",
    }
)
_DEFAULT_TIMEOUT_SECONDS = 3600.0
_DEFAULT_OUTPUT_BYTES = 256 * 1024
_DEFAULT_SAMPLE_LIMIT = 100
_VALID_PHASES = frozenset({"smoke", "full"})
_SAME_ATTEMPT_ERRORS = frozenset(
    {
        "SyntaxError",
        "NameError",
        "TypeError",
        "ModuleNotFoundError",
        "ImportError",
        "AttributeError",
        "KeyError",
        "ValueError",
    }
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, DataAssetRef):
        return value.to_dict()
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple | set | frozenset):
        return tuple(_freeze(item) for item in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_stat_signature(path: Path) -> dict[str, int]:
    stat_result = path.stat()
    return {
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Durably publish a directory entry on POSIX filesystems."""

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


def _json_file_bytes(value: Any) -> bytes:
    return (json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _atomic_write_json_durable(path: Path, value: Any) -> None:
    """Atomically write a JSON object and fsync its containing directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_json_file_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _transition_failpoint(stage: str) -> None:
    """Test hook for process-loss boundaries; production execution is a no-op."""

    return None


def _transition_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: value[key] for key in value if key not in {"record_hash", "state_hash", "intent_hash"}}
    return _sha256_bytes(_json_bytes(unsigned))


def _transition_regular(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    return path


def _transition_paths(manifest_path: Path) -> tuple[Path, Path, Path]:
    root = manifest_path.parent
    return (
        root / _TRANSITION_AUDIT_FILENAME,
        root / _TRANSITION_STATE_FILENAME,
        root / _TRANSITION_INTENT_FILENAME,
    )


@contextmanager
def _transition_lock(manifest_path: Path):
    """Serialize context transition reconciliation and publication."""

    lock_path = manifest_path.parent / _TRANSITION_LOCK_FILENAME
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError("analysis context transition lock is not a regular file")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _manifest_bytes(value: Mapping[str, Any]) -> bytes:
    return _json_file_bytes(value)


def _manifest_file_digest(path: Path) -> str:
    return _sha256_file(_transition_regular(path, label="analysis context manifest"))


def _append_transition_record(path: Path, record: Mapping[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("analysis context transition audit is not a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(_json_bytes(record) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    # A directory fsync is needed both for first creation and for durable
    # replacement/append visibility after a process loss.
    _fsync_directory(path.parent)


def _read_transition_audit(path: Path, *, run_id: str, item_id: str) -> list[dict[str, Any]]:
    if not path.exists() and not path.is_symlink():
        return []
    _transition_regular(path, label="analysis context transition audit")
    records: list[dict[str, Any]] = []
    previous: str | None = None
    for line_number, line in enumerate(path.read_bytes().splitlines(), 1):
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"analysis context transition line {line_number} is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != _TRANSITION_AUDIT_FIELDS:
            raise ValueError(f"analysis context transition line {line_number} fields are invalid")
        value = dict(value)
        if value["record_kind"] != "analysis_context_implementation_transition":
            raise ValueError("analysis context transition record kind is invalid")
        if value["run_id"] != run_id or value["item_id"] != item_id:
            raise ValueError("analysis context transition identity does not match")
        if value["previous_hash"] != previous:
            raise ValueError("analysis context transition hash chain is broken")
        if value["record_hash"] != _transition_digest(value):
            raise ValueError("analysis context transition hash does not match content")
        for field_name in ("before_manifest_hash", "after_manifest_hash"):
            if not isinstance(value[field_name], str) or len(value[field_name]) != 64:
                raise ValueError("analysis context transition manifest hash is invalid")
        for field_name in ("old_sha", "new_sha", "old_tree", "new_tree"):
            raw = value[field_name]
            if not isinstance(raw, str) or len(raw) != 40 or any(char not in "0123456789abcdef" for char in raw):
                raise ValueError("analysis context transition implementation identity is invalid")
        if not isinstance(value["transition_id"], str) or not value["transition_id"]:
            raise ValueError("analysis context transition ID is invalid")
        if any(record["transition_id"] == value["transition_id"] for record in records):
            raise ValueError("analysis context transition IDs must be unique")
        previous = value["record_hash"]
        records.append(value)
    return records


def _read_transition_state(path: Path, *, run_id: str, item_id: str) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    _transition_regular(path, label="analysis context transition state")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("analysis context transition state is invalid") from exc
    if not isinstance(value, Mapping) or set(value) != _TRANSITION_STATE_FIELDS:
        raise ValueError("analysis context transition state fields are invalid")
    value = dict(value)
    if value["record_kind"] != "analysis_context_implementation_transition_state":
        raise ValueError("analysis context transition state kind is invalid")
    if value["run_id"] != run_id or value["item_id"] != item_id:
        raise ValueError("analysis context transition state identity does not match")
    if value["audit_path"] != f"work/{_TRANSITION_AUDIT_FILENAME}":
        raise ValueError("analysis context transition state audit path is invalid")
    count = value["audit_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("analysis context transition state count is invalid")
    head = value["audit_head"]
    if head is not None and (not isinstance(head, str) or len(head) != 64):
        raise ValueError("analysis context transition state head is invalid")
    if count == 0 and head is not None or count > 0 and head is None:
        raise ValueError("analysis context transition state head/count mismatch")
    if not isinstance(value["manifest_file_hash"], str) or len(value["manifest_file_hash"]) != 64:
        raise ValueError("analysis context transition state manifest hash is invalid")
    if value["state_hash"] != _transition_digest(value):
        raise ValueError("analysis context transition state hash does not match content")
    return value


def _read_transition_intent(path: Path, *, run_id: str, item_id: str) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    _transition_regular(path, label="analysis context transition intent")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("analysis context transition intent is invalid") from exc
    if not isinstance(value, Mapping) or set(value) != _TRANSITION_INTENT_FIELDS:
        raise ValueError("analysis context transition intent fields are invalid")
    value = dict(value)
    if value["record_kind"] != "analysis_context_implementation_transition_intent":
        raise ValueError("analysis context transition intent kind is invalid")
    if value["run_id"] != run_id or value["item_id"] != item_id:
        raise ValueError("analysis context transition intent identity does not match")
    if value["audit_path"] != f"work/{_TRANSITION_AUDIT_FILENAME}" or value["state_path"] != f"work/{_TRANSITION_STATE_FILENAME}":
        raise ValueError("analysis context transition intent paths are invalid")
    if value["phase"] not in {"intent", "audit_appended", "manifest_persisted", "state_persisted"}:
        raise ValueError("analysis context transition intent phase is invalid")
    for field_name in ("before_manifest_hash", "after_manifest_hash"):
        if not isinstance(value[field_name], str) or len(value[field_name]) != 64:
            raise ValueError("analysis context transition intent manifest hash is invalid")
    count = value["before_audit_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("analysis context transition intent count is invalid")
    head = value["before_audit_head"]
    if head is not None and (not isinstance(head, str) or len(head) != 64):
        raise ValueError("analysis context transition intent head is invalid")
    expected = value["expected_audit"]
    if not isinstance(expected, Mapping) or set(expected) != _TRANSITION_AUDIT_FIELDS:
        raise ValueError("analysis context transition intent audit is invalid")
    if expected["previous_hash"] != head or expected["record_hash"] != _transition_digest(expected):
        raise ValueError("analysis context transition intent audit hash is invalid")
    candidate = value["candidate_manifest"]
    if not isinstance(candidate, Mapping):
        raise ValueError("analysis context transition intent candidate is invalid")
    if candidate.get("manifest_hash") != _sha256_bytes(_json_bytes({key: item for key, item in candidate.items() if key != "manifest_hash"})):
        raise ValueError("analysis context transition intent candidate hash is invalid")
    if _sha256_bytes(_manifest_bytes(candidate)) != value["after_manifest_hash"]:
        raise ValueError("analysis context transition intent candidate bytes are invalid")
    if value["intent_hash"] != _transition_digest(value):
        raise ValueError("analysis context transition intent hash does not match content")
    return value


def _write_transition_state(path: Path, *, run_id: str, item_id: str, count: int, head: str | None, manifest_hash: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "record_kind": "analysis_context_implementation_transition_state",
        "run_id": run_id,
        "item_id": item_id,
        "audit_path": f"work/{_TRANSITION_AUDIT_FILENAME}",
        "audit_count": int(count),
        "audit_head": head,
        "manifest_file_hash": manifest_hash,
    }
    value["state_hash"] = _transition_digest(value)
    _atomic_write_json_durable(path, value)
    return value


def _write_transition_intent(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["intent_hash"] = _transition_digest(payload)
    _atomic_write_json_durable(path, payload)
    return payload


def _clear_transition_intent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ValueError("analysis context transition intent is not a regular file")
        path.unlink()
        _fsync_directory(path.parent)


def _reconcile_transition_journal(
    manifest_path: Path,
    *,
    run_id: str,
    item_id: str,
    _locked: bool = False,
) -> None:
    """Converge one interrupted implementation rebind or fail closed."""

    if not _locked:
        with _transition_lock(manifest_path):
            _reconcile_transition_journal(manifest_path, run_id=run_id, item_id=item_id, _locked=True)
        return

    audit_path, state_path, intent_path = _transition_paths(manifest_path)
    audit = _read_transition_audit(audit_path, run_id=run_id, item_id=item_id)
    state = _read_transition_state(state_path, run_id=run_id, item_id=item_id)
    intent = _read_transition_intent(intent_path, run_id=run_id, item_id=item_id)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("analysis context manifest is not a regular file")
    current_manifest_hash = _manifest_file_digest(manifest_path)
    actual_count = len(audit)
    actual_head = audit[-1]["record_hash"] if audit else None

    if intent is None:
        if state is None:
            if audit:
                raise ValueError("analysis context transition audit has no durable state anchor")
            return
        if state["audit_count"] != actual_count or state["audit_head"] != actual_head:
            raise ValueError("analysis context transition audit does not match its durable state anchor")
        if state["manifest_file_hash"] != current_manifest_hash:
            raise ValueError("analysis context transition state does not match manifest")
        return

    expected = dict(intent["expected_audit"])
    before_count = int(intent["before_audit_count"])
    before_head = intent["before_audit_head"]
    expected_count = before_count + 1
    expected_head = expected["record_hash"]
    if actual_count not in {before_count, expected_count}:
        raise ValueError("analysis context transition intent audit count is not recoverable")
    if actual_count == before_count:
        if actual_head != before_head:
            raise ValueError("analysis context transition intent audit prefix is invalid")
    else:
        if actual_head != expected_head or audit[-1] != expected:
            raise ValueError("analysis context transition intent audit tail is invalid")
    if state is not None:
        if state["audit_count"] not in {before_count, expected_count}:
            raise ValueError("analysis context transition intent state count is invalid")
        if state["audit_count"] == before_count and state["audit_head"] != before_head:
            raise ValueError("analysis context transition intent prior state head is invalid")
        if state["audit_count"] == expected_count and state["audit_head"] != expected_head:
            raise ValueError("analysis context transition intent final state head is invalid")
        if state["audit_count"] == before_count and state["manifest_file_hash"] != intent["before_manifest_hash"]:
            raise ValueError("analysis context transition intent prior manifest state is invalid")
        if state["audit_count"] == expected_count and state["manifest_file_hash"] != intent["after_manifest_hash"]:
            raise ValueError("analysis context transition intent final manifest state is invalid")
    elif before_count:
        raise ValueError("analysis context transition intent is missing its prior state anchor")

    if current_manifest_hash not in {intent["before_manifest_hash"], intent["after_manifest_hash"]}:
        raise ValueError("analysis context transition manifest is not recoverable")
    if current_manifest_hash == intent["before_manifest_hash"]:
        if actual_count == before_count:
            _append_transition_record(audit_path, expected)
            actual_count = expected_count
            actual_head = expected_head
        _atomic_write_json_durable(manifest_path, intent["candidate_manifest"])
        current_manifest_hash = intent["after_manifest_hash"]
    elif actual_count != expected_count:
        raise ValueError("analysis context transition manifest advanced before its audit record")

    _write_transition_state(
        state_path,
        run_id=run_id,
        item_id=item_id,
        count=expected_count,
        head=expected_head,
        manifest_hash=current_manifest_hash,
    )
    _clear_transition_intent(intent_path)


def _assert_no_symlink_components(path: Path, *, root: Path) -> Path:
    """Validate lexical containment and reject symlink components."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AllowedRootError(f"path escapes bound root: {path}") from exc
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        raise AllowedRootError(f"path escapes bound root: {path}")
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError(f"bound path cannot use symlink: {current}")
    return path


def _regular_file(path: Path, *, root: Path, label: str) -> Path:
    _assert_no_symlink_components(path, root=root)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file: {path}")
    return path


@dataclass(frozen=True)
class CatalogSnapshot:
    """Hash-bound view of one canonical physical catalog."""

    path: Path
    content_hash: str
    catalog_key: str
    catalog_schema_version: str
    source_hash: str
    core_version: str
    entries: tuple[DataRoomCatalogEntry, ...]
    counts: CatalogCounts

    @property
    def catalog_path(self) -> Path:
        return self.path

    @property
    def archive_hash(self) -> str:
        return self.source_hash

    @classmethod
    def from_workbench(cls, workbench: DataRoomWorkbench) -> "CatalogSnapshot":
        entries = workbench.catalog()
        room = workbench.data_room
        path = room.catalog_path
        if not path.is_file():
            raise ValueError("canonical catalog was not materialized")
        return cls(
            path=path,
            content_hash=_sha256_file(path),
            catalog_key=room.catalog_key,
            catalog_schema_version=room.catalog_schema_version,
            source_hash=room.archive_ref.content_hash or "",
            core_version=workbench.context.core_version,
            entries=tuple(entries),
            counts=room.catalog_counts(entries),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "content_hash": self.content_hash,
            "catalog_key": self.catalog_key,
            "catalog_schema_version": self.catalog_schema_version,
            "source_hash": self.source_hash,
            "core_version": self.core_version,
            "counts": self.counts.to_dict(),
        }


@dataclass(frozen=True)
class ScriptExecutionReceipt:
    """One compile, dependency, smoke, full, or deterministic script event."""

    receipt_id: str
    phase: str
    script_path: str
    script_hash: str | None
    context_path: str
    context_hash: str
    source_hash: str
    started_at: str
    finished_at: str
    wall_seconds: float
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    output_limited: bool = False
    error_type: str | None = None
    error_category: str | None = None
    traceback: str | None = None
    output_hashes: Mapping[str, str] = field(default_factory=dict)
    receipt_path: str | None = None

    @property
    def same_attempt_feedback(self) -> bool:
        """All local script failures stay in the current analyst attempt."""

        return self.error_type is not None or self.error_category is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "phase": self.phase,
            "script_path": self.script_path,
            "script_hash": self.script_hash,
            "context_path": self.context_path,
            "context_hash": self.context_hash,
            "source_hash": self.source_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wall_seconds": self.wall_seconds,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "output_limited": self.output_limited,
            "error_type": self.error_type,
            "error_category": self.error_category,
            "traceback": self.traceback,
            "output_hashes": dict(self.output_hashes),
            "receipt_path": self.receipt_path,
        }


@dataclass(frozen=True)
class ScriptRunReport:
    """Aggregate result for one bounded script pipeline."""

    status: str
    same_attempt_feedback: bool
    receipts: tuple[ScriptExecutionReceipt, ...]
    output_hashes: Mapping[str, str] = field(default_factory=dict)
    deterministic_match: bool | None = None
    error_category: str | None = None
    error_type: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "passed"


class BoundAnalysisContext:
    """Immutable program-owned context exposed to an analysis script."""

    def __init__(
        self,
        *,
        context: RunContext,
        source_identity: DataAssetRef,
        workbench: DataRoomWorkbench,
        source_catalog: CatalogSnapshot,
        item_workspace: ItemWorkspace,
        ontology_bundle: Any,
        manifest_path: Path,
        manifest_hash: str,
        runner_config: Mapping[str, Any] | None = None,
        telemetry: Any = None,
    ) -> None:
        self.context = context
        self._source_identity = source_identity
        self._workbench = workbench
        self._source_catalog = source_catalog
        self._item_workspace = item_workspace
        self._ontology_bundle = _freeze(ontology_bundle)
        self.manifest_path = manifest_path
        self.manifest_hash = manifest_hash
        config = dict(runner_config or {})
        timeout = float(config.get("default_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
        output_bytes = int(config.get("default_output_bytes", _DEFAULT_OUTPUT_BYTES))
        if timeout <= 0 or output_bytes <= 0:
            raise ValueError("analysis runner config must be positive")
        self.runner_config = MappingProxyType(
            {"default_timeout_seconds": timeout, "default_output_bytes": output_bytes}
        )
        self.telemetry = telemetry
        self._script_runner: ControlledScriptRunner | None = None

    @classmethod
    def create(
        cls,
        context: RunContext,
        archive: str | Path | DataAssetRef,
        item_workspace: ItemWorkspace,
        *,
        ontology_bundle: Any = (),
        telemetry: Any = None,
        workbench: DataRoomWorkbench | None = None,
    ) -> "BoundAnalysisContext":
        if not isinstance(context, RunContext):
            raise TypeError("BoundAnalysisContext requires one RunContext")
        if not isinstance(item_workspace, ItemWorkspace):
            raise TypeError("item_workspace must be an ItemWorkspace")
        if item_workspace.context is not context:
            raise ValueError("item_workspace must use the same RunContext")
        if workbench is None:
            workbench = DataRoomWorkbench(context, archive, telemetry=telemetry)
        elif workbench.context is not context:
            raise ValueError("workbench must use the same RunContext")
        source_identity = workbench.data_room.archive_ref
        snapshot = CatalogSnapshot.from_workbench(workbench)
        physical_members = [member.to_dict() for member in workbench.data_room.members()]
        physical_inventory = {
            "source_stat": dict(workbench.data_room.source_stat_signature),
            "central_directory_fingerprint": dict(workbench.data_room.central_directory_fingerprint),
            "inventory_hash": _sha256_bytes(_json_bytes(physical_members)),
            "members": physical_members,
        }
        runner_config = {
            "default_timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
            "default_output_bytes": _DEFAULT_OUTPUT_BYTES,
        }
        manifest_path = item_workspace.work_root / _MANIFEST_FILENAME
        _assert_no_symlink_components(manifest_path, root=item_workspace.item_root)
        unsigned = {
            "schema_version": ANALYSIS_CONTEXT_SCHEMA_VERSION,
            "kind": "bound_analysis_context",
            "run_id": context.run_id,
            "run_root": str(context.run_root),
            "input_roots": [str(root) for root in context.input_roots],
            "core_version": context.core_version,
            "skill_version": context.skill_version,
            "item_id": item_workspace.item_id,
            "item_mode": item_workspace.mode,
            "source_identity": source_identity.to_dict(),
            "catalog": snapshot.to_dict(),
            "physical_inventory": physical_inventory,
            "runner_config": runner_config,
            "ontology_bundle": _jsonable(ontology_bundle),
            "manifest_path": str(manifest_path),
        }
        manifest_hash = _sha256_bytes(_json_bytes(unsigned))
        manifest = {**unsigned, "manifest_hash": manifest_hash}
        _atomic_write_json(manifest_path, manifest)
        bound = cls(
            context=context,
            source_identity=source_identity,
            workbench=workbench,
            source_catalog=snapshot,
            item_workspace=item_workspace,
            ontology_bundle=ontology_bundle,
            manifest_path=manifest_path,
            manifest_hash=manifest_hash,
            runner_config=runner_config,
            telemetry=telemetry,
        )
        bound._script_runner = ControlledScriptRunner(
            bound,
            default_timeout_seconds=float(runner_config["default_timeout_seconds"]),
            default_output_bytes=int(runner_config["default_output_bytes"]),
        )
        return bound

    @property
    def data_room(self):
        return self._workbench.data_room

    @property
    def workbench(self) -> DataRoomWorkbench:
        return self._workbench

    @property
    def source_identity(self) -> DataAssetRef:
        return self._source_identity

    @property
    def source_catalog(self) -> CatalogSnapshot:
        return self._source_catalog

    @property
    def item_workspace(self) -> ItemWorkspace:
        return self._item_workspace

    @property
    def prepared_assets(self) -> PreparedAssetRegistry:
        return self._workbench.prepared_registry

    @property
    def ontology_bundle(self) -> Any:
        return self._ontology_bundle

    @property
    def script_runner(self) -> "ControlledScriptRunner":
        if self._script_runner is None:
            self._script_runner = ControlledScriptRunner(
                self,
                default_timeout_seconds=float(self.runner_config["default_timeout_seconds"]),
                default_output_bytes=int(self.runner_config["default_output_bytes"]),
            )
        return self._script_runner

    def ensure_valid(self, *, final: bool = False, _transition_locked: bool = False) -> None:
        """Fail closed cheaply; use ``final=True`` at a freeze boundary."""

        _reconcile_transition_journal(
            self.manifest_path,
            run_id=self.context.run_id,
            item_id=self.item_workspace.item_id,
            _locked=_transition_locked,
        )
        if not self.manifest_path.is_file() or _sha256_file(self.manifest_path) != self._manifest_file_hash():
            raise ValueError("analysis context manifest changed")
        source_path = self.context.resolve_input(self.source_identity.uri)
        if not source_path.is_file():
            raise ValueError("analysis source changed after binding")
        expected_stat = self.source_identity.metadata.get("source_stat")
        if not isinstance(expected_stat, Mapping) or _source_stat_signature(source_path) != {
            str(key): int(value) for key, value in expected_stat.items()
        }:
            raise ValueError("analysis source changed after binding (archive changed; stat signature)")
        if not self.source_catalog.path.is_file() or _sha256_file(self.source_catalog.path) != self.source_catalog.content_hash:
            raise ValueError("analysis catalog changed after binding")
        if self.source_catalog.source_hash != self.source_identity.content_hash:
            raise ValueError("analysis source/catalog hash mismatch")
        if final:
            self._workbench.data_room.verify_source_full()

    def finalize_source_verification(self) -> None:
        """Re-hash source and catalog identity before final/freeze publication."""

        self.ensure_valid(final=True)

    def rebind_implementation(
        self,
        context: RunContext,
        item_workspace: ItemWorkspace,
        lifecycle: Any,
    ) -> "BoundAnalysisContext":
        """Serialize and publish one implementation transition.

        Lock order is journal -> run lifecycle -> item state.  Lifecycle and
        item mutators use their program-owned locks, so a terminal/attempt
        writer is ordered before or after this transition rather than racing
        its authoritative guard.
        """

        from .lifecycle import RunLifecycle

        if not isinstance(context, RunContext):
            raise TypeError("rebind_implementation requires one RunContext")
        if not isinstance(item_workspace, ItemWorkspace):
            raise TypeError("item_workspace must be an ItemWorkspace")
        if not isinstance(lifecycle, RunLifecycle):
            raise TypeError("lifecycle must be a RunLifecycle")
        with _transition_lock(self.manifest_path):
            with RunLifecycle._run_lock(context):
                with item_workspace._state_transition_lock():
                    return self._rebind_implementation_locked(
                        context,
                        item_workspace,
                        lifecycle,
                        _lifecycle_locked=True,
                        _item_lock_held=True,
                    )

    def _rebind_implementation_locked(
        self,
        context: RunContext,
        item_workspace: ItemWorkspace,
        lifecycle: Any,
        *,
        _lifecycle_locked: bool = False,
        _item_lock_held: bool = False,
    ) -> "BoundAnalysisContext":
        """Move this unaccepted context across an explicit implementation chain.

        The operation is deliberately narrow: it changes only implementation
        identity in the context manifest and keeps the already-bound source,
        physical inventory, catalog, runner limits, and item artifacts intact.
        A small intent/audit/state protocol makes each persistence boundary
        recoverable on the next load while hash-chain tampering fails closed.
        """

        from .lifecycle import RunLifecycle

        if not isinstance(context, RunContext):
            raise TypeError("rebind_implementation requires one RunContext")
        if not isinstance(item_workspace, ItemWorkspace):
            raise TypeError("item_workspace must be an ItemWorkspace")
        if not isinstance(lifecycle, RunLifecycle):
            raise TypeError("lifecycle must be a RunLifecycle")
        requested_item_id = item_workspace.item_id
        requested_mode = item_workspace.mode
        _reconcile_transition_journal(
            self.manifest_path,
            run_id=self.context.run_id,
            item_id=requested_item_id,
            _locked=True,
        )
        authoritative_lifecycle = (
            RunLifecycle._load_unlocked(context)
            if _lifecycle_locked
            else RunLifecycle.load(context)
        )
        authoritative_item = ItemWorkspace.load(
            context,
            requested_item_id,
            mode=requested_mode,
            telemetry=self.telemetry,
        )
        lifecycle = authoritative_lifecycle
        item_workspace = authoritative_item
        if context.run_id != self.context.run_id or context.run_root != self.context.run_root:
            raise ValueError("implementation rebind must stay within the same run")
        if item_workspace.context is not context or item_workspace.item_id != self.item_workspace.item_id or item_workspace.mode != self.item_workspace.mode:
            raise ValueError("implementation rebind item workspace identity does not match")
        if item_workspace.item_id not in lifecycle.item_ids:
            raise ValueError("implementation rebind item is not in the lifecycle")
        if lifecycle.state in {"complete", "complete_with_limits"}:
            raise ValueError("implementation rebind cannot run after terminal lifecycle")

        try:
            current_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("analysis context manifest is unreadable") from exc
        if not isinstance(current_manifest, Mapping):
            raise ValueError("analysis context manifest must be an object")
        current_manifest = dict(current_manifest)
        current_unsigned = dict(current_manifest)
        current_manifest_hash = current_unsigned.pop("manifest_hash", None)
        if current_manifest_hash != _sha256_bytes(_json_bytes(current_unsigned)):
            raise ValueError("analysis context manifest hash does not match")

        target_core = context.core_version
        target_skill = context.skill_version
        current_core = str(current_unsigned.get("core_version", ""))
        current_skill = current_unsigned.get("skill_version")
        current_skill = str(current_skill) if current_skill is not None else None
        if current_core == target_core and current_skill == target_skill:
            # A prior successful call (or crash recovery) is an exact retry.
            return load_bound_analysis_context(
                context,
                path=self.manifest_path,
                item_workspace=item_workspace,
                telemetry=self.telemetry,
                _transition_locked=True,
            )
        if current_core != self.context.core_version or current_skill != self.context.skill_version:
            raise ValueError("analysis context manifest implementation identity is not the caller's source")
        self.ensure_valid(_transition_locked=True)

        state = item_workspace.state
        if state.get("active_attempt_id") is not None:
            raise ValueError("implementation rebind requires no active attempt")
        if item_workspace._read_business_review() is not None:
            raise ValueError("implementation rebind requires no active business review")
        if state.get("review", {}).get("status") != "pending":
            raise ValueError("implementation rebind requires a pending review")
        if state.get("terminal_intent") is not None or state.get("terminal_outcome") is not None:
            raise ValueError("implementation rebind requires a nonterminal item")
        if state.get("lifecycle_state") in {"accepted", "technical_failure"} or item_workspace.accepted_root.exists():
            raise ValueError("implementation rebind cannot mutate an accepted item")
        if state.get("integration_state") != "pending" or state.get("integration_manifest_hash") is not None or state.get("integration_manifest_ref") is not None:
            raise ValueError("implementation rebind requires pending integration")

        transitions = tuple(lifecycle.implementation_transitions)
        if not transitions:
            raise ValueError("implementation rebind requires an implementation transition chain")

        def _version_pair(value: str) -> tuple[str, str]:
            raw = str(value)
            if "/" not in raw:
                raise ValueError("implementation transition version must be skill/core")
            skill, core = raw.split("/", 1)
            if not skill.startswith("skill") or not core.startswith("core") or len(skill) == 5 or len(core) == 4:
                raise ValueError("implementation transition version must be skill/core")
            return skill, core

        expected_old = (
            str(self.context.skill_version) if str(self.context.skill_version).startswith("skill") else f"skill{self.context.skill_version}",
            str(self.context.core_version) if str(self.context.core_version).startswith("core") else f"core{self.context.core_version}",
        )
        previous_new: tuple[str, str] | None = None
        preserved: Mapping[str, str] | None = None
        for index, transition in enumerate(transitions):
            old_pair = _version_pair(transition.old_version)
            new_pair = _version_pair(transition.new_version)
            if index == 0:
                if old_pair != expected_old:
                    raise ValueError("implementation transition chain does not start at bound identity")
                old_sha = current_unsigned.get("implementation_sha")
                old_tree = current_unsigned.get("implementation_tree")
                if old_sha is not None and old_sha != transition.old_sha:
                    raise ValueError("implementation transition old SHA does not match manifest")
                if old_tree is not None and old_tree != transition.old_tree:
                    raise ValueError("implementation transition old tree does not match manifest")
            elif old_pair != previous_new:
                raise ValueError("implementation transition version chain has a gap")
            if index and (transition.old_sha != transitions[index - 1].new_sha or transition.old_tree != transitions[index - 1].new_tree):
                raise ValueError("implementation transition repository identity chain has a gap")
            if transition.earliest_affected_item != item_workspace.item_id:
                raise ValueError("implementation transition earliest affected item does not match")
            candidate_preserved = dict(transition.preserved_accepted_hashes)
            if preserved is None:
                preserved = candidate_preserved
            elif candidate_preserved != dict(preserved):
                raise ValueError("implementation transition preserved accepted hashes changed")
            previous_new = new_pair
        expected_target = (
            str(target_skill) if str(target_skill).startswith("skill") else f"skill{target_skill}",
            str(target_core) if str(target_core).startswith("core") else f"core{target_core}",
        )
        if previous_new != expected_target:
            raise ValueError("implementation transition chain does not reach target identity")
        accepted_hashes = dict(preserved or {})
        if item_workspace.item_id in accepted_hashes:
            raise ValueError("implementation transition cannot preserve the active item")
        for accepted_item, expected_hash in accepted_hashes.items():
            accepted_path = context.resolve_run_path(Path("questions") / accepted_item / "accepted" / "manifest.json")
            if not accepted_path.is_file() or _sha256_file(accepted_path) != expected_hash:
                raise ValueError("implementation transition preserved accepted hash does not match disk")

        # Reload both authorities immediately before preparing the durable
        # intent.  A stale caller object (or a concurrent terminal/attempt
        # writer) can never authorize this commit from an earlier snapshot.
        latest_lifecycle = (
            RunLifecycle._load_unlocked(context)
            if _lifecycle_locked
            else RunLifecycle.load(context)
        )
        latest_item = ItemWorkspace.load(
            context,
            item_workspace.item_id,
            mode=item_workspace.mode,
            telemetry=self.telemetry,
        )
        if latest_lifecycle.implementation_transitions != transitions:
            raise ValueError("implementation transition ledger changed during rebind")
        if latest_item.state != state:
            raise ValueError("item workspace changed during implementation rebind")
        if latest_item._read_business_review() is not None or latest_item.accepted_root.exists():
            raise ValueError("item workspace became non-rebindable during implementation rebind")
        item_workspace = latest_item
        lifecycle = latest_lifecycle

        candidate_unsigned = dict(current_unsigned)
        candidate_unsigned["core_version"] = target_core
        candidate_unsigned["skill_version"] = target_skill
        candidate_unsigned["implementation_sha"] = transitions[-1].new_sha
        candidate_unsigned["implementation_tree"] = transitions[-1].new_tree
        candidate_manifest_hash = _sha256_bytes(_json_bytes(candidate_unsigned))
        candidate_manifest = {**candidate_unsigned, "manifest_hash": candidate_manifest_hash}
        before_manifest_hash = _manifest_file_digest(self.manifest_path)
        after_manifest_hash = _sha256_bytes(_manifest_bytes(candidate_manifest))
        audit_path, state_path, intent_path = _transition_paths(self.manifest_path)
        audit = _read_transition_audit(audit_path, run_id=self.context.run_id, item_id=item_workspace.item_id)
        anchor = _read_transition_state(state_path, run_id=self.context.run_id, item_id=item_workspace.item_id)
        if anchor is None:
            if audit:
                raise ValueError("analysis context transition audit has no state anchor")
            before_count, before_head = 0, None
        else:
            if anchor["audit_count"] != len(audit) or anchor["audit_head"] != (audit[-1]["record_hash"] if audit else None):
                raise ValueError("analysis context transition state anchor is invalid")
            if anchor["manifest_file_hash"] != before_manifest_hash:
                raise ValueError("analysis context transition state manifest hash is stale")
            before_count, before_head = anchor["audit_count"], anchor["audit_head"]
        transition = transitions[-1]
        record_unsigned = {
            "record_kind": "analysis_context_implementation_transition",
            "transition_id": transition.transition_id or f"{transition.old_version}->{transition.new_version}",
            "run_id": self.context.run_id,
            "item_id": item_workspace.item_id,
            "before_manifest_hash": before_manifest_hash,
            "after_manifest_hash": after_manifest_hash,
            "old_core_version": self.context.core_version,
            "new_core_version": target_core,
            "old_skill_version": self.context.skill_version,
            "new_skill_version": target_skill,
            "old_sha": transitions[0].old_sha,
            "new_sha": transition.new_sha,
            "old_tree": transitions[0].old_tree,
            "new_tree": transition.new_tree,
            "previous_hash": before_head,
        }
        record = {**record_unsigned, "record_hash": _transition_digest(record_unsigned)}
        operation_id = f"rebind-{record['record_hash'][:16]}"
        intent = {
            "record_kind": "analysis_context_implementation_transition_intent",
            "operation_id": operation_id,
            "run_id": self.context.run_id,
            "item_id": item_workspace.item_id,
            "audit_path": f"work/{_TRANSITION_AUDIT_FILENAME}",
            "state_path": f"work/{_TRANSITION_STATE_FILENAME}",
            "before_manifest_hash": before_manifest_hash,
            "after_manifest_hash": after_manifest_hash,
            "before_audit_count": before_count,
            "before_audit_head": before_head,
            "expected_audit": record,
            "candidate_manifest": candidate_manifest,
            "phase": "intent",
        }
        if intent_path.exists() or intent_path.is_symlink():
            existing = _read_transition_intent(intent_path, run_id=self.context.run_id, item_id=item_workspace.item_id)
            if existing != {**intent, "intent_hash": _transition_digest(intent)}:
                raise ValueError("conflicting implementation rebind intent exists")
        try:
            _write_transition_intent(intent_path, intent)
            _transition_failpoint("after_intent")
            _append_transition_record(audit_path, record)
            intent = {**intent, "phase": "audit_appended"}
            _write_transition_intent(intent_path, intent)
            _transition_failpoint("after_audit")
            _atomic_write_json_durable(self.manifest_path, candidate_manifest)
            intent = {**intent, "phase": "manifest_persisted"}
            _write_transition_intent(intent_path, intent)
            _transition_failpoint("after_manifest")
            _write_transition_state(
                state_path,
                run_id=self.context.run_id,
                item_id=item_workspace.item_id,
                count=before_count + 1,
                head=record["record_hash"],
                manifest_hash=after_manifest_hash,
            )
            intent = {**intent, "phase": "state_persisted"}
            _write_transition_intent(intent_path, intent)
            _transition_failpoint("after_state")
            _clear_transition_intent(intent_path)
        except Exception:
            # The intent remains for deterministic recovery on the next load;
            # no broad rollback can safely erase an already-fsynced boundary.
            raise
        return load_bound_analysis_context(
            context,
            path=self.manifest_path,
            item_workspace=item_workspace,
            telemetry=self.telemetry,
            _transition_locked=True,
        )

    def save_prepared_candidate(
        self,
        prepared_asset_id: str,
        rows: Iterable[Mapping[str, Any]] | DataRoomMember | DataRoomCatalogEntry | str | Path,
        **kwargs: Any,
    ) -> Any:
        """Materialize a mutable candidate below this item's work/prepared."""

        self.ensure_valid()
        candidate_root = self.item_workspace.work_root / "prepared"
        return self._workbench._save_prepared_candidate(
            prepared_asset_id,
            rows,
            candidate_root=candidate_root,
            **kwargs,
        )

    def _manifest_file_hash(self) -> str:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("analysis context manifest is unreadable") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("analysis context manifest must be an object")
        unsigned = dict(payload)
        actual = unsigned.pop("manifest_hash", None)
        expected = _sha256_bytes(_json_bytes(unsigned))
        if actual != expected or actual != self.manifest_hash:
            raise ValueError("analysis context manifest hash does not match")
        return _sha256_file(self.manifest_path)

    @classmethod
    def load(
        cls,
        context: RunContext | None = None,
        *,
        path: str | Path | None = None,
        item_workspace: ItemWorkspace | None = None,
        telemetry: Any = None,
    ) -> "BoundAnalysisContext":
        return load_bound_analysis_context(context, path=path, item_workspace=item_workspace, telemetry=telemetry)


def _manifest_path_for(
    context: RunContext | None,
    *,
    path: str | Path | None,
    item_workspace: ItemWorkspace | None,
) -> Path:
    selected = path or os.environ.get(ANALYSIS_CONTEXT_ENV)
    if selected is None:
        if item_workspace is None:
            raise ValueError(f"{ANALYSIS_CONTEXT_ENV} is not set and no item workspace was supplied")
        selected = item_workspace.work_root / _MANIFEST_FILENAME
    if context is None:
        raw = Path(selected).expanduser()
        if not raw.is_absolute():
            raise ValueError("an explicit context is required for relative manifest paths")
        if not raw.is_file() or raw.is_symlink():
            raise ValueError("analysis context manifest must be a regular file")
        return raw
    resolved = context.resolve_run_path(selected)
    return _regular_file(resolved, root=context.run_root, label="analysis context manifest")


def load_bound_analysis_context(
    context: RunContext | None = None,
    *,
    path: str | Path | None = None,
    item_workspace: ItemWorkspace | None = None,
    telemetry: Any = None,
    _transition_locked: bool = False,
) -> BoundAnalysisContext:
    """Load a hash-bound context from the environment or an explicit safe path."""

    if context is not None and not isinstance(context, RunContext):
        raise TypeError("load_bound_analysis_context requires one RunContext")
    manifest_path = _manifest_path_for(context, path=path, item_workspace=item_workspace)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("analysis context manifest is unreadable") from exc
    if not isinstance(manifest, Mapping):
        raise ValueError("analysis context manifest must be an object")
    # Reconcile a crash-persisted rebind before interpreting implementation
    # identity.  The hints are used only to select the already-bound journal;
    # the full manifest hash and schema checks below remain authoritative.
    hinted_run_id = manifest.get("run_id")
    hinted_item_id = manifest.get("item_id")
    if isinstance(hinted_run_id, str) and isinstance(hinted_item_id, str) and hinted_run_id and hinted_item_id:
        _reconcile_transition_journal(
            manifest_path,
            run_id=hinted_run_id,
            item_id=hinted_item_id,
            _locked=_transition_locked,
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("analysis context manifest is unreadable") from exc
        if not isinstance(manifest, Mapping):
            raise ValueError("analysis context manifest must be an object")
    unsigned = dict(manifest)
    manifest_hash = unsigned.pop("manifest_hash", None)
    if not isinstance(manifest_hash, str) or manifest_hash != _sha256_bytes(_json_bytes(unsigned)):
        raise ValueError("analysis context manifest hash does not match")
    if unsigned.get("schema_version") != ANALYSIS_CONTEXT_SCHEMA_VERSION or unsigned.get("kind") != "bound_analysis_context":
        raise ValueError("analysis context manifest schema is unsupported")
    if context is None:
        run_root = unsigned.get("run_root")
        input_roots = unsigned.get("input_roots", ())
        if not isinstance(run_root, str) or not isinstance(input_roots, list) or any(not isinstance(root, str) for root in input_roots):
            raise ValueError("analysis context roots are invalid")
        context = RunContext(
            str(unsigned.get("run_id", "")),
            run_root,
            tuple(input_roots),
            core_version=str(unsigned.get("core_version", "")),
            skill_version=unsigned.get("skill_version"),
        )
        manifest_path = _regular_file(
            context.resolve_run_path(manifest_path),
            root=context.run_root,
            label="analysis context manifest",
        )
    if (
        unsigned.get("run_id") != context.run_id
        or unsigned.get("core_version") != context.core_version
        or unsigned.get("skill_version") != context.skill_version
    ):
        raise ValueError("analysis context run/core identity does not match")
    item_id = unsigned.get("item_id")
    item_mode = unsigned.get("item_mode", "question")
    if not isinstance(item_id, str) or not item_id:
        raise ValueError("analysis context item identity is invalid")
    if item_workspace is None:
        item_workspace = ItemWorkspace.load(context, item_id, mode=item_mode, telemetry=telemetry)
    if item_workspace.context is not context or item_workspace.item_id != item_id or item_workspace.mode != item_mode:
        raise ValueError("analysis context item workspace identity does not match")
    expected_manifest = item_workspace.work_root / _MANIFEST_FILENAME
    if manifest_path != expected_manifest:
        raise ValueError("analysis context manifest is not inside the bound item workspace")
    source_payload = unsigned.get("source_identity")
    if not isinstance(source_payload, Mapping):
        raise ValueError("analysis source identity is missing")
    source_identity = DataAssetRef.from_dict(source_payload)
    if not source_identity.content_hash:
        raise ValueError("analysis source identity must contain a content hash")
    source_path = context.resolve_input(source_identity.uri)
    if not source_path.is_file():
        raise ValueError("analysis source changed after context binding")
    inventory_payload = unsigned.get("physical_inventory")
    if not isinstance(inventory_payload, Mapping):
        raise ValueError("analysis physical inventory binding is missing")
    raw_members = inventory_payload.get("members")
    inventory_hash = inventory_payload.get("inventory_hash")
    source_stat = inventory_payload.get("source_stat")
    central_fingerprint = inventory_payload.get("central_directory_fingerprint")
    if (
        not isinstance(raw_members, list)
        or not isinstance(inventory_hash, str)
        or not isinstance(source_stat, Mapping)
        or not isinstance(central_fingerprint, Mapping)
        or inventory_hash != _sha256_bytes(_json_bytes(raw_members))
    ):
        raise ValueError("analysis physical inventory binding is invalid")
    normalized_stat = {str(key): int(value) for key, value in source_stat.items()}
    if _source_stat_signature(source_path) != normalized_stat:
        raise ValueError("analysis source changed after context binding (stat signature)")
    try:
        bound_members = tuple(DataRoomMember.from_dict(value) for value in raw_members)
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError("analysis physical inventory members are invalid") from exc
    if source_identity.metadata.get("source_stat") != normalized_stat:
        raise ValueError("analysis source/inventory stat binding does not match")
    normalized_central_fingerprint = {str(key): value for key, value in central_fingerprint.items()}
    if source_identity.metadata.get("central_directory_fingerprint") != normalized_central_fingerprint:
        raise ValueError("analysis source/inventory central-directory binding does not match")
    runner_config = unsigned.get("runner_config")
    if not isinstance(runner_config, Mapping):
        raise ValueError("analysis runner config is missing")
    try:
        normalized_runner_config = {
            "default_timeout_seconds": float(runner_config["default_timeout_seconds"]),
            "default_output_bytes": int(runner_config["default_output_bytes"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("analysis runner config is invalid") from exc
    if normalized_runner_config["default_timeout_seconds"] <= 0 or normalized_runner_config["default_output_bytes"] <= 0:
        raise ValueError("analysis runner config must be positive")
    catalog_payload = unsigned.get("catalog")
    if not isinstance(catalog_payload, Mapping):
        raise ValueError("analysis catalog binding is missing")
    catalog_core = str(catalog_payload.get("core_version", ""))
    transition_audit = _transition_paths(manifest_path)[0]
    transition_records = _read_transition_audit(
        transition_audit,
        run_id=context.run_id,
        item_id=item_id,
    )
    rebound_catalog = bool(transition_records)
    reused_entries: tuple[DataRoomCatalogEntry, ...] | None = None
    reused_catalog_path: Path | None = None
    reused_catalog_key: str | None = None
    if rebound_catalog or catalog_core != context.core_version:
        if (
            not transition_records
            or transition_records[0]["old_core_version"] != catalog_core
            or transition_records[-1]["new_core_version"] != context.core_version
            or transition_records[-1]["new_skill_version"] != str(context.skill_version)
        ):
            raise ValueError("analysis catalog implementation transition is not recorded")
        raw_catalog_path = catalog_payload.get("path")
        if not isinstance(raw_catalog_path, str) or not raw_catalog_path:
            raise ValueError("analysis catalog path is invalid")
        reused_catalog_path = _regular_file(
            context.resolve_run_path(raw_catalog_path),
            root=context.run_root,
            label="analysis catalog",
        )
        if _sha256_file(reused_catalog_path) != catalog_payload.get("content_hash"):
            raise ValueError("analysis catalog content hash does not match")
        try:
            persisted_catalog = json.loads(reused_catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("analysis catalog is unreadable") from exc
        if not isinstance(persisted_catalog, Mapping):
            raise ValueError("analysis catalog must be an object")
        if (
            persisted_catalog.get("catalog_schema_version") != catalog_payload.get("catalog_schema_version")
            or persisted_catalog.get("catalog_key") != catalog_payload.get("catalog_key")
            or persisted_catalog.get("source_hash") != source_identity.content_hash
            or persisted_catalog.get("core_version") != catalog_core
        ):
            raise ValueError("analysis catalog persisted identity does not match binding")
        raw_entries = persisted_catalog.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("analysis catalog entries are invalid")
        try:
            reused_entries = tuple(DataRoomCatalogEntry.from_dict(value) for value in raw_entries)
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("analysis catalog entries are invalid") from exc
        reused_catalog_key = str(catalog_payload.get("catalog_key", ""))
        if not reused_catalog_key:
            raise ValueError("analysis catalog key is invalid")
    workbench = DataRoomWorkbench(
        context,
        source_identity,
        telemetry=telemetry,
        _bound_members=bound_members,
        _bound_archive_hash=source_identity.content_hash,
        _bound_source_stat=normalized_stat,
        _bound_central_directory_fingerprint=normalized_central_fingerprint,
        _bound_catalog_entries=reused_entries,
        _bound_catalog_path=reused_catalog_path,
        _bound_catalog_key=reused_catalog_key,
    )
    if reused_entries is None:
        snapshot = CatalogSnapshot.from_workbench(workbench)
    else:
        for entry in reused_entries:
            workbench.data_room._resolve_member(entry.member)
        raw_counts = persisted_catalog.get("counts")
        if not isinstance(raw_counts, Mapping):
            raise ValueError("analysis catalog counts are invalid")
        try:
            counts = CatalogCounts(
                archive_members=raw_counts["archive_members"],
                catalog_entries=raw_counts["catalog_entries"],
                table_members=raw_counts["table_members"],
                sheet_entries=raw_counts["sheet_entries"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("analysis catalog counts are invalid") from exc
        if counts != workbench.data_room.catalog_counts(reused_entries):
            raise ValueError("analysis catalog counts do not match entries")
        snapshot = CatalogSnapshot(
            path=reused_catalog_path,
            content_hash=str(catalog_payload["content_hash"]),
            catalog_key=reused_catalog_key,
            catalog_schema_version=str(catalog_payload.get("catalog_schema_version", "")),
            source_hash=source_identity.content_hash or "",
            core_version=catalog_core,
            entries=reused_entries,
            counts=counts,
        )
    if (
        str(catalog_payload.get("path")) != str(snapshot.path)
        or catalog_payload.get("content_hash") != snapshot.content_hash
        or catalog_payload.get("catalog_key") != snapshot.catalog_key
        or catalog_payload.get("source_hash") != snapshot.source_hash
    ):
        raise ValueError("analysis catalog binding does not match")
    bound = BoundAnalysisContext(
        context=context,
        source_identity=source_identity,
        workbench=workbench,
        source_catalog=snapshot,
        item_workspace=item_workspace,
        ontology_bundle=unsigned.get("ontology_bundle", ()),
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        runner_config=normalized_runner_config,
        telemetry=telemetry,
    )
    bound._script_runner = ControlledScriptRunner(
        bound,
        default_timeout_seconds=normalized_runner_config["default_timeout_seconds"],
        default_output_bytes=normalized_runner_config["default_output_bytes"],
    )
    bound.ensure_valid(_transition_locked=_transition_locked)
    return bound


def _module_names(source: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return tuple(dict.fromkeys(names))


def _module_available(module: str, script_path: Path) -> bool:
    """Check imports without executing them, including local script helpers."""

    try:
        if importlib.util.find_spec(module) is not None:
            return True
    except (ImportError, ModuleNotFoundError, ValueError):
        pass
    local_file = script_path.parent / f"{module}.py"
    local_package = script_path.parent / module / "__init__.py"
    return local_file.is_file() or local_package.is_file()


def _exception_from_text(stderr: str) -> tuple[str | None, str | None]:
    for name in _SAME_ATTEMPT_ERRORS:
        if f"{name}:" in stderr or stderr.rstrip().endswith(name):
            return name, "same_attempt_feedback"
    return None, None


class ControlledScriptRunner:
    """Run one exact script path under the bound item workspace."""

    def __init__(
        self,
        analysis_context: BoundAnalysisContext,
        *,
        python_executable: str | Path | None = None,
        default_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        default_output_bytes: int = _DEFAULT_OUTPUT_BYTES,
    ) -> None:
        if not isinstance(analysis_context, BoundAnalysisContext):
            raise TypeError("ControlledScriptRunner requires a BoundAnalysisContext")
        self.analysis_context = analysis_context
        self.python_executable = str(python_executable or sys.executable)
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.default_output_bytes = int(default_output_bytes)
        if self.default_timeout_seconds <= 0 or self.default_output_bytes <= 0:
            raise ValueError("runner bounds must be positive")

    @property
    def context(self) -> BoundAnalysisContext:
        return self.analysis_context

    def _script_path(self, script: str | Path) -> Path:
        work = self.context.item_workspace.work_root
        raw = Path(script)
        candidate = raw if raw.is_absolute() else work / raw
        _assert_no_symlink_components(candidate, root=work)
        return _regular_file(candidate, root=work, label="analysis script")

    def _output_paths(self, outputs: Iterable[str | Path]) -> tuple[Path, ...]:
        item_root = self.context.item_workspace.item_root
        work_root = self.context.item_workspace.work_root
        result: list[Path] = []
        for value in outputs:
            raw = Path(value)
            if raw.is_absolute():
                candidate = raw
            elif raw.parts and raw.parts[0] in {"work", "questions", "requirements"}:
                candidate = item_root / raw if raw.parts[0] == "work" else self.context.context.resolve_run_path(raw)
            else:
                candidate = work_root / raw
            _assert_no_symlink_components(candidate, root=work_root)
            if not candidate.is_relative_to(work_root):
                raise AllowedRootError(f"script output escapes item work: {candidate}")
            if candidate.exists() and candidate.is_symlink():
                raise AllowedRootError(f"script output cannot be a symlink: {candidate}")
            if candidate.exists() and not candidate.is_file():
                raise ValueError(f"script output must be a file: {candidate}")
            result.append(candidate)
        return tuple(dict.fromkeys(result))

    def _environment(self, phase: str, sample_limit: int, *, output_root: Path) -> dict[str, str]:
        # Do not leak the parent process's credentials or unrelated service
        # configuration into an analyst script.  The child needs only normal
        # Python lookup/localization/temp settings plus the explicit bindings
        # below.  Host/container isolation remains a separate responsibility.
        env: dict[str, str] = {}
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP"):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env[ANALYSIS_CONTEXT_ENV] = str(self.context.manifest_path)
        env[ANALYSIS_PHASE_ENV] = phase
        env[ANALYSIS_SAMPLE_LIMIT_ENV] = str(sample_limit)
        env[ANALYSIS_OUTPUT_ROOT_ENV] = str(output_root)
        source_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source_root if not existing_pythonpath else source_root + os.pathsep + existing_pythonpath
        return env

    @staticmethod
    def _regular_files(root: Path) -> set[Path]:
        if not root.exists():
            return set()
        return {path for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

    @classmethod
    def _snapshot_workspace(cls, root: Path) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in cls._regular_files(root)}

    @classmethod
    def _workspace_paths(cls, root: Path) -> set[Path]:
        if not root.exists():
            return set()
        return {
            path
            for path in root.rglob("*")
            if (path.is_file() or path.is_symlink())
        }

    @classmethod
    def _workspace_violations(
        cls,
        root: Path,
        snapshot: Mapping[Path, bytes],
        allowed: Sequence[Path],
        *,
        ignored: Sequence[Path] = (),
    ) -> tuple[Path, ...]:
        before = set(snapshot)
        current = cls._workspace_paths(root)
        changed = {
            path
            for path in before & current
            if path.is_file() and not path.is_symlink() and path.read_bytes() != snapshot[path]
        }
        changed.update(path for path in before & current if path.is_symlink())
        violations = (current - before) | (before - current) | changed
        excluded = set(allowed) | set(ignored)
        return tuple(sorted(path for path in violations if path not in excluded))

    @staticmethod
    def _restore_workspace(root: Path, snapshot: Mapping[Path, bytes]) -> None:
        current = {
            path
            for path in root.rglob("*")
            if path.is_file() or path.is_symlink()
        } if root.exists() else set()
        for path in sorted(current - set(snapshot), key=lambda value: len(value.parts), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
        for path, content in snapshot.items():
            if path.exists() and (path.is_symlink() or not path.is_file()):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            ControlledScriptRunner._atomic_write_bytes(path, content)

    @staticmethod
    def _remove_bytecode(root: Path) -> bool:
        found = False
        if not root.exists():
            return False
        for path in tuple(root.rglob("*.pyc")):
            if path.is_file() and not path.is_symlink():
                found = True
                path.unlink(missing_ok=True)
        for path in sorted(tuple(root.rglob("__pycache__")), key=lambda value: len(value.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                found = True
                shutil.rmtree(path, ignore_errors=True)
        return found

    @staticmethod
    def _best_effort_stop(process: Any) -> None:
        """Stop a process after a transport error without masking that error."""

        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=1.0)
            return
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=1.0)
        except Exception:
            pass

    def _write_receipt(self, receipt: ScriptExecutionReceipt) -> ScriptExecutionReceipt:
        receipt_dir = self.context.item_workspace.work_root / _RECEIPT_DIR
        _assert_no_symlink_components(receipt_dir, root=self.context.item_workspace.item_root)
        receipt_path = receipt_dir / f"{receipt.receipt_id}.json"
        _atomic_write_json(receipt_path, {**receipt.to_dict(), "receipt_path": str(receipt_path)})
        return ScriptExecutionReceipt(**{**receipt.to_dict(), "receipt_path": str(receipt_path)})

    def _failure_receipt(
        self,
        *,
        phase: str,
        script_path: Path,
        script_hash: str | None,
        started: str,
        start_mono: float,
        error_type: str,
        error_category: str,
        traceback: str,
        stderr: str = "",
    ) -> ScriptExecutionReceipt:
        receipt = ScriptExecutionReceipt(
            receipt_id=f"receipt-{uuid.uuid4().hex}",
            phase=phase,
            script_path=str(script_path),
            script_hash=script_hash,
            context_path=str(self.context.manifest_path),
            context_hash=self.context.manifest_hash,
            source_hash=self.context.source_identity.content_hash or "",
            started_at=started,
            finished_at=_utc_now(),
            wall_seconds=max(0.0, time.monotonic() - start_mono),
            exit_code=None,
            stderr=stderr,
            error_type=error_type,
            error_category=error_category,
            traceback=traceback,
        )
        return self._write_receipt(receipt)

    def _compile_and_check(self, script_path: Path) -> tuple[str | None, ScriptExecutionReceipt | None]:
        started = _utc_now()
        start_mono = time.monotonic()
        script_hash: str | None = None
        try:
            source_bytes = script_path.read_bytes()
            script_hash = _sha256_bytes(source_bytes)
            source = source_bytes.decode("utf-8")
            ast.parse(source, filename=str(script_path))
        except SyntaxError as exc:
            return None, self._failure_receipt(
                phase="compile",
                script_path=script_path,
                script_hash=script_hash,
                started=started,
                start_mono=start_mono,
                error_type="SyntaxError",
                error_category="same_attempt_feedback",
                traceback=str(exc),
            )
        except (OSError, UnicodeDecodeError) as exc:
            return None, self._failure_receipt(
                phase="compile",
                script_path=script_path,
                script_hash=script_hash,
                started=started,
                start_mono=start_mono,
                error_type=type(exc).__name__,
                error_category="same_attempt_feedback",
                traceback=str(exc),
            )
        missing: list[str] = []
        for module in _module_names(source):
            try:
                if not _module_available(module, script_path):
                    missing.append(module)
            except (ImportError, ModuleNotFoundError, ValueError):
                missing.append(module)
        if missing:
            return None, self._failure_receipt(
                phase="dependency_check",
                script_path=script_path,
                script_hash=script_hash,
                started=started,
                start_mono=start_mono,
                error_type="ModuleNotFoundError",
                error_category="same_attempt_feedback",
                traceback="missing dependencies: " + ", ".join(sorted(missing)),
            )
        return script_hash, None

    def execute(
        self,
        script: str | Path,
        *,
        phase: str = "full",
        sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
        allowed_outputs: Iterable[str | Path] = (),
        timeout_seconds: float | None = None,
        output_bytes: int | None = None,
        _cwd: Path | None = None,
        _output_root: Path | None = None,
    ) -> ScriptExecutionReceipt:
        """Compile/check and execute one script phase without shell access."""

        if phase not in _VALID_PHASES:
            raise ValueError("phase must be smoke or full")
        if sample_limit < 0:
            raise ValueError("sample_limit cannot be negative")
        timeout = self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        cap = self.default_output_bytes if output_bytes is None else int(output_bytes)
        if timeout <= 0 or cap <= 0:
            raise ValueError("timeout_seconds and output_bytes must be positive")
        outputs = self._output_paths(allowed_outputs)
        self.context.ensure_valid()
        script_path = self._script_path(script)
        output_snapshot = self._snapshot_outputs(outputs)
        cwd = self.context.item_workspace.work_root if _cwd is None else _cwd
        output_root = self.context.item_workspace.work_root if _output_root is None else _output_root
        _assert_no_symlink_components(cwd, root=self.context.item_workspace.work_root)
        if not cwd.is_dir():
            cwd.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_components(output_root, root=self.context.item_workspace.work_root)
        if not output_root.is_dir():
            output_root.mkdir(parents=True, exist_ok=True)
        script_hash, preflight = self._compile_and_check(script_path)
        if preflight is not None:
            return preflight
        started = _utc_now()
        start_mono = time.monotonic()
        receipt_id = f"receipt-{uuid.uuid4().hex}"
        temp_dir = self.context.item_workspace.work_root / ".analysis-run"
        _assert_no_symlink_components(temp_dir, root=self.context.item_workspace.item_root)
        temp_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = temp_dir / f"{receipt_id}.stdout"
        stderr_path = temp_dir / f"{receipt_id}.stderr"
        timed_out = False
        output_limited = False
        exit_code: int | None = None
        subprocess_error: OSError | None = None
        process: Any | None = None
        # Capture the complete pre-child state before creating the temporary
        # stdout/stderr files.  If process creation itself raises after a
        # child-side mutation (or a test double simulates one), the common
        # cleanup below still has a complete rollback point.
        workspace_snapshot = self._snapshot_workspace(self.context.item_workspace.work_root)
        try:
            with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
                try:
                    process = subprocess.Popen(
                        [self.python_executable, str(script_path)],
                        cwd=str(cwd),
                        env=self._environment(phase, sample_limit, output_root=output_root),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                        shell=False,
                    )
                    while process.poll() is None:
                        if time.monotonic() - start_mono > timeout:
                            timed_out = True
                            process.terminate()
                            try:
                                process.wait(timeout=1.0)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=1.0)
                            break
                        try:
                            if stdout_path.stat().st_size + stderr_path.stat().st_size > cap:
                                output_limited = True
                                process.terminate()
                                try:
                                    process.wait(timeout=1.0)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=1.0)
                                break
                        except FileNotFoundError:
                            pass
                        time.sleep(0.01)
                    if process.poll() is None:
                        process.wait(timeout=1.0)
                    exit_code = process.returncode
                except OSError as exc:
                    subprocess_error = exc
                    if process is not None:
                        self._best_effort_stop(process)
                        try:
                            exit_code = process.returncode
                        except Exception:
                            exit_code = None
        except OSError as exc:
            subprocess_error = exc
            if process is not None:
                self._best_effort_stop(process)
                try:
                    exit_code = process.returncode
                except Exception:
                    exit_code = None
        stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
        stderr_bytes = stderr_path.read_bytes() if stderr_path.exists() else b""
        if len(stdout_bytes) + len(stderr_bytes) > cap:
            output_limited = True
        stdout_truncated = len(stdout_bytes) > cap
        stderr_truncated = len(stderr_bytes) > cap
        stdout = stdout_bytes[:cap].decode("utf-8", errors="replace")
        stderr = stderr_bytes[:cap].decode("utf-8", errors="replace")
        error_type: str | None = None
        error_category: str | None = None
        traceback_text: str | None = None
        context_error: Exception | None = None
        try:
            self.context.ensure_valid()
        except Exception as exc:  # fail closed after a child-side mutation
            context_error = exc
        bytecode_violation = self._remove_bytecode(self.context.context.run_root)
        undeclared_outputs = self._workspace_violations(
            self.context.item_workspace.work_root,
            workspace_snapshot,
            outputs,
            ignored=(stdout_path, stderr_path),
        )
        if context_error is not None:
            error_type, error_category, traceback_text = type(context_error).__name__, "context_integrity_failure", str(context_error)
        elif bytecode_violation:
            error_type, error_category, traceback_text = "BytecodeArtifact", "same_attempt_feedback", "bytecode artifacts are forbidden"
        elif undeclared_outputs:
            error_type, error_category = "UndeclaredOutput", "same_attempt_feedback"
            traceback_text = "undeclared outputs: " + ", ".join(str(path.relative_to(output_root)) for path in undeclared_outputs)
        elif timed_out:
            error_type, error_category, traceback_text = "TimeoutExpired", "runtime_timeout", "script timed out"
        elif output_limited:
            error_type, error_category, traceback_text = "OutputLimitExceeded", "runtime_output_limit", "script output exceeded cap"
        elif subprocess_error is not None:
            error_type, error_category, traceback_text = (
                type(subprocess_error).__name__,
                "same_attempt_feedback",
                str(subprocess_error),
            )
        elif exit_code not in (0, None):
            error_type, error_category = _exception_from_text(stderr)
            traceback_text = stderr or None
            if error_category is None:
                error_type, error_category = "ScriptError", "script_failure"
        if error_category is not None:
            self._restore_workspace(self.context.item_workspace.work_root, workspace_snapshot)
            self._restore_outputs(output_snapshot)
        output_hashes = self._hash_outputs(outputs)
        receipt = ScriptExecutionReceipt(
            receipt_id=receipt_id,
            phase=phase,
            script_path=str(script_path),
            script_hash=script_hash,
            context_path=str(self.context.manifest_path),
            context_hash=self.context.manifest_hash,
            source_hash=self.context.source_identity.content_hash or "",
            started_at=started,
            finished_at=_utc_now(),
            wall_seconds=max(0.0, time.monotonic() - start_mono),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=timed_out,
            output_limited=output_limited,
            error_type=error_type,
            error_category=error_category,
            traceback=traceback_text,
            output_hashes=output_hashes,
        )
        return self._write_receipt(receipt)

    def _hash_outputs(
        self,
        outputs: Sequence[Path],
        *,
        require_all: bool = False,
    ) -> dict[str, str]:
        if require_all:
            contents = self._read_outputs(outputs)
        else:
            contents = {}
            for path in outputs:
                if path.exists():
                    _regular_file(path, root=self.context.item_workspace.work_root, label="script output")
                    contents[path] = path.read_bytes()
        return {str(path): _sha256_bytes(content) for path, content in contents.items()}

    def _read_outputs(self, outputs: Sequence[Path]) -> dict[Path, bytes]:
        """Read and validate every declared output before publication.

        Reading the complete scratch set first is important: a later missing
        or symlinked output must not be discovered after an earlier target has
        already been replaced.
        """

        result: dict[Path, bytes] = {}
        work_root = self.context.item_workspace.work_root
        for path in outputs:
            _assert_no_symlink_components(path, root=work_root)
            _regular_file(path, root=work_root, label="script output")
            result[path] = path.read_bytes()
        return result

    def _scratch_paths(self, outputs: Sequence[Path], scratch_root: Path) -> tuple[Path, ...]:
        work_root = self.context.item_workspace.work_root
        _assert_no_symlink_components(scratch_root, root=work_root)
        return tuple(scratch_root / path.relative_to(work_root) for path in outputs)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _snapshot_outputs(outputs: Sequence[Path]) -> dict[Path, bytes | None]:
        snapshot: dict[Path, bytes | None] = {}
        for path in outputs:
            snapshot[path] = path.read_bytes() if path.is_file() else None
        return snapshot

    @staticmethod
    def _restore_outputs(snapshot: Mapping[Path, bytes | None]) -> None:
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def _materialize_outputs(
        self,
        source_paths: Sequence[Path],
        target_paths: Sequence[Path],
        *,
        snapshot: Mapping[Path, bytes | None] | None = None,
    ) -> None:
        """Publish a complete scratch set or restore every target.

        All sources are read and all target paths are checked before the first
        atomic replacement.  If any replacement fails, the supplied pre-run
        snapshot is restored (or a local snapshot is taken when called
        directly), so no partial output can survive a normal script failure.
        """

        if len(source_paths) != len(target_paths):
            raise ValueError("source and target output declarations differ")
        source_bytes = self._read_outputs(source_paths)
        work_root = self.context.item_workspace.work_root
        target_snapshot = dict(snapshot) if snapshot is not None else self._snapshot_outputs(target_paths)
        for target in target_paths:
            _assert_no_symlink_components(target, root=work_root)
            if target.exists() and (target.is_symlink() or not target.is_file()):
                raise ValueError(f"script output target must be a regular file: {target}")
        try:
            for source, target in zip(source_paths, target_paths):
                self._atomic_write_bytes(target, source_bytes[source])
        except Exception:
            self._restore_outputs(target_snapshot)
            raise

    def run_pipeline(
        self,
        script: str | Path,
        *,
        allowed_outputs: Iterable[str | Path] = (),
        deterministic_outputs: Iterable[str | Path] | Mapping[str | Path, str] = (),
        sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
        timeout_seconds: float | None = None,
        output_bytes: int | None = None,
    ) -> ScriptRunReport:
        """Run compile/dependency, smoke, full, and optional deterministic rerun."""

        outputs = self._output_paths(allowed_outputs)
        expected_hashes: dict[str, str] = {}
        if isinstance(deterministic_outputs, Mapping):
            expected_hashes = {str(path): str(value) for path, value in deterministic_outputs.items()}
            deterministic_values: Iterable[str | Path] = deterministic_outputs.keys()
        else:
            deterministic_values = deterministic_outputs
        deterministic = self._output_paths(deterministic_values)
        all_outputs = tuple(dict.fromkeys((*outputs, *deterministic)))
        self.context.ensure_valid()
        script_path = self._script_path(script)
        script_hash, preflight = self._compile_and_check(script_path)
        if preflight is not None:
            return ScriptRunReport(
                status="failed",
                same_attempt_feedback=preflight.error_category == "same_attempt_feedback",
                receipts=(preflight,),
                error_category=preflight.error_category,
                error_type=preflight.error_type,
            )
        snapshot = self._snapshot_outputs(all_outputs)
        smoke = self.execute(
            script_path,
            phase="smoke",
            sample_limit=sample_limit,
            allowed_outputs=outputs,
            timeout_seconds=timeout_seconds,
            output_bytes=output_bytes,
        )
        receipts: list[ScriptExecutionReceipt] = [smoke]
        smoke_ok = smoke.exit_code == 0 and not smoke.timed_out and not smoke.output_limited and smoke.error_category is None
        if not smoke_ok:
            self._restore_outputs(snapshot)
            return ScriptRunReport(
                status="failed",
                same_attempt_feedback=True,
                receipts=tuple(receipts),
                output_hashes=smoke.output_hashes,
                error_category=smoke.error_category,
                error_type=smoke.error_type,
            )
        deterministic_match: bool | None = None
        output_hashes: dict[str, str] = {}
        if not deterministic:
            full = self.execute(
                script_path,
                phase="full",
                sample_limit=sample_limit,
                allowed_outputs=outputs,
                timeout_seconds=timeout_seconds,
                output_bytes=output_bytes,
            )
            receipts.append(full)
            full_ok = full.exit_code == 0 and not full.timed_out and not full.output_limited and full.error_category is None
            if not full_ok:
                self._restore_outputs(snapshot)
                return ScriptRunReport(
                    status="failed",
                    same_attempt_feedback=True,
                    receipts=tuple(receipts),
                    output_hashes=full.output_hashes,
                    error_category=full.error_category,
                    error_type=full.error_type,
                )
            output_hashes = dict(full.output_hashes)
        if deterministic:
            # Full deterministic executions happen in disposable runner-owned
            # directories.  Nothing is copied into the item work tree until
            # both runs have succeeded and their declared hashes agree.
            scratch_base = self.context.item_workspace.work_root / ".analysis-run"
            first_root = scratch_base / f"deterministic-{uuid.uuid4().hex}-first"
            second_root = scratch_base / f"deterministic-{uuid.uuid4().hex}-second"
            first_root.mkdir(parents=True, exist_ok=False)
            second_root.mkdir(parents=True, exist_ok=False)
            first_all = self._scratch_paths(all_outputs, first_root)
            second_all = self._scratch_paths(all_outputs, second_root)
            first_det = self._scratch_paths(deterministic, first_root)
            second_det = self._scratch_paths(deterministic, second_root)
            try:
                # The ordinary full run above is retained as the first
                # receipt/validation pass; deterministic comparison itself is
                # isolated and therefore cannot overwrite an accepted output.
                first = self.execute(
                    script_path,
                    phase="full",
                    sample_limit=sample_limit,
                    allowed_outputs=first_all,
                    timeout_seconds=timeout_seconds,
                    output_bytes=output_bytes,
                    _cwd=first_root,
                    _output_root=first_root,
                )
                receipts.append(first)
                first_ok = first.exit_code == 0 and not first.timed_out and not first.output_limited and first.error_category is None
                first_hashes: dict[str, str] = {}
                first_output_error: Exception | None = None
                if first_ok:
                    try:
                        first_contents = self._read_outputs(first_all)
                        first_hashes = {
                            str(path): _sha256_bytes(first_contents[path])
                            for path in first_det
                        }
                    except Exception as exc:
                        first_output_error = exc
                if not first_ok or first_output_error is not None or len(first_hashes) != len(first_det):
                    self._restore_outputs(snapshot)
                    return ScriptRunReport(
                        status="failed",
                        same_attempt_feedback=True,
                        receipts=tuple(receipts),
                        output_hashes={},
                        deterministic_match=False,
                        error_category=first.error_category or "deterministic_output_missing",
                        error_type=first.error_type or (type(first_output_error).__name__ if first_output_error else None),
                    )
                shutil.rmtree(first_root, ignore_errors=True)
                second = self.execute(
                    script_path,
                    phase="full",
                    sample_limit=sample_limit,
                    allowed_outputs=second_all,
                    timeout_seconds=timeout_seconds,
                    output_bytes=output_bytes,
                    _cwd=second_root,
                    _output_root=second_root,
                )
                receipts.append(second)
                second_ok = second.exit_code == 0 and not second.timed_out and not second.output_limited and second.error_category is None
                second_hashes: dict[str, str] = {}
                second_output_error: Exception | None = None
                if second_ok:
                    try:
                        second_contents = self._read_outputs(second_all)
                        second_hashes = {
                            str(path): _sha256_bytes(second_contents[path])
                            for path in second_det
                        }
                    except Exception as exc:
                        second_output_error = exc
                # Compare by declared relative path, not scratch directory.
                first_relative = {str(path.relative_to(first_root)): first_hashes[str(path)] for path in first_det}
                second_relative = {str(path.relative_to(second_root)): second_hashes[str(path)] for path in second_det}
                deterministic_match = second_ok and second_output_error is None and first_relative == second_relative
                if deterministic_match and expected_hashes:
                    normalized_expected = {
                        str(self._output_paths((key,))[0]): value for key, value in expected_hashes.items()
                    }
                    deterministic_match = all(
                        second_hashes.get(str(second_path)) == expected
                        for final_path, expected in normalized_expected.items()
                        for second_path in (second_root / Path(final_path).relative_to(self.context.item_workspace.work_root),)
                    )
                if not deterministic_match:
                    self._restore_outputs(snapshot)
                    return ScriptRunReport(
                        status="failed",
                        same_attempt_feedback=True,
                        receipts=tuple(receipts),
                        output_hashes={},
                        deterministic_match=False,
                        error_category=second.error_category or ("deterministic_output_missing" if second_output_error else "deterministic_mismatch"),
                        error_type=second.error_type or (type(second_output_error).__name__ if second_output_error else None),
                    )
                try:
                    self._materialize_outputs(second_all, all_outputs, snapshot=snapshot)
                except Exception as exc:
                    self._restore_outputs(snapshot)
                    return ScriptRunReport(
                        status="failed",
                        same_attempt_feedback=True,
                        receipts=tuple(receipts),
                        output_hashes={},
                        deterministic_match=False,
                        error_category="same_attempt_feedback",
                        error_type=type(exc).__name__,
                    )
                output_hashes = self._hash_outputs(outputs)
            finally:
                shutil.rmtree(first_root, ignore_errors=True)
                shutil.rmtree(second_root, ignore_errors=True)
        return ScriptRunReport(
            status="passed",
            same_attempt_feedback=False,
            receipts=tuple(receipts),
            output_hashes=output_hashes,
            deterministic_match=deterministic_match,
        )

    def run(self, script: str | Path, **kwargs: Any) -> ScriptRunReport:
        """Explicit alias for :meth:`run_pipeline`."""

        return self.run_pipeline(script, **kwargs)


__all__ = [
    "ANALYSIS_CONTEXT_ENV",
    "ANALYSIS_CONTEXT_SCHEMA_VERSION",
    "ANALYSIS_OUTPUT_ROOT_ENV",
    "ANALYSIS_PHASE_ENV",
    "ANALYSIS_SAMPLE_LIMIT_ENV",
    "BoundAnalysisContext",
    "CatalogSnapshot",
    "ControlledScriptRunner",
    "ScriptExecutionReceipt",
    "ScriptRunReport",
    "load_bound_analysis_context",
]
