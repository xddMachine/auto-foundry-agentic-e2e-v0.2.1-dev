from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp
import os
from pathlib import Path
from queue import Empty
from typing import Any
import zipfile

import pytest

from auto_foundry_core import (
    AllowedRootError,
    AnalystWorkspace,
    BoundAnalysisContext,
    DataAssetRef,
    ItemWorkspace,
    PreparedAssetRegistry,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementRunExtension,
    RequirementSupervisorWorkspace,
    RunContext,
    RunLifecycle,
    SemanticSnapshotStore,
)
from auto_foundry_core.integration import IntegrationSession
import auto_foundry_core.run_extension as run_extension_module


def _record(item_id: str, text: str | None = None) -> RequirementRecord:
    return RequirementRecord(
        requirement_id=item_id,
        original_text=text or f"Investigate {item_id}.",
        business_objective=f"Support {item_id}",
        expected_analytical_outputs=(f"output-{item_id}",),
    )


def _terminal_item(context: RunContext, item_id: str, text: str) -> ItemWorkspace:
    item = ItemWorkspace.create(context, item_id, mode="requirement", original_text=text)
    item.write_draft({"answer": item_id})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("draft.json",))
    item.mark_integration_committed("a" * 64, "integration/manifest.json")
    return item


def _fixture(tmp_path: Path) -> tuple[RunContext, RequirementExecutionPlan, bytes, bytes]:
    context = RunContext("RUN-EXTENSION", tmp_path / "run")
    first = _record("REQ-01")
    lifecycle = RunLifecycle.create(context, (first.requirement_id,), mode="requirement")
    _terminal_item(context, first.requirement_id, first.original_text)
    plan = RequirementExecutionPlan(
        input_records=(first,),
        groups=(RequirementExecutionGroup((first.requirement_id,), "Original route."),),
        planner_ref="planner",
        portfolio_strategy="original strategy",
        revision=1,
    )
    RequirementSupervisorWorkspace(context).save(plan)
    lifecycle.reconcile_from_run(product_terminal_status="complete")
    lifecycle = RunLifecycle.load(context)
    if lifecycle.state != "complete":
        # A terminal fixture is intentionally created through the program API;
        # this final state write only supplies the product barrier for the
        # focused generation tests.
        state = lifecycle.to_dict()
        state["status"] = "complete"
        lifecycle._write_state(state)  # noqa: SLF001 - fixture barrier
    return (
        context,
        plan,
        (context.run_root / "run_state.json").read_bytes(),
        (context.run_root / "requirement_supervisor_plan.json").read_bytes(),
    )


def _generation_plan(parent: RequirementExecutionPlan) -> RequirementExecutionPlan:
    second = _record("REQ-02")
    return RequirementExecutionPlan(
        input_records=parent.input_records + (second,),
        groups=(RequirementExecutionGroup(("REQ-01", "REQ-02"), "Original route."),),
        planner_ref=parent.planner_ref,
        portfolio_strategy=parent.portfolio_strategy,
        revision=2,
    )


def _file_tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _process_append_worker(run_root: str, gate: Any, result_queue: Any) -> None:
    context = RunContext("RUN-EXTENSION", Path(run_root))
    try:
        gate.wait(timeout=30)
        parent = RequirementExecutionPlan(
            input_records=(_record("REQ-01"),),
            groups=(RequirementExecutionGroup(("REQ-01",), "Original route."),),
            planner_ref="planner",
            portfolio_strategy="original strategy",
            revision=1,
        )
        extension = RequirementRunExtension.append(
            context,
            (_record("REQ-02"),),
            plan=_generation_plan(parent),
        )
        result_queue.put(("ok", extension.generation_id, os.getpid()))
    except Exception as exc:  # pragma: no cover - asserted by the parent test
        result_queue.put(("error", type(exc).__name__, str(exc), os.getpid()))


def _process_conflicting_append_worker(run_root: str, gate: Any, result_queue: Any) -> None:
    context = RunContext("RUN-EXTENSION", Path(run_root))
    try:
        gate.wait(timeout=30)
        parent = RequirementExecutionPlan(
            input_records=(_record("REQ-01"),),
            groups=(RequirementExecutionGroup(("REQ-01",), "Original route."),),
            planner_ref="planner",
            portfolio_strategy="original strategy",
            revision=1,
        )
        conflicting = RequirementExecutionPlan(
            input_records=parent.input_records + (_record("REQ-02"),),
            groups=(RequirementExecutionGroup(("REQ-01", "REQ-02"), "Original route."),),
            planner_ref="conflicting-planner",
            portfolio_strategy="conflicting strategy",
            revision=2,
        )
        extension = RequirementRunExtension.append(context, (_record("REQ-02"),), plan=conflicting)
        result_queue.put(("ok", extension.generation_id, os.getpid()))
    except Exception as exc:  # pragma: no cover - asserted by the parent test
        result_queue.put(("error", type(exc).__name__, str(exc), os.getpid()))


def _process_replan_worker(run_root: str, gate: Any, result_queue: Any) -> None:
    context = RunContext("RUN-EXTENSION", Path(run_root))
    try:
        gate.wait(timeout=30)
        replanned = RequirementExecutionPlan(
            input_records=(_record("REQ-01"),),
            groups=(RequirementExecutionGroup(("REQ-01",), "Replanned route."),),
            planner_ref="planner-replan",
            portfolio_strategy="replanned strategy",
            revision=2,
        )
        saved = RequirementSupervisorWorkspace(context).save(replanned)
        result_queue.put(("ok", saved.revision, os.getpid()))
    except Exception as exc:  # pragma: no cover - asserted by the parent test
        result_queue.put(("error", type(exc).__name__, str(exc), os.getpid()))


def _multiprocessing_results(run_root: Path, workers: list[Any]) -> list[tuple[Any, ...]]:
    if "fork" not in mp.get_all_start_methods():
        pytest.skip("process-concurrency tests require the fork multiprocessing start method")
    context = mp.get_context("fork")
    gate = context.Barrier(len(workers))
    result_queue = context.Queue()
    processes = [context.Process(target=worker, args=(str(run_root), gate, result_queue)) for worker in workers]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    stuck = [process for process in processes if process.is_alive()]
    if stuck:
        for process in stuck:
            process.terminate()
            process.join(timeout=5)
        pytest.fail("multiprocessing admission worker did not finish")
    results: list[tuple[Any, ...]] = []
    try:
        for _ in processes:
            results.append(tuple(result_queue.get(timeout=5)))
    except Empty as exc:
        pytest.fail(f"multiprocessing admission worker exited without a result: {exc}")
    finally:
        result_queue.close()
        result_queue.join_thread()
    assert all(process.exitcode == 0 for process in processes)
    return results


def test_append_publishes_cumulative_generation_without_mutating_generation_one(tmp_path: Path) -> None:
    context, parent, state_before, plan_before = _fixture(tmp_path)
    generation_plan = _generation_plan(parent)

    extension = RequirementRunExtension.append(
        context,
        (_record("REQ-02"),),
        plan=generation_plan,
        expected_parent_state_hash=RunLifecycle.load(context).snapshot.manifest_hash,
        expected_parent_plan_hash=hashlib.sha256(plan_before).hexdigest(),
    )

    active = RunLifecycle.load(context)
    assert extension.generation_id == "G-0002"
    assert extension.lifecycle.context.run_id == context.run_id
    assert extension.lifecycle.context.run_root == context.run_root
    assert active.generation_id == "G-0002"
    assert active.item_ids == ("REQ-01", "REQ-02")
    assert active.state == "running"
    assert (context.run_root / "run_state.json").read_bytes() == state_before
    assert (context.run_root / "requirement_supervisor_plan.json").read_bytes() == plan_before
    assert json.loads((context.run_root / "active_generation.json").read_text())[
        "generation_id"
    ] == "G-0002"
    assert RequirementSupervisorWorkspace(context).load() == generation_plan


def test_append_exact_retry_is_idempotent_and_conflict_fails_closed(tmp_path: Path) -> None:
    context, parent, *_ = _fixture(tmp_path)
    generation_plan = _generation_plan(parent)
    first = RequirementRunExtension.append(context, (_record("REQ-02"),), plan=generation_plan)
    retry = RequirementRunExtension.append(context, (_record("REQ-02"),), plan=generation_plan)
    assert retry.generation_id == first.generation_id
    assert (context.run_root / "extensions/G-0002/generation_manifest.json").is_file()

    conflicting = RequirementExecutionPlan(
        input_records=generation_plan.input_records,
        groups=(RequirementExecutionGroup(("REQ-01", "REQ-02"), "Changed route."),),
        planner_ref="planner",
        portfolio_strategy="changed",
        revision=2,
    )
    with pytest.raises(ValueError, match="conflicts|route"):
        RequirementRunExtension.append(context, (_record("REQ-02"),), plan=conflicting)


def test_append_accepts_implicit_route_and_rejects_duplicate_or_stale_parent_hash(tmp_path: Path) -> None:
    context, parent, *_ = _fixture(tmp_path)
    extension = RequirementRunExtension.append(context, (_record("REQ-02"),))
    assert extension.cumulative_item_ids == ("REQ-01", "REQ-02")

    duplicate_context, duplicate_parent, *_ = _fixture(tmp_path / "duplicate")
    with pytest.raises(ValueError, match="already exist"):
        RequirementRunExtension.append(
            duplicate_context,
            (_record("REQ-01"),),
            plan=duplicate_parent,
        )

    stale_context, stale_parent, *_ = _fixture(tmp_path / "stale")
    with pytest.raises(ValueError, match="stale"):
        RequirementRunExtension.append(
            stale_context,
            (_record("REQ-02"),),
            plan=_generation_plan(stale_parent),
            expected_parent_state_hash="0" * 64,
        )


def test_generation_pointer_tamper_and_symlink_fail_closed(tmp_path: Path) -> None:
    context, parent, *_ = _fixture(tmp_path)
    RequirementRunExtension.append(context, (_record("REQ-02"),), plan=_generation_plan(parent))
    pointer_path = context.run_root / "active_generation.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["generation_id"] = "G-0003"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(ValueError, match="pointer|hash|generation"):
        RunLifecycle.load(context)


@pytest.mark.parametrize(
    "alias_kind",
    ("extensions", "generation_dir", "state", "plan", "manifest", "pointer", "root_state", "root_plan"),
)
def test_symlink_aliases_are_rejected_before_any_outside_write(tmp_path: Path, alias_kind: str) -> None:
    context, parent, *_ = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = b"outside-target-must-remain-byte-identical"
    target = outside / f"{alias_kind}-target"
    expected_target_bytes = sentinel

    if alias_kind == "extensions":
        target.mkdir()
        (target / "sentinel").write_bytes(sentinel)
        (context.run_root / "extensions").symlink_to(target, target_is_directory=True)
        target_bytes_path = target / "sentinel"
    elif alias_kind == "generation_dir":
        (context.run_root / "extensions").mkdir()
        target.mkdir()
        (target / "sentinel").write_bytes(sentinel)
        (context.run_root / "extensions/G-0002").symlink_to(target, target_is_directory=True)
        target_bytes_path = target / "sentinel"
    elif alias_kind in {"state", "plan", "manifest"}:
        generation_root = context.run_root / "extensions/G-0002"
        generation_root.mkdir(parents=True)
        target.write_bytes(sentinel)
        target_path = generation_root / {
            "state": "run_state.json",
            "plan": "requirement_supervisor_plan.json",
            "manifest": "generation_manifest.json",
        }[alias_kind]
        target_path.symlink_to(target)
        target_bytes_path = target
    elif alias_kind == "pointer":
        target.write_bytes(sentinel)
        (context.run_root / "active_generation.json").symlink_to(target)
        target_bytes_path = target
    elif alias_kind == "root_state":
        root_state = context.run_root / "run_state.json"
        expected_target_bytes = root_state.read_bytes()
        target.write_bytes(expected_target_bytes)
        root_state.unlink()
        root_state.symlink_to(target)
        target_bytes_path = target
    else:
        root_plan = context.run_root / "requirement_supervisor_plan.json"
        expected_target_bytes = root_plan.read_bytes()
        target.write_bytes(expected_target_bytes)
        root_plan.unlink()
        root_plan.symlink_to(target)
        target_bytes_path = target

    with pytest.raises((AllowedRootError, ValueError), match="symlink|generation|pointer|path|root|plan|state"):
        RequirementRunExtension.append(context, (_record("REQ-02"),), plan=_generation_plan(parent))
    assert target_bytes_path.read_bytes() == expected_target_bytes
    pointer_path = context.run_root / "active_generation.json"
    assert not pointer_path.exists() or pointer_path.is_symlink()


def test_admission_is_process_lock_serialized_and_pointer_failpoint_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context, parent, state_before, plan_before = _fixture(tmp_path)
    generation_plan = _generation_plan(parent)
    original_atomic_write = run_extension_module._atomic_write_json

    def fail_pointer(path: Path, value: object) -> None:
        if Path(path).name == "active_generation.json":
            raise RuntimeError("pointer failpoint")
        original_atomic_write(path, value)

    monkeypatch.setattr(run_extension_module, "_atomic_write_json", fail_pointer)
    with pytest.raises(RuntimeError, match="failpoint"):
        RequirementRunExtension.append(context, (_record("REQ-02"),), plan=generation_plan)
    assert not (context.run_root / "active_generation.json").exists()
    assert (context.run_root / "run_state.json").read_bytes() == state_before
    assert (context.run_root / "requirement_supervisor_plan.json").read_bytes() == plan_before

    monkeypatch.setattr(run_extension_module, "_atomic_write_json", original_atomic_write)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _index: RequirementRunExtension.append(
                    context,
                    (_record("REQ-02"),),
                    plan=generation_plan,
                ).generation_id,
                range(4),
            )
        )
    assert results == ["G-0002"] * 4
    assert RunLifecycle.load(context).item_ids == ("REQ-01", "REQ-02")


def test_generation_directories_are_fsynced_before_pointer_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent, *_ = _fixture(tmp_path)
    events: list[tuple[str, str]] = []
    original_fsync = run_extension_module._fsync_directory
    original_publish = RequirementRunExtension._publish_pointer

    def record_fsync(path: Path) -> None:
        relative = Path(path).relative_to(context.run_root).as_posix()
        events.append(("fsync", relative))
        original_fsync(path)

    def record_publish(publish_context: RunContext, metadata: object) -> None:
        events.append(("publish", getattr(metadata, "generation_id", "")))
        original_publish(publish_context, metadata)  # type: ignore[arg-type]

    monkeypatch.setattr(run_extension_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(RequirementRunExtension, "_publish_pointer", staticmethod(record_publish))

    RequirementRunExtension.append(context, (_record("REQ-02"),), plan=_generation_plan(parent))

    publish_index = next(index for index, event in enumerate(events) if event == ("publish", "G-0002"))
    assert any(
        index < publish_index and event == ("fsync", "extensions")
        for index, event in enumerate(events)
    )
    assert any(
        index < publish_index and event == ("fsync", "extensions/G-0002")
        for index, event in enumerate(events)
    )
    assert any(index < publish_index and event == ("fsync", ".") for index, event in enumerate(events))


def test_multiprocess_identical_append_is_idempotent(tmp_path: Path) -> None:
    context, parent, *_ = _fixture(tmp_path)
    results = _multiprocessing_results(
        context.run_root,
        [_process_append_worker, _process_append_worker, _process_append_worker, _process_append_worker],
    )
    assert all(result[0] == "ok" for result in results)
    assert {result[1] for result in results} == {"G-0002"}
    assert len({result[2] for result in results}) == 4
    assert RunLifecycle.load(context).item_ids == ("REQ-01", "REQ-02")


def test_multiprocess_conflicting_append_has_one_winner_and_one_fail_closed_retry(tmp_path: Path) -> None:
    context, parent, *_ = _fixture(tmp_path)
    results = _multiprocessing_results(
        context.run_root,
        [_process_append_worker, _process_conflicting_append_worker],
    )
    winners = [result for result in results if result[0] == "ok"]
    failures = [result for result in results if result[0] == "error"]
    assert len(winners) == 1
    assert len(failures) == 1
    assert failures[0][1] == "ValueError"
    assert "conflict" in failures[0][2]
    assert RunLifecycle.load(context).generation_id == "G-0002"


def test_multiprocess_replan_and_append_bind_one_consistent_parent_plan_hash(tmp_path: Path) -> None:
    context, parent, root_state_before, root_plan_before = _fixture(tmp_path)
    results = _multiprocessing_results(
        context.run_root,
        [_process_append_worker, _process_replan_worker],
    )
    append_results = [result for result in results if result[0] == "ok" and result[1] == "G-0002"]
    assert append_results or any(result[0] == "error" for result in results)

    # If append lost a process-level race or a failpoint interrupted it, the
    # normal exact retry must converge. Whichever writer won, the generation
    # parent hash must equal the plan bytes that were actually authoritative.
    if not append_results:
        current_parent = RequirementSupervisorWorkspace(context).load()
        current_group = current_parent.groups[0]
        retry_plan = RequirementExecutionPlan(
            input_records=current_parent.input_records + (_record("REQ-02"),),
            groups=(
                RequirementExecutionGroup(
                    current_group.requirement_ids + ("REQ-02",),
                    current_group.rationale,
                    current_group.shared_analysis_intent,
                    current_group.suggested_specialists,
                ),
            ),
            planner_ref=current_parent.planner_ref,
            portfolio_strategy=current_parent.portfolio_strategy,
            revision=current_parent.revision + 1,
        )
        RequirementRunExtension.append(context, (_record("REQ-02"),), plan=retry_plan)
    active = RunLifecycle.load(context)
    assert active.generation_id == "G-0002"
    manifest = json.loads((context.run_root / "extensions/G-0002/generation_manifest.json").read_text())
    root_plan_hash = hashlib.sha256((context.run_root / "requirement_supervisor_plan.json").read_bytes()).hexdigest()
    assert manifest["parent_plan_hash"] == root_plan_hash
    assert (context.run_root / "run_state.json").read_bytes() == root_state_before
    # The root plan may be the original bytes or the successful live replan,
    # but it must never be a partially written or mixed snapshot.
    assert (context.run_root / "requirement_supervisor_plan.json").read_bytes() in {
        root_plan_before,
        json.dumps(
            RequirementExecutionPlan(
                input_records=parent.input_records,
                groups=(RequirementExecutionGroup(("REQ-01",), "Replanned route."),),
                planner_ref="planner-replan",
                portfolio_strategy="replanned strategy",
                revision=2,
            ).to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n",
    }


def test_terminal_generations_append_in_order_and_preserve_prior_bytes(tmp_path: Path) -> None:
    context, parent, root_state_before, root_plan_before = _fixture(tmp_path)
    g2_plan = _generation_plan(parent)
    g2 = RequirementRunExtension.append(context, (_record("REQ-02"),), plan=g2_plan)
    assert g2.lifecycle.context.run_id == context.run_id
    assert g2.lifecycle.context.run_root == context.run_root

    g2_manifest_path = context.run_root / "extensions/G-0002/generation_manifest.json"
    g2_state_path = context.run_root / "extensions/G-0002/run_state.json"
    g2_plan_path = context.run_root / "extensions/G-0002/requirement_supervisor_plan.json"

    _terminal_item(context, "REQ-02", "Investigate REQ-02.")
    g2_terminal = RunLifecycle.load(context)
    g2_terminal.reconcile_from_run(product_terminal_status="complete")
    g2_terminal = RunLifecycle.load(context)
    assert g2_terminal.generation_id == "G-0002"
    assert g2_terminal.state == "complete"
    g2_terminal_hash = g2_terminal.snapshot.manifest_hash
    g2_current_plan_hash = hashlib.sha256(g2_plan_path.read_bytes()).hexdigest()

    # A terminal G2 product must not satisfy the later G3 product gate.
    g2_product = context.resolve_product_path("generations/G-0002/product_manifest.json")
    g2_product.parent.mkdir(parents=True, exist_ok=True)
    g2_product.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    before_g3 = {
        "root_state": root_state_before,
        "root_plan": root_plan_before,
        "g2_manifest": g2_manifest_path.read_bytes(),
        "g2_state": g2_state_path.read_bytes(),
        "g2_plan": g2_plan_path.read_bytes(),
        "g1_item": _file_tree_bytes(context.run_root / "requirements/REQ-01"),
        "g2_item": _file_tree_bytes(context.run_root / "requirements/REQ-02"),
    }

    third = _record("REQ-03")
    g3_plan = RequirementExecutionPlan(
        input_records=g2_plan.input_records + (third,),
        groups=(
            RequirementExecutionGroup(("REQ-01", "REQ-02"), "Original route."),
            RequirementExecutionGroup(("REQ-03",), "Added route."),
        ),
        planner_ref=g2_plan.planner_ref,
        portfolio_strategy=g2_plan.portfolio_strategy,
        revision=3,
    )
    g3 = RequirementRunExtension.append(context, (third,), plan=g3_plan)
    active = RunLifecycle.load(context)
    assert g3.generation_id == "G-0003"
    assert active.generation_id == "G-0003"
    assert active.generation_ordinal == 3
    assert active.item_ids == ("REQ-01", "REQ-02", "REQ-03")
    assert g3.lifecycle.parent_generation_id == "G-0002"
    assert g3.lifecycle.parent_state_hash == g2_terminal_hash
    assert g3.lifecycle.parent_plan_hash == g2_current_plan_hash

    _terminal_item(context, "REQ-03", "Investigate REQ-03.")
    active = RunLifecycle.load(context)
    active.reconcile_from_run(product_terminal_status="complete")
    assert RunLifecycle.load(context).state == "complete"
    actions = RequirementSupervisorWorkspace(context).next_actions()
    product_actions = [action for action in actions if action.action == "build_final_product"]
    assert len(product_actions) == 1
    assert product_actions[0].metadata["generation_id"] == "G-0003"
    assert product_actions[0].metadata["product_manifest_ref"] == "products/generations/G-0003/product_manifest.json"

    assert (context.run_root / "run_state.json").read_bytes() == before_g3["root_state"]
    assert (context.run_root / "requirement_supervisor_plan.json").read_bytes() == before_g3["root_plan"]
    assert g2_manifest_path.read_bytes() == before_g3["g2_manifest"]
    assert g2_state_path.read_bytes() == before_g3["g2_state"]
    assert g2_plan_path.read_bytes() == before_g3["g2_plan"]
    assert _file_tree_bytes(context.run_root / "requirements/REQ-01") == before_g3["g1_item"]
    assert _file_tree_bytes(context.run_root / "requirements/REQ-02") == before_g3["g2_item"]


def test_extension_requirement_context_reuses_prior_committed_lem_and_skips_pending_items(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("orders.csv", "id,value\nA,1\n")

    context = RunContext("RUN-EXTENSION-SEMANTICS", tmp_path / "run", (inputs,))
    lifecycle = RunLifecycle.create(context, ("REQ-01",), mode="requirement")
    first = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="first")
    first.write_plan({"item_id": first.item_id, "offline": True})
    first_bound = BoundAnalysisContext.create_for_requirement(
        context, DataAssetRef.from_path(archive), first, lifecycle
    )
    first_owner = AnalystWorkspace(first_bound, owner_ref="owner-REQ-01")
    prepared = first_owner.prepare_data(
        "prior-asset",
        [{"id": "A", "value": 1}],
        scope="reusable",
        transformations=("bounded fixture",),
    )
    first_owner.record_analytical_relationship(
        relationship_id="prior-rel",
        source_id="orders",
        target_id="orders",
        cardinality="one_to_one",
        join_keys=({"source_field": "id", "target_field": "id"},),
        matched_pairs=1,
        source_population=1,
        target_population=1,
        matched_source_count=1,
        matched_target_count=1,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="bounded fixture",
        limitations=("synthetic",),
        evidence_refs=("work/analytical_relationships.jsonl",),
        publishable=True,
    )
    first_owner.submit_answer("Prior committed answer.")
    first.record_review("accept", reviewer_ref="reviewer")
    first.accept(accepted_refs=("work/plan.json", "work/analytical_relationships.jsonl"))

    session = IntegrationSession.create(
        context,
        first,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="integration-REQ-01",
    )
    session.add_ontology_item(
        {"item_id": "orders", "item_type": "entity", "label": "Orders"},
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        {
            "relationship_id": "prior-rel",
            "analysis_relationship_id": "prior-rel",
            "source_id": "orders",
            "target_id": "orders",
            "cardinality": "one_to_one",
            "join_keys": [{"source_field": "id", "target_field": "id"}],
            "matched_pairs": 1,
            "source_population": 1,
            "target_population": 1,
            "matched_source_count": 1,
            "matched_target_count": 1,
            "source_coverage": 1.0,
            "target_coverage": 1.0,
            "date_authority": "bounded fixture",
            "as_of": None,
            "limitations": ["synthetic"],
            "evidence_refs": ["work/analytical_relationships.jsonl"],
        },
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl",),
    )
    session.register_prepared_asset(prepared, evidence_refs=("work/plan.json",))
    assert session.validate().valid
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()

    first_record = _record("REQ-01", "first")
    parent_plan = RequirementExecutionPlan(
        input_records=(first_record,),
        groups=(RequirementExecutionGroup(("REQ-01",), "Original route."),),
        planner_ref="planner",
        portfolio_strategy="original strategy",
        revision=1,
    )
    RequirementSupervisorWorkspace(context).save(parent_plan)
    lifecycle.reconcile_from_run(product_terminal_status="complete")
    assert RunLifecycle.load(context).state == "complete"

    second = _record("REQ-02", "second")
    pending = _record("REQ-03", "pending")
    extension_plan = RequirementExecutionPlan(
        input_records=(first_record, second, pending),
        groups=(
            RequirementExecutionGroup(("REQ-01",), "Original route."),
            RequirementExecutionGroup(("REQ-02", "REQ-03"), "Added route."),
        ),
        planner_ref="planner",
        portfolio_strategy="original strategy",
        revision=2,
    )
    RequirementRunExtension.append(context, (second, pending), plan=extension_plan)
    target = ItemWorkspace.load(context, "REQ-02", mode="requirement")
    target_bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(archive),
        target,
        RunLifecycle.load(context),
    )
    assert target_bound.semantic_snapshot_ref is not None
    snapshot_manifest = SemanticSnapshotStore.manifest(context, target_bound.semantic_snapshot_ref)
    assert tuple(snapshot_manifest["projection"]["source_item_ids"]) == ("REQ-01",)
    ontology = SemanticSnapshotStore.records(context, target_bound.semantic_snapshot_ref, "ontology")["ontology"]
    relationships = SemanticSnapshotStore.records(context, target_bound.semantic_snapshot_ref, "relationships")["relationships"]
    prepared_assets = SemanticSnapshotStore.records(context, target_bound.semantic_snapshot_ref, "prepared_assets")["prepared_assets"]
    assert {record["item_id"] for record in ontology} >= {"orders", "prior-rel"}
    assert {record["relationship_id"] for record in relationships} == {"prior-rel"}
    assert {record["prepared_asset_id"] for record in prepared_assets} == {"prior-asset"}
    assert "REQ-03" not in snapshot_manifest["projection"]["source_item_ids"]


def test_active_generation_replan_is_loadable_and_next_append_binds_current_plan_hash(tmp_path: Path) -> None:
    context, parent, *_ = _fixture(tmp_path)
    g2_plan = _generation_plan(parent)
    g2 = RequirementRunExtension.append(context, (_record("REQ-02"),), plan=g2_plan)
    admission_plan_hash = g2.metadata.plan_hash

    replanned = RequirementExecutionPlan(
        input_records=g2_plan.input_records,
        groups=(RequirementExecutionGroup(("REQ-01", "REQ-02"), "Replanned route."),),
        planner_ref="planner-replan",
        portfolio_strategy="replanned strategy",
        revision=3,
    )
    RequirementSupervisorWorkspace(context).save(replanned)
    assert RunLifecycle.load(context).generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_metadata.plan_hash == admission_plan_hash
    assert RequirementSupervisorWorkspace(context).load() == replanned
    current_plan_path = RunLifecycle.load(context).plan_path
    current_plan_hash = hashlib.sha256(current_plan_path.read_bytes()).hexdigest()
    assert current_plan_hash != admission_plan_hash

    _terminal_item(context, "REQ-02", "Investigate REQ-02.")
    RunLifecycle.load(context).reconcile_from_run(product_terminal_status="complete")
    third = _record("REQ-03")
    g3_plan = RequirementExecutionPlan(
        input_records=replanned.input_records + (third,),
        groups=(
            RequirementExecutionGroup(("REQ-01", "REQ-02"), "Replanned route."),
            RequirementExecutionGroup(("REQ-03",), "Added route."),
        ),
        planner_ref="planner-replan",
        portfolio_strategy="replanned strategy",
        revision=4,
    )
    g3 = RequirementRunExtension.append(context, (third,), plan=g3_plan)
    assert g3.lifecycle.parent_generation_id == "G-0002"
    assert g3.lifecycle.parent_plan_hash == current_plan_hash
    manifest = json.loads((context.run_root / "extensions/G-0003/generation_manifest.json").read_text())
    assert manifest["parent_plan_hash"] == current_plan_hash


def test_planner_uses_generation_product_ref_in_final_product_action(tmp_path: Path) -> None:
    context, parent, *_ = _fixture(tmp_path)
    extension = RequirementRunExtension.append(context, (_record("REQ-02"),), plan=_generation_plan(parent))
    # The new item is not integrated, so this assertion focuses on the
    # generation metadata contract rather than product publication.
    assert extension.lifecycle.product_manifest_ref == "products/generations/G-0002/product_manifest.json"
    actions = RequirementSupervisorWorkspace(context).next_actions()
    assert all(action.metadata.get("generation_id") != "G-0001" for action in actions)
