from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apps.control_center_operational.read_model import (
    DEFAULT_FIXTURE,
    ReadOnlyRepository,
    _find_telemetry,
    _jsonl_page,
    _normalize_event,
    _tail_jsonl,
)


class FixtureProjectionTests(unittest.TestCase):
    def test_fixture_preserves_required_agent_ownership(self) -> None:
        repository = ReadOnlyRepository(DEFAULT_FIXTURE, [])
        snapshot = repository.snapshot("fixture-mission")
        nodes = {node["id"]: node for node in snapshot["nodes"]}
        edges = snapshot["edges"]

        self.assertEqual(nodes["planner"]["role"], "planner")
        self.assertEqual(nodes["domain-customer"]["role"], "entity_resolution_owner")
        self.assertEqual(nodes["ao-2"]["role"], "analytical_owner")
        self.assertEqual(nodes["specialist-a"]["role"], "specialist")
        self.assertIn(
            {"id": "e7", "source": "ao-2", "target": "specialist-a", "kind": "ownership", "label": "delegated"},
            edges,
        )
        self.assertIn(
            {"id": "e1", "source": "planner", "target": "domain-customer", "kind": "ownership", "label": "dispatch"},
            edges,
        )
        self.assertNotIn(
            ("ao-1", "domain-customer"),
            {(edge["source"], edge["target"]) for edge in edges if edge["kind"] == "ownership"},
        )

    def test_fixture_models_shared_domain_as_dag(self) -> None:
        snapshot = ReadOnlyRepository(DEFAULT_FIXTURE, []).snapshot("fixture-mission")
        product_targets = {
            edge["target"]
            for edge in snapshot["edges"]
            if edge["source"] == "domain-product" and edge["kind"] == "dependency"
        }
        self.assertEqual(product_targets, {"ao-1", "ao-3"})
        self.assertEqual(snapshot["capacity"]["total"], 8)
        self.assertTrue(snapshot["capacity"]["plannerExcluded"])

    def test_fixture_cursors_follow_recorded_chronology(self) -> None:
        snapshot = ReadOnlyRepository(DEFAULT_FIXTURE, []).snapshot("fixture-mission")
        ordered = sorted(snapshot["events"], key=lambda event: event["cursor"])
        self.assertEqual(
            [event["timestamp"] for event in ordered],
            sorted(event["timestamp"] for event in ordered),
        )


class ReadOnlyFilesystemTests(unittest.TestCase):
    def test_discovers_temp_run_and_never_changes_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "example"
            run_dir.mkdir()
            state_path = run_dir / "run_state.json"
            state_payload = {
                "run_id": "RUN-TEMP",
                "status": "running",
                "updated_at": "2026-08-18T12:00:00Z",
                "items": [{"id": "Q-1"}],
            }
            state_path.write_text(json.dumps(state_payload), encoding="utf-8")
            before = state_path.read_bytes()

            repository = ReadOnlyRepository(None, [root])
            runs = repository.list_runs()
            self.assertEqual(len(runs), 1)
            self.assertTrue(runs[0]["readOnly"])
            self.assertTrue(runs[0]["protected"])
            snapshot = repository.snapshot(runs[0]["id"])

            self.assertEqual(snapshot["nodes"][0]["id"], "planner")
            self.assertEqual(snapshot["edges"], [])
            self.assertIn("attached read-only", snapshot["limitations"][0])
            self.assertEqual(state_path.read_bytes(), before)

    def test_normalized_event_reads_authoritative_facts_and_allowlists_details(self) -> None:
        event = _normalize_event(
            {
                "event_type": "data_room_member_read",
                "timestamp": "2026-08-18T12:00:00Z",
                "prompt": "private model prompt",
                "messages": [{"content": "private data"}],
                "facts": {
                    "member_path": "data/input.csv",
                    "format": "csv",
                    "rows": 42,
                    "role": "specialist",
                    "unknown_private_field": "must not leak",
                },
            },
            9,
            "stream-a",
        )
        self.assertEqual(event["category"], "file")
        self.assertEqual(event["path"], "data/input.csv")
        self.assertEqual(event["rows"], 42)
        self.assertEqual(event["role"], "specialist")
        self.assertEqual(event["id"], "event-stream-a-9")
        serialized = json.dumps(event["details"])
        self.assertNotIn("prompt", serialized)
        self.assertNotIn("messages", serialized)
        self.assertNotIn("private", serialized)

    def test_jsonl_tail_uses_stable_byte_offset_cursors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{"event_type":"first"}\n{"event_type":"second"}\n', encoding="utf-8")
            first_read = _tail_jsonl(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"event_type":"third"}\n')
            second_read = _tail_jsonl(path)

            self.assertEqual([cursor for cursor, _ in first_read], [cursor for cursor, _ in second_read[:2]])
            self.assertGreater(second_read[2][0], second_read[1][0])

    def test_jsonl_incremental_pages_do_not_drop_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                "".join(json.dumps({"event_type": f"event-{index}"}) + "\n" for index in range(5)),
                encoding="utf-8",
            )
            first, first_cursor, first_has_more = _jsonl_page(path, 0, max_items=2)
            second, second_cursor, second_has_more = _jsonl_page(path, first_cursor, max_items=2)
            third, _, third_has_more = _jsonl_page(path, second_cursor, max_items=2)

            self.assertEqual([item["event_type"] for _, item in first + second + third], [f"event-{index}" for index in range(5)])
            self.assertTrue(first_has_more)
            self.assertTrue(second_has_more)
            self.assertFalse(third_has_more)

    def test_partial_final_jsonl_line_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b'{"event_type":"first"}\n{"event_type":"sec')
            first, cursor, has_more = _jsonl_page(path, 0)
            self.assertEqual([item["event_type"] for _, item in first], ["first"])
            self.assertTrue(has_more)
            with path.open("ab") as handle:
                handle.write(b'ond"}\n')
            second, _, has_more = _jsonl_page(path, cursor)
            self.assertEqual([item["event_type"] for _, item in second], ["second"])
            self.assertFalse(has_more)

    def test_telemetry_symlink_outside_allowed_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            allowed = base / "allowed"
            run = allowed / "run"
            telemetry = run / "telemetry"
            telemetry.mkdir(parents=True)
            state_path = run / "run_state.json"
            state_path.write_text("{}", encoding="utf-8")
            outside = base / "outside.jsonl"
            outside.write_text('{"event_type":"secret"}\n', encoding="utf-8")
            (telemetry / "events.jsonl").symlink_to(outside)

            self.assertIsNone(_find_telemetry(state_path, (allowed.resolve(),)))

    def test_stream_reset_changes_identity_after_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            telemetry = run / "telemetry"
            telemetry.mkdir(parents=True)
            (run / "run_state.json").write_text(
                json.dumps({"run_id": "RUN", "status": "running"}), encoding="utf-8"
            )
            events_path = telemetry / "events.jsonl"
            events_path.write_text(
                '{"event_type":"first","facts":{"member_path":"first.csv"}}\n',
                encoding="utf-8",
            )
            repository = ReadOnlyRepository(None, [root])
            run_id = repository.list_runs()[0]["id"]
            snapshot = repository.snapshot(run_id)
            old_stream = snapshot["telemetry"]["streamId"]
            old_cursor = snapshot["telemetry"]["nextCursor"]
            old_event_id = snapshot["events"][0]["id"]

            events_path.write_text(
                json.dumps(
                    {
                        "event_type": "new",
                        "facts": {"member_path": "replacement/" + "x" * 160 + ".csv"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            page = repository.events_after(run_id, old_cursor, old_stream)

            self.assertTrue(page["reset"])
            self.assertNotEqual(page["streamId"], old_stream)
            self.assertNotEqual(page["events"][0]["id"], old_event_id)

    def test_existing_run_capacity_is_read_from_entity_resolution_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            extension = run / "extensions" / "G-0001"
            extension.mkdir(parents=True)
            (extension / "run_state.json").write_text(
                json.dumps({"run_id": "RUN", "status": "running"}), encoding="utf-8"
            )
            entity = run / "entity_resolution"
            entity.mkdir()
            (entity / "state.json").write_text(
                json.dumps(
                    {
                        "capacity": {
                            "total_active": 6,
                            "entity_resolution": 4,
                            "analytical_owner": 1,
                            "specialist": 3,
                        },
                        "leases": [
                            {"worker_type": "entity_resolution"},
                            {"worker_type": "specialist"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            control = run / "control_plane"
            control.mkdir()
            (control / "coordinator_state.json").write_text(
                json.dumps({
                    "active_dispatches": [{
                        "idempotency_key": "ao-dispatch",
                        "action": {"role": "analytical_owner", "subject_id": "REQ-001"},
                    }],
                }),
                encoding="utf-8",
            )
            lifecycle = run / "control_center"
            lifecycle.mkdir()
            (lifecycle / "lifecycle_events.jsonl").write_text(
                json.dumps({
                    "event_type": "agent_progress",
                    "invocation_id": "specialist-child",
                    "agent_type": "specialist",
                }) + "\n",
                encoding="utf-8",
            )
            run_summary = ReadOnlyRepository(None, [root]).list_runs()[0]

            self.assertEqual(run_summary["capacity"]["total"], 6)
            self.assertEqual(run_summary["capacity"]["active"], 4)
            self.assertEqual(run_summary["capacity"]["entityResolutionActive"], 1)
            self.assertEqual(run_summary["capacity"]["analyticalOwnerActive"], 1)
            self.assertEqual(run_summary["capacity"]["specialistActive"], 2)

            # A stopped run must not retain stale dispatches as live capacity.
            (extension / "run_state.json").write_text(
                json.dumps({"run_id": "RUN", "status": "paused"}), encoding="utf-8"
            )
            paused_summary = ReadOnlyRepository(None, [root]).list_runs()[0]
            self.assertEqual(paused_summary["capacity"]["active"], 0)

    def test_active_capacity_counts_every_coordinator_worker_but_not_planner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            (run / "run_state.json").write_text(
                json.dumps({"run_id": "RUN", "status": "running"}), encoding="utf-8"
            )
            entity = run / "entity_resolution"
            entity.mkdir()
            (entity / "state.json").write_text(
                json.dumps(
                    {
                        "capacity": {
                            "total_active": 8,
                            "entity_resolution": 4,
                            "analytical_owner": 1,
                            "specialist": 3,
                        },
                        "leases": [],
                    }
                ),
                encoding="utf-8",
            )
            control = run / "control_plane"
            control.mkdir()
            (control / "coordinator_state.json").write_text(
                json.dumps(
                    {
                        "active_dispatches": [
                            {
                                "idempotency_key": "review-product",
                                "action": {"role": "identity_reviewer", "subject_id": "product-domain"},
                            },
                            {
                                "idempotency_key": "novel-worker",
                                "action": {"role": "policy_evaluator", "subject_id": "policy-1"},
                            },
                            {
                                "idempotency_key": "planner",
                                "action": {"role": "planner", "subject_id": "mission"},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = ReadOnlyRepository(None, [root]).list_runs()[0]
            self.assertEqual(summary["capacity"]["active"], 2)
            self.assertEqual(summary["capacity"]["entityResolutionActive"], 1)
            self.assertEqual(summary["capacity"]["analyticalOwnerActive"], 0)
            self.assertEqual(summary["capacity"]["specialistActive"], 0)
            self.assertTrue(summary["capacity"]["plannerExcluded"])


if __name__ == "__main__":
    unittest.main()
