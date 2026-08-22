"""Run-scoped, durable entity-resolution runtime.

The entity-resolution lane is deliberately independent from item-local
analytical execution.  Owners can reserve a domain, publish one reviewed
resolution result, and commit it atomically under ``run_root/entity_resolution``.
The module only persists and validates typed identity evidence; matching and
bulk/pattern strategies remain owner supplied metadata.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - POSIX is the supported runtime
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .contracts import CanonicalMapping, IdentityDecision, OntologyItem
from .enterprise_model import LivingEnterpriseModel
from .workspace import AllowedRootError, RunContext


SCHEMA_VERSION = "auto_foundry.entity_resolution.v1"
# Commit manifests remain on the v1 artifact schema.  The run-level state
# gained request collections in v2 and is upgraded once from the exact v1
# legacy shape under the existing entity lock.
STATE_SCHEMA_VERSION = "auto_foundry.entity_resolution.state.v2"
LEGACY_STATE_SCHEMA_VERSION = SCHEMA_VERSION
_STATE_FILENAME = "state.json"
_LOCK_FILENAME = ".entity_resolution.lock"
_DOMAINS_DIR = "domains"
_COMMITS_DIR = "committed"
_RECORDS_FILENAME = "records.jsonl"
_RESULT_FILENAME = "result.json"
_MANIFEST_FILENAME = "manifest.json"
_WORKER_TYPES = frozenset({"entity_resolution", "analytical_owner", "specialist"})
_FORBIDDEN_WORKER_TYPES = frozenset({"planner", "control_plane", "control-plane"})
_DOMAIN_STATES = frozenset({"reserved", "resolving", "review_pending", "repair", "ready", "failed"})
_REVIEW_VERDICTS = frozenset({"accept", "repair_once", "fail"})
_HEX = frozenset("0123456789abcdef")
_OWNER_RECOVERY_AUDIT_FIELDS = frozenset({
    "event",
    "prior_owner_ref",
    "lease_id",
    "recovery_owner_ref",
    "reason",
    "lease_acquired_at",
    "stale_before",
    "recovered_at",
})
_IDENTITY_REQUEST_FIELDS = frozenset({
    "item_id",
    "owner_ref",
    "object_type",
    "rationale",
    "source_hints",
    "representation_item_ids",
})
_LEGACY_STATE_FIELDS = frozenset({
    "schema_version",
    "run_id",
    "capacity",
    "leases",
    "domains",
    "waits",
    "updated_at",
    "state_hash",
})
_LEGACY_DOMAIN_FIELDS = frozenset({
    "domain_id",
    "canonical_identity",
    "object_type",
    "discovered_by_item_id",
    "rationale",
    "source_hints",
    "representation_item_ids",
    "state",
    "resolution_owner",
    "reviewer_ref",
    "review_verdict",
    "repair_count",
    "result_hash",
    "commit_manifest_hash",
    "accepted_pending_commit",
    "resolution_owner_history",
})
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_HELD_LOCK_PATHS = threading.local()


class _RetryResolutionCommit(Exception):
    """Internal optimistic-conflict signal for two-phase resolution commit."""


class StaleIdentityScopeError(ValueError):
    """The resolver submitted work against an older material identity scope."""


def _scope_input_values(value: Any, label: str) -> tuple[Any, ...]:
    """Normalize one public scope input without treating a string as chars."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)):
        values = (value,)
    else:
        try:
            values = tuple(value)
        except TypeError as exc:
            raise ValueError(f"{label} must be a sequence") from exc
    normalized: list[Any] = []
    seen: set[bytes] = set()
    for item in values:
        if item is None:
            raise ValueError(f"{label} values must be non-null")
        if isinstance(item, str) and not item.strip():
            raise ValueError(f"{label} values must be non-empty")
        normalized_item = _jsonable(item)
        key = _canonical_bytes(normalized_item)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(normalized_item)
    return tuple(sorted(normalized, key=_canonical_bytes))


def _scope_representation_values(value: Any, label: str) -> tuple[str, ...]:
    values = _scope_input_values(value, label)
    return tuple(_text(item, label) for item in values)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any, label: str) -> datetime:
    text = _text(value, label)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        raise TypeError("entity-resolution artifacts cannot contain bytes")
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
        raise ValueError("entity-resolution value is not JSON-safe") from exc
    return (encoded + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX for char in value.lower())


_MANIFEST_FIELDS = frozenset({
    "schema_version", "kind", "domain_id", "canonical_identity", "object_type",
    "discovered_by_item_id", "result_hash", "source_hash", "records_path",
    "records_hash", "records_count", "reviewer_ref", "review_verdict", "coverage",
    "population", "exceptions", "unresolved", "evidence_refs", "script_receipt_refs",
    "metadata", "committed_at", "manifest_hash",
})


def _text(value: Any, label: str) -> str:
    if value is None:
        raise TypeError(f"{label} is required")
    result = str(value).strip()
    if not result or "\x00" in result or "\n" in result or "\r" in result:
        raise ValueError(f"{label} must be a non-empty stable identifier")
    return result


def _domain_text(value: Any) -> str:
    # Domain IDs are semantic identifiers, not path components.  Their hash is
    # used for directories so arbitrary domain names cannot escape the run.
    return _text(value, "domain_id")


def _safe_path_component(value: Any, label: str) -> str:
    result = _text(value, label)
    if result in {".", ".."} or Path(result).name != result or "\\" in result:
        raise ValueError(f"{label} must be a simple path component")
    return result


def _assert_no_symlink(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise AllowedRootError(f"{label} cannot be a symlink: {path}")
    if path.exists() and not (path.is_file() or path.is_dir()):
        raise ValueError(f"{label} is not a regular file or directory: {path}")


def _ensure_dir(path: Path, *, label: str) -> None:
    _assert_no_symlink(path, label=label)
    path.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink(path, label=label)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    _ensure_dir(path.parent, label="entity-resolution artifact directory")
    _assert_no_symlink(path, label="entity-resolution artifact")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _canonical_bytes(value))


@dataclass(frozen=True)
class ResolutionCapacity:
    """Run-level active-lane limits.

    The planner/control plane has no lease and is intentionally absent from
    this structure.
    """

    total_active: int = 8
    entity_resolution: int = 4
    analytical_owner: int = 1
    specialist: int = 3

    def __post_init__(self) -> None:
        for name in ("total_active", "entity_resolution", "analytical_owner", "specialist"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} capacity must be a non-negative integer")
        for name in ("entity_resolution", "analytical_owner", "specialist"):
            if getattr(self, name) > self.total_active:
                raise ValueError(f"{name} capacity cannot exceed total_active")

    def to_dict(self) -> dict[str, int]:
        return {
            "total_active": self.total_active,
            "entity_resolution": self.entity_resolution,
            "analytical_owner": self.analytical_owner,
            "specialist": self.specialist,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResolutionCapacity":
        if not isinstance(value, Mapping):
            raise ValueError("resolution capacity must be an object")
        expected = {"total_active", "entity_resolution", "analytical_owner", "specialist"}
        if set(value) != expected:
            raise ValueError("resolution capacity fields are invalid")
        return cls(**{key: value[key] for key in expected})


@dataclass(frozen=True)
class WorkerLease:
    lease_id: str
    worker_type: str
    owner_ref: str
    subject_id: str | None
    acquired_at: str

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "lease_id": self.lease_id,
            "worker_type": self.worker_type,
            "owner_ref": self.owner_ref,
            "subject_id": self.subject_id,
            "acquired_at": self.acquired_at,
        }
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerLease":
        expected = {"lease_id", "worker_type", "owner_ref", "subject_id", "acquired_at"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("worker lease fields are invalid")
        worker_type = _text(value["worker_type"], "worker_type").lower()
        if worker_type not in _WORKER_TYPES:
            raise ValueError("worker lease worker_type is invalid")
        subject = value.get("subject_id")
        return cls(
            lease_id=_safe_path_component(value["lease_id"], "lease_id"),
            worker_type=worker_type,
            owner_ref=_text(value["owner_ref"], "owner_ref"),
            subject_id=None if subject is None else _text(subject, "subject_id"),
            acquired_at=_text(value["acquired_at"], "acquired_at"),
        )


@dataclass(frozen=True)
class IdentityDomainRequest:
    """One requirement's semantic request for a shared identity domain."""

    item_id: str
    object_type: str
    rationale: str = ""
    source_hints: tuple[Any, ...] = ()
    representation_item_ids: tuple[str, ...] = ()
    owner_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id"))
        object.__setattr__(self, "object_type", _text(self.object_type, "object_type"))
        object.__setattr__(self, "rationale", str(self.rationale or ""))
        object.__setattr__(self, "source_hints", tuple(_jsonable(self.source_hints or ())))
        object.__setattr__(
            self,
            "representation_item_ids",
            tuple(_text(value, "representation_item_id") for value in (self.representation_item_ids or ())),
        )
        if self.owner_ref is not None:
            object.__setattr__(self, "owner_ref", _text(self.owner_ref, "owner_ref"))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "item_id": self.item_id,
            "object_type": self.object_type,
            "rationale": self.rationale,
            "source_hints": list(_jsonable(self.source_hints)),
            "representation_item_ids": list(self.representation_item_ids),
        }
        if self.owner_ref is not None:
            payload["owner_ref"] = self.owner_ref
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IdentityDomainRequest":
        required = _IDENTITY_REQUEST_FIELDS - {"owner_ref"}
        if (
            not isinstance(value, Mapping)
            or not required.issubset(value)
            or set(value).difference(_IDENTITY_REQUEST_FIELDS)
        ):
            raise ValueError("identity domain request fields are invalid")
        return cls(
            item_id=value["item_id"],
            owner_ref=value.get("owner_ref"),
            object_type=value["object_type"],
            rationale=value.get("rationale", ""),
            source_hints=value.get("source_hints", ()),
            representation_item_ids=value.get("representation_item_ids", ()),
        )


@dataclass(frozen=True)
class IdentityDomainScope:
    """The current material source/representation scope for one domain."""

    domain_id: str
    source_hints: tuple[Any, ...] = ()
    representation_item_ids: tuple[str, ...] = ()
    scope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_id", _domain_text(self.domain_id))
        object.__setattr__(self, "source_hints", _scope_input_values(self.source_hints, "source_hints"))
        object.__setattr__(
            self,
            "representation_item_ids",
            _scope_representation_values(self.representation_item_ids, "representation_item_ids"),
        )
        object.__setattr__(
            self,
            "scope_hash",
            _digest(
                {
                    "source_hints": list(_jsonable(self.source_hints)),
                    "representation_item_ids": list(self.representation_item_ids),
                }
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain_id": self.domain_id,
            "source_hints": list(_jsonable(self.source_hints)),
            "representation_item_ids": list(self.representation_item_ids),
            "scope_hash": self.scope_hash,
        }


@dataclass(frozen=True)
class IdentityDomainReservation:
    domain_id: str
    canonical_identity: str
    object_type: str
    discovered_by_item_id: str
    rationale: str
    source_hints: tuple[Any, ...] = ()
    representation_item_ids: tuple[str, ...] = ()
    state: str = "reserved"
    resolution_owner: str | None = None
    reviewer_ref: str | None = None
    review_verdict: str | None = None
    repair_count: int = 0
    result_hash: str | None = None
    result_scope_hash: str | None = None
    commit_manifest_hash: str | None = None
    accepted_pending_commit: bool = False
    resolution_owner_history: tuple[Mapping[str, Any], ...] = ()
    requested_by: tuple[str, ...] = ()
    requests: tuple[Mapping[str, Any], ...] = ()
    review: Mapping[str, Any] | None = None

    @property
    def request_records(self) -> tuple[Mapping[str, Any], ...]:
        """Read-only request records, named for callers inspecting provenance."""

        return self.requests

    @property
    def requesters(self) -> tuple[str, ...]:
        """Requirement IDs currently attached to this canonical domain."""

        return self.requested_by

    @property
    def material_scope(self) -> IdentityDomainScope:
        """Compute the material scope from this lock-consistent snapshot."""

        source_hints: list[Any] = list(self.source_hints)
        representation_item_ids: list[str] = list(self.representation_item_ids)
        for raw_request in self.requests:
            request = IdentityDomainRequest.from_dict(raw_request)
            source_hints.extend(request.source_hints)
            representation_item_ids.extend(request.representation_item_ids)
        return IdentityDomainScope(self.domain_id, tuple(source_hints), tuple(representation_item_ids))

    @property
    def scope_hash(self) -> str:
        """Stable hash of the material source/representation scope."""

        return self.material_scope.scope_hash

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_id", _domain_text(self.domain_id))
        object.__setattr__(self, "canonical_identity", _text(self.canonical_identity, "canonical_identity"))
        object.__setattr__(self, "object_type", _text(self.object_type, "object_type"))
        object.__setattr__(self, "discovered_by_item_id", _text(self.discovered_by_item_id, "discovered_by_item_id"))
        object.__setattr__(self, "rationale", str(self.rationale or ""))
        object.__setattr__(self, "source_hints", tuple(_jsonable(self.source_hints or ())))
        object.__setattr__(self, "representation_item_ids", tuple(_text(v, "representation_item_id") for v in (self.representation_item_ids or ())))
        if self.state not in _DOMAIN_STATES:
            raise ValueError(f"unsupported identity domain state: {self.state}")
        if isinstance(self.repair_count, bool) or not isinstance(self.repair_count, int) or self.repair_count not in {0, 1}:
            raise ValueError("identity domain repair_count must be 0 or 1")
        if self.result_hash is not None and not _is_hash(self.result_hash):
            raise ValueError("identity domain result_hash is invalid")
        if self.result_scope_hash is not None and not _is_hash(self.result_scope_hash):
            raise ValueError("identity domain result_scope_hash is invalid")
        if self.commit_manifest_hash is not None and not _is_hash(self.commit_manifest_hash):
            raise ValueError("identity domain commit_manifest_hash is invalid")
        if not isinstance(self.accepted_pending_commit, bool):
            raise ValueError("identity domain accepted_pending_commit is invalid")
        raw_requests = tuple(self.requests or ())
        if not raw_requests:
            raw_requests = (
                IdentityDomainRequest(
                    item_id=self.discovered_by_item_id,
                    object_type=self.object_type,
                    rationale=self.rationale,
                    source_hints=self.source_hints,
                    representation_item_ids=self.representation_item_ids,
                ).to_dict(),
            )
        request_values = tuple(
            value if isinstance(value, IdentityDomainRequest) else IdentityDomainRequest.from_dict(value)
            for value in raw_requests
        )
        by_item: list[str] = []
        for request in request_values:
            if request.item_id not in by_item:
                by_item.append(request.item_id)
        requested_by = tuple(_text(value, "requested_by item_id") for value in (self.requested_by or ()))
        if requested_by and tuple(requested_by) != tuple(by_item):
            raise ValueError("identity domain requested_by does not match request records")
        object.__setattr__(self, "requested_by", tuple(by_item))
        object.__setattr__(self, "requests", tuple(request.to_dict() for request in request_values))
        if self.review is not None:
            if not isinstance(self.review, Mapping):
                raise ValueError("identity domain review must be an object")
            object.__setattr__(self, "review", dict(_jsonable(self.review)))
        history: list[Mapping[str, Any]] = []
        for raw in self.resolution_owner_history or ():
            value = _jsonable(raw)
            if not isinstance(value, Mapping) or set(value) != _OWNER_RECOVERY_AUDIT_FIELDS:
                raise ValueError("identity domain resolution owner history is invalid")
            if value.get("event") != "resolution_owner_recovered":
                raise ValueError("identity domain resolution owner history event is invalid")
            for field_name in ("prior_owner_ref", "lease_id", "recovery_owner_ref", "reason"):
                _text(value.get(field_name), f"resolution owner history {field_name}")
            _safe_path_component(value.get("lease_id"), "resolution owner history lease_id")
            for field_name in ("lease_acquired_at", "stale_before", "recovered_at"):
                _timestamp(value.get(field_name), f"resolution owner history {field_name}")
            history.append(dict(value))
        object.__setattr__(self, "resolution_owner_history", tuple(history))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "domain_id": self.domain_id,
            "canonical_identity": self.canonical_identity,
            "object_type": self.object_type,
            "discovered_by_item_id": self.discovered_by_item_id,
            "rationale": self.rationale,
            "source_hints": list(_jsonable(self.source_hints)),
            "representation_item_ids": list(self.representation_item_ids),
            "state": self.state,
            "resolution_owner": self.resolution_owner,
            "reviewer_ref": self.reviewer_ref,
            "review_verdict": self.review_verdict,
            "repair_count": self.repair_count,
            "result_hash": self.result_hash,
            "result_scope_hash": self.result_scope_hash,
            "commit_manifest_hash": self.commit_manifest_hash,
            "accepted_pending_commit": self.accepted_pending_commit,
            "resolution_owner_history": [dict(value) for value in self.resolution_owner_history],
            "requested_by": list(self.requested_by),
            "requests": [dict(value) for value in self.requests],
        }
        if self.review is not None:
            payload["review"] = dict(_jsonable(self.review))
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IdentityDomainReservation":
        fields = {
            "domain_id",
            "canonical_identity",
            "object_type",
            "discovered_by_item_id",
            "rationale",
            "source_hints",
            "representation_item_ids",
            "state",
            "resolution_owner",
            "reviewer_ref",
            "review_verdict",
            "repair_count",
            "result_hash",
            "commit_manifest_hash",
            "accepted_pending_commit",
            "requested_by",
            "requests",
        }
        optional_fields = {"resolution_owner_history", "result_scope_hash"}
        if not isinstance(value, Mapping) or not fields.issubset(value) or set(value).difference(fields | optional_fields | {"review"}):
            raise ValueError("identity domain reservation fields are invalid")
        return cls(
            **{key: value[key] for key in fields},
            result_scope_hash=value.get("result_scope_hash"),
            resolution_owner_history=value.get("resolution_owner_history", ()),
            review=value.get("review"),
        )

def _tuple_refs(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    try:
        values = tuple(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be a sequence") from exc
    return tuple(_text(item, label) for item in values)


def _coerce_ontology(value: Any) -> OntologyItem:
    return value if isinstance(value, OntologyItem) else OntologyItem.from_dict(dict(value))


def _coerce_decision(value: Any) -> IdentityDecision:
    return value if isinstance(value, IdentityDecision) else IdentityDecision.from_dict(dict(value))


def _coerce_mapping(value: Any) -> CanonicalMapping:
    return value if isinstance(value, CanonicalMapping) else CanonicalMapping.from_dict(dict(value))


@dataclass(frozen=True)
class EntityResolutionResult:
    """One owner-submitted, reviewable resolution result.

    ``metadata`` is intentionally open-ended for pattern/bulk rules and
    owner-specific diagnostics.  It is persisted verbatim and no row-level
    review is inferred from it.
    """

    ontology_items: tuple[OntologyItem, ...] = ()
    identity_decisions: tuple[IdentityDecision, ...] = ()
    canonical_mappings: tuple[CanonicalMapping, ...] = ()
    representation_relationships: tuple[Mapping[str, Any], ...] = ()
    coverage: Mapping[str, Any] = field(default_factory=dict)
    population: Mapping[str, Any] = field(default_factory=dict)
    exceptions: tuple[Any, ...] = ()
    unresolved: tuple[Any, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    script_receipt_refs: tuple[str, ...] = ()
    source_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ontology_items", tuple(_coerce_ontology(v) for v in self.ontology_items or ()))
        object.__setattr__(self, "identity_decisions", tuple(_coerce_decision(v) for v in self.identity_decisions or ()))
        object.__setattr__(self, "canonical_mappings", tuple(_coerce_mapping(v) for v in self.canonical_mappings or ()))
        raw_representation = tuple(_jsonable(v) for v in (self.representation_relationships or ()))
        if any(not isinstance(v, Mapping) for v in raw_representation):
            raise ValueError("resolution representation relationships must be objects")
        representation = tuple(dict(v) for v in raw_representation)
        object.__setattr__(self, "representation_relationships", representation)
        object.__setattr__(self, "coverage", MappingProxyType(dict(_jsonable(self.coverage or {}))))
        object.__setattr__(self, "population", MappingProxyType(dict(_jsonable(self.population or {}))))
        object.__setattr__(self, "exceptions", tuple(_jsonable(self.exceptions or ())))
        object.__setattr__(self, "unresolved", tuple(_jsonable(self.unresolved or ())))
        object.__setattr__(self, "evidence_refs", _tuple_refs(self.evidence_refs, "evidence_ref"))
        object.__setattr__(self, "script_receipt_refs", _tuple_refs(self.script_receipt_refs, "script_receipt_ref"))
        if not _is_hash(self.source_hash):
            raise ValueError("source_hash must be a SHA-256 digest")
        object.__setattr__(self, "metadata", MappingProxyType(dict(_jsonable(self.metadata or {}))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ontology_items": [item.to_dict() for item in self.ontology_items],
            "identity_decisions": [decision.to_dict() for decision in self.identity_decisions],
            "canonical_mappings": [mapping.to_dict() for mapping in self.canonical_mappings],
            "representation_relationships": [_jsonable(value) for value in self.representation_relationships],
            "coverage": _jsonable(self.coverage),
            "population": _jsonable(self.population),
            "exceptions": _jsonable(self.exceptions),
            "unresolved": _jsonable(self.unresolved),
            "evidence_refs": list(self.evidence_refs),
            "script_receipt_refs": list(self.script_receipt_refs),
            "source_hash": self.source_hash,
            "metadata": _jsonable(self.metadata),
        }

    @property
    def result_hash(self) -> str:
        return _digest(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EntityResolutionResult":
        if not isinstance(value, Mapping):
            raise ValueError("resolution result must be an object")
        payload = dict(value)
        expected = {
            "ontology_items", "identity_decisions", "canonical_mappings",
            "representation_relationships", "coverage", "population", "exceptions", "unresolved",
            "evidence_refs", "script_receipt_refs", "source_hash", "metadata",
        }
        if not set(payload).issubset(expected) or "source_hash" not in payload:
            raise ValueError("resolution result fields are invalid")
        for field_name, default in {
            "ontology_items": (),
            "identity_decisions": (),
            "canonical_mappings": (),
            "representation_relationships": (),
            "coverage": {},
            "population": {},
            "exceptions": (),
            "unresolved": (),
            "evidence_refs": (),
            "script_receipt_refs": (),
            "metadata": {},
        }.items():
            payload.setdefault(field_name, default)
        return cls(**payload)


def _resolution_outcome(result: EntityResolutionResult) -> str:
    """Validate whether a result publishes mappings or proves that none exist."""

    outcome = str(result.metadata.get("resolution_outcome") or "").strip()
    if result.canonical_mappings:
        if outcome not in {"", "mapping_found"}:
            raise ValueError(
                "resolution_outcome must be mapping_found when canonical mappings are published"
            )
        return "mapping_found"

    if outcome != "no_mapping_found":
        raise ValueError(
            "resolution result with no canonical mappings requires "
            "metadata.resolution_outcome=no_mapping_found"
        )
    if result.ontology_items or result.identity_decisions or result.representation_relationships:
        raise ValueError(
            "no_mapping_found result cannot publish ontology, identity decisions, or relationships"
        )
    if not result.coverage or not result.population:
        raise ValueError("no_mapping_found result requires coverage and population evidence")
    if not result.unresolved:
        raise ValueError("no_mapping_found result requires explicit unresolved records")
    if not result.evidence_refs:
        raise ValueError("no_mapping_found result requires evidence_refs")
    return outcome


@dataclass(frozen=True)
class ResolutionCommit:
    domain_id: str
    manifest_hash: str
    records_hash: str
    result_hash: str
    records_path: str
    manifest_path: str
    source_hash: str
    record_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain_id": self.domain_id,
            "manifest_hash": self.manifest_hash,
            "records_hash": self.records_hash,
            "result_hash": self.result_hash,
            "records_path": self.records_path,
            "manifest_path": self.manifest_path,
            "source_hash": self.source_hash,
            "record_count": self.record_count,
        }

class EntityResolutionWorkspace:
    """Run-level identity-domain registry and resolution commit authority."""

    def __init__(self, context: RunContext, state: Mapping[str, Any]) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("EntityResolutionWorkspace requires a RunContext")
        self.context = context
        self.root = context.resolve_run_path("entity_resolution")
        self.state_path = self.root / _STATE_FILENAME
        self.domains_root = self.root / _DOMAINS_DIR
        self.commits_root = self.root / _COMMITS_DIR
        self._state = dict(state)

    @classmethod
    def create(
        cls,
        context: RunContext,
        *,
        capacity: ResolutionCapacity | Mapping[str, Any] | None = None,
    ) -> "EntityResolutionWorkspace":
        if not isinstance(context, RunContext):
            raise TypeError("EntityResolutionWorkspace requires a RunContext")
        root = context.resolve_run_path("entity_resolution")
        _assert_no_symlink(root, label="entity-resolution root")
        _ensure_dir(root, label="entity-resolution root")
        requested = capacity if isinstance(capacity, ResolutionCapacity) else ResolutionCapacity.from_dict(capacity) if capacity is not None else ResolutionCapacity()
        state_path = root / _STATE_FILENAME
        if state_path.exists() or state_path.is_symlink():
            workspace = cls.load(context)
            if workspace.capacity != requested:
                raise ValueError("entity-resolution workspace capacity conflicts with existing state")
            return workspace
        _ensure_dir(root / _DOMAINS_DIR, label="entity-resolution domains")
        _ensure_dir(root / _COMMITS_DIR, label="entity-resolution commits")
        now = _now()
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "run_id": context.run_id,
            "capacity": requested.to_dict(),
            "leases": [],
            "domains": {},
            "waits": {},
            "updated_at": now,
        }
        state["state_hash"] = _digest(state)
        _atomic_write_json(state_path, state)
        return cls(context, state)

    @classmethod
    def _upgrade_legacy_state_value(cls, context: RunContext, state: Any) -> dict[str, Any] | None:
        """Upgrade exactly the pre-request v1 state shape.

        The caller owns the entity lock.  Unknown near-legacy shapes are
        rejected before any state is assigned or persisted; this is a one-time
        schema admission, not a compatibility fallback.
        """

        if not isinstance(state, Mapping) or state.get("schema_version") != LEGACY_STATE_SCHEMA_VERSION:
            return None
        if set(state) != _LEGACY_STATE_FIELDS:
            raise ValueError("legacy entity-resolution state fields are invalid")
        if state.get("run_id") != context.run_id:
            raise ValueError("legacy entity-resolution state identity is invalid")
        if state.get("state_hash") != _digest({key: value for key, value in state.items() if key != "state_hash"}):
            raise ValueError("legacy entity-resolution state hash does not match content")
        domains = state.get("domains")
        if not isinstance(domains, Mapping):
            raise ValueError("legacy entity-resolution domains are invalid")
        upgraded_domains: dict[str, dict[str, Any]] = {}
        for domain_id, raw in domains.items():
            if not isinstance(raw, Mapping):
                raise ValueError("legacy entity-resolution domain is invalid")
            domain_fields = set(raw)
            allowed = {_LEGACY_DOMAIN_FIELDS, _LEGACY_DOMAIN_FIELDS | {"review"}}
            if domain_fields not in allowed:
                raise ValueError("legacy identity domain reservation fields are invalid")
            if domain_id != raw.get("domain_id"):
                raise ValueError("legacy entity-resolution domain identity is invalid")
            domain = dict(raw)
            discovered = domain.get("discovered_by_item_id")
            domain["requested_by"] = [discovered]
            request = {
                "item_id": discovered,
                "object_type": domain.get("object_type"),
                "rationale": domain.get("rationale"),
                "source_hints": domain.get("source_hints"),
                "representation_item_ids": domain.get("representation_item_ids"),
            }
            # No owner_ref is invented.  If a later authoritative AO proposal
            # carries one, the Planner appends that requester separately.
            domain["requests"] = [request]
            upgraded_domains[str(domain_id)] = domain
        upgraded = dict(state)
        upgraded["schema_version"] = STATE_SCHEMA_VERSION
        upgraded["domains"] = upgraded_domains
        # Validate the complete v2 candidate before the first durable write.
        # The old v1 hash cannot be reused after adding request collections,
        # so compute the candidate hash in memory for the same full-state
        # validator used by ordinary v2 loads.
        upgraded.pop("state_hash", None)
        upgraded["state_hash"] = _digest(upgraded)
        cls._validate_state(context, upgraded)
        return upgraded

    @classmethod
    def load(cls, context: RunContext) -> "EntityResolutionWorkspace":
        if not isinstance(context, RunContext):
            raise TypeError("EntityResolutionWorkspace requires a RunContext")
        root = context.resolve_run_path("entity_resolution")
        _assert_no_symlink(root, label="entity-resolution root")
        state_path = root / _STATE_FILENAME
        if not state_path.is_file() or state_path.is_symlink():
            raise FileNotFoundError(state_path)
        _assert_no_symlink(state_path, label="entity-resolution state")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("entity-resolution state is invalid") from exc
        _assert_no_symlink(root / _DOMAINS_DIR, label="entity-resolution domains")
        _assert_no_symlink(root / _COMMITS_DIR, label="entity-resolution commits")
        if not (root / _DOMAINS_DIR).is_dir() or not (root / _COMMITS_DIR).is_dir():
            raise ValueError("entity-resolution artifact directories are missing")
        workspace = cls(context, state)
        # The first state read only establishes the file's shape.  Reconcile
        # against an authoritative snapshot after taking the run lock so a
        # concurrent domain/lease/wait update cannot be overwritten by a
        # stale crash-recovery projection.
        with workspace._locked():
            authoritative = json.loads(state_path.read_text(encoding="utf-8"))
            upgraded = cls._upgrade_legacy_state_value(context, authoritative)
            if upgraded is not None:
                workspace._state = upgraded
                workspace._persist()
            else:
                workspace._refresh()
            if workspace._reconcile_commit_refs():
                workspace._persist()
        return workspace

    @staticmethod
    def _validate_state(context: RunContext, state: Any) -> None:
        expected = {"schema_version", "run_id", "capacity", "leases", "domains", "waits", "updated_at", "state_hash"}
        if not isinstance(state, Mapping) or set(state) != expected:
            raise ValueError("entity-resolution state fields are invalid")
        if state.get("schema_version") != STATE_SCHEMA_VERSION or state.get("run_id") != context.run_id:
            raise ValueError("entity-resolution state identity is invalid")
        if state.get("state_hash") != _digest({key: value for key, value in state.items() if key != "state_hash"}):
            raise ValueError("entity-resolution state hash does not match content")
        ResolutionCapacity.from_dict(state["capacity"])
        if not isinstance(state.get("leases"), list) or not isinstance(state.get("domains"), Mapping) or not isinstance(state.get("waits"), Mapping):
            raise ValueError("entity-resolution state collections are invalid")
        leases = [WorkerLease.from_dict(value) for value in state["leases"]]
        if len({lease.lease_id for lease in leases}) != len(leases):
            raise ValueError("entity-resolution state contains duplicate leases")
        for domain_id, raw in state["domains"].items():
            if domain_id != raw.get("domain_id"):
                raise ValueError("entity-resolution domain identity is invalid")
            IdentityDomainReservation.from_dict(raw)

    def _lock_key(self) -> str:
        return str(self.root.resolve())

    @contextmanager
    def _locked(self):
        _ensure_dir(self.root, label="entity-resolution root")
        lock_path = self.root / _LOCK_FILENAME
        _assert_no_symlink(lock_path, label="entity-resolution lock")
        key = self._lock_key()
        with _PROCESS_LOCKS_GUARD:
            lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
        with lock:
            held = getattr(_HELD_LOCK_PATHS, "paths", None)
            if held is None:
                held = set()
                _HELD_LOCK_PATHS.paths = held
            if key in held:
                yield
                return
            with lock_path.open("a+b") as stream:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                held.add(key)
                try:
                    yield
                finally:
                    held.discard(key)
                    if fcntl is not None:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _refresh(self) -> None:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._validate_state(self.context, state)
        self._state = dict(state)

    def _persist(self) -> None:
        state = dict(self._state)
        state["updated_at"] = _now()
        state.pop("state_hash", None)
        state["state_hash"] = _digest(state)
        _atomic_write_json(self.state_path, state)
        self._state = state

    def _reconcile_commit_refs(self) -> bool:
        """Bind crash-published commit directories back into state idempotently.

        The caller must hold ``_locked``.  Returning the change bit keeps the
        state write in that same critical section and avoids persisting a
        stale in-memory snapshot from a nested lock acquisition.
        """
        # Stage every binding in a detached state.  A later malformed commit
        # must not leave an earlier valid directory reconciled in this public
        # workspace snapshot while the durable state file remains unchanged.
        staged_state = copy.deepcopy(self._state)
        changed = False
        for path in sorted(self.commits_root.glob("*/")) if self.commits_root.exists() else ():
            if path.is_symlink() or not path.is_dir():
                raise AllowedRootError(f"entity-resolution commit directory is invalid: {path}")
            manifest_path = path / _MANIFEST_FILENAME
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise ValueError("entity-resolution commit manifest is missing")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("entity-resolution commit manifest is invalid") from exc
            _validate_manifest(manifest)
            _read_records(self, manifest, path)
            domain_id = manifest.get("domain_id") if isinstance(manifest, Mapping) else None
            if domain_id not in staged_state["domains"]:
                raise ValueError("entity-resolution commit references an unknown domain")
            entry = staged_state["domains"][domain_id]
            if entry.get("state") == "failed":
                raise ValueError("failed identity domain cannot have a committed publication")
            expected = entry.get("commit_manifest_hash")
            if expected is not None and expected != manifest.get("manifest_hash"):
                raise ValueError("entity-resolution state commit binding is stale")
            if manifest.get("result_hash") != entry.get("result_hash"):
                raise ValueError(
                    "crash-published entity-resolution commit result does not match the current candidate"
                )
            result_scope_hash = entry.get("result_scope_hash")
            if entry.get("state") != "ready":
                if entry.get("accepted_pending_commit") is not True:
                    raise ValueError(
                        "crash-published entity-resolution commit lacks an accepted candidate boundary"
                    )
                current_scope = self._scope_for_entry(domain_id, entry)
                if not _is_hash(result_scope_hash) or result_scope_hash != current_scope.scope_hash:
                    raise ValueError(
                        "crash-published entity-resolution commit scope does not match the current candidate"
                    )
            elif result_scope_hash is not None:
                current_scope = self._scope_for_entry(domain_id, entry)
                if result_scope_hash != current_scope.scope_hash:
                    raise ValueError(
                        "ready entity-resolution commit scope does not match the current material scope"
                    )
            self._load_result(domain_id, entry)
            if expected != manifest.get("manifest_hash"):
                entry = dict(staged_state["domains"][domain_id])
                entry["commit_manifest_hash"] = manifest.get("manifest_hash")
                entry["state"] = "ready"
                entry["accepted_pending_commit"] = False
                staged_state["domains"][domain_id] = entry
                changed = True
        if changed:
            self._state = staged_state
        return changed

    @property
    def capacity(self) -> ResolutionCapacity:
        return ResolutionCapacity.from_dict(self._state["capacity"])

    @property
    def state(self) -> Mapping[str, Any]:
        """Immutable view of the authoritative run-level state."""
        return MappingProxyType(_jsonable(self._state))

    @property
    def active_leases(self) -> tuple[WorkerLease, ...]:
        return tuple(WorkerLease.from_dict(value) for value in self._state["leases"])

    @property
    def active_resolution_count(self) -> int:
        return sum(lease.worker_type == "entity_resolution" for lease in self.active_leases)

    def _domain_entry(self, domain_id: Any) -> dict[str, Any]:
        key = _domain_text(domain_id)
        value = self._state["domains"].get(key)
        if value is None:
            raise KeyError(key)
        return dict(value)

    def domains(self) -> tuple[IdentityDomainReservation, ...]:
        return tuple(IdentityDomainReservation.from_dict(self._state["domains"][key]) for key in sorted(self._state["domains"]))

    def get_domain(self, domain_id: str) -> IdentityDomainReservation:
        return IdentityDomainReservation.from_dict(self._domain_entry(domain_id))

    @staticmethod
    def _scope_for_entry(domain_id: str, entry: Mapping[str, Any]) -> IdentityDomainScope:
        source_hints: list[Any] = list(entry.get("source_hints", ()))
        representation_item_ids: list[str] = list(entry.get("representation_item_ids", ()))
        for raw_request in entry.get("requests", ()):
            request = IdentityDomainRequest.from_dict(raw_request)
            source_hints.extend(request.source_hints)
            representation_item_ids.extend(request.representation_item_ids)
        return IdentityDomainScope(domain_id, tuple(source_hints), tuple(representation_item_ids))

    def current_scope(self, domain_id: str) -> IdentityDomainScope:
        """Return the lock-consistent material scope and its stable token."""

        domain_id = _domain_text(domain_id)
        with self._locked():
            self._refresh()
            return self._scope_for_entry(domain_id, self._domain_entry(domain_id))

    def _has_active_resolution_lease(self, domain_id: str, owner_ref: str) -> bool:
        return any(
            value.get("worker_type") == "entity_resolution"
            and value.get("owner_ref") == owner_ref
            and value.get("subject_id") == domain_id
            for value in self._state["leases"]
        )

    @staticmethod
    def _invalidate_review_candidate(entry: dict[str, Any]) -> bool:
        if entry.get("state") != "review_pending":
            return False
        entry["state"] = "resolving"
        entry["reviewer_ref"] = None
        entry["review_verdict"] = None
        entry["review"] = None
        entry["result_hash"] = None
        entry["result_scope_hash"] = None
        entry["accepted_pending_commit"] = False
        return True

    def record_scope_discovery(
        self,
        domain_id: str,
        owner_ref: str,
        *,
        source_hints: Iterable[Any] = (),
        representation_item_ids: Iterable[str] = (),
    ) -> Mapping[str, Any]:
        """Idempotently add owner-discovered material scope to an active domain."""

        domain_id = _domain_text(domain_id)
        owner = _text(owner_ref, "owner_ref")
        discovered_sources = _scope_input_values(source_hints, "source_hints")
        discovered_representations = _scope_representation_values(
            representation_item_ids,
            "representation_item_ids",
        )
        if not discovered_sources and not discovered_representations:
            raise ValueError("scope discovery must add a source hint or representation item ID")
        with self._locked():
            self._refresh()
            reconciled = self._reconcile_commit_refs()

            def persist_reconciliation() -> None:
                if reconciled:
                    self._persist()

            if domain_id not in self._state["domains"]:
                persist_reconciliation()
                raise KeyError(domain_id)
            entry = self._domain_entry(domain_id)
            if entry.get("state") in {"failed", "ready"} or entry.get("commit_manifest_hash") is not None:
                persist_reconciliation()
                raise ValueError("identity domain is not an active, uncommitted resolution domain")
            if entry.get("resolution_owner") != owner:
                persist_reconciliation()
                raise ValueError("scope discovery owner does not own the domain")
            if not self._has_active_resolution_lease(domain_id, owner):
                persist_reconciliation()
                raise ValueError("scope discovery requires the active resolution-owner lease")

            current = self._scope_for_entry(domain_id, entry)
            existing_sources = {_canonical_bytes(value) for value in current.source_hints}
            existing_representations = set(current.representation_item_ids)
            added_sources = tuple(
                value for value in discovered_sources
                if _canonical_bytes(value) not in existing_sources
            )
            added_representations = tuple(
                value for value in discovered_representations
                if value not in existing_representations
            )
            if added_sources or added_representations:
                merged_sources = _scope_input_values(
                    (*entry.get("source_hints", ()), *added_sources),
                    "source_hints",
                )
                merged_representations = _scope_representation_values(
                    (*entry.get("representation_item_ids", ()), *added_representations),
                    "representation_item_ids",
                )
                entry["source_hints"] = list(merged_sources)
                entry["representation_item_ids"] = list(merged_representations)
                self._invalidate_review_candidate(entry)
                self._state["domains"][domain_id] = entry
                self._persist()
                current = self._scope_for_entry(domain_id, entry)
                status = "added"
            else:
                persist_reconciliation()
                status = "already_present"
            return {
                "domain_id": domain_id,
                "status": status,
                "added_source_hints": list(_jsonable(added_sources)),
                "added_representation_item_ids": list(added_representations),
                "source_hints": list(_jsonable(current.source_hints)),
                "representation_item_ids": list(current.representation_item_ids),
                "scope_hash": current.scope_hash,
                "scope": current.to_dict(),
            }

    def reserve_identity_domain(
        self,
        domain_id: str,
        object_type: str,
        discovered_by_item_id: str,
        rationale: str,
        *,
        source_hints: Iterable[Any] = (),
        representation_item_ids: Iterable[str] = (),
        canonical_identity: str | None = None,
        request_owner_ref: str | None = None,
    ) -> IdentityDomainReservation:
        domain_id = _domain_text(domain_id)
        canonical_supplied = canonical_identity is not None
        canonical = _text(canonical_identity if canonical_supplied else domain_id, "canonical_identity")
        source_hints_tuple = tuple(source_hints or ())
        representation_item_ids_tuple = tuple(representation_item_ids or ())
        request = IdentityDomainRequest(
            item_id=discovered_by_item_id,
            owner_ref=request_owner_ref,
            object_type=object_type,
            rationale=rationale,
            source_hints=source_hints_tuple,
            representation_item_ids=representation_item_ids_tuple,
        )
        # A materialized Requirement Mode item is the authority for discovering
        # its semantic dependency.  This prevents a Planner/bootstrap process
        # from pre-reserving guessed domains while preserving run-level use in
        # contexts that do not have a requirement item.
        from .durable import ItemWorkspace
        from .lifecycle import RunLifecycle

        try:
            lifecycle = RunLifecycle.load(self.context)
        except FileNotFoundError:
            lifecycle = None
        requirement_mode = lifecycle is not None and lifecycle.snapshot.mode == "requirement"

        try:
            item = ItemWorkspace.load(
                self.context,
                request.item_id,
                mode="requirement",
            )
        except FileNotFoundError:
            if requirement_mode:
                raise ValueError(
                    "requirement identity domain reservation requires a materialized Requirement item"
                )
            item = None
        except ValueError as exc:
            # A Question Mode item may legitimately use the run-level API;
            # this admission gate is specifically for materialized Requirement
            # Mode scheduling, where the observed Planner bypass occurred.
            if "mode does not match requested mode" not in str(exc):
                raise
            if requirement_mode:
                raise ValueError(
                    "requirement identity domain reservation item mode does not match Requirement mode"
                ) from exc
            item = None
        if item is not None:
            matches = [
                proposal
                for proposal in item.read_identity_domain_proposals()
                if proposal.get("record_kind") == "identity_domain_proposal"
                and proposal.get("domain_id") == domain_id
            ]
            if not matches:
                raise ValueError(
                    "requirement identity domain reservation requires an Analytical Owner proposal"
                )
            proposal = matches[0]
            proposal_owner = proposal.get("owner_ref")
            if not isinstance(proposal_owner, str) or not proposal_owner.strip():
                raise ValueError("identity domain proposal owner binding is invalid")
            proposal_owner = _text(proposal_owner, "proposal owner_ref")
            # The durable item binding is the current program-owned authority.
            # ``proposal_owner`` is retained as item-local provenance because
            # historical retries may carry a transport label that differs from
            # the current binding.  Admission still requires an exact bound
            # owner, so a fabricated request owner cannot borrow a proposal.
            bound_owner = _text(item.analysis_owner_ref(), "bound Analytical Owner")
            if request_owner_ref is not None and _text(request_owner_ref, "request_owner_ref") != bound_owner:
                raise ValueError("identity domain reservation owner does not match the bound Analytical Owner")
            request = replace(request, owner_ref=bound_owner)
            expected = {
                "item_id": request.item_id,
                "object_type": request.object_type,
                "rationale": request.rationale,
                "source_hints": list(request.source_hints),
                "representation_item_ids": list(request.representation_item_ids),
            }
            if any(proposal.get(key) != value for key, value in expected.items()):
                raise ValueError(
                    "identity domain reservation does not match the Analytical Owner proposal"
                )
        with self._locked():
            self._refresh()
            reconciled = self._reconcile_commit_refs()

            def persist_reconciliation() -> None:
                if reconciled:
                    self._persist()

            existing_raw = self._state["domains"].get(domain_id)
            if existing_raw is not None:
                existing = IdentityDomainReservation.from_dict(existing_raw)
                if not canonical_supplied:
                    canonical = existing.canonical_identity
                if existing.canonical_identity != canonical:
                    persist_reconciliation()
                    raise ValueError("identity domain canonical key conflicts with first reservation")
                if existing.object_type != request.object_type:
                    persist_reconciliation()
                    raise ValueError("identity domain object_type conflicts with first reservation")
                existing_requests = [IdentityDomainRequest.from_dict(value) for value in existing.requests]
                for prior in existing_requests:
                    if prior.item_id != request.item_id:
                        continue
                    # A repeated request from one requirement is idempotent;
                    # a materially different semantic request is not.
                    if (
                        prior.object_type != request.object_type
                        or prior.rationale != request.rationale
                        or tuple(prior.source_hints) != tuple(request.source_hints)
                        or tuple(prior.representation_item_ids) != tuple(request.representation_item_ids)
                    ):
                        persist_reconciliation()
                        raise ValueError("identity domain request conflicts with prior request")
                    persist_reconciliation()
                    return existing
                updated_requests = (*existing_requests, request)
                prior_scope = self._scope_for_entry(domain_id, existing_raw)
                updated_scope = IdentityDomainScope(
                    domain_id,
                    (
                        *existing.source_hints,
                        *(value for prior in updated_requests for value in prior.source_hints),
                    ),
                    (
                        *existing.representation_item_ids,
                        *(value for prior in updated_requests for value in prior.representation_item_ids),
                    ),
                )
                material_scope_changed = prior_scope.scope_hash != updated_scope.scope_hash
                if material_scope_changed and (
                    existing.commit_manifest_hash is not None or existing.state == "ready"
                ):
                    persist_reconciliation()
                    raise ValueError(
                        "identity domain is committed and cannot expand material scope"
                    )
                updated = replace(
                    existing,
                    requested_by=tuple((*existing.requested_by, request.item_id)),
                    requests=tuple(value.to_dict() for value in updated_requests),
                    source_hints=updated_scope.source_hints,
                    representation_item_ids=updated_scope.representation_item_ids,
                )
                updated_payload = updated.to_dict()
                if material_scope_changed:
                    # The prior candidate was bound to a narrower scope.  It
                    # remains useful as an orphaned historical artifact, but
                    # no longer has review or commit authority.  Keep the
                    # existing owner marker so Planner resume can reacquire
                    # the same domain instead of launching another resolver.
                    self._invalidate_review_candidate(updated_payload)
                self._state["domains"][domain_id] = updated_payload
                self._persist()
                return IdentityDomainReservation.from_dict(updated_payload)
            reservation = IdentityDomainReservation(
                domain_id=domain_id,
                canonical_identity=canonical,
                object_type=request.object_type,
                discovered_by_item_id=request.item_id,
                rationale=request.rationale,
                source_hints=request.source_hints,
                representation_item_ids=request.representation_item_ids,
                requested_by=(request.item_id,),
                requests=(request.to_dict(),),
            )
            # Distinct canonical domain keys stay distinct even when a caller
            # happens to provide the same optional identity label.  Any
            # collision in accepted ontology/mapping records is still checked
            # by the existing result/commit authority.
            self._state["domains"][domain_id] = reservation.to_dict()
            self._persist()
            return reservation

    def _normalize_worker_type(self, worker_type: Any) -> str:
        value = _text(worker_type, "worker_type").lower()
        if value in _FORBIDDEN_WORKER_TYPES or value not in _WORKER_TYPES:
            raise ValueError("planner/control-plane work is not a leaseable worker type")
        return value

    def claim_worker(
        self,
        worker_type: str,
        owner_ref: str,
        subject_id: str | None = None,
    ) -> WorkerLease:
        worker_type = self._normalize_worker_type(worker_type)
        owner = _text(owner_ref, "owner_ref")
        subject_value = None if subject_id is None else _text(subject_id, "subject_id")
        lease_id = "lease-" + hashlib.sha256(f"{worker_type}\0{owner}\0{subject_value or ''}".encode()).hexdigest()[:24]
        with self._locked():
            self._refresh()
            leases = [WorkerLease.from_dict(value) for value in self._state["leases"]]
            for lease in leases:
                if lease.worker_type == worker_type and lease.subject_id == subject_value:
                    if lease.owner_ref == owner:
                        return lease
                    raise ValueError("worker subject is already leased by another owner")
            capacity = self.capacity
            if len(leases) >= capacity.total_active:
                raise ValueError("total active worker capacity is exhausted")
            per_type = sum(lease.worker_type == worker_type for lease in leases)
            if per_type >= getattr(capacity, worker_type):
                raise ValueError(f"{worker_type} active worker capacity is exhausted")
            lease = WorkerLease(lease_id, worker_type, owner, subject_value, _now())
            self._state["leases"].append(lease.to_dict())
            self._persist()
            return lease

    def _resolve_lease(self, lease: WorkerLease | Mapping[str, Any] | str | None) -> WorkerLease | None:
        if lease is None:
            return None
        if isinstance(lease, WorkerLease):
            return lease
        if isinstance(lease, Mapping):
            return WorkerLease.from_dict(lease)
        key = _safe_path_component(lease, "lease_id")
        for value in self._state["leases"]:
            if value.get("lease_id") == key:
                return WorkerLease.from_dict(value)
        return WorkerLease(key, "entity_resolution", "", None, "")

    def release_worker(
        self,
        lease: WorkerLease | Mapping[str, Any] | str | None = None,
        *,
        worker_type: str | None = None,
        owner_ref: str | None = None,
        subject_id: str | None = None,
        recovery: bool = False,
    ) -> bool:
        owner = None if owner_ref is None else _text(owner_ref, "owner_ref")
        wtype = None if worker_type is None else self._normalize_worker_type(worker_type)
        subject = None if subject_id is None else _text(subject_id, "subject_id")
        with self._locked():
            self._refresh()
            requested = self._resolve_lease(lease)
            # A recovery release still needs an exact owner proof.  The old
            # recovery bypass allowed one worker to remove another worker's
            # live lease; an explicit resolution-owner recovery API below is
            # the only path that may reassign a stale owner.
            if recovery and owner is None and requested is not None:
                owner = requested.owner_ref
            if recovery and owner is None:
                raise ValueError("recovery lease release requires an exact owner")
            active = [WorkerLease.from_dict(value) for value in self._state["leases"]]
            matches = []
            for value in active:
                if requested is not None and value.lease_id != requested.lease_id:
                    continue
                if wtype is not None and value.worker_type != wtype:
                    continue
                if owner is not None and value.owner_ref != owner:
                    continue
                if subject is not None and value.subject_id != subject:
                    continue
                matches.append(value)
            if requested is not None and not matches and requested.owner_ref and owner and requested.owner_ref != owner:
                raise ValueError("worker lease is owned by another owner")
            if not matches:
                return False
            self._state["leases"] = [value.to_dict() for value in active if value not in matches]
            self._persist()
            return True


    def claim_resolution_owner(self, domain_id: str, owner_ref: str) -> WorkerLease:
        domain_id = _domain_text(domain_id)
        owner = _text(owner_ref, "owner_ref")
        with self._locked():
            self._refresh()
            entry = self._domain_entry(domain_id)
            if entry["state"] in {"failed", "ready"}:
                raise ValueError("identity domain is not claimable")
            prior_owner = entry.get("resolution_owner")
            if prior_owner is not None and prior_owner != owner:
                raise ValueError("identity domain is owned by another resolution owner")
            lease = self.claim_worker("entity_resolution", owner, domain_id)
            entry["resolution_owner"] = owner
            if entry["state"] == "reserved":
                entry["state"] = "resolving"
            self._state["domains"][domain_id] = entry
            self._persist()
            return lease

    def release_resolution_owner(self, domain_id: str, owner_ref: str) -> bool:
        domain_id = _domain_text(domain_id)
        owner = _text(owner_ref, "owner_ref")
        with self._locked():
            self._refresh()
            entry = self._domain_entry(domain_id)
            if entry.get("resolution_owner") != owner:
                raise ValueError("identity domain is owned by another resolution owner")
            active = [WorkerLease.from_dict(value) for value in self._state["leases"]]
            matches = [
                value for value in active
                if value.worker_type == "entity_resolution"
                and value.owner_ref == owner
                and value.subject_id == domain_id
            ]
            if not matches:
                # A missing lease is not proof of a stale owner.  Preserve the
                # owner marker and require the explicit stale-recovery route.
                return False
            self._state["leases"] = [value.to_dict() for value in active if value not in matches]
            entry["resolution_owner"] = None
            if entry.get("state") == "resolving":
                entry["state"] = "reserved"
            self._state["domains"][domain_id] = entry
            self._persist()
            return True

    def recover_resolution_owner(
        self,
        domain_id: str,
        *,
        expected_lease_id: str,
        expected_owner_ref: str,
        stale_before: str,
        recovery_owner_ref: str,
        reason: str,
    ) -> Mapping[str, Any]:
        """Atomically recover an explicitly stale resolution-owner lease.

        ``stale_before`` is an operator-supplied cutoff, but it cannot be in
        the future.  The exact active lease must belong to the expected owner
        and have been acquired strictly before that cutoff; this prevents an
        operator from taking over a live resolver by merely requesting
        recovery.  The prior owner and recovery decision remain in the
        domain's append-only history before a replacement can claim it.
        """
        domain_id = _domain_text(domain_id)
        lease_id = _safe_path_component(expected_lease_id, "expected_lease_id")
        expected_owner = _text(expected_owner_ref, "expected_owner_ref")
        recovery_owner = _text(recovery_owner_ref, "recovery_owner_ref")
        recovery_reason = _text(reason, "recovery reason")
        cutoff = _timestamp(stale_before, "stale_before")
        now = datetime.now(timezone.utc)
        if cutoff > now:
            raise ValueError("stale_before cannot be in the future")
        with self._locked():
            self._refresh()
            entry = self._domain_entry(domain_id)
            if entry.get("state") in {"failed", "ready"}:
                raise ValueError("identity domain is not recoverable")
            if entry.get("resolution_owner") != expected_owner:
                raise ValueError("identity domain owner does not match expected stale owner")
            active = [WorkerLease.from_dict(value) for value in self._state["leases"]]
            matches = [
                value for value in active
                if value.lease_id == lease_id
                and value.worker_type == "entity_resolution"
                and value.subject_id == domain_id
            ]
            if not matches:
                raise ValueError("expected resolution-owner lease is not active")
            lease = matches[0]
            if lease.owner_ref != expected_owner:
                raise ValueError("expected resolution-owner lease belongs to another owner")
            acquired_at = _timestamp(lease.acquired_at, "resolution-owner lease acquired_at")
            if acquired_at >= cutoff:
                raise ValueError("resolution-owner lease is not stale")
            audit = {
                "event": "resolution_owner_recovered",
                "prior_owner_ref": expected_owner,
                "lease_id": lease.lease_id,
                "recovery_owner_ref": recovery_owner,
                "reason": recovery_reason,
                "lease_acquired_at": lease.acquired_at,
                "stale_before": cutoff.isoformat(),
                "recovered_at": _now(),
            }
            history = list(entry.get("resolution_owner_history", ()))
            history.append(audit)
            entry["resolution_owner_history"] = history
            entry["resolution_owner"] = None
            if entry.get("state") == "resolving":
                entry["state"] = "reserved"
            self._state["leases"] = [value.to_dict() for value in active if value.lease_id != lease.lease_id]
            self._state["domains"][domain_id] = entry
            self._persist()
            return dict(audit)

    def _result_path(self, domain_id: str) -> Path:
        digest = hashlib.sha256(domain_id.encode("utf-8")).hexdigest()
        directory = self.domains_root / digest
        _assert_no_symlink(directory, label="entity-resolution domain directory")
        return directory / _RESULT_FILENAME

    def work_root(self, domain_id: str) -> Path:
        """Return the run-scoped authoring root for one resolution domain."""
        domain_id = _domain_text(domain_id)
        self._domain_entry(domain_id)
        directory = self.domains_root / hashlib.sha256(domain_id.encode("utf-8")).hexdigest() / "work"
        _ensure_dir(directory, label="entity-resolution domain work directory")
        return directory

    def submit_result(
        self,
        domain_id: str,
        owner_ref: str,
        result: EntityResolutionResult | Mapping[str, Any],
        *,
        expected_scope_hash: str,
    ) -> EntityResolutionResult:
        domain_id = _domain_text(domain_id)
        owner = _text(owner_ref, "owner_ref")
        expected_scope_hash = _text(expected_scope_hash, "expected_scope_hash")
        if not _is_hash(expected_scope_hash):
            raise ValueError("expected_scope_hash must be a SHA-256 digest")
        normalized = result if isinstance(result, EntityResolutionResult) else EntityResolutionResult.from_dict(result)
        _resolution_outcome(normalized)
        # Validate the candidate against the same detached LEM base used by
        # commit *before* publishing it for independent review.  This keeps a
        # malformed owner result in the resolver's ordinary technical retry
        # loop instead of discovering the defect after the bounded business
        # repair has already been consumed.
        for _attempt in range(3):
            with self._locked():
                self._refresh()
                entry = self._domain_entry(domain_id)
                current_scope = self._scope_for_entry(domain_id, entry)
                if current_scope.scope_hash != expected_scope_hash:
                    raise StaleIdentityScopeError(
                        "stale scope: resolution result expected scope is stale; refresh current_scope and continue"
                    )
                optimistic_token = self._resolution_authority_token()
            before_token = self._resolution_authority_token()
            if before_token != optimistic_token:
                continue
            base = self._base_model_for_validation()
            after_token = self._resolution_authority_token()
            if before_token != after_token:
                continue
            self._apply_result(base, normalized)
            with self._locked():
                self._refresh()
                entry = self._domain_entry(domain_id)
                current_scope = self._scope_for_entry(domain_id, entry)
                if current_scope.scope_hash != expected_scope_hash:
                    raise StaleIdentityScopeError(
                        "stale scope: resolution result expected scope is stale; refresh current_scope and continue"
                    )
                if self._resolution_authority_token() != after_token:
                    continue
                if entry.get("resolution_owner") != owner:
                    raise ValueError("resolution result owner does not hold the domain")
                if entry["state"] not in {"resolving", "repair", "review_pending"}:
                    raise ValueError("identity domain is not accepting a resolution result")
                prior_result_hash = entry.get("result_hash")
                if entry["state"] == "review_pending" and prior_result_hash is not None:
                    self._assert_result_scope_current(domain_id, entry)
                    existing_result = self._load_result(domain_id, entry)
                    if existing_result.result_hash != normalized.result_hash:
                        raise ValueError("resolution result conflicts with existing submission")
                    # Exact review-pending retries are non-mutating.  Validate
                    # both the candidate artifact and its scope binding before
                    # returning so tampering cannot be hidden by idempotency.
                    return normalized
                if entry["state"] in {"resolving", "repair"} and not self._has_active_resolution_lease(domain_id, owner):
                    raise ValueError("resolution result submission requires the active resolution-owner lease")
                # A repaired domain is the only state that may replace a submitted
                # result.  The original owner must have reacquired its resolver
                # lease above; once the replacement is written, the domain waits
                # for a fresh independent review.  Resolving/review_pending remain
                # immutable (apart from exact idempotent retries).
                if entry["state"] != "repair" and prior_result_hash is not None and prior_result_hash != normalized.result_hash:
                    raise ValueError("resolution result conflicts with existing submission")
                path = self._result_path(domain_id)
                _ensure_dir(path.parent, label="entity-resolution domain directory")
                _atomic_write_json(
                    path,
                    {
                        "domain_id": domain_id,
                        "result": normalized.to_dict(),
                        "result_hash": normalized.result_hash,
                        "result_scope_hash": current_scope.scope_hash,
                    },
                )
                entry["result_hash"] = normalized.result_hash
                entry["result_scope_hash"] = current_scope.scope_hash
                entry["state"] = "review_pending"
                entry["accepted_pending_commit"] = False
                self._state["domains"][domain_id] = entry
                self._persist()
                # Review is independent and can wait arbitrarily long; the
                # resolver lane must not consume one of the four active slots.
                self.release_worker(worker_type="entity_resolution", owner_ref=owner, subject_id=domain_id)
                return normalized
        raise RuntimeError("entity-resolution authority changed during submission; retry deterministically")

    def _load_result(self, domain_id: str, entry: Mapping[str, Any]) -> EntityResolutionResult:
        path = self._result_path(domain_id)
        _assert_no_symlink(path, label="entity-resolution result")
        if not path.is_file():
            raise ValueError("resolution result artifact is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("resolution result artifact is invalid") from exc
        if not isinstance(value, Mapping) or value.get("domain_id") != domain_id:
            raise ValueError("resolution result domain identity is invalid")
        result = EntityResolutionResult.from_dict(value.get("result", {}))
        if value.get("result_hash") != result.result_hash or entry.get("result_hash") != result.result_hash:
            raise ValueError("resolution result hash does not match state")
        result_scope_hash = entry.get("result_scope_hash")
        if (
            not _is_hash(result_scope_hash)
            and entry.get("state") == "ready"
            and value.get("result_scope_hash") is None
        ):
            return result
        if not _is_hash(result_scope_hash) or value.get("result_scope_hash") != result_scope_hash:
            raise ValueError("resolution result scope binding is missing or invalid")
        return result

    def _assert_result_scope_current(
        self,
        domain_id: str,
        entry: Mapping[str, Any],
    ) -> IdentityDomainScope:
        current_scope = self._scope_for_entry(domain_id, entry)
        result_scope_hash = entry.get("result_scope_hash")
        if not _is_hash(result_scope_hash) or result_scope_hash != current_scope.scope_hash:
            raise StaleIdentityScopeError(
                "stale scope binding: refresh and continue resolution"
            )
        return current_scope

    def mapping_completeness_advisory(self) -> tuple[Any, ...]:
        """Return non-gating completeness summaries for Planner and AO.

        This background view never participates in reserve, review, commit,
        resume, or lifecycle decisions.  A malformed or not-yet-submitted
        domain is reported as unavailable/pending so an experimental summary
        cannot stop otherwise valid work.
        """

        from .mapping_view import MappingCompletenessAdvisory

        values: list[MappingCompletenessAdvisory] = []
        with self._locked():
            self._refresh()
            for domain_id in sorted(self._state["domains"]):
                entry = self._domain_entry(domain_id)
                if not entry.get("result_hash"):
                    values.append(
                        MappingCompletenessAdvisory(
                            domain_id=domain_id,
                            status="pending",
                            warning="identity result has not been submitted",
                        )
                    )
                    continue
                try:
                    result = self._load_result(domain_id, entry)
                except Exception as exc:  # advisory path must not become a gate
                    values.append(
                        MappingCompletenessAdvisory(
                            domain_id=domain_id,
                            status="unavailable",
                            warning=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                mapped_sources = {
                    source_identity
                    for mapping in result.canonical_mappings
                    for source_identity in mapping.source_identities
                }
                values.append(
                    MappingCompletenessAdvisory(
                        domain_id=domain_id,
                        status=(
                            "no_mapping_found"
                            if not result.canonical_mappings
                            else "available"
                        ),
                        canonical_mapping_count=len(result.canonical_mappings),
                        mapped_source_identity_count=len(mapped_sources),
                        unresolved_record_count=len(result.unresolved),
                        exception_record_count=len(result.exceptions),
                        coverage=dict(result.coverage),
                        warning=(
                            "no deterministic mapping was accepted; continue source-local"
                            if not result.canonical_mappings
                            else None
                        ),
                    )
                )
        return tuple(values)

    def record_review(
        self,
        domain_id: str,
        verdict: str,
        reviewer_ref: str,
        *,
        owner_ref: str | None = None,
        findings: Iterable[Any] = (),
        note: str | None = None,
    ) -> Mapping[str, Any]:
        domain_id = _domain_text(domain_id)
        verdict = _text(verdict, "review verdict").lower()
        if verdict not in _REVIEW_VERDICTS:
            raise ValueError("review verdict must be accept, repair_once, or fail")
        reviewer = _text(reviewer_ref, "reviewer_ref")
        with self._locked():
            self._refresh()
            entry = self._domain_entry(domain_id)
            if owner_ref is not None and entry.get("resolution_owner") != _text(owner_ref, "owner_ref"):
                raise ValueError("review owner does not match resolution owner")
            if reviewer == entry.get("resolution_owner"):
                raise ValueError("resolution review must be independent of the owner")
            prior_review = entry.get("review")
            if entry.get("state") in {"review_pending", "ready", "failed"} and isinstance(prior_review, Mapping):
                if prior_review.get("reviewer_ref") == reviewer and prior_review.get("verdict") == verdict:
                    return dict(prior_review)
            if entry["state"] != "review_pending":
                raise ValueError("identity domain is not awaiting independent review")
            self._assert_result_scope_current(domain_id, entry)
            result = self._load_result(domain_id, entry)
            _resolution_outcome(result)
            review = {
                "reviewer_ref": reviewer,
                "verdict": verdict,
                "findings": list(_jsonable(tuple(findings or ()))),
                "note": str(note or ""),
                "result_hash": result.result_hash,
                "reviewed_at": _now(),
            }
            if verdict == "repair_once":
                if int(entry.get("repair_count", 0)) >= 1:
                    raise ValueError("identity domain permits at most one repair")
                entry["repair_count"] = 1
                entry["state"] = "repair"
                entry["accepted_pending_commit"] = False
            elif verdict == "accept":
                entry["state"] = "review_pending"
                entry["accepted_pending_commit"] = True
            else:
                entry["state"] = "failed"
                entry["accepted_pending_commit"] = False
            entry["reviewer_ref"] = reviewer
            entry["review_verdict"] = verdict
            entry["review"] = review
            self._state["domains"][domain_id] = entry
            self._persist()
            return dict(review)

    def _resolution_authority_token(self) -> str:
        """Hash state and published commit manifests for optimistic commits.

        This is deliberately a filesystem-only read.  It never loads the
        lifecycle or projector and therefore can be sampled before taking the
        entity-resolution lock without introducing a lock-order edge.
        """

        _assert_no_symlink(self.state_path, label="entity-resolution state")
        state_bytes = self.state_path.read_bytes() if self.state_path.is_file() else b""
        published: list[dict[str, Any]] = []
        if self.commits_root.exists() or self.commits_root.is_symlink():
            _assert_no_symlink(self.commits_root, label="entity-resolution commits")
            for directory in sorted(self.commits_root.iterdir(), key=lambda path: path.name):
                if directory.name.startswith("."):
                    # Atomic commit staging directories are not published
                    # authority and may be incomplete while another process
                    # holds the entity lock.
                    continue
                _assert_no_symlink(directory, label="entity-resolution commit directory")
                manifest_path = directory / _MANIFEST_FILENAME
                _assert_no_symlink(manifest_path, label="entity-resolution commit manifest")
                published.append(
                    {
                        "directory": directory.name,
                        "manifest_hash": (
                            hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                            if manifest_path.is_file()
                            else None
                        ),
                    }
                )
        return _digest(
            {
                "state_hash": hashlib.sha256(state_bytes).hexdigest(),
                "published_commits": published,
            }
        )

    def _base_model_for_validation(self) -> LivingEnterpriseModel:
        model = LivingEnterpriseModel(run_id=self.context.run_id)
        projection_succeeded = False
        try:
            from .lem_projection import LivingEnterpriseModelProjector
            projection = LivingEnterpriseModelProjector.project(self.context)
            model = LivingEnterpriseModel.from_export(projection.model.export())
            projection_succeeded = True
        except FileNotFoundError:
            # A run may reserve/resolve domains before its lifecycle is
            # materialized.  Existing committed entity-resolution domains are
            # still part of the validation base even without item projection.
            pass
        except ValueError as exc:
            # A lifecycle may be declared before its item workspaces are
            # materialized.  Resolution publication remains run-scoped and
            # does not need to wait for that independent item writer.  Other
            # projection failures are integrity errors and stay fail-closed.
            if not any(token in str(exc) for token in ("missing lifecycle item", "uncommitted lifecycle gap")):
                raise
        # The lifecycle projector replays resolution commits when it can
        # complete.  If it stopped at a lifecycle gap, replay directly from
        # the authoritative commit directories so the collision base is never
        # empty; malformed prior commits fail closed in that path as well.
        if not projection_succeeded:
            try:
                _replay_commits_with_bindings(self.context, model)
            except FileNotFoundError:
                # No entity-resolution workspace means no prior resolution
                # authority; an empty base is valid only in that case.
                pass
        return model

    @staticmethod
    def _apply_result(model: LivingEnterpriseModel, result: EntityResolutionResult) -> None:
        for item in result.ontology_items:
            existing = model.ontology.get(item.item_id)
            if existing is None:
                model.add_ontology_item(item)
            elif existing != item:
                raise ValueError(f"ontology item collision: {item.item_id}")
        for decision in result.identity_decisions:
            if decision.review_status not in {"reviewed", "accepted"}:
                raise ValueError("identity decisions must be reviewed or accepted before commit")
            existing = model.identity_decisions.get(decision.decision_id)
            if existing is None:
                model.register_identity_decision(decision)
            elif existing != decision:
                raise ValueError(f"identity decision collision: {decision.decision_id}")
        for mapping in result.canonical_mappings:
            if mapping.status != "accepted" or not mapping.source_identities:
                raise ValueError("canonical mappings must be accepted mappings with source identities")
            if mapping.decision_id not in model.identity_decisions:
                raise ValueError(f"canonical mapping has no reviewed decision: {mapping.decision_id}")
            model.add_mapping(mapping)
        for relationship in result.representation_relationships:
            payload = dict(relationship)
            relationship_id = payload.get("relationship_id") or payload.get("item_id") or payload.get("id")
            if "source_id" not in payload or "target_id" not in payload:
                raise ValueError("resolution representation relationships require source_id and target_id")
            source = payload["source_id"]
            target = payload["target_id"]
            if relationship_id is None:
                raise ValueError("resolution representation relationships require explicit relationship_id")
            model._validate_relationship_endpoints(source, target)
            existing = model.relationships.get(str(relationship_id))
            if existing is None:
                model.add_relationship(payload)
            elif existing != payload:
                raise ValueError(f"relationship collision: {relationship_id}")

    def _records_for_result(self, domain_id: str, result: EntityResolutionResult) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in result.ontology_items:
            records.append({"record_id": f"ontology:{item.item_id}", "kind": "ontology_item", "domain_id": domain_id, "payload": item.to_dict()})
        for decision in result.identity_decisions:
            records.append({"record_id": f"identity_decision:{decision.decision_id}", "kind": "identity_decision", "domain_id": domain_id, "payload": decision.to_dict()})
        for mapping in result.canonical_mappings:
            records.append({"record_id": f"canonical_mapping:{mapping.canonical_id}", "kind": "canonical_mapping", "domain_id": domain_id, "payload": mapping.to_dict()})
        for index, relationship in enumerate(result.representation_relationships):
            relationship = dict(relationship)
            if "source_id" not in relationship or "target_id" not in relationship:
                raise ValueError("resolution representation relationships require source_id and target_id")
            relationship_id = relationship.get("relationship_id") or relationship.get("item_id") or relationship.get("id") or f"relationship-{index + 1}"
            records.append({"record_id": f"relationship:{relationship_id}", "kind": "relationship", "domain_id": domain_id, "payload": _jsonable(relationship)})
        return records

    def _commit_with_precomputed_base(
        self,
        domain_id: str,
        base: LivingEnterpriseModel,
        expected_token: str,
    ) -> ResolutionCommit:
        """Publish one commit after an optimistic, lock-free projection."""

        domain_id = _domain_text(domain_id)
        with self._locked():
            self._refresh()
            if self._resolution_authority_token() != expected_token:
                raise _RetryResolutionCommit
            entry = self._domain_entry(domain_id)
            # Current submissions carry an exact material-scope binding.  A
            # ready entry may be an older upgraded artifact without that
            # additive field, but any present binding must still be checked
            # against the current scope before a commit can proceed.
            if entry.get("result_scope_hash") is not None:
                self._assert_result_scope_current(domain_id, entry)
            if entry["state"] == "ready":
                if not entry.get("commit_manifest_hash"):
                    raise ValueError("ready identity domain is missing its committed manifest")
            elif not (entry["state"] == "review_pending" and entry.get("accepted_pending_commit") is True):
                raise ValueError("identity domain must be independently accepted before commit")
            if entry["state"] != "ready":
                self._assert_result_scope_current(domain_id, entry)
            result = self._load_result(domain_id, entry)
            _resolution_outcome(result)
            # The validation base is private to this operation.  Applying the
            # candidate result in place avoids a lossy export/rehydration
            # round-trip for representation relationships that target a
            # canonical mapping rather than an ontology item.
            self._apply_result(base, result)
            records = self._records_for_result(domain_id, result)
            records_bytes = b"".join(_canonical_bytes(record) for record in records)
            records_hash = hashlib.sha256(records_bytes).hexdigest()
            commit_digest = hashlib.sha256(domain_id.encode("utf-8")).hexdigest()
            final_root = self.commits_root / commit_digest
            manifest_path = final_root / _MANIFEST_FILENAME
            if final_root.exists() or final_root.is_symlink():
                _assert_no_symlink(final_root, label="entity-resolution commit")
                if not manifest_path.is_file():
                    raise ValueError("entity-resolution commit manifest is missing")
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                _validate_manifest(existing)
                if existing.get("result_hash") != result.result_hash or existing.get("records_hash") != records_hash:
                    raise ValueError("entity-resolution commit conflicts with existing publication")
                commit = ResolutionCommit(domain_id, str(existing["manifest_hash"]), records_hash, result.result_hash, f"{_COMMITS_DIR}/{commit_digest}/{_RECORDS_FILENAME}", f"{_COMMITS_DIR}/{commit_digest}/{_MANIFEST_FILENAME}", result.source_hash, len(records))
                entry["commit_manifest_hash"] = commit.manifest_hash
                entry["state"] = "ready"
                entry["accepted_pending_commit"] = False
                self._state["domains"][domain_id] = entry
                self._persist()
                self.release_worker(worker_type="entity_resolution", owner_ref=entry.get("resolution_owner"), subject_id=domain_id, recovery=True)
                return commit
            manifest_unsigned = {
                "schema_version": SCHEMA_VERSION,
                "kind": "entity_resolution_commit",
                "domain_id": domain_id,
                "canonical_identity": entry["canonical_identity"],
                "object_type": entry["object_type"],
                "discovered_by_item_id": entry["discovered_by_item_id"],
                "result_hash": result.result_hash,
                "source_hash": result.source_hash,
                "records_path": _RECORDS_FILENAME,
                "records_hash": records_hash,
                "records_count": len(records),
                "reviewer_ref": entry.get("reviewer_ref"),
                "review_verdict": entry.get("review_verdict"),
                "coverage": _jsonable(result.coverage),
                "population": _jsonable(result.population),
                "exceptions": _jsonable(result.exceptions),
                "unresolved": _jsonable(result.unresolved),
                "evidence_refs": list(result.evidence_refs),
                "script_receipt_refs": list(result.script_receipt_refs),
                "metadata": _jsonable(result.metadata),
                "committed_at": _now(),
            }
            manifest = {**manifest_unsigned, "manifest_hash": _digest(manifest_unsigned)}
            _ensure_dir(self.commits_root, label="entity-resolution commits")
            temporary = Path(tempfile.mkdtemp(prefix=".resolution-commit-", dir=self.commits_root))
            try:
                _atomic_write_bytes(temporary / _RECORDS_FILENAME, records_bytes)
                _atomic_write_json(temporary / _MANIFEST_FILENAME, manifest)
                os.replace(temporary, final_root)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            entry["commit_manifest_hash"] = manifest["manifest_hash"]
            entry["state"] = "ready"
            entry["accepted_pending_commit"] = False
            self._state["domains"][domain_id] = entry
            self._persist()
            self.release_worker(worker_type="entity_resolution", owner_ref=entry.get("resolution_owner"), subject_id=domain_id, recovery=True)
            return ResolutionCommit(domain_id, manifest["manifest_hash"], records_hash, result.result_hash, f"{_COMMITS_DIR}/{commit_digest}/{_RECORDS_FILENAME}", f"{_COMMITS_DIR}/{commit_digest}/{_MANIFEST_FILENAME}", result.source_hash, len(records))

    def commit(self, domain_id: str) -> ResolutionCommit:
        """Optimistically project outside the entity lock, then publish.

        The projector may acquire the lifecycle lock and replay committed
        domains (which acquires the entity lock).  Keeping that phase outside
        ``_locked`` avoids the lifecycle/entity AB/BA cycle with a concurrent
        Requirement semantic refresh.  A state/commit token closes the race
        before the final atomic publication; concurrent writers recompute from
        the newer detached base and retry deterministically.
        """

        domain_id = _domain_text(domain_id)
        for _attempt in range(3):
            before_token = self._resolution_authority_token()
            base = self._base_model_for_validation()
            after_token = self._resolution_authority_token()
            if before_token != after_token:
                continue
            try:
                return self._commit_with_precomputed_base(domain_id, base, after_token)
            except _RetryResolutionCommit:
                continue
        raise RuntimeError("entity-resolution authority changed during commit; retry deterministically")

    def mark_waiting_on_resolution(
        self,
        requirement_id: str,
        domain_ids: Iterable[str],
        reason: str,
        *,
        owner_ref: str,
    ) -> Mapping[str, Any]:
        requirement = _safe_path_component(requirement_id, "requirement_id")
        domains = tuple(dict.fromkeys(_domain_text(value) for value in domain_ids))
        if not domains:
            raise ValueError("domain_ids must be non-empty")
        owner = _text(owner_ref, "owner_ref")
        with self._locked():
            self._refresh()
            for domain_id in domains:
                self._domain_entry(domain_id)
            prior = self._state["waits"].get(requirement)
            if isinstance(prior, Mapping):
                comparable = {
                    "requirement_id": requirement,
                    "domain_ids": list(domains),
                    "reason": str(reason or ""),
                    "owner_ref": owner,
                }
                if all(prior.get(key) == value for key, value in comparable.items()):
                    # Exact planner retries must not churn the wait record or
                    # the run-level state hash.
                    return dict(prior)
            self._state["leases"] = [
                value for value in self._state["leases"]
                if not (value.get("worker_type") == "analytical_owner" and value.get("owner_ref") == owner and value.get("subject_id") == requirement)
            ]
            value = {
                "requirement_id": requirement,
                "domain_ids": list(domains),
                "reason": str(reason or ""),
                "owner_ref": owner,
                "state": "waiting_on_resolution",
                "updated_at": _now(),
                "resumed_at": None,
            }
            self._state["waits"][requirement] = value
            self._persist()
            return dict(value)

    def requirement_runtime_statuses(self) -> Mapping[str, Mapping[str, Any]]:
        with self._locked():
            self._refresh()
            changed = False
            statuses: dict[str, Mapping[str, Any]] = {}
            for requirement, raw in self._state["waits"].items():
                entry = dict(raw)
                if entry.get("state") == "waiting_on_resolution":
                    ready = all(self._state["domains"].get(domain_id, {}).get("state") == "ready" for domain_id in entry.get("domain_ids", ()))
                    expected = "ready_to_resume" if ready else "waiting_on_resolution"
                    if entry.get("state") != expected:
                        entry["state"] = expected
                        entry["updated_at"] = _now()
                        self._state["waits"][requirement] = entry
                        changed = True
                statuses[requirement] = dict(entry)
            if changed:
                self._persist()
            return statuses

    def acknowledge_requirement_resume(
        self,
        requirement_id: str,
        *,
        owner_ref: str,
        reacquire: bool = True,
    ) -> WorkerLease | Mapping[str, Any]:
        requirement = _safe_path_component(requirement_id, "requirement_id")
        owner = _text(owner_ref, "owner_ref")
        statuses = self.requirement_runtime_statuses()
        current = statuses.get(requirement)
        if current is None:
            raise KeyError(requirement)
        if current.get("owner_ref") != owner:
            raise ValueError("requirement wait is owned by another analytical owner")
        if current.get("state") != "ready_to_resume":
            raise ValueError("requirement is not ready to resume")
        lease = self.claim_worker("analytical_owner", owner, requirement) if reacquire else None
        with self._locked():
            self._refresh()
            value = dict(self._state["waits"][requirement])
            value["state"] = "resumed"
            value["resumed_at"] = _now()
            self._state["waits"][requirement] = value
            self._persist()
        return lease if lease is not None else value

    def _commit_manifests(self) -> list[Mapping[str, Any]]:
        manifests: list[Mapping[str, Any]] = []
        if not self.commits_root.exists():
            raise ValueError("entity-resolution commits directory is missing")
        for directory in sorted(self.commits_root.iterdir(), key=lambda path: path.name):
            if directory.is_symlink() or not directory.is_dir():
                raise AllowedRootError(f"entity-resolution commit directory is invalid: {directory}")
            path = directory / _MANIFEST_FILENAME
            _assert_no_symlink(path, label="entity-resolution commit manifest")
            if not path.is_file():
                raise ValueError("entity-resolution commit manifest is missing")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("entity-resolution commit manifest is invalid")
            _validate_manifest(value)
            manifests.append(value)
        return manifests

    @classmethod
    def committed_bindings(cls, context: RunContext) -> tuple[Mapping[str, Any], ...]:
        workspace = cls.load(context)
        return tuple(
            {
                "domain_id": manifest["domain_id"],
                "manifest_hash": manifest["manifest_hash"],
                "records_hash": manifest["records_hash"],
                "result_hash": manifest["result_hash"],
                "source_hash": manifest.get("source_hash"),
                "records_count": manifest["records_count"],
            }
            for manifest in sorted(workspace._commit_manifests(), key=lambda value: str(value["domain_id"]))
        )


def _read_records(workspace: EntityResolutionWorkspace, manifest: Mapping[str, Any], directory: Path) -> list[Mapping[str, Any]]:
    if manifest.get("records_path") != _RECORDS_FILENAME:
        raise ValueError("entity-resolution records path is invalid")
    path = directory / _RECORDS_FILENAME
    _assert_no_symlink(path, label="entity-resolution records")
    if not path.is_file():
        raise ValueError("entity-resolution records are missing")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest.get("records_hash"):
        raise ValueError("entity-resolution records hash does not match manifest")
    rows: list[Mapping[str, Any]] = []
    for line in payload.splitlines():
        if not line:
            continue
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("entity-resolution record is invalid")
        rows.append(value)
    if len(rows) != manifest.get("records_count"):
        raise ValueError("entity-resolution records count does not match manifest")
    return rows


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if not isinstance(manifest, Mapping) or set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("entity-resolution commit manifest fields are invalid")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "entity_resolution_commit":
        raise ValueError("entity-resolution commit manifest identity is invalid")
    for name in ("result_hash", "records_hash", "manifest_hash"):
        if not _is_hash(manifest.get(name)):
            raise ValueError(f"entity-resolution commit {name} is invalid")
    source_hash = manifest.get("source_hash")
    if not _is_hash(source_hash):
        raise ValueError("entity-resolution commit source_hash is invalid")
    if manifest.get("records_path") != _RECORDS_FILENAME:
        raise ValueError("entity-resolution commit records_path is invalid")
    count = manifest.get("records_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("entity-resolution commit records_count is invalid")
    if not isinstance(manifest.get("evidence_refs"), list) or not isinstance(manifest.get("script_receipt_refs"), list):
        raise ValueError("entity-resolution commit receipt references are invalid")
    if manifest.get("manifest_hash") != _digest({key: value for key, value in manifest.items() if key != "manifest_hash"}):
        raise ValueError("entity-resolution commit manifest hash does not match content")


def _apply_record(model: LivingEnterpriseModel, record: Mapping[str, Any]) -> None:
    kind = record.get("kind")
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("entity-resolution record payload is invalid")
    if kind == "ontology_item":
        item = OntologyItem.from_dict(payload)
        existing = model.ontology.get(item.item_id)
        if existing is None:
            model.add_ontology_item(item)
        elif existing != item:
            raise ValueError(f"entity-resolution ontology collision: {item.item_id}")
    elif kind == "identity_decision":
        decision = IdentityDecision.from_dict(payload)
        if decision.review_status not in {"reviewed", "accepted"}:
            raise ValueError("entity-resolution identity decision is not reviewed")
        existing = model.identity_decisions.get(decision.decision_id)
        if existing is None:
            model.register_identity_decision(decision)
        elif existing != decision:
            raise ValueError(f"entity-resolution identity decision collision: {decision.decision_id}")
    elif kind == "canonical_mapping":
        mapping = CanonicalMapping.from_dict(payload)
        if mapping.status != "accepted" or not mapping.source_identities:
            raise ValueError("entity-resolution canonical mapping is not accepted")
        model.add_mapping(mapping)
    elif kind == "relationship":
        payload = dict(payload)
        if "source_id" not in payload or "target_id" not in payload:
            raise ValueError("entity-resolution representation relationship requires source_id and target_id")
        source = payload["source_id"]
        target = payload["target_id"]
        model._validate_relationship_endpoints(source, target)
        relationship_id = payload.get("relationship_id") or payload.get("item_id") or payload.get("id")
        if relationship_id is None:
            raise ValueError("entity-resolution relationship requires relationship_id")
        existing = model.relationships.get(str(relationship_id))
        if existing is None:
            model.add_relationship(payload)
        elif existing != dict(payload):
            raise ValueError(f"entity-resolution relationship collision: {relationship_id}")
    else:
        raise ValueError(f"unknown entity-resolution record kind: {kind}")


def _replay_commits_with_bindings(context: RunContext, model: LivingEnterpriseModel) -> tuple[Mapping[str, Any], ...]:
    workspace = EntityResolutionWorkspace.load(context)
    if model.run_id != context.run_id:
        raise ValueError("entity-resolution replay model belongs to another run")
    candidate = LivingEnterpriseModel.from_export(model.export())
    bindings: list[Mapping[str, Any]] = []
    for manifest in sorted(workspace._commit_manifests(), key=lambda value: str(value["domain_id"])):
        domain_id = _domain_text(manifest.get("domain_id"))
        directory = workspace.commits_root / hashlib.sha256(domain_id.encode("utf-8")).hexdigest()
        records = _read_records(workspace, manifest, directory)
        for record in records:
            if record.get("domain_id") != domain_id:
                raise ValueError("entity-resolution record domain identity is invalid")
            _apply_record(candidate, record)
        bindings.append({
            "domain_id": domain_id,
            "manifest_hash": manifest["manifest_hash"],
            "records_hash": manifest["records_hash"],
            "result_hash": manifest["result_hash"],
            "source_hash": manifest.get("source_hash"),
            "records_count": manifest["records_count"],
        })
    model.__dict__.clear()
    model.__dict__.update(candidate.__dict__)
    return tuple(bindings)


def replay_ready_commits(context: RunContext, model: LivingEnterpriseModel | None = None) -> LivingEnterpriseModel:
    """Validate and deterministically replay every committed domain.

    All records are applied to a temporary model first; a malformed later
    domain therefore cannot leave a partially mutated caller model.
    """

    if not isinstance(context, RunContext):
        raise TypeError("entity-resolution replay requires a RunContext")
    target = model if model is not None else LivingEnterpriseModel(run_id=context.run_id)
    if not isinstance(target, LivingEnterpriseModel):
        raise TypeError("entity-resolution replay model must be a LivingEnterpriseModel")
    try:
        _replay_commits_with_bindings(context, target)
    except FileNotFoundError:
        # No run-level workspace means no resolution authority, not an error.
        return target
    return target


__all__ = [
    "EntityResolutionResult",
    "IdentityDomainScope",
    "EntityResolutionWorkspace",
    "IdentityDomainRequest",
    "IdentityDomainReservation",
    "ResolutionCapacity",
    "ResolutionCommit",
    "StaleIdentityScopeError",
    "WorkerLease",
    "replay_ready_commits",
]
