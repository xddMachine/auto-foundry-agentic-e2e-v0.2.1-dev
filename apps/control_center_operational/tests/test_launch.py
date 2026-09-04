from __future__ import annotations

import io
import json
import hashlib
import os
import signal
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from auto_foundry_core.workbench import DataRoom
from auto_foundry_core.workspace import RunContext
from auto_foundry_core.lifecycle import RunLifecycle
import auto_foundry_core.coordinator as coordinator_module
import apps.control_center_operational.launch as launch_module
from apps.control_center_operational.launch import (
    _BoundedRedirectHandler,
    _PinnedHTTPSConnection,
    _resolve_public_host,
    LaunchManager,
    CodexRequirementIntakePlanner,
    LaunchConflictError,
    LaunchSettings,
    LockedLaunchError,
    ZIP64_EOCD_LOCATOR_BYTES,
    default_codex_binary,
    _planner_plan_hash,
    SubprocessRunner,
    validate_remote_url,
    _inspect_zip_source,
    atomic_write_json,
)
from apps.control_center_operational.projection import OperationalRepository
from apps.control_center_operational.run_control import RunControlManager


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.intake_responses: list[dict[str, object]] = []

    def start(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "monitorRunId": "fake-monitor",
            "pid": 5252,
            "processGroupId": 5252,
            "processGroupToken": "fake-runner-token",
        }

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


class TimeoutRunner(FakeRunner):
    """Return a complete identity while retaining a timed-out live child."""

    def start(self, **kwargs):
        result = super().start(**kwargs)
        result.update({"ready": False, "startupTimedOut": True})
        return result


class RepairingPlanner:
    """Return one malformed representation, then a valid plan."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def plan_intake(self, **kwargs):
        self.calls.append(dict(kwargs))
        if len(self.calls) == 1:
            return {"schemaVersion": 1, "requirements": [], "groups": []}
        blocks = [str(item["text"]) for item in kwargs["intake_blocks"]]
        return fake_intake_response(blocks)


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
        # Use the actual paired skill. Real subprocess tests must not depend
        # on sitecustomize replacing a production integrity hash in the child.
        source_skill = Path(__file__).resolve().parents[3] / "skills" / coordinator_module.PRODUCTION_SKILL_NAME
        shutil.copytree(source_skill, skill_path, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
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

    def test_publication_policy_is_exact_boolean_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LaunchManager(self.settings(Path(directory), enabled=False))
            self.assertEqual(manager._canonical_publication_policy({"enabled": False}), {"enabled": False})
            self.assertEqual(manager._canonical_publication_policy({"enabled": True}), {"enabled": True})
            for invalid in ({}, {"enabled": 1}, {"enabled": None}, {"enabled": False, "channel": "local"}):
                with self.assertRaises(LaunchConflictError):
                    manager._canonical_publication_policy(invalid)

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
            self.assertNotIn("originalText", kwargs["input"])
            self.assertNotIn("content_hash", kwargs["input"])

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

    def test_prepare_is_idempotent_across_manager_reload_and_can_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            payload = {
                "mode": "new",
                "projectName": "Durable preparation",
                "intakeBlocks": ["same request"],
                "sources": [],
                "maxAgents": 1,
                "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                "idempotencyKey": "browser-tab-preparation-1",
            }
            first = LaunchManager(settings).prepare(payload)
            second_manager = LaunchManager(settings)
            second = second_manager.prepare(dict(payload))
            self.assertEqual(second["draftId"], first["draftId"])
            self.assertEqual(second["fingerprint"], first["fingerprint"])
            self.assertTrue(second["reused"])
            self.assertTrue(second_manager.status(first["draftId"])["cancelable"])
            cancelled = second_manager.cancel(
                draft_id=first["draftId"],
                fingerprint=first["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertFalse(cancelled["cancelable"])

    def test_production_role_bindings_require_every_canonical_dispatch_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LaunchManager(self.settings(root))
            api = manager._core_imports()
            routes = api["production_role_routing"]()
            routes.pop("analytical_owner")
            api["production_role_routing"] = lambda: routes
            with self.assertRaisesRegex(LaunchConflictError, "analytical_owner"):
                manager._production_role_bindings(api)

    def test_intake_representation_repair_is_one_validator_informed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            planner = RepairingPlanner()
            manager = LaunchManager(settings, runner=FakeRunner(), intake_planner=planner)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "One repair",
                    "intakeBlocks": ["One exact requirement"],
                    "sources": [],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            result = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(planner.calls), 2)
            self.assertEqual(planner.calls[1]["repair_context"]["kind"], "representation_repair")
            self.assertEqual(planner.calls[1]["response_schema"]["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_planner_original_text_echo_is_ignored_and_host_derives_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            calls: list[dict[str, object]] = []
            source_text = "Revenue margin\nby region"

            def planner(**kwargs):
                calls.append(dict(kwargs))
                return fake_intake_response(
                    [source_text],
                    candidates=[{"originalText": "Planner paraphrase of the requirement"}],
                )

            manager = LaunchManager(settings, runner=FakeRunner(), intake_planner=planner)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Canonical requirement text",
                    "intakeBlocks": [source_text],
                    "sources": [],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            result = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(calls), 1)
            plan = json.loads((Path(result["runRoot"]) / "requirement_supervisor_plan.json").read_text())
            self.assertEqual(plan["input_records"][0]["original_text"], source_text)

    def test_intake_aliases_and_block_context_text_hash_are_host_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            calls: list[dict[str, object]] = []
            source_text = "Revenue margin\nby region"

            def planner(**kwargs):
                calls.append(dict(kwargs))
                return {
                    "schema_version": 1,
                    "portfolio_strategy": "canonicalize safe wire aliases",
                    "requirements": {
                        "candidate_id": "C-001",
                        "source_spans": {"block_id": "INPUT-001", "start": 0, "end": len(source_text)},
                        "source_bindings": {
                            "ref": "INPUT-001",
                            "span": {"block_id": "INPUT-001", "start": 0, "end": len(source_text)},
                            "text": "wrong planner echo",
                            "content_hash": "0" * 64,
                        },
                        "original_text": "another planner paraphrase",
                        "business_objective": "Assess margin by region.",
                    },
                    "groups": {
                        "members": "C-001",
                        "rationale": "One independent decision.",
                    },
                    "product_brief": {
                        "audience": {
                            "value": "wrong context echo",
                            "source_bindings": {
                                "ref": "INPUT-001",
                                "span": {"block_id": "INPUT-001", "start": 0, "end": len(source_text)},
                                "text": "wrong context echo",
                                "content_hash": "f" * 64,
                            },
                        }
                    },
                }

            manager = LaunchManager(settings, runner=FakeRunner(), intake_planner=planner)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Alias normalization",
                    "intakeBlocks": [source_text],
                    "sources": [],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            result = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(calls), 1)
            run_root = Path(result["runRoot"])
            plan = json.loads((run_root / "requirement_supervisor_plan.json").read_text())
            self.assertEqual(plan["input_records"][0]["original_text"], source_text)
            binding = plan["input_records"][0]["metadata"]["source_bindings"][0]
            self.assertEqual(binding["text"], source_text)
            self.assertEqual(binding["content_hash"], hashlib.sha256(source_text.encode()).hexdigest())
            context_artifact = run_root / "control_center" / "launches" / prepared["draftId"] / "mission_context.json"
            context = json.loads(context_artifact.read_text())
            audience = context["context"]["product_brief"]["audience"][0]
            self.assertEqual(audience["text"], source_text)
            self.assertEqual(audience["source_bindings"][0]["text"], source_text)
            self.assertEqual(audience["source_bindings"][0]["content_hash"], hashlib.sha256(source_text.encode()).hexdigest())

    def test_document_binding_aliases_ignore_planner_text_and_hash_echoes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            upload_bytes = b"Investigate margin and inventory as separate decisions.\n"
            upload = LaunchManager(settings).upload(
                io.BytesIO(upload_bytes),
                filename="brief.md",
                relative_path="brief.md",
                content_length=len(upload_bytes),
            )
            calls: list[dict[str, object]] = []
            canonical_text = "Investigate margin and inventory as separate decisions."

            def planner(**kwargs):
                calls.append(dict(kwargs))
                return {
                    "schema_version": 1,
                    "portfolio_strategy": "document source aliases",
                    "requirements": {
                        "candidate_id": "C-001",
                        "source_spans": [],
                        "document_refs": "brief.md",
                        "source_bindings": {
                            "ref": "brief.md",
                            "location": {"section": 1, "paragraph": 1},
                            "contentHash": "0" * 64,
                            "text": "Planner paraphrase",
                        },
                        "original_text": "Planner paraphrase",
                        "business_objective": "Determine the required margin and inventory analyses.",
                    },
                    "groups": {
                        "members": "C-001",
                        "rationale": "Document-grounded decision requirement.",
                    },
                }

            manager = LaunchManager(settings, runner=FakeRunner(), intake_planner=planner)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Document aliases",
                    "intakeBlocks": [],
                    "sources": [{"kind": "upload", "uploadId": upload.upload_id}],
                    "maxAgents": 2,
                    "capacity": {"total": 2, "entityResolution": 1, "analyticalOwner": 1, "specialist": 0},
                }
            )
            result = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(len(calls), 1)
            plan = json.loads((Path(result["runRoot"]) / "requirement_supervisor_plan.json").read_text())
            self.assertEqual(plan["input_records"][0]["original_text"], canonical_text)
            binding = plan["input_records"][0]["metadata"]["source_bindings"][0]
            self.assertEqual(binding["text"], canonical_text)
            self.assertEqual(binding["content_hash"], hashlib.sha256(canonical_text.encode()).hexdigest())

    def test_intake_semantic_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            calls: list[dict[str, object]] = []

            def semantic_failure(**kwargs):
                calls.append(dict(kwargs))
                return fake_intake_response(
                    ["One exact requirement"],
                    groups=[["not-a-requirement"]],
                )

            manager = LaunchManager(settings, runner=FakeRunner(), intake_planner=semantic_failure)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "No semantic rerun",
                    "intakeBlocks": ["One exact requirement"],
                    "sources": [],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            with self.assertRaisesRegex(LaunchConflictError, "unknown requirement"):
                manager.execute(
                    draft_id=prepared["draftId"],
                    fingerprint=prepared["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(len(calls), 1)

    def test_intake_trusted_source_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            calls: list[dict[str, object]] = []

            def source_failure(**kwargs):
                calls.append(dict(kwargs))
                return fake_intake_response(
                    ["One exact requirement"],
                    candidates=[{"documentRefs": ["missing.md"]}],
                )

            manager = LaunchManager(settings, runner=FakeRunner(), intake_planner=source_failure)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "No trusted source rerun",
                    "intakeBlocks": ["One exact requirement"],
                    "sources": [],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            with self.assertRaisesRegex(LaunchConflictError, "unavailable document"):
                manager.execute(
                    draft_id=prepared["draftId"],
                    fingerprint=prepared["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(len(calls), 1)

    def test_upload_rejects_traversal_and_hashes_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LaunchManager(self.settings(Path(directory)))
            with self.assertRaises(Exception):
                manager.upload(io.BytesIO(b"x"), filename="x.csv", relative_path="../x.csv", content_length=1)
            record = manager.upload(io.BytesIO(b"a,b\n1,2\n"), filename="x.csv", relative_path="folder/x.csv", content_length=8)
            self.assertEqual(record.size, 8)
            self.assertEqual(len(record.sha256), 64)
            self.assertEqual(record.path.read_bytes(), b"a,b\n1,2\n")

    def test_local_upload_and_sqlite_sources_are_admitted_without_extension_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LaunchManager(self.settings(root))
            database = root / "source" / "sample.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
                connection.execute("INSERT INTO orders VALUES (1, 12.5)")
                connection.commit()
            extensionless = root / "source" / "README"
            extensionless.write_text("opaque local payload\n", encoding="utf-8")
            uploaded = manager.upload(
                io.BytesIO(b"opaque upload bytes"),
                filename="payload.bin",
                relative_path="payload.bin",
                content_length=len(b"opaque upload bytes"),
            )
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Universal local sources",
                    "intakeBlocks": ["r"],
                    "sources": [
                        {"kind": "local_path", "path": str(database)},
                        {"kind": "local_path", "path": str(extensionless)},
                        {"kind": "upload", "uploadId": uploaded.upload_id},
                    ],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["valid"], prepared.get("errors"))
            draft = manager._load_draft(prepared["draftId"], prepared["fingerprint"])
            destination = root / "package.zip"
            manager._package_zip(draft, destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.namelist(), ["payload.bin", "README", "sample.sqlite3"])
                self.assertIn(b"SQLite format 3", archive.read("sample.sqlite3")[:32])

    def test_planner_document_catalog_does_not_readmit_large_structured_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "RUN-MIXED"
            inputs = run_root / "inputs"
            inputs.mkdir(parents=True)
            archive_path = inputs / "data_room.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("erp_transactions.parquet", b"p" * (17 * 1024 * 1024))
                archive.writestr("requirements.md", b"Analyze the admitted data room.")

            projection, catalog = LaunchManager._document_catalog_for_planner(
                run_root,
                "inputs/data_room.zip",
                allowed_roots=(run_root,),
            )

            self.assertIsNotNone(catalog)
            assert catalog is not None
            self.assertEqual([document.document_ref for document in catalog.documents], ["requirements.md"])
            self.assertEqual([document["document_ref"] for document in projection["documents"]], ["requirements.md"])

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

    def test_initial_status_write_failure_cleans_token_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            manager = LaunchManager(settings, runner=runner)
            prepared = manager.prepare(self.payload(manager, root))
            original_atomic_write = launch_module.atomic_write_json

            def fail_accepted_status(path, value):
                if value.get("status") == "accepted":
                    raise OSError("test accepted status write failure")
                return original_atomic_write(path, value)

            with patch("apps.control_center_operational.launch.atomic_write_json", side_effect=fail_accepted_status):
                with patch(
                    "apps.control_center_operational.launch._terminate_token_owned_process_group",
                    return_value=True,
                ) as terminate:
                    with self.assertRaises(OSError):
                        manager.execute(
                            draft_id=prepared["draftId"],
                            fingerprint=prepared["fingerprint"],
                            confirmed=True,
                        )
            terminate.assert_called_once_with(5252, "fake-runner-token")
            self.assertEqual(manager.status(prepared["draftId"])["status"], "failed")
            self.assertNotIn("processGroupToken", manager.status(prepared["draftId"]))

    def test_continuation_status_write_failure_cleans_token_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, runner=runner)
            initial = manager.prepare(self.payload(manager, root))
            created = manager.execute(
                draft_id=initial["draftId"],
                fingerprint=initial["fingerprint"],
                confirmed=True,
            )
            run_id = repository.list_runs()[0]["id"]
            continuation = manager.prepare(
                {
                    "mode": "continue",
                    "runId": run_id,
                    "intakeBlocks": ["Continuation status failure"],
                    "sources": [],
                    "maxAgents": 64,
                    "capacity": {"total": 64, "entityResolution": 32, "analyticalOwner": 8, "specialist": 24},
                }
            )
            original_atomic_write = launch_module.atomic_write_json

            def fail_accepted_status(path, value):
                if value.get("status") == "accepted":
                    raise OSError("test continuation status write failure")
                return original_atomic_write(path, value)

            with patch("apps.control_center_operational.launch.atomic_write_json", side_effect=fail_accepted_status):
                with patch(
                    "apps.control_center_operational.launch._terminate_token_owned_process_group",
                    return_value=True,
                ) as terminate:
                    with self.assertRaises(OSError):
                        manager.execute(
                            draft_id=continuation["draftId"],
                            fingerprint=continuation["fingerprint"],
                            confirmed=True,
                        )
            terminate.assert_called_once_with(5252, "fake-runner-token")
            self.assertEqual(manager.status(continuation["draftId"])["status"], "failed")
            self.assertEqual(created["status"], "accepted")

    def test_continuation_cleanup_unconfirmed_is_idempotent_while_identity_is_owned(self) -> None:
        """An in-flight continuation must not spawn a duplicate Supervisor."""

        class ContinuationFailureRunner(FakeRunner):
            def start(self, **kwargs):
                started = super().start(**kwargs)
                if len(self.calls) == 2:
                    started.update(
                        {
                            "startupToken": "startup-token-5252",
                            "processGroupToken": "group-token-5252",
                        }
                    )
                return started

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            runner = ContinuationFailureRunner()
            repository = OperationalRepository(None, [settings.runs_root], launch_state_root=settings.state_root)
            manager = LaunchManager(settings, repository=repository, runner=runner)
            initial = manager.prepare(self.payload(manager, root))
            manager.execute(
                draft_id=initial["draftId"],
                fingerprint=initial["fingerprint"],
                confirmed=True,
            )
            run_id = repository.list_runs()[0]["id"]
            continuation = manager.prepare(
                {
                    "mode": "continue",
                    "runId": run_id,
                    "intakeBlocks": ["Continuation cleanup failure"],
                    "sources": [],
                    "maxAgents": 64,
                    "capacity": {"total": 64, "entityResolution": 32, "analyticalOwner": 8, "specialist": 24},
                }
            )
            original_atomic_write = launch_module.atomic_write_json

            def fail_accepted_status(path, value):
                if value.get("status") == "accepted":
                    raise OSError("test continuation final status failure")
                return original_atomic_write(path, value)

            with patch("apps.control_center_operational.launch.atomic_write_json", side_effect=fail_accepted_status), patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=False,
            ) as terminate:
                with self.assertRaisesRegex(LaunchConflictError, "cleanup failed after launch error"):
                    manager.execute(
                        draft_id=continuation["draftId"],
                        fingerprint=continuation["fingerprint"],
                        confirmed=True,
                    )
            terminate.assert_called_once_with(5252, "group-token-5252")

            private_status = json.loads(manager._status_path(continuation["draftId"]).read_text(encoding="utf-8"))
            self.assertEqual(private_status["status"], "starting")
            self.assertEqual(private_status["processGroupId"], 5252)
            self.assertEqual(private_status["processGroupToken"], "group-token-5252")
            self.assertEqual(private_status["startupToken"], "startup-token-5252")

            # The process-group is still live/unknown, so both same-process
            # and fresh-manager continuation retries return the recoverable
            # status without invoking runner.start again.
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=True,
            ):
                observed = manager.status(continuation["draftId"])
                self.assertEqual(observed["status"], "starting")
                self.assertTrue(observed["recoverable"])
                self.assertNotIn("processGroupToken", observed)
                self.assertNotIn("startupToken", observed)
                retried = manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
                reloaded = LaunchManager(settings, repository=repository, runner=runner)
                reloaded_result = reloaded.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(retried["status"], "starting")
            self.assertEqual(reloaded_result["status"], "starting")
            self.assertEqual(len(runner.calls), 2)

    def test_same_continuation_execute_instances_spawn_exactly_once(self) -> None:
        """The per-run flock serializes concurrent continuation admission."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            runner = FakeRunner()
            repository = OperationalRepository(None, [settings.runs_root], launch_state_root=settings.state_root)
            initial_manager = LaunchManager(settings, repository=repository, runner=runner)
            initial = initial_manager.prepare(self.payload(initial_manager, root))
            initial_manager.execute(
                draft_id=initial["draftId"],
                fingerprint=initial["fingerprint"],
                confirmed=True,
            )
            run_id = repository.list_runs()[0]["id"]
            continuation = initial_manager.prepare(
                {
                    "mode": "continue",
                    "runId": run_id,
                    "intakeBlocks": ["Concurrent continuation"],
                    "sources": [],
                    "maxAgents": 64,
                    "capacity": {"total": 64, "entityResolution": 32, "analyticalOwner": 8, "specialist": 24},
                }
            )
            managers = [
                LaunchManager(settings, repository=repository, runner=runner),
                LaunchManager(settings, repository=repository, runner=runner),
            ]
            barrier = threading.Barrier(len(managers))
            results: list[dict[str, Any]] = []
            errors: list[BaseException] = []

            def invoke(manager: LaunchManager) -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        manager.execute(
                            draft_id=continuation["draftId"],
                            fingerprint=continuation["fingerprint"],
                            confirmed=True,
                        )
                    )
                except BaseException as exc:  # pragma: no cover - assertion below reports any race failure
                    errors.append(exc)

            threads = [threading.Thread(target=invoke, args=(manager,)) for manager in managers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "concurrent continuation admission deadlocked")
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result["status"] == "accepted" for result in results))
            self.assertEqual(len(runner.calls), 2)  # initial launch + exactly one continuation start
            private_status = json.loads(initial_manager._status_path(continuation["draftId"]).read_text(encoding="utf-8"))
            self.assertEqual(private_status["status"], "accepted")
            self.assertEqual(private_status["processGroupId"], 5252)
            self.assertTrue(private_status["processGroupToken"])

    def test_distinct_continuation_drafts_share_one_run_owned_supervisor(self) -> None:
        """A newer draft cannot hide an older token-owned child."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, settings, repository, created, run_id = self._initial_continue_context(root)
            authoritative_run_id = created["runId"]
            manager = LaunchManager(settings, repository=repository, runner=runner)
            first = manager.prepare(
                {
                    "mode": "continue",
                    "runId": run_id,
                    "intakeBlocks": ["First distinct continuation"],
                    "sources": [],
                    "maxAgents": 4,
                    "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                }
            )
            manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)

            # Prepare a second draft after the first generation is durable so
            # its parent lineage is compatible.  The queued record is newer
            # but intentionally has no identity; it must not hide the first
            # draft's live process-group ownership.
            second = manager.prepare(
                {
                    "mode": "continue",
                    "runId": run_id,
                    "intakeBlocks": ["Second distinct continuation"],
                    "sources": [],
                    "maxAgents": 4,
                    "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                }
            )
            newer = settings.state_root / "statuses" / "D-newer-queued.json"
            newer.write_text(
                json.dumps(
                    {
                        "draftId": "D-newer-queued",
                        "runId": authoritative_run_id,
                        "runRoot": str(Path(manager.status(first["draftId"])["runRoot"])),
                        "status": "queued",
                        "startedAt": "9999-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            newer_gone = settings.state_root / "statuses" / "D-newer-gone.json"
            newer_gone.write_text(
                json.dumps(
                    {
                        "draftId": "D-newer-gone",
                        "runId": authoritative_run_id,
                        "runRoot": str(Path(manager.status(first["draftId"])["runRoot"])),
                        "status": "accepted",
                        "startedAt": "9998-01-01T00:00:00Z",
                        "pid": 6262,
                        "processGroupId": 6262,
                        "processGroupToken": "gone-token-6262",
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                side_effect=lambda _process_group_id, process_group_token: process_group_token == "fake-runner-token",
            ):
                attached = manager.execute(
                    draft_id=second["draftId"],
                    fingerprint=second["fingerprint"],
                    confirmed=True,
                )
            self.assertIn(attached["status"], {"accepted", "running", "starting", "queued"})
            self.assertEqual(len(runner.calls), 2)  # initial launch + first continuation only
            private = json.loads(manager._status_path(second["draftId"]).read_text(encoding="utf-8"))
            self.assertEqual(private["processGroupId"], 5252)
            self.assertEqual(private["processGroupToken"], "fake-runner-token")
            self.assertNotIn("processGroupToken", attached)
            self.assertNotIn("startupToken", attached)

            class OrphanController:
                def find(self, _run_id, _run_root):
                    return None

                def group_alive(self, process_group_id, process_group_token=None):
                    return (process_group_id, process_group_token) == (5252, "fake-runner-token")

            run_root = Path(private["runRoot"])
            RunLifecycle.load(RunContext(run_id=created["runId"], run_root=run_root)).pause("test distinct draft ownership")
            control = RunControlManager(manager, process_controller=OrphanController())
            observed = control.status(repository.list_runs()[0]["id"])
            self.assertTrue(observed["coordinatorOrphaned"])
            self.assertFalse(observed["canResume"])

            # Once every distinct identity is positively gone, the run is
            # quiescent again; duplicate status copies do not change that.
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=False,
            ):
                self.assertIsNone(
                    manager._run_owned_supervisor_status(
                        authoritative_run_id,
                        run_root,
                    )
                )
            class GoneController:
                def find(self, _run_id, _run_root):
                    return None

                def group_alive(self, _process_group_id, _process_group_token=None):
                    return False

            quiescent_control = RunControlManager(manager, process_controller=GoneController())
            quiescent = quiescent_control.status(repository.list_runs()[0]["id"])
            self.assertFalse(quiescent["coordinatorOrphaned"])
            self.assertTrue(quiescent["canResume"])

    def test_run_admission_lock_identity_is_distinct_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            first = launch_module._run_admission_lock_path(settings, "RUN-A", root / "runs" / "RUN-A")
            second = launch_module._run_admission_lock_path(settings, "RUN-B", root / "runs" / "RUN-B")
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, second.parent)
            self.assertTrue(first.parent.is_dir())

    def test_starting_owner_alias_is_queued_and_not_cancellable(self) -> None:
        """A continuation alias cannot cancel the shared starting owner."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, settings, repository, _created, run_id = self._initial_continue_context(root)
            manager = LaunchManager(settings, repository=repository, runner=runner)
            first = manager.prepare(
                {
                    "mode": "continue",
                    "runId": run_id,
                    "intakeBlocks": ["Starting owner continuation"],
                    "sources": [],
                    "maxAgents": 4,
                    "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                }
            )
            manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
            owner_path = manager._status_path(first["draftId"])
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            owner.update(
                {
                    "status": "starting",
                    "startupTimedOut": True,
                    "startupToken": "startup-token-5252",
                }
            )
            atomic_write_json(owner_path, owner)

            second = manager.prepare(
                {
                    "mode": "continue",
                    "runId": run_id,
                    "intakeBlocks": ["Starting owner alias"],
                    "sources": [],
                    "maxAgents": 4,
                    "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                }
            )
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=True,
            ):
                alias = manager.execute(
                    draft_id=second["draftId"],
                    fingerprint=second["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(alias["status"], "queued")
            self.assertNotIn("processGroupToken", alias)
            self.assertNotIn("startupToken", alias)
            private_alias = json.loads(manager._status_path(second["draftId"]).read_text(encoding="utf-8"))
            self.assertEqual(private_alias["status"], "queued")
            self.assertEqual(private_alias["processGroupToken"], "fake-runner-token")
            self.assertEqual(private_alias["startupToken"], "startup-token-5252")

            with patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=True,
            ) as terminate:
                with self.assertRaisesRegex(LaunchConflictError, "Only an unstarted or failed launch preparation"):
                    manager.cancel(
                        draft_id=second["draftId"],
                        fingerprint=second["fingerprint"],
                        confirmed=True,
                    )
            terminate.assert_not_called()

            # Once the shared group is explicitly gone, the queued alias may
            # retry its durable continuation and start a replacement.
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=False,
            ):
                retry = manager.execute(
                    draft_id=second["draftId"],
                    fingerprint=second["fingerprint"],
                    confirmed=True,
                )
            self.assertIn(retry["status"], {"accepted", "running", "starting"})
            self.assertEqual(len(runner.calls), 3)

    def test_cancel_wins_preidentity_race_without_launch_overwrite(self) -> None:
        """A cancellation serialized first cannot be overwritten by execute."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            runner = FakeRunner()
            prepared_manager = LaunchManager(settings, runner=runner)
            prepared = prepared_manager.prepare(self.payload(prepared_manager, root))
            execute_manager = LaunchManager(settings, runner=runner)
            cancel_manager = LaunchManager(settings, runner=runner)
            barrier = threading.Barrier(2)
            execute_load_entered = threading.Event()
            release_execute_load = threading.Event()
            cancel_done = threading.Event()
            original_load = execute_manager._load_draft
            load_calls = 0

            def delayed_load(*args, **kwargs):
                nonlocal load_calls
                load_calls += 1
                if load_calls == 1:
                    execute_load_entered.set()
                    if not release_execute_load.wait(timeout=5):
                        raise RuntimeError("test execute admission barrier timed out")
                return original_load(*args, **kwargs)

            execute_manager._load_draft = delayed_load
            results: dict[str, dict[str, Any]] = {}
            errors: list[BaseException] = []

            def execute_worker() -> None:
                try:
                    barrier.wait(timeout=5)
                    results["execute"] = execute_manager.execute(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )
                except BaseException as exc:  # pragma: no cover - assertion below reports any race failure
                    errors.append(exc)

            def cancel_worker() -> None:
                try:
                    barrier.wait(timeout=5)
                    results["cancel"] = cancel_manager.cancel(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )
                except BaseException as exc:  # pragma: no cover - assertion below reports any race failure
                    errors.append(exc)
                finally:
                    cancel_done.set()

            execute_thread = threading.Thread(target=execute_worker)
            cancel_thread = threading.Thread(target=cancel_worker)
            execute_thread.start()
            cancel_thread.start()
            self.assertTrue(execute_load_entered.wait(timeout=5), "execute did not reach the preidentity barrier")
            self.assertTrue(cancel_done.wait(timeout=5), "cancel did not complete while execute was preidentity")
            release_execute_load.set()
            execute_thread.join(timeout=10)
            cancel_thread.join(timeout=10)
            self.assertFalse(execute_thread.is_alive(), "cancel/execute admission deadlocked")
            self.assertFalse(cancel_thread.is_alive(), "cancel/execute admission deadlocked")
            self.assertEqual(errors, [])
            self.assertEqual(results["cancel"]["status"], "cancelled")
            self.assertEqual(results["execute"]["status"], "cancelled")
            self.assertEqual(runner.calls, [])
            self.assertEqual(prepared_manager.status(prepared["draftId"])["status"], "cancelled")

            # Cancellation remains idempotent after the serialized loser has
            # observed the terminal cancelled record.
            again = cancel_manager.cancel(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(again["status"], "cancelled")

    def test_invalid_returned_process_identity_cleans_token_owned_group(self) -> None:
        class InvalidIdentityRunner(FakeRunner):
            def start(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "monitorRunId": "invalid-identity",
                    "pid": 5251,
                    "processGroupId": 5252,
                    "processGroupToken": "fake-runner-token",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = InvalidIdentityRunner()
            settings = self.settings(root, enabled=True)
            manager = LaunchManager(settings, runner=runner)
            prepared = manager.prepare(self.payload(manager, root))
            with patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=True,
            ) as terminate:
                with self.assertRaisesRegex(LaunchConflictError, "complete process identity"):
                    manager.execute(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )
            terminate.assert_called_once_with(5252, "fake-runner-token")
            self.assertEqual(manager.status(prepared["draftId"])["status"], "failed")

    def test_post_spawn_readiness_identity_mismatch_cleans_exact_group(self) -> None:
        """A readiness exception cannot strand the child before manager ownership."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            with patch(
                "apps.control_center_operational.launch.subprocess.Popen",
                return_value=RecordingProcess(),
            ) as popen, patch.object(
                SubprocessRunner,
                "_wait_for_ready",
                side_effect=LaunchConflictError("Foundry Supervisor readiness identity does not match this launch"),
            ), patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=True,
            ) as terminate:
                runner = SubprocessRunner("codex")
                manager = LaunchManager(
                    settings,
                    runner=runner,
                    intake_planner=FakeRunner(),
                )
                prepared = manager.prepare(self.payload(manager, root))
                with self.assertRaisesRegex(LaunchConflictError, "readiness identity"):
                    manager.execute(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )

                process_group_token = popen.call_args.kwargs["env"][
                    "AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN"
                ]
                terminate.assert_called_once_with(91234, process_group_token)
                status = manager.status(prepared["draftId"])
                self.assertEqual(status["status"], "failed")
                self.assertTrue(status["recoverable"])

    def test_post_spawn_identity_extraction_failure_cleans_exact_group(self) -> None:
        """A failure before readiness still cleans the exact spawned group."""

        class IdentityExtractionFailureProcess:
            pid = 91234

            @property
            def pgid(self):
                raise RuntimeError("identity extraction failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            with patch(
                "apps.control_center_operational.launch.subprocess.Popen",
                return_value=IdentityExtractionFailureProcess(),
            ) as popen, patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=True,
            ) as terminate:
                runner = SubprocessRunner("codex")
                manager = LaunchManager(
                    settings,
                    runner=runner,
                    intake_planner=FakeRunner(),
                )
                prepared = manager.prepare(self.payload(manager, root))
                with self.assertRaisesRegex(RuntimeError, "identity extraction failed"):
                    manager.execute(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )

                process_group_token = popen.call_args.kwargs["env"][
                    "AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN"
                ]
                terminate.assert_called_once_with(91234, process_group_token)
                status = manager.status(prepared["draftId"])
                self.assertEqual(status["status"], "failed")
                self.assertTrue(status["recoverable"])

    def test_post_return_promotion_failure_retains_identity_when_cleanup_unconfirmed(self) -> None:
        """Post-start admission errors cannot strand an untracked Supervisor."""

        class PostReturnRunner(FakeRunner):
            def start(self, **kwargs):
                started = super().start(**kwargs)
                started.update(
                    {
                        "startupToken": "startup-token-5252",
                        "processGroupToken": "group-token-5252",
                        "ready": False,
                        "startupTimedOut": True,
                    }
                )
                return started

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            runner = PostReturnRunner()
            repository = OperationalRepository(None, [settings.runs_root], launch_state_root=settings.state_root)
            manager = LaunchManager(settings, repository=repository, runner=runner)
            prepared = manager.prepare(self.payload(manager, root))
            with patch.object(
                manager,
                "_promote_staged_mission_artifacts",
                side_effect=RuntimeError("test promotion failure after Supervisor start"),
            ), patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=False,
            ) as terminate:
                with self.assertRaisesRegex(LaunchConflictError, "cleanup failed after launch error"):
                    manager.execute(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )
            terminate.assert_called_once_with(5252, "group-token-5252")

            private_path = manager._status_path(prepared["draftId"])
            private_status = json.loads(private_path.read_text(encoding="utf-8"))
            self.assertEqual(private_status["status"], "starting")
            self.assertEqual(private_status["pid"], 5252)
            self.assertEqual(private_status["processGroupId"], 5252)
            self.assertEqual(private_status["processGroupToken"], "group-token-5252")
            self.assertEqual(private_status["startupToken"], "startup-token-5252")

            class OrphanController:
                def find(self, _run_id, _run_root):
                    return None

                def group_alive(self, process_group_id, process_group_token=None):
                    return (process_group_id, process_group_token) == (5252, "group-token-5252")

            # Run-control reload sees the same durable ownership and refuses
            # a second Supervisor start while the group remains live.
            run_id = private_status["runId"]
            run_root = Path(private_status["runRoot"])
            RunLifecycle.load(RunContext(run_id=run_id, run_root=run_root)).pause("test launch promotion failure")
            control = RunControlManager(manager, process_controller=OrphanController())
            browser_run_id = repository.list_runs()[0]["id"]
            orphaned = control.status(browser_run_id)
            self.assertTrue(orphaned["coordinatorOrphaned"])
            self.assertFalse(orphaned["canResume"])
            with self.assertRaisesRegex(Exception, "process-group members are still active"):
                control.resume(browser_run_id, confirmed=True)
            self.assertEqual(len(runner.calls), 1)

            # A fresh manager can prove the live token-owned group without
            # exposing either private token, and a retry cannot spawn a
            # duplicate Supervisor while ownership remains unresolved.
            reloaded = LaunchManager(settings, runner=runner)
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=True,
            ):
                observed = reloaded.status(prepared["draftId"])
                self.assertEqual(observed["status"], "starting")
                self.assertTrue(observed["recoverable"])
                self.assertNotIn("processGroupToken", observed)
                self.assertNotIn("startupToken", observed)
                retried = reloaded.execute(
                    draft_id=prepared["draftId"],
                    fingerprint=prepared["fingerprint"],
                    confirmed=True,
                )
            self.assertEqual(retried["status"], "starting")
            self.assertEqual(len(runner.calls), 1)

    def test_transferred_start_cleanup_unconfirmed_retains_identity(self) -> None:
        """Transferred startup ownership stays durable when manager cleanup is uncertain."""

        class TransferredStartRunner(FakeRunner):
            def start(self, **kwargs):
                self.calls.append(dict(kwargs))
                raise launch_module._SupervisorStartCleanupError(
                    "test transferred startup cleanup",
                    started={
                        "monitorRunId": "transferred-monitor",
                        "pid": 5252,
                        "processGroupId": 5252,
                        "processGroupToken": "group-token-5252",
                        "startupToken": "startup-token-5252",
                        "ready": False,
                        "startupTimedOut": True,
                    },
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            runner = TransferredStartRunner()
            manager = LaunchManager(settings, runner=runner)
            prepared = manager.prepare(self.payload(manager, root))
            with patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=False,
            ) as terminate:
                with self.assertRaisesRegex(LaunchConflictError, "cleanup failed after launch error"):
                    manager.execute(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )
            terminate.assert_called_once_with(5252, "group-token-5252")
            private_status = json.loads(manager._status_path(prepared["draftId"]).read_text(encoding="utf-8"))
            self.assertEqual(private_status["status"], "starting")
            self.assertEqual(private_status["processGroupToken"], "group-token-5252")
            self.assertEqual(private_status["startupToken"], "startup-token-5252")

    def test_timeout_cancel_terminates_owned_group_before_reload(self) -> None:
        """A timed-out start is cancellable only after exact group cleanup."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            runner = TimeoutRunner()
            manager = LaunchManager(settings, runner=runner)
            prepared = manager.prepare(self.payload(manager, root))
            started = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(started["status"], "starting")
            self.assertTrue(started["startupTimedOut"])

            with patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                return_value=True,
            ) as terminate:
                cancelled = manager.cancel(
                    draft_id=prepared["draftId"],
                    fingerprint=prepared["fingerprint"],
                    confirmed=True,
                )
            terminate.assert_called_once_with(5252, "fake-runner-token")
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertFalse(cancelled["recoverable"])

            # A fresh manager sees the durable cancellation only after the
            # private identity has been retained for reload-time liveness
            # checks.  No child is hidden behind a dropped token.
            reloaded = LaunchManager(settings, runner=TimeoutRunner())
            observed = reloaded.status(prepared["draftId"])
            self.assertEqual(observed["status"], "cancelled")
            self.assertFalse(observed["cancelable"])
            self.assertFalse(observed["recoverable"])

    def test_timeout_cancel_termination_failure_stays_recoverable(self) -> None:
        """Failed cleanup leaves the timeout in starting, never cancelled."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root, enabled=True)
            manager = LaunchManager(settings, runner=TimeoutRunner())
            prepared = manager.prepare(self.payload(manager, root))
            started = manager.execute(
                draft_id=prepared["draftId"],
                fingerprint=prepared["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(started["status"], "starting")
            with patch(
                "apps.control_center_operational.launch._terminate_token_owned_process_group",
                side_effect=LaunchConflictError("group stop could not be verified"),
            ) as terminate:
                with self.assertRaisesRegex(LaunchConflictError, "remains recoverable"):
                    manager.cancel(
                        draft_id=prepared["draftId"],
                        fingerprint=prepared["fingerprint"],
                        confirmed=True,
                    )
            terminate.assert_called_once_with(5252, "fake-runner-token")
            observed = manager.status(prepared["draftId"])
            self.assertEqual(observed["status"], "starting")
            self.assertTrue(observed["recoverable"])
            self.assertTrue(observed["cancelable"])

    def test_reload_running_uses_process_group_token_not_startup_token(self) -> None:
        """Receipt and process-group tokens remain independent on reload."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LaunchManager(self.settings(root, enabled=True), runner=FakeRunner())
            draft_id = "reconcile-running"
            run_root = root / "runs" / "running"
            launch_module.atomic_write_json(
                manager._status_path(draft_id),
                {
                    "draftId": draft_id,
                    "status": "running",
                    "runId": "run-running",
                    "runRoot": str(run_root),
                    "pid": 5252,
                    "processGroupId": 5252,
                    "startupToken": "startup-token-1234",
                    "processGroupToken": "group-token-5678",
                    "startedAt": "2026-01-01T00:00:00+00:00",
                },
            )
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=True,
            ) as has_token:
                observed = manager.status(draft_id)
            has_token.assert_called_once_with(5252, "group-token-5678")
            self.assertEqual(observed["status"], "running")
            self.assertEqual(observed["liveness"], "live")

    def test_reload_timed_out_starting_uses_process_group_token_not_startup_token(self) -> None:
        """A stale timeout remains recoverable while its owned group is live."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LaunchManager(self.settings(root, enabled=True), runner=FakeRunner())
            draft_id = "reconcile-timeout"
            run_root = root / "runs" / "timed-out"
            launch_module.atomic_write_json(
                manager._status_path(draft_id),
                {
                    "draftId": draft_id,
                    "status": "starting",
                    "runId": "run-timeout",
                    "runRoot": str(run_root),
                    "pid": 5252,
                    "processGroupId": 5252,
                    "startupToken": "startup-token-1234",
                    "processGroupToken": "group-token-5678",
                    "startupTimedOut": True,
                    "startedAt": "2020-01-01T00:00:00+00:00",
                },
            )
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=True,
            ) as has_token:
                observed = manager.status(draft_id)
            has_token.assert_called_once_with(5252, "group-token-5678")
            self.assertEqual(observed["status"], "starting")
            self.assertEqual(observed["liveness"], "live")
            self.assertTrue(observed["startupTimedOut"])
            self.assertTrue(observed["recoverable"])

    def test_malformed_receipt_fallback_uses_process_group_token_for_live_or_unknown(self) -> None:
        """Malformed receipts do not false-fail a live/unknown owned child."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LaunchManager(self.settings(root, enabled=True), runner=FakeRunner())
            draft_id = "reconcile-malformed"
            run_root = root / "runs" / "malformed"
            control = run_root / "control_plane"
            control.mkdir(parents=True)
            (control / launch_module.SUPERVISOR_READY_FILENAME).write_text("{}\n", encoding="utf-8")
            launch_module.atomic_write_json(
                manager._status_path(draft_id),
                {
                    "draftId": draft_id,
                    "status": "running",
                    "runId": "run-malformed",
                    "runRoot": str(run_root),
                    "pid": 5252,
                    "processGroupId": 5252,
                    "startupToken": "startup-token-1234",
                    "processGroupToken": "group-token-5678",
                    "startedAt": "2026-01-01T00:00:00+00:00",
                },
            )
            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                return_value=True,
            ) as has_token:
                live = manager.status(draft_id)
            has_token.assert_called_once_with(5252, "group-token-5678")
            self.assertEqual(live["status"], "starting")
            self.assertEqual(live["liveness"], "live")
            self.assertTrue(live["recoverable"])

            with patch(
                "apps.control_center_operational.launch._process_group_has_token",
                side_effect=LaunchConflictError("process liveness unavailable"),
            ) as has_token:
                unknown = manager.status(draft_id)
            has_token.assert_called_once_with(5252, "group-token-5678")
            self.assertEqual(unknown["status"], "starting")
            self.assertEqual(unknown["liveness"], "unknown")
            self.assertTrue(unknown["recoverable"])

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

    def test_mission_context_sidecar_is_hash_bound_into_launch_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            brief = "Analyse revenue trends. Build an operations dashboard. Do not expose personal data."
            first_end = brief.index(" Build")
            dashboard_start = brief.index("Build")
            dashboard_end = brief.index(" Do not")
            runner.intake_responses.append({
                "schemaVersion": 1,
                "missionIntent": "hybrid",
                "portfolioStrategy": "separate analysis from product constraints",
                "requirements": [{
                    "candidateId": "C-001",
                    "sourceSpans": [{"blockId": "INPUT-001", "start": 0, "end": first_end}],
                    "businessObjective": "Analyse revenue trends.",
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
                "groups": [{"members": ["C-001"], "rationale": "Independent analysis.", "sharedAnalysisIntent": None, "suggestedSpecialists": []}],
                "productBrief": {"audience": [{"text": "Build an operations dashboard.", "sourceSpans": [{"blockId": "INPUT-001", "start": dashboard_start, "end": dashboard_end}]}]},
                "technicalConstraints": [{"text": "Do not expose personal data.", "sourceSpans": [{"blockId": "INPUT-001", "start": dashboard_end + 1, "end": len(brief)}]}],
                "sourceContext": [],
                "additionalContext": [],
                "unassignedContext": [],
            })
            manager = LaunchManager(self.settings(root, enabled=True), runner=runner)
            prepared = manager.prepare({
                "mode": "new",
                "projectName": "Mission context",
                "intakeBlocks": [brief],
                "sources": [],
                "maxAgents": 2,
                "capacity": {"total": 2, "entityResolution": 1, "analyticalOwner": 1, "specialist": 0},
            })
            result = manager.execute(draft_id=prepared["draftId"], fingerprint=prepared["fingerprint"], confirmed=True)
            run_root = Path(result["runRoot"])
            artifact_root = run_root / "control_center" / "launches" / prepared["draftId"]
            context = json.loads((artifact_root / "mission_context.json").read_text())
            manifest = json.loads((run_root / "control_center" / "launch_manifest.json").read_text())
            intake_plan = json.loads((artifact_root / "intake_plan.json").read_text())
            self.assertEqual(context["contextHash"], manifest["missionContextHash"])
            self.assertEqual(context["contextHash"], intake_plan["missionContextHash"])
            self.assertEqual(context["context"]["mission_intent"], "hybrid")
            self.assertTrue((artifact_root / "mission_plan.json").is_file())

    def test_mission_context_artifacts_stage_without_advancing_active_pointer(self) -> None:
        from auto_foundry_core.mission_context import MissionContext

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LaunchManager(self.settings(root, enabled=True), runner=FakeRunner())
            run_root = (root / "runs" / "RUN-STAGE").resolve()
            run_root.mkdir(parents=True)
            parent_draft = {"draftId": "D-PARENT", "runId": "RUN-STAGE", "fingerprint": "parent"}
            parent_artifacts = manager._write_mission_artifacts(
                run_root,
                parent_draft,
                mission_context=MissionContext("specification"),
                requirement_ids=("REQ-001",),
                portfolio_strategy="parent",
            )
            self.assertFalse((run_root / "control_center" / "mission_context_active.json").exists())
            manager._promote_staged_mission_artifacts(run_root, {"activePointer": parent_artifacts["activePointer"]})
            pointer_path = run_root / "control_center" / "mission_context_active.json"
            before = pointer_path.read_bytes()

            child_artifacts = manager._write_mission_artifacts(
                run_root,
                {"draftId": "D-CHILD", "runId": "RUN-STAGE", "fingerprint": "child"},
                mission_context=MissionContext("hybrid"),
                requirement_ids=("REQ-001", "REQ-002"),
                portfolio_strategy="child",
            )
            # Simulate a staged D publication/admission failure before the
            # promotion boundary: the authoritative bytes and parent remain.
            self.assertEqual(pointer_path.read_bytes(), before)
            active, _ref, _hash = manager._load_existing_mission_context(run_root)
            self.assertEqual(active.mission_intent, "specification")

            manager._promote_staged_mission_artifacts(run_root, {"activePointer": child_artifacts["activePointer"]})
            self.assertNotEqual(pointer_path.read_bytes(), before)
            active, _ref, _hash = manager._load_existing_mission_context(run_root)
            self.assertEqual(active.mission_intent, "hybrid")

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
                    "sourceBindings": [{
                        "source_ref": "brief.md",
                        "locator": {"section": 1, "paragraph": 1},
                        "content_hash": hashlib.sha256(
                            b"Investigate margin and inventory as separate decisions."
                        ).hexdigest(),
                    }],
                    "originalText": "Investigate margin and inventory as separate decisions.",
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

    def test_production_runner_uses_canonical_supervisor_cli_for_launch_and_continuation(self) -> None:
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
            self.assertEqual(
                spec["codex_exec"]["role_models"]["foundry_supervisor"],
                "gpt-5.6-sol",
            )
            self.assertEqual(
                spec["codex_exec"]["role_reasoning_efforts"]["foundry_supervisor"],
                "high",
            )
            self.assertEqual(created["processGroupId"], 91234)
            self.assertNotIn("processGroupToken", created)
            private_statuses = sorted((settings.state_root / "statuses").glob("*.json"))
            self.assertEqual(len(private_statuses), 1)
            private_status = json.loads(private_statuses[0].read_text(encoding="utf-8"))
            process_group_token = private_status["processGroupToken"]
            self.assertRegex(process_group_token, r"^[A-Za-z0-9_-]{8,128}$")
            initial_argv = popen.calls[0][0]
            self.assertEqual(initial_argv[:5], [sys.executable, "-m", "auto_foundry_core.cli", "supervisor", "run"])
            self.assertNotIn("coordinator", initial_argv[:5])
            self.assertNotIn("--spec", initial_argv)
            self.assertNotIn("$auto-foundry-agentic-e2e", initial_argv)
            self.assertNotIn("exec", initial_argv)
            initial_kwargs = popen.calls[0][1]
            self.assertEqual(initial_kwargs["cwd"], str(run_root))
            self.assertEqual(
                initial_kwargs["env"]["AUTO_FOUNDRY_SUPERVISOR_PROCESS_GROUP_TOKEN"],
                process_group_token,
            )
            self.assertNotIn(process_group_token, " ".join(initial_argv))
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
            self.assertEqual(resume_argv[:5], [sys.executable, "-m", "auto_foundry_core.cli", "supervisor", "run"])
            self.assertNotIn("coordinator", resume_argv[:5])
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
            self.assertEqual(
                persisted["codex_exec"]["role_models"]["foundry_supervisor"],
                "gpt-5.6-sol",
            )
            self.assertEqual(
                persisted["codex_exec"]["role_reasoning_efforts"]["foundry_supervisor"],
                "high",
            )
            self.assertEqual(persisted["codex_exec"]["skill_version"], "0.8.0")
            self.assertEqual(persisted["codex_exec"]["core_version"], "0.9.0")
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

    def test_resume_rebinds_legacy_spec_before_supervisor_spawn(self) -> None:
        from auto_foundry_core import (
            ItemWorkspace,
            RequirementRecord,
            RequirementSupervisorWorkspace,
            RunContext,
            RunLifecycle,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "RUN-G5-RESUME"
            _write_legacy_g5_control_plane(root, run_id=run_id, generation_id="G-0001")
            context = RunContext(run_id, root)
            RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            ItemWorkspace.create(context, "REQ-001", mode="requirement", original_text="Legacy resume")
            plan = RequirementSupervisorWorkspace(context).plan_requirements(
                (RequirementRecord("REQ-001", "Legacy resume"),),
                planner_ref="control-center-planner",
                persist=True,
            )
            settings = self.settings(root, enabled=True)
            manager = LaunchManager(settings)

            prepared = manager.prepare_resume_coordinator(run_id, root)

            self.assertEqual(prepared["operation"], "run")
            persisted = json.loads((root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["generation_id"], "G-0001")
            self.assertEqual(persisted["planner_ref"], plan.planner_ref)
            self.assertEqual(persisted["codex_exec"]["role_models"]["foundry_supervisor"], "gpt-5.6-sol")
            self.assertEqual(persisted["codex_exec"]["role_reasoning_efforts"]["foundry_supervisor"], "high")
            events = [
                json.loads(line)
                for line in (root / "control_plane" / "coordinator_events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["event"], "legacy_imported")
            self.assertEqual(events[-1]["event"], "plan_rebound")

    def test_resume_rebinds_same_lineage_transport_without_losing_paused_progress(self) -> None:
        from auto_foundry_core import (
            CoordinatorRunSpec,
            ItemWorkspace,
            RequirementRecord,
            RequirementSupervisorWorkspace,
            RunContext,
            RunCoordinator,
            RunLifecycle,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "RUN-TRANSPORT-RESUME"
            context = RunContext(run_id, root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            ItemWorkspace.create(context, "REQ-001", mode="requirement", original_text="Transport resume")
            plan = RequirementSupervisorWorkspace(context).plan_requirements(
                (RequirementRecord("REQ-001", "Transport resume"),),
                planner_ref="control-center-planner",
                persist=True,
            )
            lifecycle.pause("test transport resume")
            settings = self.settings(root, enabled=True)
            binding = coordinator_module.resolve_production_skill_binding(repo_root=root, role_cwd=root)
            old_spec = CoordinatorRunSpec(
                run_id=run_id,
                generation_id="G-0001",
                planner_ref=plan.planner_ref,
                planner_hash=_planner_plan_hash(plan.to_dict()),
                publication_policy={"enabled": False},
                codex_exec={
                    "binary": settings.codex_bin,
                    "sandbox": "workspace-write",
                    "ephemeral": True,
                    **binding,
                },
            )
            coordinator = RunCoordinator(context, planner=lambda _state: ())
            coordinator.start(old_spec)
            before_state = json.loads(coordinator.state_path.read_text(encoding="utf-8"))
            manager = LaunchManager(settings)

            prepared = manager.prepare_resume_coordinator(run_id, root)

            self.assertEqual(prepared["operation"], "run")
            persisted = json.loads((root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["generation_id"], old_spec.generation_id)
            self.assertEqual(persisted["planner_ref"], old_spec.planner_ref)
            self.assertEqual(persisted["planner_hash"], old_spec.planner_hash)
            self.assertEqual(persisted["codex_exec"]["role_models"]["foundry_supervisor"], "gpt-5.6-sol")
            self.assertEqual(persisted["codex_exec"]["role_reasoning_efforts"]["foundry_supervisor"], "high")
            after_state = json.loads(coordinator.state_path.read_text(encoding="utf-8"))
            self.assertEqual(after_state["status"], before_state["status"])
            self.assertEqual(after_state["phase"], before_state["phase"])
            self.assertEqual(after_state["active_dispatches"], before_state["active_dispatches"])
            events = [
                json.loads(line)
                for line in (root / "control_plane" / "coordinator_events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event"], "coordinator_transport_rebound")
            self.assertEqual(lifecycle.status, "paused")

    def test_resume_rebinds_rotated_skill_transport_after_coordinator_restart(self) -> None:
        from auto_foundry_core import (
            CoordinatorRunSpec,
            ItemWorkspace,
            RequirementRecord,
            RequirementSupervisorWorkspace,
            RunContext,
            RunCoordinator,
            RunLifecycle,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "RUN-TRANSPORT-ROTATED-RESUME"
            settings = self.settings(root, enabled=True)
            context = RunContext(run_id, root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            ItemWorkspace.create(context, "REQ-001", mode="requirement", original_text="Rotated transport resume")
            plan = RequirementSupervisorWorkspace(context).plan_requirements(
                (RequirementRecord("REQ-001", "Rotated transport resume"),),
                planner_ref="control-center-planner",
                persist=True,
            )
            lifecycle.pause("test rotated transport resume")
            old_binding = coordinator_module.resolve_production_skill_binding(repo_root=root, role_cwd=root)
            old_spec = CoordinatorRunSpec(
                run_id=run_id,
                generation_id="G-0001",
                planner_ref=plan.planner_ref,
                planner_hash=_planner_plan_hash(plan.to_dict()),
                publication_policy={"enabled": False},
                codex_exec={
                    "binary": settings.codex_bin,
                    "sandbox": "workspace-write",
                    "ephemeral": True,
                    **old_binding,
                },
            )
            coordinator = RunCoordinator(context, planner=lambda _state: ())
            coordinator.start(old_spec)
            before_state = json.loads(coordinator.state_path.read_text(encoding="utf-8"))

            skill_path = Path(old_binding["skill_path"])
            (skill_path / "README.md").write_text("rotated release fixture\n", encoding="utf-8")
            new_hash = hashlib.sha256(coordinator_module._skill_release_bytes(skill_path)).hexdigest()
            self.assertNotEqual(old_binding["skill_sha256"], new_hash)
            coordinator_module.PRODUCTION_SKILL_SHA256 = new_hash

            # Recreate the launch manager after the skill release changed. The
            # persisted coordinator object above is intentionally not reused;
            # preparation must rotate transport before reconstructing it.
            manager = LaunchManager(settings)
            prepared = manager.prepare_resume_coordinator(run_id, root)

            self.assertEqual(prepared["operation"], "run")
            persisted = json.loads((root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["codex_exec"]["skill_sha256"], new_hash)
            after_state = json.loads(coordinator.state_path.read_text(encoding="utf-8"))
            for field in ("generation_id", "planner_ref", "planner_hash", "status", "phase", "active_dispatches", "attempt", "no_progress_count", "last_action"):
                self.assertEqual(after_state[field], before_state[field])
            events = [
                json.loads(line)
                for line in (root / "control_plane" / "coordinator_events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event"], "coordinator_transport_rebound")
            self.assertEqual(lifecycle.status, "paused")

    def test_resume_rebinds_pending_plan_after_rotated_skill_and_restart(self) -> None:
        """A pending plan intent is retargeted before a resumed child is built."""

        from auto_foundry_core import (
            CoordinatorRunSpec,
            ItemWorkspace,
            RequirementRecord,
            RequirementRunExtension,
            RequirementSupervisorWorkspace,
            RunCoordinator,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "RUN-PENDING-ROTATED-RESUME"
            settings = self.settings(root, enabled=True)
            context = RunContext(run_id, root)
            lifecycle = RunLifecycle.create(context, ["REQ-001"], mode="requirement")
            ItemWorkspace.create(context, "REQ-001", mode="requirement", original_text="Pending resume")
            workspace = RequirementSupervisorWorkspace(context)
            first_plan = workspace.plan_requirements(
                (RequirementRecord("REQ-001", "Pending resume"),),
                planner_ref="control-center-planner",
                persist=True,
            )
            RequirementRunExtension.append(
                context,
                RequirementRecord("REQ-002", "New pending requirement"),
            )
            second_plan = workspace.load()
            self.assertEqual(lifecycle.active_generation_id(context), "G-0002")

            manager = LaunchManager(settings)
            role_models, role_reasoning = manager._production_role_bindings(manager._core_imports())
            old_binding = coordinator_module.resolve_production_skill_binding(repo_root=root, role_cwd=root)
            old_codex = {
                "binary": settings.codex_bin,
                "sandbox": "workspace-write",
                "ephemeral": True,
                "role_models": role_models,
                "role_reasoning_efforts": role_reasoning,
                **old_binding,
            }
            source = CoordinatorRunSpec(
                run_id,
                "G-0001",
                first_plan.planner_ref,
                _planner_plan_hash(first_plan.to_dict()),
                publication_policy={"enabled": False},
                codex_exec=old_codex,
            )
            old_target = CoordinatorRunSpec(
                run_id,
                "G-0002",
                second_plan.planner_ref,
                _planner_plan_hash(second_plan.to_dict()),
                publication_policy={"enabled": False},
                codex_exec=old_codex,
            )
            coordinator = RunCoordinator(context, planner=lambda _state: ())
            coordinator.start(source)

            def fail_started(name: str) -> None:
                if name == "plan_rebind_after_spec":
                    raise RuntimeError(name)

            coordinator._failpoint = fail_started
            with self.assertRaises(RuntimeError):
                coordinator.publish_and_rebind(old_target, lambda _spec: None)
            raw_spec = json.loads((root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8"))
            state = json.loads((root / "control_plane" / "coordinator_state.json").read_text(encoding="utf-8"))
            pending = state["pending_plan_rebind"]
            self.assertEqual(raw_spec["generation_id"], old_target.generation_id)
            self.assertNotEqual(state["generation_id"], raw_spec["generation_id"])
            self.assertEqual(pending["old_spec_hash"], state["spec_hash"])
            self.assertEqual(pending["new_spec_hash"], coordinator._spec_hash(old_target))
            coordinator.close()

            skill_path = Path(old_binding["skill_path"])
            (skill_path / "README.md").write_text("rotated release fixture\n", encoding="utf-8")
            new_hash = hashlib.sha256(coordinator_module._skill_release_bytes(skill_path)).hexdigest()
            coordinator_module.PRODUCTION_SKILL_SHA256 = new_hash
            os.environ["AUTO_FOUNDRY_TEST_SKILL_SHA256"] = new_hash

            prepared = LaunchManager(settings).prepare_resume_coordinator(run_id, root)

            self.assertEqual(prepared["operation"], "run")
            persisted = json.loads((root / "control_plane" / "coordinator_spec.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["generation_id"], "G-0002")
            self.assertEqual(persisted["codex_exec"]["skill_sha256"], new_hash)
            events = [
                json.loads(line)
                for line in (root / "control_plane" / "coordinator_events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["event"] for event in events][-2:],
                ["plan_rebind_transport_retargeted", "plan_rebound"],
            )

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
                result = manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
                self.assertEqual(result["status"], "queued")
                self.assertTrue(result["pendingDataRefresh"])
                self.assertTrue(
                    (run_root / "control_center" / "launches" / continuation["draftId"] / "pending_data_refresh.json").is_file()
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
            run_root = Path(created["runRoot"])
            mission_pointer_path = run_root / "control_center" / "mission_context_active.json"
            mission_pointer_before = mission_pointer_path.read_bytes()
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
                queued = initial.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
                self.assertEqual(queued["status"], "queued")
                self.assertTrue(queued["pendingDataRefresh"])
            finally:
                RunCoordinator.publish_and_rebind = original_publish

            self.assertTrue((run_root / "extensions" / "G-0002" / "requirement_supervisor_plan.json").is_file())
            self.assertEqual(mission_pointer_path.read_bytes(), mission_pointer_before)
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
            self.assertNotEqual(mission_pointer_path.read_bytes(), mission_pointer_before)
            self.assertEqual(json.loads((run_root / "control_plane" / "coordinator_spec.json").read_text())["generation_id"], "G-0002")
            events = events_path.read_text(encoding="utf-8")
            self.assertEqual(events.count('"event":"plan_rebound"'), 1)
            self.assertEqual(len(runner.calls), 2)

    def test_queued_data_refresh_retries_through_consumer_and_promotes_once(self) -> None:
        """The launch retry is the production pending->applied boundary.

        The first consumer pass leaves the canonical D admission pending.  A
        retry invokes the same ``RunCoordinator.consume_pending_data_refresh``
        entrypoint, then promotes the staged MissionContext exactly once when
        that consumer reports a non-pending phase.
        """

        from auto_foundry_core.coordinator import RunCoordinator
        from auto_foundry_core.run_extension import RequirementRunExtension

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            settings = self.settings(root, enabled=True)
            repository = OperationalRepository(None, [settings.runs_root])
            manager = LaunchManager(settings, repository=repository, runner=runner)
            initial = manager.prepare({
                "mode": "new",
                "projectName": "Queued refresh consumer",
                "intakeBlocks": ["Initial"],
                "sources": [],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            created = manager.execute(
                draft_id=initial["draftId"],
                fingerprint=initial["fingerprint"],
                confirmed=True,
            )
            run_id = repository.list_runs()[0]["id"]
            upload = manager.upload(
                io.BytesIO(b"id,value\n2,3\n"),
                filename="refresh.csv",
                relative_path="refresh.csv",
                content_length=len(b"id,value\n2,3\n"),
            )
            continuation = manager.prepare({
                "mode": "continue",
                "runId": run_id,
                "intakeBlocks": ["Refresh"],
                "sources": [{"kind": "upload", "uploadId": upload.upload_id}],
                "maxAgents": 4,
                "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
            })
            run_root = Path(created["runRoot"])
            mission_pointer = run_root / "control_center" / "mission_context_active.json"
            before = mission_pointer.read_bytes()
            consume_calls = 0

            def consume_once_then_apply(_coordinator):
                nonlocal consume_calls
                consume_calls += 1
                if consume_calls == 1:
                    return SimpleNamespace(phase="data_refresh_pending", active_dispatches=())
                return SimpleNamespace(phase="waiting", active_dispatches=())

            class LoadedExtension:
                generation_id = "G-0002"

            LoadedExtension.plan_path = run_root / "requirement_supervisor_plan.json"

            with patch.object(RunCoordinator, "consume_pending_data_refresh", consume_once_then_apply), patch.object(
                RequirementRunExtension,
                "load",
                return_value=LoadedExtension(),
            ), patch.object(manager, "_prepare_coordinator", return_value={"operation": "run"}), patch.object(
                manager,
                "_promote_staged_mission_artifacts",
                wraps=manager._promote_staged_mission_artifacts,
            ) as promote:
                queued = manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
                self.assertTrue(queued["pendingDataRefresh"])
                self.assertEqual(queued["status"], "queued")
                self.assertEqual(mission_pointer.read_bytes(), before)
                applied = manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )

            self.assertEqual(applied["status"], "accepted")
            self.assertEqual(consume_calls, 2)
            self.assertEqual(promote.call_count, 1)
            self.assertNotEqual(mission_pointer.read_bytes(), before)

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
            # New-run bootstrap owns an immutable D-0001 alias.  Keep the
            # legacy archive in place; an unrelated external catalog must not
            # replace or invalidate that authoritative revision.
            self.assertTrue((run_root / "inputs" / "data_room.zip").is_file())
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
            result = manager.execute(
                draft_id=continuation["draftId"],
                fingerprint=continuation["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["dataRevisionId"], "D-0001")
            self.assertTrue((run_root / "inputs" / "data_room.zip").is_file())

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

    def test_zip_member_over_64_mib_is_streamed_without_business_size_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "large-member.zip"
            source.parent.mkdir(parents=True)
            member_size = 64 * 1024 * 1024 + 1
            info = zipfile.ZipInfo("payload.bin")
            info.compress_type = zipfile.ZIP_STORED
            with zipfile.ZipFile(source, "w", allowZip64=True) as archive:
                with archive.open(info, "w") as target:
                    chunk = b"x" * (1024 * 1024)
                    remaining = member_size
                    while remaining:
                        piece = chunk if remaining >= len(chunk) else chunk[:remaining]
                        target.write(piece)
                        remaining -= len(piece)
            manager = LaunchManager(self.settings(root))
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Large archive member",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(source)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["valid"], prepared.get("errors"))
            draft = manager._load_draft(prepared["draftId"], prepared["fingerprint"])
            destination = root / "large-package.zip"
            entries = manager._package_zip(draft, destination)
            self.assertEqual(entries[0]["size"], member_size)
            with zipfile.ZipFile(destination) as packaged:
                self.assertEqual(packaged.getinfo("payload.bin").file_size, member_size)

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

    def test_local_package_binds_one_descriptor_and_keeps_destination_on_mutation(self) -> None:
        """A source mutation during streaming cannot publish a partial package."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "mutable.bin"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"a" * (2 * 1024 * 1024))
            manager = LaunchManager(self.settings(root))
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "Local descriptor binding",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(source)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["prepared"], prepared.get("errors"))
            draft = manager._load_draft(prepared["draftId"], prepared["fingerprint"])
            destination = root / "package.zip"
            destination.write_bytes(b"previous package remains intact")
            original_open = launch_module.Path.open
            mutated = False

            class MutatingReader:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def read(self, *args, **kwargs):
                    nonlocal mutated
                    data = self.stream.read(*args, **kwargs)
                    if data and not mutated:
                        mutated = True
                        descriptor = os.open(source, os.O_RDWR)
                        try:
                            os.pwrite(descriptor, b"b", 1_500_000)
                        finally:
                            os.close(descriptor)
                    return data

                def __getattr__(self, name):
                    return getattr(self.stream, name)

            def hooked_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                mode = args[0] if args else kwargs.get("mode", "r")
                if Path(path) == source.resolve() and mode == "rb":
                    return MutatingReader(stream)
                return stream

            with patch("apps.control_center_operational.launch.Path.open", new=hooked_open):
                with self.assertRaisesRegex(ValueError, "local source changed after prepare"):
                    manager._package_zip(draft, destination)
            self.assertTrue(mutated)
            self.assertEqual(destination.read_bytes(), b"previous package remains intact")
            self.assertEqual(list(root.glob(f".{destination.name}.*.tmp")), [])

    def test_symlink_component_walk_reaches_deep_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            source_root.mkdir()
            target = source_root / "target"
            target.mkdir()
            alias = source_root / "deep-alias"
            alias.symlink_to(target, target_is_directory=True)
            deep_alias = alias
            for index in range(130):
                deep_alias = deep_alias / f"d{index}"
            deep_alias.mkdir(parents=True)
            aliased_file = deep_alias / "payload.bin"
            aliased_file.write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "symlink source paths"):
                launch_module.reject_symlink_components(aliased_file, source_root)

            deep_plain = source_root / "deep-plain"
            for index in range(130):
                deep_plain = deep_plain / f"d{index}"
            deep_plain.mkdir(parents=True)
            plain_file = deep_plain / "payload.bin"
            plain_file.write_bytes(b"x")
            launch_module.reject_symlink_components(plain_file, source_root)

    def test_zip_adversarial_members_report_concrete_errors(self) -> None:
        cases = [
            ("traversal", [("../evil.csv", b"x")], "evil.csv"),
            ("absolute", [("/evil.csv", b"x")], "evil.csv"),
            ("backslash", [("dir\\evil.csv", b"x")], "evil.csv"),
            ("drive", [("C:evil.csv", b"x")], "evil.csv"),
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

            for label, member_name in (("nested", "inner.zip"), ("unknown", "payload.bin")):
                path = root / "source" / f"{label}.zip"
                path.write_bytes(_zip_payload([(member_name, b"opaque")]))
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
                self.assertTrue(result["valid"], label)
                draft = json.loads((manager.drafts_root / f"{result['draftId']}.json").read_text())
                self.assertEqual(draft["sources"][0]["memberCount"], 1)
                packaged = root / f"{label}-package.zip"
                manager._package_zip(draft, packaged)
                with zipfile.ZipFile(packaged) as output:
                    self.assertEqual(output.namelist(), [member_name])

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

            source.write_bytes(_forge_eocd_fields(valid, entries=4097))
            # The historical count is now an explicit opt-in cap.  Keep this
            # pre-allocation guard regression without coupling it to the
            # unbounded default.
            with patch("apps.control_center_operational.launch.MAX_ZIP_MEMBER_COUNT", 4096):
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
            manager = LaunchManager(self.settings(root))
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "ZIP64 package",
                    "intakeBlocks": ["r"],
                    "sources": [{"kind": "local_path", "path": str(source)}],
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["valid"], prepared.get("errors"))
            packaged = root / "zip64-package.zip"
            manager._package_zip(manager._load_draft(prepared["draftId"], prepared["fingerprint"]), packaged)
            with zipfile.ZipFile(packaged) as output:
                self.assertEqual(output.namelist(), ["safe.csv"])

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

    def test_default_source_and_zip_counts_are_unbounded_but_explicit_caps_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            manager = LaunchManager(settings)
            source_root = root / "source"
            source_paths = []
            for index in range(257):
                path = source_root / f"source-{index:03d}.txt"
                path.write_text("x", encoding="utf-8")
                source_paths.append({"kind": "local_path", "path": str(path)})

            canonical, errors = manager._canonical_sources(source_paths, mode="new")
            self.assertEqual(len(canonical), 257)
            self.assertNotIn("sources", errors)
            prepared = manager.prepare(
                {
                    "mode": "new",
                    "projectName": "many local sources",
                    "intakeBlocks": ["r"],
                    "sources": source_paths,
                    "maxAgents": 1,
                    "capacity": {"total": 1, "entityResolution": 0, "analyticalOwner": 1, "specialist": 0},
                }
            )
            self.assertTrue(prepared["valid"], prepared.get("errors"))
            self.assertEqual(prepared["summary"]["sources"], 257)

            capped = LaunchSettings(
                runtime_root=root,
                runs_root=root / "capped-runs",
                source_roots=(source_root,),
                max_source_count=2,
            )
            capped_manager = LaunchManager(capped)
            _canonical, capped_errors = capped_manager._canonical_sources(source_paths[:3], mode="new")
            self.assertEqual(capped_errors.get("sources"), "Too many source entries.")

            archive = source_root / "more-than-legacy-members.zip"
            archive.write_bytes(
                _zip_payload(
                    [(f"member-{index:04d}.txt", b"") for index in range(4097)],
                    compression=zipfile.ZIP_STORED,
                )
            )
            inspection = _inspect_zip_source(archive, max_total_bytes=None, read_members=False)
            self.assertEqual(inspection.member_count, 4097)
            self.assertEqual(inspection.physical_entry_count, 4097)
            with patch("apps.control_center_operational.launch.MAX_ZIP_MEMBER_COUNT", 2), self.assertRaisesRegex(
                ValueError, "physical entry limit"
            ):
                _inspect_zip_source(archive, max_total_bytes=None, read_members=False)

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
