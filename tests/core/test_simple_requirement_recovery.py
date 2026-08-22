"""Focused per-item repair and integration recovery regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.workspace import RunContext


def _item(tmp_path: Path, item_id: str = "Q-001") -> ItemWorkspace:
    context = RunContext("RUN-SIMPLE-REPAIR", tmp_path / "run")
    return ItemWorkspace.create(context, item_id, original_text=f"question {item_id}")


def _active_item(tmp_path: Path, item_id: str = "Q-001") -> ItemWorkspace:
    item = _item(tmp_path, item_id)
    item.write_draft(
        {
            "schema_version": "auto_foundry.analyst_answer.v1",
            "item_id": item_id,
            "answer": "initial answer",
            "method": "initial method",
            "headline_findings": [],
            "scope": None,
            "supported_components": [],
            "unsupported_components": [],
            "limitations": [],
            "next_actions": [],
            "visuals": [],
            "evidence_refs": [],
        }
    )
    item.record_review(
        "repair_once",
        reviewer_ref="independent-reviewer",
        findings=[
            {
                "finding_id": "BR-CALC-PRESENTATION",
                "pointers": ["/answer"],
                "semantic_categories": ["calculation", "presentation"],
            }
        ],
    )
    item.use_business_repair(owner_ref="owner-Q-001")
    return item


def test_authorized_repair_is_item_local_without_category_artifact_allowlists(tmp_path: Path) -> None:
    item = _active_item(tmp_path)
    sibling = ItemWorkspace.create(item.context, "Q-002", original_text="question Q-002")
    sibling.write_draft({"answer": "sibling sentinel"})
    sibling_before = sibling.draft_root.read_bytes()

    calculations = item.work_root / "calculations"
    calculations.mkdir()
    (calculations / "recompute.py").write_text("ORDER_COUNT = 2\n", encoding="utf-8")
    results = item.work_root / "results"
    results.mkdir()
    (results / "result.json").write_text('{"order_count": 2}\n', encoding="utf-8")
    item.append_evidence({"evidence_id": "E-2", "conclusion": "recomputed"})
    item.write_handoff({"status": "ready", "owner": "owner-Q-001"})
    item.write_draft(
        {
            "schema_version": "auto_foundry.analyst_answer.v1",
            "item_id": "Q-001",
            "answer": "corrected answer",
            "method": "corrected method",
            "headline_findings": ["two supplied rows"],
            "scope": "fixture only",
            "supported_components": [],
            "unsupported_components": [],
            "limitations": [],
            "next_actions": ["reuse the bounded result"],
            "visuals": [{"title": "corrected"}],
            "evidence_refs": ["work/evidence.jsonl#E-2"],
        }
    )

    assert (calculations / "recompute.py").is_file()
    assert (results / "result.json").is_file()
    assert (item.work_root / "evidence.jsonl").is_file()
    assert (item.work_root / "handoff.json").is_file()
    assert sibling.draft_root.read_bytes() == sibling_before


def test_terminal_technical_failure_deactivates_repair_and_reloads(tmp_path: Path) -> None:
    item = _active_item(tmp_path)
    packet_path = item.business_review_path
    before = json.loads(packet_path.read_text(encoding="utf-8"))
    assert before["repair_active"] is True

    snapshot = item.technical_failure("repair runtime exhausted", recovery_exhausted=True)
    assert snapshot.outcome == "technical_failure"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["repair_active"] is False
    assert packet["findings"] == before["findings"]

    reloaded = ItemWorkspace.load(item.context, item.item_id)
    assert reloaded.state["lifecycle_state"] == "technical_failure"
    assert json.loads(reloaded.business_review_path.read_text(encoding="utf-8"))["repair_active"] is False


def test_fidelity_result_diagnostic_is_none_until_targeted_acceptance(tmp_path: Path) -> None:
    item = _item(tmp_path)
    RunLifecycle.create(item.context, (item.item_id,))
    item.write_plan({"item_id": item.item_id})
    item.write_draft({"answer": item.item_id})
    item.record_review("accept", reviewer_ref="business-reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    registry = PreparedAssetRegistry(item.context)
    session = IntegrationSession.create(
        item.context,
        item,
        registry,
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    record_id = session.add_claim(
        {"claim": "initial claim"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review(
        "repair_once",
        findings=[{"message": "claim correction", "record_id": record_id}],
        checked_record_ids=(record_id,),
    )
    session.correct_record(record_id, {"claim": "corrected claim"})

    assert session.fidelity_result is None
    with pytest.raises(ValueError, match="fidelity|acceptance|stale|unbound"):
        session.commit()

    session.build_fidelity_packet()
    targeted = session.record_fidelity_review("accept", checked_record_ids=(record_id,))
    assert targeted.review_kind == "targeted"
    assert session.fidelity_result is not None
    session.commit()
