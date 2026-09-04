from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import zipfile

import pytest

from auto_foundry_core.data_revisions import (
    DataRevisionError,
    DataRevisionStore,
    PendingDataRefreshConflict,
    RevisionCASMismatch,
    RevisionConflictError,
)
from auto_foundry_core import RequirementRunExtension
from auto_foundry_core.contracts import RequirementRecord
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.requirement_planning import RequirementExecutionPlan
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _archive(path: Path, value: str) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("orders.csv", f"id,value\n1,{value}\n")
    return path


def _plan(*item_ids: str) -> RequirementExecutionPlan:
    records = tuple(
        RequirementRecord(
            requirement_id=item_id,
            original_text=f"Text for {item_id}",
            business_objective=f"Objective for {item_id}",
            expected_analytical_outputs=(),
            expected_visual_outputs=(),
            dependencies=(),
            data_needs=(),
            ontology_needs=(),
            prepared_data_needs=(),
            working_definitions=(),
            limitations=(),
            explicit_priority=None,
            scope="analytics",
            metadata={},
        )
        for item_id in item_ids
    )
    return RequirementExecutionPlan.from_requirements(records)


@pytest.fixture()
def fixture(tmp_path: Path) -> tuple[RunContext, DataRevisionStore, dict[str, Path]]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archives = {name: _archive(inputs / f"{name}.zip", value) for name, value in (("legacy", "one"), ("second", "two"), ("third", "three"), ("fourth", "four"))}
    context = RunContext("RUN-DATA-REVISIONS", tmp_path / "run", (inputs,))
    return context, DataRevisionStore(context), archives


def _refresh_and_receipt(
    tmp_path: Path,
) -> tuple[RunContext, DataRevisionStore, RequirementExecutionPlan, object, object]:
    """Create D2/G2 and archive its applied admission for lineage tests."""

    # Reuse the deterministic generation fixture so the resolver is exercised
    # against real lifecycle/generation metadata, not a synthetic pointer.
    from tests.core.test_data_refresh_generation import _fixture as refresh_fixture

    context, plan, d2 = refresh_fixture(tmp_path)
    store = DataRevisionStore(context)
    parent = RunLifecycle.load(context)
    parent_state_hash = parent.snapshot.manifest_hash
    parent_plan_hash = hashlib.sha256(parent.plan_path.read_bytes()).hexdigest()
    pending = store.admit_pending_data_refresh(
        data_revision=d2,
        plan=plan.to_dict(),
        reopened_item_ids=(),
        expected_parent_generation_id=parent.generation_id,
        expected_parent_state_hash=parent_state_hash,
        expected_parent_plan_hash=parent_plan_hash,
        launch_draft_id="DRAFT-LINEAGE-1",
        launch_fingerprint="a" * 64,
        created_at="2026-08-26T00:00:00Z",
    )
    extension = RequirementRunExtension.refresh_data(context, plan, data_revision=d2)
    applied = store.mark_pending_data_refresh_applied(pending.intent_hash, generation_id=extension.generation_id)
    assert applied is not None
    return context, store, plan, d2, extension


def test_legacy_d0001_alias_and_idempotent_initialization(fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]]) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    retry = store.initialize_legacy("legacy.zip")

    assert first == retry
    assert first.revision_id == "D-0001"
    assert first.archive_alias is True
    assert first.archive_path == archives["legacy"].resolve()
    assert not (context.run_root / "data_room/revisions/D-0001/archive.zip").exists()
    assert first.catalog_path.is_file()
    assert json.loads((context.run_root / "data_room/current_revision.json").read_text())['revision_id'] == "D-0001"


def test_legacy_catalog_from_older_core_loads_but_catalog_hash_tamper_still_fails(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    upgraded = RunContext(
        context.run_id,
        context.run_root,
        context.input_roots,
        core_version="0.8.1",
        skill_version="new-skill",
    )

    loaded = DataRevisionStore(upgraded).load(first.revision_id)
    assert loaded.catalog_core_version == context.core_version

    first.catalog_path.write_bytes(first.catalog_path.read_bytes() + b"tampered")
    with pytest.raises(DataRevisionError, match="catalog hash"):
        DataRevisionStore(upgraded).load(first.revision_id)


def test_append_exact_retry_and_occupied_ordinal_conflict(fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]]) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    second = store.append(
        archives["second"],
        expected_current_revision_id=first.revision_id,
        expected_current_manifest_hash=first.manifest_hash,
    )
    retry = store.append(
        archives["second"],
        expected_current_revision_id=first.revision_id,
        expected_current_manifest_hash=first.manifest_hash,
    )
    assert second == retry
    assert second.revision_id == "D-0002"
    with pytest.raises(RevisionConflictError):
        store.append(
            archives["third"],
            expected_current_revision_id=first.revision_id,
            expected_current_manifest_hash=first.manifest_hash,
        )


def test_stale_expected_current_cas_fails_closed(fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]]) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    second = store.append(archives["second"], expected_current_revision_id=first.revision_id)
    third = store.append(archives["third"], expected_current_revision_id=second.revision_id)
    with pytest.raises(RevisionCASMismatch):
        store.append(archives["fourth"], expected_current_revision_id=first.revision_id)
    assert store.current() == third


def test_concurrent_append_has_one_winner(fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]]) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    stores = [DataRevisionStore(context), DataRevisionStore(context)]

    def append(index: int):
        try:
            return stores[index].append(
                archives["second" if index == 0 else "third"],
                expected_current_revision_id=first.revision_id,
                expected_current_manifest_hash=first.manifest_hash,
            )
        except Exception as exc:  # the test asserts one winner and one failure
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, range(2)))
    successes = [value for value in results if not isinstance(value, Exception)]
    failures = [value for value in results if isinstance(value, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], (RevisionCASMismatch, RevisionConflictError))
    assert store.current() == successes[0]


def test_failpoint_before_pointer_swap_leaves_old_pointer_and_exact_retry(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    original_pointer = (context.run_root / "data_room/current_revision.json").read_bytes()

    def fail(name: str) -> None:
        if name == "before_pointer_swap":
            raise RuntimeError("injected before pointer swap")

    store._failpoint = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before pointer swap"):
        store.append(archives["second"], expected_current_revision_id=first.revision_id)
    assert (context.run_root / "data_room/current_revision.json").read_bytes() == original_pointer
    assert store.current() == first
    assert store.load("D-0002").parent_manifest_hash == first.manifest_hash

    store._failpoint = lambda _name: None  # type: ignore[method-assign]
    recovered = store.append(archives["second"], expected_current_revision_id=first.revision_id)
    assert recovered.revision_id == "D-0002"
    assert store.current() == recovered


def test_candidate_materialization_does_not_hold_run_lifecycle_lock(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    entered = threading.Event()
    release = threading.Event()
    lock_acquired = threading.Event()
    result: list[object] = []

    def pause(stage_archive: Path) -> None:
        assert stage_archive.is_file()
        entered.set()
        assert release.wait(5)

    def append_worker() -> None:
        try:
            result.append(store.append(archives["second"], expected_current_revision_id=first.revision_id))
        except Exception as exc:  # pragma: no cover - assertion below reports it
            result.append(exc)

    def lock_probe() -> None:
        with RunLifecycle._run_lock(context):
            lock_acquired.set()

    store._candidate_build_hook = pause  # type: ignore[method-assign]
    worker = threading.Thread(target=append_worker)
    worker.start()
    assert entered.wait(5)
    probe = threading.Thread(target=lock_probe)
    probe.start()
    assert lock_acquired.wait(2)
    release.set()
    worker.join(5)
    probe.join(5)
    assert result and getattr(result[0], "revision_id", None) == "D-0002"


def test_revision_transaction_and_admission_journal_recover_exactly(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    metadata = {
        "launch_draft_id": "DRAFT-TX",
        "launch_fingerprint": "a" * 64,
        "created_at": "2026-08-26T00:00:00Z",
    }
    failed = {"name": "before_revision_transaction"}

    def fail(name: str) -> None:
        if name == failed["name"]:
            raise RuntimeError(name)

    store._failpoint = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before_revision_transaction"):
        store.append(
            archives["second"],
            expected_current_revision_id=first.revision_id,
            expected_current_manifest_hash=first.manifest_hash,
            transaction=metadata,
        )
    assert store.current() == first
    assert store.revision_transaction() is None

    failed["name"] = "before_pointer_swap"
    with pytest.raises(RuntimeError, match="before_pointer_swap"):
        store.append(
            archives["second"],
            expected_current_revision_id=first.revision_id,
            expected_current_manifest_hash=first.manifest_hash,
            transaction=metadata,
        )
    assert store.current() == first
    assert store.revision_transaction() is not None

    store._failpoint = lambda _name: None  # type: ignore[method-assign]
    second = store.append(
        archives["second"],
        expected_current_revision_id=first.revision_id,
        expected_current_manifest_hash=first.manifest_hash,
        transaction=metadata,
    )
    assert store.current() == second
    assert store.revision_transaction() is not None

    failed["name"] = "before_revision_transaction_clear"
    store._failpoint = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before_revision_transaction_clear"):
        store.admit_pending_data_refresh(
            data_revision=second,
            plan=_plan("REQ-01").to_dict(),
            reopened_item_ids=("REQ-01",),
            expected_parent_generation_id="G-0001",
            expected_parent_state_hash="b" * 64,
            expected_parent_plan_hash="c" * 64,
            launch_draft_id=metadata["launch_draft_id"],
            launch_fingerprint=metadata["launch_fingerprint"],
            created_at=metadata["created_at"],
            data_revision_ref="data_room/revisions/D-0002/revision_manifest.json",
        )
    assert store.pending_data_refresh() is not None
    assert store.revision_transaction() is not None

    store._failpoint = lambda _name: None  # type: ignore[method-assign]
    admitted = store.admit_pending_data_refresh(
        data_revision=second,
        plan=_plan("REQ-01").to_dict(),
        reopened_item_ids=("REQ-01",),
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="b" * 64,
        expected_parent_plan_hash="c" * 64,
        launch_draft_id=metadata["launch_draft_id"],
        launch_fingerprint=metadata["launch_fingerprint"],
        created_at=metadata["created_at"],
        data_revision_ref="data_room/revisions/D-0002/revision_manifest.json",
    )
    assert admitted.state == "pending"
    assert store.revision_transaction() is None


def test_revision_transaction_clear_requires_owner_or_canonical_admission(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    second = store.append(
        archives["second"],
        expected_current_revision_id=first.revision_id,
        expected_current_manifest_hash=first.manifest_hash,
        transaction={
            "launch_draft_id": "DRAFT-A",
            "launch_fingerprint": "a" * 64,
            "created_at": "2026-08-26T00:00:00Z",
        },
    )
    with pytest.raises(RevisionConflictError):
        store.complete_revision_transaction(
            second,
            launch_draft_id="DRAFT-B",
            launch_fingerprint="b" * 64,
        )
    assert store.revision_transaction() is not None
    store.complete_revision_transaction(
        second,
        launch_draft_id="DRAFT-A",
        launch_fingerprint="a" * 64,
    )
    assert store.revision_transaction() is None

    third = store.append(
        archives["third"],
        expected_current_revision_id=second.revision_id,
        transaction={
            "launch_draft_id": "DRAFT-C",
            "launch_fingerprint": "c" * 64,
            "created_at": "2026-08-26T00:00:01Z",
        },
    )
    store._failpoint = lambda name: (_ for _ in ()).throw(RuntimeError(name)) if name == "before_revision_transaction_clear" else None  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before_revision_transaction_clear"):
        store.admit_pending_data_refresh(
            data_revision=third,
            plan=_plan("REQ-01").to_dict(),
            reopened_item_ids=("REQ-01",),
            expected_parent_generation_id="G-0001",
            expected_parent_state_hash="d" * 64,
            expected_parent_plan_hash="e" * 64,
            launch_draft_id="DRAFT-B",
            launch_fingerprint="b" * 64,
            created_at="2026-08-26T00:00:02Z",
        )
    store._failpoint = lambda _name: None  # type: ignore[method-assign]
    pending = store.pending_data_refresh()
    assert pending.state == "pending"
    # Canonical admission is now the proof, even though the transaction was
    # originally created by another launch draft.
    store.complete_revision_transaction(
        third,
        launch_draft_id="DRAFT-B",
        launch_fingerprint="b" * 64,
    )
    assert store.revision_transaction() is None


def _handoff(
    plan: RequirementExecutionPlan,
    reopened: tuple[str, ...],
    *,
    draft_id: str,
    fingerprint: str,
    created_at: str,
) -> dict[str, object]:
    return {
        "launch_draft_id": draft_id,
        "launch_fingerprint": fingerprint,
        "created_at": created_at,
        "plan": plan.to_dict(),
        "reopened_item_ids": reopened,
        "expected_parent_generation_id": "G-0001",
        "expected_parent_state_hash": "b" * 64,
        "expected_parent_plan_hash": "c" * 64,
    }


def test_complete_handoff_recovers_before_same_d_revision_takeover(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    plan_a = _plan("REQ-A")
    second = store.append(
        archives["second"],
        expected_current_revision_id=first.revision_id,
        expected_current_manifest_hash=first.manifest_hash,
        transaction=_handoff(
            plan_a,
            ("REQ-A",),
            draft_id="DRAFT-A",
            fingerprint="a" * 64,
            created_at="2026-08-26T00:00:00Z",
        ),
    )
    transaction = store.revision_transaction()
    assert transaction is not None and transaction.has_handoff
    assert transaction.plan["input_records"][0]["requirement_id"] == "REQ-A"

    plan_b = _plan("REQ-B")
    retry = store.append(
        archives["second"],
        expected_current_revision_id=first.revision_id,
        expected_current_manifest_hash=first.manifest_hash,
        transaction=_handoff(
            plan_b,
            ("REQ-B",),
            draft_id="DRAFT-B",
            fingerprint="b" * 64,
            created_at="2026-08-26T00:00:01Z",
        ),
    )
    assert retry.revision_id == second.revision_id
    pending = store.pending_data_refresh(allow_stale=True)
    assert pending is not None
    assert pending.data_revision_id == "D-0002"
    assert [record["requirement_id"] for record in pending.plan["input_records"]] == ["REQ-A"]

    admitted = store.admit_pending_data_refresh(
        data_revision=retry,
        plan=plan_b.to_dict(),
        reopened_item_ids=("REQ-B",),
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="b" * 64,
        expected_parent_plan_hash="c" * 64,
        launch_draft_id="DRAFT-B",
        launch_fingerprint="b" * 64,
        created_at="2026-08-26T00:00:01Z",
    )
    assert [record["requirement_id"] for record in admitted.plan["input_records"]] == ["REQ-A", "REQ-B"]
    assert admitted.reopened_item_ids == ("REQ-A", "REQ-B")
    assert admitted.original_parent_generation_id == "G-0001"
    assert store.revision_transaction() is None


def test_complete_handoff_recovers_before_successor_d_revision_append(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    plan_a = _plan("REQ-A")
    second = store.append(
        archives["second"],
        expected_current_revision_id=first.revision_id,
        expected_current_manifest_hash=first.manifest_hash,
        transaction=_handoff(
            plan_a,
            ("REQ-A",),
            draft_id="DRAFT-A",
            fingerprint="a" * 64,
            created_at="2026-08-26T00:00:00Z",
        ),
    )
    plan_b = _plan("REQ-B")
    third = store.append(
        archives["third"],
        expected_current_revision_id=second.revision_id,
        expected_current_manifest_hash=second.manifest_hash,
        transaction=_handoff(
            plan_b,
            ("REQ-B",),
            draft_id="DRAFT-B",
            fingerprint="b" * 64,
            created_at="2026-08-26T00:00:01Z",
        ),
    )
    stale = store.pending_data_refresh(allow_stale=True)
    assert stale is not None
    assert stale.data_revision_id == "D-0002"
    assert [record["requirement_id"] for record in stale.plan["input_records"]] == ["REQ-A"]

    admitted = store.admit_pending_data_refresh(
        data_revision=third,
        plan=plan_b.to_dict(),
        reopened_item_ids=("REQ-B",),
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="b" * 64,
        expected_parent_plan_hash="c" * 64,
        launch_draft_id="DRAFT-B",
        launch_fingerprint="b" * 64,
        created_at="2026-08-26T00:00:01Z",
    )
    assert admitted.data_revision_id == "D-0003"
    assert [record["requirement_id"] for record in admitted.plan["input_records"]] == ["REQ-A", "REQ-B"]
    assert admitted.reopened_item_ids == ("REQ-A", "REQ-B")
    assert admitted.original_parent_generation_id == "G-0001"
    assert store.revision_transaction() is None


def test_legacy_transaction_recovery_journals_an_existing_unpublished_directory(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, archives = fixture
    metadata = {
        "launch_draft_id": "DRAFT-LEGACY-TX",
        "launch_fingerprint": "d" * 64,
        "created_at": "2026-08-26T00:00:00Z",
    }
    fail_once = {"enabled": True}

    def fail(name: str) -> None:
        if name == "before_revision_transaction" and fail_once["enabled"]:
            fail_once["enabled"] = False
            raise RuntimeError(name)

    store._failpoint = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before_revision_transaction"):
        store.initialize_legacy("legacy.zip", transaction=metadata)
    assert store.current() is None
    assert (context.run_root / "data_room/revisions/D-0001").is_dir()
    assert store.revision_transaction() is None

    recovered = store.initialize_legacy("legacy.zip", transaction=metadata)
    assert recovered.revision_id == "D-0001"
    assert store.current() == recovered
    assert store.revision_transaction() is not None


@pytest.mark.parametrize("artifact", ["pointer", "manifest", "catalog", "archive"])
def test_pointer_manifest_catalog_and_archive_tamper_fail_closed(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
    artifact: str,
) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    paths = {
        "pointer": context.run_root / "data_room/current_revision.json",
        "manifest": first.manifest_path,
        "catalog": first.catalog_path,
        "archive": first.archive_path,
    }
    path = paths[artifact]
    original = path.read_bytes()
    path.write_bytes(original + b"tampered")
    try:
        with pytest.raises(ValueError):
            store.current() if artifact == "pointer" else store.load(first.revision_id)
    finally:
        path.write_bytes(original)


def test_symlinked_pointer_manifest_catalog_and_archive_are_rejected(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    for path, operation in (
        (context.run_root / "data_room/current_revision.json", lambda: store.current()),
        (first.manifest_path, lambda: store.load(first.revision_id)),
        (first.catalog_path, lambda: store.load(first.revision_id)),
        (first.archive_path, lambda: store.load(first.revision_id)),
    ):
        backup = path.with_name(f"{path.name}.backup")
        outside = context.run_root.parent / f"outside-{path.name}"
        outside.write_bytes(path.read_bytes())
        path.rename(backup)
        path.symlink_to(outside)
        try:
            with pytest.raises((AllowedRootError, ValueError)):
                operation()
        finally:
            path.unlink()
            backup.rename(path)
            outside.unlink()


def test_old_revision_bytes_remain_unchanged_after_d0003_and_paths_are_confined(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    second = store.append(archives["second"], expected_current_revision_id=first.revision_id)
    first_bytes = (first.archive_path.read_bytes(), first.catalog_path.read_bytes(), first.manifest_path.read_bytes())
    second_bytes = (second.archive_path.read_bytes(), second.catalog_path.read_bytes(), second.manifest_path.read_bytes())
    third = store.append(archives["third"], expected_current_revision_id=second.revision_id)
    assert third.revision_id == "D-0003"
    assert (first.archive_path.read_bytes(), first.catalog_path.read_bytes(), first.manifest_path.read_bytes()) == first_bytes
    assert (second.archive_path.read_bytes(), second.catalog_path.read_bytes(), second.manifest_path.read_bytes()) == second_bytes
    with pytest.raises((AllowedRootError, ValueError)):
        store.append(Path("../../outside.zip"), expected_current_revision_id=third.revision_id)
    with pytest.raises(ValueError):
        store.load("../D-0001")


def test_pending_refresh_exact_retry_and_d2_d3_structural_coalesce(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    second = store.append(archives["second"], expected_current_revision_id=first.revision_id)
    first_plan = _plan("REQ-001", "REQ-002")
    pending = store.admit_pending_data_refresh(
        data_revision=second,
        plan=first_plan.to_dict(),
        reopened_item_ids=["REQ-001"],
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="a" * 64,
        expected_parent_plan_hash="b" * 64,
        launch_draft_id="DRAFT-1",
        launch_fingerprint="c" * 64,
        created_at="2026-08-26T00:00:00Z",
    )
    retry = store.admit_pending_data_refresh(
        data_revision=second,
        plan=first_plan.to_dict(),
        reopened_item_ids=["REQ-001"],
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="a" * 64,
        expected_parent_plan_hash="b" * 64,
        launch_draft_id="DRAFT-1",
        launch_fingerprint="c" * 64,
        created_at="2026-08-26T00:00:00Z",
    )
    assert retry.intent_hash == pending.intent_hash
    third = store.append(archives["third"], expected_current_revision_id=second.revision_id)
    second_plan = _plan("REQ-001", "REQ-003")
    coalesced = store.admit_pending_data_refresh(
        data_revision=third,
        plan=second_plan.to_dict(),
        reopened_item_ids=["REQ-003"],
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="a" * 64,
        expected_parent_plan_hash="b" * 64,
        launch_draft_id="DRAFT-2",
        launch_fingerprint="d" * 64,
        created_at="2026-08-26T00:00:01Z",
    )
    assert coalesced.data_revision_id == "D-0003"
    assert tuple(record["requirement_id"] for record in coalesced.plan["input_records"]) == (
        "REQ-001",
        "REQ-002",
        "REQ-003",
    )
    assert coalesced.reopened_item_ids == ("REQ-001", "REQ-003")
    with pytest.raises(PendingDataRefreshConflict):
        store.admit_pending_data_refresh(
            data_revision=third,
            plan=_plan("REQ-001", "REQ-004").to_dict(),
            reopened_item_ids=["REQ-004"],
            expected_parent_generation_id="G-0002",
            expected_parent_state_hash="e" * 64,
            expected_parent_plan_hash="f" * 64,
            launch_draft_id="DRAFT-3",
            launch_fingerprint="1" * 64,
            created_at="2026-08-26T00:00:02Z",
        )


def test_pending_refresh_rebase_preserves_original_parent_and_coalesces_current_plan(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    _context, store, archives = fixture
    first = store.initialize_legacy("legacy.zip")
    second = store.append(archives["second"], expected_current_revision_id=first.revision_id)
    original_plan = _plan("REQ-001")
    pending = store.admit_pending_data_refresh(
        data_revision=second,
        plan=original_plan.to_dict(),
        reopened_item_ids=("REQ-001",),
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="a" * 64,
        expected_parent_plan_hash="b" * 64,
        launch_draft_id="DRAFT-REBASE",
        launch_fingerprint="c" * 64,
        created_at="2026-08-26T00:00:00Z",
    )
    current_plan = _plan("REQ-001", "REQ-002")
    rebased = store.rebase_pending_data_refresh(
        pending.intent_hash,
        expected_parent_generation_id="G-0002",
        expected_parent_state_hash="d" * 64,
        expected_parent_plan_hash="e" * 64,
        plan=current_plan.to_dict(),
    )
    assert rebased.expected_parent_generation_id == "G-0002"
    assert rebased.expected_parent_state_hash == "d" * 64
    assert rebased.original_parent_generation_id == "G-0001"
    assert rebased.original_parent_state_hash == "a" * 64
    assert rebased.original_parent_plan_hash == "b" * 64
    assert tuple(record["requirement_id"] for record in rebased.plan["input_records"]) == (
        "REQ-001",
        "REQ-002",
    )


def test_pending_refresh_tamper_and_symlink_fail_closed(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, _archives = fixture
    revision = store.initialize_legacy("legacy.zip")
    pending = store.admit_pending_data_refresh(
        data_revision=revision,
        plan=_plan("REQ-001").to_dict(),
        reopened_item_ids=["REQ-001"],
        expected_parent_generation_id="G-0001",
        expected_parent_state_hash="a" * 64,
        expected_parent_plan_hash="b" * 64,
        launch_draft_id="DRAFT-1",
        launch_fingerprint="c" * 64,
        created_at="2026-08-26T00:00:00Z",
    )
    path = store.pending_data_refresh_path
    original = path.read_bytes()
    path.write_bytes(original + b"tampered")
    try:
        with pytest.raises(DataRevisionError):
            store.pending_data_refresh()
    finally:
        path.write_bytes(original)
    backup = path.with_name("pending_data_refresh.backup")
    outside = context.run_root.parent / "pending-refresh-outside.json"
    outside.write_bytes(original)
    path.rename(backup)
    path.symlink_to(outside)
    try:
        with pytest.raises((DataRevisionError, AllowedRootError)):
            store.pending_data_refresh()
    finally:
        path.unlink()
        backup.rename(path)
        outside.unlink()
    assert store.pending_data_refresh().intent_hash == pending.intent_hash


def test_active_generation_resolver_keeps_d1_when_current_pointer_is_d2(
    fixture: tuple[RunContext, DataRevisionStore, dict[str, Path]],
) -> None:
    context, store, archives = fixture
    RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    first = store.initialize_legacy("legacy.zip")
    store.append(archives["second"], expected_current_revision_id=first.revision_id)

    # The lifecycle is still at its initial G1 boundary; a newer mutable D
    # pointer is not an active analytical binding until a generation admits it.
    resolved = store.active_generation_revision()
    assert resolved is not None
    assert resolved.revision_id == "D-0001"
    explicit = store.active_generation_revision(generation_id="G-0001")
    assert explicit is not None and explicit.revision_id == "D-0001"


def test_active_generation_resolver_direct_binding_and_receipt_lineage(tmp_path: Path) -> None:
    context, store, plan, d2, extension = _refresh_and_receipt(tmp_path)
    direct = store.active_generation_revision()
    assert direct is not None and direct.revision_id == "D-0002"

    # During a crash window where the generation metadata is present but its
    # applied receipt has not yet been observed, the direct binding wins.
    metadata = extension.metadata
    metadata_without_direct = {
        "generation_id": metadata.generation_id,
        "data_revision_ref": None,
        "data_revision_hash": None,
    }
    inherited = store.active_generation_revision(generation_metadata=metadata_without_direct)
    assert inherited is not None and inherited.revision_id == "D-0002"

    # A later applied D3 receipt becomes authoritative for G3 and later even
    # when the generation's direct fields are temporarily absent.
    archive_three = _archive(context.read_roots[0] / "three.zip", "three")
    d3 = store.append(archive_three, expected_current_revision_id=d2.revision_id)
    parent = RunLifecycle.load(context)
    parent_state_hash = parent.snapshot.manifest_hash
    parent_plan_hash = hashlib.sha256(parent.plan_path.read_bytes()).hexdigest()
    pending = store.admit_pending_data_refresh(
        data_revision=d3,
        plan=plan.to_dict(),
        reopened_item_ids=(),
        expected_parent_generation_id=parent.generation_id,
        expected_parent_state_hash=parent_state_hash,
        expected_parent_plan_hash=parent_plan_hash,
        launch_draft_id="DRAFT-LINEAGE-2",
        launch_fingerprint="b" * 64,
        created_at="2026-08-26T00:00:01Z",
    )
    extension_three = RequirementRunExtension.refresh_data(context, plan, data_revision=d3)
    applied = store.mark_pending_data_refresh_applied(pending.intent_hash, generation_id=extension_three.generation_id)
    assert applied is not None
    latest_metadata = extension_three.metadata
    latest_without_direct = {
        "generation_id": latest_metadata.generation_id,
        "data_revision_ref": None,
        "data_revision_hash": None,
    }
    latest = store.active_generation_revision(generation_metadata=latest_without_direct)
    assert latest is not None and latest.revision_id == "D-0003"


def test_active_generation_resolver_loads_only_selected_receipt_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _context, store, _plan, _d2, extension = _refresh_and_receipt(tmp_path)
    metadata = extension.metadata
    without_direct = {
        "generation_id": metadata.generation_id,
        "data_revision_ref": None,
        "data_revision_hash": None,
    }
    original_load = store.load
    calls: list[str] = []

    def counted(revision_id: str):
        calls.append(revision_id)
        return original_load(revision_id)

    monkeypatch.setattr(store, "load", counted)
    resolved = store.active_generation_revision(generation_metadata=without_direct)
    assert resolved is not None and resolved.revision_id == "D-0002"
    assert calls == ["D-0002"]


def test_active_generation_resolver_ignores_unrelated_audit_names(tmp_path: Path) -> None:
    _context, store, _plan, _d2, extension = _refresh_and_receipt(tmp_path)
    (store.pending_data_refresh_archive_root / "audit-note.txt").write_text("not a receipt", encoding="utf-8")
    (store.pending_data_refresh_archive_root / "unrelated.json").write_text("{}\n", encoding="utf-8")
    metadata = extension.metadata
    resolved = store.active_generation_revision(
        generation_metadata={
            "generation_id": metadata.generation_id,
            "data_revision_ref": None,
            "data_revision_hash": None,
        }
    )
    assert resolved is not None and resolved.revision_id == "D-0002"


def test_active_generation_resolver_rejects_tampered_matching_receipt(tmp_path: Path) -> None:
    _context, store, _plan, _d2, extension = _refresh_and_receipt(tmp_path)
    receipt_path = next(store.pending_data_refresh_archive_root.glob("*.json"))
    body = json.loads(receipt_path.read_text(encoding="utf-8"))
    body["data_revision_manifest_hash"] = "0" * 64
    receipt_path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    metadata = extension.metadata
    with pytest.raises(DataRevisionError):
        store.active_generation_revision(
            generation_metadata={
                "generation_id": metadata.generation_id,
                "data_revision_ref": None,
                "data_revision_hash": None,
            }
        )


def test_active_generation_resolver_rejects_conflicting_receipts_for_generation(tmp_path: Path) -> None:
    context, store, plan, d2, extension = _refresh_and_receipt(tmp_path)
    archive_three = _archive(context.read_roots[0] / "three.zip", "three")
    d3 = store.append(archive_three, expected_current_revision_id=d2.revision_id)
    parent = RunLifecycle.load(context)
    pending = store.admit_pending_data_refresh(
        data_revision=d3,
        plan=plan.to_dict(),
        reopened_item_ids=(),
        expected_parent_generation_id=parent.generation_id,
        expected_parent_state_hash=parent.snapshot.manifest_hash,
        expected_parent_plan_hash=hashlib.sha256(parent.plan_path.read_bytes()).hexdigest(),
        launch_draft_id="DRAFT-LINEAGE-CONFLICT",
        launch_fingerprint="c" * 64,
        created_at="2026-08-26T00:00:02Z",
    )
    # Deliberately bind a structurally valid second receipt to the same G2;
    # lineage selection must fail closed instead of choosing by filename/time.
    store.mark_pending_data_refresh_applied(pending.intent_hash, generation_id=extension.generation_id)
    metadata = extension.metadata
    with pytest.raises(DataRevisionError, match="conflict"):
        store.active_generation_revision(
            generation_metadata={
                "generation_id": metadata.generation_id,
                "data_revision_ref": None,
                "data_revision_hash": None,
            }
        )
