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
from .workspace import require_allowed_roots, validate_allowed_path


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


def _looks_like_path(value: str) -> bool:
    """Recognize path-shaped strings without probing the filesystem.

    Manifest construction is an execution/public boundary.  It must validate
    a declared root before calling ``Path.is_file`` or hashing a path, while
    still allowing simple inline marker strings to be hashed as values.
    """

    text = value.strip()
    if not text:
        return False
    candidate = Path(text).expanduser()
    return (
        candidate.is_absolute()
        or text.startswith((".", "~"))
        or "/" in text
        or "\\" in text
        or bool(candidate.suffix)
    )


def _manifest_string_hash(value: str, *, allowed_roots, context: str) -> str:
    if not _looks_like_path(value):
        return hash_value(value)
    roots = require_allowed_roots(allowed_roots, context=context)
    candidate = validate_allowed_path(value, roots)
    if candidate.is_file():
        return hash_file(candidate, allowed_roots=roots)
    return hash_value(value)


def _rows(data: Any) -> list[dict[str, Any]] | None:
    if isinstance(data, list) and all(isinstance(item, Mapping) for item in data):
        return [dict(item) for item in data]
    if isinstance(data, tuple) and all(isinstance(item, Mapping) for item in data):
        return [dict(item) for item in data]
    return None


def write_artifact(
    data: Any,
    path: str | Path,
    *,
    format: str | None = None,
    source_refs: Iterable[DataAssetRef | str] = (),
    operation_spec: OperationSpec | Mapping[str, Any] | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> OperationResultRef:
    """Write JSON/CSV/Parquet deterministically and return a hashed result ref."""

    destination = Path(path).expanduser().resolve(strict=False)
    if allowed_roots is not None:
        destination = validate_allowed_path(destination, allowed_roots)
    source_hashes = []
    for source in source_refs:
        if isinstance(source, Mapping) and "uri" in source:
            source = DataAssetRef.from_dict(source)
        if isinstance(source, DataAssetRef):
            # Validate and re-hash descriptors before creating any output so a
            # rejected input cannot leave a misleading derived artifact.
            current_hash = hash_file(source.uri, allowed_roots=allowed_roots)
            if source.content_hash and current_hash != source.content_hash:
                raise ValueError(f"source changed after registration: {source.uri}")
            source_hashes.append(current_hash)
        else:
            source_hashes.append(hash_file(source, allowed_roots=allowed_roots))
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
    output_hash = hash_file(destination, allowed_roots=allowed_roots)
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
    inputs: Iterable[DataAssetRef | OperationResultRef | str] = (),
    outputs: Iterable[OperationResultRef | str] = (),
    core_version: str = "0.1.0",
    metadata: Mapping[str, Any] | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    spec = operation_spec if isinstance(operation_spec, OperationSpec) else OperationSpec.from_dict(operation_spec)
    input_hashes: list[str] = []
    input_refs: list[Any] = []
    for item in inputs:
        if isinstance(item, (DataAssetRef, OperationResultRef)):
            input_refs.append(item.to_dict())
            location = item.uri if isinstance(item, DataAssetRef) else item.location
            roots = require_allowed_roots(allowed_roots, context="manifest input hashing")
            input_hashes.append(hash_file(location, allowed_roots=roots))
        else:
            input_refs.append(str(item))
            input_hashes.append(_manifest_string_hash(str(item), allowed_roots=allowed_roots, context="manifest input hashing"))
    output_refs: list[Any] = []
    output_hashes: list[str] = []
    for item in outputs:
        if isinstance(item, OperationResultRef):
            output_refs.append(item.to_dict())
            roots = require_allowed_roots(allowed_roots, context="manifest output hashing")
            output_hashes.append(hash_file(item.location, allowed_roots=roots))
        else:
            output_refs.append(str(item))
            output_hashes.append(_manifest_string_hash(str(item), allowed_roots=allowed_roots, context="manifest output hashing"))
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


def write_manifest(path: str | Path, manifest: Mapping[str, Any] | None = None, *, allowed_roots: Iterable[str | Path] | None = None, **kwargs: Any) -> dict[str, Any]:
    if manifest is None:
        manifest = build_manifest(allowed_roots=allowed_roots, **kwargs)
    roots = require_allowed_roots(allowed_roots, context="manifest write")
    destination = Path(path).expanduser().resolve(strict=False)
    destination = validate_allowed_path(destination, roots)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(_jsonable(dict(manifest)), stream, sort_keys=True, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")
    return dict(manifest)


write = write_artifact
manifest = build_manifest

__all__ = ["build_manifest", "hash_value", "manifest", "write", "write_artifact", "write_manifest"]
