"""Requirement-generation history replay for the durable LEM projector."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from auto_foundry_core.contracts import KnowledgeDelta, LEMRef, OntologyItem
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.enterprise_model import LivingEnterpriseModel
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.lem_projection import LivingEnterpriseModelProjector
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _accepted(context: RunContext, item_id: str, text: str | None = None) -> ItemWorkspace:
    item = ItemWorkspace.create(
        context,
        item_id,
        mode="requirement",
        original_text=text or item_id,
    )
    item.write_plan({"item_id": item_id})
    item.write_draft({"answer": item_id})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    return item


def _commit_ontology(context: RunContext, item: ItemWorkspace, item_id: str) -> dict[str, object]:
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id=f"inv-{item.item_id}-{item_id}",
    )
    session.add_ontology_item(
        OntologyItem(item_id=item_id, item_type="entity", label=item_id),
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    return dict(session.commit())


def _archive(context: RunContext, item_id: str, generation_id: str = "G-0002") -> Path:
    source = context.run_root / "requirements" / item_id
    history = context.run_root / "history" / "requirements" / item_id / generation_id
    history.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, history)
    return history


def _commit_successor(context: RunContext, item: ItemWorkspace) -> dict[str, object]:
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-successor",
    )
    assert set(session.lem.ontology) == {"old-q1", "old-q2"}
    session.add_knowledge_delta(
        KnowledgeDelta(
            "q1-successor",
            "add_ontology_item",
            {"item_id": "new-q1", "item_type": "entity", "label": "new-q1"},
            evidence_refs=("work/plan.json",),
            supersedes=(
                LEMRef("ontology", "old-q1"),
                LEMRef("ontology", "old-q2"),
            ),
            accepted=True,
        ),
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    return dict(session.commit())


def test_generation_history_replays_before_successor_and_orders_by_commit_time(tmp_path: Path) -> None:
    context = RunContext("RUN-HISTORY", tmp_path / "run")
    RunLifecycle.create(context, ("Q1", "Q2"), mode="requirement")

    # Commit the lexical-later item first; chronology must still make it a
    # predecessor when Q1's refreshed head publishes its successor.
    q1 = _accepted(context, "Q1")
    q2 = _accepted(context, "Q2")
    q2_manifest = _commit_ontology(context, q2, "old-q2")
    q1_manifest = _commit_ontology(context, q1, "old-q1")
    history = _archive(context, "Q1")
    assert (history / "integration/committed/manifest.json").is_file()

    refreshed = _accepted(context, "Q1", "refreshed Q1")
    before = LivingEnterpriseModelProjector.project(context, before_item_id="Q1")
    assert set(before.model.ontology) == {"old-q1", "old-q2"}
    assert "new-q1" not in before.model.ontology

    successor_manifest = _commit_successor(context, refreshed)
    projection = LivingEnterpriseModelProjector.project(context, include_item_id="Q1")
    repeated = LivingEnterpriseModelProjector.project(context, include_item_id="Q1")
    assert projection.projection_hash == repeated.projection_hash
    assert projection.model.export() == repeated.model.export()
    assert projection.model.current_ontology.keys() == {"new-q1"}
    assert set(projection.model.ontology) == {"old-q1", "old-q2", "new-q1"}
    assert projection.model.ontology["old-q1"].status == "superseded"
    assert projection.model.ontology["old-q2"].status == "superseded"
    assert projection.model.export()["ontology_index"] == [
        {
            "item_id": "new-q1",
            "item_type": "entity",
            "label": "new-q1",
            "scope": None,
            "status": "active",
        }
    ]
    manifest_ids = [binding.manifest_hash for binding in projection.bindings]
    assert manifest_ids == [
        q2_manifest["manifest_hash"],
        q1_manifest["manifest_hash"],
        successor_manifest["manifest_hash"],
    ]


@pytest.mark.parametrize("tamper", ["missing_state", "symlink", "malformed_generation"])
def test_generation_history_path_and_content_tampering_fails_closed(tmp_path: Path, tamper: str) -> None:
    context = RunContext("RUN-HISTORY-TAMPER", tmp_path / "run")
    RunLifecycle.create(context, ("Q1", "Q2"), mode="requirement")
    q1 = _accepted(context, "Q1")
    _commit_ontology(context, q1, "old-q1")
    history = _archive(context, "Q1")
    if tamper == "missing_state":
        (history / "item_state.json").unlink()
    elif tamper == "symlink":
        state_target = history / "item_state.json"
        state_target.unlink()
        os.symlink(history / "accepted", state_target)
    else:
        (history.parent / "G-0004-extra").mkdir()

    with pytest.raises((AllowedRootError, ValueError), match="historical|item_state|symlink"):
        LivingEnterpriseModelProjector.project(context, before_item_id="Q1")


def test_generation_history_records_hash_and_uncommitted_histories_are_safe(tmp_path: Path) -> None:
    context = RunContext("RUN-HISTORY-RECORDS", tmp_path / "run")
    RunLifecycle.create(context, ("Q1", "Q2", "Q3"), mode="requirement")
    q1 = _accepted(context, "Q1")
    _commit_ontology(context, q1, "old-q1")
    history = _archive(context, "Q1")
    records_path = history / "integration/committed/records.jsonl"
    record = json.loads(records_path.read_text(encoding="utf-8").splitlines()[0])
    record["payload"]["label"] = "tampered"
    records_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        LivingEnterpriseModelProjector.project(context, before_item_id="Q1")

    # A valid archived item with no integration is ignored; no bytes from its
    # uncommitted head enter the LEM.
    context2 = RunContext("RUN-HISTORY-SKIP", tmp_path / "run-skip")
    RunLifecycle.create(context2, ("Q2",), mode="requirement")
    pending = _accepted(context2, "Q2")
    pending_history = _archive(context2, "Q2")
    projection = LivingEnterpriseModelProjector.project(context2)
    assert projection.model.export() == LivingEnterpriseModel(run_id=context2.run_id).export()
    assert pending_history.is_dir()

    # Terminal technical-failure history is validated as a non-semantic
    # version and contributes no committed bytes either.
    context3 = RunContext("RUN-HISTORY-FAILURE", tmp_path / "run-failure")
    RunLifecycle.create(context3, ("Q3",), mode="requirement")
    failed = ItemWorkspace.create(context3, "Q3", mode="requirement", original_text="Q3")
    failed.technical_failure("fixture failure", recovery_exhausted=True)
    failure_history = _archive(context3, "Q3")
    projection3 = LivingEnterpriseModelProjector.project(context3)
    assert projection3.model.export() == LivingEnterpriseModel(run_id=context3.run_id).export()
    assert failure_history.is_dir()


def test_sparse_history_and_removed_item_records_remain_replayable(tmp_path: Path) -> None:
    context = RunContext("RUN-HISTORY-SPARSE", tmp_path / "run")
    RunLifecycle.create(context, ("Q1", "Q2"), mode="requirement")
    q1 = _accepted(context, "Q1")
    q2 = _accepted(context, "Q2")
    _commit_ontology(context, q1, "old-q1")
    _commit_ontology(context, q2, "old-q2")

    # Q2 archives in G-0002; Q1's first archive is naturally sparse at G-0003
    # because another item already consumed the earlier generation.
    _archive(context, "Q2", "G-0002")
    _archive(context, "Q1", "G-0003")
    _accepted(context, "Q1", "refreshed Q1")
    _accepted(context, "Q2", "refreshed Q2")
    before = LivingEnterpriseModelProjector.project(context, before_item_id="Q1")
    assert set(before.model.ontology) == {"old-q1", "old-q2"}

    # A removed requirement is no longer in the lifecycle frontier, but its
    # archived committed records remain durable semantic authority.
    lifecycle = RunLifecycle.load(context)
    state = lifecycle.to_dict()
    state["item_ids"] = ["Q2"]
    lifecycle._write_state(state)  # noqa: SLF001 - compact historical fixture
    removed_projection = LivingEnterpriseModelProjector.project(context)
    assert set(removed_projection.model.ontology) == {"old-q1", "old-q2"}
