"""Behavioral, offline fake-role coverage for Requirement Mode."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_requirement_mode import (  # noqa: E402
    CandidateLEMRecord,
    CompactIndexRecord,
    ExactIDValidationError,
    FakeLivingEnterpriseModel,
    FakeRequirement,
    FakeRequirementRun,
    StoredLEMRecord,
)


def test_requirement_roles_plan_navigate_analyze_and_disclose_reviewer_fallback() -> None:
    requirements = (
        FakeRequirement("R-001", "Review late delivery concentration", 2, "analytics_in_scope"),
        FakeRequirement("R-002", "Reconcile a missing field", 1, "analytics_requires_missing_data"),
    )
    indexes = {
        "O-001": CompactIndexRecord("O-001", "ontology", "metric", "RUN-FAKE", ("E-O-001",), "2024"),
        "P-001": CompactIndexRecord("P-001", "prepared", "view", "RUN-FAKE", ("E-P-001",), "2024"),
    }
    candidates = {
        "R-001": CandidateLEMRecord("LEM-001", "delivery.late_rate", "late rate", "2024", ("E-O-001",)),
        "R-002": CandidateLEMRecord("LEM-002", "payments.reconciliation", "receipt match", "2024", ("E-P-001",)),
    }
    run = FakeRequirementRun(reviewer_available=False)
    plan, results = run.run(
        requirements,
        indexes,
        {"R-001": ("O-001",), "R-002": ("P-001",)},
        candidates,
        run_id="RUN-FAKE",
    )

    assert plan.full_portfolio_seen is True
    assert plan.ordered_ids == ("R-002", "R-001")
    assert plan.classifications["R-002"] == "analytics_requires_missing_data"
    assert results[0].requirement_id == "R-002"
    assert all(result.route == "fresh" for result in results)
    assert all(result.review.review_status == "unavailable" for result in results)
    assert all(result.review.review_strength == "none" for result in results)
    assert all(result.review.verdict == "not_reviewed" for result in results)
    assert run.planner.portfolios_seen == [("R-001", "R-002")]
    assert run.navigator.selections == [("R-002", ("P-001",)), ("R-001", ("O-001",))]
    assert run.analyst.calls == ["R-002", "R-001"]
    assert run.reviewer.calls == ["R-002", "R-001"]


def test_lem_acceptance_matrix_found_reuse_extend_fresh_and_conflict_supersession() -> None:
    lem = FakeLivingEnterpriseModel(
        [StoredLEMRecord("LEM-OLD", "orders.otif", "arrival proxy", "2024-Q1", ("E-1",))]
    )
    assert lem.accept(CandidateLEMRecord("LEM-REUSE", "orders.otif", "arrival proxy", "2024-Q1", ("E-1",))) == "found_reuse"
    assert lem.accept(CandidateLEMRecord("LEM-EXTEND", "orders.otif", "arrival proxy", "2024-Q2", ("E-2",))) == "extend"
    assert lem.records[0].effective_scope == "2024-Q2"
    assert lem.records[0].evidence_refs == ("E-1", "E-2")
    assert lem.accept(CandidateLEMRecord("LEM-FRESH", "orders.promise", "accepted promise", "2024-Q2", ("E-3",))) == "fresh"
    assert lem.accept(CandidateLEMRecord("LEM-CONFLICT", "orders.otif", "customer promise", "2024-Q2", ("E-4",))) == "conflict_supersession"
    assert any(record.record_id == "LEM-OLD" and record.status == "superseded" for record in lem.history)
    current = [record for record in lem.records if record.semantic_key == "orders.otif"][-1]
    assert current.record_id == "LEM-CONFLICT"
    assert current.supersedes == "LEM-OLD"


def test_navigator_rejects_scoped_exact_ids_before_analysis() -> None:
    requirement = FakeRequirement("R-001", "Use a reviewed view", 1, "analytics_in_scope")
    navigator = FakeRequirementRun().navigator
    indexes = {
        "P-OTHER-RUN": CompactIndexRecord("P-OTHER-RUN", "prepared", "view", "RUN-OTHER", ("E-1",), "2024"),
        "O-WRONG-LAYER": CompactIndexRecord("O-WRONG-LAYER", "ontology", "metric", "RUN-FAKE", ("E-2",), "2024"),
        "P-NO-EVIDENCE": CompactIndexRecord("P-NO-EVIDENCE", "prepared", "view", "RUN-FAKE", (), "2024"),
    }
    for selected in (("UNKNOWN",), ("P-OTHER-RUN",), ("O-WRONG-LAYER",), ("P-NO-EVIDENCE",)):
        with pytest.raises(ExactIDValidationError):
            navigator.select(requirement, indexes, selected, run_id="RUN-FAKE", allowed_layers={"prepared"})
