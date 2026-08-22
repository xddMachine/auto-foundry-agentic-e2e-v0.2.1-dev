"""Focused closure checks for typed LEM and reviewed identity contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import pytest

from auto_foundry_core.contracts import (
    AggregationSpec,
    CanonicalMapping,
    IdentityDecision,
    KnowledgeDelta,
    LEMRef,
    OntologyItem,
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


def test_later_delta_snapshots_nested_frozen_contracts_and_rejects_invalid_delta_atomically():
    model = LivingEnterpriseModel(run_id="r")
    model.add_ontology_item(OntologyItem("supplier", "entity", "Supplier"))
    model.add_ontology_item(OntologyItem("purchase-order", "entity", "Purchase order"))
    embedded = OntologyItem(
        "embedded",
        "object",
        "Embedded",
        properties={"attributes": {"region": "north"}},
        metadata={"source": "contract"},
    )
    first = KnowledgeDelta(
        "d-relationship",
        "add_relationship",
        {
            "relationship_id": "rel-1",
            "label": "Relationship",
            "source_id": "supplier",
            "target_id": "purchase-order",
            "metadata": {
                "embedded": embedded,
                "evidence": [{"contract": embedded}, (embedded,)],
                "links": {LEMRef("ontology", "embedded")},
            },
        },
        accepted=True,
    )
    second = KnowledgeDelta(
        "d-metric",
        "add_metric",
        {"item_id": "metric-1", "label": "Metric", "metadata": {"embedded": embedded}},
        accepted=True,
    )

    assert model.apply_delta(first)["applied"]
    assert model.apply_delta(second)["applied"]
    assert model.knowledge["d-relationship"]["payload"]["metadata"]["embedded"] is embedded
    exported = model.export()
    exported_bytes = json.dumps(exported, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert exported["relationships"]["rel-1"]["metadata"]["embedded"]["item_id"] == "embedded"
    exported["relationships"]["rel-1"]["metadata"]["embedded"]["properties"]["attributes"]["region"] = "south"
    exported["relationships"]["rel-1"]["metadata"]["evidence"][0]["contract"]["label"] = "Changed"
    exported["relationships"]["rel-1"]["metadata"]["links"].append({"namespace": "ontology", "object_id": "mutated"})
    exported["knowledge"]["d-relationship"]["payload"]["metadata"]["embedded"]["metadata"]["source"] = "changed"
    exported["ontology"][0]["metadata"]["embedded"]["properties"]["attributes"]["region"] = "west"
    assert json.dumps(exported, sort_keys=True, separators=(",", ":")).encode("utf-8") != exported_bytes
    assert embedded.properties["attributes"]["region"] == "north"
    assert embedded.metadata["source"] == "contract"
    assert model.relationships["rel-1"]["metadata"]["embedded"] is embedded
    assert model.knowledge["d-relationship"]["payload"]["metadata"]["embedded"] is embedded
    assert model.ontology["metric-1"].metadata["embedded"] is embedded

    before = model.export()
    before_bytes = json.dumps(before, sort_keys=True, separators=(",", ":")).encode("utf-8")

    invalid = KnowledgeDelta(
        "d-invalid",
        "supersede",
        supersedes=(LEMRef("ontology", "missing"),),
        accepted=True,
    )
    with pytest.raises(KeyError):
        model.apply_delta(invalid)

    after = model.export()
    assert after == before
    assert json.dumps(after, sort_keys=True, separators=(",", ":")).encode("utf-8") == before_bytes
    assert "d-invalid" not in model.knowledge
    assert all(revision["delta_id"] != "d-invalid" for revision in model.revisions)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_lem_export_rejects_nonfinite_float_evidence(value: float):
    model = LivingEnterpriseModel(run_id="r")
    model.relationships["rel"] = {"relationship_id": "rel", "evidence": value}
    with pytest.raises(TypeError, match="finite floats"):
        model.export()


def test_lem_export_rejects_non_string_mapping_keys():
    model = LivingEnterpriseModel(run_id="r")
    model.relationships["rel"] = {"relationship_id": "rel", "evidence": {1: "not-json-contract"}}
    with pytest.raises(TypeError, match="mapping keys must be strings"):
        model.export()


def test_lem_export_rejects_foreign_frozen_dataclass_even_with_to_dict():
    @dataclass(frozen=True)
    class ForeignEvidence:
        value: str

        def to_dict(self) -> dict[str, str]:
            return {"value": self.value}

    model = LivingEnterpriseModel(run_id="r")
    model.relationships["rel"] = {"relationship_id": "rel", "evidence": ForeignEvidence("foreign")}
    with pytest.raises(TypeError, match="does not support frozen dataclass"):
        model.export()


def test_lem_export_validates_original_top_level_contract_graph():
    @dataclass(frozen=True)
    class ForeignEvidence:
        value: str

        def to_dict(self) -> dict[str, str]:
            return {"value": self.value}

    key_model = LivingEnterpriseModel(run_id="r-key")
    key_model.ontology["bad-key"] = OntologyItem(
        "bad-key",
        "object",
        "Bad key",
        properties={"nested": {1: "must reject"}},
    )
    with pytest.raises(TypeError, match="mapping keys must be strings"):
        key_model.export()

    dataclass_model = LivingEnterpriseModel(run_id="r-dataclass")
    dataclass_model.ontology["bad-dataclass"] = OntologyItem(
        "bad-dataclass",
        "object",
        "Bad dataclass",
        metadata={"foreign": ForeignEvidence("must reject")},
    )
    with pytest.raises(TypeError, match="does not support frozen dataclass"):
        dataclass_model.export()


def test_late_delta_failure_restores_every_lem_registry_after_mutation():
    class LateFailingModel(LivingEnterpriseModel):
        def add_metric(self, item=None, **values):
            super().add_metric(item, **values)
            raise RuntimeError("late metric failure")

    model = LateFailingModel(run_id="r")
    model.apply_delta(
        KnowledgeDelta(
            "seed-relationship",
            "add_relationship",
            {"relationship_id": "seed-rel", "label": "Seed", "scope": "shared"},
            accepted=True,
        )
    )
    model.register_prepared_asset(PreparedAssetDescriptor("seed-asset", location="seed.json"))
    model.apply_delta(
        KnowledgeDelta(
            "seed-conflict",
            "record_conflict",
            {"working_definition": "seed", "scope": "shared", "unresolved": True},
            conflicts_with=("seed-relationship",),
            accepted=True,
        )
    )
    model.apply_delta(
        KnowledgeDelta(
            "seed-supersede",
            "supersede",
            supersedes=(LEMRef("ontology", "seed-rel"), LEMRef("prepared_asset", "seed-asset")),
            accepted=True,
        )
    )
    before_state = {
        "ontology": dict(model.ontology),
        "prepared_assets": dict(model.prepared_assets),
        "canonical_mappings": dict(model.canonical_mappings),
        "identity_decisions": dict(model.identity_decisions),
        "relationships": dict(model.relationships),
        "knowledge": dict(model.knowledge),
        "conflicts": list(model.conflicts),
        "conflict_links": {key: set(value) for key, value in model.conflict_links.items()},
        "supersession_links": {key: set(value) for key, value in model.supersession_links.items()},
        "conflict_state": dict(model.conflict_state),
        "revisions": list(model.revisions),
    }
    before_bytes = json.dumps(model.export(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    with pytest.raises(RuntimeError, match="late metric failure"):
        model.apply_delta(KnowledgeDelta("late-failure", "add_metric", {"item_id": "late", "label": "Late"}, accepted=True))

    for name, expected in before_state.items():
        assert getattr(model, name) == expected
    assert json.dumps(model.export(), sort_keys=True, separators=(",", ":")).encode("utf-8") == before_bytes
    assert "late" not in model.ontology
    assert "late-failure" not in model.knowledge
    assert all(revision["delta_id"] != "late-failure" for revision in model.revisions)


def test_semantic_operations_are_ontology_index_items_and_relationships_bounded():
    model = LivingEnterpriseModel(run_id="r")
    for kind in ("metric", "definition", "rule", "process"):
        model.apply_delta(KnowledgeDelta(f"d-{kind}", f"add_{kind}", {"item_id": kind, "label": kind.title()}, accepted=True))
    reviewed = IdentityDecision("relationship-candidates", "same_object", decision_id="relationship-decision", reviewer_ref="reviewer", review_status="reviewed")
    model.register_identity_decision(reviewed)
    model.add_mapping(CanonicalMapping("a", "entity", ("source-a",), reviewed.decision_id))
    model.add_mapping(CanonicalMapping("b", "entity", ("source-b",), reviewed.decision_id))
    model.apply_delta(KnowledgeDelta("d-rel", "add_relationship", {"relationship_id": "rel", "label": "Rel", "source_id": "a", "target_id": "b", "scope": "s", "effective_period": "2024"}, accepted=True))
    indexed = {item["item_id"]: item["item_type"] for item in model.ontology_index}
    assert indexed == {"definition": "definition", "metric": "metric", "process": "process", "rel": "relationship", "rule": "rule"}
    assert model.relevant_bundle(["metric"], relationship_ids=["rel"], scope="s", effective_period="2024")["metadata"]["total_count"] == 2


def test_mapping_idempotence_alias_normalization_and_collision():
    model = LivingEnterpriseModel(run_id="r")
    reviewed = IdentityDecision("candidate-1", "same_object", decision_id="decision-1", reviewer_ref="reviewer", review_status="reviewed")
    reviewed_2 = IdentityDecision("candidate-2", "alternate_representation", decision_id="decision-2", reviewer_ref="reviewer", review_status="reviewed")
    model.register_identity_decision(reviewed)
    model.register_identity_decision(reviewed_2)
    mapping = CanonicalMapping("c1", "entity", ("a", "a2", "a3"), reviewed.decision_id)
    assert model.add_mapping(mapping) == mapping
    assert model.add_mapping(mapping) == mapping
    with pytest.raises(ValueError, match="unique"):
        model.add_mapping(CanonicalMapping("duplicate-sources", "entity", ("a", "a"), reviewed.decision_id))
    with pytest.raises(ValueError, match="different value"):
        model.add_mapping(CanonicalMapping("c1", "entity", ("b",), "decision-2"))
    model.apply_delta(KnowledgeDelta("alias", "add_alias", {"canonical_id": "c1", "alias": "Alpha", "source_identity": "a2"}, accepted=True))
    assert model.canonical_mappings["c1"].aliases == ("Alpha",)
    assert model.canonical_mappings["c1"].source_identities == ("a", "a2", "a3")


def test_mapping_requires_registered_reviewed_decision():
    model = LivingEnterpriseModel(run_id="r")
    missing = CanonicalMapping("missing", "entity", ("a",), "decision-missing")
    with pytest.raises(ValueError, match="registered identity decision"):
        model.add_mapping(missing)
    pending = IdentityDecision("candidate-pending", "different_objects", decision_id="decision-pending", reviewer_ref="reviewer", review_status="pending")
    with pytest.raises(ValueError, match="publication"):
        model.register_identity_decision(pending)
    rejected_semantic = IdentityDecision("candidate-rejected", "different_objects", decision_id="decision-rejected", reviewer_ref="reviewer", review_status="accepted")
    model.register_identity_decision(rejected_semantic)
    with pytest.raises(ValueError, match="same_object or alternate_representation"):
        model.add_mapping(CanonicalMapping("rejected", "entity", ("a",), rejected_semantic.decision_id))
    accepted = IdentityDecision("candidate-accepted", "same_object", decision_id="decision-accepted", reviewer_ref="reviewer", review_status="accepted")
    model.register_identity_decision(accepted)
    mapping = CanonicalMapping("accepted", "entity", ("a",), accepted.decision_id)
    assert model.add_mapping(mapping) == mapping
    assert model.add_mapping(mapping) == mapping


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
    defaults = model.relevant_bundle(["o"])["metadata"]
    assert defaults["limits"]["total"] > 0
    assert defaults["limits"]["bytes"] > 0
    assert set(defaults["limits"]["per_layer"]) == {"ontology", "prepared_assets", "mappings", "relationships"}
    for index in range(257):
        model.add_ontology_item({"item_id": f"o-{index}", "item_type": "object", "label": "O"})
    with pytest.raises(ValueError, match="ontology layer exceeds limit"):
        model.relevant_bundle([f"o-{index}" for index in range(257)])
    with pytest.raises((TypeError, ValueError)):
        model.relevant_bundle(["o"], max_bytes=float("inf"))


def test_aggregation_spec_is_explicit_without_distinct_flag():
    spec = AggregationSpec("share", numerator=2, denominator=4, period_order=("2023", "2024"))
    assert "distinct" not in spec.to_dict()
    assert spec.period_order == ("2023", "2024")
