"""Small agent-facing CLI for catalog discovery and deterministic operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from .autopilot import LocalRunAutopilot
from .coordinator import RunCoordinator, _strict_json_loads
from .catalog import capability_catalog, get_capability, search_capabilities
from .contracts import OperationSpec
from .contracts import RequirementRecord
from .lifecycle import RunLifecycle
from .requirement_planning import RequirementExecutionGroup, RequirementExecutionPlan, RequirementSupervisorWorkspace
from .run_extension import RequirementRunExtension
from .runtime import CoreRuntime
from .workspace import RunContext


def _print(value: Any) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, default=str))


def _add_context_arguments(parser: argparse.ArgumentParser, *, inputs: bool = False) -> None:
    parser.add_argument("--run-root", required=True, help="current run directory")
    parser.add_argument("--run-id", help="optional simple run identifier (defaults to the run-root name)")
    if inputs:
        parser.add_argument("--input-root", action="append", default=[], help="source input root (repeatable)")


def _context(args: argparse.Namespace) -> RunContext:
    run_root = Path(args.run_root).expanduser().resolve(strict=False)
    return RunContext(
        run_id=args.run_id or run_root.name or "run",
        run_root=run_root,
        input_roots=tuple(getattr(args, "input_root", ())),
    )


def _read_json(path: str) -> Any:
    with Path(path).expanduser().resolve(strict=True).open("rb") as stream:
        return _strict_json_loads(stream.read())


def _coordinator_exit_code(status: Any, *, operation: str, max_steps: int | None = None) -> int:
    """Map coordinator outcomes to stable command-line exit codes."""

    value = status.status if hasattr(status, "status") else status.get("status") if isinstance(status, dict) else str(status)
    if value in {"complete", "complete_with_limits"}:
        return 0
    if value in {"blocked_rethink", "failed", "rethink"}:
        return 2
    if value == "waiting":
        return 3
    # Every non-terminal result is bounded work. The caller can resume it
    # after inspecting the JSON status.
    return 3


def _print_coordinator_status(status: Any, *, operation: str, max_steps: int | None = None) -> int:
    _print(status)
    return _coordinator_exit_code(status, operation=operation, max_steps=max_steps)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto_foundry_core", description="Deterministic local analytics core")
    sub = parser.add_subparsers(dest="command", required=True)
    catalog = sub.add_parser("catalog", help="discover deterministic capabilities")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sub.add_parser("list")
    search = catalog_sub.add_parser("search")
    search.add_argument("text")
    describe = catalog_sub.add_parser("describe")
    describe.add_argument("capability_id")
    run = sub.add_parser("run", help="execute one deterministic capability")
    run.add_argument("capability_id")
    run.add_argument("--spec", required=True, help="JSON operation specification path")
    _add_context_arguments(run, inputs=True)
    run.add_argument("--output", help="optional run-relative result directory (defaults to products)")

    lifecycle = sub.add_parser("lifecycle", help="inspect, pause, resume, or reopen a run")
    _add_context_arguments(lifecycle)
    lifecycle.add_argument("operation", choices=("status", "pause", "resume", "reopen"))
    lifecycle.add_argument("--reason", help="optional pause reason")

    requirements = sub.add_parser("requirements", help="edit the active requirement portfolio")
    _add_context_arguments(requirements)
    requirement_sub = requirements.add_subparsers(dest="requirements_operation", required=True)
    apply_plan = requirement_sub.add_parser("apply", help="apply an exact RequirementExecutionPlan JSON file")
    apply_plan.add_argument("--plan", required=True)
    add = requirement_sub.add_parser("add", help="add one RequirementRecord JSON file")
    add.add_argument("--record", required=True)
    update = requirement_sub.add_parser("update", help="replace one RequirementRecord by ID")
    update.add_argument("--record", required=True)
    remove = requirement_sub.add_parser("remove", help="remove one or more requirement IDs")
    remove.add_argument("requirement_id", nargs="+")

    autopilot = sub.add_parser("autopilot", help="continuously dispatch typed Planner actions")
    _add_context_arguments(autopilot)
    autopilot.add_argument("operation", choices=("once", "watch"))
    autopilot.add_argument("--interval", type=float, default=1.0, help="watch polling interval in seconds")
    autopilot.add_argument(
        "--dispatch-command",
        nargs=argparse.REMAINDER,
        required=True,
        help="local command; one PlannerAction JSON object is supplied on stdin",
    )

    coordinator = sub.add_parser("coordinator", help="durable autonomous run coordinator")
    _add_context_arguments(coordinator)
    coordinator.add_argument("operation", choices=("start", "step", "run", "resume", "status", "watchdog", "reopen"))
    coordinator.add_argument("--spec", help="JSON CoordinatorRunSpec path (used only for initial start)")
    coordinator.add_argument("--owner-id", help="stable coordinator owner identity")
    coordinator.add_argument("--max-steps", type=int, help="bound run operation for one invocation")
    coordinator.add_argument("--reason", help="reason for reopening a waiting or blocked run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "catalog":
        if args.catalog_command == "list":
            _print([descriptor.to_dict() for descriptor in capability_catalog()])
            return 0
        if args.catalog_command == "search":
            _print([descriptor.to_dict() for descriptor in search_capabilities(args.text)])
            return 0
        _print(get_capability(args.capability_id))
        return 0
    context = _context(args)

    if args.command == "lifecycle":
        lifecycle = RunLifecycle.load(context)
        if args.operation == "pause":
            snapshot = lifecycle.pause(args.reason)
        elif args.operation == "resume":
            snapshot = lifecycle.resume()
        elif args.operation == "reopen":
            snapshot = lifecycle.reopen()
        else:
            snapshot = lifecycle.snapshot
        current_lifecycle = RunLifecycle.load(context)
        _print({
            **current_lifecycle.to_dict(),
            "generation_id": current_lifecycle.generation_id,
        })
        return 0

    if args.command == "requirements":
        workspace = RequirementSupervisorWorkspace(context)
        operation = args.requirements_operation
        if operation == "apply":
            workspace.save(RequirementExecutionPlan.from_dict(_read_json(args.plan)))
        elif operation == "add":
            RequirementRunExtension.append(context, RequirementRecord.from_dict(_read_json(args.record)))
        else:
            current = workspace.load()
            records = list(current.input_records)
            if operation == "update":
                replacement = RequirementRecord.from_dict(_read_json(args.record))
                positions = [index for index, record in enumerate(records) if record.requirement_id == replacement.requirement_id]
                if not positions:
                    raise ValueError("updated requirement ID is not in the active portfolio")
                records[positions[0]] = replacement
                removed_ids: set[str] = set()
            else:
                removed_ids = set(args.requirement_id)
                known_ids = {record.requirement_id for record in records}
                missing = sorted(removed_ids - known_ids)
                if missing:
                    raise ValueError("removed requirement IDs are not in the active portfolio: " + ", ".join(missing))
                records = [record for record in records if record.requirement_id not in removed_ids]
            groups: list[RequirementExecutionGroup] = []
            for group in current.groups:
                remaining = tuple(item_id for item_id in group.requirement_ids if item_id not in removed_ids)
                if not remaining:
                    continue
                value = group.to_dict()
                value["requirement_ids"] = list(remaining)
                groups.append(RequirementExecutionGroup.from_dict(value))
            revised = RequirementExecutionPlan(
                input_records=tuple(records),
                groups=tuple(groups),
                planner_ref=current.planner_ref,
                portfolio_strategy=current.portfolio_strategy,
                revision=current.revision + 1,
            )
            workspace.save(revised)
        _print(workspace.load())
        return 0

    if args.command == "autopilot":
        autopilot = LocalRunAutopilot(context)

        def dispatch(action: Any) -> Any:
            completed = subprocess.run(
                args.dispatch_command,
                input=json.dumps(action.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode:
                raise RuntimeError(
                    f"dispatch command failed with exit code {completed.returncode}: {completed.stderr.strip()}"
                )
            output = completed.stdout.strip()
            if not output:
                return {"returncode": 0}
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return {"returncode": 0, "stdout": output}

        if args.operation == "once":
            _print(autopilot.tick(dispatch))
            return 0
        try:
            autopilot.run(dispatch, interval_seconds=args.interval, on_tick=_print)
        except KeyboardInterrupt:
            return 130
        return 0

    if args.command == "coordinator":
        spec = None
        if args.spec:
            spec = _read_json(args.spec)
        canonical_spec = context.resolve_run_path("control_plane/coordinator_spec.json")
        if canonical_spec.is_file() and not canonical_spec.is_symlink():
            coordinator = RunCoordinator.from_persisted_spec(context, owner_id=args.owner_id)
        elif args.operation == "start":
            if spec is None:
                raise ValueError("coordinator start requires --spec when no persisted spec exists")
            coordinator = RunCoordinator(context, owner_id=args.owner_id)
        elif spec is not None:
            coordinator = RunCoordinator.from_persisted_spec(context, spec_path=args.spec, owner_id=args.owner_id)
        else:
            raise ValueError("coordinator operation requires a persisted coordinator spec")
        if args.operation == "start":
            if canonical_spec.is_file() and not canonical_spec.is_symlink():
                return _print_coordinator_status(coordinator.status(), operation=args.operation, max_steps=args.max_steps)
            assert spec is not None
            return _print_coordinator_status(coordinator.start(spec), operation=args.operation, max_steps=args.max_steps)
        if args.operation == "run":
            if spec is not None and not canonical_spec.is_file():
                coordinator.start(spec)
            return _print_coordinator_status(coordinator.run(max_steps=args.max_steps), operation=args.operation, max_steps=args.max_steps)
        if args.operation == "step":
            return _print_coordinator_status(coordinator.step(), operation=args.operation, max_steps=args.max_steps)
        elif args.operation == "resume":
            return _print_coordinator_status(coordinator.resume(), operation=args.operation, max_steps=args.max_steps)
        elif args.operation == "reopen":
            if not args.reason:
                raise ValueError("coordinator reopen requires --reason")
            return _print_coordinator_status(
                coordinator.reopen(args.reason),
                operation=args.operation,
                max_steps=args.max_steps,
            )
        elif args.operation == "status":
            return _print_coordinator_status(
                coordinator.status(),
                operation=args.operation,
                max_steps=args.max_steps,
            )
        else:
            return _print_coordinator_status(coordinator.watchdog(), operation=args.operation, max_steps=args.max_steps)
        return 0

    # Validate both the control-plane spec and the result destination before
    # opening, probing, or creating anything.  The JSON spec cannot broaden
    # this context.
    spec_path = context.resolve_input(args.spec)
    if args.output:
        output_dir = context.resolve_product_path(args.output)
    else:
        output_dir = context.resolve_product_path("")
    result_path = context.resolve_run_path(output_dir / "result.json")
    with spec_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    payload["capability_id"] = args.capability_id
    parameters = dict(payload.get("parameters") or {})
    # Legacy embedded root declarations are intentionally ignored: the run
    # context is the sole normal-path boundary.
    parameters.pop("allowed_roots", None)
    payload["parameters"] = parameters
    payload.pop("allowed_roots", None)
    spec = OperationSpec.from_dict(payload)
    execution = CoreRuntime(context).execute(spec)
    value = execution.value.to_dict() if hasattr(execution.value, "to_dict") else execution.value
    serialized = {
        "value": value,
        "receipt": execution.receipt.to_dict(),
        "cache_status": execution.cache_status,
    }
    # Every CLI run has a small stable result envelope in the requested output
    # directory; capability-specific writers may add their own derived files.
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(serialized, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    _print(serialized)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
