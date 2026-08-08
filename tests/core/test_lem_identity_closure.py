"""Focused closure checks for typed LEM and reviewed identity contracts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from auto_foundry_core.contracts import (
    AggregationSpec,
    CanonicalMapping,
    IdentityDecision,
    KnowledgeDelta,
    LEMRef,
    PreparedAssetDescriptor,
)
from auto_foundry_core.enterprise_model import LivingEnterpriseModel
from auto_foundry_core.identity import apply_decision, generate_candidates


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_lem_refs_are_frozen_and_namespace_safe():
    ref = LEMRef("ontology", "same-id")
    assert ref.to_dict() == {"namespace": "ontology", "object_id": "same-id"}
    with pytest.raises(ValueError):
        LEMRef("unknown", "same-id")
    with pytest.raises(TypeError):
        KnowledgeDelta("bad", "supersede", supersedes=("same-id",))
    with pytest.raises(ValueError):
        KnowledgeDelta("legacy", "supersede", {"item_ids": ("same-id",)})


def test_typed_supersession_resolves_collision_and_rolls_back():
    model = LivingEnterpriseModel(run_id="r")
    model.add_ontology_item({"item_id": "same-id", "item_type": "object", "label": "O"})
    model.register_prepared_asset(PreparedAssetDescriptor("same-id", location="out.json"))
    model.apply_delta(KnowledgeDelta("d1", "no_change", accepted=True))
    with pytest.raises(KeyError):
        model.apply_delta(KnowledgeDelta("bad", "supersede", supersedes=(LEMRef("ontology", "same-id"), LEMRef("prepared_asset", "missing")), accepted=True))
    assert model.ontology["same-id"].status == "active"
    assert "bad" not in model.knowledge
    model.apply_delta(KnowledgeDelta("good", "supersede", supersedes=(LEMRef("ontology", "same-id"), LEMRef("prepared_asset", "same-id")), accepted=True))
    exported = model.export()
    assert exported["supersession_links"]["good"] == [
        {"namespace": "ontology", "object_id": "same-id"},
        {"namespace": "prepared_asset", "object_id": "same-id"},
    ]


def test_semantic_operations_are_ontology_index_items_and_relationships_bounded():
    model = LivingEnterpriseModel(run_id="r")
    for kind in ("metric", "definition", "rule", "process"):
        model.apply_delta(KnowledgeDelta(f"d-{kind}", f"add_{kind}", {"item_id": kind, "label": kind.title()}, accepted=True))
    model.apply_delta(KnowledgeDelta("d-rel", "add_relationship", {"relationship_id": "rel", "label": "Rel", "source_id": "a", "target_id": "b", "scope": "s", "effective_period": "2024"}, accepted=True))
    indexed = {item["item_id"]: item["item_type"] for item in model.ontology_index}
    assert indexed == {"definition": "definition", "metric": "metric", "process": "process", "rel": "relationship", "rule": "rule"}
    assert model.relevant_bundle(["metric"], relationship_ids=["rel"], scope="s", effective_period="2024")["metadata"]["total_count"] == 2


def test_mapping_idempotence_alias_normalization_and_collision():
    model = LivingEnterpriseModel(run_id="r")
    mapping = CanonicalMapping("c1", "entity", ("a",), "decision-1")
    assert model.add_mapping(mapping) == mapping
    assert model.add_mapping(mapping) == mapping
    with pytest.raises(ValueError):
        model.add_mapping(CanonicalMapping("c1", "entity", ("b",), "decision-2"))
    model.apply_delta(KnowledgeDelta("alias", "add_alias", {"canonical_id": "c1", "alias": "Alpha", "source_identity": "a2"}, accepted=True))
    assert model.canonical_mappings["c1"].aliases == ("Alpha",)
    assert model.canonical_mappings["c1"].source_identities == ("a", "a2")


def test_reviewed_identity_trace_hash_and_exact_mapping_linkage():
    candidate = generate_candidates([{"id": "a", "name": "North"}], [{"id": "b", "name": "North"}], compare_fields=["name"])[0]
    pending = IdentityDecision(candidate.candidate_id, "same_object", reviewer_ref="reviewer", review_status="pending")
    with pytest.raises(ValueError):
        apply_decision(candidate, pending)
    decision = IdentityDecision(candidate.candidate_id, "same_object", decision_id="decision-1", reviewer_ref="reviewer", review_status="reviewed", evidence_refs=("e1",), rationale="same source")
    assert decision.decision_hash == IdentityDecision.from_dict(decision.to_dict()).decision_hash
    mapping = apply_decision(candidate, decision)
    assert mapping.decision_id == decision.decision_id
    assert mapping.metadata["reviewed_trace"]["decision_hash"] == decision.decision_hash


def test_prepared_output_hash_lookup_and_mismatch(tmp_path: Path):
    output = tmp_path / "prepared.json"
    output.write_bytes(b"prepared")
    model = LivingEnterpriseModel(run_id="r")
    model.register_prepared_asset(PreparedAssetDescriptor("p1", location=str(output), prepared_content_hash=_sha(b"prepared"), operation_manifest_hash=_sha(b"manifest"), row_count=1, byte_count=8, core_version="0.1"))
    assert model.lookup_prepared_asset("p1", operation_manifest_hash=_sha(b"manifest")) is not None
    assert model.verify_prepared_asset_reuse("p1") is True
    output.write_bytes(b"changed")
    with pytest.raises(ValueError):
        model.verify_prepared_asset_reuse("p1")


def test_relevant_bundle_rejects_duplicate_limits_bytes_scope_and_period():
    model = LivingEnterpriseModel(run_id="r")
    model.add_ontology_item({"item_id": "o", "item_type": "object", "label": "O", "scope": "s", "effective_period": "2024"})
    with pytest.raises(ValueError):
        model.relevant_bundle(refs=[LEMRef("ontology", "o"), LEMRef("ontology", "o")])
    with pytest.raises(ValueError):
        model.relevant_bundle(["o"], max_total_items=0)
    with pytest.raises(ValueError):
        model.relevant_bundle(["o"], max_json_bytes=1)
    with pytest.raises(ValueError):
        model.relevant_bundle(["o"], scope="other")
    with pytest.raises(ValueError):
        model.relevant_bundle(["o"], effective_period="2025")


def test_aggregation_spec_is_explicit_without_distinct_flag():
    spec = AggregationSpec("share", numerator=2, denominator=4, period_order=("2023", "2024"))
    assert "distinct" not in spec.to_dict()
    assert spec.period_order == ("2023", "2024")
