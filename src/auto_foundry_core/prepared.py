"""Durable run-local registry for accepted prepared assets.

The registry is deliberately independent of the in-memory Living Enterprise
Model.  A materialized prepared asset is registered once it has passed the
descriptor/content checks, regardless of whether its scope makes it eligible
for reuse.  Later integration code can search the registry and explicitly
load an asset without treating registration as a semantic promotion.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

try:  # pragma: no cover - fcntl is present on supported macOS/POSIX hosts
    import fcntl
except ImportError:  # pragma: no cover - defensive for non-POSIX packaging
    fcntl = None  # type: ignore[assignment]

from .contracts import DataAssetRef, PreparedAssetDescriptor
from .workspace import AllowedRootError, RunContext


REGISTRY_SCHEMA_VERSION = "1"
INDEX_SCHEMA_VERSION = "1"
ALLOWED_SCOPES = frozenset({"requirement_scoped", "reusable", "exploratory", "superseded"})
_REGISTRY_RELATIVE = Path("lem") / "prepared_data_registry.jsonl"
_INDEX_RELATIVE = Path("indexes") / "prepared_index.json"
_LOCK_RELATIVE = Path("lem") / ".prepared_data_registry.lock"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    """Write one registry/index file atomically and durably."""

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
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
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


@contextmanager
def _registry_lock(path: Path):
    """Serialize registry/index publication across concurrent run callers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _regular_file(context: RunContext, value: str | Path, *, label: str) -> Path:
    try:
        resolved = context.resolve_run_path(value)
    except Exception as exc:
        raise AllowedRootError(f"{label} escapes current run") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} must be a regular run-local file: {resolved.name}")
    return resolved


def _decode_rows_bytes(
    encoded: bytes,
    descriptor: PreparedAssetDescriptor,
    *,
    fallback_format: str = "jsonl",
) -> list[dict[str, Any]]:
    fmt = str(descriptor.metadata.get("format", fallback_format)).lower()
    if fmt == "jsonl":
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(encoded.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"prepared JSONL row {line_number} is not an object")
            rows.append(dict(value))
        return rows
    if fmt == "csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(encoded.decode("utf-8")))]
    raise ValueError(f"unsupported prepared asset format: {fmt}")


def _decode_rows(path: Path, descriptor: PreparedAssetDescriptor) -> list[dict[str, Any]]:
    return _decode_rows_bytes(path.read_bytes(), descriptor, fallback_format=path.suffix.lstrip("."))


def _verify_descriptor(context: RunContext, descriptor: PreparedAssetDescriptor) -> Path:
    if descriptor.scope not in ALLOWED_SCOPES:
        raise ValueError(f"prepared asset scope is invalid: {descriptor.scope!r}")
    path = _regular_file(context, descriptor.location, label="prepared asset location")
    actual_hash = _sha256(path)
    if descriptor.prepared_content_hash and actual_hash != descriptor.prepared_content_hash:
        raise ValueError(f"prepared asset content changed: {descriptor.prepared_asset_id}")
    if descriptor.byte_count is not None and descriptor.byte_count != path.stat().st_size:
        raise ValueError(f"prepared asset byte count changed: {descriptor.prepared_asset_id}")
    rows = _decode_rows(path, descriptor)
    if descriptor.row_count is not None and descriptor.row_count != len(rows):
        raise ValueError(f"prepared asset row count changed: {descriptor.prepared_asset_id}")
    return path


def _resolve_materialization_path(context: RunContext, value: str | Path, *, label: str) -> Path:
    """Resolve a run-owned destination before a materialization transaction.

    ``RunContext.resolve_run_path`` resolves containment and symlinks.  The
    additional regular-file check rejects a pre-existing directory while still
    allowing a not-yet-created file, which is required for crash-resume
    publication.
    """

    try:
        resolved = context.resolve_run_path(value)
    except Exception as exc:
        raise AllowedRootError(f"{label} escapes current run") from exc
    if resolved.exists() and (resolved.is_symlink() or not resolved.is_file()):
        raise ValueError(f"{label} must be a regular run-local file: {resolved}")
    return resolved


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically publish one prepared payload and fsync its parent."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(directory)
            except OSError:
                pass
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_descriptor_sidecar(path: Path) -> PreparedAssetDescriptor | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("prepared descriptor sidecar must be an object")
        return PreparedAssetDescriptor.from_dict(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"prepared descriptor sidecar is invalid: {path.name}") from exc


class PreparedAssetRegistry:
    """Durable registry of accepted, run-local prepared descriptors."""

    allowed_scopes = ALLOWED_SCOPES

    def __init__(self, context: RunContext, *, telemetry: Any = None) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("PreparedAssetRegistry requires one RunContext")
        self.context = context
        self.telemetry = telemetry
        self.registry_path = context.resolve_run_path(_REGISTRY_RELATIVE)
        self.index_path = context.resolve_run_path(_INDEX_RELATIVE)
        self.lock_path = context.resolve_run_path(_LOCK_RELATIVE)

    def _read_records(self) -> list[PreparedAssetDescriptor]:
        if not self.registry_path.is_file():
            return []
        records: list[PreparedAssetDescriptor] = []
        seen: set[str] = set()
        try:
            lines = self.registry_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("prepared registry is unreadable") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                descriptor = PreparedAssetDescriptor.from_dict(payload)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"prepared registry line {line_number} is invalid") from exc
            if descriptor.prepared_asset_id in seen:
                raise ValueError(f"prepared registry contains duplicate asset: {descriptor.prepared_asset_id}")
            seen.add(descriptor.prepared_asset_id)
            records.append(descriptor)
        return records

    @staticmethod
    def _index_entry(descriptor: PreparedAssetDescriptor) -> dict[str, Any]:
        return {
            "prepared_asset_id": descriptor.prepared_asset_id,
            "location": descriptor.location,
            "source_refs": [
                ref.to_dict() if isinstance(ref, DataAssetRef) else str(ref)
                for ref in descriptor.source_refs
            ],
            "prepared_content_hash": descriptor.prepared_content_hash,
            "operation_manifest_hash": descriptor.operation_manifest_hash,
            "source_hashes": list(descriptor.source_hashes),
            "scope": descriptor.scope,
            "status": descriptor.status,
            "core_version": descriptor.core_version,
            "row_count": descriptor.row_count,
            "byte_count": descriptor.byte_count,
            "effective_period": descriptor.effective_period,
        }

    def _write_records(self, records: Iterable[PreparedAssetDescriptor]) -> None:
        ordered = sorted(records, key=lambda descriptor: descriptor.prepared_asset_id)
        content = "".join(
            json.dumps(descriptor.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for descriptor in ordered
        )
        _atomic_write(self.registry_path, content)
        index = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "registry_schema_version": REGISTRY_SCHEMA_VERSION,
            "run_id": self.context.run_id,
            "entries": [self._index_entry(descriptor) for descriptor in ordered],
        }
        _atomic_write(self.index_path, json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    @staticmethod
    def _coerce_descriptor(descriptor: PreparedAssetDescriptor | Mapping[str, Any]) -> PreparedAssetDescriptor:
        return descriptor if isinstance(descriptor, PreparedAssetDescriptor) else PreparedAssetDescriptor.from_dict(descriptor)

    def _validated_descriptor(self, descriptor: PreparedAssetDescriptor | Mapping[str, Any]) -> PreparedAssetDescriptor:
        value = self._coerce_descriptor(descriptor)
        _verify_descriptor(self.context, value)
        return value

    def _validated_materialization(
        self,
        descriptor: PreparedAssetDescriptor | Mapping[str, Any],
        content: bytes | bytearray | memoryview,
        descriptor_path: str | Path,
    ) -> tuple[PreparedAssetDescriptor, bytes, Path, Path]:
        """Validate an in-memory payload before entering publication.

        This validation deliberately does not touch a destination file.  It
        lets :meth:`materialize_accepted` make the same-ID decision and then
        publish payload, descriptor sidecar, registry, and index while holding
        one lock.
        """

        value = self._coerce_descriptor(descriptor)
        if value.scope not in ALLOWED_SCOPES:
            raise ValueError(f"prepared asset scope is invalid: {value.scope!r}")
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("prepared asset content must be bytes-like")
        encoded = bytes(content)
        if value.prepared_content_hash is None:
            raise ValueError("prepared asset content hash is required for materialization")
        if value.byte_count is None:
            raise ValueError("prepared asset byte count is required for materialization")
        if value.row_count is None:
            raise ValueError("prepared asset row count is required for materialization")
        actual_hash = hashlib.sha256(encoded).hexdigest()
        if value.prepared_content_hash != actual_hash:
            raise ValueError(f"prepared asset content hash does not match: {value.prepared_asset_id}")
        if value.byte_count != len(encoded):
            raise ValueError(f"prepared asset byte count does not match: {value.prepared_asset_id}")
        rows = _decode_rows_bytes(
            encoded,
            value,
            fallback_format=Path(value.location).suffix.lstrip("."),
        )
        if value.row_count != len(rows):
            raise ValueError(f"prepared asset row count does not match: {value.prepared_asset_id}")
        payload_path = _resolve_materialization_path(self.context, value.location, label="prepared asset location")
        sidecar = _resolve_materialization_path(self.context, descriptor_path, label="prepared descriptor location")
        return value, encoded, payload_path, sidecar

    @staticmethod
    def _check_residue(
        path: Path,
        expected: bytes,
        *,
        label: str,
    ) -> None:
        """Reject an unregistered crash residue that does not match exactly."""

        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} residue is not a regular file: {path}")
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"{label} residue is unreadable: {path}") from exc
        if current != expected:
            raise ValueError(f"{label} residue differs from accepted descriptor")

    def materialize_accepted(
        self,
        descriptor: PreparedAssetDescriptor | Mapping[str, Any],
        content: bytes | bytearray | memoryview,
        descriptor_path: str | Path,
    ) -> PreparedAssetDescriptor:
        """Atomically materialize and register one accepted prepared asset.

        The lock covers the same-ID decision, crash-residue checks, payload and
        sidecar writes, and registry/index publication.  A retry after any
        boundary failure therefore either observes exact residue and resumes,
        or fails closed before changing a conflicting asset.
        """

        value, encoded, payload_path, sidecar_path = self._validated_materialization(
            descriptor,
            content,
            descriptor_path,
        )
        sidecar_text = json.dumps(value.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        sidecar_bytes = sidecar_text.encode("utf-8")
        with _registry_lock(self.lock_path):
            records = self._read_records()
            existing = next(
                (record for record in records if record.prepared_asset_id == value.prepared_asset_id),
                None,
            )
            if existing is not None and existing != value:
                raise ValueError(f"prepared asset already exists with different descriptor: {value.prepared_asset_id}")

            # A registered equal descriptor may repair missing files/index
            # projections.  An unregistered residue may only be resumed when
            # both payload and sidecar are exact; otherwise a prior failed or
            # conflicting writer must not be overwritten.
            if existing is None:
                self._check_residue(payload_path, encoded, label="prepared payload")
                self._check_residue(sidecar_path, sidecar_bytes, label="prepared descriptor")
            else:
                if payload_path.exists() and payload_path.read_bytes() != encoded:
                    raise ValueError(f"registered prepared payload changed: {value.prepared_asset_id}")
                sidecar_existing = _read_descriptor_sidecar(sidecar_path)
                if sidecar_existing is not None and sidecar_existing != value:
                    raise ValueError(f"registered prepared descriptor changed: {value.prepared_asset_id}")

            # Publication order is intentional: payload, sidecar, registry,
            # then derived index.  Each step is atomic and the enclosing lock
            # makes crash retries converge without a competing writer.
            _atomic_write_bytes(payload_path, encoded)
            _atomic_write(sidecar_path, sidecar_text)
            if existing is None:
                records.append(value)
            self._write_records(records)

        if existing is None:
            self._emit("prepared_registry_registered", value)
        return existing or value

    def preflight_register(self, descriptor: PreparedAssetDescriptor | Mapping[str, Any]) -> PreparedAssetDescriptor:
        """Validate an accepted descriptor without publishing registry state.

        The same-ID decision is made while holding the registry lock so an
        integration owner can preflight a complete bundle before its first
        commit and receive the exact descriptor that would be accepted.
        """

        value = self._validated_descriptor(descriptor)
        with _registry_lock(self.lock_path):
            records = self._read_records()
            existing = next((record for record in records if record.prepared_asset_id == value.prepared_asset_id), None)
            if existing is not None and existing != value:
                raise ValueError(f"prepared asset already exists with different descriptor: {value.prepared_asset_id}")
            return existing or value

    def register_accepted(self, descriptor: PreparedAssetDescriptor | Mapping[str, Any]) -> PreparedAssetDescriptor:
        """Register one verified descriptor; duplicate registration is idempotent."""

        descriptor = self._validated_descriptor(descriptor)
        with _registry_lock(self.lock_path):
            records = self._read_records()
            existing = next((record for record in records if record.prepared_asset_id == descriptor.prepared_asset_id), None)
            if existing is not None:
                if existing != descriptor:
                    raise ValueError(f"prepared asset already exists with different descriptor: {descriptor.prepared_asset_id}")
                return existing
            records.append(descriptor)
            self._write_records(records)
        self._emit("prepared_registry_registered", descriptor)
        return descriptor

    def search(
        self,
        query: str | None = None,
        *,
        prepared_asset_id: str | None = None,
        scope: str | None = None,
        reusable_only: bool = False,
        source_hashes: Iterable[str] | None = None,
        include_superseded: bool = False,
    ) -> tuple[PreparedAssetDescriptor, ...]:
        """Search descriptors without reading prepared payloads."""

        if scope is not None and scope not in ALLOWED_SCOPES:
            raise ValueError(f"prepared asset scope is invalid: {scope!r}")
        wanted_hashes = set(str(value) for value in (source_hashes or ()))
        normalized_query = str(query).strip().lower() if query is not None else ""
        records = self._read_records()
        result: list[PreparedAssetDescriptor] = []
        for descriptor in records:
            if prepared_asset_id is not None and descriptor.prepared_asset_id != str(prepared_asset_id):
                continue
            if scope is not None and descriptor.scope != scope:
                continue
            if reusable_only and (descriptor.scope != "reusable" or descriptor.status == "superseded"):
                continue
            if not include_superseded and (descriptor.scope == "superseded" or descriptor.status == "superseded"):
                continue
            if wanted_hashes and not wanted_hashes.issubset(set(descriptor.source_hashes)):
                continue
            if normalized_query:
                haystack = " ".join(
                    (
                        descriptor.prepared_asset_id,
                        descriptor.location,
                        descriptor.scope,
                        descriptor.status,
                        *(str(value) for value in descriptor.source_hashes),
                        *(str(value) for value in descriptor.source_refs),
                        *(str(value) for value in descriptor.ontology_refs),
                    )
                ).lower()
                if normalized_query not in haystack:
                    continue
            result.append(descriptor)
        return tuple(sorted(result, key=lambda item: item.prepared_asset_id))

    def load(self, prepared_asset_id: str) -> Any:
        """Load and re-verify one explicitly selected prepared asset."""

        matches = self.search(prepared_asset_id=prepared_asset_id, include_superseded=True)
        if not matches:
            raise FileNotFoundError(f"prepared asset is not registered: {prepared_asset_id}")
        descriptor = matches[0]
        path = _verify_descriptor(self.context, descriptor)
        rows = tuple(_decode_rows(path, descriptor))
        # Import lazily to avoid the workbench -> registry import cycle.
        from .workbench import PreparedAsset

        return PreparedAsset(descriptor=descriptor, rows=rows)

    def _emit(self, event_type: str, descriptor: PreparedAssetDescriptor) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry.record(
                event_type,
                rows=descriptor.row_count,
                bytes_processed=descriptor.byte_count,
                output_hashes=(descriptor.prepared_content_hash,) if descriptor.prepared_content_hash else (),
                facts={"prepared_asset_id": descriptor.prepared_asset_id, "scope": descriptor.scope},
            )
        except Exception:
            pass


__all__ = ["ALLOWED_SCOPES", "PreparedAssetRegistry"]
