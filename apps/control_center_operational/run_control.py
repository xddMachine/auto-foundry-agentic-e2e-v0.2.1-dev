"""Bounded pause/resume control for one durable Foundry Supervisor run.

Pausing always records the lifecycle transition before terminating the exact
Supervisor process group.  Committed artifacts and append-only events remain
untouched; unfinished in-flight work is retried from durable Coordinator state
after resume.
"""

from __future__ import annotations

import hashlib
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

from .launch import (
    _SupervisorStartCleanupError,
    atomic_write_json,
    LaunchConflictError,
    LaunchManager,
    LaunchValidationError,
    LockedLaunchError,
    _process_group_has_token,
    _terminate_token_owned_process_group,
    _run_admission_lock,
    _run_bound_status_records,
    _validated_process_identity,
    _valid_process_group_token,
    utc_now,
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
    """Find and stop only the exact standalone Foundry Supervisor for one run."""

    @staticmethod
    def _matches(argv: tuple[str, ...], run_id: str, run_root: Path) -> bool:
        try:
            module_index = argv.index("auto_foundry_core.cli")
            supervisor_index = argv.index("supervisor", module_index + 1)
            operation = argv[supervisor_index + 1]
            root_index = argv.index("--run-root", supervisor_index + 2)
            id_index = argv.index("--run-id", supervisor_index + 2)
            command_root = Path(argv[root_index + 1]).expanduser().resolve(strict=False)
            command_run_id = argv[id_index + 1]
        except (ValueError, IndexError, OSError):
            return False
        return (
            operation == "run"
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
            raise LaunchConflictError("Could not verify the Foundry Supervisor process") from exc
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
            raise LaunchConflictError("Multiple Foundry Supervisor processes match this run")
        return matches[0] if matches else None

    @staticmethod
    def group_alive(process_group_id: int, process_group_token: str | None = None) -> bool:
        """Return whether a non-zombie member remains in one exact PGID.

        Orphan admission supplies the private per-launch token.  A token
        match is required there so an OS-recycled PGID cannot block resume.
        Token-less callers are treated as unverified and return false.
        """

        return _process_group_has_token(process_group_id, process_group_token)

    @staticmethod
    def terminate_token_group(process_group_id: int, process_group_token: str) -> bool:
        """Terminate one exact token-owned group, including leaderless children."""

        return _terminate_token_owned_process_group(process_group_id, process_group_token)

    @staticmethod
    def _numeric_group_alive(process_group_id: int) -> bool:
        """Reap a previously identified leader without orphan admission."""

        if isinstance(process_group_id, bool) or not isinstance(process_group_id, int) or process_group_id <= 1:
            return False
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pgid=,stat="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LaunchConflictError("Could not verify that the Foundry Supervisor stopped") from exc
        for raw_line in completed.stdout.splitlines():
            parts = raw_line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pgid = int(parts[0])
            except ValueError:
                continue
            if pgid == process_group_id and not parts[1].startswith("Z"):
                return True
        return False

    @classmethod
    def _group_alive(cls, process: CoordinatorProcess) -> bool:
        return cls._numeric_group_alive(process.pgid)

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
            raise LaunchConflictError("Could not re-verify the Foundry Supervisor process") from exc
        parts = completed.stdout.strip().split(None, 2)
        if len(parts) != 3 or parts[1].startswith("Z"):
            return False
        try:
            return int(parts[0]) == process.pgid and tuple(shlex.split(parts[2])) == process.command
        except (ValueError, TypeError):
            return False

    def stop(self, process: CoordinatorProcess, *, timeout_seconds: float = 5.0) -> None:
        if process.pid <= 1 or process.pgid <= 1 or process.pgid != process.pid:
            raise LaunchConflictError("Foundry Supervisor is not an isolated process group")
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
            raise LaunchConflictError("Foundry Supervisor process did not stop")


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

    def _preliminary_target(self, browser_run_id: str) -> tuple[str, Path]:
        """Resolve only the immutable run identity before taking its lock."""

        if not isinstance(browser_run_id, str) or not browser_run_id.strip():
            raise LaunchValidationError({"runId": "Select a durable run."})
        known = self.launch_manager._known_run(browser_run_id)
        if known is None:
            raise LaunchValidationError({"runId": "Run is not available under the configured run root."})
        run_id, run_root, _state = known
        if self.settings.is_protected_run(run_id, run_root):
            raise LockedLaunchError("This run is protected from operational controls")
        try:
            resolved_root = Path(run_root).expanduser().resolve(strict=False)
        except (OSError, TypeError, ValueError) as exc:
            raise LaunchConflictError("Run root is unavailable for operational control") from exc
        return run_id, resolved_root

    def _target(self, browser_run_id: str) -> tuple[str, Path, Any]:
        run_id, run_root = self._preliminary_target(browser_run_id)
        # Resolve the core through LaunchManager's checkout-bound import
        # boundary.  A run-control request must never load an ambient installed
        # lifecycle implementation from site-packages.
        api = self.launch_manager._core_imports()
        lifecycle = api["RunLifecycle"].load(api["RunContext"](run_id=run_id, run_root=run_root))
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

    def _persisted_process_group_identities(
        self,
        run_id: str,
        run_root: Path,
    ) -> tuple[tuple[int, str], ...]:
        """Read every exact run-bound PGID/token pair from launch status.

        Distinct launch drafts share one Supervisor ownership boundary.  Do
        not let a newer queued/identity-less draft hide an older live child;
        invalid records and groups proven gone are naturally ignored by the
        liveness check in :meth:`_active_process`.
        """

        # The process-group identity is the ownership unit.  Multiple draft
        # statuses may copy it; retain only the newest record for each exact
        # ``(PGID, token)`` pair, then probe all unique identities in stable
        # newest-first order so a distinct older live group cannot be hidden
        # by a newer group that is already gone.
        identities_by_pair: dict[tuple[int, str], tuple[str, Path]] = {}
        for _stamp, _path, value in _run_bound_status_records(self.settings, run_id, run_root):
            identity = _validated_process_identity(value)
            if identity is None:
                continue
            _pid, process_group_id, process_group_token = identity
            pair = (process_group_id, process_group_token)
            current = identities_by_pair.get(pair)
            if current is None or (_stamp, _path.name) > (current[0], current[1].name):
                identities_by_pair[pair] = (_stamp, _path)
        if not identities_by_pair:
            return ()
        ordered = sorted(
            identities_by_pair.items(),
            key=lambda item: (item[1][0], item[1][1].name),
            reverse=True,
        )
        return tuple(pair for pair, _record in ordered)

    def _active_process(self, run_id: str, run_root: Path) -> tuple[CoordinatorProcess | None, bool]:
        """Return (leader, orphaned) for one run without killing stale groups."""

        process = self.process_controller.find(run_id, run_root)
        if process is not None:
            return process, False
        group_alive = getattr(self.process_controller, "group_alive", None)
        if not callable(group_alive):
            return None, False
        for process_group_id, process_group_token in self._persisted_process_group_identities(run_id, run_root):
            try:
                alive = bool(group_alive(process_group_id, process_group_token))
            except TypeError:
                # A legacy/fake controller that cannot verify the private
                # token must fail safe as quiescent, never fall back to a
                # numeric-only orphan decision.
                alive = False
            except LaunchConflictError:
                raise
            except Exception as exc:
                raise LaunchConflictError("Could not verify the Foundry Supervisor process group") from exc
            if alive:
                # Do not manufacture a stoppable leader from stale metadata.
                # The group is active for resume admission, but no signal is
                # sent unless the exact Supervisor leader is re-identified.
                return None, True
        return None, False

    def _product_regeneration_projection(
        self,
        run_id: str,
        run_root: Path,
    ) -> tuple[bool, str | None, str | None]:
        """Read the durable Product-regeneration phase for UI admission.

        Pending state is owned by the Coordinator event projection rather than
        browser memory.  Status polling therefore disables duplicate clicks
        across refreshes while retaining a fail-closed read-only fallback for
        legacy runs that do not yet have a persisted coordinator spec.
        """

        try:
            api = self.launch_manager._core_imports()
            context = api["RunContext"](run_id=run_id, run_root=run_root)
            # Probe the immutable recovery boundary before loading the
            # persisted Coordinator.  A valid pending/orphan transport
            # rebind is a mixed old/new specification and must remain
            # unavailable to GET status until the normal POST preparation
            # transaction reconciles it.  The class-level check keeps older
            # test seams that provide only ``from_persisted_spec`` isolated;
            # canonical production RunCoordinator always exposes this guard.
            coordinator_type = api["RunCoordinator"]
            preflight = getattr(coordinator_type, "validate_read_only_resume_evidence", None)
            if callable(preflight):
                guard = coordinator_type(context)
                try:
                    preflight = getattr(guard, "validate_read_only_resume_evidence", None)
                    if not callable(preflight):
                        raise LaunchConflictError("Coordinator recovery preview validator is unavailable")
                    preflight(reject_transport_rebind=True)
                finally:
                    guard.close(wait_for_roles=False)
            try:
                coordinator = coordinator_type.from_persisted_spec(context)
            except Exception as persisted_error:
                mismatch_type = api.get("CoordinatorProductionBindingMismatch")
                if mismatch_type is None or not isinstance(persisted_error, mismatch_type):
                    raise
                # A persisted coordinator bound to a prior installed Product
                # release must remain invalid for normal reconstruction.  For
                # read-only status only, ask LaunchManager for the exact
                # post-rebind spec and use the core's pure projection API.
                # The preview helper proves that the failure is specifically
                # a stale production binding; malformed state/specs never
                # receive this fallback.
                preview_reader = getattr(self.launch_manager, "preview_resume_coordinator", None)
                if not callable(preview_reader):
                    raise persisted_error
                preview = preview_reader(run_id, run_root)
                if not isinstance(preview, Mapping) or preview.get("persistedBindingStale") is not True:
                    raise persisted_error
                desired_spec = preview.get("spec")
                if not isinstance(desired_spec, Mapping) and desired_spec is None:
                    raise persisted_error
                coordinator = api["RunCoordinator"](context)
                pure_reader = getattr(coordinator, "product_regeneration_projection_for_spec", None)
                if not callable(pure_reader):
                    raise persisted_error
                try:
                    request_projection = pure_reader(desired_spec)
                finally:
                    coordinator.close(wait_for_roles=False)
                if not isinstance(request_projection, Mapping):
                    return False, None, None
                request_status = str(request_projection.get("status", "")).strip().lower()
                pending = (
                    bool(request_projection.get("pending"))
                    if "pending" in request_projection
                    else request_status in {"requested", "dispatched", "running"}
                )
                if request_projection.get("eligible") is False and not pending:
                    return pending, request_status or "ready", None
                intent_key = request_projection.get("idempotency_key")
                if not isinstance(intent_key, str) or not intent_key.strip():
                    return pending, request_status or "ready", None
                return pending, request_status or "ready", intent_key.strip()
            try:
                request_projection = None
                projection_reader = getattr(coordinator, "product_regeneration_projection", None)
                if callable(projection_reader):
                    request_projection = projection_reader()
                projection = coordinator.status()
            finally:
                coordinator.close(wait_for_roles=False)
            phase = str(getattr(projection, "phase", "") or "").strip().lower() or None
            request_status = (
                str(request_projection.get("status", "")).strip().lower()
                if isinstance(request_projection, Mapping)
                else ""
            )
            pending = (
                bool(request_projection.get("pending"))
                if isinstance(request_projection, Mapping) and "pending" in request_projection
                else request_status in {"requested", "dispatched", "running"}
            )
            if isinstance(request_projection, Mapping):
                if request_projection.get("eligible") is False and not pending:
                    # The canonical Coordinator projection has failed its
                    # current accepted-input/spec/revision validation.  Do
                    # not fall back to the legacy event-derived key, which
                    # would incorrectly leave a tampered terminal run
                    # clickable.
                    return pending, request_status or phase, None
                intent_key = request_projection.get("idempotency_key")
                if isinstance(intent_key, str) and intent_key.strip():
                    # The Coordinator binds both pending request keys and
                    # terminal next-intent keys to immutable product/spec
                    # identities.  Reusing this backend-derived value exactly
                    # (without a UI-added prefix) is safe and deterministic;
                    # a terminal request never leaks its old one-shot key
                    # back to the browser.
                    return pending, request_status or phase, intent_key.strip()
                request_id = request_projection.get("request_id")
                if pending and isinstance(request_id, str) and request_id.strip():
                    return pending, request_status or phase, request_id.strip()
            # Legacy/incomplete coordinators have no durable request marker;
            # retain the old event-bound read-only key solely for status/UI
            # continuity.  The public command still performs strict admission.
            generation_id = str(getattr(projection, "generation_id", "") or "")
            event_hash = str(getattr(projection, "last_event_hash", "") or "")
            if not generation_id or not event_hash:
                return pending, request_status or phase, None
            seed = "\x00".join((run_id, generation_id, request_status or phase or "", event_hash))
            key = "product-regeneration-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
            return pending, request_status or phase, key
        except Exception:
            # The run-control status endpoint remains useful for ordinary
            # lifecycle control when a legacy/incomplete coordinator cannot be
            # projected.  The public regeneration command itself performs the
            # strict accepted-input/spec admission and will return the exact
            # error if an operator attempts it.
            return False, None, None

    def status(self, browser_run_id: str) -> dict[str, Any]:
        run_id, run_root, lifecycle = self._target(browser_run_id)
        process, orphaned = self._active_process(run_id, run_root)
        state = str(lifecycle.status).strip().lower()
        regeneration_pending, regeneration_phase, regeneration_key = (
            self._product_regeneration_projection(run_id, run_root)
            if process is None and not orphaned
            else (False, None, None)
        )
        can_pause = (
            self.settings.commands_enabled
            and state not in TERMINAL_RUN_STATES
            and state != "paused"
            and not orphaned
        )
        can_resume = self.settings.commands_enabled and state == "paused" and process is None and not orphaned
        # Regeneration is an intentional Product-layer request, not a
        # transport recovery shortcut.  A terminal analytical outcome may
        # still receive a new Product revision; generic resume remains
        # unavailable for those states and the core performs the stronger
        # accepted-input/spec/owner admission checks.
        regeneration_lifecycle_allowed = state not in TERMINAL_RUN_STATES or state in {
            "complete",
            "completed",
            "complete_with_limits",
        }
        can_regenerate_product = (
            self.settings.commands_enabled
            and regeneration_lifecycle_allowed
            and process is None
            and not orphaned
            and not regeneration_pending
            and bool(regeneration_key)
        )
        action = "pause" if can_pause else "resume" if can_resume else None
        return {
            "runId": browser_run_id,
            "authoritativeRunId": run_id,
            "lifecycleStatus": state,
            "coordinatorActive": process is not None,
            "coordinatorOrphaned": orphaned,
            "canPause": can_pause,
            "canResume": can_resume,
            "canRegenerateProduct": can_regenerate_product,
            "productRegenerationPending": regeneration_pending,
            "productRegenerationPhase": regeneration_phase,
            "productRegenerationIdempotencyKey": regeneration_key,
            "action": action,
            "message": (
                "Pause saves durable progress and stops the isolated Foundry Supervisor."
                if can_pause
                else "Resume waits for orphaned Foundry Supervisor process-group members to exit."
                if orphaned
                else "Resume continues through the Foundry Supervisor from the durable Coordinator checkpoint."
                if can_resume
                else "Run control is unavailable for this lifecycle state."
            ),
            "productRegenerationMessage": (
                "Product dashboard regeneration is already requested and will dispatch from the durable Supervisor checkpoint."
                if regeneration_pending
                else
                "Request one Product Agent dashboard regeneration from accepted business outputs."
                if can_regenerate_product
                else "Product dashboard regeneration is unavailable while this run is active, orphaned, or terminally failed."
            ),
        }

    def pause(self, browser_run_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Run controls are disabled on this server")
        if not confirmed:
            raise LaunchValidationError({"confirmed": "Pause requires explicit confirmation."})
        preliminary_run_id, preliminary_root = self._preliminary_target(browser_run_id)
        with _run_admission_lock(self.settings, preliminary_run_id, preliminary_root):
            # Re-resolve the target after lock acquisition.  The lifecycle,
            # process, and status reads in ``_pause`` are authoritative and
            # therefore cannot use a pre-lock run snapshot.
            current_run_id, current_root = self._preliminary_target(browser_run_id)
            if current_run_id != preliminary_run_id or current_root != preliminary_root:
                raise LaunchConflictError("Run identity changed during pause admission")
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
        process, orphaned = self._active_process(run_id, run_root)
        if orphaned:
            raise LaunchConflictError("Foundry Supervisor process-group members are still active")
        lifecycle.pause("Paused from Control Center")
        if process is not None:
            self.process_controller.stop(process)
        result = self.status(browser_run_id)
        result["message"] = "Run paused. Durable artifacts and graph history were preserved."
        return result

    def regenerate_product(
        self,
        browser_run_id: str,
        *,
        confirmed: bool,
        reason: str = "operator requested Product dashboard regeneration",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Request one intentional Product dashboard regeneration.

        The command records the core's generation-scoped one-shot
        authorization and ensures the normal Supervisor path is started.  It
        never marks a role stale; the bound Product Agent action is dispatched
        from the durable request on that Supervisor.
        """

        if not self.settings.commands_enabled:
            raise LockedLaunchError("Run controls are disabled on this server")
        if not confirmed:
            raise LaunchValidationError({"confirmed": "Product regeneration requires explicit confirmation."})
        preliminary_run_id, preliminary_root = self._preliminary_target(browser_run_id)
        with _run_admission_lock(self.settings, preliminary_run_id, preliminary_root):
            current_run_id, current_root = self._preliminary_target(browser_run_id)
            if current_run_id != preliminary_run_id or current_root != preliminary_root:
                raise LaunchConflictError("Run identity changed during Product regeneration admission")
            with self._mutation_lock:
                return self._regenerate_product(
                    browser_run_id,
                    confirmed=confirmed,
                    reason=reason,
                    idempotency_key=idempotency_key,
                )

    def _regenerate_product(
        self,
        browser_run_id: str,
        *,
        confirmed: bool,
        reason: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Run controls are disabled on this server")
        if not confirmed:
            raise LaunchValidationError({"confirmed": "Product regeneration requires explicit confirmation."})
        run_id, run_root, lifecycle = self._target(browser_run_id)
        state = str(lifecycle.status).strip().lower()
        if state in TERMINAL_RUN_STATES and state not in {"complete", "completed", "complete_with_limits"}:
            raise LaunchConflictError("Product regeneration is unavailable for a terminally failed run")
        process, orphaned = self._active_process(run_id, run_root)
        if process is not None:
            raise LaunchConflictError("A Foundry Supervisor process is already active for this run")
        if orphaned:
            raise LaunchConflictError("Foundry Supervisor process-group members are still active")

        api = self.launch_manager._core_imports()
        context = getattr(lifecycle, "context", None)
        if not isinstance(context, api["RunContext"]):
            context = api["RunContext"](run_id=run_id, run_root=run_root)
        # Rebind the transport/spec before constructing the coordinator used
        # for admission.  Product regeneration is explicitly allowed to use
        # a newly installed Product release; constructing the old persisted
        # adapter first would reject the exact drift we intend to repair.
        try:
            self.launch_manager.prepare_resume_coordinator(run_id, run_root)
        except (LaunchConflictError, LockedLaunchError, LaunchValidationError):
            raise
        except Exception as exc:
            raise LaunchConflictError("Foundry Supervisor coordinator could not be prepared for Product regeneration") from exc
        coordinator = api["RunCoordinator"].from_persisted_spec(context)
        before_request_projection: Mapping[str, Any] | None = None
        after_request_projection: Mapping[str, Any] | None = None
        projection_reader = getattr(coordinator, "product_regeneration_projection", None)
        try:
            if callable(projection_reader):
                before_value = projection_reader()
                if isinstance(before_value, Mapping):
                    before_request_projection = dict(before_value)
                    if (
                        before_request_projection.get("eligible") is False
                        and not bool(before_request_projection.get("pending"))
                    ):
                        # The canonical Coordinator has failed its current
                        # Product evidence/spec/input admission.  Keep the
                        # endpoint fail-closed as well as the status button;
                        # in particular, never invoke the Supervisor start
                        # path for a missing/tampered revision pointer.
                        raise LaunchConflictError(
                            "Product regeneration is unavailable for the current Product evidence"
                        )
                    submitted_key = idempotency_key.strip() if isinstance(idempotency_key, str) else None
                    expected_key = before_request_projection.get("idempotency_key")
                    before_pending = bool(before_request_projection.get("pending"))
                    historical_request_key = before_request_projection.get("request_id")
                    if (
                        submitted_key is not None
                        and isinstance(expected_key, str)
                        and expected_key.strip()
                        and not before_pending
                        and submitted_key != historical_request_key
                        and submitted_key != expected_key.strip()
                    ):
                        raise LaunchConflictError(
                            "Product regeneration intent is stale; refresh the run status before retrying"
                        )
            try:
                coordinator_status = coordinator.regenerate_product(
                    reason=reason,
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:
                # The core's typed conflict is the public 409 boundary for
                # duplicate/ineligible regeneration requests.  Keep other
                # coordinator integrity/runtime failures on the existing
                # 422 path; the dynamic class lookup preserves checkout
                # binding and keeps unit fakes that omit it compatible.
                conflict_type = api.get("CoordinatorConflictError")
                if conflict_type is not None and isinstance(exc, conflict_type):
                    raise LaunchConflictError(str(exc)) from exc
                raise
            if callable(projection_reader):
                after_value = projection_reader()
                if isinstance(after_value, Mapping):
                    after_request_projection = dict(after_value)
        finally:
            coordinator.close(wait_for_roles=False)

        after_status = (
            str(after_request_projection.get("status", "")).strip().lower()
            if isinstance(after_request_projection, Mapping)
            else ""
        )
        after_pending = (
            bool(after_request_projection.get("pending"))
            if isinstance(after_request_projection, Mapping) and "pending" in after_request_projection
            else after_status in {"requested", "dispatched", "running"}
        )
        # Submitting a terminal request's historical one-shot key is a
        # deliberate idempotent no-op.  Do not restart a Supervisor merely
        # because the caller repeated that old key; a fresh backend-derived
        # intent key is required to create the next revision.  Fakes/legacy
        # coordinators without the projection retain the established start
        # behavior and are covered by the compatibility-shaped unit seams.
        terminal_idempotent = (
            isinstance(before_request_projection, Mapping)
            and str(before_request_projection.get("status", "")).strip().lower() in {"accepted", "failed"}
            and isinstance(after_request_projection, Mapping)
            and str(after_request_projection.get("status", "")).strip().lower() in {"accepted", "failed"}
            and isinstance(idempotency_key, str)
            and idempotency_key.strip() == str(before_request_projection.get("request_id") or "").strip()
            and str(after_request_projection.get("request_id") or "").strip() == str(before_request_projection.get("request_id") or "").strip()
        )
        should_start = after_pending and not terminal_idempotent

        # The command is intentionally a complete operator operation: once
        # the request is durably recorded, use the same checkout-bound
        # preparation/start path as Resume so the browser does not require a
        # second click.  A paused lifecycle takes the normal resume branch;
        # an initialized/ready lifecycle is started in place without writing
        # a synthetic pause/resume transition.  If start fails, the request
        # remains pending in Coordinator state and the response exposes a
        # recoverable outcome for a later retry.
        startup: dict[str, Any] | None = None
        startup_error: Exception | None = None
        if should_start or after_request_projection is None:
            try:
                startup = self._start_supervisor(
                    browser_run_id,
                    run_id,
                    run_root,
                    lifecycle,
                    resume_lifecycle=state == "paused",
                )
            except Exception as exc:
                startup_error = exc

        # Keep the browser response privacy-safe: the durable core status is
        # exposed as scalar operation metadata, not raw action bindings or
        # internal artifact paths.
        result = self.status(browser_run_id)
        result.update(
            {
                "operation": "regenerate_product",
                "requested": not terminal_idempotent,
                "idempotent": terminal_idempotent,
                "coordinatorStatus": str(getattr(coordinator_status, "status", "requested")),
                "coordinatorPhase": str(getattr(coordinator_status, "phase", "ready")),
                "startupStatus": (
                    startup.get("startupStatus")
                    if isinstance(startup, Mapping)
                    else "pending"
                    if startup_error is not None
                    else "pending"
                ),
                "message": (
                    "The Product regeneration request was already terminal; no new Supervisor was started."
                    if terminal_idempotent
                    else "Product dashboard regeneration requested and the Foundry Supervisor is ready."
                    if isinstance(startup, Mapping) and startup.get("startupStatus") == "running"
                    else "Product dashboard regeneration requested; the Foundry Supervisor is starting."
                    if isinstance(startup, Mapping)
                    else "Product dashboard regeneration requested; Supervisor start is pending and can be retried safely."
                ),
            }
        )
        if startup_error is not None:
            # Do not turn a durable request into an HTTP failure merely
            # because the optional transport start was unavailable.  The
            # pending request is the authoritative retry boundary.
            result["startupError"] = str(startup_error)[:300]
            result["productRegenerationPending"] = True
        elif isinstance(startup, Mapping):
            result["monitorRunId"] = startup.get("monitorRunId")
        return result

    def _latest_status_record(
        self,
        run_id: str,
        run_root: Path,
    ) -> tuple[Path, dict[str, Any]] | None:
        """Return the newest private launch status bound to one run."""

        statuses_root = Path(self.settings.state_root) / "statuses"
        if statuses_root.is_symlink() or not statuses_root.is_dir():
            return None
        try:
            expected_root = run_root.expanduser().resolve(strict=False)
            children = sorted(statuses_root.iterdir(), key=lambda item: item.name)
        except OSError:
            return None
        matches: list[tuple[str, Path, dict[str, Any]]] = []
        for child in children:
            if child.is_symlink() or not child.is_file() or child.suffix != ".json":
                continue
            try:
                value = json.loads(child.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or value.get("runId") != run_id:
                continue
            raw_root = value.get("runRoot")
            if not isinstance(raw_root, str) or not raw_root:
                continue
            try:
                if Path(raw_root).expanduser().resolve(strict=False) != expected_root:
                    continue
            except OSError:
                continue
            matches.append(
                (
                    str(value.get("startedAt") or value.get("acceptedAt") or ""),
                    child,
                    value,
                )
            )
        if not matches:
            return None
        _stamp, path, value = max(matches, key=lambda item: (item[0], item[1].name))
        return path, value

    def _persist_resume_start_failure(
        self,
        run_id: str,
        run_root: Path,
        started: Mapping[str, Any],
        *,
        status: str,
        message: str,
    ) -> None:
        """Persist a truthful paused/recoverable resume-start outcome.

        ``ensure_process_status`` creates the run-bound private status before
        resume spawns.  This helper updates that same record with the exact
        token-bound identity carried by ``SubprocessRunner``; the public
        run-control response never exposes either token.
        """

        record = self._latest_status_record(run_id, run_root)
        if record is None:
            raise LaunchConflictError("Resume status record is unavailable for process recovery")
        path, existing = record
        payload = dict(existing)
        for key in (
            "monitorRunId",
            "pid",
            "processGroupId",
            "processGroupToken",
            "startupToken",
            "processStart",
            "ready",
            "readyAt",
            "startupTimedOut",
            "childExited",
            "exitCode",
            "exitAt",
        ):
            if key in started and started.get(key) is not None:
                payload[key] = started[key]
        payload.update({"status": status, "message": message})
        if status == "failed":
            payload["completedAt"] = utc_now()
        atomic_write_json(path, payload)

    def _terminate_transferred_start(self, started: Any) -> Exception | None:
        """Retry exact token-owned cleanup for a start exception transfer."""

        identity = _validated_process_identity(started)
        if identity is None:
            return LaunchConflictError(
                "Foundry Supervisor resume failure did not carry a complete process identity"
            )
        _pid, process_group_id, process_group_token = identity
        terminate_group = getattr(self.process_controller, "terminate_token_group", None)
        if not callable(terminate_group):
            return LaunchConflictError("Foundry Supervisor process cleanup is unavailable")
        try:
            terminated = terminate_group(process_group_id, process_group_token)
        except Exception as exc:
            return exc
        if terminated is not True:
            return LaunchConflictError("Foundry Supervisor process-group cleanup was not confirmed")
        return None

    def resume(self, browser_run_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Run controls are disabled on this server")
        if not confirmed:
            raise LaunchValidationError({"confirmed": "Resume requires explicit confirmation."})
        preliminary_run_id, preliminary_root = self._preliminary_target(browser_run_id)
        with _run_admission_lock(self.settings, preliminary_run_id, preliminary_root):
            # Re-resolve the target after lock acquisition.  ``_resume`` then
            # reloads lifecycle/process state while holding both the run lock
            # and instance mutation lock, preventing duplicate starts across
            # manager instances without serializing unrelated runs.
            current_run_id, current_root = self._preliminary_target(browser_run_id)
            if current_run_id != preliminary_run_id or current_root != preliminary_root:
                raise LaunchConflictError("Run identity changed during resume admission")
            with self._mutation_lock:
                return self._resume(browser_run_id, confirmed=confirmed)

    def _start_supervisor(
        self,
        browser_run_id: str,
        run_id: str,
        run_root: Path,
        lifecycle: Any,
        *,
        resume_lifecycle: bool,
    ) -> dict[str, Any]:
        """Start the normal Supervisor transport for an admitted run.

        ``resume_lifecycle`` is true only for the public pause/resume
        transition.  Product regeneration may be requested while a run is
        already at an initialized/ready boundary; in that case the exact
        same launch and identity-tracking path is used without manufacturing
        a lifecycle transition that did not occur.
        """

        self.launch_manager.ensure_process_status(run_id, run_root)
        if resume_lifecycle:
            lifecycle.resume()
        try:
            started = self.launch_manager.runner.start(
                run_id=run_id,
                run_root=run_root,
                manifest_path=run_root / "control_plane" / "coordinator_spec.json",
                capacity=self._capacity(run_root),
            )
        except Exception as exc:
            transferred_started = getattr(exc, "started", None)
            cleanup_error: Exception | None = None
            transferred_identity = isinstance(exc, _SupervisorStartCleanupError) and isinstance(
                transferred_started,
                Mapping,
            )
            if transferred_identity:
                cleanup_error = self._terminate_transferred_start(transferred_started)
            if resume_lifecycle:
                try:
                    pause_reason = (
                        "Control Center resume failed after Supervisor start; child cleanup was confirmed"
                        if transferred_identity and cleanup_error is None
                        else "Control Center resume failed; process cleanup requires recovery"
                        if transferred_identity
                        else "Control Center resume failed before Foundry Supervisor start"
                    )
                    lifecycle.pause(pause_reason)
                except Exception as pause_error:
                    if cleanup_error is None:
                        cleanup_error = pause_error
            if transferred_identity:
                recovery_status = "failed" if cleanup_error is None else "starting"
                recovery_message = (
                    "Resume failed after Supervisor start; the token-owned child was terminated and the run is paused/recoverable."
                    if cleanup_error is None
                    else "Resume failed after Supervisor start; token-owned child cleanup is unconfirmed and the run remains recoverable."
                )
                try:
                    self._persist_resume_start_failure(
                        run_id,
                        run_root,
                        transferred_started,
                        status=recovery_status,
                        message=recovery_message,
                    )
                except Exception as persist_error:
                    if cleanup_error is None:
                        cleanup_error = persist_error
            if cleanup_error is not None:
                raise LaunchConflictError(
                    "Foundry Supervisor resume failed; process cleanup or recovery remains pending"
                    if resume_lifecycle
                    else "Foundry Supervisor start failed; process cleanup or recovery remains pending"
                ) from cleanup_error
            if transferred_identity:
                raise LaunchConflictError(
                    "Foundry Supervisor resume failed after start; cleanup was confirmed and the run is paused/recoverable"
                    if resume_lifecycle
                    else "Foundry Supervisor start failed after start; cleanup was confirmed and the run is recoverable"
                ) from exc
            if isinstance(exc, (LaunchConflictError, LockedLaunchError, LaunchValidationError)):
                raise
            raise LaunchConflictError("Foundry Supervisor could not be started") from exc
        try:
            identity = _validated_process_identity(started)
            if identity is None:
                raise LaunchConflictError("Foundry Supervisor returned no complete process identity")
            tracked = self.launch_manager.record_process_start(run_id, run_root, started)
            if tracked is not True:
                raise LaunchConflictError("Foundry Supervisor process identity was not persisted")
        except Exception as exc:
            cleanup_error: Exception | None = None
            identity = _validated_process_identity(started)
            if identity is None:
                cleanup_error = LaunchConflictError(
                    "Foundry Supervisor start failure did not carry a complete process identity"
                )
            else:
                _pid, process_group_id, process_group_token = identity
                terminate_group = getattr(self.process_controller, "terminate_token_group", None)
                if not callable(terminate_group):
                    cleanup_error = LaunchConflictError("Foundry Supervisor process cleanup is unavailable")
                else:
                    try:
                        terminated = terminate_group(process_group_id, process_group_token)
                        if terminated is not True:
                            cleanup_error = LaunchConflictError(
                                "Foundry Supervisor process-group cleanup was not confirmed"
                            )
                    except Exception as cleanup_exc:
                        cleanup_error = cleanup_exc

            # Persist the complete identity before pausing the lifecycle.  A
            # failed/unknown termination therefore survives reload and blocks
            # duplicate starts instead of exposing a tokenless status.
            recovery_status = "failed" if cleanup_error is None else "starting"
            recovery_message = (
                "Supervisor process identity tracking failed; the token-owned child was terminated and the run is recoverable."
                if cleanup_error is None
                else "Supervisor process identity tracking failed; token-owned child cleanup is unconfirmed and the run remains recoverable."
            )
            try:
                if not isinstance(started, Mapping):
                    raise LaunchConflictError(
                        "Foundry Supervisor start failure did not carry a persistable process identity"
                    )
                self._persist_resume_start_failure(
                    run_id,
                    run_root,
                    started,
                    status=recovery_status,
                    message=recovery_message,
                )
            except Exception as persist_error:
                if cleanup_error is None:
                    cleanup_error = persist_error
            if resume_lifecycle:
                try:
                    lifecycle.pause("Control Center Supervisor process identity tracking failed")
                except Exception as pause_error:
                    if cleanup_error is None:
                        cleanup_error = pause_error
            if cleanup_error is not None:
                raise LaunchConflictError(
                    "Foundry Supervisor process cleanup failed after resume error"
                    if resume_lifecycle
                    else "Foundry Supervisor process cleanup failed after start error"
                ) from cleanup_error
            if isinstance(exc, (LaunchConflictError, LockedLaunchError, LaunchValidationError)):
                raise
            raise LaunchConflictError("Foundry Supervisor process identity could not be persisted") from exc
        if isinstance(started, Mapping) and started.get("childExited") is True:
            # A child that exits before readiness cannot leave a lifecycle in
            # an apparently resumed state.  Product regeneration otherwise
            # leaves the durable request pending for a later retry.
            if resume_lifecycle:
                try:
                    lifecycle.pause("Foundry Supervisor exited before readiness")
                except Exception:
                    pass
        result = self.status(browser_run_id)
        startup_status = (
            "running"
            if isinstance(started, Mapping) and started.get("ready") is True
            else "starting"
            if isinstance(started, Mapping) and started.get("startupTimedOut") is True
            else "failed"
            if isinstance(started, Mapping) and started.get("childExited") is True
            else "accepted"
        )
        result.update(
            {
                "message": (
                    "Run resumed and Foundry Supervisor is ready."
                    if resume_lifecycle and startup_status == "running"
                    else "Product dashboard regeneration requested and Foundry Supervisor is ready."
                    if not resume_lifecycle and startup_status == "running"
                    else "Run resume is still starting; the live child was retained while readiness is pending."
                    if resume_lifecycle and startup_status == "starting"
                    else "Product dashboard regeneration requested; the Foundry Supervisor is starting."
                    if not resume_lifecycle and startup_status == "starting"
                    else "Run resume child exited before readiness; the run is paused and recoverable."
                    if resume_lifecycle and startup_status == "failed"
                    else "Product dashboard regeneration requested; Supervisor exited before readiness and the request remains pending."
                    if not resume_lifecycle and startup_status == "failed"
                    else "Run resumed from durable progress."
                ),
                "monitorRunId": started.get("monitorRunId"),
                "startupStatus": startup_status,
            }
        )
        return result

    def _resume(self, browser_run_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not self.settings.commands_enabled:
            raise LockedLaunchError("Run controls are disabled on this server")
        if not confirmed:
            raise LaunchValidationError({"confirmed": "Resume requires explicit confirmation."})
        run_id, run_root, lifecycle = self._target(browser_run_id)
        if str(lifecycle.status).strip().lower() != "paused":
            raise LaunchConflictError("Only a paused run can be resumed")
        process, orphaned = self._active_process(run_id, run_root)
        if process is not None:
            raise LaunchConflictError("A Foundry Supervisor process is already active for this run")
        if orphaned:
            raise LaunchConflictError("Foundry Supervisor process-group members are still active")
        # Reopen/import/rebind the public coordinator while the run is still
        # paused.  No Supervisor child is allowed to observe an old Codex
        # transport spec or a legacy wrapper.
        try:
            self.launch_manager.prepare_resume_coordinator(run_id, run_root)
        except (LaunchConflictError, LockedLaunchError, LaunchValidationError):
            raise
        except Exception as exc:
            raise LaunchConflictError("Foundry Supervisor coordinator could not be prepared for resume") from exc
        return self._start_supervisor(
            browser_run_id,
            run_id,
            run_root,
            lifecycle,
            resume_lifecycle=True,
        )


__all__ = ["CoordinatorProcess", "CoordinatorProcessController", "RunControlManager"]
