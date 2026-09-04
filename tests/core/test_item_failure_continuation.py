"""Focused regressions for item-local recovery exhaustion routing."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from auto_foundry_core import (
    EntityResolutionWorkspace,
    ItemWorkspace,
    PlannerAction,
    RequirementRecord,
    RequirementSupervisorWorkspace,
    RoleExecution,
    RunContext,
    RunCoordinator,
    RunLifecycle,
)
from auto_foundry_core.coordinator import MAX_RUN_RETRIES_PER_ACTION, CoordinatorRunSpec


def _spec(run_id: str) -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://item-failure-continuation",
        hashlib.sha256(b"item-failure-continuation-planner").hexdigest(),
    )


def _run_with_items(tmp_path: Path, *item_ids: str) -> tuple[RunContext, list[ItemWorkspace]]:
    context = RunContext("RUN-ITEM-FAILURE", tmp_path / "run")
    RunLifecycle.create(context, item_ids, mode="requirement")
    workspaces = [
        ItemWorkspace.create(context, item_id, mode="requirement", original_text=f"Requirement {item_id}")
        for item_id in item_ids
    ]
    RequirementSupervisorWorkspace(context).plan_requirements(
        [RequirementRecord(item_id, f"Requirement {item_id}") for item_id in item_ids]
    )
    return context, workspaces


def test_multi_item_coordinator_exhaustion_terminalizes_req1_and_dispatches_req2_without_reopen(
    tmp_path: Path,
) -> None:
    """A failed first item is settled and the next item is offered in one run call."""

    context, _ = _run_with_items(tmp_path, "REQ-1", "REQ-2")
    calls: list[str] = []

    class Planner:
        def next_actions(self, current_context: RunContext, _state: dict) -> tuple[PlannerAction, ...]:
            failed = ItemWorkspace.load(current_context, "REQ-1", mode="requirement").state[
                "lifecycle_state"
            ] == "technical_failure"
            item_id = "REQ-2" if failed else "REQ-1"
            return (PlannerAction("analyze_requirement", "analytical_owner", item_id, "test transport"),)

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={
            "analyze_requirement": lambda action, **_: calls.append(action.subject_id)
            or RoleExecution(output="unchanged"),
        },
    )
    try:
        coordinator.start(_spec(context.run_id))
        status = coordinator.run(max_steps=MAX_RUN_RETRIES_PER_ACTION + 1)
    finally:
        coordinator.close(wait_for_roles=True)

    assert status.status == "waiting"
    assert calls[:MAX_RUN_RETRIES_PER_ACTION] == ["REQ-1"] * MAX_RUN_RETRIES_PER_ACTION
    assert "REQ-2" in calls[MAX_RUN_RETRIES_PER_ACTION:]
    assert ItemWorkspace.load(context, "REQ-1", mode="requirement").state["lifecycle_state"] == "technical_failure"
    events = [
        json.loads(line)
        for line in (context.run_root / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    ]
    assert sum(event.get("event") == "requirement_terminalized" for event in events) == 1
    assert not any(event.get("event") == "run_reopened" for event in events)


def test_requirement_retry_budget_survives_unrelated_item_progress_between_completions(
    tmp_path: Path,
) -> None:
    """Unrelated requirement progress must not reset A's unchanged retry count."""

    context, workspaces = _run_with_items(tmp_path, "REQ-A", "REQ-B")
    action_a = PlannerAction(
        "analyze_requirement",
        "analytical_owner",
        "REQ-A",
        "unchanged A transport",
    )

    class Planner:
        def next_actions(
            self,
            current_context: RunContext,
            _state: dict,
        ) -> tuple[PlannerAction, ...]:
            # Once A is terminalized, the focused reconciliation has no work
            # left to admit. Before that point, keep offering the unchanged A
            # action whose retry evidence was seeded below.
            if ItemWorkspace.load(current_context, "REQ-A", mode="requirement").state[
                "lifecycle_state"
            ] == "technical_failure":
                return ()
            return (action_a,)

    calls: list[str] = []

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={
            "analyze_requirement": lambda action, **_: calls.append(action.subject_id)
            or RoleExecution(output="unexpected dispatch"),
        },
    )
    try:
        coordinator.start(_spec(context.run_id))
        # Materialize the execution-state projection before binding the retry
        # fingerprint; the first Planner read performs this one-time migration
        # for freshly created item workspaces.
        for item_id in ("REQ-A", "REQ-B"):
            ItemWorkspace.load(context, item_id, mode="requirement")
        # Persist A's exhausted retry evidence at its current item boundary,
        # then advance only unrelated B. A requirement-local fingerprint must
        # remain unchanged even though the global phase snapshot changes.
        with coordinator._locked(create=False):  # noqa: SLF001 - test boundary
            state, _ = coordinator._read_replay()  # noqa: SLF001
            assert state is not None
            state_fingerprint = coordinator._authoritative_state_fingerprint(  # noqa: SLF001
                state,
                action_a,
            )
            coordinator._record_retry_locked(  # noqa: SLF001
                state,
                action_a,
                RoleExecution(output="unchanged"),
                state_fingerprint,
            )
            coordinator._record_retry_locked(  # noqa: SLF001
                state,
                action_a,
                RoleExecution(output="unchanged"),
                state_fingerprint,
            )
            coordinator._append_event_locked(  # noqa: SLF001
                state,
                "retry_seed",
                {"action": action_a.to_dict(), "reason": "focused_interleaving_fixture"},
            )
        workspaces[1].technical_failure("unrelated B settled", recovery_exhausted=True)
        status = coordinator._refresh_and_launch(set())  # noqa: SLF001
    finally:
        coordinator.close(wait_for_roles=True)

    assert status.status in {"complete_with_limits", "waiting"}
    assert calls == []
    assert ItemWorkspace.load(context, "REQ-A", mode="requirement").state["lifecycle_state"] == "technical_failure"
    assert ItemWorkspace.load(context, "REQ-B", mode="requirement").state["lifecycle_state"] == "technical_failure"
    state = json.loads((context.run_root / "control_plane/coordinator_state.json").read_text())
    assert any(
        value.get("count") == MAX_RUN_RETRIES_PER_ACTION
        for value in state.get("retry_blocked", {}).values()
        if isinstance(value, dict)
    )


def test_no_progress_retry_is_checkpointed_with_unrelated_dispatch_active(tmp_path: Path) -> None:
    """A retry recorded beside an active sibling survives a state reload."""

    context, _ = _run_with_items(tmp_path, "REQ-A", "REQ-B")
    release_b = threading.Event()
    sentinel = "SECRET-ROLE-TRANSPORT-SENTINEL"
    action_a = PlannerAction(
        "analyze_requirement",
        "analytical_owner",
        "REQ-A",
        "unchanged A transport",
    )
    action_b = PlannerAction(
        "specialist",
        "specialist",
        "REQ-B",
        "unrelated active transport",
    )

    class Planner:
        def next_actions(
            self,
            _current_context: RunContext,
            _state: dict,
        ) -> tuple[PlannerAction, ...]:
            return (action_a, action_b)

    calls: list[str] = []

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action.subject_id)
        if action.subject_id == "REQ-B":
            # Keep B active while A's first unchanged completion is
            # reconciled, forcing the retry checkpoint through the active
            # branch of ``_refresh_and_launch``.
            release_b.wait(5)
        return RoleExecution(output=sentinel, error=sentinel)

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={"analyze_requirement": role, "specialist": role},
    )
    try:
        coordinator.start(_spec(context.run_id))
        status = coordinator.step()
        assert status.status == "dispatching"
        state = json.loads((context.run_root / "control_plane/coordinator_state.json").read_text())
        assert any(
            int(value) == 1
            for value in state.get("retry_counts", {}).values()
        )
        assert any(event.get("event") == "retry_recorded" for event in (
            json.loads(line)
            for line in (context.run_root / "control_plane/coordinator_events.jsonl").read_text().splitlines()
        ))
        coordinator_documents = "\n".join(
            path.read_text()
            for path in (
                context.run_root / "control_plane/coordinator_events.jsonl",
                context.run_root / "control_plane/coordinator_state.json",
            )
        )
        assert sentinel not in coordinator_documents
    finally:
        release_b.set()
        coordinator.close(wait_for_roles=True)

    assert calls[:2] == ["REQ-A", "REQ-B"]


def test_legacy_raw_transport_is_scrubbed_before_next_coordinator_checkpoint(tmp_path: Path) -> None:
    """A resumed coordinator does not copy legacy role text into new state."""

    context, _ = _run_with_items(tmp_path, "REQ-A")
    coordinator = RunCoordinator(context, planner_provider=lambda *_: ())
    sentinel = "LEGACY-ROLE-TRANSPORT-SENTINEL"
    try:
        coordinator.start(_spec(context.run_id))
        # Seed an old, pre-projection state in this temporary fixture.  Dropping
        # the initial event keeps the fixture's state-only replay path valid;
        # the production event chain still validates every historical event
        # before projecting its state.
        coordinator.events_path.unlink()
        legacy_state = json.loads(coordinator.state_path.read_text())
        legacy_state.update({"last_event_seq": 0, "last_event_hash": ""})
        legacy_state["last_action"] = {
            "action": "metadata_fixture",
            "subject_id": "REQ-A",
            "metadata": {"transport": {"output": "ACTION-METADATA-MUST-REMAIN"}},
        }
        legacy_state["diagnostics"] = [
            {
                "kind": "role_transport_failure",
                "action": {
                    "action": "analyze_requirement",
                    "metadata": {
                        "transport": {
                            "output": "ACTION-DIAGNOSTIC-METADATA-MUST-REMAIN",
                            "mode": "record",
                        }
                    },
                },
                "transport": {
                    "exit_code": 1,
                    "output": sentinel,
                    "error": sentinel,
                    "timed_out": False,
                    "session_id": "legacy-session",
                },
            }
        ]
        coordinator.state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

        status = coordinator.status()
        assert sentinel not in json.dumps(status.to_dict())
        with coordinator._locked(create=False):  # noqa: SLF001 - test boundary
            state, _ = coordinator._read_replay()  # noqa: SLF001
            assert state is not None
            assert sentinel not in json.dumps(state)
            state["status"] = "waiting"
            state["phase"] = "waiting"
            coordinator._append_event_locked(  # noqa: SLF001
                state,
                "legacy_transport_scrubbed",
                {"reason": "legacy_diagnostic_projection"},
            )
        persisted = coordinator.state_path.read_text()
        events = coordinator.events_path.read_text()
        assert sentinel not in persisted
        assert sentinel not in events
        persisted_state = json.loads(persisted)
        assert (
            persisted_state["last_action"]["metadata"]["transport"]["output"]
            == "ACTION-METADATA-MUST-REMAIN"
        )
        diagnostic = persisted_state["diagnostics"][0]
        assert diagnostic["action"]["metadata"]["transport"] == {
            "output": "ACTION-DIAGNOSTIC-METADATA-MUST-REMAIN",
            "mode": "record",
        }
        # Scrubbing is a projection change, not an event-chain rewrite.
        coordinator._read_replay()  # noqa: SLF001 - hash-chain assertion
    finally:
        coordinator.close(wait_for_roles=True)


def test_identity_retry_budget_survives_unrelated_item_progress(tmp_path: Path) -> None:
    """A domain retry remains bounded when another requirement advances."""

    context, workspaces = _run_with_items(tmp_path, "REQ-A", "REQ-B")
    requester = workspaces[0]
    requester.bind_analysis_owner("ao-REQ-A")
    requester.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": "customer-domain",
            "object_type": "customer",
            "rationale": "identity is required",
            "source_hints": [],
            "representation_item_ids": [],
            "item_id": "REQ-A",
            "owner_ref": "ao-REQ-A",
        }
    )
    EntityResolutionWorkspace.create(context).reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-A",
        "identity is required",
        request_owner_ref="ao-REQ-A",
    )
    action = PlannerAction(
        "resolve_identity",
        "entity_resolution_owner",
        "customer-domain",
        "unchanged identity transport",
    )

    class Planner:
        def next_actions(
            self,
            current_context: RunContext,
            _state: dict,
        ) -> tuple[PlannerAction, ...]:
            if ItemWorkspace.load(current_context, "REQ-A", mode="requirement").state[
                "lifecycle_state"
            ] == "technical_failure":
                return ()
            return (action,)

    calls: list[str] = []
    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={
            "resolve_identity": lambda current, **_: calls.append(current.subject_id)
            or RoleExecution(output="unexpected dispatch"),
        },
    )
    try:
        coordinator.start(_spec(context.run_id))
        for item_id in ("REQ-A", "REQ-B"):
            ItemWorkspace.load(context, item_id, mode="requirement")
        with coordinator._locked(create=False):  # noqa: SLF001 - test boundary
            state, _ = coordinator._read_replay()  # noqa: SLF001
            assert state is not None
            state_fingerprint = coordinator._authoritative_state_fingerprint(  # noqa: SLF001
                state,
                action,
            )
            coordinator._record_retry_locked(  # noqa: SLF001
                state,
                action,
                RoleExecution(output="unchanged"),
                state_fingerprint,
            )
            coordinator._record_retry_locked(  # noqa: SLF001
                state,
                action,
                RoleExecution(output="unchanged"),
                state_fingerprint,
            )
            coordinator._append_event_locked(  # noqa: SLF001
                state,
                "retry_seed",
                {"action": action.to_dict(), "reason": "focused_identity_interleaving_fixture"},
            )
        workspaces[1].technical_failure("unrelated B settled", recovery_exhausted=True)
        status = coordinator._refresh_and_launch(set())  # noqa: SLF001
    finally:
        coordinator.close(wait_for_roles=True)

    assert status.status in {"complete_with_limits", "waiting"}
    assert calls == []
    assert ItemWorkspace.load(context, "REQ-A", mode="requirement").state["lifecycle_state"] == "technical_failure"
    assert ItemWorkspace.load(context, "REQ-B", mode="requirement").state["lifecycle_state"] == "technical_failure"


def test_executable_entity_action_exhaustion_terminalizes_requester_and_dispatches_unrelated_requirement(
    tmp_path: Path,
) -> None:
    """An exhausted identity-domain action settles its requester only."""

    context, workspaces = _run_with_items(tmp_path, "REQ-1", "REQ-2")
    requester = workspaces[0]
    requester.bind_analysis_owner("ao-REQ-1")
    requester.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": "customer-domain",
            "object_type": "customer",
            "rationale": "identity is required",
            "source_hints": [],
            "representation_item_ids": [],
            "item_id": "REQ-1",
            "owner_ref": "ao-REQ-1",
        }
    )
    EntityResolutionWorkspace.create(context).reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity is required",
        request_owner_ref="ao-REQ-1",
    )
    calls: list[tuple[str, str]] = []

    class Planner:
        def next_actions(self, current_context: RunContext, _state: dict) -> tuple[PlannerAction, ...]:
            failed = ItemWorkspace.load(current_context, "REQ-1", mode="requirement").state[
                "lifecycle_state"
            ] == "technical_failure"
            if failed:
                return (PlannerAction("analyze_requirement", "analytical_owner", "REQ-2", "unrelated"),)
            return (
                PlannerAction(
                    "resolve_identity",
                    "entity_resolution_owner",
                    "customer-domain",
                    "identity transport",
                ),
            )

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={
            "resolve_identity": lambda action, **_: calls.append((action.action, action.subject_id))
            or RoleExecution(output="unchanged"),
            "analyze_requirement": lambda action, **_: calls.append((action.action, action.subject_id))
            or RoleExecution(output="unchanged"),
        },
    )
    try:
        coordinator.start(_spec(context.run_id))
        status = coordinator.run(max_steps=MAX_RUN_RETRIES_PER_ACTION + 1)
    finally:
        coordinator.close(wait_for_roles=True)

    assert status.status == "waiting"
    assert calls[:MAX_RUN_RETRIES_PER_ACTION] == [
        ("resolve_identity", "customer-domain")
    ] * MAX_RUN_RETRIES_PER_ACTION
    assert ("analyze_requirement", "REQ-2") in calls[MAX_RUN_RETRIES_PER_ACTION:]
    assert ItemWorkspace.load(context, "REQ-1", mode="requirement").state["lifecycle_state"] == "technical_failure"
    assert ItemWorkspace.load(context, "REQ-2", mode="requirement").state["lifecycle_state"] == "work"


def test_unbound_global_identity_exhaustion_stays_run_level_and_does_not_terminalize_item(
    tmp_path: Path,
) -> None:
    """A domain with only foreign requesters remains a fail-closed run concern."""

    context, workspaces = _run_with_items(tmp_path, "REQ-1")
    requester = workspaces[0]
    requester.bind_analysis_owner("ao-REQ-1")
    requester.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": "foreign-domain",
            "object_type": "customer",
            "rationale": "foreign identity",
            "source_hints": [],
            "representation_item_ids": [],
            "item_id": "REQ-1",
            "owner_ref": "ao-REQ-1",
        }
    )
    EntityResolutionWorkspace.create(context).reserve_identity_domain(
        "foreign-domain",
        "customer",
        "REQ-1",
        "foreign identity",
        request_owner_ref="ao-REQ-1",
    )
    # Rewrite the authoritative domain requester binding to a foreign item.
    # This models a global/foreign integrity row: there is no active local
    # requirement whose claim may be released by item-local routing.
    resolution = EntityResolutionWorkspace.load(context)
    with resolution._locked():  # noqa: SLF001 - test fixture boundary
        resolution._refresh()  # noqa: SLF001
        domain = resolution._state["domains"]["foreign-domain"]  # noqa: SLF001
        domain["requested_by"] = ["FOREIGN-REQ"]
        domain["discovered_by_item_id"] = "FOREIGN-REQ"
        domain["requests"][0]["item_id"] = "FOREIGN-REQ"
        resolution._state["domains"]["foreign-domain"] = domain  # noqa: SLF001
        resolution._persist()  # noqa: SLF001

    class Planner:
        def next_actions(self, _context: RunContext, _state: dict) -> tuple[PlannerAction, ...]:
            return (
                PlannerAction(
                    "resolve_identity",
                    "entity_resolution_owner",
                    "foreign-domain",
                    "global integrity transport",
                ),
            )

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={"resolve_identity": lambda action, **_: RoleExecution(output="unchanged")},
    )
    try:
        coordinator.start(_spec(context.run_id))
        status = coordinator.run(max_steps=MAX_RUN_RETRIES_PER_ACTION + 1)
    finally:
        coordinator.close(wait_for_roles=True)

    assert status.status == "waiting"
    assert ItemWorkspace.load(context, "REQ-1", mode="requirement").state["lifecycle_state"] == "work"
    state = json.loads((context.run_root / "control_plane/coordinator_state.json").read_text())
    assert any(
        value.get("count") == MAX_RUN_RETRIES_PER_ACTION
        for value in state.get("retry_blocked", {}).values()
        if isinstance(value, dict)
    )
    events = [
        json.loads(line)
        for line in (context.run_root / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    ]
    assert not any(event.get("event") == "requirement_terminalized" for event in events)
