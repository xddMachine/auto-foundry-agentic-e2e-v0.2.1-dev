"""Program-owned cumulative reporting and terminalization.

The projector accepts only authoritative item reports/manifests and registry
snapshots supplied by callers.  It never imports private integration or LEM
state, which keeps report recomputation independent from result integration.
The finalizer publishes a self-excluding canonical manifest and a receipt that
binds both the report and manifest hashes.
"""

from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping

from .contracts import IncidentRecord, ImplementationTransition, PhaseTimingRecord
from .telemetry import _normalize_incident


_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_DIRNAME = "reporting"
_REPORT_FILENAME = "final_report.json"
_MANIFEST_FILENAME = "run_manifest.json"
_RECEIPT_FILENAME = "terminalization_receipt.json"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _hash_mapping(value: Mapping[str, Any], field: str) -> str:
    unsigned = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_json_bytes(unsigned)).hexdigest()


def _validate_sha40(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA40.fullmatch(value):
        raise ValueError(f"{label} must be exactly 40 lowercase hexadecimal characters")
    return value


def _validate_optional_sha256(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _as_records(value: Any, *, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        if "item_id" in value or "outcome" in value or "state" in value:
            return [value]
        records: list[Mapping[str, Any]] = []
        for item_id, record in value.items():
            if not isinstance(record, Mapping):
                raise TypeError(f"{label} records must be mappings")
            records.append({"item_id": item_id, **dict(record)})
        return records
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be a sequence of mappings")
    records = list(value)
    if any(not isinstance(record, Mapping) for record in records):
        raise TypeError(f"{label} records must be mappings")
    return records


def _item_outcome(value: Mapping[str, Any]) -> str | None:
    state = value.get("state", value.get("item_state"))
    if isinstance(state, Mapping):
        terminal = state.get("terminal_outcome")
        if isinstance(terminal, Mapping) and isinstance(terminal.get("outcome"), str):
            return terminal["outcome"]
        lifecycle = state.get("lifecycle_state")
        if lifecycle in {"accepted", "accepted_with_limits", "technical_failure"}:
            return lifecycle
    terminal = value.get("terminal_outcome")
    if isinstance(terminal, Mapping) and isinstance(terminal.get("outcome"), str):
        return terminal["outcome"]
    outcome = value.get("outcome", value.get("status"))
    return str(outcome) if outcome in {"accepted", "accepted_with_limits", "technical_failure"} else None


def _item_lifecycle(value: Mapping[str, Any]) -> str | None:
    state = value.get("state", value.get("item_state"))
    if isinstance(state, Mapping):
        lifecycle = state.get("lifecycle_state")
        if lifecycle is not None:
            return str(lifecycle)
    lifecycle = value.get("lifecycle_state")
    return str(lifecycle) if lifecycle is not None else None


def _implementation(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = value.get("implementation", value.get("implementation_metadata"))
    if isinstance(raw, str):
        raw = {"sha": raw}
    raw = dict(raw or {}) if isinstance(raw, Mapping) else {}
    if not raw:
        raw = {
            key: value[key]
            for key in (
                "sha",
                "implementation_sha",
                "commit_sha",
                "tree",
                "tree_sha",
                "implementation_tree",
                "version",
                "implementation_version",
            )
            if key in value
        }
    if "implementation_version" in raw and "version" not in raw:
        raw["version"] = raw["implementation_version"]
    for key in ("sha", "implementation_sha", "commit_sha"):
        if key in raw:
            raw["sha"] = raw[key]
            break
    for key in ("tree", "tree_sha", "implementation_tree"):
        if key in raw:
            raw["tree"] = raw[key]
            break
    if raw.get("sha") is None or raw.get("tree") is None or raw.get("version") is None:
        raise ValueError("implementation requires exact sha, tree, and version")
    if not isinstance(raw["version"], str) or not raw["version"].strip():
        raise ValueError("implementation version must be non-empty")
    result = {
        "sha": _validate_sha40(raw["sha"], "implementation SHA"),
        "tree": _validate_sha40(raw["tree"], "implementation tree"),
        "version": raw["version"].strip(),
    }
    return result


def _record_id(value: Mapping[str, Any], keys: tuple[str, ...], prefix: str) -> str:
    for key in keys:
        if value.get(key) is not None:
            candidate = str(value[key]).strip()
            if candidate:
                return candidate
    return prefix + hashlib.sha256(_json_bytes(value)).hexdigest()[:16]


def _require_item_id(value: Any, *, label: str, item_ids: set[str]) -> str:
    item_id = str(value or "").strip()
    if not item_id:
        raise ValueError(f"{label} item_id is required")
    if item_ids and item_id not in item_ids:
        raise ValueError(f"{label} references unknown item_id {item_id!r}")
    return item_id


def _records(value: Any, *, label: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)) or (hasattr(value, "to_dict") and callable(value.to_dict)):
        # A mapping is a single record for the explicit record inputs.  The
        # item-report mapping form is intentionally handled by _as_records.
        return [value]
    return list(value)


def _normalise_receipts(value: Any, *, item_ids: set[str]) -> list[dict[str, Any]]:
    """Preserve every invocation receipt while validating its identity/link."""

    normalized: dict[str, dict[str, Any]] = {}
    for raw in _records(value, label="receipts"):
        payload = _jsonable(raw)
        if not isinstance(payload, Mapping):
            raise TypeError("receipts must be mappings or receipt contracts")
        payload = dict(payload)
        receipt_id = payload.get("invocation_id", payload.get("receipt_id", payload.get("id")))
        receipt_id = str(receipt_id or "").strip()
        if not receipt_id:
            raise ValueError("receipt invocation_id is required")
        payload["invocation_id"] = receipt_id
        _require_item_id(payload.get("item_id"), label="receipt", item_ids=item_ids)
        if receipt_id in normalized:
            raise ValueError(f"receipt {receipt_id!r} appears more than once")
        normalized[receipt_id] = payload
    return [normalized[key] for key in sorted(normalized)]


def _normalise_timings(
    value: Any,
    *,
    item_ids: set[str],
    receipt_ids: set[str],
) -> list[dict[str, Any]]:
    """Keep passive timing facts verbatim, rejecting ambiguous identities."""

    normalized: dict[str, dict[str, Any]] = {}
    for raw in _records(value, label="timings"):
        payload = _jsonable(raw)
        if not isinstance(payload, Mapping):
            raise TypeError("timings must be mappings or timing contracts")
        payload = dict(payload)
        phase = str(payload.get("phase", "")).strip()
        if not phase:
            raise ValueError("timing phase is required")
        payload["phase"] = phase
        if payload.get("item_id") is not None:
            _require_item_id(payload.get("item_id"), label="timing", item_ids=item_ids)
        if payload.get("wall_time_ms") is not None:
            wall = payload["wall_time_ms"]
            if isinstance(wall, bool) or not isinstance(wall, (int, float)) or wall < 0:
                raise ValueError("timing wall_time_ms must be nonnegative or null")
            payload["wall_time_ms"] = float(wall)
        receipt_ref = payload.get("receipt_ref")
        if receipt_ref is not None:
            receipt_ref = str(receipt_ref).strip()
            if not receipt_ref:
                raise ValueError("timing receipt_ref cannot be empty")
            if receipt_ids:
                ref_id = receipt_ref.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
                if receipt_ref not in receipt_ids and ref_id not in receipt_ids:
                    raise ValueError(f"timing receipt_ref {receipt_ref!r} is not linked to a receipt")
            else:
                raise ValueError(f"timing receipt_ref {receipt_ref!r} has no authoritative receipt")
            payload["receipt_ref"] = receipt_ref
        stable_id = payload.get("timing_id", payload.get("phase_id"))
        stable_id = str(stable_id or "").strip()
        digest = hashlib.sha256(_json_bytes(payload)).hexdigest()
        key = stable_id or f"timing-{digest}"
        if key in normalized:
            raise ValueError(f"timing {key!r} appears more than once")
        normalized[key] = payload
    return [normalized[key] for key in sorted(normalized)]


def _normalise_incidents(value: Any, *, item_ids: set[str] | None = None) -> list[dict[str, Any]]:
    normalized: dict[str, IncidentRecord] = {}
    for raw in _records(value, label="incidents"):
        incident = _normalize_incident(raw)
        if incident.item_id is not None:
            _require_item_id(incident.item_id, label="incident", item_ids=item_ids or set())
        existing = normalized.get(incident.incident_id)
        if existing is not None and existing != incident:
            raise ValueError(f"incident {incident.incident_id!r} appears with conflicting facts")
        normalized[incident.incident_id] = incident
    return [normalized[key].to_dict() for key in sorted(normalized)]


def _normalise_review_records(value: Any, *, kind: str, item_ids: set[str]) -> list[dict[str, Any]]:
    if kind not in {"business", "fidelity"}:
        raise ValueError(f"unsupported review kind: {kind}")
    normalized: dict[str, dict[str, Any]] = {}
    for raw in _records(value, label=f"{kind}_reviews"):
        payload = _jsonable(raw)
        if not isinstance(payload, Mapping):
            raise TypeError(f"{kind}_reviews must be mappings")
        payload = dict(payload)
        item_id = _require_item_id(payload.get("item_id"), label=f"{kind}_review", item_ids=item_ids)
        review_id = payload.get("review_id", payload.get("review_ref", payload.get("id")))
        review_id = str(review_id or "").strip()
        if not review_id:
            raise ValueError(f"{kind}_review review_id is required")
        supplied_kind = payload.get("review_kind", payload.get("kind"))
        if supplied_kind is not None and str(supplied_kind).strip().lower() != kind:
            raise ValueError(f"review {review_id!r} has the wrong review kind")
        payload["item_id"] = item_id
        payload["review_id"] = review_id
        payload["review_kind"] = kind
        findings = payload.get("findings", payload.get("review_findings", []))
        if findings is None:
            findings = []
        if isinstance(findings, Mapping) or isinstance(findings, (str, bytes)):
            raise TypeError(f"review {review_id!r} findings must be a sequence")
        normalized_findings: list[dict[str, Any]] = []
        seen_findings: set[str] = set()
        for finding in findings:
            finding_value = _jsonable(finding)
            if not isinstance(finding_value, Mapping):
                raise TypeError(f"review {review_id!r} findings must be mappings")
            finding_value = dict(finding_value)
            finding_id = finding_value.get("finding_id", finding_value.get("id"))
            finding_id = str(finding_id or "").strip()
            if not finding_id:
                raise ValueError(f"review {review_id!r} finding_id is required")
            if finding_id in seen_findings:
                raise ValueError(f"review {review_id!r} finding IDs are duplicated")
            seen_findings.add(finding_id)
            if finding_value.get("item_id") is not None:
                finding_item = _require_item_id(
                    finding_value.get("item_id"),
                    label=f"review {review_id!r} finding",
                    item_ids=item_ids,
                )
                if finding_item != item_id:
                    raise ValueError(f"review {review_id!r} finding item_id is inconsistent")
            finding_value["finding_id"] = finding_id
            for hash_key in (
                "draft_hash",
                "reviewed_draft_hash",
                "before_hash",
                "after_hash",
                "unchanged_aggregate_hash",
            ):
                if hash_key in finding_value:
                    _validate_optional_sha256(finding_value[hash_key], f"review {review_id!r} {hash_key}")
            normalized_findings.append(finding_value)
        normalized_findings.sort(key=lambda finding: finding["finding_id"])
        payload["findings"] = normalized_findings
        repairs = payload.get("repairs", payload.get("repair", []))
        if repairs is None:
            repairs = []
        if isinstance(repairs, Mapping):
            repairs = [repairs]
        if isinstance(repairs, (str, bytes)):
            raise TypeError(f"review {review_id!r} repairs must be a sequence")
        normalized_repairs: list[dict[str, Any]] = []
        for repair in repairs:
            repair_value = _jsonable(repair)
            if not isinstance(repair_value, Mapping):
                raise TypeError(f"review {review_id!r} repairs must be mappings")
            repair_value = dict(repair_value)
            if repair_value.get("item_id") is not None:
                repair_item = _require_item_id(
                    repair_value.get("item_id"),
                    label=f"review {review_id!r} repair",
                    item_ids=item_ids,
                )
                if repair_item != item_id:
                    raise ValueError(f"review {review_id!r} repair item_id is inconsistent")
            for hash_key in ("draft_hash", "before_hash", "after_hash", "unchanged_aggregate_hash"):
                if hash_key in repair_value:
                    _validate_optional_sha256(repair_value[hash_key], f"review {review_id!r} {hash_key}")
            normalized_repairs.append(repair_value)
        payload["repairs"] = normalized_repairs
        rechecks = payload.get("targeted_rechecks", payload.get("targeted_recheck", []))
        if rechecks is None:
            rechecks = []
        if isinstance(rechecks, Mapping):
            rechecks = [rechecks]
        if isinstance(rechecks, (str, bytes)):
            raise TypeError(f"review {review_id!r} targeted rechecks must be a sequence")
        normalized_rechecks: list[dict[str, Any]] = []
        for recheck in rechecks:
            recheck_value = _jsonable(recheck)
            if not isinstance(recheck_value, Mapping):
                raise TypeError(f"review {review_id!r} targeted rechecks must be mappings")
            recheck_value = dict(recheck_value)
            if recheck_value.get("item_id") is not None:
                recheck_item = _require_item_id(
                    recheck_value.get("item_id"),
                    label=f"review {review_id!r} recheck",
                    item_ids=item_ids,
                )
                if recheck_item != item_id:
                    raise ValueError(f"review {review_id!r} recheck item_id is inconsistent")
            for hash_key in ("draft_hash", "before_hash", "after_hash", "unchanged_aggregate_hash"):
                if hash_key in recheck_value:
                    _validate_optional_sha256(recheck_value[hash_key], f"review {review_id!r} {hash_key}")
            normalized_rechecks.append(recheck_value)
        payload["targeted_rechecks"] = normalized_rechecks
        if "unchanged_proofs" in payload and payload["unchanged_proofs"] is None:
            payload["unchanged_proofs"] = []
        payload["finding_count"] = len(normalized_findings)
        payload["repair_count"] = len(normalized_repairs)
        payload["targeted_recheck_count"] = len(normalized_rechecks)
        if review_id in normalized:
            raise ValueError(f"review {review_id!r} appears more than once")
        normalized[review_id] = payload
    return [normalized[key] for key in sorted(normalized)]


def _normalise_transitions(value: Any, *, item_ids: set[str]) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for raw in _records(value, label="implementation_transitions"):
        payload = _jsonable(raw)
        if not isinstance(payload, Mapping):
            raise TypeError("implementation_transitions must be mappings or transition contracts")
        transition = ImplementationTransition.from_dict(dict(payload))
        if item_ids and transition.earliest_affected_item not in item_ids:
            raise ValueError(
                "implementation transition earliest_affected_item is not in the item report set"
            )
        normalized_payload = transition.to_dict()
        key = transition.transition_id or "T-" + hashlib.sha256(_json_bytes(normalized_payload)).hexdigest()[:16]
        if key in normalized:
            raise ValueError(f"implementation transition {key!r} appears more than once")
        normalized[key] = normalized_payload
    return [normalized[key] for key in sorted(normalized)]


def _normalise_implementation_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("implementation_metadata must be a mapping")
    raw = dict(value)
    # A single implementation mapping is treated as the final checkpoint.
    if any(key in raw for key in ("sha", "tree", "version", "implementation_sha", "implementation_tree", "implementation_version")):
        return {"final": _implementation(raw)}
    result: dict[str, Any] = {}
    for stage in ("initial", "intermediate", "final"):
        if stage not in raw:
            continue
        candidate = raw[stage]
        if stage == "intermediate":
            if isinstance(candidate, Mapping):
                candidate = [candidate]
            if isinstance(candidate, (str, bytes)):
                raise TypeError("intermediate implementation metadata must be a sequence")
            result[stage] = [_implementation(dict(entry)) for entry in candidate]
        else:
            result[stage] = _implementation(candidate)
    unknown = set(raw) - {"initial", "intermediate", "final"}
    if unknown:
        raise ValueError(f"unsupported implementation metadata stages: {sorted(unknown)}")
    return result


def _normalize_snapshot(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    raw = dict(value)
    counts = raw.get("counts")
    if counts is None:
        counts = {key: item for key, item in raw.items() if key.endswith("_count") or key in {"count", "total"}}
    if counts is not None:
        if not isinstance(counts, Mapping):
            raise TypeError(f"{label}.counts must be a mapping")
        for key, item in counts.items():
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"{label} count {key!r} is invalid")
        raw["counts"] = dict(sorted((str(k), int(v)) for k, v in counts.items()))
    return _jsonable(raw)


def _validate_projected_report(report: Mapping[str, Any]) -> None:
    """Reject a stale or hand-edited cumulative report before terminalization."""

    outcomes = report.get("item_outcomes")
    if not isinstance(outcomes, Mapping):
        raise ValueError("report item_outcomes is invalid")
    expected_outcomes = dict(sorted(Counter(str(value) for value in outcomes.values()).items()))
    if report.get("outcome_counts") != expected_outcomes:
        raise ValueError("report outcome_counts are stale")
    items = report.get("items", ())
    if not isinstance(items, list):
        raise ValueError("report items are invalid")
    item_ids = [str(item.get("item_id", "")) for item in items if isinstance(item, Mapping)]
    if len(item_ids) != len(items) or len(item_ids) != len(set(item_ids)) or set(item_ids) != set(outcomes):
        raise ValueError("report item states are stale or incomplete")
    implementation_map = report.get("implementation")
    if not isinstance(implementation_map, Mapping) or set(implementation_map) != set(outcomes):
        raise ValueError("report implementation metadata is stale or incomplete")
    recomputed_kinds: Counter[str] = Counter()
    for item in items:
        if not isinstance(item, Mapping) or outcomes.get(item.get("item_id")) != item.get("outcome"):
            raise ValueError("report item outcome is stale")
        item_implementation = _implementation({"implementation": item.get("implementation")})
        if implementation_map.get(item.get("item_id")) != item_implementation:
            raise ValueError("report item implementation metadata is stale")
        kinds = item.get("record_kind_totals", {})
        if not isinstance(kinds, Mapping):
            raise ValueError("report item record_kind_totals are invalid")
        for kind, count in kinds.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("report item record-kind count is invalid")
            recomputed_kinds[str(kind)] += count
    if report.get("record_kind_totals") != dict(sorted(recomputed_kinds.items())):
        raise ValueError("report record_kind_totals are stale")
    incidents = report.get("incidents")
    if not isinstance(incidents, list) or len(incidents) != len({str(item.get("incident_id")) for item in incidents if isinstance(item, Mapping)}):
        raise ValueError("report incidents are duplicated or invalid")
    if report.get("incident_count") != len(incidents):
        raise ValueError("report incident count is stale")
    for field, array_name in (
        ("receipt_count", "receipts"),
        ("timing_count", "timings"),
        ("incident_count", "incidents"),
        ("business_review_count", "business_reviews"),
        ("fidelity_review_count", "fidelity_reviews"),
        ("implementation_transition_count", "implementation_transitions"),
    ):
        value = report.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"report {field} is invalid")
        records = report.get(array_name)
        if not isinstance(records, list) or value != len(records):
            raise ValueError(f"report {array_name} or {field} is stale")

    item_set = set(outcomes)
    for incident in incidents:
        if not isinstance(incident, Mapping) or not str(incident.get("incident_id", "")).strip():
            raise ValueError("report incident records are invalid")
        if incident.get("item_id") is not None:
            _require_item_id(incident.get("item_id"), label="report incident", item_ids=item_set)
    receipts = report.get("receipts")
    receipt_ids: set[str] = set()
    if not isinstance(receipts, list):
        raise ValueError("report receipts are invalid")
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise ValueError("report receipt records are invalid")
        receipt_id = str(receipt.get("invocation_id", receipt.get("receipt_id", ""))).strip()
        if not receipt_id or receipt_id in receipt_ids:
            raise ValueError("report receipt identities are duplicated or invalid")
        receipt_ids.add(receipt_id)
        _require_item_id(receipt.get("item_id"), label="report receipt", item_ids=item_set)
    timings = report.get("timings")
    if not isinstance(timings, list):
        raise ValueError("report timings are invalid")
    timing_ids: set[str] = set()
    for timing in timings:
        if not isinstance(timing, Mapping) or not str(timing.get("phase", "")).strip():
            raise ValueError("report timing records are invalid")
        if timing.get("item_id") is not None:
            _require_item_id(timing.get("item_id"), label="report timing", item_ids=item_set)
        receipt_ref = timing.get("receipt_ref")
        if receipt_ref is not None:
            ref_id = str(receipt_ref).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            if receipt_ref not in receipt_ids and ref_id not in receipt_ids:
                raise ValueError("report timing receipt linkage is stale")
        stable_id = str(timing.get("timing_id", timing.get("phase_id", ""))).strip()
        stable_id = stable_id or "timing-" + hashlib.sha256(_json_bytes(timing)).hexdigest()
        if stable_id in timing_ids:
            raise ValueError("report timing identities are duplicated")
        timing_ids.add(stable_id)
    all_review_ids: set[str] = set()
    for array_name, kind in (("business_reviews", "business"), ("fidelity_reviews", "fidelity")):
        reviews = report.get(array_name)
        if not isinstance(reviews, list):
            raise ValueError(f"report {array_name} are invalid")
        review_ids: set[str] = set()
        for review in reviews:
            if not isinstance(review, Mapping):
                raise ValueError(f"report {array_name} records are invalid")
            review_id = str(review.get("review_id", "")).strip()
            if not review_id or review_id in review_ids or review_id in all_review_ids or review.get("review_kind") != kind:
                raise ValueError(f"report {array_name} identities are invalid")
            review_ids.add(review_id)
            all_review_ids.add(review_id)
            _require_item_id(review.get("item_id"), label=f"report {kind} review", item_ids=item_set)
            findings = review.get("findings")
            repairs = review.get("repairs")
            rechecks = review.get("targeted_rechecks")
            if not isinstance(findings, list) or review.get("finding_count") != len(findings):
                raise ValueError(f"report {kind} review findings are stale")
            if not isinstance(repairs, list) or review.get("repair_count") != len(repairs):
                raise ValueError(f"report {kind} review repairs are stale")
            if not isinstance(rechecks, list) or review.get("targeted_recheck_count") != len(rechecks):
                raise ValueError(f"report {kind} review rechecks are stale")
            finding_ids = [str(finding.get("finding_id", "")) for finding in findings if isinstance(finding, Mapping)]
            if len(finding_ids) != len(findings) or len(finding_ids) != len(set(finding_ids)) or not all(finding_ids):
                raise ValueError(f"report {kind} review finding identities are invalid")
    transitions = report.get("implementation_transitions")
    if not isinstance(transitions, list):
        raise ValueError("report implementation transitions are invalid")
    transition_ids: set[str] = set()
    for payload in transitions:
        if not isinstance(payload, Mapping):
            raise ValueError("report implementation transition is invalid")
        transition = ImplementationTransition.from_dict(dict(payload))
        transition_key = transition.transition_id or "T-" + hashlib.sha256(_json_bytes(payload)).hexdigest()[:16]
        if transition_key in transition_ids:
            raise ValueError("report implementation transitions are duplicated")
        transition_ids.add(transition_key)
        if item_set and transition.earliest_affected_item not in item_set:
            raise ValueError("report implementation transition item is stale")
    _normalise_implementation_metadata(report.get("implementation_metadata"))


class RunReportProjector:
    """Recompute a cumulative report from item-local authoritative inputs."""

    def __init__(self, *, run_id: str | None = None) -> None:
        self.run_id = run_id

    def project(
        self,
        item_reports: Any,
        *,
        item_manifests: Any = None,
        registry_snapshot: Mapping[str, Any] | None = None,
        lem_snapshot: Mapping[str, Any] | None = None,
        receipts: Iterable[Any] = (),
        timings: Iterable[Any] = (),
        incidents: Iterable[Any] = (),
        business_reviews: Iterable[Any] = (),
        fidelity_reviews: Iterable[Any] = (),
        implementation_records: Any = None,
        implementation_transitions: Iterable[Any] = (),
        implementation_metadata: Mapping[str, Any] | None = None,
        lifecycle_status: Any = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        reports = _as_records(item_reports, label="item_reports")
        manifests = _as_records(item_manifests, label="item_manifests")
        by_manifest: dict[str, Mapping[str, Any]] = {}
        for manifest in manifests:
            item_id = str(manifest.get("item_id", "")).strip()
            if not item_id or item_id in by_manifest:
                raise ValueError("item manifests must have unique item IDs")
            by_manifest[item_id] = manifest
        seen_items: set[str] = set()
        normalized_items: list[dict[str, Any]] = []
        record_kind_totals: Counter[str] = Counter()
        implementation_map = dict(implementation_records or {}) if isinstance(implementation_records, Mapping) else {}
        for report in reports:
            item_id = str(report.get("item_id", "")).strip()
            if not item_id or item_id in seen_items:
                raise ValueError("item reports must have unique item IDs")
            seen_items.add(item_id)
            manifest = by_manifest.get(item_id)
            if manifest is not None and manifest.get("item_id") != item_id:
                raise ValueError("item manifest identity is stale")
            outcome = _item_outcome(report)
            if outcome is None:
                raise ValueError(f"item {item_id} has no terminal outcome")
            lifecycle = _item_lifecycle(report)
            if lifecycle in {"accepted", "technical_failure"} and outcome not in {
                "accepted",
                "accepted_with_limits",
                "technical_failure",
            }:
                raise ValueError(f"item {item_id} lifecycle and outcome are stale")
            if manifest is not None:
                manifest_outcome = _item_outcome(manifest)
                if manifest_outcome != outcome:
                    raise ValueError(f"item {item_id} manifest outcome is stale")
            report_kinds = report.get("record_kind_totals", report.get("record_kinds", {}))
            manifest_kinds = manifest.get("record_kind_totals", {}) if manifest is not None else {}
            if report_kinds is None:
                report_kinds = {}
            if not isinstance(report_kinds, Mapping) or not isinstance(manifest_kinds, Mapping):
                raise TypeError("record_kind_totals must be mappings")
            if manifest is not None and dict(report_kinds) != dict(manifest_kinds):
                raise ValueError(f"item {item_id} record-kind totals are stale")
            for kind, count in report_kinds.items():
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError(f"item {item_id} record-kind count is invalid")
                record_kind_totals[str(kind)] += count
            implementation = _implementation({**(implementation_map.get(item_id, {}) if isinstance(implementation_map.get(item_id), Mapping) else {}), **dict(report)})
            normalized_items.append(
                {
                    "item_id": item_id,
                    "outcome": outcome,
                    "lifecycle_state": lifecycle,
                    "record_kind_totals": dict(sorted((str(k), int(v)) for k, v in report_kinds.items())),
                    "implementation": implementation,
                }
            )
        if by_manifest and set(by_manifest) != seen_items:
            raise ValueError("item manifests and reports are stale or incomplete")

        outcomes = {item["item_id"]: item["outcome"] for item in normalized_items}
        item_ids = set(outcomes)
        normalized_receipts = _normalise_receipts(receipts, item_ids=item_ids)
        receipt_ids = {str(payload["invocation_id"]) for payload in normalized_receipts}
        normalized_timings = _normalise_timings(
            timings,
            item_ids=item_ids,
            receipt_ids=receipt_ids,
        )
        incident_payload = _normalise_incidents(incidents, item_ids=item_ids)
        normalized_business_reviews = _normalise_review_records(
            business_reviews,
            kind="business",
            item_ids=item_ids,
        )
        normalized_fidelity_reviews = _normalise_review_records(
            fidelity_reviews,
            kind="fidelity",
            item_ids=item_ids,
        )
        review_ids = {review["review_id"] for review in normalized_business_reviews}
        duplicate_review_ids = review_ids.intersection(review["review_id"] for review in normalized_fidelity_reviews)
        if duplicate_review_ids:
            raise ValueError(f"review IDs are duplicated across kinds: {sorted(duplicate_review_ids)}")
        normalized_transitions = _normalise_transitions(
            implementation_transitions,
            item_ids=item_ids,
        )
        normalized_metadata = _normalise_implementation_metadata(implementation_metadata)
        outcome_counts = dict(sorted(Counter(outcomes.values()).items()))
        lifecycle = lifecycle_status.get("status") if isinstance(lifecycle_status, Mapping) else lifecycle_status
        lifecycle = str(lifecycle).strip() if lifecycle is not None else ("analytical_complete" if normalized_items else "running")
        if lifecycle in {"complete", "complete_with_limits", "analytical_complete"} and any(
            outcome not in {"accepted", "accepted_with_limits", "technical_failure"} for outcome in outcomes.values()
        ):
            raise ValueError("lifecycle status is stale while item outcomes are non-terminal")
        implementation = {item["item_id"]: item["implementation"] for item in normalized_items}
        report = {
            "report_version": "1",
            "run_id": self.run_id,
            "item_outcomes": outcomes,
            "outcome_counts": outcome_counts,
            "record_kind_totals": dict(sorted(record_kind_totals.items())),
            "registry_counts": _normalize_snapshot(registry_snapshot, "registry_snapshot"),
            "lem_counts": _normalize_snapshot(lem_snapshot, "lem_snapshot"),
            "receipt_count": len(normalized_receipts),
            "receipts": normalized_receipts,
            "timing_count": len(normalized_timings),
            "timings": normalized_timings,
            "incident_count": len(incident_payload),
            "incidents": incident_payload,
            "business_review_count": len(normalized_business_reviews),
            "business_reviews": normalized_business_reviews,
            "fidelity_review_count": len(normalized_fidelity_reviews),
            "fidelity_reviews": normalized_fidelity_reviews,
            "implementation_transition_count": len(normalized_transitions),
            "implementation_transitions": normalized_transitions,
            "implementation_metadata": normalized_metadata,
            "implementation": implementation,
            "lifecycle_status": lifecycle,
            "items": normalized_items,
        }
        if generated_at is not None:
            report["generated_at"] = str(generated_at)
        return report


def project_run_report(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return RunReportProjector(run_id=kwargs.pop("run_id", None)).project(*args, **kwargs)


def _safe_root(root: Any) -> Path:
    if hasattr(root, "run_root"):
        root = root.run_root
    path = Path(root).expanduser().resolve(strict=False)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class RunReportFinalizer:
    """Atomically publish and verify the report/manifest/receipt triplet."""

    def __init__(self, root: Any = None) -> None:
        self.root = _safe_root(root) if root is not None else None

    def finalize(
        self,
        report: Mapping[str, Any],
        *,
        run_manifest: Mapping[str, Any] | None = None,
        authoritative_receipts: Iterable[Any] | None = None,
        authoritative_timings: Iterable[Any] | None = None,
        authoritative_incidents: Iterable[Any] | None = None,
        authoritative_business_reviews: Iterable[Any] | None = None,
        authoritative_fidelity_reviews: Iterable[Any] | None = None,
        authoritative_implementation_transitions: Iterable[Any] | None = None,
        authoritative_implementation_metadata: Mapping[str, Any] | None = None,
        lifecycle_status: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(report, Mapping):
            raise TypeError("report must be a mapping")
        report_value = copy.deepcopy(dict(report))
        supplied_report_hash = report_value.pop("report_hash", None)
        _validate_projected_report(report_value)
        report_hash = hashlib.sha256(_json_bytes(report_value)).hexdigest()
        if supplied_report_hash is not None and supplied_report_hash != report_hash:
            raise ValueError("report hash does not match content")
        item_ids = set(report_value.get("item_outcomes", {}))
        if authoritative_receipts is not None:
            expected = _normalise_receipts(authoritative_receipts, item_ids=item_ids)
            if expected != report_value.get("receipts"):
                raise ValueError("report receipts are stale or incomplete")
        receipt_ids = {str(payload["invocation_id"]) for payload in report_value.get("receipts", ())}
        if authoritative_timings is not None:
            expected = _normalise_timings(
                authoritative_timings,
                item_ids=item_ids,
                receipt_ids=receipt_ids,
            )
            if expected != report_value.get("timings"):
                raise ValueError("report timings are stale or incomplete")
        if authoritative_incidents is not None:
            expected = _normalise_incidents(authoritative_incidents, item_ids=item_ids)
            if expected != report_value.get("incidents"):
                raise ValueError("report incident facts are stale or incomplete")
        if authoritative_business_reviews is not None:
            expected = _normalise_review_records(
                authoritative_business_reviews,
                kind="business",
                item_ids=item_ids,
            )
            if expected != report_value.get("business_reviews"):
                raise ValueError("report business review records are stale or incomplete")
        if authoritative_fidelity_reviews is not None:
            expected = _normalise_review_records(
                authoritative_fidelity_reviews,
                kind="fidelity",
                item_ids=item_ids,
            )
            if expected != report_value.get("fidelity_reviews"):
                raise ValueError("report fidelity review records are stale or incomplete")
        if authoritative_implementation_transitions is not None:
            expected = _normalise_transitions(
                authoritative_implementation_transitions,
                item_ids=item_ids,
            )
            if expected != report_value.get("implementation_transitions"):
                raise ValueError("report implementation transitions are stale or incomplete")
        if authoritative_implementation_metadata is not None:
            expected = _normalise_implementation_metadata(authoritative_implementation_metadata)
            if expected != report_value.get("implementation_metadata"):
                raise ValueError("report implementation metadata are stale or incomplete")
        if report_value.get("incident_count") != len(report_value.get("incidents", ())):
            raise ValueError("report incident count is stale")
        status = lifecycle_status or report_value.get("lifecycle_status")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("finalization lifecycle status is unavailable")
        report_value["report_hash"] = report_hash
        manifest_unsigned = dict(run_manifest or {})
        manifest_unsigned.pop("manifest_hash", None)
        manifest_unsigned.pop("terminalization_receipt_hash", None)
        manifest_unsigned.update(
            {
                "report_hash": report_hash,
                "item_ids": sorted(report_value.get("item_outcomes", {})),
                "item_outcomes": report_value.get("item_outcomes", {}),
                "outcome_counts": report_value.get("outcome_counts", {}),
                "record_kind_totals": report_value.get("record_kind_totals", {}),
                "registry_counts": report_value.get("registry_counts", {}),
                "lem_counts": report_value.get("lem_counts", {}),
                "receipt_count": report_value.get("receipt_count"),
                "timing_count": report_value.get("timing_count"),
                "incident_count": report_value.get("incident_count"),
                "business_review_count": report_value.get("business_review_count"),
                "fidelity_review_count": report_value.get("fidelity_review_count"),
                "implementation_transition_count": report_value.get("implementation_transition_count"),
                "lifecycle_status": status,
            }
        )
        if any(
            manifest_unsigned.get(key) is None
            for key in (
                "receipt_count",
                "timing_count",
                "incident_count",
                "business_review_count",
                "fidelity_review_count",
                "implementation_transition_count",
            )
        ):
            raise ValueError("finalization counts cannot be null")
        manifest = {**manifest_unsigned, "manifest_hash": _hash_mapping(manifest_unsigned, "manifest_hash")}
        receipt_unsigned = {
            "report_hash": report_hash,
            "manifest_hash": manifest["manifest_hash"],
            "item_count": len(report_value.get("item_outcomes", {})),
            "outcome_count": len(report_value.get("outcome_counts", {})),
            "receipt_count": report_value["receipt_count"],
            "timing_count": report_value["timing_count"],
            "incident_count": report_value["incident_count"],
            "business_review_count": report_value["business_review_count"],
            "fidelity_review_count": report_value["fidelity_review_count"],
            "implementation_transition_count": report_value["implementation_transition_count"],
            "lifecycle_status": status,
            "terminal_status": "terminalized",
        }
        if any(value is None for value in receipt_unsigned.values()):
            raise ValueError("terminalization receipt cannot contain null hashes or counts")
        receipt = {**receipt_unsigned, "receipt_hash": _hash_mapping(receipt_unsigned, "receipt_hash")}
        if self.root is None:
            return {"report": report_value, "manifest": manifest, "receipt": receipt}

        final_dir = self.root / _REPORT_DIRNAME
        report_path = final_dir / _REPORT_FILENAME
        manifest_path = final_dir / _MANIFEST_FILENAME
        receipt_path = final_dir / _RECEIPT_FILENAME
        if final_dir.exists():
            if not final_dir.is_dir():
                raise ValueError("reporting path is not a directory")
            try:
                disk_report = json.loads(report_path.read_text(encoding="utf-8"))
                disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                disk_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("existing finalization artifacts are invalid") from exc
            disk_report_hash = _hash_mapping(disk_report, "report_hash") if isinstance(disk_report, Mapping) else None
            disk_manifest_hash = _hash_mapping(disk_manifest, "manifest_hash") if isinstance(disk_manifest, Mapping) else None
            disk_receipt_hash = _hash_mapping(disk_receipt, "receipt_hash") if isinstance(disk_receipt, Mapping) else None
            if not isinstance(disk_report, Mapping) or not isinstance(disk_manifest, Mapping) or not isinstance(disk_receipt, Mapping):
                raise ValueError("existing finalization artifacts must be objects")
            if (
                disk_report_hash != disk_report.get("report_hash")
                or disk_manifest_hash != disk_manifest.get("manifest_hash")
                or disk_receipt_hash != disk_receipt.get("receipt_hash")
            ):
                raise ValueError("existing finalization artifact hash is invalid")
            if disk_report != report_value or disk_manifest != manifest or disk_receipt != receipt:
                raise ValueError("existing finalization artifacts are stale or tampered")
            return copy.deepcopy(disk_receipt)

        temporary = Path(tempfile.mkdtemp(prefix=f".{_REPORT_DIRNAME}.tmp-", dir=self.root))
        try:
            _atomic_write(temporary / _REPORT_FILENAME, _json_bytes(report_value))
            _atomic_write(temporary / _MANIFEST_FILENAME, _json_bytes(manifest))
            _atomic_write(temporary / _RECEIPT_FILENAME, _json_bytes(receipt))
            os.replace(temporary, final_dir)
        except Exception:
            import shutil

            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return copy.deepcopy(receipt)


def finalize_run_report(root: Any, report: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return RunReportFinalizer(root).finalize(report, **kwargs)


__all__ = [
    "RunReportFinalizer",
    "RunReportProjector",
    "finalize_run_report",
    "project_run_report",
]
