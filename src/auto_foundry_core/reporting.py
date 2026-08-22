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
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .contracts import IncidentRecord, ImplementationTransition, PhaseTimingRecord
from .telemetry import _normalize_incident


_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_DIRNAME = "reporting"
_REPORT_FILENAME = "final_report.json"
_MANIFEST_FILENAME = "run_manifest.json"
_RECEIPT_FILENAME = "terminalization_receipt.json"
_FINALIZE_INTENT_FILENAME = ".reporting.finalize.intent.json"
_FINALIZE_STAGING_PREFIX = ".reporting.finalize-staging-"
_FINALIZE_BACKUP_PREFIX = ".reporting.finalize-backup-"

# ``None`` remains the public "not supplied" value on the typed envelope for
# backwards compatibility.  Gather's keyword surface uses this private
# sentinel so an explicitly supplied empty list/mapping is not confused with
# an omitted authoritative binding.
_UNSUPPLIED = object()


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
        if lifecycle in {"accepted", "accepted_with_limits", "technical_failure", "blocked_by_evidence"}:
            return lifecycle
    terminal = value.get("terminal_outcome")
    if isinstance(terminal, Mapping) and isinstance(terminal.get("outcome"), str):
        return terminal["outcome"]
    outcome = value.get("outcome", value.get("status"))
    return str(outcome) if outcome in {"accepted", "accepted_with_limits", "technical_failure", "blocked_by_evidence"} else None


def _item_lifecycle(value: Mapping[str, Any]) -> str | None:
    state = value.get("state", value.get("item_state"))
    if isinstance(state, Mapping):
        lifecycle = state.get("lifecycle_state")
        if lifecycle is not None:
            return str(lifecycle)
    lifecycle = value.get("lifecycle_state")
    return str(lifecycle) if lifecycle is not None else None


def _validate_item_lifecycle_outcome_pair(
    lifecycle: str | None,
    outcome: str,
    *,
    item_id: str,
    allow_missing_lifecycle: bool = False,
) -> None:
    """Reject crossed terminal lifecycle/outcome identities."""

    allowed: Mapping[str, frozenset[str]] = {
        "accepted": frozenset({"accepted", "accepted_with_limits"}),
        "accepted_with_limits": frozenset({"accepted_with_limits"}),
        "technical_failure": frozenset({"technical_failure"}),
        "blocked_by_evidence": frozenset({"blocked_by_evidence"}),
    }
    # Only an item manifest is allowed to omit lifecycle metadata. Authoritative
    # item reports and projected report items must carry an exact terminal
    # lifecycle, while a present manifest lifecycle is still checked strictly.
    if lifecycle is None:
        if allow_missing_lifecycle:
            return
        raise ValueError(f"item {item_id} lifecycle and outcome are stale")
    expected = allowed.get(lifecycle)
    if expected is None or outcome not in expected:
        raise ValueError(f"item {item_id} lifecycle and outcome are stale")


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


class ReportPreflightError(ValueError):
    """Raised when a terminal report lacks an authoritative input binding."""


@dataclass(frozen=True)
class RunReportEventBindings:
    """Coordinator-independent event bindings supplied to report gathering.

    The coordinator (or another host) owns collecting canonical invocation,
    timing, and implementation events.  It passes those records through this
    typed value; reporting never imports or introspects the coordinator.
    ``None`` means the binding was not supplied, whereas an empty tuple is an
    explicit authoritative assertion that no records exist.
    """

    invocation_receipts: Sequence[Any] | None = None
    item_manifests: Sequence[Any] | None = None
    registry_snapshot: Mapping[str, Any] | None = None
    lem_snapshot: Mapping[str, Any] | None = None
    timings: Sequence[Any] | None = None
    incidents: Sequence[Any] | None = None
    business_reviews: Sequence[Any] | None = None
    fidelity_reviews: Sequence[Any] | None = None
    implementation_transitions: Sequence[Any] | None = None
    implementation_metadata: Mapping[str, Any] | None = None
    implementation_identity: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_receipts": _jsonable(self.invocation_receipts),
            "item_manifests": _jsonable(self.item_manifests),
            "registry_snapshot": _jsonable(self.registry_snapshot),
            "lem_snapshot": _jsonable(self.lem_snapshot),
            "timings": _jsonable(self.timings),
            "incidents": _jsonable(self.incidents),
            "business_reviews": _jsonable(self.business_reviews),
            "fidelity_reviews": _jsonable(self.fidelity_reviews),
            "implementation_transitions": _jsonable(self.implementation_transitions),
            "implementation_metadata": _jsonable(self.implementation_metadata),
            "implementation_identity": _jsonable(self.implementation_identity),
        }

    @classmethod
    def from_value(cls, value: "RunReportEventBindings | Mapping[str, Any]") -> "RunReportEventBindings":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("event_bindings must be RunReportEventBindings or a mapping")
        return cls(
            invocation_receipts=value.get("invocation_receipts", value.get("receipts")),
            item_manifests=value.get("item_manifests"),
            registry_snapshot=value.get("registry_snapshot"),
            lem_snapshot=value.get("lem_snapshot"),
            timings=value.get("timings"),
            incidents=value.get("incidents"),
            business_reviews=value.get("business_reviews"),
            fidelity_reviews=value.get("fidelity_reviews"),
            implementation_transitions=value.get("implementation_transitions"),
            implementation_metadata=value.get("implementation_metadata"),
            implementation_identity=value.get("implementation_identity"),
        )


@dataclass(frozen=True)
class RunReportPreflight:
    """Hash-bound report inputs persisted before terminal projection."""

    run_id: str | None
    item_ids: tuple[str, ...]
    projected_report: Mapping[str, Any]
    input_counts: Mapping[str, int]
    input_hashes: Mapping[str, str]
    artifact_bindings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    schema_version: int = 1
    kind: str = "run_report_preflight"
    preflight_hash: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.kind != "run_report_preflight":
            raise ValueError("report preflight schema or kind is invalid")
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id.strip()):
            raise ValueError("report preflight run_id is invalid")
        ids = tuple(str(item).strip() for item in self.item_ids)
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            raise ValueError("report preflight item_ids are invalid")
        object.__setattr__(self, "item_ids", tuple(sorted(ids)))
        if not isinstance(self.projected_report, Mapping):
            raise TypeError("report preflight projected_report must be a mapping")
        counts = dict(self.input_counts)
        for key, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"report preflight count {key!r} is invalid")
        object.__setattr__(self, "input_counts", dict(sorted((str(k), int(v)) for k, v in counts.items())))
        hashes = dict(self.input_hashes)
        for key, value in hashes.items():
            _validate_optional_sha256(value, f"report preflight input hash {key}")
        object.__setattr__(self, "input_hashes", dict(sorted((str(k), str(v)) for k, v in hashes.items())))
        if not isinstance(self.artifact_bindings, Mapping):
            raise TypeError("report preflight artifact_bindings must be a mapping")
        object.__setattr__(self, "artifact_bindings", _jsonable(self.artifact_bindings))
        if self.preflight_hash is not None:
            _validate_optional_sha256(self.preflight_hash, "preflight_hash")

    def unsigned(self) -> dict[str, Any]:
        report_hash = hashlib.sha256(_json_bytes(self.projected_report)).hexdigest()
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "item_ids": list(self.item_ids),
            "projected_report": _jsonable(self.projected_report),
            "report_hash": report_hash,
            "input_counts": dict(self.input_counts),
            "input_hashes": dict(self.input_hashes),
            "artifact_bindings": _jsonable(self.artifact_bindings),
        }

    @property
    def computed_hash(self) -> str:
        return hashlib.sha256(_json_bytes(self.unsigned())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned()
        value["preflight_hash"] = self.preflight_hash or self.computed_hash
        return value

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunReportPreflight":
        required = {
            "schema_version",
            "kind",
            "run_id",
            "item_ids",
            "projected_report",
            "input_counts",
            "input_hashes",
            "preflight_hash",
        }
        if not isinstance(value, Mapping) or not required.issubset(value):
            raise ValueError("report preflight is missing required fields")
        preflight = cls(
            run_id=value.get("run_id"), item_ids=tuple(value.get("item_ids", ())),
            projected_report=value.get("projected_report", value.get("report", {})),
            input_counts=value.get("input_counts", {}), input_hashes=value.get("input_hashes", {}),
            artifact_bindings=value.get("artifact_bindings", {}),
            schema_version=value.get("schema_version", 1), kind=value.get("kind", "run_report_preflight"),
            preflight_hash=value.get("preflight_hash"),
        )
        supplied_report_hash = value.get("report_hash")
        expected_report_hash = hashlib.sha256(_json_bytes(preflight.projected_report)).hexdigest()
        if supplied_report_hash is not None and supplied_report_hash != expected_report_hash:
            raise ValueError("report preflight report_hash does not match projected report")
        if preflight.preflight_hash is not None and preflight.preflight_hash != preflight.computed_hash:
            raise ValueError("report preflight hash does not match content")
        return preflight


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
        _validate_item_lifecycle_outcome_pair(
            str(item.get("lifecycle_state")) if item.get("lifecycle_state") is not None else None,
            str(item.get("outcome")),
            item_id=str(item.get("item_id")),
        )
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

    @staticmethod
    def project_from_preflight(preflight: RunReportPreflight | Mapping[str, Any]) -> dict[str, Any]:
        """Return the exact projected report bound by a persisted preflight."""

        value = preflight if isinstance(preflight, RunReportPreflight) else RunReportPreflight.from_dict(preflight)
        return copy.deepcopy(dict(value.projected_report))

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
            _validate_item_lifecycle_outcome_pair(lifecycle, outcome, item_id=item_id)
            if manifest is not None:
                manifest_outcome = _item_outcome(manifest)
                if manifest_outcome != outcome:
                    raise ValueError(f"item {item_id} manifest outcome is stale")
                _validate_item_lifecycle_outcome_pair(
                    _item_lifecycle(manifest),
                    manifest_outcome,
                    item_id=item_id,
                    allow_missing_lifecycle=True,
                )
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
            outcome not in {"accepted", "accepted_with_limits", "technical_failure", "blocked_by_evidence"}
            for outcome in outcomes.values()
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


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must contain an object")
    return dict(value)


def inspect_report_artifacts(root: Any, *, run_id: str | None = None) -> dict[str, Any]:
    """Purely validate persisted preflight and final report triplet."""

    base = Path(root.run_root if hasattr(root, "run_root") else root).expanduser().resolve(strict=False)
    reporting = base / _REPORT_DIRNAME
    preflight_path = reporting / "report_preflight.json"
    report_path = reporting / _REPORT_FILENAME
    manifest_path = reporting / _MANIFEST_FILENAME
    receipt_path = reporting / _RECEIPT_FILENAME
    present = {"preflight": preflight_path.exists(), "report": report_path.exists(), "manifest": manifest_path.exists(), "receipt": receipt_path.exists()}
    intent_path = base / _FINALIZE_INTENT_FILENAME
    transaction_residue: list[Path] = []
    if intent_path.exists() or intent_path.is_symlink():
        transaction_residue.append(intent_path)
    try:
        transaction_residue.extend(
            path
            for path in base.iterdir()
            if path.name.startswith(_FINALIZE_STAGING_PREFIX) or path.name.startswith(_FINALIZE_BACKUP_PREFIX)
        )
    except OSError:
        transaction_residue.append(base / "<unreadable-finalization-namespace>")
    transaction_stage = None
    if transaction_residue:
        transaction_stage = "recovery_required" if intent_path.exists() or intent_path.is_symlink() else "transaction_pending"
    diagnostics: list[str] = []
    preflight: RunReportPreflight | None = None
    report: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    try:
        if present["preflight"]:
            preflight = RunReportPreflight.from_dict(_load_json(preflight_path, "report preflight"))
            if run_id is not None and preflight.run_id not in {None, run_id}:
                raise ValueError("report preflight run_id is stale")
        final_present = [present[name] for name in ("report", "manifest", "receipt")]
        if any(final_present) and not all(final_present):
            raise ValueError("final report triplet is partial")
        if all(final_present):
            report = _load_json(report_path, "final report")
            manifest = _load_json(manifest_path, "run manifest")
            receipt = _load_json(receipt_path, "terminalization receipt")
            supplied_report_hash = report.get("report_hash")
            report_unsigned = {key: value for key, value in report.items() if key != "report_hash"}
            expected_report_hash = hashlib.sha256(_json_bytes(report_unsigned)).hexdigest()
            if supplied_report_hash != expected_report_hash:
                raise ValueError("final report hash does not match content")
            _validate_projected_report(report_unsigned)
            if manifest.get("manifest_hash") != _hash_mapping(manifest, "manifest_hash"):
                raise ValueError("run manifest hash does not match content")
            if receipt.get("receipt_hash") != _hash_mapping(receipt, "receipt_hash"):
                raise ValueError("terminalization receipt hash does not match content")
            if manifest.get("report_hash") != supplied_report_hash or receipt.get("report_hash") != supplied_report_hash:
                raise ValueError("final report triplet report binding is stale")
            if receipt.get("manifest_hash") != manifest.get("manifest_hash"):
                raise ValueError("terminalization receipt manifest binding is stale")
            if preflight is not None and preflight.projected_report != report_unsigned:
                raise ValueError("final report does not match persisted report preflight")
        elif preflight is None and transaction_stage is None:
            return {"valid": True, "stage": "not_started", "diagnostics": [], "preflight": None, "report": None}
        if transaction_stage is not None:
            diagnostics.append(
                "report finalization transaction requires recovery: "
                + ", ".join(path.name for path in transaction_residue)
            )
            return {
                "valid": False,
                "stage": transaction_stage,
                "diagnostics": diagnostics,
                "preflight": preflight.to_dict() if preflight is not None else None,
                "report": report,
                "manifest": manifest,
                "receipt": receipt,
            }
        return {
            "valid": True,
            "stage": "finalized" if report is not None else "preflighted",
            "diagnostics": diagnostics,
            "preflight": preflight.to_dict() if preflight is not None else None,
            "report": report,
            "manifest": manifest,
            "receipt": receipt,
        }
    except Exception as exc:
        diagnostics.append(str(exc))
        return {
            "valid": False,
            "stage": transaction_stage or "invalid",
            "diagnostics": diagnostics,
            "preflight": preflight.to_dict() if preflight is not None else None,
            "report": report,
            "manifest": manifest,
            "receipt": receipt,
        }


class RunReportInputGatherer:
    """Gather authoritative report inputs and persist a hash-bound preflight.

    ``event_bindings`` is intentionally a small typed boundary rather than a
    coordinator import.  The caller supplies the canonical invocation,
    timing, review, and implementation events; this class validates and
    projects them, then records the exact input counts/hashes used for report
    finalization.
    """

    def __init__(self, root: Any = None, *, run_id: str | None = None, require_complete: bool = True) -> None:
        self.root = _safe_root(root) if root is not None else None
        self.run_id = run_id
        self.require_complete = bool(require_complete)

    @property
    def preflight_path(self) -> Path | None:
        return None if self.root is None else self.root / _REPORT_DIRNAME / "report_preflight.json"

    @staticmethod
    def _records(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (str, bytes, Mapping)) or (hasattr(value, "to_dict") and callable(value.to_dict)):
            return [value]
        return list(value)

    def _require_inputs(
        self,
        reports: list[Mapping[str, Any]],
        bindings: RunReportEventBindings,
        projected: Mapping[str, Any],
        *,
        supplied: Mapping[str, bool],
        item_manifests: Sequence[Any],
        implementation_identity: Mapping[str, Any] | None,
    ) -> None:
        if not self.require_complete:
            return
        if not reports:
            raise ReportPreflightError("report preflight is missing terminal item IDs/reports")
        # Presence is intentionally checked separately from truthiness: an
        # empty incidents/transitions/snapshot binding is authoritative, while
        # an omitted binding is not.  The mandatory identity remains a
        # non-empty canonical mapping because it anchors the report to the
        # implementation that produced the supplied events.
        missing_fields = [name for name in (
            "item_manifests",
            "registry_snapshot",
            "lem_snapshot",
            "invocation_receipts",
            "timings",
            "incidents",
            "business_reviews",
            "fidelity_reviews",
            "implementation_transitions",
            "implementation_metadata",
            "implementation_identity",
        ) if not supplied.get(name, False)]
        if missing_fields:
            raise ReportPreflightError("report preflight is missing authoritative inputs: " + ", ".join(missing_fields))
        if not item_manifests:
            raise ReportPreflightError("report preflight item_manifests are explicitly empty for terminal items")
        item_ids = set(str(item.get("item_id", "")).strip() for item in reports)
        item_ids.discard("")
        receipts = projected.get("receipts", ())
        receipt_items = {str(item.get("item_id", "")) for item in receipts if isinstance(item, Mapping)}
        if not receipt_items.issuperset(item_ids):
            raise ReportPreflightError("report preflight is missing invocation receipts for terminal items")
        if not projected.get("timings"):
            raise ReportPreflightError("report preflight has no authoritative phase timings")
        reviews = [*projected.get("business_reviews", ()), *projected.get("fidelity_reviews", ())]
        review_items = {str(item.get("item_id", "")) for item in reviews if isinstance(item, Mapping)}
        if not review_items.issuperset(item_ids):
            raise ReportPreflightError("report preflight is missing independent review records")
        metadata = projected.get("implementation_metadata")
        if not isinstance(metadata, Mapping) or not metadata:
            raise ReportPreflightError("report preflight is missing implementation metadata")
        if not isinstance(implementation_identity, Mapping) or not implementation_identity:
            raise ReportPreflightError("report preflight is missing implementation identity")

    def _artifact_bindings(self, values: Mapping[str, Any] | None) -> dict[str, Any]:
        if values is None:
            return {}
        if not isinstance(values, Mapping):
            raise TypeError("artifact_bindings must be a mapping")
        result: dict[str, Any] = {}
        for name, raw in values.items():
            if not isinstance(raw, Mapping):
                raise TypeError(f"artifact binding {name!r} must be a mapping")
            payload = dict(raw)
            ref = payload.get("ref", payload.get("path"))
            digest = payload.get("sha256", payload.get("hash"))
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError(f"artifact binding {name!r} has no ref")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError(f"artifact binding {name!r} has no SHA-256")
            if self.root is not None:
                path = self.root / ref
                if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ReportPreflightError(f"artifact binding {name!r} is stale or tampered")
            result[str(name)] = {"ref": ref, "sha256": digest}
        return result

    @staticmethod
    def _collector_path(
        context: Any,
        relative_or_path: str | os.PathLike[str],
        *,
        label: str,
        directory: bool = False,
        require_exists: bool = True,
    ) -> Path:
        """Resolve one canonical run-local path without following aliases.

        Reporting is a read-only projection boundary.  It must not silently
        accept a path which resolves into another run (or another generation)
        through a symlink, and it must not turn an absolute path supplied by an
        artifact into a new authority.  The lifecycle already validates its
        own pointers; this helper applies the same lexical check to the
        item/product/telemetry artifacts discovered below.
        """

        raw = Path(relative_or_path).expanduser()
        candidate = raw if raw.is_absolute() else context.run_root / raw
        try:
            relative = candidate.relative_to(context.run_root)
        except ValueError as exc:
            raise ReportPreflightError(f"{label} is foreign to the run root") from exc
        if any(component in {".", ".."} for component in relative.parts):
            raise ReportPreflightError(f"{label} uses traversal components")
        current = context.run_root
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise ReportPreflightError(f"{label} contains a symlink")
        if require_exists:
            if directory:
                if not candidate.is_dir():
                    raise ReportPreflightError(f"{label} is missing or not a directory")
            elif not candidate.is_file():
                raise ReportPreflightError(f"{label} is missing or not a regular file")
        return candidate

    @classmethod
    def _collector_json(cls, context: Any, relative: str, *, label: str) -> tuple[Path, dict[str, Any]]:
        path = cls._collector_path(context, relative, label=label)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportPreflightError(f"{label} is invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise ReportPreflightError(f"{label} must contain an object")
        return path, dict(value)

    @classmethod
    def _collector_jsonl(cls, context: Any, relative: str, *, label: str) -> tuple[Path, list[dict[str, Any]]]:
        path = cls._collector_path(context, relative, label=label)
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_bytes().splitlines()
        except OSError as exc:
            raise ReportPreflightError(f"{label} is unreadable") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReportPreflightError(f"{label} line {line_number} is invalid JSON") from exc
            if not isinstance(value, Mapping):
                raise ReportPreflightError(f"{label} line {line_number} must contain an object")
            rows.append(dict(value))
        return path, rows

    @staticmethod
    def _collector_digest(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise ReportPreflightError(f"cannot hash canonical artifact: {path}") from exc
        return digest.hexdigest()

    @staticmethod
    def _collector_owned_ref(
        context: Any,
        value: Any,
        *,
        label: str,
        require_file: bool = False,
        require_exists: bool = True,
    ) -> str:
        """Validate an artifact-provided path and return a run-relative ref."""

        if not isinstance(value, str) or not value.strip():
            raise ReportPreflightError(f"{label} path is missing")
        raw = Path(value).expanduser()
        path = RunReportInputGatherer._collector_path(
            context,
            raw,
            label=label,
            directory=not require_file,
            require_exists=require_exists,
        )
        try:
            return path.relative_to(context.run_root).as_posix()
        except ValueError as exc:  # defensive: _collector_path already checks this
            raise ReportPreflightError(f"{label} is foreign to the run root") from exc

    @classmethod
    def _collector_receipts_and_timings(
        cls,
        context: Any,
        item_ids: Sequence[str],
        accepted_hashes: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        receipts: list[dict[str, Any]] = []
        timings: list[dict[str, Any]] = []
        seen_receipts: set[str] = set()
        for item_id in sorted(str(value) for value in item_ids):
            relative_dir = f"requirements/{item_id}/work/script_receipts"
            directory = cls._collector_path(context, relative_dir, label=f"{item_id} script receipts", directory=True)
            children = sorted(directory.iterdir(), key=lambda path: path.name)
            if any(child.is_symlink() for child in children):
                raise ReportPreflightError(f"{item_id} script receipts contain a symlink")
            if any(child.suffix != ".json" for child in children):
                raise ReportPreflightError(f"{item_id} script receipts contain a non-canonical artifact")
            json_children = [child for child in children if child.suffix == ".json"]
            if not json_children:
                raise ReportPreflightError(f"{item_id} has no canonical script receipts")
            expected_hashes = accepted_hashes.get(item_id, {})
            if not isinstance(expected_hashes, Mapping):
                raise ReportPreflightError(f"{item_id} accepted artifact hashes are invalid")
            expected_receipt_hashes: dict[str, Any] = {}
            for raw_ref, raw_digest in expected_hashes.items():
                ref = str(raw_ref)
                if ref.startswith("work/script_receipts/"):
                    if Path(ref).name != ref.removeprefix("work/script_receipts/") or not ref.endswith(".json"):
                        raise ReportPreflightError(f"{item_id} accepted receipt artifact path is invalid")
                    expected_receipt_hashes[ref] = raw_digest
            discovered_receipt_hashes = {
                f"work/script_receipts/{child.name}": cls._collector_digest(child)
                for child in json_children
            }
            if set(discovered_receipt_hashes) != set(expected_receipt_hashes):
                raise ReportPreflightError(f"{item_id} discovered receipts do not match accepted artifact bindings")
            for ref, digest in discovered_receipt_hashes.items():
                expected_digest = expected_receipt_hashes.get(ref)
                if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest) or expected_digest != digest:
                    raise ReportPreflightError(f"receipt {ref!r} is tampered")
            for child in json_children:
                if not child.is_file():
                    raise ReportPreflightError(f"{item_id} script receipt is not a regular file")
                try:
                    payload = json.loads(child.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReportPreflightError(f"{item_id} script receipt {child.name} is invalid JSON") from exc
                if not isinstance(payload, Mapping):
                    raise ReportPreflightError(f"{item_id} script receipt {child.name} must contain an object")
                payload = dict(payload)
                receipt_id = str(payload.get("receipt_id", "")).strip()
                if not receipt_id or receipt_id != child.stem:
                    raise ReportPreflightError(f"{item_id} script receipt identity is stale")
                if receipt_id in seen_receipts:
                    raise ReportPreflightError(f"receipt {receipt_id!r} appears more than once")
                seen_receipts.add(receipt_id)
                if payload.get("item_id") not in (None, item_id):
                    raise ReportPreflightError(f"receipt {receipt_id!r} references another item")
                # Absolute paths in runner receipts are evidence, not a new
                # authority.  Require each one to remain inside this run and
                # expose only canonical relative refs in the report.
                receipt_path = payload.get("receipt_path")
                if receipt_path is not None:
                    actual_ref = cls._collector_owned_ref(context, receipt_path, label=f"receipt {receipt_id}", require_file=True)
                    expected_ref = f"requirements/{item_id}/work/script_receipts/{child.name}"
                    if actual_ref != expected_ref:
                        raise ReportPreflightError(f"receipt {receipt_id!r} path is stale")
                for path_key in ("script_path", "context_path"):
                    if payload.get(path_key) is not None:
                        cls._collector_owned_ref(context, payload[path_key], label=f"receipt {receipt_id} {path_key}", require_file=True)
                output_hashes = payload.get("output_hashes")
                if output_hashes is not None:
                    if not isinstance(output_hashes, Mapping):
                        raise ReportPreflightError(f"receipt {receipt_id!r} output_hashes are invalid")
                    for output_path in output_hashes:
                        cls._collector_owned_ref(
                            context,
                            output_path,
                            label=f"receipt {receipt_id} output",
                            require_file=False,
                            require_exists=False,
                        )
                started = payload.get("started_at")
                finished = payload.get("finished_at")
                for timestamp_name, timestamp in (("started_at", started), ("finished_at", finished)):
                    if timestamp is not None and (not isinstance(timestamp, str) or not timestamp.strip()):
                        raise ReportPreflightError(f"receipt {receipt_id!r} {timestamp_name} is invalid")
                wall_seconds = payload.get("wall_seconds")
                if wall_seconds is not None and (
                    isinstance(wall_seconds, bool)
                    or not isinstance(wall_seconds, (int, float))
                    or wall_seconds < 0
                    or wall_seconds != wall_seconds
                ):
                    raise ReportPreflightError(f"receipt {receipt_id!r} wall_seconds is invalid")
                exit_code = payload.get("exit_code")
                if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
                    raise ReportPreflightError(f"receipt {receipt_id!r} exit_code is invalid")
                relative_ref = f"requirements/{item_id}/work/script_receipts/{child.name}"
                receipt = {
                    "invocation_id": receipt_id,
                    "receipt_id": receipt_id,
                    "item_id": item_id,
                    "phase": payload.get("phase"),
                    "started_at": started,
                    "finished_at": finished,
                    "wall_seconds": wall_seconds,
                    "exit_code": exit_code,
                    "error_category": payload.get("error_category"),
                    "error_type": payload.get("error_type"),
                    "timed_out": payload.get("timed_out"),
                    "script_hash": payload.get("script_hash"),
                    "source_hash": payload.get("source_hash"),
                    "context_hash": payload.get("context_hash"),
                    "artifact_ref": relative_ref,
                }
                receipts.append(receipt)
                timings.append(
                    {
                        "timing_id": f"TIM-{receipt_id}",
                        "phase": payload.get("phase") or "controlled_execution",
                        "item_id": item_id,
                        "start": started,
                        "finish": finished,
                        "wall_time_ms": None if wall_seconds is None else float(wall_seconds) * 1000.0,
                        "receipt_ref": receipt_id,
                        "artifact_ref": relative_ref,
                    }
                )
        if {str(item.get("item_id")) for item in receipts} != set(str(value) for value in item_ids):
            raise ReportPreflightError("script receipts do not cover every terminal item")
        return receipts, timings

    @classmethod
    def gather_from_run(
        cls,
        context: Any,
        *,
        persist: bool = True,
        generated_at: str | None = None,
        require_complete: bool = True,
    ) -> RunReportPreflight:
        """Collect report inputs from the run's canonical public artifacts.

        This adapter is intentionally deterministic and read-only apart from
        delegating optional preflight persistence to :meth:`gather`.  It never
        imports a coordinator, launches a provider, or manufactures missing
        model/provider/timing facts.
        """

        from .lifecycle import RunLifecycle, current_implementation_identity
        from .prepared import PreparedAssetRegistry
        from .durable import ItemWorkspace
        from .workspace import RunContext

        if not isinstance(context, RunContext):
            raise TypeError("gather_from_run requires a RunContext")
        try:
            lifecycle = RunLifecycle.load(context)
        except Exception as exc:
            raise ReportPreflightError(f"run lifecycle is unavailable: {exc}") from exc
        if lifecycle.context.run_id != context.run_id or lifecycle.context.run_root != context.run_root:
            raise ReportPreflightError("run lifecycle identity is stale")
        try:
            product_path = cls._collector_path(context, lifecycle.product_manifest_path, label="active product manifest")
            product_manifest = json.loads(product_path.read_text(encoding="utf-8"))
        except ReportPreflightError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportPreflightError("active product manifest is invalid") from exc
        if not isinstance(product_manifest, Mapping):
            raise ReportPreflightError("active product manifest must contain an object")
        product_manifest = dict(product_manifest)
        product_ref = product_path.relative_to(context.run_root).as_posix()
        product_lifecycle = product_manifest.get("lifecycle")
        if (
            product_manifest.get("run_id") != context.run_id
            or not isinstance(product_lifecycle, Mapping)
            or product_lifecycle.get("generation_id") != lifecycle.generation_id
            or product_lifecycle.get("all_items_terminal") is not True
            or product_lifecycle.get("all_items_integrated") is not True
            or product_manifest.get("terminal") is not True
            or product_manifest.get("status") not in {"complete", "complete_with_limits"}
        ):
            raise ReportPreflightError("active product manifest is not bound to the terminal generation")
        lem_counts = product_manifest.get("lem")
        if not isinstance(lem_counts, Mapping) or not lem_counts:
            raise ReportPreflightError("active product manifest is missing canonical LEM counts")
        for key, value in lem_counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReportPreflightError(f"active product manifest LEM count {key!r} is invalid")
        artifact_bindings: dict[str, dict[str, str]] = {
            "product_manifest": {"ref": product_ref, "sha256": cls._collector_digest(product_path)}
        }
        # Product asset refs are a compact integrity boundary and are all
        # generation-local in the accepted product.  A foreign or tampered
        # product asset must prevent report projection, never get silently
        # copied into a report.
        for index, asset in enumerate(product_manifest.get("assets", ())):
            if not isinstance(asset, Mapping):
                raise ReportPreflightError("active product manifest asset binding is invalid")
            asset_ref = asset.get("ref")
            asset_hash = asset.get("sha256")
            if not isinstance(asset_ref, str) or not isinstance(asset_hash, str) or not _SHA256.fullmatch(asset_hash):
                raise ReportPreflightError("active product manifest asset binding is invalid")
            asset_path = cls._collector_path(context, asset_ref, label=f"product asset {index}")
            if cls._collector_digest(asset_path) != asset_hash:
                raise ReportPreflightError(f"product asset {asset_ref!r} is tampered")
        if isinstance(product_manifest.get("presentation_plan_ref"), str):
            presentation_ref = product_manifest["presentation_plan_ref"]
            presentation_path = cls._collector_path(context, presentation_ref, label="business presentation plan")
            expected_presentation_hash = product_manifest.get("presentation_plan_sha256")
            if expected_presentation_hash is not None and (
                not isinstance(expected_presentation_hash, str)
                or not _SHA256.fullmatch(expected_presentation_hash)
                or cls._collector_digest(presentation_path) != expected_presentation_hash
            ):
                raise ReportPreflightError("business presentation plan is tampered")
            artifact_bindings["presentation_plan"] = {
                "ref": presentation_path.relative_to(context.run_root).as_posix(),
                "sha256": cls._collector_digest(presentation_path),
            }

        item_reports: list[dict[str, Any]] = []
        item_manifests: list[dict[str, Any]] = []
        accepted_hashes: dict[str, Mapping[str, Any]] = {}
        business_reviews: list[dict[str, Any]] = []
        fidelity_reviews: list[dict[str, Any]] = []
        for item_id in sorted(lifecycle.item_ids):
            item_prefix = f"requirements/{item_id}"
            _state_path, state = cls._collector_json(context, f"{item_prefix}/item_state.json", label=f"{item_id} item state")
            accepted_path, accepted_manifest = cls._collector_json(context, f"{item_prefix}/accepted/manifest.json", label=f"{item_id} accepted manifest")
            envelope_path, envelope = cls._collector_json(context, f"{item_prefix}/accepted/acceptance_envelope.json", label=f"{item_id} acceptance envelope")
            committed_path, committed_manifest = cls._collector_json(context, f"{item_prefix}/integration/committed/manifest.json", label=f"{item_id} integration manifest")
            session_path, session = cls._collector_json(context, f"{item_prefix}/integration/staging/session.json", label=f"{item_id} integration session")
            snapshot_path, snapshot = cls._collector_json(context, f"{item_prefix}/integration/staging/snapshot.json", label=f"{item_id} integration snapshot")
            review_path, fidelity = cls._collector_json(context, f"{item_prefix}/integration/review/result.json", label=f"{item_id} fidelity review")
            business_path, _business_raw = cls._collector_json(context, f"{item_prefix}/work/business_review.json", label=f"{item_id} business review")
            try:
                business_workspace = ItemWorkspace(
                    context,
                    item_id,
                    mode="requirement",
                    original_text=str(state.get("original_text", "")),
                    state=state,
                )
                business = business_workspace._read_business_review()
            except Exception as exc:
                raise ReportPreflightError(f"{item_id} business review is invalid: {exc}") from exc
            if not isinstance(business, Mapping):
                raise ReportPreflightError(f"{item_id} business review is missing")
            if state.get("item_id") != item_id or accepted_manifest.get("item_id") != item_id or envelope.get("item_id") != item_id or committed_manifest.get("item_id") != item_id or session.get("item_id") != item_id or snapshot.get("state", {}).get("item_id") != item_id or fidelity.get("item_id") != item_id or business.get("item_id") != item_id:
                raise ReportPreflightError(f"{item_id} artifact identity is stale")
            if accepted_manifest.get("manifest_hash") != _hash_mapping(accepted_manifest, "manifest_hash"):
                raise ReportPreflightError(f"{item_id} accepted manifest is tampered")
            if committed_manifest.get("manifest_hash") != _hash_mapping(committed_manifest, "manifest_hash"):
                raise ReportPreflightError(f"{item_id} integration manifest is tampered")
            # The committed manifest is the integration-side projection of the
            # accepted bundle.  Both hashes are authoritative links, not
            # optional hints; a self-consistent manifest that points at a
            # different accepted bundle is stale.
            if committed_manifest.get("accepted_manifest_hash") != accepted_manifest.get("manifest_hash"):
                raise ReportPreflightError(f"{item_id} committed accepted_manifest_hash binding is stale")
            if committed_manifest.get("accepted_content_hash") != accepted_manifest.get("content_hash"):
                raise ReportPreflightError(f"{item_id} committed accepted_content_hash binding is stale")
            expected_session_fields = {
                "schema_version", "session_id", "item_id", "owner_id", "invocation_id", "status",
                "accepted_content_hash", "accepted_manifest_hash", "records_count", "records_hash",
                "created_at", "updated_at", "state_hash",
            }
            if (
                not expected_session_fields.issubset(session)
                or set(session) - (expected_session_fields | {"unreviewed_removed_record_hashes"})
                or session.get("schema_version") != "1"
                or session.get("status") != "committed"
                or session.get("state_hash") != _hash_mapping(session, "state_hash")
            ):
                raise ReportPreflightError(f"{item_id} integration session is invalid")
            expected_snapshot_fields = {"schema_version", "state", "records", "snapshot_hash"}
            if (
                set(snapshot) != expected_snapshot_fields
                or snapshot.get("schema_version") != "1"
                or snapshot.get("snapshot_hash") != _hash_mapping(snapshot, "snapshot_hash")
                or snapshot.get("state") != session
                or not isinstance(snapshot.get("records"), list)
            ):
                raise ReportPreflightError(f"{item_id} integration snapshot is invalid")
            if (
                session.get("accepted_manifest_hash") != committed_manifest.get("accepted_manifest_hash")
                or session.get("accepted_content_hash") != committed_manifest.get("accepted_content_hash")
                or session.get("session_id") != committed_manifest.get("session_id")
                or session.get("invocation_id") != committed_manifest.get("invocation_id")
                or session.get("records_hash") != committed_manifest.get("records_hash")
                or session.get("records_count") != committed_manifest.get("records_count")
            ):
                raise ReportPreflightError(f"{item_id} integration session binding is stale")
            content_path = cls._collector_path(context, f"{item_prefix}/accepted/{accepted_manifest.get('content_path', 'answer_content.json')}", label=f"{item_id} accepted content")
            if accepted_manifest.get("content_hash") != cls._collector_digest(content_path):
                raise ReportPreflightError(f"{item_id} accepted content is tampered")
            if accepted_manifest.get("envelope_hash") != cls._collector_digest(envelope_path):
                raise ReportPreflightError(f"{item_id} acceptance envelope is tampered")
            records_path = cls._collector_path(context, f"{item_prefix}/integration/committed/{committed_manifest.get('records_path', 'records.jsonl')}", label=f"{item_id} committed records")
            if committed_manifest.get("records_hash") != cls._collector_digest(records_path):
                raise ReportPreflightError(f"{item_id} committed records are tampered")
            staging_records_path = cls._collector_path(context, f"{item_prefix}/integration/staging/records.jsonl", label=f"{item_id} staged records")
            staging_records = b"".join(_json_bytes(record) for record in snapshot["records"])
            try:
                staging_records_bytes = staging_records_path.read_bytes()
            except OSError as exc:
                raise ReportPreflightError(f"{item_id} staged records are unreadable") from exc
            if staging_records_bytes != staging_records or hashlib.sha256(staging_records_bytes).hexdigest() != session.get("records_hash"):
                raise ReportPreflightError(f"{item_id} staged records binding is stale")
            terminal_intent = state.get("terminal_intent")
            terminal_outcome = state.get("terminal_outcome")
            if (
                not isinstance(terminal_intent, Mapping)
                or set(terminal_intent) != {"outcome", "manifest_hash"}
                or terminal_intent.get("manifest_hash") != accepted_manifest.get("manifest_hash")
                or terminal_intent.get("outcome") != accepted_manifest.get("outcome")
            ):
                raise ReportPreflightError(f"{item_id} terminal intent is stale")
            if (
                not isinstance(terminal_outcome, Mapping)
                or set(terminal_outcome) != {"status", "item_id", "outcome", "manifest_path", "content_hash"}
                or terminal_outcome.get("status") != accepted_manifest.get("outcome")
                or terminal_outcome.get("item_id") != item_id
                or terminal_outcome.get("outcome") != accepted_manifest.get("outcome")
                or terminal_outcome.get("content_hash") != accepted_manifest.get("content_hash")
            ):
                raise ReportPreflightError(f"{item_id} terminal outcome is stale")
            terminal_manifest_path = cls._collector_path(
                context,
                terminal_outcome.get("manifest_path"),
                label=f"{item_id} terminal manifest",
            )
            if terminal_manifest_path != accepted_path:
                raise ReportPreflightError(f"{item_id} terminal manifest reference is stale")
            review = state.get("review")
            if not isinstance(review, Mapping):
                raise ReportPreflightError(f"{item_id} item review is missing")
            if (
                any(field not in envelope for field in ("outcome", "reviewer_ref", "draft_hash", "content_hash"))
                or any(field not in review for field in ("reviewer_ref", "draft_hash"))
                or envelope.get("outcome") != accepted_manifest.get("outcome")
                or envelope.get("content_hash") != accepted_manifest.get("content_hash")
                or envelope.get("draft_hash") != accepted_manifest.get("content_hash")
                or envelope.get("reviewer_ref") != review.get("reviewer_ref")
                or envelope.get("draft_hash") != review.get("draft_hash")
            ):
                raise ReportPreflightError(f"{item_id} acceptance envelope binding is stale")
            if accepted_manifest.get("outcome") != terminal_outcome.get("outcome") or committed_manifest.get("status") != "committed" or state.get("integration_state") != "integrated":
                raise ReportPreflightError(f"{item_id} lifecycle/integration binding is stale")
            outcome = str(state.get("terminal_outcome", {}).get("outcome", ""))
            lifecycle_state = str(state.get("lifecycle_state", ""))
            if not outcome or not lifecycle_state:
                raise ReportPreflightError(f"{item_id} terminal lifecycle is missing")
            counts = committed_manifest.get("counts", {})
            if not isinstance(counts, Mapping) or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
                raise ReportPreflightError(f"{item_id} integration record counts are invalid")
            item_reports.append({
                "item_id": item_id,
                "outcome": outcome,
                "lifecycle_state": lifecycle_state,
                "record_kind_totals": dict(counts),
            })
            item_manifest = dict(accepted_manifest)
            item_manifest["terminal_outcome"] = {"outcome": outcome}
            item_manifest["record_kind_totals"] = dict(counts)
            item_manifests.append(item_manifest)
            accepted_hashes[item_id] = (accepted_manifest.get("artifact_progress") or {}).get("hashes", {}) if isinstance(accepted_manifest.get("artifact_progress"), Mapping) else {}
            draft_hash = state.get("review", {}).get("draft_hash") if isinstance(state.get("review"), Mapping) else None
            reviewed_hash = business.get("reviewed_draft_hash")
            if draft_hash is not None and reviewed_hash is not None and draft_hash != reviewed_hash:
                raise ReportPreflightError(f"{item_id} business review draft binding is stale")
            business_findings = business.get("findings", [])
            if not isinstance(business_findings, list):
                raise ReportPreflightError(f"{item_id} business review findings are invalid")
            business_reviews.append({
                "review_id": f"BR-{item_id}",
                "item_id": item_id,
                "review_kind": "business",
                "reviewer_ref": envelope.get("reviewer_ref") or (state.get("review", {}) or {}).get("reviewer_ref"),
                "verdict": envelope.get("review_verdict") or (state.get("review", {}) or {}).get("verdict"),
                "review_scope": business.get("review_scope"),
                "reviewed_draft_hash": reviewed_hash,
                "findings": business_findings,
                "repairs": [],
                "targeted_rechecks": ([{"recheck_id": f"RR-{item_id}", "scope": business.get("changed_pointers", [])}] if business.get("targeted_recheck") else []),
                "artifact_ref": business_path.relative_to(context.run_root).as_posix(),
            })
            fidelity_findings = fidelity.get("findings", [])
            if not isinstance(fidelity_findings, list):
                raise ReportPreflightError(f"{item_id} fidelity findings are invalid")
            if (
                fidelity.get("records_hash") != committed_manifest.get("records_hash")
                or fidelity.get("records_hash") != session.get("records_hash")
                or fidelity.get("session_id") != committed_manifest.get("session_id")
                or fidelity.get("session_id") != session.get("session_id")
                or fidelity.get("invocation_id") != committed_manifest.get("invocation_id")
                or fidelity.get("invocation_id") != session.get("invocation_id")
            ):
                raise ReportPreflightError(f"{item_id} fidelity review binding is stale")
            fidelity_hash = fidelity.get("result_hash")
            if not isinstance(fidelity_hash, str) or not _SHA256.fullmatch(fidelity_hash):
                raise ReportPreflightError(f"{item_id} fidelity review hash is invalid")
            fidelity_unsigned = {key: value for key, value in fidelity.items() if key != "result_hash"}
            if hashlib.sha256(_json_bytes(fidelity_unsigned)[:-1]).hexdigest() != fidelity_hash:
                raise ReportPreflightError(f"{item_id} fidelity review is tampered")
            fidelity_reviews.append({
                "review_id": f"FR-{item_id}-{fidelity_hash[:16]}",
                "item_id": item_id,
                "review_kind": "fidelity",
                "fidelity_review_kind": fidelity.get("review_kind"),
                "verdict": fidelity.get("verdict"),
                "findings": fidelity_findings,
                "repairs": [],
                "targeted_rechecks": [],
                "packet_hash": fidelity.get("packet_hash"),
                "result_hash": fidelity_hash,
                "records_hash": fidelity.get("records_hash"),
                "session_id": fidelity.get("session_id"),
                "checked_record_ids": fidelity.get("checked_record_ids", []),
                "affected_record_ids": fidelity.get("affected_record_ids", []),
                "artifact_ref": review_path.relative_to(context.run_root).as_posix(),
            })

        receipts, timings = cls._collector_receipts_and_timings(context, lifecycle.item_ids, accepted_hashes)
        incidents: list[dict[str, Any]] = []
        telemetry_path, telemetry_rows = cls._collector_jsonl(context, "telemetry/events.jsonl", label="telemetry events")
        for event in telemetry_rows:
            if event.get("event_type") != "incident":
                continue
            facts = event.get("facts")
            if not isinstance(facts, Mapping):
                raise ReportPreflightError("incident event facts are invalid")
            incidents.append(dict(facts))
        artifact_bindings["telemetry_events"] = {"ref": telemetry_path.relative_to(context.run_root).as_posix(), "sha256": cls._collector_digest(telemetry_path)}

        try:
            descriptors = PreparedAssetRegistry(context).search(include_superseded=True)
        except Exception as exc:
            raise ReportPreflightError(f"prepared registry is unavailable: {exc}") from exc
        active = [descriptor for descriptor in descriptors if getattr(descriptor, "status", None) != "superseded" and getattr(descriptor, "scope", None) != "superseded"]
        for descriptor in descriptors:
            cls._collector_owned_ref(context, getattr(descriptor, "location", None), label=f"prepared asset {getattr(descriptor, 'prepared_asset_id', '')}", require_file=True)
        registry_snapshot = {
            "counts": {
                "prepared_asset_count": len(descriptors),
                "active_prepared_asset_count": len(active),
                "reusable_prepared_asset_count": sum(1 for descriptor in active if getattr(descriptor, "scope", None) == "reusable"),
            },
            "generation_id": lifecycle.generation_id,
        }
        if "prepared_assets" in lem_counts and int(lem_counts["prepared_assets"]) != registry_snapshot["counts"]["active_prepared_asset_count"]:
            raise ReportPreflightError("active product LEM prepared asset count does not match the prepared registry")
        try:
            identity_sha, identity_tree = current_implementation_identity(context)
        except Exception as exc:
            raise ReportPreflightError(f"implementation identity is unavailable: {exc}") from exc
        version = f"skill {context.skill_version} / auto_foundry_core {context.core_version}" if context.skill_version is not None else f"auto_foundry_core {context.core_version}"
        identity = {"sha": identity_sha, "tree": identity_tree, "version": version}
        for item in item_reports:
            item.update({"implementation_sha": identity_sha, "implementation_tree": identity_tree, "implementation_version": version})
        transitions = tuple(_jsonable(value) for value in lifecycle.implementation_transitions)
        metadata: dict[str, Any] = {"final": identity}
        if transitions:
            first = transitions[0]
            if isinstance(first, Mapping):
                metadata["initial"] = {"sha": first.get("old_sha"), "tree": first.get("old_tree"), "version": first.get("old_version")}
                metadata["intermediate"] = [
                    {"sha": value.get("new_sha"), "tree": value.get("new_tree"), "version": value.get("new_version")}
                    for value in transitions[:-1]
                    if isinstance(value, Mapping)
                ]
        preflight = cls(context.run_root, run_id=context.run_id, require_complete=require_complete).gather(
            item_reports,
            item_manifests=item_manifests,
            registry_snapshot=registry_snapshot,
            lem_snapshot={"counts": dict(lem_counts), "generation_id": lifecycle.generation_id, "source_ref": product_ref},
            event_bindings=RunReportEventBindings(
                invocation_receipts=tuple(receipts),
                timings=tuple(timings),
                incidents=tuple(incidents),
                business_reviews=tuple(business_reviews),
                fidelity_reviews=tuple(fidelity_reviews),
                implementation_transitions=transitions,
                implementation_metadata=metadata,
                implementation_identity=identity,
            ),
            lifecycle_status=lifecycle.state,
            generated_at=generated_at,
            artifact_bindings=artifact_bindings,
            persist=persist,
        )
        return preflight

    def gather(
        self,
        item_reports: Any,
        *,
        item_manifests: Any = _UNSUPPLIED,
        registry_snapshot: Mapping[str, Any] | object = _UNSUPPLIED,
        lem_snapshot: Mapping[str, Any] | object = _UNSUPPLIED,
        event_bindings: RunReportEventBindings | Mapping[str, Any] | None = None,
        receipts: Iterable[Any] | object = _UNSUPPLIED,
        timings: Iterable[Any] | object = _UNSUPPLIED,
        incidents: Iterable[Any] | object = _UNSUPPLIED,
        business_reviews: Iterable[Any] | object = _UNSUPPLIED,
        fidelity_reviews: Iterable[Any] | object = _UNSUPPLIED,
        implementation_records: Any = None,
        implementation_transitions: Iterable[Any] | object = _UNSUPPLIED,
        implementation_metadata: Mapping[str, Any] | object = _UNSUPPLIED,
        implementation_identity: Mapping[str, Any] | object = _UNSUPPLIED,
        lifecycle_status: Any = None,
        generated_at: str | None = None,
        artifact_bindings: Mapping[str, Any] | None = None,
        persist: bool = True,
    ) -> RunReportPreflight:
        reports = _as_records(item_reports, label="item_reports")
        bindings = RunReportEventBindings.from_value(event_bindings) if event_bindings is not None else RunReportEventBindings()
        def _effective(keyword: Any, envelope: Any) -> tuple[Any, bool]:
            if keyword is not _UNSUPPLIED:
                # Explicit ``None`` is still an omitted binding.  Empty
                # containers, in contrast, are preserved as supplied.
                return keyword, keyword is not None
            return envelope, envelope is not None

        manifests_value, manifests_supplied = _effective(item_manifests, bindings.item_manifests)
        registry_value, registry_supplied = _effective(registry_snapshot, bindings.registry_snapshot)
        lem_value, lem_supplied = _effective(lem_snapshot, bindings.lem_snapshot)
        receipts_value, receipts_supplied = _effective(receipts, bindings.invocation_receipts)
        timings_value, timings_supplied = _effective(timings, bindings.timings)
        incidents_value, incidents_supplied = _effective(incidents, bindings.incidents)
        business_reviews_value, business_reviews_supplied = _effective(business_reviews, bindings.business_reviews)
        fidelity_reviews_value, fidelity_reviews_supplied = _effective(fidelity_reviews, bindings.fidelity_reviews)
        transitions_value, transitions_supplied = _effective(implementation_transitions, bindings.implementation_transitions)
        metadata_value, metadata_supplied = _effective(implementation_metadata, bindings.implementation_metadata)
        identity_value, identity_supplied = _effective(implementation_identity, bindings.implementation_identity)
        # Explicit keyword arguments override the typed envelope so adapters
        # can migrate incrementally without importing coordinator internals.
        bindings = RunReportEventBindings(
            invocation_receipts=receipts_value,
            item_manifests=manifests_value,
            registry_snapshot=registry_value,
            lem_snapshot=lem_value,
            timings=timings_value,
            incidents=incidents_value,
            business_reviews=business_reviews_value,
            fidelity_reviews=fidelity_reviews_value,
            implementation_transitions=transitions_value,
            implementation_metadata=metadata_value,
            implementation_identity=identity_value,
        )
        metadata = bindings.implementation_metadata
        if metadata is None and bindings.implementation_identity is not None:
            metadata = bindings.implementation_identity
        try:
            projected = RunReportProjector(run_id=self.run_id).project(
                reports,
                item_manifests=() if manifests_value is None else manifests_value,
                registry_snapshot={} if registry_value is None else registry_value,
                lem_snapshot={} if lem_value is None else lem_value,
                receipts=() if bindings.invocation_receipts is None else bindings.invocation_receipts,
                timings=() if bindings.timings is None else bindings.timings,
                incidents=() if bindings.incidents is None else bindings.incidents,
                business_reviews=() if bindings.business_reviews is None else bindings.business_reviews,
                fidelity_reviews=() if bindings.fidelity_reviews is None else bindings.fidelity_reviews,
                implementation_records=implementation_records,
                implementation_transitions=() if bindings.implementation_transitions is None else bindings.implementation_transitions,
                implementation_metadata=metadata,
                lifecycle_status=lifecycle_status,
                generated_at=generated_at,
            )
        except (TypeError, ValueError) as exc:
            raise ReportPreflightError(f"report preflight inputs are invalid: {exc}") from exc
        supplied = {
            "item_manifests": manifests_supplied,
            "registry_snapshot": registry_supplied,
            "lem_snapshot": lem_supplied,
            "invocation_receipts": receipts_supplied,
            "timings": timings_supplied,
            "incidents": incidents_supplied,
            "business_reviews": business_reviews_supplied,
            "fidelity_reviews": fidelity_reviews_supplied,
            "implementation_transitions": transitions_supplied,
            "implementation_metadata": metadata_supplied,
            "implementation_identity": identity_supplied,
        }
        self._require_inputs(
            reports,
            bindings,
            projected,
            supplied=supplied,
            item_manifests=_as_records(manifests_value, label="item_manifests"),
            implementation_identity=identity_value if isinstance(identity_value, Mapping) else None,
        )
        item_ids = tuple(sorted(str(item.get("item_id")) for item in reports))
        input_values = {
            "item_reports": reports,
            "item_manifests": _as_records(manifests_value, label="item_manifests"),
            "registry_snapshot": {} if registry_value is None else registry_value,
            "lem_snapshot": {} if lem_value is None else lem_value,
            "invocation_receipts": projected.get("receipts", []),
            "timings": projected.get("timings", []),
            "incidents": projected.get("incidents", []),
            "business_reviews": projected.get("business_reviews", []),
            "fidelity_reviews": projected.get("fidelity_reviews", []),
            "implementation_transitions": projected.get("implementation_transitions", []),
            "implementation_metadata": projected.get("implementation_metadata", {}),
            "implementation_identity": {} if identity_value is None else identity_value,
        }
        preflight = RunReportPreflight(
            run_id=self.run_id,
            item_ids=item_ids,
            projected_report=projected,
            input_counts={key: len(value) if isinstance(value, list) else (len(value) if isinstance(value, Mapping) else 0) for key, value in input_values.items()},
            input_hashes={key: hashlib.sha256(_json_bytes(value)).hexdigest() for key, value in input_values.items()},
            artifact_bindings=self._artifact_bindings(artifact_bindings),
        )
        if persist and self.preflight_path is not None:
            path = self.preflight_path
            if path.exists() or path.is_symlink():
                existing = RunReportPreflight.from_dict(_load_json(path, "report preflight"))
                if existing.to_dict() != preflight.to_dict():
                    raise ReportPreflightError("existing report preflight is stale or tampered")
                return existing
            _atomic_write(path, _json_bytes(preflight.to_dict()))
        return preflight

    preflight = gather

    def load(self) -> RunReportPreflight:
        if self.preflight_path is None:
            raise ValueError("report preflight has no persistence root")
        return RunReportPreflight.from_dict(_load_json(self.preflight_path, "report preflight"))


class RunReportFinalizer:
    """Atomically publish and verify the report/manifest/receipt triplet."""

    def __init__(self, root: Any = None) -> None:
        self.root = _safe_root(root) if root is not None else None

    @property
    def intent_path(self) -> Path | None:
        return None if self.root is None else self.root / _FINALIZE_INTENT_FILENAME

    @staticmethod
    def _triplet_paths(directory: Path) -> tuple[Path, Path, Path]:
        return (
            directory / _REPORT_FILENAME,
            directory / _MANIFEST_FILENAME,
            directory / _RECEIPT_FILENAME,
        )

    @staticmethod
    def _tree_hash(directory: Path) -> str | None:
        """Hash every regular file below one transaction-owned directory."""

        if not directory.exists() and not directory.is_symlink():
            return None
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("reporting transaction target is not a directory")
        files: dict[str, str] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("reporting transaction target contains a symlink")
            if path.is_file():
                files[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashlib.sha256(_json_bytes({"files": files})).hexdigest()

    @staticmethod
    def _read_triplet(directory: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("reporting path is not a directory")
        paths = RunReportFinalizer._triplet_paths(directory)
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("final report triplet is partial")
        try:
            report = _load_json(paths[0], "final report")
            manifest = _load_json(paths[1], "run manifest")
            receipt = _load_json(paths[2], "terminalization receipt")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("existing finalization artifacts are invalid") from exc
        if (
            report.get("report_hash") != _hash_mapping(report, "report_hash")
            or manifest.get("manifest_hash") != _hash_mapping(manifest, "manifest_hash")
            or receipt.get("receipt_hash") != _hash_mapping(receipt, "receipt_hash")
        ):
            raise ValueError("existing finalization artifact hash is invalid")
        if (
            manifest.get("report_hash") != report.get("report_hash")
            or receipt.get("report_hash") != report.get("report_hash")
            or receipt.get("manifest_hash") != manifest.get("manifest_hash")
        ):
            raise ValueError("existing finalization artifact binding is stale")
        return report, manifest, receipt

    def _intent(
        self,
        *,
        staging: Path,
        backup: Path,
        report: Mapping[str, Any],
        manifest: Mapping[str, Any],
        receipt: Mapping[str, Any],
        phase: str,
        old_tree_sha256: str | None,
    ) -> dict[str, Any]:
        if self.root is None:
            raise ValueError("report finalization has no persistence root")
        def ref(path: Path) -> str:
            return path.relative_to(self.root).as_posix()
        unsigned = {
            "schema_version": "2",
            "final_dir": _REPORT_DIRNAME,
            "staging_ref": ref(staging),
            "backup_ref": ref(backup),
            "old_tree_sha256": old_tree_sha256,
            "report_hash": str(report["report_hash"]),
            "manifest_hash": str(manifest["manifest_hash"]),
            "receipt_hash": str(receipt["receipt_hash"]),
            "phase": phase,
        }
        return {**unsigned, "intent_hash": hashlib.sha256(_json_bytes(unsigned)).hexdigest()}

    def _write_intent(self, intent: Mapping[str, Any]) -> None:
        if self.intent_path is None:
            raise ValueError("report finalization has no persistence root")
        _atomic_write(self.intent_path, _json_bytes(intent))

    def _load_intent(self) -> dict[str, Any] | None:
        if self.intent_path is None or not self.intent_path.exists():
            return None
        if self.intent_path.is_symlink() or not self.intent_path.is_file():
            raise ValueError("report finalization intent is invalid")
        value = _load_json(self.intent_path, "report finalization intent")
        expected = {"schema_version", "final_dir", "staging_ref", "backup_ref", "old_tree_sha256", "report_hash", "manifest_hash", "receipt_hash", "phase", "intent_hash"}
        if set(value) != expected or value.get("schema_version") != "2":
            raise ValueError("report finalization intent fields are invalid")
        unsigned = {key: item for key, item in value.items() if key != "intent_hash"}
        if value.get("intent_hash") != hashlib.sha256(_json_bytes(unsigned)).hexdigest():
            raise ValueError("report finalization intent hash is invalid")
        if value.get("final_dir") != _REPORT_DIRNAME or value.get("phase") not in {"preparing", "prepared", "backup_moved", "published"}:
            raise ValueError("report finalization intent binding is invalid")
        old_tree = value.get("old_tree_sha256")
        if old_tree is not None:
            _validate_optional_sha256(old_tree, "report finalization intent old_tree_sha256")
        for field in ("report_hash", "manifest_hash", "receipt_hash"):
            _validate_optional_sha256(value.get(field), f"report finalization intent {field}")
        return value

    def _resolve_transaction_path(self, ref: Any, prefix: str) -> Path:
        if self.root is None or not isinstance(ref, str) or not ref.startswith(prefix):
            raise ValueError("report finalization transaction path is invalid")
        path = (self.root / ref).resolve(strict=False)
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("report finalization transaction path escapes run root") from exc
        if path.name != ref.rsplit("/", 1)[-1]:
            raise ValueError("report finalization transaction path is invalid")
        return path

    def _cleanup_transaction(self, staging: Path, backup: Path) -> None:
        for path in (staging, backup):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path)
        if self.intent_path is not None and (self.intent_path.exists() or self.intent_path.is_symlink()):
            self.intent_path.unlink()

    def _expected_triplet_matches(
        self,
        final_dir: Path,
        expected: tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]],
    ) -> bool:
        if not final_dir.exists() or final_dir.is_symlink() or not final_dir.is_dir():
            return False
        try:
            return self._read_triplet(final_dir) == tuple(expected)
        except (OSError, ValueError):
            return False

    def _intent_hashes_match(
        self,
        intent: Mapping[str, Any],
        expected: tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]],
    ) -> None:
        expected_hashes = (
            ("report_hash", expected[0].get("report_hash")),
            ("manifest_hash", expected[1].get("manifest_hash")),
            ("receipt_hash", expected[2].get("receipt_hash")),
        )
        if any(intent[field] != value for field, value in expected_hashes):
            raise ValueError("report finalization intent is bound to another triplet")

    def _recover_transaction(self, final_dir: Path, expected: tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]) -> None:
        intent = self._load_intent()
        known = [
            path
            for path in self.root.iterdir()
            if path.name.startswith(_FINALIZE_STAGING_PREFIX) or path.name.startswith(_FINALIZE_BACKUP_PREFIX)
        ] if self.root is not None else []
        if intent is None:
            if not known:
                return
            raise ValueError("unbound report finalization transaction residue")
        staging = self._resolve_transaction_path(intent["staging_ref"], _FINALIZE_STAGING_PREFIX)
        backup = self._resolve_transaction_path(intent["backup_ref"], _FINALIZE_BACKUP_PREFIX)
        self._intent_hashes_match(intent, expected)
        old_tree = intent.get("old_tree_sha256")
        current_tree = self._tree_hash(final_dir)
        backup_tree = self._tree_hash(backup)

        # A complete target is authoritative even if the process died before
        # the journal cleanup.  Validate all three hashes before removing only
        # this transaction's staging/backup paths.
        if self._expected_triplet_matches(final_dir, expected):
            self._cleanup_transaction(staging, backup)
            return

        # A process death during preparation leaves either no staging path or
        # a partial one.  The old target must still be byte-identical before
        # those owned paths are discarded; otherwise fail closed.
        if intent.get("phase") == "preparing":
            if current_tree == old_tree:
                if backup.exists() or backup.is_symlink():
                    if backup_tree != old_tree:
                        raise ValueError("report finalization backup is not the bound old target")
                self._cleanup_transaction(staging, backup)
                return
            if current_tree is None and backup_tree == old_tree:
                os.replace(backup, final_dir)
                self._cleanup_transaction(staging, backup)
                return
            raise ValueError("report finalization preparation changed the old target")

        # After staging verification, converge a process death at either side
        # of the directory swap.  A missing staging path is recoverable only
        # when the complete target is already installed.
        if staging.exists() and not staging.is_symlink():
            staged = self._read_triplet(staging)
            if tuple(staged) != tuple(expected):
                raise ValueError("report finalization staging conflicts with intent")
        elif intent.get("phase") in {"prepared", "backup_moved"}:
            raise ValueError("report finalization staging is missing")
        else:
            self._cleanup_transaction(staging, backup)
            return
        if final_dir.exists() or final_dir.is_symlink():
            if current_tree != old_tree:
                raise ValueError("report finalization target is not the bound old tree")
            if backup.exists() or backup.is_symlink():
                raise ValueError("report finalization backup is conflicting")
            os.replace(final_dir, backup)
        elif backup.exists() or backup.is_symlink():
            if backup_tree != old_tree:
                raise ValueError("report finalization backup is not the bound old target")
        os.replace(staging, final_dir)
        if not self._expected_triplet_matches(final_dir, expected):
            raise ValueError("report finalization recovery did not converge")
        self._cleanup_transaction(staging, backup)

    def _publish_transaction(self, final_dir: Path, report: Mapping[str, Any], manifest: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
        if self.root is None:
            raise ValueError("report finalization has no persistence root")
        token = uuid.uuid4().hex
        staging = self.root / f"{_FINALIZE_STAGING_PREFIX}{token}"
        backup = self.root / f"{_FINALIZE_BACKUP_PREFIX}{token}"
        if staging.exists() or staging.is_symlink() or backup.exists() or backup.is_symlink():
            raise ValueError("report finalization transaction paths already exist")
        old_tree = self._tree_hash(final_dir)
        intent = self._intent(
            staging=staging,
            backup=backup,
            report=report,
            manifest=manifest,
            receipt=receipt,
            phase="preparing",
            old_tree_sha256=old_tree,
        )
        # The journal is durable before the first mkdir/copy/write.  Recovery
        # can therefore distinguish an owned partial staging path from an
        # unrelated directory and prove the target remained unchanged.
        self._write_intent(intent)
        try:
            if final_dir.exists() or final_dir.is_symlink():
                if final_dir.is_symlink() or not final_dir.is_dir():
                    raise ValueError("reporting path is not a directory")
                shutil.copytree(final_dir, staging)
            else:
                staging.mkdir(parents=True, exist_ok=False)
            _atomic_write(staging / _REPORT_FILENAME, _json_bytes(report))
            _atomic_write(staging / _MANIFEST_FILENAME, _json_bytes(manifest))
            _atomic_write(staging / _RECEIPT_FILENAME, _json_bytes(receipt))
            if self._read_triplet(staging) != (dict(report), dict(manifest), dict(receipt)):
                raise ValueError("report finalization staging verification failed")
            intent = self._intent(
                staging=staging,
                backup=backup,
                report=report,
                manifest=manifest,
                receipt=receipt,
                phase="prepared",
                old_tree_sha256=old_tree,
            )
            self._write_intent(intent)
            if final_dir.exists() or final_dir.is_symlink():
                os.replace(final_dir, backup)
                intent = self._intent(
                    staging=staging,
                    backup=backup,
                    report=report,
                    manifest=manifest,
                    receipt=receipt,
                    phase="backup_moved",
                    old_tree_sha256=old_tree,
                )
                self._write_intent(intent)
            os.replace(staging, final_dir)
            intent = self._intent(
                staging=staging,
                backup=backup,
                report=report,
                manifest=manifest,
                receipt=receipt,
                phase="published",
                old_tree_sha256=old_tree,
            )
            self._write_intent(intent)
            self._recover_transaction(final_dir, (report, manifest, receipt))
        except Exception:
            # Ordinary I/O/validation failures are not process death.  Restore
            # the old target and remove only this journal's owned paths.  A
            # BaseException (the testable process-death boundary) deliberately
            # leaves the intent for the next fresh process to reconcile.
            try:
                current_tree = self._tree_hash(final_dir)
                backup_tree = self._tree_hash(backup)
                if backup.exists() or backup.is_symlink():
                    if backup_tree != old_tree:
                        raise ValueError("report finalization backup changed unexpectedly")
                    if final_dir.exists() or final_dir.is_symlink():
                        if current_tree != old_tree:
                            if final_dir.is_symlink() or not final_dir.is_dir():
                                raise ValueError("report finalization target changed unexpectedly")
                            shutil.rmtree(final_dir)
                        else:
                            shutil.rmtree(backup)
                            backup = self.root / f"{_FINALIZE_BACKUP_PREFIX}{token}.removed"
                    if backup.exists() or backup.is_symlink():
                        os.replace(backup, final_dir)
                elif current_tree != old_tree:
                    raise ValueError("report finalization target changed unexpectedly")
                self._cleanup_transaction(staging, backup)
            except Exception:
                # Preserve the journal if rollback itself cannot prove the old
                # target; the next process must fail closed rather than guess.
                pass
            raise

    def _recover_preflight_transaction(self) -> None:
        """Recover a directory swap before strict preflight loading.

        A process can die after moving ``reporting/`` to its backup but before
        installing the staged directory.  In that window the persisted
        preflight lives only in staging, so strict finalization must converge
        the journal before attempting to read ``reporting/report_preflight``.
        """

        if self.root is None:
            return
        intent = self._load_intent()
        if intent is None:
            known = [path for path in self.root.iterdir() if path.name.startswith(_FINALIZE_STAGING_PREFIX) or path.name.startswith(_FINALIZE_BACKUP_PREFIX)]
            if known:
                raise ValueError("unbound report finalization transaction residue")
            return
        final_dir = self.root / _REPORT_DIRNAME
        staging = self._resolve_transaction_path(intent["staging_ref"], _FINALIZE_STAGING_PREFIX)
        backup = self._resolve_transaction_path(intent["backup_ref"], _FINALIZE_BACKUP_PREFIX)
        # A process can die before mkdir or during one of the three writes.
        # When the target still has the exact old tree, discard only this
        # journal's partial staging and retry; do not manufacture a report.
        if intent.get("phase") == "preparing" and self._tree_hash(final_dir) == intent.get("old_tree_sha256"):
            self._cleanup_transaction(staging, backup)
            return
        if (
            self._tree_hash(final_dir) is None
            and backup.exists()
            and self._tree_hash(backup) == intent.get("old_tree_sha256")
            and not (staging.exists() and all(path.exists() for path in self._triplet_paths(staging)))
        ):
            # The old directory was moved to its owned backup before a
            # process died while preparing the new staging. Restore the old
            # target, then discard only the owned partial transaction.
            os.replace(backup, final_dir)
            self._cleanup_transaction(staging, backup)
            return
        if final_dir.exists() and all(path.exists() for path in self._triplet_paths(final_dir)):
            expected = self._read_triplet(final_dir)
        elif staging.exists() and not staging.is_symlink() and all(path.exists() for path in self._triplet_paths(staging)):
            expected = self._read_triplet(staging)
        else:
            raise ValueError("report finalization staging is missing or incomplete")
        self._recover_transaction(final_dir, expected)

    def finalize(
        self,
        report: Mapping[str, Any] | RunReportPreflight,
        *,
        preflight: RunReportPreflight | Mapping[str, Any] | None = None,
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
        if isinstance(report, RunReportPreflight) or (
            isinstance(report, Mapping) and report.get("kind") == "run_report_preflight"
        ):
            report_preflight = report if isinstance(report, RunReportPreflight) else RunReportPreflight.from_dict(report)
            report_value = copy.deepcopy(dict(report_preflight.projected_report))
            if preflight is not None:
                supplied_preflight = preflight if isinstance(preflight, RunReportPreflight) else RunReportPreflight.from_dict(preflight)
                if supplied_preflight.to_dict() != report_preflight.to_dict():
                    raise ValueError("report preflight bindings conflict")
            preflight = report_preflight
        else:
            if not isinstance(report, Mapping):
                raise TypeError("report must be a mapping")
            report_value = copy.deepcopy(dict(report))
            if preflight is not None:
                report_preflight = preflight if isinstance(preflight, RunReportPreflight) else RunReportPreflight.from_dict(preflight)
                expected_hash = hashlib.sha256(_json_bytes({key: value for key, value in report_value.items() if key != "report_hash"})).hexdigest()
                projected_hash = hashlib.sha256(_json_bytes(report_preflight.projected_report)).hexdigest()
                if expected_hash != projected_hash:
                    raise ValueError("report does not match report preflight")
        # ``root=None`` is deliberately a pure in-memory validation mode used
        # by offline replay probes; it has no durable publication surface.
        # Every root-backed finalizer remains strict by default and must load
        # the persisted preflight below.
        if self.root is not None:
            self._recover_preflight_transaction()
            persisted = RunReportPreflight.from_dict(
                _load_json(self.root / _REPORT_DIRNAME / "report_preflight.json", "report preflight")
            )
            if preflight is not None:
                supplied_preflight = preflight if isinstance(preflight, RunReportPreflight) else RunReportPreflight.from_dict(preflight)
                if supplied_preflight.to_dict() != persisted.to_dict():
                    raise ValueError("supplied report preflight does not match persisted preflight")
            unsigned_candidate = {key: value for key, value in report_value.items() if key != "report_hash"}
            if hashlib.sha256(_json_bytes(unsigned_candidate)).hexdigest() != hashlib.sha256(_json_bytes(persisted.projected_report)).hexdigest():
                raise ValueError("report does not match persisted report preflight")
            preflight = persisted
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
        final_artifacts_exist = any(path.exists() or path.is_symlink() for path in (report_path, manifest_path, receipt_path))
        self._recover_transaction(final_dir, (report_value, manifest, receipt))
        if final_dir.exists() and all(path.exists() for path in (report_path, manifest_path, receipt_path)):
            disk_report, disk_manifest, disk_receipt = self._read_triplet(final_dir)
            if disk_report != report_value or disk_manifest != manifest or disk_receipt != receipt:
                raise ValueError("existing finalization artifacts are stale or tampered")
            return copy.deepcopy(disk_receipt)
        if final_dir.exists() and final_artifacts_exist:
            raise ValueError("final report triplet is partial")
        self._publish_transaction(final_dir, report_value, manifest, receipt)
        return copy.deepcopy(receipt)


def finalize_run_report(root: Any, report: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return RunReportFinalizer(root).finalize(report, **kwargs)


# Descriptive aliases used by host adapters; both names remain the same typed
# boundary and do not introduce coordinator dependencies.
RunReportInputBindings = RunReportEventBindings
ReportPreflight = RunReportPreflight


__all__ = [
    "ReportPreflightError",
    "ReportPreflight",
    "RunReportEventBindings",
    "RunReportFinalizer",
    "RunReportInputBindings",
    "RunReportInputGatherer",
    "RunReportPreflight",
    "RunReportProjector",
    "finalize_run_report",
    "inspect_report_artifacts",
    "project_run_report",
]
