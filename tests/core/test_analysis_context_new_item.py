from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import zipfile

import pytest

import auto_foundry_core.analysis as analysis_module
from auto_foundry_core.analysis import BoundAnalysisContext, load_bound_analysis_context
from auto_foundry_core.contracts import DataAssetRef, ImplementationTransition
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.workspace import RunContext


def _fixture(
    tmp_path: Path,
    item_ids: tuple[str, ...] = ("Q-001", "Q-002"),
) -> tuple[RunContext, RunContext, ItemWorkspace, BoundAnalysisContext, RunLifecycle, Path]:
    inputs = tmp_path / "inputs"
    run = tmp_path / "run"
    inputs.mkdir()
    run.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", b"order_id,region\nA-1,DE\nA-2,FR\n")

    old = RunContext(
        "RUN-NEW-ITEM",
        run,
        (inputs,),
        core_version="0.3.1",
        skill_version="0.2.4",
    )
    source_item = ItemWorkspace.create(old, "Q-001", original_text="Summarize the fixture.")
    source_bound = BoundAnalysisContext.create(old, DataAssetRef.from_path(archive), source_item)
    lifecycle = RunLifecycle.create(old, item_ids)
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-031-032",
            old_sha="a" * 40,
            new_sha="b" * 40,
            old_tree="c" * 40,
            new_tree="d" * 40,
            old_version="skill0.2.4/core0.3.1",
            new_version="skill0.2.5/core0.3.2",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="implementation transition",
            resume_point="analysis_context_rebind",
        )
    )
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-032-034",
            old_sha="b" * 40,
            new_sha="e" * 40,
            old_tree="d" * 40,
            new_tree="f" * 40,
            old_version="skill0.2.5/core0.3.2",
            new_version="skill0.2.8/core0.3.5",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="implementation transition",
            resume_point="analysis_context_rebind",
        )
    )
    current = RunContext(
        "RUN-NEW-ITEM",
        run,
        (inputs,),
        core_version="0.3.5",
        skill_version="0.2.8",
    )
    rebound = source_bound.rebind_implementation(current, ItemWorkspace.load(current, "Q-001"), lifecycle)
    return old, current, source_item, rebound, lifecycle, archive


def _skill_only_fixture(
    tmp_path: Path,
) -> tuple[RunContext, RunContext, ItemWorkspace, BoundAnalysisContext, RunLifecycle, Path]:
    """Build a catalog whose core is unchanged while only the skill advances."""

    inputs = tmp_path / "inputs"
    run = tmp_path / "run"
    inputs.mkdir()
    run.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", b"order_id,region\nA-1,DE\n")
    old = RunContext("RUN-SKILL-ONLY", run, (inputs,), core_version="0.3.5", skill_version="0.2.4")
    source_item = ItemWorkspace.create(old, "Q-001", original_text="Summarize the fixture.")
    source_bound = BoundAnalysisContext.create(old, DataAssetRef.from_path(archive), source_item)
    lifecycle = RunLifecycle.create(old, ("Q-001", "Q-002"))
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-SKILL-024-027",
            old_sha="1" * 40,
            new_sha="2" * 40,
            old_tree="3" * 40,
            new_tree="4" * 40,
            old_version="skill0.2.4/core0.3.5",
            new_version="skill0.2.8/core0.3.5",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="skill transition",
            resume_point="analysis_context_rebind",
        )
    )
    current = RunContext("RUN-SKILL-ONLY", run, (inputs,), core_version="0.3.5", skill_version="0.2.8")
    rebound = source_bound.rebind_implementation(current, ItemWorkspace.load(current, "Q-001"), lifecycle)
    return old, current, source_item, rebound, lifecycle, archive


def _operations(run_root: Path) -> dict[str, int]:
    path = run_root / "telemetry" / "inventory_counters.json"
    return dict(json.loads(path.read_text(encoding="utf-8"))["operations"])


def _data_room_events(bound: BoundAnalysisContext) -> tuple[str, ...]:
    return tuple(event.event_type for event in bound.workbench.telemetry.events if event.event_type.startswith("data_room_"))


def test_new_item_reuses_rebound_catalog_without_reads_or_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Summarize the fixture again.")
    counters_before = _operations(current.run_root)
    events_before = _data_room_events(rebound)
    catalog = rebound.source_catalog
    manifest_path = target.work_root / "analysis_context.json"
    inheritance_path = target.work_root / "analysis_context_inheritance.json"

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("new-item context must not read ZIP metadata or members")

    monkeypatch.setattr(zipfile.ZipFile, "infolist", fail)
    monkeypatch.setattr(zipfile.ZipFile, "read", fail)
    created = BoundAnalysisContext.create_from_transitioned_catalog(
        current,
        target,
        rebound,
        lifecycle,
        telemetry=rebound.workbench.telemetry,
    )
    assert created.context.core_version == current.core_version
    assert created.context.skill_version == current.skill_version
    assert created.source_catalog.core_version == catalog.core_version == "0.3.1"
    assert created.source_catalog.catalog_key == catalog.catalog_key
    assert created.source_catalog.path == catalog.path
    assert created.source_catalog.content_hash == catalog.content_hash
    assert dict(created.runner_config) == dict(rebound.runner_config)
    assert _operations(current.run_root) == counters_before
    assert _data_room_events(rebound) == events_before
    assert manifest_path.is_file() and inheritance_path.is_file()
    assert (target.work_root / "analysis_context_inheritance_state.json").is_file()
    assert not (target.work_root / "analysis_context_transitions.jsonl").exists()

    manifest_bytes = manifest_path.read_bytes()
    inheritance_bytes = inheritance_path.read_bytes()
    loaded = load_bound_analysis_context(current, path=manifest_path, item_workspace=target)
    assert loaded.source_catalog.catalog_key == catalog.catalog_key
    retry = BoundAnalysisContext.create_from_transitioned_catalog(
        current,
        target,
        rebound,
        lifecycle,
        telemetry=rebound.workbench.telemetry,
    )
    assert retry.manifest_hash == loaded.manifest_hash
    assert manifest_path.read_bytes() == manifest_bytes
    assert inheritance_path.read_bytes() == inheritance_bytes


def test_new_item_allows_later_item_when_transition_earliest_is_first(tmp_path: Path) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="A later item.")
    created = BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert created.item_workspace.item_id == "Q-002"
    assert created.source_catalog.core_version == "0.3.1"


def test_new_item_rejects_item_before_transition_earliest(tmp_path: Path) -> None:
    old, current, _source_item, rebound, lifecycle, archive = _fixture(
        tmp_path,
        item_ids=("Q-000", "Q-001", "Q-002"),
    )
    target = ItemWorkspace.create(current, "Q-000", original_text="An earlier item.")
    earlier_bound = BoundAnalysisContext.create(
        old,
        DataAssetRef.from_path(archive),
        ItemWorkspace.create(old, "Q-000", original_text="An earlier item."),
    )
    with pytest.raises(ValueError, match="earliest item"):
        earlier_bound.rebind_implementation(current, target, lifecycle)
    # The old item context intentionally occupies the same item path.  Remove
    # that failed pre-transition manifest before exercising fresh target
    # creation; no transition audit/state was published by the rejected call.
    earlier_bound.manifest_path.unlink()
    with pytest.raises(ValueError, match="source item must precede target"):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert not (target.work_root / "analysis_context.json").exists()


@pytest.mark.parametrize("case", ("target_residue", "source_review", "source_catalog", "source_audit"))
def test_new_item_rejects_residue_or_unstable_source(tmp_path: Path, case: str) -> None:
    _old, current, source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    target_manifest = target.work_root / "analysis_context.json"
    if case == "target_residue":
        (target.work_root / "analysis_context_inheritance.json").write_text("{}\n", encoding="utf-8")
    elif case == "source_review":
        source_item = ItemWorkspace.load(current, "Q-001")
        source_item.write_draft({"answer": "reviewed"})
        source_item.record_review("accept", reviewer_ref="reviewer")
    elif case == "source_catalog":
        rebound.source_catalog.path.unlink()
    else:
        audit_path = rebound.manifest_path.parent / "analysis_context_transitions.jsonl"
        audit_path.write_text(audit_path.read_text(encoding="utf-8").replace("T-032-034", "TAMPERED"), encoding="utf-8")
    with pytest.raises(ValueError):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert not target_manifest.exists()


def test_new_item_rejects_active_target_and_conflicting_run(tmp_path: Path) -> None:
    old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    target.begin_attempt("lane", "role")
    with pytest.raises(ValueError, match="target item"):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)

    other_root = tmp_path / "other-run"
    other_inputs = tmp_path / "other-inputs"
    other_root.mkdir()
    other_inputs.mkdir()
    other = RunContext("RUN-OTHER", other_root, (other_inputs,), core_version="0.3.5", skill_version="0.2.8")
    with pytest.raises(ValueError, match="same run"):
        BoundAnalysisContext.create_from_transitioned_catalog(other, target, rebound, lifecycle)
    assert old.run_id != other.run_id


def test_new_item_creation_is_concurrently_idempotent(tmp_path: Path) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                BoundAnalysisContext.create_from_transitioned_catalog,
                current,
                target,
                rebound,
                lifecycle,
            )
            for _ in range(2)
        ]
        results = [future.result() for future in futures]
    assert all(result.context.core_version == "0.3.5" for result in results)
    audit_path = target.work_root / "analysis_context_transitions.jsonl"
    assert not audit_path.exists()
    inheritance_path = target.work_root / "analysis_context_inheritance.json"
    assert json.loads(inheritance_path.read_text(encoding="utf-8"))["record_kind"] == "analysis_context_catalog_inheritance"
    assert load_bound_analysis_context(current, path=target.work_root / "analysis_context.json", item_workspace=target).source_catalog.core_version == "0.3.1"


@pytest.mark.parametrize(
    "stage",
    ("inheritance_after_intent", "inheritance_after_manifest", "inheritance_after_record", "inheritance_after_state"),
)
def test_new_item_inheritance_failpoints_reconcile_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")

    def failpoint(actual: str) -> None:
        if actual == stage:
            raise RuntimeError(actual)

    monkeypatch.setattr(analysis_module, "_transition_failpoint", failpoint)
    with pytest.raises(RuntimeError, match=stage):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    monkeypatch.setattr(analysis_module, "_transition_failpoint", lambda _actual: None)

    # The intent and transition lock are recoverable program state; a retry
    # must complete the same canonical publication, never reject the lock as
    # residue or create synthetic target transition records.
    if stage == "inheritance_after_intent":
        assert not (target.work_root / "analysis_context.json").exists()
        assert load_bound_analysis_context(
            current,
            path=target.work_root / "analysis_context.json",
            item_workspace=target,
        ).source_catalog.core_version == "0.3.1"
    else:
        assert BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle).source_catalog.core_version == "0.3.1"
    assert not (target.work_root / "analysis_context_inheritance_intent.json").exists()
    assert not (target.work_root / "analysis_context_transitions.jsonl").exists()
    assert load_bound_analysis_context(
        current,
        path=target.work_root / "analysis_context.json",
        item_workspace=target,
    ).source_catalog.catalog_key == rebound.source_catalog.catalog_key


def test_new_item_failure_then_corrected_retry_and_source_provenance_tamper(
    tmp_path: Path,
) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    source_manifest = rebound.manifest_path
    original_source = source_manifest.read_bytes()
    source_manifest.write_bytes(original_source + b"\n")
    with pytest.raises(ValueError):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert not (target.work_root / "analysis_context.json").exists()
    source_manifest.write_bytes(original_source)
    created = BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    source_manifest.write_bytes(original_source + b"\n")
    with pytest.raises(ValueError, match="source analysis context manifest provenance"):
        created.ensure_valid()


def test_new_item_retry_compares_full_manifest_fields_and_rejects_synthetic_audit(tmp_path: Path) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    created = BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    manifest_path = created.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runner_config"]["default_output_bytes"] += 1
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    manifest["manifest_hash"] = analysis_module._sha256_bytes(analysis_module._json_bytes(unsigned))
    analysis_module._atomic_write_json_durable(manifest_path, manifest)
    state_path = target.work_root / "analysis_context_inheritance_state.json"
    analysis_module._write_inheritance_state(
        state_path,
        run_id=current.run_id,
        item_id=target.item_id,
        record_hash=json.loads((target.work_root / "analysis_context_inheritance.json").read_text(encoding="utf-8"))["record_hash"],
        manifest_hash=analysis_module._sha256_file(manifest_path),
    )
    with pytest.raises(ValueError):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)

    (target.work_root / "analysis_context_transitions.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="analysis context transition"):
        load_bound_analysis_context(current, path=manifest_path, item_workspace=target)


def test_new_item_rejects_well_formed_lifecycle_or_original_skill_mismatch(tmp_path: Path) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    ledger_path = current.run_root / "implementation_transitions.jsonl"
    ledger_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(ledger_lines[0])
    first["new_version"] = "skill9.9/core0.3.2"
    first_unsigned = {key: value for key, value in first.items() if key != "record_hash"}
    first["record_hash"] = analysis_module._sha256_bytes(analysis_module._json_bytes(first_unsigned) + b"\n")
    ledger_path.write_text(json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n" + ledger_lines[1] + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="transition version chain|transition audit"):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)

    # Restore the lifecycle ledger, then alter only the source audit's origin
    # skill while preserving a valid audit/state hash chain.  The inheritance
    # builder must connect the source audit to the authoritative ledger rather
    # than accepting a well-formed but unrelated origin identity.
    ledger_path.write_text("\n".join(ledger_lines) + "\n", encoding="utf-8")
    source_audit = rebound.manifest_path.parent / "analysis_context_transitions.jsonl"
    audit_record = json.loads(source_audit.read_text(encoding="utf-8").splitlines()[0])
    audit_record["old_skill_version"] = "skill9.9"
    audit_unsigned = {key: value for key, value in audit_record.items() if key != "record_hash"}
    audit_record["record_hash"] = analysis_module._transition_digest(audit_unsigned)
    source_audit.write_text(json.dumps(audit_record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    source_state = rebound.manifest_path.parent / "analysis_context_transition_state.json"
    analysis_module._write_transition_state(
        source_state,
        run_id=current.run_id,
        item_id="Q-001",
        count=1,
        head=audit_record["record_hash"],
        manifest_hash=analysis_module._sha256_file(rebound.manifest_path),
    )
    with pytest.raises(ValueError, match="source context transition audit"):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)


@pytest.mark.parametrize("missing", ("audit", "state"))
def test_skill_only_missing_source_provenance_fails_before_catalog_instrumentation(
    tmp_path: Path,
    missing: str,
) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _skill_only_fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    counters_before = _operations(current.run_root)
    events_before = _data_room_events(rebound)
    audit_path = rebound.manifest_path.parent / "analysis_context_transitions.jsonl"
    state_path = rebound.manifest_path.parent / "analysis_context_transition_state.json"
    saved = (audit_path.read_bytes(), state_path.read_bytes())
    (audit_path if missing == "audit" else state_path).unlink()
    with pytest.raises(ValueError, match="provenance|audit|state"):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert _operations(current.run_root) == counters_before
    assert _data_room_events(rebound) == events_before
    assert not (target.work_root / "analysis_context.json").exists()
    assert not (target.work_root / "analysis_context_inheritance.json").exists()
    assert not (target.work_root / "analysis_context_inheritance_intent.json").exists()
    audit_path.write_bytes(saved[0])
    state_path.write_bytes(saved[1])


@pytest.mark.parametrize("identity_field", ("implementation_sha", "implementation_tree"))
def test_false_current_repository_identity_fails_prepublication_then_corrected_retry(
    tmp_path: Path,
    identity_field: str,
) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    source_manifest = rebound.manifest_path
    source_audit = source_manifest.parent / "analysis_context_transitions.jsonl"
    source_state = source_manifest.parent / "analysis_context_transition_state.json"
    original_manifest = source_manifest.read_bytes()
    original_state = source_state.read_bytes()
    manifest = json.loads(original_manifest)
    manifest[identity_field] = ("f" if identity_field == "implementation_sha" else "0") * 40
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    manifest["manifest_hash"] = analysis_module._sha256_bytes(analysis_module._json_bytes(unsigned))
    analysis_module._atomic_write_json_durable(source_manifest, manifest)
    records = json.loads(source_audit.read_text(encoding="utf-8").splitlines()[-1])
    analysis_module._write_transition_state(
        source_state,
        run_id=current.run_id,
        item_id="Q-001",
        count=1,
        head=records["record_hash"],
        manifest_hash=analysis_module._sha256_file(source_manifest),
    )
    with pytest.raises(ValueError, match="repository identity|authoritative"):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert not (target.work_root / "analysis_context.json").exists()
    assert not (target.work_root / "analysis_context_inheritance.json").exists()
    assert not (target.work_root / "analysis_context_inheritance_state.json").exists()
    assert not (target.work_root / "analysis_context_inheritance_intent.json").exists()
    source_manifest.write_bytes(original_manifest)
    source_state.write_bytes(original_state)
    created = BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert created.source_catalog.catalog_key == rebound.source_catalog.catalog_key


def test_source_transition_intent_is_rejected_live_and_before_target_publish(tmp_path: Path) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(tmp_path)
    target = ItemWorkspace.create(current, "Q-002", original_text="Target.")
    source_intent = rebound.manifest_path.parent / "analysis_context_transition_intent.json"
    source_intent.write_text("{\"malformed\": true}\n", encoding="utf-8")
    counters_before = _operations(current.run_root)
    events_before = _data_room_events(rebound)
    with pytest.raises(ValueError, match="transition intent"):
        BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    assert _operations(current.run_root) == counters_before
    assert _data_room_events(rebound) == events_before
    assert not (target.work_root / "analysis_context.json").exists()
    source_intent.unlink()
    created = BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    record = json.loads((target.work_root / "analysis_context_inheritance.json").read_text(encoding="utf-8"))
    assert record["source_transition_intent_path"].endswith("analysis_context_transition_intent.json")
    assert record["source_transition_intent_bytes"] == ""
    assert record["source_transition_intent_hash"] == analysis_module._sha256_bytes(b"")
    source_intent.write_text("{\"malformed\": true}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="transition intent"):
        created.ensure_valid()
    source_intent.unlink()
    created.ensure_valid()


def test_inherited_rebind_exact_retry_rechecks_source_hint_and_serializes_q3_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(
        tmp_path,
        item_ids=("Q-001", "Q-002", "Q-003"),
    )
    q2_item = ItemWorkspace.create(current, "Q-002", original_text="Target two.")
    q2 = BoundAnalysisContext.create_from_transitioned_catalog(current, q2_item, rebound, lifecycle)
    q3_item = ItemWorkspace.create(current, "Q-003", original_text="Target three.")

    # A changed contained record is rejected before a target transition lock
    # can authorize an exact retry.  Restore the exact bytes before the live
    # concurrency check.
    inheritance_path = q2.manifest_path.parent / "analysis_context_inheritance.json"
    original_inheritance = inheritance_path.read_bytes()
    inheritance_path.write_bytes(original_inheritance.replace(b"Q-001", b"Q-999", 1))
    with pytest.raises(ValueError, match="inheritance|record|hash"):
        q2.rebind_implementation(current, ItemWorkspace.load(current, "Q-002"), lifecycle)
    inheritance_path.write_bytes(original_inheritance)

    def retry() -> BoundAnalysisContext:
        return q2.rebind_implementation(current, ItemWorkspace.load(current, "Q-002"), lifecycle)

    def create_q3() -> BoundAnalysisContext:
        return BoundAnalysisContext.create_from_transitioned_catalog(current, q3_item, q2, lifecycle)

    # The inherited source path must use its recursive catalog provenance;
    # only a direct rebound source may enter the local-transition preflight.
    def fail_direct_preflight(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("inherited source creation must not require a local transition audit")

    monkeypatch.setattr(analysis_module, "_source_transition_preflight", fail_direct_preflight)
    with ThreadPoolExecutor(max_workers=2) as executor:
        retry_future = executor.submit(retry)
        create_future = executor.submit(create_q3)
        retry_result = retry_future.result(timeout=5)
        create_result = create_future.result(timeout=5)
    assert retry_result.context.core_version == current.core_version
    assert create_result.item_workspace.item_id == "Q-003"
    assert load_bound_analysis_context(current, path=q3_item.work_root / "analysis_context.json", item_workspace=q3_item).source_catalog.catalog_key == rebound.source_catalog.catalog_key


def test_inherited_multi_hop_source_tamper_rejects_before_target_publish(tmp_path: Path) -> None:
    _old, current, _source_item, rebound, lifecycle, _archive = _fixture(
        tmp_path,
        item_ids=("Q-001", "Q-002", "Q-003"),
    )
    q2_item = ItemWorkspace.create(current, "Q-002", original_text="Target two.")
    q2 = BoundAnalysisContext.create_from_transitioned_catalog(current, q2_item, rebound, lifecycle)
    q3_item = ItemWorkspace.create(current, "Q-003", original_text="Target three.")
    q1_audit = rebound.manifest_path.parent / "analysis_context_transitions.jsonl"
    original_audit = q1_audit.read_bytes()
    q1_audit.write_bytes(original_audit.replace(b"T-032-034", b"TAMPERED", 1))
    try:
        with pytest.raises(ValueError, match="transition|provenance|audit"):
            BoundAnalysisContext.create_from_transitioned_catalog(current, q3_item, q2, lifecycle)
        assert not (q3_item.work_root / "analysis_context.json").exists()
        assert not (q3_item.work_root / "analysis_context_inheritance.json").exists()
    finally:
        q1_audit.write_bytes(original_audit)
    created = BoundAnalysisContext.create_from_transitioned_catalog(current, q3_item, q2, lifecycle)
    assert created.item_workspace.item_id == "Q-003"
    assert created.source_catalog.catalog_key == rebound.source_catalog.catalog_key
