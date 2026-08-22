"""Acceptance tests for the zero-call requirement-mode cassette replay."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from scripts import run_canned_requirement_replay as replay


REPO = Path(__file__).resolve().parents[2]


def test_one_cycle_persists_parent_plan_runs_script_and_commits_typed_records(tmp_path: Path) -> None:
    summary = replay.run_cycle(1, tmp_path / "cycle-001", replay.load_fixture())
    assert summary["status"] == "integration_complete"
    assert summary["item_status"] == "accepted_with_limits"
    assert summary["plan_before_analysis_enforced"] is True
    assert summary["requirement_text"] == replay.REQUIREMENT_TEXT
    assert summary["plan_task_ids"] == ["T-FAT", "T-PRICE", "T-RATIO"]
    assert summary["plan_task_count"] == 3
    assert summary["call_counters"] == {"agent_calls": 0, "model_calls": 0, "network_calls": 0}
    assert summary["acceptance_immutable"] is True
    assert summary["script_phases"] == ["smoke", "full", "full"]
    result = summary["result"]
    assert result["ratio"]["value"] == 1.75
    assert result["ratio"]["unit"] == "percentage-points per EUR/kg"
    assert result["denominator"]["unit"] == "EUR/kg"
    assert result["population"]["row_count"] == 3
    assert len(summary["accepted_hash"]) == 64
    assert len(summary["integration_hash"]) == 64
    assert summary["record_ids"] and len(summary["record_ids"]) == 2

    run_root = tmp_path / "cycle-001" / "run"
    plan_path = run_root / "requirements/R-001/work/requirement_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["original_text"] == replay.REQUIREMENT_TEXT
    tasks = {value["task_id"]: value for value in plan["tasks"]}
    assert tasks["T-RATIO"]["dependencies"] == ["T-FAT", "T-PRICE"]
    assert (run_root / "requirements/R-001/work/analysis.json").is_file()
    assert (run_root / "requirements/R-001/accepted/manifest.json").is_file()
    committed = run_root / "requirements/R-001/integration/committed/records.jsonl"
    assert committed.is_file()
    records = [json.loads(line) for line in committed.read_text(encoding="utf-8").splitlines()]
    assert {value["kind"] for value in records} == {"metric", "dashboard_fact"}
    assert not (run_root / "requirements/T-FAT").exists()
    assert not any(path.suffix == ".pyc" for path in tmp_path.rglob("*"))


def test_three_cycles_are_deterministic_and_fresh(tmp_path: Path) -> None:
    fixture = replay.load_fixture()
    summaries = [replay.run_cycle(index, tmp_path / f"cycle-{index:03d}", fixture) for index in range(1, 4)]
    assert len({value["stable_digest"] for value in summaries}) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cycle-001", "cycle-002", "cycle-003"]


def test_fixture_mutation_changes_digest(tmp_path: Path) -> None:
    fixture = replay.load_fixture()
    mutated = copy.deepcopy(fixture)
    mutated["rows"][0]["milk_fat_percent"] = "4.5"
    original = replay.run_cycle(1, tmp_path / "original", fixture)
    changed = replay.run_cycle(1, tmp_path / "changed", mutated)
    assert original["stable_digest"] != changed["stable_digest"]


def test_plan_gate_and_malformed_plan_fail_closed(tmp_path: Path) -> None:
    fixture = replay.load_fixture()
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = replay._create_archive(input_root, fixture)
    context = replay.RunContext("RUN-PLAN-GATE", tmp_path / "run", (input_root,))
    replay.RunLifecycle.create(context, (replay.REQUIREMENT_ID,), mode="requirement")
    workbench = replay.DataRoomWorkbench(context, replay.DataAssetRef.from_path(archive))
    item = replay.ItemWorkspace.create(
        context,
        replay.REQUIREMENT_ID,
        mode="requirement",
        original_text=replay.REQUIREMENT_TEXT,
    )
    analyst = replay.AnalystWorkspace(
        replay.BoundAnalysisContext.create(context, replay.DataAssetRef.from_path(archive), item, workbench=workbench),
        owner_ref="owner-R-001",
    )
    with pytest.raises(ValueError, match="persisted semantic plan"):
        analyst.begin_analysis(objective="ratio", strategy="bounded")
    analyst.plan_requirement(replay._plan())
    plan_path = item.work_root / "requirement_plan.json"
    plan_path.write_text("{\"tasks\": []}", encoding="utf-8")
    with pytest.raises((TypeError, ValueError)):
        analyst.begin_analysis(objective="ratio", strategy="bounded")


def test_offline_guard_rejects_socket_attempt() -> None:
    with replay.no_external_call_guard() as guard:
        with pytest.raises(RuntimeError, match="offline replay blocked external socket call"):
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)
        assert guard.forbidden_attempts == 1
        assert guard.completed_network_calls == 0


def test_cli_emits_one_json_summary_for_three_cycles(tmp_path: Path) -> None:
    output_root = tmp_path / "retained"
    result = subprocess.run(
        [sys.executable, "-B", "scripts/run_canned_requirement_replay.py", "--cycles", "3", "--output-root", str(output_root)],
        cwd=REPO,
        env={**__import__("os").environ, "PYTHONPATH": ".:src", "PYTHONDONTWRITEBYTECODE": "1"},
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
    assert summary["call_counters"] == {"agent_calls": 0, "model_calls": 0, "network_calls": 0}
    assert sorted(path.name for path in output_root.iterdir()) == ["cycle-001", "cycle-002", "cycle-003"]
