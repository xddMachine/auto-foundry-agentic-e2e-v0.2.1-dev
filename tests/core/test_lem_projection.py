"""Event-sourced cumulative LEM projection regressions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from auto_foundry_core import AnalystWorkspace, BoundAnalysisContext, DataAssetRef
from auto_foundry_core.contracts import OntologyItem
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.lem_projection import LivingEnterpriseModelProjector
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.workspace import RunContext


def _accepted(context: RunContext, item_id: str) -> ItemWorkspace:
    item = ItemWorkspace.create(context, item_id, original_text=f"question {item_id}")
    item.write_plan({"item_id": item_id})
    item.write_draft({"answer": item_id})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    return item


def _session(context: RunContext, item_id: str) -> IntegrationSession:
    return IntegrationSession.create(
        context,
        _accepted(context, item_id),
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id=f"inv-{item_id}",
    )


def _commit(session: IntegrationSession) -> dict[str, object]:
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    return dict(session.commit())


def test_projection_rebuilds_prior_commits_in_lifecycle_order(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("customers.csv", "customer_id\nC-1\n")
        output.writestr("orders.csv", "order_id,customer_id\nO-1,C-1\n")

    context = RunContext("RUN", tmp_path / "run", (input_root,))
    RunLifecycle.create(context, ("Q-001", "Q-002", "Q-003"))

    first = _session(context, "Q-001")
    first.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer"),
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    limitation_id = first.add_limitation(
        {"limitation": "bounded fixture"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    first_manifest = _commit(first)

    second_item = ItemWorkspace.create(context, "Q-002", original_text="question Q-002")
    second_bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        second_item,
    )
    second_analyst = AnalystWorkspace(second_bound, owner_ref="owner-Q-002")
    second_analyst.begin_analysis(
        objective="Record the reviewed customer-order relationship.",
        strategy="Use the explicit customer_id equality in the bounded fixture.",
    )
    relationship = second_analyst.record_analytical_relationship(
        relationship_id="customer-order",
        source_id="customer",
        target_id="order",
        cardinality="one_to_one",
        join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
        matched_pairs=1,
        source_population=1,
        target_population=1,
        matched_source_count=1,
        matched_target_count=1,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        as_of=None,
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/plan.json",),
        publishable=True,
    )
    relationship_payload = relationship.to_dict()
    second_analyst.submit_answer("The bounded fixture contains one customer-order pair.")
    second_item.record_review("accept", reviewer_ref="reviewer")
    second_analyst.accept(accepted_refs=("work/plan.json", "work/analytical_relationships.jsonl"))
    second = IntegrationSession.create(
        context,
        second_item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-002",
    )
    assert set(second.lem.ontology) == {"customer"}
    assert set(second.lem.knowledge) == {limitation_id}
    second.add_ontology_item(
        OntologyItem(item_id="order", item_type="entity", label="Order"),
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    second.add_relationship(
        {
            "relationship_id": relationship_payload["relationship_id"],
            "analysis_relationship_id": relationship_payload["relationship_id"],
            "source_id": relationship_payload["source_id"],
            "target_id": relationship_payload["target_id"],
            "cardinality": relationship_payload["cardinality"],
            "join_keys": relationship_payload["join_keys"],
            "matched_pairs": relationship_payload["matched_pairs"],
            "source_population": relationship_payload["source_population"],
            "target_population": relationship_payload["target_population"],
            "matched_source_count": relationship_payload["matched_source_count"],
            "matched_target_count": relationship_payload["matched_target_count"],
            "source_coverage": relationship_payload["source_coverage"],
            "target_coverage": relationship_payload["target_coverage"],
            "date_authority": relationship_payload["date_authority"],
            "as_of": relationship_payload["as_of"],
            "limitations": relationship_payload["limitations"],
            "evidence_refs": relationship_payload["evidence_refs"],
        },
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl", "work/plan.json"),
    )
    second_manifest = _commit(second)

    projection = LivingEnterpriseModelProjector.project(context, before_item_id="Q-003")
    repeated = LivingEnterpriseModelProjector.project(context, before_item_id="Q-003")
    assert projection.projection_hash == repeated.projection_hash
    assert projection.model.export() == repeated.model.export()
    assert [binding.item_id for binding in projection.bindings] == ["Q-001", "Q-002"]
    assert [binding.manifest_hash for binding in projection.bindings] == [
        first_manifest["manifest_hash"],
        second_manifest["manifest_hash"],
    ]
    assert set(projection.model.ontology) == {"customer", "order", "customer-order"}
    assert set(projection.model.relationships) == {"customer-order"}
    assert set(projection.model.knowledge) == {limitation_id}

    third = _session(context, "Q-003")
    assert third.lem_projection.projection_hash == projection.projection_hash
    assert third.lem.export() == projection.model.export()


def test_projection_rejects_lifecycle_bound_to_another_run(tmp_path: Path) -> None:
    context = RunContext("RUN-CONTEXT-A", tmp_path / "run-a")
    other_context = RunContext("RUN-CONTEXT-B", tmp_path / "run-b")
    other_lifecycle = RunLifecycle.create(other_context, ("Q-001", "Q-002"))

    with pytest.raises(ValueError, match="different run"):
        LivingEnterpriseModelProjector.project(
            context,
            before_item_id="Q-002",
            _lifecycle=other_lifecycle,
        )


def test_projection_skips_nonaccepted_and_integration_failed_items(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002", "Q-003"))

    failed_analysis = ItemWorkspace.create(context, "Q-001", original_text="question Q-001")
    failed_analysis.technical_failure("fixture failure", recovery_exhausted=True)

    failed_integration = _session(context, "Q-002")
    failed_integration.mark_technical_failure("fixture integration failure")

    third = _session(context, "Q-003")
    assert third.lem_projection.bindings == ()
    assert third.lem.ontology == {}
    assert third.lem.knowledge == {}


def test_projection_rejects_uncommitted_prior_item_gap(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"))
    _accepted(context, "Q-001")
    second = _accepted(context, "Q-002")

    with pytest.raises(ValueError, match="uncommitted lifecycle gap"):
        IntegrationSession.create(
            context,
            second,
            PreparedAssetRegistry(context),
            "integration-owner",
            invocation_id="inv-Q-002",
        )


def test_projection_rejects_tampered_committed_records(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"))
    first = _session(context, "Q-001")
    first.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer"),
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    _commit(first)
    records_path = first.committed_root / "records.jsonl"
    value = json.loads(records_path.read_text(encoding="utf-8").splitlines()[0])
    value["payload"]["label"] = "tampered"
    records_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        LivingEnterpriseModelProjector.project(context, before_item_id="Q-002")


def test_projection_rejects_stale_integrated_state_binding(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"))
    first = _session(context, "Q-001")
    first.add_claim(
        {"claim": "committed binding"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    _commit(first)
    state_path = first.item_workspace.item_root / "item_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["integration_manifest_hash"] = "f" * 64
    state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest hash is stale"):
        LivingEnterpriseModelProjector.project(context, before_item_id="Q-002")


def test_projection_rejects_missing_integration_failure_manifest(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"))
    first = _session(context, "Q-001")
    first.mark_technical_failure("fixture integration failure")
    (first.item_workspace.item_root / "integration" / "technical_failure" / "manifest.json").unlink()

    with pytest.raises(ValueError, match="technical failure manifest is missing"):
        LivingEnterpriseModelProjector.project(context, before_item_id="Q-002")


def test_projection_rejects_stale_accepted_manifest_binding(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"))
    first = _session(context, "Q-001")
    first.add_claim(
        {"claim": "committed binding"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    _commit(first)
    manifest_path = first.committed_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["accepted_manifest_hash"] = "a" * 64
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    manifest["manifest_hash"] = hashlib.sha256(
        (json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    state_path = first.item_workspace.item_root / "item_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["integration_manifest_hash"] = manifest["manifest_hash"]
    state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="accepted manifest binding is stale"):
        LivingEnterpriseModelProjector.project(context, before_item_id="Q-002")


def test_caller_model_mutation_is_not_commit_authority(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"))
    first = _session(context, "Q-001")
    first.lem.add_ontology_item(
        OntologyItem(item_id="poison", item_type="entity", label="Caller mutation")
    )
    first.add_claim(
        {"claim": "record-only commit"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    _commit(first)
    projection = LivingEnterpriseModelProjector.project(context, before_item_id="Q-002")
    assert "poison" not in projection.model.ontology


def test_session_rejects_item_mode_mismatch_before_integration_write(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001",), mode="question")
    item = ItemWorkspace.create(context, "Q-001", original_text="requirement", mode="requirement")
    item.write_plan({"item_id": "Q-001"})
    item.write_draft({"answer": "bounded"})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json",))

    with pytest.raises(ValueError, match="mode does not match"):
        IntegrationSession.create(
            context,
            item,
            PreparedAssetRegistry(context),
            "integration-owner",
            invocation_id="inv-Q-001",
        )
    assert not (item.item_root / "integration").exists()
