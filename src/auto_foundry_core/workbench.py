"""Read-only data-room access and run-local prepared assets.

The workbench is intentionally a small boundary around :mod:`zipfile`.  It
does not extract an archive, build a dataframe abstraction, or infer business
meaning.  It gives an analyst deterministic physical metadata, bounded rows,
and document excerpts while keeping all derived state inside one
``RunContext``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence
import zipfile

from .contracts import DataAssetRef, PreparedAssetDescriptor
from .enterprise_model import LivingEnterpriseModel
from .telemetry import TelemetryRecorder
from .workspace import RunContext


DEFAULT_JSON_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_MEMBER_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_TOTAL_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_COMPRESSION_RATIO = 1000.0
DEFAULT_CATALOG_ROWS = 100_000
DEFAULT_XLSX_MEMBER_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_XLSX_TOTAL_MAX_BYTES = 512 * 1024 * 1024
# XLSX XML parts can compress substantially more than ordinary text files;
# retain a high but finite bound while total/member caps remain authoritative.
DEFAULT_XLSX_COMPRESSION_RATIO = 10_000.0
DEFAULT_XLSX_ENTRY_LIMIT = 4096
DEFAULT_PREPARED_MAX_BYTES = 64 * 1024 * 1024

TABULAR_FORMATS = frozenset({"csv", "tsv", "json", "jsonl", "ndjson", "xlsx"})
DOCUMENT_FORMATS = frozenset(
    {
        "txt",
        "text",
        "md",
        "markdown",
        "rst",
        "html",
        "htm",
        "xml",
        "log",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "sql",
        "py",
        "sh",
        "pdf",
    }
)
UNSUPPORTED_FORMATS = frozenset({"xls", "parquet", "feather", "avro", "doc", "docx", "ppt", "pptx"})
_RESERVED_LINEAGE_KEYS = frozenset({"archive", "members", "transformations"})


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (Path, PurePosixPath)):
        return str(value)
    return value


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _jsonable(item) for key, item in (value or {}).items()})


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_id(value: str, label: str = "identifier") -> str:
    value = str(value).strip()
    if not value or value in {".", ".."} or Path(value).name != value or "\\" in value:
        raise ValueError(f"{label} must be a simple path component")
    return value


def _format_for_name(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower().lstrip(".")
    if suffix == "ndjson":
        return "jsonl"
    if suffix:
        return suffix
    return "document"


def _member_kind(fmt: str) -> str:
    if fmt in TABULAR_FORMATS:
        return "table"
    if fmt in DOCUMENT_FORMATS or fmt == "document":
        return "document"
    return "unsupported"


def _validate_member_name(name: str) -> None:
    if not name or "\x00" in name or name.startswith("/") or name.startswith("\\") or "\\" in name:
        raise ValueError(f"unsafe ZIP member name: {name!r}")
    # PurePosixPath keeps archive names in their own namespace.  Rejecting
    # dot segments as well as parent traversal prevents ambiguous references.
    parts = PurePosixPath(name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe ZIP member name: {name!r}")
    if ":" in parts[0] or PurePosixPath(name).is_absolute():
        raise ValueError(f"unsafe ZIP member name: {name!r}")


def _is_ignored_archive_metadata(name: str) -> bool:
    """Return whether *name* is inert macOS archive metadata.

    ``__MACOSX`` metadata is recognized only below that directory component;
    an ordinary member named ``__MACOSX`` remains part of the data room.  A
    ``.DS_Store`` file is ignored at any depth.  Callers must perform the
    normal member safety checks before using this predicate.
    """

    path = PurePosixPath(name)
    return path.name == ".DS_Store" or "__MACOSX" in path.parts[:-1]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _check_limits(info: zipfile.ZipInfo, *, max_member_bytes: int, max_compression_ratio: float) -> None:
    if info.file_size < 0 or info.compress_size < 0:
        raise ValueError(f"invalid ZIP member size: {info.filename}")
    if info.file_size > max_member_bytes:
        raise ValueError(
            f"ZIP member exceeds max_member_bytes: {info.filename} ({info.file_size} > {max_member_bytes})"
        )
    if info.file_size and info.compress_size == 0:
        raise ValueError(f"ZIP member has an invalid compression ratio: {info.filename}")
    if info.compress_size and info.file_size / info.compress_size > max_compression_ratio:
        raise ValueError(f"ZIP member exceeds max_compression_ratio: {info.filename}")


def _open_xlsx_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid XLSX ZIP container") from exc


def _validate_xlsx_entry_name(info: zipfile.ZipInfo, seen_names: set[str]) -> None:
    _validate_member_name(info.filename)
    if info.filename in seen_names:
        raise ValueError(f"duplicate XLSX member name: {info.filename}")
    seen_names.add(info.filename)
    if _is_symlink(info):
        raise ValueError(f"XLSX symlink entries are not supported: {info.filename}")


def _count_xlsx_entry(info: zipfile.ZipInfo, current: int, maximum: int) -> int:
    count = current + 1
    if count > maximum:
        raise ValueError(f"XLSX exceeds max_xlsx_entries: {count} > {maximum}")
    return count


def _validate_xlsx_file_limits(
    info: zipfile.ZipInfo,
    *,
    max_member_bytes: int,
    max_compression_ratio: float,
) -> None:
    if info.flag_bits & 0x1:
        raise ValueError(f"encrypted XLSX members are not supported: {info.filename}")
    _check_limits(
        info,
        max_member_bytes=max_member_bytes,
        max_compression_ratio=max_compression_ratio,
    )


def _add_xlsx_size(info: zipfile.ZipInfo, current: int, maximum: int) -> int:
    total = current + info.file_size
    if total > maximum:
        raise ValueError(f"XLSX exceeds max_xlsx_total_bytes: {total} > {maximum}")
    return total


def _preflight_xlsx_bytes(
    data: bytes,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    max_compression_ratio: float,
    max_entries: int,
) -> None:
    """Validate the nested ZIP container before handing bytes to openpyxl."""

    if max_entries < 0:
        raise ValueError("max_xlsx_entries cannot be negative")
    nested = _open_xlsx_zip(data)
    total_size = 0
    entry_count = 0
    seen_names: set[str] = set()
    try:
        for info in nested.infolist():
            _validate_xlsx_entry_name(info, seen_names)
            entry_count = _count_xlsx_entry(info, entry_count, max_entries)
            if info.is_dir():
                continue
            _validate_xlsx_file_limits(
                info,
                max_member_bytes=max_member_bytes,
                max_compression_ratio=max_compression_ratio,
            )
            total_size = _add_xlsx_size(info, total_size, max_total_bytes)
    finally:
        nested.close()


def _physical_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _cell_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if value is None:
        return ""
    return value


@dataclass(frozen=True)
class DataRoomMember:
    """A validated physical member of the supplied archive."""

    path: str
    format: str
    kind: str
    size_bytes: int
    compressed_size_bytes: int
    content_hash: str

    @property
    def name(self) -> str:
        return self.path

    @property
    def compressed_size(self) -> int:
        """Compatibility-friendly short alias for the physical ZIP size."""

        return self.compressed_size_bytes

    @property
    def member_id(self) -> str:
        return self.path

    @property
    def sha256(self) -> str:
        return self.content_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "compressed_size_bytes": self.compressed_size_bytes,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DataRoomMember":
        return cls(
            path=str(data["path"]),
            format=str(data["format"]),
            kind=str(data["kind"]),
            size_bytes=int(data["size_bytes"]),
            compressed_size_bytes=int(data["compressed_size_bytes"]),
            content_hash=str(data["content_hash"]),
        )


@dataclass(frozen=True)
class DataRoomCatalogEntry:
    """Bounded physical metadata for one member/table/sheet."""

    member: DataRoomMember
    table_name: str | None = None
    sheet_name: str | None = None
    columns: tuple[str, ...] = ()
    sample_values: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    row_count: int | None = None
    row_count_exact: bool = False
    row_count_lower_bound: int | None = None
    sample_rows: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(str(value) for value in self.columns))
        object.__setattr__(
            self,
            "sample_values",
            MappingProxyType(
                {
                    str(key): tuple(_jsonable(value) for value in values)
                    for key, values in self.sample_values.items()
                }
            ),
        )
        object.__setattr__(self, "sample_rows", tuple(_freeze_mapping(row) for row in self.sample_rows))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    @property
    def path(self) -> str:
        return self.member.path

    @property
    def member_path(self) -> str:
        return self.member.path

    @property
    def table(self) -> str | None:
        return self.table_name

    @property
    def sheet(self) -> str | None:
        return self.sheet_name

    @property
    def format(self) -> str:
        return self.member.format

    @property
    def kind(self) -> str:
        return self.member.kind

    @property
    def content_hash(self) -> str:
        return self.member.content_hash

    @property
    def size_bytes(self) -> int:
        return self.member.size_bytes

    @property
    def compressed_size_bytes(self) -> int:
        return self.member.compressed_size_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "member": self.member.to_dict(),
            "table_name": self.table_name,
            "sheet_name": self.sheet_name,
            "columns": list(self.columns),
            "sample_values": {key: list(values) for key, values in self.sample_values.items()},
            "row_count": self.row_count,
            "row_count_exact": self.row_count_exact,
            "row_count_lower_bound": self.row_count_lower_bound,
            "sample_rows": [_jsonable(row) for row in self.sample_rows],
            "metadata": _jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DataRoomCatalogEntry":
        return cls(
            member=DataRoomMember.from_dict(data["member"]),
            table_name=data.get("table_name"),
            sheet_name=data.get("sheet_name"),
            columns=tuple(data.get("columns", ())),
            sample_values={key: tuple(values) for key, values in dict(data.get("sample_values", {})).items()},
            row_count=data.get("row_count"),
            row_count_exact=bool(data.get("row_count_exact", False)),
            row_count_lower_bound=data.get("row_count_lower_bound"),
            sample_rows=tuple(data.get("sample_rows", ())),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class PreparedAsset:
    """A loadable prepared asset and its integrity descriptor."""

    descriptor: PreparedAssetDescriptor
    rows: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(_freeze_mapping(row) for row in self.rows))

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> Mapping[str, Any]:
        return self.rows[index]

    @property
    def prepared_asset_id(self) -> str:
        return self.descriptor.prepared_asset_id


@dataclass(frozen=True)
class _ArchiveLimits:
    max_member_bytes: int = DEFAULT_MEMBER_MAX_BYTES
    max_total_bytes: int = DEFAULT_TOTAL_MAX_BYTES
    max_compression_ratio: float = DEFAULT_COMPRESSION_RATIO
    max_json_bytes: int = DEFAULT_JSON_MAX_BYTES
    max_catalog_rows: int = DEFAULT_CATALOG_ROWS
    max_xlsx_member_bytes: int = DEFAULT_XLSX_MEMBER_MAX_BYTES
    max_xlsx_total_bytes: int = DEFAULT_XLSX_TOTAL_MAX_BYTES
    max_xlsx_compression_ratio: float = DEFAULT_XLSX_COMPRESSION_RATIO
    max_xlsx_entries: int = DEFAULT_XLSX_ENTRY_LIMIT


def _archive_limits(
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    max_compression_ratio: float,
    max_json_bytes: int,
    max_catalog_rows: int,
    max_xlsx_member_bytes: int,
    max_xlsx_total_bytes: int,
    max_xlsx_compression_ratio: float,
    max_xlsx_entries: int,
) -> _ArchiveLimits:
    nonnegative = (
        max_member_bytes,
        max_total_bytes,
        max_json_bytes,
        max_catalog_rows,
        max_xlsx_member_bytes,
        max_xlsx_total_bytes,
        max_xlsx_entries,
    )
    if any(value < 0 for value in nonnegative):
        raise ValueError("workbench bounds cannot be negative")
    if max_compression_ratio <= 0 or max_xlsx_compression_ratio <= 0:
        raise ValueError("compression ratios must be positive")
    return _ArchiveLimits(
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
        max_compression_ratio=max_compression_ratio,
        max_json_bytes=max_json_bytes,
        max_catalog_rows=max_catalog_rows,
        max_xlsx_member_bytes=max_xlsx_member_bytes,
        max_xlsx_total_bytes=max_xlsx_total_bytes,
        max_xlsx_compression_ratio=max_xlsx_compression_ratio,
        max_xlsx_entries=max_xlsx_entries,
    )


def _inspect_archive_member(
    source: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    limits: _ArchiveLimits,
    seen_names: set[str],
) -> DataRoomMember | None:
    _validate_member_name(info.filename)
    if info.filename in seen_names:
        raise ValueError(f"duplicate ZIP member name: {info.filename}")
    seen_names.add(info.filename)
    if _is_symlink(info):
        raise ValueError(f"ZIP symlink entries are not supported: {info.filename}")
    if info.is_dir():
        return None
    if info.flag_bits & 0x1:
        raise ValueError(f"encrypted ZIP members are not supported: {info.filename}")
    _check_limits(
        info,
        max_member_bytes=limits.max_member_bytes,
        max_compression_ratio=limits.max_compression_ratio,
    )
    if _is_ignored_archive_metadata(info.filename):
        return None
    fmt = _format_for_name(info.filename)
    if _member_kind(fmt) == "unsupported":
        raise ValueError(f"unsupported ZIP member format: {info.filename} ({fmt})")
    digest = hashlib.sha256()
    xlsx_bytes = bytearray() if fmt == "xlsx" else None
    with source.open(info, "r") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if xlsx_bytes is not None:
                xlsx_bytes.extend(chunk)
    if xlsx_bytes is not None:
        _preflight_xlsx_bytes(
            bytes(xlsx_bytes),
            max_member_bytes=limits.max_xlsx_member_bytes,
            max_total_bytes=limits.max_xlsx_total_bytes,
            max_compression_ratio=limits.max_xlsx_compression_ratio,
            max_entries=limits.max_xlsx_entries,
        )
    return DataRoomMember(
        path=info.filename,
        format=fmt,
        kind=_member_kind(fmt),
        size_bytes=info.file_size,
        compressed_size_bytes=info.compress_size,
        content_hash=digest.hexdigest(),
    )


def _inventory_archive(archive_path: Path, *, limits: _ArchiveLimits) -> tuple[DataRoomMember, ...]:
    members: list[DataRoomMember] = []
    seen_names: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(archive_path, "r") as source:
            for info in source.infolist():
                member = _inspect_archive_member(source, info, limits=limits, seen_names=seen_names)
                if member is None:
                    # Ignored metadata is still part of the archive's bounded
                    # physical footprint, while directory accounting remains
                    # unchanged from the pre-filter inventory.
                    if not info.is_dir() and _is_ignored_archive_metadata(info.filename):
                        total_size += info.file_size
                        if total_size > limits.max_total_bytes:
                            raise ValueError(f"ZIP exceeds max_total_bytes: {total_size} > {limits.max_total_bytes}")
                    continue
                total_size += member.size_bytes
                if total_size > limits.max_total_bytes:
                    raise ValueError(f"ZIP exceeds max_total_bytes: {total_size} > {limits.max_total_bytes}")
                members.append(member)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid ZIP archive: {archive_path}") from exc
    return tuple(sorted(members, key=lambda item: item.path))


class DataRoom:
    """Read-only access to one validated ZIP archive."""

    def __init__(
        self,
        context: RunContext,
        archive_path: Path,
        archive_ref: DataAssetRef,
        members: tuple[DataRoomMember, ...],
        *,
        limits: _ArchiveLimits,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        self.context = context
        self.archive_path = archive_path
        self.archive_ref = archive_ref
        self._members = members
        self.limits = limits
        self.telemetry = telemetry
        self.catalog_path = context.resolve_run_path("data_room/source_catalog.json")

    @classmethod
    def open(
        cls,
        context: RunContext,
        archive: str | Path | DataAssetRef,
        *,
        telemetry: TelemetryRecorder | None = None,
        max_member_bytes: int = DEFAULT_MEMBER_MAX_BYTES,
        max_total_bytes: int = DEFAULT_TOTAL_MAX_BYTES,
        max_compression_ratio: float = DEFAULT_COMPRESSION_RATIO,
        max_json_bytes: int = DEFAULT_JSON_MAX_BYTES,
        max_catalog_rows: int = DEFAULT_CATALOG_ROWS,
        max_xlsx_member_bytes: int = DEFAULT_XLSX_MEMBER_MAX_BYTES,
        max_xlsx_total_bytes: int = DEFAULT_XLSX_TOTAL_MAX_BYTES,
        max_xlsx_compression_ratio: float = DEFAULT_XLSX_COMPRESSION_RATIO,
        max_xlsx_entries: int = DEFAULT_XLSX_ENTRY_LIMIT,
    ) -> "DataRoom":
        limits = _archive_limits(
            max_member_bytes=max_member_bytes,
            max_total_bytes=max_total_bytes,
            max_compression_ratio=max_compression_ratio,
            max_json_bytes=max_json_bytes,
            max_catalog_rows=max_catalog_rows,
            max_xlsx_member_bytes=max_xlsx_member_bytes,
            max_xlsx_total_bytes=max_xlsx_total_bytes,
            max_xlsx_compression_ratio=max_xlsx_compression_ratio,
            max_xlsx_entries=max_xlsx_entries,
        )
        archive_value = archive
        supplied_hash: str | None = None
        if isinstance(archive_value, DataAssetRef):
            supplied_hash = archive_value.content_hash
            archive_value = archive_value.uri
        archive_path = context.resolve_input(archive_value)
        if not archive_path.exists() or not archive_path.is_file():
            raise FileNotFoundError(archive_path)
        archive_hash = _sha256_file(archive_path)
        if supplied_hash and supplied_hash != archive_hash:
            raise ValueError(f"source changed after registration: {archive_path}")
        members_tuple = _inventory_archive(archive_path, limits=limits)
        room = cls(
            context,
            archive_path,
            DataAssetRef(
                uri=str(archive_path),
                format="zip",
                content_hash=archive_hash,
                size_bytes=archive_path.stat().st_size,
                metadata={"read_only": True},
            ),
            members_tuple,
            limits=limits,
            telemetry=telemetry,
        )
        room._emit(
            "data_room_archive_read",
            bytes_processed=archive_path.stat().st_size,
            facts={"archive_hash": archive_hash, "member_count": len(members_tuple)},
        )
        return room

    def members(self) -> tuple[DataRoomMember, ...]:
        self._check_archive_unchanged()
        return self._members

    def _emit(self, event_type: str, *, bytes_processed: int | None = None, facts: Mapping[str, Any] | None = None) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry.record(
                event_type,
                bytes_processed=bytes_processed,
                facts=dict(facts or {}),
            )
        except Exception:
            # Telemetry is observational only.
            pass

    def _check_archive_unchanged(self) -> None:
        current = _sha256_file(self.archive_path)
        if current != self.archive_ref.content_hash:
            raise ValueError(f"archive changed after registration: {self.archive_path}")

    def _resolve_member(self, member: DataRoomMember | DataRoomCatalogEntry | str | Path) -> DataRoomMember:
        if isinstance(member, DataRoomCatalogEntry):
            member = member.member
        path = member.path if isinstance(member, DataRoomMember) else str(member)
        for candidate in self._members:
            if candidate.path == path:
                return candidate
        raise KeyError(f"unknown data-room member: {path}")

    def _zip_info(self, member: DataRoomMember) -> zipfile.ZipInfo:
        with zipfile.ZipFile(self.archive_path, "r") as source:
            try:
                info = source.getinfo(member.path)
            except KeyError as exc:
                raise KeyError(f"unknown data-room member: {member.path}") from exc
        _validate_member_name(info.filename)
        if _is_symlink(info) or info.flag_bits & 0x1:
            raise ValueError(f"unsafe ZIP member: {member.path}")
        _check_limits(
            info,
            max_member_bytes=self.limits.max_member_bytes,
            max_compression_ratio=self.limits.max_compression_ratio,
        )
        if info.file_size != member.size_bytes or info.compress_size != member.compressed_size_bytes:
            raise ValueError(f"archive member metadata changed: {member.path}")
        return info

    def _read_member_bytes(
        self,
        member: DataRoomMember,
        *,
        max_bytes: int | None = None,
        allow_truncate: bool = False,
    ) -> bytes:
        self._check_archive_unchanged()
        info = self._zip_info(member)
        cap = self.limits.max_member_bytes if max_bytes is None else max_bytes
        if cap < 0:
            raise ValueError("max_bytes cannot be negative")
        if info.file_size > cap and not allow_truncate:
            raise ValueError(f"member exceeds max_bytes: {member.path} ({info.file_size} > {cap})")
        with zipfile.ZipFile(self.archive_path, "r") as source, source.open(info, "r") as stream:
            data = stream.read(cap + 1 if not allow_truncate else cap)
        if len(data) > cap and not allow_truncate:
            raise ValueError(f"member exceeds max_bytes: {member.path}")
        if not allow_truncate and _sha256_bytes(data) != member.content_hash:
            raise ValueError(f"archive member content changed: {member.path}")
        self._emit(
            "data_room_member_read",
            bytes_processed=len(data),
            facts={"member_path": member.path, "format": member.format},
        )
        return data

    def _iter_csv_rows(
        self,
        member: DataRoomMember,
        *,
        delimiter: str,
        limit: int | None,
        offset: int,
    ) -> list[dict[str, Any]]:
        self._check_archive_unchanged()
        info = self._zip_info(member)
        output: list[dict[str, Any]] = []
        with zipfile.ZipFile(self.archive_path, "r") as source, source.open(info, "r") as raw:
            with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter=delimiter)
                for index, row in enumerate(reader):
                    if index < offset:
                        continue
                    output.append(dict(row))
                    if limit is not None and len(output) >= limit:
                        break
        self._emit(
            "data_room_member_read",
            bytes_processed=member.size_bytes,
            facts={"member_path": member.path, "format": member.format, "rows": len(output)},
        )
        return output

    def _iter_json_rows(
        self,
        member: DataRoomMember,
        *,
        limit: int | None,
        offset: int,
        max_json_bytes: int | None = None,
    ) -> list[dict[str, Any]]:
        if member.format == "json":
            data = self._read_member_bytes(
                member,
                max_bytes=self.limits.max_json_bytes if max_json_bytes is None else max_json_bytes,
            )
            value = json.loads(data.decode("utf-8-sig"))
            if isinstance(value, Mapping):
                records = value.get("records", value.get("data"))
                value = records if isinstance(records, list) else [value]
            elif not isinstance(value, list):
                value = [value]
            rows = [item if isinstance(item, Mapping) else {"value": item} for item in value]
            selected = rows[offset:] if limit is None else rows[offset : offset + limit]
            return [dict(row) for row in selected]

        self._check_archive_unchanged()
        info = self._zip_info(member)
        output: list[dict[str, Any]] = []
        row_index = 0
        with zipfile.ZipFile(self.archive_path, "r") as source, source.open(info, "r") as raw:
            with io.TextIOWrapper(raw, encoding="utf-8-sig") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    row = dict(value) if isinstance(value, Mapping) else {"value": value, "_line": line_number}
                    if row_index < offset:
                        row_index += 1
                        continue
                    output.append(row)
                    row_index += 1
                    if limit is not None and len(output) >= limit:
                        break
        self._emit(
            "data_room_member_read",
            bytes_processed=member.size_bytes,
            facts={"member_path": member.path, "format": member.format, "rows": len(output)},
        )
        return output

    def _xlsx_rows(
        self,
        member: DataRoomMember,
        *,
        sheet: str | None,
        limit: int | None,
        offset: int,
    ) -> list[dict[str, Any]]:
        data = self._read_member_bytes(member)
        _preflight_xlsx_bytes(
            data,
            max_member_bytes=self.limits.max_xlsx_member_bytes,
            max_total_bytes=self.limits.max_xlsx_total_bytes,
            max_compression_ratio=self.limits.max_xlsx_compression_ratio,
            max_entries=self.limits.max_xlsx_entries,
        )
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("XLSX support requires the optional 'openpyxl' dependency") from exc
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            sheet_name = sheet or workbook.sheetnames[0]
            if sheet_name not in workbook.sheetnames:
                raise KeyError(f"unknown worksheet: {sheet_name}")
            values = workbook[sheet_name].iter_rows(values_only=True)
            try:
                headers = [str(value) if value is not None else f"column_{index + 1}" for index, value in enumerate(next(values))]
            except StopIteration:
                return []
            output: list[dict[str, Any]] = []
            for index, row in enumerate(values):
                if index < offset:
                    continue
                output.append({headers[column]: row[column] if column < len(row) else None for column in range(len(headers))})
                if limit is not None and len(output) >= limit:
                    break
            self._emit(
                "data_room_member_read",
                bytes_processed=len(data),
                facts={"member_path": member.path, "format": member.format, "sheet": sheet_name, "rows": len(output)},
            )
            return output
        finally:
            workbook.close()

    def read_rows(
        self,
        member: DataRoomMember | DataRoomCatalogEntry | str | Path,
        *,
        sheet: str | None = None,
        limit: int | None = 1000,
        offset: int = 0,
        max_json_bytes: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if limit == 0:
            return []
        resolved = self._resolve_member(member)
        if isinstance(member, DataRoomCatalogEntry) and sheet is None:
            sheet = member.sheet_name
        if resolved.kind == "unsupported":
            raise ValueError(f"unsupported data-room member format: {resolved.format}")
        if resolved.format == "csv":
            return self._iter_csv_rows(resolved, delimiter=",", limit=limit, offset=offset)
        if resolved.format == "tsv":
            return self._iter_csv_rows(resolved, delimiter="\t", limit=limit, offset=offset)
        if resolved.format in {"json", "jsonl"}:
            if max_json_bytes is not None:
                if max_json_bytes < 0:
                    raise ValueError("max_json_bytes cannot be negative")
            return self._iter_json_rows(
                resolved,
                limit=limit,
                offset=offset,
                max_json_bytes=max_json_bytes,
            )
        if resolved.format == "xlsx":
            return self._xlsx_rows(resolved, sheet=sheet, limit=limit, offset=offset)
        if sheet is not None:
            raise ValueError("sheet is only valid for XLSX members")
        raise ValueError(f"member is a document, not a table: {resolved.path}")

    def document_excerpt(
        self,
        member: DataRoomMember | DataRoomCatalogEntry | str | Path,
        *,
        max_bytes: int = 65536,
    ) -> str:
        if max_bytes < 0:
            raise ValueError("max_bytes cannot be negative")
        resolved = self._resolve_member(member)
        if resolved.kind == "unsupported":
            raise ValueError(f"unsupported data-room member format: {resolved.format}")
        if resolved.kind != "document":
            raise ValueError(f"member is tabular, not a document: {resolved.path}")
        if resolved.format == "pdf":
            raise ValueError(f"PDF document excerpts require custom code: {resolved.path}")
        data = self._read_member_bytes(resolved, max_bytes=max_bytes, allow_truncate=True)
        return data.decode("utf-8-sig", errors="replace")

    def _catalog_for_member(self, member: DataRoomMember, *, sample_rows: int, categorical_limit: int) -> list[DataRoomCatalogEntry]:
        if member.kind == "unsupported":
            raise ValueError(f"unsupported data-room member format: {member.format}")
        if member.kind == "document":
            return self._catalog_document(member, sample_rows=sample_rows, categorical_limit=categorical_limit)
        if member.format == "xlsx":
            return self._catalog_xlsx(member, sample_rows=sample_rows, categorical_limit=categorical_limit)
        if member.format in {"json", "jsonl"}:
            return self._catalog_json(member, sample_rows=sample_rows, categorical_limit=categorical_limit)
        return self._catalog_delimited(member, sample_rows=sample_rows, categorical_limit=categorical_limit)

    def _catalog_document(
        self,
        member: DataRoomMember,
        *,
        sample_rows: int,
        categorical_limit: int,
    ) -> list[DataRoomCatalogEntry]:
        if member.format == "pdf":
            return [
                DataRoomCatalogEntry(
                    member=member,
                    row_count=None,
                    row_count_exact=False,
                    row_count_lower_bound=None,
                    metadata={"extraction": "opaque", "requires_custom_code": True},
                )
            ]
        excerpt = self.document_excerpt(member, max_bytes=min(65536, self.limits.max_member_bytes))
        lines = tuple(excerpt.splitlines()[:sample_rows])
        return [
            DataRoomCatalogEntry(
                member=member,
                columns=("text",),
                sample_values={"text": lines[:categorical_limit]},
                row_count=None,
                row_count_exact=False,
                row_count_lower_bound=None,
                sample_rows=tuple({"text": line} for line in lines),
                metadata={"excerpt_bytes": len(excerpt.encode("utf-8"))},
            )
        ]

    def _catalog_xlsx(
        self,
        member: DataRoomMember,
        *,
        sample_rows: int,
        categorical_limit: int,
    ) -> list[DataRoomCatalogEntry]:
        data = self._read_member_bytes(member)
        _preflight_xlsx_bytes(
            data,
            max_member_bytes=self.limits.max_xlsx_member_bytes,
            max_total_bytes=self.limits.max_xlsx_total_bytes,
            max_compression_ratio=self.limits.max_xlsx_compression_ratio,
            max_entries=self.limits.max_xlsx_entries,
        )
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("XLSX support requires the optional 'openpyxl' dependency") from exc
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        entries: list[DataRoomCatalogEntry] = []
        try:
            for sheet_name in workbook.sheetnames:
                entries.append(
                    self._catalog_xlsx_sheet(
                        member,
                        workbook[sheet_name],
                        sheet_name=sheet_name,
                        sample_rows=sample_rows,
                        categorical_limit=categorical_limit,
                    )
                )
        finally:
            workbook.close()
        self._emit("data_room_member_read", bytes_processed=len(data), facts={"member_path": member.path, "format": member.format, "sheets": len(entries)})
        return entries

    def _catalog_xlsx_sheet(
        self,
        member: DataRoomMember,
        worksheet: Any,
        *,
        sheet_name: str,
        sample_rows: int,
        categorical_limit: int,
    ) -> DataRoomCatalogEntry:
        values = worksheet.iter_rows(values_only=True)
        try:
            headers = [str(value) if value is not None else f"column_{index + 1}" for index, value in enumerate(next(values))]
        except StopIteration:
            headers = []
        rows: list[dict[str, Any]] = []
        count = 0
        exhausted = True
        for row in values:
            count += 1
            if len(rows) < sample_rows:
                rows.append({headers[column]: row[column] if column < len(row) else None for column in range(len(headers))})
            if count >= self.limits.max_catalog_rows:
                try:
                    next(values)
                except StopIteration:
                    pass
                else:
                    exhausted = False
                break
        return self._entry_from_rows(member, rows, headers, sheet_name, count, exhausted, categorical_limit)

    def _catalog_json(
        self,
        member: DataRoomMember,
        *,
        sample_rows: int,
        categorical_limit: int,
    ) -> list[DataRoomCatalogEntry]:
        rows = self._iter_json_rows(member, limit=self.limits.max_catalog_rows + 1, offset=0)
        headers: list[str] = []
        for row in rows[:sample_rows]:
            for key in row:
                if str(key) not in headers:
                    headers.append(str(key))
        exhausted = len(rows) <= self.limits.max_catalog_rows
        count = min(len(rows), self.limits.max_catalog_rows)
        return [self._entry_from_rows(member, rows[:sample_rows], headers, None, count, exhausted, categorical_limit)]

    def _catalog_delimited(
        self,
        member: DataRoomMember,
        *,
        sample_rows: int,
        categorical_limit: int,
    ) -> list[DataRoomCatalogEntry]:
        self._check_archive_unchanged()
        info = self._zip_info(member)
        rows: list[dict[str, Any]] = []
        delimiter = "\t" if member.format == "tsv" else ","
        count = 0
        exhausted = True
        with zipfile.ZipFile(self.archive_path, "r") as source, source.open(info, "r") as raw:
            with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter=delimiter)
                headers = [str(value) for value in (reader.fieldnames or ())]
                for row in reader:
                    count += 1
                    if len(rows) < sample_rows:
                        rows.append(dict(row))
                    if count >= self.limits.max_catalog_rows:
                        try:
                            next(reader)
                        except StopIteration:
                            pass
                        else:
                            exhausted = False
                        break
        self._emit("data_room_member_read", bytes_processed=member.size_bytes, facts={"member_path": member.path, "format": member.format, "rows": count})
        return [self._entry_from_rows(member, rows, headers, None, count, exhausted, categorical_limit)]

    @staticmethod
    def _entry_from_rows(
        member: DataRoomMember,
        rows: Sequence[Mapping[str, Any]],
        headers: Sequence[str],
        sheet_name: str | None,
        count: int,
        exact: bool,
        categorical_limit: int,
    ) -> DataRoomCatalogEntry:
        columns: list[str] = [str(value) for value in headers]
        for row in rows:
            for key in row:
                if str(key) not in columns:
                    columns.append(str(key))
        samples: dict[str, tuple[Any, ...]] = {}
        for column in columns:
            values: list[Any] = []
            for row in rows:
                value = row.get(column)
                if value is None or value == "":
                    continue
                if value not in values and len(values) < categorical_limit:
                    values.append(_jsonable(value))
            samples[column] = tuple(values)
        return DataRoomCatalogEntry(
            member=member,
            table_name=sheet_name or PurePosixPath(member.path).stem,
            sheet_name=sheet_name,
            columns=tuple(columns),
            sample_values=samples,
            row_count=count if exact else None,
            row_count_exact=exact,
            row_count_lower_bound=count,
            sample_rows=tuple(rows),
        )

    def build_catalog(self, *, sample_rows: int = 20, categorical_limit: int = 20) -> tuple[DataRoomCatalogEntry, ...]:
        if sample_rows < 0 or categorical_limit < 0:
            raise ValueError("sample_rows and categorical_limit cannot be negative")
        self._check_archive_unchanged()
        catalog_path = self.catalog_path
        try:
            if catalog_path.is_file():
                payload = json.loads(catalog_path.read_text(encoding="utf-8"))
                if (
                    payload.get("archive_hash") == self.archive_ref.content_hash
                    and payload.get("sample_rows") == sample_rows
                    and payload.get("categorical_limit") == categorical_limit
                ):
                    entries = tuple(DataRoomCatalogEntry.from_dict(value) for value in payload.get("entries", ()))
                    self._emit("data_room_catalog_reused", facts={"entry_count": len(entries), "catalog_path": str(catalog_path)})
                    return entries
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        entries: list[DataRoomCatalogEntry] = []
        for member in self._members:
            entries.extend(self._catalog_for_member(member, sample_rows=sample_rows, categorical_limit=categorical_limit))
        result = tuple(sorted(entries, key=lambda item: (item.path, item.sheet_name or "")))
        payload = {
            "archive": self.archive_ref.to_dict(),
            "archive_hash": self.archive_ref.content_hash,
            "sample_rows": sample_rows,
            "categorical_limit": categorical_limit,
            "entries": [entry.to_dict() for entry in result],
        }
        _atomic_write_json(catalog_path, payload)
        self._emit("data_room_catalog_created", facts={"entry_count": len(result), "catalog_path": str(catalog_path)})
        return result

    def search(
        self,
        query: str,
        *,
        catalog: Iterable[DataRoomCatalogEntry] | None = None,
        limit: int = 20,
    ) -> tuple[DataRoomCatalogEntry, ...]:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        entries = tuple(catalog) if catalog is not None else self.build_catalog()
        tokens = tuple(token for token in str(query).strip().lower().split() if token)
        if not tokens:
            return tuple(sorted(entries, key=lambda item: (item.path, item.sheet_name or "")))[:limit]
        ranked: list[tuple[int, DataRoomCatalogEntry]] = []
        for entry in entries:
            values = [
                entry.path,
                entry.format,
                entry.kind,
                entry.table_name or "",
                entry.sheet_name or "",
                *entry.columns,
            ]
            values.extend(str(value) for sample in entry.sample_values.values() for value in sample)
            haystack = " ".join(values).lower()
            score = sum(1 for token in tokens if token in haystack)
            if score:
                ranked.append((score, entry))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].path, pair[1].sheet_name or ""))
        return tuple(entry for _, entry in ranked[:limit])


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DataRoomWorkbench:
    """Analyst-facing workbench for one archive and one run context."""

    def __init__(
        self,
        context: RunContext,
        archive: str | Path | DataAssetRef,
        *,
        runtime: Any = None,
        telemetry: TelemetryRecorder | None = None,
        lem: LivingEnterpriseModel | None = None,
        **limits: Any,
    ) -> None:
        if runtime is not None and getattr(runtime, "context", context) is not context:
            raise ValueError("runtime must use the same RunContext as the workbench")
        self.context = context
        self.runtime = runtime
        self.telemetry = telemetry or getattr(runtime, "telemetry", None) or TelemetryRecorder(context=context)
        self.lem = lem
        self._room = DataRoom.open(context, archive, telemetry=self.telemetry, **limits)
        self._prepared: dict[str, PreparedAssetDescriptor] = {}

    @property
    def data_room(self) -> DataRoom:
        return self._room

    def catalog(self, *, sample_rows: int = 20, categorical_limit: int = 20) -> tuple[DataRoomCatalogEntry, ...]:
        return self._room.build_catalog(sample_rows=sample_rows, categorical_limit=categorical_limit)

    def _resolve_source_member(self, value: DataRoomMember | DataRoomCatalogEntry | str | Path) -> DataRoomMember:
        return self._room._resolve_member(value)

    def _normalize_source_refs(
        self,
        source_refs: Iterable[DataAssetRef | str],
    ) -> tuple[tuple[DataAssetRef | str, ...], tuple[str, ...]]:
        """Resolve and rehash caller-supplied refs before any output write."""

        normalized: list[DataAssetRef | str] = []
        hashes: list[str] = []
        for ref in source_refs:
            if isinstance(ref, DataAssetRef):
                source_path = self.context.resolve_input(ref.uri)
                if not source_path.is_file():
                    raise FileNotFoundError(source_path)
                actual_hash = _sha256_file(source_path)
                if ref.content_hash and ref.content_hash != actual_hash:
                    raise ValueError(f"source changed after registration: {ref.uri}")
                normalized.append(
                    DataAssetRef(
                        uri=str(source_path),
                        format=ref.format,
                        content_hash=actual_hash,
                        size_bytes=source_path.stat().st_size,
                        metadata=ref.metadata,
                    )
                )
                hashes.append(actual_hash)
                continue
            source_path = self.context.resolve_input(ref)
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            actual_hash = _sha256_file(source_path)
            normalized.append(str(source_path))
            hashes.append(actual_hash)
        return tuple(normalized), tuple(hashes)

    def _canonical_lineage(
        self,
        lineage: Mapping[str, Any] | None,
        *,
        member_lineage: Sequence[Mapping[str, Any]],
        transformations: Sequence[str],
    ) -> dict[str, Any]:
        provided = dict(lineage or {})
        reserved = sorted(_RESERVED_LINEAGE_KEYS.intersection(provided))
        if reserved:
            raise ValueError(f"lineage contains reserved keys: {', '.join(reserved)}")
        return {
            **provided,
            "archive": self._room.archive_ref.to_dict(),
            "members": list(member_lineage),
            "transformations": list(transformations),
        }

    def _materialize_rows(
        self,
        rows: Iterable[Mapping[str, Any]] | DataRoomMember | DataRoomCatalogEntry | str | Path,
        *,
        sheet: str | None,
        max_rows: int,
    ) -> tuple[list[dict[str, Any]], list[DataRoomMember], str | None]:
        inferred_members: list[DataRoomMember] = []
        if isinstance(rows, (DataRoomMember, DataRoomCatalogEntry, str, Path)):
            catalog_entry = rows if isinstance(rows, DataRoomCatalogEntry) else None
            inferred_member = self._resolve_source_member(rows)
            inferred_members.append(inferred_member)
            if sheet is None and catalog_entry is not None:
                sheet = catalog_entry.sheet_name
            rows_value: Iterable[Mapping[str, Any]] = self._room.read_rows(
                inferred_member,
                sheet=sheet,
                limit=max_rows + 1,
            )
        else:
            rows_value = rows
        materialized: list[dict[str, Any]] = []
        for row in rows_value:
            if not isinstance(row, Mapping):
                raise TypeError("prepared rows must be mappings")
            if len(materialized) >= max_rows:
                raise ValueError(f"prepared asset exceeds max_rows: {max_rows}")
            materialized.append(dict(row))
        return materialized, inferred_members, sheet

    @staticmethod
    def _infer_schema(rows: Sequence[Mapping[str, Any]], schema: Mapping[str, str] | None) -> dict[str, str]:
        if schema:
            return dict(schema)
        field_names: list[str] = []
        for row in rows:
            for key in row:
                if str(key) not in field_names:
                    field_names.append(str(key))
        return {
            key: next((_physical_type(row.get(key)) for row in rows if row.get(key) is not None), "null")
            for key in field_names
        }

    @staticmethod
    def _encode_prepared_rows(rows: Sequence[Mapping[str, Any]], normalized_format: str, schema: Mapping[str, str]) -> bytes:
        if normalized_format == "jsonl":
            return "".join(
                json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str) + "\n"
                for row in rows
            ).encode("utf-8")
        field_names = [str(key) for key in schema]
        for row in rows:
            for key in row:
                if str(key) not in field_names:
                    field_names.append(str(key))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=field_names, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: _cell_value(row.get(key)) for key in field_names} for row in rows)
        return output.getvalue().encode("utf-8")

    def _prepared_sources(
        self,
        inferred_members: Sequence[DataRoomMember],
        source_members: Iterable[DataRoomMember | DataRoomCatalogEntry | str | Path],
        source_refs: Iterable[DataAssetRef | str],
    ) -> tuple[tuple[DataRoomMember, ...], tuple[DataAssetRef | str, ...], tuple[str, ...]]:
        members = list(inferred_members)
        members.extend(self._resolve_source_member(value) for value in source_members)
        unique_members = tuple(dict((member.path, member) for member in members).values())
        normalized_refs, supplied_ref_hashes = self._normalize_source_refs(source_refs)
        source_hashes = [self._room.archive_ref.content_hash or ""]
        source_hashes.extend(member.content_hash for member in unique_members)
        source_hashes.extend(supplied_ref_hashes)
        source_hashes = tuple(dict.fromkeys(value for value in source_hashes if value))
        source_ref_values: tuple[DataAssetRef | str, ...] = (self._room.archive_ref, *normalized_refs)
        return unique_members, source_ref_values, source_hashes

    def _prepared_descriptor(
        self,
        *,
        prepared_asset_id: str,
        normalized_format: str,
        destination: Path,
        encoded: bytes,
        rows: Sequence[Mapping[str, Any]],
        schema: Mapping[str, str],
        grain: str | None,
        transformations: tuple[str, ...],
        identity_mappings: tuple[str, ...],
        relationship_mappings: tuple[str, ...],
        limitations: tuple[str, ...],
        lineage: Mapping[str, Any] | None,
        unique_members: Sequence[DataRoomMember],
        source_refs: tuple[DataAssetRef | str, ...],
        source_hashes: tuple[str, ...],
        sheet: str | None,
        max_bytes: int,
    ) -> PreparedAssetDescriptor:
        member_lineage = [
            {"path": member.path, "content_hash": member.content_hash, "format": member.format, "sheet": sheet}
            for member in unique_members
        ]
        lineage_payload = self._canonical_lineage(
            lineage,
            member_lineage=member_lineage,
            transformations=transformations,
        )
        manifest_payload = {
            "prepared_asset_id": prepared_asset_id,
            "format": normalized_format,
            "schema": dict(schema),
            "source_hashes": list(source_hashes),
            "transformations": list(transformations),
        }
        operation_manifest_hash = _sha256_bytes(json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")).encode())
        return PreparedAssetDescriptor(
            prepared_asset_id=prepared_asset_id,
            source_refs=source_refs,
            source_hashes=source_hashes,
            location=str(destination),
            schema=dict(schema),
            grain=grain,
            transformations=transformations,
            identity_mappings=identity_mappings,
            relationship_mappings=relationship_mappings,
            limitations=limitations,
            lineage=lineage_payload,
            scope="reusable",
            prepared_content_hash=_sha256_bytes(encoded),
            operation_manifest_hash=operation_manifest_hash,
            core_version=self.context.core_version,
            row_count=len(rows),
            byte_count=len(encoded),
            metadata={
                "format": normalized_format,
                "source_member_paths": [member.path for member in unique_members],
                "max_bytes": max_bytes,
            },
        )

    def save_prepared(
        self,
        prepared_asset_id: str,
        rows: Iterable[Mapping[str, Any]] | DataRoomMember | DataRoomCatalogEntry | str | Path,
        *,
        format: str = "jsonl",
        sheet: str | None = None,
        source_members: Iterable[DataRoomMember | DataRoomCatalogEntry | str | Path] = (),
        source_refs: Iterable[DataAssetRef | str] = (),
        schema: Mapping[str, str] | None = None,
        grain: str | None = None,
        transformations: Iterable[str] = (),
        identity_mappings: Iterable[str] = (),
        relationship_mappings: Iterable[str] = (),
        limitations: Iterable[str] = (),
        lineage: Mapping[str, Any] | None = None,
        register_lem: bool = True,
        max_rows: int = DEFAULT_CATALOG_ROWS,
        max_bytes: int = DEFAULT_PREPARED_MAX_BYTES,
        max_output_bytes: int | None = None,
    ) -> PreparedAssetDescriptor:
        prepared_asset_id = _safe_id(prepared_asset_id, "prepared_asset_id")
        normalized_format = str(format).lower().lstrip(".")
        if normalized_format == "ndjson":
            normalized_format = "jsonl"
        if normalized_format not in {"jsonl", "csv"}:
            raise ValueError("prepared assets support only JSONL or CSV")
        if max_rows < 0:
            raise ValueError("max_rows cannot be negative")
        if max_output_bytes is not None:
            if max_output_bytes < 0:
                raise ValueError("max_output_bytes cannot be negative")
            max_bytes = max_output_bytes
        if max_bytes < 0:
            raise ValueError("max_bytes cannot be negative")
        self._room._check_archive_unchanged()
        source_members = tuple(source_members)
        source_refs = tuple(source_refs)
        transformations = tuple(str(value) for value in transformations)
        identity_mappings = tuple(str(value) for value in identity_mappings)
        relationship_mappings = tuple(str(value) for value in relationship_mappings)
        limitations = tuple(str(value) for value in limitations)
        materialized, inferred_members, sheet = self._materialize_rows(rows, sheet=sheet, max_rows=max_rows)
        unique_members, source_ref_values, source_hashes = self._prepared_sources(
            inferred_members,
            source_members,
            source_refs,
        )
        inferred_schema = self._infer_schema(materialized, schema)
        filename = f"{prepared_asset_id}.{normalized_format}"
        destination = self.context.resolve_run_path(Path("prepared") / filename)
        encoded = self._encode_prepared_rows(materialized, normalized_format, inferred_schema)
        if len(encoded) > max_bytes:
            raise ValueError(f"prepared asset exceeds max_bytes: {len(encoded)} > {max_bytes}")
        descriptor = self._prepared_descriptor(
            prepared_asset_id=prepared_asset_id,
            normalized_format=normalized_format,
            destination=destination,
            encoded=encoded,
            rows=materialized,
            schema=inferred_schema,
            grain=grain,
            transformations=transformations,
            identity_mappings=identity_mappings,
            relationship_mappings=relationship_mappings,
            limitations=limitations,
            lineage=lineage,
            unique_members=unique_members,
            source_refs=source_ref_values,
            source_hashes=source_hashes,
            sheet=sheet,
            max_bytes=max_bytes,
        )
        _atomic_write_bytes(destination, encoded)
        descriptor_path = self.context.resolve_run_path(Path("prepared") / f"{prepared_asset_id}.descriptor.json")
        _atomic_write_json(descriptor_path, descriptor.to_dict())
        self._prepared[prepared_asset_id] = descriptor
        if register_lem and self.lem is not None:
            self.lem.register_prepared_asset(descriptor)
        self._emit_prepared(descriptor)
        return descriptor

    def _emit_prepared(self, descriptor: PreparedAssetDescriptor) -> None:
        try:
            self.telemetry.record(
                "data_room_prepared_write",
                bytes_processed=descriptor.byte_count,
                rows=descriptor.row_count,
                output_hashes=(descriptor.prepared_content_hash,) if descriptor.prepared_content_hash else (),
                facts={"prepared_asset_id": descriptor.prepared_asset_id, "location": descriptor.location},
            )
        except Exception:
            pass

    def prepared(self, prepared_asset_id: str) -> PreparedAsset:
        prepared_asset_id = _safe_id(prepared_asset_id, "prepared_asset_id")
        descriptor = self._prepared.get(prepared_asset_id)
        descriptor_path = self.context.resolve_run_path(Path("prepared") / f"{prepared_asset_id}.descriptor.json")
        if descriptor is None:
            if not descriptor_path.is_file():
                raise FileNotFoundError(descriptor_path)
            descriptor = PreparedAssetDescriptor.from_dict(json.loads(descriptor_path.read_text(encoding="utf-8")))
            self._prepared[prepared_asset_id] = descriptor
        location = self.context.resolve_run_path(descriptor.location)
        if not location.is_file():
            raise FileNotFoundError(location)
        encoded = location.read_bytes()
        if descriptor.prepared_content_hash and _sha256_bytes(encoded) != descriptor.prepared_content_hash:
            raise ValueError(f"prepared asset content changed: {prepared_asset_id}")
        if descriptor.byte_count is not None and descriptor.byte_count != len(encoded):
            raise ValueError(f"prepared asset byte count changed: {prepared_asset_id}")
        fmt = str(descriptor.metadata.get("format", location.suffix.lstrip("."))).lower()
        rows: list[dict[str, Any]] = []
        if fmt == "jsonl":
            for line_number, line in enumerate(encoded.decode("utf-8").splitlines(), 1):
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, Mapping):
                        raise ValueError(f"prepared JSONL row {line_number} is not an object")
                    rows.append(dict(value))
        elif fmt == "csv":
            reader = csv.DictReader(io.StringIO(encoded.decode("utf-8")))
            rows = [dict(row) for row in reader]
        else:
            raise ValueError(f"unsupported prepared asset format: {fmt}")
        if descriptor.row_count is not None and descriptor.row_count != len(rows):
            raise ValueError(f"prepared asset row count changed: {prepared_asset_id}")
        return PreparedAsset(descriptor=descriptor, rows=tuple(rows))


def _atomic_write_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "DataRoom",
    "DataRoomCatalogEntry",
    "DataRoomMember",
    "DataRoomWorkbench",
    "PreparedAsset",
]
