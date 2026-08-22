from __future__ import annotations

from pathlib import Path
import json

from auto_foundry_core import (
    ItemWorkspace,
    LocalRunAutopilot,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementRunExtension,
    RequirementSupervisorWorkspace,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.cli import main as cli_main


def _record(item_id: str, text: str | None = None) -> RequirementRecord:
    return RequirementRecord(
        requirement_id=item_id,
        original_text=text or f"Investigate {item_id}.",
        business_objective=f"Support {item_id}",
        expected_analytical_outputs=(f"output-{item_id}",),
    )


def _plan(*records: RequirementRecord, revision: int = 1) -> RequirementExecutionPlan:
    return RequirementExecutionPlan(
        input_records=tuple(records),
        groups=tuple(
            RequirementExecutionGroup((record.requirement_id,), "Active portfolio requirement.")
            for record in records
        ),
        planner_ref="planner",
        portfolio_strategy="work the current portfolio",
        revision=revision,
    )


def _run(tmp_path: Path, *records: RequirementRecord) -> tuple[RunContext, RequirementSupervisorWorkspace]:
    context = RunContext("RUN-MUTABLE", tmp_path / "run")
    RunLifecycle.create(context, tuple(record.requirement_id for record in records), mode="requirement")
    for record in records:
        ItemWorkspace.create(
            context,
            record.requirement_id,
            mode="requirement",
            original_text=record.original_text,
        )
    workspace = RequirementSupervisorWorkspace(context)
    workspace.save(_plan(*records))
    return context, workspace


def test_requirement_can_be_added_while_run_is_active_without_route_boilerplate(tmp_path: Path) -> None:
    first = _record("REQ-01")
    context, workspace = _run(tmp_path, first)
    RunLifecycle.load(context).reopen()

    extension = RequirementRunExtension.append(context, _record("REQ-02"))

    assert extension.generation_id == "G-0002"
    assert RunLifecycle.load(context).item_ids == ("REQ-01", "REQ-02")
    assert tuple(record.requirement_id for record in workspace.load().input_records) == ("REQ-01", "REQ-02")
    assert ItemWorkspace.load(context, "REQ-02", mode="requirement").original_text == "Investigate REQ-02."


def test_requirement_can_be_added_directly_after_run_completion(tmp_path: Path) -> None:
    context, _ = _run(tmp_path, _record("REQ-01"))
    RunLifecycle.load(context)._advance("complete")  # noqa: SLF001 - model an existing completed run

    RequirementRunExtension.append(context, _record("REQ-17"))

    lifecycle = RunLifecycle.load(context)
    assert lifecycle.generation_id == "G-0002"
    assert lifecycle.item_ids == ("REQ-01", "REQ-17")
    assert lifecycle.state in {"initialized", "running"}


def test_portfolio_revision_adds_updates_and_removes_without_touching_history(tmp_path: Path) -> None:
    first = _record("REQ-01")
    second = _record("REQ-02")
    context, workspace = _run(tmp_path, first, second)
    old_first_state = (context.run_root / "requirements/REQ-01/item_state.json").read_bytes()
    old_second_state = (context.run_root / "requirements/REQ-02/item_state.json").read_bytes()
    RunLifecycle.load(context).reopen()

    updated_first = _record("REQ-01", "Use the revised business question.")
    third = _record("REQ-03")
    workspace.save(_plan(updated_first, third, revision=2))

    lifecycle = RunLifecycle.load(context)
    assert lifecycle.generation_id == "G-0002"
    assert lifecycle.item_ids == ("REQ-01", "REQ-03")
    assert not (context.run_root / "requirements/REQ-02").exists()
    assert (context.run_root / "history/requirements/REQ-01/G-0002/item_state.json").read_bytes() == old_first_state
    assert (context.run_root / "history/requirements/REQ-02/G-0002/item_state.json").read_bytes() == old_second_state
    assert ItemWorkspace.load(context, "REQ-01", mode="requirement").original_text == updated_first.original_text
    assert ItemWorkspace.load(context, "REQ-03", mode="requirement").original_text == third.original_text


def test_requirement_portfolio_can_become_empty_and_grow_again(tmp_path: Path) -> None:
    context, workspace = _run(tmp_path, _record("REQ-01"))
    empty = RequirementExecutionPlan(
        input_records=(),
        groups=(),
        planner_ref="planner",
        portfolio_strategy="temporarily empty",
        revision=2,
    )

    workspace.save(empty)
    assert RunLifecycle.load(context).item_ids == ()
    assert workspace.load().input_records == ()

    workspace.save(_plan(_record("REQ-17"), revision=3))
    assert RunLifecycle.load(context).item_ids == ("REQ-17",)
    assert ItemWorkspace.load(context, "REQ-17", mode="requirement").original_text == "Investigate REQ-17."


def test_portfolio_revision_while_paused_stays_paused_until_explicit_resume(tmp_path: Path) -> None:
    context, workspace = _run(tmp_path, _record("REQ-01"))
    RunLifecycle.load(context).pause("user is editing the portfolio")

    workspace.save(_plan(_record("REQ-01"), _record("REQ-02"), revision=2))

    paused = RunLifecycle.load(context)
    assert paused.state == "paused"
    assert paused.item_ids == ("REQ-01", "REQ-02")
    assert LocalRunAutopilot(context).tick().status == "paused"
    assert paused.resume().state == "running"


def test_run_can_pause_resume_and_reopen_from_any_terminal_state(tmp_path: Path) -> None:
    context, workspace = _run(tmp_path, _record("REQ-01"))
    lifecycle = RunLifecycle.load(context)
    lifecycle.reopen()

    paused = lifecycle.pause("user requested a stop")
    assert paused.state == "paused"
    assert workspace.runtime_snapshot().scheduler_status == "paused"
    assert workspace.next_actions() == ()

    assert lifecycle.resume().state == "running"
    lifecycle._advance("complete")  # noqa: SLF001 - exercise reopen from an existing terminal snapshot
    assert lifecycle.reopen().state == "running"


def test_autopilot_observes_pause_and_dispatches_new_work_after_resume(tmp_path: Path) -> None:
    context, _ = _run(tmp_path, _record("REQ-01"))
    lifecycle = RunLifecycle.load(context)
    lifecycle.reopen()
    autopilot = LocalRunAutopilot(context)

    lifecycle.pause("inspect dashboard")
    assert autopilot.tick().status == "paused"

    lifecycle.resume()
    ready = autopilot.tick()
    assert ready.status == "ready"
    assert any(action.subject_id == "REQ-01" for action in ready.actions)
    seen: list[str] = []
    dispatched = autopilot.tick(lambda action: seen.append(action.subject_id) or {"ok": True})
    assert dispatched.status == "dispatched"
    assert seen == [action.subject_id for action in dispatched.actions]


def test_cli_can_pause_add_update_remove_and_reopen(tmp_path: Path, capsys: object) -> None:
    context, _ = _run(tmp_path, _record("REQ-01"))
    common = ["--run-root", str(context.run_root), "--run-id", context.run_id]

    assert cli_main(["lifecycle", *common, "pause", "--reason", "user stop"]) == 0
    assert RunLifecycle.load(context).state == "paused"
    assert cli_main(["lifecycle", *common, "resume"]) == 0

    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(_record("REQ-02").to_dict()), encoding="utf-8")
    assert cli_main(["requirements", *common, "add", "--record", str(record_path)]) == 0

    record_path.write_text(
        json.dumps(_record("REQ-02", "Revised requirement text.").to_dict()),
        encoding="utf-8",
    )
    assert cli_main(["requirements", *common, "update", "--record", str(record_path)]) == 0
    assert ItemWorkspace.load(context, "REQ-02", mode="requirement").original_text == "Revised requirement text."

    assert cli_main(["requirements", *common, "remove", "REQ-01"]) == 0
    assert RunLifecycle.load(context).item_ids == ("REQ-02",)
    assert cli_main(["lifecycle", *common, "reopen"]) == 0
    assert RunLifecycle.load(context).state == "running"
