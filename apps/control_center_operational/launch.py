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
import signal
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
import re
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping, MutableMapping
from urllib.parse import urljoin, urlparse

try:  # pragma: no cover - supported macOS/POSIX hosts provide flock
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX hosts fail closed below
    fcntl = None  # type: ignore[assignment]


# These are retained as a descriptive catalog for the UI and remote-source
# policy.  Local uploads, configured local paths, and validated ZIP members do
# not use an extension allowlist as an admission gate.
SUPPORTED_EXTENSIONS = frozenset(
    {
        "csv",
        "tsv",
        "json",
        "jsonl",
        "ndjson",
        "xlsx",
        "parquet",
        "db",
        "sqlite",
        "sqlite3",
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
# This descriptive set is used for planner/document hints only.  ZIP members
# are admitted by safe archive structure and remain opaque when the core does
# not recognise their format (including nested ZIPs).
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
# Safe local/upload archives are not rejected solely because they contain more
# members than an arbitrary historical count.  Callers and tests may still set
# either knob explicitly when a bounded intake is desired.  Structural ZIP
# checks below (central-directory size, paths, duplicate names, compression,
# CRC/content reads, and available disk) remain mandatory regardless of count.
MAX_ZIP_MEMBER_COUNT: int | None = None
# Keep the physical-entry knob separate so a caller can bound directory and
# ignored-metadata records before semantic filtering without changing the
# default unbounded admission policy.
MAX_ZIP_PHYSICAL_ENTRY_COUNT: int | None = None
# No arbitrary business-size ceilings apply to normal local/upload/package
# flows.  Tests or callers may still set these optional knobs explicitly when
# they need a bounded fixture; structural ZIP defenses remain below.
MAX_ZIP_MEMBER_BYTES: int | None = None
MAX_ZIP_TOTAL_BYTES: int | None = None
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
# Remote fetches retain an explicit bounded network budget even though local
# uploads and paths are disk/resource checked instead of capped by bytes.
DEFAULT_UPLOAD_LIMIT = 512 * 1024 * 1024
DEFAULT_MAX_SOURCE_COUNT: int | None = None
DEFAULT_MAX_SOURCE_TOTAL = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_NETWORK_TIMEOUT = 15.0
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_PREPARATION_IDEMPOTENCY_KEY = 128
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
PLANNER_DOCUMENT_EXTENSIONS = frozenset(
    set(ZIP_MEMBER_DOCUMENT_EXTENSIONS) | {"csv", "tsv", "xlsx"}
)
MAX_PLANNER_DOCUMENT_EXCERPTS = 128
MAX_PLANNER_DOCUMENT_EXCERPT_BYTES = 128 * 1024
# Aggregate ingestion budgets bound one planner/catalog pass, including when a
# safe archive contains thousands of individually hostile PDFs.
MAX_CATALOG_PARSED_PDFS = 32
MAX_CATALOG_PDF_TOTAL_WALL_SECONDS = 30.0
MAX_CATALOG_PDF_TOTAL_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_CATALOG_NORMALIZED_TEXT_BYTES = 64 * 1024 * 1024
_PROCESS_GROUP_TOKEN_ENV = "AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN"
_PROCESS_GROUP_TOKEN_BYTES = 16
_SUPERVISOR_STARTUP_TOKEN_ENV = "AUTO_FOUNDRY_SUPERVISOR_STARTUP_TOKEN"
_CHECKOUT_SRC_ENV = "AUTO_FOUNDRY_CHECKOUT_SRC"
SUPERVISOR_READY_FILENAME = "supervisor_ready.json"
SUPERVISOR_HEARTBEAT_FILENAME = "supervisor_heartbeat.json"
SUPERVISOR_EXIT_FILENAME = "supervisor_exit.json"
# Readiness is deliberately bounded: a slow but live child remains in the
# recoverable ``starting`` state and is never killed by this timeout.
SUPERVISOR_STARTUP_TIMEOUT_SECONDS = 15.0
SUPERVISOR_STARTUP_POLL_SECONDS = 0.05
SUPERVISOR_STALE_STARTING_SECONDS = 30.0

# The intake transport is intentionally schema-first.  Materialisation below
# remains the authority for exact source coverage and typed contract checks;
# this schema gives the Planner (and one bounded repair attempt) a stable
# machine-readable shape without adding a runtime jsonschema dependency.
INTAKE_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "allOf": [
        {"anyOf": [{"required": ["schemaVersion"]}, {"required": ["schema_version"]}]},
        {"anyOf": [{"required": ["portfolioStrategy"]}, {"required": ["portfolio_strategy"]}]},
        {"required": ["requirements"]},
        {"required": ["groups"]},
    ],
    "$defs": {
        "nonEmptyString": {"type": "string", "minLength": 1},
        "stringList": {
            "oneOf": [
                {"type": "array", "items": {"$ref": "#/$defs/nonEmptyString"}},
                {"$ref": "#/$defs/nonEmptyString"},
            ],
        },
        "sourceSpanList": {
            "oneOf": [
                {"type": "array", "items": {"$ref": "#/$defs/sourceSpan"}},
                {"$ref": "#/$defs/sourceSpan"},
            ],
        },
        "sourceBindingList": {
            "oneOf": [
                {"type": "array", "items": {"$ref": "#/$defs/sourceBinding"}},
                {"$ref": "#/$defs/sourceBinding"},
            ],
        },
        "documentRefList": {
            "oneOf": [
                {"type": "array", "items": {"$ref": "#/$defs/nonEmptyString"}},
                {"$ref": "#/$defs/nonEmptyString"},
            ],
        },
        "sourceSpan": {
            "type": "object",
            "additionalProperties": False,
            "anyOf": [
                {"required": ["blockId"]},
                {"required": ["block_id"]},
            ],
            "properties": {
                "blockId": {"$ref": "#/$defs/nonEmptyString"},
                "block_id": {"$ref": "#/$defs/nonEmptyString"},
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 1},
            },
        },
        "sourceBinding": {
            "type": "object",
            "additionalProperties": False,
            "anyOf": [
                {"required": ["source_ref"]},
                {"required": ["sourceRef"]},
                {"required": ["document_ref"]},
                {"required": ["documentRef"]},
                {"required": ["ref"]},
                {"required": ["blockId"]},
                {"required": ["block_id"]},
            ],
            "properties": {
                "source_ref": {"$ref": "#/$defs/nonEmptyString"},
                "sourceRef": {"$ref": "#/$defs/nonEmptyString"},
                "document_ref": {"$ref": "#/$defs/nonEmptyString"},
                "documentRef": {"$ref": "#/$defs/nonEmptyString"},
                "ref": {"$ref": "#/$defs/nonEmptyString"},
                "blockId": {"$ref": "#/$defs/nonEmptyString"},
                "block_id": {"$ref": "#/$defs/nonEmptyString"},
                "locator": {"type": "object"},
                "location": {"type": "object"},
                "span": {
                    "oneOf": [
                        {"$ref": "#/$defs/sourceSpan"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["start", "end"],
                            "properties": {
                                "blockId": {"$ref": "#/$defs/nonEmptyString"},
                                "block_id": {"$ref": "#/$defs/nonEmptyString"},
                                "start": {"type": "integer", "minimum": 0},
                                "end": {"type": "integer", "minimum": 1},
                            },
                        },
                    ],
                },
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 1},
                "page": {"type": ["integer", "string"]},
                "sheet": {"type": "string"},
                "section": {"type": ["integer", "string"]},
                "row": {"type": ["integer", "string"]},
                "cell": {"type": "string"},
                "paragraph": {"type": ["integer", "string"]},
                "column": {"type": "string"},
                "cells": {"type": "array", "items": {"type": "string"}},
            },
        },
        "contextItem": {
            "type": "object",
            "additionalProperties": False,
            "anyOf": [
                {"required": ["sourceSpans"]},
                {"required": ["source_spans"]},
                {"required": ["sourceBindings"]},
                {"required": ["source_bindings"]},
                {"required": ["documentRefs"]},
                {"required": ["document_refs"]},
                {"required": ["blockId", "start", "end"]},
                {"required": ["block_id", "start", "end"]},
            ],
            "properties": {
                "sourceSpans": {"$ref": "#/$defs/sourceSpanList"},
                "source_spans": {"$ref": "#/$defs/sourceSpanList"},
                "sourceBindings": {"$ref": "#/$defs/sourceBindingList"},
                "source_bindings": {"$ref": "#/$defs/sourceBindingList"},
                "documentRefs": {"$ref": "#/$defs/documentRefList"},
                "document_refs": {"$ref": "#/$defs/documentRefList"},
                "bindings": {"$ref": "#/$defs/sourceBindingList"},
                "blockId": {"$ref": "#/$defs/nonEmptyString"},
                "block_id": {"$ref": "#/$defs/nonEmptyString"},
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 1},
                "reason": {"type": "string"},
                "metadata": {"type": "object"},
            },
        },
        "candidate": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "allOf": [
                {"anyOf": [{"required": ["candidateId"]}, {"required": ["candidate_id"]}]},
                {"anyOf": [{"required": ["businessObjective"]}, {"required": ["business_objective"]}]},
            ],
            "anyOf": [
                {"required": ["sourceSpans"]},
                {"required": ["source_spans"]},
                {"required": ["sourceBindings"]},
                {"required": ["source_bindings"]},
            ],
            "properties": {
                "candidateId": {"$ref": "#/$defs/nonEmptyString"},
                "candidate_id": {"$ref": "#/$defs/nonEmptyString"},
                "sourceSpans": {"$ref": "#/$defs/sourceSpanList"},
                "source_spans": {"$ref": "#/$defs/sourceSpanList"},
                "sourceBindings": {"$ref": "#/$defs/sourceBindingList"},
                "source_bindings": {"$ref": "#/$defs/sourceBindingList"},
                "documentRefs": {"$ref": "#/$defs/documentRefList"},
                "document_refs": {"$ref": "#/$defs/documentRefList"},
                "businessObjective": {"$ref": "#/$defs/nonEmptyString"},
                "business_objective": {"$ref": "#/$defs/nonEmptyString"},
                "expectedAnalyticalOutputs": {"$ref": "#/$defs/stringList"},
                "expected_analytical_outputs": {"$ref": "#/$defs/stringList"},
                "expectedVisualOutputs": {"$ref": "#/$defs/stringList"},
                "expected_visual_outputs": {"$ref": "#/$defs/stringList"},
                "dependencies": {"$ref": "#/$defs/stringList"},
                "dataNeeds": {"$ref": "#/$defs/stringList"},
                "data_needs": {"$ref": "#/$defs/stringList"},
                "ontologyNeeds": {"$ref": "#/$defs/stringList"},
                "ontology_needs": {"$ref": "#/$defs/stringList"},
                "preparedDataNeeds": {"$ref": "#/$defs/stringList"},
                "prepared_data_needs": {"$ref": "#/$defs/stringList"},
                "workingDefinitions": {"$ref": "#/$defs/stringList"},
                "working_definitions": {"$ref": "#/$defs/stringList"},
                "limitations": {"$ref": "#/$defs/stringList"},
                "explicitPriority": {},
                "explicit_priority": {},
                "scope": {"$ref": "#/$defs/nonEmptyString"},
                "decompositionRationale": {"type": "string"},
                "decomposition_rationale": {"type": "string"},
            },
        },
        "group": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "allOf": [
                {"required": ["members"]},
                {"required": ["rationale"]},
            ],
            "properties": {
                "members": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/nonEmptyString"}},
                "rationale": {"$ref": "#/$defs/nonEmptyString"},
                "sharedAnalysisIntent": {"type": ["string", "null"]},
                "shared_analysis_intent": {"type": ["string", "null"]},
                "suggestedSpecialists": {"$ref": "#/$defs/stringList"},
                "suggested_specialists": {"$ref": "#/$defs/stringList"},
            },
        },
        "productBrief": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "audience": {"$ref": "#/$defs/contextList"},
                "decision": {"$ref": "#/$defs/contextList"},
                "deliverables": {"$ref": "#/$defs/contextList"},
                "pages_or_modules": {"$ref": "#/$defs/contextList"},
                "pagesOrModules": {"$ref": "#/$defs/contextList"},
                "filters": {"$ref": "#/$defs/contextList"},
                "visual_expectations": {"$ref": "#/$defs/contextList"},
                "visualExpectations": {"$ref": "#/$defs/contextList"},
            },
        },
        "contextList": {
            "oneOf": [
                {"type": "array", "items": {"$ref": "#/$defs/contextItem"}},
                {"$ref": "#/$defs/contextItem"},
            ],
        },
    },
    "properties": {
        "schemaVersion": {"const": 1},
        "schema_version": {"const": 1},
        "missionIntent": {"type": "string"},
        "mission_intent": {"type": "string"},
        "portfolioStrategy": {"type": "string", "minLength": 1},
        "portfolio_strategy": {"type": "string", "minLength": 1},
        "requirements": {
            "oneOf": [
                {
                    "type": "array",
                    "maxItems": MAX_REQUIREMENT_RECORDS,
                    "items": {"$ref": "#/$defs/candidate"},
                },
                {"$ref": "#/$defs/candidate"},
            ],
        },
        "groups": {
            "oneOf": [
                {"type": "array", "items": {"$ref": "#/$defs/group"}},
                {"$ref": "#/$defs/group"},
            ],
        },
        "unassignedContext": {"$ref": "#/$defs/contextList"},
        "unassigned_context": {"$ref": "#/$defs/contextList"},
        "additionalContext": {"$ref": "#/$defs/contextList"},
        "additional_context": {"$ref": "#/$defs/contextList"},
        "productBrief": {"$ref": "#/$defs/productBrief"},
        "product_brief": {"$ref": "#/$defs/productBrief"},
        "sourceContext": {"$ref": "#/$defs/contextList"},
        "source_context": {"$ref": "#/$defs/contextList"},
        "technicalConstraints": {"$ref": "#/$defs/contextList"},
        "technical_constraints": {"$ref": "#/$defs/contextList"},
    },
}


def _valid_process_group_token(value: Any) -> bool:
    """Accept only bounded, shell-safe process-group tokens."""

    return (
        isinstance(value, str)
        and 8 <= len(value) <= 128
        and value.isascii()
        and all(character.isalnum() or character in "-_" for character in value)
    )


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


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability for exclusive identity records."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def _receipt_path(run_root: Path, filename: str) -> Path:
    """Resolve one private Supervisor receipt under the run control plane."""

    root = Path(run_root).expanduser().resolve(strict=False)
    path = root / "control_plane" / safe_component(filename, "receipt filename")
    reject_symlink_components(path, root)
    return path


def _load_receipt(
    run_root: Path,
    filename: str,
    *,
    kind: str,
    run_id: str,
    startup_token: str | None = None,
) -> dict[str, Any] | None:
    """Read and verify one hash-bound child receipt.

    A stale receipt from an earlier process is ignored when its per-start
    token differs.  A receipt claiming the current token but carrying invalid
    JSON/hash/identity is a hard integrity failure rather than a readiness
    signal.
    """

    path = _receipt_path(run_root, filename)
    if not path.exists() and not path.is_symlink():
        return None
    value = load_object(path)
    if value.get("schemaVersion") != 1 or value.get("kind") != kind:
        raise LaunchConflictError(f"Supervisor {kind} receipt is malformed")
    if value.get("runId") != run_id or value.get("runRoot") != str(Path(run_root).resolve(strict=False)):
        # This can only be a stale receipt in a reused run root.  Treat it as
        # absent; the child-specific token check below prevents PID/receipt
        # reuse from being mistaken for this launch.
        return None
    observed_token = value.get("startupToken")
    if startup_token is not None and observed_token != startup_token:
        return None
    if startup_token is None and observed_token not in (None, ""):
        return None
    digest = value.get("payloadHash")
    if not isinstance(digest, str) or digest != sha256_bytes(canonical_bytes({key: item for key, item in value.items() if key != "payloadHash"})):
        raise LaunchConflictError(f"Supervisor {kind} receipt hash is invalid")
    return value


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
    while True:
        if current.is_symlink():
            resolved_current = current.resolve(strict=False)
            # macOS exposes administrator-selected temporary roots through
            # aliases such as /var -> /private/var.  Treat an alias that only
            # reaches an ancestor of the configured root as harmless; a
            # symlink at or below the root remains rejected.
            if not (root == resolved_current or resolved_current in root.parents):
                raise ValueError("symlink source paths are not accepted")
        # ``/var`` and ``/private/var`` are interchangeable on macOS.  The
        # configured root is resolved above, so stop when the lexical walk's
        # spelling resolves to that root as well as on an exact match.
        try:
            resolved_current = current.resolve(strict=False)
        except OSError as exc:
            raise ValueError("unable to inspect path components") from exc
        if current == root or resolved_current == root:
            break
        parent = current.parent
        if parent == current:
            # A bounded parent loop used to stop silently after 128 levels.
            # Reaching the filesystem root without reaching the configured
            # root means the lexical path cannot be proven safe.
            raise ValueError("path does not terminate at the configured root")
        current = parent


def supported_extension(name: str) -> bool:
    """Return whether a source name is admissible without extension gating.

    The historical helper name is kept for callers that import it, but local
    source admission is now based on safe regular-file/path validation.  URL
    callers may still apply their separate remote policy after this check.
    """

    value = str(name or "").strip()
    return bool(value and value not in {".", ".."} and "\x00" not in value)


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


def _is_zip_special(info: zipfile.ZipInfo) -> bool:
    """Reject device/FIFO/socket records while allowing regular files/dirs."""

    import stat

    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    return file_type not in {0, stat.S_IFREG, stat.S_IFDIR}


def _require_disk_space(path: Path, required_bytes: int, *, label: str) -> None:
    """Fail with a concrete diagnostic before a streamed write begins."""

    if required_bytes < 0:
        raise ValueError(f"{label} size cannot be negative")
    target = path if path.is_dir() else path.parent
    try:
        target.mkdir(parents=True, exist_ok=True)
        available = int(shutil.disk_usage(target).free)
    except OSError as exc:
        raise ValueError(f"unable to determine available disk space for {label}") from exc
    if available < required_bytes:
        raise ValueError(
            f"insufficient disk space for {label}: {required_bytes} bytes required, {available} bytes available"
        )


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


def _expanded_source_limit(settings: "LaunchSettings") -> int | None:
    limits = [value for value in (settings.max_source_total_bytes, MAX_ZIP_TOTAL_BYTES) if value is not None]
    return min(int(value) for value in limits) if limits else None


def _zip_central_directory_limit() -> int:
    limits = [MAX_ZIP_CENTRAL_DIRECTORY_BYTES]
    physical_limits = [
        value
        for value in (MAX_ZIP_MEMBER_COUNT, MAX_ZIP_PHYSICAL_ENTRY_COUNT)
        if value is not None
    ]
    if physical_limits:
        limits.append(min(physical_limits) * ZIP_CENTRAL_DIRECTORY_MAX_BYTES_PER_ENTRY)
    return min(limits)


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

    physical_limits = [
        value
        for value in (MAX_ZIP_MEMBER_COUNT, MAX_ZIP_PHYSICAL_ENTRY_COUNT)
        if value is not None
    ]
    physical_limit = min(physical_limits) if physical_limits else None
    if physical_limit is not None and total_entries > physical_limit:
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
    max_total_bytes: int | None,
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
    physical_limits = [
        value
        for value in (MAX_ZIP_MEMBER_COUNT, MAX_ZIP_PHYSICAL_ENTRY_COUNT)
        if value is not None
    ]
    physical_entry_limit = min(physical_limits) if physical_limits else None
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
                if physical_entry_limit is not None and prior_entry_count + physical_entry_count > physical_entry_limit:
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
                if _is_zip_special(info):
                    raise ValueError(f"ZIP member {name!r} is a special file")
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
                if MAX_ZIP_MEMBER_BYTES is not None and size > MAX_ZIP_MEMBER_BYTES:
                    raise ValueError(
                        f"ZIP member {name!r} exceeds expanded member limit ({size} > {MAX_ZIP_MEMBER_BYTES})"
                    )
                if size and compressed_size == 0:
                    raise ValueError(f"ZIP member {name!r} has an infinite compression ratio")
                if compressed_size and size / compressed_size > MAX_ZIP_COMPRESSION_RATIO:
                    raise ValueError(
                        f"ZIP member {name!r} exceeds compression ratio limit ({MAX_ZIP_COMPRESSION_RATIO})"
                    )
                if max_total_bytes is not None and prior_total_bytes + physical_expanded_size + size > max_total_bytes:
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
                if MAX_ZIP_MEMBER_COUNT is not None and len(members) >= MAX_ZIP_MEMBER_COUNT:
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


class IntakeRepresentationError(LaunchConflictError):
    """A bounded Planner wire-shape defect that may be repaired once."""


class IntakeSemanticError(LaunchConflictError):
    """A Planner semantic/source-coverage defect that must not be rerun."""


class IntakeSourceError(LaunchConflictError):
    """A trusted source/catalog/hash defect that cannot be repaired by the Planner."""


class _SupervisorStartCleanupError(LaunchConflictError):
    """Carry a post-spawn Supervisor identity to manager cleanup.

    ``SubprocessRunner.start`` normally owns cleanup when readiness validation
    fails.  If that cleanup cannot be confirmed, the complete token-bound
    identity is carried to :class:`LaunchManager` so it can retry the exact
    process-group cleanup instead of losing ownership at the exception edge.
    """

    def __init__(self, message: str, *, started: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.started = dict(started)


def _process_group_token_assignment(payload: str, expected_token: str) -> bool:
    """Match only the exact private environment assignment in ps output."""

    if not _valid_process_group_token(expected_token):
        return False
    for item in payload.split():
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        if name == _PROCESS_GROUP_TOKEN_ENV and value == expected_token:
            return True
    return False


def _process_group_has_token(process_group_id: int, process_group_token: str) -> bool:
    """Verify one exact token-bearing member exists in one PGID."""

    if (
        isinstance(process_group_id, bool)
        or not isinstance(process_group_id, int)
        or process_group_id <= 1
        or not _valid_process_group_token(process_group_token)
    ):
        return False
    try:
        completed = subprocess.run(
            # Darwin's -E is not a GNU ps option. Both variants expose the
            # same PGID/status/environment evidence; unknown probes fail closed.
            (["ps", "eww", "-eo", "pgid=,stat=,args="] if sys.platform.startswith("linux")
             else ["ps", "-Eww", "-axo", "pgid=,stat=,command="]),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchConflictError("Could not verify the Foundry Supervisor process group") from exc
    if getattr(completed, "returncode", None) != 0:
        raise LaunchConflictError("Could not verify the Foundry Supervisor process group")
    for raw_line in (completed.stdout or "").splitlines():
        parts = raw_line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pgid = int(parts[0])
        except ValueError:
            continue
        if pgid != process_group_id or parts[1].startswith("Z"):
            continue
        payload = parts[2] if len(parts) == 3 else ""
        if _process_group_token_assignment(payload, process_group_token):
            return True
    return False


def _terminate_token_owned_process_group(
    process_group_id: int,
    process_group_token: str,
    *,
    timeout_seconds: float = 5.0,
) -> bool:
    """Terminate only a currently verified token-owned process group."""

    if (
        isinstance(process_group_id, bool)
        or not isinstance(process_group_id, int)
        or process_group_id <= 1
        or not _valid_process_group_token(process_group_token)
    ):
        return False
    if process_group_id == os.getpgrp():
        raise LaunchConflictError("Refusing to stop the Control Center process group")
    if not _process_group_has_token(process_group_id, process_group_token):
        return False
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        if not _process_group_has_token(process_group_id, process_group_token):
            return True
        time.sleep(0.05)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_group_has_token(process_group_id, process_group_token):
            return True
        time.sleep(0.05)
    raise LaunchConflictError("Foundry Supervisor process-group members did not stop")


def _validated_process_identity(started: Any) -> tuple[int, int, str] | None:
    """Return the complete identity required for durable process tracking."""

    if not isinstance(started, Mapping):
        return None
    pid = started.get("pid")
    process_group_id = started.get("processGroupId")
    process_group_token = started.get("processGroupToken")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(process_group_id, bool)
        or not isinstance(process_group_id, int)
        or process_group_id <= 1
        or pid != process_group_id
        or not _valid_process_group_token(process_group_token)
    ):
        return None
    return pid, process_group_id, process_group_token


_RUN_ADMISSION_LOCK_NAMESPACE = ".run_admission_locks"
_RUNNER_STATUS_FIELDS = (
    "monitorRunId",
    "pid",
    "processGroupId",
    "processGroupToken",
    "startupToken",
    "ready",
    "processStart",
    "readyAt",
    "startupTimedOut",
    "childExited",
    "exitCode",
    "exitAt",
)


def _run_bound_status_records(
    settings: "LaunchSettings",
    run_id: Any,
    run_root: Any,
) -> tuple[tuple[str, Path, dict[str, Any]], ...]:
    """Read every private status record bound to one exact durable run."""

    if not isinstance(run_id, str) or not run_id.strip():
        raise LaunchConflictError("Run status ownership requires a non-empty run identity")
    try:
        expected_root = Path(run_root).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError) as exc:
        raise LaunchConflictError("Run status ownership requires a valid run root") from exc
    statuses_root = Path(settings.state_root).expanduser() / "statuses"
    if statuses_root.is_symlink() or (statuses_root.exists() and not statuses_root.is_dir()):
        raise LaunchConflictError("Run status namespace is invalid")
    if not statuses_root.is_dir():
        return ()
    try:
        children = sorted(statuses_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise LaunchConflictError("Run status namespace is unavailable") from exc
    records: list[tuple[str, Path, dict[str, Any]]] = []
    for child in children:
        if child.is_symlink() or not child.is_file() or child.suffix != ".json":
            continue
        try:
            value = load_object(child)
        except (OSError, ValueError):
            # One malformed historical record must not hide another valid
            # run-bound owner; the matching identity is selected below from
            # every readable record deterministically.
            continue
        if value.get("runId") != run_id:
            continue
        raw_root = value.get("runRoot")
        if not isinstance(raw_root, str) or not raw_root:
            continue
        try:
            if Path(raw_root).expanduser().resolve(strict=False) != expected_root:
                continue
        except (OSError, TypeError, ValueError):
            continue
        records.append(
            (
                str(value.get("startedAt") or value.get("acceptedAt") or ""),
                child,
                value,
            )
        )
    return tuple(sorted(records, key=lambda item: (item[0], item[1].name)))


def _run_admission_lock_path(settings: "LaunchSettings", run_id: Any, run_root: Any) -> Path:
    """Return one trusted, collision-resistant lock path for a durable run."""

    if not isinstance(run_id, str) or not run_id.strip():
        raise LaunchConflictError("Run admission lock requires a non-empty run identity")
    try:
        resolved_root = Path(run_root).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError) as exc:
        raise LaunchConflictError("Run admission lock requires a valid run root") from exc
    state_root = Path(settings.state_root).expanduser()
    if state_root.is_symlink() or (state_root.exists() and not state_root.is_dir()):
        raise LaunchConflictError("Run admission lock namespace is unavailable")
    try:
        state_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LaunchConflictError("Run admission lock namespace is unavailable") from exc
    if state_root.is_symlink() or not state_root.is_dir():
        raise LaunchConflictError("Run admission lock namespace is unavailable")
    namespace = state_root / _RUN_ADMISSION_LOCK_NAMESPACE
    if namespace.is_symlink() or (namespace.exists() and not namespace.is_dir()):
        raise LaunchConflictError("Run admission lock namespace is invalid")
    try:
        namespace.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise LaunchConflictError("Run admission lock namespace is unavailable") from exc
    if namespace.is_symlink() or not namespace.is_dir():
        raise LaunchConflictError("Run admission lock namespace is invalid")
    identity = f"{run_id.strip()}\x00{resolved_root}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = namespace / f"{digest}.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LaunchConflictError("Run admission lock path is invalid")
    return path


@contextmanager
def _run_admission_lock(settings: "LaunchSettings", run_id: Any, run_root: Any) -> Iterable[None]:
    """Serialize one run's launch/control mutations across instances/processes.

    The lock lives below the trusted Control Center ``state_root`` and is
    keyed by the exact run id plus resolved run root.  POSIX ``flock`` releases
    it automatically when the owning file descriptor/process disappears.  A
    host without ``fcntl`` fails closed rather than claiming cross-process
    safety from the instance-local locks.
    """

    if fcntl is None:
        raise LaunchConflictError("Cross-process run admission requires POSIX advisory locks")
    path = _run_admission_lock_path(settings, run_id, run_root)
    try:
        stream = path.open("a+b")
    except OSError as exc:
        raise LaunchConflictError("Run admission lock is unavailable") from exc
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise LaunchConflictError("Run admission lock could not be acquired") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
    except BaseException:
        # Closing the descriptor releases the flock even when acquisition or
        # the protected operation raises.  Keep the original exception.
        try:
            stream.close()
        except (OSError, ValueError):
            pass
        raise


@dataclass(frozen=True)
class LaunchSettings:
    runtime_root: Path
    runs_root: Path
    source_roots: tuple[Path, ...] = ()
    state_root: Path | None = None
    max_agents: int = DEFAULT_MAX_AGENTS
    enable_launch: bool = False
    codex_bin: str = field(default_factory=default_codex_binary)
    upload_limit_bytes: int | None = None
    max_source_count: int | None = DEFAULT_MAX_SOURCE_COUNT
    max_source_total_bytes: int | None = None
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
        if (
            (self.upload_limit_bytes is not None and self.upload_limit_bytes < 1)
            or (self.max_source_count is not None and self.max_source_count < 1)
            or (self.max_source_total_bytes is not None and self.max_source_total_bytes < 1)
        ):
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
                "admission": "any_safe_regular_file",
                "maxUploadBytes": self.upload_limit_bytes,
                "maxZipMemberBytes": MAX_ZIP_MEMBER_BYTES,
                "maxZipMemberCount": MAX_ZIP_MEMBER_COUNT,
                "maxExpandedSourceBytes": _expanded_source_limit(self),
                "resourceChecks": "streamed_with_available_disk_space",
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
        if content_length is None or isinstance(content_length, bool) or not isinstance(content_length, int) or content_length < 0:
            raise LaunchValidationError({"upload": "Content-Length is required."}, "Upload is invalid")
        if self.settings.upload_limit_bytes is not None and content_length > self.settings.upload_limit_bytes:
            raise LaunchValidationError({"upload": "Upload exceeds the configured size limit."}, "Upload is too large")
        upload_id = "UP-" + uuid.uuid4().hex
        payload_path, metadata_path = self._record_paths(upload_id)
        payload_path.parent.mkdir(parents=True, exist_ok=False)
        try:
            _require_disk_space(payload_path.parent, content_length, label="upload staging")
        except ValueError as exc:
            shutil.rmtree(payload_path.parent, ignore_errors=True)
            raise LaunchValidationError({"upload": str(exc)}, "Upload cannot be staged") from exc
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
                    if size > content_length:
                        raise LaunchValidationError({"upload": "Upload exceeded its declared Content-Length."}, "Upload is invalid")
                    if self.settings.upload_limit_bytes is not None and size > self.settings.upload_limit_bytes:
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
        except OSError as exc:
            shutil.rmtree(payload_path.parent, ignore_errors=True)
            raise LaunchValidationError({"upload": f"Upload could not be staged: {exc}"}, "Upload cannot be staged") from exc
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
        raise ValueError("Remote source path is invalid.")
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
        document_catalog: Mapping[str, Any] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        role_route: Mapping[str, str] | None = None,
        repair_context: Mapping[str, Any] | None = None,
    ) -> str:
        payload = {
            "intakeBlocks": [dict(block) for block in intake_blocks],
            "existingPlan": dict(existing_plan) if existing_plan is not None else None,
            "availableDocumentRefs": list(document_refs),
            # Input-only bounded projection.  LaunchManager, not the Planner,
            # owns and persists the trusted catalog derived from source bytes.
            "documentCatalogProjection": dict(document_catalog or {}),
            "responseSchema": dict(response_schema or INTAKE_RESPONSE_SCHEMA),
            "roleRoute": dict(role_route or {}),
            "repairContext": dict(repair_context or {}),
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
            "explicit labels such as Requirement 1..5 as strong evidence, not a parser rule. Use only the "
            "bounded normalized document catalog projection supplied below; raw data-room bytes remain an "
            "owner fallback and are not Planner input. Do not edit any file.\n"
            "Return exactly one JSON object with no markdown. Validate it against the supplied JSON Schema; "
            "the host will reject any shape or source-binding mismatch. Schema:\n"
            "{\"schemaVersion\":1,\"missionIntent\":\"discovery|specification|hybrid\","
            "\"portfolioStrategy\":\"...\",\"requirements\":["
            "{\"candidateId\":\"C-001\",\"sourceSpans\":[{\"blockId\":\"INPUT-001\","
            "\"start\":0,\"end\":10}],\"sourceBindings\":[],\"documentRefs\":[],"
            "\"businessObjective\":\"...\","
            "\"expectedAnalyticalOutputs\":[],\"expectedVisualOutputs\":[],\"dependencies\":[],"
            "\"dataNeeds\":[],\"ontologyNeeds\":[],\"preparedDataNeeds\":[],"
            "\"workingDefinitions\":[],\"limitations\":[],\"explicitPriority\":null,"
            "\"scope\":\"analytics\"}],\"groups\":[{\"members\":[\"C-001\"],"
            "\"rationale\":\"...\",\"sharedAnalysisIntent\":null,\"suggestedSpecialists\":[]}],"
            "\"productBrief\":{\"audience\":[],\"decision\":[],\"deliverables\":[],"
            "\"pagesOrModules\":[],\"filters\":[],\"visualExpectations\":[]},"
            "\"sourceContext\":[],\"technicalConstraints\":[],"
            "\"additionalContext\":[{\"sourceSpans\":[{\"blockId\":\"INPUT-001\","
            "\"start\":10,\"end\":20}],\"sourceBindings\":[],\"documentRefs\":[],\"reason\":\"context only\"}]}\n"
            "Span offsets are Python character offsets into the exact block text. A requirement needs at "
            "least one source span or one trusted document sourceBinding. The host derives canonical source text "
            "and section hashes from those bindings; omit redundant text and hash echoes. Every non-whitespace "
            "character must be covered by at least one requirement span or typed context span. Every context "
            "item must include sourceSpans and/or documentRefs; document-backed items must include a "
            "sourceBinding with a locator (page, sheet, section, cell, paragraph, or row). Classify the mission intent "
            "semantically; do not infer it from a keyword or heading. A product brief, source description, "
            "or technical constraint is context, not an Analytical Owner requirement, unless it also asks "
            "for an independent business decision. Each "
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
        document_catalog: Mapping[str, Any] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        role_route: Mapping[str, str] | None = None,
        repair_context: Mapping[str, Any] | None = None,
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
            if isinstance(role_route, Mapping):
                model = role_route.get("model")
                reasoning = role_route.get("reasoning_effort")
                if not isinstance(model, str) or not model.strip() or not isinstance(reasoning, str) or not reasoning.strip():
                    raise LaunchConflictError("Requirement Planner role route is incomplete")
                # The route is explicit on the transport as well as in the
                # prompt, preventing a host-level ambient model/profile from
                # silently handling intake planning.
                argv[3:3] = ["--model", model.strip(), "-c", f"model_reasoning_effort={reasoning.strip().lower()}"]
            try:
                completed = self._run(
                    argv,
                    input=self._prompt(
                        intake_blocks=intake_blocks,
                        existing_plan=existing_plan,
                        data_room=data_room,
                        document_refs=document_refs,
                        document_catalog=document_catalog,
                        skill_binding=skill_binding,
                        response_schema=response_schema,
                        role_route=role_route,
                        repair_context=repair_context,
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
    """Start the top-level Foundry Supervisor CLI for one run.

    Tests replace this object with a fake runner.  The request payload never
    controls executable, shell text, or flags.
    """

    def __init__(
        self,
        codex_bin: str = "codex",
        *,
        popen: Callable[..., Any] | None = None,
        startup_timeout_seconds: float = SUPERVISOR_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.codex_bin = str(codex_bin)
        self._using_real_popen = popen is None
        self._popen = popen or subprocess.Popen
        if isinstance(startup_timeout_seconds, bool) or not isinstance(startup_timeout_seconds, (int, float)) or startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        self.startup_timeout_seconds = float(startup_timeout_seconds)

    @staticmethod
    def _coordinator_spec_hash(run_root: Path) -> str:
        path = _receipt_path(run_root, "coordinator_spec.json")
        value = load_object(path)
        if value.get("kind") != "run_coordinator_spec" or value.get("schema_version") != 1:
            raise LaunchConflictError("Coordinator specification is malformed")
        body = {key: item for key, item in value.items() if key not in {"kind", "schema_version"}}
        return sha256_bytes(canonical_bytes(body))

    @staticmethod
    def _role_routing_hash(run_root: Path) -> str:
        path = _receipt_path(run_root, "coordinator_spec.json")
        value = load_object(path)
        codex = value.get("codex_exec")
        if not isinstance(codex, Mapping):
            raise LaunchConflictError("Coordinator Codex execution binding is unavailable")
        models = codex.get("role_models")
        reasoning = codex.get("role_reasoning_efforts")
        if not isinstance(models, Mapping) or not isinstance(reasoning, Mapping):
            raise LaunchConflictError("Coordinator role routing is unavailable")
        return sha256_bytes(canonical_bytes({"role_models": dict(models), "role_reasoning_efforts": dict(reasoning)}))

    def _wait_for_ready(
        self,
        *,
        process: Any,
        run_id: str,
        run_root: Path,
        startup_token: str,
        pid: int | None,
        process_group_id: int | None,
    ) -> dict[str, Any]:
        expected_spec_hash = self._coordinator_spec_hash(run_root)
        expected_route_hash = self._role_routing_hash(run_root)
        deadline = time.monotonic() + self.startup_timeout_seconds
        while True:
            ready = _load_receipt(
                run_root,
                SUPERVISOR_READY_FILENAME,
                kind="foundry_supervisor_ready",
                run_id=run_id,
                startup_token=startup_token,
            )
            if ready is not None:
                if (
                    ready.get("specHash") != expected_spec_hash
                    or ready.get("roleRoutingHash") != expected_route_hash
                    or ready.get("pid") != pid
                    or ready.get("processGroupId") != process_group_id
                    or not isinstance(ready.get("processStart"), str)
                    or not ready.get("processStart")
                ):
                    raise LaunchConflictError("Foundry Supervisor readiness identity does not match this launch")
                return {
                    "ready": True,
                    "readyAt": ready.get("readyAt"),
                    "processStart": ready.get("processStart"),
                    "startupToken": startup_token,
                }
            exit_receipt = _load_receipt(
                run_root,
                SUPERVISOR_EXIT_FILENAME,
                kind="foundry_supervisor_exit",
                run_id=run_id,
                startup_token=startup_token,
            )
            if exit_receipt is not None:
                return {
                    "ready": False,
                    "childExited": True,
                    "exitCode": exit_receipt.get("exitCode"),
                    "exitAt": exit_receipt.get("exitAt"),
                    "startupToken": startup_token,
                }
            poll = getattr(process, "poll", None)
            if callable(poll):
                try:
                    code = poll()
                except Exception:
                    code = None
                if code is not None:
                    return {
                        "ready": False,
                        "childExited": True,
                        "exitCode": code,
                        "startupToken": startup_token,
                    }
            if time.monotonic() >= deadline:
                return {
                    "ready": False,
                    "startupTimedOut": True,
                    "startupToken": startup_token,
                }
            time.sleep(SUPERVISOR_STARTUP_POLL_SECONDS)

    def start(
        self,
        *,
        run_id: str,
        run_root: Path,
        manifest_path: Path,
        capacity: Mapping[str, int],
    ) -> dict[str, Any]:
        checkout_src = (Path(__file__).resolve().parents[2] / "src").resolve(strict=False)
        if not checkout_src.is_dir() or not (checkout_src / "auto_foundry_core" / "__init__.py").is_file():
            raise LaunchConflictError(
                "Current checkout core source is unavailable; refusing to launch an installed/ambient core"
            )
        control = run_root / "control_plane"
        control.mkdir(parents=True, exist_ok=True)
        # The canonical coordinator spec is already materialized by
        # LaunchManager through RunCoordinator.start/from_persisted_spec.
        # The subprocess always enters through the single public Supervisor
        # wrapper, which owns ordinary coordinator monitoring and repair.
        raw_log = control / "coordinator.jsonl"
        stderr_log = control / "coordinator.stderr.log"
        # The operational server is commonly started with ``PYTHONPATH=src:.``.
        # Its child runs from the isolated run root, so relative entries would
        # no longer resolve to this checkout.  Keep only existing absolute
        # entries and prepend the checkout's source tree explicitly.
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
        child_env[_CHECKOUT_SRC_ENV] = str(checkout_src)
        # Keep the process-group identity private to the child environment so
        # descendants inherit it without exposing it in argv, logs, or the
        # public launch/status response.
        process_group_token = secrets.token_hex(_PROCESS_GROUP_TOKEN_BYTES)
        child_env[_PROCESS_GROUP_TOKEN_ENV] = process_group_token
        startup_token = secrets.token_hex(_PROCESS_GROUP_TOKEN_BYTES)
        child_env[_SUPERVISOR_STARTUP_TOKEN_ENV] = startup_token
        # Generate every independent, fallible launch value before Popen.
        # Once the child exists, the remaining identity/result/readiness path
        # is enclosed by the exact token-owned cleanup below.
        monitor_id = "supervisor-" + uuid.uuid4().hex[:16]
        argv = [
            sys.executable,
            "-m",
            "auto_foundry_core.cli",
            "supervisor",
            "run",
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
        ]
        # ``start_new_session=True`` makes the child the leader of a new
        # process group.  Fakes may expose only ``pid``; retaining this
        # deterministic fallback keeps the persisted identity useful in
        # tests without probing or mutating external process state.
        process: Any = None
        pid: Any = None
        process_group_id: Any = None
        result: dict[str, Any] = {}
        try:
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
            pid = getattr(process, "pid", None)
            process_group_id = getattr(process, "pgid", None)
            if process_group_id is None and isinstance(pid, int):
                process_group_id = pid
            result = {
                "monitorRunId": monitor_id,
                "pid": pid,
                "processGroupId": process_group_id,
                "processGroupToken": process_group_token,
                "startupToken": startup_token,
                "argv": argv,
            }
            # Injected Popen fakes intentionally do not represent a child that
            # can publish receipts.  Real launches always wait for the child
            # to prove readiness (or to exit/timeout) before execute exposes a
            # running run.
            if self._using_real_popen:
                result.update(
                    self._wait_for_ready(
                        process=process,
                        run_id=run_id,
                        run_root=run_root,
                        startup_token=startup_token,
                        pid=pid,
                        process_group_id=process_group_id,
                    )
                )
        except Exception:
            # ``LaunchManager.execute`` cannot receive ``result`` until
            # ``start`` returns.  Any post-Popen identity/result/readiness
            # failure therefore must stop this exact token-owned group here,
            # before the exception crosses that ownership boundary.  If
            # cleanup is itself uncertain, carry the complete identity to the
            # manager for one further exact retry; never broad-kill by PID or
            # leave an untracked child running.
            if process is None:
                # Popen did not return a child, so there is no process-group
                # identity to clean.  Preserve the original launch error.
                raise
            cleanup_result: Mapping[str, Any]
            if not result:
                # Identity extraction itself may have failed after Popen.  The
                # process-group leader is the child PID under
                # ``start_new_session=True``; reuse the successfully extracted
                # PID (or make one bounded best-effort read) to retain a
                # complete token-bound cleanup mapping.
                if pid is None:
                    try:
                        pid = getattr(process, "pid", None)
                    except Exception:
                        pid = None
                if process_group_id is None and isinstance(pid, int):
                    process_group_id = pid
                cleanup_result = {
                    "monitorRunId": monitor_id,
                    "pid": pid,
                    "processGroupId": process_group_id,
                    "processGroupToken": process_group_token,
                    "startupToken": startup_token,
                    "argv": argv,
                }
            else:
                cleanup_result = result
            try:
                identity = _validated_process_identity(cleanup_result)
                if identity is None:
                    raise LaunchConflictError(
                        "Foundry Supervisor process identity is incomplete after startup failure"
                    )
                _pid, owned_group_id, owned_group_token = identity
                terminated = _terminate_token_owned_process_group(
                    owned_group_id,
                    owned_group_token,
                )
                if terminated is not True:
                    raise LaunchConflictError(
                        "Foundry Supervisor process-group cleanup was not confirmed"
                    )
            except Exception as cleanup_exc:
                raise _SupervisorStartCleanupError(
                    "Foundry Supervisor startup failed and process cleanup was not confirmed",
                    started=cleanup_result,
                ) from cleanup_exc
            raise
        return result


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

    @property
    def preparations_root(self) -> Path:
        """Durable request identities shared by reloads and launch tabs."""

        return Path(self.settings.state_root) / "preparations"

    def _run_owned_supervisor_status(
        self,
        run_id: str,
        run_root: Path,
        *,
        exclude_draft_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest still-owned Supervisor identity for one run.

        Launch preparation records are per draft, while the Supervisor is
        owned by the durable run.  Admission therefore cannot inspect only
        the current/latest draft: a newer queued or identity-less draft must
        not hide an older token-bearing child.  Every complete identity is
        probed under the already-held run lock; an explicit ``False`` probe
        is the only quiescent result, while an unavailable probe remains an
        owned/unknown child and blocks a second spawn.
        """

        # Several drafts can durably copy the same Supervisor identity.  The
        # identity (PGID + private process-group token), rather than the draft
        # record, is the ownership unit; collapse duplicates before probing so
        # one child is never counted as multiple owners.
        candidates_by_identity: dict[tuple[int, str], tuple[str, Path, dict[str, Any]]] = {}
        for stamp, path, value in _run_bound_status_records(self.settings, run_id, run_root):
            if exclude_draft_id is not None and value.get("draftId") == exclude_draft_id:
                continue
            identity = _validated_process_identity(value)
            if identity is None:
                continue
            _pid, process_group_id, process_group_token = identity
            key = (process_group_id, process_group_token)
            current = candidates_by_identity.get(key)
            if current is None or (stamp, path.name) > (current[0], current[1].name):
                candidates_by_identity[key] = (stamp, path, value)
        if not candidates_by_identity:
            return None
        candidates: list[tuple[str, Path, dict[str, Any], bool | None]] = []
        for stamp, path, value in candidates_by_identity.values():
            identity = _validated_process_identity(value)
            assert identity is not None
            _pid, process_group_id, process_group_token = identity
            try:
                liveness = _process_group_has_token(process_group_id, process_group_token)
            except LaunchConflictError:
                liveness = None
            # Only an identity individually proven gone is quiescent.  A
            # live/unknown distinct identity remains an owner even when a
            # newer distinct status record is gone.
            if liveness is not False:
                candidates.append((stamp, path, value, liveness))
        if not candidates:
            return None
        _stamp, _path, newest, _liveness = max(candidates, key=lambda item: (item[0], item[1].name))
        return dict(newest)

    @staticmethod
    def _attach_run_owned_identity(
        status_payload: MutableMapping[str, Any],
        owner: Mapping[str, Any],
    ) -> str:
        """Attach private runner fields and derive a truthful public state."""

        for key in _RUNNER_STATUS_FIELDS:
            if key in owner:
                status_payload[key] = owner[key]
        owner_status = str(owner.get("status") or "").strip().lower()
        if owner_status not in {"queued", "starting", "running", "accepted"}:
            owner_status = "running" if owner.get("ready") is True else "starting"
        return owner_status

    def _preparation_path(self, idempotency_key: str) -> Path:
        # Keep user-controlled identity material out of a filename while still
        # making the record stable across manager/process instances.
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        path = self.preparations_root / f"{digest}.json"
        reject_symlink_components(path, Path(self.settings.state_root))
        return path

    @staticmethod
    def _normalized_idempotency_key(value: Any) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError("idempotencyKey must be text")
        key = value.strip()
        if not key or len(key) > MAX_PREPARATION_IDEMPOTENCY_KEY:
            raise ValueError("idempotencyKey must be between 1 and 128 characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError("idempotencyKey contains control characters")
        # A preparation identity is a logical token, never a path or a
        # transport fragment.  Keep the same conservative component rules as
        # draft/status IDs while allowing UUID punctuation and Unicode labels.
        safe_component(key, "idempotencyKey")
        return key

    @staticmethod
    def _preparation_request_hash(payload: Mapping[str, Any]) -> str:
        material = {
            str(key): value
            for key, value in payload.items()
            if str(key) not in {"idempotencyKey", "preparationId"}
        }
        return sha256_bytes(canonical_bytes(material))

    def _load_idempotent_draft(
        self,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        path = self._preparation_path(idempotency_key)
        if not path.exists() and not path.is_symlink():
            return None
        record = load_object(path)
        if (
            record.get("schemaVersion") != 1
            or record.get("kind") != "launch_preparation"
            or record.get("idempotencyKey") != idempotency_key
            or record.get("requestHash") != request_hash
        ):
            raise LaunchConflictError("idempotencyKey is already bound to a different launch request")
        draft_id = record.get("draftId")
        fingerprint = record.get("fingerprint")
        if not isinstance(draft_id, str) or not isinstance(fingerprint, str):
            raise LaunchConflictError("durable launch preparation identity is incomplete")
        try:
            return self._load_draft(draft_id, fingerprint)
        except (OSError, ValueError, LaunchConflictError) as exc:
            raise LaunchConflictError("durable launch preparation is unavailable for recovery") from exc

    def _publish_preparation_identity(self, value: Mapping[str, Any]) -> dict[str, Any] | None:
        key = value.get("idempotencyKey")
        if not isinstance(key, str) or not key:
            return None
        path = self._preparation_path(key)
        self.preparations_root.mkdir(parents=True, exist_ok=True)
        payload = canonical_bytes(dict(value)) + b"\n"
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(path.parent)
            return None
        except FileExistsError:
            # A second browser tab may win the O_EXCL race.  Return its exact
            # identity so the caller can remove its unreferenced draft and
            # surface the already-visible preparation instead of duplicating
            # a placeholder.
            if path.is_symlink() or not path.is_file():
                raise LaunchConflictError("durable launch preparation identity is unavailable")
            return load_object(path)
        except OSError as exc:
            raise LaunchConflictError("durable launch preparation identity could not be persisted") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _prepared_response(draft: Mapping[str, Any], *, reused: bool = False) -> dict[str, Any]:
        data_identity = draft.get("dataRevision")
        return {
            "valid": True,
            "prepared": True,
            "draftId": draft.get("draftId"),
            "fingerprint": draft.get("fingerprint"),
            "runId": draft.get("runId"),
            "runRoot": draft.get("runRoot"),
            "summary": {
                "inputBlocks": len(draft.get("intakeBlocks") or ()),
                "sources": len(draft.get("sources") or ()),
            },
            "effectiveCapacity": draft.get("effectiveCapacity"),
            "dataRevision": data_identity,
            "idempotencyKey": draft.get("idempotencyKey"),
            "reused": reused,
            "message": "Launch package is already prepared; confirm the fingerprint to start the run." if reused else "Launch package prepared. Confirm the fingerprint to start the run.",
            "errors": {},
        }

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
                        if self.settings.upload_limit_bytes is not None and size > self.settings.upload_limit_bytes:
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
        if self.settings.max_source_count is not None and len(canonical) > self.settings.max_source_count:
            errors["sources"] = "Too many source entries."
        if expanded_limit is not None and (physical_total > expanded_limit or accepted_total > expanded_limit):
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
        try:
            idempotency_key = self._normalized_idempotency_key(
                payload.get("idempotencyKey", payload.get("preparationId"))
            )
            request_hash = self._preparation_request_hash(payload) if idempotency_key else None
        except (TypeError, ValueError) as exc:
            errors["idempotencyKey"] = str(exc)
            idempotency_key = None
            request_hash = None
        if idempotency_key is not None and request_hash is not None:
            existing_draft = self._load_idempotent_draft(idempotency_key, request_hash)
            if existing_draft is not None:
                return self._prepared_response(existing_draft, reused=True)
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
        data_identity: dict[str, Any] | None = None
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
                try:
                    data_identity = self._data_revision_identity(
                        self._discover_existing_data_room(target_root, target_run_id)
                    )
                except LaunchConflictError as exc:
                    errors["runId"] = str(exc)
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
        # A continuation may be data-only.  Its existing cumulative plan is
        # the semantic input; the new source snapshot is admitted separately
        # as an immutable data revision during execute.
        if mode == "continue" and not intake_blocks:
            errors.pop("intakeBlocks", None)
        elif not intake_blocks and (available_documents or has_document_archive):
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
        if idempotency_key is not None:
            unsigned["idempotencyKey"] = idempotency_key
        if data_identity is not None:
            unsigned["dataRevision"] = data_identity
        fingerprint = self._fingerprint(unsigned)
        draft = {**unsigned, "fingerprint": fingerprint, "status": "prepared"}
        atomic_write_json(self._draft_path(draft_id), draft)
        if idempotency_key is not None and request_hash is not None:
            winner = self._publish_preparation_identity(
                {
                    "schemaVersion": 1,
                    "kind": "launch_preparation",
                    "idempotencyKey": idempotency_key,
                    "requestHash": request_hash,
                    "draftId": draft_id,
                    "fingerprint": fingerprint,
                    "createdAt": unsigned["createdAt"],
                }
            )
            if winner is not None:
                # Another process won the identity race.  The local draft was
                # never exposed with a status record; remove it before
                # returning the durable winner so projection cannot show a
                # duplicate placeholder.
                self._draft_path(draft_id).unlink(missing_ok=True)
                winner_draft_id = winner.get("draftId")
                winner_fingerprint = winner.get("fingerprint")
                if winner.get("requestHash") != request_hash or not isinstance(winner_draft_id, str) or not isinstance(winner_fingerprint, str):
                    raise LaunchConflictError("idempotencyKey is already bound to a different launch request")
                return self._prepared_response(self._load_draft(winner_draft_id, winner_fingerprint), reused=True)
        return self._prepared_response(draft)

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
                if resolved.is_file():
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
            _require_disk_space(
                destination_parent,
                source_path.stat().st_size,
                label="ZIP source snapshot",
            )
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
                    if (
                        self.settings.upload_limit_bytes is not None
                        and size > self.settings.upload_limit_bytes
                    ):
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

    @staticmethod
    def _stream_local_source_to_zip(
        source_path: Path,
        archive: zipfile.ZipFile,
        relative: str,
        *,
        expected_size: Any,
        expected_sha256: Any,
        max_total_bytes: int | None,
        prior_total_bytes: int,
    ) -> tuple[int, str]:
        """Copy one local source descriptor while binding the bytes observed.

        Preparation records a size and digest, but a local path can be
        replaced between preparation and execution.  Open the source once,
        hash/count exactly the bytes written to the package, and compare the
        observations only after the streamed copy completes.  The caller
        writes to a temporary package, so a binding failure cannot publish a
        partial destination archive.
        """

        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
            raise ValueError("local source changed after prepare: prepared size binding is invalid")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ValueError("local source changed after prepare: prepared hash binding is invalid")
        if max_total_bytes is not None and max_total_bytes < 0:
            raise ValueError("local source aggregate bound is invalid")
        if prior_total_bytes < 0:
            raise ValueError("local source aggregate offset is invalid")

        digest = hashlib.sha256()
        observed = 0
        info = zipfile.ZipInfo(relative)
        info.compress_type = zipfile.ZIP_DEFLATED
        try:
            with source_path.open("rb") as stream, archive.open(info, "w") as target:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    next_size = observed + len(chunk)
                    if max_total_bytes is not None and prior_total_bytes + next_size > max_total_bytes:
                        raise ValueError(
                            f"Expanded source bytes exceed the configured aggregate limit ({max_total_bytes})."
                        )
                    observed = next_size
                    digest.update(chunk)
                    target.write(chunk)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"local source could not be packaged: {exc}") from exc
        calculated = digest.hexdigest()
        if observed != expected_size or calculated != expected_sha256:
            raise ValueError("local source changed after prepare")
        return observed, calculated

    def _package_zip(self, draft: Mapping[str, Any], destination: Path) -> list[dict[str, Any]]:
        sources = draft.get("sources")
        if not isinstance(sources, list):
            raise ValueError("draft source list is invalid")
        if self.settings.max_source_count is not None and len(sources) > self.settings.max_source_count:
            raise ValueError("source count exceeds configured bound")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists() and not destination.is_file():
            raise ValueError("launch archive destination is not a regular file")
        # Build beside the requested destination and publish only after every
        # source has passed its binding/resource checks.  On any exception the
        # temporary inode is removed and a pre-existing destination is left
        # untouched.
        descriptor_fd: int | None = None
        staging_path: Path | None = None
        try:
            descriptor_fd, raw_staging = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            staging_path = Path(raw_staging)
            os.chmod(staging_path, 0o600)
            staging_file = os.fdopen(descriptor_fd, "w+b")
            descriptor_fd = None
        except OSError as exc:
            if descriptor_fd is not None:
                try:
                    os.close(descriptor_fd)
                except OSError:
                    pass
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)
            raise ValueError(f"launch archive staging could not be created: {exc}") from exc
        entries: list[dict[str, Any]] = []
        accepted_total = 0
        physical_total = 0
        physical_entry_total = 0
        names: set[str] = set()
        expanded_limit = _expanded_source_limit(self.settings)
        try:
            with staging_file:
                with zipfile.ZipFile(staging_file, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
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
                                size = source.get("size")
                                digest = source.get("sha256")
                        elif kind == "remote_url":
                            if _is_zip_name(relative):
                                raise ValueError("Remote ZIP sources are not supported; use a local upload or path.")
                            url = str(source.get("url"))
                            remote_limit = self.settings.upload_limit_bytes or DEFAULT_UPLOAD_LIMIT
                            if hasattr(self.fetcher, "fetch"):
                                remaining = (
                                    remote_limit
                                    if expanded_limit is None
                                    else max(0, expanded_limit - physical_total)
                                )
                                data, final_url = self.fetcher.fetch(
                                    url,
                                    max_bytes=min(remote_limit, remaining),
                                )
                            else:
                                remaining = (
                                    remote_limit
                                    if expanded_limit is None
                                    else max(0, expanded_limit - physical_total)
                                )
                                data, final_url = self.fetcher(
                                    url,
                                    max_bytes=min(remote_limit, remaining),
                                )
                            size = len(data)
                            digest = sha256_bytes(data)
                            output_key = _normalized_archive_name(relative)
                            if output_key in names:
                                raise ValueError("source path collision")
                            names.add(output_key)
                            if expanded_limit is not None and physical_total + size > expanded_limit:
                                raise ValueError(f"Expanded source bytes exceed the configured aggregate limit ({expanded_limit}).")
                            _require_disk_space(destination.parent, size, label="launch archive packaging")
                            archive.writestr(relative, data)
                            entries.append({"relativePath": relative, "kind": kind, "size": size, "sha256": digest, "source": final_url})
                            accepted_total += size
                            physical_total += size
                            continue
                        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                            raise ValueError("source bytes cannot be negative")
                        if (
                            kind != "remote_url"
                            and self.settings.upload_limit_bytes is not None
                            and size > self.settings.upload_limit_bytes
                        ):
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
                                _require_disk_space(
                                    destination.parent,
                                    inspection.expanded_size,
                                    label="launch archive packaging",
                                )
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
                        if expanded_limit is not None and physical_total > expanded_limit:
                            raise ValueError(f"Expanded source bytes exceed the configured aggregate limit ({expanded_limit}).")
                        _require_disk_space(destination.parent, size, label="launch archive packaging")
                        observed_size, observed_digest = self._stream_local_source_to_zip(
                            source_path,
                            archive,
                            relative,
                            expected_size=size,
                            expected_sha256=digest,
                            max_total_bytes=expanded_limit,
                            prior_total_bytes=physical_total,
                        )
                        entries.append({"relativePath": relative, "kind": kind, "size": observed_size, "sha256": observed_digest})
                        accepted_total += observed_size
                        physical_total += observed_size
                if expanded_limit is not None and (physical_total > expanded_limit or accepted_total > expanded_limit):
                    raise ValueError(f"Expanded source bytes exceed the configured aggregate limit ({expanded_limit}).")
                staging_file.flush()
                os.fsync(staging_file.fileno())
            os.replace(staging_path, destination)
            staging_path = None
            return entries
        finally:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)

    @staticmethod
    def _zip_member_payloads(path: Path) -> dict[str, tuple[str, int, str, Path]]:
        """Inventory one validated archive without retaining member bytes.

        Values are ``(display_name, expanded_size, sha256, source_path)``.
        Continuation merging compares hashes from bounded streams and opens
        the source member again only while copying the selected bytes into the
        atomically staged destination archive.
        """

        # Keep this helper safe when called directly as well as through the
        # normal continuation request path.  Preflight validates the bounded
        # central directory and archive structure before ``infolist`` builds
        # its metadata table.
        _preflight_zip_archive(path)

        payloads: dict[str, tuple[str, int, str, Path]] = {}
        if path.is_symlink() or not path.is_file():
            raise LaunchConflictError(f"Data-room archive cannot be merged: {path.name}")
        total_size = 0
        try:
            with zipfile.ZipFile(path, "r") as archive:
                for info in archive.infolist():
                    name = _validate_zip_member_name(_zip_raw_member_name(info), directory=info.is_dir())
                    if info.is_dir():
                        continue
                    if _is_zip_symlink(info) or _is_zip_special(info) or info.flag_bits & 0x1:
                        raise ValueError(f"ZIP member {name!r} is unsafe")
                    _check_zip_compression(info, name)
                    if info.file_size and info.compress_size == 0:
                        raise ValueError(f"ZIP member {name!r} has an infinite compression ratio")
                    if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO:
                        raise ValueError(f"ZIP member {name!r} exceeds compression ratio limit ({MAX_ZIP_COMPRESSION_RATIO})")
                    key = _normalized_archive_name(name)
                    if key in payloads:
                        raise ValueError(f"ZIP member {name!r} duplicates another member")
                    if MAX_ZIP_MEMBER_BYTES is not None and info.file_size > MAX_ZIP_MEMBER_BYTES:
                        raise ValueError(f"ZIP member {name!r} exceeds member byte limit ({MAX_ZIP_MEMBER_BYTES})")
                    total_size += int(info.file_size)
                    if MAX_ZIP_TOTAL_BYTES is not None and total_size > MAX_ZIP_TOTAL_BYTES:
                        raise ValueError(f"ZIP exceeds total byte limit ({MAX_ZIP_TOTAL_BYTES})")
                    digest = hashlib.sha256()
                    with archive.open(info, "r") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    payloads[key] = (name, int(info.file_size), digest.hexdigest(), path)
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, NotImplementedError) as exc:
            raise LaunchConflictError(f"Data-room archive cannot be merged: {path.name}") from exc
        return payloads

    @staticmethod
    def _write_deterministic_zip_streaming(
        members: Mapping[str, tuple[str, int, str, Path]],
        destination: Path,
    ) -> None:
        """Write selected archive members in bounded chunks and publish atomically."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise LaunchConflictError("Data-room merge candidate is not a regular file")
        required_bytes = sum(int(value[1]) for value in members.values())
        _require_disk_space(destination.parent, required_bytes, label="Data-room merge candidate")
        staging_path: Path | None = None
        source_archives: dict[Path, zipfile.ZipFile] = {}
        try:
            descriptor, raw_staging = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            os.close(descriptor)
            staging_path = Path(raw_staging)
            os.chmod(staging_path, 0o600)
            with zipfile.ZipFile(
                staging_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=True,
            ) as output:
                for key in sorted(members):
                    name, expected_size, expected_digest, source_path = members[key]
                    source_archive = source_archives.get(source_path)
                    if source_archive is None:
                        source_archive = zipfile.ZipFile(source_path, "r")
                        source_archives[source_path] = source_archive
                    try:
                        source_info = source_archive.getinfo(name)
                    except KeyError as exc:
                        raise LaunchConflictError(f"Data-room merge source member disappeared: {name}") from exc
                    output_info = zipfile.ZipInfo(name)
                    output_info.date_time = (1980, 1, 1, 0, 0, 0)
                    output_info.compress_type = zipfile.ZIP_DEFLATED
                    output_info.create_system = 3
                    output_info.external_attr = 0o600 << 16
                    output_info.comment = b""
                    output_info.extra = b""
                    observed_size = 0
                    digest = hashlib.sha256()
                    try:
                        with source_archive.open(source_info, "r") as source, output.open(output_info, "w") as target:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                observed_size += len(chunk)
                                digest.update(chunk)
                                target.write(chunk)
                    except Exception as exc:
                        raise LaunchConflictError(f"Data-room merge member failed CRC/content validation: {name}") from exc
                    if observed_size != expected_size or digest.hexdigest() != expected_digest:
                        raise LaunchConflictError(f"Data-room merge source changed while staging: {name}")
            with staging_path.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(staging_path, destination)
            staging_path = None
            _fsync_directory(destination.parent)
        except LaunchConflictError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
            raise LaunchConflictError("Data-room merge candidate could not be written") from exc
        finally:
            for source_archive in source_archives.values():
                try:
                    source_archive.close()
                except OSError:
                    pass
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)

    @staticmethod
    def _write_deterministic_zip(
        payloads: Mapping[str, tuple[str, bytes, str]],
        destination: Path,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists() and not destination.is_file():
            raise LaunchConflictError("Data-room merge candidate is not a regular file")
        try:
            with zipfile.ZipFile(
                destination,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=True,
            ) as archive:
                for key in sorted(payloads):
                    name, data, _digest = payloads[key]
                    info = zipfile.ZipInfo(name)
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    info.external_attr = 0o600 << 16
                    info.comment = b""
                    info.extra = b""
                    archive.writestr(info, data)
        except (OSError, ValueError, zipfile.LargeZipFile) as exc:
            raise LaunchConflictError("Data-room merge candidate could not be written") from exc
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())

    def _merge_data_room_archives(
        self,
        current_archive: Path,
        addition_archive: Path,
        destination: Path,
        *,
        source_entries: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Deterministically merge current bytes with one source snapshot."""

        current = self._zip_member_payloads(current_archive)
        additions = self._zip_member_payloads(addition_archive)
        merged = dict(current)
        added: list[str] = []
        replaced: list[str] = []
        unchanged: list[str] = []
        for key in sorted(additions):
            value = additions[key]
            prior = current.get(key)
            if prior is None:
                merged[key] = value
                added.append(value[0])
            elif prior[2] == value[2]:
                unchanged.append(prior[0])
            else:
                merged[key] = value
                replaced.append(value[0])
        changed = bool(added or replaced)
        if changed:
            self._write_deterministic_zip_streaming(merged, destination)
            candidate_sha = sha256_file(destination)
            candidate_size = destination.stat().st_size
        else:
            candidate_sha = sha256_file(current_archive)
            candidate_size = current_archive.stat().st_size
        return {
            "candidatePath": str(destination) if changed else None,
            "candidateSha256": candidate_sha,
            "candidateSizeBytes": int(candidate_size),
            "addedPaths": added,
            "replacedPaths": replaced,
            "unchangedPaths": unchanged,
            "sourceEntries": [dict(value) for value in source_entries],
            "changed": changed,
        }

    @staticmethod
    def _validated_revision_ancestor(store: Any, current_revision: Any, expected: Mapping[str, Any]) -> bool:
        """Return whether the draft's immutable D is on current D's chain.

        The expected manifest/archive identity is validated before walking the
        parent links.  Each link is loaded strictly and its bound parent hash
        is checked, so a pointer-only fork, missing ancestor, cycle, or
        tampered manifest fails closed rather than silently rebasing sources.
        """

        expected_id = expected.get("revisionId")
        expected_manifest_hash = expected.get("manifestHash")
        expected_archive_hash = expected.get("archiveSha256")
        expected_size = expected.get("archiveSizeBytes")
        if (
            not isinstance(expected_id, str)
            or not isinstance(expected_manifest_hash, str)
            or not isinstance(expected_archive_hash, str)
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise LaunchConflictError("Launch draft data revision identity is incomplete")
        try:
            expected_revision = store.load(expected_id)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Launch draft data revision ancestor is unavailable") from exc
        if (
            expected_revision.manifest_hash != expected_manifest_hash
            or expected_revision.archive_sha256 != expected_archive_hash
            or expected_revision.archive_size_bytes != expected_size
        ):
            raise LaunchConflictError("Launch draft data revision ancestor identity is stale")
        seen: set[str] = set()
        cursor = current_revision
        while cursor is not None:
            revision_id = getattr(cursor, "revision_id", None)
            if not isinstance(revision_id, str) or revision_id in seen:
                raise LaunchConflictError("Data revision parent lineage is cyclic or invalid")
            seen.add(revision_id)
            if revision_id == expected_id:
                if cursor.manifest_hash != expected_manifest_hash or cursor.archive_sha256 != expected_archive_hash:
                    raise LaunchConflictError("Data revision parent lineage is hash-inconsistent")
                return True
            parent_id = getattr(cursor, "parent_revision_id", None)
            parent_hash = getattr(cursor, "parent_manifest_hash", None)
            if parent_id is None or parent_hash is None:
                return False
            try:
                parent = store.load(parent_id)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Data revision parent lineage is unavailable") from exc
            if parent.manifest_hash != parent_hash:
                raise LaunchConflictError("Data revision parent lineage hash is inconsistent")
            cursor = parent
        return False

    def _ensure_data_revision(
        self,
        draft: Mapping[str, Any],
        run_root: Path,
        staging_root: Path,
        *,
        intent: Mapping[str, Any] | None = None,
        publish: bool = True,
        transaction_handoff: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Initialize/materialize the current D revision and return its binding.

        ``publish=False`` keeps a changed deterministic merge as a confined
        candidate for the semantic intake planner.  The caller publishes that
        already-built candidate only after planning succeeds.
        """

        api = self._core_imports()
        run_id = safe_component(draft.get("runId"), "run_id")
        observed = self._discover_existing_data_room(run_root, run_id)
        expected = draft.get("dataRevision")
        if not isinstance(expected, Mapping):
            raise LaunchConflictError("Launch draft is missing its authoritative data revision identity")
        observed_identity = self._data_revision_identity(observed)
        intended = intent.get("dataRevision") if isinstance(intent, Mapping) else None
        intended_matches = isinstance(intended, Mapping) and all(
            observed_identity.get(key) == intended.get(key)
            for key in ("revisionId", "manifestHash", "archiveSha256", "archiveSizeBytes")
        )
        expected_matches = all(
            observed_identity.get(key) == expected.get(key)
            for key in ("revisionId", "manifestHash", "archiveSha256", "archiveSizeBytes")
        )
        legacy_initialized = bool(
            not expected_matches
            and observed.get("revision") is not None
            and observed["revision"].revision_id == "D-0001"
            and observed["revision"].archive_alias
            and observed_identity.get("archiveSha256") == expected.get("archiveSha256")
        )
        recovery_provenance: dict[str, Any] | None = None
        context = api["RunContext"](
            run_id,
            run_root,
            input_roots=observed["inputRoots"],
        )
        store = api["DataRevisionStore"](context)
        try:
            # A prior append may have published D and its complete handoff
            # journal before the canonical admission/intent write.  Recover
            # that immutable plan before this draft asks the Planner for a
            # semantic parent, so another draft cannot allocate over it.
            store.recover_revision_transaction()
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Data revision handoff recovery failed") from exc
        revision = observed.get("revision")
        lineage_rebase = bool(
            not expected_matches
            and not intended_matches
            and revision is not None
            and self._validated_revision_ancestor(store, revision, expected)
        )
        if not expected_matches and not intended_matches:
            # A process may have crashed after the D pointer swap but before
            # writing continuation_intent.json.  Recover only when the
            # current archive is exactly the deterministic merge of the
            # draft's expected parent and source snapshot; any other current
            # revision remains a stale CAS conflict.
            raw_sources = draft.get("sources")
            recoverable_journal = False
            if not isinstance(raw_sources, list) or not raw_sources:
                expected_id = expected.get("revisionId")
                expected_hash = expected.get("manifestHash")
                transaction = store.revision_transaction()
                pending = store.pending_data_refresh(allow_stale=True)
                expected_revision = None
                if isinstance(expected_id, str) and isinstance(expected_hash, str):
                    try:
                        expected_revision = store.load(expected_id)
                    except (OSError, KeyError, TypeError, ValueError):
                        expected_revision = None
                recoverable_journal = bool(
                    isinstance(expected_id, str)
                    and isinstance(expected_hash, str)
                    and expected_revision is not None
                    and expected_revision.manifest_hash == expected_hash
                    and revision is not None
                    and revision.ordinal == expected_revision.ordinal + 1
                    and revision.revision_id == observed_identity.get("revisionId")
                    and transaction is not None
                    and transaction.revision_id == revision.revision_id
                    and transaction.revision_manifest_hash == revision.manifest_hash
                ) or bool(
                    pending is not None
                    and revision is not None
                    and pending.data_revision_id == revision.revision_id
                    and pending.data_revision_manifest_hash == revision.manifest_hash
                )
            if (
                (not isinstance(raw_sources, list) or not raw_sources)
                and not recoverable_journal
                and not lineage_rebase
                and not legacy_initialized
            ):
                raise LaunchConflictError("Current data revision changed since the launch draft was prepared")
        if revision is None:
            try:
                revision = store.initialize_legacy(
                    observed["archivePath"],
                )
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Legacy data-room archive could not be initialized") from exc
            observed = self._discover_existing_data_room(run_root, run_id)
            revision = observed.get("revision")
        if revision is None:
            raise LaunchConflictError("Current data revision is unavailable after initialization")

        source_entries: list[dict[str, Any]] = []
        raw_sources = draft.get("sources")
        if isinstance(raw_sources, list) and raw_sources:
            addition_archive = staging_root / "source_additions.zip"
            try:
                source_entries = self._package_zip(draft, addition_archive)
                candidate_path = run_root / "control_center" / "launches" / safe_component(
                    draft.get("draftId"), "draftId"
                ) / ".data-merge-candidate.zip"
                reject_symlink_components(candidate_path.parent, run_root)
                merge_base = Path(revision.archive_path)
                if not expected_matches and not intended_matches and not lineage_rebase:
                    expected_revision_id = expected.get("revisionId")
                    if isinstance(expected_revision_id, str):
                        try:
                            expected_revision = store.load(expected_revision_id)
                        except (OSError, KeyError, TypeError, ValueError) as exc:
                            raise LaunchConflictError("Expected data revision is unavailable for recovery") from exc
                        if revision.ordinal != expected_revision.ordinal + 1:
                            raise LaunchConflictError("Current data revision is not the expected continuation target")
                        merge_base = Path(expected_revision.archive_path)
                    else:
                        merge_base = run_root / "inputs" / "data_room.zip"
                        if not merge_base.is_file() or merge_base.is_symlink() or sha256_file(merge_base) != expected.get("archiveSha256"):
                            raise LaunchConflictError("Expected legacy data-room archive is unavailable for recovery")
                merge = self._merge_data_room_archives(
                    merge_base,
                    addition_archive,
                    candidate_path,
                    source_entries=source_entries,
                )
                if not expected_matches and not intended_matches and not legacy_initialized and not lineage_rebase:
                    if merge["candidateSha256"] != revision.archive_sha256:
                        raise LaunchConflictError("Current data revision changed since the launch draft was prepared")
                    merge["changed"] = True
                    merge["candidatePath"] = None
                    recovery_provenance = merge
                elif merge["changed"] and publish:
                    transaction_metadata = {
                        "launch_draft_id": safe_component(draft.get("draftId"), "draftId"),
                        "launch_fingerprint": str(draft.get("fingerprint") or ""),
                        "created_at": str(draft.get("createdAt") or utc_now()),
                    }
                    if isinstance(transaction_handoff, Mapping):
                        transaction_metadata.update(dict(transaction_handoff))
                    try:
                        revision = store.append(
                            candidate_path,
                            expected_current_revision_id=revision.revision_id,
                            expected_current_manifest_hash=revision.manifest_hash,
                            transaction=transaction_metadata,
                        )
                    except (OSError, KeyError, TypeError, ValueError) as exc:
                        raise LaunchConflictError("Current data revision changed while appending sources") from exc
                    observed = self._discover_existing_data_room(run_root, run_id)
                elif merge["changed"]:
                    # Keep the immutable candidate available for planning;
                    # no current pointer or revision directory is changed in
                    # this phase.
                    pass
                else:
                    merge["candidatePath"] = None
                provenance = recovery_provenance or merge
            finally:
                addition_archive.unlink(missing_ok=True)
                candidate_path = locals().get("candidate_path")
                keep_candidate = (
                    not publish
                    and isinstance(candidate_path, Path)
                    and bool(locals().get("merge", {}).get("changed"))
                )
                if isinstance(candidate_path, Path) and not keep_candidate:
                    candidate_path.unlink(missing_ok=True)
        else:
            provenance = {
                "candidatePath": None,
                "candidateSha256": revision.archive_sha256,
                "candidateSizeBytes": revision.archive_size_bytes,
                "addedPaths": [],
                "replacedPaths": [],
                "unchangedPaths": [],
                "sourceEntries": [],
                "changed": False,
            }
        return {
            "context": api["RunContext"](
                run_id,
                run_root,
                input_roots=observed["inputRoots"],
            ),
            "revision": revision,
            "dataRoom": observed,
            "identity": self._data_revision_identity(observed),
            "provenance": provenance,
            "candidatePath": (
                Path(str(provenance["candidatePath"]))
                if not publish and provenance.get("candidatePath")
                else None
            ),
        }

    def _publish_staged_data_revision(
        self,
        draft: Mapping[str, Any],
        run_root: Path,
        staged: Mapping[str, Any],
        *,
        transaction_handoff: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish a candidate after planning, retaining one append CAS."""

        candidate_path = staged.get("candidatePath")
        revision = staged.get("revision")
        if not isinstance(candidate_path, Path) or revision is None:
            return dict(staged)
        api = self._core_imports()
        transaction_metadata = {
            "launch_draft_id": safe_component(draft.get("draftId"), "draftId"),
            "launch_fingerprint": str(draft.get("fingerprint") or ""),
            "created_at": str(draft.get("createdAt") or utc_now()),
        }
        if isinstance(transaction_handoff, Mapping):
            transaction_metadata.update(dict(transaction_handoff))
        try:
            published_revision = api["DataRevisionStore"](staged["context"]).append(
                candidate_path,
                expected_current_revision_id=revision.revision_id,
                expected_current_manifest_hash=revision.manifest_hash,
                transaction=transaction_metadata,
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Current data revision changed while publishing sources") from exc
        observed = self._discover_existing_data_room(run_root, safe_component(draft.get("runId"), "run_id"))
        result = dict(staged)
        result["revision"] = published_revision
        result["context"] = api["RunContext"](
            safe_component(draft.get("runId"), "run_id"),
            run_root,
            input_roots=observed["inputRoots"],
        )
        result["dataRoom"] = observed
        result["identity"] = self._data_revision_identity(observed)
        provenance = dict(staged.get("provenance") or {})
        provenance["candidatePath"] = None
        result["provenance"] = provenance
        result["candidatePath"] = candidate_path
        return result

    def _core_imports(self):
        # Operational execution must use the source tree that owns this app.
        # Importing an arbitrary installed package (or silently retrying after
        # an internal ImportError) can bind a run to a different core release.
        repo_src = (Path(__file__).resolve().parents[2] / "src").resolve(strict=False)
        package_init = repo_src / "auto_foundry_core" / "__init__.py"
        if not repo_src.is_dir() or not package_init.is_file():
            raise LaunchConflictError(
                "Current checkout core source is unavailable; operational execution is disabled"
            )
        import sys

        def _module_is_from_checkout(module: Any) -> bool:
            origin = getattr(module, "__file__", None)
            if not origin:
                return False
            try:
                Path(origin).resolve(strict=False).relative_to(repo_src)
            except (OSError, ValueError):
                return False
            return True

        loaded = sys.modules.get("auto_foundry_core")
        if loaded is not None and not _module_is_from_checkout(loaded):
            raise LaunchConflictError(
                "Operational core is already imported from outside the current checkout"
            )
        if str(repo_src) not in sys.path:
            sys.path.insert(0, str(repo_src))
        try:
            from auto_foundry_core import (
                CoordinatorRunSpec,
                DataRevision,
                DataRevisionError,
                DataRevisionStore,
                EntityResolutionWorkspace,
                ItemWorkspace,
                RequirementExecutionPlan,
                RequirementRecord,
                RequirementRunExtension,
                RequirementSupervisorWorkspace,
                ResolutionCapacity,
                RunCoordinator,
                CoordinatorConflictError,
                CoordinatorProductionBindingMismatch,
                RunContext,
                RunLifecycle,
                production_role_routing,
            )
            from auto_foundry_core.requirement_planning import AUTHORIZED_ACTION_ROLE_CONTRACTS
            from auto_foundry_core.coordinator import resolve_production_skill_binding
        except (ImportError, ModuleNotFoundError) as exc:
            raise LaunchConflictError(
                "Current checkout core could not be imported; operational execution is disabled"
            ) from exc
        imported_modules = {
            "auto_foundry_core",
            "auto_foundry_core.coordinator",
            "auto_foundry_core.requirement_planning",
        }
        for value in (
            CoordinatorRunSpec,
            DataRevision,
            DataRevisionError,
            DataRevisionStore,
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
            production_role_routing,
        ):
            imported_modules.add(str(getattr(value, "__module__", "")))
        for module_name in imported_modules:
            module = sys.modules.get(module_name)
            if module is None or not _module_is_from_checkout(module):
                raise LaunchConflictError(
                    "Operational core import is not bound to the current checkout"
                )
        return {
            "CoordinatorRunSpec": CoordinatorRunSpec,
            "DataRevision": DataRevision,
            "DataRevisionError": DataRevisionError,
            "DataRevisionStore": DataRevisionStore,
            "EntityResolutionWorkspace": EntityResolutionWorkspace,
            "ItemWorkspace": ItemWorkspace,
            "RequirementExecutionPlan": RequirementExecutionPlan,
            "RequirementRecord": RequirementRecord,
            "RequirementRunExtension": RequirementRunExtension,
            "RequirementSupervisorWorkspace": RequirementSupervisorWorkspace,
            "ResolutionCapacity": ResolutionCapacity,
            "RunCoordinator": RunCoordinator,
            "CoordinatorConflictError": CoordinatorConflictError,
            "CoordinatorProductionBindingMismatch": CoordinatorProductionBindingMismatch,
            "RunContext": RunContext,
            "RunLifecycle": RunLifecycle,
            "resolve_production_skill_binding": resolve_production_skill_binding,
            "production_role_routing": production_role_routing,
            "authorized_action_role_contracts": AUTHORIZED_ACTION_ROLE_CONTRACTS,
        }

    @staticmethod
    def _production_role_bindings(api: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
        """Return the reviewed current role maps for persisted execution.

        ``ROLE_MODEL_CONTRACT`` also exposes historical aliases and non-Planner
        helper labels.  They remain readable in the core manifest but are not
        dispatch authority for a new operational run.  The typed
        ``AUTHORIZED_ACTION_ROLE_CONTRACTS`` list is the canonical set; intake
        and supervision are explicit additional boundaries.
        """

        raw_routes = api["production_role_routing"]()
        contracts = api.get("authorized_action_role_contracts")
        if not isinstance(raw_routes, Mapping) or not isinstance(contracts, (tuple, list)):
            raise LaunchConflictError("Production role routing manifest is unavailable")
        # ``planner`` owns deterministic control-plane transitions (wait,
        # pause, rethink, and recovery markers), not a model transport route.
        # Keep those typed action contracts for validation, but do not require
        # an ambient/legacy model binding for them.  Every other role in the
        # canonical action contract is dispatchable and must have one explicit
        # route in the current production manifest.
        non_dispatchable_roles = {"planner"}
        canonical_roles = {
            str(contract.role).strip().lower()
            for contract in contracts
            if getattr(contract, "role", None)
            and str(contract.role).strip().lower() not in non_dispatchable_roles
        }
        canonical_roles.update({"intake_planner", "foundry_supervisor"})
        if not canonical_roles or any(not role for role in canonical_roles):
            raise LaunchConflictError("Production role routing manifest has no canonical dispatch roles")
        role_models: dict[str, str] = {}
        role_reasoning: dict[str, str] = {}
        for role in sorted(canonical_roles):
            route = raw_routes.get(role)
            if not isinstance(route, Mapping):
                raise LaunchConflictError(f"Production role route is missing: {role}")
            model = route.get("model")
            reasoning = route.get("reasoning_effort")
            if (
                not isinstance(model, str)
                or not model.strip()
                or not isinstance(reasoning, str)
                or not reasoning.strip()
            ):
                raise LaunchConflictError(f"Production role route is incomplete: {role}")
            role_models[role] = model.strip()
            role_reasoning[role] = reasoning.strip().lower()
        if set(role_models) != canonical_roles or set(role_reasoning) != canonical_roles:
            raise LaunchConflictError("Production role routing manifest is incomplete")
        return role_models, role_reasoning

    @staticmethod
    def _canonical_publication_policy(value: Any) -> dict[str, bool]:
        """Validate the one current publication-policy contract.

        Operational launches deliberately start with publication disabled.  A
        caller may not smuggle destinations, channels, or legacy aliases into
        the persisted coordinator spec; the coordinator owns only the exact
        ``{"enabled": bool}`` policy that is hash-bound by its spec.
        """

        if not isinstance(value, Mapping) or set(value) != {"enabled"} or not isinstance(value.get("enabled"), bool):
            raise LaunchConflictError("Coordinator publication policy must be exactly {enabled: bool}")
        return {"enabled": bool(value["enabled"])}

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

    def _discover_legacy_data_room(self, run_root: Path, run_id: str) -> dict[str, Any]:
        """Resolve an existing run's pre-revision archive/catalog safely."""

        # Operationally-created runs keep their immutable package inside the
        # run root.  Preserve this relative reference for the manifest while
        # still validating the lexical path and bounded hash before reuse.
        packaged = run_root / "inputs" / "data_room.zip"
        if packaged.is_file() and not packaged.is_symlink() and is_within(packaged, (run_root,)):
            try:
                size = packaged.stat().st_size
            except OSError as exc:
                raise LaunchConflictError("Existing run data room is unreadable") from exc
            if self.settings.max_source_total_bytes is not None and size > self.settings.max_source_total_bytes:
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
            if size != expected_size or (
                self.settings.max_source_total_bytes is not None
                and size > self.settings.max_source_total_bytes
            ):
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

    def _discover_existing_data_room(self, run_root: Path, run_id: str) -> dict[str, Any]:
        """Resolve the pointer-authoritative current data revision, if present.

        Legacy runs are intentionally left untouched until execute initializes
        D-0001.  A present revision pointer is strict: malformed/tampered
        revision state is a launch conflict, never a fallback to an older
        archive.
        """

        pointer_path = run_root / "data_room" / "current_revision.json"
        if not pointer_path.exists() and not pointer_path.is_symlink():
            return self._discover_legacy_data_room(run_root, run_id)
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise LaunchConflictError("Existing run data-revision pointer is unavailable")
        revisions_root = pointer_path.parent / "revisions"
        if revisions_root.is_symlink() or not revisions_root.is_dir():
            raise LaunchConflictError("Existing run data-revision store is unavailable")
        api = self._core_imports()
        input_roots_list = list(self._validated_input_roots(run_root, run_id))
        packaged = run_root / "inputs" / "data_room.zip"
        if packaged.is_file() and not packaged.is_symlink():
            input_roots_list.append(packaged.parent.resolve(strict=True))
        if not input_roots_list:
            input_roots_list.append(run_root / "inputs")
        input_roots = tuple(dict.fromkeys((*input_roots_list, revisions_root)))
        try:
            context = api["RunContext"](run_id, run_root, input_roots=input_roots)
            revision = api["DataRevisionStore"](context).current()
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Existing run data-revision state is invalid") from exc
        if revision is None:
            raise LaunchConflictError("Existing run data-revision pointer is empty")
        archive_path = Path(revision.archive_path)
        if archive_path.is_symlink() or not archive_path.is_file() or not is_within(archive_path, (run_root,)):
            raise LaunchConflictError("Current data-revision archive is unavailable")
        archive_ref = archive_path.relative_to(run_root).as_posix()
        roots = list(input_roots_list)
        if archive_path.parent not in roots:
            roots.append(archive_path.parent)
        return {
            "archivePath": archive_path,
            "sha256": revision.archive_sha256,
            "size": revision.archive_size_bytes,
            "catalogPath": revision.catalog_path,
            "inputRoots": tuple(roots),
            "dataRoom": archive_ref,
            "revision": revision,
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
            if extension in PLANNER_DOCUMENT_EXTENSIONS:
                refs.append(value)
        return tuple(dict.fromkeys(refs))

    @staticmethod
    def _data_revision_identity(data_room: Mapping[str, Any]) -> dict[str, Any]:
        """Return the immutable identity bound into a launch fingerprint."""

        revision = data_room.get("revision")
        return {
            "revisionId": None if revision is None else revision.revision_id,
            "manifestHash": None if revision is None else revision.manifest_hash,
            "archiveSha256": str(data_room["sha256"]),
            "archiveSizeBytes": int(data_room["size"]),
        }

    @staticmethod
    def _data_room_document_refs(
        draft: Mapping[str, Any],
        run_root: Path,
        data_room: str,
    ) -> tuple[str, ...]:
        _projection, catalog = LaunchManager._document_catalog_for_planner(
            run_root,
            data_room,
            allowed_roots=(Path(run_root),),
        )
        if catalog is None:
            return ()
        return tuple(document.document_ref for document in catalog.documents)

    @staticmethod
    def _document_catalog_for_planner(
        run_root: Path,
        data_room: str,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[dict[str, Any], Any | None]:
        """Build a bounded document catalog without making raw bytes planner input.

        The immutable data-room archive remains the source of truth.  A safe
        member that cannot be decoded is represented as an opaque/limited
        catalog entry; only unsafe archive structure is escalated to a launch
        conflict.  The second return value is the typed ``DocumentCatalog``
        when available and is kept private to this launch adapter.
        """

        from auto_foundry_core.document_ingestion import (
            DEFAULT_MAX_MEMBER_BYTES,
            DEFAULT_MAX_TOTAL_BYTES,
            UnsafeDocumentArchiveError,
            ingest_document_catalog,
        )

        path = Path(str(data_room)).expanduser()
        if not path.is_absolute():
            path = run_root / path
        # Data-room paths are created by this module or by the validated core
        # revision store.  Reject aliases before handing them to a reader.
        roots = tuple(Path(value).expanduser().resolve(strict=False) for value in (run_root, *allowed_roots))
        matched_root = next((root for root in roots if is_within(path, (root,))), None)
        if matched_root is None:
            raise LaunchConflictError("Data-room document catalog path is unsafe")
        try:
            reject_symlink_components(path, matched_root)
        except ValueError as exc:
            raise LaunchConflictError("Data-room document catalog path is unsafe") from exc
        try:
            document_member_limit = DEFAULT_MAX_MEMBER_BYTES
            if MAX_ZIP_MEMBER_BYTES is not None:
                document_member_limit = (
                    MAX_ZIP_MEMBER_BYTES
                    if document_member_limit is None
                    else max(document_member_limit, MAX_ZIP_MEMBER_BYTES)
                )
            document_total_limit = DEFAULT_MAX_TOTAL_BYTES
            if MAX_ZIP_TOTAL_BYTES is not None:
                document_total_limit = (
                    MAX_ZIP_TOTAL_BYTES
                    if document_total_limit is None
                    else max(document_total_limit, MAX_ZIP_TOTAL_BYTES)
                )
            catalog = ingest_document_catalog(
                path,
                # Catalog every admitted supported member.  Excerpt limits
                # below bound Planner input; count caps are optional and only
                # apply when an explicit caller/test setting supplies one.
                max_documents=MAX_ZIP_MEMBER_COUNT,
                # Extraction limits are intentionally below the launch ZIP
                # bounds.  Oversized safe source members become opaque rather
                # than expanding Planner input; the raw archive remains
                # available for owner-authored fallback work.
                max_member_bytes=document_member_limit,
                max_total_bytes=document_total_limit,
                max_excerpt_bytes=MAX_PLANNER_DOCUMENT_EXCERPT_BYTES,
                max_entries=MAX_ZIP_PHYSICAL_ENTRY_COUNT,
                max_parsed_pdfs=MAX_CATALOG_PARSED_PDFS,
                max_pdf_total_wall_seconds=MAX_CATALOG_PDF_TOTAL_WALL_SECONDS,
                max_pdf_total_output_bytes=MAX_CATALOG_PDF_TOTAL_OUTPUT_BYTES,
                max_total_normalized_text_bytes=MAX_CATALOG_NORMALIZED_TEXT_BYTES,
                # This archive has already passed the native Data Room
                # admission boundary.  The Planner adapter extracts only real
                # documents and treats extraction budgets as soft; structured
                # sources such as Parquet/SQLite stay in the native catalog and
                # can no longer fail launch as oversized "documents".
                include_opaque_members=False,
                strict_archive_resource_limits=False,
            )
        except UnsafeDocumentArchiveError as exc:
            raise LaunchConflictError("Data-room document archive is unsafe") from exc
        except (OSError, ValueError, TypeError) as exc:
            # A readable data room with an unsupported/failed reader still has
            # a durable opaque catalog record.  Keep planner input bounded and
            # let the owner inspect the raw member explicitly later.
            fallback = {
                "schema_version": 1,
                "documents": [],
                "limitations": [f"document catalog unavailable: {exc}"],
                "bounded": True,
                "max_excerpt_bytes": MAX_PLANNER_DOCUMENT_EXCERPT_BYTES,
                "max_excerpts": MAX_PLANNER_DOCUMENT_EXCERPTS,
            }
            return fallback, None
        payload = catalog.planner_payload(
            max_excerpt_bytes=MAX_PLANNER_DOCUMENT_EXCERPT_BYTES,
            max_excerpts=MAX_PLANNER_DOCUMENT_EXCERPTS,
        )
        return payload, catalog

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
        if not isinstance(value, Mapping):
            raise IntakeRepresentationError(f"{label} must be an exact source span")
        raw = dict(value)
        allowed = {"blockId", "block_id", "start", "end"}
        if set(raw) - allowed or not {"start", "end"}.issubset(raw):
            raise IntakeRepresentationError(f"{label} must be an exact source span")
        block_id = raw.get("blockId", raw.get("block_id"))
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
            raise IntakeRepresentationError(f"{label} is outside its exact input block")
        return block_id, start, end

    def _materialize_intake_plan(
        self,
        api: Mapping[str, Any],
        draft: Mapping[str, Any],
        interpretation: Mapping[str, Any],
        *,
        parent_plan: Any | None = None,
        document_refs: tuple[str, ...] = (),
        document_catalog: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate a cognitive interpretation and build the exact durable plan."""

        def normalize_mapping(
            value: Any,
            *,
            aliases: Mapping[str, str],
            label: str,
        ) -> Any:
            """Canonicalise safe wire aliases without dropping unknown fields."""

            if not isinstance(value, Mapping):
                return value
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                canonical = aliases.get(str(key), str(key))
                if canonical in normalized and normalized[canonical] != item:
                    raise IntakeRepresentationError(f"{label} contains conflicting aliases for {canonical}")
                normalized[canonical] = item
            return normalized

        def normalize_collection(value: Any, *, label: str, strings: bool = False) -> Any:
            """Accept a singleton wire object where the contract is a list."""

            if value is None:
                return []
            if isinstance(value, Mapping):
                if "items" in value:
                    value = value["items"]
                elif "values" in value:
                    value = value["values"]
                else:
                    return [value]
            if isinstance(value, str):
                return [value] if strings else value
            if isinstance(value, (bytes, bytearray)):
                return [value] if strings else value
            if isinstance(value, Mapping):
                return [value]
            if isinstance(value, tuple):
                return list(value)
            return value

        top_aliases = {
            "schema_version": "schemaVersion",
            "mission_intent": "missionIntent",
            "portfolio_strategy": "portfolioStrategy",
            "unassigned_context": "unassignedContext",
            "additional_context": "additionalContext",
            "product_brief": "productBrief",
            "source_context": "sourceContext",
            "technical_constraints": "technicalConstraints",
        }
        candidate_aliases = {
            "candidate_id": "candidateId",
            "source_spans": "sourceSpans",
            "source_bindings": "sourceBindings",
            "document_refs": "documentRefs",
            "original_text": "originalText",
            "business_objective": "businessObjective",
            "expected_analytical_outputs": "expectedAnalyticalOutputs",
            "expected_visual_outputs": "expectedVisualOutputs",
            "data_needs": "dataNeeds",
            "ontology_needs": "ontologyNeeds",
            "prepared_data_needs": "preparedDataNeeds",
            "working_definitions": "workingDefinitions",
            "explicit_priority": "explicitPriority",
            "decomposition_rationale": "decompositionRationale",
        }
        group_aliases = {
            "shared_analysis_intent": "sharedAnalysisIntent",
            "suggested_specialists": "suggestedSpecialists",
        }
        interpretation = normalize_mapping(interpretation, aliases=top_aliases, label="Requirement Planner response")
        allowed_top = {
            "schemaVersion",
            "missionIntent",
            "portfolioStrategy",
            "requirements",
            "groups",
            "unassignedContext",
            "additionalContext",
            "productBrief",
            "sourceContext",
            "technicalConstraints",
        }
        if not isinstance(interpretation, Mapping):
            raise IntakeRepresentationError("Requirement Planner response must be an object")
        if set(interpretation) - allowed_top or interpretation.get("schemaVersion") != 1:
            raise IntakeRepresentationError("Requirement Planner response has an unsupported schema")
        raw_requirements = normalize_collection(interpretation.get("requirements"), label="requirements")
        if isinstance(raw_requirements, list):
            normalized_requirements: list[Any] = []
            for index, raw in enumerate(raw_requirements):
                normalized = normalize_mapping(raw, aliases=candidate_aliases, label=f"requirements[{index}]")
                if isinstance(normalized, Mapping):
                    normalized = dict(normalized)
                    normalized["sourceSpans"] = normalize_collection(
                        normalized.get("sourceSpans"), label=f"requirements[{index}].sourceSpans"
                    )
                    normalized["sourceBindings"] = normalize_collection(
                        normalized.get("sourceBindings"), label=f"requirements[{index}].sourceBindings"
                    )
                    normalized["documentRefs"] = normalize_collection(
                        normalized.get("documentRefs"), label=f"requirements[{index}].documentRefs", strings=True
                    )
                normalized_requirements.append(normalized)
            raw_requirements = normalized_requirements
        raw_groups = normalize_collection(interpretation.get("groups"), label="groups")
        if isinstance(raw_groups, list):
            normalized_groups: list[Any] = []
            for index, raw in enumerate(raw_groups):
                normalized = normalize_mapping(raw, aliases=group_aliases, label=f"groups[{index}]")
                if isinstance(normalized, Mapping):
                    normalized = dict(normalized)
                    normalized["members"] = normalize_collection(
                        normalized.get("members"), label=f"groups[{index}].members", strings=True
                    )
                    normalized["suggestedSpecialists"] = normalize_collection(
                        normalized.get("suggestedSpecialists"), label=f"groups[{index}].suggestedSpecialists", strings=True
                    )
                normalized_groups.append(normalized)
            raw_groups = normalized_groups
        raw_unassigned = interpretation.get("unassignedContext", [])
        strategy = interpretation.get("portfolioStrategy")
        if (
            not isinstance(raw_requirements, list)
            or (not raw_requirements and parent_plan is None)
            or len(raw_requirements) > MAX_REQUIREMENT_RECORDS
            or not isinstance(raw_groups, list)
            or not isinstance(raw_unassigned, list)
            or not isinstance(strategy, str)
            or not strategy.strip()
        ):
            raise IntakeRepresentationError("Requirement Planner response is incomplete")

        intake_blocks = self._intake_blocks(draft)
        block_text = {block["blockId"]: block["text"] for block in intake_blocks}
        block_order = {block["blockId"]: index for index, block in enumerate(intake_blocks)}
        covered: dict[str, list[tuple[int, int]]] = {block_id: [] for block_id in block_text}
        # ``document_refs`` is retained as a call-site hint only.  The
        # admissible set is always reconstructed from the trusted catalog
        # generated from exact source bytes; Planner-provided catalog data is
        # never consulted here.
        from auto_foundry_core.document_ingestion import DocumentCatalog

        try:
            trusted_catalog = DocumentCatalog.from_dict(document_catalog) if document_catalog is not None else DocumentCatalog()
        except (KeyError, TypeError, ValueError) as exc:
            raise IntakeSourceError("Trusted document catalog is invalid") from exc
        catalog_documents = {document.document_ref: document for document in trusted_catalog.documents}
        available_document_refs = set(catalog_documents)

        # Context source bindings share the same exact-span validator as
        # analytical requirements.  A document reference binds the entire
        # normalized document/member, while a block span binds only the exact
        # character interval.  This keeps every source statement durable even
        # when it is intentionally not an AO requirement.
        from auto_foundry_core.mission_context import ContextItem, MissionContext, MissionPlan

        def binding_source_ref(value: Mapping[str, Any]) -> Any:
            """Read the supported source-reference aliases in wire order."""

            for key in (
                "source_ref",
                "sourceRef",
                "document_ref",
                "documentRef",
                "ref",
                "blockId",
                "block_id",
            ):
                if value.get(key) is not None:
                    return value.get(key)
            return None

        def document_binding(
            raw_binding: Any,
            *,
            label: str,
        ) -> tuple[dict[str, Any], str]:
            """Validate one binding against a normalized catalog section."""

            if not isinstance(raw_binding, Mapping):
                raise IntakeRepresentationError(f"{label} is invalid")
            value = dict(raw_binding)
            source_ref = binding_source_ref(value)
            if not isinstance(source_ref, str) or not source_ref:
                raise IntakeRepresentationError(f"{label} has no source reference")
            if source_ref not in catalog_documents:
                raise IntakeSourceError(f"{label} references an unavailable trusted document")
            document = catalog_documents[source_ref]
            if document.extraction != "normalized":
                raise IntakeSourceError(f"{label} references a limited or opaque document")
            locator = value.get("locator", value.get("location"))
            if locator is None:
                locator = {
                    key: value[key]
                    for key in ("page", "sheet", "section", "row", "cell", "paragraph", "column")
                    if key in value
                }
            if not isinstance(locator, Mapping) or not locator:
                raise IntakeSourceError(f"{label} needs a normalized section locator")
            locator = {str(key): item for key, item in locator.items()}
            matches = [
                section
                for section in document.sections
                if all(section.locator.get(key) == item for key, item in locator.items())
            ]
            if len(matches) != 1:
                raise IntakeSourceError(f"{label} locator does not identify one normalized section")
            section = matches[0]
            if section.limitations:
                raise IntakeSourceError(f"{label} references a limited normalized section")
            normalized = {
                **value,
                "source_ref": source_ref,
                "locator": dict(section.locator),
                # The section digest is the sole admissible evidence hash;
                # the raw-document digest is not a locator-level proof.
                # The section digest is host-owned.  A Planner supplied hash
                # is redundant metadata and is never an admission predicate.
                "content_hash": section.content_hash,
                "text": section.text,
            }
            normalized.pop("sourceRef", None)
            normalized.pop("document_ref", None)
            normalized.pop("documentRef", None)
            normalized.pop("ref", None)
            normalized.pop("blockId", None)
            normalized.pop("block_id", None)
            normalized.pop("location", None)
            normalized.pop("contentHash", None)
            normalized.pop("source_path", None)
            normalized.pop("sourcePath", None)
            return normalized, section.text

        def context_item(
            raw: Any,
            *,
            label: str,
            default_reason: str | None = None,
        ) -> dict[str, Any]:
            if not isinstance(raw, Mapping):
                raise IntakeRepresentationError(f"{label} must be a source-grounded context item")
            value = dict(
                normalize_mapping(
                    raw,
                    aliases={
                        "source_spans": "sourceSpans",
                        "source_bindings": "sourceBindings",
                        "document_refs": "documentRefs",
                        "block_id": "blockId",
                    },
                    label=label,
                )
            )
            spans_raw = normalize_collection(
                value.pop("sourceSpans", value.pop("source_spans", [])),
                label=f"{label}.sourceSpans",
            )
            refs_raw = normalize_collection(
                value.pop("documentRefs", value.pop("document_refs", [])),
                label=f"{label}.documentRefs",
                strings=True,
            )
            bindings_raw = normalize_collection(
                value.pop("sourceBindings", value.pop("source_bindings", value.pop("bindings", []))),
                label=f"{label}.sourceBindings",
            )
            # Accept the original unassignedContext wire shape while writing
            # the new typed sidecar.  This is an evidence-shape conversion,
            # not a parser boundary: coverage still comes from exact spans.
            if not spans_raw and {"blockId", "start", "end"}.issubset(value):
                spans_raw = [{key: value.pop(key) for key in ("blockId", "start", "end")}]
            if not isinstance(spans_raw, list) or not isinstance(refs_raw, list) or not isinstance(bindings_raw, list):
                raise IntakeRepresentationError(f"{label} source bindings are invalid")
            if not all(isinstance(ref, str) and ref in available_document_refs for ref in refs_raw):
                raise IntakeSourceError(f"{label} references an unavailable document")
            normalized_bindings: list[dict[str, Any]] = []
            normalized_spans: list[tuple[str, int, int]] = []
            normalized_document_texts: list[str] = []
            for index, span in enumerate(spans_raw):
                block_id, start, end = self._intake_span(span, block_text, label=f"{label}.sourceSpans[{index}]")
                normalized_spans.append((block_id, start, end))
                covered[block_id].append((start, end))
                expected_text = block_text[block_id][start:end]
                normalized_bindings.append(
                    {
                        "source_ref": block_id,
                        "span": {"blockId": block_id, "start": start, "end": end},
                        "text": expected_text,
                        "content_hash": sha256_bytes(expected_text.encode("utf-8")),
                    }
                )
            for index, binding in enumerate(bindings_raw):
                if not isinstance(binding, Mapping):
                    raise IntakeRepresentationError(f"{label}.sourceBindings[{index}] is invalid")
                binding_value = dict(
                    normalize_mapping(
                        binding,
                        aliases={
                            "source_ref": "source_ref",
                            "sourceRef": "sourceRef",
                            "document_ref": "document_ref",
                            "documentRef": "documentRef",
                            "ref": "ref",
                            "block_id": "blockId",
                            "contentHash": "contentHash",
                        },
                        label=f"{label}.sourceBindings[{index}]",
                    )
                )
                source_ref = binding_source_ref(binding_value)
                if not isinstance(source_ref, str) or not source_ref:
                    raise IntakeRepresentationError(f"{label}.sourceBindings[{index}] has no source reference")
                if source_ref not in block_text and source_ref not in available_document_refs:
                    raise IntakeSourceError(f"{label}.sourceBindings[{index}] references an unavailable source")
                if source_ref in block_text:
                    raw_span = binding_value.get("span")
                    if raw_span is None and {"start", "end"}.issubset(binding_value):
                        raw_span = {
                            "blockId": source_ref,
                            "start": binding_value.get("start"),
                            "end": binding_value.get("end"),
                        }
                    if isinstance(raw_span, Mapping) and raw_span.get("blockId", raw_span.get("block_id", source_ref)) != source_ref:
                        raise IntakeRepresentationError(f"{label}.sourceBindings[{index}] span block does not match source_ref")
                    if not isinstance(raw_span, Mapping):
                        raise IntakeRepresentationError(f"{label}.sourceBindings[{index}] needs an exact span")
                    block_id, start, end = self._intake_span(
                        {"blockId": source_ref, "start": raw_span.get("start"), "end": raw_span.get("end")},
                        block_text,
                        label=f"{label}.sourceBindings[{index}].span",
                    )
                    covered[block_id].append((start, end))
                    normalized_spans.append((block_id, start, end))
                    expected_text = block_text[block_id][start:end]
                    binding_value["source_ref"] = source_ref
                    binding_value["span"] = {"blockId": block_id, "start": start, "end": end}
                    binding_value["text"] = expected_text
                    binding_value["content_hash"] = sha256_bytes(expected_text.encode("utf-8"))
                    for alias in ("sourceRef", "document_ref", "documentRef", "ref", "blockId", "block_id", "contentHash", "start", "end"):
                        binding_value.pop(alias, None)
                    normalized_bindings.append(binding_value)
                else:
                    normalized_binding, section_text = document_binding(
                        binding_value,
                        label=f"{label}.sourceBindings[{index}]",
                    )
                    normalized_bindings.append(normalized_binding)
                    normalized_document_texts.append(section_text)
            # A documentRefs declaration is evidence only when each reference
            # has an independently verified, hash-bound section locator.
            bound_document_refs = {
                binding.get("source_ref")
                for binding in normalized_bindings
                if binding.get("source_ref") in catalog_documents
            }
            if any(ref not in bound_document_refs for ref in refs_raw):
                raise IntakeSourceError(f"{label} documentRefs need matching sourceBindings")
            if not normalized_bindings:
                raise IntakeRepresentationError(f"{label} must include sourceSpans, documentRefs, or sourceBindings")
            text_value = value.pop("text", value.pop("value", value.pop("statement", None)))
            ordered = sorted(normalized_spans, key=lambda item: (block_order[item[0]], item[1], item[2]))
            expected_parts = [block_text[block_id][start:end] for block_id, start, end in ordered]
            expected_parts.extend(normalized_document_texts)
            expected_text = "\n\n".join(expected_parts)
            if expected_text:
                if text_value is None:
                    text_value = expected_text
                else:
                    # Supplied context text is a redundant Planner echo.  The
                    # exact span/section bytes above are authoritative.
                    text_value = expected_text
            elif not isinstance(text_value, str) or not text_value.strip():
                raise IntakeSourceError(f"{label} needs text when bound only to a document")
            reason = value.pop("reason", default_reason)
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise IntakeRepresentationError(f"{label} reason is invalid")
            metadata = value.pop("metadata", {})
            if value:
                metadata = {**dict(metadata or {}), **value}
            # Constructor validation catches malformed hash/locator details;
            # convert its errors to the launch conflict vocabulary.
            try:
                item = ContextItem.from_dict(
                    {
                        "text": text_value,
                        "source_bindings": normalized_bindings,
                        "reason": reason,
                        "metadata": metadata,
                    },
                    default_reason=default_reason,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntakeRepresentationError(f"{label} is invalid") from exc
            return item.to_dict()

        def context_list(raw: Any, *, label: str, default_reason: str | None = None) -> list[dict[str, Any]]:
            if raw is None:
                return []
            if isinstance(raw, Mapping):
                raw = raw.get("items", raw.get("values", raw))
                if isinstance(raw, Mapping):
                    raw = [raw]
            elif isinstance(raw, tuple):
                raw = list(raw)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, list):
                raise IntakeRepresentationError(f"{label} must be a list of source-grounded context items")
            return [context_item(item, label=f"{label}[{index}]", default_reason=default_reason) for index, item in enumerate(raw)]

        raw_product = interpretation.get("productBrief", {})
        if raw_product is None:
            raw_product = {}
        if not isinstance(raw_product, Mapping):
            raise IntakeRepresentationError("Requirement Planner productBrief is invalid")
        product = dict(raw_product)
        product_aliases = {"pagesOrModules": "pages_or_modules", "visualExpectations": "visual_expectations"}
        product_context: dict[str, list[dict[str, Any]]] = {}
        for field_name in ("audience", "decision", "deliverables", "pages_or_modules", "filters", "visual_expectations"):
            wire_name = next((key for key, normalized in product_aliases.items() if normalized == field_name), field_name)
            product_context[field_name] = context_list(
                product.get(field_name, product.get(wire_name, [])),
                label=f"productBrief.{wire_name}",
            )
        source_context = context_list(interpretation.get("sourceContext", interpretation.get("source_context", [])), label="sourceContext")
        technical_constraints = context_list(
            interpretation.get("technicalConstraints", interpretation.get("technical_constraints", [])),
            label="technicalConstraints",
        )
        raw_additional_context = interpretation.get("additionalContext")
        if raw_additional_context is None:
            raw_additional_context = interpretation.get("unassignedContext", [])
        elif interpretation.get("unassignedContext"):
            # New typed context and legacy unassigned spans may coexist while
            # a Planner skill is being upgraded; retain both source bindings.
            raw_additional_context = normalize_collection(raw_additional_context, label="additionalContext")
            legacy_context = normalize_collection(interpretation.get("unassignedContext"), label="unassignedContext")
            if isinstance(raw_additional_context, list) and isinstance(legacy_context, list):
                raw_additional_context = raw_additional_context + legacy_context
        additional_context = context_list(
            raw_additional_context,
            label="additionalContext",
            default_reason="additional context",
        )
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
            "sourceBindings",
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
                raise IntakeRepresentationError("Requirement Planner candidate is invalid")
            try:
                candidate_id = safe_component(raw.get("candidateId"), "candidateId")
            except (TypeError, ValueError) as exc:
                raise IntakeRepresentationError("Requirement Planner candidate ID is invalid") from exc
            if candidate_id in candidate_to_id or candidate_id in used_ids:
                raise IntakeRepresentationError("Requirement Planner candidate IDs are not unique")
            candidate_value = dict(raw)
            raw_spans = normalize_collection(candidate_value.get("sourceSpans"), label=f"requirements[{index}].sourceSpans")
            raw_document_refs = normalize_collection(
                candidate_value.get("documentRefs"), label=f"requirements[{index}].documentRefs", strings=True
            )
            raw_bindings = normalize_collection(candidate_value.get("sourceBindings"), label=f"requirements[{index}].sourceBindings")
            if isinstance(raw_bindings, Mapping):
                raw_bindings = [raw_bindings]
            if not isinstance(raw_spans, list) or not isinstance(raw_document_refs, list) or not isinstance(raw_bindings, list):
                raise IntakeRepresentationError("Requirement Planner sources are invalid")
            candidate_value["sourceSpans"] = raw_spans
            candidate_value["documentRefs"] = raw_document_refs
            candidate_value["sourceBindings"] = raw_bindings
            if (
                not all(isinstance(value, str) and value in available_document_refs for value in raw_document_refs)
                or not raw_spans and not raw_bindings
            ):
                if not all(isinstance(value, str) and value in available_document_refs for value in raw_document_refs):
                    raise IntakeSourceError("Requirement Planner requirement references an unavailable document")
                raise IntakeRepresentationError("Every planned requirement needs exact text or document sources")
            spans = [
                self._intake_span(value, block_text, label=f"requirements[{index}].sourceSpans")
                for value in raw_spans
            ]
            for block_id, start, end in spans:
                covered[block_id].append((start, end))
            normalized_bindings: list[dict[str, Any]] = []
            document_texts: list[str] = []
            bound_document_refs: set[str] = set()
            for binding_index, binding in enumerate(raw_bindings):
                if not isinstance(binding, Mapping):
                    raise IntakeRepresentationError(f"requirements[{index}].sourceBindings is invalid")
                binding = normalize_mapping(
                    binding,
                    aliases={
                        "sourceRef": "sourceRef",
                        "documentRef": "documentRef",
                        "block_id": "blockId",
                    },
                    label=f"requirements[{index}].sourceBindings[{binding_index}]",
                )
                source_ref = binding_source_ref(binding)
                if not isinstance(source_ref, str) or not source_ref:
                    raise IntakeRepresentationError(f"requirements[{index}].sourceBindings[{binding_index}] has no source reference")
                if source_ref in block_text:
                    raw_span = binding.get("span")
                    if raw_span is None and {"start", "end"}.issubset(binding):
                        raw_span = {"blockId": source_ref, "start": binding.get("start"), "end": binding.get("end")}
                    if isinstance(raw_span, Mapping) and raw_span.get("blockId", raw_span.get("block_id", source_ref)) != source_ref:
                        raise IntakeRepresentationError(f"requirements[{index}].sourceBindings[{binding_index}] span block does not match source_ref")
                    block_id, start, end = self._intake_span(
                        {"blockId": source_ref, "start": raw_span.get("start") if isinstance(raw_span, Mapping) else None, "end": raw_span.get("end") if isinstance(raw_span, Mapping) else None},
                        block_text,
                        label=f"requirements[{index}].sourceBindings[{binding_index}].span",
                    )
                    covered[block_id].append((start, end))
                    expected_text = block_text[block_id][start:end]
                    normalized_binding = {
                        **dict(binding),
                        "source_ref": block_id,
                        "span": {"blockId": block_id, "start": start, "end": end},
                        "text": expected_text,
                        "content_hash": sha256_bytes(expected_text.encode("utf-8")),
                    }
                    for alias in ("sourceRef", "document_ref", "documentRef", "ref", "blockId", "block_id", "contentHash", "start", "end"):
                        normalized_binding.pop(alias, None)
                    normalized_bindings.append(normalized_binding)
                else:
                    normalized_binding, section_text = document_binding(
                        binding,
                        label=f"requirements[{index}].sourceBindings[{binding_index}]",
                    )
                    normalized_bindings.append(normalized_binding)
                    document_texts.append(section_text)
                    bound_document_refs.add(normalized_binding["source_ref"])
            if any(ref not in bound_document_refs for ref in raw_document_refs):
                raise IntakeSourceError(f"requirements[{index}].documentRefs need matching sourceBindings")
            if normalized_bindings:
                candidate_value["sourceBindings"] = normalized_bindings
            while f"REQ-{next_number:03d}" in used_ids:
                next_number += 1
            requirement_id = f"REQ-{next_number:03d}"
            next_number += 1
            used_ids.add(requirement_id)
            candidate_to_id[candidate_id] = requirement_id
            candidate_value["_normalizedDocumentTexts"] = document_texts
            candidate_rows.append((candidate_id, candidate_value, spans))

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
                    raise IntakeSemanticError(
                        f"Requirement Planner dropped source text from {block_id} at offset {position}"
                    )

        def text_list(raw: Mapping[str, Any], name: str) -> list[str]:
            value = raw.get(name, [])
            if value is None:
                value = []
            elif isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise IntakeRepresentationError(f"Requirement Planner field {name} must be a string list")
            return list(value)

        records: list[Any] = []
        for candidate_id, raw, spans in candidate_rows:
            ordered = sorted(spans, key=lambda value: (block_order[value[0]], value[1], value[2]))
            document_refs = list(raw.get("documentRefs", []))
            if not document_refs:
                document_refs = list(
                    dict.fromkeys(
                        binding.get("source_ref")
                        for binding in raw.get("sourceBindings", [])
                        if isinstance(binding, Mapping) and binding.get("source_ref") in catalog_documents
                    )
                )
            derived_parts = [block_text[block_id][start:end] for block_id, start, end in ordered]
            derived_parts.extend(raw.get("_normalizedDocumentTexts", ()))
            derived_text = "\n\n".join(derived_parts)
            # Source spans and normalized sections are host-owned.  A Planner
            # originalText echo is metadata only and can never override or
            # veto the canonical bytes, including an intentionally empty or
            # whitespace-only trusted source excerpt.
            original_text = derived_text
            dependencies = text_list(raw, "dependencies")
            mapped_dependencies: list[str] = []
            existing_ids = {record.requirement_id for record in existing_records}
            for dependency in dependencies:
                mapped = candidate_to_id.get(dependency, dependency)
                if mapped not in used_ids and mapped not in existing_ids:
                    raise IntakeSemanticError("Requirement Planner dependency names an unknown requirement")
                mapped_dependencies.append(mapped)
            objective = raw.get("businessObjective", "")
            scope = raw.get("scope", "analytics")
            if not isinstance(objective, str) or not isinstance(scope, str) or not scope.strip():
                raise IntakeRepresentationError("Requirement Planner objective or scope is invalid")
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
                    "source_bindings": list(raw.get("sourceBindings", [])),
                    "decomposition_rationale": str(raw.get("decompositionRationale") or ""),
                },
            }
            try:
                records.append(api["RequirementRecord"].from_dict(value))
            except (KeyError, TypeError, ValueError) as exc:
                raise IntakeRepresentationError("Requirement Planner produced an invalid RequirementRecord") from exc

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
                raise IntakeRepresentationError("Requirement Planner group is invalid")
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
                raise IntakeRepresentationError("Requirement Planner group is incomplete")
            mapped_members = [candidate_to_id.get(member, member) for member in members]
            if any(member not in known_members for member in mapped_members):
                raise IntakeSemanticError("Requirement Planner group names an unknown requirement")
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
            raise IntakeSemanticError("Requirement Planner groups must cover every requirement exactly once")
        try:
            plan = api["RequirementExecutionPlan"](
                input_records=all_records,
                groups=tuple(groups),
                planner_ref="semantic-intake-planner",
                portfolio_strategy=strategy,
                revision=(parent_plan.revision + 1) if parent_plan is not None else 1,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntakeRepresentationError("Requirement Planner execution plan is invalid") from exc
        mission_intent = interpretation.get("missionIntent", interpretation.get("mission_intent", "specification"))
        try:
            mission_context = MissionContext(
                mission_intent=mission_intent,
                product_brief=product_context,
                source_context=source_context,
                technical_constraints=technical_constraints,
                additional_context=additional_context,
                document_catalog=document_catalog,
                metadata={
                    "draft_id": draft.get("draftId"),
                    "input_blocks": [block["blockId"] for block in intake_blocks],
                },
            )
            mission_plan = MissionPlan(
                mission_context=mission_context,
                requirement_ids=tuple(record.requirement_id for record in all_records),
                portfolio_strategy=strategy,
                planner_ref="semantic-intake-planner",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntakeRepresentationError("Requirement Planner mission context is invalid") from exc
        return {
            "records": tuple(records),
            "plan": plan,
            "interpretation": dict(interpretation),
            "missionContext": mission_context,
            "missionPlan": mission_plan,
        }

    def _plan_intake(
        self,
        api: Mapping[str, Any],
        draft: Mapping[str, Any],
        run_root: Path,
        *,
        data_room: str,
        parent_plan: Any | None = None,
        parent_context: Any | None = None,
    ) -> dict[str, Any]:
        skill_binding = api["resolve_production_skill_binding"](
            repo_root=self.settings.runtime_root,
            role_cwd=run_root,
        )
        document_catalog_projection, trusted_catalog = self._document_catalog_for_planner(
            run_root,
            data_room,
            allowed_roots=(Path(self.settings.state_root),),
        )
        if trusted_catalog is not None and parent_context is not None:
            from auto_foundry_core.document_ingestion import revision_qualify_catalog

            trusted_catalog, _child_ref_map = revision_qualify_catalog(
                getattr(parent_context, "document_catalog", None),
                trusted_catalog,
            )
            document_catalog_projection = trusted_catalog.planner_payload(
                max_excerpt_bytes=MAX_PLANNER_DOCUMENT_EXCERPT_BYTES,
                max_excerpts=MAX_PLANNER_DOCUMENT_EXCERPTS,
            )
        # Admissible document references come only from the catalog generated
        # from the exact immutable archive bytes.  A draft/source declaration
        # or a Planner response cannot introduce a new reference.
        document_refs = tuple(
            document.document_ref for document in (trusted_catalog.documents if trusted_catalog is not None else ())
        )
        trusted_catalog_payload = (
            trusted_catalog.to_dict() if trusted_catalog is not None else dict(document_catalog_projection)
        )
        role_models, role_reasoning_efforts = self._production_role_bindings(api)
        intake_route = {
            "model": role_models["intake_planner"],
            "reasoning_effort": role_reasoning_efforts["intake_planner"],
        }
        callback = getattr(self.intake_planner, "plan_intake", self.intake_planner)
        if not callable(callback):
            raise LaunchConflictError("Requirement Planner transport is unavailable")
        callback_kwargs = {
            "intake_blocks": self._intake_blocks(draft),
            "existing_plan": parent_plan.to_dict() if parent_plan is not None else None,
            "data_room": data_room,
            "document_refs": document_refs,
            "document_catalog": document_catalog_projection,
            "role_cwd": run_root,
            "skill_binding": skill_binding,
            "response_schema": INTAKE_RESPONSE_SCHEMA,
            "role_route": intake_route,
        }
        try:
            interpretation = callback(**callback_kwargs)
        except LaunchConflictError:
            raise
        except Exception as exc:
            raise LaunchConflictError("Requirement Planner transport failed") from exc
        if not isinstance(interpretation, Mapping):
            raise LaunchConflictError("Requirement Planner response must be an object")
        interpretation = dict(interpretation)
        try:
            return self._materialize_intake_plan(
                api,
                draft,
                interpretation,
                parent_plan=parent_plan,
                document_refs=document_refs,
                document_catalog=trusted_catalog_payload,
            )
        except IntakeRepresentationError as first_error:
            # Representation repair is deliberately one-shot and validator
            # informed.  A transport, trusted-source, or semantic failure is
            # never retried; the type carries that policy without brittle
            # matching on human-readable error strings.
            repair_kwargs = dict(callback_kwargs)
            repair_kwargs["repair_context"] = {
                "kind": "representation_repair",
                "validation_error": str(first_error)[:512],
                "response_schema": INTAKE_RESPONSE_SCHEMA,
            }
            try:
                repaired = callback(**repair_kwargs)
            except LaunchConflictError:
                raise
            except Exception as exc:
                raise LaunchConflictError("Requirement Planner representation repair transport failed") from exc
            if not isinstance(repaired, Mapping):
                raise LaunchConflictError("Requirement Planner representation repair must return an object")
            try:
                return self._materialize_intake_plan(
                    api,
                    draft,
                    dict(repaired),
                    parent_plan=parent_plan,
                    document_refs=document_refs,
                    document_catalog=trusted_catalog_payload,
                )
            except LaunchConflictError as repair_error:
                raise LaunchConflictError(
                    f"Requirement Planner representation repair failed: {str(repair_error)[:240]}"
                ) from repair_error

    def _prepare_coordinator(
        self,
        bootstrap: Mapping[str, Any],
        run_root: Path,
        *,
        publisher: Callable[[Any], Any] | None = None,
    ) -> dict[str, Any]:
        """Materialize or quiescently publish/rebind the public coordinator."""

        api = self._core_imports()
        publication_status: Any | None = None
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
        target_planner_hash = bootstrap.get("coordinatorPlannerHash")
        if target_planner_hash is None:
            target_planner_hash = sha256_file(plan_path)
        spec = self._desired_coordinator_spec(
            api=api,
            context=context,
            plan=plan,
            target_generation_id=target_generation_id,
            target_planner_hash=target_planner_hash,
            run_root=run_root,
        )
        skill_binding = {
            field_name: spec.codex_exec[field_name]
            for field_name in ("skill_path", "skill_version", "core_version", "skill_sha256")
            if field_name in spec.codex_exec
        }
        control = run_root / "control_plane"
        spec_path = control / "coordinator_spec.json"
        if spec_path.is_symlink():
            raise LaunchConflictError("Coordinator spec cannot be a symlink")
        coordinator: Any | None = None
        if spec_path.is_file():
            # Existing current specs may predate the exact production skill
            # binding.  Inspect their JSON without constructing a role
            # adapter, upgrade the same lineage quiescently, and only then
            # load the persisted coordinator normally.  Legacy G5 wrappers
            # are intentionally left to the public import/reopen path.
            needs_binding_upgrade = False
            persisted_spec = None
            raw_spec = load_object(spec_path)
            state_path = control / "coordinator_state.json"
            pending_plan_rebind = False
            if state_path.is_file() and not state_path.is_symlink():
                state_value = load_object(state_path)
                pending_plan_rebind = isinstance(state_value.get("pending_plan_rebind"), Mapping)
            if "run_spec" not in raw_spec and "lineage_binding" not in raw_spec:
                self._canonical_publication_policy(raw_spec.get("publication_policy"))
                persisted_codex = raw_spec.get("codex_exec")
                binding_fields = ("skill_path", "skill_version", "core_version", "skill_sha256")
                if not isinstance(persisted_codex, Mapping):
                    raise LaunchConflictError("Coordinator specification is invalid")
                bound_fields = {field for field in binding_fields if persisted_codex.get(field) is not None}
                if bound_fields and bound_fields != set(binding_fields):
                    raise LaunchConflictError("Coordinator Codex binding is incomplete and cannot be upgraded")
                if bound_fields == set(binding_fields) and dict(persisted_codex) != dict(spec.codex_exec):
                    # A complete binding can refer to a previous release whose
                    # bytes were rotated in place.  Project only the current
                    # Codex transport into the persisted outer spec.  Keep the
                    # public transaction choice here: same-lineage rotation is
                    # a transport-only rebind; a changed Planner lineage must
                    # go through the plan transaction directly.  The latter
                    # must not reconstruct the raw persisted adapter before
                    # ``publish_and_rebind`` has safely replaced its bytes.
                    projected_raw = dict(raw_spec)
                    projected_raw["codex_exec"] = spec.codex_exec
                    try:
                        projected_spec = api["CoordinatorRunSpec"].from_dict(projected_raw)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise LaunchConflictError("Coordinator specification is invalid") from exc
                    coordinator = api["RunCoordinator"](context)
                    same_lineage = all(
                        getattr(projected_spec, field_name) == getattr(spec, field_name)
                        for field_name in ("run_id", "generation_id", "planner_ref", "planner_hash")
                    ) and not pending_plan_rebind
                    if same_lineage:
                        coordinator.rebind_transport(projected_spec)
                    else:
                        publication = coordinator.publish_and_rebind(
                            spec,
                            publisher if publisher is not None else (lambda _target: None),
                        )
                        publication_status = publication
                else:
                    try:
                        persisted_spec = api["CoordinatorRunSpec"].from_dict(raw_spec)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise LaunchConflictError("Coordinator specification is invalid") from exc
                    self._canonical_publication_policy(persisted_spec.publication_policy)
                    needs_binding_upgrade = bound_fields != set(binding_fields)
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
                # Binding-only migration is complete, but the surrounding
                # launch path still needs to reload the canonical coordinator
                # and run its normal publication/resume branch below.
                api["RunCoordinator"](context).upgrade_and_rebind(upgrade_spec)
                coordinator = None
            if coordinator is None:
                coordinator = api["RunCoordinator"].from_persisted_spec(context)
                current = coordinator.status()
                if current.phase == "legacy_import_required":
                    coordinator.reopen("control center imported legacy coordinator state")
                if publisher is None:
                    # Resume/new-launch retries must bind the exact desired
                    # Supervisor transport before the child is spawned.  A
                    # same-lineage Codex change uses the narrow public
                    # transport transaction; a legacy import with a changed
                    # Planner lineage uses the public no-op publication
                    # boundary.
                    persisted = coordinator.persisted_spec()
                    if persisted.to_dict() == spec.to_dict():
                        coordinator.start(spec)
                    else:
                        same_lineage = all(
                            getattr(persisted, field_name) == getattr(spec, field_name)
                            for field_name in ("run_id", "generation_id", "planner_ref", "planner_hash")
                        )
                        if same_lineage:
                            coordinator.rebind_transport(spec)
                        else:
                            coordinator.publish_and_rebind(spec, lambda _target: None)
                else:
                    publication = coordinator.publish_and_rebind(spec, publisher)
                    publication_status = publication
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
            "publicationStatus": publication_status,
        }

    def _desired_coordinator_spec(
        self,
        *,
        api: Mapping[str, Any],
        context: Any,
        plan: Any,
        target_generation_id: Any,
        target_planner_hash: Any,
        run_root: Path,
    ) -> Any:
        """Build the current desired CoordinatorRunSpec without persisting it.

        Resume preparation and read-only Product-regeneration status must use
        exactly the same installed skill/role routing binding.  Keeping this
        construction in one side-effect-free helper prevents status polling
        from loading a persisted adapter bound to an older release while also
        ensuring POST rebinds to the exact spec whose hash the UI preview
        exposed.
        """

        if not isinstance(target_generation_id, str) or not target_generation_id:
            raise LaunchConflictError("Coordinator target generation is invalid")
        if not isinstance(target_planner_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", target_planner_hash):
            raise LaunchConflictError("Coordinator target Planner hash is invalid")
        skill_binding = api["resolve_production_skill_binding"](
            repo_root=self.settings.runtime_root,
            role_cwd=run_root,
        )
        role_models, role_reasoning_efforts = self._production_role_bindings(api)
        publication_policy = self._canonical_publication_policy({"enabled": False})
        return api["CoordinatorRunSpec"](
            run_id=context.run_id,
            generation_id=target_generation_id,
            planner_ref=plan.planner_ref,
            planner_hash=target_planner_hash,
            publication_policy=publication_policy,
            codex_exec={
                "binary": self.settings.codex_bin,
                "sandbox": "workspace-write",
                "ephemeral": True,
                # Persist every canonical current dispatch route, including
                # intake_planner, so child readiness can reject missing or
                # ambient model selection before any work starts.
                "role_models": role_models,
                "role_reasoning_efforts": role_reasoning_efforts,
                **skill_binding,
            },
        )

    def preview_resume_coordinator(self, run_id: str, run_root: Path) -> dict[str, Any]:
        """Return the desired post-rebind Coordinator spec without writing.

        ``RunControlManager.status`` uses this when the persisted coordinator
        is still bound to a prior production skill release.  The helper only
        reads the active plan/generation and validates the installed binding;
        it never calls the mutating preparation transaction, starts a child,
        or constructs an adapter from the stale persisted spec.
        """

        api = self._core_imports()
        context = api["RunContext"](safe_component(run_id, "run_id"), Path(run_root))
        validator = api["RunCoordinator"](context)
        try:
            validate_preview_evidence = getattr(validator, "validate_read_only_resume_evidence", None)
            if not callable(validate_preview_evidence):
                raise LaunchConflictError("Coordinator recovery preview validator is unavailable")
            # A read-only status/preview must not project through a valid
            # half-complete transport transaction.  POST preparation remains
            # the sole recovery boundary for pending/orphan rebind evidence.
            validate_preview_evidence(reject_transport_rebind=True)
        finally:
            validator.close(wait_for_roles=False)
        plan_path = api["RunLifecycle"].active_plan_path(context)
        if plan_path.is_symlink() or not plan_path.is_file():
            raise LaunchConflictError("Coordinator Planner plan is unavailable for resume")
        try:
            plan = api["RequirementSupervisorWorkspace"](context).load()
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Coordinator Planner plan is invalid for resume") from exc
        generation_id = api["RunLifecycle"].active_generation_id(context)
        target_planner_hash = sha256_file(plan_path)
        spec = self._desired_coordinator_spec(
            api=api,
            context=context,
            plan=plan,
            target_generation_id=generation_id,
            target_planner_hash=target_planner_hash,
            run_root=Path(run_root),
        )
        spec_path = context.resolve_run_path("control_plane/coordinator_spec.json")
        if spec_path.is_symlink() or not spec_path.is_file():
            raise LaunchConflictError("Coordinator persisted specification is unavailable for preview")
        raw_spec = load_object(spec_path)
        if "run_spec" in raw_spec or "lineage_binding" in raw_spec:
            raise LaunchConflictError("Legacy coordinator specification cannot be previewed")
        persisted_codex = raw_spec.get("codex_exec")
        binding_fields = ("skill_path", "skill_version", "core_version", "skill_sha256")
        if not isinstance(persisted_codex, Mapping):
            raise LaunchConflictError("Coordinator persisted Codex binding is unavailable for preview")
        bound_fields = {field_name for field_name in binding_fields if persisted_codex.get(field_name) is not None}
        if bound_fields != set(binding_fields):
            raise LaunchConflictError("Coordinator persisted Codex binding is incomplete for preview")
        desired_binding = {
            field_name: spec.codex_exec.get(field_name)
            for field_name in binding_fields
        }
        persisted_binding = {
            field_name: persisted_codex.get(field_name)
            for field_name in binding_fields
        }
        if persisted_binding == desired_binding:
            # The caller may only use this read-only path for the precise
            # failure where the persisted coordinator is tied to a different
            # installed production release.  A malformed or otherwise
            # invalid persisted coordinator must remain fail-closed.
            raise LaunchConflictError("Coordinator persisted binding is not a stale production release")
        return {
            "operation": "preview",
            "context": context,
            "spec": spec,
            "specHash": sha256_bytes(canonical_bytes(spec.to_dict())),
            "specPath": spec_path,
            "persistedBindingStale": True,
        }

    def prepare_resume_coordinator(self, run_id: str, run_root: Path) -> dict[str, Any]:
        """Quiescently validate/rebind one persisted coordinator before resume."""

        api = self._core_imports()
        context = api["RunContext"](safe_component(run_id, "run_id"), Path(run_root))
        plan_path = api["RunLifecycle"].active_plan_path(context)
        if plan_path.is_symlink() or not plan_path.is_file():
            raise LaunchConflictError("Coordinator Planner plan is unavailable for resume")
        try:
            plan = api["RequirementSupervisorWorkspace"](context).load()
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Coordinator Planner plan is invalid for resume") from exc
        generation_id = api["RunLifecycle"].active_generation_id(context)
        return self._prepare_coordinator(
            {
                "context": context,
                "plan": plan.to_dict(),
                "coordinatorGenerationId": generation_id,
                "coordinatorPlannerHash": sha256_file(plan_path),
            },
            Path(run_root),
        )

    def _bootstrap_new(self, draft: Mapping[str, Any], zip_path: Path, run_root: Path) -> dict[str, Any]:
        api = self._core_imports()
        run_id = safe_component(draft.get("runId"), "run_id")
        run_root.mkdir(parents=True, exist_ok=False)
        inputs_root = run_root / "inputs"
        inputs_root.mkdir(parents=True, exist_ok=False)
        final_zip = inputs_root / "data_room.zip"
        shutil.copyfile(zip_path, final_zip)
        context = api["RunContext"](run_id, run_root, input_roots=(inputs_root,))
        try:
            revision = api["DataRevisionStore"](context).initialize_legacy(final_zip)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Initial data-room revision could not be initialized") from exc
        planned = self._plan_intake(
            api,
            draft,
            run_root,
            data_room="inputs/data_room.zip",
        )
        records = planned["records"]
        plan = planned["plan"]
        mission_context = planned.get("missionContext")
        mission_artifacts = None
        if mission_context is not None:
            mission_artifacts = self._write_mission_artifacts(
                run_root,
                draft,
                mission_context=mission_context,
                requirement_ids=(record.requirement_id for record in records),
                portfolio_strategy=plan.portfolio_strategy,
            )
            # Keep one run-control pointer for operators that inspect the
            # canonical namespace directly; the launch-scoped artifacts remain
            # the immutable hash-bound records referenced by the manifest.
            artifact_root = run_root / "control_center" / "launches" / safe_component(draft.get("draftId"), "draftId")
            for filename in ("mission_context.json", "mission_plan.json", "document_catalog.json"):
                candidate = artifact_root / filename
                if candidate.is_file() and not candidate.is_symlink():
                    self._write_control_once(run_root, filename, load_object(candidate))
        mission_plan = mission_artifacts.get("missionPlan") if mission_artifacts else None
        mission_context_ref = mission_artifacts.get("missionContextRef") if mission_artifacts else None
        mission_context_hash = mission_artifacts.get("missionContextHash") if mission_artifacts else None
        mission_plan_ref = mission_artifacts.get("missionPlanRef") if mission_artifacts else None
        mission_plan_hash = mission_artifacts.get("missionPlanHash") if mission_artifacts else None
        document_catalog_ref = mission_artifacts.get("documentCatalogRef") if mission_artifacts else None
        document_catalog_hash = mission_artifacts.get("documentCatalogHash") if mission_artifacts else None
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
                "missionContextRef": mission_context_ref,
                "missionContextHash": mission_context_hash,
                "missionPlanRef": mission_plan_ref if mission_plan is not None else None,
                "missionPlanHash": mission_plan_hash,
                "missionContext": mission_context.to_dict() if mission_context is not None else None,
                "documentCatalogRef": document_catalog_ref,
                "documentCatalogHash": document_catalog_hash,
            },
        )
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
            "dataRoomSha256": revision.archive_sha256,
            "dataRoomSize": revision.archive_size_bytes,
            "inputRoots": [str(value) for value in (inputs_root,)],
            "dataRevision": {
                "revisionId": revision.revision_id,
                "manifestHash": revision.manifest_hash,
                "archiveSha256": revision.archive_sha256,
                "archiveSizeBytes": revision.archive_size_bytes,
            },
            "dataRevisionMetadata": revision.to_dict(),
            "intakePlan": f"control_center/launches/{draft['draftId']}/intake_plan.json",
            "missionContextRef": mission_context_ref,
            "missionContextHash": mission_context_hash,
            "missionPlanRef": mission_plan_ref,
            "missionPlanHash": mission_plan_hash,
            "documentCatalogRef": document_catalog_ref,
            "documentCatalogHash": document_catalog_hash,
            "activeMissionPointer": mission_artifacts.get("activePointer") if mission_artifacts else None,
        }

    def _continuation_intent_path(self, run_root: Path, draft_id: str) -> Path:
        draft_component = safe_component(draft_id, "draftId")
        path = run_root / "control_center" / "launches" / draft_component / "continuation_intent.json"
        reject_symlink_components(path, run_root)
        return path

    def _pending_refresh_path(self, run_root: Path, draft_id: str) -> Path:
        path = run_root / "control_center" / "launches" / safe_component(draft_id, "draftId") / "pending_data_refresh.json"
        reject_symlink_components(path, run_root)
        return path

    @staticmethod
    def _active_mission_pointer_path(run_root: Path) -> Path:
        path = run_root / "control_center" / "mission_context_active.json"
        reject_symlink_components(path, run_root)
        return path

    @staticmethod
    def _load_active_mission_pointer(run_root: Path) -> dict[str, Any] | None:
        """Load and verify the authoritative cumulative-context pointer.

        A present pointer is strict: any path, hash, or run-lineage mismatch is
        a launch conflict rather than a fallback to an arbitrary historical
        UUID directory.
        """

        from auto_foundry_core.document_ingestion import DocumentCatalog
        from auto_foundry_core.mission_context import (
            MissionContext,
            MissionPlan,
            sha256_value,
            validate_mission_context_catalog,
        )

        path = LaunchManager._active_mission_pointer_path(run_root)
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            raise LaunchConflictError("Active mission-context pointer is unavailable")
        try:
            pointer = load_object(path)
        except (OSError, ValueError) as exc:
            raise LaunchConflictError("Active mission-context pointer is unreadable") from exc
        if not isinstance(pointer, Mapping):
            raise LaunchConflictError("Active mission-context pointer is invalid")
        if pointer.get("schemaVersion") != 1 or pointer.get("kind") != "active_mission_context_pointer":
            raise LaunchConflictError("Active mission-context pointer schema is invalid")
        if pointer.get("runRoot") != str(run_root) or pointer.get("runId") != run_root.name:
            raise LaunchConflictError("Active mission-context pointer run lineage changed")

        def resolve_ref(value: Any, label: str) -> tuple[str, Path]:
            if not isinstance(value, str) or not value or Path(value).is_absolute():
                raise LaunchConflictError(f"Active mission-context {label} is invalid")
            target = run_root / value
            if not is_within(target, (run_root,)):
                raise LaunchConflictError(f"Active mission-context {label} escapes the run root")
            try:
                reject_symlink_components(target, run_root)
            except ValueError as exc:
                raise LaunchConflictError(f"Active mission-context {label} is unsafe") from exc
            return value, target

        context_ref, context_path = resolve_ref(pointer.get("missionContextRef"), "context reference")
        expected_draft_id = context_path.parent.name
        if pointer.get("draftId") != expected_draft_id:
            raise LaunchConflictError("Active mission-context pointer draft lineage changed")
        context_hash = pointer.get("missionContextHash")
        if not isinstance(context_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", context_hash):
            raise LaunchConflictError("Active mission-context hash is invalid")
        try:
            context_value = load_object(context_path)
            context = MissionContext.from_dict(context_value.get("context"))
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Active mission-context sidecar is invalid") from exc
        if (
            context_value.get("draftId") != expected_draft_id
            or context_value.get("contextHash") != context.context_hash
            or context_hash.lower() != context.context_hash
        ):
            raise LaunchConflictError("Active mission-context sidecar hash does not match its pointer")

        plan_ref, plan_path = resolve_ref(pointer.get("missionPlanRef"), "plan reference")
        plan_hash = pointer.get("missionPlanHash")
        if not isinstance(plan_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", plan_hash):
            raise LaunchConflictError("Active mission-plan hash is invalid")
        try:
            plan_value = load_object(plan_path)
            mission_plan = MissionPlan.from_dict(plan_value)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Active mission-plan sidecar is invalid") from exc
        if (
            plan_value.get("draftId") != expected_draft_id
            or
            plan_value.get("contextHash") != context.context_hash
            or plan_value.get("planHash") != mission_plan.plan_hash
            or plan_hash.lower() != mission_plan.plan_hash
            or mission_plan.context_hash != context.context_hash
        ):
            raise LaunchConflictError("Active mission-plan sidecar hash does not match its pointer")

        catalog_ref = pointer.get("documentCatalogRef")
        catalog_hash = pointer.get("documentCatalogHash")
        if catalog_ref is not None or catalog_hash is not None:
            catalog_ref, catalog_path = resolve_ref(catalog_ref, "catalog reference")
            if not isinstance(catalog_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", catalog_hash):
                raise LaunchConflictError("Active document-catalog hash is invalid")
            try:
                catalog_value = load_object(catalog_path)
                catalog = DocumentCatalog.from_dict(catalog_value.get("catalog"))
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Active document catalog sidecar is invalid") from exc
            catalog_digest = sha256_value(catalog.to_dict())
            if (
                catalog_value.get("draftId") != expected_draft_id
                or
                catalog_value.get("catalogHash") != catalog_digest
                or catalog_hash.lower() != catalog_digest
                or context.document_catalog is None
                or sha256_value(context.document_catalog) != catalog_digest
            ):
                raise LaunchConflictError("Active document-catalog hash does not match its pointer")
            try:
                validate_mission_context_catalog(context, catalog.to_dict())
            except (TypeError, ValueError) as exc:
                raise LaunchConflictError("Active mission-context document provenance is invalid") from exc
        elif context.document_catalog is not None:
            raise LaunchConflictError("Active document catalog pointer is missing")
        return {
            **dict(pointer),
            "missionContextRef": context_ref,
            "missionContextHash": context.context_hash,
            "missionPlanRef": plan_ref,
            "missionPlanHash": mission_plan.plan_hash,
            "documentCatalogRef": catalog_ref,
            "documentCatalogHash": catalog_hash,
            "missionContext": context,
            "missionPlan": mission_plan,
        }

    @staticmethod
    def _load_existing_mission_context(run_root: Path) -> tuple[Any | None, str | None, str | None]:
        """Read the active cumulative mission sidecar for a retry."""

        from auto_foundry_core.mission_context import MissionContext

        pointer = LaunchManager._load_active_mission_pointer(run_root)
        if pointer is not None:
            return pointer["missionContext"], pointer["missionContextRef"], pointer["missionContextHash"]
        # Legacy runs may have the immutable first-context compatibility copy;
        # do not search arbitrary UUID launch directories.
        path = run_root / "control_center" / "mission_context.json"
        if path.is_symlink() or not path.is_file() or not is_within(path, (run_root,)):
            return None, None, None
        try:
            value = load_object(path)
            context = MissionContext.from_dict(value.get("context"))
            if value.get("contextHash") != context.context_hash:
                raise ValueError("legacy mission-context hash mismatch")
            return context, path.relative_to(run_root).as_posix(), context.context_hash
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Mission-context compatibility pointer is invalid") from exc

    @staticmethod
    def _load_mission_context_by_hash(run_root: Path, expected_hash: str) -> Any | None:
        """Find one immutable historical context by its declared hash."""

        from auto_foundry_core.mission_context import MissionContext

        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
            return None
        launches_root = run_root / "control_center" / "launches"
        if launches_root.is_symlink() or not launches_root.is_dir():
            return None
        try:
            candidates = sorted(launches_root.iterdir(), key=lambda path: path.name)
        except OSError:
            return None
        for child in candidates:
            if child.is_symlink() or not child.is_dir():
                continue
            path = child / "mission_context.json"
            if path.is_symlink() or not path.is_file() or not is_within(path, (run_root,)):
                continue
            try:
                value = load_object(path)
                context = MissionContext.from_dict(value.get("context"))
                if value.get("contextHash") == context.context_hash and context.context_hash == expected_hash.lower():
                    return context
            except (OSError, KeyError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _write_active_mission_pointer(run_root: Path, pointer: Mapping[str, Any]) -> None:
        path = LaunchManager._active_mission_pointer_path(run_root)
        if path.exists() or path.is_symlink():
            # Validate the old pointer before replacing it.  This prevents a
            # tampered pointer from being silently healed by a retry.
            LaunchManager._load_active_mission_pointer(run_root)
        atomic_write_json(path, dict(pointer))

    def _write_mission_artifacts(
        self,
        run_root: Path,
        draft: Mapping[str, Any],
        *,
        mission_context: Any,
        requirement_ids: Iterable[str],
        portfolio_strategy: str,
    ) -> dict[str, Any]:
        """Stage immutable MissionContext/MissionPlan sidecars for a launch.

        This helper deliberately does *not* advance the active pointer.  The
        sidecars may be written while a continuation is still only staged (or
        while the initial launch has not yet reached its admission boundary).
        Call :meth:`_promote_staged_mission_artifacts` only after the enclosing
        launch/data-refresh transaction is durable.
        """

        from auto_foundry_core.mission_context import MissionContext, MissionPlan, sha256_value

        if not isinstance(mission_context, MissionContext):
            raise LaunchConflictError("MissionContext is unavailable")
        mission_plan = MissionPlan(
            mission_context=mission_context,
            requirement_ids=tuple(str(value) for value in requirement_ids),
            portfolio_strategy=portfolio_strategy,
            planner_ref="semantic-intake-planner",
        )
        draft_id = safe_component(draft.get("draftId"), "draftId")
        context_ref = f"control_center/launches/{draft_id}/mission_context.json"
        plan_ref = f"control_center/launches/{draft_id}/mission_plan.json"
        catalog_ref = None
        catalog_hash = None
        self._write_launch_artifact(
            run_root,
            draft_id,
            "mission_context.json",
            {
                "schemaVersion": 1,
                "kind": "mission_context",
                "draftId": draft_id,
                "fingerprint": draft.get("fingerprint"),
                "contextHash": mission_context.context_hash,
                "context": mission_context.to_dict(),
            },
        )
        self._write_launch_artifact(
            run_root,
            draft_id,
            "mission_plan.json",
            {
                "schemaVersion": 1,
                "kind": "mission_plan",
                "draftId": draft_id,
                "fingerprint": draft.get("fingerprint"),
                "contextHash": mission_plan.context_hash,
                "planHash": mission_plan.plan_hash,
                "missionPlan": mission_plan.to_dict(),
            },
        )
        if isinstance(mission_context.document_catalog, Mapping):
            catalog_ref = f"control_center/launches/{draft_id}/document_catalog.json"
            catalog_hash = sha256_value(mission_context.document_catalog)
            self._write_launch_artifact(
                run_root,
                draft_id,
                "document_catalog.json",
                {
                    "schemaVersion": 1,
                    "kind": "mission_document_catalog",
                    "draftId": draft_id,
                    "fingerprint": draft.get("fingerprint"),
                    "catalogHash": catalog_hash,
                    "catalog": dict(mission_context.document_catalog),
                },
            )
        pointer = {
            "schemaVersion": 1,
            "kind": "active_mission_context_pointer",
            "runId": draft.get("runId"),
            "runRoot": str(run_root),
            "draftId": draft_id,
            "missionContextRef": context_ref,
            "missionContextHash": mission_context.context_hash,
            "missionPlanRef": plan_ref,
            "missionPlanHash": mission_plan.plan_hash,
            "documentCatalogRef": catalog_ref,
            "documentCatalogHash": catalog_hash,
        }
        return {
            "missionContextRef": context_ref,
            "missionContextHash": mission_context.context_hash,
            "missionPlanRef": plan_ref,
            "missionPlanHash": mission_plan.plan_hash,
            "documentCatalogRef": catalog_ref,
            "documentCatalogHash": catalog_hash,
            "missionContext": mission_context,
            "missionPlan": mission_plan,
            "activePointer": pointer,
        }

    @staticmethod
    def _promote_staged_mission_artifacts(run_root: Path, artifacts: Mapping[str, Any]) -> None:
        """Atomically publish a previously staged mission-context pointer."""

        pointer = artifacts.get("activePointer")
        if not isinstance(pointer, Mapping):
            raise LaunchConflictError("Staged mission-context pointer is unavailable")
        path = LaunchManager._active_mission_pointer_path(run_root)
        previous_bytes: bytes | None = None
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise LaunchConflictError("Active mission-context pointer is unavailable")
            try:
                previous_bytes = path.read_bytes()
                existing = LaunchManager._load_active_mission_pointer(run_root)
            except (OSError, LaunchConflictError) as exc:
                raise LaunchConflictError("Existing active mission-context pointer is invalid") from exc
            if existing is not None and all(existing.get(key) == pointer.get(key) for key in (
                "runId",
                "runRoot",
                "draftId",
                "missionContextRef",
                "missionContextHash",
                "missionPlanRef",
                "missionPlanHash",
                "documentCatalogRef",
                "documentCatalogHash",
            )):
                return
        LaunchManager._write_active_mission_pointer(run_root, pointer)
        try:
            # Re-read the target sidecars before exposing the new pointer.  If
            # an injected write/sidecar failure is observed, restore the exact
            # prior bytes so retry still selects the previous parent.
            LaunchManager._load_active_mission_pointer(run_root)
        except Exception:
            try:
                if previous_bytes is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(path, previous_bytes)
            except Exception:
                pass
            raise

    @staticmethod
    def _staged_pointer_from_intent(
        run_root: Path,
        intent: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Resolve the staged pointer carried by a durable continuation intent."""

        if not isinstance(intent, Mapping):
            return None
        carried = intent.get("activeMissionPointer")
        if isinstance(carried, Mapping):
            return dict(carried)
        required = (
            "missionContextRef",
            "missionContextHash",
            "missionPlanRef",
            "missionPlanHash",
        )
        if not all(intent.get(key) is not None for key in required):
            return None
        return {
            "schemaVersion": 1,
            "kind": "active_mission_context_pointer",
            "runId": intent.get("runId") or run_root.name,
            "runRoot": intent.get("runRoot") or str(run_root),
            "draftId": intent.get("draftId"),
            "missionContextRef": intent.get("missionContextRef"),
            "missionContextHash": intent.get("missionContextHash"),
            "missionPlanRef": intent.get("missionPlanRef"),
            "missionPlanHash": intent.get("missionPlanHash"),
            "documentCatalogRef": intent.get("documentCatalogRef"),
            "documentCatalogHash": intent.get("documentCatalogHash"),
        }

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
        revision = intent.get("dataRevision")
        if not isinstance(revision, Mapping):
            raise LaunchConflictError("Continuation intent data-revision binding is unavailable")
        manifest = {
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
            "dataRevisionId": revision.get("revision_id"),
            "dataRevisionRef": intent.get("dataRevisionRef", data_room),
            "dataRevisionManifestHash": revision.get("manifest_hash"),
            "dataRevisionArchiveSha256": revision.get("archive_sha256"),
            "reopenedItemIds": list(intent.get("reopenedItemIds") or []),
            "pendingDataRefresh": False,
        }
        # Context sidecars were introduced after the original continuation
        # protocol.  Preserve old intents exactly while binding new intents
        # by the immutable MissionContext hash and run-local reference.
        for key in (
            "missionContextRef",
            "missionContextHash",
            "missionPlanRef",
            "missionPlanHash",
            "documentCatalogRef",
            "documentCatalogHash",
            "parentMissionContextHash",
        ):
            if intent.get(key) is not None:
                manifest[key] = intent.get(key)
        return manifest

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
        revision = intent.get("dataRevision")
        if not isinstance(revision, Mapping):
            raise LaunchConflictError("Continuation intent data-revision binding is unavailable")
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
            "dataRevisionId": intent.get("dataRevisionId") or revision.get("revision_id"),
            "dataRevisionRef": intent.get("dataRevisionRef", intent.get("dataRoom")),
            "dataRevisionManifestHash": intent.get("dataRevisionManifestHash") or revision.get("manifest_hash"),
            "dataRevisionArchiveSha256": intent.get("dataRevisionArchiveSha256") or revision.get("archive_sha256"),
            "reopenedItemIds": list(intent.get("reopenedItemIds") or []),
            "pendingDataRefresh": False,
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
            for filename in ("launch_manifest.json", "launch_receipt.json", "pending_data_refresh.json")
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
        planning_data_room: str | None = None,
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
        planning_parent_plan = parent_plan
        mission_context = None
        mission_context_ref = None
        mission_context_hash = None
        mission_plan_ref = None
        mission_plan_hash = None
        document_catalog_ref = None
        document_catalog_hash = None
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
            data_only = bool(intent.get("dataOnly")) or not draft.get("intakeBlocks")
            if (
                not isinstance(raw_records, list)
                or not isinstance(raw_plan, Mapping)
                or not isinstance(raw_added_ids, list)
                or not generation_id
            ):
                raise LaunchConflictError("Continuation intent is incomplete")
            if data_only:
                if raw_records or raw_added_ids:
                    raise LaunchConflictError("Data-only continuation intent contains added requirements")
                records = ()
                plan = parent_plan
                raw_interpretation = raw_interpretation if isinstance(raw_interpretation, Mapping) else {
                    "schemaVersion": 1,
                    "portfolioStrategy": parent_plan.portfolio_strategy,
                    "requirements": [],
                    "groups": [],
                    "unassignedContext": [],
                }
                mission_context, mission_context_ref, mission_context_hash = self._load_existing_mission_context(run_root)
                pointer = self._load_active_mission_pointer(run_root)
                mission_plan_ref = pointer.get("missionPlanRef") if pointer else (mission_context_ref.replace("mission_context.json", "mission_plan.json") if mission_context_ref else None)
                mission_plan_hash = pointer.get("missionPlanHash") if pointer else mission_context_hash
                document_catalog_ref = pointer.get("documentCatalogRef") if pointer else (mission_context_ref.replace("mission_context.json", "document_catalog.json") if mission_context_ref else None)
                document_catalog_hash = pointer.get("documentCatalogHash") if pointer else None
            else:
                if not isinstance(raw_interpretation, Mapping):
                    raise LaunchConflictError("Continuation intent interpretation is unavailable")
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
                raw_mission_context = intent.get("missionContext")
                if isinstance(raw_mission_context, Mapping):
                    try:
                        from auto_foundry_core.mission_context import MissionContext

                        mission_context = MissionContext.from_dict(raw_mission_context)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise LaunchConflictError("Continuation intent mission context is invalid") from exc
                    mission_context_hash = mission_context.context_hash
                    mission_context_ref = intent.get("missionContextRef")
                    mission_plan_ref = intent.get("missionPlanRef")
                    mission_plan_hash = intent.get("missionPlanHash") or mission_context_hash
                    document_catalog_ref = intent.get("documentCatalogRef")
                    document_catalog_hash = intent.get("documentCatalogHash")
                    pointer = self._load_active_mission_pointer(run_root)
                    if pointer is not None and any(
                        intent.get(key) != pointer.get(key)
                        for key in (
                            "missionContextRef",
                            "missionContextHash",
                            "missionPlanRef",
                            "missionPlanHash",
                            "documentCatalogRef",
                            "documentCatalogHash",
                        )
                    ):
                        # A freshly persisted continuation intent intentionally
                        # points at staged child sidecars while the active
                        # pointer still names the prior admitted context.  It
                        # is admissible only when the intent records that
                        # exact parent hash and carries a matching staged
                        # pointer payload; otherwise a tampered intent must
                        # fail closed.
                        staged_pointer = intent.get("activeMissionPointer")
                        staged_parent = intent.get("parentMissionContextHash")
                        staged_ok = (
                            isinstance(staged_pointer, Mapping)
                            and all(
                                staged_pointer.get(key) == intent.get(key)
                                for key in (
                                    "missionContextRef",
                                    "missionContextHash",
                                    "missionPlanRef",
                                    "missionPlanHash",
                                    "documentCatalogRef",
                                    "documentCatalogHash",
                                )
                            )
                            and staged_parent == pointer.get("missionContextHash")
                        )
                        if not staged_ok:
                            raise LaunchConflictError("Continuation intent mission context is not bound to the active pointer")
                else:
                    mission_context, mission_context_ref, mission_context_hash = self._load_existing_mission_context(run_root)
                    pointer = self._load_active_mission_pointer(run_root)
                    mission_plan_ref = pointer.get("missionPlanRef") if pointer else (mission_context_ref.replace("mission_context.json", "mission_plan.json") if mission_context_ref else None)
                    mission_plan_hash = pointer.get("missionPlanHash") if pointer else mission_context_hash
                    document_catalog_ref = pointer.get("documentCatalogRef") if pointer else (mission_context_ref.replace("mission_context.json", "document_catalog.json") if mission_context_ref else None)
                    document_catalog_hash = pointer.get("documentCatalogHash") if pointer else None
            metadata = lifecycle.generation_metadata
            active_retry = bool(
                metadata is not None
                and parent_state_hash != str(intent.get("parentStateHash") or "")
                and parent_plan_hash != str(intent.get("parentPlanHash") or "")
                and metadata.generation_id == generation_id
                and tuple(metadata.added_item_ids) == tuple(record.requirement_id for record in records)
                and parent_plan.to_dict() == plan.to_dict()
            )
            if not active_retry and not data_only:
                semantic_parent_plan = parent_plan
                coalesced_pending_validation = False
                try:
                    canonical_pending = api["DataRevisionStore"](context).pending_data_refresh(allow_stale=True)
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    raise LaunchConflictError("Canonical data refresh admission is invalid") from exc
                if canonical_pending is not None:
                    pending_ids = {
                        record.get("requirement_id")
                        for record in (canonical_pending.plan.get("input_records") or ())
                        if isinstance(record, Mapping)
                    }
                    intent_added_ids = set(str(value) for value in raw_added_ids)
                    if (
                        canonical_pending.expected_parent_generation_id == lifecycle.generation_id
                        and canonical_pending.expected_parent_state_hash == parent_state_hash
                        and canonical_pending.expected_parent_plan_hash == parent_plan_hash
                        and bool(
                            pending_ids
                            - {record.requirement_id for record in parent_plan.input_records}
                            - intent_added_ids
                        )
                    ):
                        coalesced_pending_validation = True
                        try:
                            pending_payload = dict(canonical_pending.plan)
                            removed_ids = intent_added_ids & pending_ids
                            if removed_ids:
                                pending_payload["input_records"] = [
                                    record
                                    for record in (pending_payload.get("input_records") or ())
                                    if isinstance(record, Mapping)
                                    and record.get("requirement_id") not in removed_ids
                                ]
                                pending_payload["groups"] = [
                                    {
                                        **group,
                                        "requirement_ids": [
                                            item_id
                                            for item_id in (group.get("requirement_ids") or ())
                                            if item_id not in removed_ids
                                        ],
                                    }
                                    for group in (pending_payload.get("groups") or ())
                                    if isinstance(group, Mapping)
                                    and any(item_id not in removed_ids for item_id in (group.get("requirement_ids") or ()))
                                ]
                            semantic_parent_plan = api["RequirementExecutionPlan"].from_dict(pending_payload)
                        except (KeyError, TypeError, ValueError) as exc:
                            raise LaunchConflictError("Canonical data refresh plan is invalid") from exc
                trusted_projection, trusted_catalog = self._document_catalog_for_planner(
                    run_root,
                    existing_data_room["dataRoom"],
                    allowed_roots=(Path(self.settings.state_root),),
                )
                trusted_catalog_payload = (
                    trusted_catalog.to_dict() if trusted_catalog is not None else dict(trusted_projection)
                )
                if trusted_catalog is not None and mission_context is not None:
                    parent_context_hash_for_merge = mission_context.metadata.get("parent_context_hash")
                    if isinstance(parent_context_hash_for_merge, str):
                        parent_context_for_catalog = self._load_mission_context_by_hash(
                            run_root,
                            parent_context_hash_for_merge,
                        )
                        if parent_context_for_catalog is None:
                            raise LaunchConflictError("Continuation mission-context lineage is unavailable")
                        from auto_foundry_core.document_ingestion import revision_qualify_catalog

                        trusted_catalog, _child_ref_map = revision_qualify_catalog(
                            parent_context_for_catalog.document_catalog,
                            trusted_catalog,
                        )
                        trusted_projection = trusted_catalog.planner_payload(
                            max_excerpt_bytes=MAX_PLANNER_DOCUMENT_EXCERPT_BYTES,
                            max_excerpts=MAX_PLANNER_DOCUMENT_EXCERPTS,
                        )
                        trusted_catalog_payload = trusted_catalog.to_dict()
                trusted_document_refs = tuple(
                    document.document_ref for document in (trusted_catalog.documents if trusted_catalog is not None else ())
                )
                rebuilt = self._materialize_intake_plan(
                    api,
                    draft,
                    raw_interpretation,
                    parent_plan=semantic_parent_plan,
                    document_refs=trusted_document_refs,
                    document_catalog=trusted_catalog_payload,
                )
                rebuilt_context = rebuilt.get("missionContext")
                if rebuilt_context is not None and mission_context is not None:
                    parent_context_hash_for_merge = mission_context.metadata.get("parent_context_hash")
                    if isinstance(parent_context_hash_for_merge, str):
                        parent_context_for_merge = self._load_mission_context_by_hash(
                            run_root,
                            parent_context_hash_for_merge,
                        )
                        if parent_context_for_merge is None:
                            raise LaunchConflictError("Continuation mission-context lineage is unavailable")
                        from auto_foundry_core.mission_context import merge_mission_contexts

                        rebuilt["missionContext"] = merge_mission_contexts(
                            parent_context_for_merge,
                            rebuilt_context,
                        )
                if (
                    not coalesced_pending_validation
                    and (
                    [record.to_dict() for record in records]
                    != [record.to_dict() for record in rebuilt["records"]]
                    or plan.to_dict() != rebuilt["plan"].to_dict()
                    or mission_context is not None
                    and rebuilt.get("missionContext") is not None
                    and mission_context.context_hash != rebuilt["missionContext"].context_hash
                    )
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
            current_revision = existing_data_room.get("revision")
            bound_revision = intent.get("dataRevision")
            if not isinstance(bound_revision, Mapping) or current_revision is None:
                raise LaunchConflictError("Continuation intent data-revision binding is incomplete")
            if any(
                bound_revision.get(key) != getattr(current_revision, attr)
                for key, attr in (
                    ("revision_id", "revision_id"),
                    ("manifest_hash", "manifest_hash"),
                    ("archive_sha256", "archive_sha256"),
                )
            ):
                raise LaunchConflictError("Continuation intent data revision changed")
        else:
            # A concurrent D admission may already carry a cumulative plan
            # with newly added requirements while the active generation still
            # points at its old plan.  Use that immutable canonical plan as
            # semantic input so deterministic REQ numbering and groups do not
            # manufacture a false same-ID conflict during coalescing.  The
            # lifecycle plan/state hashes remain the admission CAS parent.
            try:
                pending = api["DataRevisionStore"](context).pending_data_refresh(allow_stale=True)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Canonical data refresh admission is invalid") from exc
            if pending is not None:
                try:
                    pending_plan = api["RequirementExecutionPlan"].from_dict(dict(pending.plan))
                except (KeyError, TypeError, ValueError) as exc:
                    raise LaunchConflictError("Canonical data refresh plan is invalid") from exc
                if (
                    pending.expected_parent_generation_id == lifecycle.generation_id
                    and pending.expected_parent_state_hash == parent_state_hash
                    and pending.expected_parent_plan_hash == parent_plan_hash
                ):
                    planning_parent_plan = pending_plan
            data_only = not draft.get("intakeBlocks")
            if data_only:
                records = ()
                plan = planning_parent_plan
                raw_interpretation = {
                    "schemaVersion": 1,
                    "portfolioStrategy": planning_parent_plan.portfolio_strategy,
                    "requirements": [],
                    "groups": [],
                    "unassignedContext": [],
                }
                mission_context, mission_context_ref, mission_context_hash = self._load_existing_mission_context(run_root)
                pointer = self._load_active_mission_pointer(run_root)
                mission_plan_ref = pointer.get("missionPlanRef") if pointer else (mission_context_ref.replace("mission_context.json", "mission_plan.json") if mission_context_ref else None)
                mission_plan_hash = pointer.get("missionPlanHash") if pointer else mission_context_hash
                document_catalog_ref = pointer.get("documentCatalogRef") if pointer else (mission_context_ref.replace("mission_context.json", "document_catalog.json") if mission_context_ref else None)
                document_catalog_hash = pointer.get("documentCatalogHash") if pointer else None
            else:
                prior_context, _prior_context_ref, _prior_context_hash = self._load_existing_mission_context(run_root)
                planned = self._plan_intake(
                    api,
                    draft,
                    run_root,
                    data_room=planning_data_room or existing_data_room["dataRoom"],
                    parent_plan=planning_parent_plan,
                    parent_context=prior_context,
                )
                records = planned["records"]
                plan = planned["plan"]
                raw_interpretation = planned["interpretation"]
                mission_context = planned.get("missionContext")
                if mission_context is not None:
                    if prior_context is not None:
                        from auto_foundry_core.mission_context import merge_mission_contexts

                        mission_context = merge_mission_contexts(prior_context, mission_context)
                    mission_context_hash = mission_context.context_hash
                    draft_component = safe_component(draft.get("draftId"), "draftId")
                    mission_context_ref = f"control_center/launches/{draft_component}/mission_context.json"
                    mission_plan_ref = f"control_center/launches/{draft_component}/mission_plan.json"
                    from auto_foundry_core.mission_context import MissionPlan

                    mission_plan_hash = MissionPlan(
                        mission_context=mission_context,
                        requirement_ids=tuple(record.requirement_id for record in plan.input_records),
                        portfolio_strategy=plan.portfolio_strategy,
                        planner_ref="semantic-intake-planner",
                    ).plan_hash
                    document_catalog_ref = f"control_center/launches/{draft_component}/document_catalog.json"
                    if isinstance(mission_context.document_catalog, Mapping):
                        from auto_foundry_core.mission_context import sha256_value

                        document_catalog_hash = sha256_value(mission_context.document_catalog)
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
            "missionContext": mission_context,
            "missionContextRef": mission_context_ref,
            "missionContextHash": mission_context_hash,
            "missionPlanRef": mission_plan_ref,
            "missionPlanHash": mission_plan_hash,
            "documentCatalogRef": document_catalog_ref,
            "documentCatalogHash": document_catalog_hash,
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

    def _ensure_continue_intent(
        self,
        draft: Mapping[str, Any],
        run_root: Path,
        staging_root: Path | None = None,
    ) -> dict[str, Any]:
        api = self._core_imports()
        if staging_root is None:
            staging_root = Path(self.settings.state_root) / "continuation-staging" / safe_component(
                draft.get("draftId"), "draftId"
            )
            staging_root.mkdir(parents=True, exist_ok=True)
        path = self._continuation_intent_path(run_root, str(draft.get("draftId")))
        if path.exists() or path.is_symlink():
            try:
                intent = load_object(path)
            except ValueError as exc:
                raise LaunchConflictError("Continuation intent is unreadable") from exc
            intent_handoff = None
            if isinstance(intent.get("plan"), Mapping):
                intent_handoff = {
                    "plan": intent.get("plan"),
                    "reopened_item_ids": tuple(intent.get("reopenedItemIds") or ()),
                    "expected_parent_generation_id": intent.get("generationId"),
                    "expected_parent_state_hash": intent.get("parentStateHash"),
                    "expected_parent_plan_hash": intent.get("parentPlanHash"),
                }
            data = self._ensure_data_revision(
                draft,
                run_root,
                staging_root,
                intent=intent,
                transaction_handoff=intent_handoff,
            )
            built = self._continue_plan(draft, run_root, intent=intent)
            refresh_required, _active_revision, _canonical_pending = self._continuation_refresh_required(built, data)
            if refresh_required:
                reopened = list(intent.get("reopenedItemIds") or ())
                if not reopened:
                    reopened = [record.requirement_id for record in built["parentPlan"].input_records]
                updated = dict(intent)
                updated["reopenedItemIds"] = reopened
                updated["refreshRequired"] = True
                pending = self._admit_continue_data_refresh(draft, built, updated)
                updated["pendingDataRefreshIntentHash"] = pending.intent_hash
                if updated != dict(intent):
                    self._write_launch_artifact(
                        run_root,
                        str(draft["draftId"]),
                        "continuation_intent.json",
                        updated,
                    )
                    intent = updated
            return intent
        # Snapshot and merge source bytes without publishing the D pointer so
        # document-aware intake planning sees the candidate archive while a
        # planner/materialisation failure leaves the authoritative pointer
        # untouched.  The candidate is committed only after this plan builds.
        staged = self._ensure_data_revision(draft, run_root, staging_root, publish=False)
        candidate_path = staged.get("candidatePath")
        mission_context = None
        mission_artifacts = None
        try:
            built = self._continue_plan(
                draft,
                run_root,
                planning_data_room=str(candidate_path) if isinstance(candidate_path, Path) else None,
            )
            transaction_handoff = None
            if isinstance(candidate_path, Path):
                transaction_handoff = {
                    "plan": built["plan"].to_dict(),
                    "reopened_item_ids": tuple(record.requirement_id for record in built["parentPlan"].input_records),
                    "expected_parent_generation_id": built["lifecycle"].generation_id,
                    "expected_parent_state_hash": built["parentStateHash"],
                    "expected_parent_plan_hash": built["parentPlanHash"],
                }
            mission_context = built.get("missionContext")
            if mission_context is not None:
                mission_artifacts = self._write_mission_artifacts(
                    run_root,
                    draft,
                    mission_context=mission_context,
                    requirement_ids=(record.requirement_id for record in built["plan"].input_records),
                    portfolio_strategy=built["plan"].portfolio_strategy,
                )
            data = self._publish_staged_data_revision(
                draft,
                run_root,
                staged,
                transaction_handoff=transaction_handoff,
            )
            prior_revision = built["dataRoom"].get("revision")
            if prior_revision is None or data["revision"].manifest_hash != prior_revision.manifest_hash:
                built = dict(built)
                built["context"] = data["context"]
                built["dataRoom"] = data["dataRoom"]
        finally:
            if isinstance(candidate_path, Path):
                candidate_path.unlink(missing_ok=True)
        revision = data["revision"]
        expected = draft.get("dataRevision") if isinstance(draft.get("dataRevision"), Mapping) else {}
        refresh_required, _active_revision, _canonical_pending = self._continuation_refresh_required(built, data)
        data_advanced = bool(data["provenance"].get("changed"))
        reopened_ids = [record.requirement_id for record in built["parentPlan"].input_records] if refresh_required else []
        if not refresh_required:
            # A duplicate/no-op source snapshot does not need a refresh
            # admission.  If D-0001 initialization created a journal while
            # this candidate was being planned, clear it only after the plan
            # has succeeded.
            api["DataRevisionStore"](data["context"]).complete_revision_transaction(
                revision,
                launch_draft_id=str(draft["draftId"]),
                launch_fingerprint=str(draft["fingerprint"]),
            )
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
            "dataOnly": not bool(draft.get("intakeBlocks")),
            "reopenedItemIds": reopened_ids,
            "dataRoom": built["dataRoom"]["dataRoom"],
            "dataRoomSha256": built["dataRoom"]["sha256"],
            "dataRoomSize": built["dataRoom"]["size"],
            "inputRoots": [str(value) for value in built["dataRoom"]["inputRoots"]],
            "dataRevision": revision.to_dict(),
            "dataRevisionRef": built["dataRoom"]["dataRoom"],
            "dataRevisionManifestHash": revision.manifest_hash,
            "dataRevisionArchiveSha256": revision.archive_sha256,
            "dataRevisionProvenance": data["provenance"],
            "refreshRequired": refresh_required,
            "expectedDataRevision": expected,
            "capacity": built["capacity"],
            "createdAt": utc_now(),
        }
        if mission_artifacts is not None:
            intent.update(
                {
                    "missionContext": mission_context.to_dict(),
                    "missionContextRef": mission_artifacts["missionContextRef"],
                    "missionContextHash": mission_artifacts["missionContextHash"],
                    "missionPlanRef": mission_artifacts["missionPlanRef"],
                    "missionPlanHash": mission_artifacts["missionPlanHash"],
                    "documentCatalogRef": mission_artifacts.get("documentCatalogRef"),
                    "documentCatalogHash": mission_artifacts.get("documentCatalogHash"),
                    "activeMissionPointer": mission_artifacts.get("activePointer"),
                    "parentMissionContextHash": (
                        mission_context.metadata.get("parent_context_hash")
                        if isinstance(mission_context.metadata, Mapping)
                        else None
                    ),
                }
            )
        elif mission_context is not None and built.get("missionContextHash") is not None:
            intent.update(
                {
                    "missionContext": mission_context.to_dict(),
                    "missionContextRef": built.get("missionContextRef"),
                    "missionContextHash": built.get("missionContextHash"),
                    "missionPlanRef": built.get("missionPlanRef"),
                    "missionPlanHash": built.get("missionPlanHash"),
                    "documentCatalogRef": built.get("documentCatalogRef"),
                    "documentCatalogHash": built.get("documentCatalogHash"),
                    "activeMissionPointer": built.get("activeMissionPointer"),
                    "parentMissionContextHash": (
                        mission_context.metadata.get("parent_context_hash")
                        if isinstance(mission_context.metadata, Mapping)
                        else None
                    ),
                }
            )
        if reopened_ids:
            pending = self._admit_continue_data_refresh(draft, built, intent)
            intent["pendingDataRefreshIntentHash"] = pending.intent_hash
        self._write_launch_artifact(run_root, str(draft["draftId"]), "continuation_intent.json", intent)
        return intent

    def _continuation_refresh_required(
        self,
        built: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> tuple[bool, Any | None, Any | None]:
        """Resolve refresh necessity from authoritative D/G lineage.

        Source-byte provenance alone cannot detect a draft recovering an
        already-published but not-yet-admitted D revision.  Compare the
        pointer-authoritative current D with the active generation's direct or
        receipt-bound D, and include canonical pending/journal state in the
        structural decision.
        """

        revision = data.get("revision")
        context = data.get("context")
        if revision is None or context is None:
            raise LaunchConflictError("Current data revision is unavailable for continuation")
        store = self._core_imports()["DataRevisionStore"](context)
        try:
            active = store.active_generation_revision(
                generation_metadata=built["lifecycle"].generation_metadata,
            )
            pending = store.pending_data_refresh(allow_stale=True)
            transaction = store.revision_transaction()
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Data revision lineage is invalid") from exc
        same_active = bool(
            active is not None
            and active.revision_id == revision.revision_id
            and active.manifest_hash == revision.manifest_hash
            and active.archive_sha256 == revision.archive_sha256
        )
        pending_current = bool(
            pending is not None
            and pending.data_revision_id == revision.revision_id
            and pending.data_revision_manifest_hash == revision.manifest_hash
            and pending.data_revision_archive_sha256 == revision.archive_sha256
        )
        transaction_current = bool(
            transaction is not None
            and transaction.revision_id == revision.revision_id
            and transaction.revision_manifest_hash == revision.manifest_hash
            and transaction.revision_archive_sha256 == revision.archive_sha256
        )
        return (not same_active) or pending_current or (transaction_current and not same_active), active, pending

    def _admit_continue_data_refresh(
        self,
        draft: Mapping[str, Any],
        built: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> Any:
        """Bind a changed D revision to the canonical run-owned admission."""

        api = built["api"]
        revision = built["dataRoom"].get("revision")
        reopened = tuple(intent.get("reopenedItemIds") or ())
        if revision is None or not reopened:
            raise LaunchConflictError("Data refresh admission is missing its revision or reopened IDs")
        try:
            return api["DataRevisionStore"](built["context"]).admit_pending_data_refresh(
                data_revision=revision,
                data_revision_ref=built["dataRoom"].get("dataRoom"),
                plan=built["plan"].to_dict(),
                reopened_item_ids=reopened,
                expected_parent_generation_id=built["lifecycle"].generation_id,
                expected_parent_state_hash=built["parentStateHash"],
                expected_parent_plan_hash=built["parentPlanHash"],
                launch_draft_id=str(draft["draftId"]),
                launch_fingerprint=str(draft["fingerprint"]),
                created_at=str(intent.get("createdAt") or draft.get("createdAt") or utc_now()),
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Canonical data refresh admission failed") from exc

    def _bootstrap_continue(self, draft: Mapping[str, Any], run_root: Path, intent: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Publish or retry one exact generation through the coordinator transaction."""

        built = self._continue_plan(draft, run_root, intent=intent)
        api = built["api"]
        plan_payload = built["plan"].to_dict()
        published: dict[str, Any] = {}
        data_refresh = bool(
            intent
            and (
                intent.get("refreshRequired") is True
                or tuple(intent.get("reopenedItemIds") or ())
            )
        )

        def result(
            *,
            queued: bool,
            coordinator: Any,
            extension: Any | None = None,
            continue_runner: bool = False,
            pending_phase: str | None = None,
        ) -> dict[str, Any]:
            if queued:
                return {
                    "queued": True,
                    "continueRunner": continue_runner,
                    "pendingPhase": pending_phase,
                    "pendingDataRefresh": True,
                    "context": built["context"],
                    "records": built["records"],
                    "capacity": built["capacity"],
                    "plan": built["plan"].to_dict(),
                    "generationId": built["generationId"],
                    "revision": None,
                    "dataRoom": built["dataRoom"]["dataRoom"],
                    "dataRoomSha256": built["dataRoom"]["sha256"],
                    "dataRoomSize": built["dataRoom"]["size"],
                    "dataRevision": built["dataRoom"].get("revision"),
                    "inputRoots": [str(value) for value in built["dataRoom"]["inputRoots"]],
                    "coordinator": coordinator,
                }
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
                "dataRevision": built["dataRoom"].get("revision"),
                "inputRoots": [str(value) for value in built["dataRoom"]["inputRoots"]],
                "coordinator": coordinator,
            }

        def publish(target_spec: Any) -> Any:
            # The coordinator owns the sole quiescent boundary.  A changed D
            # revision deliberately reopens every existing requirement; a
            # plan-only continuation uses the normal revise admission path.
            reopened = tuple(intent.get("reopenedItemIds") or ()) if isinstance(intent, Mapping) else ()
            if reopened:
                extension = api["RequirementRunExtension"].refresh_data(
                    built["context"],
                    plan=built["plan"],
                    data_revision=built["dataRoom"]["revision"],
                    reopened_item_ids=reopened,
                    generation_id=built["generationId"],
                )
            else:
                extension = api["RequirementRunExtension"].revise(
                    built["context"],
                    plan=built["plan"],
                    generation_id=built["generationId"],
                )
            published["extension"] = extension
            return extension

        control_spec = run_root / "control_plane" / "coordinator_spec.json"
        coordinator: Any | None = None
        if control_spec.is_file() and not control_spec.is_symlink():
            try:
                current_coordinator = api["RunCoordinator"].from_persisted_spec(built["context"])
                current_status = current_coordinator.status()
            except (OSError, KeyError, TypeError, ValueError):
                current_status = None
            if current_status is not None and getattr(current_status, "active_dispatches", ()):
                return result(queued=True, coordinator={"operation": "queued"})

        if data_refresh:
            # Data admissions use the same Coordinator consumer for both an
            # existing active coordinator and a stopped run.  A stopped run
            # without a control-plane spec is first started against its
            # current parent plan; the consumer then publishes the target G.
            if coordinator is None and control_spec.is_file() and not control_spec.is_symlink():
                coordinator = current_coordinator
            if coordinator is None:
                parent_payload = built["parentPlan"].to_dict()
                self._prepare_coordinator(
                    {
                        "context": built["context"],
                        "plan": parent_payload,
                        "coordinatorGenerationId": built["lifecycle"].generation_id,
                        "coordinatorPlannerHash": built["parentPlanHash"],
                    },
                    run_root,
                )
                coordinator = api["RunCoordinator"].from_persisted_spec(built["context"])
            consumed = coordinator.consume_pending_data_refresh()
            consumed_phase = getattr(consumed, "phase", None)
            if consumed_phase in {
                "data_refresh_pending",
                "plan_rebind_pending",
                "waiting_product",
                "data_revision_recovery",
            }:
                # A stopped/quiescent run still needs its ordinary Supervisor
                # loop when the parent product is incomplete or a successor D
                # handoff is recoverable.  Active dispatches remain owned by
                # the existing process and return a durable queued result.
                continue_runner = consumed_phase in {"waiting_product", "data_revision_recovery"} and not getattr(
                    consumed, "active_dispatches", ()
                )
                return result(
                    queued=True,
                    coordinator={"operation": "queued"},
                    continue_runner=continue_runner,
                    pending_phase=consumed_phase,
                )
            extension = api["RequirementRunExtension"].load(built["context"])
            coordinator_info = self._prepare_coordinator(
                {
                    "context": built["context"],
                    "plan": plan_payload,
                    "coordinatorGenerationId": extension.generation_id,
                    "coordinatorPlannerHash": sha256_file(extension.plan_path),
                },
                run_root,
            )
            staged_pointer = self._staged_pointer_from_intent(run_root, intent)
            if staged_pointer is not None:
                self._promote_staged_mission_artifacts(
                    run_root,
                    {"activePointer": staged_pointer},
                )
            return result(queued=False, coordinator=coordinator_info, extension=extension)

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
        publication_status = coordinator.get("publicationStatus")
        if publication_status is not None and getattr(publication_status, "phase", None) == "plan_rebind_pending":
            return result(queued=True, coordinator=coordinator)
        staged_pointer = self._staged_pointer_from_intent(run_root, intent)
        if staged_pointer is not None:
            self._promote_staged_mission_artifacts(
                run_root,
                {"activePointer": staged_pointer},
            )
        return result(queued=False, coordinator=coordinator, extension=published.get("extension"))

    def execute(self, *, draft_id: str, fingerprint: str, confirmed: bool) -> dict[str, Any]:
        """Execute one draft under the exact run-scoped cross-process lock."""

        if not self.settings.commands_enabled:
            raise LockedLaunchError("Launch commands are disabled; start the loopback server with --enable-launch.")
        if confirmed is not True:
            raise LaunchValidationError({"confirmed": "A second explicit confirmation is required."}, "Launch confirmation is required")
        # Resolve the lock identity before taking the instance-local mutex.
        # The authoritative draft/status admission is reloaded by the locked
        # implementation below, so a concurrent draft mutation cannot be
        # admitted under a stale run identity.
        preliminary = self._load_draft(draft_id, fingerprint)
        preliminary_run_id = preliminary.get("runId")
        preliminary_run_root = preliminary.get("runRoot")
        if not isinstance(preliminary_run_id, str) or not preliminary_run_id.strip() or not isinstance(preliminary_run_root, str) or not preliminary_run_root:
            raise LaunchConflictError("Run admission lock requires a complete draft identity")
        try:
            preliminary_root = Path(preliminary_run_root).expanduser().resolve(strict=False)
        except (OSError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Run admission lock requires a valid draft root") from exc
        with _run_admission_lock(self.settings, preliminary_run_id, preliminary_root):
            authoritative = self._load_draft(draft_id, fingerprint)
            current_run_id = authoritative.get("runId")
            current_run_root = authoritative.get("runRoot")
            if not isinstance(current_run_id, str) or not isinstance(current_run_root, str) or not current_run_root:
                raise LaunchConflictError("Run admission lock requires a complete draft identity")
            try:
                current_root = Path(current_run_root).expanduser().resolve(strict=False)
            except (OSError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Run admission lock requires a valid draft root") from exc
            if current_run_id != preliminary_run_id or current_root != preliminary_root:
                raise LaunchConflictError("Launch draft run identity changed during admission")
            return self._execute_with_run_lock(
                draft_id=draft_id,
                fingerprint=fingerprint,
                confirmed=confirmed,
            )

    def _execute_with_run_lock(self, *, draft_id: str, fingerprint: str, confirmed: bool) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Launch commands are disabled; start the loopback server with --enable-launch.")
        if confirmed is not True:
            raise LaunchValidationError({"confirmed": "A second explicit confirmation is required."}, "Launch confirmation is required")
        with self._lock:
            draft = self._load_draft(draft_id, fingerprint)
            prior = self.status(draft_id)
            # A starting launch that already carries a complete private
            # token-owned Supervisor identity is in flight, regardless of
            # whether it is a new launch or a continuation.  Re-opening a
            # continuation while that identity is live/unknown would create
            # a second child.  ``liveness == gone`` is the only positive
            # quiescent signal that permits the existing continuation retry
            # contract to proceed; missing/unknown liveness fails closed.
            if prior.get("status") == "starting":
                try:
                    private_prior = load_object(self._status_path(draft_id))
                except (OSError, ValueError):
                    private_prior = None
                if (
                    isinstance(private_prior, Mapping)
                    and _validated_process_identity(private_prior) is not None
                    and prior.get("liveness") != "gone"
                ):
                    return prior
            retry_pending = bool(
                draft.get("mode") == "continue"
                and prior.get("pendingDataRefresh") is True
                and prior.get("status") in {"accepted", "queued", "starting"}
            )
            if prior.get("status") in {"accepted", "running", "completed", "failed", "cancelled"} and not retry_pending:
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
            if draft.get("mode") == "continue":
                # A retry of the same continuation draft may already have
                # durably adopted a token-owned Supervisor (for example a
                # pending data refresh).  Treat that identity as in-flight
                # before rebuilding the continuation, while leaving a
                # different draft to complete its own durable bootstrap and
                # attach to the owner at the pre-spawn admission below.
                current_owner = self._run_owned_supervisor_status(
                    str(draft["runId"]),
                    run_root,
                )
                if current_owner is not None and current_owner.get("draftId") == draft_id:
                    return prior
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
                "idempotencyKey": draft.get("idempotencyKey"),
                "status": "starting",
                "runId": draft["runId"],
                "runRoot": str(run_root),
                "startedAt": utc_now(),
                "message": "Materialising the immutable launch package.",
            }
            atomic_write_json(self._status_path(draft_id), status_payload)
            runner_started = False
            runner_result: Mapping[str, Any] | None = None
            continue_runner_for_pending = False
            try:
                if draft.get("mode") == "new":
                    zip_path = staging_root / "data_room.zip"
                    entries = self._package_zip(draft, zip_path)
                    bootstrap = self._bootstrap_new(draft, zip_path, run_root)
                else:
                    entries = []
                    intent = continuation_intent or self._ensure_continue_intent(draft, run_root, staging_root)
                    # The intent is now durable.  Validate any pre-existing
                    # artifact bytes against the exact immutable draft+intent
                    # before RequirementRunExtension can append a generation.
                    intent = self._preflight_continuation_artifacts(draft, run_root, intent=intent)
                    assert intent is not None
                    bootstrap = self._bootstrap_continue(draft, run_root, intent=intent)
                    if bootstrap.get("queued"):
                        pending = {
                            "schemaVersion": 1,
                            "kind": "pending_data_refresh",
                            "draftId": draft_id,
                            "fingerprint": fingerprint,
                            "runId": draft["runId"],
                            "runRoot": str(run_root),
                            "generationId": bootstrap["generationId"],
                            "dataRevisionId": intent.get("dataRevisionId") or intent.get("dataRevision", {}).get("revision_id"),
                            "dataRevisionManifestHash": intent.get("dataRevisionManifestHash") or intent.get("dataRevision", {}).get("manifest_hash"),
                            "dataRevisionArchiveSha256": intent.get("dataRevisionArchiveSha256") or intent.get("dataRevision", {}).get("archive_sha256"),
                            "reopenedItemIds": list(intent.get("reopenedItemIds") or []),
                            "createdAt": draft["createdAt"],
                        }
                        self._write_launch_artifact(run_root, draft_id, "pending_data_refresh.json", pending)
                        continue_runner_for_pending = bool(bootstrap.get("continueRunner")) and not retry_pending
                    else:
                        self._pending_refresh_path(run_root, draft_id).unlink(missing_ok=True)
                    manifest = self._continuation_manifest_from_intent(draft, run_root, intent)
                    receipt = self._continuation_receipt_from_intent(draft, run_root, intent)
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
                        "missionContextRef": bootstrap.get("missionContextRef"),
                        "missionContextHash": bootstrap.get("missionContextHash"),
                        "missionPlanRef": bootstrap.get("missionPlanRef"),
                        "missionPlanHash": bootstrap.get("missionPlanHash"),
                        "documentCatalogRef": bootstrap.get("documentCatalogRef"),
                        "documentCatalogHash": bootstrap.get("documentCatalogHash"),
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
                    revision_identity = bootstrap.get("dataRevision")
                    revision_metadata = bootstrap.get("dataRevisionMetadata")
                    if isinstance(revision_identity, Mapping):
                        manifest.update(
                            {
                                "dataRevisionId": revision_identity.get("revisionId"),
                                "dataRevisionManifestHash": revision_identity.get("manifestHash"),
                                "dataRevisionArchiveSha256": revision_identity.get("archiveSha256"),
                            }
                        )
                    if isinstance(revision_metadata, Mapping):
                        manifest["dataRevision"] = dict(revision_metadata)
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
                    if isinstance(bootstrap.get("dataRevision"), Mapping):
                        receipt.update(
                            {
                                "dataRevisionId": bootstrap["dataRevision"].get("revisionId"),
                                "dataRevisionManifestHash": bootstrap["dataRevision"].get("manifestHash"),
                                "dataRevisionArchiveSha256": bootstrap["dataRevision"].get("archiveSha256"),
                            }
                        )
                self._write_launch_artifact(run_root, draft_id, "launch_receipt.json", receipt)
                if draft.get("mode") == "new":
                    self._write_control_once(run_root, "launch_receipt.json", receipt)
                if draft.get("mode") == "continue" and bootstrap.get("queued") and not continue_runner_for_pending:
                    owner = self._run_owned_supervisor_status(
                        str(draft["runId"]),
                        run_root,
                        exclude_draft_id=draft_id,
                    )
                    if owner is not None:
                        status_payload.update(
                            {
                                "pendingDataRefresh": True,
                                "dataRevisionId": intent.get("dataRevisionId") or intent.get("dataRevision", {}).get("revision_id"),
                                "dataRevisionManifestHash": intent.get("dataRevisionManifestHash") or intent.get("dataRevision", {}).get("manifest_hash"),
                                "dataRevisionArchiveSha256": intent.get("dataRevisionArchiveSha256") or intent.get("dataRevision", {}).get("archive_sha256"),
                                "reopenedItemIds": list(intent.get("reopenedItemIds") or []),
                            }
                        )
                        self._attach_run_owned_identity(status_payload, owner)
                        status_payload.update(
                            {
                                "status": "queued",
                                "message": "Continuation is queued behind the existing token-owned Foundry Supervisor.",
                            }
                        )
                        atomic_write_json(self._status_path(draft_id), status_payload)
                        return self.status(draft_id)
                    if not retry_pending:
                        status_payload.update(
                            {
                                "status": "queued",
                                "message": "Data revision published; refresh is queued for the next safe scheduler boundary.",
                                "pendingDataRefresh": True,
                                "dataRevisionId": intent.get("dataRevisionId") or intent.get("dataRevision", {}).get("revision_id"),
                                "dataRevisionManifestHash": intent.get("dataRevisionManifestHash") or intent.get("dataRevision", {}).get("manifest_hash"),
                                "dataRevisionArchiveSha256": intent.get("dataRevisionArchiveSha256") or intent.get("dataRevision", {}).get("archive_sha256"),
                                "reopenedItemIds": list(intent.get("reopenedItemIds") or []),
                            }
                        )
                        atomic_write_json(self._status_path(draft_id), status_payload)
                        return self.status(draft_id)
                    atomic_write_json(self._status_path(draft_id), prior)
                    return dict(prior)
                coordinator = bootstrap.get("coordinator") if draft.get("mode") == "continue" else None
                if not isinstance(coordinator, Mapping):
                    coordinator = self._prepare_coordinator(
                        bootstrap,
                        run_root,
                    )
                # Bootstrap/rebind work above is durable and may be needed
                # even when another prepared draft has already started this
                # run's Supervisor.  Admission is run-wide, however: reload
                # every exact run-bound status immediately before spawning so
                # a newer identity-less draft cannot hide an older owner.
                owner = self._run_owned_supervisor_status(
                    str(draft["runId"]),
                    run_root,
                    exclude_draft_id=draft_id,
                )
                if owner is not None:
                    if draft.get("mode") == "new":
                        raise LaunchConflictError(
                            "Run already has a token-owned Foundry Supervisor"
                        )
                    self._attach_run_owned_identity(status_payload, owner)
                    if draft.get("mode") == "continue" and bootstrap.get("queued"):
                        status_payload["pendingDataRefresh"] = True
                        status_payload.update(
                            {
                                "dataRevisionId": intent.get("dataRevisionId") or intent.get("dataRevision", {}).get("revision_id"),
                                "dataRevisionManifestHash": intent.get("dataRevisionManifestHash") or intent.get("dataRevision", {}).get("manifest_hash"),
                                "dataRevisionArchiveSha256": intent.get("dataRevisionArchiveSha256") or intent.get("dataRevision", {}).get("archive_sha256"),
                                "reopenedItemIds": list(intent.get("reopenedItemIds") or []),
                            }
                        )
                    status_payload.update(
                        {
                            # This is a different draft aliasing an already
                            # running/starting Supervisor.  Keep it queued,
                            # never ``starting``: cancellation of the alias
                            # must not terminate the shared owner.  The
                            # owner status remains private identity metadata
                            # and is reconciled by run-control reload.
                            "status": "queued",
                            "message": "Continuation attached to the existing token-owned Foundry Supervisor; no second process was started.",
                        }
                    )
                    atomic_write_json(self._status_path(draft_id), status_payload)
                    return self.status(draft_id)
                runner_result = self.runner.start(
                    run_id=str(draft["runId"]),
                    run_root=run_root,
                    manifest_path=manifest_path,
                    capacity=draft["effectiveCapacity"],
                )
                runner_started = True
                if _validated_process_identity(runner_result) is None:
                    raise LaunchConflictError("Foundry Supervisor returned no complete process identity")
                # The Supervisor owns a live child as soon as ``start``
                # returns.  Merge the complete private identity and persist
                # it before any further post-start operation (including the
                # staged mission-artifact promotion) can fail.  This makes
                # manager cleanup/reload authoritative even when a later
                # admission step raises and token-owned termination is
                # uncertain.
                allowed_runner = {
                    key: runner_result[key]
                    for key in (
                        "monitorRunId",
                        "pid",
                        "processGroupId",
                        "processGroupToken",
                        "startupToken",
                        "ready",
                        "processStart",
                        "readyAt",
                        "startupTimedOut",
                        "childExited",
                        "exitCode",
                        "exitAt",
                    )
                    if isinstance(runner_result, Mapping) and key in runner_result
                }
                status_payload.update(allowed_runner)
                atomic_write_json(self._status_path(draft_id), status_payload)
                if draft.get("mode") == "new":
                    # Initial launch admission is durable only after the
                    # manifest/receipt, coordinator spec, and Supervisor
                    # process identity all exist.  Until this point the
                    # staged sidecars cannot become the retry parent.
                    self._promote_staged_mission_artifacts(
                        run_root,
                        {"activePointer": bootstrap.get("activeMissionPointer")},
                    )
                if draft.get("mode") == "new" and isinstance(bootstrap.get("dataRevision"), Mapping):
                    status_payload.update(
                        {
                            "dataRevisionId": bootstrap["dataRevision"].get("revisionId"),
                            "dataRevisionManifestHash": bootstrap["dataRevision"].get("manifestHash"),
                            "dataRevisionArchiveSha256": bootstrap["dataRevision"].get("archiveSha256"),
                            "reopenedItemIds": [],
                        }
                    )
                elif draft.get("mode") == "continue" and isinstance(intent, Mapping):
                    status_payload.update(
                        {
                            "dataRevisionId": intent.get("dataRevisionId") or intent.get("dataRevision", {}).get("revision_id"),
                            "dataRevisionManifestHash": intent.get("dataRevisionManifestHash") or intent.get("dataRevision", {}).get("manifest_hash"),
                            "dataRevisionArchiveSha256": intent.get("dataRevisionArchiveSha256") or intent.get("dataRevision", {}).get("archive_sha256"),
                            "reopenedItemIds": list(intent.get("reopenedItemIds") or []),
                        }
                    )
                if isinstance(runner_result, Mapping) and runner_result.get("ready") is False:
                    if runner_result.get("childExited") is True:
                        status_payload.update(
                            {
                                "status": "failed",
                                "message": "Foundry Supervisor exited before publishing its readiness receipt.",
                                "completedAt": utc_now(),
                            }
                        )
                    else:
                        status_payload.update(
                            {
                                "status": "starting",
                                "message": "Foundry Supervisor is still starting; readiness timed out but the live child was retained.",
                            }
                        )
                    atomic_write_json(self._status_path(draft_id), status_payload)
                    return self.status(draft_id)
                runner_ready = isinstance(runner_result, Mapping) and runner_result.get("ready") is True
                if draft.get("mode") == "continue" and bootstrap.get("queued") and continue_runner_for_pending:
                    status_payload.update(
                        {
                            "status": "running" if runner_ready else "accepted",
                            "message": "Run continues; data refresh is pending the next safe scheduler boundary.",
                            "pendingDataRefresh": True,
                        }
                    )
                else:
                    status_payload.update(
                        {
                            "status": "running" if runner_ready else "accepted",
                            "message": (
                                "Run initialized and Foundry Supervisor is ready."
                                if runner_ready
                                else "Run initialized and Foundry Supervisor accepted."
                            ),
                            "acceptedAt": utc_now(),
                        }
                    )
                atomic_write_json(self._status_path(draft_id), status_payload)
                return self.status(draft_id)
            except Exception as exc:
                cleanup_error: Exception | None = None
                # A real SubprocessRunner may fail during readiness before
                # ``start`` can return its identity.  The startup exception
                # carries that exact token-bound mapping so manager cleanup
                # can retry ownership without guessing a PID or process
                # group.  Do not mark the launch successful on this path.
                transferred_started = getattr(exc, "started", None)
                if not runner_started and isinstance(transferred_started, Mapping):
                    runner_result = dict(transferred_started)
                    runner_started = True
                    status_payload.update(
                        {
                            key: runner_result[key]
                            for key in (
                                "monitorRunId",
                                "pid",
                                "processGroupId",
                                "processGroupToken",
                                "startupToken",
                                "ready",
                                "processStart",
                                "readyAt",
                                "startupTimedOut",
                                "childExited",
                                "exitCode",
                                "exitAt",
                            )
                            if key in runner_result
                        }
                    )
                if runner_started and isinstance(runner_result, Mapping):
                    process_group_id = runner_result.get("processGroupId")
                    process_group_token = runner_result.get("processGroupToken")
                    if (
                        isinstance(process_group_id, int)
                        and not isinstance(process_group_id, bool)
                        and process_group_id > 1
                        and _valid_process_group_token(process_group_token)
                    ):
                        try:
                            terminated = _terminate_token_owned_process_group(process_group_id, process_group_token)
                            if terminated is not True:
                                cleanup_error = LaunchConflictError(
                                    "Foundry Supervisor process-group cleanup was not confirmed"
                                )
                        except Exception as cleanup_exc:
                            cleanup_error = cleanup_exc
                complete_identity = _validated_process_identity(runner_result)
                if cleanup_error is not None and complete_identity is not None:
                    # A live/unknown token-owned child remains under the
                    # manager's durable ownership when exact cleanup is not
                    # confirmed.  Keep all private identity fields and leave
                    # the launch in recoverable ``starting`` rather than
                    # falsely terminal ``failed``; reload/control can then
                    # detect the orphan and block duplicate resume.
                    status_payload.update(
                        {
                            key: runner_result[key]
                            for key in (
                                "monitorRunId",
                                "pid",
                                "processGroupId",
                                "processGroupToken",
                                "startupToken",
                                "ready",
                                "processStart",
                                "readyAt",
                                "startupTimedOut",
                                "childExited",
                                "exitCode",
                                "exitAt",
                            )
                            if key in runner_result
                        }
                    )
                    status_payload.update(
                        {
                            "status": "starting",
                            "message": (
                                "Foundry Supervisor launch hit an admission error; "
                                "process cleanup was not confirmed and the child remains recoverable."
                            ),
                        }
                    )
                elif draft.get("mode") == "continue" and not runner_started:
                    status_payload.update({"status": "starting", "message": f"Continuation is retryable: {str(exc)[:240]}"})
                else:
                    status_payload.update({"status": "failed", "message": str(exc)[:300], "completedAt": utc_now()})
                status_error: Exception | None = None
                try:
                    atomic_write_json(self._status_path(draft_id), status_payload)
                except Exception as status_exc:
                    status_error = status_exc
                if cleanup_error is not None:
                    raise LaunchConflictError("Foundry Supervisor process cleanup failed after launch error") from cleanup_error
                if status_error is not None:
                    raise LaunchConflictError("Launch failed and its status could not be persisted") from status_error
                raise
            finally:
                shutil.rmtree(staging_root, ignore_errors=True)

    def record_process_start(
        self,
        run_id: str,
        run_root: Path,
        started: Mapping[str, Any],
    ) -> bool:
        """Persist the latest Supervisor identity in the existing launch status.

        Pause/resume starts the child outside :meth:`execute`, so it cannot
        rely on the normal status write that accompanies a new launch.  Keep
        the process-group identity in that same bounded status store and
        update only a status whose run binding matches exactly.
        """

        if not isinstance(started, Mapping):
            return False
        try:
            expected_root = Path(run_root).expanduser().resolve(strict=False)
        except OSError:
            raise LaunchConflictError("Supervisor run root is unavailable for process tracking")
        statuses_root = self.status_root
        if statuses_root.is_symlink() or (statuses_root.exists() and not statuses_root.is_dir()):
            raise LaunchConflictError("Supervisor process status store is unavailable")
        statuses_root.mkdir(parents=True, exist_ok=True)
        matches: list[tuple[str, Path, dict[str, Any]]] = []
        try:
            children = sorted(statuses_root.iterdir(), key=lambda item: item.name)
        except OSError:
            raise LaunchConflictError("Supervisor process status store is unreadable")
        for child in children:
            if child.is_symlink() or not child.is_file() or child.suffix != ".json":
                continue
            try:
                value = load_object(child)
            except ValueError:
                continue
            if value.get("runId") != run_id:
                continue
            raw_root = value.get("runRoot")
            if not isinstance(raw_root, str) or not raw_root:
                continue
            try:
                if Path(raw_root).expanduser().resolve(strict=False) != expected_root:
                    continue
            except OSError:
                continue
            matches.append((str(value.get("startedAt") or value.get("acceptedAt") or ""), child, value))
        if not matches:
            status_digest = hashlib.sha256(
                (run_id + "\x00" + str(expected_root)).encode("utf-8")
            ).hexdigest()[:32]
            status_id = f"resume-{status_digest}"
            path = self._status_path(status_id)
            if path.exists() or path.is_symlink():
                try:
                    existing = load_object(path)
                except ValueError as exc:
                    raise LaunchConflictError("Supervisor process status record is unreadable") from exc
                if (
                    existing.get("runId") != run_id
                    or existing.get("runRoot") != str(expected_root)
                ):
                    raise LaunchConflictError("Supervisor process status record is bound to another run")
                value = existing
            else:
                now = utc_now()
                value = {
                    "draftId": status_id,
                    "runId": run_id,
                    "runRoot": str(expected_root),
                    "status": "accepted",
                    "message": "Foundry Supervisor resumed from durable progress.",
                    "startedAt": now,
                    "acceptedAt": now,
                }
                atomic_write_json(path, value)
            matches.append((str(value.get("startedAt") or value.get("acceptedAt") or ""), path, value))
        _stamp, path, value = max(matches, key=lambda item: (item[0], item[1].name))
        updated = dict(value)
        for key in ("monitorRunId", "pid"):
            if key in started and started[key] is not None:
                updated[key] = started[key]
        process_group_id = started.get("processGroupId")
        if (
            isinstance(process_group_id, int)
            and not isinstance(process_group_id, bool)
            and process_group_id > 1
        ):
            updated["processGroupId"] = process_group_id
        process_group_token = started.get("processGroupToken")
        if _valid_process_group_token(process_group_token):
            updated["processGroupToken"] = process_group_token
        startup_token = started.get("startupToken")
        if _valid_process_group_token(startup_token):
            updated["startupToken"] = startup_token
        for key in ("processStart", "readyAt", "startupTimedOut", "childExited", "exitCode", "exitAt", "ready"):
            if key in started and started.get(key) is not None:
                updated[key] = started[key]
        if started.get("ready") is True:
            updated.update(
                {
                    "status": "running",
                    "acceptedAt": updated.get("acceptedAt") or started.get("readyAt") or utc_now(),
                    "message": "Run resumed and Foundry Supervisor is ready.",
                }
            )
        elif started.get("childExited") is True:
            updated.update(
                {
                    "status": "failed",
                    "completedAt": started.get("exitAt") or utc_now(),
                    "message": "Foundry Supervisor exited before readiness.",
                }
            )
        elif started.get("startupTimedOut") is True:
            updated.update(
                {
                    "status": "starting",
                    "message": "Foundry Supervisor is still starting; readiness timed out but the live child was retained.",
                }
            )
        atomic_write_json(path, updated)
        return True

    def ensure_process_status(self, run_id: str, run_root: Path) -> bool:
        """Create a deterministic resume status record before spawning."""

        return self.record_process_start(run_id, run_root, {})

    def cancel(self, *, draft_id: str, fingerprint: str, confirmed: bool) -> dict[str, Any]:
        """Cancel one draft under the exact run-scoped cross-process lock."""

        if not self.settings.commands_enabled:
            raise LockedLaunchError("Launch commands are disabled; start the loopback server with --enable-launch.")
        if confirmed is not True:
            raise LaunchValidationError({"confirmed": "A second explicit confirmation is required."}, "Cancellation confirmation is required")
        preliminary = self._load_draft(draft_id, fingerprint)
        preliminary_run_id = preliminary.get("runId")
        preliminary_run_root = preliminary.get("runRoot")
        if not isinstance(preliminary_run_id, str) or not preliminary_run_id.strip() or not isinstance(preliminary_run_root, str) or not preliminary_run_root:
            raise LaunchConflictError("Run admission lock requires a complete draft identity")
        try:
            preliminary_root = Path(preliminary_run_root).expanduser().resolve(strict=False)
        except (OSError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Run admission lock requires a valid draft root") from exc
        with _run_admission_lock(self.settings, preliminary_run_id, preliminary_root):
            authoritative = self._load_draft(draft_id, fingerprint)
            current_run_id = authoritative.get("runId")
            current_run_root = authoritative.get("runRoot")
            if not isinstance(current_run_id, str) or not isinstance(current_run_root, str) or not current_run_root:
                raise LaunchConflictError("Run admission lock requires a complete draft identity")
            try:
                current_root = Path(current_run_root).expanduser().resolve(strict=False)
            except (OSError, TypeError, ValueError) as exc:
                raise LaunchConflictError("Run admission lock requires a valid draft root") from exc
            if current_run_id != preliminary_run_id or current_root != preliminary_root:
                raise LaunchConflictError("Launch draft run identity changed during cancellation")
            return self._cancel_with_run_lock(draft_id=draft_id, fingerprint=fingerprint, confirmed=confirmed)

    def _cancel_with_run_lock(self, *, draft_id: str, fingerprint: str, confirmed: bool) -> dict[str, Any]:
        """Run cancellation after the per-run lock and authoritative reload."""

        with self._lock:
            draft = self._load_draft(draft_id, fingerprint)
            prior = self.status(draft_id)
            current_status = str(prior.get("status") or "prepared").strip().lower()
            if current_status == "cancelled":
                return prior
            if current_status not in {"prepared", "starting", "failed"}:
                raise LaunchConflictError("Only an unstarted or failed launch preparation can be cancelled")
            # ``status()`` intentionally omits the private process-group token
            # from its public projection.  Reload the bounded status record so
            # cancellation can prove ownership of a live Supervisor before it
            # writes ``cancelled``.  A timeout leaves the child in ``starting``;
            # termination failure therefore remains recoverable and must never
            # be represented as a successful cancellation.
            private_status: Mapping[str, Any] = {}
            try:
                loaded_private = load_object(self._status_path(draft_id))
            except (OSError, ValueError):
                loaded_private = {}
            if isinstance(loaded_private, Mapping):
                private_status = loaded_private
            if current_status in {"starting", "failed"}:
                identity = _validated_process_identity(private_status)
                if current_status == "starting" and identity is None:
                    raise LaunchConflictError(
                        "Supervisor cancellation requires a complete token-owned process identity; launch remains recoverable"
                    )
                if identity is not None:
                    _pid, process_group_id, process_group_token = identity
                    try:
                        terminated = _terminate_token_owned_process_group(
                            process_group_id,
                            process_group_token,
                        )
                    except Exception as exc:
                        raise LaunchConflictError(
                            "Supervisor process-group termination failed; launch remains recoverable"
                        ) from exc
                    if terminated is not True:
                        raise LaunchConflictError(
                            "Supervisor process-group termination was not confirmed; launch remains recoverable"
                        )
            payload = {
                "draftId": draft_id,
                "fingerprint": fingerprint,
                "idempotencyKey": draft.get("idempotencyKey"),
                "status": "cancelled",
                "runId": draft.get("runId"),
                "runRoot": draft.get("runRoot"),
                "message": "Launch preparation cancelled; start a new preparation to retry.",
                "completedAt": utc_now(),
            }
            # Retain the private identity in the durable status record (the
            # public ``status()`` projection still omits the token).  Reload
            # can therefore detect an unexpected live child instead of
            # treating a stale cancellation as terminal truth.
            payload.update(
                {
                    key: private_status[key]
                    for key in (
                        "monitorRunId",
                        "pid",
                        "processGroupId",
                        "processGroupToken",
                        "startupToken",
                        "processStart",
                        "ready",
                        "readyAt",
                        "startupTimedOut",
                        "childExited",
                        "exitCode",
                        "exitAt",
                    )
                    if key in private_status
                }
            )
            atomic_write_json(self._status_path(draft_id), payload)
            return self.status(draft_id)

    @staticmethod
    def _status_age_seconds(value: Mapping[str, Any]) -> float | None:
        stamp = value.get("startedAt") or value.get("acceptedAt")
        if not isinstance(stamp, str) or not stamp:
            return None
        try:
            observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())
        except (TypeError, ValueError):
            return None

    def _reconcile_status(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Reconcile launch state from hash-bound child receipts after reload.

        This method never signals a process.  A live child without a receipt
        remains ``starting``/``accepted``; only an observed terminal receipt or
        a positively stale, token-owned process absence changes the durable
        launch status.
        """

        result = dict(value)
        current = str(result.get("status") or "unknown").strip().lower()
        if current == "cancelled":
            # A cancellation is terminal only after the caller has proved
            # that the exact token-owned Supervisor group stopped.  If a
            # forged/stale status says ``cancelled`` while that group is still
            # live (or liveness cannot be checked), project recoverable
            # ``starting`` instead of concealing a child on reload.  Prepared
            # cancellations have no process identity and remain unchanged.
            identity = _validated_process_identity(result)
            if identity is None:
                return result
            _pid, process_group_id, process_group_token = identity
            try:
                liveness = _process_group_has_token(process_group_id, process_group_token)
            except LaunchConflictError:
                liveness = None
            if liveness is True or liveness is None:
                result.update(
                    {
                        "status": "starting",
                        "liveness": "live" if liveness is True else "unknown",
                        "startupTimedOut": True,
                        "message": (
                            "Supervisor cancellation was not confirmed; the live child remains recoverable."
                            if liveness is True
                            else "Supervisor cancellation could not be verified; launch remains recoverable."
                        ),
                    }
                )
                draft_component = result.get("draftId")
                if isinstance(draft_component, str) and draft_component:
                    atomic_write_json(self._status_path(draft_component), result)
            return result
        if current not in {"starting", "accepted", "running"}:
            return result
        raw_root = result.get("runRoot")
        run_id = result.get("runId")
        startup_token = result.get("startupToken")
        process_group_token = result.get("processGroupToken")
        if not isinstance(raw_root, str) or not raw_root or not isinstance(run_id, str):
            return result
        # Startup and process-group tokens are deliberately distinct.  The
        # startup token binds readiness/exit receipts; the process-group token
        # is private ownership proof for ``ps``/termination liveness checks.
        # Never use the receipt token to probe process-group membership.
        receipt_token = startup_token if _valid_process_group_token(startup_token) else None
        liveness_token = (
            process_group_token
            if _valid_process_group_token(process_group_token)
            else None
        )
        try:
            run_root = Path(raw_root).expanduser().resolve(strict=False)
            ready = _load_receipt(
                run_root,
                SUPERVISOR_READY_FILENAME,
                kind="foundry_supervisor_ready",
                run_id=run_id,
                startup_token=receipt_token,
            ) if receipt_token is not None else None
            exit_receipt = _load_receipt(
                run_root,
                SUPERVISOR_EXIT_FILENAME,
                kind="foundry_supervisor_exit",
                run_id=run_id,
                startup_token=receipt_token,
            ) if receipt_token is not None else None
            expected_spec_hash = SubprocessRunner._coordinator_spec_hash(run_root) if (ready is not None or exit_receipt is not None) else None
            expected_route_hash = SubprocessRunner._role_routing_hash(run_root) if (ready is not None or exit_receipt is not None) else None
            for receipt in (ready, exit_receipt):
                if receipt is not None and (
                    receipt.get("specHash") != expected_spec_hash
                    or receipt.get("roleRoutingHash") != expected_route_hash
                ):
                    raise LaunchConflictError("Supervisor receipt is bound to a different coordinator specification")
            if ready is not None and (
                ready.get("pid") != result.get("pid")
                or ready.get("processGroupId") != result.get("processGroupId")
                or not isinstance(ready.get("processStart"), str)
                or not ready.get("processStart")
            ):
                raise LaunchConflictError("Supervisor readiness identity does not match launch status")
        except (OSError, ValueError, LaunchConflictError) as exc:
            # A receipt can be malformed while the token-bearing child is
            # still live.  Marking such a launch ``failed`` would hide an
            # owned process from reload/retry.  Preserve a recoverable
            # starting state until liveness is positively gone; unknown
            # liveness also fails closed to starting.
            liveness: bool | None = None
            process_group_id = result.get("processGroupId")
            if (
                liveness_token is not None
                and isinstance(process_group_id, int)
                and not isinstance(process_group_id, bool)
                and process_group_id > 1
            ):
                try:
                    liveness = _process_group_has_token(process_group_id, liveness_token)
                except LaunchConflictError:
                    liveness = None
            if liveness_token is not None and liveness is not False:
                result.update(
                    {
                        "status": "starting",
                        "liveness": "live" if liveness is True else "unknown",
                        "message": (
                            "Supervisor receipt integrity failed while the child remains live; launch is recoverable."
                            if liveness is True
                            else "Supervisor receipt integrity failed and child liveness is unverified; launch is recoverable."
                        ),
                    }
                )
            else:
                result.update(
                    {
                        "status": "failed",
                        "message": f"Supervisor receipt integrity check failed: {str(exc)[:220]}",
                        "completedAt": utc_now(),
                    }
                )
            draft_component = result.get("draftId")
            if isinstance(draft_component, str) and draft_component:
                atomic_write_json(self._status_path(draft_component), result)
            return result

        changed = False
        if ready is not None and current in {"starting", "accepted"}:
            result.update(
                {
                    "status": "running",
                    "ready": True,
                    "readyAt": ready.get("readyAt"),
                    "processStart": ready.get("processStart"),
                    "message": "Run initialized and Foundry Supervisor is ready.",
                    "acceptedAt": result.get("acceptedAt") or ready.get("readyAt") or utc_now(),
                }
            )
            current = "running"
            changed = True
        if exit_receipt is not None and current in {"starting", "accepted", "running"}:
            code = exit_receipt.get("exitCode")
            failed = isinstance(code, bool) or not isinstance(code, int) or code != 0
            result.update(
                {
                    "status": "failed" if failed else "completed",
                    "exitCode": code,
                    "exitAt": exit_receipt.get("exitAt"),
                    "completedAt": exit_receipt.get("exitAt") or utc_now(),
                    "message": (
                        "Foundry Supervisor exited before readiness."
                        if current == "starting" and failed
                        else "Foundry Supervisor exited with an error."
                        if failed
                        else "Foundry Supervisor completed."
                    ),
                }
            )
            changed = True
            current = str(result["status"])

        # A timeout or crash before a receipt must remain recoverable while a
        # token-bearing process group is still live.  Once the process is
        # demonstrably gone, mark the launch failed so reloads do not show a
        # permanently running placeholder.  Legacy/fake statuses without a
        # persisted process-group token retain their historical semantics.
        if current in {"starting", "running"} and liveness_token is not None and exit_receipt is None:
            process_group_id = result.get("processGroupId")
            liveness: bool | None = None
            if isinstance(process_group_id, int) and not isinstance(process_group_id, bool) and process_group_id > 1:
                try:
                    liveness = _process_group_has_token(process_group_id, liveness_token)
                except LaunchConflictError:
                    liveness = None
            age = self._status_age_seconds(result)
            stale = age is not None and age >= SUPERVISOR_STALE_STARTING_SECONDS
            if liveness is False and (current == "running" or stale or not result.get("startupTimedOut")):
                result.update(
                    {
                        "status": "failed",
                        "message": "Foundry Supervisor is no longer alive; launch is recoverable.",
                        "completedAt": utc_now(),
                    }
                )
                changed = True
            elif liveness is not None:
                result["liveness"] = "live" if liveness else "gone"

        if changed:
            draft_component = result.get("draftId")
            if isinstance(draft_component, str) and draft_component:
                atomic_write_json(self._status_path(draft_component), result)
        return result

    def status(self, draft_id: str) -> dict[str, Any]:
        try:
            value = load_object(self._status_path(draft_id))
        except (OSError, ValueError):
            try:
                draft = self._load_draft(draft_id)
            except Exception:
                return {"draftId": draft_id, "status": "unknown", "message": "Launch status unavailable."}
            return {
                "draftId": draft_id,
                "fingerprint": draft.get("fingerprint"),
                "idempotencyKey": draft.get("idempotencyKey"),
                "status": "prepared",
                "runId": draft.get("runId"),
                "runRoot": draft.get("runRoot"),
                "recoverable": True,
                "cancelable": True,
                "message": "Launch package is prepared and awaiting confirmation.",
            }
        allowed = (
            "draftId",
            "fingerprint",
            "idempotencyKey",
            "status",
            "runId",
            "runRoot",
            "monitorRunId",
            "message",
            "startedAt",
            "acceptedAt",
            "completedAt",
            "pid",
            "processGroupId",
            "ready",
            "readyAt",
            "processStart",
            "startupTimedOut",
            "childExited",
            "exitCode",
            "exitAt",
            "liveness",
            "pendingDataRefresh",
            "dataRevisionId",
            "dataRevisionRef",
            "dataRevisionManifestHash",
            "dataRevisionArchiveSha256",
            "reopenedItemIds",
        )
        reconciled = self._reconcile_status(value)
        result = {key: reconciled[key] for key in allowed if key in reconciled}
        status_value = str(result.get("status") or "unknown").strip().lower()
        result["recoverable"] = status_value in {"prepared", "starting", "failed"}
        result["cancelable"] = status_value in {"prepared", "starting"}
        return result

    def reconcile_launch_statuses(self, *, max_records: int = 512) -> int:
        """Reconcile persisted launch states after a server/reload boundary.

        The browser normally polls one draft while a launch is in flight.  A
        reload or process crash has no in-memory draft, however, so the runs
        listing must still consume hash-bound readiness/exit receipts before
        projecting placeholders.  Only bounded status metadata is read; this
        method never starts, stops, or resumes a Supervisor process.
        """

        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValueError("max_records must be positive")
        root = self.status_root
        if root.is_symlink() or not root.is_dir():
            return 0
        try:
            paths = [
                child
                for child in root.iterdir()
                if child.suffix == ".json" and child.is_file() and not child.is_symlink()
            ]
        except OSError:
            return 0
        # Timestamp ordering is owned by the projection.  Reconcile every
        # bounded status file in deterministic filename order; status() itself
        # remains cheap when no startup token/receipt is present.
        reconciled = 0
        for path in sorted(paths, key=lambda item: item.name)[:max_records]:
            try:
                value = load_object(path)
                draft_id = value.get("draftId")
                if isinstance(draft_id, str) and draft_id:
                    self.status(draft_id)
                    reconciled += 1
            except (OSError, ValueError, LaunchError):
                continue
        return reconciled


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
