from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

import auto_foundry_core.analysis as analysis_module
from auto_foundry_core import (
    BoundAnalysisContext,
    DataAssetRef,
    EntityResolutionWorkspace,
    ItemWorkspace,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementRunExtension,
    RequirementSupervisorWorkspace,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.data_revisions import DataRevisionStore


def _archive(path: Path, marker: str) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("rows.csv", f"id,value\n1,{marker}\n")
    return path


def _record(item_id: str, text: str | None = None) -> RequirementRecord:
    return RequirementRecord(
        requirement_id=item_id,
        original_text=text or f"Investigate {item_id}.",
        business_objective=f"Support {item_id}.",
    )


def _proposal(item: ItemWorkspace, domain_id: str = "shared-domain") -> None:
    owner_ref = f"ao-{item.item_id}"
    item.bind_analysis_owner(owner_ref)
    item.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": domain_id,
            "object_type": "business_object",
            "rationale": "The owner found multiple representations that need one reviewed identity.",
            "source_hints": ["rows.csv"],
            "representation_item_ids": ["source-a", "source-b"],
            "item_id": item.item_id,
            "owner_ref": owner_ref,
        }
    )


def _setup(tmp_path: Path) -> tuple[RunContext, RunLifecycle, DataRevisionStore, Path, Path, RequirementExecutionPlan]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    first_archive = _archive(inputs / "first.zip", "first")
    second_archive = _archive(inputs / "second.zip", "second")
    context = RunContext("RUN-ACTIVE-DATA-BINDING", tmp_path / "run", (inputs,))
    lifecycle = RunLifecycle.create(context, ("REQ-1", "REQ-2"), mode="requirement")
    first = ItemWorkspace.create(context, "REQ-1", mode="requirement", original_text="first")
    second = ItemWorkspace.create(context, "REQ-2", mode="requirement", original_text="second")
    _proposal(first)
    _proposal(second)
    plan = RequirementExecutionPlan(
        input_records=(_record("REQ-1"), _record("REQ-2")),
        groups=(RequirementExecutionGroup(("REQ-1", "REQ-2"), "One bounded route."),),
        planner_ref="planner",
        portfolio_strategy="strategy",
        revision=1,
    )
    RequirementSupervisorWorkspace(context).save(plan)
    state = lifecycle.to_dict()
    state["status"] = "complete"
    lifecycle._write_state(state)  # noqa: SLF001 - deterministic fixture barrier
    store = DataRevisionStore(context)
    store.initialize_legacy(first_archive)
    return context, lifecycle, store, first_archive, second_archive, plan


def test_g1_and_pending_d2_bind_ao_and_entity_resolution_to_d1(tmp_path: Path) -> None:
    context, lifecycle, store, first_archive, second_archive, _plan = _setup(tmp_path)
    first_revision = store.load("D-0001")

    # The first Requirement Mode context and first resolver admission both use
    # the immutable bootstrap revision.
    bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(first_archive),
        ItemWorkspace.load(context, "REQ-1", mode="requirement"),
        lifecycle,
    )
    assert bound.source_identity.content_hash == first_revision.archive_sha256
    assert bound.source_catalog.path == first_revision.catalog_path
    resolver = EntityResolutionWorkspace.create(context)
    reservation = resolver.reserve_identity_domain(
        "shared-domain",
        "business_object",
        "REQ-1",
        "The owner found multiple representations that need one reviewed identity.",
        source_hints=("rows.csv",),
        representation_item_ids=("source-a", "source-b"),
    )
    assert reservation.request_records[0]["generation_id"] == "G-0001"
    assert reservation.request_records[0]["data_revision_id"] == "D-0001"

    # Appending D2 moves only the mutable upload pointer.  Until a generation
    # safely admits it, a second owner context and resolver request remain on D1.
    store.append(second_archive, expected_current_revision_id="D-0001")
    stale_archive_bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(second_archive),
        ItemWorkspace.load(context, "REQ-2", mode="requirement"),
        RunLifecycle.load(context),
    )
    assert stale_archive_bound.source_identity.content_hash == first_revision.archive_sha256
    assert stale_archive_bound.source_catalog.path == first_revision.catalog_path
    pending_reservation = resolver.reserve_identity_domain(
        "shared-domain",
        "business_object",
        "REQ-2",
        "The owner found multiple representations that need one reviewed identity.",
        source_hints=("rows.csv",),
        representation_item_ids=("source-a", "source-b"),
    )
    assert pending_reservation.request_records[-1]["data_revision_id"] == "D-0001"


def test_bound_requirement_context_accepts_catalog_from_older_core_patch(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archive = _archive(inputs / "legacy.zip", "legacy")
    old_context = RunContext(
        "RUN-OLDER-CATALOG-CONTEXT",
        tmp_path / "run",
        (inputs,),
        core_version="0.8.0",
        skill_version="old-skill",
    )
    lifecycle = RunLifecycle.create(old_context, ("REQ-004",), mode="requirement")
    ItemWorkspace.create(old_context, "REQ-004", mode="requirement", original_text="Reuse the catalog.")
    revision = DataRevisionStore(old_context).initialize_legacy(archive)

    current_context = RunContext(
        old_context.run_id,
        old_context.run_root,
        old_context.input_roots,
        core_version="0.8.1",
        skill_version="new-skill",
    )
    current_item = ItemWorkspace.load(current_context, "REQ-004", mode="requirement")
    bound = BoundAnalysisContext.create_for_requirement(
        current_context,
        DataAssetRef.from_path(archive),
        current_item,
        RunLifecycle.load(current_context),
    )
    loaded = BoundAnalysisContext.load(
        current_context,
        path=bound.manifest_path,
        item_workspace=current_item,
    )

    assert bound.source_catalog.path == revision.catalog_path
    assert bound.source_catalog.content_hash == revision.catalog_sha256
    assert loaded.source_catalog.path == revision.catalog_path


def test_bound_requirement_context_rejects_catalog_mutation_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, lifecycle, store, archive, _second_archive, _plan = _setup(tmp_path)
    revision = store.load("D-0001")
    original_snapshot = analysis_module._semantic_reuse_snapshot

    def mutate_after_revision_resolution(*args: object, **kwargs: object) -> object:
        result = original_snapshot(*args, **kwargs)
        revision.catalog_path.write_bytes(revision.catalog_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(analysis_module, "_semantic_reuse_snapshot", mutate_after_revision_resolution)
    with pytest.raises(ValueError, match="catalog content hash does not match data revision"):
        BoundAnalysisContext.create_for_requirement(
            context,
            DataAssetRef.from_path(archive),
            ItemWorkspace.load(context, "REQ-1", mode="requirement"),
            lifecycle,
        )


def test_bound_requirement_context_rejects_catalog_symlink_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, lifecycle, store, archive, _second_archive, _plan = _setup(tmp_path)
    revision = store.load("D-0001")
    original_snapshot = analysis_module._semantic_reuse_snapshot

    def swap_after_revision_resolution(*args: object, **kwargs: object) -> object:
        result = original_snapshot(*args, **kwargs)
        replacement = revision.catalog_path.with_name("catalog-identical-copy.json")
        replacement.write_bytes(revision.catalog_path.read_bytes())
        revision.catalog_path.unlink()
        revision.catalog_path.symlink_to(replacement)
        return result

    monkeypatch.setattr(analysis_module, "_semantic_reuse_snapshot", swap_after_revision_resolution)
    with pytest.raises(ValueError, match="symlink|regular file"):
        BoundAnalysisContext.create_for_requirement(
            context,
            DataAssetRef.from_path(archive),
            ItemWorkspace.load(context, "REQ-1", mode="requirement"),
            lifecycle,
        )


def test_refreshed_g2_binds_d2_and_later_g3_inherits_applied_d2(tmp_path: Path) -> None:
    context, _lifecycle, store, first_archive, second_archive, plan = _setup(tmp_path)
    first_revision = store.load("D-0001")
    second_revision = store.append(second_archive, expected_current_revision_id=first_revision.revision_id)
    parent = RunLifecycle.load(context)
    pending = store.admit_pending_data_refresh(
        data_revision=second_revision,
        plan=plan.to_dict(),
        reopened_item_ids=("REQ-1",),
        expected_parent_generation_id=parent.generation_id,
        expected_parent_state_hash=parent.snapshot.manifest_hash,
        expected_parent_plan_hash=hashlib.sha256(parent.plan_path.read_bytes()).hexdigest(),
        launch_draft_id="DRAFT-ACTIVE-DATA",
        launch_fingerprint="a" * 64,
        created_at="2026-08-26T00:00:00Z",
    )
    extension = RequirementRunExtension.refresh_data(
        context,
        plan,
        data_revision=second_revision,
        reopened_item_ids=("REQ-1",),
    )
    store.mark_pending_data_refresh_applied(pending.intent_hash, generation_id=extension.generation_id)
    active = RunLifecycle.load(context)
    assert active.generation_id == "G-0002"

    reopened = ItemWorkspace.load(context, "REQ-1", mode="requirement")
    _proposal(reopened)
    bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(first_archive),  # stale caller input is ignored
        reopened,
        active,
    )
    assert bound.source_identity.content_hash == second_revision.archive_sha256
    assert bound.source_catalog.path == second_revision.catalog_path

    resolver = EntityResolutionWorkspace.create(context)
    reservation = resolver.reserve_identity_domain(
        "shared-domain",
        "business_object",
        "REQ-1",
        "The owner found multiple representations that need one reviewed identity.",
        source_hints=("rows.csv",),
        representation_item_ids=("source-a", "source-b"),
    )
    assert reservation.request_records[-1]["generation_id"] == "G-0002"
    assert reservation.request_records[-1]["data_revision_id"] == "D-0002"

    # A normal later append has no direct D metadata.  The applied receipt for
    # G2 is the resolver's lineage and keeps AO/ER on D2.
    extension3 = RequirementRunExtension.append(context, _record("REQ-3"))
    active3 = RunLifecycle.load(context)
    assert extension3.generation_id == "G-0003"
    assert active3.generation_metadata.data_revision_ref is None
    third = ItemWorkspace.load(context, "REQ-3", mode="requirement")
    bound3 = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(first_archive),
        third,
        active3,
    )
    assert bound3.source_identity.content_hash == second_revision.archive_sha256
    assert bound3.source_catalog.path == second_revision.catalog_path
