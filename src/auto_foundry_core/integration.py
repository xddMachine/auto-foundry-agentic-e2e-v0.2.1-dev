"""Mechanical integration of an accepted analytical result.

The integration agent supplies typed records and explicit evidence.  This
module persists those records, verifies their local identity, and applies them
through the public prepared-registry and Living Enterprise Model APIs.  It
never parses answer prose, recalculates metrics, launches a model, or infers
semantic relationships.  Mechanical validation cannot prove semantic
completeness; a live Integration Agent and an external test-only fidelity
audit are required for that judgment.  There is deliberately no prose parser,
semantic compiler, or Integration Reviewer hidden in this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import shutil
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - POSIX is used on supported hosts
    import fcntl
except ImportError:  # pragma: no cover - defensive fallback
    fcntl = None  # type: ignore[assignment]

from .contracts import (
    CanonicalMapping,
    IncidentRecord,
    IdentityDecision,
    KnowledgeDelta,
    LEMRef,
    OntologyItem,
    PreparedAssetDescriptor,
)
from .analytical_artifacts import (
    ANALYTICAL_ARTIFACT_TYPES,
    AnalyticalArtifact,
    AnalyticalArtifactValidationError,
)
from .durable import _atomic_write_bytes, _atomic_write_json, _json_bytes, _sha256_bytes
from .enterprise_model import LivingEnterpriseModel
from .analyst_workspace import (
    AnalyticalRelationshipEvidence,
    _canonical_hash as _semantic_selection_journal_hash,
    validate_analytical_relationship_measurement,
)
from .lem_projection import LEMProjection, LivingEnterpriseModelProjector
from .integration_review import (
    FidelityFinding,
    FidelityRepairAuthorization,
    FidelityRepairProgress,
    FidelityResult,
    IntegrationFidelityPacket,
    _digest as _fidelity_digest,
    write_packet,
    write_repair_authorization,
    write_repair_progress,
    write_result,
)
from .lifecycle import RunLifecycle
from .prepared import PreparedAssetRegistry, _new_registry_commit_authority
from .semantic_store import SemanticSnapshotRef, SemanticSnapshotStore
from .workspace import AllowedRootError, RunContext


_INTEGRATION_ROOT = "integration"
_STAGING_DIR = "staging"
_COMMITTED_DIR = "committed"
_TECHNICAL_FAILURE_DIR = "technical_failure"
_RECORDS_FILENAME = "records.jsonl"
_SESSION_FILENAME = "session.json"
_INTENT_FILENAME = "commit_intent.json"
_SNAPSHOT_FILENAME = "snapshot.json"
_MANIFEST_FILENAME = "manifest.json"
_INVOCATION_LOCK_FILENAME = ".invocation.lock"
_FIDELITY_DIR = "review"
_FIDELITY_PACKET_FILENAME = "packet.json"
_FIDELITY_RESULT_FILENAME = "result.json"
_FIDELITY_AUTHORIZATION_FILENAME = "repair_authorization.json"
_FIDELITY_PROGRESS_FILENAME = "repair_progress.json"
_FIDELITY_REMOVAL_INTENT_FILENAME = "removal_intent.json"
_FIDELITY_REMOVAL_PHASES = frozenset({"prepared", "records_persisted", "progress_persisted"})
_UNREVIEWED_REMOVED_RECORD_HASHES = "unreviewed_removed_record_hashes"
_SCHEMA_VERSION = "1"
_RECORD_KINDS = frozenset(
    {
        "claim",
        "metric",
        "limitation",
        "evidence_link",
        "prepared_asset",
        "ontology_item",
        "relationship",
        "identity_decision",
        "canonical_mapping",
        "knowledge_delta",
        "dashboard_fact",
        "analytical_artifact",
    }
)

# ``analytical_artifact`` is a record-only integration output.  The typed
# artifact remains the authority for analytical content; this small envelope
# binds it to the accepted requirement and to the immutable committed copy.
# Keep the field names explicit so a future renderer can consume the artifact
# without mining answer prose or trusting a work/raw path.
_ANALYTICAL_ARTIFACT_PAYLOAD_FIELDS = frozenset(
    {
        "artifact",
        "artifact_id",
        "artifact_type",
        "schema_version",
        "requirement_id",
        "content_hash",
        "envelope_hash",
        "canonical_bytes_sha256",
        "artifact_ref",
    }
)
_ANALYTICAL_ARTIFACT_REF_PREFIX = "integration/committed/artifacts/"
_ANALYTICAL_OUTPUT_DESCRIPTOR_FIELDS = frozenset(
    {
        "path",
        "format",
        "sha256",
        "size_bytes",
        "row_count",
        "complete",
    }
)

# A relationship may be analytically publishable for more than one
# requirement without introducing another ontology edge.  The marker remains
# on the typed ``relationship`` record so the analytical artifact can bind the
# requirement-local analysis row, while the referenced committed LEM edge is
# the sole ontology authority.
_RELATIONSHIP_REUSE_FIELD = "reuse_existing_relationship_id"
_RELATIONSHIP_REUSE_IGNORED_FIELDS = frozenset(
    {
        "relationship_id",
        "item_id",
        "id",
        "analysis_relationship_id",
        "scope",
        "owner_ref",
        "audit_id",
        "evidence_refs",
        _RELATIONSHIP_REUSE_FIELD,
    }
)
_SESSION_STATES = frozenset({"open", "committed", "technical_failure"})
_SHA256_HEX = frozenset("0123456789abcdef")
_SEMANTIC_SELECTION_PREFIX = "semantic_store/selections/"
_ANALYSIS_CONTEXT_REF = "work/analysis_context.json"
_SEMANTIC_SELECTIONS_REF = "work/semantic_selections.jsonl"
_SEMANTIC_SELECTION_COUNT_NAMES = frozenset(
    {
        "ontology_ids",
        "relationship_ids",
        "identity_decision_ids",
        "mapping_ids",
        "prepared_asset_ids",
    }
)

_LEASES_LOCK = threading.RLock()
_HELD_LEASES: dict[str, dict[str, Any]] = {}


class _ProcessLease:
    """Reference-counted process-local wrapper around a POSIX advisory lock."""

    def __init__(self, path: Path, stream: Any, *, shared: bool = False) -> None:
        self.path = path
        self._stream = stream
        self._shared = shared
        self._released = False

    @classmethod
    def acquire(cls, path: Path, owner_id: str, invocation_id: str) -> "_ProcessLease":
        key = str(path.resolve())
        with _LEASES_LOCK:
            held = _HELD_LEASES.get(key)
            if held is not None:
                if held["owner_id"] == owner_id and held["invocation_id"] == invocation_id:
                    held["refs"] += 1
                    return cls(path, held["stream"], shared=True)
                raise ValueError("integration item is already leased by another process/invocation")
            _assert_no_symlink(path, label="integration invocation lease")
            stream = path.open("a+b")
            try:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                stream.close()
                raise ValueError("integration item is already leased by another process/invocation") from exc
            _HELD_LEASES[key] = {"stream": stream, "owner_id": owner_id, "invocation_id": invocation_id, "refs": 1}
            return cls(path, stream)

    def release(self) -> None:
        if self._released:
            return
        key = str(self.path.resolve())
        with _LEASES_LOCK:
            held = _HELD_LEASES.get(key)
            self._released = True
            if held is None:
                return
            held["refs"] -= 1
            if held["refs"] > 0:
                return
            _HELD_LEASES.pop(key, None)
            stream = held["stream"]
            try:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    close = release


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_invocation_id(value: Any) -> str:
    if value is None:
        raise TypeError("invocation_id is required")
    return _safe_component(value, "invocation_id")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        raise TypeError("integration payloads cannot contain opaque bytes")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return _json_bytes(_jsonable(value))


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _analysis_context_manifest_hash(value: Any) -> str:
    """Hash the v3 analysis context's canonical bytes (without JSONL newline).

    ``BoundAnalysisContext`` predates the durable JSONL helper used by this
    module and deliberately hashes its compact JSON object without the
    trailing newline that is written to disk.  Keep that established
    contract explicit rather than silently applying the integration record
    hash convention here.
    """

    return _sha256_bytes(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _SHA256_HEX for char in value)


def _copy_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(copy.deepcopy(dict(value)))


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze JSON-shaped accepted metadata for in-memory use."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _deep_freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _deep_freeze(dict(value))


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


def _assert_no_symlink(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise AllowedRootError(f"{label} cannot be a symlink: {path}")
    if path.exists() and not (path.is_file() or path.is_dir()):
        raise ValueError(f"{label} is not a regular file or directory: {path}")


def _safe_component(value: Any, label: str) -> str:
    result = str(value).strip()
    if (
        not result
        or result in {".", ".."}
        or Path(result).name != result
        or "\\" in result
        or "\x00" in result
    ):
        raise ValueError(f"{label} must be a simple path component")
    return result


def _safe_relative_ref(value: Any, label: str = "reference") -> str:
    result = str(value).strip()
    path = PurePath(result)
    if (
        not result
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in result
        or "\\" in result
    ):
        raise ValueError(f"{label} must be a safe run-relative reference")
    return path.as_posix()


def _analytical_artifact_bytes(artifact: AnalyticalArtifact) -> bytes:
    """Return the exact canonical bytes persisted for one analytical artifact.

    ``AnalyticalArtifact.to_json`` is already the artifact contract's
    canonical serialization (sorted keys, finite JSON, no observational
    newline).  Persisting those bytes verbatim means the record can bind both
    the typed content hashes and the actual file bytes without any renderer or
    integration-side reserialization differences.
    """

    return artifact.to_json().encode("utf-8")


def _analytical_artifact_ref(artifact_id: str, *, existing_ids: Iterable[str] = ()) -> str:
    """Build a safe committed artifact reference from an artifact identity.

    Simple IDs retain a readable ``<safe-id>.json`` filename.  If a second
    artifact identity normalizes to the same filename, append a deterministic
    digest of the original ID so distinct identities cannot alias one file.
    """

    safe = _slug_record_component(artifact_id)
    existing = tuple(str(value) for value in existing_ids)
    if safe != artifact_id or any(value != artifact_id and _slug_record_component(value) == safe for value in existing):
        suffix = hashlib.sha256(str(artifact_id).encode("utf-8")).hexdigest()[:12]
        safe = f"{safe}-{suffix}"
    return f"{_ANALYTICAL_ARTIFACT_REF_PREFIX}{safe}.json"


def _parse_analytical_artifact_payload(
    payload: Mapping[str, Any],
    *,
    expected_item_id: str | None = None,
    require_committed_ref: bool = True,
) -> tuple[AnalyticalArtifact, bytes]:
    """Validate and parse one canonical analytical-artifact record payload."""

    if not isinstance(payload, Mapping) or set(payload) != _ANALYTICAL_ARTIFACT_PAYLOAD_FIELDS:
        raise ValueError("analytical artifact integration payload fields are invalid")
    raw_artifact = payload.get("artifact")
    if not isinstance(raw_artifact, Mapping):
        raise ValueError("analytical artifact payload artifact is invalid")
    try:
        artifact = AnalyticalArtifact.from_dict(raw_artifact)
    except (AnalyticalArtifactValidationError, TypeError, ValueError) as exc:
        raise ValueError("analytical artifact payload artifact is invalid") from exc
    if payload.get("artifact_id") != artifact.artifact_id:
        raise ValueError("analytical artifact payload artifact_id does not match artifact")
    if payload.get("artifact_type") != artifact.artifact_type:
        raise ValueError("analytical artifact payload artifact_type does not match artifact")
    if payload.get("schema_version") != artifact.schema_version:
        raise ValueError("analytical artifact payload schema_version does not match artifact")
    if payload.get("requirement_id") != artifact.requirement_id:
        raise ValueError("analytical artifact payload requirement_id does not match artifact")
    if expected_item_id is not None and artifact.requirement_id != expected_item_id:
        raise ValueError("analytical artifact requirement_id does not match integration item")
    if payload.get("content_hash") != artifact.content_hash:
        raise ValueError("analytical artifact payload content_hash does not match artifact")
    if payload.get("envelope_hash") != artifact.envelope_hash:
        raise ValueError("analytical artifact payload envelope_hash does not match artifact")
    canonical_bytes = _analytical_artifact_bytes(artifact)
    canonical_hash = payload.get("canonical_bytes_sha256")
    if not _is_sha256(canonical_hash) or canonical_hash != _sha256_bytes(canonical_bytes):
        raise ValueError("analytical artifact payload canonical bytes hash does not match artifact")
    artifact_ref = payload.get("artifact_ref")
    if require_committed_ref:
        if not isinstance(artifact_ref, str) or not artifact_ref.startswith(_ANALYTICAL_ARTIFACT_REF_PREFIX) or not artifact_ref.endswith(".json"):
            raise ValueError("analytical artifact committed reference is invalid")
        relative = _safe_relative_ref(artifact_ref, "analytical artifact reference")
        if relative != artifact_ref or PurePath(relative).parent.as_posix() != _ANALYTICAL_ARTIFACT_REF_PREFIX.rstrip("/"):
            raise ValueError("analytical artifact committed reference is invalid")
        filename = PurePath(relative).name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", filename):
            raise ValueError("analytical artifact committed reference is invalid")
    elif artifact_ref is not None:
        # Staging/fidelity records still carry the future reference; this
        # branch is reserved for compatibility with a diagnostic parser.
        _safe_relative_ref(artifact_ref, "analytical artifact reference")
    return artifact, canonical_bytes


def _analytical_output_relative_path(value: Any, *, label: str) -> str:
    """Validate one output descriptor path relative to its admitted root.

    Analytical toolkit output descriptors are intentionally root-relative.  A
    descriptor must never be interpreted as a generic run path: absolute paths,
    parent traversal, alternate separators, and dot components are all
    rejected before any filesystem resolution.  The ``work/`` prefix is not a
    valid alias here; callers pass paths relative to the explicit output root.
    """

    raw = value.get("path") if isinstance(value, Mapping) else None
    if (
        not isinstance(raw, str)
        or not raw
        or raw != raw.strip()
        or "\\" in raw
        or "\x00" in raw
        or raw.startswith("~")
        or re.match(r"^[A-Za-z]:", raw)
    ):
        raise ValueError(f"{label} path is invalid")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] in {"work", "accepted", "integration", "questions", "requirements"}
    ):
        raise ValueError(f"{label} path is invalid")
    return path.as_posix()


def _jsonl_row_count(payload: bytes, *, label: str) -> int:
    """Validate bounded JSONL syntax and return its exact row count."""

    if not payload:
        return 0
    rows = payload.splitlines()
    for index, line in enumerate(rows, 1):
        if not line.strip():
            raise ValueError(f"{label} contains an empty JSONL row at line {index}")
        try:
            json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} contains invalid JSONL at line {index}") from exc
    return len(rows)


def _stream_jsonl_file(
    path: Path,
    *,
    label: str,
    sink: Any | None = None,
) -> tuple[str, int, int]:
    """Hash, copy (optionally), and validate JSONL in one bounded pass.

    The output file is intentionally not read with ``Path.read_bytes``.  The
    buffered binary iterator retains one JSONL line while computing the exact
    byte count and row count required by the sealed descriptor.  There is no
    arbitrary file or row-size cap: memory is proportional only to the row
    currently being parsed.  A caller may provide a binary sink when
    publishing a validated copy; the same bytes are hashed, parsed, and
    written without a second whole-file buffer.
    """

    digest = hashlib.sha256()
    observed_size = 0
    rows = 0

    def consume(raw_line: bytes) -> None:
        nonlocal rows
        # JSONL permits a CRLF terminator; do not treat the CR as part of the
        # JSON value.  Blank lines remain invalid so row_count is unambiguous.
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        if not raw_line.strip():
            raise ValueError(f"{label} contains an empty JSONL row at line {rows + 1}")
        try:
            json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} contains invalid JSONL at line {rows + 1}") from exc
        rows += 1

    try:
        with path.open("rb") as stream:
            for raw_line in stream:
                digest.update(raw_line)
                observed_size += len(raw_line)
                if sink is not None:
                    sink.write(raw_line)
                consume(raw_line)
    except OSError as exc:
        raise ValueError(f"{label} output cannot be read") from exc
    return digest.hexdigest(), observed_size, rows


def _validate_analytical_output_descriptor(
    descriptor: Any,
    *,
    output_root: Path,
    accepted_hashes: Mapping[str, Any] | None,
    label: str,
) -> tuple[str, Path]:
    """Re-verify one external analytical output against a sealed root.

    ``accepted_hashes`` is the immutable item artifact-progress map during
    staging/fidelity.  On committed reload the copied output lives under the
    committed artifact root, so the record's descriptor hash is the authority
    and no accepted-work map is needed.  Both paths retain the same lexical
    path, symlink, SHA-256, byte-size, and JSONL row-count checks.
    """

    if not isinstance(descriptor, Mapping):
        raise ValueError(f"{label} descriptor is invalid")
    missing = _ANALYTICAL_OUTPUT_DESCRIPTOR_FIELDS - set(descriptor)
    if missing:
        raise ValueError(f"{label} descriptor is missing fields: {sorted(missing)!r}")
    relative = _analytical_output_relative_path(descriptor, label=label)
    if descriptor.get("format") != "jsonl":
        raise ValueError(f"{label} format must be jsonl")
    expected_hash = descriptor.get("sha256")
    if not _is_sha256(expected_hash):
        raise ValueError(f"{label} sha256 is invalid")
    size_bytes = descriptor.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError(f"{label} size_bytes is invalid")
    row_count = descriptor.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise ValueError(f"{label} row_count is invalid")
    if descriptor.get("complete") is not True:
        raise ValueError(f"{label} complete must be true")

    root = Path(output_root)
    _assert_no_symlink(root, label=f"{label} output root")
    root_resolved = root.resolve(strict=False)
    current = root
    for component in PurePosixPath(relative).parts:
        current = current / component
        _assert_no_symlink(current, label=f"{label} output")
    candidate = root / Path(relative)
    try:
        if candidate.resolve(strict=False).relative_to(root_resolved) != Path(relative):
            raise ValueError(f"{label} path escapes output root")
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} path escapes output root") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"{label} output is missing or not a regular file")

    if accepted_hashes is not None:
        sealed_ref = f"work/{relative}"
        sealed_hash = accepted_hashes.get(sealed_ref)
        if not _is_sha256(sealed_hash) or sealed_hash != expected_hash:
            raise ValueError(f"{label} output is not bound by accepted artifact progress")

    observed_hash, observed_size, observed_rows = _stream_jsonl_file(candidate, label=label)
    if observed_size != size_bytes:
        raise ValueError(f"{label} size_bytes does not match output")
    if observed_hash != expected_hash:
        raise ValueError(f"{label} sha256 does not match output")
    if observed_rows != row_count:
        raise ValueError(f"{label} row_count does not match JSONL output")
    return relative, candidate


def _validate_analytical_output_refs(
    artifact: AnalyticalArtifact,
    *,
    output_root: Path,
    accepted_hashes: Mapping[str, Any] | None,
    label: str,
) -> tuple[tuple[str, bytes], ...]:
    """Verify every external output declared by one analytical artifact."""

    refs = artifact.output_refs
    if not isinstance(refs, (tuple, list)):
        raise ValueError(f"{label} output_refs are invalid")
    checked: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for index, descriptor in enumerate(refs, 1):
        relative, candidate = _validate_analytical_output_descriptor(
            descriptor,
            output_root=output_root,
            accepted_hashes=accepted_hashes,
            label=f"{label} output_ref[{index}]",
        )
        if relative in seen:
            raise ValueError(f"{label} output_refs contain duplicate paths: {relative}")
        seen.add(relative)
        checked.append((relative, candidate))
    return tuple(checked)


def _validate_record_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("integration record_id must be a string")
    result = value.strip()
    if (
        not result
        or result != value
        or Path(result).name != result
        or "\\" in result
        or "\x00" in result
        or "\n" in result
        or "\r" in result
    ):
        raise ValueError("integration record_id is invalid")
    if len(result) > 256:
        raise ValueError("integration record_id is too long")
    return result


def validate_record_id(value: Any) -> str:
    """Validate one caller-supplied integration record identifier."""

    return _validate_record_id(value)


def _slug_record_component(value: Any) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip()).strip("-.")
    return result or "record"


def deterministic_record_id(kind: str, semantic_key: Any, raw_key: Any = None) -> str:
    """Build a collision-safe deterministic ID from semantic and raw keys.

    The readable semantic slug is supplemented with a digest of the exact raw
    key, so values that normalize to the same slug remain distinct while exact
    retries produce the same ID.
    """

    kind_slug = _slug_record_component(kind)
    semantic_slug = _slug_record_component(semantic_key)
    raw_digest = hashlib.sha256(_canonical_bytes(raw_key)).hexdigest()[:16]
    return _validate_record_id(f"{kind_slug}-{semantic_slug}-{raw_digest}")


# Descriptive aliases keep the helper discoverable for callers using either
# the implementation or domain vocabulary.
collision_safe_record_id = deterministic_record_id
make_record_id = deterministic_record_id


def _validate_scope(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("integration scope must be a string")
    result = value.strip()
    if not result or result != value or "\x00" in result or "\n" in result or "\r" in result:
        raise ValueError("integration scope is invalid")
    if len(result) > 256:
        raise ValueError("integration scope is too long")
    return result


def _validate_evidence_ref(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("evidence reference must be a string")
    result = value.strip()
    if not result or result != value:
        raise ValueError("evidence reference is invalid")
    # References may point at a run-relative artifact or a staged record ID.
    # Both forms are deliberately bounded and cannot escape the item root.
    if "/" in result or result in {".", ".."}:
        return _safe_relative_ref(result, "evidence reference")
    if "\\" in result or "\x00" in result or "\n" in result or "\r" in result:
        raise ValueError("evidence reference is invalid")
    return result


@dataclass(frozen=True)
class AcceptedAnalysisBundle:
    """Immutable accepted bytes plus independently validated metadata."""

    item_id: str
    outcome: str
    answer_content: bytes
    content_hash: str
    acceptance_envelope: Mapping[str, Any]
    envelope_hash: str
    manifest: Mapping[str, Any]
    manifest_hash: str

    @property
    def content(self) -> bytes:
        return self.answer_content

    @property
    def answer_content_bytes(self) -> bytes:
        return self.answer_content

    @property
    def accepted_refs(self) -> tuple[str, ...]:
        return tuple(self.acceptance_envelope.get("accepted_refs", ()))

    @property
    def analytical_artifact_handoff(self) -> tuple[Mapping[str, Any], ...]:
        """Return the immutable, manifest-bound typed artifact descriptors."""

        handoff = self.acceptance_envelope.get("analytical_artifact_handoff")
        if not isinstance(handoff, Mapping):
            return ()
        artifacts = handoff.get("artifacts", ())
        if not isinstance(artifacts, (list, tuple)):
            return ()
        return tuple(item for item in artifacts if isinstance(item, Mapping))

    # Descriptive aliases keep callers independent of the envelope's wire
    # field name while preserving one canonical sealed representation.
    @property
    def accepted_analytical_artifacts(self) -> tuple[Mapping[str, Any], ...]:
        return self.analytical_artifact_handoff

    @property
    def typed_analytical_artifacts(self) -> tuple[Mapping[str, Any], ...]:
        return self.analytical_artifact_handoff

    @classmethod
    def load(cls, item_workspace: Any) -> "AcceptedAnalysisBundle":
        """Load and validate one immutable accepted item bundle."""

        if not hasattr(item_workspace, "accepted_root") or not hasattr(item_workspace, "item_id"):
            raise TypeError("AcceptedAnalysisBundle.load requires an ItemWorkspace")
        accepted = item_workspace.accepted_root
        _assert_no_symlink(accepted, label="accepted bundle")
        if not accepted.is_dir():
            raise ValueError("accepted bundle directory is missing")
        # The durable owner performs the exact terminal manifest, content,
        # envelope, and intent checks.  Calling it here keeps integration from
        # duplicating or weakening that boundary.
        try:
            _snapshot, manifest = item_workspace._read_valid_terminal_snapshot()
        except Exception as exc:
            raise ValueError("accepted analysis bundle is invalid") from exc
        try:
            # The accepted directory is immutable, but a self-consistent
            # replacement directory must still be bound to the durable state
            # intent that authorized publication.  Hash checks alone cannot
            # prove that binding.
            item_workspace._validate_preterminal_binding(manifest["outcome"], manifest)
        except Exception as exc:
            raise ValueError("accepted analysis bundle is not bound to terminal intent") from exc
        if manifest.get("outcome") not in {"accepted", "accepted_with_limits"}:
            raise ValueError("accepted analysis bundle requires accepted outcome")
        content_name = manifest.get("content_path")
        envelope_name = manifest.get("envelope_path")
        if content_name != "answer_content.json" or envelope_name != "acceptance_envelope.json":
            raise ValueError("accepted analysis bundle paths are invalid")
        content_path = accepted / content_name
        envelope_path = accepted / envelope_name
        _assert_no_symlink(content_path, label="accepted answer content")
        _assert_no_symlink(envelope_path, label="acceptance envelope")
        answer_content = content_path.read_bytes()
        if _sha256_bytes(answer_content) != manifest.get("content_hash"):
            raise ValueError("accepted answer content hash does not match manifest")
        envelope_bytes = envelope_path.read_bytes()
        envelope_hash = _sha256_bytes(envelope_bytes)
        if envelope_hash != manifest.get("envelope_hash"):
            raise ValueError("acceptance envelope hash does not match manifest")
        try:
            envelope = json.loads(envelope_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("acceptance envelope is invalid") from exc
        if not isinstance(envelope, Mapping) or envelope.get("item_id") != item_workspace.item_id:
            raise ValueError("acceptance envelope item identity is invalid")
        if envelope.get("content_hash") != manifest.get("content_hash") or envelope.get("draft_hash") != manifest.get("content_hash"):
            raise ValueError("acceptance envelope content hash is invalid")
        if _sha256_bytes(_canonical_bytes({key: value for key, value in manifest.items() if key != "manifest_hash"})) != manifest.get("manifest_hash"):
            raise ValueError("accepted manifest hash does not match content")
        if manifest.get("item_id") != item_workspace.item_id:
            raise ValueError("accepted manifest item identity is invalid")
        return cls(
            item_id=item_workspace.item_id,
            outcome=str(manifest["outcome"]),
            answer_content=bytes(answer_content),
            content_hash=str(manifest["content_hash"]),
            acceptance_envelope=_deep_freeze_mapping(envelope),
            envelope_hash=envelope_hash,
            manifest=_deep_freeze_mapping(manifest),
            manifest_hash=str(manifest["manifest_hash"]),
        )


@dataclass(frozen=True)
class CurrentObservationFact:
    """One measured current-state fact, explicitly not an ontology definition."""

    observation_id: str
    metric: str
    value: int | float | str
    unit: str
    population: str
    as_of: str
    date_authority: str
    evidence_refs: tuple[str, ...]
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("observation_id", "metric", "unit", "population", "as_of", "date_authority"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float, str)):
            raise TypeError("current observation value must be a number or string")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("current observation value must be finite")
        try:
            datetime.fromisoformat(self.as_of.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("current observation as_of must be an ISO date or timestamp") from exc
        refs = (self.evidence_refs,) if isinstance(self.evidence_refs, str) else tuple(self.evidence_refs)
        if not refs or any(not isinstance(value, str) or not value.strip() for value in refs):
            raise ValueError("current observation needs explicit evidence_refs")
        object.__setattr__(self, "evidence_refs", tuple(value.strip() for value in refs))
        limitations = (self.limitations,) if isinstance(self.limitations, str) else tuple(self.limitations)
        object.__setattr__(self, "limitations", tuple(str(value).strip() for value in limitations if str(value).strip()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "semantic_role": "current_observation_not_definition",
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "population": self.population,
            "as_of": self.as_of,
            "date_authority": self.date_authority,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class IntegrationRecord:
    record_id: str
    kind: str
    item_id: str
    accepted_content_hash: str
    scope: str
    evidence_refs: tuple[str, ...]
    evidence_hashes: Mapping[str, str]
    payload: Mapping[str, Any]
    record_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "item_id": self.item_id,
            "accepted_content_hash": self.accepted_content_hash,
            "scope": self.scope,
            "evidence_refs": list(self.evidence_refs),
            "evidence_hashes": dict(self.evidence_hashes),
            "payload": _jsonable(self.payload),
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IntegrationRecord":
        expected = {
            "record_id",
            "kind",
            "item_id",
            "accepted_content_hash",
            "scope",
            "evidence_refs",
            "evidence_hashes",
            "payload",
            "record_hash",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("integration record fields are invalid")
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in _RECORD_KINDS:
            raise ValueError("integration record kind is unknown")
        record_id = _validate_record_id(value.get("record_id"))
        item_id = _validate_record_id(value.get("item_id"))
        accepted_content_hash = value.get("accepted_content_hash")
        if not _is_sha256(accepted_content_hash):
            raise ValueError("integration record accepted_content_hash is invalid")
        scope = _validate_scope(value.get("scope"))
        raw_refs = value.get("evidence_refs")
        if not isinstance(raw_refs, list):
            raise ValueError("integration record evidence_refs must be a list")
        refs = tuple(_validate_evidence_ref(ref) for ref in raw_refs)
        if len(set(refs)) != len(refs):
            raise ValueError("integration record evidence_refs contain duplicates")
        raw_hashes = value.get("evidence_hashes")
        if not isinstance(raw_hashes, Mapping):
            raise ValueError("integration record evidence_hashes must be an object")
        hashes = dict(raw_hashes)
        if set(hashes) != set(refs) or any(not isinstance(key, str) or not _is_sha256(hash_value) for key, hash_value in hashes.items()):
            raise ValueError("integration record evidence_hashes are invalid")
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("integration record payload must be an object")
        try:
            json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("integration record payload is not JSON-safe") from exc
        if kind == "analytical_artifact":
            # Record deserialization is intentionally strict but does not
            # touch the filesystem: the committed reference is a future
            # destination at staging/fidelity time.  The session-level
            # validators bind it to this record's item and verify bytes on
            # committed reload.
            _parse_analytical_artifact_payload(payload)
        if kind == "knowledge_delta":
            try:
                delta = KnowledgeDelta.from_dict(payload)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("knowledge delta integration record payload is invalid") from exc
            if not delta.accepted:
                raise ValueError("knowledge delta integration record must be accepted")
            if delta.to_dict() != dict(payload):
                raise ValueError("knowledge delta integration record payload is not canonical")
            if tuple(delta.evidence_refs) != refs:
                raise ValueError("knowledge delta evidence_refs must match integration evidence_refs")
        record_hash = value.get("record_hash")
        if not _is_sha256(record_hash):
            raise ValueError("integration record hash is invalid")
        record = cls(
            record_id=record_id,
            kind=kind,
            item_id=item_id,
            accepted_content_hash=accepted_content_hash,
            scope=scope,
            evidence_refs=refs,
            evidence_hashes=hashes,
            payload=_copy_mapping(payload),
            record_hash=record_hash,
        )
        expected_hash = _sha256_value({key: item for key, item in record.to_dict().items() if key != "record_hash"})
        if record.record_hash != expected_hash:
            raise ValueError("integration record hash does not match content")
        return record


@dataclass(frozen=True)
class IntegrationValidation:
    valid: bool
    counts: Mapping[str, int]
    omissions: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "counts": dict(self.counts),
            "omissions": list(self.omissions),
            "errors": list(self.errors),
        }


class IntegrationSession:
    """One owner-controlled, resumable result-integration session."""

    def __init__(
        self,
        context: RunContext,
        item_workspace: Any,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        invocation_id: str,
        bundle: AcceptedAnalysisBundle,
        session_state: Mapping[str, Any],
        records: Sequence[IntegrationRecord],
        lease: _ProcessLease | None = None,
    ) -> None:
        self.context = context
        self.item_workspace = item_workspace
        self.lem_projection = LivingEnterpriseModelProjector.project(
            context,
            before_item_id=str(getattr(item_workspace, "item_id", "")),
        )
        self.lem = self.lem_projection.model
        self.prepared_registry = prepared_registry
        self.owner_id = _safe_component(owner_id, "owner_id")
        self.invocation_id = _safe_component(invocation_id, "invocation_id")
        self.bundle = bundle
        self._lease = lease
        self._state = dict(session_state)
        self._records = list(records)
        self._by_id = {record.record_id: record for record in self._records}

    @property
    def item_id(self) -> str:
        return self.bundle.item_id

    @property
    def session_id(self) -> str:
        return str(self._state["session_id"])

    @property
    def invocation(self) -> str:
        return self.invocation_id

    @property
    def status(self) -> str:
        return str(self._state["status"])

    @property
    def state(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    @property
    def records(self) -> tuple[IntegrationRecord, ...]:
        return tuple(self._records)

    @property
    def staging_root(self) -> Path:
        return self._staging_root(self.item_workspace)

    @property
    def committed_root(self) -> Path:
        return self._integration_root(self.item_workspace) / _COMMITTED_DIR

    @property
    def committed_manifest_path(self) -> Path:
        return self.committed_root / _MANIFEST_FILENAME

    @property
    def fidelity_root(self) -> Path:
        root = self._integration_root(self.item_workspace) / _FIDELITY_DIR
        self._ensure_safe_dir(root)
        return root

    @property
    def fidelity_packet_path(self) -> Path:
        return self.fidelity_root / _FIDELITY_PACKET_FILENAME

    @property
    def fidelity_result_path(self) -> Path:
        return self.fidelity_root / _FIDELITY_RESULT_FILENAME

    @property
    def fidelity_authorization_path(self) -> Path:
        return self.fidelity_root / _FIDELITY_AUTHORIZATION_FILENAME

    @property
    def fidelity_progress_path(self) -> Path:
        return self.fidelity_root / _FIDELITY_PROGRESS_FILENAME

    @property
    def fidelity_removal_intent_path(self) -> Path:
        return self.fidelity_root / _FIDELITY_REMOVAL_INTENT_FILENAME

    @classmethod
    def create(
        cls,
        context: RunContext,
        item_workspace: Any,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        invocation_id: str,
    ) -> "IntegrationSession":
        cls._validate_lifecycle_target(context, item_workspace)
        with cls._session_lock(item_workspace):
            return cls._create_unlocked(context, item_workspace, prepared_registry, owner_id, invocation_id)

    @classmethod
    def _create_unlocked(
        cls,
        context: RunContext,
        item_workspace: Any,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        invocation_id: str,
    ) -> "IntegrationSession":
        if not isinstance(context, RunContext):
            raise TypeError("IntegrationSession requires a RunContext")
        if getattr(item_workspace, "context", None) is not context:
            raise ValueError("item workspace must use the same RunContext")
        if not isinstance(prepared_registry, PreparedAssetRegistry) or prepared_registry.context is not context:
            raise ValueError("prepared registry must use the same RunContext")
        owner_id = _safe_component(owner_id, "owner_id")
        bundle = AcceptedAnalysisBundle.load(item_workspace)
        invocation_id = _normalize_invocation_id(invocation_id)
        integration_state = getattr(item_workspace, "integration_state", item_workspace.state.get("integration_state"))
        if integration_state == "technical_failure":
            raise ValueError("item integration is terminal technical_failure")
        staging_root = cls._staging_root(item_workspace)
        existing_state_path = staging_root / _SESSION_FILENAME
        existing_snapshot_path = staging_root / _SNAPSHOT_FILENAME
        if (
            existing_state_path.exists()
            or existing_state_path.is_symlink()
            or existing_snapshot_path.exists()
            or existing_snapshot_path.is_symlink()
        ):
            session = cls._load_existing(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle)
            if session.status == "committed":
                return session
            return session
        committed = cls._committed_manifest(item_workspace)
        if committed is not None:
            if integration_state not in {"pending", "integrated"}:
                raise ValueError("committed integration conflicts with item state")
            session = cls._load_committed(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle, committed)
            return session
        if integration_state != "pending":
            raise ValueError("IntegrationSession requires pending item integration state")
        staging = staging_root
        cls._ensure_safe_dir(staging)
        lease = cls._acquire_invocation_lease(item_workspace, owner_id, invocation_id)
        session_id = "IS-" + _sha256_value({"item_id": bundle.item_id, "owner_id": owner_id, "content_hash": bundle.content_hash})[:24]
        now = _now()
        state = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": session_id,
            "item_id": bundle.item_id,
            "owner_id": owner_id,
            "invocation_id": invocation_id,
            "status": "open",
            "accepted_content_hash": bundle.content_hash,
            "accepted_manifest_hash": bundle.manifest_hash,
            "records_count": 0,
            "records_hash": _sha256_bytes(b""),
            "created_at": now,
            "updated_at": now,
        }
        state["state_hash"] = _sha256_value(state)
        try:
            cls._write_staging_snapshot(staging, state, ())
        except Exception:
            lease.release()
            raise
        session = cls(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle, state, (), lease)
        try:
            session._auto_stage_sealed_analytical_artifacts()
        except Exception:
            # Acceptance has already committed the business result, so an
            # internal handoff/staging fault is retryable.  Leave the open
            # staging snapshot and pending item integration state intact for a
            # subsequent create/load attempt.
            lease.release()
            raise
        return session

    @classmethod
    def load(
        cls,
        context: RunContext,
        item_workspace: Any,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        invocation_id: str,
    ) -> "IntegrationSession":
        """Reload and validate the run-local staging or committed session."""
        cls._validate_lifecycle_target(context, item_workspace)
        with cls._session_lock(item_workspace):
            return cls._load_unlocked(context, item_workspace, prepared_registry, owner_id, invocation_id)

    @classmethod
    def _load_unlocked(
        cls,
        context: RunContext,
        item_workspace: Any,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        invocation_id: str,
    ) -> "IntegrationSession":
        if not isinstance(context, RunContext):
            raise TypeError("IntegrationSession requires a RunContext")
        if getattr(item_workspace, "context", None) is not context:
            raise ValueError("item workspace must use the same RunContext")
        if not isinstance(prepared_registry, PreparedAssetRegistry) or prepared_registry.context is not context:
            raise ValueError("prepared registry must use the same RunContext")
        bundle = AcceptedAnalysisBundle.load(item_workspace)
        owner_id = _safe_component(owner_id, "owner_id")
        invocation_id = _normalize_invocation_id(invocation_id)
        staging = cls._staging_root(item_workspace)
        if (staging / _SNAPSHOT_FILENAME).exists() or (staging / _SESSION_FILENAME).exists():
            return cls._load_existing(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle)
        committed = cls._committed_manifest(item_workspace)
        if committed is not None:
            return cls._load_committed(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle, committed)
        return cls._load_existing(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle)

    @classmethod
    def _load_existing(
        cls,
        context: RunContext,
        item_workspace: Any,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        invocation_id: str,
        bundle: AcceptedAnalysisBundle,
    ) -> "IntegrationSession":
        staging = cls._staging_root(item_workspace)
        state, records = cls._read_staging_snapshot(staging, bundle)
        if state["owner_id"] != owner_id:
            raise ValueError("integration staging is owned by another owner")
        if state.get("invocation_id") != invocation_id:
            raise ValueError("integration staging is owned by another invocation")
        lease = cls._acquire_invocation_lease(item_workspace, owner_id, invocation_id)
        session = cls(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle, state, records, lease)
        try:
            session._reconcile_fidelity_removal_unlocked()
            if state.get("status") == "open":
                session._auto_stage_sealed_analytical_artifacts()
        except Exception:
            lease.release()
            raise
        if state.get("status") == "committed":
            # A committed staging snapshot is a normal post-publication
            # recovery path.  Validate its artifact files before rebuilding
            # the projection; otherwise a missing/tampered/symlinked artifact
            # could be bypassed whenever the staging snapshot still exists.
            cls._validate_committed_artifacts(session.committed_root, records)
            session._reproject_current()
            session.release()
        return session

    @classmethod
    def _load_committed(
        cls,
        context: RunContext,
        item_workspace: Any,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        invocation_id: str,
        bundle: AcceptedAnalysisBundle,
        manifest: Mapping[str, Any],
    ) -> "IntegrationSession":
        if manifest.get("owner_id") != owner_id:
            raise ValueError("committed integration is owned by another owner")
        if manifest.get("invocation_id") != invocation_id:
            raise ValueError("committed integration is owned by another invocation")
        committed = cls._integration_root(item_workspace) / _COMMITTED_DIR
        records = cls._read_records(committed / _RECORDS_FILENAME, manifest, bundle)
        # Artifact files are part of the committed record contract.  Verify
        # them before projecting or reusing a committed session so missing,
        # tampered, and symlinked files fail closed on reload/recovery.
        cls._validate_committed_artifacts(committed, records)
        state = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": manifest["session_id"],
            "item_id": bundle.item_id,
            "owner_id": owner_id,
            "invocation_id": invocation_id,
            "status": "committed",
            "accepted_content_hash": bundle.content_hash,
            "accepted_manifest_hash": bundle.manifest_hash,
            "records_count": len(records),
            "records_hash": manifest["records_hash"],
            "created_at": manifest["created_at"],
            "updated_at": manifest["committed_at"],
            "state_hash": _sha256_value({
                "schema_version": _SCHEMA_VERSION,
                "session_id": manifest["session_id"],
                "item_id": bundle.item_id,
                "owner_id": owner_id,
                "invocation_id": invocation_id,
                "status": "committed",
                "accepted_content_hash": bundle.content_hash,
                "accepted_manifest_hash": bundle.manifest_hash,
                "records_count": len(records),
                "records_hash": manifest["records_hash"],
                "created_at": manifest["created_at"],
                "updated_at": manifest["committed_at"],
            }),
        }
        lease = cls._acquire_invocation_lease(item_workspace, owner_id, invocation_id)
        session = cls(context, item_workspace, prepared_registry, owner_id, invocation_id, bundle, state, records, lease)
        if item_workspace.integration_state == "pending":
            session._preflight_all()
            candidate = session._candidate_lem(committed_at=str(manifest["committed_at"]))
            session._apply_records()
            session._verify_candidate_projection(candidate)
            session._finish_committed(manifest)
        else:
            session._ensure_bundle_current()
            if item_workspace.integration_manifest_hash != manifest.get("manifest_hash"):
                raise ValueError("item integration state does not match committed manifest")
            candidate = session._candidate_lem(committed_at=str(manifest["committed_at"]))
            session._verify_candidate_projection(candidate)
            session._finish_committed(manifest)
        return session

    @staticmethod
    def _integration_root(item_workspace: Any) -> Path:
        root = item_workspace.item_root / _INTEGRATION_ROOT
        _assert_no_symlink(root, label="integration root")
        return root

    @classmethod
    def _staging_root(cls, item_workspace: Any) -> Path:
        root = cls._integration_root(item_workspace) / _STAGING_DIR
        _assert_no_symlink(root, label="integration staging")
        return root

    @classmethod
    def _acquire_invocation_lease(cls, item_workspace: Any, owner_id: str, invocation_id: str) -> _ProcessLease:
        root = cls._integration_root(item_workspace)
        cls._ensure_safe_dir(root)
        return _ProcessLease.acquire(root / _INVOCATION_LOCK_FILENAME, owner_id, invocation_id)

    @staticmethod
    def _ensure_safe_dir(path: Path) -> None:
        _assert_no_symlink(path, label="integration directory")
        path.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink(path, label="integration directory")

    @staticmethod
    def _validate_lifecycle_target(context: RunContext, item_workspace: Any) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("IntegrationSession requires a RunContext")
        if getattr(item_workspace, "context", None) is not context:
            raise ValueError("item workspace must use the same RunContext")
        lifecycle = RunLifecycle.load(context)
        item_id = str(getattr(item_workspace, "item_id", ""))
        if item_id not in lifecycle.item_ids:
            raise ValueError("integration item is outside lifecycle item order")
        if getattr(item_workspace, "mode", None) != lifecycle.snapshot.mode:
            raise ValueError("integration item mode does not match run lifecycle")

    @classmethod
    @contextmanager
    def _session_lock(cls, item_workspace: Any):
        """Serialize one item's integration owner and durable mutations."""

        root = cls._integration_root(item_workspace)
        cls._ensure_safe_dir(root)
        lock_path = root / ".session.lock"
        _assert_no_symlink(lock_path, label="integration session lock")
        with lock_path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _refresh_authoritative(self) -> None:
        staging = self.staging_root
        if not (staging / _SNAPSHOT_FILENAME).is_file():
            intent_path = self.fidelity_removal_intent_path
            if intent_path.exists() or intent_path.is_symlink():
                raise ValueError("fidelity removal intent exists without an integration snapshot")
            return
        state, records = self._read_staging_snapshot(staging, self.bundle)
        if (
            state.get("session_id") != self.session_id
            or state.get("owner_id") != self.owner_id
            or state.get("invocation_id") != self.invocation_id
        ):
            raise ValueError("integration staging identity changed")
        self._state = state
        self._records = records
        self._by_id = {record.record_id: record for record in records}
        self._reconcile_fidelity_removal_unlocked()

    @staticmethod
    def _fidelity_removal_crash_hook(_phase: str) -> None:
        """Private no-op seam used only by subprocess crash-boundary tests."""

    def _read_fidelity_removal_intent(self) -> dict[str, Any] | None:
        path = self.fidelity_removal_intent_path
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("fidelity removal intent is invalid")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("fidelity removal intent is invalid") from exc
        expected = {
            "schema_version",
            "item_id",
            "session_id",
            "owner_id",
            "invocation_id",
            "authorization_hash",
            "record_id",
            "baseline_record_hash",
            "before_records_hash",
            "after_records_hash",
            "phase",
            "intent_hash",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("fidelity removal intent fields are invalid")
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("fidelity removal intent schema_version is invalid")
        for name in ("item_id", "session_id", "owner_id", "invocation_id", "record_id"):
            if name == "owner_id":
                _safe_component(value.get(name), "fidelity removal intent owner_id")
            else:
                _validate_record_id(value.get(name))
        for name in ("authorization_hash", "baseline_record_hash", "before_records_hash", "after_records_hash", "intent_hash"):
            if not _is_sha256(value.get(name)):
                raise ValueError(f"fidelity removal intent {name} is invalid")
        if value.get("phase") not in _FIDELITY_REMOVAL_PHASES:
            raise ValueError("fidelity removal intent phase is invalid")
        unsigned = {key: item for key, item in value.items() if key != "intent_hash"}
        if value.get("intent_hash") != _sha256_value(unsigned):
            raise ValueError("fidelity removal intent hash does not match content")
        return dict(value)

    def _write_fidelity_removal_intent(
        self,
        authorization: FidelityRepairAuthorization,
        *,
        target: str,
        baseline_record_hash: str,
        before_records_hash: str,
        after_records_hash: str,
        phase: str,
    ) -> dict[str, Any]:
        if phase not in _FIDELITY_REMOVAL_PHASES:
            raise ValueError("fidelity removal intent phase is invalid")
        unsigned = {
            "schema_version": _SCHEMA_VERSION,
            "item_id": self.item_id,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "invocation_id": self.invocation_id,
            "authorization_hash": authorization.authorization_hash,
            "record_id": _validate_record_id(target),
            "baseline_record_hash": baseline_record_hash,
            "before_records_hash": before_records_hash,
            "after_records_hash": after_records_hash,
            "phase": phase,
        }
        if any(not _is_sha256(unsigned[name]) for name in ("authorization_hash", "baseline_record_hash", "before_records_hash", "after_records_hash")):
            raise ValueError("fidelity removal intent hash binding is invalid")
        value = {**unsigned, "intent_hash": _sha256_value(unsigned)}
        _atomic_write_json(self.fidelity_removal_intent_path, value)
        return value

    def _reconcile_fidelity_removal_unlocked(self) -> None:
        """Converge a removal journal before any normal repair validation."""

        intent = self._read_fidelity_removal_intent()
        if intent is None:
            return
        if self.status != "open" or self._lease is None:
            raise ValueError("fidelity removal intent is bound to a closed integration")
        commit_intent = self.staging_root / _INTENT_FILENAME
        if commit_intent.exists() or commit_intent.is_symlink():
            raise ValueError("fidelity removal intent conflicts with integration commit intent")
        self._ensure_bundle_current()
        authorization = self._read_fidelity_removal_authorization_for_recovery(intent)
        target = str(intent["record_id"])
        baseline = authorization.baseline_record_hashes.get(target)
        if baseline is None or target not in authorization.affected_record_ids or target in authorization.dependency_ids:
            raise ValueError("fidelity removal intent target is outside the authorized affected scope")
        if baseline != intent["baseline_record_hash"]:
            raise ValueError("fidelity removal intent baseline binding is invalid")

        progress = self._read_fidelity_progress_for_recovery(authorization)
        prior_removed = set(progress.removed_record_ids)
        if target in progress.corrected_record_hashes:
            raise ValueError("fidelity removal intent target was already corrected")
        if not prior_removed.issubset(set(authorization.affected_record_ids)):
            raise ValueError("fidelity removal progress is outside the affected scope")
        if prior_removed & set(progress.corrected_record_hashes):
            raise ValueError("fidelity removal progress cannot correct and remove one record")
        current_records_hash = _sha256_bytes(self._records_bytes())
        before_records_hash = str(intent["before_records_hash"])
        after_records_hash = str(intent["after_records_hash"])
        if current_records_hash not in {before_records_hash, after_records_hash}:
            raise ValueError("fidelity removal intent records hash is unexpected")

        def assert_expected_hashes(removed_ids: set[str]) -> None:
            expected_ids = set(authorization.baseline_record_hashes) - removed_ids
            current_hashes = self._current_record_hashes()
            if set(current_hashes) != expected_ids:
                raise ValueError("fidelity removal intent record set is unexpected")
            for record_id, baseline_hash in authorization.baseline_record_hashes.items():
                if record_id in removed_ids:
                    continue
                expected_hash = progress.corrected_record_hashes.get(record_id, baseline_hash)
                if current_hashes.get(record_id) != expected_hash:
                    raise ValueError("fidelity removal intent record hash is stale or tampered")

        current_ids = set(self._by_id)
        expected_before_ids = set(authorization.baseline_record_hashes) - prior_removed
        expected_after_ids = expected_before_ids - {target}
        candidate_validated = False
        if current_records_hash == before_records_hash:
            if intent["phase"] != "prepared":
                raise ValueError("fidelity removal intent phase does not match pre-removal records")
            assert_expected_hashes(prior_removed)
            if target not in self._by_id or current_ids != expected_before_ids:
                raise ValueError("fidelity removal intent pre-removal records are unexpected")
            current_target = self._by_id[target]
            if current_target.record_hash != baseline:
                raise ValueError("fidelity removal intent target baseline is stale")
            if progress.current_records_hash != before_records_hash or target in prior_removed:
                raise ValueError("fidelity removal progress does not match pre-removal records")
            removal_records = [record for record in self._records if record.record_id != target]
            expected_hash = _sha256_bytes(b"".join(_canonical_bytes(record.to_dict()) for record in removal_records))
            if expected_hash != after_records_hash:
                raise ValueError("fidelity removal intent after-records hash is invalid")
            prior_records = list(self._records)
            prior_by_id = dict(self._by_id)
            prior_lem_projection = self.lem_projection
            prior_lem = self.lem
            self._records = removal_records
            self._by_id = {record.record_id: record for record in removal_records}
            try:
                # Recovery is validating the intermediate post-removal
                # snapshot.  Permit it to be empty here while retaining the
                # full analytical relationship checks; a required
                # relationship removal still fails closed.
                self._preflight_all(allow_empty_staging=True)
            except Exception:
                # A validly-shaped journal can still describe a candidate
                # rejected by full mechanical/LEM preflight (for example a
                # required relationship).  Keep recovery fail-closed and
                # leave the in-memory session exactly as it was read.
                self._records = prior_records
                self._by_id = prior_by_id
                self.lem_projection = prior_lem_projection
                self.lem = prior_lem
                raise
            candidate_validated = True
            removed_hashes = dict(self._state.get(_UNREVIEWED_REMOVED_RECORD_HASHES, {}))
            removed_hashes[target] = baseline
            state = dict(self._state)
            state[_UNREVIEWED_REMOVED_RECORD_HASHES] = dict(sorted(removed_hashes.items()))
            self._persist_state(state)
            self._write_fidelity_removal_intent(
                authorization,
                target=target,
                baseline_record_hash=baseline,
                before_records_hash=before_records_hash,
                after_records_hash=after_records_hash,
                phase="records_persisted",
            )
            current_records_hash = after_records_hash
        else:
            assert_expected_hashes(prior_removed | {target})
            if target in self._by_id or current_ids != expected_after_ids:
                raise ValueError("fidelity removal intent post-removal records are unexpected")
            if progress.current_records_hash not in {before_records_hash, after_records_hash}:
                raise ValueError("fidelity removal progress records hash is unexpected")
            progress_converged = (
                progress.current_records_hash == after_records_hash
                and target in progress.removed_record_ids
            )
            if progress_converged:
                if intent["phase"] not in {"records_persisted", "progress_persisted"}:
                    raise ValueError("fidelity removal intent phase does not match progress")
            elif intent["phase"] not in {"prepared", "records_persisted"}:
                raise ValueError("fidelity removal intent phase does not match post-removal records")

        if current_records_hash != after_records_hash:
            raise ValueError("fidelity removal did not converge records")
        if not candidate_validated:
            # A process crash normally resumes a candidate that was already
            # preflighted before its snapshot write.  Re-run the same full
            # mechanical/registry/LEM preflight on reload so a forged or
            # otherwise unexpected post-removal state cannot advance repair
            # progress merely because its hashes line up.
            # A reviewed repair may legitimately remove its last affected
            # record.  Validate the resulting empty snapshot as an
            # intermediate transaction, but retain full analytical
            # relationship completeness so removing a required relationship
            # still rolls back byte-for-byte.  The durable removal progress
            # written below records the explicit no-op history that makes the
            # final public validation auditable.
            self._preflight_all(allow_empty_staging=True)
        completed_removed = prior_removed | {target}
        if progress.current_records_hash == after_records_hash and target in progress.removed_record_ids:
            # The progress write already converged; only the journal cleanup
            # remains.  Keep this path byte-stable for exact retries.
            pass
        else:
            if progress.current_records_hash != before_records_hash or target in progress.removed_record_ids:
                raise ValueError("fidelity removal progress transition is invalid")
            self._write_repair_progress(
                self.fidelity_progress_path,
                authorization,
                progress.corrected_record_hashes,
                current_records_hash=after_records_hash,
                current_packet_hash=None,
                removed_record_ids=completed_removed,
            )
        # Persist the explicit removal marker even when this method is
        # converging a crash journal whose pre-removal state did not yet carry
        # it.  This makes the resulting empty snapshot distinguishable from a
        # fresh, incomplete session at commit/reopen time.
        removed_hashes = dict(self._state.get(_UNREVIEWED_REMOVED_RECORD_HASHES, {}))
        removed_hashes[target] = baseline
        state = dict(self._state)
        state[_UNREVIEWED_REMOVED_RECORD_HASHES] = dict(sorted(removed_hashes.items()))
        self._persist_state(state)
        self._write_fidelity_removal_intent(
            authorization,
            target=target,
            baseline_record_hash=baseline,
            before_records_hash=before_records_hash,
            after_records_hash=after_records_hash,
            phase="progress_persisted",
        )
        self.fidelity_removal_intent_path.unlink()

    def _read_fidelity_removal_authorization_for_recovery(
        self,
        intent: Mapping[str, Any],
    ) -> FidelityRepairAuthorization:
        if (
            intent.get("item_id") != self.item_id
            or intent.get("session_id") != self.session_id
            or intent.get("owner_id") != self.owner_id
            or intent.get("invocation_id") != self.invocation_id
        ):
            raise ValueError("fidelity removal intent identity binding is invalid")
        authorization = self._read_repair_authorization()
        if (
            authorization.authorization_hash != intent.get("authorization_hash")
            or authorization.item_id != self.item_id
            or authorization.session_id != self.session_id
            or authorization.invocation_id != self.invocation_id
        ):
            raise ValueError("fidelity removal intent authorization binding is invalid")
        if intent.get("owner_id") != self.owner_id:
            raise ValueError("fidelity removal intent owner binding is invalid")
        return authorization

    def _read_fidelity_progress_for_recovery(
        self,
        authorization: FidelityRepairAuthorization,
    ) -> FidelityRepairProgress:
        path = self.fidelity_progress_path
        if not path.is_file() or path.is_symlink():
            raise ValueError("integration fidelity repair progress is missing")
        try:
            progress = FidelityRepairProgress.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("integration fidelity repair progress is invalid") from exc
        if (
            progress.item_id != self.item_id
            or progress.session_id != self.session_id
            or progress.invocation_id != self.invocation_id
            or progress.authorization_hash != authorization.authorization_hash
        ):
            raise ValueError("integration fidelity repair progress is unbound")
        affected = set(authorization.affected_record_ids)
        if not set(progress.corrected_record_hashes).issubset(affected):
            raise ValueError("integration fidelity repair progress references unauthorized records")
        if not set(progress.removed_record_ids).issubset(set(authorization.affected_record_ids)):
            raise ValueError("integration fidelity repair progress removals are outside the affected scope")
        if set(progress.corrected_record_hashes) & set(progress.removed_record_ids):
            raise ValueError("integration fidelity repair progress cannot correct and remove one record")
        if any(record_id not in authorization.baseline_record_hashes for record_id in progress.removed_record_ids):
            raise ValueError("integration fidelity repair progress removal has no fidelity baseline")
        for record_id, corrected_hash in progress.corrected_record_hashes.items():
            if corrected_hash == authorization.baseline_record_hashes.get(record_id):
                raise ValueError("integration fidelity repair progress correction does not differ from baseline")
        packet = self._read_fidelity_packet_raw()
        if packet is None or packet.item_id != self.item_id or packet.session_id != self.session_id or packet.invocation_id != self.invocation_id:
            raise ValueError("integration fidelity repair progress packet binding is invalid")
        if (
            packet.accepted_content_hash != self.bundle.content_hash
            or packet.accepted_manifest_hash != self.bundle.manifest_hash
        ):
            raise ValueError("integration fidelity repair progress packet binding is invalid")
        if progress.current_packet_hash is None:
            if packet.records_hash == progress.current_records_hash:
                raise ValueError("integration fidelity repair progress packet hash is missing")
        elif packet.packet_hash != progress.current_packet_hash or packet.records_hash != progress.current_records_hash:
            raise ValueError("integration fidelity repair progress packet hash is stale")
        return progress

    def _ensure_bundle_current(self) -> None:
        """Reject any accepted-directory replacement after session creation."""

        current = AcceptedAnalysisBundle.load(self.item_workspace)
        if (
            current.item_id != self.bundle.item_id
            or current.content_hash != self.bundle.content_hash
            or current.envelope_hash != self.bundle.envelope_hash
            or current.manifest_hash != self.bundle.manifest_hash
            or current.answer_content != self.bundle.answer_content
            or current.acceptance_envelope != self.bundle.acceptance_envelope
            or current.manifest != self.bundle.manifest
        ):
            raise ValueError("accepted analysis bundle changed after integration session creation")

    @staticmethod
    def _snapshot_value(state: Mapping[str, Any], records: Sequence[IntegrationRecord]) -> dict[str, Any]:
        body = {
            "schema_version": _SCHEMA_VERSION,
            "state": dict(state),
            "records": [record.to_dict() for record in records],
        }
        body["snapshot_hash"] = _sha256_value(body)
        return body

    @classmethod
    def _write_staging_snapshot(
        cls,
        staging: Path,
        state: Mapping[str, Any],
        records: Sequence[IntegrationRecord],
    ) -> None:
        """Publish one authoritative staging snapshot, then projections.

        ``snapshot.json`` is written first and is the recovery authority.  The
        human-readable session/JSONL projections are written afterward; a
        crash between those writes is reconciled on reload from the snapshot;
        projection mismatch is repaired from that authoritative snapshot.
        """

        cls._ensure_safe_dir(staging)
        snapshot = cls._snapshot_value(state, records)
        _atomic_write_json(staging / _SNAPSHOT_FILENAME, snapshot)
        _atomic_write_json(staging / _SESSION_FILENAME, state)
        _atomic_write_bytes(staging / _RECORDS_FILENAME, b"".join(_canonical_bytes(record.to_dict()) for record in records))

    @classmethod
    def _read_staging_snapshot(
        cls,
        staging: Path,
        bundle: AcceptedAnalysisBundle,
    ) -> tuple[dict[str, Any], list[IntegrationRecord]]:
        snapshot_path = staging / _SNAPSHOT_FILENAME
        if snapshot_path.is_symlink() or not snapshot_path.is_file():
            raise ValueError("integration staging snapshot is missing")
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("integration staging snapshot is invalid") from exc
        expected = {"schema_version", "state", "records", "snapshot_hash"}
        if not isinstance(snapshot, Mapping) or set(snapshot) != expected or snapshot.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("integration staging snapshot fields are invalid")
        unsigned = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
        if not _is_sha256(snapshot.get("snapshot_hash")) or snapshot["snapshot_hash"] != _sha256_value(unsigned):
            raise ValueError("integration staging snapshot hash does not match content")
        state = snapshot.get("state")
        cls._validate_session_state(state, bundle)
        raw_records = snapshot.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("integration staging snapshot records are invalid")
        records: list[IntegrationRecord] = []
        for value in raw_records:
            record = IntegrationRecord.from_dict(value)
            if record.item_id != bundle.item_id or record.accepted_content_hash != bundle.content_hash:
                raise ValueError("integration record is bound to a different accepted item")
            records.append(record)
        if len({record.record_id for record in records}) != len(records):
            raise ValueError("integration staging snapshot contains duplicate IDs")
        records_bytes = b"".join(_canonical_bytes(record.to_dict()) for record in records)
        if state.get("records_count") != len(records) or state.get("records_hash") != _sha256_bytes(records_bytes):
            raise ValueError("integration staging snapshot record binding is invalid")
        session_path = staging / _SESSION_FILENAME
        if session_path.exists() or session_path.is_symlink():
            if session_path.is_symlink():
                raise ValueError("integration session projection is invalid")
            if not session_path.is_file():
                _atomic_write_json(session_path, state)
                projection_state = state
            else:
                try:
                    projection_state = json.loads(session_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    projection_state = None
                if projection_state != state:
                    _atomic_write_json(session_path, state)
        else:
            _atomic_write_json(session_path, state)
        records_path = staging / _RECORDS_FILENAME
        if records_path.exists() or records_path.is_symlink():
            if records_path.is_symlink():
                raise ValueError("integration records projection is invalid")
            if not records_path.is_file() or records_path.read_bytes() != records_bytes:
                _atomic_write_bytes(records_path, records_bytes)
        else:
            _atomic_write_bytes(records_path, records_bytes)
        return dict(state), records

    @staticmethod
    def _validate_session_state(state: Mapping[str, Any], bundle: AcceptedAnalysisBundle) -> None:
        expected = {
            "schema_version",
            "session_id",
            "item_id",
            "owner_id",
            "invocation_id",
            "status",
            "accepted_content_hash",
            "accepted_manifest_hash",
            "records_count",
            "records_hash",
            "created_at",
            "updated_at",
            "state_hash",
        }
        optional = {_UNREVIEWED_REMOVED_RECORD_HASHES}
        if not isinstance(state, Mapping) or not expected.issubset(set(state)) or not set(state).issubset(expected | optional):
            raise ValueError("integration session fields are invalid")
        if state["schema_version"] != _SCHEMA_VERSION or state["item_id"] != bundle.item_id:
            raise ValueError("integration session identity is invalid")
        _validate_record_id(state.get("session_id"))
        if not isinstance(state.get("owner_id"), str):
            raise ValueError("integration session owner_id is invalid")
        _safe_component(state.get("owner_id"), "owner_id")
        _safe_component(state.get("invocation_id"), "invocation_id")
        if not isinstance(state.get("created_at"), str) or not isinstance(state.get("updated_at"), str):
            raise ValueError("integration session timestamps are invalid")
        if state["accepted_content_hash"] != bundle.content_hash or state["accepted_manifest_hash"] != bundle.manifest_hash:
            raise ValueError("integration session accepted bundle hash is stale")
        if state["status"] not in _SESSION_STATES:
            raise ValueError("integration session status is invalid")
        if not isinstance(state["records_count"], int) or isinstance(state["records_count"], bool) or state["records_count"] < 0:
            raise ValueError("integration session records_count is invalid")
        if not _is_sha256(state["records_hash"]):
            raise ValueError("integration session records_hash is invalid")
        removed_hashes = state.get(_UNREVIEWED_REMOVED_RECORD_HASHES, {})
        if not isinstance(removed_hashes, Mapping):
            raise ValueError("integration session unreviewed removal hashes are invalid")
        for record_id, record_hash in removed_hashes.items():
            _validate_record_id(record_id)
            if not _is_sha256(record_hash):
                raise ValueError("integration session unreviewed removal hash is invalid")
        unsigned = {key: value for key, value in state.items() if key != "state_hash"}
        if state["state_hash"] != _sha256_value(unsigned):
            raise ValueError("integration session state hash does not match content")

    @classmethod
    def _read_records(cls, path: Path, state: Mapping[str, Any], bundle: AcceptedAnalysisBundle) -> list[IntegrationRecord]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("integration records file is missing")
        payload = path.read_bytes()
        if _sha256_bytes(payload) != state.get("records_hash"):
            raise ValueError("integration records hash does not match session")
        records: list[IntegrationRecord] = []
        for line_number, line in enumerate(payload.splitlines(), 1):
            try:
                value = json.loads(line.decode("utf-8"))
                record = IntegrationRecord.from_dict(value)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"integration record line {line_number} is invalid") from exc
            if record.item_id != bundle.item_id or record.accepted_content_hash != bundle.content_hash:
                raise ValueError("integration record is bound to a different accepted item")
            records.append(record)
        if int(state.get("records_count", -1)) != len(records):
            raise ValueError("integration session records_count does not match records")
        if len({record.record_id for record in records}) != len(records):
            raise ValueError("integration records contain duplicate IDs")
        return records

    def _persist_state(self, state: Mapping[str, Any]) -> None:
        candidate = dict(state)
        candidate["records_count"] = len(self._records)
        records_bytes = self._records_bytes()
        candidate["records_hash"] = _sha256_bytes(records_bytes)
        candidate["updated_at"] = _now()
        unsigned = {key: value for key, value in candidate.items() if key != "state_hash"}
        candidate["state_hash"] = _sha256_value(unsigned)
        self._validate_session_state(candidate, self.bundle)
        self._write_staging_snapshot(self.staging_root, candidate, self._records)
        self._state = candidate

    def _records_bytes(self) -> bytes:
        return b"".join(_canonical_bytes(record.to_dict()) for record in self._records)

    @staticmethod
    def _committed_artifact_path(committed_root: Path, artifact_ref: str) -> Path:
        """Resolve one committed artifact ref without following symlinks."""

        if not isinstance(artifact_ref, str) or not artifact_ref.startswith(_ANALYTICAL_ARTIFACT_REF_PREFIX):
            raise ValueError("analytical artifact committed reference is invalid")
        relative = _safe_relative_ref(artifact_ref, "analytical artifact reference")
        if relative != artifact_ref or PurePath(relative).parent.as_posix() != _ANALYTICAL_ARTIFACT_REF_PREFIX.rstrip("/"):
            raise ValueError("analytical artifact committed reference is invalid")
        filename = PurePath(relative).name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", filename):
            raise ValueError("analytical artifact committed reference is invalid")
        destination = committed_root / "artifacts" / filename
        current = committed_root
        for component in PurePath("artifacts", filename).parts:
            current = current / component
            _assert_no_symlink(current, label="committed analytical artifact")
        return destination

    def _accepted_artifact_progress_hashes(self) -> Mapping[str, Any]:
        """Return the immutable work-artifact hashes sealed at acceptance."""

        progress = self.bundle.manifest.get("artifact_progress")
        hashes = progress.get("hashes") if isinstance(progress, Mapping) else None
        if not isinstance(hashes, Mapping):
            raise ValueError("accepted artifact progress hashes are invalid")
        return hashes

    def _validate_external_artifact_outputs(self, artifact: AnalyticalArtifact) -> tuple[tuple[str, Path], ...]:
        """Re-verify external artifact outputs in the accepted work root."""

        return _validate_analytical_output_refs(
            artifact,
            output_root=self.item_workspace.work_root,
            accepted_hashes=self._accepted_artifact_progress_hashes(),
            label=f"analytical artifact {artifact.artifact_id}",
        )

    @classmethod
    def _validate_committed_artifacts(
        cls,
        committed_root: Path,
        records: Sequence[IntegrationRecord],
    ) -> None:
        """Verify every record-bound artifact file in a committed tree."""

        seen_refs: set[str] = set()
        seen_ids: dict[str, tuple[str, str, str]] = {}
        for record in records:
            if record.kind != "analytical_artifact":
                continue
            artifact, canonical_bytes = _parse_analytical_artifact_payload(
                record.payload,
                expected_item_id=record.item_id,
            )
            ref = str(record.payload["artifact_ref"])
            if ref in seen_refs:
                raise ValueError(f"duplicate committed analytical artifact reference: {ref}")
            seen_refs.add(ref)
            identity = (
                artifact.content_hash,
                artifact.envelope_hash,
                str(record.payload["canonical_bytes_sha256"]),
                ref,
            )
            prior_identity = seen_ids.get(artifact.artifact_id)
            if prior_identity is not None and prior_identity != identity:
                raise ValueError(f"analytical artifact ID collision: {artifact.artifact_id}")
            seen_ids[artifact.artifact_id] = identity
            # External outputs are copied into the committed artifact root at
            # commit.  Re-verify the copy before a committed session can be
            # projected or reused; a missing/tampered/symlinked JSONL output
            # therefore fails closed on every reload.
            _validate_analytical_output_refs(
                artifact,
                output_root=committed_root / "artifacts",
                accepted_hashes=None,
                label=f"committed analytical artifact {artifact.artifact_id}",
            )
            path = cls._committed_artifact_path(committed_root, ref)
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"committed analytical artifact is missing: {ref}")
            try:
                actual = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"committed analytical artifact cannot be read: {ref}") from exc
            if actual != canonical_bytes or _sha256_bytes(actual) != record.payload.get("canonical_bytes_sha256"):
                raise ValueError(f"committed analytical artifact bytes do not match record: {ref}")
            try:
                restored = AnalyticalArtifact.from_json(actual.decode("utf-8"))
            except (UnicodeDecodeError, AnalyticalArtifactValidationError, TypeError, ValueError) as exc:
                raise ValueError(f"committed analytical artifact JSON is invalid: {ref}") from exc
            if restored.to_dict() != artifact.to_dict():
                raise ValueError(f"committed analytical artifact content does not match record: {ref}")

    def _current_record_hashes(self) -> dict[str, str]:
        return {record.record_id: record.record_hash for record in self._records}

    def _build_fidelity_packet(self) -> IntegrationFidelityPacket:
        """Build a packet from this item only; no cumulative state traversal."""

        self._ensure_bundle_current()
        # Fidelity is an item-local review, but relationship endpoint
        # references are a closed semantic boundary.  Re-run the same
        # side-effect-free validation used by commit so an unknown endpoint
        # cannot receive a durable ``accept`` fidelity result and fail only
        # later during publication.
        validation = self.validate()
        if not validation.valid:
            raise ValueError(f"integration validation failed: {list(validation.errors)}")
        repair_binding: tuple[FidelityRepairAuthorization, FidelityRepairProgress] | None = None
        if self.fidelity_authorization_path.exists() or self.fidelity_authorization_path.is_symlink():
            authorization = self._read_repair_authorization()
            progress = self._read_repair_progress(authorization)
            self._assert_repair_snapshot(authorization, progress, self._current_record_hashes())
            repair_binding = (authorization, progress)
        try:
            answer_content = json.loads(self.bundle.answer_content.decode("utf-8"))
            json.dumps(answer_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("accepted answer content is not JSON-safe UTF-8") from exc
        answer_content_b64 = base64.b64encode(self.bundle.answer_content).decode("ascii")
        acceptance_envelope = json.loads(json.dumps(_jsonable(self.bundle.acceptance_envelope), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        manifest = json.loads(json.dumps(_jsonable(self.bundle.manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        record_values = tuple(record.to_dict() for record in self._records)
        evidence_values = tuple(
            {
                "record_id": record.record_id,
                "evidence_refs": list(record.evidence_refs),
                "evidence_hashes": dict(record.evidence_hashes),
            }
            for record in self._records
        )
        candidate_values = tuple(
            dict(record.payload)
            for record in self._records
            if record.kind == "prepared_asset"
        )
        unsigned = {
            "schema_version": "1",
            "item_id": self.item_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "accepted_content_hash": self.bundle.content_hash,
            "accepted_manifest_hash": self.bundle.manifest_hash,
            "answer_content": answer_content,
            "answer_content_b64": answer_content_b64,
            "accepted_answer_bytes_hash": self.bundle.content_hash,
            "acceptance_envelope": acceptance_envelope,
            "manifest": manifest,
            "records_hash": _sha256_bytes(self._records_bytes()),
            "records": list(record_values),
            "evidence": list(evidence_values),
            "candidates": list(candidate_values),
            "created_at": _now(),
        }
        packet = IntegrationFidelityPacket(
            schema_version="1",
            item_id=self.item_id,
            session_id=self.session_id,
            invocation_id=self.invocation_id,
            accepted_content_hash=self.bundle.content_hash,
            accepted_manifest_hash=self.bundle.manifest_hash,
            answer_content=answer_content,
            answer_content_b64=answer_content_b64,
            accepted_answer_bytes_hash=self.bundle.content_hash,
            acceptance_envelope=acceptance_envelope,
            manifest=manifest,
            records_hash=unsigned["records_hash"],
            records=record_values,
            evidence=evidence_values,
            candidates=candidate_values,
            created_at=unsigned["created_at"],
            packet_hash=_fidelity_digest(unsigned),
        )
        packet_path = self.fidelity_packet_path
        prior_packet_exists = packet_path.exists() or packet_path.is_symlink()
        prior_packet_bytes = packet_path.read_bytes() if prior_packet_exists else None
        progress_path = self.fidelity_progress_path
        prior_progress_exists = progress_path.exists() or progress_path.is_symlink()
        prior_progress_bytes = progress_path.read_bytes() if prior_progress_exists else None
        try:
            write_packet(packet_path, packet)
            if repair_binding is not None:
                authorization, progress = repair_binding
                self._write_repair_progress(
                    progress_path,
                    authorization,
                    progress.corrected_record_hashes,
                    removed_record_ids=progress.removed_record_ids,
                    current_records_hash=packet.records_hash,
                    current_packet_hash=packet.packet_hash,
                )
        except Exception:
            if prior_packet_exists and prior_packet_bytes is not None:
                _atomic_write_bytes(packet_path, prior_packet_bytes)
            elif packet_path.exists() or packet_path.is_symlink():
                packet_path.unlink()
            if prior_progress_exists and prior_progress_bytes is not None:
                _atomic_write_bytes(progress_path, prior_progress_bytes)
            elif progress_path.exists() or progress_path.is_symlink():
                progress_path.unlink()
            raise
        return packet

    def build_fidelity_packet(self) -> IntegrationFidelityPacket:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._build_fidelity_packet()

    create_fidelity_packet = build_fidelity_packet

    def _read_fidelity_packet(self) -> IntegrationFidelityPacket | None:
        self._ensure_bundle_current()
        path = self.fidelity_packet_path
        if not path.exists() or path.is_symlink():
            return None
        try:
            packet = IntegrationFidelityPacket.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("integration fidelity packet is invalid") from exc
        if (
            packet.item_id != self.item_id
            or packet.session_id != self.session_id
            or packet.invocation_id != self.invocation_id
            or packet.accepted_content_hash != self.bundle.content_hash
            or packet.accepted_manifest_hash != self.bundle.manifest_hash
            or packet.records_hash != _sha256_bytes(self._records_bytes())
        ):
            raise ValueError("integration fidelity packet is stale or bound to another item")
        # ``IntegrationFidelityPacket`` validates its envelope and record
        # bytes, but intentionally treats records as generic mappings.  Parse
        # every packet record through the same strict IntegrationRecord
        # contract used by staging/reload so an artifact payload cannot be
        # smuggled through a recomputed packet hash.  The packet must also be
        # an exact snapshot of the current durable records (including order).
        packet_records: list[IntegrationRecord] = []
        for line_number, value in enumerate(packet.records, 1):
            try:
                record = IntegrationRecord.from_dict(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"integration fidelity packet record {line_number} is invalid") from exc
            packet_records.append(record)
        if tuple(record.to_dict() for record in packet_records) != tuple(record.to_dict() for record in self._records):
            raise ValueError("integration fidelity packet records do not match staging")
        # The packet hash covers the artifact JSON record, not the separate
        # external output bytes.  Re-verify those bytes on every fidelity
        # read so a post-generation tamper cannot be hidden behind a valid
        # packet hash.
        for record in packet_records:
            if record.kind != "analytical_artifact":
                continue
            artifact, _artifact_bytes = _parse_analytical_artifact_payload(
                record.payload,
                expected_item_id=self.item_id,
            )
            self._validate_external_artifact_outputs(artifact)
        return packet

    def _read_fidelity_packet_raw(self) -> IntegrationFidelityPacket | None:
        """Read a packet without binding it to the current record snapshot."""

        path = self.fidelity_packet_path
        if not path.exists() or path.is_symlink():
            return None
        try:
            packet = IntegrationFidelityPacket.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("integration fidelity packet is invalid") from exc
        # Even in the raw/recovery path, packet records are not opaque JSON:
        # strict record parsing keeps analytical-artifact identity/hash
        # validation active before a packet can seed a rebuilt fidelity view.
        for line_number, value in enumerate(packet.records, 1):
            try:
                IntegrationRecord.from_dict(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"integration fidelity packet record {line_number} is invalid") from exc
        return packet

    def _read_fidelity_result(self) -> FidelityResult | None:
        path = self.fidelity_result_path
        if not path.exists() or path.is_symlink():
            return None
        try:
            result = FidelityResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("integration fidelity result is invalid") from exc
        packet = self._read_fidelity_packet()
        if packet is None or result.packet_hash != packet.packet_hash or result.records_hash != _sha256_bytes(self._records_bytes()):
            raise ValueError("integration fidelity result is stale or unbound")
        if result.item_id != self.item_id or result.session_id != self.session_id or result.invocation_id != self.invocation_id:
            raise ValueError("integration fidelity result identity is invalid")
        current_hashes = self._current_record_hashes()
        known_ids = set(current_hashes)
        removed_ids: set[str] = set()
        if result.review_kind == "targeted":
            authorization_path = self.fidelity_authorization_path
            if not authorization_path.is_file() or authorization_path.is_symlink():
                raise ValueError("integration fidelity repair authorization is missing")
            try:
                authorization = FidelityRepairAuthorization.from_dict(
                    json.loads(authorization_path.read_text(encoding="utf-8"))
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError("integration fidelity repair authorization is invalid") from exc
            if (
                authorization.item_id != self.item_id
                or authorization.session_id != self.session_id
                or authorization.invocation_id != self.invocation_id
            ):
                raise ValueError("integration fidelity repair authorization identity is invalid")
            progress = self._read_repair_progress(authorization, require_current_packet=True)
            self._assert_repair_snapshot(authorization, progress, current_hashes)
            removed_ids = set(progress.removed_record_ids)
        referenced_ids = set(result.affected_record_ids) | set(result.dependency_ids)
        for finding in result.findings:
            referenced_ids.update(finding.record_ids)
            referenced_ids.update(finding.dependency_ids)
        if not referenced_ids.issubset(known_ids | removed_ids):
            raise ValueError("integration fidelity result references unknown record")
        if result.review_kind == "initial":
            if set(result.baseline_record_hashes) != known_ids:
                raise ValueError("integration fidelity result baseline record set is invalid")
            if result.verdict in {"accept", "accept_with_limits", "repair_once"} and set(result.checked_record_ids) != known_ids:
                raise ValueError("initial fidelity result checked_record_ids are invalid")
            if result.verdict in {"unavailable", "fail"} and result.checked_record_ids:
                raise ValueError("non-accepting initial fidelity result cannot claim checked records")
            if result.verdict == "repair_once" and not result.affected_record_ids:
                raise ValueError("initial fidelity repair result requires affected records")
            if dict(result.baseline_record_hashes) != current_hashes:
                raise ValueError("initial fidelity result baseline hashes are stale or tampered")
        else:
            if set(result.baseline_record_hashes) != set(authorization.baseline_record_hashes):
                raise ValueError("targeted fidelity result baseline record set is invalid")
            if result.verdict == "repair_once":
                raise ValueError("targeted fidelity result cannot authorize another repair")
            expected_checked = set(result.affected_record_ids) | set(result.dependency_ids)
            if set(result.checked_record_ids) != expected_checked:
                raise ValueError("targeted fidelity result checked_record_ids are invalid")
        return result

    def _read_fidelity_result_raw(self) -> FidelityResult | None:
        """Read a result before current-record binding for repair transitions."""

        path = self.fidelity_result_path
        if not path.exists() or path.is_symlink():
            return None
        try:
            return FidelityResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("integration fidelity result is invalid") from exc

    def _read_repair_authorization(self) -> FidelityRepairAuthorization:
        path = self.fidelity_authorization_path
        if not path.exists() or path.is_symlink():
            raise ValueError("integration fidelity repair authorization is missing")
        try:
            authorization = FidelityRepairAuthorization.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("integration fidelity repair authorization is invalid") from exc
        if (
            authorization.item_id != self.item_id
            or authorization.session_id != self.session_id
            or authorization.invocation_id != self.invocation_id
        ):
            raise ValueError("integration fidelity repair authorization identity is invalid")
        result = self._read_fidelity_result_raw()
        packet = self._read_fidelity_packet_raw()
        if result is None or packet is None:
            raise ValueError("integration fidelity repair authorization binding is missing")
        if (
            packet.item_id != self.item_id
            or packet.session_id != self.session_id
            or packet.invocation_id != self.invocation_id
            or packet.accepted_content_hash != self.bundle.content_hash
            or packet.accepted_manifest_hash != self.bundle.manifest_hash
        ):
            raise ValueError("integration fidelity repair authorization packet binding is invalid")
        if (
            result.review_kind != "initial"
            or result.verdict != "repair_once"
            or result.result_hash != authorization.initial_result_hash
            or result.packet_hash != authorization.initial_packet_hash
            or dict(result.baseline_record_hashes) != dict(authorization.baseline_record_hashes)
            or set(result.affected_record_ids) != set(authorization.affected_record_ids)
            or set(result.dependency_ids) != set(authorization.dependency_ids)
        ):
            raise ValueError("integration fidelity repair authorization is stale or mismatched")
        return authorization

    def _read_repair_progress(
        self,
        authorization: FidelityRepairAuthorization,
        *,
        require_current_packet: bool = False,
    ) -> FidelityRepairProgress:
        path = self.fidelity_progress_path
        if not path.exists() or path.is_symlink():
            raise ValueError("integration fidelity repair progress is missing")
        try:
            progress = FidelityRepairProgress.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("integration fidelity repair progress is invalid") from exc
        if (
            progress.item_id != self.item_id
            or progress.session_id != self.session_id
            or progress.invocation_id != self.invocation_id
            or progress.authorization_hash != authorization.authorization_hash
        ):
            raise ValueError("integration fidelity repair progress is unbound")
        affected = set(authorization.affected_record_ids)
        if not set(progress.corrected_record_hashes).issubset(affected):
            raise ValueError("integration fidelity repair progress references unauthorized records")
        removed = set(progress.removed_record_ids)
        if not removed.issubset(set(authorization.affected_record_ids)):
            raise ValueError("integration fidelity repair progress removals are outside the affected scope")
        if removed & set(progress.corrected_record_hashes):
            raise ValueError("integration fidelity repair progress cannot correct and remove one record")
        if any(record_id not in authorization.baseline_record_hashes for record_id in removed):
            raise ValueError("integration fidelity repair progress removal has no fidelity baseline")
        for record_id, corrected_hash in progress.corrected_record_hashes.items():
            if corrected_hash == authorization.baseline_record_hashes.get(record_id):
                raise ValueError("integration fidelity repair progress correction does not differ from baseline")
        current_records_hash = _sha256_bytes(self._records_bytes())
        if progress.current_records_hash != current_records_hash:
            raise ValueError("integration fidelity repair progress records hash is stale")
        packet = self._read_fidelity_packet_raw()
        if packet is None:
            raise ValueError("integration fidelity repair progress packet is missing")
        if (
            packet.item_id != self.item_id
            or packet.session_id != self.session_id
            or packet.invocation_id != self.invocation_id
            or packet.accepted_content_hash != self.bundle.content_hash
            or packet.accepted_manifest_hash != self.bundle.manifest_hash
        ):
            raise ValueError("integration fidelity repair progress packet binding is invalid")
        if progress.current_packet_hash is None:
            if require_current_packet:
                raise ValueError("targeted fidelity recheck requires a rebuilt current packet")
            if packet.records_hash == progress.current_records_hash:
                raise ValueError("integration fidelity repair progress packet hash is missing")
        else:
            if packet.packet_hash != progress.current_packet_hash or packet.records_hash != progress.current_records_hash:
                raise ValueError("integration fidelity repair progress packet hash is stale")
        self._assert_repair_snapshot(authorization, progress, self._current_record_hashes())
        return progress

    @staticmethod
    def _write_repair_progress(
        path: Path,
        authorization: FidelityRepairAuthorization,
        corrected_record_hashes: Mapping[str, str],
        *,
        current_records_hash: str,
        current_packet_hash: str | None,
        removed_record_ids: Iterable[str] = (),
    ) -> FidelityRepairProgress:
        removed = tuple(sorted(_validate_record_id(value) for value in removed_record_ids))
        if len(removed) != len(set(removed)):
            raise ValueError("fidelity repair progress removed record IDs contain duplicates")
        unsigned = {
            "schema_version": _SCHEMA_VERSION,
            "item_id": authorization.item_id,
            "session_id": authorization.session_id,
            "invocation_id": authorization.invocation_id,
            "authorization_hash": authorization.authorization_hash,
            "corrected_record_hashes": {
                str(key): str(value) for key, value in sorted(corrected_record_hashes.items())
            },
            "removed_record_ids": list(removed),
            "current_records_hash": current_records_hash,
            "current_packet_hash": current_packet_hash,
        }
        progress = FidelityRepairProgress(
            schema_version=_SCHEMA_VERSION,
            item_id=authorization.item_id,
            session_id=authorization.session_id,
            invocation_id=authorization.invocation_id,
            authorization_hash=authorization.authorization_hash,
            corrected_record_hashes=unsigned["corrected_record_hashes"],
            removed_record_ids=removed,
            current_records_hash=current_records_hash,
            current_packet_hash=current_packet_hash,
            progress_hash=_fidelity_digest(unsigned),
        )
        write_repair_progress(path, progress)
        return progress

    @staticmethod
    def _exact_checked_record_ids(
        checked_record_ids: Iterable[str] | None,
        expected_ids: set[str],
        *,
        label: str,
    ) -> tuple[str, ...]:
        if checked_record_ids is None:
            raise ValueError(f"{label} checked_record_ids are required")
        values = tuple(_validate_record_id(value) for value in checked_record_ids)
        if not values:
            raise ValueError(f"{label} checked_record_ids cannot be empty")
        if len(values) != len(set(values)):
            raise ValueError(f"{label} checked_record_ids contain duplicates")
        if set(values) != expected_ids:
            raise ValueError(f"{label} checked_record_ids must match exactly the authorized record IDs")
        return values

    @staticmethod
    def _assert_repair_snapshot(
        authorization: FidelityRepairAuthorization,
        progress: FidelityRepairProgress,
        current_hashes: Mapping[str, str],
    ) -> None:
        baseline = dict(authorization.baseline_record_hashes)
        corrected = dict(progress.corrected_record_hashes)
        removed = set(progress.removed_record_ids)
        if not set(corrected).issubset(set(authorization.affected_record_ids)):
            raise ValueError("integration repair correction is outside the affected scope")
        if removed & set(corrected):
            raise ValueError("integration repair cannot correct and remove one record")
        if not removed.issubset(set(authorization.affected_record_ids)):
            raise ValueError("integration repair removal is outside the affected scope")
        expected_ids = set(baseline) - removed
        if set(current_hashes) != expected_ids:
            raise ValueError("integration repair changed the staged record set")
        for record_id, baseline_hash in baseline.items():
            if record_id in removed:
                continue
            expected = corrected.get(record_id, baseline_hash)
            if current_hashes.get(record_id) != expected:
                raise ValueError("integration repair record hash is stale or tampered")

    def _active_corrected_repair_for_diagnostics(self) -> bool:
        """Return whether a stale public result is expected during correction.

        ``fidelity_result`` is a diagnostic view.  During an authorized
        correction the durable result intentionally remains bound to the
        pre-repair records until a targeted recheck publishes its replacement;
        the strict internal readers must continue to reject that stale view.
        Detect that narrow, valid transition through the immutable
        authorization and current repair progress records.
        """

        try:
            raw_result = self._read_fidelity_result_raw()
            if raw_result is None or raw_result.review_kind != "initial" or raw_result.verdict != "repair_once":
                return False
            authorization = self._read_repair_authorization()
            progress = self._read_repair_progress(authorization)
            return bool(progress.corrected_record_hashes or progress.removed_record_ids) and progress.current_packet_hash is None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False

    def _diagnostic_fidelity_result(self) -> FidelityResult | None:
        try:
            return self._read_fidelity_result()
        except ValueError:
            if self._active_corrected_repair_for_diagnostics():
                return None
            raise

    @property
    def fidelity_result(self) -> FidelityResult | None:
        return self._diagnostic_fidelity_result()

    @property
    def fidelity_review(self) -> FidelityResult | None:
        return self._diagnostic_fidelity_result()

    @property
    def fidelity_packet(self) -> IntegrationFidelityPacket | None:
        return self._read_fidelity_packet()

    def _normalize_finding_values(self, findings: Iterable[Any], *, known_ids: set[str]) -> tuple[FidelityFinding, ...]:
        parsed = tuple(FidelityFinding.from_value(value, index) for index, value in enumerate(findings or ()))
        for finding in parsed:
            for record_id in (*finding.record_ids, *finding.dependency_ids):
                if record_id not in known_ids:
                    raise ValueError(f"fidelity finding references unknown record: {record_id}")
        return parsed

    def record_fidelity_review(
        self,
        verdict: str,
        *,
        findings: Iterable[Any] = (),
        affected_record_ids: Iterable[str] = (),
        dependency_ids: Iterable[str] = (),
        checked_record_ids: Iterable[str] | None = None,
        review_kind: str | None = None,
    ) -> FidelityResult:
        """Persist one initial or exact targeted item-only fidelity verdict."""

        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            self._require_open()
            self._ensure_bundle_current()
            existing = self._read_fidelity_result_raw() if self.fidelity_result_path.exists() else None
            current_records_hash = _sha256_bytes(self._records_bytes())
            current_hashes = self._current_record_hashes()
            known_ids = set(current_hashes)
            verdict = str(verdict).strip()
            if verdict not in {"accept", "accept_with_limits", "repair_once", "unavailable", "fail"}:
                raise ValueError("fidelity verdict is invalid")
            if review_kind is None:
                review_kind = "targeted" if existing is not None and existing.verdict == "repair_once" else "initial"
            if review_kind not in {"initial", "targeted"}:
                raise ValueError("fidelity review kind is invalid")
            if review_kind == "initial":
                if existing is not None:
                    if (
                        existing.verdict in {"accept", "accept_with_limits"}
                        and existing.records_hash == current_records_hash
                        and verdict == existing.verdict
                    ):
                        packet = self._read_fidelity_packet()
                        self._exact_checked_record_ids(checked_record_ids, known_ids, label="initial fidelity")
                        return existing
                    if existing.verdict == "repair_once":
                        raise ValueError("initial fidelity repair is already recorded; use targeted recheck")
                    if existing.review_kind == "targeted":
                        raise ValueError("targeted fidelity recheck is terminal")
                parsed_findings = self._normalize_finding_values(findings, known_ids=known_ids)
                raw_affected = tuple(affected_record_ids)
                raw_dependencies = tuple(dependency_ids)
                affected_values = (*raw_affected, *(item for finding in parsed_findings for item in finding.record_ids))
                dependency_values = (*raw_dependencies, *(item for finding in parsed_findings for item in finding.dependency_ids))
                affected = tuple(_validate_record_id(value) for value in affected_values)
                dependencies = tuple(_validate_record_id(value) for value in dependency_values)
                if len(affected) != len(set(affected)) or len(dependencies) != len(set(dependencies)):
                    raise ValueError("fidelity review record IDs contain duplicates")
                if set(affected) & set(dependencies):
                    raise ValueError("fidelity review affected and dependency record IDs overlap")
                if any(value not in known_ids for value in (*affected, *dependencies)):
                    raise ValueError("fidelity review references unknown record")
                if verdict in {"accept", "accept_with_limits", "repair_once"}:
                    checked = self._exact_checked_record_ids(
                        checked_record_ids,
                        known_ids,
                        label="initial fidelity",
                    )
                else:
                    checked = tuple()
                if verdict == "repair_once" and not affected:
                    raise ValueError("repair_once fidelity result requires affected records")
                raw_packet = self._read_fidelity_packet_raw()
                packet = (
                    self._read_fidelity_packet()
                    if raw_packet is not None and raw_packet.records_hash == current_records_hash
                    else self._build_fidelity_packet()
                )
            else:
                if existing is None or existing.review_kind != "initial" or existing.verdict != "repair_once":
                    raise ValueError("targeted fidelity recheck requires an initial repair_once result")
                authorization = self._read_repair_authorization()
                progress = self._read_repair_progress(authorization)
                self._assert_repair_snapshot(authorization, progress, current_hashes)
                completed = set(progress.corrected_record_hashes) | set(progress.removed_record_ids)
                if not set(authorization.affected_record_ids).issubset(completed):
                    raise ValueError("targeted fidelity recheck requires all authorized corrections or removals")
                expected_checked = completed | set(authorization.dependency_ids)
                checked = self._exact_checked_record_ids(
                    checked_record_ids,
                    expected_checked,
                    label="targeted fidelity",
                )
                progress = self._read_repair_progress(authorization, require_current_packet=True)
                if verdict == "repair_once":
                    raise ValueError("targeted fidelity recheck cannot authorize another repair")
                parsed_findings = self._normalize_finding_values(
                    findings,
                    known_ids=known_ids | set(progress.removed_record_ids),
                )
                if affected_record_ids and set(affected_record_ids) != set(authorization.affected_record_ids):
                    raise ValueError("targeted fidelity affected records do not match authorization")
                if dependency_ids and set(dependency_ids) != set(authorization.dependency_ids):
                    raise ValueError("targeted fidelity dependencies do not match authorization")
                affected = authorization.affected_record_ids
                dependencies = authorization.dependency_ids
                # Only now, after every authorized correction is complete, is
                # the new packet allowed to replace the initial packet binding.
                packet = self._build_fidelity_packet()
            result_unsigned = {
                "schema_version": "1",
                "item_id": self.item_id,
                "session_id": self.session_id,
                "invocation_id": self.invocation_id,
                "review_kind": review_kind,
                "verdict": verdict,
                "packet_hash": packet.packet_hash,
                "records_hash": _sha256_bytes(self._records_bytes()),
                "findings": [finding.to_dict() for finding in parsed_findings],
                "affected_record_ids": list(affected),
                "dependency_ids": list(dependencies),
                "baseline_record_hashes": current_hashes if review_kind == "initial" else dict(authorization.baseline_record_hashes),
                "checked_record_ids": list(checked),
                "created_at": _now(),
            }
            result = FidelityResult(
                schema_version="1",
                item_id=self.item_id,
                session_id=self.session_id,
                invocation_id=self.invocation_id,
                review_kind=review_kind,
                verdict=verdict,
                packet_hash=packet.packet_hash,
                records_hash=result_unsigned["records_hash"],
                findings=parsed_findings,
                affected_record_ids=affected,
                dependency_ids=dependencies,
                baseline_record_hashes=result_unsigned["baseline_record_hashes"],
                checked_record_ids=checked,
                created_at=result_unsigned["created_at"],
                result_hash=_fidelity_digest(result_unsigned),
            )
            write_result(self.fidelity_result_path, result)
            if review_kind == "initial" and verdict == "repair_once":
                authorization_unsigned = {
                    "schema_version": _SCHEMA_VERSION,
                    "item_id": self.item_id,
                    "session_id": self.session_id,
                    "invocation_id": self.invocation_id,
                    "initial_packet_hash": packet.packet_hash,
                    "initial_result_hash": result.result_hash,
                    "baseline_record_hashes": dict(current_hashes),
                    "affected_record_ids": sorted(affected),
                    "dependency_ids": sorted(dependencies),
                    "created_at": _now(),
                }
                authorization = FidelityRepairAuthorization(
                    schema_version=_SCHEMA_VERSION,
                    item_id=self.item_id,
                    session_id=self.session_id,
                    invocation_id=self.invocation_id,
                    initial_packet_hash=packet.packet_hash,
                    initial_result_hash=result.result_hash,
                    baseline_record_hashes=dict(current_hashes),
                    affected_record_ids=tuple(sorted(affected)),
                    dependency_ids=tuple(sorted(dependencies)),
                    created_at=authorization_unsigned["created_at"],
                    authorization_hash=_fidelity_digest(authorization_unsigned),
                )
                write_repair_authorization(self.fidelity_authorization_path, authorization)
                self._write_repair_progress(
                    self.fidelity_progress_path,
                    authorization,
                    {},
                    current_records_hash=current_records_hash,
                    current_packet_hash=packet.packet_hash,
                )
            return result

    review_fidelity = record_fidelity_review
    record_integration_review = record_fidelity_review
    accept_fidelity = record_fidelity_review

    def _require_fidelity_acceptance(self) -> None:
        result = self._read_fidelity_result()
        if result is None or result.verdict not in {"accept", "accept_with_limits"}:
            raise ValueError("integration commit requires durable fidelity acceptance")

    def _assert_fidelity_correction_scope(
        self,
        target: str,
    ) -> tuple[FidelityRepairAuthorization | None, FidelityRepairProgress | None]:
        result = self._read_fidelity_result_raw()
        if result is None:
            # A session can arrive here with a legacy/partially materialized
            # staged record that is already mechanically invalid.  Permit the
            # owner to repair that record once before a fidelity packet/result
            # exists, but never use this path as an arbitrary edit mechanism
            # for an otherwise valid pre-fidelity session.
            validation, invalid_record_ids = self._validate_with_invalid_record_ids()
            if not validation.valid and target in invalid_record_ids:
                return None, None
            raise ValueError("record correction requires an item fidelity repair_once result")
        if result.review_kind != "initial" or result.verdict != "repair_once":
            raise ValueError("record correction requires an item fidelity repair_once result")
        authorization = self._read_repair_authorization()
        progress = self._read_repair_progress(authorization)
        current_hashes = self._current_record_hashes()
        self._assert_repair_snapshot(authorization, progress, current_hashes)
        if target in set(authorization.dependency_ids):
            raise ValueError("record correction target is a fidelity dependency, not an affected record")
        if target not in set(authorization.affected_record_ids):
            raise ValueError("record correction is outside the affected fidelity finding scope")
        if target in progress.removed_record_ids:
            raise ValueError("record correction target was already removed")
        if target in progress.corrected_record_hashes:
            raise ValueError("record correction target was already corrected")
        baseline = authorization.baseline_record_hashes.get(target)
        if baseline is None:
            raise ValueError("record correction target has no fidelity baseline")
        return authorization, progress

    def _assert_fidelity_removal_scope(
        self,
        target: str,
    ) -> tuple[FidelityRepairAuthorization, FidelityRepairProgress, str, bool]:
        """Validate one authorized initial-repair removal.

        The boolean return marks an exact retry of an already durable removal.
        Retries still traverse all authorization, progress, and snapshot
        bindings but do not rewrite any bytes.
        """

        result = self._read_fidelity_result_raw()
        if result is None or result.review_kind != "initial" or result.verdict != "repair_once":
            raise ValueError("record removal requires an item fidelity repair_once result")
        authorization = self._read_repair_authorization()
        progress = self._read_repair_progress(authorization)
        current_hashes = self._current_record_hashes()
        self._assert_repair_snapshot(authorization, progress, current_hashes)
        baseline = authorization.baseline_record_hashes.get(target)
        if baseline is None:
            raise ValueError("record removal target has no fidelity baseline")
        if target in authorization.dependency_ids:
            raise ValueError("record removal target is a fidelity dependency, not an affected record")
        if target not in authorization.affected_record_ids:
            raise ValueError("record removal is outside the fidelity finding scope")
        if target in progress.corrected_record_hashes:
            raise ValueError("record removal target was already corrected")
        if target in progress.removed_record_ids:
            if target in current_hashes:
                raise ValueError("record removal target is present after durable removal")
            return authorization, progress, baseline, True
        if target not in current_hashes:
            raise ValueError("record removal target is already missing without durable removal")
        return authorization, progress, baseline, False

    def _require_open(self) -> None:
        if self.status != "open":
            raise ValueError("integration session is terminal")
        if self._lease is None:
            raise ValueError("integration invocation lease is released")

    @staticmethod
    def _payload(value: Any, label: str) -> dict[str, Any]:
        if isinstance(value, Mapping):
            payload = _jsonable(dict(value))
        else:
            raise TypeError(f"{label} must be a mapping")
        if not isinstance(payload, Mapping):
            raise TypeError(f"{label} payload is invalid")
        # Round-trip through JSON to reject NaN, custom objects, and bytes
        # before a record can reach the durable staging file.
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            decoded = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TypeError(f"{label} payload is not JSON-safe") from exc
        return dict(decoded)

    @staticmethod
    def _scope(scope: Any, payload: Mapping[str, Any] | None = None) -> str:
        value = scope if scope is not None else (payload or {}).get("scope")
        if value is None:
            raise ValueError("integration scope is required")
        return _validate_scope(value)

    def _prepared_candidate_root(self) -> Path:
        """Return the only location where this item may stage a candidate."""

        root = self.item_workspace.work_root / "prepared"
        _assert_no_symlink(root, label="prepared candidate root")
        # Check the lexical work/item components as well as the resolved root;
        # a symlink that points back inside the item is still not an accepted
        # candidate boundary.
        current = self.item_workspace.item_root
        try:
            relative = root.relative_to(current)
        except ValueError as exc:  # pragma: no cover - defensive ItemWorkspace contract
            raise AllowedRootError("prepared candidate root escapes item workspace") from exc
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise AllowedRootError("prepared candidate root cannot use symlinks")
        return root

    def _resolve_prepared_candidate(self, descriptor: PreparedAssetDescriptor) -> Path:
        """Resolve and constrain one staged candidate to item ``work/prepared``."""

        root = self._prepared_candidate_root()
        raw = Path(descriptor.location).expanduser()
        if raw.is_absolute():
            lexical = raw
        else:
            # Workbench descriptors are absolute in normal operation.  The
            # relative forms below keep the public contract deterministic for
            # manually-created offline candidates and recovery fixtures.
            if raw.parts and raw.parts[0] in {"questions", "requirements"}:
                lexical = self.context.run_root / raw
            elif raw.parts and raw.parts[0] == "work":
                lexical = self.item_workspace.item_root / raw
            else:
                lexical = root / raw
        try:
            candidate = self.context.resolve_run_path(lexical)
            resolved_root = self.context.resolve_run_path(root)
        except Exception as exc:
            raise AllowedRootError("prepared candidate location escapes run context") from exc
        if resolved_root != candidate and resolved_root not in candidate.parents:
            raise AllowedRootError("prepared candidate must be under item work/prepared")
        _assert_no_symlink(candidate, label="prepared candidate")
        if not candidate.is_file():
            raise ValueError("prepared candidate must be a regular file")
        # Do not follow lexical parent symlinks even when their resolved target
        # happens to remain inside the item root.
        current = self.item_workspace.item_root
        try:
            relative = lexical.relative_to(current)
        except ValueError:
            try:
                relative = lexical.relative_to(self.context.run_root)
            except ValueError as exc:
                raise AllowedRootError("prepared candidate location escapes item workspace") from exc
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise AllowedRootError("prepared candidate cannot use symlinks")
        return candidate

    def _prepared_descriptor_for_path(self, relative: str) -> PreparedAssetDescriptor | None:
        """Return a staged descriptor whose candidate path is ``relative``."""

        try:
            path = self._resolve_item_ref(relative)
        except (AllowedRootError, FileNotFoundError, ValueError):
            return None
        for record in self._records:
            if record.kind != "prepared_asset":
                continue
            try:
                descriptor = PreparedAssetDescriptor.from_dict(record.payload)
                candidate = self._resolve_prepared_candidate(descriptor)
            except Exception:
                continue
            if candidate == path:
                return descriptor
        return None

    def _evidence(
        self,
        evidence_refs: Any,
        *,
        required: bool = True,
        prepared_descriptor: PreparedAssetDescriptor | None = None,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        # Evidence authorization is bound to the exact accepted bytes and
        # manifest at the moment refs are resolved, including link_evidence,
        # which performs its check before the generic staging helper.
        self._ensure_bundle_current()
        if evidence_refs is None:
            evidence_refs = ()
        if isinstance(evidence_refs, (str, Mapping)):
            evidence_refs = (evidence_refs,)
        refs: list[str] = []
        hashes: dict[str, str] = {}
        for raw in evidence_refs:
            supplied_hash: str | None = None
            if isinstance(raw, Mapping):
                ref = raw.get("ref", raw.get("path", raw.get("evidence_ref")))
                supplied_hash = raw.get("hash", raw.get("content_hash"))
            else:
                ref = raw
            ref = _validate_evidence_ref(ref)
            if ref in refs:
                raise ValueError("duplicate evidence reference")
            if ref in self._by_id:
                actual_hash = self._by_id[ref].record_hash
            else:
                relative = _safe_relative_ref(ref, "evidence reference")
                if relative.startswith(_SEMANTIC_SELECTION_PREFIX):
                    # Semantic selections are run-global, immutable,
                    # content-addressed artifacts.  They are not accepted
                    # item files, so the accepted-bundle hash map cannot be
                    # their authority.  The resolver below binds the
                    # selection to this item's accepted analysis context and
                    # local selection journal before returning its file hash.
                    self._validate_semantic_selection_evidence(relative)
                    path = self.context.resolve_run_path(relative)
                    actual_hash = _sha256_bytes(path.read_bytes())
                else:
                    expected = self._evidence_expected_hash(relative, prepared_descriptor=prepared_descriptor)
                    path = self._resolve_item_ref(relative)
                    actual_hash = _sha256_bytes(path.read_bytes())
                    if expected != actual_hash:
                        raise ValueError(f"evidence changed after acceptance: {relative}")
            if supplied_hash is not None and str(supplied_hash) != actual_hash:
                raise ValueError(f"evidence hash mismatch: {ref}")
            refs.append(ref)
            hashes[ref] = actual_hash
        if required and not refs:
            raise ValueError("integration evidence is required")
        return tuple(refs), hashes

    def _evidence_expected_hash(
        self,
        relative: str,
        *,
        prepared_descriptor: PreparedAssetDescriptor | None = None,
    ) -> str:
        if relative in {"answer_content.json", "accepted/answer_content.json"}:
            return self.bundle.content_hash
        progress = self.bundle.manifest.get("artifact_progress", {})
        hashes = progress.get("hashes", {}) if isinstance(progress, Mapping) else {}
        if relative in hashes:
            return str(hashes[relative])
        if relative.startswith("accepted/"):
            name = relative.split("/", 1)[1]
            if name == "acceptance_envelope.json":
                return self.bundle.envelope_hash
        # A prepared candidate may be created after analytical acceptance, so
        # it need not appear in the accepted artifact-progress hash map.  Its
        # descriptor is the explicit mechanical evidence binding instead.
        if prepared_descriptor is not None:
            candidate = self._resolve_prepared_candidate(prepared_descriptor)
            if self._resolve_item_ref(relative) == candidate and prepared_descriptor.prepared_content_hash:
                return prepared_descriptor.prepared_content_hash
        staged = self._prepared_descriptor_for_path(relative)
        if staged is not None and staged.prepared_content_hash:
            return staged.prepared_content_hash
        raise ValueError(f"evidence reference is not bound by accepted manifest: {relative}")

    @staticmethod
    def _semantic_selection_hash(relative: str) -> str:
        """Return the hash component of one canonical selection reference.

        Only the semantic-selection namespace is handled here.  Other
        run-relative paths continue through the accepted-manifest resolver;
        this prevents a generic run-root read from becoming evidence merely
        because the file happens to exist.
        """

        if not relative.startswith(_SEMANTIC_SELECTION_PREFIX):
            raise ValueError("semantic selection reference is outside its namespace")
        name = relative[len(_SEMANTIC_SELECTION_PREFIX) :]
        if "/" in name or not name.endswith(".json"):
            raise ValueError("semantic selection reference is not canonical")
        selection_hash = name[: -len(".json")]
        if not _is_sha256(selection_hash):
            raise ValueError("semantic selection reference is not canonical")
        return selection_hash

    def _accepted_analysis_context_for_semantic_selection(
        self,
    ) -> tuple[SemanticSnapshotRef, str, list[Mapping[str, Any]]]:
        """Load the accepted item's exact semantic snapshot context.

        The context and selection journal are ordinary item-local artifacts,
        therefore resolving them through ``_evidence`` proves their bytes are
        still the bytes bound by the immutable accepted manifest.  The
        semantic snapshot itself is then checked by the content-addressed
        store, which validates its manifest, layer indexes and path safety.
        """

        self._evidence((_ANALYSIS_CONTEXT_REF,), required=True)
        context_path = self._resolve_item_ref(_ANALYSIS_CONTEXT_REF)
        context_bytes = context_path.read_bytes()
        try:
            value = json.loads(context_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("accepted analysis context is invalid") from exc
        # The existing BoundAnalysisContext loader hash-binds the compact
        # unsigned object but intentionally permits a pretty-printed file
        # (older accepted runs contain that representation).  Preserve that
        # public contract here; the accepted artifact hash above still binds
        # the exact on-disk bytes, and semantic-store selections remain
        # strict content-addressed JSON.
        if not isinstance(value, Mapping):
            raise ValueError("accepted analysis context is not an object")
        manifest_hash = value.get("manifest_hash")
        unsigned = dict(value)
        unsigned.pop("manifest_hash", None)
        if not _is_sha256(manifest_hash) or _analysis_context_manifest_hash(unsigned) != manifest_hash:
            raise ValueError("accepted analysis context manifest hash does not match")
        expected_manifest_path = self.item_workspace.work_root / "analysis_context.json"
        if (
            value.get("schema_version") != "3"
            or value.get("kind") != "bound_analysis_context"
            or value.get("run_id") != self.context.run_id
            or value.get("run_root") != str(self.context.run_root)
            or value.get("item_id") != self.item_id
            or value.get("item_mode") != getattr(self.item_workspace, "mode", None)
            or value.get("manifest_path") != str(expected_manifest_path)
        ):
            raise ValueError("accepted analysis context provenance does not match integration item")
        try:
            snapshot_ref = SemanticSnapshotRef.from_dict(value.get("semantic_snapshot"))
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("accepted analysis context semantic snapshot is invalid") from exc
        checked = SemanticSnapshotStore.read_ref(self.context, snapshot_ref)
        if checked is None:  # pragma: no cover - type narrowing guard
            raise ValueError("accepted analysis context semantic snapshot is missing")

        # The local selection journal is accepted and hash-bound independently
        # of the global content-addressed selection.  It is append-only
        # history: older records may bind prior snapshots/contexts, while the
        # resolver below selects exactly one final-context edge for the
        # referenced selection.
        self._evidence((_SEMANTIC_SELECTIONS_REF,), required=True)
        selections_path = self._resolve_item_ref(_SEMANTIC_SELECTIONS_REF)
        try:
            selection_lines = selections_path.read_bytes().splitlines(keepends=True)
        except OSError as exc:  # pragma: no cover - _evidence already opens it
            raise ValueError("accepted semantic selection journal is unreadable") from exc
        if not selection_lines:
            raise ValueError("accepted semantic selection journal is empty")
        journal_records: list[Mapping[str, Any]] = []
        for line_number, line in enumerate(selection_lines, 1):
            if not line.strip():
                raise ValueError(f"accepted semantic selection journal line {line_number} is empty")
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"accepted semantic selection journal line {line_number} is invalid") from exc
            if not isinstance(record, Mapping) or line != _canonical_bytes(record):
                raise ValueError(f"accepted semantic selection journal line {line_number} is not canonical")
            if record.get("record_kind") != "semantic_selection":
                raise ValueError(f"accepted semantic selection journal line {line_number} has an invalid kind")
            item_id = record.get("item_id")
            try:
                _validate_record_id(item_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid item identity") from exc
            owner_ref = record.get("owner_ref")
            if (
                not isinstance(owner_ref, str)
                or not owner_ref.strip()
                or owner_ref != owner_ref.strip()
                or Path(owner_ref).name != owner_ref
                or "\\" in owner_ref
                or "\x00" in owner_ref
                or "\n" in owner_ref
                or "\r" in owner_ref
            ):
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid owner_ref")
            selection_kind = record.get("selection_kind")
            if not isinstance(selection_kind, str) or not selection_kind.strip() or selection_kind != selection_kind.strip():
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid selection_kind")
            purpose = record.get("purpose")
            if not isinstance(purpose, str) or not purpose.strip() or purpose != purpose.strip():
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid purpose")
            selection_hash = record.get("selection_hash")
            selection_ref = record.get("selection_ref")
            if (selection_ref is None) != (selection_hash is None):
                raise ValueError(f"accepted semantic selection journal line {line_number} has incomplete selection identity")
            if selection_ref is not None:
                if not isinstance(selection_hash, str) or not _is_sha256(selection_hash) or not isinstance(selection_ref, str):
                    raise ValueError(f"accepted semantic selection journal line {line_number} has invalid selection identity")
                if selection_ref != f"{_SEMANTIC_SELECTION_PREFIX}{selection_hash}.json":
                    raise ValueError(f"accepted semantic selection journal line {line_number} has non-canonical selection_ref")
            selection_id = record.get("selection_id")
            if not isinstance(selection_id, str) or not _is_sha256(selection_id):
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid selection_id")
            unsigned_record = dict(record)
            unsigned_record.pop("selection_id", None)
            if _semantic_selection_journal_hash(unsigned_record) != selection_id:
                raise ValueError(f"accepted semantic selection journal line {line_number} selection_id does not match content")
            optional_registry_hash = record.get("registry_hash")
            if optional_registry_hash is not None and not _is_sha256(optional_registry_hash):
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid registry_hash")
            snapshot_hash = record.get("snapshot_hash")
            context_hash = record.get("context_manifest_hash")
            if not _is_sha256(snapshot_hash) or not _is_sha256(context_hash):
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid context binding")
            counts = record.get("selection_counts")
            if (
                not isinstance(counts, Mapping)
                or set(counts) != _SEMANTIC_SELECTION_COUNT_NAMES
                or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts.values())
            ):
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid selection_counts")
            if selection_ref is None and any(counts.values()):
                raise ValueError(f"accepted semantic selection journal line {line_number} has counts without a selection")
            optional_run_id = record.get("run_id")
            if optional_run_id is not None:
                try:
                    _safe_component(optional_run_id, "semantic selection run_id")
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"accepted semantic selection journal line {line_number} has invalid run_id") from exc
            optional_mode = record.get("item_mode")
            if optional_mode is not None and optional_mode not in {"question", "requirement"}:
                raise ValueError(f"accepted semantic selection journal line {line_number} has invalid item_mode")
            journal_records.append(record)
        return checked, str(manifest_hash), journal_records

    def _validate_semantic_selection_evidence(self, relative: str) -> Mapping[str, tuple[str, ...]]:
        """Validate one run-local semantic selection as accepted evidence.

        ``SemanticSnapshotStore.load_selection`` is the sole authority for
        selection bytes, snapshot binding, layer IDs, and symlink-safe paths.
        This wrapper adds the item-level accepted-context and journal edge, so
        a valid selection from another item/snapshot cannot be smuggled in as
        generic run-root evidence.
        """

        selection_hash = self._semantic_selection_hash(relative)
        snapshot_ref, context_manifest_hash, journal = self._accepted_analysis_context_for_semantic_selection()
        candidates = [
            record
            for record in journal
            if (
                record.get("item_id") == self.item_id
                and record.get("snapshot_hash") == snapshot_ref.snapshot_hash
                and record.get("context_manifest_hash") == context_manifest_hash
                and record.get("selection_ref") == relative
                and record.get("selection_hash") == selection_hash
            )
        ]
        if len(candidates) != 1:
            raise ValueError(
                "semantic selection is not bound exactly once to this accepted item's current selection journal"
            )
        matching = [
            record
            for record in candidates
            if record.get("run_id") in {None, self.context.run_id}
            and record.get("item_mode") in {None, getattr(self.item_workspace, "mode", None)}
        ]
        if len(matching) != 1:
            raise ValueError("semantic selection current journal binding has contradictory run/mode provenance")
        # ``RunContext.resolve_run_path`` returns a resolved path.  Check the
        # lexical path first so a symlink at any semantic-store component is
        # rejected before that resolution can erase the evidence of the link.
        current = self.context.run_root
        for component in PurePath(relative).parts:
            current = current / component
            if current.is_symlink():
                raise AllowedRootError(f"semantic selection cannot use symlink components: {current}")
        sets = SemanticSnapshotStore.load_selection(
            self.context,
            snapshot_ref,
            relative,
            selection_hash,
        )
        record_counts = dict(matching[0]["selection_counts"])
        actual_counts = {name: len(values) for name, values in sets.items()}
        if record_counts != actual_counts:
            raise ValueError("semantic selection journal counts do not match selection")
        if matching[0].get("context_manifest_hash") != context_manifest_hash:  # pragma: no cover - checked above
            raise ValueError("semantic selection context provenance is invalid")
        return sets

    def _resolve_item_ref(self, relative: str) -> Path:
        # ``answer_content.json`` is a short alias for the immutable accepted
        # content.  Resolve it inside ``accepted/`` rather than at the item
        # root, where a stale draft or unrelated file could be substituted.
        if relative == "answer_content.json":
            destination = self.item_workspace.accepted_root / "answer_content.json"
        else:
            destination = self.item_workspace.item_root / Path(relative)
        _assert_no_symlink(destination, label="evidence artifact")
        if not destination.is_file():
            raise FileNotFoundError(destination)
        return destination

    def _ontology_preview(
        self,
        records: Sequence[IntegrationRecord] | None = None,
    ) -> LivingEnterpriseModel:
        """Build the authoritative prior-plus-staged ontology view."""

        prior = self._reproject_prior()
        preview = LivingEnterpriseModel.from_export(prior.export())
        for record in self._records if records is None else records:
            if record.kind == "ontology_item":
                preview.ensure_ontology_item(record.payload)
        return preview

    def _canonical_ontology_payload(
        self,
        payload: Mapping[str, Any],
        *,
        exclude_record_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate one ontology payload against the canonical ensure view.

        The durable integration record keeps the requirement-specific payload
        intact.  Canonical merging is performed by replay into the shared LEM;
        replacing this payload with the merged view would erase later wording
        and could collapse a distinct reviewed record into an earlier retry.
        """

        records = (
            record
            for record in self._records
            if exclude_record_id is None or record.record_id != exclude_record_id
        )
        preview = self._ontology_preview(tuple(records))
        preview.ensure_ontology_item(payload)
        return dict(payload)

    def _stage(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        scope: Any,
        evidence_refs: Any,
        record_id: str | None = None,
    ) -> str:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._stage_unlocked(kind, payload, scope=scope, evidence_refs=evidence_refs, record_id=record_id)

    def _stage_unlocked(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        scope: Any,
        evidence_refs: Any,
        record_id: str | None = None,
    ) -> str:
        self._require_open()
        self._ensure_bundle_current()
        current_result = self._read_fidelity_result() if self.fidelity_result_path.exists() else None
        if current_result is not None and current_result.verdict == "repair_once":
            raise ValueError("new integration records are not allowed during a fidelity repair")
        if kind not in _RECORD_KINDS:
            raise ValueError("unsupported integration record kind")
        normalized_payload = self._payload(payload, kind)
        normalized_scope = self._scope(scope, normalized_payload)
        if kind == "ontology_item":
            normalized_payload = self._canonical_ontology_payload(normalized_payload)
        prepared_descriptor = None
        existing_artifact_record: IntegrationRecord | None = None
        if kind == "analytical_artifact":
            # Re-parse at the staging boundary with the session item binding;
            # IntegrationRecord.from_dict can only validate the self-contained
            # payload and deliberately has no item context.
            artifact, artifact_bytes = _parse_analytical_artifact_payload(
                normalized_payload,
                expected_item_id=self.item_id,
            )
            self._validate_external_artifact_outputs(artifact)
            artifact_identity = (
                artifact.artifact_id,
                artifact.artifact_type,
                artifact.schema_version,
                artifact.requirement_id,
                artifact.content_hash,
                artifact.envelope_hash,
                _sha256_bytes(artifact_bytes),
                normalized_payload.get("artifact_ref"),
            )
            # One artifact identity and one committed reference are allowed
            # within an item.  An exact retry is resolved to the original
            # record below, even when a caller supplies a different record ID;
            # it must never append a second record that can poison a later
            # commit after LEM application.
            for candidate_record in self._records:
                if candidate_record.kind != "analytical_artifact":
                    continue
                existing_payload = candidate_record.payload
                existing_artifact, existing_bytes = _parse_analytical_artifact_payload(
                    existing_payload,
                    expected_item_id=self.item_id,
                )
                existing_identity = (
                    existing_artifact.artifact_id,
                    existing_artifact.artifact_type,
                    existing_artifact.schema_version,
                    existing_artifact.requirement_id,
                    existing_artifact.content_hash,
                    existing_artifact.envelope_hash,
                    _sha256_bytes(existing_bytes),
                    existing_payload.get("artifact_ref"),
                )
                if existing_artifact.artifact_id == artifact.artifact_id:
                    if existing_identity != artifact_identity or existing_bytes != artifact_bytes:
                        raise ValueError(f"analytical artifact ID collision: {artifact.artifact_id}")
                    if existing_artifact_record is not None:
                        raise ValueError(f"analytical artifact ID is staged more than once: {artifact.artifact_id}")
                    existing_artifact_record = candidate_record
                elif existing_payload.get("artifact_ref") == normalized_payload.get("artifact_ref"):
                    raise ValueError(
                        "analytical artifact committed reference collision: "
                        f"{normalized_payload.get('artifact_ref')}"
                    )
        if kind == "prepared_asset":
            prepared_descriptor = PreparedAssetDescriptor.from_dict(normalized_payload)
            self._resolve_prepared_candidate(prepared_descriptor)
            self.prepared_registry.preflight_candidate(prepared_descriptor, self.item_workspace)
        refs, hashes = self._evidence(
            evidence_refs,
            required=kind != "prepared_asset",
            prepared_descriptor=prepared_descriptor,
        )
        if kind == "knowledge_delta":
            delta = self._knowledge_delta_from_payload(normalized_payload)
            if tuple(delta.evidence_refs) != refs:
                raise ValueError("knowledge delta evidence_refs must match integration evidence_refs")
            if (
                delta.operation == "add_relationship"
                and _RELATIONSHIP_REUSE_FIELD in delta.payload
            ):
                raise ValueError(
                    "knowledge delta add_relationship cannot use "
                    "reuse_existing_relationship_id; use typed add_relationship"
                )
        body = {
            "kind": kind,
            "item_id": self.item_id,
            "accepted_content_hash": self.bundle.content_hash,
            "scope": normalized_scope,
            "evidence_refs": list(refs),
            "evidence_hashes": hashes,
            "payload": normalized_payload,
        }
        semantic_key = (
            normalized_payload.get("item_id")
            or normalized_payload.get("metric_id")
            or normalized_payload.get("definition_id")
            or normalized_payload.get("relationship_id")
            or normalized_payload.get("analysis_relationship_id")
            or normalized_payload.get("decision_id")
            or normalized_payload.get("canonical_id")
            or normalized_payload.get("delta_id")
            or normalized_payload.get("prepared_asset_id")
            or normalized_payload.get("claim_id")
            or normalized_payload.get("id")
            or normalized_scope
        )
        generated_id = deterministic_record_id(kind, semantic_key, normalized_payload)
        normalized_id = _validate_record_id(record_id) if record_id is not None else generated_id
        body["record_id"] = normalized_id
        record_hash = _sha256_value(body)
        record = IntegrationRecord(
            record_id=normalized_id,
            kind=kind,
            item_id=self.item_id,
            accepted_content_hash=self.bundle.content_hash,
            scope=normalized_scope,
            evidence_refs=refs,
            evidence_hashes=hashes,
            payload=_copy_mapping(normalized_payload),
            record_hash=record_hash,
        )
        existing = self._by_id.get(normalized_id)
        if existing is not None:
            if existing != record:
                raise ValueError(f"integration record ID collision: {normalized_id}")
            removed_hashes = self._state.get(_UNREVIEWED_REMOVED_RECORD_HASHES, {})
            if normalized_id in removed_hashes:
                state = dict(self._state)
                remaining = dict(removed_hashes)
                remaining.pop(normalized_id, None)
                if remaining:
                    state[_UNREVIEWED_REMOVED_RECORD_HASHES] = remaining
                else:
                    state.pop(_UNREVIEWED_REMOVED_RECORD_HASHES, None)
                self._persist_state(state)
            return normalized_id
        if existing_artifact_record is not None:
            # The immutable artifact was already staged under another caller
            # record ID.  Return that durable ID rather than appending a
            # duplicate record; this keeps the exact retry side-effect free
            # and prevents a mixed LEM/artifact session from failing only at
            # commit after applying other records.
            return existing_artifact_record.record_id
        self._records.append(record)
        self._by_id[normalized_id] = record
        state = dict(self._state)
        removed_hashes = state.get(_UNREVIEWED_REMOVED_RECORD_HASHES, {})
        if normalized_id in removed_hashes:
            remaining = dict(removed_hashes)
            remaining.pop(normalized_id, None)
            if remaining:
                state[_UNREVIEWED_REMOVED_RECORD_HASHES] = remaining
            else:
                state.pop(_UNREVIEWED_REMOVED_RECORD_HASHES, None)
        self._persist_state(state)
        return normalized_id

    def add_claim(self, claim: Mapping[str, Any], *, scope: str | None = None, evidence_refs: Any = (), claim_id: str | None = None, **values: Any) -> str:
        payload = self._payload(claim, "claim")
        payload.update(_jsonable(values))
        return self._stage("claim", payload, scope=scope, evidence_refs=evidence_refs, record_id=claim_id)

    def add_metric(self, metric: Mapping[str, Any] | None = None, *, scope: str | None = None, evidence_refs: Any = (), metric_id: str | None = None, **values: Any) -> str:
        payload = self._payload(metric or values, "metric")
        payload.update(_jsonable(values))
        self._preview_metric(payload, self.lem.run_id)
        return self._stage("metric", payload, scope=scope, evidence_refs=evidence_refs, record_id=metric_id)

    def _add_analytical_artifact(
        self,
        artifact: AnalyticalArtifact | Mapping[str, Any],
        *,
        scope: str | None = None,
        evidence_refs: Any = (),
        artifact_record_id: str | None = None,
    ) -> str:
        """Stage one immutable typed analytical artifact internally.

        This serializer/staging seam is used only by the accepted-ref handoff
        and focused internal tests.  Public integration callers must use
        :meth:`add_accepted_analytical_artifact`, which resolves the exact
        business-accepted item-local bytes before reaching this method.  No
        LEM or prepared-registry mutation occurs.
        """

        if isinstance(artifact, AnalyticalArtifact):
            parsed = artifact
        elif isinstance(artifact, Mapping):
            try:
                parsed = AnalyticalArtifact.from_dict(artifact)
            except (AnalyticalArtifactValidationError, TypeError, ValueError) as exc:
                raise ValueError("analytical artifact is invalid") from exc
        else:
            raise TypeError("analytical artifact must be an AnalyticalArtifact or mapping")
        if parsed.requirement_id != self.item_id:
            raise ValueError("analytical artifact requirement_id does not match integration item")
        payload = self._analytical_artifact_payload(parsed)
        return self._stage(
            "analytical_artifact",
            payload,
            scope=scope,
            evidence_refs=evidence_refs,
            record_id=artifact_record_id,
        )

    def _analytical_artifact_payload(self, parsed: AnalyticalArtifact) -> dict[str, Any]:
        """Build the canonical staged envelope for one typed artifact."""

        artifact_bytes = _analytical_artifact_bytes(parsed)
        # Existing refs are consulted only to avoid filename aliasing for
        # distinct IDs that normalize to the same safe path.  Ordinary IDs
        # retain the readable ``<artifact_id>.json`` form.
        existing_ids = {
            str(record.payload.get("artifact_id"))
            for record in self._records
            if record.kind == "analytical_artifact" and isinstance(record.payload, Mapping)
        }
        payload = {
            "artifact": parsed.to_dict(),
            "artifact_id": parsed.artifact_id,
            "artifact_type": parsed.artifact_type,
            "schema_version": parsed.schema_version,
            "requirement_id": parsed.requirement_id,
            "content_hash": parsed.content_hash,
            "envelope_hash": parsed.envelope_hash,
            "canonical_bytes_sha256": _sha256_bytes(artifact_bytes),
            "artifact_ref": _analytical_artifact_ref(parsed.artifact_id, existing_ids=existing_ids),
        }
        return payload

    def _sealed_analytical_artifact(
        self,
        descriptor: Mapping[str, Any],
    ) -> tuple[AnalyticalArtifact, str]:
        """Read one accepted handoff descriptor and verify exact bytes."""

        ref = descriptor.get("ref")
        if not isinstance(ref, str) or not ref.startswith("work/"):
            raise ValueError("sealed analytical artifact reference is invalid")
        # ``_evidence`` rechecks both accepted manifest hash and lexical path
        # safety, including after a session has been resumed.
        self._evidence((ref,), required=True)
        path = self._resolve_item_ref(ref)
        raw_bytes = path.read_bytes()
        try:
            artifact = AnalyticalArtifact.from_json(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, AnalyticalArtifactValidationError, TypeError, ValueError) as exc:
            raise ValueError("sealed analytical artifact JSON is invalid") from exc
        canonical_bytes = _analytical_artifact_bytes(artifact)
        if raw_bytes != canonical_bytes:
            raise ValueError("sealed analytical artifact bytes are not canonical")
        if artifact.artifact_type not in ANALYTICAL_ARTIFACT_TYPES:
            raise ValueError("sealed analytical artifact type is unsupported")
        if artifact.requirement_id != self.item_id:
            raise ValueError("sealed analytical artifact requirement_id does not match item")
        # External output refs are part of the typed artifact contract but
        # live as separate JSONL files under the runner's admitted output
        # root.  Verify them while the accepted handoff is first sealed so a
        # generated output cannot be replaced before staging/fidelity.
        self._validate_external_artifact_outputs(artifact)
        expected = {
            "hash": _sha256_bytes(raw_bytes),
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "schema_version": artifact.schema_version,
            "requirement_id": artifact.requirement_id,
            "content_hash": artifact.content_hash,
            "envelope_hash": artifact.envelope_hash,
            "canonical_bytes_sha256": _sha256_bytes(canonical_bytes),
        }
        for name, value in expected.items():
            if descriptor.get(name) != value:
                raise ValueError(f"sealed analytical artifact descriptor {name} is stale")
        return artifact, ref

    def _auto_stage_sealed_analytical_artifacts(self) -> None:
        """Stage every sealed typed artifact before Integration Agent work."""

        for descriptor in self.bundle.analytical_artifact_handoff:
            artifact, source_ref = self._sealed_analytical_artifact(descriptor)
            payload = self._analytical_artifact_payload(artifact)
            already_staged = False
            for existing in self._records:
                if existing.kind != "analytical_artifact" or source_ref not in existing.evidence_refs:
                    continue
                try:
                    existing_artifact, existing_bytes = _parse_analytical_artifact_payload(
                        existing.payload,
                        expected_item_id=self.item_id,
                        require_committed_ref=False,
                    )
                except ValueError:
                    continue
                if existing_artifact == artifact and existing_bytes == _analytical_artifact_bytes(artifact):
                    already_staged = True
                    break
            if already_staged:
                continue
            # The source ref is the immutable accepted handoff binding.  A
            # repeated create/load sees the same generated record and is an
            # exact idempotent retry, not a second guessed declaration.
            self._stage_unlocked(
                "analytical_artifact",
                payload,
                scope="analytical_artifact",
                evidence_refs=(source_ref,),
            )

    def add_accepted_analytical_artifact(
        self,
        artifact_ref: str,
        *,
        scope: str | None = None,
        evidence_refs: Any = (),
        artifact_record_id: str | None = None,
    ) -> str:
        """Stage one canonical typed artifact from an explicitly accepted ref.

        The Analytical Owner publishes the artifact in the item work area and
        the business reviewer accepts that exact ref.  Result Integration
        resolves the accepted bytes, parses the strict typed envelope, and
        delegates staging to its internal immutable-artifact serializer; it never
        constructs or recomputes analytical content.  The accepted ref is
        always retained as evidence on the staged record.
        """

        if not isinstance(artifact_ref, str):
            raise TypeError("accepted analytical artifact reference must be a string")
        relative = _safe_relative_ref(artifact_ref, "accepted analytical artifact reference")
        if relative != artifact_ref:
            raise ValueError("accepted analytical artifact reference must be canonical")
        # Accepted analytical outputs are item-local work artifacts.  A
        # semantic-store/global ref, answer alias, accepted envelope, or
        # integration path is not an artifact handoff and must not be
        # interpreted as one merely because it appears in accepted_refs.
        if not relative.startswith("work/"):
            raise ValueError("accepted analytical artifact reference must be under item work/")
        sealed_refs = {str(descriptor.get("ref")) for descriptor in self.bundle.analytical_artifact_handoff}
        if relative not in self.bundle.accepted_refs and relative not in sealed_refs:
            raise ValueError("accepted analytical artifact reference is not in the accepted bundle")

        lexical = self.item_workspace.item_root
        for component in PurePath(relative).parts:
            lexical = lexical / component
            _assert_no_symlink(lexical, label="accepted analytical artifact")

        # Resolve through the existing accepted-manifest evidence/hash checks
        # before parsing.  This rejects post-acceptance tampering and all
        # symlinked/non-regular paths using the same boundary as every other
        # integration evidence reference.
        self._evidence((relative,), required=True)
        path = self._resolve_item_ref(relative)
        try:
            raw_bytes = path.read_bytes()
            artifact = AnalyticalArtifact.from_json(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, AnalyticalArtifactValidationError, TypeError, ValueError) as exc:
            raise ValueError("accepted analytical artifact JSON is invalid") from exc

        canonical_bytes = _analytical_artifact_bytes(artifact)
        if raw_bytes != canonical_bytes:
            raise ValueError("accepted analytical artifact bytes are not canonical")
        if artifact.artifact_type not in ANALYTICAL_ARTIFACT_TYPES:
            raise ValueError(f"accepted analytical artifact type is unsupported: {artifact.artifact_type!r}")
        if artifact.requirement_id != self.item_id:
            raise ValueError("accepted analytical artifact requirement_id does not match integration item")

        if evidence_refs is None:
            merged_evidence_refs: Any = (relative,)
        elif isinstance(evidence_refs, (str, Mapping)):
            merged_evidence_refs = (relative, evidence_refs)
        else:
            merged_evidence_refs = (relative, *tuple(evidence_refs))
        # ``create``/``load`` may already have staged this sealed descriptor.
        # Treat an explicit redeclaration as an exact idempotent retry even if
        # the caller supplies additional evidence or a different record ID.
        for existing in self._records:
            if existing.kind != "analytical_artifact" or relative not in existing.evidence_refs:
                continue
            try:
                existing_artifact, existing_bytes = _parse_analytical_artifact_payload(
                    existing.payload,
                    expected_item_id=self.item_id,
                    require_committed_ref=False,
                )
            except ValueError:
                continue
            if existing_artifact == artifact and existing_bytes == canonical_bytes:
                return existing.record_id
        return self._add_analytical_artifact(
            artifact,
            scope=scope,
            evidence_refs=merged_evidence_refs,
            artifact_record_id=artifact_record_id,
        )

    def add_metric_definition(
        self,
        definition: OntologyItem | Mapping[str, Any],
        *,
        scope: str | None = None,
        evidence_refs: Any = (),
        definition_id: str | None = None,
    ) -> str:
        """Stage a stable metric meaning as an explicit ontology item."""

        value = definition.to_dict() if isinstance(definition, OntologyItem) else self._payload(definition, "metric_definition")
        value.setdefault("item_type", "metric_definition")
        if value.get("item_type") != "metric_definition":
            raise ValueError("metric definition item_type must be metric_definition")
        return self.add_ontology_item(value, scope=scope, evidence_refs=evidence_refs, ontology_record_id=definition_id)

    def add_limitation(self, limitation: Mapping[str, Any], *, scope: str | None = None, evidence_refs: Any = (), limitation_id: str | None = None, **values: Any) -> str:
        payload = self._payload(limitation, "limitation")
        payload.update(_jsonable(values))
        return self._stage("limitation", payload, scope=scope, evidence_refs=evidence_refs, record_id=limitation_id)

    def add_knowledge_delta(
        self,
        delta: KnowledgeDelta | Mapping[str, Any],
        *,
        scope: str | None = None,
        evidence_refs: Any = None,
        delta_record_id: str | None = None,
    ) -> str:
        """Stage one accepted, evidence-bound semantic successor delta.

        The outer integration envelope remains the durable authority for the
        accepted item, scope, evidence references, and content hashes.  The
        payload is the exact canonical KnowledgeDelta representation replayed
        through LivingEnterpriseModel.apply_delta.
        """

        if isinstance(delta, KnowledgeDelta):
            candidate = delta
        else:
            raw = self._payload(delta, "knowledge_delta")
            try:
                candidate = KnowledgeDelta.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("knowledge delta payload is invalid") from exc
        if not candidate.accepted:
            raise ValueError("knowledge delta staging requires accepted=True")
        # Validate the local semantic shape without resolving predecessor
        # targets; target resolution remains an atomic commit/preflight rule.
        semantic_payload = candidate.payload
        if candidate.operation == "add_ontology_item":
            LivingEnterpriseModel(run_id=self.lem.run_id).ensure_ontology_item(semantic_payload)
        elif candidate.operation in {
            "add_metric", "add_definition", "add_rule", "add_process",
            "add_event", "add_dimension",
        }:
            preview = LivingEnterpriseModel(run_id=self.lem.run_id)
            getattr(preview, candidate.operation)(semantic_payload)
        elif candidate.operation == "add_relationship":
            # KnowledgeDelta relationship rows are evidence/audit records,
            # not a second analytical-edge contract.  Resolve the stable
            # relationship identity before inspecting the requirement-local
            # payload so a repeat can retain its exact raw wording even when
            # endpoints/analytical fields are absent or malformed.
            if _RELATIONSHIP_REUSE_FIELD in semantic_payload:
                raise ValueError(
                    "knowledge delta add_relationship cannot use "
                    "reuse_existing_relationship_id; use typed add_relationship"
                )
            self._reproject_prior()
            relationship_id = self._relationship_id_from_payload(semantic_payload)
            existing_relationship = (
                relationship_id is not None
                and relationship_id in self._known_relationship_ids()
            )
            if not existing_relationship:
                self._validate_relationship_payload(semantic_payload)
                ontology_ids, mapping_ids = self._known_relationship_endpoint_ids()
                self._preview_relationship(
                    semantic_payload,
                    self.lem.run_id,
                    known_ontology_ids=ontology_ids,
                    known_mapping_ids=mapping_ids,
                )
        elif candidate.operation == "add_prepared_asset":
            PreparedAssetDescriptor.from_dict(semantic_payload)
        elif candidate.operation == "add_canonical_mapping":
            CanonicalMapping.from_dict(semantic_payload)
        requested_refs = candidate.evidence_refs if evidence_refs is None else evidence_refs
        refs, _hashes = self._evidence(requested_refs, required=True)
        if candidate.evidence_refs and tuple(candidate.evidence_refs) != refs:
            raise ValueError("knowledge delta evidence_refs must match staged evidence_refs")
        canonical = KnowledgeDelta(
            delta_id=candidate.delta_id,
            operation=candidate.operation,
            payload=candidate.payload,
            evidence_refs=refs,
            conflicts_with=candidate.conflicts_with,
            supersedes=candidate.supersedes,
            reviewer_note=candidate.reviewer_note,
            accepted=True,
            metadata=candidate.metadata,
        )
        return self._stage(
            "knowledge_delta",
            canonical.to_dict(),
            scope=scope,
            evidence_refs=refs,
            record_id=delta_record_id,
        )

    def link_evidence(self, record_id: str, evidence_refs: Any, *, scope: str | None = None, link_id: str | None = None) -> str:
        target = _validate_record_id(record_id)
        if target not in self._by_id:
            raise KeyError(f"unknown integration record: {target}")
        refs, _hashes = self._evidence(evidence_refs, required=True)
        payload = {"target_record_id": target, "evidence_refs": list(refs)}
        return self._stage("evidence_link", payload, scope=scope or self._by_id[target].scope, evidence_refs=refs, record_id=link_id)

    def register_prepared_asset(self, descriptor: PreparedAssetDescriptor | Mapping[str, Any], *, evidence_refs: Any = (), asset_record_id: str | None = None) -> str:
        value = descriptor if isinstance(descriptor, PreparedAssetDescriptor) else PreparedAssetDescriptor.from_dict(descriptor)
        # Verify descriptor/file/hash/scope and same-ID collisions without
        # publishing registry state during staging.  The candidate remains in
        # the item work area until the accepted commit boundary.
        self._resolve_prepared_candidate(value)
        self.prepared_registry.preflight_candidate(value, self.item_workspace)
        payload = value.to_dict()
        return self._stage("prepared_asset", payload, scope=value.scope, evidence_refs=evidence_refs, record_id=asset_record_id)

    def add_ontology_item(self, item: OntologyItem | Mapping[str, Any], *, scope: str | None = None, evidence_refs: Any = (), ontology_record_id: str | None = None) -> str:
        value = item.to_dict() if isinstance(item, OntologyItem) else self._payload(item, "ontology_item")
        normalized_scope = self._scope(scope, value)
        if not value.get("scope"):
            value["scope"] = normalized_scope
        return self._stage("ontology_item", value, scope=normalized_scope, evidence_refs=evidence_refs, record_id=ontology_record_id)

    @staticmethod
    def _bind_analytical_relationship_evidence_refs(evidence_refs: Any) -> tuple[Any, ...]:
        """Bind public analytical relationship staging to its AO artifact.

        The relationship payload's ``analysis_relationship_id`` identifies a
        row in the accepted Analytical Owner artifact.  Keep caller-supplied
        evidence intact, but ensure the outer integration envelope carries
        the artifact reference required by the mechanical validator.  The
        normal ``_stage``/``_evidence`` path still authorizes the reference
        against the accepted manifest and verifies its exact hash.
        """

        artifact_ref = "work/analytical_relationships.jsonl"
        if evidence_refs is None:
            values: tuple[Any, ...] = ()
        elif isinstance(evidence_refs, (str, Mapping)):
            values = (evidence_refs,)
        else:
            values = tuple(evidence_refs)

        def reference(value: Any) -> Any:
            if isinstance(value, Mapping):
                return value.get("ref", value.get("path", value.get("evidence_ref")))
            return value

        if any(reference(value) == artifact_ref for value in values):
            return values
        return (*values, artifact_ref)

    def add_relationship(self, relationship: Mapping[str, Any], *, scope: str | None = None, evidence_refs: Any = (), relationship_record_id: str | None = None) -> str:
        value = self._payload(relationship, "relationship")
        if not value.get("relationship_id"):
            raise ValueError("relationship requires relationship_id")
        relationship_key = str(value["relationship_id"])
        # Rebuild the authoritative prior projection before deciding whether
        # this is a new edge.  A repeated ordinary ID is an idempotent audit
        # row: retain the submitted payload and defer semantic authority to
        # the stored relationship during replay.  Explicit reuse markers are
        # a separate user-facing contract and remain fully validated.
        prior = self._reproject_prior()
        existing_relationship = relationship_key in prior.relationships
        if not existing_relationship:
            existing_relationship = relationship_key in self._known_relationship_ids()
        explicit_reuse = _RELATIONSHIP_REUSE_FIELD in value
        if explicit_reuse or not existing_relationship:
            if value.get("source_id") is None or value.get("target_id") is None:
                raise ValueError("relationship requires explicit source_id and target_id")
            self._validate_relationship_payload(value)
        if explicit_reuse:
            self._validate_relationship_reuse(value)
        normalized_scope = self._scope(scope, value)
        if not value.get("scope"):
            value["scope"] = normalized_scope
        if not existing_relationship and not explicit_reuse:
            ontology_ids, mapping_ids = self._known_relationship_endpoint_ids()
            source_id = str(value["source_id"])
            target_id = str(value["target_id"])
            if (
                source_id in ontology_ids or source_id in mapping_ids
            ) and (
                target_id in ontology_ids or target_id in mapping_ids
            ):
                self._preview_relationship(
                    value,
                    self.lem.run_id,
                    known_ontology_ids=ontology_ids,
                    known_mapping_ids=mapping_ids,
                )
        if value.get("analysis_relationship_id") and not existing_relationship:
            evidence_refs = self._bind_analytical_relationship_evidence_refs(evidence_refs)
        return self._stage("relationship", value, scope=normalized_scope, evidence_refs=evidence_refs, record_id=relationship_record_id)

    def add_identity_decision(
        self,
        decision: IdentityDecision | Mapping[str, Any],
        *,
        scope: str | None = None,
        evidence_refs: Any = (),
        decision_record_id: str | None = None,
    ) -> str:
        """Stage one explicit identity decision for the accepted result.

        Identity decisions are typed contracts, not prose fragments.  Their
        review status and reviewer trace remain part of the payload so a
        later canonical mapping can bind to the exact decision by ID.
        """

        value = decision.to_dict() if isinstance(decision, IdentityDecision) else self._payload(decision, "identity_decision")
        if not isinstance(value, Mapping):  # pragma: no cover - contract guard
            raise TypeError("identity decision payload is invalid")
        normalized_scope = self._scope(scope, value)
        if not value.get("scope"):
            value["scope"] = normalized_scope
        # Reconstruct the contract after scope normalization so the
        # content-addressed decision_hash covers the exact staged value.
        normalized = IdentityDecision.from_dict(value).to_dict()
        self._identity_decision_from_payload(normalized)
        return self._stage(
            "identity_decision",
            normalized,
            scope=normalized_scope,
            evidence_refs=evidence_refs,
            record_id=decision_record_id,
        )

    def add_canonical_mapping(
        self,
        mapping: CanonicalMapping | Mapping[str, Any],
        *,
        scope: str | None = None,
        evidence_refs: Any = (),
        mapping_record_id: str | None = None,
    ) -> str:
        """Stage a canonical mapping bound to a reviewed identity decision.

        The exact decision must already be in the cumulative LEM or in an
        earlier record in this session.  This ordering rule is checked before
        staging and repeated during validation/preflight to keep recovery and
        tamper paths fail-closed.
        """

        value = mapping.to_dict() if isinstance(mapping, CanonicalMapping) else self._payload(mapping, "canonical_mapping")
        if not isinstance(value, Mapping):  # pragma: no cover - contract guard
            raise TypeError("canonical mapping payload is invalid")
        normalized_scope = self._scope(scope, value)
        if not value.get("scope"):
            value["scope"] = normalized_scope
        normalized = CanonicalMapping.from_dict(value).to_dict()
        self._validate_canonical_mapping_payload(normalized, known_decisions=self._known_identity_decisions())
        return self._stage(
            "canonical_mapping",
            normalized,
            scope=normalized_scope,
            evidence_refs=evidence_refs,
            record_id=mapping_record_id,
        )

    def add_dashboard_fact(self, fact: Mapping[str, Any], *, scope: str | None = None, evidence_refs: Any = (), fact_id: str | None = None, **values: Any) -> str:
        payload = self._payload(fact, "dashboard_fact")
        payload.update(_jsonable(values))
        return self._stage("dashboard_fact", payload, scope=scope, evidence_refs=evidence_refs, record_id=fact_id)

    def add_current_observation(
        self,
        fact: CurrentObservationFact,
        *,
        scope: str | None = None,
    ) -> str:
        """Stage an optional current number as a fact, never a definition."""

        if not isinstance(fact, CurrentObservationFact):
            raise TypeError("fact must be a CurrentObservationFact")
        return self.add_dashboard_fact(
            fact.to_dict(),
            scope=scope,
            evidence_refs=fact.evidence_refs,
            fact_id=fact.observation_id,
        )

    def correct_record(self, record_id: str, payload: Mapping[str, Any], *, evidence_refs: Any = None, scope: str | None = None) -> str:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._correct_record_unlocked(record_id, payload, evidence_refs=evidence_refs, scope=scope)

    def remove_record(self, record_id: str) -> str:
        """Remove one staged record and return its stable baseline hash.

        Before an item fidelity packet/result or commit boundary exists, the
        owner may discard any staged record.  Once a review/commit boundary is
        present, the established ``repair_once`` removal contract remains in
        force.  Both paths are idempotent for an exact retry.
        """

        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._remove_record_unlocked(record_id)

    def _remove_record_unlocked(self, record_id: str) -> str:
        self._require_open()
        self._ensure_bundle_current()
        target = _validate_record_id(record_id)
        if self._prepublication_removal_available():
            return self._remove_unreviewed_record_unlocked(target)
        authorization, progress, baseline, already_removed = self._assert_fidelity_removal_scope(target)
        intent_path = self.staging_root / _INTENT_FILENAME
        if intent_path.exists() or intent_path.is_symlink():
            raise ValueError("integration commit intent exists; corrections are closed")
        if already_removed:
            return baseline

        existing = self._by_id.get(target)
        if existing is None:  # pragma: no cover - scope assertion guards this
            raise ValueError("record removal target is already missing without durable removal")

        # Capture the exact bytes that form the correction boundary.  The
        # snapshot remains authoritative, while the projections and progress
        # file are restored byte-for-byte if either durable write fails.
        staging_paths = (
            self.staging_root / _SNAPSHOT_FILENAME,
            self.staging_root / _SESSION_FILENAME,
            self.staging_root / _RECORDS_FILENAME,
            self.fidelity_progress_path,
            self.fidelity_packet_path,
            self.fidelity_removal_intent_path,
        )
        prior_files = {
            path: path.read_bytes() if path.exists() and not path.is_symlink() else None
            for path in staging_paths
        }
        prior_records = list(self._records)
        prior_by_id = dict(self._by_id)
        prior_state = dict(self._state)
        before_records_hash = _sha256_bytes(
            b"".join(_canonical_bytes(record.to_dict()) for record in prior_records)
        )
        self._records = [record for record in self._records if record.record_id != target]
        self._by_id = {record.record_id: record for record in self._records}
        after_records_hash = _sha256_bytes(self._records_bytes())

        def restore_memory() -> None:
            self._records = prior_records
            self._by_id = prior_by_id
            self._state = prior_state

        def restore_prior() -> None:
            restore_memory()
            for path, data in prior_files.items():
                if data is None:
                    if path.exists() and not path.is_symlink():
                        path.unlink()
                else:
                    _atomic_write_bytes(path, data)

        durable_started = False
        try:
            # This is the same validation, prepared-registry preflight, and
            # side-effect-free LEM simulation used immediately before commit.
            # A reviewed repair may legitimately remove its last affected
            # record.  Validate that empty intermediate snapshot as a
            # transaction while retaining full analytical relationship
            # completeness; the durable removal progress below records the
            # explicit no-op history for final public validation.
            self._preflight_all(allow_empty_staging=True)
            durable_started = True
            self._write_fidelity_removal_intent(
                authorization,
                target=target,
                baseline_record_hash=baseline,
                before_records_hash=before_records_hash,
                after_records_hash=after_records_hash,
                phase="prepared",
            )
            self._fidelity_removal_crash_hook("after_intent")
            self._persist_state(self._state)
            self._write_fidelity_removal_intent(
                authorization,
                target=target,
                baseline_record_hash=baseline,
                before_records_hash=before_records_hash,
                after_records_hash=after_records_hash,
                phase="records_persisted",
            )
            self._fidelity_removal_crash_hook("after_records")
            self._write_repair_progress(
                self.fidelity_progress_path,
                authorization,
                progress.corrected_record_hashes,
                current_records_hash=after_records_hash,
                current_packet_hash=None,
                removed_record_ids=(*progress.removed_record_ids, target),
            )
            # Keep a compact, hash-bound marker in the session snapshot for
            # the intentional empty-result case.  The targeted result replaces
            # the initial ``repair_once`` result, so relying on that result
            # alone would make a valid remove-all repair look like a fresh
            # empty session at the subsequent commit preflight.
            removed_hashes = dict(self._state.get(_UNREVIEWED_REMOVED_RECORD_HASHES, {}))
            removed_hashes[target] = baseline
            state = dict(self._state)
            state[_UNREVIEWED_REMOVED_RECORD_HASHES] = dict(sorted(removed_hashes.items()))
            self._persist_state(state)
            self._write_fidelity_removal_intent(
                authorization,
                target=target,
                baseline_record_hash=baseline,
                before_records_hash=before_records_hash,
                after_records_hash=after_records_hash,
                phase="progress_persisted",
            )
            self._fidelity_removal_crash_hook("after_progress")
            self.fidelity_removal_intent_path.unlink()
        except Exception:
            try:
                if durable_started:
                    restore_prior()
                else:
                    restore_memory()
            except Exception:
                # Preserve the original failure while leaving the caller in a
                # fail-closed state if an unexpected rollback fault occurs.
                pass
            raise
        return baseline

    def _prepublication_removal_available(self) -> bool:
        """Return whether this open session is still before a review boundary.

        The check deliberately inspects only durable boundary artifacts.  A
        malformed or symlinked artifact is itself a boundary and therefore
        fails closed instead of being treated as an editable staging session.
        """

        boundary_paths = (
            self.fidelity_packet_path,
            self.fidelity_result_path,
            self.fidelity_authorization_path,
            self.fidelity_progress_path,
            self.fidelity_removal_intent_path,
            self.staging_root / _INTENT_FILENAME,
        )
        if any(path.exists() or path.is_symlink() for path in boundary_paths):
            return False
        if self._committed_manifest(self.item_workspace) is not None:
            return False
        return True

    def _remove_unreviewed_record_unlocked(self, target: str) -> str:
        """Discard one owner-bound staged record before fidelity review.

        ``snapshot.json`` remains the authoritative transaction boundary.  A
        small hash map in that snapshot remembers removed IDs so an exact
        retry remains a read-only, stable operation without introducing a
        second removal journal for the pre-review path.
        """

        removed_hashes = dict(self._state.get(_UNREVIEWED_REMOVED_RECORD_HASHES, {}))
        existing = self._by_id.get(target)
        if existing is None:
            baseline = removed_hashes.get(target)
            if baseline is None:
                raise ValueError("record removal target is not staged")
            return str(baseline)

        baseline = existing.record_hash
        prior_records = list(self._records)
        prior_by_id = dict(self._by_id)
        prior_state = dict(self._state)
        staging_paths = (
            self.staging_root / _SNAPSHOT_FILENAME,
            self.staging_root / _SESSION_FILENAME,
            self.staging_root / _RECORDS_FILENAME,
        )
        prior_files = {
            path: path.read_bytes() if path.exists() and not path.is_symlink() else None
            for path in staging_paths
        }
        self._records = [record for record in self._records if record.record_id != target]
        self._by_id = {record.record_id: record for record in self._records}
        try:
            # Keep the same semantic/prepared-registry checks used at commit;
            # removing a required endpoint or otherwise invalidating staging
            # must fail before any durable bytes change.
            self._preflight_all(require_complete=False, allow_empty_staging=True)
            removed_hashes[target] = baseline
            state = dict(self._state)
            state[_UNREVIEWED_REMOVED_RECORD_HASHES] = dict(sorted(removed_hashes.items()))
            self._persist_state(state)
        except Exception:
            self._records = prior_records
            self._by_id = prior_by_id
            self._state = prior_state
            for path, data in prior_files.items():
                try:
                    if data is None:
                        if path.exists() and not path.is_symlink():
                            path.unlink()
                    else:
                        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                            _atomic_write_bytes(path, data)
                except Exception:
                    # Preserve the original validation/write failure.  The
                    # authoritative snapshot remains the recovery source.
                    pass
            raise
        return baseline

    def _correct_record_unlocked(self, record_id: str, payload: Mapping[str, Any], *, evidence_refs: Any = None, scope: str | None = None) -> str:
        """Replace one same-session record while retaining deterministic identity."""
        self._require_open()
        self._ensure_bundle_current()
        target = _validate_record_id(record_id)
        existing = self._by_id.get(target)
        if existing is None:
            raw_result = self._read_fidelity_result_raw()
            if raw_result is not None and raw_result.review_kind == "initial" and raw_result.verdict == "repair_once":
                authorization = self._read_repair_authorization()
                progress = self._read_repair_progress(authorization)
                if target in progress.removed_record_ids:
                    raise ValueError("record correction target was already removed")
            raise KeyError(target)
        authorization, progress = self._assert_fidelity_correction_scope(_validate_record_id(target))
        pre_fidelity_correction = authorization is None
        before_invalid_record_ids: frozenset[str] = frozenset()
        if pre_fidelity_correction:
            # ``_assert_fidelity_correction_scope`` already established that
            # this target is mechanically invalid.  Retain that exact set so
            # the candidate check below can prove that a pre-fidelity edit
            # resolves only its target and does not introduce a new defect.
            _, before_invalid_record_ids = self._validate_with_invalid_record_ids()
        if (self.staging_root / _INTENT_FILENAME).exists() or (self.staging_root / _INTENT_FILENAME).is_symlink():
            raise ValueError("integration commit intent exists; corrections are closed")
        normalized_payload = self._payload(payload, existing.kind)
        if existing.kind == "prepared_asset":
            descriptor = PreparedAssetDescriptor.from_dict(normalized_payload)
            self._resolve_prepared_candidate(descriptor)
            self.prepared_registry.preflight_candidate(descriptor, self.item_workspace)
        elif existing.kind == "ontology_item":
            normalized_payload = self._canonical_ontology_payload(
                normalized_payload,
                exclude_record_id=target,
            )
        elif existing.kind == "metric":
            self._preview_metric(normalized_payload, self.lem.run_id)
        elif existing.kind == "relationship":
            self._validate_relationship_payload(normalized_payload)
            self._validate_relationship_reuse(normalized_payload)
            ontology_ids, mapping_ids = self._known_relationship_endpoint_ids(
                tuple(record for record in self._records if record.record_id != target)
            )
            source_id = str(normalized_payload["source_id"])
            target_id = str(normalized_payload["target_id"])
            if _RELATIONSHIP_REUSE_FIELD not in normalized_payload and (
                (source_id in ontology_ids or source_id in mapping_ids)
                and (target_id in ontology_ids or target_id in mapping_ids)
            ):
                self._preview_relationship(
                    normalized_payload,
                    self.lem.run_id,
                    known_ontology_ids=ontology_ids,
                    known_mapping_ids=mapping_ids,
                )
        elif existing.kind == "identity_decision":
            self._identity_decision_from_payload(normalized_payload)
        elif existing.kind == "canonical_mapping":
            self._validate_canonical_mapping_payload(
                normalized_payload,
                known_decisions=self._known_identity_decisions(
                    tuple(record for record in self._records if record.record_id != target)
                ),
            )
        elif existing.kind == "analytical_artifact":
            # Corrections are still a durable record boundary.  Validate the
            # complete typed envelope (including canonical bytes/ref hashes)
            # before constructing a replacement record; a generic
            # IntegrationRecord dataclass constructor intentionally does not
            # perform this parser work itself.
            artifact, artifact_bytes = _parse_analytical_artifact_payload(
                normalized_payload,
                expected_item_id=self.item_id,
            )
            for other in self._records:
                if other.record_id == target or other.kind != "analytical_artifact":
                    continue
                if other.payload.get("artifact_id") != artifact.artifact_id:
                    continue
                if (
                    other.payload.get("content_hash") != artifact.content_hash
                    or other.payload.get("envelope_hash") != artifact.envelope_hash
                    or other.payload.get("canonical_bytes_sha256") != _sha256_bytes(artifact_bytes)
                ):
                    raise ValueError(f"analytical artifact ID collision: {artifact.artifact_id}")
        normalized_scope = self._scope(scope if scope is not None else existing.scope, normalized_payload)
        refs = existing.evidence_refs if evidence_refs is None else evidence_refs
        normalized_refs, hashes = self._evidence(
            refs,
            required=existing.kind != "prepared_asset",
            prepared_descriptor=descriptor if existing.kind == "prepared_asset" else None,
        )
        body = {
            "record_id": target,
            "kind": existing.kind,
            "item_id": self.item_id,
            "accepted_content_hash": self.bundle.content_hash,
            "scope": normalized_scope,
            "evidence_refs": list(normalized_refs),
            "evidence_hashes": hashes,
            "payload": normalized_payload,
        }
        replacement = IntegrationRecord(
            record_id=target,
            kind=existing.kind,
            item_id=self.item_id,
            accepted_content_hash=self.bundle.content_hash,
            scope=normalized_scope,
            evidence_refs=normalized_refs,
            evidence_hashes=hashes,
            payload=_copy_mapping(normalized_payload),
            record_hash=_sha256_value(body),
        )
        if replacement == existing:
            raise ValueError("record correction must change the authorized record")
        index = next(index for index, record in enumerate(self._records) if record.record_id == target)
        self._records[index] = replacement
        self._by_id[target] = replacement
        durable_started = False
        try:
            if pre_fidelity_correction:
                candidate_validation, candidate_invalid_record_ids = self._validate_with_invalid_record_ids()
                candidate_invalid = set(candidate_invalid_record_ids)
                allowed_remaining = set(before_invalid_record_ids) - {target}
                introduced = candidate_invalid - allowed_remaining
                if target in candidate_invalid:
                    raise ValueError(
                        f"corrected record remains invalid: {target}; "
                        f"errors={list(candidate_validation.errors)}"
                    )
                if introduced:
                    raise ValueError(
                        "record correction introduced invalid records: "
                        + ", ".join(sorted(introduced))
                    )
                if len(candidate_invalid) >= len(before_invalid_record_ids):
                    raise ValueError("record correction must strictly reduce the invalid record set")

            durable_started = True
            self._persist_state(self._state)
            if not pre_fidelity_correction:
                assert authorization is not None and progress is not None
                self._write_repair_progress(
                    self.fidelity_progress_path,
                    authorization,
                    {
                        **dict(progress.corrected_record_hashes),
                        target: replacement.record_hash,
                    },
                    current_records_hash=_sha256_bytes(self._records_bytes()),
                    current_packet_hash=None,
                    removed_record_ids=progress.removed_record_ids,
                )
        except Exception:
            self._records[index] = existing
            self._by_id[target] = existing
            if durable_started:
                try:
                    self._persist_state(self._state)
                    if not pre_fidelity_correction:
                        assert authorization is not None and progress is not None
                        self._write_repair_progress(
                            self.fidelity_progress_path,
                            authorization,
                            progress.corrected_record_hashes,
                            current_records_hash=_sha256_bytes(self._records_bytes()),
                            current_packet_hash=progress.current_packet_hash,
                            removed_record_ids=progress.removed_record_ids,
                        )
                except Exception:
                    pass
            raise
        return target

    @staticmethod
    def _relationship_id_from_payload(payload: Mapping[str, Any]) -> str | None:
        """Derive one stable relationship identity without validating shape."""

        value = payload.get("relationship_id") or payload.get("item_id") or payload.get("id")
        return str(value) if value is not None else None

    def _known_relationship_ids(
        self,
        records: Sequence[IntegrationRecord] | None = None,
        *,
        model: LivingEnterpriseModel | None = None,
    ) -> set[str]:
        """Collect authoritative and earlier same-session relationship IDs.

        KnowledgeDelta ``add_relationship`` rows are deliberately included as
        audit identities but are never promoted to an AO analytical edge.
        Callers pass the session's already-staged prefix when order matters;
        the default is the full current record list, which is all earlier
        records at public staging boundaries.
        """

        source_model = self.lem if model is None else model
        identifiers = {str(value) for value in source_model.relationships}
        for record in records if records is not None else self._records:
            if record.kind == "relationship":
                relationship_id = self._relationship_id_from_payload(record.payload)
            elif record.kind == "knowledge_delta":
                delta = self._knowledge_delta_from_payload(record.payload)
                if delta.operation != "add_relationship":
                    continue
                relationship_id = self._relationship_id_from_payload(delta.payload)
            else:
                continue
            if relationship_id is not None:
                identifiers.add(relationship_id)
        return identifiers

    @staticmethod
    def _validate_relationship_payload(payload: Mapping[str, Any]) -> None:
        """Validate one explicit, tested analytical join payload."""

        if "coverage" in payload:
            raise ValueError("analytical relationship coverage is not a canonical field; use source_coverage and target_coverage")

        relationship_id = payload.get("relationship_id")
        if not isinstance(relationship_id, str) or not relationship_id.strip():
            raise ValueError("analytical relationship requires relationship_id")
        item_id = payload.get("item_id")
        if item_id is not None and item_id != relationship_id:
            raise ValueError("analytical relationship item_id must match relationship_id when present")
        analysis_id = payload.get("analysis_relationship_id")
        if not isinstance(analysis_id, str) or not analysis_id.strip():
            raise ValueError("analytical relationship requires analysis_relationship_id")
        source = payload.get("source_id")
        target = payload.get("target_id")
        if not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip():
            raise ValueError("analytical relationship requires explicit source_id and target_id")
        cardinality = payload.get("cardinality")
        if not isinstance(cardinality, str) or not cardinality.strip():
            raise ValueError("analytical relationship requires explicit cardinality")
        join_keys = payload.get("join_keys")
        if not isinstance(join_keys, list) or not join_keys:
            raise ValueError("analytical relationship requires non-empty join_keys list")
        for index, value in enumerate(join_keys):
            if not isinstance(value, Mapping) or set(value) != {"source_field", "target_field"}:
                raise ValueError(f"analytical relationship join_keys[{index}] is invalid")
            if (
                not isinstance(value["source_field"], str)
                or not value["source_field"].strip()
                or not isinstance(value["target_field"], str)
                or not value["target_field"].strip()
            ):
                raise ValueError(f"analytical relationship join_keys[{index}] is invalid")
        validate_analytical_relationship_measurement(
            cardinality=cardinality,
            matched_pairs=payload.get("matched_pairs"),
            source_population=payload.get("source_population"),
            target_population=payload.get("target_population"),
            matched_source_count=payload.get("matched_source_count"),
            matched_target_count=payload.get("matched_target_count"),
            source_coverage=payload.get("source_coverage"),
            target_coverage=payload.get("target_coverage"),
            publishable=True,
        )
        if not any(
            isinstance(value, str) and value.strip()
            for value in (payload.get("date_authority"), payload.get("as_of"))
        ):
            raise ValueError("publishable analytical relationships require date_authority or as_of")
        limitations = payload.get("limitations")
        if not isinstance(limitations, list) or any(not isinstance(value, str) or not value.strip() for value in limitations):
            raise ValueError("analytical relationship limitations must be a list of non-empty strings")
        refs = payload.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(value, str) or not value.strip() for value in refs):
            raise ValueError("analytical relationship evidence_refs must be a non-empty list")
        if len(set(refs)) != len(refs):
            raise ValueError("analytical relationship evidence_refs must be unique")

        if _RELATIONSHIP_REUSE_FIELD in payload:
            reuse_id = payload.get(_RELATIONSHIP_REUSE_FIELD)
            if not isinstance(reuse_id, str) or not reuse_id.strip():
                raise ValueError(
                    "relationship reuse requires a non-empty reuse_existing_relationship_id"
                )

    @staticmethod
    def _relationship_reuse_semantics(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return the identity-independent semantics of one relationship.

        Requirement-local IDs, owner/audit metadata, scope, and evidence
        references are intentionally excluded.  Evidence is checked by the
        dedicated monotonic rule below; every other field remains substantive
        relationship semantics and must match exactly.
        """

        return {
            str(key): _jsonable(value)
            for key, value in payload.items()
            if str(key) not in _RELATIONSHIP_REUSE_IGNORED_FIELDS
        }

    @staticmethod
    def _relationship_reuse_evidence_refs(payload: Mapping[str, Any]) -> tuple[str, ...]:
        """Read and mechanically validate one relationship's evidence refs."""

        refs = payload.get("evidence_refs")
        if not isinstance(refs, list) or any(not isinstance(value, str) or not value.strip() for value in refs):
            raise ValueError("relationship reuse evidence_refs must be a list of non-empty strings")
        normalized = tuple(value.strip() for value in refs)
        if len(set(normalized)) != len(normalized):
            raise ValueError("relationship reuse evidence_refs must be unique")
        return normalized

    @staticmethod
    def _validate_relationship_reuse_on_model(
        model: LivingEnterpriseModel,
        payload: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Validate one reuse marker against a projected committed model.

        The return value is the ordered set of evidence refs added by the
        requirement-local payload.  The instance wrapper resolves those refs
        against the current accepted bundle before staging/commit.
        """

        if _RELATIONSHIP_REUSE_FIELD not in payload:
            return ()
        reference = payload.get(_RELATIONSHIP_REUSE_FIELD)
        if not isinstance(reference, str) or not reference.strip():  # pragma: no cover - static guard also covers this
            raise ValueError(
                "relationship reuse requires a non-empty reuse_existing_relationship_id"
            )
        if reference != reference.strip():
            raise ValueError(
                "relationship reuse_existing_relationship_id must not have surrounding whitespace"
            )
        existing = model.relationships.get(reference.strip())
        if existing is None:
            raise ValueError(
                f"relationship reuse references an unknown committed relationship: {reference}"
            )
        expected = IntegrationSession._relationship_reuse_semantics(payload)
        actual = IntegrationSession._relationship_reuse_semantics(existing)
        if expected != actual:
            differing = sorted(
                key
                for key in set(expected) | set(actual)
                if expected.get(key) != actual.get(key)
            )
            details = ", ".join(differing) or "relationship semantics"
            raise ValueError(
                f"relationship reuse semantics do not match committed relationship {reference}: {details}"
            )
        existing_refs = IntegrationSession._relationship_reuse_evidence_refs(existing)
        requested_refs = IntegrationSession._relationship_reuse_evidence_refs(payload)
        existing_set = set(existing_refs)
        requested_set = set(requested_refs)
        missing = sorted(existing_set - requested_set)
        if missing:
            raise ValueError(
                "relationship reuse cannot remove committed evidence refs: "
                + ", ".join(missing)
            )
        return tuple(value for value in requested_refs if value not in existing_set)

    def _validate_relationship_reuse(
        self,
        payload: Mapping[str, Any],
        *,
        model: LivingEnterpriseModel | None = None,
    ) -> None:
        """Validate an explicit reference to an existing cumulative edge.

        Reuse is deliberately checked against the projected committed model,
        never against same-session staged records.  This prevents a forward
        reference or a newly staged alias from becoming an ontology authority.
        """

        added_refs = IntegrationSession._validate_relationship_reuse_on_model(
            self.lem if model is None else model,
            payload,
        )
        for evidence_ref in added_refs:
            # Reuse may add requirement-local evidence, but every addition
            # must be authorized by this item's immutable accepted bundle (or
            # the existing prepared/record evidence mechanisms) exactly as a
            # normal integration evidence reference would be.
            self._evidence((evidence_ref,), required=True)

        if _RELATIONSHIP_REUSE_FIELD not in payload:
            return

        # A reuse marker is only meaningful when the current analytical owner
        # explicitly selected the committed relationship in the immutable
        # semantic context.  Inherited refs belong to the already-projected
        # committed relationship and must remain present unchanged, but they
        # are not revalidated against this item's accepted bundle.  Only refs
        # added by this requirement can authorize current reuse.
        added_semantic_refs = tuple(
            value for value in added_refs if value.startswith(_SEMANTIC_SELECTION_PREFIX)
        )
        if not added_semantic_refs:
            raise ValueError(
                "relationship reuse requires a current semantic selection evidence reference added by this item"
            )
        reference = str(payload[_RELATIONSHIP_REUSE_FIELD]).strip()
        selected_relationship_ids: set[str] = set()
        for semantic_ref in added_semantic_refs:
            selection_sets = self._validate_semantic_selection_evidence(semantic_ref)
            selected_relationship_ids.update(selection_sets.get("relationship_ids", ()))
        if reference not in selected_relationship_ids:
            raise ValueError(
                f"relationship reuse semantic selection does not include committed relationship {reference}"
            )

    @staticmethod
    def _identity_decision_from_payload(payload: Mapping[str, Any]) -> IdentityDecision:
        try:
            decision = IdentityDecision.from_dict(payload)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("identity decision payload is invalid") from exc
        # The contract recomputes decision_hash.  Requiring a canonical
        # round-trip rejects a tampered hash or an otherwise untyped payload
        # instead of silently normalizing it during projection.
        if decision.to_dict() != dict(payload):
            raise ValueError("identity decision payload is not canonical")
        if decision.review_status not in {"reviewed", "accepted"} or not str(decision.reviewer_ref or "").strip():
            raise ValueError(
                f"identity decision publication requires reviewed or accepted status with reviewer_ref: {decision.decision_id}"
            )
        return decision

    @staticmethod
    def _knowledge_delta_from_payload(payload: Mapping[str, Any]) -> KnowledgeDelta:
        try:
            delta = KnowledgeDelta.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("knowledge delta payload is invalid") from exc
        if not delta.accepted:
            raise ValueError("knowledge delta payload must be accepted")
        if delta.to_dict() != dict(payload):
            raise ValueError("knowledge delta payload is not canonical")
        return delta

    @staticmethod
    def _canonical_mapping_from_payload(payload: Mapping[str, Any]) -> CanonicalMapping:
        try:
            mapping = CanonicalMapping.from_dict(payload)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("canonical mapping payload is invalid") from exc
        if mapping.to_dict() != dict(payload):
            raise ValueError("canonical mapping payload is not canonical")
        return mapping

    def _known_identity_decisions(self, records: Sequence[IntegrationRecord] | None = None) -> dict[str, IdentityDecision]:
        """Return cumulative decisions plus earlier same-session decisions."""

        decisions = dict(self.lem.identity_decisions)
        for record in records if records is not None else self._records:
            if record.kind != "identity_decision":
                continue
            decision = self._identity_decision_from_payload(record.payload)
            existing = decisions.get(decision.decision_id)
            if existing is not None and existing != decision:
                raise ValueError(f"identity decision collision: {decision.decision_id}")
            decisions[decision.decision_id] = decision
        return decisions

    @staticmethod
    def _validate_canonical_mapping_payload(
        payload: Mapping[str, Any],
        *,
        known_decisions: Mapping[str, IdentityDecision],
    ) -> CanonicalMapping:
        mapping = IntegrationSession._canonical_mapping_from_payload(payload)
        if mapping.status != "accepted":
            raise ValueError(f"canonical mapping publication requires accepted status: {mapping.canonical_id}")
        if not mapping.source_identities or any(not str(value).strip() for value in mapping.source_identities):
            raise ValueError(f"canonical mapping requires non-empty source_identities: {mapping.canonical_id}")
        if len(set(mapping.source_identities)) != len(mapping.source_identities):
            raise ValueError(f"canonical mapping source_identities must be unique: {mapping.canonical_id}")
        decision = known_decisions.get(mapping.decision_id)
        if decision is None:
            raise ValueError(f"canonical mapping requires an earlier exact identity decision: {mapping.decision_id}")
        if decision.review_status not in {"reviewed", "accepted"} or not str(decision.reviewer_ref or "").strip():
            raise ValueError(f"canonical mapping requires a reviewed identity decision with reviewer_ref: {mapping.decision_id}")
        if decision.decision not in {"same_object", "alternate_representation"}:
            raise ValueError(
                f"canonical mapping requires same_object or alternate_representation decision: {mapping.decision_id}"
            )
        return mapping

    def _validate_relationship_refs(
        self,
        record: IntegrationRecord,
        *,
        known: set[str] | None = None,
        known_mappings: set[str] | None = None,
    ) -> None:
        payload = record.payload
        self._validate_relationship_payload(payload)
        source = payload.get("source_id")
        target = payload.get("target_id")
        if known is None:
            known = set(self.lem.ontology)
        if known_mappings is None:
            known_mappings = set(self.lem.canonical_mappings)
        if (str(source) not in known and str(source) not in known_mappings) or (
            str(target) not in known and str(target) not in known_mappings
        ):
            raise ValueError("relationship references unknown ontology item or canonical mapping")

    def _validate_prepared_candidate(self, record: IntegrationRecord) -> PreparedAssetDescriptor:
        """Re-check one candidate at every validation/commit boundary."""

        descriptor = PreparedAssetDescriptor.from_dict(record.payload)
        if descriptor.scope != record.scope:
            raise ValueError("prepared asset record scope does not match descriptor")
        candidate = self._resolve_prepared_candidate(descriptor)
        if descriptor.prepared_content_hash is None or descriptor.byte_count is None or descriptor.row_count is None:
            raise ValueError("prepared asset descriptor is missing exact content binding")
        if candidate.stat().st_size != descriptor.byte_count:
            raise ValueError(f"prepared candidate byte count changed: {descriptor.prepared_asset_id}")
        # ``preflight_register`` performs the exact hash/row decode and same-ID
        # conflict check without mutating the accepted registry.
        self.prepared_registry.preflight_register(descriptor, item_workspace=self.item_workspace)
        return descriptor

    def _read_analytical_relationship_artifact(
        self,
        *,
        invalid_record_ids: set[str] | None = None,
        errors: list[str] | None = None,
    ) -> tuple[bool, tuple[dict[str, Any], ...]]:
        """Read the optional AO analytical-relationship JSONL artifact.

        The artifact is item-local evidence.  Its absence is intentionally
        compatible with older mechanical callers; when present, malformed or
        semantically incomplete records fail closed before fidelity review or
        commit.
        """

        path = self.item_workspace.work_root / "analytical_relationships.jsonl"
        _assert_no_symlink(path, label="analytical relationships artifact")
        if not path.exists():
            return False, ()
        if not path.is_file():
            raise ValueError("analytical relationships artifact must be a regular file")
        try:
            artifact_bytes = path.read_bytes()
            lines = artifact_bytes.decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("analytical relationships artifact is invalid") from exc
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"analytical relationships artifact line {line_number} is invalid") from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"analytical relationships artifact line {line_number} is invalid")
            records.append(dict(value))
        artifact_hash = _sha256_bytes(artifact_bytes)
        stale_record_ids: list[str] = []
        for record in self._records:
            if "work/analytical_relationships.jsonl" in record.evidence_refs:
                if record.evidence_hashes.get("work/analytical_relationships.jsonl") != artifact_hash:
                    stale_record_ids.append(record.record_id)
        if stale_record_ids:
            if invalid_record_ids is not None:
                invalid_record_ids.update(stale_record_ids)
            stale_error = (
                "analytical relationships artifact evidence hash is stale for records: "
                + ", ".join(stale_record_ids)
            )
            if errors is None:
                raise ValueError(stale_error)
            errors.append(stale_error)
        return True, tuple(records)

    def _validate_analytical_relationship_artifact(
        self,
        *,
        require_complete: bool = True,
        invalid_record_ids: set[str] | None = None,
    ) -> None:
        """Bind staged relationships to publishable AO joins.

        Normal validation requires every publishable AO relationship to be
        staged.  Pre-review edits use the same parser and staged-record checks
        while deliberately allowing an incomplete candidate; the final
        fidelity/commit path keeps the default completeness requirement.
        """

        errors: list[str] = []
        present, records = self._read_analytical_relationship_artifact(
            invalid_record_ids=invalid_record_ids,
            errors=errors,
        )
        # Existing ordinary relationship IDs are canonical no-op records.
        # They must not be forced through this requirement's analytical
        # artifact/shape contract; only new IDs (and explicit reuse-marker
        # records) are checked here.  Fold same-session IDs in order so a
        # repeated raw record is treated the same way as a prior committed
        # edge.
        known_relationship_ids = set(self.lem.relationships)
        known_knowledge_delta_relationship_ids: set[str] = set()
        known_relationship_analysis_ids = {
            str(payload.get("analysis_relationship_id"))
            for payload in self.lem.relationships.values()
            if payload.get("analysis_relationship_id") is not None
        }
        staged: list[IntegrationRecord] = []
        for record in self._records:
            if record.kind == "knowledge_delta":
                delta = self._knowledge_delta_from_payload(record.payload)
                if delta.operation == "add_relationship":
                    relationship_id = self._relationship_id_from_payload(delta.payload)
                    if relationship_id is not None:
                        # A KD relationship is an audit identity only.  It
                        # still makes a later typed duplicate an ordinary
                        # ID-first no-op, while never being treated as an AO
                        # analytical edge itself.
                        known_knowledge_delta_relationship_ids.add(relationship_id)
                continue
            if record.kind != "relationship":
                continue
            relationship_id = self._relationship_id_from_payload(record.payload)
            if (
                relationship_id is not None
                and (
                    relationship_id in known_relationship_ids
                    or relationship_id in known_knowledge_delta_relationship_ids
                )
                and _RELATIONSHIP_REUSE_FIELD not in record.payload
            ):
                continue
            staged.append(record)
            if relationship_id is not None and _RELATIONSHIP_REUSE_FIELD not in record.payload:
                known_relationship_ids.add(relationship_id)
        if not present:
            if staged:
                message = "analytical_relationships.jsonl is required for staged relationships"
                if invalid_record_ids is not None:
                    invalid_record_ids.update(record.record_id for record in staged)
                raise ValueError(message)
            return

        def mark_invalid(record: IntegrationRecord, message: str) -> None:
            if invalid_record_ids is not None:
                invalid_record_ids.add(record.record_id)
            errors.append(f"{record.record_id}: {message}")

        publishable: dict[str, Mapping[str, Any]] = {}
        artifact_ids: set[str] = set()
        for index, value in enumerate(records, 1):
            record_kind = value.get("record_kind")
            relationship_id = value.get("relationship_id")
            if not isinstance(relationship_id, str) or not relationship_id.strip():
                errors.append(f"analytical relationship record {index} requires relationship_id")
                continue
            if relationship_id in artifact_ids:
                errors.append(f"analytical relationship ID is duplicated: {relationship_id}")
                continue
            artifact_ids.add(relationship_id)
            source = value.get("source_id")
            target = value.get("target_id")
            cardinality = value.get("cardinality")
            if not all(isinstance(item, str) and item.strip() for item in (source, target, cardinality)):
                errors.append(
                    f"analytical relationship {relationship_id} requires source_id, target_id, and cardinality"
                )
                continue
            try:
                evidence = AnalyticalRelationshipEvidence.from_dict(value)
            except (TypeError, ValueError, KeyError) as exc:
                errors.append(f"analytical relationship {relationship_id} is invalid: {exc}")
                continue
            canonical_value = dict(value)
            canonical_value.pop("item_id", None)
            canonical_value.pop("owner_ref", None)
            canonical_expected = evidence.to_dict()
            if evidence.no_relationship_reason is not None:
                # Audits may omit all measurement fields; when present the
                # contract constructor has already required exact zeros.
                measurement_fields = (
                    "matched_pairs",
                    "source_population",
                    "target_population",
                    "matched_source_count",
                    "matched_target_count",
                    "source_coverage",
                    "target_coverage",
                )
                for field in measurement_fields:
                    canonical_value.pop(field, None)
                    canonical_expected.pop(field, None)
            if canonical_expected != canonical_value:
                errors.append(f"analytical relationship {relationship_id} is not canonical")
                continue
            if record_kind == "analytical_relationship":
                if not evidence.publishable or evidence.no_relationship_reason is not None:
                    errors.append(f"analytical relationship {relationship_id} must be publishable")
                    continue
                publishable[relationship_id] = value
            elif record_kind == "relationship_audit":
                if evidence.publishable or evidence.no_relationship_reason is None:
                    errors.append(f"relationship audit is invalid: {relationship_id}")
            else:
                errors.append(f"analytical relationships artifact record_kind is invalid: {record_kind!r}")

        staged_by_analysis: dict[str, IntegrationRecord] = {}
        for record in staged:
            payload = record.payload
            try:
                self._validate_relationship_payload(payload)
            except Exception as exc:
                mark_invalid(record, str(exc))
                continue
            analysis_id = payload.get("analysis_relationship_id")
            if "work/analytical_relationships.jsonl" not in record.evidence_refs:
                mark_invalid(
                    record,
                    "staged analytical relationship must reference "
                    f"work/analytical_relationships.jsonl: {analysis_id}",
                )
            if analysis_id in staged_by_analysis:
                mark_invalid(record, f"analytical relationship is staged more than once: {analysis_id}")
                mark_invalid(
                    staged_by_analysis[analysis_id],
                    f"analytical relationship is staged more than once: {analysis_id}",
                )
            else:
                staged_by_analysis[analysis_id] = record
            if analysis_id not in publishable:
                mark_invalid(record, f"staged relationship is not a publishable AO relationship: {analysis_id}")

        for relationship_id, source in publishable.items():
            record = staged_by_analysis.get(relationship_id)
            if record is None:
                # A publishable analytical row may describe an edge that was
                # already committed by an earlier requirement.  Its ordinary
                # duplicate is a no-op, so requiring a second staged payload
                # here would reintroduce collision-producing validation.
                if relationship_id in known_relationship_ids or relationship_id in known_relationship_analysis_ids:
                    continue
                if require_complete:
                    errors.append(
                        "publishable analytical relationship is not staged exactly once: "
                        f"{relationship_id}"
                    )
                continue
            payload = record.payload
            for field in (
                "source_id",
                "target_id",
                "cardinality",
                "join_keys",
                "matched_pairs",
                "source_population",
                "target_population",
                "matched_source_count",
                "matched_target_count",
                "source_coverage",
                "target_coverage",
                "limitations",
                "evidence_refs",
                "date_authority",
                "as_of",
            ):
                if payload.get(field) != source.get(field):
                    mark_invalid(
                        record,
                        f"staged analytical relationship {field} mismatch: {relationship_id}",
                    )

        if errors:
            raise ValueError("; ".join(errors))

    def _validate_with_invalid_record_ids(
        self,
        *,
        require_complete_analytical_relationships: bool = True,
        allow_empty_staging: bool = False,
    ) -> tuple[IntegrationValidation, frozenset[str]]:
        self._ensure_bundle_current()
        counts = {kind: 0 for kind in sorted(_RECORD_KINDS)}
        omissions: list[str] = []
        errors: list[str] = []
        invalid_record_ids: set[str] = set()
        seen: set[str] = set()
        analytical_artifact_ids: dict[str, tuple[Any, ...]] = {}
        analytical_artifact_refs: dict[str, tuple[Any, ...]] = {}
        # An untouched/partially materialized session has no integration
        # result to review.  Keep the deliberate pre-publication removal path
        # usable for an owner repairing a previously populated session, but a
        # fresh empty snapshot is an explicit staging handoff defect rather
        # than a successful no-op.  A caller that truly has no semantic
        # change must stage a typed ``KnowledgeDelta(operation="no_change")``
        # (or a limitation record), so the reviewer receives an auditable
        # record instead of an empty checked set.
        if not self._records and not allow_empty_staging and not self._has_explicit_empty_noop_history():
            omissions.append("staging_incomplete")
            errors.append(
                "staging_incomplete: integration session has no records; "
                "stage an explicit no_change or limitation record before fidelity review"
            )
        # Relationship references are intentionally order-sensitive.  The
        # known set grows as staged ontology/metric/relationship records are
        # encountered; a forward-only or unknown reference fails closed.
        known_ontology_ids = set(self.lem.ontology)
        # Relationship IDs are checked before validating an incoming payload.
        # Existing authoritative edges are ordinary idempotent audit rows;
        # only genuinely new IDs need endpoint/analytical-shape validation.
        known_relationships: dict[str, Mapping[str, Any]] = {
            str(relationship_id): payload
            for relationship_id, payload in self.lem.relationships.items()
        }
        known_decisions: dict[str, IdentityDecision] = dict(self.lem.identity_decisions)
        known_mappings: dict[str, CanonicalMapping] = dict(self.lem.canonical_mappings)
        for record in self._records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
            if record.record_id in seen:
                errors.append(f"duplicate record_id: {record.record_id}")
            seen.add(record.record_id)
            try:
                IntegrationRecord.from_dict(record.to_dict())
                self._scope(record.scope)
                if record.kind == "analytical_artifact":
                    artifact, artifact_bytes = _parse_analytical_artifact_payload(
                        record.payload,
                        expected_item_id=self.item_id,
                    )
                    # Re-read every external JSONL output at the fidelity
                    # boundary.  The accepted artifact JSON hash alone is
                    # insufficient because the output file is a separate
                    # mutable work artifact that may have changed after
                    # generation or packet creation.
                    self._validate_external_artifact_outputs(artifact)
                    artifact_ref = record.payload.get("artifact_ref")
                    identity = (
                        artifact.artifact_id,
                        artifact.artifact_type,
                        artifact.schema_version,
                        artifact.requirement_id,
                        artifact.content_hash,
                        artifact.envelope_hash,
                        _sha256_bytes(artifact_bytes),
                        artifact_ref,
                    )
                    prior = analytical_artifact_ids.get(artifact.artifact_id)
                    if prior is not None:
                        if prior != identity:
                            raise ValueError(f"analytical artifact ID collision: {artifact.artifact_id}")
                        raise ValueError(f"analytical artifact ID is staged more than once: {artifact.artifact_id}")
                    ref_prior = analytical_artifact_refs.get(str(artifact_ref))
                    if ref_prior is not None and ref_prior != identity:
                        raise ValueError(f"analytical artifact committed reference collision: {artifact_ref}")
                    if ref_prior is not None:
                        raise ValueError(f"analytical artifact committed reference is staged more than once: {artifact_ref}")
                    analytical_artifact_ids[artifact.artifact_id] = identity
                    analytical_artifact_refs[str(artifact_ref)] = identity
                elif record.kind == "ontology_item":
                    item = LivingEnterpriseModel(run_id=self.lem.run_id).add_ontology_item(record.payload)
                    known_ontology_ids.add(item.item_id)
                elif record.kind == "metric":
                    self._preview_metric(record.payload, self.lem.run_id)
                elif record.kind == "identity_decision":
                    decision = self._identity_decision_from_payload(record.payload)
                    existing_decision = known_decisions.get(decision.decision_id)
                    if existing_decision is not None and existing_decision != decision:
                        raise ValueError(f"identity decision collision: {decision.decision_id}")
                    known_decisions[decision.decision_id] = decision
                elif record.kind == "canonical_mapping":
                    mapping = self._validate_canonical_mapping_payload(
                        record.payload,
                        known_decisions=known_decisions,
                    )
                    existing_mapping = known_mappings.get(mapping.canonical_id)
                    if existing_mapping is not None and existing_mapping != mapping:
                        raise ValueError(f"canonical mapping collision: {mapping.canonical_id}")
                    known_mappings[mapping.canonical_id] = mapping
                elif record.kind == "knowledge_delta":
                    delta = self._knowledge_delta_from_payload(record.payload)
                    if tuple(delta.evidence_refs) != record.evidence_refs:
                        raise ValueError("knowledge delta evidence_refs do not match integration envelope")
                    delta_payload = delta.payload
                    if delta.operation == "add_relationship":
                        if _RELATIONSHIP_REUSE_FIELD in delta_payload:
                            raise ValueError(
                                "knowledge delta add_relationship cannot use "
                                "reuse_existing_relationship_id; use typed add_relationship"
                            )
                        relationship_id = self._relationship_id_from_payload(delta_payload)
                        existing_relationship = (
                            relationship_id is not None
                            and relationship_id in known_relationships
                        )
                        if not existing_relationship:
                            self._validate_relationship_payload(delta_payload)
                            source = str(delta_payload["source_id"])
                            target = str(delta_payload["target_id"])
                            if (
                                source not in known_ontology_ids
                                and source not in known_mappings
                            ) or (
                                target not in known_ontology_ids
                                and target not in known_mappings
                            ):
                                raise ValueError(
                                    "relationship references unknown ontology item or canonical mapping"
                                )
                            (
                                preview_relationship_id,
                                preview_payload,
                                relationship_item,
                            ) = self._preview_relationship(
                                delta_payload,
                                self.lem.run_id,
                                known_ontology_ids=known_ontology_ids,
                                known_mapping_ids=known_mappings,
                            )
                            known_ontology_ids.add(relationship_item.item_id)
                            known_relationships[preview_relationship_id] = preview_payload
                        elif relationship_id is not None:
                            # Keep the generated companion visible to later
                            # same-session endpoint references.  The raw KD
                            # payload remains untouched and is not promoted
                            # to an analytical relationship record.
                            known_ontology_ids.add(relationship_id)
                    elif delta.operation in {
                        "add_ontology_item", "add_metric", "add_definition", "add_rule",
                        "add_process", "add_event", "add_dimension",
                    }:
                        item_id = (
                            delta_payload.get("item_id")
                            or delta_payload.get("relationship_id")
                            or delta_payload.get("metric_id")
                            or delta_payload.get("definition_id")
                            or delta_payload.get("rule_id")
                            or delta_payload.get("process_id")
                            or delta_payload.get("event_id")
                            or delta_payload.get("dimension_id")
                            or delta_payload.get("id")
                        )
                        if item_id is not None:
                            known_ontology_ids.add(str(item_id))
                    elif delta.operation == "add_canonical_mapping":
                        mapping_id = delta_payload.get("canonical_id")
                        if mapping_id is not None:
                            known_mappings[str(mapping_id)] = CanonicalMapping.from_dict(delta_payload)
                if record.kind == "relationship":
                    relationship_id_value = (
                        record.payload.get("relationship_id")
                        or record.payload.get("item_id")
                        or record.payload.get("id")
                    )
                    relationship_id = (
                        str(relationship_id_value)
                        if relationship_id_value is not None
                        else None
                    )
                    existing_relationship = (
                        relationship_id is not None
                        and relationship_id in known_relationships
                        and _RELATIONSHIP_REUSE_FIELD not in record.payload
                    )
                    if not existing_relationship:
                        self._validate_relationship_refs(
                            record,
                            known=known_ontology_ids,
                            known_mappings=set(known_mappings),
                        )
                    if _RELATIONSHIP_REUSE_FIELD in record.payload:
                        self._validate_relationship_reuse(record.payload)
                    elif not existing_relationship:
                        _relationship_id, _relationship_payload, relationship_item = self._preview_relationship(
                            record.payload,
                            self.lem.run_id,
                            known_ontology_ids=known_ontology_ids,
                            known_mapping_ids=known_mappings,
                        )
                        known_ontology_ids.add(relationship_item.item_id)
                        known_relationships[_relationship_id] = _relationship_payload
                    elif relationship_id is not None:
                        # Keep the generated ontology companion visible to
                        # later staged references even when the stored edge
                        # was only partially rehydrated.
                        known_ontology_ids.add(relationship_id)
                if record.kind == "prepared_asset":
                    self._validate_prepared_candidate(record)
            except Exception as exc:
                invalid_record_ids.add(record.record_id)
                errors.append(f"{record.record_id}: {exc}")
        try:
            self._validate_analytical_relationship_artifact(
                require_complete=require_complete_analytical_relationships,
                invalid_record_ids=invalid_record_ids,
            )
        except Exception as exc:
            errors.append(f"analytical_relationships.jsonl: {exc}")
        return IntegrationValidation(not errors, counts, tuple(omissions), tuple(errors)), frozenset(invalid_record_ids)

    def _has_explicit_empty_noop_history(self) -> bool:
        """Return whether an empty session records an intentional no-op repair.

        Removing every previously staged record is an explicit owner action,
        and the durable removal maps/journal preserve that fact for exact
        retries.  A newly-created empty session has no such history and must
        remain ``staging_incomplete`` until a typed no-change/limitation record
        is staged.
        """

        if self._records:
            return False
        removed = self._state.get(_UNREVIEWED_REMOVED_RECORD_HASHES)
        if isinstance(removed, Mapping) and bool(removed):
            return True
        try:
            result = self._read_fidelity_result_raw()
            if result is None or result.verdict != "repair_once":
                return False
            progress = self._read_repair_progress(self._read_repair_authorization())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        return bool(progress.removed_record_ids)

    def validate(self) -> IntegrationValidation:
        validation, _ = self._validate_with_invalid_record_ids()
        return validation

    @staticmethod
    def _preview_metric(payload: Mapping[str, Any], run_id: str) -> Mapping[str, Any]:
        """Validate a current metric record without creating ontology state."""

        if not isinstance(payload, Mapping) or not payload:
            raise ValueError("metric record payload must be a non-empty object")
        # The record is intentionally observation-capable.  JSON-shape
        # validation is all this boundary can prove; meaning remains in the
        # accepted answer and evidence.
        json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return dict(payload)

    def _known_relationship_endpoint_ids(
        self,
        records: Sequence[IntegrationRecord] | None = None,
    ) -> tuple[set[str], set[str]]:
        """Collect cumulative and earlier staged relationship endpoint IDs."""

        ontology_ids = set(self.lem.ontology)
        mapping_ids = set(self.lem.canonical_mappings)
        for record in records if records is not None else self._records:
            payload = record.payload
            if record.kind == "ontology_item":
                item_id = payload.get("item_id")
                if item_id is not None:
                    ontology_ids.add(str(item_id))
            elif record.kind == "canonical_mapping":
                mapping_id = payload.get("canonical_id")
                if mapping_id is not None:
                    mapping_ids.add(str(mapping_id))
            elif record.kind == "knowledge_delta":
                delta = self._knowledge_delta_from_payload(payload)
                delta_payload = delta.payload
                if delta.operation in {
                    "add_ontology_item", "add_metric", "add_definition", "add_rule",
                    "add_process", "add_event", "add_dimension", "add_relationship",
                }:
                    item_id = (
                        delta_payload.get("item_id")
                        or delta_payload.get("relationship_id")
                        or delta_payload.get("metric_id")
                        or delta_payload.get("definition_id")
                        or delta_payload.get("rule_id")
                        or delta_payload.get("process_id")
                        or delta_payload.get("event_id")
                        or delta_payload.get("dimension_id")
                        or delta_payload.get("id")
                    )
                    if item_id is not None:
                        ontology_ids.add(str(item_id))
                elif delta.operation == "add_canonical_mapping":
                    mapping_id = delta_payload.get("canonical_id")
                    if mapping_id is not None:
                        mapping_ids.add(str(mapping_id))
            elif record.kind == "relationship" and _RELATIONSHIP_REUSE_FIELD not in payload:
                relationship_id = payload.get("relationship_id") or payload.get("item_id") or payload.get("id")
                if relationship_id is not None:
                    ontology_ids.add(str(relationship_id))
        return ontology_ids, mapping_ids

    @staticmethod
    def _preview_relationship(
        payload: Mapping[str, Any],
        run_id: str,
        *,
        known_ontology_ids: Iterable[str] = (),
        known_mapping_ids: Iterable[str] = (),
    ) -> tuple[str, dict[str, Any], OntologyItem]:
        IntegrationSession._validate_relationship_payload(payload)
        preview = LivingEnterpriseModel(run_id=run_id)
        # ``LivingEnterpriseModel.add_relationship`` validates endpoints
        # against the model's registries.  A preview is intentionally
        # side-effect-free, so seed detached placeholders for the exact IDs
        # already known by the cumulative/staged view before invoking the
        # canonical model operation.
        for item_id in known_ontology_ids:
            identifier = str(item_id)
            preview.ontology[identifier] = OntologyItem(item_id=identifier, item_type="entity", label=identifier)
        for canonical_id in known_mapping_ids:
            identifier = str(canonical_id)
            preview.canonical_mappings[identifier] = CanonicalMapping(
                canonical_id=identifier,
                object_type="preview",
                source_identities=(identifier,),
                decision_id="preview",
                status="accepted",
            )
        ontology_item = preview.add_relationship(payload)
        relationship_id = str(payload["relationship_id"])
        return relationship_id, copy.deepcopy(preview.relationships[relationship_id]), ontology_item

    def _preflight_registry(self) -> None:
        for record in self._records:
            if record.kind == "prepared_asset":
                descriptor = self._validate_prepared_candidate(record)
                # Public, lock-protected and non-mutating.  This must happen
                # before commit intent or any registry/LEM mutation.
                self.prepared_registry.preflight_register(descriptor, item_workspace=self.item_workspace)

    def _preflight_lem(self) -> None:
        # Replay the exact accepted-record primitive into an isolated model.
        # This keeps successor target resolution and rollback semantics
        # identical to commit/replay without mutating the projected authority.
        simulated = LivingEnterpriseModel.from_export(self.lem.export())
        for record in self._records:
            payload = dict(record.payload)
            if record.kind == "metric":
                self._preview_metric(payload, self.lem.run_id)
            elif record.kind == "relationship" and _RELATIONSHIP_REUSE_FIELD in payload:
                self._validate_relationship_reuse(payload, model=simulated)
            elif record.kind in {
                "ontology_item",
                "relationship",
                "identity_decision",
                "canonical_mapping",
                "prepared_asset",
                "limitation",
                "knowledge_delta",
            }:
                self._apply_lem_record_to_model(
                    simulated,
                    record,
                    applied_at="preflight",
                )

    def _validate_partial_staging(self) -> IntegrationValidation:
        """Validate a mechanically consistent but not-yet-complete candidate."""

        validation, _ = self._validate_with_invalid_record_ids(
            require_complete_analytical_relationships=False,
            # The only current partial-preflight caller is the owner-bound
            # pre-publication removal transaction.  It must be able to
            # validate the intermediate empty snapshot before persisting the
            # explicit removal history; public ``validate()`` remains strict
            # and reports ``staging_incomplete`` for a fresh empty session.
            allow_empty_staging=True,
        )
        return validation

    def _preflight_all(
        self,
        *,
        require_complete: bool = True,
        allow_empty_staging: bool = False,
    ) -> None:
        # The caller-visible model is a convenience view, never commit
        # authority. Rebuild from durable prior commits immediately before
        # every commit preflight so caller mutation cannot poison publication.
        self._reproject_prior()
        self._ensure_bundle_current()
        validation, _ = self._validate_with_invalid_record_ids(
            require_complete_analytical_relationships=require_complete,
            allow_empty_staging=allow_empty_staging,
        )
        if not validation.valid:
            raise ValueError(f"integration validation failed: {list(validation.errors)}")
        self._preflight_registry()
        self._preflight_lem()

    def _commit_registry_authority(self) -> Any:
        """Mint the registry capability from the exact durable commit intent."""

        intent_path = self.staging_root / _INTENT_FILENAME
        intent = self._read_intent(
            intent_path,
            self.bundle,
            session_id=self.session_id,
            owner_id=self.owner_id,
            invocation_id=self.invocation_id,
        )
        if intent is None:
            raise ValueError("accepted registry publication requires a persisted commit intent")
        return _new_registry_commit_authority(
            self.item_workspace,
            session_id=self.session_id,
            owner_id=self.owner_id,
            intent_path=intent_path,
            intent_hash=str(intent["intent_hash"]),
        )

    def _apply_records(self) -> None:
        self._ensure_bundle_current()
        authority = self._commit_registry_authority()
        for record in self._records:
            # Revalidate accepted bytes/envelope/manifest immediately before
            # each external mutation, including registry repair retries.
            self._ensure_bundle_current()
            if record.kind == "prepared_asset":
                descriptor = self._validate_prepared_candidate(record)
                self.prepared_registry.register_accepted(
                    descriptor,
                    item_workspace=self.item_workspace,
                    _commit_authority=authority,
                )

    @staticmethod
    def _apply_lem_record_to_model(
        model: LivingEnterpriseModel,
        record: IntegrationRecord,
        *,
        applied_at: str,
    ) -> None:
        """Apply one accepted promoted record to a supplied LEM model.

        This is the sole replay primitive shared by normal commit and
        cumulative checkpoint recovery.  Metric, claim, evidence, and
        dashboard observations intentionally remain record-only.
        """
        if not isinstance(applied_at, str) or not applied_at:
            raise ValueError("LEM replay applied_at is invalid")
        payload = dict(record.payload)
        if record.kind == "ontology_item":
            item = OntologyItem.from_dict(payload)
            model.ensure_ontology_item(item)
        elif record.kind == "identity_decision":
            decision = IntegrationSession._identity_decision_from_payload(payload)
            existing = model.identity_decisions.get(decision.decision_id)
            if existing is None:
                model.register_identity_decision(decision)
            elif existing != decision:
                raise ValueError(f"identity decision collision: {decision.decision_id}")
        elif record.kind == "canonical_mapping":
            mapping = IntegrationSession._canonical_mapping_from_payload(payload)
            existing = model.canonical_mappings.get(mapping.canonical_id)
            if existing is None:
                model.add_mapping(mapping)
            elif existing != mapping:
                raise ValueError(f"canonical mapping collision: {mapping.canonical_id}")
        elif record.kind == "metric":
            # Current observations remain durable integration records only;
            # promoting them to cumulative ontology would make the ontology a
            # report cache and would permit later relationships to depend on a
            # transient value.
            IntegrationSession._preview_metric(payload, model.run_id)
        elif record.kind == "relationship":
            relationship_id_value = (
                payload.get("relationship_id")
                or payload.get("item_id")
                or payload.get("id")
            )
            relationship_id = (
                str(relationship_id_value)
                if relationship_id_value is not None
                else None
            )
            if _RELATIONSHIP_REUSE_FIELD in payload:
                # The relationship is already authoritative in the
                # cumulative model.  Validate the full semantic payload and
                # deliberately do not call add_relationship: that operation
                # would create a second ontology item/edge.
                IntegrationSession._validate_relationship_payload(payload)
                source = payload.get("source_id")
                target = payload.get("target_id")
                model._validate_relationship_endpoints(source, target)
                IntegrationSession._validate_relationship_reuse_on_model(model, payload)
                return
            if relationship_id is not None and relationship_id in model.relationships:
                # Relationship IDs are canonical edge identities.  A normal
                # repeated add is an idempotent reuse; delegate to the model so
                # a partially rehydrated edge can regain its missing generated
                # ontology companion.  Only the explicit reuse marker above
                # performs the stricter semantic/evidence check.
                model.add_relationship(payload)
                return
            # Only a genuinely new relationship receives full endpoint and
            # analytical-shape validation.
            IntegrationSession._validate_relationship_payload(payload)
            source = payload.get("source_id")
            target = payload.get("target_id")
            model._validate_relationship_endpoints(source, target)
            relationship_id, _expected_relationship, _ontology_item = IntegrationSession._preview_relationship(
                payload,
                model.run_id,
                known_ontology_ids=model.ontology,
                known_mapping_ids=model.canonical_mappings,
            )
            model.add_relationship(payload)
        elif record.kind == "prepared_asset":
            descriptor = PreparedAssetDescriptor.from_dict(payload)
            existing = model.prepared_assets.get(descriptor.prepared_asset_id)
            if existing is None:
                model.register_prepared_asset(descriptor)
            elif existing != descriptor:
                raise ValueError(f"prepared asset collision: {descriptor.prepared_asset_id}")
        elif record.kind == "knowledge_delta":
            delta = IntegrationSession._knowledge_delta_from_payload(payload)
            if tuple(delta.evidence_refs) != record.evidence_refs:
                raise ValueError("knowledge delta evidence_refs do not match integration envelope")
            if (
                delta.operation == "add_relationship"
                and _RELATIONSHIP_REUSE_FIELD in delta.payload
            ):
                raise ValueError(
                    "knowledge delta add_relationship cannot use "
                    "reuse_existing_relationship_id; use typed add_relationship"
                )
            existing = model.knowledge.get(delta.delta_id)
            if existing is None:
                model.apply_delta(delta, accepted=True)
                # Direct application records wall-clock time.  Replay binds the
                # revision to the durable integration commit for deterministic
                # exports and exact projection comparisons.
                model.revisions[-1]["applied_at"] = applied_at
            else:
                expected = {
                    "ref": LEMRef("knowledge_delta", delta.delta_id).to_dict(),
                    "operation": delta.operation,
                    "payload": dict(delta.payload),
                    "evidence_refs": list(delta.evidence_refs),
                    "conflicts_with": list(delta.conflicts_with),
                    "supersedes": [ref.to_dict() for ref in delta.supersedes],
                    "unresolved": model.conflict_state.get(delta.delta_id, {}).get(
                        "unresolved",
                        delta.operation == "record_conflict",
                    ),
                    "working_definition": delta.payload.get("working_definition"),
                }
                comparable = {
                    key: existing.get(key)
                    for key in expected
                    if key in existing
                }
                if comparable != expected:
                    raise ValueError(f"knowledge delta collision: {delta.delta_id}")
        elif record.kind == "limitation":
            delta = KnowledgeDelta(
                delta_id=record.record_id,
                operation="record_limitation",
                payload=payload,
                evidence_refs=record.evidence_refs,
                accepted=True,
            )
            existing = model.knowledge.get(record.record_id)
            if existing is None:
                model.apply_delta(delta)
                # LivingEnterpriseModel records wall-clock application time.
                # A replayed projection must instead bind this field to the
                # durable integration commit that authorized the record.
                model.revisions[-1]["applied_at"] = applied_at
            elif existing.get("operation") != "record_limitation" or existing.get("payload") != payload:
                raise ValueError(f"knowledge delta collision: {record.record_id}")

    @staticmethod
    def _committed_manifest(item_workspace: Any) -> Mapping[str, Any] | None:
        root = IntegrationSession._integration_root(item_workspace) / _COMMITTED_DIR
        manifest_path = root / _MANIFEST_FILENAME
        if not root.exists() and not root.is_symlink():
            return None
        _assert_no_symlink(root, label="committed integration")
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError("committed integration manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("committed integration manifest is invalid") from exc
        if not isinstance(manifest, Mapping):
            raise ValueError("committed integration manifest is invalid")
        expected = {
            "schema_version", "session_id", "item_id", "owner_id", "invocation_id", "status",
            "accepted_content_hash", "accepted_manifest_hash", "records_path",
            "records_hash", "records_count", "counts", "created_at", "committed_at",
            "manifest_hash",
        }
        if set(manifest) != expected or manifest.get("schema_version") != _SCHEMA_VERSION or manifest.get("status") != "committed":
            raise ValueError("committed integration manifest fields are invalid")
        if not _is_sha256(manifest.get("accepted_content_hash")) or not _is_sha256(manifest.get("accepted_manifest_hash")):
            raise ValueError("committed integration accepted hashes are invalid")
        if manifest.get("records_path") != _RECORDS_FILENAME or not _is_sha256(manifest.get("records_hash")):
            raise ValueError("committed integration records binding is invalid")
        if not isinstance(manifest.get("records_count"), int) or isinstance(manifest.get("records_count"), bool) or manifest["records_count"] < 0:
            raise ValueError("committed integration records_count is invalid")
        if not isinstance(manifest.get("counts"), Mapping) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in manifest["counts"].values()
        ):
            raise ValueError("committed integration counts are invalid")
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
        if not _is_sha256(manifest.get("manifest_hash")) or manifest.get("manifest_hash") != _sha256_value(unsigned):
            raise ValueError("committed integration manifest hash does not match content")
        return manifest

    @staticmethod
    def _technical_failure_manifest(
        item_workspace: Any,
        bundle: AcceptedAnalysisBundle,
    ) -> Mapping[str, Any] | None:
        root = IntegrationSession._integration_root(item_workspace) / _TECHNICAL_FAILURE_DIR
        path = root / _MANIFEST_FILENAME
        if not root.exists() and not root.is_symlink():
            return None
        _assert_no_symlink(root, label="integration technical failure")
        if not path.is_file() or path.is_symlink():
            raise ValueError("technical failure manifest is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("technical failure manifest is invalid") from exc
        expected = {
            "schema_version", "session_id", "item_id", "owner_id", "status",
            "accepted_content_hash", "reason", "created_at", "manifest_hash",
        }
        optional = {"recovery_exhausted"}
        if not isinstance(value, Mapping) or not expected.issubset(set(value)) or set(value) - expected - optional:
            raise ValueError("technical failure manifest fields are invalid")
        if value.get("schema_version") != _SCHEMA_VERSION or value.get("status") != "technical_failure":
            raise ValueError("technical failure manifest fields are invalid")
        if value.get("item_id") != bundle.item_id or value.get("accepted_content_hash") != bundle.content_hash:
            raise ValueError("technical failure manifest accepted bundle binding is invalid")
        if any(not isinstance(value.get(key), str) or not str(value[key]).strip() for key in ("session_id", "owner_id", "reason", "created_at")):
            raise ValueError("technical failure manifest identity is invalid")
        if "recovery_exhausted" in value and not isinstance(value.get("recovery_exhausted"), bool):
            raise ValueError("technical failure manifest recovery marker is invalid")
        unsigned = {key: item for key, item in value.items() if key != "manifest_hash"}
        if not _is_sha256(value.get("manifest_hash")) or value["manifest_hash"] != _sha256_value(unsigned):
            raise ValueError("technical failure manifest hash does not match content")
        return dict(value)

    @classmethod
    def _read_intent(
        cls,
        path: Path,
        bundle: AcceptedAnalysisBundle,
        *,
        session_id: str,
        owner_id: str,
        invocation_id: str,
    ) -> Mapping[str, Any] | None:
        if not (path.exists() or path.is_symlink()):
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("integration commit intent is invalid")
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("integration commit intent is invalid") from exc
        expected = {"schema_version", "session_id", "item_id", "owner_id", "invocation_id", "manifest", "intent_hash"}
        if not isinstance(intent, Mapping) or set(intent) != expected or intent.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("integration commit intent fields are invalid")
        if intent.get("session_id") != session_id or intent.get("item_id") != bundle.item_id or intent.get("owner_id") != owner_id or intent.get("invocation_id") != invocation_id:
            raise ValueError("integration commit intent identity is invalid")
        unsigned = {key: value for key, value in intent.items() if key != "intent_hash"}
        if not _is_sha256(intent.get("intent_hash")) or intent["intent_hash"] != _sha256_value(unsigned):
            raise ValueError("integration commit intent hash does not match content")
        manifest = intent.get("manifest")
        if not isinstance(manifest, Mapping):
            raise ValueError("integration commit intent manifest is invalid")
        expected_manifest = {
            "schema_version", "session_id", "item_id", "owner_id", "invocation_id", "status",
            "accepted_content_hash", "accepted_manifest_hash", "records_path",
            "records_hash", "records_count", "counts", "created_at", "committed_at",
            "manifest_hash",
        }
        if set(manifest) != expected_manifest or manifest.get("session_id") != session_id or manifest.get("owner_id") != owner_id or manifest.get("invocation_id") != intent.get("invocation_id"):
            raise ValueError("integration commit intent manifest identity is invalid")
        if manifest.get("item_id") != bundle.item_id or manifest.get("accepted_content_hash") != bundle.content_hash or manifest.get("accepted_manifest_hash") != bundle.manifest_hash:
            raise ValueError("integration commit intent accepted bundle binding is invalid")
        if manifest.get("schema_version") != _SCHEMA_VERSION or manifest.get("status") != "committed" or manifest.get("records_path") != _RECORDS_FILENAME:
            raise ValueError("integration commit intent manifest status/path is invalid")
        if not _is_sha256(manifest.get("records_hash")) or not isinstance(manifest.get("records_count"), int) or isinstance(manifest.get("records_count"), bool) or manifest["records_count"] < 0:
            raise ValueError("integration commit intent records binding is invalid")
        unsigned_manifest = {key: value for key, value in manifest.items() if key != "manifest_hash"}
        if not _is_sha256(manifest.get("manifest_hash")) or manifest["manifest_hash"] != _sha256_value(unsigned_manifest):
            raise ValueError("integration commit intent manifest hash is invalid")
        return dict(intent)

    def _build_manifest(self, records_hash: str, validation: IntegrationValidation, committed_at: str) -> dict[str, Any]:
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": self.session_id,
            "item_id": self.item_id,
            "owner_id": self.owner_id,
            "invocation_id": self.invocation_id,
            "status": "committed",
            "accepted_content_hash": self.bundle.content_hash,
            "accepted_manifest_hash": self.bundle.manifest_hash,
            "records_path": _RECORDS_FILENAME,
            "records_hash": records_hash,
            "records_count": len(self._records),
            "counts": dict(validation.counts),
            "created_at": self._state["created_at"],
            "committed_at": committed_at,
        }
        manifest["manifest_hash"] = _sha256_value(manifest)
        return manifest

    def _finish_committed(self, manifest: Mapping[str, Any]) -> None:
        if self.item_workspace.integration_state == "pending":
            self.item_workspace.mark_integration_committed(manifest["manifest_hash"], "integration/committed/manifest.json")
        elif self.item_workspace.integration_state == "integrated":
            if self.item_workspace.integration_manifest_hash != manifest["manifest_hash"]:
                raise ValueError("item integration state does not match committed manifest")
        else:
            raise ValueError("item integration state does not permit committed integration")
        state = dict(self._state)
        state["status"] = "committed"
        state["updated_at"] = manifest["committed_at"]
        self._persist_state(state)
        self.release()

    def release(self) -> None:
        """Release the lifetime invocation lease (idempotent)."""

        if self._lease is not None:
            self._lease.release()
            self._lease = None

    close = release

    def __enter__(self) -> "IntegrationSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown path
        try:
            self.release()
        except Exception:
            pass

    def _write_committed(self, manifest: Mapping[str, Any], records_bytes: bytes) -> None:
        committed = self.committed_root
        if committed.exists() or committed.is_symlink():
            existing = self._committed_manifest(self.item_workspace)
            if existing is None or existing.get("manifest_hash") != manifest.get("manifest_hash"):
                raise ValueError("committed integration collision")
            existing_records = self._read_records(committed / _RECORDS_FILENAME, existing, self.bundle)
            self._validate_committed_artifacts(committed, existing_records)
            return
        integration_root = self._integration_root(self.item_workspace)
        self._ensure_safe_dir(integration_root)
        temporary = Path(tempfile.mkdtemp(prefix=".committed.tmp-", dir=integration_root))
        try:
            _atomic_write_bytes(temporary / _RECORDS_FILENAME, records_bytes)
            external_outputs: dict[str, Mapping[str, Any]] = {}
            external_artifact_paths: set[str] = set()
            for record in self._records:
                if record.kind != "analytical_artifact":
                    continue
                artifact, artifact_bytes = _parse_analytical_artifact_payload(
                    record.payload,
                    expected_item_id=self.item_id,
                )
                for relative, _source_path in self._validate_external_artifact_outputs(artifact):
                    descriptor = next(
                        (
                            value
                            for value in artifact.output_refs
                            if isinstance(value, Mapping) and value.get("path") == relative
                        ),
                        None,
                    )
                    if descriptor is None:  # pragma: no cover - validator already enforces this
                        raise ValueError(f"committed analytical output descriptor is missing: {relative}")
                    prior = external_outputs.get(relative)
                    if prior is not None and any(
                        prior.get(name) != descriptor.get(name)
                        for name in ("format", "sha256", "size_bytes", "row_count", "complete")
                    ):
                        raise ValueError(f"committed analytical output path collision: {relative}")
                    external_outputs[relative] = dict(descriptor)
                destination = self._committed_artifact_path(
                    temporary,
                    str(record.payload["artifact_ref"]),
                )
                artifact_relative = destination.relative_to(temporary / "artifacts").as_posix()
                external_artifact_paths.add(artifact_relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(destination, artifact_bytes)
            for relative, descriptor in sorted(external_outputs.items()):
                if relative in external_artifact_paths:
                    raise ValueError(f"committed analytical output collides with artifact file: {relative}")
                source_root = self.item_workspace.work_root
                _assert_no_symlink(source_root, label="accepted analytical output root")
                source = source_root / Path(relative)
                current_source = source_root
                for component in PurePosixPath(relative).parts:
                    current_source = current_source / component
                    _assert_no_symlink(current_source, label="accepted analytical output")
                if not source.is_file() or source.is_symlink():
                    raise ValueError(f"accepted analytical output is missing or not a regular file: {relative}")

                destination = temporary / "artifacts" / Path(relative)
                current = temporary / "artifacts"
                _assert_no_symlink(current, label="committed analytical artifact output root")
                for component in PurePosixPath(relative).parts:
                    current = current / component
                    _assert_no_symlink(current, label="committed analytical artifact output")
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with destination.open("wb") as sink:
                        observed_hash, observed_size, observed_rows = _stream_jsonl_file(
                            source,
                            label=f"accepted analytical output {relative}",
                            sink=sink,
                        )
                        sink.flush()
                        os.fsync(sink.fileno())
                except OSError as exc:
                    raise ValueError(f"committed analytical output cannot be copied: {relative}") from exc
                if (
                    observed_hash != descriptor.get("sha256")
                    or observed_size != descriptor.get("size_bytes")
                    or observed_rows != descriptor.get("row_count")
                    or descriptor.get("complete") is not True
                    or descriptor.get("format") != "jsonl"
                ):
                    raise ValueError(f"accepted analytical output changed during commit: {relative}")
            _atomic_write_json(temporary / _MANIFEST_FILENAME, manifest)
            self._validate_committed_artifacts(temporary, self._records)
            os.replace(temporary, committed)
            _fsync_directory(integration_root)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def commit(self) -> Mapping[str, Any]:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._commit_unlocked()

    def _reproject_prior(self) -> LivingEnterpriseModel:
        projection = LivingEnterpriseModelProjector.project(
            self.context,
            before_item_id=self.item_id,
        )
        self.lem_projection = projection
        self.lem = projection.model
        return projection.model

    def _candidate_lem(self, *, committed_at: str) -> LivingEnterpriseModel:
        """Build the post-commit model without persisting a second artifact."""

        candidate = LivingEnterpriseModel.from_export(self._reproject_prior().export())
        for record in self._records:
            if record.kind in {
                "ontology_item",
                "relationship",
                "limitation",
                "prepared_asset",
                "identity_decision",
                "canonical_mapping",
                "knowledge_delta",
            }:
                self._apply_lem_record_to_model(candidate, record, applied_at=committed_at)
        return candidate

    def _reproject_current(self) -> LivingEnterpriseModel:
        """Reload the committed-record projection including this item."""

        projection = LivingEnterpriseModelProjector.project(self.context, include_item_id=self.item_id)
        self.lem_projection = projection
        self.lem = projection.model
        return projection.model

    def _verify_candidate_projection(self, candidate: LivingEnterpriseModel) -> None:
        projected = self._reproject_current()
        if projected.export() != candidate.export():
            raise ValueError("committed integration projection does not match records")

    def _commit_unlocked(self) -> Mapping[str, Any]:
        """Validate, apply typed records, publish, and mark integration committed."""

        if self.status == "committed":
            manifest = self._committed_manifest(self.item_workspace)
            if manifest is None:
                raise ValueError("committed integration manifest is missing")
            existing_records = self._read_records(self.committed_root / _RECORDS_FILENAME, manifest, self.bundle)
            self._validate_committed_artifacts(self.committed_root, existing_records)
            if tuple(existing_records) != tuple(self._records):
                raise ValueError("committed integration records differ from staging")
            candidate = self._candidate_lem(committed_at=str(manifest["committed_at"]))
            if self.item_workspace.integration_state == "pending":
                self._preflight_all()
                self._apply_records()
                self._verify_candidate_projection(candidate)
                self._finish_committed(manifest)
            else:
                self._ensure_bundle_current()
                if self.item_workspace.integration_manifest_hash != manifest.get("manifest_hash"):
                    raise ValueError("item integration state does not match committed manifest")
                self._verify_candidate_projection(candidate)
                self._finish_committed(manifest)
            return dict(manifest)
        self._require_open()
        existing_manifest = self._committed_manifest(self.item_workspace)
        if existing_manifest is not None:
            if (
                existing_manifest.get("session_id") != self.session_id
                or existing_manifest.get("owner_id") != self.owner_id
                or existing_manifest.get("invocation_id") != self.invocation_id
                or existing_manifest.get("accepted_content_hash") != self.bundle.content_hash
            ):
                raise ValueError("committed integration identity collision")
            existing_records = self._read_records(
                self.committed_root / _RECORDS_FILENAME,
                existing_manifest,
                self.bundle,
            )
            self._validate_committed_artifacts(self.committed_root, existing_records)
            if tuple(existing_records) != tuple(self._records):
                raise ValueError("committed integration records differ from staging")
            candidate = self._candidate_lem(committed_at=str(existing_manifest["committed_at"]))
            if self.item_workspace.integration_state == "pending":
                self._preflight_all()
                self._apply_records()
                self._verify_candidate_projection(candidate)
                self._finish_committed(existing_manifest)
            else:
                self._ensure_bundle_current()
                if self.item_workspace.integration_manifest_hash != existing_manifest.get("manifest_hash"):
                    raise ValueError("item integration state does not match committed manifest")
                self._verify_candidate_projection(candidate)
                self._finish_committed(existing_manifest)
            return dict(existing_manifest)
        if self.item_workspace.integration_state != "pending":
            raise ValueError("item integration is no longer pending")
        # The item-local fidelity result is the only semantic acceptance gate;
        # no registry or LEM mutation occurs before this durable pass.
        self._require_fidelity_acceptance()
        self._preflight_all()
        validation = self.validate()
        records_bytes = self._records_bytes()
        records_hash = _sha256_bytes(records_bytes)
        intent = self._read_intent(
            self.staging_root / _INTENT_FILENAME,
            self.bundle,
            session_id=self.session_id,
            owner_id=self.owner_id,
            invocation_id=self.invocation_id,
        )
        if intent is not None:
            manifest = dict(intent["manifest"])
            if manifest.get("records_hash") != records_hash or manifest.get("records_count") != len(self._records):
                raise ValueError("integration commit intent records differ from staging")
        else:
            manifest = self._build_manifest(records_hash, validation, _now())
            intent = {
                "schema_version": _SCHEMA_VERSION,
                "session_id": self.session_id,
                "item_id": self.item_id,
                "owner_id": self.owner_id,
                "invocation_id": self.invocation_id,
                "manifest": manifest,
            }
            intent["intent_hash"] = _sha256_value(intent)
        # The durable intent is written before registry/LEM mutations.  A
        # crash after a partial apply therefore retries this exact plan.
        _atomic_write_json(self.staging_root / _INTENT_FILENAME, intent)
        candidate = self._candidate_lem(committed_at=str(manifest["committed_at"]))
        self._apply_records()
        self._write_committed(manifest, records_bytes)
        self._verify_candidate_projection(candidate)
        self._finish_committed(manifest)
        return dict(manifest)

    def mark_technical_failure(
        self,
        reason: str,
        *,
        recovery_exhausted: bool = False,
    ) -> Mapping[str, Any]:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._mark_technical_failure_unlocked(
                reason,
                recovery_exhausted=recovery_exhausted,
            )

    def _record_incident_unlocked(self, reason: str) -> Mapping[str, Any]:
        """Record an accepted-integration fault without terminalizing it.

        Business acceptance is already sealed in ``accepted/``.  Handoff,
        staging, and post-fidelity transport faults therefore remain positive,
        same-session recovery work: a typed run incident is durable and
        idempotent while the item/session stay ``pending``/``open``.  The
        incident identity is derived from the exact session, accepted content,
        and reason so replaying the same compatibility call converges on the
        original record rather than appending duplicates.
        """

        from .requirement_planning import RequirementSupervisorWorkspace

        incident_id = "INC-" + _sha256_value(
            {
                "kind": "accepted_integration_recovery",
                "session_id": self.session_id,
                "item_id": self.item_id,
                "accepted_content_hash": self.bundle.content_hash,
                "reason": reason,
            }
        )[:24]
        incident = IncidentRecord(
            incident_id=incident_id,
            category="recovery",
            disposition="pending_same_session",
            admissible=False,
            item_id=self.item_id,
            scope=("integration", self.item_id),
            source="integration_session",
            facts={
                "session_id": self.session_id,
                "accepted_content_hash": self.bundle.content_hash,
                "reason": reason,
                "continuation": "same_session",
            },
        )
        recorded = RequirementSupervisorWorkspace(self.context).record_incident(incident)
        return {
            "status": "pending",
            "recoverable": True,
            "continuation": "same_session",
            "session_id": self.session_id,
            "item_id": self.item_id,
            "accepted_content_hash": self.bundle.content_hash,
            "incident": dict(recorded),
        }

    def record_incident(
        self,
        incident: IncidentRecord | Mapping[str, Any] | str,
    ) -> Mapping[str, Any]:
        """Persist a typed recoverable incident and reoffer this session.

        ``record_incident`` is the positive API.  A plain reason is accepted
        only as the narrow compatibility form used by older callers; typed
        ``IncidentRecord`` values remain authoritative for new integrations.
        All accepted-item incidents are constrained to this item/session and
        are idempotent through the run-level incident ledger.
        """

        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            if self.status != "open" or self.item_workspace.integration_state != "pending":
                raise ValueError("integration incident requires an open pending session")
            if isinstance(incident, str):
                reason = incident.strip()
                if not reason:
                    raise ValueError("integration incident reason is required")
                return self._record_incident_unlocked(reason)
            value = incident if isinstance(incident, IncidentRecord) else IncidentRecord.from_dict(incident)
            if value.item_id not in {None, self.item_id}:
                raise ValueError("integration incident item_id does not match the session")
            if value.category != "recovery":
                raise ValueError("accepted integration incidents must use recovery category")
            facts = dict(value.facts)
            reason = facts.get("reason") or value.disposition
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("integration incident reason is required")
            facts.update(
                {
                    "session_id": self.session_id,
                    "accepted_content_hash": self.bundle.content_hash,
                    "continuation": "same_session",
                }
            )
            normalized = IncidentRecord(
                incident_id=value.incident_id,
                category="recovery",
                disposition=value.disposition,
                admissible=False,
                item_id=self.item_id,
                scope=value.scope or ("integration", self.item_id),
                source=value.source or "integration_session",
                facts=facts,
            )
            from .requirement_planning import RequirementSupervisorWorkspace

            recorded = RequirementSupervisorWorkspace(self.context).record_incident(normalized)
            return {
                "status": "pending",
                "recoverable": True,
                "continuation": "same_session",
                "session_id": self.session_id,
                "item_id": self.item_id,
                "accepted_content_hash": self.bundle.content_hash,
                "incident": dict(recorded),
            }

    def _mark_technical_failure_unlocked(
        self,
        reason: str,
        *,
        recovery_exhausted: bool = False,
    ) -> Mapping[str, Any]:
        """Normalize failure-shaped calls into positive recovery incidents.

        ``IntegrationSession`` exists only after analytical acceptance.  A
        technical failure at this boundary must preserve that accepted content
        and leave the same session available for staging/fidelity continuation;
        it is never a new terminal item outcome.
        """

        reason = str(reason).strip()
        if not reason:
            raise ValueError("technical failure reason is required")
        if self._lease is None:
            raise ValueError("integration invocation lease is released")
        # Once a commit intent exists, registry/LEM application may already be
        # partially durable.  Terminalizing the item at that point would leave
        # an accepted registry entry without an accepted integration outcome;
        # recovery must instead retry the exact intent to convergence.
        if (self.staging_root / _INTENT_FILENAME).exists() or (self.staging_root / _INTENT_FILENAME).is_symlink():
            raise ValueError("integration commit intent exists; retry commit instead of technical failure")
        if self.status == "open" and self.item_workspace.integration_state == "pending" and not recovery_exhausted:
            return self._record_incident_unlocked(reason)
        failure_root = self._integration_root(self.item_workspace) / _TECHNICAL_FAILURE_DIR
        failure_path = failure_root / _MANIFEST_FILENAME
        manifest: dict[str, Any] | None = None
        if failure_root.exists() or failure_root.is_symlink():
            loaded = self._technical_failure_manifest(self.item_workspace, self.bundle)
            if loaded is None:  # pragma: no cover - root existence checked above
                raise ValueError("technical failure manifest is missing")
            if loaded.get("session_id") != self.session_id or loaded.get("owner_id") != self.owner_id:
                raise ValueError("technical failure manifest identity is invalid")
            manifest = dict(loaded)
        # Historical terminal manifests remain readable for old runs, but no
        # current accepted/pending call reaches this branch: the positive
        # incident path above handles it before any terminal write.
        if manifest is None:
            if self.status != "open":
                raise ValueError("integration session is terminal")
            if self.item_workspace.integration_state != "pending":
                raise ValueError("item integration state has no failure manifest")
            self._ensure_safe_dir(failure_root)
            manifest = {
                "schema_version": _SCHEMA_VERSION,
                "session_id": self.session_id,
                "item_id": self.item_id,
                "owner_id": self.owner_id,
                "status": "technical_failure",
                "accepted_content_hash": self.bundle.content_hash,
                "reason": reason,
                "created_at": _now(),
            }
            if recovery_exhausted:
                # This marker is covered by the manifest hash and lets the
                # Planner distinguish an explicit exhausted terminal boundary
                # from historical/recoverable failure evidence.
                manifest["recovery_exhausted"] = True
            manifest["manifest_hash"] = _sha256_value(manifest)
            _atomic_write_json(failure_path, manifest)
        elif reason and manifest.get("reason") != reason:
            # A retry must converge on the first durable reason, never rewrite
            # a terminal failure with a competing explanation.
            raise ValueError("technical failure reason differs from durable manifest")
        if recovery_exhausted and manifest.get("recovery_exhausted") is not True:
            raise ValueError("historical technical failure is recoverable; exhaustion marker is missing")
        if self.item_workspace.integration_state == "pending":
            self.item_workspace.mark_integration_failed(
                manifest["manifest_hash"],
                "integration/technical_failure/manifest.json",
                recovery_exhausted=recovery_exhausted,
            )
        elif self.item_workspace.integration_state == "technical_failure":
            if self.item_workspace.integration_manifest_hash != manifest["manifest_hash"]:
                raise ValueError("item integration state does not match technical failure manifest")
        else:
            raise ValueError("item integration state does not permit technical failure")
        state = dict(self._state)
        state["status"] = "technical_failure"
        state["updated_at"] = manifest.get("created_at", _now())
        self._persist_state(state)
        self.release()
        return dict(manifest)

    def finalize_technical_failure(self, reason: str) -> Mapping[str, Any]:
        """Settle accepted integration only after retry exhaustion.

        The default ``mark_technical_failure`` call remains recoverable and
        reoffers the same session.  Coordinator recovery invokes this explicit
        terminal path; accepted business bytes are never rewritten.
        """

        return self.mark_technical_failure(reason, recovery_exhausted=True)

    technical_failure = mark_technical_failure
    record_failure = mark_technical_failure


__all__ = ["AcceptedAnalysisBundle", "IntegrationRecord", "IntegrationSession", "IntegrationValidation"]
