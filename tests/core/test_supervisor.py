from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_foundry_core import (
    CodexExecConfig,
    CoordinatorIntegrityError,
    CoordinatorStatus,
    FoundrySupervisor,
    RunContext,
    SupervisorResult,
    SupervisorRepairResult,
    build_supervisor_prompt,
)
import auto_foundry_core.supervisor as supervisor_module


def _status(value: str, *, event_hash: str = "") -> CoordinatorStatus:
    return CoordinatorStatus(
        run_id="RUN-SUPERVISOR",
        generation_id="G-1",
        status=value,
        phase=value,
        next_action=None,
        owner=None,
        lease_expires_at=None,
        diagnostics=(),
        last_event_seq=0,
        last_event_hash=event_hash,
        publication_ready=False,
        publication_enabled=False,
    )


class _Coordinator:
    def __init__(self, initial: str = "waiting") -> None:
        self.current = _status(initial)
        self.calls: list[str] = []

    def run(self, *, max_steps=None):
        self.calls.append("run")
        if self.current.status == "ready":
            self.current = _status("complete")
        return self.current

    def status(self):
        return self.current

    def reopen(self, reason: str):
        self.calls.append("reopen")
        self.current = _status("ready")
        return self.current


def test_supervisor_is_dormant_for_clean_terminal(tmp_path: Path) -> None:
    coordinator = _Coordinator("complete")
    called: list[str] = []
    supervisor = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=coordinator,
        repository_root=tmp_path,
        agent=lambda *_args, **_kwargs: called.append("agent"),
    )

    result = supervisor.run()

    assert result.action == "dormant"
    assert result.status.status == "complete"
    assert called == []
    assert coordinator.calls == ["run"]


def test_supervisor_refreshes_completion_after_injected_fresh_process_repair(tmp_path: Path) -> None:
    coordinator = _Coordinator()
    seen: list[str] = []

    def agent(observation, **_kwargs):
        seen.append(observation.status)
        # The injected agent stands in for a fresh Python process that calls
        # public reopen/run after source repair.  The host must only refresh
        # this result, never call those lifecycle methods itself.
        coordinator.current = _status("complete", event_hash="fresh-process")
        return SupervisorRepairResult(repaired=True, tests_passed=True, durable_progress=True)

    supervisor = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=coordinator,
        repository_root=tmp_path,
        agent=agent,
    )
    result = supervisor.run()

    assert result.action == "repaired_and_refreshed"
    assert result.status.status == "complete"
    assert seen == ["waiting"]
    assert coordinator.calls == ["run"]


def test_supervisor_stops_when_the_same_durable_incident_repeats(tmp_path: Path) -> None:
    class StuckCoordinator(_Coordinator):
        def __init__(self):
            super().__init__()
            self.counter = 0

        def run(self, *, max_steps=None):
            self.calls.append("run")
            self.counter += 1
            self.current = _status("waiting", event_hash=f"event-{self.counter}")
            return self.current

    coordinator = StuckCoordinator()
    calls: list[str] = []
    supervisor = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=coordinator,
        repository_root=tmp_path,
        agent=lambda observation, **_kwargs: calls.append(observation.status) or SupervisorRepairResult(
            repaired=True,
            tests_passed=True,
            durable_progress=True,
        ),
    )

    def agent(observation, **_kwargs):
        calls.append(observation.status)
        # Simulate fresh-process reopen/run producing a new event hash while
        # leaving the same semantic incident unresolved.
        coordinator.current = _status("waiting", event_hash="fresh-event")
        return SupervisorRepairResult(repaired=True, tests_passed=True, durable_progress=True)

    supervisor.agent = agent
    result = supervisor.run()

    assert result.action == "repair_no_progress"
    assert calls == ["waiting"]
    assert coordinator.calls == ["run"]


def test_supervisor_prompt_allows_minimal_code_repair_but_forbids_run_state_edits(tmp_path: Path) -> None:
    observation = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=_Coordinator(),
        repository_root=tmp_path,
        agent=lambda *_args, **_kwargs: False,
    ).observe()
    prompt = build_supervisor_prompt(
        observation,
        context=RunContext("RUN-SUPERVISOR", tmp_path),
        repository_root=tmp_path,
    )

    assert "minimal technical repair" in prompt
    assert "run_state.json" in prompt
    assert "Do not commit, push, publish" in prompt
    assert "fresh Python process" in prompt
    assert prompt.index("coordinator.reopen") < prompt.index("coordinator.run")
    assert "blocked_by_evidence" in prompt
    assert "technical representation and mechanical recovery" in prompt
    assert "do not decide business identity" in prompt.lower()
    assert "analytical acceptance" in prompt
    assert "blocked/rethink outcomes" in prompt
    assert "lifecycle.resume()" in prompt
    assert "authorize integration/publication" in prompt


def test_repair_result_requires_three_explicit_true_fields() -> None:
    assert not SupervisorRepairResult(repaired=True, tests_passed=True).accepted
    assert not SupervisorRepairResult(repaired=True, tests_passed=True, durable_progress=1).accepted
    assert SupervisorRepairResult(repaired=True, tests_passed=True, durable_progress=True).accepted
    assert not supervisor_module._normalize_repair_result(True).accepted
    assert not supervisor_module._normalize_repair_result("repaired").accepted
    assert not supervisor_module._normalize_repair_result(supervisor_module.RoleExecution()).accepted
    assert not supervisor_module._normalize_repair_result(
        {"repaired": True, "tests_passed": True, "message": "ok"}
    ).accepted


def test_supervisor_result_serialization_discards_raw_transport_and_run_payloads(tmp_path: Path) -> None:
    sentinel = "RAW_SENTINEL_stdout_stderr_tool_arg_model_run"
    context = RunContext("RUN-SUPERVISOR", tmp_path)
    observation = FoundrySupervisor(
        context,
        coordinator=_Coordinator(),
        repository_root=tmp_path,
        agent=lambda *_args, **_kwargs: False,
    ).observe()
    observation = replace(
        observation,
        state={"raw": sentinel},
        events=({"tool_arguments": sentinel},),
        phase_snapshot={"model": sentinel},
        repository_status=(sentinel,),
        repository_diff_stat=sentinel,
        inspection_errors=(sentinel,),
        run_error=sentinel,
    )
    repair = SupervisorRepairResult(
        repaired=False,
        tests_passed=False,
        durable_progress=False,
        message=sentinel,
        execution=supervisor_module.RoleExecution(
            exit_code=1,
            output=sentinel,
            error=sentinel,
        ),
    )
    result = SupervisorResult(
        status=observation.coordinator,
        observation=observation,
        repair=repair,
        action="repair_declined",
        diagnostics=(sentinel,),
    )

    serialized = json.dumps(result.to_dict(), sort_keys=True)
    assert sentinel not in serialized
    assert "output" not in result.to_dict()["repair"]["execution"]
    assert "error" in result.to_dict()["repair"]["execution"]


def test_codex_exec_config_reasoning_round_trips_scalar_and_per_role() -> None:
    config = CodexExecConfig.from_dict(
        {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "role_models": {"foundry_supervisor": "gpt-5.6-sol"},
            "role_reasoning_efforts": {"foundry_supervisor": "high"},
        }
    )
    restored = CodexExecConfig.from_dict(config.to_dict())

    assert restored.to_dict() == config.to_dict()
    selected = restored.for_role("foundry_supervisor")
    assert selected.model == "gpt-5.6-sol"
    assert selected.reasoning_effort == "high"


def test_supervisor_preserves_unchanged_complete_with_limits_as_unrepaired(tmp_path: Path) -> None:
    coordinator = _Coordinator("complete_with_limits")
    calls: list[str] = []

    def agent(observation, **_kwargs):
        calls.append(observation.status)
        return SupervisorRepairResult(
            repaired=True,
            tests_passed=True,
            durable_progress=True,
            message="tests ran but status did not move",
        )

    result = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=coordinator,
        repository_root=tmp_path,
        agent=agent,
    ).run()

    assert calls == ["complete_with_limits"]
    assert result.status.status == "complete_with_limits"
    assert result.action == "repair_no_progress"
    assert not result.repaired


def test_supervisor_invokes_one_agent_even_when_fresh_status_is_a_distinct_attention_state(tmp_path: Path) -> None:
    coordinator = _Coordinator()
    calls: list[str] = []

    def agent(observation, **_kwargs):
        calls.append(observation.status)
        coordinator.current = _status("limited", event_hash="progress")
        return SupervisorRepairResult(repaired=True, tests_passed=True, durable_progress=True)

    result = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=coordinator,
        repository_root=tmp_path,
        agent=agent,
    ).run()

    assert calls == ["waiting"]
    assert result.action == "repair_no_progress"
    assert result.status.status == "limited"


def test_supervisor_diagnoses_ordinary_initial_run_exception(tmp_path: Path) -> None:
    class FailingCoordinator(_Coordinator):
        def run(self, *, max_steps=None):
            self.calls.append("run")
            raise RuntimeError("role transport unavailable")

    coordinator = FailingCoordinator()
    seen: list[str | None] = []

    def agent(observation, **_kwargs):
        seen.append(observation.run_error)
        coordinator.current = _status("complete", event_hash="repaired")
        return SupervisorRepairResult(repaired=True, tests_passed=True, durable_progress=True)

    result = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=coordinator,
        repository_root=tmp_path,
        agent=agent,
    ).run()

    assert result.action == "repaired_and_refreshed"
    assert seen and "role transport unavailable" in (seen[0] or "")
    assert coordinator.calls == ["run"]


def test_supervisor_does_not_repair_integrity_failure(tmp_path: Path) -> None:
    class IntegrityCoordinator(_Coordinator):
        def run(self, *, max_steps=None):
            self.calls.append("run")
            raise CoordinatorIntegrityError("tampered event chain")

    coordinator = IntegrityCoordinator()
    calls: list[str] = []
    supervisor = FoundrySupervisor(
        RunContext("RUN-SUPERVISOR", tmp_path),
        coordinator=coordinator,
        repository_root=tmp_path,
        agent=lambda *_args, **_kwargs: calls.append("agent"),
    )

    with pytest.raises(CoordinatorIntegrityError, match="tampered event chain"):
        supervisor.run()
    assert calls == []


def test_codex_supervisor_adapter_requires_strict_final_output_and_reasoning_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-SUPERVISOR", tmp_path)
    observation = FoundrySupervisor(
        context,
        coordinator=_Coordinator(),
        repository_root=tmp_path,
        agent=lambda *_args, **_kwargs: False,
    ).observe()
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "repaired": True,
                    "tests_passed": True,
                    "durable_progress": True,
                    "message": "fixed and resumed",
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            returncode=0,
            stdout="RAW_CODEX_JSONL_TOOL_ARGUMENT_MODEL_TEXT",
            stderr="RAW_CODEX_STDERR",
        )

    monkeypatch.setattr(supervisor_module.subprocess, "run", fake_run)
    adapter = supervisor_module.CodexSupervisorAdapter(
        context,
        tmp_path,
        CodexExecConfig(
            binary="fake-codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        ),
    )
    result = adapter(observation)

    argv = seen["argv"]
    assert result.accepted
    assert "--output-schema" in argv
    assert "--output-last-message" in argv
    assert ["--model", "gpt-5.6-sol"] == argv[argv.index("--model") : argv.index("--model") + 2]
    assert ["-c", "model_reasoning_effort=high"] == argv[argv.index("-c") : argv.index("-c") + 2]
    assert result.execution is not None
    assert result.execution.output == ""
    assert result.execution.error == ""


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"repaired": True, "tests_passed": True, "durable_progress": True},
        {"repaired": True, "tests_passed": False, "durable_progress": True, "message": "tests failed"},
        {"repaired": True, "tests_passed": True, "durable_progress": False, "message": "no progress"},
    ],
)
def test_codex_supervisor_adapter_rejects_missing_malformed_or_declined_final_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload,
) -> None:
    context = RunContext("RUN-SUPERVISOR", tmp_path)
    observation = FoundrySupervisor(
        context,
        coordinator=_Coordinator(),
        repository_root=tmp_path,
        agent=lambda *_args, **_kwargs: False,
    ).observe()

    def fake_run(argv, **kwargs):
        if payload is not None:
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps(payload), encoding="utf-8"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(supervisor_module.subprocess, "run", fake_run)
    result = supervisor_module.CodexSupervisorAdapter(context, tmp_path, CodexExecConfig(binary="fake-codex"))(observation)

    assert not result.accepted
