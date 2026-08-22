"""Bounded pause/resume control for one durable Coordinator run.

Pausing always records the lifecycle transition before terminating the exact
Coordinator process group.  Committed artifacts and append-only events remain
untouched; unfinished in-flight work is retried from durable Coordinator state
after resume.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.workspace import RunContext

from .launch import (
    LaunchConflictError,
    LaunchManager,
    LaunchValidationError,
    LockedLaunchError,
)


TERMINAL_RUN_STATES = frozenset(
    {
        "complete",
        "completed",
        "complete_with_limits",
        "failed",
        "cancelled",
        "blocked",
        "blocked_rethink",
    }
)


@dataclass(frozen=True)
class CoordinatorProcess:
    pid: int
    pgid: int
    command: tuple[str, ...]


class CoordinatorProcessController:
    """Find and stop only the exact standalone Coordinator for one run."""

    @staticmethod
    def _matches(argv: tuple[str, ...], run_id: str, run_root: Path) -> bool:
        try:
            module_index = argv.index("auto_foundry_core.cli")
            coordinator_index = argv.index("coordinator", module_index + 1)
            operation = argv[coordinator_index + 1]
            root_index = argv.index("--run-root", coordinator_index + 2)
            id_index = argv.index("--run-id", coordinator_index + 2)
            command_root = Path(argv[root_index + 1]).expanduser().resolve(strict=False)
            command_run_id = argv[id_index + 1]
        except (ValueError, IndexError, OSError):
            return False
        return (
            operation in {"run", "resume"}
            and command_root == run_root.resolve(strict=False)
            and command_run_id == run_id
        )

    def find(self, run_id: str, run_root: Path) -> CoordinatorProcess | None:
        try:
            completed = subprocess.run(
                ["ps", "-ww", "-axo", "pid=,pgid=,stat=,command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LaunchConflictError("Could not verify the Coordinator process") from exc
        matches: list[CoordinatorProcess] = []
        for raw_line in completed.stdout.splitlines():
            parts = raw_line.strip().split(None, 3)
            if len(parts) != 4:
                continue
            raw_pid, raw_pgid, state, command = parts
            if state.startswith("Z"):
                continue
            try:
                pid = int(raw_pid)
                pgid = int(raw_pgid)
                argv = tuple(shlex.split(command))
            except (ValueError, TypeError):
                continue
            if self._matches(argv, run_id, run_root):
                matches.append(CoordinatorProcess(pid=pid, pgid=pgid, command=argv))
        if len(matches) > 1:
            raise LaunchConflictError("Multiple Coordinator processes match this run")
        return matches[0] if matches else None

    @staticmethod
    def _group_alive(process: CoordinatorProcess) -> bool:
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pgid=,stat="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LaunchConflictError("Could not verify that the Coordinator stopped") from exc
        for raw_line in completed.stdout.splitlines():
            parts = raw_line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pgid = int(parts[0])
            except ValueError:
                continue
            if pgid == process.pgid and not parts[1].startswith("Z"):
                return True
        return False

    @staticmethod
    def _leader_matches(process: CoordinatorProcess) -> bool:
        try:
            completed = subprocess.run(
                ["ps", "-ww", "-p", str(process.pid), "-o", "pgid=,stat=,command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LaunchConflictError("Could not re-verify the Coordinator process") from exc
        parts = completed.stdout.strip().split(None, 2)
        if len(parts) != 3 or parts[1].startswith("Z"):
            return False
        try:
            return int(parts[0]) == process.pgid and tuple(shlex.split(parts[2])) == process.command
        except (ValueError, TypeError):
            return False

    def stop(self, process: CoordinatorProcess, *, timeout_seconds: float = 5.0) -> None:
        if process.pid <= 1 or process.pgid <= 1 or process.pgid != process.pid:
            raise LaunchConflictError("Coordinator is not an isolated process group")
        if process.pgid == os.getpgrp():
            raise LaunchConflictError("Refusing to stop the Control Center process group")
        if not self._leader_matches(process):
            return
        try:
            os.killpg(process.pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            if not self._group_alive(process):
                return
            time.sleep(0.05)
        try:
            os.killpg(process.pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self._group_alive(process):
            time.sleep(0.05)
        if self._group_alive(process):
            raise LaunchConflictError("Coordinator process did not stop")


class RunControlManager:
    """Expose confirmed, durable pause/resume operations to the local UI."""

    def __init__(
        self,
        launch_manager: LaunchManager,
        *,
        process_controller: CoordinatorProcessController | None = None,
    ) -> None:
        self.launch_manager = launch_manager
        self.process_controller = process_controller or CoordinatorProcessController()
        self._mutation_lock = threading.RLock()

    @property
    def settings(self):
        return self.launch_manager.settings

    def _target(self, browser_run_id: str) -> tuple[str, Path, RunLifecycle]:
        if not isinstance(browser_run_id, str) or not browser_run_id.strip():
            raise LaunchValidationError({"runId": "Select a durable run."})
        known = self.launch_manager._known_run(browser_run_id)
        if known is None:
            raise LaunchValidationError({"runId": "Run is not available under the configured run root."})
        run_id, run_root, _state = known
        if self.settings.is_protected_run(run_id, run_root):
            raise LockedLaunchError("This run is protected from operational controls")
        lifecycle = RunLifecycle.load(RunContext(run_id=run_id, run_root=run_root))
        return run_id, run_root, lifecycle

    @staticmethod
    def _capacity(run_root: Path) -> dict[str, int]:
        path = run_root / "entity_resolution" / "state.json"
        if path.is_symlink() or not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        capacity = value.get("capacity") if isinstance(value, Mapping) else None
        if not isinstance(capacity, Mapping):
            return {}
        result: dict[str, int] = {}
        for key, item in capacity.items():
            if isinstance(key, str) and isinstance(item, int) and not isinstance(item, bool):
                result[key] = item
        return result

    def status(self, browser_run_id: str) -> dict[str, Any]:
        run_id, run_root, lifecycle = self._target(browser_run_id)
        process = self.process_controller.find(run_id, run_root)
        state = str(lifecycle.status).strip().lower()
        can_pause = self.settings.commands_enabled and state not in TERMINAL_RUN_STATES and state != "paused"
        can_resume = self.settings.commands_enabled and state == "paused" and process is None
        action = "pause" if can_pause else "resume" if can_resume else None
        return {
            "runId": browser_run_id,
            "authoritativeRunId": run_id,
            "lifecycleStatus": state,
            "coordinatorActive": process is not None,
            "canPause": can_pause,
            "canResume": can_resume,
            "action": action,
            "message": (
                "Pause saves durable progress and stops the isolated Coordinator."
                if can_pause
                else "Resume continues from the durable Coordinator checkpoint."
                if can_resume
                else "Run control is unavailable for this lifecycle state."
            ),
        }

    def pause(self, browser_run_id: str, *, confirmed: bool) -> dict[str, Any]:
        with self._mutation_lock:
            return self._pause(browser_run_id, confirmed=confirmed)

    def _pause(self, browser_run_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Run controls are disabled on this server")
        if not confirmed:
            raise LaunchValidationError({"confirmed": "Pause requires explicit confirmation."})
        run_id, run_root, lifecycle = self._target(browser_run_id)
        state = str(lifecycle.status).strip().lower()
        if state in TERMINAL_RUN_STATES:
            raise LaunchConflictError("A terminal run cannot be paused")
        process = self.process_controller.find(run_id, run_root)
        lifecycle.pause("Paused from Control Center")
        if process is not None:
            self.process_controller.stop(process)
        result = self.status(browser_run_id)
        result["message"] = "Run paused. Durable artifacts and graph history were preserved."
        return result

    def resume(self, browser_run_id: str, *, confirmed: bool) -> dict[str, Any]:
        with self._mutation_lock:
            return self._resume(browser_run_id, confirmed=confirmed)

    def _resume(self, browser_run_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Run controls are disabled on this server")
        if not confirmed:
            raise LaunchValidationError({"confirmed": "Resume requires explicit confirmation."})
        run_id, run_root, lifecycle = self._target(browser_run_id)
        if str(lifecycle.status).strip().lower() != "paused":
            raise LaunchConflictError("Only a paused run can be resumed")
        if self.process_controller.find(run_id, run_root) is not None:
            raise LaunchConflictError("A Coordinator process is already active for this run")
        lifecycle.resume()
        try:
            started = self.launch_manager.runner.start(
                run_id=run_id,
                run_root=run_root,
                manifest_path=run_root / "control_plane" / "coordinator_spec.json",
                capacity=self._capacity(run_root),
                coordinator_operation="run",
            )
        except Exception as exc:
            lifecycle.pause("Control Center resume failed before Coordinator start")
            if isinstance(exc, (LaunchConflictError, LockedLaunchError, LaunchValidationError)):
                raise
            raise LaunchConflictError("Coordinator could not be restarted") from exc
        result = self.status(browser_run_id)
        result.update({"message": "Run resumed from durable progress.", "monitorRunId": started.get("monitorRunId")})
        return result


__all__ = ["CoordinatorProcess", "CoordinatorProcessController", "RunControlManager"]
