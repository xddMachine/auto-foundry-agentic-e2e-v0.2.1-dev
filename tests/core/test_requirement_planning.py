from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from auto_foundry_core import (
    CatalogCounts,
    CatalogSnapshot,
    DataRoomCatalogEntry,
    DataRoomMember,
    EntityResolutionResult,
    EntityResolutionWorkspace,
    IncidentRecord,
    IntegrationSession,
    ItemWorkspace,
    OntologyItem,
    PlannerAction,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementRunExtension,
    RequirementSupervisorWorkspace,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.prepared import PreparedAssetRegistry


def _record(requirement_id: str, text: str | None = None) -> RequirementRecord:
    return RequirementRecord(
        requirement_id=requirement_id,
        original_text=text or f"Investigate {requirement_id}.",
        business_objective=f"Support {requirement_id}",
        expected_analytical_outputs=(f"output-{requirement_id}",),
        dependencies=("source-a",),
        data_needs=("orders",),
        limitations=("synthetic fixture",),
        status="queued",
    )


def _catalog() -> CatalogSnapshot:
    member = DataRoomMember(
        path="orders.csv",
        format="csv",
        kind="table",
        size_bytes=42,
        compressed_size_bytes=30,
        content_hash="a" * 64,
    )
    entry = DataRoomCatalogEntry(
        member=member,
        table_name="orders",
        columns=("order_id", "amount"),
        row_count=2,
        row_count_exact=True,
    )
    return CatalogSnapshot(
        path=Path("/not-used/archive.zip"),
        content_hash="b" * 64,
        catalog_key="c" * 64,
        catalog_schema_version="1",
        source_hash="d" * 64,
        core_version="0.8.0",
        entries=(entry,),
        counts=CatalogCounts(archive_members=1, catalog_entries=1, table_members=1, sheet_entries=0),
    )


def _plan(records: tuple[RequirementRecord, ...], *, revision: int = 1) -> RequirementExecutionPlan:
    return RequirementExecutionPlan(
        input_records=records,
        groups=(
            RequirementExecutionGroup(("R3",), "Run the independent third requirement."),
            RequirementExecutionGroup(("R1",), "Run the first requirement."),
            RequirementExecutionGroup(
                ("R2",),
                "Compare the second requirement after R1.",
            ),
        ),
        planner_ref="cognitive-supervisor",
        portfolio_strategy="independent first, then evidence-dependent comparison",
        revision=revision,
    )


def _accept_and_integrate(context: RunContext, item: ItemWorkspace) -> None:
    """Publish one real accepted/integrated item for Planner boundary tests."""

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


def _accept_only(item: ItemWorkspace) -> None:
    item.write_plan({"item_id": item.item_id, "offline": True})
    item.write_draft({"answer": item.item_id})
    item.record_review("accept", reviewer_ref="business-reviewer")
    item.accept(accepted_refs=("work/plan.json",))


def _failed_identity_domain(
    context: RunContext,
    item: ItemWorkspace,
    *,
    domain_id: str = "inventory-product",
) -> EntityResolutionWorkspace:
    """Create a real failed identity domain bound to ``item``."""

    owner_ref = f"owner-{item.item_id}"
    item.bind_analysis_owner(owner_ref)
    item.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": domain_id,
            "object_type": "product",
            "rationale": "reviewed product identity required",
            "source_hints": ["inventory.csv"],
            "representation_item_ids": ["inventory-source"],
            "item_id": item.item_id,
            "owner_ref": owner_ref,
        }
    )
    resolution = EntityResolutionWorkspace.create(context)
    resolution.reserve_identity_domain(
        domain_id,
        "product",
        item.item_id,
        "reviewed product identity required",
        source_hints=("inventory.csv",),
        representation_item_ids=("inventory-source",),
    )
    resolution.claim_resolution_owner(domain_id, "resolution-owner")
    resolution.submit_result(
        domain_id,
        "resolution-owner",
        EntityResolutionResult(
            coverage={"source_count": 1, "mapped_count": 0},
            population={"source_count": 1},
            unresolved=(
                {"reason": "no deterministic identity match", "row_count": 1},
            ),
            evidence_refs=("work/plan.json",),
            source_hash="a" * 64,
            metadata={"resolution_outcome": "no_mapping_found"},
        ),
        expected_scope_hash=resolution.current_scope(domain_id).scope_hash,
    )
    resolution.record_review(domain_id, "fail", "independent-reviewer")
    assert EntityResolutionWorkspace.load(context).get_domain(domain_id).state == "failed"
    return resolution


def _save_plan(context: RunContext, records: tuple[RequirementRecord, ...], *, revision: int = 1) -> None:
    groups = tuple(
        RequirementExecutionGroup((record.requirement_id,), f"Run {record.requirement_id} independently.")
        for record in records
    )
    RequirementSupervisorWorkspace(context).save(
        RequirementExecutionPlan(
            input_records=records,
            groups=groups,
            planner_ref="planner",
            portfolio_strategy="preserve independent requirement order",
            revision=revision,
        )
    )


def _append_identity_proposal(
    context: RunContext,
    item_id: str,
    domain_id: str,
    *,
    owner_ref: str | None = None,
    object_type: str = "customer",
    rationale: str = "reviewed identity required",
) -> None:
    item = ItemWorkspace.load(context, item_id, mode="requirement")
    owner = owner_ref or f"ao-{item_id}"
    item.bind_analysis_owner(owner)
    item.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": domain_id,
            "object_type": object_type,
            "rationale": rationale,
            "source_hints": [f"{domain_id}.csv"],
            "representation_item_ids": [f"{domain_id}-source"],
            "item_id": item_id,
            "owner_ref": owner,
        }
    )


def test_plan_preserves_exact_records_and_cognitive_reorder() -> None:
    records = (_record("R1"), _record("R2"), _record("R3"))
    plan = _plan(records)

    assert plan.input_records == records
    assert plan.execution_order == ("R3", "R1", "R2")
    assert plan.groups[0].requirement_ids == ("R3",)
    assert not hasattr(plan.groups[2], "depends_on_requirement_ids")
    assert set(plan.to_dict()) == {
        "input_records",
        "groups",
        "planner_ref",
        "portfolio_strategy",
        "revision",
    }
    assert not any("hash" in key or "digest" in key or "fingerprint" in key for key in plan.to_dict())


def test_automatic_plan_orders_priority_dependencies_and_stable_input_without_dropping_records() -> None:
    records = (
        replace(_record("R1"), explicit_priority=20),
        replace(_record("R2"), explicit_priority=1, dependencies=("R1",)),
        replace(_record("R3"), explicit_priority=5),
        replace(_record("R4"), explicit_priority=None),
    )
    plan = RequirementExecutionPlan.from_requirements(records)

    assert plan.input_records == records
    assert plan.execution_order == ("R3", "R1", "R2", "R4")
    assert {item_id for group in plan.groups for item_id in group.requirement_ids} == {
        "R1",
        "R2",
        "R3",
        "R4",
    }


def test_runtime_known_dependency_waits_for_terminal_boundary_before_offer(tmp_path: Path) -> None:
    records = (
        replace(_record("R1"), dependencies=("source-a",)),
        replace(_record("R2"), dependencies=("R1",)),
    )
    plan = RequirementExecutionPlan.from_requirements(records)
    assert plan.execution_order == ("R1", "R2")
    workspace = RequirementSupervisorWorkspace(RunContext("RUN-RUNTIME-DEPS", tmp_path / "run", (tmp_path,)))
    workspace.save(plan)

    # A resolver wait on R1 must keep its known dependent out of the offer.
    assert workspace.next_requirement({}, {"R1": "waiting_on_resolution"}) is None
    assert workspace.next_requirement({"R1": "waiting"}, {"R1": "waiting_on_resolution"}) is None

    # Terminal/accepted boundaries release the dependent; a runtime resume
    # token alone does not bypass the dependency gate.
    assert workspace._runnable_requirement_ids(plan, {}, {"R1": "ready_to_resume"}) == ("R1",)
    assert workspace.next_requirement({"R1": "accepted"}, {"R1": "waiting_on_resolution"}) is None
    assert workspace.next_requirement({"R1": "accepted"}, {}) == "R2"
    assert workspace.next_requirement({"R1": "technical_failure"}, {}) == "R2"


def test_shared_identity_wait_keeps_known_dependent_out_of_planner_offer(tmp_path: Path) -> None:
    context = RunContext("RUN-SHARED-IDENTITY-DEPS", tmp_path / "run", (tmp_path,))
    records = (
        _record("R1"),
        replace(_record("R2"), dependencies=("R1",)),
    )
    RunLifecycle.create(context, ("R1", "R2"), mode="requirement")
    for item_id in ("R1", "R2"):
        ItemWorkspace.create(context, item_id, mode="requirement", original_text=item_id)
    _append_identity_proposal(context, "R1", "shared-customer")
    planner = RequirementSupervisorWorkspace(context)
    planner.plan_requirements(records)

    actions = planner.next_actions()
    assert [(action.action, action.subject_id) for action in actions] == [
        ("resolve_identity", "shared-customer"),
    ]
    assert EntityResolutionWorkspace.load(context).requirement_runtime_statuses()["R1"]["state"] == "waiting_on_resolution"

    # Simulate the externally committed identity completion, then advance R1
    # to its accepted boundary.  The same Planner tick can now offer R2.
    _accept_only(ItemWorkspace.load(context, "R1", mode="requirement"))
    resolution = EntityResolutionWorkspace.load(context)
    with resolution._locked():  # test-only durable fixture transition
        resolution._refresh()
        entry = dict(resolution._state["domains"]["shared-customer"])
        entry["state"] = "ready"
        resolution._state["domains"]["shared-customer"] = entry
        resolution._persist()
    resumed = planner.next_actions()
    assert any(action.action == "analyze_requirement" and action.subject_id == "R2" for action in resumed)


def test_planner_reconciles_shared_identity_requests_and_keeps_next_analytical_owner_runnable(tmp_path: Path) -> None:
    context = RunContext("RUN-SHARED-IDENTITY", tmp_path / "run", (tmp_path,))
    records = tuple(_record(item_id) for item_id in ("R1", "R2"))
    RunLifecycle.create(context, ("R1", "R2"), mode="requirement")
    for item_id in ("R1", "R2"):
        ItemWorkspace.create(context, item_id, mode="requirement", original_text=item_id)
    _append_identity_proposal(context, "R1", "shared-customer")
    planner = RequirementSupervisorWorkspace(context)
    planner.plan_requirements(records)

    first = planner.next_actions()
    assert [(action.action, action.subject_id) for action in first] == [
        ("resolve_identity", "shared-customer"),
        ("analyze_requirement", "R2"),
    ]
    first_state = (context.run_root / "entity_resolution" / "state.json").read_bytes()

    _append_identity_proposal(context, "R2", "shared-customer")
    second = planner.next_actions()
    assert [(action.action, action.subject_id) for action in second] == [
        ("resolve_identity", "shared-customer"),
    ]
    resolution = EntityResolutionWorkspace.load(context)
    domain = resolution.get_domain("shared-customer")
    assert domain.requested_by == ("R1", "R2")
    assert set(resolution.requirement_runtime_statuses()) == {"R1", "R2"}
    # A subsequent exact scheduling retry does not rewrite the durable state.
    second_state = (context.run_root / "entity_resolution" / "state.json").read_bytes()
    assert planner.next_actions() == second
    assert (context.run_root / "entity_resolution" / "state.json").read_bytes() == second_state
    assert first_state != second_state


def test_material_request_expansion_resumes_one_existing_identity_domain(tmp_path: Path) -> None:
    context = RunContext("RUN-SHARED-IDENTITY-EXPANSION", tmp_path / "run", (tmp_path,))
    records = tuple(_record(item_id) for item_id in ("R1", "R2"))
    RunLifecycle.create(context, ("R1", "R2"), mode="requirement")
    for item_id in ("R1", "R2"):
        ItemWorkspace.create(context, item_id, mode="requirement", original_text=item_id)
    _append_identity_proposal(context, "R1", "shared-customer")
    planner = RequirementSupervisorWorkspace(context)
    planner.plan_requirements(records)
    first = planner.next_actions()
    assert [(action.action, action.subject_id) for action in first if action.role == "entity_resolution_owner"] == [
        ("resolve_identity", "shared-customer")
    ]

    resolution = EntityResolutionWorkspace.load(context)
    resolution.claim_resolution_owner("shared-customer", "resolution-owner")
    resolution.submit_result(
        "shared-customer",
        "resolution-owner",
        EntityResolutionResult(
            coverage={"source_count": 1, "mapped_count": 0},
            population={"source_count": 1},
            unresolved=({"reason": "awaiting expanded scope", "row_count": 1},),
            evidence_refs=("work/identity.json",),
            source_hash="a" * 64,
            metadata={"resolution_outcome": "no_mapping_found"},
        ),
        expected_scope_hash=resolution.current_scope("shared-customer").scope_hash,
    )
    second_item = ItemWorkspace.load(context, "R2", mode="requirement")
    second_item.bind_analysis_owner("ao-R2")
    second_item.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": "shared-customer",
            "object_type": "customer",
            "rationale": "new returns representation requires refreshed identity",
            "source_hints": ["returns.csv"],
            "representation_item_ids": ["returns-customer-id"],
            "item_id": "R2",
            "owner_ref": "ao-R2",
        }
    )
    resumed = planner.next_actions()
    identity_actions = [
        (action.action, action.subject_id)
        for action in resumed
        if action.role == "entity_resolution_owner"
    ]
    assert identity_actions == [("resume_identity_resolution", "shared-customer")]
    assert EntityResolutionWorkspace.load(context).get_domain("shared-customer").result_hash is None


def test_planner_recovers_mixed_historical_owner_provenance_without_rewriting_proposals(
    tmp_path: Path,
) -> None:
    context = RunContext("RUN-MIXED-OWNER-RECOVERY", tmp_path / "run", (tmp_path,))
    RunLifecycle.create(context, ("R1",), mode="requirement")
    item = ItemWorkspace.create(context, "R1", mode="requirement", original_text="R1")
    _append_identity_proposal(context, "R1", "customer-domain", owner_ref="owner-stable")

    proposal_path = item.work_root / "identity_domain_proposals.jsonl"
    legacy = {
        "record_kind": "identity_domain_proposal",
        "domain_id": "supplier-domain",
        "object_type": "customer",
        "rationale": "reviewed identity required",
        "source_hints": ["supplier-domain.csv"],
        "representation_item_ids": ["supplier-domain-source"],
        "item_id": "R1",
        "owner_ref": "owner-retry-transport-label",
    }
    with proposal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n")
    proposal_bytes = proposal_path.read_bytes()

    planner = RequirementSupervisorWorkspace(context)
    planner.plan_requirements((_record("R1"),))
    reconciled = planner.reconcile_identity_requests()

    assert reconciled[0]["domain_ids"] == ("customer-domain", "supplier-domain")
    assert proposal_path.read_bytes() == proposal_bytes
    assert item.analysis_owner_ref() == "owner-stable"
    resolution = EntityResolutionWorkspace.load(context)
    for domain_id in ("customer-domain", "supplier-domain"):
        request = resolution.get_domain(domain_id).requests[0]
        assert request["owner_ref"] == "owner-stable"


def test_planner_parallel_identity_capacity_and_multi_domain_wakeup(tmp_path: Path) -> None:
    context = RunContext("RUN-PARALLEL-IDENTITY", tmp_path / "run", (tmp_path,))
    ids = ("R1", "R2", "R3", "R4")
    records = tuple(_record(item_id) for item_id in ids)
    RunLifecycle.create(context, ids, mode="requirement")
    for item_id in ids:
        ItemWorkspace.create(context, item_id, mode="requirement", original_text=item_id)
    for item_id, domain_id in zip(ids[:3], ("D1", "D2", "D3")):
        _append_identity_proposal(context, item_id, domain_id)
    planner = RequirementSupervisorWorkspace(context)
    planner.plan_requirements(records)
    actions = planner.next_actions()
    assert [(action.action, action.subject_id) for action in actions] == [
        ("resolve_identity", "D1"),
        ("resolve_identity", "D2"),
        ("resolve_identity", "D3"),
        ("analyze_requirement", "R4"),
    ]

    # A requirement with two required domains remains waiting until both are
    # ready; readiness wakes all requesters in plan order.
    context2 = RunContext("RUN-MULTI-DOMAIN-IDENTITY", tmp_path / "run2", (tmp_path,))
    RunLifecycle.create(context2, ("R1", "R2"), mode="requirement")
    for item_id in ("R1", "R2"):
        ItemWorkspace.create(context2, item_id, mode="requirement", original_text=item_id)
    _append_identity_proposal(context2, "R1", "D1")
    _append_identity_proposal(context2, "R1", "D2")
    planner2 = RequirementSupervisorWorkspace(context2)
    planner2.plan_requirements(tuple(_record(item_id) for item_id in ("R1", "R2")))
    initial = planner2.next_actions()
    assert [(action.action, action.subject_id) for action in initial] == [
        ("resolve_identity", "D1"),
        ("resolve_identity", "D2"),
        ("analyze_requirement", "R2"),
    ]
    assert EntityResolutionWorkspace.load(context2).state["waits"]["R1"]["domain_ids"] == ["D1", "D2"]


def test_group_can_hold_japan_and_spain_analog_requirements_without_synthetic_id() -> None:
    records = (_record("Japan"), _record("Spain"), _record("R3"))
    group = RequirementExecutionGroup(
        ("Japan", "Spain"),
        "Analyze the two analogous country requirements together.",
        shared_analysis_intent="Use one comparable regional analysis.",
        suggested_specialists=("regional-analyst", "finance"),
    )
    plan = RequirementExecutionPlan(
        input_records=records,
        groups=(group, RequirementExecutionGroup(("R3",), "Keep the unrelated work independent.")),
        planner_ref="cognitive-supervisor",
        portfolio_strategy="group analogous requirements",
        revision=1,
    )

    assert plan.groups[0].requirement_ids == ("Japan", "Spain")
    assert not hasattr(plan.groups[0], "group_id")
    assert plan.execution_order == ("Japan", "Spain", "R3")


def test_grouped_terminal_member_does_not_suppress_pending_sibling_or_hide_later_group(tmp_path: Path) -> None:
    records = (_record("Japan"), _record("Spain"), _record("France"))
    grouped = RequirementExecutionGroup(
        ("Japan", "Spain"),
        "Analyze the two analogous country requirements together.",
        shared_analysis_intent="Use one comparable regional analysis.",
        suggested_specialists=("regional-analyst", "finance"),
    )
    later_group = RequirementExecutionGroup(("France",), "Run France after the grouped investigation.")
    workspace = RequirementSupervisorWorkspace(RunContext("RUN-GROUP-READY", tmp_path / "run", (tmp_path,)))
    workspace.save(
        RequirementExecutionPlan(
            input_records=records,
            groups=(grouped, later_group),
            planner_ref="cognitive-supervisor",
            portfolio_strategy="share regional investigation",
            revision=1,
        )
    )

    ready = workspace.ready_groups({"Japan": "technical_failure"})
    assert [group.requirement_ids for group in ready] == [("Spain",), ("France",)]
    assert ready[0].rationale == grouped.rationale
    assert ready[0].shared_analysis_intent == grouped.shared_analysis_intent
    assert ready[0].suggested_specialists == grouped.suggested_specialists
    assert workspace.next_group({"Japan": "technical_failure"}) == ready[0]


def test_replan_can_replace_add_and_remove_input_records(tmp_path: Path) -> None:
    records = (_record("R1"), _record("R2"), _record("R3"))
    context = RunContext("RUN-RECORDS", tmp_path / "run", (tmp_path,))
    workspace = RequirementSupervisorWorkspace(context)
    first = _plan(records)
    workspace.save(first)
    initial_payload = json.loads(workspace.plan_path.read_text(encoding="utf-8"))
    initial_records = initial_payload["input_records"]

    revised = replace(
        first,
        groups=(
            RequirementExecutionGroup(("R3",), "Keep R3 first."),
            RequirementExecutionGroup(("R1", "R2"), "Share the R1/R2 investigation."),
        ),
        portfolio_strategy="replanned shared investigation",
        revision=2,
    )
    workspace.save(revised)
    persisted = json.loads(workspace.plan_path.read_text(encoding="utf-8"))
    assert persisted["input_records"] == initial_records

    replacement = replace(
        revised,
        input_records=(_record("R1"), _record("R2", "A changed requirement."), _record("R3")),
        revision=3,
    )
    workspace.save(replacement)
    assert workspace.load().input_records[1].original_text == "A changed requirement."

    removal = replace(
        revised,
        input_records=(_record("R1"), _record("R2")),
        groups=(
            RequirementExecutionGroup(("R1",), "R1 remains."),
            RequirementExecutionGroup(("R2",), "R2 remains."),
        ),
        revision=4,
    )
    workspace.save(removal)
    assert tuple(record.requirement_id for record in workspace.load().input_records) == ("R1", "R2")

    addition = replace(
        revised,
        input_records=records + (_record("R4"),),
        groups=(
            RequirementExecutionGroup(("R3",), "Keep R3 first."),
            RequirementExecutionGroup(("R1", "R2"), "Share the R1/R2 investigation."),
            RequirementExecutionGroup(("R4",), "Add a new requirement."),
        ),
        revision=5,
    )
    workspace.save(addition)
    assert tuple(record.requirement_id for record in workspace.load().input_records) == ("R1", "R2", "R3", "R4")


def test_workspace_save_load_and_revision_replaces_current_plan(tmp_path: Path) -> None:
    records = (_record("R1"), _record("R2"), _record("R3"))
    context = RunContext("RUN-SUPERVISOR", tmp_path / "run", (tmp_path,))
    workspace = RequirementSupervisorWorkspace(context)
    first = _plan(records)

    assert workspace.save(first) == first
    assert workspace.load() == first
    assert workspace.save(first) == first  # exact retry is harmless

    revised = replace(
        first,
        groups=(
            RequirementExecutionGroup(("R1",), "Retry R1 with a corrected approach."),
            RequirementExecutionGroup(("R3",), "The independent requirement stays available."),
            RequirementExecutionGroup(("R2",), "Compare R2 after revised R1."),
        ),
        portfolio_strategy="repair failed R1 before dependent comparison",
        revision=2,
    )
    assert workspace.save(revised) == revised
    assert workspace.load() == revised
    with pytest.raises(ValueError, match="higher revision"):
        workspace.save(replace(revised, portfolio_strategy="stale", revision=1))


def test_failure_does_not_block_later_groups(tmp_path: Path) -> None:
    records = tuple(_record(item_id) for item_id in ("R1", "R2", "R3", "R4"))
    plan = RequirementExecutionPlan(
        input_records=records,
        groups=(
            RequirementExecutionGroup(("R1",), "First requirement."),
            RequirementExecutionGroup(("R2",), "Later requirement."),
            RequirementExecutionGroup(("R3",), "Independent later requirement."),
            RequirementExecutionGroup(("R4",), "Another later requirement."),
        ),
        planner_ref="cognitive-supervisor",
        portfolio_strategy="keep independent work moving",
        revision=1,
    )
    workspace = RequirementSupervisorWorkspace(RunContext("RUN-READY", tmp_path / "run", (tmp_path,)))
    workspace.save(plan)

    ready = workspace.ready_groups({"R1": "technical_failure"})
    assert [group.requirement_ids for group in ready] == [("R2",), ("R3",), ("R4",)]
    assert workspace.next_group({"R1": "technical_failure"}) == ready[0]

    # Every later group remains schedulable; semantic blocking is discovered
    # by the runtime analytical owner rather than declared by the Planner.
    assert [group.requirement_ids for group in workspace.ready_groups({"R1": "processed"})] == [("R2",), ("R3",), ("R4",)]
    assert [group.requirement_ids for group in workspace.ready_groups({"R1": "limited"})] == [("R2",), ("R3",), ("R4",)]


def test_planner_input_keeps_records_outcomes_and_compact_catalog_without_rows() -> None:
    records = (_record("R1"), _record("R2"))
    outcomes = {"R1": "technical_failure"}
    payload = RequirementSupervisorWorkspace.planner_input(records, _catalog(), outcomes)

    assert payload["requirements"] == records
    assert payload["item_outcomes"] == outcomes
    assert set(payload["catalog"]) == {"entries"}
    encoded = json.dumps(payload, default=lambda value: value.to_dict() if hasattr(value, "to_dict") else str(value))
    assert "sample_rows" not in encoded
    assert "sample_values" not in encoded
    assert "technical_failure" in encoded


def test_compact_catalog_rejects_rows_and_omits_physical_hashes() -> None:
    catalog = _catalog()
    tainted = replace(
        catalog,
        entries=(replace(catalog.entries[0], sample_rows=({"amount": 1},)),),
    )
    with pytest.raises(ValueError, match="samples or rows"):
        RequirementSupervisorWorkspace.planner_input((_record("R1"),), tainted)

    compact = RequirementSupervisorWorkspace.planner_input((_record("R1"),), catalog)["catalog"]
    encoded = json.dumps(compact, sort_keys=True)
    assert "content_hash" not in encoded
    assert "source_hash" not in encoded
    assert "catalog_key" not in encoded


def test_plan_rejects_duplicate_groups_and_removed_dependency_field() -> None:
    records = (_record("R1"), _record("R2"))
    with pytest.raises(ValueError, match="duplicates"):
        RequirementExecutionPlan(
            input_records=records,
            groups=(RequirementExecutionGroup(("R1", "R1"), "duplicate"), RequirementExecutionGroup(("R2",), "other")),
            planner_ref="planner",
            portfolio_strategy="strategy",
            revision=1,
        )
    with pytest.raises(TypeError):
        RequirementExecutionGroup(("R1",), "first", depends_on_requirement_ids=("R2",))  # type: ignore[call-arg]


def test_plan_json_roundtrip_has_no_hash_or_lifecycle_authority() -> None:
    records = (_record("R1"), _record("R2"), _record("R3"))
    plan = _plan(records)
    restored = RequirementExecutionPlan.from_dict(json.loads(json.dumps(plan.to_dict())))

    assert restored == plan
    assert restored.input_records == records
    assert "plan_hash" not in plan.to_dict()
    assert "catalog_fingerprint" not in plan.to_dict()
    assert "implementation_identity" not in plan.to_dict()
    assert "lifecycle_authority" not in plan.to_dict()


def test_runtime_waiting_skips_and_resume_returns_to_original_plan_position(tmp_path: Path) -> None:
    records = tuple(_record(item_id) for item_id in ("R1", "R2", "R3"))
    workspace = RequirementSupervisorWorkspace(RunContext("RUN-RUNTIME-WAIT", tmp_path / "run", (tmp_path,)))
    workspace.save(_plan(records))

    # Cognitive plan order is R3, R1, R2.  Waiting R3 must not be appended or
    # treated as a planner-predicted dependency; R1 is the earliest runnable.
    assert workspace.next_requirement(
        {},
        {"R3": "waiting_on_resolution", "R1": "ready_to_resume"},
    ) == "R1"
    assert [group.requirement_ids for group in workspace.ready_groups(
        {},
        {"R3": "waiting_on_resolution", "R1": "ready_to_resume"},
    )] == [("R1",), ("R2",)]

    # Once the resolver releases R3, it returns to its original earliest
    # position rather than being scheduled after the later requirements.
    assert workspace.next_requirement(
        {},
        {"R3": "ready_to_resume", "R1": "ready_to_resume"},
    ) == "R3"


def test_scheduling_tick_starts_next_requirement_while_prior_item_waits(tmp_path: Path) -> None:
    context = RunContext("RUN-EVENT-DRIVEN", tmp_path / "run", (tmp_path,))
    records = (_record("REQ-04"), _record("REQ-05"))
    RunLifecycle.create(context, ("REQ-04", "REQ-05"), mode="requirement")
    req04 = ItemWorkspace.create(context, "REQ-04", original_text="four", mode="requirement")
    ItemWorkspace.create(context, "REQ-05", original_text="five", mode="requirement")
    req04.begin_attempt("ao-REQ-04", "Analytical Owner")
    supervisor = RequirementSupervisorWorkspace(context)
    supervisor.save(
        RequirementExecutionPlan(
            input_records=records,
            groups=(RequirementExecutionGroup(("REQ-04", "REQ-05"), "Preserve user order."),),
            planner_ref="planner",
            portfolio_strategy="preserve order and skip runtime waits",
            revision=1,
        )
    )

    busy = supervisor.scheduling_tick()
    assert busy["run_status"] == "running"
    assert busy["scheduler_status"] == "at_capacity"
    assert busy["next_requirement_id"] is None

    resolution = EntityResolutionWorkspace.create(context)
    with pytest.raises(ValueError, match="Analytical Owner proposal"):
        resolution.reserve_identity_domain(
            "req04.supplier",
            "supplier",
            "REQ-04",
            "reviewed identity required",
            source_hints=("supplier.csv",),
            representation_item_ids=("supplier-source",),
        )
    req04.bind_analysis_owner("ao-REQ-04")
    req04.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": "req04.supplier",
            "object_type": "supplier",
            "rationale": "reviewed identity required",
            "source_hints": ["supplier.csv"],
            "representation_item_ids": ["supplier-source"],
            "item_id": "REQ-04",
            "owner_ref": "ao-REQ-04",
        }
    )
    resolution.reserve_identity_domain(
        "req04.supplier",
        "supplier",
        "REQ-04",
        "reviewed identity required",
        source_hints=("supplier.csv",),
        representation_item_ids=("supplier-source",),
    )
    resolution.mark_waiting_on_resolution(
        "REQ-04",
        ("req04.supplier",),
        "reviewed identity required",
        owner_ref="ao-REQ-04",
    )
    ready = supervisor.scheduling_tick()
    assert ready["run_status"] == "running"
    assert RunLifecycle.load(context).state == "running"
    assert ready["scheduler_status"] == "runnable"
    assert ready["next_requirement_id"] == "REQ-05"
    assert ready["runnable_requirement_ids"] == ["REQ-05"]
    assert ready["item_outcomes"] == {"REQ-04": "waiting", "REQ-05": "pending"}
    assert ready["runtime_statuses"] == {"REQ-04": "waiting_on_resolution"}

    typed = supervisor.runtime_snapshot()
    assert typed.next_requirement_id == "REQ-05"
    assert typed.runnable_requirement_ids == ("REQ-05",)
    assert typed.to_dict() == ready

    actions = supervisor.next_actions()
    assert all(isinstance(action, PlannerAction) for action in actions)
    assert [(action.action, action.role, action.subject_id) for action in actions] == [
        ("resolve_identity", "entity_resolution_owner", "req04.supplier"),
        ("analyze_requirement", "analytical_owner", "REQ-05"),
    ]


def test_active_business_repair_does_not_offer_stale_review_and_reoffers_after_attempt(
    tmp_path: Path,
) -> None:
    context = RunContext("RUN-REPAIR-ADMISSION", tmp_path / "run", (tmp_path,))
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    item = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="repair")
    item.write_draft({"answer": "initial"})
    initial_attempt = item.begin_attempt("ao-initial", "Analytical Owner")
    item.finish_attempt(initial_attempt.attempt_id, status="completed")
    item.record_review(
        "repair_once",
        reviewer_ref="reviewer",
        findings=(
            {
                "finding_id": "F-REPAIR",
                "message": "answer needs a bounded correction",
                "pointers": ("/answer",),
                "semantic_categories": ("answer",),
            },
        ),
    )
    item.use_business_repair(owner_ref="ao-repair")

    planner = RequirementSupervisorWorkspace(context)
    planner.save(
        RequirementExecutionPlan(
            input_records=(RequirementRecord(requirement_id="REQ-01", original_text="repair"),),
            groups=(RequirementExecutionGroup(("REQ-01",), "one item"),),
            planner_ref="planner",
            portfolio_strategy="single item",
            revision=1,
        )
    )

    during_repair = planner.next_actions()
    assert [(action.action, action.role, action.subject_id) for action in during_repair] == [
        ("resume_requirement_analysis", "analytical_owner", "REQ-01")
    ]
    assert during_repair[0].metadata == {"repair_active": True, "repair_count": 1}

    repair_attempt = item.begin_attempt("ao-repair", "Analytical Owner")
    item.finish_attempt(repair_attempt.attempt_id, status="completed")
    after_repair = planner.next_actions()
    assert [(action.action, action.role, action.subject_id) for action in after_repair] == [
        ("review_requirement", "business_reviewer", "REQ-01")
    ]


def test_failed_business_repair_attempt_resumes_authorization_without_review(
    tmp_path: Path,
) -> None:
    context = RunContext("RUN-REPAIR-FAILED-RESUME", tmp_path / "run", (tmp_path,))
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    item = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="repair")
    item.write_draft({"answer": "initial"})
    initial_attempt = item.begin_attempt("ao-initial", "Analytical Owner")
    item.finish_attempt(initial_attempt.attempt_id, status="completed")
    item.record_review(
        "repair_once",
        reviewer_ref="reviewer",
        findings=(
            {
                "finding_id": "F-REPAIR",
                "message": "answer needs a bounded correction",
                "pointers": ("/answer",),
                "semantic_categories": ("answer",),
            },
        ),
    )
    item.use_business_repair(owner_ref="ao-repair")
    repair_attempt = item.begin_attempt("ao-repair", "Analytical Owner")
    item.finish_attempt(repair_attempt.attempt_id, status="failed", error="script failed")

    planner = RequirementSupervisorWorkspace(context)
    planner.save(
        RequirementExecutionPlan(
            input_records=(RequirementRecord(requirement_id="REQ-01", original_text="repair"),),
            groups=(RequirementExecutionGroup(("REQ-01",), "one item"),),
            planner_ref="planner",
            portfolio_strategy="single item",
            revision=1,
        )
    )

    actions = planner.next_actions()
    assert [(action.action, action.role, action.subject_id) for action in actions] == [
        ("resume_requirement_analysis", "analytical_owner", "REQ-01")
    ]
    assert actions[0].metadata == {"repair_active": True, "repair_count": 1}


def test_historical_failed_identity_domain_does_not_preempt_appended_requirement(tmp_path: Path) -> None:
    """A G5-shaped cumulative lifecycle suppresses only valid prior boundaries."""

    context = RunContext("RUN-HISTORICAL-IDENTITY", tmp_path / "run", (tmp_path,))
    first_record = _record("REQ-06")
    RunLifecycle.create(context, (first_record.requirement_id,), mode="requirement")
    first_item = ItemWorkspace.create(context, first_record.requirement_id, mode="requirement", original_text=first_record.original_text)
    _failed_identity_domain(context, first_item)
    _accept_and_integrate(context, first_item)
    _save_plan(context, (first_record,))
    lifecycle = RunLifecycle.load(context)
    lifecycle.reconcile_from_run(product_terminal_status="complete")
    assert RunLifecycle.load(context).state == "complete"

    added_record = _record("REQ-23")
    extended_plan = RequirementExecutionPlan(
        input_records=(first_record, added_record),
        groups=(
            RequirementExecutionGroup((first_record.requirement_id,), "Run REQ-06 independently."),
            RequirementExecutionGroup((added_record.requirement_id,), "Analyze the new requirement."),
        ),
        planner_ref="planner",
        portfolio_strategy="preserve cumulative history and run the new requirement",
        revision=2,
    )
    RequirementRunExtension.append(context, (added_record,), plan=extended_plan)

    entity_state_path = context.run_root / "entity_resolution" / "state.json"
    entity_state_before = entity_state_path.read_bytes()
    actions = RequirementSupervisorWorkspace(context).next_actions()
    assert [(action.action, action.role, action.subject_id) for action in actions] == [
        ("analyze_requirement", "analytical_owner", "REQ-23"),
    ]
    assert entity_state_path.read_bytes() == entity_state_before
    assert EntityResolutionWorkspace.load(context).get_domain("inventory-product").state == "failed"


def test_current_nonterminal_failed_identity_domain_remains_actionable(tmp_path: Path) -> None:
    context = RunContext("RUN-ACTIVE-IDENTITY", tmp_path / "run", (tmp_path,))
    record = _record("REQ-23")
    RunLifecycle.create(context, (record.requirement_id,), mode="requirement")
    item = ItemWorkspace.create(context, record.requirement_id, mode="requirement", original_text=record.original_text)
    _failed_identity_domain(context, item)
    _save_plan(context, (record,))

    actions = RequirementSupervisorWorkspace(context).next_actions()
    failure_actions = [action for action in actions if action.action == "escalate_identity_failure"]
    assert len(failure_actions) == 1
    assert failure_actions[0].metadata == {
        "binding_status": "active_unresolved",
        "discovered_by_item_id": "REQ-23",
    }
    assert not any(
        action.action == "analyze_requirement" and action.subject_id == "REQ-23"
        for action in actions
    )
    assert EntityResolutionWorkspace.load(context).requirement_runtime_statuses()["REQ-23"]["state"] == "waiting_on_resolution"


def test_shared_failed_identity_domain_escalates_once_for_active_requester_and_wakes_after_ready(
    tmp_path: Path,
) -> None:
    context = RunContext("RUN-SHARED-FAILED-IDENTITY", tmp_path / "run", (tmp_path,))
    first_record = _record("R1")
    second_record = _record("R2")
    RunLifecycle.create(context, ("R1", "R2"), mode="requirement")
    first_item = ItemWorkspace.create(context, "R1", mode="requirement", original_text="R1")
    ItemWorkspace.create(context, "R2", mode="requirement", original_text="R2")
    _failed_identity_domain(context, first_item, domain_id="shared-customer")
    _accept_and_integrate(context, first_item)
    _append_identity_proposal(context, "R2", "shared-customer", object_type="product")
    _save_plan(context, (first_record, second_record))

    planner = RequirementSupervisorWorkspace(context)
    initial = planner.next_actions()
    failures = [action for action in initial if action.action == "escalate_identity_failure"]
    assert len(failures) == 1
    assert failures[0].subject_id == "shared-customer"
    assert failures[0].metadata["requested_by"] == ["R1", "R2"]
    assert not any(action.action == "analyze_requirement" and action.subject_id == "R2" for action in initial)
    resolution = EntityResolutionWorkspace.load(context)
    domain = resolution.get_domain("shared-customer")
    assert domain.requested_by == ("R1", "R2")
    assert resolution.requirement_runtime_statuses()["R2"]["state"] == "waiting_on_resolution"

    # A ready transition wakes every exact requester; the failed-domain
    # escalation disappears and the active R2 AO action is offered.
    with resolution._locked():  # test-only durable fixture transition
        resolution._refresh()
        entry = dict(resolution._state["domains"]["shared-customer"])
        entry["state"] = "ready"
        resolution._state["domains"]["shared-customer"] = entry
        resolution._persist()
    resumed = planner.next_actions()
    assert not any(action.action == "escalate_identity_failure" for action in resumed)
    assert any(action.action == "analyze_requirement" and action.subject_id == "R2" for action in resumed)


@pytest.mark.parametrize("integration_mode", ("pending", "invalid"))
def test_terminal_failed_identity_domain_is_retained_until_integration_boundary_is_valid(
    tmp_path: Path,
    integration_mode: str,
) -> None:
    context = RunContext(f"RUN-TERMINAL-IDENTITY-{integration_mode}", tmp_path / integration_mode, (tmp_path,))
    record = _record("REQ-06")
    RunLifecycle.create(context, (record.requirement_id,), mode="requirement")
    item = ItemWorkspace.create(context, record.requirement_id, mode="requirement", original_text=record.original_text)
    _failed_identity_domain(context, item)
    _accept_only(item)
    if integration_mode == "invalid":
        # The public item boundary records an integration pointer, but the
        # manifest is intentionally absent; phase_snapshot must fail closed.
        item.mark_integration_committed("a" * 64, "integration/committed/manifest.json")
        (item.item_root / "integration" / "committed").mkdir(parents=True)
    _save_plan(context, (record,))

    planner = RequirementSupervisorWorkspace(context)
    snapshot = planner.phase_snapshot()["items"][record.requirement_id]
    assert snapshot["terminal_status"] == "accepted"
    if integration_mode == "pending":
        assert snapshot["integration_state"] == "pending"
        assert snapshot["integration_stage"] == "not_started"
    else:
        assert snapshot["integration_state"] == "integrated"
        assert snapshot["integration_stage"] == "invalid"
    actions = planner.next_actions()
    assert any(action.action == "escalate_identity_failure" for action in actions)


def test_planner_incident_log_is_canonical_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    context = RunContext("RUN-INCIDENT", tmp_path / "run", (tmp_path,))
    workspace = RequirementSupervisorWorkspace(context)
    incident = IncidentRecord(
        incident_id="REQ-01-SCRIPT-001",
        category="program",
        disposition="corrected_same_attempt",
        admissible=True,
        item_id="REQ-01",
        scope=("analysis_script",),
        source="analytical_owner",
        facts={"error": "KeyError", "product_fix_status": "pending"},
    )

    first = workspace.record_incident(incident)
    assert workspace.record_incident(incident) == first
    lines = context.resolve_run_path("run_incidents.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.dumps(json.loads(lines[0]), ensure_ascii=False, sort_keys=True, separators=(",", ":")) == lines[0]

    conflicting = IncidentRecord(
        incident_id=incident.incident_id,
        category="program",
        disposition="different",
        admissible=True,
        item_id="REQ-01",
    )
    with pytest.raises(ValueError, match="incident_id conflicts"):
        workspace.record_incident(conflicting)


def test_all_waiting_status_distinguishes_active_resolution_from_true_blocked(tmp_path: Path) -> None:
    records = (_record("R1"),)
    workspace = RequirementSupervisorWorkspace(RunContext("RUN-RUNTIME-STATUS", tmp_path / "run", (tmp_path,)))
    workspace.save(
        RequirementExecutionPlan(
            input_records=records,
            groups=(RequirementExecutionGroup(("R1",), "Resolve R1 first."),),
            planner_ref="cognitive-supervisor",
            portfolio_strategy="wait for external resolution",
            revision=1,
        )
    )

    runtime = {"R1": "waiting_on_resolution"}
    assert workspace.all_waiting_status({}, runtime, active_resolver_count=1) == "waiting_on_resolution"
    assert workspace.all_waiting_status({}, runtime, resolver_progressed=True) == "waiting_on_resolution"
    assert workspace.all_waiting_status({}, runtime, active_resolver_count=0) == "blocked"


def test_runtime_status_contract_does_not_accept_legacy_aliases(tmp_path: Path) -> None:
    records = tuple(_record(item_id) for item_id in ("R1", "R2", "R3"))
    workspace = RequirementSupervisorWorkspace(RunContext("RUN-RUNTIME-EXACT", tmp_path / "run", (tmp_path,)))
    workspace.save(_plan(records))

    # ``waiting`` is an item outcome, not a runtime resolver state.  Runtime
    # aliases therefore cannot suppress a fresh requirement or release an
    # item whose normal outcome remains waiting.
    assert workspace.next_requirement({"R3": "success"}, {"R1": "waiting_on_resolution"}) == "R2"
    assert workspace.next_requirement({"R3": "success"}, {"R1": "waiting"}) == "R1"
    assert workspace.next_requirement(
        {"R3": "success", "R1": "waiting"},
        {"R1": "resume_ready"},
    ) == "R2"

    # Runtime resolver tokens are exact byte strings; punctuation, spacing,
    # and case variants are ignored rather than normalized into authority.
    assert workspace.next_requirement({"R3": "waiting"}, {"R3": "ready-to-resume"}) == "R1"
    assert workspace.next_requirement({}, {"R3": "waiting on resolution"}) == "R3"
    assert workspace.next_requirement({}, {"R3": "WAITING_ON_RESOLUTION"}) == "R3"
