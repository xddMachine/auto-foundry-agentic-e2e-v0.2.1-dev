from __future__ import annotations

from pathlib import Path

import pytest

from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.requirement_planning import (
    PlannerAction,
    inspect_integration_fidelity,
    validate_action_role,
)
from auto_foundry_core.workspace import RunContext


def _accepted_item(context: RunContext, item_id: str = "Q-001") -> ItemWorkspace:
    RunLifecycle.create(context, (item_id,), mode="question")
    item = ItemWorkspace.create(context, item_id, mode="question", original_text="bounded question")
    item.write_plan({"item_id": item_id, "offline": True})
    item.write_draft({"answer": "reviewed"})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    return item


def test_empty_integration_session_is_staging_incomplete_and_never_reviewed(tmp_path: Path) -> None:
    context = RunContext("RUN-WAVE1-EMPTY", tmp_path / "run")
    item = _accepted_item(context)
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-001",
    )

    validation = session.validate()
    assert not validation.valid
    assert validation.omissions == ("staging_incomplete",)
    with pytest.raises(ValueError, match="staging_incomplete"):
        session.build_fidelity_packet()


def test_partial_staging_returns_same_session_positive_handoff(tmp_path: Path) -> None:
    context = RunContext("RUN-WAVE1-PARTIAL", tmp_path / "run")
    item = _accepted_item(context)
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    session_path = session.staging_root / "session.json"
    (session.staging_root / "snapshot.json").unlink()

    view = inspect_integration_fidelity(context, item.item_id)
    assert view["valid"] is False
    assert view["stage"] == "staging_incomplete"
    handoff = view["handoff"]
    assert handoff["kind"] == "staging_incomplete"
    assert handoff["action"] == "integrate_requirement"
    assert handoff["role"] == "integration_agent"
    assert handoff["continuation"] == "same_session"
    assert handoff["session_id"] == session.session_id
    assert handoff["owner_id"] == "integration-owner"
    assert handoff["invocation_id"] == "inv-Q-001"
    assert session_path.is_file()


def test_explicit_limitation_record_makes_genuine_noop_reviewable(tmp_path: Path) -> None:
    context = RunContext("RUN-WAVE1-NOOP", tmp_path / "run")
    item = _accepted_item(context)
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    limitation_id = session.add_limitation(
        {"limitation": "No semantic change was required for this item."},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    assert session.validate().valid
    packet = session.build_fidelity_packet()
    assert [record["record_id"] for record in packet.records] == [limitation_id]
    assert packet.records[0]["kind"] == "limitation"


def test_action_role_contract_rejects_cross_role_dispatch() -> None:
    with pytest.raises(ValueError, match="may target role"):
        PlannerAction(
            "integrate_requirement",
            "planner",
            "Q-001",
            "wrong owner",
        )
    contract = validate_action_role("repair_integration_fidelity", "integration_agent")
    assert contract.to_dict() == {
        "action": "repair_integration_fidelity",
        "role": "integration_agent",
    }


def test_lifecycle_reconciliation_requires_committed_boundary_and_rejects_failure(tmp_path: Path) -> None:
    context = RunContext("RUN-WAVE1-LIFECYCLE", tmp_path / "run")
    lifecycle = RunLifecycle.create(context, ("Q-001",))
    accepted = {
        "item_id": "Q-001",
        "lifecycle_state": "accepted",
        "terminal_outcome": {"outcome": "accepted"},
        "integration_state": "integrated",
    }
    assert lifecycle.reconcile((accepted,)).state == "analytical_complete"

    lifecycle = RunLifecycle.load(context)
    accepted["committed_integration_validation"] = {
        "valid": True,
        "stage": "committed",
    }
    assert lifecycle.reconcile((accepted,)).state == "integration_complete"

    lifecycle = RunLifecycle.create(RunContext("RUN-WAVE1-FAIL", tmp_path / "failure"), ("Q-001",))
    failed = {
        "item_id": "Q-001",
        "lifecycle_state": "accepted",
        "terminal_outcome": {"outcome": "accepted"},
        "integration_state": "technical_failure",
    }
    assert lifecycle.reconcile((failed,)).state == "analytical_complete"
