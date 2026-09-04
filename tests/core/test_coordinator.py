from __future__ import annotations

import hashlib
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Mapping
import zipfile

import pytest

from auto_foundry_core import (
    CoordinatorConflictError,
    CoordinatorIntegrityError,
    CoordinatorProductionBindingMismatch,
    CoordinatorRunSpec,
    DataRevisionStore,
    EntityResolutionWorkspace,
    ItemWorkspace,
    PlannerAction,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementRunExtension,
    RequirementSupervisorWorkspace,
    ResolutionCapacity,
    RoleExecution,
    RunContext,
    RunLifecycle,
    RunCoordinator,
)
import auto_foundry_core.coordinator as coordinator_module
import auto_foundry_core.run_extension as run_extension_module
from auto_foundry_core.product_review import ProductCandidate, ProductReviewStore, canonical_hash
from tests.core.test_data_refresh_generation import _fixture as _base_refresh_fixture


def _refresh_fixture(*args: Any, **kwargs: Any) -> tuple[RunContext, RequirementExecutionPlan, object]:
    """Build the refresh fixture with the valid G1 product parent required by D admission."""

    context, plan, revision = _base_refresh_fixture(*args, **kwargs)
    root = context.run_root
    dashboard_root = root / "products" / "dashboard"
    dashboard_root.mkdir(parents=True, exist_ok=True)
    fixture_path = dashboard_root / "dashboard_fixture.json"
    chart_map_path = dashboard_root / "chart_map.json"
    chart_registry_path = dashboard_root / "chart_registry.json"
    blueprint_path = dashboard_root / "dashboard_blueprint_v2.json"
    for path in (fixture_path, chart_map_path, chart_registry_path):
        path.write_bytes(b"{}\n")
    blueprint_path.write_bytes(
        json.dumps(
            {"schema_version": "dashboard.business_presentation_plan.v2", "kind": "dashboard_blueprint"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    site_path = dashboard_root / "site"
    site_path.mkdir(exist_ok=True)
    site_manifest_path = site_path / "site_manifest.json"
    site_manifest_path.write_bytes(b"{}\n")
    plan_path = root / "requirement_supervisor_plan.json"
    plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    receipt_ref = "products/dashboard/build_receipt.json"
    receipt_path = root / receipt_ref
    receipt = {
        "schema_version": "1",
        "status": "complete",
        "run_id": context.run_id,
        "generation_id": "G-0001",
        "new_analytics": False,
        "parent": {
            "root_generation": True,
            "parent_generation_id": None,
            "parent_manifest_ref": None,
            "parent_manifest_hash": None,
        },
        "plan_binding": {
            "ref": "requirement_supervisor_plan.json",
            "sha256": plan_hash,
            "admission_sha256": plan_hash,
            "generation_id": "G-0001",
        },
        "outputs": {
            "fixture_ref": str(fixture_path.relative_to(root)),
            "chart_map_ref": str(chart_map_path.relative_to(root)),
            "chart_registry_ref": str(chart_registry_path.relative_to(root)),
            "blueprint_ref": str(blueprint_path.relative_to(root)),
            "site_ref": str(site_path.relative_to(root)),
            "receipt_ref": receipt_ref,
        },
        "output_hashes": {
            "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            "chart_map_sha256": hashlib.sha256(chart_map_path.read_bytes()).hexdigest(),
            "chart_registry_sha256": hashlib.sha256(chart_registry_path.read_bytes()).hexdigest(),
            "blueprint_sha256": hashlib.sha256(blueprint_path.read_bytes()).hexdigest(),
            "site_manifest_sha256": hashlib.sha256(site_manifest_path.read_bytes()).hexdigest(),
        },
        "blueprint_binding": {
            "ref": str(blueprint_path.relative_to(root)),
            "sha256": hashlib.sha256(blueprint_path.read_bytes()).hexdigest(),
            "schema_version": "dashboard.business_presentation_plan.v2",
            "status": "Preview",
        },
    }
    receipt_path.write_bytes(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    manifest = {
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "run_id": context.run_id,
        "status": "complete",
        "terminal": True,
        "source_status": "reviewed_outputs_only",
        "new_analytics": False,
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "lifecycle": {"generation_id": "G-0001"},
        "dashboard": {
            "receipt_ref": receipt_ref,
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        },
        "lineage": {"root_generation": True},
    }
    (root / "products" / "product_manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    return context, plan, revision


def _write_valid_extension_product(context: RunContext, generation_id: str) -> None:
    """Fixture one published generation product for the successor-parent gate."""

    lifecycle = RunLifecycle.load(context)
    metadata = lifecycle.generation_metadata
    assert metadata is not None and metadata.generation_id == generation_id
    root = context.run_root
    product_root = root / "products" / "generations" / generation_id / "dashboard"
    product_root.mkdir(parents=True, exist_ok=True)
    fixture_path = product_root / "dashboard_fixture.json"
    chart_map_path = product_root / "chart_map.json"
    chart_registry_path = product_root / "chart_registry.json"
    blueprint_path = product_root / "dashboard_blueprint_v2.json"
    for path in (fixture_path, chart_map_path, chart_registry_path):
        path.write_bytes(b"{}\n")
    blueprint_path.write_bytes(
        json.dumps(
            {"schema_version": "dashboard.business_presentation_plan.v2", "kind": "dashboard_blueprint"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    site_path = product_root / "site"
    site_path.mkdir(exist_ok=True)
    site_manifest_path = site_path / "site_manifest.json"
    site_manifest_path.write_bytes(b"{}\n")
    presentation_ref = f"extensions/{generation_id}/business_presentation_plan.json"
    presentation_path = root / presentation_ref
    presentation_path.parent.mkdir(parents=True, exist_ok=True)
    presentation_path.write_bytes(b"{}\n")
    plan_ref = Path(metadata.plan_path).relative_to(root).as_posix()
    plan_hash = hashlib.sha256(Path(metadata.plan_path).read_bytes()).hexdigest()
    parent_generation_id = metadata.parent_generation_id
    parent_ref = (
        "products/product_manifest.json"
        if parent_generation_id == "G-0001"
        else f"products/generations/{parent_generation_id}/product_manifest.json"
    )
    parent_path = root / parent_ref
    parent_hash = hashlib.sha256(parent_path.read_bytes()).hexdigest()
    receipt_ref = f"products/generations/{generation_id}/dashboard/build_receipt.json"
    receipt_path = root / receipt_ref
    receipt = {
        "schema_version": "1",
        "status": "complete",
        "run_id": context.run_id,
        "generation_id": generation_id,
        "parent_generation_id": parent_generation_id,
        "new_analytics": False,
        "parent": {
            "product_manifest_ref": parent_ref,
            "product_manifest_sha256": parent_hash,
        },
        "plan_binding": {
            "ref": plan_ref,
            "sha256": plan_hash,
            "admission_sha256": plan_hash,
            "generation_id": generation_id,
        },
        "presentation_plan_ref": presentation_ref,
        "presentation_plan_sha256": hashlib.sha256(presentation_path.read_bytes()).hexdigest(),
        "outputs": {
            "fixture_ref": str(fixture_path.relative_to(root)),
            "chart_map_ref": str(chart_map_path.relative_to(root)),
            "chart_registry_ref": str(chart_registry_path.relative_to(root)),
            "blueprint_ref": str(blueprint_path.relative_to(root)),
            "site_ref": str(site_path.relative_to(root)),
            "receipt_ref": receipt_ref,
        },
        "output_hashes": {
            "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            "chart_map_sha256": hashlib.sha256(chart_map_path.read_bytes()).hexdigest(),
            "chart_registry_sha256": hashlib.sha256(chart_registry_path.read_bytes()).hexdigest(),
            "blueprint_sha256": hashlib.sha256(blueprint_path.read_bytes()).hexdigest(),
            "site_manifest_sha256": hashlib.sha256(site_manifest_path.read_bytes()).hexdigest(),
        },
        "blueprint_binding": {
            "ref": str(blueprint_path.relative_to(root)),
            "sha256": hashlib.sha256(blueprint_path.read_bytes()).hexdigest(),
            "schema_version": "dashboard.business_presentation_plan.v2",
            "status": "Preview",
        },
    }
    receipt_path.write_bytes(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    manifest = {
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "run_id": context.run_id,
        "status": "complete",
        "terminal": True,
        "source_status": "reviewed_outputs_only",
        "new_analytics": False,
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "lifecycle": {"generation_id": generation_id},
        "dashboard": {
            "receipt_ref": receipt_ref,
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        },
        "lineage": {
            "parent_generation_id": parent_generation_id,
            "parent_product_manifest_ref": parent_ref,
            "parent_product_manifest_sha256": parent_hash,
            "generation_manifest_hash": metadata.manifest_hash,
            "active_plan_hash": metadata.plan_hash,
            "delta_receipt_ref": receipt_ref,
        },
        "presentation_plan_ref": presentation_ref,
        "presentation_plan_sha256": hashlib.sha256(presentation_path.read_bytes()).hexdigest(),
    }
    manifest_path = root / "products" / "generations" / generation_id / "product_manifest.json"
    manifest_path.write_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n")


class _InputSink:
    def __init__(self, capture: dict[str, object]) -> None:
        self.capture = capture

    def write(self, value: bytes) -> int:
        self.capture["input"] = value.decode("utf-8")
        return len(value)

    def flush(self) -> None: pass
    def close(self) -> None: pass


class _FakeCodexProcess:
    def __init__(self, capture: dict[str, object]) -> None:
        self.stdin = _InputSink(capture)
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.returncode = 0

    def wait(self, timeout=None): return self.returncode
    def terminate(self): self.returncode = -15
    def kill(self): self.returncode = -9


def _spec(run_id: str = "RUN-COORD") -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
    )


def _seed_product_repair_candidate(context: RunContext) -> tuple[ProductCandidate, object]:
    """Persist a valid repair_once candidate/review for coordinator admission tests."""

    plan_path = context.run_root / "requirement_supervisor_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text('{"schema_version":1}\n', encoding="utf-8")
    product_root = context.run_root / "products" / "generations" / "G-0001"
    product_root.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    for name, filename in {
        "manifest": "product_manifest.json",
        "fixture": "dashboard_fixture.json",
        "chart_map": "chart_map.json",
        "chart_registry": "chart_registry.json",
        "blueprint": "dashboard_blueprint_v2.json",
        "receipt": "build_receipt.json",
    }.items():
        path = product_root / filename
        path.write_text(json.dumps({"name": name}) + "\n", encoding="utf-8")
        outputs[name] = path
    site = product_root / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text("<html>offline</html>\n", encoding="utf-8")
    outputs["site"] = site
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
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
    store = ProductReviewStore(context, "G-0001")
    persisted = store.record_candidate(candidate)
    review = store.record_review(
        reviewer_ref="independent-product-reviewer",
        verdict="repair_once",
        reviewed_at="2026-01-01T00:00:00Z",
    )
    return persisted, review


def _rebound_spec(run_id: str, generation_id: str, planner_ref: str) -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        generation_id,
        planner_ref,
        hashlib.sha256(planner_ref.encode("utf-8")).hexdigest(),
    )


def _bound_spec(run_id: str, binding: Mapping[str, str]) -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        # The public migration is binding-only: an old unbound spec must not
        # gain a new binary/sandbox/model setting as a side effect.
        codex_exec=dict(binding),
    )


def _install_test_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    code_home = tmp_path / "codex-home"
    skill_path = code_home / "skills" / coordinator_module.PRODUCTION_SKILL_NAME
    _write_synthetic_skill(
        skill_path,
        skill_version=coordinator_module.PRODUCTION_SKILL_VERSION,
        core_version=coordinator_module.PRODUCTION_CORE_VERSION,
        release=coordinator_module.PRODUCTION_RELEASE,
    )
    expected_hash = hashlib.sha256(coordinator_module._skill_release_bytes(skill_path)).hexdigest()
    monkeypatch.setattr(coordinator_module, "PRODUCTION_SKILL_SHA256", expected_hash)
    monkeypatch.setenv("CODEX_HOME", str(code_home))
    return skill_path


def _write_synthetic_skill(
    skill_path: Path,
    *,
    skill_version: str,
    core_version: str,
    release: str,
) -> None:
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "SKILL.md").write_text(
        "---\n"
        f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n"
        "description: synthetic test fixture\n"
        "metadata:\n"
        f"  version: \"{skill_version}\"\n"
        "  core_name: auto_foundry_core\n"
        f"  core_version: \"{core_version}\"\n"
        f"  release: {release}\n"
        "---\n\n"
        f"skill_name: {coordinator_module.PRODUCTION_SKILL_NAME}\n"
        f"skill_version: {skill_version}\n"
        "core_name: auto_foundry_core\n"
        f"core_version: {core_version}\n",
        encoding="utf-8",
    )
    (skill_path / "README.md").write_text("synthetic release fixture\n", encoding="utf-8")


class QueuePlanner:
    def __init__(self, *offers: tuple[PlannerAction, ...]):
        self.offers = list(offers)
        self.calls = 0

    def next_actions(self, context: RunContext, state: dict) -> tuple[PlannerAction, ...]:
        self.calls += 1
        return self.offers.pop(0) if self.offers else ()


def _seed_active_dispatch(
    coordinator: RunCoordinator,
    action: PlannerAction,
    *,
    runner_pid: int | None = None,
    runner_process_start: str | None = None,
) -> str:
    with coordinator._locked(create=False):
        state, _ = coordinator._read_replay()
        assert state is not None
        key = coordinator._idempotency_key(state, action)
        entry = {
            "action": action.to_dict(),
            "idempotency_key": key,
            "slot_key": coordinator._slot_key(action),
        }
        if runner_pid is not None:
            entry.update({"runner_id": "dead-owner", "runner_pid": runner_pid})
        if runner_process_start is not None:
            entry["runner_process_start"] = runner_process_start
        state["active_dispatches"] = [entry]
        state["status"] = "dispatching"
        state["phase"] = action.action
        coordinator._append_event_locked(
            state,
            "dispatch_started",
            {"action": action.to_dict(), "idempotency_key": key},
        )
        return key


def _claim_process_worker(
    run_root: str,
    run_id: str,
    owner_id: str,
    action: PlannerAction,
    barrier: Any,
    release: Any,
    marker_path: str,
) -> None:
    context = RunContext(run_id, Path(run_root))

    class Planner:
        def next_actions(self, context: RunContext, state: dict) -> tuple[PlannerAction, ...]:
            return (action,)

    def role(current: PlannerAction, *, idempotency_key: str, **_: object) -> RoleExecution:
        with Path(marker_path).open("a", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()}:{idempotency_key}\n")
            stream.flush()
        release.wait(5)
        return RoleExecution()

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={action.action: role},
        owner_id=owner_id,
    )
    barrier.wait(5)
    coordinator.step()
    coordinator.close(wait_for_roles=True)


def _role_session_registry_race_worker(
    run_root: str,
    run_id: str,
    token: str,
    barrier: Any,
    release: Any,
    result_queue: Any,
) -> None:
    """Race two real processes through one durable role-session reservation."""

    context = RunContext(run_id, Path(run_root))
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-RACE", "race")
    barrier.wait(5)
    reservation: Mapping[str, Any] | None = None
    try:
        reservation = registry.prepare(
            action,
            generation_id="G-0001",
            idempotency_key=token,
            reservation_owner_id=f"race-owner-{os.getpid()}",
            reservation_pid=os.getpid(),
            reservation_process_start=coordinator_module._process_start_token(os.getpid()),
        )
        result_queue.put({"pid": os.getpid(), "mode": reservation.get("mode"), "status": reservation.get("status")})
        release.wait(5)
    except Exception as exc:
        result_queue.put({"pid": os.getpid(), "error": str(exc)})
    finally:
        if isinstance(reservation, Mapping):
            owner = reservation.get("logical_owner")
            token_value = reservation.get("reservation_token")
            if isinstance(owner, str) and isinstance(token_value, str):
                registry.release_reservation(owner, token_value)


def _rebind_process_worker(
    run_root: str,
    run_id: str,
    generation_id: str,
    planner_ref: str,
    barrier: Any,
    result_path: str,
) -> None:
    context = RunContext(run_id, Path(run_root))
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), owner_id=generation_id)
    barrier.wait(5)
    try:
        status = coordinator.publish_and_rebind(
            _rebound_spec(run_id, generation_id, planner_ref),
            lambda _spec: None,
        )
        Path(result_path).write_text(f"ok:{status.status}", encoding="utf-8")
    except CoordinatorConflictError as exc:
        Path(result_path).write_text(f"conflict:{exc}", encoding="utf-8")


def _reconciliation_race_worker(
    run_root: str,
    run_id: str,
    owner_id: str,
    offered: PlannerAction,
    hold_planner_read: bool,
    planner_read: Any,
    release_planner_read: Any,
    started: Any,
    finished: Any,
    release_role: Any,
    marker_path: str,
) -> None:
    context = RunContext(run_id, Path(run_root))

    class Planner:
        def next_actions(self, context: RunContext, state: dict) -> tuple[PlannerAction, ...]:
            if hold_planner_read:
                planner_read.set()
                assert release_planner_read.wait(5)
            return (offered,)

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        Path(marker_path).write_text(action.subject_id, encoding="utf-8")
        if hold_planner_read:
            assert release_role.wait(5)
        return RoleExecution()

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={offered.action: role},
        owner_id=owner_id,
    )
    started.set()
    try:
        coordinator.step()
    finally:
        coordinator.close(wait_for_roles=True)
        finished.set()


def _action(name: str = "analyze_requirement", subject: str = "REQ-1") -> PlannerAction:
    roles = {
        "resolve_identity": "entity_resolution_owner",
        "resume_identity_resolution": "entity_resolution_owner",
        "repair_identity_result": "entity_resolution_owner",
        "review_identity_result": "identity_reviewer",
        "commit_identity_result": "identity_reviewer",
        "review_requirement": "business_reviewer",
        "finalize_requirement_review": "business_reviewer",
        "integrate_requirement": "integration_agent",
        "repair_integration_fidelity": "integration_agent",
        "commit_integration_requirement": "integration_agent",
        "review_integration_fidelity": "integration_fidelity_reviewer",
        "build_product_candidate": "product_agent",
        "build_final_product": "product_agent",
        "publish_final_product": "product_agent",
        "review_final_product": "product_reviewer",
        "recover_final_report": "reporting_agent",
        "preflight_final_report": "reporting_agent",
        "finalize_final_report": "reporting_agent",
        "repair_run_lifecycle": "planner",
        "repair_identity_request": "planner",
        "escalate_identity_failure": "planner",
    }
    return PlannerAction(name, roles.get(name, "analytical_owner"), subject, "test action")


def test_role_guidance_finalization_precedes_business_reviewer() -> None:
    action = PlannerAction("finalize_requirement_review", "business_reviewer", "REQ-1", "finalize")

    guidance = coordinator_module._role_guidance(action)

    assert guidance.startswith("Finalization sequence:")
    assert "Business reviewer sequence" not in guidance


def test_role_guidance_build_product_candidate_uses_public_refs_sequence() -> None:
    guidance = coordinator_module._role_guidance(_action("build_product_candidate", "PRODUCT"))
    names = ["ProductWorkspace(context, action)", "workspace.feedback()", "workspace.inventory(offset=...)", "workspace.detail(widget_id)", "workspace.build(choices, presentation={...})"]
    assert [guidance.index(name) for name in names] == sorted(guidance.index(name) for name in names)
    assert "Never calculate these by hand" in guidance
    assert "never accept your own product" in guidance



def test_role_guidance_build_product_candidate_is_direct_and_history_bounded() -> None:
    guidance = coordinator_module._role_guidance(_action("build_product_candidate", "PRODUCT"))
    for phrase in ("Do not call index_repository", "recursive run-root searches", "~/.codex/sessions/history", "idempotent re-entry", "Use public metadata"):
        assert phrase in guidance



def test_role_guidance_product_uses_generation_aware_assembler_entry_point() -> None:
    for name in ("build_product_candidate", "build_final_product", "refresh_product_preview"):
        guidance = coordinator_module._role_guidance(PlannerAction(name,"product_agent","PRODUCT","build"))
        assert "ProductWorkspace(context, action)" in guidance
        assert "generation routes, immutable revision paths" in guidance
        assert "workspace.build" in guidance
        assert "expected_current_plan_sha256" not in guidance
    publish = coordinator_module._role_guidance(_action("publish_final_product", "PRODUCT"))
    assert "not a new build" in publish and "independently reviewed" in publish



def test_role_guidance_product_composes_views_from_all_admissible_facts() -> None:
    guidance = coordinator_module._role_guidance(_action("build_product_candidate", "PRODUCT")).lower()
    for phrase in ("every accepted answer visual and committed candidate visual fact", "decision surface or explicit limitation", "do not invent metrics or run new analytics", "semantic defect", "denominator", "pie", "histogram", "scatter", "audit"):
        assert phrase in guidance



def test_role_guidance_preview_uses_preflight_inventory_and_single_assembly() -> None:
    guidance = coordinator_module._role_guidance(PlannerAction("refresh_product_preview", "product_agent", "PRODUCT", "preview"))
    assert "ProductWorkspace(context, action)" in guidance
    assert "workspace.build(choices, presentation={...}) once" in guidance
    assert "preview manifest and candidate registration" in guidance
    assert "never accept your own product" in guidance



def test_role_guidance_product_reviewer_checks_accepted_coverage_and_limits() -> None:
    guidance = coordinator_module._role_guidance(
        PlannerAction("review_final_product", "product_reviewer", "PRODUCT", "review")
    ).lower()

    for phrase in (
        "every accepted requirement has a meaningful decision surface or an explicit business limitation",
        "including accepted visuals whose integration projection is terminally failed",
        "integration success is not a presentation prerequisite",
        "execution traces, files/inventory, pipeline/source-process diagnostics",
        "no raw failure reasons or internal absolute paths",
    ):
        assert phrase in guidance


def test_role_guidance_product_quality_is_business_first_and_rendered() -> None:
    product_guidance = coordinator_module._role_guidance(
        PlannerAction("build_product_candidate", "product_agent", "PRODUCT", "build")
    ).lower()
    reviewer_guidance = coordinator_module._role_guidance(
        PlannerAction("review_final_product", "product_reviewer", "PRODUCT", "review")
    ).lower()

    for phrase in (
        "one semantic representative per requirement/business metric/scope",
        "prefer substantive accepted-evidence surfaces over unavailable or unbound placeholders",
        "populate the executive overview from the explicit manager selection",
        "product agent itself chooses only decision-useful business information",
        "preserve the selected business meaning and exact rows/columns/values",
        "no assembler or renderer code may reinterpret the plan",
    ):
        assert phrase in product_guidance
    for phrase in (
        "inspect the assembled candidate's receipt",
        "verify each manager visual is decision-useful",
        "explicit plan membership is honored without semantic re-evaluation",
        "chart count/type/layout remain product agent design decisions",
        "contains no raw failure reasons or internal absolute paths",
    ):
        assert phrase in reviewer_guidance


def test_role_prompt_forbids_nested_agents_and_source_edits(tmp_path: Path) -> None:
    prompt = coordinator_module.build_role_prompt(
        _action(),
        context=RunContext("RUN-COORD", tmp_path),
        idempotency_key="key",
    )

    assert "Do not spawn or delegate subagents." in prompt
    assert "Do not edit repository source, tests, or configuration files" in prompt
    assert "make all run changes through public APIs" in prompt


@pytest.mark.parametrize("action_name", ["resolve_identity", "resume_identity_resolution"])
def test_entity_resolution_owner_custom_prompt_keeps_mandatory_contract_last(
    tmp_path: Path,
    action_name: str,
) -> None:
    action = PlannerAction(action_name, "entity_resolution_owner", "domain-1", "resolve")
    custom = "CUSTOM OWNER CONTEXT"
    prompt = coordinator_module.build_role_prompt(
        action,
        context=RunContext("RUN-COORD", tmp_path),
        idempotency_key="key",
        custom=custom,
    )
    mandatory = "Entity Resolution Owner sequence:"
    assert prompt.index(custom) < prompt.index(mandatory)
    assert "current_scope" in prompt
    assert "record_scope_discovery" in prompt
    assert "expected_scope_hash=current_scope.scope_hash" in prompt
    assert "stale scope" in prompt


def test_role_guidance_reporting_finalization_precedes_role_fallback() -> None:
    action = PlannerAction("finalize_final_report", "reporting_agent", "REPORT", "finalize")

    guidance = coordinator_module._role_guidance(action)

    assert guidance.startswith("Reporting finalization sequence:")
    assert "RunReportInputGatherer.gather_from_run(context, persist=True)" in guidance
    assert "load the persisted preflight" in guidance
    assert "RunReportFinalizer.finalize" in guidance
    assert "Do not author timings, incidents, reviews, or implementation hashes" in guidance


def test_role_guidance_reporting_recovery_uses_persisted_preflight() -> None:
    action = PlannerAction("recover_final_report", "reporting_agent", "REPORT", "recover")

    guidance = coordinator_module._role_guidance(action)

    recover = guidance.index("RunReportFinalizer.recover()")
    gather = guidance.index("RunReportInputGatherer.gather_from_run(context, persist=True)")
    load = guidance.index("load the persisted preflight")
    assert guidance.startswith("Reporting preflight/recovery sequence:")
    assert recover < gather < load
    assert "agent-authored values" in guidance


def test_codex_role_adapter_appends_reporting_contract_to_custom_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeCodexProcess:
        seen["argv"] = argv
        return _FakeCodexProcess(seen)

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", fake_popen)
    context = RunContext("RUN-COORD", tmp_path)
    action = PlannerAction("preflight_final_report", "reporting_agent", "REPORT", "preflight")
    custom = "Persisted reporting context only."
    execution = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(
            binary="fake-codex",
            role_prompts={"reporting_agent": custom},
        ),
    )(action, idempotency_key="key", context=context)

    assert execution.ok
    prompt = str(seen["input"])
    assert custom in prompt
    gather = prompt.index("RunReportInputGatherer.gather_from_run(context, persist=True)")
    load = prompt.index("load the persisted preflight")
    assert prompt.index(custom) < gather < load


def test_complete_role_model_contract_routes_all_current_roles() -> None:
    routes = coordinator_module.production_role_routing()
    assert routes["intake_planner"] == {"model": "gpt-5.6-sol", "reasoning_effort": "high"}
    assert routes["foundry_supervisor"] == {"model": "gpt-5.6-sol", "reasoning_effort": "high"}
    assert routes["analytical_owner"] == {"model": "gpt-5.6-sol", "reasoning_effort": "high"}
    assert routes["business_reviewer"] == {"model": "gpt-5.6-sol", "reasoning_effort": "high"}
    assert routes["entity_resolution_owner"] == {"model": "gpt-5.6-luna", "reasoning_effort": "max"}
    assert routes["specialist"] == {"model": "gpt-5.6-luna", "reasoning_effort": "max"}
    selected = coordinator_module.CodexExecConfig(binary="fake-codex").for_role("reporting_agent")
    assert selected.model == "gpt-5.6-luna"
    assert selected.reasoning_effort == "max"


def test_generic_custom_prompt_always_keeps_mandatory_guidance(tmp_path: Path) -> None:
    action = PlannerAction("review_requirement", "business_reviewer", "REQ-001", "review")
    prompt = coordinator_module.build_role_prompt(
        action,
        context=RunContext("RUN-CUSTOM-CONTRACT", tmp_path),
        idempotency_key="key",
        custom="Use the persisted item review packet as context.",
    )
    assert prompt.index("Use the persisted item review packet as context.") < prompt.index("Business reviewer sequence:")
    assert prompt.count("Business reviewer sequence:") == 1


def test_planner_control_offer_is_deterministic_and_never_dispatched(tmp_path: Path) -> None:
    action = PlannerAction(
        "repair_identity_request",
        "planner",
        "RUN-CONTROL",
        "identity proposal conflict",
        metadata={"requires_rethink": True},
    )
    calls: list[str] = []

    def forbidden_transport(current: PlannerAction, **_: object) -> RoleExecution:
        calls.append(current.action)
        raise AssertionError("planner control must not reach a role transport")

    context = RunContext("RUN-CONTROL", tmp_path)
    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={"repair_identity_request": forbidden_transport},
    )
    coordinator.start(_spec(context.run_id))
    status = coordinator.step()
    assert calls == []
    assert status.status == "blocked_rethink"
    assert status.phase == "rethink"
    assert status.next_action == action.to_dict()
    assert any(item.get("kind") == "coordinator_control" for item in status.diagnostics)


def test_rethink_metadata_does_not_turn_executable_action_into_control(tmp_path: Path) -> None:
    action = PlannerAction(
        "analyze_requirement",
        "analytical_owner",
        "REQ-EXECUTABLE",
        "repair evidence",
        metadata={"requires_rethink": True},
    )
    calls: list[str] = []

    def transport(current: PlannerAction, **_: object) -> RoleExecution:
        calls.append(current.action)
        return RoleExecution()

    context = RunContext("RUN-TYPED-CONTROL", tmp_path)
    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={action.action: transport},
    )
    coordinator.start(_spec(context.run_id))
    status = coordinator.step()
    coordinator.close(wait_for_roles=True)
    assert coordinator_module._is_control_action(action) is False
    assert calls == ["analyze_requirement"]
    assert status.status in {"dispatching", "waiting"}


def test_persisted_resolution_capacity_gates_total_and_role_subcaps(tmp_path: Path) -> None:
    context = RunContext("RUN-CAPACITY", tmp_path)
    EntityResolutionWorkspace.create(
        context,
        capacity=ResolutionCapacity(total_active=2, entity_resolution=1, analytical_owner=1, specialist=1),
    )
    resolver = PlannerAction("resolve_identity", "entity_resolution_owner", "DOMAIN-1", "resolve")
    owner = PlannerAction("analyze_requirement", "analytical_owner", "REQ-1", "analyze")
    product = PlannerAction("build_product_candidate", "product_agent", "RUN-CAPACITY", "build")
    entered: list[str] = []
    release = threading.Event()
    started = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        entered.append(action.action)
        started.set()
        release.wait(2)
        return RoleExecution()

    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((resolver, owner, product)),
        adapters={"resolve_identity": role, "analyze_requirement": role, "build_product_candidate": role},
    )
    coordinator.start(_spec(context.run_id))
    thread = threading.Thread(target=coordinator.step)
    thread.start()
    assert started.wait(2)
    deadline = time.time() + 2
    while len(entered) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert sorted(entered[:2]) == ["analyze_requirement", "resolve_identity"]
    assert "build_product_candidate" not in entered[:2]
    release.set()
    thread.join(timeout=3)
    coordinator.close(wait_for_roles=True)


def test_retry_budget_survives_coordinator_restart(tmp_path: Path) -> None:
    action = _action()
    context = RunContext("RUN-DURABLE-RETRY", tmp_path)
    first = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,), (action,), (action,), (action,)),
        adapters={"analyze_requirement": lambda action, **_: RoleExecution(output="unchanged")},
    )
    first.start(_spec(context.run_id))
    first.run(max_steps=5)
    first.close(wait_for_roles=True)
    persisted = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert persisted["retry_blocked"]

    calls: list[str] = []

    def should_not_run(current: PlannerAction, **_: object) -> RoleExecution:
        calls.append(current.action)
        return RoleExecution()

    restarted = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={"analyze_requirement": should_not_run},
    )
    status = restarted.step()
    assert calls == []
    assert status.status == "waiting"
    assert status.next_action == action.to_dict()
    assert any(item.get("kind") == "retry_budget_exhausted" for item in status.diagnostics)
    restarted.close(wait_for_roles=True)


def test_codex_role_adapter_binds_production_skill_and_rejects_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = _install_test_skill(tmp_path, monkeypatch)
    binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    context = RunContext("RUN-COORD", tmp_path)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    calls: list[dict[str, object]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeCodexProcess:
        capture: dict[str, object] = {"argv": argv, "kwargs": kwargs}
        calls.append(capture)
        return _FakeCodexProcess(capture)

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", fake_popen)
    adapter = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(binary="fake-codex", **binding),
        require_skill_binding=True,
    )
    execution = adapter(action, idempotency_key="key", context=context)
    assert execution.ok
    assert calls
    prompt = str(calls[0]["input"])
    assert binding["skill_path"] in prompt
    assert binding["skill_version"] in prompt
    assert binding["core_version"] in prompt
    assert binding["skill_sha256"] in prompt

    calls.clear()
    skill_file = Path(binding["skill_path"]) / "SKILL.md"
    skill_file.write_text("---\nname: stale\n---\n", encoding="utf-8")
    rejected = adapter(action, idempotency_key="key-2", context=context)
    assert rejected.exit_code == 1
    assert "skill binding rejected" in rejected.error
    assert not calls


def test_role_session_reservation_is_single_owner_and_session_id_is_cas_bound(tmp_path: Path) -> None:
    context = RunContext("RUN-ROLE-RESERVATION", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")

    first = registry.prepare(action, generation_id="G-0001", idempotency_key="dispatch-1")
    second = registry.prepare(action, generation_id="G-0001", idempotency_key="dispatch-2")
    assert first["mode"] == "new"
    assert second["mode"] == "blocked"
    assert second["reservation_status"] == "reserved"
    assert registry.get("analytical_owner:REQ-001")["status"] == "reserved"

    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-1",
        reservation_token="dispatch-1",
        session_id="SID-ONE",
    )
    with pytest.raises(CoordinatorConflictError, match="compare-and-set"):
        registry.bind_reservation(
            action,
            generation_id="G-0001",
            idempotency_key="dispatch-1",
            reservation_token="dispatch-1",
            session_id="SID-TWO",
        )
    assert registry.get("analytical_owner:REQ-001")["session_id"] == "SID-ONE"
    registry.complete_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-1",
        reservation_token="dispatch-1",
        session_id="SID-ONE",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-1")

    resumed = registry.prepare(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        generation_id="G-0001",
        idempotency_key="dispatch-3",
    )
    assert resumed["mode"] == "resume"
    assert resumed["session_id"] == "SID-ONE"
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-3")


def test_role_session_process_start_identity_uses_required_portable_provider_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS-style hosts must use the required psutil provider and fail closed."""

    class NoProcPath:
        def __init__(self, _: str) -> None:
            pass

        def is_file(self) -> bool:
            return False

        def is_symlink(self) -> bool:
            return False

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return 1234.5

    class ProcessProvider:
        pass

    ProcessProvider.Process = Process

    monkeypatch.setattr(coordinator_module, "Path", NoProcPath)
    monkeypatch.setattr(coordinator_module, "_load_psutil", lambda: ProcessProvider)
    assert coordinator_module._process_start_token(4242) == "psutil:1234.500000"

    monkeypatch.setattr(coordinator_module, "_load_psutil", lambda: None)
    assert coordinator_module._process_start_token(4242) is None
    with pytest.raises(CoordinatorIntegrityError, match="exact process start identity"):
        coordinator_module._required_process_start_token(4242)


def test_role_session_adapter_fails_closed_without_exact_process_start_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-ROLE-IDENTITY-UNKNOWN", tmp_path)
    context.resolve_run_path("control_plane").mkdir(parents=True, exist_ok=True)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    calls: list[object] = []

    monkeypatch.setattr(coordinator_module, "_process_start_token", lambda _pid=None: None)
    monkeypatch.setattr(
        coordinator_module.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    adapter = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(binary="fake-codex"),
    )

    execution = adapter(action, idempotency_key="dispatch-identity", context=context)

    assert execution.exit_code == 1
    assert execution.session_status == "replacement_required"
    assert "role session registry is unavailable" in execution.error
    assert calls == []


def test_role_session_legacy_missing_process_start_requires_explicit_replacement(
    tmp_path: Path,
) -> None:
    context = RunContext("RUN-ROLE-LEGACY-IDENTITY", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    process_start = coordinator_module._required_process_start_token(os.getpid())

    registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_owner_id="owner-old",
        reservation_pid=os.getpid(),
        reservation_process_start=process_start,
    )
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_token="dispatch-old",
        session_id="SID-LEGACY",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-old")

    document = registry.read()
    document["sessions"]["analytical_owner:REQ-001"]["reservation_process_start"] = None
    coordinator_module._atomic_json(registry.path, document)

    resumed = registry.prepare(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        generation_id="G-0001",
        idempotency_key="dispatch-unknown",
        reservation_owner_id="owner-current",
        reservation_pid=os.getpid(),
        reservation_process_start=process_start,
    )
    assert resumed["mode"] == "blocked"
    assert resumed["status"] == "replacement_required"
    assert resumed["session_id"] == "SID-LEGACY"
    stale = registry.get("analytical_owner:REQ-001")
    assert stale is not None
    assert stale["stale_reason"] == "reservation_owner_unknown"
    assert stale["reservation_process_start"] is None

    replacement = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-replacement",
        allow_replacement=True,
        reservation_owner_id="owner-current",
        reservation_pid=os.getpid(),
        reservation_process_start=process_start,
    )
    assert replacement["mode"] == "replace"
    assert replacement["session_id"] is None
    assert replacement["reservation_process_start"] == process_start
    assert registry.get("analytical_owner:REQ-001")["replacement_of"] == "SID-LEGACY"
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-replacement")


def test_role_session_same_pid_without_current_start_proof_cannot_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-ROLE-CURRENT-IDENTITY-UNKNOWN", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    persisted_start = "psutil:1234.500000"

    registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_owner_id="owner-old",
        reservation_pid=os.getpid(),
        reservation_process_start=persisted_start,
    )
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_token="dispatch-old",
        session_id="SID-BOUND",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-old")

    # Supplying the persisted token explicitly models a caller that can read
    # the legacy row but cannot prove the current process instance.  It must
    # not turn a same-PID claim into an implicit continuation.
    monkeypatch.setattr(coordinator_module, "_process_start_token", lambda _pid=None: None)
    resumed = registry.prepare(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        generation_id="G-0001",
        idempotency_key="dispatch-unknown-current",
        reservation_owner_id="owner-current",
        reservation_pid=os.getpid(),
        reservation_process_start=persisted_start,
    )
    assert resumed["mode"] == "blocked"
    assert resumed["status"] == "replacement_required"
    assert resumed["session_id"] == "SID-BOUND"
    assert registry.get("analytical_owner:REQ-001")["stale_reason"] == "reservation_owner_unknown"


def test_role_session_current_exact_start_token_allows_abandoned_same_pid_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-ROLE-CURRENT-IDENTITY-EXACT", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    persisted_start = "psutil:1234.500000"

    registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_owner_id="owner-old",
        reservation_pid=os.getpid(),
        reservation_process_start=persisted_start,
    )
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_token="dispatch-old",
        session_id="SID-BOUND",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-old")

    monkeypatch.setattr(coordinator_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(coordinator_module, "_process_start_token", lambda _pid=None: persisted_start)
    resumed = registry.prepare(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        generation_id="G-0001",
        idempotency_key="dispatch-next",
        reservation_owner_id="owner-current",
        reservation_pid=os.getpid(),
        reservation_process_start=persisted_start,
    )
    assert resumed["mode"] == "resume"
    assert resumed["session_id"] == "SID-BOUND"
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-next")


def test_role_session_restart_claim_can_resume_bound_orphan_with_local_active_dispatch(
    tmp_path: Path,
) -> None:
    context = RunContext("RUN-ROLE-RESTART", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")

    initial = registry.prepare(action, generation_id="G-0001", idempotency_key="dispatch-1")
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-1",
        reservation_token="dispatch-1",
        session_id="SID-BOUND",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-1")

    document = registry.read()
    document["sessions"]["analytical_owner:REQ-001"]["reservation_pid"] = 99999999
    coordinator_module._atomic_json(registry.path, document)
    resumed = registry.prepare(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        generation_id="G-0001",
        idempotency_key="dispatch-2",
        active_dispatch_tokens={"dispatch-1"},
        active_dispatch_owner_pid=os.getpid(),
    )
    assert resumed["mode"] == "resume"
    assert resumed["session_id"] == "SID-BOUND"
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-2")


def test_role_session_pid_reuse_requires_explicit_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-ROLE-PID-REUSE", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    monkeypatch.setattr(coordinator_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(coordinator_module, "_process_start_token", lambda _pid=None: "start-new")

    first = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_owner_id="owner-old",
        reservation_pid=4242,
        reservation_process_start="start-old",
    )
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_token="dispatch-old",
        session_id="SID-OLD",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-old")

    orphan = registry.prepare(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        generation_id="G-0001",
        idempotency_key="dispatch-reused",
        reservation_owner_id="owner-new",
        reservation_pid=4242,
        reservation_process_start="start-new",
    )
    assert orphan["mode"] == "blocked"
    assert orphan["status"] == "replacement_required"
    assert registry.get("analytical_owner:REQ-001")["session_id"] == "SID-OLD"

    replacement = registry.prepare(
        PlannerAction(
            "analyze_requirement",
            "analytical_owner",
            "REQ-001",
            "authorized replacement",
        ),
        generation_id="G-0001",
        idempotency_key="dispatch-replacement",
        allow_replacement=True,
        reservation_owner_id="owner-new",
        reservation_pid=4242,
        reservation_process_start="start-new",
    )
    assert replacement["mode"] == "replace"
    assert replacement["session_id"] is None
    assert registry.get("analytical_owner:REQ-001")["replacement_of"] == "SID-OLD"
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-replacement")


def test_role_session_abandoned_same_process_is_recoverable_without_active_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-ROLE-ABANDONED", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    current_pid = os.getpid()
    monkeypatch.setattr(coordinator_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(coordinator_module, "_process_start_token", lambda _pid=None: "same-start")

    registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_owner_id="owner-abandoned",
        reservation_pid=current_pid,
        reservation_process_start="same-start",
    )
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-old",
        reservation_token="dispatch-old",
        session_id="SID-ABANDONED",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-old")

    resumed = registry.prepare(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        generation_id="G-0001",
        idempotency_key="dispatch-next",
        reservation_owner_id="owner-restarted",
        reservation_pid=current_pid,
        reservation_process_start="same-start",
    )
    assert resumed["mode"] == "resume"
    assert resumed["session_id"] == "SID-ABANDONED"
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-next")


def test_role_session_registry_multiprocess_race_has_one_reservation_winner(tmp_path: Path) -> None:
    context = RunContext("RUN-ROLE-MP-RACE", tmp_path)
    context.resolve_run_path("control_plane").mkdir(parents=True, exist_ok=True)
    mp_context = mp.get_context("fork")
    barrier = mp_context.Barrier(2)
    release = mp_context.Event()
    result_queue = mp_context.Queue()
    processes = [
        mp_context.Process(
            target=_role_session_registry_race_worker,
            args=(str(tmp_path), context.run_id, f"dispatch-{index}", barrier, release, result_queue),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=5) for _ in processes]
    release.set()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    assert sorted(result.get("mode") for result in results) == ["blocked", "new"]
    assert all("error" not in result for result in results)
    document = coordinator_module.RoleSessionRegistry(context).read()
    assert set(document["sessions"]) == {"analytical_owner:REQ-RACE"}
    entry = document["sessions"]["analytical_owner:REQ-RACE"]
    assert entry["status"] == "reserved"
    assert entry["reservation_token"] in {"dispatch-0", "dispatch-1"}


def test_codex_role_adapter_binds_thread_started_before_process_exit_and_resumes_exact_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[list[str]] = []

    class StreamingProcess:
        def __init__(self, argv: list[str], **_: object) -> None:
            calls.append(list(argv))
            self.stdin = _InputSink({})
            self.stdout = io.BytesIO(b'{"type":"thread.started","thread_id":"SID-STREAM"}\n')
            self.stderr = io.BytesIO()
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            entered.set()
            assert release.wait(2)
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", StreamingProcess)
    context = RunContext("RUN-ROLE-STREAM", tmp_path)
    (tmp_path / "control_plane").mkdir()
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    adapter = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(binary="fake-codex"),
    )
    result: list[RoleExecution] = []
    thread = threading.Thread(target=lambda: result.append(adapter(action, idempotency_key="dispatch-1", context=context)))
    thread.start()
    assert entered.wait(2)
    registry = coordinator_module.RoleSessionRegistry(context)
    for _ in range(100):
        bound = registry.get("analytical_owner:REQ-001")
        if bound is not None and bound.get("session_id") == "SID-STREAM":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("thread.started must bind the session before process exit")
    assert "--ephemeral" not in calls[0]
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result and result[0].ok

    resume = adapter(
        PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume"),
        idempotency_key="dispatch-2",
        context=context,
    )
    assert resume.ok
    assert calls[1][0:4] == ["fake-codex", "exec", "resume", "--skip-git-repo-check"]
    assert "SID-STREAM" in calls[1]
    assert "--ephemeral" not in calls[1]


@pytest.mark.parametrize("replacement_succeeds", [True, False])
def test_codex_role_adapter_replaces_unavailable_resume_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_succeeds: bool,
) -> None:
    """A dead exact resume is recovered by one fresh root, never by retrying its SID."""

    calls: list[list[str]] = []

    class SequencedProcess:
        def __init__(self, argv: list[str], **_: object) -> None:
            index = len(calls)
            calls.append(list(argv))
            self.stdin = _InputSink({})
            if index == 0:
                stdout = b'{"type":"thread.started","thread_id":"SID-OLD"}\n'
                stderr = b""
                self.returncode = 0
            elif index == 1:
                # The transport/app-server cannot open the state DB.  No root
                # event proves that the expected SID was never resumed.
                stdout = b""
                stderr = b"readonly state db: unable to open database file"
                self.returncode = 1
            else:
                stdout = (
                    b'{"type":"thread.started","thread_id":"SID-NEW"}\n'
                    if replacement_succeeds
                    else b""
                )
                stderr = b"" if replacement_succeeds else b"replacement root failed"
                self.returncode = 0 if replacement_succeeds else 1
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO(stderr)

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", SequencedProcess)
    context = RunContext("RUN-ROLE-RESUME-REPLACEMENT", tmp_path)
    (tmp_path / "control_plane").mkdir()
    adapter = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(binary="fake-codex", sandbox="workspace-write"),
    )
    initial = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    resumed = PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume")

    assert adapter(initial, idempotency_key="dispatch-1", context=context).ok
    result = adapter(resumed, idempotency_key="dispatch-2", context=context)

    assert len(calls) == 3
    assert calls[1][0:4] == ["fake-codex", "exec", "resume", "--skip-git-repo-check"]
    assert "SID-OLD" in calls[1]
    assert "resume" not in calls[2]
    sandbox_index = calls[2].index("--sandbox")
    assert calls[2][sandbox_index : sandbox_index + 2] == ["--sandbox", "workspace-write"]
    assert "SID-OLD" not in calls[2]
    assert sum("resume" in argv for argv in calls) == 1

    entry = coordinator_module.RoleSessionRegistry(context).get("analytical_owner:REQ-001")
    assert entry is not None
    assert entry["replacement_of"] == "SID-OLD"
    assert entry["last_action"] == resumed.action
    assert entry["last_idempotency_key"] == "dispatch-2"
    assert any(
        item["idempotency_key"] == "dispatch-2"
        and item["action"] == resumed.action
        for item in entry["action_lineage"]
    )
    assert entry["logical_owner"] == "analytical_owner:REQ-001"

    if replacement_succeeds:
        assert result.ok
        assert result.session_id == "SID-NEW"
        assert entry["session_id"] == "SID-NEW"
        assert entry["status"] == "active"
    else:
        assert not result.ok
        assert result.session_status == "replacement_required"
        assert entry["session_id"] is None
        assert entry["status"] == "replacement_required"


def test_role_session_registry_failed_resume_replacement_is_atomic_and_preserves_owner(
    tmp_path: Path,
) -> None:
    context = RunContext("RUN-ROLE-FAILED-RESUME-CAS", tmp_path)
    registry = coordinator_module.RoleSessionRegistry(context)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    process_start = coordinator_module._required_process_start_token()
    reservation = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-1",
        reservation_owner_id="owner-1",
        reservation_process_start=process_start,
    )
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-1",
        reservation_token="dispatch-1",
        session_id="SID-OLD",
    )
    registry.complete_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="dispatch-1",
        reservation_token="dispatch-1",
        session_id="SID-OLD",
    )
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-1")
    resumed_action = PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume")
    reservation = registry.prepare(
        resumed_action,
        generation_id="G-0001",
        idempotency_key="dispatch-2",
        reservation_owner_id="owner-1",
        reservation_process_start=process_start,
    )

    replacement = registry.replace_failed_resume(
        resumed_action,
        generation_id="G-0001",
        idempotency_key="dispatch-2",
        reservation_token=str(reservation["reservation_token"]),
        expected_session_id="SID-OLD",
    )

    assert replacement["mode"] == "replace"
    assert replacement["session_id"] is None
    assert replacement["reservation_token"] == "dispatch-2"
    entry = registry.get("analytical_owner:REQ-001")
    assert entry is not None
    assert entry["status"] == "reserved"
    assert entry["replacement_of"] == "SID-OLD"
    assert entry["reservation_owner_id"] == "owner-1"
    assert entry["reservation_process_start"] == process_start
    assert entry["reservation_action"] == "resume_requirement_analysis"
    assert entry["action_lineage"][-1]["idempotency_key"] == "dispatch-2"
    registry.release_reservation("analytical_owner:REQ-001", "dispatch-2")


def test_coordinator_internal_resume_replacement_does_not_consume_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class SequencedProcess:
        def __init__(self, argv: list[str], **_: object) -> None:
            index = len(calls)
            calls.append(list(argv))
            self.stdin = _InputSink({})
            payload = (
                b'{"type":"thread.started","thread_id":"SID-OLD"}\n'
                if index == 0
                else b'{"type":"thread.started","thread_id":"SID-NEW"}\n'
                if index == 2
                else b""
            )
            self.stdout = io.BytesIO(payload)
            self.stderr = io.BytesIO(b"readonly state db" if index == 1 else b"")
            self.returncode = 0 if index != 1 else 1

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", SequencedProcess)
    initial = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    resumed = PlannerAction("resume_requirement_analysis", "analytical_owner", "REQ-001", "resume")
    planner = QueuePlanner((initial,), (resumed,), ())
    context = RunContext("RUN-COORD-RESUME-REPLACEMENT", tmp_path)
    adapter = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(binary="fake-codex", sandbox="workspace-write"),
    )
    coordinator = RunCoordinator(context, planner_provider=planner, role_runner=adapter)
    coordinator.start(_spec(context.run_id))

    status = coordinator.run(max_steps=3)

    assert len(calls) == 3
    assert status.status == "waiting"
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    waits = [event for event in events if event["event"] == "wait"]
    assert waits
    assert all(event["payload"].get("reason") != "retry_budget" for event in waits)
    assert not any(
        diagnostic.get("kind") == "role_transport_failure"
        and diagnostic.get("transport", {}).get("session_status") == "replacement_required"
        for diagnostic in coordinator.status().diagnostics
    )
    coordinator.close(wait_for_roles=True)


def test_codex_role_adapter_orphan_without_thread_started_requires_explicit_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class CrashedProcess:
        def __init__(self, argv: list[str], **_: object) -> None:
            calls.append(list(argv))
            self.stdin = _InputSink({})
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            raise RuntimeError("simulated transport crash")

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", CrashedProcess)
    context = RunContext("RUN-ROLE-ORPHAN", tmp_path)
    (tmp_path / "control_plane").mkdir()
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    adapter = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(binary="fake-codex"),
    )
    failed = adapter(action, idempotency_key="dispatch-1", context=context)
    assert failed.exit_code == 1
    assert failed.session_status == "replacement_required"
    entry = coordinator_module.RoleSessionRegistry(context).get("analytical_owner:REQ-001")
    assert entry["status"] == "replacement_required"
    assert entry["stale_reason"] == "thread_started_missing"
    assert len(calls) == 1

    class ReplacementProcess(CrashedProcess):
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            super().__init__(argv, **kwargs)
            self.stdout = io.BytesIO(b'{"type":"thread.started","thread_id":"SID-REPLACEMENT"}\n')

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", ReplacementProcess)
    replacement = adapter(
        PlannerAction(
            "analyze_requirement",
            "analytical_owner",
            "REQ-001",
            "replace orphan",
            metadata={"allow_session_replacement": True},
        ),
        idempotency_key="dispatch-2",
        context=context,
    )
    assert replacement.ok
    assert replacement.session_id == "SID-REPLACEMENT"
    assert len(calls) == 2


def test_analytical_owner_guidance_requires_identity_fanout_and_wait() -> None:
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    guidance = coordinator_module._role_guidance(action)

    for marker in (
        "readiness check",
        "search_ontology",
        "selected_sources()",
        "load_selected_source_ids()",
        "DataRoomCatalogEntry.source_id",
        "ControlledScriptRunner.validate_script/run_analysis",
        "same Analytical Owner action and begin_attempt",
        "repair_active=true",
        "reuse that authorization",
        "never call use_business_repair again",
        "search_identity_mappings",
        "propose_identity_domain",
        "mark_waiting_on_resolution",
        "one Entity Resolution Owner request per domain",
    ):
        assert marker in guidance


def test_initial_business_repair_activates_before_readiness_or_selection() -> None:
    action = PlannerAction("repair_requirement", "analytical_owner", "REQ-001", "repair")
    guidance = coordinator_module._role_guidance(action)

    begin = guidance.index("BusinessReviewAdapter.begin_repair")
    use = guidance.index("ItemWorkspace.use_business_repair")
    readiness = guidance.index("readiness check")
    search = guidance.index("search_ontology")
    assert begin < use < readiness < search
    assert "do not call BusinessReviewAdapter.begin_repair" not in guidance


def test_integration_guidance_auto_stages_accepted_artifacts_and_keeps_failures_open() -> None:
    action = PlannerAction("integrate_requirement", "integration_agent", "REQ-001", "integrate")
    guidance = coordinator_module._role_guidance(action)

    assert "IntegrationSession.create/load" in guidance
    assert "auto-stages all sealed business-accepted typed AnalyticalArtifact refs" in guidance
    assert "sealed item-local state" in guidance
    assert "mandatory handoff" in guidance
    assert "Do not manually re-submit or re-declare accepted analytical artifacts" in guidance
    assert "accepted_content_hash are immutable" in guidance
    assert "derived pre-commit projection" in guidance
    assert "correct_record for every authorized affected record" in guidance
    assert "remove_record when removal is authorized" in guidance
    assert "literal difference between normalized typed fields" in guidance
    assert "targeted recheck" in guidance
    assert "do not invent a new integration method" in guidance
    assert "open/pending" in guidance
    assert "record an incident" in guidance
    assert "retry/repair" in guidance
    assert "never terminalize accepted integration as a technical failure" in guidance
    assert "mark_technical_failure" not in guidance
    assert guidance.index("validate") < guidance.index("build_fidelity_packet")
    assert "session.commit" in guidance


def test_active_business_repair_reuses_authorization_without_reactivation() -> None:
    action = PlannerAction(
        "resume_requirement_analysis",
        "analytical_owner",
        "REQ-001",
        "resume active repair",
        metadata={"repair_active": True},
    )
    guidance = coordinator_module._role_guidance(action)

    assert guidance.index("repair_active=true") < guidance.index("readiness check")
    assert "Do not call BusinessReviewAdapter.begin_repair" in guidance
    assert "or ItemWorkspace.use_business_repair" in guidance


def test_analytical_owner_custom_prompt_cannot_replace_public_source_and_retry_contract(
    tmp_path: Path,
) -> None:
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    custom = "CUSTOM AO CONTEXT: use any convenient catalog field and hand off failures."
    prompt = coordinator_module.build_role_prompt(
        action,
        context=RunContext("RUN-COORD", tmp_path),
        idempotency_key="key",
        custom=custom,
    )
    assert prompt.index(custom) < prompt.index("Analytical Owner sequence:")
    assert prompt.index("selected_sources()") > prompt.index(custom)
    assert prompt.index("same Analytical Owner action and begin_attempt") > prompt.index(custom)


def test_active_skill_release_rotation_has_a_typed_binding_mismatch_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _install_test_skill(tmp_path, monkeypatch)
    binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path, role_cwd=tmp_path)
    stale = coordinator_module.CodexExecConfig.from_dict(
        {**binding, "skill_sha256": "f" * 64},
        validate_binding=False,
    )

    with pytest.raises(CoordinatorProductionBindingMismatch, match="active installed skill"):
        stale.validate_skill_binding(
            required=True,
            verify_active=True,
            repo_root=tmp_path,
            role_cwd=tmp_path,
        )

    missing = coordinator_module.CodexExecConfig.from_dict(
        {**binding, "skill_path": str(tmp_path / "missing-skill")},
        validate_binding=False,
    )
    with pytest.raises(CoordinatorIntegrityError) as error:
        missing.validate_skill_binding(
            required=True,
            verify_active=True,
            repo_root=tmp_path,
            role_cwd=tmp_path,
        )
    assert not isinstance(error.value, CoordinatorProductionBindingMismatch)
    assert active.is_dir()


def test_active_skill_binding_rejects_missing_stale_symlink_frontmatter_hash_and_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _install_test_skill(tmp_path, monkeypatch)
    assert coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)["skill_path"] == str(active.resolve())

    shutil.rmtree(active)
    with pytest.raises(CoordinatorIntegrityError, match="active installed skill"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    _install_test_skill(tmp_path, monkeypatch)
    stale = tmp_path / "stale" / "skills" / "renamed-stale-skill"
    _write_synthetic_skill(stale, skill_version="0.3.0", core_version="0.3.0", release="legacy-release")
    shutil.rmtree(active)
    shutil.copytree(stale, active)
    with pytest.raises(CoordinatorIntegrityError, match="frontmatter"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    shutil.rmtree(active)
    _install_test_skill(tmp_path, monkeypatch)
    shutil.rmtree(active)
    active.symlink_to(stale, target_is_directory=True)
    with pytest.raises(CoordinatorIntegrityError, match="non-symlink"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    active.unlink()
    _install_test_skill(tmp_path, monkeypatch)
    (active / "SKILL.md").write_text("---\nname: stale\n---\n", encoding="utf-8")
    with pytest.raises(CoordinatorIntegrityError, match="frontmatter"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    shutil.rmtree(active)
    _install_test_skill(tmp_path, monkeypatch)
    (active / "README.md").write_text("drift\n", encoding="utf-8")
    with pytest.raises(CoordinatorIntegrityError, match="bytes"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    shutil.rmtree(active)
    _install_test_skill(tmp_path, monkeypatch)
    duplicate = tmp_path / ".agents" / "skills" / "renamed-production-skill"
    shutil.copytree(active, duplicate)
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)


def test_active_skill_binding_rejects_home_and_run_root_discovery_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _install_test_skill(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    home_duplicate = home / ".agents" / "skills" / "renamed-home-production-skill"
    shutil.copytree(active, home_duplicate)
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    shutil.rmtree(home_duplicate)

    run_root = tmp_path / "runs" / "RUN-001"
    run_duplicate = run_root / ".agents" / "skills" / "renamed-run-production-skill"
    shutil.copytree(active, run_duplicate)
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path, role_cwd=run_root)


def test_discovery_uses_top_level_quoted_name_and_ignores_unrelated_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _install_test_skill(tmp_path, monkeypatch)
    duplicate = tmp_path / ".agents" / "skills" / "arbitrary-directory-name"
    shutil.copytree(active, duplicate)
    skill_file = duplicate / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    text = text.replace(
        f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n",
        f'name: "{coordinator_module.PRODUCTION_SKILL_NAME}" # production alias\n',
        1,
    ).replace(
        "description: synthetic test fixture\n",
        "description: synthetic test fixture\nmetadata:\n  name: unrelated-nested-name\n",
        1,
    )
    skill_file.write_text(text, encoding="utf-8")
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    shutil.rmtree(duplicate)

    unrelated = tmp_path / "unrelated-skill"
    _write_synthetic_skill(
        unrelated,
        skill_version="0.3.0",
        core_version="0.3.0",
        release="unrelated-release",
    )
    unrelated_file = unrelated / "SKILL.md"
    unrelated_file.write_text(
        unrelated_file.read_text(encoding="utf-8").replace(
            f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n",
            "name: other-skill\n",
            1,
        ),
        encoding="utf-8",
    )
    alias = tmp_path / "home" / ".agents" / "skills" / "unrelated-alias"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(unrelated, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)["skill_path"] == str(active.resolve())


def test_upgrade_and_rebind_binds_unbound_spec_once_and_loads_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    run_id = "RUN-BINDING-UPGRADE"
    context = RunContext(run_id, tmp_path)
    old = _spec(run_id)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(old)
    target = _bound_spec(run_id, coordinator_module.resolve_production_skill_binding(repo_root=tmp_path))

    upgraded = coordinator.upgrade_and_rebind(target)
    assert upgraded.phase == "queued"
    assert coordinator.persisted_spec() == target
    events_path = tmp_path / "control_plane" / "coordinator_events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "coordinator_binding_upgraded"
    sequence = upgraded.last_event_seq
    event_bytes = events_path.read_bytes()
    state_bytes = (tmp_path / "control_plane" / "coordinator_state.json").read_bytes()

    retried = coordinator.upgrade_and_rebind(target)
    assert retried.last_event_seq == sequence
    assert events_path.read_bytes() == event_bytes
    assert (tmp_path / "control_plane" / "coordinator_state.json").read_bytes() == state_bytes

    loaded = RunCoordinator.from_persisted_spec(context, planner_provider=QueuePlanner(()))
    assert loaded.persisted_spec() == target
    loaded.close()


def test_binding_upgrade_preserves_every_known_nondefault_spec_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    run_id = "RUN-BINDING-PRESERVE"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        role_dispatch_command=("fake-role", "--flag"),
        publication_policy={"enabled": True, "scope": "custom"},
        codex_exec={
            "binary": "fake-codex",
            "model": "gpt-test",
            "profile": "offline",
            "sandbox": "read-only",
            "timeout_seconds": 17,
            "ephemeral": False,
            "role_prompts": {"analytical_owner": "custom prompt"},
            "role_models": {"analytical_owner": "gpt-role"},
            "role_profiles": {"analytical_owner": "role-profile"},
            "role_sandboxes": {"analytical_owner": "read-only"},
            "role_timeouts": {"analytical_owner": 19},
        },
        lease_ttl_seconds=41,
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(old)
    binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    target = CoordinatorRunSpec(
        old.run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        role_dispatch_command=old.role_dispatch_command,
        publication_policy=old.publication_policy,
        codex_exec={**old.codex_exec, **binding},
        lease_ttl_seconds=old.lease_ttl_seconds,
    )
    coordinator.upgrade_and_rebind(target)
    assert coordinator.persisted_spec() == target
    assert coordinator.persisted_spec().to_dict() == target.to_dict()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"unexpected": True}),
        lambda value: value["codex_exec"].update({"unexpected": True}),
    ),
)
def test_unknown_persisted_spec_fields_reject_without_writing(
    tmp_path: Path,
    mutate: Any,
) -> None:
    context = RunContext("RUN-UNKNOWN-SPEC", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    spec_path = tmp_path / "control_plane" / "coordinator_spec.json"
    value = json.loads(spec_path.read_text(encoding="utf-8"))
    mutate(value)
    spec_path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    before = spec_path.read_bytes()
    with pytest.raises(ValueError, match="unknown/deprecated"):
        coordinator.persisted_spec()
    assert spec_path.read_bytes() == before


def test_upgrade_and_rebind_rejects_active_lineage_and_hash_conflicts_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    active_context = RunContext("RUN-UPGRADE-ACTIVE", tmp_path / "active")
    active_coordinator = RunCoordinator(active_context, planner_provider=QueuePlanner(()))
    active_coordinator.start(_spec(active_context.run_id))
    _seed_active_dispatch(active_coordinator, _action("analyze_requirement", "REQ-ACTIVE"), runner_pid=999_999)
    active_before = _control_plane_tree(active_context.run_root / "control_plane")
    with pytest.raises(CoordinatorConflictError, match="dispatches are active"):
        active_coordinator.upgrade_and_rebind(_bound_spec(active_context.run_id, binding))
    assert _control_plane_tree(active_context.run_root / "control_plane") == active_before

    lineage_context = RunContext("RUN-UPGRADE-LINEAGE", tmp_path / "lineage")
    lineage_coordinator = RunCoordinator(lineage_context, planner_provider=QueuePlanner(()))
    lineage_coordinator.start(_spec(lineage_context.run_id))
    lineage_before = _control_plane_tree(lineage_context.run_root / "control_plane")
    different_lineage = CoordinatorRunSpec(
        lineage_context.run_id,
        "G-0002",
        "planner://changed",
        hashlib.sha256(b"changed").hexdigest(),
        codex_exec={"binary": "fake-codex", **binding},
    )
    with pytest.raises(CoordinatorConflictError, match="unchanged planner lineage"):
        lineage_coordinator.upgrade_and_rebind(different_lineage)
    assert _control_plane_tree(lineage_context.run_root / "control_plane") == lineage_before

    hash_context = RunContext("RUN-UPGRADE-HASH", tmp_path / "hash")
    hash_coordinator = RunCoordinator(hash_context, planner_provider=QueuePlanner(()))
    hash_coordinator.start(_spec(hash_context.run_id))
    state_path = hash_context.run_root / "control_plane" / "coordinator_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["spec_hash"] = "f" * 64
    state_path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    hash_before = _control_plane_tree(hash_context.run_root / "control_plane")
    with pytest.raises((CoordinatorConflictError, CoordinatorIntegrityError)):
        hash_coordinator.upgrade_and_rebind(_bound_spec(hash_context.run_id, binding))
    assert _control_plane_tree(hash_context.run_root / "control_plane") == hash_before


def test_rebind_transport_preserves_lineage_progress_and_is_idempotent(tmp_path: Path) -> None:
    run_id = "RUN-TRANSPORT-REBIND"
    sentinel = "SECRET_TRANSPORT_ROLE_PROMPT_MODEL_COMMAND"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        role_dispatch_command=(sentinel,),
        codex_exec={"binary": "old-codex"},
    )
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        role_dispatch_command=(sentinel,),
        codex_exec={
            "binary": sentinel,
            "role_models": {"foundry_supervisor": sentinel},
            "role_reasoning_efforts": {"foundry_supervisor": "high"},
            "role_prompts": {"foundry_supervisor": sentinel},
        },
    )
    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner(()),
        role_runner=object(),
    )
    coordinator.start(old)
    before_state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    rebound = coordinator.rebind_transport(target)
    assert rebound.status == "ready"
    assert rebound.generation_id == old.generation_id
    assert coordinator.persisted_spec() == target
    after_state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    for field in ("generation_id", "planner_ref", "planner_hash", "status", "phase", "attempt", "no_progress_count", "last_action"):
        assert after_state[field] == before_state[field]
    events_path = tmp_path / "control_plane/coordinator_events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert sentinel.encode("utf-8") not in events_path.read_bytes()
    assert [event["event"] for event in events][-2:] == [
        "coordinator_transport_rebind_started",
        "coordinator_transport_rebound",
    ]
    assert not coordinator.transport_rebind_intent_path.exists()
    event_bytes = events_path.read_bytes()
    state_bytes = (tmp_path / "control_plane/coordinator_state.json").read_bytes()
    retried = coordinator.rebind_transport(target)
    assert retried.last_event_seq == rebound.last_event_seq
    assert events_path.read_bytes() == event_bytes
    assert (tmp_path / "control_plane/coordinator_state.json").read_bytes() == state_bytes


def test_rebind_transport_accepts_same_path_skill_release_upgrade_without_old_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiescent transport rotation must not revalidate removed old bytes."""

    skill_path = _install_test_skill(tmp_path, monkeypatch)
    run_id = "RUN-TRANSPORT-SKILL-ROTATION"
    context = RunContext(run_id, tmp_path)
    old_binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        codex_exec={"binary": "fake-codex", **old_binding},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(old)

    # Keep the canonical path but replace its bytes.  The previous release is
    # no longer recoverable from the active install, which is the production
    # skill-upgrade scenario that used to make rebind_transport fail while
    # loading the persisted old spec.
    (skill_path / "README.md").write_text("rotated release fixture\n", encoding="utf-8")
    new_hash = hashlib.sha256(coordinator_module._skill_release_bytes(skill_path)).hexdigest()
    monkeypatch.setattr(coordinator_module, "PRODUCTION_SKILL_SHA256", new_hash)
    new_binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "fake-codex", **new_binding},
    )

    rebound = coordinator.rebind_transport(target)
    assert rebound.status == "ready"
    assert coordinator.persisted_spec() == target
    assert not coordinator.transport_rebind_intent_path.exists()


@pytest.mark.parametrize(
    "failpoint",
    (
        "transport_rebind_after_intent",
        "transport_rebind_after_started",
        "transport_rebind_after_spec",
        "transport_rebind_after_event",
    ),
)
def test_transport_rebind_failpoint_recovery_keeps_secret_target_private(
    tmp_path: Path,
    failpoint: str,
) -> None:
    run_id = f"RUN-TRANSPORT-FAIL-{failpoint}"
    sentinel = "SECRET_TRANSPORT_FAILPOINT_TARGET"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        role_dispatch_command=(sentinel,),
        codex_exec={"binary": "old-codex"},
    )
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        role_dispatch_command=(sentinel,),
        codex_exec={
            "binary": sentinel,
            "role_models": {"foundry_supervisor": sentinel},
            "role_prompts": {"foundry_supervisor": sentinel},
        },
    )
    first = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    first.start(old)

    def inject(name: str) -> None:
        if name == failpoint:
            raise BaseException(name)

    first._failpoint = inject
    with pytest.raises(BaseException, match=failpoint):
        first.rebind_transport(target)

    restarted = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    restarted.rebind_transport(target)
    assert restarted.persisted_spec() == target
    assert not restarted.transport_rebind_intent_path.exists()
    events_path = tmp_path / "control_plane" / "coordinator_events.jsonl"
    assert sentinel.encode("utf-8") not in events_path.read_bytes()
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events][-2:] == [
        "coordinator_transport_rebind_started",
        "coordinator_transport_rebound",
    ]


def test_rebind_transport_rejects_active_dispatch_without_write(tmp_path: Path) -> None:
    run_id = "RUN-TRANSPORT-ACTIVE"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        codex_exec={"binary": "old-codex"},
    )
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "new-codex"},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    coordinator.start(old)
    _seed_active_dispatch(
        coordinator,
        _action("analyze_requirement", "REQ-ACTIVE"),
        runner_pid=os.getpid(),
        runner_process_start=coordinator_module._required_process_start_token(os.getpid()),
    )
    before = _control_plane_tree(tmp_path / "control_plane")

    with pytest.raises(CoordinatorConflictError, match="dispatches are active"):
        coordinator.rebind_transport(target)
    assert _control_plane_tree(tmp_path / "control_plane") == before


def test_rebind_transport_clears_dead_orphan_and_records_durable_cleanup(
    tmp_path: Path,
) -> None:
    run_id = "RUN-TRANSPORT-DEAD-ORPHAN"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        codex_exec={"binary": "old-codex"},
    )
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "new-codex"},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    coordinator.start(old)
    key = _seed_active_dispatch(
        coordinator,
        _action("analyze_requirement", "REQ-DEAD-ORPHAN"),
        runner_pid=999_999,
    )

    rebound = coordinator.rebind_transport(target)

    assert rebound.status == "waiting"
    assert rebound.active_dispatches == ()
    assert coordinator.persisted_spec() == target
    state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert state["active_dispatches"] == []
    diagnostics = state["diagnostics"]
    cleanup_diagnostics = [
        value
        for value in diagnostics
        if value.get("reason") == "transport_rebind_orphan_cleanup"
    ]
    assert cleanup_diagnostics
    assert cleanup_diagnostics[-1]["removed_dispatches"][0]["idempotency_key"] == key
    events = [
        json.loads(line)
        for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    ]
    cleanup_events = [
        event
        for event in events
        if event["event"] == "dispatch_claims_cleared"
        and event["payload"].get("reason") == "transport_rebind_orphan_cleanup"
    ]
    assert cleanup_events
    assert cleanup_events[-1]["payload"]["removed_dispatches"][0]["idempotency_key"] == key
    assert any(
        event["event"] == "dispatch_started"
        and event["payload"]["idempotency_key"] == key
        for event in events
    )
    assert [event["event"] for event in events][-2:] == [
        "coordinator_transport_rebind_started",
        "coordinator_transport_rebound",
    ]


@pytest.mark.parametrize("candidate_hash_mode", ("valid", "missing", "mismatch"))
def test_transport_rebind_marks_matching_product_reservation_for_explicit_replacement(
    tmp_path: Path,
    candidate_hash_mode: str,
) -> None:
    """Dead dispatch cleanup preserves Product Agent lineage as stale."""

    run_id = "RUN-TRANSPORT-PRODUCT-ORPHAN"
    context = RunContext(run_id, tmp_path)
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    candidate, _review = _seed_product_repair_candidate(context)
    old = _spec(run_id)
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "new-codex"},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    coordinator.start(old)
    action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        run_id,
        "reviewed product repair",
        metadata={
            "review_verdict": "repair_once",
            **(
                {"candidate_hash": candidate.computed_hash}
                if candidate_hash_mode == "valid"
                else ({"candidate_hash": "0" * 64} if candidate_hash_mode == "mismatch" else {})
            ),
        },
    )
    registry = coordinator_module.RoleSessionRegistry(context)
    state, _ = coordinator._read_replay()  # noqa: SLF001
    assert state is not None
    key = coordinator._idempotency_key(state, action)
    reservation = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key=key,
        reservation_owner_id="dead-owner",
        reservation_pid=999_999,
        reservation_process_start="dead-start",
    )
    assert reservation["mode"] == "new"
    with coordinator._locked(create=False):  # noqa: SLF001 - admission regression
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        state["active_dispatches"] = [
            {
                "action": action.to_dict(),
                "idempotency_key": key,
                "slot_key": coordinator._slot_key(action),
                "runner_id": "dead-owner",
                "runner_pid": 999_999,
                "runner_process_start": "dead-start",
            }
        ]
        state["status"] = "dispatching"
        state["phase"] = action.action
        coordinator._append_event_locked(  # noqa: SLF001
            state,
            "dispatch_started",
            {"action": action.to_dict(), "idempotency_key": key},
        )

    rebound = coordinator.rebind_transport(target)

    assert rebound.status == "waiting"
    stale = registry.get(f"product_agent:{run_id}:G-0001")
    assert stale is not None
    assert stale["status"] == "replacement_required"
    assert stale["stale_reason"] == "orphaned_reservation"
    assert stale["session_id"] is None
    reopened = coordinator.reopen("authorize reviewed product repair")
    assert reopened.status == "ready"
    with coordinator._locked(create=False):  # noqa: SLF001 - one-shot auth admission
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        authorized = coordinator._authorize_preview_replacement_for_final_locked(state, action)  # noqa: SLF001
        assert authorized is (candidate_hash_mode == "valid")
        if candidate_hash_mode == "valid":
            consumed = coordinator._consume_replacement_authorization(state, action)  # noqa: SLF001
            assert consumed is not None
            authorized_action, _binding = consumed
            assert authorized_action.metadata["allow_session_replacement"] is True
            assert coordinator._consume_replacement_authorization(state, action) is None  # noqa: SLF001
        else:
            # The stale authorization remains available for a later exact
            # action, but the admission guard must not consume it after this
            # malformed/mismatched repair offer.
            assert coordinator._replacement_authorization_for_action(state, action) is not None  # noqa: SLF001
        coordinator._append_event_locked(  # noqa: SLF001 - persist one-shot consumption
            state,
            "test_product_replacement_authorized",
            {"action": action.to_dict()},
        )
    # The row is not silently deleted: only the explicit replacement path may
    # reserve a new Product Agent root.
    blocked = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="replacement-without-authorization",
        reservation_owner_id="new-owner",
        reservation_pid=os.getpid(),
        reservation_process_start=coordinator_module._required_process_start_token(os.getpid()),
    )
    assert blocked["mode"] == "blocked"
    assert blocked["status"] == "replacement_required"
    replacement = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="authorized-product-replacement",
        allow_replacement=True,
        reservation_owner_id="new-owner",
        reservation_pid=os.getpid(),
        reservation_process_start=coordinator_module._required_process_start_token(os.getpid()),
    )
    assert replacement["mode"] == "replace"
    assert replacement["session_id"] is None
    registry.release_reservation(f"product_agent:{run_id}:G-0001", "authorized-product-replacement")


def test_transport_rebind_keeps_live_product_reservation_blocked(
    tmp_path: Path,
) -> None:
    """A live Product Agent owner cannot be adopted by rebind cleanup."""

    run_id = "RUN-TRANSPORT-PRODUCT-LIVE"
    context = RunContext(run_id, tmp_path)
    old = _spec(run_id)
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "new-codex"},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    coordinator.start(old)
    action = PlannerAction("build_product_candidate", "product_agent", run_id, "live reservation")
    registry = coordinator_module.RoleSessionRegistry(context)
    process_start = coordinator_module._required_process_start_token(os.getpid())
    state, _ = coordinator._read_replay()  # noqa: SLF001
    assert state is not None
    key = coordinator._idempotency_key(state, action)
    reservation = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key=key,
        reservation_owner_id="live-owner",
        reservation_pid=os.getpid(),
        reservation_process_start=process_start,
    )
    assert reservation["mode"] == "new"
    with coordinator._locked(create=False):  # noqa: SLF001 - admission regression
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        state["active_dispatches"] = [
            {
                "action": action.to_dict(),
                "idempotency_key": key,
                "slot_key": coordinator._slot_key(action),
                "runner_id": "live-owner",
                "runner_pid": os.getpid(),
                "runner_process_start": process_start,
            }
        ]
        state["status"] = "dispatching"
        state["phase"] = action.action
        coordinator._append_event_locked(  # noqa: SLF001
            state,
            "dispatch_started",
            {"action": action.to_dict(), "idempotency_key": key},
        )

    with pytest.raises(CoordinatorConflictError, match="dispatches are active"):
        coordinator.rebind_transport(target)
    live = registry.get(f"product_agent:{run_id}:G-0001")
    assert live is not None
    assert live["status"] == "reserved"
    assert live["reservation_status"] == "reserved"
    registry.release_reservation(f"product_agent:{run_id}:G-0001", key)


def test_reopen_admits_one_stale_final_product_replacement_after_retry_exhaustion(
    tmp_path: Path,
) -> None:
    """A public reopen grants exactly one stale-final Product Agent attempt."""

    run_id = "RUN-REOPEN-STALE-FINAL-PRODUCT"
    input_fingerprint = "a" * 64
    old_candidate_hash = "b" * 64
    action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        run_id,
        "rebuild stale final candidate after product implementation repair",
        metadata={
            "generation_id": "G-0001",
            "input_fingerprint": input_fingerprint,
            # The old candidate identity is deliberately retained in the
            # offer; its artifact bindings are what the Product Agent must
            # rebuild.  No review_verdict is available while that candidate
            # is invalid, matching the live recovery boundary.
            "candidate_hash": old_candidate_hash,
        },
    )
    context = RunContext(run_id, tmp_path)
    calls: list[PlannerAction] = []

    def transport(current: PlannerAction, **_: object) -> RoleExecution:
        calls.append(current)
        return RoleExecution()

    planner = QueuePlanner((action,), (action,), (action,))
    coordinator = RunCoordinator(context, planner_provider=planner, adapters={action.action: transport})
    coordinator._phase_snapshot = lambda: {  # type: ignore[method-assign]
        "lifecycle_validation": {"valid": True},
        "product": {"preview_input_fingerprint": input_fingerprint},
        "items": {},
    }
    coordinator.start(_spec(run_id))

    registry = coordinator_module.RoleSessionRegistry(context)
    reservation = registry.prepare(
        action,
        generation_id="G-0001",
        idempotency_key="stale-product-dispatch",
        reservation_owner_id="dead-product-owner",
        reservation_pid=999_999,
        reservation_process_start="dead-start",
    )
    assert reservation["mode"] == "new"
    registry.mark_stale(
        action,
        generation_id="G-0001",
        idempotency_key="stale-product-dispatch",
        reservation_token="stale-product-dispatch",
        reason="orphaned_reservation",
    )
    registry.release_reservation(
        f"product_agent:{run_id}:G-0001",
        "stale-product-dispatch",
    )

    with coordinator._locked(create=False):  # noqa: SLF001 - retry/reopen boundary
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        retry_fingerprint = coordinator_module.logical_action_fingerprint(
            action,
            run_id=run_id,
            generation_id="G-0001",
        )
        state_fingerprint = coordinator._authoritative_state_fingerprint(state, action)  # noqa: SLF001
        state["retry_counts"] = {
            retry_fingerprint: coordinator_module.MAX_RUN_RETRIES_PER_ACTION,
        }
        state["retry_blocked"] = {
            retry_fingerprint: {
                "action": action.to_dict(),
                "count": coordinator_module.MAX_RUN_RETRIES_PER_ACTION,
                "recoverable": True,
            }
        }
        state["retry_state_fingerprints"] = {retry_fingerprint: state_fingerprint}
        state["status"] = "waiting"
        state["phase"] = "waiting"
        coordinator._append_event_locked(  # noqa: SLF001
            state,
            "seed_stale_final_retry",
            {"action": action.to_dict(), "retry_fingerprint": retry_fingerprint},
        )

    reopened = coordinator.reopen("explicitly authorize stale final product replacement")
    assert reopened.status == "ready"
    first = coordinator._refresh_and_launch(set())  # noqa: SLF001
    assert first.status == "dispatching"
    completed = coordinator._consume_one()  # noqa: SLF001
    assert completed is not None
    slot = coordinator._slot_key(action)  # noqa: SLF001
    coordinator._refresh_and_launch({slot}, completed=completed)  # noqa: SLF001
    assert len(calls) == 1

    state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert state["replacement_authorizations"] == {}

    # The same exhausted offer is still blocked after the one-shot
    # authorization is consumed.  A second attempt requires another public
    # reopen; the retry budget is not globally reset.
    third = coordinator._refresh_and_launch(set())  # noqa: SLF001
    assert third.status == "waiting"
    assert third.active_dispatches == ()
    assert len(calls) == 1
    assert any(
        diagnostic.get("kind") == "retry_budget_exhausted"
        for diagnostic in third.diagnostics
    )
    coordinator.close(wait_for_roles=True)


def test_rebind_transport_keeps_ambiguous_live_dispatch_without_write(tmp_path: Path) -> None:
    run_id = "RUN-TRANSPORT-AMBIGUOUS-LIVE"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        codex_exec={"binary": "old-codex"},
    )
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "new-codex"},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    coordinator.start(old)
    _seed_active_dispatch(
        coordinator,
        _action("analyze_requirement", "REQ-AMBIGUOUS-LIVE"),
        runner_pid=os.getpid(),
    )
    before = _control_plane_tree(tmp_path / "control_plane")

    with pytest.raises(CoordinatorConflictError, match="dispatches are active"):
        coordinator.rebind_transport(target)

    assert _control_plane_tree(tmp_path / "control_plane") == before


def test_rebind_transport_rejects_stale_spec_hash_without_write(tmp_path: Path) -> None:
    run_id = "RUN-TRANSPORT-HASH"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        codex_exec={"binary": "old-codex"},
    )
    target = CoordinatorRunSpec(
        run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "new-codex"},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), role_runner=object())
    coordinator.start(old)
    state_path = tmp_path / "control_plane/coordinator_state.json"
    state = json.loads(state_path.read_text())
    state["spec_hash"] = "f" * 64
    state_path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
    before = _control_plane_tree(tmp_path / "control_plane")

    with pytest.raises((CoordinatorConflictError, CoordinatorIntegrityError)):
        coordinator.rebind_transport(target)
    assert _control_plane_tree(tmp_path / "control_plane") == before


@pytest.mark.parametrize(
    "failpoint",
    (
        "binding_upgrade_after_intent",
        "binding_upgrade_after_spec",
        "binding_upgrade_after_event",
        "binding_upgrade_after_state",
    ),
)
def test_binding_upgrade_failpoint_recovery_is_idempotent_and_split_brain_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    run_id = f"RUN-BINDING-FAIL-{failpoint}"
    context = RunContext(run_id, tmp_path)
    first = RunCoordinator(context, planner_provider=QueuePlanner(()))
    first.start(_spec(run_id))
    target = _bound_spec(run_id, coordinator_module.resolve_production_skill_binding(repo_root=tmp_path))

    def inject(name: str) -> None:
        if name == failpoint:
            raise BaseException(name)

    first._failpoint = inject
    with pytest.raises(BaseException, match=failpoint):
        first.upgrade_and_rebind(target)

    restarted = RunCoordinator(context, planner_provider=QueuePlanner(()))
    restarted.upgrade_and_rebind(target)
    assert restarted.persisted_spec() == target
    events_path = tmp_path / "control_plane" / "coordinator_events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events][-1] == "coordinator_binding_upgraded"
    assert all(event["seq"] == index for index, event in enumerate(events, 1))
    before = events_path.read_bytes()
    restarted.upgrade_and_rebind(target)
    assert events_path.read_bytes() == before


def test_one_lock_start_and_hash_chain(tmp_path: Path) -> None:
    context = RunContext("RUN-COORD", tmp_path)
    planner = QueuePlanner((_action(),))
    coordinator = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": lambda action, **_: "diagnostic"})
    started = coordinator.start(_spec())
    assert started.status == "ready"
    assert (tmp_path / "control_plane" / ".coordinator.lock").is_file()
    assert not (tmp_path / ".coordinator_admission.lock").exists()
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert [event["seq"] for event in events] == [1]
    assert events[0]["event"] == "run_started"
    assert events[0]["event_hash"] == started.last_event_hash


def test_publish_rebinds_idle_generation_and_is_idempotent(tmp_path: Path) -> None:
    run_id = "RUN-PLAN-REBIND"
    first = _spec(run_id)
    second = _rebound_spec(run_id, "G-0002", "planner://g2")
    coordinator = RunCoordinator(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    coordinator.start(first)
    coordinator._terminal_projection = lambda: (True, "complete_with_limits", {"projection": "valid"})  # type: ignore[method-assign]
    terminal = coordinator.step()
    assert terminal.status == "complete_with_limits"

    rebound = coordinator.publish_and_rebind(second, lambda _spec: None)

    assert rebound.status == "ready"
    assert rebound.phase == "ready"
    assert rebound.active_dispatches == ()
    assert coordinator.persisted_spec() == second
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "plan_rebound"
    assert events[-1]["payload"]["from"]["generation_id"] == first.generation_id
    assert events[-1]["payload"]["to"]["generation_id"] == second.generation_id
    seq = rebound.last_event_seq
    assert coordinator.publish_and_rebind(second, lambda _spec: pytest.fail("duplicate publisher")).last_event_seq == seq


def test_start_rebind_rejects_active_or_non_lineage_changes(tmp_path: Path) -> None:
    run_id = "RUN-PLAN-REBIND-CONFLICT"
    first = _spec(run_id)
    second = _rebound_spec(run_id, "G-0002", "planner://g2")
    coordinator = RunCoordinator(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    coordinator.start(first)
    non_lineage = CoordinatorRunSpec(
        run_id,
        first.generation_id,
        first.planner_ref,
        first.planner_hash,
        role_dispatch_command=("different-role",),
    )
    before_non_lineage = _control_plane_tree(tmp_path / "control_plane")
    with pytest.raises(CoordinatorConflictError):
        coordinator.start(non_lineage)
    assert _control_plane_tree(tmp_path / "control_plane") == before_non_lineage
    active = _action("analyze_requirement", "REQ-ACTIVE")
    _seed_active_dispatch(coordinator, active, runner_pid=999_999)
    before_lineage = _control_plane_tree(tmp_path / "control_plane")
    with pytest.raises(CoordinatorConflictError):
        coordinator.start(second)
    assert _control_plane_tree(tmp_path / "control_plane") == before_lineage


def test_cross_process_rebind_has_one_winner(tmp_path: Path) -> None:
    run_id = "RUN-PLAN-REBIND-MP"
    context = RunContext(run_id, tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(run_id))
    mp_context = mp.get_context("fork")
    barrier = mp_context.Barrier(2)
    results = [tmp_path / f"rebind-{index}.txt" for index in range(2)]
    processes = [
        mp_context.Process(
            target=_rebind_process_worker,
            args=(str(tmp_path), run_id, "G-0002", "planner://g2", barrier, str(results[index])),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    outcomes = [result.read_text() for result in results]
    assert all(value.startswith("ok:") for value in outcomes)
    persisted = json.loads((tmp_path / "control_plane/coordinator_spec.json").read_text())
    assert persisted["generation_id"] == "G-0002"
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events].count("plan_rebound") == 1


def test_action_change_is_authoritative_and_output_is_diagnostic(tmp_path: Path) -> None:
    first = _action()
    second = _action("review_requirement", "REQ-1")
    planner = QueuePlanner((first,), (second,))
    calls: list[str] = []

    def role(action: PlannerAction, **_: object) -> str:
        calls.append(action.action)
        return "not a result envelope"

    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": role})
    coordinator.start(_spec())
    status = coordinator.step()
    # The changed Planner action is reconciled and launched immediately; a
    # second active dispatch is therefore visible while this step returns.
    assert status.status == "dispatching"
    assert calls == ["analyze_requirement"]
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert "planner_advanced" in [event["event"] for event in events]
    assert not any(event["event"] == "role_completed" for event in events)


def test_same_action_is_waiting_and_reopen_preserves_retry_history(tmp_path: Path) -> None:
    action = _action()
    planner = QueuePlanner((action,), (action,), (action,), (action,), (action,), (action,))
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={"analyze_requirement": lambda action, **_: RoleExecution(output="ordinary text")},
    )
    coordinator.start(_spec())
    status = coordinator.run(max_steps=5)
    assert status.status == "waiting"
    assert status.no_progress_count == 2
    assert any(item.get("kind") == "no_progress" for item in status.diagnostics)
    before = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert before["retry_counts"]
    assert before["retry_state_fingerprints"]
    reopened = coordinator.reopen("retry after external repair")
    assert reopened.status == "ready"
    assert reopened.no_progress_count == 0
    assert reopened.last_event_seq > status.last_event_seq
    after = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert after["retry_counts"] == before["retry_counts"]
    assert after["retry_blocked"] == before["retry_blocked"]


def test_reopen_authorizes_only_matching_replacement_owner_once(tmp_path: Path) -> None:
    """Planner metadata cannot self-authorize; reopen grants one exact owner once."""

    context = RunContext("RUN-COORD-REOPEN-AUTH", tmp_path)
    stale = _action("resume_requirement_analysis", "REQ-1")
    unrelated = _action("resume_requirement_analysis", "REQ-2")
    registry = coordinator_module.RoleSessionRegistry(context)
    registry.mark_stale(
        stale,
        generation_id="G-0001",
        idempotency_key="stale-session",
        reason="thread_started_missing",
    )
    seen: list[PlannerAction] = []

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        seen.append(action)
        return RoleExecution()

    # The Planner's authority-looking metadata is stripped at intake.
    planner_action = PlannerAction(
        stale.action,
        stale.role,
        stale.subject_id,
        stale.reason,
        metadata={"allow_session_replacement": True},
    )
    planner = QueuePlanner((), (planner_action,), (unrelated,))
    coordinator = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={stale.action: transport},
    )
    coordinator.start(_spec(context.run_id))
    assert coordinator.step().status == "waiting"
    assert "allow_session_replacement" not in coordinator._strip_untrusted_replacement_authorization(
        planner_action
    ).metadata
    reopened = coordinator.reopen("replace stale analytical owner")
    assert reopened.status == "ready"
    state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert "analytical_owner:REQ-1" in state["replacement_authorizations"]
    assert RunCoordinator._replacement_authorization_for_action(state, unrelated) is None

    # The matching offer receives the coordinator-only flag and consumes it.
    coordinator.step()
    coordinator.run(max_steps=2)
    assert seen
    assert seen[0].metadata.get("allow_session_replacement") is True
    state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert state["replacement_authorizations"] == {}
    coordinator.close(wait_for_roles=True)


def test_changed_durable_state_reopens_bounded_retry_for_same_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    context = RunContext("RUN-STATE-AWARE-RETRY", tmp_path)
    calls: list[str] = []

    def transport(current: PlannerAction, **_: object) -> RoleExecution:
        calls.append(current.action)
        return RoleExecution()

    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={action.action: transport},
    )
    coordinator.start(_spec(context.run_id))
    retry_fingerprint = coordinator_module.logical_action_fingerprint(
        action,
        run_id=context.run_id,
        generation_id="G-0001",
    )
    with coordinator._locked(create=False):
        state, _ = coordinator._read_replay()
        assert state is not None
        state["retry_counts"] = {retry_fingerprint: 2}
        state["retry_blocked"] = {
            retry_fingerprint: {"action": action.to_dict(), "count": 2, "recoverable": True}
        }
        state["retry_state_fingerprints"] = {retry_fingerprint: "a" * 64}
        coordinator._append_event_locked(state, "seed_retry", {"action": action.to_dict()})
    monkeypatch.setattr(coordinator, "_phase_snapshot", lambda: {"marker": "advanced"})
    status = coordinator._refresh_and_launch(set())
    assert calls == ["analyze_requirement"]
    assert status.status == "dispatching"
    coordinator.close(wait_for_roles=True)


def test_transport_rebind_invalidates_only_offered_retry_for_changed_spec(
    tmp_path: Path,
) -> None:
    """A changed transport identity reopens the offered action only."""

    action_a = _action("analyze_requirement", "REQ-A")
    action_b = _action("analyze_requirement", "REQ-B")
    context = RunContext("RUN-TRANSPORT-RETRY-ROTATION", tmp_path)
    old = CoordinatorRunSpec(
        context.run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        codex_exec={"binary": "old-codex"},
    )
    target = CoordinatorRunSpec(
        context.run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        codex_exec={"binary": "new-codex"},
    )
    calls: list[str] = []
    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action_a,)),
        adapters={
            "analyze_requirement": lambda action, **_: calls.append(action.subject_id)
            or RoleExecution(),
        },
    )
    # Keep a validated item-scoped phase projection so the regression covers
    # the live requirement path (where the old implementation omitted spec
    # identity from the fingerprint).
    coordinator._phase_snapshot = lambda: {  # type: ignore[method-assign]
        "lifecycle_validation": {"valid": True},
        "items": {
            "REQ-A": {"lifecycle_state": "work"},
            "REQ-B": {"lifecycle_state": "work"},
        },
    }
    coordinator.start(old)
    retry_fingerprints: dict[str, str] = {}
    with coordinator._locked(create=False):  # noqa: SLF001 - retry fixture boundary
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        retry_counts: dict[str, int] = {}
        retry_blocked: dict[str, dict[str, object]] = {}
        retry_states: dict[str, str] = {}
        for action in (action_a, action_b):
            fingerprint = coordinator_module.logical_action_fingerprint(
                action,
                run_id=context.run_id,
                generation_id=old.generation_id,
            )
            retry_fingerprints[action.subject_id] = fingerprint
            retry_counts[fingerprint] = coordinator_module.MAX_RUN_RETRIES_PER_ACTION
            retry_blocked[fingerprint] = {
                "action": action.to_dict(),
                "count": coordinator_module.MAX_RUN_RETRIES_PER_ACTION,
                "recoverable": True,
            }
            retry_states[fingerprint] = coordinator._authoritative_state_fingerprint(  # noqa: SLF001
                state,
                action,
            )
        state["retry_counts"] = retry_counts
        state["retry_blocked"] = retry_blocked
        state["retry_state_fingerprints"] = retry_states
        coordinator._append_event_locked(  # noqa: SLF001
            state,
            "retry_seed",
            {"actions": [action_a.to_dict(), action_b.to_dict()]},
        )
        old_spec_hash = str(state["spec_hash"])

    coordinator.rebind_transport(target)
    with coordinator._locked(create=False):  # noqa: SLF001 - retry fixture boundary
        rebound_state, _ = coordinator._read_replay()  # noqa: SLF001
        assert rebound_state is not None
        assert rebound_state["spec_hash"] != old_spec_hash
        assert coordinator._authoritative_state_fingerprint(  # noqa: SLF001
            rebound_state,
            action_a,
        ) != retry_states[retry_fingerprints["REQ-A"]]

    status = coordinator._refresh_and_launch(set())  # noqa: SLF001
    try:
        assert status.status == "dispatching"
        with coordinator._locked(create=False):  # noqa: SLF001 - retry fixture boundary
            after, _ = coordinator._read_replay()  # noqa: SLF001
            assert after is not None
            # The offered action is reopened and admitted; the unrelated
            # action's retry evidence remains durable and is not globally
            # wiped by the transport rotation.
            assert retry_fingerprints["REQ-A"] not in after["retry_counts"]
            assert retry_fingerprints["REQ-A"] not in after["retry_blocked"]
            assert retry_fingerprints["REQ-A"] not in after["retry_state_fingerprints"]
            assert after["retry_counts"].get(retry_fingerprints["REQ-B"]) == coordinator_module.MAX_RUN_RETRIES_PER_ACTION
            assert retry_fingerprints["REQ-B"] in after["retry_blocked"]
            assert retry_fingerprints["REQ-B"] in after["retry_state_fingerprints"]
            assert any(
                entry.get("action", {}).get("subject_id") == "REQ-A"
                for entry in after["active_dispatches"]
            )
    finally:
        coordinator.close(wait_for_roles=True)


def test_idempotent_transport_rebind_preserves_exhausted_retry(
    tmp_path: Path,
) -> None:
    """Rebinding to the persisted transport identity does not reset retries."""

    action = _action("analyze_requirement", "REQ-SAME")
    context = RunContext("RUN-TRANSPORT-RETRY-IDEMPOTENT", tmp_path)
    spec = CoordinatorRunSpec(
        context.run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        codex_exec={"binary": "same-codex"},
    )
    calls: list[str] = []
    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={
            "analyze_requirement": lambda current, **_: calls.append(current.subject_id)
            or RoleExecution(),
        },
    )
    coordinator._phase_snapshot = lambda: {  # type: ignore[method-assign]
        "lifecycle_validation": {"valid": True},
        "items": {"REQ-SAME": {"lifecycle_state": "work"}},
    }
    coordinator.start(spec)
    with coordinator._locked(create=False):  # noqa: SLF001 - retry fixture boundary
        state, _ = coordinator._read_replay()  # noqa: SLF001
        assert state is not None
        fingerprint = coordinator_module.logical_action_fingerprint(
            action,
            run_id=context.run_id,
            generation_id=spec.generation_id,
        )
        state_fingerprint = coordinator._authoritative_state_fingerprint(  # noqa: SLF001
            state,
            action,
        )
        state["retry_counts"] = {fingerprint: coordinator_module.MAX_RUN_RETRIES_PER_ACTION}
        state["retry_blocked"] = {
            fingerprint: {
                "action": action.to_dict(),
                "count": coordinator_module.MAX_RUN_RETRIES_PER_ACTION,
                "recoverable": True,
            }
        }
        state["retry_state_fingerprints"] = {fingerprint: state_fingerprint}
        coordinator._append_event_locked(  # noqa: SLF001
            state,
            "retry_seed",
            {"action": action.to_dict()},
        )

    # This is an idempotent no-op at the persisted spec boundary.
    rebound = coordinator.rebind_transport(spec)
    assert rebound.status == "ready"
    status = coordinator._refresh_and_launch(set())  # noqa: SLF001
    try:
        assert status.status == "waiting"
        assert calls == []
        with coordinator._locked(create=False):  # noqa: SLF001 - retry fixture boundary
            after, _ = coordinator._read_replay()  # noqa: SLF001
            assert after is not None
            assert after["retry_counts"].get(fingerprint) == coordinator_module.MAX_RUN_RETRIES_PER_ACTION
            assert fingerprint in after["retry_blocked"]
            assert after["active_dispatches"] == []
    finally:
        coordinator.close(wait_for_roles=True)


def test_retry_budget_wait_projects_blocked_action(tmp_path: Path) -> None:
    action = _action()
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner((action,)))
    coordinator.start(_spec())

    status = coordinator._refresh_and_launch({coordinator._slot_key(action)})

    assert status.status == "waiting"
    assert status.next_action == action.to_dict()
    assert status.active_dispatches == ()


def test_nonzero_role_transport_waits_and_is_retryable(tmp_path: Path) -> None:
    action = _action()
    planner = QueuePlanner((action,), (action,))
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={"analyze_requirement": lambda action, **_: RoleExecution(exit_code=7, error="temporary")},
    )
    coordinator.start(_spec())
    status = coordinator.step()
    assert status.status == "waiting"
    assert status.next_action["action"] == action.action
    assert status.diagnostics[-1]["kind"] == "role_transport_failure"
    assert "role_completed" not in [json.loads(line)["event"] for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]


def test_event_log_ahead_replays_after_checkpoint_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    action = _action()
    planner = QueuePlanner((action,), ())
    context = RunContext("RUN-COORD", tmp_path)
    fired = {"value": False}
    now = {"value": 100.0}

    def failpoint(name: str) -> None:
        if name == "after_event_before_checkpoint" and not fired["value"]:
            fired["value"] = True
            raise RuntimeError("simulated crash")

    first = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": lambda action, **_: "ok"}, clock=lambda: now["value"])
    first.start(_spec())
    first._failpoint = failpoint
    with pytest.raises(RuntimeError):
        first.step()
    now["value"] = 200.0
    # The failpoint models a process crash.  This test runs both coordinator
    # instances in one Python process, so mark the persisted owner dead
    # explicitly before exercising restart replay.
    monkeypatch.setattr(coordinator_module, "_pid_alive", lambda pid: False)
    resumed = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": lambda action, **_: "ok"}, clock=lambda: now["value"])
    status = resumed.resume()
    assert status.status == "waiting"
    assert status.last_event_seq >= 2


def test_terminal_status_requires_authoritative_projection(tmp_path: Path) -> None:
    planner = QueuePlanner(())
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=planner)
    coordinator.start(_spec())
    coordinator._terminal_projection = lambda: (True, "complete_with_limits", {"projection": "valid"})  # type: ignore[method-assign]
    status = coordinator.step()
    assert status.status == "complete_with_limits"
    assert status.publication_ready


def test_tampered_event_fails_closed(tmp_path: Path) -> None:
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec())
    events_path = tmp_path / "control_plane/coordinator_events.jsonl"
    event = json.loads(events_path.read_text().splitlines()[0])
    event["payload"] = {"tampered": True}
    events_path.write_text(json.dumps(event) + "\n")
    with pytest.raises(CoordinatorIntegrityError):
        coordinator.status()


def test_ready_set_launches_in_planner_order_and_overlaps(tmp_path: Path) -> None:
    first = _action("resolve_identity", "domain-1")
    second = _action("analyze_requirement", "REQ-2")
    planner = QueuePlanner((first, second), ())
    entered: list[str] = []
    entered_lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        with entered_lock:
            entered.append(action.action)
            if len(entered) == 2:
                both_entered.set()
        assert release.wait(2)
        return RoleExecution(output="ordinary transport")

    coordinator = RunCoordinator(context := RunContext("RUN-READY-SET", tmp_path), planner_provider=planner, adapters={"resolve_identity": role, "analyze_requirement": role})
    coordinator.start(_spec(context.run_id))
    thread = threading.Thread(target=coordinator.step)
    thread.start()
    assert both_entered.wait(2), "both ready actions must overlap"
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert entered[:2] == ["resolve_identity", "analyze_requirement"]


def test_requirement_mutation_admission_serializes_same_requirement_and_releases_queue(tmp_path: Path) -> None:
    repair = PlannerAction("repair_requirement", "analytical_owner", "REQ-1", "repair")
    review = PlannerAction("review_requirement", "business_reviewer", "REQ-1", "review")
    planner = QueuePlanner((repair, review), (review,), ())
    entered: list[str] = []
    first_started = threading.Event()
    review_started = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        entered.append(action.action)
        if action == repair:
            first_started.set()
            assert release.wait(2)
        else:
            review_started.set()
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-SAME-REQUIREMENT", tmp_path),
        planner_provider=planner,
        adapters={"repair_requirement": role, "review_requirement": role},
    )
    coordinator.start(_spec("RUN-SAME-REQUIREMENT"))
    thread = threading.Thread(target=coordinator.step)
    thread.start()
    assert first_started.wait(2)
    assert not review_started.is_set(), "review must remain queued behind same-item repair"
    release.set()
    assert review_started.wait(2)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert entered == ["repair_requirement", "review_requirement"]
    coordinator.close(wait_for_roles=True)


def test_analytical_owner_capacity_is_global_but_entity_resolution_remains_parallel(tmp_path: Path) -> None:
    first = PlannerAction("analyze_requirement", "analytical_owner", "REQ-1", "analyze")
    second = PlannerAction("analyze_requirement", "analytical_owner", "REQ-2", "analyze")
    planner = QueuePlanner((first, second), (second,), ())
    entered: list[str] = []
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        entered.append(action.subject_id)
        if action == first:
            first_started.set()
            assert release.wait(2)
        else:
            second_started.set()
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-GLOBAL-AO", tmp_path),
        planner_provider=planner,
        adapters={"analyze_requirement": role},
    )
    coordinator.start(_spec("RUN-GLOBAL-AO"))
    thread = threading.Thread(target=coordinator.step)
    thread.start()
    assert first_started.wait(2)
    assert not second_started.is_set(), "a second requirement must wait for the single AO slot"
    release.set()
    assert second_started.wait(2)
    thread.join(timeout=3)
    coordinator.close(wait_for_roles=True)


def test_reopen_resume_admission_does_not_oversubscribe_persisted_ao(tmp_path: Path) -> None:
    first = PlannerAction("analyze_requirement", "analytical_owner", "REQ-1", "analyze")
    second = PlannerAction("analyze_requirement", "analytical_owner", "REQ-2", "analyze")
    context = RunContext("RUN-REOPEN-AO-CAPACITY", tmp_path)
    seed = RunCoordinator(context, planner_provider=QueuePlanner(()))
    seed.start(_spec(context.run_id))
    _seed_active_dispatch(seed, first)
    seed.close(wait_for_roles=True)

    planner = QueuePlanner((first, second), (second,), ())
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        if action == first:
            first_started.set()
            assert release.wait(2)
        else:
            second_started.set()
        return RoleExecution()

    restarted = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={"analyze_requirement": role},
    )
    thread = threading.Thread(target=restarted.step)
    thread.start()
    assert first_started.wait(2)
    assert not second_started.is_set(), "resume must not submit a second persisted AO"
    release.set()
    assert second_started.wait(2)
    thread.join(timeout=3)
    restarted.close(wait_for_roles=True)


def test_first_completion_reloads_planner_and_launches_new_action(tmp_path: Path) -> None:
    first = _action("resolve_identity", "domain-1")
    long_action = _action("analyze_requirement", "REQ-2")
    newly_ready = _action("review_requirement", "REQ-3")
    planner = QueuePlanner((first, long_action), (newly_ready, long_action), ())
    long_started = threading.Event()
    release_long = threading.Event()
    newly_ready_started = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        if action.action == "analyze_requirement":
            long_started.set()
            assert release_long.wait(3)
        elif action.action == "review_requirement":
            newly_ready_started.set()
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-FIRST-COMPLETE", tmp_path),
        planner_provider=planner,
        adapters={"resolve_identity": role, "analyze_requirement": role, "review_requirement": role},
    )
    coordinator.start(_spec("RUN-FIRST-COMPLETE"))
    status = coordinator.step()
    assert status.status == "dispatching"
    assert long_started.is_set()
    assert newly_ready_started.wait(2), "new action must launch before the long action finishes"
    release_long.set()
    coordinator.close(wait_for_roles=True)


def test_two_process_reconciliation_serializes_planner_advance(tmp_path: Path) -> None:
    old_action = _action("analyze_requirement", "REQ-RACE-OLD")
    new_action = _action("review_requirement", "REQ-RACE-NEW")
    run_id = "RUN-RECONCILE-RACE"
    coordinator = RunCoordinator(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    coordinator.start(_spec(run_id))

    mp_context = mp.get_context("fork")
    planner_read = mp_context.Event()
    release_planner_read = mp_context.Event()
    a_started = mp_context.Event()
    b_started = mp_context.Event()
    a_finished = mp_context.Event()
    b_finished = mp_context.Event()
    release_a_role = mp_context.Event()
    old_marker = tmp_path / "old-role.marker"
    new_marker = tmp_path / "new-role.marker"
    process_a = mp_context.Process(
        target=_reconciliation_race_worker,
        args=(
            str(tmp_path),
            run_id,
            "race-a",
            old_action,
            True,
            planner_read,
            release_planner_read,
            a_started,
            a_finished,
            release_a_role,
            str(old_marker),
        ),
    )
    process_b = mp_context.Process(
        target=_reconciliation_race_worker,
        args=(
            str(tmp_path),
            run_id,
            "race-b",
            new_action,
            False,
            planner_read,
            release_planner_read,
            b_started,
            b_finished,
            release_a_role,
            str(new_marker),
        ),
    )
    process_a.start()
    assert a_started.wait(5)
    assert planner_read.wait(5)
    process_b.start()
    assert b_started.wait(5)
    assert not b_finished.is_set(), "B must wait for A's lock-held Planner read"
    release_planner_read.set()
    assert b_finished.wait(5)
    release_a_role.set()
    assert a_finished.wait(5)
    for process in (process_a, process_b):
        process.join(timeout=5)
        assert process.exitcode == 0

    assert old_marker.read_text() == old_action.subject_id
    assert new_marker.read_text() == new_action.subject_id
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    dispatches = [
        event["payload"]["action"]["subject_id"]
        for event in events
        if event["event"] == "dispatch_started"
    ]
    assert dispatches == [old_action.subject_id, new_action.subject_id]
    assert not any(
        event["event"] == "planner_advanced"
        and event["payload"].get("reason") == "restart_active_not_offered"
        and any(
            removed["action"]["subject_id"] == old_action.subject_id
            for removed in event["payload"].get("removed_dispatches", ())
        )
        for event in events
    ), "a live runner claim must not be pruned by another process's Planner snapshot"
    state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert state["active_dispatches"] == []


def test_duplicate_ready_slots_are_dispatched_once(tmp_path: Path) -> None:
    first = _action("resolve_identity", "domain-1")
    duplicate = _action("resolve_identity", "domain-1")
    second = _action("analyze_requirement", "REQ-2")
    planner = QueuePlanner((first, duplicate, second), (), ())
    calls: list[str] = []

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action.action + ":" + action.subject_id)
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-DEDUPE", tmp_path),
        planner_provider=planner,
        adapters={"resolve_identity": role, "analyze_requirement": role},
    )
    coordinator.start(_spec("RUN-DEDUPE"))
    coordinator.run(max_steps=2)
    assert sorted(calls) == ["analyze_requirement:REQ-2", "resolve_identity:domain-1"]


def test_crash_restart_resumes_multiple_dispatches_with_same_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _action("resolve_identity", "domain-1")
    second = _action("analyze_requirement", "REQ-2")
    planner = QueuePlanner((first, second), ())
    fail_count = {"value": 0}

    def failpoint(name: str) -> None:
        if name != "after_event_before_checkpoint":
            return
        fail_count["value"] += 1
        if fail_count["value"] == 2:
            raise RuntimeError("simulated crash")

    context = RunContext("RUN-RESTART-SET", tmp_path)
    first_coordinator = RunCoordinator(context, planner_provider=planner, adapters={"resolve_identity": lambda action, **_: RoleExecution(), "analyze_requirement": lambda action, **_: RoleExecution()})
    first_coordinator.start(_spec(context.run_id))
    first_coordinator._failpoint = failpoint
    with pytest.raises(RuntimeError):
        first_coordinator.run(max_steps=1)

    seen_keys: list[str] = []

    def idempotent_role(action: PlannerAction, *, idempotency_key: str, **_: object) -> RoleExecution:
        seen_keys.append(idempotency_key)
        return RoleExecution(output="committed once by public API")

    restarted = RunCoordinator(
        context,
        planner_provider=QueuePlanner((first, second), ()),
        adapters={"resolve_identity": idempotent_role, "analyze_requirement": idempotent_role},
    )
    monkeypatch.setattr(coordinator_module, "_pid_alive", lambda pid: False)
    restarted.run(max_steps=2)
    assert len(seen_keys) == 2
    assert len(set(seen_keys)) == 2


def _legacy_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _legacy_hash(value: object) -> str:
    return hashlib.sha256(_legacy_json_bytes(value)).hexdigest()


def _legacy_state_hash(value: dict[str, object]) -> str:
    snapshot = dict(value)
    snapshot["last_event_hash"] = ""
    return _legacy_hash(snapshot)


def _synthetic_legacy_g5_control_plane(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    destination = tmp_path / "control_plane"
    destination.mkdir()
    run_id = "RUN-G5-SYNTHETIC"
    generation_id = "G-0005"
    planner_hash = hashlib.sha256(b"synthetic-planner").hexdigest()
    run_spec = {
        "actions": [],
        "adapter_capabilities": {},
        "codex_exec": {
            "binary": "legacy-codex",
            "role_prompts": {"analytical_owner": "legacy strict RoleResult prompt"},
            "role_models": {"analytical_owner": "legacy-model"},
        },
        "coordinator_agent_command": [],
        "generation_id": generation_id,
        "offline_test_mode": True,
        "parent_lineage": {},
        "phase_validator_command": [],
        "planner_hash": planner_hash,
        "planner_ref": "planner://synthetic",
        "policy": {},
        "publication_policy": {"enabled": True, "legacy_channel": "legacy"},
        "role_dispatch_command": ["legacy-role-command"],
        "run_id": run_id,
    }
    lineage = {
        "active_generation_pointer_hash": hashlib.sha256(b"pointer").hexdigest(),
        "generation_id": generation_id,
        "manifest_hash": hashlib.sha256(b"manifest").hexdigest(),
        "plan_hash": planner_hash,
        "planner_hash": planner_hash,
        "planner_ref": "planner://synthetic",
    }
    spec_hash = _legacy_hash(run_spec)
    spec_document = {
        "schema_version": 1,
        "kind": "run_coordinator_spec",
        "run_spec": run_spec,
        "lineage_binding": lineage,
        "spec_hash": spec_hash,
    }
    state: dict[str, object] = {
        "schema_version": 1,
        "kind": "run_coordinator_state",
        "run_id": run_id,
        "generation_id": generation_id,
        "planner_ref": "planner://synthetic",
        "planner_hash": planner_hash,
        "spec_hash": spec_hash,
        "spec_ref": "control_plane/coordinator_spec.json",
        "actions": [],
        "active_action": None,
        "active_idempotency_key": None,
        "adapter_capabilities": {},
        "attempt": 0,
        "completed": {},
        "diagnostics": [],
        "last_completed_action": None,
        "last_event_seq": 0,
        "last_event_hash": "",
        "lease": None,
        "lineage_binding": lineage,
        "next_action_index": 0,
        "offline_test_mode": True,
        "owner": None,
        "parent_lineage": {},
        "phase": "queued",
        "planner_refresh_required": False,
        "planner_revision": None,
        "policy": {},
        "publication_policy": {},
        "publication_ready": False,
        "remaining_repair_grant": 0,
        "repair_count": 0,
        "replan_count": 0,
        "role_results": {},
        "status": "ready",
    }
    after_state = dict(state)
    after_state["last_event_seq"] = 1
    after_state["last_event_hash"] = ""
    event: dict[str, object] = {
        "schema_version": 1,
        "kind": "run_coordinator_event",
        "seq": 1,
        "event": "run_started",
        "run_id": run_id,
        "generation_id": generation_id,
        "planner_ref": "planner://synthetic",
        "planner_hash": planner_hash,
        "phase": "queued",
        "action": None,
        "subject_id": None,
        "idempotency_key": None,
        "owner": None,
        "lease": None,
        "attempt": 0,
        "repair_count": 0,
        "replan_count": 0,
        "parent_lineage": {},
        "publication_policy": {},
        "payload": {"action_count": 0, "spec_hash": spec_hash},
        "created_at": "2026-01-01T00:00:00+00:00",
        "previous_event_hash": "",
        "after_state": after_state,
        "state_hash": _legacy_state_hash(after_state),
    }
    event["event_hash"] = _legacy_hash(event)
    state["last_event_seq"] = 1
    state["last_event_hash"] = event["event_hash"]
    files = {
        "coordinator_spec.json": _legacy_json_bytes(spec_document),
        "coordinator_state.json": _legacy_json_bytes(state),
        "coordinator_events.jsonl": _legacy_json_bytes(event),
        ".coordinator.lock": b"",
    }
    for name, payload in files.items():
        (destination / name).write_bytes(payload)
    return destination, files


def test_exact_g5_import_archives_bytes_and_starts_new_chain(tmp_path: Path) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    wrapper = json.loads(original["coordinator_spec.json"])
    run_id = wrapper["run_spec"]["run_id"]
    action = _action("analyze_requirement", "REQ-23")
    planner = QueuePlanner((action,), ())
    seen: list[str] = []

    coordinator = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path),
        planner_provider=planner,
        adapters={"analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"]) or RoleExecution()},
    )
    pending = coordinator.status()
    assert pending.status == "waiting"
    assert pending.phase == "legacy_import_required"

    imported = coordinator.reopen("import exact G5 snapshot")
    assert imported.status == "ready"
    archive = control_plane / "legacy_import"
    for name in ("coordinator_spec.json", "coordinator_state.json", "coordinator_events.jsonl"):
        assert (archive / name).read_bytes() == original[name]
    current_spec = json.loads((control_plane / "coordinator_spec.json").read_text())
    assert set(current_spec) == {
        "schema_version",
        "kind",
        "run_id",
        "generation_id",
        "planner_ref",
        "planner_hash",
        "role_dispatch_command",
        "publication_policy",
        "codex_exec",
        "lease_ttl_seconds",
    }
    current_state = json.loads((control_plane / "coordinator_state.json").read_text())
    assert current_state["active_dispatches"] == []
    assert current_state["last_action"] is None
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["legacy_imported"]
    assert events[0]["payload"]["archive"]["coordinator_state.json"]["sha256"] == hashlib.sha256(original["coordinator_state.json"]).hexdigest()
    assert coordinator.status().last_event_hash == events[0]["event_hash"]

    # Reopen is idempotent after import and does not append another import.
    before_seq = coordinator.status().last_event_seq
    assert coordinator.reopen("repeat import").last_event_seq == before_seq
    resumed = coordinator.resume()
    assert resumed.status == "waiting"
    assert len(seen) == 1
    assert coordinator.status().last_event_seq >= 2


def test_g5_import_rebinds_legacy_transport_to_minimal_generation(tmp_path: Path) -> None:
    control_plane, _ = _synthetic_legacy_g5_control_plane(tmp_path)
    run_id = "RUN-G5-SYNTHETIC"
    new_spec = CoordinatorRunSpec(
        run_id,
        "G-0006",
        "planner://g6",
        hashlib.sha256(b"planner://g6").hexdigest(),
        publication_policy={"enabled": False, "channel": "new"},
        codex_exec={"binary": "new-codex", "role_models": {"analytical_owner": "new-model"}},
        lease_ttl_seconds=19,
    )
    coordinator = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path),
        planner_provider=QueuePlanner(()),
        adapters={"analyze_requirement": lambda current, **_: RoleExecution()},
    )
    assert coordinator.reopen("import G5 before generation rebind").status == "ready"

    published: list[str] = []
    rebound = coordinator.publish_and_rebind(new_spec, lambda spec: published.append(spec.generation_id))

    assert rebound.status == "ready"
    assert published == ["G-0006"]
    persisted = json.loads((control_plane / "coordinator_spec.json").read_text())
    assert persisted["generation_id"] == "G-0006"
    assert persisted["planner_ref"] == "planner://g6"
    assert persisted["publication_policy"] == {"enabled": False, "channel": "new"}
    assert persisted["codex_exec"] == {"binary": "new-codex", "role_models": {"analytical_owner": "new-model"}}
    assert persisted["lease_ttl_seconds"] == 19.0
    assert "role_prompts" not in json.dumps(persisted)
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    rebound_event = events[-1]
    assert rebound_event["event"] == "plan_rebound"
    assert rebound_event["payload"]["old_spec_hash"] != rebound_event["payload"]["new_spec_hash"]
    assert rebound_event["payload"]["transport"]["fields"] == [
        "role_dispatch_command",
        "codex_exec",
        "lease_ttl_seconds",
    ]
    assert rebound_event["payload"]["publication"]["fields"] == ["publication_policy"]
    assert "legacy strict RoleResult prompt" not in (control_plane / "coordinator_events.jsonl").read_text()
    assert coordinator.status().last_event_hash == rebound_event["event_hash"]

    before_retry = _control_plane_tree(control_plane)
    retry = coordinator.publish_and_rebind(new_spec, lambda _spec: pytest.fail("duplicate publisher"))
    assert retry.last_event_seq == rebound.last_event_seq
    assert _control_plane_tree(control_plane) == before_retry

    _seed_active_dispatch(coordinator, _action("analyze_requirement", "REQ-G5-ACTIVE"), runner_pid=999_999)
    before_active_conflict = _control_plane_tree(control_plane)
    with pytest.raises(CoordinatorConflictError):
        coordinator.start(_rebound_spec(run_id, "G-0007", "planner://g7"))
    assert _control_plane_tree(control_plane) == before_active_conflict


@pytest.mark.parametrize("mutation", ("near_wrapper", "partial_state"))
def test_malformed_or_partial_g5_import_fails_without_mutation(tmp_path: Path, mutation: str) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    if mutation == "near_wrapper":
        value = json.loads((control_plane / "coordinator_spec.json").read_text())
        value["unexpected"] = True
        (control_plane / "coordinator_spec.json").write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    else:
        (control_plane / "coordinator_events.jsonl").unlink()
    before = {path.name: path.read_bytes() for path in control_plane.iterdir() if path.is_file()}
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]
    with pytest.raises(CoordinatorIntegrityError):
        RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path))
    after = {path.name: path.read_bytes() for path in control_plane.iterdir() if path.is_file()}
    assert after == before
    assert not (control_plane / "legacy_import").exists()


def _control_plane_tree(control_plane: Path) -> dict[str, tuple[int, bytes]]:
    return {
        path.relative_to(control_plane).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in control_plane.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "mutation",
    ("payload", "previous_hash", "after_state", "state_hash", "tail", "checkpoint"),
)
def test_g5_hash_audit_rejects_tamper_without_mutation(tmp_path: Path, mutation: str) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    if mutation in {"payload", "previous_hash", "after_state", "state_hash"}:
        event_path = control_plane / "coordinator_events.jsonl"
        event = json.loads(event_path.read_text())
        if mutation == "payload":
            event["payload"]["tampered"] = True
        elif mutation == "previous_hash":
            event["previous_event_hash"] = "0" * 64
        elif mutation == "after_state":
            event["after_state"]["phase"] = "tampered"
        else:
            event["state_hash"] = "f" * 64
        event_path.write_bytes(_legacy_json_bytes(event))
    else:
        state_path = control_plane / "coordinator_state.json"
        state = json.loads(state_path.read_text())
        if mutation == "tail":
            state["last_event_hash"] = "f" * 64
        else:
            state["last_event_seq"] = 0
        state_path.write_bytes(_legacy_json_bytes(state))

    before = _control_plane_tree(control_plane)
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]
    with pytest.raises(CoordinatorIntegrityError):
        RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path))
    assert _control_plane_tree(control_plane) == before


def test_publish_and_rebind_is_quiescent_and_recoverable(tmp_path: Path) -> None:
    run_id = "RUN-PUBLISH-REBIND"
    context = RunContext(run_id, tmp_path)
    planner = QueuePlanner(())
    coordinator = RunCoordinator(context, planner_provider=planner)
    first = _spec(run_id)
    second = _rebound_spec(run_id, "G-0002", "planner://g2")
    coordinator.start(first)
    seen: list[str] = []

    def fail_started(name: str) -> None:
        if name == "plan_rebind_after_started":
            raise RuntimeError(name)

    coordinator._failpoint = fail_started
    with pytest.raises(RuntimeError):
        coordinator.publish_and_rebind(second, lambda spec: seen.append(spec.generation_id))
    coordinator.close()

    restarted = RunCoordinator.from_persisted_spec(context, planner_provider=planner)
    pending = restarted.status()
    assert pending.status == "waiting"
    assert pending.phase == "plan_rebind_pending"
    assert planner.calls == 0
    assert restarted.step().phase == "plan_rebind_pending"

    restarted._failpoint = None
    rebound = restarted.publish_and_rebind(second, lambda spec: seen.append(spec.generation_id))
    assert rebound.generation_id == "G-0002"
    assert seen == ["G-0002"]
    assert [event["event"] for event in (
        json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    )][-2:] == ["plan_rebind_started", "plan_rebound"]
    assert restarted.publish_and_rebind(second, lambda spec: seen.append("duplicate")).last_event_seq == rebound.last_event_seq
    assert seen == ["G-0002"]


def _rotated_pending_plan_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failpoint: str = "plan_rebind_after_started",
) -> tuple[RunCoordinator, CoordinatorRunSpec, CoordinatorRunSpec, Path]:
    """Create a pending plan rebind whose source skill is rotated in place."""

    skill_path = _install_test_skill(tmp_path, monkeypatch)
    run_id = "RUN-PENDING-ROTATED"
    context = RunContext(run_id, tmp_path)
    old_binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    source = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://g1",
        hashlib.sha256(b"planner://g1").hexdigest(),
        codex_exec={"binary": "fake-codex", **old_binding},
    )
    old_target = CoordinatorRunSpec(
        run_id,
        "G-0002",
        "planner://g2",
        hashlib.sha256(b"planner://g2").hexdigest(),
        codex_exec={"binary": "fake-codex", **old_binding},
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(source)

    def fail_started(name: str) -> None:
        if name == failpoint:
            raise RuntimeError(name)

    coordinator._failpoint = fail_started
    with pytest.raises(RuntimeError, match=failpoint):
        coordinator.publish_and_rebind(old_target, lambda _spec: None)
    coordinator._failpoint = None

    # Keep the canonical path but rotate its release bytes.  The old binding
    # remains in the raw coordinator spec and is no longer loadable through a
    # production Codex adapter.
    (skill_path / "README.md").write_text("rotated release fixture\n", encoding="utf-8")
    new_hash = hashlib.sha256(coordinator_module._skill_release_bytes(skill_path)).hexdigest()
    monkeypatch.setattr(coordinator_module, "PRODUCTION_SKILL_SHA256", new_hash)
    new_binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    target = CoordinatorRunSpec(
        run_id,
        old_target.generation_id,
        old_target.planner_ref,
        old_target.planner_hash,
        codex_exec={"binary": "fake-codex", **new_binding},
    )
    return coordinator, old_target, target, tmp_path / "control_plane"


def test_pending_plan_rebind_retargets_rotated_skill_and_finishes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, old_target, target, control_plane = _rotated_pending_plan_fixture(tmp_path, monkeypatch)
    published: list[str] = []

    recovered = coordinator.publish_and_rebind(target, lambda spec: published.append(spec.generation_id))

    assert recovered.status == "ready"
    assert published == [target.generation_id]
    assert coordinator.persisted_spec() == target
    assert not coordinator.transport_rebind_intent_path.exists()
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events][-2:] == [
        "plan_rebind_transport_retargeted",
        "plan_rebound",
    ]
    retargeted = next(event for event in events if event["event"] == "plan_rebind_transport_retargeted")
    assert retargeted["payload"]["old_new_spec_hash"] == coordinator._spec_hash(old_target)
    assert retargeted["payload"]["new_spec_hash"] == coordinator._spec_hash(target)
    assert events[-1]["event"] == "plan_rebound"


def test_pending_plan_rebind_recovers_after_spec_crash_and_skill_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart accepts the durable window after spec write but before state."""

    coordinator, old_target, target, control_plane = _rotated_pending_plan_fixture(
        tmp_path,
        monkeypatch,
        failpoint="plan_rebind_after_spec",
    )
    raw_spec = json.loads((control_plane / "coordinator_spec.json").read_text())
    state = json.loads((control_plane / "coordinator_state.json").read_text())
    pending = state["pending_plan_rebind"]
    assert raw_spec["generation_id"] == old_target.generation_id
    assert state["generation_id"] != raw_spec["generation_id"]
    assert pending["old_spec_hash"] == state["spec_hash"]
    assert pending["new_spec_hash"] == coordinator._spec_hash(old_target)

    coordinator.close()
    restarted = RunCoordinator(coordinator.context, planner_provider=QueuePlanner(()))
    published: list[str] = []
    recovered = restarted.publish_and_rebind(target, lambda spec: published.append(spec.generation_id))

    assert recovered.status == "ready"
    assert published == [target.generation_id]
    assert restarted.persisted_spec() == target
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events][-2:] == [
        "plan_rebind_transport_retargeted",
        "plan_rebound",
    ]


def test_pending_plan_rebind_rejects_mismatched_rotated_target_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _old_target, target, control_plane = _rotated_pending_plan_fixture(tmp_path, monkeypatch)
    wrong_target = CoordinatorRunSpec(
        target.run_id,
        "G-0003",
        "planner://g3",
        hashlib.sha256(b"planner://g3").hexdigest(),
        codex_exec=target.codex_exec,
    )
    before = _control_plane_tree(control_plane)

    with pytest.raises(CoordinatorConflictError, match="different plan rebind"):
        coordinator.publish_and_rebind(wrong_target, lambda _spec: pytest.fail("publisher must not run"))

    assert _control_plane_tree(control_plane) == before


def test_pending_plan_rebind_publisher_failure_after_retarget_converges_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _old_target, target, control_plane = _rotated_pending_plan_fixture(tmp_path, monkeypatch)
    calls = 0

    def publish(_spec: CoordinatorRunSpec) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("publisher unavailable")

    deferred = coordinator.publish_and_rebind(target, publish)
    assert deferred.status == "waiting"
    assert deferred.phase == "plan_rebind_pending"
    pending = json.loads((control_plane / "coordinator_state.json").read_text())["pending_plan_rebind"]
    assert pending["new_spec_hash"] == coordinator._spec_hash(target)
    events_before = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events_before].count("plan_rebind_transport_retargeted") == 1

    recovered = coordinator.publish_and_rebind(target, publish)
    assert recovered.status == "ready"
    assert calls == 2
    assert coordinator.persisted_spec() == target
    events_after = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events_after].count("plan_rebind_transport_retargeted") == 1
    assert [event["event"] for event in events_after].count("plan_rebound") == 1


@pytest.mark.parametrize(
    "failpoint",
    (
        "legacy_import_after_intent",
        "legacy_import_after_stage_coordinator_spec.json",
        "legacy_import_after_stage_coordinator_state.json",
        "legacy_import_after_stage_coordinator_events.jsonl",
        "legacy_import_after_archive_rename",
        "legacy_import_after_flat_spec",
        "legacy_import_after_state",
        "legacy_import_after_events",
    ),
)
def test_legacy_import_failpoint_recovery_converges(tmp_path: Path, failpoint: str) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]

    def inject(name: str) -> None:
        if name == failpoint:
            raise BaseException(name)

    crashed = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()), failpoint=inject
    )
    with pytest.raises(BaseException):
        crashed.reopen("crash import")

    recovered = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path), planner_provider=QueuePlanner(())
    )
    assert recovered.status().status == "ready"
    archive = control_plane / "legacy_import"
    for name in ("coordinator_spec.json", "coordinator_state.json", "coordinator_events.jsonl"):
        assert (archive / name).read_bytes() == original[name]
    assert not (control_plane / ".legacy_import.intent.json").exists()
    assert not (control_plane / ".legacy_import.staging").exists()
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["legacy_imported"]


def _seed_archive_with_interrupted_import(tmp_path: Path) -> tuple[Path, dict[str, bytes], str]:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]

    def inject(name: str) -> None:
        if name == "legacy_import_after_archive_rename":
            raise BaseException(name)

    crashed = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()), failpoint=inject
    )
    with pytest.raises(BaseException):
        crashed.reopen("archive then crash")
    return control_plane, original, run_id


def test_archive_with_rogue_staging_fails_closed_without_mutation(tmp_path: Path) -> None:
    control_plane, _original, run_id = _seed_archive_with_interrupted_import(tmp_path)
    staging = control_plane / ".legacy_import.staging"
    staging.mkdir()
    (staging / "rogue").write_bytes(b"unrelated")
    before = _control_plane_tree(control_plane)
    with pytest.raises(CoordinatorIntegrityError):
        RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    assert _control_plane_tree(control_plane) == before


def test_archive_with_owned_partial_staging_recovers_and_cleans_only_staging(tmp_path: Path) -> None:
    control_plane, original, run_id = _seed_archive_with_interrupted_import(tmp_path)
    staging = control_plane / ".legacy_import.staging"
    staging.mkdir()
    (staging / "coordinator_spec.json").write_bytes(original["coordinator_spec.json"])
    recovered = RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    assert recovered.status().status == "ready"
    assert not staging.exists()
    assert not (control_plane / ".legacy_import.intent.json").exists()
    for name in ("coordinator_spec.json", "coordinator_state.json", "coordinator_events.jsonl"):
        assert (control_plane / "legacy_import" / name).read_bytes() == original[name]


def test_live_pid_claim_allows_exactly_one_multiprocess_submit(tmp_path: Path) -> None:
    context = RunContext("RUN-MP-CLAIM", tmp_path)
    action = _action("analyze_requirement", "REQ-MP")
    planner = QueuePlanner((action,))
    coordinator = RunCoordinator(context, planner_provider=planner)
    coordinator.start(_spec(context.run_id))
    key = _seed_active_dispatch(coordinator, action)

    mp_context = mp.get_context("fork")
    barrier = mp_context.Barrier(2)
    release = mp_context.Event()
    marker = tmp_path / "role-submissions.log"
    processes = [
        mp_context.Process(
            target=_claim_process_worker,
            args=(str(tmp_path), context.run_id, f"worker-{index}", action, barrier, release, str(marker)),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    # Opening the append file precedes the worker's write. Wait for the
    # submission, not merely the directory entry, and always release workers
    # even when an assertion fails so the test cannot leak blocked processes.
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text().splitlines():
                break
            time.sleep(0.02)
        assert marker.exists()
        assert len(marker.read_text().splitlines()) == 1
    finally:
        release.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)
    submissions = marker.read_text().splitlines()
    assert len(submissions) == 1
    assert submissions[0].split(":", 1)[1] == key


def test_unknown_live_active_claim_is_not_silently_adopted(tmp_path: Path) -> None:
    context = RunContext("RUN-UNKNOWN-LIVE-CLAIM", tmp_path)
    action = _action("analyze_requirement", "REQ-UNKNOWN-LIVE")
    seen: list[str] = []
    seed = RunCoordinator(context, planner_provider=QueuePlanner(()))
    seed.start(_spec(context.run_id))
    _seed_active_dispatch(seed, action, runner_pid=os.getpid())
    seed.close(wait_for_roles=True)

    restarted = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={
            "analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"])
            or RoleExecution()
        },
    )

    status = restarted.step()

    assert status.status == "dispatching"
    assert status.active_dispatches
    assert seen == []


def test_dead_pid_adopts_active_slot_with_same_idempotency_key(tmp_path: Path) -> None:
    context = RunContext("RUN-DEAD-CLAIM", tmp_path)
    action = _action("analyze_requirement", "REQ-DEAD")
    seen: list[str] = []
    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={"analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"]) or RoleExecution()},
    )
    coordinator.start(_spec(context.run_id))
    key = _seed_active_dispatch(coordinator, action, runner_pid=999_999)
    resumed = coordinator.step()
    assert resumed.status == "waiting"
    assert seen == [key]


def test_restart_reconciles_unoffered_active_slot_before_submit(tmp_path: Path) -> None:
    context = RunContext("RUN-RESTART-STALE", tmp_path)
    action = _action("analyze_requirement", "REQ-STALE")
    seed = RunCoordinator(context, planner_provider=QueuePlanner(()))
    seed.start(_spec(context.run_id))
    key = _seed_active_dispatch(seed, action, runner_pid=999_999)
    seen: list[str] = []
    planner = QueuePlanner(())
    restarted = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={
            "analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"])
            or RoleExecution()
        },
    )

    status = restarted.step()

    assert status.status == "waiting"
    assert status.active_dispatches == ()
    assert seen == []
    assert planner.calls == 1
    events = [
        json.loads(line)
        for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    ]
    assert any(
        event["event"] == "planner_advanced"
        and event["payload"]["reason"] == "restart_active_not_offered"
        and event["payload"]["removed_dispatches"][0]["idempotency_key"] == key
        for event in events
    )


def test_restart_adopts_still_offered_active_slot_once(tmp_path: Path) -> None:
    context = RunContext("RUN-RESTART-OFFERED", tmp_path)
    action = _action("analyze_requirement", "REQ-OFFERED")
    seed = RunCoordinator(context, planner_provider=QueuePlanner(()))
    seed.start(_spec(context.run_id))
    key = _seed_active_dispatch(seed, action, runner_pid=999_999)
    seen: list[str] = []
    planner = QueuePlanner((action,), ())
    restarted = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={
            "analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"])
            or RoleExecution()
        },
    )

    status = restarted.step()

    assert status.status == "waiting"
    assert seen == [key]
    assert planner.calls == 2


def test_reopen_clears_only_dead_claims(tmp_path: Path) -> None:
    context = RunContext("RUN-REOPEN-CLAIM", tmp_path)
    action = _action("analyze_requirement", "REQ-REOPEN")
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner((action,)))
    coordinator.start(_spec(context.run_id))
    _seed_active_dispatch(coordinator, action, runner_pid=999_999)
    reopened = coordinator.reopen("clear dead claim")
    assert reopened.status == "dispatching"
    assert reopened.active_dispatches[0].get("runner_pid") is None
    assert reopened.active_dispatches[0].get("runner_id") is None
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "dispatch_claims_cleared"


def _admit_refresh(context: RunContext, parent: Any, revision: Any, coordinator: RunCoordinator) -> Any:
    lifecycle = RunLifecycle.load(context)
    return DataRevisionStore(context).admit_pending_data_refresh(
        data_revision=revision,
        plan=parent.to_dict(),
        reopened_item_ids=("REQ-01",),
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash=lifecycle.snapshot.manifest_hash,
        expected_parent_plan_hash=hashlib.sha256(lifecycle.plan_path.read_bytes()).hexdigest(),
        launch_draft_id="DRAFT-REFRESH",
        launch_fingerprint="a" * 64,
        created_at="2026-08-26T00:00:00Z",
        data_revision_ref="data_room/revisions/D-0002/revision_manifest.json",
    )


def _expanded_refresh_plan(parent: RequirementExecutionPlan) -> RequirementExecutionPlan:
    added = RequirementRecord(
        requirement_id="REQ-02",
        original_text="Investigate REQ-02.",
        business_objective="Support REQ-02",
        expected_analytical_outputs=("output-REQ-02",),
    )
    return RequirementExecutionPlan(
        input_records=(*parent.input_records, added),
        groups=(RequirementExecutionGroup(("REQ-01", "REQ-02"), "Original route."),),
        planner_ref=parent.planner_ref,
        portfolio_strategy=parent.portfolio_strategy,
        revision=parent.revision + 1,
    )


def test_data_refresh_active_attempt_defers_without_plan_rebind_trap(tmp_path: Path) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    item = ItemWorkspace.load(context, "REQ-01", mode="requirement")
    attempt = item.begin_attempt("owner", "analysis")
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    pending = _admit_refresh(context, parent, revision, coordinator)

    status = coordinator.consume_pending_data_refresh()

    assert status.phase == "data_refresh_pending"
    assert status.generation_id == "G-0001"
    assert store.pending_data_refresh().intent_hash == pending.intent_hash
    state = json.loads((tmp_path / "run/control_plane/coordinator_state.json").read_text())
    assert state["pending_plan_rebind"] is None
    assert all("error" not in diagnostic for diagnostic in status.diagnostics)

    item.finish_attempt(attempt.attempt_id, status="interrupted", error="host_interruption")
    recovered = coordinator.consume_pending_data_refresh()
    assert recovered.generation_id == "G-0002"
    assert store.pending_data_refresh() is None


def test_root_g1_without_product_remains_pending_before_data_refresh(tmp_path: Path) -> None:
    context, parent, revision = _base_refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    pending = _admit_refresh(context, parent, revision, coordinator)

    status = coordinator.consume_pending_data_refresh()

    assert status.phase == "waiting_product"
    assert status.generation_id == "G-0001"
    assert store.pending_data_refresh().intent_hash == pending.intent_hash
    assert RunLifecycle.load(context).generation_metadata is None


def test_root_g1_valid_product_allows_first_data_refresh(tmp_path: Path) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    _admit_refresh(context, parent, revision, coordinator)

    status = coordinator.consume_pending_data_refresh()

    assert status.generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_metadata.data_revision_hash == revision.manifest_hash
    assert store.pending_data_refresh() is None


def test_append_successor_transaction_leaves_stale_pending_in_recovery_until_exact_admit(tmp_path: Path) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    _admit_refresh(context, parent, revision, coordinator)

    third_archive = tmp_path / "inputs" / "three-transaction.zip"
    with zipfile.ZipFile(third_archive, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("orders.csv", "id,value\n1,three\n")
    d3 = store.append(
        third_archive,
        expected_current_revision_id=revision.revision_id,
        expected_current_manifest_hash=revision.manifest_hash,
        transaction={
            "launch_draft_id": "DRAFT-SUCCESSOR-TX",
            "launch_fingerprint": "b" * 64,
            "created_at": "2026-08-26T00:00:01Z",
        },
    )

    # The old D2 admission is immutable audit, but no longer current after the
    # crash-window D3 pointer swap. Coordinator projects recovery rather than
    # raising a technical CAS error or starting a second generation.
    status = coordinator.step()
    assert status.status == "waiting"
    assert status.phase == "data_revision_recovery"
    assert status.diagnostics[-1]["kind"] == "data_revision_recovery"
    assert status.diagnostics[-1]["pending_revision_id"] == revision.revision_id
    assert store.pending_data_refresh(allow_stale=True).data_revision_id == revision.revision_id
    assert RunLifecycle.load(context).generation_id == "G-0001"

    lifecycle = RunLifecycle.load(context)
    recovered_pending = store.admit_pending_data_refresh(
        data_revision=d3,
        plan=parent.to_dict(),
        reopened_item_ids=("REQ-01",),
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash=lifecycle.snapshot.manifest_hash,
        expected_parent_plan_hash=hashlib.sha256(lifecycle.plan_path.read_bytes()).hexdigest(),
        launch_draft_id="DRAFT-SUCCESSOR-TX",
        launch_fingerprint="b" * 64,
        created_at="2026-08-26T00:00:01Z",
        data_revision_ref="data_room/revisions/D-0003/revision_manifest.json",
    )
    assert recovered_pending.data_revision_id == "D-0003"
    assert store.revision_transaction() is None
    recovered = coordinator.consume_pending_data_refresh()
    assert recovered.generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_metadata.data_revision_hash == d3.manifest_hash
    assert store.pending_data_refresh() is None


def test_revision_recovery_keeps_current_generation_planner_work_running(tmp_path: Path) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    seen: list[str] = []
    action = _action("analyze_requirement", "REQ-01")
    planner = QueuePlanner((action,), ())
    coordinator = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={
            "analyze_requirement": lambda _context, **kwargs: seen.append(kwargs["idempotency_key"]) or RoleExecution(),
        },
    )
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    _admit_refresh(context, parent, revision, coordinator)
    third_archive = tmp_path / "inputs" / "three-planner-recovery.zip"
    with zipfile.ZipFile(third_archive, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("orders.csv", "id,value\n1,three\n")
    store.append(
        third_archive,
        expected_current_revision_id=revision.revision_id,
        expected_current_manifest_hash=revision.manifest_hash,
        transaction={
            "launch_draft_id": "DRAFT-PLANNER-RECOVERY",
            "launch_fingerprint": "d" * 64,
            "created_at": "2026-08-26T00:00:02Z",
        },
    )

    status = coordinator.step()
    assert seen
    assert status.phase == "data_revision_recovery"
    assert status.status == "waiting"
    assert RunLifecycle.load(context).generation_id == "G-0001"


def test_data_refresh_superseded_pointer_clears_temporary_rebind_and_retries_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    lifecycle = RunLifecycle.load(context)
    parent_state_hash = lifecycle.snapshot.manifest_hash
    parent_plan_hash = hashlib.sha256(lifecycle.plan_path.read_bytes()).hexdigest()
    _admit_refresh(context, parent, revision, coordinator)

    inputs = tmp_path / "inputs"
    third_archive = inputs / "three.zip"
    with zipfile.ZipFile(third_archive, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("orders.csv", "id,value\n1,three\n")
    started = threading.Event()
    release = threading.Event()
    original = RequirementRunExtension.refresh_data

    def pause_before_refresh(context_arg: RunContext, *args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(5)
        return original(context_arg, *args, **kwargs)

    monkeypatch.setattr(
        RequirementRunExtension,
        "refresh_data",
        staticmethod(pause_before_refresh),
    )
    outcomes: list[Any] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            outcomes.append(coordinator.consume_pending_data_refresh())
        except BaseException as exc:  # noqa: BLE001 - thread evidence is asserted below
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    assert started.wait(5)

    d3 = store.append(
        third_archive,
        expected_current_revision_id=revision.revision_id,
        expected_current_manifest_hash=revision.manifest_hash,
    )
    store.admit_pending_data_refresh(
        data_revision=d3,
        plan=_expanded_refresh_plan(parent).to_dict(),
        reopened_item_ids=("REQ-01",),
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash=parent_state_hash,
        expected_parent_plan_hash=parent_plan_hash,
        launch_draft_id="DRAFT-REFRESH",
        launch_fingerprint="a" * 64,
        created_at="2026-08-26T00:00:00Z",
        data_revision_ref="data_room/revisions/D-0003/revision_manifest.json",
    )
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert outcomes and outcomes[0].phase == "data_refresh_pending"
    assert outcomes[0].generation_id == "G-0001"
    assert store.pending_data_refresh().data_revision_id == "D-0003"
    state = json.loads((tmp_path / "run/control_plane/coordinator_state.json").read_text())
    assert state["pending_plan_rebind"] is None
    assert not (tmp_path / "run/extensions/G-0002").exists()

    monkeypatch.setattr(RequirementRunExtension, "refresh_data", original)
    recovered = coordinator.consume_pending_data_refresh()
    assert recovered.generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_metadata.data_revision_hash == d3.manifest_hash
    assert store.pending_data_refresh() is None


def test_data_refresh_successor_after_pointer_before_mark_waits_for_product_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    lifecycle = RunLifecycle.load(context)
    parent_state_hash = lifecycle.snapshot.manifest_hash
    parent_plan_hash = hashlib.sha256(lifecycle.plan_path.read_bytes()).hexdigest()
    first_pending = _admit_refresh(context, parent, revision, coordinator)

    third_archive = tmp_path / "inputs" / "three-after-mark.zip"
    with zipfile.ZipFile(third_archive, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("orders.csv", "id,value\n1,three\n")
    original_mark = DataRevisionStore.mark_pending_data_refresh_applied
    injected = {"value": False}

    def append_successor(self: DataRevisionStore, intent_hash: str, *, generation_id: str) -> Any:
        if not injected["value"]:
            injected["value"] = True
            successor = self.append(
                third_archive,
                expected_current_revision_id=revision.revision_id,
                expected_current_manifest_hash=revision.manifest_hash,
            )
            self.admit_pending_data_refresh(
                data_revision=successor,
                plan=_expanded_refresh_plan(parent).to_dict(),
                reopened_item_ids=("REQ-01",),
                expected_parent_generation_id="G-0001",
                expected_parent_state_hash=parent_state_hash,
                expected_parent_plan_hash=parent_plan_hash,
                launch_draft_id="DRAFT-REFRESH",
                launch_fingerprint="a" * 64,
                created_at="2026-08-26T00:00:00Z",
                data_revision_ref="data_room/revisions/D-0003/revision_manifest.json",
            )
        return original_mark(self, intent_hash, generation_id=generation_id)

    monkeypatch.setattr(DataRevisionStore, "mark_pending_data_refresh_applied", append_successor)
    first_applied = coordinator.consume_pending_data_refresh()

    assert first_applied.generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_id == "G-0002"
    successor_pending = store.pending_data_refresh()
    assert successor_pending is not None
    assert successor_pending.data_revision_id == "D-0003"
    assert successor_pending.expected_parent_generation_id == "G-0002"
    # A second D cannot supersede G2 in the same safe-boundary call: G2 must
    # first publish its own generation product for G3's parent lineage.
    _write_valid_extension_product(context, "G-0002")
    recovered = coordinator.consume_pending_data_refresh()
    assert recovered.generation_id == "G-0003"
    assert RunLifecycle.load(context).generation_id == "G-0003"
    assert store.pending_data_refresh() is None
    assert RunLifecycle.load(context).generation_metadata.data_revision_ref.endswith("D-0003/revision_manifest.json")
    assert first_pending.intent_hash != ""


def test_data_refresh_rebases_after_real_plan_generation_and_preserves_admission_provenance(
    tmp_path: Path,
) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    admitted = _admit_refresh(context, parent, revision, coordinator)

    # Existing contracts publish a real current-plan generation before the
    # pending data admission reaches its safe boundary.
    revised = RequirementRunExtension.revise(context, plan=_expanded_refresh_plan(parent))
    assert revised.generation_id == "G-0002"
    _write_valid_extension_product(context, "G-0002")

    status = coordinator.consume_pending_data_refresh()
    assert status.generation_id == "G-0003"
    assert store.pending_data_refresh() is None
    current_plan = RequirementSupervisorWorkspace(context).load()
    assert tuple(record.requirement_id for record in current_plan.input_records) == ("REQ-01", "REQ-02")

    # The applied receipt retains the original G1 parent CAS even though the
    # canonical pending bytes were rebased to G2 before publication.
    receipts = list(store.pending_data_refresh_archive_root.glob("*.json"))
    assert receipts
    receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
    assert receipt["original_parent_generation_id"] == admitted.expected_parent_generation_id
    assert receipt["original_parent_state_hash"] == admitted.expected_parent_state_hash
    assert receipt["original_parent_plan_hash"] == admitted.expected_parent_plan_hash


@pytest.mark.parametrize(
    "crash_point",
    ("after_g_before_spec", "after_spec_before_state", "after_state_before_applied"),
)
def test_data_refresh_restart_recovery_converges_across_rebind_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    _admit_refresh(context, parent, revision, coordinator)

    if crash_point == "after_g_before_spec":
        original = RequirementRunExtension.refresh_data

        def crash_after_generation(context_arg: RunContext, *args: Any, **kwargs: Any) -> Any:
            result = original(context_arg, *args, **kwargs)
            raise BaseException("after generation publication")

        monkeypatch.setattr(
            RequirementRunExtension,
            "refresh_data",
            staticmethod(crash_after_generation),
        )
    else:
        failpoint_name = {
            "after_spec_before_state": "data_refresh_after_spec",
            "after_state_before_applied": "data_refresh_before_applied",
        }[crash_point]

        def failpoint(name: str) -> None:
            if name == failpoint_name:
                raise BaseException(name)

        coordinator._failpoint = failpoint

    with pytest.raises(BaseException):
        coordinator.consume_pending_data_refresh()
    if crash_point == "after_g_before_spec":
        monkeypatch.setattr(RequirementRunExtension, "refresh_data", original)

    restarted = RunCoordinator.from_persisted_spec(
        context,
        planner_provider=QueuePlanner(()),
    )
    recovered = restarted.consume_pending_data_refresh()

    assert recovered.generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_id == "G-0002"
    assert store.pending_data_refresh() is None
    state = json.loads((tmp_path / "run/control_plane/coordinator_state.json").read_text())
    assert state["pending_plan_rebind"] is None


def test_data_refresh_runtime_failpoint_retains_rebind_for_exact_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent, revision = _refresh_fixture(tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    store = DataRevisionStore(context)
    _admit_refresh(context, parent, revision, coordinator)
    original = run_extension_module.RequirementRunExtension._refresh_failpoint

    def failpoint(name: str) -> None:
        if name == "after_intent":
            raise RuntimeError("refresh failpoint")

    monkeypatch.setattr(
        run_extension_module.RequirementRunExtension,
        "_refresh_failpoint",
        staticmethod(failpoint),
    )
    pending_status = coordinator.consume_pending_data_refresh()
    assert pending_status.phase == "plan_rebind_pending"
    assert store.pending_data_refresh() is not None
    state = json.loads((tmp_path / "run/control_plane/coordinator_state.json").read_text())
    assert state["pending_plan_rebind"] is not None

    monkeypatch.setattr(
        run_extension_module.RequirementRunExtension,
        "_refresh_failpoint",
        original,
    )
    recovered = coordinator.consume_pending_data_refresh()
    assert recovered.generation_id == "G-0002"
    assert store.pending_data_refresh() is None
