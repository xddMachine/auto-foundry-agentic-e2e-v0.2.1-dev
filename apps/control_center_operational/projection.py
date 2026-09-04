"""Passive, privacy-preserving operational graph projection.

The operational graph is intentionally event-driven.  A non-Planner node is
created only from a bounded coordinator dispatch/result event or an explicitly
emitted lifecycle event.  Durable item/domain/lease files remain useful run
metadata, but they are not agent-invocation evidence and therefore do not
populate the live graph.  Prompts, messages, model responses, commands, raw
payloads, and analytical data are never copied to the browser projection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import re
import secrets
import threading
from time import monotonic
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from apps.control_center_operational.launch import LaunchManager
from apps.control_center_operational.read_model import (
    MAX_DISCOVERED_RUNS,
    MAX_EVENT_PAGE_BYTES,
    MAX_EVENT_PAGE_ITEMS,
    ReadOnlyRepository,
    RunRecord,
    SKIPPED_DIRECTORIES,
    _capacity_projection,
    _jsonl_page,
    _run_id_for_path,
    _safe_text,
    _summarize_run_state,
)


MAX_PROJECTION_BYTES = 2 * 1024 * 1024
MAX_JSONL_LINES = 1200
# The activity feed stays small, while graph reconstruction gets a larger
# explicit durable-history window so browser/server restarts do not erase the
# already recorded mission shape.
MAX_GRAPH_HISTORY_BYTES = 64 * 1024 * 1024
MAX_GRAPH_HISTORY_LINES = 50_000
MAX_NODES = 500
MAX_EDGES = 1200
MAX_TRACE = 1200
MAX_REQUIREMENTS = 256
# Retained as a compatibility name for callers that imported the old setting;
# accepted business provenance and identity domains are not truncated by a
# projection-layer default.  A bounded operation must opt in at its call site.
MAX_SPECIALIST_RECORDS: int | None = None
MAX_DISCOVERED_PLACEHOLDERS = 500
# Run discovery is shared by every read endpoint in one server process.  Keep
# the complete validated result briefly so concurrent/stale tabs do not each
# repeat the full filesystem walk while still surfacing newly durable runs
# within one browser refresh interval.
RECORDS_CACHE_TTL_SECONDS = 2.0
# Product site inventories and individual assets are not capped by the
# projection layer.  Integrity, renderer/format, path-containment, and safe
# serving checks remain mandatory; callers that need a bounded operation can
# pass an explicit limit to the private validators below.
MAX_PRODUCT_SITE_FILES: int | None = None
MAX_PRODUCT_ASSET_BYTES: int | None = None
# The incremental product hand-off is a separate, immutable preview contract.
# Keep its schema/version vocabulary here rather than importing the assembler
# (the Control Center must remain a passive adapter with no renderer/runtime
# dependency).
PREVIEW_SCHEMA_VERSION = "dashboard.preview.v1"
BLUEPRINT_V2_SCHEMA = "dashboard.business_presentation_plan.v2"
BLUEPRINT_KIND = "dashboard_blueprint"
PREVIEW_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "generation_id",
        "finalizable",
        "input_fingerprint",
        "item_ids",
        "item_bindings",
        "assembly_receipt_ref",
        "assembly_receipt_sha256",
        "blueprint_ref",
        "blueprint_sha256",
        "site_manifest_ref",
        "site_manifest_sha256",
        "site_ref",
        "site_tree_sha256",
    }
)
# Product manifests and their site inventories are immutable once published.
# Keep a small process-local validation cache keyed by the manifest identity;
# every cached member still gets a physical-stat check and product requests
# re-hash the one requested asset before returning it.  This avoids hashing an
# entire site on every projection while preserving same-size/same-mtime
# tamper detection through ctime/inode changes.
_PRODUCT_SITE_CACHE_LIMIT = 128
_PRODUCT_SITE_CACHE_LOCK = threading.RLock()
_PRODUCT_SITE_VALIDATION_CACHE: dict[
    tuple[str, str, str],
    tuple[Mapping[str, Any], dict[str, str], dict[str, tuple[int, int, int, int, int]]],
] = {}
TERMINAL_STATUSES = frozenset({"completed", "failed"})
PLACEHOLDER_TERMINAL_STATUSES = frozenset({"failed", "completed", "cancelled"})
PLACEHOLDER_STAGES = {
    "prepared": "launch_prepared",
    "starting": "semantic_intake",
    "accepted": "planner_starting",
    "running": "planner_starting",
    "queued": "pending_data_refresh",
    "failed": "launch_failed",
    "completed": "complete",
    "cancelled": "cancelled",
}
# The lifecycle stream is an optional host-side signal.  Keep the accepted
# family deliberately small, but do not maintain a role inventory: arbitrary
# safe role identifiers are rendered as-is and missing roles become neutral.
LIFECYCLE_TYPES = frozenset(
    {
        "spawn",
        "start",
        "started",
        "progress",
        "wait",
        "waiting",
        "complete",
        "completed",
        "finish",
        "finished",
        "fail",
        "failed",
        "agent_spawn",
        "agent_start",
        "agent_started",
        "agent_progress",
        "agent_wait",
        "agent_waiting",
        "agent_complete",
        "agent_completed",
        "agent_finish",
        "agent_finished",
        "agent_fail",
        "agent_failed",
        "task_spawn",
        "task_start",
        "task_started",
        "task_progress",
        "task_wait",
        "task_waiting",
        "task_complete",
        "task_completed",
        "task_finish",
        "task_finished",
        "task_fail",
        "task_failed",
        "invocation_spawn",
        "invocation_start",
        "invocation_started",
        "invocation_progress",
        "invocation_wait",
        "invocation_waiting",
        "invocation_complete",
        "invocation_completed",
        "invocation_finish",
        "invocation_finished",
        "invocation_fail",
        "invocation_failed",
    }
)
COORDINATOR_EVENT_TYPES = frozenset(
    {
        "dispatch_started",
        "dispatch_resumed",
        "role_exit",
        "role_completed",
        "role_wait",
        "role_diagnostic",
        "blocked_rethink",
    }
)
UNKNOWN_ROLE = "unknown"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_CURSOR_SHIFT = 64
_CURSOR_MASK = (1 << _CURSOR_SHIFT) - 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_id(value: Any, *, limit: int = 120) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    if not text or len(text) > limit or not SAFE_ID.fullmatch(text):
        return None
    return text


def _safe_label(value: Any, fallback: str, *, limit: int = 120) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.replace("\x00", "").split())
    if not text or len(text) > limit:
        return fallback
    # Labels are identifiers/roles only.  Reject anything that resembles a
    # free-form sentence, URL, or serialized payload.
    if any(char in text for char in ("{", "}", "[", "]", "http://", "https://")):
        return fallback
    return text


def _is_within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        base = root.resolve(strict=False)
        return resolved == base or base in resolved.parents
    except OSError:
        return False


def _has_symlink_component(path: Path) -> bool:
    """Return true when any existing component of ``path`` is a symlink."""

    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            if current.is_symlink():
                resolved = current.resolve(strict=False)
                # macOS exposes /tmp and /var as root-level aliases into
                # /private.  Existing run-root validation treats these as
                # canonical filesystem aliases rather than unsafe links.
                if current.parent == Path("/") and resolved == Path("/private") / current.name:
                    continue
                return True
        except OSError:
            return True
    return False


def _validated_run_root(raw_root: Any, state_path: Path, roots: Iterable[Path]) -> Path | None:
    """Resolve a persisted authoritative run root inside configured roots."""

    if isinstance(raw_root, str) and raw_root:
        raw_path = Path(raw_root).expanduser()
        if not raw_path.is_absolute():
            return None
        candidate = raw_path.resolve(strict=False)
    else:
        candidate = state_path.parent.resolve(strict=False)
        raw_path = candidate
    if not candidate.is_dir() or candidate.is_symlink():
        return None
    if not any(_is_within(candidate, root) for root in roots):
        return None
    # Reject a symlink at/below the configured run root while tolerating the
    # macOS /var -> /private/var ancestor alias.
    for root in roots:
        if not _is_within(candidate, root):
            continue
        current = Path(os.path.abspath(raw_path))
        configured = root.resolve(strict=False)
        while True:
            if current.is_symlink():
                resolved_current = current.resolve(strict=False)
                if not (configured == resolved_current or resolved_current in configured.parents):
                    return None
            if current == raw_path.anchor or current.parent == current or current == configured:
                break
            current = current.parent
        break
    return candidate


def _safe_file(root: Path, relative: str, *, max_bytes: int | None = MAX_PROJECTION_BYTES) -> Path | None:
    candidate = root / relative
    if not _is_within(candidate, root):
        return None
    current = root
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return None
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return None
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        if max_bytes is not None and candidate.stat().st_size > max_bytes:
            return None
    except OSError:
        return None
    return candidate


def _safe_stream_file(root: Path, relative: str) -> Path | None:
    """Validate an in-root JSONL stream without imposing a whole-file size cap.

    Invocation logs can grow beyond the bounded projection window.  Callers
    must still use `_tail_jsonl` or `_jsonl_page`, so no unbounded bytes enter
    the projection.
    """

    candidate = root / relative
    if not _is_within(candidate, root):
        return None
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return None
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return None
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        candidate.stat()
    except OSError:
        return None
    return candidate


def _read_json(root: Path, relative: str, *, max_bytes: int | None = MAX_PROJECTION_BYTES) -> dict[str, Any] | None:
    path = _safe_file(root, relative, max_bytes=max_bytes)
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_launch_draft_fingerprint(draft: Mapping[str, Any]) -> bool:
    """Recompute the launch fingerprint using LaunchManager's canonical path.

    ``LaunchManager._load_draft`` is the authority for the persisted draft
    schema: it removes only the transport/status fields and hashes the exact
    remaining mapping with the manager's canonical JSON helper.  Reuse that
    helper here so a browser projection cannot trust a merely non-empty or
    status-matching fingerprint from a tampered draft.
    """

    stored = draft.get("fingerprint")
    if not isinstance(stored, str) or len(stored) != 64:
        return False
    unsigned = {key: value for key, value in draft.items() if key not in {"fingerprint", "status"}}
    try:
        expected = LaunchManager._fingerprint(unsigned)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        return False
    return secrets.compare_digest(stored, expected)


def _authoritative_lifecycle_state(state: Mapping[str, Any], root: Path, run_id: str) -> bool:
    """Validate the exact minimum state accepted by ``RunLifecycle``.

    The generic Control Center repository intentionally tolerates legacy or
    partially written state files for read-only display.  A file may replace a
    launch placeholder only after the core lifecycle validator accepts its
    complete identity, status, item portfolio, generation, timestamps, and
    manifest hash.  Calling the existing validator avoids maintaining a
    divergent copy of that contract and performs no filesystem writes.
    """

    try:
        from auto_foundry_core.lifecycle import RunLifecycle
        from auto_foundry_core.workspace import RunContext

        context = RunContext(run_id=run_id, run_root=root)
        # Constructing a lifecycle is the core's read-only validation path;
        # unlike ``RunLifecycle.load`` it does not acquire a lock or create a
        # run directory while the repository is projecting state.
        RunLifecycle(context, state)
    except Exception:
        return False
    return True


def _bounded_tail_jsonl(path: Path, *, max_bytes: int, max_items: int) -> list[tuple[int, dict[str, Any]]]:
    """Read a bounded latest JSONL window without an unbounded line read."""

    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(min(max_bytes, size - start))
    except OSError:
        return []
    if start:
        first_newline = data.find(b"\n")
        if first_newline < 0:
            return []
        base = start + first_newline + 1
        data = data[first_newline + 1 :]
    else:
        base = 0
    # If the bounded chunk ended before EOF, discard its partial final line;
    # the next snapshot will pick it up once a newline is durable.
    if start + min(max_bytes, size - start) < size and not data.endswith(b"\n"):
        last_newline = data.rfind(b"\n")
        if last_newline < 0:
            return []
        data = data[: last_newline + 1]
    result: list[tuple[int, dict[str, Any]]] = []
    offset = 0
    for raw_line in data.splitlines(keepends=True):
        offset += len(raw_line)
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result.append((base + offset, value))
    return result[-max_items:]


def _read_jsonl(root: Path, relative: str, *, max_lines: int = MAX_JSONL_LINES) -> list[tuple[int, dict[str, Any]]]:
    path = _safe_stream_file(root, relative)
    if path is None:
        return []
    try:
        # Snapshot projection is a bounded tail, not a prefix.  The reader
        # discards a partial first/last line and never reads beyond the byte
        # window, while preserving stable line-end cursors.
        page = _bounded_tail_jsonl(path, max_bytes=MAX_PROJECTION_BYTES, max_items=max_lines)
    except (OSError, ValueError):
        return []
    return [(cursor, value) for cursor, value in page if isinstance(value, dict)]


def _read_graph_history(root: Path, relative: str) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
    """Read a bounded durable history window for graph reconstruction."""

    path = _safe_stream_file(root, relative)
    if path is None:
        return [], False
    try:
        size = path.stat().st_size
        page = _bounded_tail_jsonl(
            path,
            max_bytes=MAX_GRAPH_HISTORY_BYTES,
            max_items=MAX_GRAPH_HISTORY_LINES,
        )
    except (OSError, ValueError):
        return [], False
    truncated = size > MAX_GRAPH_HISTORY_BYTES or len(page) >= MAX_GRAPH_HISTORY_LINES
    return ([(cursor, value) for cursor, value in page if isinstance(value, dict)], truncated)


def _launch_manifest(root: Path) -> dict[str, Any] | None:
    """Read the newest safe operational launch manifest for display only."""

    direct = _read_json(root, "control_center/launch_manifest.json")
    if direct is not None:
        return direct
    launches = root / "control_center" / "launches"
    if launches.is_symlink() or not launches.is_dir() or not _is_within(launches, root):
        return None
    try:
        # Launch directory names are opaque ids.  Read every direct child
        # before applying the bounded manifest window so an alphabetically
        # late launch cannot hide a newer durable creation/update timestamp.
        children = [child for child in launches.iterdir() if child.is_dir() and not child.is_symlink()]
    except OSError:
        return None
    manifests: list[tuple[dict[str, Any], str]] = []
    for child in children:
        relative = f"control_center/launches/{child.name}/launch_manifest.json"
        value = _read_json(root, relative)
        if value is not None:
            manifests.append((value, child.name))
    if not manifests:
        return None
    manifests.sort(
        key=lambda entry: (
            _timestamp_order(
                _latest_timestamp(
                    entry[0].get(name)
                    for name in ("updatedAt", "updated_at", "createdAt", "created_at")
                )
            ),
            entry[1],
        ),
        reverse=True,
    )
    return manifests[0][0]


def _data_revision_run_id(root: Path, values: Iterable[Mapping[str, Any] | None]) -> str | None:
    """Resolve one run identity for read-only DataRevisionStore validation."""

    identities: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        raw = value.get("run_id")
        if raw is None:
            continue
        safe = _safe_id(raw)
        if safe is None or safe != raw:
            raise ValueError("data revision run identity is invalid")
        identities.add(safe)
    if len(identities) > 1:
        raise ValueError("data revision sidecars disagree on run identity")
    return next(iter(identities), None)


def _data_revision_store_for_projection(root: Path, values: Iterable[Mapping[str, Any] | None]):
    """Construct a store without invoking any root-creating API."""

    from auto_foundry_core.data_revisions import DataRevisionStore
    from auto_foundry_core.workspace import RunContext

    run_id = _data_revision_run_id(root, values)
    if run_id is None:
        return None
    return DataRevisionStore(RunContext(run_id, root))


def _data_revision_projection(root: Path) -> dict[str, Any] | None:
    """Read canonical D revision identities without mutating the run root.

    ``DataRevisionStore.current()``, ``pending_data_refresh()`` and
    ``revision_transaction()`` intentionally ensure their directory roots for
    writers.  The Control Center is strictly read-only, so it uses the
    store's private parsing/validation methods directly after proving every
    sidecar path is local and non-symlinked.  Those methods validate the same
    pointer, manifest, archive and transaction contracts without creating
    directories or acquiring a run lock.
    """

    pointer_lexical = root / "data_room/current_revision.json"
    pointer_path = _safe_file(root, "data_room/current_revision.json")
    pointer = _read_json(root, "data_room/current_revision.json")
    pending_lexical = root / "data_room/pending_data_refresh.json"
    pending_path = _safe_file(root, "data_room/pending_data_refresh.json")
    pending_value = _read_json(root, "data_room/pending_data_refresh.json")
    transaction_lexical = root / "data_room/revision_transaction.json"
    transaction_path = _safe_file(root, "data_room/revision_transaction.json")
    transaction_value = _read_json(root, "data_room/revision_transaction.json")
    run_state = _read_json(root, "run_state.json")
    limitations: list[str] = []
    sidecars = (pointer, pending_value, transaction_value, run_state)
    try:
        store = _data_revision_store_for_projection(root, sidecars)
    except Exception:
        store = None
        limitations.append("Data revision sidecar run identity is invalid.")

    current: dict[str, Any] | None = None
    pointer_hash = _sha256_file(pointer_path) if pointer_path is not None else None
    pointer_invalid = pointer_path is None and (pointer_lexical.exists() or pointer_lexical.is_symlink())
    if pointer_invalid:
        limitations.append("Current data revision pointer is missing, unsafe, or non-canonical.")
    elif pointer_path is not None:
        if store is None:
            pointer_invalid = True
            limitations.append("Current data revision pointer cannot be bound to this run.")
        else:
            try:
                revision_id, manifest_hash = store._read_pointer()  # noqa: SLF001 - read-only canonical validator
                revision = store._read_revision(  # noqa: SLF001 - read-only canonical validator
                    revision_id,
                    record_instrumentation=False,
                )
                if revision.manifest_hash != manifest_hash:
                    raise ValueError("current pointer manifest hash does not match revision")
                current = {
                    "revisionId": revision.revision_id,
                    "manifestHash": revision.manifest_hash,
                    "archiveSha256": revision.archive_sha256,
                    "archiveSizeBytes": revision.archive_size_bytes,
                    "manifestRef": store._canonical_manifest_path(revision.revision_id),  # noqa: SLF001
                    "pointerRef": "data_room/current_revision.json",
                    "pointerHash": pointer_hash,
                }
            except Exception:
                pointer_invalid = True
                limitations.append("Current data revision pointer or its immutable revision failed strict validation.")

    pending: dict[str, Any] | None = None
    pending_pointer_hash = _sha256_file(pending_path) if pending_path is not None else None
    pending_invalid = pending_path is None and (pending_lexical.exists() or pending_lexical.is_symlink())
    if pending_invalid:
        limitations.append("Pending data refresh is missing, unsafe, or non-canonical.")
    elif pending_path is not None:
        if store is None:
            pending_invalid = True
            limitations.append("Pending data refresh cannot be bound to this run.")
        else:
            try:
                # ``RunContext`` canonicalises /var to /private/var on macOS;
                # pass the resolved safe path so the store's containment check
                # compares equivalent roots without weakening symlink guards.
                pending_record = store._read_pending_file(pending_path.resolve(strict=False))  # noqa: SLF001 - read-only canonical validator
                pending = {
                    "revisionId": pending_record.data_revision_id,
                    "manifestHash": pending_record.data_revision_manifest_hash,
                    "archiveSha256": pending_record.data_revision_archive_sha256,
                    "reopenedItemIds": list(pending_record.reopened_item_ids),
                    "draftId": pending_record.launch_draft_id,
                    "intentHash": pending_record.intent_hash,
                    "pointerRef": "data_room/pending_data_refresh.json",
                    "pointerHash": pending_pointer_hash,
                }
                if current is not None and any(
                    pending.get(key) != current.get(current_key)
                    for key, current_key in (
                        ("revisionId", "revisionId"),
                        ("manifestHash", "manifestHash"),
                        ("archiveSha256", "archiveSha256"),
                    )
                ):
                    pending = None
            except Exception:
                pending_invalid = True
                limitations.append("Pending data refresh failed strict validation.")

    pending_revision: dict[str, Any] | None = None
    transaction_invalid = transaction_path is None and (transaction_lexical.exists() or transaction_lexical.is_symlink())
    if transaction_invalid:
        limitations.append("Data revision transaction is missing, unsafe, or non-canonical.")
    elif transaction_path is not None:
        if store is None:
            transaction_invalid = True
            limitations.append("Data revision transaction cannot be bound to this run.")
        else:
            try:
                transaction = store._read_revision_transaction()  # noqa: SLF001 - read-only canonical validator
                if transaction is not None:
                    pending_revision = {
                        "revisionId": transaction.revision_id,
                        "manifestHash": transaction.revision_manifest_hash,
                        "archiveSha256": transaction.revision_archive_sha256,
                        "draftId": transaction.launch_draft_id,
                        "transactionHash": transaction.transaction_hash,
                        "transactionRef": "data_room/revision_transaction.json",
                    }
                    if current is not None and all(
                        pending_revision.get(key) == current.get(current_key)
                        for key, current_key in (
                            ("revisionId", "revisionId"),
                            ("manifestHash", "manifestHash"),
                            ("archiveSha256", "archiveSha256"),
                        )
                    ):
                        pending_revision["state"] = "revision_pending_admission"
                    else:
                        pending_revision["state"] = "revision_recovery"
            except Exception:
                transaction_invalid = True
                limitations.append("Data revision transaction failed strict validation.")

    if current is None and pending is None and pending_revision is None and not (pointer_invalid or pending_invalid or transaction_invalid):
        return None
    result: dict[str, Any] = {}
    if current is not None:
        result["current"] = current
    if pending is not None:
        result["pending"] = pending
        result["state"] = "pending_next_safe_scheduler_boundary"
    if pending_revision is not None:
        result["pendingRevision"] = pending_revision
        if pending is None:
            result["state"] = pending_revision.get("state", "revision_recovery")
    if pointer_invalid or pending_invalid or transaction_invalid:
        result["state"] = "recovery"
    else:
        result.setdefault("state", "active")
    if limitations:
        result["limitations"] = list(dict.fromkeys(limitations))
    return result


def _safe_bound_file(root: Path, value: Any) -> Path | None:
    """Resolve an in-run immutable binding without imposing JSON size caps."""

    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    try:
        normalized = Path(value).as_posix()
    except (TypeError, ValueError):
        return None
    if normalized != value or normalized.startswith("../") or "/../" in normalized:
        return None
    candidate = root / normalized
    if not _is_within(candidate, root) or candidate.is_symlink() or not candidate.is_file():
        return None
    current = root
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    return candidate


def _data_revision_signature_identity(root: Path) -> tuple[Any, ...] | None:
    """Return a cheap invalidation identity for immutable D revisions.

    Full archive verification still runs on a cache miss.  UI polling uses the
    declared digest plus filesystem identity (including ctime) so it does not
    hash a potentially large archive simply to check whether cached metadata
    changed.
    """

    pointer_path = _safe_file(root, "data_room/current_revision.json")
    pointer = _read_json(root, "data_room/current_revision.json")
    if pointer_path is None or not isinstance(pointer, Mapping):
        return None
    manifest_ref = pointer.get("manifest_path")
    manifest_path = _safe_bound_file(root, manifest_ref)
    if manifest_path is None:
        return ("pointer", _sha256_file(pointer_path), None)
    manifest = _read_json(root, manifest_ref)
    manifest_stat = None
    manifest_hash = _sha256_file(manifest_path)
    try:
        stat = manifest_path.stat()
        manifest_stat = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        pass
    archive_path = _safe_bound_file(root, manifest.get("archive_path") if isinstance(manifest, Mapping) else None)
    archive_stat = None
    if archive_path is not None:
        try:
            stat = archive_path.stat()
            archive_stat = (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
        except OSError:
            archive_stat = None
    return (
        "data_revision",
        pointer.get("pointer_hash"),
        _sha256_file(pointer_path),
        pointer.get("manifest_hash"),
        manifest_stat,
        manifest_hash,
        manifest.get("archive_sha256") if isinstance(manifest, Mapping) else None,
        archive_stat,
    )


def _data_revision_projection_signature(root: Path) -> tuple[Any, ...]:
    """Bind the UI cache to small sidecars and immutable file identities."""

    values: list[Any] = [_data_revision_signature_identity(root)]
    for relative in (
        "run_state.json",
        "data_room/current_revision.json",
        "data_room/pending_data_refresh.json",
        "data_room/revision_transaction.json",
    ):
        path = _safe_file(root, relative)
        if path is None:
            values.append(None)
            continue
        try:
            stat = path.stat()
            values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except OSError:
            values.append(None)
    return tuple(values)


def _attach_data_revision(summary: dict[str, Any], projection: Mapping[str, Any] | None) -> None:
    """Add only pointer/pending identities to a run summary."""

    # A cached run record may outlive a removed or invalidated revision
    # sidecar.  Clear the prior projection before applying the refreshed
    # result so a failed revalidation cannot leave stale UI metadata behind.
    summary.pop("dataRevision", None)
    summary.pop("dataRevisionStatus", None)
    summary.pop("pendingDataRefresh", None)
    summary.pop("pendingDataRevision", None)
    if not projection:
        return
    current = projection.get("current")
    if isinstance(current, Mapping):
        summary["dataRevision"] = dict(current)
    pending = projection.get("pending")
    summary["dataRevisionStatus"] = projection.get("state", "active")
    if isinstance(pending, Mapping):
        summary["pendingDataRefresh"] = dict(pending)
    else:
        summary.pop("pendingDataRefresh", None)
    pending_revision = projection.get("pendingRevision")
    if isinstance(pending_revision, Mapping):
        summary["pendingDataRevision"] = dict(pending_revision)
    else:
        summary.pop("pendingDataRevision", None)


def _safe_reference(
    root: Path,
    value: Any,
    *,
    prefix: str | None = None,
    allow_directory: bool = False,
    max_bytes: int | None = MAX_PROJECTION_BYTES,
) -> tuple[str, Path] | None:
    """Resolve one persisted relative reference without following links.

    Operational projections expose references only after lexical and physical
    containment checks.  The returned string is always the canonical POSIX
    relative value persisted by the producer; absolute/workspace paths and
    symlink components are rejected before any bytes are read.
    """

    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    try:
        normalized = Path(value).as_posix()
    except (TypeError, ValueError):
        return None
    if normalized != value or normalized in {".", ""} or normalized.startswith("../") or "/../" in normalized:
        return None
    if prefix is not None and not normalized.startswith(prefix):
        return None
    path = _safe_file(root, normalized, max_bytes=max_bytes)
    if path is None and allow_directory:
        candidate = root / normalized
        if not _is_within(candidate, root) or candidate.is_symlink() or not candidate.is_dir():
            return None
        current = root
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            return None
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                return None
        path = candidate
    if path is None:
        return None
    return normalized, path


def _sha256_file(path: Path) -> str | None:
    """Hash a regular file incrementally without buffering it wholesale."""

    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _file_fingerprint(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return a cheap physical identity for an already validated file.

    ``st_ctime_ns`` is intentionally included: a caller can restore mtime
    after an in-place rewrite, but cannot normally forge the filesystem change
    time.  Replacements are covered by the inode/device pair.  This metadata
    is only a cache key; requested product bytes are always hash-checked.
    """

    try:
        stat = path.stat()
        return (
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
        )
    except OSError:
        return None


def _read_verified_product_asset(
    path: Path,
    expected_hash: str,
    *,
    max_bytes: int | None = None,
) -> bytes | None:
    """Read one product asset while verifying its manifest digest.

    The default path has no arbitrary byte ceiling.  ``max_bytes`` is an
    explicit caller opt-in for bounded consumers and is enforced while
    streaming the validated regular file.
    """

    if max_bytes is not None and (isinstance(max_bytes, bool) or max_bytes < 0):
        raise ValueError("max_bytes cannot be negative")

    digest = hashlib.sha256()
    payload = bytearray()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if max_bytes is not None and len(payload) + len(chunk) > max_bytes:
                    return None
                payload.extend(chunk)
                digest.update(chunk)
    except OSError:
        return None
    if digest.hexdigest() != expected_hash:
        return None
    return bytes(payload)


def _safe_hash(value: Any) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        return None
    return value.lower()


def _run_id_for_projection(root: Path, summary: Mapping[str, Any]) -> str | None:
    """Read the run identity needed by strict sidecar validators."""

    value = summary.get("authoritativeRunId") or summary.get("runId")
    safe = _safe_id(value)
    if safe is not None:
        return safe
    state = _read_json(root, "run_state.json")
    if isinstance(state, Mapping):
        safe = _safe_id(state.get("run_id") or state.get("runId"))
        if safe is not None:
            return safe
    # Generation states are authoritative when a run has been extended.
    pointer = _read_json(root, "active_generation.json")
    if isinstance(pointer, Mapping):
        safe = _safe_id(pointer.get("run_id") or pointer.get("runId"))
        if safe is not None:
            return safe
    return None


def _generation_id_for_projection(root: Path, summary: Mapping[str, Any]) -> str | None:
    pointer = _read_json(root, "active_generation.json")
    if isinstance(pointer, Mapping):
        value = _safe_id(pointer.get("generation_id") or pointer.get("generationId"))
        if value is not None and re.fullmatch(r"G-[0-9]{4}", value):
            return value
    value = _safe_id(summary.get("generationId"))
    if value is not None and re.fullmatch(r"G-[0-9]{4}", value):
        return value
    state = _read_json(root, "run_state.json")
    if isinstance(state, Mapping):
        raw_generation = state.get("generation")
        if isinstance(raw_generation, int) and not isinstance(raw_generation, bool) and raw_generation > 0:
            return f"G-{raw_generation:04d}"
        # RunLifecycle's initial requirement generation is named G-0001 even
        # before an active_generation pointer exists.  This is a public
        # lifecycle identity, not a role/domain inference.
        if state.get("mode") == "requirement":
            return "G-0001"
    return None


def _run_mode_for_projection(root: Path, summary: Mapping[str, Any]) -> str | None:
    mode = summary.get("mode")
    if mode in {"question", "requirement"}:
        return mode
    state = _read_json(root, "run_state.json")
    if isinstance(state, Mapping) and state.get("mode") in {"question", "requirement"}:
        return str(state.get("mode"))
    return None


def _mission_context_projection(root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Project validated MissionContext/MissionPlan/catalog metadata only."""

    result: dict[str, Any] = {
        "available": False,
        "contextHash": None,
        "planHash": None,
        "documentCatalogHash": None,
        "missionIntent": None,
        "mode": _run_mode_for_projection(root, summary),
        "generationId": _generation_id_for_projection(root, summary),
        "itemIds": [],
        "requirementIds": [],
        "refs": {},
        "documents": [],
        "limitations": [],
    }
    pointer_path = _safe_file(root, "control_center/mission_context_active.json")
    if pointer_path is None:
        result["limitations"].append("Mission-context pointer is missing or unsafe.")
        return result
    pointer = _read_json(root, "control_center/mission_context_active.json")
    if not isinstance(pointer, Mapping) or pointer.get("schemaVersion") != 1 or pointer.get("kind") != "active_mission_context_pointer":
        result["limitations"].append("Mission-context pointer failed strict validation.")
        return result
    run_id = _run_id_for_projection(root, summary)
    pointer_run_id = _safe_id(pointer.get("runId"))
    if run_id is None or pointer_run_id is None or pointer_run_id != run_id:
        result["limitations"].append("Mission-context pointer run lineage is unavailable.")
        return result

    context_binding = _safe_reference(root, pointer.get("missionContextRef"), prefix="control_center/")
    plan_binding = _safe_reference(root, pointer.get("missionPlanRef"), prefix="control_center/")
    if context_binding is None or plan_binding is None:
        result["limitations"].append("Mission-context sidecar references are missing or unsafe.")
        return result
    context_ref, context_path = context_binding
    plan_ref, plan_path = plan_binding
    context_wrapper = _read_json(root, context_ref)
    plan_wrapper = _read_json(root, plan_ref)
    try:
        from auto_foundry_core.document_ingestion import DocumentCatalog
        from auto_foundry_core.mission_context import MissionContext, MissionPlan, sha256_value, validate_mission_context_catalog

        if not isinstance(context_wrapper, Mapping) or context_wrapper.get("kind") != "mission_context":
            raise ValueError("context wrapper")
        context = MissionContext.from_dict(context_wrapper.get("context"))
        context_hash = _safe_hash(pointer.get("missionContextHash"))
        if context_hash is None or context_hash != context.context_hash or context_wrapper.get("contextHash") != context.context_hash:
            raise ValueError("context hash")
        if not isinstance(plan_wrapper, Mapping) or plan_wrapper.get("kind") != "mission_plan":
            raise ValueError("plan wrapper")
        plan = MissionPlan.from_dict(plan_wrapper.get("missionPlan"))
        plan_hash = _safe_hash(pointer.get("missionPlanHash"))
        if plan_hash is None or plan_hash != plan.plan_hash or plan_wrapper.get("planHash") != plan.plan_hash or plan.context_hash != context.context_hash:
            raise ValueError("plan hash")
        if plan_wrapper.get("contextHash") != context.context_hash:
            raise ValueError("plan context hash")

        catalog_ref_value = pointer.get("documentCatalogRef")
        catalog_hash_value = pointer.get("documentCatalogHash")
        catalog: DocumentCatalog | None = None
        catalog_ref: str | None = None
        catalog_hash: str | None = None
        if catalog_ref_value is not None or catalog_hash_value is not None:
            binding = _safe_reference(root, catalog_ref_value, prefix="control_center/")
            catalog_hash = _safe_hash(catalog_hash_value)
            if binding is None or catalog_hash is None:
                raise ValueError("catalog binding")
            catalog_ref, _catalog_path = binding
            catalog_wrapper = _read_json(root, catalog_ref)
            if not isinstance(catalog_wrapper, Mapping) or catalog_wrapper.get("kind") != "mission_document_catalog":
                raise ValueError("catalog wrapper")
            catalog = DocumentCatalog.from_dict(catalog_wrapper.get("catalog"))
            expected_catalog_hash = sha256_value(catalog.to_dict())
            if catalog_hash != expected_catalog_hash or catalog_wrapper.get("catalogHash") != expected_catalog_hash:
                raise ValueError("catalog hash")
            if context.document_catalog is None or sha256_value(context.document_catalog) != expected_catalog_hash:
                raise ValueError("catalog context binding")
            validate_mission_context_catalog(context, catalog.to_dict())
        elif context.document_catalog is not None:
            raise ValueError("catalog pointer")

        result.update(
            {
                "available": True,
                "contextHash": context.context_hash,
                "planHash": plan.plan_hash,
                "documentCatalogHash": catalog_hash,
                "missionIntent": context.mission_intent,
                "itemIds": list(plan.requirement_ids),
                "requirementIds": list(plan.requirement_ids),
                "refs": {
                    "missionContext": context_ref,
                    "missionPlan": plan_ref,
                    **({"documentCatalog": catalog_ref} if catalog_ref is not None else {}),
                },
            }
        )
        if catalog is not None:
            result["documents"] = [
                {
                    "ref": document.document_ref,
                    "hash": document.content_hash,
                    "format": document.format,
                    "sizeBytes": document.size_bytes,
                    "extraction": document.extraction,
                    "sectionCount": len(document.sections),
                }
                for document in catalog.documents
            ][:MAX_REQUIREMENTS]
    except Exception:
        result["limitations"].append("Mission-context sidecars failed strict validation.")
        # Never expose partially validated hashes/refs.
        result.update({"available": False, "contextHash": None, "planHash": None, "documentCatalogHash": None, "refs": {}, "documents": [], "itemIds": [], "requirementIds": []})
    return result


def _role_session_projection(root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Project logical role sessions separately from each invocation action."""

    result: dict[str, Any] = {"available": False, "roleSessions": [], "invocations": [], "edges": [], "limitations": []}
    path = _safe_file(root, "control_plane/role_sessions.json")
    if path is None:
        result["limitations"].append("Role-session registry is missing or unsafe.")
        return result
    run_id = _run_id_for_projection(root, summary)
    raw = _read_json(root, "control_plane/role_sessions.json")
    if run_id is None or not isinstance(raw, Mapping):
        result["limitations"].append("Role-session registry run lineage is unavailable.")
        return result
    try:
        from auto_foundry_core.coordinator import RoleSessionRegistry

        validated = RoleSessionRegistry._validate_document(raw, run_id=run_id)
    except Exception:
        result["limitations"].append("Role-session registry failed strict validation.")
        return result
    sessions: list[dict[str, Any]] = []
    invocations: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for logical_owner, entry in sorted(validated.get("sessions", {}).items()):
        owner_id = _safe_id(logical_owner)
        role = _safe_id(entry.get("role"))
        subject = _safe_id(entry.get("subject_id"))
        session_id = _safe_id(entry.get("session_id"))
        if owner_id is None or role is None or subject is None:
            result["limitations"].append("A role-session entry contains an unsafe identity and was omitted.")
            continue
        session_node_id = f"role-session:{owner_id}"
        session: dict[str, Any] = {
            "id": session_node_id,
            "logicalOwner": owner_id,
            "role": role,
            "subjectId": subject,
            "sessionId": session_id,
            "status": entry.get("status"),
            "replacementRequired": bool(entry.get("replacement_required")),
            "generationId": _safe_id(entry.get("generation_id")),
            "lastAction": _safe_id(entry.get("last_action")),
            "lastInvocationId": _safe_id(entry.get("last_idempotency_key")),
        }
        replacement_of = _safe_id(entry.get("replacement_of"))
        if replacement_of is not None:
            session["replacementOf"] = replacement_of
        sessions.append(session)
        for lineage in entry.get("action_lineage", ()):
            if not isinstance(lineage, Mapping):
                continue
            invocation_id = _safe_id(lineage.get("idempotency_key"))
            action = _safe_id(lineage.get("action"))
            invocation_subject = _safe_id(lineage.get("subject_id"))
            at = _timestamp(lineage.get("at"))
            if invocation_id is None or action is None or invocation_subject is None:
                result["limitations"].append("An invocation lineage entry contains an unsafe identity and was omitted.")
                continue
            invocation_node_id = f"invocation:{invocation_id}"
            invocation = {
                "id": invocation_node_id,
                "invocationId": invocation_id,
                "sessionId": session_id,
                "logicalOwner": owner_id,
                "role": role,
                "action": action,
                "subjectId": invocation_subject,
            }
            if at is not None:
                invocation["at"] = at
            invocations.append(invocation)
            edges.append({"source": session_node_id, "target": invocation_node_id, "kind": "invokes", "label": "invokes"})
            edges.append({"source": invocation_node_id, "target": invocation_subject, "kind": "subject", "label": "subject"})
    result.update({"available": True, "roleSessions": sessions, "invocations": invocations, "edges": edges})
    return result


def _identity_domain_projection(root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Project strict entity-resolution domains and explicit lineage links."""

    result: dict[str, Any] = {"available": False, "domains": [], "edges": [], "limitations": []}
    path = _safe_file(root, "entity_resolution/state.json")
    if path is None:
        result["limitations"].append("Identity-domain state is missing or unsafe.")
        return result
    raw = _read_json(root, "entity_resolution/state.json")
    run_id = _run_id_for_projection(root, summary)
    if run_id is None or not isinstance(raw, Mapping):
        result["limitations"].append("Identity-domain state run lineage is unavailable.")
        return result
    domains_root = root / "entity_resolution" / "domains"
    commits_root = root / "entity_resolution" / "committed"
    if domains_root.is_symlink() or commits_root.is_symlink() or not domains_root.is_dir() or not commits_root.is_dir():
        result["limitations"].append("Identity-domain artifact directories are missing or unsafe.")
        return result
    try:
        from auto_foundry_core.entity_resolution import EntityResolutionWorkspace
        from auto_foundry_core.workspace import RunContext

        context = RunContext(run_id=run_id, run_root=root)
        # The strict parser is intentionally used without ``load``: load takes
        # a writer lock and may reconcile/persist crash state.  Validation plus
        # the public ``domains`` accessor gives the same typed boundaries while
        # keeping this HTTP projection read-only.
        EntityResolutionWorkspace._validate_state(context, raw)
        workspace = EntityResolutionWorkspace(context, raw)
        domains = workspace.domains()
    except Exception:
        result["limitations"].append("Identity-domain state failed strict validation.")
        return result
    edges: list[dict[str, Any]] = []
    projected: list[dict[str, Any]] = []
    for domain in domains:
        domain_id = _safe_id(domain.domain_id)
        if domain_id is None:
            result["limitations"].append("An identity-domain entry contains an unsafe identifier and was omitted.")
            continue
        node_id = f"identity-domain:{domain_id}"
        identity: dict[str, Any] = {
            "id": node_id,
            "domainId": domain_id,
            "canonicalIdentity": _safe_label(domain.canonical_identity, domain_id),
            "objectType": _safe_label(domain.object_type, "identity"),
            "state": domain.state,
            "ownerRef": _safe_id(domain.resolution_owner),
            "reviewerRef": _safe_id(domain.reviewer_ref),
            "reviewVerdict": _safe_label(domain.review_verdict, "") if domain.review_verdict is not None else None,
            "requestedBy": [value for value in (_safe_id(item) for item in domain.requesters) if value is not None][:MAX_REQUIREMENTS],
            "scopeHash": _safe_hash(domain.scope_hash),
            "resultHash": _safe_hash(domain.result_hash),
            "resultScopeHash": _safe_hash(domain.result_scope_hash),
            "commitManifestHash": _safe_hash(domain.commit_manifest_hash),
            "acceptedPendingCommit": bool(domain.accepted_pending_commit),
            "revision": domain.revision,
            "publishedRevision": domain.published_revision,
            "publishedScopeHash": _safe_hash(domain.published_scope_hash),
            "generationId": _safe_id(domain.generation_id),
            "dataRevisionId": _safe_id(domain.data_revision_id),
        }
        projected.append(identity)
        for requirement_id in identity["requestedBy"]:
            edges.append({"source": requirement_id, "target": node_id, "kind": "requests", "label": "requests"})
        reviewer = identity.get("reviewerRef")
        if reviewer:
            reviewer_node = f"reviewer:{reviewer}"
            edges.append({"source": reviewer_node, "target": node_id, "kind": "reviews", "label": "reviews"})
        owner = identity.get("ownerRef")
        if owner:
            owner_node = f"identity-owner:{owner}"
            edges.append({"source": owner_node, "target": node_id, "kind": "owns", "label": "owns"})
    result.update({"available": True, "domains": projected, "edges": edges})
    return result


def _validated_analytical_artifact_descriptors(
    root: Path,
    receipt: Mapping[str, Any],
    fixture: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate Lane A's typed analytical-artifact provenance envelope.

    The dashboard assembler's ``analytical_artifacts`` list is the only
    supported product provenance schema.  Receipt and fixture lists must be
    present and byte-for-byte equal; each descriptor is then bound to the
    immutable committed ``AnalyticalArtifact`` JSON under its requirement
    namespace.  This keeps the Control Center a metadata-only reader while
    ensuring a forged descriptor cannot make a link appear valid.
    """

    receipt_values = receipt.get("analytical_artifacts")
    fixture_values = fixture.get("analytical_artifacts")
    if not isinstance(receipt_values, list) or not isinstance(fixture_values, list):
        raise ValueError("analytical artifact provenance list is missing")
    if receipt_values != fixture_values:
        raise ValueError("analytical artifact provenance differs between receipt and fixture")

    from auto_foundry_core.analytical_artifacts import AnalyticalArtifact

    required = {
        "item_id",
        "artifact_id",
        "artifact_type",
        "schema_version",
        "requirement_id",
        "content_hash",
        "envelope_hash",
        "canonical_bytes_sha256",
        "artifact_ref",
        "integration_record_id",
        "integration_record_hash",
    }
    projected: list[dict[str, Any]] = []
    seen_artifacts: set[str] = set()
    seen_refs: set[str] = set()
    for descriptor in receipt_values:
        if not isinstance(descriptor, Mapping) or set(descriptor) != required:
            raise ValueError("analytical artifact provenance descriptor is invalid")
        item_id = _safe_id(descriptor.get("item_id"))
        artifact_id = _safe_id(descriptor.get("artifact_id"))
        artifact_type = _safe_id(descriptor.get("artifact_type"))
        schema_version = _safe_id(descriptor.get("schema_version"))
        requirement_id = _safe_id(descriptor.get("requirement_id"))
        integration_record_id = _safe_id(descriptor.get("integration_record_id"))
        if None in {item_id, artifact_id, artifact_type, schema_version, requirement_id, integration_record_id}:
            raise ValueError("analytical artifact provenance identity is invalid")
        if item_id != requirement_id:
            raise ValueError("analytical artifact provenance requirement binding is invalid")
        content_hash = _safe_hash(descriptor.get("content_hash"))
        envelope_hash = _safe_hash(descriptor.get("envelope_hash"))
        canonical_hash = _safe_hash(descriptor.get("canonical_bytes_sha256"))
        integration_hash = _safe_hash(descriptor.get("integration_record_hash"))
        if None in {content_hash, envelope_hash, canonical_hash, integration_hash}:
            raise ValueError("analytical artifact provenance hash is invalid")
        if artifact_id in seen_artifacts:
            raise ValueError("analytical artifact provenance contains duplicate IDs")
        seen_artifacts.add(artifact_id)

        artifact_ref = descriptor.get("artifact_ref")
        prefix = "integration/committed/artifacts/"
        if (
            not isinstance(artifact_ref, str)
            or not artifact_ref.startswith(prefix)
            or not artifact_ref.endswith(".json")
            or artifact_ref in seen_refs
        ):
            raise ValueError("analytical artifact provenance reference is invalid")
        relative_name = artifact_ref[len(prefix):]
        if not relative_name or "/" in relative_name or "\\" in relative_name or relative_name in {".", ".."}:
            raise ValueError("analytical artifact provenance reference is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", relative_name):
            raise ValueError("analytical artifact provenance reference is invalid")
        seen_refs.add(artifact_ref)
        committed_ref = f"requirements/{item_id}/{artifact_ref}"
        # Committed analytical artifacts are hash-bound product provenance,
        # not operational sidecars.  Do not apply the generic sidecar byte
        # window here; strict typed deserialization and the canonical digest
        # checks below remain the integrity boundary.
        binding = _safe_reference(root, committed_ref, prefix=f"requirements/{item_id}/", max_bytes=None)
        if binding is None:
            raise ValueError("analytical artifact committed binding is missing or unsafe")
        artifact_value = _read_json(root, binding[0], max_bytes=None)
        if artifact_value is None:
            raise ValueError("analytical artifact committed JSON is missing or invalid")
        try:
            artifact = AnalyticalArtifact.from_dict(artifact_value)
        except Exception as exc:
            raise ValueError("analytical artifact committed JSON failed strict validation") from exc
        if (
            artifact.artifact_id != artifact_id
            or artifact.artifact_type != artifact_type
            or artifact.schema_version != schema_version
            or artifact.requirement_id != requirement_id
            or artifact.content_hash != content_hash
            or artifact.envelope_hash != envelope_hash
        ):
            raise ValueError("analytical artifact committed identity or hash binding is stale")
        canonical_bytes = artifact.to_json().encode("utf-8")
        if _sha256_file(binding[1]) != canonical_hash or hashlib.sha256(canonical_bytes).hexdigest() != canonical_hash:
            raise ValueError("analytical artifact committed bytes hash is stale")
        projected.append(
            {
                "itemId": item_id,
                "artifactId": artifact_id,
                "artifactType": artifact_type,
                "schemaVersion": schema_version,
                "requirementId": requirement_id,
                "contentHash": content_hash,
                "envelopeHash": envelope_hash,
                "canonicalBytesSha256": canonical_hash,
                "artifactRef": artifact_ref,
                "integrationRecordId": integration_record_id,
                "integrationRecordHash": integration_hash,
            }
        )
    return projected


def _normalise_site_asset_path(value: Any) -> str | None:
    """Normalize one request/manifest site path without allowing traversal."""

    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    if value.startswith("/"):
        return None
    parts = value.split("/")
    if any(part == ".." for part in parts):
        return None
    normalized = posixpath.normpath(value)
    if normalized in {"", ".", ".."} or normalized.startswith("../") or normalized.startswith("/"):
        return None
    return normalized


def _canonical_json_hash(value: Any) -> str:
    """Hash one JSON value using the assembler's canonical byte contract."""

    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _forbidden_product_reference(value: str) -> bool:
    """Return true for references into raw/work namespaces.

    Product projections may expose only generated/committed artifacts.  A
    segment check avoids rejecting harmless labels such as ``framework`` while
    still closing the common ``.../raw/...`` and ``.../work/...`` escapes.
    """

    segments = [segment.casefold() for segment in value.replace("\\", "/").split("/") if segment]
    return any(segment in {"raw", "work"} for segment in segments)


def _safe_product_reference(
    root: Path,
    value: Any,
    *,
    prefix: str | None = "products/",
    allow_directory: bool = False,
) -> tuple[str, Path] | None:
    """Resolve a generated-product reference without raw/work or link escapes."""

    if not isinstance(value, str) or not value or "\\" in value or _forbidden_product_reference(value):
        return None
    binding = _safe_reference(
        root,
        value,
        prefix=prefix,
        allow_directory=allow_directory,
        max_bytes=None,
    )
    if binding is None or _forbidden_product_reference(binding[0]):
        return None
    return binding


def _tree_inventory(path: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    """Return a deterministic hash inventory for a regular-file tree."""

    if path.is_symlink() or not path.is_dir():
        raise ValueError("product site tree is missing or symlinked")
    excluded = exclude or set()
    inventory: dict[str, str] = {}
    try:
        children = sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix())
    except OSError as exc:
        raise ValueError("product site tree cannot be enumerated") from exc
    for child in children:
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            raise ValueError("product site tree contains a symlink")
        if not child.is_file() or relative in excluded:
            continue
        digest = _sha256_file(child)
        if digest is None:
            raise ValueError("product site tree file cannot be read")
        inventory[relative] = digest
    if not inventory:
        raise ValueError("product site tree contains no files")
    return inventory


def _tree_hash(files: Mapping[str, str]) -> str:
    return _canonical_json_hash(dict(files))


def _hash_bound_reference(
    root: Path,
    reference: Any,
    expected_hash: Any,
    *,
    prefix: str | None = "products/",
    allow_directory: bool = False,
) -> tuple[str, Path, dict[str, str] | None]:
    """Validate one local file/tree reference and return its bytes identity."""

    binding = _safe_product_reference(root, reference, prefix=prefix, allow_directory=allow_directory)
    expected = _safe_hash(expected_hash)
    if binding is None or expected is None:
        raise ValueError("product reference or hash is invalid")
    canonical, path = binding
    if path.is_dir():
        files = _tree_inventory(path)
        if _tree_hash(files) != expected:
            raise ValueError("product tree hash is stale")
        return canonical, path, files
    if _sha256_file(path) != expected:
        raise ValueError("product file hash is stale")
    return canonical, path, None


def _validate_embedded_reference_fields(root: Path, value: Any) -> None:
    """Reject raw/work references and stale ref/hash pairs in nested metadata."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.casefold()
            if key_lower.endswith("_ref") or key_lower in {"ref", "path", "source", "sources"}:
                refs = item if isinstance(item, (list, tuple)) else (item,)
                for raw_ref in refs:
                    if not isinstance(raw_ref, str):
                        continue
                    if _forbidden_product_reference(raw_ref):
                        raise ValueError("raw/work product reference is not allowed")
            _validate_embedded_reference_fields(root, item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_embedded_reference_fields(root, item)


def _validated_blueprint(
    root: Path,
    reference: Any,
    expected_hash: Any,
    *,
    run_id: str,
    generation_id: str,
) -> tuple[str, Mapping[str, Any]]:
    """Validate the exact source-bound dashboard Blueprint v2."""

    # Blueprint source bindings may point at other generated public namespaces
    # (for example ``requirements/`` or ``extensions/``) in addition to the
    # ``products/`` output directory.  Keep the same in-root/symlink/raw-work
    # checks while deliberately not narrowing the namespace here.
    canonical, path, _ = _hash_bound_reference(root, reference, expected_hash, prefix=None)
    blueprint = _read_json(root, canonical, max_bytes=None)
    if not isinstance(blueprint, Mapping):
        raise ValueError("dashboard blueprint is invalid")
    if blueprint.get("schema_version") != BLUEPRINT_V2_SCHEMA or blueprint.get("kind") != BLUEPRINT_KIND:
        raise ValueError("dashboard blueprint schema is not V2")
    if blueprint.get("run_id") != run_id or blueprint.get("generation_id") != generation_id:
        raise ValueError("dashboard blueprint is bound to another run or generation")
    source_bindings = blueprint.get("source_bindings")
    if not isinstance(source_bindings, Mapping):
        raise ValueError("dashboard blueprint source bindings are missing")
    for key, raw_ref in source_bindings.items():
        key_text = str(key)
        if not (key_text.endswith("_ref") or key_text in {"ref", "path"}):
            continue
        if raw_ref in (None, ""):
            continue
        if not isinstance(raw_ref, str):
            raise ValueError("dashboard blueprint source reference is invalid")
        if key_text == "blueprint_ref":
            if raw_ref != canonical:
                raise ValueError("dashboard blueprint self-reference is stale")
            continue
        hash_candidates = (
            key_text[:-4] + "_sha256",
            key_text[:-4] + "_hash",
            "sha256",
            "hash",
        )
        supplied_hash = next((source_bindings.get(candidate) for candidate in hash_candidates if candidate in source_bindings), None)
        if supplied_hash is None:
            raise ValueError("dashboard blueprint source hash is missing")
        _hash_bound_reference(root, raw_ref, supplied_hash, prefix=None, allow_directory=True)
    _validate_embedded_reference_fields(root, blueprint)
    return canonical, blueprint


def _validated_product_site(
    root: Path,
    site_ref: Any,
    expected_manifest_hash: Any,
    *,
    max_files: int | None = None,
    max_asset_bytes: int | None = None,
    expected_tree_hash: Any = None,
    expected_blueprint_ref: str | None = None,
    expected_blueprint_hash: str | None = None,
    expected_run_id: str | None = None,
    expected_generation_id: str | None = None,
    require_runtime: bool = False,
) -> tuple[str, Mapping[str, Any], dict[str, str]]:
    """Validate a generated site's manifest and every declared file binding.

    Site-file count and byte limits are explicit opt-ins.  The normal product
    path remains unbounded by arbitrary ceilings while retaining all physical
    containment, symlink/regular-file, digest, and renderer vocabulary checks.
    """

    for name, value in (("max_files", max_files), ("max_asset_bytes", max_asset_bytes)):
        if value is not None and (isinstance(value, bool) or value < 0):
            raise ValueError(f"{name} cannot be negative")

    binding = _safe_reference(root, site_ref, prefix="products/", allow_directory=True, max_bytes=None)
    expected_hash = _safe_hash(expected_manifest_hash)
    if binding is None or expected_hash is None:
        raise ValueError("product site binding is missing or unsafe")
    site_prefix = binding[0].rstrip("/")
    manifest_ref = f"{site_prefix}/site_manifest.json"
    manifest_binding = _safe_reference(root, manifest_ref, prefix=f"{site_prefix}/", max_bytes=None)
    if manifest_binding is None or _sha256_file(manifest_binding[1]) != expected_hash:
        raise ValueError("product site manifest is missing or stale")
    manifest = _read_json(root, manifest_ref, max_bytes=None)
    if not isinstance(manifest, Mapping):
        raise ValueError("product site manifest is invalid")
    if expected_run_id is not None and manifest.get("run_id") not in {None, expected_run_id}:
        raise ValueError("product site manifest is bound to another run")
    if expected_generation_id is not None and manifest.get("generation_id") not in {None, expected_generation_id}:
        raise ValueError("product site manifest is bound to another generation")
    if expected_blueprint_ref is not None:
        if manifest.get("blueprint_ref") != expected_blueprint_ref:
            raise ValueError("product site Blueprint reference is stale")
        if _safe_hash(manifest.get("blueprint_sha256")) != _safe_hash(expected_blueprint_hash):
            raise ValueError("product site Blueprint hash is stale")
    raw_files = manifest.get("site_file_hashes")
    if not isinstance(raw_files, Mapping) or not raw_files:
        raise ValueError("product site manifest file inventory is missing or invalid")
    if max_files is not None and len(raw_files) > max_files:
        raise ValueError(f"product site manifest exceeds explicit max_files: {len(raw_files)} > {max_files}")
    # The renderer owns a deliberately tiny static vocabulary.  Bind the
    # inventory to the manifest's page/asset declarations and reject active
    # script/object formats even when a tampered manifest supplies a matching
    # self-consistent hash.  This is the canonical renderer boundary; unknown
    # generated files must never become an operational-origin capability.
    declared_paths: set[str] = set()
    for field_name, suffixes in (("pages", {".html"}), ("assets", {".css", ".svg", ".js"})):
        declared = manifest.get(field_name)
        if not isinstance(declared, list) or not declared:
            raise ValueError("product site manifest declarations are missing")
        for raw_ref in declared:
            normalized = _normalise_site_asset_path(raw_ref)
            if normalized is None or normalized != raw_ref or Path(normalized).suffix.lower() not in suffixes:
                raise ValueError("product site manifest declarations are invalid")
            if field_name == "assets" and not normalized.startswith("assets/"):
                raise ValueError("product site asset declaration is outside the assets namespace")
            if Path(normalized).suffix.lower() == ".js" and normalized != "assets/dashboard.js":
                raise ValueError("product site JavaScript declaration is not the canonical dashboard runtime")
            if normalized in declared_paths:
                raise ValueError("product site manifest declarations contain duplicates")
            declared_paths.add(normalized)
    if set(str(value) for value in raw_files) != declared_paths:
        raise ValueError("product site file inventory does not match renderer declarations")

    runtime = manifest.get("runtime")
    javascript_declared = "assets/dashboard.js" in declared_paths
    if runtime is not None and not javascript_declared:
        raise ValueError("product site runtime declaration has no canonical JavaScript asset")
    if javascript_declared or runtime is not None or require_runtime:
        if not isinstance(runtime, Mapping):
            raise ValueError("product site runtime declaration is missing")
        if set(runtime) != {"asset", "deterministic", "network"}:
            raise ValueError("product site runtime declaration is not exact")
        if runtime.get("asset") != "assets/dashboard.js" or runtime.get("deterministic") is not True or runtime.get("network") is not False:
            raise ValueError("product site runtime declaration is not deterministic and offline")
    if require_runtime and not javascript_declared:
        raise ValueError("product site canonical dashboard runtime is missing")

    # A site binding is exact, not merely an allowlist: every regular file
    # beneath the generated site must appear in ``site_file_hashes`` and every
    # declared member must be present physically.  This closes unlisted JS,
    # source maps, data files, and accidental workspace drops.
    actual_files = _tree_inventory(binding[1], exclude={"site_manifest.json"})
    if set(actual_files) != set(str(value) for value in raw_files):
        raise ValueError("product site file inventory does not match site tree")
    for relative, actual_hash in actual_files.items():
        expected = _safe_hash(raw_files.get(relative))
        if expected is None or expected != actual_hash:
            raise ValueError("product site file binding is missing or stale")
    computed_tree_hash = _tree_hash(actual_files)
    manifest_tree_hash = manifest.get("site_tree_sha256")
    if manifest_tree_hash is not None and _safe_hash(manifest_tree_hash) != computed_tree_hash:
        raise ValueError("product site tree hash is stale")
    if expected_tree_hash is not None and _safe_hash(expected_tree_hash) != computed_tree_hash:
        raise ValueError("product site tree hash does not match preview")

    # The manifest digest binds the complete inventory.  Reuse a prior
    # validation when every member retains its physical identity; this keeps
    # repeated snapshots from hashing the whole site while still detecting
    # in-place rewrites (ctime) and replacements (inode/device).  A cache hit
    # still walks every declared member through the symlink/containment checks.
    cache_key = (str(root.resolve(strict=False)), site_prefix, expected_hash)
    with _PRODUCT_SITE_CACHE_LOCK:
        cached = _PRODUCT_SITE_VALIDATION_CACHE.get(cache_key)
    if cached is not None:
        cached_manifest, cached_files, cached_fingerprints = cached
        if set(cached_files) == set(raw_files):
            cache_valid = True
            for normalized, expected in cached_files.items():
                file_binding = _safe_reference(
                    root,
                    f"{site_prefix}/{normalized}",
                    prefix=f"{site_prefix}/",
                    max_bytes=None,
                )
                if file_binding is not None and max_asset_bytes is not None:
                    try:
                        if file_binding[1].stat().st_size > max_asset_bytes:
                            raise ValueError(
                                f"product site file exceeds explicit max_asset_bytes: "
                                f"{file_binding[1].stat().st_size} > {max_asset_bytes}"
                            )
                    except OSError as exc:
                        raise ValueError("product site file cannot be inspected") from exc
                if (
                    file_binding is None
                    or _file_fingerprint(file_binding[1]) != cached_fingerprints.get(normalized)
                    or expected != _safe_hash(raw_files.get(normalized))
                ):
                    cache_valid = False
                    break
            if cache_valid and "index.html" in cached_files:
                return site_prefix, cached_manifest, dict(cached_files)

    files: dict[str, str] = {}
    fingerprints: dict[str, tuple[int, int, int, int, int]] = {}
    for raw_ref, raw_hash in raw_files.items():
        normalized = _normalise_site_asset_path(raw_ref)
        expected = _safe_hash(raw_hash)
        if normalized is None or normalized != raw_ref or expected is None or normalized == "site_manifest.json":
            raise ValueError("product site manifest file inventory is invalid")
        file_ref = f"{site_prefix}/{normalized}"
        file_binding = _safe_reference(root, file_ref, prefix=f"{site_prefix}/", max_bytes=None)
        if file_binding is None or _sha256_file(file_binding[1]) != expected:
            raise ValueError("product site file binding is missing or stale")
        try:
            if max_asset_bytes is not None and file_binding[1].stat().st_size > max_asset_bytes:
                raise ValueError(
                    f"product site file exceeds explicit max_asset_bytes: "
                    f"{file_binding[1].stat().st_size} > {max_asset_bytes}"
                )
        except OSError as exc:
            raise ValueError("product site file cannot be inspected") from exc
        fingerprint = _file_fingerprint(file_binding[1])
        if fingerprint is None:
            raise ValueError("product site file cannot be inspected")
        files[normalized] = expected
        fingerprints[normalized] = fingerprint
    if "index.html" not in files:
        raise ValueError("product site index is not declared")
    cached_value = (dict(manifest), dict(files), dict(fingerprints))
    with _PRODUCT_SITE_CACHE_LOCK:
        if len(_PRODUCT_SITE_VALIDATION_CACHE) >= _PRODUCT_SITE_CACHE_LIMIT and cache_key not in _PRODUCT_SITE_VALIDATION_CACHE:
            _PRODUCT_SITE_VALIDATION_CACHE.pop(next(iter(_PRODUCT_SITE_VALIDATION_CACHE)))
        _PRODUCT_SITE_VALIDATION_CACHE[cache_key] = cached_value
    return site_prefix, manifest, files


def _preview_manifest_path(generation_id: str) -> str:
    return f"products/generations/{generation_id}/preview/preview_manifest.json"


def _validated_incremental_preview(
    root: Path,
    *,
    run_id: str,
    generation_id: str,
) -> dict[str, Any]:
    """Validate and project the immutable generation-scoped preview."""

    preview_root = f"products/generations/{generation_id}/preview"
    preview_ref = f"{preview_root}/preview_manifest.json"
    preview_binding = _safe_product_reference(root, preview_ref, prefix=f"{preview_root}/")
    if preview_binding is None:
        raise ValueError("incremental preview manifest is missing or unsafe")
    # Reuse the core Product Agent inspector for the canonical preview schema,
    # exact item bindings, and the current pure input fingerprint.  The
    # Control Center remains a read-only adapter: constructing these typed
    # views and calling ``inspect_preview_manifest`` performs no lifecycle
    # mutation or run launch.
    try:
        from auto_foundry_core.requirement_planning import (
            RequirementSupervisorWorkspace,
            inspect_preview_manifest,
        )
        from auto_foundry_core.workspace import RunContext

        context = RunContext(run_id=run_id, run_root=root)
        product_state = RequirementSupervisorWorkspace(context).phase_snapshot().get("product")
        current_input_fingerprint = product_state.get("preview_input_fingerprint") if isinstance(product_state, Mapping) else None
        if _safe_hash(current_input_fingerprint) is None:
            raise ValueError("current preview input fingerprint is unavailable")
        inspected = inspect_preview_manifest(
            context,
            generation_id,
            preview_ref,
            expected_input_fingerprint=current_input_fingerprint,
        )
        if not isinstance(inspected, Mapping) or inspected.get("valid") is not True:
            diagnostics = inspected.get("diagnostics") if isinstance(inspected, Mapping) else None
            detail = "; ".join(str(value) for value in diagnostics) if isinstance(diagnostics, (list, tuple)) else "core preview inspection failed"
            raise ValueError(detail)
    except Exception as exc:
        raise ValueError("incremental preview failed core validation") from exc
    preview = _read_json(root, preview_ref, max_bytes=None)
    allowed_preview_fields = PREVIEW_REQUIRED_FIELDS | {"failed_items", "limitations"}
    if (
        not isinstance(preview, Mapping)
        or not PREVIEW_REQUIRED_FIELDS.issubset(set(preview))
        or bool(set(preview) - allowed_preview_fields)
    ):
        raise ValueError("incremental preview manifest schema is incomplete")
    if preview.get("schema_version") != PREVIEW_SCHEMA_VERSION or preview.get("finalizable") is not False:
        raise ValueError("incremental preview manifest is not a non-finalizable V1 preview")
    if preview.get("run_id") != run_id or preview.get("generation_id") != generation_id:
        raise ValueError("incremental preview manifest is bound to another run or generation")
    if _safe_hash(preview.get("input_fingerprint")) is None:
        raise ValueError("incremental preview input fingerprint is invalid")
    raw_items = preview.get("item_ids")
    if not isinstance(raw_items, list):
        raise ValueError("incremental preview item_ids are invalid")
    item_ids: list[str] = []
    for raw_item in raw_items:
        item_id = _safe_id(raw_item)
        if item_id is None or item_id in item_ids:
            raise ValueError("incremental preview item identity is invalid")
        item_ids.append(item_id)
    for field_name in ("failed_items", "limitations"):
        value = preview.get(field_name, [])
        if (
            not isinstance(value, list)
            or value != sorted(value)
            or len(value) != len(set(value))
            or any(not isinstance(item, str) or not item.strip() or _safe_text(item, 512) is None for item in value)
        ):
            raise ValueError(f"incremental preview {field_name} are invalid")
        _validate_embedded_reference_fields(root, value)

    receipt_ref, receipt_path, _ = _hash_bound_reference(
        root,
        preview.get("assembly_receipt_ref"),
        preview.get("assembly_receipt_sha256"),
        prefix=f"{preview_root}/",
    )
    receipt = _read_json(root, receipt_ref, max_bytes=None)
    if not isinstance(receipt, Mapping):
        raise ValueError("incremental preview assembly receipt is invalid")
    if (
        receipt.get("status") != "complete"
        or receipt.get("new_analytics") is not False
        or receipt.get("run_id") != run_id
        or receipt.get("generation_id") != generation_id
    ):
        raise ValueError("incremental preview assembly receipt is bound to another run or generation")
    _validate_embedded_reference_fields(root, receipt)

    blueprint_binding = _safe_product_reference(
        root,
        preview.get("blueprint_ref"),
        prefix=f"{preview_root}/",
    )
    if blueprint_binding is None:
        raise ValueError("incremental preview Blueprint reference is missing or unsafe")
    blueprint_ref, blueprint = _validated_blueprint(
        root,
        blueprint_binding[0],
        preview.get("blueprint_sha256"),
        run_id=run_id,
        generation_id=generation_id,
    )
    site_ref = preview.get("site_ref")
    site_manifest_ref = preview.get("site_manifest_ref")
    site_manifest_hash = preview.get("site_manifest_sha256")
    site_binding = _safe_product_reference(
        root,
        site_ref,
        prefix=f"{preview_root}/",
        allow_directory=True,
    )
    if site_binding is None or not site_binding[1].is_dir():
        raise ValueError("incremental preview site reference is missing or unsafe")
    expected_site_manifest_ref = f"{site_binding[0].rstrip('/')}/site_manifest.json"
    if site_manifest_ref != expected_site_manifest_ref:
        raise ValueError("incremental preview site manifest reference is stale")
    site_manifest_binding = _safe_product_reference(root, site_manifest_ref, prefix=f"{site_binding[0].rstrip('/')}/")
    if site_manifest_binding is None:
        raise ValueError("incremental preview site manifest is missing or unsafe")
    site_manifest_hash_value = _safe_hash(site_manifest_hash)
    if site_manifest_hash_value is None or _sha256_file(site_manifest_binding[1]) != site_manifest_hash_value:
        raise ValueError("incremental preview site manifest hash is stale")
    expected_tree_hash = _safe_hash(preview.get("site_tree_sha256"))
    if expected_tree_hash is None:
        raise ValueError("incremental preview site tree hash is invalid")
    site_manifest = _read_json(root, site_manifest_ref, max_bytes=None)
    if not isinstance(site_manifest, Mapping):
        raise ValueError("incremental preview site manifest is invalid")
    if site_manifest.get("run_id") not in {None, run_id} or site_manifest.get("generation_id") not in {None, generation_id}:
        raise ValueError("incremental preview site manifest is bound to another run or generation")
    # ``site_manifest.site_tree_sha256`` binds the renderer inventory only;
    # it excludes site_manifest.json so the manifest does not hash itself.
    # The preview manifest and assembler receipt carry the separate complete
    # tree binding, including site_manifest.json.
    embedded_site_tree_hash = _safe_hash(site_manifest.get("site_tree_sha256"))
    if embedded_site_tree_hash is None:
        raise ValueError("incremental preview site manifest tree hash is invalid")
    _validated_product_site(
        root,
        site_ref,
        site_manifest_hash_value,
        # The preview contract uses the assembler's canonical direct
        # sorted file-map hash.  The site manifest itself is intentionally
        # excluded from that inventory because its bytes contain the hash.
        expected_tree_hash=embedded_site_tree_hash,
        expected_blueprint_ref=blueprint_ref,
        expected_blueprint_hash=_safe_hash(preview.get("blueprint_sha256")),
        expected_run_id=run_id,
        expected_generation_id=generation_id,
        require_runtime=True,
    )

    # The receipt, when it carries the canonical output bindings, must point
    # at exactly the same Blueprint/site bytes as the preview manifest.  No
    # receipt field can redirect the preview elsewhere.
    complete_site_files = _tree_inventory(site_binding[1])
    if _tree_hash(complete_site_files) != expected_tree_hash:
        raise ValueError("incremental preview site tree hash is stale")

    outputs = receipt.get("outputs")
    if (
        not isinstance(outputs, Mapping)
        or outputs.get("receipt_ref") != receipt_ref
        or outputs.get("blueprint_ref") != blueprint_ref
        or outputs.get("site_ref") != site_binding[0]
    ):
        raise ValueError("incremental preview receipt output binding is stale")
    blueprint_binding = receipt.get("blueprint_binding")
    if (
        not isinstance(blueprint_binding, Mapping)
        or blueprint_binding.get("ref") != blueprint_ref
        or _safe_hash(blueprint_binding.get("sha256")) != _safe_hash(preview.get("blueprint_sha256"))
    ):
        raise ValueError("incremental preview receipt Blueprint binding is stale")
    receipt_site_binding = receipt.get("site_binding")
    supplied_tree = _safe_hash(receipt_site_binding.get("tree_sha256")) if isinstance(receipt_site_binding, Mapping) else None
    supplied_files = receipt_site_binding.get("files") if isinstance(receipt_site_binding, Mapping) else None
    if supplied_tree != expected_tree_hash or not isinstance(supplied_files, Mapping) or dict(supplied_files) != complete_site_files:
        raise ValueError("incremental preview receipt site binding is stale")

    return {
        "available": True,
        "valid": True,
        "source": "incremental_preview",
        "previewUrl": f"/api/product/preview/{quote(run_id, safe='')}/index.html",
        "dashboardUrl": f"/api/product/preview/{quote(run_id, safe='')}/index.html",
        "generationId": generation_id,
        "manifestRef": preview_ref,
        "refs": {
            "manifest": preview_ref,
            "preview_manifest_ref": preview_ref,
            "assembly_receipt_ref": receipt_ref,
            "blueprint_ref": blueprint_ref,
            "site_manifest_ref": site_manifest_ref,
            "site_ref": site_binding[0],
        },
        "hashes": {
            "manifest": _sha256_file(preview_binding[1]),
            "input_fingerprint": _safe_hash(preview.get("input_fingerprint")),
            "assembly_receipt_sha256": _safe_hash(preview.get("assembly_receipt_sha256")),
            "blueprint_sha256": _safe_hash(preview.get("blueprint_sha256")),
            "site_manifest_sha256": site_manifest_hash_value,
            "site_tree_sha256": expected_tree_hash,
        },
        "itemIds": item_ids,
        "failedItems": copy.deepcopy(preview.get("failed_items", [])),
        "limitations": copy.deepcopy(preview.get("limitations", [])),
        "runtime": {"deterministic": True, "network": False},
    }


def _active_product_sidecar_refs(
    root: Path,
    *,
    run_id: str,
    generation_id: str,
) -> dict[str, Any] | None:
    """Resolve the active Product revision pointer, read-only and fail-closed.

    The pointer is the sole mutable selector for candidate/review evidence.
    Legacy generation-root sidecars remain a fallback only while no pointer
    exists; once ``product_revision.json`` is present, projection and asset
    serving follow its exact hash-bound refs and never consult the old root
    names.  Every pointer path/ref is local, non-symlinked, and re-hashed
    before any bytes are exposed to the browser.
    """

    pointer_ref = f"products/generations/{generation_id}/product_revision.json"
    pointer_path = _safe_product_reference(
        root,
        pointer_ref,
        prefix=f"products/generations/{generation_id}/",
    )
    raw_pointer_path = root / pointer_ref
    pointer_exists = raw_pointer_path.exists() or raw_pointer_path.is_symlink()
    if pointer_path is None:
        if pointer_exists:
            raise ValueError("product revision pointer is invalid")
        return None

    # ProductReviewStore is the sole validator for revision pointer, revision,
    # candidate, and review semantics.  In particular, its hashes are typed
    # canonical-object digests (not raw JSON-byte digests), and its candidate
    # loader validates every source-bound artifact under the revision scope.
    # Keep this adapter read-only: ``read_active_revision`` never adopts or
    # rewrites legacy root evidence.  The explicit regeneration boundary is
    # responsible for that one-time mutating adoption.
    try:
        from auto_foundry_core.product_review import ProductReviewStore
        from auto_foundry_core.workspace import RunContext

        store = ProductReviewStore(RunContext(run_id=run_id, run_root=root), generation_id)
        pointer = store.read_active_revision()
        if pointer is None:
            raise ValueError("product revision pointer is missing")
        revision = store.load_revision(pointer.revision_id)
        candidate = store.load_candidate()
        review = store.load_review()
    except Exception as exc:
        raise ValueError("product revision evidence failed strict validation") from exc
    if pointer.run_id != run_id or pointer.generation_id != generation_id:
        raise ValueError("product revision pointer is bound to another run or generation")
    candidate_ref = pointer.candidate_ref
    review_ref = pointer.review_ref
    return {
        "pointer_ref": pointer_ref,
        "pointer_hash": _sha256_file(pointer_path[1]),
        "pointer": pointer,
        "revision": revision,
        "candidate": candidate,
        "review": review,
        "store": store,
        "revision_ref": pointer.revision_ref,
        "revision_hash": pointer.revision_hash,
        "candidate_ref": candidate_ref,
        "candidate_hash": pointer.candidate_hash,
        "review_ref": review_ref,
        "review_hash": pointer.review_hash,
    }


def _validated_product_candidate(
    root: Path,
    *,
    run_id: str,
    generation_id: str,
    candidate_ref: str | None = None,
    expected_candidate_hash: str | None = None,
) -> tuple[Any, str, Path, dict[str, Any], str, Mapping[str, Any], dict[str, str]] | None:
    """Load and hash-check one generation-scoped ProductCandidate."""

    sidecars = _active_product_sidecar_refs(root, run_id=run_id, generation_id=generation_id)
    if isinstance(sidecars, Mapping):
        selected_ref = sidecars.get("candidate_ref")
        if not isinstance(selected_ref, str) or not selected_ref.strip():
            raise ValueError("product revision candidate reference is missing")
        if candidate_ref is not None and candidate_ref != selected_ref:
            raise ValueError("product revision candidate reference is stale")
        candidate_ref = selected_ref
    elif candidate_ref is None:
        candidate_ref = f"products/generations/{generation_id}/product_candidate.json"
    if not isinstance(candidate_ref, str) or not candidate_ref.strip():
        raise ValueError("product candidate reference is missing")
    binding = _safe_product_reference(root, candidate_ref, prefix=f"products/generations/{generation_id}/")
    if binding is None:
        return None
    # The canonical store validates the typed candidate and all of its
    # source-bound artifact refs/hashes.  Reuse that object instead of
    # maintaining a second ProductCandidate parser in the projection.
    if isinstance(sidecars, Mapping) and sidecars.get("candidate") is not None:
        candidate = sidecars["candidate"]
    else:
        try:
            from auto_foundry_core.product_review import ProductReviewStore
            from auto_foundry_core.workspace import RunContext

            candidate = ProductReviewStore(
                RunContext(run_id=run_id, run_root=root),
                generation_id,
            ).load_candidate()
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise ValueError("product candidate failed strict validation") from exc
    payload = candidate.to_dict() if hasattr(candidate, "to_dict") else None
    if not isinstance(payload, Mapping):
        raise ValueError("product candidate is invalid")
    if candidate.run_id != run_id or candidate.generation_id != generation_id:
        raise ValueError("product candidate is bound to another run or generation")
    expected_pointer_hash = expected_candidate_hash
    if expected_pointer_hash is None and isinstance(sidecars, Mapping):
        raw_pointer_hash = sidecars.get("candidate_hash")
        expected_pointer_hash = raw_pointer_hash if isinstance(raw_pointer_hash, str) else None
    if candidate.candidate_hash != candidate.computed_hash:
        raise ValueError("product candidate hash is stale")
    if expected_pointer_hash is not None and candidate.computed_hash != expected_pointer_hash:
        raise ValueError("product revision pointer candidate hash is stale")
    # ProductCandidate's dataclass validates the shape/digest format of these
    # lineage fields; the Control Center must also verify that the referenced
    # bytes still exist in the run and have not drifted since assembly.
    plan_binding = candidate.plan_binding
    _hash_bound_reference(
        root,
        plan_binding.get("plan_ref"),
        plan_binding.get("plan_hash"),
        prefix=None,
    )
    parent_lineage = candidate.parent_lineage
    for ref_key, hash_key in (("parent_manifest_ref", "parent_manifest_hash"),):
        parent_ref = parent_lineage.get(ref_key)
        parent_hash = parent_lineage.get(hash_key)
        if parent_ref in (None, "") and parent_hash in (None, ""):
            continue
        _hash_bound_reference(root, parent_ref, parent_hash, prefix=None)
    artifact_refs: dict[str, str] = {}
    artifact_hashes: dict[str, str] = {}
    site_manifest: Mapping[str, Any] | None = None
    site_ref = ""
    for name, artifact in candidate.artifact_bindings.items():
        if not isinstance(artifact, Mapping):
            raise ValueError("product candidate artifact binding is invalid")
        raw_ref = artifact.get("ref", artifact.get("path"))
        expected_hash = artifact.get("sha256", artifact.get("hash"))
        if not isinstance(raw_ref, str) or _forbidden_product_reference(raw_ref):
            raise ValueError("product candidate artifact reference is invalid")
        if not isinstance(expected_hash, str):
            raise ValueError("product candidate artifact hash is missing")
        canonical_binding = _safe_product_reference(
            root,
            raw_ref,
            prefix="products/",
            allow_directory=name == "site",
        )
        expected_digest = _safe_hash(expected_hash)
        if canonical_binding is None or expected_digest is None:
            raise ValueError("product candidate artifact reference or hash is invalid")
        canonical, path = canonical_binding
        # A ProductReviewStore candidate uses the canonical ``hash_artifact``
        # digest for both files and trees.  Recheck that exact helper at the
        # serving boundary so an in-place tamper after store validation cannot
        # leak stale bytes into the projection.
        try:
            from auto_foundry_core.product_review import hash_artifact

            actual_kind, actual_digest = hash_artifact(path)
        except Exception as exc:
            raise ValueError("product candidate artifact cannot be hashed") from exc
        if actual_digest != expected_digest:
            raise ValueError("product candidate artifact hash is stale")
        files = _tree_inventory(path) if actual_kind == "tree" else None
        supplied_kind = artifact.get("kind")
        if supplied_kind is not None and supplied_kind != actual_kind:
            raise ValueError("product candidate artifact kind is stale")
        if actual_kind == "tree":
            supplied_files = artifact.get("files")
            if supplied_files is not None and (not isinstance(supplied_files, Mapping) or dict(supplied_files) != files):
                raise ValueError("product candidate artifact tree inventory is stale")
            if name == "site":
                site_ref = canonical
                manifest_path = _safe_product_reference(root, f"{canonical.rstrip('/')}/site_manifest.json", prefix=f"{canonical.rstrip('/')}/")
                if manifest_path is None:
                    raise ValueError("product candidate site manifest is missing or unsafe")
                site_manifest = _read_json(root, manifest_path[0], max_bytes=None)
                if not isinstance(site_manifest, Mapping):
                    raise ValueError("product candidate site manifest is invalid")
        artifact_refs[name] = canonical
        artifact_hashes[name] = expected_digest
    if not site_ref or not isinstance(site_manifest, Mapping):
        raise ValueError("product candidate site binding is missing")
    site_manifest_ref = f"{site_ref.rstrip('/')}/site_manifest.json"
    site_manifest_hash = _sha256_file(root / site_manifest_ref)
    if site_manifest_hash is None:
        raise ValueError("product candidate site manifest cannot be hashed")
    return candidate, candidate_ref, binding[1], dict(payload), site_ref, site_manifest, {
        "site_manifest_ref": site_manifest_ref,
        "site_manifest_sha256": site_manifest_hash,
        "candidate_sha256": candidate.computed_hash,
        **artifact_refs,
        **{f"{name}_sha256": digest for name, digest in artifact_hashes.items()},
    }


def _candidate_site_projection(
    root: Path,
    *,
    run_id: str,
    generation_id: str,
    candidate_data: tuple[Any, str, Path, dict[str, Any], str, Mapping[str, Any], dict[str, str]],
    review: Any = None,
) -> dict[str, Any]:
    candidate, candidate_ref, _candidate_path, _payload, site_ref, site_manifest, hashes = candidate_data
    blueprint_ref = site_manifest.get("blueprint_ref")
    blueprint_hash = _safe_hash(site_manifest.get("blueprint_sha256"))
    if blueprint_ref is not None or blueprint_hash is not None or "assets/dashboard.js" in (site_manifest.get("assets") or []):
        if not isinstance(blueprint_ref, str) or blueprint_hash is None:
            raise ValueError("product candidate site Blueprint binding is missing")
        _validated_blueprint(root, blueprint_ref, blueprint_hash, run_id=run_id, generation_id=generation_id)
    site_tree_hash = _safe_hash(site_manifest.get("site_tree_sha256"))
    _site_prefix, _validated_manifest, site_files = _validated_product_site(
        root,
        site_ref,
        hashes["site_manifest_sha256"],
        expected_tree_hash=site_tree_hash,
        expected_blueprint_ref=blueprint_ref if isinstance(blueprint_ref, str) else None,
        expected_blueprint_hash=blueprint_hash,
        expected_run_id=run_id,
        expected_generation_id=generation_id,
        require_runtime="assets/dashboard.js" in (site_manifest.get("assets") or []),
    )
    # Candidate artifact bindings hash the complete site tree, while the
    # renderer manifest's ``site_tree_sha256`` covers declared members only.
    # Preserve the complete-tree identity in the projection even when a
    # legacy candidate omitted the optional manifest field.
    site_tree_hash = _safe_hash(site_manifest.get("site_tree_sha256")) or _tree_hash(site_files)
    url = f"/api/product/preview/{quote(run_id, safe='')}/index.html"
    result = {
        "available": True,
        "valid": True,
        "source": "candidate",
        "previewUrl": url,
        "dashboardUrl": url,
        "generationId": generation_id,
        "candidateRef": candidate_ref,
        "candidateHash": candidate.computed_hash,
        "refs": {
            "candidate": candidate_ref,
            "site_ref": site_ref,
            "site_manifest_ref": hashes["site_manifest_ref"],
        },
        "hashes": {
            "candidate": candidate.computed_hash,
            "site_manifest_sha256": hashes["site_manifest_sha256"],
            "site_tree_sha256": site_tree_hash,
        },
        "artifacts": [],
        "limitations": ["Product candidate is awaiting independent review."] if review is None else ["Product candidate review is not accepted."],
    }
    # Preserve the existing metadata-only artifact surface for callers that
    # inspect candidate provenance.  Every alias is derived from the
    # ProductCandidate binding above; no free-form path is copied into the
    # browser projection.
    for name in candidate.artifact_bindings:
        artifact_ref = hashes.get(name)
        artifact_hash = hashes.get(f"{name}_sha256")
        if isinstance(artifact_ref, str):
            result["refs"][f"{name}_ref"] = artifact_ref
        if isinstance(artifact_hash, str):
            result["hashes"][f"{name}_sha256"] = artifact_hash

    fixture_ref = hashes.get("fixture")
    receipt_ref = hashes.get("receipt")
    fixture = _read_json(root, fixture_ref, max_bytes=None) if isinstance(fixture_ref, str) else None
    receipt = _read_json(root, receipt_ref, max_bytes=None) if isinstance(receipt_ref, str) else None
    if isinstance(fixture, Mapping) and isinstance(receipt, Mapping):
        if "analytical_artifacts" in fixture or "analytical_artifacts" in receipt:
            result["artifacts"] = _validated_analytical_artifact_descriptors(root, receipt, fixture)
    if isinstance(blueprint_ref, str) and blueprint_hash is not None:
        result["refs"]["blueprint_ref"] = blueprint_ref
        result["hashes"]["blueprint_sha256"] = blueprint_hash
    if isinstance(review, Mapping):
        result["reviewRef"] = review.get("review_ref")
    return result


def _load_product_review(
    root: Path,
    *,
    run_id: str,
    generation_id: str,
    candidate: Any,
    candidate_ref: str,
    review_ref: str | None = None,
    expected_review_hash: str | None = None,
) -> tuple[Any, str] | None:
    """Read one review and return its canonical hash, or ``None`` if absent."""

    sidecars = _active_product_sidecar_refs(root, run_id=run_id, generation_id=generation_id)
    if isinstance(sidecars, Mapping):
        selected_ref = sidecars.get("review_ref")
        if not isinstance(selected_ref, str) or not selected_ref.strip():
            raise ValueError("product revision review reference is missing")
        if review_ref is not None and review_ref != selected_ref:
            raise ValueError("product revision review reference is stale")
        review_ref = selected_ref
    elif review_ref is None:
        review_ref = f"products/generations/{generation_id}/product_review.json"
    if not isinstance(review_ref, str) or not review_ref.strip():
        raise ValueError("product review reference is missing")
    binding = _safe_product_reference(root, review_ref, prefix=f"products/generations/{generation_id}/")
    if binding is None:
        return None
    if isinstance(sidecars, Mapping) and sidecars.get("review") is not None:
        review = sidecars["review"]
    else:
        try:
            from auto_foundry_core.product_review import ProductReviewStore
            from auto_foundry_core.workspace import RunContext

            review = ProductReviewStore(
                RunContext(run_id=run_id, run_root=root),
                generation_id,
            ).load_review()
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise ValueError("product review failed strict validation") from exc
    if review.run_id != run_id or review.generation_id != generation_id:
        raise ValueError("product review is bound to another run or generation")
    # ProductReviewStore is authoritative for the typed candidate/review
    # binding.  A one-time legacy-root adoption intentionally copies the
    # reviewed record byte-for-byte, so its historical ``candidate_ref`` may
    # still name the pre-adoption root while the candidate hash remains the
    # exact active evidence binding.
    if review.candidate_hash != candidate.computed_hash:
        raise ValueError("product review is stale against the candidate")
    if review.review_hash != review.computed_hash:
        raise ValueError("product review hash is stale")
    if expected_review_hash is None and isinstance(sidecars, Mapping):
        raw_pointer_hash = sidecars.get("review_hash")
        expected_review_hash = raw_pointer_hash if isinstance(raw_pointer_hash, str) else None
    if expected_review_hash is not None and review.computed_hash != expected_review_hash:
        raise ValueError("product revision pointer review hash is stale")
    _validate_embedded_reference_fields(root, review.to_dict())
    return review, review_ref


def _active_product_manifest_ref(root: Path, generation_id: str) -> str | None:
    """Resolve the same active product manifest reference for projection/cache.

    A valid active-generation pointer selects the generation-scoped manifest;
    before a pointer exists, the initial ``G-0001`` lifecycle uses the legacy
    root manifest.  Keeping this decision in one read-only helper prevents the
    projection and its cache identity from observing different manifests.
    """

    # Once the ProductReviewStore pointer exists, the active candidate's
    # manifest binding is authoritative.  This keeps cache invalidation and
    # product status/asset projection aligned after a revision swap.
    run_id = _safe_id((_read_json(root, "run_state.json") or {}).get("run_id"))
    if run_id is None:
        run_id = _safe_id((_read_json(root, "active_generation.json") or {}).get("run_id"))
    if run_id is not None:
        sidecars = _active_product_sidecar_refs(root, run_id=run_id, generation_id=generation_id)
        if sidecars is not None:
            candidate = sidecars.get("candidate")
            if candidate is None:
                return None
            manifest = candidate.artifact_bindings.get("manifest")
            if not isinstance(manifest, Mapping):
                return None
            manifest_ref = manifest.get("ref", manifest.get("path"))
            manifest_hash = manifest.get("sha256", manifest.get("hash"))
            try:
                return _hash_bound_reference(
                    root,
                    manifest_ref,
                    manifest_hash,
                    prefix="products/",
                )[0]
            except (OSError, TypeError, ValueError):
                return None

    state = _read_json(root, "active_generation.json")
    if isinstance(state, Mapping):
        generation_manifest_ref = state.get("manifest_ref") or state.get("manifestRef")
        if isinstance(generation_manifest_ref, str) and generation_manifest_ref:
            return f"products/generations/{generation_id}/product_manifest.json"
    return "products/product_manifest.json" if generation_id == "G-0001" else f"products/generations/{generation_id}/product_manifest.json"


def _empty_product_projection(kind: str, *, limitation: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "valid": False,
        "source": kind,
        "generationId": None,
        "previewUrl": None,
        "dashboardUrl": None,
        "refs": {},
        "hashes": {},
        "artifacts": [],
        "limitations": [],
    }
    if limitation:
        result["limitations"].append(limitation)
    return result


def _product_projection_bundle(root: Path, summary: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(final_dashboard, preview)`` from immutable local sidecars."""

    final = _empty_product_projection("accepted_candidate")
    preview = _empty_product_projection("preview")
    run_id = _run_id_for_projection(root, summary)
    generation_id = _generation_id_for_projection(root, summary)
    if run_id is None or generation_id is None:
        message = "Active product generation is unavailable."
        final["limitations"].append(message)
        preview["limitations"].append(message)
        return final, preview
    # Sidecar validation uses the authoritative run identity, while HTTP
    # repository records are addressed by their public path-derived summary
    # id.  Keep those concerns separate and publish URLs with the latter.
    public_run_id = _safe_id(summary.get("id") or summary.get("runId")) or run_id

    sidecars: Mapping[str, Any] | None = None
    pointer_ref = f"products/generations/{generation_id}/product_revision.json"
    try:
        sidecars = _active_product_sidecar_refs(
            root,
            run_id=run_id,
            generation_id=generation_id,
        )
    except (OSError, TypeError, ValueError, UnicodeError):
        # A present pointer is an authoritative selector.  Never fall back to
        # mutable generation-root candidate/review bytes when that selector
        # is malformed or its refs/hashes drift; expose a safe limitation
        # instead of serving a stale dashboard.
        if (root / pointer_ref).exists() or (root / pointer_ref).is_symlink():
            limitation = "Active Product revision evidence is invalid; dashboard output is unavailable."
            final["limitations"].append(limitation)
            preview["limitations"].append(limitation)
            return final, preview
        sidecars = None

    candidate_data = None
    try:
        candidate_data = _validated_product_candidate(
            root,
            run_id=run_id,
            generation_id=generation_id,
            candidate_ref=(sidecars.get("candidate_ref") if isinstance(sidecars, Mapping) else None),
            expected_candidate_hash=(sidecars.get("candidate_hash") if isinstance(sidecars, Mapping) else None),
        )
    except (OSError, TypeError, ValueError, UnicodeError):
        candidate_data = None

    review = None
    review_ref = None
    if candidate_data is not None:
        candidate, candidate_ref, *_ = candidate_data
        try:
            loaded = _load_product_review(
                root,
                run_id=run_id,
                generation_id=generation_id,
                candidate=candidate,
                candidate_ref=candidate_ref,
                review_ref=(sidecars.get("review_ref") if isinstance(sidecars, Mapping) else None),
                expected_review_hash=(sidecars.get("review_hash") if isinstance(sidecars, Mapping) else None),
            )
        except (OSError, TypeError, ValueError, UnicodeError):
            loaded = None
        if loaded is not None:
            review, review_ref = loaded
        try:
            candidate_preview = _candidate_site_projection(
                root,
                run_id=run_id,
                generation_id=generation_id,
                candidate_data=candidate_data,
                review=review,
            )
        except (OSError, TypeError, ValueError, UnicodeError):
            candidate_preview = None
        if candidate_preview is not None:
            candidate_preview = dict(candidate_preview)
            candidate_preview["previewUrl"] = f"/api/product/preview/{quote(public_run_id, safe='')}/index.html"
            candidate_preview["dashboardUrl"] = candidate_preview["previewUrl"]
            if review is not None and review.verdict in {"accept", "accept_with_limits"}:
                final = dict(candidate_preview)
                final.update(
                    {
                        "source": "accepted_candidate",
                        "dashboardUrl": f"/api/product/dashboard/{quote(public_run_id, safe='')}/index.html",
                        "previewUrl": None,
                        "reviewRef": review_ref,
                        "reviewHash": review.computed_hash,
                        "reviewVerdict": review.verdict,
                        "limitations": (
                            ["Product review accepted with limits."]
                            if review.verdict == "accept_with_limits"
                            else []
                        ),
                    }
                )
                final["refs"] = {**dict(final.get("refs") or {}), "review": review_ref}
                final["hashes"] = {**dict(final.get("hashes") or {}), "review": review.computed_hash}
                return final, preview
            preview = candidate_preview
            return final, preview

    # An incremental preview is the fallback only when no valid candidate
    # bytes can be exposed.  Invalid manifests are absent from the projection;
    # they never alter run lifecycle status or become a run failure.
    try:
        incremental = _validated_incremental_preview(root, run_id=run_id, generation_id=generation_id)
    except (OSError, TypeError, ValueError, UnicodeError):
        incremental = None
    if incremental is not None:
        incremental = dict(incremental)
        incremental["previewUrl"] = f"/api/product/preview/{quote(public_run_id, safe='')}/index.html"
        incremental["dashboardUrl"] = incremental["previewUrl"]
        preview = incremental
    return final, preview


def _product_dashboard_projection(root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only a ProductCandidate with an accepted independent review."""

    return _product_projection_bundle(root, summary)[0]


def _product_preview_projection(root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only a hash-validated candidate or incremental preview."""

    return _product_projection_bundle(root, summary)[1]


def _product_signature_identity(root: Path, summary: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Hash active product outputs and generated-site identities for cache keys."""

    generation_id = _generation_id_for_projection(root, summary)
    if generation_id is None:
        return None
    manifest_ref = _active_product_manifest_ref(root, generation_id)
    manifest_binding = _safe_reference(root, manifest_ref, prefix="products/", max_bytes=None)
    if manifest_binding is None:
        return ("product", ("manifest", manifest_ref, None))
    identities: list[Any] = [("manifest", manifest_binding[0], _sha256_file(manifest_binding[1]))]
    manifest = _read_json(root, manifest_binding[0], max_bytes=None)
    dashboard = manifest.get("dashboard") if isinstance(manifest, Mapping) else None
    receipt_ref = dashboard.get("receipt_ref") if isinstance(dashboard, Mapping) else None
    receipt_binding = _safe_reference(root, receipt_ref, prefix="products/", max_bytes=None)
    if receipt_binding is None:
        identities.append(("receipt", str(receipt_ref), None))
        return ("product", *identities)
    identities.append(("receipt", receipt_binding[0], _sha256_file(receipt_binding[1])))
    receipt = _read_json(root, receipt_binding[0], max_bytes=None)
    outputs = receipt.get("outputs") if isinstance(receipt, Mapping) else None
    if not isinstance(outputs, Mapping):
        return ("product", *identities)
    for output_key in ("fixture_ref", "chart_map_ref", "chart_registry_ref", "site_ref"):
        output_binding = _safe_reference(
            root,
            outputs.get(output_key),
            prefix="products/",
            allow_directory=output_key == "site_ref",
            max_bytes=None,
        )
        if output_binding is None:
            identities.append((output_key, str(outputs.get(output_key)), None))
            continue
        identities.append((output_key, output_binding[0], _sha256_file(output_binding[1]) if output_binding[1].is_file() else None))
        if output_key != "site_ref":
            continue
        site_prefix = output_binding[0].rstrip("/")
        site_manifest_ref = f"{site_prefix}/site_manifest.json"
        site_manifest_binding = _safe_reference(root, site_manifest_ref, prefix=f"{site_prefix}/", max_bytes=None)
        if site_manifest_binding is None:
            identities.append(("site_manifest", site_manifest_ref, None))
            continue
        identities.append(("site_manifest", site_manifest_ref, _sha256_file(site_manifest_binding[1])))
        site_manifest = _read_json(root, site_manifest_ref, max_bytes=None)
        raw_files = site_manifest.get("site_file_hashes") if isinstance(site_manifest, Mapping) else None
        if not isinstance(raw_files, Mapping):
            continue
        for raw_ref in sorted(raw_files, key=lambda value: str(value)):
            normalized = _normalise_site_asset_path(raw_ref)
            if normalized is None or normalized != raw_ref or normalized == "site_manifest.json":
                identities.append(("site_file_invalid", str(raw_ref)))
                continue
            file_binding = _safe_reference(
                root,
                f"{site_prefix}/{normalized}",
                prefix=f"{site_prefix}/",
                max_bytes=None,
            )
            # The manifest hash already binds each expected digest.  Use a
            # cheap physical fingerprint for projection-cache invalidation;
            # ``product_asset`` re-hashes the requested bytes before serving,
            # and a changed ctime/inode forces the next full site validation.
            identities.append(("site_file", normalized, _file_fingerprint(file_binding[1]) if file_binding is not None else None))
    return ("product", *identities)


def _candidate_preview_signature_identity(root: Path, summary: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Return content identities for candidate/review and incremental preview.

    Product projection is cached with the broader run graph.  These sidecars
    are immutable hand-offs, so a digest (rather than a stat tuple) is the
    smallest reliable invalidation key: an in-place rewrite that preserves
    size and mtime must still cause the next snapshot to revalidate and hide
    stale bytes.
    """

    generation_id = _generation_id_for_projection(root, summary)
    if generation_id is None:
        return None
    generation_prefix = f"products/generations/{generation_id}"
    identities: list[Any] = ["candidate-preview"]

    def add_binding(label: str, raw_ref: Any, *, allow_directory: bool = False) -> tuple[str, Path] | None:
        if not isinstance(raw_ref, str):
            identities.append((label, str(raw_ref), None))
            return None
        binding = _safe_reference(root, raw_ref, prefix=None, allow_directory=allow_directory, max_bytes=None)
        if binding is None:
            identities.append((label, raw_ref, None))
            return None
        canonical, path = binding
        if path.is_dir():
            try:
                digest = _tree_hash(_tree_inventory(path))
            except (OSError, ValueError):
                digest = None
        else:
            digest = _sha256_file(path)
        identities.append((label, canonical, digest))
        return binding

    pointer_ref = f"products/generations/{generation_id}/product_revision.json"
    try:
        sidecars = _active_product_sidecar_refs(
            root,
            run_id=_run_id_for_projection(root, summary) or "",
            generation_id=generation_id,
        )
    except (OSError, TypeError, ValueError, UnicodeError):
        sidecars = None
    if sidecars is not None:
        add_binding("revision_pointer", pointer_ref)
        candidate_ref = sidecars.get("candidate_ref")
        review_ref = sidecars.get("review_ref")
    else:
        candidate_ref = f"products/generations/{generation_id}/product_candidate.json"
        review_ref = f"products/generations/{generation_id}/product_review.json"
    preview_ref = f"{generation_prefix}/preview/preview_manifest.json"
    for label, relative in (("candidate", candidate_ref), ("review", review_ref), ("preview_manifest", preview_ref)):
        add_binding(label, relative)

    # Follow only references published by the candidate/preview sidecars.  A
    # malformed sidecar contributes a digest for the sidecar itself above and
    # therefore still invalidates the projection without making snapshot
    # observation fail.
    for label, relative in (("candidate", candidate_ref), ("preview", preview_ref)):
        payload = _read_json(root, relative, max_bytes=None)
        if not isinstance(payload, Mapping):
            continue
        fields = (
            ("site", payload.get("site_ref"), True),
            ("site_manifest", payload.get("site_manifest_ref"), False),
            ("blueprint", payload.get("blueprint_ref"), False),
            ("receipt", payload.get("assembly_receipt_ref"), False),
        ) if label == "preview" else ()
        if label == "candidate":
            artifact_bindings = payload.get("artifact_bindings")
            if isinstance(artifact_bindings, Mapping):
                fields = []
                for name, artifact in artifact_bindings.items():
                    if isinstance(artifact, Mapping):
                        fields.append((str(name), artifact.get("ref", artifact.get("path")), artifact.get("kind") == "tree"))
        for field_label, raw_ref, allow_directory in fields:
            add_binding(f"{label}_{field_label}", raw_ref, allow_directory=allow_directory)
    return tuple(identities)


def _as_records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [item for item in value.values() if isinstance(item, Mapping)]
    return []


def _status(value: Any, *, default: str = "active") -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if any(token in text for token in ("fail", "error", "incident")):
        return "failed"
    if any(token in text for token in ("review", "approval", "verify")):
        return "review"
    if any(token in text for token in ("wait", "block", "pause", "pending")):
        return "waiting"
    if any(token in text for token in ("complete", "finish", "accept", "commit", "success", "terminal")):
        return "completed"
    if text in {"active", "running", "started", "start", "work", "in_progress", "queued", "ready"}:
        return "active"
    return default


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _timestamp_order(value: Any) -> tuple[int, datetime, str]:
    """Return a chronological, deterministic ordering key for a timestamp.

    Run and launch state is written with ISO-8601 timestamps, but persisted
    records can use either ``Z`` or an explicit offset (and legacy records can
    omit a timestamp altogether).  Comparing the raw strings would put an
    offset timestamp in the wrong position and would make discovery order
    depend on the filename when the records are paginated.  Valid timestamps
    therefore sort ahead of missing/invalid values and are normalized to UTC.
    """

    text = _timestamp(value)
    if text is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc), "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # ``_timestamp`` performs the same validation.  Keep this defensive
        # branch so this helper remains safe if that validator changes.
        return (0, datetime.min.replace(tzinfo=timezone.utc), text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return (1, parsed, text)


def _latest_timestamp(values: Iterable[Any]) -> str | None:
    """Choose the newest valid timestamp while retaining its original text."""

    candidates = [text for value in values if (text := _timestamp(value)) is not None]
    return max(candidates, key=_timestamp_order) if candidates else None


def _discovery_sort_key(summary: Mapping[str, Any]) -> tuple[int, datetime, str, str]:
    """Sort a run/placeholder by durable activity, newest first.

    ``_discoveryTimestamp`` is an internal fallback populated while scanning
    durable run state.  The public summary keeps the existing ``updatedAt``
    contract, while this key also understands legacy camelCase/snake_case
    creation and update names so a missing preferred field cannot demote a
    genuinely newer run before the bounded discovery window is applied.
    """

    timestamps = [
        summary.get(name)
        for name in (
            "updatedAt",
            "updated_at",
            "lastUpdatedAt",
            "last_updated_at",
            "createdAt",
            "created_at",
            "_discoveryTimestamp",
        )
    ]
    newest = max((_timestamp_order(value) for value in timestamps), default=_timestamp_order(None))
    return (*newest, str(summary.get("id") or ""))


def _first_present(raw: Mapping[str, Any], names: Iterable[str]) -> tuple[Any, bool]:
    seen = False
    for name in names:
        if name in raw:
            seen = True
            value = raw.get(name)
            if value is not None:
                return value, True
    return None, seen


def _optional_id(raw: Mapping[str, Any], names: Iterable[str]) -> tuple[str | None, bool]:
    value, present = _first_present(raw, names)
    if not present or value is None:
        return None, True
    safe = _safe_id(value)
    return safe, safe is not None


def _optional_timestamp(raw: Mapping[str, Any], names: Iterable[str]) -> tuple[str | None, bool]:
    value, present = _first_present(raw, names)
    if not present or value is None:
        return None, True
    timestamp = _timestamp(value)
    return timestamp, timestamp is not None


def _safe_role(value: Any) -> tuple[str, bool]:
    if value is None:
        return UNKNOWN_ROLE, True
    safe = _safe_id(value)
    if safe is None:
        return UNKNOWN_ROLE, False
    return safe, True


def _lifecycle_status(event_type: str) -> str | None:
    """Return a status only for the accepted lifecycle event families."""

    normalized = event_type.strip().lower().replace("-", "_")
    if normalized not in LIFECYCLE_TYPES:
        return None
    if any(token in normalized for token in ("fail",)):
        return "failed"
    if any(token in normalized for token in ("complete", "finish")):
        return "completed"
    if any(token in normalized for token in ("wait", "waiting")):
        return "waiting"
    # A progress event is active but does not imply a percentage.  A start or
    # spawn event is likewise only evidence of activity, not model success.
    return "active"


def _lifecycle_identity(raw: Mapping[str, Any], cursor: int) -> tuple[str, bool]:
    invocation, valid = _optional_id(raw, ("invocation_id", "invocationId"))
    if not valid:
        return "", False
    agent_id, valid = _optional_id(raw, ("agent_id", "agentId"))
    if not valid:
        return "", False
    task_id, valid = _optional_id(raw, ("task_id", "taskId", "task_name", "taskName", "task"))
    if not valid:
        return "", False
    # Event IDs identify observations, not agents.  Never use a unique event
    # ID or line cursor as a node identity: doing so would fabricate one agent
    # per progress/terminal event and prevent lifecycle updates from merging.
    identity = invocation or agent_id or task_id
    return (identity, identity is not None)


def parse_lifecycle_line(raw: Mapping[str, Any], cursor: int = 0) -> dict[str, Any] | None:
    """Extract bounded lifecycle metadata without maintaining a role inventory.

    The parser accepts arbitrary safe identifiers and canonical field synonyms.
    An unknown role is retained as ``unknown``; unsafe values, malformed
    timestamps, and unsupported event families are rejected fail-closed.
    """

    if not isinstance(raw, Mapping):
        return None
    event_type, present = _first_present(raw, ("event_type", "eventType", "type", "event"))
    if not present or not isinstance(event_type, str):
        return None
    normalized = event_type.strip().lower().replace("-", "_")
    status = _lifecycle_status(normalized)
    if status is None:
        return None

    identity, valid = _lifecycle_identity(raw, cursor)
    if not valid:
        return None
    event_id, valid = _optional_id(raw, ("event_id", "eventId", "tool_event_id"))
    if not valid:
        return None
    event_id = event_id or f"line-{cursor}"

    role_value, role_present = _first_present(raw, ("role", "agent_type", "agentType"))
    role, valid = _safe_role(role_value if role_present else None)
    if not valid:
        return None
    task_name, valid = _optional_id(raw, ("task_name", "taskName", "task", "task_id", "taskId"))
    if not valid:
        return None
    timestamp, valid = _optional_timestamp(raw, ("timestamp", "created_at", "at"))
    if not valid:
        return None
    parent_id, valid = _optional_id(raw, ("parent_agent_id", "parentAgentId", "controller_id", "controllerId", "invoked_by", "invokedBy"))
    if not valid:
        return None
    target, valid = _optional_id(raw, ("target_id", "targetId", "subject_id", "subjectId", "requirement_id", "requirementId"))
    if not valid:
        return None
    requester, valid = _optional_id(raw, ("requester_id", "requesterId"))
    if not valid:
        return None
    reviews, valid = _optional_id(raw, ("reviews_node_id", "reviewsNodeId"))
    if not valid:
        return None

    progress, progress_present = _first_present(raw, ("progress", "progress_pct", "progressPct"))
    if progress_present and progress is not None:
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= float(progress) <= 100:
            return None
        progress = float(progress)
    else:
        progress = None

    return {
        "id": f"codex-{event_id}",
        "eventId": event_id,
        "eventType": normalized,
        "timestamp": timestamp,
        "status": status,
        "invocationId": identity,
        "taskName": task_name,
        "agentType": _safe_id(role_value) if role_value is not None else None,
        "role": role,
        "targetId": target,
        "parentId": parent_id,
        "controllerId": parent_id,
        "requesterId": requester,
        "reviewsNodeId": reviews,
        "progress": progress,
        "cursor": cursor,
    }


def _coordinator_payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def parse_coordinator_line(raw: Mapping[str, Any], cursor: int = 0) -> dict[str, Any] | None:
    """Extract only invocation-safe fields from one coordinator event."""

    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if kind is not None and kind != "run_coordinator_event":
        return None
    event_type = raw.get("event")
    if not isinstance(event_type, str):
        return None
    normalized = event_type.strip().lower().replace("-", "_")
    if normalized not in COORDINATOR_EVENT_TYPES:
        return None
    seq = raw.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1 or seq > 2**63 - 1:
        return None
    envelope_invocation_id, valid = _optional_id(raw, ("idempotency_key",))
    payload = _coordinator_payload(raw)
    payload_id, valid_payload = _optional_id(payload, ("idempotency_key",))
    if not valid or not valid_payload:
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        result = {}
    result_id, valid = _optional_id(result, ("idempotency_key",))
    if not valid:
        return None
    # The per-event payload is the authoritative action record. A coordinator
    # checkpoint may contain several concurrent dispatches while legacy
    # envelope fields still describe the first dispatch. Prefer the payload so
    # distinct workers are never collapsed into one graph node.
    invocation_id = payload_id or result_id or envelope_invocation_id
    if invocation_id is None:
        return None

    envelope_action, valid = _optional_id(raw, ("action",))
    if not valid:
        return None
    envelope_subject, valid = _optional_id(raw, ("subject_id",))
    if not valid:
        return None
    payload_subject, valid = _optional_id(payload, ("subject_id",))
    if not valid:
        return None
    result_subject, valid = _optional_id(result, ("subject_id",))
    if not valid:
        return None

    action_record = payload.get("action")
    if not isinstance(action_record, Mapping):
        action_record = {}
    payload_action, valid = _optional_id(action_record, ("action",))
    if not valid:
        return None
    action_subject, valid = _optional_id(action_record, ("subject_id",))
    if not valid:
        return None
    action = payload_action or envelope_action
    subject_id = action_subject or payload_subject or result_subject or envelope_subject
    role_value, role_present = _first_present(payload, ("role",))
    if not role_present:
        role_value, role_present = _first_present(result, ("role",))
    if not role_present:
        role_value, role_present = _first_present(action_record, ("role",))
    role, valid = _safe_role(role_value if role_present else None)
    if not valid:
        return None
    timestamp, valid = _optional_timestamp(raw, ("created_at",))
    if not valid:
        return None
    started_at, valid = _optional_timestamp(result, ("started_at",))
    if not valid:
        return None
    finished_at, valid = _optional_timestamp(result, ("finished_at",))
    if not valid:
        return None
    owner_ref, valid = _optional_id(result, ("owner_ref",))
    if not valid:
        return None
    reviewer_ref, valid = _optional_id(result, ("reviewer_ref",))
    if not valid:
        return None
    result_status, valid = _optional_id(result, ("status",))
    if not valid:
        return None

    if normalized in {"dispatch_started", "dispatch_resumed"}:
        status = "dispatching"
    elif normalized == "role_exit":
        transport = payload.get("transport")
        if not isinstance(transport, Mapping):
            return None
        exit_code = transport.get("exit_code")
        timed_out = transport.get("timed_out", False)
        if isinstance(exit_code, bool) or (exit_code is not None and not isinstance(exit_code, int)) or not isinstance(timed_out, bool):
            return None
        status = "completed" if exit_code == 0 and not timed_out else "failed"
    elif normalized == "role_wait":
        status = "waiting"
    elif normalized == "role_completed":
        status = "failed" if result_status in {"retryable_failure", "diagnostic", "blocked_rethink", "failed"} else "completed"
    else:
        status = "failed"

    return {
        "id": f"coordinator-{seq}",
        "eventId": f"coordinator-{seq}",
        "eventType": normalized,
        "timestamp": timestamp,
        "status": status,
        "invocationId": invocation_id,
        "taskName": action,
        "agentType": None,
        "role": role,
        "targetId": subject_id,
        "subjectId": subject_id,
        "parentId": None,
        "controllerId": None,
        "requesterId": None,
        "reviewsNodeId": None,
        "progress": None,
        "cursor": cursor,
        "seq": seq,
        "action": action,
        "ownerRef": owner_ref,
        "reviewerRef": reviewer_ref,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "plannerEvidence": normalized in {"dispatch_started", "dispatch_resumed"},
    }


NODE_STATUSES = frozenset(
    {"dispatching", "active", "historical", "waiting", "review", "completed", "failed", UNKNOWN_ROLE}
)


def _merge_status(existing: str, incoming: str) -> str:
    """Merge lifecycle observations without regressing a terminal result."""

    if incoming not in NODE_STATUSES:
        return existing if existing in NODE_STATUSES else UNKNOWN_ROLE
    if existing in TERMINAL_STATUSES:
        return existing
    return incoming


def _invocation_node_id(invocation_id: Any) -> str | None:
    safe = _safe_id(invocation_id)
    if safe is None:
        return None
    return safe if safe.startswith("invocation:") else f"invocation:{safe}"


def _display_identifier(value: Any) -> str | None:
    safe = _safe_id(value)
    if safe is None:
        return None
    if re.fullmatch(r"REQ-[A-Za-z0-9.]+", safe, flags=re.IGNORECASE):
        return safe.upper()
    words = safe.replace("_", " ").replace("-", " ").split()
    if not words:
        return None
    return " ".join(word if word.isupper() else word.capitalize() for word in words)


def _node_presentation(task_name: Any, subject_id: Any, role: Any) -> tuple[str, str | None]:
    task = _display_identifier(task_name)
    subject = _display_identifier(subject_id)
    role_label = _display_identifier(role) or "Invocation"
    task_id = _safe_id(task_name)
    if subject and task_id == "review_identity_result":
        label = f"{subject} Reviewer"
    elif subject and task_id == "commit_identity_result":
        label = f"{subject} Commit"
    elif subject and task_id == "repair_identity_result":
        label = f"{subject} Repair"
    elif subject and role == "identity_reviewer":
        label = f"{subject} Reviewer"
    else:
        label = subject or task or role_label
    if task and subject:
        return label, f"{task} · {subject}"
    return label, task


class _GraphBuilder:
    def __init__(self, summary: Mapping[str, Any]) -> None:
        self.summary = summary
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.trace: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.limitations: list[str] = [
            "Graph nodes are created only from bounded coordinator or lifecycle invocation evidence.",
            "Missing parent/controller lineage is omitted rather than inferred.",
            "Prompts, messages, model responses, raw commands, and analytical data are excluded.",
        ]

    def node(
        self,
        node_id: str,
        *,
        role: str,
        label: str,
        status: str = "active",
        source: str = "lifecycle",
        classification: str = "explicit",
        completed_at: str | None = None,
        started_at: str | None = None,
        invocation_id: str | None = None,
        task_name: str | None = None,
        subject_id: str | None = None,
        parent_id: str | None = None,
        progress: float | None = None,
        objective: str | None = None,
    ) -> str | None:
        if len(self.nodes) >= MAX_NODES and node_id not in self.nodes:
            # Keep the bounded projection useful for a long-running stream:
            # evict the oldest non-Planner node so the latest invocation can
            # still appear.  Its trace/event facts remain append-only.
            evictable = next((key for key in self.nodes if key != "planner"), None)
            if evictable is None:
                return None
            self.nodes.pop(evictable, None)
            for edge_id in [
                edge_id
                for edge_id, edge in self.edges.items()
                if edge.get("source") == evictable or edge.get("target") == evictable
            ]:
                self.edges.pop(edge_id, None)
        node_id = _safe_id(node_id)
        if node_id is None:
            return None
        role = _safe_id(role) or UNKNOWN_ROLE
        status = status if status in NODE_STATUSES else UNKNOWN_ROLE
        existing = self.nodes.get(node_id)
        if existing is not None:
            existing["status"] = _merge_status(str(existing.get("status") or UNKNOWN_ROLE), status)
            if existing.get("role") in {None, UNKNOWN_ROLE} and role != UNKNOWN_ROLE:
                existing["role"] = role
            if label and existing.get("label") in {None, "", UNKNOWN_ROLE}:
                existing["label"] = _safe_label(label, role)
            for key, value in (
                ("completedAt", completed_at),
                ("startedAt", started_at),
                ("invocationId", invocation_id),
                ("taskName", task_name),
                ("subjectId", subject_id),
                ("parentId", parent_id),
                ("progress", progress),
                ("objective", objective),
            ):
                if value is not None:
                    existing[key] = value
            existing["active"] = existing["status"] in {"dispatching", "active"}
            if completed_at:
                existing["recent"] = _recent(completed_at)
                existing["visible"] = True
            return node_id
        value: dict[str, Any] = {
            "id": node_id,
            "role": role,
            # The browser derives columns from the actual parent DAG.  This
            # neutral marker preserves compatibility with older clients while
            # avoiding a backend role/lane inventory.
            "lane": "dynamic",
            "label": _safe_label(label, role),
            "status": status,
            "active": status in {"dispatching", "active"},
            "source": source,
            "classification": classification,
            "visible": True,
            "recent": True,
        }
        for key, value_item in (
            ("invocationId", invocation_id),
            ("taskName", task_name),
            ("subjectId", subject_id),
            ("parentId", parent_id),
            ("progress", progress),
            ("startedAt", started_at),
            ("completedAt", completed_at),
            ("objective", objective),
        ):
            if value_item is not None:
                value[key] = value_item
        if completed_at:
            value["recent"] = _recent(completed_at)
            value["visible"] = True
        self.nodes[node_id] = value
        return node_id

    def edge(
        self,
        source: str | None,
        target: str | None,
        *,
        kind: str,
        label: str,
        parent_id: str | None = None,
        controller_id: str | None = None,
        requester_id: str | None = None,
        reviews_node_id: str | None = None,
    ) -> None:
        if not source or not target or len(self.edges) >= MAX_EDGES:
            return
        if source == target or source not in self.nodes or target not in self.nodes:
            return
        edge_id = hashlib.sha256(f"{source}|{target}|{kind}|{label}".encode()).hexdigest()[:16]
        value: dict[str, Any] = {"id": edge_id, "source": source, "target": target, "kind": kind, "label": label}
        for key, value_item in (
            ("parentId", parent_id),
            ("controllerId", controller_id),
            ("requesterId", requester_id),
            ("reviewsNodeId", reviews_node_id),
        ):
            if value_item:
                value[key] = value_item
        self.edges[edge_id] = value

    def span(self, event: Mapping[str, Any], node_id: str | None, *, source: str) -> None:
        label = _safe_label(event.get("taskName") or event.get("role"), "Invocation")
        span: dict[str, Any] = {
            "id": event.get("id"),
            "nodeId": node_id,
            "label": label,
            "role": event.get("role", UNKNOWN_ROLE),
            "status": event.get("status", UNKNOWN_ROLE),
            "startMs": 0,
            "durationMs": 0,
            "depth": 0,
            "source": source,
            "eventId": event.get("eventId"),
        }
        for key in ("invocationId", "parentId", "timestamp", "seq", "cursor"):
            if event.get(key) is not None:
                span[key] = event[key]
        self.trace.append(span)


def _recent(timestamp: str) -> bool:
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (_now() - value).total_seconds() <= 30
    except (ValueError, TypeError):
        return False


def _trace_sort_key(item: tuple[int, Mapping[str, Any]]) -> tuple[int, float, int, int, int, str, int]:
    """Return a deterministic cross-stream chronology key for one span."""

    index, span = item
    timestamp = span.get("timestamp")
    has_timestamp = 0
    timestamp_value = 0.0
    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp_value = parsed.astimezone(timezone.utc).timestamp()
            has_timestamp = 1
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    seq = span.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        seq = 0
    cursor = span.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int):
        cursor = 0
    source_rank = 0 if span.get("source") == "coordinator" else 1
    return (has_timestamp, timestamp_value, seq, cursor, source_rank, str(span.get("eventId") or ""), index)


def _bounded_trace(builder: _GraphBuilder, visible_nodes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep a latest bounded cross-stream trace and every visible terminal span.

    Each invocation source is independently bounded before projection, so
    collecting the safe spans here remains finite.  Chronology is based on
    validated timestamps, then source sequence/cursor.  A visible terminal
    node's latest terminal observation is pinned into the final window when a
    large cross-stream history would otherwise trim it.
    """

    ordered = [span for _index, span in sorted(enumerate(builder.trace), key=_trace_sort_key)]
    if len(ordered) <= MAX_TRACE:
        return ordered
    retained = ordered[-MAX_TRACE:]
    retained_ids = {span.get("nodeId") for span in retained}
    required_ids = {
        str(node.get("id"))
        for node in visible_nodes
        if node.get("id") and node.get("status") in TERMINAL_STATUSES
    }
    latest_terminal: dict[str, dict[str, Any]] = {}
    for span in ordered:
        node_id = span.get("nodeId")
        if node_id in required_ids and span.get("status") in TERMINAL_STATUSES:
            latest_terminal[str(node_id)] = span
    missing = [span for node_id, span in latest_terminal.items() if node_id not in retained_ids or not any(span is item for item in retained)]
    if not missing:
        return retained
    required_span_objects = {id(span) for span in latest_terminal.values()}
    replaceable = [index for index, span in enumerate(retained) if id(span) not in required_span_objects]
    for span in missing:
        if not replaceable:
            break
        retained[replaceable.pop(0)] = span
    retained.sort(key=lambda span: _trace_sort_key((0, span)))
    return retained


def _projected_event(event: Mapping[str, Any], *, cursor: int, stream_id: str, category: str) -> dict[str, Any]:
    """Build the browser event envelope from the parser's safe fields only."""

    details: dict[str, Any] = {}
    for source_key, output_key in (
        ("eventId", "eventId"),
        ("eventType", "eventType"),
        ("seq", "seq"),
        ("invocationId", "invocationId"),
        ("role", "role"),
        ("status", "status"),
        ("taskName", "taskName"),
        ("subjectId", "subjectId"),
        ("parentId", "parentId"),
        ("progress", "progress"),
        ("startedAt", "startedAt"),
        ("finishedAt", "finishedAt"),
        ("ownerRef", "ownerRef"),
        ("reviewerRef", "reviewerRef"),
    ):
        value = event.get(source_key)
        if value is not None:
            details[output_key] = value
    role = str(event.get("role") or UNKNOWN_ROLE)
    event_type = str(event.get("eventType") or "lifecycle")
    task = _display_identifier(event.get("taskName"))
    subject = _display_identifier(event.get("subjectId"))
    summary_parts = [event_type.replace("_", " ")]
    if subject:
        summary_parts.append(subject)
    if task and task != subject:
        summary_parts.append(task)
    if len(summary_parts) == 1:
        summary_parts.append(role)
    return {
        "id": event.get("id"),
        "cursor": cursor,
        "streamId": stream_id,
        "timestamp": event.get("timestamp") or "",
        "type": event_type,
        "category": category,
        "status": event.get("status") or UNKNOWN_ROLE,
        "role": role,
        "nodeId": _invocation_node_id(event.get("invocationId")),
        "summary": " · ".join(summary_parts),
        "details": details,
    }


def _active_coordinator_invocations(run_root: Path, run_status: str) -> set[str] | None:
    """Return the authoritative set of currently dispatched top-level calls.

    ``dispatch_started`` is durable history, not proof that an invocation is
    still alive after a pause, crash, or later checkpoint.  The Coordinator's
    current ``active_dispatches`` list is the authority for the live subset.
    ``None`` means that no valid current-state record was available, in which
    case projection keeps the event status rather than guessing.
    """

    state = _read_json(run_root, "control_plane/coordinator_state.json")
    if not isinstance(state, Mapping):
        return None
    raw_dispatches = state.get("active_dispatches")
    if not isinstance(raw_dispatches, list):
        return None
    # A paused or terminal lifecycle cannot have a live role even if a stale
    # coordinator_state survived an abrupt process stop.
    if run_status in {
        "paused",
        "waiting",
        "failed",
        "blocked",
        "blocked_rethink",
        "complete",
        "completed",
        "complete_with_limits",
        "cancelled",
    }:
        return set()
    active: set[str] = set()
    for item in raw_dispatches:
        if not isinstance(item, Mapping):
            continue
        invocation_id = _safe_id(item.get("idempotency_key"))
        if invocation_id is not None:
            active.add(invocation_id)
    return active


def _project_run(run_root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    builder = _GraphBuilder(summary)
    coordinator_history, coordinator_truncated = _read_graph_history(
        run_root,
        "control_plane/coordinator_events.jsonl",
    )
    lifecycle_history, lifecycle_truncated = _read_graph_history(
        run_root,
        "control_center/lifecycle_events.jsonl",
    )
    coordinator_events = [
        event
        for cursor, raw in coordinator_history
        if (event := parse_coordinator_line(raw, cursor)) is not None
    ]
    lifecycle_events = [
        event
        for cursor, raw in lifecycle_history
        if (event := parse_lifecycle_line(raw, cursor)) is not None
    ]
    if coordinator_truncated or lifecycle_truncated:
        builder.limitations.append(
            "Graph history exceeded the durable reconstruction bound; the oldest prefix is unavailable."
        )

    # Coordinator events establish the authoritative top-level dispatcher.
    # They are the strongest signal available without changing runtime
    # semantics: dispatch_started/resumed means an adapter boundary was
    # entered, while result/diagnostic events terminalize that same key.
    run_status = str(summary.get("status") or "").lower()
    if run_status in {"paused", "waiting"}:
        planner_status = "waiting"
    elif run_status in {"failed", "blocked", "blocked_rethink"}:
        planner_status = "failed"
    elif run_status in {"complete", "completed", "complete_with_limits"}:
        planner_status = "completed"
    else:
        planner_status = "active"
    active_coordinator_invocations = _active_coordinator_invocations(run_root, run_status)

    identity_workflow_actions = {
        "resolve_identity": "resolve",
        "review_identity_result": "review",
        "repair_identity_result": "repair",
        "commit_identity_result": "commit",
    }
    identity_workflow_roles = {"entity_resolution_owner", "identity_reviewer"}
    identity_tail_by_subject: dict[str, str] = {}
    sequenced_dispatch_nodes: set[str] = set()

    for event in coordinator_events:
        node_id = _invocation_node_id(event.get("invocationId"))
        if node_id is None:
            continue
        if event.get("plannerEvidence"):
            builder.node("planner", role="planner", label="Planner", status=planner_status, source="coordinator", classification="explicit")
        role = str(event.get("role") or UNKNOWN_ROLE)
        label, objective = _node_presentation(event.get("taskName"), event.get("subjectId"), role)
        completed_at = event.get("finishedAt") or (event.get("timestamp") if event.get("status") in TERMINAL_STATUSES else None)
        node_id = builder.node(
            node_id,
            role=role,
            label=label,
            status=str(event.get("status") or UNKNOWN_ROLE),
            source="coordinator",
            completed_at=completed_at,
            started_at=event.get("startedAt") or (event.get("timestamp") if event.get("status") == "dispatching" else None),
            invocation_id=event.get("invocationId"),
            task_name=event.get("taskName"),
            subject_id=event.get("subjectId"),
            objective=objective,
        )
        if event.get("plannerEvidence") and node_id not in sequenced_dispatch_nodes:
            sequenced_dispatch_nodes.add(node_id)
            subject_id = _safe_id(event.get("subjectId"))
            action = _safe_id(event.get("action"))
            is_identity_workflow = bool(
                subject_id
                and role in identity_workflow_roles
                and action in identity_workflow_actions
            )
            previous_id = identity_tail_by_subject.get(subject_id) if is_identity_workflow else None
            if previous_id and previous_id != node_id and previous_id in builder.nodes:
                relation = identity_workflow_actions[action]
                builder.edge(
                    previous_id,
                    node_id,
                    kind=relation,
                    label=relation,
                    controller_id="planner",
                )
            else:
                builder.edge("planner", node_id, kind="dispatch", label="dispatch", controller_id="planner")
            if is_identity_workflow:
                identity_tail_by_subject[subject_id] = node_id
        builder.span(event, node_id, source="coordinator")
        builder.events.append(_projected_event(event, cursor=event.get("cursor", 0), stream_id="control-center-coordinator", category="lifecycle"))

    # Lifecycle records are independent optional evidence.  First add every
    # node, then resolve explicit parent links so child-before-parent ordering
    # does not create dangling or guessed edges.
    parent_links: list[tuple[str, str]] = []
    for event in lifecycle_events:
        node_id = _invocation_node_id(event.get("invocationId"))
        if node_id is None:
            continue
        role = str(event.get("role") or UNKNOWN_ROLE)
        label, objective = _node_presentation(event.get("taskName"), event.get("targetId"), role)
        completed_at = event.get("timestamp") if event.get("status") in TERMINAL_STATUSES else None
        node_id = builder.node(
            node_id,
            role=role,
            label=label,
            status=str(event.get("status") or UNKNOWN_ROLE),
            source="codex_lifecycle",
            completed_at=completed_at,
            started_at=event.get("timestamp") if event.get("status") == "active" else None,
            invocation_id=event.get("invocationId"),
            task_name=event.get("taskName"),
            subject_id=event.get("targetId"),
            parent_id=event.get("parentId"),
            progress=event.get("progress"),
            objective=objective,
        )
        parent_id = _invocation_node_id(event.get("parentId"))
        if parent_id:
            parent_links.append((parent_id, node_id))
        builder.span(event, node_id, source="codex_lifecycle")
        builder.events.append(_projected_event(event, cursor=event.get("cursor", 0), stream_id="control-center-lifecycle", category="lifecycle"))

    for parent_id, child_id in parent_links:
        builder.edge(parent_id, child_id, kind="parent", label="invokes", parent_id=parent_id.removeprefix("invocation:"))

    historical_node_ids: set[str] = set()
    if active_coordinator_invocations is not None:
        for node_id, node in builder.nodes.items():
            if (
                node_id == "planner"
                or node.get("source") != "coordinator"
                or node.get("status") not in {"dispatching", "active"}
            ):
                continue
            invocation_id = _safe_id(node.get("invocationId"))
            if invocation_id not in active_coordinator_invocations:
                node["status"] = "historical"
                historical_node_ids.add(node_id)

    # Explicit lifecycle children of an interrupted historical dispatch are
    # retained too, but cannot remain visually active.  Missing lineage is not
    # inferred.  When the whole run is paused/terminal, every nonterminal
    # lifecycle observation is necessarily historical.
    inactive_run = run_status in {
        "paused",
        "waiting",
        "failed",
        "blocked",
        "blocked_rethink",
        "complete",
        "completed",
        "complete_with_limits",
        "cancelled",
    }
    changed = True
    while changed:
        changed = False
        for node_id, node in builder.nodes.items():
            if node.get("source") != "codex_lifecycle" or node.get("status") not in {"dispatching", "active"}:
                continue
            parent_id = _invocation_node_id(node.get("parentId"))
            if inactive_run or (parent_id is not None and parent_id in historical_node_ids):
                node["status"] = "historical"
                historical_node_ids.add(node_id)
                changed = True

    # Durable sidecars are projected only after the event graph has been
    # reconstructed.  This keeps invocation chronology authoritative while
    # allowing operators to see the logical role session, identity-domain and
    # explicit requirement/reviewer lineage that the event stream alone does
    # not contain.
    mission_context = _mission_context_projection(run_root, summary)
    role_projection = _role_session_projection(run_root, summary)
    identity_projection = _identity_domain_projection(run_root, summary)
    product_projection, product_preview = _product_projection_bundle(run_root, summary)

    def sidecar_node(
        node_id: str,
        *,
        role: str,
        label: str,
        status: str = "historical",
        source: str = "durable_sidecar",
        **fields: Any,
    ) -> str | None:
        created = builder.node(
            node_id,
            role=role,
            label=label,
            status=status,
            source=source,
            classification="explicit",
        )
        if created is not None:
            for key, value in fields.items():
                if value is not None:
                    builder.nodes[created][key] = value
            builder.nodes[created]["active"] = builder.nodes[created].get("status") in {"dispatching", "active"}
        return created

    # Role-session registry: one logical node per registry entry and one
    # separate invocation node per idempotency key.  The session edge is
    # explicit; no cursor/event id is used as a role identity.
    for session in role_projection.get("roleSessions", ()):
        if not isinstance(session, Mapping):
            continue
        session_id = _safe_id(session.get("id"))
        role = _safe_id(session.get("role"))
        owner = _safe_id(session.get("logicalOwner"))
        if session_id is None or role is None or owner is None:
            continue
        session_status = "waiting" if session.get("status") == "replacement_required" else "historical"
        sidecar_node(
            session_id,
            role=role,
            label=owner,
            status=session_status,
            logicalOwner=owner,
            sessionId=_safe_id(session.get("sessionId")),
            subjectId=_safe_id(session.get("subjectId")),
            source="role_session_registry",
        )
    for invocation in role_projection.get("invocations", ()):
        if not isinstance(invocation, Mapping):
            continue
        invocation_id = _safe_id(invocation.get("id"))
        role = _safe_id(invocation.get("role"))
        if invocation_id is None or role is None:
            continue
        created = sidecar_node(
            invocation_id,
            role=role,
            label=_safe_label(invocation.get("action"), role),
            status="historical",
            invocationId=_safe_id(invocation.get("invocationId")),
            taskName=_safe_id(invocation.get("action")),
            subjectId=_safe_id(invocation.get("subjectId")),
            sessionId=_safe_id(invocation.get("sessionId")),
            source="role_session_registry",
        )
        if created is None:
            continue
        subject = _safe_id(invocation.get("subjectId"))
        if subject:
            subject_node = sidecar_node(
                subject,
                role="subject",
                label=subject,
                status="historical",
                source="role_session_registry",
            )
            builder.edge(created, subject_node, kind="subject", label="subject")
        session_node = _safe_id(f"role-session:{invocation.get('logicalOwner')}")
        if session_node in builder.nodes:
            builder.edge(session_node, created, kind="invokes", label="invokes")

    # Identity domains are strict typed state, not inferred labels.  Create
    # explicit requester/reviewer/owner nodes so every durable relationship is
    # visible without exposing result prose or source paths.
    for domain in identity_projection.get("domains", ()):
        if not isinstance(domain, Mapping):
            continue
        domain_node = _safe_id(domain.get("id"))
        domain_id = _safe_id(domain.get("domainId"))
        if domain_node is None or domain_id is None:
            continue
        sidecar_node(
            domain_node,
            role="identity_domain",
            label=domain_id,
            status=_status(domain.get("state"), default="historical"),
            domainId=domain_id,
            canonicalIdentity=_safe_label(domain.get("canonicalIdentity"), domain_id),
            objectType=_safe_label(domain.get("objectType"), "identity"),
            ownerRef=_safe_id(domain.get("ownerRef")),
            reviewerRef=_safe_id(domain.get("reviewerRef")),
            source="entity_resolution_state",
        )
        for requirement_id in domain.get("requestedBy", ()):
            requirement = _safe_id(requirement_id)
            if requirement is None:
                continue
            requirement_node = sidecar_node(
                requirement,
                role="requirement",
                label=requirement,
                status="historical",
                source="entity_resolution_state",
            )
            builder.edge(requirement_node, domain_node, kind="requests", label="requests")
        reviewer = _safe_id(domain.get("reviewerRef"))
        if reviewer:
            reviewer_node = sidecar_node(
                f"reviewer:{reviewer}",
                role="reviewer",
                label=reviewer,
                status="historical",
                source="entity_resolution_state",
            )
            builder.edge(reviewer_node, domain_node, kind="reviews", label="reviews")
        owner = _safe_id(domain.get("ownerRef"))
        if owner:
            owner_node = sidecar_node(
                f"identity-owner:{owner}",
                role="identity_owner",
                label=owner,
                status="historical",
                source="entity_resolution_state",
            )
            builder.edge(owner_node, domain_node, kind="owns", label="owns")

    for node in builder.nodes.values():
        node["active"] = node.get("status") in {"dispatching", "active"}
        if node.get("status") in TERMINAL_STATUSES and "completedAt" not in node:
            node["recent"] = False
            node["visible"] = True
    visible_nodes = list(builder.nodes.values())
    base_limitations = list(summary.get("limitations") or [])
    for limitation in builder.limitations:
        if limitation not in base_limitations:
            base_limitations.append(limitation)
    for sidecar in (mission_context, role_projection, identity_projection, product_projection, product_preview):
        for limitation in sidecar.get("limitations", ()):
            if limitation not in base_limitations:
                base_limitations.append(limitation)
    visible_ids = {node.get("id") for node in visible_nodes}
    return {
        "nodes": visible_nodes,
        "edges": [edge for edge in builder.edges.values() if edge.get("source") in visible_ids and edge.get("target") in visible_ids],
        "trace": _bounded_trace(builder, visible_nodes),
        "events": builder.events,
        "limitations": base_limitations,
        "missionContext": mission_context,
        "roleSessions": role_projection.get("roleSessions", []),
        "invocations": role_projection.get("invocations", []),
        "identityDomains": identity_projection.get("domains", []),
        "dataRevisions": _data_revision_projection(run_root),
        "productDashboard": product_projection,
        "productPreview": product_preview,
    }


def _effective_run_status(run_root: Path, lifecycle_status: Any) -> str:
    """Overlay a live Coordinator status only while lifecycle says running."""

    status = str(lifecycle_status or "unknown").strip().lower()
    if status != "running":
        return status
    coordinator = _read_json(run_root, "control_plane/coordinator_state.json")
    if not isinstance(coordinator, Mapping):
        return status
    coordinator_status = str(coordinator.get("status") or "").strip().lower()
    return {
        "waiting": "waiting",
        "blocked": "blocked",
        "blocked_rethink": "blocked",
        "failed": "failed",
        "complete": "complete",
        "completed": "complete",
    }.get(coordinator_status, status)


class OperationalRepository(ReadOnlyRepository):
    """Read-only repository with a bounded durable/lifecycle graph overlay."""

    def __init__(self, fixture_path: Path | None, run_roots: Iterable[Path], launch_state_root: Path | None = None) -> None:
        super().__init__(fixture_path, run_roots)
        # LaunchManager writes the draft/status pair before the run root has a
        # run_state.json.  Keep this optional so the inherited read-only
        # repository remains usable by callers that have no operational launch
        # manager (fixtures and direct projection tests).
        raw_launch_root = Path(launch_state_root).expanduser() if launch_state_root else None
        self.launch_state_root = raw_launch_root
        self._launch_state_root_symlinked = bool(raw_launch_root and _has_symlink_component(raw_launch_root))
        self._projection_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._data_revision_projection_cache: dict[
            str,
            tuple[tuple[Any, ...], dict[str, Any] | None],
        ] = {}
        self._projection_cache_lock = threading.RLock()
        self._records_cache: tuple[float, tuple[RunRecord, ...]] | None = None

    def _cached_data_revision_projection(self, root: Path) -> dict[str, Any] | None:
        """Strictly verify a revision once per unchanged filesystem identity."""

        key = str(root)
        signature = _data_revision_projection_signature(root)
        with self._projection_cache_lock:
            cached = self._data_revision_projection_cache.get(key)
            if cached is not None and cached[0] == signature:
                return copy.deepcopy(cached[1])
        projection = _data_revision_projection(root)
        with self._projection_cache_lock:
            if len(self._data_revision_projection_cache) >= 128 and key not in self._data_revision_projection_cache:
                self._data_revision_projection_cache.pop(next(iter(self._data_revision_projection_cache)))
            self._data_revision_projection_cache[key] = (signature, copy.deepcopy(projection))
        return projection

    @staticmethod
    def _projection_signature(root: Path, summary: Mapping[str, Any]) -> tuple[Any, ...]:
        values: list[Any] = [str(summary.get("status") or "unknown")]
        relatives: list[str] = [
            "control_plane/coordinator_events.jsonl",
            "control_plane/coordinator_state.json",
            "control_plane/role_sessions.json",
            "control_center/lifecycle_events.jsonl",
            "control_center/mission_context_active.json",
            "active_generation.json",
            "entity_resolution/state.json",
            "data_room/current_revision.json",
            "data_room/pending_data_refresh.json",
            "data_room/revision_transaction.json",
            "products/product_manifest.json",
        ]
        # Product validation follows the active generation's manifest to a
        # hash-bound receipt and output files.  Include those exact local
        # bindings in the cache signature too: otherwise a receipt/output
        # tamper after the first read would leave a stale ``valid`` overlay in
        # memory until an unrelated lifecycle event arrived.  All candidate
        # references are passed through the same in-root, non-symlink guard as
        # the projection itself; malformed values simply do not contribute a
        # path and are therefore fail-closed on the next projection pass.
        generation_id = _generation_id_for_projection(root, summary)
        if generation_id:
            relatives.append(f"products/generations/{generation_id}/product_manifest.json")
            relatives.append(f"products/generations/{generation_id}/product_revision.json")
        manifest_candidates = list(relatives[-2:])
        for manifest_relative in manifest_candidates:
            manifest_path = _safe_file(root, manifest_relative, max_bytes=None)
            if manifest_path is None:
                continue
            try:
                manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest_value, Mapping):
                continue
            dashboard = manifest_value.get("dashboard")
            receipt_ref = dashboard.get("receipt_ref") if isinstance(dashboard, Mapping) else None
            if not isinstance(receipt_ref, str):
                continue
            receipt_binding = _safe_reference(root, receipt_ref, prefix="products/", max_bytes=None)
            if receipt_binding is None:
                continue
            relatives.append(receipt_binding[0])
            receipt_value = _read_json(root, receipt_binding[0], max_bytes=None)
            outputs = receipt_value.get("outputs") if isinstance(receipt_value, Mapping) else None
            if not isinstance(outputs, Mapping):
                continue
            for output_key in ("fixture_ref", "chart_map_ref", "chart_registry_ref", "site_ref"):
                output_ref = outputs.get(output_key)
                output_binding = _safe_reference(
                    root,
                    output_ref,
                    prefix="products/",
                    allow_directory=output_key == "site_ref",
                    max_bytes=None,
                )
                if output_binding is None:
                    continue
                relatives.append(output_binding[0])
                if output_key == "site_ref":
                    # Site bindings are directories; the reviewed manifest is
                    # the file whose hash is checked by the product
                    # inspector and whose changes must invalidate this cache.
                    site_manifest = _safe_file(root, f"{output_binding[0]}/site_manifest.json", max_bytes=None)
                    if site_manifest is not None:
                        relatives.append(f"{output_binding[0]}/site_manifest.json")
        # Preserve order while dropping duplicate references from a manifest
        # that aliases the same output more than once.
        seen_relatives: set[str] = set()
        for relative in relatives:
            if relative in seen_relatives:
                continue
            seen_relatives.add(relative)
            path = _safe_stream_file(root, relative)
            if path is None:
                # Sidecars are bounded JSON files; streams use the separate
                # validator above.  Include safe files here so a pointer,
                # transaction, registry, or product update invalidates the
                # projection cache immediately.
                path = _safe_file(root, relative)
            if path is None:
                values.append(None)
                continue
            try:
                stat = path.stat()
                values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except OSError:
                values.append(None)
        # Metadata-only stat tuples above preserve chronology cheaply, while
        # this bounded content identity closes the same-size/same-mtime cache
        # gap for every active product output and declared site asset.
        values.append(_product_signature_identity(root, summary))
        values.append(_candidate_preview_signature_identity(root, summary))
        values.append(_data_revision_signature_identity(root))
        return tuple(values)

    def _cached_projection(self, root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
        key = str(root)
        signature = self._projection_signature(root, summary)
        with self._projection_cache_lock:
            cached = self._projection_cache.get(key)
            if cached is not None and cached[0] == signature:
                return copy.deepcopy(cached[1])
        projection = _project_run(root, summary)
        with self._projection_cache_lock:
            if len(self._projection_cache) >= 128 and key not in self._projection_cache:
                self._projection_cache.pop(next(iter(self._projection_cache)))
            self._projection_cache[key] = (signature, copy.deepcopy(projection))
        return projection

    @staticmethod
    def _placeholder_id(run_root: Path) -> str:
        """Use the same path-derived identity as a future durable run."""

        return _run_id_for_path(run_root / "run_state.json")

    def _launch_file(self, directory: str, name: str) -> Path | None:
        root = self.launch_state_root
        if root is None or self._launch_state_root_symlinked or _has_symlink_component(root) or root.is_symlink() or not root.is_dir():
            return None
        folder = root / directory
        if folder.is_symlink() or not folder.is_dir() or not _is_within(folder, root):
            return None
        path = folder / name
        if path.is_symlink() or not _is_within(path, root) or not path.is_file():
            return None
        try:
            if path.stat().st_size > MAX_PROJECTION_BYTES:
                return None
        except OSError:
            return None
        return path

    def _launch_placeholders(self) -> list[RunRecord]:
        """Project persisted draft/status pairs before run_state exists.

        Only the direct ``drafts``/``statuses`` children written by
        LaunchManager are considered.  A placeholder is hidden as soon as a
        durable run with the same path-derived identity is discoverable.
        """

        root = self.launch_state_root
        if root is None or self._launch_state_root_symlinked or _has_symlink_component(root) or root.is_symlink() or not root.is_dir():
            return []
        drafts_dir = root / "drafts"
        statuses_dir = root / "statuses"
        for directory in (drafts_dir, statuses_dir):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()) or not _is_within(directory, root):
                return []
        try:
            draft_paths = {
                child.stem: child
                for child in drafts_dir.iterdir()
                if child.suffix == ".json"
            } if drafts_dir.is_dir() else {}
            status_paths = {
                child.stem: child
                for child in statuses_dir.iterdir()
                if child.suffix == ".json"
            } if statuses_dir.is_dir() else {}
        except OSError:
            return []

        placeholders: list[RunRecord] = []
        # Do not paginate the filename set before reading the durable launch
        # timestamps.  Draft ids are opaque UUIDs and alphabetical order says
        # nothing about which launch was created or updated most recently.
        for draft_id in sorted(set(draft_paths) | set(status_paths)):
            draft_path = self._launch_file("drafts", f"{draft_id}.json")
            status_path = self._launch_file("statuses", f"{draft_id}.json")
            draft_entry_exists = draft_id in draft_paths
            status_entry_exists = draft_id in status_paths
            # A malformed or symlinked counterpart is not silently treated as
            # absent: fail closed instead of projecting a partial binding.
            if draft_entry_exists and draft_path is None:
                continue
            if status_entry_exists and status_path is None:
                continue
            draft = _read_json(draft_path.parent, draft_path.name) if draft_path else None
            status = _read_json(status_path.parent, status_path.name) if status_path else None
            if not isinstance(draft, Mapping):
                continue
            if status_entry_exists and not isinstance(status, Mapping):
                continue
            if not isinstance(status, Mapping):
                status = {}
            draft_fingerprint = draft.get("fingerprint")
            draft_run_id = draft.get("runId")
            draft_run_root = draft.get("runRoot")
            if (
                draft.get("draftId") != draft_id
                or not isinstance(draft_fingerprint, str)
                or not draft_fingerprint.strip()
                or not _valid_launch_draft_fingerprint(draft)
                or not isinstance(draft_run_id, str)
                or not draft_run_id.strip()
                or not isinstance(draft_run_root, str)
                or not draft_run_root.strip()
            ):
                continue
            if status_entry_exists:
                if any(
                    status.get(key) != expected
                    for key, expected in (
                        ("draftId", draft_id),
                        ("fingerprint", draft_fingerprint),
                        ("runId", draft_run_id),
                        ("runRoot", draft_run_root),
                    )
                ):
                    continue
                if not isinstance(status.get("status"), str) or not status.get("status", "").strip():
                    continue
            if "projectName" in draft and not isinstance(draft.get("projectName"), str):
                continue
            if "createdAt" in draft and draft.get("createdAt") is not None and _timestamp(draft.get("createdAt")) is None:
                continue
            if any(
                timestamp_key in status
                and status.get(timestamp_key) is not None
                and _timestamp(status.get(timestamp_key)) is None
                for timestamp_key in (
                    "startedAt",
                    "started_at",
                    "acceptedAt",
                    "accepted_at",
                    "completedAt",
                    "completed_at",
                    "updatedAt",
                    "updated_at",
                )
            ):
                continue
            if "message" in status and status.get("message") is not None and not isinstance(status.get("message"), str):
                continue
            if "monitorRunId" in status and status.get("monitorRunId") is not None and _safe_id(status.get("monitorRunId")) is None:
                continue
            raw_root = Path(str(draft_run_root)).expanduser()
            if not raw_root.is_absolute() or _has_symlink_component(raw_root):
                continue
            try:
                run_root = raw_root.resolve(strict=False)
            except OSError:
                continue
            if run_root.is_symlink() or not any(_is_within(run_root, root_path) for root_path in self.run_roots):
                continue
            status_value_raw = status.get("status") if status_entry_exists else draft.get("status") or "prepared"
            if not isinstance(status_value_raw, str):
                continue
            status_value = status_value_raw.strip().lower().replace("-", "_")
            if status_value not in {"prepared", "starting", "accepted", "running", "queued", "failed", "completed", "cancelled"}:
                continue
            started_at = _timestamp(status.get("startedAt")) or _timestamp(status.get("started_at")) or _timestamp(draft.get("createdAt"))
            updated_at = _latest_timestamp(
                [
                    *(status.get(timestamp_key) for timestamp_key in (
                        "updatedAt",
                        "updated_at",
                        "completedAt",
                        "completed_at",
                        "acceptedAt",
                        "accepted_at",
                        "startedAt",
                        "started_at",
                    )),
                    draft.get("createdAt"),
                    draft.get("created_at"),
                ]
            )
            project_name = draft.get("projectName")
            if not isinstance(project_name, str) or not project_name.strip():
                project_name = str(draft.get("runId") or run_root.name)
            project_name = _safe_label(project_name, run_root.name, limit=140)
            message = status.get("message")
            if not isinstance(message, str) or not message.strip():
                message = {
                    "prepared": "Launch package is prepared and awaiting confirmation.",
                    "starting": "Interpreting requirements before the Foundry Supervisor starts.",
                    "accepted": "Foundry Supervisor accepted; waiting for durable run state.",
                    "running": "Foundry Supervisor is starting; waiting for durable run state.",
                    "queued": "Data revision is published; refresh is queued for the next safe scheduler boundary.",
                    "failed": "Launch failed before a durable run was created.",
                }.get(status_value, "Launch status is being observed.")
            summary: dict[str, Any] = {
                "id": self._placeholder_id(run_root),
                "name": project_name,
                "status": status_value,
                "updatedAt": updated_at or "",
                "requirementCount": 0,
                "requirementCountKnown": False,
                "runStatePath": None,
                "runRoot": str(run_root),
                "authoritativeRunRoot": str(run_root),
                "authoritativeRunId": draft_run_id,
                "source": "launch_placeholder",
                "readOnly": True,
                "protected": True,
                "placeholder": True,
                "draftId": draft_id,
                "observedStage": PLACEHOLDER_STAGES.get(status_value, "semantic_intake"),
                "message": _safe_text(message, 280),
                "startedAt": started_at,
            }
            _attach_data_revision(summary, self._cached_data_revision_projection(run_root))
            if status.get("monitorRunId"):
                summary["monitorRunId"] = _safe_id(status.get("monitorRunId"))
            placeholders.append(RunRecord(summary=summary, state_path=None))
        # Several draft ids can legitimately point at one not-yet-created run
        # root (for example, a retry after a failed browser request).  The
        # browser identity is path-derived, so retaining both records would
        # render duplicate rows with the same key and one can appear to be
        # missing.  Keep the newest durable status for each projected id,
        # then apply the bounded discovery window *after* chronological
        # ordering rather than slicing the alphabetically ordered filenames.
        latest_by_id: dict[str, RunRecord] = {}
        for record in placeholders:
            record_id = str(record.summary.get("id") or "")
            current = latest_by_id.get(record_id)
            if current is None or (
                _discovery_sort_key(record.summary),
                str(record.summary.get("draftId") or ""),
            ) > (
                _discovery_sort_key(current.summary),
                str(current.summary.get("draftId") or ""),
            ):
                latest_by_id[record_id] = record
        ordered = sorted(
            latest_by_id.values(),
            key=lambda record: (
                _discovery_sort_key(record.summary),
                str(record.summary.get("draftId") or ""),
            ),
            reverse=True,
        )
        return ordered[:MAX_DISCOVERED_PLACEHOLDERS]

    def _filesystem_records(self) -> list[RunRecord]:
        """Discover durable run states before applying the bounded window.

        ``ReadOnlyRepository`` bounds its generic walker while traversing the
        filesystem.  Traversal order is not a durable ordering guarantee, so
        an older run can consume a slot before a newer run is even inspected.
        Control Center discovery keeps the same read-only validation and
        payload projection, but scans all candidate state files, orders them
        by their recorded creation/update timestamp.  ``records()`` applies
        the public ``MAX_DISCOVERED_RUNS`` window only after it reconciles
        same-root launch placeholders, so a valid durable successor cannot be
        dropped before identity binding is checked.
        """

        records: list[RunRecord] = []
        seen: set[Path] = set()
        for root in self.run_roots:
            if not root.is_dir():
                continue
            for directory, names, files in os.walk(root):
                names[:] = sorted(name for name in names if name not in SKIPPED_DIRECTORIES)
                if "run_state.json" not in files:
                    continue
                state_path = Path(directory) / "run_state.json"
                try:
                    resolved = state_path.resolve()
                except OSError:
                    continue
                if resolved in seen or not any(_is_within(resolved, configured) for configured in self.run_roots):
                    continue
                seen.add(resolved)
                # Full discovery still bounds each state payload to the
                # projection's existing safe JSON limit.  A complete scan of
                # candidate paths must not turn into an unbounded content
                # read for any single durable state file.
                payload = _read_json(resolved.parent, resolved.name)
                if payload is None:
                    continue
                summary = _summarize_run_state(resolved, payload)
                summary["capacity"] = _capacity_projection(resolved, self.run_roots)
                # Keep legacy/camelCase timestamp fields available to the
                # ordering key without changing the public run summary.
                durable_timestamp = _latest_timestamp(
                    payload.get(name)
                    for name in (
                        "updated_at",
                        "updatedAt",
                        "last_updated_at",
                        "lastUpdatedAt",
                        "created_at",
                        "createdAt",
                    )
                )
                if durable_timestamp is not None:
                    summary["_discoveryTimestamp"] = durable_timestamp
                records.append(RunRecord(summary=summary, state_path=resolved))
        records.sort(key=lambda record: _discovery_sort_key(record.summary), reverse=True)
        # The public display window is applied only after records() has
        # reconciled authoritative runs with same-root launch placeholders.
        # Returning the complete safe scan here prevents an older but valid
        # placeholder-bound state from being lost before that reconciliation.
        return records

    def _candidate(self, record: RunRecord) -> dict[str, Any] | None:
        if record.state_path is None or record.fixture is not None:
            return None
        state_path = Path(record.state_path)
        state = _read_json(state_path.parent, state_path.name)
        if state is None:
            return None
        root = _validated_run_root(state.get("run_root"), state_path, self.run_roots)
        run_id = state.get("run_id")
        if root is None or not isinstance(run_id, str) or not run_id.strip():
            return None
        try:
            generation = int(state.get("generation", 0))
        except (TypeError, ValueError):
            generation = 0
        generation_match = re.search(r"(?:^|/)G-(\d+)(?:/|$)", state_path.as_posix())
        if generation_match:
            generation = max(generation, int(generation_match.group(1)))
        return {
            "record": record,
            "state_path": state_path,
            "state": state,
            "root": root,
            "run_id": run_id.strip(),
            # Generic read-only projection remains tolerant of old state
            # files, but only a fully validated lifecycle state may replace a
            # pending launch placeholder with the same path-derived ID.
            "authoritative": _authoritative_lifecycle_state(state, root, run_id.strip()),
            "generation": generation,
            "updated": (
                state.get("updated_at")
                or state.get("updatedAt")
                or state.get("last_updated_at")
                or state.get("lastUpdatedAt")
                or state.get("created_at")
                or state.get("createdAt")
                or ""
            ),
        }

    def _active_generation_path(self, root: Path, run_id: str) -> Path | None:
        pointer = _read_json(root, "active_generation.json")
        if not isinstance(pointer, Mapping):
            return None
        state_ref = pointer.get("state_ref") or pointer.get("stateRef")
        if not isinstance(state_ref, str) or not state_ref:
            return None
        path = _safe_file(root, state_ref)
        if path is None:
            return None
        state = _read_json(path.parent, path.name)
        if not isinstance(state, Mapping) or state.get("run_id") != run_id:
            return None
        return path

    def _records_uncached(self) -> list[RunRecord]:
        """Collapse generation run_state files into one stable project record."""

        # Keep fixtures from the inherited repository, but use this class's
        # full durable scan for run candidates.  The generic walker applies
        # its discovery cap while traversing paths; using it here can omit an
        # authoritative run whose path sorts after the cap, allowing a
        # same-root launch placeholder to win the identity reconciliation.
        base_records = super().records()
        fixtures = [record for record in base_records if record.fixture is not None]
        durable_records = self._filesystem_records()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in durable_records:
            candidate = self._candidate(record)
            if candidate is None:
                continue
            key = (str(candidate["root"]), str(candidate["run_id"]))
            grouped.setdefault(key, []).append(candidate)
        selected: list[RunRecord] = []
        for (root_text, run_id), candidates in grouped.items():
            root = Path(root_text)
            pointer_path = self._active_generation_path(root, run_id)
            pointer_candidate = next(
                (candidate for candidate in candidates if pointer_path is not None and candidate["state_path"].resolve(strict=False) == pointer_path.resolve(strict=False)),
                None,
            )
            chosen = pointer_candidate or max(
                candidates,
                key=lambda candidate: (
                    int(candidate["generation"]),
                    _timestamp_order(candidate["updated"]),
                    str(candidate["state_path"]),
                ),
            )
            root_candidate = next(
                (candidate for candidate in candidates if candidate["state_path"].resolve(strict=False) == root / "run_state.json"),
                None,
            )
            stable_id = (
                root_candidate["record"].summary.get("id")
                if root_candidate is not None
                else _run_id_for_path(root / "run_state.json")
            )
            summary = dict(chosen["record"].summary)
            summary["id"] = stable_id
            summary["runStatePath"] = str(chosen["state_path"])
            summary["authoritativeRunRoot"] = str(root)
            summary["authoritativeRunId"] = run_id
            summary["_authoritativeLifecycle"] = bool(chosen.get("authoritative"))
            _attach_data_revision(summary, self._cached_data_revision_projection(root))
            summary["status"] = _effective_run_status(root, summary.get("status"))
            if isinstance(chosen["state"].get("item_ids"), list):
                summary["requirementCount"] = len(chosen["state"]["item_ids"])
            selected.append(RunRecord(summary=summary, state_path=chosen["state_path"]))
        selected.sort(key=lambda record: _discovery_sort_key(record.summary), reverse=True)
        pending_placeholders = self._launch_placeholders()
        pending_by_id = {str(record.summary.get("id")): record for record in pending_placeholders}

        def binding_matches(record: RunRecord, placeholder: RunRecord) -> bool:
            if record.summary.get("_authoritativeLifecycle") is not True:
                return False
            expected_root = placeholder.summary.get("runRoot")
            actual_root = record.summary.get("authoritativeRunRoot")
            expected_run_id = placeholder.summary.get("authoritativeRunId")
            actual_run_id = record.summary.get("authoritativeRunId")
            if not isinstance(expected_root, str) or not isinstance(actual_root, str):
                return False
            try:
                roots_match = Path(expected_root).resolve(strict=False) == Path(actual_root).resolve(strict=False)
            except OSError:
                roots_match = False
            return roots_match and actual_run_id == expected_run_id

        # A parseable but foreign/partial state file can share the same
        # path-derived ID.  It is not a valid successor for this draft, so do
        # not let it hide the still-pending placeholder (or create duplicate
        # IDs in the browser list).
        selected = [
            record
            for record in selected
            if str(record.summary.get("id")) not in pending_by_id
            or binding_matches(record, pending_by_id[str(record.summary.get("id"))])
        ]
        placeholders = [
            record
            for record in pending_placeholders
            if not any(binding_matches(durable, record) for durable in selected)
        ]
        # Keep every authoritative durable successor for a launch placeholder
        # in the public window, even when its timestamp ranks below the
        # generic discovery cap.  Remaining durable records retain the normal
        # bounded, deterministic display order.
        placeholder_bound = {
            (
                str(record.summary.get("authoritativeRunRoot")),
                str(record.summary.get("authoritativeRunId")),
            )
            for record in selected
            if any(binding_matches(record, placeholder) for placeholder in pending_placeholders)
        }
        if len(selected) > MAX_DISCOVERED_RUNS:
            protected = [
                record
                for record in selected
                if (
                    str(record.summary.get("authoritativeRunRoot")),
                    str(record.summary.get("authoritativeRunId")),
                ) in placeholder_bound
            ]
            ordinary = [
                record
                for record in selected
                if (
                    str(record.summary.get("authoritativeRunRoot")),
                    str(record.summary.get("authoritativeRunId")),
                ) not in placeholder_bound
            ]
            selected = protected + ordinary[: max(0, MAX_DISCOVERED_RUNS - len(protected))]
            selected.sort(key=lambda record: _discovery_sort_key(record.summary), reverse=True)
        placeholders.sort(
            key=lambda record: (
                _discovery_sort_key(record.summary),
                str(record.summary.get("draftId") or ""),
            ),
            reverse=True,
        )
        result = fixtures + selected + placeholders
        for record in result:
            # Internal admission metadata must never become browser-visible
            # run data or part of the public snapshot contract.
            record.summary.pop("_authoritativeLifecycle", None)
            record.summary.pop("_discoveryTimestamp", None)
        return result

    def records(self) -> list[RunRecord]:
        """Return a short-lived, singleflight snapshot of discovered runs."""

        # Keep the lock across discovery.  A concurrent tab waits for the
        # first caller's complete scan and then receives an independent copy
        # of the same post-reconciliation/post-cap result.
        with self._projection_cache_lock:
            scan_started_at = monotonic()
            cached = self._records_cache
            if cached is not None and scan_started_at < cached[0]:
                # Preserve the public list return type while keeping the
                # internal cache immutable to callers.
                return list(copy.deepcopy(cached[1]))
            result = self._records_uncached()
            # Anchor expiry to the beginning of discovery.  A long scan must
            # consume the same TTL budget rather than extending visibility of
            # an already-stale result by ``scan_duration + TTL``.
            expires_at = scan_started_at + RECORDS_CACHE_TTL_SECONDS
            self._records_cache = (expires_at, tuple(copy.deepcopy(result)))
            return copy.deepcopy(result)

    def get(self, run_id: str) -> RunRecord | None:
        """Return one cached record with a cheap data-revision refresh."""

        record = super().get(run_id)
        if record is None or record.fixture is not None or record.state_path is None:
            return record
        state_path = Path(record.state_path)
        state = _read_json(state_path.parent, state_path.name)
        if state is None:
            return record
        root = _validated_run_root(state.get("run_root"), state_path, self.run_roots)
        if root is not None:
            _attach_data_revision(record.summary, self._cached_data_revision_projection(root))
        return record

    def list_runs(self) -> list[dict[str, Any]]:
        runs = super().list_runs()
        for summary in runs:
            state_ref = summary.get("runStatePath")
            if not state_ref:
                continue
            state_path = Path(str(state_ref))
            state = _read_json(state_path.parent, state_path.name)
            if isinstance(state, Mapping) and isinstance(state.get("item_ids"), list):
                summary["requirementCount"] = len(state["item_ids"])
            authoritative_root = summary.get("authoritativeRunRoot")
            root = Path(str(authoritative_root)) if isinstance(authoritative_root, str) else state_path.parent
            _attach_data_revision(summary, self._cached_data_revision_projection(root))
            manifest = _launch_manifest(root)
            if isinstance(manifest, Mapping):
                project_name = manifest.get("projectName")
                if isinstance(project_name, str) and project_name and len(project_name) <= 140 and not any(
                    ord(char) < 32 or ord(char) == 127 for char in project_name
                ):
                    # Only this bounded, launch-owned field is allowed to
                    # replace the filesystem-derived display name.  The
                    # authoritative run id remains untouched.
                    summary["name"] = project_name
        return runs

    def _run_root(self, run_id: str) -> tuple[Any, Path] | None:
        try:
            record = self.get(run_id)
        except (KeyError, OSError, ValueError):
            return None
        state_path = getattr(record, "state_path", None)
        if state_path is None:
            return None
        state_path = Path(state_path)
        state = _read_json(state_path.parent, state_path.name)
        if state is None:
            return None
        root = _validated_run_root(state.get("run_root"), state_path, self.run_roots)
        if root is None:
            return None
        return record, root

    def product_asset(self, run_id: str, asset_path: str, *, preview: bool = False) -> tuple[bytes, str] | None:
        """Return one hash-bound final/preview dashboard asset, or ``None``.

        This is deliberately uncached and read-only.  The current product is
        revalidated on every request, then the exact generated site's
        ``site_file_hashes`` inventory authorizes one relative regular file.
        No arbitrary run path, directory, or unlisted site file is exposed.
        """

        target = self._run_root(run_id)
        if target is None:
            return None
        record, root = target
        try:
            product = (
                _product_preview_projection(root, record.summary)
                if preview
                else _product_dashboard_projection(root, record.summary)
            )
            if not isinstance(product, Mapping) or product.get("valid") is not True:
                return None
            refs = product.get("refs")
            hashes = product.get("hashes")
            if not isinstance(refs, Mapping) or not isinstance(hashes, Mapping):
                return None
            normalized = _normalise_site_asset_path(asset_path)
            if normalized is None:
                return None
            source = product.get("source")
            embedded_tree_hash = None
            if source == "incremental_preview":
                # Preview manifests carry the complete tree hash, while the
                # embedded site manifest carries its non-manifest inventory
                # hash.  Validate both identities independently.
                site_manifest_ref = refs.get("site_manifest_ref")
                site_manifest_path = _safe_product_reference(root, site_manifest_ref, prefix="products/")
                site_manifest_value = _read_json(root, site_manifest_ref, max_bytes=None) if site_manifest_path is not None else None
                embedded_tree_hash = _safe_hash(site_manifest_value.get("site_tree_sha256")) if isinstance(site_manifest_value, Mapping) else None
                if embedded_tree_hash is None:
                    return None
            site_prefix, manifest, files = _validated_product_site(
                root,
                refs.get("site_ref"),
                hashes.get("site_manifest_sha256"),
                expected_tree_hash=embedded_tree_hash if source == "incremental_preview" else hashes.get("site_tree_sha256"),
                expected_blueprint_ref=refs.get("blueprint_ref"),
                expected_blueprint_hash=hashes.get("blueprint_sha256"),
                # ``run_id`` is the authoritative durable identity.  The
                # route may use a public path-derived summary id, so never
                # bind generated site bytes to that display id.
                expected_run_id=_run_id_for_projection(root, record.summary) or run_id,
                expected_generation_id=product.get("generationId"),
                require_runtime=source == "incremental_preview",
            )
            if source == "incremental_preview":
                complete_files = _tree_inventory(root / site_prefix)
                if _tree_hash(complete_files) != _safe_hash(hashes.get("site_tree_sha256")):
                    return None
            expected = files.get(normalized)
            if expected is None:
                return None
            binding = _safe_reference(
                root,
                f"{site_prefix}/{normalized}",
                prefix=f"{site_prefix}/",
                max_bytes=None,
            )
            if binding is None:
                return None
            body = _read_verified_product_asset(binding[1], expected)
            if body is None:
                return None
            return body, normalized
        except (OSError, ValueError, TypeError, UnicodeError):
            return None

    def snapshot(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        if record is not None and record.summary.get("placeholder") is True:
            summary = dict(record.summary)
            status = str(summary.get("status") or "starting")
            stream_id = f"launch-placeholder-{summary.get('id', 'unknown')}"
            return {
                "schemaVersion": "control-center-snapshot-v1",
                "run": summary,
                "capacity": None,
                "nodes": [{
                    "id": "planner",
                    "role": "planner",
                    "label": "Planner",
                    "objective": "Interpreting requirements",
                    "status": status,
                    "lane": "planner",
                    "active": status not in PLACEHOLDER_TERMINAL_STATUSES,
                }],
                "edges": [],
                "events": [],
                "trace": [],
                "observedAt": summary.get("updatedAt") or _now().isoformat().replace("+00:00", "Z"),
                "telemetry": {
                    "path": None,
                    "streamId": stream_id,
                    "nextCursor": 0,
                    "eventCountInBoundedTail": 0,
                    "truncatedToLatest": 0,
                },
                "limitations": [
                    "Semantic intake is still preparing the durable run.",
                    "Requirement count is not available until run_state.json exists.",
                ],
                "projection": {"source": "launch_placeholder", "bounded": True, "privacy": "allowlisted_metadata_only"},
            }
        base = super().snapshot(run_id)
        target = self._run_root(run_id)
        if target is None:
            return base
        record, root = target
        if isinstance(base.get("run"), dict):
            _attach_data_revision(base["run"], self._cached_data_revision_projection(root))
        projection = self._cached_projection(root, base.get("run") or record.summary)
        # Preserve inherited allowlisted telemetry events, then append only
        # normalized lifecycle metadata.  No raw lifecycle line is exposed.
        events = list(base.get("events") or [])
        event_ids = {event.get("id") for event in events if isinstance(event, Mapping)}
        events.extend(event for event in projection["events"] if event.get("id") not in event_ids)
        events = events[-MAX_TRACE:]
        base.update({"nodes": projection["nodes"], "edges": projection["edges"], "trace": projection["trace"], "events": events})
        # Sidecars are kept in dedicated keys as well as the bounded graph so
        # clients can distinguish logical sessions/domains from invocations.
        for key in (
            "missionContext",
            "roleSessions",
            "invocations",
            "identityDomains",
            "dataRevisions",
            "productDashboard",
            "productPreview",
        ):
            if key in projection:
                base[key] = projection[key]
        limitations = list(base.get("limitations") or [])
        for value in projection["limitations"]:
            if value not in limitations:
                limitations.append(value)
        base["limitations"] = limitations
        base["projection"] = {"source": "durable_projection", "bounded": True, "privacy": "allowlisted_metadata_only"}
        return base

    @staticmethod
    def _encode_cursor(coordinator: int, lifecycle: int) -> str:
        # Keep the packed pair opaque.  A browser must not round a >2^53
        # integer through IEEE-754 Number before sending it back.
        packed = (max(0, int(coordinator)) << _CURSOR_SHIFT) | (max(0, int(lifecycle)) & _CURSOR_MASK)
        return str(packed)

    @staticmethod
    def _decode_cursor(cursor: int | str) -> tuple[int, int]:
        value = max(0, int(cursor))
        return value >> _CURSOR_SHIFT, value & _CURSOR_MASK

    def _invocation_streams(self, root: Path) -> tuple[tuple[str, Path | None], ...]:
        return (
            ("control-center-coordinator", _safe_stream_file(root, "control_plane/coordinator_events.jsonl")),
            ("control-center-lifecycle", _safe_stream_file(root, "control_center/lifecycle_events.jsonl")),
        )

    def _invocation_stream_info(self, root: Path) -> tuple[str | None, dict[str, int]]:
        parts: list[str] = []
        sizes: dict[str, int] = {}
        available = False
        for name, path in self._invocation_streams(root):
            if path is None:
                parts.append(f"{name}=none")
                sizes[name] = 0
                continue
            try:
                stream_id, size = self._stream_info(path)
            except (OSError, ValueError):
                parts.append(f"{name}=none")
                sizes[name] = 0
                continue
            parts.append(f"{name}={stream_id}")
            available = True
            sizes[name] = size
        stream_id = "|".join(parts)
        return (stream_id if available else None), sizes

    def _invocation_page(
        self,
        root: Path,
        *,
        coordinator_cursor: int,
        lifecycle_cursor: int,
    ) -> tuple[list[tuple[str, int, dict[str, Any], dict[str, Any]]], tuple[int, int], bool]:
        pages: list[tuple[str, int, dict[str, Any], dict[str, Any]]] = []
        scanned_offsets = {"control-center-coordinator": coordinator_cursor, "control-center-lifecycle": lifecycle_cursor}
        valid_seen = {"control-center-coordinator": False, "control-center-lifecycle": False}
        more = False
        for name, path in self._invocation_streams(root):
            if path is None:
                continue
            offset = coordinator_cursor if name == "control-center-coordinator" else lifecycle_cursor
            try:
                lines, next_cursor, has_more = _jsonl_page(
                    path,
                    offset,
                    max_bytes=MAX_EVENT_PAGE_BYTES,
                    max_items=MAX_EVENT_PAGE_ITEMS,
                )
            except (OSError, ValueError):
                lines, next_cursor, has_more = [], offset, False
            scanned_offsets[name] = next_cursor
            more = more or has_more
            for source_cursor, raw in lines:
                event = (
                    parse_coordinator_line(raw, source_cursor)
                    if name == "control-center-coordinator"
                    else parse_lifecycle_line(raw, source_cursor)
                )
                if event is not None:
                    valid_seen[name] = True
                    pages.append((name, source_cursor, raw, event))

        # Source order is deterministic and each source's byte cursor is
        # append-only.  The combined cursor tracks both offsets independently,
        # so appends to one stream never skip unseen records from the other.
        pages.sort(key=lambda item: (0 if item[0] == "control-center-coordinator" else 1, item[1]))
        if len(pages) > MAX_EVENT_PAGE_ITEMS:
            more = True
            pages = pages[:MAX_EVENT_PAGE_ITEMS]
        consumed = {"control-center-coordinator": coordinator_cursor, "control-center-lifecycle": lifecycle_cursor}
        for name, source_cursor, _raw, _event in pages:
            consumed[name] = max(consumed[name], source_cursor)
        # Invalid/malformed lines are intentionally not projected, but they
        # must still advance the source cursor or a bad tail would be retried
        # forever on every poll.
        for name in consumed:
            if not valid_seen[name]:
                consumed[name] = scanned_offsets[name]
        return pages, (consumed["control-center-coordinator"], consumed["control-center-lifecycle"]), more

    def events_after(self, run_id: str, cursor: int | str, stream_id: str = "") -> dict[str, Any]:
        record = self.get(run_id)
        if record is not None and record.summary.get("placeholder") is True:
            current_stream = f"launch-placeholder-{record.summary.get('id', 'unknown')}"
            return {
                "events": [],
                "nextCursor": 0,
                "streamId": current_stream,
                "reset": bool(stream_id and stream_id != current_stream),
                "hasMore": False,
                "observedAt": _now().isoformat().replace("+00:00", "Z"),
            }
        target = self._run_root(run_id)
        if target is None:
            return super().events_after(run_id, int(cursor), stream_id)
        _, root = target
        current_stream, sizes = self._invocation_stream_info(root)
        if current_stream is None:
            # Preserve the base bounded telemetry stream when no invocation
            # source exists at all; once either authoritative source appears,
            # this operational endpoint switches to the merged opaque cursor.
            return super().events_after(run_id, int(cursor), stream_id)
        coordinator_cursor, lifecycle_cursor = self._decode_cursor(cursor)
        reset = bool(stream_id and stream_id != current_stream)
        if coordinator_cursor > sizes.get("control-center-coordinator", 0) or lifecycle_cursor > sizes.get("control-center-lifecycle", 0):
            reset = True
        if reset:
            coordinator_cursor = lifecycle_cursor = 0
        pages, (next_coordinator, next_lifecycle), has_more = self._invocation_page(
            root,
            coordinator_cursor=coordinator_cursor,
            lifecycle_cursor=lifecycle_cursor,
        )
        events = [
            _projected_event(
                event,
                cursor=source_cursor,
                stream_id=name,
                category="lifecycle",
            )
            for name, source_cursor, _raw, event in pages
        ]
        return {
            "events": events,
            "nextCursor": self._encode_cursor(next_coordinator, next_lifecycle),
            "streamId": current_stream,
            "reset": reset,
            "hasMore": has_more,
            "observedAt": _safe_text(_now().isoformat(), 80),
        }


__all__ = ["OperationalRepository", "parse_coordinator_line", "parse_lifecycle_line"]
