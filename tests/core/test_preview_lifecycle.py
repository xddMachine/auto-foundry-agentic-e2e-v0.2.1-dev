"""Incremental Product Agent preview scheduling and durable handoff tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading

import pytest

from auto_foundry_core import (
    CoordinatorRunSpec,
    IntegrationSession,
    ItemWorkspace,
    PlannerAction,
    RequirementSupervisorWorkspace,
    RoleExecution,
    RunContext,
    RunCoordinator,
    RunLifecycle,
)
from auto_foundry_core import coordinator as coordinator_module
from auto_foundry_core import requirement_planning as planning
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.product_review import ProductCandidate, ProductReviewStore, canonical_hash

from tests.core.test_requirement_planning import (
    _accept_and_integrate,
    _accept_only,
    _record,
    _save_plan,
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _load_dashboard_assembler() -> object:
    """Load the production assembler module for an end-to-end preview check."""

    assembler_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "auto-foundry-agentic-e2e"
        / "scripts"
        / "dashboard_assembler.py"
    )
    spec = importlib.util.spec_from_file_location("preview_lifecycle_dashboard_assembler", assembler_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_dashboard_generation_product() -> object:
    """Load the public generation preview/product entry-point module."""

    delta_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "auto-foundry-agentic-e2e"
        / "scripts"
        / "dashboard_delta_assembler.py"
    )
    spec = importlib.util.spec_from_file_location("preview_lifecycle_dashboard_generation_product", delta_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assembler_site_binding(site_root: Path) -> dict[str, object]:
    """Use the production assembler's canonical site-tree contract."""

    module = _load_dashboard_assembler()
    return dict(module._site_tree_binding(site_root))


def _write_preview_assembler_outputs(context: RunContext, generation_id: str) -> None:
    """Write the smallest assembler-shaped output set accepted by the inspector."""

    prefix = f"products/generations/{generation_id}/preview"
    output_root = context.resolve_run_path(prefix)
    site_root = output_root / "site"
    site_root.mkdir(parents=True, exist_ok=True)
    blueprint_ref = f"{prefix}/dashboard_blueprint_v2.json"
    site_ref = f"{prefix}/site"
    receipt_ref = f"{prefix}/build_receipt.json"
    blueprint_path = context.resolve_run_path(blueprint_ref)
    blueprint_path.write_bytes(
        _json_bytes(
            {
                "schema_version": "dashboard.blueprint.v2",
                "run_id": context.run_id,
                "generation_id": generation_id,
            }
        )
    )
    blueprint_hash = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()
    (site_root / "index.html").write_text("<main>preview</main>\n", encoding="utf-8")
    (site_root / "site_manifest.json").write_bytes(
        _json_bytes(
            {
                "blueprint_ref": blueprint_ref,
                "blueprint_sha256": blueprint_hash,
            }
        )
    )
    site_binding = _assembler_site_binding(site_root)
    context.resolve_run_path(receipt_ref).write_bytes(
        _json_bytes(
            {
                "status": "complete",
                "run_id": context.run_id,
                "generation_id": generation_id,
                "new_analytics": False,
                "outputs": {
                    "blueprint_ref": blueprint_ref,
                    "site_ref": site_ref,
                    "receipt_ref": receipt_ref,
                },
                "blueprint_binding": {
                    "ref": blueprint_ref,
                    "sha256": blueprint_hash,
                },
                "site_binding": site_binding,
            }
        )
    )


def _partial_context(tmp_path: Path, item_ids: tuple[str, ...] = ("R1", "R2")) -> RunContext:
    context = RunContext("RUN-PREVIEW-LIFECYCLE", tmp_path / "run", (tmp_path,))
    RunLifecycle.create(context, item_ids, mode="requirement")
    records = tuple(_record(item_id) for item_id in item_ids)
    for item_id in item_ids:
        ItemWorkspace.create(context, item_id, mode="requirement", original_text=item_id)
    _accept_and_integrate(context, ItemWorkspace.load(context, item_ids[0], mode="requirement"))
    _save_plan(context, records)
    return context


def _partial_context_with_metric(tmp_path: Path, item_ids: tuple[str, ...]) -> RunContext:
    """Create a partial run whose committed subset has one visual metric."""

    context = RunContext("RUN-PREVIEW-LIFECYCLE-METRIC", tmp_path / "run", (tmp_path,))
    RunLifecycle.create(context, item_ids, mode="requirement")
    for item_id in item_ids:
        ItemWorkspace.create(context, item_id, mode="requirement", original_text=item_id)
    item = ItemWorkspace.load(context, item_ids[0], mode="requirement")
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
    session.add_metric(
        metric_id=f"metric-{item.item_id}",
        scope=item.item_id,
        evidence_refs=("answer_content.json",),
        label=f"Reviewed {item.item_id}",
        units="records",
        value=1,
        population=1,
    )
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    session.commit()
    _save_plan(context, tuple(_record(value) for value in item_ids))
    return context


def _seed_product_repair_boundary(context: RunContext, generation_id: str = "G-0001") -> None:
    """Persist the smallest valid candidate/review pair for Planner tests."""

    root = context.run_root / "products" / "generations" / generation_id
    root.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    for name, filename in {
        "manifest": "product_manifest.json",
        "fixture": "dashboard_fixture.json",
        "chart_map": "chart_map.json",
        "chart_registry": "chart_registry.json",
        "blueprint": "dashboard_blueprint_v2.json",
        "receipt": "build_receipt.json",
    }.items():
        path = root / filename
        path.write_text(json.dumps({"name": name}) + "\n", encoding="utf-8")
        outputs[name] = path
    site = root / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text("<html>offline</html>\n", encoding="utf-8")
    outputs["site"] = site
    plan_path = context.resolve_run_path("requirement_supervisor_plan.json")
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id=generation_id,
        product_owner="product-owner",
        parent_lineage={
            "root_generation": True,
            "parent_generation_id": None,
            "parent_manifest_ref": None,
            "parent_manifest_hash": None,
        },
        plan_binding={
            "plan_ref": "requirement_supervisor_plan.json",
            "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        publication_policy_hash=canonical_hash({"enabled": False}),
        artifact_bindings={
            name: {"ref": str(path.relative_to(context.run_root))}
            for name, path in outputs.items()
        },
    )
    store = ProductReviewStore(context, generation_id)
    store.record_candidate(candidate)
    store.record_review(
        reviewer_ref="independent-product-reviewer",
        verdict="repair_once",
        reviewed_at="2026-01-01T00:00:00Z",
    )


def test_terminal_accepted_answers_feed_preview_without_integration_eligibility(tmp_path: Path) -> None:
    """Accepted business output remains visible while integration lags/fails."""

    context = RunContext("RUN-ACCEPTED-PREVIEW", tmp_path / "run")
    item_ids = ("REQ-008", "REQ-009")
    RunLifecycle.create(context, item_ids, mode="requirement")
    failed_item = ItemWorkspace.create(context, "REQ-008", mode="requirement", original_text="accepted failure")
    _accept_only(failed_item)
    failed_session = IntegrationSession.create(
        context,
        failed_item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-REQ-008",
    )
    failed_session.finalize_technical_failure("terminal integration failure")
    pending_item = ItemWorkspace.create(context, "REQ-009", mode="requirement", original_text="accepted pending")
    _accept_only(pending_item)
    _save_plan(context, tuple(_record(item_id) for item_id in item_ids))

    planner = RequirementSupervisorWorkspace(context)
    snapshot = planner.phase_snapshot()
    product = snapshot["product"]

    assert snapshot["all_items_integrated"] is False
    assert product["preview_item_ids"] == list(item_ids)
    assert product["preview_failed_items"] == ["REQ-009"]
    assert set(product["preview_item_bindings"]) == set(item_ids)
    assert "committed_manifest_ref" not in product["preview_item_bindings"]["REQ-008"]
    assert product["preview_item_bindings"]["REQ-008"]["accepted_content_ref"].endswith(
        "/accepted/answer_content.json"
    )
    refresh = next(action for action in planner.next_actions() if action.action == "refresh_product_preview")
    assert refresh.metadata["item_ids"] == list(item_ids)
    assert refresh.metadata["failed_items"] == ["REQ-009"]


def test_reviewed_product_repair_is_reachable_before_all_integrations_finish(tmp_path: Path) -> None:
    """A repair_once review reoffers candidate rebuild with accepted IDs."""

    context = _partial_context(tmp_path, ("R1", "R2"))
    pending = ItemWorkspace.load(context, "R2", mode="requirement")
    _accept_only(pending)
    _save_plan(context, (_record("R1"), _record("R2")))
    _seed_product_repair_boundary(context)

    planner = RequirementSupervisorWorkspace(context)
    assert planner.phase_snapshot()["all_items_integrated"] is False
    repair = next(
        action
        for action in planner.next_actions()
        if action.action == "build_product_candidate"
    )
    assert repair.metadata["review_verdict"] == "repair_once"
    assert repair.metadata["item_ids"] == ["R1", "R2"]
    assert repair.metadata["item_bindings"]["R2"]["accepted_content_ref"].endswith(
        "/accepted/answer_content.json"
    )


@pytest.mark.parametrize("tamper", ("missing_manifest", "tampered_content"))
def test_reviewed_product_repair_with_invalid_accepted_boundary_is_not_partial(
    tmp_path: Path,
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate repair must not run with only a subset of accepted answers."""

    context = _partial_context(tmp_path, ("R1", "R2"))
    pending = ItemWorkspace.load(context, "R2", mode="requirement")
    _accept_only(pending)
    _save_plan(context, (_record("R1"), _record("R2")))
    _seed_product_repair_boundary(context)

    planner = RequirementSupervisorWorkspace(context)
    original_phase_snapshot = planner.phase_snapshot

    def tampered_phase_snapshot() -> dict[str, object]:
        snapshot = original_phase_snapshot()
        product = dict(snapshot["product"])
        bindings = {
            item_id: dict(binding)
            for item_id, binding in product["preview_item_bindings"].items()
        }
        if tamper == "missing_manifest":
            product["preview_item_ids"] = ["R1"]
            bindings.pop("R2", None)
        else:
            bindings["R2"]["accepted_content_hash"] = "0" * 64
        product["preview_item_bindings"] = bindings
        return {**snapshot, "product": product}

    monkeypatch.setattr(planner, "phase_snapshot", tampered_phase_snapshot)
    actions = planner.next_actions()
    assert not any(action.action == "build_product_candidate" for action in actions)
    refresh = next(action for action in actions if action.action == "refresh_product_preview")
    assert refresh.metadata["item_ids"] == (["R1"] if tamper == "missing_manifest" else ["R1", "R2"])


def test_first_integrated_item_offers_low_priority_preview_alongside_next_requirement(tmp_path: Path) -> None:
    context = _partial_context(tmp_path)
    planner = RequirementSupervisorWorkspace(context)

    actions = planner.next_actions()

    assert [(action.action, action.role, action.subject_id) for action in actions] == [
        ("analyze_requirement", "analytical_owner", "R2"),
        ("refresh_product_preview", "product_agent", context.run_id),
    ]
    assert actions[0].priority < actions[1].priority
    preview = actions[1]
    assert preview.metadata["output_dir"] == "generations/G-0001/preview"
    assert preview.metadata["presentation_inventory_ref"] == (
        "extensions/G-0001/dashboard_preflight/dashboard_fixture_v4.json"
    )
    assert preview.metadata["presentation_plan_ref"] == (
        "extensions/G-0001/business_presentation_plan.json"
    )
    assert not context.resolve_run_path(preview.metadata["presentation_inventory_ref"]).exists()
    assert not context.resolve_run_path(preview.metadata["presentation_plan_ref"]).exists()
    assert preview.metadata["item_ids"] == ["R1"]
    assert preview.metadata["input_fingerprint"] == planner.phase_snapshot()["product"]["preview_input_fingerprint"]


def test_all_terminal_without_preview_offers_final_with_preflight_refs(tmp_path: Path) -> None:
    """A direct-final run still has enough metadata to bootstrap presentation."""

    context = _partial_context(tmp_path, ("R1",))
    planner = RequirementSupervisorWorkspace(context)

    actions = planner.next_actions()

    assert not any(action.action == "refresh_product_preview" for action in actions)
    final_action = next(action for action in actions if action.action == "build_product_candidate")
    assert final_action.metadata["presentation_inventory_ref"] == (
        "extensions/G-0001/dashboard_preflight/dashboard_fixture_v4.json"
    )
    assert final_action.metadata["presentation_plan_ref"] == (
        "extensions/G-0001/business_presentation_plan.json"
    )
    assert final_action.metadata["item_ids"] == ["R1"]
    assert final_action.metadata["input_fingerprint"] == planner.phase_snapshot()["product"]["preview_input_fingerprint"]
    assert not context.resolve_run_path(final_action.metadata["presentation_inventory_ref"]).exists()
    assert not context.resolve_run_path(final_action.metadata["presentation_plan_ref"]).exists()

    guidance = coordinator_module._role_guidance(final_action).lower()
    for phrase in (
        "business_presentation_preflight",
        "presentation_inventory_ref",
        "presentation_plan_ref",
        "assemble_generation_product(context, ...)",
        "exactly one canonical dashboard generation entry point",
    ):
        assert phrase in guidance, (phrase, guidance)


def test_all_integrated_reviewed_repair_carries_candidate_hash_to_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final repair offers carry the persisted candidate hash into CAS admission."""

    context = _partial_context(tmp_path, ("R1",))
    _seed_product_repair_boundary(context)
    planner = RequirementSupervisorWorkspace(context)
    original_phase_snapshot = planner.phase_snapshot

    def valid_product_snapshot() -> dict[str, object]:
        snapshot = original_phase_snapshot()
        product = dict(snapshot["product"])
        # The helper intentionally writes minimal product output.  Keep the
        # test focused on final Planner metadata while presenting the durable
        # candidate/review boundary as otherwise valid.
        product["validation"] = {"valid": True, "stage": "validated"}
        return {**snapshot, "product": product}

    monkeypatch.setattr(planner, "phase_snapshot", valid_product_snapshot)
    snapshot = planner.phase_snapshot()
    assert snapshot["all_items_integrated"] is True
    candidate_hash = snapshot["product"]["candidate"]["candidate_hash"]
    action = next(
        action
        for action in planner.next_actions()
        if action.action == "build_product_candidate"
    )
    assert action.metadata["review_verdict"] == "repair_once"
    assert action.metadata["candidate_hash"] == candidate_hash

    coordinator = RunCoordinator(context, planner_provider=object(), role_runner=object())
    coordinator.start(
        CoordinatorRunSpec(
            context.run_id,
            "G-0001",
            "planner://final-repair",
            hashlib.sha256(b"planner").hexdigest(),
        )
    )
    registry = coordinator_module.RoleSessionRegistry(context)
    reservation_key = "final-repair-dispatch"
    reservation = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key=reservation_key,
        reservation_owner_id="dead-owner",
        reservation_pid=999_999,
        reservation_process_start="dead-start",
    )
    assert reservation["mode"] == "new"
    registry.mark_stale(
        action,
        generation_id="G-0001",
        idempotency_key=reservation_key,
        reservation_token=reservation_key,
        reason="invocation_failed",
    )
    registry.release_reservation(
        f"product_agent:{context.run_id}:G-0001",
        reservation_key,
    )

    with coordinator._locked(create=False):  # noqa: SLF001 - admission regression
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        assert coordinator._authorize_preview_replacement_for_final_locked(state, action)  # noqa: SLF001
        consumed = coordinator._consume_replacement_authorization(state, action)  # noqa: SLF001
        assert consumed is not None
        authorized_action, _binding = consumed
        assert authorized_action.metadata["allow_session_replacement"] is True

        missing_hash_metadata = dict(action.metadata)
        missing_hash_metadata.pop("candidate_hash")
        missing_hash_action = PlannerAction(
            action=action.action,
            role=action.role,
            subject_id=action.subject_id,
            reason=action.reason,
            priority=action.priority,
            metadata=missing_hash_metadata,
        )
        assert not coordinator._authorize_preview_replacement_for_final_locked(  # noqa: SLF001
            state,
            missing_hash_action,
        )
    coordinator.close(wait_for_roles=True)


def test_partial_preview_entry_persists_manifest_without_terminal_publication(tmp_path: Path) -> None:
    """The public preview entry renders a committed subset and stays non-final."""

    context = _partial_context_with_metric(tmp_path, ("R1", "R2", "R3"))
    planner = RequirementSupervisorWorkspace(context)
    preview_action = next(action for action in planner.next_actions() if action.action == "refresh_product_preview")
    generation_id = planner.phase_snapshot()["generation_id"]
    assembler = _load_dashboard_assembler()
    generation_product = _load_dashboard_generation_product()

    preflight = assembler.business_presentation_preflight(
        context,
        item_ids=preview_action.metadata["item_ids"],
        generation_id=generation_id,
    )
    candidate = next(iter(preflight["inventory"]["candidates"]), None)
    assert candidate is not None
    plan = assembler.write_business_presentation_plan(
        context,
        manager_entries=[candidate],
        reviewer_ref="tests/preview-lifecycle",
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
        item_ids=preview_action.metadata["item_ids"],
        generation_id=generation_id,
        presentation_plan_ref=preview_action.metadata["presentation_plan_ref"],
    )
    receipt = generation_product.assemble_generation_preview(
        context,
        item_ids=preview_action.metadata["item_ids"],
        presentation_plan_ref=preview_action.metadata["presentation_plan_ref"],
        output_dir=preview_action.metadata["output_dir"],
    )
    manifest = planning.persist_preview_manifest(
        context,
        generation_id,
        input_fingerprint=preview_action.metadata["input_fingerprint"],
        item_ids=preview_action.metadata["item_ids"],
        item_bindings=preview_action.metadata["item_bindings"],
        failed_items=preview_action.metadata["failed_items"],
        limitations=preview_action.metadata["limitations"],
    )

    assert receipt["generation_id"] == generation_id
    assert manifest["finalizable"] is False
    assert planning.inspect_preview_manifest(
        context,
        generation_id,
        expected_input_fingerprint=preview_action.metadata["input_fingerprint"],
    )["valid"] is True
    assert context.resolve_run_path(preview_action.metadata["preview_manifest_ref"]).is_file()
    assert not context.resolve_product_path("product_manifest.json").exists()
    assert not context.resolve_product_path(f"generations/{generation_id}/product_manifest.json").exists()

    # A new committed item changes the same-generation frontier and offers a
    # refresh for the larger subset while the third requirement remains pending.
    _accept_and_integrate(context, ItemWorkspace.load(context, "R2", mode="requirement"))
    refreshed = next(action for action in planner.next_actions() if action.action == "refresh_product_preview")
    assert refreshed.metadata["item_ids"] == ["R1", "R2"]
    assert refreshed.metadata["input_fingerprint"] != preview_action.metadata["input_fingerprint"]


def test_preview_manifest_dedupes_unchanged_inputs_and_wakes_for_new_integration(tmp_path: Path) -> None:
    context = _partial_context(tmp_path, ("R1", "R2", "R3"))
    planner = RequirementSupervisorWorkspace(context)
    before = planner.phase_snapshot()
    product = before["product"]
    generation_id = before["generation_id"]

    _write_preview_assembler_outputs(context, generation_id)
    manifest = planning.persist_preview_manifest(
        context,
        generation_id,
        input_fingerprint=product["preview_input_fingerprint"],
        item_ids=product["preview_item_ids"],
        item_bindings=product["preview_item_bindings"],
    )
    assert manifest["schema_version"] == planning.PREVIEW_MANIFEST_SCHEMA_VERSION
    receipt = json.loads(
        context.resolve_run_path(f"products/generations/{generation_id}/preview/build_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    site_binding = receipt["site_binding"]
    assert manifest["site_tree_sha256"] == site_binding["tree_sha256"]
    assert manifest["site_tree_sha256"] == planning._sha256_value(site_binding["files"])
    assert planning.inspect_preview_manifest(
        context,
        generation_id,
        expected_input_fingerprint=product["preview_input_fingerprint"],
    )["valid"] is True
    assert not any(action.action == "refresh_product_preview" for action in planner.next_actions())

    _accept_and_integrate(context, ItemWorkspace.load(context, "R2", mode="requirement"))
    after = planner.phase_snapshot()
    assert after["product"]["preview_input_fingerprint"] != product["preview_input_fingerprint"]
    refresh = next(action for action in planner.next_actions() if action.action == "refresh_product_preview")
    assert refresh.metadata["item_ids"] == ["R1", "R2"]


def test_malformed_or_exhausted_preview_is_nonblocking_and_final_flow_suppresses_refresh(tmp_path: Path) -> None:
    context = _partial_context(tmp_path)
    planner = RequirementSupervisorWorkspace(context)
    initial_preview = next(action for action in planner.next_actions() if action.action == "refresh_product_preview")
    lifecycle_state_before_preview_retry = RunLifecycle.load(context).state
    manifest_path = context.resolve_run_path(initial_preview.metadata["preview_manifest_ref"])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{}\n", encoding="utf-8")

    malformed_actions = planner.next_actions()
    assert any(action.action == "analyze_requirement" and action.subject_id == "R2" for action in malformed_actions)
    assert any(action.action == "refresh_product_preview" for action in malformed_actions)
    assert RunLifecycle.load(context).state == lifecycle_state_before_preview_retry

    exhausted_actions = planner.next_actions(
        coordinator_state={
            "retry_blocked": {
                "preview": {
                    "action": initial_preview.to_dict(),
                    "attempts": 3,
                    "requires_rethink": False,
                    "isolated_preview": True,
                }
            }
        }
    )
    assert any(action.action == "analyze_requirement" and action.subject_id == "R2" for action in exhausted_actions)
    assert not any(action.action == "refresh_product_preview" for action in exhausted_actions)

    _accept_and_integrate(context, ItemWorkspace.load(context, "R2", mode="requirement"))
    final_action = next(action for action in planner.next_actions() if action.action == "build_product_candidate")
    generation_id = RunLifecycle.load(context).generation_id
    assert coordinator_module._role_session_identity(
        initial_preview,
        run_id=context.run_id,
        generation_id=generation_id,
    ) == coordinator_module._role_session_identity(
        final_action,
        run_id=context.run_id,
        generation_id=generation_id,
    )
    assert coordinator_module.logical_action_fingerprint(
        initial_preview,
        run_id=context.run_id,
        generation_id=generation_id,
    ) != coordinator_module.logical_action_fingerprint(
        final_action,
        run_id=context.run_id,
        generation_id=generation_id,
    )
    assert not any(action.action == "refresh_product_preview" for action in planner.next_actions())


def test_preview_stale_session_gets_one_scoped_final_replacement(tmp_path: Path) -> None:
    """A failed preview must not strand the shared Product Agent at finalization."""

    context = _partial_context(tmp_path)
    planner = RequirementSupervisorWorkspace(context)
    initial = planner.next_actions()
    preview_action = next(action for action in initial if action.action == "refresh_product_preview")
    analyze_action = next(action for action in initial if action.action == "analyze_requirement")
    final_action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        context.run_id,
        "all requirements reached terminal integration boundaries",
        metadata={"generation_id": "G-0001"},
    )

    class SequencedPlanner:
        def __init__(self) -> None:
            self.calls = 0
            self.final_dispatched = False

        def next_actions(self, _context: RunContext, _state: dict[str, object]) -> tuple[PlannerAction, ...]:
            self.calls += 1
            if self.final_dispatched:
                return ()
            if self.calls == 1:
                return (preview_action,)
            if self.calls == 2:
                return (analyze_action,)
            assert planner.phase_snapshot()["all_items_integrated"] is True
            return (final_action,)

    sequenced = SequencedPlanner()
    seen: list[PlannerAction] = []
    generation_id = "G-0001"
    logical_owner = f"product_agent:{context.run_id}:{generation_id}"

    def role(current: PlannerAction, *, idempotency_key: str, context: RunContext) -> RoleExecution:
        seen.append(current)
        registry = coordinator_module.RoleSessionRegistry(context)
        if current.action == "refresh_product_preview":
            reservation = registry.prepare(current, generation_id=generation_id, idempotency_key=idempotency_key)
            registry.bind_reservation(
                current,
                generation_id=generation_id,
                idempotency_key=idempotency_key,
                reservation_token=idempotency_key,
                session_id="SID-PREVIEW",
            )
            registry.complete_reservation(
                current,
                generation_id=generation_id,
                idempotency_key=idempotency_key,
                reservation_token=idempotency_key,
                session_id="SID-PREVIEW",
            )
            registry.release_reservation(logical_owner, idempotency_key)
            registry.mark_stale(
                current,
                generation_id=generation_id,
                idempotency_key=idempotency_key,
                reason="invocation_failed",
            )
            assert reservation["mode"] == "new"
            return RoleExecution(
                exit_code=1,
                session_id="SID-PREVIEW",
                session_key=logical_owner,
                session_status="replacement_required",
            )
        if current.action == "analyze_requirement":
            _accept_and_integrate(context, ItemWorkspace.load(context, "R2", mode="requirement"))
            return RoleExecution()
        assert current.action == "build_product_candidate"
        assert current.metadata.get("allow_session_replacement") is True
        replacement = registry.prepare(
            current,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
            allow_replacement=True,
        )
        assert replacement["mode"] == "replace"
        registry.bind_reservation(
            current,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
            reservation_token=idempotency_key,
            session_id="SID-FINAL",
            replacement=True,
        )
        registry.complete_reservation(
            current,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
            reservation_token=idempotency_key,
            session_id="SID-FINAL",
            replacement=True,
        )
        registry.release_reservation(logical_owner, idempotency_key)
        sequenced.final_dispatched = True
        return RoleExecution(session_id="SID-FINAL", session_key=logical_owner, session_status="active")

    coordinator = RunCoordinator(
        context,
        planner_provider=sequenced,
        role_runner=role,
    )
    coordinator.start(
        CoordinatorRunSpec(
            context.run_id,
            generation_id,
            "planner://preview-test",
            hashlib.sha256(b"planner").hexdigest(),
        )
    )
    status = coordinator.run(max_steps=3)
    coordinator.close(wait_for_roles=True)

    assert status.status == "waiting"
    assert [action.action for action in seen] == [
        "refresh_product_preview",
        "analyze_requirement",
        "build_product_candidate",
    ]
    final_seen = seen[-1]
    assert final_seen.metadata["allow_session_replacement"] is True
    entry = coordinator_module.RoleSessionRegistry(context).get(logical_owner)
    assert entry is not None
    assert entry["status"] == "active"
    assert entry["session_id"] == "SID-FINAL"
    assert entry["replacement_of"] == "SID-PREVIEW"
    assert entry["last_action"] == "build_product_candidate"


def test_active_preview_serializes_final_candidate_until_preview_completes(tmp_path: Path) -> None:
    """An all-terminal offer waits behind an active preview owner."""

    context = _partial_context(tmp_path)
    planner = RequirementSupervisorWorkspace(context)
    initial = planner.next_actions()
    preview_action = next(action for action in initial if action.action == "refresh_product_preview")
    final_action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        context.run_id,
        "all requirements reached terminal integration boundaries",
        metadata={"generation_id": "G-0001"},
    )

    class OverlapPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def next_actions(self, _context: RunContext, _state: dict[str, object]) -> tuple[PlannerAction, ...]:
            self.calls += 1
            if self.calls == 1:
                return (preview_action,)
            if self.calls in (2, 3):
                assert planner.phase_snapshot()["all_items_integrated"] is True
                return (final_action,)
            return ()

    preview_started = threading.Event()
    release_preview = threading.Event()
    seen: list[PlannerAction] = []
    session_keys: dict[str, tuple[str, str] | None] = {}
    logical_owner = f"product_agent:{context.run_id}:G-0001"

    def role(current: PlannerAction, *, context: RunContext, **_: object) -> RoleExecution:
        seen.append(current)
        session_keys[current.action] = coordinator_module._role_session_identity(
            current,
            run_id=context.run_id,
            generation_id="G-0001",
        )
        if current.action == "refresh_product_preview":
            preview_started.set()
            assert release_preview.wait(5)
        else:
            assert current.action == "build_product_candidate"
        return RoleExecution(
            session_id="SID-SHARED",
            session_key=logical_owner,
            session_status="active",
        )

    sequenced = OverlapPlanner()
    coordinator = RunCoordinator(
        context,
        planner_provider=sequenced,
        role_runner=role,
    )
    coordinator.start(
        CoordinatorRunSpec(
            context.run_id,
            "G-0001",
            "planner://preview-overlap-test",
            hashlib.sha256(b"planner").hexdigest(),
        )
    )
    initial_status = coordinator._refresh_and_launch(set())  # noqa: SLF001 - admission regression
    assert initial_status.status == "dispatching"
    assert preview_started.wait(30)

    # Advance the second requirement while the preview transport remains in
    # flight, making the final candidate offer eligible on the next Planner
    # read without completing/cancelling the preview.
    _accept_and_integrate(context, ItemWorkspace.load(context, "R2", mode="requirement"))
    deferred = coordinator._refresh_and_launch(set())  # noqa: SLF001 - admission regression
    assert deferred.status == "dispatching"
    with coordinator._locked(create=False):  # noqa: SLF001 - inspect durable admission evidence
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        active = state["active_dispatches"]
        assert len(active) == 1
        assert active[0]["action"]["action"] == "refresh_product_preview"
        assert state["attempt"] == 1
        final_fingerprint = coordinator_module.logical_action_fingerprint(
            final_action,
            run_id=context.run_id,
            generation_id="G-0001",
        )
        assert final_fingerprint not in state["retry_counts"]
        assert final_fingerprint not in state["retry_blocked"]
        assert any(
            diagnostic.get("kind") == "dispatch_deferred"
            and any(
                deferred_action.get("reason") == "product_agent_owner_busy"
                and deferred_action.get("action", {}).get("action") == "build_product_candidate"
                for deferred_action in diagnostic.get("actions", ())
            )
            for diagnostic in deferred.diagnostics
        )

    release_preview.set()
    completed = coordinator._consume_one()  # noqa: SLF001 - consume active preview completion
    assert completed is not None
    completed_action, _execution = completed
    assert completed_action.action == "refresh_product_preview"
    preview_slot = coordinator._slot_key(completed_action)  # noqa: SLF001
    coordinator._refresh_and_launch(  # noqa: SLF001 - reconcile deferred final admission
        {preview_slot},
        completed=completed,
    )

    # The deferred final offer is admitted only after preview completion.
    final_completed = coordinator._consume_one()  # noqa: SLF001 - consume final completion
    assert final_completed is not None
    final_action_seen, final_execution = final_completed
    assert final_action_seen.action == "build_product_candidate"
    final_slot = coordinator._slot_key(final_action_seen)  # noqa: SLF001
    final_status = coordinator._refresh_and_launch(  # noqa: SLF001 - reconcile final completion
        {final_slot},
        completed=(final_action_seen, final_execution),
    )
    coordinator.close(wait_for_roles=True)

    assert final_status.status == "waiting"
    assert [action.action for action in seen] == [
        "refresh_product_preview",
        "build_product_candidate",
    ]
    assert session_keys["refresh_product_preview"] == session_keys["build_product_candidate"]
    assert session_keys["refresh_product_preview"] == (logical_owner, "product_agent")


def test_product_reservation_contention_is_deferred_without_retry(tmp_path: Path) -> None:
    """A registry in-flight race is not a Product Agent transport retry."""

    context = _partial_context(tmp_path)
    generation_id = "G-0001"
    final_action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        context.run_id,
        "all requirements reached terminal integration boundaries",
        metadata={"generation_id": generation_id},
    )

    class ContentionPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def next_actions(self, _context: RunContext, _state: dict[str, object]) -> tuple[PlannerAction, ...]:
            self.calls += 1
            return (final_action,) if self.calls < 3 else ()

    seen: list[str] = []

    def role(current: PlannerAction, **_: object) -> RoleExecution:
        seen.append(current.action)
        return RoleExecution(
            exit_code=1,
            error="shared Product Agent reservation is still in flight",
            session_key=f"product_agent:{context.run_id}:{generation_id}",
            session_status="reservation_in_flight",
        )

    coordinator = RunCoordinator(
        context,
        planner_provider=ContentionPlanner(),
        role_runner=role,
    )
    coordinator.start(
        CoordinatorRunSpec(
            context.run_id,
            generation_id,
            "planner://preview-contention-test",
            hashlib.sha256(b"planner").hexdigest(),
        )
    )
    first = coordinator.step()
    coordinator.close(wait_for_roles=True)

    assert first.status == "waiting"
    assert seen == ["build_product_candidate"]
    final_fingerprint = coordinator_module.logical_action_fingerprint(
        final_action,
        run_id=context.run_id,
        generation_id=generation_id,
    )
    with coordinator._locked(create=False):  # noqa: SLF001 - inspect retry boundary
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        assert state["retry_counts"].get(final_fingerprint) is None
        assert final_fingerprint not in state["retry_blocked"]
        assert any(
            diagnostic.get("kind") == "dispatch_deferred"
            and any(
                deferred_action.get("reason") == "role_session_reservation_in_flight"
                for deferred_action in diagnostic.get("actions", ())
            )
            for diagnostic in state["diagnostics"]
        )
