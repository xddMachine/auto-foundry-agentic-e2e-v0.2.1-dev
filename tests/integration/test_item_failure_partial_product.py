"""Mixed-result product/report regressions for requirement-local failures."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import pytest
import sys
from typing import Any

from auto_foundry_core import (
    ItemWorkspace,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementSupervisorWorkspace,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.reporting import ReportPreflightError, RunReportInputGatherer
from auto_foundry_core.requirement_planning import inspect_committed_integration


ROOT = Path(__file__).resolve().parents[2]
ASSEMBLER_PATH = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_assembler.py"
_assembler_spec = importlib.util.spec_from_file_location("dashboard_assembler_item_failure_test", ASSEMBLER_PATH)
assert _assembler_spec and _assembler_spec.loader
dashboard_assembler = importlib.util.module_from_spec(_assembler_spec)
sys.modules[_assembler_spec.name] = dashboard_assembler
_assembler_spec.loader.exec_module(dashboard_assembler)


def _write_canonical_json(path: Path, value: Any) -> None:
    path.write_bytes(
        (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    )


def _write_receipt(workspace: ItemWorkspace) -> None:
    """Create one accepted, hash-bound execution receipt for the good item."""

    script_path = workspace.item_root / "work" / "calc.py"
    context_path = workspace.item_root / "work" / "analysis_context.json"
    receipt_path = workspace.item_root / "work" / "script_receipts" / "receipt-good.json"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("print('ok')\n", encoding="utf-8")
    context_path.write_text(json.dumps({"item_id": workspace.item_id}) + "\n", encoding="utf-8")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "receipt_id": "receipt-good",
        "receipt_path": str(receipt_path),
        "script_path": str(script_path),
        "context_path": str(context_path),
        "phase": "full",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "wall_seconds": 1.0,
        "exit_code": 0,
        "output_hashes": {},
        "script_hash": hashlib.sha256(script_path.read_bytes()).hexdigest(),
        "source_hash": "a" * 64,
        "context_hash": hashlib.sha256(context_path.read_bytes()).hexdigest(),
    }
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _accept_item(context: RunContext, item_id: str, *, receipt: bool = False) -> ItemWorkspace:
    workspace = ItemWorkspace.create(
        context,
        item_id,
        mode="requirement",
        original_text=f"Synthetic requirement {item_id}",
    )
    workspace.write_plan({"item_id": item_id, "offline": True})
    if receipt:
        _write_receipt(workspace)
    workspace.write_draft(
        {
            "item_id": item_id,
            "answer": f"accepted answer {item_id}",
            "limitations": ["synthetic only"],
        }
    )
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    return workspace


def _seed_mixed_run(tmp_path: Path, *, accepted_integration_failure: bool) -> tuple[RunContext, dict[str, Any]]:
    run_id = "RUN-ACCEPTED-INTEGRATION-FAILURE" if accepted_integration_failure else "RUN-PREACCEPT-FAILURE"
    context = RunContext(run_id, tmp_path / "run")
    lifecycle = RunLifecycle.create(context, ("REQ-FAILED", "REQ-GOOD"), mode="requirement")
    telemetry = context.run_root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text("", encoding="utf-8")

    if accepted_integration_failure:
        failed = _accept_item(context, "REQ-FAILED")
        failed_session = IntegrationSession.create(
            context,
            failed,
            PreparedAssetRegistry(context),
            "synthetic-integration",
            invocation_id="inv-failed",
        )
        failed_session.finalize_technical_failure("integration recovery exhausted")
    else:
        failed = ItemWorkspace.create(
            context,
            "REQ-FAILED",
            mode="requirement",
            original_text="Synthetic requirement REQ-FAILED",
        )
        failed.technical_failure("analysis transport exhausted", recovery_exhausted=True)

    good = _accept_item(context, "REQ-GOOD", receipt=True)
    good_session = IntegrationSession.create(
        context,
        good,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-good",
    )
    good_session.add_metric(
        metric_id="metric-good",
        scope="REQ-GOOD",
        evidence_refs=("answer_content.json",),
        label="Reviewed good metric",
        units="records",
        value=7,
        population=10,
    )
    good_session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in good_session.records),
    )
    good_session.commit()

    (context.run_root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "id": "synthetic",
                        "title": "Synthetic requirements",
                        "requirement_ids": ["REQ-FAILED", "REQ-GOOD"],
                    }
                ]
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    receipt = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="products/mixed-result-dashboard",
    )
    fixture = json.loads(
        (context.run_root / "products/mixed-result-dashboard/dashboard_fixture_v4.json").read_text(
            encoding="utf-8"
        )
    )
    # The reporting adapter consumes the active root product manifest.  This
    # compact test boundary binds it to the assembler's successful-item LEM
    # projection while preserving the limited mixed-result status.
    product_manifest = {
        "run_id": context.run_id,
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "status": "complete_with_limits",
        "terminal": True,
        "lifecycle": {
            "generation_id": lifecycle.generation_id,
            "all_items_terminal": True,
            "all_items_integrated": True,
        },
        "lem": fixture["ontology_summary"],
        "assets": [],
    }
    product_root = context.run_root / "products"
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "product_manifest.json").write_text(
        json.dumps(product_manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    # Item-local terminal facts drive the lifecycle; one reconcile pass moves
    # through the valid aggregate states when the product is terminal.
    final_lifecycle = RunLifecycle.load(context).reconcile_from_run(
        product_terminal_status={"status": "complete_with_limits", "terminal": True}
    )
    assert final_lifecycle.state == "complete_with_limits"
    return context, {"receipt": receipt, "fixture": fixture}


def _seed_mixed_planner_run(tmp_path: Path) -> RunContext:
    """Build mixed terminal items behind the real Planner boundary."""

    context = RunContext("RUN-PLANNER-PREACCEPT-FAILURE", tmp_path / "planner-run")
    item_ids = ("REQ-FAILED", "REQ-GOOD")
    RunLifecycle.create(context, item_ids, mode="requirement")
    failed = ItemWorkspace.create(
        context,
        "REQ-FAILED",
        mode="requirement",
        original_text="Synthetic failed requirement",
    )
    failed.technical_failure("analysis transport exhausted", recovery_exhausted=True)
    good = _accept_item(context, "REQ-GOOD", receipt=True)
    good_session = IntegrationSession.create(
        context,
        good,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-good-planner",
    )
    good_session.add_metric(
        metric_id="metric-good-planner",
        scope="REQ-GOOD",
        evidence_refs=("answer_content.json",),
        label="Reviewed good metric",
        units="records",
        value=7,
        population=10,
    )
    good_session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in good_session.records),
    )
    good_session.commit()
    records = tuple(
        RequirementRecord(
            requirement_id=item_id,
            original_text=f"Synthetic requirement {item_id}",
            business_objective=f"Support {item_id}",
            expected_analytical_outputs=(f"output-{item_id}",),
            status="queued",
        )
        for item_id in item_ids
    )
    RequirementSupervisorWorkspace(context).save(
        RequirementExecutionPlan(
            input_records=records,
            groups=tuple(
                RequirementExecutionGroup((item_id,), f"Run {item_id} independently.")
                for item_id in item_ids
            ),
            planner_ref="synthetic-planner",
            portfolio_strategy="preserve independent requirement order",
            revision=1,
        )
    )
    return context


def test_planner_routes_product_after_mixed_preacceptance_failure_without_reopen(tmp_path: Path) -> None:
    """Planner settles the failed item and emits product work for the good one."""

    context = _seed_mixed_planner_run(tmp_path)
    actions = RequirementSupervisorWorkspace(context).next_actions()
    assert any(action.action in {"build_product_candidate", "build_final_product"} for action in actions)
    assert not any(
        action.action in {"repair_integration_fidelity", "review_integration_fidelity"}
        and action.subject_id == "REQ-FAILED"
        for action in actions
    )


def test_preacceptance_technical_failure_is_omitted_from_analytics_but_visible_in_partial_dashboard_and_report(
    tmp_path: Path,
) -> None:
    context, outputs = _seed_mixed_run(tmp_path, accepted_integration_failure=False)
    fixture = outputs["fixture"]
    failed = {entry["item_id"] for entry in fixture["failed_items"]}
    assert failed == {"REQ-FAILED"}
    assert {widget["requirement_id"] for widget in fixture["widgets"]} == {"REQ-GOOD"}
    assert all(record.get("scope") != "REQ-FAILED" for record in fixture["audit_records"])

    preflight = RunReportInputGatherer.gather_from_run(context, persist=False)
    report = preflight.projected_report
    assert report["lifecycle_status"] == "complete_with_limits"
    items = {item["item_id"]: item for item in report["items"]}
    assert items["REQ-FAILED"]["outcome"] == "technical_failure"
    assert items["REQ-FAILED"]["integration_state"] == "pending"
    assert items["REQ-FAILED"]["failure"]["recovery_exhausted"] is True
    assert items["REQ-GOOD"]["record_kind_totals"]["metric"] == 1
    assert report["timing_count"] == 1


def test_preacceptance_report_rejects_rehashed_terminal_failure_progress(
    tmp_path: Path,
) -> None:
    context, _outputs = _seed_mixed_run(tmp_path, accepted_integration_failure=False)
    workspace = ItemWorkspace.load(context, "REQ-FAILED", mode="requirement")
    manifest_path = workspace.accepted_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Forge a self-consistent outer content/manifest hash and state pointers,
    # but make the artifact progress non-canonical (duplicate file entry).
    # Reporting's former shape-only validator accepted this; the durable/core
    # validator must reject it before any report projection is trusted.
    forged_progress = dict(manifest["artifact_progress"])
    forged_progress["files"] = ["forged.json", "forged.json"]
    forged_progress["hashes"] = {"forged.json": "b" * 64}
    manifest["artifact_progress"] = forged_progress
    manifest["hashes"] = dict(forged_progress["hashes"])
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key not in {"content_hash", "manifest_hash"}
    }
    manifest["content_hash"] = hashlib.sha256(
        (
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    manifest["manifest_hash"] = hashlib.sha256(
        (
            json.dumps(
                {key: value for key, value in manifest.items() if key != "manifest_hash"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    _write_canonical_json(manifest_path, manifest)

    state_path = workspace.item_root / "item_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["terminal_intent"]["manifest_hash"] = manifest["manifest_hash"]
    state["terminal_outcome"]["content_hash"] = manifest["content_hash"]
    _write_canonical_json(state_path, state)

    with pytest.raises(ValueError, match="accepted snapshot"):
        ItemWorkspace.load(context, "REQ-FAILED", mode="requirement")
    with pytest.raises(ValueError, match="accepted snapshot"):
        inspect_committed_integration(context, "REQ-FAILED", raise_on_error=True)
    with pytest.raises(ReportPreflightError, match="terminal failure integration boundary"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


def test_accepted_integration_failure_retains_business_output_and_is_visible_in_partial_dashboard_and_report(
    tmp_path: Path,
) -> None:
    context, outputs = _seed_mixed_run(tmp_path, accepted_integration_failure=True)
    fixture = outputs["fixture"]
    failed = {entry["item_id"] for entry in fixture["failed_items"]}
    assert failed == {"REQ-FAILED"}
    assert {widget["requirement_id"] for widget in fixture["widgets"]} == {"REQ-GOOD"}
    assert all(record.get("scope") != "REQ-FAILED" for record in fixture["audit_records"])

    failed_workspace = ItemWorkspace.load(context, "REQ-FAILED", mode="requirement")
    assert failed_workspace.state["terminal_outcome"]["outcome"] == "accepted"
    accepted_answer = json.loads(
        (failed_workspace.accepted_root / "answer_content.json").read_text(encoding="utf-8")
    )
    assert accepted_answer["answer"] == "accepted answer REQ-FAILED"
    assert not (failed_workspace.item_root / "integration" / "committed" / "records.jsonl").exists()

    preflight = RunReportInputGatherer.gather_from_run(context, persist=False)
    report = preflight.projected_report
    assert report["lifecycle_status"] == "complete_with_limits"
    items = {item["item_id"]: item for item in report["items"]}
    assert items["REQ-FAILED"]["outcome"] == "accepted"
    assert items["REQ-FAILED"]["integration_state"] == "technical_failure"
    assert items["REQ-FAILED"]["integration_failure"]["recovery_exhausted"] is True
    assert items["REQ-FAILED"]["record_kind_totals"] == {}
    assert items["REQ-GOOD"]["record_kind_totals"]["metric"] == 1
    assert report["timing_count"] == 1
