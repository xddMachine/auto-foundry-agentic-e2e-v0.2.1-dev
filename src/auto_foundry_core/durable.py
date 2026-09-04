"""Small durable item workspaces for offline, incremental analysis.

The module owns the item-local durable contract.  It creates a bounded item
directory before an agent is invoked, persists incrementally written artifacts,
and records deterministic execution/review/terminal transitions.  It never
executes models or scripts itself.

Canonical extended ``item_state.json`` keys are exposed as ``ITEM_STATE_FIELDS``
and ``ITEM_STATE_SCHEMA`` below.  A freshly-created workspace deliberately
writes only the eight base fields; the first layer-2 operation migrates it to
the extended shape in place.  Analytical acceptance publishes an immutable
``accepted/`` directory containing the exact reviewed bytes, a separate
acceptance envelope, and a hash-bound manifest; integration status remains a
program-owned state field rather than business content.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path, PurePath
import shutil
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import IncidentRecord
from .workspace import AllowedRootError, RunContext

try:  # pragma: no cover - POSIX hosts provide advisory flock
    import fcntl
except ImportError:  # pragma: no cover - defensive non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


_VALID_MODES = frozenset({"question", "requirement"})
_STATE_FILENAME = "item_state.json"
_PLAN_FILENAME = "plan.json"
_SOURCE_MAP_FILENAME = "source_map.json"
_SOURCE_MAP_FIELDS = frozenset(
    {
        "record_kind",
        "source_id",
        "purpose",
        "path",
        "content_hash",
        "columns",
        "row_count",
        "row_count_exact",
    }
)
_FINDINGS_FILENAME = "findings.jsonl"
_EVIDENCE_FILENAME = "evidence.jsonl"
_SPECIALIST_TASKS_FILENAME = "specialist_tasks.jsonl"
_SPECIALIST_MEMOS_FILENAME = "specialist_memos.jsonl"
_SEMANTIC_SELECTIONS_FILENAME = "semantic_selections.jsonl"
_IDENTITY_DOMAIN_PROPOSALS_FILENAME = "identity_domain_proposals.jsonl"
_IDENTITY_DOMAIN_PROPOSAL_BASE_FIELDS = frozenset(
    {
        "record_kind",
        "domain_id",
        "object_type",
        "rationale",
        "source_hints",
        "representation_item_ids",
    }
)
_IDENTITY_DOMAIN_PROPOSAL_REVISION_FIELDS = frozenset(
    {"revision", "supersedes_hash", "proposal_hash", "superseded_object_type"}
)
_ANALYTICAL_RELATIONSHIPS_FILENAME = "analytical_relationships.jsonl"
_OPEN_ISSUES_FILENAME = "open_issues.json"
_HANDOFF_FILENAME = "handoff.json"
_HANDOFF_ARTIFACT_PATH = (Path("work") / _HANDOFF_FILENAME).as_posix()
_ANALYST_HANDOFF_SCHEMA = "auto_foundry.analyst_handoff.v1"
_ANALYST_HANDOFF_FIELDS = frozenset(
    {
        "analysis_output_summary",
        "analysis_status",
        "attempt_id",
        "business_repair_count",
        "calculation_outputs",
        "calculation_script",
        "evidence_refs",
        "freeze_note",
        "item_id",
        "limits",
        "output_hashes",
        "owner_ref",
        "receipt_hashes",
        "repair_finding_id",
        "requirement",
        "review_status",
        "schema_version",
    }
)
_ANALYST_HANDOFF_RESERVED_REFS = frozenset(
    {
        "item_state.json",
        "work/analysis_owner.json",
        "work/business_review.json",
        "work/data_insufficiency_conclusion.json",
        _HANDOFF_ARTIFACT_PATH,
    }
)
_DRAFT_FILENAME = "draft.json"
_BUSINESS_REVIEW_FILENAME = "business_review.json"
# ``draft.json`` is an AnalystAnswer envelope.  During an authorized business
# repair the reviewer still owns the immutable metadata envelope (schema
# version and item identity), while the analytical owner may revise any
# answer-facing section.  These names are deliberately independent from the
# review finding categories: categories describe reviewer evidence, not write
# capabilities.
_ANSWER_DRAFT_SECTIONS = frozenset(
    {
        "answer",
        "headline_findings",
        "scope",
        "method",
        "supported_components",
        "unsupported_components",
        "limitations",
        "next_actions",
        "visuals",
        "evidence_refs",
    }
)
_DRAFT_IMMUTABLE_FIELDS = frozenset({"schema_version", "item_id"})
_DATA_INSUFFICIENCY_FILENAME = "data_insufficiency_conclusion.json"
_ANALYSIS_OWNER_FILENAME = "analysis_owner.json"
_PROGRAM_CONTEXT_ARTIFACT_PATHS = frozenset(
    {
        "work/analysis_context.json",
        "work/analysis_context_transitions.jsonl",
        "work/analysis_context_transition_state.json",
        "work/analysis_context_transition_intent.json",
        "work/analysis_context_repair_upgrades.jsonl",
        "work/analysis_context_repair_upgrade_intent.json",
    }
)
_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME = "business_review_discard_audit.jsonl"
_BUSINESS_REVIEW_DISCARD_STATE_FILENAME = "business_review_discard_state.json"
_ITEM_STATE_TRANSITION_LOCK_FILENAME = ".item_state_transition.lock"
_ITEM_STATE_TRANSITION_LOCK_NAMESPACE = Path(".locks") / "item_state"
_HELD_ITEM_LOCKS = threading.local()


@dataclass(frozen=True)
class _IdentityDomainProposalNode:
    """Minimal digest node retained for legacy malformed proposal rows."""

    domain_id: str
    object_type: str
    rationale: str
    source_hints: tuple[Any, ...]
    representation_item_ids: tuple[str, ...]
    revision: int
    supersedes_hash: str | None
    superseded_object_type: str | None
    digest: str
_BUSINESS_REVIEW_DISCARD_AUDIT_FIELDS = frozenset(
    {
        "record_kind",
        "audit_id",
        "audit_path",
        "item_id",
        "incident",
        "discarded_packet_hash",
        "draft_hash",
        "previous_audit_hash",
        "audit_hash",
    }
)
_BUSINESS_REVIEW_DISCARD_STATE_FIELDS = frozenset(
    {
        "record_kind",
        "item_id",
        "audit_path",
        "audit_count",
        "audit_head",
        "intent",
        "state_hash",
    }
)
_BUSINESS_REVIEW_DISCARD_INTENT_FIELDS = frozenset(
    {
        "operation_id",
        "target",
        "packet_path",
        "audit_path",
        "incident",
        "discarded_packet_hash",
        "draft_hash",
        "prior_audit_count",
        "prior_audit_head",
        "expected_audit_count",
        "expected_audit_head",
        "expected_audit",
        "before_state_hash",
        "after_state_hash",
        "after_state",
        "phase",
        "intent_hash",
    }
)
_DATA_INSUFFICIENCY_FIELDS = frozenset(
    {
        "record_kind",
        "item_id",
        "mode",
        "original_text_hash",
        "draft_hash",
        "artifact_progress_hash",
        "reason",
        "unanswerable_component",
        "missing_information",
        "searches_performed",
        "evidence_refs",
        "supported_components",
    }
)
_ANALYSIS_OWNER_FIELDS = frozenset(
    {
        "record_kind",
        "item_id",
        "mode",
        "original_text_hash",
        "owner_ref",
        "owner_hash",
    }
)
_ACCEPTED_FILENAME = "accepted"
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
        "integration_state",
        "integration_manifest_hash",
        "integration_manifest_ref",
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
    "integration_state",
    "integration_manifest_hash",
    "integration_manifest_ref",
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
    "recovery_receipt_ref",
    "recovery_invocation_id",
    "recovery_receipt_hash",
)
_REVIEW_FIELDS = frozenset({"status", "strength", "verdict", "reviewer_ref", "draft_hash"})
_REVIEW_VERDICTS = frozenset({"accept", "accept_with_limits", "repair_once", "confirm_data_insufficiency"})
# Semantic categories are persisted with each finding and are the sole source
# for its program-derived dependency/artifact scope.
_REPAIR_CATEGORY_ORDER = (
    "answer",
    "calculation",
    "evidence",
    "method",
    "source_completeness",
    "presentation",
)
_REPAIR_EVIDENCE_BINDING_CATEGORIES = frozenset({"evidence", "source_completeness"})
# A visual repair that combines calculation and presentation is allowed to
# bind the evidence record produced by that recomputation.  This is narrower
# than granting every calculation/presentation repair the whole evidence-ref
# field, and leaves ordinary category scopes unchanged.
_REPAIR_VISUAL_EVIDENCE_BINDING_CATEGORIES = frozenset({"calculation", "presentation"})
_REPAIR_ANSWER_BINDING_CATEGORIES = frozenset({"answer", "presentation", "calculation"})
_REPAIR_CATEGORY_DEPENDENCIES = MappingProxyType(
    {
        # The owner-produced reviewer packet is an exact answer dependency.
        # It is deliberately not the authoritative ``business_review.json``
        # packet and does not authorize the surrounding work/ directory.
        "answer": ("work/business_review_packet.json",),
        "calculation": (
            "work/calculations",
            "work/analysis.py",
            "work/analysis.json",
            "work/prepared",
            "work/evidence.jsonl",
            "work/.analysis-run",
            "work/script_receipts",
        ),
        "evidence": ("work/evidence.jsonl", "work/source_map.json", "work/specialist_memos.jsonl"),
        "method": (
            "work/plan.json",
            "work/calculations",
            "work/analysis.py",
            "work/analysis.json",
            "work/prepared",
            "work/evidence.jsonl",
            "work/source_map.json",
            "work/specialist_memos.jsonl",
            "work/.analysis-run",
            "work/script_receipts",
        ),
        "source_completeness": ("work/source_map.json", "work/evidence.jsonl", "work/specialist_memos.jsonl"),
        "presentation": (),
    }
)
_REPAIR_CATEGORY_ARTIFACT_PATHS = MappingProxyType(
    {
        "answer": ("work/results",),
        "presentation": ("work/results",),
        "calculation": ("work/results",),
        "evidence": (),
        "method": (),
        "source_completeness": (),
    }
)
_KNOWLEDGE_DELTAS = frozenset({"promoted", "promoted_with_limits", "no_change"})
_BLOCKED_REVIEW_OUTCOME = "blocked_by_evidence"
_CONFIRM_DATA_VERDICT = "confirm_data_insufficiency"
_LIFECYCLE_STATES = frozenset(
    {"work", "recovering", "recovery_ready", "review", "accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}
)
ITEM_STATE_SCHEMA = {
    "fields": ITEM_STATE_FIELDS,
    "attempt_fields": _ATTEMPT_FIELDS,
    "review_fields": tuple(sorted(_REVIEW_FIELDS)),
    "accepted_directory": "accepted/",
    "accepted_content": "accepted/answer_content.json",
    "acceptance_envelope": "accepted/acceptance_envelope.json",
    "accepted_manifest": "accepted/manifest.json",
    "business_review_artifact": "work/business_review.json",
    "data_insufficiency_artifact": "work/data_insufficiency_conclusion.json",
    "integration_fields": ("integration_state", "integration_manifest_hash", "integration_manifest_ref"),
    "terminal_intent_fields": ("outcome", "manifest_hash"),
}
_TERMINAL_FIELDS = frozenset({"status", "item_id", "outcome", "manifest_path", "content_hash"})
_TERMINAL_INTENT_FIELDS = frozenset({"outcome", "manifest_hash"})
_ACCEPTED_MANIFEST_FIELDS = frozenset(
    {
        "item_id",
        "outcome",
        "content_path",
        "content_hash",
        "envelope_path",
        "envelope_hash",
        "hashes",
        "artifact_progress",
        "manifest_hash",
    }
)
# Business acceptance seals the exact typed analytical artifacts named by the
# accepted answer.  The handoff lives in the acceptance envelope so the
# answer, envelope, and manifest can be published by the same atomic rename;
# no second mutable registry is needed between the reviewer and integration.
_ACCEPTED_ANALYTICAL_HANDOFF_SCHEMA = "auto_foundry.accepted_analytical_artifact_handoff.v1"
_ACCEPTED_ANALYTICAL_HANDOFF_FIELD = "analytical_artifact_handoff"
_ACCEPTED_ANALYTICAL_HANDOFF_FIELDS = frozenset({"schema_version", "artifacts", "handoff_hash"})
_ACCEPTED_ANALYTICAL_DESCRIPTOR_FIELDS = frozenset(
    {
        "ref",
        "hash",
        "artifact_id",
        "artifact_type",
        "schema_version",
        "requirement_id",
        "content_hash",
        "envelope_hash",
        "canonical_bytes_sha256",
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
_BLOCKED_MANIFEST_FIELDS = frozenset(
    {
        "item_id",
        "outcome",
        "reason",
        "draft_path",
        "draft_hash",
        "source_draft_path",
        "source_draft_hash",
        "business_review_path",
        "business_review_hash",
        "source_business_review_path",
        "source_business_review_hash",
        "data_insufficiency_path",
        "data_insufficiency_hash",
        "source_data_insufficiency_path",
        "source_data_insufficiency_hash",
        "review_status",
        "review_strength",
        "review_verdict",
        "reviewer_ref",
        "review_scope",
        "targeted_recheck",
        "repair_active",
        "reviewed_draft_hash",
        "finding_count",
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


def _held_item_lock_paths() -> set[str]:
    paths = getattr(_HELD_ITEM_LOCKS, "paths", None)
    if paths is None:
        paths = set()
        _HELD_ITEM_LOCKS.paths = paths
    return paths


@contextmanager
def _item_state_transition_lock(path: Path):
    """One process-wide/per-item advisory lock for state transitions.

    Callers acquire this after the transition-journal and lifecycle locks;
    nested calls in the same thread reuse the held lock path and therefore do
    not recurse into ``flock`` on a second file descriptor.
    """

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AllowedRootError("item state transition lock is not a regular file")
    key = str(path)
    held = _held_item_lock_paths()
    if key in held:
        yield
        return
    parent = path.parent
    while not parent.exists() and not parent.is_symlink() and parent != parent.parent:
        parent = parent.parent
    if parent.is_symlink() or not parent.is_dir():
        raise AllowedRootError("item state transition lock namespace is not a regular directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


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


def _owner_ref_value(value: Any) -> str:
    """Normalize one host-supplied Analytical Owner identity."""

    owner_ref = str(value).strip()
    if not owner_ref or "\x00" in owner_ref:
        raise ValueError("owner_ref must be a non-empty stable identifier")
    return owner_ref


def _validate_mode(mode: str) -> str:
    value = str(mode).strip()
    if value not in _VALID_MODES:
        raise ValueError("mode must be 'question' or 'requirement'")
    return value


def _state_transition_lock_path(context: RunContext, item_id: str, mode: str) -> Path:
    """Return the stable run-local lock path for one mode/item pair."""

    item_id = _simple_component(item_id, "item_id")
    mode = _validate_mode(mode)
    relative = _ITEM_STATE_TRANSITION_LOCK_NAMESPACE / mode / item_id / _ITEM_STATE_TRANSITION_LOCK_FILENAME
    current = context.run_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise AllowedRootError(f"item state transition lock path cannot use symlink: {current}")
    return context.resolve_run_path(relative)


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
            allow_nan=False,
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
    _fsync_directory(path.parent)


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


def _pointer_escape(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _pointer_join(parent: str, component: Any) -> str:
    return f"{parent}/{_pointer_escape(component)}" if parent else f"/{_pointer_escape(component)}"


def _pointer_hashes(value: Any, path: str = "") -> dict[str, str]:
    """Return canonical hashes for every structured JSON pointer node."""

    hashes = {_path: _sha256_bytes(_json_bytes(node)) for _path, node in ((path, value),)}
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            child = _pointer_join(path, key)
            hashes.update(_pointer_hashes(value[key], child))
    elif isinstance(value, list):
        for index, child_value in enumerate(value):
            hashes.update(_pointer_hashes(child_value, _pointer_join(path, index)))
    return hashes


def _pointer_diff(before: Any, after: Any, path: str = "") -> list[str]:
    """Return minimal changed JSON pointers without parsing prose."""

    if before == after and type(before) is type(after):
        return []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[str] = []
        keys = sorted(set(before) | set(after), key=str)
        for key in keys:
            child = _pointer_join(path, key)
            if key not in before or key not in after:
                changes.append(child)
            else:
                changes.extend(_pointer_diff(before[key], after[key], child))
        return changes or [path]
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        if len(before) != len(after):
            # The new/removed tail is represented at the exact array index;
            # existing positions are still compared structurally.
            for index in range(min(len(before), len(after))):
                changes.extend(_pointer_diff(before[index], after[index], _pointer_join(path, index)))
            for index in range(min(len(before), len(after)), max(len(before), len(after))):
                changes.append(_pointer_join(path, index))
            return changes or [path]
        for index, (left, right) in enumerate(zip(before, after)):
            changes.extend(_pointer_diff(left, right, _pointer_join(path, index)))
        return changes or [path]
    return [path]


_INVOCATION_RECEIPT_REF_PREFIX = "telemetry/invocation_receipts.jsonl#"


def _is_stable_receipt_ref(value: Any) -> bool:
    """Validate the exact run-local invocation ledger reference shape."""

    if not isinstance(value, str) or not value.startswith(_INVOCATION_RECEIPT_REF_PREFIX):
        return False
    invocation_id = value[len(_INVOCATION_RECEIPT_REF_PREFIX) :]
    return bool(invocation_id) and "#" not in invocation_id and "/" not in invocation_id and "\\" not in invocation_id


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
        if self.action not in {"continue", "materialize_now", "retry_same_attempt"}:
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
        with _item_state_transition_lock(_state_transition_lock_path(context, item_id, mode)):
            return cls._create_unlocked(
                context,
                item_id,
                mode=mode,
                original_text=original_text,
                telemetry=telemetry,
            )

    @classmethod
    def _create_unlocked(
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
            if (
                set(state) == _EXECUTION_STATE_FIELDS
                or workspace.accepted_root.exists()
                or workspace.accepted_root.is_symlink()
                or workspace.business_review_discard_state_path.exists()
                or workspace.business_review_discard_state_path.is_symlink()
                or workspace.business_review_discard_audit_path.exists()
                or workspace.business_review_discard_audit_path.is_symlink()
            ):
                workspace._ensure_execution_state()
                workspace._reconcile_business_review_discard()
                workspace._validate_recovery_authorizations()
                workspace._reconcile_review_draft()
                workspace._reconcile_terminal_snapshot()
            # A prior process may have been interrupted after state creation
            # but before work/ creation.  Re-establish the required workspace
            # directory without touching any user artifact.
            if workspace.state.get("lifecycle_state") not in {"accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
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
        with _item_state_transition_lock(_state_transition_lock_path(context, item_id, mode)):
            return cls._load_unlocked(context, item_id, mode=mode, telemetry=telemetry)

    @classmethod
    def _load_unlocked(
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
        workspace._reconcile_business_review_discard()
        workspace._validate_recovery_authorizations()
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
        if outcome not in {"accepted", "accepted_with_limits", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
            raise ValueError("accepted snapshot outcome is invalid")
        if outcome in {"accepted", "accepted_with_limits"}:
            expected_fields = _ACCEPTED_MANIFEST_FIELDS
        elif outcome == _BLOCKED_REVIEW_OUTCOME:
            expected_fields = _BLOCKED_MANIFEST_FIELDS
        else:
            expected_fields = _TECHNICAL_MANIFEST_FIELDS
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

    def _validate_blocked_manifest(self, manifest: Mapping[str, Any], files: set[str]) -> None:
        """Validate an immutable owner-data-insufficiency terminal snapshot."""

        if manifest.get("outcome") != _BLOCKED_REVIEW_OUTCOME:
            raise ValueError("blocked snapshot outcome is invalid")
        if manifest.get("reason") != "data_insufficiency":
            raise ValueError("blocked snapshot reason is invalid")
        expected_paths = {
            "draft_path": "reviewed_draft.json",
            "business_review_path": "business_review.json",
            "data_insufficiency_path": _DATA_INSUFFICIENCY_FILENAME,
            "source_draft_path": _DRAFT_FILENAME,
            "source_business_review_path": f"work/{_BUSINESS_REVIEW_FILENAME}",
            "source_data_insufficiency_path": f"work/{_DATA_INSUFFICIENCY_FILENAME}",
        }
        for field, expected in expected_paths.items():
            if manifest.get(field) != expected:
                raise ValueError(f"blocked snapshot {field} is invalid")
        for field in (
            "draft_hash",
            "source_draft_hash",
            "business_review_hash",
            "source_business_review_hash",
            "data_insufficiency_hash",
            "source_data_insufficiency_hash",
            "reviewed_draft_hash",
        ):
            if not _is_sha256(manifest.get(field)):
                raise ValueError(f"blocked snapshot {field} is invalid")
        if manifest.get("review_status") != "reviewed":
            raise ValueError("blocked snapshot review_status is invalid")
        if manifest.get("review_verdict") != _CONFIRM_DATA_VERDICT:
            raise ValueError("blocked snapshot review_verdict is invalid")
        if not isinstance(manifest.get("reviewer_ref"), str) or not manifest["reviewer_ref"].strip():
            raise ValueError("blocked snapshot reviewer_ref is invalid")
        state_review = self._state.get("review")
        if not isinstance(state_review, Mapping):
            raise ValueError("blocked snapshot review metadata is missing")
        for manifest_field, review_field in (
            ("review_status", "status"),
            ("review_strength", "strength"),
            ("review_verdict", "verdict"),
            ("reviewer_ref", "reviewer_ref"),
        ):
            if manifest.get(manifest_field) != state_review.get(review_field):
                raise ValueError("blocked snapshot review metadata is stale")
        if manifest.get("review_scope") not in {"full", "targeted"}:
            raise ValueError("blocked snapshot review_scope is invalid")
        if manifest.get("repair_active") is not False:
            raise ValueError("blocked snapshot review remains repair-active")
        finding_count = manifest.get("finding_count")
        if isinstance(finding_count, bool) or not isinstance(finding_count, int) or finding_count != 0:
            raise ValueError("blocked snapshot finding_count is invalid")
        refs = self._validate_manifest_refs(manifest.get("refs"), label="refs")
        if refs != [
            _DRAFT_FILENAME,
            f"work/{_BUSINESS_REVIEW_FILENAME}",
            f"work/{_DATA_INSUFFICIENCY_FILENAME}",
        ]:
            raise ValueError("blocked snapshot refs are invalid")
        self._validate_manifest_artifacts(manifest)
        if files != {
            "manifest.json",
            "reviewed_draft.json",
            "business_review.json",
            _DATA_INSUFFICIENCY_FILENAME,
        }:
            raise ValueError("blocked snapshot files are inconsistent")

        draft_copy = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / manifest["draft_path"])
        review_copy = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / manifest["business_review_path"])
        conclusion_copy = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / manifest["data_insufficiency_path"])
        source_draft = self._resolve_item_subpath(manifest["source_draft_path"])
        source_review = self._resolve_item_subpath(manifest["source_business_review_path"])
        source_conclusion = self._resolve_item_subpath(manifest["source_data_insufficiency_path"])
        for path, label in (
            (draft_copy, "blocked reviewed draft"),
            (review_copy, "blocked business review"),
            (conclusion_copy, "blocked data insufficiency conclusion"),
            (source_draft, "blocked source draft"),
            (source_review, "blocked source business review"),
            (source_conclusion, "blocked source data insufficiency conclusion"),
        ):
            _assert_regular_no_symlink(path, label=label)
            if not path.is_file():
                raise ValueError(f"{label} is missing")
        draft_copy_bytes = draft_copy.read_bytes()
        source_draft_bytes = source_draft.read_bytes()
        review_copy_bytes = review_copy.read_bytes()
        source_review_bytes = source_review.read_bytes()
        conclusion_copy_bytes = conclusion_copy.read_bytes()
        source_conclusion_bytes = source_conclusion.read_bytes()
        if _sha256_bytes(draft_copy_bytes) != manifest["draft_hash"]:
            raise ValueError("blocked reviewed draft hash does not match manifest")
        if _sha256_bytes(source_draft_bytes) != manifest["source_draft_hash"]:
            raise ValueError("blocked source draft hash does not match manifest")
        if _sha256_bytes(review_copy_bytes) != manifest["business_review_hash"]:
            raise ValueError("blocked business review hash does not match manifest")
        if _sha256_bytes(source_review_bytes) != manifest["source_business_review_hash"]:
            raise ValueError("blocked source business review hash does not match manifest")
        if _sha256_bytes(conclusion_copy_bytes) != manifest["data_insufficiency_hash"]:
            raise ValueError("blocked data insufficiency hash does not match manifest")
        if _sha256_bytes(source_conclusion_bytes) != manifest["source_data_insufficiency_hash"]:
            raise ValueError("blocked source data insufficiency hash does not match manifest")
        if (
            draft_copy_bytes != source_draft_bytes
            or review_copy_bytes != source_review_bytes
            or conclusion_copy_bytes != source_conclusion_bytes
        ):
            raise ValueError("blocked snapshot source and immutable copies differ")
        if manifest["draft_hash"] != manifest["source_draft_hash"]:
            raise ValueError("blocked snapshot draft hashes are inconsistent")
        if manifest["content_hash"] != manifest["draft_hash"]:
            raise ValueError("blocked snapshot content hash is not the reviewed draft hash")
        if manifest["reviewed_draft_hash"] != manifest["source_draft_hash"]:
            raise ValueError("blocked snapshot reviewed draft hash is inconsistent")
        progress = manifest["artifact_progress"]
        if progress["hashes"].get(_DRAFT_FILENAME) != manifest["source_draft_hash"]:
            raise ValueError("blocked snapshot artifact progress is not bound to the draft")

        try:
            review_value = json.loads(review_copy_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("blocked business review copy is invalid") from exc
        if not isinstance(review_value, Mapping):
            raise ValueError("blocked business review copy is invalid")
        self._validate_business_review_payload(review_value)
        if review_value.get("reviewed_draft_hash") != manifest["source_draft_hash"]:
            raise ValueError("blocked business review copy draft hash is stale")
        if review_value.get("repair_active") is not False:
            raise ValueError("blocked business review copy remains repair-active")
        if review_value.get("findings") != []:
            raise ValueError("blocked business review copy findings are invalid")
        try:
            conclusion_value = json.loads(conclusion_copy_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("blocked data insufficiency copy is invalid") from exc
        self._validate_data_insufficiency_binding(conclusion_value, require_current=False)
        if conclusion_value.get("draft_hash") != manifest["source_draft_hash"]:
            raise ValueError("blocked data insufficiency conclusion draft is stale")

    def _validate_terminal_manifest(self, manifest: Mapping[str, Any], files: set[str]) -> None:
        if _manifest_hash(manifest) != manifest["manifest_hash"]:
            raise ValueError("accepted snapshot manifest hash does not match content")
        if manifest.get("item_id") != self.item_id:
            raise ValueError("accepted snapshot item_id is invalid")
        outcome = manifest["outcome"]
        content_hash = manifest["content_hash"]
        if outcome in {"accepted", "accepted_with_limits"}:
            content_name = manifest["content_path"]
            if content_name != "answer_content.json":
                raise ValueError("accepted snapshot content_path is invalid")
            envelope_name = manifest["envelope_path"]
            if envelope_name != "acceptance_envelope.json":
                raise ValueError("accepted snapshot envelope_path is invalid")
            envelope_hash = manifest["envelope_hash"]
            if not _is_sha256(envelope_hash):
                raise ValueError("accepted snapshot envelope_hash is invalid")
            self._validate_manifest_artifacts(manifest)
            if files != {"manifest.json", content_name, envelope_name}:
                raise ValueError("accepted snapshot files are inconsistent")
            content_path = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / content_name)
            envelope_path = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / envelope_name)
            _assert_regular_no_symlink(content_path, label="accepted answer content")
            _assert_regular_no_symlink(envelope_path, label="acceptance envelope")
            if _sha256_file(content_path) != content_hash:
                raise ValueError("accepted answer content hash does not match manifest")
            if _sha256_file(envelope_path) != envelope_hash:
                raise ValueError("acceptance envelope hash does not match manifest")
            try:
                envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("acceptance envelope is invalid") from exc
            self._validate_acceptance_envelope(
                envelope,
                item_id=self.item_id,
                content_hash=content_hash,
                outcome=outcome,
            )
            self._validate_accepted_analytical_handoff(envelope, manifest)
            return
        if outcome == _BLOCKED_REVIEW_OUTCOME:
            self._validate_blocked_manifest(manifest, files)
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

    @staticmethod
    def _validate_analytical_handoff_shape(
        value: Any,
        *,
        item_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Validate the self-contained shape of an accepted artifact handoff.

        Filesystem binding is deliberately kept in the instance method below;
        this helper only validates deterministic wire fields so an envelope
        cannot smuggle arbitrary JSON into the integration boundary.
        """

        if not isinstance(value, Mapping) or set(value) != _ACCEPTED_ANALYTICAL_HANDOFF_FIELDS:
            raise ValueError("accepted analytical artifact handoff fields are invalid")
        if value.get("schema_version") != _ACCEPTED_ANALYTICAL_HANDOFF_SCHEMA:
            raise ValueError("accepted analytical artifact handoff schema_version is invalid")
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("accepted analytical artifact handoff artifacts are invalid")
        if not _is_sha256(value.get("handoff_hash")):
            raise ValueError("accepted analytical artifact handoff hash is invalid")
        unsigned = {
            "schema_version": value["schema_version"],
            "artifacts": artifacts,
        }
        if _sha256_bytes(_json_bytes(unsigned)) != value["handoff_hash"]:
            raise ValueError("accepted analytical artifact handoff hash does not match content")
        normalized: list[dict[str, Any]] = []
        seen_refs: set[str] = set()
        seen_artifact_ids: set[str] = set()
        for descriptor in artifacts:
            if not isinstance(descriptor, Mapping) or set(descriptor) != _ACCEPTED_ANALYTICAL_DESCRIPTOR_FIELDS:
                raise ValueError("accepted analytical artifact descriptor fields are invalid")
            ref = descriptor.get("ref")
            if not isinstance(ref, str) or not ref or ref != ref.strip():
                raise ValueError("accepted analytical artifact descriptor ref is invalid")
            path = PurePath(ref)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in ref
                or "\x00" in ref
                or not ref.startswith("work/")
                or not ref.endswith(".json")
                or path.as_posix() != ref
                or ref in seen_refs
            ):
                raise ValueError("accepted analytical artifact descriptor ref is invalid")
            seen_refs.add(ref)
            for field in (
                "hash",
                "content_hash",
                "envelope_hash",
                "canonical_bytes_sha256",
            ):
                if not _is_sha256(descriptor.get(field)):
                    raise ValueError(f"accepted analytical artifact descriptor {field} is invalid")
            for field in ("artifact_id", "artifact_type", "schema_version", "requirement_id"):
                if not isinstance(descriptor.get(field), str) or not descriptor[field].strip():
                    raise ValueError(f"accepted analytical artifact descriptor {field} is invalid")
            artifact_id = str(descriptor["artifact_id"])
            if artifact_id in seen_artifact_ids:
                # Artifact identity is independent of bytes/ref.  Reusing an
                # ID for two accepted descriptors is therefore a contract
                # defect even when the underlying files happen to be byte
                # identical; reject it before terminal intent publication.
                raise ValueError(f"accepted analytical artifact_id values must be unique: {artifact_id}")
            seen_artifact_ids.add(artifact_id)
            if descriptor.get("requirement_id") != item_id:
                raise ValueError("accepted analytical artifact descriptor requirement_id is invalid")
            normalized.append(copy.deepcopy(dict(descriptor)))
        if normalized != sorted(normalized, key=lambda item: item["ref"]):
            raise ValueError("accepted analytical artifact descriptors are not canonical")
        return tuple(normalized)

    def _validate_accepted_analytical_handoff(
        self,
        envelope: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        """Bind accepted typed artifacts to exact reviewed work-file bytes."""

        if _ACCEPTED_ANALYTICAL_HANDOFF_FIELD not in envelope:
            return ()
        descriptors = self._validate_analytical_handoff_shape(
            envelope.get(_ACCEPTED_ANALYTICAL_HANDOFF_FIELD),
            item_id=self.item_id,
        )
        hashes = manifest.get("hashes")
        if not isinstance(hashes, Mapping):
            raise ValueError("accepted analytical artifact handoff manifest is invalid")
        from .analytical_artifacts import (
            ANALYTICAL_ARTIFACT_TYPES,
            AnalyticalArtifact,
            AnalyticalArtifactValidationError,
        )
        for descriptor in descriptors:
            ref = descriptor["ref"]
            expected_hash = hashes.get(ref)
            if expected_hash != descriptor["hash"]:
                raise ValueError("accepted analytical artifact descriptor hash is not manifest-bound")
            path = self.item_root / Path(ref)
            lexical = self.item_root
            for component in PurePath(ref).parts:
                lexical = lexical / component
                if lexical.is_symlink():
                    raise AllowedRootError(f"accepted analytical artifact cannot use symlink components: {lexical}")
            _assert_regular_no_symlink(path, label="accepted analytical artifact")
            if not path.is_file():
                raise ValueError("accepted analytical artifact reference is missing")
            raw_bytes = path.read_bytes()
            if _sha256_bytes(raw_bytes) != expected_hash:
                raise ValueError("accepted analytical artifact bytes do not match manifest")
            try:
                artifact = AnalyticalArtifact.from_json(raw_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, AnalyticalArtifactValidationError, TypeError, ValueError) as exc:
                raise ValueError("accepted analytical artifact JSON is invalid") from exc
            canonical_bytes = artifact.to_json().encode("utf-8")
            if raw_bytes != canonical_bytes:
                raise ValueError("accepted analytical artifact bytes are not canonical")
            if artifact.artifact_type not in ANALYTICAL_ARTIFACT_TYPES:
                raise ValueError("accepted analytical artifact type is unsupported")
            if artifact.requirement_id != self.item_id:
                raise ValueError("accepted analytical artifact requirement_id does not match item")
            if descriptor["artifact_id"] != artifact.artifact_id or descriptor["artifact_type"] != artifact.artifact_type:
                raise ValueError("accepted analytical artifact descriptor identity is stale")
            if descriptor["schema_version"] != artifact.schema_version:
                raise ValueError("accepted analytical artifact descriptor schema_version is stale")
            if descriptor["content_hash"] != artifact.content_hash or descriptor["envelope_hash"] != artifact.envelope_hash:
                raise ValueError("accepted analytical artifact descriptor hashes are stale")
            if descriptor["canonical_bytes_sha256"] != _sha256_bytes(canonical_bytes):
                raise ValueError("accepted analytical artifact descriptor canonical hash is stale")
        return descriptors

    @staticmethod
    def _validate_acceptance_envelope(
        envelope: Mapping[str, Any],
        *,
        item_id: str,
        content_hash: str,
        outcome: str,
    ) -> None:
        expected = {
            "item_id",
            "outcome",
            "review_status",
            "review_strength",
            "review_verdict",
            "reviewer_ref",
            "content_hash",
            "draft_hash",
            "accepted_refs",
            "knowledge_delta",
            "accepted_at",
        }
        if not isinstance(envelope, Mapping) or set(envelope) not in (expected, expected | {_ACCEPTED_ANALYTICAL_HANDOFF_FIELD}):
            raise ValueError("acceptance envelope fields are invalid")
        if envelope.get("item_id") != item_id or envelope.get("outcome") != outcome:
            raise ValueError("acceptance envelope outcome is invalid")
        if envelope.get("content_hash") != content_hash or envelope.get("draft_hash") != content_hash:
            raise ValueError("acceptance envelope content hash is invalid")
        if envelope.get("review_status") not in {"reviewed", "unavailable"}:
            raise ValueError("acceptance envelope review_status is invalid")
        if envelope.get("review_verdict") not in {"accept", "accept_with_limits", "not_reviewed"}:
            raise ValueError("acceptance envelope review_verdict is invalid")
        if envelope.get("review_status") == "unavailable":
            if envelope.get("review_verdict") != "not_reviewed" or envelope.get("review_strength") != "none":
                raise ValueError("unavailable acceptance envelope is not explicitly limited")
            if envelope.get("reviewer_ref") is not None:
                raise ValueError("unavailable acceptance envelope cannot have reviewer_ref")
        if not isinstance(envelope.get("accepted_refs"), list) or any(
            not isinstance(ref, str) or not ref for ref in envelope["accepted_refs"]
        ):
            raise ValueError("acceptance envelope accepted_refs are invalid")
        if _ACCEPTED_ANALYTICAL_HANDOFF_FIELD in envelope:
            ItemWorkspace._validate_analytical_handoff_shape(
                envelope.get(_ACCEPTED_ANALYTICAL_HANDOFF_FIELD),
                item_id=item_id,
            )
        if envelope.get("knowledge_delta") not in _KNOWLEDGE_DELTAS:
            raise ValueError("acceptance envelope knowledge_delta is invalid")
        if not isinstance(envelope.get("accepted_at"), str) or not envelope.get("accepted_at"):
            raise ValueError("acceptance envelope accepted_at is invalid")

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
        if outcome in {"accepted", "accepted_with_limits"}:
            # ``accepted`` is the canonical program lifecycle state for both a
            # clean acceptance and an acceptance carrying explicit limits.
            # The business outcome remains distinct in the immutable snapshot
            # and terminal envelope.
            if lifecycle not in {"review", "accepted"}:
                raise ValueError("accepted snapshot requires a review preterminal state")
            review = self._state.get("review", {})
            accepted_review = (
                review.get("status") == "reviewed" and review.get("verdict") in {"accept", "accept_with_limits"}
            ) or (review.get("status") == "unavailable" and review.get("verdict") == "not_reviewed")
            if not accepted_review:
                raise ValueError("accepted snapshot requires a valid review")
            if review.get("draft_hash") != manifest.get("content_hash"):
                raise ValueError("accepted snapshot review hash does not match content")
            return
        if outcome == _BLOCKED_REVIEW_OUTCOME:
            if lifecycle not in {"review", _BLOCKED_REVIEW_OUTCOME}:
                raise ValueError("blocked snapshot requires a reviewed preterminal state")
            review = self._state.get("review", {})
            if review.get("status") != "reviewed" or review.get("verdict") != _CONFIRM_DATA_VERDICT:
                raise ValueError("blocked snapshot requires reviewer confirmation of data insufficiency")
            if review.get("draft_hash") != manifest.get("source_draft_hash"):
                raise ValueError("blocked snapshot review hash does not match source draft")
            conclusion = self._read_data_insufficiency_conclusion()
            if conclusion is None:
                raise ValueError("blocked snapshot requires owner data insufficiency conclusion")
            self._validate_data_insufficiency_binding(conclusion, require_current=True)
            return
        if lifecycle not in {"work", "review", "technical_failure"}:
            raise ValueError("technical failure snapshot requires a valid preterminal state")

    def _reconcile_terminal_snapshot(self) -> None:
        """Reconcile directory publication and state persistence after a crash."""

        accepted = self.accepted_root
        if not accepted.exists() and not accepted.is_symlink():
            if self._state.get("lifecycle_state") in {"accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
                raise ValueError("terminal state has no accepted snapshot")
            if self._state.get("terminal_intent") is not None:
                state = copy.deepcopy(self._state)
                state["terminal_intent"] = None
                self._persist_state(state)
            return
        snapshot, manifest = self._read_valid_terminal_snapshot()
        self._validate_preterminal_binding(snapshot.outcome, manifest)
        lifecycle = self._state["lifecycle_state"]
        if lifecycle in {"accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
            terminal = self._state.get("terminal_outcome")
            # Compare the terminal payload's business outcome independently
            # from the canonical lifecycle state.  In particular, limited
            # acceptance persists lifecycle_state=accepted while terminal
            # outcome/status remains accepted_with_limits.
            expected = {"status": snapshot.outcome, **snapshot.to_dict()}
            if terminal != expected:
                raise ValueError("terminal state does not match accepted snapshot")
            return
        state = copy.deepcopy(self._state)
        state["lifecycle_state"] = "accepted" if snapshot.outcome in {"accepted", "accepted_with_limits"} else snapshot.outcome
        state["terminal_outcome"] = {"status": snapshot.outcome, **snapshot.to_dict()}
        self._persist_state(state)

    def _reconcile_review_draft(self) -> None:
        """Invalidate a review that no longer covers the current draft bytes."""

        review = self._state.get("review")
        if not isinstance(review, Mapping) or review.get("status") == "pending":
            return
        if self._state.get("lifecycle_state") in {"accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
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

    @contextmanager
    def _state_transition_lock(self):
        """Serialize every durable ``item_state.json`` transition.

        The lock lives in a run-local namespace outside the movable item
        tree, so refresh archive/reopen operations retain one lock identity.
        """

        with _item_state_transition_lock(_state_transition_lock_path(self.context, self.item_id, self.mode)):
            yield

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
        auth_fields = (
            record["recovery_receipt_ref"],
            record["recovery_invocation_id"],
            record["recovery_receipt_hash"],
        )
        if record["route"] == "recovery":
            if record["prior_attempt_id"] is None:
                raise ValueError("recovery attempt must identify prior_attempt_id")
            if not _is_stable_receipt_ref(record["recovery_receipt_ref"]):
                raise ValueError("recovery attempt receipt_ref is invalid")
            if not isinstance(record["recovery_invocation_id"], str) or not record["recovery_invocation_id"].strip():
                raise ValueError("recovery attempt invocation_id is invalid")
            if not _is_sha256(record["recovery_receipt_hash"]):
                raise ValueError("recovery attempt receipt_hash is invalid")
        elif any(value is not None for value in auth_fields):
            raise ValueError("non-recovery attempt cannot contain recovery authorization")
        return record["status"] == "active"

    @staticmethod
    def _validate_attempt_collection(attempts: Any) -> tuple[set[str], int, set[str]]:
        if not isinstance(attempts, list):
            raise ValueError("item_state.json attempts must be a list")
        attempt_ids: set[str] = set()
        active_ids: set[str] = set()
        active_count = 0
        recovery_refs: set[str] = set()
        for record in attempts:
            active_count += int(ItemWorkspace._validate_attempt_record(record))
            attempt_id = record["attempt_id"]
            if attempt_id in attempt_ids:
                raise ValueError("item_state.json attempt IDs must be unique")
            attempt_ids.add(attempt_id)
            if record["status"] == "active":
                active_ids.add(attempt_id)
            if record["route"] == "recovery":
                receipt_ref = record["recovery_receipt_ref"]
                if receipt_ref in recovery_refs:
                    raise ValueError("item_state.json recovery receipt references must be unique")
                recovery_refs.add(receipt_ref)
        for record in attempts:
            if record["route"] != "recovery":
                if record["prior_attempt_id"] is not None:
                    raise ValueError("non-recovery attempt cannot identify prior_attempt_id")
                continue
            prior_id = record["prior_attempt_id"]
            if prior_id not in attempt_ids or prior_id == record["attempt_id"]:
                raise ValueError("recovery attempt prior_attempt_id is invalid")
            prior = next(item for item in attempts if item["attempt_id"] == prior_id)
            if prior["status"] != "recovered":
                raise ValueError("recovery attempt prior attempt must be recovered")
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
    def _validate_recovery_count(state: Mapping[str, Any]) -> None:
        expected = sum(1 for record in state["attempts"] if record.get("route") == "recovery")
        if state["execution_recovery_count"] != expected:
            raise ValueError("item_state.json execution_recovery_count does not match recovery attempts")

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
            if terminal["outcome"] not in {"accepted", "accepted_with_limits", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
                raise ValueError("terminal_outcome outcome is invalid")
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
        if intent["outcome"] not in {"accepted", "accepted_with_limits", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
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
        if lifecycle in {"accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
            accepted_match = lifecycle == "accepted" and terminal is not None and terminal["outcome"] in {
                "accepted",
                "accepted_with_limits",
            }
            if terminal is None or (not accepted_match and terminal["outcome"] != lifecycle):
                raise ValueError("terminal lifecycle requires matching terminal_outcome")
            intent_match = lifecycle == "accepted" and intent is not None and intent["outcome"] in {
                "accepted",
                "accepted_with_limits",
            }
            if intent is None or (not intent_match and intent["outcome"] != lifecycle):
                raise ValueError("terminal lifecycle requires matching terminal_intent")
            if active is not None:
                raise ValueError("terminal lifecycle cannot have an active attempt")
        elif terminal is not None:
            raise ValueError("non-terminal lifecycle cannot have terminal_outcome")
        if intent is not None and active is not None:
            raise ValueError("terminal intent cannot have an active attempt")
        if intent is not None and lifecycle not in {"accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
            allowed_preterminal = {"review"} if intent["outcome"] in {"accepted", "accepted_with_limits", _BLOCKED_REVIEW_OUTCOME} else {"work", "review"}
            if lifecycle not in allowed_preterminal:
                raise ValueError("terminal intent lifecycle is invalid")
        if lifecycle == "review" and (active is not None or review_status not in {"reviewed", "unavailable"}):
            raise ValueError("review lifecycle requires an inactive reviewed state")
        if lifecycle in {"work", "recovering", "recovery_ready"} and review_status != "pending":
            raise ValueError("work lifecycle requires pending review")
        if lifecycle in {"recovering", "recovery_ready"} and active is None:
            raise ValueError("recovery lifecycle requires an active attempt")

    @staticmethod
    def _validate_integration_state(state: Mapping[str, Any]) -> None:
        integration_state = state.get("integration_state")
        if integration_state not in {"pending", "integrated", "technical_failure"}:
            raise ValueError("item_state.json integration_state is invalid")
        manifest_hash = state.get("integration_manifest_hash")
        if manifest_hash is not None and not _is_sha256(manifest_hash):
            raise ValueError("item_state.json integration_manifest_hash is invalid")
        manifest_ref = state.get("integration_manifest_ref")
        if manifest_ref is not None and (not isinstance(manifest_ref, str) or not manifest_ref.strip()):
            raise ValueError("item_state.json integration_manifest_ref is invalid")
        if integration_state == "pending" and (manifest_hash is not None or manifest_ref is not None):
            raise ValueError("pending integration cannot have a manifest reference")
        if integration_state in {"integrated", "technical_failure"} and manifest_hash is None:
            raise ValueError("terminal integration requires a manifest hash")

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
        ItemWorkspace._validate_recovery_count(state)
        review_status = ItemWorkspace._validate_review_state(state["review"])
        ItemWorkspace._validate_terminal_state(state, active, review_status)
        ItemWorkspace._validate_integration_state(state)

    @staticmethod
    def _execution_defaults() -> dict[str, Any]:
        return {
            "attempts": [],
            "active_attempt_id": None,
            "consecutive_no_progress": 0,
            "review": ItemWorkspace._pending_review(),
            "terminal_outcome": None,
            "terminal_intent": None,
            "integration_state": "pending",
            "integration_manifest_hash": None,
            "integration_manifest_ref": None,
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

    def _persist_state(
        self,
        state: Mapping[str, Any],
        *,
        touch: bool = True,
        _lock_held: bool = False,
    ) -> None:
        if not _lock_held:
            with self._state_transition_lock():
                self._persist_state_unlocked(state, touch=touch)
            return
        self._persist_state_unlocked(state, touch=touch)

    def _persist_state_unlocked(self, state: Mapping[str, Any], *, touch: bool = True) -> None:
        candidate = copy.deepcopy(dict(state))
        if touch:
            candidate["updated_at"] = _now()
        # A terminal directory is the durable publication authority.  A
        # stale workspace that began a write before terminalization must not
        # acquire the lock afterward and erase the terminal state with a
        # non-terminal snapshot.
        if (self.accepted_root.exists() or self.accepted_root.is_symlink()) and candidate.get("lifecycle_state") not in {
            "accepted",
            "technical_failure",
            _BLOCKED_REVIEW_OUTCOME,
        }:
            raise ValueError("cannot persist non-terminal state after terminal snapshot publication")
        self._validate_state_shape(candidate)
        self._validate_recovery_authorizations(candidate)
        _atomic_write_json(self._resolve_item_subpath(_STATE_FILENAME), candidate)
        self._state = candidate

    def _validate_recovery_authorizations(self, state: Mapping[str, Any] | None = None) -> None:
        """Verify every persisted recovery record against the append-only ledger.

        Item state carries only a stable reference, invocation identity, and
        record hash.  The ledger remains the authority for all receipt facts;
        a changed reference/hash or a deleted/tampered ledger line therefore
        makes reload and every subsequent state mutation fail closed.
        """

        candidate = self._state if state is None else state
        if set(candidate) != _EXECUTION_STATE_FIELDS:
            return
        records = candidate.get("attempts", ())
        recovery_records = [record for record in records if record.get("route") == "recovery"]
        if not recovery_records:
            return
        from .lifecycle import InvocationReceiptLedger, classify_terminal_reason

        ledger = InvocationReceiptLedger(context=self.context)
        by_attempt = {record.get("attempt_id"): record for record in records}
        for record in recovery_records:
            receipt_ref = record["recovery_receipt_ref"]
            receipt, record_hash = ledger.resolve(receipt_ref)
            prior = by_attempt.get(record.get("prior_attempt_id"))
            if prior is None:
                raise ValueError("recovery authorization prior attempt is missing")
            if record["recovery_receipt_hash"] != record_hash:
                raise ValueError("recovery authorization receipt hash does not match ledger")
            if record["recovery_invocation_id"] != receipt.invocation_id:
                raise ValueError("recovery authorization invocation_id does not match ledger")
            if receipt.item_id != candidate["item_id"]:
                raise ValueError("recovery authorization item_id does not match workspace")
            if receipt.attempt_id != prior["attempt_id"]:
                raise ValueError("recovery authorization attempt_id does not match prior attempt")
            if receipt.lane_id != prior["lane_id"]:
                raise ValueError("recovery authorization lane_id does not match prior attempt")
            if receipt.role != prior["role"]:
                raise ValueError("recovery authorization role does not match prior attempt")
            if receipt.finish is None or classify_terminal_reason(receipt.terminal_reason) != "execution_recovery":
                raise ValueError("recovery authorization receipt does not prove execution loss")

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

    @property
    def analysis_owner_path(self) -> Path:
        """Program-owned binding for the Analytical Owner of this item."""

        return self._resolve_item_subpath(Path("work") / _ANALYSIS_OWNER_FILENAME)

    @staticmethod
    def _validate_analysis_owner_payload(value: Any, *, item_id: str | None = None) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _ANALYSIS_OWNER_FIELDS:
            raise ValueError("analysis owner binding fields are invalid")
        if value.get("record_kind") != "analysis_owner_binding":
            raise ValueError("analysis owner binding kind is invalid")
        if item_id is not None and value.get("item_id") != item_id:
            raise ValueError("analysis owner binding item_id is invalid")
        if value.get("mode") not in _VALID_MODES:
            raise ValueError("analysis owner binding mode is invalid")
        owner_ref = value.get("owner_ref")
        if not isinstance(owner_ref, str) or not owner_ref.strip() or "\x00" in owner_ref:
            raise ValueError("analysis owner binding owner_ref is invalid")
        if not _is_sha256(value.get("original_text_hash")) or not _is_sha256(value.get("owner_hash")):
            raise ValueError("analysis owner binding hash is invalid")
        unsigned = {key: value[key] for key in _ANALYSIS_OWNER_FIELDS if key != "owner_hash"}
        if _sha256_bytes(_json_bytes(unsigned)) != value["owner_hash"]:
            raise ValueError("analysis owner binding hash is inconsistent")
        return copy.deepcopy(dict(value))

    def _read_analysis_owner(self) -> dict[str, Any] | None:
        path = self.analysis_owner_path
        if not path.exists() and not path.is_symlink():
            return None
        _assert_regular_no_symlink(path, label="analysis owner binding")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("analysis owner binding is invalid") from exc
        normalized = self._validate_analysis_owner_payload(value, item_id=self.item_id)
        if normalized["mode"] != self.mode:
            raise ValueError("analysis owner binding mode does not match workspace")
        expected_original = _sha256_bytes(self.original_text.encode("utf-8"))
        if normalized["original_text_hash"] != expected_original:
            raise ValueError("analysis owner binding original text is stale")
        return normalized

    def _verify_analysis_owner_locked(self, owner_ref: Any, *, bind_if_missing: bool = False) -> str:
        """Verify one stable logical owner, adopting it during facade binding."""

        normalized_owner = _owner_ref_value(owner_ref)
        existing = self._read_analysis_owner()
        if existing is not None:
            if existing["owner_ref"] == normalized_owner:
                return normalized_owner
            if bind_if_missing:
                # A replacement process continues as the already-bound
                # logical owner.  Rotating this durable identity would make
                # append-only owner-authored evidence internally inconsistent.
                return str(existing["owner_ref"])
            raise ValueError("Analytical Owner does not match the bound item owner")
        if not bind_if_missing:
            raise ValueError("item has no bound Analytical Owner")
        self._ensure_not_terminal()
        unsigned = {
            "record_kind": "analysis_owner_binding",
            "item_id": self.item_id,
            "mode": self.mode,
            "original_text_hash": _sha256_bytes(self.original_text.encode("utf-8")),
            "owner_ref": normalized_owner,
        }
        payload = {**unsigned, "owner_hash": _sha256_bytes(_json_bytes(unsigned))}
        _atomic_write_json(self.analysis_owner_path, payload)
        return normalized_owner

    def bind_analysis_owner(self, owner_ref: Any) -> str:
        """Bind or reload the one program-owned Analytical Owner identity."""

        with self._state_transition_lock():
            self._reload_authoritative_state_locked()
            bound = self._verify_analysis_owner_locked(owner_ref, bind_if_missing=True)
            self._ensure_execution_state()
            self._reconcile_business_review_discard()
            return bound

    def analysis_owner_ref(self) -> str:
        """Return the current program-owned Analytical Owner identity."""

        with self._state_transition_lock():
            self._reload_authoritative_state_locked()
            existing = self._read_analysis_owner()
            if existing is None:
                raise ValueError("item has no bound Analytical Owner")
            return str(existing["owner_ref"])

    @property
    def data_insufficiency_path(self) -> Path:
        """Path for the owner-authored data-insufficiency conclusion."""

        return self._resolve_item_subpath(Path("work") / _DATA_INSUFFICIENCY_FILENAME)

    @staticmethod
    def _validate_data_insufficiency_payload(value: Any, *, item_id: str | None = None) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _DATA_INSUFFICIENCY_FIELDS:
            raise ValueError("data insufficiency conclusion fields are invalid")
        if value.get("record_kind") != "data_insufficiency_conclusion":
            raise ValueError("data insufficiency conclusion kind is invalid")
        if item_id is not None and value.get("item_id") != item_id:
            raise ValueError("data insufficiency conclusion item_id is invalid")
        if value.get("mode") not in _VALID_MODES:
            raise ValueError("data insufficiency conclusion mode is invalid")
        for field_name in ("original_text_hash", "draft_hash", "artifact_progress_hash"):
            if not _is_sha256(value.get(field_name)):
                raise ValueError(f"data insufficiency conclusion {field_name} is invalid")
        for field_name in ("reason", "unanswerable_component"):
            if not isinstance(value.get(field_name), str) or not value[field_name].strip():
                raise ValueError(f"data insufficiency conclusion {field_name} must be non-empty")
        for field_name in ("missing_information", "searches_performed", "evidence_refs"):
            values = value.get(field_name)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(entry, str) or not entry.strip() for entry in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"data insufficiency conclusion {field_name} is invalid")
        supported = value.get("supported_components")
        if (
            not isinstance(supported, list)
            or any(not isinstance(entry, str) or not entry.strip() for entry in supported)
            or len(supported) != len(set(supported))
        ):
            raise ValueError("data insufficiency conclusion supported_components is invalid")
        return copy.deepcopy(dict(value))

    def _read_data_insufficiency_conclusion(self) -> dict[str, Any] | None:
        path = self.data_insufficiency_path
        if not path.exists() and not path.is_symlink():
            return None
        _assert_regular_no_symlink(path, label="data insufficiency conclusion")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("data insufficiency conclusion is invalid") from exc
        return self._validate_data_insufficiency_payload(value, item_id=self.item_id)

    def _validate_data_insufficiency_binding(
        self,
        value: Mapping[str, Any],
        *,
        require_current: bool = True,
    ) -> dict[str, Any]:
        normalized = self._validate_data_insufficiency_payload(value, item_id=self.item_id)
        if normalized["mode"] != self.mode:
            raise ValueError("data insufficiency conclusion mode does not match workspace")
        expected_original = _sha256_bytes(self.original_text.encode("utf-8"))
        if normalized["original_text_hash"] != expected_original:
            raise ValueError("data insufficiency conclusion original text is stale")
        if require_current:
            draft_hash = self._draft_hash()
            if normalized["draft_hash"] != draft_hash:
                raise ValueError("data insufficiency conclusion draft is stale")
            progress_hash = _sha256_bytes(_json_bytes(self.artifact_progress().to_dict()))
            if normalized["artifact_progress_hash"] != progress_hash:
                raise ValueError("data insufficiency conclusion artifact progress is stale")
        return normalized

    def record_data_insufficiency_conclusion(
        self,
        value: Mapping[str, Any],
        *,
        owner_ref: Any,
    ) -> dict[str, Any]:
        """Persist the analytical owner's explicit material data insufficiency.

        The caller supplies semantic fields only.  Item identity, mode, draft
        and artifact bindings are program-owned and are added while holding the
        shared transition lock.  A second identical conclusion is idempotent;
        a different conclusion is rejected without mutation.
        """

        if not isinstance(value, Mapping):
            raise TypeError("data insufficiency conclusion must be a mapping")
        semantic_fields = {
            "reason",
            "unanswerable_component",
            "missing_information",
            "searches_performed",
            "evidence_refs",
            "supported_components",
        }
        if set(value) != semantic_fields:
            raise ValueError("data insufficiency conclusion semantic fields are invalid")
        with self._state_transition_lock():
            self._reload_authoritative_state_locked()
            self._verify_analysis_owner_locked(owner_ref, bind_if_missing=True)
            self._ensure_execution_state()
            self._reconcile_business_review_discard()
            self._ensure_not_terminal()
            self._require_no_active_attempt()
            draft_hash = self._draft_hash()
            progress = self.artifact_progress()
            payload = {
                "record_kind": "data_insufficiency_conclusion",
                "item_id": self.item_id,
                "mode": self.mode,
                "original_text_hash": _sha256_bytes(self.original_text.encode("utf-8")),
                "draft_hash": draft_hash,
                "artifact_progress_hash": _sha256_bytes(_json_bytes(progress.to_dict())),
                **copy.deepcopy(dict(value)),
            }
            self._validate_data_insufficiency_payload(payload, item_id=self.item_id)
            existing = self._read_data_insufficiency_conclusion()
            if existing is not None:
                if existing != payload:
                    raise ValueError("data insufficiency conclusion is immutable")
                return copy.deepcopy(existing)
            destination = self.data_insufficiency_path
            _assert_regular_no_symlink(destination, label="data insufficiency conclusion")
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_state = copy.deepcopy(self._state)
            try:
                _atomic_write_json(destination, payload)
                state = copy.deepcopy(self._state)
                state["updated_at"] = _now()
                self._persist_state_unlocked(state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    destination.unlink(missing_ok=True)
                    _fsync_directory(destination.parent)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("data insufficiency conclusion rollback failed") from exc
                raise
        self._emit("item_data_insufficiency_concluded", artifact=f"work/{_DATA_INSUFFICIENCY_FILENAME}")
        return copy.deepcopy(payload)

    @property
    def business_review_path(self) -> Path:
        """Path for the item-local structured business-review packet."""

        return self._resolve_item_subpath(Path("work") / _BUSINESS_REVIEW_FILENAME)

    @property
    def business_review_discard_audit_path(self) -> Path:
        """Append-only audit path for discarded reviewer-scope packets."""

        return self._resolve_item_subpath(Path("work") / _BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME)

    @property
    def business_review_discard_state_path(self) -> Path:
        """Durable anchor and in-flight intent for reviewer-scope discard."""

        return self._resolve_item_subpath(Path("work") / _BUSINESS_REVIEW_DISCARD_STATE_FILENAME)

    def _normalize_discard_incident(self, value: IncidentRecord | Mapping[str, Any]) -> IncidentRecord:
        if isinstance(value, IncidentRecord):
            incident = value
        elif isinstance(value, Mapping):
            raw = dict(value)
            required = {"incident_id", "category", "disposition", "admissible", "item_id", "scope", "source"}
            missing = sorted(required - set(raw))
            if missing:
                raise ValueError(f"discard incident is missing fields: {missing}")
            unknown = sorted(set(raw) - (required | {"facts"}))
            if unknown:
                raise ValueError(f"discard incident has unknown fields: {unknown}")
            if str(raw["category"]).strip() != "reviewer_scope":
                raise ValueError("discard incident category must be reviewer_scope")
            if not isinstance(raw["source"], str) or not raw["source"].strip():
                raise ValueError("discard incident source must be a non-empty string")
            scope = raw["scope"]
            if isinstance(scope, str) or not isinstance(scope, (list, tuple)):
                raise TypeError("discard incident scope must be a sequence of strings")
            incident = IncidentRecord(
                incident_id=raw["incident_id"],
                category=raw["category"],
                disposition=raw["disposition"],
                admissible=raw["admissible"],
                item_id=raw["item_id"],
                scope=tuple(scope),
                source=raw.get("source"),
                facts=raw.get("facts", {}),
            )
        else:
            raise TypeError("discard incident must be an IncidentRecord or mapping")
        if incident.category != "reviewer_scope":
            raise ValueError("discard incident category must be reviewer_scope")
        if incident.admissible:
            raise ValueError("discard incident must be inadmissible")
        if incident.item_id != self.item_id:
            raise ValueError("discard incident item_id does not match workspace")
        if not incident.incident_id or not incident.disposition:
            raise ValueError("discard incident requires non-empty incident_id and disposition")
        if not isinstance(incident.source, str) or not incident.source.strip():
            raise ValueError("discard incident source must be a non-empty string")
        if not incident.scope or len(incident.scope) != len(set(incident.scope)):
            raise ValueError("discard incident requires a non-empty unique scope")
        return incident

    def _validate_business_review_discard_record(
        self,
        raw: Any,
        *,
        line_number: int,
        previous_hash: str | None,
        seen_ids: set[str],
    ) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) != _BUSINESS_REVIEW_DISCARD_AUDIT_FIELDS:
            raise ValueError(f"business review discard audit line {line_number} fields are invalid")
        if raw["record_kind"] != "business_review_discard":
            raise ValueError(f"business review discard audit line {line_number} kind is invalid")
        if raw["audit_path"] != f"work/{_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME}":
            raise ValueError(f"business review discard audit line {line_number} path is invalid")
        if raw["item_id"] != self.item_id:
            raise ValueError(f"business review discard audit line {line_number} item_id is invalid")
        incident = self._normalize_discard_incident(raw["incident"])
        if raw["audit_id"] != incident.incident_id or raw["audit_id"] in seen_ids:
            raise ValueError("business review discard audit incident_id is duplicate or inconsistent")
        for name in ("discarded_packet_hash", "draft_hash", "audit_hash"):
            if not _is_sha256(raw[name]):
                raise ValueError(f"business review discard audit {name} is invalid")
        if raw["previous_audit_hash"] != previous_hash:
            raise ValueError("business review discard audit hash chain is invalid")
        unsigned = {key: raw[key] for key in _BUSINESS_REVIEW_DISCARD_AUDIT_FIELDS if key != "audit_hash"}
        if _sha256_bytes(_json_bytes(unsigned)) != raw["audit_hash"]:
            raise ValueError("business review discard audit hash is invalid")
        return copy.deepcopy(raw)

    def _read_business_review_discard_audit(self) -> list[dict[str, Any]]:
        path = self.business_review_discard_audit_path
        if not path.exists() and not path.is_symlink():
            return []
        _assert_regular_no_symlink(path, label="business review discard audit")
        if not path.is_file():
            raise ValueError("business review discard audit is not a file")
        if path.stat().st_size == 0:
            raise ValueError("business review discard audit is empty")
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_hash: str | None = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("business review discard audit is unreadable") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise ValueError(f"business review discard audit has a blank line at {line_number}")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"business review discard audit line {line_number} is invalid") from exc
            record = self._validate_business_review_discard_record(
                raw,
                line_number=line_number,
                previous_hash=previous_hash,
                seen_ids=seen_ids,
            )
            records.append(record)
            seen_ids.add(record["audit_id"])
            previous_hash = record["audit_hash"]
        return records

    def _validate_business_review_discard_intent(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _BUSINESS_REVIEW_DISCARD_INTENT_FIELDS:
            raise ValueError("business review discard intent fields are invalid")
        if not isinstance(value["operation_id"], str) or not value["operation_id"]:
            raise ValueError("business review discard intent operation_id is invalid")
        if value["target"] != "discard_business_review":
            raise ValueError("business review discard intent target is invalid")
        if value["packet_path"] != f"work/{_BUSINESS_REVIEW_FILENAME}" or value["audit_path"] != f"work/{_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME}":
            raise ValueError("business review discard intent paths are invalid")
        incident = self._normalize_discard_incident(value["incident"])
        if incident.incident_id != value["operation_id"]:
            raise ValueError("business review discard intent incident identity is invalid")
        for name in ("discarded_packet_hash", "draft_hash", "before_state_hash", "after_state_hash", "expected_audit_head"):
            if not _is_sha256(value[name]):
                raise ValueError(f"business review discard intent {name} is invalid")
        for name in ("prior_audit_head",):
            if value[name] is not None and not _is_sha256(value[name]):
                raise ValueError(f"business review discard intent {name} is invalid")
        for name in ("prior_audit_count", "expected_audit_count"):
            count = value[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"business review discard intent {name} is invalid")
        if value["expected_audit_count"] != value["prior_audit_count"] + 1:
            raise ValueError("business review discard intent audit counts are invalid")
        expected_audit = self._validate_business_review_discard_record(
            value["expected_audit"],
            line_number=0,
            previous_hash=value["prior_audit_head"],
            seen_ids=set(),
        )
        if (
            expected_audit["audit_id"] != incident.incident_id
            or expected_audit["audit_hash"] != value["expected_audit_head"]
            or expected_audit["incident"] != incident.to_dict()
            or expected_audit["discarded_packet_hash"] != value["discarded_packet_hash"]
            or expected_audit["draft_hash"] != value["draft_hash"]
        ):
            raise ValueError("business review discard intent expected audit is invalid")
        after_state = value["after_state"]
        if not isinstance(after_state, Mapping):
            raise ValueError("business review discard intent after_state is invalid")
        self._validate_state_shape(after_state)
        if _sha256_bytes(_json_bytes(after_state)) != value["after_state_hash"]:
            raise ValueError("business review discard intent after_state hash is invalid")
        if value["phase"] not in {"intent", "audit_appended", "packet_removed", "state_persisted"}:
            raise ValueError("business review discard intent phase is invalid")
        unsigned = {key: value[key] for key in _BUSINESS_REVIEW_DISCARD_INTENT_FIELDS if key != "intent_hash"}
        if not _is_sha256(value["intent_hash"]) or _sha256_bytes(_json_bytes(unsigned)) != value["intent_hash"]:
            raise ValueError("business review discard intent hash is invalid")
        return copy.deepcopy(dict(value))

    def _read_business_review_discard_state(self) -> dict[str, Any] | None:
        state_path = self.business_review_discard_state_path
        audit_path = self.business_review_discard_audit_path
        if not state_path.exists() and not state_path.is_symlink():
            if audit_path.exists() or audit_path.is_symlink():
                raise ValueError("business review discard audit is missing its durable state anchor")
            return None
        _assert_regular_no_symlink(state_path, label="business review discard state")
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("business review discard state is invalid") from exc
        if not isinstance(value, Mapping) or set(value) != _BUSINESS_REVIEW_DISCARD_STATE_FIELDS:
            raise ValueError("business review discard state fields are invalid")
        if value["record_kind"] != "business_review_discard_state" or value["item_id"] != self.item_id:
            raise ValueError("business review discard state identity is invalid")
        if value["audit_path"] != f"work/{_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME}":
            raise ValueError("business review discard state audit path is invalid")
        count = value["audit_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("business review discard state audit_count is invalid")
        head = value["audit_head"]
        if (count == 0 and head is not None) or (count > 0 and not _is_sha256(head)):
            raise ValueError("business review discard state audit_head is invalid")
        if value["intent"] is not None:
            self._validate_business_review_discard_intent(value["intent"])
        unsigned = {key: value[key] for key in _BUSINESS_REVIEW_DISCARD_STATE_FIELDS if key != "state_hash"}
        if not _is_sha256(value["state_hash"]) or _sha256_bytes(_json_bytes(unsigned)) != value["state_hash"]:
            raise ValueError("business review discard state hash is invalid")
        return copy.deepcopy(dict(value))

    def _write_business_review_discard_state(
        self,
        *,
        audit_count: int,
        audit_head: str | None,
        intent: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        value = {
            "record_kind": "business_review_discard_state",
            "item_id": self.item_id,
            "audit_path": f"work/{_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME}",
            "audit_count": audit_count,
            "audit_head": audit_head,
            "intent": copy.deepcopy(dict(intent)) if intent is not None else None,
        }
        value["state_hash"] = _sha256_bytes(_json_bytes(value))
        _atomic_write_json(self.business_review_discard_state_path, value)
        return value

    @staticmethod
    def _discard_intent_with_phase(intent: Mapping[str, Any], phase: str) -> dict[str, Any]:
        updated = copy.deepcopy(dict(intent))
        updated["phase"] = phase
        unsigned = {key: updated[key] for key in _BUSINESS_REVIEW_DISCARD_INTENT_FIELDS if key != "intent_hash"}
        updated["intent_hash"] = _sha256_bytes(_json_bytes(unsigned))
        return updated

    def _reconcile_business_review_discard(self) -> None:
        anchor = self._read_business_review_discard_state()
        audit = self._read_business_review_discard_audit()
        if anchor is None:
            return
        actual_count = len(audit)
        actual_head = audit[-1]["audit_hash"] if audit else None
        intent = anchor["intent"]
        if intent is None:
            if anchor["audit_count"] != actual_count or anchor["audit_head"] != actual_head:
                raise ValueError("business review discard audit does not match its durable state anchor")
            return

        prior_count = intent["prior_audit_count"]
        prior_head = intent["prior_audit_head"]
        expected_count = intent["expected_audit_count"]
        expected_head = intent["expected_audit_head"]
        if actual_count not in {prior_count, expected_count}:
            raise ValueError("business review discard intent audit count is not recoverable")
        if actual_count == prior_count:
            if anchor["audit_count"] != prior_count or anchor["audit_head"] != prior_head:
                raise ValueError("business review discard intent anchor is inconsistent")
        else:
            if actual_head != expected_head or audit[-1] != intent["expected_audit"]:
                raise ValueError("business review discard intent audit tail is invalid")
            if anchor["audit_count"] not in {prior_count, expected_count} or anchor["audit_head"] not in {prior_head, expected_head}:
                raise ValueError("business review discard intent anchor is inconsistent")

        state_path = self._resolve_item_subpath(_STATE_FILENAME)
        state_bytes = state_path.read_bytes()
        state_hash = _sha256_bytes(state_bytes)
        before_hash = intent["before_state_hash"]
        after_hash = intent["after_state_hash"]
        packet_path = self.business_review_path
        packet_present = packet_path.exists() or packet_path.is_symlink()
        # The discard protocol never mutates the draft.  Bind it on every
        # recovery path, including after the packet unlink boundary, so an
        # external draft edit cannot be mistaken for a completed operation.
        if self._draft_hash() != intent["draft_hash"]:
            raise ValueError("business review discard intent draft is invalid")
        if packet_present:
            _assert_regular_no_symlink(packet_path, label="business review artifact")
            if not packet_path.is_file() or _sha256_file(packet_path) != intent["discarded_packet_hash"]:
                raise ValueError("business review discard intent packet is invalid")

        if state_hash == after_hash:
            if packet_present or actual_count != expected_count:
                raise ValueError("business review discard intent completed state is inconsistent")
            self._write_business_review_discard_state(
                audit_count=expected_count,
                audit_head=expected_head,
                intent=None,
            )
            return
        if state_hash != before_hash:
            raise ValueError("business review discard intent state is not recoverable")
        if actual_count == prior_count:
            if not packet_present:
                raise ValueError("business review discard intent lost its packet before audit append")
            self._write_business_review_discard_state(
                audit_count=prior_count,
                audit_head=prior_head,
                intent=None,
            )
            return
        if packet_present:
            packet_path.unlink()
            _fsync_directory(packet_path.parent)
        after_state = copy.deepcopy(intent["after_state"])
        self._persist_state(after_state, touch=False)
        self._write_business_review_discard_state(
            audit_count=expected_count,
            audit_head=expected_head,
            intent=None,
        )

    def discard_business_review(self, incident: IncidentRecord | Mapping[str, Any]) -> dict[str, Any]:
        """Discard an invalid reviewer-scope packet and reset repair authority.

        A durable intent and audit head make the operation converge after a
        process loss at any persistence boundary.  Ordinary exceptions still
        roll back every touched byte before returning control to the caller.
        """

        # Rebind validates the packet, discard audit, and item state as one
        # boundary.  Keep the entire discard transaction under the same item
        # lock so it cannot interleave with that authoritative reload.
        with self._state_transition_lock():
            return self._discard_business_review_locked(incident)

    def _discard_business_review_locked(self, incident: IncidentRecord | Mapping[str, Any]) -> dict[str, Any]:
        """Perform discard while ``_state_transition_lock`` is held."""

        self._ensure_not_terminal()
        self._require_no_active_attempt()
        if self._state.get("terminal_intent") is not None:
            raise ValueError("business review discard cannot run with terminal intent")
        self._reconcile_business_review_discard()
        packet_path = self.business_review_path
        if not packet_path.exists():
            raise ValueError("business review discard requires a structured review packet")
        self._ensure_execution_state()
        normalized_incident = self._normalize_discard_incident(incident)
        packet = self._read_business_review()
        if packet is None:
            raise ValueError("business review discard requires a structured review packet")
        _assert_regular_no_symlink(packet_path, label="business review artifact")
        packet_bytes = packet_path.read_bytes()
        prior_audit = self._read_business_review_discard_audit()
        if any(record["audit_id"] == normalized_incident.incident_id for record in prior_audit):
            raise ValueError("business review discard incident_id was already recorded")
        draft_hash = self._draft_hash()
        previous_audit_hash = prior_audit[-1]["audit_hash"] if prior_audit else None
        unsigned_audit = {
            "record_kind": "business_review_discard",
            "audit_id": normalized_incident.incident_id,
            "audit_path": f"work/{_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME}",
            "item_id": self.item_id,
            "incident": normalized_incident.to_dict(),
            "discarded_packet_hash": _sha256_bytes(packet_bytes),
            "draft_hash": draft_hash,
            "previous_audit_hash": previous_audit_hash,
        }
        audit = {
            **unsigned_audit,
            "audit_hash": _sha256_bytes(_json_bytes(unsigned_audit)),
        }
        state_path = self._resolve_item_subpath(_STATE_FILENAME)
        _assert_regular_no_symlink(state_path, label="item state")
        prior_state_bytes = state_path.read_bytes()
        prior_state = copy.deepcopy(self._state)
        before_state_hash = _sha256_bytes(prior_state_bytes)
        after_state = copy.deepcopy(self._state)
        after_state["business_repair_count"] = 0
        after_state["review"] = self._pending_review()
        after_state["lifecycle_state"] = "work"
        after_state["updated_at"] = _now()
        self._validate_state_shape(after_state)
        after_state_hash = _sha256_bytes(_json_bytes(after_state))
        intent_unsigned = {
            "operation_id": normalized_incident.incident_id,
            "target": "discard_business_review",
            "packet_path": f"work/{_BUSINESS_REVIEW_FILENAME}",
            "audit_path": f"work/{_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME}",
            "incident": normalized_incident.to_dict(),
            "discarded_packet_hash": audit["discarded_packet_hash"],
            "draft_hash": draft_hash,
            "prior_audit_count": len(prior_audit),
            "prior_audit_head": previous_audit_hash,
            "expected_audit_count": len(prior_audit) + 1,
            "expected_audit_head": audit["audit_hash"],
            "expected_audit": audit,
            "before_state_hash": before_state_hash,
            "after_state_hash": after_state_hash,
            "after_state": after_state,
            "phase": "intent",
        }
        intent = {
            **intent_unsigned,
            "intent_hash": _sha256_bytes(_json_bytes(intent_unsigned)),
        }
        audit_path = self.business_review_discard_audit_path
        audit_exists = audit_path.exists() or audit_path.is_symlink()
        audit_bytes = audit_path.read_bytes() if audit_exists else None
        discard_state_path = self.business_review_discard_state_path
        discard_state_exists = discard_state_path.exists() or discard_state_path.is_symlink()
        discard_state_bytes = discard_state_path.read_bytes() if discard_state_exists else None

        try:
            self._write_business_review_discard_state(
                audit_count=len(prior_audit),
                audit_head=previous_audit_hash,
                intent=intent,
            )
            _append_jsonl(audit_path, audit)
            intent = self._discard_intent_with_phase(intent, "audit_appended")
            self._write_business_review_discard_state(
                audit_count=len(prior_audit) + 1,
                audit_head=audit["audit_hash"],
                intent=intent,
            )
            packet_path.unlink()
            _fsync_directory(packet_path.parent)
            intent = self._discard_intent_with_phase(intent, "packet_removed")
            self._write_business_review_discard_state(
                audit_count=len(prior_audit) + 1,
                audit_head=audit["audit_hash"],
                intent=intent,
            )
            self._persist_state(after_state, touch=False)
            self._write_business_review_discard_state(
                audit_count=len(prior_audit) + 1,
                audit_head=audit["audit_hash"],
                intent=None,
            )
        except Exception as exc:
            rollback_errors: list[Exception] = []
            try:
                _atomic_write_bytes(state_path, prior_state_bytes)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            try:
                _atomic_write_bytes(packet_path, packet_bytes)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            try:
                if audit_exists and audit_bytes is not None:
                    _atomic_write_bytes(audit_path, audit_bytes)
                elif audit_path.exists() or audit_path.is_symlink():
                    audit_path.unlink()
                    _fsync_directory(audit_path.parent)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            try:
                if discard_state_exists and discard_state_bytes is not None:
                    _atomic_write_bytes(discard_state_path, discard_state_bytes)
                elif discard_state_path.exists() or discard_state_path.is_symlink():
                    discard_state_path.unlink()
                    _fsync_directory(discard_state_path.parent)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            self._state = prior_state
            if rollback_errors:
                raise RuntimeError("business review discard rollback failed") from exc
            raise

        self._emit(
            "item_business_review_discarded",
            artifact=f"work/{_BUSINESS_REVIEW_FILENAME}",
            audit_artifact=f"work/{_BUSINESS_REVIEW_DISCARD_AUDIT_FILENAME}",
            incident_id=normalized_incident.incident_id,
            packet_hash=audit["discarded_packet_hash"],
            audit_hash=audit["audit_hash"],
        )
        return copy.deepcopy(audit)

    @staticmethod
    def _canonical_draft_value(payload: bytes) -> Any:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("draft must contain valid JSON for scoped business repair") from exc
        # Drafts are JSON structures by contract.  Scalars are valid but still
        # represented by the root pointer in the mechanical diff.
        return value

    @staticmethod
    def _normalize_finding(
        value: Mapping[str, Any],
        index: int,
        *,
        allow_legacy_scope_subset: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("business review findings must be mappings")
        raw = dict(value)
        has_semantic_categories = "semantic_categories" in raw
        semantic_categories = raw.pop("semantic_categories", None)
        finding_id = raw.pop("finding_id", raw.pop("id", None))
        message = raw.pop("message", raw.pop("finding", raw.pop("reason", "")))
        pointers = raw.pop("pointers", raw.pop("json_pointers", raw.pop("pointer", raw.pop("path", ()))))
        artifact_paths = raw.pop(
            "artifact_paths",
            raw.pop("artifacts", raw.pop("artifact_path", ())),
        )
        dependent_outputs = raw.pop(
            "dependent_outputs",
            raw.pop("dependencies", raw.pop("dependent_output", ())),
        )
        if isinstance(pointers, str):
            pointers = (pointers,)
        if isinstance(artifact_paths, str):
            artifact_paths = (artifact_paths,)
        if isinstance(dependent_outputs, str):
            dependent_outputs = (dependent_outputs,)
        pointers = tuple(str(path).strip() for path in pointers or () if str(path).strip())
        artifact_paths = tuple(str(path).strip() for path in artifact_paths or () if str(path).strip())
        dependent_outputs = tuple(str(path).strip() for path in dependent_outputs or () if str(path).strip())
        if any(path == "*" for path in (*pointers, *artifact_paths, *dependent_outputs)):
            raise ValueError("business finding scope cannot use wildcard paths")
        for pointer in pointers:
            if not pointer.startswith("/") and pointer != "":
                raise ValueError("business finding JSON pointers must use RFC 6901 paths")
        canonical = {
            "finding_id": str(finding_id).strip() if finding_id is not None else None,
            "message": str(message),
            "pointers": tuple(sorted(set(pointers))),
            "artifact_paths": tuple(sorted(set(artifact_paths))),
            "dependent_outputs": tuple(sorted(set(dependent_outputs))),
            "material": bool(raw.pop("material", True)),
        }
        if has_semantic_categories:
            if isinstance(semantic_categories, (str, bytes)):
                raise TypeError("business finding semantic_categories must be a nonempty sequence")
            try:
                supplied_categories = tuple(str(category).strip() for category in semantic_categories)
            except TypeError as exc:
                raise TypeError("business finding semantic_categories must be a nonempty sequence") from exc
            if not supplied_categories or any(not category for category in supplied_categories):
                raise ValueError("business finding semantic_categories must be non-empty")
            if len(supplied_categories) != len(set(supplied_categories)):
                raise ValueError("business finding semantic_categories must not contain duplicates")
            if any(category not in _REPAIR_CATEGORY_DEPENDENCIES for category in supplied_categories):
                raise ValueError("business finding semantic category is invalid")
            canonical_categories = tuple(
                category for category in _REPAIR_CATEGORY_ORDER if category in supplied_categories
            )
            if canonical_categories != supplied_categories:
                raise ValueError("business finding semantic_categories must use canonical order")
            canonical["semantic_categories"] = canonical_categories
            # Public core callers may provide typed categories without using
            # the business facade.  Derive the same immutable scope here so
            # category provenance cannot silently omit controlled outputs or
            # canonical answer fields.  Explicit non-canonical scope is
            # rejected rather than widened by the normalizer.
            expected_dependencies = frozenset(
                dependency
                for category in canonical_categories
                for dependency in _REPAIR_CATEGORY_DEPENDENCIES[category]
            )
            expected_artifacts = frozenset(
                artifact_path
                for category in canonical_categories
                for artifact_path in _REPAIR_CATEGORY_ARTIFACT_PATHS[category]
            )
            supplied_dependencies = frozenset(
                path for path in canonical["dependent_outputs"] if not path.startswith("/")
            )
            supplied_artifacts = frozenset(canonical["artifact_paths"])
            if supplied_dependencies:
                if allow_legacy_scope_subset:
                    if not supplied_dependencies.issubset(expected_dependencies):
                        raise ValueError("business review finding dependencies do not match semantic categories")
                elif supplied_dependencies != expected_dependencies:
                    raise ValueError("business review finding dependencies do not match semantic categories")
            if supplied_artifacts:
                if allow_legacy_scope_subset:
                    if not supplied_artifacts.issubset(expected_artifacts):
                        raise ValueError("business review finding artifacts do not match semantic categories")
                elif supplied_artifacts != expected_artifacts:
                    raise ValueError("business review finding artifacts do not match semantic categories")
            if supplied_dependencies:
                dependency_scope = (
                    supplied_dependencies if allow_legacy_scope_subset else expected_dependencies
                )
                canonical["dependent_outputs"] = tuple(sorted(dependency_scope)) + tuple(
                    path for path in canonical["dependent_outputs"] if path.startswith("/")
                )
            if supplied_artifacts:
                artifact_scope = supplied_artifacts if allow_legacy_scope_subset else expected_artifacts
                canonical["artifact_paths"] = tuple(sorted(artifact_scope))
            answer_bound = bool(_REPAIR_ANSWER_BINDING_CATEGORIES.intersection(canonical_categories))
            forbidden_answer_pointers = tuple(
                pointer
                for pointer in (*canonical["pointers"], *canonical["dependent_outputs"])
                if pointer.startswith("/")
                and (
                    pointer in {"/scope", "/next_actions"}
                    or pointer.startswith("/scope/")
                    or pointer.startswith("/next_actions/")
                )
            )
            if forbidden_answer_pointers and not answer_bound:
                raise ValueError(
                    "business review canonical answer pointers require answer, calculation, or presentation semantic categories"
                )
        if not canonical["finding_id"]:
            seed = _json_bytes({key: value for key, value in canonical.items() if key != "finding_id"})
            canonical["finding_id"] = f"F-{_sha256_bytes(seed)[:12]}"
        return canonical

    @classmethod
    def _normalize_findings(
        cls,
        findings: Any,
        *,
        allow_legacy_scope_subset: bool = False,
    ) -> list[dict[str, Any]]:
        if findings is None:
            return []
        if isinstance(findings, Mapping):
            findings = (findings,)
        if isinstance(findings, (str, bytes)):
            raise TypeError("business review findings must be mappings")
        normalized = [
            cls._normalize_finding(
                value,
                index,
                allow_legacy_scope_subset=allow_legacy_scope_subset,
            )
            for index, value in enumerate(findings)
        ]
        ids = [value["finding_id"] for value in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("business finding IDs must be unique")
        return normalized

    @staticmethod
    def _business_review_scope_union(
        findings: Iterable[Mapping[str, Any]],
    ) -> tuple[list[str], list[str], list[str]]:
        """Derive packet aggregate scope from canonical finding rows."""

        pointers = sorted(
            {
                path
                for finding in findings
                for path in (*finding.get("pointers", ()), *finding.get("dependent_outputs", ()))
                if str(path).startswith("/") or path == ""
            }
        )
        artifacts = sorted({path for finding in findings for path in finding.get("artifact_paths", ())})
        dependencies = sorted(
            {
                path
                for finding in findings
                for path in finding.get("dependent_outputs", ())
                if not str(path).startswith("/")
            }
        )
        return pointers, artifacts, dependencies

    @staticmethod
    def _require_repair_findings(findings: list[dict[str, Any]]) -> None:
        """Require explicit material, pointer/artifact/dependency repair scope."""

        if not findings or not any(finding.get("material", True) for finding in findings):
            raise ValueError("repair_once requires at least one material finding")
        for finding in findings:
            if not (
                finding.get("pointers")
                or finding.get("artifact_paths")
                or finding.get("dependent_outputs")
            ):
                raise ValueError("every repair finding must authorize an exact scope")

    @classmethod
    def _normalize_repair_category_sets(cls, semantic_categories: Any) -> tuple[tuple[str, ...], ...]:
        """Validate typed semantic-category tuples supplied by the adapter."""

        if isinstance(semantic_categories, (str, bytes)):
            raise TypeError("active repair semantic categories must be tuples per finding")
        try:
            raw_findings = tuple(semantic_categories)
        except TypeError as exc:
            raise TypeError("active repair semantic categories must be tuples per finding") from exc
        normalized: list[tuple[str, ...]] = []
        for raw_categories in raw_findings:
            if isinstance(raw_categories, (str, bytes)):
                raise TypeError("active repair semantic categories must be tuples per finding")
            try:
                supplied = tuple(str(value).strip() for value in raw_categories)
            except TypeError as exc:
                raise TypeError("active repair semantic categories must be tuples per finding") from exc
            if not supplied or any(not value for value in supplied):
                raise ValueError("active repair semantic categories must be non-empty")
            if len(supplied) != len(set(supplied)):
                raise ValueError("active repair semantic categories must not contain duplicates")
            if any(value not in _REPAIR_CATEGORY_DEPENDENCIES for value in supplied):
                raise ValueError("active repair semantic category is invalid")
            canonical = tuple(category for category in _REPAIR_CATEGORY_ORDER if category in supplied)
            if canonical != supplied:
                raise ValueError("active repair semantic categories must use canonical order")
            normalized.append(canonical)
        return tuple(normalized)

    @classmethod
    def _derived_repair_scope(cls, categories: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
        dependencies = frozenset(
            dependency
            for category in categories
            for dependency in _REPAIR_CATEGORY_DEPENDENCIES[category]
        )
        artifacts = frozenset(
            artifact_path
            for category in categories
            for artifact_path in _REPAIR_CATEGORY_ARTIFACT_PATHS[category]
        )
        return dependencies, artifacts

    @staticmethod
    def _repair_scope_is_superset(original: Mapping[str, Any], proposed: Mapping[str, Any]) -> bool:
        """Return whether each proposed finding retains all prior scope."""

        for field in ("pointers", "artifact_paths", "dependent_outputs"):
            if not set(original.get(field, ())).issubset(set(proposed.get(field, ()) )):
                return False
        return True

    def _reconcile_active_business_repair_scope(
        self,
        findings: Any,
        *,
        semantic_categories: tuple[tuple[str, ...], ...],
        owner_ref: Any,
    ) -> dict[str, Any]:
        """Reconcile one active repair packet under the item transition lock."""

        # Reload the authoritative state only after acquiring the same lock
        # used by every state transition.  A caller may hold a stale
        # ItemWorkspace instance while another instance starts an attempt;
        # validating that stale in-memory state before the lock could allow a
        # later commit to erase the attempt.
        with self._state_transition_lock():
            self._reload_authoritative_state_locked()
            # Owner identity is an authorization boundary.  Verify it before
            # any execution-state migration or discard/recovery reconciliation
            # can touch durable bytes.
            self._verify_analysis_owner_locked(owner_ref)
            self._ensure_execution_state()
            self._reconcile_business_review_discard()
            return self._reconcile_active_business_repair_scope_locked(
                findings,
                semantic_categories=semantic_categories,
            )

    def _reconcile_active_business_repair_scope_locked(
        self,
        findings: Any,
        *,
        semantic_categories: tuple[tuple[str, ...], ...],
    ) -> dict[str, Any]:
        """Upgrade one active pre-fix repair packet to the current scope.

        Only :class:`BusinessReviewAdapter` supplies ``semantic_categories``;
        this private core operation is not a generic scope-extension escape
        hatch.  It preserves the original review baseline and validates all
        current changes against the exact category-derived scope before one
        atomic commit.
        """

        self._ensure_not_terminal()
        self._require_no_active_attempt()
        state = self._state
        review = state.get("review")
        if state.get("lifecycle_state") != "work":
            raise ValueError("active repair scope reconciliation requires lifecycle_state=work")
        if not isinstance(review, Mapping) or review.get("status") != "pending" or review.get("verdict") is not None:
            raise ValueError("active repair scope reconciliation requires a pending review")
        if any(review.get(field) is not None for field in ("strength", "reviewer_ref", "draft_hash")):
            raise ValueError("active repair scope reconciliation requires an untouched pending review")
        if int(state.get("business_repair_count", -1)) < 1:
            raise ValueError("active repair scope reconciliation requires a business repair")
        if state.get("integration_state") != "pending":
            raise ValueError("active repair scope reconciliation requires pending integration")
        if state.get("terminal_outcome") is not None or state.get("terminal_intent") is not None:
            raise ValueError("active repair scope reconciliation requires a non-terminal item")

        # A packet produced before a program-owned dependency was added may
        # be a valid active baseline with a strict subset of today's derived
        # scope.  Read it under the reconciliation-only legacy validator; the
        # proposed typed finding is still checked against the exact current
        # category union below before any commit.  Keep the call itself
        # argument-free so instrumentation/lock tests that wrap this read
        # continue to observe the same boundary.
        self._allow_legacy_business_review_scope = True
        try:
            packet = self._read_business_review()
        finally:
            self._allow_legacy_business_review_scope = False
        if packet is None:
            raise ValueError("active repair scope reconciliation requires an existing packet")
        if packet.get("review_scope") != "full" or packet.get("repair_active") is not True:
            raise ValueError("active repair scope reconciliation requires a full active repair packet")
        if packet.get("targeted_recheck") is not False:
            raise ValueError("active repair scope reconciliation requires a pre-recheck packet")

        normalized = self._normalize_findings(findings)
        categories_by_finding = self._normalize_repair_category_sets(semantic_categories)
        if len(categories_by_finding) != len(normalized):
            raise ValueError("active repair semantic categories must match finding count")
        original = self._normalize_findings(
            packet.get("findings"),
            allow_legacy_scope_subset=True,
        )
        self._require_repair_findings(original)
        if len(original) != len(normalized):
            raise ValueError("active repair finding count cannot change")
        for prior, proposed, categories in zip(original, normalized, categories_by_finding):
            persisted_categories = prior.get("semantic_categories")
            if persisted_categories is None:
                raise ValueError("active repair packet lacks semantic category provenance")
            if (
                prior["finding_id"] != proposed["finding_id"]
                or prior["message"] != proposed["message"]
                or prior["material"] is not proposed["material"]
            ):
                raise ValueError("active repair finding identity, message, or material flag changed")
            expected_dependencies, expected_artifacts = self._derived_repair_scope(categories)
            proposed_dependencies = frozenset(proposed.get("dependent_outputs", ()))
            proposed_artifacts = frozenset(proposed.get("artifact_paths", ()))
            if proposed_dependencies != expected_dependencies:
                raise ValueError("active repair scope does not match exact semantic category union")
            if proposed_artifacts != expected_artifacts:
                raise ValueError("active repair artifact scope does not match exact semantic category union")
            prior_pointers = frozenset(prior.get("pointers", ()))
            proposed_pointers = frozenset(proposed.get("pointers", ()))
            prior_dependencies = frozenset(prior.get("dependent_outputs", ()))
            prior_artifacts = frozenset(prior.get("artifact_paths", ()))
            if tuple(categories) != tuple(persisted_categories):
                raise ValueError("active repair semantic category set changed")
            # A packet opened by an older program version may have captured a
            # strict subset of the current category-derived scope.  Permit
            # only the program-derived additive upgrade: every prior path
            # must remain authorized, while the proposed finding must still
            # equal the exact scope derived from its unchanged categories.
            if not prior_dependencies.issubset(proposed_dependencies):
                raise ValueError("active repair dependency scope removed")
            if not prior_artifacts.issubset(proposed_artifacts):
                raise ValueError("active repair artifact scope removed")
            if proposed_pointers != prior_pointers:
                raise ValueError("active repair pointer scope changed")

        proposed_packet = copy.deepcopy(packet)
        proposed_packet["findings"] = normalized
        proposed_packet["allowed_pointers"] = sorted(
            {
                pointer
                for finding in normalized
                for pointer in (*finding["pointers"], *finding["dependent_outputs"])
                if pointer.startswith("/") or pointer == ""
            }
        )
        proposed_packet["allowed_artifact_paths"] = sorted(
            {path for finding in normalized for path in finding["artifact_paths"]}
        )
        proposed_packet["allowed_dependencies"] = sorted(
            {
                path
                for finding in normalized
                for path in finding["dependent_outputs"]
                if not path.startswith("/")
            }
        )
        # Validate the packet and every current draft/artifact change against
        # the proposed scope before touching the persisted baseline.  This
        # intentionally does not recompute any before/after hash fields.
        self._validate_business_review_payload(proposed_packet)
        self._repair_scope_check(_packet=proposed_packet)
        self._commit_business_review(proposed_packet, copy.deepcopy(dict(state)))
        self._emit(
            "item_business_repair_scope_reconciled",
            review_scope=proposed_packet["review_scope"],
            finding_count=len(proposed_packet["findings"]),
            repair_active=proposed_packet["repair_active"],
            repair_count=state["business_repair_count"],
        )
        return copy.deepcopy(proposed_packet)

    def _read_business_review(self, *, allow_legacy_scope_subset: bool = False) -> dict[str, Any] | None:
        allow_legacy_scope_subset = allow_legacy_scope_subset or bool(
            getattr(self, "_allow_legacy_business_review_scope", False)
        )
        path = self.business_review_path
        if not path.exists():
            return None
        _assert_regular_no_symlink(path, label="business review artifact")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("business review artifact is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("business review artifact must be an object")
        self._validate_business_review_payload(
            value,
            allow_legacy_scope_subset=allow_legacy_scope_subset,
        )
        return copy.deepcopy(value)

    def _deactivate_active_business_repair(self) -> None:
        """Mark an active repair packet historical before terminal failure.

        Technical failure is a terminal item-local outcome, not a business
        re-review.  A packet left with ``repair_active=true`` would advertise
        authority that no longer exists and could make a later recovery/load
        path attempt stale scope reconciliation.  Keep the packet bytes and
        reviewer findings, but clear only the active flag.  Parsing is kept
        deliberately shallow: a stale/corrupt historical packet must never
        block terminalization of the item itself; the fixed item-local path is
        still checked for symlink escapes.
        """

        path = self.business_review_path
        if not path.exists() and not path.is_symlink():
            return
        _assert_regular_no_symlink(path, label="business review artifact")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(raw, Mapping) or raw.get("repair_active") is not True:
            return
        packet = copy.deepcopy(dict(raw))
        packet["repair_active"] = False
        _atomic_write_json(path, packet)
        self._emit("item_business_repair_deactivated", artifact=f"work/{_BUSINESS_REVIEW_FILENAME}")

    def _validate_business_review_payload(
        self,
        value: Mapping[str, Any],
        *,
        allow_legacy_scope_subset: bool = False,
    ) -> None:
        """Validate one complete review packet without mutating state."""

        required = {
            "item_id",
            "review_scope",
            "reviewed_draft_hash",
            "before_hash",
            "before_snapshot",
            "before_pointer_hashes",
            "before_artifact_hashes",
            "after_pointer_hashes",
            "after_artifact_hashes",
            "findings",
            "allowed_pointers",
            "allowed_artifact_paths",
            "allowed_dependencies",
            "changed_pointers",
            "unchanged_paths",
            "unchanged_aggregate_hash",
            "repair_active",
            "targeted_recheck",
        }
        if set(value) != required:
            raise ValueError("business review artifact fields are invalid")
        if value.get("item_id") != self.item_id:
            raise ValueError("business review artifact item_id is invalid")
        if value.get("review_scope") not in {"full", "targeted"}:
            raise ValueError("business review artifact review_scope is invalid")
        for name in ("reviewed_draft_hash", "before_hash", "unchanged_aggregate_hash"):
            if not _is_sha256(value.get(name)):
                raise ValueError(f"business review artifact {name} is invalid")
        for name in ("before_pointer_hashes", "after_pointer_hashes"):
            if not isinstance(value.get(name), Mapping):
                raise ValueError("business review artifact pointer hashes are invalid")
        for name in ("before_artifact_hashes", "after_artifact_hashes"):
            if not isinstance(value.get(name), Mapping):
                raise ValueError("business review artifact artifact hashes are invalid")
        if not isinstance(value.get("before_pointer_hashes"), Mapping):
            raise ValueError("business review artifact pointer hashes are invalid")
        if not isinstance(value.get("before_artifact_hashes"), Mapping):
            raise ValueError("business review artifact artifact hashes are invalid")
        for digest in (
            list(value["before_pointer_hashes"].values())
            + list(value["after_pointer_hashes"].values())
            + list(value["before_artifact_hashes"].values())
            + list(value["after_artifact_hashes"].values())
        ):
            if not _is_sha256(digest):
                raise ValueError("business review artifact contains an invalid hash")
        if _sha256_bytes(_json_bytes(value["before_snapshot"])) != value["before_hash"]:
            raise ValueError("business review artifact before_hash does not match snapshot")
        if dict(_pointer_hashes(value["before_snapshot"])) != dict(value["before_pointer_hashes"]):
            raise ValueError("business review artifact before pointer hashes do not match snapshot")
        if not isinstance(value.get("findings"), list):
            raise ValueError("business review artifact findings are invalid")
        normalized_findings = self._normalize_findings(
            value["findings"],
            allow_legacy_scope_subset=allow_legacy_scope_subset,
        )
        for finding in normalized_findings:
            categories = finding.get("semantic_categories")
            if categories is None:
                raise ValueError("business review finding lacks semantic category provenance")
            expected_dependencies, expected_artifacts = self._derived_repair_scope(tuple(categories))
            actual_dependencies = frozenset(finding.get("dependent_outputs", ()))
            if allow_legacy_scope_subset:
                if not actual_dependencies.issubset(expected_dependencies):
                    raise ValueError("business review finding dependencies do not match semantic categories")
            elif actual_dependencies != expected_dependencies:
                # Preserve the historical minimal packet accepted by direct
                # durable callers: when no artifact/dependency scope was
                # supplied, the typed category remains provenance-only and
                # cannot authorize any work/ path.  BusinessReviewAdapter
                # findings always carry their exact program-derived scope
                # (including the answer-owned reviewer packet).
                if actual_dependencies or finding.get("artifact_paths"):
                    raise ValueError("business review finding dependencies do not match semantic categories")
            actual_artifacts = frozenset(finding.get("artifact_paths", ()))
            # Typed findings emitted by BusinessReviewAdapter carry the exact
            # controlled result root.  Retain the core's historical minimal
            # packet form when a direct durable caller supplied no artifact
            # scope; it cannot authorize that root or any other artifact.
            if allow_legacy_scope_subset:
                if not actual_artifacts.issubset(expected_artifacts):
                    raise ValueError("business review finding artifacts do not match semantic categories")
            elif actual_artifacts and actual_artifacts != expected_artifacts:
                raise ValueError("business review finding artifacts do not match semantic categories")
            has_evidence_category = bool(_REPAIR_EVIDENCE_BINDING_CATEGORIES.intersection(categories))
            has_visual_evidence_binding = (
                "/visuals" in set(finding.get("pointers", ()))
                and _REPAIR_VISUAL_EVIDENCE_BINDING_CATEGORIES.issubset(categories)
            )
            has_evidence_pointer = "/evidence_refs" in set(finding.get("pointers", ()))
            if (has_evidence_category or has_visual_evidence_binding) != has_evidence_pointer:
                raise ValueError("business review finding /evidence_refs pointer does not match semantic categories")
            required_answer_pointers = {"/scope", "/next_actions"}
            if actual_artifacts.intersection(expected_artifacts) and _REPAIR_ANSWER_BINDING_CATEGORIES.intersection(categories):
                if not required_answer_pointers.issubset(set(finding.get("pointers", ()) )):
                    raise ValueError(
                        "business review finding answer pointers do not match semantic categories"
                    )
        for name in (
            "allowed_pointers",
            "allowed_artifact_paths",
            "allowed_dependencies",
            "changed_pointers",
            "unchanged_paths",
        ):
            if not isinstance(value.get(name), list) or any(not isinstance(path, str) for path in value[name]):
                raise ValueError(f"business review artifact {name} is invalid")
        if any(
            path == "*"
            for name in ("allowed_pointers", "allowed_artifact_paths", "allowed_dependencies")
            for path in value[name]
        ):
            raise ValueError("business review artifact cannot use wildcard scope")
        if _PROGRAM_CONTEXT_ARTIFACT_PATHS.intersection(
            str(path).replace("\\", "/")
            for name in ("allowed_artifact_paths", "allowed_dependencies")
            for path in value[name]
        ):
            raise ValueError("business review artifact cannot authorize program-owned context artifacts")
        if normalized_findings:
            expected_pointers, expected_artifacts, expected_dependencies = self._business_review_scope_union(
                normalized_findings
            )
            if value["allowed_pointers"] != expected_pointers:
                raise ValueError("business review artifact allowed_pointers do not match findings")
            if value["allowed_artifact_paths"] != expected_artifacts:
                raise ValueError("business review artifact allowed_artifact_paths do not match findings")
            if value["allowed_dependencies"] != expected_dependencies:
                raise ValueError("business review artifact allowed_dependencies do not match findings")
        if not isinstance(value.get("repair_active"), bool) or not isinstance(value.get("targeted_recheck"), bool):
            raise ValueError("business review artifact flags are invalid")

    def _write_business_review(
        self,
        value: Mapping[str, Any],
        *,
        touch_state: bool = True,
        emit: bool = True,
    ) -> None:
        self._ensure_not_terminal()
        self._validate_business_review_payload(value)
        destination = self.business_review_path
        _assert_regular_no_symlink(destination, label="business review artifact")
        _atomic_write_json(destination, value)
        if touch_state:
            self._touch_state()
        if emit:
            self._emit("item_business_review_packet", artifact="work/business_review.json")

    def _commit_business_review(self, packet: Mapping[str, Any], state: Mapping[str, Any]) -> None:
        """Publish a validated packet and state together, rolling back on I/O failure."""

        # Packet and state are one authority boundary.  Rebind acquires this
        # same per-item lock before its authoritative item reload; holding it
        # across both writes prevents a context transition from observing a
        # packet without its matching review state (or vice versa).
        with self._state_transition_lock():
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            packet_path = self.business_review_path
            prior_state_bytes = state_path.read_bytes()
            prior_state = copy.deepcopy(self._state)
            prior_packet_exists = packet_path.exists()
            prior_packet_bytes = packet_path.read_bytes() if prior_packet_exists else None
            try:
                self._write_business_review(packet, touch_state=False, emit=False)
                # The lock is already held; bypass the public wrapper so an
                # injected or nested lock cannot split the transaction.
                self._persist_state_unlocked(state)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                self._state = prior_state
                try:
                    if prior_packet_exists and prior_packet_bytes is not None:
                        _atomic_write_bytes(packet_path, prior_packet_bytes)
                    else:
                        packet_path.unlink(missing_ok=True)
                        _fsync_directory(packet_path.parent)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("business review commit rollback failed") from exc
                raise
        self._emit("item_business_review_packet", artifact="work/business_review.json")

    def _current_draft_value_and_hash(self) -> tuple[Any, str]:
        draft = self.draft_root
        _assert_regular_no_symlink(draft, label="draft artifact")
        if not draft.is_file():
            raise FileNotFoundError(draft)
        payload = draft.read_bytes()
        return self._canonical_draft_value(payload), _sha256_bytes(payload)

    def _validate_repair_handoff(
        self,
        packet: Mapping[str, Any],
        *,
        current_hashes: Mapping[str, str],
    ) -> frozenset[str]:
        """Validate one owner handoff that was produced by the current repair attempt.

        ``write_handoff`` remains a general work-artifact writer for ordinary
        execution and recovery.  A changed handoff is different while a
        business repair is being reconciled: it may advance the repair only
        when its typed analyst-handoff payload binds the current item owner,
        completed latest attempt, repair finding, and every referenced output
        or receipt to the bytes currently on disk.  This keeps the exception
        narrow without turning ``work/handoff.json`` into a general repair
        allowlist entry.
        """

        destination = self._resolve_item_subpath(_HANDOFF_ARTIFACT_PATH)
        _assert_regular_no_symlink(destination, label="handoff artifact")
        if not destination.is_file():
            raise ValueError("active repair handoff artifact is missing")
        raw = destination.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("active repair handoff artifact is invalid") from exc
        if not isinstance(value, Mapping):
            raise ValueError("active repair handoff artifact must be an object")
        if raw != _json_bytes(value):
            raise ValueError("active repair handoff artifact is not canonical JSON")
        if set(value) != _ANALYST_HANDOFF_FIELDS:
            raise ValueError("active repair handoff artifact fields are invalid")

        def required_text(field_name: str) -> str:
            field_value = value.get(field_name)
            if not isinstance(field_value, str) or not field_value.strip() or field_value != field_value.strip():
                raise ValueError(f"active repair handoff {field_name} is invalid")
            return field_value

        if value.get("schema_version") != _ANALYST_HANDOFF_SCHEMA:
            raise ValueError("active repair handoff schema_version is invalid")
        if value.get("item_id") != self.item_id:
            raise ValueError("active repair handoff item_id does not match workspace")
        owner_binding = self._read_analysis_owner()
        if owner_binding is None:
            raise ValueError("active repair handoff requires a bound Analytical Owner")
        if value.get("owner_ref") != owner_binding["owner_ref"]:
            raise ValueError("active repair handoff owner_ref does not match item owner")
        if value.get("requirement") != self.original_text:
            raise ValueError("active repair handoff requirement is stale")
        if value.get("analysis_status") != "completed":
            raise ValueError("active repair handoff analysis_status is invalid")
        if value.get("review_status") != "pending_targeted_business_recheck":
            raise ValueError("active repair handoff review_status is invalid")
        if not isinstance(value.get("analysis_output_summary"), Mapping):
            raise ValueError("active repair handoff analysis_output_summary is invalid")
        freeze_note = required_text("freeze_note")
        if not freeze_note:
            raise ValueError("active repair handoff freeze_note is invalid")

        repair_count = value.get("business_repair_count")
        if isinstance(repair_count, bool) or not isinstance(repair_count, int):
            raise ValueError("active repair handoff business_repair_count is invalid")
        if repair_count != int(self._state.get("business_repair_count", -1)):
            raise ValueError("active repair handoff business_repair_count does not match item state")
        if packet.get("repair_active") is not True or packet.get("targeted_recheck") is not False:
            raise ValueError("active repair handoff requires the current full repair")
        finding_ids = {
            str(finding.get("finding_id"))
            for finding in packet.get("findings", ())
            if isinstance(finding, Mapping) and finding.get("finding_id")
        }
        if value.get("repair_finding_id") not in finding_ids:
            raise ValueError("active repair handoff repair_finding_id is not in the active repair")

        if self._state.get("active_attempt_id") is not None:
            raise ValueError("active repair handoff requires a completed attempt")
        attempts = self._state.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise ValueError("active repair handoff attempt state is invalid")
        attempt_id = required_text("attempt_id")
        attempt = attempts[-1]
        if not isinstance(attempt, Mapping) or attempt.get("attempt_id") != attempt_id:
            raise ValueError("active repair handoff attempt_id is not the current attempt")
        if attempt.get("lane_id") != owner_binding["owner_ref"]:
            raise ValueError("active repair handoff attempt owner does not match item owner")
        if attempt.get("role") != "Analytical Owner":
            raise ValueError("active repair handoff attempt role is invalid")
        if attempt.get("route") != "requirement":
            raise ValueError("active repair handoff attempt route is invalid")
        if attempt.get("status") != "completed":
            raise ValueError("active repair handoff attempt is not completed")
        baseline = attempt.get("baseline")
        packet_after = packet.get("after_artifact_hashes")
        baseline_hashes = baseline.get("hashes") if isinstance(baseline, Mapping) else None
        packet_after_hashes = packet_after if isinstance(packet_after, Mapping) else None
        handoff_finding = next(
            (
                finding
                for finding in packet.get("findings", ())
                if isinstance(finding, Mapping)
                and finding.get("finding_id") == value.get("repair_finding_id")
            ),
            None,
        )
        if handoff_finding is None:
            raise ValueError("active repair handoff repair finding scope is invalid")
        handoff_scope_roots = tuple(
            str(root).replace("\\", "/")
            for name in ("artifact_paths", "dependent_outputs")
            for root in handoff_finding.get(name, ())
            if not str(root).startswith("/")
        )

        def packet_scope_allows(path: str) -> bool:
            normalized = path.replace("\\", "/")
            if normalized == _DRAFT_FILENAME:
                return True
            return any(
                normalized == root or normalized.startswith(root.rstrip("/") + "/")
                for root in handoff_scope_roots
            )

        def packet_after_matches_current(path: str) -> bool:
            return (
                path in packet_after_hashes
                and packet_after_hashes.get(path) == current_hashes.get(path)
            )

        def current_work_file(
            raw_ref: Any,
            *,
            field_name: str,
            prefix: str | None = None,
        ) -> tuple[str, Path]:
            """Resolve a typed handoff reference to one current item-local file."""

            if (
                not isinstance(raw_ref, str)
                or not raw_ref
                or raw_ref != raw_ref.strip()
                or "\\" in raw_ref
                or "\x00" in raw_ref
            ):
                raise ValueError(f"active repair handoff {field_name} path is invalid")
            pure = PurePath(raw_ref)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or pure.as_posix() != raw_ref
                or not raw_ref.startswith("work/")
            ):
                raise ValueError(f"active repair handoff {field_name} path is invalid")
            if field_name == "evidence_refs" and raw_ref in _ANALYST_HANDOFF_RESERVED_REFS:
                raise ValueError(f"active repair handoff {field_name} path is invalid")
            if prefix is not None and not raw_ref.startswith(prefix):
                raise ValueError(f"active repair handoff {field_name} path is invalid")
            path = self._resolve_item_subpath(raw_ref)
            _assert_regular_no_symlink(path, label=f"active repair handoff {field_name}")
            if not path.is_file():
                raise ValueError(f"active repair handoff {field_name} path is missing")
            if raw_ref not in current_hashes:
                raise ValueError(
                    f"active repair handoff {field_name} path is outside current artifact progress"
                )
            return raw_ref, path

        def validate_handoff_execution_refs() -> frozenset[str]:
            """Validate every current handoff-declared execution reference.

            This runs before repair-baseline reconciliation so these exact,
            hash-bound references can provide a narrowly typed execution
            provenance closure.  The finding scope still gates which of them
            may explain progress drift below.
            """

            script_ref, _ = current_work_file(
                value.get("calculation_script"),
                field_name="calculation_script",
                prefix="work/calculations/",
            )
            outputs = value.get("calculation_outputs")
            if (
                not isinstance(outputs, list)
                or not outputs
                or any(not isinstance(output, str) for output in outputs)
                or len(outputs) != len(set(outputs))
            ):
                raise ValueError("active repair handoff calculation_outputs are invalid")
            output_paths = {
                current_work_file(output, field_name="calculation_outputs", prefix="work/results/")[0]: output
                for output in outputs
            }
            if len(output_paths) != len(outputs):
                raise ValueError("active repair handoff calculation_outputs are invalid")

            evidence_refs = value.get("evidence_refs")
            if (
                not isinstance(evidence_refs, list)
                or not evidence_refs
                or any(not isinstance(ref, str) for ref in evidence_refs)
                or len(evidence_refs) != len(set(evidence_refs))
            ):
                raise ValueError("active repair handoff evidence_refs are invalid")
            evidence_paths = {
                current_work_file(ref, field_name="evidence_refs")[0]: ref
                for ref in evidence_refs
            }
            if not set(output_paths).issubset(evidence_paths):
                raise ValueError("active repair handoff evidence_refs do not cover calculation outputs")

            output_hashes = value.get("output_hashes")
            if not isinstance(output_hashes, Mapping):
                raise ValueError("active repair handoff output_hashes are invalid")
            expected_output_refs = {script_ref, *output_paths}
            if set(output_hashes) != expected_output_refs:
                raise ValueError("active repair handoff output_hashes are incomplete")
            for ref, expected_hash in output_hashes.items():
                if not _is_sha256(expected_hash):
                    raise ValueError("active repair handoff output_hashes are invalid")
                _ref, path = current_work_file(ref, field_name="output_hashes")
                actual_hash = _sha256_file(path)
                if actual_hash != expected_hash or current_hashes.get(ref) != expected_hash:
                    raise ValueError(f"active repair handoff output hash is stale: {ref}")

            receipt_refs = {
                ref for ref in evidence_paths if ref.startswith("work/script_receipts/")
            }
            receipt_hashes = value.get("receipt_hashes")
            if (
                not isinstance(receipt_hashes, Mapping)
                or set(receipt_hashes) != receipt_refs
                or not receipt_refs
            ):
                raise ValueError("active repair handoff receipt_hashes are incomplete")
            for ref, expected_hash in receipt_hashes.items():
                if not _is_sha256(expected_hash):
                    raise ValueError("active repair handoff receipt_hashes are invalid")
                receipt_path = PurePath(ref)
                if (
                    receipt_path.parent.as_posix() != "work/script_receipts"
                    or not receipt_path.name.startswith("receipt-")
                    or not receipt_path.name.endswith(".json")
                ):
                    raise ValueError("active repair handoff receipt path is invalid")
                _ref, path = current_work_file(
                    ref,
                    field_name="receipt_hashes",
                    prefix="work/script_receipts/",
                )
                actual_hash = _sha256_file(path)
                if actual_hash != expected_hash or current_hashes.get(ref) != expected_hash:
                    raise ValueError(f"active repair handoff receipt hash is stale: {ref}")

            limits = value.get("limits")
            if (
                not isinstance(limits, list)
                or any(not isinstance(limit, str) or not limit.strip() for limit in limits)
                or len(limits) != len(set(limits))
            ):
                raise ValueError("active repair handoff limits are invalid")
            # Only references covered by a declared byte hash can authorize a
            # newly-produced artifact at the repair boundary.  Evidence refs
            # without an accompanying hash remain subject to the review packet's
            # existing allowed paths, preventing the handoff from becoming a
            # generic item-local allowlist.
            return frozenset({script_ref, *output_paths, *receipt_refs})

        handoff_execution_refs = validate_handoff_execution_refs()

        def baseline_omission_matches_current(path: str) -> bool:
            """Allow an unchanged authorized file omitted by a stale packet map."""

            return (
                path not in program_paths
                and path in current_hashes
                and current_hashes.get(path) == baseline_hashes.get(path)
                and (
                    handoff_baseline_matches_current(str(path))
                    or (
                        is_execution_history_path(str(path))
                        and (
                            handoff_execution_history_matches(str(path))
                            or prior_execution_history_matches(str(path))
                        )
                    )
                    or (
                        not is_execution_history_path(str(path))
                        and packet_scope_allows(str(path))
                    )
                )
            )

        program_paths = set(_PROGRAM_CONTEXT_ARTIFACT_PATHS)
        repair_upgrade_paths = {
            "work/analysis_context.json",
            "work/analysis_context_repair_upgrades.jsonl",
        }
        unsupported_program_paths = program_paths - repair_upgrade_paths
        existing_program_paths = {
            path
            for path in program_paths
            if (
                self._resolve_item_subpath(path).exists()
                or self._resolve_item_subpath(path).is_symlink()
            )
        }
        current_program_paths = set(current_hashes) & program_paths
        if current_program_paths != existing_program_paths:
            raise ValueError("active repair handoff program context current paths are invalid")
        if existing_program_paths & unsupported_program_paths:
            raise ValueError("active repair handoff program context has unauthorized artifacts")

        # A finding that explicitly authorizes ``work/results`` may rely on
        # the exact execution references declared by this typed handoff when
        # those references explain a packet/baseline drift.  The output files
        # themselves still have to be under that finding's declared roots;
        # script and receipt paths are admitted only through this fixed,
        # hash-bound handoff schema.
        handoff_scope_allows_execution = packet_scope_allows("work/results")

        def handoff_ref_matches_current(path: str) -> bool:
            return (
                handoff_scope_allows_execution
                and path in handoff_execution_refs
                and (
                    not path.startswith("work/results/")
                    or packet_scope_allows(path)
                )
            )

        def is_execution_history_path(path: str) -> bool:
            """Recognize the fixed direct receipt/log families only."""

            pure = PurePath(path)
            return pure.parent.as_posix() == "work/script_receipts" or (
                pure.parent.as_posix() == "work/.analysis-run"
                and pure.name.startswith("receipt-")
            )

        receipt_fields = frozenset(
            {
                "receipt_id",
                "phase",
                "script_path",
                "script_hash",
                "context_path",
                "context_hash",
                "source_hash",
                "started_at",
                "finished_at",
                "wall_seconds",
                "exit_code",
                "stdout",
                "stderr",
                "stdout_truncated",
                "stderr_truncated",
                "timed_out",
                "output_limited",
                "error_type",
                "error_category",
                "traceback",
                "output_hashes",
                "receipt_path",
            }
        )
        receipt_phases = frozenset({"compile", "dependency", "dependency_check", "smoke", "full"})
        packet_before_for_receipts = packet.get("before_artifact_hashes")
        packet_after_for_receipts = packet_after_hashes if isinstance(packet_after_hashes, Mapping) else {}
        program_context_rebased_for_receipts = any(
            isinstance(mapping, Mapping)
            and (
                mapping.get("work/analysis_context.json")
                != packet_after_for_receipts.get("work/analysis_context.json")
                or mapping.get("work/analysis_context_repair_upgrades.jsonl")
                != packet_after_for_receipts.get("work/analysis_context_repair_upgrades.jsonl")
            )
            for mapping in (baseline_hashes, packet_before_for_receipts)
        )

        def item_relative_absolute(raw_path: Any, *, label: str) -> tuple[str, Path]:
            if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
                raise ValueError(f"active repair handoff {label} is invalid")
            absolute = Path(raw_path)
            if not absolute.is_absolute():
                raise ValueError(f"active repair handoff {label} is invalid")
            try:
                relative = absolute.relative_to(self.item_root)
            except ValueError as exc:
                raise ValueError(f"active repair handoff {label} escapes item") from exc
            normalized = relative.as_posix()
            if (
                not normalized.startswith("work/")
                or ".." in relative.parts
                or str(self._resolve_item_subpath(relative)) != str(absolute)
            ):
                raise ValueError(f"active repair handoff {label} is invalid")
            path = self._resolve_item_subpath(relative)
            _assert_regular_no_symlink(path, label=f"active repair handoff {label}")
            return normalized, path

        def current_manifest_identity() -> tuple[str, str]:
            manifest_ref = "work/analysis_context.json"
            manifest = self._resolve_item_subpath(manifest_ref)
            _assert_regular_no_symlink(manifest, label="analysis context manifest")
            if not manifest.is_file():
                raise ValueError("active repair handoff analysis context manifest is missing")
            try:
                manifest_bytes = manifest.read_bytes()
                manifest_value = json.loads(manifest_bytes.decode("utf-8"))
                from .analysis import (
                    _json_bytes as _analysis_json_bytes,
                    _manifest_bytes as _analysis_manifest_bytes,
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ImportError) as exc:
                raise ValueError("active repair handoff analysis context manifest is invalid") from exc
            if (
                not isinstance(manifest_value, Mapping)
                or manifest_bytes != _analysis_manifest_bytes(manifest_value)
                or not _is_sha256(manifest_value.get("manifest_hash"))
                or manifest_value.get("manifest_hash")
                != _sha256_bytes(
                    _analysis_json_bytes(
                        {key: item for key, item in manifest_value.items() if key != "manifest_hash"}
                    )
                )
            ):
                raise ValueError("active repair handoff analysis context manifest is invalid")
            source_identity = manifest_value.get("source_identity")
            source_hash = source_identity.get("content_hash") if isinstance(source_identity, Mapping) else None
            if not _is_sha256(source_hash):
                raise ValueError("active repair handoff analysis context source identity is invalid")
            return str(manifest_value["manifest_hash"]), str(source_hash)

        def validate_script_receipt(
            receipt_ref: str,
            *,
            expected_script_hash: str | None,
        ) -> tuple[str, frozenset[str]]:
            """Validate one canonical ScriptExecutionReceipt and its outputs."""

            pure = PurePath(receipt_ref)
            if (
                pure.parent.as_posix() != "work/script_receipts"
                or not pure.name.startswith("receipt-")
                or not pure.name.endswith(".json")
                or receipt_ref not in current_hashes
            ):
                raise ValueError("active repair handoff receipt path is invalid")
            receipt_path = self._resolve_item_subpath(receipt_ref)
            _assert_regular_no_symlink(receipt_path, label="active repair handoff receipt")
            try:
                receipt_bytes = receipt_path.read_bytes()
                receipt_value = json.loads(receipt_bytes.decode("utf-8"))
                from .analysis import _json_file_bytes as _analysis_json_file_bytes
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ImportError) as exc:
                raise ValueError("active repair handoff receipt is invalid") from exc
            if (
                not isinstance(receipt_value, Mapping)
                or set(receipt_value) != receipt_fields
                or receipt_bytes != _analysis_json_file_bytes(receipt_value)
            ):
                raise ValueError("active repair handoff receipt is not canonical")
            receipt_id = receipt_value.get("receipt_id")
            if not isinstance(receipt_id, str) or receipt_id != pure.stem:
                raise ValueError("active repair handoff receipt identity is invalid")
            phase = receipt_value.get("phase")
            if phase not in receipt_phases:
                raise ValueError("active repair handoff receipt phase is invalid")

            script_ref, script_path = item_relative_absolute(
                receipt_value.get("script_path"),
                label="receipt script_path",
            )
            if not script_ref.startswith("work/calculations/"):
                raise ValueError("active repair handoff receipt script_path is invalid")
            script_hash = receipt_value.get("script_hash")
            if not _is_sha256(script_hash):
                raise ValueError("active repair handoff receipt script_hash is invalid")
            if expected_script_hash is not None and script_hash != expected_script_hash:
                raise ValueError("active repair handoff receipt script identity is stale")
            if not script_path.is_file() or _sha256_file(script_path) != script_hash:
                raise ValueError("active repair handoff receipt script hash is stale")

            context_path = self._resolve_item_subpath("work/analysis_context.json")
            if receipt_value.get("context_path") != str(context_path):
                raise ValueError("active repair handoff receipt context_path is invalid")
            context_hash, source_hash = current_manifest_identity()
            receipt_context_hash = receipt_value.get("context_hash")
            if not _is_sha256(receipt_context_hash):
                raise ValueError("active repair handoff receipt context hash is invalid")
            if receipt_context_hash != context_hash:
                # A public implementation upgrade legitimately rebases the
                # current context after an attempt's receipts were written.
                # Accept that historical context only when an unchanged,
                # canonical receipt already present in the immutable attempt
                # baseline carries the same identity; a caller cannot invent
                # a new context hash through the handoff itself.
                known_context_hashes = {context_hash}
                if isinstance(baseline_hashes, Mapping):
                    for candidate_ref, candidate_hash in baseline_hashes.items():
                        candidate_pure = PurePath(str(candidate_ref))
                        if (
                            candidate_pure.parent.as_posix() != "work/script_receipts"
                            or not candidate_pure.name.startswith("receipt-")
                            or not candidate_pure.name.endswith(".json")
                            or current_hashes.get(candidate_ref) != candidate_hash
                        ):
                            continue
                        candidate_path = self._resolve_item_subpath(str(candidate_ref))
                        _assert_regular_no_symlink(candidate_path, label="active repair handoff receipt")
                        try:
                            candidate_value = json.loads(candidate_path.read_text(encoding="utf-8"))
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if (
                            isinstance(candidate_value, Mapping)
                            and candidate_value.get("context_path") == str(context_path)
                            and _is_sha256(candidate_value.get("context_hash"))
                        ):
                            known_context_hashes.add(str(candidate_value["context_hash"]))
                if (
                    receipt_context_hash not in known_context_hashes
                    and not program_context_rebased_for_receipts
                ):
                    raise ValueError("active repair handoff receipt context hash is stale")
            if receipt_value.get("source_hash") != source_hash:
                raise ValueError("active repair handoff receipt source hash is stale")

            for field_name in ("started_at", "finished_at"):
                field_value = receipt_value.get(field_name)
                if not isinstance(field_value, str) or not field_value.strip():
                    raise ValueError(f"active repair handoff receipt {field_name} is invalid")
            wall_seconds = receipt_value.get("wall_seconds")
            if isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float)) or wall_seconds < 0:
                raise ValueError("active repair handoff receipt wall_seconds is invalid")
            exit_code = receipt_value.get("exit_code")
            if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
                raise ValueError("active repair handoff receipt exit_code is invalid")
            for field_name in ("stdout", "stderr"):
                if not isinstance(receipt_value.get(field_name), str):
                    raise ValueError(f"active repair handoff receipt {field_name} is invalid")
            for field_name in (
                "stdout_truncated",
                "stderr_truncated",
                "timed_out",
                "output_limited",
            ):
                if not isinstance(receipt_value.get(field_name), bool):
                    raise ValueError(f"active repair handoff receipt {field_name} is invalid")
            for field_name in ("error_type", "error_category", "traceback"):
                field_value = receipt_value.get(field_name)
                if field_value is not None and not isinstance(field_value, str):
                    raise ValueError(f"active repair handoff receipt {field_name} is invalid")

            output_hashes = receipt_value.get("output_hashes")
            if not isinstance(output_hashes, Mapping):
                raise ValueError("active repair handoff receipt output_hashes are invalid")
            output_refs: set[str] = set()
            for raw_output_path, expected_hash in output_hashes.items():
                output_ref, output_path = item_relative_absolute(
                    raw_output_path,
                    label="receipt output path",
                )
                if not (
                    output_ref.startswith("work/.analysis-run/")
                    or output_ref.startswith("work/results/")
                ):
                    raise ValueError("active repair handoff receipt output path is invalid")
                if not _is_sha256(expected_hash):
                    raise ValueError("active repair handoff receipt output hash is invalid")
                if output_path.is_file():
                    if _sha256_file(output_path) != expected_hash:
                        raise ValueError("active repair handoff receipt output hash is stale")
                    if output_ref not in current_hashes or current_hashes.get(output_ref) != expected_hash:
                        raise ValueError("active repair handoff receipt output is outside current progress")
                output_refs.add(output_ref)

            receipt_path_value = receipt_value.get("receipt_path")
            if receipt_path_value != str(receipt_path):
                raise ValueError("active repair handoff receipt_path is invalid")
            return script_ref, frozenset(output_refs)

        declared_script_ref = value.get("calculation_script")
        declared_output_hashes = value.get("output_hashes")
        declared_script_hash = (
            declared_output_hashes.get(declared_script_ref)
            if isinstance(declared_output_hashes, Mapping)
            and isinstance(declared_script_ref, str)
            else None
        )
        validated_handoff_execution_history_refs = set(handoff_execution_refs)
        for receipt_ref in sorted(
            ref for ref in handoff_execution_refs if ref.startswith("work/script_receipts/")
        ):
            receipt_script_ref, _outputs = validate_script_receipt(
                receipt_ref,
                expected_script_hash=declared_script_hash,
            )
            if receipt_script_ref != declared_script_ref:
                raise ValueError("active repair handoff receipt script does not match handoff")
            receipt_id = PurePath(receipt_ref).stem
            for suffix in (".stdout", ".stderr"):
                sibling_ref = f"work/.analysis-run/{receipt_id}{suffix}"
                if sibling_ref not in current_hashes:
                    continue
                sibling_path = self._resolve_item_subpath(sibling_ref)
                _assert_regular_no_symlink(sibling_path, label="active repair handoff receipt output")
                if not sibling_path.is_file():
                    raise ValueError("active repair handoff receipt output is missing")
                validated_handoff_execution_history_refs.add(sibling_ref)

        def handoff_execution_history_matches(path: str) -> bool:
            """Bind a receipt's exact stdout/stderr siblings to its handoff ref."""

            if handoff_ref_matches_current(path):
                return True
            pure = PurePath(path)
            if pure.parent.as_posix() != "work/.analysis-run" or pure.suffix not in {".stdout", ".stderr"}:
                return False
            receipt_ref = f"work/script_receipts/{pure.stem}.json"
            if receipt_ref not in handoff_execution_refs:
                return False
            try:
                receipt_script_ref, _outputs = validate_script_receipt(
                    receipt_ref,
                    expected_script_hash=declared_script_hash,
                )
            except ValueError:
                return False
            return receipt_script_ref == declared_script_ref

        validated_prior_execution_history_refs: set[str] = set()

        def handoff_baseline_matches_current(path: str) -> bool:
            """Allow a declared A5 execution ref whose packet hash is stale.

            A completed attempt snapshots its calculation script/results before
            the public repair boundary.  A handoff may then be written from
            those same bytes while an older packet map still carries a prior
            execution's hashes.  Only the exact typed handoff ref (or its
            validated receipt log sibling) may bridge that common-path drift,
            and only when the latest attempt baseline and current bytes are
            byte-identical.  This is deliberately separate from the prior
            failed-attempt closure: an undeclared or changed path cannot use
            the exception merely because it lives under ``work/results``.
            """

            return (
                path in baseline_hashes
                and path in current_hashes
                and current_hashes.get(path) == baseline_hashes.get(path)
                and handoff_execution_history_matches(path)
            )

        def prior_execution_history_matches(path: str) -> bool:
            """Authorize only unchanged receipt/log families from the prior terminal attempt."""

            if not isinstance(baseline_hashes, Mapping) or path not in baseline_hashes:
                return False
            if path not in current_hashes:
                return False
            if current_hashes.get(path) != baseline_hashes.get(path):
                return False
            pure = PurePath(path)
            if pure.parent.as_posix() == "work/script_receipts" and pure.name.startswith("receipt-") and pure.name.endswith(".json"):
                receipt_ref = path
                receipt_id = pure.stem
            elif pure.parent.as_posix() == "work/.analysis-run" and pure.name.startswith("receipt-") and pure.suffix in {".stdout", ".stderr"}:
                receipt_id = pure.stem
                receipt_ref = f"work/script_receipts/{receipt_id}.json"
            else:
                return False
            sibling_refs = {
                receipt_ref,
                f"work/.analysis-run/{receipt_id}.stdout",
                f"work/.analysis-run/{receipt_id}.stderr",
            }
            if any(
                sibling not in baseline_hashes
                or sibling not in current_hashes
                or current_hashes.get(sibling) != baseline_hashes.get(sibling)
                or (
                    sibling in packet_after_hashes
                    and packet_after_hashes.get(sibling) != current_hashes.get(sibling)
                )
                for sibling in sibling_refs
            ):
                return False
            if not isinstance(attempts, list) or len(attempts) < 2:
                return False
            prior_attempt = attempts[-2]
            if not isinstance(prior_attempt, Mapping):
                return False
            if (
                prior_attempt.get("status") not in {"failed", "completed"}
                or prior_attempt.get("lane_id") != owner_binding["owner_ref"]
                or prior_attempt.get("role") != "Analytical Owner"
                or prior_attempt.get("route") != "requirement"
                or not isinstance(prior_attempt.get("attempt_id"), str)
                or prior_attempt.get("attempt_id") == attempt_id
            ):
                return False
            prior_baseline = prior_attempt.get("baseline")
            prior_hashes = prior_baseline.get("hashes") if isinstance(prior_baseline, Mapping) else None
            if not isinstance(prior_hashes, Mapping) or any(sibling in prior_hashes for sibling in sibling_refs):
                return False
            script_ref = value.get("calculation_script")
            expected_script_hash = (
                baseline_hashes.get(script_ref)
                if isinstance(script_ref, str)
                else None
            )
            try:
                receipt_script_ref, _outputs = validate_script_receipt(
                    receipt_ref,
                    expected_script_hash=expected_script_hash,
                )
            except ValueError:
                return False
            if receipt_script_ref != script_ref:
                return False
            validated_prior_execution_history_refs.update(sibling_refs)
            return True

        packet_before_candidate = packet.get("before_artifact_hashes")
        for mapping in (baseline_hashes, packet_before_candidate, packet_after_hashes):
            if isinstance(mapping, Mapping) and set(mapping) & unsupported_program_paths:
                raise ValueError("active repair handoff program context packet paths are invalid")

        def validate_rebased_program_context(
            baseline_hashes: Mapping[str, str],
            packet_before_hashes: Mapping[str, str],
            packet_after_hashes: Mapping[str, str],
            current_hashes: Mapping[str, str],
        ) -> None:
            """Authenticate only the exact program-context rebase contract."""

            # ``_rebase_active_repair_context_artifacts`` is deliberately
            # narrower than the complete program-context path set: a public
            # repair implementation upgrade changes the manifest and creates
            # (or appends) its owner/audit journal, but it does not create an
            # inheritance transition journal, transition state/intent, or an
            # upgrade intent.  Do not let a forged packet map turn those paths
            # into an authorization channel.
            if current_program_paths != existing_program_paths:
                raise ValueError("active repair handoff program context current paths are invalid")
            if existing_program_paths != repair_upgrade_paths:
                raise ValueError("active repair handoff program context has unauthorized artifacts")

            baseline_program_paths = set(baseline_hashes) & program_paths
            packet_before_program_paths = set(packet_before_hashes) & program_paths
            packet_after_program_paths = set(packet_after_hashes) & program_paths
            if baseline_program_paths - repair_upgrade_paths:
                raise ValueError("active repair handoff program context baseline paths are invalid")
            if packet_before_program_paths - repair_upgrade_paths:
                raise ValueError("active repair handoff program context before paths are invalid")
            if packet_after_program_paths - repair_upgrade_paths:
                raise ValueError("active repair handoff program context after paths are invalid")
            if packet_after_program_paths != repair_upgrade_paths:
                raise ValueError("active repair handoff program context rebase paths are incomplete")
            if packet_before_program_paths != repair_upgrade_paths:
                raise ValueError("active repair handoff program context before paths are incomplete")
            if baseline_program_paths - packet_after_program_paths:
                raise ValueError("active repair handoff program context rebase removed artifacts")
            added_program_paths = packet_after_program_paths - baseline_program_paths
            changed_program_paths = {
                path
                for path in baseline_program_paths & packet_after_program_paths
                if baseline_hashes[path] != packet_after_hashes[path]
            }
            if "work/analysis_context.json" not in changed_program_paths:
                raise ValueError("active repair handoff program context manifest was not upgraded")
            if not (
                {"work/analysis_context_repair_upgrades.jsonl"}.issubset(
                    added_program_paths | changed_program_paths
                )
            ):
                raise ValueError("active repair handoff program context upgrade audit was not published")

            nonprogram_added_paths = (set(packet_after_hashes) - set(packet_before_hashes)) - program_paths
            if any(
                not (
                    handoff_execution_history_matches(str(path))
                    or prior_execution_history_matches(str(path))
                    or (
                        not is_execution_history_path(str(path))
                        and packet_scope_allows(str(path))
                    )
                )
                or not packet_after_matches_current(str(path))
                for path in nonprogram_added_paths
            ):
                raise ValueError("active repair handoff program context rebase is invalid")
            if (set(current_hashes) - set(packet_after_hashes)) & program_paths:
                raise ValueError("active repair handoff program context rebase is stale")
            for path in program_paths:
                before = packet_before_hashes.get(path)
                after = packet_after_hashes.get(path)
                current = current_hashes.get(path)
                if after is None:
                    if before is not None or current is not None:
                        raise ValueError("active repair handoff program context rebase is incomplete")
                    continue
                if before is not None and before != after:
                    raise ValueError("active repair handoff program context rebase baseline is stale")
                if current != after:
                    raise ValueError("active repair handoff program context rebase hash is stale")
            intent_paths = {
                path for path in program_paths if path.endswith("_intent.json")
            }
            for path in intent_paths:
                intent = self._resolve_item_subpath(path)
                if intent.exists() or intent.is_symlink():
                    raise ValueError("active repair handoff program context rebase has intent residue")
            manifest_ref = "work/analysis_context.json"
            manifest = self._resolve_item_subpath(manifest_ref)
            _assert_regular_no_symlink(manifest, label="analysis context manifest")
            if not manifest.is_file() or packet_after_hashes.get(manifest_ref) != current_hashes.get(manifest_ref):
                raise ValueError("active repair handoff program context manifest is stale")
            try:
                manifest_bytes = manifest.read_bytes()
                manifest_value = json.loads(manifest_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("active repair handoff program context manifest is invalid") from exc
            if not isinstance(manifest_value, Mapping):
                raise ValueError("active repair handoff program context manifest is not canonical")
            unsigned_manifest = dict(manifest_value)
            manifest_hash = unsigned_manifest.pop("manifest_hash", None)
            try:
                from .analysis import (
                    _json_bytes as _analysis_json_bytes,
                    _manifest_bytes,
                    _validate_implementation_identity,
                    _validate_repair_upgrade_manifest_binding,
                )
            except ImportError as exc:
                raise ValueError("active repair handoff program context validator is unavailable") from exc
            if manifest_hash != _sha256_bytes(_analysis_json_bytes(unsigned_manifest)):
                raise ValueError("active repair handoff program context manifest hash is stale")
            try:
                if manifest_bytes != _manifest_bytes(manifest_value):
                    raise ValueError("manifest is not canonical")
                records = _validate_repair_upgrade_manifest_binding(
                    manifest,
                    manifest_value,
                    run_id=self.context.run_id,
                    item_id=self.item_id,
                )
                if not records:
                    raise ValueError("manifest does not bind a repair-upgrade audit")
            except ValueError as exc:
                raise ValueError("active repair handoff program context audit is invalid") from exc

            # The analysis module owns the implementation identity algorithm.
            # Keep this repair-boundary exception fail-closed when the source
            # has changed again: a packet/audit pair must be bound to the
            # implementation currently executing this validator, not merely
            # to a self-consistent identity chosen by its writer.
            for field_name, expected in (
                ("run_id", self.context.run_id),
                ("run_root", str(self.context.run_root)),
                ("item_id", self.item_id),
                ("item_mode", self.mode),
                ("core_version", self.context.core_version),
                ("skill_version", self.context.skill_version),
            ):
                if manifest_value.get(field_name) != expected:
                    raise ValueError(
                        "active repair handoff program context manifest identity is not current"
                    )
            try:
                current_pair = _validate_implementation_identity(
                    self.context,
                    manifest_value,
                    require_current=True,
                )
            except ValueError as exc:
                raise ValueError(
                    "active repair handoff program context manifest identity is not current"
                ) from exc

            audit_ref = "work/analysis_context_repair_upgrades.jsonl"
            audit_path = self._resolve_item_subpath(audit_ref)
            _assert_regular_no_symlink(audit_path, label="analysis context repair-upgrade audit")
            if not audit_path.is_file():
                raise ValueError("active repair handoff program context audit is missing")
            try:
                audit_bytes = audit_path.read_bytes()
            except OSError as exc:
                raise ValueError("active repair handoff program context audit is unreadable") from exc
            expected_audit_bytes = b"".join(
                _analysis_json_bytes(record) + b"\n" for record in records
            )
            if audit_bytes != expected_audit_bytes:
                raise ValueError("active repair handoff program context audit is not canonical")

            # ``_validate_repair_upgrade_manifest_binding`` authenticates each
            # row and the manifest's count/head pointer.  It intentionally
            # cannot know the business attempt's immutable baseline or owner,
            # so bind those facts here before allowing the program-context
            # rebase to cross the repair boundary.
            for record in records:
                if (
                    record.get("run_id") != self.context.run_id
                    or record.get("item_id") != self.item_id
                    or record.get("owner_ref") != owner_binding["owner_ref"]
                ):
                    raise ValueError(
                        "active repair handoff program context audit owner or item binding is invalid"
                    )
            latest = records[-1]
            if (
                latest.get("run_id") != self.context.run_id
                or latest.get("item_id") != self.item_id
                or latest.get("owner_ref") != owner_binding["owner_ref"]
                or manifest_value.get("item_mode") != self.mode
                or (latest.get("new_sha"), latest.get("new_tree")) != current_pair
            ):
                raise ValueError(
                    "active repair handoff program context audit latest binding is invalid"
                )

            baseline_manifest_hash = baseline_hashes.get(manifest_ref)
            if not _is_sha256(baseline_manifest_hash):
                raise ValueError(
                    "active repair handoff program context baseline manifest anchor is invalid"
                )
            baseline_audit_hash = baseline_hashes.get(audit_ref)
            if baseline_audit_hash is not None and not _is_sha256(baseline_audit_hash):
                raise ValueError(
                    "active repair handoff program context baseline audit anchor is invalid"
                )

            # Locate the immutable attempt's audit prefix by its byte hash.  A
            # missing baseline journal means A-003 began before the first
            # public implementation upgrade; an existing journal may be a
            # prefix from an earlier completed upgrade and is still part of
            # the chain root, never a free-form writer capability.
            audit_prefix_count: int | None = None
            if baseline_audit_hash is None:
                audit_prefix_count = 0
            else:
                for count in range(len(records) + 1):
                    prefix = b"".join(
                        _analysis_json_bytes(record) + b"\n"
                        for record in records[:count]
                    )
                    if _sha256_bytes(prefix) == baseline_audit_hash:
                        audit_prefix_count = count
                        break
                if audit_prefix_count is None:
                    raise ValueError(
                        "active repair handoff program context audit prefix is not rooted"
                    )
            if audit_prefix_count >= len(records):
                raise ValueError(
                    "active repair handoff program context audit prefix has no current upgrade"
                )

            def manifest_for_state(
                implementation_sha: str,
                implementation_tree: str,
                audit_count: int,
                audit_head: str | None,
            ) -> tuple[dict[str, Any], bytes]:
                candidate_unsigned = dict(manifest_value)
                candidate_unsigned.pop("manifest_hash", None)
                candidate_unsigned["implementation_sha"] = implementation_sha
                candidate_unsigned["implementation_tree"] = implementation_tree
                if audit_count:
                    if audit_head is None:
                        raise ValueError(
                            "active repair handoff program context audit binding is incomplete"
                        )
                    candidate_unsigned["active_repair_implementation_upgrade"] = {
                        "path": audit_ref,
                        "audit_count": audit_count,
                        "audit_head": audit_head,
                    }
                else:
                    candidate_unsigned.pop("active_repair_implementation_upgrade", None)
                candidate_manifest_hash = _sha256_bytes(_analysis_json_bytes(candidate_unsigned))
                candidate_manifest = {
                    **candidate_unsigned,
                    "manifest_hash": candidate_manifest_hash,
                }
                return candidate_manifest, _manifest_bytes(candidate_manifest)

            # Reconstruct every manifest transition from the immutable static
            # context fields and the append-only audit rows.  This proves the
            # first appended row starts at the attempt baseline, each row's
            # old identity is exactly the previous new identity, and every
            # before-manifest hash is the canonical bytes published by the
            # preceding state.  The final reconstructed bytes must be the
            # manifest currently on disk, so a self-consistent forged audit
            # plus rewritten manifest cannot authorize the rebase.
            prior_manifest: dict[str, Any] | None = None
            prior_manifest_bytes: bytes | None = None
            for index, record in enumerate(records):
                if index == 0:
                    prior_manifest, prior_manifest_bytes = manifest_for_state(
                        record["old_sha"],
                        record["old_tree"],
                        0,
                        None,
                    )
                else:
                    previous = records[index - 1]
                    if (
                        record["old_sha"] != previous["new_sha"]
                        or record["old_tree"] != previous["new_tree"]
                    ):
                        raise ValueError(
                            "active repair handoff program context audit implementation chain is broken"
                        )
                    prior_manifest, prior_manifest_bytes = manifest_for_state(
                        previous["new_sha"],
                        previous["new_tree"],
                        index,
                        previous["record_hash"],
                    )
                if prior_manifest_bytes is None or _sha256_bytes(prior_manifest_bytes) != record["before_manifest_hash"]:
                    raise ValueError(
                        "active repair handoff program context audit manifest chain is broken"
                    )
                if index == audit_prefix_count and record["before_manifest_hash"] != baseline_manifest_hash:
                    raise ValueError(
                        "active repair handoff program context audit baseline root is invalid"
                    )
                _next_manifest, next_manifest_bytes = manifest_for_state(
                    record["new_sha"],
                    record["new_tree"],
                    index + 1,
                    record["record_hash"],
                )
                if index + 1 == len(records):
                    if next_manifest_bytes != manifest_bytes:
                        raise ValueError(
                            "active repair handoff program context audit manifest continuity is invalid"
                        )
                elif _sha256_bytes(next_manifest_bytes) != records[index + 1]["before_manifest_hash"]:
                    raise ValueError(
                        "active repair handoff program context audit manifest continuity is invalid"
                    )

        if (
            not isinstance(baseline, Mapping)
            or not isinstance(baseline_hashes, Mapping)
            or not isinstance(packet_after_hashes, Mapping)
        ):
            raise ValueError("active repair handoff attempt is not bound to the repair baseline")

        current_only_paths = set(current_hashes) - (set(baseline_hashes) | set(packet_after_hashes))
        if any(
            handoff_scope_allows_execution
            and
            is_execution_history_path(str(path))
            and not handoff_execution_history_matches(str(path))
            for path in current_only_paths
        ):
            raise ValueError("active repair handoff execution history is outside its bound family")

        baseline_only_paths = set(baseline_hashes) - set(packet_after_hashes)
        after_only_paths = set(packet_after_hashes) - set(baseline_hashes)
        common_changed_paths = {
            path
            for path in set(baseline_hashes) & set(packet_after_hashes)
            if path != _HANDOFF_ARTIFACT_PATH
            and baseline_hashes[path] != packet_after_hashes[path]
        }
        # An exact retry may have reconciled the prior family into
        # ``after_artifact_hashes`` on the first public replacement.  Re-run
        # the same prior-attempt provenance check for unchanged common receipt
        # paths so the outer scope receives that exact family again; no path
        # absent from the immutable current-attempt baseline can enter here.
        for path in set(baseline_hashes) & set(packet_after_hashes):
            if is_execution_history_path(str(path)):
                prior_execution_history_matches(str(path))
        if (
            baseline_only_paths & program_paths
            or any(
                not baseline_omission_matches_current(str(path))
                for path in baseline_only_paths
            )
            or any(
                path not in program_paths
                and (
                    not (
                        handoff_execution_history_matches(str(path))
                        or (
                            not is_execution_history_path(str(path))
                            and packet_scope_allows(str(path))
                        )
                    )
                    or not packet_after_matches_current(str(path))
                )
                for path in after_only_paths
            )
            or any(
                path not in program_paths
                and (
                    not (
                        handoff_baseline_matches_current(str(path))
                        or (
                            (
                                handoff_execution_history_matches(str(path))
                                or (
                                    not is_execution_history_path(str(path))
                                    and packet_scope_allows(str(path))
                                )
                            )
                            and packet_after_matches_current(str(path))
                        )
                    )
                )
                for path in common_changed_paths
            )
        ):
            raise ValueError("active repair handoff attempt is not bound to the repair baseline")
        if isinstance(baseline_hashes, Mapping) and isinstance(packet_after_hashes, Mapping):
            packet_before_hashes = packet.get("before_artifact_hashes")
            packet_program_rebased = (
                isinstance(packet_before_hashes, Mapping)
                and (
                    bool((set(packet_after_hashes) - set(packet_before_hashes)) & program_paths)
                    or any(
                        packet_before_hashes[path] != packet_after_hashes[path]
                        for path in set(packet_before_hashes) & set(packet_after_hashes) & program_paths
                    )
                )
            )
            baseline_program_rebased = (
                bool((set(packet_after_hashes) - set(baseline_hashes)) & program_paths)
                or any(
                    baseline_hashes[path] != packet_after_hashes[path]
                    for path in set(baseline_hashes) & set(packet_after_hashes) & program_paths
                )
            )
            if packet_program_rebased or baseline_program_rebased:
                if not isinstance(packet_before_hashes, Mapping):
                    raise ValueError("active repair handoff program context baseline is invalid")
                validate_rebased_program_context(
                    baseline_hashes,
                    packet_before_hashes,
                    packet_after_hashes,
                    current_hashes,
                )
        return frozenset(
            validated_handoff_execution_history_refs | validated_prior_execution_history_refs
        )

    def _repair_scope_check(
        self,
        *,
        candidate_payload: bytes | None = None,
        artifact_path: str | None = None,
        _packet: Mapping[str, Any] | None = None,
        _ignore_artifact_paths: Iterable[str] = (),
    ) -> dict[str, Any] | None:
        """Validate the *item boundary* of an active business repair.

        Review findings retain their semantic categories for reviewer evidence
        and targeted re-checks.  They are intentionally not translated into a
        filesystem capability list here.  Once the same owner has consumed a
        bounded repair authorization, every answer section and every artifact
        below this item's ``work/`` directory is writable.  The lexical item
        path checks in :meth:`_resolve_item_subpath` remain the authority that
        prevents a write from escaping the item/run root.

        ``_packet`` and ``_ignore_artifact_paths`` are retained for the atomic
        packet-reconciliation caller.  They affect only which current packet
        is read and which self-generated progress paths are ignored; neither
        widens nor narrows the item-local repair boundary.
        """

        # ``_packet`` is reserved for the atomic active-repair scope upgrade.
        # It lets the same current-state checks run against a proposed packet
        # without re-reading or mutating the persisted baseline packet.
        packet = self._read_business_review() if _packet is None else copy.deepcopy(dict(_packet))
        if packet is None or not packet.get("repair_active"):
            return packet

        before_value = packet["before_snapshot"]
        current_value, _current_hash = self._current_draft_value_and_hash()
        candidate_value = current_value if candidate_payload is None else self._canonical_draft_value(candidate_payload)
        changed_current = _pointer_diff(before_value, current_value)
        changed_candidate = _pointer_diff(before_value, candidate_value)

        def answer_pointer(pointer: str) -> bool:
            # A whole-envelope replacement is only accepted when both values
            # remain structured answer envelopes.  This keeps the immutable
            # item/schema binding out of an owner repair without prescribing a
            # category-derived subset of answer sections.
            if pointer == "":
                return isinstance(before_value, Mapping) and isinstance(candidate_value, Mapping)
            if not pointer.startswith("/"):
                return False
            first = pointer[1:].split("/", 1)[0].replace("~1", "/").replace("~0", "~")
            return first in _ANSWER_DRAFT_SECTIONS and first not in _DRAFT_IMMUTABLE_FIELDS

        for pointer in (*changed_current, *changed_candidate):
            if not answer_pointer(pointer):
                raise ValueError(f"business repair changed answer outside reviewed scope: {pointer}")

        ignored_artifacts = frozenset(str(path).replace("\\", "/") for path in _ignore_artifact_paths)

        def item_work_path(raw_path: str) -> str:
            normalized = str(raw_path).replace("\\", "/")
            pure = PurePath(normalized)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise AllowedRootError("business repair artifact must remain under item work/")
            normalized = pure.as_posix()
            if not normalized.startswith("work/") or normalized == "work/":
                raise AllowedRootError("business repair artifact must remain under item work/")
            return normalized

        if artifact_path is not None:
            normalized = str(artifact_path).replace("\\", "/")
            if normalized not in {_DRAFT_FILENAME, "work/" + _DRAFT_FILENAME}:
                normalized = item_work_path(normalized)
                if normalized not in ignored_artifacts:
                    # Resolving the path is deliberately enough here: the
                    # caller's write method performs the regular-file and
                    # lexical containment checks before touching bytes.
                    pass

        before_artifacts = dict(packet.get("before_artifact_hashes", {}))
        current_progress = self._artifact_progress(candidate_payload if candidate_payload is not None else None)
        for path, digest in current_progress.hashes.items():
            if path in ignored_artifacts or path == _DRAFT_FILENAME:
                continue
            normalized = item_work_path(path)
            if before_artifacts.get(normalized) != digest:
                # Any changed work artifact is in scope for this item-local
                # repair.  No finding category is consulted.
                continue
        for path in set(before_artifacts) - set(current_progress.hashes):
            if path in ignored_artifacts or path == _DRAFT_FILENAME:
                continue
            item_work_path(path)
        return packet

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
        if self._state.get("lifecycle_state") in {"accepted", "technical_failure", _BLOCKED_REVIEW_OUTCOME}:
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

    def _reload_authoritative_state_locked(self) -> None:
        """Reload and validate item state without recovery-side effects."""

        state_path = self._resolve_item_subpath(_STATE_FILENAME)
        authoritative_state = self._read_state(state_path)
        self._validate_state(
            authoritative_state,
            item_id=self.item_id,
            mode=self.mode,
            original_text=self.original_text,
        )
        self._state = authoritative_state

    def _reload_authoritative_for_artifact_mutation_locked(self) -> None:
        """Reload item state before a work-artifact mutation under its lock.

        Terminal publication and artifact writes share the same transition
        lock.  A stale workspace must therefore re-check the authoritative
        lifecycle before touching bytes; otherwise it could write a draft
        while a finalizer is binding the prior draft into an immutable
        terminal snapshot.
        """

        self._reload_authoritative_state_locked()
        self._ensure_execution_state()
        self._reconcile_business_review_discard()

    def _reload_authoritative_for_terminal_transition_locked(self) -> None:
        """Reload and reconcile every terminal precondition under one lock."""

        self._reload_authoritative_for_artifact_mutation_locked()
        self._validate_recovery_authorizations()
        self._reconcile_terminal_snapshot()

    @staticmethod
    def _restore_artifact_bytes(path: Path, existed: bool, payload: bytes | None) -> None:
        """Restore one artifact after a paired state-write failure."""

        if existed:
            if payload is None:
                raise RuntimeError("artifact rollback payload is missing")
            _atomic_write_bytes(path, payload)
        elif path.exists() or path.is_symlink():
            path.unlink()
            _fsync_directory(path.parent)

    def _write_json_artifact(self, relative: str, value: Any, *, event_type: str = "item_workspace_write") -> Path:
        relative_path = Path(relative).as_posix()
        payload = _json_bytes(value)
        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            self._ensure_not_terminal()
            destination = self._resolve_item_subpath(relative_path)
            _assert_regular_no_symlink(destination, label="item artifact")
            if relative_path == _DRAFT_FILENAME:
                self._repair_scope_check(candidate_payload=payload, artifact_path=_DRAFT_FILENAME)
            else:
                self._repair_scope_check(artifact_path=relative_path)
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_state = copy.deepcopy(self._state)
            prior_destination_exists = destination.exists() or destination.is_symlink()
            prior_destination_bytes = destination.read_bytes() if prior_destination_exists else None
            try:
                _atomic_write_bytes(destination, payload)
                state = copy.deepcopy(self._state)
                if relative_path == _DRAFT_FILENAME:
                    state["review"] = self._pending_review()
                    state["lifecycle_state"] = "work"
                state["updated_at"] = _now()
                self._persist_state_unlocked(state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    self._restore_artifact_bytes(destination, prior_destination_exists, prior_destination_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("item artifact mutation rollback failed") from exc
                raise
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

    @property
    def integration_state(self) -> str:
        self._ensure_execution_state()
        return str(self._state["integration_state"])

    @property
    def integration_manifest_hash(self) -> str | None:
        self._ensure_execution_state()
        value = self._state.get("integration_manifest_hash")
        return str(value) if value is not None else None

    @property
    def integration_manifest_ref(self) -> str | None:
        self._ensure_execution_state()
        value = self._state.get("integration_manifest_ref")
        return str(value) if value is not None else None

    def write_plan(self, mapping: Mapping[str, Any]) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError("plan must be a mapping")
        self._write_json_artifact(Path("work") / _PLAN_FILENAME, mapping)

    def append_source_map(self, mapping: Mapping[str, Any]) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError("source map entry must be a mapping")
        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            self._ensure_not_terminal()
            destination = self._resolve_item_subpath(Path("work") / _SOURCE_MAP_FILENAME)
            _assert_regular_no_symlink(destination, label="source map artifact")
            self._repair_scope_check(artifact_path="work/source_map.json")
            prior_state = copy.deepcopy(self._state)
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_destination_exists = destination.exists() or destination.is_symlink()
            prior_destination_bytes = destination.read_bytes() if prior_destination_exists else None
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
            try:
                _atomic_write_json(destination, entries)
                state = copy.deepcopy(self._state)
                state["updated_at"] = _now()
                self._persist_state_unlocked(state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    self._restore_artifact_bytes(destination, prior_destination_exists, prior_destination_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("source map mutation rollback failed") from exc
                raise
        self._emit("item_workspace_append", artifact="work/source_map.json")

    @staticmethod
    def _validate_source_map_rows(
        rows: Iterable[Mapping[str, Any]],
        *,
        unique: bool,
    ) -> tuple[dict[str, Any], ...]:
        """Validate the canonical shape used by ``select_sources``.

        The append API intentionally remains permissive for historical work
        records.  Replacement is stricter: callers provide complete,
        canonical source-selection rows and duplicate identities are rejected.
        Existing rows are shape-checked but may contain the duplicate identity
        that a replacement is explicitly repairing.
        """

        if isinstance(rows, (str, bytes)):
            raise TypeError("source map rows must be an iterable of mappings")
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, value in enumerate(rows):
            if not isinstance(value, Mapping):
                raise TypeError(f"source_map[{index}] must be a mapping")
            row = dict(value)
            if set(row) != _SOURCE_MAP_FIELDS:
                raise ValueError(f"source_map[{index}] fields are not canonical")
            if row.get("record_kind") != "analyst_source_selection":
                raise ValueError(f"source_map[{index}] record_kind is invalid")
            for field_name in ("source_id", "purpose", "path"):
                field_value = row.get(field_name)
                if (
                    not isinstance(field_value, str)
                    or not field_value.strip()
                    or field_value != field_value.strip()
                ):
                    raise ValueError(f"source_map[{index}] {field_name} must be non-empty text")
            if not _is_sha256(row.get("content_hash")):
                raise ValueError(f"source_map[{index}] content_hash must be a SHA-256 digest")
            columns = row.get("columns")
            if not isinstance(columns, list) or any(
                not isinstance(column, str) or not column.strip() or column != column.strip()
                for column in columns
            ):
                raise ValueError(f"source_map[{index}] columns must be a canonical string array")
            if len(columns) != len(set(columns)):
                raise ValueError(f"source_map[{index}] columns must not contain duplicates")
            row_count = row.get("row_count")
            if row_count is not None and (isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0):
                raise ValueError(f"source_map[{index}] row_count must be a non-negative integer or null")
            if not isinstance(row.get("row_count_exact"), bool):
                raise ValueError(f"source_map[{index}] row_count_exact must be boolean")
            source_id = row["source_id"]
            if unique and source_id in seen:
                raise ValueError("source map source_id values must be unique")
            seen.add(source_id)
            # Enforce JSON-safe, canonical value types before the list is
            # serialized.  _json_bytes also rejects NaN/Infinity.
            try:
                canonical = json.loads(_json_bytes(row).decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"source_map[{index}] is not canonical JSON") from exc
            if not isinstance(canonical, dict) or canonical != row:
                raise ValueError(f"source_map[{index}] is not canonical")
            validated.append(canonical)
        return tuple(validated)

    def replace_source_map(
        self,
        mappings: Iterable[Mapping[str, Any]],
        *,
        owner_ref: Any,
        expected_artifact_hash: str,
    ) -> tuple[dict[str, Any], ...]:
        """Atomically replace the complete source-selection map using CAS.

        ``expected_artifact_hash`` is required and is compared with the raw
        current ``work/source_map.json`` bytes under the item lock.  The
        current artifact must also be canonical JSON; this prevents a caller
        from silently normalizing an unrelated or tampered source map.  New
        rows are strict, unique, owner-scoped source-selection records.  An
        exact retry returns without rewriting bytes or lifecycle state.
        """

        if isinstance(mappings, (str, bytes)):
            raise TypeError("source map rows must be an iterable of mappings")
        try:
            supplied = tuple(mappings)
        except TypeError as exc:
            raise TypeError("source map rows must be an iterable of mappings") from exc
        desired_rows = self._validate_source_map_rows(supplied, unique=True)
        owner = _owner_ref_value(owner_ref)
        if not _is_sha256(expected_artifact_hash):
            raise ValueError("expected_artifact_hash must be a SHA-256 digest")

        relative = (Path("work") / _SOURCE_MAP_FILENAME).as_posix()
        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            self._ensure_not_terminal()
            # Replacement is an owner-scoped CAS operation.  Unlike the
            # initial owner-binding APIs, it must never silently rebind an
            # existing workspace to a different caller.
            existing_owner = self._read_analysis_owner()
            if existing_owner is None:
                raise ValueError("item has no bound Analytical Owner")
            if existing_owner["owner_ref"] != owner:
                raise ValueError("owner_ref does not match the bound Analytical Owner")
            self._verify_analysis_owner_locked(owner, bind_if_missing=False)

            destination = self._resolve_item_subpath(relative)
            _assert_regular_no_symlink(destination, label="source map artifact")
            if destination.exists() and not destination.is_file():
                raise ValueError("source map artifact must be a regular file")
            current_bytes = destination.read_bytes() if destination.exists() else b""
            current_hash = _sha256_bytes(current_bytes)
            if expected_artifact_hash != current_hash:
                raise ValueError("expected_artifact_hash does not match the current source map artifact")
            if destination.exists():
                try:
                    current_value = json.loads(current_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("source map artifact is not valid JSON") from exc
                if not isinstance(current_value, list):
                    raise ValueError("source map artifact must contain a JSON array")
                current_rows = self._validate_source_map_rows(current_value, unique=False)
                if _json_bytes(list(current_rows)) != current_bytes:
                    raise ValueError("source map artifact is not canonical JSON")
            elif current_bytes:
                raise ValueError("source map artifact is not a regular file")

            desired_bytes = _json_bytes(list(desired_rows))
            # Exact retries are read-only, including state and mtime.
            if desired_bytes == current_bytes:
                return tuple(copy.deepcopy(dict(row)) for row in desired_rows)

            self._repair_scope_check(artifact_path=relative)
            prior_state = copy.deepcopy(self._state)
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_destination_exists = destination.exists() or destination.is_symlink()
            prior_destination_bytes = destination.read_bytes() if prior_destination_exists else None
            try:
                _atomic_write_bytes(destination, desired_bytes)
                next_state = copy.deepcopy(self._state)
                next_state["updated_at"] = _now()
                self._persist_state_unlocked(next_state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    self._restore_artifact_bytes(destination, prior_destination_exists, prior_destination_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("source map replacement rollback failed") from exc
                raise
        self._emit(
            "item_workspace_replace_source_map",
            artifact=relative,
            source_count=len(desired_rows),
            artifact_hash=_sha256_bytes(desired_bytes),
        )
        return tuple(copy.deepcopy(dict(row)) for row in desired_rows)

    def _append_work_record(
        self,
        filename: str,
        mapping: Mapping[str, Any],
        *,
        label: str,
        dedupe_field: str | tuple[str, ...] | None = None,
        dedupe_ignored_fields: tuple[str, ...] = (),
        owner_ref: str | None = None,
    ) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError(f"{label} must be a mapping")
        relative = (Path("work") / filename).as_posix()
        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            self._ensure_not_terminal()
            if owner_ref is not None:
                self._verify_analysis_owner_locked(owner_ref)
            destination = self._resolve_item_subpath(relative)
            _assert_regular_no_symlink(destination, label=f"{label} artifact")
            self._repair_scope_check(artifact_path=relative)
            if dedupe_field is not None:
                dedupe_fields = (dedupe_field,) if isinstance(dedupe_field, str) else tuple(dedupe_field)
                dedupe_values = {
                    field: mapping.get(field)
                    for field in dedupe_fields
                    if mapping.get(field) is not None
                }
                if not dedupe_values:
                    raise ValueError(f"{label} dedupe field is missing: {', '.join(dedupe_fields)}")
                if destination.exists():
                    try:
                        prior_records = [
                            json.loads(line)
                            for line in destination.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        ]
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(f"{label} artifact is invalid") from exc
                    for prior in prior_records:
                        if not isinstance(prior, Mapping):
                            raise ValueError(f"{label} artifact is invalid")
                        matched_field = next(
                            (
                                field
                                for field, value in dedupe_values.items()
                                if prior.get(field) == value
                            ),
                            None,
                        )
                        if matched_field is not None:
                            comparable_prior = {
                                key: value
                                for key, value in prior.items()
                                if key not in dedupe_ignored_fields
                            }
                            comparable_mapping = {
                                key: value
                                for key, value in dict(_jsonable(mapping)).items()
                                if key not in dedupe_ignored_fields
                            }
                            if comparable_prior == comparable_mapping:
                                return
                            raise ValueError(
                                f"{label} dedupe identity conflicts: {matched_field}={dedupe_values[matched_field]}"
                            )
            prior_state = copy.deepcopy(self._state)
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_destination_exists = destination.exists() or destination.is_symlink()
            prior_destination_bytes = destination.read_bytes() if prior_destination_exists else None
            try:
                _append_jsonl(destination, mapping)
                state = copy.deepcopy(self._state)
                state["updated_at"] = _now()
                self._persist_state_unlocked(state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    self._restore_artifact_bytes(destination, prior_destination_exists, prior_destination_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("work-record mutation rollback failed") from exc
                raise
        self._emit("item_workspace_append", artifact=relative)

    def append_finding(self, mapping: Mapping[str, Any]) -> None:
        self._append_work_record(_FINDINGS_FILENAME, mapping, label="finding")

    def append_evidence(self, mapping: Mapping[str, Any]) -> None:
        """Append one program-normalized analytical evidence record."""

        self._append_work_record(_EVIDENCE_FILENAME, mapping, label="evidence record")

    def _validate_evidence_replacement_scope(self, packet: Mapping[str, Any]) -> None:
        """Require reviewer scope that explicitly authorizes evidence replacement.

        A broad business repair may carry calculation, method, presentation,
        and answer findings alongside its evidence finding.  The evidence
        replacement boundary therefore binds to the evidence finding's
        semantic category and pointer, plus the packet's explicit evidence
        dependency, without treating unrelated findings as evidence authority.
        """

        findings = packet.get("findings")
        if not isinstance(findings, list) or not findings:
            raise ValueError("evidence replacement requires an evidence repair finding")
        evidence_path = "work/evidence.jsonl"
        evidence_findings = []
        for finding in findings:
            if not isinstance(finding, Mapping):
                raise ValueError("evidence replacement finding is invalid")
            categories = tuple(finding.get("semantic_categories", ()))
            if "evidence" not in categories:
                continue
            pointers = frozenset(str(value) for value in finding.get("pointers", ()))
            dependencies = frozenset(str(value) for value in finding.get("dependent_outputs", ()))
            if "/evidence_refs" not in pointers or evidence_path not in dependencies:
                raise ValueError("evidence replacement finding does not authorize evidence refs")
            evidence_findings.append(finding)
        if not evidence_findings:
            raise ValueError("evidence replacement requires an evidence finding")
        allowed_pointers = frozenset(str(value) for value in packet.get("allowed_pointers", ()))
        if "/evidence_refs" not in allowed_pointers:
            raise ValueError("evidence replacement allowed pointers are invalid")
        allowed_paths = frozenset(
            str(value)
            for name in ("allowed_artifact_paths", "allowed_dependencies")
            for value in packet.get(name, ())
        )
        if evidence_path not in allowed_paths:
            raise ValueError("evidence replacement evidence path is not authorized")

    def _validate_evidence_note_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        current_hashes: Mapping[str, str],
        existing_external_refs: frozenset[str] = frozenset(),
        allow_unknown_external_refs: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """Validate canonical typed evidence rows and item-local references."""

        # Import lazily to keep the durable module's import graph acyclic.
        from .analyst_workspace import EvidenceNote

        required_fields = frozenset(
            {
                "record_kind",
                "evidence_id",
                "conclusion",
                "method",
                "evidence_refs",
                "limitations",
                "facts",
            }
        )
        validated: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        def validate_ref(raw_ref: Any) -> str:
            if (
                not isinstance(raw_ref, str)
                or not raw_ref
                or raw_ref != raw_ref.strip()
                or "\\" in raw_ref
                or "\x00" in raw_ref
                or "\n" in raw_ref
                or "\r" in raw_ref
            ):
                raise ValueError("evidence reference is invalid")
            base, separator, fragment = raw_ref.partition("#")
            pure = PurePath(base)
            if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != base or not base:
                raise ValueError("evidence reference path is invalid")
            if separator and not fragment:
                raise ValueError("evidence reference fragment is invalid")
            if base.startswith("work/"):
                if base in _ANALYST_HANDOFF_RESERVED_REFS:
                    raise ValueError("evidence reference path is reserved")
                path = self._resolve_item_subpath(base)
                _assert_regular_no_symlink(path, label="evidence reference")
                if not path.is_file() or base not in current_hashes:
                    raise ValueError("evidence reference is outside current artifact progress")
            elif not allow_unknown_external_refs and raw_ref not in existing_external_refs:
                # Source/catalog references are not item files and have no
                # durable hash at this boundary.  A replacement may retain a
                # previously recorded source reference, but cannot introduce a
                # new opaque ref that the durable core cannot authenticate.
                raise ValueError("evidence reference is not current item evidence")
            return raw_ref

        for value in rows:
            if not isinstance(value, Mapping):
                raise TypeError("evidence note must be a mapping")
            if set(value) != required_fields:
                raise ValueError("evidence note row fields are invalid")
            if value.get("record_kind") != "analytical_evidence":
                raise ValueError("evidence note record_kind is invalid")
            raw = dict(value)
            raw.pop("record_kind", None)
            try:
                typed = EvidenceNote(**raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("evidence note row is invalid") from exc
            canonical = typed.to_dict()
            if dict(value) != canonical:
                raise ValueError("evidence note row is not canonical")
            if typed.evidence_id in seen_ids:
                raise ValueError("evidence_id values must be unique")
            seen_ids.add(typed.evidence_id)
            for ref in typed.evidence_refs:
                validate_ref(ref)
            validated.append(canonical)
        return tuple(validated)

    def replace_evidence_notes(
        self,
        mappings: Iterable[Mapping[str, Any]],
        *,
        owner_ref: Any,
        expected_artifact_hash: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Atomically replace the current evidence view.

        Evidence is an editable analytical artifact, not a repair-only
        privilege.  The current owner is recorded for audit, while attempts,
        review packets and implementation versions do not gate the write.
        ``expected_artifact_hash`` remains an optional caller-side concurrency
        check and exact retries remain byte/mtime stable.
        """

        if isinstance(mappings, (str, bytes)):
            raise TypeError("evidence notes must be an iterable of mappings")
        try:
            supplied = tuple(mappings)
        except TypeError as exc:
            raise TypeError("evidence notes must be an iterable of mappings") from exc
        if any(not isinstance(value, Mapping) for value in supplied):
            raise TypeError("evidence notes must contain mappings")
        owner = _owner_ref_value(owner_ref)
        if expected_artifact_hash is not None and not _is_sha256(expected_artifact_hash):
            raise ValueError("expected_artifact_hash must be a SHA-256 digest")

        relative = (Path("work") / _EVIDENCE_FILENAME).as_posix()
        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            self._ensure_not_terminal()
            self._verify_analysis_owner_locked(owner, bind_if_missing=True)

            destination = self._resolve_item_subpath(relative)
            _assert_regular_no_symlink(destination, label="evidence artifact")
            if destination.exists() and not destination.is_file():
                raise ValueError("evidence artifact must be a regular file")
            current_bytes = destination.read_bytes() if destination.exists() else b""
            current_hash = _sha256_bytes(current_bytes) if destination.exists() else None
            if expected_artifact_hash is not None and expected_artifact_hash != current_hash:
                raise ValueError("expected_artifact_hash does not match the current evidence artifact")

            current_progress = self._artifact_progress()
            current_hashes = dict(current_progress.hashes)
            current_records = self._read_work_records(_EVIDENCE_FILENAME, label="evidence")
            canonical_current = b"".join(_json_bytes(row) for row in current_records)
            if canonical_current != current_bytes:
                raise ValueError("evidence artifact is not canonical JSONL")
            current_external_refs = frozenset(
                ref
                for row in current_records
                for ref in row.get("evidence_refs", ())
                if isinstance(ref, str) and not ref.startswith("work/")
            )
            current_rows = self._validate_evidence_note_rows(
                current_records,
                current_hashes=current_hashes,
                allow_unknown_external_refs=True,
            )
            new_rows = self._validate_evidence_note_rows(
                supplied,
                current_hashes=current_hashes,
                existing_external_refs=current_external_refs,
            )
            desired_bytes = b"".join(_json_bytes(row) for row in new_rows)
            if desired_bytes == current_bytes:
                return tuple(copy.deepcopy(dict(row)) for row in new_rows)

            prior_state = copy.deepcopy(self._state)
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_destination_exists = destination.exists() or destination.is_symlink()
            prior_destination_bytes = destination.read_bytes() if prior_destination_exists else None
            try:
                _atomic_write_bytes(destination, desired_bytes)
                progress = self._artifact_progress()
                next_state = copy.deepcopy(self._state)
                next_state["updated_at"] = _now()
                self._persist_state_unlocked(next_state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    self._restore_artifact_bytes(destination, prior_destination_exists, prior_destination_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("evidence replacement rollback failed") from exc
                raise
        self._emit(
            "item_workspace_replace_evidence_notes",
            artifact=relative,
            evidence_count=len(new_rows),
            artifact_hash=progress.hashes.get(relative),
        )
        return tuple(copy.deepcopy(dict(row)) for row in new_rows)

    def append_specialist_task(self, mapping: Mapping[str, Any]) -> None:
        """Append one bounded specialist assignment owned by the analytical lead."""

        self._append_work_record(_SPECIALIST_TASKS_FILENAME, mapping, label="specialist task")

    def append_specialist_memo(self, mapping: Mapping[str, Any]) -> None:
        """Append one specialist evidence memo without creating another item lifecycle."""

        self._append_work_record(_SPECIALIST_MEMOS_FILENAME, mapping, label="specialist memo")

    def append_semantic_selection(self, mapping: Mapping[str, Any]) -> None:
        """Append one owner-bound semantic reuse selection trace."""

        self._append_work_record(
            _SEMANTIC_SELECTIONS_FILENAME,
            mapping,
            label="semantic selection",
            dedupe_field="selection_id",
        )

    @staticmethod
    def _identity_domain_proposal_digest(
        mapping: Mapping[str, Any],
    ) -> tuple[Any, str]:
        """Parse one proposal and return its typed value plus stable digest.

        The digest intentionally excludes transport-only ``item_id`` and
        ``owner_ref`` fields.  Those fields remain independently validated for
        item/owner authorization, while historical owner-label rotation does
        not fork the semantic proposal chain.
        """

        from .analyst_workspace import IdentityDomainProposal

        if not isinstance(mapping, Mapping):
            raise ValueError("identity domain proposal must be a mapping")
        if mapping.get("record_kind") != "identity_domain_proposal":
            raise ValueError("identity domain proposal record_kind is invalid")
        try:
            proposal = IdentityDomainProposal.from_dict(mapping)
        except (TypeError, ValueError):
            # Keep legacy low-level rows observable long enough for the
            # Entity Resolution admission boundary to report its own precise
            # owner/proposal diagnostic.  Planner/effective callers still
            # parse the row through IdentityDomainProposal and fail closed on
            # genuinely malformed semantic fields.
            required = _IDENTITY_DOMAIN_PROPOSAL_BASE_FIELDS - {"record_kind"}
            if any(key not in mapping for key in required):
                raise ValueError("identity domain proposal is invalid")
            allowed = (
                _IDENTITY_DOMAIN_PROPOSAL_BASE_FIELDS
                | _IDENTITY_DOMAIN_PROPOSAL_REVISION_FIELDS
                | {"item_id", "owner_ref"}
            )
            if set(mapping).difference(allowed):
                raise ValueError("identity domain proposal contains unexpected fields")
            domain_id = mapping.get("domain_id")
            object_type = mapping.get("object_type")
            rationale = mapping.get("rationale")
            source_hints = mapping.get("source_hints")
            representation_item_ids = mapping.get("representation_item_ids")
            if not all(isinstance(value, str) and value.strip() for value in (domain_id, object_type, rationale)):
                raise ValueError("identity domain proposal is invalid")
            if not isinstance(source_hints, (list, tuple)) or not isinstance(
                representation_item_ids,
                (list, tuple),
            ):
                raise ValueError("identity domain proposal is invalid")
            if any(not isinstance(value, str) or not value.strip() for value in source_hints):
                raise ValueError("identity domain proposal is invalid")
            if any(not isinstance(value, str) or not value.strip() for value in representation_item_ids):
                raise ValueError("identity domain proposal is invalid")
            revision = mapping.get("revision", 1)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError("identity domain proposal revision is invalid")
            supersedes_hash = mapping.get("supersedes_hash")
            superseded_object_type = mapping.get("superseded_object_type")
            if revision == 1 and (supersedes_hash is not None or superseded_object_type is not None):
                raise ValueError("identity domain proposal revision metadata is invalid")
            payload: dict[str, Any] = {
                "record_kind": "identity_domain_proposal",
                "domain_id": str(domain_id).strip(),
                "object_type": str(object_type).strip(),
                "rationale": str(rationale).strip(),
                "source_hints": list(source_hints),
                "representation_item_ids": list(representation_item_ids),
            }
            if revision != 1:
                payload.update(
                    {
                        "revision": revision,
                        "supersedes_hash": supersedes_hash,
                        "superseded_object_type": superseded_object_type,
                    }
                )
            digest = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            ).hexdigest()
            supplied_digest = mapping.get("proposal_hash")
            if supplied_digest is not None and supplied_digest != digest:
                raise ValueError("identity domain proposal proposal_hash is invalid")
            return (
                _IdentityDomainProposalNode(
                    payload["domain_id"],
                    payload["object_type"],
                    payload["rationale"],
                    tuple(payload["source_hints"]),
                    tuple(payload["representation_item_ids"]),
                    revision,
                    supersedes_hash,
                    superseded_object_type,
                    digest,
                ),
                digest,
            )
        canonical = proposal.to_dict()
        for field_name in (*_IDENTITY_DOMAIN_PROPOSAL_BASE_FIELDS, *_IDENTITY_DOMAIN_PROPOSAL_REVISION_FIELDS):
            if field_name in mapping and mapping.get(field_name) != canonical.get(field_name):
                raise ValueError(f"identity domain proposal {field_name} is not canonical")
        digest = proposal.digest
        supplied_digest = mapping.get("proposal_hash")
        if supplied_digest is not None and supplied_digest != digest:
            raise ValueError("identity domain proposal proposal_hash is invalid")
        return proposal, digest

    def _identity_domain_proposal_records_locked(self) -> tuple[dict[str, Any], ...]:
        """Validate the append-only proposal chains under the item lock."""

        from .analyst_workspace import IdentityDomainProposal

        raw_records = self._read_work_records(
            _IDENTITY_DOMAIN_PROPOSALS_FILENAME,
            label="identity domain proposal",
        )
        by_domain: dict[str, dict[int, tuple[IdentityDomainProposal, str, dict[str, Any]]]] = {}
        domain_order: list[str] = []
        history: list[dict[str, Any]] = []
        for raw in raw_records:
            if raw.get("item_id") != self.item_id:
                raise ValueError("identity domain proposal item binding is invalid")
            raw_owner = raw.get("owner_ref")
            if not isinstance(raw_owner, str) or not raw_owner.strip():
                raise ValueError("identity domain proposal owner binding is invalid")
            try:
                owner_ref = _owner_ref_value(raw_owner)
            except (TypeError, ValueError) as exc:
                raise ValueError("identity domain proposal owner binding is invalid") from exc
            proposal, digest = self._identity_domain_proposal_digest(raw)
            revision = proposal.revision
            domain = proposal.domain_id
            record = dict(raw)
            # ``proposal_hash`` is a derived read value for legacy revision-one
            # rows.  It is not written back and therefore cannot alter history
            # bytes; successor rows persist the same digest explicitly.
            record["owner_ref"] = owner_ref
            record["proposal_hash"] = digest
            history.append(record)
            if domain not in by_domain:
                by_domain[domain] = {}
                domain_order.append(domain)
            revisions = by_domain[domain]
            prior = revisions.get(revision)
            if prior is not None:
                if prior[1] != digest:
                    raise ValueError(
                        "identity domain proposal revision conflicts with prior proposal: "
                        f"domain_id={domain}, revision={revision}"
                    )
                # Exact duplicate rows are harmless historical retries.  Keep
                # both in the audit history but use one canonical chain node.
                continue
            revisions[revision] = (proposal, digest, record)

        effective: list[dict[str, Any]] = []
        for domain in domain_order:
            revisions = by_domain[domain]
            if not revisions:
                continue
            ordinals = sorted(revisions)
            if ordinals[0] != 1 or ordinals != list(range(1, ordinals[-1] + 1)):
                raise ValueError(f"identity domain proposal revisions are not contiguous: domain_id={domain}")
            for revision in ordinals[1:]:
                predecessor, predecessor_hash, _predecessor_record = revisions[revision - 1]
                proposal, _digest, _record = revisions[revision]
                if proposal.supersedes_hash != predecessor_hash:
                    raise ValueError(
                        "identity domain proposal predecessor digest is invalid: "
                        f"domain_id={domain}, revision={revision}"
                    )
                if proposal.superseded_object_type != predecessor.object_type:
                    raise ValueError(
                        "identity domain proposal predecessor type audit is invalid: "
                        f"domain_id={domain}, revision={revision}"
                    )
            effective.append(dict(revisions[ordinals[-1]][2]))
        # Return rows in first-domain order for deterministic Planner/domain
        # ordering while selecting only the latest node of each valid chain.
        return tuple(effective)

    def _identity_domain_proposal_history_locked(self) -> tuple[dict[str, Any], ...]:
        """Validate and return all proposal rows with derived digests."""

        # Re-run the chain validator first so history callers never receive a
        # partially trusted branch.  The second read is cheap and preserves the
        # exact append order for the audit projection.
        self._identity_domain_proposal_records_locked()
        history: list[dict[str, Any]] = []
        for raw in self._read_work_records(
            _IDENTITY_DOMAIN_PROPOSALS_FILENAME,
            label="identity domain proposal",
        ):
            if raw.get("item_id") != self.item_id:
                raise ValueError("identity domain proposal item binding is invalid")
            raw_owner = raw.get("owner_ref")
            if not isinstance(raw_owner, str) or not raw_owner.strip():
                raise ValueError("identity domain proposal owner binding is invalid")
            proposal, digest = self._identity_domain_proposal_digest(raw)
            row = dict(raw)
            row["owner_ref"] = _owner_ref_value(raw_owner)
            row["proposal_hash"] = digest
            history.append(row)
        return tuple(history)

    def append_identity_domain_proposal(self, mapping: Mapping[str, Any]) -> None:
        """Append one owner-bound identity-domain proposal.

        Proposals are deliberately item-local evidence.  They do not reserve
        an entity-resolution domain or publish an accepted mapping; the
        runtime/entity-resolution boundary decides what, if anything, to do
        with them later.
        """

        if not isinstance(mapping, Mapping):
            raise TypeError("identity domain proposal must be a mapping")
        owner_ref = mapping.get("owner_ref")
        if owner_ref is None:
            raise ValueError("identity domain proposal owner_ref is required")
        # The ordinary append path is intentionally limited to a new semantic
        # proposal or an exact retry.  Once a domain has a durable row, callers
        # must use the explicit successor API below; silently treating a
        # changed object type as a retry would erase the canonical-type
        # collision that the Planner is required to surface.
        revision = mapping.get("revision", 1)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("identity domain proposal revision is invalid")
        if revision != 1 or mapping.get("supersedes_hash") is not None:
            raise ValueError("successor proposals require supersede_identity_domain_proposal")
        self._append_work_record(
            _IDENTITY_DOMAIN_PROPOSALS_FILENAME,
            mapping,
            label="identity domain proposal",
            dedupe_field="domain_id",
            # Historical retries may have used a different transport label
            # for the same logical owner.  Semantic proposal identity does
            # not include that provenance-only field.
            dedupe_ignored_fields=("owner_ref",),
            owner_ref=_owner_ref_value(owner_ref),
        )

    def supersede_identity_domain_proposal(
        self,
        mapping: Mapping[str, Any],
        *,
        expected_predecessor_hash: str,
        owner_ref: Any | None = None,
    ) -> dict[str, Any]:
        """Append one immutable, CAS-bound identity-domain proposal successor.

        The predecessor remains in the append-only JSONL history.  The
        successor receives the next contiguous revision and records the prior
        requested object type as ``superseded_object_type`` audit metadata.
        Transport owner/item labels are validated independently and are not
        part of the semantic predecessor digest.
        """

        if not isinstance(mapping, Mapping):
            raise TypeError("identity domain proposal successor must be a mapping")
        if mapping.get("record_kind") != "identity_domain_proposal":
            raise ValueError("identity domain proposal successor record_kind is invalid")
        if not isinstance(expected_predecessor_hash, str) or not _is_sha256(expected_predecessor_hash):
            raise ValueError("expected_predecessor_hash must be a SHA-256 digest")
        supplied_item = mapping.get("item_id")
        if supplied_item is not None and supplied_item != self.item_id:
            raise ValueError("identity domain proposal successor item binding is invalid")
        supplied_owner = mapping.get("owner_ref")
        effective_owner = owner_ref if owner_ref is not None else supplied_owner
        if effective_owner is None:
            raise ValueError("identity domain proposal successor owner_ref is required")

        from .analyst_workspace import IdentityDomainProposal

        try:
            supplied = IdentityDomainProposal.from_dict(mapping)
        except (TypeError, ValueError) as exc:
            raise ValueError("identity domain proposal successor is invalid") from exc
        if supplied.revision != 1 or supplied.supersedes_hash is not None or supplied.proposal_hash is not None:
            # Revision and hashes are program-derived from the current head;
            # callers may not smuggle an alternate branch through this API.
            raise ValueError("successor revision and hashes are program-owned")
        owner = _owner_ref_value(effective_owner)
        if supplied_owner is not None and _owner_ref_value(supplied_owner) != owner:
            raise ValueError("identity domain proposal successor owner_ref does not match the bound owner")

        destination_relative = (Path("work") / _IDENTITY_DOMAIN_PROPOSALS_FILENAME).as_posix()
        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            self._ensure_not_terminal()
            self._verify_analysis_owner_locked(owner)
            destination = self._resolve_item_subpath(destination_relative)
            _assert_regular_no_symlink(destination, label="identity domain proposal artifact")
            history = self._identity_domain_proposal_history_locked()
            predecessor = next(
                (
                    row
                    for row in history
                    if row.get("proposal_hash") == expected_predecessor_hash
                    and row.get("domain_id") == supplied.domain_id
                ),
                None,
            )
            if predecessor is None:
                raise ValueError("identity domain proposal predecessor is unknown")
            predecessor_proposal, predecessor_hash = self._identity_domain_proposal_digest(predecessor)
            if predecessor_hash != expected_predecessor_hash:
                raise ValueError("identity domain proposal predecessor digest is invalid")

            effective_rows = self._identity_domain_proposal_records_locked()
            current = next(
                (row for row in effective_rows if row.get("domain_id") == supplied.domain_id),
                None,
            )
            current_hash = None if current is None else current.get("proposal_hash")
            expected_revision = predecessor_proposal.revision + 1
            candidate = IdentityDomainProposal(
                domain_id=supplied.domain_id,
                object_type=supplied.object_type,
                rationale=supplied.rationale,
                source_hints=supplied.source_hints,
                representation_item_ids=supplied.representation_item_ids,
                revision=expected_revision,
                supersedes_hash=expected_predecessor_hash,
                superseded_object_type=predecessor_proposal.object_type,
            )
            candidate_hash = candidate.digest
            candidate_with_hash = IdentityDomainProposal(
                domain_id=candidate.domain_id,
                object_type=candidate.object_type,
                rationale=candidate.rationale,
                source_hints=candidate.source_hints,
                representation_item_ids=candidate.representation_item_ids,
                revision=candidate.revision,
                supersedes_hash=candidate.supersedes_hash,
                proposal_hash=candidate_hash,
                superseded_object_type=candidate.superseded_object_type,
            )
            candidate_payload = {
                **candidate_with_hash.to_dict(),
                "item_id": self.item_id,
                "owner_ref": owner,
            }
            if mapping.get("revision") is not None and mapping.get("revision") != expected_revision:
                raise ValueError("identity domain proposal successor revision is not contiguous")
            if mapping.get("supersedes_hash") is not None and mapping.get("supersedes_hash") != expected_predecessor_hash:
                raise ValueError("identity domain proposal successor predecessor digest conflicts")
            if (
                mapping.get("superseded_object_type") is not None
                and mapping.get("superseded_object_type") != predecessor_proposal.object_type
            ):
                raise ValueError("identity domain proposal successor predecessor type audit conflicts")

            # Exact retries are idempotent even after the successor has become
            # the effective head.  A different successor on the same
            # predecessor is a stale/conflicting CAS and must fail closed.
            matching_successors = [
                row
                for row in history
                if row.get("domain_id") == supplied.domain_id
                and row.get("supersedes_hash") == expected_predecessor_hash
            ]
            for existing in matching_successors:
                existing_proposal, existing_hash = self._identity_domain_proposal_digest(existing)
                if existing_proposal.to_dict() == candidate_with_hash.to_dict() and existing_hash == candidate_hash:
                    return dict(existing)
                raise ValueError("identity domain proposal successor conflicts with predecessor")
            if current_hash != expected_predecessor_hash:
                raise ValueError("identity domain proposal predecessor is stale")

            prior_state = copy.deepcopy(self._state)
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_destination_exists = destination.exists() or destination.is_symlink()
            prior_destination_bytes = destination.read_bytes() if prior_destination_exists else None
            try:
                _append_jsonl(destination, candidate_payload)
                state = copy.deepcopy(self._state)
                state["updated_at"] = _now()
                self._persist_state_unlocked(state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    self._restore_artifact_bytes(destination, prior_destination_exists, prior_destination_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("identity domain proposal successor rollback failed") from exc
                raise
        self._emit(
            "item_workspace_supersede_identity_domain_proposal",
            artifact=destination_relative,
            domain_id=supplied.domain_id,
            revision=expected_revision,
            supersedes_hash=expected_predecessor_hash,
            proposal_hash=candidate_hash,
        )
        return dict(candidate_payload)

    def append_analytical_relationship(self, mapping: Mapping[str, Any]) -> None:
        """Append one owner-bound analytical relationship evidence record."""

        if not isinstance(mapping, Mapping):
            raise TypeError("analytical relationship must be a mapping")
        owner_ref = mapping.get("owner_ref")
        if owner_ref is None:
            raise ValueError("analytical relationship owner_ref is required")
        dedupe_field = ("relationship_id", "audit_id")
        self._append_work_record(
            _ANALYTICAL_RELATIONSHIPS_FILENAME,
            mapping,
            label="analytical relationship",
            dedupe_field=dedupe_field,
            owner_ref=_owner_ref_value(owner_ref),
        )

    def replace_analytical_relationships(
        self,
        mappings: Iterable[Mapping[str, Any]],
        *,
        owner_ref: Any,
        replace_ids: Iterable[str] | None = None,
        expected_artifact_hash: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Atomically replace analytical relationships in the current view.

        With ``replace_ids`` omitted, supplied rows are the complete artifact;
        otherwise only the named relationships change.  Review packets,
        attempts and implementation versions are history, not write locks.
        """

        if isinstance(mappings, (str, bytes)):
            raise TypeError("analytical relationships must be an iterable of mappings")
        try:
            supplied = tuple(mappings)
        except TypeError as exc:
            raise TypeError("analytical relationships must be an iterable of mappings") from exc
        if any(not isinstance(value, Mapping) for value in supplied):
            raise TypeError("analytical relationships must contain mappings")
        owner = _owner_ref_value(owner_ref)
        if expected_artifact_hash is not None and not _is_sha256(expected_artifact_hash):
            raise ValueError("expected_artifact_hash must be a SHA-256 digest")

        if replace_ids is None:
            target_ids: tuple[str, ...] | None = None
        else:
            if isinstance(replace_ids, (str, bytes)):
                raise TypeError("replace_ids must be an iterable of relationship IDs")
            try:
                target_ids = tuple(_owner_ref_value(value) for value in replace_ids)
            except TypeError as exc:
                raise TypeError("replace_ids must be an iterable of relationship IDs") from exc
            if not target_ids:
                raise ValueError("replace_ids cannot be empty; omit it for a full replacement")
            if len(target_ids) != len(set(target_ids)):
                raise ValueError("replace_ids must not contain duplicates")

        relative = (Path("work") / _ANALYTICAL_RELATIONSHIPS_FILENAME).as_posix()
        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            self._ensure_not_terminal()
            self._verify_analysis_owner_locked(owner, bind_if_missing=True)

            destination = self._resolve_item_subpath(relative)
            _assert_regular_no_symlink(destination, label="analytical relationship artifact")
            if destination.exists() and not destination.is_file():
                raise ValueError("analytical relationship artifact must be a regular file")
            current_bytes = destination.read_bytes() if destination.exists() else b""
            current_hash = _sha256_bytes(current_bytes) if destination.exists() else None
            if expected_artifact_hash is not None and expected_artifact_hash != current_hash:
                raise ValueError("expected_artifact_hash does not match the current relationship artifact")

            current_records = self._read_work_records(
                _ANALYTICAL_RELATIONSHIPS_FILENAME,
                label="analytical relationship",
            )
            current_rows = self._validate_analytical_relationship_rows(current_records, owner=None)
            new_rows = self._validate_analytical_relationship_rows(supplied, owner=owner)
            current_by_id = {row["relationship_id"]: row for row in current_rows}
            if len(current_by_id) != len(current_rows):
                raise ValueError("analytical relationship relationship_id values must be unique")
            if target_ids is not None:
                missing = [value for value in target_ids if value not in current_by_id]
                if missing:
                    raise ValueError(
                        "replace_ids must identify existing analytical relationships: "
                        + ", ".join(missing)
                    )
                supplied_ids = tuple(row["relationship_id"] for row in new_rows)
                if set(supplied_ids) != set(target_ids) or len(supplied_ids) != len(target_ids):
                    raise ValueError("replacement rows must match replace_ids exactly")
                replacements = {row["relationship_id"]: row for row in new_rows}
                desired_rows = tuple(
                    replacements.get(row["relationship_id"], row)
                    for row in current_rows
                )
            else:
                desired_rows = tuple(new_rows)
            self._validate_analytical_relationship_rows(desired_rows, owner=None)
            desired_ids = [row["relationship_id"] for row in desired_rows]
            if len(desired_ids) != len(set(desired_ids)):
                raise ValueError("analytical relationship relationship_id values must be unique")
            audit_ids = [row["audit_id"] for row in desired_rows if row.get("audit_id") is not None]
            if len(audit_ids) != len(set(audit_ids)):
                raise ValueError("analytical relationship audit_id values must be unique")

            desired_bytes = b"".join(_json_bytes(row) for row in desired_rows)
            # Exact retries are read-only.  In particular, do not rewrite the
            # JSONL file, state, or review packet and thereby perturb mtimes.
            if desired_bytes == current_bytes:
                return tuple(copy.deepcopy(dict(row)) for row in desired_rows)

            prior_state = copy.deepcopy(self._state)
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            prior_state_bytes = state_path.read_bytes()
            prior_destination_exists = destination.exists() or destination.is_symlink()
            prior_destination_bytes = destination.read_bytes() if prior_destination_exists else None
            try:
                _atomic_write_bytes(destination, desired_bytes)
                progress = self._artifact_progress()
                next_state = copy.deepcopy(self._state)
                next_state["updated_at"] = _now()
                self._persist_state_unlocked(next_state, touch=False)
            except Exception as exc:
                rollback_errors: list[Exception] = []
                try:
                    self._restore_artifact_bytes(destination, prior_destination_exists, prior_destination_bytes)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    _atomic_write_bytes(state_path, prior_state_bytes)
                    self._state = prior_state
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("analytical relationship replacement rollback failed") from exc
                raise
        self._emit(
            "item_workspace_replace_analytical_relationships",
            artifact=relative,
            relationship_count=len(desired_rows),
            artifact_hash=progress.hashes.get(relative),
        )
        return tuple(copy.deepcopy(dict(row)) for row in desired_rows)

    def _validate_active_repair_progress(
        self,
        packet: Mapping[str, Any],
        *,
        current_hashes: Mapping[str, str],
        relationship_path: str,
    ) -> None:
        """Reject stale or out-of-scope progress before a repair replacement."""

        before_hashes = packet.get("before_artifact_hashes")
        after_hashes = packet.get("after_artifact_hashes")
        allowed_paths = tuple(
            str(path).replace("\\", "/")
            for name in ("allowed_artifact_paths", "allowed_dependencies")
            for path in packet.get(name, ())
            if not str(path).startswith("/")
        )

        def allowed(path: str) -> bool:
            normalized = path.replace("\\", "/")
            if normalized == relationship_path:
                return True
            # The draft file is governed by the JSON-pointer check below;
            # keeping it out of the artifact-path allowlist would reject the
            # ordinary same-owner narrative revision that an active repair
            # is explicitly authorized to make.
            if normalized == _DRAFT_FILENAME:
                return True
            return any(
                normalized == root or normalized.startswith(root.rstrip("/") + "/")
                for root in allowed_paths
            )

        if not isinstance(before_hashes, Mapping) or not isinstance(after_hashes, Mapping):
            raise ValueError("active business repair artifact hashes are invalid")
        if after_hashes.get(relationship_path) != current_hashes.get(relationship_path):
            raise ValueError("analytical relationship artifact changed since the active repair baseline")
        handoff_changed = (
            _HANDOFF_ARTIFACT_PATH in set(before_hashes) | set(after_hashes) | set(current_hashes)
            and before_hashes.get(_HANDOFF_ARTIFACT_PATH) != current_hashes.get(_HANDOFF_ARTIFACT_PATH)
            and after_hashes.get(_HANDOFF_ARTIFACT_PATH) != current_hashes.get(_HANDOFF_ARTIFACT_PATH)
        )
        handoff_refs = frozenset()
        typed_handoff_present = False
        if current_hashes.get(_HANDOFF_ARTIFACT_PATH) is not None:
            handoff_path = self._resolve_item_subpath(_HANDOFF_ARTIFACT_PATH)
            _assert_regular_no_symlink(handoff_path, label="handoff artifact")
            try:
                handoff_value = json.loads(handoff_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                handoff_value = None
            typed_handoff_present = (
                isinstance(handoff_value, Mapping)
                and handoff_value.get("schema_version") == _ANALYST_HANDOFF_SCHEMA
            )
        if (handoff_changed or typed_handoff_present) and current_hashes.get(_HANDOFF_ARTIFACT_PATH) is not None:
            handoff_refs = self._validate_repair_handoff(
                packet,
                current_hashes=current_hashes,
            )
        for path in set(before_hashes) | set(after_hashes) | set(current_hashes):
            before = before_hashes.get(path)
            after = after_hashes.get(path)
            current = current_hashes.get(path)
            if before != current or after != current:
                normalized = str(path).replace("\\", "/")
                if normalized == _HANDOFF_ARTIFACT_PATH and current is not None:
                    continue
                if normalized in handoff_refs:
                    continue
                if not allowed(str(path)):
                    raise ValueError(
                        "business repair changed artifact outside authorized scope: " + str(path)
                    )

        draft_value, _draft_hash = self._current_draft_value_and_hash()
        before_snapshot = packet.get("before_snapshot")
        if not isinstance(before_snapshot, Mapping):
            raise ValueError("active business repair draft baseline is invalid")
        changed_pointers = _pointer_diff(before_snapshot, draft_value)
        allowed_pointers = {
            str(path) for path in packet.get("allowed_pointers", ())
        }
        # ``/answer`` is the canonical narrative root emitted by the
        # semantic review adapter.  An answer-semantic finding owns the
        # complete mutable AnalystAnswer body (headline findings, scope,
        # method, supported/unsupported components, limitations, next
        # actions, visuals, and evidence refs), while the immutable envelope
        # fields remain outside this set.  Older packets intentionally retain
        # their narrow stored pointer union; this deterministic virtual
        # expansion lets the durable boundary enforce the same semantic
        # contract without rewriting historical review bytes.
        answer_body_bound = False
        answer_aliases = {"/answer", "/scope", "/next_actions"}
        for finding in packet.get("findings", ()):
            if not isinstance(finding, Mapping):
                continue
            categories = {
                str(category) for category in finding.get("semantic_categories", ())
            }
            pointers = {
                str(pointer) for pointer in finding.get("pointers", ())
            }
            if "answer" in categories or (
                categories.intersection({"calculation", "presentation"})
                and "/answer" in pointers
                and pointers.issubset(answer_aliases)
            ):
                answer_body_bound = True
                break
        if answer_body_bound:
            allowed_pointers.update(f"/{section}" for section in _ANSWER_DRAFT_SECTIONS)
        for pointer in changed_pointers:
            if not any(
                pointer == allowed_pointer
                or pointer.startswith(allowed_pointer.rstrip("/") + "/")
                for allowed_pointer in allowed_pointers
            ):
                raise ValueError(
                    "business repair changed answer outside authorized scope: " + str(pointer)
                )
        # Preserve the existing lexical/item-root and immutable-envelope
        # checks.  The explicit comparisons above narrow its item-local
        # repair allowance to the reviewer packet's actual scope.
        self._repair_scope_check()

    def _validate_analytical_relationship_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        owner: str | None,
    ) -> tuple[dict[str, Any], ...]:
        """Re-validate the strict typed facade shape at the durable boundary."""

        # Import lazily to keep the durable module's import graph acyclic.
        from .analyst_workspace import AnalyticalRelationshipEvidence

        validated: list[dict[str, Any]] = []
        for value in rows:
            if not isinstance(value, Mapping):
                raise TypeError("analytical relationship must be a mapping")
            if value.get("item_id") != self.item_id:
                raise ValueError("analytical relationship item_id is invalid")
            row_owner = value.get("owner_ref")
            if not isinstance(row_owner, str) or not row_owner.strip():
                raise ValueError("analytical relationship owner_ref is invalid")
            if owner is not None and row_owner != owner:
                raise ValueError("analytical relationship owner_ref is invalid")
            if "record_kind" not in value:
                raise ValueError("analytical relationship record_kind is required")
            try:
                typed = AnalyticalRelationshipEvidence.from_dict(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("analytical relationship row is invalid") from exc
            canonical = typed.to_dict()
            if set(value) != set(canonical) | {"item_id", "owner_ref"}:
                raise ValueError("analytical relationship row fields are invalid")
            validated.append(
                {
                    **canonical,
                    "item_id": self.item_id,
                    "owner_ref": row_owner,
                }
            )
        return tuple(validated)

    def _read_work_records(self, filename: str, *, label: str) -> tuple[dict[str, Any], ...]:
        """Read an item-local JSONL work record without exposing mutable state."""

        path = self._resolve_item_subpath(Path("work") / filename)
        _assert_regular_no_symlink(path, label=f"{label} artifact")
        if not path.exists():
            return ()
        try:
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} artifact is invalid") from exc
        if any(not isinstance(record, Mapping) for record in records):
            raise ValueError(f"{label} artifact is invalid")
        return tuple(copy.deepcopy(dict(record)) for record in records)

    def read_identity_domain_proposals(
        self,
        *,
        include_superseded: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """Return effective proposal heads or the validated full history.

        Planner/entity-resolution callers receive exactly one latest proposal
        per domain by default.  ``include_superseded`` is an explicit audit
        read and retains every immutable predecessor row.
        """

        if type(include_superseded) is not bool:
            raise TypeError("include_superseded must be a bool")
        if include_superseded:
            return self.read_identity_domain_proposal_history()
        return tuple(
            dict(row)
            for row in self._identity_domain_proposal_records_locked()
        )

    def read_identity_domain_proposal_history(self) -> tuple[dict[str, Any], ...]:
        """Return all validated append-only proposal rows with digests."""

        return tuple(
            dict(row)
            for row in self._identity_domain_proposal_history_locked()
        )

    def read_analytical_relationships(self) -> tuple[dict[str, Any], ...]:
        """Return immutable-by-copy analytical relationship evidence."""

        return self._read_work_records(
            _ANALYTICAL_RELATIONSHIPS_FILENAME,
            label="analytical relationship",
        )

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
                if relative in {
                    _STATE_FILENAME,
                    str(Path("work") / _BUSINESS_REVIEW_FILENAME),
                    str(Path("work") / _DATA_INSUFFICIENCY_FILENAME),
                    # The repair-upgrade intent is a transient transaction
                    # record.  It remains independently validated and any
                    # residue fails closed, but must never become part of the
                    # business repair artifact baseline: the intent is
                    # removed after the manifest/audit commit.
                    "work/analysis_context_repair_upgrade_intent.json",
                } or "telemetry" in relative_path.parts:
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
        self._reconcile_business_review_discard()
        lane_id = str(lane_id).strip()
        role = str(role).strip()
        route = str(route).strip()
        if not lane_id or not role or not route:
            raise ValueError("lane_id, role, and route must be non-empty")
        if route == "recovery":
            raise ValueError("route='recovery' requires begin_recovery authorization")
        if self._state.get("active_attempt_id") is not None:
            raise ValueError("an attempt is already active")
        if self._state.get("lifecycle_state") == "recovery_ready":
            raise ValueError("recovery_ready requires begin_recovery")
        baseline = self.artifact_progress()
        attempt = ExecutionAttempt(self._next_attempt_id(), lane_id, role, route, "active", baseline)
        state = dict(self._state)
        state["attempts"] = [dict(record) for record in self._state["attempts"]]
        record = attempt.to_dict()
        record.update(
            {
                "prior_attempt_id": None,
                "handoff_ref": None,
                "error": None,
                "recovery_receipt_ref": None,
                "recovery_invocation_id": None,
                "recovery_receipt_hash": None,
            }
        )
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
        self._reconcile_business_review_discard()
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
                # Do not leave the only Analytical Owner slot occupied by a
                # turn that has produced no material artifact twice.  This is
                # an operational retry of the same owner and attempt, not a
                # lifecycle recovery or a business-repair transition.
                action = "retry_same_attempt"
                state["lifecycle_state"] = "work"
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
        self._reconcile_business_review_discard()
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

    def begin_recovery(
        self,
        lane_id: str,
        role: str,
        *,
        prior_attempt_id: str,
        receipt_ref: str,
    ) -> ExecutionAttempt:
        """Start recovery only for one persisted, hash-bound loss receipt.

        Filesystem silence and direct receipt objects are deliberately not
        authorization inputs.  The stable reference is resolved against a
        freshly-reloaded run-local ledger before any item state is changed.
        """

        self._ensure_execution_state()
        self._ensure_not_terminal()
        self._reconcile_business_review_discard()
        if self._state.get("lifecycle_state") not in {"work", "recovery_ready"}:
            raise ValueError("begin_recovery requires an active work attempt")
        prior_attempt_id = str(prior_attempt_id).strip()
        if self._state.get("active_attempt_id") != prior_attempt_id:
            raise ValueError("begin_recovery requires the current active attempt")
        prior_index, prior = self._attempt_record(prior_attempt_id)
        if prior.get("status") != "active":
            raise ValueError("begin_recovery requires the current active attempt")
        try:
            from .lifecycle import InvocationReceiptLedger, classify_terminal_reason

            ledger = InvocationReceiptLedger(context=self.context)
            receipt, receipt_hash = ledger.resolve(receipt_ref)
            if receipt.item_id != self.item_id:
                raise ValueError("invocation receipt item_id does not match workspace")
            if receipt.attempt_id != prior_attempt_id:
                raise ValueError("invocation receipt attempt_id does not match prior attempt")
            if receipt.lane_id != prior["lane_id"]:
                raise ValueError("invocation receipt lane_id does not match prior attempt")
            if receipt.role != prior["role"]:
                raise ValueError("invocation receipt role does not match prior attempt")
            if receipt.finish is None or not receipt.terminal_reason:
                raise ValueError("begin_recovery requires a completed invocation receipt")
            if classify_terminal_reason(receipt.terminal_reason) != "execution_recovery":
                raise ValueError("invocation receipt does not authorize execution recovery")
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("begin_recovery requires a valid persisted invocation receipt") from exc
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
        recovery_record["recovery_receipt_ref"] = str(receipt_ref).strip()
        recovery_record["recovery_invocation_id"] = receipt.invocation_id
        recovery_record["recovery_receipt_hash"] = receipt_hash
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
            recovery_receipt_ref=recovery_record["recovery_receipt_ref"],
            recovery_invocation_id=receipt.invocation_id,
            recovery_receipt_hash=receipt_hash,
        )
        return attempt

    def _record_review_unlocked(
        self,
        verdict: str,
        *,
        reviewer_ref: str | None = None,
        review_status: str = "reviewed",
        findings: Any = None,
    ) -> dict[str, Any]:
        """Persist a reviewer result and a structured, item-local review packet.

        The ordinary review state remains the compact five-field lifecycle
        contract.  Material findings and their exact scope live in the
        ``work/business_review.json`` packet so older state templates remain
        mechanically valid while repair authorization is still durable.
        """

        self._ensure_execution_state()
        self._ensure_not_terminal()
        self._require_no_active_attempt()
        self._reconcile_business_review_discard()
        status = str(review_status).strip()
        verdict = str(verdict).strip()
        normalized_findings = self._normalize_findings(findings)
        if verdict == "repair_once":
            self._require_repair_findings(normalized_findings)
            if any(finding.get("semantic_categories") is None for finding in normalized_findings):
                raise ValueError("repair_once requires canonical semantic category provenance")
        if verdict == _CONFIRM_DATA_VERDICT:
            if normalized_findings:
                raise ValueError("confirm_data_insufficiency cannot carry reviewer findings")
            if reviewer_ref is None or not str(reviewer_ref).strip():
                raise ValueError("confirm_data_insufficiency requires reviewer_ref")
            conclusion = self._read_data_insufficiency_conclusion()
            if conclusion is None:
                raise ValueError("confirm_data_insufficiency requires an owner conclusion")
            self._validate_data_insufficiency_binding(conclusion, require_current=True)

        # Read and validate every input needed by the packet before touching
        # item_state.json or the prior business-review artifact.
        draft_value, draft_hash = self._current_draft_value_and_hash()
        progress = self._artifact_progress()
        pointer_hashes = _pointer_hashes(draft_value)
        previous_packet = self._read_business_review()
        targeted = previous_packet is not None and previous_packet.get("repair_active") is True
        if targeted:
            # A repair re-review is intentionally narrow.  It cannot silently
            # become another full-answer review.
            # The active repair boundary is item-local, not category-local:
            # validate all current answer/work changes before producing the
            # targeted packet.  Finding categories remain reviewer evidence.
            self._repair_scope_check()
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
        if targeted and previous_packet is not None:
            before_value = previous_packet["before_snapshot"]
            changed_pointers = _pointer_diff(before_value, draft_value)
            packet = copy.deepcopy(previous_packet)
            packet.update(
                {
                    "review_scope": "targeted",
                    "reviewed_draft_hash": draft_hash,
                    "after_pointer_hashes": pointer_hashes,
                    "after_artifact_hashes": dict(progress.hashes),
                    "changed_pointers": sorted(set(changed_pointers)),
                    "unchanged_paths": sorted(
                        path
                        for path, digest in previous_packet["before_pointer_hashes"].items()
                        if pointer_hashes.get(path) == digest
                    ),
                    "targeted_recheck": True,
                    "repair_active": False,
                }
            )
            unchanged_hashes = {
                path: pointer_hashes[path]
                for path in packet["unchanged_paths"]
                if path in pointer_hashes
            }
            packet["unchanged_aggregate_hash"] = _sha256_bytes(_json_bytes(unchanged_hashes))
            if verdict == _CONFIRM_DATA_VERDICT:
                packet["findings"] = []
            elif verdict == "repair_once":
                packet["findings"] = normalized_findings
                # A targeted reviewer may authorize a second same-owner
                # repair with a narrower semantic finding set.  Replace the
                # aggregate authorization with the exact union derived from
                # those new canonical findings; retaining the prior repair's
                # union would fail validation (and, worse, leave the old
                # scope active for the next repair).  The prior scope was
                # already used above to validate every changed pointer and
                # artifact before this replacement is staged.
                (
                    packet["allowed_pointers"],
                    packet["allowed_artifact_paths"],
                    packet["allowed_dependencies"],
                ) = self._business_review_scope_union(normalized_findings)
            elif normalized_findings:
                packet["findings"] = normalized_findings
        else:
            allowed_pointers = sorted(
                {
                    pointer
                    for finding in normalized_findings
                    for pointer in (*finding["pointers"], *finding["dependent_outputs"])
                    if pointer.startswith("/") or pointer == ""
                }
            )
            allowed_artifacts = sorted(
                {path for finding in normalized_findings for path in finding["artifact_paths"]}
            )
            allowed_dependencies = sorted(
                {path for finding in normalized_findings for path in finding["dependent_outputs"] if not path.startswith("/")}
            )
            packet = {
                "item_id": self.item_id,
                "review_scope": "full",
                "reviewed_draft_hash": draft_hash,
                "before_hash": draft_hash,
                "before_snapshot": draft_value,
                "before_pointer_hashes": pointer_hashes,
                "before_artifact_hashes": dict(progress.hashes),
                "after_pointer_hashes": pointer_hashes,
                "after_artifact_hashes": dict(progress.hashes),
                "findings": normalized_findings,
                "allowed_pointers": allowed_pointers,
                "allowed_artifact_paths": allowed_artifacts,
                "allowed_dependencies": allowed_dependencies,
                "changed_pointers": [],
                "unchanged_paths": sorted(pointer_hashes),
                "unchanged_aggregate_hash": _sha256_bytes(_json_bytes(pointer_hashes)),
                "repair_active": False,
                "targeted_recheck": False,
            }
        self._validate_business_review_payload(packet)
        state = dict(self._state)
        state["review"] = review
        state["lifecycle_state"] = "review"
        self._commit_business_review(packet, state)
        self._emit(
            "item_review_recorded",
            review_status=review["status"],
            review_strength=review["strength"],
            verdict=review["verdict"],
            reviewer_ref=review["reviewer_ref"],
            draft_hash=draft_hash,
            review_scope=packet["review_scope"],
            finding_count=len(packet["findings"]),
            targeted_recheck=packet["targeted_recheck"],
        )
        result = dict(review)
        result.update(
            {
                "review_scope": packet["review_scope"],
                "finding_count": len(packet["findings"]),
                "findings": _jsonable(packet["findings"]),
                "changed_pointers": list(packet["changed_pointers"]),
                "targeted_recheck": packet["targeted_recheck"],
            }
        )
        return result

    def record_review(
        self,
        verdict: str,
        *,
        reviewer_ref: str | None = None,
        review_status: str = "reviewed",
        findings: Any = None,
    ) -> dict[str, Any]:
        """Record one reviewer verdict against freshly reloaded item state."""

        with self._state_transition_lock():
            self._reload_authoritative_for_artifact_mutation_locked()
            return self._record_review_unlocked(
                verdict,
                reviewer_ref=reviewer_ref,
                review_status=review_status,
                findings=findings,
            )

    def use_business_repair(self, *, owner_ref: Any) -> dict[str, Any]:
        """Open the next item-local business repair from a reviewed finding."""

        with self._state_transition_lock():
            self._reload_authoritative_state_locked()
            # Validate an existing owner binding without creating a new
            # program-owned artifact before the reviewer baseline check.  A
            # direct durable caller may establish its owner at repair
            # activation; that first binding must not mask reviewer-artifact
            # drift or mutate state before the fail-closed check below.
            owner_binding = self._read_analysis_owner()
            if owner_binding is not None:
                self._verify_analysis_owner_locked(owner_ref)
            self._ensure_execution_state()
            self._reconcile_business_review_discard()
            self._ensure_not_terminal()
            self._require_no_active_attempt()
            self._reconcile_business_review_discard()
            count = int(self._state["business_repair_count"])
            review = self._state["review"]
            if review.get("verdict") != "repair_once":
                raise ValueError("business repair requires a repair_once review verdict")
            packet = self._read_business_review()
            if packet is None:
                raise ValueError("business repair requires a structured finding packet")
            self._require_repair_findings(self._normalize_findings(packet.get("findings")))
            draft_value, draft_hash = self._current_draft_value_and_hash()
            if packet.get("reviewed_draft_hash") != draft_hash:
                raise ValueError("business repair requires the exact currently reviewed draft")
            progress = self._artifact_progress()
            reviewed_artifact_hashes = packet.get("after_artifact_hashes")
            if not isinstance(reviewed_artifact_hashes, Mapping) or dict(progress.hashes) != dict(reviewed_artifact_hashes):
                raise ValueError("business repair requires the exact currently reviewed artifact progress")
            self._verify_analysis_owner_locked(owner_ref, bind_if_missing=True)
            # Binding a previously-unbound owner is itself a durable
            # program-owned artifact.  Capture it in the active repair
            # baseline only after the exact reviewer baseline has passed.
            progress = self._artifact_progress()
            pointer_hashes = _pointer_hashes(draft_value)
            packet = copy.deepcopy(packet)
            # Every repair gets the currently reviewed draft and artifact set
            # as its own immutable baseline.  This prevents a second repair
            # from inheriting the first repair's before-snapshot and scope.
            packet.update(
                {
                    "reviewed_draft_hash": draft_hash,
                    "before_hash": draft_hash,
                    "before_snapshot": draft_value,
                    "before_pointer_hashes": pointer_hashes,
                    "before_artifact_hashes": dict(progress.hashes),
                    "after_pointer_hashes": pointer_hashes,
                    "after_artifact_hashes": dict(progress.hashes),
                    "changed_pointers": [],
                    "unchanged_paths": sorted(pointer_hashes),
                    "unchanged_aggregate_hash": _sha256_bytes(_json_bytes(pointer_hashes)),
                    "repair_active": True,
                    "targeted_recheck": False,
                    "review_scope": "full",
                }
            )
            state = dict(self._state)
            state["business_repair_count"] = count + 1
            state["review"] = self._execution_defaults()["review"]
            state["lifecycle_state"] = "work"
            self._commit_business_review(packet, state)
            self._emit(
                "item_business_repair_used",
                repair_count=state["business_repair_count"],
                allowed_pointers=packet["allowed_pointers"],
                allowed_artifact_paths=packet["allowed_artifact_paths"],
                allowed_dependencies=packet["allowed_dependencies"],
                before_hash=packet["before_hash"],
                unchanged_aggregate_hash=packet["unchanged_aggregate_hash"],
            )
            return copy.deepcopy(packet)

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

    def _normalise_acceptance_ref(self, value: Any) -> tuple[str, bool]:
        """Normalize an answer evidence ref and identify item-local paths.

        Analyst answers may carry source identifiers and JSONL row fragments in
        addition to item-local files.  The latter are reduced to their
        manifest-bound regular-file path; source identifiers remain useful
        analytical evidence but are not mistaken for filesystem artifacts.
        """

        if not isinstance(value, str):
            raise ValueError("answer evidence_refs must contain strings")
        ref = value.strip()
        if not ref:
            raise ValueError("answer evidence_refs must contain non-empty strings")
        # Owners sometimes persist a run-relative path including the current
        # item namespace (``requirements/<item>/work/...``).  Canonicalize
        # that one form to item-local ``work/...``; a sibling item's path is
        # deliberately left external and can never become a typed handoff.
        for namespace in ("requirements", "questions"):
            prefix = f"{namespace}/{self.item_id}/"
            if ref.startswith(prefix):
                ref = ref[len(prefix) :]
                break
        local = ref.startswith(("work/", "draft.json", "answer_content.json", "accepted/"))
        base = ref.split("#", 1)[0] if local else ref
        if local:
            if not base or "#" in base or "\\" in base or "\x00" in base:
                raise ValueError("answer evidence reference is invalid")
            path = PurePath(base)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != base:
                raise ValueError("answer evidence reference is invalid")
            return base, True
        return ref, False

    def _validate_acceptance_refs(
        self,
        payload: Mapping[str, Any],
        progress: ArtifactProgress,
        explicit_refs: Iterable[Any],
        *,
        content_hash: str,
    ) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
        """Derive accepted refs and typed artifact descriptors from the answer."""

        raw_answer_refs = payload.get("evidence_refs", ())
        if raw_answer_refs is None:
            raw_answer_refs = ()
        if isinstance(raw_answer_refs, (str, bytes, Mapping)) or not isinstance(raw_answer_refs, (list, tuple)):
            raise ValueError("answer evidence_refs must be a sequence")
        manifest_hashes = dict(progress.hashes)
        normalized: list[str] = []
        answer_local_refs: list[str] = []

        def bind_ref(raw: Any, *, from_answer: bool) -> None:
            ref, local = self._normalise_acceptance_ref(raw)
            if not local:
                # External/source identifiers do not identify item files and
                # therefore cannot be validated against the terminal manifest.
                # Preserve them only when supplied through the legacy explicit
                # accepted_refs argument; answer evidence remains the typed
                # handoff authority.
                if not from_answer and ref not in normalized:
                    normalized.append(ref)
                return
            if ref in normalized:
                if from_answer and ref not in answer_local_refs:
                    answer_local_refs.append(ref)
                return
            if ref == "answer_content.json":
                expected_hash = content_hash
                path = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / "answer_content.json")
            elif ref == "accepted/answer_content.json":
                expected_hash = content_hash
                path = self._resolve_item_subpath(Path(_ACCEPTED_FILENAME) / "answer_content.json")
            else:
                expected_hash = manifest_hashes.get(ref)
                if expected_hash is None:
                    if from_answer:
                        raise ValueError(f"accepted evidence reference is not bound by reviewed manifest: {ref}")
                    # The caller-supplied accepted_refs argument is only a
                    # legacy supplement.  It must never be needed for the
                    # sealed answer handoff, so an obsolete/missing hint is
                    # retained as a legacy envelope hint rather than blocking
                    # business acceptance.  Integration never treats this
                    # hint as a typed artifact without a sealed descriptor.
                    normalized.append(ref)
                    return
                path = self._resolve_item_subpath(ref)
            _assert_regular_no_symlink(path, label="accepted evidence artifact")
            if not path.is_file():
                if from_answer:
                    raise ValueError(f"accepted evidence reference is missing: {ref}")
                normalized.append(ref)
                return
            if _sha256_file(path) != expected_hash:
                if from_answer:
                    raise ValueError(f"accepted evidence reference hash does not match reviewed manifest: {ref}")
                normalized.append(ref)
                return
            normalized.append(ref)
            if from_answer:
                answer_local_refs.append(ref)

        for raw in raw_answer_refs:
            bind_ref(raw, from_answer=True)
        # Explicit refs are retained as supplemental evidence for existing
        # callers, but the answer-derived refs above are sufficient to create
        # the sealed typed handoff when the caller supplies no whitelist.
        if explicit_refs is None:
            explicit_refs = ()
        elif isinstance(explicit_refs, (str, bytes, Mapping)):
            explicit_refs = (explicit_refs,)
        for raw in explicit_refs:
            bind_ref(raw, from_answer=False)

        descriptors: list[dict[str, Any]] = []
        seen_artifact_ids: set[str] = set()
        from .analytical_artifacts import (
            ANALYTICAL_ARTIFACT_TYPES,
            AnalyticalArtifact,
            AnalyticalArtifactValidationError,
        )
        for ref in sorted(set(answer_local_refs)):
            if not ref.startswith("work/") or not ref.lower().endswith(".json"):
                continue
            path = self._resolve_item_subpath(ref)
            raw_bytes = path.read_bytes()
            try:
                raw_value = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw_value, Mapping):
                continue
            # Only an explicit analytical-artifact discriminator opts a work
            # JSON file into strict artifact parsing.  Projection/result JSON
            # without that discriminator remains ordinary evidence.
            if "artifact_type" not in raw_value:
                continue
            try:
                artifact = AnalyticalArtifact.from_dict(raw_value)
            except (AnalyticalArtifactValidationError, TypeError, ValueError) as exc:
                raise ValueError(f"accepted analytical artifact is invalid: {ref}") from exc
            if artifact.artifact_type not in ANALYTICAL_ARTIFACT_TYPES:
                raise ValueError(f"accepted analytical artifact type is unsupported: {artifact.artifact_type!r}")
            if artifact.requirement_id != self.item_id:
                raise ValueError(f"accepted analytical artifact requirement_id does not match item: {ref}")
            if artifact.artifact_id in seen_artifact_ids:
                # Do not defer duplicate identity detection to Integration
                # staging.  Business acceptance must fail before its durable
                # terminal intent can be written, regardless of matching
                # content bytes or distinct work-file references.
                raise ValueError(
                    f"accepted analytical artifact_id values must be unique: {artifact.artifact_id}"
                )
            seen_artifact_ids.add(artifact.artifact_id)
            canonical_bytes = artifact.to_json().encode("utf-8")
            if raw_bytes != canonical_bytes:
                raise ValueError(f"accepted analytical artifact bytes are not canonical: {ref}")
            descriptors.append(
                {
                    "ref": ref,
                    "hash": manifest_hashes.get(ref),
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "schema_version": artifact.schema_version,
                    "requirement_id": artifact.requirement_id,
                    "content_hash": artifact.content_hash,
                    "envelope_hash": artifact.envelope_hash,
                    "canonical_bytes_sha256": _sha256_bytes(canonical_bytes),
                }
            )
        descriptors.sort(key=lambda descriptor: descriptor["ref"])
        if any(not _is_sha256(descriptor["hash"]) for descriptor in descriptors):
            raise ValueError("accepted analytical artifact descriptor is not manifest-bound")
        return tuple(sorted(set(normalized))), tuple(descriptors)

    def accept(
        self,
        *,
        knowledge_delta: str = "no_change",
        accepted_refs: tuple[str, ...] = (),
    ) -> AcceptedSnapshot:
        """Linearize acceptance against all item writers and terminalizers."""

        with self._state_transition_lock():
            self._reload_authoritative_for_terminal_transition_locked()
            return self._accept_unlocked(
                knowledge_delta=knowledge_delta,
                accepted_refs=accepted_refs,
            )

    def _accept_unlocked(
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
        self._reconcile_business_review_discard()
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
        accepted_outcome = "accepted"
        if review.get("verdict") == "accept_with_limits" or review.get("status") == "unavailable":
            accepted_outcome = "accepted_with_limits"
        content_name = "answer_content.json"
        envelope_name = "acceptance_envelope.json"
        # Business acceptance must publish the same item-local artifact set
        # that the final Business Review inspected.  The draft hash alone is
        # insufficient: a work artifact can be added or replaced between
        # review and acceptance, and those bytes would otherwise become
        # authorized merely because the caller listed their refs in
        # ``accepted_refs``.  Compare the complete post-review hash map while
        # the transition lock is held; a mismatch is a review-boundary drift
        # and requires a fresh Business Review.
        progress = self._artifact_progress(payload)
        review_packet = self._read_business_review()
        reviewed_artifact_hashes = (
            review_packet.get("after_artifact_hashes")
            if isinstance(review_packet, Mapping)
            else None
        )
        if not isinstance(reviewed_artifact_hashes, Mapping) or dict(progress.hashes) != dict(reviewed_artifact_hashes):
            raise ValueError(
                "accept requires the exact currently reviewed artifact progress; re-review required"
            )
        try:
            answer_value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("accepted answer content is invalid JSON") from exc
        if not isinstance(answer_value, Mapping):
            raise ValueError("accepted answer content must be a JSON object")
        refs, analytical_descriptors = self._validate_acceptance_refs(
            answer_value,
            progress,
            accepted_refs or (),
            content_hash=content_hash,
        )
        handoff_unsigned = {
            "schema_version": _ACCEPTED_ANALYTICAL_HANDOFF_SCHEMA,
            "artifacts": list(analytical_descriptors),
        }
        analytical_handoff = {
            **handoff_unsigned,
            "handoff_hash": _sha256_bytes(_json_bytes(handoff_unsigned)),
        }
        accepted_at = _now()
        envelope = {
            "item_id": self.item_id,
            "outcome": accepted_outcome,
            "review_status": review.get("status"),
            "review_strength": review.get("strength"),
            "review_verdict": review.get("verdict"),
            "reviewer_ref": review.get("reviewer_ref"),
            "content_hash": content_hash,
            "draft_hash": content_hash,
            "accepted_refs": list(refs),
            "knowledge_delta": knowledge_delta,
            "accepted_at": accepted_at,
        }
        # Keep content-only/relationship-only answers on the compact legacy
        # envelope shape; typed analytical output carries the additional
        # sealed handoff object.  Bundle loaders treat an absent handoff as an
        # empty tuple, while any present handoff is validated strictly.
        if analytical_descriptors:
            envelope[_ACCEPTED_ANALYTICAL_HANDOFF_FIELD] = analytical_handoff
        envelope_payload = _json_bytes(envelope)
        envelope_hash = _sha256_bytes(envelope_payload)
        manifest = {
            "item_id": self.item_id,
            "outcome": accepted_outcome,
            "content_path": content_name,
            "content_hash": content_hash,
            "envelope_path": envelope_name,
            "envelope_hash": envelope_hash,
            "hashes": dict(progress.hashes),
            "artifact_progress": progress.to_dict(),
        }
        manifest["manifest_hash"] = _manifest_hash(manifest)
        intent_state = dict(self._state)
        intent_state["terminal_intent"] = {"outcome": accepted_outcome, "manifest_hash": manifest["manifest_hash"]}
        self._persist_state(intent_state)
        manifest_path = self._publish_accepted_directory(
            {content_name: payload, envelope_name: envelope_payload},
            manifest,
        )
        snapshot = AcceptedSnapshot(self.item_id, accepted_outcome, str(manifest_path), content_hash)
        state = dict(self._state)
        state["lifecycle_state"] = "accepted"
        state["terminal_outcome"] = {"status": accepted_outcome, **snapshot.to_dict()}
        self._persist_state(state)
        self._emit("item_accepted", outcome=accepted_outcome, manifest_path=str(manifest_path), content_hash=content_hash)
        return snapshot

    def mark_integration_committed(self, manifest_hash: str, manifest_ref: str) -> dict[str, Any]:
        """Record a program-owned integration commit for an accepted item.

        Integration is deliberately separate from analytical acceptance.  The
        item remains business-terminal while this state is ``pending`` until a
        trusted IntegrationSession calls this method with the immutable
        integration manifest identity.
        """

        self._ensure_execution_state()
        self._reconcile_business_review_discard()
        if self._state.get("lifecycle_state") != "accepted":
            raise ValueError("integration can be committed only for an accepted item")
        if self._state.get("integration_state") != "pending":
            raise ValueError("integration is already terminal")
        manifest_hash = str(manifest_hash).strip()
        manifest_ref = str(manifest_ref).strip()
        if not _is_sha256(manifest_hash):
            raise ValueError("integration manifest hash must be a SHA-256 digest")
        if not manifest_ref or Path(manifest_ref).is_absolute() or "\x00" in manifest_ref:
            raise ValueError("integration manifest ref is invalid")
        state = copy.deepcopy(self._state)
        state["integration_state"] = "integrated"
        state["integration_manifest_hash"] = manifest_hash
        state["integration_manifest_ref"] = manifest_ref
        self._persist_state(state)
        self._emit("item_integration_committed", integration_manifest_ref=manifest_ref, integration_manifest_hash=manifest_hash)
        return copy.deepcopy(self._state)

    def mark_integration_failed(
        self,
        manifest_hash: str,
        manifest_ref: str,
        *,
        recovery_exhausted: bool = False,
    ) -> dict[str, Any]:
        """Record a recoverable accepted-integration incident.

        Analytical acceptance is already immutable under ``accepted/``.  A
        failure-shaped compatibility call must therefore leave the item
        ``pending`` and return a typed same-session continuation envelope,
        rather than creating a terminal integration state or throwing.  The
        run-level incident ledger supplies the durable, idempotent record; the
        accepted bytes and item state are not rewritten.
        """

        self._ensure_execution_state()
        self._reconcile_business_review_discard()
        if self._state.get("lifecycle_state") != "accepted":
            raise ValueError("integration can be failed only for an accepted item")
        if self._state.get("integration_state") != "pending":
            raise ValueError("integration is already terminal")
        manifest_hash = str(manifest_hash).strip()
        manifest_ref = str(manifest_ref).strip()
        if not _is_sha256(manifest_hash):
            raise ValueError("integration manifest hash must be a SHA-256 digest")
        if not manifest_ref or Path(manifest_ref).is_absolute() or "\x00" in manifest_ref:
            raise ValueError("integration manifest ref is invalid")
        terminal = self._state.get("terminal_outcome")
        accepted_content_hash = terminal.get("content_hash") if isinstance(terminal, Mapping) else None
        if not _is_sha256(accepted_content_hash):
            raise ValueError("accepted integration content hash is unavailable")
        from .requirement_planning import RequirementSupervisorWorkspace

        incident_id = "INC-" + _sha256_bytes(
            _json_bytes(
                {
                    "kind": "accepted_integration_recovery",
                    "item_id": self.item_id,
                    "accepted_content_hash": accepted_content_hash,
                    "manifest_hash": manifest_hash,
                    "manifest_ref": manifest_ref,
                }
            )
        )[:24]
        incident = IncidentRecord(
            incident_id=incident_id,
            category="recovery",
            disposition="recovery_exhausted" if recovery_exhausted else "pending_same_session",
            admissible=False,
            item_id=self.item_id,
            scope=("integration", self.item_id),
            source="item_workspace",
            facts={
                "accepted_content_hash": accepted_content_hash,
                "manifest_hash": manifest_hash,
                "manifest_ref": manifest_ref,
                "continuation": "terminal" if recovery_exhausted else "same_session",
                "recovery_exhausted": bool(recovery_exhausted),
            },
        )
        recorded = RequirementSupervisorWorkspace(self.context).record_incident(incident)
        if recovery_exhausted:
            state = dict(self._state)
            state["integration_state"] = "technical_failure"
            state["integration_manifest_hash"] = manifest_hash
            state["integration_manifest_ref"] = manifest_ref
            self._persist_state(state)
            self._emit(
                "item_integration_technical_failure",
                integration_manifest_ref=manifest_ref,
                integration_manifest_hash=manifest_hash,
                recovery_exhausted=True,
            )
        return {
            "status": "technical_failure" if recovery_exhausted else "pending",
            "recoverable": not recovery_exhausted,
            "continuation": "terminal" if recovery_exhausted else "same_session",
            "item_id": self.item_id,
            "integration_state": "technical_failure" if recovery_exhausted else "pending",
            "accepted_content_hash": accepted_content_hash,
            "manifest_hash": manifest_hash,
            "manifest_ref": manifest_ref,
            "incident": dict(recorded),
        }

    # Positive naming for callers migrating away from failure-shaped APIs.
    record_integration_incident = mark_integration_failed

    def technical_failure(self, reason: str, *, recovery_exhausted: bool) -> AcceptedSnapshot:
        """Linearize technical terminalization against all item writers."""

        with self._state_transition_lock():
            self._reload_authoritative_for_terminal_transition_locked()
            return self._technical_failure_unlocked(reason, recovery_exhausted=recovery_exhausted)

    def _technical_failure_unlocked(self, reason: str, *, recovery_exhausted: bool) -> AcceptedSnapshot:
        """Publish a terminal workflow failure only after recovery exhaustion."""

        if recovery_exhausted is not True:
            raise ValueError("technical_failure requires recovery_exhausted=True")
        self._ensure_execution_state()
        self._reconcile_business_review_discard()
        if self.accepted_root.exists() or self.accepted_root.is_symlink():
            # Replaying the same terminalization is an idempotent durable
            # operation.  A competing reason still fails closed rather than
            # rewriting an immutable outcome.
            if self._state.get("lifecycle_state") == "technical_failure":
                try:
                    _snapshot, manifest = self._read_valid_terminal_snapshot()
                except Exception:
                    raise FileExistsError(self.accepted_root)
                if manifest.get("outcome") == "technical_failure" and manifest.get("reason") == str(reason):
                    return _snapshot
            raise FileExistsError(self.accepted_root)
        self._ensure_not_terminal()
        reason = str(reason)
        if not reason:
            raise ValueError("technical_failure reason must be non-empty")
        # A requirement-scoped transport failure may arrive while its role
        # attempt is still active.  Close that attempt as part of the same
        # item transition so the capacity claim is released before Planner is
        # read again.  This is deliberately item-local and does not alter any
        # run-level retry accounting.
        active_attempt_id = self._state.get("active_attempt_id")
        if active_attempt_id is not None:
            index, record = self._active_record(str(active_attempt_id))
            state = dict(self._state)
            state["attempts"] = [dict(item) for item in self._state["attempts"]]
            state["attempts"][index] = dict(record)
            state["attempts"][index]["status"] = "failed"
            state["attempts"][index]["error"] = "recovery_exhausted"
            state["active_attempt_id"] = None
            state["lifecycle_state"] = "work"
            self._persist_state(state)
        self._require_no_active_attempt()
        # Retain reviewer findings as historical evidence, but make the
        # consumed repair authority inactive before publishing the terminal
        # snapshot.  This prevents stale repair metadata from participating in
        # a later terminal reload or next-item transition.
        self._deactivate_active_business_repair()
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
        from .requirement_planning import RequirementSupervisorWorkspace

        incident_id = "INC-" + _sha256_bytes(
            _json_bytes(
                {
                    "kind": "requirement_recovery_exhausted",
                    "item_id": self.item_id,
                    "content_hash": content_hash,
                    "manifest_hash": manifest["manifest_hash"],
                }
            )
        )[:24]
        RequirementSupervisorWorkspace(self.context).record_incident(
            IncidentRecord(
                incident_id=incident_id,
                category="recovery",
                disposition="terminal_item_failure",
                admissible=False,
                item_id=self.item_id,
                scope=("requirement", self.item_id),
                source="item_workspace",
                facts={
                    "failure_class": "requirement_action",
                    "reason_hash": _sha256_bytes(reason.encode("utf-8")),
                    "recovery_exhausted": True,
                    "terminal_outcome": "technical_failure",
                },
            )
        )
        self._emit(
            "item_technical_failure",
            outcome="technical_failure",
            reason=reason,
            manifest_path=str(manifest_path),
            content_hash=content_hash,
        )
        return snapshot

    def finalize_blocked_by_evidence(self) -> AcceptedSnapshot:
        """Publish an immutable terminal snapshot for owner data insufficiency.

        The transition is authorized only by an explicit Analytical Owner
        conclusion bound to the current draft/artifact progress and an
        independent reviewer ``confirm_data_insufficiency`` verdict.  No
        accepted answer content or integration bundle is produced.
        """

        with self._state_transition_lock():
            # A caller-held workspace is not an authorization token.  Reload
            # under the shared lock so an attempt, terminal intent, or review
            # update committed by another workspace cannot be overwritten.
            state_path = self._resolve_item_subpath(_STATE_FILENAME)
            authoritative_state = self._read_state(state_path)
            self._validate_state(
                authoritative_state,
                item_id=self.item_id,
                mode=self.mode,
                original_text=self.original_text,
            )
            self._state = authoritative_state
            self._ensure_execution_state()
            self._reconcile_business_review_discard()
            if self.accepted_root.exists() or self.accepted_root.is_symlink():
                raise FileExistsError(self.accepted_root)
            self._ensure_not_terminal()
            self._require_no_active_attempt()
            if self._state.get("terminal_outcome") is not None or self._state.get("terminal_intent") is not None:
                raise ValueError("blocked review finalization requires a non-terminal item")
            if self._state.get("lifecycle_state") != "review":
                raise ValueError("blocked_by_evidence finalization requires lifecycle_state=review")
            if self._state.get("integration_state") != "pending":
                raise ValueError("blocked_by_evidence finalization requires pending integration")

            review = self._state.get("review")
            if not isinstance(review, Mapping):
                raise ValueError("blocked_by_evidence finalization requires review metadata")
            if review.get("status") != "reviewed" or review.get("verdict") != _CONFIRM_DATA_VERDICT:
                raise ValueError("blocked_by_evidence finalization requires reviewer data-insufficiency confirmation")

            draft_path = self.draft_root
            _assert_regular_no_symlink(draft_path, label="draft artifact")
            if not draft_path.is_file():
                raise FileNotFoundError(draft_path)
            draft_bytes = draft_path.read_bytes()
            draft_hash = _sha256_bytes(draft_bytes)
            if review.get("draft_hash") != draft_hash:
                raise ValueError("blocked_by_evidence finalization requires the exact currently reviewed draft")

            conclusion = self._read_data_insufficiency_conclusion()
            if conclusion is None:
                raise ValueError("blocked_by_evidence finalization requires an owner conclusion")
            conclusion = self._validate_data_insufficiency_binding(conclusion, require_current=True)
            conclusion_path = self.data_insufficiency_path
            _assert_regular_no_symlink(conclusion_path, label="data insufficiency conclusion")
            conclusion_bytes = conclusion_path.read_bytes()

            review_path = self.business_review_path
            _assert_regular_no_symlink(review_path, label="business review artifact")
            if not review_path.is_file():
                raise FileNotFoundError(review_path)
            review_bytes = review_path.read_bytes()
            try:
                packet = json.loads(review_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("business review artifact is invalid") from exc
            if not isinstance(packet, Mapping):
                raise ValueError("business review artifact must be an object")
            self._validate_business_review_payload(packet)
            if packet.get("review_scope") not in {"full", "targeted"}:
                raise ValueError("blocked_by_evidence finalization requires a completed review packet")
            if packet.get("repair_active") is not False:
                raise ValueError("blocked_by_evidence finalization requires an inactive repair packet")
            findings = packet.get("findings")
            if findings != []:
                raise ValueError("blocked_by_evidence finalization cannot use reviewer findings as authority")
            if packet.get("reviewed_draft_hash") != draft_hash:
                raise ValueError("blocked_by_evidence finalization requires a packet bound to the draft")

            progress = self._artifact_progress(draft_bytes)
            unsigned = {
                "item_id": self.item_id,
                "outcome": _BLOCKED_REVIEW_OUTCOME,
                "reason": "data_insufficiency",
                "draft_path": "reviewed_draft.json",
                "draft_hash": draft_hash,
                "source_draft_path": _DRAFT_FILENAME,
                "source_draft_hash": draft_hash,
                "business_review_path": "business_review.json",
                "business_review_hash": _sha256_bytes(review_bytes),
                "source_business_review_path": f"work/{_BUSINESS_REVIEW_FILENAME}",
                "source_business_review_hash": _sha256_bytes(review_bytes),
                "data_insufficiency_path": _DATA_INSUFFICIENCY_FILENAME,
                "data_insufficiency_hash": _sha256_bytes(conclusion_bytes),
                "source_data_insufficiency_path": f"work/{_DATA_INSUFFICIENCY_FILENAME}",
                "source_data_insufficiency_hash": _sha256_bytes(conclusion_bytes),
                "review_status": str(review.get("status")),
                "review_strength": review.get("strength"),
                "review_verdict": str(review.get("verdict")),
                "reviewer_ref": review.get("reviewer_ref"),
                "review_scope": packet["review_scope"],
                "targeted_recheck": packet["targeted_recheck"],
                "repair_active": packet["repair_active"],
                "reviewed_draft_hash": packet["reviewed_draft_hash"],
                "finding_count": len(findings),
                "hashes": dict(progress.hashes),
                "artifact_progress": progress.to_dict(),
                "refs": [
                    _DRAFT_FILENAME,
                    f"work/{_BUSINESS_REVIEW_FILENAME}",
                    f"work/{_DATA_INSUFFICIENCY_FILENAME}",
                ],
            }
            # ``content_hash`` retains the terminal snapshot convention used
            # by accepted items: for this no-answer outcome the immutable
            # reviewed draft is the canonical content, while the business
            # review copy has its own explicit hash.
            content_hash = draft_hash
            manifest = {**unsigned, "content_hash": content_hash}
            manifest["manifest_hash"] = _manifest_hash(manifest)

            intent_state = copy.deepcopy(self._state)
            intent_state["terminal_intent"] = {
                "outcome": _BLOCKED_REVIEW_OUTCOME,
                "manifest_hash": manifest["manifest_hash"],
            }
            self._persist_state(intent_state, _lock_held=True)
            manifest_path = self._publish_accepted_directory(
                {
                    "reviewed_draft.json": draft_bytes,
                    "business_review.json": review_bytes,
                    _DATA_INSUFFICIENCY_FILENAME: conclusion_bytes,
                },
                manifest,
            )
            snapshot = AcceptedSnapshot(self.item_id, _BLOCKED_REVIEW_OUTCOME, str(manifest_path), content_hash)
            state = copy.deepcopy(self._state)
            state["lifecycle_state"] = _BLOCKED_REVIEW_OUTCOME
            state["terminal_outcome"] = {"status": _BLOCKED_REVIEW_OUTCOME, **snapshot.to_dict()}
            self._persist_state(state, _lock_held=True)
            self._emit(
                "item_blocked_by_evidence",
                outcome=_BLOCKED_REVIEW_OUTCOME,
                manifest_path=str(manifest_path),
                content_hash=content_hash,
                finding_count=len(findings),
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
