#!/usr/bin/env python3
"""Create a receipt-bound bridge for a legacy G-0001 dashboard product.

Older completed Requirement Mode runs predate the receipt binding required by
the same-run dashboard delta assembler.  This module creates one derived
manifest under ``products/generations/G-0001`` while leaving the legacy
``products/product_manifest.json`` byte-identical.  It only reads frozen
product/receipt artifacts; it never reads sources, workspaces, or calculation
outputs.

The bridge is deliberately narrow.  A complete, canonical legacy root
manifest and a complete ordinary assembler receipt must agree on run identity,
freeze markers, and the root manifest hash.  Every receipt output is checked
against its declared hash, including the complete site tree.  Publication is
serialized by a generation-scoped advisory lock and uses a durable atomic
JSON replacement.  Existing bridge bytes are returned only when the exact
same validated inputs reconstruct the exact same manifest.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterator, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported hosts provide fcntl
    fcntl = None  # type: ignore[assignment]

try:
    from auto_foundry_core.product_contracts import validate_product_manifest
    from auto_foundry_core.workspace import AllowedRootError, RunContext
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from auto_foundry_core.product_contracts import validate_product_manifest
    from auto_foundry_core.workspace import AllowedRootError, RunContext


BRIDGE_SCHEMA = "dashboard.legacy_parent_bridge.v1"
ASSEMBLER_SCHEMA = "dashboard.assembler_receipt.v1"
FIXTURE_SCHEMA = "dashboard.reviewed_fixture.v4"
CHART_MAP_SCHEMA = "dashboard.chart_map.v4"
CHART_REGISTRY_SCHEMA = "dashboard.chart_registry.v1"
LEGACY_GENERATION_ID = "G-0001"
LEGACY_GENERATION_ORDINAL = 1
ROOT_MANIFEST_REF = "products/product_manifest.json"
DEFAULT_RECEIPT_REF = "products/repro_dashboard_v4_auto_refined_final6/build_receipt.json"

_FREEZE_KEYS = frozenset(
    {
        "answers_frozen",
        "living_enterprise_model_frozen",
        "prepared_data_registry_frozen",
        "dashboard_frozen",
        "telemetry_frozen",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "run_id",
        "generation_id",
        "source_policy",
        "new_analytics",
        "input_items",
        "plan_binding",
        "parent",
        "outputs",
        "output_hashes",
        "site_binding",
        "freeze_inputs",
        "widget_count",
        "ontology_counts",
        "retry",
    }
)
_ROOT_PARENT_KEYS = frozenset(
    {"root_generation", "parent_generation_id", "parent_manifest_ref", "parent_manifest_hash"}
)
_ROOT_PLAN_BINDING_KEYS = frozenset({"ref", "sha256", "admission_sha256", "generation_id"})
_RECEIPT_OUTPUT_KEYS = frozenset(
    {"fixture_ref", "chart_map_ref", "chart_registry_ref", "site_ref", "receipt_ref"}
)
_RECEIPT_HASH_KEYS = frozenset(
    {"fixture_sha256", "chart_map_sha256", "chart_registry_sha256", "site_manifest_sha256"}
)
_FREEZE_INPUT_KEYS = frozenset(
    {
        "bindings",
        "export_sha256",
        "freeze_markers",
        "item_order",
        "prepared_index",
        "prepared_registry",
        "product_manifest_ref",
        "product_manifest_sha256",
        "projection_hash",
        "summary",
        "telemetry",
    }
)
_PREPARED_KEYS = frozenset({"ref", "present", "sha256", "descriptor_count"})
_TELEMETRY_KEYS = frozenset({"sha256", "assets"})
_BRIDGE_KEYS = frozenset(
    {
        "schema_version",
        "product_type",
        "bridge_type",
        "run_id",
        "status",
        "terminal",
        "source_status",
        "new_analytics",
        "freeze_markers",
        "lifecycle",
        "dashboard",
        "lem",
        "assets",
        "lineage",
        "limitations",
    }
)
_BRIDGE_LIFECYCLE_KEYS = frozenset(
    {"generation_id", "generation_ordinal", "all_items_terminal", "all_items_integrated", "state_at_product_freeze"}
)
_BRIDGE_DASHBOARD_KEYS = frozenset(
    {
        "assets_local",
        "internal_links_checked",
        "external_asset_refs",
        "missing_evidence_refs",
        "domain_count",
        "widget_count",
        "receipt_ref",
        "receipt_sha256",
    }
)
_BRIDGE_LINEAGE_KEYS = frozenset(
    {
        "bridge_schema",
        "generation_id",
        "historical_telemetry",
        "legacy_root_manifest_ref",
        "legacy_root_manifest_sha256",
        "receipt_ref",
        "receipt_sha256",
        "receipt_freeze_product_manifest_ref",
        "receipt_freeze_product_manifest_sha256",
        "source_policy",
    }
)
_PRODUCT_ASSET_ROLES = (
    ("fixture_ref", "reviewed_dashboard_fixture", "fixture_sha256"),
    ("chart_map_ref", "dashboard_chart_map", "chart_map_sha256"),
    ("chart_registry_ref", "dashboard_chart_registry", "chart_registry_sha256"),
)


class LegacyParentBridgeError(ValueError):
    """Raised when a legacy product cannot be safely bridged."""


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, label: str) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise LegacyParentBridgeError(f"{label} cannot be read: {path}") from exc


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _lexical_path(context: RunContext, reference: str | Path, *, product: bool, label: str) -> Path:
    raw = Path(reference).expanduser()
    base = context.product_root if product else context.run_root
    if any(part in {".."} for part in raw.parts):
        raise LegacyParentBridgeError(f"{label} contains traversal: {reference}")
    candidate = raw if raw.is_absolute() else base / raw
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise LegacyParentBridgeError(f"{label} escapes its boundary: {reference}") from exc
    if base.is_symlink():
        raise LegacyParentBridgeError(f"{label} root is symlinked: {base}")
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LegacyParentBridgeError(f"{label} uses a symlink alias: {reference}")
    return candidate


def _safe_product_path(context: RunContext, reference: str | Path, *, label: str) -> Path:
    _lexical_path(context, reference, product=True, label=label)
    try:
        return context.resolve_product_path(reference)
    except (AllowedRootError, OSError, ValueError) as exc:
        raise LegacyParentBridgeError(f"{label} is outside the product boundary: {reference}") from exc


def _safe_run_path(context: RunContext, reference: str | Path, *, label: str) -> Path:
    _lexical_path(context, reference, product=False, label=label)
    try:
        return context.resolve_run_path(reference)
    except (AllowedRootError, OSError, ValueError) as exc:
        raise LegacyParentBridgeError(f"{label} is outside the run boundary: {reference}") from exc


def _read_json(path: Path, *, label: str, canonical: bool = False) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LegacyParentBridgeError(f"{label} is missing or symlinked: {path}")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyParentBridgeError(f"{label} is invalid: {path}") from exc
    if not isinstance(value, Mapping):
        raise LegacyParentBridgeError(f"{label} must be a JSON object: {path}")
    if canonical and raw != _canonical_bytes(value):
        raise LegacyParentBridgeError(f"{label} is not canonical: {path}")
    return value


def _site_tree_binding(site_root: Path) -> dict[str, Any]:
    if site_root.is_symlink() or not site_root.is_dir():
        raise LegacyParentBridgeError(f"receipt site output is missing or symlinked: {site_root}")
    files: dict[str, str] = {}
    try:
        entries = sorted(site_root.rglob("*"), key=lambda value: value.relative_to(site_root).as_posix())
        for path in entries:
            relative = path.relative_to(site_root).as_posix()
            if path.is_symlink():
                raise LegacyParentBridgeError(f"receipt site contains a symlink: {relative}")
            if path.is_file():
                files[relative] = _sha256_file(path, label="receipt site file")
    except OSError as exc:
        raise LegacyParentBridgeError(f"receipt site cannot be enumerated: {site_root}") from exc
    if not files:
        raise LegacyParentBridgeError("receipt site contains no files")
    return {"files": files, "tree_sha256": _sha256_bytes(_canonical_bytes(files)), "file_count": len(files)}


def _validate_freeze_markers(value: Any, *, label: str) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != _FREEZE_KEYS:
        raise LegacyParentBridgeError(f"{label} freeze marker schema is invalid")
    if any(type(value[field]) is not bool for field in _FREEZE_KEYS) or any(value[field] is not True for field in _FREEZE_KEYS):
        raise LegacyParentBridgeError(f"{label} freeze markers are not all true")
    try:
        validate_product_manifest({"freeze_markers": dict(value)}, require_all=True)
    except Exception as exc:
        raise LegacyParentBridgeError(f"{label} freeze markers are invalid") from exc
    return {field: value[field] for field in sorted(_FREEZE_KEYS)}


def _validate_root_manifest(context: RunContext) -> tuple[Path, Mapping[str, Any], str]:
    path = _safe_run_path(context, ROOT_MANIFEST_REF, label="legacy root product manifest")
    # Historical root manifests are valid but may use human-readable
    # indentation.  Their exact raw bytes remain the lineage authority: the
    # bridge records the SHA of this file, while only the newly generated
    # bridge and receipt artifacts are required to be canonical JSON.
    value = _read_json(path, label="legacy root product manifest", canonical=False)
    required = {
        "schema_version",
        "run_id",
        "status",
        "terminal",
        "product_type",
        "source_status",
        "new_analytics",
        "freeze_markers",
        "lifecycle",
        "assets",
        "lem",
        "dashboard",
        "limitations",
    }
    if not required.issubset(set(value)):
        raise LegacyParentBridgeError("legacy root product manifest is incomplete")
    if value.get("schema_version") != "1" or value.get("product_type") != "reviewed_run_product_bundle" or value.get("source_status") != "reviewed_outputs_only":
        raise LegacyParentBridgeError("legacy root product manifest identity is invalid")
    if value.get("run_id") != context.run_id or value.get("status") not in {"complete", "complete_with_limits"} or value.get("terminal") is not True or value.get("new_analytics") is not False:
        raise LegacyParentBridgeError("legacy root product manifest is not terminal")
    freeze = _validate_freeze_markers(value.get("freeze_markers"), label="legacy root product manifest")
    lifecycle = value.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or lifecycle.get("all_items_terminal") is not True or lifecycle.get("all_items_integrated") is not True:
        raise LegacyParentBridgeError("legacy root product lifecycle is not fully terminal")
    assets = value.get("assets")
    if not isinstance(assets, list) or not assets:
        raise LegacyParentBridgeError("legacy root product assets are missing")
    for asset in assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("ref"), str) or not _is_sha256(asset.get("sha256")):
            raise LegacyParentBridgeError("legacy root product asset binding is invalid")
        asset_path = _safe_run_path(context, asset["ref"], label="legacy root product asset")
        # Telemetry files are mutable operational observations.  Their
        # historical hashes are checked against the immutable receipt below;
        # reading today's bytes here would incorrectly reject a valid legacy
        # parent after a later run appended telemetry.  Keep lexical and
        # symlink safety, but do not compare live telemetry content.
        if _is_telemetry_asset(asset):
            if asset_path.is_symlink():
                raise LegacyParentBridgeError(f"legacy root telemetry asset is symlinked: {asset['ref']}")
            continue
        if asset_path.is_symlink() or not asset_path.is_file() or _sha256_file(asset_path, label="legacy root product asset") != asset["sha256"]:
            raise LegacyParentBridgeError(f"legacy root product asset hash mismatch: {asset['ref']}")
    return path, value, _sha256_file(path, label="legacy root product manifest")


def _is_telemetry_asset(asset: Mapping[str, Any]) -> bool:
    reference = asset.get("ref")
    role = asset.get("role")
    return (
        isinstance(reference, str)
        and (reference == "telemetry" or reference.startswith("telemetry/"))
    ) or (isinstance(role, str) and "telemetry" in role.lower())


def _historical_telemetry_assets(root: Mapping[str, Any]) -> dict[str, str]:
    assets = root.get("assets")
    if not isinstance(assets, list):
        raise LegacyParentBridgeError("legacy root product telemetry bindings are missing")
    historical: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, Mapping) or not _is_telemetry_asset(asset):
            continue
        reference = asset.get("ref")
        digest = asset.get("sha256")
        if not isinstance(reference, str) or not _is_sha256(digest):
            raise LegacyParentBridgeError("legacy root product telemetry binding is invalid")
        if reference in historical and historical[reference] != digest:
            raise LegacyParentBridgeError("legacy root product telemetry binding is conflicting")
        historical[reference] = digest
    if not historical:
        raise LegacyParentBridgeError("legacy root product telemetry bindings are missing")
    return historical


def _validate_historical_telemetry(root: Mapping[str, Any], freeze: Mapping[str, Any]) -> dict[str, Any]:
    historical = _historical_telemetry_assets(root)
    telemetry = freeze.get("telemetry")
    if not isinstance(telemetry, Mapping) or set(telemetry) != _TELEMETRY_KEYS:
        raise LegacyParentBridgeError("legacy parent receipt telemetry binding is incomplete")
    receipt_assets = telemetry.get("assets")
    if not isinstance(receipt_assets, Mapping) or not receipt_assets or any(not isinstance(key, str) or not _is_sha256(value) for key, value in receipt_assets.items()):
        raise LegacyParentBridgeError("legacy parent receipt telemetry assets are invalid")
    if dict(receipt_assets) != historical:
        raise LegacyParentBridgeError("legacy root and receipt telemetry bindings differ")
    if not _is_sha256(telemetry.get("sha256")) or _sha256_bytes(_canonical_bytes(historical)) != telemetry.get("sha256"):
        raise LegacyParentBridgeError("legacy parent receipt telemetry aggregate is invalid")
    return {"assets": dict(historical), "sha256": telemetry["sha256"]}


def _validate_receipt_outputs(
    context: RunContext,
    receipt_path: Path,
    receipt: Mapping[str, Any],
    root_manifest_hash: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]:
    if set(receipt) != _RECEIPT_KEYS:
        raise LegacyParentBridgeError("legacy parent receipt schema fields are not exact")
    if receipt.get("schema_version") != ASSEMBLER_SCHEMA or receipt.get("status") != "complete" or receipt.get("run_id") != context.run_id or receipt.get("generation_id") != LEGACY_GENERATION_ID or receipt.get("source_policy") != "accepted_and_committed_only" or receipt.get("new_analytics") is not False:
        raise LegacyParentBridgeError("legacy parent receipt identity is invalid")
    parent = receipt.get("parent")
    if (
        not isinstance(parent, Mapping)
        or set(parent) != _ROOT_PARENT_KEYS
        or parent.get("root_generation") is not True
        or parent.get("parent_generation_id") is not None
        or parent.get("parent_manifest_ref") is not None
        or parent.get("parent_manifest_hash") is not None
    ):
        raise LegacyParentBridgeError("legacy parent receipt root parent binding is invalid")
    plan_binding = receipt.get("plan_binding")
    if not isinstance(plan_binding, Mapping) or set(plan_binding) != _ROOT_PLAN_BINDING_KEYS:
        raise LegacyParentBridgeError("legacy parent receipt plan binding is not exact")
    if (
        plan_binding.get("ref") != "requirement_supervisor_plan.json"
        or plan_binding.get("generation_id") != LEGACY_GENERATION_ID
        or not _is_sha256(plan_binding.get("sha256"))
        or not _is_sha256(plan_binding.get("admission_sha256"))
    ):
        raise LegacyParentBridgeError("legacy parent receipt plan binding is invalid")
    supervisor_plan_path = _safe_run_path(context, plan_binding["ref"], label="legacy parent supervisor plan")
    supervisor_plan_hash = _sha256_file(supervisor_plan_path, label="legacy parent supervisor plan")
    if plan_binding.get("sha256") != supervisor_plan_hash or plan_binding.get("admission_sha256") != supervisor_plan_hash:
        raise LegacyParentBridgeError("legacy parent receipt plan binding is stale")
    outputs = receipt.get("outputs")
    hashes = receipt.get("output_hashes")
    freeze = receipt.get("freeze_inputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _RECEIPT_OUTPUT_KEYS or not isinstance(hashes, Mapping) or set(hashes) != _RECEIPT_HASH_KEYS or not isinstance(freeze, Mapping) or set(freeze) != _FREEZE_INPUT_KEYS:
        raise LegacyParentBridgeError("legacy parent receipt output bindings are incomplete")
    if (
        not isinstance(freeze.get("prepared_registry"), Mapping)
        or set(freeze["prepared_registry"]) != _PREPARED_KEYS
        or not isinstance(freeze.get("prepared_index"), Mapping)
        or set(freeze["prepared_index"]) != frozenset({"ref", "present", "sha256"})
        or not isinstance(freeze.get("telemetry"), Mapping)
        or set(freeze["telemetry"]) != _TELEMETRY_KEYS
        or not isinstance(freeze.get("summary"), Mapping)
        or not isinstance(freeze.get("bindings"), list)
        or not isinstance(freeze.get("item_order"), list)
    ):
        raise LegacyParentBridgeError("legacy parent receipt freeze inputs are incomplete")
    if receipt.get("outputs", {}).get("receipt_ref") != _relative_run_ref(context, receipt_path):
        raise LegacyParentBridgeError("legacy parent receipt self-reference is invalid")
    if freeze.get("product_manifest_ref") != ROOT_MANIFEST_REF or freeze.get("product_manifest_sha256") != root_manifest_hash:
        raise LegacyParentBridgeError("legacy parent receipt is not bound to the immutable root manifest")
    freeze_markers = _validate_freeze_markers(freeze.get("freeze_markers"), label="legacy parent receipt")
    receipt_path_ref = _relative_run_ref(context, receipt_path)
    for key in ("fixture_ref", "chart_map_ref", "chart_registry_ref", "site_ref", "receipt_ref"):
        reference = outputs.get(key)
        if not isinstance(reference, str) or not reference.startswith("products/"):
            raise LegacyParentBridgeError(f"legacy parent receipt output reference is invalid: {key}")
        output_path = _safe_run_path(context, reference, label=f"legacy parent {key}")
        if key != "site_ref" and (output_path.is_symlink() or not output_path.is_file()):
            raise LegacyParentBridgeError(f"legacy parent output is missing or symlinked: {key}")
        if key == "site_ref" and (output_path.is_symlink() or not output_path.is_dir()):
            raise LegacyParentBridgeError("legacy parent site output is missing or symlinked")
    for key in ("fixture_sha256", "chart_map_sha256", "chart_registry_sha256", "site_manifest_sha256"):
        if not _is_sha256(hashes.get(key)):
            raise LegacyParentBridgeError(f"legacy parent output hash is invalid: {key}")
    fixture_path = _safe_run_path(context, outputs["fixture_ref"], label="legacy parent fixture")
    chart_map_path = _safe_run_path(context, outputs["chart_map_ref"], label="legacy parent chart map")
    chart_registry_path = _safe_run_path(context, outputs["chart_registry_ref"], label="legacy parent chart registry")
    fixture = _read_json(fixture_path, label="legacy parent fixture", canonical=True)
    chart_map = _read_json(chart_map_path, label="legacy parent chart map", canonical=True)
    # The renderer copies the chart registry from its registry source and may
    # preserve that source's human-readable indentation.  Its receipt hash is
    # the authority; unlike the generated fixture/map, registry formatting is
    # not a canonicalisation boundary.
    chart_registry = _read_json(chart_registry_path, label="legacy parent chart registry", canonical=False)
    if fixture.get("schema_version") != FIXTURE_SCHEMA or chart_map.get("schema_version") != CHART_MAP_SCHEMA or chart_registry.get("schema_version") != CHART_REGISTRY_SCHEMA:
        raise LegacyParentBridgeError("legacy parent dashboard output schema is unsupported")
    expected_files = {
        "fixture_sha256": fixture_path,
        "chart_map_sha256": chart_map_path,
        "chart_registry_sha256": chart_registry_path,
    }
    for key, output_path in expected_files.items():
        if _sha256_file(output_path, label=f"legacy parent {key}") != hashes[key]:
            raise LegacyParentBridgeError(f"legacy parent output hash mismatch: {key}")
    site_path = _safe_run_path(context, outputs["site_ref"], label="legacy parent site")
    site_binding = _site_tree_binding(site_path)
    if site_binding != dict(receipt.get("site_binding") or {}):
        raise LegacyParentBridgeError("legacy parent site tree/hash does not match its receipt")
    site_manifest = site_path / "site_manifest.json"
    if site_manifest.is_symlink() or not site_manifest.is_file() or _sha256_file(site_manifest, label="legacy parent site manifest") != hashes["site_manifest_sha256"]:
        raise LegacyParentBridgeError("legacy parent site manifest hash mismatch")
    # The receipt itself is canonical and must not be replaced while a bridge
    # is being derived.  Its hash is intentionally not self-referential.
    receipt_hash = _sha256_file(receipt_path, label="legacy parent receipt")
    return dict(outputs), dict(hashes), dict(freeze), {"freeze_markers": freeze_markers, "fixture": fixture, "chart_map": chart_map}, receipt_hash


def _relative_run_ref(context: RunContext, path: Path) -> str:
    try:
        return path.relative_to(context.run_root).as_posix()
    except ValueError as exc:
        raise LegacyParentBridgeError(f"path escapes run root: {path}") from exc


def _bridge_assets(outputs: Mapping[str, Any], hashes: Mapping[str, Any], receipt_ref: str, receipt_hash: str) -> list[dict[str, str]]:
    assets = [
        {"ref": outputs[key], "role": role, "sha256": hashes[digest_key]}
        for key, role, digest_key in _PRODUCT_ASSET_ROLES
    ]
    assets.extend(
        [
            {
                "ref": str(outputs["site_ref"]).rstrip("/") + "/site_manifest.json",
                "role": "dashboard_site_manifest",
                "sha256": str(hashes["site_manifest_sha256"]),
            },
            {"ref": receipt_ref, "role": "dashboard_receipt", "sha256": receipt_hash},
        ]
    )
    return assets


def _build_bridge(
    context: RunContext,
    *,
    receipt_ref: str | Path,
) -> tuple[dict[str, Any], Path, Path, str]:
    root_path, root, root_hash = _validate_root_manifest(context)
    receipt_path = _safe_run_path(context, receipt_ref, label="legacy parent receipt")
    receipt = _read_json(receipt_path, label="legacy parent receipt", canonical=True)
    outputs, hashes, freeze, details, receipt_hash = _validate_receipt_outputs(context, receipt_path, receipt, root_hash)
    root_markers = _validate_freeze_markers(root.get("freeze_markers"), label="legacy root product manifest")
    if root_markers != details["freeze_markers"]:
        raise LegacyParentBridgeError("legacy root and receipt freeze markers differ")
    historical_telemetry = _validate_historical_telemetry(root, freeze)
    lifecycle = root["lifecycle"]
    summary = freeze.get("summary")
    if not isinstance(summary, Mapping):
        raise LegacyParentBridgeError("legacy parent receipt LEM summary is missing")
    input_items = receipt.get("input_items")
    if not isinstance(input_items, list) or not input_items:
        raise LegacyParentBridgeError("legacy parent receipt input items are missing")
    widget_count = receipt.get("widget_count")
    if type(widget_count) is not int or widget_count < 0:
        raise LegacyParentBridgeError("legacy parent receipt widget count is invalid")
    domains = details["fixture"].get("domains")
    if not isinstance(domains, list) or not domains:
        raise LegacyParentBridgeError("legacy parent fixture domains are missing")
    receipt_ref_rel = _relative_run_ref(context, receipt_path)
    bridge = {
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "bridge_type": BRIDGE_SCHEMA,
        "run_id": context.run_id,
        "status": root["status"],
        "terminal": True,
        "source_status": "reviewed_outputs_only",
        "new_analytics": False,
        "freeze_markers": root_markers,
        "lifecycle": {
            "generation_id": LEGACY_GENERATION_ID,
            "generation_ordinal": LEGACY_GENERATION_ORDINAL,
            "all_items_terminal": True,
            "all_items_integrated": True,
            "state_at_product_freeze": lifecycle.get("state_at_product_freeze"),
        },
        "dashboard": {
            "assets_local": True,
            "internal_links_checked": True,
            "external_asset_refs": 0,
            "missing_evidence_refs": 0,
            "domain_count": len(domains),
            "widget_count": widget_count,
            "receipt_ref": receipt_ref_rel,
            "receipt_sha256": receipt_hash,
        },
        "lem": dict(summary),
        "assets": _bridge_assets(outputs, hashes, receipt_ref_rel, receipt_hash),
        "lineage": {
            "bridge_schema": BRIDGE_SCHEMA,
            "generation_id": LEGACY_GENERATION_ID,
            "historical_telemetry": historical_telemetry,
            "legacy_root_manifest_ref": ROOT_MANIFEST_REF,
            "legacy_root_manifest_sha256": root_hash,
            "receipt_ref": receipt_ref_rel,
            "receipt_sha256": receipt_hash,
            "receipt_freeze_product_manifest_ref": freeze.get("product_manifest_ref"),
            "receipt_freeze_product_manifest_sha256": freeze.get("product_manifest_sha256"),
            "source_policy": receipt.get("source_policy"),
        },
        "limitations": [
            *list(root.get("limitations") or []),
            "This receipt-bound bridge preserves the immutable legacy root product manifest.",
        ],
    }
    if set(bridge) != _BRIDGE_KEYS:
        raise LegacyParentBridgeError("internal bridge schema construction failed")
    return bridge, root_path, receipt_path, root_hash


def validate_legacy_parent_bridge(
    context: RunContext,
    bridge_path: str | Path,
    *,
    receipt_path: str | Path | None = None,
) -> tuple[str, str, str, str]:
    """Validate a canonical G-0001 bridge and return parent lineage fields.

    The expected bridge is reconstructed from the immutable root and receipt,
    so a bridge tamper, stale receipt, or changed output fails closed.
    """

    if not isinstance(context, RunContext):
        raise TypeError("validate_legacy_parent_bridge requires a RunContext")
    bridge = _safe_product_path(context, bridge_path, label="legacy parent bridge")
    value = _read_json(bridge, label="legacy parent bridge", canonical=True)
    expected_receipt = receipt_path or DEFAULT_RECEIPT_REF
    expected, _root_path, receipt_file, _root_hash = _build_bridge(context, receipt_ref=expected_receipt)
    if value != expected:
        raise LegacyParentBridgeError("legacy parent bridge does not match immutable receipt-bound inputs")
    return (
        _relative_run_ref(context, bridge),
        _sha256_file(bridge, label="legacy parent bridge"),
        _relative_run_ref(context, receipt_file),
        _sha256_file(receipt_file, label="legacy parent receipt"),
    )


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _bridge_lock(generation_root: Path) -> Iterator[None]:
    if fcntl is None:
        raise LegacyParentBridgeError("legacy parent bridge requires process file-lock support")
    generation_root.mkdir(parents=True, exist_ok=True)
    if generation_root.is_symlink():
        raise LegacyParentBridgeError("legacy parent bridge generation root is symlinked")
    lock_path = generation_root / ".legacy_parent_bridge.lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fsync(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise LegacyParentBridgeError(f"legacy parent bridge lock cannot be acquired: {lock_path}") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _write_atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise LegacyParentBridgeError("bridge destination unexpectedly exists during publication")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap_legacy_parent_manifest(
    context: RunContext,
    *,
    receipt_ref: str | Path = DEFAULT_RECEIPT_REF,
) -> dict[str, str]:
    """Build or exactly retry the canonical receipt-bound G-0001 bridge."""

    if not isinstance(context, RunContext):
        raise TypeError("bootstrap_legacy_parent_manifest requires a RunContext")
    bridge_path = _safe_product_path(context, f"generations/{LEGACY_GENERATION_ID}/product_manifest.json", label="legacy parent bridge")
    generation_root = bridge_path.parent
    with _bridge_lock(generation_root):
        expected, _root_path, receipt_path, _root_hash = _build_bridge(context, receipt_ref=receipt_ref)
        if bridge_path.exists() or bridge_path.is_symlink():
            if bridge_path.is_symlink() or not bridge_path.is_file():
                raise LegacyParentBridgeError("existing legacy parent bridge is missing or symlinked")
            existing = _read_json(bridge_path, label="existing legacy parent bridge", canonical=True)
            if existing != expected:
                raise LegacyParentBridgeError("existing legacy parent bridge conflicts with receipt-bound inputs")
        else:
            _write_atomic_json(bridge_path, expected)
        bridge_hash = _sha256_file(bridge_path, label="legacy parent bridge")
        return {
            "bridge_ref": _relative_run_ref(context, bridge_path),
            "bridge_sha256": bridge_hash,
            "receipt_ref": _relative_run_ref(context, receipt_path),
            "receipt_sha256": _sha256_file(receipt_path, label="legacy parent receipt"),
            "generation_id": LEGACY_GENERATION_ID,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT_REF)
    args = parser.parse_args(argv)
    try:
        context = RunContext(run_id=args.run_id, run_root=args.run_root)
        result = bootstrap_legacy_parent_manifest(context, receipt_ref=args.receipt)
    except (OSError, ValueError, LegacyParentBridgeError, AllowedRootError) as exc:
        print(f"legacy parent bridge: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


__all__ = [
    "BRIDGE_SCHEMA",
    "DEFAULT_RECEIPT_REF",
    "LEGACY_GENERATION_ID",
    "LegacyParentBridgeError",
    "bootstrap_legacy_parent_manifest",
    "validate_legacy_parent_bridge",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
