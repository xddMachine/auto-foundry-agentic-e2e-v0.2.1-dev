"""Legacy G-0001 receipt-bound bridge contract checks."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


delta_helpers = _load_script(
    "dashboard_delta_legacy_bridge_helpers",
    ROOT / "tests" / "integration" / "test_dashboard_delta_assembler.py",
)
dashboard_assembler = _load_script(
    "dashboard_assembler_legacy_bridge_test",
    ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_assembler.py",
)
bridge = _load_script(
    "dashboard_legacy_parent_bootstrap_test",
    ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_legacy_parent_bootstrap.py",
)

from auto_foundry_core import (  # noqa: E402
    ItemWorkspace,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementSupervisorWorkspace,
    RunContext,
    RunLifecycle,
)


def _seed_receipt_bound_parent(
    tmp_path: Path,
    *,
    pretty_root: bool = False,
) -> tuple[RunContext, tuple, dict, bytes]:
    context = RunContext("RUN-LEGACY-BRIDGE", tmp_path / "run")
    item_ids = ("REQ-A",)
    records = tuple(delta_helpers._record(item_id) for item_id in item_ids)
    lifecycle = RunLifecycle.create(context, item_ids, mode="requirement")
    for item_id, record in zip(item_ids, records):
        delta_helpers._complete_item(
            context,
            ItemWorkspace.create(context, item_id, mode="requirement", original_text=record.original_text),
            f"parent-{item_id}",
        )
    plan = RequirementExecutionPlan(
        input_records=records,
        groups=(RequirementExecutionGroup(item_ids, "Synthetic section 1"),),
        planner_ref="synthetic-planner",
        portfolio_strategy="synthetic-strategy",
        revision=1,
    )
    RequirementSupervisorWorkspace(context).save(plan)
    delta_helpers._telemetry(context)

    note = context.run_root / "legacy-note.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("legacy root\n", encoding="utf-8")
    root_manifest = {
        "schema_version": "1",
        "run_id": context.run_id,
        "status": "complete",
        "terminal": True,
        "product_type": "reviewed_run_product_bundle",
        "source_status": "reviewed_outputs_only",
        "new_analytics": False,
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "lifecycle": {
            "state_at_product_freeze": "integration_complete",
            "all_items_terminal": True,
            "all_items_integrated": True,
        },
        "assets": [
            {
                "ref": "legacy-note.txt",
                "role": "legacy_note",
                "sha256": hashlib.sha256(note.read_bytes()).hexdigest(),
            },
            {
                "ref": "telemetry/events.jsonl",
                "role": "frozen_telemetry_events",
                "sha256": hashlib.sha256((context.run_root / "telemetry/events.jsonl").read_bytes()).hexdigest(),
            },
            {
                "ref": "telemetry/inventory_counters.json",
                "role": "frozen_telemetry_counters",
                "sha256": hashlib.sha256((context.run_root / "telemetry/inventory_counters.json").read_bytes()).hexdigest(),
            },
        ],
        "lem": {},
        "dashboard": {},
        "limitations": [],
    }
    root_path = context.resolve_product_path("product_manifest.json")
    if pretty_root:
        root_path.parent.mkdir(parents=True, exist_ok=True)
        root_path.write_text(json.dumps(root_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        delta_helpers._canonical_write(root_path, root_manifest)
    lifecycle.reconcile_from_run(product_terminal_status="complete")
    assert RunLifecycle.load(context).state == "complete"
    receipt = dashboard_assembler.assemble_dashboard(context, output_dir="products/parent-dashboard")
    root_bytes = context.resolve_product_path("product_manifest.json").read_bytes()
    return context, records, receipt, root_bytes


def test_bridge_exact_retry_and_legacy_root_immutability(tmp_path: Path) -> None:
    context, _records, receipt, root_bytes = _seed_receipt_bound_parent(tmp_path)
    result = bridge.bootstrap_legacy_parent_manifest(
        context,
        receipt_ref=receipt["outputs"]["receipt_ref"],
    )
    assert result["generation_id"] == "G-0001"
    bridge_path = context.resolve_product_path("generations/G-0001/product_manifest.json")
    first_bytes = bridge_path.read_bytes()
    assert bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt["outputs"]["receipt_ref"]) == result
    assert bridge_path.read_bytes() == first_bytes
    assert context.resolve_product_path("product_manifest.json").read_bytes() == root_bytes


def test_pretty_legacy_root_is_bound_by_raw_bytes_without_rewrite(tmp_path: Path) -> None:
    context, _records, receipt, root_bytes = _seed_receipt_bound_parent(tmp_path, pretty_root=True)
    root_path = context.resolve_product_path("product_manifest.json")
    root_mtime = root_path.stat().st_mtime_ns
    assert root_bytes != (json.dumps(json.loads(root_bytes), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()

    result = bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt["outputs"]["receipt_ref"])
    assert result["generation_id"] == "G-0001"
    assert root_path.read_bytes() == root_bytes
    assert root_path.stat().st_mtime_ns == root_mtime

    tampered = json.loads(root_bytes)
    tampered["new_analytics"] = True
    root_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(bridge.LegacyParentBridgeError, match="not terminal|new analytics"):
        bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt["outputs"]["receipt_ref"])


def test_historical_telemetry_drift_is_allowed_but_bindings_and_outputs_are_strict(tmp_path: Path) -> None:
    context, _records, receipt, root_bytes = _seed_receipt_bound_parent(tmp_path)
    root_path = context.resolve_product_path("product_manifest.json")
    events_path = context.resolve_run_path("telemetry/events.jsonl")
    counters_path = context.resolve_run_path("telemetry/inventory_counters.json")
    events_path.write_bytes(events_path.read_bytes() + b'{"event":"later-run"}\n')
    counters_path.write_bytes(counters_path.read_bytes() + b"\n")

    result = bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt["outputs"]["receipt_ref"])
    assert result["generation_id"] == "G-0001"
    assert root_path.read_bytes() == root_bytes

    receipt_path = context.resolve_run_path(receipt["outputs"]["receipt_ref"])
    receipt_value = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_value["freeze_inputs"]["telemetry"]["assets"]["telemetry/events.jsonl"] = "0" * 64
    receipt_path.write_bytes((json.dumps(receipt_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    with pytest.raises(bridge.LegacyParentBridgeError, match="telemetry"):
        bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt["outputs"]["receipt_ref"])

    receipt_path.write_bytes((json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    fixture_path = context.resolve_run_path(receipt["outputs"]["fixture_ref"])
    fixture_path.write_bytes(fixture_path.read_bytes() + b"\n")
    with pytest.raises(bridge.LegacyParentBridgeError, match="output hash|not canonical"):
        bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt["outputs"]["receipt_ref"])


def test_bridge_rejects_conflict_tamper_and_symlink(tmp_path: Path) -> None:
    context, _records, receipt, _root_bytes = _seed_receipt_bound_parent(tmp_path)
    receipt_ref = receipt["outputs"]["receipt_ref"]
    bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt_ref)
    bridge_path = context.resolve_product_path("generations/G-0001/product_manifest.json")
    value = json.loads(bridge_path.read_text(encoding="utf-8"))
    value["status"] = "complete"
    value["limitations"] = ["tampered"]
    bridge_path.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    with pytest.raises(bridge.LegacyParentBridgeError, match="conflicts"):
        bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt_ref)

    bridge_path.unlink()
    bridge_path.symlink_to(context.resolve_product_path("parent-dashboard/build_receipt.json"))
    with pytest.raises(bridge.LegacyParentBridgeError, match="symlinked|symlink"):
        bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt_ref)


def test_delta_succeeds_from_bridge_without_rewriting_legacy_root(tmp_path: Path) -> None:
    context, records, receipt, root_bytes = _seed_receipt_bound_parent(tmp_path)
    bridge.bootstrap_legacy_parent_manifest(context, receipt_ref=receipt["outputs"]["receipt_ref"])
    delta_helpers._append(context, records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    delta_helpers._telemetry(context)
    result = delta_helpers._assemble(
        context,
        {"kind": "existing", "group_id": "group-01"},
        parent_receipt_ref=receipt["outputs"]["receipt_ref"],
    )
    assert result["generation_id"] == "G-0002"
    assert context.resolve_product_path("generations/G-0002/product_manifest.json").is_file()
    assert context.resolve_product_path("product_manifest.json").read_bytes() == root_bytes
    parent = json.loads(context.resolve_product_path("generations/G-0001/product_manifest.json").read_text(encoding="utf-8"))
    assert parent["bridge_type"] == bridge.BRIDGE_SCHEMA
