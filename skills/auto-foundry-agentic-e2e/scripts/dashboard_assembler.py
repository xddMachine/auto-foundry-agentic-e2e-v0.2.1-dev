#!/usr/bin/env python3
"""Deterministically assemble an offline V4 dashboard from frozen run facts.

This module is the Product Agent's presentation boundary.  It consumes only
accepted answer bundles, committed integration records, frozen planning/LEM
metadata, and the renderer's local assets.  It never reads raw sources,
``work``/calculation artifacts, calls a model, or mutates an existing product.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from auto_foundry_core.workspace import AllowedRootError, RunContext
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from auto_foundry_core.workspace import AllowedRootError, RunContext


ASSEMBLER_SCHEMA = "dashboard.assembler_receipt.v1"
FIXTURE_SCHEMA = "dashboard.reviewed_fixture.v4"
CHART_MAP_SCHEMA = "dashboard.chart_map.v4"
REGISTRY_SCHEMA = "dashboard.chart_registry.v1"
PRESENTATION_PLAN_SCHEMA = "dashboard.business_presentation_plan.v1"
PRESENTATION_PLAN_V2_SCHEMA = "dashboard.business_presentation_plan.v2"
PRESENTATION_PLAN_FILENAME = "business_presentation_plan.json"

# Reviewed visual partition for the current G-0003 migration.  These IDs are
# intentionally ordered: reviewer order is part of the durable presentation
# contract and must survive writer, fixture, receipt, and renderer paths.
V2_MANAGER_VISUAL_WIDGET_IDS = [
    "REQ-01-REQ-01.metric.carrier-late-queue",
    "REQ-01-REQ-01.metric.erp-delivery-late-rate",
    "REQ-01-REQ-01.metric.erp-shipment-late",
    "REQ-01-REQ-01.metric.erp-wms-state-conflicts",
    "REQ-01-REQ-01.metric.tms-delayed-erp-on-time",
    "REQ-01-REQ-01.metric.wms-dispatch-late",
    "REQ-01-REQ-01.metric.wms-tms-state-conflicts",
    "REQ-03-REQ-03.metric.cleaned-complaint-statuses",
    "REQ-03-REQ-03.metric.finance-pending-refunds",
    "REQ-03-REQ-03.metric.policy-window-days",
    "REQ-03-REQ-03.metric.representative-approval-to-refund",
    "REQ-03-REQ-03.metric.support-urgent-high-open",
    "REQ-12-REQ-12.metric.closed-tickets",
    "REQ-12-REQ-12.metric.support-tickets-as-of",
    "REQ-04-REQ-04.metric.delay-tracker-status",
    "REQ-04-REQ-04.metric.late-latest-receipt",
    "REQ-04-REQ-04.metric.on-before-promise",
    "REQ-04-REQ-04.metric.supplier-late-queues",
    "REQ-04-REQ-04.metric.wms-receipt-reasons",
    "REQ-06-req06-metric-movement-population-movement_quantity_by_type",
    "REQ-08-req08-metric-payment-ledger-posted_amount_variance_by_currency",
    "REQ-11-REQ-11.metric.finance_pending_distinct_refs",
    "REQ-11-REQ-11.metric.finance_pending_exact_rows",
    "REQ-11-REQ-11.metric.finance_settled_distinct_refs",
    "REQ-11-REQ-11.metric.finance_settled_exact_rows",
    "REQ-11-REQ-11.metric.numeric_sale_return_qty",
    "REQ-11-REQ-11.metric.sale_return_movement_rows",
]
V2_AUDIT_VISUAL_WIDGET_IDS = [
    "REQ-02-REQ-02.metric.ecommerce-promotion-groups",
    "REQ-02-REQ-02.metric.erp-promotion-groups",
    "REQ-02-REQ-02.metric.promotion-discount-partition-total",
    "REQ-02-REQ-02.metric.refund-reference-coverage",
    "REQ-02-REQ-02.metric.refund-value-matched",
    "REQ-03-REQ-03.metric.support-missing-closed-at",
    "REQ-12-REQ-12.dashboard.V-REQ12-A002-COVERAGE-001",
    "REQ-12-REQ-12.metric.mapped-order-linked-rows",
    "REQ-12-REQ-12.metric.mapped-order-references",
    "REQ-12-REQ-12.metric.open-or-unknown-tickets",
    "REQ-12-REQ-12.metric.terminal-milestone-rows",
    "REQ-12-REQ-12.metric.unresolved-order-linked-rows",
    "REQ-12-REQ-12.metric.unresolved-order-references",
    "REQ-04-REQ-04.metric.policy-tolerance",
    "REQ-05-req05-metric-control-queues-control_exception_counts",
    "REQ-05-req05-metric-invoice-ledger-status_counts",
    "REQ-05-req05-metric-payment-terms-supplier_master_term_distribution_for_matched_invoices",
    "REQ-05-req05-metric-populations",
    "REQ-06-req06-metric-adjustment-controls",
    "REQ-06-req06-metric-erp-wms-available_qty_to_available",
    "REQ-06-req06-metric-erp-wms-damaged_qty_to_damaged",
    "REQ-06-req06-metric-erp-wms-in_transit_qty_to_inbound",
    "REQ-06-req06-metric-erp-wms-reserved_qty_to_reserved",
    "REQ-06-req06-metric-erp-wms-unrestricted_qty_to_on_hand",
    "REQ-06-req06-metric-movement-population-movement_types",
    "REQ-06-req06-metric-physical-count",
    "REQ-06-req06-metric-policy-watchlist-watchlist_action_counts",
    "REQ-07-req07-metric-inventory-policy-trigger_counts",
    "REQ-08-req08-metric-collection-emails-action_counts",
    "REQ-08-req08-metric-invoice-ledger-currency_counts",
    "REQ-08-req08-metric-invoice-ledger-outstanding_by_currency",
    "REQ-08-req08-metric-invoice-ledger-overdue_age_buckets",
    "REQ-08-req08-metric-invoice-ledger-overdue_currency_counts",
    "REQ-08-req08-metric-invoice-ledger-status_counts",
    "REQ-08-req08-metric-payment-ledger-posted_amount_by_currency",
    "REQ-11-REQ-11.metric.available_positive_damaged_zero_keys",
    "REQ-11-REQ-11.metric.available_positive_keys",
    "REQ-11-REQ-11.metric.both_available_damaged_keys",
    "REQ-11-REQ-11.metric.damaged_positive_keys",
    "REQ-11-REQ-11.metric.distinct_return_refund_references",
    "REQ-11-REQ-11.metric.finance_refund_ledger_rows",
    "REQ-11-REQ-11.metric.movement_linked_keys",
    "REQ-11-REQ-11.metric.parsed_worklist_rows",
    "REQ-11-REQ-11.metric.sale_return_rows_on_cutoff_date",
    "REQ-11-REQ-11.metric.wms_snapshot_rows",
    "REQ-11-REQ-11.metric.worklist_source_rows",
    "REQ-14-REQ-14-fact-V-REQ14-WATCHLIST",
    "REQ-17-REQ17-metric-anchor-connectivity",
]


class AssemblyError(ValueError):
    """Raised when frozen presentation inputs are incomplete or inconsistent."""


class BusinessPresentationPlanError(AssemblyError):
    """Raised when an explicit manager presentation plan is not admissible."""


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _slug(value: Any) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", _text(value, "item")).strip("-")
    return slug or "item"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _read_json(context: RunContext, reference: str | Path, *, label: str) -> Mapping[str, Any]:
    """Read one run-relative JSON object after containment validation."""

    path = context.resolve_run_path(reference)
    if not path.is_file() or path.is_symlink():
        raise AssemblyError(f"{label} is missing or symlinked: {reference}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{label} is invalid: {reference}") from exc
    if not isinstance(value, Mapping):
        raise AssemblyError(f"{label} must be a JSON object: {reference}")
    return value


def _active_generation_id(context: RunContext) -> str:
    """Read the active generation identifier without requiring a live call."""

    for reference in ("active_generation.json", "run_state.json"):
        path = context.run_root / reference
        if not path.is_file() or path.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        candidate = payload.get("generation_id")
        generation = payload.get("generation")
        if candidate is None and isinstance(generation, Mapping):
            candidate = generation.get("generation_id") or generation.get("id")
        if candidate is None and isinstance(generation, str):
            candidate = generation
        value = _text(candidate).strip()
        if value and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            return value
    return ""


def _load_legacy_chart_hints(context: RunContext) -> dict[str, Mapping[str, Any]]:
    """Load the active generation's prior dashboard chart projections, if any.

    A same-generation rebuild may be asked to republish manager presentation
    fields while preserving already-reviewed chart geometry.  The prior
    fixture is itself a frozen product input; no source/work data is read and
    a missing prior fixture simply means there are no hints.
    """

    generation_id = _active_generation_id(context)
    if not generation_id:
        return {}
    # A cumulative rebuild must inherit geometry from the immutable immediate
    # parent, never from a stale/current child fixture.  The active-generation
    # fixture may already contain the same new dashboard facts with an earlier
    # projection and would otherwise overwrite the freshly bound geometry.
    manifest_path = context.run_root / "extensions" / generation_id / "generation_manifest.json"
    try:
        generation_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() and not manifest_path.is_symlink() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        generation_manifest = {}
    parent_generation_id = _text(generation_manifest.get("parent_generation_id")).strip() if isinstance(generation_manifest, Mapping) else ""
    generation_id = parent_generation_id or generation_id
    reference = Path("products") / "generations" / generation_id / "dashboard" / "dashboard_fixture_v4.json"
    path = context.run_root / reference
    try:
        path.relative_to(context.run_root)
    except ValueError:
        return {}
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    raw_widgets = fixture.get("widgets") if isinstance(fixture, Mapping) else None
    if not isinstance(raw_widgets, list):
        return {}
    return {
        _text(widget.get("id")): widget
        for widget in raw_widgets
        if isinstance(widget, Mapping) and _text(widget.get("id")).strip()
    }


def _load_legacy_chart_map_hints(context: RunContext) -> dict[str, Mapping[str, Any]]:
    """Load immutable parent chart-map entries for cumulative rebuilds.

    The chart map is part of the reviewed presentation envelope: rebuilding a
    child generation must not add current-generation presentation metadata to
    inherited entries or silently change their bound geometry.  New records
    are still projected by the current assembler, while IDs present in the
    immediate parent's map are copied byte-for-byte into the candidate map.
    """

    generation_id = _active_generation_id(context)
    if not generation_id:
        return {}
    manifest_path = context.run_root / "extensions" / generation_id / "generation_manifest.json"
    try:
        generation_manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file() and not manifest_path.is_symlink()
            else {}
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        generation_manifest = {}
    parent_generation_id = (
        _text(generation_manifest.get("parent_generation_id")).strip()
        if isinstance(generation_manifest, Mapping)
        else ""
    )
    generation_id = parent_generation_id or generation_id
    reference = (
        Path("products")
        / "generations"
        / generation_id
        / "dashboard"
        / "dashboard_chart_map_v4.json"
    )
    path = context.run_root / reference
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        chart_map = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    raw_charts = chart_map.get("charts") if isinstance(chart_map, Mapping) else None
    if not isinstance(raw_charts, list):
        return {}
    return {
        _text(chart.get("id")): chart
        for chart in raw_charts
        if isinstance(chart, Mapping) and _text(chart.get("id")).strip()
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_canonical_bytes(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _json_hash(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _safe_record_ref(value: Any) -> str:
    ref = _text(value).strip()
    if not ref or "\x00" in ref or "\\" in ref:
        raise AssemblyError("integration reference is invalid")
    return ref


def _read_bytes(context: RunContext, reference: str | Path, *, label: str) -> tuple[Path, bytes]:
    path = context.resolve_run_path(reference)
    if not path.is_file() or path.is_symlink():
        raise AssemblyError(f"{label} is missing or symlinked: {reference}")
    try:
        return path, path.read_bytes()
    except OSError as exc:
        raise AssemblyError(f"{label} cannot be read: {reference}") from exc


def _validate_product_asset_reference(reference: str) -> str:
    """Validate a frozen product asset ref before any filesystem open.

    Product manifests may mention old source/work artifacts for audit history,
    but the presentation assembler must never probe those paths.  Reject
    absolute paths, escapes, and known raw/calculation/data-room components at
    the reference boundary so a bad ref cannot reach ``_read_bytes``.
    """

    value = _text(reference).strip()
    if not value or "\x00" in value or "\\" in value:
        raise AssemblyError("forbidden product asset reference")
    raw = Path(value)
    if raw.is_absolute() or not raw.parts:
        raise AssemblyError(f"forbidden product asset reference: {value}")
    forbidden = {"work", "calculations", "data-room", "source", "sources", "raw", "data", "dataset", "datasets"}
    for part in raw.parts:
        normalized = part.strip().lower().replace("_", "-")
        if normalized in {".", ".."} or normalized in forbidden or normalized.startswith(("source-", "raw-", "calculation-")):
            raise AssemblyError(f"forbidden product asset reference: {value}")
    return value


def _load_public_accepted_bundle(context: RunContext, item_id: str) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Load an accepted bundle through core's immutable public validator.

    Constructing an ``ItemWorkspace`` directly avoids ``ItemWorkspace.load``
    telemetry/state reconciliation side effects during a product-only build.
    ``AcceptedAnalysisBundle.load`` still owns the exact accepted manifest,
    envelope, content, and terminal-intent/hash checks.
    """

    try:
        from auto_foundry_core.durable import ItemWorkspace
        from auto_foundry_core.integration import AcceptedAnalysisBundle
    except ModuleNotFoundError as exc:  # pragma: no cover - package install guard
        raise AssemblyError("auto_foundry_core public accepted-bundle types are unavailable") from exc
    state_ref = f"requirements/{item_id}/item_state.json"
    state = _read_json(context, state_ref, label=f"{item_id} item state")
    if state.get("item_id") != item_id or state.get("mode") != "requirement":
        raise AssemblyError(f"{item_id} item state identity/mode mismatch")
    if state.get("lifecycle_state") not in {"accepted", "integrated", "complete"}:
        raise AssemblyError(f"{item_id} is not terminally accepted")
    if state.get("integration_state") != "integrated":
        raise AssemblyError(f"{item_id} is not integrated")
    workspace = ItemWorkspace(
        context,
        item_id,
        mode="requirement",
        original_text=_text(state.get("original_text")),
        telemetry=None,
        state=state,
    )
    try:
        bundle = AcceptedAnalysisBundle.load(workspace)
    except Exception as exc:
        raise AssemblyError(f"{item_id} accepted bundle validation failed") from exc
    try:
        content = json.loads(bundle.answer_content_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{item_id} accepted answer content is not JSON") from exc
    if not isinstance(content, Mapping):
        raise AssemblyError(f"{item_id} accepted answer content must be an object")
    manifest = dict(bundle.manifest)
    envelope = dict(bundle.acceptance_envelope)
    terminal = state.get("terminal_intent")
    if not isinstance(terminal, Mapping) or terminal.get("manifest_hash") != bundle.manifest_hash:
        raise AssemblyError(f"{item_id} terminal intent does not bind accepted manifest")
    if terminal.get("outcome") != bundle.outcome:
        raise AssemblyError(f"{item_id} terminal intent outcome mismatch")
    return content, manifest, {"state": state, "envelope": envelope, "bundle": bundle}


def _load_committed_records(
    context: RunContext,
    item_id: str,
    accepted_manifest: Mapping[str, Any],
    bundle: Any,
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    manifest_ref = f"requirements/{item_id}/integration/committed/manifest.json"
    _manifest_path, manifest_bytes = _read_bytes(context, manifest_ref, label=f"{item_id} committed integration manifest")
    try:
        integration_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"{item_id} committed integration manifest is invalid") from exc
    if not isinstance(integration_manifest, Mapping):
        raise AssemblyError(f"{item_id} committed integration manifest is invalid")
    expected = {
        "schema_version", "session_id", "item_id", "owner_id", "invocation_id", "status",
        "accepted_content_hash", "accepted_manifest_hash", "records_path", "records_hash",
        "records_count", "counts", "created_at", "committed_at", "manifest_hash",
    }
    if set(integration_manifest) != expected or integration_manifest.get("schema_version") != "1" or integration_manifest.get("status") != "committed":
        raise AssemblyError(f"{item_id} committed integration manifest fields are invalid")
    if integration_manifest.get("item_id") != item_id:
        raise AssemblyError(f"{item_id} committed integration item identity mismatch")
    if integration_manifest.get("accepted_content_hash") != bundle.content_hash or integration_manifest.get("accepted_manifest_hash") != bundle.manifest_hash:
        raise AssemblyError(f"{item_id} committed integration accepted hash binding mismatch")
    if integration_manifest.get("records_path") != "records.jsonl" or not _is_sha256(integration_manifest.get("records_hash")):
        raise AssemblyError(f"{item_id} committed integration records binding is invalid")
    if not isinstance(integration_manifest.get("records_count"), int) or isinstance(integration_manifest.get("records_count"), bool) or integration_manifest["records_count"] < 0:
        raise AssemblyError(f"{item_id} committed integration records_count is invalid")
    if not isinstance(integration_manifest.get("counts"), Mapping) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in integration_manifest["counts"].values()
    ):
        raise AssemblyError(f"{item_id} committed integration counts are invalid")
    unsigned = {key: value for key, value in integration_manifest.items() if key != "manifest_hash"}
    if integration_manifest.get("manifest_hash") != _json_hash(unsigned):
        raise AssemblyError(f"{item_id} committed integration manifest hash mismatch")
    records_ref = f"requirements/{item_id}/integration/committed/records.jsonl"
    _records_path, records_bytes = _read_bytes(context, records_ref, label=f"{item_id} committed integration records")
    if _sha256_bytes(records_bytes) != integration_manifest["records_hash"]:
        raise AssemblyError(f"{item_id} committed integration records hash mismatch")
    try:
        from auto_foundry_core.integration import IntegrationRecord
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise AssemblyError("auto_foundry_core IntegrationRecord is unavailable") from exc
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(records_bytes.decode("utf-8").splitlines(), 1):
        try:
            raw = json.loads(line)
            record = IntegrationRecord.from_dict(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise AssemblyError(f"{item_id} committed integration record {line_number} is invalid") from exc
        if record.item_id != item_id or record.accepted_content_hash != bundle.content_hash:
            raise AssemblyError(f"{item_id} committed integration record {line_number} hash/identity mismatch")
        records.append(record.to_dict())
    if len(records) != integration_manifest["records_count"]:
        raise AssemblyError(f"{item_id} committed integration records_count does not match bytes")
    return dict(integration_manifest), records


def _discover_item_ids(context: RunContext, explicit: Sequence[str] | None, plan: Mapping[str, Any] | None) -> list[str]:
    if explicit:
        item_ids = [_text(value).strip() for value in explicit]
    elif plan and isinstance(plan.get("groups"), list):
        item_ids = [
            _text(item_id).strip()
            for group in plan["groups"]
            if isinstance(group, Mapping)
            for item_id in _as_list(group.get("requirement_ids"))
        ]
    else:
        requirements = context.resolve_run_path("requirements")
        item_ids = sorted(path.name for path in requirements.iterdir() if path.is_dir()) if requirements.is_dir() else []
    if not item_ids:
        raise AssemblyError("no requirement IDs were supplied or discoverable")
    seen: set[str] = set()
    normalized: list[str] = []
    for item_id in item_ids:
        if not item_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", item_id) or item_id in seen:
            raise AssemblyError(f"requirement IDs must be unique safe components: {item_id!r}")
        seen.add(item_id)
        normalized.append(item_id)
    return normalized


def _load_plan(context: RunContext, plan_ref: str | Path | None) -> Mapping[str, Any] | None:
    reference = plan_ref or "requirement_supervisor_plan.json"
    path = context.resolve_run_path(reference)
    if not path.exists():
        return None
    return _read_json(context, reference, label="requirement supervisor plan")


def _presentation_generation_metadata(
    context: RunContext,
    generation_id: str | None = None,
    *,
    _lock_held: bool = False,
) -> tuple[str, Any | None]:
    """Resolve the generation identity without changing lifecycle state.

    The dashboard skill deliberately does not extend ``RunLifecycle``.  When
    an active generation exists, its public metadata is the authority.  A
    caller may name a generation for an offline/root product (for example a
    copied G-0001 fixture); otherwise the active pointer is used.  Missing
    metadata is never synthesized into a manager admission.
    """

    metadata = None
    try:
        from auto_foundry_core.lifecycle import RunLifecycle

        if _lock_held:
            pointer = RunLifecycle._read_generation_pointer_unlocked(context)  # noqa: SLF001 - lock is held by caller
            metadata = (
                RunLifecycle._load_generation_unlocked(context, pointer)  # noqa: SLF001 - lock is held by caller
                if pointer is not None
                else None
            )
        else:
            metadata = RunLifecycle.active_generation_metadata(context)
    except Exception:
        metadata = None
    candidate = _text(generation_id).strip() or _text(getattr(metadata, "generation_id", "")).strip() or _active_generation_id(context)
    if not candidate:
        candidate = "G-0001"
    if not re.fullmatch(r"G-[0-9]{4}", candidate):
        raise BusinessPresentationPlanError("generation_id must be a canonical G-0000 identifier")
    if metadata is not None and _text(getattr(metadata, "generation_id", "")) != candidate:
        raise BusinessPresentationPlanError("presentation plan generation does not match the active generation")
    return candidate, metadata


def _presentation_supervisor_binding(
    context: RunContext,
    generation_id: str,
    metadata: Any | None,
) -> tuple[str, Mapping[str, Any], str]:
    """Return the authoritative supervisor-plan ref, value, and hash."""

    if metadata is not None:
        path = Path(str(metadata.plan_path))
        try:
            reference = path.relative_to(context.run_root).as_posix()
        except ValueError as exc:
            raise BusinessPresentationPlanError("active supervisor plan escaped the run root") from exc
    elif generation_id == "G-0001":
        reference = "requirement_supervisor_plan.json"
    else:
        reference = f"extensions/{generation_id}/requirement_supervisor_plan.json"
    value = _read_json(context, reference, label="presentation supervisor plan")
    path = context.resolve_run_path(reference)
    return reference, value, _sha256_bytes(path.read_bytes())


def _presentation_parent_binding(context: RunContext, generation_id: str, metadata: Any | None) -> dict[str, Any] | None:
    """Bind the immediate product parent without reading unrelated generations."""

    parent_id = _text(getattr(metadata, "parent_generation_id", "")).strip() if metadata is not None else ""
    if not parent_id:
        if generation_id == "G-0001":
            return None
        ordinal = int(generation_id[2:])
        parent_id = f"G-{ordinal - 1:04d}"
    manifest_ref = f"products/generations/{parent_id}/product_manifest.json"
    manifest_path = context.resolve_run_path(manifest_ref)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BusinessPresentationPlanError("immediate parent product manifest is missing or symlinked")
    parent = _read_json(context, manifest_ref, label="presentation parent product manifest")
    dashboard = parent.get("dashboard")
    receipt_ref = dashboard.get("receipt_ref") if isinstance(dashboard, Mapping) else None
    if not isinstance(receipt_ref, str) or not receipt_ref:
        raise BusinessPresentationPlanError("immediate parent product manifest lacks a dashboard receipt")
    receipt_path = context.resolve_run_path(receipt_ref)
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise BusinessPresentationPlanError("immediate parent dashboard receipt is missing or symlinked")
    return {
        "generation_id": parent_id,
        "product_manifest_ref": manifest_ref,
        "product_manifest_sha256": _sha256_bytes(manifest_path.read_bytes()),
        "receipt_ref": receipt_ref,
        "receipt_sha256": _sha256_bytes(receipt_path.read_bytes()),
    }


def _presentation_input_bindings(context: RunContext, item_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Load the exact accepted/committed bindings used by a plan."""

    bindings: list[dict[str, Any]] = []
    for item_id in item_ids:
        content, accepted_manifest, accepted_meta = _load_public_accepted_bundle(context, item_id)
        integration_manifest, records = _load_committed_records(
            context,
            item_id,
            accepted_manifest,
            accepted_meta["bundle"],
        )
        bindings.append({
            "item_id": item_id,
            "accepted_content_hash": accepted_meta["bundle"].content_hash,
            "accepted_manifest_hash": accepted_meta["bundle"].manifest_hash,
            "integration_manifest_hash": integration_manifest["manifest_hash"],
            "record_count": len(records),
        })
    return bindings


def _presentation_widget_binding(widget: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable widget identity used by inventory and plan validation.

    Manager text is deliberately *not* copied into a plan from this helper.
    Every selected display value must be bound to a JSON pointer into the
    authoritative committed record (see ``display_projection`` below).
    """

    record_ids = sorted({
        _text(value).strip()
        for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
        if _text(value).strip()
    })
    record_refs = sorted({
        _text(value).strip()
        for value in (_as_list(widget.get("integration_record_refs")) or [widget.get("integration_record_ref")])
        if _text(value).strip()
    })
    return {
        "widget_id": _text(widget.get("id")),
        "requirement_id": _text(widget.get("requirement_id")),
        "presentation_role": _text(widget.get("presentation_role") or "decision_view"),
        "integration_record_ids": record_ids,
        "integration_record_refs": record_refs,
    }


_PRESENTATION_PROJECTION_FIELDS = frozenset({
    "title", "label", "body", "value", "display_value", "denominator", "unit",
    "rows", "period", "as_of", "status", "note", "subtitle",
})


def _presentation_pointer_escape(value: Any) -> str:
    return _text(value).replace("~", "~0").replace("/", "~1")


def _presentation_pointer_value(root: Mapping[str, Any], pointer: Any) -> Any:
    """Resolve one bounded JSON pointer into an authoritative record root."""

    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer in {"/", "/payload", "/accepted"}:
        raise BusinessPresentationPlanError("display projection pointer must be a non-root JSON pointer")
    raw_parts = pointer[1:].split("/")
    parts: list[str] = []
    for raw in raw_parts:
        if re.search(r"~(?![01])", raw):
            raise BusinessPresentationPlanError("display projection pointer has invalid escaping")
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    if parts[0] not in {"payload", "accepted"}:
        raise BusinessPresentationPlanError("display projection pointers must target payload or accepted fields")
    current: Any = root
    for part in parts:
        if isinstance(current, Mapping):
            if part not in current:
                raise BusinessPresentationPlanError(f"display projection pointer is missing: {pointer}")
            current = current[part]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            if index >= len(current):
                raise BusinessPresentationPlanError(f"display projection pointer is out of range: {pointer}")
            current = current[index]
        else:
            raise BusinessPresentationPlanError(f"display projection pointer cannot descend: {pointer}")
    return current


def _presentation_projection_value_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's bool/int equality ambiguity."""

    return _canonical_bytes(actual) == _canonical_bytes(expected)


def _visual_pointer_value(root: Mapping[str, Any], pointer: Any) -> Any:
    """Resolve a JSON pointer into one immutable visual source object.

    V2 visual entries deliberately bind title fields to the widget snapshot
    and chart geometry/value fields to the chart-map entry.  The resolver is
    kept separate from the accepted-record pointer resolver so a visual plan
    can never accidentally bind to an unreviewed source/work field.
    """

    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        raise BusinessPresentationPlanError("visual projection pointer must be a non-root JSON pointer")
    parts: list[str] = []
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise BusinessPresentationPlanError("visual projection pointer has invalid escaping")
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    current: Any = root
    for part in parts:
        if isinstance(current, Mapping):
            if part not in current:
                raise BusinessPresentationPlanError(f"visual projection pointer is missing: {pointer}")
            current = current[part]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            if index >= len(current):
                raise BusinessPresentationPlanError(f"visual projection pointer is out of range: {pointer}")
            current = current[index]
        else:
            raise BusinessPresentationPlanError(f"visual projection pointer cannot descend: {pointer}")
    return current


def _visual_snapshot_hash(widget: Mapping[str, Any]) -> str:
    # The widget snapshot binds the reviewed visual payload and immutable
    # identity, while excluding the presentation-plan envelope itself.  The
    # assembler attaches ``manager_admission``/``manager_presentation`` only
    # after a plan is selected (the latter necessarily contains this hash), so
    # hashing those fields would create a self-referential, non-reproducible
    # plan.  The exact full widget remains available in ``audit_widgets``.
    snapshot = {
        key: copy.deepcopy(value)
        for key, value in widget.items()
        if key not in {
            "manager_admission", "manager_presentation", "presentation_audience",
            "presentation_tier", "manager_anchor", "presentation_plan_ref",
            "presentation_plan_sha256", "overview",
        }
    }
    return _sha256_bytes(_canonical_bytes(snapshot))


def _visual_chart_hash(chart: Mapping[str, Any]) -> str:
    # Chart role/tier are plan-derived metadata, not chart geometry or values.
    # Keep the hash stable when a V1 fixture is rebound to the reviewed V2
    # visual partition while retaining exact type/fields/provenance binding.
    snapshot = copy.deepcopy(dict(chart))
    fields = snapshot.get("fields_or_values_used")
    if isinstance(fields, Mapping):
        snapshot["fields_or_values_used"] = {
            key: copy.deepcopy(value)
            for key, value in fields.items()
            if key not in {"presentation_role", "presentation_tier"}
        }
    return _sha256_bytes(_canonical_bytes(snapshot))


def _visual_entry_shape(entry: Mapping[str, Any]) -> None:
    required = {
        "widget_id", "requirement_id", "record_ids", "presentation_audience",
        "visual_type", "chart_family", "widget_snapshot_sha256",
        "chart_entry_sha256", "allowed_visual_fields", "title_projection",
        "visual_projection",
    }
    if set(entry) != required:
        raise BusinessPresentationPlanError("v2 visual entry fields are invalid")
    if not isinstance(entry.get("widget_id"), str) or not entry["widget_id"].strip():
        raise BusinessPresentationPlanError("v2 visual entry widget identity is invalid")
    if not isinstance(entry.get("requirement_id"), str) or not entry["requirement_id"].strip():
        raise BusinessPresentationPlanError("v2 visual entry requirement identity is invalid")
    if entry.get("presentation_audience") not in {"business_manager", "technical_audit_gallery"}:
        raise BusinessPresentationPlanError("v2 visual entry audience is invalid")
    if not isinstance(entry.get("record_ids"), list) or len(set(entry["record_ids"])) != len(entry["record_ids"]):
        raise BusinessPresentationPlanError("v2 visual entry record bindings are invalid")
    if any(not isinstance(value, str) or not value.strip() for value in entry["record_ids"]):
        raise BusinessPresentationPlanError("v2 visual entry record bindings are invalid")
    if not isinstance(entry.get("visual_type"), str) or not entry["visual_type"].strip():
        raise BusinessPresentationPlanError("v2 visual entry type is invalid")
    if not isinstance(entry.get("chart_family"), str) or not entry["chart_family"].strip():
        raise BusinessPresentationPlanError("v2 visual entry family is invalid")
    for field in ("widget_snapshot_sha256", "chart_entry_sha256"):
        if not _is_sha256(entry.get(field)):
            raise BusinessPresentationPlanError(f"v2 visual entry {field} is invalid")
    allowed = entry.get("allowed_visual_fields")
    projection = entry.get("visual_projection")
    if not isinstance(allowed, list) or len(set(allowed)) != len(allowed) or any(not isinstance(value, str) or not value for value in allowed):
        raise BusinessPresentationPlanError("v2 visual entry allowed fields are invalid")
    if not isinstance(projection, Mapping) or set(projection) != set(allowed):
        raise BusinessPresentationPlanError("v2 visual entry projection fields do not match allowed fields")
    title = entry.get("title_projection")
    if not isinstance(title, Mapping) or set(title) != {"pointer", "value"} or not isinstance(title.get("pointer"), str):
        raise BusinessPresentationPlanError("v2 visual entry title projection is invalid")
    for field, binding in projection.items():
        if not isinstance(binding, Mapping) or set(binding) != {"pointer", "value"} or not isinstance(binding.get("pointer"), str):
            raise BusinessPresentationPlanError(f"v2 visual entry projection is invalid: {field}")


def _v2_manager_entry_from_visual(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one visual entry into the renderer's manager envelope binding."""

    return {
        "widget_id": entry["widget_id"],
        "requirement_id": entry["requirement_id"],
        "record_ids": list(entry["record_ids"]),
        "presentation_role": "decision_view",
        "visual_type": entry["visual_type"],
        "chart_family": entry["chart_family"],
        "widget_snapshot_sha256": entry["widget_snapshot_sha256"],
        "chart_entry_sha256": entry["chart_entry_sha256"],
        "allowed_visual_fields": list(entry["allowed_visual_fields"]),
        "title_projection": copy.deepcopy(entry["title_projection"]),
        "visual_projection": copy.deepcopy(entry["visual_projection"]),
    }


_V1_MANAGER_ENTRY_FIELDS = (
    "widget_id",
    "record_id",
    "requirement_id",
    "presentation_role",
    "file_sha256",
    "canonical_payload_sha256",
    "display_projection",
)


def _v2_predecessor_manager_contract(plan: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Return the exact V1 manager envelope retained by a V2 successor.

    V2 is a one-time successor to a plan-bearing V1 product.  The successor
    therefore carries an explicit predecessor binding rather than asking a
    delta caller to infer old IDs from the visual partition.  The copied V1
    entries are the immutable record/file/projection contract; V2 may augment
    the overlapping pending-refunds entry with visual fields, but it may not
    rewrite any V1 field.
    """

    source = plan.get("source_bindings")
    if not isinstance(source, Mapping):
        raise BusinessPresentationPlanError("v2 predecessor manager binding is missing")
    ids = source.get("previous_manager_widget_ids")
    entries = source.get("previous_manager_entries")
    if (
        not isinstance(ids, list)
        or not ids
        or len(set(ids)) != len(ids)
        or any(not isinstance(value, str) or not value.strip() for value in ids)
        or not isinstance(entries, list)
        or len(entries) != len(ids)
    ):
        raise BusinessPresentationPlanError("v2 predecessor manager binding is invalid")
    if [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)] != ids:
        raise BusinessPresentationPlanError("v2 predecessor manager entries do not match IDs")
    predecessor_entries: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise BusinessPresentationPlanError("v2 predecessor manager entry is invalid")
        entry = {key: copy.deepcopy(raw_entry.get(key)) for key in _V1_MANAGER_ENTRY_FIELDS}
        if set(raw_entry) != set(_V1_MANAGER_ENTRY_FIELDS):
            raise BusinessPresentationPlanError("v2 predecessor manager entry fields are invalid")
        _presentation_entry_shape(entry)
        predecessor_entries.append(entry)

    manager_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in plan.get("manager_entries", [])
        if isinstance(entry, Mapping)
    }
    for widget_id, predecessor in zip(ids, predecessor_entries):
        candidate = manager_by_id.get(widget_id)
        if not isinstance(candidate, Mapping):
            raise BusinessPresentationPlanError(f"v2 manager entries omit predecessor widget: {widget_id}")
        candidate_base = {key: copy.deepcopy(candidate.get(key)) for key in _V1_MANAGER_ENTRY_FIELDS}
        if set(_V1_MANAGER_ENTRY_FIELDS) - set(candidate):
            raise BusinessPresentationPlanError(f"v2 predecessor projection is missing: {widget_id}")
        _presentation_entry_shape(candidate_base)
        if candidate_base != predecessor:
            raise BusinessPresentationPlanError(f"v2 predecessor projection drifted: {widget_id}")
    return list(ids), predecessor_entries


def _v2_predecessor_plan_manager_contract(
    plan: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate the complete manager envelope of a V2 predecessor.

    ``previous_manager_*`` is deliberately retained as the small V1
    migration contract (the six reviewed conclusion entries).  Once a V2
    plan is itself succeeded, however, the predecessor manager surface also
    contains its visual projection envelopes.  Those entries have a
    different, richer shape and cannot be validated through the V1 helper.
    Bind that full prior manager order and byte-level entry shape separately
    so a generation successor cannot silently drop, reorder, or rewrite an
    inherited manager entry.
    """

    source = plan.get("source_bindings")
    if not isinstance(source, Mapping):
        return [], []
    ids = source.get("previous_plan_manager_widget_ids")
    entries = source.get("previous_plan_manager_entries")
    if ids is None and entries is None:
        return [], []
    if (
        not isinstance(ids, list)
        or not ids
        or len(set(ids)) != len(ids)
        or any(not isinstance(value, str) or not value.strip() for value in ids)
        or not isinstance(entries, list)
        or len(entries) != len(ids)
        or [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)] != ids
    ):
        raise BusinessPresentationPlanError("v2 predecessor plan-manager binding is invalid")
    predecessor_entries: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping) or not raw_entry.get("widget_id"):
            raise BusinessPresentationPlanError("v2 predecessor plan-manager entry is invalid")
        # Keep the exact key/value envelope.  V2 visual entries intentionally
        # carry projection fields in addition to the V1 record projection;
        # validating the mapping as a whole prevents a candidate from
        # replacing one with a semantically similar but structurally weaker
        # entry.
        predecessor_entries.append(copy.deepcopy(dict(raw_entry)))
    manager_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in plan.get("manager_entries", [])
        if isinstance(entry, Mapping)
    }
    for widget_id, predecessor in zip(ids, predecessor_entries):
        candidate = manager_by_id.get(widget_id)
        if not isinstance(candidate, Mapping) or dict(candidate) != predecessor:
            raise BusinessPresentationPlanError(f"v2 predecessor plan-manager drifted: {widget_id}")
    return list(ids), predecessor_entries


def _v2_predecessor_visual_contract(
    plan: Mapping[str, Any],
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Validate and return an optional exact predecessor visual contract.

    G3's original V2 plan predates these fields, so all three are optional
    for that already-frozen plan.  A successor plan writes them explicitly;
    when present they bind the predecessor's ordered manager/audit partition
    and every pointer/hash entry, preventing a same-generation rebuild from
    silently changing an inherited audience or visual payload.
    """

    source = plan.get("source_bindings")
    if not isinstance(source, Mapping):
        return [], [], []
    keys = (
        "previous_manager_visual_widget_ids",
        "previous_audit_visual_widget_ids",
        "previous_visual_entries",
    )
    present = [key in source for key in keys]
    if not any(present):
        return [], [], []
    if not all(present):
        raise BusinessPresentationPlanError("v2 predecessor visual binding is incomplete")
    manager = source.get(keys[0])
    audit = source.get(keys[1])
    entries = source.get(keys[2])
    if (
        not isinstance(manager, list)
        or not isinstance(audit, list)
        or not isinstance(entries, list)
        or len(set(manager)) != len(manager)
        or len(set(audit)) != len(audit)
        or set(manager).intersection(audit)
        or any(not isinstance(value, str) or not value.strip() for value in manager + audit)
        or [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)] != manager + audit
        or len(set(manager + audit)) != len(entries)
    ):
        raise BusinessPresentationPlanError("v2 predecessor visual binding is invalid")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise BusinessPresentationPlanError("v2 predecessor visual entry is invalid")
        _visual_entry_shape(entry)
    current_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in plan.get("visual_entries", [])
        if isinstance(entry, Mapping)
    }
    for predecessor in entries:
        widget_id = _text(predecessor.get("widget_id"))
        current = current_by_id.get(widget_id)
        if not isinstance(current, Mapping):
            raise BusinessPresentationPlanError(f"v2 successor omits predecessor visual: {widget_id}")
        # Audience and exact visual bindings are immutable across a
        # generation successor.  The successor may add new visuals only.
        for field in (
            "requirement_id", "record_ids", "presentation_audience", "visual_type",
            "chart_family", "widget_snapshot_sha256", "chart_entry_sha256",
            "allowed_visual_fields", "title_projection", "visual_projection",
        ):
            if current.get(field) != predecessor.get(field):
                raise BusinessPresentationPlanError(f"v2 predecessor visual drifted: {widget_id}:{field}")
    return list(manager), list(audit), [copy.deepcopy(dict(entry)) for entry in entries]


def _presentation_authoritative_root(
    record: Mapping[str, Any],
    accepted_content: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root: dict[str, Any] = {"payload": copy.deepcopy(_record_payload(record))}
    if accepted_content is not None:
        root["accepted"] = copy.deepcopy(dict(accepted_content))
    return root


def _presentation_record_id_for_widget(widget: Mapping[str, Any]) -> str:
    record_ids = _presentation_widget_binding(widget)["integration_record_ids"]
    if len(record_ids) != 1:
        raise BusinessPresentationPlanError(
            f"manager presentation widget must bind exactly one committed record: {_text(widget.get('id'))}"
        )
    return record_ids[0]


def _presentation_entry_shape(entry: Mapping[str, Any]) -> None:
    required = {
        "widget_id", "record_id", "requirement_id", "presentation_role",
        "file_sha256", "canonical_payload_sha256", "display_projection",
    }
    if set(entry) != required:
        raise BusinessPresentationPlanError("presentation plan manager entry fields are invalid")
    for key in ("widget_id", "record_id", "requirement_id", "presentation_role"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise BusinessPresentationPlanError("presentation plan manager entry identity is invalid")
    if not _is_sha256(entry.get("file_sha256")) or not _is_sha256(entry.get("canonical_payload_sha256")):
        raise BusinessPresentationPlanError("presentation plan manager entry hashes are invalid")
    projection = entry.get("display_projection")
    if not isinstance(projection, Mapping) or "title" not in projection:
        raise BusinessPresentationPlanError("presentation plan manager entry requires a title projection")
    if not ("body" in projection or "value" in projection or "rows" in projection or "display_value" in projection):
        raise BusinessPresentationPlanError("presentation plan manager entry requires a display projection")
    if any(not isinstance(key, str) or key not in _PRESENTATION_PROJECTION_FIELDS for key in projection):
        raise BusinessPresentationPlanError("presentation plan display projection field is unsupported")
    for field, binding in projection.items():
        if not isinstance(binding, Mapping) or set(binding) != {"pointer", "value"}:
            raise BusinessPresentationPlanError(f"presentation plan projection {field} must bind pointer and value")
        if not isinstance(binding.get("pointer"), str):
            raise BusinessPresentationPlanError(f"presentation plan projection {field} pointer is invalid")


def _candidate_display_projection(
    root: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, dict[str, Any]] | None:
    """Suggest a pointer-only manager projection for reviewer selection.

    This helper never invents text or derives metrics.  It only exposes
    existing scalar/list fields from the committed payload.  Reviewers may
    narrow the projection, but cannot supply a value that is not present at a
    validated JSON pointer.
    """

    payload = root.get("payload") if isinstance(root, Mapping) else None
    if not isinstance(payload, Mapping):
        return None
    kind = _text(record.get("kind")).lower()
    title_key = next((key for key in ("label", "title", "name", "claim") if key in payload and isinstance(payload[key], str) and payload[key].strip()), None)
    if title_key is None:
        return None
    projection: dict[str, dict[str, Any]] = {
        "title": {"pointer": f"/payload/{_presentation_pointer_escape(title_key)}", "value": copy.deepcopy(payload[title_key])},
    }
    if kind in {"claim", "limitation"} and "claim" in payload and isinstance(payload.get("claim"), str):
        projection["body"] = {"pointer": "/payload/claim", "value": copy.deepcopy(payload["claim"])}
    elif "value" in payload and not isinstance(payload.get("value"), (Mapping, list)):
        projection["value"] = {"pointer": "/payload/value", "value": copy.deepcopy(payload.get("value"))}
    elif "display_value" in payload and not isinstance(payload.get("display_value"), (Mapping, list)):
        projection["display_value"] = {"pointer": "/payload/display_value", "value": copy.deepcopy(payload.get("display_value"))}
    else:
        return None
    for output_field, payload_keys in (
        ("unit", ("unit", "units", "distinct_unit")),
        ("denominator", ("denominator", "population")),
        ("period", ("period", "as_of")),
        ("status", ("status",)),
    ):
        key = next((candidate for candidate in payload_keys if candidate in payload and payload[candidate] not in (None, "")), None)
        if key is not None and not isinstance(payload[key], (Mapping, list)):
            projection[output_field] = {"pointer": f"/payload/{_presentation_pointer_escape(key)}", "value": copy.deepcopy(payload[key])}
    return projection


def _presentation_plan_ref(context: RunContext, generation_id: str, reference: str | Path | None = None) -> str:
    value = Path(reference or Path("extensions") / generation_id / PRESENTATION_PLAN_FILENAME)
    if value.is_absolute() or ".." in value.parts:
        raise BusinessPresentationPlanError("presentation plan reference must remain run-relative")
    path = context.resolve_run_path(value)
    try:
        path.relative_to(context.run_root)
    except ValueError as exc:
        raise BusinessPresentationPlanError("presentation plan reference escaped the run root") from exc
    return value.as_posix()


def _validate_presentation_plan_shape(plan: Mapping[str, Any]) -> None:
    expected = {
        "schema_version", "run_id", "generation_id", "supervisor_plan_ref", "supervisor_plan_sha256",
        "item_order", "input_items", "parent", "reviewer_ref", "manager_widget_ids", "manager_entries",
    }
    if set(plan) != expected or plan.get("schema_version") != PRESENTATION_PLAN_SCHEMA:
        raise BusinessPresentationPlanError("presentation plan fields are invalid")
    if not isinstance(plan.get("run_id"), str) or not plan["run_id"]:
        raise BusinessPresentationPlanError("presentation plan run_id is invalid")
    if not isinstance(plan.get("generation_id"), str) or not re.fullmatch(r"G-[0-9]{4}", plan["generation_id"]):
        raise BusinessPresentationPlanError("presentation plan generation_id is invalid")
    if not _is_sha256(plan.get("supervisor_plan_sha256")):
        raise BusinessPresentationPlanError("presentation plan supervisor hash is invalid")
    item_order = plan.get("item_order")
    if not isinstance(item_order, list) or len(set(item_order)) != len(item_order) or any(not isinstance(v, str) or not v for v in item_order):
        raise BusinessPresentationPlanError("presentation plan item_order is invalid")
    input_items = plan.get("input_items")
    if not isinstance(input_items, list) or [v.get("item_id") for v in input_items if isinstance(v, Mapping)] != item_order:
        raise BusinessPresentationPlanError("presentation plan input bindings do not match item_order")
    for item in input_items:
        if not isinstance(item, Mapping) or set(item) != {"item_id", "accepted_content_hash", "accepted_manifest_hash", "integration_manifest_hash", "record_count"}:
            raise BusinessPresentationPlanError("presentation plan input binding is invalid")
        if not all(_is_sha256(item.get(key)) for key in ("accepted_content_hash", "accepted_manifest_hash", "integration_manifest_hash")):
            raise BusinessPresentationPlanError("presentation plan input hash is invalid")
        if isinstance(item.get("record_count"), bool) or not isinstance(item.get("record_count"), int) or item["record_count"] < 0:
            raise BusinessPresentationPlanError("presentation plan input record_count is invalid")
    parent = plan.get("parent")
    if parent is not None:
        if not isinstance(parent, Mapping) or set(parent) != {"generation_id", "product_manifest_ref", "product_manifest_sha256", "receipt_ref", "receipt_sha256"}:
            raise BusinessPresentationPlanError("presentation plan parent binding is invalid")
        if not _is_sha256(parent.get("product_manifest_sha256")) or not _is_sha256(parent.get("receipt_sha256")):
            raise BusinessPresentationPlanError("presentation plan parent hash is invalid")
    reviewer_ref = plan.get("reviewer_ref")
    if not isinstance(reviewer_ref, str) or not reviewer_ref.strip():
        raise BusinessPresentationPlanError("presentation plan reviewer_ref is required")
    widget_ids = plan.get("manager_widget_ids")
    if not isinstance(widget_ids, list) or len(set(widget_ids)) != len(widget_ids) or any(not isinstance(v, str) or not v for v in widget_ids):
        raise BusinessPresentationPlanError("presentation plan manager_widget_ids are invalid")
    entries = plan.get("manager_entries")
    if not isinstance(entries, list) or [v.get("widget_id") for v in entries if isinstance(v, Mapping)] != widget_ids:
        raise BusinessPresentationPlanError("presentation plan manager entries do not match manager_widget_ids")
    if len(set(widget_ids)) != len(entries):
        raise BusinessPresentationPlanError("presentation plan manager entries must be unique")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise BusinessPresentationPlanError("presentation plan manager entry is invalid")
        _presentation_entry_shape(entry)


def _validate_presentation_plan_v2_shape(plan: Mapping[str, Any]) -> None:
    expected = {
        "schema_version", "run_id", "generation_id", "supervisor_plan_ref", "supervisor_plan_sha256",
        "item_order", "input_items", "parent", "reviewer_ref", "manager_widget_ids", "manager_entries",
        "manager_visual_widget_ids", "audit_visual_widget_ids", "visual_entries", "source_bindings",
    }
    if set(plan) != expected or plan.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
        raise BusinessPresentationPlanError("v2 presentation plan fields are invalid")
    # Validate the immutable lifecycle/input envelope directly.  V2 manager
    # entries may carry visual projection fields, so they must not be passed
    # through the stricter V1 manager-entry shape.
    if not isinstance(plan.get("run_id"), str) or not plan["run_id"]:
        raise BusinessPresentationPlanError("v2 presentation plan run_id is invalid")
    if not isinstance(plan.get("generation_id"), str) or not re.fullmatch(r"G-[0-9]{4}", plan["generation_id"]):
        raise BusinessPresentationPlanError("v2 presentation plan generation_id is invalid")
    if not _is_sha256(plan.get("supervisor_plan_sha256")):
        raise BusinessPresentationPlanError("v2 supervisor hash is invalid")
    item_order = plan.get("item_order")
    input_items = plan.get("input_items")
    if not isinstance(item_order, list) or len(set(item_order)) != len(item_order) or any(not isinstance(value, str) or not value for value in item_order):
        raise BusinessPresentationPlanError("v2 item_order is invalid")
    if not isinstance(input_items, list) or [value.get("item_id") for value in input_items if isinstance(value, Mapping)] != item_order:
        raise BusinessPresentationPlanError("v2 input bindings do not match item_order")
    for item in input_items:
        if not isinstance(item, Mapping) or set(item) != {"item_id", "accepted_content_hash", "accepted_manifest_hash", "integration_manifest_hash", "record_count"}:
            raise BusinessPresentationPlanError("v2 input binding is invalid")
        if any(not _is_sha256(item.get(key)) for key in ("accepted_content_hash", "accepted_manifest_hash", "integration_manifest_hash")):
            raise BusinessPresentationPlanError("v2 input binding hash is invalid")
        if isinstance(item.get("record_count"), bool) or not isinstance(item.get("record_count"), int) or item["record_count"] < 0:
            raise BusinessPresentationPlanError("v2 input binding record_count is invalid")
    parent = plan.get("parent")
    if parent is not None and (not isinstance(parent, Mapping) or set(parent) != {"generation_id", "product_manifest_ref", "product_manifest_sha256", "receipt_ref", "receipt_sha256"}):
        raise BusinessPresentationPlanError("v2 parent binding is invalid")
    if isinstance(parent, Mapping) and (not _is_sha256(parent.get("product_manifest_sha256")) or not _is_sha256(parent.get("receipt_sha256"))):
        raise BusinessPresentationPlanError("v2 parent binding hashes are invalid")
    if not isinstance(plan.get("reviewer_ref"), str) or not plan["reviewer_ref"].strip():
        raise BusinessPresentationPlanError("v2 reviewer_ref is required")
    manager_visual = plan.get("manager_visual_widget_ids")
    audit_visual = plan.get("audit_visual_widget_ids")
    if (
        not isinstance(manager_visual, list)
        or not isinstance(audit_visual, list)
        or len(set(manager_visual)) != len(manager_visual)
        or len(set(audit_visual)) != len(audit_visual)
        or set(manager_visual).intersection(audit_visual)
        or any(not isinstance(value, str) or not value.strip() for value in manager_visual + audit_visual)
        or not manager_visual
        or not audit_visual
    ):
        raise BusinessPresentationPlanError("v2 visual partition is invalid")
    entries = plan.get("visual_entries")
    if not isinstance(entries, list) or len(entries) != len(set(manager_visual + audit_visual)):
        raise BusinessPresentationPlanError("v2 visual entries must cover the visual partition")
    entry_ids = [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)]
    if entry_ids != manager_visual + audit_visual or len(set(entry_ids)) != len(entry_ids):
        raise BusinessPresentationPlanError("v2 visual entries must preserve the reviewed partition order")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise BusinessPresentationPlanError("v2 visual entry is invalid")
        _visual_entry_shape(entry)
    manager_ids = plan.get("manager_widget_ids")
    manager_entries = plan.get("manager_entries")
    if not isinstance(manager_ids, list) or not isinstance(manager_entries, list) or len(manager_ids) != len(manager_entries):
        raise BusinessPresentationPlanError("v2 manager entries are invalid")
    if len(set(manager_ids)) != len(manager_ids) or [entry.get("widget_id") for entry in manager_entries if isinstance(entry, Mapping)] != manager_ids:
        raise BusinessPresentationPlanError("v2 manager entries must preserve manager order")
    manager_visual_set = set(manager_visual)
    if not manager_visual_set <= set(manager_ids):
        raise BusinessPresentationPlanError("v2 manager visuals are missing manager entries")
    visual_by_id = {entry["widget_id"]: entry for entry in entries}
    for entry in manager_entries:
        if not isinstance(entry, Mapping):
            raise BusinessPresentationPlanError("v2 manager entry is invalid")
        widget_id = _text(entry.get("widget_id"))
        if widget_id in manager_visual_set:
            visual = visual_by_id[widget_id]
            # Every visual manager entry must carry the exact chart projection
            # in addition to its immutable envelope.  Existing V1 entries may
            # retain their reviewed record projection on the overlap card.
            for key in ("visual_type", "chart_family", "widget_snapshot_sha256", "chart_entry_sha256", "allowed_visual_fields", "title_projection", "visual_projection"):
                if entry.get(key) != visual.get(key):
                    raise BusinessPresentationPlanError(f"v2 manager visual entry drifted: {widget_id}:{key}")
        else:
            # The six pre-existing conclusion entries intentionally retain the
            # V1 pointer projection shape.  They are not chart entries except
            # for the pending-refunds overlap, which is checked above.
            _presentation_entry_shape(entry)
    source_bindings = plan.get("source_bindings")
    if not isinstance(source_bindings, Mapping):
        raise BusinessPresentationPlanError("v2 source bindings are invalid")
    for key in ("fixture_ref", "fixture_sha256", "chart_map_ref", "chart_map_sha256"):
        if not isinstance(source_bindings.get(key), str) or not source_bindings[key].strip():
            raise BusinessPresentationPlanError(f"v2 source binding is missing: {key}")
    for key in ("fixture_sha256", "chart_map_sha256"):
        if not _is_sha256(source_bindings.get(key)):
            raise BusinessPresentationPlanError(f"v2 source binding hash is invalid: {key}")
    # The successor must carry the exact V1 manager contract it claims to
    # preserve.  This is intentionally validated from the plan bytes rather
    # than inferred from non-empty IDs in an existing receipt.
    _v2_predecessor_manager_contract(plan)
    # A V2 successor also binds the complete manager envelope of its
    # immediate V2 predecessor.  Keep this separate from the six-entry V1
    # migration binding above because visual manager entries have richer
    # projection fields.
    _v2_predecessor_plan_manager_contract(plan)
    _v2_predecessor_visual_contract(plan)


def _validate_business_presentation_plan_v2(
    context: RunContext,
    plan: Mapping[str, Any],
    *,
    fixture: Mapping[str, Any],
    fixture_ref: str,
    chart_map: Mapping[str, Any],
    chart_map_ref: str,
    widgets: Sequence[Mapping[str, Any]],
    strict_source_hash: bool = True,
) -> list[str]:
    """Validate V2 visual snapshots/projections against the candidate bytes."""

    _validate_presentation_plan_v2_shape(plan)
    source = plan["source_bindings"]
    fixture_path = context.resolve_run_path(fixture_ref)
    chart_map_path = context.resolve_run_path(chart_map_ref)
    if strict_source_hash and (source["fixture_ref"] != fixture_ref or source["chart_map_ref"] != chart_map_ref):
        raise BusinessPresentationPlanError("v2 visual source references drifted")
    if strict_source_hash and (_sha256_bytes(fixture_path.read_bytes()) != source["fixture_sha256"] or _sha256_bytes(chart_map_path.read_bytes()) != source["chart_map_sha256"]):
        raise BusinessPresentationPlanError("v2 visual source hashes drifted")
    raw_charts = chart_map.get("charts")
    if not isinstance(raw_charts, list):
        raise BusinessPresentationPlanError("v2 chart map charts are invalid")
    charts_by_id = {_text(value.get("id")): value for value in raw_charts if isinstance(value, Mapping)}
    widgets_by_id = {_text(widget.get("id")): widget for widget in widgets}
    if set(charts_by_id) != set(widgets_by_id):
        raise BusinessPresentationPlanError("v2 chart map/widget IDs drifted")
    visual_ids = set(plan["manager_visual_widget_ids"] + plan["audit_visual_widget_ids"])
    current_visual_ids = set(_true_visual_ids(widgets_by_id, charts_by_id))
    if current_visual_ids != visual_ids:
        raise BusinessPresentationPlanError("v2 visual partition does not cover the current fixture visual universe")
    visual_by_id = {entry["widget_id"]: entry for entry in plan["visual_entries"]}
    for widget_id in visual_ids:
        widget = widgets_by_id.get(widget_id)
        chart = charts_by_id.get(widget_id)
        entry = visual_by_id.get(widget_id)
        if widget is None or chart is None or entry is None:
            raise BusinessPresentationPlanError(f"v2 visual widget is missing: {widget_id}")
        if _visual_snapshot_hash(widget) != entry["widget_snapshot_sha256"] or _visual_chart_hash(chart) != entry["chart_entry_sha256"]:
            raise BusinessPresentationPlanError(f"v2 visual snapshot/chart hash drifted: {widget_id}")
        if entry["requirement_id"] != _text(widget.get("requirement_id")) or entry["visual_type"] != _text(widget.get("type") or widget.get("kind")):
            raise BusinessPresentationPlanError(f"v2 visual identity drifted: {widget_id}")
        record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        })
        if entry["record_ids"] != record_ids:
            raise BusinessPresentationPlanError(f"v2 visual record bindings drifted: {widget_id}")
        title_binding = entry["title_projection"]
        title_root = {"widget_snapshot": dict(widget)}
        title_value = _visual_pointer_value(title_root, title_binding["pointer"])
        if not _presentation_projection_value_equal(title_value, title_binding["value"]):
            raise BusinessPresentationPlanError(f"v2 visual title projection drifted: {widget_id}")
        chart_root = dict(chart)
        chart_root["title"] = widget.get("title")
        root = {"widget_snapshot": dict(widget), "chart_entry": chart_root}
        for field, binding in entry["visual_projection"].items():
            pointer = binding["pointer"]
            if pointer.startswith("/widget_snapshot"):
                actual = _visual_pointer_value(root, pointer)
            elif pointer.startswith("/chart_entry"):
                actual = _visual_pointer_value(root, pointer)
            else:
                raise BusinessPresentationPlanError(f"v2 visual projection pointer source is invalid: {widget_id}:{field}")
            if not _presentation_projection_value_equal(actual, binding["value"]):
                raise BusinessPresentationPlanError(f"v2 visual projection drifted: {widget_id}:{field}")
    return list(plan["manager_widget_ids"])


def _validate_v2_plan_lineage(
    context: RunContext,
    plan: Mapping[str, Any],
    *,
    generation_id: str,
    supervisor_ref: str,
    input_items: Sequence[Mapping[str, Any]],
    parent: Mapping[str, Any] | None,
) -> None:
    """Validate V2 lifecycle/input bindings before candidate staging."""

    _validate_presentation_plan_v2_shape(plan)
    if plan.get("run_id") != context.run_id or plan.get("generation_id") != generation_id:
        raise BusinessPresentationPlanError("v2 presentation plan run/generation binding is stale")
    supervisor_path = context.resolve_run_path(supervisor_ref)
    if plan.get("supervisor_plan_ref") != supervisor_ref or not supervisor_path.is_file() or _sha256_bytes(supervisor_path.read_bytes()) != plan.get("supervisor_plan_sha256"):
        raise BusinessPresentationPlanError("v2 supervisor plan binding drifted")
    planned = {value.get("item_id"): dict(value) for value in plan.get("input_items", []) if isinstance(value, Mapping)}
    current = {value.get("item_id"): dict(value) for value in input_items if isinstance(value, Mapping)}
    if len(planned) != len(plan.get("input_items", [])) or len(current) != len(input_items) or planned != current:
        raise BusinessPresentationPlanError("v2 accepted/committed input bindings drifted")
    if plan.get("parent") != parent:
        raise BusinessPresentationPlanError("v2 immediate parent lineage drifted")


def _validate_business_presentation_plan(
    context: RunContext,
    plan: Mapping[str, Any],
    *,
    generation_id: str,
    supervisor_ref: str,
    supervisor_plan: Mapping[str, Any],
    input_items: Sequence[Mapping[str, Any]],
    parent: Mapping[str, Any] | None,
    widgets: Sequence[Mapping[str, Any]] | None = None,
    authoritative_roots: Mapping[str, Mapping[str, Any]] | None = None,
    record_file_hashes: Mapping[str, str] | None = None,
) -> list[str]:
    _validate_presentation_plan_shape(plan)
    if plan.get("run_id") != context.run_id or plan.get("generation_id") != generation_id:
        raise BusinessPresentationPlanError("presentation plan run/generation binding is stale")
    if plan.get("supervisor_plan_ref") != supervisor_ref or plan.get("supervisor_plan_sha256") != _sha256_bytes(context.resolve_run_path(supervisor_ref).read_bytes()):
        raise BusinessPresentationPlanError("presentation plan supervisor binding drifted")
    # ``item_order`` is the reviewed supervisor/display order, not an
    # identity binding for the cumulative input set.  Fresh and delta builds
    # may legitimately discover the same accepted/committed items in a
    # different order (for example, a delta starts with its immediate
    # parent's receipt order and appends the new item).  Compare the
    # immutable bindings as an identity map instead: exact cardinality,
    # unique item IDs, and the complete per-item hash/count record must all
    # agree.  This keeps the security boundary (no missing, foreign,
    # duplicated, or changed accepted/integration input) without making
    # supervisor order a false integrity constraint.
    planned_items = plan.get("input_items")
    current_items = [dict(value) for value in input_items if isinstance(value, Mapping)]
    if not isinstance(planned_items, list) or len(current_items) != len(input_items):
        raise BusinessPresentationPlanError("presentation plan accepted/committed input bindings drifted")

    def _binding_map(values: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]] | None:
        result: dict[str, dict[str, Any]] = {}
        for value in values:
            item_id = value.get("item_id")
            if not isinstance(item_id, str) or not item_id or item_id in result:
                return None
            result[item_id] = dict(value)
        return result

    planned_map = _binding_map([value for value in planned_items if isinstance(value, Mapping)])
    current_map = _binding_map(current_items)
    if planned_map is None or current_map is None or set(planned_map) != set(current_map) or any(
        planned_map[item_id] != current_map[item_id] for item_id in planned_map
    ):
        raise BusinessPresentationPlanError("presentation plan accepted/committed input bindings drifted")
    if plan.get("parent") != parent:
        raise BusinessPresentationPlanError("presentation plan immediate parent lineage drifted")
    entries = {entry["widget_id"]: entry for entry in plan["manager_entries"]}
    if widgets is None:
        if authoritative_roots is not None:
            for entry in entries.values():
                root = authoritative_roots.get(entry["record_id"])
                if not isinstance(root, Mapping):
                    raise BusinessPresentationPlanError(f"presentation plan record is not authoritative: {entry['record_id']}")
                payload = root.get("payload")
                if not isinstance(payload, Mapping) or entry["canonical_payload_sha256"] != _sha256_bytes(_canonical_bytes(payload)):
                    raise BusinessPresentationPlanError(f"presentation plan canonical payload hash drifted: {entry['record_id']}")
                if record_file_hashes is not None and entry["file_sha256"] != record_file_hashes.get(entry["record_id"]):
                    raise BusinessPresentationPlanError(f"presentation plan committed records file hash drifted: {entry['record_id']}")
                for field, binding in entry["display_projection"].items():
                    actual_value = _presentation_pointer_value(root, binding["pointer"])
                    if not _presentation_projection_value_equal(actual_value, binding["value"]):
                        raise BusinessPresentationPlanError(f"presentation plan projection value drifted: {entry['record_id']}:{field}")
        return list(plan["manager_widget_ids"])
    by_id = {_text(widget.get("id")): widget for widget in widgets if _text(widget.get("id"))}
    if set(plan["manager_widget_ids"]) - set(by_id):
        missing = sorted(set(plan["manager_widget_ids"]) - set(by_id))
        raise BusinessPresentationPlanError(f"presentation plan references unknown widget IDs: {missing[:5]}")
    for widget_id in plan["manager_widget_ids"]:
        widget = by_id[widget_id]
        entry = entries[widget_id]
        actual = _presentation_widget_binding(widget)
        if entry["requirement_id"] != actual["requirement_id"] or entry["presentation_role"] != actual["presentation_role"]:
            raise BusinessPresentationPlanError(f"presentation plan widget identity drifted: {widget_id}")
        record_id = _presentation_record_id_for_widget(widget)
        if entry["record_id"] != record_id:
            raise BusinessPresentationPlanError(f"presentation plan manager record drifted: {widget_id}")
        if authoritative_roots is not None:
            root = authoritative_roots.get(record_id)
            if not isinstance(root, Mapping):
                raise BusinessPresentationPlanError(f"presentation plan record is not authoritative: {record_id}")
            payload = root.get("payload")
            if not isinstance(payload, Mapping):
                raise BusinessPresentationPlanError(f"presentation plan record payload is invalid: {record_id}")
            expected_payload_hash = _sha256_bytes(_canonical_bytes(payload))
            if entry["canonical_payload_sha256"] != expected_payload_hash:
                raise BusinessPresentationPlanError(f"presentation plan canonical payload hash drifted: {record_id}")
            if record_file_hashes is not None and entry["file_sha256"] != record_file_hashes.get(record_id):
                raise BusinessPresentationPlanError(f"presentation plan committed records file hash drifted: {record_id}")
            projection = entry["display_projection"]
            for field, binding in projection.items():
                actual_value = _presentation_pointer_value(root, binding["pointer"])
                if not _presentation_projection_value_equal(actual_value, binding["value"]):
                    raise BusinessPresentationPlanError(f"presentation plan projection value drifted: {record_id}:{field}")
        # Attach the exact entry to the widget.  The renderer consumes this
        # projection and never reconstructs manager text from the raw widget.
        if widget.get("manager_presentation") != dict(entry):
            raise BusinessPresentationPlanError(f"presentation plan widget projection is not bound: {widget_id}")
    return list(plan["manager_widget_ids"])


def _load_business_presentation_plan(context: RunContext, reference: str | Path) -> tuple[Mapping[str, Any], str]:
    path = context.resolve_run_path(reference)
    if path.is_symlink() or not path.is_file():
        raise BusinessPresentationPlanError("presentation plan is missing or symlinked")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("presentation plan is invalid") from exc
    if not isinstance(raw, Mapping):
        raise BusinessPresentationPlanError("presentation plan must be an object")
    if raw.get("schema_version") == PRESENTATION_PLAN_V2_SCHEMA:
        _validate_presentation_plan_v2_shape(raw)
    else:
        _validate_presentation_plan_shape(raw)
    return dict(raw), _sha256_bytes(path.read_bytes())


def business_presentation_inventory(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    generation_id: str | None = None,
) -> dict[str, Any]:
    """Return a read-only exact widget inventory for reviewer selection."""

    if not isinstance(context, RunContext):
        raise TypeError("business_presentation_inventory requires one RunContext")
    generation, metadata = _presentation_generation_metadata(context, generation_id)
    supervisor_ref, supervisor_plan, supervisor_hash = _presentation_supervisor_binding(context, generation, metadata)
    fixture = _read_json(context, fixture_ref, label="presentation inventory fixture")
    widgets = fixture.get("widgets")
    if not isinstance(widgets, list):
        raise BusinessPresentationPlanError("presentation inventory fixture widgets are invalid")
    item_order = _discover_item_ids(context, None, supervisor_plan)
    input_items = _presentation_input_bindings(context, item_order)
    parent = _presentation_parent_binding(context, generation, metadata)
    # Build the authoritative record index once.  Inventory values are
    # suggestions only; a recorder revalidates every pointer and hash against
    # these immutable committed bytes before writing a plan.
    authoritative: dict[str, dict[str, Any]] = {}
    record_file_hashes: dict[str, str] = {}
    for item_id in item_order:
        content, accepted_manifest, accepted_meta = _load_public_accepted_bundle(context, item_id)
        _integration_manifest, records = _load_committed_records(context, item_id, accepted_manifest, accepted_meta["bundle"])
        records_ref = f"requirements/{item_id}/integration/committed/records.jsonl"
        records_path = context.resolve_run_path(records_ref)
        records_file_sha = _sha256_bytes(records_path.read_bytes())
        for record in records:
            record_id = _text(record.get("record_id")).strip()
            if not record_id:
                continue
            authoritative[record_id] = {
                "root": _presentation_authoritative_root(record, content),
                "record": record,
                "file_sha256": records_file_sha,
                "canonical_payload_sha256": _sha256_bytes(_canonical_bytes(_record_payload(record))),
            }
            record_file_hashes[record_id] = records_file_sha
    candidates = []
    for widget in widgets:
        if not isinstance(widget, Mapping) or not _text(widget.get("id")):
            continue
        binding = _presentation_widget_binding(widget)
        binding["type"] = _text(widget.get("type") or widget.get("kind"))
        binding["value"] = widget.get("value") if "value" in widget else None
        binding["unit"] = widget.get("unit")
        binding["denominator"] = widget.get("denominator")
        binding["evidence_refs"] = list(_as_list(widget.get("evidence_refs")))
        binding["trace_refs"] = list(_as_list(widget.get("trace_refs")))
        binding["audit_payload"] = copy.deepcopy(widget.get("audit_payload"))
        record_ids = binding["integration_record_ids"]
        if len(record_ids) == 1 and record_ids[0] in authoritative:
            details = authoritative[record_ids[0]]
            binding["record_id"] = record_ids[0]
            binding["file_sha256"] = details["file_sha256"]
            binding["canonical_payload_sha256"] = details["canonical_payload_sha256"]
            binding["authoritative_payload"] = copy.deepcopy(details["root"].get("payload", {}))
            binding["accepted_fields"] = copy.deepcopy(details["root"].get("accepted", {}))
            binding["display_projection"] = _candidate_display_projection(details["root"], details["record"])
        candidates.append(binding)
    return {
        "schema_version": PRESENTATION_PLAN_SCHEMA,
        "run_id": context.run_id,
        "generation_id": generation,
        "supervisor_plan_ref": supervisor_ref,
        "supervisor_plan_sha256": supervisor_hash,
        "item_order": item_order,
        "input_items": input_items,
        "parent": parent,
        "inventory_widget_count": len(candidates),
        "record_file_hashes": record_file_hashes,
        "candidates": candidates,
    }


def business_presentation_visual_inventory(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Return exact chart snapshots and map projections for V2 review.

    The inventory is read-only.  It exposes only the existing fixture/chart
    map bytes and their canonical hashes; a V2 writer revalidates these values
    immediately before recording a plan.
    """

    fixture_path = context.resolve_run_path(fixture_ref)
    if fixture_path.is_symlink() or not fixture_path.is_file():
        raise BusinessPresentationPlanError("v2 visual inventory fixture is missing or symlinked")
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 visual inventory fixture is invalid") from exc
    if not isinstance(fixture, Mapping) or not isinstance(fixture.get("widgets"), list):
        raise BusinessPresentationPlanError("v2 visual inventory fixture widgets are invalid")
    resolved_chart_ref = _text(chart_map_ref or fixture.get("chart_map_ref")).strip()
    if not resolved_chart_ref:
        raise BusinessPresentationPlanError("v2 visual inventory requires chart_map_ref")
    chart_path = context.resolve_run_path(resolved_chart_ref)
    if chart_path.is_symlink() or not chart_path.is_file():
        raise BusinessPresentationPlanError("v2 visual inventory chart map is missing or symlinked")
    try:
        chart_map = json.loads(chart_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 visual inventory chart map is invalid") from exc
    if not isinstance(chart_map, Mapping) or not isinstance(chart_map.get("charts"), list):
        raise BusinessPresentationPlanError("v2 visual inventory chart map charts are invalid")
    widgets_by_id = {
        _text(widget.get("id")): widget
        for widget in fixture["widgets"]
        if isinstance(widget, Mapping) and _text(widget.get("id"))
    }
    charts_by_id = {
        _text(chart.get("id")): chart
        for chart in chart_map["charts"]
        if isinstance(chart, Mapping) and _text(chart.get("id"))
    }
    if set(widgets_by_id) != set(charts_by_id):
        raise BusinessPresentationPlanError("v2 visual inventory fixture/chart map IDs drifted")
    visual_ids = _true_visual_ids(widgets_by_id, charts_by_id)
    if not visual_ids:
        raise BusinessPresentationPlanError("v2 visual inventory has no supported visual charts")
    fixture_manager_visual = fixture.get("manager_visual_widget_ids")
    fixture_audit_visual = fixture.get("audit_visual_widget_ids")
    if (
        isinstance(fixture_manager_visual, list)
        and isinstance(fixture_audit_visual, list)
        and set(fixture_manager_visual).union(fixture_audit_visual) == set(visual_ids)
        and not set(fixture_manager_visual).intersection(fixture_audit_visual)
    ):
        manager_visual_ids = list(fixture_manager_visual)
        audit_visual_ids = list(fixture_audit_visual)
    else:
        # A planless candidate has no audience authority yet.  Inventory all
        # true visuals in deterministic fixture order; the successor writer
        # applies the predecessor partition and explicitly admits only the
        # reviewed additions.
        manager_visual_ids = list(visual_ids)
        audit_visual_ids = []
    entries: list[dict[str, Any]] = []
    for widget_id in manager_visual_ids + audit_visual_ids:
        widget = widgets_by_id.get(widget_id)
        chart = charts_by_id.get(widget_id)
        if widget is None or chart is None:
            raise BusinessPresentationPlanError(f"v2 visual inventory widget is missing: {widget_id}")
        kind = _text(widget.get("type") or widget.get("kind")).strip()
        reviewed_table_fact = bool(widget.get("dashboard_fact")) and kind == "table"
        if (kind in {"status_table", ""} or (kind == "table" and not reviewed_table_fact) or _text(chart.get("type")) != kind):
            raise BusinessPresentationPlanError(f"v2 visual inventory widget type is invalid: {widget_id}")
        fields = chart.get("fields_or_values_used")
        if not isinstance(fields, Mapping):
            raise BusinessPresentationPlanError(f"v2 visual inventory chart fields are invalid: {widget_id}")
        record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        })
        visual_projection: dict[str, dict[str, Any]] = {
            "type": {"pointer": "/chart_entry/type", "value": copy.deepcopy(chart.get("type"))},
            "family": {"pointer": "/chart_entry/family", "value": copy.deepcopy(chart.get("family"))},
        }
        for field, value in fields.items():
            # Presentation role/tier are plan-derived envelope metadata, not
            # visual geometry or a reviewed value.  Excluding them keeps a
            # V1 fixture rebinding stable while every chart field remains
            # pointer-bound and exact.
            if field in {"presentation_role", "presentation_tier"}:
                continue
            visual_projection[field] = {
                "pointer": f"/chart_entry/fields_or_values_used/{_presentation_pointer_escape(field)}",
                "value": copy.deepcopy(value),
            }
        audience = "business_manager" if widget_id in manager_visual_ids else "technical_audit_gallery"
        entries.append({
            "widget_id": widget_id,
            "requirement_id": _text(widget.get("requirement_id")),
            "record_ids": record_ids,
            "presentation_audience": audience,
            "visual_type": kind,
            "chart_family": _text(chart.get("family")),
            "widget_snapshot_sha256": _visual_snapshot_hash(widget),
            "chart_entry_sha256": _visual_chart_hash(chart),
            "allowed_visual_fields": list(visual_projection),
            "title_projection": {
                "pointer": "/widget_snapshot/title",
                "value": copy.deepcopy(widget.get("title") or widget.get("label") or widget_id),
            },
            "visual_projection": visual_projection,
        })
    try:
        fixture_run_ref = fixture_path.relative_to(context.run_root).as_posix()
        chart_run_ref = chart_path.relative_to(context.run_root).as_posix()
    except ValueError as exc:
        raise BusinessPresentationPlanError("v2 visual inventory source escaped the run root") from exc
    return {
        "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
        "fixture_ref": fixture_run_ref,
        "fixture_sha256": _sha256_bytes(fixture_path.read_bytes()),
        "chart_map_ref": chart_run_ref,
        "chart_map_sha256": _sha256_bytes(chart_path.read_bytes()),
        "fixture_widget_count": len(widgets_by_id),
        "visual_widget_count": len(entries),
        "all_visual_widget_ids": list(visual_ids),
        "manager_visual_widget_ids": manager_visual_ids,
        "audit_visual_widget_ids": audit_visual_ids,
        "visual_entries": entries,
    }


def write_business_presentation_plan_v2(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path | None = None,
    previous_plan_ref: str | Path,
    reviewer_ref: str,
    presentation_plan_ref: str | Path | None = None,
    _lock_held: bool = False,
) -> dict[str, Any]:
    """Build a canonical V2 plan candidate without mutating a live plan.

    The returned object is suitable for :func:`revise_business_presentation_plan_v2`;
    callers may write it to a temporary path for independent review first.
    """

    visual_inventory = business_presentation_visual_inventory(context, fixture_ref=fixture_ref, chart_map_ref=chart_map_ref)
    raw_previous_ref = Path(previous_plan_ref).as_posix()
    old_plan, old_hash = _load_business_presentation_plan(context, raw_previous_ref)
    if old_plan.get("schema_version") not in {PRESENTATION_PLAN_SCHEMA, PRESENTATION_PLAN_V2_SCHEMA}:
        raise BusinessPresentationPlanError("v2 successor requires a V1 or V2 predecessor plan")

    generation_id, metadata = _presentation_generation_metadata(context, _lock_held=_lock_held)
    supervisor_ref, supervisor_plan, supervisor_hash = _presentation_supervisor_binding(context, generation_id, metadata)
    current_item_order = _discover_item_ids(context, None, supervisor_plan)
    current_input_items = _presentation_input_bindings(context, current_item_order)
    current_parent = _presentation_parent_binding(context, generation_id, metadata)

    old_order = [_text(entry.get("widget_id")) for entry in old_plan.get("manager_entries", [])]
    manager_entries_by_id: dict[str, dict[str, Any]] = {
        _text(entry.get("widget_id")): copy.deepcopy(dict(entry))
        for entry in old_plan.get("manager_entries", [])
        if isinstance(entry, Mapping)
    }
    current_visual_by_id = {entry["widget_id"]: entry for entry in visual_inventory["visual_entries"]}
    current_visual_ids = list(visual_inventory.get("all_visual_widget_ids") or [
        *visual_inventory["manager_visual_widget_ids"],
        *visual_inventory["audit_visual_widget_ids"],
    ])

    if old_plan.get("schema_version") == PRESENTATION_PLAN_V2_SCHEMA:
        previous_manager_visual_ids = list(old_plan.get("manager_visual_widget_ids") or [])
        previous_audit_visual_ids = list(old_plan.get("audit_visual_widget_ids") or [])
        previous_visual_entries = [copy.deepcopy(dict(entry)) for entry in old_plan.get("visual_entries", [])]
    else:
        # V1 has no visual partition.  For the one-time G3 migration the
        # reviewed constants remain the predecessor contract; later
        # generation successors inherit the V2 partition above.
        previous_manager_visual_ids = [widget_id for widget_id in V2_MANAGER_VISUAL_WIDGET_IDS if widget_id in current_visual_ids]
        previous_audit_visual_ids = [widget_id for widget_id in V2_AUDIT_VISUAL_WIDGET_IDS if widget_id in current_visual_ids]
        previous_visual_entries = []

    previous_visual_set = set(previous_manager_visual_ids + previous_audit_visual_ids)
    if not previous_visual_set.issubset(set(current_visual_ids)):
        missing = sorted(previous_visual_set - set(current_visual_ids))
        raise BusinessPresentationPlanError(f"v2 predecessor visual IDs are missing from the successor fixture: {missing[:5]}")
    new_visual_ids = [widget_id for widget_id in current_visual_ids if widget_id not in previous_visual_set]
    manager_visual_ids = previous_manager_visual_ids + new_visual_ids
    audit_visual_ids = list(previous_audit_visual_ids)
    if set(manager_visual_ids).intersection(audit_visual_ids) or set(manager_visual_ids + audit_visual_ids) != set(current_visual_ids):
        raise BusinessPresentationPlanError("v2 successor visual partition does not cover the current fixture")

    previous_visual_by_id = {
        _text(entry.get("widget_id")): entry
        for entry in previous_visual_entries
        if isinstance(entry, Mapping)
    }
    visual_entries: list[dict[str, Any]] = []
    for widget_id in manager_visual_ids + audit_visual_ids:
        visual = copy.deepcopy(current_visual_by_id[widget_id])
        if widget_id in previous_visual_by_id:
            predecessor = previous_visual_by_id[widget_id]
            # Preserve the full predecessor binding.  A changed snapshot,
            # title, value, or geometry is a semantic review event, not an
            # implicit rebind during product assembly.
            for key in (
                "requirement_id", "record_ids", "visual_type", "chart_family",
                "widget_snapshot_sha256", "chart_entry_sha256", "allowed_visual_fields",
                "title_projection", "visual_projection",
            ):
                if visual.get(key) != predecessor.get(key):
                    raise BusinessPresentationPlanError(f"v2 predecessor visual drifted: {widget_id}:{key}")
            visual["presentation_audience"] = predecessor["presentation_audience"]
        else:
            visual["presentation_audience"] = "business_manager" if widget_id in manager_visual_ids else "technical_audit_gallery"
        visual_entries.append(visual)

    # Keep inherited manager entries byte-compatible at the V1 envelope while
    # attaching the exact current visual projection for visual overlaps.
    visual_by_id = {entry["widget_id"]: entry for entry in visual_entries}
    for widget_id in manager_visual_ids:
        visual = visual_by_id[widget_id]
        visual_manager = _v2_manager_entry_from_visual(visual)
        if widget_id in manager_entries_by_id:
            manager_entries_by_id[widget_id].update(visual_manager)
            manager_entries_by_id[widget_id]["widget_id"] = widget_id
            manager_entries_by_id[widget_id]["requirement_id"] = visual["requirement_id"]
        else:
            manager_entries_by_id[widget_id] = visual_manager
    manager_order = old_order + [widget_id for widget_id in manager_visual_ids if widget_id not in old_order]

    if old_plan.get("schema_version") == PRESENTATION_PLAN_V2_SCHEMA:
        # V2 predecessors carry two manager contracts: the original six
        # V1 conclusion entries used for the one-time migration guard, and
        # the complete prior V2 manager envelope (including visual entries).
        # Preserve both explicitly; never reinterpret the richer manager
        # entries as the V1 shape.
        old_source = old_plan.get("source_bindings")
        if not isinstance(old_source, Mapping):
            raise BusinessPresentationPlanError("v2 predecessor source bindings are missing")
        previous_manager_widget_ids = copy.deepcopy(old_source.get("previous_manager_widget_ids"))
        previous_manager_entries = copy.deepcopy(old_source.get("previous_manager_entries"))
        if previous_manager_widget_ids is None or previous_manager_entries is None:
            raise BusinessPresentationPlanError("v2 predecessor V1 manager binding is missing")
    else:
        previous_manager_widget_ids = list(old_order)
        previous_manager_entries = copy.deepcopy(old_plan["manager_entries"])

    source_bindings = {
        "fixture_ref": visual_inventory["fixture_ref"],
        "fixture_sha256": visual_inventory["fixture_sha256"],
        "chart_map_ref": visual_inventory["chart_map_ref"],
        "chart_map_sha256": visual_inventory["chart_map_sha256"],
        "previous_plan_ref": raw_previous_ref,
        "previous_plan_sha256": old_hash,
        "previous_manager_widget_ids": previous_manager_widget_ids,
        "previous_manager_entries": previous_manager_entries,
        "previous_manager_visual_widget_ids": list(previous_manager_visual_ids),
        "previous_audit_visual_widget_ids": list(previous_audit_visual_ids),
        "previous_visual_entries": copy.deepcopy(previous_visual_entries or [
            current_visual_by_id[widget_id]
            for widget_id in previous_manager_visual_ids + previous_audit_visual_ids
        ]),
    }
    if old_plan.get("schema_version") == PRESENTATION_PLAN_V2_SCHEMA:
        source_bindings["previous_plan_manager_widget_ids"] = list(old_order)
        source_bindings["previous_plan_manager_entries"] = copy.deepcopy(old_plan["manager_entries"])
    plan = {
        "schema_version": PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": context.run_id,
        "generation_id": generation_id,
        "supervisor_plan_ref": supervisor_ref,
        "supervisor_plan_sha256": supervisor_hash,
        "item_order": current_item_order,
        "input_items": copy.deepcopy(current_input_items),
        "parent": copy.deepcopy(current_parent),
        "reviewer_ref": _text(reviewer_ref).strip(),
        "manager_widget_ids": manager_order,
        "manager_entries": [manager_entries_by_id[widget_id] for widget_id in manager_order],
        "manager_visual_widget_ids": manager_visual_ids,
        "audit_visual_widget_ids": audit_visual_ids,
        "visual_entries": visual_entries,
        "source_bindings": source_bindings,
    }
    _validate_presentation_plan_v2_shape(plan)
    return plan


def _revise_business_presentation_plan_v2_unlocked(
    context: RunContext,
    *,
    successor_plan: Mapping[str, Any] | str | Path,
    expected_current_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path,
) -> dict[str, Any]:
    """CAS-replace a V1 plan with one validated V2 successor atomically.

    This public mutation is intentionally narrow: it accepts exactly the
    expected current V1 bytes and expected canonical successor bytes.  A
    retry against already-published successor bytes is idempotent; any other
    current/successor combination fails before touching the plan file.
    """

    if not _is_sha256(expected_current_plan_sha256) or not _is_sha256(expected_successor_plan_sha256):
        raise BusinessPresentationPlanError("v2 revision expected hashes are invalid")
    path_ref = _presentation_plan_ref(context, _text(successor_plan.get("generation_id")) if isinstance(successor_plan, Mapping) else _active_generation_id(context), presentation_plan_ref)
    path = context.resolve_run_path(path_ref)
    if path.is_symlink() or not path.is_file():
        raise BusinessPresentationPlanError("v2 revision current plan is missing or symlinked")
    if isinstance(successor_plan, (str, Path)):
        successor_path = Path(successor_plan)
        if successor_path.is_symlink() or not successor_path.is_file():
            raise BusinessPresentationPlanError("v2 successor plan is missing or symlinked")
        successor_bytes = successor_path.read_bytes()
        try:
            candidate = json.loads(successor_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessPresentationPlanError("v2 successor plan is invalid") from exc
    elif isinstance(successor_plan, Mapping):
        candidate = dict(successor_plan)
        successor_bytes = _canonical_bytes(candidate)
    else:
        raise BusinessPresentationPlanError("v2 successor plan must be an object or JSON path")
    if not isinstance(candidate, Mapping):
        raise BusinessPresentationPlanError("v2 successor plan must be an object")
    _validate_presentation_plan_v2_shape(candidate)
    canonical_successor_bytes = _canonical_bytes(candidate)
    if successor_bytes != canonical_successor_bytes:
        raise BusinessPresentationPlanError("v2 successor plan is not canonical")
    if _sha256_bytes(successor_bytes) != expected_successor_plan_sha256:
        raise BusinessPresentationPlanError("v2 successor plan hash does not match expected hash")
    current_bytes = path.read_bytes()
    current_hash = _sha256_bytes(current_bytes)
    if current_hash == expected_successor_plan_sha256:
        if current_bytes != successor_bytes:
            raise BusinessPresentationPlanError("v2 successor hash matches but bytes differ")
        return dict(candidate)
    if current_hash != expected_current_plan_sha256:
        raise BusinessPresentationPlanError("v2 revision current plan hash does not match expected V1")
    try:
        current = json.loads(current_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 revision current plan is invalid") from exc
    if not isinstance(current, Mapping) or current.get("schema_version") != PRESENTATION_PLAN_SCHEMA:
        raise BusinessPresentationPlanError("v2 revision current plan must be V1")
    predecessor_ids, predecessor_entries = _v2_predecessor_manager_contract(candidate)
    current_ids = current.get("manager_widget_ids")
    current_entries = current.get("manager_entries")
    if current_ids != predecessor_ids or current_entries != predecessor_entries:
        raise BusinessPresentationPlanError("v2 successor does not preserve the exact V1 manager contract")
    source_bindings = candidate.get("source_bindings")
    if not isinstance(source_bindings, Mapping):
        raise BusinessPresentationPlanError("v2 successor source bindings are missing")
    previous_ref = Path(_text(source_bindings.get("previous_plan_ref"))).as_posix()
    if previous_ref != path_ref or source_bindings.get("previous_plan_sha256") != expected_current_plan_sha256:
        raise BusinessPresentationPlanError("v2 successor previous-plan CAS binding is invalid")
    fixture_ref = _text(source_bindings.get("fixture_ref"))
    chart_map_ref = _text(source_bindings.get("chart_map_ref"))
    if not fixture_ref or not chart_map_ref:
        raise BusinessPresentationPlanError("v2 successor visual source bindings are missing")
    fixture_path = context.resolve_run_path(fixture_ref)
    chart_map_path = context.resolve_run_path(chart_map_ref)
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        chart_map = json.loads(chart_map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("v2 successor visual source is invalid") from exc
    if not isinstance(fixture, Mapping) or not isinstance(chart_map, Mapping) or not isinstance(fixture.get("widgets"), list):
        raise BusinessPresentationPlanError("v2 successor visual source shape is invalid")
    _validate_business_presentation_plan_v2(
        context,
        candidate,
        fixture=fixture,
        fixture_ref=fixture_ref,
        chart_map=chart_map,
        chart_map_ref=chart_map_ref,
        widgets=[widget for widget in fixture["widgets"] if isinstance(widget, Mapping)],
        strict_source_hash=True,
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.v2.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(successor_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(candidate)


def revise_business_presentation_plan_v2(
    context: RunContext,
    *,
    successor_plan: Mapping[str, Any] | str | Path,
    expected_current_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path,
) -> dict[str, Any]:
    """CAS-replace a V1 plan with one validated V2 successor under run lock.

    The compare/read/validate/replace sequence is serialized with dashboard
    delta and lifecycle publication through the same run lock.  This keeps a
    concurrent generation admission from racing a plan CAS while preserving
    the public function's exact-retry semantics.
    """

    try:
        from auto_foundry_core.lifecycle import RunLifecycle
    except ModuleNotFoundError as exc:  # pragma: no cover - package guard
        raise BusinessPresentationPlanError("RunLifecycle is unavailable for plan revision") from exc
    with RunLifecycle._run_lock(context):  # noqa: SLF001 - publication CAS boundary
        return _revise_business_presentation_plan_v2_unlocked(
            context,
            successor_plan=successor_plan,
            expected_current_plan_sha256=expected_current_plan_sha256,
            expected_successor_plan_sha256=expected_successor_plan_sha256,
            presentation_plan_ref=presentation_plan_ref,
        )


def _direct_v2_reference(
    context: RunContext,
    reference: str | Path,
    *,
    expected: str,
    label: str,
) -> tuple[str, Path]:
    """Validate one canonical generation-scoped input reference.

    The direct recorder intentionally accepts only the active generation's
    canonical fixture/map, its immediate predecessor plan, and its own plan
    target.  This keeps the public mutation path deterministic and prevents a
    caller from selecting an unrelated run-relative file while still using
    the normal run-root containment checks.
    """

    value = _text(reference).strip()
    if not value or "\\" in value or "\x00" in value:
        raise BusinessPresentationPlanError(f"{label} reference is invalid")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts or raw.as_posix() != expected:
        raise BusinessPresentationPlanError(f"{label} reference is not canonical: {value}")

    # ``RunContext.resolve_run_path`` returns a resolved path.  Checking only
    # that result would therefore miss an in-root symlink (the resolved path
    # is no longer itself a symlink).  Walk the lexical path first so the
    # direct publication boundary cannot read through or replace a symlinked
    # source/target component.
    lexical = context.run_root.joinpath(*raw.parts)
    current = context.run_root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise BusinessPresentationPlanError(f"{label} cannot be symlinked: {expected}")
    path = context.resolve_run_path(expected)
    if path != lexical or not path.is_file():
        raise BusinessPresentationPlanError(f"{label} is missing or symlinked: {expected}")
    return expected, path


def _direct_v2_target_path(
    context: RunContext,
    reference: str,
    *,
    expected: str,
) -> Path:
    """Resolve the canonical V2 target while preserving lexical symlink checks."""

    raw = Path(reference)
    if raw.is_absolute() or ".." in raw.parts or raw.as_posix() != expected:
        raise BusinessPresentationPlanError(f"direct V2 target must be canonical: {reference}")
    lexical = context.run_root.joinpath(*raw.parts)
    current = context.run_root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise BusinessPresentationPlanError("direct V2 target cannot be symlinked")
    resolved = context.resolve_run_path(reference)
    if resolved != lexical:
        raise BusinessPresentationPlanError("direct V2 target escaped the run root")
    return resolved


def _atomic_replace_plan_bytes(path: Path, payload: bytes) -> None:
    """Atomically publish bytes and restore the prior target on post-swap failure."""

    previous_exists = path.exists() or path.is_symlink()
    previous_bytes = path.read_bytes() if previous_exists and path.is_file() and not path.is_symlink() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.v2.record.", dir=path.parent)
    temporary = Path(temporary_name)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        replaced = True
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        # The target write is the only mutation in this helper.  If the
        # directory durability step fails after replace, restore the exact
        # previous bytes (or remove a newly-created target) before re-raising.
        if replaced:
            try:
                if previous_bytes is None:
                    if path.is_file() and not path.is_symlink() and path.read_bytes() == payload:
                        path.unlink()
                else:
                    restore_fd, restore_name = tempfile.mkstemp(prefix=f".{path.name}.v2.restore.", dir=path.parent)
                    restore = Path(restore_name)
                    try:
                        with os.fdopen(restore_fd, "wb") as stream:
                            stream.write(previous_bytes)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(restore, path)
                    finally:
                        restore.unlink(missing_ok=True)
            except OSError:
                # Preserve the original durability error; callers must not
                # mistake a failed rollback for a successful publication.
                pass
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _record_business_presentation_plan_v2_unlocked(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path,
    previous_plan_ref: str | Path,
    reviewer_ref: str,
    expected_fixture_sha256: str,
    expected_chart_map_sha256: str,
    expected_previous_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path | None,
) -> dict[str, Any]:
    """Create the active generation's absent V2 plan under the held run lock."""

    ensure_valid = getattr(context, "ensure_valid", None)
    if callable(ensure_valid):
        ensure_valid()
    try:
        from auto_foundry_core.lifecycle import RunLifecycle

        pointer = RunLifecycle._read_generation_pointer_unlocked(context)  # noqa: SLF001 - lock is held here
        if pointer is None:
            raise BusinessPresentationPlanError("direct V2 recording requires an active generation")
        metadata = RunLifecycle._load_generation_unlocked(context, pointer)  # noqa: SLF001 - lock is held here
    except BusinessPresentationPlanError:
        raise
    except Exception as exc:
        raise BusinessPresentationPlanError("active generation validation failed") from exc

    generation_id = _text(getattr(metadata, "generation_id", "")).strip()
    parent_generation_id = _text(getattr(metadata, "parent_generation_id", "")).strip()
    if not re.fullmatch(r"G-[0-9]{4}", generation_id) or not re.fullmatch(r"G-[0-9]{4}", parent_generation_id):
        raise BusinessPresentationPlanError("active generation lineage is invalid")
    ordinal = int(generation_id[2:])
    if int(parent_generation_id[2:]) != ordinal - 1:
        raise BusinessPresentationPlanError("active generation parent lineage is invalid")

    canonical_fixture_ref = f"products/generations/{generation_id}/dashboard/dashboard_fixture_v4.json"
    canonical_chart_map_ref = f"products/generations/{generation_id}/dashboard/dashboard_chart_map_v4.json"
    canonical_previous_ref = f"extensions/{parent_generation_id}/{PRESENTATION_PLAN_FILENAME}"
    canonical_target_ref = f"extensions/{generation_id}/{PRESENTATION_PLAN_FILENAME}"
    fixture_ref, fixture_path = _direct_v2_reference(
        context, fixture_ref, expected=canonical_fixture_ref, label="fixture"
    )
    chart_map_ref, chart_map_path = _direct_v2_reference(
        context, chart_map_ref, expected=canonical_chart_map_ref, label="chart map"
    )
    previous_plan_ref, previous_plan_path = _direct_v2_reference(
        context, previous_plan_ref, expected=canonical_previous_ref, label="predecessor plan"
    )
    target_ref = _presentation_plan_ref(context, generation_id, presentation_plan_ref)
    if target_ref != canonical_target_ref:
        raise BusinessPresentationPlanError("direct V2 target must be the active generation plan path")
    target_path = _direct_v2_target_path(context, target_ref, expected=canonical_target_ref)
    if target_path.exists() and not target_path.is_file():
        raise BusinessPresentationPlanError("direct V2 target is not a regular file")
    reviewer = _text(reviewer_ref).strip()
    if not reviewer or reviewer != _text(reviewer_ref) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", reviewer):
        raise BusinessPresentationPlanError("direct V2 reviewer_ref is invalid")
    for label, value in (
        ("fixture", expected_fixture_sha256),
        ("chart map", expected_chart_map_sha256),
        ("predecessor plan", expected_previous_plan_sha256),
    ):
        if not _is_sha256(value):
            raise BusinessPresentationPlanError(f"expected {label} hash is invalid")
    if not _is_sha256(expected_successor_plan_sha256):
        raise BusinessPresentationPlanError("expected successor plan hash is invalid")

    def _check_source(path: Path, expected_hash: str, label: str) -> bytes:
        raw = path.read_bytes()
        if _sha256_bytes(raw) != expected_hash:
            raise BusinessPresentationPlanError(f"{label} hash drifted")
        return raw

    fixture_bytes = _check_source(fixture_path, expected_fixture_sha256, "fixture")
    chart_map_bytes = _check_source(chart_map_path, expected_chart_map_sha256, "chart map")
    previous_plan_bytes = _check_source(previous_plan_path, expected_previous_plan_sha256, "predecessor plan")

    # This is a direct successor path, not the one-time V1 migration route.
    # Reject a V1 (or otherwise non-V2) predecessor before invoking the
    # builder, so callers cannot bootstrap a new generation through this API.
    try:
        previous_plan = json.loads(previous_plan_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("direct V2 predecessor plan is invalid") from exc
    if not isinstance(previous_plan, Mapping) or previous_plan.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
        raise BusinessPresentationPlanError("direct V2 predecessor must be a V2 plan")

    candidate = write_business_presentation_plan_v2(
        context,
        fixture_ref=fixture_ref,
        chart_map_ref=chart_map_ref,
        previous_plan_ref=previous_plan_ref,
        reviewer_ref=reviewer,
        presentation_plan_ref=target_ref,
        _lock_held=True,
    )
    _validate_presentation_plan_v2_shape(candidate)
    if candidate.get("generation_id") != generation_id or candidate.get("reviewer_ref") != reviewer:
        raise BusinessPresentationPlanError("direct V2 candidate lineage/reviewer binding is invalid")
    source = candidate.get("source_bindings")
    expected_source = {
        "fixture_ref": fixture_ref,
        "fixture_sha256": expected_fixture_sha256,
        "chart_map_ref": chart_map_ref,
        "chart_map_sha256": expected_chart_map_sha256,
        "previous_plan_ref": previous_plan_ref,
        "previous_plan_sha256": expected_previous_plan_sha256,
    }
    if not isinstance(source, Mapping) or any(source.get(key) != value for key, value in expected_source.items()):
        raise BusinessPresentationPlanError("direct V2 candidate source binding is invalid")
    # Recheck all immutable inputs after the expensive build and before any
    # target mutation, so a concurrent source change fails closed.
    fixture_bytes = _check_source(fixture_path, expected_fixture_sha256, "fixture")
    chart_map_bytes = _check_source(chart_map_path, expected_chart_map_sha256, "chart map")
    _check_source(previous_plan_path, expected_previous_plan_sha256, "predecessor plan")

    try:
        fixture = json.loads(fixture_bytes.decode("utf-8"))
        chart_map = json.loads(chart_map_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessPresentationPlanError("direct V2 visual source is invalid") from exc
    if not isinstance(fixture, Mapping) or not isinstance(chart_map, Mapping):
        raise BusinessPresentationPlanError("direct V2 visual source must be JSON objects")
    widgets = fixture.get("widgets")
    if not isinstance(widgets, list):
        raise BusinessPresentationPlanError("direct V2 fixture widgets are invalid")
    _validate_business_presentation_plan_v2(
        context,
        candidate,
        fixture=fixture,
        fixture_ref=fixture_ref,
        chart_map=chart_map,
        chart_map_ref=chart_map_ref,
        widgets=[widget for widget in widgets if isinstance(widget, Mapping)],
        strict_source_hash=True,
    )
    payload = _canonical_bytes(candidate)
    candidate_hash = _sha256_bytes(payload)
    if candidate_hash != expected_successor_plan_sha256:
        raise BusinessPresentationPlanError("direct V2 candidate hash does not match expected successor")
    if target_path.is_file() and not target_path.is_symlink():
        current = target_path.read_bytes()
        if current == payload:
            return dict(candidate)
        raise BusinessPresentationPlanError("existing direct V2 target conflicts with the requested candidate")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace_plan_bytes(target_path, payload)
    return dict(candidate)


def record_business_presentation_plan_v2(
    context: RunContext,
    *,
    fixture_ref: str | Path,
    chart_map_ref: str | Path,
    previous_plan_ref: str | Path,
    reviewer_ref: str,
    expected_fixture_sha256: str,
    expected_chart_map_sha256: str,
    expected_previous_plan_sha256: str,
    expected_successor_plan_sha256: str,
    presentation_plan_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Public absent-target V2 recorder with strict source CAS semantics."""

    try:
        from auto_foundry_core.lifecycle import RunLifecycle
    except ModuleNotFoundError as exc:  # pragma: no cover - package guard
        raise BusinessPresentationPlanError("RunLifecycle is unavailable for direct V2 recording") from exc
    with RunLifecycle._run_lock(context):  # noqa: SLF001 - publication boundary
        return _record_business_presentation_plan_v2_unlocked(
            context,
            fixture_ref=fixture_ref,
            chart_map_ref=chart_map_ref,
            previous_plan_ref=previous_plan_ref,
            reviewer_ref=reviewer_ref,
            expected_fixture_sha256=expected_fixture_sha256,
            expected_chart_map_sha256=expected_chart_map_sha256,
            expected_previous_plan_sha256=expected_previous_plan_sha256,
            expected_successor_plan_sha256=expected_successor_plan_sha256,
            presentation_plan_ref=presentation_plan_ref,
        )


def write_business_presentation_plan(
    context: RunContext,
    *,
    manager_entries: Sequence[Mapping[str, Any]] | None = None,
    manager_widget_ids: Sequence[str] | None = None,
    reviewer_ref: str,
    fixture_ref: str | Path,
    generation_id: str | None = None,
    presentation_plan_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically record one explicit generation-scoped manager admission."""

    inventory = business_presentation_inventory(context, fixture_ref=fixture_ref, generation_id=generation_id)
    if manager_entries is None:
        # Bare IDs are intentionally no longer a plan API.  They cannot bind
        # exact reviewed display fields and would reintroduce renderer-side
        # inference; callers must provide pointer-bound entries.
        raise BusinessPresentationPlanError("manager_entries with pointer-bound projections are required")
    if manager_widget_ids not in (None, []):
        raise BusinessPresentationPlanError("manager_widget_ids are derived from manager_entries, not accepted as input")
    requested_entries = [dict(value) for value in manager_entries if isinstance(value, Mapping)]
    if len(requested_entries) != len(manager_entries):
        raise BusinessPresentationPlanError("manager_entries must contain objects")
    requested_ids = [_text(value.get("widget_id")).strip() for value in requested_entries]
    if len(requested_ids) != len(set(requested_ids)) or any(not value for value in requested_ids):
        raise BusinessPresentationPlanError("manager_entries widget IDs must be unique and non-empty")
    # Reviewer order is part of the plan contract.  Preserve the caller's
    # deterministic sequence through validation, fixture, receipt, and site
    # metadata; do not impose lexical ordering at the product boundary.
    selected = list(requested_ids)
    by_id = {value["widget_id"]: value for value in inventory["candidates"]}
    unknown = sorted(set(selected) - set(by_id))
    if unknown:
        raise BusinessPresentationPlanError(f"manager_entries reference unknown widgets: {unknown[:5]}")
    entries_by_id = {entry["widget_id"]: entry for entry in requested_entries}
    entries: list[dict[str, Any]] = []
    for widget_id in selected:
        candidate = by_id[widget_id]
        entry = entries_by_id[widget_id]
        _presentation_entry_shape(entry)
        if candidate.get("record_id") != entry.get("record_id"):
            raise BusinessPresentationPlanError(f"manager entry record does not match inventory: {widget_id}")
        # Replace caller-supplied hashes with no implicit values: an incorrect
        # hash must fail validation rather than being silently repaired.
        if entry.get("file_sha256") != candidate.get("file_sha256") or entry.get("canonical_payload_sha256") != candidate.get("canonical_payload_sha256"):
            raise BusinessPresentationPlanError(f"manager entry hash does not match inventory: {widget_id}")
        if entry.get("requirement_id") != candidate.get("requirement_id") or entry.get("presentation_role") != candidate.get("presentation_role"):
            raise BusinessPresentationPlanError(f"manager entry identity does not match inventory: {widget_id}")
        root = {
            "payload": candidate.get("authoritative_payload", {}),
            "accepted": candidate.get("accepted_fields", {}),
        }
        for field, projection in entry["display_projection"].items():
            actual_value = _presentation_pointer_value(root, projection["pointer"])
            if not _presentation_projection_value_equal(actual_value, projection["value"]):
                raise BusinessPresentationPlanError(f"manager entry projection does not match inventory: {widget_id}:{field}")
        entries.append(entry)
    plan: dict[str, Any] = {
        "schema_version": PRESENTATION_PLAN_SCHEMA,
        "run_id": inventory["run_id"],
        "generation_id": inventory["generation_id"],
        "supervisor_plan_ref": inventory["supervisor_plan_ref"],
        "supervisor_plan_sha256": inventory["supervisor_plan_sha256"],
        "item_order": inventory["item_order"],
        "input_items": inventory["input_items"],
        "parent": inventory["parent"],
        "reviewer_ref": _text(reviewer_ref).strip(),
        "manager_widget_ids": selected,
        "manager_entries": entries,
    }
    _validate_presentation_plan_shape(plan)
    reference = _presentation_plan_ref(context, inventory["generation_id"], presentation_plan_ref)
    path = context.resolve_run_path(reference)
    payload = _canonical_bytes(plan)
    if path.is_file() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != payload:
            raise BusinessPresentationPlanError("existing presentation plan conflicts with the requested admission")
        return plan
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return plan


def _group_definitions(plan: Mapping[str, Any] | None, item_ids: Sequence[str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    assigned: set[str] = set()
    if plan and isinstance(plan.get("groups"), list):
        for index, raw in enumerate(plan["groups"], 1):
            if not isinstance(raw, Mapping):
                raise AssemblyError("requirement supervisor plan groups must be objects")
            ids = [_text(value).strip() for value in _as_list(raw.get("requirement_ids"))]
            ids = [value for value in ids if value]
            if not ids:
                continue
            if any(value in assigned for value in ids):
                raise AssemblyError("requirement supervisor plan assigns an item more than once")
            assigned.update(ids)
            groups.append({
                "id": _slug(raw.get("id") or raw.get("group_id") or f"group-{index:02d}"),
                "title": _text(raw.get("title") or raw.get("label") or raw.get("name") or raw.get("rationale") or f"Decision group {index}"),
                "order": index,
                "requirement_ids": ids,
                "summary": _text(raw.get("summary") or raw.get("rationale")),
            })
    for item_id in item_ids:
        if item_id not in assigned:
            index = len(groups) + 1
            groups.append({"id": f"group-{index:02d}", "title": item_id, "order": index, "requirement_ids": [item_id], "summary": ""})
            assigned.add(item_id)
    # A plan may mention an item that was not requested; do not silently read it.
    for group in groups:
        group["requirement_ids"] = [item_id for item_id in group["requirement_ids"] if item_id in item_ids]
    groups = [group for group in groups if group["requirement_ids"]]
    return groups


def _manager_requirement_title(
    item_id: str,
    content: Mapping[str, Any],
    original_text: str,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Choose a complete, scope-level business clause for the manager surface.

    Requirement wording is the authority for this heading.  Strip only the
    request/time boilerplate, then stop at an explicit secondary-clause cue;
    never hard-slice or ellipsize a sentence.  The complete original scope is
    carried separately in ``requirement_scope``/the technical audit.
    """

    lexical = re.sub(r"\s+", " ", _text(original_text)).strip(" \t\r\n.:;,-")
    if lexical:
        body = _SCOPE_TIME_PREFIX_RE.sub("", lexical, count=1)
        body = _SCOPE_REQUEST_PREFIX_RE.sub("", body, count=1).strip()
        cue = _SCOPE_SECONDARY_CUE_RE.search(body)
        candidate = body[:cue.start()] if cue else body
        candidate = candidate.strip(" \t\r\n,;:-.")
        while candidate:
            token = re.sub(r"[^A-Za-z]+$", "", candidate.split()[-1]).lower()
            if token not in _DANGLING_SCOPE_TOKENS:
                break
            candidate = candidate.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if (
            candidate
            and len(candidate) <= 130
            and "…" not in candidate
            and _balanced_parentheses(candidate)
        ):
            return candidate[:1].upper() + candidate[1:]

    # If the first reviewed clause is unusually long, use a supplied human
    # dashboard-fact title rather than inventing a word-boundary truncation.
    for record in records or ():
        if _text(record.get("kind")) != "dashboard_fact":
            continue
        payload = _record_payload(record)
        candidate_payload = payload.get("widget") if isinstance(payload.get("widget"), Mapping) else payload
        for key in ("title", "short_title", "label", "name"):
            candidate = _text(candidate_payload.get(key)).strip()
            if _is_manager_title_candidate(candidate, item_id):
                return candidate

    # A few legacy/manual fixtures have no original text or dashboard fact.
    # Only then may an explicit content title be used; signal labels remain out
    # of this fallback so a missing scope cannot silently become a metric
    # heading.
    for key in ("short_title", "title", "name", "label"):
        candidate = _text(content.get(key)).strip()
        if _is_manager_title_candidate(candidate, item_id):
            return candidate
    return "Reviewed decision"


_SCHEMA_TOKEN_TITLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_RAW_REQUIREMENT_TITLE_RE = re.compile(r"^REQ[-_]?\d+$", flags=re.IGNORECASE)
_SCOPE_REQUEST_PREFIX_RE = re.compile(
    r"^(?:provide\s+a\s+management\s+view\s+of|provide|show|identify|determine|evaluate)\s+",
    flags=re.IGNORECASE,
)
_SCOPE_TIME_PREFIX_RE = re.compile(
    r"^(?:as\s+of|through|at|for)\s+.*?,\s*",
    flags=re.IGNORECASE,
)
_SCOPE_SECONDARY_CUE_RE = re.compile(
    r"(?:,\s+(?:where|which|how|separating|keeping|preserving|reporting|separately)\b"
    r"|\s+and\s+(?:where|whether|which|identify)\b"
    r"|\s+to\s+identify\b"
    r"|\s+so\s+Operations\b"
    r"|\s+spanning\b)",
    flags=re.IGNORECASE,
)
_DANGLING_SCOPE_TOKENS = {
    "and", "or", "from", "through", "to", "with", "of", "for", "how",
    "where", "which", "that", "the", "a", "an", "in", "on", "by", "as",
    "into", "over", "under", "at", "vs", "versus", "whether",
}


def _balanced_parentheses(value: str) -> bool:
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _is_manager_title_candidate(candidate: str, item_id: str = "") -> bool:
    """Reject raw requirement/schema identifiers as manager titles."""

    if not candidate or len(candidate) > 130 or "\n" in candidate:
        return False
    if _RAW_REQUIREMENT_TITLE_RE.fullmatch(candidate.strip()):
        return False
    if re.fullmatch(r"[a-z][a-z0-9_]*", candidate.strip()):
        return False
    if item_id and candidate.strip().lower() == item_id.strip().lower():
        return False
    return True


def _requirement_limitations(records: Sequence[Mapping[str, Any]]) -> list[str]:
    values: list[str] = []
    for record in records:
        payload = _record_payload(record)
        kind = _text(record.get("kind"))
        candidates = []
        if kind == "limitation":
            candidates.append(payload.get("limitation"))
        candidates.extend(_as_list(payload.get("limitations")))
        for value in candidates:
            text = _text(value).strip()
            if text and text not in values:
                values.append(text)
    return values


def _manager_takeaway(content: Mapping[str, Any], scope: str) -> str:
    """Choose one exact business headline clause for the manager header."""

    for value in _as_list(content.get("headline_findings")):
        clauses = _manager_business_clauses(value, subject_context=scope)
        if clauses:
            return clauses[0]
    return ""


def _manager_limitations(content: Mapping[str, Any], records: Sequence[Mapping[str, Any]], scope: str) -> list[str]:
    """Keep only exact limitations that state a business-facing boundary."""

    values: list[str] = []
    candidates: list[Any] = [*_as_list(content.get("limitations"))]
    for record in records:
        payload = _record_payload(record)
        candidates.extend(_as_list(payload.get("limitations")))
        if _text(record.get("kind")) == "limitation":
            candidates.append(payload.get("limitation"))
    for value in candidates:
        clauses = _manager_business_clauses(value, subject_context=scope)
        for clause in clauses:
            if clause not in values:
                values.append(clause)
    return values


def _plan_binding(plan: Mapping[str, Any] | None, groups: Sequence[Mapping[str, Any]], plan_ref: str | Path | None) -> dict[str, Any]:
    """Return canonical supervisor-plan/grouping hashes for retry binding."""

    reference = Path(plan_ref or "requirement_supervisor_plan.json").as_posix()
    canonical_plan: Any = plan if plan is not None else None
    return {
        "ref": reference,
        "present": plan is not None,
        "sha256": _json_hash(canonical_plan),
        "groups_sha256": _json_hash(list(groups)),
        "group_count": len(groups),
    }


def _root_plan_binding(supervisor_plan_ref: str, supervisor_plan_hash: str) -> dict[str, Any]:
    """Return the exact root-generation supervisor-plan admission binding.

    Root product inspection binds the receipt to the authoritative supervisor
    plan *bytes*, rather than to a re-serialized JSON value.  Keep this shape
    deliberately small and exact: downstream validators use it as the CAS
    boundary for the G-0001 product.
    """

    reference = Path(supervisor_plan_ref).as_posix()
    if reference != "requirement_supervisor_plan.json" or not _is_sha256(supervisor_plan_hash):
        raise AssemblyError("root supervisor-plan binding is not canonical")
    return {
        "ref": reference,
        "sha256": supervisor_plan_hash,
        "admission_sha256": supervisor_plan_hash,
        "generation_id": "G-0001",
    }


def _record_payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else {}


# Presentation admission is intentionally conservative.  The assembler owns
# the policy so that the renderer does not need to infer business meaning from
# arbitrary labels or payload shapes.  A record is manager-facing only when its
# reviewed text names a business subject and an explicit outcome/exposure,
# action, or magnitude.  Data-quality/model mechanics veto unless a separate
# clause carries that business implication; the exact original payload remains
# in ``audit_payload`` either way.
_MANAGER_BUSINESS_SUBJECT_RE = re.compile(
    r"\b(?:order|orders|delivery|deliveries|shipment|shipments|invoice|invoices|"
    r"cash|payment|payments|refund|refunds|return|returns|support|ticket|tickets|"
    r"inventory|stock|warehouse|supplier|suppliers|vendor|vendors|customer|customers|"
    r"cost|costs|revenue|service|services|purchase|purchases|receivable|receivables|"
    r"forecast|forecasts|demand|promotion|promotions|collection|collections|"
    r"erp|wms|tms|warehouse|"
    r"late|lateness|overdue|terms|fulfil|fulfill|fulfillment|margin|expense|expenses)\b",
    flags=re.IGNORECASE,
)
_MANAGER_BUSINESS_SIGNAL_RE = re.compile(
    r"\b(?:outcome|outcomes|risk|risks|exposure|exposures|action|actions|decision|"
    r"decisions|magnitude|amount|amounts|value|values|late|lateness|overdue|"
    r"disputed|matched|paid|pending|closed|open|urgent|priority|exception|"
    r"exceptions|divergen(?:ce|ces)|shortage|shortages|delay|delays|queue|queues|"
    r"lead[- ]?time|deviation|deviations|settlement|reconcile|reconciled|"
    r"on[- ]?time|unpaid|unresolved|watchlist|watchlists|threshold|thresholds|"
    r"count|counts|total|totals|amount|amounts|percent|rate|rates|ratio|"
    r"loss|losses|margin|cost|costs|revenue|service|services|closure|closed|"
    r"status|milestone|availability|available|stock|shortfall|shortfalls|terms|"
    r"diverge|diverges|divergence|bottleneck|handoff|handoffs|recovery|"
    r"settlement|settled)\b",
    flags=re.IGNORECASE,
)
_MANAGER_TECHNICAL_VETO_RE = re.compile(
    r"\b(?:mapping|mapped|mappings|coverage|covered|source|sources|source[- ]local|"
    r"schema|schemas|row|rows|distinct|identity|identities|id|ids|identifier|identifiers|key|keys|join|joins|"
    r"namespace|namespaces|ontology|ontologies|connectivity|connected|relationship|"
    r"relationships|diagnostic|diagnostics|method|methodology|model|models|"
    r"canonical|normalization|normalisation|lineage|provenance|evidence|record|"
    r"records|field|fields|column|columns|endpoint|endpoints|edge|edges|fanout|"
    r"recheck|raw|population|populations|reference|references|parse|parsed|numeric|non[- ]?negative|"
    r"data[- ]?quality|nonpublishable|unresolved namespace|"
    # Bounded schema-field vocabulary that otherwise makes a technical
    # closure/coverage diagnostic look like a manager conclusion.  The
    # original reviewed sentence remains byte-identical in audit_payload.
    r"closed[_ ]at|case[_ ]status|source[_ ]population|source[_ ]coverage|"
    r"target[_ ]population|target[_ ]coverage|watchlist[_ ]rows|order[_ ]created[_ ]at|"
    r"promised[_ ]ship[_ ]by|qty[_ ]delta|start[_ ]date|end[_ ]date|"
    r"distinct[_ ](?:id|ids|reference|references|key|keys)|"
    r"available[_ ](?:gt|lt|eq|gte|lte)[_ ]\d+|"
    r"(?:field|row|column)[_ ](?:count|name|value|type))\b",
    flags=re.IGNORECASE,
)
_MANAGER_CLAUSE_SPLIT_RE = re.compile(r"(?:;|\n+|\s+[—–-]\s+|\.(?=\s+[A-Z]))")
_MANAGER_PURE_TECHNICAL_CONTEXT_RE = re.compile(
    r"\b(?:ontology|ontologies|master[- ]data|identity recovery|canonical identities|"
    r"identity|model stress|"
    r"semantic connectivity|relationship graph)\b",
    flags=re.IGNORECASE,
)
_MANAGER_SCHEMA_TITLE_RE = re.compile(
    r"\b(?:count|counts|coverage|distribution|status|statuses|population|populations|"
    r"reconciliation|reconcile|qty|quantity|rows?|snapshot|by|to|from|in|out|"
    r"source|field|fields|key|keys|mapping|mapped|reference|references)\b",
    flags=re.IGNORECASE,
)


def _manager_text_parts(value: Any) -> list[str]:
    """Return bounded lexical fragments from a reviewed presentation value."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key, child in value.items():
            parts.extend(_manager_text_parts(key))
            parts.extend(_manager_text_parts(child))
        return parts
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for child in value:
            parts.extend(_manager_text_parts(child))
        return parts
    text = _text(value).strip()
    return [text] if text else []


def _manager_schema_like_title(value: Any) -> bool:
    """Recognize schema/projection headings without classifying free prose."""

    title = _text(value).strip()
    if not title or not re.search(r"[_./]", title):
        return False
    semantic = re.sub(r"[_./:]+", " ", title)
    return bool(_MANAGER_SCHEMA_TITLE_RE.search(semantic))


def _manager_business_clauses(
    value: Any,
    *,
    subject_context: str = "",
    context_requires_magnitude: bool = False,
) -> list[str]:
    """Select exact reviewed clauses that are safe for a business manager.

    No values are recalculated or paraphrased.  A clause survives only when it
    contains both a business subject and an explicit business signal and does
    not contain data/model mechanics.  Splitting at reviewed punctuation lets
    a mixed claim expose its separable business sentence while retaining the
    complete original claim in the audit payload.
    """

    output: list[str] = []
    for raw in _manager_text_parts(value):
        for fragment in _MANAGER_CLAUSE_SPLIT_RE.split(raw):
            clause = re.sub(r"\s+", " ", fragment).strip(" \t\r\n,.:;-")
            if not clause:
                continue
            semantic = re.sub(r"[_./:]+", " ", clause)
            has_subject = bool(_MANAGER_BUSINESS_SUBJECT_RE.search(semantic))
            if not has_subject and subject_context:
                # A requirement scope may supply the subject for a terse
                # reviewed headline (for example ``707 physical-count pairs
                # diverge``), but method/queue prose without an explicit
                # magnitude must not inherit that subject by accident.
                has_subject = bool(_MANAGER_BUSINESS_SUBJECT_RE.search(subject_context)) and (
                    not context_requires_magnitude or bool(re.search(r"\d", clause))
                )
            if has_subject and _MANAGER_BUSINESS_SIGNAL_RE.search(semantic) and not _MANAGER_TECHNICAL_VETO_RE.search(semantic):
                if clause not in output:
                    output.append(clause)
    return output


def _manager_admission(
    item_id: str,
    widget: Mapping[str, Any],
    *,
    subject_context: str = "",
) -> dict[str, Any]:
    """Return one explicit, deterministic manager-admission classification."""

    role = _text(widget.get("presentation_role")).lower()
    if role == "relationship_matrix":
        return {
            "status": "audit_only",
            "presentation_audience": "technical_audit",
            "role": "technical_audit",
            "reasons": ["relationship matrices are retained only for technical audit"],
        }
    if _MANAGER_PURE_TECHNICAL_CONTEXT_RE.search(subject_context):
        subject_context = ""
    title = _text(widget.get("title") or widget.get("label") or widget.get("display_title")).strip()
    title_clauses = _manager_business_clauses([title], subject_context=subject_context)
    title_veto = bool(_MANAGER_TECHNICAL_VETO_RE.search(re.sub(r"[_./:]+", " ", title))) or _manager_schema_like_title(title)
    candidates: list[Any] = [title]
    if role == "finding_list":
        candidates.extend(
            finding.get("finding")
            for finding in _as_list(widget.get("manager_findings"))
            if isinstance(finding, Mapping)
        )
    else:
        candidates.extend([widget.get("manager_rows"), widget.get("rows"), widget.get("tiles"), widget.get("bars"), widget.get("categories"), widget.get("segments")])
    clauses = _manager_business_clauses(
        candidates,
        subject_context=subject_context,
        context_requires_magnitude=role == "finding_list",
    )
    # A fact/table title is the reviewed semantic admission boundary.  Do not
    # let a row label such as ``WMS stock SKU`` turn a source/mapping/ontology
    # card into a business conclusion.  Finding lists and the aggregated
    # ``Key signals`` strip are the only composite roles whose rows are allowed
    # to carry a separately reviewed business clause.
    if role not in {"finding_list"} and title != "Key signals" and (title_veto or not title_clauses):
        clauses = []
    # A compact scalar metric can have a business title but no repeated row
    # labels; the title/value remains the supplied reviewed signal.
    if not clauses and role in {"finding_list"}:
        title = _text(widget.get("title") or widget.get("label"))
        payload = widget.get("audit_payload")
        clauses = _manager_business_clauses(
            [title, payload],
            subject_context=subject_context,
            context_requires_magnitude=role == "finding_list",
        )
    admitted = bool(clauses)
    # Keep a technical veto visible in the reason even when another exact
    # clause was admitted; this is useful for the manifest and audit review.
    all_text = " ".join(_manager_text_parts(candidates)).lower()
    veto = bool(_MANAGER_TECHNICAL_VETO_RE.search(all_text))
    reasons: list[str] = []
    if admitted:
        reasons.append("explicit reviewed business subject and outcome/exposure/action/magnitude")
        if veto:
            reasons.append("technical clauses excluded from the manager projection")
    else:
        reasons.append("no explicit reviewed business implication; retained for technical audit")
    return {
        "status": "admitted" if admitted else "audit_only",
        "presentation_audience": "business_manager" if admitted else "technical_audit",
        "role": "business_outcome" if admitted else "technical_audit",
        "reasons": reasons,
        "business_clauses": clauses,
    }


def _manager_row_is_technical(row: Mapping[str, Any], *, metric_tile: bool = False) -> bool:
    # KPI tiles carry useful denominator/unit/period metadata; those field
    # names are not themselves business conclusions and must not cause the
    # whole scalar tile to disappear.  Table rows, by contrast, treat those
    # labels as audit mechanics and filter them from the manager projection.
    if metric_tile:
        metadata_keys = {
            "value", "display_value", "denominator", "denominator_value", "numerator",
            "population", "unit", "units", "period", "as_of", "date_authority",
            "size", "share", "percent", "rate",
        }
        parts: list[Any] = []
        for key, value in row.items():
            if _text(key).strip().lower() in metadata_keys:
                continue
            parts.extend(_manager_text_parts(key))
            parts.extend(_manager_text_parts(value))
    else:
        parts = _manager_text_parts(row)
    semantic = re.sub(r"[_./:]+", " ", " ".join(parts))
    return bool(_MANAGER_TECHNICAL_VETO_RE.search(semantic))


def _filter_manager_projection(widget: dict[str, Any]) -> None:
    """Remove raw/schema rows from an admitted table without touching audit."""

    kind = _text(widget.get("type") or widget.get("kind")).lower()
    # A reviewed dashboard_fact already has an explicit, lossless projection
    # built by ``_structured_fact_projection``.  Keep that projection intact
    # while the planless candidate is inventoried; plan membership (not this
    # lexical legacy filter) decides whether it is visible later.  Legacy G3
    # status tables continue through the existing sanitation path.
    if widget.get("dashboard_fact") is True and kind in {"bar", "column", "scatter", "table"}:
        return
    if kind in {"table", "status_table"}:
        for key in ("manager_rows", "rows"):
            raw = widget.get(key)
            if isinstance(raw, list):
                widget[key] = [row for row in raw if isinstance(row, Mapping) and not _manager_row_is_technical(row)]


def _apply_manager_admission(widgets: list[dict[str, Any]], *, subject_context: str = "") -> None:
    """Attach admission metadata and fail closed for non-business widgets."""

    for widget in widgets:
        _filter_manager_projection(widget)
        admission = _manager_admission(_text(widget.get("requirement_id")), widget, subject_context=subject_context)
        kind = _text(widget.get("type") or widget.get("kind")).lower()
        if kind in {"table", "status_table"}:
            # A table/grid whose manager projection became empty after the
            # technical-row filter is audit-only.  This prevents a blank
            # manager card while preserving the exact raw payload below.
            projection_keys = ("manager_rows", "rows", "data")
            supplied_projection = any(isinstance(widget.get(key), list) for key in projection_keys)
            has_projection = any(
                isinstance(widget.get(key), list)
                and any(isinstance(row, Mapping) for row in widget.get(key, []))
                for key in projection_keys
            )
            if supplied_projection and not has_projection:
                admission = dict(admission)
                admission["status"] = "audit_only"
                admission["presentation_audience"] = "technical_audit"
                admission["role"] = "technical_audit"
                admission.setdefault("reasons", []).append("manager projection contains no business rows")
        if _text(widget.get("presentation_role")).lower() == "finding_list":
            safe_findings: list[dict[str, Any]] = []
            for finding in _as_list(widget.get("manager_findings")):
                if not isinstance(finding, Mapping):
                    continue
                for clause in _manager_business_clauses(
                    finding.get("finding"),
                    subject_context=subject_context,
                    context_requires_magnitude=True,
                ):
                    entry = dict(finding)
                    entry["finding"] = clause
                    safe_findings.append(entry)
            widget["manager_findings"] = safe_findings
            if safe_findings:
                admission = dict(admission)
                admission["status"] = "admitted"
                admission["presentation_audience"] = "business_manager"
                admission["role"] = "business_outcome"
                admission["business_clauses"] = [entry["finding"] for entry in safe_findings]
            else:
                admission = dict(admission)
                admission["status"] = "audit_only"
                admission["presentation_audience"] = "technical_audit"
                admission["role"] = "technical_audit"
        widget["manager_admission"] = admission
        widget["presentation_audience"] = admission["presentation_audience"]
        if admission["status"] != "admitted":
            widget["presentation_tier"] = "audit"
            if widget.get("presentation_role") == "decision_view" and not widget.get("_legacy_presentation_role"):
                widget["presentation_role"] = "support_metric"
        elif widget.get("presentation_tier") is None:
            widget["presentation_tier"] = "primary"


def _apply_explicit_manager_admission(
    widgets: list[dict[str, Any]],
    manager_widget_ids: Sequence[str],
    manager_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Bind manager visibility to one persisted plan, never to lexical rules."""

    for widget in widgets:
        widget_id = _text(widget.get("id")).strip()
        admitted = widget_id in manager_widget_ids
        role = _text(widget.get("presentation_role") or "decision_view")
        widget["manager_admission"] = {
            "status": "admitted" if admitted else "audit_only",
            "presentation_audience": "business_manager" if admitted else "technical_audit",
            "policy": "explicit_business_presentation_plan",
            "role": role if admitted else "technical_audit",
            "plan_membership": admitted,
        }
        widget["presentation_audience"] = "business_manager" if admitted else "technical_audit"
        widget["presentation_tier"] = "primary" if admitted else "audit"
        if admitted:
            if not isinstance(manager_entries, Mapping) or widget_id not in manager_entries:
                raise BusinessPresentationPlanError(f"manager widget has no pointer-bound plan entry: {widget_id}")
            widget["manager_presentation"] = copy.deepcopy(dict(manager_entries[widget_id]))
        else:
            widget.pop("manager_presentation", None)


def _humanize_label(value: Any) -> str:
    """Turn a reviewed field label into a short presentation label.

    This is deliberately lexical only.  It never maps a field to a different
    business concept, changes a value, or interprets an identifier.  The raw
    key remains available in the audit payload/trace projection.
    """

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", _text(value)).strip()
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Reviewed value"
    words: list[str] = []
    for word in text.split(" "):
        if word.isupper() or any(char.isdigit() for char in word):
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def _endpoint_label(value: Any) -> str:
    """Create a human endpoint label without exposing a source path."""

    text = _text(value).strip()
    if "::" in text:
        text = text.split("::", 1)[0]
    text = text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    text = re.sub(r"\.(?:csv|jsonl?|xlsx?|parquet)$", "", text, flags=re.IGNORECASE)
    return _humanize_label(text)


def _manager_rows_from_sequence(values: Any, *, group_label: str = "") -> list[dict[str, Any]]:
    """Project explicitly supplied row records into business-facing columns.

    Raw field/path/value_json/evidence/path data is intentionally omitted from
    this presentation projection; the exact candidate remains in
    ``audit_payload``.  Only scalar reviewed fields are copied.
    """

    rows: list[dict[str, Any]] = []
    technical = {
        "path", "field", "source_field", "row_kind", "value_json", "evidence_ref",
        "evidence_refs", "record_id", "record_hash", "accepted_content_hash",
        "integration_record_id", "integration_record_ids", "relationship_id",
        "analysis_relationship_id", "audit_id", "join_keys", "owner_ref",
    }
    for index, raw in enumerate(_as_list(values), 1):
        if isinstance(raw, Mapping):
            row: dict[str, Any] = {}
            if group_label:
                row["view"] = _humanize_label(group_label)
            for key, value in raw.items():
                key_text = _text(key)
                if key_text in technical or isinstance(value, (Mapping, list, tuple)):
                    continue
                if value is None:
                    continue
                row[_humanize_label(key_text)] = value
            if row:
                rows.append(row)
        elif raw is not None:
            rows.append({"Label": f"{_humanize_label(group_label) if group_label else 'Reviewed value'} {index}", "Value": raw})
    return rows


def _manager_rows_from_mapping(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project a supplied mapping into compact, deterministic business rows."""

    rows: list[dict[str, Any]] = []
    for key, value in sorted(values.items(), key=lambda pair: _text(pair[0])):
        label = _humanize_label(key)
        if isinstance(value, list):
            nested = _manager_rows_from_sequence(value, group_label=label)
            rows.extend(nested)
        elif isinstance(value, Mapping):
            nested = _manager_rows_from_sequence([value], group_label=label)
            if nested:
                rows.extend(nested)
        elif value is not None:
            rows.append({"Label": label, "Value": value})
    return rows


# Dashboard facts are reviewed visual contracts, rather than generic
# ``field/value`` records.  Keep their projection deliberately separate from
# the legacy status-table path above: the latter is part of the inherited G3
# byte contract, while new facts carry explicit grouping/measure/series/point
# semantics that must not be reduced to the first scalar in a row.
_FACT_TECHNICAL_KEYS = frozenset({
    "fact_id", "visual_id", "outcome_chart_id", "source_ref", "source_refs",
    "evidence_ref", "evidence_refs", "record_id", "record_ids", "record_hash",
    "integration_record_id", "integration_record_ids", "accepted_content_hash",
    "accepted_manifest_hash", "trace_ref", "trace_refs", "path", "paths",
    "field", "fields", "row_kind", "value_json", "data_ref", "item_id",
    "requirement_id", "relationship_id", "join_keys", "owner_ref",
})
_FACT_CONTROL_KEYS = frozenset({
    "source_local", "review_status", "comparability", "facet_scale", "scale",
    "ordering", "faceting", "period_authority", "date_authority", "date_period",
    "population_period", "snapshot_range", "full_ledger_audit", "supporting_stage",
    "stage", "business_area", "chart_contract", "notes", "sample_policy",
    "sample_rows", "exact_values", "invalid_count", "status_conflict_count",
})
_FACT_AUDIT_NUMERIC_KEYS = frozenset({
    "stock_rows", "forecast_rows", "ticket_rows", "delivery_rows", "line_count",
    "order_count", "invoice_count", "po_count", "row_count", "valid_quantity_rows",
    "invalid_quantity_rows", "matched_refund_rows", "pending_refund_rows",
    "pending_refund_row_count", "refund_row_count", "invalid_dates", "invalid_week_dates",
    "invalid_opened_dates", "invalid_start_dates", "missing_order_total_lines",
    "confirmed_date_count", "requested_date_count", "in_transit_missing_actual_count",
    "missing_actual_count", "missing_planned_with_valid_actual_count",
})
_FACT_IMPORTANT_COUNT_KEYS = frozenset({
    "late_count", "on_time_count", "open_or_unresolved_rows", "high_priority_rows",
    "open_or_unresolved_count", "unresolved_ticket_count", "late_or_open_count",
})
_FACT_DIMENSION_ORDER = (
    "category", "product_category", "campaign_name", "campaign_code", "channel",
    "sales_channel", "currency", "return_reason", "reason", "issue_category",
    "priority", "carrier", "service_level", "origin_location", "destination_city",
    "destination_country", "warehouse", "vendor", "vendor_no", "customer_segment",
)
_FACT_CONTROL_VALUE_RE = re.compile(
    r"^(?:unavailable(?:[_ ]in[_ ]source)?|independent[_ ]per[_ ]currency|"
    r"native[_ ]currency[_ ]facets?|no[_ ](?:fx|pooling)|not[_ ](?:applicable|available))$",
    flags=re.IGNORECASE,
)
_FACT_IDENTIFIER_RE = re.compile(r"^(?P<prefix>LOC-WH|DP-WH|WH|VEND)-(?P<number>[A-Za-z0-9]+)$", flags=re.IGNORECASE)
_FACT_IDENTIFIER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<legacy>OLD-)?(?P<prefix>LOC-WH|DP-WH|WH|VEND)-(?P<number>[A-Za-z0-9]+)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_FACT_NUMERIC_TEXT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?$")


def _fact_scalar(value: Any) -> bool:
    return value is not None and not isinstance(value, (Mapping, list, tuple))


def _fact_numeric(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return bool(_FACT_NUMERIC_TEXT_RE.fullmatch(_text(value).strip()))


def _fact_control_value(value: Any) -> bool:
    return bool(_FACT_CONTROL_VALUE_RE.fullmatch(_text(value).strip().replace("-", "_")))


def _fact_identifier_label(value: Any) -> str:
    """Humanize source identifiers without changing their bound identity."""

    text = _text(value).strip()
    def replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix").upper()
        number = match.group("number")
        legacy = bool(match.group("legacy"))
        if prefix == "VEND":
            return f"{'Legacy ' if legacy else ''}Supplier {number}"
        if prefix == "DP-WH":
            return f"{'Legacy ' if legacy else ''}Planning warehouse {number}"
        return f"{'Legacy ' if legacy else ''}Warehouse {number}"

    # Apply the same deterministic mapping to a reviewed composite group
    # label (for example ``USD · OLD-VEND-000002``), not just to a scalar
    # dimension.  The exact source token remains in audit payloads; manager
    # rows retain the stable identity in a readable form.
    return _FACT_IDENTIFIER_TOKEN_RE.sub(replace, text)


def _fact_visible_value(key: Any, value: Any) -> Any:
    """Return a manager-safe copy of one fact scalar, or ``None`` to omit."""

    key_text = re.sub(r"[^a-z0-9]+", "_", _text(key).strip().lower()).strip("_")
    if key_text in _FACT_TECHNICAL_KEYS or key_text in _FACT_CONTROL_KEYS:
        return None
    if not _fact_scalar(value) or _fact_control_value(value):
        return None
    if isinstance(value, str):
        return _fact_identifier_label(value)
    return value


def _fact_key_label(key: Any) -> str:
    return _humanize_label(key)


def _fact_group_keys(candidate: Mapping[str, Any], row: Mapping[str, Any]) -> list[str]:
    """Resolve explicit grouping names to exact row keys, in reviewed order."""

    keys = [key for key in row if _fact_visible_value(key, row.get(key)) is not None]
    normalized = {re.sub(r"[^a-z0-9]+", "_", _text(key).lower()).strip("_"): key for key in keys}
    grouping = _text(candidate.get("grouping")).strip()
    requested: list[str] = []
    if grouping:
        for token in re.split(r"\s*(?:x|×|,|/|\+|;| and )\s*", grouping, flags=re.IGNORECASE):
            token_key = re.sub(r"[^a-z0-9]+", "_", token.lower()).strip("_")
            if token_key and token_key in normalized:
                requested.append(normalized[token_key])
    for key in _FACT_DIMENSION_ORDER:
        if key in normalized and normalized[key] not in requested:
            requested.append(normalized[key])
    # A supplied label/name is already a reviewed group label and wins over
    # mechanically joined dimensions.  It is not a source identifier.
    for key in ("label", "name"):
        if key in normalized and normalized[key] not in requested:
            requested.insert(0, normalized[key])
    return requested


def _fact_measure_keys(candidate: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Find explicitly named measure/series fields without choosing a first scalar."""

    available = {key for row in rows for key in row if _fact_visible_value(key, row.get(key)) is not None}
    explicit = _text(candidate.get("measure")).strip()
    if explicit and explicit in available:
        return [explicit]
    x_key = _text(candidate.get("x")).strip()
    y_key = _text(candidate.get("y")).strip()
    if y_key and y_key in available:
        return [y_key]
    named: list[str] = []
    series = candidate.get("series")
    for item in _as_list(series):
        if isinstance(item, str) and item in available and item not in named:
            named.append(item)
        elif isinstance(item, Mapping):
            field = _text(item.get("field") or item.get("name") or item.get("key")).strip()
            if field in available and field not in named:
                named.append(field)
    # REQ22's accepted chart contract names both exact quantities.  The fields
    # are copied as supplied; no delta/ratio is calculated here.
    contract = _text(candidate.get("chart_contract"))
    for field in ("ordered_quantity", "shipped_quantity"):
        if field in contract and field in available and field not in named:
            named.append(field)
    if named:
        return named
    if "value" in available:
        return ["value"]
    if "display_value" in available:
        return ["display_value"]
    # Some reviewed facts omit a top-level measure but explicitly expose
    # multiple numeric row fields.  Preserve those named fields as a series;
    # audit/count mechanics are excluded while business measures remain exact.
    numeric: list[str] = []
    for key in available:
        values = [row.get(key) for row in rows]
        if not any(_fact_numeric(value) for value in values):
            continue
        normalized = re.sub(r"[^a-z0-9]+", "_", _text(key).lower()).strip("_")
        if normalized in _FACT_AUDIT_NUMERIC_KEYS or normalized.endswith("_count") or normalized.endswith("_rows"):
            if normalized not in _FACT_IMPORTANT_COUNT_KEYS:
                continue
        numeric.append(key)
    return sorted(numeric)


def _fact_group_label(candidate: Mapping[str, Any], row: Mapping[str, Any], index: int) -> str:
    values: list[str] = []
    for key in _fact_group_keys(candidate, row):
        value = _fact_visible_value(key, row.get(key))
        if value is None:
            continue
        text = _text(value).strip()
        if text and text not in values:
            values.append(text)
    if values:
        return " · ".join(values)
    return f"Reviewed row {index}"


def _fact_chart_rows(candidate: Mapping[str, Any], raw_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    measures = _fact_measure_keys(candidate, raw_rows)
    descriptors = {
        measure: _fact_scale_descriptor(candidate, measure, raw_rows)
        for measure in measures
    }
    if len(measures) > 1 and any(descriptor.get("scale_unknown") for descriptor in descriptors.values()):
        unknown = [measure for measure, descriptor in descriptors.items() if descriptor.get("scale_unknown")]
        raise AssemblyError(
            "dashboard fact mixes numeric series without an accepted scale classification: "
            + ", ".join(unknown)
        )
    geometry_facet = _fact_geometry_facet_dimension(candidate)
    if not geometry_facet and any(
        _fact_currency_value(raw.get("currency")) is not None
        for raw in raw_rows
        if isinstance(raw, Mapping)
    ):
        geometry_facet = "currency"
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, 1):
        row: dict[str, Any] = {}
        supplied_label = _fact_visible_value("label", raw.get("label")) or _fact_visible_value("name", raw.get("name"))
        row["label"] = _text(supplied_label).strip() if supplied_label is not None else _fact_group_label(candidate, raw, index)
        series: list[dict[str, Any]] = []
        for measure in measures:
            value = _fact_visible_value(measure, raw.get(measure))
            if value is None:
                continue
            series.append({
                "label": _fact_key_label(measure),
                "value": value,
                **descriptors[measure],
            })
        if len(series) == 1:
            row["value"] = series[0]["value"]
            for key in (
                "scale_group", "scale_domain", "scale_basis",
                "scale_facet_dimension", "scale_fixed_max", "scale_unknown",
            ):
                if key in series[0]:
                    row[key] = series[0][key]
            series = []
        elif len(series) > 1:
            row["series"] = series
        # Explicit bars may already carry a display value but no named source
        # measure.  Preserve that exact accepted value rather than replacing it.
        if not series:
            value = _fact_visible_value("value", raw.get("value"))
            if value is None:
                value = _fact_visible_value("display_value", raw.get("display_value"))
            if value is not None:
                row["value"] = value
        # Geometry is presentation metadata, not a visible business field.
        # Keep the exact accepted facet value alongside the projected row so
        # independent-per-currency scales can be applied without parsing a
        # human label or re-reading unbound audit payload fields later.
        if geometry_facet:
            facet_value = _fact_visible_value(geometry_facet, raw.get(geometry_facet))
            if facet_value is not None:
                row["_geometry_facet"] = _text(facet_value)
        if len(row) > 1:
            output.append(row)
    return output


def _fact_geometry_facet_dimension(candidate: Mapping[str, Any]) -> str:
    """Return an explicitly reviewed facet field used for chart scaling.

    A facet is used only when the accepted fact names an independent scale
    policy (or an explicit facet dimension).  We never infer a currency
    partition merely because a row happens to contain a currency-looking
    value; absent policy means one scale for the whole chart.
    """

    policy_values: list[Any] = [candidate.get("facet_scale"), candidate.get("scale")]
    faceting = candidate.get("faceting")
    if isinstance(faceting, Mapping):
        policy_values.extend([faceting.get("scale"), faceting.get("dimension")])
    elif faceting is not None:
        policy_values.append(faceting)
    independent = any(
        "independent_per_currency" in _text(value).strip().lower().replace(" ", "_")
        for value in policy_values
        if value is not None
    )
    explicit_dimension = _text(candidate.get("facet_by")).strip()
    if isinstance(faceting, Mapping):
        explicit_dimension = _text(faceting.get("dimension") or explicit_dimension).strip()
    if independent:
        return explicit_dimension or "currency"
    # A named facet without an independent scale still scopes the chart's
    # geometry to that accepted facet, rather than inventing a new grouping.
    return explicit_dimension if explicit_dimension else ""


def _fact_geometry_number(value: Any) -> float | None:
    """Parse one supplied numeric measure for presentation-only geometry."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        text = _text(value).strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        if not _FACT_NUMERIC_TEXT_RE.fullmatch(text):
            return None
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            return None
    return parsed if math.isfinite(parsed) else None


def _fact_currency_value(value: Any) -> str | None:
    """Return one usable source currency facet, excluding control values."""

    text = _text(value).strip()
    if not text or _fact_control_value(text):
        return None
    return text


def _fact_scale_descriptor(
    candidate: Mapping[str, Any],
    measure: str,
    raw_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify one reviewed measure for presentation-only scaling.

    The classifier is intentionally lexical only over the accepted field name
    and payload metadata.  It never derives a business metric or combines
    unlike units.  A lone unknown numeric field may use a chart-local scale;
    an unknown field in a mixed series fails closed because comparability is
    not reviewable from the payload.
    """

    normalized = re.sub(r"[^a-z0-9]+", "_", _text(measure).strip().lower()).strip("_")
    tokens = set(filter(None, normalized.split("_")))
    values = [
        _fact_geometry_number(row.get(measure))
        for row in raw_rows
        if isinstance(row, Mapping) and row.get(measure) is not None
    ]
    numeric_values = [value for value in values if value is not None]
    facet_dimension = _fact_geometry_facet_dimension(candidate)
    if not facet_dimension and any(
        _fact_currency_value(row.get("currency")) is not None
        for row in raw_rows
        if isinstance(row, Mapping)
    ):
        # Monetary source-local fields remain currency-local even when an
        # older accepted fact omitted an explicit facet_scale declaration.
        facet_dimension = "currency"

    is_count_name = bool(
        "count" in tokens
        or "rows" in tokens
        or "population" in tokens
        or "denominator" in tokens
        or normalized.endswith("_records")
        or normalized.endswith("_tickets")
        or normalized.endswith("_shipments")
    )
    is_rate = bool(
        {"rate", "ratio", "share", "fraction", "percent", "percentage"}.intersection(tokens)
        and not is_count_name
    )
    if is_rate:
        # Only an accepted fractional [0, 1] rate is safe to normalize to a
        # fixed domain.  Percent-like values such as ``95%`` are not silently
        # interpreted as fractions and therefore fail closed.
        if not numeric_values or any(value < 0 or value > 1 for value in numeric_values):
            raise AssemblyError(f"dashboard fact rate has no accepted fractional 0..1 domain: {measure}")
        return {
            "scale_group": "rate",
            "scale_domain": "0..1",
            "scale_basis": "accepted fractional rate domain",
            "scale_facet_dimension": facet_dimension,
            "scale_fixed_max": 1.0,
        }

    is_amount = bool(
        normalized.endswith("_source_local")
        and {"amount", "value", "cost", "price", "total", "paid", "outstanding", "line", "freight", "po"}.intersection(tokens)
    ) or bool({"amount", "cost", "price", "value"}.intersection(tokens) and "currency" in normalized)
    if is_amount:
        return {
            "scale_group": "amount",
            "scale_domain": "currency-local" if facet_dimension == "currency" else "amount-local",
            "scale_basis": "accepted source-local monetary measure"
            + ("; currency-local facet" if facet_dimension == "currency" else ""),
            "scale_facet_dimension": facet_dimension if facet_dimension == "currency" else "",
            "scale_fixed_max": None,
        }

    is_quantity = bool(
        {"qty", "quantity", "ordered", "shipped", "received", "reserved", "available", "damaged", "inbound", "on_hand", "stock"}.intersection(tokens)
        or any(
            marker in normalized
            for marker in ("on_hand", "in_transit", "qty", "quantity", "ordered", "shipped", "received", "reserved", "available", "damaged", "inbound", "stock")
        )
    )
    if is_quantity:
        return {
            "scale_group": "quantity",
            "scale_domain": "quantity-local",
            "scale_basis": "accepted quantity measure",
            "scale_facet_dimension": facet_dimension,
            "scale_fixed_max": None,
        }

    if is_count_name:
        return {
            "scale_group": "count",
            "scale_domain": "count-local",
            "scale_basis": "accepted count measure",
            "scale_facet_dimension": facet_dimension,
            "scale_fixed_max": None,
        }

    return {
        "scale_group": "chart",
        "scale_domain": "chart-local",
        "scale_basis": "single supplied numeric field; chart-local scale",
        "scale_facet_dimension": facet_dimension,
        "scale_fixed_max": None,
        "scale_unknown": True,
    }


def _fact_geometry_rows(candidate: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Attach deterministic geometry to explicit bar/column fact rows.

    This computes no business metric: every ratio below is a CSS presentation
    size derived from a supplied numeric value.  A chart uses one shared max;
    an explicitly independent-per-currency contract uses one max per bound
    currency facet.  Multi-series rows share that same applicable max so the
    series remain comparable.  The original scalar values are untouched.
    """

    facet_dimension = _fact_geometry_facet_dimension(candidate)
    entries: list[tuple[dict[str, Any], list[tuple[dict[str, Any], float, dict[str, Any]]], str]] = []
    for raw in rows:
        row = dict(raw)
        numeric_entries: list[tuple[dict[str, Any], float, dict[str, Any]]] = []
        series = row.get("series")
        if isinstance(series, list):
            for item in series:
                if not isinstance(item, Mapping):
                    continue
                parsed = _fact_geometry_number(item.get("value"))
                if parsed is not None:
                    numeric_entries.append((dict(item), parsed, dict(item)))
        else:
            parsed = _fact_geometry_number(row.get("value"))
            if parsed is not None:
                numeric_entries.append((row, parsed, dict(row)))
        facet = _text(row.get("_geometry_facet")) or "__chart__"
        entries.append((row, numeric_entries, facet or "__chart__"))

    maxima: dict[tuple[str, str], float] = {}
    for _row, numeric_entries, facet in entries:
        for _item, value, descriptor in numeric_entries:
            group = _text(descriptor.get("scale_group") or "chart")
            descriptor_facet = (
                facet
                if _text(descriptor.get("scale_facet_dimension")) and facet != "__chart__"
                else "__chart__"
            )
            key = (group, descriptor_facet)
            maximum = maxima.get(key, 0.0)
            fixed_max = descriptor.get("scale_fixed_max")
            if isinstance(fixed_max, (int, float)):
                maximum = max(maximum, float(fixed_max))
            maximum = max(maximum, abs(value))
            maxima[key] = maximum

    geometry_basis = "independent facet max normalization" if facet_dimension else "chart max normalization"
    for row, numeric_entries, facet in entries:
        if isinstance(row.get("series"), list):
            projected_series: list[dict[str, Any]] = []
            # Preserve every projected series item, including a non-numeric
            # reviewed value; only numeric items receive a drawable size.
            by_identity = {id(item): item for item in row["series"] if isinstance(item, Mapping)}
            for original in row["series"]:
                item = dict(original) if isinstance(original, Mapping) else {"value": original}
                parsed = _fact_geometry_number(item.get("value"))
                if parsed is not None:
                    descriptor = item
                    group = _text(descriptor.get("scale_group") or "chart")
                    descriptor_facet = (
                        facet
                        if _text(descriptor.get("scale_facet_dimension")) and facet != "__chart__"
                        else "__chart__"
                    )
                    maximum = maxima.get((group, descriptor_facet), 0.0)
                    ratio = abs(parsed) / maximum * 100.0 if maximum else 0.0
                    item["size"] = _format_geometry(ratio)
                    item["scale_facet"] = facet if descriptor_facet != "__chart__" else None
                    item.pop("scale_facet_dimension", None)
                    item.pop("scale_fixed_max", None)
                    item.pop("scale_unknown", None)
                    if parsed < 0:
                        item["signed_size"] = _format_geometry(-ratio)
                    elif "signed_size" in item:
                        item["signed_size"] = _format_geometry(ratio)
                projected_series.append(item)
            row["series"] = projected_series
            # The row itself is geometric when at least one explicit series
            # is geometric; the renderer will draw each supplied series track.
            row["geometry_basis"] = geometry_basis
        else:
            parsed = _fact_geometry_number(row.get("value"))
            if parsed is not None:
                descriptor = row
                group = _text(descriptor.get("scale_group") or "chart")
                descriptor_facet = (
                    facet
                    if _text(descriptor.get("scale_facet_dimension")) and facet != "__chart__"
                    else "__chart__"
                )
                maximum = maxima.get((group, descriptor_facet), 0.0)
                ratio = abs(parsed) / maximum * 100.0 if maximum else 0.0
                row["size"] = _format_geometry(ratio)
                row["scale_facet"] = facet if descriptor_facet != "__chart__" else None
                row.pop("scale_facet_dimension", None)
                row.pop("scale_fixed_max", None)
                row.pop("scale_unknown", None)
                if parsed < 0:
                    row["signed_size"] = _format_geometry(-ratio)
                elif "signed_size" in row:
                    row["signed_size"] = _format_geometry(ratio)
                row["geometry_basis"] = geometry_basis
    return [dict(row) for row, _numeric_entries, _facet in entries]


def _fact_table_rows(raw_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in raw_rows:
        row: dict[str, Any] = {}
        for key, value in raw.items():
            visible = _fact_visible_value(key, value)
            if visible is None:
                continue
            row[_fact_key_label(key)] = visible
        if row:
            output.append(row)
    return output


def _fact_scatter_rows(candidate: Mapping[str, Any], raw_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    x_key = _text(candidate.get("x")).strip()
    y_key = _text(candidate.get("y")).strip()
    if not x_key or not y_key:
        return []
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, 1):
        x_value = _fact_visible_value(x_key, raw.get(x_key))
        y_value = _fact_visible_value(y_key, raw.get(y_key))
        if x_value is None or y_value is None:
            continue
        output.append({"label": _fact_group_label(candidate, raw, index), "x": x_value, "y": y_value})
    return output


def _structured_fact_projection(candidate: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    raw_type = _text(candidate.get("type")).strip().lower()
    output_type = raw_type if raw_type else "table"
    raw_rows_value = None
    for key in ("rows", "data", "bars", "points", "cells"):
        if isinstance(candidate.get(key), list) and candidate.get(key):
            raw_rows_value = candidate.get(key)
            break
    raw_rows = [row for row in _as_list(raw_rows_value) if isinstance(row, Mapping)]
    if output_type == "scatter":
        return output_type, _fact_scatter_rows(candidate, raw_rows)
    if output_type in {"table", "status_table"}:
        return output_type, _fact_table_rows(raw_rows)
    chart_rows = _fact_chart_rows(candidate, raw_rows)
    if output_type in {"bar", "column"}:
        chart_rows = _fact_geometry_rows(candidate, chart_rows)
    return output_type, chart_rows


def _manager_fact_projection(candidate: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]] | list[dict[str, Any]]]:
    """Select a manager-safe view from explicit dashboard-fact fields only."""

    # A reviewed dashboard fact already carries its visual contract.  Keep
    # that type when projecting the supplied rows; the previous implementation
    # routed every non-KPI fact through ``status_table`` and thereby discarded
    # accepted bar/column/scatter geometry.  The projection below remains
    # presentation-only (labels are humanized, values are copied), while the
    # untouched candidate is retained in ``audit_payload`` by ``_fact_widget``.
    raw_type = _text(candidate.get("type")).strip().lower()
    supported_type = {
        "bar", "column", "lollipop", "donut", "waffle", "progress",
        "scatter", "leaderboard", "diverging_bar", "stacked_composition",
        "line", "heatmap", "table", "status_table", "metric_grid", "kpi_grid",
    }
    output_type = raw_type if raw_type in supported_type else "status_table"

    # New reviewed dashboard facts carry a real visual contract.  Resolve the
    # contract before any generic status-table fallback so grouping, measure,
    # multi-series, scatter points, and table rows survive intact.  Legacy G3
    # status_table/kpi_grid facts intentionally remain on the older branch to
    # preserve their inherited fixture/hash contract byte-for-byte.
    if raw_type in {"bar", "column", "scatter", "table"}:
        return _structured_fact_projection(candidate)

    metrics = candidate.get("metrics")
    if isinstance(metrics, Mapping):
        metrics = [{"label": key, "value": value} for key, value in metrics.items()]
    if isinstance(metrics, list) and metrics:
        tiles: list[dict[str, Any]] = []
        for index, metric in enumerate(metrics, 1):
            if not isinstance(metric, Mapping) or metric.get("value") is None:
                continue
            label = _humanize_label(metric.get("label") or metric.get("name") or f"Metric {index}")
            denominator = metric.get("denominator")
            display = metric.get("value")
            if denominator is not None and metric.get("value") is not None:
                display = f"{_text(metric.get('value'))} of {_text(denominator)}"
            tile = {"label": label, "value": display, "raw_value": metric.get("value")}
            if denominator is not None:
                tile["denominator"] = denominator
            if metric.get("unit") not in (None, ""):
                tile["unit"] = metric.get("unit")
            tiles.append(tile)
        if tiles:
            return ("kpi_grid" if output_type in {"status_table", "kpi", "kpi_grid", "metric_grid"} else output_type), tiles

    for key in ("rows", "data", "series", "bars", "categories", "segments", "points", "cells", "tiles"):
        rows = _manager_rows_from_sequence(candidate.get(key))
        if rows:
            return output_type, rows

    values = candidate.get("values")
    if isinstance(values, Mapping):
        rows = _manager_rows_from_mapping(values)
        if rows:
            return output_type, rows

    for key in ("targets", "edges", "nodes"):
        rows = _manager_rows_from_sequence(candidate.get(key), group_label=key)
        if rows:
            return output_type, rows

    scalar_rows: list[dict[str, Any]] = []
    for key in ("count", "returned", "limit", "queue_count", "status"):
        if candidate.get(key) is not None:
            scalar_rows.append({"Label": _humanize_label(key), "Value": candidate.get(key)})
    return output_type, scalar_rows or [{"Label": "Reviewed detail", "Value": "See technical audit"}]


def _decorate_manager_fact_rows(
    candidate: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Add only mechanical chart labels/value summaries to fact projections.

    Reviewed dashboard facts use domain-specific row keys (for example
    ``carrier`` + ``late_rate`` or ``category`` + ``ordered_quantity``), while
    the offline chart renderer accepts a compact label/value envelope.  Keep
    every supplied scalar row field and add deterministic presentation aliases;
    no totals, rates, or geometry are calculated here.  The untouched fact is
    retained in ``audit_payload``.
    """

    decorated: list[dict[str, Any]] = []
    measure = _text(candidate.get("measure")).strip()
    measure_label = _humanize_label(measure) if measure else ""
    for index, raw in enumerate(rows, 1):
        row = dict(raw)
        if not _text(row.get("label") or row.get("name")).strip():
            label_parts = [
                _text(value).strip()
                for key, value in row.items()
                if isinstance(value, str)
                and _text(key).lower() not in {"unit", "period", "status", "measure"}
                and _text(value).strip()
            ]
            row["label"] = " · ".join(label_parts) if label_parts else f"Reviewed row {index}"
        # Do not infer a display value from row insertion order.  Structured
        # facts have already selected their explicit measure/series above;
        # rows without one remain table-like and are rendered as supplied.
        decorated.append(row)
    return decorated


def _manager_metric_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build readable rows for a structured metric when no fact exists."""

    rows: list[dict[str, Any]] = []
    for key in ("label", "name", "metric", "source", "as_of", "date_authority", "unit", "units", "distinct_unit", "value"):
        value = payload.get(key)
        if value is not None and not isinstance(value, (Mapping, list, tuple)):
            rows.append({"Label": _humanize_label(key), "Value": value})
    for key in ("components", "breakdown", "rows", "series", "items", "records"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            rows.extend(_manager_rows_from_mapping(value))
        elif isinstance(value, list):
            rows.extend(_manager_rows_from_sequence(value, group_label=key))
    if not rows:
        rows = _manager_rows_from_mapping(payload)
    return rows


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return None
    return float(value)


def _numeric_map(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    result: dict[str, float] = {}
    for key, item in value.items():
        parsed = _numeric(item)
        if parsed is None:
            return None
        result[_text(key)] = parsed
    return result


def _format_geometry(value: float) -> str:
    rendered = f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return rendered + "%"


def _explicit_coverage(payload: Mapping[str, Any]) -> tuple[float, Any] | None:
    coverage = _numeric(payload.get("coverage"))
    if coverage is None or coverage < 0 or coverage > 1:
        return None
    denominator = next((payload[key] for key in ("population", "denominator", "worklist_population", "source_population", "target_population") if key in payload), None)
    if denominator is None:
        return None
    return coverage, denominator


def _currency_partition(payload: Mapping[str, Any], mapping: Mapping[str, float]) -> bool:
    def _currency_key(value: str) -> bool:
        normalized = value.strip().upper()
        return normalized in {"", "UNKNOWN"} or (len(normalized) == 3 and normalized.isalpha())

    units = _text(payload.get("units") or payload.get("unit")).lower()
    currencies = payload.get("currencies")
    if isinstance(currencies, list) and currencies and all(isinstance(value, str) and len(value) == 3 for value in currencies):
        return True
    if any(token in units for token in ("currency", "no fx", "source-currency", "source currency")):
        return all(_currency_key(key) for key in mapping)
    return all(_currency_key(key) for key in mapping) and len(mapping) >= 2


def _scalar_currency_partition(payload: Mapping[str, Any]) -> bool:
    """Identify a scalar that is explicitly a source-currency partition.

    A scalar with a currency partition/no-FX contract is still not a KPI total:
    it is retained as one metric-grid tile so the presentation cannot imply a
    cross-currency aggregate.  Ordinary scalar USD/EUR metrics remain KPIs.
    """

    units = _text(payload.get("units") or payload.get("unit")).lower()
    if isinstance(payload.get("currency_partitions"), Mapping):
        return True
    if isinstance(payload.get("currencies"), (list, tuple, set, frozenset)) and payload.get("currencies"):
        return True
    return "currency" in units and ("partition" in units or "no fx" in units or "source-currency" in units or "source currency" in units)


def _signed_shape(payload: Mapping[str, Any], mapping: Mapping[str, float]) -> bool:
    shape = _text(payload.get("shape") or payload.get("value_shape")).lower()
    comparability = _text(payload.get("comparability") or payload.get("comparison")).lower()
    return bool(payload.get("signed") is True or payload.get("signed_comparable") is True or shape in {"signed", "signed_categories", "diverging"} or comparability in {"signed", "signed_comparable", "comparable_signed"}) and any(value < 0 for value in mapping.values())


def _flat_map_comparable(payload: Mapping[str, Any]) -> bool:
    """Reject explicitly mixed category units/grains before drawing geometry."""

    if payload.get("comparable") is False or payload.get("category_comparable") is False:
        return False
    for key in ("units", "unit", "grain"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            values = {_text(item).strip() for item in value.values() if _text(item).strip()}
            if len(values) > 1:
                return False
        elif isinstance(value, (list, tuple, set, frozenset)):
            values = {_text(item).strip() for item in value if _text(item).strip()}
            if len(values) > 1:
                return False
    return True


def _record_refs(item_id: str, record: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    evidence = [_safe_record_ref(value) for value in _as_list(record.get("evidence_refs")) if _text(value).strip()]
    record_id = _safe_record_ref(record.get("record_id"))
    # Trace refs must remain clickable run-local artifacts.  The typed record
    # id is retained separately for machine provenance; it is not itself a
    # filesystem reference.
    trace = [
        f"requirements/{item_id}/accepted/manifest.json",
        f"requirements/{item_id}/accepted/answer_content.json",
        f"requirements/{item_id}/integration/committed/manifest.json",
        f"requirements/{item_id}/integration/committed/records.jsonl",
    ]
    return evidence, trace


def _audit_record_entries(
    records_by_item: Mapping[str, Sequence[Mapping[str, Any]]],
    widgets: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return one lossless audit entry for every committed integration record.

    This record-level projection is authoritative for technical audit: widget
    projections can be aggregated or demoted, but the exact reviewed payload
    and the union of evidence/trace references remain available and clickable.
    No values are calculated or inferred here.
    """

    widget_record_ids: dict[str, set[str]] = {}
    for widget in widgets:
        widget_id = _text(widget.get("id"))
        if not widget_id:
            continue
        ids = {
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        }
        for record_id in ids:
            widget_record_ids.setdefault(record_id, set()).add(widget_id)
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item_id, records in records_by_item.items():
        for record in records:
            if not isinstance(record, Mapping):
                continue
            record_id = _text(record.get("record_id")).strip()
            if not record_id:
                continue
            # Only records represented by at least one typed dashboard widget
            # belong to the dashboard-relevant audit projection.  The
            # committed integration ledger can contain supporting records
            # that are intentionally not dashboard outputs; retaining those
            # here would make the audit count diverge from the widget
            # provenance set (291 records for the current G3 product).
            if record_id not in widget_record_ids:
                continue
            key = (item_id, record_id)
            if key in seen:
                raise AssemblyError(f"duplicate committed integration record ID: {item_id}/{record_id}")
            seen.add(key)
            evidence_refs, trace_refs = _record_refs(item_id, record)
            references = sorted(set(evidence_refs) | set(trace_refs))
            payload = copy.deepcopy(dict(_record_payload(record)))
            entries.append({
                "item_id": item_id,
                "record_id": record_id,
                "kind": _text(record.get("kind") or record.get("record_type") or payload.get("kind") or "reviewed_output"),
                "payload": payload,
                "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "committed_record_payload": copy.deepcopy(payload),
                "evidence_refs": sorted(set(evidence_refs)),
                "trace_refs": sorted(set(trace_refs)),
                "reference_union": references,
                "widget_ids": sorted(widget_record_ids.get(record_id, set())),
            })
    return entries


def _audit_widget_entries(widgets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one exact audit snapshot for every typed dashboard widget.

    Record-level audit entries link to widgets by ID, while this separate
    inventory owns each raw widget snapshot exactly once.  Keeping these two
    authorities separate prevents relationship widgets bound to several
    records from multiplying full bars/categories/geometry in the record
    audit while preserving every byte for technical review.
    """

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for widget in widgets:
        if not isinstance(widget, Mapping):
            continue
        widget_id = _text(widget.get("id")).strip()
        if not widget_id or widget_id in seen:
            raise AssemblyError(f"dashboard widget audit requires unique ID: {widget_id or '<missing>'}")
        seen.add(widget_id)
        record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        })
        def _refs(value: Any) -> list[str]:
            return sorted({
                _text(item.get("id") or item.get("ref") or item.get("path") if isinstance(item, Mapping) else item).strip()
                for item in _as_list(value)
                if _text(item.get("id") or item.get("ref") or item.get("path") if isinstance(item, Mapping) else item).strip()
            })

        evidence_refs = _refs(widget.get("evidence_refs"))
        trace_refs = _refs(widget.get("trace_refs"))
        entries.append({
            "widget_id": widget_id,
            "requirement_id": _text(widget.get("requirement_id")),
            "record_ids": record_ids,
            "widget_snapshot": copy.deepcopy(dict(widget)),
            "evidence_refs": evidence_refs,
            "trace_refs": trace_refs,
            "reference_union": sorted(set(evidence_refs) | set(trace_refs)),
        })
    return entries


def _widget_base(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any], *, title: str) -> dict[str, Any]:
    payload = _record_payload(record)
    evidence_refs, trace_refs = _record_refs(item_id, record)
    record_id = _safe_record_ref(record.get("record_id"))
    limitations: list[str] = []
    for source in (_as_list(content.get("limitations")), _as_list(payload.get("limitations"))):
        for value in source:
            if _text(value).strip() and _text(value) not in limitations:
                limitations.append(_text(value))
    period = payload.get("period")
    if period is None and (payload.get("period_from") is not None or payload.get("period_to") is not None):
        period = f"{_text(payload.get('period_from'))}..{_text(payload.get('period_to'))}".strip(".")
    population = payload.get("population")
    if population is None:
        population = payload.get("denominator")
    base: dict[str, Any] = {
        "id": f"{_slug(item_id)}-{_slug(record_id)}",
        "title": title or record_id,
        # The exact reviewed payload is retained for the collapsed technical
        # audit.  Manager-facing projections below never render this field by
        # default and never use it to calculate a value.
        "audit_payload": dict(payload),
        "reviewed_item_ref": f"requirements/{item_id}/accepted/manifest.json",
        "reviewed_output_ref": f"requirements/{item_id}/accepted/answer_content.json",
        "evidence_refs": evidence_refs,
        "trace_refs": trace_refs,
        "review_status": "accepted_and_integrated",
        "period": period,
        "population": population,
        # Keep the supplied denominator independent of population.  Aggregate
        # KPI tiles use this exact field for a direct ``value of denominator``
        # display; no denominator is inferred when the numerator is absent.
        "denominator": payload.get("denominator"),
        "unit": payload.get("units") or payload.get("unit"),
        "limitations": limitations,
        "grain": payload.get("grain"),
        "integration_record_hash": record.get("record_hash"),
        "integration_record_id": record_id,
        "integration_record_ref": f"requirements/{item_id}/integration/committed/records.jsonl",
        "accepted_content_hash": record.get("accepted_content_hash"),
    }
    # Scalar KPIs may retain their compact presentation when the reviewed
    # value is genuinely scalar, but provenance/authority fields must remain
    # visible on that card.  Structured and non-scalar values use the
    # lossless table projection below; these explicit scalar fields keep the
    # KPI path faithful without coercing or hiding source context.
    for key in ("source", "as_of", "date_authority", "distinct_unit"):
        if key in payload and payload[key] not in (None, ""):
            base[key] = payload[key]
    return {key: value for key, value in base.items() if value not in (None, "", [])}


def _table_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any], *, title: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    widget = _widget_base(item_id, content, record, title=title)
    widget.update({"type": "table", "rows": rows, "presentation_tier": "primary", "presentation_role": "decision_view"})
    return widget


def _metric_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _record_payload(record)
    value = payload.get("value")
    title = _text(payload.get("label") or payload.get("name") or record.get("record_id"))
    # Structured metrics (components, source populations, breakdowns, etc.)
    # intentionally have no scalar ``value``.  They are still reviewed
    # presentation data and must remain visible as a faithful table instead
    # of becoming a misleading KPI card with an em-dash.  A non-flat mapping
    # or list is treated the same way: the lossless table below preserves the
    # complete reviewed payload before any chart-family classification can
    # discard context.
    if _requires_lossless_metric_table(payload):
        rows = _structured_metric_rows(payload)
        if rows:
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
    widget = _widget_base(item_id, content, record, title=title)
    widget["type"] = "kpi"
    widget["value"] = value
    numerator = payload.get("numerator")
    denominator_value = payload.get("denominator")
    if numerator is not None and denominator_value is not None:
        # This is an explicit reviewed numerator/denominator display, not an
        # inferred rate or percentage.  The exact scalar ratio remains in
        # ``value`` and the denominator is carried into audit/context.
        widget["manager_display_value"] = f"{_text(numerator)} of {_text(denominator_value)}"
        widget["denominator"] = denominator_value
    if isinstance(value, Mapping):
        numeric_map = _numeric_map(value)
        if numeric_map is None:
            rows = [{"field": key, "value": item} for key, item in sorted(value.items(), key=lambda pair: _text(pair[0]))]
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
        if any(not key.strip() for key in numeric_map) and not _currency_partition(payload, numeric_map):
            # Empty category labels are not safe chart dimensions (and the
            # renderer rejects them).  Keep the exact source value visible in
            # a table rather than inventing an ``unknown`` bucket or zero.
            rows = [{"field": key, "value": item} for key, item in sorted(value.items(), key=lambda pair: _text(pair[0]))]
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
        if not _flat_map_comparable(payload):
            rows = [{"field": key, "value": item} for key, item in sorted(value.items(), key=lambda pair: _text(pair[0]))]
            widget = _table_widget(item_id, content, record, title=title, rows=rows)
            widget["presentation_role"] = "support_metric"
            widget["manager_rows"] = _manager_metric_rows(payload)
            return widget
        if _currency_partition(payload, numeric_map):
            widget["type"] = "metric_grid"
            widget.pop("value", None)
            widget["tiles"] = [
                {
                    "label": key if key.strip() else "(blank source currency)",
                    "source_key": key,
                    "value": value[key],
                }
                for key in sorted(value)
            ]
            widget["limitations"] = list(widget.get("limitations", [])) + ["Currency partitions remain separate; no FX conversion or cross-currency total is shown."]
            return widget
        if _signed_shape(payload, numeric_map):
            maximum = max(abs(item) for item in numeric_map.values())
            rows = [{"label": key, "value": value[key], "signed_size": _format_geometry((item / maximum) * 100 if maximum else 0)} for key, item in sorted(numeric_map.items())]
            widget.update({"type": "diverging_bar", "bars": rows, "presentation_geometry_only": True, "geometry_basis": "stable max-absolute normalization"})
            return widget
        denominator = _numeric(payload.get("denominator") or payload.get("population"))
        total = sum(numeric_map.values())
        # A flat, non-negative scalar map may be shown as part-to-whole when
        # its explicit population/denominator reconciles exactly.  This is a
        # presentation classification, not a new analytical calculation; all
        # source values and the denominator remain in the widget.
        explicit_part_to_whole = (
            denominator is not None
            and denominator > 0
            and all(item >= 0 for item in numeric_map.values())
            and 2 <= len(numeric_map) <= 5
            and abs(total - denominator) <= max(0.000001, abs(denominator) * 0.000001)
        )
        if explicit_part_to_whole and 2 <= len(numeric_map) <= 5:
            categories = [{"label": key, "value": value[key], "size": _format_geometry((numeric_map[key] / denominator) * 100 if denominator else 0)} for key in sorted(numeric_map)]
            composition_families = ("donut", "waffle", "stacked_composition")
            family_index = int(_sha256_bytes(_safe_record_ref(record.get("record_id")).encode("utf-8"))[:8], 16) % len(composition_families)
            family = composition_families[family_index]
            widget.update({"type": family, "denominator_value": payload.get("denominator") or payload.get("population"), "denominator_label": _text(payload.get("denominator_label") or payload.get("units") or "rows"), "presentation_geometry_only": True, "geometry_basis": "explicit denominator composition"})
            if family == "stacked_composition":
                widget["segments"] = categories
            else:
                widget["categories"] = categories
            return widget
        maximum = max(abs(item) for item in numeric_map.values())
        families = ("column", "lollipop", "bar")
        kind = families[int(_sha256_bytes(_safe_record_ref(record.get("record_id")).encode("utf-8"))[:8], 16) % len(families)]
        rows = [{"label": key, "value": value[key], "size": _format_geometry((abs(item) / maximum) * 100 if maximum else 0)} for key, item in sorted(numeric_map.items())]
        widget.update({"type": kind, "bars": rows, "presentation_geometry_only": True, "geometry_basis": "stable max normalization; values remain exact"})
        return widget
    if _scalar_currency_partition(payload):
        # Keep the exact source value but make the absence of FX/aggregation
        # explicit.  A one-tile metric grid is intentionally not a KPI total.
        widget["type"] = "metric_grid"
        widget.pop("value", None)
        widget["tiles"] = [{"label": title, "value": value}]
        widget["limitations"] = list(widget.get("limitations", [])) + ["Currency partitions remain source-local; no FX conversion or cross-currency total is shown."]
        return widget
    coverage = _explicit_coverage(payload)
    if coverage is not None:
        ratio, denominator = coverage
        widget.update({"type": "progress", "bars": [{"label": title, "value": value, "size": _format_geometry(ratio * 100)}], "population": denominator, "presentation_geometry_only": True, "geometry_basis": "explicit reviewed coverage"})
        return widget
    if isinstance(value, list):
        rows = [{"row": index + 1, "value": item} for index, item in enumerate(value)]
        return _table_widget(item_id, content, record, title=title, rows=rows)
    return widget


def _requires_lossless_metric_table(payload: Mapping[str, Any]) -> bool:
    """Return whether a metric payload needs a lossless table projection.

    Flat numeric ``value`` maps remain eligible for the existing reviewed
    chart families (currency partitions, composition, and bars).  Any null,
    list, nested mapping, or structured companion field instead gets the
    deterministic recursive projection so no source field is silently
    dropped.  This boundary is deliberately shape-based and performs no
    numeric coercion.
    """

    value = payload.get("value")
    if value is None or isinstance(value, list):
        return True
    # An empty/blank scalar is not a usable KPI.  Route it to the recursive
    # table even when the record carries no other structured field, and keep
    # it out of overview selection by construction (tables are not KPIs).
    if isinstance(value, str) and not value.strip():
        return True
    structured_keys = {"components", "sources", "rows", "items", "records", "series", "breakdown"}
    if structured_keys.intersection(payload):
        return True
    if any(isinstance(item, (Mapping, list)) for key, item in payload.items() if key != "value"):
        return True
    if isinstance(value, Mapping):
        # Flat numeric maps are still useful reviewed chart inputs when their
        # only siblings are presentation controls (label/unit/population).
        # Provenance/authority companions, however, make chart-only paths
        # lossy: the complete payload must be rendered as recursive rows.
        chart_control_keys = {
            "label", "name", "units", "unit", "population", "denominator",
            "denominator_label", "limitations", "grain", "period", "period_from",
            "period_to", "shape", "value_shape", "signed", "signed_comparable",
            "comparability", "comparison", "comparable", "category_comparable",
            "currency_partitions", "currencies", "denominators", "status_rows",
            "watchlist_rows", "source_population", "target_population",
        }
        if any(key not in chart_control_keys and key != "value" for key in payload):
            return True
        # Keep the established flat numeric-map visualizations intact.  A map
        # containing nested values is not chart-safe and must be fully
        # represented in the table instead.
        return _numeric_map(value) is None
    return False


def _structured_metric_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten one reviewed structured metric without dropping context.

    A structured metric is not a scalar KPI.  Earlier normalization returned
    as soon as it found ``components``/``series``/``breakdown`` and silently
    discarded top-level review fields (metric name, source, date authority,
    units, totals, and conflict flags).  The dashboard is a faithful view of
    accepted records, so context rows are emitted first in deterministic key
    order, followed by the nested values.  No value is calculated, coerced, or
    replaced with zero.
    """

    rows: list[dict[str, Any]] = []

    def value_json(value: Any) -> str:
        # JSON is the faithful display representation for null, empty strings,
        # mappings, and lists.  It is intentionally not parsed or normalized
        # into a number/string surrogate.
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def append_detail(path: str, value: Any, *, row_kind: str = "detail") -> None:
        field = path.rsplit(".", 1)[-1]
        rows.append(
            {
                "row_kind": row_kind,
                "path": path,
                "field": field,
                "value": value,
                "value_json": value_json(value),
            }
        )
        if isinstance(value, Mapping):
            for child_key in sorted(value, key=lambda item: _text(item)):
                child_path = f"{path}.{_text(child_key)}"
                append_detail(child_path, value[child_key])
        elif isinstance(value, list):
            for index, child in enumerate(value, 1):
                append_detail(f"{path}[{index}]", child)

    # Emit one context row for every top-level key, including ``value`` when it
    # is null/empty and structured payloads in their entirety.  This preserves
    # the compatibility fields consumed by existing fixtures while the detail
    # rows below provide a complete path-addressable recursive projection.
    for key, value in sorted(payload.items(), key=lambda pair: _text(pair[0])):
        field = _text(key)
        rows.append(
            {
                "row_kind": "context",
                "path": field,
                "field": field,
                "value": value,
                "value_json": value_json(value),
            }
        )
        if isinstance(value, (Mapping, list)):
            append_detail(field, value)

    # Preserve the established first-level nested rows used by dashboard
    # review contracts.  Recursive ``detail`` rows carry the complete child
    # payload without changing the meaning/order of these compatibility rows.
    for key in ("components", "sources", "rows", "items", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            for index, item in enumerate(value, 1):
                if isinstance(item, Mapping):
                    row = {"row_kind": "nested", "source_field": key, "source_index": index, **dict(item)}
                else:
                    row = {"row_kind": "nested", "source_field": key, "source_index": index, "value": item}
                row["path"] = f"{key}[{index}]"
                row["value_json"] = value_json(item)
                rows.append(row)
    series = payload.get("series")
    if isinstance(series, list):
        for index, item in enumerate(series, 1):
            rows.append(
                {
                    "row_kind": "nested",
                    "source_field": "series",
                    "source_index": index,
                    "value": item,
                    "path": f"series[{index}]",
                    "value_json": value_json(item),
                }
            )
    breakdown = payload.get("breakdown")
    if isinstance(breakdown, Mapping):
        for key, value in sorted(breakdown.items(), key=lambda pair: _text(pair[0])):
            if isinstance(value, Mapping):
                row = {"row_kind": "nested", "source_field": "breakdown", "category": _text(key), **dict(value)}
            else:
                row = {"row_kind": "nested", "source_field": "breakdown", "category": _text(key), "value": value}
            row["path"] = f"breakdown.{_text(key)}"
            row["value_json"] = value_json(value)
            rows.append(row)
    return rows


def _nested_numeric_maps(payload: Mapping[str, Any]) -> dict[str, dict[str, float]] | None:
    """Return independently chartable numeric children from a mixed mapping.

    Accepted records often carry scalar summaries, previews, and a handful of
    flat maps together.  Requiring every top-level value to be a mapping made
    the useful maps disappear into one large table.  We therefore extract only
    children whose values are all numeric; complex siblings remain available to
    the conservative table fallback in ``_metric_widgets``.
    """

    value = payload.get("value")
    if not isinstance(value, Mapping) or not value:
        return None
    nested: dict[str, dict[str, float]] = {}
    for key, item in value.items():
        parsed = _numeric_map(item)
        if parsed is not None:
            nested[_text(key)] = parsed
    return nested or None


def _nested_denominator(payload: Mapping[str, Any], subkey: str, submap: Mapping[str, float]) -> tuple[Any, str] | None:
    """Find an explicit sibling denominator that reconciles exactly.

    A denominator is used for composition geometry only when its supplied
    value equals the exact sum of the child map.  This prevents a broad
    population count from being silently treated as the denominator for an
    unrelated category map.
    """

    total = sum(submap.values())
    if total < 0:
        return None
    stem = re.sub(r"(?:_counts?|_types?|_map)$", "", subkey)
    stems = [stem]
    # ``watchlist_action_counts`` is normally reconciled to the containing
    # metric's ``watchlist_rows``; similarly status/type/category maps may use
    # the broader population stem.  Keep this expansion lexical and bounded.
    for suffix in ("_action", "_status", "_type", "_category"):
        if stem.endswith(suffix):
            stems.append(stem[: -len(suffix)])
    candidate_keys = [
        *(f"{candidate}_rows" for candidate in stems),
        "rows",
        *(f"{candidate}_count" for candidate in stems),
        "population",
        "denominator",
        "source_population",
        "target_population",
    ]
    sibling_denominators = payload.get("denominators")
    candidates: list[tuple[str, Any]] = []
    if isinstance(sibling_denominators, Mapping) and subkey in sibling_denominators:
        candidates.append((f"denominators.{subkey}", sibling_denominators[subkey]))
    # Integrated metric records commonly place scalar siblings (for example
    # ``watchlist_rows`` or ``rows``) beside the child map inside ``value``.
    # Check that containing mapping first, then explicit payload-level fields.
    containing_value = payload.get("value")
    for key in candidate_keys:
        if isinstance(containing_value, Mapping) and key in containing_value:
            candidates.append((f"value.{key}", containing_value[key]))
        if key in payload:
            candidates.append((key, payload[key]))
    for key, raw in candidates:
        numeric = _numeric(raw)
        if numeric is None or numeric <= 0:
            continue
        if abs(total - numeric) <= max(0.000001, abs(numeric) * 0.000001):
            return raw, key
    return None


def _entity_leaderboard(value: Mapping[str, Any]) -> tuple[str, dict[str, float]] | None:
    """Collapse an entity -> metric map into one deterministic leaderboard.

    Supplier/entity maps can contain several fields per entity.  Rendering one
    widget per entity is not decision-sized, so when a stable exception field
    is shared by every entity we draw one chart across entities.  If no common
    exception field exists, the caller falls back to one honest table.
    """

    if not value or not all(isinstance(item, Mapping) for item in value.values()):
        return None
    keys = [_text(key) for key in value]
    if not any(":" in key or re.match(r"(?i)^(supplier|vendor|customer|sku|warehouse|wh)[_:-]", key) for key in keys):
        return None
    parsed_children: dict[str, dict[str, float]] = {}
    for key, item in value.items():
        parsed = _numeric_map(item)
        if parsed is None:
            return None
        parsed_children[_text(key)] = parsed
    common = set.intersection(*(set(item) for item in parsed_children.values()))
    priorities = ("late", "overdue", "exception", "mismatch", "unmatched", "open", "pending", "error", "count")
    for token in priorities:
        candidates = sorted(field for field in common if token in field.lower())
        if candidates:
            field = candidates[0]
            return field, {key: children[field] for key, children in sorted(parsed_children.items())}
    return None


def _metric_widgets(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = _record_payload(record)
    nested = _nested_numeric_maps(payload)
    if nested is None:
        return [_metric_widget(item_id, content, record)]
    value = payload.get("value")
    if isinstance(value, Mapping):
        leaderboard = _entity_leaderboard(value)
        if leaderboard is not None:
            field, aggregate = leaderboard
            subpayload = dict(payload)
            subpayload["value"] = aggregate
            subpayload["label"] = f"{_text(payload.get('label') or payload.get('name') or record.get('record_id'))} · {field} by entity"
            subrecord = dict(record)
            subrecord["payload"] = subpayload
            widget = _metric_widget(item_id, content, subrecord)
            widget["source_metric_key"] = field
            widget["entity_projection"] = "deterministic common exception field"
            return [widget]
    base_title = _text(payload.get("label") or payload.get("name") or record.get("record_id"))
    widgets: list[dict[str, Any]] = []
    for subkey, submap in sorted(nested.items()):
        subpayload = dict(payload)
        subpayload["value"] = submap
        subpayload["label"] = f"{base_title} · {subkey}"
        denominator = _nested_denominator(payload, subkey, submap)
        for denominator_key in ("denominator", "population", "source_population", "target_population"):
            subpayload.pop(denominator_key, None)
        if denominator is not None:
            raw_denominator, denominator_key = denominator
            # Preserve the key's semantic label while allowing the generic
            # metric classifier to apply the reconciled composition rule.
            subpayload["denominator"] = raw_denominator
            subpayload["denominator_label"] = denominator_key
        subrecord = dict(record)
        subrecord["payload"] = subpayload
        widget = _metric_widget(item_id, content, subrecord)
        widget["id"] = f"{_slug(item_id)}-{_slug(record.get('record_id'))}-{_slug(subkey)}"
        widget["source_metric_key"] = subkey
        widgets.append(widget)
        if len(widgets) >= 6:
            break
    # If there are no (or only one) chartable child maps, retain complex
    # siblings as one collapsed table instead of silently dropping them.  A
    # rich set of deterministic charts remains decision-sized without a second
    # prose/preview table per metric.
    complex_keys = [key for key in value if _text(key) not in nested]
    if complex_keys and len(widgets) < 2:
        rows = [{"field": _text(key), "value": value[key]} for key in sorted(complex_keys, key=_text)]
        table = _table_widget(item_id, content, record, title=base_title, rows=rows)
        widgets.append(table)
    return widgets


def _relationship_widget(item_id: str, content: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(records, key=lambda value: _text(value.get("record_id")))
    first = ordered[0]
    payload = _record_payload(first)
    title = "Relationship coverage"
    widget = _widget_base(item_id, content, first, title=title)
    widget["id"] = f"{_slug(item_id)}-relationship-coverage"
    widget["presentation_role"] = "relationship_matrix"
    widget["presentation_tier"] = "primary"
    widget["integration_record_ids"] = [_safe_record_ref(record.get("record_id")) for record in ordered]
    widget["integration_record_refs"] = [f"requirements/{item_id}/integration/committed/records.jsonl" for _ in ordered]
    widget["trace_refs"] = sorted({ref for record in ordered for ref in _record_refs(item_id, record)[1]})
    widget["evidence_refs"] = sorted({ref for record in ordered for ref in _record_refs(item_id, record)[0]})
    rows: list[dict[str, Any]] = []
    audit_payloads: list[Mapping[str, Any]] = []
    for record in ordered:
        relationship_payload = _record_payload(record)
        audit_payloads.append(dict(relationship_payload))
        source = relationship_payload.get("source_id")
        target = relationship_payload.get("target_id")
        row: dict[str, Any] = {
            "Relationship": f"{_endpoint_label(source)} → {_endpoint_label(target)}",
            "Source": _endpoint_label(source),
            "Target": _endpoint_label(target),
        }
        for side, label in (("source", "Source coverage"), ("target", "Target coverage")):
            population = relationship_payload.get(f"{side}_population")
            matched = relationship_payload.get(f"matched_{side}_count")
            coverage = relationship_payload.get(f"{side}_coverage")
            if matched is not None and population is not None:
                row[label] = f"{_text(matched)}/{_text(population)}"
            elif coverage is not None:
                row[label] = coverage
        if relationship_payload.get("cardinality") not in (None, ""):
            row["Cardinality"] = relationship_payload.get("cardinality")
        rows.append(row)
    if rows:
        widget.update({"type": "table", "rows": rows, "manager_rows": rows, "audit_payload": audit_payloads})
        return widget
    table_rows = []
    for record in ordered:
        relationship_payload = _record_payload(record)
        row = {key: value for key, value in relationship_payload.items() if key in {"source_id", "target_id", "relationship_id", "cardinality", "matched_pairs", "source_population", "target_population", "limitations"}}
        table_rows.append(row or {"record_id": record.get("record_id")})
    widget.update({"type": "table", "rows": table_rows, "manager_rows": table_rows, "audit_payload": audit_payloads})
    return widget


def _claim_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _record_payload(record)
    title = _text(payload.get("claim_id") or record.get("record_id"))
    widget = _table_widget(item_id, content, record, title=title, rows=[{
        "claim": payload.get("claim"),
        "status": payload.get("status"),
        "period": payload.get("period"),
    }])
    widget["presentation_role"] = "finding_record"
    widget["presentation_tier"] = "audit"
    return widget


def _fact_widget(item_id: str, content: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = _record_payload(record)
    candidate = payload.get("widget")
    if not isinstance(candidate, Mapping):
        # Result Integration's public dashboard_fact API stores the reviewed
        # presentation fields directly in the payload.  Treat that payload as
        # the candidate rather than dropping the fact when no nested widget
        # envelope was supplied.
        candidate = payload
    if not isinstance(candidate, Mapping) or not any(
        key in candidate for key in ("title", "label", "type", "rows", "series", "value", "display_value", "visual_id")
    ):
        return None
    source_widget_id = _text(candidate.get("id"))
    title = _text(candidate.get("title") or candidate.get("label") or source_widget_id or _text(record.get("record_id")))
    base = _widget_base(item_id, content, record, title=title)
    authoritative_id = _text(base["id"])
    # A dashboard fact is a presentation hint only.  Merge the small
    # presentation vocabulary and retain every item/provenance/evidence/hash,
    # status, and limitation field owned by the assembler.
    presentation_fields = {
        "type", "title", "value", "display_value", "bars", "tiles", "categories", "segments", "rows",
        "points", "data",
        "presentation_geometry_only", "geometry_basis",
        "denominator_value", "denominator_label", "chart_notes", "notes", "layout", "span", "overview",
        "small_multiple_group", "small_multiple_label", "series", "sample_policy", "sample_rows", "visual_id",
        "columns",
    }
    base.update({key: candidate[key] for key in sorted(presentation_fields) if key in candidate})
    base["presentation_role"] = "decision_view"
    base["presentation_tier"] = "primary"
    base["audit_payload"] = dict(candidate)
    raw_type = _text(candidate.get("type")).lower()
    if raw_type in {"kpi", "kpi_grid", "metric_grid"}:
        base["type"] = "kpi_grid"
        if "tiles" not in base:
            tiles = candidate.get("tiles") or candidate.get("rows") or candidate.get("series")
            if isinstance(tiles, list):
                base["tiles"] = [dict(item) if isinstance(item, Mapping) else {"label": item, "value": item} for item in tiles]
            elif candidate.get("value") is not None:
                base["tiles"] = [{"label": title, "value": candidate.get("value")}]
    else:
        # Local facts are intentionally conservative: semantic fields are
        # projected into a compact decision view; the exact candidate remains
        # in audit_payload for the collapsed technical audit.
        manager_type, manager_values = _manager_fact_projection(candidate)
        base["type"] = manager_type
        if manager_type in {"kpi_grid", "metric_grid"}:
            base["tiles"] = manager_values
            base.pop("rows", None)
        else:
            manager_rows = _decorate_manager_fact_rows(candidate, manager_values)
            # Keep the manager projection in the renderer's explicit chart
            # channel.  The original candidate remains byte-exact in
            # ``audit_payload``; unbound raw ``bars``/``categories`` fields
            # must not win over the projection when a manager card renders.
            # Inherited status/table facts are not newly admitted chart
            # contracts. Preserve their exact row shape across cumulative
            # rebuilds; only non-table visual facts receive the compact
            # label/value aliases needed by chart renderers.
            if manager_type in {"table", "status_table"}:
                manager_rows = [dict(row) for row in manager_values if isinstance(row, Mapping)]
            if manager_type in {"bar", "column", "lollipop", "progress", "leaderboard", "diverging_bar", "stacked_composition"}:
                base["bars"] = manager_rows
                base.pop("categories", None)
                base.pop("segments", None)
                base.pop("points", None)
                # ``rows``/``data``/``series`` from the raw accepted payload
                # are audit-only once the explicit chart projection exists.
                base.pop("rows", None)
                base.pop("data", None)
                base.pop("series", None)
            elif manager_type == "scatter":
                # ``_structured_fact_projection`` already binds exact x/y
                # values and a human group label; never re-read a first scalar
                # from the raw row here.
                base["points"] = [dict(row) for row in manager_rows]
                base.pop("bars", None)
                base.pop("categories", None)
                base.pop("rows", None)
                base.pop("data", None)
                base.pop("series", None)
            else:
                base["rows"] = manager_rows
                base.pop("bars", None)
                base.pop("categories", None)
                base.pop("segments", None)
                base.pop("points", None)
                base.pop("data", None)
                # Legacy G3 dashboard facts (status/exception/coverage
                # tables) carry an accepted ``series`` snapshot even when
                # their manager rows are audit-only.  Preserve that raw
                # envelope exactly for inherited widgets; only the new
                # structured chart contract consumes/rewrites series above.
                if raw_type in {"bar", "column", "scatter", "table"}:
                    base.pop("series", None)
            base["manager_rows"] = manager_rows
    base["title"] = title
    base["dashboard_fact"] = True
    # The record-bound id is authoritative and globally scoped by item.  Keep
    # a caller-provided fact id as descriptive metadata only.
    base["id"] = authoritative_id
    if source_widget_id and source_widget_id != authoritative_id:
        base["source_widget_id"] = source_widget_id
    return base


def _widget_identity(item_id: str, group_id: str, widget: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build exact raw identity material used only for collision disambiguation."""

    record_ids = [_safe_record_ref(value) for value in _as_list(widget.get("integration_record_ids")) if _text(value).strip()]
    if not record_ids and _text(widget.get("integration_record_id")).strip():
        record_ids = [_safe_record_ref(widget.get("integration_record_id"))]
    selected: list[Mapping[str, Any]] = []
    if record_ids:
        wanted = set(record_ids)
        selected = [record for record in records if _safe_record_ref(record.get("record_id")) in wanted]
    identity: dict[str, Any] = {
        "item_id": item_id,
        "group_id": group_id,
        "record_ids": record_ids,
        "records": selected,
        "source_metric_key": _text(widget.get("source_metric_key")),
        "source_widget_id": _text(widget.get("source_widget_id")),
    }
    if not selected:
        identity["widget"] = {key: value for key, value in widget.items() if key != "id"}
    return identity


def _collision_safe_widget_ids(
    item_id: str,
    group_id: str,
    widgets: list[dict[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append deterministic raw-identity digests only when slugs collide."""

    by_base: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for widget in widgets:
        base_id = _text(widget.get("id"))
        by_base.setdefault(base_id, []).append((widget, _widget_identity(item_id, group_id, widget, records)))
    for base_id, entries in by_base.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda pair: _canonical_bytes(pair[1]))
        for collision_index, (widget, identity) in enumerate(entries):
            identity = dict(identity)
            identity["collision_index"] = collision_index
            widget["id"] = f"{base_id}--{_json_hash(identity)}"
    return widgets


_LEGACY_CHART_TYPES = frozenset({
    "kpi", "bar", "column", "lollipop", "donut", "metric_grid", "kpi_grid",
    "waffle", "diverging_bar", "stacked_composition", "line", "heatmap",
    "scatter", "leaderboard", "progress",
})
_LEGACY_CHART_FIELDS = frozenset({
    "type", "title", "value", "manager_display_value", "bars", "categories", "tiles",
    "segments", "points", "cells", "data", "presentation_geometry_only",
    "geometry_basis", "denominator_value", "denominator_label", "chart_notes",
    "small_multiple_group", "small_multiple_label", "scale_policy",
    # These fields are still presentation-envelope material (not a source or
    # identity decision) and are required to reproduce an inherited aggregate
    # metric grid exactly, e.g. REQ12's closed/open signal strip.
    "aggregated_metric_ids", "integration_record_ids", "integration_record_refs",
    "integration_record_hashes", "audit_payload",
})


def _true_visual_ids(
    widgets_by_id: Mapping[str, Mapping[str, Any]],
    charts_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Return the current generation's exact visual universe.

    V2 used to hard-code the G3 75-ID partition.  The visual universe is a
    property of the candidate fixture/chart map instead: every supported
    chart type is eligible for exactly one manager/audit partition entry.
    Ordinary table/status-table projections remain record/widget audit only;
    a reviewed ``dashboard_fact`` whose accepted chart type is ``table`` is
    an explicit visual contract and is included as-is.  This keeps inherited
    G3 status tables out of the chart partition while preserving new fact
    types without lexical admission.
    """

    result: list[str] = []
    for widget_id, widget in widgets_by_id.items():
        chart = charts_by_id.get(widget_id)
        if not isinstance(chart, Mapping):
            continue
        widget_type = _text(widget.get("type") or widget.get("kind")).strip().lower()
        chart_type = _text(chart.get("type")).strip().lower()
        is_legacy_chart = (
            widget_type
            and chart_type == widget_type
            and chart_type in _LEGACY_CHART_TYPES
            and chart_type not in {"table", "status_table"}
        )
        is_reviewed_table_fact = (
            bool(widget.get("dashboard_fact"))
            and widget_type == "table"
            and chart_type == "table"
        )
        if is_legacy_chart or is_reviewed_table_fact:
            result.append(widget_id)
    return result


def _apply_legacy_chart_hint(widget: dict[str, Any], hint: Mapping[str, Any] | None) -> None:
    """Preserve a reviewed chart projection while rebuilding its envelope."""

    if not isinstance(hint, Mapping):
        return
    hint_type = _text(hint.get("type")).strip().lower()
    if hint_type not in _LEGACY_CHART_TYPES:
        return
    if _text(hint.get("presentation_role")).lower() == "relationship_matrix":
        return
    # Never copy identity/provenance or requirement metadata from an old
    # fixture.  Those bindings belong to the current accepted/integrated
    # record; only the already-reviewed visual shape and supplied values are
    # reusable.
    for key in _LEGACY_CHART_FIELDS:
        if key in hint:
            widget[key] = copy.deepcopy(hint[key])
        elif hint_type in {"metric_grid", "kpi_grid"} and key in {"value", "manager_display_value"}:
            # Preserve absence as well as value: an inherited aggregate grid
            # has tiles, not a duplicate scalar KPI field.
            widget.pop(key, None)
    inherited_role = _text(hint.get("presentation_role")).strip()
    if inherited_role:
        widget["presentation_role"] = inherited_role
    elif hint_type != "table":
        widget["presentation_role"] = "decision_view"
    if inherited_role:
        # Carry the inherited role through the lexical admission pass without
        # persisting an internal marker into the visual snapshot.
        widget["_legacy_presentation_role"] = inherited_role


def _metric_record_ids(widget: Mapping[str, Any]) -> set[str]:
    values = _as_list(widget.get("integration_record_ids"))
    if not values and widget.get("integration_record_id") not in (None, ""):
        values = [widget.get("integration_record_id")]
    return {_safe_record_ref(value) for value in values if _text(value).strip()}


def _schema_only_metric_widget(widget: Mapping[str, Any]) -> bool:
    """Identify support tables that expose only metric schema metadata."""

    if _text(widget.get("presentation_role")).lower() in {"finding_list", "finding_record", "relationship_matrix"}:
        return False
    kind = _text(widget.get("type") or widget.get("kind")).lower()
    if kind not in {"table", "status_table"}:
        return False
    rows = widget.get("manager_rows")
    if not isinstance(rows, list):
        rows = widget.get("rows")
    label_value_rows = [row for row in _as_list(rows) if isinstance(row, Mapping)]
    if label_value_rows and all(set(row) <= {"Label", "Value"} for row in label_value_rows):
        labels = {_text(row.get("Label")).strip().lower() for row in label_value_rows}
        if labels and labels <= {"label", "name", "units", "unit", "source", "metric"}:
            return True
    normalized: set[str] = set()
    for row in _as_list(rows):
        if not isinstance(row, Mapping):
            continue
        normalized.update(re.sub(r"[^a-z0-9]+", "_", _text(key).lower()).strip("_") for key in row)
    if not normalized:
        return True
    return normalized <= {"label", "name", "units", "unit", "source", "metric"}


def _consolidate_metric_widgets_without_facts(widgets: list[dict[str, Any]]) -> None:
    """Keep at most one meaningful primary projection per metric record."""

    hinted_aggregates = [
        widget for widget in widgets
        if _text(widget.get("type") or widget.get("kind")).lower() in {"metric_grid", "kpi_grid"}
        and isinstance(widget.get("tiles"), list)
        and _text(widget.get("_legacy_presentation_role") or widget.get("presentation_role")).lower() == "decision_view"
        and (len(_metric_record_ids(widget)) > 1 or len(widget.get("tiles") or []) > 1)
    ]
    if hinted_aggregates:
        primary_ids = {id(widget) for widget in hinted_aggregates}
        for widget in widgets:
            if id(widget) in primary_ids:
                widget["presentation_tier"] = "primary"
                widget["presentation_role"] = _text(widget.get("_legacy_presentation_role"), "decision_view")
            elif _text(widget.get("type") or widget.get("kind")).lower() in {"kpi", "progress", "metric_grid", "kpi_grid"}:
                widget["presentation_tier"] = "audit"
                widget["presentation_role"] = "support_metric"
        return

    groups: dict[str, list[dict[str, Any]]] = {}
    for widget in widgets:
        if widget.get("presentation_role") in {"finding_list", "finding_record", "relationship_matrix"}:
            continue
        for record_id in sorted(_metric_record_ids(widget)):
            groups.setdefault(record_id, []).append(widget)
    for group in groups.values():
        candidates = [
            widget for widget in group
            if not _schema_only_metric_widget(widget)
            and _text(widget.get("manager_admission", {}).get("status"), "admitted") == "admitted"
        ]
        if not candidates:
            for widget in group:
                widget["presentation_tier"] = "audit"
                widget["presentation_role"] = "support_metric"
            continue
        # Explicit chart projections outrank raw table/support projections;
        # otherwise retain the first deterministic widget for this record.
        primary = next(
            (widget for widget in candidates if _text(widget.get("type")).lower() in _LEGACY_CHART_TYPES),
            candidates[0],
        )
        for widget in group:
            if widget is primary:
                widget.setdefault("presentation_tier", "primary")
                if _schema_only_metric_widget(widget):
                    widget["presentation_tier"] = "audit"
                    widget["presentation_role"] = "support_metric"
                continue
            widget["presentation_tier"] = "audit"
            widget["presentation_role"] = "support_metric"


def _aggregate_scalar_metric_widgets(
    widgets: list[dict[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> None:
    """Collapse scalar KPI/progress metrics into one reviewed signal strip.

    Dashboard facts are already the primary decision views.  Keeping every
    scalar metric as a separate card makes a requirement page noisy, so the
    first scalar metric's stable ID becomes the aggregate ``metric_grid`` and
    all remaining scalar metric records move to audit.  Values, units,
    denominators, and provenance remain supplied fields; no totals or ratios
    are calculated here.
    """

    record_by_id = {
        _safe_record_ref(record.get("record_id")): record
        for record in records
        if _safe_record_ref(record.get("record_id"))
    }
    # A legacy chart hint may already carry the reviewed aggregate metric
    # grid (for example REQ-12's closed/open tiles).  Keep that stable
    # envelope as the primary projection instead of promoting whichever
    # scalar happens to be encountered last during a cumulative rebuild.
    hinted_aggregate = next(
        (
            widget for widget in widgets
            if _text(widget.get("type")).lower() in {"metric_grid", "kpi_grid"}
            and isinstance(widget.get("tiles"), list)
            and _text(widget.get("_legacy_presentation_role") or widget.get("presentation_role")).lower() == "decision_view"
            and (len(_metric_record_ids(widget)) > 1 or len(widget.get("tiles") or []) > 1)
        ),
        None,
    )
    if hinted_aggregate is not None:
        related = _metric_record_ids(hinted_aggregate)
        for widget in widgets:
            if widget is hinted_aggregate:
                widget["presentation_tier"] = "primary"
                widget["presentation_role"] = "decision_view"
                continue
            if (
                related.intersection(_metric_record_ids(widget))
                or _text(widget.get("type") or widget.get("kind")).lower() in {"kpi", "progress", "metric_grid", "kpi_grid"}
            ):
                widget["presentation_tier"] = "audit"
                widget["presentation_role"] = "support_metric"
        return

    scalar: list[dict[str, Any]] = []
    for widget in widgets:
        kind = _text(widget.get("type") or widget.get("kind")).lower()
        if kind not in {"kpi", "progress"}:
            continue
        if _text(widget.get("manager_admission", {}).get("status"), "admitted") != "admitted":
            continue
        record_ids = [_safe_record_ref(value) for value in _as_list(widget.get("integration_record_ids"))]
        if not record_ids and _text(widget.get("integration_record_id")):
            record_ids = [_safe_record_ref(widget.get("integration_record_id"))]
        if not any(_text(record_by_id.get(record_id, {}).get("kind")) == "metric" for record_id in record_ids):
            continue
        value = widget.get("value")
        if value in (None, ""):
            continue
        scalar.append(widget)
    if not scalar:
        return

    primary = scalar[0]
    tiles: list[dict[str, Any]] = []
    ordered_ids: list[str] = []
    ordered_refs: list[str] = []
    ordered_evidence: list[str] = []
    ordered_trace: list[str] = []
    ordered_hashes: list[str] = []
    audit_payloads: list[Any] = []

    def extend_unique(target: list[str], values: Iterable[Any]) -> None:
        for value in values:
            text = _text(value).strip()
            if text and text not in target:
                target.append(text)

    for widget in scalar:
        label = _text(widget.get("title") or widget.get("label") or widget.get("id"))
        tile: dict[str, Any] = {"label": label, "value": widget.get("value")}
        if widget.get("manager_display_value") not in (None, ""):
            tile["display_value"] = widget.get("manager_display_value")
        for key in ("denominator", "population", "unit", "period", "as_of", "date_authority"):
            if widget.get(key) not in (None, ""):
                tile[key] = widget[key]
        tiles.append(tile)
        extend_unique(ordered_ids, widget.get("integration_record_ids") or [widget.get("integration_record_id")])
        extend_unique(ordered_refs, widget.get("integration_record_refs") or [widget.get("integration_record_ref")])
        extend_unique(ordered_evidence, widget.get("evidence_refs"))
        extend_unique(ordered_trace, widget.get("trace_refs"))
        extend_unique(ordered_hashes, [widget.get("integration_record_hash")])
        payload = widget.get("audit_payload")
        if payload is None:
            record_id = _text(widget.get("integration_record_id"))
            payload = _record_payload(record_by_id.get(record_id, {}))
        audit_payloads.append(payload)

    primary["type"] = "metric_grid"
    primary.pop("value", None)
    primary["tiles"] = tiles
    primary["title"] = "Key signals"
    primary["presentation_role"] = "decision_view"
    primary["presentation_tier"] = "primary"
    primary["integration_record_ids"] = ordered_ids
    primary["integration_record_refs"] = ordered_refs
    primary["evidence_refs"] = ordered_evidence
    primary["trace_refs"] = ordered_trace
    primary["integration_record_hashes"] = ordered_hashes
    primary["audit_payload"] = audit_payloads
    primary["aggregated_metric_ids"] = [_text(widget.get("id")) for widget in scalar]
    for widget in scalar[1:]:
        widget["presentation_tier"] = "audit"
        widget["presentation_role"] = "support_metric"


def _build_widgets(
    item_id: str,
    content: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    legacy_hints: Mapping[str, Mapping[str, Any]] | None = None,
    manager_widget_ids: Sequence[str] | None = None,
    manager_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    widgets: list[dict[str, Any]] = []
    relationship_records: list[Mapping[str, Any]] = []
    for record in sorted(records, key=lambda value: (_text(value.get("kind")), _text(value.get("record_id")))):
        kind = _text(record.get("kind"))
        if kind == "metric":
            widgets.extend(_metric_widgets(item_id, content, record))
        elif kind == "relationship":
            relationship_records.append(record)
        elif kind == "dashboard_fact":
            fact = _fact_widget(item_id, content, record)
            if fact is not None:
                widgets.append(fact)
        elif kind == "claim":
            # Accepted business conclusions are reviewed outputs too.  Keep
            # the exact claim text in a local table with the same evidence and
            # provenance envelope as every other widget; no value is derived.
            widget = _claim_widget(item_id, content, record)
            widgets.append(widget)
    if relationship_records:
        widgets.append(_relationship_widget(item_id, content, relationship_records))
    if not widgets:
        # A typed limitation-only result remains visible without manufacturing
        # a zero-valued KPI.
        for record in records:
            if _text(record.get("kind")) == "limitation":
                widgets.append(_claim_widget(item_id, content, record))
    # Aggregate claims into one manager-facing finding list while retaining
    # every original claim widget/record as an audit item.  The first claim's
    # stable ID remains the primary binding; no synthetic record or ID is
    # introduced.
    claim_widgets = [widget for widget in widgets if widget.get("presentation_role") == "finding_record"]
    if claim_widgets:
        record_by_id = {_safe_record_ref(record.get("record_id")): record for record in records}
        primary = claim_widgets[0]
        findings: list[dict[str, Any]] = []
        for claim_widget in claim_widgets:
            record_id = _text(claim_widget.get("integration_record_id"))
            payload = _record_payload(record_by_id.get(record_id, {}))
            claim = payload.get("claim")
            if claim is None:
                continue
            findings.append({
                "finding": claim,
                "status": payload.get("status"),
                "period": payload.get("period") or payload.get("as_of"),
            })
        primary["title"] = "Reviewed findings"
        primary["presentation_role"] = "finding_list"
        primary["presentation_tier"] = "primary"
        primary["manager_findings"] = findings
        for audit_widget in claim_widgets[1:]:
            audit_widget["presentation_role"] = "finding_record"
            audit_widget["presentation_tier"] = "audit"

    for widget in widgets:
        hint = legacy_hints.get(_text(widget.get("id"))) if legacy_hints else None
        _apply_legacy_chart_hint(widget, hint)

    # Classify the raw reviewed projections before consolidating scalar metrics
    # so technical metrics can never be folded into a manager-facing signal
    # strip.  The second pass below refreshes the aggregate widget metadata.
    _apply_manager_admission(
        widgets,
        subject_context=" ".join(
            _text(content.get(key))
            for key in ("__manager_requirement_scope", "scope", "method")
            if _text(content.get(key)).strip()
        ),
    )

    # When dashboard facts exist, metrics are supporting evidence rather than
    # competing primary cards.  Mark every metric-derived widget audit-first;
    # the scalar aggregation below then restores one stable ``Key signals``
    # strip to primary.  This keeps structured tables from inflating the
    # manager surface while retaining their exact rows in the audit tier.
    has_dashboard_fact = any(_text(record.get("kind")) == "dashboard_fact" for record in records)
    if has_dashboard_fact:
        metric_record_ids = {
            _safe_record_ref(record.get("record_id"))
            for record in records
            if _text(record.get("kind")) == "metric" and _safe_record_ref(record.get("record_id"))
        }
        for widget in widgets:
            widget_record_ids = {
                _safe_record_ref(value)
                for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
                if _safe_record_ref(value)
            }
            if widget.get("aggregated_metric_ids"):
                continue
            if widget.get("presentation_role") == "support_metric" or metric_record_ids.intersection(widget_record_ids):
                widget["presentation_tier"] = "audit"
                widget["presentation_role"] = "support_metric"
        _aggregate_scalar_metric_widgets(widgets, records)
    else:
        _consolidate_metric_widgets_without_facts(widgets)
    _apply_manager_admission(
        widgets,
        subject_context=" ".join(
            _text(content.get(key))
            for key in ("__manager_requirement_scope", "scope", "method")
            if _text(content.get(key)).strip()
        ),
    )
    for widget in widgets:
        widget.setdefault("presentation_tier", "primary")
        widget.setdefault("presentation_role", "decision_view")
    # Public assembly always supplies an explicit set (possibly empty).  The
    # lexical classifier above remains available to focused low-level helper
    # callers only; it is never the effective product admission boundary.
    if manager_widget_ids is not None:
        _apply_explicit_manager_admission(widgets, manager_widget_ids, manager_entries)
    for widget in widgets:
        widget.pop("_legacy_presentation_role", None)
    # Every accepted/integrated record is a reviewed output.  The renderer
    # supports arbitrary ordered widget lists; no records are truncated.
    return widgets


def _apply_overview_selection(widgets: list[dict[str, Any]]) -> list[str]:
    """Mark a deterministic subset of existing, non-null KPI cards."""

    for widget in widgets:
        widget.pop("overview", None)
    selected = [
        widget for widget in widgets
        if _text(widget.get("type") or widget.get("kind")).lower() == "kpi"
        and widget.get("value") is not None
        and not (isinstance(widget.get("value"), str) and not widget["value"].strip())
        and (
            not isinstance(widget.get("manager_admission"), Mapping)
            or (
                _text(widget.get("presentation_audience")) == "business_manager"
                and _text(widget.get("manager_admission", {}).get("status")) == "admitted"
            )
        )
    ][:4]
    for widget in selected:
        widget["overview"] = True
    return [_text(widget.get("id")) for widget in selected]


def _ontology_projection(all_records: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    relationships: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    group_members: dict[str, list[str]] = {}
    group_meta: dict[str, dict[str, Any]] = {}
    for record in all_records:
        payload = _record_payload(record)
        kind = _text(record.get("kind"))
        if kind == "ontology_item" and (payload.get("ontology_projection") is True or (payload.get("ontology_id") and payload.get("kind"))):
            node_id = _text(payload.get("ontology_id") or payload.get("id"))
            label = _text(payload.get("label") or node_id)
            node_kind = _text(payload.get("kind") or payload.get("object_type"))
            if not node_id or not label or not node_kind:
                raise AssemblyError("ontology nodes require stable id, label, and kind")
            if node_id in node_ids:
                raise AssemblyError(f"duplicate ontology node: {node_id}")
            node_ids.add(node_id)
            node = {"id": node_id, "label": label, "kind": node_kind}
            group_id = _text(payload.get("group_id") or payload.get("group"))
            if group_id:
                node["group_id"] = group_id
                group_members.setdefault(group_id, []).append(node_id)
                meta = group_meta.setdefault(group_id, {"id": group_id, "label": group_id})
                if payload.get("group_label"):
                    meta["label"] = _text(payload.get("group_label"))
                if payload.get("group_order") is not None:
                    meta["order"] = payload.get("group_order")
            nodes.append(node)
        elif kind == "relationship" and payload.get("ontology_projection") is True:
            source = _text(payload.get("source_id") or payload.get("source"))
            target = _text(payload.get("target_id") or payload.get("target"))
            label = _text(payload.get("label") or payload.get("relationship"))
            if not source or not target or not label:
                continue
            edge = (source, target, label)
            if edge in edge_keys:
                raise AssemblyError(f"duplicate ontology relationship: {source}->{target}/{label}")
            edge_keys.add(edge)
            relationships.append({"source": source, "target": target, "label": label})
    if relationships and any(edge["source"] not in node_ids or edge["target"] not in node_ids for edge in relationships):
        raise AssemblyError("ontology relationship references an unknown endpoint")
    groups: list[dict[str, Any]] = []
    if group_members:
        covered: set[str] = set()
        for group_id, node_list in sorted(group_members.items(), key=lambda pair: (group_meta[pair[0]].get("order", 10**9), pair[0])):
            if any(node_id in covered for node_id in node_list):
                raise AssemblyError("ontology groups contain duplicate nodes")
            covered.update(node_list)
            meta = dict(group_meta[group_id])
            meta["node_ids"] = node_list
            groups.append(meta)
        if covered != node_ids:
            raise AssemblyError("ontology groups must cover every projected node")
    nodes.sort(key=lambda value: value["id"])
    relationships.sort(key=lambda value: (value["source"], value["target"], value["label"]))
    return nodes, relationships, groups


def _load_projection_metadata(context: RunContext, item_ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind the presentation build to the public committed LEM projection.

    The product assembler does not trust a stale product snapshot as its
    semantic authority.  It rebuilds the read-only projection from the
    lifecycle's accepted/committed records, then records hashes for the
    projection, prepared registry, telemetry metadata, and (when present) the
    existing terminal product manifest.
    """

    try:
        from auto_foundry_core.lem_projection import LivingEnterpriseModelProjector
        from auto_foundry_core.prepared import PreparedAssetRegistry
    except ModuleNotFoundError as exc:  # pragma: no cover - package install guard
        raise AssemblyError("public LEM/prepared registry types are unavailable") from exc
    try:
        projection = LivingEnterpriseModelProjector.project(context, item_ids=item_ids)
    except Exception as exc:
        raise AssemblyError("accepted/committed LEM projection validation failed") from exc
    exported = projection.model.export()
    count_map = {
        "ontology_items": len(exported.get("ontology", [])) if isinstance(exported.get("ontology"), list) else None,
        "relationships": len(exported.get("relationships", {})) if isinstance(exported.get("relationships"), Mapping) else None,
        "canonical_mappings": len(exported.get("canonical_mappings", [])) if isinstance(exported.get("canonical_mappings"), list) else None,
        "identity_decisions": len(exported.get("identity_decisions", [])) if isinstance(exported.get("identity_decisions"), list) else None,
        "prepared_assets": len(exported.get("prepared_assets", [])) if isinstance(exported.get("prepared_assets"), list) else None,
        "knowledge": len(exported.get("knowledge", {})) if isinstance(exported.get("knowledge"), Mapping) else None,
        "resolution_bindings": len(projection.resolution_bindings),
        "item_bindings": len(projection.bindings),
    }
    summary = {key: value for key, value in count_map.items() if value is not None}

    registry = PreparedAssetRegistry(context)
    descriptors = registry.search(include_superseded=True)
    registry_path = registry.registry_path
    registry_present = registry_path.is_file() and not registry_path.is_symlink()
    if registry_present:
        registry_bytes = registry_path.read_bytes()
        registry_hash = _sha256_bytes(registry_bytes)
    else:
        # The public registry reports an explicit empty set when no prepared
        # assets were committed.  Bind that empty semantic state without
        # manufacturing a run-local file or reading candidate work payloads.
        registry_hash = _json_hash([descriptor.to_dict() for descriptor in descriptors])
    index_path = registry.index_path
    index_present = index_path.is_file() and not index_path.is_symlink()
    index_hash = _sha256_bytes(index_path.read_bytes()) if index_present else _json_hash({"entries": [descriptor.to_dict() for descriptor in descriptors]})

    telemetry_hashes: dict[str, str] = {}
    for reference in ("telemetry/events.jsonl", "telemetry/inventory_counters.json"):
        path = context.resolve_run_path(reference)
        if path.is_file() and not path.is_symlink():
            telemetry_hashes[reference] = _sha256_bytes(path.read_bytes())
    telemetry_hash = _json_hash(telemetry_hashes)

    product_manifest_ref = "products/product_manifest.json"
    product_manifest_hash: str | None = None
    product_manifest_markers: Mapping[str, Any] = {}
    product_manifest: Mapping[str, Any] | None = None
    product_manifest_path = context.resolve_run_path(product_manifest_ref)
    if product_manifest_path.is_file() and not product_manifest_path.is_symlink():
        product_manifest = _read_json(context, product_manifest_ref, label="frozen product manifest")
        if product_manifest.get("run_id") not in {None, context.run_id}:
            raise AssemblyError("frozen product manifest run identity mismatch")
        product_manifest_hash = _sha256_bytes(product_manifest_path.read_bytes())
        raw_markers = product_manifest.get("freeze_markers")
        if isinstance(raw_markers, Mapping):
            for key, value in raw_markers.items():
                if type(value) is not bool:
                    raise AssemblyError(f"frozen product marker is not boolean: {key}")
            product_manifest_markers = dict(raw_markers)
        raw_assets = product_manifest.get("assets")
        if isinstance(raw_assets, list):
            for asset in raw_assets:
                if not isinstance(asset, Mapping):
                    raise AssemblyError("frozen product assets must be objects")
                reference = _text(asset.get("ref")).strip()
                expected_hash = asset.get("sha256")
                if not reference or not _is_sha256(expected_hash):
                    raise AssemblyError("frozen product asset reference/hash is invalid")
                # Validate before any open.  Absolute/external archives and
                # source/work/calculation refs are outside the presentation
                # boundary and fail closed rather than being silently skipped.
                safe_reference = _validate_product_asset_reference(reference)
                _path, data = _read_bytes(context, safe_reference, label="frozen product asset")
                if _sha256_bytes(data) != expected_hash:
                    raise AssemblyError(f"frozen product asset hash mismatch: {safe_reference}")
        lem_meta = product_manifest.get("lem")
        if isinstance(lem_meta, Mapping) and lem_meta.get("projection_hash") not in {None, projection.projection_hash}:
            raise AssemblyError("frozen product LEM projection hash mismatch")
        if product_manifest_markers.get("telemetry_frozen") is True and not telemetry_hashes:
            raise AssemblyError("frozen product claims telemetry is frozen but telemetry metadata is absent")
        if product_manifest_markers.get("prepared_data_registry_frozen") is True and not registry_present:
            prepared_count = lem_meta.get("prepared_asset_count") if isinstance(lem_meta, Mapping) else None
            if prepared_count not in {None, 0}:
                raise AssemblyError("frozen product claims prepared registry is frozen but registry is absent")

    freeze_markers = {
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_data_registry_frozen": bool(registry_present or (not descriptors and product_manifest_markers.get("prepared_data_registry_frozen") is True)),
        "dashboard_frozen": True,
        "telemetry_frozen": bool(telemetry_hashes),
    }
    projection_metadata = {
        "projection_hash": projection.projection_hash,
        "export_sha256": _json_hash(exported),
        "item_order": list(projection.item_order),
        "bindings": [binding.to_dict() for binding in projection.bindings],
        "summary": summary,
        "prepared_registry": {
            "ref": "lem/prepared_data_registry.jsonl",
            "present": registry_present,
            "descriptor_count": len(descriptors),
            "sha256": registry_hash,
        },
        "prepared_index": {
            "ref": "indexes/prepared_index.json",
            "present": index_present,
            "sha256": index_hash,
        },
        "telemetry": {"sha256": telemetry_hash, "assets": telemetry_hashes},
        "product_manifest_ref": product_manifest_ref if product_manifest is not None else None,
        "product_manifest_sha256": product_manifest_hash,
        "freeze_markers": freeze_markers,
    }
    return summary, projection_metadata


def _build_registry(source: Path, destination: Path) -> dict[str, Any]:
    try:
        registry = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssemblyError("committed dashboard chart registry asset is invalid") from exc
    if not isinstance(registry, Mapping) or registry.get("schema_version") != REGISTRY_SCHEMA or not isinstance(registry.get("families"), list):
        raise AssemblyError("dashboard chart registry schema is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    return {"schema_version": registry["schema_version"], "sha256": _sha256_bytes(destination.read_bytes())}


def _chart_map_entry(widget: Mapping[str, Any], requirement_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
    kind = _text(widget.get("type") or "table")
    family = {
        "kpi": "kpi_card", "bar": "horizontal_bar", "column": "column", "lollipop": "lollipop",
        "diverging_bar": "diverging_bar", "waffle": "waffle", "donut": "donut_pie", "stacked_composition": "stacked_bar", "progress": "horizontal_bar",
        "metric_grid": "metric_grid", "kpi_grid": "metric_grid", "table": "table", "status_table": "table", "line": "line_area_slope", "scatter": "scatter_bubble",
    }.get(kind, "table")
    fields: dict[str, Any] = {key: value for key, value in widget.items() if key in {"value", "manager_display_value", "bars", "tiles", "categories", "segments", "points", "series", "rows", "manager_rows", "population", "denominator", "unit", "period", "presentation_geometry_only", "geometry_basis", "denominator_value", "denominator_label", "grain", "integration_record_id", "integration_record_ids", "integration_record_ref", "integration_record_refs", "presentation_role", "presentation_tier"}}
    return {
        "id": _text(widget.get("id")),
        "type": kind,
        "family": family,
        "requirement_id": requirement_id,
        "fields_or_values_used": fields,
        "provenance": {
            "item_id": _text(item.get("item_id")),
            "accepted_manifest_hash": _text(item.get("accepted_manifest_hash")),
            "accepted_content_hash": _text(item.get("accepted_content_hash")),
            "integration_manifest_hash": _text(item.get("integration_manifest_hash")),
            "integration_record_hash": _text(widget.get("integration_record_hash")),
            "integration_record_id": _text(widget.get("integration_record_id")),
            "integration_record_ids": list(widget.get("integration_record_ids", [])),
            "integration_record_ref": _text(widget.get("integration_record_ref")),
            "evidence_refs": list(widget.get("evidence_refs", [])),
            "trace_refs": list(widget.get("trace_refs", [])),
        },
    }


def _product_ref(context: RunContext, value: str | Path) -> tuple[Path, str]:
    raw = Path(value)
    # ``RunContext.resolve_product_path`` already anchors relative paths under
    # ``run_root/products``.  Accepting a leading ``products/`` convenience
    # prefix without stripping it would create ``products/products/...``;
    # normalize that prefix while retaining all containment checks.
    if not raw.is_absolute() and raw.parts and raw.parts[0] == "products":
        raw = Path(*raw.parts[1:]) if len(raw.parts) > 1 else Path(".")
    path = context.resolve_product_path(raw)
    relative = path.relative_to(context.run_root).as_posix()
    return path, relative


def _replace_prefix(root: Path, old: str, new: str) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AssemblyError(f"cannot read staged presentation asset {path}") from exc
        if old.encode("utf-8") in data:
            path.write_bytes(data.replace(old.encode("utf-8"), new.encode("utf-8")))


def _publish_staged_output(staging_root: Path, output_root: Path, *, retain_backup: bool = False) -> None:
    """Replace one product namespace with a validated staged directory.

    The previous namespace is moved to a sibling backup before the staged
    directory is published.  If publication fails, the old directory is
    restored, leaving the prior product bytes and receipt intact.
    """

    backup_root = output_root.parent / f".{output_root.name}.previous"
    if backup_root.exists() or backup_root.is_symlink():
        raise AssemblyError(f"previous product backup already exists: {backup_root.name}")
    moved_previous = False
    published = False
    try:
        if output_root.exists() or output_root.is_symlink():
            os.replace(output_root, backup_root)
            moved_previous = True
        os.replace(staging_root, output_root)
        published = True
    except Exception:
        if moved_previous and not output_root.exists() and (backup_root.exists() or backup_root.is_symlink()):
            os.replace(backup_root, output_root)
        raise
    finally:
        if published and moved_previous and not retain_backup and (backup_root.exists() or backup_root.is_symlink()):
            if backup_root.is_dir() and not backup_root.is_symlink():
                shutil.rmtree(backup_root, ignore_errors=True)
            else:
                backup_root.unlink(missing_ok=True)


def _site_tree_binding(site_root: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Return canonical per-file hashes and a complete deterministic tree hash."""

    if not site_root.is_dir() or site_root.is_symlink():
        raise AssemblyError(f"site output directory is missing or symlinked: {site_root}")
    excluded = exclude or set()
    files: dict[str, str] = {}
    for path in sorted(site_root.rglob("*")):
        relative = path.relative_to(site_root).as_posix()
        if relative in excluded:
            continue
        if path.is_symlink():
            raise AssemblyError(f"site output contains symlink: {relative}")
        if not path.is_file():
            continue
        try:
            files[relative] = _sha256_bytes(path.read_bytes())
        except OSError as exc:
            raise AssemblyError(f"site output file cannot be read: {relative}") from exc
    if not files:
        raise AssemblyError("site output contains no files")
    return {"files": files, "tree_sha256": _json_hash(files), "file_count": len(files)}


def assemble_dashboard(
    context: RunContext,
    *,
    output_dir: str | Path = "repro_dashboard_v4",
    item_ids: Sequence[str] | None = None,
    plan_ref: str | Path | None = None,
    fixture_ref: str | Path | None = None,
    chart_map_ref: str | Path | None = None,
    chart_registry_ref: str | Path | None = None,
    site_ref: str | Path | None = None,
    receipt_ref: str | Path | None = None,
    presentation_plan_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble a deterministic run-local V4 fixture, map, registry, and site."""

    if not isinstance(context, RunContext):
        raise TypeError("assemble_dashboard requires one RunContext")
    output_root, output_run_ref = _product_ref(context, output_dir)
    if output_root == context.run_root / "products":
        raise AssemblyError("output_dir must be a dedicated reproducibility namespace")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    plan = _load_plan(context, plan_ref)
    selected_ids = _discover_item_ids(context, item_ids, plan)
    groups = _group_definitions(plan, selected_ids)
    plan_binding = _plan_binding(plan, groups, plan_ref)
    generation_id, generation_metadata = _presentation_generation_metadata(context)
    if generation_metadata is not None:
        supervisor_plan_ref, supervisor_plan, supervisor_plan_hash = _presentation_supervisor_binding(
            context,
            generation_id,
            generation_metadata,
        )
    else:
        supervisor_plan_ref = Path(plan_ref or "requirement_supervisor_plan.json").as_posix()
        supervisor_plan = plan or _read_json(context, supervisor_plan_ref, label="presentation supervisor plan")
        supervisor_plan_hash = _sha256_bytes(context.resolve_run_path(supervisor_plan_ref).read_bytes())
    if generation_id == "G-0001":
        # Root products must bind to the lifecycle's canonical supervisor plan
        # bytes.  Reject an alias before any product staging or publication.
        if plan_ref is not None and Path(plan_ref).as_posix() != supervisor_plan_ref:
            raise AssemblyError("root plan_ref must be requirement_supervisor_plan.json")
        plan_binding = _root_plan_binding(supervisor_plan_ref, supervisor_plan_hash)
    presentation_plan: Mapping[str, Any] | None = None
    presentation_plan_sha256: str | None = None
    manager_widget_ids: list[str] = []
    manager_entries: dict[str, Mapping[str, Any]] = {}
    resolved_presentation_plan_ref: str | None = None
    if presentation_plan_ref is not None:
        resolved_presentation_plan_ref = _presentation_plan_ref(context, generation_id, presentation_plan_ref)
        presentation_plan, presentation_plan_sha256 = _load_business_presentation_plan(
            context,
            resolved_presentation_plan_ref,
        )
        # Input/parent bindings are checked again after all accepted and
        # committed bundles have been loaded below.
    loaded: dict[str, dict[str, Any]] = {}
    all_records: list[Mapping[str, Any]] = []
    authoritative_roots: dict[str, Mapping[str, Any]] = {}
    record_file_hashes: dict[str, str] = {}
    for item_id in selected_ids:
        content, accepted_manifest, accepted_meta = _load_public_accepted_bundle(context, item_id)
        integration_manifest, records = _load_committed_records(context, item_id, accepted_manifest, accepted_meta["bundle"])
        records_path = context.resolve_run_path(f"requirements/{item_id}/integration/committed/records.jsonl")
        records_file_sha = _sha256_bytes(records_path.read_bytes())
        for record in records:
            record_id = _text(record.get("record_id")).strip()
            if record_id:
                authoritative_roots[record_id] = _presentation_authoritative_root(record, content)
                record_file_hashes[record_id] = records_file_sha
        raw_scope = _text(content.get("scope") or content.get("method"))
        # The full reviewed scope is retained in ``requirement_scope`` and the
        # collapsed audit.  It is not a manager subtitle because method/source
        # mechanics in that text would bypass admission.
        compact_scope = ""
        loaded[item_id] = {
            "item_id": item_id,
            "content": content,
            "requirement_scope": _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
            "requirement_title": _manager_requirement_title(
                item_id,
                content,
                _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
                records,
            ),
            "requirement_subtitle": compact_scope,
            "takeaway": _manager_takeaway(
                content,
                _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
            ),
            "limitations": _manager_limitations(
                content,
                records,
                _text(accepted_meta["state"].get("original_text") or content.get("scope") or item_id),
            ),
            "accepted_manifest_hash": accepted_meta["bundle"].manifest_hash,
            "accepted_content_hash": accepted_meta["bundle"].content_hash,
            "integration_manifest_hash": integration_manifest["manifest_hash"],
            "records": records,
        }
        all_records.extend(records)
    lem_summary, projection_metadata = _load_projection_metadata(context, selected_ids)
    legacy_hints = _load_legacy_chart_hints(context)
    legacy_chart_map_hints = _load_legacy_chart_map_hints(context)
    nodes, relationships, ontology_groups = _ontology_projection(all_records)
    fixture_path, fixture_run_ref = _product_ref(context, fixture_ref or (Path(output_dir) / "dashboard_fixture_v4.json"))
    chart_map_path, chart_map_run_ref = _product_ref(context, chart_map_ref or (Path(output_dir) / "dashboard_chart_map_v4.json"))
    registry_path, registry_run_ref = _product_ref(context, chart_registry_ref or (Path(output_dir) / "dashboard_chart_registry_v4.json"))
    site_path, site_run_ref = _product_ref(context, site_ref or (Path(output_dir) / "site"))
    receipt_path, receipt_run_ref = _product_ref(context, receipt_ref or (Path(output_dir) / "build_receipt.json"))
    for path, label in ((fixture_path, "fixture_ref"), (chart_map_path, "chart_map_ref"), (registry_path, "chart_registry_ref"), (site_path, "site_ref"), (receipt_path, "receipt_ref")):
        try:
            path.relative_to(output_root)
        except ValueError as exc:
            raise AssemblyError(f"{label} must remain inside output_dir") from exc
    current_input_items = [
        {"item_id": item_id, "accepted_content_hash": loaded[item_id]["accepted_content_hash"], "accepted_manifest_hash": loaded[item_id]["accepted_manifest_hash"], "integration_manifest_hash": loaded[item_id]["integration_manifest_hash"], "record_count": len(loaded[item_id]["records"])}
        for item_id in selected_ids
    ]
    presentation_parent = _presentation_parent_binding(context, generation_id, generation_metadata)
    if presentation_plan is not None:
        if presentation_plan.get("schema_version") == PRESENTATION_PLAN_V2_SCHEMA:
            # V2 visual/source validation is completed after the candidate
            # widgets and chart map have been rebuilt below.  Input/parent
            # lineage is still checked before any staging namespace is made.
            _validate_v2_plan_lineage(
                context,
                presentation_plan,
                generation_id=generation_id,
                supervisor_ref=supervisor_plan_ref,
                input_items=current_input_items,
                parent=presentation_parent,
            )
            manager_widget_ids = list(presentation_plan["manager_widget_ids"])
        else:
            manager_widget_ids = _validate_business_presentation_plan(
                context,
                presentation_plan,
                generation_id=generation_id,
                supervisor_ref=supervisor_plan_ref,
                supervisor_plan=supervisor_plan,
                input_items=current_input_items,
                parent=presentation_parent,
                authoritative_roots=authoritative_roots,
                record_file_hashes=record_file_hashes,
            )
        manager_entries = {entry["widget_id"]: entry for entry in presentation_plan["manager_entries"]}
    existing_receipt: dict[str, Any] | None = None
    if output_root.exists():
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise AssemblyError(f"output namespace already exists without a valid receipt: {output_dir}")
        try:
            existing_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssemblyError(f"existing output receipt is invalid: {output_dir}") from exc
        if not isinstance(existing_receipt, Mapping) or existing_receipt.get("schema_version") != ASSEMBLER_SCHEMA or existing_receipt.get("status") != "complete":
            raise AssemblyError(f"existing output receipt is invalid: {output_dir}")
        if existing_receipt.get("input_items") != current_input_items:
            raise AssemblyError("existing output namespace input hashes do not match current accepted/committed inputs")
        if existing_receipt.get("plan_binding") != plan_binding:
            raise AssemblyError("existing output namespace supervisor plan/grouping hash does not match")
        existing_presentation_ref = existing_receipt.get("presentation_plan_ref")
        existing_presentation_hash = existing_receipt.get("presentation_plan_sha256")
        existing_manager_ids = existing_receipt.get("manager_widget_ids")
        if presentation_plan is None:
            if existing_presentation_ref is not None or existing_presentation_hash is not None or existing_manager_ids not in (None, []):
                raise AssemblyError("existing output namespace requires its explicit presentation plan")
        elif (
            existing_presentation_ref is not None
            and (existing_presentation_ref != resolved_presentation_plan_ref or existing_presentation_hash != presentation_plan_sha256 or existing_manager_ids != list(manager_widget_ids))
        ):
            raise AssemblyError("existing output namespace presentation plan binding does not match")
        existing_freeze = existing_receipt.get("freeze_inputs")
        def _bound_hash(value: Any) -> Any:
            return value.get("sha256") if isinstance(value, Mapping) else None

        if (
            not isinstance(existing_freeze, Mapping)
            or existing_freeze.get("projection_hash") != projection_metadata["projection_hash"]
            or _bound_hash(existing_freeze.get("prepared_registry")) != projection_metadata["prepared_registry"]["sha256"]
            or _bound_hash(existing_freeze.get("prepared_index")) != projection_metadata["prepared_index"]["sha256"]
            or _bound_hash(existing_freeze.get("telemetry")) != projection_metadata["telemetry"]["sha256"]
            or existing_freeze.get("product_manifest_sha256") != projection_metadata.get("product_manifest_sha256")
        ):
            raise AssemblyError("existing output namespace frozen projection/metadata hashes do not match")
        hashes = existing_receipt.get("output_hashes")
        outputs = existing_receipt.get("outputs")
        if not isinstance(hashes, Mapping) or not isinstance(outputs, Mapping):
            raise AssemblyError("existing output receipt lacks output bindings")
        check_refs = {
            "fixture_sha256": outputs.get("fixture_ref"),
            "chart_map_sha256": outputs.get("chart_map_ref"),
            "chart_registry_sha256": outputs.get("chart_registry_ref"),
        }
        for key, reference in check_refs.items():
            if not isinstance(reference, str) or not reference:
                raise AssemblyError(f"existing output receipt reference is missing: {key}")
            path = context.resolve_run_path(reference)
            if not path.is_file() or path.is_symlink() or _sha256_bytes(path.read_bytes()) != hashes.get(key):
                raise AssemblyError(f"existing output hash mismatch: {key}")
        site_ref_value = outputs.get("site_ref")
        if not isinstance(site_ref_value, str) or not site_ref_value:
            raise AssemblyError("existing output receipt reference is missing: site_ref")
        actual_site_binding = _site_tree_binding(context.resolve_run_path(site_ref_value))
        if actual_site_binding != existing_receipt.get("site_binding"):
            raise AssemblyError("existing output site file hash mismatch")
        site_manifest_path = context.resolve_run_path(f"{site_ref_value.rstrip('/')}/site_manifest.json")
        if _sha256_bytes(site_manifest_path.read_bytes()) != hashes.get("site_manifest_sha256"):
            raise AssemblyError("existing output hash mismatch: site_manifest_sha256")
        existing_receipt = dict(existing_receipt)
    staging_root = output_root.parent / f".{output_root.name}.staging"
    if staging_root.exists():
        raise AssemblyError(f"staging namespace already exists: {staging_root.name}")
    staging_root.mkdir(parents=True)
    staging_prefix = staging_root.relative_to(context.run_root).as_posix()
    final_prefix = output_root.relative_to(context.run_root).as_posix()
    fixture_rel = fixture_path.relative_to(output_root)
    chart_map_rel = chart_map_path.relative_to(output_root)
    registry_rel = registry_path.relative_to(output_root)
    site_rel = site_path.relative_to(output_root)
    receipt_rel = receipt_path.relative_to(output_root)
    staged_fixture_path = staging_root / fixture_rel
    staged_map_path = staging_root / chart_map_rel
    staged_registry_path = staging_root / registry_rel
    staged_site_path = staging_root / site_rel
    staged_receipt_path = staging_root / receipt_rel
    staged_fixture_ref = staged_fixture_path.relative_to(context.run_root).as_posix()
    staged_map_ref = staged_map_path.relative_to(context.run_root).as_posix()
    staged_registry_ref = staged_registry_path.relative_to(context.run_root).as_posix()
    staged_site_ref = staged_site_path.relative_to(context.run_root / "products").as_posix()
    try:
        registry_source = Path(__file__).resolve().parent.parent / "assets" / "dashboard_chart_registry.json"
        registry_info = _build_registry(registry_source, staged_registry_path)
        staged_fixture_path.parent.mkdir(parents=True, exist_ok=True)
        staged_map_path.parent.mkdir(parents=True, exist_ok=True)
        widgets: list[dict[str, Any]] = []
        domains: list[dict[str, Any]] = []
        chart_items: list[dict[str, Any]] = []
        for group in groups:
            flow_defs: list[dict[str, Any]] = []
            for flow_order, item_id in enumerate(group["requirement_ids"], 1):
                item = loaded[item_id]
                widget_content = dict(item["content"])
                widget_content["__manager_requirement_scope"] = item["requirement_scope"]
                item_widgets = _build_widgets(
                    item_id,
                    widget_content,
                    item["records"],
                    legacy_hints=legacy_hints,
                    manager_widget_ids=manager_widget_ids,
                    manager_entries=manager_entries,
                )
                _collision_safe_widget_ids(item_id, group["id"], item_widgets, item["records"])
                admitted_widget_ids = {
                    _text(widget.get("id"))
                    for widget in item_widgets
                    if isinstance(widget.get("manager_admission"), Mapping)
                    and widget["manager_admission"].get("status") == "admitted"
                }
                item["manager_admission"] = {
                    "status": "admitted" if admitted_widget_ids else "audit_only",
                    "presentation_audience": "business_manager" if admitted_widget_ids else "technical_audit",
                    "policy": "explicit_business_presentation_plan",
                "admitted_widget_ids": [widget_id for widget_id in manager_widget_ids if widget_id in admitted_widget_ids],
                    "presentation_plan_ref": resolved_presentation_plan_ref,
                    "presentation_plan_sha256": presentation_plan_sha256,
                }
                for widget_index, widget in enumerate(item_widgets, 1):
                    widget["requirement_id"] = item_id
                    widget["requirement_title"] = item["requirement_title"]
                    if item["requirement_subtitle"]:
                        widget["requirement_subtitle"] = item["requirement_subtitle"]
                    if item["takeaway"]:
                        widget["takeaway"] = item["takeaway"]
                    if item["requirement_scope"]:
                        widget["requirement_scope"] = item["requirement_scope"]
                    if item["limitations"]:
                        widget["requirement_limitations"] = list(item["limitations"])
                    widget["requirement_order"] = flow_order
                    widget["domain_id"] = group["id"]
                    widget["manager_admission"] = dict(widget.get("manager_admission") or {})
                    widget["manager_admission"]["requirement_status"] = item["manager_admission"]["status"]
                    widget["manager_anchor"] = f"{_slug(item_id)}-{_slug(widget.get('presentation_role') or 'decision-view')}-{widget_index}"
                    widgets.append(widget)
                    chart_entry = _chart_map_entry(widget, item_id, item)
                    inherited_chart_entry = legacy_chart_map_hints.get(_text(widget.get("id")).strip())
                    chart_items.append(
                        copy.deepcopy(inherited_chart_entry)
                        if inherited_chart_entry is not None
                        else chart_entry
                    )
                flow = {
                    "id": f"{group['id']}-{_slug(item_id)}",
                    "title": item["requirement_title"],
                    "order": flow_order,
                    "widget_ids": [widget["id"] for widget in item_widgets],
                    "manager_admission": dict(item["manager_admission"]),
                    "presentation_audience": item["manager_admission"]["presentation_audience"],
                }
                # Section headers carry the original requirement text and the
                # accepted headline/takeaway.  The latter is context only;
                # no number mining or re-analysis occurs in the assembler.
                if item["requirement_subtitle"]:
                    flow["subtitle"] = item["requirement_subtitle"]
                if item["takeaway"]:
                    flow["takeaway"] = item["takeaway"]
                if item["requirement_scope"]:
                    flow["scope"] = item["requirement_scope"]
                if item["limitations"]:
                    flow["limitations"] = list(item["limitations"])
                flow_defs.append(flow)
            if flow_defs:
                domain_title = group["title"]
                if len(flow_defs) == 1 and not _is_manager_title_candidate(domain_title):
                    domain_title = loaded[group["requirement_ids"][0]]["requirement_title"]
                domains.append({"id": group["id"], "title": domain_title, "summary": group.get("summary"), "order": group["order"], "decision_flow": flow_defs})
        if presentation_plan is not None and presentation_plan.get("schema_version") != PRESENTATION_PLAN_V2_SCHEMA:
            _validate_business_presentation_plan(
                context,
                presentation_plan,
                generation_id=generation_id,
                supervisor_ref=supervisor_plan_ref,
                supervisor_plan=supervisor_plan,
                input_items=current_input_items,
                parent=presentation_parent,
                widgets=widgets,
                authoritative_roots=authoritative_roots,
                record_file_hashes=record_file_hashes,
            )
        if not widgets:
            raise AssemblyError("accepted/integrated inputs produced no typed presentation widgets")
        overview_widget_ids = _apply_overview_selection(widgets)
        audit_records = _audit_record_entries(
            {item_id: loaded[item_id]["records"] for item_id in selected_ids},
            widgets,
        )
        audit_widgets = _audit_widget_entries(widgets)
        fixture: dict[str, Any] = {
            "schema_version": FIXTURE_SCHEMA,
            "dashboard_version": 4,
            "site_version": 4,
            "title": "Reproducible reviewed decision workspace",
            "subtitle": "Deterministic presentation of accepted and committed results; no new analytics are calculated.",
            "run_id": context.run_id,
            "skill_version": context.skill_version or "0.7.1",
            "freeze_markers": dict(projection_metadata["freeze_markers"]),
            "chart_registry_ref": staged_registry_ref,
            "chart_map_ref": staged_map_ref,
            "domains": domains,
            "widgets": widgets,
            "audit_records": audit_records,
            "audit_widgets": audit_widgets,
            "audit_widget_entry_count": len(audit_widgets),
            "ontology_summary": lem_summary,
            "overview_widget_ids": overview_widget_ids,
            "presentation_plan_ref": resolved_presentation_plan_ref,
            "presentation_plan_sha256": presentation_plan_sha256,
            "manager_widget_ids": list(manager_widget_ids),
            "manager_entries": [copy.deepcopy(manager_entries[key]) for key in manager_widget_ids],
            "manager_admission": {
                "policy": "explicit_business_presentation_plan",
                "presentation_plan_ref": resolved_presentation_plan_ref,
                "presentation_plan_sha256": presentation_plan_sha256,
                "business_requirements": sorted(
                    item_id for item_id, item in loaded.items()
                    if item.get("manager_admission", {}).get("status") == "admitted"
                ),
                "technical_requirements": sorted(
                    item_id for item_id, item in loaded.items()
                    if item.get("manager_admission", {}).get("status") != "admitted"
                ),
            },
            "lem_projection_hash": projection_metadata["projection_hash"],
            "lem_export_sha256": projection_metadata["export_sha256"],
            "prepared_registry_hash": projection_metadata["prepared_registry"]["sha256"],
            "telemetry_metadata_hash": projection_metadata["telemetry"]["sha256"],
            "ontology_objects": nodes,
            "ontology_relationships": relationships,
            "ontology_groups": ontology_groups,
            "limitations": ["Presentation values are copied from accepted answer/integration records only.", "Unknown, malformed, mixed-unit, and no-FX values remain visible as limitations or tables; no missing value is rendered as zero."],
        }
        if presentation_plan is not None and presentation_plan.get("schema_version") == PRESENTATION_PLAN_V2_SCHEMA:
            fixture["manager_visual_widget_ids"] = list(presentation_plan["manager_visual_widget_ids"])
            fixture["audit_visual_widget_ids"] = list(presentation_plan["audit_visual_widget_ids"])
            fixture["visual_entries"] = copy.deepcopy(presentation_plan["visual_entries"])
            fixture["presentation_plan_schema"] = PRESENTATION_PLAN_V2_SCHEMA
        chart_map = {"schema_version": CHART_MAP_SCHEMA, "chart_registry_ref": staged_registry_ref, "fixture_ref": staged_fixture_ref, "charts": chart_items}
        if presentation_plan is not None and presentation_plan.get("schema_version") == PRESENTATION_PLAN_V2_SCHEMA:
            _validate_business_presentation_plan_v2(
                context,
                presentation_plan,
                fixture={"widgets": widgets},
                fixture_ref=staged_fixture_ref,
                chart_map=chart_map,
                chart_map_ref=staged_map_ref,
                widgets=widgets,
                strict_source_hash=False,
            )
        _write_json(staged_fixture_path, fixture)
        _write_json(staged_map_path, chart_map)
        # render_site_fixture validates the fixture, chart map, registry, links,
        # and the offline stylesheet before the staging namespace is published.
        renderer_path = Path(__file__).resolve().with_name("dashboard_renderer.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("dashboard_renderer_for_assembler", renderer_path)
        if spec is None or spec.loader is None:
            raise AssemblyError("dashboard renderer cannot be loaded")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        renderer.render_site_fixture(context, staged_fixture_ref, staged_site_ref, f"{staged_site_ref}/site_manifest.json")
        _replace_prefix(staging_root, staging_prefix, final_prefix)
        fixture["chart_registry_ref"] = registry_run_ref
        fixture["chart_map_ref"] = chart_map_run_ref
        chart_map["chart_registry_ref"] = registry_run_ref
        chart_map["fixture_ref"] = fixture_run_ref
        _write_json(staged_fixture_path, fixture)
        _write_json(staged_map_path, chart_map)
        staged_site_manifest_path = staged_site_path / "site_manifest.json"
        if not staged_site_manifest_path.is_file():
            raise AssemblyError("renderer did not produce site_manifest.json")
        site_manifest = _read_json(context, staged_site_manifest_path.relative_to(context.run_root), label="staged site manifest")
        site_manifest = dict(site_manifest)
        site_manifest["chart_map_sha256"] = _sha256_bytes(staged_map_path.read_bytes())
        non_manifest_site_binding = _site_tree_binding(staged_site_path, exclude={"site_manifest.json"})
        site_manifest["site_file_hashes"] = non_manifest_site_binding["files"]
        site_manifest["site_tree_sha256"] = non_manifest_site_binding["tree_sha256"]
        site_manifest["site_tree_file_count"] = non_manifest_site_binding["file_count"]
        _write_json(staged_site_manifest_path, site_manifest)
        site_binding = _site_tree_binding(staged_site_path)
        receipt = {
            "schema_version": ASSEMBLER_SCHEMA,
            "status": "complete",
            "run_id": context.run_id,
            "generation_id": generation_id,
            "source_policy": "accepted_and_committed_only",
            "new_analytics": False,
            "input_items": [
                {"item_id": item_id, "accepted_content_hash": loaded[item_id]["accepted_content_hash"], "accepted_manifest_hash": loaded[item_id]["accepted_manifest_hash"], "integration_manifest_hash": loaded[item_id]["integration_manifest_hash"], "record_count": len(loaded[item_id]["records"])}
                for item_id in selected_ids
            ],
            "plan_binding": plan_binding,
            "outputs": {"fixture_ref": fixture_run_ref, "chart_map_ref": chart_map_run_ref, "chart_registry_ref": registry_run_ref, "site_ref": site_run_ref, "receipt_ref": receipt_run_ref},
            "output_hashes": {
                "fixture_sha256": _sha256_bytes(staged_fixture_path.read_bytes()),
                "chart_map_sha256": _sha256_bytes(staged_map_path.read_bytes()),
                "chart_registry_sha256": registry_info["sha256"],
                "site_manifest_sha256": _sha256_bytes(staged_site_manifest_path.read_bytes()),
            },
            "site_binding": site_binding,
            "freeze_inputs": projection_metadata,
            "widget_count": len(widgets),
            "ontology_counts": {"objects": len(nodes), "relationships": len(relationships), "groups": len(ontology_groups), "summary": lem_summary},
            "retry": "idempotent only when the existing receipt/input hashes match; conflicting output namespaces fail closed",
        }
        if generation_id == "G-0001":
            receipt["parent"] = {
                "root_generation": True,
                "parent_generation_id": None,
                "parent_manifest_ref": None,
                "parent_manifest_hash": None,
            }
        # Keep plan metadata on receipts that actually bind a manager plan.
        # Historical no-plan receipts remain consumable by the legacy parent
        # bridge; their fixture is explicitly all-audit and carries no
        # admitted IDs, so omitting absent plan fields is semantically exact.
        if presentation_plan is not None:
            receipt.update({
                "presentation_plan_ref": resolved_presentation_plan_ref,
                "presentation_plan_sha256": presentation_plan_sha256,
                "manager_widget_ids": list(manager_widget_ids),
            })
        _write_json(staged_receipt_path, receipt)
        # Receipt hash is intentionally not self-referential; every file is
        # complete before publication.  An unchanged candidate is a true
        # idempotent retry.  A changed candidate is allowed to replace only
        # this product namespace; accepted answers, integration, LEM, and
        # source artifacts are never touched by the swap.
        if existing_receipt is not None and existing_receipt == receipt:
            shutil.rmtree(staging_root, ignore_errors=True)
            return existing_receipt
        _publish_staged_output(staging_root, output_root)
        return receipt
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=False, help="products-relative reproducibility output directory")
    parser.add_argument("--presentation-plan-ref", required=False, help="run-relative business presentation plan")
    parser.add_argument("--write-presentation-plan", action="store_true", help="write an explicit manager presentation plan")
    parser.add_argument("--presentation-inventory-fixture-ref", required=False, help="export a read-only candidate inventory")
    parser.add_argument("--presentation-visual-inventory", action="store_true", help="export the read-only V2 visual inventory")
    parser.add_argument("--presentation-fixture-ref", required=False, help="run-relative candidate fixture for plan selection")
    parser.add_argument("--reviewer-ref", required=False, help="reviewer reference for an explicit presentation plan")
    parser.add_argument("--manager-entry-json", action="append", default=[], help="JSON file containing one pointer-bound manager entry (repeatable)")
    parser.add_argument("--revise-presentation-plan-v2", action="store_true", help="CAS-replace the expected V1 plan with a canonical V2 successor")
    parser.add_argument("--record-presentation-plan-v2", action="store_true", help="create the active generation's absent canonical V2 plan")
    parser.add_argument("--successor-plan-json", required=False, help="JSON path for the V2 successor plan")
    parser.add_argument("--expected-current-plan-sha256", required=False)
    parser.add_argument("--expected-successor-plan-sha256", required=False)
    parser.add_argument("--presentation-chart-map-ref", required=False, help="run-relative active-generation V2 chart map")
    parser.add_argument("--presentation-previous-plan-ref", required=False, help="run-relative immediate predecessor V2 plan")
    parser.add_argument("--expected-fixture-sha256", required=False)
    parser.add_argument("--expected-chart-map-sha256", required=False)
    parser.add_argument("--expected-previous-plan-sha256", required=False)
    args = parser.parse_args(argv)
    try:
        context = RunContext(run_id=args.run_id, run_root=args.run_root)
        if args.revise_presentation_plan_v2:
            if not args.presentation_plan_ref or not args.successor_plan_json or not args.expected_current_plan_sha256 or not args.expected_successor_plan_sha256:
                raise BusinessPresentationPlanError("v2 revision requires --presentation-plan-ref, --successor-plan-json, and both expected hashes")
            result = revise_business_presentation_plan_v2(
                context,
                successor_plan=args.successor_plan_json,
                expected_current_plan_sha256=args.expected_current_plan_sha256,
                expected_successor_plan_sha256=args.expected_successor_plan_sha256,
                presentation_plan_ref=args.presentation_plan_ref,
            )
        elif args.record_presentation_plan_v2:
            required = (
                args.presentation_fixture_ref,
                args.presentation_chart_map_ref,
                args.presentation_previous_plan_ref,
                args.reviewer_ref,
                args.expected_fixture_sha256,
                args.expected_chart_map_sha256,
                args.expected_previous_plan_sha256,
                args.expected_successor_plan_sha256,
            )
            if any(value is None for value in required):
                raise BusinessPresentationPlanError(
                    "direct V2 recording requires fixture/map/previous refs, reviewer, and all expected source/successor hashes"
                )
            result = record_business_presentation_plan_v2(
                context,
                fixture_ref=args.presentation_fixture_ref,
                chart_map_ref=args.presentation_chart_map_ref,
                previous_plan_ref=args.presentation_previous_plan_ref,
                reviewer_ref=args.reviewer_ref,
                expected_fixture_sha256=args.expected_fixture_sha256,
                expected_chart_map_sha256=args.expected_chart_map_sha256,
                expected_previous_plan_sha256=args.expected_previous_plan_sha256,
                expected_successor_plan_sha256=args.expected_successor_plan_sha256,
                presentation_plan_ref=args.presentation_plan_ref,
            )
        elif args.presentation_visual_inventory:
            if not args.presentation_inventory_fixture_ref:
                raise BusinessPresentationPlanError("v2 visual inventory requires --presentation-inventory-fixture-ref")
            result = business_presentation_visual_inventory(
                context,
                fixture_ref=args.presentation_inventory_fixture_ref,
            )
        elif args.presentation_inventory_fixture_ref:
            result = business_presentation_inventory(
                context,
                fixture_ref=args.presentation_inventory_fixture_ref,
            )
        elif args.write_presentation_plan:
            if not args.presentation_fixture_ref or not args.reviewer_ref or not args.manager_entry_json:
                raise BusinessPresentationPlanError("plan writing requires --presentation-fixture-ref, --reviewer-ref, and --manager-entry-json")
            manager_entries = []
            for entry_ref in args.manager_entry_json:
                entry_path = Path(entry_ref)
                if entry_path.is_symlink() or not entry_path.is_file():
                    raise BusinessPresentationPlanError(f"manager entry JSON is missing or symlinked: {entry_ref}")
                raw_entry = json.loads(entry_path.read_text(encoding="utf-8"))
                if not isinstance(raw_entry, Mapping):
                    raise BusinessPresentationPlanError(f"manager entry JSON must be an object: {entry_ref}")
                manager_entries.append(dict(raw_entry))
            result = write_business_presentation_plan(
                context,
                manager_entries=manager_entries,
                reviewer_ref=args.reviewer_ref,
                fixture_ref=args.presentation_fixture_ref,
                presentation_plan_ref=args.presentation_plan_ref,
            )
        else:
            if not args.output_dir:
                raise AssemblyError("--output-dir is required for dashboard assembly")
            result = assemble_dashboard(context, output_dir=args.output_dir, presentation_plan_ref=args.presentation_plan_ref)
    except (OSError, ValueError, AssemblyError, BusinessPresentationPlanError, AllowedRootError) as exc:
        print(f"dashboard assembler: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
