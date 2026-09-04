"""Operational read-model primitives.

This module owns the bounded, read-only filesystem projection shared by the
single Operational Control Center runtime.  It deliberately contains no HTTP
handler and no executable compatibility surface.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import mimetypes
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_FIXTURE = APP_DIR / "fixtures" / "mission.json"
MAX_REQUEST_BYTES = 256 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENT_PAGE_BYTES = 512 * 1024
MAX_EVENT_PAGE_ITEMS = 300
MAX_DISCOVERED_RUNS = 500
SKIPPED_DIRECTORIES = {".git", ".pytest_cache", "node_modules", "__pycache__"}
EVENT_FACT_FIELDS = {
    "agent_role",
    "artifact",
    "artifact_path",
    "attempt_id",
    "domain_id",
    "file_path",
    "format",
    "goal_id",
    "invocation_id",
    "item_id",
    "lease_id",
    "member_path",
    "owner_ref",
    "path",
    "requirement_id",
    "role",
    "row_count",
    "rows",
    "script_path",
    "sheet",
    "sheets",
    "source_path",
    "status",
    "subject_id",
    "tool_name",
    "worker_type",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def _coalesce(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _safe_text(value: Any, limit: int = 280) -> str:
    text = str(value if value is not None else "").replace("\x00", "").strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _safe_scalar(value: Any, limit: int = 500) -> str | int | float | bool | None:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _safe_text(value, limit)


def _allowlisted_facts(raw: Mapping[str, Any]) -> dict[str, Any]:
    facts = raw.get("facts")
    if not isinstance(facts, Mapping):
        return {}
    return {
        key: _safe_scalar(facts[key])
        for key in EVENT_FACT_FIELDS
        if key in facts and not isinstance(facts[key], (Mapping, list, tuple, set))
    }


def _safe_event_details(raw: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    """Project only explicitly approved diagnostic fields.

    This intentionally does not return unknown raw telemetry fields, prompts,
    messages, model responses, data values, or nested payloads.
    """
    details: dict[str, Any] = {}
    top_level = {
        "capability_id": "capabilityId",
        "duration_ms": "durationMs",
        "bytes_processed": "bytesProcessed",
        "cache_status": "cacheStatus",
        "rows": "rows",
    }
    for source_key, target_key in top_level.items():
        if source_key in raw and not isinstance(raw[source_key], (Mapping, list, tuple, set)):
            details[target_key] = _safe_scalar(raw[source_key])
    for key, value in facts.items():
        details[key] = value
    if raw.get("error") not in (None, ""):
        details["errorPresent"] = True
    return details


def _run_id_for_path(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"run-{digest}"


def _is_within(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _iter_run_state_files(root: Path) -> Iterable[Path]:
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in SKIPPED_DIRECTORIES]
        if "run_state.json" in files:
            yield Path(directory) / "run_state.json"


def _tail_jsonl(path: Path, max_bytes: int = MAX_EVENT_BYTES) -> list[tuple[int, dict[str, Any]]]:
    """Read a bounded JSONL tail with stable byte-offset cursors."""
    size = path.stat().st_size
    events: list[tuple[int, dict[str, Any]]] = []
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()
        while True:
            raw_line = handle.readline()
            if not raw_line:
                break
            cursor = handle.tell()
            try:
                item = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                events.append((cursor, item))
    return events


def _jsonl_page(
    path: Path,
    after: int,
    *,
    max_bytes: int = MAX_EVENT_PAGE_BYTES,
    max_items: int = MAX_EVENT_PAGE_ITEMS,
) -> tuple[list[tuple[int, dict[str, Any]]], int, bool]:
    """Read one incremental page after a byte cursor without dropping lines."""
    size = path.stat().st_size
    cursor = min(max(0, after), size)
    events: list[tuple[int, dict[str, Any]]] = []
    bytes_read = 0
    with path.open("rb") as handle:
        handle.seek(cursor)
        while len(events) < max_items and bytes_read < max_bytes:
            line_start = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                break
            bytes_read += len(raw_line)
            line_end = handle.tell()
            try:
                item = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if not raw_line.endswith(b"\n") and line_end >= size:
                    cursor = line_start
                    break
                cursor = line_end
                continue
            cursor = line_end
            if isinstance(item, dict):
                events.append((cursor, item))
    return events, cursor, cursor < size


def _find_telemetry(run_state_path: Path, allowed_roots: tuple[Path, ...]) -> Path | None:
    candidates = ("telemetry/events.jsonl", "telemetry.jsonl", "events.jsonl")
    current = run_state_path.parent
    for _ in range(7):
        if not _is_within(current, allowed_roots):
            break
        for candidate in candidates:
            path = current / candidate
            if not path.is_file() or path.is_symlink():
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if _is_within(resolved, allowed_roots):
                return resolved
        if current.parent == current:
            break
        current = current.parent
    return None


def _find_entity_resolution_state(
    run_state_path: Path, allowed_roots: tuple[Path, ...]
) -> Path | None:
    current = run_state_path.parent
    for _ in range(7):
        if not _is_within(current, allowed_roots):
            break
        candidate = current / "entity_resolution" / "state.json"
        if candidate.is_file() and not candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                resolved = None
            if resolved is not None and _is_within(resolved, allowed_roots):
                return resolved
        if current.parent == current:
            break
        current = current.parent
    return None


def _capacity_projection(
    run_state_path: Path, allowed_roots: tuple[Path, ...]
) -> dict[str, Any]:
    state_path = _find_entity_resolution_state(run_state_path, allowed_roots)
    if state_path is None:
        return {
            "total": None,
            "active": None,
            "entityResolution": None,
            "entityResolutionActive": None,
            "analyticalOwner": None,
            "analyticalOwnerActive": None,
            "specialist": None,
            "specialistActive": None,
            "plannerExcluded": True,
            "source": "unavailable",
        }
    try:
        state = _load_json(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "total": None,
            "active": None,
            "entityResolution": None,
            "entityResolutionActive": None,
            "analyticalOwner": None,
            "analyticalOwnerActive": None,
            "specialist": None,
            "specialistActive": None,
            "plannerExcluded": True,
            "source": "invalid",
        }
    capacity = state.get("capacity") if isinstance(state.get("capacity"), Mapping) else {}
    leases = [lease for lease in _as_list(state.get("leases")) if isinstance(lease, Mapping)]
    try:
        run_state = _load_json(run_state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        run_state = {}
    run_status = str(run_state.get("status") or "").strip().lower() if isinstance(run_state, Mapping) else ""
    scheduling_active = run_status not in {
        "paused", "failed", "blocked", "blocked_rethink", "complete", "completed", "complete_with_limits"
    }

    # Entity Resolution owns its lease ledger, while the Coordinator owns
    # top-level Analytical Owner and other role dispatches.  Count both
    # durable authorities and de-duplicate a resolver recorded in both.
    dispatches: list[Mapping[str, Any]] = []
    lifecycle_active: dict[str, str | None] = {}
    coordinator_path = state_path.parent.parent / "control_plane" / "coordinator_state.json"
    if scheduling_active and coordinator_path.is_file() and not coordinator_path.is_symlink():
        try:
            resolved_coordinator = coordinator_path.resolve(strict=True)
            if _is_within(resolved_coordinator, allowed_roots):
                coordinator_state = _load_json(resolved_coordinator)
                if isinstance(coordinator_state, Mapping):
                    dispatches = [
                        value for value in _as_list(coordinator_state.get("active_dispatches"))
                        if isinstance(value, Mapping)
                    ]
        except (OSError, ValueError, json.JSONDecodeError):
            dispatches = []
    lifecycle_path = state_path.parent.parent / "control_center" / "lifecycle_events.jsonl"
    if scheduling_active and lifecycle_path.is_file() and not lifecycle_path.is_symlink():
        try:
            resolved_lifecycle = lifecycle_path.resolve(strict=True)
            if _is_within(resolved_lifecycle, allowed_roots):
                for _, row in _tail_jsonl(resolved_lifecycle):
                    invocation_id = row.get("invocation_id") or row.get("invocationId") or row.get("agent_id") or row.get("agentId")
                    if not isinstance(invocation_id, str) or not invocation_id.strip() or len(invocation_id) > 120:
                        continue
                    event_type = str(row.get("event_type") or row.get("eventType") or row.get("type") or "").lower()
                    role = row.get("agent_type") or row.get("agentType") or row.get("role")
                    safe_role = str(role) if isinstance(role, str) and len(role) <= 120 else None
                    if any(token in event_type for token in ("complete", "finish", "fail")):
                        lifecycle_active.pop(invocation_id, None)
                    elif any(token in event_type for token in ("start", "spawn", "progress")):
                        lifecycle_active[invocation_id] = safe_role
        except (OSError, ValueError, json.JSONDecodeError):
            lifecycle_active = {}

    def capacity_int(key: str) -> int | None:
        value = capacity.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    active_keys: dict[str, set[str]] = {
        "entity_resolution": set(), "analytical_owner": set(), "specialist": set()
    }
    uncategorized_active: set[str] = set()

    def normalized_role(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().lower().replace("-", "_").replace(" ", "_")

    def capacity_type(role: str) -> str | None:
        return {
            "entity_resolution": "entity_resolution",
            "entity_resolution_owner": "entity_resolution",
            "identity_reviewer": "entity_resolution",
            "analytical_owner": "analytical_owner",
            "specialist": "specialist",
        }.get(role)

    if scheduling_active:
        for index, lease in enumerate(leases):
            role = normalized_role(lease.get("worker_type"))
            worker_type = capacity_type(role)
            if worker_type not in active_keys:
                continue
            identity = lease.get("subject_id") or lease.get("owner_ref") or lease.get("lease_id") or f"lease-{index}"
            active_keys[worker_type].add(f"{role}:{identity}")
        for index, dispatch in enumerate(dispatches):
            action = dispatch.get("action") if isinstance(dispatch.get("action"), Mapping) else {}
            role = normalized_role(action.get("role") or dispatch.get("role"))
            if role == "planner":
                continue
            worker_type = capacity_type(role)
            subject = action.get("subject_id") or dispatch.get("subject_id")
            identity = subject or dispatch.get("idempotency_key") or dispatch.get("slot_key") or f"dispatch-{index}"
            key = f"{role or 'unknown'}:{identity}"
            if worker_type is None:
                uncategorized_active.add(key)
            else:
                active_keys[worker_type].add(key)
        for invocation_id, lifecycle_role in lifecycle_active.items():
            role = normalized_role(lifecycle_role)
            if role == "planner":
                continue
            worker_type = capacity_type(role)
            key = f"lifecycle:{invocation_id}"
            if worker_type is None:
                uncategorized_active.add(key)
            else:
                active_keys[worker_type].add(key)
    active_by_type = {worker_type: len(values) for worker_type, values in active_keys.items()}
    active_total = sum(active_by_type.values()) + len(uncategorized_active)
    return {
        "total": capacity_int("total_active"),
        "active": active_total,
        "entityResolution": capacity_int("entity_resolution"),
        "entityResolutionActive": active_by_type["entity_resolution"],
        "analyticalOwner": capacity_int("analytical_owner"),
        "analyticalOwnerActive": active_by_type["analytical_owner"],
        "specialist": capacity_int("specialist"),
        "specialistActive": active_by_type["specialist"],
        "plannerExcluded": True,
        "source": "+".join(
            ["entity_resolution_state"]
            + (["coordinator_state"] if dispatches else [])
            + (["lifecycle_events"] if lifecycle_active else [])
        ),
    }


def _classify_event(event_type: str) -> tuple[str, str]:
    normalized = event_type.lower()
    if any(word in normalized for word in ("fail", "error", "incident")):
        return "error", "error"
    if any(word in normalized for word in ("review", "verify", "approval")):
        return "review", "review"
    if any(word in normalized for word in ("write", "created", "published", "committed")):
        return "artifact", "completed"
    if any(word in normalized for word in ("file", "read", "catalog", "dataset", "member")):
        return "file", "active"
    if any(word in normalized for word in ("wait", "blocked", "pause")):
        return "dependency", "waiting"
    if any(word in normalized for word in ("complete", "finish", "success", "accepted")):
        return "lifecycle", "completed"
    if any(word in normalized for word in ("start", "begin", "claim", "dispatch", "lease")):
        return "lifecycle", "active"
    return "system", "neutral"


def _normalize_event(raw: Mapping[str, Any], cursor: int, stream_id: str = "fixture") -> dict[str, Any]:
    facts = _allowlisted_facts(raw)
    event_type = _safe_text(
        _coalesce(raw, "event_type", "type", "event", "name", default="telemetry_event"),
        100,
    )
    category, status = _classify_event(event_type)
    timestamp = _safe_text(
        _coalesce(raw, "timestamp", "recorded_at", "created_at", "at", default=""), 80
    )
    role = _safe_text(
        _coalesce(raw, "role", "agent_role", default=_coalesce(facts, "role", "agent_role", "worker_type", default="")),
        80,
    )
    item_id = _safe_text(
        _coalesce(
            raw,
            "item_id",
            "requirement_id",
            "goal_id",
            default=_coalesce(facts, "item_id", "requirement_id", "goal_id", "domain_id", "subject_id", default=""),
        ),
        100,
    )
    path = _safe_text(
        _coalesce(
            raw,
            "path",
            "file_path",
            "source_path",
            default=_coalesce(facts, "member_path", "path", "file_path", "source_path", default=""),
        ),
        500,
    )
    rows = _coalesce(raw, "rows", "row_count", default=_coalesce(facts, "rows", "row_count"))
    artifact = _safe_text(
        _coalesce(raw, "artifact", "artifact_path", default=_coalesce(facts, "artifact", "artifact_path", default="")),
        500,
    )
    summary_parts = [event_type.replace("_", " ")]
    if path:
        summary_parts.append(Path(path).name)
    if rows not in (None, ""):
        summary_parts.append(f"{rows} rows")
    if artifact:
        summary_parts.append(Path(artifact).name)
    return {
        "id": f"event-{stream_id}-{cursor}",
        "cursor": cursor,
        "streamId": stream_id,
        "timestamp": timestamp,
        "type": event_type,
        "category": category,
        "status": status,
        "role": role,
        "itemId": item_id,
        "path": path,
        "rows": rows,
        "artifact": artifact,
        "summary": " · ".join(summary_parts),
        "details": _safe_event_details(raw, facts),
    }


def _summarize_run_state(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    items = _as_list(payload.get("items"))
    requirements = _as_list(payload.get("requirements"))
    updated = _safe_text(
        _coalesce(
            payload,
            "updated_at",
            "updatedAt",
            "last_updated_at",
            "lastUpdatedAt",
            "created_at",
            "createdAt",
            default="",
        ),
        80,
    )
    return {
        "id": _run_id_for_path(path),
        "name": _safe_text(
            _coalesce(payload, "name", "run_name", "run_id", default=path.parent.name), 140
        ),
        "status": _safe_text(_coalesce(payload, "status", "lifecycle_state", default="unknown"), 60),
        "updatedAt": updated,
        "requirementCount": len(requirements) or len(items),
        "runStatePath": str(path),
        "source": "filesystem",
        "readOnly": True,
        "protected": True,
    }


@dataclass(frozen=True)
class RunRecord:
    summary: dict[str, Any]
    state_path: Path | None
    fixture: dict[str, Any] | None = None


class ReadOnlyRepository:
    """Bounded read projection over fixtures and explicitly allowed roots."""

    def __init__(self, fixture_path: Path | None, run_roots: Iterable[Path]) -> None:
        self.fixture_path = fixture_path.resolve() if fixture_path else None
        self.run_roots = tuple(root.resolve() for root in run_roots)
        self._instance_id = uuid.uuid4().hex[:10]
        self._stream_cache: dict[Path, tuple[tuple[int, int], int, int, str]] = {}

    @staticmethod
    def _stream_anchor(path: Path, size: int) -> str:
        if size <= 0:
            return hashlib.sha256(b"").hexdigest()
        length = min(256, size)
        with path.open("rb") as handle:
            handle.seek(size - length)
            return hashlib.sha256(handle.read(length)).hexdigest()

    def _stream_info(self, path: Path) -> tuple[str, int]:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
        identity = (stat.st_dev, stat.st_ino)
        prior = self._stream_cache.get(resolved)
        generation = 0
        if prior is not None:
            prior_identity, prior_size, prior_generation, prior_anchor = prior
            replaced = identity != prior_identity or stat.st_size < prior_size
            if not replaced and prior_size > 0:
                replaced = self._stream_anchor(resolved, prior_size) != prior_anchor
            generation = prior_generation + 1 if replaced else prior_generation
        anchor = self._stream_anchor(resolved, stat.st_size)
        self._stream_cache[resolved] = (identity, stat.st_size, generation, anchor)
        return f"{self._instance_id}-{identity[0]:x}-{identity[1]:x}-g{generation}", stat.st_size

    def _fixture_record(self) -> RunRecord | None:
        if not self.fixture_path:
            return None
        fixture = _load_json(self.fixture_path)
        run = dict(fixture.get("run") or {})
        run.setdefault("id", "fixture-mission")
        run.setdefault("name", "Fixture · Multi-agent mission")
        run.setdefault("status", "running")
        run.setdefault("source", "fixture")
        run.setdefault("readOnly", True)
        run.setdefault("protected", True)
        run.setdefault("updatedAt", _utc_now())
        run.setdefault("requirementCount", len(fixture.get("requirements") or []))
        run.setdefault("capacity", fixture.get("capacity"))
        return RunRecord(summary=run, state_path=None, fixture=fixture)

    def _filesystem_records(self) -> list[RunRecord]:
        # Discover every candidate before applying the cap.  A filesystem walk
        # is not ordered by durable update time, so stopping after the first
        # ``MAX_DISCOVERED_RUNS`` paths could hide the newest runs.  Keep only
        # the newest bounded heap entries while scanning to avoid retaining an
        # unbounded number of parsed records.
        newest: list[tuple[str, str, int, RunRecord]] = []
        seen: set[Path] = set()
        sequence = 0
        for root in self.run_roots:
            if not root.is_dir():
                continue
            for state_path in _iter_run_state_files(root):
                resolved = state_path.resolve()
                if resolved in seen or not _is_within(resolved, self.run_roots):
                    continue
                seen.add(resolved)
                try:
                    payload = _load_json(resolved)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                summary = _summarize_run_state(resolved, payload)
                summary["capacity"] = _capacity_projection(resolved, self.run_roots)
                record = RunRecord(
                    summary=summary,
                    state_path=resolved,
                )
                updated = str(summary.get("updatedAt", "") or "")
                # The path is a deterministic tie-breaker after durable
                # recency; sequence only prevents heap comparisons between
                # otherwise identical keys.
                rank = (updated, str(resolved), sequence, record)
                sequence += 1
                if len(newest) < MAX_DISCOVERED_RUNS:
                    heapq.heappush(newest, rank)
                elif rank[:3] > newest[0][:3]:
                    heapq.heapreplace(newest, rank)
        records = [entry[3] for entry in newest]
        records.sort(
            key=lambda record: (
                str(record.summary.get("updatedAt", "") or ""),
                str(record.summary.get("id", "") or ""),
            ),
            reverse=True,
        )
        return records[:MAX_DISCOVERED_RUNS]

    def records(self) -> list[RunRecord]:
        records: list[RunRecord] = []
        fixture = self._fixture_record()
        if fixture:
            records.append(fixture)
        records.extend(self._filesystem_records())
        return records

    def list_runs(self) -> list[dict[str, Any]]:
        return [record.summary for record in self.records()]

    def get(self, run_id: str) -> RunRecord | None:
        return next((record for record in self.records() if record.summary["id"] == run_id), None)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        if not record:
            raise KeyError(run_id)
        if record.fixture is not None:
            fixture = json.loads(json.dumps(record.fixture))
            fixture["observedAt"] = _utc_now()
            telemetry = fixture.setdefault("telemetry", {})
            telemetry.setdefault("streamId", "fixture-mission-v1")
            telemetry.setdefault(
                "nextCursor",
                max([0, *[int(event.get("cursor", 0)) for event in fixture.get("events", [])]]),
            )
            return fixture
        assert record.state_path is not None
        telemetry_path = _find_telemetry(record.state_path, self.run_roots)
        stream_id, _ = self._stream_info(telemetry_path) if telemetry_path else (None, 0)
        raw_events = _tail_jsonl(telemetry_path) if telemetry_path else []
        events = [_normalize_event(event, cursor, stream_id or "no-stream") for cursor, event in raw_events]
        latest_events = events[-300:]
        return {
            "schemaVersion": "control-center-snapshot-v1",
            "run": record.summary,
            "capacity": record.summary.get("capacity"),
            "nodes": [
                {
                    "id": "planner",
                    "role": "planner",
                    "label": "Planner",
                    "objective": "Runtime control plane",
                    "status": record.summary.get("status", "unknown"),
                    "lane": "planner",
                    "active": record.summary.get("status") == "running",
                }
            ],
            "edges": [],
            "events": latest_events,
            "trace": [],
            "observedAt": _utc_now(),
            "telemetry": {
                "path": str(telemetry_path) if telemetry_path else None,
                "streamId": stream_id,
                "nextCursor": raw_events[-1][0] if raw_events else 0,
                "eventCountInBoundedTail": len(events),
                "truncatedToLatest": len(latest_events),
            },
            "limitations": [
                "This run is attached read-only.",
                "Only relationships explicitly recorded in durable state or telemetry are shown.",
                "Exact historical agent spawn lineage is unavailable when invocation receipts are absent.",
                "The event reader uses a bounded tail and may not include the full run history.",
            ],
        }
    def events_after(self, run_id: str, cursor: int, stream_id: str = "") -> dict[str, Any]:
        record = self.get(run_id)
        if not record:
            raise KeyError(run_id)
        if record.fixture is not None:
            current_stream = "fixture-mission-v1"
            reset = bool(stream_id and stream_id != current_stream)
            effective_cursor = 0 if reset else cursor
            events = [
                event
                for event in record.fixture.get("events", [])
                if int(event.get("cursor", 0)) > effective_cursor
            ][:MAX_EVENT_PAGE_ITEMS]
            next_cursor = max(
                [effective_cursor, *[int(event.get("cursor", 0)) for event in events]]
            )
            maximum = max(
                [0, *[int(event.get("cursor", 0)) for event in record.fixture.get("events", [])]]
            )
            return {
                "events": events,
                "nextCursor": next_cursor,
                "streamId": current_stream,
                "reset": reset,
                "hasMore": next_cursor < maximum,
                "observedAt": _utc_now(),
            }

        assert record.state_path is not None
        telemetry_path = _find_telemetry(record.state_path, self.run_roots)
        if telemetry_path is None:
            return {
                "events": [],
                "nextCursor": 0,
                "streamId": None,
                "reset": bool(stream_id),
                "hasMore": False,
                "observedAt": _utc_now(),
            }
        current_stream, size = self._stream_info(telemetry_path)
        reset = bool(stream_id and stream_id != current_stream) or cursor > size
        effective_cursor = 0 if reset else cursor
        raw_events, next_cursor, has_more = _jsonl_page(telemetry_path, effective_cursor)
        events = [
            _normalize_event(event, event_cursor, current_stream)
            for event_cursor, event in raw_events
        ]
        return {
            "events": events,
            "nextCursor": next_cursor,
            "streamId": current_stream,
            "reset": reset,
            "hasMore": has_more,
            "observedAt": _utc_now(),
        }
