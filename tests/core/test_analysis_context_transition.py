from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import zipfile

import pytest

import auto_foundry_core.analysis as analysis_module
from auto_foundry_core.analysis import BoundAnalysisContext, load_bound_analysis_context
from auto_foundry_core.contracts import DataAssetRef, ImplementationTransition
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.workspace import RunContext


def _fixture(tmp_path: Path) -> tuple[RunContext, RunContext, ItemWorkspace, BoundAnalysisContext, RunLifecycle]:
    inputs = tmp_path / "inputs"
    run = tmp_path / "run"
    inputs.mkdir()
    run.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", b"order_id,region\nA-1,DE\nA-2,FR\n")
    old = RunContext("RUN-REBIND", run, (inputs,), core_version="0.3.1", skill_version="0.2.5")
    item = ItemWorkspace.create(old, "Q-001", original_text="Summarize the fixture.")
    bound = BoundAnalysisContext.create(old, DataAssetRef.from_path(archive), item)
    lifecycle = RunLifecycle.create(old, ["Q-001"])
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-031-032",
            old_sha="a" * 40,
            new_sha="b" * 40,
            old_tree="c" * 40,
            new_tree="d" * 40,
            old_version="skill0.2.5/core0.3.1",
            new_version="skill0.2.7/core0.3.2",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="implementation transition",
            resume_point="analysis_context_rebind",
        )
    )
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-032-033",
            old_sha="b" * 40,
            new_sha="e" * 40,
            old_tree="d" * 40,
            new_tree="f" * 40,
            old_version="skill0.2.7/core0.3.2",
            new_version="skill0.2.7/core0.3.4",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="implementation transition",
            resume_point="analysis_context_rebind",
        )
    )
    new = RunContext("RUN-REBIND", run, (inputs,), core_version="0.3.4", skill_version="0.2.7")
    return old, new, item, bound, lifecycle


def _skill_only_fixture(tmp_path: Path) -> tuple[RunContext, RunContext, ItemWorkspace, BoundAnalysisContext, RunLifecycle]:
    old, _unused_new, item, bound, _unused_lifecycle = _fixture(tmp_path)
    run = old.run_root
    inputs = old.input_roots[0]
    (run / "implementation_transitions.jsonl").unlink()
    lifecycle = RunLifecycle.create(old, ["Q-001"])
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-SKILL-ONLY",
            old_sha="a" * 40,
            new_sha="e" * 40,
            old_tree="c" * 40,
            new_tree="f" * 40,
            old_version="skill0.2.5/core0.3.1",
            new_version="skill0.2.7/core0.3.1",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="skill transition",
            resume_point="analysis_context_rebind",
        )
    )
    new = RunContext("RUN-REBIND", run, (inputs,), core_version="0.3.1", skill_version="0.2.7")
    return old, new, item, bound, lifecycle


def test_rebind_reuses_catalog_and_old_identity_fails_closed(tmp_path: Path) -> None:
    old, new, old_item, bound, lifecycle = _fixture(tmp_path)
    catalog_path = bound.source_catalog.path
    catalog_key = bound.source_catalog.catalog_key
    catalog_hash = bound.source_catalog.content_hash
    counters_path = old.run_root / "telemetry" / "inventory_counters.json"
    before_counters = json.loads(counters_path.read_text(encoding="utf-8"))["operations"]
    new_item = ItemWorkspace.load(new, "Q-001")

    rebound = bound.rebind_implementation(new, new_item, lifecycle)

    assert rebound.context.core_version == "0.3.4"
    assert rebound.context.skill_version == "0.2.7"
    assert rebound.source_catalog.path == catalog_path
    assert rebound.source_catalog.catalog_key == catalog_key
    assert rebound.source_catalog.content_hash == catalog_hash
    assert rebound.workbench.catalog() == rebound.source_catalog.entries
    assert json.loads(counters_path.read_text(encoding="utf-8"))["operations"] == before_counters
    with pytest.raises(ValueError, match="run/core identity"):
        load_bound_analysis_context(old, path=bound.manifest_path, item_workspace=old_item)
    loaded = load_bound_analysis_context(new, path=bound.manifest_path, item_workspace=new_item)
    manifest = json.loads(bound.manifest_path.read_text(encoding="utf-8"))
    assert manifest["implementation_sha"] == "e" * 40
    assert manifest["implementation_tree"] == "f" * 40
    assert loaded.source_catalog.path == catalog_path
    assert (bound.manifest_path.parent / "analysis_context_transitions.jsonl").is_file()


def test_skill_only_rebind_preserves_catalog_without_zip_or_inventory_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _old, new, _old_item, bound, lifecycle = _skill_only_fixture(tmp_path)
    new_item = ItemWorkspace.load(new, "Q-001")
    catalog_path = bound.source_catalog.path
    counters_path = bound.context.run_root / "telemetry" / "inventory_counters.json"
    before_counters = json.loads(counters_path.read_text(encoding="utf-8"))["operations"]
    telemetry = bound.workbench.telemetry
    before_archive_events = sum(event.event_type == "data_room_archive_read" for event in telemetry.events)
    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("rebound context must not read ZIP members")

    monkeypatch.setattr(zipfile.ZipFile, "infolist", fail)
    monkeypatch.setattr(zipfile.ZipFile, "read", fail)
    rebound = bound.rebind_implementation(new, new_item, lifecycle)
    loaded = load_bound_analysis_context(new, path=bound.manifest_path, item_workspace=new_item)
    assert rebound.source_catalog.path == catalog_path
    assert loaded.source_catalog.path == catalog_path
    assert json.loads(counters_path.read_text(encoding="utf-8"))["operations"] == before_counters
    assert sum(event.event_type == "data_room_archive_read" for event in telemetry.events) == before_archive_events


def test_rebound_load_requires_preserved_catalog(tmp_path: Path) -> None:
    _old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    new_item = ItemWorkspace.load(new, "Q-001")
    rebound = bound.rebind_implementation(new, new_item, lifecycle)
    rebound.source_catalog.path.unlink()
    with pytest.raises(ValueError, match="analysis catalog"):
        load_bound_analysis_context(new, path=rebound.manifest_path, item_workspace=new_item)


@pytest.mark.parametrize("tamper", ("delete", "truncate", "rewrite"))
def test_bound_context_mutations_revalidate_transition_audit(tmp_path: Path, tamper: str) -> None:
    _old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    new_item = ItemWorkspace.load(new, "Q-001")
    rebound = bound.rebind_implementation(new, new_item, lifecycle)
    script = rebound.item_workspace.work_root / "noop.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    audit_path = rebound.manifest_path.parent / "analysis_context_transitions.jsonl"
    if tamper == "delete":
        audit_path.unlink()
    elif tamper == "truncate":
        audit_path.write_bytes(b"")
    else:
        audit_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        rebound.save_prepared_candidate("candidate", [{"value": 1}])
    with pytest.raises(ValueError):
        rebound.script_runner.execute(script, phase="smoke")


def test_concurrent_rebind_is_one_idempotent_audit_record(tmp_path: Path) -> None:
    _old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    new_item = ItemWorkspace.load(new, "Q-001")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bound.rebind_implementation, new, new_item, lifecycle) for _ in range(2)]
        results = [future.result() for future in futures]
    assert all(result.context.core_version == "0.3.4" for result in results)
    audit_path = bound.manifest_path.parent / "analysis_context_transitions.jsonl"
    assert len(audit_path.read_text(encoding="utf-8").splitlines()) == 1


def test_active_attempt_race_is_ordered_by_item_state_lock(tmp_path: Path) -> None:
    _old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    requested_item = ItemWorkspace.load(new, "Q-001")

    def rebind() -> tuple[str, object]:
        try:
            return "rebind", bound.rebind_implementation(new, requested_item, lifecycle)
        except Exception as exc:  # noqa: BLE001 - assert the race outcome below
            return "rebind_error", exc

    def begin() -> tuple[str, object]:
        try:
            item = ItemWorkspace.load(new, "Q-001")
            return "attempt", item.begin_attempt("race-lane", "race-role")
        except Exception as exc:  # noqa: BLE001 - assert the race outcome below
            return "attempt_error", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = (future.result() for future in (executor.submit(rebind), executor.submit(begin)))
    results = {first[0]: first[1], second[0]: second[1]}
    assert set(results) <= {"rebind", "rebind_error", "attempt", "attempt_error"}
    assert "rebind" in results or "rebind_error" in results
    final_item = ItemWorkspace.load(new, "Q-001")
    if "rebind" in results:
        assert final_item.state["active_attempt_id"] is not None
        assert json.loads(bound.manifest_path.read_text(encoding="utf-8"))["core_version"] == "0.3.4"
    else:
        assert final_item.state["active_attempt_id"] is not None
        assert not (bound.manifest_path.parent / "analysis_context_transition_intent.json").exists()
        assert isinstance(results["rebind_error"], ValueError)


def test_review_commit_and_rebind_linearize_at_item_state_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, new, old_item, bound, lifecycle = _fixture(tmp_path)
    old_item.write_draft({"answer": "reviewable"})
    new_item = ItemWorkspace.load(new, "Q-001")
    manifest_before = bound.manifest_path.read_bytes()
    review_entered = threading.Event()
    release_review = threading.Event()
    rebind_lock_attempted = threading.Event()
    rebind_done = threading.Event()
    review_errors: list[BaseException] = []
    rebind_results: list[object] = []

    original_write = old_item._write_business_review

    def blocked_write(
        packet: object,
        *,
        touch_state: bool = True,
        emit: bool = True,
    ) -> None:
        review_entered.set()
        if not release_review.wait(5):
            raise AssertionError("review commit was not released")
        original_write(packet, touch_state=touch_state, emit=emit)

    monkeypatch.setattr(old_item, "_write_business_review", blocked_write)
    original_item_lock = new_item._state_transition_lock

    @contextmanager
    def observed_item_lock():
        rebind_lock_attempted.set()
        with original_item_lock():
            yield

    monkeypatch.setattr(new_item, "_state_transition_lock", observed_item_lock)

    def record_review() -> None:
        try:
            old_item.record_review("accept", reviewer_ref="reviewer")
        except BaseException as exc:  # noqa: BLE001 - report the race outcome
            review_errors.append(exc)

    def rebind() -> None:
        try:
            rebind_results.append(bound.rebind_implementation(new, new_item, lifecycle))
        except BaseException as exc:  # noqa: BLE001 - expected linearization rejection
            rebind_results.append(exc)
        finally:
            rebind_done.set()

    review_thread = threading.Thread(target=record_review)
    review_thread.start()
    assert review_entered.wait(5)
    rebind_thread = threading.Thread(target=rebind)
    rebind_thread.start()
    assert rebind_lock_attempted.wait(5)
    # The review writer owns the item lock while its packet write is paused;
    # rebind must not publish a context transition through that half-written
    # boundary.
    assert not rebind_done.wait(0.1)
    release_review.set()
    review_thread.join(timeout=5)
    rebind_thread.join(timeout=5)
    assert not review_thread.is_alive()
    assert not rebind_thread.is_alive()
    assert review_errors == []
    assert len(rebind_results) == 1
    assert isinstance(rebind_results[0], ValueError)
    assert bound.manifest_path.read_bytes() == manifest_before
    assert not (bound.manifest_path.parent / "analysis_context_transitions.jsonl").exists()
    final_item = ItemWorkspace.load(new, "Q-001")
    assert final_item.business_review_path.is_file()
    assert final_item.state["review"]["status"] == "reviewed"


def test_terminal_lifecycle_mutation_precedes_rebind_and_rejects(tmp_path: Path) -> None:
    _old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    state = lifecycle.to_dict()
    state["status"] = "complete"
    lifecycle._write_state(state)
    new_item = ItemWorkspace.load(new, "Q-001")
    with pytest.raises(ValueError, match="terminal lifecycle"):
        bound.rebind_implementation(new, new_item, lifecycle)
    assert not (bound.manifest_path.parent / "analysis_context_transition_intent.json").exists()


@pytest.mark.parametrize("stage", ("after_intent", "after_audit", "after_manifest", "after_state"))
def test_rebind_crash_boundaries_converge_on_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    new_item = ItemWorkspace.load(new, "Q-001")
    monkeypatch.setattr(analysis_module, "_transition_failpoint", lambda actual: (_ for _ in ()).throw(RuntimeError(actual)) if actual == stage else None)
    with pytest.raises(RuntimeError, match=stage):
        bound.rebind_implementation(new, new_item, lifecycle)
    monkeypatch.setattr(analysis_module, "_transition_failpoint", lambda _actual: None)
    loaded = load_bound_analysis_context(new, path=bound.manifest_path, item_workspace=new_item)
    assert loaded.context.core_version == "0.3.4"
    assert not (bound.manifest_path.parent / "analysis_context_transition_intent.json").exists()
    assert loaded.source_catalog.path == bound.source_catalog.path


@pytest.mark.parametrize("tamper", ("delete", "truncate", "rewrite"))
def test_rebind_audit_tamper_fails_closed(tmp_path: Path, tamper: str) -> None:
    _old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    new_item = ItemWorkspace.load(new, "Q-001")
    rebound = bound.rebind_implementation(new, new_item, lifecycle)
    audit_path = rebound.manifest_path.parent / "analysis_context_transitions.jsonl"
    if tamper == "delete":
        audit_path.unlink()
    elif tamper == "truncate":
        audit_path.write_bytes(b"")
    else:
        audit_path.write_text(audit_path.read_text(encoding="utf-8").replace("T-032-033", "TAMPERED"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_bound_analysis_context(new, path=rebound.manifest_path, item_workspace=new_item)


@pytest.mark.parametrize("case", ("missing", "gap", "earliest", "review", "accepted"))
def test_rebind_rejects_invalid_chain_or_item_state(tmp_path: Path, case: str) -> None:
    old, new, _old_item, bound, lifecycle = _fixture(tmp_path)
    new_item = ItemWorkspace.load(new, "Q-001")
    if case == "missing":
        lifecycle_path = old.run_root / "implementation_transitions.jsonl"
        lifecycle_path.unlink()
    elif case == "gap":
        path = old.run_root / "implementation_transitions.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        second = json.loads(lines[1])
        second["old_sha"] = "1" * 40
        second["record_hash"] = "0" * 64
        path.write_text(lines[0] + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    elif case == "earliest":
        path = old.run_root / "implementation_transitions.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        unsigned = {key: value for key, value in first.items() if key != "record_hash"}
        unsigned["earliest_affected_item"] = "Q-002"
        import hashlib

        unsigned["record_hash"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path.write_text(json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n" + lines[1] + "\n", encoding="utf-8")
    elif case == "review":
        new_item.write_draft({"answer": "reviewed"})
        new_item.record_review("accept", reviewer_ref="reviewer")
    else:
        new_item.accepted_root.mkdir()
    with pytest.raises(ValueError):
        bound.rebind_implementation(new, new_item, lifecycle)
