from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import auto_foundry_core.coordinator as coordinator_module
from auto_foundry_core import (
    CoordinatorRunSpec,
    PlannerAction,
    ProductCandidate,
    ProductReviewStore,
    RequirementRecord,
    RequirementSupervisorWorkspace,
    RunCoordinator,
)
from auto_foundry_core.coordinator import RoleSessionRegistry
from auto_foundry_core.product_review import canonical_hash
from auto_foundry_core.requirement_planning import preview_input_fingerprint
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.workspace import RunContext

from apps.control_center_operational.launch import (
    LaunchConflictError,
    LaunchManager,
    LaunchSettings,
    _SupervisorStartCleanupError,
)
from apps.control_center_operational.projection import OperationalRepository
from apps.control_center_operational.run_control import CoordinatorProcess, CoordinatorProcessController, RunControlManager


class FakeProcessController:
    def __init__(self) -> None:
        self.current = CoordinatorProcess(
            pid=4242,
            pgid=4242,
            command=("python3", "-m", "auto_foundry_core.cli", "supervisor", "run"),
        )
        self.stopped: list[int] = []
        self.orphan_groups: set[tuple[int, str]] = set()
        self.terminated_groups: list[tuple[int, str]] = []
        self.terminate_error: Exception | None = None

    def find(self, run_id: str, run_root: Path):
        return self.current

    def group_alive(self, process_group_id: int, process_group_token: str | None = None) -> bool:
        return (process_group_id, process_group_token or "") in self.orphan_groups

    def terminate_token_group(self, process_group_id: int, process_group_token: str) -> bool:
        if self.terminate_error is not None:
            raise self.terminate_error
        self.terminated_groups.append((process_group_id, process_group_token))
        self.orphan_groups.discard((process_group_id, process_group_token))
        self.current = None
        return True

    def stop(self, process: CoordinatorProcess, *, timeout_seconds: float = 5.0) -> None:
        self.stopped.append(process.pid)
        self.current = None


class FakeRunner:
    def __init__(self, controller: FakeProcessController) -> None:
        self.controller = controller
        self.calls: list[dict[str, object]] = []

    def start(self, **kwargs):
        self.calls.append(dict(kwargs))
        self.controller.current = CoordinatorProcess(
            pid=5252,
            pgid=5252,
            command=("python3", "-m", "auto_foundry_core.cli", "supervisor", "run"),
        )
        return {
            "monitorRunId": "supervisor-test",
            "pid": 5252,
            "processGroupId": 5252,
            "processGroupToken": "run-control-token-5252",
            "argv": [],
        }


class FakeResumeLaunchManager(LaunchManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resume_prepare_calls: list[tuple[str, Path]] = []

    def prepare_resume_coordinator(self, run_id: str, run_root: Path):
        self.resume_prepare_calls.append((run_id, run_root))
        return {"operation": "run"}


class FailingRecordResumeLaunchManager(FakeResumeLaunchManager):
    def __init__(self, controller: FakeProcessController, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controller = controller

    def record_process_start(self, run_id: str, run_root: Path, started):
        if started:
            self.controller.current = None
            self.controller.orphan_groups.add((5252, "run-control-token-5252"))
            raise RuntimeError("test status persistence failure")
        return super().record_process_start(run_id, run_root, started)


class TransferredStartFailureRunner(FakeRunner):
    """Raise the launch-owned transfer after a child identity exists."""

    def start(self, **kwargs):
        self.calls.append(dict(kwargs))
        self.controller.current = None
        self.controller.orphan_groups.add((5252, "run-control-token-5252"))
        raise _SupervisorStartCleanupError(
            "resume start cleanup was not confirmed",
            started={
                "monitorRunId": "supervisor-transferred",
                "pid": 5252,
                "processGroupId": 5252,
                "processGroupToken": "run-control-token-5252",
                "startupToken": "startup-token-5252",
            },
        )


def _install_preview_skill(root: Path) -> tuple[Path, str]:
    """Create the isolated active skill tree used by the real-path harness."""

    code_home = root / "codex-home"
    skill_path = code_home / "skills" / coordinator_module.PRODUCTION_SKILL_NAME
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "SKILL.md").write_text(
        "---\n"
        f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n"
        "description: synthetic release fixture\n"
        "metadata:\n"
        f"  version: \"{coordinator_module.PRODUCTION_SKILL_VERSION}\"\n"
        "  core_name: auto_foundry_core\n"
        f"  core_version: \"{coordinator_module.PRODUCTION_CORE_VERSION}\"\n"
        f"  release: {coordinator_module.PRODUCTION_RELEASE}\n"
        "---\n\n"
        f"skill_name: {coordinator_module.PRODUCTION_SKILL_NAME}\n"
        f"skill_version: {coordinator_module.PRODUCTION_SKILL_VERSION}\n"
        "core_name: auto_foundry_core\n"
        f"core_version: {coordinator_module.PRODUCTION_CORE_VERSION}\n",
        encoding="utf-8",
    )
    (skill_path / "README.md").write_text("synthetic release fixture\n", encoding="utf-8")
    return code_home, hashlib.sha256(coordinator_module._skill_release_bytes(skill_path)).hexdigest()


def _seed_preview_product(context, plan_path: Path) -> ProductReviewStore:
    """Persist a complete accepted root Product bundle for public projection."""

    product_root = context.run_root / "products" / "generations" / "G-0001"
    product_root.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    for name, filename in {
        "manifest": "product_manifest.json",
        "fixture": "dashboard_fixture.json",
        "chart_map": "chart_map.json",
        "chart_registry": "chart_registry.json",
        "blueprint": "dashboard_blueprint_v2.json",
        "receipt": "build_receipt.json",
    }.items():
        path = product_root / filename
        path.write_text(json.dumps({"name": name}, sort_keys=True) + "\n", encoding="utf-8")
        outputs[name] = path
    site = product_root / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text("<html>reviewed</html>\n", encoding="utf-8")
    outputs["site"] = site
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
        product_owner="product-agent",
        parent_lineage={
            "root_generation": True,
            "parent_generation_id": None,
            "parent_manifest_ref": None,
            "parent_manifest_hash": None,
        },
        plan_binding={
            "plan_ref": str(plan_path.relative_to(context.run_root)),
            "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        publication_policy_hash=canonical_hash({"enabled": False}),
        artifact_bindings={
            name: {"ref": str(path.relative_to(context.run_root))}
            for name, path in outputs.items()
        },
    )
    store = ProductReviewStore(context, "G-0001")
    store.record_candidate(candidate)
    store.record_review(
        reviewer_ref="product-reviewer",
        verdict="accept",
        reviewed_at="2026-01-01T00:00:00Z",
    )
    return store


def _public_preview_phase() -> dict[str, object]:
    manifest_hash = "b" * 64
    content_hash = "c" * 64
    phase: dict[str, object] = {
        "product": {
            "preview_item_ids": ["REQ-001"],
            "preview_item_bindings": {
                "REQ-001": {
                    "accepted_manifest_ref": "requirements/REQ-001/accepted/manifest.json",
                    "accepted_manifest_hash": manifest_hash,
                    "accepted_content_ref": "requirements/REQ-001/accepted/answer_content.json",
                    "accepted_content_hash": content_hash,
                }
            },
            "preview_input_fingerprint": "",
        },
        "items": {
            "REQ-001": {
                "terminal_outcome": {"status": "accepted"},
                "accepted_business_validation": {
                    "valid": True,
                    "stage": "accepted",
                    "manifest_ref": "requirements/REQ-001/accepted/manifest.json",
                    "manifest_hash": manifest_hash,
                    "content_ref": "requirements/REQ-001/accepted/answer_content.json",
                    "content_hash": content_hash,
                },
            }
        },
    }
    product = phase["product"]
    items = phase["items"]
    assert isinstance(product, dict) and isinstance(items, dict)
    product["preview_input_fingerprint"] = preview_input_fingerprint(items)
    return phase


def _public_preview_fixture(root: Path):
    run_id = "RUN-PRODUCT-REGENERATION-PUBLIC-PREVIEW"
    runs = root / "runs"
    run_root = runs / run_id
    context = RunContext(run_id=run_id, run_root=run_root)
    lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
    plan = RequirementSupervisorWorkspace(context).plan_requirements(
        (RequirementRecord("REQ-001", "Preview accepted Product"),),
        planner_ref="planner://public-product-preview",
        persist=True,
    )
    plan_path = RunLifecycle.active_plan_path(context)
    _seed_preview_product(context, plan_path)
    code_home, skill_hash = _install_preview_skill(root)
    current_binding = {
        "skill_path": str((code_home / "skills" / coordinator_module.PRODUCTION_SKILL_NAME).resolve()),
        "skill_version": coordinator_module.PRODUCTION_SKILL_VERSION,
        "core_version": coordinator_module.PRODUCTION_CORE_VERSION,
        "skill_sha256": skill_hash,
    }
    old_binding = dict(current_binding)
    old_binding["skill_sha256"] = "f" * 64
    old_spec = CoordinatorRunSpec(
        run_id,
        "G-0001",
        plan.planner_ref,
        hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        publication_policy={"enabled": False},
        codex_exec={"binary": "codex", "sandbox": "workspace-write", "ephemeral": True, **old_binding},
    )
    persisted = RunCoordinator(
        context,
        planner=lambda _state: (),
        role_runner=lambda *_args, **_kwargs: SimpleNamespace(exit_code=0),
    )
    persisted.start(old_spec)
    settings = LaunchSettings(
        runtime_root=root,
        runs_root=runs,
        source_roots=(root,),
        state_root=root / "state",
        enable_launch=True,
        launch_token="run-control-token",
    )
    repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
    controller = FakeProcessController()
    controller.current = None
    runner = FakeRunner(controller)
    launch_manager = LaunchManager(settings, repository=repository, runner=runner)
    manager = RunControlManager(launch_manager, process_controller=controller)
    return {
        "context": context,
        "lifecycle": lifecycle,
        "phase": _public_preview_phase(),
        "code_home": code_home,
        "skill_hash": skill_hash,
        "old_spec": old_spec,
        "persisted": persisted,
        "settings": settings,
        "repository": repository,
        "controller": controller,
        "runner": runner,
        "launch_manager": launch_manager,
        "manager": manager,
        "run_id": run_id,
        "browser_run_id": repository.list_runs()[0]["id"],
    }


class RunControlTests(unittest.TestCase):
    @patch("apps.control_center_operational.launch.subprocess.run")
    def test_process_probe_uses_host_ps_dialect(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="")
        for platform, expected in (("linux", ["ps", "eww", "-eo", "pgid=,stat=,args="]),
                                   ("darwin", ["ps", "-Eww", "-axo", "pgid=,stat=,command="])):
            with patch("apps.control_center_operational.launch.sys.platform", platform):
                self.assertFalse(CoordinatorProcessController.group_alive(7777, "token-match"))
                self.assertEqual(run.call_args.args[0], expected)

    @patch("apps.control_center_operational.launch.subprocess.run")
    def test_group_alive_requires_matching_private_token(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=(
                "7777 S /usr/bin/python supervisor "
                "AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN=token-match\n"
                "7777 S /usr/bin/python unrelated\n"
            )
        )
        self.assertTrue(CoordinatorProcessController.group_alive(7777, "token-match"))
        self.assertFalse(CoordinatorProcessController.group_alive(7777, "token-other"))
        self.assertFalse(CoordinatorProcessController.group_alive(7777))

    @patch("apps.control_center_operational.launch.subprocess.run")
    def test_group_alive_rejects_adversarial_token_assignments(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=(
                "7777 S supervisor "
                "AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN_EXTRA=token-match\n"
                "7777 S supervisor "
                "XAUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN=token-match\n"
                "7777 S supervisor "
                "AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN=prefix-token-match\n"
                "7777 S supervisor "
                "AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN=token-match-suffix\n"
            )
        )
        self.assertFalse(CoordinatorProcessController.group_alive(7777, "token-match"))

    @patch("apps.control_center_operational.launch.os.killpg")
    @patch("apps.control_center_operational.launch.subprocess.run")
    def test_nonzero_ps_fails_closed_without_signaling(self, run, killpg):
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="private ps failure")
        with self.assertRaisesRegex(Exception, "Could not verify the Foundry Supervisor process group") as error:
            CoordinatorProcessController.group_alive(7777, "token-match")
        self.assertNotIn("private ps failure", str(error.exception))
        with self.assertRaisesRegex(Exception, "Could not verify the Foundry Supervisor process group"):
            CoordinatorProcessController.terminate_token_group(7777, "token-match")
        killpg.assert_not_called()

    @patch("apps.control_center_operational.launch.os.killpg")
    @patch("apps.control_center_operational.launch._process_group_has_token")
    def test_terminate_token_group_never_signals_unverified_pgid(self, has_token, killpg):
        has_token.return_value = False
        self.assertFalse(CoordinatorProcessController.terminate_token_group(7777, "token-match"))
        killpg.assert_not_called()

        has_token.side_effect = [True, False]
        self.assertTrue(CoordinatorProcessController.terminate_token_group(7777, "token-match"))
        killpg.assert_called_once()

    def test_pause_preserves_artifacts_and_resume_restarts_from_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-CONTROL"
            context = RunContext(run_id="RUN-CONTROL", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            artifact = run_root / "requirements" / "REQ-001" / "work" / "checkpoint.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b'{"committed":true}\n')
            before = hashlib.sha256(artifact.read_bytes()).hexdigest()

            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            runner = FakeRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            paused = manager.pause(browser_run_id, confirmed=True)
            self.assertEqual(paused["lifecycleStatus"], "paused")
            self.assertTrue(paused["canResume"])
            self.assertEqual(controller.stopped, [4242])
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), before)

            resumed = manager.resume(browser_run_id, confirmed=True)
            self.assertEqual(resumed["lifecycleStatus"], "initialized")
            self.assertTrue(resumed["coordinatorActive"])
            self.assertEqual(resumed["monitorRunId"], "supervisor-test")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(
                launch_manager.resume_prepare_calls,
                [("RUN-CONTROL", run_root.resolve())],
            )
            self.assertNotIn("coordinator_operation", runner.calls[0])
            status_files = sorted((settings.state_root / "statuses").glob("*.json"))
            self.assertEqual(len(status_files), 1)
            process_status = json.loads(status_files[0].read_text(encoding="utf-8"))
            self.assertEqual(process_status["runId"], "RUN-CONTROL")
            self.assertEqual(process_status["processGroupId"], 5252)
            self.assertEqual(process_status["processGroupToken"], "run-control-token-5252")
            self.assertNotIn("processGroupToken", resumed)
            self.assertEqual(RunLifecycle.load(context).status, "initialized")
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), before)

            # Simulate the Supervisor leader exiting while a child remains in
            # the persisted run-owned process group.
            controller.current = None
            controller.orphan_groups.add((5252, "run-control-token-5252"))
            RunLifecycle.load(context).pause("test resumed leader crash")
            orphaned = manager.status(browser_run_id)
            self.assertFalse(orphaned["coordinatorActive"])
            self.assertTrue(orphaned["coordinatorOrphaned"])
            self.assertFalse(orphaned["canResume"])

    def test_same_paused_resume_instances_spawn_exactly_once(self) -> None:
        """The run admission flock serializes concurrent resume requests."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-CONCURRENT-RESUME"
            context = RunContext(run_id="RUN-CONCURRENT-RESUME", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            lifecycle.pause("test concurrent resume")

            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            runner = FakeRunner(controller)
            launch_managers = [
                FakeResumeLaunchManager(settings, repository=repository, runner=runner),
                FakeResumeLaunchManager(settings, repository=repository, runner=runner),
            ]
            managers = [RunControlManager(item, process_controller=controller) for item in launch_managers]
            browser_run_id = repository.list_runs()[0]["id"]
            barrier = threading.Barrier(len(managers))
            results: list[dict[str, object]] = []
            errors: list[BaseException] = []

            def invoke(manager: RunControlManager) -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(manager.resume(browser_run_id, confirmed=True))
                except BaseException as exc:  # pragma: no cover - assertion below reports any race failure
                    errors.append(exc)

            threads = [threading.Thread(target=invoke, args=(manager,)) for manager in managers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "concurrent resume admission deadlocked")
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertRegex(str(errors[0]), "Only a paused run can be resumed")
            self.assertEqual(len(runner.calls), 1)
            self.assertNotIn("processGroupToken", results[0])
            self.assertNotIn("startupToken", results[0])
            status_files = sorted((settings.state_root / "statuses").glob("*.json"))
            self.assertEqual(len(status_files), 1)
            private_status = json.loads(status_files[0].read_text(encoding="utf-8"))
            self.assertEqual(private_status["processGroupId"], 5252)
            self.assertEqual(private_status["processGroupToken"], "run-control-token-5252")
            self.assertEqual(RunLifecycle.load(context).status, "initialized")

    def test_process_matching_accepts_only_the_supervisor_run_wrapper(self) -> None:
        run_root = Path("/tmp/control-center-supervisor-run").resolve()
        supervisor = (
            "/usr/bin/python3",
            "-m",
            "auto_foundry_core.cli",
            "supervisor",
            "run",
            "--run-root",
            str(run_root),
            "--run-id",
            "RUN-CONTROL",
        )
        self.assertTrue(CoordinatorProcessController._matches(supervisor, "RUN-CONTROL", run_root))
        self.assertFalse(
            CoordinatorProcessController._matches(
                supervisor[:3] + ("coordinator", "run") + supervisor[5:],
                "RUN-CONTROL",
                run_root,
            )
        )
        self.assertFalse(
            CoordinatorProcessController._matches(
                supervisor[:4] + ("resume",) + supervisor[5:],
                "RUN-CONTROL",
                run_root,
            )
        )

    def test_resume_blocks_when_supervisor_leader_died_but_run_group_member_remains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-ORPHAN"
            context = RunContext(run_id="RUN-ORPHAN", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            lifecycle.pause("test leader crash")

            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            controller.orphan_groups.add((7777, "orphan-token-7777"))
            runner = FakeRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            statuses = settings.state_root / "statuses"
            statuses.mkdir(parents=True)
            (statuses / "D-old.json").write_text(
                json.dumps(
                    {
                        "draftId": "D-old",
                        "runId": "RUN-ORPHAN",
                        "runRoot": str(run_root),
                        "status": "accepted",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "pid": 8888,
                        "processGroupId": 8888,
                        "processGroupToken": "old-token-8888",
                    }
                ),
                encoding="utf-8",
            )
            (statuses / "D-orphan.json").write_text(
                json.dumps(
                    {
                        "draftId": "D-orphan",
                        "runId": "RUN-ORPHAN",
                        "runRoot": str(run_root),
                        "status": "accepted",
                        "startedAt": "2026-01-02T00:00:00Z",
                        "pid": 7777,
                        "processGroupId": 7777,
                        "processGroupToken": "orphan-token-7777",
                    }
                ),
                encoding="utf-8",
            )
            # A newer queued draft without a process identity must not hide
            # the older still-live token-owned group during reload.
            (statuses / "D-newer-queued.json").write_text(
                json.dumps(
                    {
                        "draftId": "D-newer-queued",
                        "runId": "RUN-ORPHAN",
                        "runRoot": str(run_root),
                        "status": "queued",
                        "startedAt": "2026-01-03T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            observed = manager.status(browser_run_id)
            self.assertFalse(observed["coordinatorActive"])
            self.assertTrue(observed["coordinatorOrphaned"])
            self.assertFalse(observed["canPause"])
            self.assertFalse(observed["canResume"])
            self.assertIsNone(observed["action"])
            with self.assertRaisesRegex(Exception, "process-group members are still active"):
                manager.resume(browser_run_id, confirmed=True)
            self.assertEqual(runner.calls, [])
            self.assertEqual(RunLifecycle.load(context).status, "paused")

            # A distinct older identity remains authoritative until it is
            # individually proven gone; a newer gone group cannot hide it.
            controller.orphan_groups = {(8888, "old-token-8888")}
            still_owned = manager.status(browser_run_id)
            self.assertTrue(still_owned["coordinatorOrphaned"])
            self.assertFalse(still_owned["canResume"])
            controller.orphan_groups = set()
            quiescent = manager.status(browser_run_id)
            self.assertFalse(quiescent["coordinatorOrphaned"])
            self.assertTrue(quiescent["canResume"])

    def test_recycled_process_group_id_without_matching_token_does_not_block_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-RECYCLED-PGID"
            context = RunContext(run_id="RUN-RECYCLED-PGID", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            lifecycle.pause("test recycled process group")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            # The OS has reused this numeric PGID, but the live member carries
            # a different launch token.  It must be treated as stale metadata.
            controller.orphan_groups.add((7777, "different-launch-token"))
            runner = FakeRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            statuses = settings.state_root / "statuses"
            statuses.mkdir(parents=True)
            (statuses / "D-recycled.json").write_text(
                json.dumps(
                    {
                        "draftId": "D-recycled",
                        "runId": "RUN-RECYCLED-PGID",
                        "runRoot": str(run_root),
                        "status": "accepted",
                        "startedAt": "2026-01-03T00:00:00Z",
                        "pid": 7777,
                        "processGroupId": 7777,
                        "processGroupToken": "expected-launch-token",
                    }
                ),
                encoding="utf-8",
            )
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            observed = manager.status(browser_run_id)
            self.assertFalse(observed["coordinatorOrphaned"])
            self.assertTrue(observed["canResume"])
            resumed = manager.resume(browser_run_id, confirmed=True)
            self.assertTrue(resumed["coordinatorActive"])
            self.assertEqual(len(runner.calls), 1)

            # A legacy numeric-only record is likewise non-authoritative.
            lifecycle.pause("test numeric-only stale metadata")
            latest = statuses / "D-recycled.json"
            value = json.loads(latest.read_text(encoding="utf-8"))
            value.pop("processGroupToken")
            latest.write_text(json.dumps(value), encoding="utf-8")
            controller.current = None
            observed = manager.status(browser_run_id)
            self.assertFalse(observed["coordinatorOrphaned"])
            self.assertTrue(observed["canResume"])

    def test_resume_status_failure_cleans_leaderless_token_owned_descendant_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-RESUME-CLEANUP"
            context = RunContext(run_id="RUN-RESUME-CLEANUP", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            lifecycle.pause("test resume tracking failure")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            launch_manager = FailingRecordResumeLaunchManager(
                controller,
                settings,
                repository=repository,
                runner=FakeRunner(controller),
            )
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            with self.assertRaisesRegex(Exception, "process identity"):
                manager.resume(browser_run_id, confirmed=True)
            self.assertEqual(
                controller.terminated_groups,
                [(5252, "run-control-token-5252")],
            )
            self.assertFalse(controller.orphan_groups)
            self.assertEqual(RunLifecycle.load(context).status, "paused")
            controller.terminate_error = RuntimeError("test cleanup failure")
            with self.assertRaisesRegex(Exception, "cleanup failed"):
                manager.resume(browser_run_id, confirmed=True)
            self.assertEqual(RunLifecycle.load(context).status, "paused")

    def test_resume_tracking_cleanup_false_persists_identity_and_blocks_duplicate(self) -> None:
        """A false cleanup result cannot expose a tokenless paused launch."""

        class DistinctTokenRunner(FakeRunner):
            def start(self, **kwargs):
                started = super().start(**kwargs)
                started["startupToken"] = "startup-token-5252"
                return started

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-RESUME-TRACKING-FALSE"
            context = RunContext(run_id="RUN-RESUME-TRACKING-FALSE", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            lifecycle.pause("test tracking cleanup false")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            launch_manager = FailingRecordResumeLaunchManager(
                controller,
                settings,
                repository=repository,
                runner=DistinctTokenRunner(controller),
            )
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            with patch.object(controller, "terminate_token_group", return_value=False) as terminate:
                with self.assertRaisesRegex(Exception, "cleanup failed after resume error"):
                    manager.resume(browser_run_id, confirmed=True)
            terminate.assert_called_once_with(5252, "run-control-token-5252")
            self.assertEqual(RunLifecycle.load(context).status, "paused")
            status_files = sorted((settings.state_root / "statuses").glob("*.json"))
            self.assertEqual(len(status_files), 1)
            private_status = json.loads(status_files[0].read_text(encoding="utf-8"))
            self.assertEqual(private_status["status"], "starting")
            self.assertEqual(private_status["processGroupId"], 5252)
            self.assertEqual(private_status["processGroupToken"], "run-control-token-5252")
            self.assertEqual(private_status["startupToken"], "startup-token-5252")

            # Reload/control must detect the still-owned group and refuse a
            # second runner start for this paused run.
            reloaded = RunControlManager(launch_manager, process_controller=controller)
            observed = reloaded.status(browser_run_id)
            self.assertTrue(observed["coordinatorOrphaned"])
            self.assertFalse(observed["canResume"])
            self.assertNotIn("processGroupToken", observed)
            self.assertNotIn("startupToken", observed)
            with self.assertRaisesRegex(Exception, "process-group members are still active"):
                reloaded.resume(browser_run_id, confirmed=True)
            self.assertEqual(len(launch_manager.runner.calls), 1)

    def test_resume_transferred_start_cleanup_failure_persists_identity_and_blocks_duplicate(self) -> None:
        """An unconfirmed transferred cleanup survives reload and blocks respawn."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-RESUME-TRANSFER"
            context = RunContext(run_id="RUN-RESUME-TRANSFER", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            lifecycle.pause("test transferred start cleanup")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            controller.terminate_error = RuntimeError("test transferred cleanup failure")
            runner = TransferredStartFailureRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            with self.assertRaisesRegex(Exception, "cleanup or recovery remains pending"):
                manager.resume(browser_run_id, confirmed=True)
            self.assertEqual(RunLifecycle.load(context).status, "paused")
            status_files = sorted((settings.state_root / "statuses").glob("*.json"))
            self.assertEqual(len(status_files), 1)
            private_status = json.loads(status_files[0].read_text(encoding="utf-8"))
            self.assertEqual(private_status["status"], "starting")
            self.assertEqual(private_status["processGroupId"], 5252)
            self.assertEqual(private_status["processGroupToken"], "run-control-token-5252")
            self.assertEqual(private_status["startupToken"], "startup-token-5252")

            # A fresh manager must see the live token-owned group as orphaned;
            # it may not spawn a second Supervisor for the paused run.
            reloaded = RunControlManager(launch_manager, process_controller=controller)
            observed = reloaded.status(browser_run_id)
            self.assertTrue(observed["coordinatorOrphaned"])
            self.assertFalse(observed["canResume"])
            self.assertNotIn("processGroupToken", observed)
            self.assertNotIn("startupToken", observed)
            with self.assertRaisesRegex(Exception, "process-group members are still active"):
                reloaded.resume(browser_run_id, confirmed=True)
            self.assertEqual(len(runner.calls), 1)

    def test_resume_transferred_start_confirmed_cleanup_persists_failed_paused_state(self) -> None:
        """Confirmed transferred cleanup leaves no live ownership on reload."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-RESUME-TRANSFER-CLEAN"
            context = RunContext(run_id="RUN-RESUME-TRANSFER-CLEAN", run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            lifecycle.pause("test transferred cleanup confirmed")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            runner = TransferredStartFailureRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            with self.assertRaisesRegex(Exception, "cleanup was confirmed and the run is paused/recoverable"):
                manager.resume(browser_run_id, confirmed=True)
            self.assertEqual(controller.terminated_groups, [(5252, "run-control-token-5252")])
            self.assertFalse(controller.orphan_groups)
            self.assertEqual(RunLifecycle.load(context).status, "paused")
            status_files = sorted((settings.state_root / "statuses").glob("*.json"))
            self.assertEqual(len(status_files), 1)
            private_status = json.loads(status_files[0].read_text(encoding="utf-8"))
            self.assertEqual(private_status["status"], "failed")
            self.assertEqual(private_status["processGroupToken"], "run-control-token-5252")
            observed = manager.status(browser_run_id)
            self.assertFalse(observed["coordinatorOrphaned"])
            self.assertTrue(observed["canResume"])
            self.assertNotIn("processGroupToken", observed)
            self.assertNotIn("startupToken", observed)

    def test_pause_requires_confirmation_and_commands_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            context = RunContext(run_id="RUN-LOCKED", run_root=runs / "RUN-LOCKED")
            RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            settings = LaunchSettings(runtime_root=root, runs_root=runs, state_root=root / "state")
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            manager = RunControlManager(
                LaunchManager(settings, repository=repository, runner=FakeRunner(FakeProcessController())),
                process_controller=FakeProcessController(),
            )
            browser_run_id = repository.list_runs()[0]["id"]
            with self.assertRaisesRegex(Exception, "disabled"):
                manager.pause(browser_run_id, confirmed=True)

    def test_product_regeneration_requests_and_starts_supervisor_without_resume_click(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-PRODUCT-REGENERATION-CONTROL"
            context = RunContext(run_id=run_root.name, run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            runner = FakeRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]
            calls: list[tuple[str, str | None]] = []

            class FakeCoordinator:
                @classmethod
                def from_persisted_spec(cls, _context):
                    return cls()

                def regenerate_product(self, *, reason: str, idempotency_key: str | None = None):
                    calls.append((reason, idempotency_key))
                    return SimpleNamespace(status="ready", phase="product_regeneration_requested")

                def close(self, *, wait_for_roles: bool = False):
                    assert wait_for_roles is False

            fake_api = {
                "RunContext": RunContext,
                "RunCoordinator": FakeCoordinator,
                "RunLifecycle": RunLifecycle,
            }
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                result = manager.regenerate_product(
                    browser_run_id,
                    confirmed=True,
                    reason="refresh reviewed dashboard",
                    idempotency_key="regen-control-1",
                )

            assert result["operation"] == "regenerate_product"
            assert result["requested"] is True
            assert result["coordinatorStatus"] == "ready"
            assert calls == [("refresh reviewed dashboard", "regen-control-1")]
            assert len(runner.calls) == 1
            assert launch_manager.resume_prepare_calls == [("RUN-PRODUCT-REGENERATION-CONTROL", run_root.resolve())]
            assert result["startupStatus"] == "accepted"
            assert lifecycle.status == "initialized"

    def test_product_regeneration_start_failure_keeps_request_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-PRODUCT-REGENERATION-START-FAILURE"
            context = RunContext(run_id=run_root.name, run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None

            class FailingRunner(FakeRunner):
                def start(self, **kwargs):
                    self.calls.append(dict(kwargs))
                    raise RuntimeError("Supervisor unavailable")

            runner = FailingRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            class FakeCoordinator:
                @classmethod
                def from_persisted_spec(cls, _context):
                    return cls()

                def regenerate_product(self, *, reason: str, idempotency_key: str | None = None):
                    return SimpleNamespace(status="ready", phase="product_regeneration_requested")

                def close(self, *, wait_for_roles: bool = False):
                    assert wait_for_roles is False

            fake_api = {
                "RunContext": RunContext,
                "RunCoordinator": FakeCoordinator,
                "RunLifecycle": RunLifecycle,
            }
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                result = manager.regenerate_product(
                    browser_run_id,
                    confirmed=True,
                    idempotency_key="regen-start-failure",
                )

            assert result["requested"] is True
            assert result["productRegenerationPending"] is True
            assert result["startupStatus"] == "pending"
            assert "could not be started" in result["startupError"]
            assert lifecycle.status == "initialized"
            assert len(runner.calls) == 1

    def test_product_regeneration_idempotency_key_is_stable_from_coordinator_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-PRODUCT-REGENERATION-KEY"
            context = RunContext(run_id=run_root.name, run_root=run_root)
            RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            settings = LaunchSettings(runtime_root=root, runs_root=runs, state_root=root / "state")
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            launch_manager = LaunchManager(settings, repository=repository, runner=FakeRunner(controller))
            manager = RunControlManager(launch_manager, process_controller=controller)

            class FakeCoordinator:
                @classmethod
                def from_persisted_spec(cls, _context):
                    return cls()

                def status(self):
                    return SimpleNamespace(
                        phase="ready",
                        generation_id="G-001",
                        last_event_hash="a" * 64,
                    )

                def close(self, *, wait_for_roles: bool = False):
                    assert wait_for_roles is False

            fake_api = {
                "RunContext": RunContext,
                "RunCoordinator": FakeCoordinator,
                "RunLifecycle": RunLifecycle,
            }
            browser_run_id = repository.list_runs()[0]["id"]
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                first = manager.status(browser_run_id)
                second = manager.status(browser_run_id)
            assert first["productRegenerationIdempotencyKey"]
            assert first["productRegenerationIdempotencyKey"] == second["productRegenerationIdempotencyKey"]

    def test_product_regeneration_status_previews_rotated_binding_without_mutation(self) -> None:
        """A stale persisted skill binding remains invalid but exposes the POST key read-only."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-PRODUCT-REGENERATION-PREVIEW"
            context = RunContext(run_id=run_root.name, run_root=run_root)
            RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            # The lightweight lifecycle fixture does not need a coordinator
            # yet, so create its empty control-plane namespace explicitly for
            # the read-only hash-stability assertion below.
            (run_root / "control_plane").mkdir(parents=True, exist_ok=True)
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            runner = FakeRunner(controller)

            class PreviewLaunchManager(FakeResumeLaunchManager):
                rebound = False
                preview_calls: list[tuple[str, Path]] = []

                def preview_resume_coordinator(self, run_id: str, run_root: Path):
                    self.preview_calls.append((run_id, run_root))
                    return {
                        "operation": "preview",
                        "persistedBindingStale": True,
                        "spec": {"run_id": run_id, "spec_hash": "new"},
                    }

                def prepare_resume_coordinator(self, run_id: str, run_root: Path):
                    self.rebound = True
                    return super().prepare_resume_coordinator(run_id, run_root)

            launch_manager = PreviewLaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)
            key = "product-regeneration-preview-key"
            calls: list[str | None] = []

            class FakeCoordinator:
                projection: dict[str, object] = {
                    "status": "accepted",
                    "pending": False,
                    "eligible": True,
                    "request_id": "regen-old",
                    "idempotency_key": key,
                }
                rebound = False

                def __init__(self, _context):
                    pass

                @classmethod
                def from_persisted_spec(cls, _context):
                    if not cls.rebound:
                        raise coordinator_module.CoordinatorProductionBindingMismatch(
                            "active installed skill bytes do not match persisted release"
                        )
                    return cls(_context)

                def product_regeneration_projection_for_spec(self, _spec):
                    return dict(self.projection)

                def product_regeneration_projection(self):
                    return dict(self.projection)

                def status(self):
                    return SimpleNamespace(
                        phase="product_regeneration_complete",
                        generation_id="G-0001",
                        last_event_hash="a" * 64,
                    )

                def regenerate_product(self, *, reason: str, idempotency_key: str | None = None):
                    del reason
                    calls.append(idempotency_key)
                    self.projection = {
                        "status": "requested",
                        "pending": True,
                        "request_id": idempotency_key,
                        "idempotency_key": idempotency_key,
                    }
                    return SimpleNamespace(status="ready", phase="product_regeneration_requested")

                def close(self, *, wait_for_roles: bool = False):
                    assert wait_for_roles is False

            fake_api = {
                "RunContext": RunContext,
                "RunCoordinator": FakeCoordinator,
                "CoordinatorProductionBindingMismatch": coordinator_module.CoordinatorProductionBindingMismatch,
                "RunLifecycle": RunLifecycle,
            }
            browser_run_id = repository.list_runs()[0]["id"]
            control_files = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (run_root / "control_plane").iterdir()
                if path.is_file() and path.name != "coordinator.lock"
            }
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                before = manager.status(browser_run_id)
                repeated = manager.status(browser_run_id)
            after_files = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (run_root / "control_plane").iterdir()
                if path.is_file() and path.name != "coordinator.lock"
            }
            assert before["canRegenerateProduct"] is True
            assert before["productRegenerationIdempotencyKey"] == key
            assert before["productRegenerationIdempotencyKey"] == repeated["productRegenerationIdempotencyKey"]
            assert launch_manager.preview_calls == [(run_root.name, run_root.resolve()), (run_root.name, run_root.resolve())]
            assert after_files == control_files

            # POST rebinds first, then recomputes the same key against the
            # rebound coordinator before creating the one Product request.
            FakeCoordinator.rebound = True
            launch_manager.rebound = False
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                result = manager.regenerate_product(browser_run_id, confirmed=True, idempotency_key=key)
            assert result["requested"] is True
            assert calls == [key]
            assert launch_manager.resume_prepare_calls == [(run_root.name, run_root.resolve())]
            assert len(runner.calls) == 1

            # A stale GET key is rejected before the Product request/start
            # boundary after a rebind or accepted-input change.
            launch_manager.rebound = False
            FakeCoordinator.rebound = True
            FakeCoordinator.projection = {
                "status": "accepted",
                "pending": False,
                "eligible": True,
                "request_id": "regen-old",
                "idempotency_key": "new-preview-key",
            }
            controller.current = None
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                with self.assertRaisesRegex(Exception, "intent is stale"):
                    manager.regenerate_product(browser_run_id, confirmed=True, idempotency_key=key)
            assert calls == [key]
            assert len(runner.calls) == 1

    def test_product_regeneration_public_path_previews_and_rebinds_real_coordinator(self) -> None:
        """The public status/action path handles a rotated binding end to end."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _public_preview_fixture(Path(directory))
            context = fixture["context"]
            persisted = fixture["persisted"]
            manager = fixture["manager"]
            launch_manager = fixture["launch_manager"]
            controller = fixture["controller"]
            runner = fixture["runner"]
            run_id = fixture["browser_run_id"]
            phase = fixture["phase"]
            code_home = fixture["code_home"]
            skill_hash = fixture["skill_hash"]
            run_root = context.run_root
            assert isinstance(persisted, RunCoordinator)
            assert isinstance(manager, RunControlManager)
            before = {
                str(path.relative_to(run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in run_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            with patch.object(RunCoordinator, "_phase_snapshot", return_value=phase):
                with patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash):
                    with patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False):
                        first = manager.status(run_id)
                        repeated = manager.status(run_id)
                        after_get = {
                            str(path.relative_to(run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in run_root.rglob("*")
                            if path.is_file() and not path.is_symlink()
                        }
                        assert first["canRegenerateProduct"] is True
                        assert first["productRegenerationIdempotencyKey"]
                        assert first["productRegenerationIdempotencyKey"] == repeated["productRegenerationIdempotencyKey"]
                        assert after_get == before

                        controller.current = None
                        result = manager.regenerate_product(
                            run_id,
                            confirmed=True,
                            idempotency_key=first["productRegenerationIdempotencyKey"],
                        )
            try:
                assert result["requested"] is True
                assert len(runner.calls) == 1
                state = json.loads((run_root / "control_plane" / "coordinator_state.json").read_text(encoding="utf-8"))
                request = state["product_regeneration"]
                assert request["status"] == "requested"
                assert request["request_id"] == first["productRegenerationIdempotencyKey"]
                persisted_spec = json.loads(
                    (run_root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8")
                )
                assert persisted_spec["codex_exec"]["skill_sha256"] == skill_hash
                events = [
                    json.loads(line)
                    for line in (run_root / "control_plane" / "coordinator_events.jsonl").read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]
                event_names = [event["event"] for event in events]
                assert event_names[-3:-1] == [
                    "coordinator_transport_rebind_started",
                    "coordinator_transport_rebound",
                ]
                assert launch_manager is not None
            finally:
                persisted.close(wait_for_roles=True)

    def test_product_regeneration_public_conflict_preflight_is_no_product_write(self) -> None:
        """A real public action returns 409 before Product revision admission writes."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _public_preview_fixture(Path(directory))
            context = fixture["context"]
            persisted = fixture["persisted"]
            manager = fixture["manager"]
            launch_manager = fixture["launch_manager"]
            controller = fixture["controller"]
            runner = fixture["runner"]
            run_id = fixture["browser_run_id"]
            phase = fixture["phase"]
            code_home = fixture["code_home"]
            skill_hash = fixture["skill_hash"]

            # Rebind the fixture once before taking the no-write snapshot.  The
            # operation under test then uses the normal public path with an
            # already-current persisted spec, so a conflict cannot be hidden
            # by an unrelated transport-rebind event.
            with (
                patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash),
                patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False),
            ):
                desired = launch_manager.preview_resume_coordinator(fixture["run_id"], context.run_root)["spec"]
                persisted.rebind_transport(desired)

            action = PlannerAction(
                "build_product_candidate",
                "product_agent",
                context.run_id,
                "active Product owner blocks regeneration",
            )
            registry = RoleSessionRegistry(context)
            reservation = registry.prepare(
                action,
                generation_id="G-0001",
                idempotency_key="active-product-owner",
            )
            assert reservation["mode"] == "new"
            with (
                patch.object(RunCoordinator, "_phase_snapshot", return_value=phase),
                patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash),
                patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False),
            ):
                intent = manager.status(run_id)["productRegenerationIdempotencyKey"]
            assert isinstance(intent, str) and intent
            before = {
                str(path.relative_to(context.run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in context.run_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            try:
                with (
                    patch.object(RunCoordinator, "_phase_snapshot", return_value=phase),
                    patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash),
                    patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False),
                ):
                    with self.assertRaisesRegex(LaunchConflictError, "reservation is active"):
                        manager.regenerate_product(
                            run_id,
                            confirmed=True,
                            reason="blocked before Product revision creation",
                            idempotency_key=intent,
                        )
                after = {
                    str(path.relative_to(context.run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in context.run_root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                self.assertEqual(after, before)
                self.assertEqual(runner.calls, [])
                self.assertEqual(
                    registry.read(),
                    json.loads(registry.registry_path.read_text(encoding="utf-8")),
                )
            finally:
                registry.release_reservation(
                    f"product_agent:{context.run_id}:G-0001",
                    "active-product-owner",
                )
                persisted.close(wait_for_roles=True)

    def test_product_regeneration_status_rejects_malformed_rebind_intent_without_preview(self) -> None:
        """A malformed recovery intent never receives the stale-binding preview fallback."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _public_preview_fixture(Path(directory))
            context = fixture["context"]
            persisted = fixture["persisted"]
            manager = fixture["manager"]
            launch_manager = fixture["launch_manager"]
            runner = fixture["runner"]
            run_id = fixture["browser_run_id"]
            phase = fixture["phase"]
            code_home = fixture["code_home"]
            skill_hash = fixture["skill_hash"]
            intent_path = context.run_root / "control_plane" / ".coordinator_transport_rebind.intent.json"
            intent_path.write_text('{"malformed":true}\n', encoding="utf-8")
            before = {
                str(path.relative_to(context.run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in context.run_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            try:
                with patch.object(RunCoordinator, "_phase_snapshot", return_value=phase):
                    with patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash):
                        with patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False):
                            with patch.object(launch_manager, "preview_resume_coordinator", wraps=launch_manager.preview_resume_coordinator) as preview:
                                status = manager.status(run_id)
                                preview.assert_not_called()
                            with self.assertRaises(coordinator_module.CoordinatorIntegrityError):
                                launch_manager.preview_resume_coordinator(fixture["run_id"], context.run_root)
                assert status["canRegenerateProduct"] is False
                assert status["productRegenerationIdempotencyKey"] is None
                assert runner.calls == []
                after = {
                    str(path.relative_to(context.run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in context.run_root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                assert after == before
            finally:
                persisted.close(wait_for_roles=True)

    def test_product_regeneration_status_rejects_pending_transport_rebind_before_preview(self) -> None:
        """GET never projects a key through a valid half-complete rebind transaction."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _public_preview_fixture(Path(directory))
            context = fixture["context"]
            persisted = fixture["persisted"]
            manager = fixture["manager"]
            launch_manager = fixture["launch_manager"]
            run_id = fixture["browser_run_id"]
            phase = fixture["phase"]
            code_home = fixture["code_home"]
            skill_hash = fixture["skill_hash"]

            with (
                patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash),
                patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False),
            ):
                target = launch_manager.preview_resume_coordinator(fixture["run_id"], context.run_root)["spec"]

            def fail_after_started(name: str) -> None:
                if name == "transport_rebind_after_started":
                    raise KeyboardInterrupt(name)

            persisted._failpoint = fail_after_started
            try:
                with self.assertRaises(KeyboardInterrupt):
                    persisted.rebind_transport(target)
                state = json.loads((context.run_root / "control_plane" / "coordinator_state.json").read_text(encoding="utf-8"))
                assert isinstance(state.get("pending_transport_rebind"), dict)
                assert persisted.transport_rebind_intent_path.is_file()

                before = {
                    str(path.relative_to(context.run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in context.run_root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                with (
                    patch.object(RunCoordinator, "_phase_snapshot", return_value=phase),
                    patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash),
                    patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False),
                    patch.object(launch_manager, "preview_resume_coordinator", wraps=launch_manager.preview_resume_coordinator) as preview,
                ):
                    status = manager.status(run_id)
                preview.assert_not_called()
                assert status["canRegenerateProduct"] is False
                assert status["productRegenerationIdempotencyKey"] is None
                after = {
                    str(path.relative_to(context.run_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in context.run_root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                assert after == before

                # The mutating preparation boundary remains the supported
                # recovery path once an operator submits a Product action.
                with (
                    patch.object(coordinator_module, "PRODUCTION_SKILL_SHA256", skill_hash),
                    patch.dict(os.environ, {"CODEX_HOME": str(code_home)}, clear=False),
                ):
                    prepared = launch_manager.prepare_resume_coordinator(fixture["run_id"], context.run_root)
                assert prepared["operation"] == "run"
                assert not persisted.transport_rebind_intent_path.exists()
            finally:
                persisted.close(wait_for_roles=True)

    def test_product_regeneration_status_does_not_preview_generic_integrity_error(self) -> None:
        """Only the typed production-release mismatch may invoke read-only preview."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _public_preview_fixture(Path(directory))
            launch_manager = fixture["launch_manager"]
            manager = fixture["manager"]
            run_id = fixture["browser_run_id"]

            class GenericFailureCoordinator:
                @classmethod
                def from_persisted_spec(cls, _context):
                    raise coordinator_module.CoordinatorIntegrityError("persisted state is malformed")

            fake_api = {
                "RunContext": RunContext,
                "RunCoordinator": GenericFailureCoordinator,
                "RunLifecycle": RunLifecycle,
            }
            with (
                patch.object(launch_manager, "_core_imports", return_value=fake_api),
                patch.object(launch_manager, "preview_resume_coordinator") as preview,
            ):
                status = manager.status(run_id)
            assert status["canRegenerateProduct"] is False
            assert status["productRegenerationIdempotencyKey"] is None
            preview.assert_not_called()

    def test_product_regeneration_next_intent_changes_after_accept_or_failure_and_pending_is_stable(self) -> None:
        """UI keys follow the backend terminal outcome, not the old request id."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-PRODUCT-REGENERATION-NEXT-KEY"
            context = RunContext(run_id=run_root.name, run_root=run_root)
            RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            launch_manager = LaunchManager(settings, repository=repository, runner=FakeRunner(controller))
            manager = RunControlManager(launch_manager, process_controller=controller)

            class FakeCoordinator:
                projection: dict[str, object] = {
                    "status": "accepted",
                    "pending": False,
                    "request_id": "regen-rev2",
                    "idempotency_key": "regen-rev3-intent",
                }

                @classmethod
                def from_persisted_spec(cls, _context):
                    return cls()

                def product_regeneration_projection(self):
                    return dict(self.projection)

                def status(self):
                    return SimpleNamespace(
                        phase="product_regeneration_complete",
                        generation_id="G-0001",
                        last_event_hash="a" * 64,
                    )

                def close(self, *, wait_for_roles: bool = False):
                    assert wait_for_roles is False

            fake_api = {
                "RunContext": RunContext,
                "RunCoordinator": FakeCoordinator,
                "RunLifecycle": RunLifecycle,
            }
            browser_run_id = repository.list_runs()[0]["id"]
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                accepted_first = manager.status(browser_run_id)
                accepted_second = manager.status(browser_run_id)
                assert accepted_first["canRegenerateProduct"] is True
                assert accepted_first["productRegenerationPending"] is False
                assert accepted_first["productRegenerationIdempotencyKey"] == "regen-rev3-intent"
                assert accepted_first["productRegenerationIdempotencyKey"] == accepted_second["productRegenerationIdempotencyKey"]

                FakeCoordinator.projection = {
                    "status": "failed",
                    "pending": False,
                    "request_id": "regen-rev2",
                    "revision_id": "rev-0002",
                    "idempotency_key": "regen-retry-rev3-intent",
                }
                failed_first = manager.status(browser_run_id)
                failed_second = manager.status(browser_run_id)
                assert failed_first["canRegenerateProduct"] is True
                assert failed_first["productRegenerationPending"] is False
                assert failed_first["productRegenerationIdempotencyKey"] == "regen-retry-rev3-intent"
                assert failed_first["productRegenerationIdempotencyKey"] != accepted_first["productRegenerationIdempotencyKey"]
                assert failed_first["productRegenerationIdempotencyKey"] == failed_second["productRegenerationIdempotencyKey"]

                FakeCoordinator.projection = {
                    "status": "requested",
                    "pending": True,
                    "request_id": "regen-rev3",
                    "idempotency_key": "regen-rev3",
                }
                pending = manager.status(browser_run_id)
                assert pending["canRegenerateProduct"] is False
                assert pending["productRegenerationPending"] is True
                assert pending["productRegenerationIdempotencyKey"] == "regen-rev3"

    def test_product_regeneration_invalid_terminal_projection_is_not_clickable(self) -> None:
        """A failed current binding must not fall back to a legacy event key."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-PRODUCT-REGENERATION-INVALID-KEY"
            context = RunContext(run_id=run_root.name, run_root=run_root)
            RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            launch_manager = LaunchManager(settings, repository=repository, runner=FakeRunner(controller))
            manager = RunControlManager(launch_manager, process_controller=controller)

            class FakeCoordinator:
                @classmethod
                def from_persisted_spec(cls, _context):
                    return cls()

                def product_regeneration_projection(self):
                    return {"status": "failed", "pending": False, "eligible": False, "idempotency_key": None}

                def status(self):
                    return SimpleNamespace(phase="product_regeneration_failed", generation_id="G-0001", last_event_hash="a" * 64)

                def close(self, *, wait_for_roles: bool = False):
                    assert wait_for_roles is False

            fake_api = {"RunContext": RunContext, "RunCoordinator": FakeCoordinator, "RunLifecycle": RunLifecycle}
            browser_run_id = repository.list_runs()[0]["id"]
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                status = manager.status(browser_run_id)
            assert status["canRegenerateProduct"] is False
            assert status["productRegenerationIdempotencyKey"] is None

            with (
                patch.object(launch_manager, "_core_imports", return_value=fake_api),
                patch.object(launch_manager, "prepare_resume_coordinator", return_value={"operation": "run"}),
                patch.object(manager, "_start_supervisor") as start_supervisor,
            ):
                with self.assertRaisesRegex(Exception, "current Product evidence"):
                    manager.regenerate_product(
                        browser_run_id,
                        confirmed=True,
                        idempotency_key="invalid-terminal-intent",
                    )
                self.assertFalse(start_supervisor.called)

    def test_product_regeneration_old_terminal_key_is_noop_and_new_intent_starts_once(self) -> None:
        """A terminal key does not restart the Supervisor; a fresh intent does."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_root = runs / "RUN-PRODUCT-REGENERATION-START-KEY"
            context = RunContext(run_id=run_root.name, run_root=run_root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            settings = LaunchSettings(
                runtime_root=root,
                runs_root=runs,
                source_roots=(root,),
                state_root=root / "state",
                enable_launch=True,
                launch_token="run-control-token",
            )
            repository = OperationalRepository(None, [runs], launch_state_root=settings.state_root)
            controller = FakeProcessController()
            controller.current = None
            runner = FakeRunner(controller)
            launch_manager = FakeResumeLaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)

            class FakeCoordinator:
                projection: dict[str, object] = {
                    "status": "accepted",
                    "pending": False,
                    "request_id": "regen-rev2",
                    "idempotency_key": "regen-rev3-intent",
                }
                calls: list[str | None] = []

                @classmethod
                def from_persisted_spec(cls, _context):
                    return cls()

                def product_regeneration_projection(self):
                    return dict(self.projection)

                def status(self):
                    return SimpleNamespace(
                        phase="product_regeneration_complete"
                        if self.projection.get("status") in {"accepted", "failed"}
                        else "product_regeneration_requested",
                        generation_id="G-0001",
                        last_event_hash="a" * 64,
                    )

                def regenerate_product(self, *, reason: str, idempotency_key: str | None = None):
                    del reason
                    self.calls.append(idempotency_key)
                    if idempotency_key == self.projection.get("request_id"):
                        return SimpleNamespace(status="accepted", phase="product_regeneration_complete")
                    self.projection = {
                        "status": "requested",
                        "pending": True,
                        "request_id": idempotency_key,
                        "idempotency_key": idempotency_key,
                    }
                    return SimpleNamespace(status="ready", phase="product_regeneration_requested")

                def close(self, *, wait_for_roles: bool = False):
                    assert wait_for_roles is False

            fake_api = {
                "RunContext": RunContext,
                "RunCoordinator": FakeCoordinator,
                "RunLifecycle": RunLifecycle,
            }
            browser_run_id = repository.list_runs()[0]["id"]
            with patch.object(launch_manager, "_core_imports", return_value=fake_api):
                old = manager.regenerate_product(
                    browser_run_id,
                    confirmed=True,
                    idempotency_key="regen-rev2",
                )
                assert old["idempotent"] is True
                assert old["requested"] is False
                assert old["startupStatus"] == "pending"
                assert runner.calls == []
                assert lifecycle.status == "initialized"

                # The fresh backend-derived intent creates exactly one pending
                # request and starts the ordinary Supervisor path.
                new = manager.regenerate_product(
                    browser_run_id,
                    confirmed=True,
                    idempotency_key="regen-rev3-intent",
                )
                assert new["requested"] is True
                assert new["idempotent"] is False
                assert len(runner.calls) == 1
                with self.assertRaisesRegex(Exception, "already active"):
                    manager.regenerate_product(
                        browser_run_id,
                        confirmed=True,
                        idempotency_key="regen-rev3-intent",
                    )
                assert len(runner.calls) == 1
                assert FakeCoordinator.calls == ["regen-rev2", "regen-rev3-intent"]


if __name__ == "__main__":
    unittest.main()
