from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from apps.control_center_operational.projection import OperationalRepository, _data_revision_projection
from auto_foundry_core.analytical_artifacts import DataProfileArtifact
from auto_foundry_core.data_revisions import DataRevisionStore
from auto_foundry_core.entity_resolution import IdentityDomainReservation, IdentityDomainRequest, ResolutionCapacity
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.mission_context import ContextItem, MissionContext, MissionPlan, SourceBinding
from auto_foundry_core.product_review import ProductCandidate, ProductReview, hash_artifact
from auto_foundry_core.workspace import RunContext


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _state_hash(value: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _run(root: Path, run_id: str = "RUN-SIDECAR") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "run_state.json").write_text(
        json.dumps({
            "run_id": run_id,
            "run_root": str(root),
            "mode": "requirement",
            "generation": 0,
            "status": "running",
            "item_ids": ["REQ-001", "REQ-002"],
        }),
        encoding="utf-8",
    )
    return root


def _repository(run_root: Path) -> tuple[OperationalRepository, str]:
    repository = OperationalRepository(None, [run_root.parent])
    return repository, repository.list_runs()[0]["id"]


def _write_mission_sidecars(run_root: Path) -> None:
    artifact_root = run_root / "control_center" / "launches" / "D-SIDECAR"
    artifact_root.mkdir(parents=True)
    context = MissionContext(
        "hybrid",
        source_context=(ContextItem("A bounded business brief", (SourceBinding("INPUT-001"),)),),
    )
    plan = MissionPlan(context, ("REQ-001", "REQ-002"))
    (artifact_root / "mission_context.json").write_bytes(_canonical({
        "kind": "mission_context",
        "draftId": "D-SIDECAR",
        "contextHash": context.context_hash,
        "context": context.to_dict(),
    }))
    (artifact_root / "mission_plan.json").write_bytes(_canonical({
        "kind": "mission_plan",
        "draftId": "D-SIDECAR",
        "contextHash": context.context_hash,
        "planHash": plan.plan_hash,
        "missionPlan": plan.to_dict(),
    }))
    pointer = {
        "schemaVersion": 1,
        "kind": "active_mission_context_pointer",
        "runId": "RUN-SIDECAR",
        "runRoot": str(run_root),
        "draftId": "D-SIDECAR",
        "missionContextRef": "control_center/launches/D-SIDECAR/mission_context.json",
        "missionContextHash": context.context_hash,
        "missionPlanRef": "control_center/launches/D-SIDECAR/mission_plan.json",
        "missionPlanHash": plan.plan_hash,
        "documentCatalogRef": None,
        "documentCatalogHash": None,
    }
    control_root = run_root / "control_center"
    control_root.mkdir(exist_ok=True)
    (control_root / "mission_context_active.json").write_bytes(_canonical(pointer))


def _write_role_registry(run_root: Path) -> None:
    control_root = run_root / "control_plane"
    control_root.mkdir(parents=True, exist_ok=True)
    at = "2026-08-30T00:00:00Z"
    owner = "analytical_owner:REQ-001"
    entry = {
        "logical_owner": owner,
        "role": "analytical_owner",
        "subject_id": "REQ-001",
        "run_id": "RUN-SIDECAR",
        "generation_id": "G-0001",
        "session_id": "session-1",
        "status": "active",
        "replacement_required": False,
        "stale_reason": None,
        "created_at": at,
        "updated_at": at,
        "last_action": "analyze_requirement",
        "last_idempotency_key": "inv-2",
        "replacement_of": None,
        "reservation_token": None,
        "reservation_status": None,
        "reservation_action": None,
        "reservation_owner_id": None,
        "reservation_pid": None,
        "reservation_process_start": None,
        "action_lineage": [
            {"action": "analyze_requirement", "subject_id": "REQ-001", "idempotency_key": "inv-1", "at": at},
            {"action": "resume_requirement_analysis", "subject_id": "REQ-001", "idempotency_key": "inv-2", "at": at},
        ],
        "audit": [
            {"event": "session_created", "at": at, "action": "analyze_requirement", "subject_id": "REQ-001", "idempotency_key": "inv-1", "reason": None, "session_id": "session-1"},
        ],
    }
    (control_root / "role_sessions.json").write_bytes(_canonical({
        "schema_version": 2,
        "kind": "role_session_registry",
        "run_id": "RUN-SIDECAR",
        "sessions": {owner: entry},
    }))


def _write_identity_state(run_root: Path) -> None:
    entity_root = run_root / "entity_resolution"
    (entity_root / "domains").mkdir(parents=True)
    (entity_root / "committed").mkdir(parents=True)
    requests = (
        IdentityDomainRequest("REQ-001", "customer", owner_ref="ao-1").to_dict(),
        IdentityDomainRequest("REQ-002", "customer", owner_ref="ao-2").to_dict(),
    )
    domain = IdentityDomainReservation(
        domain_id="customer-domain",
        canonical_identity="customer",
        object_type="customer",
        discovered_by_item_id="REQ-001",
        rationale="shared identity",
        state="review_pending",
        resolution_owner="identity-owner-1",
        reviewer_ref="reviewer-1",
        review_verdict="accept",
        requested_by=("REQ-001", "REQ-002"),
        requests=requests,
        revision=1,
        published_revision=0,
    )
    state: dict[str, object] = {
        "schema_version": "auto_foundry.entity_resolution.state.v2",
        "run_id": "RUN-SIDECAR",
        "capacity": ResolutionCapacity().to_dict(),
        "leases": [],
        "domains": {domain.domain_id: domain.to_dict()},
        "waits": {},
        "updated_at": "2026-08-30T00:00:00+00:00",
    }
    state["state_hash"] = _state_hash(state)
    (entity_root / "state.json").write_bytes(_canonical(state))


def test_mission_pointer_missing_tampered_and_symlink_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = _run(Path(directory) / "RUN-SIDECAR")
        repository, run_id = _repository(run_root)
        missing = repository.snapshot(run_id)["missionContext"]
        assert missing["available"] is False
        _write_mission_sidecars(run_root)
        valid = repository.snapshot(run_id)["missionContext"]
        assert valid["available"] is True
        assert valid["requirementIds"] == ["REQ-001", "REQ-002"]
        assert "A bounded business brief" not in json.dumps(valid)
        pointer = run_root / "control_center" / "mission_context_active.json"
        pointer.write_text(pointer.read_text(encoding="utf-8").replace("D-SIDECAR", "D-TAMPER"), encoding="utf-8")
        assert repository.snapshot(run_id)["missionContext"]["available"] is False
        pointer.unlink()
        outside = Path(directory) / "outside-pointer.json"
        outside.write_text("{}", encoding="utf-8")
        pointer.symlink_to(outside)
        assert repository.snapshot(run_id)["missionContext"]["available"] is False


def test_two_invocations_are_distinct_under_one_logical_role_session() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = _run(Path(directory) / "RUN-SIDECAR")
        _write_role_registry(run_root)
        repository, run_id = _repository(run_root)
        snapshot = repository.snapshot(run_id)
        assert len(snapshot["roleSessions"]) == 1
        assert {item["invocationId"] for item in snapshot["invocations"]} == {"inv-1", "inv-2"}
        invokes = [edge for edge in snapshot["edges"] if edge["kind"] == "invokes"]
        assert len(invokes) == 2
        assert len({edge["target"] for edge in invokes}) == 2


def test_identity_domain_links_every_requester_and_reviewer() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = _run(Path(directory) / "RUN-SIDECAR")
        _write_identity_state(run_root)
        repository, run_id = _repository(run_root)
        snapshot = repository.snapshot(run_id)
        assert len(snapshot["identityDomains"]) == 1
        domain = snapshot["identityDomains"][0]
        assert domain["requestedBy"] == ["REQ-001", "REQ-002"]
        assert domain["reviewerRef"] == "reviewer-1"
        edges = {(edge["source"], edge["target"], edge["kind"]) for edge in snapshot["edges"]}
        assert ("REQ-001", domain["id"], "requests") in edges
        assert ("REQ-002", domain["id"], "requests") in edges
        assert ("reviewer:reviewer-1", domain["id"], "reviews") in edges


def test_data_revision_pointer_and_transaction_invalidate_projection_cache() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = _run(Path(directory) / "RUN-SIDECAR")
        inputs = run_root / "inputs"
        inputs.mkdir(parents=True)
        archive = inputs / "data_room.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("orders.csv", "id,value\n1,2\n")
        revision_store = DataRevisionStore(RunContext("RUN-SIDECAR", run_root, input_roots=(inputs,)))
        revision = revision_store.initialize_legacy("data_room.zip")
        repository, run_id = _repository(run_root)
        first = repository.snapshot(run_id)["dataRevisions"]
        assert first["current"]["pointerHash"]
        manifest_path = run_root / "data_room" / "revisions" / "D-0001" / "revision_manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        archive_bytes = archive.read_bytes()
        archive_stat = archive.stat()
        manifest_path.write_bytes(b"tampered manifest\n")
        assert "current" not in repository.snapshot(run_id)["dataRevisions"]
        manifest_path.write_bytes(manifest_bytes)
        archive.write_bytes(archive_bytes + b"tampered archive\n")
        assert "current" not in repository.snapshot(run_id)["dataRevisions"]
        archive.write_bytes(archive_bytes)
        # The canonical D-0001 manifest binds source archive stat metadata as
        # well as its digest.  Restore the original timestamps so the test
        # proves that a byte-identical archive is accepted again rather than
        # relying on a stale projection cache.
        os.utime(archive, ns=(archive_stat.st_atime_ns, archive_stat.st_mtime_ns))
        assert repository.snapshot(run_id)["dataRevisions"]["current"]["revisionId"] == "D-0001"
        transaction = revision_store.begin_revision_transaction(
            revision,
            parent_revision_id=None,
            parent_manifest_hash=None,
            launch_draft_id="D",
            launch_fingerprint="c" * 64,
            created_at="2026-08-30T00:00:00Z",
        )
        second = repository.snapshot(run_id)["dataRevisions"]
        assert second["pendingRevision"]["transactionRef"] == "data_room/revision_transaction.json"
        assert second["pendingRevision"]["transactionHash"] == transaction.transaction_hash
        tx_path = run_root / "data_room" / "revision_transaction.json"
        tx_path.write_bytes(b"tampered transaction\n")
        assert repository.snapshot(run_id)["dataRevisions"]["state"] == "recovery"


def test_data_revision_projection_is_not_reverified_on_unchanged_ui_poll(monkeypatch) -> None:
    import apps.control_center_operational.projection as projection_module

    with tempfile.TemporaryDirectory() as directory:
        run_root = _run(Path(directory) / "RUN-SIDECAR")
        inputs = run_root / "inputs"
        inputs.mkdir(parents=True)
        archive = inputs / "data_room.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("orders.csv", "id,value\n1,2\n")
        DataRevisionStore(RunContext("RUN-SIDECAR", run_root, input_roots=(inputs,))).initialize_legacy(
            "data_room.zip"
        )
        repository = OperationalRepository(None, [run_root.parent])
        original = projection_module._data_revision_projection
        calls = 0

        def counted(root: Path):
            nonlocal calls
            calls += 1
            return original(root)

        monkeypatch.setattr(projection_module, "_data_revision_projection", counted)
        repository.list_runs()
        first_poll_calls = calls
        assert first_poll_calls >= 1
        for _ in range(5):
            repository.list_runs()
        assert calls == first_poll_calls

        # A real archive mutation changes file identity and forces one fresh
        # strict verification instead of returning stale UI state.
        archive.write_bytes(archive.read_bytes() + b"tamper")
        repository.list_runs()
        assert calls == first_poll_calls + 1


def test_repeated_read_only_projection_keeps_entire_run_tree_byte_identical(tmp_path: Path) -> None:
    """Status/discovery validation must not persist inventory telemetry.

    The strict D-revision validator still re-hashes the archive, but a
    Control Center read must be observational.  Exercise both the direct
    projection and the repository paths used by list/status/snapshot after
    the first cache warm-up, then compare every regular file in the run root.
    """

    run_root = _run(tmp_path / "RUN-SIDECAR")
    inputs = run_root / "inputs"
    inputs.mkdir(parents=True)
    archive = inputs / "data_room.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("orders.csv", "id,value\n1,2\n")
    context = RunContext("RUN-SIDECAR", run_root, input_roots=(inputs,))
    DataRevisionStore(context).initialize_legacy("data_room.zip")
    repository = OperationalRepository(None, [run_root.parent])
    run_id = repository.list_runs()[0]["id"]

    def tree_hashes() -> dict[str, str]:
        return {
            path.relative_to(run_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in run_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    before = tree_hashes()
    for _ in range(3):
        # Bypass the projection cache once per pass as well as exercising the
        # public repository methods that status/list/snapshot use.
        assert _data_revision_projection(run_root)["current"]["revisionId"] == "D-0001"
        assert repository.list_runs()[0]["id"] == run_id
        assert repository.snapshot(run_id)["dataRevisions"]["current"]["revisionId"] == "D-0001"
    assert tree_hashes() == before


def _write_valid_product(run_root: Path, analytical_artifacts: tuple[DataProfileArtifact, ...] = ()) -> Path:
    context = RunContext("RUN-SIDECAR", run_root)
    # Replace the lightweight discovery state with a lifecycle-authoritative
    # state for the strict product inspector.
    (run_root / "run_state.json").unlink()
    lifecycle = RunLifecycle.create(context, ["REQ-001", "REQ-002"], mode="requirement")
    plan_path = lifecycle.plan_path
    plan_path.write_bytes(b"{}\n")
    products = run_root / "products"
    products.mkdir(parents=True)
    provenance: list[dict[str, object]] = []
    for artifact in analytical_artifacts:
        artifact_ref = f"integration/committed/artifacts/{artifact.artifact_id}.json"
        committed = run_root / "requirements" / artifact.requirement_id / artifact_ref
        committed.parent.mkdir(parents=True, exist_ok=True)
        committed.write_bytes(artifact.to_json().encode("utf-8"))
        provenance.append(
            {
                "item_id": artifact.requirement_id,
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "schema_version": artifact.schema_version,
                "requirement_id": artifact.requirement_id,
                "content_hash": artifact.content_hash,
                "envelope_hash": artifact.envelope_hash,
                "canonical_bytes_sha256": hashlib.sha256(artifact.to_json().encode("utf-8")).hexdigest(),
                "artifact_ref": artifact_ref,
                "integration_record_id": f"record-{artifact.artifact_id}",
                "integration_record_hash": "a" * 64,
            }
        )
    fixture = products / "fixture.json"; fixture.write_bytes(_canonical({"analytical_artifacts": provenance}))
    chart_map = products / "chart_map.json"; chart_map.write_bytes(b"{}\n")
    chart_registry = products / "chart_registry.json"; chart_registry.write_bytes(b"{}\n")
    blueprint = products / "dashboard_blueprint_v2.json"
    blueprint.write_bytes(_canonical({
        "schema_version": "dashboard.business_presentation_plan.v2",
        "kind": "dashboard_blueprint",
        "run_id": "RUN-SIDECAR",
        "generation_id": "G-0001",
        "review_status": "Reviewed",
        "source_bindings": {
            "blueprint_ref": "products/dashboard_blueprint_v2.json",
        },
    }))
    blueprint_hash = hashlib.sha256(blueprint.read_bytes()).hexdigest()
    site = products / "site"; site.mkdir()
    (site / "assets").mkdir()
    index = site / "index.html"; index.write_bytes(b"<!doctype html><link rel=\"stylesheet\" href=\"assets/dashboard.css\">")
    stylesheet = site / "assets" / "dashboard.css"; stylesheet.write_bytes(b"body { color: #123456; }\n")
    site_manifest = site / "site_manifest.json"
    site_manifest.write_bytes(_canonical({
        "site_version": 4,
        "pages": ["index.html"],
        "assets": ["assets/dashboard.css"],
        "site_file_hashes": {
            "index.html": hashlib.sha256(index.read_bytes()).hexdigest(),
            "assets/dashboard.css": hashlib.sha256(stylesheet.read_bytes()).hexdigest(),
        },
        "blueprint_ref": "products/dashboard_blueprint_v2.json",
        "blueprint_sha256": blueprint_hash,
    }))
    receipt = {
        "status": "complete", "new_analytics": False, "run_id": "RUN-SIDECAR", "generation_id": "G-0001",
        "plan_binding": {"ref": str(plan_path.resolve().relative_to(run_root.resolve())), "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(), "admission_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(), "generation_id": "G-0001"},
        "parent": {"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None},
        "outputs": {"receipt_ref": "products/receipt.json", "fixture_ref": "products/fixture.json", "chart_map_ref": "products/chart_map.json", "chart_registry_ref": "products/chart_registry.json", "blueprint_ref": "products/dashboard_blueprint_v2.json", "site_ref": "products/site"},
        "output_hashes": {"fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(), "chart_map_sha256": hashlib.sha256(chart_map.read_bytes()).hexdigest(), "chart_registry_sha256": hashlib.sha256(chart_registry.read_bytes()).hexdigest(), "blueprint_sha256": blueprint_hash, "site_manifest_sha256": hashlib.sha256((site / "site_manifest.json").read_bytes()).hexdigest()},
        "blueprint_binding": {
            "ref": "products/dashboard_blueprint_v2.json",
            "sha256": blueprint_hash,
            "schema_version": "dashboard.business_presentation_plan.v2",
            "status": "Reviewed",
        },
        "analytical_artifacts": provenance,
    }
    receipt_path = products / "receipt.json"; receipt_path.write_bytes(_canonical(receipt))
    manifest = {
        "freeze_markers": {"answers_frozen": True, "living_enterprise_model_frozen": True, "prepared_data_registry_frozen": True, "dashboard_frozen": True, "telemetry_frozen": True},
        "run_id": "RUN-SIDECAR", "terminal": True, "new_analytics": False, "lifecycle": {"generation_id": "G-0001"},
        "dashboard": {"receipt_ref": "products/receipt.json", "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()},
        "lineage": {},
    }
    manifest_path = products / "product_manifest.json"; manifest_path.write_bytes(_canonical(manifest))
    # The operational projection now exposes a final dashboard only through
    # the generation-scoped candidate + independent accepted review boundary.
    generation_root = products / "generations" / "G-0001"
    generation_root.mkdir(parents=True)
    artifact_paths = {
        "manifest": manifest_path,
        "fixture": fixture,
        "chart_map": chart_map,
        "chart_registry": chart_registry,
        "blueprint": blueprint,
        "site": site,
        "receipt": receipt_path,
    }
    artifact_bindings: dict[str, dict[str, object]] = {}
    for name, path in artifact_paths.items():
        relative = path.resolve().relative_to(run_root.resolve()).as_posix()
        if path.is_dir():
            files = {
                child.relative_to(path).as_posix(): hashlib.sha256(child.read_bytes()).hexdigest()
                for child in sorted(path.rglob("*"))
                if child.is_file()
            }
            kind, digest = hash_artifact(path)
            artifact_bindings[name] = {
                "ref": relative,
                "kind": kind,
                "sha256": digest,
                "files": files,
            }
        else:
            artifact_bindings[name] = {
                "ref": relative,
                "kind": "file",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    candidate = ProductCandidate(
        run_id="RUN-SIDECAR",
        generation_id="G-0001",
        product_owner="product-owner",
        parent_lineage={"root_generation": True, "parent_generation_id": None, "parent_manifest_ref": None, "parent_manifest_hash": None},
        plan_binding={
            "plan_ref": plan_path.resolve().relative_to(run_root.resolve()).as_posix(),
            "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        publication_policy_hash="b" * 64,
        artifact_bindings=artifact_bindings,
        created_at="2026-08-30T00:00:00Z",
    )
    candidate_path = generation_root / "product_candidate.json"
    candidate_path.write_bytes(_canonical(candidate.to_dict()))
    review = ProductReview(
        run_id="RUN-SIDECAR",
        generation_id="G-0001",
        candidate_ref="products/generations/G-0001/product_candidate.json",
        candidate_hash=candidate.computed_hash,
        product_owner="product-owner",
        reviewer_ref="independent-reviewer",
        verdict="accept",
        reviewed_at="2026-08-30T00:01:00Z",
    )
    (generation_root / "product_review.json").write_bytes(_canonical(review.to_dict()))
    return manifest_path


def test_validated_product_links_expose_only_hash_bound_outputs_and_tampering_is_hidden() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = _run(Path(directory) / "RUN-SIDECAR")
        _write_valid_product(run_root)
        repository, run_id = _repository(run_root)
        product = repository.snapshot(run_id)["productDashboard"]
        assert product["valid"] is True
        assert product["dashboardUrl"] == f"/api/product/dashboard/{run_id}/index.html"
        assert str(run_root) not in json.dumps(product)
        assert "fixture.json" in product["refs"]["fixture_ref"]
        (run_root / "products" / "receipt.json").write_bytes(b"tampered\n")
        product = repository.snapshot(run_id)["productDashboard"]
        assert product["valid"] is False
        assert product["refs"] == {}


def test_product_projection_consumes_typed_analytical_artifact_provenance() -> None:
    artifact = DataProfileArtifact(
        artifact_id="artifact-profile",
        requirement_id="REQ-001",
        dataset_fingerprint="b" * 64,
        source_refs=("data_room.zip",),
        population={"rows": 2},
        grain="row",
        period="2026",
        method="profile",
        profile={"columns": [{"name": "value", "dtype": "integer"}]},
        created_at="2026-08-30T00:00:00+00:00",
    )
    with tempfile.TemporaryDirectory() as directory:
        run_root = _run(Path(directory) / "RUN-SIDECAR")
        _write_valid_product(run_root, (artifact,))
        repository, run_id = _repository(run_root)
        product = repository.snapshot(run_id)["productDashboard"]
        assert product["valid"] is True
        assert product["artifacts"] == [
            {
                "itemId": "REQ-001",
                "artifactId": "artifact-profile",
                "artifactType": "data_profile",
                "schemaVersion": "1.0",
                "requirementId": "REQ-001",
                "contentHash": artifact.content_hash,
                "envelopeHash": artifact.envelope_hash,
                "canonicalBytesSha256": hashlib.sha256(artifact.to_json().encode("utf-8")).hexdigest(),
                "artifactRef": "integration/committed/artifacts/artifact-profile.json",
                "integrationRecordId": "record-artifact-profile",
                "integrationRecordHash": "a" * 64,
            }
        ]
        assert "data_room.zip" not in json.dumps(product)
        fixture = run_root / "products" / "fixture.json"
        fixture.write_bytes(_canonical({"artifacts": [{"kind": "chart", "id": "legacy"}]}))
        assert repository.snapshot(run_id)["productDashboard"]["valid"] is False
