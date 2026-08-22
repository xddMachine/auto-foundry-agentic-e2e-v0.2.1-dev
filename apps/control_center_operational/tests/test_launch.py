from __future__ import annotations

import io
import json
import hashlib
import os
import signal
import stat
import struct
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from auto_foundry_core.workbench import DataRoom
from auto_foundry_core.workspace import RunContext
import auto_foundry_core.coordinator as coordinator_module
from apps.control_center_operational.launch import (
    _BoundedRedirectHandler,
    _PinnedHTTPSConnection,
    _resolve_public_host,
    LaunchManager,
    CodexRequirementIntakePlanner,
    LaunchConflictError,
    LaunchSettings,
    LockedLaunchError,
    MAX_ZIP_MEMBER_COUNT,
    ZIP64_EOCD_LOCATOR_BYTES,
    default_codex_binary,
    _planner_plan_hash,
    SubprocessRunner,
    validate_remote_url,
    _inspect_zip_source,
)
from apps.control_center_operational.projection import OperationalRepository


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.intake_responses: list[dict[str, object]] = []

    def start(self, **kwargs):
        self.calls.append(kwargs)
        return {"monitorRunId": "fake-monitor"}

    def plan_intake(self, *, intake_blocks, existing_plan, **_kwargs):
        if self.intake_responses:
            return self.intake_responses.pop(0)
        requirements = []
        groups = []
        if existing_plan:
            for group in existing_plan["groups"]:
                groups.append({
                    "members": list(group["requirement_ids"]),
                    "rationale": group["rationale"],
                    "sharedAnalysisIntent": group.get("shared_analysis_intent"),
                    "suggestedSpecialists": list(group.get("suggested_specialists", [])),
                })
        for index, block in enumerate(intake_blocks, start=1):
            candidate_id = f"C-{index:03d}"
            requirements.append({
                "candidateId": candidate_id,
                "sourceSpans": [{"blockId": block["blockId"], "start": 0, "end": len(block["text"])}],
                "businessObjective": block["text"].strip(),
                "expectedAnalyticalOutputs": [],
                "expectedVisualOutputs": [],
                "dependencies": [],
                "dataNeeds": [],
                "ontologyNeeds": [],
                "preparedDataNeeds": [],
                "workingDefinitions": [],
                "limitations": [],
                "explicitPriority": None,
                "scope": "analytics",
            })
            groups.append({
                "members": [candidate_id],
                "rationale": "Planner selected one independent decision requirement.",
                "sharedAnalysisIntent": None,
                "suggestedSpecialists": [],
            })
        return {
            "schemaVersion": 1,
            "portfolioStrategy": "semantic requirement decomposition and dependency-aware scheduling",
            "requirements": requirements,
            "groups": groups,
            "unassignedContext": [],
        }


def fake_intake_response(blocks, *, candidates=None, groups=None, strategy="semantic test plan"):
    requirements = []
    candidates = candidates or [{} for _ in blocks]
    for index, (text, overrides) in enumerate(zip(blocks, candidates), start=1):
        candidate_id = f"C-{index:03d}"
        value = {
            "candidateId": candidate_id,
            "sourceSpans": [{"blockId": f"INPUT-{index:03d}", "start": 0, "end": len(text)}],
            "businessObjective": text,
            "expectedAnalyticalOutputs": [],
            "expectedVisualOutputs": [],
            "dependencies": [],
            "dataNeeds": [],
            "ontologyNeeds": [],
            "preparedDataNeeds": [],
            "workingDefinitions": [],
            "limitations": [],
            "explicitPriority": None,
            "scope": "analytics",
        }
        value.update(overrides)
        requirements.append(value)
    if groups is None:
        groups = [[f"C-{index:03d}"] for index in range(1, len(blocks) + 1)]
    return {
        "schemaVersion": 1,
        "portfolioStrategy": strategy,
        "requirements": requirements,
        "groups": [
            {
                "members": members,
                "rationale": "Planner selected this cognitive execution group.",
                "sharedAnalysisIntent": None,
                "suggestedSpecialists": [],
            }
            for members in groups
        ],
        "unassignedContext": [],
    }


class RecordingProcess:
    pid = 91234


class RecordingPopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return RecordingProcess()


class CompletedRun:
    returncode = 0


class CrashAfterAppendManager(LaunchManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.crash_once = True

    def _bootstrap_continue(self, draft, run_root, intent=None):
        result = super()._bootstrap_continue(draft, run_root, intent=intent)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("test failpoint after append")
        return result


class CrashAfterArtifactManager(LaunchManager):
    def __init__(self, *args, fail_filename: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_filename = fail_filename
        self.crash_once = True

    def _write_launch_artifact(self, run_root, draft_id, filename, value):
        path = super()._write_launch_artifact(run_root, draft_id, filename, value)
        if self.crash_once and filename == self.fail_filename:
            self.crash_once = False
            raise RuntimeError(f"test failpoint after {filename}")
        return path


def _legacy_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _legacy_hash(value: object) -> str:
    return hashlib.sha256(_legacy_json_bytes(value)).hexdigest()


def _legacy_state_hash(value: dict[str, object]) -> str:
    snapshot = dict(value)
    snapshot["last_event_hash"] = ""
    return _legacy_hash(snapshot)


def _zip_payload(entries: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, payload in entries:
            if name.endswith("/"):
                # Directory records carry no payload.  Some writers emit a
                # small deflate stream for an empty directory; use the
                # canonical stored form so security tests can distinguish it
                # from a forged non-empty directory record.
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, payload)
            else:
                archive.writestr(name, payload)
    return output.getvalue()


def _xlsx_payload() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Orders"
    sheet.append(["order_id", "amount"])
    sheet.append(["A-1", 10])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _mark_zip_encrypted(payload: bytes) -> bytes:
    """Set ZIP general-purpose encryption flags in local and central headers."""

    value = bytearray(payload)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        cursor = 0
        while True:
            position = value.find(signature, cursor)
            if position < 0:
                break
            flags = int.from_bytes(value[position + flag_offset : position + flag_offset + 2], "little")
            value[position + flag_offset : position + flag_offset + 2] = (flags | 0x1).to_bytes(2, "little")
            cursor = position + len(signature)
    return bytes(value)


def _forge_raw_nul_filename(payload: bytes, suffix: bytes = b"\x00xxx") -> bytes:
    """Insert a raw NUL suffix into one local and central filename."""

    value = bytearray(payload)
    local = value.find(b"PK\x03\x04")
    local_length = int.from_bytes(value[local + 26 : local + 28], "little")
    local_end = local + 30 + local_length
    value[local + 26 : local + 28] = (local_length + len(suffix)).to_bytes(2, "little")
    value[local_end:local_end] = suffix
    central = value.find(b"PK\x01\x02")
    central_length = int.from_bytes(value[central + 28 : central + 30], "little")
    central_end = central + 46 + central_length
    value[central + 28 : central + 30] = (central_length + len(suffix)).to_bytes(2, "little")
    value[central_end:central_end] = suffix
    eocd = value.rfind(b"PK\x05\x06")
    size_cd = int.from_bytes(value[eocd + 12 : eocd + 16], "little")
    offset_cd = int.from_bytes(value[eocd + 16 : eocd + 20], "little")
    value[eocd + 12 : eocd + 16] = (size_cd + len(suffix)).to_bytes(4, "little")
    value[eocd + 16 : eocd + 20] = (offset_cd + len(suffix)).to_bytes(4, "little")
    return bytes(value)


def _forge_directory_payload(payload: bytes, *, size: int = 1) -> bytes:
    """Set nonzero local/central sizes on a directory entry."""

    value = bytearray(payload)
    local = value.find(b"PK\x03\x04")
    value[local + 18 : local + 22] = size.to_bytes(4, "little")
    value[local + 22 : local + 26] = size.to_bytes(4, "little")
    central = value.find(b"PK\x01\x02")
    value[central + 20 : central + 24] = size.to_bytes(4, "little")
    value[central + 24 : central + 28] = size.to_bytes(4, "little")
    return bytes(value)


def _forge_compression_method(payload: bytes, method: int) -> bytes:
    value = bytearray(payload)
    local = value.find(b"PK\x03\x04")
    value[local + 8 : local + 10] = method.to_bytes(2, "little")
    central = value.find(b"PK\x01\x02")
    value[central + 10 : central + 12] = method.to_bytes(2, "little")
    return bytes(value)


def _forge_eocd_fields(
    payload: bytes,
    *,
    entries: int | None = None,
    central_size: int | None = None,
    disk: int | None = None,
) -> bytes:
    value = bytearray(payload)
    eocd = value.rfind(b"PK\x05\x06")
    if entries is not None:
        value[eocd + 8 : eocd + 10] = int(entries).to_bytes(2, "little")
        value[eocd + 10 : eocd + 12] = int(entries).to_bytes(2, "little")
    if central_size is not None:
        value[eocd + 12 : eocd + 16] = int(central_size).to_bytes(4, "little")
    if disk is not None:
        value[eocd + 4 : eocd + 6] = int(disk).to_bytes(2, "little")
    return bytes(value)


def _zip64_one_member_payload() -> bytes:
    payload = _zip_payload([("safe.csv", b"x")], compression=zipfile.ZIP_STORED)
    eocd = payload.rfind(b"PK\x05\x06")
    _, _, _, _, _, central_size, central_offset, _ = struct.unpack_from("<4s4H2LH", payload, eocd)
    prefix = payload[:eocd]
    zip64_offset = len(prefix)
    zip64 = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        1,
        1,
        central_size,
        central_offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1)
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return prefix + zip64 + locator + end


def _write_legacy_g5_control_plane(run_root: Path, *, run_id: str, generation_id: str) -> Path:
    """Write the exact legacy wrapper shape accepted by the import audit."""

    destination = run_root / "control_plane"
    destination.mkdir(parents=True, exist_ok=True)
    planner_hash = hashlib.sha256(b"legacy-planner").hexdigest()
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
        "planner_ref": "planner://legacy-g5",
        "policy": {},
        "publication_policy": {"enabled": True, "legacy_channel": "legacy"},
        "role_dispatch_command": ["legacy-role-command"],
        "run_id": run_id,
    }
    lineage = {
        "active_generation_pointer_hash": hashlib.sha256(b"legacy-pointer").hexdigest(),
        "generation_id": generation_id,
        "manifest_hash": hashlib.sha256(b"legacy-manifest").hexdigest(),
        "plan_hash": planner_hash,
        "planner_hash": planner_hash,
        "planner_ref": "planner://legacy-g5",
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
        "planner_ref": "planner://legacy-g5",
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
        "planner_ref": "planner://legacy-g5",
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
    return destination


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_codex_home = os.environ.get("CODEX_HOME")
        self._saved_skill_hash = coordinator_module.PRODUCTION_SKILL_SHA256
        self._saved_test_skill_hash = os.environ.get("AUTO_FOUNDRY_TEST_SKILL_SHA256")
        self._saved_pythonpath = os.environ.get("PYTHONPATH")

    def tearDown(self) -> None:
        if self._saved_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._saved_codex_home
        coordinator_module.PRODUCTION_SKILL_SHA256 = self._saved_skill_hash
        if self._saved_test_skill_hash is None:
            os.environ.pop("AUTO_FOUNDRY_TEST_SKILL_SHA256", None)
        else:
            os.environ["AUTO_FOUNDRY_TEST_SKILL_SHA256"] = self._saved_test_skill_hash
        if self._saved_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self._saved_pythonpath

    def settings(self, root: Path, *, enabled: bool = False) -> LaunchSettings:
        source = root / "source"
        source.mkdir(exist_ok=True)
        code_home = root / "codex-home"
        skill_path = code_home / "skills" / coordinator_module.PRODUCTION_SKILL_NAME
        skill_path.mkdir(parents=True, exist_ok=True)
        (skill_path / "SKILL.md").write_text(
            "---\n"
            f"name: {coordinator_module.PRODUCTION_SKILL_NAME}\n"
            "description: synthetic test fixture\n"
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
        coordinator_module.PRODUCTION_SKILL_SHA256 = hashlib.sha256(
            coordinator_module._skill_release_bytes(skill_path)
        ).hexdigest()
        support_root = root / "test-support"
        support_root.mkdir(parents=True, exist_ok=True)
        (support_root / "sitecustomize.py").write_text(
            "import os\n"
            "import auto_foundry_core.coordinator as _coordinator\n"
            "_value = os.environ.get('AUTO_FOUNDRY_TEST_SKILL_SHA256')\n"
            "if _value:\n"
            "    _coordinator.PRODUCTION_SKILL_SHA256 = _value\n",
            encoding="utf-8",
        )
        os.environ["AUTO_FOUNDRY_TEST_SKILL_SHA256"] = coordinator_module.PRODUCTION_SKILL_SHA256
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(support_root) + (os.pathsep + current_pythonpath if current_pythonpath else "")
        os.environ["CODEX_HOME"] = str(code_home)
        return LaunchSettings(
            runtime_root=root,
            runs_root=root / "runs",
            source_roots=(source,),
            enable_launch=enabled,
            launch_token="test-token",
        )

    def payload(self, manager: LaunchManager, root: Path):
        upload = manager.upload(
            io.BytesIO(b"id,value\n1,2\n"),
            filename="folder/orders.csv",
            relative_path="folder/orders.csv",
            content_length=len(b"id,value\n1,2\n"),
        )
        local = root / "source" / "local.json"
        local.write_text('{"ok": true}\n', encoding="utf-8")
        return {
            "mode": "new",
            "projectName": "Multiple requirements",
            "intakeBlocks": ["First exact requirement", "Second exact requirement"],
            "sources": [
                {"kind": "upload", "uploadId": upload.upload_id},
                {"kind": "local_path", "path": str(local)},
            ],
            "maxAgents": 64,
            "capacity": {"total": 64, "entityResolution": 32, "analyticalOwner": 8, "specialist": 24},
        }

    def test_default_codex_binary_prefers_executable_desktop_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            bundled.parent.mkdir(parents=True)
            bundled.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bundled.chmod(bundled.stat().st_mode | stat.S_IXUSR)
            with patch("apps.control_center_operational.launch.MACOS_APP_CODEX", bundled), patch(
                "apps.control_center_operational.launch.shutil.which", return_value="/usr/local/bin/codex"
            ):
                self.assertEqual(default_codex_binary(), str(bundled))

    def test_production_intake_planner_uses_read_only_codex_and_exact_raw_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def fake_run(argv, **kwargs):
                calls.append((list(argv), dict(kwargs)))
                output = Path(argv[argv.index("--output-last-message") + 1])
                output.write_text(json.dumps(fake_intake_response(["Requirement 1\nRequirement 2"])), encoding="utf-8")
                return CompletedRun()

            planner = CodexRequirementIntakePlanner("codex", root, run=fake_run)
            result = planner.plan_intake(
                intake_blocks=({"blockId": "INPUT-001", "text": "Requirement 1\nRequirement 2"},),
                existing_plan=None,
                data_room="inputs/data_room.zip",
                document_refs=(),
                role_cwd=root,
                skill_binding={
                    "skill_path": "/tmp/skill",
                    "skill_version": "0.7.1",
                    "core_version": "0.8.0",
                    "skill_sha256": "a" * 64,
                },
            )
            self.assertEqual(result["schemaVersion"], 1)
            argv, kwargs = calls[0]
            self.assertIn("read-only", argv)
            self.assertIn("--ephemeral", argv)
            self.assertFalse(kwargs["shell"])
            self.assertIn("The UI fields are input blocks, not requirement boundaries", kwargs["input"])
            self.assertIn("Requirement 1\\nRequirement 2", kwargs["input"])

    def _initial_continue_context(self, root: Path):
        runner = FakeRunner()
        settings = self.settings(root, enabled=True)
        repository = OperationalRepository(None, [settings.runs_root])
        manager = LaunchManager(settings, repository=repository, runner=runner)
        first = manager.prepare({
            "mode": "new", "projectName": "Artifact recovery", "intakeBlocks": ["Initial"], "sources": [],
            "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
        })
        created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
        discoverable_id = repository.list_runs()[0]["id"]
        return runner, settings, repository, created, discoverable_id

    def test_prepare_is_nonmutating_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            manager = LaunchManager(settings)
            prepared = manager.prepare(self.payload(manager, root))
            self.assertTrue(prepared["valid"])
            self.assertTrue(prepared["prepared"])
            self.assertEqual(prepared["effectiveCapacity"]["total"], 64)
            self.assertEqual(prepared["summary"]["inputBlocks"], 2)
            self.assertFalse(list(settings.runs_root.glob("RUN-*")))
            draft = json.loads((settings.state_root / "drafts" / f"{prepared['draftId']}.json").read_text())
            self.assertEqual(
                draft["intakeBlocks"],
                [
                    {"blockId": "INPUT-001", "text": "First exact requirement"},
                    {"blockId": "INPUT-002", "text": "Second exact requirement"},
                ],
            )
            self.assertEqual(draft["fingerprint"], prepared["fingerprint"])

    def test_prepare_preserves_free_form_edge_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LaunchManager(self.settings(root))
            payload = {
                "mode": "new", "projectName": "Exact", "intakeBlocks": [" padded requirement"], "sources": [],
                "maxAgents": 1, "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
            }
            prepared = manager.prepare(payload)
            self.assertTrue(prepared["valid"])
            draft = json.loads((manager.drafts_root / f"{prepared['draftId']}.json").read_text())
            self.assertEqual(draft["intakeBlocks"][0]["text"], " padded requirement")

    def test_upload_rejects_traversal_and_hashes_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LaunchManager(self.settings(Path(directory)))
            with self.assertRaises(Exception):
                manager.upload(io.BytesIO(b"x"), filename="x.csv", relative_path="../x.csv", content_length=1)
            record = manager.upload(io.BytesIO(b"a,b\n1,2\n"), filename="x.csv", relative_path="folder/x.csv", content_length=8)
            self.assertEqual(record.size, 8)
            self.assertEqual(len(record.sha256), 64)
            self.assertEqual(record.path.read_bytes(), b"a,b\n1,2\n")

    def test_locked_execute_has_no_run_or_status_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=False)
            manager = LaunchManager(settings)
            prepared = manager.prepare(self.payload(manager, root))
            with self.assertRaises(LockedLaunchError):
                manager.execute(draft_id=prepared["draftId"], fingerprint=prepared["fingerprint"], confirmed=True)
            self.assertFalse(list(settings.runs_root.glob("RUN-*")))
            self.assertFalse((settings.state_root / "statuses").exists())

    def test_fake_execute_bootstraps_core_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            manager = LaunchManager(settings, runner=runner)
            prepared = manager.prepare(self.payload(manager, root))
            result = manager.execute(draft_id=prepared["draftId"], fingerprint=prepared["fingerprint"], confirmed=True)
            run_root = Path(result["runRoot"])
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["monitorRunId"], "fake-monitor")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(json.loads((run_root / "entity_resolution" / "state.json").read_text())["capacity"]["total_active"], 64)
            run_state = json.loads((run_root / "run_state.json").read_text())
            self.assertEqual(run_state["item_ids"], ["REQ-001", "REQ-002"])
            plan = json.loads((run_root / "requirement_supervisor_plan.json").read_text())
            self.assertEqual([record["original_text"] for record in plan["input_records"]], ["First exact requirement", "Second exact requirement"])
            self.assertEqual(
                plan["portfolio_strategy"],
                "semantic requirement decomposition and dependency-aware scheduling",
            )
            self.assertEqual(
                [group["requirement_ids"] for group in plan["groups"]],
                [["REQ-001"], ["REQ-002"]],
            )
            self.assertTrue(all("Planner" in group["rationale"] for group in plan["groups"]))
            manifest = json.loads((run_root / "control_center" / "launch_manifest.json").read_text())
            self.assertEqual(
                manifest["intakeBlocks"],
                [
                    {"blockId": "INPUT-001", "text": "First exact requirement"},
                    {"blockId": "INPUT-002", "text": "Second exact requirement"},
                ],
            )
            self.assertEqual(manifest["projectName"], "Multiple requirements")
            with zipfile.ZipFile(run_root / "inputs" / "data_room.zip") as archive:
                self.assertEqual(sorted(archive.namelist()), ["folder/orders.csv", "local.json"])
            self.assertTrue((run_root / "control_center" / "launch_manifest.json").is_file())
            self.assertFalse((settings.runtime_root / "runtime-mutated").exists())
            self.assertEqual(manager.execute(draft_id=prepared["draftId"], fingerprint=prepared["fingerprint"], confirmed=True)["status"], "accepted")

    def test_semantic_planner_splits_five_requirements_from_one_free_form_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            manager = LaunchManager(self.settings(root, enabled=True), runner=runner)
            sections = [
                "Requirement 1: analyse supplier reliability.",
                "Requirement 2: explain customer margin.",
                "Requirement 3: assess inventory availability.",
                "Requirement 4: investigate receivables.",
                "Requirement 5: connect cross-domain operations.",
            ]
            brief = "\n\n".join(sections)
            candidates = []
            groups = []
            for index, section in enumerate(sections, start=1):
                start = brief.index(section)
                candidate_id = f"C-{index:03d}"
                candidates.append({
                    "candidateId": candidate_id,
                    "sourceSpans": [{"blockId": "INPUT-001", "start": start, "end": start + len(section)}],
                    "businessObjective": section,
                    "expectedAnalyticalOutputs": [],
                    "expectedVisualOutputs": [],
                    "dependencies": [],
                    "dataNeeds": [],
                    "ontologyNeeds": [],
                    "preparedDataNeeds": [],
                    "workingDefinitions": [],
                    "limitations": [],
                    "explicitPriority": None,
                    "scope": "analytics",
                })
                groups.append({
                    "members": [candidate_id],
                    "rationale": "Independent business decision.",
                    "sharedAnalysisIntent": None,
                    "suggestedSpecialists": [],
                })
            runner.intake_responses.append({
                "schemaVersion": 1,
                "portfolioStrategy": "five independently scheduled decisions",
                "requirements": candidates,
                "groups": groups,
                "unassignedContext": [],
            })
            prepared = manager.prepare({
                "mode": "new",
                "projectName": "Five requirements",
                "intakeBlocks": [brief],
                "sources": [],
                "maxAgents": 8,
                "capacity": {"total": 8, "entityResolution": 4, "analyticalOwner": 1, "specialist": 3},
            })
            result = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            run_root = Path(result["runRoot"])
            run_state = json.loads((run_root / "run_state.json").read_text())
            self.assertEqual(run_state["item_ids"], [f"REQ-{index:03d}" for index in range(1, 6)])
            plan = json.loads((run_root / "requirement_supervisor_plan.json").read_text())
            self.assertEqual([record["original_text"] for record in plan["input_records"]], sections)

    def test_semantic_planner_cannot_drop_non_whitespace_intake_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            runner.intake_responses.append(fake_intake_response(["first"], strategy="invalid omission"))
            manager = LaunchManager(self.settings(root, enabled=True), runner=runner)
            prepared = manager.prepare({
                "mode": "new",
                "projectName": "Coverage gate",
                "intakeBlocks": ["first and omitted"],
                "sources": [],
                "maxAgents": 2,
                "capacity": {"total": 2, "entityResolution": 1, "analyticalOwner": 1, "specialist": 0},
            })
            with self.assertRaisesRegex(LaunchConflictError, "dropped source text"):
                manager.execute(
                    draft_id=prepared["draftId"],
                    fingerprint=prepared["fingerprint"],
                    confirmed=True,
                )

    def test_document_only_intake_is_planned_without_a_required_text_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            manager = LaunchManager(self.settings(root, enabled=True), runner=runner)
            upload = manager.upload(
                io.BytesIO(b"Investigate margin and inventory as separate decisions.\n"),
                filename="brief.md",
                relative_path="brief.md",
                content_length=56,
            )
            runner.intake_responses.append({
                "schemaVersion": 1,
                "portfolioStrategy": "document-grounded semantic decomposition",
                "requirements": [{
                    "candidateId": "C-001",
                    "sourceSpans": [],
                    "documentRefs": ["brief.md"],
                    "originalText": "Investigate margin and inventory as separate business decisions.",
                    "businessObjective": "Determine the required margin and inventory analyses.",
                    "expectedAnalyticalOutputs": [],
                    "expectedVisualOutputs": [],
                    "dependencies": [],
                    "dataNeeds": [],
                    "ontologyNeeds": [],
                    "preparedDataNeeds": [],
                    "workingDefinitions": [],
                    "limitations": [],
                    "explicitPriority": None,
                    "scope": "analytics",
                }],
                "groups": [{
                    "members": ["C-001"],
                    "rationale": "Document-grounded decision requirement.",
                    "sharedAnalysisIntent": None,
                    "suggestedSpecialists": [],
                }],
                "unassignedContext": [],
            })
            prepared = manager.prepare({
                "mode": "new",
                "projectName": "Document brief",
                "intakeBlocks": [],
                "sources": [{"kind": "upload", "uploadId": upload.upload_id}],
                "maxAgents": 2,
                "capacity": {"total": 2, "entityResolution": 1, "analyticalOwner": 1, "specialist": 0},
            })
            self.assertTrue(prepared["valid"])
            result = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            plan = json.loads((Path(result["runRoot"]) / "requirement_supervisor_plan.json").read_text())
            self.assertEqual(plan["input_records"][0]["source_refs"], ["data_room:brief.md"])

    def test_production_runner_uses_canonical_coordinator_cli_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            popen = RecordingPopen()
            runner = SubprocessRunner("codex", popen=popen)
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(
                settings,
                repository=repository,
                runner=runner,
                intake_planner=FakeRunner(),
            )
            first = manager.prepare({
                "mode": "new",
                "projectName": "Coordinator launch",
                "intakeBlocks": ["Initial"],
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            run_root = Path(created["runRoot"])
            spec_path = run_root / "control_plane" / "coordinator_spec.json"
            self.assertTrue(spec_path.is_file())
            spec = json.loads(spec_path.read_text())
            self.assertEqual(spec["kind"], "run_coordinator_spec")
            self.assertEqual(spec["run_id"], created["runId"])
            self.assertEqual(spec["generation_id"], "G-0001")
            self.assertEqual(spec["codex_exec"]["binary"], settings.codex_bin)
            initial_argv = popen.calls[0][0]
            self.assertEqual(initial_argv[:5], [sys.executable, "-m", "auto_foundry_core.cli", "coordinator", "run"])
            self.assertNotIn("--spec", initial_argv)
            self.assertNotIn("$auto-foundry-agentic-e2e", initial_argv)
            self.assertNotIn("exec", initial_argv)
            initial_kwargs = popen.calls[0][1]
            self.assertEqual(initial_kwargs["cwd"], str(run_root))
            child_python_path = str(Path(__file__).resolve().parents[3] / "src")
            child_python_entries = str(initial_kwargs["env"]["PYTHONPATH"]).split(os.pathsep)
            self.assertEqual(child_python_entries[0], child_python_path)
            self.assertTrue(all(Path(entry).is_absolute() for entry in child_python_entries))

            run_id = repository.list_runs()[0]["id"]
            continued = manager.prepare({
                "mode": "continue",
                "runId": run_id,
                "intakeBlocks": ["Continuation"],
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            manager.execute(draft_id=continued["draftId"], fingerprint=continued["fingerprint"], confirmed=True)
            resume_argv = popen.calls[1][0]
            self.assertEqual(resume_argv[:5], [sys.executable, "-m", "auto_foundry_core.cli", "coordinator", "run"])
            rebound_spec = json.loads(spec_path.read_text())
            self.assertEqual(rebound_spec["generation_id"], "G-0002")
            self.assertNotIn("--spec", resume_argv)
            self.assertNotIn("$auto-foundry-agentic-e2e", resume_argv)
            self.assertNotIn("exec", resume_argv)

    def test_production_child_imports_checkout_and_dispatches_fake_role(self) -> None:
        """Exercise the real module child without model/product transport."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_codex = root / "fake-codex"
            fake_codex.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)
            settings = self.settings(root, enabled=True)
            settings = LaunchSettings(
                runtime_root=settings.runtime_root,
                runs_root=settings.runs_root,
                source_roots=settings.source_roots,
                state_root=settings.state_root,
                max_agents=settings.max_agents,
                codex_bin=str(fake_codex),
                enable_launch=True,
                launch_token=settings.launch_token,
            )
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, intake_planner=FakeRunner())
            prepared = manager.prepare({
                "mode": "new",
                "projectName": "Offline coordinator child",
                "intakeBlocks": ["Dispatch one fake role"],
                "sources": [],
                "maxAgents": 2,
                "capacity": {"total": 2, "entityResolution": 1, "analyticalOwner": 1, "specialist": 0},
            })
            accepted = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            run_root = Path(accepted["runRoot"])
            child_pid = int(accepted["pid"])
            events_path = run_root / "control_plane" / "coordinator_events.jsonl"
            deadline = time.monotonic() + 10.0
            reaped = False
            while time.monotonic() < deadline:
                if events_path.is_file():
                    events = events_path.read_text(encoding="utf-8")
                    if '"event":"dispatch_started"' in events and '"event":"role_exit"' in events:
                        break
                try:
                    waited, _ = os.waitpid(child_pid, os.WNOHANG)
                except ChildProcessError:
                    waited = child_pid
                if waited:
                    reaped = True
                    break
                time.sleep(0.05)
            if not reaped:
                try:
                    os.kill(child_pid, 15)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(child_pid, 0)
                except ChildProcessError:
                    pass
            events = events_path.read_text(encoding="utf-8")
            self.assertIn('"event":"dispatch_started"', events)
            self.assertIn('"event":"role_exit"', events)
            self.assertNotIn("$auto-foundry-agentic-e2e", events)
            self.assertNotIn("coordinator_agent_command", json.loads((run_root / "control_plane" / "coordinator_spec.json").read_text()))

    def test_legacy_g5_continuation_imports_rebinds_and_starts_child(self) -> None:
        """Import a strict G5 chain, rebind G2, and dispatch the real child."""

        from auto_foundry_core import (
            ItemWorkspace,
            RequirementRecord,
            RequirementRunExtension,
            RequirementSupervisorWorkspace,
            RunContext,
            RunLifecycle,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "RUN-G5-LAUNCH"
            _write_legacy_g5_control_plane(root, run_id=run_id, generation_id="G-0001")
            context = RunContext(run_id, root)
            RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            ItemWorkspace.create(context, "REQ-001", mode="requirement", original_text="Legacy requirement")
            workspace = RequirementSupervisorWorkspace(context)
            initial_record = RequirementRecord("REQ-001", "Legacy requirement")
            workspace.plan_requirements((initial_record,), planner_ref="control-center-planner", persist=True)
            RequirementRunExtension.append(
                context,
                RequirementRecord("REQ-002", "Continuation requirement"),
            )
            cumulative_plan = workspace.load()
            self.assertEqual(RunLifecycle.active_generation_id(context), "G-0002")

            fake_codex = root / "fake-codex"
            fake_codex.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)
            base_settings = self.settings(root, enabled=True)
            settings = LaunchSettings(
                runtime_root=base_settings.runtime_root,
                runs_root=base_settings.runs_root,
                source_roots=base_settings.source_roots,
                state_root=base_settings.state_root,
                max_agents=base_settings.max_agents,
                codex_bin=str(fake_codex),
                enable_launch=True,
                launch_token=base_settings.launch_token,
            )
            manager = LaunchManager(settings)
            published: dict[str, object] = {}

            def publish(target_spec: object) -> object:
                extension = RequirementRunExtension.revise(
                    context,
                    plan=cumulative_plan,
                    generation_id="G-0002",
                )
                published["extension"] = extension
                return extension

            coordinator = manager._prepare_coordinator(
                {
                    "context": context,
                    "plan": cumulative_plan.to_dict(),
                    "coordinatorGenerationId": "G-0002",
                    "coordinatorPlannerHash": _planner_plan_hash(cumulative_plan.to_dict()),
                },
                root,
                publisher=publish,
            )
            spec_path = root / "control_plane" / "coordinator_spec.json"
            persisted = json.loads(spec_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["generation_id"], "G-0002")
            self.assertEqual(persisted["publication_policy"], {"enabled": False})
            self.assertEqual(persisted["role_dispatch_command"], [])
            self.assertEqual(persisted["lease_ttl_seconds"], 30.0)
            self.assertEqual(persisted["codex_exec"]["binary"], str(fake_codex))
            self.assertEqual(persisted["codex_exec"]["sandbox"], "workspace-write")
            self.assertTrue(persisted["codex_exec"]["ephemeral"])
            self.assertEqual(persisted["codex_exec"]["skill_version"], "0.7.1")
            self.assertEqual(persisted["codex_exec"]["core_version"], "0.8.0")
            self.assertEqual(
                persisted["codex_exec"]["skill_sha256"],
                coordinator_module.PRODUCTION_SKILL_SHA256,
            )
            skill_path = Path(persisted["codex_exec"]["skill_path"])
            self.assertTrue(skill_path.is_dir())
            self.assertNotIn(".codex/skills", str(skill_path))
            persisted_text = spec_path.read_text(encoding="utf-8")
            self.assertNotIn("role_prompts", persisted_text)
            self.assertNotIn("legacy strict RoleResult prompt", persisted_text)
            self.assertEqual(getattr(published["extension"], "generation_id"), "G-0002")
            events_path = root / "control_plane" / "coordinator_events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["event"] for event in events], ["legacy_imported", "plan_rebind_started", "plan_rebound"])
            self.assertEqual(
                events[-1]["payload"]["transport"]["fields"],
                ["role_dispatch_command", "codex_exec", "lease_ttl_seconds"],
            )
            self.assertEqual(events[-1]["payload"]["publication"]["fields"], ["publication_policy"])
            self.assertNotIn("legacy strict RoleResult prompt", events_path.read_text(encoding="utf-8"))
            self.assertEqual(coordinator["operation"], "run")

            runner = SubprocessRunner(str(fake_codex))
            accepted = runner.start(
                run_id=run_id,
                run_root=root,
                manifest_path=root / "control_center" / "launch_manifest.json",
                capacity={},
                coordinator_operation=coordinator["operation"],
            )
            child_pid = int(accepted["pid"])
            deadline = time.monotonic() + 10.0
            reaped = False
            while time.monotonic() < deadline:
                if events_path.is_file():
                    events_text = events_path.read_text(encoding="utf-8")
                    if '"event":"dispatch_started"' in events_text and '"event":"role_exit"' in events_text:
                        break
                try:
                    waited, _ = os.waitpid(child_pid, os.WNOHANG)
                except ChildProcessError:
                    waited = child_pid
                if waited:
                    reaped = True
                    break
                time.sleep(0.05)
            if not reaped:
                try:
                    os.kill(child_pid, 15)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(child_pid, 0)
                except ChildProcessError:
                    pass
            events_text = events_path.read_text(encoding="utf-8")
            self.assertIn('"event":"dispatch_started"', events_text)
            self.assertIn('"event":"role_exit"', events_text)
            self.assertNotIn("$auto-foundry-agentic-e2e", events_text)

    def test_production_launch_uses_planner_order_and_reusable_need_grouping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            manager = LaunchManager(settings, runner=runner)
            blocks = [
                "First dependent requirement",
                "Second dependent requirement",
                "Independent requirement",
            ]
            runner.intake_responses.append(fake_intake_response(
                blocks,
                candidates=[
                    {"explicitPriority": 20, "dataNeeds": ["shared-orders"]},
                    {"explicitPriority": 1, "dependencies": ["C-001"], "dataNeeds": ["shared-orders"]},
                    {"explicitPriority": 5, "dataNeeds": ["regional-orders"]},
                ],
                groups=[["C-003"], ["C-001", "C-002"]],
            ))
            prepared = manager.prepare({
                "mode": "new",
                "projectName": "Planner-selected portfolio",
                "intakeBlocks": blocks,
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            self.assertTrue(prepared["prepared"])
            result = manager.execute(draft_id=prepared["draftId"], fingerprint=prepared["fingerprint"], confirmed=True)
            run_root = Path(result["runRoot"])
            plan = json.loads((run_root / "requirement_supervisor_plan.json").read_text())
            self.assertEqual(
                [item_id for group in plan["groups"] for item_id in group["requirement_ids"]],
                ["REQ-003", "REQ-001", "REQ-002"],
            )
            self.assertEqual(
                [group["requirement_ids"] for group in plan["groups"]],
                [["REQ-003"], ["REQ-001", "REQ-002"]],
            )
            self.assertEqual(
                [record["requirement_id"] for record in plan["input_records"]],
                ["REQ-001", "REQ-002", "REQ-003"],
            )
            self.assertEqual(
                [record["data_needs"] for record in plan["input_records"]],
                [["shared-orders"], ["shared-orders"], ["regional-orders"]],
            )

    def test_production_continuation_replans_cumulative_records_and_keeps_prior_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, runner=runner)
            initial_blocks = ["First", "Second", "Third"]
            runner.intake_responses.append(fake_intake_response(
                initial_blocks,
                candidates=[
                    {"explicitPriority": 20, "dataNeeds": ["shared"]},
                    {"explicitPriority": 1, "dependencies": ["C-001"], "dataNeeds": ["shared"]},
                    {"explicitPriority": 5, "dataNeeds": ["other"]},
                ],
                groups=[["C-003"], ["C-001", "C-002"]],
            ))
            runner.intake_responses.append(fake_intake_response(
                ["Fourth"],
                candidates=[{"dataNeeds": ["shared"]}],
                groups=[["REQ-003"], ["REQ-001", "REQ-002", "C-001"]],
            ))
            first = manager.prepare({
                "mode": "new",
                "projectName": "Planner continuation",
                "intakeBlocks": initial_blocks,
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            run_id = repository.list_runs()[0]["id"]
            continuation = manager.prepare({
                "mode": "continue",
                "runId": run_id,
                "intakeBlocks": ["Fourth"],
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            result = manager.execute(draft_id=continuation["draftId"], fingerprint=continuation["fingerprint"], confirmed=True)
            run_root = Path(result["runRoot"])
            pointer = json.loads((run_root / "active_generation.json").read_text())
            plan = json.loads((run_root / pointer["plan_ref"]).read_text())
            self.assertEqual(
                [group["requirement_ids"] for group in plan["groups"]],
                [["REQ-003"], ["REQ-001", "REQ-002", "REQ-004"]],
            )
            self.assertEqual(
                [record["requirement_id"] for record in plan["input_records"]],
                ["REQ-001", "REQ-002", "REQ-003", "REQ-004"],
            )
            self.assertEqual(pointer["generation_id"], "G-0002")
            coordinator_events = (run_root / "control_plane" / "coordinator_events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event":"plan_rebound"', coordinator_events)
            coordinator_spec = json.loads((run_root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8"))
            self.assertEqual(coordinator_spec["planner_hash"], hashlib.sha256((run_root / pointer["plan_ref"]).read_bytes()).hexdigest())
            self.assertTrue((run_root / "requirements" / "REQ-001" / "item_state.json").is_file())
            self.assertTrue((run_root / "requirements" / "REQ-004" / "item_state.json").is_file())

    def test_continuation_rejects_active_coordinator_dispatch_without_g2_publication(self) -> None:
        from auto_foundry_core.coordinator import CoordinatorConflictError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_codex = root / "slow-fake-codex"
            fake_codex.write_text("#!/bin/sh\nsleep 5\nexit 3\n", encoding="utf-8")
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)
            base_settings = self.settings(root, enabled=True)
            settings = LaunchSettings(
                runtime_root=base_settings.runtime_root,
                runs_root=base_settings.runs_root,
                source_roots=base_settings.source_roots,
                state_root=base_settings.state_root,
                max_agents=base_settings.max_agents,
                codex_bin=str(fake_codex),
                enable_launch=True,
                launch_token=base_settings.launch_token,
            )
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, intake_planner=FakeRunner())
            first = manager.prepare({
                "mode": "new", "projectName": "Active race", "intakeBlocks": ["Initial"], "sources": [],
                "maxAgents": 2, "capacity": {"total": 2, "entityResolution": 1, "analyticalOwner": 1, "specialist": 0},
            })
            created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            child_pid = int(created["pid"])
            run_root = Path(created["runRoot"])
            events_path = run_root / "control_plane" / "coordinator_events.jsonl"
            try:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if events_path.is_file() and '"event":"dispatch_started"' in events_path.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                self.assertIn('"event":"dispatch_started"', events_path.read_text(encoding="utf-8"))
                run_id = repository.list_runs()[0]["id"]
                continuation = manager.prepare({
                    "mode": "continue", "runId": run_id, "intakeBlocks": ["Blocked append"], "sources": [],
                    "maxAgents": 2, "capacity": {"total": 2, "entityResolution": 1, "analyticalOwner": 1, "specialist": 0},
                })
                with self.assertRaises(CoordinatorConflictError):
                    manager.execute(
                        draft_id=continuation["draftId"],
                        fingerprint=continuation["fingerprint"],
                        confirmed=True,
                    )
                self.assertFalse((run_root / "active_generation.json").exists())
                self.assertFalse((run_root / "extensions" / "G-0002").exists())
                self.assertEqual(
                    json.loads((run_root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8"))["generation_id"],
                    "G-0001",
                )
                events = events_path.read_text(encoding="utf-8")
                self.assertNotIn('"event":"plan_rebound"', events)
            finally:
                try:
                    os.killpg(os.getpgid(child_pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    os.waitpid(child_pid, 0)
                except ChildProcessError:
                    pass

    def test_continuation_retries_after_public_revise_failpoint_and_rebinds_once(self) -> None:
        from auto_foundry_core.coordinator import RunCoordinator

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            initial = LaunchManager(settings, repository=repository, runner=runner)
            first = initial.prepare({
                "mode": "new", "projectName": "Rebind retry", "intakeBlocks": ["Initial"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            created = initial.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            run_id = repository.list_runs()[0]["id"]
            continuation = initial.prepare({
                "mode": "continue", "runId": run_id, "intakeBlocks": ["Retry append"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            original_publish = RunCoordinator.publish_and_rebind
            failed = {"value": False}

            def fail_after_public_revise(coordinator, spec, publisher):
                if failed["value"]:
                    return original_publish(coordinator, spec, publisher)

                def publish_then_fail(target_spec):
                    result = publisher(target_spec)
                    failed["value"] = True
                    raise RuntimeError("failpoint after public revise")

                return original_publish(coordinator, spec, publish_then_fail)

            RunCoordinator.publish_and_rebind = fail_after_public_revise
            try:
                with self.assertRaises(LaunchConflictError):
                    initial.execute(
                        draft_id=continuation["draftId"],
                        fingerprint=continuation["fingerprint"],
                        confirmed=True,
                    )
            finally:
                RunCoordinator.publish_and_rebind = original_publish

            run_root = Path(created["runRoot"])
            self.assertTrue((run_root / "extensions" / "G-0002" / "requirement_supervisor_plan.json").is_file())
            self.assertEqual(json.loads((run_root / "active_generation.json").read_text())["generation_id"], "G-0002")
            self.assertEqual(
                json.loads((run_root / "control_plane" / "coordinator_spec.json").read_text())["generation_id"],
                "G-0001",
            )
            events_path = run_root / "control_plane" / "coordinator_events.jsonl"
            self.assertIn('"event":"plan_rebind_started"', events_path.read_text(encoding="utf-8"))
            recovered = LaunchManager(settings, repository=repository, runner=runner)
            result = recovered.execute(
                draft_id=continuation["draftId"],
                fingerprint=continuation["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(json.loads((run_root / "control_plane" / "coordinator_spec.json").read_text())["generation_id"], "G-0002")
            events = events_path.read_text(encoding="utf-8")
            self.assertEqual(events.count('"event":"plan_rebound"'), 1)
            self.assertEqual(len(runner.calls), 2)

    def test_continuation_uses_public_revision_when_planner_splits_an_existing_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, runner=runner)
            runner.intake_responses.append(fake_intake_response(
                ["First", "Second"],
                candidates=[
                    {"explicitPriority": 1, "dataNeeds": ["shared"]},
                    {"explicitPriority": 20, "dataNeeds": ["shared"]},
                ],
                groups=[["C-001", "C-002"]],
            ))
            runner.intake_responses.append(fake_intake_response(
                ["Inserted"],
                candidates=[{"explicitPriority": 10, "dataNeeds": ["other"]}],
                groups=[["REQ-001"], ["C-001"], ["REQ-002"]],
            ))
            first = manager.prepare({
                "mode": "new",
                "projectName": "Planner revision",
                "intakeBlocks": ["First", "Second"],
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            run_id = repository.list_runs()[0]["id"]
            continuation = manager.prepare({
                "mode": "continue",
                "runId": run_id,
                "intakeBlocks": ["Inserted"],
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            result = manager.execute(draft_id=continuation["draftId"], fingerprint=continuation["fingerprint"], confirmed=True)
            run_root = Path(result["runRoot"])
            pointer = json.loads((run_root / "active_generation.json").read_text())
            plan = json.loads((run_root / pointer["plan_ref"]).read_text())
            self.assertEqual(pointer["generation_id"], "G-0002")
            self.assertEqual(
                [item_id for group in plan["groups"] for item_id in group["requirement_ids"]],
                ["REQ-001", "REQ-003", "REQ-002"],
            )
            self.assertEqual(
                [group["requirement_ids"] for group in plan["groups"]],
                [["REQ-001"], ["REQ-003"], ["REQ-002"]],
            )
            self.assertTrue((run_root / "requirements" / "REQ-001" / "item_state.json").is_file())
            self.assertTrue((run_root / "requirements" / "REQ-002" / "item_state.json").is_file())
            self.assertTrue((run_root / "requirements" / "REQ-003" / "item_state.json").is_file())

    def test_continue_appends_noncolliding_requirements_without_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, runner=runner)
            first = manager.prepare({
                "mode": "new",
                "projectName": "Initial",
                "intakeBlocks": ["Initial requirement"],
                "sources": [],
                "maxAgents": 8,
                "capacity": {"total": 8, "entityResolution": 4, "analyticalOwner": 1, "specialist": 3},
            })
            manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            discoverable_id = repository.list_runs()[0]["id"]
            continued = manager.prepare({
                "mode": "continue",
                "runId": discoverable_id,
                "intakeBlocks": ["Added requirement", "Another added requirement"],
                "sources": [],
                "maxAgents": 8,
                "capacity": {"total": 8, "entityResolution": 4, "analyticalOwner": 1, "specialist": 3},
            })
            self.assertTrue(continued["prepared"])
            result = manager.execute(draft_id=continued["draftId"], fingerprint=continued["fingerprint"], confirmed=True)
            run_root = Path(result["runRoot"])
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(json.loads((run_root / "entity_resolution" / "state.json").read_text())["capacity"]["total_active"], 8)
            active_pointer = json.loads((run_root / "active_generation.json").read_text())
            generation_plan = run_root / active_pointer["plan_ref"]
            plan = json.loads(generation_plan.read_text())
            self.assertEqual([record["requirement_id"] for record in plan["input_records"]], ["REQ-001", "REQ-002", "REQ-003"])
            self.assertEqual(
                plan["portfolio_strategy"],
                "semantic requirement decomposition and dependency-aware scheduling",
            )
            self.assertEqual(
                [group["requirement_ids"] for group in plan["groups"]],
                [["REQ-001"], ["REQ-002"], ["REQ-003"]],
            )
            self.assertFalse((run_root / "inputs" / "data_room.zip").is_symlink())

    def test_continue_resolves_external_catalog_archive_and_preserves_generation_audits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, runner=runner)
            first = manager.prepare({
                "mode": "new", "projectName": "External archive", "intakeBlocks": ["Initial"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            run_root = Path(created["runRoot"])
            archive = root / "source" / "external.zip"
            archive_bytes = b"immutable external archive"
            archive.write_bytes(archive_bytes)
            digest = hashlib.sha256(archive_bytes).hexdigest()
            (run_root / "inputs" / "data_room.zip").unlink()
            context_manifest = {
                "run_id": created["runId"],
                "run_root": str(run_root),
                "input_roots": [str(root / "source")],
                "source_identity": {"content_hash": digest},
            }
            (run_root / "requirements" / "REQ-001" / "work" / "analysis_context.json").write_text(json.dumps(context_manifest), encoding="utf-8")
            catalog_root = run_root / "data_room" / "catalogs"
            catalog_root.mkdir(parents=True, exist_ok=True)
            (catalog_root / "external.json").write_text(json.dumps({
                "source_hash": digest,
                "archive": {"uri": str(archive), "content_hash": digest, "size_bytes": len(archive_bytes)},
            }), encoding="utf-8")
            discoverable_id = repository.list_runs()[0]["id"]
            continuation = manager.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["External append"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            result = manager.execute(draft_id=continuation["draftId"], fingerprint=continuation["fingerprint"], confirmed=True)
            manifest_path = Path(runner.calls[-1]["manifest_path"])
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["dataRoom"], str(archive))
            self.assertEqual(manifest["dataRoomSha256"], digest)
            self.assertFalse((run_root / "inputs" / "data_room.zip").exists())
            self.assertTrue((run_root / "control_center" / "launches" / continuation["draftId"] / "launch_receipt.json").is_file())
            second = manager.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Second external append"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            manager.execute(draft_id=second["draftId"], fingerprint=second["fingerprint"], confirmed=True)
            self.assertTrue((run_root / "control_center" / "launches" / second["draftId"] / "launch_receipt.json").is_file())
            self.assertTrue((run_root / "control_center" / "launches" / continuation["draftId"] / "launch_receipt.json").is_file())

    def test_continue_intent_recovers_after_append_before_runner_and_status_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            initial = LaunchManager(settings, repository=repository, runner=runner)
            first = initial.prepare({
                "mode": "new", "projectName": "Recovery", "intakeBlocks": ["Initial"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            created = initial.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            discoverable_id = repository.list_runs()[0]["id"]
            crashing = CrashAfterAppendManager(settings, repository=repository, runner=runner)
            continuation = crashing.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Recovered append"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            with self.assertRaisesRegex(RuntimeError, "failpoint"):
                crashing.execute(draft_id=continuation["draftId"], fingerprint=continuation["fingerprint"], confirmed=True)
            run_root = Path(created["runRoot"])
            intent_path = run_root / "control_center" / "launches" / continuation["draftId"] / "continuation_intent.json"
            intent = json.loads(intent_path.read_text())
            self.assertEqual(intent["addedItemIds"], ["REQ-002"])
            self.assertEqual(intent["generationId"], "G-0002")
            self.assertTrue(intent["parentStateHash"])
            self.assertTrue(intent["parentPlanHash"])
            generation_states = list((run_root / "extensions").glob("G-*/run_state.json"))
            self.assertEqual(len(generation_states), 1)
            status_path = settings.state_root / "statuses" / f"{continuation['draftId']}.json"
            status_path.unlink()
            recovered = LaunchManager(settings, repository=repository, runner=runner)
            result = recovered.execute(draft_id=continuation["draftId"], fingerprint=continuation["fingerprint"], confirmed=True)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(list((run_root / "extensions").glob("G-*/run_state.json"))), 1)
            self.assertEqual(len(runner.calls), 2)
            plan_path = run_root / "extensions" / "G-0002" / "requirement_supervisor_plan.json"
            plan = json.loads(plan_path.read_text())
            self.assertEqual([record["requirement_id"] for record in plan["input_records"]], ["REQ-001", "REQ-002"])

    def test_continue_artifacts_recover_after_manifest_write_before_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, settings, repository, created, discoverable_id = self._initial_continue_context(root)
            crashing = CrashAfterArtifactManager(
                settings,
                repository=repository,
                runner=runner,
                fail_filename="launch_manifest.json",
            )
            continuation = crashing.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Manifest checkpoint"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            with self.assertRaisesRegex(RuntimeError, "launch_manifest.json"):
                crashing.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
            run_root = Path(created["runRoot"])
            artifact_root = run_root / "control_center" / "launches" / continuation["draftId"]
            manifest_before = artifact_root / "launch_manifest.json"
            self.assertTrue(manifest_before.is_file())
            self.assertFalse((artifact_root / "launch_receipt.json").exists())
            self.assertEqual(len(list((run_root / "extensions").glob("G-*/run_state.json"))), 1)
            status_path = settings.state_root / "statuses" / f"{continuation['draftId']}.json"
            status_path.unlink()

            recovered = LaunchManager(settings, repository=repository, runner=runner)
            result = recovered.execute(
                draft_id=continuation["draftId"],
                fingerprint=continuation["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(len(list((run_root / "extensions").glob("G-*/run_state.json"))), 1)
            self.assertTrue((artifact_root / "launch_receipt.json").is_file())
            self.assertEqual(manifest_before.read_bytes(), (artifact_root / "launch_manifest.json").read_bytes())
            plan_path = run_root / "extensions" / "G-0002" / "requirement_supervisor_plan.json"
            self.assertEqual(
                [record["requirement_id"] for record in json.loads(plan_path.read_text())["input_records"]],
                ["REQ-001", "REQ-002"],
            )

    def test_continue_artifacts_recover_after_receipt_write_before_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, settings, repository, created, discoverable_id = self._initial_continue_context(root)
            crashing = CrashAfterArtifactManager(
                settings,
                repository=repository,
                runner=runner,
                fail_filename="launch_receipt.json",
            )
            continuation = crashing.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Receipt checkpoint"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            with self.assertRaisesRegex(RuntimeError, "launch_receipt.json"):
                crashing.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
            run_root = Path(created["runRoot"])
            artifact_root = run_root / "control_center" / "launches" / continuation["draftId"]
            manifest_before = (artifact_root / "launch_manifest.json").read_bytes()
            receipt_before = (artifact_root / "launch_receipt.json").read_bytes()
            status_path = settings.state_root / "statuses" / f"{continuation['draftId']}.json"
            status_path.unlink()

            recovered = LaunchManager(settings, repository=repository, runner=runner)
            result = recovered.execute(
                draft_id=continuation["draftId"],
                fingerprint=continuation["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(len(list((run_root / "extensions").glob("G-*/run_state.json"))), 1)
            self.assertEqual(manifest_before, (artifact_root / "launch_manifest.json").read_bytes())
            self.assertEqual(receipt_before, (artifact_root / "launch_receipt.json").read_bytes())

    def test_continue_artifact_mismatch_is_rejected_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, settings, repository, created, discoverable_id = self._initial_continue_context(root)
            manager = LaunchManager(settings, repository=repository, runner=runner)
            continuation = manager.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Conflicting artifact"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            run_root = Path(created["runRoot"])
            artifact_root = run_root / "control_center" / "launches" / continuation["draftId"]
            artifact_root.mkdir(parents=True)
            (artifact_root / "launch_manifest.json").write_text(
                json.dumps({"draftId": continuation["draftId"], "fingerprint": "wrong"}),
                encoding="utf-8",
            )
            with self.assertRaises(LaunchConflictError):
                manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(len(runner.calls), 1)
            self.assertFalse((run_root / "control_center" / "launches" / continuation["draftId"] / "continuation_intent.json").exists())
            self.assertEqual(len(list((run_root / "extensions").glob("G-*/run_state.json"))), 0)

    def test_continue_altered_manifest_body_rejected_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, settings, repository, created, discoverable_id = self._initial_continue_context(root)
            manager = LaunchManager(settings, repository=repository, runner=runner)
            continuation = manager.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Manifest body"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            run_root = Path(created["runRoot"])
            draft = manager._load_draft(continuation["draftId"], continuation["fingerprint"])
            intent = manager._ensure_continue_intent(draft, run_root)
            artifact_root = run_root / "control_center" / "launches" / continuation["draftId"]
            altered = manager._continuation_manifest_from_intent(draft, run_root, intent)
            altered["intakeBlocks"] = [{"blockId": "INPUT-001", "text": "Different requirement"}]
            (artifact_root / "launch_manifest.json").write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaises(LaunchConflictError):
                manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(len(list((run_root / "extensions").glob("G-*/run_state.json"))), 0)
            self.assertFalse((settings.state_root / "statuses" / f"{continuation['draftId']}.json").exists())

    def test_continue_altered_receipt_body_rejected_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, settings, repository, created, discoverable_id = self._initial_continue_context(root)
            manager = LaunchManager(settings, repository=repository, runner=runner)
            continuation = manager.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Receipt body"], "sources": [],
                "maxAgents": 4, "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            run_root = Path(created["runRoot"])
            draft = manager._load_draft(continuation["draftId"], continuation["fingerprint"])
            intent = manager._ensure_continue_intent(draft, run_root)
            artifact_root = run_root / "control_center" / "launches" / continuation["draftId"]
            altered = manager._continuation_receipt_from_intent(draft, run_root, intent)
            altered["items"] = ["REQ-001", "REQ-999"]
            (artifact_root / "launch_receipt.json").write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaises(LaunchConflictError):
                manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(len(list((run_root / "extensions").glob("G-*/run_state.json"))), 0)
            self.assertFalse((settings.state_root / "statuses" / f"{continuation['draftId']}.json").exists())

    def test_protected_run_is_rejected_before_continue_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            initial_settings = self.settings(root, enabled=True)
            initial = LaunchManager(initial_settings, runner=runner)
            draft = initial.prepare({
                "mode": "new", "projectName": "Protected", "intakeBlocks": ["Initial"], "sources": [],
                "maxAgents": 1, "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
            })
            created = initial.execute(draft_id=draft["draftId"], fingerprint=draft["fingerprint"], confirmed=True)
            repository = OperationalRepository(None, [initial_settings.runs_root])
            protected_settings = LaunchSettings(runtime_root=root, runs_root=root / "runs", source_roots=(root / "source",), enable_launch=True, launch_token="protected", protected_run_ids=(created["runId"],))
            protected = LaunchManager(protected_settings, repository=repository, runner=runner)
            discoverable_id = repository.list_runs()[0]["id"]
            result = protected.prepare({"mode": "continue", "runId": discoverable_id, "intakeBlocks": ["blocked"], "sources": [], "maxAgents": 1, "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0}})
            self.assertFalse(result["prepared"])
            self.assertIn("protected", result["errors"]["runId"])

    def test_protected_run_is_rechecked_at_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            initial = LaunchManager(settings, repository=repository, runner=runner)
            first = initial.prepare({
                "mode": "new", "projectName": "Protected later", "intakeBlocks": ["Initial"], "sources": [],
                "maxAgents": 1, "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
            })
            created = initial.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            discoverable_id = repository.list_runs()[0]["id"]
            continuation = initial.prepare({
                "mode": "continue", "runId": discoverable_id, "intakeBlocks": ["Must not append"], "sources": [],
                "maxAgents": 1, "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
            })
            self.assertTrue(continuation["prepared"])
            protected_settings = LaunchSettings(
                runtime_root=root,
                runs_root=root / "runs",
                source_roots=(root / "source",),
                enable_launch=True,
                launch_token="protected-at-execute",
                protected_run_ids=(created["runId"],),
            )
            protected = LaunchManager(protected_settings, repository=repository, runner=runner)
            with self.assertRaises(LaunchConflictError):
                protected.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(len(runner.calls), 1)

    def test_hostname_fetch_uses_resolved_ip_and_original_host_header(self) -> None:
        class Response:
            status = 200
            headers = {"Content-Length": "5"}

            def read(self, size=-1):
                value, self.body = getattr(self, "body", b"hello"), b""
                return value[:size]

            def getheader(self, name):
                return self.headers.get(name)

            def close(self):
                return None

        class Connection:
            def __init__(self, args):
                self.args = args
                self.requests = []
                self.response = Response()

            def request(self, method, target, headers):
                self.requests.append((method, target, headers))

            def getresponse(self):
                return self.response

            def close(self):
                return None

        calls = []
        connections = []

        def factory(scheme, original_host, connect_address, port, timeout):
            calls.append((scheme, original_host, connect_address, port))
            connection = Connection(calls[-1])
            connections.append(connection)
            return connection

        handler = _BoundedRedirectHandler(
            timeout=0.01,
            max_redirects=0,
            resolver=lambda host: "8.8.8.8",
            connection_factory=factory,
        )
        data, final_url = handler.fetch("https://example.com/source.csv", max_bytes=32)
        self.assertEqual(data, b"hello")
        self.assertEqual(final_url, "https://example.com/source.csv")
        self.assertEqual(calls, [("https", "example.com", "8.8.8.8", 443)])
        self.assertEqual(connections[0].requests[0][0], "GET")
        self.assertEqual(connections[0].requests[0][2]["Host"], "example.com")

    def test_hostname_redirect_revalidates_and_bounds_body(self) -> None:
        class Response:
            def __init__(self, status, body=b"", location=None):
                self.status = status
                self.body = body
                self.headers = {"Content-Length": str(len(body))}
                if location:
                    self.headers["Location"] = location

            def read(self, size=-1):
                value, self.body = self.body[:size], self.body[size:]
                return value

            def getheader(self, name):
                return self.headers.get(name)

            def close(self):
                return None

        class Connection:
            def __init__(self, response):
                self.response = response

            def request(self, method, target, headers):
                self.headers = headers

            def getresponse(self):
                return self.response

            def close(self):
                return None

        resolved = []
        responses = [
            Response(302, location="https://cdn.example/final.csv"),
            Response(200, body=b"ok"),
        ]

        def resolver(host):
            resolved.append(host)
            return {"example.com": "8.8.8.8", "cdn.example": "1.1.1.1"}[host]

        def factory(scheme, original_host, connect_address, port, timeout):
            return Connection(responses.pop(0))

        handler = _BoundedRedirectHandler(timeout=0.01, max_redirects=1, resolver=resolver, connection_factory=factory)
        data, final_url = handler.fetch("https://example.com/source.csv", max_bytes=8)
        self.assertEqual(data, b"ok")
        self.assertEqual(final_url, "https://cdn.example/final.csv")
        self.assertEqual(resolved, ["example.com", "cdn.example"])
        with self.assertRaisesRegex(ValueError, "globally routable"):
            _BoundedRedirectHandler(timeout=0.01, max_redirects=0, resolver=lambda host: "127.0.0.1", connection_factory=factory).fetch("https://example.com/source.csv", max_bytes=8)
        responses[:] = [Response(200, body=b"too-large")]
        with self.assertRaisesRegex(ValueError, "exceeds"):
            _BoundedRedirectHandler(timeout=0.01, max_redirects=0, resolver=lambda host: "9.9.9.9", connection_factory=factory).fetch("https://example.com/source.csv", max_bytes=3)

    def test_url_policy_rejects_non_global_addresses_for_literal_and_resolved_hosts(self) -> None:
        non_global = (
            "100.64.0.1",      # CGNAT
            "192.0.2.1",       # documentation
            "198.51.100.1",    # documentation
            "203.0.113.1",     # documentation
            "169.254.1.1",    # link-local
            "10.0.0.1",       # private
            "fc00::1",        # IPv6 ULA
            "2001:db8::1",    # IPv6 documentation
            "fe80::1",        # IPv6 link-local
        )
        for address in non_global:
            with self.assertRaises(ValueError, msg=address):
                validate_remote_url(f"https://[{address}]/source.csv" if ":" in address else f"https://{address}/source.csv")
            with self.assertRaises(ValueError, msg=address):
                _BoundedRedirectHandler(
                    timeout=0.01,
                    max_redirects=0,
                    resolver=lambda host, value=address: value,
                    connection_factory=lambda *args: self.fail("private resolver must not connect"),
                ).fetch("https://public.example/source.csv", max_bytes=8)
        from unittest.mock import patch

        for address in non_global:
            with patch("apps.control_center_operational.launch.socket.getaddrinfo", return_value=[(0, 0, 0, "", (address, 0))]):
                with self.assertRaises(ValueError, msg=address):
                    _resolve_public_host("public.example")

    def test_https_connection_pins_socket_but_uses_original_sni(self) -> None:
        from unittest.mock import patch

        class Socket:
            def close(self):
                return None

        class Context:
            def __init__(self):
                self.server_name = None

            def wrap_socket(self, sock, *, server_hostname):
                self.server_name = server_hostname
                return sock

        context = Context()
        with patch("apps.control_center_operational.launch.ssl.create_default_context", return_value=context):
            connection = _PinnedHTTPSConnection("example.com", "8.8.4.4", 443, 0.01)
        with patch("apps.control_center_operational.launch.socket.create_connection", return_value=Socket()) as connect:
            connection.connect()
        self.assertEqual(connect.call_args.args[0], ("8.8.4.4", 443))
        self.assertEqual(context.server_name, "example.com")

    def test_zip_upload_and_absolute_local_prepare_records_member_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            manager = LaunchManager(settings)
            upload_bytes = _zip_payload([("upload/orders.csv", b"id,value\n1,2\n")])
            upload = manager.upload(
                io.BytesIO(upload_bytes),
                filename="incoming.zip",
                relative_path="incoming.zip",
                content_length=len(upload_bytes),
            )
            local = root / "source" / "local.zip"
            local.write_bytes(_zip_payload([("local/events.jsonl", b'{"event":"open"}\n')]))
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "ZIP sources",
                    "intakeBlocks": ["Inspect ZIP sources"],
                    "sources": [
                        {"kind": "upload", "uploadId": upload.upload_id},
                        {"kind": "local_path", "path": str(local)},
                    ],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["prepared"])
            draft = json.loads((settings.state_root / "drafts" / f"{prepared['draftId']}.json").read_text())
            self.assertEqual([source["memberCount"] for source in draft["sources"]], [1, 1])
            self.assertEqual([source["expandedSize"] for source in draft["sources"]], [len(b"id,value\n1,2\n"), len(b'{"event":"open"}\n')])

    def test_zip_mixed_members_flatten_and_open_with_core_dataroom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "source" / "mixed.zip"
            local.parent.mkdir(parents=True)
            local.write_bytes(
                _zip_payload(
                    [
                        ("data/table.csv", b"id,value\n1,2\n"),
                        ("data/events.jsonl", b'{"event":"open"}\n'),
                        ("data/workbook.xlsx", _xlsx_payload()),
                        ("docs/README.md", b"# Read me\n"),
                        ("docs/notes.txt", b"Notes\n"),
                        ("docs/report.pdf", b"%PDF-1.4\n"),
                        ("__MACOSX/._table.csv", b"ignored"),
                        (".DS_Store", b"ignored"),
                        ("data/", b""),
                    ]
                )
            )
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            manager = LaunchManager(settings, runner=runner)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Mixed ZIP",
                    "intakeBlocks": ["Open the mixed archive"],
                    "sources": [{"kind": "local_path", "path": str(local)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["prepared"])
            result = manager.execute(draft_id=prepared["draftId"], fingerprint=prepared["fingerprint"], confirmed=True)
            archive_path = Path(result["runRoot"]) / "inputs" / "data_room.zip"
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["data/events.jsonl", "data/table.csv", "data/workbook.xlsx", "docs/README.md", "docs/notes.txt", "docs/report.pdf"],
                )
                self.assertNotIn("mixed.zip", archive.namelist())
            context = RunContext(result["runId"], Path(result["runRoot"]), (archive_path.parent,))
            room = DataRoom.open(context, archive_path)
            self.assertEqual(
                [member.path for member in room.members()],
                ["data/events.jsonl", "data/table.csv", "data/workbook.xlsx", "docs/README.md", "docs/notes.txt", "docs/report.pdf"],
            )

    def test_zip_and_ordinary_sources_are_deterministic_and_collisions_are_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            ordinary = source / "plain.csv"
            archive_path = source / "nested.zip"
            source.mkdir()
            ordinary.write_bytes(b"id\n1\n")
            archive_path.write_bytes(_zip_payload([("nested/data.csv", b"id\n2\n")]))
            manager = LaunchManager(self.settings(root))
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Deterministic ZIP",
                    "intakeBlocks": ["r"],
                    "sources": [
                        {"kind": "local_path", "path": str(archive_path)},
                        {"kind": "local_path", "path": str(ordinary)},
                    ],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            draft = manager._load_draft(prepared["draftId"], prepared["fingerprint"])
            first = root / "first.zip"
            second = root / "second.zip"
            manager._package_zip(draft, first)
            manager._package_zip(draft, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as output:
                self.assertEqual(output.namelist(), ["nested/data.csv", "plain.csv"])

            collision = source / "collision.zip"
            collision.write_bytes(_zip_payload([("PLAIN.CSV", b"id\n3\n")]))
            rejected = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Collision",
                    "intakeBlocks": ["r"],
                    "sources": [
                        {"kind": "local_path", "path": str(ordinary)},
                        {"kind": "local_path", "path": str(collision)},
                    ],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertFalse(rejected["valid"])
            self.assertIn("sources[1]", rejected["errors"])
            self.assertIn("PLAIN.CSV", rejected["errors"]["sources[1]"])

    def test_zip_adversarial_members_report_concrete_errors(self) -> None:
        cases = [
            ("traversal", [("../evil.csv", b"x")], "evil.csv"),
            ("absolute", [("/evil.csv", b"x")], "evil.csv"),
            ("backslash", [("dir\\evil.csv", b"x")], "evil.csv"),
            ("drive", [("C:evil.csv", b"x")], "evil.csv"),
            ("nested", [("inner.zip", b"x")], "inner.zip"),
            ("unsupported", [("payload.bin", b"x")], "payload.bin"),
            ("duplicate", [("Thing.CSV", b"x"), ("thing.csv", b"y")], "thing.csv"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, entries, expected in cases:
                path = root / "source" / f"{label}.zip"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_zip_payload(entries))
                manager = LaunchManager(self.settings(root))
                result = manager.prepare(
                    {
                        "mode": "new",
                        "projectName": label,
                        "intakeBlocks": ["r"],
                        "sources": [{"kind": "local_path", "path": str(path)}],
                        "maxAgents": 1,
                        "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                    }
                )
                self.assertFalse(result["valid"], label)
                self.assertIn("sources[0]", result["errors"], label)
                self.assertIn(expected, result["errors"]["sources[0]"], label)

            symlink_path = root / "source" / "symlink.zip"
            symlink_output = io.BytesIO()
            with zipfile.ZipFile(symlink_output, "w") as archive:
                info = zipfile.ZipInfo("link.csv")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"target")
            symlink_path.write_bytes(symlink_output.getvalue())
            result = LaunchManager(self.settings(root)).prepare(
                {
                    "mode": "new",
                    "projectName": "symlink",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(symlink_path)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertIn("link.csv", result["errors"]["sources[0]"])

            encrypted_path = root / "source" / "encrypted.zip"
            encrypted_path.write_bytes(_mark_zip_encrypted(_zip_payload([("secret.csv", b"secret")], compression=zipfile.ZIP_STORED)))
            result = LaunchManager(self.settings(root)).prepare(
                {
                    "mode": "new",
                    "projectName": "encrypted",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(encrypted_path)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertIn("secret.csv", result["errors"]["sources[0]"])

            malformed = root / "source" / "malformed.zip"
            malformed.write_bytes(b"not a ZIP")
            result = LaunchManager(self.settings(root)).prepare(
                {
                    "mode": "new",
                    "projectName": "malformed",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(malformed)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertIn("invalid ZIP archive", result["errors"]["sources[0]"])

            corrupted = root / "source" / "corrupted.zip"
            corrupted.write_bytes(_zip_payload([("corrupt.csv", b"payload")], compression=zipfile.ZIP_STORED).replace(b"payload", b"payloae", 1))
            result = LaunchManager(self.settings(root)).prepare(
                {
                    "mode": "new",
                    "projectName": "corrupted",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(corrupted)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertIn("corrupt.csv", result["errors"]["sources[0]"])

    def test_zip_physical_metadata_and_compression_fail_closed_with_member_context(self) -> None:
        """Validate names and physical ZIP records before semantic filtering."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def prepare(label: str, payload: bytes):
                path = root / "source" / f"{label}.zip"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                return LaunchManager(self.settings(root)).prepare(
                    {
                        "mode": "new",
                        "projectName": label,
                        "intakeBlocks": ["r"],
                        "sources": [{"kind": "local_path", "path": str(path)}],
                        "maxAgents": 1,
                        "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                    }
                )

            raw_nul = prepare("raw-nul", _forge_raw_nul_filename(_zip_payload([("safe.csv", b"x")], compression=zipfile.ZIP_STORED)))
            self.assertFalse(raw_nul["valid"])
            self.assertIn("safe.csv", raw_nul["errors"]["sources[0]"])
            self.assertIn("raw filename", raw_nul["errors"]["sources[0]"])

            nonempty_directory = prepare(
                "nonempty-directory",
                _forge_directory_payload(_zip_payload([("folder/", b"")], compression=zipfile.ZIP_STORED)),
            )
            self.assertFalse(nonempty_directory["valid"])
            self.assertIn("folder/", nonempty_directory["errors"]["sources[0]"])
            self.assertIn("nonempty payload", nonempty_directory["errors"]["sources[0]"])

            metadata = _zip_payload([("__MACOSX/._metadata.csv", b"metadata")], compression=zipfile.ZIP_STORED)
            corrupted_metadata = metadata.replace(b"metadata", b"metadate", 1)
            metadata_result = prepare("corrupt-metadata", corrupted_metadata)
            self.assertFalse(metadata_result["valid"])
            self.assertIn("._metadata.csv", metadata_result["errors"]["sources[0]"])
            self.assertIn("CRC/content", metadata_result["errors"]["sources[0]"])

            unsupported_compression = prepare(
                "unsupported-compression",
                _forge_compression_method(_zip_payload([("unsupported.csv", b"x")], compression=zipfile.ZIP_STORED), 99),
            )
            self.assertFalse(unsupported_compression["valid"])
            self.assertIn("unsupported.csv", unsupported_compression["errors"]["sources[0]"])
            self.assertIn("compression method 99", unsupported_compression["errors"]["sources[0]"])

    def test_zip_preparse_rejects_oversized_eocd_claims_before_zipfile_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "preparse.zip"
            source.parent.mkdir(parents=True)
            valid = _zip_payload([("safe.csv", b"x")], compression=zipfile.ZIP_STORED)

            source.write_bytes(_forge_eocd_fields(valid, entries=MAX_ZIP_MEMBER_COUNT + 1))
            with patch("apps.control_center_operational.launch.zipfile.ZipFile", side_effect=AssertionError("ZipFile allocation was not bounded")):
                with self.assertRaisesRegex(ValueError, "physical entry limit"):
                    _inspect_zip_source(source, max_total_bytes=100, read_members=True)

            source.write_bytes(_forge_eocd_fields(valid, central_size=65))
            with patch("apps.control_center_operational.launch.MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 64):
                with patch("apps.control_center_operational.launch.zipfile.ZipFile", side_effect=AssertionError("ZipFile allocation was not bounded")):
                    with self.assertRaisesRegex(ValueError, "central directory exceeds"):
                        _inspect_zip_source(source, max_total_bytes=100, read_members=True)

    def test_zip_preparse_handles_zip64_and_rejects_malformed_multidisk_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "zip64.zip"
            source.parent.mkdir(parents=True)
            source.write_bytes(_zip64_one_member_payload())
            inspection = _inspect_zip_source(source, max_total_bytes=100, read_members=True)
            self.assertEqual(inspection.member_count, 1)
            self.assertEqual(inspection.members[0].name, "safe.csv")

            ordinary_sfx = root / "source" / "ordinary-sfx.zip"
            ordinary_sfx.write_bytes(b"SFX-PREFIX" + _zip_payload([("safe.csv", b"x")], compression=zipfile.ZIP_STORED))
            self.assertEqual(_inspect_zip_source(ordinary_sfx, max_total_bytes=100, read_members=True).member_count, 1)
            zip64_sfx = root / "source" / "zip64-sfx.zip"
            zip64_sfx.write_bytes(b"SFX-PREFIX" + _zip64_one_member_payload())
            self.assertEqual(_inspect_zip_source(zip64_sfx, max_total_bytes=100, read_members=True).member_count, 1)

            mismatch = bytearray(_zip64_one_member_payload())
            mismatch_eocd = mismatch.rfind(b"PK\x05\x06")
            mismatch_locator = mismatch_eocd - 20
            mismatch[mismatch_locator + 8 : mismatch_locator + 16] = (999).to_bytes(8, "little")
            mismatch_path = root / "source" / "zip64-mismatch.zip"
            mismatch_path.write_bytes(mismatch)
            with patch("apps.control_center_operational.launch.zipfile.ZipFile", side_effect=AssertionError("ZipFile allocation was not bounded")):
                with self.assertRaisesRegex(ValueError, "locator offset"):
                    _inspect_zip_source(mismatch_path, max_total_bytes=100, read_members=True)

            malformed = bytearray(source.read_bytes())
            eocd = malformed.rfind(b"PK\x05\x06")
            locator = eocd - 20
            malformed[locator + 16 : locator + 20] = (2).to_bytes(4, "little")
            source.write_bytes(malformed)
            with patch("apps.control_center_operational.launch.zipfile.ZipFile", side_effect=AssertionError("ZipFile allocation was not bounded")):
                with self.assertRaisesRegex(ValueError, "multi-disk"):
                    _inspect_zip_source(source, max_total_bytes=100, read_members=True)

            source.write_bytes(b"not a ZIP")
            with patch("apps.control_center_operational.launch.zipfile.ZipFile", side_effect=AssertionError("ZipFile allocation was not bounded")):
                with self.assertRaisesRegex(ValueError, "end-of-central-directory"):
                    _inspect_zip_source(source, max_total_bytes=100, read_members=True)

    def test_zip_preparse_rejects_dual_zip64_record_and_comment_before_zipfile(self) -> None:
        """A locator cannot redirect preflight to a second ZIP64 record.

        CPython reads the fixed ZIP64 record immediately before the locator and
        ignores the locator's relative offset.  Keep a second forged record at
        the SFX prefix and another copy in the EOCD comment, then point the
        locator at that forged prefix.  The explicit concat/locator policy must
        reject the ambiguity before ``zipfile.ZipFile`` allocates its central
        directory.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "dual-zip64.zip"
            source.parent.mkdir(parents=True)
            real = _zip64_one_member_payload()
            forged = struct.pack(
                "<4sQ2H2L4Q",
                b"PK\x06\x06",
                44,
                45,
                45,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            value = bytearray(forged + real)
            eocd = value.rfind(b"PK\x05\x06")
            comment = forged + b"reviewer-dual-zip64"
            value[eocd + 20 : eocd + 22] = len(comment).to_bytes(2, "little")
            value.extend(comment)
            locator = eocd - ZIP64_EOCD_LOCATOR_BYTES
            value[locator + 8 : locator + 16] = (0).to_bytes(8, "little")
            source.write_bytes(value)
            with patch(
                "apps.control_center_operational.launch.zipfile.ZipFile",
                side_effect=AssertionError("ZipFile allocation was not bounded"),
            ):
                with self.assertRaisesRegex(ValueError, "locator offset"):
                    _inspect_zip_source(source, max_total_bytes=100, read_members=True)

    def test_zip_snapshot_is_immutable_during_package_and_cleans_up(self) -> None:
        """Packaging reads one private snapshot, even if the source is replaced."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "source" / "mutable.zip"
            local.parent.mkdir(parents=True)
            local.write_bytes(_zip_payload([("data.csv", b"before\n")]))
            manager = LaunchManager(self.settings(root))
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Snapshot",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(local)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["prepared"])
            draft = manager._load_draft(prepared["draftId"], prepared["fingerprint"])
            destination = root / "snapshot.zip"
            original_inspect = __import__("apps.control_center_operational.launch", fromlist=["_inspect_zip_source"])._inspect_zip_source
            replaced = False

            def mutate_after_snapshot(path, **kwargs):
                nonlocal replaced
                if not replaced and Path(path).name.startswith(".zip-snapshot-"):
                    replaced = True
                    local.write_bytes(_zip_payload([("data.csv", b"after replacement\n")]))
                return original_inspect(path, **kwargs)

            with patch("apps.control_center_operational.launch._inspect_zip_source", side_effect=mutate_after_snapshot):
                manager._package_zip(draft, destination)
            self.assertTrue(replaced)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.read("data.csv"), b"before\n")
            self.assertEqual(list(root.glob(".zip-snapshot-*")), [])

            # Restore the prepared bytes so the next assertion reaches the
            # inspection failpoint rather than the expected top-level binding.
            local.write_bytes(_zip_payload([("data.csv", b"before\n")]))

            def fail_inspection(path, **kwargs):
                raise ValueError("forced snapshot inspection failure")

            failure_destination = root / "snapshot-failure.zip"
            with patch("apps.control_center_operational.launch._inspect_zip_source", side_effect=fail_inspection):
                with self.assertRaisesRegex(ValueError, "forced snapshot inspection failure"):
                    manager._package_zip(draft, failure_destination)
            self.assertEqual(list(root.glob(".zip-snapshot-*")), [])

    def test_zip_prepare_inventory_uses_one_snapshot_for_local_and_upload_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            manager = LaunchManager(settings)
            before = _zip_payload([("data.csv", b"before\n")])
            after = _zip_payload([("data.csv", b"after replacement\n")])
            module = __import__("apps.control_center_operational.launch", fromlist=["_inspect_zip_source"])
            original_inspect = module._inspect_zip_source

            def prepare_with_replacement(payload, replacement_path: Path):
                replaced = False

                def inspect_snapshot(path, **kwargs):
                    nonlocal replaced
                    if not replaced and Path(path).name.startswith(".zip-snapshot-"):
                        replaced = True
                        replacement_path.write_bytes(after)
                    return original_inspect(path, **kwargs)

                with patch("apps.control_center_operational.launch._inspect_zip_source", side_effect=inspect_snapshot):
                    prepared = manager.prepare(payload)
                self.assertTrue(replaced)
                self.assertTrue(prepared["prepared"])
                self.assertEqual(list(settings.state_root.glob(".zip-snapshot-*")), [])
                draft = manager._load_draft(prepared["draftId"], prepared["fingerprint"])
                self.assertEqual(draft["sources"][0]["size"], len(before))
                self.assertEqual(draft["sources"][0]["sha256"], hashlib.sha256(before).hexdigest())

            local = root / "source" / "prepare-local.zip"
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(before)
            prepare_with_replacement(
                {
                    "mode": "new",
                    "projectName": "prepare-local-snapshot",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(local)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                },
                local,
            )

            upload = manager.upload(
                io.BytesIO(before),
                filename="prepare-upload.zip",
                relative_path="prepare-upload.zip",
                content_length=len(before),
            )
            prepare_with_replacement(
                {
                    "mode": "new",
                    "projectName": "prepare-upload-snapshot",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "upload", "uploadId": upload.upload_id}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                },
                upload.path,
            )

    def test_zip_bounds_are_injected_without_large_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def prepare(label: str, payload: bytes, *, settings: LaunchSettings | None = None):
                path = root / "source" / f"{label}.zip"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                manager = LaunchManager(settings or self.settings(root))
                return manager.prepare(
                    {
                        "mode": "new",
                        "projectName": label,
                        "intakeBlocks": ["r"],
                        "sources": [{"kind": "local_path", "path": str(path)}],
                        "maxAgents": 1,
                        "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                    }
                )

            with patch("apps.control_center_operational.launch.MAX_ZIP_MEMBER_BYTES", 4):
                result = prepare("member-bound", _zip_payload([("large.csv", b"12345")]))
                self.assertIn("large.csv", result["errors"]["sources[0]"])
            with patch("apps.control_center_operational.launch.MAX_ZIP_COMPRESSION_RATIO", 1.0):
                result = prepare("ratio-bound", _zip_payload([("ratio.csv", b"a" * 100_000)]))
                self.assertIn("ratio.csv", result["errors"]["sources[0]"])
            with patch("apps.control_center_operational.launch.MAX_ZIP_MEMBER_COUNT", 2):
                result = prepare("count-bound", _zip_payload([(f"{index}.csv", b"x") for index in range(3)]))
                self.assertIn("physical entry limit", result["errors"]["sources[0]"])
            # Use a reduced source aggregate setting without allocating a
            # large expanded fixture.
            reduced = self.settings(root)
            reduced = LaunchSettings(
                runtime_root=reduced.runtime_root,
                runs_root=reduced.runs_root,
                source_roots=reduced.source_roots,
                max_source_total_bytes=4,
            )
            result = prepare("aggregate-bound-reduced", _zip_payload([("aggregate.csv", b"12345")]), settings=reduced)
            self.assertIn("aggregate.csv", result["errors"]["sources[0]"])

    def test_zip_physical_entry_and_ignored_metadata_bytes_honor_reduced_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def prepare(label: str, payload: bytes):
                path = root / "source" / f"{label}.zip"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                return LaunchManager(self.settings(root)).prepare(
                    {
                        "mode": "new",
                        "projectName": label,
                        "intakeBlocks": ["r"],
                        "sources": [{"kind": "local_path", "path": str(path)}],
                        "maxAgents": 1,
                        "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                    }
                )

            with patch("apps.control_center_operational.launch.MAX_ZIP_MEMBER_COUNT", 2):
                directory_flood = prepare(
                    "directory-flood",
                    _zip_payload([("one/", b""), ("two/", b""), ("three/", b"")]),
                )
                self.assertFalse(directory_flood["valid"])
                self.assertIn("physical entry limit", directory_flood["errors"]["sources[0]"])

                metadata_flood = prepare(
                    "metadata-flood",
                    _zip_payload(
                        [
                            ("__MACOSX/._one.csv", b"1"),
                            ("__MACOSX/._two.csv", b"2"),
                            ("__MACOSX/._three.csv", b"3"),
                        ]
                    ),
                )
                self.assertFalse(metadata_flood["valid"])
                self.assertIn("physical entry limit", metadata_flood["errors"]["sources[0]"])

                first_entries = root / "source" / "first-entries.zip"
                second_entries = root / "source" / "second-entries.zip"
                first_entries.write_bytes(
                    _zip_payload(
                        [("__MACOSX/._first-a.csv", b"1"), ("__MACOSX/._first-b.csv", b"2")]
                    )
                )
                second_entries.write_bytes(
                    _zip_payload(
                        [("__MACOSX/._second-a.csv", b"3"), ("__MACOSX/._second-b.csv", b"4")]
                    )
                )
                cross_entry = LaunchManager(self.settings(root)).prepare(
                    {
                        "mode": "new",
                        "projectName": "entry-cross-source",
                        "intakeBlocks": ["r"],
                        "sources": [
                            {"kind": "local_path", "path": str(first_entries)},
                            {"kind": "local_path", "path": str(second_entries)},
                        ],
                        "maxAgents": 1,
                        "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                    }
                )
                self.assertFalse(cross_entry["valid"])
                self.assertIn("sources[1]", cross_entry["errors"])
                self.assertIn("physical entry limit", cross_entry["errors"]["sources[1]"])

            with patch("apps.control_center_operational.launch.MAX_ZIP_TOTAL_BYTES", 4):
                metadata_bytes = prepare(
                    "metadata-bytes",
                    _zip_payload([("__MACOSX/._oversized.csv", b"12345")], compression=zipfile.ZIP_STORED),
                )
                self.assertFalse(metadata_bytes["valid"])
                self.assertIn("expanded bytes", metadata_bytes["errors"]["sources[0]"])
                self.assertIn("._oversized.csv", metadata_bytes["errors"]["sources[0]"])

                first = root / "source" / "first.zip"
                second = root / "source" / "second.zip"
                first.write_bytes(_zip_payload([("__MACOSX/._first.csv", b"123")], compression=zipfile.ZIP_STORED))
                second.write_bytes(_zip_payload([("__MACOSX/._second.csv", b"456")], compression=zipfile.ZIP_STORED))
                cross_source = LaunchManager(self.settings(root)).prepare(
                    {
                        "mode": "new",
                        "projectName": "metadata-cross-source",
                        "intakeBlocks": ["r"],
                        "sources": [
                            {"kind": "local_path", "path": str(first)},
                            {"kind": "local_path", "path": str(second)},
                        ],
                        "maxAgents": 1,
                        "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                    }
                )
                self.assertFalse(cross_source["valid"])
                self.assertIn("sources[1]", cross_source["errors"])
                self.assertIn("expanded bytes", cross_source["errors"]["sources[1]"])
                self.assertIn("._second.csv", cross_source["errors"]["sources[1]"])

    def test_local_zip_mutation_after_prepare_is_rejected_by_hash_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "source" / "mutable.zip"
            local.parent.mkdir(parents=True)
            local.write_bytes(_zip_payload([("data.csv", b"before\n")]))
            manager = LaunchManager(self.settings(root, enabled=True), runner=FakeRunner())
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Mutation",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(local)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["prepared"])
            local.write_bytes(_zip_payload([("data.csv", b"after mutation\n")]))
            with self.assertRaisesRegex(ValueError, "local source changed after prepare"):
                manager.execute(draft_id=prepared["draftId"], fingerprint=prepared["fingerprint"], confirmed=True)


if __name__ == "__main__":
    unittest.main()
