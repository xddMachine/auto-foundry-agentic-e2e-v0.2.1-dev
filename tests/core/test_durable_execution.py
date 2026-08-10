from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from auto_foundry_core.durable import (
    AcceptedSnapshot,
    ArtifactProgress,
    ExecutionAttempt,
    ITEM_STATE_FIELDS,
    ITEM_STATE_SCHEMA,
    ItemWorkspace,
    ProgressDecision,
)
from auto_foundry_core.lifecycle import AgentInvocationReceipt
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _workspace(tmp_path: Path, *, telemetry=None) -> ItemWorkspace:
    context = RunContext("RUN-EXECUTION", tmp_path / "run")
    return ItemWorkspace.create(context, "Q-001", original_text="Analyze the supplied evidence", telemetry=telemetry)


def _accepted_workspace(tmp_path: Path) -> ItemWorkspace:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    workspace.record_review("accept", reviewer_ref="review-1")
    workspace.accept(accepted_refs=("work/plan.json",))
    return workspace


def test_attempt_progress_decisions_are_deterministic_and_reloadable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    attempt = workspace.begin_attempt("lane-1", "Lead Analyst")
    assert attempt == ExecutionAttempt("A-001", "lane-1", "Lead Analyst", "lead", "active", attempt.baseline)
    assert attempt.attempt_id == "A-001"
    assert workspace.state["active_attempt_id"] == "A-001"

    first = workspace.observe_attempt("A-001")
    assert first.action == "materialize_now"
    assert first.changed_files == ()
    assert workspace.state["consecutive_no_progress"] == 1

    second = workspace.observe_attempt("A-001")
    assert second.action == "await_runtime"
    assert workspace.state["consecutive_no_progress"] == 2
    assert workspace.state["lifecycle_state"] == "work"

    workspace.write_plan({"objective": "bounded"})
    progressed = workspace.observe_attempt("A-001")
    assert progressed.action == "continue"
    assert "work/plan.json" in progressed.changed_files
    assert workspace.state["consecutive_no_progress"] == 0
    assert workspace.state["lifecycle_state"] == "work"

    reloaded = ItemWorkspace.load(workspace.context, "Q-001")
    assert reloaded.state["attempts"][0]["baseline"] == progressed.progress.to_dict()
    assert reloaded.observe_attempt("A-001").action == "materialize_now"


def test_recovery_is_separate_from_business_repair_and_preserves_handoff(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_handoff({"next": "resume from source map"})
    attempt = workspace.begin_attempt("lane-1", "Lead Analyst")
    workspace.observe_attempt(attempt.attempt_id)
    workspace.observe_attempt(attempt.attempt_id)

    receipt = AgentInvocationReceipt(
        "I-001", "Q-001", "Lead Analyst", "lead", start="2026-01-01T00:00:00Z",
        finish="2026-01-01T00:00:01Z", terminal_reason="process_lost",
    )
    recovery = workspace.begin_recovery(
        "lane-2", "Lead Analyst", prior_attempt_id=attempt.attempt_id, receipt=receipt
    )
    assert recovery.attempt_id == "A-002"
    assert recovery.route == "recovery"
    assert workspace.state["execution_recovery_count"] == 1
    assert workspace.state["business_repair_count"] == 0
    record = workspace.state["attempts"][-1]
    assert record["prior_attempt_id"] == "A-001"
    assert record["handoff_ref"] == "work/handoff.json"
    assert workspace.state["lifecycle_state"] == "recovering"

    with pytest.raises(ValueError, match="active work"):
        workspace.begin_recovery("lane-3", "Lead Analyst", prior_attempt_id="A-002")


def test_review_guards_repair_once_and_acceptance_are_immutable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(FileNotFoundError):
        workspace.record_review("accept")
    workspace.write_draft({"answer": "bounded", "limits": ["source-local"]})
    workspace.record_review("repair_once", reviewer_ref="review-1")
    assert workspace.state["review"]["verdict"] == "repair_once"
    workspace.use_business_repair()
    assert workspace.state["business_repair_count"] == 1
    with pytest.raises(ValueError, match="one business repair"):
        workspace.use_business_repair()
    with pytest.raises(ValueError, match="review"):
        workspace.accept()

    workspace.record_review("accept_with_limits", reviewer_ref="review-2")
    reviewed_draft = workspace.draft_root.read_bytes()
    reviewed_hash = hashlib.sha256(reviewed_draft).hexdigest()
    accepted = workspace.accept(accepted_refs=("work/findings.jsonl",))
    assert isinstance(accepted, AcceptedSnapshot)
    accepted_dir = workspace.accepted_root
    assert accepted_dir.is_dir()
    assert (accepted_dir / "answer_content.json").read_bytes() == reviewed_draft
    envelope = json.loads((accepted_dir / "acceptance_envelope.json").read_text(encoding="utf-8"))
    assert envelope["outcome"] == "accepted_with_limits"
    assert envelope["review_status"] == "reviewed"
    assert envelope["review_verdict"] == "accept_with_limits"
    manifest = json.loads((accepted_dir / "manifest.json").read_text(encoding="utf-8"))
    assert envelope["knowledge_delta"] == "no_change"
    assert envelope["accepted_refs"] == ["work/findings.jsonl"]
    assert manifest["content_hash"] == reviewed_hash
    assert workspace.state["lifecycle_state"] == "accepted"
    with pytest.raises(FileExistsError):
        workspace.accept()


def test_unavailable_review_disclosure_can_be_accepted_with_limits(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "useful partial"})
    workspace.record_review("not_reviewed", review_status="unavailable")
    assert workspace.state["review"] == {
        "status": "unavailable",
        "strength": "none",
        "verdict": "not_reviewed",
        "reviewer_ref": None,
        "draft_hash": hashlib.sha256(workspace.draft_root.read_bytes()).hexdigest(),
    }
    accepted = workspace.accept(knowledge_delta="promoted_with_limits")
    assert accepted.outcome == "accepted_with_limits"
    assert (workspace.accepted_root / "answer_content.json").is_file()


def test_limited_acceptance_reload_keeps_canonical_accepted_lifecycle(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "limited"})
    workspace.record_review("accept_with_limits", reviewer_ref="review-limited")
    accepted = workspace.accept()
    assert accepted.outcome == "accepted_with_limits"
    assert workspace.state["lifecycle_state"] == "accepted"
    assert workspace.state["terminal_outcome"]["status"] == "accepted_with_limits"
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["lifecycle_state"] == "accepted"
    assert reloaded.state["terminal_outcome"]["outcome"] == "accepted_with_limits"


def test_limited_acceptance_reconciles_after_directory_publish_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "limited"})
    workspace.record_review("accept_with_limits", reviewer_ref="review-limited")
    original_persist = workspace._persist_state
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected limited state persistence gap")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(workspace, "_persist_state", interrupted)
    with pytest.raises(OSError, match="limited state persistence gap"):
        workspace.accept()
    assert workspace.accepted_root.is_dir()
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["lifecycle_state"] == "accepted"
    assert reloaded.state["terminal_outcome"]["status"] == "accepted_with_limits"


def test_unavailable_limited_acceptance_reload_keeps_canonical_lifecycle(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "not independently reviewed"})
    workspace.record_review("not_reviewed", review_status="unavailable")
    workspace.accept()
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["lifecycle_state"] == "accepted"
    assert reloaded.state["terminal_outcome"]["status"] == "accepted_with_limits"


def test_technical_failure_requires_exhaustion_and_preserves_work(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_handoff({"next": "recover"})
    with pytest.raises(ValueError, match="recovery_exhausted"):
        workspace.technical_failure("still recoverable", recovery_exhausted=False)
    failure = workspace.technical_failure("recovery routes exhausted", recovery_exhausted=True)
    assert failure.outcome == "technical_failure"
    assert workspace.state["lifecycle_state"] == "technical_failure"
    assert workspace.state["terminal_outcome"]["status"] == "technical_failure"
    assert (workspace.work_root / "handoff.json").is_file()
    manifest = json.loads((workspace.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["outcome"] == "technical_failure"
    assert manifest["reason"] == "recovery routes exhausted"
    with pytest.raises(FileExistsError):
        workspace.technical_failure("rewrite", recovery_exhausted=True)


def test_attempt_dataclasses_and_telemetry_are_metadata_only(tmp_path: Path) -> None:
    context = RunContext("RUN-TELEMETRY", tmp_path / "run")
    telemetry = TelemetryRecorder(context=context)
    workspace = ItemWorkspace.create(context, "Q-002", original_text="PRIVATE ORIGINAL", telemetry=telemetry)
    workspace.begin_attempt("lane-1", "Lead Analyst", route="lead")
    workspace.observe_attempt("A-001")
    workspace.finish_attempt("A-001", status="completed", error="PRIVATE ROW ERROR")
    event_text = telemetry.event_path.read_text(encoding="utf-8")
    assert "PRIVATE ORIGINAL" not in event_text
    assert "PRIVATE ROW ERROR" not in event_text
    assert "Q-002" in event_text
    assert ProgressDecision("continue", ArtifactProgress((), {}, 0, 0, 0, 0, False)).to_dict()["action"] == "continue"
    assert ExecutionAttempt("A-001", "lane-1", "Lead Analyst", "lead", "active", ArtifactProgress((), {}, 0, 0, 0, 0, False)).to_dict()["attempt_id"] == "A-001"


def test_review_hash_rejects_direct_draft_mutation_and_writer_mutation_resets_review(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "before"})
    review = workspace.record_review("accept", reviewer_ref="review-1")
    assert review["draft_hash"] == hashlib.sha256(workspace.draft_root.read_bytes()).hexdigest()

    # A process-independent mutation must not let an unreviewed payload be
    # accepted under a stale review hash.
    workspace.draft_root.write_bytes(workspace.draft_root.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="exact currently reviewed draft"):
        workspace.accept()

    # The durable writer resets review and lifecycle in the same state write.
    workspace.write_draft({"answer": "after"})
    assert workspace.state["review"]["status"] == "pending"
    assert workspace.state["lifecycle_state"] == "work"
    with pytest.raises(ValueError, match="review"):
        workspace.accept()


def test_review_and_accept_require_no_active_attempt(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    attempt = workspace.begin_attempt("lane-1", "Lead Analyst")
    with pytest.raises(ValueError, match="no active attempt"):
        workspace.record_review("accept", reviewer_ref="review-1")
    workspace.finish_attempt(attempt.attempt_id, status="completed")
    workspace.record_review("accept", reviewer_ref="review-1")


def test_accept_reconciles_if_state_persistence_is_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    workspace.record_review("accept", reviewer_ref="review-1")
    original_persist = workspace._persist_state
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected state persistence gap")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(workspace, "_persist_state", interrupted)
    with pytest.raises(OSError, match="persistence gap"):
        workspace.accept()
    assert workspace.accepted_root.is_dir()

    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["lifecycle_state"] == "accepted"
    assert reloaded.state["terminal_outcome"]["content_hash"] == hashlib.sha256(
        (reloaded.accepted_root / "answer_content.json").read_bytes()
    ).hexdigest()
    assert original_persist is not None


def test_corrupt_terminal_snapshot_fails_closed_on_reload(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    workspace.record_review("accept", reviewer_ref="review-1")
    workspace.accept()
    manifest_path = workspace.accepted_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["content_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def test_terminal_state_rejects_all_work_and_draft_writers(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    workspace.record_review("accept", reviewer_ref="review-1")
    workspace.accept()
    writers = (
        lambda: workspace.write_plan({"late": True}),
        lambda: workspace.append_source_map({"late": True}),
        lambda: workspace.append_finding({"late": True}),
        lambda: workspace.write_open_issues({"late": True}),
        lambda: workspace.write_handoff({"late": True}),
        lambda: workspace.write_draft({"late": True}),
    )
    for writer in writers:
        with pytest.raises(ValueError, match="terminal"):
            writer()


def test_post_create_alias_replacement_is_rejected_on_every_boundary(tmp_path: Path) -> None:
    context = RunContext("RUN-SYMLINK", tmp_path / "run")
    first = ItemWorkspace.create(context, "Q-001", original_text="first")
    second = ItemWorkspace.create(context, "Q-002", original_text="second")
    item_path = context.run_root / "questions" / "Q-001"
    moved = context.run_root / "questions" / "Q-001-real"
    os.replace(item_path, moved)
    item_path.symlink_to(second.item_root, target_is_directory=True)
    with pytest.raises(AllowedRootError):
        first.write_plan({"not": "on alias"})
    with pytest.raises(AllowedRootError):
        first.artifact_progress()
    with pytest.raises(AllowedRootError):
        ItemWorkspace.load(context, "Q-001")


def test_state_is_deep_copied_and_schema_is_explicit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    attempt = workspace.begin_attempt("lane-1", "Lead Analyst")
    snapshot = workspace.state
    snapshot["attempts"][0]["baseline"]["files"].append("forged")
    snapshot["review"]["status"] = "reviewed"
    assert "forged" not in workspace.state["attempts"][0]["baseline"]["files"]
    assert workspace.state["review"]["status"] == "pending"
    assert tuple(workspace.state) == ITEM_STATE_FIELDS
    assert tuple(ITEM_STATE_SCHEMA["attempt_fields"]) == (
        "attempt_id",
        "lane_id",
        "role",
        "route",
        "status",
        "baseline",
        "prior_attempt_id",
        "handoff_ref",
        "error",
    )
    assert set(ITEM_STATE_SCHEMA["review_fields"]) == {
        "status",
        "strength",
        "verdict",
        "reviewer_ref",
        "draft_hash",
    }
    assert attempt.attempt_id == "A-001"


def test_terminal_manifest_schema_and_hash_bind_every_field(tmp_path: Path) -> None:
    workspace = _accepted_workspace(tmp_path)
    manifest_path = workspace.accepted_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {
        "item_id",
        "outcome",
        "content_path",
        "content_hash",
        "envelope_path",
        "envelope_hash",
        "hashes",
        "artifact_progress",
        "manifest_hash",
    }
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    expected_hash = hashlib.sha256((json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
    assert manifest["manifest_hash"] == expected_hash
    assert manifest["content_hash"] == hashlib.sha256(
        (workspace.accepted_root / "answer_content.json").read_bytes()
    ).hexdigest()
    assert manifest["envelope_hash"] == hashlib.sha256(
        (workspace.accepted_root / "acceptance_envelope.json").read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest.__setitem__("content_hash", "0" * 64),
        lambda manifest: manifest.__setitem__("envelope_hash", "0" * 64),
        lambda manifest: manifest["artifact_progress"].__setitem__("finding_count", 9),
        lambda manifest: manifest.__setitem__("manifest_hash", "0" * 64),
    ],
)
def test_manifest_mutations_fail_closed(tmp_path: Path, mutation) -> None:
    workspace = _accepted_workspace(tmp_path)
    manifest_path = workspace.accepted_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest|snapshot|hash|inconsistent"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


@pytest.mark.parametrize(
    "change",
    [
        lambda manifest: manifest.pop("envelope_hash"),
        lambda manifest: manifest.__setitem__("unexpected", True),
    ],
)
def test_manifest_missing_or_extra_fields_fail_closed(tmp_path: Path, change) -> None:
    workspace = _accepted_workspace(tmp_path)
    manifest_path = workspace.accepted_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    change(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def test_manifest_draft_content_mismatch_fails_closed(tmp_path: Path) -> None:
    workspace = _accepted_workspace(tmp_path)
    final_path = workspace.accepted_root / "answer_content.json"
    final_path.write_bytes(b'{"answer":"tampered"}\n')
    with pytest.raises(ValueError, match="hash"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def test_create_reconciles_crash_published_terminal_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    workspace.record_review("accept", reviewer_ref="review-1")
    original_persist = workspace._persist_state
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("state gap after publication")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(workspace, "_persist_state", interrupted)
    with pytest.raises(OSError, match="after publication"):
        workspace.accept()
    reconciled = ItemWorkspace.create(workspace.context, "Q-001", original_text="Analyze the supplied evidence")
    assert reconciled.state["lifecycle_state"] == "accepted"
    assert reconciled.state["terminal_intent"]["outcome"] == "accepted"


def test_crash_before_directory_clears_prepublication_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    workspace.record_review("accept", reviewer_ref="review-1")

    def interrupted(*args, **kwargs):
        raise OSError("publication gap")

    monkeypatch.setattr(workspace, "_publish_accepted_directory", interrupted)
    with pytest.raises(OSError, match="publication gap"):
        workspace.accept()
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["lifecycle_state"] == "review"
    assert reloaded.state["terminal_intent"] is None


def test_active_attempt_id_must_point_to_active_record(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = workspace.begin_attempt("lane-1", "Lead Analyst")
    workspace.finish_attempt(first.attempt_id, status="completed")
    workspace.begin_attempt("lane-2", "Lead Analyst")
    state_path = workspace.item_root / "item_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["active_attempt_id"] = "A-001"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="active attempt"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def test_accept_reads_and_publishes_one_exact_draft_buffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "original"})
    workspace.record_review("accept", reviewer_ref="review-1")
    draft = workspace.draft_root
    original_read = Path.read_bytes
    reads = 0

    def raced_read(path: Path) -> bytes:
        nonlocal reads
        if path == draft:
            reads += 1
            payload = original_read(path)
            path.write_bytes(b'{"answer":"replacement"}\n')
            return payload
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", raced_read)
    accepted = workspace.accept()
    assert reads == 1
    assert (workspace.accepted_root / "answer_content.json").read_bytes() == b'{"answer":"original"}\n'
    assert accepted.content_hash == hashlib.sha256(b'{"answer":"original"}\n').hexdigest()
