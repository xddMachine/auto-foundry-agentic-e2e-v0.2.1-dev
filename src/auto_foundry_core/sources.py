"""Read-only local source registration, discovery, previews, and bounded reads."""

from __future__ import annotations

import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .contracts import DataAssetRef, DocumentRef, TableRef
from .workspace import validate_allowed_path


TABULAR_FORMATS = frozenset({"csv", "tsv", "json", "jsonl", "ndjson", "xlsx", "xls", "parquet"})
TEXT_FORMATS = frozenset({"txt", "md", "markdown", "rst", "html", "htm", "xml", "log"})


def hash_file(
    path: str | Path,
    *,
    chunk_size: int = 1024 * 1024,
    allowed_roots: Iterable[str | Path] | None = None,
) -> str:
    """Return a stable SHA-256 hash without loading the whole file."""

    path = _bounded(path, allowed_roots)
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _format(path: Path, explicit: str | None = None) -> str:
    value = explicit or path.suffix.lstrip(".") or "binary"
    value = value.lower().lstrip(".")
    if value == "ndjson":
        return "jsonl"
    return value


def _bounded(path: str | Path, allowed_roots: Iterable[str | Path] | None) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    if allowed_roots is not None:
        candidate = validate_allowed_path(candidate, allowed_roots)
    return candidate


def register_source(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
    format: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DataAssetRef:
    """Register a local source without changing it."""

    candidate = _bounded(path, allowed_roots)
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(candidate)
    return DataAssetRef(
        uri=str(candidate),
        format=_format(candidate, format),
        content_hash=hash_file(candidate, allowed_roots=allowed_roots),
        size_bytes=candidate.stat().st_size,
        metadata={"read_only": True, **dict(metadata or {})},
    )


def register_document(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
    mime_type: str | None = None,
    title: str | None = None,
) -> DocumentRef:
    asset = register_source(path, allowed_roots=allowed_roots)
    return DocumentRef(asset=asset, title=title or Path(asset.uri).name, mime_type=mime_type)


def _asset(source: DataAssetRef | str | Path, allowed_roots: Iterable[str | Path] | None = None) -> DataAssetRef:
    if isinstance(source, DataAssetRef):
        path = _bounded(source.uri, allowed_roots)
        if source.content_hash and path.is_file() and hash_file(path, allowed_roots=allowed_roots) != source.content_hash:
            raise ValueError(f"source changed after registration: {path}")
        # Re-hash only when caller passed a descriptor that does not carry one.
        if source.content_hash:
            return source
        return register_source(path, allowed_roots=allowed_roots, format=source.format, metadata=source.metadata)
    return register_source(source, allowed_roots=allowed_roots)


def _openpyxl():
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("Excel support requires the optional 'openpyxl' dependency") from exc
    return openpyxl


def _pyarrow():
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("Parquet support requires the optional 'pyarrow' dependency") from exc
    return pq


def discover(
    source: DataAssetRef | str | Path,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
) -> list[TableRef | DocumentRef]:
    """Discover logical tables/sheets without reading unbounded data."""

    asset = _asset(source, allowed_roots)
    path = Path(asset.uri)
    fmt = asset.format or _format(path)
    if fmt in {"xlsx", "xls"}:
        workbook = _openpyxl().load_workbook(path, read_only=True, data_only=True)
        try:
            return [TableRef(asset=asset, name=str(name), kind="sheet") for name in workbook.sheetnames]
        finally:
            workbook.close()
    if fmt in TABULAR_FORMATS:
        return [TableRef(asset=asset, name=path.stem, kind="table")]
    return [DocumentRef(asset=asset, title=path.name)]


def _rows_csv(path: Path, *, delimiter: str = ",") -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, delimiter=delimiter):
            yield dict(row)


def _rows_json(path: Path, fmt: str) -> Iterator[dict[str, Any]]:
    if fmt == "jsonl":
        with path.open("r", encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                yield value if isinstance(value, dict) else {"value": value, "_line": line_number}
        return
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if isinstance(value, dict):
        # A conventional records container is convenient, while preserving a
        # scalar/object document as a single row.
        records = value.get("records", value.get("data"))
        if isinstance(records, list):
            value = records
        else:
            value = [value]
    if not isinstance(value, list):
        value = [value]
    for item in value:
        yield item if isinstance(item, dict) else {"value": item}


def _rows_excel(path: Path, sheet: str | None = None) -> Iterator[dict[str, Any]]:
    workbook = _openpyxl().load_workbook(path, read_only=True, data_only=True)
    try:
        name = sheet or workbook.sheetnames[0]
        if name not in workbook.sheetnames:
            raise KeyError(f"unknown worksheet: {name}")
        values = workbook[name].iter_rows(values_only=True)
        try:
            headers = [str(v) if v is not None else f"column_{i + 1}" for i, v in enumerate(next(values))]
        except StopIteration:
            return
        for row in values:
            yield {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
    finally:
        workbook.close()


def _rows_parquet(path: Path) -> Iterator[dict[str, Any]]:
    table = _pyarrow().read_table(path)
    yield from table.to_pylist()


def read_rows(
    source: DataAssetRef | TableRef | str | Path,
    *,
    table: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    allowed_roots: Iterable[str | Path] | None = None,
) -> list[dict[str, Any]]:
    """Read at most ``limit`` records, materializing only the bounded slice."""

    if limit is not None and limit < 0:
        raise ValueError("limit cannot be negative")
    if offset < 0:
        raise ValueError("offset cannot be negative")
    if isinstance(source, TableRef):
        sheet = table or source.name
        source = source.asset
        table = sheet
    asset = _asset(source, allowed_roots)
    path = Path(asset.uri)
    fmt = asset.format or _format(path)
    if fmt == "csv":
        rows = _rows_csv(path)
    elif fmt == "tsv":
        rows = _rows_csv(path, delimiter="\t")
    elif fmt in {"json", "jsonl"}:
        rows = _rows_json(path, fmt)
    elif fmt in {"xlsx", "xls"}:
        rows = _rows_excel(path, table)
    elif fmt == "parquet":
        rows = _rows_parquet(path)
    elif fmt in TEXT_FORMATS:
        rows = ({"text": line.rstrip("\n")} for line in path.open("r", encoding="utf-8", errors="replace"))
    else:
        raise ValueError(f"unsupported source format: {fmt}")
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index < offset:
            continue
        output.append(dict(row))
        if limit is not None and len(output) >= limit:
            break
    return output


def preview(
    source: DataAssetRef | TableRef | str | Path,
    *,
    limit: int = 20,
    allowed_roots: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Return a bounded, JSON-serializable source preview."""

    if limit < 0:
        raise ValueError("limit cannot be negative")
    asset = source.asset if isinstance(source, TableRef) else source
    if isinstance(asset, DataAssetRef):
        ref = _asset(asset, allowed_roots)
    else:
        ref = _asset(asset, allowed_roots)
    rows = read_rows(source, limit=limit, allowed_roots=allowed_roots)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return {
        "source": ref.to_dict(),
        "format": ref.format,
        "columns": columns,
        "sample": rows,
        "bounded": True,
    }


register = register_source
read = read_rows
source_hash = hash_file

__all__ = ["TABULAR_FORMATS", "discover", "hash_file", "preview", "read", "read_rows", "register", "register_document", "register_source", "source_hash"]
