"""Thin top-level repair wrapper above the ordinary Foundry coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping, Protocol

from .agent_lifecycle import LifecycleEventWriter, normalize_codex_json_line
from .coordinator import (
    CoordinatorError,
    CoordinatorIntegrityError,
    CoordinatorStatus,
    CodexExecConfig,
    RoleExecution,
    RunCoordinator,
    _normalize_role_execution,
    _safe_text,
)
from .workspace import RunContext


SUPERVISOR_ROLE = "foundry_supervisor"
SUPERVISOR_ATTENTION_STATUSES = frozenset(
    {"waiting", "complete_with_limits", "blocked_rethink", "rethink", "failed", "technical_failure", "limited"}
)
SUPERVISOR_CLEAN_STATUSES = frozenset({"complete"})

# This is deliberately tiny and closed.  The Codex transport must return a
# final JSON object matching this shape; process exit status and telemetry are
# never treated as repair evidence.
SUPERVISOR_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repaired", "tests_passed", "durable_progress", "message"],
    "properties": {
        "repaired": {"type": "boolean"},
        "tests_passed": {"type": "boolean"},
        "durable_progress": {"type": "boolean"},
        "message": {"type": "string"},
    },
}


def _canonical(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _canonical(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_status(status: CoordinatorStatus) -> dict[str, Any]:
    """Return the small status projection safe for CLI/result serialization."""

    return {
        "run_id": status.run_id,
        "generation_id": status.generation_id,
        "status": status.status,
        "phase": status.phase,
        "owner": status.owner,
        "lease_expires_at": status.lease_expires_at,
        "publication_ready": bool(status.publication_ready),
        "publication_enabled": bool(status.publication_enabled),
        "no_progress_count": int(status.no_progress_count),
        "next_action_present": status.next_action is not None,
        "next_action_count": len(status.next_actions),
        "active_dispatch_count": len(status.active_dispatches),
        "diagnostic_count": len(status.diagnostics),
        "last_event_seq": int(status.last_event_seq),
        "has_last_event": bool(status.last_event_hash),
    }


def _safe_transport_status(execution: RoleExecution | None) -> dict[str, Any] | None:
    """Expose transport state without copying stdout/stderr or model text."""

    if execution is None:
        return None
    result: dict[str, Any] = {
        "exit_code": execution.exit_code,
        "timed_out": bool(execution.timed_out),
    }
    if execution.timed_out:
        result["error"] = "transport_timeout"
    elif execution.exit_code is None:
        result["error"] = "transport_unavailable"
    elif execution.exit_code != 0:
        result["error"] = "transport_failed"
    return result


def _public_final_message(result: "SupervisorRepairResult") -> str:
    """Keep the strict result envelope while never echoing model text."""

    if result.accepted:
        return "supervisor repair accepted"
    if result.repaired is False and result.tests_passed is False and result.durable_progress is False:
        return "supervisor repair declined"
    return "supervisor final result invalid"


def _public_diagnostics(values: tuple[str, ...]) -> list[str]:
    """Return bounded diagnostic categories rather than arbitrary role text."""

    if not values:
        return []
    return ["supervisor diagnostic available"]


def _git_inspection(repo_root: Path) -> tuple[tuple[str, ...], str, str | None]:
    """Read repository status only; repairs preserve dirty work by contract."""

    try:
        status = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD", "--"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (), "", f"git inspection unavailable: {exc}"
    if status.returncode != 0:
        return (), diff.stdout.strip(), _safe_text(status.stderr or "git status failed")
    return tuple(line for line in status.stdout.splitlines() if line.strip()), diff.stdout.strip(), None


@dataclass(frozen=True)
class SupervisorObservation:
    """Verified coordinator projection plus read-only repository evidence."""

    coordinator: CoordinatorStatus
    state: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[Mapping[str, Any], ...] = ()
    phase_snapshot: Mapping[str, Any] = field(default_factory=dict)
    repository_root: str = ""
    repository_status: tuple[str, ...] = ()
    repository_diff_stat: str = ""
    inspection_errors: tuple[str, ...] = ()
    run_error: str | None = None
    observed_at: str = ""

    @property
    def status(self) -> str:
        return self.coordinator.status

    @property
    def needs_attention(self) -> bool:
        return self.status in SUPERVISOR_ATTENTION_STATUSES

    def _diagnostic_dict(self) -> dict[str, Any]:
        return {
            "coordinator": self.coordinator.to_dict(),
            "state": dict(_canonical(self.state)),
            "events": [dict(_canonical(item)) for item in self.events],
            "phase_snapshot": dict(_canonical(self.phase_snapshot)),
            "repository_root": self.repository_root,
            "repository_status": list(self.repository_status),
            "repository_diff_stat": self.repository_diff_stat,
            "inspection_errors": list(self.inspection_errors),
            "run_error": self.run_error,
            "observed_at": self.observed_at,
            "needs_attention": self.needs_attention,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a sanitized projection for CLI/results; prompt uses diagnostics privately."""

        return {
            "coordinator": _public_status(self.coordinator),
            "state_present": bool(self.state),
            "state_key_count": len(self.state),
            "event_count": len(self.events),
            "phase_present": bool(self.phase_snapshot),
            "repository_root": self.repository_root,
            "repository_status_count": len(self.repository_status),
            "repository_diff_present": bool(self.repository_diff_stat),
            "inspection_error_count": len(self.inspection_errors),
            "run_error_present": self.run_error is not None,
            "observed_at": self.observed_at,
            "needs_attention": self.needs_attention,
        }


@dataclass(frozen=True)
class SupervisorRepairResult:
    """Technical transport result; it is never a business answer."""

    repaired: bool = False
    tests_passed: bool | None = None
    durable_progress: bool | None = None
    message: str = ""
    execution: RoleExecution | None = None

    @property
    def accepted(self) -> bool:
        # Every field is required evidence.  In particular, exit 0, a role
        # execution object, or a truthy string cannot authorize continuation.
        return self.repaired is True and self.tests_passed is True and self.durable_progress is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "repaired": self.repaired,
            "tests_passed": self.tests_passed,
            "durable_progress": self.durable_progress,
            "message": _public_final_message(self),
            "execution": _safe_transport_status(self.execution),
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class SupervisorResult:
    status: CoordinatorStatus
    observation: SupervisorObservation | None = None
    repair: SupervisorRepairResult | None = None
    action: str = "dormant"
    diagnostics: tuple[str, ...] = ()

    @property
    def repaired(self) -> bool:
        # A strict role contract is necessary but not sufficient.  The host
        # reports a repair only after the refreshed durable projection moved
        # (or reached a terminal); an unchanged incident is no-progress.
        return self.repair is not None and self.repair.accepted and self.action == "repaired_and_refreshed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": _public_status(self.status),
            "observation": self.observation.to_dict() if self.observation else None,
            "repair": self.repair.to_dict() if self.repair else None,
            "action": self.action,
            "diagnostics": _public_diagnostics(self.diagnostics),
            "repaired": self.repaired,
        }


class SupervisorAgent(Protocol):
    def __call__(self, observation: SupervisorObservation, *, prompt: str | None = None) -> Any: ...


def _normalize_repair_result(value: Any) -> SupervisorRepairResult:
    if isinstance(value, SupervisorRepairResult):
        return value
    if isinstance(value, RoleExecution):
        return SupervisorRepairResult(
            repaired=False,
            tests_passed=False,
            durable_progress=False,
            message=value.error or value.output,
            execution=value,
        )
    if isinstance(value, Mapping):
        execution = (
            _normalize_role_execution(value)
            if any(key in value for key in ("exit_code", "returncode", "output", "stdout", "error", "stderr", "timed_out"))
            else None
        )
        return _parse_supervisor_result(value, execution=execution)
    # Strings, booleans, and ordinary role transport values are diagnostic
    # only.  They intentionally remain unaccepted.
    return SupervisorRepairResult()


def _parse_supervisor_result(payload: Any, *, execution: RoleExecution | None = None) -> SupervisorRepairResult:
    """Strictly decode the final JSON object emitted by the supervisor role."""

    if not isinstance(payload, Mapping):
        return SupervisorRepairResult(
            message="supervisor final result must be a JSON object",
            execution=execution,
        )
    required = {"repaired", "tests_passed", "durable_progress", "message"}
    if set(payload) != required:
        missing = sorted(required - set(payload))
        extra = sorted(set(payload) - required)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("unexpected=" + ",".join(extra))
        return SupervisorRepairResult(
            message="supervisor final result schema mismatch: " + "; ".join(detail),
            execution=execution,
        )
    if any(type(payload[name]) is not bool for name in ("repaired", "tests_passed", "durable_progress")):
        return SupervisorRepairResult(
            message="supervisor final result booleans must be explicit JSON booleans",
            execution=execution,
        )
    if not isinstance(payload["message"], str):
        return SupervisorRepairResult(message="supervisor final result message must be a string", execution=execution)
    return SupervisorRepairResult(
        repaired=payload["repaired"],
        tests_passed=payload["tests_passed"],
        durable_progress=payload["durable_progress"],
        message=payload["message"],
        execution=execution,
    )


def build_supervisor_prompt(
    observation: SupervisorObservation,
    *,
    context: RunContext,
    repository_root: Path,
    custom: str | None = None,
) -> str:
    """Build the one top-level repair contract sent to Codex or a test agent."""

    mandatory = (
        "You are the top-level Foundry Supervisor for one bounded local run.\n"
        "The ordinary RunCoordinator and Requirement Planner remain authoritative. You are not a Planner, "
        "Analytical Owner, Business Reviewer, integration reviewer, or business-answer authority.\n"
        "Inspect the verified run state/event tail, phase projection, repository status, and relevant source. "
        "If a root technical cause is supported, make one minimal technical repair to repository source, tests, or "
        "configuration and run focused tests. Preserve all pre-existing dirty changes and untracked files; "
        "never reset, stash, checkout, clean, or overwrite unrelated work. Run-local state may change only "
        "through the public APIs below, never by editing JSON/state/artifact files directly.\n"
        "Never fabricate business semantics or rewrite run-owned JSON/state/artifacts directly, including run_state.json, "
        "item_state.json, planner files, control_plane/coordinator_state.json, control_plane/coordinator_events.jsonl, "
        "review packets, integration records, manifests, telemetry, or report internals. Do not commit, push, "
        "publish, delegate, add another planner/reviewer, or create a scheduler journal. Preserve genuine "
        "technical_failure, complete_with_limits, blocked_by_evidence, and other semantic/evidence outcomes; "
        "never turn a business insufficiency or error into synthetic success.\n"
        "Supervisor repairs are limited to technical representation and mechanical recovery: stale pointers, "
        "schema/hash/serialization defects, CAS or lease residue, transport startup/exit receipts, and other "
        "reproducible runtime failures. Do not decide business identity, merge or split semantic domains, "
        "interpret analytical evidence, accept/reject a business answer, or authorize integration/publication. "
        "Identity conflicts, analytical acceptance, and other semantic disagreements must remain explicit "
        "blocked/rethink outcomes for the owning Planner/reviewer role; a Supervisor may only report the "
        "verified evidence and repair the mechanical boundary around them.\n"
        "If no safe repair is supported, report that and leave the checkout/run unchanged. After code repair and "
        "focused tests, you MUST start a fresh Python process using the repository src (for example, "
        "PYTHONPATH=<repository-root>/src python3 -c ...), load the public RunContext and RunLifecycle, "
        "call lifecycle.resume() when the run is paused, then load the persisted RunCoordinator, call public "
        "coordinator.reopen(...) and then coordinator.run(...), and continue "
        "observing public status in that fresh process until a clean terminal or a genuine semantic/evidence "
        "block. Do not use stale in-memory coordinator objects. Exit nonzero when no safe repair, tests, or "
        "durable progress is available; exit 0 only after an actual minimal repair, focused tests, and durable "
        "run progress/clean terminal. Your final response MUST be exactly one JSON object with only these keys: "
        "repaired (boolean), tests_passed (boolean), durable_progress (boolean), and message (string). Set all "
        "three booleans to true only when the repair and focused tests completed and the fresh public process "
        "observed durable progress or a clean terminal; otherwise exit nonzero and set them false."
    )
    prefix = f"{custom.strip()}\n\n" if isinstance(custom, str) and custom.strip() else ""
    payload = json.dumps(observation._diagnostic_dict(), sort_keys=True, ensure_ascii=False, indent=2)
    return (
        prefix
        + mandatory
        + "\n\n"
        + f"Repository root: {repository_root}\nRun root: {context.run_root}\nRun id: {context.run_id}\n"
        + "Verified observation (diagnostic input only):\n"
        + payload
        + "\n"
    )


class CodexSupervisorAdapter:
    """Small Codex transport with repository cwd and run-root add-dir."""

    def __init__(
        self,
        context: RunContext,
        repository_root: str | Path,
        config: CodexExecConfig | Mapping[str, Any] | None = None,
        *,
        require_skill_binding: bool = False,
    ) -> None:
        self.context = context
        self.repository_root = Path(repository_root).expanduser().resolve(strict=True)
        if self.repository_root.is_symlink() or not self.repository_root.is_dir():
            raise CoordinatorIntegrityError("supervisor repository root must be a regular directory")
        self.config = config if isinstance(config, CodexExecConfig) else CodexExecConfig.from_dict(config)
        self.require_skill_binding = bool(require_skill_binding)
        if self.require_skill_binding:
            self.config.validate_skill_binding(
                required=True,
                verify_active=True,
                repo_root=self.repository_root,
                role_cwd=self.repository_root,
            )

    def __call__(self, observation: SupervisorObservation, *, prompt: str | None = None) -> SupervisorRepairResult:
        try:
            config = self.config.for_role(SUPERVISOR_ROLE)
            if self.require_skill_binding:
                config.validate_skill_binding(
                    required=True,
                    verify_active=True,
                    repo_root=self.repository_root,
                    role_cwd=self.repository_root,
                )
        except CoordinatorIntegrityError as exc:
            return SupervisorRepairResult(repaired=False, tests_passed=False, message=f"skill binding rejected: {exc}")
        final_prompt = prompt or build_supervisor_prompt(
            observation,
            context=self.context,
            repository_root=self.repository_root,
            custom=config.role_prompts.get(SUPERVISOR_ROLE),
        )
        argv = [config.binary, "exec", "--skip-git-repo-check"]
        if config.ephemeral:
            argv.append("--ephemeral")
        argv.extend(["--sandbox", config.sandbox])
        if config.model:
            argv.extend(["--model", config.model])
        if config.profile:
            argv.extend(["--profile", config.profile])
        if config.reasoning_effort:
            argv.extend(["-c", f"model_reasoning_effort={config.reasoning_effort}"])
        with tempfile.TemporaryDirectory(prefix="auto-foundry-supervisor-") as temporary:
            temporary_root = Path(temporary)
            schema_path = temporary_root / "supervisor-result.schema.json"
            output_path = temporary_root / "supervisor-result.json"
            schema_path.write_text(
                json.dumps(SUPERVISOR_RESULT_SCHEMA, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            argv.extend(
                [
                    "--add-dir",
                    str(self.context.run_root),
                    "--json",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ]
            )
            try:
                completed = subprocess.run(
                    argv,
                    input=final_prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=config.timeout_seconds,
                    cwd=self.repository_root,
                )
            except subprocess.TimeoutExpired as exc:
                execution = RoleExecution(
                    exit_code=None,
                    timed_out=True,
                )
                return SupervisorRepairResult(
                    repaired=False,
                    tests_passed=False,
                    durable_progress=False,
                    message="supervisor codex invocation timed out",
                    execution=execution,
                )
            except Exception as exc:
                execution = RoleExecution(exit_code=1)
                return SupervisorRepairResult(
                    repaired=False,
                    tests_passed=False,
                    durable_progress=False,
                    message="supervisor codex invocation failed",
                    execution=execution,
                )

            # The stream is diagnostic only; normalize rows into the same
            # run-local lifecycle telemetry used by ordinary Codex roles.
            try:
                writer = LifecycleEventWriter(self.context.run_root)
                root_thread: str | None = None
                raw_stdout = (
                    completed.stdout
                    if isinstance(completed.stdout, bytes)
                    else str(completed.stdout or "").encode("utf-8", "replace")
                )
                for line in raw_stdout.splitlines(keepends=True):
                    try:
                        root_thread, rows = normalize_codex_json_line(
                            line,
                            root_thread=root_thread,
                            root_invocation_id=f"supervisor:{self.context.run_id}",
                        )
                        for row in rows:
                            writer.append(row)
                    except Exception:
                        continue
            except Exception:
                pass
            execution = RoleExecution(
                exit_code=completed.returncode,
            )
            if not output_path.is_file() or output_path.is_symlink():
                return SupervisorRepairResult(
                    repaired=False,
                    tests_passed=False,
                    durable_progress=False,
                    message="supervisor final result is missing",
                    execution=execution,
                )
            try:
                final_payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                return SupervisorRepairResult(
                    repaired=False,
                    tests_passed=False,
                    durable_progress=False,
                    message="supervisor final result is malformed",
                    execution=execution,
                )
            result = _parse_supervisor_result(final_payload, execution=execution)
            if not execution.ok:
                return SupervisorRepairResult(
                    repaired=False,
                    tests_passed=False,
                    durable_progress=False,
                    message="supervisor exited nonzero",
                    execution=execution,
                )
            return SupervisorRepairResult(
                repaired=result.repaired,
                tests_passed=result.tests_passed,
                durable_progress=result.durable_progress,
                message=_public_final_message(result),
                execution=execution,
            )


class FoundrySupervisor:
    """One bounded pass above the ordinary coordinator."""

    def __init__(
        self,
        context: RunContext,
        *,
        coordinator: RunCoordinator | Any | None = None,
        agent: SupervisorAgent | None = None,
        repository_root: str | Path | None = None,
        codex_config: CodexExecConfig | Mapping[str, Any] | None = None,
        require_skill_binding: bool = False,
    ) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("FoundrySupervisor requires a RunContext")
        self.context = context
        self.repository_root = (
            Path(repository_root).expanduser().resolve(strict=True)
            if repository_root is not None
            else Path(__file__).resolve().parents[2]
        )
        if self.repository_root.is_symlink() or not self.repository_root.is_dir():
            raise CoordinatorIntegrityError("supervisor repository root must be a regular directory")
        self.coordinator = coordinator or RunCoordinator(context)
        if agent is not None and not callable(agent) and not callable(getattr(agent, "dispatch", None)):
            raise TypeError("supervisor agent must be callable")
        self.agent: Any = agent or CodexSupervisorAdapter(
            context,
            self.repository_root,
            codex_config,
            require_skill_binding=require_skill_binding,
        )

    def _status(self) -> CoordinatorStatus:
        value = self.coordinator.status()
        if isinstance(value, CoordinatorStatus):
            return value
        if isinstance(value, Mapping) and {"run_id", "generation_id", "status", "phase"}.issubset(value):
            return CoordinatorStatus(
                run_id=str(value["run_id"]),
                generation_id=str(value["generation_id"]),
                status=str(value["status"]),
                phase=str(value["phase"]),
                next_action=value.get("next_action"),
                owner=value.get("owner"),
                lease_expires_at=value.get("lease_expires_at"),
                diagnostics=tuple(value.get("diagnostics") or ()),
                last_event_seq=int(value.get("last_event_seq", 0) or 0),
                last_event_hash=str(value.get("last_event_hash", "")),
                publication_ready=bool(value.get("publication_ready", False)),
                publication_enabled=bool(value.get("publication_enabled", False)),
                no_progress_count=int(value.get("no_progress_count", 0) or 0),
                next_actions=tuple(value.get("next_actions") or ()),
                active_dispatches=tuple(value.get("active_dispatches") or ()),
            )
        raise TypeError("coordinator.status() must return CoordinatorStatus")

    def observe(
        self,
        status: CoordinatorStatus | None = None,
        *,
        run_error: str | None = None,
    ) -> SupervisorObservation:
        current = status or self._status()
        state: Mapping[str, Any] = {}
        events: tuple[Mapping[str, Any], ...] = ()
        errors: list[str] = []
        reader = getattr(self.coordinator, "_read_replay", None)
        if callable(reader):
            try:
                with self.coordinator._locked(create=False):  # type: ignore[attr-defined]
                    replayed, replay_events = reader()
                if isinstance(replayed, Mapping):
                    state = dict(replayed)
                events = tuple(dict(item) for item in replay_events[-32:] if isinstance(item, Mapping))
                verifier = getattr(self.coordinator, "_verify_planner_binding", None)
                if callable(verifier):
                    verifier(state)
            except CoordinatorError:
                raise
            except Exception as exc:
                errors.append(f"durable inspection unavailable: {exc}")
        phase: Mapping[str, Any] = {}
        phase_reader = getattr(self.coordinator, "_phase_snapshot", None)
        if callable(phase_reader):
            try:
                value = phase_reader()
                if isinstance(value, Mapping):
                    phase = dict(value)
            except Exception as exc:
                errors.append(f"phase snapshot unavailable: {exc}")
        status_lines, diff_stat, git_error = _git_inspection(self.repository_root)
        if git_error:
            errors.append(git_error)
        return SupervisorObservation(
            coordinator=current,
            state=state,
            events=events,
            phase_snapshot=phase,
            repository_root=str(self.repository_root),
            repository_status=status_lines,
            repository_diff_stat=diff_stat,
            inspection_errors=tuple(errors),
            run_error=run_error,
            observed_at=_now(),
        )

    def _invoke_agent(
        self,
        status: CoordinatorStatus,
        *,
        observation: SupervisorObservation | None = None,
        force: bool = False,
    ) -> SupervisorResult:
        observation = observation or self.observe(status)
        if not force and not observation.needs_attention:
            return SupervisorResult(status=status, observation=observation, action="dormant")
        config = getattr(self.agent, "config", None)
        custom = config.role_prompts.get(SUPERVISOR_ROLE) if isinstance(config, CodexExecConfig) else None
        prompt = build_supervisor_prompt(
            observation,
            context=self.context,
            repository_root=self.repository_root,
            custom=custom,
        )
        try:
            dispatcher = getattr(self.agent, "dispatch", None)
            if callable(dispatcher):
                try:
                    raw = dispatcher(observation, prompt=prompt)
                except TypeError as exc:
                    if "prompt" not in str(exc):
                        raise
                    raw = dispatcher(observation)
            else:
                try:
                    raw = self.agent(observation, prompt=prompt)
                except TypeError as exc:
                    if "prompt" not in str(exc):
                        raise
                    raw = self.agent(observation)
            repair = _normalize_repair_result(raw)
        except Exception as exc:
            repair = SupervisorRepairResult(
                repaired=False,
                tests_passed=False,
                durable_progress=False,
                message=f"supervisor agent failed: {exc}",
            )
            return SupervisorResult(status=status, observation=observation, repair=repair, action="repair_failed", diagnostics=(repair.message,))
        if not repair.accepted:
            diagnostic = repair.message or "supervisor did not produce a tested repair"
            return SupervisorResult(status=status, observation=observation, repair=repair, action="repair_declined", diagnostics=(diagnostic,))
        # The dedicated Supervisor process owns the repair-and-continue
        # transaction.  The host must not call reopen/run on this stale Python
        # object after source files have changed.
        return SupervisorResult(status=status, observation=observation, repair=repair, action="repair_completed")

    @staticmethod
    def _incident_fingerprint(status: CoordinatorStatus) -> tuple[str, str, str, str, str]:
        next_action = json.dumps(status.next_action or {}, sort_keys=True, ensure_ascii=False)
        latest_diagnostic: Mapping[str, Any] = {}
        if status.diagnostics and isinstance(status.diagnostics[-1], Mapping):
            latest_diagnostic = status.diagnostics[-1]
        diagnostic = json.dumps(
            {
                key: latest_diagnostic.get(key)
                for key in ("kind", "reason", "error", "message", "action")
                if latest_diagnostic.get(key) is not None
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        # Event hashes change whenever the fresh Supervisor process reopens;
        # they are evidence, not a stable semantic incident identity.
        return (status.generation_id, status.status, status.phase, next_action, diagnostic)

    def _refresh_status_from_disk(self) -> CoordinatorStatus:
        """Reload the coordinator projection without executing lifecycle work."""

        if isinstance(self.coordinator, RunCoordinator):
            owner_id = getattr(self.coordinator, "owner_id", None)
            refreshed = RunCoordinator.from_persisted_spec(self.context, owner_id=owner_id)
            self.coordinator = refreshed
            return refreshed.status()
        status = getattr(self.coordinator, "status", None)
        if callable(status):
            value = status()
            if isinstance(value, CoordinatorStatus):
                return value
        return self._status()

    def run(self, *, max_steps: int | None = None) -> SupervisorResult:
        """Run the coordinator once, then give one repair transaction one refresh."""

        run_error: str | None = None
        try:
            status = self.coordinator.run(max_steps=max_steps)
        except CoordinatorIntegrityError:
            # A failed integrity check is not diagnosable by a repair role.
            # Keep this fail-closed and do not invoke the agent.
            raise
        except Exception as exc:
            run_error = f"ordinary coordinator run failed: {exc}"
            try:
                status = self._status()
            except CoordinatorIntegrityError:
                raise
        observation = self.observe(status, run_error=run_error)
        if status.status in SUPERVISOR_CLEAN_STATUSES and run_error is None:
            return SupervisorResult(status=status, observation=observation, action="dormant")
        if not observation.needs_attention and run_error is None:
            return SupervisorResult(status=status, observation=observation, action="dormant")
        fingerprint = self._incident_fingerprint(status)
        result = self._invoke_agent(status, observation=observation, force=run_error is not None)
        if result.repair is None or not result.repair.accepted or result.action != "repair_completed":
            diagnostics = list(result.diagnostics)
            if run_error:
                diagnostics.insert(0, run_error)
            return SupervisorResult(
                status=result.status,
                observation=result.observation,
                repair=result.repair,
                action=result.action,
                diagnostics=tuple(diagnostics),
            )
        try:
            refreshed_status = self._refresh_status_from_disk()
        except CoordinatorIntegrityError:
            raise
        except Exception as exc:
            diagnostic = f"verified coordinator refresh after fresh Supervisor failed: {exc}"
            return SupervisorResult(
                status=result.status,
                observation=result.observation,
                repair=result.repair,
                action="refresh_failed",
                diagnostics=(diagnostic,),
            )
        refreshed_observation = self.observe(refreshed_status)
        refreshed_fingerprint = self._incident_fingerprint(refreshed_status)
        # Event hashes are intentionally excluded from the fingerprint.  A
        # fresh reopen may append events without making any semantic progress.
        progressed = refreshed_fingerprint != fingerprint
        if not progressed:
            return SupervisorResult(
                status=refreshed_status,
                observation=refreshed_observation,
                repair=result.repair,
                action="repair_no_progress",
                diagnostics=("fresh Supervisor exited without durable run progress",),
            )
        # A limited terminal is a valid disclosed run outcome, not synthetic
        # success.  It is still progress when reached from a prior incident.
        return SupervisorResult(
            status=refreshed_status,
            observation=refreshed_observation,
            repair=result.repair,
            action="repaired_and_refreshed",
        )


__all__ = [
    "SUPERVISOR_ROLE",
    "SUPERVISOR_ATTENTION_STATUSES",
    "SUPERVISOR_CLEAN_STATUSES",
    "SupervisorObservation",
    "SupervisorRepairResult",
    "SupervisorResult",
    "SupervisorAgent",
    "CodexSupervisorAdapter",
    "FoundrySupervisor",
    "build_supervisor_prompt",
]
