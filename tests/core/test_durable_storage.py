from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from auto_foundry_core.durable import ArtifactProgress, ItemWorkspace
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _context(tmp_path: Path) -> RunContext:
    return RunContext("RUN-DURABLE", tmp_path / "run")


def test_create_makes_workspace_and_authoritative_state_before_agent(tmp_path: Path) -> None:
    context = _context(tmp_path)
    workspace = ItemWorkspace.create(context, "Q-001", original_text="Count fulfilled lines")

    assert workspace.item_root == context.run_root / "questions/Q-001"
    assert workspace.work_root.is_dir()
    assert workspace.draft_root == workspace.item_root / "draft.json"
    assert workspace.accepted_root == workspace.item_root / "accepted.json"
    assert not workspace.accepted_root.exists()
    state_path = workspace.item_root / "item_state.json"
    assert state_path.is_file() and state_path.stat().st_size > 0
    assert json.loads(state_path.read_text(encoding="utf-8")) == workspace.state
    assert workspace.state == {
        "item_id": "Q-001",
        "mode": "question",
        "original_text": "Count fulfilled lines",
        "lifecycle_state": "work",
        "execution_recovery_count": 0,
        "business_repair_count": 0,
        "created_at": workspace.state["created_at"],
        "updated_at": workspace.state["updated_at"],
    }


def test_writes_progress_hashes_and_reload_survives(tmp_path: Path) -> None:
    context = _context(tmp_path)
    workspace = ItemWorkspace.create(context, "Q-002", original_text="Compare invoices")
    before = workspace.artifact_progress()
    assert before == ArtifactProgress((), {}, 0, 0, 0, 0, False)

    workspace.write_plan({"objective": "compare", "source_ids": ["src-1"]})
    workspace.append_source_map({"source_id": "src-1", "purpose": "invoice"})
    workspace.append_source_map({"source_id": "src-2", "purpose": "payments"})
    workspace.append_finding({"finding_id": "F-1", "claim": "source-local"})
    workspace.append_finding({"finding_id": "F-2", "claim": "coverage"})
    workspace.write_open_issues([{"issue": "missing approval"}])
    workspace.write_handoff({"next": "review", "finding_count": 2})
    calculations = workspace.work_root / "calculations"
    calculations.mkdir()
    (calculations / "reconcile.py").write_text("print('ok')\n", encoding="utf-8")
    workspace.write_draft({"answer": "source-local", "evidence": ["F-1"]})

    after = workspace.artifact_progress()
    assert after.materially_changed(before)
    assert after.files == (
        "draft.json",
        "work/calculations/reconcile.py",
        "work/findings.jsonl",
        "work/handoff.json",
        "work/open_issues.json",
        "work/plan.json",
        "work/source_map.json",
    )
    assert after.hashes["draft.json"] == hashlib.sha256(workspace.draft_root.read_bytes()).hexdigest()
    assert after.finding_count == 2
    assert after.source_map_count == 2
    assert after.script_count == 1
    assert after.draft_count == 1
    assert after.handoff_present
    assert json.loads((workspace.work_root / "source_map.json").read_text(encoding="utf-8"))[1]["source_id"] == "src-2"
    assert len((workspace.work_root / "findings.jsonl").read_text(encoding="utf-8").splitlines()) == 2

    loaded = ItemWorkspace.load(context, "Q-002")
    assert loaded.state == workspace.state
    assert loaded.artifact_progress() == after


def test_create_is_idempotent_but_rejects_mode_or_original_text_changes(tmp_path: Path) -> None:
    context = _context(tmp_path)
    first = ItemWorkspace.create(context, "R-001", mode="requirement", original_text="Need a trend")
    second = ItemWorkspace.create(context, "R-001", mode="requirement", original_text="Need a trend")
    assert second.state == first.state
    with pytest.raises(ValueError, match="mode"):
        ItemWorkspace.create(context, "R-001", mode="question", original_text="Need a trend")
    with pytest.raises(ValueError, match="original_text"):
        ItemWorkspace.create(context, "R-001", mode="requirement", original_text="Changed")
    with pytest.raises(ValueError, match="mode"):
        ItemWorkspace.load(context, "R-001", mode="question")


@pytest.mark.parametrize("item_id", ["../escape", "/absolute", "nested/item", "", ".", ".."])
def test_item_id_and_mode_boundaries_are_rejected(tmp_path: Path, item_id: str) -> None:
    context = _context(tmp_path)
    with pytest.raises(ValueError):
        ItemWorkspace.create(context, item_id, original_text="x")
    with pytest.raises(ValueError):
        ItemWorkspace.create(context, "Q-002", mode="unknown", original_text="x")


def test_namespace_and_item_symlink_escapes_are_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    context.run_root.mkdir()
    (context.run_root / "questions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AllowedRootError):
        ItemWorkspace.create(context, "Q-003", original_text="x")

    (context.run_root / "questions").unlink()
    (context.run_root / "questions").mkdir()
    (context.run_root / "questions/Q-004").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AllowedRootError):
        ItemWorkspace.create(context, "Q-004", original_text="x")
    assert not (outside / "item_state.json").exists()


def test_telemetry_is_passive_and_contains_metadata_only(tmp_path: Path) -> None:
    context = _context(tmp_path)
    telemetry = TelemetryRecorder(context=context)
    secret_rows = [{"customer": "SECRET-ROW"}]
    workspace = ItemWorkspace.create(context, "Q-005", original_text="Do not leak this question", telemetry=telemetry)
    workspace.write_plan({"rows": secret_rows})
    workspace.append_finding({"rows": secret_rows})
    workspace.artifact_progress()

    event_text = (telemetry.event_path or Path()).read_text(encoding="utf-8")
    assert "SECRET-ROW" not in event_text
    assert "Do not leak this question" not in event_text
    assert "item_id" in event_text and "Q-005" in event_text

    class BrokenTelemetry:
        def record(self, *args, **kwargs):
            raise RuntimeError("telemetry unavailable")

    resilient = ItemWorkspace.create(context, "Q-006", original_text="x", telemetry=BrokenTelemetry())
    resilient.write_handoff({"next": "continue"})
    assert resilient.work_root.joinpath("handoff.json").is_file()
