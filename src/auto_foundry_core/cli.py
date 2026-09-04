"""Small agent-facing CLI for catalog discovery and deterministic operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from .autopilot import LocalRunAutopilot
from .coordinator import (
    RunCoordinator,
    _required_process_start_token,
    production_role_routing,
    _strict_json_loads,
)
from .supervisor import FoundrySupervisor
from .catalog import capability_catalog, get_capability, search_capabilities
from .contracts import OperationSpec
from .contracts import RequirementRecord
from .lifecycle import RunLifecycle
from .requirement_planning import RequirementExecutionGroup, RequirementExecutionPlan, RequirementSupervisorWorkspace
from .run_extension import RequirementRunExtension
from .runtime import CoreRuntime
from .workspace import RunContext
from . import __version__


_SUPERVISOR_STARTUP_TOKEN_ENV = "AUTO_FOUNDRY_SUPERVISOR_STARTUP_TOKEN"
_CHECKOUT_SRC_ENV = "AUTO_FOUNDRY_CHECKOUT_SRC"
_SUPERVISOR_READY_FILENAME = "supervisor_ready.json"
_SUPERVISOR_HEARTBEAT_FILENAME = "supervisor_heartbeat.json"
_SUPERVISOR_EXIT_FILENAME = "supervisor_exit.json"
_SUPERVISOR_HEARTBEAT_SECONDS = 2.0


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _receipt_payload_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _receipt_path(context: RunContext, filename: str) -> Path:
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ValueError("Supervisor receipt filename is invalid")
    path = context.run_root / "control_plane" / filename
    current = path
    while True:
        if current.is_symlink():
            raise ValueError("Supervisor receipt path cannot contain symlinks")
        if current == context.run_root:
            return path
        parent = current.parent
        if parent == current:
            raise ValueError("Supervisor receipt path escapes the run root")
        current = parent


def _write_hashed_receipt(context: RunContext, filename: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = _receipt_path(context, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["payloadHash"] = _receipt_payload_hash(body)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_canonical_json_bytes(body) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return body


def _startup_token() -> str | None:
    value = os.environ.get(_SUPERVISOR_STARTUP_TOKEN_ENV)
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not (8 <= len(value) <= 128) or not value.isascii() or not all(
        char.isalnum() or char in "-_" for char in value
    ):
        raise ValueError("Supervisor startup token is invalid")
    return value


def _ensure_current_checkout() -> None:
    """Refuse an operational child imported from an ambient installed core."""

    expected = os.environ.get(_CHECKOUT_SRC_ENV)
    if expected in (None, ""):
        return
    expected_root = Path(expected).expanduser().resolve(strict=False)
    actual = Path(__file__).resolve().parents[1]
    if actual != expected_root:
        raise RuntimeError("Current checkout core source is unavailable; refusing an ambient installed core")
    # ``python -m auto_foundry_core.cli`` can still be influenced by an
    # already-loaded package/module (for example through sitecustomize).  The
    # source path check above is necessary but not sufficient: verify the
    # package and coordinator objects that provide the Supervisor contract
    # are from the same checkout before touching durable run state.
    for module_name in ("auto_foundry_core", "auto_foundry_core.coordinator"):
        module = sys.modules.get(module_name)
        origin = getattr(module, "__file__", None) if module is not None else None
        if not origin:
            continue
        try:
            Path(origin).resolve(strict=False).relative_to(expected_root)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "Current checkout core is unavailable; refusing an ambient installed core"
            ) from exc


def _canonical_role_sets() -> set[str]:
    from .requirement_planning import AUTHORIZED_ACTION_ROLE_CONTRACTS

    # Planner/rethink/wait/pause actions are deterministic control-plane
    # records.  They intentionally do not have a model transport route; only
    # dispatchable action owners (plus the explicit intake/supervisor control
    # boundaries) belong in the persisted supervisor routing contract.
    non_dispatchable_roles = {"planner"}
    roles = {
        str(contract.role).strip().lower()
        for contract in AUTHORIZED_ACTION_ROLE_CONTRACTS
        if getattr(contract, "role", None)
        and str(contract.role).strip().lower() not in non_dispatchable_roles
    }
    roles.update({"intake_planner", "foundry_supervisor"})
    return roles


def _validate_supervisor_routes(persisted: Any) -> tuple[str, str]:
    routes = production_role_routing()
    required_roles = _canonical_role_sets()
    config = getattr(persisted, "codex_exec", None)
    if not isinstance(config, dict):
        config = dict(config or {}) if config is not None else {}
    models = config.get("role_models")
    reasoning = config.get("role_reasoning_efforts")
    if not isinstance(models, dict) or not isinstance(reasoning, dict):
        raise RuntimeError("Coordinator role routing is missing")
    if set(models) != required_roles or set(reasoning) != required_roles:
        raise RuntimeError("Coordinator role routing is incomplete or contains non-canonical aliases")
    for role in sorted(required_roles):
        route = routes.get(role)
        if not isinstance(route, dict):
            raise RuntimeError(f"Production role route is missing: {role}")
        if models.get(role) != route.get("model") or str(reasoning.get(role)).lower() != str(route.get("reasoning_effort")).lower():
            raise RuntimeError(f"Coordinator role route does not match the production manifest: {role}")
    spec_hash = hashlib.sha256(_canonical_json_bytes(persisted.to_dict())).hexdigest()
    route_hash = hashlib.sha256(
        _canonical_json_bytes({"role_models": dict(models), "role_reasoning_efforts": dict(reasoning)})
    ).hexdigest()
    return spec_hash, route_hash


def _write_ready_receipt(context: RunContext, coordinator: RunCoordinator, persisted: Any) -> dict[str, Any]:
    _ensure_current_checkout()
    lifecycle = RunLifecycle.load(context)
    if lifecycle.snapshot.run_id != context.run_id or lifecycle.snapshot.run_root != str(context.run_root):
        raise RuntimeError("Durable run access does not match the current supervisor context")
    spec_hash, route_hash = _validate_supervisor_routes(persisted)
    startup_token = _startup_token()
    process_start = _required_process_start_token(os.getpid())
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "foundry_supervisor_ready",
        "runId": context.run_id,
        "runRoot": str(context.run_root),
        "pid": os.getpid(),
        "processGroupId": os.getpgrp(),
        "processStart": process_start,
        "specHash": spec_hash,
        "roleRoutingHash": route_hash,
        "startupToken": startup_token,
        "readyAt": utc_now(),
    }
    return _write_hashed_receipt(context, _SUPERVISOR_READY_FILENAME, payload)


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _SupervisorHeartbeat:
    def __init__(self, context: RunContext, coordinator: RunCoordinator, persisted: Any) -> None:
        self.context = context
        self.coordinator = coordinator
        self.persisted = persisted
        self.spec_hash, self.route_hash = _validate_supervisor_routes(persisted)
        self.startup_token = _startup_token()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def _write(self) -> None:
        try:
            status = self.coordinator.status()
            coordinator_status = getattr(status, "status", None)
            coordinator_phase = getattr(status, "phase", None)
        except Exception as exc:
            coordinator_status = None
            coordinator_phase = f"status_error:{type(exc).__name__}"
        _write_hashed_receipt(
            self.context,
            _SUPERVISOR_HEARTBEAT_FILENAME,
            {
                "schemaVersion": 1,
                "kind": "foundry_supervisor_heartbeat",
                "runId": self.context.run_id,
                "runRoot": str(self.context.run_root),
                "pid": os.getpid(),
                "processGroupId": os.getpgrp(),
                "processStart": _required_process_start_token(os.getpid()),
                "specHash": self.spec_hash,
                "roleRoutingHash": self.route_hash,
                "startupToken": self.startup_token,
                "heartbeatAt": utc_now(),
                "coordinatorStatus": coordinator_status,
                "coordinatorPhase": coordinator_phase,
            },
        )

    def start(self) -> None:
        # The first heartbeat is synchronous so a parent observing readiness
        # can immediately distinguish a live child from a stale receipt.
        self._write()
        self.thread = threading.Thread(target=self._run, name="foundry-supervisor-heartbeat", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.wait(_SUPERVISOR_HEARTBEAT_SECONDS):
            try:
                self._write()
            except Exception:
                # A heartbeat is advisory; terminal state and the ready/exit
                # receipts remain the authoritative lifecycle signals.
                continue

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, _SUPERVISOR_HEARTBEAT_SECONDS + 0.5))


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
        core_version=__version__,
    )


def _read_json(path: str) -> Any:
    with Path(path).expanduser().resolve(strict=True).open("rb") as stream:
        return _strict_json_loads(stream.read())


def _coordinator_exit_code(
    status: Any,
    *,
    operation: str,
    max_steps: int | None = None,
    action: str | None = None,
) -> int:
    """Map coordinator outcomes to stable command-line exit codes."""

    value = status.status if hasattr(status, "status") else status.get("status") if isinstance(status, dict) else str(status)
    if operation == "supervisor" and action in {
        "repair_failed",
        "repair_declined",
        "refresh_failed",
        "repair_no_progress",
    }:
        # A known limited terminal remains visible in the JSON status, but an
        # unproven repair/transport result must still be actionable to callers.
        return 4
    if value in {"complete", "complete_with_limits"}:
        return 0
    if value in {"blocked_rethink", "failed", "rethink"}:
        return 2
    if value == "waiting":
        return 3
    # Every non-terminal result is bounded work. The caller can resume it
    # after inspecting the JSON status.
    return 3


def _coordinator_status_value(status: Any) -> str | None:
    """Extract the scalar status string used by durable CLI receipts."""

    if isinstance(status, str):
        return status
    if isinstance(status, dict):
        value = status.get("status")
    else:
        value = getattr(status, "status", None)
    return value if isinstance(value, str) else None


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
    coordinator.add_argument(
        "operation",
        choices=("start", "step", "run", "resume", "status", "watchdog", "reopen", "regenerate_product"),
    )
    coordinator.add_argument("--spec", help="JSON CoordinatorRunSpec path (used only for initial start)")
    coordinator.add_argument("--owner-id", help="stable coordinator owner identity")
    coordinator.add_argument("--max-steps", type=int, help="bound run operation for one invocation")
    coordinator.add_argument("--reason", help="reason for reopening a waiting or blocked run")
    coordinator.add_argument(
        "--idempotency-key",
        help="optional stable key for one intentional Product dashboard regeneration request",
    )

    supervisor = sub.add_parser(
        "supervisor",
        help="inspect a stalled/limited run and perform one bounded technical repair pass",
    )
    _add_context_arguments(supervisor)
    supervisor.add_argument("operation", choices=("observe", "run"))
    supervisor.add_argument("--spec", help="JSON CoordinatorRunSpec path (used only for initial start)")
    supervisor.add_argument("--owner-id", help="stable coordinator owner identity")
    supervisor.add_argument("--repo-root", help="repository root for supervisor inspection/repair")
    supervisor.add_argument("--max-steps", type=int, help="bound ordinary coordinator work")
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
        elif args.operation == "regenerate_product":
            return _print_coordinator_status(
                coordinator.regenerate_product(
                    reason=args.reason or "operator requested Product dashboard regeneration",
                    idempotency_key=args.idempotency_key,
                ),
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

    if args.command == "supervisor":
        _ensure_current_checkout()
        spec = _read_json(args.spec) if args.spec else None
        canonical_spec = context.resolve_run_path("control_plane/coordinator_spec.json")
        if canonical_spec.is_file() and not canonical_spec.is_symlink():
            coordinator = RunCoordinator.from_persisted_spec(context, owner_id=args.owner_id)
        elif spec is not None:
            coordinator = RunCoordinator(context, owner_id=args.owner_id)
            coordinator.start(spec)
        else:
            raise ValueError("supervisor operation requires a persisted coordinator spec or --spec")
        persisted = coordinator.persisted_spec()
        supervisor = FoundrySupervisor(
            context,
            coordinator=coordinator,
            repository_root=args.repo_root,
            codex_config=persisted.codex_exec,
            require_skill_binding=bool(persisted.codex_exec),
        )
        # Readiness is published only after source/spec import, coordinator
        # construction, complete role ownership validation, and durable run
        # access.  The parent launch waits for this receipt before exposing a
        # running status.
        ready_receipt = _write_ready_receipt(context, coordinator, persisted)
        heartbeat = _SupervisorHeartbeat(context, coordinator, persisted)
        heartbeat.start()
        exit_code = 1
        terminal_status: str | None = None
        error_text: str | None = None
        try:
            if args.operation == "observe":
                observation = supervisor.observe()
                terminal_status = getattr(observation.coordinator, "status", None)
                exit_code = _coordinator_exit_code(observation.coordinator, operation=args.operation, max_steps=args.max_steps)
                _print(observation)
            else:
                result = supervisor.run(max_steps=args.max_steps)
                terminal_status = _coordinator_status_value(getattr(result, "status", None))
                exit_code = _coordinator_exit_code(
                    result.status,
                    operation=args.command,
                    action=result.action,
                    max_steps=args.max_steps,
                )
                _print(result)
            return exit_code
        except Exception as exc:
            error_text = str(exc)[:512]
            raise
        finally:
            heartbeat.stop()
            try:
                _write_hashed_receipt(
                    context,
                    _SUPERVISOR_EXIT_FILENAME,
                    {
                        "schemaVersion": 1,
                        "kind": "foundry_supervisor_exit",
                        "runId": context.run_id,
                        "runRoot": str(context.run_root),
                        "pid": os.getpid(),
                        "processGroupId": os.getpgrp(),
                        "processStart": _required_process_start_token(os.getpid()),
                        "specHash": ready_receipt["specHash"],
                        "roleRoutingHash": ready_receipt["roleRoutingHash"],
                        "startupToken": ready_receipt.get("startupToken"),
                        "exitCode": int(exit_code),
                        "status": terminal_status,
                        "error": error_text,
                        "exitAt": utc_now(),
                    },
                )
            except Exception:
                # Preserve the original supervisor exception/exit code.  A
                # missing terminal receipt remains visible as recoverable
                # startup state to the parent instead of masking the failure.
                if error_text is None:
                    raise

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
