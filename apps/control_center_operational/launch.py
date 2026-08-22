"""Guarded launch preparation and new-run materialisation.

This module contains the operational app's side-effect boundary.  Preparing a
draft only validates and records state below ``state_root``.  A run root and
the Auto Foundry core are touched only from :meth:`LaunchManager.execute`,
after the caller supplies the exact draft fingerprint and ``confirmed: true``.
The implementation is stdlib-first and keeps all source/URL handling bounded.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import unicodedata
import uuid
import zipfile
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping, MutableMapping
from urllib.parse import urljoin, urlparse


SUPPORTED_EXTENSIONS = frozenset(
    {
        "csv",
        "tsv",
        "json",
        "jsonl",
        "ndjson",
        "xlsx",
        "parquet",
        "zip",
        "txt",
        "text",
        "md",
        "markdown",
        "rst",
        "pdf",
        "docx",
        "odt",
    }
)
# ZIP members may use the core's bounded document formats because a container
# is flattened into the canonical data room rather than exposed as an opaque
# nested archive.
ZIP_MEMBER_DOCUMENT_EXTENSIONS = frozenset(
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
        "docx",
        "odt",
    }
)
ZIP_MEMBER_EXTENSIONS = frozenset((SUPPORTED_EXTENSIONS - {"zip"}) | ZIP_MEMBER_DOCUMENT_EXTENSIONS)
SUPPORTED_ZIP_COMPRESSION_METHODS = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }
)
MAX_ZIP_MEMBER_COUNT = 4096
# The physical-entry bound is intentionally no larger than the accepted
# semantic-member bound.  Keep it separately named so validation can account
# for directory and ignored-metadata records before semantic filtering.
MAX_ZIP_PHYSICAL_ENTRY_COUNT = MAX_ZIP_MEMBER_COUNT
MAX_ZIP_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 1000.0
# A central-directory record is 46 fixed bytes plus three variable fields,
# each capped by the ZIP format at 65,535 bytes.  Keep a much smaller explicit
# aggregate ceiling so ``zipfile`` cannot allocate an enormous central table
# even when the top-level source file itself is within the upload bound.
ZIP_CENTRAL_DIRECTORY_FIXED_BYTES = 46
ZIP_CENTRAL_DIRECTORY_MAX_FIELD_BYTES = 65_535
ZIP_CENTRAL_DIRECTORY_MAX_BYTES_PER_ENTRY = ZIP_CENTRAL_DIRECTORY_FIXED_BYTES + (3 * ZIP_CENTRAL_DIRECTORY_MAX_FIELD_BYTES)
MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024
ZIP_EOCD_FIXED_BYTES = 22
ZIP_EOCD_MAX_COMMENT_BYTES = 65_535
ZIP64_EOCD_FIXED_BYTES = 56
ZIP64_EOCD_LOCATOR_BYTES = 20
DEFAULT_MAX_AGENTS = 64
DEFAULT_UPLOAD_LIMIT = 512 * 1024 * 1024
DEFAULT_MAX_SOURCE_COUNT = 256
DEFAULT_MAX_SOURCE_TOTAL = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_NETWORK_TIMEOUT = 15.0
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MACOS_APP_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def default_codex_binary() -> str:
    """Prefer the current desktop-bundled CLI when it is executable.

    The desktop app and its config schema are released together.  A separate
    ``codex`` found on PATH can lag behind that schema and fail before an agent
    process starts.  Non-macOS installs retain the ordinary PATH lookup.
    """

    if MACOS_APP_CODEX.is_file() and os.access(MACOS_APP_CODEX, os.X_OK):
        return str(MACOS_APP_CODEX)
    return shutil.which("codex") or "codex"
MAX_REQUIREMENT_RECORDS = 256
MAX_INTAKE_BLOCKS = 256
MAX_INTAKE_TEXT_BYTES = 2 * 1024 * 1024
MAX_CATALOG_FILES = 256


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _planner_plan_hash(plan: Mapping[str, Any]) -> str:
    """Hash the exact canonical bytes RequirementRunExtension persists."""

    return sha256_bytes(canonical_bytes(dict(plan)) + b"\n")


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write a file without following an existing destination symlink."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing to overwrite symlink: {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def load_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"state file is not a regular file: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON state: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def safe_component(value: Any, label: str = "value") -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise ValueError(f"{label} must be a simple path component")
    return text


def safe_relative_path(value: Any, *, label: str = "relative_path") -> str:
    """Return a portable relative archive name and reject traversal aliases."""

    raw = str(value or "").replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("~") or "\x00" in raw:
        raise ValueError(f"{label} must be a relative path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} contains traversal")
    # A drive prefix is absolute on Windows even when parsed on POSIX.
    if len(parts[0]) == 2 and parts[0][1] == ":":
        raise ValueError(f"{label} must be relative")
    return "/".join(parts)


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def reject_symlink_components(path: Path, root: Path) -> None:
    """Reject aliases in an explicitly administrator-approved tree."""

    root = root.resolve(strict=False)
    if root.is_symlink():
        raise ValueError("configured root cannot be a symlink")
    if not is_within(path, (root,)):
        raise ValueError("path escapes configured root")
    # Walk the caller's lexical spelling so an in-root symlink is rejected
    # even when its resolved target is also in-root.  The walk intentionally
    # does not depend on ``relative_to`` because macOS may spell the same
    # temporary directory as ``/var`` or ``/private/var``.
    current = Path(os.path.abspath(path))
    for _ in range(128):
        if current.is_symlink():
            resolved_current = current.resolve(strict=False)
            # macOS exposes administrator-selected temporary roots through
            # aliases such as /var -> /private/var.  Treat an alias that only
            # reaches an ancestor of the configured root as harmless; a
            # symlink at or below the root remains rejected.
            if not (root == resolved_current or resolved_current in root.parents):
                raise ValueError("symlink source paths are not accepted")
        if current == root or current.parent == current:
            break
        current = current.parent


def supported_extension(name: str) -> bool:
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix in SUPPORTED_EXTENSIONS


def _is_zip_name(name: Any) -> bool:
    return Path(str(name or "")).suffix.lower().lstrip(".") == "zip"


def _normalized_archive_name(name: str) -> str:
    """Return the collision key shared by ordinary and ZIP source names."""

    return unicodedata.normalize("NFC", str(name)).casefold()


def _zip_member_name_error(name: Any) -> ValueError:
    return ValueError(f"ZIP member {name!r} has an unsafe name")


def _validate_zip_member_name(name: Any, *, directory: bool = False) -> str:
    """Validate an archive member using the core DataRoom path boundary.

    ZIP names are not passed through :func:`safe_relative_path`: that helper
    deliberately normalizes backslashes for browser-uploaded paths, whereas a
    ZIP member containing a backslash is an ambiguous cross-platform alias and
    must fail closed.
    """

    if not isinstance(name, str) or not name or "\x00" in name:
        raise _zip_member_name_error(name)
    if name.startswith(("/", "\\")) or "\\" in name:
        raise _zip_member_name_error(name)
    raw_parts = name.split("/")
    if directory and raw_parts[-1] == "":
        raw_parts = raw_parts[:-1]
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise _zip_member_name_error(name)
    if ":" in raw_parts[0]:
        raise _zip_member_name_error(name)
    return name


def _is_ignored_zip_metadata(name: str) -> bool:
    """Match the core DataRoom's inert macOS metadata policy exactly."""

    parts = name.split("/")
    return parts[-1] == ".DS_Store" or "__MACOSX" in parts[:-1]


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    # Keep this independent from pathlib and extraction APIs.  ZIP external
    # attributes carry the POSIX mode in the high word, as in DataRoom.
    import stat

    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _zip_raw_member_name(info: zipfile.ZipInfo) -> str:
    """Return the central-directory spelling before ZipInfo NUL truncation."""

    raw = getattr(info, "orig_filename", info.filename)
    if not isinstance(raw, str) or raw != info.filename:
        raise ValueError(f"ZIP member raw filename is not the decoded filename: {raw!r}")
    return raw


def _check_zip_compression(info: zipfile.ZipInfo, name: str) -> None:
    if info.compress_type not in SUPPORTED_ZIP_COMPRESSION_METHODS:
        method = zipfile.compressor_names.get(info.compress_type, "unknown")
        raise ValueError(
            f"ZIP member {name!r} uses unsupported compression method {info.compress_type} ({method})"
        )


@dataclass(frozen=True)
class _ZipMember:
    name: str
    size: int
    compressed_size: int
    info: zipfile.ZipInfo


@dataclass(frozen=True)
class _ZipInspection:
    members: tuple[_ZipMember, ...]
    expanded_size: int
    physical_entry_count: int = 0
    physical_expanded_size: int = 0

    @property
    def member_count(self) -> int:
        return len(self.members)


def _expanded_source_limit(settings: "LaunchSettings") -> int:
    return min(int(settings.max_source_total_bytes), MAX_ZIP_TOTAL_BYTES)


def _zip_central_directory_limit() -> int:
    physical_limit = min(MAX_ZIP_MEMBER_COUNT, MAX_ZIP_PHYSICAL_ENTRY_COUNT)
    return min(
        MAX_ZIP_CENTRAL_DIRECTORY_BYTES,
        physical_limit * ZIP_CENTRAL_DIRECTORY_MAX_BYTES_PER_ENTRY,
    )


def _preflight_zip_archive(path: Path) -> None:
    """Bound ZIP metadata before ``zipfile.ZipFile`` allocates ``ZipInfo`` rows.

    Python's ``ZipFile`` reads the entire claimed central directory into a
    ``BytesIO`` and only then checks member counts.  Read only the bounded EOCD
    tail here, resolve a ZIP64 EOCD when required, and reject impossible or
    oversized central-directory claims before constructing ``ZipFile``.
    """

    file_size = int(path.stat().st_size)
    if file_size < ZIP_EOCD_FIXED_BYTES:
        raise ValueError("ZIP end-of-central-directory record is missing")
    tail_size = min(file_size, ZIP_EOCD_FIXED_BYTES + ZIP_EOCD_MAX_COMMENT_BYTES)
    with path.open("rb") as stream:
        stream.seek(file_size - tail_size)
        tail = stream.read(tail_size)
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0 or marker + ZIP_EOCD_FIXED_BYTES > len(tail):
        raise ValueError("ZIP end-of-central-directory record is missing")
    (
        _signature,
        disk_number,
        central_disk,
        entries_this_disk,
        total_entries,
        central_size_32,
        central_offset_32,
        comment_length,
    ) = struct.unpack_from("<4s4H2LH", tail, marker)
    if marker + ZIP_EOCD_FIXED_BYTES + comment_length != len(tail):
        raise ValueError("ZIP end-of-central-directory comment is truncated or has trailing bytes")
    eocd_offset = file_size - tail_size + marker
    if disk_number != 0 or central_disk != 0 or entries_this_disk != total_entries:
        raise ValueError("multi-disk ZIP archives are not supported")

    zip64_needed = (
        entries_this_disk == 0xFFFF
        or total_entries == 0xFFFF
        or central_size_32 == 0xFFFFFFFF
        or central_offset_32 == 0xFFFFFFFF
    )
    locator_offset = eocd_offset - ZIP64_EOCD_LOCATOR_BYTES
    locator: tuple[int, int, int] | None = None
    if locator_offset >= 0:
        with path.open("rb") as stream:
            stream.seek(locator_offset)
            locator_bytes = stream.read(ZIP64_EOCD_LOCATOR_BYTES)
        if len(locator_bytes) == ZIP64_EOCD_LOCATOR_BYTES and locator_bytes[:4] == b"PK\x06\x07":
            if not zip64_needed:
                raise ValueError("ambiguous ZIP64 end records")
            _sig, locator_disk, locator_reloff, total_disks = struct.unpack("<4sLQL", locator_bytes)
            if locator_disk != 0 or total_disks != 1:
                raise ValueError("multi-disk ZIP archives are not supported")
            locator = (locator_disk, locator_reloff, total_disks)
    if zip64_needed and locator is None:
        raise ValueError("ZIP64 end records are missing")

    if locator is not None:
        _locator_disk, locator_reloff, _total_disks = locator
        # CPython 3.12 ignores the locator's relative offset and reads the
        # fixed ZIP64 record immediately before the locator.  Follow that
        # exact rule, then validate the offset under an explicit SFX concat
        # policy so a second attacker-controlled record cannot disagree.
        zip64_offset = locator_offset - ZIP64_EOCD_FIXED_BYTES
        if zip64_offset < 0:
            raise ValueError("ZIP64 end-of-central-directory bounds are invalid")
        with path.open("rb") as stream:
            stream.seek(zip64_offset)
            zip64_bytes = stream.read(ZIP64_EOCD_FIXED_BYTES)
        if len(zip64_bytes) != ZIP64_EOCD_FIXED_BYTES:
            raise ValueError("ZIP64 end-of-central-directory record is truncated")
        (
            signature,
            record_size,
            _create_version,
            _read_version,
            zip64_disk,
            zip64_central_disk,
            entries_this_disk,
            total_entries,
            central_size,
            central_offset,
        ) = struct.unpack("<4sQ2H2L4Q", zip64_bytes)
        if signature != b"PK\x06\x06" or record_size != 44:
            raise ValueError("ZIP64 end-of-central-directory record is malformed")
        if zip64_disk != 0 or zip64_central_disk != 0 or entries_this_disk != total_entries:
            raise ValueError("multi-disk ZIP archives are not supported")
        if central_size > _zip_central_directory_limit():
            raise ValueError(
                f"ZIP central directory exceeds the preflight byte limit ({central_size} > {_zip_central_directory_limit()})"
            )
        concat = zip64_offset - central_size - central_offset
        if concat < 0 or locator_reloff + concat != zip64_offset:
            raise ValueError("ZIP64 locator offset does not match the immediate end record")
        central_end = zip64_offset
    else:
        central_size = int(central_size_32)
        central_offset = int(central_offset_32)
        central_end = eocd_offset
        if central_size > _zip_central_directory_limit():
            raise ValueError(
                f"ZIP central directory exceeds the preflight byte limit ({central_size} > {_zip_central_directory_limit()})"
            )
        concat = central_end - central_size - central_offset
        if concat < 0:
            raise ValueError("ZIP central directory offset is unsafe")

    physical_limit = min(MAX_ZIP_MEMBER_COUNT, MAX_ZIP_PHYSICAL_ENTRY_COUNT)
    if total_entries > physical_limit:
        raise ValueError(
            f"ZIP archive exceeds physical entry limit ({physical_limit}) before central-directory allocation"
        )
    central_limit = _zip_central_directory_limit()
    if central_size > central_limit:
        raise ValueError(
            f"ZIP central directory exceeds the preflight byte limit ({central_size} > {central_limit})"
        )
    if central_size < total_entries * ZIP_CENTRAL_DIRECTORY_FIXED_BYTES:
        raise ValueError("ZIP central directory is too small for its entry count")
    if central_end > file_size or central_size > central_end:
        raise ValueError("ZIP central directory bounds are invalid")
    central_start = central_end - central_size
    if central_start < 0 or central_offset + concat != central_start or central_start + central_size != central_end:
        raise ValueError("ZIP central directory offset is unsafe")


def _consume_zip_member(source: zipfile.ZipFile, info: zipfile.ZipInfo, name: str) -> int:
    observed = 0
    try:
        with source.open(info, "r") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                observed += len(chunk)
    except Exception as exc:
        # zipfile exposes CRC, decompressor, Unicode, and unsupported-method
        # failures through several stdlib exception types.  Normalize all of
        # them at this concrete member boundary without exposing source paths.
        raise ValueError(f"ZIP member {name!r} failed CRC/content validation: {exc}") from exc
    return observed


def _inspect_zip_source(
    path: Path,
    *,
    max_total_bytes: int,
    read_members: bool,
    prior_total_bytes: int = 0,
    prior_entry_count: int = 0,
) -> _ZipInspection:
    """Inventory one ZIP without extracting it.

    ``read_members`` is true during prepare so ``zipfile`` verifies CRCs.  At
    execute the same metadata checks are repeated and the streaming copy below
    reads each member exactly once while writing the output archive, which also
    verifies its CRC.  ``prior_*`` offsets let callers enforce the shared
    launch-wide physical bounds while the returned ``expanded_size`` and
    ``member_count`` retain accepted-member semantics for draft bindings.
    """

    archive_label = path.name or "archive.zip"
    members: list[_ZipMember] = []
    names: set[str] = set()
    expanded_size = 0
    physical_entry_count = 0
    physical_expanded_size = 0
    physical_entry_limit = min(MAX_ZIP_MEMBER_COUNT, MAX_ZIP_PHYSICAL_ENTRY_COUNT)
    if prior_total_bytes < 0 or prior_entry_count < 0:
        raise ValueError("ZIP physical-bound offsets cannot be negative")
    try:
        try:
            _preflight_zip_archive(path)
        except ValueError as exc:
            raise ValueError(f"invalid ZIP archive {archive_label!r}: {exc}") from exc
        with zipfile.ZipFile(path, "r") as source:
            for info in source.infolist():
                raw_name = _zip_raw_member_name(info)
                physical_entry_count += 1
                if prior_entry_count + physical_entry_count > physical_entry_limit:
                    raise ValueError(
                        f"ZIP archive exceeds physical entry limit ({physical_entry_limit}) at {raw_name!r}"
                    )
                is_directory = info.is_dir()
                name = _validate_zip_member_name(raw_name, directory=is_directory)
                normalized = _normalized_archive_name(name.rstrip("/"))
                if normalized in names:
                    raise ValueError(f"ZIP member {name!r} duplicates another member (case-insensitive)")
                names.add(normalized)
                if _is_zip_symlink(info):
                    raise ValueError(f"ZIP member {name!r} is a symlink")
                if info.flag_bits & 0x1:
                    raise ValueError(f"ZIP member {name!r} is encrypted")
                _check_zip_compression(info, name)
                size = int(info.file_size)
                compressed_size = int(info.compress_size)
                if size < 0 or compressed_size < 0:
                    raise ValueError(f"ZIP member {name!r} has invalid sizes")
                if is_directory:
                    if size != 0 or compressed_size != 0:
                        raise ValueError(f"ZIP directory {name!r} has a nonempty payload")
                    continue
                if size > MAX_ZIP_MEMBER_BYTES:
                    raise ValueError(
                        f"ZIP member {name!r} exceeds expanded member limit ({size} > {MAX_ZIP_MEMBER_BYTES})"
                    )
                if size and compressed_size == 0:
                    raise ValueError(f"ZIP member {name!r} has an infinite compression ratio")
                if compressed_size and size / compressed_size > MAX_ZIP_COMPRESSION_RATIO:
                    raise ValueError(
                        f"ZIP member {name!r} exceeds compression ratio limit ({MAX_ZIP_COMPRESSION_RATIO})"
                    )
                if prior_total_bytes + physical_expanded_size + size > max_total_bytes:
                    raise ValueError(
                        f"ZIP expanded bytes exceed the configured aggregate limit ({max_total_bytes}) at {name!r}"
                    )
                physical_expanded_size += size
                if _is_ignored_zip_metadata(name):
                    if read_members:
                        observed = _consume_zip_member(source, info, name)
                        if observed != size:
                            raise ValueError(f"ZIP member {name!r} expanded size changed while reading")
                    continue
                suffix = Path(name).suffix.lower().lstrip(".")
                if suffix == "zip":
                    raise ValueError(f"ZIP member {name!r} is a nested archive")
                if suffix not in ZIP_MEMBER_EXTENSIONS:
                    extension = f".{suffix}" if suffix else "(no extension)"
                    raise ValueError(f"ZIP member {name!r} has unsupported extension {extension}")
                if len(members) >= MAX_ZIP_MEMBER_COUNT:
                    raise ValueError(
                        f"ZIP archive exceeds accepted member limit ({MAX_ZIP_MEMBER_COUNT}) at {name!r}"
                    )
                if read_members:
                    observed = _consume_zip_member(source, info, name)
                    if observed != size:
                        raise ValueError(f"ZIP member {name!r} expanded size changed while reading")
                members.append(_ZipMember(name, size, compressed_size, info))
                expanded_size += size
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, UnicodeError) as exc:
        raise ValueError(f"invalid ZIP archive {archive_label!r}: {exc}") from exc
    except (OSError, EOFError) as exc:
        raise ValueError(f"invalid ZIP archive {archive_label!r}: {exc}") from exc
    return _ZipInspection(
        tuple(sorted(members, key=lambda item: item.name)),
        expanded_size,
        physical_entry_count,
        physical_expanded_size,
    )


def capacity_for_total(total: int) -> dict[str, int]:
    """Use the same deterministic role split as the operational browser."""

    total = int(total)
    analytical_owner = max(1, (total + 7) // 8)
    specialist = min(analytical_owner * 3, (total * 3) // 8)
    return {
        "total": total,
        "entityResolution": max(0, total - analytical_owner - specialist),
        "analyticalOwner": analytical_owner,
        "specialist": specialist,
    }


class LaunchError(Exception):
    """Expected, fail-closed launch error with an HTTP-friendly status."""

    status_code = 422


class LaunchValidationError(LaunchError):
    def __init__(self, errors: Mapping[str, str], message: str = "Launch draft is invalid") -> None:
        super().__init__(message)
        self.errors = dict(errors)


class LockedLaunchError(LaunchError):
    status_code = 403


class LaunchConflictError(LaunchError):
    status_code = 409


@dataclass(frozen=True)
class LaunchSettings:
    runtime_root: Path
    runs_root: Path
    source_roots: tuple[Path, ...] = ()
    state_root: Path | None = None
    max_agents: int = DEFAULT_MAX_AGENTS
    enable_launch: bool = False
    codex_bin: str = field(default_factory=default_codex_binary)
    upload_limit_bytes: int = DEFAULT_UPLOAD_LIMIT
    max_source_count: int = DEFAULT_MAX_SOURCE_COUNT
    max_source_total_bytes: int = DEFAULT_MAX_SOURCE_TOTAL
    launch_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    protected_run_ids: tuple[str, ...] = ()
    protected_run_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        raw_runtime = Path(self.runtime_root).expanduser()
        raw_runs = Path(self.runs_root).expanduser()
        raw_sources = tuple(Path(value).expanduser() for value in self.source_roots)
        raw_state = Path(self.state_root).expanduser() if self.state_root is not None else None
        raw_protected = tuple(Path(value).expanduser() for value in self.protected_run_roots)
        if raw_runtime.is_symlink() or raw_runs.is_symlink() or (raw_state is not None and raw_state.is_symlink()) or any(value.is_symlink() for value in raw_sources) or any(value.is_symlink() for value in raw_protected):
            raise ValueError("configured roots cannot be symlinks")
        runtime_root = Path(self.runtime_root).expanduser().resolve(strict=False)
        runs_root = Path(self.runs_root).expanduser().resolve(strict=False)
        source_roots = tuple(Path(value).expanduser().resolve(strict=False) for value in self.source_roots)
        state_root = Path(self.state_root).expanduser().resolve(strict=False) if self.state_root else runs_root / ".control-center-operational"
        protected_roots = tuple(Path(value).expanduser().resolve(strict=False) for value in self.protected_run_roots)
        if self.max_agents < 1 or self.max_agents > 64:
            raise ValueError("max_agents must be between 1 and 64")
        if self.upload_limit_bytes < 1 or self.max_source_count < 1 or self.max_source_total_bytes < 1:
            raise ValueError("launch bounds must be positive")
        object.__setattr__(self, "runtime_root", runtime_root)
        object.__setattr__(self, "runs_root", runs_root)
        object.__setattr__(self, "source_roots", source_roots)
        object.__setattr__(self, "state_root", state_root)
        object.__setattr__(self, "protected_run_ids", tuple(str(value) for value in self.protected_run_ids))
        object.__setattr__(self, "protected_run_roots", protected_roots)
        object.__setattr__(self, "codex_bin", str(self.codex_bin))
        object.__setattr__(self, "launch_token", str(self.launch_token))

    @property
    def commands_enabled(self) -> bool:
        return bool(self.enable_launch)

    def config_payload(self) -> dict[str, Any]:
        return {
            "mode": "operational_requirement",
            "maxAgents": self.max_agents,
            "commandsEnabled": self.commands_enabled,
            "launchToken": self.launch_token,
            "sourcePolicy": {
                "extensions": sorted(SUPPORTED_EXTENSIONS),
                "zipMemberExtensions": sorted(ZIP_MEMBER_EXTENSIONS),
                "maxUploadBytes": self.upload_limit_bytes,
                "maxZipMemberBytes": MAX_ZIP_MEMBER_BYTES,
                "maxZipMemberCount": MAX_ZIP_MEMBER_COUNT,
                "maxExpandedSourceBytes": _expanded_source_limit(self),
                "remoteFetch": "execute_only_public_http_https",
                "sourceRoots": [str(root) for root in self.source_roots],
            },
            "confirmation": {"required": True, "fingerprintBound": True},
        }

    def is_protected_run(self, run_id: str, run_root: Path) -> bool:
        if run_id in self.protected_run_ids:
            return True
        return any(is_within(run_root, (root,)) for root in self.protected_run_roots)


@dataclass(frozen=True)
class UploadRecord:
    upload_id: str
    path: Path
    relative_path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "uploadId": self.upload_id,
            "relativePath": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
        }


class UploadStore:
    def __init__(self, settings: LaunchSettings) -> None:
        self.settings = settings

    @property
    def root(self) -> Path:
        return Path(self.settings.state_root) / "uploads"

    def _record_paths(self, upload_id: str) -> tuple[Path, Path]:
        upload_id = safe_component(upload_id, "uploadId")
        directory = self.root / upload_id
        reject_symlink_components(directory, Path(self.settings.state_root))
        return directory / "payload", directory / "metadata.json"

    def load(self, upload_id: str, *, verify: bool = True) -> UploadRecord:
        payload_path, metadata_path = self._record_paths(upload_id)
        metadata = load_object(metadata_path)
        if metadata.get("uploadId") != upload_id:
            raise ValueError("upload ID does not match metadata")
        relative = safe_relative_path(metadata.get("relativePath"), label="stored relative path")
        if payload_path.is_symlink() or not payload_path.is_file():
            raise ValueError("staged upload payload is unavailable")
        if verify:
            size = payload_path.stat().st_size
            digest = sha256_file(payload_path)
            if size != metadata.get("size") or digest != metadata.get("sha256"):
                raise ValueError("staged upload hash or size does not match metadata")
        else:
            size = metadata.get("size")
            digest = metadata.get("sha256")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("staged upload metadata has an invalid size")
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError("staged upload metadata has an invalid hash")
        return UploadRecord(upload_id, payload_path, relative, size, digest)

    def stage(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        relative_path: str | None,
        content_length: int | None,
    ) -> UploadRecord:
        relative = safe_relative_path(relative_path or filename, label="relative_path")
        if not supported_extension(relative):
            raise LaunchValidationError({"filename": f"Unsupported source extension: {relative}"})
        if content_length is None or content_length < 0:
            raise LaunchValidationError({"upload": "Content-Length is required."}, "Upload is invalid")
        if content_length > self.settings.upload_limit_bytes:
            raise LaunchValidationError({"upload": "Upload exceeds the configured size limit."}, "Upload is too large")
        upload_id = "UP-" + uuid.uuid4().hex
        payload_path, metadata_path = self._record_paths(upload_id)
        payload_path.parent.mkdir(parents=True, exist_ok=False)
        digest = hashlib.sha256()
        size = 0
        try:
            with payload_path.open("wb") as target:
                while True:
                    remaining = content_length - size
                    if remaining <= 0:
                        break
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.settings.upload_limit_bytes:
                        raise LaunchValidationError({"upload": "Upload exceeds the configured size limit."}, "Upload is too large")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if size != content_length:
                raise LaunchValidationError({"upload": "Content-Length does not match the body."}, "Upload is incomplete")
            record = UploadRecord(upload_id, payload_path, relative, size, digest.hexdigest())
            atomic_write_json(metadata_path, record.to_dict())
            return record
        except Exception:
            shutil.rmtree(payload_path.parent, ignore_errors=True)
            raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capacity_contract(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, Mapping):
        return None
    aliases = {
        "total": "total",
        "entityResolution": "entityResolution",
        "analyticalOwner": "analyticalOwner",
        "specialist": "specialist",
        "total_active": "total",
        "entity_resolution": "entityResolution",
        "analytical_owner": "analyticalOwner",
    }
    if any(str(key) not in aliases for key in raw):
        return None
    result: dict[str, int] = {}
    for key, value in raw.items():
        target = aliases.get(str(key))
        if target is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        result[target] = int(value)
    if set(result) != {"total", "entityResolution", "analyticalOwner", "specialist"}:
        return None
    return result


def _validate_capacity(payload: Mapping[str, Any], settings: LaunchSettings, *, continue_capacity: Mapping[str, int] | None = None) -> tuple[dict[str, int] | None, str | None]:
    max_agents = payload.get("maxAgents")
    requested = _capacity_contract(payload.get("capacity"))
    if "capacity" in payload and payload.get("capacity") is not None and requested is None:
        return None, "Capacity must include exactly total and all role limits."
    if requested is None and isinstance(max_agents, int) and not isinstance(max_agents, bool):
        if 1 <= max_agents <= settings.max_agents:
            requested = capacity_for_total(max_agents)
    if requested is None:
        return None, "Provide maxAgents and a complete role capacity."
    total = requested["total"]
    if not 1 <= total <= settings.max_agents:
        return None, f"Agent capacity must be between 1 and {settings.max_agents}."
    if not isinstance(max_agents, int) or isinstance(max_agents, bool) or max_agents != total:
        return None, "Total capacity does not match maxAgents."
    if any(requested[name] < 0 or requested[name] > total for name in ("entityResolution", "analyticalOwner", "specialist")):
        return None, "Role limits must be non-negative and cannot exceed total capacity."
    if continue_capacity is not None and requested != dict(continue_capacity):
        return None, "Existing-run capacity is authoritative and cannot be changed."
    if continue_capacity is None and sum(requested[name] for name in ("entityResolution", "analyticalOwner", "specialist")) != total:
        return None, "New-run role limits must sum to total capacity."
    return requested, None


def _authoritative_capacity(run_root: Path) -> dict[str, int] | None:
    state_path = run_root / "entity_resolution" / "state.json"
    if state_path.is_symlink() or not state_path.is_file():
        return None
    state = load_object(state_path)
    raw = state.get("capacity")
    if not isinstance(raw, Mapping):
        return None
    return _capacity_contract(raw)


def validate_remote_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Remote source must use http or https.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Remote source credentials are not accepted.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Remote source port is invalid.") from exc
    _reject_private_host(parsed.hostname)
    if _is_zip_name(parsed.path):
        raise ValueError("Remote ZIP sources are not supported; use a local upload or path.")
    if not supported_extension(parsed.path):
        raise ValueError("Remote source must end in a supported data extension.")
    return url


def _reject_private_host(host: str) -> None:
    normalized = host.strip("[]").lower().rstrip(".")
    if normalized in {"localhost", "localhost.localdomain"}:
        raise ValueError("Private or loopback remote hosts are not accepted.")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("Remote host must resolve to a globally routable address.")


def _resolve_public_host(host: str) -> str:
    """Resolve one host and return an address that was checked for privacy.

    The returned address is subsequently passed directly to a socket
    connection.  Resolving and connecting through separate hostname APIs
    would re-open a DNS-rebinding window, so callers must never feed the
    original hostname into a network connector.
    """

    _reject_private_host(host)
    try:
        values = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Remote host could not be resolved safely.") from exc
    if not values:
        raise ValueError("Remote host has no address.")
    addresses: list[str] = []
    for value in values:
        address = str(value[4][0])
        _reject_private_host(address)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("Remote host has no public address.")
    return addresses[0]


def _resolved_address(value: Any) -> str:
    """Normalize an injected resolver result and enforce the public policy."""

    candidates: list[Any] = []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, Mapping):
        candidates = [value.get("address") or value.get("host")]
    elif isinstance(value, (tuple, list)):
        # Accept one sockaddr/getaddrinfo row, a list of addresses, or a list
        # of getaddrinfo rows from an injected resolver.
        if len(value) >= 5 and isinstance(value[4], (tuple, list)):
            candidates = [value[4][0]]
        else:
            for item in value:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, Mapping):
                    candidates.append(item.get("address") or item.get("host"))
                elif isinstance(item, (tuple, list)) and len(item) >= 5 and isinstance(item[4], (tuple, list)):
                    candidates.append(item[4][0])
                elif isinstance(item, (tuple, list)) and item and isinstance(item[0], str):
                    candidates.append(item[0])
    if not candidates:
        raise ValueError("Remote resolver returned no address.")
    selected: str | None = None
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        _reject_private_host(candidate)
        selected = selected or candidate.strip("[]")
    if selected is None:
        raise ValueError("Remote resolver returned no address.")
    return selected


def _host_header(parsed: Any) -> str:
    host = str(parsed.hostname or "")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if port is not None and port != (443 if parsed.scheme.lower() == "https" else 80):
        return f"{host}:{port}"
    return host


def _request_target(parsed: Any) -> str:
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials a validated address, never a hostname."""

    def __init__(self, original_host: str, connect_address: str, port: int, timeout: float) -> None:
        self.original_host = original_host
        self.connect_address = connect_address
        super().__init__(connect_address, port=port, timeout=timeout)

    def connect(self) -> None:  # pragma: no cover - exercised by integration seams
        self.sock = socket.create_connection((self.connect_address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an IP while retaining hostname SNI/verify."""

    def __init__(self, original_host: str, connect_address: str, port: int, timeout: float) -> None:
        self.original_host = original_host
        self.connect_address = connect_address
        context = ssl.create_default_context()
        super().__init__(connect_address, port=port, timeout=timeout, context=context)

    def connect(self) -> None:  # pragma: no cover - exercised by integration seams
        self.sock = socket.create_connection((self.connect_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.original_host)
        except Exception:
            self.sock.close()
            self.sock = None
            raise


def _default_connection_factory(
    scheme: str,
    original_host: str,
    connect_address: str,
    port: int,
    timeout: float,
) -> Any:
    if scheme.lower() == "https":
        return _PinnedHTTPSConnection(original_host, connect_address, port, timeout)
    return _PinnedHTTPConnection(original_host, connect_address, port, timeout)


class _BoundedRedirectHandler:
    """Bounded direct HTTP(S) fetcher with DNS-pinned connections."""

    def __init__(
        self,
        timeout: float,
        max_redirects: int,
        *,
        resolver: Callable[[str], Any] | None = None,
        connection_factory: Callable[[str, str, str, int, float], Any] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.resolver = resolver or _resolve_public_host
        self.connection_factory = connection_factory or _default_connection_factory

    def fetch(self, url: str, *, max_bytes: int) -> tuple[bytes, str]:
        current = validate_remote_url(url)
        for _ in range(self.max_redirects + 1):
            parsed = urlparse(current)
            host = parsed.hostname or ""
            resolved = self.resolver(host)
            # A custom resolver is still treated as untrusted input.  Validate
            # its address before handing it to the direct connection factory.
            connect_address = _resolved_address(resolved)
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            connection = self.connection_factory(parsed.scheme, host, connect_address, port, self.timeout)
            try:
                connection.request(
                    "GET",
                    _request_target(parsed),
                    headers={
                        "Host": _host_header(parsed),
                        "User-Agent": "AutoFoundry-ControlCenter/1",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                status = int(getattr(response, "status", 0))
                if status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location") if hasattr(response, "getheader") else None
                    if not location:
                        raise ValueError("Remote source redirect has no location.")
                    current = validate_remote_url(urljoin(current, str(location)))
                    continue
                if not 200 <= status < 300:
                    raise ValueError("Remote source request failed.")
                header_size = response.headers.get("Content-Length")
                if header_size is not None:
                    try:
                        declared_size = int(header_size)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Remote source Content-Length is invalid.") from exc
                    if declared_size < 0 or declared_size > max_bytes:
                        raise ValueError("Remote source exceeds the configured size limit.")
                data = bytearray()
                while True:
                    chunk = response.read(min(1024 * 1024, max_bytes - len(data) + 1))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Remote source exceeds the configured size limit.")
                return bytes(data), current
            finally:
                try:
                    response.close()
                except (UnboundLocalError, AttributeError):
                    pass
                try:
                    connection.close()
                except (OSError, AttributeError):
                    pass
        raise ValueError("Remote source exceeded the redirect limit.")


def fetch_public_url(url: str, *, max_bytes: int = DEFAULT_MAX_SOURCE_TOTAL) -> tuple[bytes, str]:
    """Fetch one bounded public URL; exposed for offline dependency injection."""

    return _BoundedRedirectHandler(DEFAULT_NETWORK_TIMEOUT, DEFAULT_MAX_REDIRECTS).fetch(url, max_bytes=max_bytes)


class CodexRequirementIntakePlanner:
    """Ask the cognitive Planner to turn an unstructured brief into a portfolio.

    The transport owns no durable requirement state.  It returns a candidate
    interpretation; :class:`LaunchManager` validates exact source coverage,
    assigns IDs, constructs RequirementRecords, and persists the accepted plan.
    """

    def __init__(
        self,
        codex_bin: str,
        state_root: Path,
        *,
        run: Callable[..., Any] | None = None,
        timeout_seconds: float = 900.0,
    ) -> None:
        self.codex_bin = str(codex_bin)
        self.state_root = Path(state_root)
        self._run = run or subprocess.run
        self.timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _prompt(
        *,
        intake_blocks: tuple[Mapping[str, str], ...],
        existing_plan: Mapping[str, Any] | None,
        data_room: str,
        document_refs: tuple[str, ...],
        skill_binding: Mapping[str, Any],
    ) -> str:
        payload = {
            "intakeBlocks": [dict(block) for block in intake_blocks],
            "existingPlan": dict(existing_plan) if existing_plan is not None else None,
            "dataRoom": data_room,
            "availableDocumentRefs": list(document_refs),
        }
        return (
            "You are the Requirement Portfolio Planner for Auto Foundry.\n"
            "Read the exact production skill first: "
            f"{skill_binding['skill_path']}/SKILL.md (skill {skill_binding['skill_version']}, "
            f"core {skill_binding['core_version']}, release {skill_binding['skill_sha256']}).\n"
            "Interpret the supplied raw business brief semantically. The UI fields are input blocks, "
            "not requirement boundaries. Never split by regex, headings, bullets, numbering, or a fixed "
            "template alone. Decide which independent business decisions/questions need their own durable "
            "RequirementRecord; keep steps of one decision together; merge duplicate phrasing; and treat "
            "explicit labels such as Requirement 1..5 as strong evidence, not a parser rule. Inspect the "
            "read-only data room when a document provides requirement context. Do not edit any file.\n"
            "Return exactly one JSON object with no markdown. Schema:\n"
            "{\"schemaVersion\":1,\"portfolioStrategy\":\"...\",\"requirements\":["
            "{\"candidateId\":\"C-001\",\"sourceSpans\":[{\"blockId\":\"INPUT-001\","
            "\"start\":0,\"end\":10}],\"documentRefs\":[],\"originalText\":null,"
            "\"businessObjective\":\"...\","
            "\"expectedAnalyticalOutputs\":[],\"expectedVisualOutputs\":[],\"dependencies\":[],"
            "\"dataNeeds\":[],\"ontologyNeeds\":[],\"preparedDataNeeds\":[],"
            "\"workingDefinitions\":[],\"limitations\":[],\"explicitPriority\":null,"
            "\"scope\":\"analytics\"}],\"groups\":[{\"members\":[\"C-001\"],"
            "\"rationale\":\"...\",\"sharedAnalysisIntent\":null,\"suggestedSpecialists\":[]}],"
            "\"unassignedContext\":[{\"blockId\":\"INPUT-001\",\"start\":10,\"end\":20,"
            "\"reason\":\"context only\"}]}\n"
            "Span offsets are Python character offsets into the exact block text. A requirement needs at "
            "least one source span or one exact availableDocumentRef. For a document-only requirement, set "
            "originalText to a faithful concise statement grounded in that document. Every non-whitespace "
            "character must be covered by at least one requirement span or unassignedContext span. Each "
            "new candidate must occur in exactly one group. For a continuation, groups must cover every "
            "existing requirement ID and every new candidate exactly once; you may preserve or revise prior "
            "grouping. Dependencies may name existing requirement IDs or new candidate IDs. Use a short "
            "read-only Python calculation if necessary to get exact offsets.\n\nINPUT JSON:\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    def plan_intake(
        self,
        *,
        intake_blocks: tuple[Mapping[str, str], ...],
        existing_plan: Mapping[str, Any] | None,
        data_room: str,
        document_refs: tuple[str, ...],
        role_cwd: Path,
        skill_binding: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.state_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="intake-planner-", dir=self.state_root) as temporary:
            output_path = Path(temporary) / "last-message.json"
            argv = [
                self.codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--output-last-message",
                str(output_path),
                "-",
            ]
            try:
                completed = self._run(
                    argv,
                    input=self._prompt(
                        intake_blocks=intake_blocks,
                        existing_plan=existing_plan,
                        data_room=data_room,
                        document_refs=document_refs,
                        skill_binding=skill_binding,
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(role_cwd),
                    timeout=self.timeout_seconds,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LaunchConflictError("Requirement Planner transport failed") from exc
            if int(getattr(completed, "returncode", 1)) != 0 or not output_path.is_file():
                raise LaunchConflictError("Requirement Planner did not return an interpretation")
            try:
                value = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LaunchConflictError("Requirement Planner returned invalid JSON") from exc
            if not isinstance(value, Mapping):
                raise LaunchConflictError("Requirement Planner response must be a JSON object")
            return dict(value)


class SubprocessRunner:
    """Start the public durable coordinator CLI for one run.

    Tests replace this object with a fake runner.  The request payload never
    controls executable, shell text, or flags.
    """

    def __init__(self, codex_bin: str = "codex", *, popen: Callable[..., Any] | None = None) -> None:
        self.codex_bin = str(codex_bin)
        self._popen = popen or subprocess.Popen

    def start(
        self,
        *,
        run_id: str,
        run_root: Path,
        manifest_path: Path,
        capacity: Mapping[str, int],
        coordinator_operation: str = "run",
    ) -> dict[str, Any]:
        control = run_root / "control_plane"
        control.mkdir(parents=True, exist_ok=True)
        if coordinator_operation not in {"run", "resume"}:
            raise ValueError("coordinator_operation must be run or resume")
        # The canonical coordinator spec is already materialized by
        # LaunchManager through RunCoordinator.start/from_persisted_spec.
        # The subprocess only enters the public CLI run/resume path; it never
        # invokes a generic root skill agent or constructs a second coordinator.
        raw_log = control / "coordinator.jsonl"
        stderr_log = control / "coordinator.stderr.log"
        # The operational server is commonly started with ``PYTHONPATH=src:.``.
        # Its child runs from the isolated run root, so relative entries would
        # no longer resolve to this checkout.  Keep only existing absolute
        # entries and prepend the checkout's source tree explicitly.
        checkout_src = Path(__file__).resolve().parents[2] / "src"
        python_paths = [str(checkout_src)]
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
            if not entry:
                continue
            candidate = Path(entry).expanduser()
            if candidate.is_absolute():
                value = str(candidate.resolve(strict=False))
                if value not in python_paths:
                    python_paths.append(value)
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = os.pathsep.join(python_paths)
        argv = [
            sys.executable,
            "-m",
            "auto_foundry_core.cli",
            "coordinator",
            coordinator_operation,
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
        ]
        with raw_log.open("ab") as stdout, stderr_log.open("ab") as stderr:
            process = self._popen(
                argv,
                stdout=stdout,
                stderr=stderr,
                cwd=str(run_root),
                env=child_env,
                start_new_session=True,
                shell=False,
            )
        monitor_id = "coordinator-" + uuid.uuid4().hex[:16]
        return {"monitorRunId": monitor_id, "pid": getattr(process, "pid", None), "argv": argv}


class LaunchManager:
    """Own upload, draft, bootstrap, and guarded runner operations."""

    def __init__(
        self,
        settings: LaunchSettings,
        *,
        repository: Any | None = None,
        runner: Any | None = None,
        fetcher: Any | None = None,
        intake_planner: Any | None = None,
    ) -> None:
        self.settings = settings
        self.uploads = UploadStore(settings)
        self.repository = repository
        self.runner = runner or SubprocessRunner(settings.codex_bin)
        self.intake_planner = (
            intake_planner
            or getattr(self.runner, "plan_intake", None)
            or CodexRequirementIntakePlanner(settings.codex_bin, Path(settings.state_root))
        )
        self.fetcher = fetcher or _BoundedRedirectHandler(DEFAULT_NETWORK_TIMEOUT, DEFAULT_MAX_REDIRECTS)
        self._lock = threading.RLock()

    @property
    def drafts_root(self) -> Path:
        return Path(self.settings.state_root) / "drafts"

    @property
    def status_root(self) -> Path:
        return Path(self.settings.state_root) / "statuses"

    def upload(self, stream: BinaryIO, *, filename: str, relative_path: str | None, content_length: int | None) -> UploadRecord:
        return self.uploads.stage(stream, filename=filename, relative_path=relative_path, content_length=content_length)

    def _known_run(self, run_id: str) -> tuple[str, Path, dict[str, Any]] | None:
        repository = self.repository
        if repository is None:
            return None
        try:
            record = repository.get(run_id)
        except (AttributeError, KeyError):
            record = None
        if record is None:
            return None
        state_path = getattr(record, "state_path", None)
        if state_path is None:
            return None
        state_path = Path(state_path)
        if state_path.is_symlink() or not state_path.is_file() or not is_within(state_path, (self.settings.runs_root,)):
            return None
        state = load_object(state_path)
        actual_id = safe_component(state.get("run_id"), "run_id")
        raw_root = state.get("run_root")
        if isinstance(raw_root, str) and raw_root:
            raw_path = Path(raw_root).expanduser()
            if not raw_path.is_absolute():
                return None
            run_root = raw_path.resolve(strict=False)
            try:
                reject_symlink_components(raw_path, self.settings.runs_root)
            except ValueError:
                return None
        else:
            run_root = state_path.parent.resolve(strict=False)
        if not run_root.is_dir() or run_root.is_symlink() or not is_within(run_root, (self.settings.runs_root,)):
            return None
        return actual_id, run_root, state

    def _canonical_sources(self, payload_sources: Any, *, mode: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
        errors: dict[str, str] = {}
        if payload_sources is None:
            payload_sources = []
        if not isinstance(payload_sources, list):
            return [], {"sources": "Sources must be a list."}
        if mode == "continue" and payload_sources:
            return [], {"sources": "Existing runs use an immutable shared data room; sources must be empty."}
        canonical: list[dict[str, Any]] = []
        # Source paths identify the user-supplied top-level entries.  Output
        # names identify the flattened data-room members.  A ZIP container's
        # own filename is not an output member, but its accepted children are.
        seen_source_paths: set[str] = set()
        seen_output_names: set[str] = set()
        accepted_total = 0
        physical_total = 0
        physical_entry_total = 0
        expanded_limit = _expanded_source_limit(self.settings)
        for index, raw in enumerate(payload_sources):
            key = f"sources[{index}]"
            if not isinstance(raw, Mapping):
                errors[key] = "Source entry must be an object."
                continue
            kind = str(raw.get("kind") or "").strip()
            try:
                if kind == "upload":
                    record = self.uploads.load(raw.get("uploadId"), verify=False)
                    relative = safe_relative_path(raw.get("relativePath") or record.relative_path)
                    if relative != record.relative_path:
                        raise ValueError("relativePath must match the staged upload")
                    source_key = _normalized_archive_name(relative)
                    if source_key in seen_source_paths:
                        raise ValueError("source path collides with another source")
                    seen_source_paths.add(source_key)
                    if _is_zip_name(relative):
                        snapshot_path, snapshot_size, snapshot_digest = self._snapshot_zip_source(
                            record.path,
                            Path(self.settings.state_root),
                            expected_size=record.size,
                            expected_sha256=record.sha256,
                            binding_error="staged upload changed while preparing",
                        )
                        try:
                            inspection = _inspect_zip_source(
                                snapshot_path,
                                max_total_bytes=expanded_limit,
                                read_members=True,
                                prior_total_bytes=physical_total,
                                prior_entry_count=physical_entry_total,
                            )
                            for member in inspection.members:
                                member_key = _normalized_archive_name(member.name)
                                if member_key in seen_output_names:
                                    raise ValueError(
                                        f"ZIP member {member.name!r} collides with an ordinary source or another ZIP member"
                                    )
                                seen_output_names.add(member_key)
                            accepted_total += inspection.expanded_size
                            physical_total += inspection.physical_expanded_size
                            physical_entry_total += inspection.physical_entry_count
                            canonical.append(
                                {
                                    "kind": "upload",
                                    "uploadId": record.upload_id,
                                    "relativePath": relative,
                                    "size": snapshot_size,
                                    "sha256": snapshot_digest,
                                    "expandedSize": inspection.expanded_size,
                                    "memberCount": inspection.member_count,
                                }
                            )
                        finally:
                            snapshot_path.unlink(missing_ok=True)
                    else:
                        record = self.uploads.load(raw.get("uploadId"))
                        output_key = _normalized_archive_name(relative)
                        if output_key in seen_output_names:
                            raise ValueError("source path collides with another source")
                        seen_output_names.add(output_key)
                        accepted_total += record.size
                        physical_total += record.size
                        canonical.append({"kind": "upload", "uploadId": record.upload_id, "relativePath": relative, "size": record.size, "sha256": record.sha256})
                elif kind == "local_path":
                    raw_path = Path(str(raw.get("path") or "")).expanduser()
                    if not raw_path.is_absolute():
                        raise ValueError("local_path must be absolute")
                    matched_root: Path | None = None
                    resolved = raw_path.resolve(strict=True)
                    for root in self.settings.source_roots:
                        if is_within(resolved, (root,)):
                            reject_symlink_components(raw_path, root)
                            matched_root = root
                            break
                    if matched_root is None or not resolved.is_file():
                        raise ValueError("local_path must be a regular file inside a configured source root")
                    relative = safe_relative_path(resolved.relative_to(matched_root).as_posix())
                    source_key = _normalized_archive_name(relative)
                    if source_key in seen_source_paths:
                        raise ValueError("source path collides with another source")
                    if not supported_extension(relative):
                        raise ValueError(f"unsupported source extension: {relative}")
                    seen_source_paths.add(source_key)
                    if _is_zip_name(relative):
                        snapshot_path, snapshot_size, snapshot_digest = self._snapshot_zip_source(
                            resolved,
                            Path(self.settings.state_root),
                            expected_size=None,
                            expected_sha256=None,
                            binding_error="local source changed while preparing",
                        )
                        try:
                            inspection = _inspect_zip_source(
                                snapshot_path,
                                max_total_bytes=expanded_limit,
                                read_members=True,
                                prior_total_bytes=physical_total,
                                prior_entry_count=physical_entry_total,
                            )
                            for member in inspection.members:
                                member_key = _normalized_archive_name(member.name)
                                if member_key in seen_output_names:
                                    raise ValueError(
                                        f"ZIP member {member.name!r} collides with an ordinary source or another ZIP member"
                                    )
                                seen_output_names.add(member_key)
                            accepted_total += inspection.expanded_size
                            physical_total += inspection.physical_expanded_size
                            physical_entry_total += inspection.physical_entry_count
                            canonical.append(
                                {
                                    "kind": "local_path",
                                    "path": str(resolved),
                                    "relativePath": relative,
                                    "size": snapshot_size,
                                    "sha256": snapshot_digest,
                                    "sourceRoot": str(matched_root),
                                    "expandedSize": inspection.expanded_size,
                                    "memberCount": inspection.member_count,
                                }
                            )
                        finally:
                            snapshot_path.unlink(missing_ok=True)
                    else:
                        size = resolved.stat().st_size
                        if size > self.settings.upload_limit_bytes:
                            raise ValueError("local source exceeds the configured per-file size limit")
                        digest = sha256_file(resolved)
                        output_key = _normalized_archive_name(relative)
                        if output_key in seen_output_names:
                            raise ValueError("source path collides with another source")
                        seen_output_names.add(output_key)
                        accepted_total += size
                        physical_total += size
                        canonical.append({"kind": "local_path", "path": str(resolved), "relativePath": relative, "size": size, "sha256": digest, "sourceRoot": str(matched_root)})
                elif kind == "remote_url":
                    url = validate_remote_url(raw.get("url"))
                    parsed = urlparse(url)
                    name = Path(parsed.path).name or "remote-source"
                    relative = safe_relative_path(name, label="remote filename")
                    source_key = _normalized_archive_name(relative)
                    if source_key in seen_source_paths:
                        raise ValueError("source path collides with another source")
                    if _is_zip_name(relative):
                        raise ValueError("Remote ZIP sources are not supported; use a local upload or path.")
                    if source_key in seen_output_names:
                        raise ValueError("source path collides with another source")
                    seen_source_paths.add(source_key)
                    seen_output_names.add(source_key)
                    canonical.append({"kind": "remote_url", "url": url, "relativePath": relative})
                else:
                    raise ValueError("source kind must be upload, local_path, or remote_url")
            except (OSError, ValueError, TypeError) as exc:
                errors[key] = str(exc)
        if len(canonical) > self.settings.max_source_count:
            errors["sources"] = "Too many source entries."
        if physical_total > expanded_limit or accepted_total > expanded_limit:
            errors["sources"] = f"Expanded source bytes exceed the configured aggregate limit ({expanded_limit})."
        return canonical, errors

    def _draft_path(self, draft_id: str) -> Path:
        draft_id = safe_component(draft_id, "draftId")
        path = self.drafts_root / f"{draft_id}.json"
        reject_symlink_components(path, Path(self.settings.state_root))
        return path

    def _status_path(self, draft_id: str) -> Path:
        draft_id = safe_component(draft_id, "draftId")
        path = self.status_root / f"{draft_id}.json"
        reject_symlink_components(path, Path(self.settings.state_root))
        return path

    @staticmethod
    def _fingerprint(unsigned: Mapping[str, Any]) -> str:
        return sha256_bytes(canonical_bytes(unsigned))

    def prepare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise LaunchValidationError({"payload": "Expected a JSON object."})
        errors: dict[str, str] = {}
        mode = str(payload.get("mode") or "").strip()
        if mode not in {"new", "continue"}:
            errors["mode"] = "Choose new or continue."
        intake = payload.get("intakeBlocks")
        if not isinstance(intake, list) or not intake:
            errors["intakeBlocks"] = "Enter a business brief, questions, or requirements."
            intake_blocks: list[dict[str, str]] = []
        else:
            intake_blocks = []
            total_bytes = 0
            if len(intake) > MAX_INTAKE_BLOCKS:
                errors["intakeBlocks"] = f"At most {MAX_INTAKE_BLOCKS} input blocks are allowed."
            for index, text in enumerate(intake[:MAX_INTAKE_BLOCKS]):
                if not isinstance(text, str) or not text.strip():
                    errors[f"intakeBlocks[{index}]"] = "Input blocks must contain text."
                else:
                    total_bytes += len(text.encode("utf-8"))
                    intake_blocks.append({"blockId": f"INPUT-{index + 1:03d}", "text": text})
            if total_bytes > MAX_INTAKE_TEXT_BYTES:
                errors["intakeBlocks"] = "The combined business brief is too large."
        raw_project_name = payload.get("projectName")
        project_name = str(raw_project_name or "")
        if mode == "new" and not project_name.strip():
            errors["projectName"] = "Project name is required for a new run."
        if len(project_name) > 140 or any(ord(char) < 32 or ord(char) == 127 for char in project_name):
            errors["projectName"] = "Project name must be at most 140 characters without control characters."
        effective: dict[str, int] | None = None
        target_run_id: str | None = None
        target_root: Path | None = None
        existing = None
        if mode == "continue":
            discoverable = str(payload.get("runId") or "").strip()
            existing = self._known_run(discoverable)
            if existing is None:
                errors["runId"] = "Select a discoverable run."
            else:
                target_run_id, target_root, _ = existing
                if self.settings.is_protected_run(target_run_id, target_root):
                    errors["runId"] = "Selected run is protected from operational continuation."
                effective_existing = _authoritative_capacity(target_root)
                if effective_existing is None:
                    errors["maxAgents"] = "Existing-run capacity is unavailable."
                else:
                    effective, capacity_error = _validate_capacity(payload, self.settings, continue_capacity=effective_existing)
                    if capacity_error:
                        errors["maxAgents"] = capacity_error
        else:
            effective, capacity_error = _validate_capacity(payload, self.settings)
            if capacity_error:
                errors["maxAgents"] = capacity_error
            target_run_id = "RUN-" + uuid.uuid4().hex[:16]
            target_root = self.settings.runs_root / target_run_id
        sources, source_errors = self._canonical_sources(payload.get("sources"), mode=mode)
        errors.update(source_errors)
        available_documents = self._available_document_refs({"sources": sources})
        has_document_archive = any(
            isinstance(source, Mapping)
            and isinstance(source.get("relativePath"), str)
            and Path(str(source["relativePath"])).suffix.lower() == ".zip"
            for source in sources
        )
        if not intake_blocks and (available_documents or has_document_archive):
            errors.pop("intakeBlocks", None)
        if errors:
            return {
                "valid": False,
                "prepared": False,
                "errors": errors,
                "effectiveCapacity": effective,
                "message": "Launch draft needs attention.",
            }
        assert effective is not None and target_run_id is not None and target_root is not None
        if mode == "new" and target_root.exists():
            # The UUID collision is extraordinarily unlikely, but fail closed
            # rather than ever reusing a pre-existing run directory.
            raise LaunchConflictError("Generated run root already exists")
        draft_id = "D-" + uuid.uuid4().hex
        unsigned: dict[str, Any] = {
            "schemaVersion": 2,
            "draftId": draft_id,
            "mode": mode,
            "projectName": project_name,
            "intakeBlocks": intake_blocks,
            "sources": sources,
            "effectiveCapacity": effective,
            "runId": target_run_id,
            "runRoot": str(target_root),
            "createdAt": utc_now(),
        }
        fingerprint = self._fingerprint(unsigned)
        draft = {**unsigned, "fingerprint": fingerprint, "status": "prepared"}
        atomic_write_json(self._draft_path(draft_id), draft)
        return {
            "valid": True,
            "prepared": True,
            "draftId": draft_id,
            "fingerprint": fingerprint,
            "runId": target_run_id,
            "runRoot": str(target_root),
            "summary": {"inputBlocks": len(intake_blocks), "sources": len(sources)},
            "effectiveCapacity": effective,
            "message": "Launch package prepared. Confirm the fingerprint to start the run.",
            "errors": {},
        }

    def _load_draft(self, draft_id: str, fingerprint: str | None = None) -> dict[str, Any]:
        draft = load_object(self._draft_path(draft_id))
        stored = draft.get("fingerprint")
        unsigned = {key: value for key, value in draft.items() if key not in {"fingerprint", "status"}}
        if not isinstance(stored, str) or self._fingerprint(unsigned) != stored:
            raise LaunchConflictError("Draft fingerprint is invalid")
        if fingerprint is not None and not secrets.compare_digest(stored, str(fingerprint)):
            raise LaunchConflictError("Draft fingerprint does not match")
        return draft

    def _safe_local_source(self, source: Mapping[str, Any]) -> Path:
        path = Path(str(source.get("path") or "")).expanduser()
        resolved = path.resolve(strict=True)
        for root in self.settings.source_roots:
            if is_within(resolved, (root,)):
                reject_symlink_components(path, root)
                if resolved.is_file() and supported_extension(source.get("relativePath", resolved.name)):
                    return resolved
        raise ValueError("local source is no longer inside a configured source root")

    def _snapshot_zip_source(
        self,
        source_path: Path,
        destination_parent: Path,
        *,
        expected_size: Any | None,
        expected_sha256: Any | None,
        binding_error: str,
    ) -> tuple[Path, int, str]:
        """Copy one mutable ZIP source to a private, hash-bound snapshot.

        The source is opened exactly once here.  All subsequent ZIP inventory
        and member reads use the snapshot, so a replacement after this point
        cannot alter the bytes that are packaged.
        """

        if expected_size is not None and (
            isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0
        ):
            raise ValueError(f"{binding_error}: prepared size binding is invalid")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or len(expected_sha256) != 64):
            raise ValueError(f"{binding_error}: prepared hash binding is invalid")
        snapshot_path: Path | None = None
        descriptor_fd: int | None = None
        try:
            destination_parent.mkdir(parents=True, exist_ok=True)
            descriptor_fd, raw_snapshot = tempfile.mkstemp(
                dir=destination_parent,
                prefix=".zip-snapshot-",
                suffix=".zip",
            )
            snapshot_path = Path(raw_snapshot)
            os.chmod(snapshot_path, 0o600)
            digest = hashlib.sha256()
            size = 0
            with source_path.open("rb") as source, os.fdopen(descriptor_fd, "wb") as target:
                descriptor_fd = None
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.settings.upload_limit_bytes:
                        raise ValueError("ZIP source exceeds the configured per-file size limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            calculated = digest.hexdigest()
            if expected_size is not None and size != expected_size:
                raise ValueError(binding_error)
            if expected_sha256 is not None and calculated != expected_sha256:
                raise ValueError(binding_error)
            return snapshot_path, size, calculated
        except Exception:
            if descriptor_fd is not None:
                try:
                    os.close(descriptor_fd)
                except OSError:
                    pass
            if snapshot_path is not None:
                snapshot_path.unlink(missing_ok=True)
            raise

    def _package_zip(self, draft: Mapping[str, Any], destination: Path) -> list[dict[str, Any]]:
        sources = draft.get("sources")
        if not isinstance(sources, list):
            raise ValueError("draft source list is invalid")
        if len(sources) > self.settings.max_source_count:
            raise ValueError("source count exceeds configured bound")
        destination.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        accepted_total = 0
        physical_total = 0
        physical_entry_total = 0
        names: set[str] = set()
        expanded_limit = _expanded_source_limit(self.settings)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive:
            # Requirement order is kept in the draft/plan.  Source order has
            # no analytical meaning, so sort it for a stable central directory
            # while preserving deterministic member order inside each ZIP.
            ordered_sources = sorted(
                sources,
                key=lambda item: _normalized_archive_name(str(item.get("relativePath") or ""))
                if isinstance(item, Mapping)
                else "",
            )
            for source in ordered_sources:
                if not isinstance(source, Mapping):
                    raise ValueError("draft source entry is invalid")
                relative = safe_relative_path(source.get("relativePath"), label="archive path")
                kind = source.get("kind")
                if kind == "upload":
                    record = self.uploads.load(
                        source.get("uploadId"),
                        verify=not _is_zip_name(relative),
                    )
                    source_path = record.path
                    size = record.size
                    digest = record.sha256
                    if source.get("size") != size or source.get("sha256") != digest:
                        raise ValueError("staged upload changed after prepare")
                elif kind == "local_path":
                    source_path = self._safe_local_source(source)
                    if _is_zip_name(relative):
                        size = 0
                        digest = None
                    else:
                        size = source_path.stat().st_size
                        digest = sha256_file(source_path)
                        if source.get("size") != size or source.get("sha256") != digest:
                            raise ValueError("local source changed after prepare")
                elif kind == "remote_url":
                    if _is_zip_name(relative):
                        raise ValueError("Remote ZIP sources are not supported; use a local upload or path.")
                    url = str(source.get("url"))
                    if hasattr(self.fetcher, "fetch"):
                        remaining = max(0, expanded_limit - physical_total)
                        data, final_url = self.fetcher.fetch(url, max_bytes=min(self.settings.upload_limit_bytes, remaining))
                    else:
                        remaining = max(0, expanded_limit - physical_total)
                        data, final_url = self.fetcher(url, max_bytes=min(self.settings.upload_limit_bytes, remaining))
                    size = len(data)
                    digest = sha256_bytes(data)
                    output_key = _normalized_archive_name(relative)
                    if output_key in names:
                        raise ValueError("source path collision")
                    names.add(output_key)
                    if physical_total + size > expanded_limit:
                        raise ValueError(f"Expanded source bytes exceed the configured aggregate limit ({expanded_limit}).")
                    if size > self.settings.upload_limit_bytes:
                        raise ValueError("remote source exceeds the configured per-file size limit")
                    archive.writestr(relative, data)
                    entries.append({"relativePath": relative, "kind": kind, "size": size, "sha256": digest, "source": final_url})
                    accepted_total += size
                    physical_total += size
                    continue
                if size < 0:
                    raise ValueError("source bytes cannot be negative")
                if size > self.settings.upload_limit_bytes and kind != "remote_url":
                    raise ValueError("source exceeds configured per-file bound")
                if _is_zip_name(relative):
                    binding_error = "staged upload changed after prepare" if kind == "upload" else "local source changed after prepare"
                    snapshot_path, size, digest = self._snapshot_zip_source(
                        source_path,
                        destination.parent,
                        expected_size=source.get("size"),
                        expected_sha256=source.get("sha256"),
                        binding_error=binding_error,
                    )
                    try:
                        inspection = _inspect_zip_source(
                            snapshot_path,
                            max_total_bytes=expanded_limit,
                            read_members=False,
                            prior_total_bytes=physical_total,
                            prior_entry_count=physical_entry_total,
                        )
                        expected_expanded = source.get("expandedSize")
                        expected_count = source.get("memberCount")
                        if expected_expanded is not None and expected_expanded != inspection.expanded_size:
                            raise ValueError("ZIP expanded member inventory changed after prepare")
                        if expected_count is not None and expected_count != inspection.member_count:
                            raise ValueError("ZIP member count changed after prepare")
                        try:
                            with zipfile.ZipFile(snapshot_path, "r") as source_archive:
                                for member in inspection.members:
                                    output_key = _normalized_archive_name(member.name)
                                    if output_key in names:
                                        raise ValueError(
                                            f"ZIP member {member.name!r} collides with an ordinary source or another ZIP member"
                                        )
                                    names.add(output_key)
                                    member_digest = hashlib.sha256()
                                    observed = 0
                                    output_info = zipfile.ZipInfo(member.name)
                                    output_info.compress_type = zipfile.ZIP_DEFLATED
                                    try:
                                        with source_archive.open(member.info, "r") as stream, archive.open(output_info, "w") as target:
                                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                                observed += len(chunk)
                                                member_digest.update(chunk)
                                                target.write(chunk)
                                    except Exception as exc:
                                        raise ValueError(
                                            f"ZIP member {member.name!r} failed CRC/content validation: {exc}"
                                        ) from exc
                                    if observed != member.size:
                                        raise ValueError(f"ZIP member {member.name!r} expanded size changed while reading")
                                    entries.append(
                                        {
                                            "relativePath": member.name,
                                            "kind": kind,
                                            "size": observed,
                                            "sha256": member_digest.hexdigest(),
                                            "source": relative,
                                        }
                                    )
                        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, UnicodeError) as exc:
                            raise ValueError(f"invalid ZIP archive {snapshot_path.name!r}: {exc}") from exc
                        accepted_total += inspection.expanded_size
                        physical_total += inspection.physical_expanded_size
                        physical_entry_total += inspection.physical_entry_count
                        continue
                    finally:
                        snapshot_path.unlink(missing_ok=True)
                output_key = _normalized_archive_name(relative)
                if output_key in names:
                    raise ValueError("source path collision")
                names.add(output_key)
                if physical_total + size > expanded_limit:
                    raise ValueError(f"Expanded source bytes exceed the configured aggregate limit ({expanded_limit}).")
                with source_path.open("rb") as stream:
                    info = zipfile.ZipInfo(relative)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with archive.open(info, "w") as target:
                        shutil.copyfileobj(stream, target, length=1024 * 1024)
                entries.append({"relativePath": relative, "kind": kind, "size": size, "sha256": digest})
                accepted_total += size
                physical_total += size
        if physical_total > expanded_limit or accepted_total > expanded_limit:
            raise ValueError(f"Expanded source bytes exceed the configured aggregate limit ({expanded_limit}).")
        return entries

    def _core_imports(self):
        try:
            from auto_foundry_core import (
                CoordinatorRunSpec,
                EntityResolutionWorkspace,
                ItemWorkspace,
                RequirementExecutionPlan,
                RequirementRecord,
                RequirementRunExtension,
                RequirementSupervisorWorkspace,
                ResolutionCapacity,
                RunCoordinator,
                RunContext,
                RunLifecycle,
            )
            from auto_foundry_core.coordinator import resolve_production_skill_binding
        except ImportError:
            repo_src = Path(__file__).resolve().parents[2] / "src"
            import sys

            if str(repo_src) not in sys.path:
                sys.path.insert(0, str(repo_src))
            from auto_foundry_core import (
                CoordinatorRunSpec,
                EntityResolutionWorkspace,
                ItemWorkspace,
                RequirementExecutionPlan,
                RequirementRecord,
                RequirementRunExtension,
                RequirementSupervisorWorkspace,
                ResolutionCapacity,
                RunCoordinator,
                RunContext,
                RunLifecycle,
            )
            from auto_foundry_core.coordinator import resolve_production_skill_binding
        return {
            "CoordinatorRunSpec": CoordinatorRunSpec,
            "EntityResolutionWorkspace": EntityResolutionWorkspace,
            "ItemWorkspace": ItemWorkspace,
            "RequirementExecutionPlan": RequirementExecutionPlan,
            "RequirementRecord": RequirementRecord,
            "RequirementRunExtension": RequirementRunExtension,
            "RequirementSupervisorWorkspace": RequirementSupervisorWorkspace,
            "ResolutionCapacity": ResolutionCapacity,
            "RunCoordinator": RunCoordinator,
            "RunContext": RunContext,
            "RunLifecycle": RunLifecycle,
            "resolve_production_skill_binding": resolve_production_skill_binding,
        }

    def _write_control(self, run_root: Path, filename: str, value: Mapping[str, Any]) -> Path:
        control = run_root / "control_center"
        control.mkdir(parents=True, exist_ok=True)
        path = control / filename
        atomic_write_json(path, dict(value))
        return path

    def _write_launch_artifact(
        self,
        run_root: Path,
        draft_id: str,
        filename: str,
        value: Mapping[str, Any],
    ) -> Path:
        """Write one immutable, fingerprint-bound launch artifact."""

        draft_component = safe_component(draft_id, "draftId")
        filename_component = safe_component(filename, "launch artifact")
        launches_root = run_root / "control_center" / "launches"
        artifact_root = launches_root / draft_component
        reject_symlink_components(artifact_root, run_root)
        if artifact_root.exists():
            if artifact_root.is_symlink() or not artifact_root.is_dir():
                raise LaunchConflictError("launch artifact directory is not safe")
        else:
            artifact_root.mkdir(parents=True, exist_ok=False)
        path = artifact_root / filename_component
        reject_symlink_components(path, run_root)
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise LaunchConflictError(f"launch artifact already exists: {filename_component}")
            try:
                existing = load_object(path)
            except ValueError as exc:
                raise LaunchConflictError(f"launch artifact is unreadable: {filename_component}") from exc
            if canonical_bytes(existing) != canonical_bytes(dict(value)):
                raise LaunchConflictError(f"launch artifact conflicts: {filename_component}")
            return path
        atomic_write_json(path, dict(value))
        return path

    def _write_control_once(self, run_root: Path, filename: str, value: Mapping[str, Any]) -> Path:
        """Keep a compatibility pointer/copy without overwriting history."""

        control = run_root / "control_center"
        reject_symlink_components(control, run_root)
        control.mkdir(parents=True, exist_ok=True)
        path = control / safe_component(filename, "control artifact")
        reject_symlink_components(path, run_root)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise LaunchConflictError(f"control artifact is not a regular file: {filename}")
            return path
        atomic_write_json(path, dict(value))
        return path

    def _validated_input_roots(self, run_root: Path, run_id: str) -> tuple[Path, ...]:
        """Read immutable input-root declarations from existing contexts."""

        candidates: list[Path] = []
        for family in ("requirements", "questions"):
            family_root = run_root / family
            if family_root.is_symlink() or not family_root.is_dir():
                continue
            try:
                children = [child for child in sorted(family_root.iterdir()) if child.is_dir() and not child.is_symlink()][:MAX_REQUIREMENT_RECORDS]
            except OSError:
                continue
            for child in children:
                context_path = child / "work" / "analysis_context.json"
                if context_path.is_symlink() or not context_path.is_file() or not is_within(context_path, (run_root,)):
                    continue
                try:
                    context = load_object(context_path)
                except ValueError:
                    continue
                if context.get("run_id") != run_id or context.get("run_root") != str(run_root):
                    continue
                raw_roots = context.get("input_roots")
                if not isinstance(raw_roots, list):
                    continue
                for raw_root in raw_roots:
                    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
                        continue
                    root = Path(raw_root).expanduser()
                    resolved = root.resolve(strict=False)
                    for allowed in self.settings.source_roots:
                        if is_within(resolved, (allowed,)):
                            try:
                                reject_symlink_components(root, allowed)
                            except ValueError:
                                continue
                            if resolved.is_dir() and not resolved.is_symlink() and resolved not in candidates:
                                candidates.append(resolved)
                            break
        return tuple(candidates)

    def _discover_existing_data_room(self, run_root: Path, run_id: str) -> dict[str, Any]:
        """Resolve an existing run's immutable archive/catalog safely."""

        # Operationally-created runs keep their immutable package inside the
        # run root.  Preserve this relative reference for the manifest while
        # still validating the lexical path and bounded hash before reuse.
        packaged = run_root / "inputs" / "data_room.zip"
        if packaged.is_file() and not packaged.is_symlink() and is_within(packaged, (run_root,)):
            try:
                size = packaged.stat().st_size
            except OSError as exc:
                raise LaunchConflictError("Existing run data room is unreadable") from exc
            if size > self.settings.max_source_total_bytes:
                raise LaunchConflictError("Existing run data room exceeds the configured bound")
            return {
                "archivePath": packaged.resolve(strict=True),
                "sha256": sha256_file(packaged),
                "size": size,
                "catalogPath": None,
                "inputRoots": (packaged.parent.resolve(strict=True),),
                "dataRoom": "inputs/data_room.zip",
            }

        input_roots = self._validated_input_roots(run_root, run_id)
        if not input_roots:
            raise LaunchConflictError("Existing run input roots are unavailable or outside source roots")
        catalog_root = run_root / "data_room" / "catalogs"
        if catalog_root.is_symlink() or not catalog_root.is_dir() or not is_within(catalog_root, (run_root,)):
            raise LaunchConflictError("Existing run data-room catalog directory is unavailable")
        try:
            catalog_paths = [
                child
                for child in sorted(catalog_root.iterdir())
                if child.suffix.lower() == ".json" and child.is_file() and not child.is_symlink()
            ][:MAX_CATALOG_FILES]
        except OSError as exc:
            raise LaunchConflictError("Existing run data-room catalogs are unreadable") from exc
        if not catalog_paths:
            raise LaunchConflictError("Existing run data-room catalog is unavailable")
        context_hashes: set[str] = set()
        for family in ("requirements", "questions"):
            family_root = run_root / family
            if family_root.is_symlink() or not family_root.is_dir():
                continue
            try:
                children = [child for child in sorted(family_root.iterdir()) if child.is_dir() and not child.is_symlink()][:MAX_REQUIREMENT_RECORDS]
            except OSError:
                continue
            for child in children:
                context_path = child / "work" / "analysis_context.json"
                if context_path.is_symlink() or not context_path.is_file():
                    continue
                try:
                    context = load_object(context_path)
                except ValueError:
                    continue
                source_identity = context.get("source_identity")
                if isinstance(source_identity, Mapping):
                    value = source_identity.get("content_hash") or source_identity.get("sha256")
                    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                        context_hashes.add(value.lower())
        archives: list[dict[str, Any]] = []
        for catalog_path in catalog_paths:
            try:
                payload = load_object(catalog_path)
            except ValueError:
                continue
            archive = payload.get("archive")
            if not isinstance(archive, Mapping):
                continue
            uri = archive.get("uri") or archive.get("path")
            if not isinstance(uri, str) or not Path(uri).expanduser().is_absolute():
                continue
            expected_hash = archive.get("content_hash") or archive.get("sha256") or payload.get("source_hash")
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
                continue
            expected_size = archive.get("size_bytes")
            if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
                continue
            raw_path = Path(uri).expanduser()
            resolved = raw_path.resolve(strict=False)
            matched_root: Path | None = None
            for source_root in self.settings.source_roots:
                if is_within(resolved, (source_root,)):
                    try:
                        reject_symlink_components(raw_path, source_root)
                    except ValueError:
                        continue
                    matched_root = source_root
                    break
            if matched_root is None or not resolved.is_file() or resolved.is_symlink():
                continue
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            if size != expected_size or size > self.settings.max_source_total_bytes:
                continue
            digest = sha256_file(resolved)
            if digest != expected_hash.lower():
                continue
            if context_hashes and digest not in context_hashes:
                continue
            if any(existing["path"] == resolved for existing in archives):
                continue
            archives.append({"path": resolved, "uri": uri, "sha256": digest, "size": size, "catalog": catalog_path})
        if len(archives) != 1:
            raise LaunchConflictError("Existing run does not have one authoritative immutable data-room archive")
        archive = archives[0]
        roots = list(input_roots)
        parent = archive["path"].parent
        if parent not in roots:
            roots.append(parent)
        return {
            "archivePath": archive["path"],
            "sha256": archive["sha256"],
            "size": archive["size"],
            "catalogPath": archive["catalog"],
            "inputRoots": tuple(roots),
            "dataRoom": archive["uri"],
        }

    @staticmethod
    def _available_document_refs(draft: Mapping[str, Any]) -> tuple[str, ...]:
        refs: list[str] = []
        raw_sources = draft.get("sources")
        if not isinstance(raw_sources, list):
            return ()
        for source in raw_sources:
            if not isinstance(source, Mapping):
                continue
            value = source.get("relativePath")
            if not isinstance(value, str) or not value:
                continue
            extension = Path(value).suffix.lower().lstrip(".")
            if extension in ZIP_MEMBER_DOCUMENT_EXTENSIONS:
                refs.append(value)
        return tuple(dict.fromkeys(refs))

    @staticmethod
    def _data_room_document_refs(
        draft: Mapping[str, Any],
        run_root: Path,
        data_room: str,
    ) -> tuple[str, ...]:
        refs = list(LaunchManager._available_document_refs(draft))
        archive_path = Path(data_room)
        if not archive_path.is_absolute():
            archive_path = run_root / archive_path
        if archive_path.is_file() and not archive_path.is_symlink() and archive_path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    for name in archive.namelist():
                        extension = Path(name).suffix.lower().lstrip(".")
                        if not name.endswith("/") and extension in ZIP_MEMBER_DOCUMENT_EXTENSIONS:
                            refs.append(name)
            except (OSError, zipfile.BadZipFile):
                # Existing external catalogs may use a .zip URI for an opaque
                # immutable archive. Text intake remains valid; it simply
                # cannot bind document-only requirements to unknown members.
                return tuple(dict.fromkeys(refs))
        return tuple(dict.fromkeys(refs))

    @staticmethod
    def _intake_blocks(draft: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
        raw = draft.get("intakeBlocks")
        if not isinstance(raw, list):
            raise LaunchConflictError("Launch intake blocks are unavailable")
        blocks: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in raw:
            if not isinstance(value, Mapping) or set(value) != {"blockId", "text"}:
                raise LaunchConflictError("Launch intake block is invalid")
            block_id = safe_component(value.get("blockId"), "blockId")
            text = value.get("text")
            if block_id in seen or not isinstance(text, str) or not text.strip():
                raise LaunchConflictError("Launch intake block is invalid")
            seen.add(block_id)
            blocks.append({"blockId": block_id, "text": text})
        if not blocks and not LaunchManager._available_document_refs(draft):
            raw_sources = draft.get("sources")
            has_document_archive = isinstance(raw_sources, list) and any(
                isinstance(source, Mapping)
                and isinstance(source.get("relativePath"), str)
                and Path(str(source["relativePath"])).suffix.lower() == ".zip"
                for source in raw_sources
            )
            if not has_document_archive:
                raise LaunchConflictError("Launch intake blocks are unavailable")
        return tuple(blocks)

    @staticmethod
    def _intake_span(
        value: Any,
        blocks: Mapping[str, str],
        *,
        label: str,
    ) -> tuple[str, int, int]:
        if not isinstance(value, Mapping) or set(value) != {"blockId", "start", "end"}:
            raise LaunchConflictError(f"{label} must be an exact source span")
        block_id = value.get("blockId")
        start = value.get("start")
        end = value.get("end")
        if (
            not isinstance(block_id, str)
            or block_id not in blocks
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > len(blocks[block_id])
        ):
            raise LaunchConflictError(f"{label} is outside its exact input block")
        return block_id, start, end

    def _materialize_intake_plan(
        self,
        api: Mapping[str, Any],
        draft: Mapping[str, Any],
        interpretation: Mapping[str, Any],
        *,
        parent_plan: Any | None = None,
        document_refs: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Validate a cognitive interpretation and build the exact durable plan."""

        allowed_top = {
            "schemaVersion",
            "portfolioStrategy",
            "requirements",
            "groups",
            "unassignedContext",
        }
        if set(interpretation) - allowed_top or interpretation.get("schemaVersion") != 1:
            raise LaunchConflictError("Requirement Planner response has an unsupported schema")
        raw_requirements = interpretation.get("requirements")
        raw_groups = interpretation.get("groups")
        raw_unassigned = interpretation.get("unassignedContext", [])
        strategy = interpretation.get("portfolioStrategy")
        if (
            not isinstance(raw_requirements, list)
            or not raw_requirements
            or len(raw_requirements) > MAX_REQUIREMENT_RECORDS
            or not isinstance(raw_groups, list)
            or not isinstance(raw_unassigned, list)
            or not isinstance(strategy, str)
            or not strategy.strip()
        ):
            raise LaunchConflictError("Requirement Planner response is incomplete")

        intake_blocks = self._intake_blocks(draft)
        block_text = {block["blockId"]: block["text"] for block in intake_blocks}
        block_order = {block["blockId"]: index for index, block in enumerate(intake_blocks)}
        covered: dict[str, list[tuple[int, int]]] = {block_id: [] for block_id in block_text}
        existing_records = tuple(parent_plan.input_records) if parent_plan is not None else ()
        used_ids = {record.requirement_id for record in existing_records}
        next_number = 1
        for item_id in used_ids:
            match = re.fullmatch(r"REQ-(\d+)", str(item_id))
            if match:
                next_number = max(next_number, int(match.group(1)) + 1)

        candidate_rows: list[tuple[str, Mapping[str, Any], list[tuple[str, int, int]]]] = []
        candidate_to_id: dict[str, str] = {}
        candidate_allowed = {
            "candidateId",
            "sourceSpans",
            "documentRefs",
            "originalText",
            "businessObjective",
            "expectedAnalyticalOutputs",
            "expectedVisualOutputs",
            "dependencies",
            "dataNeeds",
            "ontologyNeeds",
            "preparedDataNeeds",
            "workingDefinitions",
            "limitations",
            "explicitPriority",
            "scope",
            "decompositionRationale",
        }
        for index, raw in enumerate(raw_requirements):
            if not isinstance(raw, Mapping) or set(raw) - candidate_allowed:
                raise LaunchConflictError("Requirement Planner candidate is invalid")
            candidate_id = safe_component(raw.get("candidateId"), "candidateId")
            if candidate_id in candidate_to_id or candidate_id in used_ids:
                raise LaunchConflictError("Requirement Planner candidate IDs are not unique")
            raw_spans = raw.get("sourceSpans", [])
            raw_document_refs = raw.get("documentRefs", [])
            if not isinstance(raw_spans, list) or not isinstance(raw_document_refs, list):
                raise LaunchConflictError("Requirement Planner sources are invalid")
            available_document_refs = set(document_refs or self._available_document_refs(draft))
            if (
                not all(isinstance(value, str) and value in available_document_refs for value in raw_document_refs)
                or not raw_spans and not raw_document_refs
            ):
                raise LaunchConflictError("Every planned requirement needs exact text or document sources")
            spans = [
                self._intake_span(value, block_text, label=f"requirements[{index}].sourceSpans")
                for value in raw_spans
            ]
            for block_id, start, end in spans:
                covered[block_id].append((start, end))
            while f"REQ-{next_number:03d}" in used_ids:
                next_number += 1
            requirement_id = f"REQ-{next_number:03d}"
            next_number += 1
            used_ids.add(requirement_id)
            candidate_to_id[candidate_id] = requirement_id
            candidate_rows.append((candidate_id, raw, spans))

        for index, raw in enumerate(raw_unassigned):
            if not isinstance(raw, Mapping) or set(raw) != {"blockId", "start", "end", "reason"}:
                raise LaunchConflictError("Requirement Planner unassigned context is invalid")
            reason = raw.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise LaunchConflictError("Requirement Planner unassigned context needs a reason")
            block_id, start, end = self._intake_span(
                {key: raw[key] for key in ("blockId", "start", "end")},
                block_text,
                label=f"unassignedContext[{index}]",
            )
            covered[block_id].append((start, end))

        for block_id, text in block_text.items():
            intervals = sorted(covered[block_id])
            cursor = 0
            for position, character in enumerate(text):
                while cursor < len(intervals) and intervals[cursor][1] <= position:
                    cursor += 1
                if not character.isspace() and (
                    cursor >= len(intervals)
                    or intervals[cursor][0] > position
                    or intervals[cursor][1] <= position
                ):
                    raise LaunchConflictError(
                        f"Requirement Planner dropped source text from {block_id} at offset {position}"
                    )

        def text_list(raw: Mapping[str, Any], name: str) -> list[str]:
            value = raw.get(name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise LaunchConflictError(f"Requirement Planner field {name} must be a string list")
            return list(value)

        records: list[Any] = []
        for candidate_id, raw, spans in candidate_rows:
            ordered = sorted(spans, key=lambda value: (block_order[value[0]], value[1], value[2]))
            original_text = "\n\n".join(block_text[block_id][start:end] for block_id, start, end in ordered)
            document_refs = list(raw.get("documentRefs", []))
            if not original_text:
                original_text = raw.get("originalText")
                if not isinstance(original_text, str) or not original_text.strip():
                    raise LaunchConflictError("Document-only requirement needs faithful originalText")
            dependencies = text_list(raw, "dependencies")
            mapped_dependencies: list[str] = []
            existing_ids = {record.requirement_id for record in existing_records}
            for dependency in dependencies:
                mapped = candidate_to_id.get(dependency, dependency)
                if mapped not in used_ids and mapped not in existing_ids:
                    raise LaunchConflictError("Requirement Planner dependency names an unknown requirement")
                mapped_dependencies.append(mapped)
            objective = raw.get("businessObjective", "")
            scope = raw.get("scope", "analytics")
            if not isinstance(objective, str) or not isinstance(scope, str) or not scope.strip():
                raise LaunchConflictError("Requirement Planner objective or scope is invalid")
            source_spans = [
                {"blockId": block_id, "start": start, "end": end}
                for block_id, start, end in ordered
            ]
            value = {
                "requirement_id": candidate_to_id[candidate_id],
                "original_text": original_text,
                "explicit_priority": raw.get("explicitPriority"),
                "business_objective": objective,
                "expected_analytical_outputs": text_list(raw, "expectedAnalyticalOutputs"),
                "expected_visual_outputs": text_list(raw, "expectedVisualOutputs"),
                "dependencies": mapped_dependencies,
                "data_needs": text_list(raw, "dataNeeds"),
                "ontology_needs": text_list(raw, "ontologyNeeds"),
                "prepared_data_needs": text_list(raw, "preparedDataNeeds"),
                "working_definitions": text_list(raw, "workingDefinitions"),
                "limitations": text_list(raw, "limitations"),
                "scope": scope,
                "source_refs": [
                    f"control_center:intake:{span['blockId']}:{span['start']}-{span['end']}"
                    for span in source_spans
                ] + [f"data_room:{value}" for value in document_refs],
                "metadata": {
                    "intake_candidate_id": candidate_id,
                    "source_spans": source_spans,
                    "document_refs": document_refs,
                    "decomposition_rationale": str(raw.get("decompositionRationale") or ""),
                },
            }
            try:
                records.append(api["RequirementRecord"].from_dict(value))
            except (KeyError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Requirement Planner produced an invalid RequirementRecord") from exc

        all_records = existing_records + tuple(records)
        known_members = {record.requirement_id for record in all_records}
        groups: list[dict[str, Any]] = []
        flattened: list[str] = []
        for raw in raw_groups:
            if not isinstance(raw, Mapping) or set(raw) - {
                "members",
                "rationale",
                "sharedAnalysisIntent",
                "suggestedSpecialists",
            }:
                raise LaunchConflictError("Requirement Planner group is invalid")
            members = raw.get("members")
            specialists = raw.get("suggestedSpecialists", [])
            rationale = raw.get("rationale")
            shared = raw.get("sharedAnalysisIntent")
            if (
                not isinstance(members, list)
                or not members
                or not all(isinstance(member, str) for member in members)
                or not isinstance(rationale, str)
                or not rationale.strip()
                or shared is not None and not isinstance(shared, str)
                or not isinstance(specialists, list)
                or not all(isinstance(value, str) and value.strip() for value in specialists)
            ):
                raise LaunchConflictError("Requirement Planner group is incomplete")
            mapped_members = [candidate_to_id.get(member, member) for member in members]
            if any(member not in known_members for member in mapped_members):
                raise LaunchConflictError("Requirement Planner group names an unknown requirement")
            flattened.extend(mapped_members)
            groups.append(
                {
                    "requirement_ids": mapped_members,
                    "rationale": rationale,
                    "shared_analysis_intent": shared,
                    "suggested_specialists": specialists,
                }
            )
        if len(flattened) != len(set(flattened)) or set(flattened) != known_members:
            raise LaunchConflictError("Requirement Planner groups must cover every requirement exactly once")
        try:
            plan = api["RequirementExecutionPlan"](
                input_records=all_records,
                groups=tuple(groups),
                planner_ref="semantic-intake-planner",
                portfolio_strategy=strategy,
                revision=(parent_plan.revision + 1) if parent_plan is not None else 1,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Requirement Planner execution plan is invalid") from exc
        return {
            "records": tuple(records),
            "plan": plan,
            "interpretation": dict(interpretation),
        }

    def _plan_intake(
        self,
        api: Mapping[str, Any],
        draft: Mapping[str, Any],
        run_root: Path,
        *,
        data_room: str,
        parent_plan: Any | None = None,
    ) -> dict[str, Any]:
        skill_binding = api["resolve_production_skill_binding"](
            repo_root=self.settings.runtime_root,
            role_cwd=run_root,
        )
        document_refs = self._data_room_document_refs(draft, run_root, data_room)
        callback = getattr(self.intake_planner, "plan_intake", self.intake_planner)
        if not callable(callback):
            raise LaunchConflictError("Requirement Planner transport is unavailable")
        try:
            interpretation = callback(
                intake_blocks=self._intake_blocks(draft),
                existing_plan=parent_plan.to_dict() if parent_plan is not None else None,
                data_room=data_room,
                document_refs=document_refs,
                role_cwd=run_root,
                skill_binding=skill_binding,
            )
        except LaunchConflictError:
            raise
        except Exception as exc:
            raise LaunchConflictError("Requirement Planner transport failed") from exc
        if not isinstance(interpretation, Mapping):
            raise LaunchConflictError("Requirement Planner response must be an object")
        return self._materialize_intake_plan(
            api,
            draft,
            interpretation,
            parent_plan=parent_plan,
            document_refs=document_refs,
        )

    def _prepare_coordinator(
        self,
        bootstrap: Mapping[str, Any],
        run_root: Path,
        *,
        publisher: Callable[[Any], Any] | None = None,
    ) -> dict[str, Any]:
        """Materialize or quiescently publish/rebind the public coordinator."""

        api = self._core_imports()
        context = bootstrap.get("context")
        if not isinstance(context, api["RunContext"]):
            raise LaunchConflictError("Coordinator RunContext is unavailable")
        plan_path = api["RunLifecycle"].active_plan_path(context)
        if plan_path.is_symlink() or not plan_path.is_file():
            raise LaunchConflictError("Coordinator Planner plan is unavailable")
        plan = api["RequirementExecutionPlan"].from_dict(dict(bootstrap["plan"]))
        target_generation_id = bootstrap.get("coordinatorGenerationId")
        if target_generation_id is None:
            target_generation_id = api["RunLifecycle"].active_generation_id(context)
        elif not isinstance(target_generation_id, str) or not target_generation_id:
            raise LaunchConflictError("Coordinator target generation is invalid")
        target_planner_hash = bootstrap.get("coordinatorPlannerHash")
        if target_planner_hash is None:
            target_planner_hash = sha256_file(plan_path)
        elif not isinstance(target_planner_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", target_planner_hash):
            raise LaunchConflictError("Coordinator target Planner hash is invalid")
        skill_binding = api["resolve_production_skill_binding"](
            repo_root=self.settings.runtime_root,
            role_cwd=run_root,
        )
        spec = api["CoordinatorRunSpec"](
            run_id=context.run_id,
            generation_id=target_generation_id,
            planner_ref=plan.planner_ref,
            planner_hash=target_planner_hash,
            publication_policy={"enabled": False},
            codex_exec={
                "binary": self.settings.codex_bin,
                "sandbox": "workspace-write",
                "ephemeral": True,
                **skill_binding,
            },
        )
        control = run_root / "control_plane"
        spec_path = control / "coordinator_spec.json"
        if spec_path.is_symlink():
            raise LaunchConflictError("Coordinator spec cannot be a symlink")
        if spec_path.is_file():
            # Existing current specs may predate the exact production skill
            # binding.  Inspect their JSON without constructing a role
            # adapter, upgrade the same lineage quiescently, and only then
            # load the persisted coordinator normally.  Legacy G5 wrappers
            # are intentionally left to the public import/reopen path.
            needs_binding_upgrade = False
            persisted_spec = None
            raw_spec = load_object(spec_path)
            if "run_spec" not in raw_spec and "lineage_binding" not in raw_spec:
                try:
                    persisted_spec = api["CoordinatorRunSpec"].from_dict(raw_spec)
                except (KeyError, TypeError, ValueError) as exc:
                    raise LaunchConflictError("Coordinator specification is invalid") from exc
                persisted_codex = raw_spec.get("codex_exec")
                binding_fields = ("skill_path", "skill_version", "core_version", "skill_sha256")
                needs_binding_upgrade = not (
                    isinstance(persisted_codex, Mapping)
                    and all(persisted_codex.get(field) is not None for field in binding_fields)
                )
            if needs_binding_upgrade:
                assert persisted_spec is not None
                persisted_codex = dict(persisted_spec.codex_exec)
                # The migration entrypoint is intentionally binding-only.  Do
                # not replace a persisted binary/model/profile/timeout or
                # publication policy while repairing an old spec.
                if any(field in persisted_codex for field in binding_fields):
                    raise LaunchConflictError("Coordinator Codex binding is incomplete and cannot be upgraded")
                persisted_codex.update(skill_binding)
                upgrade_spec = api["CoordinatorRunSpec"](
                    run_id=persisted_spec.run_id,
                    generation_id=persisted_spec.generation_id,
                    planner_ref=persisted_spec.planner_ref,
                    planner_hash=persisted_spec.planner_hash,
                    role_dispatch_command=persisted_spec.role_dispatch_command,
                    publication_policy=persisted_spec.publication_policy,
                    codex_exec=persisted_codex,
                    lease_ttl_seconds=persisted_spec.lease_ttl_seconds,
                )
                coordinator = api["RunCoordinator"](context)
                coordinator.upgrade_and_rebind(upgrade_spec)
                if publisher is None:
                    spec = upgrade_spec
            coordinator = api["RunCoordinator"].from_persisted_spec(context)
            current = coordinator.status()
            if current.phase == "legacy_import_required":
                coordinator.reopen("control center imported legacy coordinator state")
            if publisher is None:
                # ``start`` is the public idempotent/rebind entrypoint for a
                # fresh launch.  Continuations use the quiescent transaction
                # below so Planner admission and coordinator lineage cannot
                # be observed half-published.
                coordinator.start(spec)
            else:
                publication = coordinator.publish_and_rebind(spec, publisher)
                publication_phase = getattr(publication, "phase", None)
                if publication_phase == "plan_rebind_pending":
                    raise LaunchConflictError("Coordinator plan rebind remains pending; continuation is retryable")
        elif spec_path.exists():
            raise LaunchConflictError("Coordinator spec is not a regular file")
        else:
            if publisher is not None:
                raise LaunchConflictError("Continuation coordinator spec is unavailable")
            coordinator = api["RunCoordinator"](context)
            coordinator.start(spec)
        return {
            # A freshly rebound coordinator is ready for the same durable
            # ``run`` loop used by both initial and continuation launches.
            "operation": "run",
            "spec": spec,
            "specPath": spec_path,
        }

    def _bootstrap_new(self, draft: Mapping[str, Any], zip_path: Path, run_root: Path) -> dict[str, Any]:
        api = self._core_imports()
        run_id = safe_component(draft.get("runId"), "run_id")
        run_root.mkdir(parents=True, exist_ok=False)
        inputs_root = run_root / "inputs"
        inputs_root.mkdir(parents=True, exist_ok=False)
        final_zip = inputs_root / "data_room.zip"
        shutil.copyfile(zip_path, final_zip)
        planned = self._plan_intake(
            api,
            draft,
            run_root,
            data_room="inputs/data_room.zip",
        )
        records = planned["records"]
        plan = planned["plan"]
        self._write_launch_artifact(
            run_root,
            str(draft["draftId"]),
            "intake_plan.json",
            {
                "schemaVersion": 1,
                "kind": "semantic_requirement_intake",
                "draftId": draft["draftId"],
                "fingerprint": draft["fingerprint"],
                "inputBlocks": list(draft["intakeBlocks"]),
                "interpretation": planned["interpretation"],
                "records": [record.to_dict() for record in records],
                "plan": plan.to_dict(),
            },
        )
        context = api["RunContext"](run_id, run_root, input_roots=(inputs_root,))
        lifecycle = api["RunLifecycle"].create(context, [record.requirement_id for record in records], mode="requirement")
        capacity = draft["effectiveCapacity"]
        resolution = api["EntityResolutionWorkspace"].create(
            context,
            capacity=api["ResolutionCapacity"](
                total_active=capacity["total"],
                entity_resolution=capacity["entityResolution"],
                analytical_owner=capacity["analyticalOwner"],
                specialist=capacity["specialist"],
            ),
        )
        items = tuple(api["ItemWorkspace"].create(context, record.requirement_id, mode="requirement", original_text=record.original_text) for record in records)
        api["RequirementSupervisorWorkspace"](context).save(plan)
        lifecycle.reconcile(items)
        return {
            "context": context,
            "records": records,
            "capacity": resolution.capacity.to_dict(),
            "plan": plan.to_dict(),
            "dataRoom": "inputs/data_room.zip",
            "intakePlan": f"control_center/launches/{draft['draftId']}/intake_plan.json",
        }

    def _continuation_intent_path(self, run_root: Path, draft_id: str) -> Path:
        draft_component = safe_component(draft_id, "draftId")
        path = run_root / "control_center" / "launches" / draft_component / "continuation_intent.json"
        reject_symlink_components(path, run_root)
        return path

    def _continuation_manifest_from_intent(
        self,
        draft: Mapping[str, Any],
        run_root: Path,
        intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Derive the exact continuation manifest without mutable post-append state."""

        draft_component = safe_component(draft.get("draftId"), "draftId")
        data_room = intent.get("dataRoom")
        data_room_sha256 = intent.get("dataRoomSha256")
        data_room_size = intent.get("dataRoomSize")
        input_roots = intent.get("inputRoots")
        if (
            not isinstance(data_room, str)
            or not data_room
            or not isinstance(data_room_sha256, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", data_room_sha256)
            or isinstance(data_room_size, bool)
            or not isinstance(data_room_size, int)
            or data_room_size < 0
            or not isinstance(input_roots, list)
            or not all(isinstance(value, str) and value for value in input_roots)
        ):
            raise LaunchConflictError("Continuation intent data-room binding is incomplete")
        if intent.get("capacity") != self._receipt_capacity(draft.get("effectiveCapacity")):
            raise LaunchConflictError("Continuation intent capacity binding is invalid")
        return {
            "schemaVersion": 2,
            "kind": "control_center_launch",
            "runId": draft["runId"],
            "runRoot": str(run_root),
            "projectName": draft.get("projectName", ""),
            "mode": "requirement",
            "intakeBlocks": list(draft["intakeBlocks"]),
            "intakePlan": f"control_center/launches/{draft_component}/continuation_intent.json",
            "capacity": draft["effectiveCapacity"],
            "dataRoom": data_room,
            "sources": [],
            "draftId": draft["draftId"],
            "fingerprint": draft["fingerprint"],
            "createdAt": draft["createdAt"],
            "dataRoomSha256": data_room_sha256,
            "dataRoomSize": data_room_size,
            "inputRoots": list(input_roots),
        }

    def _continuation_receipt_from_intent(
        self,
        draft: Mapping[str, Any],
        run_root: Path,
        intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Derive the exact continuation receipt paired with its manifest."""

        plan = intent.get("plan")
        if not isinstance(plan, Mapping):
            raise LaunchConflictError("Continuation intent plan is unavailable")
        raw_records = plan.get("input_records")
        if not isinstance(raw_records, list) or not all(isinstance(value, Mapping) for value in raw_records):
            raise LaunchConflictError("Continuation intent items are unavailable")
        item_ids: list[str] = []
        for value in raw_records:
            item_id = value.get("requirement_id")
            if not isinstance(item_id, str) or not item_id:
                raise LaunchConflictError("Continuation intent item ID is invalid")
            item_ids.append(item_id)
        draft_component = safe_component(draft.get("draftId"), "draftId")
        return {
            "schemaVersion": 1,
            "draftId": draft["draftId"],
            "fingerprint": draft["fingerprint"],
            "runId": draft["runId"],
            "runRoot": str(run_root),
            "manifest": f"control_center/launches/{draft_component}/launch_manifest.json",
            "dataRoom": intent["dataRoom"],
            "capacity": intent["capacity"],
            "items": item_ids,
            "createdAt": draft["createdAt"],
        }

    @staticmethod
    def _compare_existing_artifact(path: Path, expected: Mapping[str, Any], filename: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise LaunchConflictError(f"continuation artifact is not a regular file: {filename}")
        try:
            existing = load_object(path)
        except ValueError as exc:
            raise LaunchConflictError(f"continuation artifact is unreadable: {filename}") from exc
        if canonical_bytes(existing) != canonical_bytes(dict(expected)):
            raise LaunchConflictError(f"continuation artifact conflicts: {filename}")

    def _preflight_continuation_artifacts(
        self,
        draft: Mapping[str, Any],
        run_root: Path,
        *,
        intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Validate intent and exact artifacts before any core admission."""

        draft_component = safe_component(draft.get("draftId"), "draftId")
        artifact_root = run_root / "control_center" / "launches" / draft_component
        intent_path = artifact_root / "continuation_intent.json"
        reject_symlink_components(intent_path, run_root)
        artifact_paths = {
            filename: artifact_root / filename
            for filename in ("launch_manifest.json", "launch_receipt.json")
        }
        for path in artifact_paths.values():
            reject_symlink_components(path, run_root)
        has_artifacts = any(path.exists() or path.is_symlink() for path in artifact_paths.values())

        if intent is None:
            if intent_path.exists() or intent_path.is_symlink():
                try:
                    intent = load_object(intent_path)
                except ValueError as exc:
                    raise LaunchConflictError("Continuation intent is unreadable") from exc
            elif has_artifacts:
                raise LaunchConflictError("Continuation artifacts require a preceding immutable intent")
            else:
                return None
        else:
            if not intent_path.exists() or intent_path.is_symlink():
                raise LaunchConflictError("Continuation intent is unavailable")
            try:
                persisted_intent = load_object(intent_path)
            except ValueError as exc:
                raise LaunchConflictError("Continuation intent is unreadable") from exc
            if canonical_bytes(persisted_intent) != canonical_bytes(dict(intent)):
                raise LaunchConflictError("Continuation intent changed during launch")
            intent = persisted_intent

        assert intent is not None
        # This read-only validation binds the intent to the current immutable
        # parent, data-room declaration, capacity, and cumulative plan.
        self._continue_plan(draft, run_root, intent=intent)
        expected_manifest = self._continuation_manifest_from_intent(draft, run_root, intent)
        expected_receipt = self._continuation_receipt_from_intent(draft, run_root, intent)
        for filename, expected in (
            ("launch_manifest.json", expected_manifest),
            ("launch_receipt.json", expected_receipt),
        ):
            path = artifact_paths[filename]
            if path.exists() or path.is_symlink():
                self._compare_existing_artifact(path, expected, filename)
        return dict(intent)

    def _continue_plan(
        self,
        draft: Mapping[str, Any],
        run_root: Path,
        *,
        intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build an exact cumulative continuation plan without publishing it."""

        api = self._core_imports()
        run_id = safe_component(draft.get("runId"), "run_id")
        existing_data_room = self._discover_existing_data_room(run_root, run_id)
        context = api["RunContext"](run_id, run_root, input_roots=existing_data_room["inputRoots"])
        lifecycle = api["RunLifecycle"].load(context)
        if lifecycle.snapshot.mode != "requirement":
            raise LaunchConflictError("Selected run is not Requirement Mode")
        parent_plan = api["RequirementSupervisorWorkspace"](context).load()
        parent_state_hash = str(lifecycle.snapshot.manifest_hash)
        parent_plan_hash = sha256_file(lifecycle.plan_path)
        expected_capacity = self._receipt_capacity(draft.get("effectiveCapacity"))
        authoritative_capacity = _authoritative_capacity(run_root)
        if authoritative_capacity is None or self._receipt_capacity(authoritative_capacity) != expected_capacity:
            raise LaunchConflictError("Existing-run capacity changed since the draft was prepared")

        if intent is not None:
            if (
                intent.get("draftId") != draft.get("draftId")
                or intent.get("fingerprint") != draft.get("fingerprint")
                or intent.get("runId") != run_id
                or intent.get("runRoot") != str(run_root)
            ):
                raise LaunchConflictError("Continuation intent does not match this draft")
            raw_records = intent.get("records")
            raw_plan = intent.get("plan")
            raw_interpretation = intent.get("interpretation")
            raw_added_ids = intent.get("addedItemIds")
            generation_id = str(intent.get("generationId") or "")
            expected_parent_state_hash = str(intent.get("parentStateHash") or "")
            expected_parent_plan_hash = str(intent.get("parentPlanHash") or "")
            if (
                not isinstance(raw_records, list)
                or not isinstance(raw_plan, Mapping)
                or not isinstance(raw_interpretation, Mapping)
                or not isinstance(raw_added_ids, list)
                or not generation_id
            ):
                raise LaunchConflictError("Continuation intent is incomplete")
            try:
                normalized_records: list[dict[str, Any]] = []
                for value in raw_records:
                    if not isinstance(value, Mapping):
                        raise TypeError("record must be an object")
                    normalized = dict(value)
                    normalized["requirement_id"] = safe_component(
                        normalized.get("requirement_id"), "requirement_id"
                    )
                    normalized_records.append(normalized)
                records = tuple(api["RequirementRecord"].from_dict(value) for value in normalized_records)
            except (KeyError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Continuation intent records are invalid") from exc
            if tuple(raw_added_ids) != tuple(record.requirement_id for record in records):
                raise LaunchConflictError("Continuation intent item IDs do not match its records")
            plan = api["RequirementExecutionPlan"].from_dict(dict(raw_plan))
            metadata = lifecycle.generation_metadata
            active_retry = bool(
                metadata is not None
                and parent_state_hash != str(intent.get("parentStateHash") or "")
                and parent_plan_hash != str(intent.get("parentPlanHash") or "")
                and metadata.generation_id == generation_id
                and tuple(metadata.added_item_ids) == tuple(record.requirement_id for record in records)
                and parent_plan.to_dict() == plan.to_dict()
            )
            if not active_retry:
                rebuilt = self._materialize_intake_plan(
                    api,
                    draft,
                    raw_interpretation,
                    parent_plan=parent_plan,
                    document_refs=self._data_room_document_refs(
                        draft,
                        run_root,
                        existing_data_room["dataRoom"],
                    ),
                )
                if (
                    [record.to_dict() for record in records]
                    != [record.to_dict() for record in rebuilt["records"]]
                    or plan.to_dict() != rebuilt["plan"].to_dict()
                ):
                    raise LaunchConflictError("Continuation intent does not match its semantic intake plan")
            if (
                intent.get("capacity") != expected_capacity
                or intent.get("dataRoom") != existing_data_room["dataRoom"]
                or intent.get("dataRoomSha256") != existing_data_room["sha256"]
                or intent.get("dataRoomSize") != existing_data_room["size"]
                or intent.get("inputRoots") != [str(value) for value in existing_data_room["inputRoots"]]
            ):
                raise LaunchConflictError("Continuation intent data-room or capacity binding changed")
        else:
            planned = self._plan_intake(
                api,
                draft,
                run_root,
                data_room=existing_data_room["dataRoom"],
                parent_plan=parent_plan,
            )
            records = planned["records"]
            plan = planned["plan"]
            raw_interpretation = planned["interpretation"]
            metadata = lifecycle.generation_metadata
            ordinal = (metadata.generation_ordinal + 1) if metadata is not None else 2
            generation_id = f"G-{ordinal:04d}"
            expected_parent_state_hash = parent_state_hash
            expected_parent_plan_hash = parent_plan_hash

        if parent_state_hash != expected_parent_state_hash or parent_plan_hash != expected_parent_plan_hash:
            metadata = lifecycle.generation_metadata
            active_matches = bool(
                metadata is not None
                and metadata.generation_id == generation_id
                and tuple(metadata.added_item_ids) == tuple(record.requirement_id for record in records)
                and parent_plan.to_dict() == plan.to_dict()
            )
            if not active_matches:
                raise LaunchConflictError("Continuation parent changed since the durable intent was prepared")

        return {
            "api": api,
            "context": context,
            "lifecycle": lifecycle,
            "parentPlan": parent_plan,
            "records": records,
            "plan": plan,
            "interpretation": dict(raw_interpretation),
            "generationId": generation_id,
            "parentStateHash": expected_parent_state_hash,
            "parentPlanHash": expected_parent_plan_hash,
            "dataRoom": existing_data_room,
            "capacity": expected_capacity,
        }

    @staticmethod
    def _receipt_capacity(value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise LaunchConflictError("Continuation capacity is unavailable")
        keys = ("total", "entityResolution", "analyticalOwner", "specialist")
        try:
            values = {key: int(value[key]) for key in keys}
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise LaunchConflictError("Continuation capacity is invalid") from exc
        if any(isinstance(value[key], bool) or values[key] < 0 for key in keys):
            raise LaunchConflictError("Continuation capacity is invalid")
        return {
            "total_active": values["total"],
            "entity_resolution": values["entityResolution"],
            "analytical_owner": values["analyticalOwner"],
            "specialist": values["specialist"],
        }

    def _ensure_continue_intent(self, draft: Mapping[str, Any], run_root: Path) -> dict[str, Any]:
        path = self._continuation_intent_path(run_root, str(draft.get("draftId")))
        if path.exists() or path.is_symlink():
            try:
                intent = load_object(path)
            except ValueError as exc:
                raise LaunchConflictError("Continuation intent is unreadable") from exc
            self._continue_plan(draft, run_root, intent=intent)
            return intent
        built = self._continue_plan(draft, run_root)
        intent = {
            "schemaVersion": 1,
            "kind": "control_center_continuation_intent",
            "draftId": draft["draftId"],
            "fingerprint": draft["fingerprint"],
            "runId": draft["runId"],
            "runRoot": str(run_root),
            "parentStateHash": built["parentStateHash"],
            "parentPlanHash": built["parentPlanHash"],
            "generationId": built["generationId"],
            "records": [record.to_dict() for record in built["records"]],
            "addedItemIds": [record.requirement_id for record in built["records"]],
            "plan": built["plan"].to_dict(),
            "interpretation": built["interpretation"],
            "dataRoom": built["dataRoom"]["dataRoom"],
            "dataRoomSha256": built["dataRoom"]["sha256"],
            "dataRoomSize": built["dataRoom"]["size"],
            "inputRoots": [str(value) for value in built["dataRoom"]["inputRoots"]],
            "capacity": built["capacity"],
            "createdAt": utc_now(),
        }
        self._write_launch_artifact(run_root, str(draft["draftId"]), "continuation_intent.json", intent)
        return intent

    def _bootstrap_continue(self, draft: Mapping[str, Any], run_root: Path, intent: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Publish or retry one exact generation through the coordinator transaction."""

        built = self._continue_plan(draft, run_root, intent=intent)
        api = built["api"]
        plan_payload = built["plan"].to_dict()
        published: dict[str, Any] = {}

        def publish(target_spec: Any) -> Any:
            # The coordinator owns the sole quiescent boundary.  This public
            # revision callback is invoked only after it has recorded the
            # pending target; retries therefore converge through the same
            # RequirementRunExtension admission API.
            extension = api["RequirementRunExtension"].revise(
                built["context"],
                plan=built["plan"],
                generation_id=built["generationId"],
            )
            published["extension"] = extension
            return extension

        coordinator = self._prepare_coordinator(
            {
                "context": built["context"],
                "plan": plan_payload,
                "coordinatorGenerationId": built["generationId"],
                "coordinatorPlannerHash": _planner_plan_hash(plan_payload),
            },
            run_root,
            publisher=publish,
        )
        extension = published.get("extension")
        generation_id = getattr(extension, "generation_id", built["generationId"])
        return {
            "context": built["context"],
            "records": built["records"],
            "capacity": built["capacity"],
            "plan": built["plan"].to_dict(),
            "generationId": generation_id,
            "revision": extension,
            "dataRoom": built["dataRoom"]["dataRoom"],
            "dataRoomSha256": built["dataRoom"]["sha256"],
            "dataRoomSize": built["dataRoom"]["size"],
            "inputRoots": [str(value) for value in built["dataRoom"]["inputRoots"]],
            "coordinator": coordinator,
        }

    def execute(self, *, draft_id: str, fingerprint: str, confirmed: bool) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Launch commands are disabled; start the loopback server with --enable-launch.")
        if confirmed is not True:
            raise LaunchValidationError({"confirmed": "A second explicit confirmation is required."}, "Launch confirmation is required")
        with self._lock:
            draft = self._load_draft(draft_id, fingerprint)
            prior = self.status(draft_id)
            if prior.get("status") in {"accepted", "running", "completed", "failed"}:
                return prior
            if prior.get("status") == "starting" and draft.get("mode") != "continue":
                return prior
            if draft.get("mode") not in {"new", "continue"}:
                raise LaunchValidationError({"mode": "Unsupported launch mode."})
            run_root = Path(str(draft.get("runRoot"))).resolve(strict=False)
            if draft.get("mode") == "new":
                if not is_within(run_root, (self.settings.runs_root,)) or run_root.exists() or run_root.is_symlink():
                    raise LaunchConflictError("Target run root is not an unused child of runs_root")
            elif not is_within(run_root, (self.settings.runs_root,)) or not run_root.is_dir() or run_root.is_symlink():
                raise LaunchConflictError("Existing run root is unavailable or outside runs_root")
            continuation_intent: dict[str, Any] | None = None
            if draft.get("mode") == "continue":
                if self.settings.is_protected_run(str(draft.get("runId")), run_root):
                    raise LaunchConflictError("Selected run is protected from operational continuation")
                continuation_intent = self._preflight_continuation_artifacts(draft, run_root)
            staging_root = Path(self.settings.state_root) / "launch-staging" / safe_component(draft_id, "draftId")
            if staging_root.exists() or staging_root.is_symlink():
                raise LaunchConflictError("Launch staging directory already exists")
            staging_root.mkdir(parents=True, exist_ok=False)
            status_payload: dict[str, Any] = {
                "draftId": draft_id,
                "fingerprint": fingerprint,
                "status": "starting",
                "runId": draft["runId"],
                "runRoot": str(run_root),
                "startedAt": utc_now(),
                "message": "Materialising the immutable launch package.",
            }
            atomic_write_json(self._status_path(draft_id), status_payload)
            runner_started = False
            try:
                if draft.get("mode") == "new":
                    zip_path = staging_root / "data_room.zip"
                    entries = self._package_zip(draft, zip_path)
                    bootstrap = self._bootstrap_new(draft, zip_path, run_root)
                else:
                    entries = []
                    intent = continuation_intent or self._ensure_continue_intent(draft, run_root)
                    # The intent is now durable.  Validate any pre-existing
                    # artifact bytes against the exact immutable draft+intent
                    # before RequirementRunExtension can append a generation.
                    intent = self._preflight_continuation_artifacts(draft, run_root, intent=intent)
                    assert intent is not None
                    manifest = self._continuation_manifest_from_intent(draft, run_root, intent)
                    receipt = self._continuation_receipt_from_intent(draft, run_root, intent)
                    bootstrap = self._bootstrap_continue(draft, run_root, intent=intent)
                if draft.get("mode") == "new":
                    data_room = bootstrap.get("dataRoom")
                    if not isinstance(data_room, str) or not data_room:
                        raise LaunchConflictError("Launch data-room reference is unavailable")
                    manifest = {
                        "schemaVersion": 2,
                        "kind": "control_center_launch",
                        "runId": draft["runId"],
                        "runRoot": str(run_root),
                        "projectName": draft.get("projectName", ""),
                        "mode": "requirement",
                        "intakeBlocks": list(draft["intakeBlocks"]),
                        "intakePlan": bootstrap.get("intakePlan"),
                        "capacity": draft["effectiveCapacity"],
                        "dataRoom": data_room,
                        "sources": entries,
                        "draftId": draft_id,
                        "fingerprint": fingerprint,
                        "createdAt": draft["createdAt"],
                    }
                    for key in ("dataRoomSha256", "dataRoomSize", "inputRoots"):
                        if key in bootstrap:
                            manifest[key] = bootstrap[key]
                    receipt = None
                manifest_path = self._write_launch_artifact(run_root, draft_id, "launch_manifest.json", manifest)
                if draft.get("mode") == "new":
                    self._write_control_once(run_root, "launch_manifest.json", manifest)
                if receipt is None:
                    receipt = {
                        "schemaVersion": 1,
                        "draftId": draft_id,
                        "fingerprint": fingerprint,
                        "runId": draft["runId"],
                        "runRoot": str(run_root),
                        "manifest": manifest_path.relative_to(run_root).as_posix(),
                        "dataRoom": bootstrap["dataRoom"],
                        "capacity": bootstrap["capacity"],
                        "items": [record["requirement_id"] for record in bootstrap["plan"]["input_records"]],
                        # Bind the receipt to the immutable draft rather than
                        # the retry attempt so exact continuation retries can
                        # reuse the committed bytes.
                        "createdAt": draft["createdAt"],
                    }
                self._write_launch_artifact(run_root, draft_id, "launch_receipt.json", receipt)
                if draft.get("mode") == "new":
                    self._write_control_once(run_root, "launch_receipt.json", receipt)
                coordinator = bootstrap.get("coordinator") if draft.get("mode") == "continue" else None
                if not isinstance(coordinator, Mapping):
                    coordinator = self._prepare_coordinator(
                        bootstrap,
                        run_root,
                    )
                runner_started = True
                runner_result = self.runner.start(
                    run_id=str(draft["runId"]),
                    run_root=run_root,
                    manifest_path=manifest_path,
                    capacity=draft["effectiveCapacity"],
                    coordinator_operation=coordinator["operation"],
                )
                allowed_runner = {key: runner_result[key] for key in ("monitorRunId", "pid") if isinstance(runner_result, Mapping) and key in runner_result}
                status_payload.update(allowed_runner)
                status_payload.update({"status": "accepted", "message": "Run initialized and Planner process accepted.", "acceptedAt": utc_now()})
                atomic_write_json(self._status_path(draft_id), status_payload)
                return self.status(draft_id)
            except Exception as exc:
                if draft.get("mode") == "continue" and not runner_started:
                    status_payload.update({"status": "starting", "message": f"Continuation is retryable: {str(exc)[:240]}"})
                else:
                    status_payload.update({"status": "failed", "message": str(exc)[:300], "completedAt": utc_now()})
                atomic_write_json(self._status_path(draft_id), status_payload)
                raise
            finally:
                shutil.rmtree(staging_root, ignore_errors=True)

    def status(self, draft_id: str) -> dict[str, Any]:
        try:
            value = load_object(self._status_path(draft_id))
        except (OSError, ValueError):
            try:
                draft = self._load_draft(draft_id)
            except Exception:
                return {"draftId": draft_id, "status": "unknown", "message": "Launch status unavailable."}
            return {"draftId": draft_id, "fingerprint": draft.get("fingerprint"), "status": "prepared", "runId": draft.get("runId"), "runRoot": draft.get("runRoot"), "message": "Launch package is prepared and awaiting confirmation."}
        allowed = ("draftId", "fingerprint", "status", "runId", "runRoot", "monitorRunId", "message", "startedAt", "acceptedAt", "completedAt", "pid")
        return {key: value[key] for key in allowed if key in value}


__all__ = [
    "LaunchError",
    "LaunchValidationError",
    "LaunchConflictError",
    "LockedLaunchError",
    "LaunchManager",
    "LaunchSettings",
    "SubprocessRunner",
    "UploadRecord",
    "UploadStore",
    "capacity_for_total",
    "default_codex_binary",
    "fetch_public_url",
    "validate_remote_url",
]
