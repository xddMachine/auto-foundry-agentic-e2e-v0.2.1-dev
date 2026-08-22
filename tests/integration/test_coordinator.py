from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from auto_foundry_core import (
    CodexExecConfig,
    CodexRoleAdapter,
    CoordinatorRunSpec,
    PlannerAction,
    RoleExecution,
    RunContext,
    RunCoordinator,
    build_role_prompt,
)
import auto_foundry_core.coordinator as coordinator_module


def _spec(run_id: str) -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://frozen",
        hashlib.sha256(b"frozen").hexdigest(),
        publication_policy={"enabled": True},
    )


class Planner:
    def __init__(self, action: PlannerAction):
        self.action = action
        self.advanced = False

    def next_actions(self, context: RunContext, state: dict):
        return () if self.advanced else (self.action,)


def test_plain_role_prompt_names_public_analytical_sequence(tmp_path: Path) -> None:
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-23", "next requirement")
    prompt = build_role_prompt(action, context=RunContext("RUN", tmp_path), idempotency_key="key")
    assert "begin_attempt" in prompt
    assert "submit_answer" in prompt
    assert "finish_attempt(status='completed')" in prompt
    assert "RoleResult" not in prompt
    assert "output-schema" not in prompt


def test_codex_role_transport_has_no_output_schema(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    class InputSink:
        def write(self, value: bytes) -> int:
            seen["input"] = value.decode("utf-8")
            return len(value)
        def flush(self) -> None: pass
        def close(self) -> None: pass

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = InputSink()
            self.stdout = io.BytesIO(
                (json.dumps({"type": "thread.started", "thread_id": "root-thread"}) + "\n" +
                 json.dumps({
                     "type": "item.started",
                     "item": {
                         "id": "collab-1", "type": "collab_tool_call", "tool": "spawn_agent",
                         "sender_thread_id": "root-thread",
                         "receiver_thread_ids": ["child-thread"],
                         "prompt": "PRIVATE PROMPT",
                         "agents_states": {"child-thread": {"status": "running", "message": "PRIVATE MESSAGE"}},
                     },
                 }) + "\n").encode("utf-8")
            )
            self.stderr = io.BytesIO(b"")
            self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def terminate(self): self.returncode = -15
        def kill(self): self.returncode = -9

    def fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        output_arg = list(argv)[list(argv).index("--output-last-message") + 1]
        Path(output_arg).write_text("last message", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", fake_popen)
    context = RunContext("RUN", tmp_path)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-23", "next requirement")
    execution = CodexRoleAdapter(context, CodexExecConfig(binary="fake-codex"))(action, idempotency_key="key", context=context)
    assert isinstance(execution, RoleExecution)
    assert execution.ok
    assert "--skip-git-repo-check" in seen["argv"]
    assert "--json" in seen["argv"]
    assert "--output-schema" not in seen["argv"]
    assert "begin_attempt" in str(seen["input"])
    lifecycle = [json.loads(line) for line in (tmp_path / "control_center/lifecycle_events.jsonl").read_text().splitlines()]
    assert lifecycle[0]["invocation_id"] == "child-thread"
    assert lifecycle[0]["parent_agent_id"] == "key"
    assert "PRIVATE" not in json.dumps(lifecycle)


def test_canonical_persisted_spec_wins_over_private_copy(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path)
    planner = Planner(PlannerAction("analyze_requirement", "analytical_owner", "REQ-23", "next requirement"))
    coordinator = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": lambda action, **_: RoleExecution()})
    coordinator.start(_spec("RUN"))
    private_copy = tmp_path / "private-spec.json"
    private_copy.write_text('{"run_id":"RUN","generation_id":"bad","planner_ref":"planner://bad","planner_hash":"' + hashlib.sha256(b"bad").hexdigest() + '"}', encoding="utf-8")
    resumed = RunCoordinator.from_persisted_spec(context, spec_path=private_copy, owner_id="new-owner")
    assert resumed.persisted_spec().generation_id == "G-0001"


def test_planner_advance_not_role_output_is_success_authority(tmp_path: Path) -> None:
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-23", "next requirement")
    planner = Planner(action)

    def role(action: PlannerAction, **kwargs: object) -> RoleExecution:
        planner.advanced = True
        return RoleExecution(exit_code=13, output="failed self-report")

    context = RunContext("RUN", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": role})
    coordinator.start(_spec("RUN"))
    status = coordinator.step()
    assert status.status == "waiting"
    assert any(item.get("event") == "planner_advanced" for item in [
        __import__("json").loads(line)
        for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    ])
