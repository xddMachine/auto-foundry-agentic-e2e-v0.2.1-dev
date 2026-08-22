from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from apps.control_center_operational.launch import LaunchManager, LaunchSettings
from apps.control_center_operational.server import OperationalRepository, OperationalServer, render_index


class FakeRunControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def status(self, run_id: str):
        return {"runId": run_id, "lifecycleStatus": "running", "action": "pause", "canPause": True}

    def pause(self, run_id: str, *, confirmed: bool):
        self.calls.append(("pause", run_id, confirmed))
        return {"runId": run_id, "lifecycleStatus": "paused", "action": "resume", "canResume": True}

    def resume(self, run_id: str, *, confirmed: bool):
        self.calls.append(("resume", run_id, confirmed))
        return {"runId": run_id, "lifecycleStatus": "running", "action": "pause", "canPause": True}


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = LaunchSettings(runtime_root=root, runs_root=root / "runs", source_roots=(root,), launch_token="server-token")
        repository = OperationalRepository(None, [settings.runs_root], launch_state_root=settings.state_root)
        self.server = OperationalServer(("127.0.0.1", 0), repository, LaunchManager(settings, repository=repository))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, *, body: bytes = b"", headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=4)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data

    def test_render_layers_operational_assets_and_copy(self) -> None:
        html = render_index().decode()
        self.assertIn('/base.css', html)
        self.assertIn('/theme.css', html)
        self.assertIn('/operational.css', html)
        self.assertIn('/operational.js', html)
        self.assertIn('max="64"', html)
        self.assertIn("Prepare launch", html)
        self.assertIn('id="runControlButton"', html)
        self.assertNotIn('<em>deferred</em>', html)

    def test_run_pause_resume_endpoints_use_same_token_and_confirmation_boundary(self) -> None:
        fake = FakeRunControl()
        self.server.run_control = fake
        status, body = self.request("GET", "/api/run/status?run_id=run-test")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["action"], "pause")

        payload = json.dumps({"runId": "run-test", "confirmed": True}).encode()
        headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/json",
            "X-Control-Center-Token": "server-token",
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        status, body = self.request("POST", "/api/run/pause", body=payload, headers=headers)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["lifecycleStatus"], "paused")
        status, body = self.request("POST", "/api/run/resume", body=payload, headers=headers)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["lifecycleStatus"], "running")
        self.assertEqual(fake.calls, [("pause", "run-test", True), ("resume", "run-test", True)])

        bad_headers = {**headers, "X-Control-Center-Token": "wrong"}
        status, _ = self.request("POST", "/api/run/pause", body=payload, headers=bad_headers)
        self.assertEqual(status, 403)

    def test_config_and_token_origin_guards(self) -> None:
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertEqual(config["maxAgents"], 64)
        self.assertFalse(config["commandsEnabled"])
        payload = json.dumps({"mode": "new", "projectName": "X", "intakeBlocks": ["r"], "sources": [], "maxAgents": 1, "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0}}).encode()
        status, _ = self.request("POST", "/api/launch/prepare", body=payload, headers={"Content-Length": str(len(payload)), "X-Control-Center-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/launch/prepare", body=payload, headers={"Content-Length": str(len(payload)), "X-Control-Center-Token": "server-token", "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, body = self.request("POST", "/api/launch/prepare", body=payload, headers={"Content-Length": str(len(payload)), "X-Control-Center-Token": "server-token", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["prepared"])

    def test_zip_policy_upload_and_ui_contract(self) -> None:
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertIn("zip", config["sourcePolicy"]["extensions"])
        self.assertIn("md", config["sourcePolicy"]["zipMemberExtensions"])

        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("data.csv", b"id\n1\n")
        body = archive_bytes.getvalue()
        headers = {"Content-Length": str(len(body)), "X-Control-Center-Token": "server-token"}
        status, response = self.request(
            "POST",
            "/api/launch/upload?filename=dataset.zip&relative_path=folder%2Fdataset.zip",
            body=body,
            headers=headers,
        )
        self.assertEqual(status, 201)
        uploaded = json.loads(response)
        self.assertEqual(uploaded["relativePath"], "folder/dataset.zip")

        repository_root = Path(__file__).resolve().parents[2]
        index = (repository_root / "control_center" / "static" / "index.html").read_text()
        base_script = (repository_root / "control_center" / "static" / "app.js").read_text()
        operational_script = (repository_root / "control_center_operational" / "static" / "operational.js").read_text()
        self.assertIn(".zip", index)
        self.assertIn("ZIP", index)
        self.assertIn('"zip"', base_script)
        self.assertIn('"zip"', operational_script)
        self.assertIn("validationError", operational_script)
        self.assertIn("operationalValidationDetails", operational_script)
        self.assertIn("const remoteUrlIndex = localPathIndex + (sourcePath ? 1 : 0);", operational_script)
        self.assertIn("index === remoteUrlIndex", operational_script)
        self.assertIn("error.errors", base_script)

    def test_host_header_must_be_explicit_loopback_endpoint(self) -> None:
        hostile = {"Host": f"evil.example:{self.port}", "Origin": f"http://evil.example:{self.port}"}
        status, _ = self.request("GET", "/api/config", headers=hostile)
        self.assertEqual(status, 403)
        missing = {"Host": "", "Origin": f"http://127.0.0.1:{self.port}"}
        status, _ = self.request("GET", "/api/config", headers=missing)
        self.assertEqual(status, 403)
        alternate_port = {"Host": f"127.0.0.1:{self.port + 1}", "Origin": f"http://127.0.0.1:{self.port + 1}"}
        status, _ = self.request("GET", "/api/config", headers=alternate_port)
        self.assertEqual(status, 403)
        alternate_origin = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port + 1}"}
        status, _ = self.request("GET", "/api/config", headers=alternate_origin)
        self.assertEqual(status, 403)

    def test_upload_stream_endpoint(self) -> None:
        body = b"a,b\n1,2\n"
        headers = {"Content-Length": str(len(body)), "X-Control-Center-Token": "server-token"}
        status, response = self.request("POST", "/api/launch/upload?filename=x.csv&relative_path=folder%2Fx.csv", body=body, headers=headers)
        self.assertEqual(status, 201)
        result = json.loads(response)
        self.assertEqual(result["relativePath"], "folder/x.csv")
        self.assertEqual(len(result["sha256"]), 64)

    def test_runs_api_exposes_persisted_launch_placeholder_and_snapshot(self) -> None:
        settings = self.server.manager.settings
        run_root = settings.runs_root / "RUN-HTTP-PLACEHOLDER"
        draft_root = settings.state_root / "drafts"
        status_root = settings.state_root / "statuses"
        draft_root.mkdir(parents=True)
        status_root.mkdir(parents=True)
        draft = {
            "draftId": "D-http-placeholder",
            "projectName": "HTTP semantic intake",
            "runId": "RUN-HTTP-PLACEHOLDER",
            "runRoot": str(run_root),
            "createdAt": "2026-08-20T13:00:00Z",
            "status": "prepared",
        }
        draft["fingerprint"] = LaunchManager._fingerprint(
            {key: value for key, value in draft.items() if key not in {"fingerprint", "status"}}
        )
        (draft_root / "D-http-placeholder.json").write_text(json.dumps(draft), encoding="utf-8")
        (status_root / "D-http-placeholder.json").write_text(json.dumps({
            **draft,
            "status": "starting",
            "startedAt": "2026-08-20T13:00:01Z",
            "message": "Interpreting requirements",
        }), encoding="utf-8")
        status, body = self.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        runs = json.loads(body)["runs"]
        placeholder = next(run for run in runs if run.get("placeholder"))
        self.assertEqual(placeholder["name"], "HTTP semantic intake")
        self.assertEqual(placeholder["requirementCount"], 0)
        status, body = self.request("GET", f"/api/snapshot?run_id={placeholder['id']}")
        self.assertEqual(status, 200)
        snapshot = json.loads(body)
        self.assertEqual(snapshot["run"]["id"], placeholder["id"])
        self.assertEqual(snapshot["projection"]["source"], "launch_placeholder")


if __name__ == "__main__":
    unittest.main()
