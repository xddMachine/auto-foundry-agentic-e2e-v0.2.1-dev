from __future__ import annotations

import json
import hashlib
from pathlib import Path
import zipfile

import pytest

from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core import (
    AnalystAnswer,
    AnalystWorkspace,
    BoundAnalysisContext,
    DataAssetRef,
    DataInsufficiencyConclusion,
    DataRoomWorkbench,
    ReviewFinding,
)
from auto_foundry_core.contracts import OntologyItem
from auto_foundry_core.requirement_planning import (
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementSupervisorWorkspace,
    inspect_committed_integration,
    inspect_product_manifest,
)
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.prepared import PreparedAssetRegistry
from auto_foundry_core.product_review import (
    ProductCandidate,
    ProductReviewStore,
    ProductReview,
    canonical_hash,
    discard_stale_product_candidate,
)
from auto_foundry_core.reporting import (
    ReportPreflightError,
    RunReportFinalizer,
    RunReportEventBindings,
    RunReportInputGatherer,
    inspect_report_artifacts,
)
from auto_foundry_core.workspace import RunContext


def _product_outputs(root: Path) -> dict[str, object]:
    product = root / "products" / "generations" / "G-0001"
    product.mkdir(parents=True)
    paths: dict[str, object] = {}
    for name, filename in {
        "manifest": "product_manifest.json",
        "fixture": "dashboard_fixture.json",
        "chart_map": "chart_map.json",
        "chart_registry": "chart_registry.json",
        "receipt": "build_receipt.json",
    }.items():
        path = product / filename
        path.write_text(json.dumps({"name": name}), encoding="utf-8")
        paths[name] = path
    site = product / "site"
    site.mkdir()
    (site / "index.html").write_text("<html>offline</html>", encoding="utf-8")
    paths["site"] = site
    return paths


def _report_preflight(root: Path, run_id: str):
    report = {
        "item_id": "REQ-01",
        "outcome": "accepted",
        "lifecycle_state": "accepted",
        "record_kind_totals": {},
        "implementation_sha": "a" * 40,
        "implementation_tree": "b" * 40,
        "implementation_version": "0.8.0",
    }
    bindings = RunReportEventBindings(
        invocation_receipts=({"invocation_id": "INV-1", "item_id": "REQ-01"},),
        item_manifests=({"item_id": "REQ-01", "outcome": "accepted", "record_kind_totals": {}},),
        registry_snapshot={},
        lem_snapshot={},
        timings=({"timing_id": "TIM-1", "phase": "analysis", "item_id": "REQ-01", "receipt_ref": "INV-1"},),
        incidents=(),
        business_reviews=({"item_id": "REQ-01", "review_id": "BR-1", "review_kind": "business", "findings": [], "repairs": [], "targeted_rechecks": []},),
        fidelity_reviews=({"item_id": "REQ-01", "review_id": "FR-1", "review_kind": "fidelity", "findings": [], "repairs": [], "targeted_rechecks": []},),
        implementation_transitions=(),
        implementation_metadata={"final": {"sha": "a" * 40, "tree": "b" * 40, "version": "0.8.0"}},
        implementation_identity={"sha": "a" * 40, "tree": "b" * 40, "version": "0.8.0"},
    )
    return RunReportInputGatherer(root, run_id=run_id).gather([report], event_bindings=bindings, lifecycle_status="complete")


def _blocked_planner_fixture(tmp_path: Path) -> tuple[RunContext, ItemWorkspace, RequirementSupervisorWorkspace]:
    """Build one real blocked terminal through AnalystWorkspace review APIs."""

    input_root = tmp_path / "inputs"
    input_root.mkdir(parents=True)
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,amount\nO-1,10\n")
    context = RunContext("RUN-BLOCKED-PLANNER", tmp_path / "run", (input_root,))
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    item = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="Summarize the bounded fixture.")
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item, workbench=workbench)
    analyst = AnalystWorkspace(bound, owner_ref="owner-REQ-01")
    analyst.submit_answer(AnalystAnswer(answer="Initial bounded answer.", method="Initial method."))
    finding = ReviewFinding(
        finding_id="REQ01-BLOCK",
        target_sections=("method",),
        semantic_categories=("method",),
        problem="The method cannot support the claim.",
        evidence="The fixture has no authoritative control.",
        required_change="Disclose the evidence limit.",
    )
    analyst.review.record("repair_once", reviewer_ref="business-reviewer", findings=(finding,))
    analyst.review.begin_repair()
    analyst.submit_answer(AnalystAnswer(answer="Revised bounded answer.", method="Revised method."))
    analyst.conclude_data_insufficiency(
        DataInsufficiencyConclusion(
            reason="The fixture has no authoritative control.",
            direct_answer_component="the unsupported claim",
            missing_data=("authoritative control source",),
            searches_tests=("searched the supplied catalog", "checked the bounded fixture"),
            evidence_refs=("work/business_review.json",),
            supported_components=("bounded fixture observations",),
        )
    )
    analyst.review.record("confirm_data_insufficiency", reviewer_ref="targeted-reviewer")
    analyst.review.finalize_blocked_by_evidence()
    planner = RequirementSupervisorWorkspace(context)
    planner.save(
        RequirementExecutionPlan(
            input_records=(RequirementRecord(requirement_id="REQ-01", original_text="Summarize the bounded fixture."),),
            groups=(RequirementExecutionGroup(("REQ-01",), "one blocked requirement"),),
            planner_ref="planner",
            portfolio_strategy="single item",
            revision=1,
        )
    )
    return context, item, planner


def test_product_candidate_review_and_publish_are_independent_and_idempotent(tmp_path: Path) -> None:
    context = RunContext("RUN-PRODUCT-GATE", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    outputs = _product_outputs(context.run_root)
    plan_path = context.run_root / "requirement_supervisor_plan.json"
    plan_path.write_text("{\"schema_version\": 1}\n", encoding="utf-8")
    policy = {"enabled": True, "publication": "local-test"}
    policy_hash = canonical_hash(policy)
    bindings = {name: {"ref": str(path.relative_to(context.run_root))} for name, path in outputs.items()}
    with pytest.raises(ValueError, match="parent_lineage"):
        ProductCandidate(
            run_id=context.run_id,
            generation_id="G-0001",
            product_owner="product-owner",
            parent_lineage={},
            plan_binding={"plan_ref": "extensions/G-0001/requirement_supervisor_plan.json", "plan_hash": "b" * 64},
            publication_policy_hash=policy_hash,
            artifact_bindings=bindings,
        )
    with pytest.raises(ValueError, match="plan_binding"):
        ProductCandidate(
            run_id=context.run_id,
            generation_id="G-0001",
            product_owner="product-owner",
            parent_lineage={"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None},
            plan_binding={},
            publication_policy_hash=policy_hash,
            artifact_bindings=bindings,
        )
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
        product_owner="product-owner",
        parent_lineage={"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None},
        plan_binding={"plan_ref": "requirement_supervisor_plan.json", "plan_hash": __import__("hashlib").sha256(plan_path.read_bytes()).hexdigest()},
        publication_policy_hash=policy_hash,
        artifact_bindings=bindings,
    )
    store = ProductReviewStore(context, "G-0001")
    canonical_candidate = candidate.to_dict()
    for alias, field in (
        ("owner_ref", "product_owner"),
        ("policy_hash", "publication_policy_hash"),
        ("outputs", "artifact_bindings"),
    ):
        aliased = dict(canonical_candidate)
        aliased[alias] = aliased.pop(field)
        with pytest.raises(ValueError, match="schema is not exact"):
            store.record_candidate(aliased)
        assert not store.candidate_path.exists()
    unknown = dict(canonical_candidate)
    unknown["unexpected"] = True
    with pytest.raises(ValueError, match="schema is not exact"):
        store.record_candidate(unknown)
    assert not store.candidate_path.exists()
    missing = dict(canonical_candidate)
    missing.pop("candidate_hash")
    with pytest.raises(ValueError, match="schema is not exact"):
        store.record_candidate(missing)
    assert not store.candidate_path.exists()
    persisted_candidate = store.record_candidate(candidate)
    assert persisted_candidate.candidate_hash
    assert store.record_candidate(persisted_candidate).to_dict() == persisted_candidate.to_dict()
    manifest_path = outputs["manifest"]
    assert isinstance(manifest_path, Path)
    original_manifest = manifest_path.read_bytes()
    manifest_path.write_bytes(original_manifest + b"tamper")
    with pytest.raises(ValueError, match="hash"):
        store.record_candidate(persisted_candidate)
    manifest_path.write_bytes(original_manifest)

    with pytest.raises(ValueError, match="independent"):
        store.record_review(reviewer_ref="product-owner", verdict="accept")
    review = store.record_review(reviewer_ref="reviewer", verdict="accept", reviewed_at="2026-01-01T00:00:00Z")
    assert isinstance(review, ProductReview)
    assert store.record_review(reviewer_ref="reviewer", verdict="accept", reviewed_at="2026-01-01T00:00:00Z").to_dict() == review.to_dict()
    authorization = store.authorize_publish(
        publisher_ref="publisher",
        publication_policy=policy,
        authorized_at="2026-01-01T00:00:01Z",
    )
    assert authorization.authorization_hash
    assert store.authorize_publish(
        publisher_ref="publisher",
        publication_policy=policy,
        authorized_at="2026-01-01T00:00:01Z",
    ).to_dict() == authorization.to_dict()
    review_bytes = store.review_path.read_bytes()
    authorization_bytes = store.authorization_path.read_bytes()
    review_mtime = store.review_path.stat().st_mtime_ns
    authorization_mtime = store.authorization_path.stat().st_mtime_ns
    manifest_path.write_bytes(original_manifest + b"tamper-again")
    with pytest.raises(ValueError, match="hash|artifact"):
        store.load_review()
    with pytest.raises(ValueError, match="hash|artifact"):
        store.load_authorization()
    assert store.review_path.read_bytes() == review_bytes
    assert store.authorization_path.read_bytes() == authorization_bytes
    assert store.review_path.stat().st_mtime_ns == review_mtime
    assert store.authorization_path.stat().st_mtime_ns == authorization_mtime
    manifest_path.write_bytes(original_manifest)
    with pytest.raises(PermissionError, match="denied"):
        ProductReviewStore(context, "G-0001").authorize_publish(
            publisher_ref="other",
            publication_policy={"enabled": False},
        )
    with pytest.raises(PermissionError, match="full canonical policy"):
        ProductReviewStore(context, "G-0001").authorize_publish(
            publisher_ref="other",
            publication_policy_hash=policy_hash,
        )


def test_stale_product_candidate_discard_rebuilds_refs_and_guards_review_auth(tmp_path: Path) -> None:
    context = RunContext("RUN-PRODUCT-CANDIDATE-REBUILD", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    outputs = _product_outputs(context.run_root)
    plan_path = context.run_root / "requirement_supervisor_plan.json"
    plan_path.write_text('{"schema_version": 1}\n', encoding="utf-8")
    policy = {"enabled": True, "publication": "local-test"}
    policy_hash = canonical_hash(policy)
    bindings = {name: {"ref": str(path.relative_to(context.run_root))} for name, path in outputs.items()}
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
        product_owner="product-owner",
        parent_lineage={"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None},
        plan_binding={"plan_ref": "requirement_supervisor_plan.json", "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest()},
        publication_policy_hash=policy_hash,
        artifact_bindings=bindings,
    )
    store = ProductReviewStore(context, "G-0001")
    persisted = store.record_candidate(candidate)
    manifest_path = outputs["manifest"]
    assert isinstance(manifest_path, Path)
    original_manifest = manifest_path.read_bytes()

    # Match the live G5 failure: the durable candidate points at old output hashes.
    manifest_path.write_bytes(original_manifest + b"stale-g5-output")
    with pytest.raises(ValueError, match="hash"):
        store.load_candidate()
    assert discard_stale_product_candidate(context, generation_id="G-0001") is True
    assert not store.candidate_path.exists()
    assert discard_stale_product_candidate(context, generation_id="G-0001") is False

    # A refs-only retry binds the current bytes and is stable thereafter.
    manifest_path.write_bytes(original_manifest)
    rebuilt = store.record_candidate(candidate)
    assert rebuilt.artifact_bindings["manifest"]["sha256"] == hashlib.sha256(original_manifest).hexdigest()
    assert store.load_candidate().to_dict() == rebuilt.to_dict()
    assert store.discard_stale_candidate_for_rebuild() is False

    # Neither a regular review/auth file nor a symlink may be bypassed by cleanup.
    marker = tmp_path / "marker.json"
    marker.write_text("{}\n", encoding="utf-8")
    store.review_path.symlink_to(marker)
    with pytest.raises(ValueError, match="review or authorization"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()
    store.review_path.unlink()
    store.authorization_path.symlink_to(marker)
    with pytest.raises(ValueError, match="review or authorization"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()
    store.authorization_path.unlink()

    review = store.record_review(reviewer_ref="reviewer", verdict="accept", reviewed_at="2026-01-01T00:00:00Z")
    assert review.candidate_hash == rebuilt.computed_hash
    manifest_path.write_bytes(original_manifest + b"review-guard")
    with pytest.raises(ValueError, match="review or authorization"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()
    manifest_path.write_bytes(original_manifest)

    store.authorize_publish(
        publisher_ref="publisher",
        publication_policy=policy,
        authorized_at="2026-01-01T00:00:01Z",
    )
    manifest_path.write_bytes(original_manifest + b"authorization-guard")
    with pytest.raises(ValueError, match="review or authorization"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()


def test_stale_product_candidate_discard_fails_closed_for_foreign_malformed_and_lineage(tmp_path: Path) -> None:
    context = RunContext("RUN-PRODUCT-CANDIDATE-GUARDS", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    outputs = _product_outputs(context.run_root)
    plan_path = context.run_root / "requirement_supervisor_plan.json"
    plan_path.write_text('{"schema_version": 1}\n', encoding="utf-8")
    policy_hash = canonical_hash({"enabled": True, "publication": "local-test"})
    bindings = {name: {"ref": str(path.relative_to(context.run_root))} for name, path in outputs.items()}
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
        product_owner="product-owner",
        parent_lineage={"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None},
        plan_binding={"plan_ref": "requirement_supervisor_plan.json", "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest()},
        publication_policy_hash=policy_hash,
        artifact_bindings=bindings,
    )
    store = ProductReviewStore(context, "G-0001")
    persisted = store.record_candidate(candidate)
    durable = persisted.to_dict()

    def write_payload(payload: dict[str, object]) -> None:
        store.candidate_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    foreign = dict(durable)
    foreign["run_id"] = "RUN-FOREIGN"
    foreign["candidate_hash"] = canonical_hash({key: value for key, value in foreign.items() if key != "candidate_hash"})
    write_payload(foreign)
    with pytest.raises(ValueError, match="another run or generation"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()

    write_payload({})
    with pytest.raises(ValueError, match="schema"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()

    lineage_stale = dict(durable)
    lineage_stale["parent_lineage"] = {
        "root_generation": False,
        "parent_generation_id": "G-0000",
        "parent_manifest_ref": "extensions/G-0000/generation_manifest.json",
        "parent_manifest_hash": "0" * 64,
    }
    lineage_stale["candidate_hash"] = canonical_hash({key: value for key, value in lineage_stale.items() if key != "candidate_hash"})
    write_payload(lineage_stale)
    with pytest.raises(ValueError, match="parent generation metadata"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()

    malformed_kind = dict(durable)
    malformed_kind_bindings = {name: dict(binding) for name, binding in durable["artifact_bindings"].items()}
    malformed_kind_bindings["manifest"]["kind"] = 17
    malformed_kind["artifact_bindings"] = malformed_kind_bindings
    malformed_kind["candidate_hash"] = canonical_hash({key: value for key, value in malformed_kind.items() if key != "candidate_hash"})
    write_payload(malformed_kind)
    with pytest.raises(ValueError, match="kind does not match"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()

    malformed_files = dict(durable)
    malformed_files_bindings = {name: dict(binding) for name, binding in durable["artifact_bindings"].items()}
    malformed_files_bindings["site"]["files"] = ["not-a-file-map"]
    malformed_files["artifact_bindings"] = malformed_files_bindings
    malformed_files["candidate_hash"] = canonical_hash({key: value for key, value in malformed_files.items() if key != "candidate_hash"})
    write_payload(malformed_files)
    with pytest.raises(ValueError, match="files binding is malformed"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.exists()

    # Candidate symlinks are rejected even when no review/auth exists.
    store.candidate_path.unlink()
    marker = tmp_path / "candidate-target.json"
    marker.write_text("{}\n", encoding="utf-8")
    store.candidate_path.symlink_to(marker)
    with pytest.raises(ValueError, match="candidate cannot be a symlink"):
        store.discard_stale_candidate_for_rebuild()
    assert store.candidate_path.is_symlink()


def test_product_receipt_inspector_rejects_foreign_extension_receipt(tmp_path: Path) -> None:
    context = RunContext("RUN-PRODUCT-RECEIPT", tmp_path / "run")
    root = context.run_root
    parent_ref = "products/generations/G-0001/product_manifest.json"
    parent_path = root / parent_ref
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    parent_path.write_text('{"parent":true}\n', encoding="utf-8")
    parent_hash = __import__("hashlib").sha256(parent_path.read_bytes()).hexdigest()
    generation_root = root / "products/generations/G-0002/dashboard"
    generation_root.mkdir(parents=True, exist_ok=True)
    fixture_path = generation_root / "dashboard_fixture.json"
    chart_map_path = generation_root / "chart_map.json"
    chart_registry_path = generation_root / "chart_registry.json"
    for path, payload in (
        (fixture_path, {"widgets": []}),
        (chart_map_path, {"charts": []}),
        (chart_registry_path, {"charts": []}),
    ):
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    site_path = generation_root / "site"
    site_path.mkdir()
    site_manifest_path = site_path / "site_manifest.json"
    site_manifest_path.write_text('{"site":true}\n', encoding="utf-8")
    plan_ref = "extensions/G-0002/business_presentation_plan.json"
    plan_path = root / plan_ref
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text('{"plan":true}\n', encoding="utf-8")
    plan_hash = __import__("hashlib").sha256(plan_path.read_bytes()).hexdigest()
    supervisor_plan_ref = "extensions/G-0002/requirement_supervisor_plan.json"
    supervisor_plan_path = root / supervisor_plan_ref
    supervisor_plan_path.write_text('{"supervisor":true}\n', encoding="utf-8")
    supervisor_plan_hash = __import__("hashlib").sha256(supervisor_plan_path.read_bytes()).hexdigest()
    receipt_ref = "products/generations/G-0002/dashboard/build_receipt.json"
    receipt_path = root / receipt_ref
    output_hashes = {
        "fixture_sha256": __import__("hashlib").sha256(fixture_path.read_bytes()).hexdigest(),
        "chart_map_sha256": __import__("hashlib").sha256(chart_map_path.read_bytes()).hexdigest(),
        "chart_registry_sha256": __import__("hashlib").sha256(chart_registry_path.read_bytes()).hexdigest(),
        "site_manifest_sha256": __import__("hashlib").sha256(site_manifest_path.read_bytes()).hexdigest(),
    }
    receipt = {
        "schema_version": "1",
        "status": "complete",
        "run_id": context.run_id,
        "generation_id": "G-0002",
        "parent_generation_id": "G-0001",
        "new_analytics": False,
        "parent": {"product_manifest_ref": parent_ref, "product_manifest_sha256": parent_hash},
        "plan_binding": {
            "ref": supervisor_plan_ref,
            "sha256": supervisor_plan_hash,
            "admission_sha256": supervisor_plan_hash,
            "generation_id": "G-0002",
        },
        "presentation_plan_ref": plan_ref,
        "presentation_plan_sha256": plan_hash,
        "outputs": {
            "fixture_ref": str(fixture_path.relative_to(root)),
            "chart_map_ref": str(chart_map_path.relative_to(root)),
            "chart_registry_ref": str(chart_registry_path.relative_to(root)),
            "site_ref": str(site_path.relative_to(root)),
            "receipt_ref": receipt_ref,
        },
        "output_hashes": output_hashes,
    }
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    receipt_hash = __import__("hashlib").sha256(receipt_path.read_bytes()).hexdigest()
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
        "lifecycle": {"generation_id": "G-0002"},
        "dashboard": {"receipt_ref": receipt_ref, "receipt_sha256": receipt_hash},
        "lineage": {
            "parent_generation_id": "G-0001",
            "parent_product_manifest_ref": parent_ref,
            "parent_product_manifest_sha256": parent_hash,
            "delta_receipt_ref": receipt_ref,
        },
        "presentation_plan_ref": plan_ref,
        "presentation_plan_sha256": plan_hash,
    }
    manifest_ref = "products/generations/G-0002/product_manifest.json"
    manifest_path = root / manifest_ref
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    inspected = inspect_product_manifest(context, "G-0002", manifest_ref)
    assert inspected["valid"] is True

    def rewrite_bound_receipt() -> None:
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        manifest["dashboard"]["receipt_sha256"] = __import__("hashlib").sha256(receipt_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    receipt["run_id"] = "RUN-FOREIGN"
    rewrite_bound_receipt()
    foreign = inspect_product_manifest(context, "G-0002", manifest_ref)
    assert foreign["valid"] is False
    assert any("run/generation lineage" in diagnostic for diagnostic in foreign["diagnostics"])
    receipt["run_id"] = context.run_id
    receipt["generation_id"] = "G-9999"
    rewrite_bound_receipt()
    foreign_generation = inspect_product_manifest(context, "G-0002", manifest_ref)
    assert foreign_generation["valid"] is False
    assert any("run/generation lineage" in diagnostic for diagnostic in foreign_generation["diagnostics"])
    receipt["generation_id"] = "G-0002"
    receipt.pop("plan_binding")
    rewrite_bound_receipt()
    missing_plan = inspect_product_manifest(context, "G-0002", manifest_ref)
    assert missing_plan["valid"] is False
    assert any("plan binding is missing" in diagnostic for diagnostic in missing_plan["diagnostics"])
    receipt["plan_binding"] = {
        "ref": supervisor_plan_ref,
        "sha256": "0" * 64,
        "admission_sha256": supervisor_plan_hash,
        "generation_id": "G-0002",
    }
    rewrite_bound_receipt()
    stale_plan = inspect_product_manifest(context, "G-0002", manifest_ref)
    assert stale_plan["valid"] is False
    assert any("plan binding is stale" in diagnostic for diagnostic in stale_plan["diagnostics"])


def test_product_receipt_inspector_requires_root_parent_marker_and_plan_binding(tmp_path: Path) -> None:
    context = RunContext("RUN-PRODUCT-ROOT-RECEIPT", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    root = context.run_root
    plan_path = root / "requirement_supervisor_plan.json"
    plan_path.write_text('{"supervisor":true}\n', encoding="utf-8")
    plan_hash = __import__("hashlib").sha256(plan_path.read_bytes()).hexdigest()
    product_root = root / "products/dashboard"
    product_root.mkdir(parents=True, exist_ok=True)
    fixture_path = product_root / "dashboard_fixture.json"
    chart_map_path = product_root / "chart_map.json"
    chart_registry_path = product_root / "chart_registry.json"
    for path in (fixture_path, chart_map_path, chart_registry_path):
        path.write_text("{}\n", encoding="utf-8")
    site_path = product_root / "site"
    site_path.mkdir()
    site_manifest_path = site_path / "site_manifest.json"
    site_manifest_path.write_text("{}\n", encoding="utf-8")
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
            "site_ref": str(site_path.relative_to(root)),
            "receipt_ref": receipt_ref,
        },
        "output_hashes": {
            "fixture_sha256": __import__("hashlib").sha256(fixture_path.read_bytes()).hexdigest(),
            "chart_map_sha256": __import__("hashlib").sha256(chart_map_path.read_bytes()).hexdigest(),
            "chart_registry_sha256": __import__("hashlib").sha256(chart_registry_path.read_bytes()).hexdigest(),
            "site_manifest_sha256": __import__("hashlib").sha256(site_manifest_path.read_bytes()).hexdigest(),
        },
    }
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest_ref = "products/product_manifest.json"
    manifest_path = root / manifest_ref
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
            "receipt_sha256": __import__("hashlib").sha256(receipt_path.read_bytes()).hexdigest(),
        },
        "lineage": {"root_generation": True},
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    assert inspect_product_manifest(context, "G-0001", manifest_ref)["valid"] is True
    receipt.pop("parent")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest["dashboard"]["receipt_sha256"] = __import__("hashlib").sha256(receipt_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    invalid_root = inspect_product_manifest(context, "G-0001", manifest_ref)
    assert invalid_root["valid"] is False
    assert any("root parent binding" in diagnostic for diagnostic in invalid_root["diagnostics"])


def test_report_preflight_requires_authoritative_bindings_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "run"
    report = {
        "item_id": "REQ-01",
        "outcome": "accepted",
        "lifecycle_state": "accepted",
        "record_kind_totals": {},
        "implementation_sha": "a" * 40,
        "implementation_tree": "b" * 40,
        "implementation_version": "0.8.0",
    }
    gatherer = RunReportInputGatherer(root, run_id="RUN-REPORT-GATE")
    with pytest.raises(ReportPreflightError, match="authoritative inputs"):
        gatherer.gather([report], lifecycle_status="complete")
    bindings = RunReportEventBindings(
        invocation_receipts=({"invocation_id": "INV-1", "item_id": "REQ-01"},),
        item_manifests=({"item_id": "REQ-01", "outcome": "accepted"},),
        registry_snapshot={},
        lem_snapshot={},
        timings=({"timing_id": "TIM-1", "phase": "analysis", "item_id": "REQ-01", "receipt_ref": "INV-1"},),
        incidents=(),
        business_reviews=({"item_id": "REQ-01", "review_id": "BR-1", "review_kind": "business", "findings": [], "repairs": [], "targeted_rechecks": []},),
        fidelity_reviews=({"item_id": "REQ-01", "review_id": "FR-1", "review_kind": "fidelity", "findings": [], "repairs": [], "targeted_rechecks": []},),
        implementation_transitions=(),
        implementation_metadata={"final": {"sha": "a" * 40, "tree": "b" * 40, "version": "0.8.0"}},
        implementation_identity={"sha": "a" * 40, "tree": "b" * 40, "version": "0.8.0"},
    )
    first = gatherer.gather([report], event_bindings=bindings, lifecycle_status="complete")
    preflight_path = root / "reporting" / "report_preflight.json"
    first_mtime = preflight_path.stat().st_mtime_ns
    second = gatherer.gather([report], event_bindings=bindings, lifecycle_status="complete")
    assert first.to_dict() == second.to_dict() == gatherer.load().to_dict()
    assert preflight_path.stat().st_mtime_ns == first_mtime
    for field_name in (
        "invocation_receipts",
        "item_manifests",
        "registry_snapshot",
        "lem_snapshot",
        "timings",
        "incidents",
        "business_reviews",
        "fidelity_reviews",
        "implementation_transitions",
        "implementation_metadata",
        "implementation_identity",
    ):
        incomplete = bindings.to_dict()
        incomplete[field_name] = None
        with pytest.raises(ReportPreflightError, match="(authoritative inputs|inputs are invalid)"):
            RunReportInputGatherer(tmp_path / f"missing-{field_name}", run_id="RUN-REPORT-GATE").gather(
                [report], event_bindings=incomplete, lifecycle_status="complete"
            )
    preflight_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="preflight"):
        gatherer.load()


def test_blocked_terminal_is_valid_noop_integration_and_routes_product(tmp_path: Path) -> None:
    _context, _item, planner = _blocked_planner_fixture(tmp_path)
    snapshot = planner.phase_snapshot()
    item_view = snapshot["items"]["REQ-01"]
    assert item_view["terminal_status"] == "blocked_by_evidence"
    assert item_view["integration_state"] == "pending"
    assert item_view["integration_stage"] == "blocked_by_evidence"
    assert item_view["blocked_integration_validation"] == {
        "valid": True,
        "stage": "blocked_by_evidence",
        "diagnostics": [],
    }
    assert snapshot["all_items_integrated"] is True
    actions = planner.next_actions()
    assert all(
        action.action
        not in {"integrate_requirement", "review_integration_fidelity", "repair_integration_fidelity", "commit_integration_requirement"}
        for action in actions
    )
    assert any(action.action in {"build_product_candidate", "build_final_product"} for action in actions)


@pytest.mark.parametrize("tamper", ("pointer", "residue"))
def test_blocked_terminal_integration_tamper_stays_in_repair(tmp_path: Path, tamper: str) -> None:
    context, item, planner = _blocked_planner_fixture(tmp_path / tamper)
    state_path = context.run_root / "requirements" / item.item_id / "item_state.json"
    if tamper == "pointer":
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["integration_manifest_ref"] = "integration/committed/manifest.json"
        state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    else:
        residue = context.run_root / "requirements" / item.item_id / "integration" / "committed"
        residue.mkdir(parents=True)
        (residue / "foreign.json").write_text("{}\n", encoding="utf-8")
    snapshot = planner.phase_snapshot()
    item_view = snapshot["items"][item.item_id]
    assert item_view["integration_stage"] == "invalid"
    assert item_view["blocked_integration_validation"]["valid"] is False
    assert snapshot["all_items_integrated"] is False
    actions = planner.next_actions()
    assert [(action.action, action.role) for action in actions] == [("repair_integration_fidelity", "integration_agent")]
    assert actions[0].metadata["requires_rethink"] is True


def test_planner_reads_integration_fidelity_boundary_from_persisted_artifacts(tmp_path: Path) -> None:
    context = RunContext("RUN-INTEGRATION-PHASES", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    item = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="integrate")
    item.write_draft({"answer": "accepted"})
    item.record_review("accept", reviewer_ref="business-reviewer")
    item.accept(accepted_refs=("draft.json",))
    RequirementSupervisorWorkspace(context).save(
        RequirementExecutionPlan(
            input_records=(RequirementRecord(requirement_id="REQ-01", original_text="integrate"),),
            groups=(RequirementExecutionGroup(("REQ-01",), "one item"),),
            planner_ref="planner",
            portfolio_strategy="single item",
            revision=1,
        )
    )
    planner = RequirementSupervisorWorkspace(context)
    assert [(a.action, a.role) for a in planner.next_actions()] == [("integrate_requirement", "integration_agent")]

    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-REQ-01",
    )
    session.add_ontology_item(
        OntologyItem(item_id="customer", item_type="entity", label="Customer"),
        scope="requirement",
        evidence_refs=("draft.json",),
    )
    session.add_ontology_item(
        OntologyItem(item_id="supplier", item_type="entity", label="Supplier"),
        scope="requirement",
        evidence_refs=("draft.json",),
    )
    session.build_fidelity_packet()
    assert [(a.action, a.role) for a in planner.next_actions()] == [("review_integration_fidelity", "integration_fidelity_reviewer")]
    record_id = session.records[0].record_id
    dependency_id = session.records[1].record_id
    session.record_fidelity_review(
        "repair_once",
        affected_record_ids=(record_id,),
        dependency_ids=(dependency_id,),
        checked_record_ids=(record_id, dependency_id),
    )
    assert [(a.action, a.role) for a in planner.next_actions()] == [("repair_integration_fidelity", "integration_agent")]

    result_path = context.run_root / "requirements" / "REQ-01" / "integration" / "review" / "result.json"
    original_result = result_path.read_bytes()
    forged = json.loads(original_result)
    forged["verdict"] = "accept"
    result_path.write_text(json.dumps(forged), encoding="utf-8")
    snapshot = planner.phase_snapshot()["items"]["REQ-01"]
    assert snapshot["integration_validation"]["valid"] is False
    assert snapshot["integration_stage"] == "invalid"
    assert all(action.action != "commit_integration_requirement" for action in planner.next_actions())
    result_path.write_bytes(original_result)
    corrected_payload = dict(session.records[0].payload)
    corrected_payload["label"] = "Corrected Customer"
    session.correct_record(record_id, corrected_payload)
    session.build_fidelity_packet()
    transition = planner.phase_snapshot()["items"]["REQ-01"]
    assert transition["integration_validation"]["valid"] is True
    assert transition["integration_validation"]["stage"] == "awaiting_targeted_fidelity_review"
    assert transition["integration_stage"] == "awaiting_targeted_fidelity_review"
    assert transition["fidelity_verdict"] is None
    assert [(a.action, a.role) for a in planner.next_actions()] == [
        ("review_integration_fidelity", "integration_fidelity_reviewer")
    ]
    assert planner.next_actions()[0].metadata["targeted_recheck"] is True

    # A forged-but-self-hashed progress file that drops the authorized
    # correction must remain fail-closed; it is not another review handoff.
    progress_path = context.run_root / "requirements/REQ-01/integration/review/repair_progress.json"
    original_progress = progress_path.read_bytes()
    forged_progress = json.loads(original_progress)
    forged_progress["corrected_record_hashes"] = {}
    unsigned_progress = {
        key: value for key, value in forged_progress.items() if key != "progress_hash"
    }
    forged_progress["progress_hash"] = hashlib.sha256(
        json.dumps(
            unsigned_progress,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    progress_path.write_text(json.dumps(forged_progress), encoding="utf-8")
    invalid_transition = planner.phase_snapshot()["items"]["REQ-01"]
    assert invalid_transition["integration_validation"]["valid"] is False
    assert any("incomplete" in diagnostic for diagnostic in invalid_transition["integration_validation"]["diagnostics"])
    assert invalid_transition["integration_stage"] == "invalid"
    assert [(a.action, a.role) for a in planner.next_actions()] == [
        ("repair_integration_fidelity", "integration_agent")
    ]

    # A dependency is review scope, not mutation scope.
    dependency_progress = json.loads(original_progress)
    dependency_progress["corrected_record_hashes"] = {
        json.loads(original_result)["dependency_ids"][0]: "a" * 64,
    }
    unsigned_dependency_progress = {
        key: value for key, value in dependency_progress.items() if key != "progress_hash"
    }
    dependency_progress["progress_hash"] = hashlib.sha256(
        json.dumps(
            unsigned_dependency_progress,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    progress_path.write_text(json.dumps(dependency_progress), encoding="utf-8")
    out_of_scope = planner.phase_snapshot()["items"]["REQ-01"]
    assert out_of_scope["integration_validation"]["valid"] is False
    assert any("unauthorized" in diagnostic for diagnostic in out_of_scope["integration_validation"]["diagnostics"])
    progress_path.write_bytes(original_progress)

    session.record_fidelity_review(
        "accept",
        review_kind="targeted",
        checked_record_ids=(record_id, dependency_id),
    )
    assert [(a.action, a.role) for a in planner.next_actions()] == [("commit_integration_requirement", "integration_agent")]
    session.commit()
    committed_snapshot = planner.phase_snapshot()
    committed_item = committed_snapshot["items"]["REQ-01"]
    assert committed_item["integration_stage"] == "committed"
    assert committed_item["committed_integration_validation"]["valid"] is True
    assert committed_snapshot["all_items_integrated"] is True

    manifest_path = context.run_root / "requirements/REQ-01/integration/committed/manifest.json"
    original_state_bytes = (context.run_root / "requirements/REQ-01/item_state.json").read_bytes()
    original_manifest_bytes = manifest_path.read_bytes()
    decoy = context.run_root / "integration/committed/manifest.json"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_bytes(original_manifest_bytes)
    assert inspect_committed_integration(context, "REQ-01")["valid"] is True

    state_path = context.run_root / "requirements/REQ-01/item_state.json"
    foreign_state = json.loads(original_state_bytes)
    foreign_state["integration_manifest_ref"] = "requirements/REQ-01/integration/committed/manifest.json"
    state_path.write_text(json.dumps(foreign_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    foreign = inspect_committed_integration(context, "REQ-01")
    assert foreign["valid"] is False
    assert any("item-relative" in diagnostic or "stale or foreign" in diagnostic for diagnostic in foreign["diagnostics"])
    state_path.write_bytes(original_state_bytes)

    manifest_path.unlink()
    manifest_path.symlink_to(decoy)
    aliased = inspect_committed_integration(context, "REQ-01")
    assert aliased["valid"] is False
    assert any("symlinked" in diagnostic for diagnostic in aliased["diagnostics"])
    manifest_path.unlink()
    manifest_path.write_bytes(original_manifest_bytes)


@pytest.mark.parametrize("tamper", ("stale", "missing", "foreign"))
def test_technical_failure_manifest_is_validated_before_product_routing(tmp_path: Path, tamper: str) -> None:
    context = RunContext("RUN-TECHNICAL-FAILURE-BOUNDARY", tmp_path / tamper)
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    item = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="unrecoverable")
    item.write_draft({"answer": "accepted"})
    item.record_review("accept", reviewer_ref="business-reviewer")
    item.accept(accepted_refs=("draft.json",))
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-REQ-01",
    )
    session.mark_technical_failure("unrecoverable integration")
    RequirementSupervisorWorkspace(context).save(
        RequirementExecutionPlan(
            input_records=(RequirementRecord(requirement_id="REQ-01", original_text="unrecoverable"),),
            groups=(RequirementExecutionGroup(("REQ-01",), "one item"),),
            planner_ref="planner",
            portfolio_strategy="single item",
            revision=1,
        )
    )
    failure_path = context.run_root / "requirements/REQ-01/integration/technical_failure/manifest.json"
    if tamper == "stale":
        value = json.loads(failure_path.read_text(encoding="utf-8"))
        value["reason"] = "different reason"
        failure_path.write_text(json.dumps(value), encoding="utf-8")
    elif tamper == "missing":
        failure_path.unlink()
    else:
        value = json.loads(failure_path.read_text(encoding="utf-8"))
        value["item_id"] = "REQ-FOREIGN"
        unsigned = {key: value for key, value in value.items() if key != "manifest_hash"}
        value["manifest_hash"] = __import__("hashlib").sha256(
            (json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        ).hexdigest()
        failure_path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    planner = RequirementSupervisorWorkspace(context)
    snapshot = planner.phase_snapshot()
    item_view = snapshot["items"]["REQ-01"]
    assert item_view["committed_integration_validation"]["valid"] is False
    assert snapshot["all_items_integrated"] is False
    actions = planner.next_actions()
    assert any(action.action == "repair_integration_fidelity" for action in actions)
    assert all(action.action not in {"build_final_product", "build_product_candidate"} for action in actions)


@pytest.mark.parametrize("tamper", ("state", "pointer"))
def test_phase_snapshot_forged_lifecycle_fails_closed_without_advancement(tmp_path: Path, tamper: str) -> None:
    context = RunContext("RUN-LIFECYCLE-BOUNDARY", tmp_path / tamper)
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    target = context.run_root / ("run_state.json" if tamper == "state" else "active_generation.json")
    if tamper == "state":
        value = json.loads(target.read_text(encoding="utf-8"))
        value["status"] = "forged"
    else:
        value = {"schema_version": 1, "kind": "forged_active_generation"}
    target.write_text(json.dumps(value), encoding="utf-8")
    planner = RequirementSupervisorWorkspace(context)
    snapshot = planner.phase_snapshot()
    assert snapshot["lifecycle_validation"]["valid"] is False
    actions = planner.next_actions()
    assert [(action.action, action.role) for action in actions] == [("repair_run_lifecycle", "planner")]


def test_report_finalizer_requires_persisted_preflight_and_recovers_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "run"
    report = {
        "item_id": "REQ-01",
        "outcome": "accepted",
        "lifecycle_state": "accepted",
        "record_kind_totals": {},
        "implementation_sha": "a" * 40,
        "implementation_tree": "b" * 40,
        "implementation_version": "0.8.0",
    }
    bindings = RunReportEventBindings(
        invocation_receipts=({"invocation_id": "INV-1", "item_id": "REQ-01"},),
        item_manifests=({"item_id": "REQ-01", "outcome": "accepted"},),
        registry_snapshot={},
        lem_snapshot={},
        timings=({"timing_id": "TIM-1", "phase": "analysis", "item_id": "REQ-01", "receipt_ref": "INV-1"},),
        incidents=(),
        business_reviews=({"item_id": "REQ-01", "review_id": "BR-1", "review_kind": "business", "findings": [], "repairs": [], "targeted_rechecks": []},),
        fidelity_reviews=({"item_id": "REQ-01", "review_id": "FR-1", "review_kind": "fidelity", "findings": [], "repairs": [], "targeted_rechecks": []},),
        implementation_transitions=(),
        implementation_metadata={"final": {"sha": "a" * 40, "tree": "b" * 40, "version": "0.8.0"}},
        implementation_identity={"sha": "a" * 40, "tree": "b" * 40, "version": "0.8.0"},
    )
    gatherer = RunReportInputGatherer(root, run_id="RUN-REPORT-FINALIZER")
    preflight = gatherer.gather([report], event_bindings=bindings, lifecycle_status="complete")
    with pytest.raises((FileNotFoundError, ValueError), match="preflight"):
        RunReportFinalizer(tmp_path / "strict-without-preflight").finalize(
            dict(preflight.projected_report), lifecycle_status="complete"
        )
    finalizer = RunReportFinalizer(root)

    original_replace = __import__("auto_foundry_core.reporting", fromlist=["os"]).os.replace
    failed = {"done": False}

    class SimulatedProcessDeath(BaseException):
        pass

    def fail_stage_swap(source: object, destination: object) -> object:
        if not failed["done"] and str(source).find(".reporting.finalize-staging-") >= 0 and Path(destination).name == "reporting":
            failed["done"] = True
            raise SimulatedProcessDeath("simulated process death during report directory swap")
        return original_replace(source, destination)

    monkeypatch.setattr("auto_foundry_core.reporting.os.replace", fail_stage_swap)
    with pytest.raises(SimulatedProcessDeath, match="simulated process death"):
        finalizer.finalize(preflight, lifecycle_status="complete")
    assert (root / ".reporting.finalize.intent.json").is_file()
    assert any(path.name.startswith(".reporting.finalize-staging-") for path in root.iterdir())
    pending = inspect_report_artifacts(root, run_id="RUN-REPORT-FINALIZER")
    assert pending["stage"] == "recovery_required"
    assert pending["valid"] is False
    monkeypatch.setattr("auto_foundry_core.reporting.os.replace", original_replace)

    receipt = finalizer.finalize(preflight, lifecycle_status="complete")
    assert receipt["terminal_status"] == "terminalized"
    assert inspect_report_artifacts(root, run_id="RUN-REPORT-FINALIZER")["stage"] == "finalized"
    assert not (root / ".reporting.finalize.intent.json").exists()
    assert not any(path.name.startswith((".reporting.finalize-staging-", ".reporting.finalize-backup-")) for path in root.iterdir())
    before = {
        path.relative_to(root).as_posix(): (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    assert finalizer.finalize(preflight, lifecycle_status="complete") == receipt
    after = {
        path.relative_to(root).as_posix(): (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after


@pytest.mark.parametrize("failpoint", ("before_mkdir", "write_report", "after_staging_verify", "after_swap"))
def test_report_finalizer_process_death_boundaries_are_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    root = tmp_path / "run"
    preflight = _report_preflight(root, "RUN-REPORT-FAILPOINT")
    finalizer = RunReportFinalizer(root)

    class SimulatedProcessDeath(BaseException):
        pass

    reporting_module = __import__("auto_foundry_core.reporting", fromlist=["os"])
    fired = {"value": False}
    if failpoint == "before_mkdir":
        original_mkdir = Path.mkdir

        def fail_mkdir(path: Path, *args: object, **kwargs: object) -> object:
            if not fired["value"] and ".reporting.finalize-staging-" in path.name:
                fired["value"] = True
                raise SimulatedProcessDeath("before staging mkdir")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    elif failpoint == "write_report":
        original_atomic_write = reporting_module._atomic_write

        def fail_write(path: Path, payload: bytes) -> object:
            if not fired["value"] and ".reporting.finalize-staging-" in path.parent.name and path.name == "final_report.json":
                fired["value"] = True
                raise SimulatedProcessDeath("during report write")
            return original_atomic_write(path, payload)

        monkeypatch.setattr(reporting_module, "_atomic_write", fail_write)
    elif failpoint == "after_staging_verify":
        original_write_intent = finalizer._write_intent

        def fail_verified(intent: object) -> object:
            result = original_write_intent(intent)
            if not fired["value"] and isinstance(intent, dict) and intent.get("phase") == "prepared":
                fired["value"] = True
                raise SimulatedProcessDeath("after staging verification")
            return result

        monkeypatch.setattr(finalizer, "_write_intent", fail_verified)
    else:
        original_replace = reporting_module.os.replace

        def fail_swap(source: object, destination: object) -> object:
            result = original_replace(source, destination)
            if not fired["value"] and Path(destination).name == "reporting":
                fired["value"] = True
                raise SimulatedProcessDeath("after report swap")
            return result

        monkeypatch.setattr(reporting_module.os, "replace", fail_swap)

    with pytest.raises(SimulatedProcessDeath):
        finalizer.finalize(preflight)
    assert fired["value"] is True
    # A fresh invocation converges the owned intent/staging boundary and then
    # publishes the exact same bytes.  A second retry must not rewrite any
    # report file or change its mode/mtime.
    first = finalizer.finalize(preflight)
    snapshot_before = {
        path.relative_to(root).as_posix(): (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    assert finalizer.finalize(preflight) == first
    snapshot_after = {
        path.relative_to(root).as_posix(): (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    assert snapshot_before == snapshot_after
    assert not (root / ".reporting.finalize.intent.json").exists()
    assert not any(path.name.startswith((".reporting.finalize-staging-", ".reporting.finalize-backup-")) for path in root.iterdir())
