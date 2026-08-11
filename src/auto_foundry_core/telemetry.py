"""Passive facts-only telemetry for deterministic local runs."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Iterator, Mapping

from .contracts import IncidentRecord, OperationReceipt, PhaseTimingRecord, RunTelemetrySummary, TelemetryEvent
from .workspace import RunContext


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_PHASES = frozenset(
    {
        "analyst_model",
        "controlled_execution",
        "business_review",
        "business_repair",
        "fidelity_integration_review",
        "integration_commit",
        "products",
        "optimizer",
        "reporting_finalization",
        "genuine_recovery",
    }
)

_PHASE_ALIASES = {
    "analyst/model work": "analyst_model",
    "analyst/model": "analyst_model",
    "analyst_model_work": "analyst_model",
    "controlled execution": "controlled_execution",
    "business review": "business_review",
    "business repair": "business_repair",
    "fidelity/integration review": "fidelity_integration_review",
    "fidelity integration review": "fidelity_integration_review",
    "integration commit": "integration_commit",
    "products": "products",
    "optimizer": "optimizer",
    "reporting/finalization": "reporting_finalization",
    "reporting finalization": "reporting_finalization",
    "genuine recovery": "genuine_recovery",
    "recovery": "genuine_recovery",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _incident_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:16]


def _normalize_incident(value: IncidentRecord | Mapping[str, Any]) -> IncidentRecord:
    if isinstance(value, IncidentRecord):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("incident must be an IncidentRecord or mapping")
    raw = dict(value)
    category = raw.pop("category", raw.pop("kind", raw.pop("type", None)))
    if category is None:
        raise ValueError("incident category is required")
    disposition = raw.pop("disposition", raw.pop("resolution", raw.pop("status", None)))
    if disposition is None:
        raise ValueError("incident disposition is required")
    admissible = raw.pop("admissible", raw.pop("admissible_for_report", None))
    if not isinstance(admissible, bool):
        raise TypeError("incident admissible must be a bool")
    incident_id = raw.pop("incident_id", raw.pop("id", None))
    item_id = raw.pop("item_id", None)
    scope = raw.pop("scope", raw.pop("affected_scope", ()))
    if isinstance(scope, str):
        scope = (scope,)
    source = raw.pop("source", raw.pop("source_ref", None))
    facts = raw.pop("facts", None)
    if facts is None:
        facts = raw
    elif raw:
        facts = {**dict(facts), **raw} if isinstance(facts, Mapping) else raw
    canonical_without_id = {
        "category": str(category).strip().lower(),
        "disposition": str(disposition).strip(),
        "admissible": admissible,
        "item_id": item_id,
        "scope": list(scope or ()),
        "source": source,
        "facts": dict(facts or {}) if isinstance(facts, Mapping) else {},
    }
    incident_id = str(incident_id).strip() if incident_id is not None else f"INC-{_incident_digest(canonical_without_id)}"
    return IncidentRecord(
        incident_id=incident_id,
        category=canonical_without_id["category"],
        disposition=canonical_without_id["disposition"],
        admissible=admissible,
        item_id=item_id,
        scope=tuple(scope or ()),
        source=source,
        facts=canonical_without_id["facts"],
    )


class TelemetryRecorder:
    """Collect observations without enforcing budgets or fabricating facts."""

    def __init__(self, root: str | Path | None = None, *, run_id: str | None = None, context: RunContext | None = None) -> None:
        if isinstance(root, RunContext) and context is None:
            context = root
            root = None
        if context is not None:
            root = context.resolve_run_path("telemetry") if root is None else context.resolve_run_path(root)
            run_id = run_id or context.run_id
        self.context = context
        self.root = Path(root).expanduser().resolve(strict=False) if root is not None else None
        self.storage_available = False
        self.dropped_events = 0
        self.write_errors: list[str] = []
        if self.root is not None:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self.storage_available = True
            except Exception as exc:  # telemetry must never block the caller
                self._note_write_error(exc)
        self.run_id = run_id or "run"
        self.started_at = _now()
        self._started_clock = time.perf_counter()
        self.events: list[TelemetryEvent] = []
        self.phase_timings: list[PhaseTimingRecord] = []
        self.incidents: dict[str, IncidentRecord] = {}
        self._invocation_ledger = None

    @property
    def event_path(self) -> Path | None:
        return self.root / "events.jsonl" if self.root else None

    def record(self, event_type: str, *, timestamp: str | None = None, **facts: Any) -> TelemetryEvent:
        known = {
            "capability_id", "spec_hash", "input_hashes", "output_hashes", "duration_ms",
            "rows", "bytes_processed", "cache_status", "error",
        }
        kwargs = {key: facts.pop(key) for key in list(facts) if key in known}
        supplied_facts = facts.pop("facts", None)
        if isinstance(supplied_facts, Mapping):
            facts = {**dict(supplied_facts), **facts}
        event = TelemetryEvent(event_type=event_type, timestamp=timestamp or _now(), facts=facts, **kwargs)
        self.events.append(event)
        path = self.event_path
        if path is not None:
            try:
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(event.to_json() + "\n")
            except Exception as exc:  # passive telemetry: keep the in-memory fact
                self.dropped_events += 1
                self._note_write_error(exc)
        return event

    def _note_write_error(self, error: Exception) -> None:
        self.storage_available = False
        self.write_errors.append(f"{type(error).__name__}: {error}")

    def record_operation(self, receipt: OperationReceipt) -> TelemetryEvent:
        return self.record(
            "operation",
            capability_id=receipt.capability_id,
            spec_hash=receipt.spec_hash,
            input_hashes=receipt.input_hashes,
            output_hashes=receipt.output_hashes,
            duration_ms=receipt.duration_ms,
            cache_status=receipt.cache_status,
            error="; ".join(receipt.errors) if receipt.errors else None,
            facts={"backend": receipt.backend, "limitations": list(receipt.limitations)},
        )

    def record_phase(
        self,
        phase: str,
        *,
        start: str | None = None,
        finish: str | None = None,
        wall_time_ms: float | None = None,
        item_id: str | None = None,
        attempt_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        receipt_ref: str | None = None,
        facts: Mapping[str, Any] | None = None,
    ) -> PhaseTimingRecord:
        """Record one observed phase interval without inventing missing facts."""

        phase_value = str(phase).strip().lower()
        phase_value = _PHASE_ALIASES.get(phase_value, phase_value.replace("-", "_"))
        if phase_value not in _PHASES:
            raise ValueError(f"unsupported phase: {phase_value}")
        observed_wall = wall_time_ms
        if observed_wall is None and start is not None and finish is not None:
            try:
                started_at = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                finished_at = datetime.fromisoformat(str(finish).replace("Z", "+00:00"))
                elapsed_ms = (finished_at - started_at).total_seconds() * 1000
                if elapsed_ms >= 0:
                    observed_wall = elapsed_ms
            except (TypeError, ValueError):
                # The original timestamps remain authoritative; an
                # unparseable pair simply leaves the derived duration
                # unavailable rather than inventing one.
                observed_wall = None
        record = PhaseTimingRecord(
            phase=phase_value,
            start=start,
            finish=finish,
            wall_time_ms=observed_wall,
            item_id=item_id,
            attempt_id=attempt_id,
            provider=provider,
            model=model,
            receipt_ref=receipt_ref,
            facts=facts or {},
        )
        self.phase_timings.append(record)
        self.record(
            "phase_timing",
            timestamp=record.start or record.finish,
            facts=record.to_dict(),
        )
        return record

    def record_incident(self, incident: IncidentRecord | Mapping[str, Any]) -> IncidentRecord:
        """Normalize and de-duplicate a reportable incident by stable ID."""

        normalized = _normalize_incident(incident)
        existing = self.incidents.get(normalized.incident_id)
        if existing is not None:
            if existing != normalized:
                raise ValueError(f"incident_id {normalized.incident_id!r} is already recorded with different facts")
            return existing
        self.incidents[normalized.incident_id] = normalized
        self.record("incident", facts=normalized.to_dict())
        return normalized

    def record_incidents(self, incidents: Any) -> tuple[IncidentRecord, ...]:
        return tuple(self.record_incident(value) for value in incidents or ())

    @property
    def invocation_ledger(self):
        """Lazily expose the run-local invocation ledger when context-bound."""

        if self.context is None:
            return None
        if self._invocation_ledger is None:
            from .lifecycle import InvocationReceiptLedger

            try:
                self._invocation_ledger = InvocationReceiptLedger(context=self.context)
            except Exception as exc:
                # Receipt persistence is passive telemetry.  A ledger failure
                # must not change the operation outcome.
                self._note_write_error(exc)
                return None
        return self._invocation_ledger

    def record_invocation(self, receipt: Any) -> Any:
        """Passively append a completed invocation receipt when storage works."""

        ledger = self.invocation_ledger
        if ledger is None:
            return receipt
        try:
            return ledger.append(receipt)
        except Exception as exc:
            self._note_write_error(exc)
            return receipt

    # Explicit name for callers that want to make the passive boundary clear.
    record_invocation_receipt = record_invocation

    @contextmanager
    def operation(self, capability_id: str, *, spec_hash: str | None = None, input_hashes=()) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        context: dict[str, Any] = {}
        try:
            yield context
        except Exception as exc:
            self.record("operation", capability_id=capability_id, spec_hash=spec_hash, input_hashes=tuple(input_hashes), duration_ms=(time.perf_counter() - started) * 1000, error=str(exc), facts={"failed": True})
            raise
        else:
            self.record("operation", capability_id=capability_id, spec_hash=spec_hash, input_hashes=tuple(input_hashes), output_hashes=tuple(context.get("output_hashes", ())), duration_ms=(time.perf_counter() - started) * 1000, rows=context.get("rows"), bytes_processed=context.get("bytes_processed"), cache_status=context.get("cache_status"), facts={k: v for k, v in context.items() if k not in {"output_hashes", "rows", "bytes_processed", "cache_status"}})

    def summary(self, *, ended_at: str | None = None, extra: Mapping[str, Any] | None = None) -> RunTelemetrySummary:
        end = ended_at or _now()
        capability_usage = Counter(event.capability_id for event in self.events if event.capability_id)
        cache_hits = sum(event.event_type == "cache_hit" for event in self.events)
        cache_misses = sum(event.event_type == "cache_miss" for event in self.events)
        bytes_read = sum(event.bytes_processed or 0 for event in self.events if event.event_type in {"source_read", "operation"})
        files_read = sum(event.event_type == "source_read" for event in self.events)
        facts = dict(extra or {})
        phase_payload = [record.to_dict() for record in self.phase_timings]
        phase_totals: dict[str, dict[str, Any]] = {}
        for record in self.phase_timings:
            aggregate = phase_totals.setdefault(
                record.phase,
                {"count": 0, "wall_time_ms": None, "start": None, "finish": None},
            )
            aggregate["count"] += 1
            if record.wall_time_ms is not None:
                aggregate["wall_time_ms"] = (aggregate["wall_time_ms"] or 0.0) + record.wall_time_ms
            if aggregate["start"] is None and record.start is not None:
                aggregate["start"] = record.start
            if record.finish is not None:
                aggregate["finish"] = record.finish
        incident_payload = [incident.to_dict() for incident in self.incidents.values()]
        incident_totals = Counter(incident.category for incident in self.incidents.values())
        facts.setdefault("phase_timings", phase_payload)
        facts.setdefault("phase_timing_totals", phase_totals)
        facts.setdefault("incidents", incident_payload)
        facts.setdefault("incident_totals", dict(incident_totals))
        facts.setdefault("incident_count", len(incident_payload))
        facts.setdefault("telemetry_storage", "available" if self.storage_available else ("disabled" if self.root is None else "unavailable"))
        facts.setdefault("telemetry_write_errors", len(self.write_errors))
        facts.setdefault("telemetry_dropped_events", self.dropped_events)
        return RunTelemetrySummary(
            run_id=self.run_id,
            started_at=self.started_at,
            ended_at=end,
            wall_time_ms=(time.perf_counter() - self._started_clock) * 1000,
            model_calls="unavailable",
            model_wall_ms="unavailable",
            tool_calls="unavailable",
            files_read=files_read,
            bytes_read=bytes_read,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            capability_usage=dict(capability_usage),
            custom_script_count="unavailable",
            custom_script_loc="unavailable",
            facts=facts,
        )

    def write_summary(self, path: str | Path | None = None, *, extra: Mapping[str, Any] | None = None) -> RunTelemetrySummary:
        summary = self.summary(extra=extra)
        if path is not None and self.context is not None:
            # Context-bound summaries are system-owned run artifacts.  Resolve
            # before mkdir/write so an escape remains a technical path error.
            destination = self.context.resolve_run_path(path)
        else:
            destination = Path(path) if path is not None else (self.root / "summary.json" if self.root else None)
        if destination is not None:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(summary.to_json() + "\n", encoding="utf-8")
            except Exception as exc:  # summary persistence is observational only
                self.dropped_events += 1
                self._note_write_error(exc)
        return summary


__all__ = ["TelemetryRecorder"]
