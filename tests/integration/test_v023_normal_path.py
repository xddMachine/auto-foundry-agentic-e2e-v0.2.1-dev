"""Synthetic offline proof of the v0.2.3 normal program path.

The fixture is intentionally generic and tiny.  It exercises the program
boundaries without a model call, network access, benchmark data, or prose
parsing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import textwrap
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPTS = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dashboard_renderer = _load("v023_dashboard_renderer", SCRIPTS / "dashboard_renderer.py")
evidence_collector = _load("v023_optimizer_collector", SCRIPTS / "optimizer_evidence_collector.py")

from auto_foundry_core import (  # noqa: E402
    AcceptedAnalysisBundle,
    AgentInvocationReceipt,
    BoundAnalysisContext,
    DataAssetRef,
    DataRoomWorkbench,
    FreezeMarkers,
    IntegrationSession,
    InvocationReceiptLedger,
    ItemWorkspace,
    LivingEnterpriseModel,
    OntologyItem,
    PreparedAssetRegistry,
    RunContext,
    RunLifecycle,
    decode_freeze_markers,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _accepted_item(context: RunContext, item_id: str, answer: str) -> ItemWorkspace:
    item = ItemWorkspace.create(context, item_id, original_text=f"Summarize generic item {item_id}.")
    item.write_plan({"item_id": item_id, "offline": True})
    item.write_draft({"answer": answer, "limitations": ["synthetic fixture"]})
    item.record_review("accept", reviewer_ref="synthetic-reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    return item


def test_v023_normal_path_is_offline_and_program_owned(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()

    archive = input_root / "generic-fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("records.csv", "record_id,value\nR-1,3\nR-2,5\n")
        output.writestr("notes.txt", "Synthetic fixture only.\n")
    archive_before = _sha256(archive)
    context = RunContext("RUN-V023-NORMAL", run_root, (input_root,))
    lifecycle = RunLifecycle.create(context, ["Q-001", "Q-002"])
    telemetry = None

    # The canonical catalog is created once and then reused, independent of
    # derived sample/category views.
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    telemetry = workbench.telemetry
    first_catalog = workbench.catalog()
    second_workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive), telemetry=telemetry)
    second_catalog = second_workbench.catalog()
    assert first_catalog == second_catalog
    events = [event.event_type for event in telemetry.events]
    assert events.count("data_room_catalog_created") == 1
    assert events.count("data_room_catalog_reused") >= 1
    record_entry = workbench.data_room.search("records", catalog=first_catalog, limit=1)[0]
    assert workbench.data_room.sample(record_entry, limit=1)[0]["record_id"] == "R-1"
    assert workbench.data_room.categories(record_entry, "record_id", limit=2) == ("R-1", "R-2")

    item = ItemWorkspace.create(context, "Q-001", original_text="Summarize generic records.")
    item.write_plan({"item_id": item.item_id, "route": "controlled-script"})
    item.append_source_map({"source": "records.csv", "catalog": "canonical"})
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        ontology_bundle={"relevant": ("generic-record",)},
        workbench=workbench,
    )
    source_before = archive.read_bytes()
    manifest_before = bound.manifest_path.read_bytes()
    assert bound.source_catalog.source_hash == archive_before
    # Analysis materializes a candidate below this item only.  The accepted
    # registry remains empty until Result Integration commits the accepted
    # item; the candidate sidecar is not a registry entry.
    prepared_descriptor = bound.save_prepared_candidate(
        "generic-reusable",
        record_entry,
        scope="reusable",
        transformations=("bounded_sample",),
        limitations=("Synthetic fixture only",),
    )
    assert prepared_descriptor.scope == "reusable"
    assert bound.prepared_assets.search() == ()

    attempt = item.begin_attempt("lane-lead", "Lead Analyst")
    invalid_script = item.work_root / "calculations" / "invalid.py"
    invalid_script.parent.mkdir(parents=True, exist_ok=True)
    invalid_script.write_text("raise NameError('repair in same attempt')\n", encoding="utf-8")
    invalid_report = bound.script_runner.run_pipeline(invalid_script)
    assert invalid_report.same_attempt_feedback is True
    assert invalid_report.receipts[0].error_type == "NameError"
    assert item.state["execution_recovery_count"] == 0

    corrected_script = item.work_root / "calculations" / "corrected.py"
    corrected_script.write_text(
        textwrap.dedent(
            """
            import json
            from pathlib import Path
            from auto_foundry_core.analysis import load_bound_analysis_context

            context = load_bound_analysis_context()
            payload = {"catalog_entries": context.source_catalog.counts.catalog_entries, "status": "bounded"}
            Path("analysis.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    output = item.work_root / "analysis.json"
    corrected_report = bound.script_runner.run_pipeline(
        corrected_script,
        allowed_outputs=(output,),
        deterministic_outputs=(output,),
        timeout_seconds=3600,
    )
    assert corrected_report.succeeded
    assert corrected_report.deterministic_match is True
    assert [receipt.phase for receipt in corrected_report.receipts] == ["smoke", "full", "full"]
    assert all(receipt.same_attempt_feedback is False for receipt in corrected_report.receipts)
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "bounded"
    assert bound.runner_config["default_timeout_seconds"] == 3600.0
    assert not any(path.suffix == ".pyc" for path in run_root.rglob("*"))
    bound.ensure_valid()
    assert archive.read_bytes() == source_before
    assert bound.manifest_path.read_bytes() == manifest_before

    # Provider/model identity may be unavailable, but the completed receipt is
    # still durable evidence.  No recovery is requested for the coding error.
    ledger = InvocationReceiptLedger(context)
    unavailable = AgentInvocationReceipt(
        "I-V023-UNAVAILABLE",
        item.item_id,
        attempt.attempt_id,
        "lane-lead",
        "Lead Analyst",
        "lead",
        provider="unavailable",
        model="unavailable",
        start="2026-01-01T00:00:00+00:00",
        finish="2026-01-01T00:00:01+00:00",
        terminal_reason="provider_failure",
        provider_error="host did not provide provider identity",
    )
    unavailable_ref = ledger.append(unavailable)
    assert ledger.get(unavailable_ref).provider == "unavailable"
    item.write_draft({"answer": "bounded corrected result", "evidence": "work/analysis.json"})
    item.finish_attempt(attempt.attempt_id, status="completed")
    repair_review = item.record_review(
        "repair_once",
        reviewer_ref="synthetic-reviewer",
        findings=[
            {
                "finding_id": "F-V023-ANSWER",
                "message": "The bounded answer wording needs one targeted correction.",
                "pointers": ["/answer"],
            }
        ],
    )
    assert repair_review["findings"][0]["pointers"] == ["/answer"]
    assert repair_review["targeted_recheck"] is False
    item.use_business_repair()
    assert item.state["business_repair_count"] == 1
    item.write_draft({"answer": "bounded corrected result after one repair", "evidence": "work/analysis.json"})
    targeted_review = item.record_review("accept", reviewer_ref="synthetic-reviewer-rereview")
    assert targeted_review["targeted_recheck"] is True
    assert targeted_review["changed_pointers"] == ["/answer"]
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["evidence"] == "work/analysis.json"
    item.accept(accepted_refs=("work/analysis.json",))
    accepted_bundle = AcceptedAnalysisBundle.load(item)
    answer_before = accepted_bundle.answer_content
    envelope_before = (item.accepted_root / "acceptance_envelope.json").read_bytes()
    assert (item.accepted_root / "answer_content.json").read_bytes() == answer_before
    item2 = _accepted_item(context, "Q-002", "second bounded item")
    assert lifecycle.reconcile([item.state, item2.state]).state == "analytical_complete"

    # Exactly one owner stages all integration kinds through the program API.
    lem = LivingEnterpriseModel(run_id=context.run_id)
    # The fidelity packet is an item-local boundary.  Populate nearby
    # cumulative/sibling surfaces with unmistakable sentinels so the vertical
    # proof catches accidental traversal of those stores.
    lem.add_ontology_item(
        OntologyItem(
            item_id="cumulative-secret",
            item_type="entity",
            label="CUMULATIVE LEM SENTINEL",
        )
    )
    telemetry.record("sentinel", facts={"secret": "CUMULATIVE TELEMETRY SENTINEL"})
    (run_root / "report-sentinel.json").write_text(
        json.dumps({"secret": "CUMULATIVE REPORT SENTINEL"}, sort_keys=True),
        encoding="utf-8",
    )
    registry = workbench.prepared_registry
    assert registry.search() == ()
    session = IntegrationSession.create(
        context,
        item,
        lem,
        registry,
        "result-integration",
        invocation_id="inv-Q-001",
    )
    with pytest.raises(ValueError, match="owned by another"):
        IntegrationSession.create(
            context,
            item,
            lem,
            registry,
            "result-integration",
            invocation_id="inv-Q-001-other",
        )
    entity_a = session.add_ontology_item(
        OntologyItem(item_id="generic-record", item_type="entity", label="Generic record", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    entity_b = session.add_ontology_item(
        OntologyItem(item_id="generic-value", item_type="entity", label="Generic value", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    metric = session.add_metric(
        {"item_id": "generic-total", "label": "Generic total", "value": 8},
        scope="question",
        evidence_refs=("work/analysis.json",),
    )
    claim = session.add_claim("The fixture is bounded.", scope="question", evidence_refs=("work/analysis.json",))
    session.add_limitation("No production interpretation.", scope="question", evidence_refs=("work/analysis.json",))
    session.link_evidence(claim, ("work/analysis.json",), scope="question")
    session.register_prepared_asset(prepared_descriptor, evidence_refs=("work/analysis.json",))
    session.add_relationship(
        {"relationship_id": "generic-record-value", "source_id": "generic-record", "target_id": "generic-value", "label": "contains"},
        scope="question",
        evidence_refs=("work/analysis.json",),
    )
    session.add_dashboard_fact({"fact": "generic-total", "value": 8}, scope="question", evidence_refs=("work/analysis.json",))
    assert session.validate().valid
    packet = session.build_fidelity_packet()
    packet_text = session.fidelity_packet_path.read_text(encoding="utf-8")
    assert packet.answer_content == json.loads(accepted_bundle.answer_content.decode("utf-8"))
    assert packet.answer_content_bytes == accepted_bundle.answer_content
    assert packet.acceptance_envelope == json.loads(envelope_before.decode("utf-8"))
    assert packet.manifest == json.loads((item.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert packet.accepted_content_hash == accepted_bundle.content_hash
    assert packet.accepted_manifest_hash == accepted_bundle.manifest_hash
    for sentinel in (
        "second bounded item",
        "CUMULATIVE LEM SENTINEL",
        "CUMULATIVE TELEMETRY SENTINEL",
        "CUMULATIVE REPORT SENTINEL",
    ):
        assert sentinel not in packet_text
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    manifest = session.commit()
    assert manifest["status"] == "committed"
    assert item.integration_state == "integrated"
    assert len(session.records) == 9
    assert registry.search(prepared_asset_id="generic-reusable", scope="reusable") == (prepared_descriptor,)
    assert {record.kind for record in session.records} == {
        "ontology_item", "metric", "claim", "limitation", "evidence_link",
        "prepared_asset", "relationship", "dashboard_fact",
    }

    session2 = IntegrationSession.create(
        context,
        item2,
        lem,
        registry,
        "result-integration",
        invocation_id="inv-Q-002",
    )
    bound2 = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item2,
        ontology_bundle={"relevant": ("generic-record",)},
        workbench=workbench,
    )
    conflicting_descriptor = bound2.save_prepared_candidate(
        "generic-reusable",
        [{"record_id": "conflict", "value": 99}],
        scope="requirement_scoped",
    )
    with pytest.raises(ValueError, match="different descriptor"):
        session2.register_prepared_asset(conflicting_descriptor)
    assert registry.search(prepared_asset_id="generic-reusable", scope="reusable") == (prepared_descriptor,)
    session2.add_ontology_item(
        OntologyItem(item_id="second-item", item_type="entity", label="Second item", scope="question"),
        evidence_refs=("work/plan.json",),
    )
    accepted_bundle2 = AcceptedAnalysisBundle.load(item2)
    packet2 = session2.build_fidelity_packet()
    assert packet2.answer_content == json.loads(accepted_bundle2.answer_content.decode("utf-8"))
    assert packet2.answer_content_bytes == accepted_bundle2.answer_content
    assert packet2.acceptance_envelope == json.loads(
        (item2.accepted_root / "acceptance_envelope.json").read_text(encoding="utf-8")
    )
    assert packet2.manifest == json.loads(
        (item2.accepted_root / "manifest.json").read_text(encoding="utf-8")
    )
    session2.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session2.records),
    )
    session2.commit()
    assert len(lem.ontology) > 2
    assert registry.search(reusable_only=True, prepared_asset_id="generic-reusable")
    assert registry.load("generic-reusable").rows[0]["record_id"] == "R-1"

    # Lifecycle barriers are program-owned and explicit.
    assert lifecycle.reconcile([item.state, item2.state]).state == "integration_complete"

    fixture = {
        "title": "Generic reviewed product",
        "run_id": context.run_id,
        "skill_version": "0.2.5",
        "freeze_markers": FreezeMarkers(True, True, True, True, True).to_dict(),
        "limitations": ["Synthetic fixture only; no new analytics."],
        "domains": [{
            "id": "generic-domain",
            "title": "Generic domain",
            "order": 1,
            "decision_flow": [{"id": "generic-flow", "title": "Generic decision flow", "order": 1, "widget_ids": ["total-kpi"]}],
        }],
        "widgets": [{
            "id": "total-kpi",
            "type": "kpi",
            "title": "Generic total",
            "value": "8",
            "unit": "fixture units",
            "review_status": "reviewed",
            "reviewed_item_ref": "Q-001",
            "reviewed_output_ref": "questions/Q-001/accepted/answer_content.json",
            "evidence_refs": ["questions/Q-001/accepted/answer_content.json"],
            "trace_refs": ["telemetry/events.jsonl"],
        }],
    }
    fixture_path = run_root / "reviewed_widgets.json"
    fixture_path.write_text(json.dumps(fixture, sort_keys=True), encoding="utf-8")
    rendered = dashboard_renderer.render_fixture(context, "reviewed_widgets.json", "dashboard/index.html", "dashboard/manifest.json")
    assert rendered["freeze_markers"] == decode_freeze_markers(fixture["freeze_markers"]).to_dict()
    assert rendered["decision_flow_order"] == [{"domain_id": "generic-domain", "flow_id": "generic-flow"}]

    product_manifest_path = run_root / "products" / "product_manifest.json"
    product_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    product_manifest_path.write_text(json.dumps({
        "run_id": context.run_id,
        "freeze_markers": FreezeMarkers(True, True, True, True, True).to_dict(),
        "review_routing": {"fresh_sol_review_available": False},
    }, sort_keys=True), encoding="utf-8")
    traces = run_root / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    (traces / "normal-path.md").write_text("bounded context evidence\n", encoding="utf-8")
    optimizer = evidence_collector.collect_evidence(
        context,
        products_manifest="products/product_manifest.json",
        telemetry=("telemetry/events.jsonl",),
        traces=("traces",),
        scripts=("questions/Q-001/work/calculations",),
        analytical_inputs=("reviewed_widgets.json", "questions/Q-001/accepted/answer_content.json", "questions/Q-002/accepted/answer_content.json"),
        analytical_complete=True,
    )
    assert optimizer["optimizer_status"] == "complete"
    assert optimizer["input_hashes_unchanged"] is True
    assert lifecycle.reconcile(
        [item.state, item2.state],
        product_status={"status": "complete"},
        optimizer_status=optimizer,
    ).state == "complete"

    assert archive_before == _sha256(archive)
    assert answer_before == (item.accepted_root / "answer_content.json").read_bytes()
    assert envelope_before == (item.accepted_root / "acceptance_envelope.json").read_bytes()
    assert not (item.item_root / "accepted.json").exists()
