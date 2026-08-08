"""Deterministic local artifact writing and lineage manifests."""

from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import DataAssetRef, OperationReceipt, OperationResultRef, OperationSpec
from .sources import hash_file
from .workspace import RunContext, require_allowed_roots, validate_allowed_path


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


def hash_value(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest_path_hash(path: str | Path, *, allowed_roots, context: str) -> str:
    roots = require_allowed_roots(allowed_roots, context=context)
    candidate = validate_allowed_path(path, roots)
    return hash_file(candidate, allowed_roots=roots)


def _tagged_location(value: Mapping[str, Any]) -> str | Path | None:
    """Return a tagged filesystem location, if one was explicitly supplied."""

    if "uri" in value:
        return value["uri"]
    if "location" in value:
        return value["location"]
    return None


def _manifest_hash(item: Any, *, allowed_roots, context: str) -> str:
    if isinstance(item, DataAssetRef):
        return _manifest_path_hash(item.uri, allowed_roots=allowed_roots, context=context)
    if isinstance(item, OperationResultRef):
        return _manifest_path_hash(item.location, allowed_roots=allowed_roots, context=context)
    if isinstance(item, Path):
        return _manifest_path_hash(item, allowed_roots=allowed_roots, context=context)
    if isinstance(item, Mapping):
        location = _tagged_location(item)
        if location is not None:
            return _manifest_path_hash(location, allowed_roots=allowed_roots, context=context)
    # Plain strings are values, even when they happen to resemble filenames.
    return hash_value(item)


def _rows(data: Any) -> list[dict[str, Any]] | None:
    if isinstance(data, list) and all(isinstance(item, Mapping) for item in data):
        return [dict(item) for item in data]
    if isinstance(data, tuple) and all(isinstance(item, Mapping) for item in data):
        return [dict(item) for item in data]
    return None


def _context_read_roots(context: RunContext) -> tuple[str, ...]:
    return tuple(str(root) for root in context.read_roots)


def _context_output_path(context: RunContext, path: str | Path) -> Path:
    # Artifact outputs are products, whether callers spell the target
    # relative to that subtree or provide its absolute path.
    return context.resolve_product_path(Path(path).expanduser())


def _context_manifest_item(item: Any, context: RunContext, *, output: bool) -> Any:
    """Resolve typed manifest locations before hashing them."""

    if isinstance(item, DataAssetRef):
        path = context.resolve_run_path(item.uri) if output else context.resolve_input(item.uri)
        return DataAssetRef(uri=str(path), format=item.format, content_hash=item.content_hash, size_bytes=item.size_bytes, metadata=item.metadata)
    if isinstance(item, OperationResultRef):
        return OperationResultRef(location=str(context.resolve_run_path(item.location)), content_hash=item.content_hash, format=item.format, rows=item.rows, metadata=item.metadata)
    if isinstance(item, Path):
        return context.resolve_run_path(item) if output else context.resolve_input(item)
    if isinstance(item, Mapping):
        value = dict(item)
        if "uri" in value:
            value["uri"] = str(context.resolve_input(value["uri"]))
        if "location" in value:
            value["location"] = str(context.resolve_run_path(value["location"]))
        return value
    return item


def write_artifact(
    data: Any,
    path: str | Path,
    *,
    format: str | None = None,
    source_refs: Iterable[DataAssetRef | str] = (),
    operation_spec: OperationSpec | Mapping[str, Any] | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    context: RunContext | None = None,
) -> OperationResultRef:
    """Write JSON/CSV/Parquet deterministically and return a hashed result ref."""

    if context is not None:
        destination = _context_output_path(context, path)
        read_roots = _context_read_roots(context)
        output_roots = (str(context.run_root),)
    else:
        destination = Path(path).expanduser().resolve(strict=False)
        if allowed_roots is not None:
            destination = validate_allowed_path(destination, allowed_roots)
        read_roots = allowed_roots
        output_roots = allowed_roots
    source_hashes = []
    for source in source_refs:
        if isinstance(source, Mapping) and "uri" in source:
            source = DataAssetRef.from_dict(source)
        if isinstance(source, DataAssetRef):
            # Validate and re-hash descriptors before creating any output so a
            # rejected input cannot leave a misleading derived artifact.
            source_path = context.resolve_input(source.uri) if context is not None else source.uri
            current_hash = hash_file(source_path, allowed_roots=read_roots)
            if source.content_hash and current_hash != source.content_hash:
                raise ValueError(f"source changed after registration: {source.uri}")
            source_hashes.append(current_hash)
        else:
            source_path = context.resolve_input(source) if context is not None else source
            source_hashes.append(hash_file(source_path, allowed_roots=read_roots))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fmt = (format or destination.suffix.lstrip(".") or "json").lower()
    if fmt in {"ndjson", "jsonl"}:
        records = _rows(data)
        with destination.open("w", encoding="utf-8", newline="\n") as stream:
            for row in records if records is not None else [data]:
                stream.write(json.dumps(_jsonable(row), sort_keys=True, separators=(",", ":"), default=str) + "\n")
    elif fmt == "json":
        with destination.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(_jsonable(data), stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
            stream.write("\n")
    elif fmt in {"csv", "tsv"}:
        records = _rows(data)
        if records is None:
            raise TypeError("CSV/TSV artifacts require a sequence of mappings")
        columns: list[str] = []
        for row in records:
            for key in row:
                if str(key) not in columns:
                    columns.append(str(key))
        with destination.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, delimiter="\t" if fmt == "tsv" else ",", extrasaction="ignore")
            writer.writeheader()
            writer.writerows({key: _jsonable(row.get(key)) for key in columns} for row in records)
    elif fmt == "parquet":
        records = _rows(data)
        if records is None:
            raise TypeError("Parquet artifacts require a sequence of mappings")
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Parquet support requires the optional 'pyarrow' dependency") from exc
        pq.write_table(pa.Table.from_pylist(records), destination)
    else:
        raise ValueError(f"unsupported artifact format: {fmt}")
    output_hash = hash_file(destination, allowed_roots=output_roots)
    metadata = {
        "source_hashes": source_hashes,
        "operation_spec_hash": operation_spec.spec_hash if isinstance(operation_spec, OperationSpec) else (OperationSpec.from_dict(operation_spec).spec_hash if operation_spec else None),
        "raw_source_unchanged": True,
    }
    return OperationResultRef(location=str(destination), content_hash=output_hash, format=fmt, rows=len(_rows(data) or []), metadata=metadata)


def build_manifest(
    *,
    capability_id: str,
    operation_spec: OperationSpec | Mapping[str, Any],
    inputs: Iterable[DataAssetRef | OperationResultRef | Path | Mapping[str, Any] | str] = (),
    outputs: Iterable[OperationResultRef | Path | Mapping[str, Any] | str] = (),
    core_version: str = "0.1.0",
    metadata: Mapping[str, Any] | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    context: RunContext | None = None,
) -> dict[str, Any]:
    spec = operation_spec if isinstance(operation_spec, OperationSpec) else OperationSpec.from_dict(operation_spec)
    input_hashes: list[str] = []
    input_refs: list[Any] = []
    for item in inputs:
        original_item = item
        if context is not None:
            item = _context_manifest_item(item, context, output=False)
        if isinstance(item, (DataAssetRef, OperationResultRef)):
            input_refs.append(item.to_dict())
            roots = require_allowed_roots(_context_read_roots(context) if context is not None else allowed_roots, context="manifest input hashing")
            input_hashes.append(_manifest_hash(item, allowed_roots=roots, context="manifest input hashing"))
        else:
            input_refs.append(dict(item) if isinstance(item, Mapping) else str(original_item))
            input_hashes.append(_manifest_hash(item, allowed_roots=_context_read_roots(context) if context is not None else allowed_roots, context="manifest input hashing"))
    output_refs: list[Any] = []
    output_hashes: list[str] = []
    for item in outputs:
        original_item = item
        if context is not None:
            item = _context_manifest_item(item, context, output=True)
        if isinstance(item, OperationResultRef):
            output_refs.append(item.to_dict())
            roots = require_allowed_roots((str(context.run_root),) if context is not None else allowed_roots, context="manifest output hashing")
            output_hashes.append(_manifest_hash(item, allowed_roots=roots, context="manifest output hashing"))
        else:
            output_refs.append(dict(item) if isinstance(item, Mapping) else str(original_item))
            output_hashes.append(_manifest_hash(item, allowed_roots=(str(context.run_root),) if context is not None else allowed_roots, context="manifest output hashing"))
    return {
        "manifest_version": "1",
        "core_version": core_version,
        "capability_id": capability_id,
        "spec": spec.to_dict(),
        "spec_hash": spec.spec_hash,
        "input_refs": input_refs,
        "input_hashes": input_hashes,
        "output_refs": output_refs,
        "output_hashes": output_hashes,
        "metadata": dict(metadata or {}),
    }


def write_manifest(path: str | Path, manifest: Mapping[str, Any] | None = None, *, allowed_roots: Iterable[str | Path] | None = None, context: RunContext | None = None, **kwargs: Any) -> dict[str, Any]:
    if manifest is None:
        manifest = build_manifest(allowed_roots=allowed_roots, context=context, **kwargs)
    roots = require_allowed_roots((str(context.run_root),) if context is not None else allowed_roots, context="manifest write")
    destination = context.resolve_run_path(path) if context is not None else Path(path).expanduser().resolve(strict=False)
    destination = validate_allowed_path(destination, roots)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(_jsonable(dict(manifest)), stream, sort_keys=True, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")
    return dict(manifest)


write = write_artifact
manifest = build_manifest

__all__ = ["build_manifest", "hash_value", "manifest", "write", "write_artifact", "write_manifest"]
