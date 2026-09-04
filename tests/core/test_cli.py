from __future__ import annotations

from types import SimpleNamespace
import json

import auto_foundry_core.cli as cli_module
from auto_foundry_core import CoordinatorStatus, RoleExecution, RunContext, SupervisorRepairResult, SupervisorResult
from auto_foundry_core.cli import _coordinator_exit_code, _print, _receipt_payload_hash


def test_supervisor_cli_exit_is_action_aware_but_preserves_terminal_outcomes() -> None:
    assert _coordinator_exit_code(
        SimpleNamespace(status="waiting"),
        operation="supervisor",
        action="repair_declined",
    ) == 4
    assert _coordinator_exit_code(
        SimpleNamespace(status="waiting"),
        operation="supervisor",
        action="repair_no_progress",
    ) == 4
    assert _coordinator_exit_code(
        SimpleNamespace(status="complete"),
        operation="supervisor",
        action="repaired_and_refreshed",
    ) == 0
    assert _coordinator_exit_code(
        SimpleNamespace(status="complete_with_limits"),
        operation="supervisor",
        action="repaired_and_refreshed",
    ) == 0
    assert _coordinator_exit_code(
        SimpleNamespace(status="complete_with_limits"),
        operation="supervisor",
        action="repair_no_progress",
    ) == 4


def test_supervisor_cli_json_does_not_echo_raw_transport_payload(capsys) -> None:
    sentinel = "RAW_SENTINEL_cli_stdout_stderr_tool_model"
    _print(
        SupervisorRepairResult(
            repaired=False,
            tests_passed=False,
            durable_progress=False,
            message=sentinel,
            execution=RoleExecution(exit_code=1, output=sentinel, error=sentinel),
        )
    )

    output = capsys.readouterr().out
    assert sentinel not in output
    assert "transport_failed" in json.loads(output)["execution"]["error"]


def test_supervisor_run_finally_serializes_coordinator_status_in_hashed_exit_receipt(tmp_path, monkeypatch) -> None:
    context = RunContext("RUN-CLI-SUPERVISOR", tmp_path / "run")
    coordinator_status = CoordinatorStatus(
        run_id=context.run_id,
        generation_id="G-0001",
        status="complete",
        phase="complete",
        next_action=None,
        owner=None,
        lease_expires_at=None,
        diagnostics=(),
        last_event_seq=0,
        last_event_hash="",
        publication_ready=False,
        publication_enabled=False,
    )

    class FakeCoordinator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self, _spec) -> None:
            return None

        def persisted_spec(self):
            return SimpleNamespace(codex_exec={})

    class FakeSupervisor:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, *, max_steps=None):
            return SupervisorResult(status=coordinator_status)

    class FakeHeartbeat:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    monkeypatch.setattr(cli_module, "_context", lambda _args: context)
    monkeypatch.setattr(cli_module, "_ensure_current_checkout", lambda: None)
    monkeypatch.setattr(cli_module, "_read_json", lambda _path: {})
    monkeypatch.setattr(cli_module, "RunCoordinator", FakeCoordinator)
    monkeypatch.setattr(cli_module, "FoundrySupervisor", FakeSupervisor)
    monkeypatch.setattr(cli_module, "_SupervisorHeartbeat", FakeHeartbeat)
    monkeypatch.setattr(
        cli_module,
        "_write_ready_receipt",
        lambda *_args, **_kwargs: {
            "specHash": "a" * 64,
            "roleRoutingHash": "b" * 64,
            "startupToken": None,
        },
    )
    monkeypatch.setattr(cli_module, "_required_process_start_token", lambda _pid: "process-start")

    assert cli_module.main(
        [
            "supervisor",
            "run",
            "--run-root",
            str(context.run_root),
            "--spec",
            "spec.json",
        ]
    ) == 0

    receipt_path = context.run_root / "control_plane" / "supervisor_exit.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "complete"
    unsigned = dict(receipt)
    payload_hash = unsigned.pop("payloadHash")
    assert payload_hash == _receipt_payload_hash(unsigned)
