"""Requirement semantic refresh regressions for run-level resolution commits."""

from __future__ import annotations

import multiprocessing
import zipfile
from pathlib import Path
from typing import Any

import pytest

from auto_foundry_core import (
    AnalystWorkspace,
    CanonicalMapping,
    DataAssetRef,
    EntityResolutionResult,
    EntityResolutionWorkspace,
    IdentityDecision,
    IdentityMappingView,
    ItemWorkspace,
    OntologyItem,
    PreparedAssetRegistry,
    RequirementAnalysisTask,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.analysis import BoundAnalysisContext
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.semantic_store import SemanticSnapshotStore


def _refresh_process_worker(
    run_root: str,
    input_root: str,
    archive: str,
    gate: Any,
    results: Any,
) -> None:
    try:
        gate.wait(10)
        context = RunContext(
            "RUN-REQ-REFRESH",
            run_root,
            (input_root,),
            core_version="0.1",
            skill_version="0.1",
        )
        lifecycle = RunLifecycle.load(context)
        item = ItemWorkspace.load(context, "REQ-01", mode="requirement")
        refreshed = BoundAnalysisContext.refresh_requirement_semantics(
            context,
            item,
            lifecycle,
        )
        results.put(("refresh", "ok", refreshed.manifest_hash))
    except BaseException as exc:  # pragma: no cover - surfaced by parent assertion
        results.put(("refresh", "error", f"{type(exc).__name__}: {exc}"))


def _commit_process_worker(
    run_root: str,
    input_root: str,
    gate: Any,
    results: Any,
) -> None:
    try:
        gate.wait(10)
        context = RunContext(
            "RUN-REQ-REFRESH",
            run_root,
            (input_root,),
            core_version="0.1",
            skill_version="0.1",
        )
        resolver = EntityResolutionWorkspace.load(context)
        commit = resolver.commit("customer-domain")
        results.put(("commit", "ok", commit.manifest_hash))
    except BaseException as exc:  # pragma: no cover - surfaced by parent assertion
        results.put(("commit", "error", f"{type(exc).__name__}: {exc}"))


def _fixture(tmp_path: Path) -> tuple[RunContext, RunLifecycle, Path]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("orders.csv", "order_id,region\nA-1,DE\n")
    context = RunContext(
        "RUN-REQ-REFRESH",
        tmp_path / "run",
        (inputs,),
        core_version="0.1",
        skill_version="0.1",
    )
    lifecycle = RunLifecycle.create(context, ("REQ-01", "REQ-02"), mode="requirement")
    return context, lifecycle, archive


def _accepted_resolution() -> EntityResolutionResult:
    decision = IdentityDecision(
        candidate_id="candidate-1",
        decision="same_object",
        decision_id="decision-1",
        review_status="accepted",
        reviewer_ref="resolution-reviewer",
        evidence_refs=("resolution-evidence",),
    )
    mapping = CanonicalMapping(
        canonical_id="customer-1",
        object_type="customer",
        source_identities=("account-a", "account-b"),
        decision_id="decision-1",
    )
    return EntityResolutionResult(
        ontology_items=(OntologyItem(item_id="customer", item_type="entity", label="Customer"),),
        identity_decisions=(decision,),
        canonical_mappings=(mapping,),
        representation_relationships=(),
        coverage={"source_count": 2, "mapped_count": 2},
        population={"source_ids": ["account-a", "account-b"]},
        exceptions=(),
        metadata={"method": "deterministic fixture"},
        evidence_refs=("work/resolution-evidence.json",),
        script_receipt_refs=("script_receipts/resolution.json",),
        source_hash="a" * 64,
    )


def _commit_resolution(context: RunContext) -> EntityResolutionWorkspace:
    workspace = EntityResolutionWorkspace.create(context)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-01",
        "resolve customer representations",
        source_hints=("orders.csv",),
        representation_item_ids=("erp-customer", "ecom-customer"),
    )
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_resolution(),
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "independent-reviewer")
    workspace.commit("customer-domain")
    return workspace


def _plan_requirement(analyst: AnalystWorkspace) -> None:
    analyst.plan_requirement(
        tasks=(RequirementAnalysisTask(task_id="T-1", question="Inspect the supplied rows."),),
        synthesis_intent="Answer the requirement from bounded evidence.",
    )


def test_refresh_replays_resolution_only_semantics_and_preserves_active_attempt(tmp_path: Path) -> None:
    context, lifecycle, archive = _fixture(tmp_path)
    first = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="first requirement")
    second = ItemWorkspace.create(context, "REQ-02", mode="requirement", original_text="second requirement")
    first_bound = BoundAnalysisContext.create_for_requirement(
        context, DataAssetRef.from_path(archive), first, lifecycle
    )
    second_bound = BoundAnalysisContext.create_for_requirement(
        context, DataAssetRef.from_path(archive), second, lifecycle
    )
    owner = AnalystWorkspace(first_bound, owner_ref="owner-REQ-01")
    _plan_requirement(owner)
    attempt = first.begin_attempt("lead", "analytical_owner")

    resolver = EntityResolutionWorkspace.create(context)
    owner.propose_identity_domain(
        "customer-domain",
        "customer",
        "resolve customer representations",
        ("orders.csv",),
        ("erp-customer", "ecom-customer"),
    )
    resolver.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-01",
        "resolve customer representations",
        source_hints=("orders.csv",),
        representation_item_ids=("erp-customer", "ecom-customer"),
    )
    owner.mark_waiting_on_resolution(resolver, ("customer-domain",), "reviewed mapping required")
    assert resolver.requirement_runtime_statuses()["REQ-01"]["state"] == "waiting_on_resolution"
    resolver.claim_resolution_owner("customer-domain", "resolution-owner")
    resolver.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_resolution(),
        expected_scope_hash=resolver.current_scope("customer-domain").scope_hash,
    )
    resolver.record_review("customer-domain", "accept", "independent-reviewer")
    resolver.commit("customer-domain")
    assert resolver.requirement_runtime_statuses()["REQ-01"]["state"] == "ready_to_resume"
    resolver.acknowledge_requirement_resume("REQ-01", owner_ref="owner-REQ-01")

    refreshed = owner.refresh_semantic_scope(lifecycle)
    refreshed_projection = SemanticSnapshotStore.manifest(
        context, refreshed.semantic_snapshot_ref
    )["projection"]
    assert tuple(refreshed_projection["source_item_ids"]) == ()
    assert tuple(refreshed_projection["source_resolution_domain_ids"]) == ("customer-domain",)
    assert first.state["active_attempt_id"] == attempt.attempt_id
    assert {item.item_id for item in owner.search_ontology()} == {"customer"}
    assert [mapping.canonical_id for mapping in owner.search_identity_mappings()] == ["customer-1"]
    assert [decision.decision_id for decision in owner.search_identity_decisions()] == ["decision-1"]
    owner.select_ontology(("customer",), purpose="reuse reviewed customer ontology")
    owner.select_identity_mappings(("customer-1",), purpose="reuse reviewed customer mapping")
    mapping_view = owner.materialize_identity_mapping_view(
        object_types=("customer",),
        purpose="provide reviewed customer lookup to the calculation",
    )
    assert mapping_view.resolve("account-a") == "customer-1"
    assert mapping_view.resolve("unknown") is None
    persisted_view = IdentityMappingView.load(first.work_root / "identity_mapping_view.json")
    assert persisted_view.to_dict() == mapping_view.to_dict()

    # The pre-created second item has no item integration, but the same
    # run-level resolution commit is visible after its ordinary refresh.
    refreshed_second = BoundAnalysisContext.refresh_requirement_semantics(
        context, second, lifecycle
    )
    refreshed_second_projection = SemanticSnapshotStore.manifest(
        context, refreshed_second.semantic_snapshot_ref
    )["projection"]
    assert tuple(refreshed_second_projection["source_item_ids"]) == ()
    assert tuple(refreshed_second_projection["source_resolution_domain_ids"]) == ("customer-domain",)
    assert [mapping.canonical_id for mapping in AnalystWorkspace(
        refreshed_second, owner_ref="owner-REQ-02"
    ).search_identity_mappings()] == ["customer-1"]

    manifest_bytes = refreshed.manifest_path.read_bytes()
    retry = owner.refresh_semantic_scope(lifecycle)
    assert retry.manifest_hash == refreshed.manifest_hash
    assert retry.manifest_path.read_bytes() == manifest_bytes


def test_refresh_and_resolution_commit_cross_process_race_has_no_deadlock(tmp_path: Path) -> None:
    context, lifecycle, archive = _fixture(tmp_path)
    item = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="first requirement")
    bound = BoundAnalysisContext.create_for_requirement(
        context, DataAssetRef.from_path(archive), item, lifecycle
    )
    owner = AnalystWorkspace(bound, owner_ref="owner-REQ-01")
    owner.propose_identity_domain(
        "customer-domain",
        "customer",
        "resolve customer representations",
        ("orders.csv",),
        ("erp-customer", "ecom-customer"),
    )
    _commit_resolution_pending = EntityResolutionWorkspace.create(context)
    _commit_resolution_pending.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-01",
        "resolve customer representations",
        source_hints=("orders.csv",),
        representation_item_ids=("erp-customer", "ecom-customer"),
    )
    _commit_resolution_pending.claim_resolution_owner("customer-domain", "resolution-owner")
    _commit_resolution_pending.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_resolution(),
        expected_scope_hash=_commit_resolution_pending.current_scope("customer-domain").scope_hash,
    )
    _commit_resolution_pending.record_review("customer-domain", "accept", "independent-reviewer")

    mp = multiprocessing.get_context("fork")
    gate = mp.Event()
    results = mp.Queue()
    refresh_process = mp.Process(
        target=_refresh_process_worker,
        args=(str(context.run_root), str(archive.parent), str(archive), gate, results),
    )
    commit_process = mp.Process(
        target=_commit_process_worker,
        args=(str(context.run_root), str(archive.parent), gate, results),
    )
    refresh_process.start()
    commit_process.start()
    gate.set()
    refresh_process.join(timeout=10)
    commit_process.join(timeout=10)
    if refresh_process.is_alive():
        refresh_process.terminate()
        refresh_process.join(timeout=5)
        pytest.fail("refresh process deadlocked")
    if commit_process.is_alive():
        commit_process.terminate()
        commit_process.join(timeout=5)
        pytest.fail("resolution commit process deadlocked")

    observed = [results.get(timeout=2) for _ in range(2)]
    assert {value[0] for value in observed} == {"refresh", "commit"}
    assert all(value[1] == "ok" for value in observed), observed
    loaded = BoundAnalysisContext.load(context, item_workspace=item)
    assert loaded.manifest_hash
    assert EntityResolutionWorkspace.load(context).get_domain("customer-domain").state == "ready"
    # A retry after the race must project the committed domain into the exact
    # same ordinary context without introducing transition artifacts.
    refreshed = BoundAnalysisContext.refresh_requirement_semantics(context, item, lifecycle)
    refreshed_projection = SemanticSnapshotStore.manifest(
        context, refreshed.semantic_snapshot_ref
    )["projection"]
    assert tuple(refreshed_projection["source_resolution_domain_ids"]) == ("customer-domain",)
    assert not any(
        (item.work_root / name).exists()
        for name in (
            "analysis_context_transitions.jsonl",
            "analysis_context_transition_state.json",
            "analysis_context_transition_intent.json",
            "analysis_context_inheritance.json",
            "analysis_context_inheritance_state.json",
            "analysis_context_inheritance_intent.json",
            "analysis_context_transition.lock",
        )
    )


@pytest.mark.parametrize("terminal_state", ("draft", "review", "accepted", "integrated"))
def test_refresh_rejects_after_draft_or_terminal_state_atomically(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    context, lifecycle, archive = _fixture(tmp_path)
    item = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="requirement")
    bound = BoundAnalysisContext.create_for_requirement(
        context, DataAssetRef.from_path(archive), item, lifecycle
    )
    owner = AnalystWorkspace(bound, owner_ref="owner-REQ-01")
    item.write_plan({"item_id": item.item_id, "objective": "bounded"})
    item.write_draft({"answer": "bounded"})
    if terminal_state in {"review", "accepted", "integrated"}:
        item.record_review("accept", reviewer_ref="reviewer")
    if terminal_state in {"accepted", "integrated"}:
        item.accept(accepted_refs=("work/plan.json",))
    if terminal_state == "integrated":
        session = IntegrationSession.create(
            context,
            item,
            PreparedAssetRegistry(context),
            "integration-owner",
            invocation_id="integration-REQ-01",
        )
        session.add_ontology_item(
            OntologyItem(item_id="accepted", item_type="entity", label="Accepted"),
            scope="requirement",
            evidence_refs=("work/plan.json",),
        )
        session.record_fidelity_review(
            "accept",
            checked_record_ids=tuple(record.record_id for record in session.records),
        )
        session.commit()

    before = bound.manifest_path.read_bytes()
    with pytest.raises(ValueError):
        owner.refresh_semantic_scope(lifecycle)
    assert bound.manifest_path.read_bytes() == before
