from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from apps.control_center_operational import projection as projection_module
from apps.control_center_operational.launch import LaunchManager
from apps.control_center_operational.projection import (
    MAX_DISCOVERED_PLACEHOLDERS,
    MAX_DISCOVERED_RUNS,
    MAX_REQUIREMENTS,
    MAX_TRACE,
    OperationalRepository,
    _read_verified_product_asset,
    parse_coordinator_line,
    parse_lifecycle_line,
)
from apps.control_center_operational.tests.test_projection_sidecars import (
    _repository,
    _run,
    _write_valid_product,
)
from auto_foundry_core.analytical_artifacts import DataProfileArtifact
from auto_foundry_core.lifecycle import RunLifecycle, _manifest_hash
from auto_foundry_core.workspace import RunContext


def _with_draft_fingerprint(value: dict[str, object]) -> dict[str, object]:
    unsigned = {key: item for key, item in value.items() if key not in {"fingerprint", "status"}}
    return {**value, "fingerprint": LaunchManager._fingerprint(unsigned)}


class ProjectionTests(unittest.TestCase):
    def run_fixture(self, root: Path) -> str:
        run = root / "run"
        (run / "entity_resolution").mkdir(parents=True)
        (run / "requirements" / "REQ-001" / "work").mkdir(parents=True)
        (run / "control_center").mkdir(parents=True)
        (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-PROJECTION", "status": "running", "updated_at": "2026-08-18T00:00:00Z", "item_ids": ["REQ-001"]}), encoding="utf-8")
        (run / "control_center" / "launch_manifest.json").write_text(json.dumps({"projectName": "Projection Project", "runId": "RUN-PROJECTION"}), encoding="utf-8")
        (run / "entity_resolution" / "state.json").write_text(json.dumps({
            "capacity": {"total_active": 4, "entity_resolution": 2, "analytical_owner": 1, "specialist": 1},
            "leases": [{"worker_type": "entity_resolution", "owner_ref": "identity_alpha"}],
            "domains": {"domain_customer": {"domain_id": "domain_customer", "resolution_owner": "identity_alpha", "discovered_by_item_id": "REQ-001", "state": "reserved", "reviewer_ref": "review_identity"}},
            "waits": {},
        }), encoding="utf-8")
        item = run / "requirements" / "REQ-001"
        (item / "item_state.json").write_text(json.dumps({"item_id": "REQ-001", "lifecycle_state": "work", "review": {"reviewer_ref": "review_owner", "status": "pending"}}), encoding="utf-8")
        (item / "work" / "analysis_owner.json").write_text(json.dumps({"owner_ref": "owner_REQ-001", "status": "active"}), encoding="utf-8")
        (item / "work" / "specialist_tasks.jsonl").write_text(json.dumps({"task_id": "specialist_quality", "specialty": "data_quality", "status": "completed", "question": "PRIVATE QUESTION"}) + "\n", encoding="utf-8")
        (item / "work" / "specialist_memos.jsonl").write_text(json.dumps({"task_id": "specialist_quality", "conclusion": "PRIVATE CONCLUSION"}) + "\n", encoding="utf-8")
        lifecycle = [
            {"type": "agent_completed", "task_name": "specialist_quality", "agent_type": "specialist", "event_id": "safe-1", "timestamp": "2026-08-18T00:00:01Z", "message": "PRIVATE MESSAGE", "prompt": "PRIVATE PROMPT"},
            {"type": "agent_spawn", "task_name": "owner_REQ-001", "agent_type": "analytical_owner", "event_id": "safe-2", "timestamp": "2026-08-18T00:00:02Z", "model_response": "PRIVATE RESPONSE"},
            {"type": "unknown_event", "task_name": "specialist_should_ignore", "event_id": "ignored"},
        ]
        (run / "control_center" / "lifecycle_events.jsonl").write_text("".join(json.dumps(value) + "\n" for value in lifecycle), encoding="utf-8")
        return run.name

    def test_projection_uses_only_actual_lifecycle_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_fixture(root)
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            self.assertEqual(repository.list_runs()[0]["name"], "Projection Project")
            snapshot = repository.snapshot(run_id)
            roles = {node["role"] for node in snapshot["nodes"]}
            self.assertEqual(roles, {"analytical_owner", "specialist"})
            self.assertEqual(snapshot["edges"], [])
            self.assertFalse(any(node["id"].startswith("identity:") for node in snapshot["nodes"]))
            self.assertFalse(any(node["id"].startswith("ao:") for node in snapshot["nodes"]))
            serialized = json.dumps(snapshot)
            for secret in ("PRIVATE MESSAGE", "PRIVATE PROMPT", "PRIVATE RESPONSE", "PRIVATE QUESTION", "PRIVATE CONCLUSION"):
                self.assertNotIn(secret, serialized)
            self.assertTrue(snapshot["trace"])
            self.assertIn("codex_lifecycle", serialized)

    def test_terminal_durable_agent_is_omitted_from_nodes_but_retained_in_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_fixture(root)
            run = root / "run"
            item = run / "requirements" / "REQ-002"
            (item / "work").mkdir(parents=True)
            (item / "item_state.json").write_text(json.dumps({"item_id": "REQ-002", "lifecycle_state": "complete"}), encoding="utf-8")
            (item / "work" / "analysis_owner.json").write_text(json.dumps({"owner_ref": "owner_REQ-002", "status": "completed"}), encoding="utf-8")
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            snapshot = repository.snapshot(run_id)
            self.assertFalse(any(node["id"] == "ao:owner_REQ-002" for node in snapshot["nodes"]))
            self.assertFalse(any(node.get("id") == "invocation:owner_REQ-002" for node in snapshot["nodes"]))
            self.assertFalse(any(span.get("nodeId") == "invocation:owner_REQ-002" for span in snapshot["trace"]))

    def test_lifecycle_events_use_their_own_incremental_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_fixture(root)
            run = root / "run"
            (run / "telemetry").mkdir()
            (run / "telemetry" / "events.jsonl").write_text(json.dumps({"type": "telemetry_secret", "facts": {"status": "active"}}) + "\n", encoding="utf-8")
            lifecycle_path = run / "control_center" / "lifecycle_events.jsonl"
            lifecycle_path.write_text("".join(json.dumps({"type": "agent_started", "task_name": f"specialist_page_{index}", "agent_type": "specialist", "event_id": f"page-{index}"}) + "\n" for index in range(305)), encoding="utf-8")
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            first = repository.events_after(run_id, 0, "")
            self.assertTrue(first["streamId"])
            self.assertTrue(first["hasMore"])
            self.assertEqual(len(first["events"]), 300)
            self.assertTrue(all(event["category"] == "lifecycle" for event in first["events"]))
            self.assertFalse(any(event.get("type") == "telemetry_secret" for event in first["events"]))
            second = repository.events_after(run_id, first["nextCursor"], first["streamId"])
            self.assertEqual(len(second["events"]), 5)
            self.assertFalse(second["hasMore"])
            lifecycle_path.write_text(json.dumps({"type": "agent_failed", "task_name": "specialist_rotated", "agent_type": "specialist", "event_id": "rotated"}) + "\n", encoding="utf-8")
            rotated = repository.events_after(run_id, second["nextCursor"], second["streamId"])
            self.assertTrue(rotated["reset"])
            self.assertEqual(rotated["events"][0]["details"]["eventId"], "rotated")

    def test_generation_run_states_are_grouped_and_project_from_authoritative_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "entity_resolution").mkdir(parents=True)
            (run / "extensions" / "G-0002").mkdir(parents=True)
            (run / "extensions" / "G-0003").mkdir(parents=True)
            (run / "entity_resolution" / "state.json").write_text(json.dumps({
                "capacity": {"total_active": 4, "entity_resolution": 2, "analytical_owner": 1, "specialist": 1},
                "leases": [{"worker_type": "entity_resolution", "owner_ref": "identity_from_root"}],
            }), encoding="utf-8")
            base = {"run_id": "RUN-GROUPED", "run_root": str(run), "mode": "requirement", "generation": 0, "status": "paused", "updated_at": "2026-08-18T00:00:00Z", "item_ids": ["REQ-001"]}
            g2 = {**base, "generation": 2, "status": "paused", "updated_at": "2026-08-18T00:00:02Z", "item_ids": ["REQ-001", "REQ-002"]}
            g3 = {**base, "generation": 3, "status": "running", "updated_at": "2026-08-18T00:00:03Z", "item_ids": ["REQ-001", "REQ-002", "REQ-003"]}
            (run / "run_state.json").write_text(json.dumps(base), encoding="utf-8")
            (run / "extensions" / "G-0002" / "run_state.json").write_text(json.dumps(g2), encoding="utf-8")
            (run / "extensions" / "G-0003" / "run_state.json").write_text(json.dumps(g3), encoding="utf-8")
            (run / "active_generation.json").write_text(json.dumps({"run_root": str(run), "generation_id": "G-0003", "state_ref": "extensions/G-0003/run_state.json"}), encoding="utf-8")
            repository = OperationalRepository(None, [root])
            summaries = repository.list_runs()
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["status"], "running")
            self.assertEqual(summaries[0]["requirementCount"], 3)
            stable_id = summaries[0]["id"]
            self.assertEqual(stable_id, repository.list_runs()[0]["id"])
            snapshot = repository.snapshot(stable_id)
            self.assertFalse(any(node["id"] == "identity:identity_from_root" for node in snapshot["nodes"]))
            self.assertEqual(repository._run_root(stable_id)[1], run.resolve())

    def test_launch_placeholder_is_discoverable_and_dedupes_to_durable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            state_root = root / "state"
            run_root = runs / "RUN-PLACEHOLDER"
            drafts = state_root / "drafts"
            statuses = state_root / "statuses"
            drafts.mkdir(parents=True)
            statuses.mkdir(parents=True)
            draft = _with_draft_fingerprint({
                "schemaVersion": 2,
                "draftId": "D-placeholder",
                "mode": "new",
                "projectName": "Semantic intake project",
                "intakeBlocks": [{"blockId": "INPUT-001", "text": "requirements"}],
                "runId": "RUN-PLACEHOLDER",
                "runRoot": str(run_root),
                "createdAt": "2026-08-20T12:00:00Z",
                "status": "prepared",
            })
            (drafts / "D-placeholder.json").write_text(json.dumps(draft), encoding="utf-8")
            (statuses / "D-placeholder.json").write_text(json.dumps({
                "draftId": "D-placeholder",
                "fingerprint": draft["fingerprint"],
                "status": "starting",
                "runId": "RUN-PLACEHOLDER",
                "runRoot": str(run_root),
                "startedAt": "2026-08-20T12:00:01Z",
                "message": "Interpreting requirements",
            }), encoding="utf-8")
            repository = OperationalRepository(None, [runs], launch_state_root=state_root)
            summaries = repository.list_runs()
            self.assertEqual(len(summaries), 1)
            placeholder = summaries[0]
            self.assertEqual(placeholder["name"], "Semantic intake project")
            self.assertEqual(placeholder["status"], "starting")
            self.assertEqual(placeholder["requirementCount"], 0)
            self.assertEqual(placeholder["observedStage"], "semantic_intake")
            self.assertEqual(placeholder["startedAt"], "2026-08-20T12:00:01Z")
            self.assertTrue(placeholder["placeholder"])
            snapshot = repository.snapshot(placeholder["id"])
            self.assertEqual(snapshot["run"]["id"], placeholder["id"])
            self.assertEqual(snapshot["nodes"][0]["objective"], "Interpreting requirements")
            self.assertEqual(repository.events_after(placeholder["id"], 0)["events"], [])

            run_root.mkdir(parents=True)
            state_path = run_root / "run_state.json"
            state_path.write_text(json.dumps({
                "run_id": "RUN-PLACEHOLDER",
                "run_root": str(run_root),
            }), encoding="utf-8")
            summaries = repository.list_runs()
            self.assertEqual(len(summaries), 1)
            self.assertTrue(summaries[0].get("placeholder", False))
            self.assertEqual(summaries[0]["id"], placeholder["id"])

            # A path/run-id pair alone is not an authoritative successor.
            state_path.unlink()
            RunLifecycle.create(
                RunContext(run_id="RUN-PLACEHOLDER", run_root=run_root),
                ["REQ-001"],
                mode="requirement",
            )
            # Discovery is intentionally cached briefly for concurrent tabs;
            # force this fixture mutation past that window so the assertion
            # remains about identity reconciliation rather than cache timing.
            repository._records_cache = None
            summaries = repository.list_runs()
            self.assertEqual(len(summaries), 1)
            self.assertFalse(summaries[0].get("placeholder", False))
            self.assertEqual(summaries[0]["id"], placeholder["id"])
            self.assertEqual(summaries[0]["requirementCount"], 1)

    def test_records_cache_singleflight_and_returns_independent_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "run-a"
            run_root.mkdir(parents=True)
            (run_root / "run_state.json").write_text(
                json.dumps(
                    {
                        "run_id": "RUN-A",
                        "run_root": str(run_root),
                        "status": "paused",
                        "updated_at": "2026-08-30T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            repository = OperationalRepository(None, [root])
            seed = repository.records()
            run_id = seed[0].summary["id"]
            repository._records_cache = None

            calls: list[None] = []
            started = threading.Event()
            release = threading.Event()
            original_uncached = repository._records_uncached

            def uncached_once() -> list:
                calls.append(None)
                started.set()
                self.assertTrue(release.wait(timeout=5))
                return original_uncached()

            repository._records_uncached = uncached_once  # type: ignore[method-assign]
            operations = [repository.records, lambda: repository.get(run_id)] * 3
            with ThreadPoolExecutor(max_workers=len(operations)) as executor:
                futures = [executor.submit(operation) for operation in operations]
                self.assertTrue(started.wait(timeout=5))
                release.set()
                results = [future.result(timeout=10) for future in futures]

            self.assertEqual(len(calls), 1)
            records_result = next(result for result in results if isinstance(result, list))
            record_result = next(result for result in results if result is not None and not isinstance(result, list))
            self.assertIsNot(records_result, seed)
            self.assertIsNot(records_result[0], record_result)
            records_result[0].summary["status"] = "mutated"
            fresh = repository.get(run_id)
            self.assertIsNotNone(fresh)
            self.assertEqual(fresh.summary["status"], "paused")

    def test_records_cache_expires_and_discovers_new_durable_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_root = root / "run-a"
            first_root.mkdir(parents=True)
            (first_root / "run_state.json").write_text(
                json.dumps(
                    {
                        "run_id": "RUN-A",
                        "run_root": str(first_root),
                        "status": "paused",
                        "updated_at": "2026-08-30T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            repository = OperationalRepository(None, [root])
            calls: list[None] = []
            original_uncached = repository._records_uncached

            def counted_uncached() -> list:
                calls.append(None)
                return original_uncached()

            repository._records_uncached = counted_uncached  # type: ignore[method-assign]
            clock = [100.0]
            with patch.object(projection_module, "monotonic", side_effect=lambda: clock[0]):
                initial = repository.list_runs()
                self.assertEqual({summary["authoritativeRunId"] for summary in initial}, {"RUN-A"})

                successor_root = root / "run-successor"
                successor_root.mkdir(parents=True)
                (successor_root / "run_state.json").write_text(
                    json.dumps(
                        {
                            "run_id": "RUN-SUCCESSOR",
                            "run_root": str(successor_root),
                            "status": "running",
                            "updated_at": "2026-08-30T00:01:00Z",
                        }
                    ),
                    encoding="utf-8",
                )
                cached = repository.list_runs()
                self.assertEqual({summary["authoritativeRunId"] for summary in cached}, {"RUN-A"})

                clock[0] = 102.1
                refreshed = repository.list_runs()

            self.assertEqual(len(calls), 2)
            self.assertEqual(
                {summary["authoritativeRunId"] for summary in refreshed},
                {"RUN-A", "RUN-SUCCESSOR"},
            )

    def test_records_cache_expiry_is_anchored_to_scan_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "run-a"
            run_root.mkdir(parents=True)
            (run_root / "run_state.json").write_text(
                json.dumps(
                    {
                        "run_id": "RUN-A",
                        "run_root": str(run_root),
                        "status": "paused",
                        "updated_at": "2026-08-30T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            repository = OperationalRepository(None, [root])
            original_uncached = repository._records_uncached
            calls: list[None] = []
            clock = [100.0]

            def slow_uncached() -> list:
                calls.append(None)
                # Simulate a discovery that consumes most of the two-second
                # window before the result can be published.
                clock[0] = 101.5
                return original_uncached()

            repository._records_uncached = slow_uncached  # type: ignore[method-assign]
            with patch.object(projection_module, "monotonic", side_effect=lambda: clock[0]):
                repository.records()
                self.assertIsNotNone(repository._records_cache)
                expires_at = repository._records_cache[0]
                scan_start = 100.0
                self.assertEqual(expires_at, scan_start + projection_module.RECORDS_CACHE_TTL_SECONDS)
                self.assertLessEqual(expires_at, scan_start + projection_module.RECORDS_CACHE_TTL_SECONDS)
                clock[0] = 101.9
                repository.records()
                self.assertEqual(len(calls), 1)
                clock[0] = 102.1
                repository.records()

            self.assertEqual(len(calls), 2)

    def test_placeholder_discovery_sorts_before_bounded_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            state_root = root / "state"
            drafts = state_root / "drafts"
            drafts.mkdir(parents=True)
            newest_at = datetime(2027, 1, 1, tzinfo=timezone.utc)
            for index in range(MAX_DISCOVERED_PLACEHOLDERS):
                draft_id = f"D-{index:04d}"
                draft = _with_draft_fingerprint(
                    {
                        "draftId": draft_id,
                        "runId": f"RUN-{index:04d}",
                        "runRoot": str(runs / f"RUN-{index:04d}"),
                        "projectName": f"Older {index}",
                        "createdAt": (newest_at - timedelta(minutes=index + 1)).isoformat().replace("+00:00", "Z"),
                        "status": "prepared",
                    }
                )
                (drafts / f"{draft_id}.json").write_text(json.dumps(draft), encoding="utf-8")
            newest_id = "D-zzzz"
            newest = _with_draft_fingerprint(
                {
                    "draftId": newest_id,
                    "runId": "RUN-NEWEST",
                    "runRoot": str(runs / "RUN-NEWEST"),
                    "projectName": "Newest launch",
                    "createdAt": newest_at.isoformat().replace("+00:00", "Z"),
                    "status": "prepared",
                }
            )
            (drafts / f"{newest_id}.json").write_text(json.dumps(newest), encoding="utf-8")

            summaries = OperationalRepository(None, [runs], launch_state_root=state_root).list_runs()

            self.assertEqual(len(summaries), MAX_DISCOVERED_PLACEHOLDERS)
            self.assertEqual(summaries[0]["draftId"], newest_id)
            self.assertNotIn("D-0499", {summary["draftId"] for summary in summaries})

    def test_duplicate_placeholders_share_one_path_derived_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            state_root = root / "state"
            drafts = state_root / "drafts"
            drafts.mkdir(parents=True)
            run_root = runs / "RUN-SAME-ROOT"
            older = _with_draft_fingerprint(
                {
                    "draftId": "D-old",
                    "runId": "RUN-SAME-ROOT",
                    "runRoot": str(run_root),
                    "projectName": "Older launch",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "status": "prepared",
                }
            )
            newer = _with_draft_fingerprint(
                {
                    "draftId": "D-new",
                    "runId": "RUN-SAME-ROOT",
                    "runRoot": str(run_root),
                    "projectName": "Newer launch",
                    "createdAt": "2026-01-01T00:01:00Z",
                    "status": "prepared",
                }
            )
            (drafts / "D-old.json").write_text(json.dumps(older), encoding="utf-8")
            (drafts / "D-new.json").write_text(json.dumps(newer), encoding="utf-8")

            summaries = OperationalRepository(None, [runs], launch_state_root=state_root).list_runs()

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["draftId"], "D-new")
            self.assertEqual(summaries[0]["name"], "Newer launch")

    def test_durable_run_discovery_sorts_before_bounded_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            newest_at = datetime(2027, 1, 1, tzinfo=timezone.utc)
            for index in range(MAX_DISCOVERED_RUNS):
                run_root = root / f"run-{index:04d}"
                run_root.mkdir()
                (run_root / "run_state.json").write_text(
                    json.dumps(
                        {
                            "run_id": f"RUN-{index:04d}",
                            "status": "running",
                            "updated_at": (newest_at - timedelta(minutes=index + 1)).isoformat().replace("+00:00", "Z"),
                        }
                    ),
                    encoding="utf-8",
                )
            newest_root = root / "run-zzzz"
            newest_root.mkdir()
            (newest_root / "run_state.json").write_text(
                json.dumps(
                    {
                        "run_id": "RUN-NEWEST",
                        "status": "running",
                        "updated_at": newest_at.isoformat().replace("+00:00", "Z"),
                    }
                ),
                encoding="utf-8",
            )

            summaries = OperationalRepository(None, [root]).list_runs()

            self.assertEqual(len(summaries), MAX_DISCOVERED_RUNS)
            self.assertEqual(summaries[0]["authoritativeRunId"], "RUN-NEWEST")
            self.assertNotIn("RUN-0499", {summary["authoritativeRunId"] for summary in summaries})

    def test_authoritative_durable_state_beyond_generic_cap_replaces_placeholder(self) -> None:
        """A real run beyond the production cap still replaces its placeholder."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            state_root = root / "launch-state"
            drafts = state_root / "drafts"
            statuses = state_root / "statuses"
            drafts.mkdir(parents=True)
            statuses.mkdir(parents=True)

            target_root = runs / "run-target"
            target_context = RunContext("RUN-TARGET", target_root)
            lifecycle = RunLifecycle.create(
                target_context,
                [f"REQ-{index:03d}" for index in range(1, 10)],
                mode="requirement",
            )
            lifecycle.pause("paused for projection test")
            target_state_path = target_root / "run_state.json"
            target_state = json.loads(target_state_path.read_text(encoding="utf-8"))
            target_state["updated_at"] = "2026-01-01T00:00:00Z"
            target_state["manifest_hash"] = _manifest_hash(target_state)
            target_state_path.write_text(json.dumps(target_state), encoding="utf-8")

            # Fill the production discovery window with newer, parseable state
            # files.  These need not be lifecycle-authoritative; only the
            # target's validated state is eligible to replace the launch
            # placeholder.  Keep one more candidate than the real cap so the
            # target is outside the bounded history by timestamp.
            newest_at = datetime(2027, 1, 1, tzinfo=timezone.utc)
            for index in range(MAX_DISCOVERED_RUNS + 1):
                filler_root = runs / f"filler-{index:04d}"
                filler_root.mkdir(parents=True)
                (filler_root / "run_state.json").write_text(
                    json.dumps(
                        {
                            "run_id": f"RUN-FILLER-{index:04d}",
                            "run_root": str(filler_root),
                            "status": "running",
                            "updated_at": (
                                newest_at - timedelta(minutes=index)
                            ).isoformat().replace("+00:00", "Z"),
                        }
                    ),
                    encoding="utf-8",
                )

            draft = _with_draft_fingerprint(
                {
                    "draftId": "D-target",
                    "runId": "RUN-TARGET",
                    "runRoot": str(target_root),
                    "projectName": "Target launch",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "status": "failed",
                }
            )
            (drafts / "D-target.json").write_text(json.dumps(draft), encoding="utf-8")
            (statuses / "D-target.json").write_text(
                json.dumps(
                    {
                        "draftId": "D-target",
                        "fingerprint": draft["fingerprint"],
                        "status": "failed",
                        "runId": "RUN-TARGET",
                        "runRoot": str(target_root),
                        "updatedAt": "2026-01-01T00:00:01Z",
                        "message": "Launch failed before durable state was observed.",
                    }
                ),
                encoding="utf-8",
            )

            repository = OperationalRepository(None, [runs], launch_state_root=state_root)
            summaries = repository.list_runs()

            target_records = [
                summary
                for summary in summaries
                if summary.get("authoritativeRunId") == "RUN-TARGET"
            ]
            self.assertEqual(len(target_records), 1)
            self.assertEqual(target_records[0]["source"], "filesystem")
            self.assertFalse(target_records[0].get("placeholder", False))
            self.assertEqual(target_records[0]["status"], "paused")
            self.assertEqual(target_records[0]["requirementCount"], 9)
            self.assertEqual(len(summaries), MAX_DISCOVERED_RUNS)
            self.assertEqual(summaries[0]["authoritativeRunId"], "RUN-FILLER-0000")
            self.assertEqual(summaries[-1]["authoritativeRunId"], "RUN-TARGET")
            ids = [str(summary["id"]) for summary in summaries]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual([str(summary["id"]) for summary in repository.list_runs()], ids)

    def test_launch_manifest_discovery_sorts_before_bounded_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            launches = root / "control_center" / "launches"
            launches.mkdir(parents=True)
            newest_at = datetime(2027, 1, 1, tzinfo=timezone.utc)
            (root / "run_state.json").write_text(
                json.dumps(
                    {
                        "run_id": "RUN-MANIFESTS",
                        "status": "running",
                        "updated_at": newest_at.isoformat().replace("+00:00", "Z"),
                    }
                ),
                encoding="utf-8",
            )
            for index in range(MAX_REQUIREMENTS):
                child = launches / f"D-{index:04d}"
                child.mkdir()
                (child / "launch_manifest.json").write_text(
                    json.dumps(
                        {
                            "projectName": f"Older manifest {index}",
                            "createdAt": (newest_at - timedelta(minutes=index + 1)).isoformat().replace("+00:00", "Z"),
                        }
                    ),
                    encoding="utf-8",
                )
            newest = launches / "D-zzzz"
            newest.mkdir()
            (newest / "launch_manifest.json").write_text(
                json.dumps(
                    {
                        "projectName": "Newest manifest",
                        "createdAt": newest_at.isoformat().replace("+00:00", "Z"),
                    }
                ),
                encoding="utf-8",
            )

            summaries = OperationalRepository(None, [root.parent]).list_runs()

            self.assertEqual(summaries[0]["name"], "Newest manifest")

    def test_placeholder_rejects_mismatched_status_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            state_root = root / "state"
            run_root = runs / "RUN-BINDING"
            (state_root / "drafts").mkdir(parents=True)
            (state_root / "statuses").mkdir(parents=True)
            draft = _with_draft_fingerprint({
                "draftId": "D-binding",
                "runId": "RUN-BINDING",
                "runRoot": str(run_root),
                "projectName": "Binding check",
                "createdAt": "2026-08-20T12:00:00Z",
                "status": "prepared",
            })
            (state_root / "drafts" / "D-binding.json").write_text(json.dumps(draft), encoding="utf-8")
            mismatched = {**draft, "fingerprint": "c" * 64, "status": "starting", "startedAt": "2026-08-20T12:00:01Z"}
            (state_root / "statuses" / "D-binding.json").write_text(json.dumps(mismatched), encoding="utf-8")
            repository = OperationalRepository(None, [runs], launch_state_root=state_root)
            self.assertEqual(repository.list_runs(), [])

    def test_foreign_or_partial_run_state_does_not_hide_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            state_root = root / "state"
            run_root = runs / "RUN-FOREIGN"
            (state_root / "drafts").mkdir(parents=True)
            (state_root / "statuses").mkdir(parents=True)
            draft = _with_draft_fingerprint({
                "draftId": "D-foreign",
                "runId": "RUN-EXPECTED",
                "runRoot": str(run_root),
                "projectName": "Foreign state check",
                "createdAt": "2026-08-20T12:00:00Z",
                "status": "prepared",
            })
            (state_root / "drafts" / "D-foreign.json").write_text(json.dumps(draft), encoding="utf-8")
            (state_root / "statuses" / "D-foreign.json").write_text(json.dumps({
                **draft,
                "status": "starting",
                "startedAt": "2026-08-20T12:00:01Z",
            }), encoding="utf-8")
            run_root.mkdir(parents=True)
            state_path = run_root / "run_state.json"
            state_path.write_text(json.dumps({"run_id": "RUN-FOREIGN", "run_root": str(run_root), "status": "running"}), encoding="utf-8")
            repository = OperationalRepository(None, [runs], launch_state_root=state_root)
            summaries = repository.list_runs()
            self.assertEqual(len(summaries), 1)
            self.assertTrue(summaries[0]["placeholder"])

            state_path.write_text("{}", encoding="utf-8")
            summaries = repository.list_runs()
            self.assertEqual(len(summaries), 1)
            self.assertTrue(summaries[0]["placeholder"])

    def test_tampered_draft_fingerprint_is_not_projected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            state_root = root / "state"
            run_root = runs / "RUN-TAMPERED"
            (state_root / "drafts").mkdir(parents=True)
            (state_root / "statuses").mkdir(parents=True)
            draft = _with_draft_fingerprint({
                "draftId": "D-tampered",
                "runId": "RUN-TAMPERED",
                "runRoot": str(run_root),
                "projectName": "Original",
                "createdAt": "2026-08-20T12:00:00Z",
                "status": "prepared",
            })
            tampered = {**draft, "projectName": "Changed without re-preparing"}
            (state_root / "drafts" / "D-tampered.json").write_text(json.dumps(tampered), encoding="utf-8")
            (state_root / "statuses" / "D-tampered.json").write_text(json.dumps({
                **draft,
                "status": "starting",
                "startedAt": "2026-08-20T12:00:01Z",
            }), encoding="utf-8")
            repository = OperationalRepository(None, [runs], launch_state_root=state_root)
            self.assertEqual(repository.list_runs(), [])

    def test_symlinked_launch_state_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            real_state = root / "real-state"
            real_state.mkdir()
            symlink_state = root / "state-link"
            symlink_state.symlink_to(real_state, target_is_directory=True)
            repository = OperationalRepository(None, [runs], launch_state_root=symlink_state)
            self.assertEqual(repository.list_runs(), [])

    def test_unknown_lifecycle_shape_is_ignored_and_old_terminal_nodes_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_fixture(root)
            run = root / "run"
            with (run / "control_center" / "lifecycle_events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "agent_failed", "task_name": "reviewer_old", "agent_type": "reviewer", "event_id": "old", "timestamp": "2020-01-01T00:00:00Z"}) + "\n")
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            snapshot = repository.snapshot(run_id)
            old = next(node for node in snapshot["nodes"] if node["id"] == "invocation:reviewer_old")
            self.assertEqual(old["status"], "failed")
            self.assertTrue(old["visible"])
            self.assertTrue(any(span.get("eventId") == "old" for span in snapshot["trace"]))

    def test_parser_allowlists_prefix_and_drops_message_text(self) -> None:
        parsed = parse_lifecycle_line({"type": "agent_started", "task_name": "specialist_safe", "agent_type": "specialist", "event_id": "evt", "message": "secret"}, 3)
        self.assertEqual(parsed["role"], "specialist")
        self.assertNotIn("message", parsed)
        unknown = parse_lifecycle_line({"type": "agent_started", "task_name": "unknown_task", "agent_type": "mystery"}, 4)
        self.assertEqual(unknown["role"], "mystery")

    def test_coordinator_novel_role_is_stable_and_privacy_safe(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        dispatch = {
            "kind": "run_coordinator_event",
            "event": "dispatch_started",
            "seq": 1,
            "idempotency_key": "inv-novel",
            "action": "novel_phase",
            "subject_id": "REQ-9",
            "created_at": now,
            "payload": {"role": "novel_role", "reason": "PRIVATE", "metadata": {"prompt": "PRIVATE"}},
            "after_state": {"secret": "PRIVATE"},
        }
        completed = {
            **dispatch,
            "event": "role_completed",
            "seq": 2,
            "created_at": now,
            "payload": {"result": {"status": "success", "role": "novel_role", "subject_id": "REQ-9", "finished_at": now, "receipt": {"secret": "PRIVATE"}}},
        }
        self.assertEqual(parse_coordinator_line(dispatch, 0)["role"], "novel_role")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-COORD", "run_root": str(run), "status": "running"}), encoding="utf-8")
            (run / "control_plane" / "coordinator_events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in (dispatch, completed)), encoding="utf-8")
            repository = OperationalRepository(None, [root])
            snapshot = repository.snapshot(repository.list_runs()[0]["id"])
            node = next(node for node in snapshot["nodes"] if node["id"] == "invocation:inv-novel")
            self.assertEqual(node["role"], "novel_role")
            self.assertEqual(node["status"], "completed")
            self.assertTrue(any(edge["source"] == "planner" and edge["target"] == node["id"] for edge in snapshot["edges"]))
            serialized = json.dumps(snapshot)
            self.assertNotIn("PRIVATE", serialized)

    def test_concurrent_dispatches_use_payload_identity_and_show_each_subject(self) -> None:
        """A stale top-level envelope must not collapse concurrent workers."""

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        subjects = (
            "customer-domain",
            "procurement-order-domain",
            "product-domain",
            "sales-fulfillment-domain",
        )
        records = []
        for seq, subject in enumerate(subjects, 1):
            records.append({
                "kind": "run_coordinator_event",
                "event": "dispatch_started",
                "seq": seq,
                # Test 5 recorded the first dispatch in each top-level
                # envelope while the per-event payload remained correct.
                "idempotency_key": "stale-first-invocation",
                "action": "resolve_identity",
                "subject_id": "customer-domain",
                "created_at": now,
                "payload": {
                    "idempotency_key": f"inv-{seq}",
                    "action": {
                        "action": "resolve_identity",
                        "role": "entity_resolution_owner",
                        "subject_id": subject,
                    },
                },
            })

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "run_state.json").write_text(
                json.dumps({"run_id": "RUN-CONCURRENT", "run_root": str(run), "status": "running"}),
                encoding="utf-8",
            )
            (run / "control_plane" / "coordinator_events.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
            )
            repository = OperationalRepository(None, [root])
            snapshot = repository.snapshot(repository.list_runs()[0]["id"])
            workers = [node for node in snapshot["nodes"] if node["role"] == "entity_resolution_owner"]
            self.assertEqual(len(workers), 4)
            self.assertEqual({node["invocationId"] for node in workers}, {f"inv-{index}" for index in range(1, 5)})
            self.assertEqual({node["subjectId"] for node in workers}, set(subjects))
            self.assertEqual(
                {node["label"] for node in workers},
                {"Customer Domain", "Procurement Order Domain", "Product Domain", "Sales Fulfillment Domain"},
            )
            self.assertTrue(all("Resolve Identity" in node["objective"] for node in workers))
            self.assertEqual(sum(edge["source"] == "planner" for edge in snapshot["edges"]), 4)
            for subject in subjects:
                self.assertTrue(any(subject.replace("-", " ").title() in event["summary"] for event in snapshot["events"]))

    def test_current_state_distinguishes_live_dispatch_from_retained_history(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = [
            {
                "kind": "run_coordinator_event",
                "event": "dispatch_started",
                "seq": seq,
                "created_at": now,
                "payload": {
                    "idempotency_key": invocation_id,
                    "action": {
                        "action": "analyze_requirement",
                        "role": "analytical_owner",
                        "subject_id": subject,
                    },
                },
            }
            for seq, invocation_id, subject in (
                (1, "old-invocation", "REQ-001"),
                (2, "current-invocation", "REQ-002"),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "run_state.json").write_text(
                json.dumps({"run_id": "RUN-ACTIVE-STATE", "run_root": str(run), "status": "running"}),
                encoding="utf-8",
            )
            (run / "control_plane" / "coordinator_events.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
            )
            state_path = run / "control_plane" / "coordinator_state.json"
            state_path.write_text(
                json.dumps({
                    "status": "dispatching",
                    "phase": "analysis",
                    "active_dispatches": [{"idempotency_key": "current-invocation"}],
                }),
                encoding="utf-8",
            )
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            snapshot = repository.snapshot(run_id)
            nodes = {node["id"]: node for node in snapshot["nodes"]}
            self.assertEqual(nodes["planner"]["status"], "active")
            self.assertTrue(nodes["planner"]["active"])
            self.assertEqual(nodes["invocation:old-invocation"]["status"], "historical")
            self.assertFalse(nodes["invocation:old-invocation"]["active"])
            self.assertEqual(nodes["invocation:current-invocation"]["status"], "dispatching")
            self.assertTrue(nodes["invocation:current-invocation"]["active"])

            # The projection cache must notice a checkpoint-only state change,
            # even if the append-only event file is unchanged.
            state_path.write_text(
                json.dumps({
                    "status": "dispatching",
                    "phase": "analysis",
                    "active_dispatches": [{"idempotency_key": "old-invocation"}],
                }),
                encoding="utf-8",
            )
            refreshed = repository.snapshot(run_id)
            refreshed_nodes = {node["id"]: node for node in refreshed["nodes"]}
            self.assertTrue(refreshed_nodes["invocation:old-invocation"]["active"])
            self.assertEqual(refreshed_nodes["invocation:current-invocation"]["status"], "historical")

    def test_identity_owner_reviewer_and_commit_form_a_subject_sequence(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        steps = (
            ("owner-customer", "resolve_identity", "entity_resolution_owner"),
            ("review-customer", "review_identity_result", "identity_reviewer"),
            ("commit-customer", "commit_identity_result", "identity_reviewer"),
        )
        records = []
        for seq, (invocation_id, action, role) in enumerate(steps, 1):
            records.append({
                "kind": "run_coordinator_event",
                "event": "dispatch_started",
                "seq": seq,
                "created_at": now,
                "payload": {
                    "idempotency_key": invocation_id,
                    "action": {
                        "action": action,
                        "role": role,
                        "subject_id": "customer-domain",
                    },
                },
            })

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "run_state.json").write_text(
                json.dumps({"run_id": "RUN-IDENTITY-SEQUENCE", "run_root": str(run), "status": "running"}),
                encoding="utf-8",
            )
            (run / "control_plane" / "coordinator_events.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
            )
            repository = OperationalRepository(None, [root])
            snapshot = repository.snapshot(repository.list_runs()[0]["id"])
            nodes = {node["id"]: node for node in snapshot["nodes"]}
            self.assertEqual(nodes["invocation:owner-customer"]["label"], "Customer Domain")
            self.assertEqual(nodes["invocation:review-customer"]["label"], "Customer Domain Reviewer")
            self.assertEqual(nodes["invocation:commit-customer"]["label"], "Customer Domain Commit")
            edge_pairs = {(edge["source"], edge["target"], edge["kind"]) for edge in snapshot["edges"]}
            self.assertIn(("planner", "invocation:owner-customer", "dispatch"), edge_pairs)
            self.assertIn(("invocation:owner-customer", "invocation:review-customer", "review"), edge_pairs)
            self.assertIn(("invocation:review-customer", "invocation:commit-customer", "commit"), edge_pairs)
            self.assertNotIn(("planner", "invocation:review-customer", "dispatch"), edge_pairs)

    def test_transport_exit_terminalizes_real_invocation_and_overlays_waiting_status(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        dispatch = {
            "kind": "run_coordinator_event",
            "event": "dispatch_started",
            "seq": 1,
            "action": "analyze_requirement",
            "subject_id": "REQ-001",
            "created_at": now,
            "payload": {"idempotency_key": "inv-failed", "action": {"role": "analytical_owner"}},
        }
        role_exit = {
            **dispatch,
            "event": "role_exit",
            "seq": 2,
            "payload": {
                "idempotency_key": "inv-failed",
                "action": {"action": "analyze_requirement", "role": "analytical_owner", "subject_id": "REQ-001"},
                "transport": {"exit_code": 1, "timed_out": False, "error": "PRIVATE CONFIG ERROR"},
            },
        }
        parsed = parse_coordinator_line(role_exit, 2)
        self.assertEqual(parsed["role"], "analytical_owner")
        self.assertEqual(parsed["status"], "failed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "run_state.json").write_text(
                json.dumps({"run_id": "RUN-FAILED-TRANSPORT", "run_root": str(run), "status": "running"}),
                encoding="utf-8",
            )
            (run / "control_plane" / "coordinator_state.json").write_text(
                json.dumps({"status": "waiting", "phase": "waiting"}), encoding="utf-8"
            )
            (run / "control_plane" / "coordinator_events.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in (dispatch, role_exit)), encoding="utf-8"
            )
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            self.assertEqual(repository.list_runs()[0]["status"], "waiting")
            snapshot = repository.snapshot(run_id)
            self.assertEqual(snapshot["run"]["status"], "waiting")
            planner = next(node for node in snapshot["nodes"] if node["id"] == "planner")
            invocation = next(node for node in snapshot["nodes"] if node["id"] == "invocation:inv-failed")
            self.assertEqual(planner["status"], "waiting")
            self.assertEqual(invocation["role"], "analytical_owner")
            self.assertEqual(invocation["status"], "failed")
            serialized = json.dumps(snapshot)
            self.assertIn("role_exit", serialized)
            self.assertNotIn("PRIVATE CONFIG ERROR", serialized)

    def test_nested_unknown_role_parent_and_missing_parent_are_explicit_only(self) -> None:
        parent = parse_lifecycle_line({"type": "agent_started", "invocationId": "parent", "role": "root_role"}, 1)
        child = parse_lifecycle_line({"type": "agent_started", "invocationId": "child", "role": "future_role", "parentAgentId": "parent"}, 2)
        orphan = parse_lifecycle_line({"type": "agent_started", "invocationId": "orphan", "role": "future_role", "parentAgentId": "missing"}, 3)
        self.assertEqual(parent["role"], "root_role")
        self.assertEqual(child["parentId"], "parent")
        self.assertEqual(orphan["parentId"], "missing")

    def test_malformed_lifecycle_and_coordinator_events_fail_closed(self) -> None:
        self.assertIsNone(parse_lifecycle_line({"type": "agent_started", "invocationId": "bad id", "role": "future"}, 1))
        self.assertIsNone(parse_lifecycle_line({"type": "agent_progress", "invocationId": "ok", "role": "future", "progress": "not-a-number"}, 2))
        self.assertIsNone(parse_lifecycle_line({"type": "agent_progress", "event_id": "observation-only", "role": "future"}, 3))
        self.assertIsNone(parse_coordinator_line({"kind": "run_coordinator_event", "event": "dispatch_started", "seq": 0, "idempotency_key": "inv"}, 0))

    def test_dispatch_resume_and_terminal_result_share_one_node(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = [
            {"kind": "run_coordinator_event", "event": "dispatch_started", "seq": 1, "idempotency_key": "inv-resume", "action": "future_action", "payload": {"role": "future_role"}, "created_at": now},
            {"kind": "run_coordinator_event", "event": "dispatch_resumed", "seq": 2, "idempotency_key": "inv-resume", "action": "future_action", "payload": {"role": "future_role"}, "created_at": now},
            {"kind": "run_coordinator_event", "event": "role_diagnostic", "seq": 3, "idempotency_key": "inv-resume", "payload": {"result": {"status": "diagnostic", "role": "future_role", "finished_at": now, "diagnostic": {"private": "x"}}}, "created_at": now},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-RESUME", "run_root": str(run), "status": "running"}), encoding="utf-8")
            (run / "control_plane" / "coordinator_events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            repository = OperationalRepository(None, [root])
            snapshot = repository.snapshot(repository.list_runs()[0]["id"])
            nodes = [node for node in snapshot["nodes"] if node["id"] == "invocation:inv-resume"]
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]["status"], "failed")
            self.assertEqual(sum(edge["target"] == "invocation:inv-resume" for edge in snapshot["edges"]), 1)
            self.assertGreaterEqual(sum(span["nodeId"] == "invocation:inv-resume" for span in snapshot["trace"]), 3)

    def test_lifecycle_planner_role_is_not_a_synthetic_dispatcher(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = [
            {"type": "agent_progress", "invocationId": "planner-lifecycle", "role": "planner", "event_id": "planner-progress", "timestamp": now, "progress": 20},
            {"type": "agent_wait", "invocationId": "planner-lifecycle", "role": "planner", "event_id": "planner-wait", "timestamp": now},
            {"type": "agent_completed", "invocationId": "planner-lifecycle", "role": "planner", "event_id": "planner-complete", "timestamp": now},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_center").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-LIFECYCLE-PLANNER", "run_root": str(run), "status": "running"}), encoding="utf-8")
            (run / "control_center" / "lifecycle_events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            repository = OperationalRepository(None, [root])
            snapshot = repository.snapshot(repository.list_runs()[0]["id"])
            self.assertEqual([node["id"] for node in snapshot["nodes"]], ["invocation:planner-lifecycle"])
            self.assertEqual(snapshot["nodes"][0]["role"], "planner")
            self.assertFalse(any(node["id"] == "planner" for node in snapshot["nodes"]))
            self.assertFalse(any(edge["source"] == "planner" for edge in snapshot["edges"]))

    def test_trace_window_keeps_current_lifecycle_terminal_span(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "control_center").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-TRACE-WINDOW", "run_root": str(run), "status": "running"}), encoding="utf-8")
            coordinator = [
                {
                    "kind": "run_coordinator_event",
                    "event": "dispatch_started",
                    "seq": index,
                    "idempotency_key": f"trace-{index}",
                    "action": "future",
                    "payload": {"role": "future"},
                    "created_at": now,
                }
                for index in range(1, MAX_TRACE + 101)
            ]
            lifecycle_terminal = {
                "type": "agent_completed",
                "invocationId": "current-lifecycle-terminal",
                "role": "future_nested",
                "event_id": "current-lifecycle-terminal-event",
                "timestamp": now,
            }
            (run / "control_plane" / "coordinator_events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in coordinator), encoding="utf-8")
            (run / "control_center" / "lifecycle_events.jsonl").write_text(json.dumps(lifecycle_terminal) + "\n", encoding="utf-8")
            repository = OperationalRepository(None, [root])
            snapshot = repository.snapshot(repository.list_runs()[0]["id"])
            self.assertLessEqual(len(snapshot["trace"]), MAX_TRACE)
            self.assertTrue(any(span["eventId"] == "current-lifecycle-terminal-event" for span in snapshot["trace"]))
            self.assertTrue(any(node["id"] == "invocation:current-lifecycle-terminal" for node in snapshot["nodes"]))

    def test_combined_event_cursor_tracks_both_sources_and_rotates_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "control_center").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-CURSOR", "run_root": str(run), "status": "running"}), encoding="utf-8")
            coordinator = {"kind": "run_coordinator_event", "event": "dispatch_started", "seq": 1, "idempotency_key": "inv-cursor", "action": "future", "payload": {"role": "future"}}
            lifecycle = {"type": "agent_started", "invocationId": "child-cursor", "parentAgentId": "inv-cursor", "role": "future_child"}
            (run / "control_plane" / "coordinator_events.jsonl").write_text(json.dumps(coordinator) + "\n", encoding="utf-8")
            (run / "control_center" / "lifecycle_events.jsonl").write_text(json.dumps(lifecycle) + "\n", encoding="utf-8")
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            first = repository.events_after(run_id, 0, "")
            self.assertIn("control-center-coordinator=", first["streamId"])
            self.assertIn("control-center-lifecycle=", first["streamId"])
            self.assertEqual({event["details"]["invocationId"] for event in first["events"]}, {"inv-cursor", "child-cursor"})
            (run / "control_center" / "lifecycle_events.jsonl").write_text(json.dumps({"type": "agent_failed", "invocationId": "rotated", "role": "future_child"}) + "\n", encoding="utf-8")
            rotated = repository.events_after(run_id, first["nextCursor"], first["streamId"])
            self.assertTrue(rotated["reset"])
            self.assertIn("rotated", {event["details"]["invocationId"] for event in rotated["events"]})

    def test_snapshot_uses_latest_bounded_tail_for_large_invocation_stream(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-TAIL", "run_root": str(run), "status": "running"}), encoding="utf-8")
            filler = "PRIVATE-STREAM-FILLER" * 140
            records = [{
                "kind": "run_coordinator_event",
                "event": "dispatch_started",
                "seq": 1,
                "idempotency_key": "early-important",
                "action": "early",
                "payload": {"role": "early_role"},
                "created_at": now,
            }]
            records.extend(
                {
                    "kind": "run_coordinator_event",
                    "event": "dispatch_started",
                    "seq": index,
                    "idempotency_key": "filler",
                    "action": "old",
                    "payload": {"role": "old", "metadata": filler},
                }
                for index in range(2, 1301)
            )
            records.extend(
                [
                    {"kind": "run_coordinator_event", "event": "dispatch_started", "seq": 1301, "idempotency_key": "latest", "action": "latest", "payload": {"role": "future", "metadata": filler}, "created_at": now},
                    {"kind": "run_coordinator_event", "event": "role_completed", "seq": 1302, "idempotency_key": "latest", "payload": {"result": {"status": "success", "role": "future", "finished_at": now, "metadata": filler}}, "created_at": now},
                ]
            )
            path = run / "control_plane" / "coordinator_events.jsonl"
            path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            self.assertGreater(path.stat().st_size, 2 * 1024 * 1024)
            repository = OperationalRepository(None, [root])
            snapshot = repository.snapshot(repository.list_runs()[0]["id"])
            node = next(node for node in snapshot["nodes"] if node["id"] == "invocation:latest")
            self.assertEqual(node["status"], "completed")
            self.assertTrue(any(node["id"] == "invocation:early-important" for node in snapshot["nodes"]))
            self.assertNotIn("PRIVATE-STREAM-FILLER", json.dumps(snapshot))

    def test_mixed_stream_pages_do_not_skip_valid_events_truncated_from_first_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "control_center").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-MIXED-PAGE", "run_root": str(run), "status": "running"}), encoding="utf-8")
            coordinator = [
                {"kind": "run_coordinator_event", "event": "dispatch_started", "seq": index, "idempotency_key": f"coord-{index}", "action": "future", "payload": {"role": "future"}}
                for index in range(1, 351)
            ]
            lifecycle = [
                {"type": "agent_started", "invocationId": f"life-{index}", "role": "future"}
                for index in range(1, 351)
            ]
            (run / "control_plane" / "coordinator_events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in coordinator), encoding="utf-8")
            (run / "control_center" / "lifecycle_events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in lifecycle), encoding="utf-8")
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            cursor, stream = 0, ""
            seen: set[str] = set()
            for _ in range(5):
                page = repository.events_after(run_id, cursor, stream)
                seen.update(event["details"]["invocationId"] for event in page["events"])
                cursor, stream = page["nextCursor"], page["streamId"]
                if not page["hasMore"]:
                    break
            self.assertEqual(len(seen), 700)

    def test_combined_cursor_is_opaque_and_exact_beyond_browser_number_precision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "control_plane").mkdir(parents=True)
            (run / "control_center").mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"run_id": "RUN-OPAQUE", "run_root": str(run), "status": "running"}), encoding="utf-8")
            padding = "x" * 3000
            coordinator = [
                {"kind": "run_coordinator_event", "event": "dispatch_started", "seq": index, "idempotency_key": f"opaque-{index}", "action": "future", "payload": {"role": "future", "padding": padding}}
                for index in range(1, 401)
            ]
            lifecycle = [{"type": "agent_started", "invocationId": "opaque-child", "role": "future_child"}]
            (run / "control_plane" / "coordinator_events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in coordinator), encoding="utf-8")
            (run / "control_center" / "lifecycle_events.jsonl").write_text(json.dumps(lifecycle[0]) + "\n", encoding="utf-8")
            repository = OperationalRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            first = repository.events_after(run_id, 0, "")
            opaque_cursor = first["nextCursor"]
            self.assertIsInstance(opaque_cursor, str)
            self.assertGreater(int(opaque_cursor), 2**53)
            round_tripped = json.loads(json.dumps(opaque_cursor))
            page = repository.events_after(run_id, round_tripped, first["streamId"])
            seen = {event["details"]["invocationId"] for event in first["events"]}
            seen.update(event["details"]["invocationId"] for event in page["events"])
            cursor, stream = page["nextCursor"], page["streamId"]
            for _ in range(8):
                if not page["hasMore"]:
                    break
                page = repository.events_after(run_id, cursor, stream)
                seen.update(event["details"]["invocationId"] for event in page["events"])
                cursor, stream = page["nextCursor"], page["streamId"]
            self.assertEqual(seen, {*(f"opaque-{index}" for index in range(1, 401)), "opaque-child"})

    def test_frontend_dispatcher_and_progress_render_contract(self) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        app_source = (repository_root / "apps" / "control_center_operational" / "static" / "app.js").read_text(encoding="utf-8")
        operational_source = (repository_root / "apps" / "control_center_operational" / "static" / "operational.js").read_text(encoding="utf-8")

        self.assertIn('node.active && node.id !== "planner"', app_source)
        self.assertIn('$("#metricActive").textContent = String(active);', app_source)
        self.assertIn('`${active}/${capacity.total}`', app_source)
        self.assertNotIn('capacity.active ?? active', app_source)
        self.assertIn('const currentIds = new Set(nodes.map((node) => node.id));', operational_source)
        self.assertNotIn('now - lastSeen < 12000', operational_source)
        self.assertNotIn('node.role === "planner" || node.active', operational_source)
        self.assertIn('snapshotOwnershipIsCurrent(state.selectedRunId, state.selectionGeneration)', operational_source)
        self.assertIn('snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration)', app_source)
        self.assertIn('function displayRunStatus(run)', app_source)
        self.assertIn('Interpreting requirements', app_source)
        self.assertIn('run.placeholder ? "Semantic intake"', app_source)
        self.assertIn('function operationalRefreshRuns()', operational_source)
        self.assertIn('function operationalPlaceholderIsOpen(run)', operational_source)
        refresh_needed_block = operational_source.split("function operationalRefreshIsNeeded()", 1)[1].split("function operationalMaybeStopRunsRefresh", 1)[0]
        self.assertIn("Boolean(selected?.placeholder)", refresh_needed_block)
        self.assertIn('await operationalRefreshRuns();', operational_source)
        self.assertIn('operationalBeginPendingLaunchRefresh();', operational_source)
        self.assertIn('operationalPendingLaunchTimer', operational_source)
        self.assertIn('if (operationalStatusPollInFlight) return;', operational_source)
        self.assertIn('if (generation === operationalStatusPollGeneration && !terminal)', operational_source)
        self.assertIn('if (!operationalLaunchRequestPending && !state.runs.some(operationalPlaceholderIsOpen))', operational_source)
        self.assertIn('operationalMaybeStopRunsRefresh()', operational_source)
        self.assertNotIn('operationalStatusTimer = window.setInterval(poll, 3000);', operational_source)
        submit_block = operational_source.split('validateDraft = async function operationalSubmit', 1)[1]
        self.assertLess(submit_block.index('operationalBeginPendingLaunchRefresh();'), submit_block.index('result = await operationalExecute();'))
        self.assertLess(
            operational_source.index('snapshotOwnershipIsCurrent(state.selectedRunId, state.selectionGeneration)'),
            operational_source.index('operationalNodeCache.set'),
        )

        progress_block = app_source.split("const progressValue = node.progress;", 1)[1].split("card.append", 1)[0]
        self.assertIn('if (typeof progressValue === "number" && Number.isFinite(progressValue))', progress_block)
        self.assertIn('foot.append(progress);', progress_block)
        self.assertNotIn('Number.isFinite(progressValue) ? `${Math.max(0, Math.min(100, progressValue))}%` : "0%"', progress_block)

        bootstrap_block = app_source.split("async function bootstrap()", 1)[1].split("function bindNavigation", 1)[0]
        self.assertLess(bootstrap_block.index("route();"), bootstrap_block.index("await selectRun("))

    def test_frontend_selection_race_cannot_commit_old_snapshot_or_cache(self) -> None:
        test_script = Path(__file__).with_name("test_selection_race.js")
        completed = subprocess.run(
            ["node", str(test_script)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("production selection race: OK", completed.stdout)

    def test_frontend_selected_failed_placeholder_refreshes_until_durable_successor(self) -> None:
        test_script = Path(__file__).with_name("test_operational_refresh.js")
        completed = subprocess.run(
            ["node", str(test_script)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("operational placeholder refresh: OK", completed.stdout)

    def test_frontend_operational_poll_guard_refreshes_only_when_needed(self) -> None:
        test_script = Path(__file__).with_name("test_operational_poll.js")
        completed = subprocess.run(
            ["node", str(test_script)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("operational poll guard: OK", completed.stdout)

    def test_frontend_graph_columns_are_bounded_and_reviewers_follow_owners(self) -> None:
        test_script = Path(__file__).with_name("test_graph_layout.js")
        completed = subprocess.run(
            ["node", str(test_script)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("bounded staged graph layout: OK", completed.stdout)

    def test_product_asset_reader_has_no_default_legacy_8mib_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.css"
            path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
            expected = hashlib.sha256(path.read_bytes()).hexdigest()

            self.assertEqual(_read_verified_product_asset(path, expected), path.read_bytes())
            self.assertIsNone(_read_verified_product_asset(path, expected, max_bytes=8 * 1024 * 1024))

    def test_product_projection_accepts_more_than_legacy_specialist_record_limit(self) -> None:
        artifacts = tuple(
            DataProfileArtifact(
                artifact_id=f"artifact-{index:04d}",
                requirement_id="REQ-001",
                dataset_fingerprint="b" * 64,
                source_refs=("data_room.zip",),
                population={"rows": 2},
                grain="row",
                period="2026",
                method="profile",
                profile={"columns": [{"name": "value", "dtype": "integer"}]},
                created_at="2026-08-30T00:00:00+00:00",
            )
            for index in range(513)
        )
        with tempfile.TemporaryDirectory() as directory:
            run_root = _run(Path(directory) / "RUN-SIDECAR")
            _write_valid_product(run_root, artifacts)
            repository, run_id = _repository(run_root)
            product = repository.snapshot(run_id)["productDashboard"]

        self.assertTrue(product["valid"])
        self.assertEqual(len(product["artifacts"]), 513)


if __name__ == "__main__":
    unittest.main()
