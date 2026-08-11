"""Focused regressions for the v0.3.2 semantic/result-integration boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from auto_foundry_core import (
    IntegrationSession,
    LivingEnterpriseModel,
    PreparedAssetRegistry,
    deterministic_record_id,
)
from auto_foundry_core.contracts import KnowledgeDelta, LEMRef, PreparedAssetDescriptor
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import RunContext


def _setup(tmp_path: Path, *, item_id: str = "Q-001"):
    context = RunContext("RUN", tmp_path / "run")
    workspace = ItemWorkspace.create(context, item_id, original_text="question")
    workspace.write_plan({"item_id": item_id, "offline": True})
    workspace.write_draft({"answer": item_id})
    workspace.record_review("accept", reviewer_ref="reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    lem = LivingEnterpriseModel(run_id=context.run_id)
    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(context, workspace, lem, registry, "owner", invocation_id="inv-1")
    return context, workspace, lem, registry, session


def _accept(session: IntegrationSession) -> None:
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )


def test_observation_metric_stays_out_of_ontology_but_definition_is_allowed(tmp_path: Path) -> None:
    _context, _workspace, lem, _registry, session = _setup(tmp_path)
    metric_id = session.add_metric({"item_id": "orders", "value": 3, "share": 0.5}, scope="question", evidence_refs=("work/plan.json",))
    definition_id = session.add_metric_definition(
        {"item_id": "orders_definition", "label": "Orders", "properties": {"unit": "count", "formula": "count(order_id)", "population_description": "accepted orders"}},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    assert metric_id != definition_id
    assert "orders" not in lem.ontology
    _accept(session)
    session.commit()
    assert "orders" not in lem.ontology
    assert "orders_definition" in lem.ontology


def test_observation_shaped_ontology_is_rejected(tmp_path: Path) -> None:
    _context, _workspace, _lem, _registry, session = _setup(tmp_path)
    with pytest.raises(ValueError, match="observation-shaped"):
        session.add_ontology_item({"item_id": "bad", "item_type": "metric_definition", "label": "Bad", "properties": {"current_count": 4}}, scope="question", evidence_refs=("work/plan.json",))


def test_export_rehydrate_roundtrip_and_tamper_rejection(tmp_path: Path) -> None:
    lem = LivingEnterpriseModel(run_id="RUN")
    lem.add_metric_definition({"item_id": "m", "label": "M", "properties": {"unit": "count", "formula": "count(x)"}, "metadata": {"evidence_refs": ["answer"], "evidence_hashes": {"answer": "a" * 64}}})
    exported = lem.export()
    assert LivingEnterpriseModel.from_export(exported).export() == exported
    tampered = json.loads(json.dumps(exported))
    tampered["ontology"][0]["metadata"]["evidence_hashes"]["answer"] = "bad"
    with pytest.raises(ValueError, match="hash|round-trip"):
        LivingEnterpriseModel.from_export(tampered)


def test_export_rehydrates_relationship_prepared_and_knowledge_evidence(tmp_path: Path) -> None:
    lem = LivingEnterpriseModel(run_id="RUN")
    lem.add_ontology_item({"item_id": "customer", "item_type": "entity", "label": "Customer"})
    lem.add_ontology_item({"item_id": "order", "item_type": "entity", "label": "Order"})
    lem.add_relationship({"relationship_id": "places", "source_id": "customer", "target_id": "order", "label": "places"})
    lem.register_prepared_asset(
        PreparedAssetDescriptor(
            prepared_asset_id="asset",
            location="work/prepared/asset.jsonl",
            prepared_content_hash="a" * 64,
            operation_manifest_hash="b" * 64,
            row_count=1,
            byte_count=2,
            metadata={"evidence_refs": ["accepted/answer_content.json"], "evidence_hashes": {"accepted/answer_content.json": "c" * 64}},
        )
    )
    lem.apply_delta(
        KnowledgeDelta(
            "limitation",
            "record_limitation",
            {"reason": "partial"},
            evidence_refs=("accepted/answer_content.json",),
            accepted=True,
        )
    )
    exported = lem.export()
    restored = LivingEnterpriseModel.from_export(exported)
    assert restored.export() == exported


def test_collision_safe_ids_and_supplied_validation(tmp_path: Path) -> None:
    assert deterministic_record_id("metric", "North America", "North-America") != deterministic_record_id("metric", "North-America", "North America")
    assert deterministic_record_id("metric", "North America", "raw") == deterministic_record_id("metric", "North America", "raw")
    _context, _workspace, _lem, _registry, session = _setup(tmp_path)
    with pytest.raises(ValueError):
        session.add_claim("bad", scope="question", evidence_refs=("work/plan.json",), claim_id="../escape")


def test_commit_requires_fidelity_and_registry_lem_stay_untouched(tmp_path: Path) -> None:
    _context, workspace, lem, registry, session = _setup(tmp_path)
    session.add_metric({"item_id": "obs", "value": 1}, scope="question", evidence_refs=("work/plan.json",))
    with pytest.raises(ValueError, match="fidelity"):
        session.commit()
    assert workspace.integration_state == "pending"
    assert lem.ontology == {}
    assert registry.search() == ()


def test_initial_fidelity_checked_ids_and_invocation_identity_are_exact(tmp_path: Path) -> None:
    context, workspace, lem, registry, session = _setup(tmp_path)
    record_id = session.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    with pytest.raises(ValueError, match="checked_record_ids"):
        session.record_fidelity_review("accept", checked_record_ids=())
    with pytest.raises(ValueError, match="exactly"):
        session.record_fidelity_review("accept", checked_record_ids=("unknown",))
    with pytest.raises(ValueError, match="duplicates"):
        session.record_fidelity_review("accept", checked_record_ids=(record_id, record_id))
    session.release()
    with pytest.raises(TypeError, match="invocation_id"):
        IntegrationSession.create(context, workspace, lem, registry, "owner")


def test_multi_record_repair_authorization_is_immutable_and_ordered(tmp_path: Path) -> None:
    _context, _workspace, lem, _registry, session = _setup(tmp_path)
    first = session.add_claim("first-wrong", scope="question", evidence_refs=("work/plan.json",))
    second = session.add_claim("second-wrong", scope="question", evidence_refs=("work/plan.json",))
    unaffected = session.add_claim("unchanged", scope="question", evidence_refs=("work/plan.json",))
    all_ids = tuple(record.record_id for record in session.records)
    session.record_fidelity_review(
        "repair_once",
        findings=[{"message": "two claims need correction", "record_ids": [first, second]}],
        checked_record_ids=all_ids,
    )
    authorization_bytes = session.fidelity_authorization_path.read_bytes()
    initial_packet_hash = json.loads(session.fidelity_packet_path.read_text(encoding="utf-8"))["packet_hash"]
    baseline_unaffected = next(record.record_hash for record in session.records if record.record_id == unaffected)

    session.correct_record(first, {"claim": "first-fixed"})
    assert session.fidelity_authorization_path.read_bytes() == authorization_bytes
    assert next(record.record_hash for record in session.records if record.record_id == unaffected) == baseline_unaffected
    with pytest.raises(ValueError, match="all authorized corrections"):
        session.record_fidelity_review("accept", checked_record_ids=(first,))
    assert json.loads(session.fidelity_packet_path.read_text(encoding="utf-8"))["packet_hash"] == initial_packet_hash

    session.correct_record(second, {"claim": "second-fixed"})
    with pytest.raises(ValueError, match="already corrected"):
        session.correct_record(first, {"claim": "first-fixed-again"})
    with pytest.raises(ValueError, match="exactly"):
        session.record_fidelity_review("accept", checked_record_ids=(first, second, unaffected))
    targeted = session.record_fidelity_review("accept", checked_record_ids=(first, second))
    assert targeted.review_kind == "targeted"
    assert session.fidelity_authorization_path.read_bytes() == authorization_bytes
    assert next(record.record_hash for record in session.records if record.record_id == unaffected) == baseline_unaffected
    session.commit()
    assert lem.ontology == {}


def test_lem_rehydrate_rejects_dangling_or_mismatched_supersession_links() -> None:
    model = LivingEnterpriseModel(run_id="RUN")
    model.apply_delta(KnowledgeDelta("base", "no_change", accepted=True))
    model.apply_delta(
        KnowledgeDelta(
            "replacement",
            "no_change",
            supersedes=(LEMRef("knowledge_delta", "base"),),
            accepted=True,
        )
    )
    exported = model.export()
    assert LivingEnterpriseModel.from_export(exported).export() == exported

    mismatched = json.loads(json.dumps(exported))
    mismatched["supersession_links"]["replacement"] = []
    with pytest.raises(ValueError, match="supersession|supersedes"):
        LivingEnterpriseModel.from_export(mismatched)

    dangling = json.loads(json.dumps(exported))
    dangling["supersession_links"]["replacement"] = [{"namespace": "knowledge_delta", "object_id": "missing"}]
    with pytest.raises(ValueError, match="dangling|supersession"):
        LivingEnterpriseModel.from_export(dangling)

    reverse_mismatch = json.loads(json.dumps(exported))
    reverse_mismatch["knowledge"]["base"]["superseded_by"] = []
    with pytest.raises(ValueError, match="superseded_by|supersession"):
        LivingEnterpriseModel.from_export(reverse_mismatch)


def test_repair_once_scopes_correction_and_targeted_recheck(tmp_path: Path) -> None:
    _context, _workspace, lem, _registry, session = _setup(tmp_path)
    affected = session.add_claim("wrong", scope="question", evidence_refs=("work/plan.json",))
    unaffected = session.add_claim("right", scope="question", evidence_refs=("work/plan.json",))
    session.record_fidelity_review(
        "repair_once",
        findings=[{"message": "claim text", "record_id": affected, "parts": ["payload"]}],
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    with pytest.raises(ValueError, match="scope"):
        session.correct_record(unaffected, {"claim": "tampered"})
    before = next(record.record_hash for record in session.records if record.record_id == unaffected)
    session.correct_record(affected, {"claim": "fixed"})
    targeted = session.record_fidelity_review("accept", checked_record_ids=(affected,))
    assert targeted.review_kind == "targeted"
    assert next(record.record_hash for record in session.records if record.record_id == unaffected) == before
    session.commit()
    assert lem.ontology == {}


def test_fidelity_packet_is_item_only_and_reload_is_idempotent(tmp_path: Path) -> None:
    context, workspace, lem, registry, session = _setup(tmp_path)
    session.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    sibling = ItemWorkspace.create(context, "Q-002", original_text="sibling")
    sibling.write_plan({"sentinel": "sibling-plan-secret"})
    sibling.write_draft({"answer": "sibling-answer-secret"})
    sibling.record_review("accept", reviewer_ref="sibling-reviewer")
    sibling.accept(accepted_refs=("work/plan.json",))
    lem.add_ontology_item({"item_id": "cumulative-secret", "item_type": "entity", "label": "cumulative-secret"})
    registry.registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry.registry_path.write_text("registry-secret\n", encoding="utf-8")
    telemetry = TelemetryRecorder(context=context)
    telemetry.record("sentinel", facts={"secret": "telemetry-secret"})
    (context.run_root / "report.json").write_text(json.dumps({"secret": "report-secret"}), encoding="utf-8")
    accepted_bytes = (workspace.accepted_root / "answer_content.json").read_bytes()
    accepted_json = json.loads(accepted_bytes.decode("utf-8"))
    packet = session.build_fidelity_packet()
    packet_text = session.fidelity_packet_path.read_text(encoding="utf-8")
    assert packet.answer_content == accepted_json
    assert packet.answer_content_bytes == accepted_bytes
    assert packet.acceptance_envelope["item_id"] == workspace.item_id
    assert packet.manifest["manifest_hash"] == session.bundle.manifest_hash
    for sentinel in ("sibling-plan-secret", "sibling-answer-secret", "cumulative-secret", "registry-secret", "telemetry-secret", "report-secret"):
        assert sentinel not in packet_text
    _accept(session)
    manifest = session.commit()
    reloaded = IntegrationSession.load(context, workspace, lem, registry, "owner", invocation_id="inv-1")
    assert reloaded.status == "committed"
    assert reloaded.commit()["manifest_hash"] == manifest["manifest_hash"]


def test_process_invocation_lease_has_single_winner(tmp_path: Path) -> None:
    context, workspace, _lem, _registry, session = _setup(tmp_path)
    code = """
from auto_foundry_core import IntegrationSession, LivingEnterpriseModel, PreparedAssetRegistry
from auto_foundry_core.workspace import RunContext
from auto_foundry_core.durable import ItemWorkspace
try:
    c = RunContext('RUN', __import__('pathlib').Path(r'{root}'))
    w = ItemWorkspace.load(c, 'Q-001')
    IntegrationSession.load(c, w, LivingEnterpriseModel(run_id='RUN'), PreparedAssetRegistry(c), 'owner', invocation_id='inv-1')
except ValueError as exc:
    print(type(exc).__name__)
""".format(root=str(context.run_root))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, check=True)
    assert "ValueError" in result.stdout
    session.release()


def test_tampered_accepted_bytes_invalidate_packet_review_and_commit(tmp_path: Path) -> None:
    _context, workspace, _lem, _registry, session = _setup(tmp_path)
    session.add_claim("claim", scope="question", evidence_refs=("work/plan.json",))
    session.build_fidelity_packet()
    _accept(session)
    (workspace.accepted_root / "answer_content.json").write_bytes(b'{"answer":"tampered"}\n')
    with pytest.raises(ValueError, match="terminal|bundle|fidelity"):
        session.commit()
