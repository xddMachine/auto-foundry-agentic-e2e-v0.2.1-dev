"""Complete offline proof of the v0.2.3 workbench/durable item path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auto_foundry_core import (
    AcceptedAnalysisBundle,
    AcceptedSnapshot,
    BoundAnalysisContext,
    DataAssetRef,
    DataRoomCatalogEntry,
    DataRoomWorkbench,
    IntegrationSession,
    ITEM_STATE_FIELDS,
    ITEM_STATE_SCHEMA,
    ItemWorkspace,
    LivingEnterpriseModel,
    OntologyItem,
    RunContext,
)
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.lifecycle import AgentInvocationReceipt, InvocationReceiptLedger, RUN_STATES, RunLifecycle
from auto_foundry_core.workspace import AllowedRootError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_offline_workbench_and_durable_vertical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise physical access, durable execution, and terminal reload once."""

    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    sibling_root = tmp_path / "sibling"
    input_root.mkdir()
    sibling_root.mkdir()

    payloads = {
        "orders.csv": b"order_id,amount\nleft-1,10\nleft-2,20\n",
        "entities.jsonl": (
            b'{"entity_id":"left-1","label":"North"}\n'
            b'{"entity_id":"left-2","label":"South"}\n'
        ),
        "notes.md": b"Generic fixture notes\nNo business interpretation is embedded.\n",
        "legacy.bin": b"opaque-bytes\x00\x01\x02",
    }
    archive = input_root / "supplied-fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, payload in payloads.items():
            output.writestr(name, payload)
    source_hash_before = _sha256(archive)

    context = RunContext(
        "RUN-WORKBENCH-VERTICAL",
        run_root,
        (input_root,),
        core_version="0.4.0",
        skill_version="0.3.0",
    )
    RunLifecycle.create(context, ("Q-001",))
    telemetry = TelemetryRecorder(context=context)

    # The run boundary rejects sibling/escaped paths before probing or writing.
    sibling_source = sibling_root / "outside.csv"
    sibling_source.write_text("id\noutside\n", encoding="utf-8")
    with pytest.raises(AllowedRootError):
        context.resolve_input(sibling_source)
    with pytest.raises(AllowedRootError):
        context.resolve_run_path(sibling_root / "escaped.json")

    workbench = DataRoomWorkbench(
        context,
        DataAssetRef.from_path(archive),
        telemetry=telemetry,
    )
    catalog = workbench.catalog()
    assert catalog and all(isinstance(entry, DataRoomCatalogEntry) for entry in catalog)
    order_entry = workbench.data_room.search("orders", catalog=catalog, limit=1)[0]
    assert order_entry.path == "orders.csv"
    assert workbench.data_room.sample(order_entry, limit=2) == (
        {"order_id": "left-1", "amount": "10"},
        {"order_id": "left-2", "amount": "20"},
    )
    assert workbench.data_room.categories(order_entry, "order_id", limit=2) == ("left-1", "left-2")
    opaque_entry = workbench.data_room.search("legacy", catalog=catalog, limit=1)[0]
    assert opaque_entry.member.kind == "opaque"
    with pytest.raises(ValueError, match="explicit materialization"):
        workbench.data_room.read_rows(opaque_entry)
    opaque_destination = workbench.data_room.materialize_opaque(opaque_entry, "work/legacy.bin")
    assert opaque_destination.read_bytes() == payloads["legacy.bin"]
    with pytest.raises(AllowedRootError):
        workbench.data_room.materialize_opaque(opaque_entry, sibling_root / "unsafe.bin")

    # The authoritative item state and work directory exist before any attempt.
    item = ItemWorkspace.create(
        context,
        "Q-001",
        original_text="Summarize the supplied generic fixture.",
        telemetry=telemetry,
    )
    assert (item.item_root / "item_state.json").is_file()
    assert item.work_root.is_dir()
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        ontology_bundle={"relevant": ("orders",)},
        workbench=workbench,
    )
    prepared_descriptor = bound.save_prepared_candidate(
        "orders-prepared",
        order_entry,
        transformations=("bounded_csv_read",),
        limitations=("Generic fixture only",),
    )
    assert prepared_descriptor.prepared_content_hash
    assert prepared_descriptor.row_count == 2
    assert bound.prepared_assets.search() == ()
    attempt = item.begin_attempt("lane-lead", "Lead Analyst")
    first = item.observe_attempt(attempt.attempt_id)
    assert first.action == "materialize_now"
    second = item.observe_attempt(attempt.attempt_id)
    assert second.action == "retry_same_attempt"
    loss_receipt = AgentInvocationReceipt(
        "I-WORKBENCH-LOSS",
        item.item_id,
        attempt.attempt_id,
        "lane-lead",
        "Lead Analyst",
        "lead",
        provider="unavailable",
        model="unavailable",
        start="2026-01-01T00:00:00+00:00",
        finish="2026-01-01T00:00:01+00:00",
        terminal_reason="process_lost",
    )
    ledger = InvocationReceiptLedger(context)
    receipt_ref = ledger.append(loss_receipt)
    item.write_handoff({"next": "resume from the prepared orders asset"})
    with pytest.raises(ValueError, match="stable|unknown|attempt|valid persisted"):
        item.begin_recovery(
            "lane-recovery",
            "Lead Analyst",
            prior_attempt_id=attempt.attempt_id,
            receipt_ref="telemetry/invocation_receipts.jsonl#not-persisted",
        )
    recovery = item.begin_recovery(
        "lane-recovery",
        "Lead Analyst",
        prior_attempt_id=attempt.attempt_id,
        receipt_ref=receipt_ref,
    )
    assert item.state["lifecycle_state"] == "recovering"

    # Execution recovery is a distinct route and does not consume business repair.
    assert recovery.route == "recovery"
    assert item.state["execution_recovery_count"] == 1
    assert item.state["business_repair_count"] == 0

    runner_output = item.work_root / "runner-output.json"
    runner_output.write_text("sentinel", encoding="utf-8")
    calculations_root = item.work_root / "calculations"
    calculations_root.mkdir(parents=True, exist_ok=True)
    changing = calculations_root / "changing.py"
    changing.write_text(
        "import time\nfrom pathlib import Path\nPath('runner-output.json').write_text(str(time.time_ns()), encoding='utf-8')\n",
        encoding="utf-8",
    )
    mismatch = bound.script_runner.run_pipeline(
        changing,
        allowed_outputs=(runner_output,),
        deterministic_outputs=(runner_output,),
        timeout_seconds=3600,
    )
    assert mismatch.status == "failed"
    assert runner_output.read_text(encoding="utf-8") == "sentinel"
    corrected = calculations_root / "corrected.py"
    corrected.write_text(
        "import json\nfrom pathlib import Path\nPath('runner-output.json').write_text(json.dumps({'ok': True}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    corrected_report = bound.script_runner.run_pipeline(
        corrected,
        allowed_outputs=(runner_output,),
        deterministic_outputs=(runner_output,),
        timeout_seconds=3600,
    )
    assert corrected_report.succeeded
    assert not any(path.suffix == ".pyc" for path in run_root.rglob("*"))
    assert item.state["attempts"][-1]["handoff_ref"] == "work/handoff.json"

    item.write_draft({"answer": "bounded fixture summary", "refs": ["orders-prepared"]})
    assert item.observe_attempt(recovery.attempt_id).action == "continue"
    item.finish_attempt(recovery.attempt_id, status="completed")
    repair_review = item.record_review(
        "repair_once",
        reviewer_ref="reviewer-1",
        findings=[
            {
                "finding_id": "F-WORKBENCH-ANSWER",
                "message": "The bounded answer wording needs one targeted correction.",
                "pointers": ["/answer"],
                "semantic_categories": ["answer"],
            }
        ],
    )
    assert repair_review["findings"][0]["pointers"] == ["/answer"]
    assert repair_review["targeted_recheck"] is False
    item.use_business_repair(owner_ref="owner")
    assert item.state["execution_recovery_count"] == 1
    assert item.state["business_repair_count"] == 1

    item.write_draft({"answer": "bounded fixture summary after repair", "refs": ["orders-prepared"]})
    review = item.record_review("accept", reviewer_ref="reviewer-2")
    assert review["targeted_recheck"] is True
    assert review["changed_pointers"] == ["/answer"]
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["refs"] == ["orders-prepared"]
    reviewed_draft = item.draft_root.read_bytes()
    reviewed_draft_hash = hashlib.sha256(reviewed_draft).hexdigest()
    assert review["draft_hash"] == reviewed_draft_hash
    accepted = item.accept(
        knowledge_delta="no_change",
        accepted_refs=("work/handoff.json", "draft.json"),
    )
    assert isinstance(accepted, AcceptedSnapshot)
    assert accepted.outcome == "accepted"
    assert (item.accepted_root / "manifest.json").is_file()
    assert (item.accepted_root / "answer_content.json").read_bytes() == reviewed_draft
    assert (item.accepted_root / "acceptance_envelope.json").is_file()

    reloaded = ItemWorkspace.load(context, "Q-001", telemetry=telemetry)
    assert reloaded.state["lifecycle_state"] == "accepted"
    assert reloaded.state["review"]["draft_hash"] == reviewed_draft_hash
    assert reloaded.state["terminal_outcome"]["content_hash"] == accepted.content_hash
    manifest = json.loads((item.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["content_hash"] == reviewed_draft_hash
    envelope = json.loads((item.accepted_root / "acceptance_envelope.json").read_text(encoding="utf-8"))
    assert envelope["draft_hash"] == reviewed_draft_hash
    with pytest.raises(FileExistsError):
        reloaded.accept()

    # Every physical read and durable transition is passive telemetry only.
    event_types = {event.event_type for event in telemetry.events}
    assert {
        "data_room_archive_read",
        "data_room_catalog_created",
        "data_room_member_read",
        "data_room_prepared_write",
        "item_workspace_create",
        "item_attempt_started",
        "item_artifact_progress",
        "item_recovery_started",
        "item_review_recorded",
        "item_business_repair_used",
        "item_accepted",
    }.issubset(event_types)
    assert not any(
        event.capability_id and event.capability_id.startswith(("agent.", "model."))
        for event in telemetry.events
    )
    assert source_hash_before == _sha256(archive)
    assert json.loads((item.accepted_root / "manifest.json").read_text(encoding="utf-8"))["outcome"] == "accepted"

    # Candidate registration is a post-acceptance integration side effect. A
    # crash after registry publication is retried from the exact intent and
    # converges without mutating immutable accepted answer/envelope bytes.
    registry = bound.prepared_assets
    external_lem = LivingEnterpriseModel(run_id=context.run_id)
    external_lem.add_ontology_item(
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
    integration = IntegrationSession.create(
        context,
        item,
        registry,
        "result-integration",
        invocation_id="inv-Q-001",
    )
    integration.register_prepared_asset(prepared_descriptor)
    accepted_bundle = AcceptedAnalysisBundle.load(item)
    answer_before = (item.accepted_root / "answer_content.json").read_bytes()
    envelope_before = (item.accepted_root / "acceptance_envelope.json").read_bytes()
    packet = integration.build_fidelity_packet()
    packet_text = integration.fidelity_packet_path.read_text(encoding="utf-8")
    assert packet.answer_content == json.loads(accepted_bundle.answer_content.decode("utf-8"))
    assert packet.answer_content_bytes == accepted_bundle.answer_content
    assert packet.acceptance_envelope == json.loads(envelope_before.decode("utf-8"))
    assert packet.manifest == json.loads((item.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert packet.accepted_content_hash == accepted_bundle.content_hash
    assert packet.accepted_manifest_hash == accepted_bundle.manifest_hash
    for sentinel in (
        "outside",
        "CUMULATIVE LEM SENTINEL",
        "CUMULATIVE TELEMETRY SENTINEL",
        "CUMULATIVE REPORT SENTINEL",
    ):
        assert sentinel not in packet_text
    integration.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in integration.records),
    )
    original_apply = integration._apply_records
    raised = {"value": False}

    def fail_once():
        original_apply()
        if not raised["value"]:
            raised["value"] = True
            raise RuntimeError("integration apply crash")

    monkeypatch.setattr(integration, "_apply_records", fail_once)
    with pytest.raises(RuntimeError, match="integration apply crash"):
        integration.commit()
    assert item.integration_state == "pending"
    assert len(registry.search(prepared_asset_id="orders-prepared")) == 1
    monkeypatch.setattr(integration, "_apply_records", original_apply)
    manifest = integration.commit()
    assert manifest["status"] == "committed"
    assert item.integration_state == "integrated"
    assert len(registry.search(prepared_asset_id="orders-prepared")) == 1
    assert (item.accepted_root / "answer_content.json").read_bytes() == answer_before
    assert (item.accepted_root / "acceptance_envelope.json").read_bytes() == envelope_before


def test_run_context_defaults_to_current_versions(tmp_path: Path) -> None:
    context = RunContext("RUN-DEFAULT-VERSION", tmp_path / "run")
    assert context.core_version == "0.9.0"
    assert context.skill_version == "0.8.0"


def test_item_state_template_loads_through_durable_core(tmp_path: Path) -> None:
    """The public base template is accepted by the real durable loader."""

    template_path = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "ITEM_STATE_TEMPLATE.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    template["item_id"] = "Q-TEMPLATE"
    template["original_text"] = "Load the canonical item-state template."
    assert tuple(template) == tuple(ITEM_STATE_FIELDS)
    assert set(template) <= set(ITEM_STATE_SCHEMA["fields"])

    context = RunContext("RUN-TEMPLATE", tmp_path / "run", core_version="0.4.0", skill_version="0.3.0")
    item_root = context.resolve_run_path(Path("questions") / template["item_id"])
    (item_root / "work").mkdir(parents=True)
    state_path = item_root / "item_state.json"
    state_path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")

    loaded = ItemWorkspace.load(context, template["item_id"], mode=template["mode"])
    assert loaded.state["item_id"] == "Q-TEMPLATE"
    assert loaded.state["mode"] == "question"
    assert loaded.state["original_text"] == template["original_text"]
    assert set(json.loads(state_path.read_text(encoding="utf-8"))) == set(ITEM_STATE_FIELDS)


def test_run_state_template_matches_exact_lifecycle_schema() -> None:
    """The illustrative template mirrors RunLifecycle's nine persisted fields."""

    template_path = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "RUN_STATE_TEMPLATE.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    assert set(template) == {
        "run_id",
        "run_root",
        "item_ids",
        "mode",
        "status",
        "generation",
        "manifest_hash",
        "created_at",
        "updated_at",
    }
    assert template["item_ids"]
    assert template["mode"].split("|") == ["question", "requirement"]
    assert template["status"].split("|") == list(RUN_STATES)
    assert template["generation"] >= 0
    assert template["manifest_hash"].startswith("<sha256(")
