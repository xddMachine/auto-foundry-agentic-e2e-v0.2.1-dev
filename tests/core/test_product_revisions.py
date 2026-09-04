from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from dataclasses import replace

import pytest

from auto_foundry_core import (
    ItemWorkspace,
    ProductCandidate,
    ProductReviewStore,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.product_review import ProductRevisionPointer, canonical_hash


def _seed_store(tmp_path: Path, *, verdict: str = "accept") -> tuple[RunContext, ProductReviewStore, ProductCandidate]:
    context = RunContext("RUN-PRODUCT-REVISIONS", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-001",), mode="requirement")
    plan = context.run_root / "requirement_supervisor_plan.json"
    plan.write_text('{"schema_version":1}\n', encoding="utf-8")
    product_root = context.product_root / "generations" / "G-0001"
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
    site.mkdir()
    (site / "index.html").write_text("<main>revision fixture</main>\n", encoding="utf-8")
    outputs["site"] = site
    policy_hash = canonical_hash({"enabled": False})
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
            "plan_hash": hashlib.sha256(plan.read_bytes()).hexdigest(),
        },
        publication_policy_hash=policy_hash,
        artifact_bindings={
            name: {"ref": str(path.relative_to(context.run_root))}
            for name, path in outputs.items()
        },
    )
    store = ProductReviewStore(context, "G-0001")
    store.record_candidate(candidate)
    store.record_review(reviewer_ref="product-reviewer", verdict=verdict, reviewed_at="2026-01-01T00:00:00Z")
    return context, store, candidate


def _materialize_revision_candidate(
    store: ProductReviewStore,
    revision_id: str,
    source: ProductCandidate,
) -> ProductCandidate:
    """Copy reviewed fixture bytes into the revision-owned output bundle."""

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


def test_legacy_root_is_adopted_once_and_revision_activation_is_cas_bound(tmp_path: Path) -> None:
    context, store, candidate = _seed_store(tmp_path)
    canonical_candidate = store.load_candidate()
    root_candidate_bytes = store.candidate_path.read_bytes()
    root_review_bytes = store.review_path.read_bytes()

    pointer = store.load_active_revision()
    assert isinstance(pointer, ProductRevisionPointer)
    assert pointer.revision_id == "rev-0001"
    assert pointer.status == "accepted"
    assert store.candidate_path.read_bytes() == root_candidate_bytes
    assert store.review_path.read_bytes() == root_review_bytes
    assert store.load_candidate().computed_hash == canonical_candidate.computed_hash

    revision = store.begin_revision(
        request_id="regen-1",
        input_fingerprint="a" * 64,
        implementation_identity="b" * 64,
    )
    assert revision.revision_id == "rev-0002"
    assert revision.prior_revision_id == "rev-0001"
    assert store.begin_revision(
        request_id="regen-1",
        input_fingerprint="a" * 64,
        implementation_identity="b" * 64,
    ).revision_id == revision.revision_id
    revision_candidate = _materialize_revision_candidate(store, revision.revision_id, candidate)
    persisted_revision_candidate = store.record_candidate(revision_candidate, revision_id=revision.revision_id)
    review = store.record_review(
        reviewer_ref="product-reviewer",
        verdict="accept_with_limits",
        candidate_hash=persisted_revision_candidate.computed_hash,
        reviewed_at="2026-01-01T00:00:01Z",
        revision_id=revision.revision_id,
    )
    assert review.review_hash
    activated = store.activate_revision(revision.revision_id)
    assert activated.revision_id == revision.revision_id
    assert store.load_active_revision().revision_id == revision.revision_id  # type: ignore[union-attr]
    assert store.load_revision("rev-0001").status == "superseded"
    assert store.load_candidate().computed_hash == persisted_revision_candidate.computed_hash
    assert context.resolve_run_path(activated.candidate_ref).is_file()


def test_repair_review_is_retained_as_current_until_new_revision_accepts(tmp_path: Path) -> None:
    _context, store, candidate = _seed_store(tmp_path, verdict="repair_once")
    pointer = store.load_active_revision()
    assert pointer is not None and pointer.status == "reviewed"
    revision = store.begin_revision(
        request_id="regen-repair",
        input_fingerprint="c" * 64,
        implementation_identity="d" * 64,
    )
    assert revision.prior_revision_id == pointer.revision_id
    revision_candidate = _materialize_revision_candidate(store, revision.revision_id, candidate)
    persisted_revision_candidate = store.record_candidate(revision_candidate, revision_id=revision.revision_id)
    store.record_review(
        reviewer_ref="product-reviewer-2",
        verdict="accept",
        candidate_hash=persisted_revision_candidate.computed_hash,
        reviewed_at="2026-01-01T00:00:01Z",
        revision_id=revision.revision_id,
    )
    assert store.activate_revision(revision.revision_id).status == "accepted"


def test_incomplete_revision_namespace_fails_closed(tmp_path: Path) -> None:
    _context, store, _candidate = _seed_store(tmp_path)
    store.load_active_revision()
    orphan = store.revisions_root / "rev-0003"
    orphan.mkdir()
    with pytest.raises(ValueError, match="incomplete revision"):
        store.begin_revision(
            request_id="regen-orphan",
            input_fingerprint="e" * 64,
            implementation_identity="f" * 64,
        )


def test_pending_revision_retries_are_same_key_only_and_terminal_is_immutable(tmp_path: Path) -> None:
    """A different request cannot allocate rev-N+1 beside an open target."""

    _context, store, _candidate = _seed_store(tmp_path)
    pointer = store.load_active_revision()
    assert pointer is not None
    pending = store.begin_revision(
        request_id="orphan-request",
        input_fingerprint="1" * 64,
        implementation_identity="2" * 64,
    )
    assert store.begin_revision(
        request_id="orphan-request",
        input_fingerprint="1" * 64,
        implementation_identity="2" * 64,
    ).revision_id == pending.revision_id
    with pytest.raises(ValueError, match="still in progress"):
        store.begin_revision(
            request_id="different-request",
            input_fingerprint="3" * 64,
            implementation_identity="4" * 64,
        )
    failed = store.fail_revision(pending.revision_id)
    assert failed.status == "failed"
    with pytest.raises(ValueError, match="terminal outcome"):
        store.begin_revision(
            request_id="orphan-request",
            input_fingerprint="1" * 64,
            implementation_identity="2" * 64,
        )
    assert store.load_active_revision().revision_id == pointer.revision_id  # type: ignore[union-attr]


def _prepare_accepted_target(
    tmp_path: Path,
    *,
    failpoint: str,
) -> tuple[RunContext, ProductReviewStore, str]:
    """Create an accepted target and interrupt activation at one boundary."""

    context, store, candidate = _seed_store(tmp_path)
    pointer = store.load_active_revision()
    assert pointer is not None and pointer.revision_id == "rev-0001"
    revision = store.begin_revision(
        request_id=f"regen-{failpoint}",
        input_fingerprint="1" * 64,
        implementation_identity="2" * 64,
    )
    target = _materialize_revision_candidate(store, revision.revision_id, candidate)
    persisted = store.record_candidate(target, revision_id=revision.revision_id)
    store.record_review(
        reviewer_ref="product-reviewer",
        verdict="accept",
        candidate_hash=persisted.computed_hash,
        reviewed_at="2026-01-01T00:00:01Z",
        revision_id=revision.revision_id,
    )

    def interrupt(name: str) -> None:
        if name == failpoint:
            raise KeyboardInterrupt(f"fault at {name}")

    interrupted = ProductReviewStore(context, "G-0001", failpoint=interrupt)
    with pytest.raises(KeyboardInterrupt):
        interrupted.activate_revision(revision.revision_id)
    return context, store, revision.revision_id


def test_activation_pointer_write_interruption_reconciles_after_restart(tmp_path: Path) -> None:
    context, store, revision_id = _prepare_accepted_target(
        tmp_path,
        failpoint="before_pointer_write",
    )
    # The prior accepted pointer remains authoritative while the target is
    # durably marked activation_pending.  A fresh store instance models a
    # process restart and must complete the transition without copying or
    # rewriting the prior evidence.
    assert store.load_active_revision().revision_id == "rev-0001"  # type: ignore[union-attr]
    assert store.load_revision(revision_id).status == "activation_pending"
    restarted = ProductReviewStore(context, "G-0001")
    pointer = restarted.reconcile_revision_activation(revision_id)
    assert pointer.revision_id == revision_id
    assert pointer.status == "accepted"
    assert restarted.load_revision(revision_id).status == "accepted"


def test_activation_after_pointer_write_interruption_reconciles_after_restart(tmp_path: Path) -> None:
    context, store, revision_id = _prepare_accepted_target(
        tmp_path,
        failpoint="after_pointer_write_before_revision_accepted",
    )
    # The pointer CAS is durable, but the target state is still transitional;
    # read/load accepts only this typed pair and replay finishes it.
    pointer = store.load_active_revision()
    assert pointer is not None and pointer.revision_id == revision_id
    assert store.load_revision(revision_id).status == "activation_pending"
    restarted = ProductReviewStore(context, "G-0001")
    reconciled = restarted.reconcile_revision_activation(revision_id)
    assert reconciled.revision_id == revision_id
    assert restarted.load_revision(revision_id).status == "accepted"
