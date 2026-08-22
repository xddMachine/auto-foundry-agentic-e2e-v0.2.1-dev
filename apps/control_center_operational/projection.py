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
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from apps.control_center_operational.launch import LaunchManager
from apps.control_center.server import (
    MAX_EVENT_PAGE_BYTES,
    MAX_EVENT_PAGE_ITEMS,
    ReadOnlyRepository,
    RunRecord,
    _run_id_for_path,
    _jsonl_page,
    _safe_text,
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
MAX_SPECIALIST_RECORDS = 512
MAX_DISCOVERED_PLACEHOLDERS = 500
TERMINAL_STATUSES = frozenset({"completed", "failed"})
PLACEHOLDER_TERMINAL_STATUSES = frozenset({"failed", "completed", "cancelled"})
PLACEHOLDER_STAGES = {
    "prepared": "launch_prepared",
    "starting": "semantic_intake",
    "accepted": "planner_starting",
    "running": "planner_starting",
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


def _safe_file(root: Path, relative: str) -> Path | None:
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
        if candidate.stat().st_size > MAX_PROJECTION_BYTES:
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


def _read_json(root: Path, relative: str) -> dict[str, Any] | None:
    path = _safe_file(root, relative)
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
        children = [child for child in sorted(launches.iterdir()) if child.is_dir() and not child.is_symlink()][:MAX_REQUIREMENTS]
    except OSError:
        return None
    manifests: list[dict[str, Any]] = []
    for child in children:
        relative = f"control_center/launches/{child.name}/launch_manifest.json"
        value = _read_json(root, relative)
        if value is not None:
            manifests.append(value)
    if not manifests:
        return None
    manifests.sort(key=lambda value: str(value.get("createdAt", "")))
    return manifests[-1]


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
    visible_ids = {node.get("id") for node in visible_nodes}
    return {
        "nodes": visible_nodes,
        "edges": [edge for edge in builder.edges.values() if edge.get("source") in visible_ids and edge.get("target") in visible_ids],
        "trace": _bounded_trace(builder, visible_nodes),
        "events": builder.events,
        "limitations": base_limitations,
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
        self._projection_cache_lock = threading.RLock()

    @staticmethod
    def _projection_signature(root: Path, summary: Mapping[str, Any]) -> tuple[Any, ...]:
        values: list[Any] = [str(summary.get("status") or "unknown")]
        for relative in (
            "control_plane/coordinator_events.jsonl",
            "control_plane/coordinator_state.json",
            "control_center/lifecycle_events.jsonl",
        ):
            path = _safe_stream_file(root, relative)
            if path is None:
                values.append(None)
                continue
            try:
                stat = path.stat()
                values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except OSError:
                values.append(None)
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
        for draft_id in sorted(set(draft_paths) | set(status_paths))[:MAX_DISCOVERED_PLACEHOLDERS]:
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
                for timestamp_key in ("startedAt", "started_at", "acceptedAt", "completedAt")
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
            if status_value not in {"prepared", "starting", "accepted", "running", "failed", "completed", "cancelled"}:
                continue
            started_at = _timestamp(status.get("startedAt")) or _timestamp(status.get("started_at")) or _timestamp(draft.get("createdAt"))
            updated_at = (
                _timestamp(status.get("completedAt"))
                or _timestamp(status.get("acceptedAt"))
                or started_at
            )
            project_name = draft.get("projectName")
            if not isinstance(project_name, str) or not project_name.strip():
                project_name = str(draft.get("runId") or run_root.name)
            project_name = _safe_label(project_name, run_root.name, limit=140)
            message = status.get("message")
            if not isinstance(message, str) or not message.strip():
                message = {
                    "prepared": "Launch package is prepared and awaiting confirmation.",
                    "starting": "Interpreting requirements before the Planner starts.",
                    "accepted": "Planner process accepted; waiting for durable run state.",
                    "running": "Planner process is starting; waiting for durable run state.",
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
            if status.get("monitorRunId"):
                summary["monitorRunId"] = _safe_id(status.get("monitorRunId"))
            placeholders.append(RunRecord(summary=summary, state_path=None))
        return placeholders

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
            "updated": str(state.get("updated_at", state.get("updatedAt", "")) or ""),
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

    def records(self) -> list[RunRecord]:
        """Collapse generation run_state files into one stable project record."""

        base_records = super().records()
        fixtures = [record for record in base_records if record.fixture is not None]
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in base_records:
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
                key=lambda candidate: (int(candidate["generation"]), str(candidate["updated"]), str(candidate["state_path"])),
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
            summary["status"] = _effective_run_status(root, summary.get("status"))
            if isinstance(chosen["state"].get("item_ids"), list):
                summary["requirementCount"] = len(chosen["state"]["item_ids"])
            selected.append(RunRecord(summary=summary, state_path=chosen["state_path"]))
        selected.sort(key=lambda record: str(record.summary.get("updatedAt", "")), reverse=True)
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
        placeholders.sort(key=lambda record: str(record.summary.get("updatedAt", "")), reverse=True)
        result = fixtures + selected + placeholders
        for record in result:
            # Internal admission metadata must never become browser-visible
            # run data or part of the public snapshot contract.
            record.summary.pop("_authoritativeLifecycle", None)
        return result

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
        projection = self._cached_projection(root, base.get("run") or record.summary)
        # Preserve inherited allowlisted telemetry events, then append only
        # normalized lifecycle metadata.  No raw lifecycle line is exposed.
        events = list(base.get("events") or [])
        event_ids = {event.get("id") for event in events if isinstance(event, Mapping)}
        events.extend(event for event in projection["events"] if event.get("id") not in event_ids)
        events = events[-MAX_TRACE:]
        base.update({"nodes": projection["nodes"], "edges": projection["edges"], "trace": projection["trace"], "events": events})
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
