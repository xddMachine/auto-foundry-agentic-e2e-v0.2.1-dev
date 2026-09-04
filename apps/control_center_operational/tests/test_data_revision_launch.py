from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from auto_foundry_core.data_revisions import DataRevisionStore
from auto_foundry_core.requirement_planning import RequirementExecutionPlan
from auto_foundry_core.contracts import RequirementRecord
from auto_foundry_core.workspace import RunContext
from apps.control_center_operational.launch import LaunchConflictError, LaunchManager, LaunchSettings
from apps.control_center_operational.projection import _data_revision_projection


def _entry(name: str, payload: bytes) -> tuple[str, bytes, str]:
    return name, payload, hashlib.sha256(payload).hexdigest()


class DataRevisionLaunchTests(unittest.TestCase):
    def _manager(self, root: Path) -> LaunchManager:
        source_root = root / "source"
        source_root.mkdir(parents=True, exist_ok=True)
        settings = LaunchSettings(
            runtime_root=root,
            runs_root=root / "runs",
            source_roots=(source_root,),
            enable_launch=True,
            launch_token="test-token",
        )
        return LaunchManager(settings)

    def test_deterministic_existing_archive_merge_adds_and_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self._manager(root)
            current = root / "current.zip"
            additions = root / "additions.zip"
            candidate = root / "candidate.zip"
            manager._write_deterministic_zip(
                {"orders.csv": _entry("orders.csv", b"id,value\n1,2\n"), "notes.txt": _entry("notes.txt", b"old\n")},
                current,
            )
            manager._write_deterministic_zip(
                {"orders.csv": _entry("orders.csv", b"id,value\n1,9\n"), "new.csv": _entry("new.csv", b"a,b\n3,4\n")},
                additions,
            )
            provenance = manager._merge_data_room_archives(
                current,
                additions,
                candidate,
                source_entries=({"relativePath": "orders.csv"}, {"relativePath": "new.csv"}),
            )
            self.assertTrue(provenance["changed"])
            self.assertEqual(provenance["addedPaths"], ["new.csv"])
            self.assertEqual(provenance["replacedPaths"], ["orders.csv"])
            with zipfile.ZipFile(candidate) as archive:
                self.assertEqual(archive.namelist(), ["new.csv", "notes.txt", "orders.csv"])
                self.assertEqual(archive.read("orders.csv"), b"id,value\n1,9\n")
                self.assertEqual(archive.read("notes.txt"), b"old\n")

    def test_existing_archive_is_aliased_then_appended_without_rewriting_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "runs" / "RUN-DATA"
            inputs = run_root / "inputs"
            inputs.mkdir(parents=True)
            manager = self._manager(root)
            legacy = inputs / "data_room.zip"
            manager._write_deterministic_zip({"orders.csv": _entry("orders.csv", b"id,value\n1,2\n")}, legacy)
            context = RunContext("RUN-DATA", run_root, input_roots=(inputs,))
            store = DataRevisionStore(context)
            first = store.initialize_legacy("data_room.zip")
            before = legacy.read_bytes()
            candidate = run_root / "candidate.zip"
            manager._write_deterministic_zip(
                {"orders.csv": _entry("orders.csv", b"id,value\n1,9\n"), "new.csv": _entry("new.csv", b"a,b\n3,4\n")},
                candidate,
            )
            second = store.append(
                candidate,
                expected_current_revision_id=first.revision_id,
                expected_current_manifest_hash=first.manifest_hash,
            )
            self.assertEqual(first.revision_id, "D-0001")
            self.assertEqual(second.revision_id, "D-0002")
            self.assertEqual(legacy.read_bytes(), before)
            self.assertEqual(store.current().revision_id, "D-0002")

    def test_stale_source_rebase_requires_a_valid_immutable_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self._manager(root)
            run_root = root / "runs" / "RUN-LINEAGE"
            inputs = run_root / "inputs"
            inputs.mkdir(parents=True)
            legacy = inputs / "data_room.zip"
            candidate = inputs / "candidate.zip"
            manager._write_deterministic_zip({"base.csv": _entry("base.csv", b"id\n1\n")}, legacy)
            manager._write_deterministic_zip({"next.csv": _entry("next.csv", b"id\n2\n")}, candidate)
            store = DataRevisionStore(RunContext("RUN-LINEAGE", run_root, input_roots=(inputs,)))
            first = store.initialize_legacy("data_room.zip")
            second = store.append(
                candidate,
                expected_current_revision_id=first.revision_id,
                expected_current_manifest_hash=first.manifest_hash,
            )
            expected_second = {
                "revisionId": second.revision_id,
                "manifestHash": second.manifest_hash,
                "archiveSha256": second.archive_sha256,
                "archiveSizeBytes": second.archive_size_bytes,
            }
            # An older current revision is not a descendant of the draft's
            # expected D2 and must not be treated as a safe rebase target.
            self.assertFalse(manager._validated_revision_ancestor(store, first, expected_second))
            tampered = dict(expected_second)
            tampered["manifestHash"] = "f" * 64
            with self.assertRaises(LaunchConflictError):
                manager._validated_revision_ancestor(store, second, tampered)

    def test_projection_exposes_current_and_pending_revision_from_durable_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "runs" / "RUN-PROJECTION"
            inputs = run_root / "inputs"
            inputs.mkdir(parents=True)
            manager = self._manager(root)
            legacy = inputs / "data_room.zip"
            manager._write_deterministic_zip({"orders.csv": _entry("orders.csv", b"id,value\n1,2\n")}, legacy)
            revision = DataRevisionStore(
                RunContext("RUN-PROJECTION", run_root, input_roots=(inputs,))
            ).initialize_legacy("data_room.zip")
            record = RequirementRecord(
                requirement_id="REQ-001",
                original_text="Inspect the refreshed data.",
                business_objective="Inspect the refreshed data.",
                expected_analytical_outputs=(),
                expected_visual_outputs=(),
                dependencies=(),
                data_needs=(),
                ontology_needs=(),
                prepared_data_needs=(),
                working_definitions=(),
                limitations=(),
                explicit_priority=None,
                scope="analytics",
                metadata={},
            )
            plan = RequirementExecutionPlan.from_requirements([record])
            pending = DataRevisionStore(
                RunContext("RUN-PROJECTION", run_root, input_roots=(inputs,))
            ).admit_pending_data_refresh(
                data_revision=revision,
                plan=plan.to_dict(),
                reopened_item_ids=["REQ-001"],
                expected_parent_generation_id="G-0001",
                expected_parent_state_hash="a" * 64,
                expected_parent_plan_hash="b" * 64,
                launch_draft_id="DRAFT-1",
                launch_fingerprint="c" * 64,
                created_at="2026-08-26T00:00:00Z",
            )
            projection = _data_revision_projection(run_root)
            self.assertIsNotNone(projection)
            assert projection is not None
            self.assertEqual(projection["state"], "pending_next_safe_scheduler_boundary")
            self.assertEqual(projection["current"]["revisionId"], "D-0001")
            self.assertEqual(projection["pending"]["reopenedItemIds"], ["REQ-001"])

    def test_projection_never_calls_unbound_current_revision_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "runs" / "RUN-TX-PROJECTION"
            inputs = run_root / "inputs"
            inputs.mkdir(parents=True)
            manager = self._manager(root)
            legacy = inputs / "data_room.zip"
            addition = inputs / "addition.zip"
            manager._write_deterministic_zip({"orders.csv": _entry("orders.csv", b"id,value\n1,2\n")}, legacy)
            manager._write_deterministic_zip({"orders.csv": _entry("orders.csv", b"id,value\n1,3\n")}, addition)
            context = RunContext("RUN-TX-PROJECTION", run_root, input_roots=(inputs,))
            store = DataRevisionStore(context)
            first = store.initialize_legacy("data_room.zip")
            store.append(
                addition,
                expected_current_revision_id=first.revision_id,
                expected_current_manifest_hash=first.manifest_hash,
                transaction={
                    "launch_draft_id": "DRAFT-TX",
                    "launch_fingerprint": "a" * 64,
                    "created_at": "2026-08-26T00:00:00Z",
                },
            )
            projection = _data_revision_projection(run_root)
            self.assertIsNotNone(projection)
            assert projection is not None
            self.assertEqual(projection["state"], "revision_pending_admission")
            self.assertEqual(projection["current"]["revisionId"], "D-0002")
            self.assertEqual(projection["pendingRevision"]["revisionId"], "D-0002")

    def test_data_only_existing_run_prepare_accepts_sources_and_binds_current_d(self) -> None:
        from apps.control_center_operational.tests.test_launch import FakeRunner, LaunchTests

        helper = LaunchTests()
        helper.setUp()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner = FakeRunner()
                settings = helper.settings(root, enabled=True)
                from apps.control_center_operational.projection import OperationalRepository

                repository = OperationalRepository(None, [settings.runs_root])
                manager = LaunchManager(settings, repository=repository, runner=runner)
                first = manager.prepare(
                    {
                        "mode": "new",
                        "projectName": "Data only",
                        "intakeBlocks": ["Initial requirement"],
                        "sources": [],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
                run_id = repository.list_runs()[0]["id"]
                upload = manager.upload(
                    io.BytesIO(b"id,value\n2,3\n"),
                    filename="refresh.csv",
                    relative_path="refresh.csv",
                    content_length=len(b"id,value\n2,3\n"),
                )
                prepared = manager.prepare(
                    {
                        "mode": "continue",
                        "runId": run_id,
                        "intakeBlocks": [],
                        "sources": [{"kind": "upload", "uploadId": upload.upload_id}],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                self.assertTrue(prepared["valid"], prepared.get("errors"))
                self.assertEqual(prepared["summary"]["inputBlocks"], 0)
                draft = json.loads((settings.state_root / "drafts" / f"{prepared['draftId']}.json").read_text())
                # New-run bootstrap publishes the immutable legacy alias
                # before any continuation draft is prepared.
                self.assertEqual(draft["dataRevision"]["revisionId"], "D-0001")
                result = manager.execute(
                    draft_id=prepared["draftId"],
                    fingerprint=prepared["fingerprint"],
                    confirmed=True,
                )
                self.assertEqual(result["dataRevisionId"], "D-0002")
                self.assertEqual(result["reopenedItemIds"], ["REQ-001"])
                self.assertEqual(len(runner.calls), 2)
                self.assertTrue((Path(created["runRoot"]) / "data_room" / "current_revision.json").is_file())
        finally:
            helper.tearDown()

    def test_new_document_planner_sees_staged_candidate_and_failure_keeps_pointer(self) -> None:
        from apps.control_center_operational.tests.test_launch import FakeRunner, LaunchTests
        from apps.control_center_operational.projection import OperationalRepository

        helper = LaunchTests()
        helper.setUp()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner = FakeRunner()
                settings = helper.settings(root, enabled=True)
                repository = OperationalRepository(None, [settings.runs_root])
                manager = LaunchManager(settings, repository=repository, runner=runner)
                first = manager.prepare(
                    {
                        "mode": "new",
                        "projectName": "Document refresh",
                        "intakeBlocks": ["Initial requirement"],
                        "sources": [],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                created = manager.execute(draft_id=first["draftId"], fingerprint=first["fingerprint"], confirmed=True)
                run_id = repository.list_runs()[0]["id"]
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
                    archive.writestr("brief.txt", "A new document-backed requirement.\n")
                payload.seek(0)
                upload = manager.upload(
                    payload,
                    filename="brief.zip",
                    relative_path="brief.zip",
                    content_length=payload.getbuffer().nbytes,
                )
                continuation = manager.prepare(
                    {
                        "mode": "continue",
                        "runId": run_id,
                        "intakeBlocks": ["Assess the attached document."],
                        "sources": [{"kind": "upload", "uploadId": upload.upload_id}],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                run_root = Path(created["runRoot"])
                pointer_path = run_root / "data_room/current_revision.json"
                before_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
                mission_pointer_path = run_root / "control_center" / "mission_context_active.json"
                before_mission_pointer = mission_pointer_path.read_bytes()
                observed_rooms: list[str] = []

                class DocumentPlanner:
                    fail = True

                    def plan_intake(self, **kwargs):
                        data_room = str(kwargs["data_room"])
                        observed_rooms.append(data_room)
                        with zipfile.ZipFile(data_room) as archive:
                            if "brief.txt" not in archive.namelist():
                                raise AssertionError("planner did not receive the staged document")
                        if self.fail:
                            raise LaunchConflictError("injected planner failure")
                        return {
                            "schemaVersion": 1,
                            "portfolioStrategy": "document strategy",
                            "requirements": [
                                {
                                    "candidateId": "document-candidate",
                                    "sourceSpans": [{"blockId": "INPUT-001", "start": 0, "end": 29}],
                                    "documentRefs": ["brief.txt"],
                                    "sourceBindings": [{
                                        "source_ref": "brief.txt",
                                        "locator": {"section": 1, "paragraph": 1},
                                        "content_hash": hashlib.sha256(
                                            b"A new document-backed requirement."
                                        ).hexdigest(),
                                    }],
                                    "originalText": "Assess the attached document.\n\nA new document-backed requirement.",
                                    "businessObjective": "Support the document requirement.",
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
                                    "decompositionRationale": "document",
                                }
                            ],
                            "groups": [
                                {
                                    "members": ["REQ-001", "document-candidate"],
                                    "rationale": "document route",
                                    "sharedAnalysisIntent": None,
                                    "suggestedSpecialists": [],
                                }
                            ],
                            "unassignedContext": [],
                        }

                planner = DocumentPlanner()
                manager.intake_planner = planner
                with self.assertRaises(LaunchConflictError):
                    manager.execute(
                        draft_id=continuation["draftId"],
                        fingerprint=continuation["fingerprint"],
                        confirmed=True,
                    )
                self.assertTrue(observed_rooms)
                after_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
                # D-0001 was materialized during new-run bootstrap.  A
                # planner/materialisation failure must leave that pointer
                # byte-identical and must not create a transaction or
                # pending admission for D-0002.
                self.assertTrue(after_pointer is not None)
                self.assertEqual(before_pointer, after_pointer)
                self.assertEqual(mission_pointer_path.read_bytes(), before_mission_pointer)
                self.assertEqual(
                    DataRevisionStore(
                        RunContext(created["runId"], run_root, input_roots=(run_root / "inputs", run_root / "data_room" / "revisions"))
                    ).current().revision_id,
                    "D-0001",
                )
                self.assertFalse((run_root / "data_room/revision_transaction.json").exists())
                self.assertFalse((run_root / "data_room/pending_data_refresh.json").exists())
                planner.fail = False
                result = manager.execute(
                    draft_id=continuation["draftId"],
                    fingerprint=continuation["fingerprint"],
                    confirmed=True,
                )
                self.assertEqual(result["status"], "accepted")
                self.assertTrue(result["pendingDataRefresh"])
                # Queueing a refresh is not the active-context commit.  The
                # pointer remains on the prior admitted parent until the
                # queued consumer applies the refresh.
                self.assertEqual(mission_pointer_path.read_bytes(), before_mission_pointer)
                self.assertEqual(result["dataRevisionId"], "D-0002")
        finally:
            helper.tearDown()

    def test_cross_draft_handoff_recovery_preserves_same_d_and_successor_plans(self) -> None:
        """A crashed D2 handoff is recovered before either D2 or D3 takeover."""

        from apps.control_center_operational.tests.test_launch import FakeRunner, LaunchTests
        from apps.control_center_operational.projection import OperationalRepository

        helper = LaunchTests()
        helper.setUp()
        try:
            def start_run(root: Path):
                runner = FakeRunner()
                settings = helper.settings(root, enabled=True)
                repository = OperationalRepository(None, [settings.runs_root])
                manager = LaunchManager(settings, repository=repository, runner=runner)
                first = manager.prepare(
                    {
                        "mode": "new",
                        "projectName": "Cross-draft recovery",
                        "intakeBlocks": ["Initial requirement"],
                        "sources": [],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                created = manager.execute(
                    draft_id=first["draftId"],
                    fingerprint=first["fingerprint"],
                    confirmed=True,
                )
                return manager, repository, created, repository.list_runs()[0]["id"]

            def crash_after_revision_admission(manager: LaunchManager, draft: dict[str, object]) -> None:
                original = manager._admit_continue_data_refresh
                raised = False

                def fail_once(*args, **kwargs):
                    nonlocal raised
                    if not raised:
                        raised = True
                        raise LaunchConflictError("injected handoff crash")
                    return original(*args, **kwargs)

                manager._admit_continue_data_refresh = fail_once
                try:
                    with self.assertRaises(LaunchConflictError):
                        manager.execute(
                            draft_id=str(draft["draftId"]),
                            fingerprint=str(draft["fingerprint"]),
                            confirmed=True,
                        )
                finally:
                    manager._admit_continue_data_refresh = original

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "same-d").mkdir(parents=True)
                manager, repository, created, run_selector = start_run(root / "same-d")
                upload = manager.upload(
                    io.BytesIO(b"id,value\n2,3\n"),
                    filename="refresh.csv",
                    relative_path="refresh.csv",
                    content_length=len(b"id,value\n2,3\n"),
                )
                draft_a = manager.prepare(
                    {
                        "mode": "continue",
                        "runId": run_selector,
                        "intakeBlocks": ["A requirement"],
                        "sources": [{"kind": "upload", "uploadId": upload.upload_id}],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                crash_after_revision_admission(manager, draft_a)
                draft_b = manager.prepare(
                    {
                        "mode": "continue",
                        "runId": run_selector,
                        "intakeBlocks": ["B requirement"],
                        "sources": [],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                result = manager.execute(
                    draft_id=draft_b["draftId"],
                    fingerprint=draft_b["fingerprint"],
                    confirmed=True,
                )
                self.assertEqual(result["dataRevisionId"], "D-0002")
                self.assertTrue(result["pendingDataRefresh"])
                run_root = Path(created["runRoot"])
                pending = json.loads((run_root / "data_room" / "pending_data_refresh.json").read_text())
                self.assertEqual(pending["data_revision_id"], "D-0002")
                self.assertEqual(
                    [record["requirement_id"] for record in pending["plan"]["input_records"]],
                    ["REQ-001", "REQ-002", "REQ-003"],
                )
                self.assertFalse((run_root / "data_room" / "revision_transaction.json").exists())

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "successor").mkdir(parents=True)
                manager, repository, created, run_selector = start_run(root / "successor")
                upload_a = manager.upload(
                    io.BytesIO(b"id,value\n2,3\n"),
                    filename="refresh.csv",
                    relative_path="refresh.csv",
                    content_length=len(b"id,value\n2,3\n"),
                )
                upload_b = manager.upload(
                    io.BytesIO(b"a,b\n5,6\n"),
                    filename="more.csv",
                    relative_path="more.csv",
                    content_length=len(b"a,b\n5,6\n"),
                )
                draft_a = manager.prepare(
                    {
                        "mode": "continue",
                        "runId": run_selector,
                        "intakeBlocks": ["A requirement"],
                        "sources": [{"kind": "upload", "uploadId": upload_a.upload_id}],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                # Both drafts intentionally capture D-0001 before A advances
                # the pointer.  B must rebase its source snapshot onto A's
                # validated successor instead of taking a stale-CAS error.
                draft_b = manager.prepare(
                    {
                        "mode": "continue",
                        "runId": run_selector,
                        "intakeBlocks": ["B requirement"],
                        "sources": [{"kind": "upload", "uploadId": upload_b.upload_id}],
                        "maxAgents": 4,
                        "capacity": {"total": 4, "entityResolution": 2, "analyticalOwner": 1, "specialist": 1},
                    }
                )
                crash_after_revision_admission(manager, draft_a)
                run_root = Path(created["runRoot"])
                transaction = json.loads((run_root / "data_room" / "revision_transaction.json").read_text())
                result = manager.execute(
                    draft_id=draft_b["draftId"],
                    fingerprint=draft_b["fingerprint"],
                    confirmed=True,
                )
                self.assertEqual(result["dataRevisionId"], "D-0003")
                self.assertTrue(result["pendingDataRefresh"])
                pending = json.loads((run_root / "data_room" / "pending_data_refresh.json").read_text())
                self.assertEqual(pending["data_revision_id"], "D-0003")
                self.assertEqual(
                    [record["requirement_id"] for record in pending["plan"]["input_records"]],
                    ["REQ-001", "REQ-002", "REQ-003"],
                )
                self.assertEqual(pending["original_parent_generation_id"], transaction["expected_parent_generation_id"])
                self.assertEqual(pending["original_parent_state_hash"], transaction["expected_parent_state_hash"])
                self.assertEqual(pending["original_parent_plan_hash"], transaction["expected_parent_plan_hash"])
                with zipfile.ZipFile(run_root / "data_room" / "revisions" / "D-0003" / "archive.zip") as archive:
                    self.assertIn("refresh.csv", archive.namelist())
                    self.assertIn("more.csv", archive.namelist())
                self.assertFalse((run_root / "data_room" / "revision_transaction.json").exists())
        finally:
            helper.tearDown()


if __name__ == "__main__":
    unittest.main()
