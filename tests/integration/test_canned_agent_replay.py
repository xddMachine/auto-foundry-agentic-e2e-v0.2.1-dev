"""Acceptance tests for the zero-call analytical-role cassette harness."""

from __future__ import annotations

import hashlib
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from scripts import run_canned_agent_replay as replay


REPO = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fixture_is_readable_ordered_and_role_complete() -> None:
    fixture = replay.load_fixture()
    assert tuple(value["item_id"] for value in fixture["questions"]) == replay.QUESTION_IDS
    assert all(isinstance(value["text"], str) and value["text"].strip() for value in fixture["questions"])
    assert set(fixture["role_conclusions"]) == {
        "analytical_owner",
        "specialist",
        "business_reviewer",
        "integration",
        "fidelity_reviewer",
        "product",
        "optimizer",
    }
    assert fixture["questions"][1]["business_reviewer"]["verdict"] == "repair_once"
    assert fixture["questions"][1]["business_reviewer"]["target_sections"] == ["answer"]
    assert "pointer" not in fixture["questions"][1]["business_reviewer"]
    assert fixture["questions"][1]["targeted_business_reviewer"]["verdict"] == "repair_once"
    assert fixture["questions"][1]["second_targeted_business_reviewer"]["verdict"] == "accept_with_limits"
    assert fixture["questions"][2]["fidelity_reviewer"]["verdict"] == "repair_once"
    assert [len(value["specialists"]) for value in fixture["questions"]] == [0, 1, 2]
    assert replay._response(fixture["questions"][0], "analytical_owner")["replay_label"] == replay.REPLAY_LABEL
    assert replay._response(fixture["questions"][0], "analytical_owner")["model_calls"] == 0


def test_one_cycle_drives_real_core_product_optimizer_and_reporting(tmp_path: Path) -> None:
    cycle_root = tmp_path / "cycle-001"
    summary = replay.run_cycle(1, cycle_root, replay.load_fixture())

    assert summary["status"] == "complete_with_limits"
    assert summary["call_counters"] == {"agent_calls": 0, "model_calls": 0, "network_calls": 0}
    assert summary["cache"] == {"first": "miss", "second": "hit"}
    assert summary["catalog_entries"] == 1
    assert {probe["probe"] for probe in summary["fault_probes"]} == {
        "commit_before_fidelity",
        "conflicting_duplicate_invocation_id",
        "incomplete_fidelity_checked_ids",
        "offline_socket_connect",
        "stale_report_rejected",
    }
    assert all(probe["status"] == "PASS" for probe in summary["fault_probes"])
    assert len(summary["stable_digest"]) == 64
    assert summary["offline_guard_forbidden_attempts"] == 1
    expected_faults = {
        "offline_socket_connect": ("RuntimeError", "offline replay blocked external socket call"),
        "commit_before_fidelity": ("ValueError", "requires durable fidelity acceptance"),
        "incomplete_fidelity_checked_ids": ("ValueError", "checked_record_ids"),
        "conflicting_duplicate_invocation_id": ("ValueError", "invocation_id is already recorded"),
        "stale_report_rejected": ("ValueError", "outcome_counts are stale"),
    }
    for probe in summary["fault_probes"]:
        assert probe["rollback"] == "PASS"
        assert (probe["error"], probe["message"]) == expected_faults[probe["probe"]]

    q1, q2, q3 = summary["stage_facts"]
    assert q1["script_phases"] == ["NameError", "smoke", "full", "full"]
    assert [q1["specialist_count"], q2["specialist_count"], q3["specialist_count"]] == [0, 1, 2]
    assert all(value["analytical_owner"] == "complete_loop" for value in (q1, q2, q3))
    assert q1["business"] == "accept_with_limits"
    assert q2["business"] == "iterative_repairs_then_targeted_accept"
    assert q3["fidelity"] == "repair_once_then_targeted_accept"
    assert all(value["integration"] == "committed" for value in (q1, q2, q3))

    run_root = cycle_root / "run"
    archive = cycle_root / "input" / "synthetic_fixture.zip"
    archive_hash = _sha256(archive)
    assert archive.read_bytes()
    assert (run_root / "questions/Q-001/work/calculations/invalid.py").is_file()
    assert (run_root / "questions/Q-001/work/calculations/corrected.py").is_file()
    assert (run_root / "questions/Q-001/work/evidence.jsonl").is_file()
    assert not (run_root / "questions/Q-001/work/specialist_memos.jsonl").exists()
    assert (run_root / "questions/Q-002/work/specialist_memos.jsonl").is_file()
    q3_memos = (run_root / "questions/Q-003/work/specialist_memos.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(q3_memos) == 2
    assert (run_root / "questions/Q-001/accepted/manifest.json").is_file()
    assert (run_root / "questions/Q-002/accepted/manifest.json").is_file()
    assert (run_root / "questions/Q-003/accepted/manifest.json").is_file()
    q2_answer = json.loads((run_root / "questions/Q-002/accepted/answer_content.json").read_text(encoding="utf-8"))
    assert q2_answer["schema_version"] == "auto_foundry.analyst_answer.v1"
    assert "not a causal claim" in q2_answer["answer"]
    assert (run_root / "products/dashboard/index.html").is_file()
    assert (run_root / "products/dashboard/manifest.json").is_file()
    assert len(summary["stable_payload"]["dashboard_index_hash"]) == 64
    assert (run_root / "optimizer/optimizer_evidence_bundle.md").is_file()
    assert (run_root / "optimizer/optimizer_evidence_appendix.md").is_file()
    assert (run_root / "reporting/final_report.json").is_file()
    report = json.loads((run_root / "reporting/final_report.json").read_text(encoding="utf-8"))
    assert report["lifecycle_status"] == "complete_with_limits"
    assert report["receipt_count"] >= 16
    assert all(value["provider"] == "unavailable" for value in report["receipts"])
    assert all(value["model"] == "unavailable" for value in report["receipts"])
    business = {value["item_id"]: value for value in report["business_reviews"]}
    fidelity = {value["item_id"]: value for value in report["fidelity_reviews"]}
    assert business["Q-002"]["repair_count"] == 2
    assert business["Q-002"]["targeted_recheck_count"] == 2
    q2_repairs = business["Q-002"]["repairs"]
    q2_rechecks = business["Q-002"]["targeted_rechecks"]
    assert [value["repair_round"] for value in q2_repairs] == [1, 2]
    assert [value["recheck_round"] for value in q2_rechecks] == [1, 2]
    assert q2_repairs[0]["before_hash"] != q2_repairs[1]["before_hash"]
    assert q2_repairs[0]["after_hash"] == q2_rechecks[0]["draft_hash"]
    assert q2_repairs[1]["after_hash"] == q2_rechecks[1]["draft_hash"]
    assert all(value["review_scope"] == "full" for value in q2_repairs)
    assert all(value["review_scope"] == "targeted" for value in q2_rechecks)
    assert fidelity["Q-003"]["repair_count"] == 1
    assert fidelity["Q-003"]["targeted_recheck_count"] == 1
    assert fidelity["Q-003"]["record_pointer"] == "metric-q-003"
    matching_records = [
        value
        for value in summary["stable_payload"]["committed_records"]
        if value["kind"] == "metric" and value["payload"].get("metric_id") == "metric-q-003"
    ]
    assert len(matching_records) == 1
    actual_record_id = matching_records[0]["record_id"]
    assert fidelity["Q-003"]["findings"][0]["record_ids"] == [actual_record_id]
    assert _sha256(archive) == archive_hash
    assert not any(path.suffix == ".pyc" for path in cycle_root.rglob("*"))


def test_repeated_cycles_have_stable_digest_and_no_external_writes(tmp_path: Path) -> None:
    before = sorted(path.name for path in tmp_path.iterdir())
    fixture = replay.load_fixture()
    summaries = [replay.run_cycle(index, tmp_path / f"cycle-{index:03d}", fixture) for index in range(1, 4)]
    assert len({value["stable_digest"] for value in summaries}) == 1
    assert all(value["status"] == "complete_with_limits" for value in summaries)
    assert sorted(path.name for path in tmp_path.iterdir()) == before + ["cycle-001", "cycle-002", "cycle-003"]


def test_cli_emits_one_json_summary_and_retains_only_explicit_output_root(tmp_path: Path) -> None:
    output_root = tmp_path / "retained"
    command = [sys.executable, "-B", "scripts/run_canned_agent_replay.py", "--cycles", "3", "--output-root", str(output_root)]
    result = subprocess.run(
        command,
        cwd=REPO,
        env={**__import__("os").environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.count("\n") == 1
    summary = json.loads(result.stdout)
    assert summary["cycle_count"] == 3
    assert summary["deterministic"] is True
    assert summary["failures"] == []
    assert len(summary["cycles"]) == 3
    assert len({value["stable_digest"] for value in summary["cycles"]}) == 1
    assert sorted(path.name for path in output_root.iterdir()) == ["cycle-001", "cycle-002", "cycle-003"]


def test_fixture_conclusion_mutation_changes_digest(tmp_path: Path) -> None:
    fixture = replay.load_fixture()
    mutated = json.loads(json.dumps(fixture))
    mutated["questions"][0]["analytical_owner"]["conclusion"] += " changed"
    original = replay.run_cycle(1, tmp_path / "original", fixture)
    changed = replay.run_cycle(1, tmp_path / "changed", mutated)
    assert original["stable_digest"] != changed["stable_digest"]


def test_stable_payload_binds_persisted_records_reviews_and_dashboard(tmp_path: Path) -> None:
    summary = replay.run_cycle(1, tmp_path / "cycle-001", replay.load_fixture())
    stable = summary["stable_payload"]
    baseline = replay._semantic_hash(stable)
    assert stable["fixture_hash"] == replay._semantic_hash(replay.load_fixture())

    record_mutation = copy.deepcopy(stable)
    record = next(value for value in record_mutation["committed_records"] if value["kind"] == "metric")
    record["payload"]["value"] = 99
    record["payload_hash"] = "f" * 64
    assert replay._semantic_hash(record_mutation) != baseline

    report_path = tmp_path / "cycle-001" / "run" / "reporting" / "final_report.json"
    projection = replay._normalized_report_projection(report_path)
    q2_business = next(value for value in projection["business_reviews"] if value["item_id"] == "Q-002")
    assert q2_business["repairs"] and q2_business["targeted_rechecks"]
    repair_mutation = copy.deepcopy(stable)
    mutated_projection = copy.deepcopy(projection)
    mutated_q2 = next(value for value in mutated_projection["business_reviews"] if value["item_id"] == "Q-002")
    mutated_q2["repairs"][0]["allowed_pointers"].append("/answer/changed")
    repair_mutation["report_semantic_hash"] = replay._semantic_hash(mutated_projection)
    assert replay._semantic_hash(repair_mutation) != baseline

    dashboard_path = tmp_path / "cycle-001" / "run" / "products" / "dashboard" / "index.html"
    original_dashboard_hash = stable["dashboard_index_hash"]
    dashboard_path.write_text(dashboard_path.read_text(encoding="utf-8") + "<!-- semantic mutation -->\n", encoding="utf-8")
    changed_dashboard_hash = replay._file_semantic_hash(
        dashboard_path, run_root=tmp_path / "cycle-001" / "run"
    )
    assert changed_dashboard_hash != original_dashboard_hash
    dashboard_mutation = copy.deepcopy(stable)
    dashboard_mutation["dashboard_index_hash"] = changed_dashboard_hash
    assert replay._semantic_hash(dashboard_mutation) != baseline


def test_offline_guard_rejects_socket_attempt() -> None:
    with replay.no_external_call_guard() as guard:
        actions = [
            lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.01),
            lambda: socket.socket().connect(("127.0.0.1", 9)),
            lambda: socket.socket().connect_ex(("127.0.0.1", 9)),
            lambda: socket.socket().sendto(b"fixture", ("127.0.0.1", 9)),
        ]
        if hasattr(socket.socket, "sendmsg"):
            actions.append(lambda: socket.socket().sendmsg([b"fixture"], [], 0, ("127.0.0.1", 9)))
        for action in actions:
            with pytest.raises(RuntimeError, match="offline replay blocked external socket call"):
                action()
        assert guard.forbidden_attempts == len(actions)
        assert guard.completed_network_calls == 0


def test_call_counters_read_real_telemetry_facts(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    context = replay.RunContext("counter-test", tmp_path / "run", (inputs,))
    runtime = replay.CoreRuntime(context)
    runtime.telemetry.record(
        "agent.fixture",
        capability_id="agent.fixture",
        facts={"agent_calls": 2, "model_calls": 3, "network_calls": 4},
    )
    counters = replay._derive_call_counters(
        replay.InvocationReceiptLedger(context), runtime, replay._OfflineCallGuard()
    )
    assert counters == {"agent_calls": 2, "model_calls": 3, "network_calls": 4}


def test_cli_reports_digest_disagreement_as_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_cycle(cycle_index: int, root: Path, fixture: object) -> dict[str, object]:
        return {
            "cycle": cycle_index,
            "status": "complete_with_limits",
            "stable_digest": "a" * 64 if cycle_index == 1 else "b" * 64,
        }

    monkeypatch.setattr(replay, "run_cycle", fake_cycle)
    assert replay.main(["--cycles", "2", "--output-root", str(tmp_path / "digest-mismatch")]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["deterministic"] is False
    assert any(value["error_type"] == "DeterminismError" for value in summary["failures"])


def test_cli_reports_nonzero_external_call_counters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_cycle(cycle_index: int, root: Path, fixture: object) -> dict[str, object]:
        return {
            "cycle": cycle_index,
            "status": "complete_with_limits",
            "stable_digest": "a" * 64,
            "call_counters": {"agent_calls": 0, "model_calls": 1, "network_calls": 2},
        }

    monkeypatch.setattr(replay, "run_cycle", fake_cycle)
    assert replay.main(["--cycles", "2", "--output-root", str(tmp_path / "external-calls")]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["call_counters"] == {"agent_calls": 0, "model_calls": 2, "network_calls": 4}
    assert summary["deterministic"] is False
    assert any(value["error_type"] == "ExternalCallError" for value in summary["failures"])
