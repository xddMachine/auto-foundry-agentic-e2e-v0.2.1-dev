"""Mechanical integration of an accepted analytical result.

The integration agent supplies typed records and explicit evidence.  This
module persists those records, verifies their local identity, and applies them
through the public prepared-registry and Living Enterprise Model APIs.  It
never parses answer prose, recalculates metrics, launches a model, or infers
semantic relationships.
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
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - POSIX is used on supported hosts
    import fcntl
except ImportError:  # pragma: no cover - defensive fallback
    fcntl = None  # type: ignore[assignment]

from .contracts import KnowledgeDelta, LEMRef, OntologyItem, PreparedAssetDescriptor
from .durable import _atomic_write_bytes, _atomic_write_json, _json_bytes, _sha256_bytes
from .enterprise_model import LivingEnterpriseModel
from .prepared import PreparedAssetRegistry
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
        "dashboard_fact",
    }
)
_SESSION_STATES = frozenset({"open", "committed", "technical_failure"})
_SHA256_HEX = frozenset("0123456789abcdef")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _SHA256_HEX for char in value)


def _copy_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(copy.deepcopy(dict(value)))


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
    return result


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
            acceptance_envelope=_copy_mapping(envelope),
            envelope_hash=envelope_hash,
            manifest=_copy_mapping(manifest),
            manifest_hash=str(manifest["manifest_hash"]),
        )


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
        lem: LivingEnterpriseModel,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        bundle: AcceptedAnalysisBundle,
        session_state: Mapping[str, Any],
        records: Sequence[IntegrationRecord],
    ) -> None:
        self.context = context
        self.item_workspace = item_workspace
        self.lem = lem
        self.prepared_registry = prepared_registry
        self.owner_id = _safe_component(owner_id, "owner_id")
        self.bundle = bundle
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

    @classmethod
    def create(
        cls,
        context: RunContext,
        item_workspace: Any,
        lem: LivingEnterpriseModel,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
    ) -> "IntegrationSession":
        with cls._session_lock(item_workspace):
            return cls._create_unlocked(context, item_workspace, lem, prepared_registry, owner_id)

    @classmethod
    def _create_unlocked(
        cls,
        context: RunContext,
        item_workspace: Any,
        lem: LivingEnterpriseModel,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
    ) -> "IntegrationSession":
        if not isinstance(context, RunContext):
            raise TypeError("IntegrationSession requires a RunContext")
        if getattr(item_workspace, "context", None) is not context:
            raise ValueError("item workspace must use the same RunContext")
        if not isinstance(lem, LivingEnterpriseModel):
            raise TypeError("IntegrationSession requires LivingEnterpriseModel")
        if not isinstance(prepared_registry, PreparedAssetRegistry) or prepared_registry.context is not context:
            raise ValueError("prepared registry must use the same RunContext")
        owner_id = _safe_component(owner_id, "owner_id")
        bundle = AcceptedAnalysisBundle.load(item_workspace)
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
            session = cls._load_existing(context, item_workspace, lem, prepared_registry, owner_id, bundle)
            if session.status == "committed":
                return session
            return session
        committed = cls._committed_manifest(item_workspace)
        if committed is not None:
            if integration_state not in {"pending", "integrated"}:
                raise ValueError("committed integration conflicts with item state")
            session = cls._load_committed(context, item_workspace, lem, prepared_registry, owner_id, bundle, committed)
            return session
        if integration_state != "pending":
            raise ValueError("IntegrationSession requires pending item integration state")
        staging = staging_root
        cls._ensure_safe_dir(staging)
        session_id = "IS-" + _sha256_value({"item_id": bundle.item_id, "owner_id": owner_id, "content_hash": bundle.content_hash})[:24]
        now = _now()
        state = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": session_id,
            "item_id": bundle.item_id,
            "owner_id": owner_id,
            "status": "open",
            "accepted_content_hash": bundle.content_hash,
            "accepted_manifest_hash": bundle.manifest_hash,
            "records_count": 0,
            "records_hash": _sha256_bytes(b""),
            "created_at": now,
            "updated_at": now,
        }
        state["state_hash"] = _sha256_value(state)
        cls._write_staging_snapshot(staging, state, ())
        return cls(context, item_workspace, lem, prepared_registry, owner_id, bundle, state, ())

    @classmethod
    def load(
        cls,
        context: RunContext,
        item_workspace: Any,
        lem: LivingEnterpriseModel,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
    ) -> "IntegrationSession":
        """Reload and validate the run-local staging or committed session."""
        with cls._session_lock(item_workspace):
            return cls._load_unlocked(context, item_workspace, lem, prepared_registry, owner_id)

    @classmethod
    def _load_unlocked(
        cls,
        context: RunContext,
        item_workspace: Any,
        lem: LivingEnterpriseModel,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
    ) -> "IntegrationSession":
        if not isinstance(context, RunContext):
            raise TypeError("IntegrationSession requires a RunContext")
        if getattr(item_workspace, "context", None) is not context:
            raise ValueError("item workspace must use the same RunContext")
        if not isinstance(lem, LivingEnterpriseModel):
            raise TypeError("IntegrationSession requires LivingEnterpriseModel")
        if not isinstance(prepared_registry, PreparedAssetRegistry) or prepared_registry.context is not context:
            raise ValueError("prepared registry must use the same RunContext")
        bundle = AcceptedAnalysisBundle.load(item_workspace)
        owner_id = _safe_component(owner_id, "owner_id")
        staging = cls._staging_root(item_workspace)
        if (staging / _SNAPSHOT_FILENAME).exists() or (staging / _SESSION_FILENAME).exists():
            return cls._load_existing(context, item_workspace, lem, prepared_registry, owner_id, bundle)
        committed = cls._committed_manifest(item_workspace)
        if committed is not None:
            return cls._load_committed(context, item_workspace, lem, prepared_registry, owner_id, bundle, committed)
        return cls._load_existing(context, item_workspace, lem, prepared_registry, owner_id, bundle)

    @classmethod
    def _load_existing(
        cls,
        context: RunContext,
        item_workspace: Any,
        lem: LivingEnterpriseModel,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        bundle: AcceptedAnalysisBundle,
    ) -> "IntegrationSession":
        staging = cls._staging_root(item_workspace)
        state, records = cls._read_staging_snapshot(staging, bundle)
        if state["owner_id"] != owner_id:
            raise ValueError("integration staging is owned by another owner")
        return cls(context, item_workspace, lem, prepared_registry, owner_id, bundle, state, records)

    @classmethod
    def _load_committed(
        cls,
        context: RunContext,
        item_workspace: Any,
        lem: LivingEnterpriseModel,
        prepared_registry: PreparedAssetRegistry,
        owner_id: str,
        bundle: AcceptedAnalysisBundle,
        manifest: Mapping[str, Any],
    ) -> "IntegrationSession":
        if manifest.get("owner_id") != owner_id:
            raise ValueError("committed integration is owned by another owner")
        committed = cls._integration_root(item_workspace) / _COMMITTED_DIR
        records = cls._read_records(committed / _RECORDS_FILENAME, manifest, bundle)
        state = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": manifest["session_id"],
            "item_id": bundle.item_id,
            "owner_id": owner_id,
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
                "status": "committed",
                "accepted_content_hash": bundle.content_hash,
                "accepted_manifest_hash": bundle.manifest_hash,
                "records_count": len(records),
                "records_hash": manifest["records_hash"],
                "created_at": manifest["created_at"],
                "updated_at": manifest["committed_at"],
            }),
        }
        session = cls(context, item_workspace, lem, prepared_registry, owner_id, bundle, state, records)
        session._preflight_all()
        session._apply_records()
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

    @staticmethod
    def _ensure_safe_dir(path: Path) -> None:
        _assert_no_symlink(path, label="integration directory")
        path.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink(path, label="integration directory")

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
            return
        state, records = self._read_staging_snapshot(staging, self.bundle)
        if state.get("session_id") != self.session_id or state.get("owner_id") != self.owner_id:
            raise ValueError("integration staging identity changed")
        self._state = state
        self._records = records
        self._by_id = {record.record_id: record for record in records}

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
            "status",
            "accepted_content_hash",
            "accepted_manifest_hash",
            "records_count",
            "records_hash",
            "created_at",
            "updated_at",
            "state_hash",
        }
        if not isinstance(state, Mapping) or set(state) != expected:
            raise ValueError("integration session fields are invalid")
        if state["schema_version"] != _SCHEMA_VERSION or state["item_id"] != bundle.item_id:
            raise ValueError("integration session identity is invalid")
        _validate_record_id(state.get("session_id"))
        if not isinstance(state.get("owner_id"), str):
            raise ValueError("integration session owner_id is invalid")
        _safe_component(state.get("owner_id"), "owner_id")
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

    def _require_open(self) -> None:
        if self.status != "open":
            raise ValueError("integration session is terminal")

    @staticmethod
    def _payload(value: Any, label: str) -> dict[str, Any]:
        if isinstance(value, Mapping):
            payload = _jsonable(dict(value))
        elif isinstance(value, str):
            payload = {label: value}
        else:
            raise TypeError(f"{label} must be a mapping or string")
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

    def _evidence(self, evidence_refs: Any, *, required: bool = True) -> tuple[tuple[str, ...], dict[str, str]]:
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
                expected = self._evidence_expected_hash(relative)
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

    def _evidence_expected_hash(self, relative: str) -> str:
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
        raise ValueError(f"evidence reference is not bound by accepted manifest: {relative}")

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
        if kind not in _RECORD_KINDS:
            raise ValueError("unsupported integration record kind")
        normalized_payload = self._payload(payload, kind)
        normalized_scope = self._scope(scope, normalized_payload)
        refs, hashes = self._evidence(evidence_refs, required=kind != "prepared_asset")
        body = {
            "kind": kind,
            "item_id": self.item_id,
            "accepted_content_hash": self.bundle.content_hash,
            "scope": normalized_scope,
            "evidence_refs": list(refs),
            "evidence_hashes": hashes,
            "payload": normalized_payload,
        }
        generated_id = f"{kind}-{_sha256_value(body)[:24]}"
        normalized_id = str(record_id).strip() if record_id is not None else generated_id
        if not normalized_id:
            raise ValueError("record_id must be non-empty")
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
            return normalized_id
        self._records.append(record)
        self._by_id[normalized_id] = record
        self._persist_state(self._state)
        return normalized_id

    def add_claim(self, claim: Mapping[str, Any] | str, *, scope: str | None = None, evidence_refs: Any = (), claim_id: str | None = None, **values: Any) -> str:
        payload = self._payload(claim, "claim")
        payload.update(_jsonable(values))
        return self._stage("claim", payload, scope=scope, evidence_refs=evidence_refs, record_id=claim_id)

    def add_metric(self, metric: Mapping[str, Any] | None = None, *, scope: str | None = None, evidence_refs: Any = (), metric_id: str | None = None, **values: Any) -> str:
        payload = self._payload(metric or values, "metric")
        payload.update(_jsonable(values))
        self._preview_metric(payload, self.lem.run_id)
        return self._stage("metric", payload, scope=scope, evidence_refs=evidence_refs, record_id=metric_id)

    def add_limitation(self, limitation: Mapping[str, Any] | str, *, scope: str | None = None, evidence_refs: Any = (), limitation_id: str | None = None, **values: Any) -> str:
        payload = self._payload(limitation, "limitation")
        payload.update(_jsonable(values))
        return self._stage("limitation", payload, scope=scope, evidence_refs=evidence_refs, record_id=limitation_id)

    def link_evidence(self, record_id: str, evidence_refs: Any, *, scope: str | None = None, link_id: str | None = None) -> str:
        target = str(record_id).strip()
        if target not in self._by_id:
            raise KeyError(f"unknown integration record: {target}")
        refs, _hashes = self._evidence(evidence_refs, required=True)
        payload = {"target_record_id": target, "evidence_refs": list(refs)}
        return self._stage("evidence_link", payload, scope=scope or self._by_id[target].scope, evidence_refs=refs, record_id=link_id)

    def register_prepared_asset(self, descriptor: PreparedAssetDescriptor | Mapping[str, Any], *, evidence_refs: Any = (), asset_record_id: str | None = None) -> str:
        value = descriptor if isinstance(descriptor, PreparedAssetDescriptor) else PreparedAssetDescriptor.from_dict(descriptor)
        # Verify descriptor/file/hash/scope and same-ID collisions without
        # publishing registry state during staging.
        self.prepared_registry.preflight_register(value)
        payload = value.to_dict()
        return self._stage("prepared_asset", payload, scope=value.scope, evidence_refs=evidence_refs, record_id=asset_record_id)

    def add_ontology_item(self, item: OntologyItem | Mapping[str, Any], *, scope: str | None = None, evidence_refs: Any = (), ontology_record_id: str | None = None) -> str:
        value = item.to_dict() if isinstance(item, OntologyItem) else self._payload(item, "ontology_item")
        normalized_scope = self._scope(scope, value)
        if not value.get("scope"):
            value["scope"] = normalized_scope
        value = OntologyItem.from_dict(value).to_dict()
        return self._stage("ontology_item", value, scope=normalized_scope, evidence_refs=evidence_refs, record_id=ontology_record_id)

    def add_relationship(self, relationship: Mapping[str, Any], *, scope: str | None = None, evidence_refs: Any = (), relationship_record_id: str | None = None) -> str:
        value = self._payload(relationship, "relationship")
        if not value.get("relationship_id") and not value.get("item_id") and not value.get("id"):
            raise ValueError("relationship requires relationship_id")
        if value.get("source_id", value.get("source_item_id")) is None or value.get("target_id", value.get("target_item_id")) is None:
            raise ValueError("relationship requires explicit source_id and target_id")
        normalized_scope = self._scope(scope, value)
        if not value.get("scope"):
            value["scope"] = normalized_scope
        self._preview_relationship(value, self.lem.run_id)
        return self._stage("relationship", value, scope=normalized_scope, evidence_refs=evidence_refs, record_id=relationship_record_id)

    def add_dashboard_fact(self, fact: Mapping[str, Any] | str, *, scope: str | None = None, evidence_refs: Any = (), fact_id: str | None = None, **values: Any) -> str:
        payload = self._payload(fact, "dashboard_fact")
        payload.update(_jsonable(values))
        return self._stage("dashboard_fact", payload, scope=scope, evidence_refs=evidence_refs, record_id=fact_id)

    def correct_record(self, record_id: str, payload: Mapping[str, Any], *, evidence_refs: Any = None, scope: str | None = None) -> str:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._correct_record_unlocked(record_id, payload, evidence_refs=evidence_refs, scope=scope)

    def _correct_record_unlocked(self, record_id: str, payload: Mapping[str, Any], *, evidence_refs: Any = None, scope: str | None = None) -> str:
        """Replace one same-session record while retaining deterministic identity."""
        self._require_open()
        target = str(record_id).strip()
        existing = self._by_id.get(target)
        if existing is None:
            raise KeyError(target)
        if (self.staging_root / _INTENT_FILENAME).exists() or (self.staging_root / _INTENT_FILENAME).is_symlink():
            raise ValueError("integration commit intent exists; corrections are closed")
        normalized_payload = self._payload(payload, existing.kind)
        if existing.kind == "prepared_asset":
            self.prepared_registry.preflight_register(PreparedAssetDescriptor.from_dict(normalized_payload))
        elif existing.kind == "ontology_item":
            normalized_payload = OntologyItem.from_dict(normalized_payload).to_dict()
        elif existing.kind == "metric":
            self._preview_metric(normalized_payload, self.lem.run_id)
        elif existing.kind == "relationship":
            self._preview_relationship(normalized_payload, self.lem.run_id)
        normalized_scope = self._scope(scope if scope is not None else existing.scope, normalized_payload)
        refs = existing.evidence_refs if evidence_refs is None else evidence_refs
        normalized_refs, hashes = self._evidence(refs, required=existing.kind != "prepared_asset")
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
            return target
        index = next(index for index, record in enumerate(self._records) if record.record_id == target)
        self._records[index] = replacement
        self._by_id[target] = replacement
        try:
            self._persist_state(self._state)
        except Exception:
            self._records[index] = existing
            self._by_id[target] = existing
            raise
        return target

    def _validate_relationship_refs(self, record: IntegrationRecord, *, known: set[str] | None = None) -> None:
        payload = record.payload
        source = payload.get("source_id", payload.get("source_item_id"))
        target = payload.get("target_id", payload.get("target_item_id"))
        if source is None or target is None:
            raise ValueError("relationship requires explicit source_id and target_id")
        if known is None:
            known = set(self.lem.ontology)
        if str(source) not in known or str(target) not in known:
            raise ValueError("relationship references unknown ontology item")

    def validate(self) -> IntegrationValidation:
        counts = {kind: 0 for kind in sorted(_RECORD_KINDS)}
        omissions: list[str] = []
        errors: list[str] = []
        seen: set[str] = set()
        # Relationship references are intentionally order-sensitive.  The
        # known set grows as staged ontology/metric/relationship records are
        # encountered; a forward-only or unknown reference fails closed.
        known_ontology_ids = set(self.lem.ontology)
        for record in self._records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
            if record.record_id in seen:
                errors.append(f"duplicate record_id: {record.record_id}")
            seen.add(record.record_id)
            try:
                IntegrationRecord.from_dict(record.to_dict())
                self._scope(record.scope)
                if record.kind == "ontology_item":
                    item = OntologyItem.from_dict(record.payload)
                    known_ontology_ids.add(item.item_id)
                elif record.kind == "metric":
                    item = self._preview_metric(record.payload, self.lem.run_id)
                    known_ontology_ids.add(item.item_id)
                if record.kind == "relationship":
                    self._validate_relationship_refs(record, known=known_ontology_ids)
                    _relationship_id, _relationship_payload, relationship_item = self._preview_relationship(record.payload, self.lem.run_id)
                    known_ontology_ids.add(relationship_item.item_id)
                if record.kind == "prepared_asset":
                    PreparedAssetDescriptor.from_dict(record.payload)
            except Exception as exc:
                errors.append(f"{record.record_id}: {exc}")
        return IntegrationValidation(not errors, counts, tuple(omissions), tuple(errors))

    @staticmethod
    def _preview_metric(payload: Mapping[str, Any], run_id: str) -> OntologyItem:
        """Build the exact typed metric through the public LEM API.

        A throw-away model keeps preflight non-mutating while avoiding any
        dependency on LivingEnterpriseModel's private normalization helpers.
        """

        preview = LivingEnterpriseModel(run_id=run_id)
        return preview.add_metric(payload)

    @staticmethod
    def _preview_relationship(payload: Mapping[str, Any], run_id: str) -> tuple[str, dict[str, Any], OntologyItem]:
        preview = LivingEnterpriseModel(run_id=run_id)
        ontology_item = preview.add_relationship(payload)
        relationship_id = str(payload.get("relationship_id") or payload.get("item_id") or payload.get("id"))
        return relationship_id, copy.deepcopy(preview.relationships[relationship_id]), ontology_item

    def _preflight_registry(self) -> None:
        for record in self._records:
            if record.kind == "prepared_asset":
                descriptor = PreparedAssetDescriptor.from_dict(record.payload)
                # Public, lock-protected and non-mutating.  This must happen
                # before commit intent or any registry/LEM mutation.
                self.prepared_registry.preflight_register(descriptor)

    def _preflight_lem(self) -> None:
        simulated_ontology = dict(self.lem.ontology)
        simulated_assets = dict(self.lem.prepared_assets)
        simulated_relationships = copy.deepcopy(self.lem.relationships)
        simulated_knowledge = copy.deepcopy(self.lem.knowledge)
        for record in self._records:
            payload = dict(record.payload)
            if record.kind == "ontology_item":
                item = OntologyItem.from_dict(payload)
                existing = simulated_ontology.get(item.item_id)
                if existing is not None and existing != item:
                    raise ValueError(f"ontology item collision: {item.item_id}")
                if existing is None:
                    simulated_ontology[item.item_id] = item
            elif record.kind == "metric":
                item = self._preview_metric(payload, self.lem.run_id)
                existing = simulated_ontology.get(item.item_id)
                if existing is not None and existing != item:
                    raise ValueError(f"metric collision: {item.item_id}")
                if existing is None:
                    simulated_ontology[item.item_id] = item
            elif record.kind == "prepared_asset":
                descriptor = PreparedAssetDescriptor.from_dict(payload)
                existing = simulated_assets.get(descriptor.prepared_asset_id)
                if existing is not None and existing != descriptor:
                    raise ValueError(f"prepared asset collision: {descriptor.prepared_asset_id}")
                if existing is None:
                    simulated_assets[descriptor.prepared_asset_id] = descriptor
            elif record.kind == "relationship":
                self._validate_relationship_refs(record, known=set(simulated_ontology))
                relationship_id, expected_relationship, ontology_item = self._preview_relationship(payload, self.lem.run_id)
                existing_relationship = simulated_relationships.get(relationship_id)
                if existing_relationship is not None and existing_relationship != expected_relationship:
                    raise ValueError(f"relationship collision: {relationship_id}")
                if existing_relationship is None:
                    # add_relationship also creates a typed ontology item.
                    existing_ontology = simulated_ontology.get(ontology_item.item_id)
                    if existing_ontology is not None:
                        raise ValueError(f"relationship ontology collision: {ontology_item.item_id}")
                    simulated_relationships[relationship_id] = expected_relationship
                    simulated_ontology[ontology_item.item_id] = ontology_item
            elif record.kind == "limitation":
                existing = simulated_knowledge.get(record.record_id)
                expected_payload = payload
                if existing is not None and (
                    existing.get("operation") != "record_limitation" or existing.get("payload") != expected_payload
                ):
                    raise ValueError(f"knowledge delta collision: {record.record_id}")
                if existing is None:
                    simulated_knowledge[record.record_id] = {
                        "operation": "record_limitation",
                        "payload": expected_payload,
                    }

    def _preflight_all(self) -> None:
        validation = self.validate()
        if not validation.valid:
            raise ValueError(f"integration validation failed: {list(validation.errors)}")
        self._preflight_registry()
        self._preflight_lem()

    def _apply_records(self) -> None:
        for record in self._records:
            if record.kind == "prepared_asset":
                descriptor = PreparedAssetDescriptor.from_dict(record.payload)
                self.prepared_registry.register_accepted(descriptor)
            self._apply_lem_record(record)

    def _apply_lem_record(self, record: IntegrationRecord) -> None:
        payload = dict(record.payload)
        if record.kind == "ontology_item":
            item = OntologyItem.from_dict(payload)
            existing = self.lem.ontology.get(item.item_id)
            if existing is None:
                self.lem.add_ontology_item(item)
            elif existing != item:
                raise ValueError(f"ontology item collision: {item.item_id}")
        elif record.kind == "metric":
            item = self._preview_metric(payload, self.lem.run_id)
            existing = self.lem.ontology.get(item.item_id)
            if existing is None:
                self.lem.add_metric(payload)
            elif existing != item:
                raise ValueError(f"metric collision: {item.item_id}")
        elif record.kind == "relationship":
            self._validate_relationship_refs(record)
            relationship_id, expected_relationship, _ontology_item = self._preview_relationship(payload, self.lem.run_id)
            existing = self.lem.relationships.get(relationship_id)
            if existing is None:
                self.lem.add_relationship(payload)
            elif existing != expected_relationship:
                raise ValueError(f"relationship collision: {relationship_id}")
        elif record.kind == "prepared_asset":
            descriptor = PreparedAssetDescriptor.from_dict(payload)
            existing = self.lem.prepared_assets.get(descriptor.prepared_asset_id)
            if existing is None:
                self.lem.register_prepared_asset(descriptor)
            elif existing != descriptor:
                raise ValueError(f"prepared asset collision: {descriptor.prepared_asset_id}")
        elif record.kind == "limitation":
            delta = KnowledgeDelta(
                delta_id=record.record_id,
                operation="record_limitation",
                payload=payload,
                evidence_refs=record.evidence_refs,
                accepted=True,
            )
            existing = self.lem.knowledge.get(record.record_id)
            if existing is None:
                self.lem.apply_delta(delta)
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
            "schema_version", "session_id", "item_id", "owner_id", "status",
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

    @classmethod
    def _read_intent(
        cls,
        path: Path,
        bundle: AcceptedAnalysisBundle,
        *,
        session_id: str,
        owner_id: str,
    ) -> Mapping[str, Any] | None:
        if not (path.exists() or path.is_symlink()):
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("integration commit intent is invalid")
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("integration commit intent is invalid") from exc
        expected = {"schema_version", "session_id", "item_id", "owner_id", "manifest", "intent_hash"}
        if not isinstance(intent, Mapping) or set(intent) != expected or intent.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("integration commit intent fields are invalid")
        if intent.get("session_id") != session_id or intent.get("item_id") != bundle.item_id or intent.get("owner_id") != owner_id:
            raise ValueError("integration commit intent identity is invalid")
        unsigned = {key: value for key, value in intent.items() if key != "intent_hash"}
        if not _is_sha256(intent.get("intent_hash")) or intent["intent_hash"] != _sha256_value(unsigned):
            raise ValueError("integration commit intent hash does not match content")
        manifest = intent.get("manifest")
        if not isinstance(manifest, Mapping):
            raise ValueError("integration commit intent manifest is invalid")
        expected_manifest = {
            "schema_version", "session_id", "item_id", "owner_id", "status",
            "accepted_content_hash", "accepted_manifest_hash", "records_path",
            "records_hash", "records_count", "counts", "created_at", "committed_at",
            "manifest_hash",
        }
        if set(manifest) != expected_manifest or manifest.get("session_id") != session_id or manifest.get("owner_id") != owner_id:
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

    def _write_committed(self, manifest: Mapping[str, Any], records_bytes: bytes) -> None:
        committed = self.committed_root
        if committed.exists() or committed.is_symlink():
            existing = self._committed_manifest(self.item_workspace)
            if existing is None or existing.get("manifest_hash") != manifest.get("manifest_hash"):
                raise ValueError("committed integration collision")
            return
        integration_root = self._integration_root(self.item_workspace)
        self._ensure_safe_dir(integration_root)
        temporary = Path(tempfile.mkdtemp(prefix=".committed.tmp-", dir=integration_root))
        try:
            _atomic_write_bytes(temporary / _RECORDS_FILENAME, records_bytes)
            _atomic_write_json(temporary / _MANIFEST_FILENAME, manifest)
            os.replace(temporary, committed)
            _fsync_directory(integration_root)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def commit(self) -> Mapping[str, Any]:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._commit_unlocked()

    def _commit_unlocked(self) -> Mapping[str, Any]:
        """Validate, apply typed records, publish, and mark integration committed."""

        if self.status == "committed":
            manifest = self._committed_manifest(self.item_workspace)
            if manifest is None:
                raise ValueError("committed integration manifest is missing")
            existing_records = self._read_records(self.committed_root / _RECORDS_FILENAME, manifest, self.bundle)
            if tuple(existing_records) != tuple(self._records):
                raise ValueError("committed integration records differ from staging")
            self._preflight_all()
            self._apply_records()
            self._finish_committed(manifest)
            return dict(manifest)
        self._require_open()
        existing_manifest = self._committed_manifest(self.item_workspace)
        if existing_manifest is not None:
            if (
                existing_manifest.get("session_id") != self.session_id
                or existing_manifest.get("owner_id") != self.owner_id
                or existing_manifest.get("accepted_content_hash") != self.bundle.content_hash
            ):
                raise ValueError("committed integration identity collision")
            existing_records = self._read_records(
                self.committed_root / _RECORDS_FILENAME,
                existing_manifest,
                self.bundle,
            )
            if tuple(existing_records) != tuple(self._records):
                raise ValueError("committed integration records differ from staging")
            self._preflight_all()
            self._apply_records()
            self._finish_committed(existing_manifest)
            return dict(existing_manifest)
        if self.item_workspace.integration_state != "pending":
            raise ValueError("item integration is no longer pending")
        self._preflight_all()
        validation = self.validate()
        records_bytes = self._records_bytes()
        records_hash = _sha256_bytes(records_bytes)
        intent = self._read_intent(
            self.staging_root / _INTENT_FILENAME,
            self.bundle,
            session_id=self.session_id,
            owner_id=self.owner_id,
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
                "manifest": manifest,
            }
            intent["intent_hash"] = _sha256_value(intent)
        # The durable intent is written before registry/LEM mutations.  A
        # crash after a partial apply therefore retries this exact plan.
        _atomic_write_json(self.staging_root / _INTENT_FILENAME, intent)
        self._apply_records()
        self._write_committed(manifest, records_bytes)
        self._finish_committed(manifest)
        return dict(manifest)

    def mark_technical_failure(self, reason: str) -> Mapping[str, Any]:
        with self._session_lock(self.item_workspace):
            self._refresh_authoritative()
            return self._mark_technical_failure_unlocked(reason)

    def _mark_technical_failure_unlocked(self, reason: str) -> Mapping[str, Any]:
        """Terminalize only an explicitly unrecoverable integration failure."""

        reason = str(reason).strip()
        if not reason:
            raise ValueError("technical failure reason is required")
        failure_root = self._integration_root(self.item_workspace) / _TECHNICAL_FAILURE_DIR
        failure_path = failure_root / _MANIFEST_FILENAME
        manifest: dict[str, Any] | None = None
        if failure_root.exists() or failure_root.is_symlink():
            self._ensure_safe_dir(failure_root)
            if not failure_path.is_file() or failure_path.is_symlink():
                raise ValueError("technical failure manifest is missing")
            try:
                loaded = json.loads(failure_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("technical failure manifest is invalid") from exc
            expected = {
                "schema_version", "session_id", "item_id", "owner_id", "status",
                "accepted_content_hash", "reason", "created_at", "manifest_hash",
            }
            if not isinstance(loaded, Mapping) or set(loaded) != expected or loaded.get("schema_version") != _SCHEMA_VERSION or loaded.get("status") != "technical_failure":
                raise ValueError("technical failure manifest fields are invalid")
            if loaded.get("session_id") != self.session_id or loaded.get("item_id") != self.item_id or loaded.get("owner_id") != self.owner_id or loaded.get("accepted_content_hash") != self.bundle.content_hash:
                raise ValueError("technical failure manifest identity is invalid")
            unsigned = {key: value for key, value in loaded.items() if key != "manifest_hash"}
            if not _is_sha256(loaded.get("manifest_hash")) or loaded.get("manifest_hash") != _sha256_value(unsigned):
                raise ValueError("technical failure manifest hash does not match content")
            manifest = dict(loaded)
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
            manifest["manifest_hash"] = _sha256_value(manifest)
            _atomic_write_json(failure_path, manifest)
        elif reason and manifest.get("reason") != reason:
            # A retry must converge on the first durable reason, never rewrite
            # a terminal failure with a competing explanation.
            raise ValueError("technical failure reason differs from durable manifest")
        if self.item_workspace.integration_state == "pending":
            self.item_workspace.mark_integration_failed(manifest["manifest_hash"], "integration/technical_failure/manifest.json")
        elif self.item_workspace.integration_state == "technical_failure":
            if self.item_workspace.integration_manifest_hash != manifest["manifest_hash"]:
                raise ValueError("item integration state does not match technical failure manifest")
        else:
            raise ValueError("item integration state does not permit technical failure")
        state = dict(self._state)
        state["status"] = "technical_failure"
        state["updated_at"] = manifest.get("created_at", _now())
        self._persist_state(state)
        return dict(manifest)

    technical_failure = mark_technical_failure


__all__ = ["AcceptedAnalysisBundle", "IntegrationRecord", "IntegrationSession", "IntegrationValidation"]
