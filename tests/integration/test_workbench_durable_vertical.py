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
    AcceptedSnapshot,
    DataAssetRef,
    DataRoomCatalogEntry,
    DataRoomWorkbench,
    ITEM_STATE_FIELDS,
    ITEM_STATE_SCHEMA,
    ItemWorkspace,
    RunContext,
)
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.lifecycle import AgentInvocationReceipt, RUN_STATES
from auto_foundry_core.workspace import AllowedRootError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_offline_workbench_and_durable_vertical_path(tmp_path: Path) -> None:
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
        core_version="0.3.0",
        skill_version="0.2.3",
    )
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
    prepared_descriptor = workbench.save_prepared(
        "orders-prepared",
        order_entry,
        transformations=("bounded_csv_read",),
        limitations=("Generic fixture only",),
    )
    prepared = workbench.prepared("orders-prepared")
    assert prepared_descriptor.prepared_content_hash
    assert prepared_descriptor.row_count == 2
    assert len(prepared) == 2
    assert prepared[0]["order_id"] == "left-1"

    # The authoritative item state and work directory exist before any attempt.
    item = ItemWorkspace.create(
        context,
        "Q-001",
        original_text="Summarize the supplied generic fixture.",
        telemetry=telemetry,
    )
    assert (item.item_root / "item_state.json").is_file()
    assert item.work_root.is_dir()
    attempt = item.begin_attempt("lane-lead", "Lead Analyst")
    first = item.observe_attempt(attempt.attempt_id)
    assert first.action == "materialize_now"
    second = item.observe_attempt(attempt.attempt_id)
    assert second.action == "await_runtime"
    loss_receipt = AgentInvocationReceipt(
        "I-WORKBENCH-LOSS",
        item.item_id,
        "Lead Analyst",
        "lead",
        provider="unavailable",
        model="unavailable",
        start="2026-01-01T00:00:00+00:00",
        finish="2026-01-01T00:00:01+00:00",
        terminal_reason="process_lost",
    )
    item.write_handoff({"next": "resume from the prepared orders asset"})
    recovery = item.begin_recovery("lane-recovery", "Lead Analyst", prior_attempt_id=attempt.attempt_id, receipt=loss_receipt)
    assert item.state["lifecycle_state"] == "recovering"

    # Execution recovery is a distinct route and does not consume business repair.
    assert recovery.route == "recovery"
    assert item.state["execution_recovery_count"] == 1
    assert item.state["business_repair_count"] == 0
    assert item.state["attempts"][-1]["handoff_ref"] == "work/handoff.json"

    item.write_draft({"answer": "bounded fixture summary", "refs": ["orders-prepared"]})
    assert item.observe_attempt(recovery.attempt_id).action == "continue"
    item.finish_attempt(recovery.attempt_id, status="completed")
    item.record_review("repair_once", reviewer_ref="reviewer-1")
    item.use_business_repair()
    assert item.state["execution_recovery_count"] == 1
    assert item.state["business_repair_count"] == 1

    item.write_draft({"answer": "bounded fixture summary after repair", "refs": ["orders-prepared"]})
    review = item.record_review("accept", reviewer_ref="reviewer-2")
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


def test_run_context_defaults_to_core_v030(tmp_path: Path) -> None:
    context = RunContext("RUN-DEFAULT-VERSION", tmp_path / "run")
    assert context.core_version == "0.3.0"


def test_item_state_template_loads_through_durable_core(tmp_path: Path) -> None:
    """The public base template is accepted by the real durable loader."""

    template_path = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "ITEM_STATE_TEMPLATE.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    template["item_id"] = "Q-TEMPLATE"
    template["original_text"] = "Load the canonical item-state template."
    assert tuple(template) == tuple(ITEM_STATE_FIELDS)
    assert set(template) <= set(ITEM_STATE_SCHEMA["fields"])

    context = RunContext("RUN-TEMPLATE", tmp_path / "run", core_version="0.3.0")
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
