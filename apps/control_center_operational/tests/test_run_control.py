from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.workspace import RunContext

from apps.control_center_operational.launch import LaunchManager, LaunchSettings
from apps.control_center_operational.projection import OperationalRepository
from apps.control_center_operational.run_control import CoordinatorProcess, RunControlManager


class FakeProcessController:
    def __init__(self) -> None:
        self.current = CoordinatorProcess(
            pid=4242,
            pgid=4242,
            command=("python3", "-m", "auto_foundry_core.cli", "coordinator", "run"),
        )
        self.stopped: list[int] = []

    def find(self, run_id: str, run_root: Path):
        return self.current

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
            command=("python3", "-m", "auto_foundry_core.cli", "coordinator", "run"),
        )
        return {"monitorRunId": "coordinator-test", "pid": 5252, "argv": []}


class RunControlTests(unittest.TestCase):
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
            launch_manager = LaunchManager(settings, repository=repository, runner=runner)
            manager = RunControlManager(launch_manager, process_controller=controller)
            browser_run_id = repository.list_runs()[0]["id"]

            paused = manager.pause(browser_run_id, confirmed=True)
            self.assertEqual(paused["lifecycleStatus"], "paused")
            self.assertTrue(paused["canResume"])
            self.assertEqual(controller.stopped, [4242])
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), before)
            self.assertEqual(RunLifecycle.load(context).status, "paused")

            resumed = manager.resume(browser_run_id, confirmed=True)
            self.assertEqual(resumed["lifecycleStatus"], "initialized")
            self.assertTrue(resumed["coordinatorActive"])
            self.assertEqual(resumed["monitorRunId"], "coordinator-test")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0]["coordinator_operation"], "run")
            self.assertEqual(RunLifecycle.load(context).status, "initialized")
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), before)

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


if __name__ == "__main__":
    unittest.main()
