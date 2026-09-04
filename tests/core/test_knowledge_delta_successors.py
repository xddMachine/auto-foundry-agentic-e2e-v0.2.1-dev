"""Focused durable KnowledgeDelta successor and replay coverage."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from auto_foundry_core import IntegrationSession, LivingEnterpriseModel, PreparedAssetRegistry, RunLifecycle
from auto_foundry_core.contracts import (
    CanonicalMapping,
    IdentityDecision,
    KnowledgeDelta,
    LEMRef,
    PreparedAssetDescriptor,
)
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.lem_projection import LivingEnterpriseModelProjector
from auto_foundry_core.workspace import RunContext


def _accepted_item(context: RunContext, item_id: str) -> ItemWorkspace:
    workspace = ItemWorkspace.create(context, item_id, original_text=item_id)
    workspace.write_plan({"item_id": item_id, "offline": True})
    workspace.write_draft({"answer": item_id})
    workspace.record_review("accept", reviewer_ref="reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    return workspace


def _accept_and_commit(session: IntegrationSession) -> None:
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    session.commit()


def test_knowledge_delta_is_evidence_bound_durable_and_projected() -> None:
    with TemporaryDirectory() as temporary:
        context = RunContext("RUN-KD", Path(temporary) / "run")
        RunLifecycle.create(context, ("Q-001", "Q-002"))
        first = _accepted_item(context, "Q-001")
        registry = PreparedAssetRegistry(context)
        session = IntegrationSession.create(context, first, registry, "owner", "inv-1")
        delta = KnowledgeDelta(
            "ontology-v1",
            "add_ontology_item",
            {"item_id": "customer", "item_type": "entity", "label": "Customer"},
            accepted=True,
        )
        record_id = session.add_knowledge_delta(
            delta,
            scope="question",
            evidence_refs=("work/plan.json",),
        )
        assert session.records[0].kind == "knowledge_delta"
        assert session.records[0].payload["delta_id"] == "ontology-v1"
        assert session.records[0].evidence_hashes["work/plan.json"]
        assert record_id == session.add_knowledge_delta(
            delta,
            scope="question",
            evidence_refs=("work/plan.json",),
        )
        _accept_and_commit(session)

        reloaded = IntegrationSession.load(context, first, registry, "owner", "inv-1")
        assert "customer" in reloaded.lem.ontology
        projected = LivingEnterpriseModelProjector.project(context, include_item_id="Q-001").model
        assert projected.export() == reloaded.lem.export()
        assert projected.knowledge["ontology-v1"]["operation"] == "add_ontology_item"


def test_successor_variants_are_atomic_and_keep_history() -> None:
    model = LivingEnterpriseModel(run_id="RUN-KD")
    model.add_ontology_item({"item_id": "old", "item_type": "entity", "label": "Old"})
    before = model.export()
    with pytest.raises(KeyError):
        model.apply_delta(
            KnowledgeDelta(
                "mixed-invalid",
                "add_ontology_item",
                {"item_id": "new", "item_type": "entity", "label": "New"},
                supersedes=(LEMRef("ontology", "old"), LEMRef("ontology", "missing")),
                accepted=True,
            )
        )
    assert model.export() == before

    model.apply_delta(
        KnowledgeDelta(
            "ontology-v2",
            "add_ontology_item",
            {"item_id": "new", "item_type": "entity", "label": "New"},
            supersedes=(LEMRef("ontology", "old"),),
            accepted=True,
        )
    )
    assert model.ontology["old"].status == "superseded"
    assert model.current_ontology["new"].label == "New"
    with pytest.raises(ValueError, match="object ID"):
        model.apply_delta(
            KnowledgeDelta(
                "same-id",
                "add_ontology_item",
                {"item_id": "new", "item_type": "entity", "label": "Again"},
                supersedes=(LEMRef("ontology", "new"),),
                accepted=True,
            )
        )
    model.register_prepared_asset(PreparedAssetDescriptor("wrong-namespace-target", location="x"))
    with pytest.raises(ValueError, match="namespace"):
        model.apply_delta(
            KnowledgeDelta(
                "wrong-namespace",
                "add_ontology_item",
                {"item_id": "third", "item_type": "entity", "label": "Third"},
                supersedes=(LEMRef("prepared_asset", "wrong-namespace-target"),),
                accepted=True,
            )
        )

    model.register_prepared_asset(PreparedAssetDescriptor("asset-v1", location="v1.json"))
    model.apply_delta(
        KnowledgeDelta(
            "asset-v2",
            "add_prepared_asset",
            PreparedAssetDescriptor("asset-v2", location="v2.json").to_dict(),
            supersedes=(LEMRef("prepared_asset", "asset-v1"),),
            accepted=True,
        )
    )
    decision = IdentityDecision(
        "candidate",
        "same_object",
        decision_id="decision",
        review_status="reviewed",
        reviewer_ref="reviewer",
    )
    model.register_identity_decision(decision)
    model.add_mapping(CanonicalMapping("mapping-v1", "entity", ("source-1",), "decision"))
    model.apply_delta(
        KnowledgeDelta(
            "mapping-v2",
            "add_canonical_mapping",
            CanonicalMapping("mapping-v2", "entity", ("source-2",), "decision").to_dict(),
            supersedes=(LEMRef("canonical_mapping", "mapping-v1"),),
            accepted=True,
        )
    )
    model.add_ontology_item({"item_id": "left", "item_type": "entity", "label": "Left"})
    model.add_ontology_item({"item_id": "right", "item_type": "entity", "label": "Right"})
    model.add_relationship({"relationship_id": "relationship-v1", "source_id": "left", "target_id": "right", "label": "old"})
    model.apply_delta(
        KnowledgeDelta(
            "relationship-v2",
            "add_relationship",
            {"relationship_id": "relationship-v2", "source_id": "left", "target_id": "right", "label": "new"},
            supersedes=(LEMRef("relationship", "relationship-v1"),),
            accepted=True,
        )
    )
    assert model.relationships["relationship-v1"]["status"] == "superseded"
    assert model.current_relationships["relationship-v2"]["label"] == "new"
    exported = model.export()
    assert LivingEnterpriseModel.from_export(exported).export() == exported


def test_knowledge_delta_observations_stay_out_of_ontology_and_record_collision_is_exact() -> None:
    with TemporaryDirectory() as temporary:
        context = RunContext("RUN-KD-OBS", Path(temporary) / "run")
        RunLifecycle.create(context, ("Q-001", "Q-002"))
        workspace = _accepted_item(context, "Q-001")
        registry = PreparedAssetRegistry(context)
        session = IntegrationSession.create(context, workspace, registry, "owner", "inv-1")
        observation = KnowledgeDelta(
            "observation",
            "add_metric",
            {"item_id": "orders", "value": 3},
            accepted=True,
        )
        with pytest.raises(ValueError, match="observation"):
            session.add_knowledge_delta(
                observation,
                scope="question",
                evidence_refs=("work/plan.json",),
            )
        delta = KnowledgeDelta(
            "delta",
            "no_change",
            accepted=True,
        )
        record_id = session.add_knowledge_delta(
            delta,
            scope="question",
            evidence_refs=("work/plan.json",),
            delta_record_id="stable-record",
        )
        assert record_id == "stable-record"
        with pytest.raises(ValueError, match="collision"):
            session.add_knowledge_delta(
                KnowledgeDelta("changed", "no_change", accepted=True),
                scope="question",
                evidence_refs=("work/plan.json",),
                delta_record_id="stable-record",
            )
