"""Cold, deterministic acceptance checks for the current Auto Foundry seams.

These tests deliberately use only local public APIs and deterministic fixtures.
They are not a model or product-transport replay: only the semantic intake
boundary uses a tiny deterministic decomposer, while the coordinator and role
adapter dispatch paths are exercised against a local fake Codex process.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from pathlib import Path
import zipfile

import pytest

import auto_foundry_core.coordinator as coordinator_module
from auto_foundry_core import (
    CodexExecConfig,
    CodexRoleAdapter,
    CoordinatorRunSpec,
    EntityResolutionResult,
    EntityResolutionWorkspace,
    IntegrationSession,
    ItemWorkspace,
    MetricDefinition,
    OntologyItem,
    PlannerAction,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementRunExtension,
    RequirementSupervisorWorkspace,
    RunCoordinator,
    RunContext,
    RunLifecycle,
    compute_kpi_table,
    profile_data,
)
from auto_foundry_core.coordinator import RoleSessionRegistry
from auto_foundry_core.mission_context import ContextItem, MissionContext, MissionPlan, SourceBinding
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.data_revisions import DataRevisionStore


_REQUIREMENT_BLOCK = re.compile(
    r"(?ims)^\s*requirement\s+(?P<number>\d+)\s*(?:[-:]\s*)?(?P<body>.*?)(?=^\s*requirement\s+\d+\b|\Z)"
)


def _record(requirement_id: str, text: str, *, source_ref: str = "brief.txt") -> RequirementRecord:
    """Build one typed record from the fake decomposer's plain-language text."""

    return RequirementRecord(
        requirement_id=requirement_id,
        original_text=text,
        business_objective=f"Support the decision described by {requirement_id}.",
        expected_analytical_outputs=(f"evidence-{requirement_id}",),
        expected_visual_outputs=(f"view-{requirement_id}",),
        data_needs=("orders.csv",),
        ontology_needs=("customer",),
        limitations=("deterministic cold fixture",),
        source_refs=(source_ref,),
        status="queued",
    )


def _decompose_free_form(brief: str) -> tuple[RequirementRecord, ...]:
    """A deterministic stand-in for the semantic intake planner.

    It accepts ordinary prose around the headings and normalises whitespace;
    the production planner may use an LLM, but the durable contract receives
    exactly the same typed records.  The regex is only a deterministic fake
    planner boundary for this cold acceptance test, not production
    programmatic requirement splitting.
    """

    matches = tuple(_REQUIREMENT_BLOCK.finditer(brief))
    assert len(matches) == 9, "the cold brief must contain nine requirement blocks"
    records: list[RequirementRecord] = []
    for match in matches:
        number = int(match.group("number"))
        body = " ".join(match.group("body").split())
        records.append(_record(f"REQ-{number:03d}", body))
    return tuple(records)


def _plan(records: tuple[RequirementRecord, ...], *, planner_ref: str = "cold-fake-planner") -> RequirementExecutionPlan:
    return RequirementExecutionPlan(
        input_records=records,
        groups=tuple(
            RequirementExecutionGroup((record.requirement_id,), f"Execute {record.requirement_id} independently.")
            for record in records
        ),
        planner_ref=planner_ref,
        portfolio_strategy="preserve semantic intake order and reuse shared identity work",
        revision=1,
    )


def _append_identity_proposal(context: RunContext, item_id: str, domain_id: str) -> None:
    item = ItemWorkspace.load(context, item_id, mode="requirement")
    owner_ref = f"ao-{item_id}"
    item.bind_analysis_owner(owner_ref)
    item.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": domain_id,
            "object_type": "customer",
            "rationale": "shared customer identity is needed by the requirement",
            "source_hints": ["customers.csv"],
            "representation_item_ids": ["customer-source"],
            "item_id": item_id,
            "owner_ref": owner_ref,
        }
    )


def _publish_identity_without_mapping(resolution: EntityResolutionWorkspace, domain_id: str) -> None:
    resolution.claim_resolution_owner(domain_id, "resolution-owner")
    result = EntityResolutionResult(
        coverage={"source_count": 1, "mapped_count": 0},
        population={"source_count": 1},
        unresolved=({"reason": "no deterministic identity match", "row_count": 1},),
        evidence_refs=("work/plan.json",),
        source_hash="a" * 64,
        metadata={"resolution_outcome": "no_mapping_found"},
    )
    resolution.submit_result(
        domain_id,
        "resolution-owner",
        result,
        expected_scope_hash=resolution.current_scope(domain_id).scope_hash,
    )
    resolution.record_review(domain_id, "accept", "independent-reviewer")
    resolution.commit(domain_id)


def _archive(path: Path, value: str) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("orders.csv", f"id,value\n1,{value}\n")
    return path


def _accept_and_integrate(context: RunContext, item: ItemWorkspace) -> None:
    item.write_plan({"item_id": item.item_id, "offline": True})
    item.write_draft({"answer": item.item_id})
    item.record_review("accept", reviewer_ref="business-reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id=f"inv-{item.item_id}",
    )
    session.add_ontology_item(
        OntologyItem(item_id=f"ontology-{item.item_id}", item_type="entity", label=item.item_id),
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    session.commit()


def test_free_form_intake_materializes_nine_typed_requirements() -> None:
    brief = """
    The leadership team needs an evidence-backed operating view.
    Requirement 1 — profile the customer base and call out missing values.
    Requirement 2 — calculate revenue and margin KPIs by country.
    Requirement 3 — segment customers with a reproducible clustering method.
    Requirement 4 — inspect receivables ageing and collection exposure.
    Requirement 5 — connect supplier performance to inventory availability.
    Requirement 6 — compare ecommerce and retail channel performance.
    Requirement 7 — identify high-value occasion and gift cohorts.
    Requirement 8 — surface cross-domain dependency chains and risks.
    Requirement 9 — package decision-ready dashboard views with caveats.
    """
    binding = SourceBinding("brief.txt", text=brief)
    context = MissionContext(
        "hybrid",
        source_context=(ContextItem("Leadership supplied a free-form business brief.", (binding,)),),
        metadata={"input_kind": "free_form", "document_count": 1},
    )
    records = _decompose_free_form(brief)
    plan = MissionPlan(
        context,
        requirement_ids=tuple(record.requirement_id for record in records),
        planner_ref="cold-fake-planner",
    )
    execution_plan = RequirementExecutionPlan.from_requirements(records)

    assert len(records) == len(plan.requirement_ids) == 9
    assert plan.requirement_ids == tuple(f"REQ-{number:03d}" for number in range(1, 10))
    assert execution_plan.input_records == records
    assert set(execution_plan.execution_order) == set(plan.requirement_ids)
    assert all(record.source_refs == ("brief.txt",) for record in records)
    assert plan.mission_context.context_hash == context.context_hash


def test_shared_identity_is_reused_and_successor_data_revision_continues(tmp_path: Path) -> None:
    # Two requirements request the same domain.  The Planner offers one
    # resolver action with both requesters, then removes that action once the
    # committed identity result is available.
    context = RunContext("RUN-COLD-SHARED-IDENTITY", tmp_path / "identity-run", (tmp_path,))
    records = (
        _record("REQ-001", "Profile customer value."),
        _record("REQ-002", "Compare customer value by channel."),
    )
    RunLifecycle.create(context, tuple(record.requirement_id for record in records), mode="requirement")
    for record in records:
        ItemWorkspace.create(context, record.requirement_id, mode="requirement", original_text=record.original_text)
        _append_identity_proposal(context, record.requirement_id, "shared-customer")
    planner = RequirementSupervisorWorkspace(context)
    planner.plan_requirements(records)

    offered = planner.next_actions()
    resolver_actions = [action for action in offered if action.action == "resolve_identity"]
    assert len(resolver_actions) == 1
    assert resolver_actions[0].subject_id == "shared-customer"
    resolution = EntityResolutionWorkspace.load(context)
    assert resolution.get_domain("shared-customer").requested_by == ("REQ-001", "REQ-002")

    _publish_identity_without_mapping(resolution, "shared-customer")
    resumed = planner.next_actions()
    assert not any(action.action == "resolve_identity" for action in resumed)
    assert {action.subject_id for action in resumed if action.action == "analyze_requirement"} == {"REQ-001"}
    # The one shared resolution is reusable by the second requester as soon
    # as the first requirement reaches its normal accepted boundary.
    first_item = ItemWorkspace.load(context, "REQ-001", mode="requirement")
    first_item.write_plan({"item_id": "REQ-001", "offline": True})
    first_item.write_draft({"answer": "REQ-001"})
    first_item.record_review("accept", reviewer_ref="business-reviewer")
    first_item.accept(accepted_refs=("work/plan.json",))
    assert any(action.action == "analyze_requirement" and action.subject_id == "REQ-002" for action in planner.next_actions())

    # A live data successor is appended and admitted through the public
    # generation-refresh API.  The requirement universe/plan remains intact;
    # only the data-bound generation advances to D-0002/G-0002.
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    first_archive = _archive(inputs / "initial.zip", "initial")
    successor_archive = _archive(inputs / "successor.zip", "successor")
    live_context = RunContext("RUN-COLD-LIVE-DATA", tmp_path / "live-run", (inputs,))
    live_record = _record("REQ-001", "Profile customer value.")
    live_plan = _plan((live_record,))
    lifecycle = RunLifecycle.create(live_context, ("REQ-001",), mode="requirement")
    item = ItemWorkspace.create(live_context, "REQ-001", mode="requirement", original_text=live_record.original_text)
    RequirementSupervisorWorkspace(live_context).save(live_plan)
    _accept_and_integrate(live_context, item)
    assert lifecycle.reconcile_from_run(product_terminal_status={"status": "complete"}).state == "complete"

    revisions = DataRevisionStore(live_context)
    initial = revisions.initialize_legacy(first_archive)
    successor = revisions.append(successor_archive, expected_current_revision_id=initial.revision_id)
    extension = RequirementRunExtension.refresh_data(
        live_context,
        live_plan,
        data_revision=successor,
        reopened_item_ids=("REQ-001",),
    )
    assert initial.revision_id == "D-0001"
    assert successor.revision_id == "D-0002"
    assert extension.generation_id == "G-0002"
    assert extension.reopened_item_ids == ("REQ-001",)
    assert RunLifecycle.load(live_context).snapshot.item_ids == ("REQ-001",)
    assert RequirementSupervisorWorkspace(live_context).load().input_records == (live_record,)


def test_coordinator_dispatches_nine_analytical_owners_and_reuses_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture real coordinator dispatches and exact resumable owner sessions.

    The planner below is intentionally deterministic and only models the
    public action-provider boundary: it holds the next action until the
    coordinator actually invokes the adapter, then advances.  The transport is
    a local fake Codex process, so every command, role-exit receipt, and
    persisted session observed here comes from the real RunCoordinator and
    CodexRoleAdapter paths without a model/network call.
    """

    pytest.importorskip("pandas")
    csv_path = tmp_path / "customers.csv"
    csv_path.write_text("customer_id,revenue\nA,10\nB,20\nC,30\n", encoding="utf-8")
    profile = profile_data(
        csv_path,
        requirement_id="REQ-001",
        columns=("customer_id", "revenue"),
        allowed_roots=(tmp_path,),
    )
    kpis = compute_kpi_table(
        csv_path,
        (MetricDefinition("revenue_total", "sum", value_column="revenue"),),
        requirement_id="REQ-002",
        allowed_roots=(tmp_path,),
    )
    assert profile.artifact_type == "data_profile"
    assert kpis.artifact_type == "kpi_table"

    calls: list[list[str]] = []
    prompts: list[str] = []
    transport_events: list[dict[str, object]] = []

    class InputSink:
        def write(self, value: bytes) -> int:
            prompts.append(value.decode("utf-8"))
            return len(value)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        """One successful local Codex invocation with a stable root session."""

        def __init__(self, argv: list[str], **_: object) -> None:
            self.argv = list(argv)
            calls.append(self.argv)
            output_path = Path(self.argv[self.argv.index("--output-last-message") + 1])
            output_path.write_text("toolkit-backed owner accepted", encoding="utf-8")
            if len(self.argv) > 2 and self.argv[2] == "resume":
                session_id = self.argv[-2]
                command_kind = "resume"
            else:
                fresh_count = sum(
                    1
                    for command in calls
                    if len(command) <= 2 or command[2] != "resume"
                )
                session_id = f"SID-{fresh_count:03d}"
                command_kind = "fresh"
            # This owner handles the supported toolkit work itself.  The fake
            # stream deliberately contains no collab/spawn event, so a
            # specialist process must not appear in the coordinator receipts.
            transport_events.append(
                {
                    "kind": command_kind,
                    "session_id": session_id,
                    "event_types": ("thread.started",),
                }
            )
            self.stdin = InputSink()
            self.stdout = io.BytesIO(
                (json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n").encode(
                    "utf-8"
                )
            )
            self.stderr = io.BytesIO(b"")
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", FakeProcess)

    context = RunContext("RUN-COLD-ECONOMICS", tmp_path / "economics-run", (tmp_path,))
    requirement_ids = tuple(f"REQ-{number:03d}" for number in range(1, 10))
    toolkit_methods = ("profile_data", "compute_kpi_table")
    actions = [
        PlannerAction(
            "analyze_requirement",
            "analytical_owner",
            requirement_id,
            f"Use the typed toolkit method {toolkit_methods[index % len(toolkit_methods)]}.",
            metadata={"toolkit_method": toolkit_methods[index % len(toolkit_methods)]},
        )
        for index, requirement_id in enumerate(requirement_ids)
    ]
    actions.extend(
        PlannerAction(
            "resume_requirement_analysis",
            "analytical_owner",
            requirement_id,
            "Continue the same analytical owner session.",
            metadata={"toolkit_method": toolkit_methods[index % len(toolkit_methods)]},
        )
        for index, requirement_id in enumerate(requirement_ids)
    )
    pending = list(actions)
    dispatched: list[PlannerAction] = []
    pending_lock = threading.Lock()

    class Planner:
        def next_actions(self, _context: RunContext, _state: dict) -> tuple[PlannerAction, ...]:
            with pending_lock:
                return (pending[0],) if pending else ()

    codex_adapter = CodexRoleAdapter(context, CodexExecConfig(binary="fake-codex"))

    def role(action: PlannerAction, **kwargs: object):
        # Advance only when the real coordinator submits this action.  Keeping
        # the head visible while a role is in flight mirrors a durable Planner
        # offer and prevents a test-only provider call from dropping actions.
        with pending_lock:
            assert pending and pending[0] == action
            pending.pop(0)
            dispatched.append(action)
        return codex_adapter(action, **kwargs)

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={
            "analyze_requirement": role,
            "resume_requirement_analysis": role,
        },
    )
    try:
        started = coordinator.start(
            CoordinatorRunSpec(
                context.run_id,
                "G-0001",
                "cold-fake-planner",
                hashlib.sha256(b"cold-fake-planner").hexdigest(),
            )
        )
        assert started.status == "ready"
        status = coordinator.run(max_steps=30)
    finally:
        coordinator.close()

    assert status.status == "waiting"
    assert not pending
    assert len(dispatched) == 18
    assert [action.action for action in dispatched[:9]] == ["analyze_requirement"] * 9
    assert [action.action for action in dispatched[9:]] == ["resume_requirement_analysis"] * 9
    assert all(action.role == "analytical_owner" for action in dispatched)

    event_path = context.resolve_run_path("control_plane/coordinator_events.jsonl")
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    dispatch_events = [event for event in events if event["event"] == "dispatch_started"]
    receipts = [event["payload"] for event in events if event["event"] == "role_exit"]
    assert len(dispatch_events) == len(receipts) == 18
    assert all(event["payload"]["action"]["role"] == "analytical_owner" for event in dispatch_events)
    assert all(receipt["action"]["role"] == "analytical_owner" for receipt in receipts)
    assert all(receipt["transport"]["exit_code"] == 0 for receipt in receipts)
    assert [receipt["transport"]["session_id"] for receipt in receipts[:9]] == [
        f"SID-{index:03d}" for index in range(1, 10)
    ]
    assert [receipt["transport"]["session_id"] for receipt in receipts[9:]] == [
        f"SID-{index:03d}" for index in range(1, 10)
    ]

    fresh_commands = [command for command in calls if len(command) <= 2 or command[2] != "resume"]
    resume_commands = [command for command in calls if len(command) > 2 and command[2] == "resume"]
    assert len(calls) == 9 * 2
    assert len(fresh_commands) == len(resume_commands) == 9
    assert all(command[0:2] == ["fake-codex", "exec"] for command in calls)
    assert all("--skip-git-repo-check" in command for command in calls)
    assert all("--output-schema" not in command for command in calls)
    assert [f"SID-{index:03d}" in command for index, command in enumerate(resume_commands, 1)] == [True] * 9
    assert len(transport_events) == 18
    assert all(event["event_types"] == ("thread.started",) for event in transport_events)

    sessions = RoleSessionRegistry(context).read()["sessions"]
    assert len(sessions) == 9
    assert set(sessions) == {f"analytical_owner:{requirement_id}" for requirement_id in requirement_ids}
    assert all(entry["role"] == "analytical_owner" for entry in sessions.values())
    assert all(
        sessions[f"analytical_owner:{requirement_id}"]["session_id"] == f"SID-{index:03d}"
        for index, requirement_id in enumerate(requirement_ids, 1)
    )
    assert all(
        [lineage["action"] for lineage in entry["action_lineage"]]
        == ["analyze_requirement", "resume_requirement_analysis"]
        for entry in sessions.values()
    )
    assert len(prompts) == 18
    assert all("Do not spawn or delegate subagents." in prompt for prompt in prompts)
