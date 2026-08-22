from auto_foundry_core.aggregation import aggregate_rows
import pytest

from auto_foundry_core.contracts import IdentityDecision, KnowledgeDelta, RequirementRecord
from auto_foundry_core.identity import apply_decision, generate_candidates, mapping_coverage
from auto_foundry_core.populations import PopulationLedger
from auto_foundry_core.relationships import measure_relationship


def test_identity_is_candidate_evidence_until_reviewed():
    candidates = generate_candidates(
        [{"id": "entity-a", "name": "North Group"}, {"id": "entity-b", "name": "North Group"}],
        [{"id": "entity-c", "name": "North Grp"}, {"id": "entity-d", "name": "South Group"}],
        object_type="entity", compare_fields=["name"], threshold=0.4,
    )
    assert candidates
    candidate = candidates[0]
    assert candidate.status == "unresolved"
    decision = IdentityDecision(
        candidate.candidate_id,
        "same_object",
        reviewer_ref="review",
        review_status="reviewed",
        rationale="evidence",
    )
    result = apply_decision(candidate, decision, canonical_id="entity-canonical")
    assert result.canonical_id == "entity-canonical"
    assert result.source_identities == (candidate.left_id, candidate.right_id)
    assert mapping_coverage([result], result.source_identities)["coverage"] == 1.0


def test_identity_vocab_covers_multiple_object_classes_and_nonmerge_states():
    fixtures = [
        ("entity", {"id": "e1", "label": "North Group"}, {"id": "e2", "label": "North Group"}),
        ("document", {"id": "d1", "label": "Record 42"}, {"id": "d2", "label": "Record-42"}),
        ("item", {"id": "i1", "label": "Model A"}, {"id": "i2", "label": "Model B"}),
        ("site", {"id": "s1", "label": "Depot East"}, {"id": "s2", "label": "Depot West"}),
    ]
    for object_type, left, right in fixtures:
        candidates = generate_candidates([left], [right], object_type=object_type, compare_fields=["label"], threshold=0.2)
        assert candidates
        candidate = candidates[0]
        assert candidate.object_type == object_type
        assert candidate.status == "unresolved"
        with pytest.raises(ValueError):
            apply_decision(candidate, IdentityDecision(candidate.candidate_id, "same_object"))
        different = apply_decision(candidate, IdentityDecision(candidate.candidate_id, "different_objects"))
        assert different.status == "different_objects"
    assert {"same_object", "different_objects", "possible_match", "insufficient_evidence", "version_of", "parent_child", "alternate_representation"}.issubset(IdentityDecision.VOCABULARY)


def test_requirement_record_keeps_explicit_user_owned_fields():
    record = RequirementRecord(
        requirement_id="req-1",
        original_text="Compare two periods for a decision.",
        explicit_priority=2,
        business_objective="support a decision",
        expected_analytical_outputs=("comparison",),
        expected_visual_outputs=("trend",),
        dependencies=("source-a",),
        data_needs=("records",),
        ontology_needs=("object",),
        prepared_data_needs=("prepared-records",),
        working_definitions=("period",),
        limitations=("bounded sample",),
        status="queued",
    )
    payload = record.to_dict()
    for key in ("original_text", "explicit_priority", "business_objective", "expected_analytical_outputs", "expected_visual_outputs", "dependencies", "data_needs", "ontology_needs", "prepared_data_needs", "working_definitions", "limitations", "status"):
        assert key in payload
    assert payload["original_text"].startswith("Compare")


def test_relationship_population_and_currency_safe_aggregation():
    diagnostic = measure_relationship([{"key": "a"}, {"key": "a"}, {"key": "b"}], [{"key": "a"}, {"key": "c"}], left_key="key")
    assert diagnostic["cardinality"] == "many_to_one"
    assert diagnostic["left_unmatched"] == 1
    ledger = PopulationLedger(["a", "b", "c"], eligible=["a"])
    ledger.exclude(["b"], "missing-value").mark_unresolved(["c"])
    reconciliation = ledger.reconcile()
    assert reconciliation["excluded"] == 1
    assert reconciliation["reason_counts"] == {"missing-value": 1}
    assert reconciliation["reconciles"]
    totals = aggregate_rows([{"value": 2, "currency": "X"}, {"value": 3, "currency": "X"}, {"value": 5, "currency": "Y"}], "sum", value_field="value", currency_field="currency")
    assert totals == {"X": 5.0, "Y": 5.0}
    ranking = aggregate_rows([{"group": "a", "value": 2}, {"group": "b", "value": 5}], "ranking", value_field="value", group_by=["group"])
    assert ranking[0]["group"] == "b"
