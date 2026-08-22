"""Run-local content-addressed semantic snapshots and exact selections.

The store deliberately lives below one run root.  A snapshot is published once
as an immutable manifest whose layer/index metadata points at immutable,
content-addressed blobs; item contexts retain only a small manifest reference.
Layer bytes are opened only by the operation that needs that layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Mapping, TYPE_CHECKING

from .contracts import DataAssetRef

try:  # pragma: no cover - POSIX hosts provide advisory flock
    import fcntl
except ImportError:  # pragma: no cover - defensive non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:  # pragma: no cover
    from .workspace import RunContext


SNAPSHOT_MANIFEST_SCHEMA = "auto_foundry.semantic_snapshot.v3"
SNAPSHOT_REF_SCHEMA = "auto_foundry.semantic_snapshot_ref.v1"
SELECTION_SCHEMA = "auto_foundry.semantic_selection.v1"
CONTEXT_PAYLOAD_SCHEMA = "auto_foundry.analysis_context_payload.v1"
STORE_ROOT = Path("semantic_store")
_LAYER_NAMES = (
    "ontology",
    "relationships",
    "identity_decisions",
    "canonical_mappings",
    "prepared_assets",
)
_LAYER_ID_FIELDS = {
    "ontology": "item_id",
    "relationships": "relationship_id",
    "identity_decisions": "decision_id",
    "canonical_mappings": "canonical_id",
    "prepared_assets": "prepared_asset_id",
}
_SELECTION_SET_NAMES = {
    "ontology": "ontology_ids",
    "relationships": "relationship_ids",
    "identity_decisions": "identity_decision_ids",
    "canonical_mappings": "mapping_ids",
    "prepared_assets": "prepared_asset_ids",
}


def _canonical_context_payload(value: Any) -> Any:
    """Canonicalize a caller bundle to the value ``BoundAnalysisContext`` exposes.

    ``BoundAnalysisContext`` freezes mappings with string keys and converts all
    sequence/set values to tuples.  Paths are JSONable strings.  The store
    persists that same plain JSON-shaped value; it does not invent a second
    type-restoration protocol for caller objects.
    """

    if isinstance(value, Mapping):
        # Match the class's established ``_jsonable`` mapping behavior: keys
        # are stringified and a later colliding key wins.
        return {str(key): _canonical_context_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_context_payload(item) for item in value]
    if isinstance(value, (set, frozenset)):
        encoded = [_canonical_context_payload(item) for item in value]
        encoded.sort(key=_canonical)
        return encoded
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, DataAssetRef):
        return value.to_dict()
    if value is None or isinstance(value, (str, int, float, bool)):
        # ``_canonical`` below rejects non-finite floats.
        return value
    raise ValueError("caller context payload must contain JSON-shaped values")


def canonical_context_payload(value: Any) -> Any:
    """Return the v3 caller-payload value used by create/load contexts."""

    return _canonical_context_payload(value)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_json(value: Any) -> str:
    return _hash_bytes(_canonical(value))


def _json_bytes(value: Any) -> bytes:
    return _canonical(value) + b"\n"


def _hex_hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash")
    return value


def _safe_path(path: Path, *, root: Path, label: str) -> Path:
    """Resolve a run-local path while rejecting symlink components."""

    root = root.resolve(strict=False)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes run root") from exc
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} escapes run root")
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"{label} cannot use symlink components")
    return path


def _regular(path: Path, *, root: Path, label: str) -> Path:
    _safe_path(path, root=root, label=label)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    return path


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            descriptor = None
        if descriptor is not None:
            try:
                try:
                    os.fsync(descriptor)
                except OSError:
                    pass
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class SemanticSnapshotRef:
    """Small immutable reference persisted in an analysis context."""

    snapshot_hash: str
    manifest_ref: str
    manifest_hash: str
    counts: Mapping[str, int]
    schema_version: str = SNAPSHOT_REF_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_REF_SCHEMA:
            raise ValueError("semantic snapshot reference schema is unsupported")
        _hex_hash(self.snapshot_hash, field_name="semantic snapshot hash")
        _hex_hash(self.manifest_hash, field_name="semantic snapshot manifest hash")
        if not isinstance(self.manifest_ref, str) or not self.manifest_ref:
            raise ValueError("semantic snapshot manifest_ref is invalid")
        if not isinstance(self.counts, Mapping):
            raise ValueError("semantic snapshot counts are invalid")
        normalized = {str(key): int(value) for key, value in self.counts.items()}
        if set(normalized) != set(_LAYER_NAMES) or any(value < 0 for value in normalized.values()):
            raise ValueError("semantic snapshot counts are invalid")
        object.__setattr__(self, "counts", dict(sorted(normalized.items())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_hash": self.snapshot_hash,
            "manifest_ref": self.manifest_ref,
            "manifest_hash": self.manifest_hash,
            "counts": dict(self.counts),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SemanticSnapshotRef":
        if not isinstance(value, Mapping):
            raise ValueError("semantic snapshot reference must be an object")
        counts = value.get("counts")
        if not isinstance(counts, Mapping):
            raise ValueError("semantic snapshot reference counts are missing")
        try:
            return cls(
                schema_version=str(value.get("schema_version", "")),
                snapshot_hash=value.get("snapshot_hash"),
                manifest_ref=value.get("manifest_ref"),
                manifest_hash=value.get("manifest_hash"),
                counts=counts,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("semantic snapshot reference is invalid") from exc


@dataclass(frozen=True)
class ContextPayloadRef:
    """Small reference for an arbitrary caller-owned context bundle."""

    payload_hash: str
    payload_ref: str
    schema_version: str = CONTEXT_PAYLOAD_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != CONTEXT_PAYLOAD_SCHEMA:
            raise ValueError("analysis context payload schema is unsupported")
        _hex_hash(self.payload_hash, field_name="analysis context payload hash")
        if not isinstance(self.payload_ref, str) or not self.payload_ref:
            raise ValueError("analysis context payload ref is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "payload_hash": self.payload_hash,
            "payload_ref": self.payload_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContextPayloadRef":
        if not isinstance(value, Mapping):
            raise ValueError("analysis context payload reference must be an object")
        try:
            return cls(
                schema_version=str(value.get("schema_version", "")),
                payload_hash=value.get("payload_hash"),
                payload_ref=value.get("payload_ref"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("analysis context payload reference is invalid") from exc


@dataclass(frozen=True)
class SemanticSelectionRef:
    """Small immutable reference to one exact, content-addressed selection."""

    selection_hash: str
    selection_ref: str
    counts: Mapping[str, int]
    schema_version: str = SELECTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != SELECTION_SCHEMA:
            raise ValueError("semantic selection schema is unsupported")
        _hex_hash(self.selection_hash, field_name="semantic selection hash")
        if not isinstance(self.selection_ref, str) or not self.selection_ref:
            raise ValueError("semantic selection ref is invalid")
        if not isinstance(self.counts, Mapping):
            raise ValueError("semantic selection counts are invalid")
        normalized = {str(key): int(value) for key, value in self.counts.items()}
        if set(normalized) != set(_SELECTION_SET_NAMES.values()) or any(value < 0 for value in normalized.values()):
            raise ValueError("semantic selection counts are invalid")
        object.__setattr__(self, "counts", dict(sorted(normalized.items())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selection_hash": self.selection_hash,
            "selection_ref": self.selection_ref,
            "counts": dict(self.counts),
        }


class SemanticSnapshotStore:
    """Publisher/reader for one run-local semantic snapshot namespace."""

    @staticmethod
    def _root(context: "RunContext") -> Path:
        root = context.resolve_run_path(STORE_ROOT)
        _safe_path(root, root=context.run_root, label="semantic store")
        if root.exists() and root.is_symlink():
            raise ValueError("semantic store cannot be a symlink")
        return root

    @classmethod
    def _publish_lock(cls, context: "RunContext"):
        root = cls._root(context)
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / ".publish.lock"
        _safe_path(lock_path, root=context.run_root, label="semantic store lock")
        stream = lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return stream

    @staticmethod
    def _release_lock(stream: Any) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    @classmethod
    def publish_context_payload(cls, context: "RunContext", value: Any) -> ContextPayloadRef:
        """Persist one caller bundle without embedding it in the context manifest."""

        encoded = _canonical_context_payload(value)
        unsigned = {"schema_version": CONTEXT_PAYLOAD_SCHEMA, "payload": encoded}
        payload_hash = _hash_bytes(_json_bytes(unsigned))
        payload = {**unsigned, "payload_hash": payload_hash}
        root = cls._root(context) / "context_payloads"
        _safe_path(root, root=context.run_root, label="analysis context payloads")
        lock = cls._publish_lock(context)
        temporary: Path | None = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            target = root / f"{payload_hash}.json"
            _safe_path(target, root=context.run_root, label="analysis context payload")
            expected_bytes = _json_bytes(payload)
            if target.exists() or target.is_symlink():
                if target.is_symlink() or target.read_bytes() != expected_bytes:
                    raise ValueError("conflicting analysis context payload bytes already exist")
                return ContextPayloadRef(
                    payload_hash=payload_hash,
                    payload_ref=f"semantic_store/context_payloads/{payload_hash}.json",
                )
            temporary = Path(tempfile.mkdtemp(prefix=".context-payload-", dir=root))
            _safe_path(temporary, root=context.run_root, label="analysis context payload staging")
            _atomic_bytes(temporary / "payload.json", expected_bytes)
            os.replace(temporary / "payload.json", target)
            temporary.rmdir()
            temporary = None
            return ContextPayloadRef(
                payload_hash=payload_hash,
                payload_ref=f"semantic_store/context_payloads/{payload_hash}.json",
            )
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            cls._release_lock(lock)

    @classmethod
    def read_context_payload_ref(
        cls,
        context: "RunContext",
        value: Mapping[str, Any] | ContextPayloadRef | None,
    ) -> ContextPayloadRef | None:
        if value is None:
            return None
        ref = value if isinstance(value, ContextPayloadRef) else ContextPayloadRef.from_dict(value)
        expected = f"semantic_store/context_payloads/{ref.payload_hash}.json"
        if ref.payload_ref != expected:
            raise ValueError("analysis context payload ref is not canonical")
        path = _regular(context.resolve_run_path(ref.payload_ref), root=context.run_root, label="analysis context payload")
        raw_bytes = path.read_bytes()
        value = cls._load_json(path, label="analysis context payload")
        if raw_bytes != _json_bytes(value) or not isinstance(value, Mapping):
            raise ValueError("analysis context payload bytes are not canonical")
        unsigned = dict(value)
        payload_hash = unsigned.pop("payload_hash", None)
        if payload_hash != ref.payload_hash or _hash_bytes(_json_bytes(unsigned)) != ref.payload_hash:
            raise ValueError("analysis context payload hash does not match")
        if unsigned.get("schema_version") != CONTEXT_PAYLOAD_SCHEMA or "payload" not in unsigned:
            raise ValueError("analysis context payload schema is unsupported")
        return ref

    @classmethod
    def load_context_payload(cls, context: "RunContext", ref: ContextPayloadRef) -> Any:
        checked = cls.read_context_payload_ref(context, ref)
        if checked is None:  # pragma: no cover - type narrowing guard
            raise ValueError("analysis context payload reference is missing")
        value = cls._load_json(
            context.resolve_run_path(checked.payload_ref),
            label="analysis context payload",
        )
        try:
            payload = value["payload"]
        except (KeyError, TypeError) as exc:
            raise ValueError("analysis context payload is invalid") from exc
        if payload != _canonical_context_payload(payload):
            raise ValueError("analysis context payload is not canonical")
        return payload

    @classmethod
    def publish(cls, context: "RunContext", snapshot: Mapping[str, Any]) -> SemanticSnapshotRef:
        """Publish one immutable snapshot, idempotently for identical bytes."""

        if not isinstance(snapshot, Mapping):
            raise ValueError("semantic snapshot must be an object")
        layer_payloads: dict[str, bytes] = {}
        layer_indexes: dict[str, bytes] = {}
        layer_meta: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for layer in _LAYER_NAMES:
            raw = snapshot.get(layer, {} if layer == "relationships" else ())
            if layer == "relationships":
                if isinstance(raw, Mapping):
                    records = []
                    for raw_id, record in sorted(raw.items(), key=lambda pair: str(pair[0])):
                        if not isinstance(record, Mapping):
                            raise ValueError(f"semantic snapshot {layer} record is invalid")
                        item = {str(key): value for key, value in record.items()}
                        item.setdefault("relationship_id", str(raw_id))
                        if str(item["relationship_id"]) != str(raw_id):
                            raise ValueError("semantic relationship ID is inconsistent")
                        records.append(item)
                elif isinstance(raw, (list, tuple)):
                    records = [dict(record) for record in raw if isinstance(record, Mapping)]
                    if len(records) != len(raw):
                        raise ValueError(f"semantic snapshot {layer} record is invalid")
                else:
                    raise ValueError(f"semantic snapshot {layer} layer is invalid")
            else:
                if not isinstance(raw, (list, tuple)):
                    raise ValueError(f"semantic snapshot {layer} layer is invalid")
                records = [dict(record) for record in raw if isinstance(record, Mapping)]
                if len(records) != len(raw):
                    raise ValueError(f"semantic snapshot {layer} record is invalid")
            field = _LAYER_ID_FIELDS[layer]
            normalized: list[dict[str, Any]] = []
            ids: list[str] = []
            seen_ids: set[str] = set()
            for record in records:
                record_id = record.get(field)
                if not isinstance(record_id, str) or not record_id:
                    raise ValueError(f"semantic snapshot {layer} record ID is invalid")
                if record_id in seen_ids:
                    raise ValueError(f"semantic snapshot {layer} contains duplicate IDs")
                seen_ids.add(record_id)
                ids.append(record_id)
                normalized.append(record)
            order = sorted(range(len(normalized)), key=lambda index: ids[index])
            normalized = [normalized[index] for index in order]
            ids = [ids[index] for index in order]
            payload = _json_bytes(normalized)
            index = {"schema_version": "auto_foundry.semantic_layer_index.v1", "layer": layer, "ids": ids}
            index_bytes = _json_bytes(index)
            layer_payloads[layer] = payload
            layer_indexes[layer] = index_bytes
            counts[layer] = len(normalized)
            layer_meta[layer] = {
                "blob_ref": f"semantic_store/blobs/{_hash_bytes(payload)}.json",
                "hash": _hash_bytes(payload),
                "index_ref": f"semantic_store/blobs/{_hash_bytes(index_bytes)}.json",
                "index_hash": _hash_bytes(index_bytes),
                "count": len(normalized),
            }

        provenance = {
            key: snapshot.get(key)
            for key in (
                "projection_hash",
                "item_order",
                "source_item_ids",
                "source_resolution_domain_ids",
                "source_resolution_bindings",
            )
            if key in snapshot
        }
        unsigned = {
            "schema_version": SNAPSHOT_MANIFEST_SCHEMA,
            "projection": provenance,
            "counts": counts,
            "layers": layer_meta,
        }
        snapshot_hash = _hash_json(unsigned)
        manifest_unsigned = {**unsigned, "snapshot_hash": snapshot_hash}
        manifest_hash = _hash_json(manifest_unsigned)
        manifest = {**manifest_unsigned, "manifest_hash": manifest_hash}
        root = cls._root(context)
        snapshots_root = root / "snapshots"
        _safe_path(snapshots_root, root=context.run_root, label="semantic snapshots")
        blobs_root = root / "blobs"
        _safe_path(blobs_root, root=context.run_root, label="semantic blobs")
        lock = cls._publish_lock(context)
        temporary_root: Path | None = None
        try:
            snapshots_root.mkdir(parents=True, exist_ok=True)
            blobs_root.mkdir(parents=True, exist_ok=True)
            target = snapshots_root / snapshot_hash
            _safe_path(target, root=context.run_root, label="semantic snapshot")
            if target.exists() or target.is_symlink():
                ref = cls._ref_from_manifest_path(context, target / "manifest.json")
                if ref.manifest_hash != manifest_hash or ref.counts != counts:
                    raise ValueError("conflicting semantic snapshot bytes already exist")
                # The immutable manifest is the cheap authority check.  Layer
                # bytes remain unopened here and are verified when a caller
                # searches/selects that layer; repeated context creation does
                # not scan all potentially large semantic payloads.
                cls._read_manifest(context, ref)
                return ref

            # Blobs are immutable and content-addressed.  Publish each one
            # before the manifest so a partial attempt leaves no visible
            # snapshot; orphaned blobs are harmless and are reused by retries.
            for layer in _LAYER_NAMES:
                meta = layer_meta[layer]
                cls._publish_blob(
                    context,
                    blobs_root,
                    meta["hash"],
                    layer_payloads[layer],
                )
                cls._publish_blob(
                    context,
                    blobs_root,
                    meta["index_hash"],
                    layer_indexes[layer],
                )

            temporary_root = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=snapshots_root))
            _safe_path(temporary_root, root=context.run_root, label="semantic snapshot staging")
            _atomic_bytes(temporary_root / "manifest.json", _json_bytes(manifest))
            try:
                # Check again immediately before making the manifest visible;
                # an unexpected symlink or object is never overwritten.
                _safe_path(target, root=context.run_root, label="semantic snapshot")
                if target.exists() or target.is_symlink():
                    ref = cls._ref_from_manifest_path(context, target / "manifest.json")
                    if ref.manifest_hash != manifest_hash or ref.counts != counts:
                        raise ValueError("conflicting semantic snapshot bytes already exist")
                    cls._read_manifest(context, ref)
                    staged_snapshot = temporary_root
                    temporary_root = None
                    shutil.rmtree(staged_snapshot, ignore_errors=True)
                    return ref
                os.replace(temporary_root, target)
                temporary_root = None
            except OSError:
                if target.exists() or target.is_symlink():
                    ref = cls._ref_from_manifest_path(context, target / "manifest.json")
                    if ref.manifest_hash != manifest_hash or ref.counts != counts:
                        raise ValueError("conflicting semantic snapshot bytes already exist")
                    cls._read_manifest(context, ref)
                    return ref
                raise
            return SemanticSnapshotRef(
                snapshot_hash=snapshot_hash,
                manifest_ref=f"semantic_store/snapshots/{snapshot_hash}/manifest.json",
                manifest_hash=manifest_hash,
                counts=counts,
            )
        finally:
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)
            cls._release_lock(lock)

    @classmethod
    def _publish_blob(
        cls,
        context: "RunContext",
        blobs_root: Path,
        blob_hash: str,
        data: bytes,
    ) -> None:
        """Publish one canonical immutable blob while the store lock is held."""

        _hex_hash(blob_hash, field_name="semantic blob hash")
        if _hash_bytes(data) != blob_hash:
            raise ValueError("semantic blob hash does not match canonical bytes")
        target = blobs_root / f"{blob_hash}.json"
        _safe_path(target, root=context.run_root, label="semantic blob")
        if target.exists() or target.is_symlink():
            existing = _regular(target, root=context.run_root, label="semantic blob").read_bytes()
            if _hash_bytes(existing) != blob_hash:
                raise ValueError("semantic blob hash mismatch or collision")
            if existing != data:
                raise ValueError("conflicting semantic blob bytes already exist")
            return

        temporary: Path | None = None
        try:
            temporary = Path(tempfile.mkdtemp(prefix=".blob-", dir=blobs_root))
            _safe_path(temporary, root=context.run_root, label="semantic blob staging")
            _atomic_bytes(temporary / "blob.json", data)
            # Recheck immediately before visibility.  A symlink or an
            # unexpected file is never replaced silently.
            _safe_path(target, root=context.run_root, label="semantic blob")
            if target.exists() or target.is_symlink():
                existing = _regular(target, root=context.run_root, label="semantic blob").read_bytes()
                if _hash_bytes(existing) != blob_hash:
                    raise ValueError("semantic blob hash mismatch or collision")
                if existing != data:
                    raise ValueError("conflicting semantic blob bytes already exist")
            else:
                os.replace(temporary / "blob.json", target)
            temporary.rmdir()
            temporary = None
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)

    @classmethod
    def _verify_blob_bytes(
        cls,
        context: "RunContext",
        blob_ref: str,
        blob_hash: str,
        *,
        layer: str,
        expected: bytes | None = None,
    ) -> bytes:
        path = cls._blob_path(context, blob_ref, blob_hash, label=layer)
        data = path.read_bytes()
        if _hash_bytes(data) != blob_hash:
            raise ValueError(f"{layer} hash does not match")
        if expected is not None and data != expected:
            raise ValueError(f"conflicting {layer} bytes already exist")
        return data

    @classmethod
    def _ref_from_manifest_path(cls, context: "RunContext", path: Path) -> SemanticSnapshotRef:
        _regular(path, root=context.run_root, label="semantic snapshot manifest")
        value = cls._load_json(path, label="semantic snapshot manifest")
        if not isinstance(value, Mapping):
            raise ValueError("semantic snapshot manifest must be an object")
        try:
            return SemanticSnapshotRef(
                snapshot_hash=str(value["snapshot_hash"]),
                manifest_ref=str(path.relative_to(context.run_root)),
                manifest_hash=str(value["manifest_hash"]),
                counts=value["counts"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("semantic snapshot manifest reference is invalid") from exc

    @staticmethod
    def _load_json(path: Path, *, label: str) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is unreadable") from exc

    @staticmethod
    def _load_json_bytes(data: bytes, *, label: str) -> Any:
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is unreadable") from exc

    @classmethod
    def read_ref(cls, context: "RunContext", value: Mapping[str, Any] | SemanticSnapshotRef | None) -> SemanticSnapshotRef | None:
        if value is None:
            return None
        ref = value if isinstance(value, SemanticSnapshotRef) else SemanticSnapshotRef.from_dict(value)
        expected = f"semantic_store/snapshots/{ref.snapshot_hash}/manifest.json"
        if ref.manifest_ref != expected:
            raise ValueError("semantic snapshot manifest_ref is not canonical")
        cls._read_manifest(context, ref)
        return ref

    @classmethod
    def _manifest_path(cls, context: "RunContext", ref: SemanticSnapshotRef) -> Path:
        path = context.resolve_run_path(ref.manifest_ref)
        expected = context.resolve_run_path(f"semantic_store/snapshots/{ref.snapshot_hash}/manifest.json")
        if path != expected:
            raise ValueError("semantic snapshot manifest path is not canonical")
        return _regular(path, root=context.run_root, label="semantic snapshot manifest")

    @classmethod
    def _read_manifest(cls, context: "RunContext", ref: SemanticSnapshotRef) -> Mapping[str, Any]:
        path = cls._manifest_path(context, ref)
        snapshot_root = path.parent
        _safe_path(snapshot_root, root=context.run_root, label="semantic snapshot")
        try:
            entries = tuple(snapshot_root.iterdir())
        except OSError as exc:
            raise ValueError("semantic snapshot directory is unreadable") from exc
        for entry in entries:
            _safe_path(entry, root=context.run_root, label="semantic snapshot")
        if {entry.name for entry in entries} != {"manifest.json"}:
            raise ValueError("semantic snapshot directory must contain only manifest.json")
        raw_bytes = path.read_bytes()
        value = cls._load_json(path, label="semantic snapshot manifest")
        if not isinstance(value, Mapping):
            raise ValueError("semantic snapshot manifest must be an object")
        if raw_bytes != _json_bytes(value):
            raise ValueError("semantic snapshot manifest bytes are not canonical")
        unsigned = dict(value)
        manifest_hash = unsigned.pop("manifest_hash", None)
        if not isinstance(manifest_hash, str) or _hash_json(unsigned) != manifest_hash or manifest_hash != ref.manifest_hash:
            raise ValueError("semantic snapshot manifest hash does not match")
        snapshot_hash = unsigned.get("snapshot_hash")
        snapshot_unsigned = dict(unsigned)
        snapshot_unsigned.pop("snapshot_hash", None)
        if snapshot_hash != ref.snapshot_hash or _hash_json(snapshot_unsigned) != snapshot_hash:
            raise ValueError("semantic snapshot hash does not match")
        if unsigned.get("schema_version") != SNAPSHOT_MANIFEST_SCHEMA:
            raise ValueError("semantic snapshot manifest schema is unsupported")
        counts = unsigned.get("counts")
        layers = unsigned.get("layers")
        if not isinstance(counts, Mapping) or set(counts) != set(_LAYER_NAMES) or not isinstance(layers, Mapping) or set(layers) != set(_LAYER_NAMES):
            raise ValueError("semantic snapshot manifest layers/counts are invalid")
        for layer in _LAYER_NAMES:
            meta = layers[layer]
            if not isinstance(meta, Mapping) or set(meta) != {"blob_ref", "hash", "index_ref", "index_hash", "count"}:
                raise ValueError("semantic snapshot manifest layer metadata is invalid")
            count = meta.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count != counts[layer]:
                raise ValueError("semantic snapshot manifest layer metadata is invalid")
            payload_hash = _hex_hash(meta.get("hash"), field_name=f"semantic snapshot {layer} hash")
            index_hash = _hex_hash(meta.get("index_hash"), field_name=f"semantic snapshot {layer} index hash")
            if meta.get("blob_ref") != f"semantic_store/blobs/{payload_hash}.json" or meta.get("index_ref") != f"semantic_store/blobs/{index_hash}.json":
                raise ValueError("semantic snapshot layer path is not canonical")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueError("semantic snapshot manifest counts are invalid")
        if dict(ref.counts) != {str(key): int(value) for key, value in counts.items()}:
            raise ValueError("semantic snapshot reference counts do not match manifest")
        return value

    @classmethod
    def manifest(cls, context: "RunContext", ref: SemanticSnapshotRef) -> Mapping[str, Any]:
        """Read and verify only the snapshot manifest (no layer payloads)."""

        ref = cls.read_ref(context, ref)
        if ref is None:  # pragma: no cover - type narrowing guard
            raise ValueError("semantic snapshot reference is missing")
        return cls._read_manifest(context, ref)

    @classmethod
    def layer_ids(cls, context: "RunContext", ref: SemanticSnapshotRef, layer: str) -> tuple[str, ...]:
        meta = cls._layer_meta(cls._read_manifest(context, ref), layer)
        data = cls._verify_blob_bytes(
            context,
            meta["index_ref"],
            meta["index_hash"],
            layer=f"semantic snapshot {layer} index",
        )
        value = cls._load_json_bytes(data, label=f"semantic snapshot {layer} index")
        if data != _json_bytes(value):
            raise ValueError(f"semantic snapshot {layer} index bytes are not canonical")
        if not isinstance(value, Mapping) or value.get("schema_version") != "auto_foundry.semantic_layer_index.v1" or value.get("layer") != layer:
            raise ValueError(f"semantic snapshot {layer} index is invalid")
        ids = value.get("ids")
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids) or tuple(sorted(set(ids))) != tuple(ids):
            raise ValueError(f"semantic snapshot {layer} index IDs are invalid")
        if len(ids) != meta["count"]:
            raise ValueError(f"semantic snapshot {layer} index count does not match")
        return tuple(ids)

    @classmethod
    def validate_ids(cls, context: "RunContext", ref: SemanticSnapshotRef, layer: str, ids: Iterable[str]) -> tuple[str, ...]:
        wanted = tuple(str(value) for value in ids)
        if len(set(wanted)) != len(wanted):
            raise ValueError(f"semantic {layer} IDs must not contain duplicates")
        known = set(cls.layer_ids(context, ref, layer))
        missing = [value for value in wanted if value not in known]
        if missing:
            raise KeyError(f"unknown semantic {layer} ID: {missing[0]}")
        return wanted

    @classmethod
    def _layer_meta(cls, manifest: Mapping[str, Any], layer: str) -> Mapping[str, Any]:
        if layer not in _LAYER_NAMES:
            raise ValueError(f"unknown semantic layer: {layer}")
        layers = manifest.get("layers")
        if not isinstance(layers, Mapping) or not isinstance(layers.get(layer), Mapping):
            raise ValueError(f"semantic snapshot {layer} metadata is missing")
        return layers[layer]

    @classmethod
    def _layer_path(cls, context: "RunContext", ref: SemanticSnapshotRef, meta: Mapping[str, Any], *, index: bool) -> Path:
        ref_key = "index_ref" if index else "blob_ref"
        hash_key = "index_hash" if index else "hash"
        return cls._blob_path(
            context,
            str(meta[ref_key]),
            str(meta[hash_key]),
            label="semantic snapshot layer index" if index else "semantic snapshot layer",
        )

    @classmethod
    def _blob_path(cls, context: "RunContext", blob_ref: str, blob_hash: str, *, label: str) -> Path:
        blob_hash = _hex_hash(blob_hash, field_name=f"{label} hash")
        expected = f"semantic_store/blobs/{blob_hash}.json"
        if blob_ref != expected:
            raise ValueError(f"{label} ref is not canonical")
        blobs_root = cls._root(context) / "blobs"
        _safe_path(blobs_root, root=context.run_root, label="semantic blobs")
        path = context.resolve_run_path(blob_ref)
        _safe_path(path, root=blobs_root, label=label)
        return _regular(path, root=context.run_root, label=label)

    @classmethod
    def _read_layer(cls, context: "RunContext", ref: SemanticSnapshotRef, layer: str) -> tuple[Mapping[str, Any], ...]:
        manifest = cls._read_manifest(context, ref)
        meta = cls._layer_meta(manifest, layer)
        data = cls._verify_blob_bytes(
            context,
            meta["blob_ref"],
            meta["hash"],
            layer=f"semantic snapshot {layer}",
        )
        value = cls._load_json_bytes(data, label=f"semantic snapshot {layer} layer")
        if data != _json_bytes(value):
            raise ValueError(f"semantic snapshot {layer} layer bytes are not canonical")
        if not isinstance(value, list) or len(value) != meta["count"] or any(not isinstance(item, Mapping) for item in value):
            raise ValueError(f"semantic snapshot {layer} layer is invalid")
        records = tuple(dict(item) for item in value)
        field = _LAYER_ID_FIELDS[layer]
        ids = tuple(record.get(field) for record in records)
        if any(not isinstance(item, str) or not item for item in ids) or tuple(sorted(ids)) != ids or len(set(ids)) != len(ids):
            raise ValueError(f"semantic snapshot {layer} record IDs are invalid")
        indexed = cls.layer_ids(context, ref, layer)
        if ids != indexed:
            raise ValueError(f"semantic snapshot {layer} index does not match layer")
        return records

    @classmethod
    def records(cls, context: "RunContext", ref: SemanticSnapshotRef, layers: str | Iterable[str]) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
        names = (layers,) if isinstance(layers, str) else tuple(layers)
        if len(set(names)) != len(names):
            raise ValueError("semantic layer names must not repeat")
        return {name: cls._read_layer(context, ref, name) for name in names}

    @classmethod
    def publish_selection(
        cls,
        context: "RunContext",
        snapshot_ref: SemanticSnapshotRef,
        selections: Mapping[str, Iterable[str]],
    ) -> SemanticSelectionRef:
        cls.read_ref(context, snapshot_ref)
        normalized: dict[str, list[str]] = {}
        for set_name in _SELECTION_SET_NAMES.values():
            values = tuple(str(value) for value in selections.get(set_name, ()))
            if len(set(values)) != len(values) or any(not value for value in values):
                raise ValueError(f"semantic selection {set_name} is invalid")
            normalized[set_name] = sorted(values)
        counts = {name: len(values) for name, values in normalized.items()}
        unsigned = {
            "schema_version": SELECTION_SCHEMA,
            "snapshot_hash": snapshot_ref.snapshot_hash,
            "snapshot_manifest_hash": snapshot_ref.manifest_hash,
            "sets": normalized,
            "counts": counts,
        }
        # The selection identity covers the exact canonical bytes that are
        # published (including its single trailing newline).
        selection_hash = _hash_bytes(_json_bytes(unsigned))
        payload = {**unsigned, "selection_hash": selection_hash}
        root = cls._root(context) / "selections"
        _safe_path(root, root=context.run_root, label="semantic selections")
        lock = cls._publish_lock(context)
        temporary: Path | None = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            target = root / f"{selection_hash}.json"
            _safe_path(target, root=context.run_root, label="semantic selection")
            expected_bytes = _json_bytes(payload)
            if target.exists() or target.is_symlink():
                existing = target.read_bytes()
                if existing != expected_bytes:
                    raise ValueError("conflicting or non-canonical semantic selection bytes already exist")
                return SemanticSelectionRef(
                    selection_hash=selection_hash,
                    selection_ref=f"semantic_store/selections/{selection_hash}.json",
                    counts=counts,
                )
            temporary = Path(tempfile.mkdtemp(prefix=".selection-", dir=root))
            _safe_path(temporary, root=context.run_root, label="semantic selection staging")
            _atomic_bytes(temporary / "payload.json", expected_bytes)
            os.replace(temporary / "payload.json", target)
            temporary.rmdir()
            temporary = None
            return SemanticSelectionRef(
                selection_hash=selection_hash,
                selection_ref=f"semantic_store/selections/{selection_hash}.json",
                counts=counts,
            )
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            cls._release_lock(lock)

    @classmethod
    def load_selection(
        cls,
        context: "RunContext",
        snapshot_ref: SemanticSnapshotRef,
        selection_ref: str,
        selection_hash: str,
    ) -> Mapping[str, tuple[str, ...]]:
        # Bind every selection, including an empty/no-reuse selection, to a
        # currently valid immutable snapshot before accepting its bytes.
        snapshot_ref = cls.read_ref(context, snapshot_ref)
        if snapshot_ref is None:  # pragma: no cover - type narrowing guard
            raise ValueError("semantic selection snapshot reference is missing")
        _hex_hash(selection_hash, field_name="semantic selection hash")
        expected = f"semantic_store/selections/{selection_hash}.json"
        if selection_ref != expected:
            raise ValueError("semantic selection ref is not canonical")
        path = _regular(context.resolve_run_path(selection_ref), root=context.run_root, label="semantic selection")
        data = path.read_bytes()
        value = cls._load_json(path, label="semantic selection")
        if not isinstance(value, Mapping):
            raise ValueError("semantic selection is invalid")
        if data != _json_bytes(value):
            raise ValueError("semantic selection bytes are not canonical")
        unsigned = dict(value)
        actual_hash = unsigned.pop("selection_hash", None)
        if actual_hash != selection_hash or _hash_bytes(_json_bytes(unsigned)) != selection_hash:
            raise ValueError("semantic selection hash does not match")
        if unsigned.get("schema_version") != SELECTION_SCHEMA or unsigned.get("snapshot_hash") != snapshot_ref.snapshot_hash or unsigned.get("snapshot_manifest_hash") != snapshot_ref.manifest_hash:
            raise ValueError("semantic selection binding does not match semantic snapshot")
        counts = unsigned.get("counts")
        sets = unsigned.get("sets")
        if not isinstance(counts, Mapping) or not isinstance(sets, Mapping):
            raise ValueError("semantic selection sets/counts are invalid")
        result: dict[str, tuple[str, ...]] = {}
        for set_name in _SELECTION_SET_NAMES.values():
            values = sets.get(set_name)
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values) or tuple(sorted(set(values))) != tuple(values):
                raise ValueError("semantic selection IDs are invalid")
            if counts.get(set_name) != len(values):
                raise ValueError("semantic selection count does not match IDs")
            result[set_name] = tuple(values)
        # Validate index bindings while resolving an exact selection.  Layer
        # payloads remain unopened until a caller asks for typed records.
        for layer, set_name in _SELECTION_SET_NAMES.items():
            if result[set_name]:
                cls.validate_ids(context, snapshot_ref, layer, result[set_name])
        return result


__all__ = [
    "ContextPayloadRef",
    "canonical_context_payload",
    "SemanticSelectionRef",
    "SemanticSnapshotRef",
    "SemanticSnapshotStore",
    "SELECTION_SCHEMA",
    "CONTEXT_PAYLOAD_SCHEMA",
    "SNAPSHOT_MANIFEST_SCHEMA",
    "SNAPSHOT_REF_SCHEMA",
]
