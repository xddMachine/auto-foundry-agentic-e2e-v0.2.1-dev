"""Small durable item workspaces for offline, incremental analysis.

The module owns the item-local durable contract.  It creates a bounded item
directory before an agent is invoked, persists incrementally written artifacts,
and records deterministic execution/review/terminal transitions.  It never
executes models or scripts itself.

Canonical extended ``item_state.json`` keys are exposed as ``ITEM_STATE_FIELDS``
and ``ITEM_STATE_SCHEMA`` below.  A freshly-created workspace deliberately
writes only the eight base fields; the first layer-2 operation migrates it to
the extended shape in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path, PurePath
import shutil
import tempfile
from types import MappingProxyType
from typing import Any, Mapping

from .workspace import AllowedRootError, RunContext


_VALID_MODES = frozenset({"question", "requirement"})
_STATE_FILENAME = "item_state.json"
_PLAN_FILENAME = "plan.json"
_SOURCE_MAP_FILENAME = "source_map.json"
_FINDINGS_FILENAME = "findings.jsonl"
_OPEN_ISSUES_FILENAME = "open_issues.json"
_HANDOFF_FILENAME = "handoff.json"
_DRAFT_FILENAME = "draft.json"
_ACCEPTED_FILENAME = "accepted.json"
_BASE_STATE_FIELDS = frozenset(
    {
        "item_id",
        "mode",
        "original_text",
        "lifecycle_state",
        "execution_recovery_count",
        "business_repair_count",
        "created_at",
        "updated_at",
    }
)
_EXECUTION_STATE_FIELDS = frozenset(
    {
        *_BASE_STATE_FIELDS,
        "attempts",
        "active_attempt_id",
        "consecutive_no_progress",
        "review",
        "terminal_outcome",
        "terminal_intent",
    }
)
ITEM_STATE_FIELDS = (
    "item_id",
    "mode",
    "original_text",
    "lifecycle_state",
    "execution_recovery_count",
    "business_repair_count",
    "created_at",
    "updated_at",
    "attempts",
    "active_attempt_id",
    "consecutive_no_progress",
    "review",
    "terminal_outcome",
    "terminal_intent",
)
_ATTEMPT_FIELDS = (
    "attempt_id",
    "lane_id",
    "role",
    "route",
    "status",
    "baseline",
    "prior_attempt_id",
    "handoff_ref",
    "error",
)
_REVIEW_FIELDS = frozenset({"status", "strength", "verdict", "reviewer_ref", "draft_hash"})
_REVIEW_VERDICTS = frozenset({"accept", "accept_with_limits", "repair_once", "block_specific_claims"})
_KNOWLEDGE_DELTAS = frozenset({"promoted", "promoted_with_limits", "no_change"})
_LIFECYCLE_STATES = frozenset({"work", "recovering", "recovery_ready", "review", "accepted", "technical_failure"})
ITEM_STATE_SCHEMA = {
    "fields": ITEM_STATE_FIELDS,
    "attempt_fields": _ATTEMPT_FIELDS,
    "review_fields": tuple(sorted(_REVIEW_FIELDS)),
    "accepted_directory": "accepted.json/",
    "accepted_manifest": "accepted.json/manifest.json",
    "terminal_intent_fields": ("outcome", "manifest_hash"),
}
_TERMINAL_FIELDS = frozenset({"status", "item_id", "outcome", "manifest_path", "content_hash"})
_TERMINAL_INTENT_FIELDS = frozenset({"outcome", "manifest_hash"})
_ACCEPTED_MANIFEST_FIELDS = frozenset(
    {
        "item_id",
        "outcome",
        "final_answer",
        "content_hash",
        "draft_hash",
        "hashes",
        "artifact_progress",
        "refs",
        "accepted_refs",
        "knowledge_delta",
        "manifest_hash",
    }
)
_TECHNICAL_MANIFEST_FIELDS = frozenset(
    {
        "item_id",
        "outcome",
        "reason",
        "recovery_exhausted",
        "hashes",
        "artifact_progress",
        "refs",
        "content_hash",
        "manifest_hash",
    }
)
_SCRIPT_SUFFIXES = frozenset(
    {
        ".bash",
        ".cmd",
        ".ipynb",
        ".js",
        ".mjs",
        ".pl",
        ".ps1",
        ".py",
        ".r",
        ".rb",
        ".sh",
        ".sql",
        ".ts",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _simple_component(value: str, label: str) -> str:
    """Validate and return one non-empty path component.

    ``RunContext`` resolves paths before containment checks, but validating the
    item identifier separately keeps an identifier from changing the intended
    namespace (and mirrors the existing workbench identifier contract).
    """

    component = str(value).strip()
    if (
        not component
        or component in {".", ".."}
        or Path(component).name != component
        or "\\" in component
        or "\x00" in component
    ):
        raise ValueError(f"{label} must be a simple path component")
    return component


def _validate_mode(mode: str) -> str:
    value = str(mode).strip()
    if value not in _VALID_MODES:
        raise ValueError("mode must be 'question' or 'requirement'")
    return value


def _jsonable(value: Any) -> Any:
    """Convert common local values into deterministic JSON values."""

    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=lambda item: str(item))]
    if isinstance(value, (Path, PurePath)):
        return str(value)
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    """Best-effort fsync for a directory containing an atomic rename."""

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


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes through a same-directory temp file and atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # A directory fsync is best effort on platforms that expose it.  The
        # atomic rename above is the contract; an unsupported directory fsync
        # must not turn a successful artifact write into a failure.
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    """Append one deterministic JSONL record and force it to stable storage."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(value)
    with path.open("ab") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    return _sha256_bytes(_json_bytes(unsigned))


def _is_temp_name(name: str) -> bool:
    # Temporary files generated by _atomic_write_bytes are named
    # ``.<target>.tmp-<random>``.  A user-created hidden artifact is still
    # material and therefore is not excluded merely because it starts with a
    # dot.
    return ".tmp-" in name


def _assert_regular_no_symlink(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise AllowedRootError(f"{label} cannot be a symlink: {path}")
    if path.exists() and not path.is_file() and not path.is_dir():
        raise ValueError(f"{label} is not a regular file or directory: {path}")


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    _assert_regular_no_symlink(path, label="JSONL artifact")
    count = 0
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                count += 1
    return count


def _count_source_map(path: Path) -> int:
    if not path.exists():
        return 0
    _assert_regular_no_symlink(path, label="source map artifact")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A malformed user edit is still one material source-map artifact.  The
        # write APIs themselves always produce valid JSON.
        return 1
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        return len(value)
    return 1


def _validate_progress_files(value: Mapping[str, Any], *, label: str) -> None:
    files = value["files"]
    if not isinstance(files, (list, tuple)) or any(not isinstance(item, str) for item in files):
        raise ValueError(f"{label} files are invalid")
    if len(set(files)) != len(files) or tuple(files) != tuple(sorted(files)):
        raise ValueError(f"{label} files are not canonical")


def _validate_progress_hashes(value: Mapping[str, Any], *, label: str) -> None:
    hashes = value["hashes"]
    if not isinstance(hashes, Mapping) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in hashes.items()):
        raise ValueError(f"{label} hashes are invalid")
    if set(value["files"]) != set(hashes):
        raise ValueError(f"{label} files and hashes do not match")
    for item in hashes.values():
        if len(item) != 64 or any(char not in "0123456789abcdef" for char in item):
            raise ValueError(f"{label} hashes are invalid")


def _validate_progress_counts(value: Mapping[str, Any], *, label: str) -> None:
    for field_name in ("finding_count", "source_map_count", "script_count", "draft_count"):
        count = value[field_name]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{label} {field_name} is invalid")
    if not isinstance(value["handoff_present"], bool):
        raise ValueError(f"{label} handoff_present is invalid")


def _validate_progress_mapping(value: Any, *, label: str = "artifact progress") -> None:
    """Validate the canonical serialized ``ArtifactProgress`` shape."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    required = {
        "files",
        "hashes",
        "finding_count",
        "source_map_count",
        "script_count",
        "draft_count",
        "handoff_present",
    }
    if set(value) != required:
        raise ValueError(f"{label} fields are invalid")
    _validate_progress_files(value, label=label)
    _validate_progress_hashes(value, label=label)
    _validate_progress_counts(value, label=label)


@dataclass(frozen=True)
class ArtifactProgress:
    """Durable, hash-backed progress for one item workspace."""

    files: tuple[str, ...]
    hashes: Mapping[str, str]
    finding_count: int
    source_map_count: int
    script_count: int
    draft_count: int
    handoff_present: bool

    def __post_init__(self) -> None:
        files = tuple(str(path) for path in self.files)
        hashes = {str(path): str(value) for path, value in self.hashes.items()}
        counts = {
            "finding_count": self.finding_count,
            "source_map_count": self.source_map_count,
            "script_count": self.script_count,
            "draft_count": self.draft_count,
        }
        if any(isinstance(value, bool) or int(value) < 0 for value in counts.values()):
            raise ValueError("artifact progress counts must be nonnegative integers")
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "hashes", MappingProxyType(dict(sorted(hashes.items()))))
        for name, value in counts.items():
            object.__setattr__(self, name, int(value))
        object.__setattr__(self, "handoff_present", bool(self.handoff_present))

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "hashes": dict(self.hashes),
            "finding_count": self.finding_count,
            "source_map_count": self.source_map_count,
            "script_count": self.script_count,
            "draft_count": self.draft_count,
            "handoff_present": self.handoff_present,
        }

    def materially_changed(self, other: "ArtifactProgress") -> bool:
        """Return whether this snapshot differs in any material field."""

        if not isinstance(other, ArtifactProgress):
            return True
        return self != other


@dataclass(frozen=True)
class ProgressDecision:
    """The host-facing decision after observing one active attempt."""

    action: str
    progress: ArtifactProgress
    changed_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in {"continue", "materialize_now", "recover"}:
            raise ValueError("progress decision action is invalid")
        if not isinstance(self.progress, ArtifactProgress):
            raise TypeError("progress must be ArtifactProgress")
        object.__setattr__(self, "changed_files", tuple(str(path) for path in self.changed_files))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "progress": self.progress.to_dict(),
            "changed_files": list(self.changed_files),
        }


@dataclass(frozen=True)
class ExecutionAttempt:
    """Durable identity and baseline for one host-managed attempt."""

    attempt_id: str
    lane_id: str
    role: str
    route: str
    status: str
    baseline: ArtifactProgress

    def __post_init__(self) -> None:
        for name in ("attempt_id", "lane_id", "role", "route", "status"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must be non-empty")
            object.__setattr__(self, name, value)
        if not isinstance(self.baseline, ArtifactProgress):
            raise TypeError("baseline must be ArtifactProgress")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "route": self.route,
            "status": self.status,
            "baseline": self.baseline.to_dict(),
        }


@dataclass(frozen=True)
class AcceptedSnapshot:
    """Immutable terminal snapshot metadata."""

    item_id: str
    outcome: str
    manifest_path: str
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("item_id", "outcome", "manifest_path", "content_hash"):
            value = str(getattr(self, name))
            if not value:
                raise ValueError(f"{name} must be non-empty")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "outcome": self.outcome,
            "manifest_path": self.manifest_path,
            "content_hash": self.content_hash,
        }


class ItemWorkspace:
    """One bounded question/requirement workspace under a ``RunContext``."""

    def __init__(
        self,
        context: RunContext,
        item_id: str,
        *,
        mode: str,
        original_text: str,
        telemetry: Any = None,
        state: Mapping[str, Any],
    ) -> None:
        self.context = context
        self.item_id = _simple_component(item_id, "item_id")
        self.mode = _validate_mode(mode)
        if not isinstance(original_text, str):
            raise TypeError("original_text must be a string")
        self.original_text = original_text
        self.telemetry = telemetry
        self._state = dict(state)

    @classmethod
    def create(
        cls,
        context: RunContext,
        item_id: str,
        *,
        mode: str = "question",
        original_text: str,
        telemetry: Any = None,
    ) -> "ItemWorkspace":
        item_id = _simple_component(item_id, "item_id")
        mode = _validate_mode(mode)
        if not isinstance(original_text, str):
            raise TypeError("original_text must be a string")
        item_root = cls._resolve_item_root(context, item_id, mode)
        cls._reject_existing_symlink_components(context, item_root)
        state_path = item_root / _STATE_FILENAME
        if state_path.exists() or state_path.is_symlink():
            cls._reject_existing_symlink_components(context, item_root)
            state = cls._read_state(state_path)
            cls._validate_state(state, item_id=item_id, mode=mode, original_text=original_text)
            workspace = cls(
                context,
                item_id,
                mode=mode,
                original_text=original_text,
                telemetry=telemetry,
                state=state,
            )
            # Existing workspaces may have a crash-published terminal
            # directory.  Reconcile it before recreating/allowing work; keep
            # the original eight-field create shape untouched when no layer-2
            # state or terminal directory exists.
            if set(state) == _EXECUTION_STATE_FIELDS or workspace.accepted_root.exists() or workspace.accepted_root.is_symlink():
                workspace._ensure_execution_state()
                workspace._reconcile_review_draft()
                workspace._reconcile_terminal_snapshot()
            # A prior process may have been interrupted after state creation
            # but before work/ creation.  Re-establish the required workspace
            # directory without touching any user artifact.
            if workspace.state.get("lifecycle_state") not in {"accepted", "technical_failure"}:
                workspace._ensure_work_root()
            workspace._emit("item_workspace_load", artifact="item_state.json")
            return workspace

        # An item id is globally owned by one mode.  If the caller points at
        # the other namespace, report a mode mismatch rather than silently
        # creating a second state record for the same id.
        opposite_state = cls._opposite_state_path(context, item_id, mode)
        if opposite_state is not None:
            other_state = cls._read_state(opposite_state)
            if other_state["item_id"] == item_id:
                raise ValueError("item_state.json mode does not match requested mode")

        item_root.mkdir(parents=True, exist_ok=True)
        workspace_root = item_root / "work"
        workspace_root.mkdir(parents=True, exist_ok=True)
        created_at = _now()
        state = {
            "item_id": item_id,
            "mode": mode,
            "original_text": original_text,
            "lifecycle_state": "work",
            "execution_recovery_count": 0,
            "business_repair_count": 0,
            "created_at": created_at,
            "updated_at": created_at,
        }
        # State is written after both bounded directories are validated and
        # before returning, so a caller can invoke an agent only after this
        # method completes with a nonempty authoritative file.
        _atomic_write_json(state_path, state)
        workspace = cls(
            context,
            item_id,
            mode=mode,
            original_text=original_text,
            telemetry=telemetry,
            state=state,
        )
        workspace._emit("item_workspace_create", artifact="item_state.json")
        return workspace

    @classmethod
    def load(
        cls,
        context: RunContext,
        item_id: str,
        *,
        mode: str = "question",
        telemetry: Any = None,
    ) -> "ItemWorkspace":
        item_id = _simple_component(item_id, "item_id")
        mode = _validate_mode(mode)
        item_root = cls._resolve_item_root(context, item_id, mode)
        cls._reject_existing_symlink_components(context, item_root)
        state_path = item_root / _STATE_FILENAME
        if not state_path.is_file():
            opposite_state = cls._opposite_state_path(context, item_id, mode)
            if opposite_state is not None:
                other_state = cls._read_state(opposite_state)
                if other_state["item_id"] == item_id:
                    raise ValueError("item_state.json mode does not match requested mode")
            raise FileNotFoundError(state_path)
        state = cls._read_state(state_path)
        cls._validate_state_shape(state)
        if state["item_id"] != item_id:
            raise ValueError("item_state.json item_id does not match requested item")
        if state["mode"] != mode:
            raise ValueError("item_state.json mode does not match requested mode")
        workspace = cls(
            context,
            item_id,
            mode=mode,
            original_text=str(state["original_text"]),
            telemetry=telemetry,
            state=state,
        )
        workspace._ensure_execution_state()
        workspace._reconcile_review_draft()
        workspace._reconcile_terminal_snapshot()
        workspace._emit("item_workspace_load", artifact="item_state.json")
        return workspace

    @staticmethod
    def _read_terminal_manifest(manifest_path: Path) -> dict[str, Any]:
        _assert_regular_no_symlink(manifest_path, label="accepted manifest")
        if not manifest_path.is_file():
            raise ValueError("accepted snapshot manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("accepted snapshot manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise ValueError("accepted snapshot manifest must be an object")
        outcome = manifest.get("outcome")
        if outcome not in {"accepted", "technical_failure"}:
            raise ValueError("accepted snapshot outcome is invalid")
        expected_fields = _ACCEPTED_MANIFEST_FIELDS if outcome == "accepted" else _TECHNICAL_MANIFEST_FIELDS
        if set(manifest) != expected_fields:
            raise ValueError("accepted snapshot manifest fields are invalid")
        content_hash = manifest.get("content_hash")
        if not _is_sha256(content_hash):
            raise ValueError("accepted snapshot content_hash is invalid")
        return manifest

    @staticmethod
    def _terminal_file_inventory(accepted: Path) -> set[str]:
        files: set[str] = set()
        for base, directories, names in os.walk(accepted, followlinks=False):
            for name in directories:
                path = Path(base) / name
                if path.is_symlink():
                    raise AllowedRootError(f"accepted snapshot cannot contain symlinks: {path}")
            for name in names:
                path = Path(base) / name
                if path.is_symlink():
                    raise AllowedRootError(f"accepted snapshot cannot contain symlinks: {path}")
                if not path.is_file():
                    raise ValueError("accepted snapshot contains a non-file artifact")
                files.add(path.relative_to(accepted).as_posix())
        return files

    @staticmethod
    def _validate_manifest_refs(value: Any, *, label: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(ref, str) or not ref for ref in value):
            raise ValueError(f"accepted snapshot {label} is invalid")
        return list(value)

    @staticmethod
    def _validate_manifest_artifacts(manifest: Mapping[str, Any]) -> None:
        progress = manifest.get("artifact_progress")
        _validate_progress_mapping(progress, label="accepted snapshot artifact_progress")
        hashes = manifest.get("hashes")
        if not isinstance(hashes, Mapping) or dict(hashes) != dict(progress["hashes"]):
            raise ValueError("accepted snapshot hashes are inconsistent")

    def _validate_terminal_manifest(self, manifest: Mapping[str, Any], files: set[str]) -> None:
        if _manifest_hash(manifest) != manifest["manifest_hash"]:
            raise ValueError("accepted snapshot manifest hash does not match content")
        if manifest.get("item_id") != self.item_id:
            raise ValueError("accepted snapshot item_id is invalid")
        outcome = manifest["outcome"]
        content_hash = manifest["content_hash"]
        if outcome == "accepted":
            final_name = manifest["final_answer"]
            if final_name != "final_answer.json":
                raise ValueError("accepted snapshot final_answer is invalid")
            draft_hash = manifest["draft_hash"]
            if not _is_sha256(draft_hash):
                raise ValueError("accepted snapshot draft_hash is invalid")
            if draft_hash != content_hash:
                raise ValueError("accepted snapshot draft_hash does not match content_hash")
            refs = self._validate_manifest_refs(manifest["refs"], label="refs")
            accepted_refs = self._validate_manifest_refs(manifest["accepted_refs"], label="accepted_refs")
            if refs != accepted_refs:
                raise ValueError("accepted snapshot refs are inconsistent")
            if manifest["knowledge_delta"] not in _KNOWLEDGE_DELTAS:
                raise ValueError("accepted snapshot knowledge_delta is invalid")
            self._validate_manifest_artifacts(manifest)
            if files != {"manifest.json", final_name}:
                raise ValueError("accepted snapshot files are inconsistent")
            final_path = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / final_name)
            _assert_regular_no_symlink(final_path, label="accepted final answer")
            if _sha256_file(final_path) != content_hash:
                raise ValueError("accepted final answer hash does not match manifest")
            return
        if not isinstance(manifest["reason"], str) or not manifest["reason"]:
            raise ValueError("technical failure reason is invalid")
        if manifest["recovery_exhausted"] is not True:
            raise ValueError("technical failure snapshot files are inconsistent")
        self._validate_manifest_refs(manifest["refs"], label="refs")
        self._validate_manifest_artifacts(manifest)
        if files != {"manifest.json"}:
            raise ValueError("technical failure snapshot files are inconsistent")
        unsigned = dict(manifest)
        unsigned.pop("content_hash", None)
        unsigned.pop("manifest_hash", None)
        if _sha256_bytes(_json_bytes(unsigned)) != content_hash:
            raise ValueError("technical failure manifest hash does not match content")

    def _read_valid_terminal_snapshot(self) -> tuple[AcceptedSnapshot, dict[str, Any]]:
        """Read and verify the immutable accepted directory.

        The directory is renamed into place before ``item_state.json`` is
        updated.  A reload therefore reconciles a valid directory after a
        state-write interruption and rejects partial snapshots closed.
        """

        accepted = self.accepted_root
        _assert_regular_no_symlink(accepted, label="accepted snapshot")
        if not accepted.is_dir():
            raise ValueError("accepted snapshot must be a directory")
        manifest_path = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / "manifest.json")
        manifest = self._read_terminal_manifest(manifest_path)
        if manifest.get("item_id") != self.item_id:
            raise ValueError("accepted snapshot item_id is invalid")
        files = self._terminal_file_inventory(accepted)
        self._validate_terminal_manifest(manifest, files)
        snapshot = AcceptedSnapshot(self.item_id, manifest["outcome"], str(manifest_path), manifest["content_hash"])
        return snapshot, manifest

    def _validate_preterminal_binding(self, outcome: str, manifest: Mapping[str, Any]) -> None:
        """Bind the published snapshot to the state that authorized it."""

        intent = self._state.get("terminal_intent")
        if not isinstance(intent, Mapping) or intent.get("outcome") != outcome or intent.get("manifest_hash") != manifest.get("manifest_hash"):
            raise ValueError("accepted snapshot does not match terminal intent")
        if self._state.get("active_attempt_id") is not None:
            raise ValueError("accepted snapshot cannot coexist with an active attempt")
        lifecycle = self._state.get("lifecycle_state")
        if outcome == "accepted":
            if lifecycle not in {"review", "accepted"}:
                raise ValueError("accepted snapshot requires a review preterminal state")
            review = self._state.get("review", {})
            accepted_review = (
                review.get("status") == "reviewed" and review.get("verdict") in {"accept", "accept_with_limits"}
            ) or (review.get("status") == "unavailable" and review.get("verdict") == "not_reviewed")
            if not accepted_review:
                raise ValueError("accepted snapshot requires a valid review")
            if review.get("draft_hash") != manifest.get("draft_hash") or review.get("draft_hash") != manifest.get("content_hash"):
                raise ValueError("accepted snapshot review hash does not match content")
            return
        if lifecycle not in {"work", "review", "technical_failure"}:
            raise ValueError("technical failure snapshot requires a valid preterminal state")

    def _reconcile_terminal_snapshot(self) -> None:
        """Reconcile directory publication and state persistence after a crash."""

        accepted = self.accepted_root
        if not accepted.exists() and not accepted.is_symlink():
            if self._state.get("lifecycle_state") in {"accepted", "technical_failure"}:
                raise ValueError("terminal state has no accepted snapshot")
            if self._state.get("terminal_intent") is not None:
                state = copy.deepcopy(self._state)
                state["terminal_intent"] = None
                self._persist_state(state)
            return
        snapshot, manifest = self._read_valid_terminal_snapshot()
        self._validate_preterminal_binding(snapshot.outcome, manifest)
        lifecycle = self._state["lifecycle_state"]
        if lifecycle in {"accepted", "technical_failure"}:
            terminal = self._state.get("terminal_outcome")
            expected = {"status": lifecycle, **snapshot.to_dict()}
            if terminal != expected:
                raise ValueError("terminal state does not match accepted snapshot")
            return
        state = copy.deepcopy(self._state)
        state["lifecycle_state"] = snapshot.outcome
        state["terminal_outcome"] = {"status": snapshot.outcome, **snapshot.to_dict()}
        self._persist_state(state)

    def _reconcile_review_draft(self) -> None:
        """Invalidate a review that no longer covers the current draft bytes."""

        review = self._state.get("review")
        if not isinstance(review, Mapping) or review.get("status") == "pending":
            return
        if self._state.get("lifecycle_state") in {"accepted", "technical_failure"}:
            return
        if self._state.get("terminal_intent") is not None and self.accepted_root.exists():
            # The intent binds the exact bytes selected before publication; a
            # concurrent post-read draft replacement must not invalidate that
            # immutable terminal snapshot during reconciliation.
            return
        try:
            current_hash = self._draft_hash()
        except FileNotFoundError:
            current_hash = None
        if current_hash == review.get("draft_hash"):
            return
        state = copy.deepcopy(self._state)
        state["review"] = self._pending_review()
        state["lifecycle_state"] = "work"
        self._persist_state(state)

    @staticmethod
    def _resolve_item_root(context: RunContext, item_id: str, mode: str) -> Path:
        namespace = "questions" if mode == "question" else "requirements"
        return ItemWorkspace._validate_lexical_item_root(context, item_id, namespace)

    @staticmethod
    def _validate_lexical_path(context: RunContext, path: Path) -> Path:
        try:
            relative = path.relative_to(context.run_root)
        except ValueError as exc:
            raise AllowedRootError(f"item workspace escapes run context: {path}") from exc
        current = context.run_root
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise AllowedRootError(f"item workspace path cannot use symlink: {current}")
        return context.resolve_run_path(path)

    @staticmethod
    def _validate_lexical_item_root(context: RunContext, item_id: str, namespace: str) -> Path:
        lexical = context.run_root / namespace / item_id
        return ItemWorkspace._validate_lexical_path(context, lexical)

    def _item_root_lexical(self) -> Path:
        namespace = "questions" if self.mode == "question" else "requirements"
        return self.context.run_root / namespace / self.item_id

    def _resolve_item_subpath(self, relative: str | Path = "") -> Path:
        lexical = self._item_root_lexical() / relative
        return self._validate_lexical_path(self.context, lexical)

    @staticmethod
    def _opposite_state_path(context: RunContext, item_id: str, mode: str) -> Path | None:
        namespace = "requirements" if mode == "question" else "questions"
        raw_namespace = context.run_root / namespace
        raw_item = raw_namespace / item_id
        if not raw_namespace.exists() and not raw_namespace.is_symlink():
            return None
        if raw_namespace.is_symlink() or raw_item.is_symlink():
            raise AllowedRootError(f"item workspace path cannot use symlink: {raw_item}")
        candidate = context.resolve_run_path(Path(namespace) / item_id / _STATE_FILENAME)
        return candidate if candidate.is_file() else None

    @staticmethod
    def _reject_existing_symlink_components(context: RunContext, item_root: Path) -> None:
        # Keep this helper for callers that already resolved a path, while all
        # instance operations use the lexical validator above.  A resolved
        # path is still checked for containment and direct symlink use.
        ItemWorkspace._validate_lexical_path(context, item_root)

    @staticmethod
    def _read_state(path: Path) -> dict[str, Any]:
        _assert_regular_no_symlink(path, label="item state")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid item state JSON: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError("item_state.json must contain an object")
        ItemWorkspace._validate_state_shape(value)
        return dict(value)

    @staticmethod
    def _validate_state_identity(state: Mapping[str, Any]) -> None:
        if not isinstance(state["item_id"], str) or not state["item_id"]:
            raise ValueError("item_state.json item_id is invalid")
        _simple_component(state["item_id"], "item_id")
        _validate_mode(state["mode"])
        if not isinstance(state["original_text"], str):
            raise ValueError("item_state.json original_text is invalid")

    @staticmethod
    def _validate_state_lifecycle(state: Mapping[str, Any], fields: set[str]) -> None:
        if fields == _BASE_STATE_FIELDS and state["lifecycle_state"] != "work":
            raise ValueError("item_state.json lifecycle_state must be 'work'")
        if fields == _EXECUTION_STATE_FIELDS and state["lifecycle_state"] not in _LIFECYCLE_STATES:
            raise ValueError("item_state.json lifecycle_state is invalid")

    @staticmethod
    def _validate_state_counters(state: Mapping[str, Any], base_shape: bool) -> None:
        for field_name in ("execution_recovery_count", "business_repair_count"):
            value = state[field_name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"item_state.json {field_name} must be a nonnegative integer")
            if base_shape and value != 0:
                raise ValueError(f"item_state.json {field_name} must be zero")

    @staticmethod
    def _validate_state_timestamps(state: Mapping[str, Any]) -> None:
        for field_name in ("created_at", "updated_at"):
            if not isinstance(state[field_name], str) or not state[field_name]:
                raise ValueError(f"item_state.json {field_name} is invalid")

    @staticmethod
    def _validate_state_fields(state: Mapping[str, Any]) -> bool:
        fields = set(state)
        if fields not in {_BASE_STATE_FIELDS, _EXECUTION_STATE_FIELDS}:
            raise ValueError("item_state.json fields do not match the durable item contract")
        ItemWorkspace._validate_state_identity(state)
        ItemWorkspace._validate_state_lifecycle(state, fields)
        ItemWorkspace._validate_state_counters(state, fields == _BASE_STATE_FIELDS)
        ItemWorkspace._validate_state_timestamps(state)
        return fields == _EXECUTION_STATE_FIELDS

    @staticmethod
    def _validate_attempt_record(record: Mapping[str, Any]) -> bool:
        if not isinstance(record, Mapping):
            raise ValueError("item_state.json attempt must be an object")
        if set(record) != set(_ATTEMPT_FIELDS):
            raise ValueError("item_state.json attempt is incomplete")
        identity = ("attempt_id", "lane_id", "role", "route", "status")
        if any(not isinstance(record[name], str) or not record[name] for name in identity):
            raise ValueError("item_state.json attempt identity is invalid")
        _validate_progress_mapping(record["baseline"], label="item_state.json attempt baseline")
        for optional in ("error", "prior_attempt_id", "handoff_ref"):
            if record[optional] is not None and not isinstance(record[optional], str):
                raise ValueError(f"item_state.json attempt {optional} is invalid")
        return record["status"] == "active"

    @staticmethod
    def _validate_attempt_collection(attempts: Any) -> tuple[set[str], int, set[str]]:
        if not isinstance(attempts, list):
            raise ValueError("item_state.json attempts must be a list")
        attempt_ids: set[str] = set()
        active_ids: set[str] = set()
        active_count = 0
        for record in attempts:
            active_count += int(ItemWorkspace._validate_attempt_record(record))
            attempt_id = record["attempt_id"]
            if attempt_id in attempt_ids:
                raise ValueError("item_state.json attempt IDs must be unique")
            attempt_ids.add(attempt_id)
            if record["status"] == "active":
                active_ids.add(attempt_id)
        return attempt_ids, active_count, active_ids

    @staticmethod
    def _validate_active_attempt(
        state: Mapping[str, Any], attempt_ids: set[str], active_count: int, active_ids: set[str]
    ) -> str | None:
        active = state["active_attempt_id"]
        if active is not None and (not isinstance(active, str) or not active):
            raise ValueError("item_state.json active_attempt_id is invalid")
        if (active is None and active_count) or (active is not None and active_count != 1):
            raise ValueError("item_state.json active_attempt_id must match exactly one active attempt")
        if active is not None and active not in attempt_ids:
            raise ValueError("item_state.json active_attempt_id is unknown")
        if active is not None and active not in active_ids:
            raise ValueError("item_state.json active_attempt_id must match the active attempt")
        consecutive = state["consecutive_no_progress"]
        if isinstance(consecutive, bool) or not isinstance(consecutive, int) or consecutive < 0:
            raise ValueError("item_state.json consecutive_no_progress is invalid")
        return active

    @staticmethod
    def _validate_attempt_state(state: Mapping[str, Any]) -> str | None:
        attempt_ids, active_count, active_ids = ItemWorkspace._validate_attempt_collection(state["attempts"])
        return ItemWorkspace._validate_active_attempt(state, attempt_ids, active_count, active_ids)

    @staticmethod
    def _validate_review_metadata(review: Mapping[str, Any]) -> tuple[str, Any, Any, Any]:
        if not isinstance(review, Mapping) or set(review) != _REVIEW_FIELDS:
            raise ValueError("item_state.json review is invalid")
        if not isinstance(review["status"], str) or not review["status"]:
            raise ValueError("item_state.json review status is invalid")
        if review["strength"] is not None and not isinstance(review["strength"], str):
            raise ValueError("item_state.json review strength is invalid")
        if review["verdict"] is not None and not isinstance(review["verdict"], str):
            raise ValueError("item_state.json review verdict is invalid")
        if review["reviewer_ref"] is not None and not isinstance(review["reviewer_ref"], str):
            raise ValueError("item_state.json reviewer_ref is invalid")
        draft_hash = review.get("draft_hash")
        if draft_hash is not None and (not isinstance(draft_hash, str) or len(draft_hash) != 64):
            raise ValueError("item_state.json review draft_hash is invalid")
        return review["status"], review["verdict"], review.get("strength"), draft_hash

    @staticmethod
    def _validate_review_status(status: str, verdict: Any, strength: Any, reviewer_ref: Any, draft_hash: Any) -> None:
        if status == "pending":
            fields = (strength, verdict, reviewer_ref, draft_hash)
            if any(value is not None for value in fields):
                raise ValueError("pending review must not contain a verdict or draft hash")
            return
        if status == "reviewed":
            if verdict not in _REVIEW_VERDICTS or draft_hash is None:
                raise ValueError("reviewed state requires a valid verdict and draft hash")
            return
        if status == "unavailable":
            if verdict != "not_reviewed":
                raise ValueError("unavailable review must disclose not_reviewed")
            if strength != "none" or reviewer_ref is not None or draft_hash is None:
                raise ValueError("unavailable review must disclose not_reviewed")
            return
        raise ValueError("item_state.json review status is invalid")

    @staticmethod
    def _validate_review_state(review: Mapping[str, Any]) -> str:
        status, verdict, strength, draft_hash = ItemWorkspace._validate_review_metadata(review)
        ItemWorkspace._validate_review_status(status, verdict, strength, review.get("reviewer_ref"), draft_hash)
        return status

    @staticmethod
    def _validate_terminal_payload(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
        terminal = state["terminal_outcome"]
        if terminal is not None:
            if not isinstance(terminal, Mapping) or set(terminal) != _TERMINAL_FIELDS:
                raise ValueError("item_state.json terminal_outcome is invalid")
            if any(not isinstance(terminal[name], str) or not terminal[name] for name in _TERMINAL_FIELDS):
                raise ValueError("item_state.json terminal_outcome values are invalid")
            if terminal["status"] != terminal["outcome"] or terminal["item_id"] != state["item_id"]:
                raise ValueError("terminal_outcome identity is invalid")
            if len(terminal["content_hash"]) != 64:
                raise ValueError("terminal_outcome content_hash is invalid")
        return terminal

    @staticmethod
    def _validate_terminal_intent(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
        intent = state["terminal_intent"]
        if intent is None:
            return None
        if not isinstance(intent, Mapping) or set(intent) != _TERMINAL_INTENT_FIELDS:
            raise ValueError("item_state.json terminal_intent is invalid")
        if intent["outcome"] not in {"accepted", "technical_failure"}:
            raise ValueError("item_state.json terminal_intent outcome is invalid")
        manifest_hash = intent["manifest_hash"]
        if not isinstance(manifest_hash, str) or len(manifest_hash) != 64 or any(char not in "0123456789abcdef" for char in manifest_hash):
            raise ValueError("item_state.json terminal_intent manifest_hash is invalid")
        return intent

    @staticmethod
    def _validate_lifecycle_consistency(
        state: Mapping[str, Any],
        terminal: Mapping[str, Any] | None,
        intent: Mapping[str, Any] | None,
        active: str | None,
        review_status: str,
    ) -> None:
        lifecycle = state["lifecycle_state"]
        if lifecycle in {"accepted", "technical_failure"}:
            if terminal is None or terminal["outcome"] != lifecycle:
                raise ValueError("terminal lifecycle requires matching terminal_outcome")
            if intent is None or intent["outcome"] != lifecycle:
                raise ValueError("terminal lifecycle requires matching terminal_intent")
            if active is not None:
                raise ValueError("terminal lifecycle cannot have an active attempt")
        elif terminal is not None:
            raise ValueError("non-terminal lifecycle cannot have terminal_outcome")
        if intent is not None and active is not None:
            raise ValueError("terminal intent cannot have an active attempt")
        if intent is not None and lifecycle not in {"accepted", "technical_failure"}:
            allowed_preterminal = {"review"} if intent["outcome"] == "accepted" else {"work", "review"}
            if lifecycle not in allowed_preterminal:
                raise ValueError("terminal intent lifecycle is invalid")
        if lifecycle == "review" and (active is not None or review_status not in {"reviewed", "unavailable"}):
            raise ValueError("review lifecycle requires an inactive reviewed state")
        if lifecycle in {"work", "recovering", "recovery_ready"} and review_status != "pending":
            raise ValueError("work lifecycle requires pending review")
        if lifecycle in {"recovering", "recovery_ready"} and active is None:
            raise ValueError("recovery lifecycle requires an active attempt")

    @staticmethod
    def _validate_terminal_state(state: Mapping[str, Any], active: str | None, review_status: str) -> None:
        terminal = ItemWorkspace._validate_terminal_payload(state)
        intent = ItemWorkspace._validate_terminal_intent(state)
        ItemWorkspace._validate_lifecycle_consistency(state, terminal, intent, active, review_status)

    @staticmethod
    def _validate_state_shape(state: Mapping[str, Any]) -> None:
        if not ItemWorkspace._validate_state_fields(state):
            return
        active = ItemWorkspace._validate_attempt_state(state)
        review_status = ItemWorkspace._validate_review_state(state["review"])
        ItemWorkspace._validate_terminal_state(state, active, review_status)

    @staticmethod
    def _execution_defaults() -> dict[str, Any]:
        return {
            "attempts": [],
            "active_attempt_id": None,
            "consecutive_no_progress": 0,
            "review": ItemWorkspace._pending_review(),
            "terminal_outcome": None,
            "terminal_intent": None,
        }

    def _ensure_execution_state(self) -> None:
        """Migrate this module's original eight-field state exactly once."""

        self._validate_state_shape(self._state)
        migrated = copy.deepcopy(dict(self._state))
        changed = False
        if set(migrated) == _BASE_STATE_FIELDS:
            migrated.update(self._execution_defaults())
            changed = True
        if not changed:
            return
        self._persist_state(migrated)

    @classmethod
    def _validate_state(
        cls,
        state: Mapping[str, Any],
        *,
        item_id: str,
        mode: str,
        original_text: str,
    ) -> None:
        cls._validate_state_shape(state)
        if state["item_id"] != item_id:
            raise ValueError("item_state.json item_id does not match requested item")
        if state["mode"] != mode:
            raise ValueError("item_state.json mode does not match requested mode")
        if state["original_text"] != original_text:
            raise ValueError("item_state.json original_text does not match requested item")

    def _ensure_work_root(self) -> None:
        work = self.work_root
        work.mkdir(parents=True, exist_ok=True)

    def _touch_state(self) -> None:
        state = dict(self._state)
        if set(state) == _BASE_STATE_FIELDS:
            state.update(self._execution_defaults())
        state["updated_at"] = _now()
        self._persist_state(state, touch=False)

    def _persist_state(self, state: Mapping[str, Any], *, touch: bool = True) -> None:
        candidate = copy.deepcopy(dict(state))
        if touch:
            candidate["updated_at"] = _now()
        self._validate_state_shape(candidate)
        _atomic_write_json(self._resolve_item_subpath(_STATE_FILENAME), candidate)
        self._state = candidate

    @staticmethod
    def _progress_from_dict(value: Mapping[str, Any]) -> ArtifactProgress:
        _validate_progress_mapping(value, label="attempt baseline")
        return ArtifactProgress(
            files=tuple(value["files"]),
            hashes=dict(value["hashes"]),
            finding_count=value["finding_count"],
            source_map_count=value["source_map_count"],
            script_count=value["script_count"],
            draft_count=value["draft_count"],
            handoff_present=value["handoff_present"],
        )

    @staticmethod
    def _changed_files(before: ArtifactProgress, after: ArtifactProgress) -> tuple[str, ...]:
        paths = sorted(set(before.hashes) | set(after.hashes))
        return tuple(path for path in paths if before.hashes.get(path) != after.hashes.get(path))

    def _draft_hash(self) -> str:
        draft = self.draft_root
        _assert_regular_no_symlink(draft, label="draft artifact")
        if not draft.is_file():
            raise FileNotFoundError(draft)
        return _sha256_file(draft)

    @staticmethod
    def _pending_review() -> dict[str, Any]:
        return {
            "status": "pending",
            "strength": None,
            "verdict": None,
            "reviewer_ref": None,
            "draft_hash": None,
        }

    def _attempt_record(self, attempt_id: str) -> tuple[int, dict[str, Any]]:
        for index, record in enumerate(self._state["attempts"]):
            if record.get("attempt_id") == attempt_id:
                return index, record
        raise ValueError(f"unknown attempt_id: {attempt_id}")

    def _active_record(self, attempt_id: str) -> tuple[int, dict[str, Any]]:
        active = self._state.get("active_attempt_id")
        if active != attempt_id:
            raise ValueError("attempt is not active")
        index, record = self._attempt_record(attempt_id)
        if record.get("status") != "active":
            raise ValueError("attempt is not active")
        return index, record

    def _require_no_active_attempt(self) -> None:
        if self._state.get("active_attempt_id") is not None:
            raise ValueError("operation requires no active attempt")

    def _ensure_not_terminal(self) -> None:
        if self._state.get("lifecycle_state") in {"accepted", "technical_failure"}:
            raise ValueError("item is terminal")

    def _next_attempt_id(self) -> str:
        used = {str(record.get("attempt_id")) for record in self._state["attempts"]}
        sequence = 1
        for value in used:
            if value.startswith("A-") and value[2:].isdigit():
                sequence = max(sequence, int(value[2:]) + 1)
        while f"A-{sequence:03d}" in used:
            sequence += 1
        return f"A-{sequence:03d}"

    def _emit(
        self,
        event_type: str,
        *,
        artifact: str | None = None,
        progress: ArtifactProgress | None = None,
        **metadata: Any,
    ) -> None:
        if self.telemetry is None:
            return
        facts: dict[str, Any] = {"item_id": self.item_id, "mode": self.mode}
        if artifact is not None:
            facts["artifact"] = artifact
        if progress is not None:
            snapshot = progress.to_dict()
            facts["artifact_files"] = snapshot["files"]
            facts["artifact_hashes"] = snapshot["hashes"]
            facts["finding_count"] = snapshot["finding_count"]
            facts["source_map_count"] = snapshot["source_map_count"]
            facts["script_count"] = snapshot["script_count"]
            facts["draft_count"] = snapshot["draft_count"]
            facts["handoff_present"] = snapshot["handoff_present"]
        for key, value in metadata.items():
            # Raw error/reason text can contain rows or user content.  Keep
            # only its presence in passive telemetry.
            if key in {"error", "reason"}:
                facts[f"{key}_present"] = value is not None
            else:
                facts[key] = value
        try:
            self.telemetry.record(event_type, facts=facts)
        except Exception:
            # Telemetry is observational only and never controls persistence.
            pass

    def _write_json_artifact(self, relative: str, value: Any, *, event_type: str = "item_workspace_write") -> Path:
        self._ensure_not_terminal()
        destination = self._resolve_item_subpath(relative)
        _assert_regular_no_symlink(destination, label="item artifact")
        _atomic_write_json(destination, value)
        if Path(relative).as_posix() == _DRAFT_FILENAME:
            state = copy.deepcopy(self._state)
            if set(state) == _BASE_STATE_FIELDS:
                state.update(self._execution_defaults())
            state["review"] = self._pending_review()
            state["lifecycle_state"] = "work"
            state["updated_at"] = _now()
            self._persist_state(state, touch=False)
        else:
            self._touch_state()
        self._emit(event_type, artifact=Path(relative).as_posix())
        return destination

    def _invalidate_review_for_draft_mutation(self) -> None:
        review = self._state.get("review")
        if not isinstance(review, Mapping) or review.get("status") == "pending":
            return
        state = dict(self._state)
        state["review"] = self._pending_review()
        state["lifecycle_state"] = "work"
        self._persist_state(state)

    @property
    def item_root(self) -> Path:
        return self._resolve_item_subpath()

    @property
    def work_root(self) -> Path:
        return self._resolve_item_subpath("work")

    @property
    def draft_root(self) -> Path:
        return self._resolve_item_subpath(_DRAFT_FILENAME)

    @property
    def accepted_root(self) -> Path:
        return self._resolve_item_subpath(_ACCEPTED_FILENAME)

    @property
    def state(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    def write_plan(self, mapping: Mapping[str, Any]) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError("plan must be a mapping")
        self._write_json_artifact(Path("work") / _PLAN_FILENAME, mapping)

    def append_source_map(self, mapping: Mapping[str, Any]) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError("source map entry must be a mapping")
        self._ensure_not_terminal()
        destination = self._resolve_item_subpath(Path("work") / _SOURCE_MAP_FILENAME)
        _assert_regular_no_symlink(destination, label="source map artifact")
        if destination.exists():
            try:
                current = json.loads(destination.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("source_map.json is not valid JSON") from exc
            if not isinstance(current, list):
                raise ValueError("source_map.json must contain a JSON array")
            entries = list(current)
        else:
            entries = []
        entries.append(_jsonable(mapping))
        _atomic_write_json(destination, entries)
        self._touch_state()
        self._emit("item_workspace_append", artifact="work/source_map.json")

    def append_finding(self, mapping: Mapping[str, Any]) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError("finding must be a mapping")
        self._ensure_not_terminal()
        destination = self._resolve_item_subpath(Path("work") / _FINDINGS_FILENAME)
        _assert_regular_no_symlink(destination, label="findings artifact")
        _append_jsonl(destination, mapping)
        self._touch_state()
        self._emit("item_workspace_append", artifact="work/findings.jsonl")

    def write_open_issues(self, value: Any) -> None:
        self._write_json_artifact(Path("work") / _OPEN_ISSUES_FILENAME, value)

    def write_handoff(self, value: Any) -> None:
        self._write_json_artifact(Path("work") / _HANDOFF_FILENAME, value)

    def write_draft(self, value: Any) -> None:
        self._write_json_artifact(Path(_DRAFT_FILENAME), value)

    def _enumerate_work_artifacts(self, work: Path) -> tuple[list[str], dict[str, str]]:
        files: list[str] = []
        hashes: dict[str, str] = {}
        if not work.exists():
            return files, hashes
        _assert_regular_no_symlink(work, label="work root")
        item_root = self.item_root
        for base, directories, names in os.walk(work, followlinks=False):
            directories[:] = sorted(name for name in directories if not _is_temp_name(name))
            for name in sorted(names):
                if _is_temp_name(name):
                    continue
                path = Path(base) / name
                if path.is_symlink():
                    raise AllowedRootError(f"item artifact cannot be a symlink: {path}")
                if not path.is_file():
                    continue
                relative_path = path.relative_to(item_root)
                relative = relative_path.as_posix()
                if relative == _STATE_FILENAME or "telemetry" in relative_path.parts:
                    continue
                files.append(relative)
                hashes[relative] = _sha256_file(path)
        return files, hashes

    def _enumerate_draft_artifact(self, payload: bytes | None = None) -> tuple[list[str], dict[str, str], Path]:
        draft = self.draft_root
        files: list[str] = []
        hashes: dict[str, str] = {}
        if draft.exists():
            _assert_regular_no_symlink(draft, label="draft artifact")
            if draft.is_file():
                relative = draft.relative_to(self.item_root).as_posix()
                files.append(relative)
                hashes[relative] = _sha256_bytes(payload) if payload is not None else _sha256_file(draft)
        return files, hashes, draft

    @staticmethod
    def _script_count(calculations: Path) -> int:
        if not calculations.exists():
            return 0
        _assert_regular_no_symlink(calculations, label="calculations root")
        count = 0
        for base, directories, names in os.walk(calculations, followlinks=False):
            directories[:] = sorted(name for name in directories if not _is_temp_name(name))
            for name in sorted(names):
                if _is_temp_name(name):
                    continue
                script = Path(base) / name
                if script.is_symlink():
                    raise AllowedRootError(f"calculation cannot be a symlink: {script}")
                if script.is_file() and (script.suffix.lower() in _SCRIPT_SUFFIXES or not script.suffix):
                    count += 1
        return count

    def _artifact_counts(self, work: Path, draft: Path) -> tuple[int, int, int, bool]:
        findings = _count_jsonl(work / _FINDINGS_FILENAME)
        source_maps = _count_source_map(work / _SOURCE_MAP_FILENAME)
        draft_count = int(draft.is_file())
        handoff = work / _HANDOFF_FILENAME
        handoff_present = handoff.is_file() and handoff.stat().st_size > 0
        return findings, source_maps, draft_count, handoff_present

    def _artifact_progress(self, draft_payload: bytes | None = None) -> ArtifactProgress:
        self._reject_existing_symlink_components(self.context, self.item_root)
        work = self.work_root
        files, hashes = self._enumerate_work_artifacts(work)
        draft_files, draft_hashes, draft = self._enumerate_draft_artifact(draft_payload)
        files.extend(draft_files)
        hashes.update(draft_hashes)
        files = sorted(dict.fromkeys(files))
        hashes = {relative: hashes[relative] for relative in files}
        scripts = self._script_count(work / "calculations")
        findings, source_maps, draft_count, handoff_present = self._artifact_counts(work, draft)
        progress = ArtifactProgress(
            files=tuple(files),
            hashes=hashes,
            finding_count=findings,
            source_map_count=source_maps,
            script_count=scripts,
            draft_count=draft_count,
            handoff_present=handoff_present,
        )
        self._emit("item_workspace_progress", progress=progress)
        return progress

    def artifact_progress(self) -> ArtifactProgress:
        return self._artifact_progress()

    def begin_attempt(self, lane_id: str, role: str, *, route: str = "lead") -> ExecutionAttempt:
        """Start one deterministic attempt from the current artifact baseline."""

        self._ensure_execution_state()
        self._ensure_not_terminal()
        lane_id = str(lane_id).strip()
        role = str(role).strip()
        route = str(route).strip()
        if not lane_id or not role or not route:
            raise ValueError("lane_id, role, and route must be non-empty")
        if self._state.get("active_attempt_id") is not None:
            raise ValueError("an attempt is already active")
        if self._state.get("lifecycle_state") == "recovery_ready":
            raise ValueError("recovery_ready requires begin_recovery")
        baseline = self.artifact_progress()
        attempt = ExecutionAttempt(self._next_attempt_id(), lane_id, role, route, "active", baseline)
        state = dict(self._state)
        state["attempts"] = [dict(record) for record in self._state["attempts"]]
        record = attempt.to_dict()
        record.update({"prior_attempt_id": None, "handoff_ref": None, "error": None})
        state["attempts"].append(record)
        state["active_attempt_id"] = attempt.attempt_id
        state["consecutive_no_progress"] = 0
        state["lifecycle_state"] = "work"
        self._persist_state(state)
        self._emit(
            "item_attempt_started",
            progress=baseline,
            attempt_id=attempt.attempt_id,
            lane_id=lane_id,
            role=role,
            route=route,
            status="started",
        )
        return attempt

    def observe_attempt(self, attempt_id: str) -> ProgressDecision:
        """Compare current artifacts with the attempt's last observed baseline."""

        self._ensure_execution_state()
        self._ensure_not_terminal()
        attempt_id = str(attempt_id).strip()
        index, record = self._active_record(attempt_id)
        baseline = self._progress_from_dict(record["baseline"])
        progress = self.artifact_progress()
        changed_files = self._changed_files(baseline, progress)
        state = dict(self._state)
        state["attempts"] = [dict(item) for item in self._state["attempts"]]
        state["attempts"][index] = dict(record)
        if progress.materially_changed(baseline):
            action = "continue"
            state["consecutive_no_progress"] = 0
            state["lifecycle_state"] = "work"
            state["attempts"][index]["baseline"] = progress.to_dict()
        else:
            consecutive = int(self._state["consecutive_no_progress"]) + 1
            state["consecutive_no_progress"] = consecutive
            if consecutive == 1:
                action = "materialize_now"
            else:
                action = "recover"
                state["lifecycle_state"] = "recovery_ready"
        self._persist_state(state)
        decision = ProgressDecision(action, progress, changed_files)
        self._emit(
            "item_artifact_progress",
            progress=progress,
            attempt_id=attempt_id,
            action=action,
            consecutive_no_progress=state["consecutive_no_progress"],
        )
        return decision

    def finish_attempt(self, attempt_id: str, *, status: str, error: str | None = None) -> ExecutionAttempt:
        """Durably close an active attempt without changing repair counts."""

        self._ensure_execution_state()
        self._ensure_not_terminal()
        attempt_id = str(attempt_id).strip()
        status = str(status).strip()
        if not status or status == "active":
            raise ValueError("finished attempt status must be non-active")
        index, record = self._active_record(attempt_id)
        state = dict(self._state)
        state["attempts"] = [dict(item) for item in self._state["attempts"]]
        state["attempts"][index] = dict(record)
        state["attempts"][index]["status"] = status
        if error is not None:
            state["attempts"][index]["error"] = str(error)
        state["active_attempt_id"] = None
        if state.get("lifecycle_state") != "recovery_ready":
            state["lifecycle_state"] = "work"
        self._persist_state(state)
        attempt = ExecutionAttempt(
            record["attempt_id"],
            record["lane_id"],
            record["role"],
            record["route"],
            status,
            self._progress_from_dict(record["baseline"]),
        )
        self._emit(
            "item_attempt_finished",
            attempt_id=attempt_id,
            status=status,
            error=error,
            error_present=error is not None,
        )
        return attempt

    def begin_recovery(self, lane_id: str, role: str, *, prior_attempt_id: str) -> ExecutionAttempt:
        """Start a recovery attempt after the second consecutive no-progress observation."""

        self._ensure_execution_state()
        self._ensure_not_terminal()
        if self._state.get("lifecycle_state") != "recovery_ready":
            raise ValueError("begin_recovery requires recovery_ready")
        prior_attempt_id = str(prior_attempt_id).strip()
        if self._state.get("active_attempt_id") != prior_attempt_id:
            raise ValueError("begin_recovery requires the current active attempt")
        prior_index, prior = self._attempt_record(prior_attempt_id)
        if prior.get("status") != "active":
            raise ValueError("begin_recovery requires the current active attempt")
        lane_id = str(lane_id).strip()
        role = str(role).strip()
        if not lane_id or not role:
            raise ValueError("lane_id and role must be non-empty")
        baseline = self.artifact_progress()
        attempt = ExecutionAttempt(self._next_attempt_id(), lane_id, role, "recovery", "active", baseline)
        handoff = self.work_root / _HANDOFF_FILENAME
        handoff_ref = "work/handoff.json" if handoff.is_file() else None
        state = dict(self._state)
        state["attempts"] = [dict(item) for item in self._state["attempts"]]
        state["attempts"][prior_index]["status"] = "recovered"
        recovery_record = attempt.to_dict()
        recovery_record["prior_attempt_id"] = prior_attempt_id
        recovery_record["handoff_ref"] = handoff_ref
        recovery_record["error"] = None
        state["attempts"].append(recovery_record)
        state["active_attempt_id"] = attempt.attempt_id
        state["consecutive_no_progress"] = 0
        state["execution_recovery_count"] = int(self._state["execution_recovery_count"]) + 1
        state["lifecycle_state"] = "recovering"
        self._persist_state(state)
        self._emit(
            "item_recovery_started",
            progress=baseline,
            attempt_id=attempt.attempt_id,
            prior_attempt_id=prior_attempt_id,
            lane_id=lane_id,
            role=role,
            route="recovery",
            handoff_ref=handoff_ref,
        )
        return attempt

    def record_review(
        self,
        verdict: str,
        *,
        reviewer_ref: str | None = None,
        review_status: str = "reviewed",
    ) -> dict[str, Any]:
        """Persist a reviewer result, including explicit unavailability disclosure."""

        self._ensure_execution_state()
        self._ensure_not_terminal()
        self._require_no_active_attempt()
        draft_hash = self._draft_hash()
        status = str(review_status).strip()
        verdict = str(verdict).strip()
        if status in {"unavailable", "not_reviewed"}:
            if reviewer_ref is not None:
                raise ValueError("unavailable review cannot have reviewer_ref")
            if verdict not in {"none", "not_reviewed"}:
                raise ValueError("unavailable review must disclose not_reviewed")
            review = {
                "status": "unavailable",
                "strength": "none",
                "verdict": "not_reviewed",
                "reviewer_ref": None,
                "draft_hash": draft_hash,
            }
        elif status in {"reviewed", "available"}:
            if verdict not in _REVIEW_VERDICTS:
                raise ValueError("review verdict is invalid")
            if reviewer_ref is not None and not str(reviewer_ref).strip():
                raise ValueError("reviewer_ref must be non-empty")
            review = {
                "status": "reviewed",
                "strength": "independent" if reviewer_ref else None,
                "verdict": verdict,
                "reviewer_ref": str(reviewer_ref).strip() if reviewer_ref is not None else None,
                "draft_hash": draft_hash,
            }
        else:
            raise ValueError("review_status is invalid")
        state = dict(self._state)
        state["review"] = review
        state["lifecycle_state"] = "review"
        self._persist_state(state)
        self._emit(
            "item_review_recorded",
            review_status=review["status"],
            review_strength=review["strength"],
            verdict=review["verdict"],
            reviewer_ref=review["reviewer_ref"],
            draft_hash=draft_hash,
        )
        return dict(review)

    def use_business_repair(self) -> None:
        """Consume the one bounded business repair and require a fresh review."""

        self._ensure_execution_state()
        self._ensure_not_terminal()
        if int(self._state["business_repair_count"]) >= 1:
            raise ValueError("only one business repair is allowed")
        review = self._state["review"]
        if review.get("verdict") != "repair_once":
            raise ValueError("business repair requires a repair_once review verdict")
        state = dict(self._state)
        state["business_repair_count"] = int(self._state["business_repair_count"]) + 1
        state["review"] = self._execution_defaults()["review"]
        state["lifecycle_state"] = "work"
        self._persist_state(state)
        self._emit("item_business_repair_used", repair_count=state["business_repair_count"])

    def _publish_accepted_directory(self, files: Mapping[str, bytes], manifest: Mapping[str, Any]) -> Path:
        self._reject_existing_symlink_components(self.context, self.item_root)
        accepted = self.accepted_root
        if accepted.exists() or accepted.is_symlink():
            raise FileExistsError(accepted)
        temporary = Path(tempfile.mkdtemp(prefix=f".{accepted.name}.tmp-", dir=self.item_root))
        try:
            for relative, payload in files.items():
                relative_path = PurePath(relative)
                if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
                    raise ValueError("accepted snapshot file path is invalid")
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(destination, payload)
            _atomic_write_json(temporary / "manifest.json", manifest)
            os.replace(temporary, accepted)
            _fsync_directory(self.item_root)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return accepted / "manifest.json"

    def accept(
        self,
        *,
        knowledge_delta: str = "no_change",
        accepted_refs: tuple[str, ...] = (),
    ) -> AcceptedSnapshot:
        """Publish exactly the bytes of the currently reviewed draft.

        The host supplies only acceptance metadata.  Passing a separate final
        answer would permit publication of content that was never reviewed, so
        the canonical ``draft.json`` bytes are copied verbatim.
        """

        self._ensure_execution_state()
        if self.accepted_root.exists() or self.accepted_root.is_symlink():
            raise FileExistsError(self.accepted_root)
        self._ensure_not_terminal()
        self._require_no_active_attempt()
        draft_path = self.draft_root
        _assert_regular_no_symlink(draft_path, label="draft artifact")
        if not draft_path.is_file():
            raise FileNotFoundError(draft_path)
        payload = draft_path.read_bytes()
        content_hash = _sha256_bytes(payload)
        review = self._state["review"]
        accepted_review = (
            review.get("status") == "reviewed" and review.get("verdict") in {"accept", "accept_with_limits"}
        ) or (review.get("status") == "unavailable" and review.get("verdict") == "not_reviewed")
        if not accepted_review:
            raise ValueError("accept requires accept/accept_with_limits or unavailable/not_reviewed review")
        if review.get("draft_hash") != content_hash:
            raise ValueError("accept requires the exact currently reviewed draft")
        knowledge_delta = str(knowledge_delta).strip()
        if knowledge_delta not in _KNOWLEDGE_DELTAS:
            raise ValueError("knowledge_delta is invalid")
        refs = tuple(str(ref) for ref in accepted_refs)
        if any(not ref for ref in refs):
            raise ValueError("accepted_refs must be non-empty strings")
        final_name = "final_answer.json"
        progress = self._artifact_progress(payload)
        manifest = {
            "item_id": self.item_id,
            "outcome": "accepted",
            "final_answer": final_name,
            "content_hash": content_hash,
            "draft_hash": content_hash,
            "hashes": dict(progress.hashes),
            "artifact_progress": progress.to_dict(),
            "refs": list(refs),
            "accepted_refs": list(refs),
            "knowledge_delta": knowledge_delta,
        }
        manifest["manifest_hash"] = _manifest_hash(manifest)
        intent_state = dict(self._state)
        intent_state["terminal_intent"] = {"outcome": "accepted", "manifest_hash": manifest["manifest_hash"]}
        self._persist_state(intent_state)
        manifest_path = self._publish_accepted_directory({final_name: payload}, manifest)
        snapshot = AcceptedSnapshot(self.item_id, "accepted", str(manifest_path), content_hash)
        state = dict(self._state)
        state["lifecycle_state"] = "accepted"
        state["terminal_outcome"] = {"status": "accepted", **snapshot.to_dict()}
        self._persist_state(state)
        self._emit("item_accepted", outcome="accepted", manifest_path=str(manifest_path), content_hash=content_hash)
        return snapshot

    def technical_failure(self, reason: str, *, recovery_exhausted: bool) -> AcceptedSnapshot:
        """Publish a terminal workflow failure only after recovery exhaustion."""

        if recovery_exhausted is not True:
            raise ValueError("technical_failure requires recovery_exhausted=True")
        self._ensure_execution_state()
        if self.accepted_root.exists() or self.accepted_root.is_symlink():
            raise FileExistsError(self.accepted_root)
        self._ensure_not_terminal()
        self._require_no_active_attempt()
        reason = str(reason)
        if not reason:
            raise ValueError("technical_failure reason must be non-empty")
        progress = self.artifact_progress()
        refs = ["work/handoff.json"] if progress.handoff_present else []
        unsigned = {
            "item_id": self.item_id,
            "outcome": "technical_failure",
            "reason": reason,
            "recovery_exhausted": True,
            "hashes": dict(progress.hashes),
            "artifact_progress": progress.to_dict(),
            "refs": refs,
        }
        content_hash = _sha256_bytes(_json_bytes(unsigned))
        manifest = {**unsigned, "content_hash": content_hash}
        manifest["manifest_hash"] = _manifest_hash(manifest)
        intent_state = dict(self._state)
        intent_state["terminal_intent"] = {"outcome": "technical_failure", "manifest_hash": manifest["manifest_hash"]}
        self._persist_state(intent_state)
        manifest_path = self._publish_accepted_directory({}, manifest)
        snapshot = AcceptedSnapshot(self.item_id, "technical_failure", str(manifest_path), content_hash)
        state = dict(self._state)
        state["lifecycle_state"] = "technical_failure"
        state["terminal_outcome"] = {"status": "technical_failure", **snapshot.to_dict()}
        self._persist_state(state)
        self._emit(
            "item_technical_failure",
            outcome="technical_failure",
            reason=reason,
            manifest_path=str(manifest_path),
            content_hash=content_hash,
        )
        return snapshot


__all__ = [
    "AcceptedSnapshot",
    "ArtifactProgress",
    "ExecutionAttempt",
    "ITEM_STATE_FIELDS",
    "ITEM_STATE_SCHEMA",
    "ItemWorkspace",
    "ProgressDecision",
]
