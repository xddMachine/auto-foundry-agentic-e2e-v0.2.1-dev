from __future__ import annotations

import json
from pathlib import Path
import threading
import zipfile

import pytest

from auto_foundry_core import (
    DataRefreshNotSafeError,
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
import auto_foundry_core.run_extension as run_extension_module
from auto_foundry_core.workspace import AllowedRootError


def _record(item_id: str, text: str | None = None) -> RequirementRecord:
    return RequirementRecord(
        requirement_id=item_id,
        original_text=text or f"Investigate {item_id}.",
        business_objective=f"Support {item_id}",
        expected_analytical_outputs=(f"output-{item_id}",),
    )


def _archive(path: Path, value: str) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("orders.csv", f"id,value\n1,{value}\n")
    return path


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _fixture(
    tmp_path: Path,
    *,
    item_ids: tuple[str, ...] = ("REQ-01",),
) -> tuple[RunContext, RequirementExecutionPlan, object]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archive_one = _archive(inputs / "one.zip", "one")
    archive_two = _archive(inputs / "two.zip", "two")
    context = RunContext("RUN-DATA-REFRESH", tmp_path / "run", (inputs,))
    records = tuple(_record(item_id) for item_id in item_ids)
    lifecycle = RunLifecycle.create(context, item_ids, mode="requirement")
    for record in records:
        ItemWorkspace.create(context, record.requirement_id, mode="requirement", original_text=record.original_text)
    plan = RequirementExecutionPlan(
        input_records=records,
        groups=(RequirementExecutionGroup(item_ids, "Original route."),),
        planner_ref="planner",
        portfolio_strategy="strategy",
        revision=1,
    )
    RequirementSupervisorWorkspace(context).save(plan)
    state = lifecycle.to_dict()
    state["status"] = "complete"
    lifecycle._write_state(state)  # noqa: SLF001 - terminal fixture barrier
    store = DataRevisionStore(context)
    store.initialize_legacy(archive_one)
    revision = store.append(archive_two, expected_current_revision_id="D-0001")
    return context, plan, revision


def _begin_attempt_after_refresh(context: RunContext, item_id: str, started: threading.Event, finished: threading.Event, errors: list[BaseException]) -> None:
    started.set()
    try:
        workspace = ItemWorkspace.load(context, item_id, mode="requirement")
        workspace.begin_attempt(f"owner-{item_id}", "analysis")
    except BaseException as exc:  # noqa: BLE001 - worker evidence is asserted by the test
        errors.append(exc)
    finally:
        finished.set()


def test_refresh_data_reopens_and_archives_only_selected_head(tmp_path: Path) -> None:
    context, parent, revision = _fixture(tmp_path)
    old_head = _tree(context.run_root / "requirements/REQ-01")
    candidate = RequirementExecutionPlan(
        input_records=parent.input_records,
        groups=parent.groups,
        planner_ref=parent.planner_ref,
        portfolio_strategy=parent.portfolio_strategy,
        revision=parent.revision,
    )

    extension = RequirementRunExtension.refresh_data(
        context,
        candidate,
        data_revision=revision,
        reopened_item_ids=("REQ-01",),
    )

    assert extension.generation_id == "G-0002"
    assert extension.reopened_item_ids == ("REQ-01",)
    assert extension.data_revision_ref == "data_room/revisions/D-0002/revision_manifest.json"
    assert extension.data_revision_hash == revision.manifest_hash
    assert (context.run_root / "history/requirements/REQ-01/G-0002/item_state.json").is_file()
    assert (context.run_root / "requirements/REQ-01/item_state.json").read_text(encoding="utf-8")
    assert _tree(context.run_root / "history/requirements/REQ-01/G-0002") == old_head
    assert json.loads((context.run_root / "extensions/G-0002/generation_intent.json").read_text())[
        "request_hash"
    ] == extension.metadata.request_hash


def test_refresh_data_empty_reopen_publishes_data_only_generation_and_retries(tmp_path: Path) -> None:
    context, parent, revision = _fixture(tmp_path)
    first = RequirementRunExtension.refresh_data(context, parent, data_revision=revision)
    retry = RequirementRunExtension.refresh_data(context, parent, data_revision=revision)
    assert first.generation_id == retry.generation_id == "G-0002"
    assert first.reopened_item_ids == ()
    assert first.metadata.plan_hash == retry.metadata.plan_hash


def test_refresh_data_combines_added_and_updated_records(tmp_path: Path) -> None:
    context, parent, revision = _fixture(tmp_path)
    changed = _record("REQ-01", "Updated requirement text.")
    added = _record("REQ-02")
    candidate = RequirementExecutionPlan(
        input_records=(changed, added),
        groups=(RequirementExecutionGroup(("REQ-01", "REQ-02"), "Updated route."),),
        planner_ref=parent.planner_ref,
        portfolio_strategy=parent.portfolio_strategy,
        revision=2,
    )
    extension = RequirementRunExtension.refresh_data(
        context,
        candidate,
        data_revision=revision,
        reopened_item_ids=(),
    )
    assert extension.generation_id == "G-0002"
    assert extension.added_item_ids == ("REQ-02",)
    assert json.loads((context.run_root / "requirements/REQ-01/item_state.json").read_text())["original_text"] == changed.original_text
    assert json.loads((context.run_root / "requirements/REQ-02/item_state.json").read_text())["original_text"] == added.original_text
    assert (context.run_root / "history/requirements/REQ-01/G-0002/item_state.json").is_file()


def test_refresh_data_rejects_active_attempt_without_mutating_pointer(tmp_path: Path) -> None:
    context, parent, revision = _fixture(tmp_path)
    item = ItemWorkspace.load(context, "REQ-01", mode="requirement")
    attempt = item.begin_attempt("owner", "analysis")
    before = _tree(context.run_root)
    pointer_before = (context.run_root / "active_generation.json").read_bytes() if (context.run_root / "active_generation.json").exists() else None
    with pytest.raises(DataRefreshNotSafeError, match="stable current requirement"):
        RequirementRunExtension.refresh_data(context, parent, data_revision=revision, reopened_item_ids=("REQ-01",))
    assert pointer_before == ((context.run_root / "active_generation.json").read_bytes() if (context.run_root / "active_generation.json").exists() else None)
    assert _tree(context.run_root) == before
    item.finish_attempt(attempt.attempt_id, status="interrupted", error="host_interruption")


@pytest.mark.parametrize("failpoint", ("after_intent", "after_archive", "after_create", "before_pointer", "after_pointer"))
def test_refresh_data_failpoint_retry_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failpoint: str) -> None:
    context, parent, revision = _fixture(tmp_path)
    fired = False

    def fail(name: str) -> None:
        nonlocal fired
        if not fired and name == failpoint:
            fired = True
            raise RuntimeError(f"{failpoint} failpoint")

    monkeypatch.setattr(run_extension_module.RequirementRunExtension, "_refresh_failpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match="failpoint"):
        RequirementRunExtension.refresh_data(context, parent, data_revision=revision, reopened_item_ids=("REQ-01",))
    monkeypatch.setattr(run_extension_module.RequirementRunExtension, "_refresh_failpoint", staticmethod(lambda _name: None))
    recovered = RequirementRunExtension.refresh_data(
        context,
        parent,
        data_revision=revision,
        reopened_item_ids=("REQ-01",),
    )
    assert recovered.generation_id == "G-0002"
    assert RunLifecycle.load(context).generation_id == "G-0002"


def test_refresh_data_stable_transition_lock_spans_archive_and_fresh_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent, revision = _fixture(tmp_path, item_ids=("REQ-01", "REQ-02"))
    old_head = _tree(context.run_root / "requirements/REQ-01")
    paused = threading.Event()
    release = threading.Event()
    refresh_errors: list[BaseException] = []

    def pause_after_archive(name: str) -> None:
        if name == "after_archive:REQ-01":
            paused.set()
            if not release.wait(5):
                raise RuntimeError("refresh pause was not released")

    monkeypatch.setattr(
        run_extension_module.RequirementRunExtension,
        "_refresh_failpoint",
        staticmethod(pause_after_archive),
    )

    def refresh_worker() -> None:
        try:
            RequirementRunExtension.refresh_data(
                context,
                parent,
                data_revision=revision,
                reopened_item_ids=("REQ-01",),
            )
        except BaseException as exc:  # noqa: BLE001 - worker evidence is asserted by the test
            refresh_errors.append(exc)

    refresh_thread = threading.Thread(target=refresh_worker)
    refresh_thread.start()
    assert paused.wait(5)
    assert not (context.run_root / "requirements/REQ-01").exists()

    same_started = threading.Event()
    same_finished = threading.Event()
    same_errors: list[BaseException] = []
    same_thread = threading.Thread(
        target=_begin_attempt_after_refresh,
        args=(context, "REQ-01", same_started, same_finished, same_errors),
    )
    same_thread.start()
    assert same_started.wait(2)
    assert not same_finished.wait(0.25)

    other_started = threading.Event()
    other_finished = threading.Event()
    other_errors: list[BaseException] = []
    other_thread = threading.Thread(
        target=_begin_attempt_after_refresh,
        args=(context, "REQ-02", other_started, other_finished, other_errors),
    )
    other_thread.start()
    assert other_started.wait(2)
    assert other_finished.wait(2)
    assert other_errors == []

    release.set()
    refresh_thread.join(timeout=5)
    same_thread.join(timeout=5)
    assert not refresh_thread.is_alive()
    assert not same_thread.is_alive()
    assert refresh_errors == []
    assert same_errors == []
    assert (context.run_root / "requirements/REQ-01/item_state.json").is_file()
    assert _tree(context.run_root / "history/requirements/REQ-01/G-0002") == old_head


@pytest.mark.parametrize("malformation", ("namespace_file", "namespace_symlink", "lock_directory"))
def test_item_transition_lock_namespace_fails_closed(tmp_path: Path, malformation: str) -> None:
    context = RunContext("RUN-LOCK-NAMESPACE", tmp_path / "run")
    context.run_root.mkdir(parents=True)
    namespace = context.run_root / ".locks"
    if malformation == "namespace_file":
        namespace.write_text("not a directory", encoding="utf-8")
    elif malformation == "namespace_symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        namespace.symlink_to(outside, target_is_directory=True)
    else:
        lock_dir = namespace / "item_state" / "requirement" / "REQ-01"
        lock_dir.mkdir(parents=True)
        (lock_dir / ".item_state_transition.lock").mkdir()

    with pytest.raises(AllowedRootError):
        ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="REQ-01")
