from __future__ import annotations

import hashlib
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Mapping

import pytest

from auto_foundry_core import (
    CoordinatorConflictError,
    CoordinatorIntegrityError,
    CoordinatorRunSpec,
    PlannerAction,
    RoleExecution,
    RunContext,
    RunCoordinator,
)
import auto_foundry_core.coordinator as coordinator_module


class _InputSink:
    def __init__(self, capture: dict[str, object]) -> None:
        self.capture = capture

    def write(self, value: bytes) -> int:
        self.capture["input"] = value.decode("utf-8")
        return len(value)

    def flush(self) -> None: pass
    def close(self) -> None: pass


class _FakeCodexProcess:
    def __init__(self, capture: dict[str, object]) -> None:
        self.stdin = _InputSink(capture)
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.returncode = 0

    def wait(self, timeout=None): return self.returncode
    def terminate(self): self.returncode = -15
    def kill(self): self.returncode = -9


def _spec(run_id: str = "RUN-COORD") -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
    )


def _rebound_spec(run_id: str, generation_id: str, planner_ref: str) -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        generation_id,
        planner_ref,
        hashlib.sha256(planner_ref.encode("utf-8")).hexdigest(),
    )


def _bound_spec(run_id: str, binding: Mapping[str, str]) -> CoordinatorRunSpec:
    return CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        # The public migration is binding-only: an old unbound spec must not
        # gain a new binary/sandbox/model setting as a side effect.
        codex_exec=dict(binding),
    )


def _install_test_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    code_home = tmp_path / "codex-home"
    skill_path = code_home / "skills" / coordinator_module.PRODUCTION_SKILL_NAME
    _write_synthetic_skill(
        skill_path,
        skill_version=coordinator_module.PRODUCTION_SKILL_VERSION,
        core_version=coordinator_module.PRODUCTION_CORE_VERSION,
        release=coordinator_module.PRODUCTION_RELEASE,
    )
    expected_hash = hashlib.sha256(coordinator_module._skill_release_bytes(skill_path)).hexdigest()
    monkeypatch.setattr(coordinator_module, "PRODUCTION_SKILL_SHA256", expected_hash)
    monkeypatch.setenv("CODEX_HOME", str(code_home))
    return skill_path


def _write_synthetic_skill(
    skill_path: Path,
    *,
    skill_version: str,
    core_version: str,
    release: str,
) -> None:
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "SKILL.md").write_text(
        "---\n"
        f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n"
        "description: synthetic test fixture\n"
        "metadata:\n"
        f"  version: \"{skill_version}\"\n"
        "  core_name: auto_foundry_core\n"
        f"  core_version: \"{core_version}\"\n"
        f"  release: {release}\n"
        "---\n\n"
        f"skill_name: {coordinator_module.PRODUCTION_SKILL_NAME}\n"
        f"skill_version: {skill_version}\n"
        "core_name: auto_foundry_core\n"
        f"core_version: {core_version}\n",
        encoding="utf-8",
    )
    (skill_path / "README.md").write_text("synthetic release fixture\n", encoding="utf-8")


class QueuePlanner:
    def __init__(self, *offers: tuple[PlannerAction, ...]):
        self.offers = list(offers)
        self.calls = 0

    def next_actions(self, context: RunContext, state: dict) -> tuple[PlannerAction, ...]:
        self.calls += 1
        return self.offers.pop(0) if self.offers else ()


def _seed_active_dispatch(coordinator: RunCoordinator, action: PlannerAction, *, runner_pid: int | None = None) -> str:
    with coordinator._locked(create=False):
        state, _ = coordinator._read_replay()
        assert state is not None
        key = coordinator._idempotency_key(state, action)
        entry = {
            "action": action.to_dict(),
            "idempotency_key": key,
            "slot_key": coordinator._slot_key(action),
        }
        if runner_pid is not None:
            entry.update({"runner_id": "dead-owner", "runner_pid": runner_pid})
        state["active_dispatches"] = [entry]
        state["status"] = "dispatching"
        state["phase"] = action.action
        coordinator._append_event_locked(
            state,
            "dispatch_started",
            {"action": action.to_dict(), "idempotency_key": key},
        )
        return key


def _claim_process_worker(
    run_root: str,
    run_id: str,
    owner_id: str,
    action: PlannerAction,
    barrier: Any,
    release: Any,
    marker_path: str,
) -> None:
    context = RunContext(run_id, Path(run_root))

    class Planner:
        def next_actions(self, context: RunContext, state: dict) -> tuple[PlannerAction, ...]:
            return (action,)

    def role(current: PlannerAction, *, idempotency_key: str, **_: object) -> RoleExecution:
        with Path(marker_path).open("a", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()}:{idempotency_key}\n")
            stream.flush()
        release.wait(5)
        return RoleExecution()

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={action.action: role},
        owner_id=owner_id,
    )
    barrier.wait(5)
    coordinator.step()
    coordinator.close(wait_for_roles=True)


def _rebind_process_worker(
    run_root: str,
    run_id: str,
    generation_id: str,
    planner_ref: str,
    barrier: Any,
    result_path: str,
) -> None:
    context = RunContext(run_id, Path(run_root))
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()), owner_id=generation_id)
    barrier.wait(5)
    try:
        status = coordinator.publish_and_rebind(
            _rebound_spec(run_id, generation_id, planner_ref),
            lambda _spec: None,
        )
        Path(result_path).write_text(f"ok:{status.status}", encoding="utf-8")
    except CoordinatorConflictError as exc:
        Path(result_path).write_text(f"conflict:{exc}", encoding="utf-8")


def _reconciliation_race_worker(
    run_root: str,
    run_id: str,
    owner_id: str,
    offered: PlannerAction,
    hold_planner_read: bool,
    planner_read: Any,
    release_planner_read: Any,
    started: Any,
    finished: Any,
    release_role: Any,
    marker_path: str,
) -> None:
    context = RunContext(run_id, Path(run_root))

    class Planner:
        def next_actions(self, context: RunContext, state: dict) -> tuple[PlannerAction, ...]:
            if hold_planner_read:
                planner_read.set()
                assert release_planner_read.wait(5)
            return (offered,)

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        Path(marker_path).write_text(action.subject_id, encoding="utf-8")
        if hold_planner_read:
            assert release_role.wait(5)
        return RoleExecution()

    coordinator = RunCoordinator(
        context,
        planner_provider=Planner(),
        adapters={offered.action: role},
        owner_id=owner_id,
    )
    started.set()
    try:
        coordinator.step()
    finally:
        coordinator.close(wait_for_roles=True)
        finished.set()


def _action(name: str = "analyze_requirement", subject: str = "REQ-1") -> PlannerAction:
    return PlannerAction(name, "analytical_owner", subject, "test action")


def test_role_guidance_finalization_precedes_business_reviewer() -> None:
    action = PlannerAction("finalize_requirement_review", "business_reviewer", "REQ-1", "finalize")

    guidance = coordinator_module._role_guidance(action)

    assert guidance.startswith("Finalization sequence:")
    assert "Business reviewer sequence" not in guidance


def test_role_guidance_build_product_candidate_uses_public_refs_sequence() -> None:
    action = PlannerAction("build_product_candidate", "product_agent", "PRODUCT", "build")

    guidance = coordinator_module._role_guidance(action)

    expected_order = [
        "write_business_presentation_plan_v2",
        "record_business_presentation_plan_v2",
        "presentation_plan_ref",
        "discard_stale_product_candidate",
        "ProductCandidate",
        "ProductReviewStore.record_candidate",
    ]
    positions = [guidance.index(value) for value in expected_order]
    assert positions == sorted(positions)
    assert "when the business presentation plan is absent" in guidance
    assert "Only after canonical assembly" in guidance


def test_role_prompt_forbids_nested_agents_and_source_edits(tmp_path: Path) -> None:
    prompt = coordinator_module.build_role_prompt(
        _action(),
        context=RunContext("RUN-COORD", tmp_path),
        idempotency_key="key",
    )

    assert "Do not spawn or delegate subagents." in prompt
    assert "Do not edit repository source, tests, or configuration files" in prompt
    assert "make all run changes through public APIs" in prompt


@pytest.mark.parametrize("action_name", ["resolve_identity", "resume_identity_resolution"])
def test_entity_resolution_owner_custom_prompt_keeps_mandatory_contract_last(
    tmp_path: Path,
    action_name: str,
) -> None:
    action = PlannerAction(action_name, "entity_resolution_owner", "domain-1", "resolve")
    custom = "CUSTOM OWNER CONTEXT"
    prompt = coordinator_module.build_role_prompt(
        action,
        context=RunContext("RUN-COORD", tmp_path),
        idempotency_key="key",
        custom=custom,
    )
    mandatory = "Entity Resolution Owner sequence:"
    assert prompt.index(custom) < prompt.index(mandatory)
    assert "current_scope" in prompt
    assert "record_scope_discovery" in prompt
    assert "expected_scope_hash=current_scope.scope_hash" in prompt
    assert "stale scope" in prompt


def test_role_guidance_reporting_finalization_precedes_role_fallback() -> None:
    action = PlannerAction("finalize_final_report", "reporting_agent", "REPORT", "finalize")

    guidance = coordinator_module._role_guidance(action)

    assert guidance.startswith("Reporting finalization sequence:")
    assert "RunReportInputGatherer.gather_from_run(context, persist=True)" in guidance
    assert "load the persisted preflight" in guidance
    assert "RunReportFinalizer.finalize" in guidance
    assert "Do not author timings, incidents, reviews, or implementation hashes" in guidance


def test_role_guidance_reporting_recovery_uses_persisted_preflight() -> None:
    action = PlannerAction("recover_final_report", "reporting_agent", "REPORT", "recover")

    guidance = coordinator_module._role_guidance(action)

    gather = guidance.index("RunReportInputGatherer.gather_from_run(context, persist=True)")
    load = guidance.index("load the persisted preflight")
    assert guidance.startswith("Reporting preflight/recovery sequence:")
    assert gather < load
    assert "agent-authored values" in guidance


def test_codex_role_adapter_appends_reporting_contract_to_custom_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeCodexProcess:
        seen["argv"] = argv
        return _FakeCodexProcess(seen)

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", fake_popen)
    context = RunContext("RUN-COORD", tmp_path)
    action = PlannerAction("preflight_final_report", "reporting_agent", "REPORT", "preflight")
    custom = "Persisted reporting context only."
    execution = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(
            binary="fake-codex",
            role_prompts={"reporting_agent": custom},
        ),
    )(action, idempotency_key="key", context=context)

    assert execution.ok
    prompt = str(seen["input"])
    assert custom in prompt
    gather = prompt.index("RunReportInputGatherer.gather_from_run(context, persist=True)")
    load = prompt.index("load the persisted preflight")
    assert prompt.index(custom) < gather < load


def test_codex_role_adapter_binds_production_skill_and_rejects_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = _install_test_skill(tmp_path, monkeypatch)
    binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    context = RunContext("RUN-COORD", tmp_path)
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    calls: list[dict[str, object]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeCodexProcess:
        capture: dict[str, object] = {"argv": argv, "kwargs": kwargs}
        calls.append(capture)
        return _FakeCodexProcess(capture)

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", fake_popen)
    adapter = coordinator_module.CodexRoleAdapter(
        context,
        coordinator_module.CodexExecConfig(binary="fake-codex", **binding),
        require_skill_binding=True,
    )
    execution = adapter(action, idempotency_key="key", context=context)
    assert execution.ok
    assert calls
    prompt = str(calls[0]["input"])
    assert binding["skill_path"] in prompt
    assert binding["skill_version"] in prompt
    assert binding["core_version"] in prompt
    assert binding["skill_sha256"] in prompt

    calls.clear()
    skill_file = Path(binding["skill_path"]) / "SKILL.md"
    skill_file.write_text("---\nname: stale\n---\n", encoding="utf-8")
    rejected = adapter(action, idempotency_key="key-2", context=context)
    assert rejected.exit_code == 1
    assert "skill binding rejected" in rejected.error
    assert not calls


def test_analytical_owner_guidance_requires_identity_fanout_and_wait() -> None:
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    guidance = coordinator_module._role_guidance(action)

    for marker in (
        "readiness check",
        "search_ontology",
        "selected_sources()",
        "load_selected_source_ids()",
        "DataRoomCatalogEntry.source_id",
        "ControlledScriptRunner.validate_script/run_analysis",
        "same Analytical Owner action and begin_attempt",
        "repair_active=true",
        "reuse that authorization",
        "never call use_business_repair again",
        "search_identity_mappings",
        "propose_identity_domain",
        "mark_waiting_on_resolution",
        "one Entity Resolution Owner request per domain",
    ):
        assert marker in guidance


def test_analytical_owner_custom_prompt_cannot_replace_public_source_and_retry_contract(
    tmp_path: Path,
) -> None:
    action = PlannerAction("analyze_requirement", "analytical_owner", "REQ-001", "analyze")
    custom = "CUSTOM AO CONTEXT: use any convenient catalog field and hand off failures."
    prompt = coordinator_module.build_role_prompt(
        action,
        context=RunContext("RUN-COORD", tmp_path),
        idempotency_key="key",
        custom=custom,
    )
    assert prompt.index(custom) < prompt.index("Analytical Owner sequence:")
    assert prompt.index("selected_sources()") > prompt.index(custom)
    assert prompt.index("same Analytical Owner action and begin_attempt") > prompt.index(custom)


def test_active_skill_binding_rejects_missing_stale_symlink_frontmatter_hash_and_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _install_test_skill(tmp_path, monkeypatch)
    assert coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)["skill_path"] == str(active.resolve())

    shutil.rmtree(active)
    with pytest.raises(CoordinatorIntegrityError, match="active installed skill"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    _install_test_skill(tmp_path, monkeypatch)
    stale = tmp_path / "stale" / "skills" / "renamed-stale-skill"
    _write_synthetic_skill(stale, skill_version="0.3.0", core_version="0.3.0", release="legacy-release")
    shutil.rmtree(active)
    shutil.copytree(stale, active)
    with pytest.raises(CoordinatorIntegrityError, match="frontmatter"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    shutil.rmtree(active)
    _install_test_skill(tmp_path, monkeypatch)
    shutil.rmtree(active)
    active.symlink_to(stale, target_is_directory=True)
    with pytest.raises(CoordinatorIntegrityError, match="non-symlink"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    active.unlink()
    _install_test_skill(tmp_path, monkeypatch)
    (active / "SKILL.md").write_text("---\nname: stale\n---\n", encoding="utf-8")
    with pytest.raises(CoordinatorIntegrityError, match="frontmatter"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    shutil.rmtree(active)
    _install_test_skill(tmp_path, monkeypatch)
    (active / "README.md").write_text("drift\n", encoding="utf-8")
    with pytest.raises(CoordinatorIntegrityError, match="bytes"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    shutil.rmtree(active)
    _install_test_skill(tmp_path, monkeypatch)
    duplicate = tmp_path / ".agents" / "skills" / "renamed-production-skill"
    shutil.copytree(active, duplicate)
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)


def test_active_skill_binding_rejects_home_and_run_root_discovery_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _install_test_skill(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    home_duplicate = home / ".agents" / "skills" / "renamed-home-production-skill"
    shutil.copytree(active, home_duplicate)
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    shutil.rmtree(home_duplicate)

    run_root = tmp_path / "runs" / "RUN-001"
    run_duplicate = run_root / ".agents" / "skills" / "renamed-run-production-skill"
    shutil.copytree(active, run_duplicate)
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path, role_cwd=run_root)


def test_discovery_uses_top_level_quoted_name_and_ignores_unrelated_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _install_test_skill(tmp_path, monkeypatch)
    duplicate = tmp_path / ".agents" / "skills" / "arbitrary-directory-name"
    shutil.copytree(active, duplicate)
    skill_file = duplicate / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    text = text.replace(
        f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n",
        f'name: "{coordinator_module.PRODUCTION_SKILL_NAME}" # production alias\n',
        1,
    ).replace(
        "description: synthetic test fixture\n",
        "description: synthetic test fixture\nmetadata:\n  name: unrelated-nested-name\n",
        1,
    )
    skill_file.write_text(text, encoding="utf-8")
    with pytest.raises(CoordinatorIntegrityError, match="duplicates"):
        coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    shutil.rmtree(duplicate)

    unrelated = tmp_path / "unrelated-skill"
    _write_synthetic_skill(
        unrelated,
        skill_version="0.3.0",
        core_version="0.3.0",
        release="unrelated-release",
    )
    unrelated_file = unrelated / "SKILL.md"
    unrelated_file.write_text(
        unrelated_file.read_text(encoding="utf-8").replace(
            f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n",
            "name: other-skill\n",
            1,
        ),
        encoding="utf-8",
    )
    alias = tmp_path / "home" / ".agents" / "skills" / "unrelated-alias"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(unrelated, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)["skill_path"] == str(active.resolve())


def test_upgrade_and_rebind_binds_unbound_spec_once_and_loads_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    run_id = "RUN-BINDING-UPGRADE"
    context = RunContext(run_id, tmp_path)
    old = _spec(run_id)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(old)
    target = _bound_spec(run_id, coordinator_module.resolve_production_skill_binding(repo_root=tmp_path))

    upgraded = coordinator.upgrade_and_rebind(target)
    assert upgraded.phase == "queued"
    assert coordinator.persisted_spec() == target
    events_path = tmp_path / "control_plane" / "coordinator_events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "coordinator_binding_upgraded"
    sequence = upgraded.last_event_seq
    event_bytes = events_path.read_bytes()
    state_bytes = (tmp_path / "control_plane" / "coordinator_state.json").read_bytes()

    retried = coordinator.upgrade_and_rebind(target)
    assert retried.last_event_seq == sequence
    assert events_path.read_bytes() == event_bytes
    assert (tmp_path / "control_plane" / "coordinator_state.json").read_bytes() == state_bytes

    loaded = RunCoordinator.from_persisted_spec(context, planner_provider=QueuePlanner(()))
    assert loaded.persisted_spec() == target
    loaded.close()


def test_binding_upgrade_preserves_every_known_nondefault_spec_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    run_id = "RUN-BINDING-PRESERVE"
    context = RunContext(run_id, tmp_path)
    old = CoordinatorRunSpec(
        run_id,
        "G-0001",
        "planner://test",
        hashlib.sha256(b"planner").hexdigest(),
        role_dispatch_command=("fake-role", "--flag"),
        publication_policy={"enabled": True, "scope": "custom"},
        codex_exec={
            "binary": "fake-codex",
            "model": "gpt-test",
            "profile": "offline",
            "sandbox": "read-only",
            "timeout_seconds": 17,
            "ephemeral": False,
            "role_prompts": {"analytical_owner": "custom prompt"},
            "role_models": {"analytical_owner": "gpt-role"},
            "role_profiles": {"analytical_owner": "role-profile"},
            "role_sandboxes": {"analytical_owner": "read-only"},
            "role_timeouts": {"analytical_owner": 19},
        },
        lease_ttl_seconds=41,
    )
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(old)
    binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)
    target = CoordinatorRunSpec(
        old.run_id,
        old.generation_id,
        old.planner_ref,
        old.planner_hash,
        role_dispatch_command=old.role_dispatch_command,
        publication_policy=old.publication_policy,
        codex_exec={**old.codex_exec, **binding},
        lease_ttl_seconds=old.lease_ttl_seconds,
    )
    coordinator.upgrade_and_rebind(target)
    assert coordinator.persisted_spec() == target
    assert coordinator.persisted_spec().to_dict() == target.to_dict()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"unexpected": True}),
        lambda value: value["codex_exec"].update({"unexpected": True}),
    ),
)
def test_unknown_persisted_spec_fields_reject_without_writing(
    tmp_path: Path,
    mutate: Any,
) -> None:
    context = RunContext("RUN-UNKNOWN-SPEC", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(context.run_id))
    spec_path = tmp_path / "control_plane" / "coordinator_spec.json"
    value = json.loads(spec_path.read_text(encoding="utf-8"))
    mutate(value)
    spec_path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    before = spec_path.read_bytes()
    with pytest.raises(ValueError, match="unknown/deprecated"):
        coordinator.persisted_spec()
    assert spec_path.read_bytes() == before


def test_upgrade_and_rebind_rejects_active_lineage_and_hash_conflicts_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    binding = coordinator_module.resolve_production_skill_binding(repo_root=tmp_path)

    active_context = RunContext("RUN-UPGRADE-ACTIVE", tmp_path / "active")
    active_coordinator = RunCoordinator(active_context, planner_provider=QueuePlanner(()))
    active_coordinator.start(_spec(active_context.run_id))
    _seed_active_dispatch(active_coordinator, _action("analyze_requirement", "REQ-ACTIVE"), runner_pid=999_999)
    active_before = _control_plane_tree(active_context.run_root / "control_plane")
    with pytest.raises(CoordinatorConflictError, match="dispatches are active"):
        active_coordinator.upgrade_and_rebind(_bound_spec(active_context.run_id, binding))
    assert _control_plane_tree(active_context.run_root / "control_plane") == active_before

    lineage_context = RunContext("RUN-UPGRADE-LINEAGE", tmp_path / "lineage")
    lineage_coordinator = RunCoordinator(lineage_context, planner_provider=QueuePlanner(()))
    lineage_coordinator.start(_spec(lineage_context.run_id))
    lineage_before = _control_plane_tree(lineage_context.run_root / "control_plane")
    different_lineage = CoordinatorRunSpec(
        lineage_context.run_id,
        "G-0002",
        "planner://changed",
        hashlib.sha256(b"changed").hexdigest(),
        codex_exec={"binary": "fake-codex", **binding},
    )
    with pytest.raises(CoordinatorConflictError, match="unchanged planner lineage"):
        lineage_coordinator.upgrade_and_rebind(different_lineage)
    assert _control_plane_tree(lineage_context.run_root / "control_plane") == lineage_before

    hash_context = RunContext("RUN-UPGRADE-HASH", tmp_path / "hash")
    hash_coordinator = RunCoordinator(hash_context, planner_provider=QueuePlanner(()))
    hash_coordinator.start(_spec(hash_context.run_id))
    state_path = hash_context.run_root / "control_plane" / "coordinator_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["spec_hash"] = "f" * 64
    state_path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    hash_before = _control_plane_tree(hash_context.run_root / "control_plane")
    with pytest.raises((CoordinatorConflictError, CoordinatorIntegrityError)):
        hash_coordinator.upgrade_and_rebind(_bound_spec(hash_context.run_id, binding))
    assert _control_plane_tree(hash_context.run_root / "control_plane") == hash_before


@pytest.mark.parametrize(
    "failpoint",
    (
        "binding_upgrade_after_intent",
        "binding_upgrade_after_spec",
        "binding_upgrade_after_event",
        "binding_upgrade_after_state",
    ),
)
def test_binding_upgrade_failpoint_recovery_is_idempotent_and_split_brain_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    _install_test_skill(tmp_path, monkeypatch)
    run_id = f"RUN-BINDING-FAIL-{failpoint}"
    context = RunContext(run_id, tmp_path)
    first = RunCoordinator(context, planner_provider=QueuePlanner(()))
    first.start(_spec(run_id))
    target = _bound_spec(run_id, coordinator_module.resolve_production_skill_binding(repo_root=tmp_path))

    def inject(name: str) -> None:
        if name == failpoint:
            raise BaseException(name)

    first._failpoint = inject
    with pytest.raises(BaseException, match=failpoint):
        first.upgrade_and_rebind(target)

    restarted = RunCoordinator(context, planner_provider=QueuePlanner(()))
    restarted.upgrade_and_rebind(target)
    assert restarted.persisted_spec() == target
    events_path = tmp_path / "control_plane" / "coordinator_events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events][-1] == "coordinator_binding_upgraded"
    assert all(event["seq"] == index for index, event in enumerate(events, 1))
    before = events_path.read_bytes()
    restarted.upgrade_and_rebind(target)
    assert events_path.read_bytes() == before


def test_one_lock_start_and_hash_chain(tmp_path: Path) -> None:
    context = RunContext("RUN-COORD", tmp_path)
    planner = QueuePlanner((_action(),))
    coordinator = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": lambda action, **_: "diagnostic"})
    started = coordinator.start(_spec())
    assert started.status == "ready"
    assert (tmp_path / "control_plane" / ".coordinator.lock").is_file()
    assert not (tmp_path / ".coordinator_admission.lock").exists()
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert [event["seq"] for event in events] == [1]
    assert events[0]["event"] == "run_started"
    assert events[0]["event_hash"] == started.last_event_hash


def test_publish_rebinds_idle_generation_and_is_idempotent(tmp_path: Path) -> None:
    run_id = "RUN-PLAN-REBIND"
    first = _spec(run_id)
    second = _rebound_spec(run_id, "G-0002", "planner://g2")
    coordinator = RunCoordinator(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    coordinator.start(first)
    coordinator._terminal_projection = lambda: (True, "complete_with_limits", {"projection": "valid"})  # type: ignore[method-assign]
    terminal = coordinator.step()
    assert terminal.status == "complete_with_limits"

    rebound = coordinator.publish_and_rebind(second, lambda _spec: None)

    assert rebound.status == "ready"
    assert rebound.phase == "ready"
    assert rebound.active_dispatches == ()
    assert coordinator.persisted_spec() == second
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "plan_rebound"
    assert events[-1]["payload"]["from"]["generation_id"] == first.generation_id
    assert events[-1]["payload"]["to"]["generation_id"] == second.generation_id
    seq = rebound.last_event_seq
    assert coordinator.publish_and_rebind(second, lambda _spec: pytest.fail("duplicate publisher")).last_event_seq == seq


def test_start_rebind_rejects_active_or_non_lineage_changes(tmp_path: Path) -> None:
    run_id = "RUN-PLAN-REBIND-CONFLICT"
    first = _spec(run_id)
    second = _rebound_spec(run_id, "G-0002", "planner://g2")
    coordinator = RunCoordinator(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    coordinator.start(first)
    non_lineage = CoordinatorRunSpec(
        run_id,
        first.generation_id,
        first.planner_ref,
        first.planner_hash,
        role_dispatch_command=("different-role",),
    )
    before_non_lineage = _control_plane_tree(tmp_path / "control_plane")
    with pytest.raises(CoordinatorConflictError):
        coordinator.start(non_lineage)
    assert _control_plane_tree(tmp_path / "control_plane") == before_non_lineage
    active = _action("analyze_requirement", "REQ-ACTIVE")
    _seed_active_dispatch(coordinator, active, runner_pid=999_999)
    before_lineage = _control_plane_tree(tmp_path / "control_plane")
    with pytest.raises(CoordinatorConflictError):
        coordinator.start(second)
    assert _control_plane_tree(tmp_path / "control_plane") == before_lineage


def test_cross_process_rebind_has_one_winner(tmp_path: Path) -> None:
    run_id = "RUN-PLAN-REBIND-MP"
    context = RunContext(run_id, tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec(run_id))
    mp_context = mp.get_context("fork")
    barrier = mp_context.Barrier(2)
    results = [tmp_path / f"rebind-{index}.txt" for index in range(2)]
    processes = [
        mp_context.Process(
            target=_rebind_process_worker,
            args=(str(tmp_path), run_id, "G-0002", "planner://g2", barrier, str(results[index])),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    outcomes = [result.read_text() for result in results]
    assert all(value.startswith("ok:") for value in outcomes)
    persisted = json.loads((tmp_path / "control_plane/coordinator_spec.json").read_text())
    assert persisted["generation_id"] == "G-0002"
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events].count("plan_rebound") == 1


def test_action_change_is_authoritative_and_output_is_diagnostic(tmp_path: Path) -> None:
    first = _action()
    second = _action("review_requirement", "REQ-1")
    planner = QueuePlanner((first,), (second,))
    calls: list[str] = []

    def role(action: PlannerAction, **_: object) -> str:
        calls.append(action.action)
        return "not a result envelope"

    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": role})
    coordinator.start(_spec())
    status = coordinator.step()
    # The changed Planner action is reconciled and launched immediately; a
    # second active dispatch is therefore visible while this step returns.
    assert status.status == "dispatching"
    assert calls == ["analyze_requirement"]
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert "planner_advanced" in [event["event"] for event in events]
    assert not any(event["event"] == "role_completed" for event in events)


def test_same_action_is_waiting_and_reopen_resets_retry(tmp_path: Path) -> None:
    action = _action()
    planner = QueuePlanner((action,), (action,), (action,), (action,), (action,), (action,))
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={"analyze_requirement": lambda action, **_: RoleExecution(output="ordinary text")},
    )
    coordinator.start(_spec())
    status = coordinator.run(max_steps=5)
    assert status.status == "waiting"
    assert status.no_progress_count == 2
    assert any(item.get("kind") == "no_progress" for item in status.diagnostics)
    reopened = coordinator.reopen("retry after external repair")
    assert reopened.status == "ready"
    assert reopened.no_progress_count == 0
    assert reopened.last_event_seq > status.last_event_seq


def test_nonzero_role_transport_waits_and_is_retryable(tmp_path: Path) -> None:
    action = _action()
    planner = QueuePlanner((action,), (action,))
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={"analyze_requirement": lambda action, **_: RoleExecution(exit_code=7, error="temporary")},
    )
    coordinator.start(_spec())
    status = coordinator.step()
    assert status.status == "waiting"
    assert status.next_action["action"] == action.action
    assert status.diagnostics[-1]["kind"] == "role_transport_failure"
    assert "role_completed" not in [json.loads(line)["event"] for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]


def test_event_log_ahead_replays_after_checkpoint_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    action = _action()
    planner = QueuePlanner((action,), ())
    context = RunContext("RUN-COORD", tmp_path)
    fired = {"value": False}
    now = {"value": 100.0}

    def failpoint(name: str) -> None:
        if name == "after_event_before_checkpoint" and not fired["value"]:
            fired["value"] = True
            raise RuntimeError("simulated crash")

    first = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": lambda action, **_: "ok"}, clock=lambda: now["value"])
    first.start(_spec())
    first._failpoint = failpoint
    with pytest.raises(RuntimeError):
        first.step()
    now["value"] = 200.0
    # The failpoint models a process crash.  This test runs both coordinator
    # instances in one Python process, so mark the persisted owner dead
    # explicitly before exercising restart replay.
    monkeypatch.setattr(coordinator_module, "_pid_alive", lambda pid: False)
    resumed = RunCoordinator(context, planner_provider=planner, adapters={"analyze_requirement": lambda action, **_: "ok"}, clock=lambda: now["value"])
    status = resumed.resume()
    assert status.status == "waiting"
    assert status.last_event_seq >= 2


def test_terminal_status_requires_authoritative_projection(tmp_path: Path) -> None:
    planner = QueuePlanner(())
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=planner)
    coordinator.start(_spec())
    coordinator._terminal_projection = lambda: (True, "complete_with_limits", {"projection": "valid"})  # type: ignore[method-assign]
    status = coordinator.step()
    assert status.status == "complete_with_limits"
    assert status.publication_ready


def test_tampered_event_fails_closed(tmp_path: Path) -> None:
    context = RunContext("RUN-COORD", tmp_path)
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner(()))
    coordinator.start(_spec())
    events_path = tmp_path / "control_plane/coordinator_events.jsonl"
    event = json.loads(events_path.read_text().splitlines()[0])
    event["payload"] = {"tampered": True}
    events_path.write_text(json.dumps(event) + "\n")
    with pytest.raises(CoordinatorIntegrityError):
        coordinator.status()


def test_ready_set_launches_in_planner_order_and_overlaps(tmp_path: Path) -> None:
    first = _action("resolve_identity", "domain-1")
    second = _action("analyze_requirement", "REQ-2")
    planner = QueuePlanner((first, second), ())
    entered: list[str] = []
    entered_lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        with entered_lock:
            entered.append(action.action)
            if len(entered) == 2:
                both_entered.set()
        assert release.wait(2)
        return RoleExecution(output="ordinary transport")

    coordinator = RunCoordinator(context := RunContext("RUN-READY-SET", tmp_path), planner_provider=planner, adapters={"resolve_identity": role, "analyze_requirement": role})
    coordinator.start(_spec(context.run_id))
    thread = threading.Thread(target=coordinator.step)
    thread.start()
    assert both_entered.wait(2), "both ready actions must overlap"
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert entered[:2] == ["resolve_identity", "analyze_requirement"]


def test_requirement_mutation_admission_serializes_same_requirement_and_releases_queue(tmp_path: Path) -> None:
    repair = PlannerAction("repair_requirement", "analytical_owner", "REQ-1", "repair")
    review = PlannerAction("review_requirement", "business_reviewer", "REQ-1", "review")
    planner = QueuePlanner((repair, review), (review,), ())
    entered: list[str] = []
    first_started = threading.Event()
    review_started = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        entered.append(action.action)
        if action == repair:
            first_started.set()
            assert release.wait(2)
        else:
            review_started.set()
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-SAME-REQUIREMENT", tmp_path),
        planner_provider=planner,
        adapters={"repair_requirement": role, "review_requirement": role},
    )
    coordinator.start(_spec("RUN-SAME-REQUIREMENT"))
    thread = threading.Thread(target=coordinator.step)
    thread.start()
    assert first_started.wait(2)
    assert not review_started.is_set(), "review must remain queued behind same-item repair"
    release.set()
    assert review_started.wait(2)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert entered == ["repair_requirement", "review_requirement"]
    coordinator.close(wait_for_roles=True)


def test_analytical_owner_capacity_is_global_but_entity_resolution_remains_parallel(tmp_path: Path) -> None:
    first = PlannerAction("analyze_requirement", "analytical_owner", "REQ-1", "analyze")
    second = PlannerAction("analyze_requirement", "analytical_owner", "REQ-2", "analyze")
    planner = QueuePlanner((first, second), (second,), ())
    entered: list[str] = []
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        entered.append(action.subject_id)
        if action == first:
            first_started.set()
            assert release.wait(2)
        else:
            second_started.set()
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-GLOBAL-AO", tmp_path),
        planner_provider=planner,
        adapters={"analyze_requirement": role},
    )
    coordinator.start(_spec("RUN-GLOBAL-AO"))
    thread = threading.Thread(target=coordinator.step)
    thread.start()
    assert first_started.wait(2)
    assert not second_started.is_set(), "a second requirement must wait for the single AO slot"
    release.set()
    assert second_started.wait(2)
    thread.join(timeout=3)
    coordinator.close(wait_for_roles=True)


def test_reopen_resume_admission_does_not_oversubscribe_persisted_ao(tmp_path: Path) -> None:
    first = PlannerAction("analyze_requirement", "analytical_owner", "REQ-1", "analyze")
    second = PlannerAction("analyze_requirement", "analytical_owner", "REQ-2", "analyze")
    context = RunContext("RUN-REOPEN-AO-CAPACITY", tmp_path)
    seed = RunCoordinator(context, planner_provider=QueuePlanner(()))
    seed.start(_spec(context.run_id))
    _seed_active_dispatch(seed, first)
    seed.close(wait_for_roles=True)

    planner = QueuePlanner((first, second), (second,), ())
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        if action == first:
            first_started.set()
            assert release.wait(2)
        else:
            second_started.set()
        return RoleExecution()

    restarted = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={"analyze_requirement": role},
    )
    thread = threading.Thread(target=restarted.step)
    thread.start()
    assert first_started.wait(2)
    assert not second_started.is_set(), "resume must not submit a second persisted AO"
    release.set()
    assert second_started.wait(2)
    thread.join(timeout=3)
    restarted.close(wait_for_roles=True)


def test_first_completion_reloads_planner_and_launches_new_action(tmp_path: Path) -> None:
    first = _action("resolve_identity", "domain-1")
    long_action = _action("analyze_requirement", "REQ-2")
    newly_ready = _action("review_requirement", "REQ-3")
    planner = QueuePlanner((first, long_action), (newly_ready, long_action), ())
    long_started = threading.Event()
    release_long = threading.Event()
    newly_ready_started = threading.Event()

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        if action.action == "analyze_requirement":
            long_started.set()
            assert release_long.wait(3)
        elif action.action == "review_requirement":
            newly_ready_started.set()
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-FIRST-COMPLETE", tmp_path),
        planner_provider=planner,
        adapters={"resolve_identity": role, "analyze_requirement": role, "review_requirement": role},
    )
    coordinator.start(_spec("RUN-FIRST-COMPLETE"))
    status = coordinator.step()
    assert status.status == "dispatching"
    assert long_started.is_set()
    assert newly_ready_started.wait(2), "new action must launch before the long action finishes"
    release_long.set()
    coordinator.close(wait_for_roles=True)


def test_two_process_reconciliation_serializes_planner_advance(tmp_path: Path) -> None:
    old_action = _action("analyze_requirement", "REQ-RACE-OLD")
    new_action = _action("review_requirement", "REQ-RACE-NEW")
    run_id = "RUN-RECONCILE-RACE"
    coordinator = RunCoordinator(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    coordinator.start(_spec(run_id))

    mp_context = mp.get_context("fork")
    planner_read = mp_context.Event()
    release_planner_read = mp_context.Event()
    a_started = mp_context.Event()
    b_started = mp_context.Event()
    a_finished = mp_context.Event()
    b_finished = mp_context.Event()
    release_a_role = mp_context.Event()
    old_marker = tmp_path / "old-role.marker"
    new_marker = tmp_path / "new-role.marker"
    process_a = mp_context.Process(
        target=_reconciliation_race_worker,
        args=(
            str(tmp_path),
            run_id,
            "race-a",
            old_action,
            True,
            planner_read,
            release_planner_read,
            a_started,
            a_finished,
            release_a_role,
            str(old_marker),
        ),
    )
    process_b = mp_context.Process(
        target=_reconciliation_race_worker,
        args=(
            str(tmp_path),
            run_id,
            "race-b",
            new_action,
            False,
            planner_read,
            release_planner_read,
            b_started,
            b_finished,
            release_a_role,
            str(new_marker),
        ),
    )
    process_a.start()
    assert a_started.wait(5)
    assert planner_read.wait(5)
    process_b.start()
    assert b_started.wait(5)
    assert not b_finished.is_set(), "B must wait for A's lock-held Planner read"
    release_planner_read.set()
    assert b_finished.wait(5)
    release_a_role.set()
    assert a_finished.wait(5)
    for process in (process_a, process_b):
        process.join(timeout=5)
        assert process.exitcode == 0

    assert old_marker.read_text() == old_action.subject_id
    assert new_marker.read_text() == new_action.subject_id
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    dispatches = [
        event["payload"]["action"]["subject_id"]
        for event in events
        if event["event"] == "dispatch_started"
    ]
    assert dispatches == [old_action.subject_id, new_action.subject_id]
    assert not any(
        event["event"] == "planner_advanced"
        and event["payload"].get("reason") == "restart_active_not_offered"
        and any(
            removed["action"]["subject_id"] == old_action.subject_id
            for removed in event["payload"].get("removed_dispatches", ())
        )
        for event in events
    ), "a live runner claim must not be pruned by another process's Planner snapshot"
    state = json.loads((tmp_path / "control_plane/coordinator_state.json").read_text())
    assert state["active_dispatches"] == []


def test_duplicate_ready_slots_are_dispatched_once(tmp_path: Path) -> None:
    first = _action("resolve_identity", "domain-1")
    duplicate = _action("resolve_identity", "domain-1")
    second = _action("analyze_requirement", "REQ-2")
    planner = QueuePlanner((first, duplicate, second), (), ())
    calls: list[str] = []

    def role(action: PlannerAction, **_: object) -> RoleExecution:
        calls.append(action.action + ":" + action.subject_id)
        return RoleExecution()

    coordinator = RunCoordinator(
        RunContext("RUN-DEDUPE", tmp_path),
        planner_provider=planner,
        adapters={"resolve_identity": role, "analyze_requirement": role},
    )
    coordinator.start(_spec("RUN-DEDUPE"))
    coordinator.run(max_steps=2)
    assert sorted(calls) == ["analyze_requirement:REQ-2", "resolve_identity:domain-1"]


def test_crash_restart_resumes_multiple_dispatches_with_same_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _action("resolve_identity", "domain-1")
    second = _action("analyze_requirement", "REQ-2")
    planner = QueuePlanner((first, second), ())
    fail_count = {"value": 0}

    def failpoint(name: str) -> None:
        if name != "after_event_before_checkpoint":
            return
        fail_count["value"] += 1
        if fail_count["value"] == 2:
            raise RuntimeError("simulated crash")

    context = RunContext("RUN-RESTART-SET", tmp_path)
    first_coordinator = RunCoordinator(context, planner_provider=planner, adapters={"resolve_identity": lambda action, **_: RoleExecution(), "analyze_requirement": lambda action, **_: RoleExecution()})
    first_coordinator.start(_spec(context.run_id))
    first_coordinator._failpoint = failpoint
    with pytest.raises(RuntimeError):
        first_coordinator.run(max_steps=1)

    seen_keys: list[str] = []

    def idempotent_role(action: PlannerAction, *, idempotency_key: str, **_: object) -> RoleExecution:
        seen_keys.append(idempotency_key)
        return RoleExecution(output="committed once by public API")

    restarted = RunCoordinator(
        context,
        planner_provider=QueuePlanner((first, second), ()),
        adapters={"resolve_identity": idempotent_role, "analyze_requirement": idempotent_role},
    )
    monkeypatch.setattr(coordinator_module, "_pid_alive", lambda pid: False)
    restarted.run(max_steps=2)
    assert len(seen_keys) == 2
    assert len(set(seen_keys)) == 2


def _legacy_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _legacy_hash(value: object) -> str:
    return hashlib.sha256(_legacy_json_bytes(value)).hexdigest()


def _legacy_state_hash(value: dict[str, object]) -> str:
    snapshot = dict(value)
    snapshot["last_event_hash"] = ""
    return _legacy_hash(snapshot)


def _synthetic_legacy_g5_control_plane(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    destination = tmp_path / "control_plane"
    destination.mkdir()
    run_id = "RUN-G5-SYNTHETIC"
    generation_id = "G-0005"
    planner_hash = hashlib.sha256(b"synthetic-planner").hexdigest()
    run_spec = {
        "actions": [],
        "adapter_capabilities": {},
        "codex_exec": {
            "binary": "legacy-codex",
            "role_prompts": {"analytical_owner": "legacy strict RoleResult prompt"},
            "role_models": {"analytical_owner": "legacy-model"},
        },
        "coordinator_agent_command": [],
        "generation_id": generation_id,
        "offline_test_mode": True,
        "parent_lineage": {},
        "phase_validator_command": [],
        "planner_hash": planner_hash,
        "planner_ref": "planner://synthetic",
        "policy": {},
        "publication_policy": {"enabled": True, "legacy_channel": "legacy"},
        "role_dispatch_command": ["legacy-role-command"],
        "run_id": run_id,
    }
    lineage = {
        "active_generation_pointer_hash": hashlib.sha256(b"pointer").hexdigest(),
        "generation_id": generation_id,
        "manifest_hash": hashlib.sha256(b"manifest").hexdigest(),
        "plan_hash": planner_hash,
        "planner_hash": planner_hash,
        "planner_ref": "planner://synthetic",
    }
    spec_hash = _legacy_hash(run_spec)
    spec_document = {
        "schema_version": 1,
        "kind": "run_coordinator_spec",
        "run_spec": run_spec,
        "lineage_binding": lineage,
        "spec_hash": spec_hash,
    }
    state: dict[str, object] = {
        "schema_version": 1,
        "kind": "run_coordinator_state",
        "run_id": run_id,
        "generation_id": generation_id,
        "planner_ref": "planner://synthetic",
        "planner_hash": planner_hash,
        "spec_hash": spec_hash,
        "spec_ref": "control_plane/coordinator_spec.json",
        "actions": [],
        "active_action": None,
        "active_idempotency_key": None,
        "adapter_capabilities": {},
        "attempt": 0,
        "completed": {},
        "diagnostics": [],
        "last_completed_action": None,
        "last_event_seq": 0,
        "last_event_hash": "",
        "lease": None,
        "lineage_binding": lineage,
        "next_action_index": 0,
        "offline_test_mode": True,
        "owner": None,
        "parent_lineage": {},
        "phase": "queued",
        "planner_refresh_required": False,
        "planner_revision": None,
        "policy": {},
        "publication_policy": {},
        "publication_ready": False,
        "remaining_repair_grant": 0,
        "repair_count": 0,
        "replan_count": 0,
        "role_results": {},
        "status": "ready",
    }
    after_state = dict(state)
    after_state["last_event_seq"] = 1
    after_state["last_event_hash"] = ""
    event: dict[str, object] = {
        "schema_version": 1,
        "kind": "run_coordinator_event",
        "seq": 1,
        "event": "run_started",
        "run_id": run_id,
        "generation_id": generation_id,
        "planner_ref": "planner://synthetic",
        "planner_hash": planner_hash,
        "phase": "queued",
        "action": None,
        "subject_id": None,
        "idempotency_key": None,
        "owner": None,
        "lease": None,
        "attempt": 0,
        "repair_count": 0,
        "replan_count": 0,
        "parent_lineage": {},
        "publication_policy": {},
        "payload": {"action_count": 0, "spec_hash": spec_hash},
        "created_at": "2026-01-01T00:00:00+00:00",
        "previous_event_hash": "",
        "after_state": after_state,
        "state_hash": _legacy_state_hash(after_state),
    }
    event["event_hash"] = _legacy_hash(event)
    state["last_event_seq"] = 1
    state["last_event_hash"] = event["event_hash"]
    files = {
        "coordinator_spec.json": _legacy_json_bytes(spec_document),
        "coordinator_state.json": _legacy_json_bytes(state),
        "coordinator_events.jsonl": _legacy_json_bytes(event),
        ".coordinator.lock": b"",
    }
    for name, payload in files.items():
        (destination / name).write_bytes(payload)
    return destination, files


def test_exact_g5_import_archives_bytes_and_starts_new_chain(tmp_path: Path) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    wrapper = json.loads(original["coordinator_spec.json"])
    run_id = wrapper["run_spec"]["run_id"]
    action = _action("analyze_requirement", "REQ-23")
    planner = QueuePlanner((action,), ())
    seen: list[str] = []

    coordinator = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path),
        planner_provider=planner,
        adapters={"analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"]) or RoleExecution()},
    )
    pending = coordinator.status()
    assert pending.status == "waiting"
    assert pending.phase == "legacy_import_required"

    imported = coordinator.reopen("import exact G5 snapshot")
    assert imported.status == "ready"
    archive = control_plane / "legacy_import"
    for name in ("coordinator_spec.json", "coordinator_state.json", "coordinator_events.jsonl"):
        assert (archive / name).read_bytes() == original[name]
    current_spec = json.loads((control_plane / "coordinator_spec.json").read_text())
    assert set(current_spec) == {
        "schema_version",
        "kind",
        "run_id",
        "generation_id",
        "planner_ref",
        "planner_hash",
        "role_dispatch_command",
        "publication_policy",
        "codex_exec",
        "lease_ttl_seconds",
    }
    current_state = json.loads((control_plane / "coordinator_state.json").read_text())
    assert current_state["active_dispatches"] == []
    assert current_state["last_action"] is None
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["legacy_imported"]
    assert events[0]["payload"]["archive"]["coordinator_state.json"]["sha256"] == hashlib.sha256(original["coordinator_state.json"]).hexdigest()
    assert coordinator.status().last_event_hash == events[0]["event_hash"]

    # Reopen is idempotent after import and does not append another import.
    before_seq = coordinator.status().last_event_seq
    assert coordinator.reopen("repeat import").last_event_seq == before_seq
    resumed = coordinator.resume()
    assert resumed.status == "waiting"
    assert len(seen) == 1
    assert coordinator.status().last_event_seq >= 2


def test_g5_import_rebinds_legacy_transport_to_minimal_generation(tmp_path: Path) -> None:
    control_plane, _ = _synthetic_legacy_g5_control_plane(tmp_path)
    run_id = "RUN-G5-SYNTHETIC"
    new_spec = CoordinatorRunSpec(
        run_id,
        "G-0006",
        "planner://g6",
        hashlib.sha256(b"planner://g6").hexdigest(),
        publication_policy={"enabled": False, "channel": "new"},
        codex_exec={"binary": "new-codex", "role_models": {"analytical_owner": "new-model"}},
        lease_ttl_seconds=19,
    )
    coordinator = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path),
        planner_provider=QueuePlanner(()),
        adapters={"analyze_requirement": lambda current, **_: RoleExecution()},
    )
    assert coordinator.reopen("import G5 before generation rebind").status == "ready"

    published: list[str] = []
    rebound = coordinator.publish_and_rebind(new_spec, lambda spec: published.append(spec.generation_id))

    assert rebound.status == "ready"
    assert published == ["G-0006"]
    persisted = json.loads((control_plane / "coordinator_spec.json").read_text())
    assert persisted["generation_id"] == "G-0006"
    assert persisted["planner_ref"] == "planner://g6"
    assert persisted["publication_policy"] == {"enabled": False, "channel": "new"}
    assert persisted["codex_exec"] == {"binary": "new-codex", "role_models": {"analytical_owner": "new-model"}}
    assert persisted["lease_ttl_seconds"] == 19.0
    assert "role_prompts" not in json.dumps(persisted)
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    rebound_event = events[-1]
    assert rebound_event["event"] == "plan_rebound"
    assert rebound_event["payload"]["old_spec_hash"] != rebound_event["payload"]["new_spec_hash"]
    assert rebound_event["payload"]["transport"]["fields"] == [
        "role_dispatch_command",
        "codex_exec",
        "lease_ttl_seconds",
    ]
    assert rebound_event["payload"]["publication"]["fields"] == ["publication_policy"]
    assert "legacy strict RoleResult prompt" not in (control_plane / "coordinator_events.jsonl").read_text()
    assert coordinator.status().last_event_hash == rebound_event["event_hash"]

    before_retry = _control_plane_tree(control_plane)
    retry = coordinator.publish_and_rebind(new_spec, lambda _spec: pytest.fail("duplicate publisher"))
    assert retry.last_event_seq == rebound.last_event_seq
    assert _control_plane_tree(control_plane) == before_retry

    _seed_active_dispatch(coordinator, _action("analyze_requirement", "REQ-G5-ACTIVE"), runner_pid=999_999)
    before_active_conflict = _control_plane_tree(control_plane)
    with pytest.raises(CoordinatorConflictError):
        coordinator.start(_rebound_spec(run_id, "G-0007", "planner://g7"))
    assert _control_plane_tree(control_plane) == before_active_conflict


@pytest.mark.parametrize("mutation", ("near_wrapper", "partial_state"))
def test_malformed_or_partial_g5_import_fails_without_mutation(tmp_path: Path, mutation: str) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    if mutation == "near_wrapper":
        value = json.loads((control_plane / "coordinator_spec.json").read_text())
        value["unexpected"] = True
        (control_plane / "coordinator_spec.json").write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    else:
        (control_plane / "coordinator_events.jsonl").unlink()
    before = {path.name: path.read_bytes() for path in control_plane.iterdir() if path.is_file()}
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]
    with pytest.raises(CoordinatorIntegrityError):
        RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path))
    after = {path.name: path.read_bytes() for path in control_plane.iterdir() if path.is_file()}
    assert after == before
    assert not (control_plane / "legacy_import").exists()


def _control_plane_tree(control_plane: Path) -> dict[str, tuple[int, bytes]]:
    return {
        path.relative_to(control_plane).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in control_plane.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "mutation",
    ("payload", "previous_hash", "after_state", "state_hash", "tail", "checkpoint"),
)
def test_g5_hash_audit_rejects_tamper_without_mutation(tmp_path: Path, mutation: str) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    if mutation in {"payload", "previous_hash", "after_state", "state_hash"}:
        event_path = control_plane / "coordinator_events.jsonl"
        event = json.loads(event_path.read_text())
        if mutation == "payload":
            event["payload"]["tampered"] = True
        elif mutation == "previous_hash":
            event["previous_event_hash"] = "0" * 64
        elif mutation == "after_state":
            event["after_state"]["phase"] = "tampered"
        else:
            event["state_hash"] = "f" * 64
        event_path.write_bytes(_legacy_json_bytes(event))
    else:
        state_path = control_plane / "coordinator_state.json"
        state = json.loads(state_path.read_text())
        if mutation == "tail":
            state["last_event_hash"] = "f" * 64
        else:
            state["last_event_seq"] = 0
        state_path.write_bytes(_legacy_json_bytes(state))

    before = _control_plane_tree(control_plane)
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]
    with pytest.raises(CoordinatorIntegrityError):
        RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path))
    assert _control_plane_tree(control_plane) == before


def test_publish_and_rebind_is_quiescent_and_recoverable(tmp_path: Path) -> None:
    run_id = "RUN-PUBLISH-REBIND"
    context = RunContext(run_id, tmp_path)
    planner = QueuePlanner(())
    coordinator = RunCoordinator(context, planner_provider=planner)
    first = _spec(run_id)
    second = _rebound_spec(run_id, "G-0002", "planner://g2")
    coordinator.start(first)
    seen: list[str] = []

    def fail_started(name: str) -> None:
        if name == "plan_rebind_after_started":
            raise RuntimeError(name)

    coordinator._failpoint = fail_started
    with pytest.raises(RuntimeError):
        coordinator.publish_and_rebind(second, lambda spec: seen.append(spec.generation_id))
    coordinator.close()

    restarted = RunCoordinator.from_persisted_spec(context, planner_provider=planner)
    pending = restarted.status()
    assert pending.status == "waiting"
    assert pending.phase == "plan_rebind_pending"
    assert planner.calls == 0
    assert restarted.step().phase == "plan_rebind_pending"

    restarted._failpoint = None
    rebound = restarted.publish_and_rebind(second, lambda spec: seen.append(spec.generation_id))
    assert rebound.generation_id == "G-0002"
    assert seen == ["G-0002"]
    assert [event["event"] for event in (
        json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    )][-2:] == ["plan_rebind_started", "plan_rebound"]
    assert restarted.publish_and_rebind(second, lambda spec: seen.append("duplicate")).last_event_seq == rebound.last_event_seq
    assert seen == ["G-0002"]


@pytest.mark.parametrize(
    "failpoint",
    (
        "legacy_import_after_intent",
        "legacy_import_after_stage_coordinator_spec.json",
        "legacy_import_after_stage_coordinator_state.json",
        "legacy_import_after_stage_coordinator_events.jsonl",
        "legacy_import_after_archive_rename",
        "legacy_import_after_flat_spec",
        "legacy_import_after_state",
        "legacy_import_after_events",
    ),
)
def test_legacy_import_failpoint_recovery_converges(tmp_path: Path, failpoint: str) -> None:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]

    def inject(name: str) -> None:
        if name == failpoint:
            raise BaseException(name)

    crashed = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()), failpoint=inject
    )
    with pytest.raises(BaseException):
        crashed.reopen("crash import")

    recovered = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path), planner_provider=QueuePlanner(())
    )
    assert recovered.status().status == "ready"
    archive = control_plane / "legacy_import"
    for name in ("coordinator_spec.json", "coordinator_state.json", "coordinator_events.jsonl"):
        assert (archive / name).read_bytes() == original[name]
    assert not (control_plane / ".legacy_import.intent.json").exists()
    assert not (control_plane / ".legacy_import.staging").exists()
    events = [json.loads(line) for line in (control_plane / "coordinator_events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["legacy_imported"]


def _seed_archive_with_interrupted_import(tmp_path: Path) -> tuple[Path, dict[str, bytes], str]:
    control_plane, original = _synthetic_legacy_g5_control_plane(tmp_path)
    run_id = json.loads(original["coordinator_spec.json"])["run_spec"]["run_id"]

    def inject(name: str) -> None:
        if name == "legacy_import_after_archive_rename":
            raise BaseException(name)

    crashed = RunCoordinator.from_persisted_spec(
        RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()), failpoint=inject
    )
    with pytest.raises(BaseException):
        crashed.reopen("archive then crash")
    return control_plane, original, run_id


def test_archive_with_rogue_staging_fails_closed_without_mutation(tmp_path: Path) -> None:
    control_plane, _original, run_id = _seed_archive_with_interrupted_import(tmp_path)
    staging = control_plane / ".legacy_import.staging"
    staging.mkdir()
    (staging / "rogue").write_bytes(b"unrelated")
    before = _control_plane_tree(control_plane)
    with pytest.raises(CoordinatorIntegrityError):
        RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    assert _control_plane_tree(control_plane) == before


def test_archive_with_owned_partial_staging_recovers_and_cleans_only_staging(tmp_path: Path) -> None:
    control_plane, original, run_id = _seed_archive_with_interrupted_import(tmp_path)
    staging = control_plane / ".legacy_import.staging"
    staging.mkdir()
    (staging / "coordinator_spec.json").write_bytes(original["coordinator_spec.json"])
    recovered = RunCoordinator.from_persisted_spec(RunContext(run_id, tmp_path), planner_provider=QueuePlanner(()))
    assert recovered.status().status == "ready"
    assert not staging.exists()
    assert not (control_plane / ".legacy_import.intent.json").exists()
    for name in ("coordinator_spec.json", "coordinator_state.json", "coordinator_events.jsonl"):
        assert (control_plane / "legacy_import" / name).read_bytes() == original[name]


def test_live_pid_claim_allows_exactly_one_multiprocess_submit(tmp_path: Path) -> None:
    context = RunContext("RUN-MP-CLAIM", tmp_path)
    action = _action("analyze_requirement", "REQ-MP")
    planner = QueuePlanner((action,))
    coordinator = RunCoordinator(context, planner_provider=planner)
    coordinator.start(_spec(context.run_id))
    key = _seed_active_dispatch(coordinator, action)

    mp_context = mp.get_context("fork")
    barrier = mp_context.Barrier(2)
    release = mp_context.Event()
    marker = tmp_path / "role-submissions.log"
    processes = [
        mp_context.Process(
            target=_claim_process_worker,
            args=(str(tmp_path), context.run_id, f"worker-{index}", action, barrier, release, str(marker)),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    deadline = time.time() + 5
    while time.time() < deadline and not marker.exists():
        time.sleep(0.02)
    assert marker.exists()
    assert len(marker.read_text().splitlines()) == 1
    release.set()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    submissions = marker.read_text().splitlines()
    assert len(submissions) == 1
    assert submissions[0].split(":", 1)[1] == key


def test_dead_pid_adopts_active_slot_with_same_idempotency_key(tmp_path: Path) -> None:
    context = RunContext("RUN-DEAD-CLAIM", tmp_path)
    action = _action("analyze_requirement", "REQ-DEAD")
    seen: list[str] = []
    coordinator = RunCoordinator(
        context,
        planner_provider=QueuePlanner((action,)),
        adapters={"analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"]) or RoleExecution()},
    )
    coordinator.start(_spec(context.run_id))
    key = _seed_active_dispatch(coordinator, action, runner_pid=999_999)
    resumed = coordinator.step()
    assert resumed.status == "waiting"
    assert seen == [key]


def test_restart_reconciles_unoffered_active_slot_before_submit(tmp_path: Path) -> None:
    context = RunContext("RUN-RESTART-STALE", tmp_path)
    action = _action("analyze_requirement", "REQ-STALE")
    seed = RunCoordinator(context, planner_provider=QueuePlanner(()))
    seed.start(_spec(context.run_id))
    key = _seed_active_dispatch(seed, action, runner_pid=999_999)
    seen: list[str] = []
    planner = QueuePlanner(())
    restarted = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={
            "analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"])
            or RoleExecution()
        },
    )

    status = restarted.step()

    assert status.status == "waiting"
    assert status.active_dispatches == ()
    assert seen == []
    assert planner.calls == 1
    events = [
        json.loads(line)
        for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()
    ]
    assert any(
        event["event"] == "planner_advanced"
        and event["payload"]["reason"] == "restart_active_not_offered"
        and event["payload"]["removed_dispatches"][0]["idempotency_key"] == key
        for event in events
    )


def test_restart_adopts_still_offered_active_slot_once(tmp_path: Path) -> None:
    context = RunContext("RUN-RESTART-OFFERED", tmp_path)
    action = _action("analyze_requirement", "REQ-OFFERED")
    seed = RunCoordinator(context, planner_provider=QueuePlanner(()))
    seed.start(_spec(context.run_id))
    key = _seed_active_dispatch(seed, action, runner_pid=999_999)
    seen: list[str] = []
    planner = QueuePlanner((action,), ())
    restarted = RunCoordinator(
        context,
        planner_provider=planner,
        adapters={
            "analyze_requirement": lambda current, **kwargs: seen.append(kwargs["idempotency_key"])
            or RoleExecution()
        },
    )

    status = restarted.step()

    assert status.status == "waiting"
    assert seen == [key]
    assert planner.calls == 2


def test_reopen_clears_only_dead_claims(tmp_path: Path) -> None:
    context = RunContext("RUN-REOPEN-CLAIM", tmp_path)
    action = _action("analyze_requirement", "REQ-REOPEN")
    coordinator = RunCoordinator(context, planner_provider=QueuePlanner((action,)))
    coordinator.start(_spec(context.run_id))
    _seed_active_dispatch(coordinator, action, runner_pid=999_999)
    reopened = coordinator.reopen("clear dead claim")
    assert reopened.status == "dispatching"
    assert reopened.active_dispatches[0].get("runner_pid") is None
    assert reopened.active_dispatches[0].get("runner_id") is None
    events = [json.loads(line) for line in (tmp_path / "control_plane/coordinator_events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "dispatch_claims_cleared"
