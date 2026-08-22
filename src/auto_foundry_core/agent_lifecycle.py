"""Privacy-safe lifecycle projection for streamed Codex collaboration events.

The Codex JSONL stream is transport telemetry, not analytical authority.  This
module keeps only the small set of identifiers and state transitions needed by
the local Control Center graph.  Prompts, messages, tool arguments, model
responses, and analytical data are deliberately ignored.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

try:  # pragma: no cover - POSIX hosts provide fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


LIFECYCLE_FILENAME = "lifecycle_events.jsonl"
LIFECYCLE_LOCK_FILENAME = ".lifecycle.lock"
MAX_CODEX_EVENT_BYTES = 64 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")


class LifecycleTelemetryError(RuntimeError):
    """Raised when the lifecycle stream cannot be written safely."""


def _safe_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not SAFE_ID.fullmatch(text):
        return None
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def root_thread_id(value: Mapping[str, Any]) -> str | None:
    """Return the exact root thread declared by a Codex ``thread.started`` event."""

    if value.get("type") != "thread.started":
        return None
    return _safe_id(value.get("thread_id"))


def _agent_status(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"pending_init", "starting", "started"}:
        return "agent_started"
    if text in {"running", "active", "in_progress"}:
        return "agent_progress"
    if text in {"waiting", "pending"}:
        return "agent_waiting"
    if text in {"completed", "complete", "finished", "success"}:
        return "agent_completed"
    if text in {"failed", "failure", "errored", "error", "shutdown"}:
        return "agent_failed"
    return None


def normalize_codex_event(
    value: Mapping[str, Any],
    *,
    root_thread: str | None,
    root_invocation_id: str,
) -> list[dict[str, Any]]:
    """Project one official Codex JSONL item event into safe agent lifecycle rows."""

    if not isinstance(value, Mapping):
        return []
    envelope = value.get("type")
    if envelope not in {"item.started", "item.updated", "item.completed"}:
        return []
    item = value.get("item")
    if not isinstance(item, Mapping) or item.get("type") not in {"collab_tool_call", "collab_agent_tool_call"}:
        return []
    tool = _safe_id(item.get("tool"))
    if tool not in {"spawn_agent", "send_input", "wait", "close_agent"}:
        return []
    item_id = _safe_id(item.get("id"))
    sender = _safe_id(item.get("sender_thread_id"))
    parent = root_invocation_id if root_thread is not None and sender == root_thread else sender
    states = item.get("agents_states")
    if not isinstance(states, Mapping):
        return []

    rows: list[dict[str, Any]] = []
    for raw_receiver, state_value in states.items():
        receiver = _safe_id(raw_receiver)
        if receiver is None or not isinstance(state_value, Mapping):
            continue
        event_type = _agent_status(state_value.get("status"))
        if event_type is None:
            continue
        state_role = _safe_id(state_value.get("role"))
        item_role = _safe_id(item.get("agent_role"))
        role = state_role or item_role or "agent"
        event_id = _safe_id(f"{item_id or tool}:{receiver}:{event_type}")
        if event_id is None:
            continue
        row: dict[str, Any] = {
            "event_type": event_type,
            "event_id": event_id,
            "timestamp": _now(),
            "invocation_id": receiver,
            "agent_id": receiver,
            "task_name": role if role != "agent" else tool,
            "agent_type": role,
        }
        if parent is not None:
            row["parent_agent_id"] = parent
        rows.append(row)
    return rows


def normalize_codex_json_line(
    line: bytes | str,
    *,
    root_thread: str | None,
    root_invocation_id: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Decode one bounded line, returning an updated root thread and safe rows."""

    if isinstance(line, str):
        payload = line.encode("utf-8", "replace")
    else:
        payload = bytes(line)
    if not payload or len(payload) > MAX_CODEX_EVENT_BYTES:
        return root_thread, []
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError, OverflowError):
        return root_thread, []
    if not isinstance(value, Mapping):
        return root_thread, []
    declared_root = root_thread_id(value)
    if declared_root is not None:
        root_thread = declared_root
    return root_thread, normalize_codex_event(
        value,
        root_thread=root_thread,
        root_invocation_id=root_invocation_id,
    )


class LifecycleEventWriter:
    """Append canonical lifecycle rows below one pinned run root."""

    def __init__(self, run_root: Path) -> None:
        root = Path(run_root)
        if root.is_symlink() or not root.is_dir():
            raise LifecycleTelemetryError("run root must be a real directory")
        self.run_root = root.resolve(strict=True)

    @staticmethod
    def _open_regular(name: str, *, directory_fd: int, flags: int) -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags | nofollow, 0o600, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise LifecycleTelemetryError(f"lifecycle target is not a regular file: {name}")
        return fd

    def append(self, row: Mapping[str, Any]) -> None:
        allowed = {
            "event_type", "event_id", "timestamp", "invocation_id", "agent_id",
            "task_name", "agent_type", "parent_agent_id",
        }
        if not isinstance(row, Mapping) or set(row) - allowed:
            raise LifecycleTelemetryError("lifecycle row contains unsupported fields")
        payload = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        if len(payload) > MAX_CODEX_EVENT_BYTES:
            raise LifecycleTelemetryError("lifecycle row is too large")

        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(self.run_root, root_flags)
        try:
            try:
                os.mkdir("control_center", 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            control_fd = os.open(
                "control_center",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            try:
                lock_fd = self._open_regular(
                    LIFECYCLE_LOCK_FILENAME,
                    directory_fd=control_fd,
                    flags=os.O_RDWR | os.O_CREAT,
                )
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    stream_fd = self._open_regular(
                        LIFECYCLE_FILENAME,
                        directory_fd=control_fd,
                        flags=os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    )
                    try:
                        remaining = memoryview(payload)
                        while remaining:
                            written = os.write(stream_fd, remaining)
                            if written <= 0:
                                raise LifecycleTelemetryError("lifecycle append made no progress")
                            remaining = remaining[written:]
                        os.fsync(stream_fd)
                    finally:
                        os.close(stream_fd)
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
            finally:
                os.close(control_fd)
        finally:
            os.close(root_fd)
