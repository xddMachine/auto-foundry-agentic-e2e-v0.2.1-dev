"""Independent Requirement Mode context and semantic-reuse regressions."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from auto_foundry_core import AnalystWorkspace, RequirementAnalysisTask
from auto_foundry_core.analysis import BoundAnalysisContext, load_bound_analysis_context
from auto_foundry_core.contracts import DataAssetRef, OntologyItem
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.semantic_store import SemanticSnapshotStore
from auto_foundry_core.workspace import RunContext


def _archive(tmp_path: Path) -> Path:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("orders.csv", "order_id,region\nA-1,DE\n")
    return archive


def _accept(item: ItemWorkspace) -> None:
    item.write_plan({"item_id": item.item_id})
    item.write_draft({"answer": item.item_id})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json",))


def _commit_ontology(context: RunContext, item: ItemWorkspace, item_id: str) -> None:
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id=f"inv-{item_id}",
    )
    session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer"),
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    session.commit()


def _run_bytes(context: RunContext) -> dict[str, bytes]:
    return {
        path.relative_to(context.run_root).as_posix(): path.read_bytes()
        for path in context.run_root.rglob("*")
        if path.is_file()
    }


def test_failed_requirement_does_not_block_later_direct_context(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    context = RunContext("RUN-REQ", tmp_path / "run", (archive.parent,), core_version="0.1", skill_version="0.1")
    lifecycle = RunLifecycle.create(context, ("REQ-01", "REQ-02"), mode="requirement")
    run_state = json.loads((context.run_root / "run_state.json").read_text(encoding="utf-8"))
    assert set(run_state) == {
        "run_id",
        "run_root",
        "item_ids",
        "mode",
        "status",
        "generation",
        "manifest_hash",
        "created_at",
        "updated_at",
    }
    failed = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="first")
    failed.technical_failure("fixture failure", recovery_exhausted=True)
    target = ItemWorkspace.create(context, "REQ-02", mode="requirement", original_text="second")

    bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(archive),
        target,
        lifecycle,
    )

    assert bound.item_workspace.item_id == "REQ-02"
    assert bound.ontology_bundle == ()
    assert load_bound_analysis_context(context, item_workspace=target).item_workspace.item_id == "REQ-02"
    for name in (
        "analysis_context_transitions.jsonl",
        "analysis_context_transition_state.json",
        "analysis_context_transition_intent.json",
        "analysis_context_inheritance.json",
        "analysis_context_inheritance_state.json",
        "analysis_context_inheritance_intent.json",
        "analysis_context_transition.lock",
    ):
        assert not (target.work_root / name).exists()


def test_requirement_reuses_only_committed_integration_semantics(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    context = RunContext("RUN-REQ", tmp_path / "run", (archive.parent,), core_version="0.1", skill_version="0.1")
    lifecycle = RunLifecycle.create(context, ("REQ-01", "REQ-02", "REQ-03"), mode="requirement")

    committed = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="committed")
    _accept(committed)
    _commit_ontology(context, committed, "REQ-01")

    pending = ItemWorkspace.create(context, "REQ-02", mode="requirement", original_text="pending")
    _accept(pending)
    pending_session = IntegrationSession.create(
        context,
        pending,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-REQ-02",
    )
    pending_session.add_ontology_item(
        OntologyItem(item_id="pending-only", item_type="entity", label="Pending"),
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )

    target = ItemWorkspace.create(context, "REQ-03", mode="requirement", original_text="target")
    bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(archive),
        target,
        lifecycle,
    )
    ref = bound.semantic_snapshot_ref
    assert ref is not None
    manifest = SemanticSnapshotStore.manifest(context, ref)
    assert tuple(manifest["projection"]["source_item_ids"]) == ("REQ-01",)
    ontology = SemanticSnapshotStore.records(context, ref, "ontology")["ontology"]
    assert tuple(value["item_id"] for value in ontology) == ("customer",)
    assert "pending-only" not in {value["item_id"] for value in ontology}


def test_requirement_semantic_scope_gate_is_empty_first_run_and_explicit_after_cumulative_semantics(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    empty_context = RunContext("RUN-REQ-FIRST", tmp_path / "first-run", (archive.parent,), core_version="0.1", skill_version="0.1")
    empty_lifecycle = RunLifecycle.create(empty_context, ("REQ-01",), mode="requirement")
    first_item = ItemWorkspace.create(empty_context, "REQ-01", mode="requirement", original_text="first requirement")
    first_bound = BoundAnalysisContext.create_for_requirement(
        empty_context,
        DataAssetRef.from_path(archive),
        first_item,
        empty_lifecycle,
    )
    first_analyst = AnalystWorkspace(first_bound, owner_ref="owner-REQ-01")
    first_analyst.plan_requirement(
        tasks=(RequirementAnalysisTask(task_id="T-1", question="Inspect the supplied rows."),),
        synthesis_intent="Answer the first requirement from supplied evidence.",
    )
    # An empty first-run ontology is not itself a blocker, but it still needs an
    # explicit owner decision so recovery cannot silently skip semantic scouting.
    first_analyst.select_semantic_scope(
        no_reuse_reason="The first-run semantic snapshot has no accepted reusable semantics."
    )
    first_analyst.begin_analysis(objective="Inspect supplied rows.", strategy="Use bounded evidence.")

    context = RunContext("RUN-REQ-CUMULATIVE", tmp_path / "cumulative", (archive.parent,), core_version="0.1", skill_version="0.1")
    lifecycle = RunLifecycle.create(context, ("REQ-01", "REQ-02"), mode="requirement")
    committed = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="committed requirement")
    _accept(committed)
    _commit_ontology(context, committed, "REQ-01")
    target = ItemWorkspace.create(context, "REQ-02", mode="requirement", original_text="cumulative requirement")
    target_bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(archive),
        target,
        lifecycle,
    )
    target_analyst = AnalystWorkspace(target_bound, owner_ref="owner-REQ-02")
    target_analyst.plan_requirement(
        tasks=(RequirementAnalysisTask(task_id="T-1", question="Reuse exact accepted semantics."),),
        synthesis_intent="Answer the cumulative requirement with explicit scope.",
    )
    with pytest.raises(ValueError, match="semantic scope decision"):
        target_analyst.begin_analysis(objective="Reuse accepted semantics.", strategy="Use exact selections.")
    decision = target_analyst.select_semantic_scope(no_reuse_reason="The accepted ontology is not relevant to this requirement.")
    assert decision["decision"] == "no_reuse"
    target_analyst.begin_analysis(objective="Reuse accepted semantics.", strategy="Use exact selections.")


def test_requirement_legacy_transition_helpers_fail_before_any_target_or_journal_bytes(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    context = RunContext("RUN-REQ-LEGACY", tmp_path / "run", (archive.parent,), core_version="0.1", skill_version="0.1")
    lifecycle = RunLifecycle.create(context, ("REQ-01", "REQ-02"), mode="requirement")
    source = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="source")
    target = ItemWorkspace.create(context, "REQ-02", mode="requirement", original_text="target")
    bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(archive),
        source,
        lifecycle,
    )
    before = _run_bytes(context)

    with pytest.raises(ValueError, match="normal Requirement Mode"):
        BoundAnalysisContext.create_from_transitioned_catalog(context, target, bound, lifecycle)
    assert _run_bytes(context) == before
