"""Deterministic fake-role harness for offline Requirement Mode coverage.

This harness is intentionally not an analytics implementation.  It exercises
the boundaries between the Portfolio Planner, Navigator, Lead Analyst,
Independent Reviewer fallback, and the run-local LEM acceptance matrix using
small structured records only.  No model, core, source, network, or file
system operation is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence


class ExactIDValidationError(ValueError):
    """A Navigator bundle ID is unknown, out of scope, or not evidence-bound."""


@dataclass(frozen=True)
class FakeRequirement:
    requirement_id: str
    original_text: str
    priority: int | None
    scope_classification: str
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompactIndexRecord:
    record_id: str
    layer: str
    record_type: str
    run_id: str
    evidence_refs: tuple[str, ...]
    effective_scope: str


@dataclass(frozen=True)
class CandidateLEMRecord:
    record_id: str
    semantic_key: str
    definition: str
    effective_scope: str
    evidence_refs: tuple[str, ...]


@dataclass
class StoredLEMRecord:
    record_id: str
    semantic_key: str
    definition: str
    effective_scope: str
    evidence_refs: tuple[str, ...]
    status: str = "promoted_with_limits"
    supersedes: str | None = None


@dataclass(frozen=True)
class PlannerResult:
    ordered_ids: tuple[str, ...]
    classifications: Mapping[str, str]
    rationale: Mapping[str, str]
    full_portfolio_seen: bool


@dataclass(frozen=True)
class ReviewResult:
    review_status: str
    review_strength: str
    verdict: str


@dataclass(frozen=True)
class RequirementResult:
    requirement_id: str
    route: str
    selected_ids: tuple[str, ...]
    review: ReviewResult
    lem_action: str


class FakePortfolioPlanner:
    """See the entire portfolio, then honor explicit priority deterministically."""

    def __init__(self) -> None:
        self.portfolios_seen: list[tuple[str, ...]] = []

    def plan(self, requirements: Sequence[FakeRequirement]) -> PlannerResult:
        ids = tuple(item.requirement_id for item in requirements)
        self.portfolios_seen.append(ids)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate requirement IDs")
        by_id = {item.requirement_id: item for item in requirements}
        if any(item.scope_classification not in {"analytics_in_scope", "analytics_requires_missing_data", "out_of_analytics_scope"} for item in requirements):
            raise ValueError("invalid semantic scope classification")
        # Explicit priority is primary; unset items retain supplied order in
        # this fake so dependency/reuse rationale remains observable.
        ordered = tuple(
            item.requirement_id
            for item in sorted(
                requirements,
                key=lambda item: (item.priority is None, item.priority if item.priority is not None else len(requirements), ids.index(item.requirement_id)),
            )
        )
        rationale = {
            item.requirement_id: ("explicit priority" if item.priority is not None else "supplied order; no explicit priority")
            for item in requirements
        }
        # Referencing dependencies here proves the planner saw them without
        # silently creating a second requirement or keyword router.
        for item in requirements:
            for dependency in item.dependencies:
                if dependency not in by_id:
                    rationale[item.requirement_id] += "; dependency missing"
        return PlannerResult(ordered, {item.requirement_id: item.scope_classification for item in requirements}, rationale, True)


class FakeNavigator:
    """Validate exact compact-index IDs before returning any full records."""

    def __init__(self) -> None:
        self.selections: list[tuple[str, tuple[str, ...]]] = []

    def select(
        self,
        requirement: FakeRequirement,
        compact_indexes: Mapping[str, CompactIndexRecord],
        selected_ids: Iterable[str],
        *,
        run_id: str,
        allowed_layers: set[str],
    ) -> tuple[CompactIndexRecord, ...]:
        selected = tuple(selected_ids)
        result: list[CompactIndexRecord] = []
        for record_id in selected:
            record = compact_indexes.get(record_id)
            if record is None:
                raise ExactIDValidationError(f"unknown exact ID: {record_id}")
            if record.run_id != run_id:
                raise ExactIDValidationError(f"cross-run exact ID: {record_id}")
            if record.layer not in allowed_layers:
                raise ExactIDValidationError(f"layer out of scope for {record_id}: {record.layer}")
            if not record.evidence_refs:
                raise ExactIDValidationError(f"evidence missing for exact ID: {record_id}")
            result.append(record)
        self.selections.append((requirement.requirement_id, selected))
        return tuple(result)


class FakeLeadAnalyst:
    """Produce a bounded answer envelope from the Navigator bundle only."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def analyze(self, requirement: FakeRequirement, bundle: Sequence[CompactIndexRecord], lem_action: str) -> dict[str, Any]:
        if not bundle:
            raise ValueError("fake analyst requires a bounded bundle")
        self.calls.append(requirement.requirement_id)
        route = "prepared_reuse" if lem_action == "found_reuse" else ("extend_reuse" if lem_action == "extend" else "fresh")
        return {
            "requirement_id": requirement.requirement_id,
            "route": route,
            "answer": f"bounded fake answer for {requirement.requirement_id}",
            "evidence_refs": [ref for record in bundle for ref in record.evidence_refs],
            "new_analytics": False,
        }


class FakeReviewer:
    """Model independent review and explicit unavailable fallback disclosure."""

    def __init__(self, available: bool) -> None:
        self.available = available
        self.calls: list[str] = []

    def review(self, answer: Mapping[str, Any]) -> ReviewResult:
        requirement_id = str(answer["requirement_id"])
        self.calls.append(requirement_id)
        if self.available:
            return ReviewResult("available", "independent", "accept_with_limits")
        return ReviewResult("unavailable", "none", "accept_with_limits")


class FakeLivingEnterpriseModel:
    """Small acceptance matrix: reuse, extend, fresh, conflict/supersession."""

    def __init__(self, records: Iterable[StoredLEMRecord] = ()) -> None:
        self.records: list[StoredLEMRecord] = list(records)
        self.history: list[StoredLEMRecord] = []

    def accept(self, candidate: CandidateLEMRecord) -> str:
        matches = [record for record in self.records if record.semantic_key == candidate.semantic_key]
        if not matches:
            self.records.append(
                StoredLEMRecord(candidate.record_id, candidate.semantic_key, candidate.definition, candidate.effective_scope, candidate.evidence_refs)
            )
            return "fresh"
        current = matches[-1]
        if current.definition != candidate.definition:
            superseded = replace(current, status="superseded", supersedes=candidate.record_id)
            self.history.append(superseded)
            self.records.remove(current)
            self.records.append(
                StoredLEMRecord(candidate.record_id, candidate.semantic_key, candidate.definition, candidate.effective_scope, candidate.evidence_refs, supersedes=current.record_id)
            )
            return "conflict_supersession"
        if current.effective_scope == candidate.effective_scope and set(candidate.evidence_refs).issubset(current.evidence_refs):
            return "found_reuse"
        current.effective_scope = candidate.effective_scope
        current.evidence_refs = tuple(dict.fromkeys((*current.evidence_refs, *candidate.evidence_refs)))
        current.status = "promoted_with_limits"
        return "extend"


class FakeRequirementRun:
    """Execute one planner/navigator/analyst/reviewer pass per requirement."""

    def __init__(self, *, reviewer_available: bool = False) -> None:
        self.planner = FakePortfolioPlanner()
        self.navigator = FakeNavigator()
        self.analyst = FakeLeadAnalyst()
        self.reviewer = FakeReviewer(reviewer_available)
        self.lem = FakeLivingEnterpriseModel()

    def run(
        self,
        requirements: Sequence[FakeRequirement],
        indexes: Mapping[str, CompactIndexRecord],
        selected_ids: Mapping[str, Sequence[str]],
        candidates: Mapping[str, CandidateLEMRecord],
        *,
        run_id: str,
    ) -> tuple[PlannerResult, tuple[RequirementResult, ...]]:
        plan = self.planner.plan(requirements)
        results: list[RequirementResult] = []
        by_id = {item.requirement_id: item for item in requirements}
        for requirement_id in plan.ordered_ids:
            requirement = by_id[requirement_id]
            bundle = self.navigator.select(requirement, indexes, selected_ids[requirement_id], run_id=run_id, allowed_layers={"ontology", "prepared"})
            lem_action = self.lem.accept(candidates[requirement_id])
            answer = self.analyst.analyze(requirement, bundle, lem_action)
            review = self.reviewer.review(answer)
            results.append(RequirementResult(requirement_id, answer["route"], tuple(record.record_id for record in bundle), review, lem_action))
        return plan, tuple(results)
