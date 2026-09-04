from __future__ import annotations

import json
from pathlib import Path
import threading
import zipfile

import pytest

from auto_foundry_core import (
    AnalystWorkspace,
    BoundAnalysisContext,
    DataAssetRef,
    DataRoomWorkbench,
    ItemWorkspace,
    RequirementAnalysisPlan,
    RequirementAnalysisTask,
    RunContext,
)


def _workspace(tmp_path: Path, *, item_id: str = "R-REV", original_text: str = "Analyze supplied rows."):
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("rows.csv", "row_id,value\nR-1,10\n")
    context = RunContext(f"RUN-{item_id}", tmp_path / "run", (input_root,))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, item_id, mode="requirement", original_text=original_text)
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        workbench=workbench,
    )
    return AnalystWorkspace(bound, owner_ref=f"owner-{item_id}"), item


def _plan(intent: str, *, original_text: str = "") -> RequirementAnalysisPlan:
    return RequirementAnalysisPlan(
        tasks=(RequirementAnalysisTask(task_id="T-1", question="Measure supplied rows."),),
        synthesis_intent=intent,
        original_text=original_text,
    )


def _revision_paths(item: ItemWorkspace) -> list[Path]:
    return sorted((item.work_root / "requirement_plan_revisions").glob("rev-*.json"))


def test_revisions_are_append_only_and_exact_retry_is_idempotent(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)

    first = analyst.plan_requirement(_plan("first"))
    paths = _revision_paths(item)
    assert [path.name for path in paths] == ["rev-0001.json"]
    first_bytes = paths[0].read_bytes()
    record = json.loads(first_bytes)
    assert set(record) == {
        "kind",
        "schema",
        "item_id",
        "revision",
        "plan_payload",
        "plan_hash",
        "parent_plan_hash",
        "analysis_context_manifest_hash",
        "active_generation_id",
        "active_generation_manifest_hash",
        "created_at",
        "record_hash",
    }
    assert record["revision"] == 1
    assert record["parent_plan_hash"] is None
    assert record["active_generation_id"] is None
    assert record["active_generation_manifest_hash"] is None

    assert analyst.plan_requirement(first) == first
    assert len(_revision_paths(item)) == 1
    assert paths[0].read_bytes() == first_bytes

    second = analyst.plan_requirement(_plan("second"))
    third = analyst.plan_requirement(_plan("third"))
    assert (second.synthesis_intent, third.synthesis_intent) == ("second", "third")
    paths = _revision_paths(item)
    assert [path.name for path in paths] == ["rev-0001.json", "rev-0002.json", "rev-0003.json"]
    assert paths[0].read_bytes() == first_bytes
    assert analyst.brief().requirement_plan == third


def test_active_attempt_allows_revision_but_terminal_item_rejects_it(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path, item_id="R-ACTIVE")
    attempt = item.begin_attempt("ao", "Analytical Owner", route="requirement")
    analyst.plan_requirement(_plan("first"))
    analyst.plan_requirement(_plan("revised during active research"))
    item.finish_attempt(attempt.attempt_id, status="completed")
    item.technical_failure("terminal test", recovery_exhausted=True)
    with pytest.raises(ValueError, match="terminal"):
        analyst.plan_requirement(_plan("must be rejected"))


def test_full_chain_current_and_path_tamper_fail_closed(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path, item_id="R-TAMPER")
    analyst.plan_requirement(_plan("first"))
    analyst.plan_requirement(_plan("second"))
    revisions = item.work_root / "requirement_plan_revisions"
    first = revisions / "rev-0001.json"
    original = first.read_bytes()
    first.write_bytes(original.replace(b'"revision":1', b'"revision":9'))
    with pytest.raises(ValueError, match="revision"):
        analyst.brief()
    first.write_bytes(original)

    current = item.work_root / "requirement_plan.json"
    current_bytes = current.read_bytes()
    current.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="plan"):
        analyst.brief()
    current.write_bytes(current_bytes)

    (revisions / "rev-0003.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="fields|ordinals"):
        analyst.brief()
    (revisions / "rev-0003.json").unlink()

    symlink = revisions / "rev-0003.json"
    symlink.symlink_to(first)
    with pytest.raises(ValueError, match="non-regular|unexpected"):
        analyst.brief()


def test_staged_revision_recovers_without_replacing_immutable_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    analyst, item = _workspace(tmp_path, item_id="R-RECOVER")
    original_writer = item._write_json_artifact
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("current-plan failpoint")
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(item, "_write_json_artifact", fail_once)
    with pytest.raises(OSError, match="failpoint"):
        analyst.plan_requirement(_plan("first"))
    revision = item.work_root / "requirement_plan_revisions" / "rev-0001.json"
    revision_bytes = revision.read_bytes()
    assert not (item.work_root / "requirement_plan.json").exists()

    assert analyst.plan_requirement(_plan("first")).synthesis_intent == "first"
    assert revision.read_bytes() == revision_bytes
    assert len(_revision_paths(item)) == 1


def test_concurrent_different_plans_have_one_cas_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    analyst, item = _workspace(tmp_path, item_id="R-RACE")
    analyst.plan_requirement(_plan("base"))
    second = AnalystWorkspace(analyst.context, owner_ref="other-owner")
    barrier = threading.Barrier(2)
    original_head = AnalystWorkspace._published_requirement_plan_hash

    def synchronized_head(self):
        value = original_head(self)
        barrier.wait(timeout=5)
        return value

    monkeypatch.setattr(AnalystWorkspace, "_published_requirement_plan_hash", synchronized_head)
    outcomes: list[object] = []

    def publish(owner: AnalystWorkspace, intent: str) -> None:
        try:
            outcomes.append(owner.plan_requirement(_plan(intent)))
        except Exception as exc:  # noqa: BLE001 - assert the losing CAS below
            outcomes.append(exc)

    left = threading.Thread(target=publish, args=(analyst, "left"))
    right = threading.Thread(target=publish, args=(second, "right"))
    left.start()
    right.start()
    left.join(timeout=10)
    right.join(timeout=10)
    assert not left.is_alive() and not right.is_alive()
    assert sum(isinstance(value, RequirementAnalysisPlan) for value in outcomes) == 1
    assert sum(isinstance(value, ValueError) for value in outcomes) == 1
    assert len(_revision_paths(item)) == 2
    assert analyst.brief().requirement_plan.synthesis_intent in {"left", "right"}
