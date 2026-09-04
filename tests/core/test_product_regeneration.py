from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
from dataclasses import replace

import pytest

import auto_foundry_core.coordinator as coordinator_module
from auto_foundry_core import (
    CoordinatorConflictError,
    CoordinatorIntegrityError,
    CoordinatorRunSpec,
    PlannerAction,
    ProductCandidate,
    ProductReviewStore,
    RoleExecution,
    RunContext,
    RunCoordinator,
    RunLifecycle,
)
from auto_foundry_core.requirement_planning import (
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementSupervisorWorkspace,
)
from auto_foundry_core.cli import build_parser
from auto_foundry_core.product_review import canonical_hash
from auto_foundry_core.requirement_planning import preview_input_fingerprint
from auto_foundry_core.coordinator import RoleSessionRegistry


def _phase_view() -> dict[str, object]:
    input_fingerprint = "a" * 64
    manifest_hash = "b" * 64
    content_hash = "c" * 64
    phase = {
        "product": {
            "preview_item_ids": ["REQ-001"],
            "preview_item_bindings": {
                "REQ-001": {
                    "accepted_manifest_ref": "requirements/REQ-001/accepted/manifest.json",
                    "accepted_manifest_hash": manifest_hash,
                    "accepted_content_ref": "requirements/REQ-001/accepted/answer_content.json",
                    "accepted_content_hash": content_hash,
                }
            },
            "preview_input_fingerprint": input_fingerprint,
            "manifest_ref": "products/generations/G-0001/product_manifest.json",
            "candidate_ref": "products/generations/G-0001/product_candidate.json",
            "review_ref": "products/generations/G-0001/product_review.json",
            "authorization_ref": "products/generations/G-0001/publish_authorization.json",
            "presentation_inventory_ref": "extensions/G-0001/dashboard_preflight/dashboard_fixture_v4.json",
            "presentation_plan_ref": "extensions/G-0001/business_presentation_plan.json",
            "preview_failed_items": ["REQ-008"],
            "preview_limitations": ["REQ-008 integration is a terminal technical limitation"],
        },
        "items": {
            "REQ-001": {
                "terminal_outcome": {"status": "accepted"},
                "accepted_business_validation": {
                    "valid": True,
                    "stage": "accepted",
                    "manifest_ref": "requirements/REQ-001/accepted/manifest.json",
                    "manifest_hash": manifest_hash,
                    "content_ref": "requirements/REQ-001/accepted/answer_content.json",
                    "content_hash": content_hash,
                },
            }
        },
    }
    product = phase["product"]
    items = phase["items"]
    assert isinstance(product, dict) and isinstance(items, dict)
    product["preview_input_fingerprint"] = preview_input_fingerprint(items)
    return phase


def _regeneration_action(run_id: str, phase: dict[str, object], state: dict[str, object]) -> PlannerAction:
    request = state["product_regeneration"]
    assert isinstance(request, dict)
    product = phase["product"]
    assert isinstance(product, dict)
    item_ids = tuple(product["preview_item_ids"])
    bindings = product["preview_item_bindings"]
    assert isinstance(bindings, dict)
    reason = str(request["reason"])
    revision_id = request.get("revision_id")
    output_root_ref = (
        f"products/generations/G-0001/product_revisions/{revision_id}/artifacts"
        if isinstance(revision_id, str)
        else None
    )
    metadata = {
        "generation_id": "G-0001",
        "product_manifest_ref": (
            f"{output_root_ref}/product_manifest.json"
            if output_root_ref
            else product["manifest_ref"]
        ),
        "output_root_ref": output_root_ref,
        "candidate_ref": (
            f"products/generations/G-0001/product_revisions/{revision_id}/product_candidate.json"
            if isinstance(revision_id, str)
            else product["candidate_ref"]
        ),
        "review_ref": (
            f"products/generations/G-0001/product_revisions/{revision_id}/product_review.json"
            if isinstance(revision_id, str)
            else product["review_ref"]
        ),
        "authorization_ref": (
            f"products/generations/G-0001/product_revisions/{revision_id}/publish_authorization.json"
            if isinstance(revision_id, str)
            else product["authorization_ref"]
        ),
        "presentation_inventory_ref": product["presentation_inventory_ref"],
        "presentation_plan_ref": product["presentation_plan_ref"],
        "input_fingerprint": product["preview_input_fingerprint"],
        "item_ids": list(item_ids),
        "item_bindings": dict(bindings),
        "failed_items": list(product["preview_failed_items"]),
        "limitations": list(product["preview_limitations"]),
        "authorization_origin": "operator_product_regeneration",
        "product_regeneration_request_id": request["request_id"],
        "product_revision_id": revision_id,
        "prior_revision_id": request.get("prior_revision_id"),
    }
    for field_name in (
        "predecessor_product_review_ref",
        "predecessor_product_review_hash",
    ):
        if request.get(field_name) is not None:
            metadata[field_name] = request[field_name]
    return PlannerAction(
        "build_product_candidate",
        "product_agent",
        run_id,
        f"operator requested Product regeneration: {reason}",
        priority=59,
        metadata=metadata,
    )


def _coordinator(
    tmp_path: Path,
    *,
    planner=None,
    adapters=None,
) -> tuple[RunCoordinator, dict[str, object]]:
    run_id = "RUN-PRODUCT-REGENERATION"
    context = RunContext(run_id, tmp_path)
    RunLifecycle.create(context, ["REQ-001"], mode="requirement")
    coordinator = RunCoordinator(
        context,
        planner=planner,
        adapters=adapters,
    )
    coordinator.start(
        CoordinatorRunSpec(
            run_id,
            "G-0001",
            "planner://product-regeneration-test",
            hashlib.sha256(b"product-regeneration-planner").hexdigest(),
        )
    )
    phase = _phase_view()
    coordinator._phase_snapshot = lambda: phase  # type: ignore[method-assign]
    return coordinator, phase


def _seed_reviewed_product(context: RunContext, *, verdict: str = "accept") -> tuple[ProductReviewStore, ProductCandidate]:
    """Create one valid root candidate/review for revision supersession tests."""

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
        path.write_text(json.dumps({"name": name}, sort_keys=True) + "\n", encoding="utf-8")
        outputs[name] = path
    site = product_root / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text("<html>reviewed</html>\n", encoding="utf-8")
    outputs["site"] = site
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
        product_owner="product-agent",
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
    store.record_review(
        reviewer_ref="product-reviewer",
        verdict=verdict,
        reviewed_at="2026-01-01T00:00:00Z",
    )
    return store, persisted


def _materialize_revision_candidate(
    store: ProductReviewStore,
    revision_id: str,
    source: ProductCandidate,
) -> ProductCandidate:
    root = store.revision_artifacts_root(revision_id)
    root.mkdir(parents=True, exist_ok=True)
    filenames = {
        "manifest": "product_manifest.json",
        "fixture": "dashboard_fixture_v4.json",
        "chart_map": "dashboard_chart_map_v4.json",
        "chart_registry": "dashboard_chart_registry_v4.json",
        "blueprint": "dashboard_blueprint_v2.json",
        "receipt": "build_receipt.json",
    }
    bindings: dict[str, dict[str, str]] = {}
    for name, filename in filenames.items():
        source_path = store.context.resolve_run_path(source.artifact_bindings[name]["ref"])
        target = root / filename
        target.write_bytes(source_path.read_bytes())
        bindings[name] = {"ref": str(target.relative_to(store.context.run_root))}
    source_site = store.context.resolve_run_path(source.artifact_bindings["site"]["ref"])
    target_site = root / "site"
    shutil.copytree(source_site, target_site)
    bindings["site"] = {"ref": str(target_site.relative_to(store.context.run_root))}
    return replace(source, artifact_bindings=bindings, candidate_hash=None)


def test_product_regeneration_is_bound_idempotent_and_dispatches_once(tmp_path: Path) -> None:
    calls: list[PlannerAction] = []
    phase_holder: dict[str, dict[str, object]] = {}

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        if not isinstance(state.get("product_regeneration"), dict):
            return ()
        # Simulate a stale/mixed Planner snapshot: coordinator admission must
        # keep intentional regeneration Product-only even if integration work
        # is still offered by the provider.
        return (
            _regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),
            PlannerAction(
                "integrate_requirement",
                "integration_agent",
                "REQ-001",
                "unfinished integration sibling",
                priority=10,
            ),
        )

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action)
        return RoleExecution()

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={"build_product_candidate": transport},
    )
    phase_holder["value"] = phase
    try:
        requested = coordinator.regenerate_product(
            reason="refresh the reviewed dashboard",
            idempotency_key="product-regeneration-request-1",
        )
        assert requested.status == "ready"
        persisted = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
        assert persisted["product_regeneration"]["status"] == "requested"
        assert persisted["product_regeneration"]["request_id"] == "product-regeneration-request-1"
        assert persisted["product_regeneration"]["prior_candidate_hash"] is None
        assert "predecessor_product_review_ref" not in persisted["product_regeneration"]
        assert "predecessor_product_review_hash" not in persisted["product_regeneration"]
        event_count = len((tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines())

        repeated = coordinator.regenerate_product(
            reason="refresh the reviewed dashboard",
            idempotency_key="product-regeneration-request-1",
        )
        assert repeated.last_event_seq == requested.last_event_seq
        assert len((tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()) == event_count

        launched = coordinator._refresh_and_launch(set())  # noqa: SLF001 - dispatch-boundary regression
        assert launched.status == "dispatching"
        assert len(calls) == 1
        assert calls[0].metadata["authorization_origin"] == "operator_product_regeneration"
        dispatched = coordinator._read_replay()[0]  # noqa: SLF001
        assert dispatched is not None
        assert dispatched["product_regeneration"]["status"] == "dispatched"
        assert coordinator.regenerate_product(idempotency_key="product-regeneration-request-1").last_event_seq == dispatched["last_event_seq"]
        assert len(calls) == 1
        with pytest.raises(CoordinatorConflictError, match="different Product regeneration"):
            coordinator.regenerate_product(idempotency_key="another-request")
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_state_binding_ignores_publication_projection_and_rejects_drift(
    tmp_path: Path,
) -> None:
    """The request/dispatch context excludes derived publication and lifecycle state."""

    coordinator, phase = _coordinator(tmp_path)
    try:
        _seed_reviewed_product(coordinator.context)
        product = phase["product"]
        assert isinstance(product, dict)
        # The operator may issue the request while scheduling is paused, then
        # resume the lifecycle before the first Product dispatch.  This
        # operational label transition must not invalidate the request's
        # accepted-input binding.
        phase["lifecycle_state"] = "paused"
        requested = coordinator.regenerate_product(idempotency_key="regen-drift")
        assert requested.phase == "product_regeneration_requested"
        state = coordinator._read_replay()[0]  # noqa: SLF001 - binding regression
        assert state is not None
        baseline = coordinator._product_regeneration_state_fingerprint(state)  # noqa: SLF001
        published = dict(state)
        published["publication_ready"] = True
        assert coordinator._product_regeneration_state_fingerprint(published) == baseline  # noqa: SLF001

        phase["lifecycle_state"] = "integration_complete"
        assert coordinator._product_regeneration_state_fingerprint(state) == baseline  # noqa: SLF001

        # A real accepted-item binding change remains part of the stable
        # context and must invalidate the previously issued request.
        items = phase["items"]
        assert isinstance(items, dict)
        item = items["REQ-001"]
        assert isinstance(item, dict)
        accepted = item["accepted_business_validation"]
        assert isinstance(accepted, dict)
        accepted["content_hash"] = "d" * 64
        assert coordinator._product_regeneration_state_fingerprint(state) != baseline  # noqa: SLF001

        # Unrelated durable coordinator context remains part of the binding;
        # changing it must deny a previously issued replacement authorization.
        action = _regeneration_action(state["run_id"], phase, state)
        drifted = dict(state)
        drifted["planner_ref"] = "planner://unrelated-drift"
        assert coordinator._authorize_preview_replacement_for_final_locked(drifted, action) is False  # noqa: SLF001
    finally:
        coordinator.close(wait_for_roles=True)


def test_failed_predecessor_review_is_bound_to_next_regeneration_offer(tmp_path: Path) -> None:
    """A failed Product review is carried to the next immutable request only."""

    coordinator, phase = _coordinator(tmp_path)
    registry = RoleSessionRegistry(coordinator.context)
    try:
        store, prior_candidate = _seed_reviewed_product(coordinator.context)
        prior = store.load_active_revision()
        assert prior is not None and prior.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": prior.candidate_hash}
        product["review"] = {"review_hash": prior.review_hash}

        # Preserve the completed Product owner so the second request crosses
        # the same replacement-authorization boundary as a live retry.
        seed = PlannerAction(
            "build_product_candidate",
            "product_agent",
            coordinator.context.run_id,
            "seed Product owner",
        )
        registry.prepare(seed, generation_id="G-0001", idempotency_key="seed-owner")
        registry.bind_reservation(
            seed,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-OLD",
        )
        registry.complete_reservation(
            seed,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-OLD",
        )
        registry.release_reservation(
            f"product_agent:{coordinator.context.run_id}:G-0001",
            "seed-owner",
        )

        coordinator.regenerate_product(idempotency_key="regen-review-predecessor")
        first_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert first_state is not None
        first_request = first_state["product_regeneration"]
        assert isinstance(first_request, dict)
        assert "predecessor_product_review_ref" not in first_request
        assert "predecessor_product_review_hash" not in first_request
        first_revision_id = first_request["revision_id"]
        assert isinstance(first_revision_id, str)

        candidate = _materialize_revision_candidate(store, first_revision_id, prior_candidate)
        candidate = store.record_candidate(candidate, revision_id=first_revision_id)
        store.record_review(
            reviewer_ref="product-reviewer",
            verdict="blocked_rethink",
            findings=(
                {
                    "finding_id": "presentation-001",
                    "summary": "repair the manager decision surface",
                },
            ),
            reviewed_at="2026-01-01T00:00:01Z",
            revision_id=first_revision_id,
        )
        failed_revision = store.fail_revision(first_revision_id)
        with coordinator._locked(create=False):  # noqa: SLF001 - replay boundary
            state, _ = coordinator._read_replay()  # noqa: SLF001
            assert state is not None
            coordinator._reconcile_product_regeneration_locked(state)  # noqa: SLF001

        failed_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert failed_state is not None
        assert failed_state["product_regeneration"]["status"] == "failed"
        assert failed_revision.review_ref is not None
        assert failed_revision.review_hash is not None

        second = coordinator.regenerate_product(idempotency_key="regen-review-successor")
        assert second.phase == "product_regeneration_requested"
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        request = state["product_regeneration"]
        assert isinstance(request, dict)
        assert request["predecessor_product_review_ref"] == failed_revision.review_ref
        assert request["predecessor_product_review_hash"] == failed_revision.review_hash
        offered_action = _regeneration_action(coordinator.context.run_id, phase, state)
        binding = coordinator._product_regeneration_binding_locked(state, offered_action)  # noqa: SLF001
        assert binding is not None
        assert binding["action_fingerprint"] == request["action_fingerprint"]
        assert binding["predecessor_product_review_ref"] == failed_revision.review_ref
        assert binding["predecessor_product_review_hash"] == failed_revision.review_hash

        owner_key = f"product_agent:{coordinator.context.run_id}:G-0001"
        authorization = state["replacement_authorizations"][owner_key]
        assert authorization["predecessor_product_review_ref"] == failed_revision.review_ref
        assert authorization["predecessor_product_review_hash"] == failed_revision.review_hash

        events = [
            json.loads(line)
            for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
        ]
        request_events = [
            event for event in events if event.get("event") == "product_regeneration_requested"
        ]
        assert request_events[-1]["payload"]["predecessor_product_review_ref"] == failed_revision.review_ref
        assert request_events[-1]["payload"]["predecessor_product_review_hash"] == failed_revision.review_hash
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_guidance_reads_predecessor_review_first() -> None:
    """Repair guidance consumes predecessor review context without reanalysis."""

    guidance = coordinator_module._role_guidance(  # noqa: SLF001 - contract regression
        PlannerAction(
            "build_product_candidate",
            "product_agent",
            "RUN-PRODUCT-REGENERATION",
            "repair the reviewed Product candidate",
            metadata={
                "predecessor_product_review_ref": "products/generations/G-0001/product_revisions/rev-0007/product_review.json",
                "predecessor_product_review_hash": "a" * 64,
            },
        )
    )
    assert guidance.startswith("Repair-first:")
    for phrase in (
        "read the exact predecessor Product Review",
        "Correct its presentation findings",
        "preserve accepted business facts",
        "do not rerun analytics",
        "substantive business decision surfaces over technical diagnostics",
        "keep technical evidence in audit",
        "concise portfolio/domain labels",
        "active accepted review hash is lineage only",
    ):
        assert phrase in guidance

    normal_guidance = coordinator_module._role_guidance(
        PlannerAction(
            "build_product_candidate",
            "product_agent",
            "RUN-PRODUCT-REGENERATION",
            "build the reviewed Product candidate",
        )
    )
    assert not normal_guidance.startswith("Repair-first:")


def test_product_regeneration_replacement_marker_launches_one_fresh_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed Product owner gets one coordinator-authorized fresh root."""

    calls: list[list[str]] = []

    class ReplacementProcess:
        def __init__(self, argv: list[str], **_: object) -> None:
            calls.append(list(argv))
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b'{"type":"thread.started","thread_id":"SID-NEW"}\n')
            self.stderr = io.BytesIO()
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", ReplacementProcess)

    # Mirror the live seq366 boundary: the pre-request coordinator projection
    # still advertises publication-ready, while ``regenerate_product`` clears
    # that derived flag before writing its request event.
    original_initial_state = coordinator_module.RunCoordinator._initial_state

    def initial_state_with_publication_ready(
        instance: coordinator_module.RunCoordinator,
        spec: CoordinatorRunSpec,
    ) -> dict[str, object]:
        state = original_initial_state(instance, spec)
        state["publication_ready"] = True
        return state

    monkeypatch.setattr(
        coordinator_module.RunCoordinator,
        "_initial_state",
        initial_state_with_publication_ready,
    )

    phase_holder: dict[str, dict[str, object]] = {}

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        if not isinstance(state.get("product_regeneration"), dict):
            return ()
        return (_regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),)

    coordinator, phase = _coordinator(tmp_path, planner=planner)
    phase_holder["value"] = phase
    try:
        store, prior_candidate = _seed_reviewed_product(coordinator.context)
        prior = store.load_active_revision()
        assert prior is not None and prior.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": prior.candidate_hash}
        product["review"] = {"review_hash": prior.review_hash}

        # Establish a completed logical owner exactly as the transport does.
        registry = RoleSessionRegistry(coordinator.context)
        seed = PlannerAction(
            "build_product_candidate",
            "product_agent",
            coordinator.context.run_id,
            "initial Product build",
        )
        reservation = registry.prepare(seed, generation_id="G-0001", idempotency_key="seed-product")
        registry.bind_reservation(
            seed,
            generation_id="G-0001",
            idempotency_key="seed-product",
            reservation_token="seed-product",
            session_id="SID-OLD",
        )
        registry.complete_reservation(
            seed,
            generation_id="G-0001",
            idempotency_key="seed-product",
            reservation_token="seed-product",
            session_id="SID-OLD",
        )
        registry.release_reservation("product_agent:RUN-PRODUCT-REGENERATION:G-0001", "seed-product")

        coordinator.role_runner = coordinator_module.CodexRoleAdapter(
            coordinator.context,
            coordinator_module.CodexExecConfig(binary="fake-codex", sandbox="workspace-write"),
        )
        coordinator.regenerate_product(idempotency_key="regen-fresh-session")
        launched = coordinator._refresh_and_launch(set())  # noqa: SLF001
        assert launched.status == "dispatching"
        completed = coordinator._consume_one()  # noqa: SLF001
        assert completed is not None and completed[1].ok
        assert len(calls) == 1
        assert "resume" not in calls[0]
        assert completed[1].session_id == "SID-NEW"

        entry = registry.get("product_agent:RUN-PRODUCT-REGENERATION:G-0001")
        assert entry is not None
        assert entry["session_id"] == "SID-NEW"
        assert entry["replacement_of"] == "SID-OLD"
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None and state["replacement_authorizations"] == {}
    finally:
        coordinator.close(wait_for_roles=True)


def test_sequential_product_regeneration_replaces_completed_owner_with_decorated_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second decorated regeneration consumes one replacement authorization."""

    calls: list[list[str]] = []

    class SequentialProcess:
        next_session = 0

        def __init__(self, argv: list[str], **_: object) -> None:
            type(self).next_session += 1
            session_id = "SID-1" if type(self).next_session < 3 else "SID-2"
            calls.append(list(argv))
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(
                (json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n").encode()
            )
            self.stderr = io.BytesIO()
            self.returncode = 1 if type(self).next_session < 3 else 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", SequentialProcess)

    phase_holder: dict[str, dict[str, object]] = {}
    coordinator_holder: dict[str, RunCoordinator] = {}

    def planner(state: dict[str, object]):
        request = state.get("product_regeneration")
        if not isinstance(request, dict) or request.get("status") not in {"requested", "dispatched"}:
            return ()
        coordinator = coordinator_holder["value"]
        action = _regeneration_action(coordinator.context.run_id, phase_holder["value"], state)
        metadata = dict(action.metadata)
        metadata.update(
            {
                "implementation_identity": request["implementation_identity"],
                "regeneration_action_fingerprint": request["action_fingerprint"],
                "regeneration_state_fingerprint": request["state_fingerprint"],
            }
        )
        return (
            PlannerAction(
                action.action,
                action.role,
                action.subject_id,
                action.reason,
                priority=action.priority,
                metadata=metadata,
            ),
        )

    coordinator, phase = _coordinator(tmp_path, planner=planner)
    coordinator_holder["value"] = coordinator
    phase_holder["value"] = phase
    transport_actions: list[PlannerAction] = []
    try:
        store, _prior_candidate = _seed_reviewed_product(coordinator.context)
        pointer = store.load_active_revision()
        assert pointer is not None and pointer.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": pointer.candidate_hash}
        product["review"] = {"review_hash": pointer.review_hash}
        adapter = coordinator_module.CodexRoleAdapter(
            coordinator.context,
            coordinator_module.CodexExecConfig(binary="fake-codex", sandbox="workspace-write"),
        )

        def transport(action: PlannerAction, **kwargs: object) -> RoleExecution:
            transport_actions.append(action)
            return adapter(action, **kwargs)

        coordinator.role_runner = transport

        first = coordinator.regenerate_product(idempotency_key="regen-sequential-first")
        assert first.phase == "product_regeneration_requested"
        launched_first = coordinator._refresh_and_launch(set())
        assert launched_first.status == "dispatching"
        completed_first = coordinator._consume_one()
        assert completed_first is not None
        assert completed_first[1].session_id == "SID-1"
        assert completed_first[1].session_status == "active"
        first_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert first_state is not None
        first_request_value = first_state["product_regeneration"]
        assert isinstance(first_request_value, dict)
        first_request = dict(first_request_value)

        retry_fingerprint = coordinator._retry_fingerprint(first_state, completed_first[0])  # noqa: SLF001
        for attempt in range(2):
            if attempt:
                launched_first = coordinator._refresh_and_launch(set())
                assert launched_first.status == "dispatching"
                completed_first = coordinator._consume_one()
                assert completed_first is not None
                assert completed_first[1].session_id == "SID-1"
                assert completed_first[1].session_status == "active"
            failed = coordinator._refresh_and_launch(
                {coordinator._slot_key(completed_first[0])},  # noqa: SLF001
                completed=completed_first,
            )
            if attempt == 0:
                assert failed.phase != "product_regeneration_failed"
            else:
                assert failed.phase == "product_regeneration_failed"
                assert failed.status == "waiting"

        exhausted_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert exhausted_state is not None
        assert exhausted_state["retry_counts"][retry_fingerprint] == 2
        exhausted_request = exhausted_state["product_regeneration"]
        assert isinstance(exhausted_request, dict)
        assert exhausted_request["status"] == "failed"
        first_recovery_count = sum(
            event.get("event") == "product_regeneration_failed"
            and event.get("payload", {}).get("reason") == "recovery_exhausted"
            for event in (
                json.loads(line)
                for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
            )
        )
        assert first_recovery_count == 1

        registry = RoleSessionRegistry(coordinator.context)
        owner_key = f"product_agent:{coordinator.context.run_id}:G-0001"
        first_owner = registry.get(owner_key)
        assert first_owner is not None
        assert first_owner["status"] == "active"
        assert first_owner["session_id"] == "SID-1"

        second = coordinator.regenerate_product(idempotency_key="regen-sequential-second")
        assert second.phase == "product_regeneration_requested"
        second_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert second_state is not None
        second_request_value = second_state["product_regeneration"]
        assert isinstance(second_request_value, dict)
        second_request = dict(second_request_value)
        second_revision_id = second_request["revision_id"]
        assert isinstance(second_revision_id, str)
        assert second_request["action_fingerprint"] != first_request["action_fingerprint"]
        assert "predecessor_product_review_ref" not in second_request
        assert "predecessor_product_review_hash" not in second_request
        replacement_required = registry.get(owner_key)
        assert replacement_required is not None
        assert replacement_required["status"] == "replacement_required"
        assert replacement_required["session_id"] == "SID-1"

        launched_second = coordinator._refresh_and_launch(set())
        assert launched_second.status == "dispatching"
        after_launch = coordinator._read_replay()[0]  # noqa: SLF001
        assert after_launch is not None
        assert after_launch["replacement_authorizations"] == {}
        assert after_launch["product_regeneration"]["status"] == "dispatched"

        completed_second = coordinator._consume_one()
        assert completed_second is not None
        assert completed_second[1].session_id == "SID-2"
        assert completed_second[1].session_status == "active"
        assert len(calls) == 3
        assert "resume" in calls[1]
        assert "resume" not in calls[2]
        assert len(transport_actions) == 3
        dispatched_action = transport_actions[2]
        assert dispatched_action.metadata["regeneration_action_fingerprint"] == second_request["action_fingerprint"]
        assert dispatched_action.metadata["allow_session_replacement"] is True
        after_second = coordinator._read_replay()[0]  # noqa: SLF001
        assert after_second is not None
        second_retry_fingerprint = coordinator._retry_fingerprint(after_second, dispatched_action)  # noqa: SLF001
        assert second_retry_fingerprint != retry_fingerprint
        assert after_second["retry_counts"].get(second_retry_fingerprint, 0) == 0
        replacement = registry.get(owner_key)
        assert replacement is not None
        assert replacement["status"] == "active"
        assert replacement["session_id"] == "SID-2"
        assert replacement["replacement_of"] == "SID-1"
        events = [
            json.loads(line)
            for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
        ]
        assert sum(
            event.get("event") == "product_regeneration_failed"
            and event.get("payload", {}).get("reason") == "recovery_exhausted"
            for event in events
        ) == first_recovery_count
    finally:
        coordinator.close(wait_for_roles=True)


@pytest.mark.parametrize("seed_completed_owner", (False, True), ids=("no-owner", "completed-owner"))
def test_product_regeneration_replacement_required_retry_gets_one_fresh_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_completed_owner: bool,
) -> None:
    """A consumed regeneration authorization is reconstructed once after stale transport."""

    calls: list[list[str]] = []

    class ReplacementRequiredProcess:
        invocation = 0

        def __init__(self, argv: list[str], **_: object) -> None:
            type(self).invocation += 1
            calls.append(list(argv))
            self.stdin = io.BytesIO()
            if type(self).invocation == 2:
                session_id = "SID-NEW"
                stdout = (json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n").encode()
                self.returncode = 0
            else:
                stdout = b""
                self.returncode = 1
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO()

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", ReplacementRequiredProcess)

    phase_holder: dict[str, dict[str, object]] = {}
    coordinator_holder: dict[str, RunCoordinator] = {}

    def planner(state: dict[str, object]):
        request = state.get("product_regeneration")
        if not isinstance(request, dict) or request.get("status") not in {"requested", "dispatched"}:
            return ()
        coordinator = coordinator_holder["value"]
        action = _regeneration_action(coordinator.context.run_id, phase_holder["value"], state)
        metadata = dict(action.metadata)
        metadata.update(
            {
                "implementation_identity": request["implementation_identity"],
                "regeneration_action_fingerprint": request["action_fingerprint"],
                "regeneration_state_fingerprint": request["state_fingerprint"],
            }
        )
        return (
            PlannerAction(
                action.action,
                action.role,
                action.subject_id,
                action.reason,
                priority=action.priority,
                metadata=metadata,
            ),
        )

    coordinator, phase = _coordinator(tmp_path, planner=planner)
    coordinator_holder["value"] = coordinator
    phase_holder["value"] = phase
    try:
        store, _prior_candidate = _seed_reviewed_product(coordinator.context)
        pointer = store.load_active_revision()
        assert pointer is not None and pointer.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": pointer.candidate_hash}
        product["review"] = {"review_hash": pointer.review_hash}

        registry = RoleSessionRegistry(coordinator.context)
        if seed_completed_owner:
            # A completed owner makes the request write the typed
            # Product-regeneration audit before the first replacement.
            seed = PlannerAction(
                "build_product_candidate",
                "product_agent",
                coordinator.context.run_id,
                "seed Product owner",
            )
            registry.prepare(seed, generation_id="G-0001", idempotency_key="seed-owner")
            registry.bind_reservation(
                seed,
                generation_id="G-0001",
                idempotency_key="seed-owner",
                reservation_token="seed-owner",
                session_id="SID-OLD",
            )
            registry.complete_reservation(
                seed,
                generation_id="G-0001",
                idempotency_key="seed-owner",
                reservation_token="seed-owner",
                session_id="SID-OLD",
            )
            registry.release_reservation(
                f"product_agent:{coordinator.context.run_id}:G-0001",
                "seed-owner",
            )

        adapter = coordinator_module.CodexRoleAdapter(
            coordinator.context,
            coordinator_module.CodexExecConfig(binary="fake-codex", sandbox="workspace-write"),
        )
        transport_actions: list[PlannerAction] = []

        def transport(action: PlannerAction, **kwargs: object) -> RoleExecution:
            transport_actions.append(action)
            return adapter(action, **kwargs)

        coordinator.role_runner = transport
        coordinator.regenerate_product(idempotency_key="regen-replacement-retry")

        first_launch = coordinator._refresh_and_launch(set())  # noqa: SLF001
        assert first_launch.status == "dispatching"
        first_completed = coordinator._consume_one()  # noqa: SLF001
        assert first_completed is not None
        assert first_completed[1].session_status == "replacement_required"
        first_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert first_state is not None
        retry_fingerprint = coordinator._retry_fingerprint(first_state, first_completed[0])  # noqa: SLF001
        assert first_state["retry_counts"].get(retry_fingerprint, 0) == 0

        recorded = coordinator._refresh_and_launch(  # noqa: SLF001
            {coordinator._slot_key(first_completed[0])},  # noqa: SLF001
            completed=first_completed,
        )
        assert recorded.status == "waiting"
        retry_status = coordinator._refresh_and_launch(set())  # noqa: SLF001
        assert retry_status.status == "dispatching"
        retry_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert retry_state is not None
        assert retry_state["retry_counts"][retry_fingerprint] == 1
        assert retry_state["replacement_authorizations"] == {}
        assert retry_state["product_regeneration"]["status"] == "dispatched"
        retry_action = transport_actions[-1]
        assert retry_action.metadata["allow_session_replacement"] is True
        assert "resume" not in calls[-1]

        fresh_completed = coordinator._consume_one()  # noqa: SLF001
        assert fresh_completed is not None
        assert fresh_completed[1].session_id == "SID-NEW"
        assert fresh_completed[1].session_status == "active"
        owner = registry.get(f"product_agent:{coordinator.context.run_id}:G-0001")
        assert owner is not None
        assert owner["session_id"] == "SID-NEW"
        assert owner["status"] == "active"
        assert owner["session_id"] != "SID-OLD"
        assert len(calls) == 2

        # A later unchanged offer consumes the normal second retry and
        # terminalizes; the one-shot reconstruction must not loop again.
        recorded_again = coordinator._refresh_and_launch(  # noqa: SLF001
            {coordinator._slot_key(fresh_completed[0])},  # noqa: SLF001
            completed=fresh_completed,
        )
        assert recorded_again.phase == "product_regeneration_failed"
        assert recorded_again.status == "waiting"
        terminal_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert terminal_state is not None
        assert terminal_state["retry_counts"][retry_fingerprint] == 2
        assert terminal_state["replacement_authorizations"] == {}
        assert terminal_state["product_regeneration"]["status"] == "failed"
        assert len(calls) == 2
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_retry_fingerprint_requires_durable_request_match(tmp_path: Path) -> None:
    """Only exact durable request ids rotate Product retry namespaces."""

    coordinator, _phase = _coordinator(tmp_path)
    try:
        state_one = {
            "run_id": coordinator.context.run_id,
            "generation_id": "G-0001",
            "product_regeneration": {"request_id": "regen-one"},
        }
        state_two = {
            **state_one,
            "product_regeneration": {"request_id": "regen-two"},
        }

        def make_action(role: str, name: str, request_id: object) -> PlannerAction:
            metadata: dict[str, object] = {"authorization_origin": "operator_product_regeneration"}
            if request_id is not None:
                metadata["product_regeneration_request_id"] = request_id
            return PlannerAction(
                name,
                role,
                coordinator.context.run_id,
                "regeneration retry fingerprint test",
                metadata=metadata,
            )

        for role, name in (
            ("product_agent", "build_product_candidate"),
            ("product_reviewer", "review_final_product"),
        ):
            base = coordinator_module.logical_action_fingerprint(
                make_action(role, name, None),
                run_id=coordinator.context.run_id,
                generation_id="G-0001",
            )
            exact_one = coordinator._retry_fingerprint(  # noqa: SLF001
                state_one,
                make_action(role, name, "regen-one"),
            )
            assert exact_one == coordinator._retry_fingerprint(  # noqa: SLF001
                state_one,
                make_action(role, name, "regen-one"),
            )
            exact_two = coordinator._retry_fingerprint(  # noqa: SLF001
                state_two,
                make_action(role, name, "regen-two"),
            )
            assert exact_two != exact_one
            assert coordinator._retry_fingerprint(  # noqa: SLF001
                state_one,
                make_action(role, name, ""),
            ) == base
            assert coordinator._retry_fingerprint(  # noqa: SLF001
                state_one,
                make_action(role, name, "forged-request"),
            ) == base
            assert coordinator._retry_fingerprint(  # noqa: SLF001
                state_one,
                make_action(role, name, "regen-two"),
            ) == base
    finally:
        coordinator.close(wait_for_roles=True)


def test_terminal_regeneration_intent_uses_current_input_spec_and_revision_identity(tmp_path: Path) -> None:
    coordinator, phase = _coordinator(tmp_path)
    # The terminal projection's positive path models a genuine pre-adoption
    # run, so provide a complete hash-bound root candidate/review bundle.  A
    # phase-only candidate/review projection is intentionally insufficient.
    _seed_reviewed_product(coordinator.context)
    try:
        first = coordinator.product_regeneration_projection()
        repeated = coordinator.product_regeneration_projection()
        assert first is not None and first["pending"] is False
        assert first["eligible"] is True
        assert first["idempotency_key"] == repeated["idempotency_key"]

        product = phase["product"]
        items = phase["items"]
        assert isinstance(product, dict) and isinstance(items, dict)
        item = items["REQ-001"]
        assert isinstance(item, dict)
        accepted = item["accepted_business_validation"]
        terminal = item["terminal_outcome"]
        bindings = product["preview_item_bindings"]
        assert isinstance(accepted, dict) and isinstance(terminal, dict) and isinstance(bindings, dict)
        accepted["content_hash"] = "d" * 64
        terminal["content_hash"] = "d" * 64
        binding = bindings["REQ-001"]
        assert isinstance(binding, dict)
        binding["accepted_content_hash"] = "d" * 64
        product["preview_input_fingerprint"] = preview_input_fingerprint(items)
        changed_input = coordinator.product_regeneration_projection()
        assert changed_input is not None
        assert changed_input["eligible"] is True
        assert changed_input["idempotency_key"] != first["idempotency_key"]

        current_spec = coordinator._spec  # noqa: SLF001 - production identity regression
        assert current_spec is not None
        rebound_spec = replace(current_spec, planner_ref="planner://product-regeneration-rebound")
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        rebound_state = dict(state)
        rebound_state["spec_hash"] = coordinator._spec_hash(rebound_spec)  # noqa: SLF001
        coordinator._spec = rebound_spec  # noqa: SLF001 - model a completed post-rebind state
        coordinator._read_replay = lambda: (rebound_state, ())  # type: ignore[method-assign]
        changed_spec = coordinator.product_regeneration_projection()
        assert changed_spec is not None
        assert changed_spec["eligible"] is True
        assert changed_spec["idempotency_key"] != changed_input["idempotency_key"]

        product["preview_item_bindings"] = {}
        invalid = coordinator.product_regeneration_projection()
        assert invalid is not None
        assert invalid["eligible"] is False
        assert invalid["idempotency_key"] is None
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_projection_for_post_rebind_spec_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotated installed skill can expose the same preview key without loading the stale adapter."""

    run_id = "RUN-PRODUCT-REGENERATION-PREVIEW"
    context = RunContext(run_id, tmp_path)
    RunLifecycle.create(context, ["REQ-001"], mode="requirement")
    _seed_reviewed_product(context)
    current_binding = coordinator_module.resolve_production_skill_binding(
        repo_root=tmp_path,
        role_cwd=tmp_path,
    )
    old_binding = dict(current_binding)
    old_binding["skill_sha256"] = "f" * 64
    old_spec = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://product-regeneration-preview",
        hashlib.sha256(b"product-regeneration-preview").hexdigest(),
        publication_policy={"enabled": False},
        codex_exec={"binary": "codex", "sandbox": "workspace-write", "ephemeral": True, **old_binding},
    )
    # A custom runner lets the fixture persist an old transport binding that
    # is no longer installed; normal from_persisted_spec must still reject it.
    coordinator = RunCoordinator(context, planner=lambda _state: (), role_runner=lambda *_args, **_kwargs: RoleExecution())
    coordinator.start(old_spec)
    try:
        with pytest.raises(Exception, match="skill|release|hash|binding"):
            RunCoordinator.from_persisted_spec(context)

        phase = _phase_view()
        monkeypatch.setattr(RunCoordinator, "_phase_snapshot", lambda _self: phase)
        desired_spec = replace(old_spec, codex_exec={"binary": "codex", "sandbox": "workspace-write", "ephemeral": True, **current_binding})
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (tmp_path / "control_plane").iterdir()
            if path.is_file() and path.name != "coordinator.lock"
        }
        previewer = RunCoordinator(context, planner=lambda _state: (), role_runner=lambda *_args, **_kwargs: RoleExecution())
        try:
            first = previewer.product_regeneration_projection_for_spec(desired_spec)
            second = previewer.product_regeneration_projection_for_spec(desired_spec)
        finally:
            previewer.close(wait_for_roles=True)
        assert first is not None and first["eligible"] is True
        assert first["pending"] is False
        assert first["idempotency_key"] == second["idempotency_key"]
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (tmp_path / "control_plane").iterdir()
            if path.is_file() and path.name != "coordinator.lock"
        }
        assert after == before
    finally:
        coordinator.close(wait_for_roles=True)


def test_post_rebind_projection_rejects_malformed_transport_intent(tmp_path: Path) -> None:
    """A stale release cannot bypass a malformed transport recovery intent."""

    run_id = "RUN-PRODUCT-REGENERATION-MALFORMED-INTENT"
    context = RunContext(run_id, tmp_path)
    RunLifecycle.create(context, ["REQ-001"], mode="requirement")
    current_binding = coordinator_module.resolve_production_skill_binding(
        repo_root=tmp_path,
        role_cwd=tmp_path,
    )
    old_binding = dict(current_binding)
    old_binding["skill_sha256"] = "f" * 64
    old_spec = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://product-regeneration-malformed-intent",
        hashlib.sha256(b"product-regeneration-malformed-intent").hexdigest(),
        publication_policy={"enabled": False},
        codex_exec={"binary": "codex", "sandbox": "workspace-write", "ephemeral": True, **old_binding},
    )
    coordinator = RunCoordinator(context, planner=lambda _state: (), role_runner=lambda *_args, **_kwargs: RoleExecution())
    coordinator.start(old_spec)
    intent_path = coordinator.transport_rebind_intent_path
    intent_path.write_text('{"malformed": true}\n', encoding="utf-8")
    desired_spec = replace(
        old_spec,
        codex_exec={"binary": "codex", "sandbox": "workspace-write", "ephemeral": True, **current_binding},
    )
    try:
        with pytest.raises(CoordinatorIntegrityError, match="transport rebind intent"):
            coordinator.product_regeneration_projection_for_spec(desired_spec)
        with pytest.raises(CoordinatorIntegrityError, match="transport rebind intent"):
            coordinator.validate_read_only_resume_evidence()
        assert intent_path.read_text(encoding="utf-8") == '{"malformed": true}\n'
    finally:
        coordinator.close(wait_for_roles=True)


def test_terminal_projection_fails_closed_when_revision_pointer_is_lost(tmp_path: Path) -> None:
    """Revision evidence without its authoritative pointer is not legacy input."""

    coordinator, _phase = _coordinator(tmp_path)
    store, _candidate = _seed_reviewed_product(coordinator.context)
    pointer = store.load_active_revision()
    assert pointer is not None and pointer.revision_id == "rev-0001"
    store.pointer_path.unlink()
    try:
        projection = coordinator.product_regeneration_projection()
        assert projection is not None
        assert projection["eligible"] is False
        assert projection["idempotency_key"] is None
    finally:
        coordinator.close(wait_for_roles=True)


def test_terminal_projection_requires_complete_accepted_legacy_root_bundle(tmp_path: Path) -> None:
    """Pre-adoption projection validates root hashes/review through the store."""

    coordinator, _phase = _coordinator(tmp_path)
    store, _candidate = _seed_reviewed_product(coordinator.context)
    try:
        valid = coordinator.product_regeneration_projection()
        assert valid is not None
        assert valid["eligible"] is True
        assert isinstance(valid["idempotency_key"], str)

        review_bytes = store.review_path.read_bytes()
        store.review_path.unlink()
        missing = coordinator.product_regeneration_projection()
        assert missing is not None
        assert missing["eligible"] is False
        assert missing["idempotency_key"] is None
        store.review_path.write_bytes(review_bytes)
        review_value = json.loads(store.review_path.read_text(encoding="utf-8"))
        review_value["review_hash"] = None
        store.review_path.write_text(json.dumps(review_value, sort_keys=True) + "\n", encoding="utf-8")
        incomplete = coordinator.product_regeneration_projection()
        assert incomplete is not None
        assert incomplete["eligible"] is False
        assert incomplete["idempotency_key"] is None
    finally:
        coordinator.close(wait_for_roles=True)


def test_typed_product_regeneration_registry_keeps_stale_reason_null(tmp_path: Path) -> None:
    assert not hasattr(RoleSessionRegistry, "request_regeneration")
    assert not hasattr(RoleSessionRegistry, "_request_regeneration_unlocked")
    context = RunContext("RUN-REGISTRY-TYPED", tmp_path)
    registry = RoleSessionRegistry(context)
    action = PlannerAction("build_product_candidate", "product_agent", context.run_id, "regenerate")
    reservation = registry.prepare(action, generation_id="G-0001", idempotency_key="seed")
    assert reservation["mode"] == "new"
    registry.bind_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="seed",
        reservation_token="seed",
        session_id="SID-PRODUCT",
    )
    registry.complete_reservation(
        action,
        generation_id="G-0001",
        idempotency_key="seed",
        reservation_token="seed",
        session_id="SID-PRODUCT",
    )
    logical_owner = f"product_agent:{context.run_id}:G-0001"
    registry.release_reservation(logical_owner, "seed")
    requested = registry.request_product_regeneration(
        action,
        generation_id="G-0001",
        idempotency_key="typed-regeneration",
    )
    assert requested["mode"] == "requested"
    entry = registry.get(logical_owner)
    assert entry is not None
    assert entry["status"] == "replacement_required"
    assert entry["stale_reason"] is None
    assert entry["audit"][-1]["event"] == "product_regeneration_requested"
    assert entry["audit"][-1]["reason"] == "operator_product_regeneration"


def test_product_regeneration_rejects_missing_accepted_boundary_without_mutation(tmp_path: Path) -> None:
    coordinator, phase = _coordinator(tmp_path)
    try:
        items = phase["items"]
        assert isinstance(items, dict)
        accepted = items["REQ-001"]["accepted_business_validation"]
        assert isinstance(accepted, dict)
        accepted["valid"] = False
        with pytest.raises(CoordinatorConflictError, match="complete source-bound accepted business inputs"):
            coordinator.regenerate_product(idempotency_key="invalid-request")
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        assert state["product_regeneration"] is None
        assert len((tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()) == 1
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_admission_conflict_precedes_revision_creation(tmp_path: Path) -> None:
    """An active Product reservation fails before creating a target revision."""

    coordinator, phase = _coordinator(tmp_path)
    registry = RoleSessionRegistry(coordinator.context)
    try:
        store, _candidate = _seed_reviewed_product(coordinator.context)
        pointer = store.load_active_revision()
        assert pointer is not None
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": pointer.candidate_hash}
        product["review"] = {"review_hash": pointer.review_hash}
        seed = PlannerAction(
            "build_product_candidate",
            "product_agent",
            coordinator.context.run_id,
            "active Product reservation",
        )
        reservation = registry.prepare(
            seed,
            generation_id="G-0001",
            idempotency_key="active-product-reservation",
        )
        assert reservation["mode"] == "new"
        revisions_root = store.revisions_root
        before = sorted(path.name for path in revisions_root.iterdir()) if revisions_root.exists() else []
        with pytest.raises(CoordinatorConflictError, match="reservation is active"):
            coordinator.regenerate_product(idempotency_key="blocked-product-regeneration")
        after = sorted(path.name for path in revisions_root.iterdir()) if revisions_root.exists() else []
        assert after == before == ["rev-0001"]
        assert coordinator._read_replay()[0]["product_regeneration"] is None  # noqa: SLF001
    finally:
        registry.release_reservation(
            f"product_agent:{coordinator.context.run_id}:G-0001",
            "active-product-reservation",
        )
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_accepted_terminal_owner_cannot_be_refreshed(tmp_path: Path) -> None:
    """A replacement-required owner is refreshable only from a failed request."""

    coordinator, _phase = _coordinator(tmp_path)
    registry = RoleSessionRegistry(coordinator.context)
    action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        coordinator.context.run_id,
        "accepted Product owner",
    )
    try:
        reservation = registry.prepare(action, generation_id="G-0001", idempotency_key="seed-owner")
        assert reservation["mode"] == "new"
        registry.bind_reservation(
            action,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-ACCEPTED",
        )
        registry.complete_reservation(
            action,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-ACCEPTED",
        )
        logical_owner = f"product_agent:{coordinator.context.run_id}:G-0001"
        registry.release_reservation(logical_owner, "seed-owner")
        requested = registry.request_product_regeneration(
            action,
            generation_id="G-0001",
            idempotency_key="accepted-terminal-request",
        )
        assert requested["mode"] == "requested"
        before = registry.registry_path.read_bytes()
        accepted_terminal = {
            "status": "accepted",
            "authorization_origin": "operator_product_regeneration",
            "request_id": "accepted-terminal-request",
            "run_id": coordinator.context.run_id,
            "generation_id": "G-0001",
        }
        with pytest.raises(CoordinatorConflictError, match="explicit replacement"):
            registry.preflight_product_regeneration(
                action,
                generation_id="G-0001",
                idempotency_key="accepted-next-request",
                terminal_request=accepted_terminal,
            )
        assert registry.registry_path.read_bytes() == before
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_same_token_requires_exact_latest_audit_and_historical_tokens_conflict(
    tmp_path: Path,
) -> None:
    """Only an exact same-token replacement row is idempotently recoverable."""

    coordinator, _phase = _coordinator(tmp_path)
    registry = RoleSessionRegistry(coordinator.context)
    action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        coordinator.context.run_id,
        "Product owner token history",
    )
    try:
        reservation = registry.prepare(action, generation_id="G-0001", idempotency_key="seed-owner")
        assert reservation["mode"] == "new"
        registry.bind_reservation(
            action,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-TOKENS",
        )
        registry.complete_reservation(
            action,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-TOKENS",
        )
        logical_owner = f"product_agent:{coordinator.context.run_id}:G-0001"
        registry.release_reservation(logical_owner, "seed-owner")
        first = registry.request_product_regeneration(
            action,
            generation_id="G-0001",
            idempotency_key="token-old",
        )
        assert first["mode"] == "requested"

        # The exact same token is accepted only when the replacement row and
        # latest typed audit still agree on owner/session/action.
        same = registry.preflight_product_regeneration(
            action,
            generation_id="G-0001",
            idempotency_key="token-old",
        )
        assert same["mode"] == "already_requested"

        terminal_failed = {
            "status": "failed",
            "authorization_origin": "operator_product_regeneration",
            "request_id": "token-old",
            "run_id": coordinator.context.run_id,
            "generation_id": "G-0001",
        }
        with registry._locked():  # noqa: SLF001 - terminal replacement fixture
            document = registry._read_unlocked()  # noqa: SLF001
            registry._request_product_regeneration_unlocked(  # noqa: SLF001
                document,
                action,
                generation_id="G-0001",
                idempotency_key="token-new",
                terminal_request=terminal_failed,
            )
        # Reusing the historical token after a newer Product audit is not an
        # idempotent retry and is rejected before any revision namespace work.
        with pytest.raises(CoordinatorConflictError, match="historical"):
            registry.preflight_product_regeneration(
                action,
                generation_id="G-0001",
                idempotency_key="token-old",
            )

        # Corrupting the latest audit/session binding cannot be downgraded to a
        # same-token success, even though the row remains replacement-required.
        with registry._locked():  # noqa: SLF001 - malformed latest-audit fixture
            document = registry._read_unlocked()  # noqa: SLF001
            entry = document["sessions"][logical_owner]
            entry["last_idempotency_key"] = "token-audit-mismatch"
            registry._write_unlocked(document)  # noqa: SLF001
        with pytest.raises(CoordinatorConflictError, match="explicit replacement|historical"):
            registry.preflight_product_regeneration(
                action,
                generation_id="G-0001",
                idempotency_key="token-new",
            )
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_replays_after_registry_commit_crash_once(tmp_path: Path) -> None:
    """A crash after registry commit reuses one pending revision on restart."""

    calls: list[PlannerAction] = []
    phase_holder: dict[str, dict[str, object]] = {}

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        if not isinstance(state.get("product_regeneration"), dict):
            return ()
        return (_regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),)

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action)
        return RoleExecution(output="replayed Product dispatch")

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={"build_product_candidate": transport},
    )
    phase_holder["value"] = phase
    registry = RoleSessionRegistry(coordinator.context)
    owner_action = PlannerAction(
        "build_product_candidate",
        "product_agent",
        coordinator.context.run_id,
        "completed Product owner",
    )
    try:
        store, _prior = _seed_reviewed_product(coordinator.context)
        pointer = store.load_active_revision()
        assert pointer is not None and pointer.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": pointer.candidate_hash}
        product["review"] = {"review_hash": pointer.review_hash}

        reservation = registry.prepare(owner_action, generation_id="G-0001", idempotency_key="seed-owner")
        assert reservation["mode"] == "new"
        registry.bind_reservation(
            owner_action,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-OLD",
        )
        registry.complete_reservation(
            owner_action,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-OLD",
        )
        registry.release_reservation(
            f"product_agent:{coordinator.context.run_id}:G-0001",
            "seed-owner",
        )

        fired = {"value": False}

        def fail_after_registry(name: str) -> None:
            if name == "product_regeneration_after_registry" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("crash after registry commit")

        coordinator._failpoint = fail_after_registry  # noqa: SLF001
        with pytest.raises(RuntimeError, match="crash after registry commit"):
            coordinator.regenerate_product(idempotency_key="regen-after-registry")
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None and state["product_regeneration"] is None
        revisions = [path.name for path in store.revisions_root.iterdir() if path.is_dir()]
        assert revisions == ["rev-0001", "rev-0002"]
        owner = registry.get(f"product_agent:{coordinator.context.run_id}:G-0001")
        assert owner is not None
        assert owner["status"] == "replacement_required"
        assert owner["last_idempotency_key"] == "regen-after-registry"
        assert [
            event
            for event in owner["audit"]
            if event["event"] == "product_regeneration_requested"
        ][-1]["idempotency_key"] == "regen-after-registry"
        coordinator.close(wait_for_roles=True)

        restarted = RunCoordinator(
            coordinator.context,
            planner=planner,
            adapters={"build_product_candidate": transport},
            owner_id=coordinator.owner_id,
        )
        restarted._phase_snapshot = lambda: phase  # type: ignore[method-assign]
        try:
            requested = restarted.regenerate_product(idempotency_key="regen-after-registry")
            assert requested.phase == "product_regeneration_requested"
            state = restarted._read_replay()[0]  # noqa: SLF001
            assert state is not None
            request = state["product_regeneration"]
            assert isinstance(request, dict)
            assert request["request_id"] == "regen-after-registry"
            assert request["revision_id"] == "rev-0002"
            events = [
                json.loads(line)
                for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
            ]
            assert [event["event"] for event in events].count("product_regeneration_requested") == 1

            launched = restarted._refresh_and_launch(set())  # noqa: SLF001
            assert launched.status == "dispatching"
            assert len(calls) == 1
            replayed_events = [
                json.loads(line)
                for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
            ]
            assert [event["event"] for event in replayed_events].count("dispatch_started") == 1
        finally:
            restarted.close(wait_for_roles=True)
    finally:
        # ``coordinator`` may already be closed after the simulated crash;
        # ``close`` is idempotent and keeps the fixture cleanup deterministic.
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_reconciles_terminal_product_orphan_revision_and_refreshes_owner(
    tmp_path: Path,
) -> None:
    """A terminal Product request may refresh its owner and reuse an orphan target."""

    coordinator, phase = _coordinator(tmp_path)
    registry = RoleSessionRegistry(coordinator.context)
    try:
        store, _candidate = _seed_reviewed_product(coordinator.context)
        pointer = store.load_active_revision()
        assert pointer is not None and pointer.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": pointer.candidate_hash}
        product["review"] = {"review_hash": pointer.review_hash}

        # Establish a completed Product owner and its first intentional
        # regeneration audit.  The coordinator state is then made terminal
        # and the target revision failed, matching the live retry-exhaustion
        # boundary without touching the real run.
        seed = PlannerAction(
            "build_product_candidate",
            "product_agent",
            coordinator.context.run_id,
            "seed Product owner",
        )
        reservation = registry.prepare(seed, generation_id="G-0001", idempotency_key="seed-owner")
        registry.bind_reservation(
            seed,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-OLD",
        )
        registry.complete_reservation(
            seed,
            generation_id="G-0001",
            idempotency_key="seed-owner",
            reservation_token="seed-owner",
            session_id="SID-OLD",
        )
        registry.release_reservation(
            f"product_agent:{coordinator.context.run_id}:G-0001",
            "seed-owner",
        )

        # First request creates rev-0002 and records the terminal request in
        # both coordinator state and the Product owner audit.
        coordinator.regenerate_product(idempotency_key="terminal-product-request")
        first_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert first_state is not None
        first_request = first_state["product_regeneration"]
        assert isinstance(first_request, dict)
        first_revision_id = first_request["revision_id"]
        assert isinstance(first_revision_id, str)
        store.fail_revision(first_revision_id)
        with coordinator._locked(create=False):  # noqa: SLF001 - terminal fixture boundary
            state, _ = coordinator._read_replay()  # noqa: SLF001
            assert state is not None
            request = state["product_regeneration"]
            assert isinstance(request, dict)
            request["status"] = "failed"
            state["status"] = "waiting"
            state["phase"] = "product_regeneration_failed"
            coordinator._append_event_locked(  # noqa: SLF001
                state,
                "product_regeneration_failed",
                {
                    "authorization_origin": "operator_product_regeneration",
                    "request_id": request["request_id"],
                    "revision_id": first_revision_id,
                    "status": "failed",
                },
            )

        failed_state = coordinator._read_replay()[0]  # noqa: SLF001
        assert failed_state is not None
        stale_action = _regeneration_action(coordinator.context.run_id, phase, failed_state)
        dispatch_like_key = coordinator._idempotency_key(failed_state, stale_action)  # noqa: SLF001
        registry.mark_stale(
            stale_action,
            generation_id="G-0001",
            idempotency_key=dispatch_like_key,
            reason="orphaned_reservation",
        )
        stale_owner = registry.get(f"product_agent:{coordinator.context.run_id}:G-0001")
        assert stale_owner is not None
        assert stale_owner["status"] == "replacement_required"
        assert stale_owner["stale_reason"] == "orphaned_reservation"
        assert stale_owner["last_idempotency_key"] == dispatch_like_key

        # The public call's first attempt in the live incident already wrote
        # this pending revision before receiving the registry conflict.  A
        # same-key retry must reconcile it, never allocate rev-0004.
        orphan = store.begin_revision(
            request_id="terminal-product-retry",
            input_fingerprint=product["preview_input_fingerprint"],
            implementation_identity=first_request["implementation_identity"],
            prior_revision_id=pointer.revision_id,
            prior_candidate_hash=pointer.candidate_hash,
            prior_review_hash=pointer.review_hash,
        )
        assert orphan.revision_id == "rev-0003" and orphan.status == "pending"
        requested = coordinator.regenerate_product(idempotency_key="terminal-product-retry")
        assert requested.phase == "product_regeneration_requested"
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        request = state["product_regeneration"]
        assert isinstance(request, dict)
        assert request["status"] == "requested"
        assert request["request_id"] == "terminal-product-retry"
        assert request["revision_id"] == "rev-0003"
        assert request["prior_revision_id"] == pointer.revision_id
        assert len([path for path in store.revisions_root.iterdir() if path.is_dir()]) == 3
        entry = registry.get(f"product_agent:{coordinator.context.run_id}:G-0001")
        assert entry is not None
        assert entry["status"] == "replacement_required"
        assert entry["stale_reason"] is None
        assert entry["last_idempotency_key"] == "terminal-product-retry"
        assert entry["audit"][-1]["event"] == "product_regeneration_requested"
        assert entry["audit"][-1]["idempotency_key"] == "terminal-product-retry"
    finally:
        coordinator.close(wait_for_roles=True)


def test_regeneration_preserves_reviewed_root_and_binds_new_revision(tmp_path: Path) -> None:
    """A reviewed root is immutable while the replacement targets a revision."""

    calls: list[PlannerAction] = []
    phase_holder: dict[str, dict[str, object]] = {}

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        if not isinstance(state.get("product_regeneration"), dict):
            return ()
        return (_regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),)

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action)
        return RoleExecution()

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={"build_product_candidate": transport},
    )
    phase_holder["value"] = phase
    try:
        store, prior_candidate = _seed_reviewed_product(coordinator.context)
        prior_pointer = store.load_active_revision()
        assert prior_pointer is not None and prior_pointer.status == "accepted"
        # The production phase projection carries the active root candidate
        # and review hashes; make that binding explicit in this focused
        # planner fixture so the request's compare-and-set is exercised.
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": prior_pointer.candidate_hash}
        product["review"] = {"review_hash": prior_pointer.review_hash}

        requested = coordinator.regenerate_product(idempotency_key="regen-reviewed-root")
        assert requested.phase == "product_regeneration_requested"
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        target_revision_id = state["product_regeneration"]["revision_id"]
        assert target_revision_id != prior_pointer.revision_id
        target_revision = store.load_revision(target_revision_id)
        assert target_revision.prior_revision_id == prior_pointer.revision_id
        assert target_revision.prior_candidate_hash == prior_pointer.candidate_hash
        assert target_revision.prior_review_hash == prior_pointer.review_hash
        assert store.load_candidate().computed_hash == prior_candidate.computed_hash

        launched = coordinator._refresh_and_launch(set())  # noqa: SLF001
        assert launched.status == "dispatching"
        assert len(calls) == 1
        dispatched = calls[0]
        assert dispatched.metadata["product_revision_id"] == target_revision_id
        assert dispatched.metadata["prior_revision_id"] == prior_pointer.revision_id
        # No root candidate/review bytes were overwritten by request/admission.
        assert store.load_active_revision().revision_id == prior_pointer.revision_id
    finally:
        coordinator.close(wait_for_roles=True)


def test_regeneration_dispatch_started_event_replays_after_checkpoint_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the dispatch event leaves one resumable Product slot."""

    calls: list[PlannerAction] = []
    phase_holder: dict[str, dict[str, object]] = {}

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        if not isinstance(state.get("product_regeneration"), dict):
            return ()
        return (_regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),)

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action)
        return RoleExecution(output="resumed exactly once")

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={"build_product_candidate": transport},
    )
    phase_holder["value"] = phase
    try:
        coordinator.regenerate_product(idempotency_key="regen-crash-replay")
        fired = {"value": False}

        def failpoint(name: str) -> None:
            if name == "after_event_before_checkpoint" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("dispatch checkpoint crash")

        coordinator._failpoint = failpoint
        with pytest.raises(RuntimeError, match="dispatch checkpoint crash"):
            coordinator._refresh_and_launch(set())  # noqa: SLF001
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        # Event-log replay, not an in-memory authorization pop, is now the
        # authority: the request is dispatched and the action remains active.
        assert state["product_regeneration"]["status"] == "dispatched"
        assert state["replacement_authorizations"] == {}
        assert len(state["active_dispatches"]) == 1
        events = [
            json.loads(line)
            for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
        ]
        assert [event["event"] for event in events].count("dispatch_started") == 1

        # Model a process restart with the same owner identity.  It claims the
        # persisted slot and submits one Product Agent call; no second
        # dispatch_started event or duplicate transport call is created.
        monkeypatch.setattr("auto_foundry_core.coordinator._pid_alive", lambda _pid: False)
        restarted = RunCoordinator(
            coordinator.context,
            planner=planner,
            adapters={"build_product_candidate": transport},
            owner_id=coordinator.owner_id,
        )
        restarted._phase_snapshot = lambda: phase  # type: ignore[method-assign]
        restarted._refresh_and_launch(set())  # noqa: SLF001
        assert len(calls) == 1
        replayed_events = [
            json.loads(line)
            for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
        ]
        assert [event["event"] for event in replayed_events].count("dispatch_started") == 1
        restarted.close(wait_for_roles=True)
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_agent_and_reviewer_use_target_revision_and_activation_is_product_only(tmp_path: Path) -> None:
    """The normal Product Agent -> Reviewer flow advances only Product state."""

    phase_holder: dict[str, dict[str, object]] = {}
    calls: list[PlannerAction] = []
    store_holder: dict[str, ProductReviewStore] = {}
    prior_holder: dict[str, ProductCandidate] = {}

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        request = state.get("product_regeneration")
        if not isinstance(request, dict):
            return ()
        if request.get("status") not in {"requested", "dispatched"}:
            return ()
        revision_id = request.get("revision_id")
        if not isinstance(revision_id, str):
            return ()
        store = store_holder["store"]
        revision = store.load_revision(revision_id)
        if revision.status == "candidate":
            product = phase["product"]
            assert isinstance(product, dict)
            return (
                PlannerAction(
                    "review_final_product",
                    "product_reviewer",
                    "RUN-PRODUCT-REGENERATION",
                    "review regenerated Product revision",
                    priority=58,
                    metadata={
                        "generation_id": "G-0001",
                        "product_revision_id": revision_id,
                        "product_regeneration_request_id": request["request_id"],
                        "input_fingerprint": product["preview_input_fingerprint"],
                        "item_ids": list(product["preview_item_ids"]),
                        "item_bindings": dict(product["preview_item_bindings"]),
                            "authorization_origin": "operator_product_regeneration",
                            "output_root_ref": store.revision_artifacts_ref(revision_id),
                            "candidate_ref": revision.candidate_ref,
                            "candidate_hash": revision.candidate_hash,
                            "review_ref": f"products/generations/G-0001/product_revisions/{revision_id}/product_review.json",
                            "implementation_identity": request["implementation_identity"],
                            "regeneration_action_fingerprint": request["action_fingerprint"],
                            "regeneration_state_fingerprint": request["state_fingerprint"],
                    },
                ),
            )
        return (_regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),)

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action)
        store = store_holder["store"]
        revision_id = action.metadata["product_revision_id"]
        if action.action == "build_product_candidate":
            candidate = _materialize_revision_candidate(store, revision_id, prior_holder["candidate"])
            store.record_candidate(candidate, revision_id=revision_id)
        else:
            candidate = store.load_revision_candidate(revision_id)
            store.record_review(
                reviewer_ref="product-reviewer",
                verdict="accept",
                candidate_hash=candidate.computed_hash,
                reviewed_at="2026-01-01T00:00:01Z",
                revision_id=revision_id,
            )
        return RoleExecution(output="Product API transition persisted")

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={
            "build_product_candidate": transport,
            "review_final_product": transport,
        },
    )
    phase_holder["value"] = phase
    try:
        store, prior_candidate = _seed_reviewed_product(coordinator.context)
        store_holder["store"] = store
        prior_holder["candidate"] = prior_candidate
        pointer = store.load_active_revision()
        assert pointer is not None and pointer.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": pointer.candidate_hash}
        product["review"] = {"review_hash": pointer.review_hash}

        coordinator.regenerate_product(idempotency_key="regen-product-only")
        first = coordinator._refresh_and_launch(set())  # noqa: SLF001
        assert first.status == "dispatching"
        assert [action.role for action in calls] == ["product_agent"]
        completed = coordinator._consume_one()
        assert completed is not None
        second = coordinator._refresh_and_launch(set(), completed=completed)  # noqa: SLF001
        assert second.status == "dispatching"
        completed_review = coordinator._consume_one()
        assert completed_review is not None
        assert [action.role for action in calls] == ["product_agent", "product_reviewer"]
        terminal = coordinator._refresh_and_launch(set(), completed=completed_review)  # noqa: SLF001
        assert terminal.phase == "product_regeneration_complete"
        assert terminal.status == "ready"
        active = store.load_active_revision()
        assert active is not None and active.revision_id != pointer.revision_id and active.status == "accepted"
        assert store.load_revision(pointer.revision_id).status == "superseded"
        events = [
            json.loads(line)
            for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
        ]
        dispatched_roles = [
            event["payload"]["action"]["role"]
            for event in events
            if event["event"] == "dispatch_started"
        ]
        assert dispatched_roles == ["product_agent", "product_reviewer"]
    finally:
        coordinator.close(wait_for_roles=True)


def test_regeneration_reviewer_binding_rejects_cross_revision_and_mismatches_before_transport(tmp_path: Path) -> None:
    """A pending regeneration admits only the exact target reviewer binding."""

    phase_holder: dict[str, dict[str, object]] = {}
    action_holder: dict[str, PlannerAction] = {}
    calls: list[PlannerAction] = []

    def planner(_state: dict[str, object]):
        action = action_holder.get("action")
        return (action,) if action is not None else ()

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action)
        return RoleExecution(output="should not run for malformed reviewer binding")

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={"review_final_product": transport},
    )
    phase_holder["value"] = phase
    try:
        store, prior_candidate = _seed_reviewed_product(coordinator.context)
        pointer = store.load_active_revision()
        assert pointer is not None
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": pointer.candidate_hash}
        product["review"] = {"review_hash": pointer.review_hash}

        coordinator.regenerate_product(idempotency_key="regen-review-binding")
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None and isinstance(state.get("product_regeneration"), dict)
        request = state["product_regeneration"]
        revision_id = request["revision_id"]
        assert isinstance(revision_id, str)
        target = _materialize_revision_candidate(store, revision_id, prior_candidate)
        persisted = store.record_candidate(target, revision_id=revision_id)

        base_metadata = {
            "authorization_origin": "operator_product_regeneration",
            "generation_id": "G-0001",
            "product_revision_id": revision_id,
            "product_regeneration_request_id": request["request_id"],
            "output_root_ref": store.revision_artifacts_ref(revision_id),
            "candidate_ref": f"products/generations/G-0001/product_revisions/{revision_id}/product_candidate.json",
            "candidate_hash": persisted.computed_hash,
            "review_ref": f"products/generations/G-0001/product_revisions/{revision_id}/product_review.json",
            "input_fingerprint": request["input_fingerprint"],
            "implementation_identity": request["implementation_identity"],
            "regeneration_action_fingerprint": request["action_fingerprint"],
            "regeneration_state_fingerprint": request["state_fingerprint"],
        }

        def make_action(**updates: object) -> PlannerAction:
            metadata = dict(base_metadata)
            metadata.update(updates)
            return PlannerAction(
                "review_final_product",
                "product_reviewer",
                "RUN-PRODUCT-REGENERATION",
                "review regenerated Product revision",
                priority=58,
                metadata=metadata,
            )

        # The invalid offers are tested through the normal planner/dispatch
        # seam, proving rejection happens before any reviewer transport.
        for invalid in (
            {"product_revision_id": "rev-9999"},
            {"output_root_ref": "products/generations/G-0001/product_revisions/rev-9999/artifacts"},
            {"input_fingerprint": "f" * 64},
            {"implementation_identity": "e" * 64},
            {"candidate_hash": "d" * 64},
        ):
            action_holder["action"] = make_action(**invalid)
            coordinator._refresh_and_launch(set())  # noqa: SLF001
            assert calls == []
            assert coordinator._product_regeneration_review_binding_locked(  # noqa: SLF001
                state,
                action_holder["action"],
            ) is False
    finally:
        coordinator.close(wait_for_roles=True)


def test_rejected_product_regeneration_leaves_prior_pointer_current(tmp_path: Path) -> None:
    """A repair/block review fails only the target revision, never the prior pointer."""

    phase_holder: dict[str, dict[str, object]] = {}
    store_holder: dict[str, ProductReviewStore] = {}
    prior_holder: dict[str, ProductCandidate] = {}
    review_verdict = "repair_once"

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        request = state.get("product_regeneration")
        if not isinstance(request, dict):
            return ()
        if request.get("status") not in {"requested", "dispatched"}:
            return ()
        revision_id = request.get("revision_id")
        if not isinstance(revision_id, str):
            return ()
        store = store_holder["store"]
        revision = store.load_revision(revision_id)
        product = phase["product"]
        assert isinstance(product, dict)
        if revision.status == "candidate":
            return (
                PlannerAction(
                    "review_final_product",
                    "product_reviewer",
                    "RUN-PRODUCT-REGENERATION",
                    "review rejected Product revision",
                    priority=58,
                    metadata={
                        "generation_id": "G-0001",
                        "product_revision_id": revision_id,
                        "product_regeneration_request_id": request["request_id"],
                        "input_fingerprint": product["preview_input_fingerprint"],
                        "item_ids": list(product["preview_item_ids"]),
                        "item_bindings": dict(product["preview_item_bindings"]),
                            "authorization_origin": "operator_product_regeneration",
                            "output_root_ref": store.revision_artifacts_ref(revision_id),
                            "candidate_ref": revision.candidate_ref,
                            "candidate_hash": revision.candidate_hash,
                            "review_ref": f"products/generations/G-0001/product_revisions/{revision_id}/product_review.json",
                            "implementation_identity": request["implementation_identity"],
                            "regeneration_action_fingerprint": request["action_fingerprint"],
                            "regeneration_state_fingerprint": request["state_fingerprint"],
                    },
                ),
            )
        return (_regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),)

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        store = store_holder["store"]
        revision_id = action.metadata["product_revision_id"]
        if action.action == "build_product_candidate":
            candidate = _materialize_revision_candidate(store, revision_id, prior_holder["candidate"])
            store.record_candidate(candidate, revision_id=revision_id)
        else:
            candidate = store.load_revision_candidate(revision_id)
            store.record_review(
                reviewer_ref="product-reviewer",
                verdict=review_verdict,
                candidate_hash=candidate.computed_hash,
                reviewed_at="2026-01-01T00:00:01Z",
                revision_id=revision_id,
            )
        return RoleExecution()

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={
            "build_product_candidate": transport,
            "review_final_product": transport,
        },
    )
    phase_holder["value"] = phase
    try:
        store, prior_candidate = _seed_reviewed_product(coordinator.context)
        store_holder["store"] = store
        prior_holder["candidate"] = prior_candidate
        prior = store.load_active_revision()
        assert prior is not None and prior.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": prior.candidate_hash}
        product["review"] = {"review_hash": prior.review_hash}

        coordinator.regenerate_product(idempotency_key="regen-product-repair")
        coordinator._refresh_and_launch(set())  # noqa: SLF001
        completed = coordinator._consume_one()
        assert completed is not None
        coordinator._refresh_and_launch(set(), completed=completed)  # noqa: SLF001
        completed_review = coordinator._consume_one()
        assert completed_review is not None
        failed = coordinator._refresh_and_launch(set(), completed=completed_review)  # noqa: SLF001
        assert failed.phase == "product_regeneration_failed"
        assert failed.status == "waiting"
        current = store.load_active_revision()
        assert current is not None and current.revision_id == prior.revision_id and current.status == "accepted"
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        target_id = state["product_regeneration"]["revision_id"]
        assert store.load_revision(target_id).status == "failed"
    finally:
        coordinator.close(wait_for_roles=True)


def test_product_regeneration_retry_exhaustion_fails_target_revision_only(tmp_path: Path) -> None:
    """Bounded Product transport failure settles the target, not the run/items."""

    phase_holder: dict[str, dict[str, object]] = {}
    calls: list[PlannerAction] = []

    def planner(state: dict[str, object]):
        phase = phase_holder["value"]
        request = state.get("product_regeneration")
        if not isinstance(request, dict) or request.get("status") not in {"requested", "dispatched"}:
            return ()
        return (_regeneration_action("RUN-PRODUCT-REGENERATION", phase, state),)

    def transport(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action)
        return RoleExecution(exit_code=1, error="Product transport unavailable")

    coordinator, phase = _coordinator(
        tmp_path,
        planner=planner,
        adapters={"build_product_candidate": transport},
    )
    phase_holder["value"] = phase
    try:
        store, prior_candidate = _seed_reviewed_product(coordinator.context)
        prior = store.load_active_revision()
        assert prior is not None and prior.status == "accepted"
        product = phase["product"]
        assert isinstance(product, dict)
        product["candidate"] = {"candidate_hash": prior.candidate_hash}
        product["review"] = {"review_hash": prior.review_hash}

        coordinator.regenerate_product(idempotency_key="regen-product-exhaustion")
        for attempt in range(2):
            launched = coordinator._refresh_and_launch(set())  # noqa: SLF001
            assert launched.status == "dispatching"
            completed = coordinator._consume_one()  # noqa: SLF001
            assert completed is not None
            status = coordinator._refresh_and_launch(  # noqa: SLF001
                {coordinator._slot_key(completed[0])},  # noqa: SLF001
                completed=completed,
            )
            if attempt == 0:
                assert status.phase != "product_regeneration_failed"

        assert len(calls) == 2
        state = coordinator._read_replay()[0]  # noqa: SLF001
        assert state is not None
        request = state["product_regeneration"]
        assert isinstance(request, dict)
        assert request["status"] == "failed"
        assert state["phase"] == "product_regeneration_failed"
        target_id = request["revision_id"]
        assert store.load_revision(target_id).status == "failed"
        current = store.load_active_revision()
        assert current is not None and current.revision_id == prior.revision_id and current.status == "accepted"
    finally:
        coordinator.close(wait_for_roles=True)


def test_planner_reemits_product_regeneration_alongside_unfinished_integration(tmp_path: Path) -> None:
    """A pending Product request does not turn integration readiness into a gate."""

    run_id = "RUN-PLANNER-PRODUCT-REGENERATION"
    context = RunContext(run_id, tmp_path)
    RunLifecycle.create(context, ["REQ-001"], mode="requirement")
    from auto_foundry_core import ItemWorkspace

    item = ItemWorkspace.create(context, "REQ-001", mode="requirement", original_text="accepted business output")
    item.write_plan({"item_id": "REQ-001"})
    item.write_draft({"answer": "reviewed"})
    item.record_review("accept", reviewer_ref="business-reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    planner = RequirementSupervisorWorkspace(context)
    planner.save(
        RequirementExecutionPlan(
            input_records=(RequirementRecord(requirement_id="REQ-001", original_text="accepted business output"),),
            groups=(RequirementExecutionGroup(("REQ-001",), "one accepted item"),),
            planner_ref="planner",
            portfolio_strategy="preserve accepted output",
            revision=1,
        )
    )
    phase = planner.phase_snapshot()
    product = phase["product"]
    assert isinstance(product, dict)
    request = {
        "status": "requested",
        "authorization_origin": "operator_product_regeneration",
        "request_id": "regen-request-1",
        "run_id": run_id,
        "generation_id": "G-0001",
        "input_fingerprint": product["preview_input_fingerprint"],
        "implementation_identity": "b" * 64,
        "action_fingerprint": "c" * 64,
        "state_fingerprint": "d" * 64,
        "predecessor_product_review_ref": (
            "products/generations/G-0001/product_revisions/rev-0007/product_review.json"
        ),
        "predecessor_product_review_hash": "e" * 64,
    }

    actions = planner.next_actions(coordinator_state={"product_regeneration": request})
    product_actions = [action for action in actions if action.action == "build_product_candidate"]
    assert len(product_actions) == 1
    assert product_actions[0].role == "product_agent"
    assert product_actions[0].metadata["item_ids"] == ["REQ-001"]
    assert product_actions[0].metadata["authorization_origin"] == "operator_product_regeneration"
    assert product_actions[0].metadata["predecessor_product_review_ref"] == request["predecessor_product_review_ref"]
    assert product_actions[0].metadata["predecessor_product_review_hash"] == request["predecessor_product_review_hash"]
    assert product_actions[0].metadata["regeneration_action_fingerprint"] == request["action_fingerprint"]
    # Intentional regeneration is an exclusive Product workflow; unfinished
    # integration remains durable but is not dispatched alongside it.
    assert not any(action.action == "integrate_requirement" for action in actions)


def test_cli_exposes_explicit_product_regeneration_operation() -> None:
    args = build_parser().parse_args(
        [
            "coordinator",
            "--run-root",
            "/tmp/RUN-CLI-PRODUCT-REGENERATION",
            "regenerate_product",
            "--reason",
            "refresh reviewed dashboard",
            "--idempotency-key",
            "regen-cli-1",
        ]
    )
    assert args.operation == "regenerate_product"
    assert args.reason == "refresh reviewed dashboard"
    assert args.idempotency_key == "regen-cli-1"
