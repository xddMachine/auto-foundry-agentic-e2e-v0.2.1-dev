from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Mapping

import pytest

import auto_foundry_core.durable as durable_module
from auto_foundry_core.contracts import IncidentRecord
from auto_foundry_core.durable import (
    AcceptedSnapshot,
    ArtifactProgress,
    ExecutionAttempt,
    ITEM_STATE_FIELDS,
    ITEM_STATE_SCHEMA,
    ItemWorkspace,
    ProgressDecision,
)
from auto_foundry_core.lifecycle import AgentInvocationReceipt, InvocationReceiptLedger
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import AllowedRootError, RunContext


class _DiscardCrash(BaseException):
    """Simulated process loss that bypasses ordinary exception rollback."""


def _workspace(tmp_path: Path, *, telemetry=None) -> ItemWorkspace:
    context = RunContext("RUN-EXECUTION", tmp_path / "run")
    return ItemWorkspace.create(context, "Q-001", original_text="Analyze the supplied evidence", telemetry=telemetry)


def _accepted_workspace(tmp_path: Path) -> ItemWorkspace:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "bounded"})
    workspace.record_review("accept", reviewer_ref="review-1")
    workspace.accept(accepted_refs=("work/plan.json",))
    return workspace


def _discard_incident(
    *,
    item_id: str = "Q-001",
    admissible: bool = False,
    category: str = "reviewer_scope",
    source: str | None = "reviewer-scope-fixture",
) -> IncidentRecord:
    return IncidentRecord(
        incident_id="INC-REVIEW-SCOPE-001",
        category=category,
        disposition="discarded_invalid_scope",
        admissible=admissible,
        item_id=item_id,
        scope=("work/q001_result.json", "/answer"),
        source=source,
    )


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
    assert second.action == "retry_same_attempt"
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
        "I-001", "Q-001", attempt.attempt_id, attempt.lane_id, "Lead Analyst", "lead", start="2026-01-01T00:00:00Z",
        finish="2026-01-01T00:00:01Z", terminal_reason="process_lost",
    )
    receipt_ref = InvocationReceiptLedger(workspace.context).append(receipt)
    recovery = workspace.begin_recovery(
        "lane-2", "Lead Analyst", prior_attempt_id=attempt.attempt_id, receipt_ref=receipt_ref
    )
    assert recovery.attempt_id == "A-002"
    assert recovery.route == "recovery"
    assert workspace.state["execution_recovery_count"] == 1
    assert workspace.state["business_repair_count"] == 0
    record = workspace.state["attempts"][-1]
    assert record["prior_attempt_id"] == "A-001"
    assert record["handoff_ref"] == "work/handoff.json"
    assert record["recovery_receipt_ref"] == receipt_ref
    assert record["recovery_invocation_id"] == "I-001"
    assert len(record["recovery_receipt_hash"]) == 64
    assert workspace.state["lifecycle_state"] == "recovering"

    with pytest.raises(ValueError, match="active work"):
        workspace.begin_recovery("lane-3", "Lead Analyst", prior_attempt_id="A-002", receipt_ref=receipt_ref)


@pytest.mark.parametrize(
    ("mismatch", "item_id", "attempt_id", "lane_id", "role"),
    (
        ("item_id", "Q-OTHER", "A-001", "lane-1", "Lead Analyst"),
        ("attempt_id", "Q-001", "A-OTHER", "lane-1", "Lead Analyst"),
        ("lane_id", "Q-001", "A-001", "lane-other", "Lead Analyst"),
        ("role", "Q-001", "A-001", "lane-1", "Other Role"),
    ),
)
def test_recovery_requires_exact_prior_item_attempt_lane_and_role(
    tmp_path: Path,
    mismatch: str,
    item_id: str,
    attempt_id: str,
    lane_id: str,
    role: str,
) -> None:
    workspace = _workspace(tmp_path / mismatch)
    prior = workspace.begin_attempt("lane-1", "Lead Analyst")
    receipt = AgentInvocationReceipt(
        f"I-{mismatch}",
        item_id,
        attempt_id,
        lane_id,
        role,
        "lead",
        finish="2026-01-01T00:00:01Z",
        terminal_reason="process_lost",
    )
    receipt_ref = InvocationReceiptLedger(workspace.context).append(receipt)
    with pytest.raises(ValueError, match=mismatch):
        workspace.begin_recovery(
            "lane-2",
            "Recovery Agent",
            prior_attempt_id=prior.attempt_id,
            receipt_ref=receipt_ref,
        )
    assert workspace.state["execution_recovery_count"] == 0
    assert workspace.state["active_attempt_id"] == prior.attempt_id


def test_recovery_rejects_unpersisted_receipt_stale_attempt_and_duplicate_ledger_id(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    prior = workspace.begin_attempt("lane-1", "Lead Analyst")
    receipt = AgentInvocationReceipt(
        "I-UNPERSISTED",
        workspace.item_id,
        prior.attempt_id,
        prior.lane_id,
        prior.role,
        prior.route,
        finish="2026-01-01T00:00:01Z",
        terminal_reason="process_lost",
    )
    with pytest.raises(ValueError, match="stable|persisted"):
        workspace.begin_recovery("lane-2", "Recovery Agent", prior_attempt_id=prior.attempt_id, receipt_ref=receipt)

    ledger = InvocationReceiptLedger(workspace.context)
    receipt_ref = ledger.append(receipt)
    workspace.finish_attempt(prior.attempt_id, status="lost")
    current = workspace.begin_attempt("lane-current", "Lead Analyst")
    with pytest.raises(ValueError, match="current active"):
        workspace.begin_recovery("lane-2", "Recovery Agent", prior_attempt_id=prior.attempt_id, receipt_ref=receipt_ref)

    line = ledger.path.read_text(encoding="utf-8")
    ledger.path.write_text(line + line, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        InvocationReceiptLedger(workspace.context)
    assert workspace.state["execution_recovery_count"] == 0
    assert workspace.state["active_attempt_id"] == current.attempt_id


def test_recovery_state_and_ledger_tampering_fail_closed_after_reload(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    prior = workspace.begin_attempt("lane-1", "Lead Analyst")
    receipt = AgentInvocationReceipt(
        "I-TAMPER",
        workspace.item_id,
        prior.attempt_id,
        prior.lane_id,
        prior.role,
        prior.route,
        finish="2026-01-01T00:00:01Z",
        terminal_reason="process_lost",
    )
    ledger = InvocationReceiptLedger(workspace.context)
    receipt_ref = ledger.append(receipt)
    workspace.begin_recovery("lane-2", "Recovery Agent", prior_attempt_id=prior.attempt_id, receipt_ref=receipt_ref)

    state_path = workspace.item_root / "item_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["execution_recovery_count"] = 2
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="execution_recovery_count"):
        ItemWorkspace.load(workspace.context, workspace.item_id)

    state["execution_recovery_count"] = 1
    state["attempts"][-1]["recovery_receipt_hash"] = "f" * 64
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="receipt hash"):
        ItemWorkspace.load(workspace.context, workspace.item_id)

    # Restore state and tamper only the ledger payload; state reload still
    # fails because the append-only receipt hash no longer matches.
    state["attempts"][-1]["recovery_receipt_hash"] = ledger.record_hash(receipt_ref)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    line = json.loads(ledger.path.read_text(encoding="utf-8"))
    line["terminal_reason"] = "syntax_error"
    ledger.path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def test_review_guards_repair_once_and_acceptance_are_immutable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(FileNotFoundError):
        workspace.record_review("accept")
    workspace.write_draft({"answer": "bounded", "limits": ["source-local"]})
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[{"finding_id": "F-LIMITS", "pointers": ["/limits"], "semantic_categories": ["answer"]}],
    )
    assert workspace.state["review"]["verdict"] == "repair_once"
    workspace.use_business_repair(owner_ref="owner")
    assert workspace.state["business_repair_count"] == 1
    with pytest.raises(ValueError, match="repair_once review verdict"):
        workspace.use_business_repair(owner_ref="owner")
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


def test_repair_once_requires_structured_scope_and_targeted_review_failure_is_atomic(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": {"value": 1}, "unrelated": "keep"})
    with pytest.raises(ValueError, match="material finding"):
        workspace.record_review("repair_once", reviewer_ref="review-1")
    with pytest.raises(ValueError, match="wildcard"):
        workspace.record_review(
            "repair_once",
            reviewer_ref="review-1",
            findings=[{"finding_id": "F-WILDCARD", "pointers": ["*"], "semantic_categories": ["answer"]}],
        )

    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[{"finding_id": "F-VALUE", "pointers": ["/answer/value"], "semantic_categories": ["answer"]}],
    )
    workspace.use_business_repair(owner_ref="owner")
    state_path = workspace.item_root / "item_state.json"
    review_path = workspace.business_review_path
    state_before = state_path.read_bytes()
    review_before = review_path.read_bytes()
    # Bypass the normal writer only to simulate an externally corrupted draft;
    # the targeted re-review must fail before either authority is changed.
    workspace.draft_root.write_text(
        json.dumps({"answer": {"value": 2}, "unrelated": "changed"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside reviewed scope"):
        workspace.record_review("accept", reviewer_ref="review-2")
    assert state_path.read_bytes() == state_before
    assert review_path.read_bytes() == review_before


@pytest.mark.parametrize("mutation", ("change", "add", "remove"))
def test_use_business_repair_rejects_artifact_progress_drift_before_activation(
    tmp_path: Path,
    mutation: str,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_plan({"version": 1})
    workspace.write_draft({"answer": "initial"})
    workspace.record_review(
        "repair_once",
        reviewer_ref="reviewer",
        findings=[
            {
                "finding_id": "F-ARTIFACT-BASELINE",
                "pointers": ["/answer"],
                "semantic_categories": ["answer"],
            }
        ],
    )
    packet_path = workspace.business_review_path
    state_path = workspace.item_root / "item_state.json"
    packet_before = packet_path.read_bytes()
    state_before = state_path.read_bytes()
    plan_path = workspace.work_root / "plan.json"
    if mutation == "change":
        plan_path.write_bytes(b'{"version":2}\n')
    elif mutation == "add":
        (workspace.work_root / "drift-output.json").write_bytes(b'{"drift":true}\n')
    else:
        plan_path.unlink()

    with pytest.raises(ValueError, match="exact currently reviewed artifact progress"):
        workspace.use_business_repair(owner_ref="owner")
    assert packet_path.read_bytes() == packet_before
    assert state_path.read_bytes() == state_before
    assert workspace.state["business_repair_count"] == 0


def test_repair_scope_allows_dependency_mutation_removal_and_targeted_recheck(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "initial", "unrelated": "keep"})
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[
            {
                "finding_id": "F-DEPENDENCY",
                "pointers": ["/answer", "/evidence_refs"],
                "dependent_outputs": [
                    "work/evidence.jsonl",
                    "work/source_map.json",
                    "work/specialist_memos.jsonl",
                ],
                "semantic_categories": ["evidence"],
            }
        ],
    )
    workspace.use_business_repair(owner_ref="owner")

    # A separate dependency remains available for mutation coverage through
    # the evidence/source-map writer authorized by the semantic category.
    workspace.append_source_map({"source_id": "S-DEPENDENCY-REPAIRED", "support": "bounded"})

    # Removal of an authorized dependency that existed at review time is also
    # in scope.  The next draft write runs the scope check and must not reject
    # the missing source map.
    (workspace.work_root / "source_map.json").unlink()
    workspace.write_draft({"answer": "repaired", "unrelated": "keep"})

    review = workspace.record_review("accept", reviewer_ref="review-2")
    assert review["targeted_recheck"] is True
    assert workspace.state["review"]["verdict"] == "accept"
    assert json.loads(workspace.business_review_path.read_text(encoding="utf-8"))["targeted_recheck"] is True


def test_repair_scope_allows_item_local_artifacts_without_touching_sibling(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    sibling = ItemWorkspace.create(
        workspace.context,
        "Q-002",
        original_text="Analyze the sibling evidence",
    )
    sibling.write_open_issues({"sibling": "untouched"})
    sibling_before = (sibling.work_root / "open_issues.json").read_bytes()
    workspace.write_draft({"answer": "initial"})
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[
            {
                "finding_id": "F-DEPENDENCY",
                "pointers": ["/answer", "/evidence_refs"],
                "dependent_outputs": [
                    "work/evidence.jsonl",
                    "work/source_map.json",
                    "work/specialist_memos.jsonl",
                ],
                "semantic_categories": ["evidence"],
            }
        ],
    )
    workspace.use_business_repair(owner_ref="owner")
    workspace.write_open_issues({"unrelated": True})
    assert json.loads((workspace.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": True
    }
    assert (sibling.work_root / "open_issues.json").read_bytes() == sibling_before


def test_discard_business_review_preserves_repair_bytes_and_allows_clean_full_review(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "initial", "limits": []})
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[{"finding_id": "F-SCOPE", "pointers": ["/answer"], "semantic_categories": ["answer"]}],
    )
    workspace.use_business_repair(owner_ref="owner")
    workspace.write_draft({"answer": "repaired", "limits": []})
    draft_before = workspace.draft_root.read_bytes()
    packet_before = workspace.business_review_path.read_bytes()

    audit = workspace.discard_business_review(_discard_incident().to_dict())

    assert workspace.draft_root.read_bytes() == draft_before
    assert not workspace.business_review_path.exists()
    assert workspace.state["business_repair_count"] == 0
    assert workspace.state["review"] == workspace._pending_review()
    assert workspace.state["lifecycle_state"] == "work"
    audit_lines = workspace.business_review_discard_audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0]) == audit
    assert audit["discarded_packet_hash"] == hashlib.sha256(packet_before).hexdigest()

    clean = workspace.record_review(
        "repair_once",
        reviewer_ref="review-2",
        findings=[{"finding_id": "F-CLEAN", "pointers": ["/answer"], "semantic_categories": ["answer"]}],
    )
    assert clean["review_scope"] == "full"
    assert clean["targeted_recheck"] is False
    assert workspace.state["business_repair_count"] == 0
    workspace.use_business_repair(owner_ref="owner")
    assert workspace.state["business_repair_count"] == 1

    with pytest.raises(ValueError, match="already recorded"):
        workspace.discard_business_review(_discard_incident())


@pytest.mark.parametrize(
    ("incident", "message"),
    (
        (_discard_incident(admissible=True), "inadmissible"),
        (_discard_incident(category="program"), "reviewer_scope"),
        (_discard_incident(item_id="Q-OTHER"), "item_id"),
    ),
)
def test_discard_business_review_rejects_invalid_incidents(tmp_path: Path, incident: IncidentRecord, message: str) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "initial"})
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[{"finding_id": "F-SCOPE", "pointers": ["/answer"], "semantic_categories": ["answer"]}],
    )
    packet_before = workspace.business_review_path.read_bytes()
    with pytest.raises(ValueError, match=message):
        workspace.discard_business_review(incident)
    assert workspace.business_review_path.read_bytes() == packet_before
    assert workspace.state["business_repair_count"] == 0
    assert not workspace.business_review_discard_audit_path.exists()


def _review_packet_workspace(tmp_path: Path) -> ItemWorkspace:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "initial"})
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[{"finding_id": "F-SCOPE", "pointers": ["/answer"], "semantic_categories": ["answer"]}],
    )
    return workspace


def _repair_packet_workspace(tmp_path: Path) -> ItemWorkspace:
    workspace = _review_packet_workspace(tmp_path)
    workspace.use_business_repair(owner_ref="owner")
    workspace.write_draft({"answer": "repaired"})
    return workspace


def test_discard_business_review_requires_typed_source(tmp_path: Path) -> None:
    workspace = _review_packet_workspace(tmp_path)
    with pytest.raises(ValueError, match="source must be a non-empty string"):
        workspace.discard_business_review(_discard_incident(source=None))
    assert workspace.business_review_path.exists()
    assert not workspace.business_review_discard_audit_path.exists()


@pytest.mark.parametrize("source", (None, "", "   "))
def test_discard_business_review_requires_nonblank_mapping_source(tmp_path: Path, source: str | None) -> None:
    workspace = _review_packet_workspace(tmp_path)
    value = _discard_incident().to_dict()
    value["source"] = source
    with pytest.raises(ValueError, match="source must be a non-empty string"):
        workspace.discard_business_review(value)
    assert workspace.business_review_path.exists()
    assert not workspace.business_review_discard_audit_path.exists()


def test_discard_business_review_requires_mapping_source_field(tmp_path: Path) -> None:
    workspace = _review_packet_workspace(tmp_path)
    value = _discard_incident().to_dict()
    del value["source"]
    with pytest.raises(ValueError, match="missing fields"):
        workspace.discard_business_review(value)
    assert workspace.business_review_path.exists()
    assert not workspace.business_review_discard_audit_path.exists()


def test_discard_business_review_rejects_missing_packet(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "initial"})
    with pytest.raises(ValueError, match="structured review packet"):
        workspace.discard_business_review(_discard_incident())
    assert not workspace.business_review_discard_audit_path.exists()


def test_discard_business_review_rolls_back_on_state_persist_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_draft({"answer": "initial"})
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[{"finding_id": "F-SCOPE", "pointers": ["/answer"], "semantic_categories": ["answer"]}],
    )
    workspace.use_business_repair(owner_ref="owner")
    workspace.write_draft({"answer": "repaired"})
    state_path = workspace.item_root / "item_state.json"
    state_before = state_path.read_bytes()
    packet_before = workspace.business_review_path.read_bytes()
    draft_before = workspace.draft_root.read_bytes()

    def fail_persist(_state: Mapping[str, object], **_kwargs: object) -> None:
        raise RuntimeError("injected discard persistence failure")

    monkeypatch.setattr(workspace, "_persist_state", fail_persist)
    with pytest.raises(RuntimeError, match="injected discard persistence failure"):
        workspace.discard_business_review(_discard_incident())

    assert state_path.read_bytes() == state_before
    assert workspace.business_review_path.read_bytes() == packet_before
    assert workspace.draft_root.read_bytes() == draft_before
    assert not workspace.business_review_discard_audit_path.exists()
    assert workspace.state["business_repair_count"] == 1


def test_discard_reload_recovers_crash_after_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    incident = _discard_incident()

    def crash_before_audit(_path: Path, _value: Mapping[str, object]) -> None:
        raise _DiscardCrash("after intent")

    monkeypatch.setattr(durable_module, "_append_jsonl", crash_before_audit)
    with pytest.raises(_DiscardCrash):
        workspace.discard_business_review(incident)

    assert workspace.business_review_discard_state_path.exists()
    assert not workspace.business_review_discard_audit_path.exists()
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["business_repair_count"] == 1
    assert reloaded.business_review_path.exists()
    assert json.loads(reloaded.business_review_discard_state_path.read_text(encoding="utf-8"))["intent"] is None


def test_discard_reload_recovers_crash_after_audit_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    incident = _discard_incident()
    original_append = durable_module._append_jsonl

    def append_then_crash(path: Path, value: Mapping[str, object]) -> None:
        original_append(path, value)
        raise _DiscardCrash("after audit")

    monkeypatch.setattr(durable_module, "_append_jsonl", append_then_crash)
    with pytest.raises(_DiscardCrash):
        workspace.discard_business_review(incident)

    assert workspace.business_review_path.exists()
    assert workspace.business_review_discard_audit_path.exists()
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["business_repair_count"] == 0
    assert not reloaded.business_review_path.exists()
    assert reloaded.business_review_discard_audit_path.read_text(encoding="utf-8").count("\n") == 1
    assert reloaded.business_review_discard_state_path.read_text(encoding="utf-8").find('"intent":null') >= 0


def test_discard_reload_recovers_crash_after_packet_unlink_before_state_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    incident = _discard_incident()

    def crash_before_state(_state: Mapping[str, object], **_kwargs: object) -> None:
        raise _DiscardCrash("after packet unlink")

    monkeypatch.setattr(workspace, "_persist_state", crash_before_state)
    with pytest.raises(_DiscardCrash):
        workspace.discard_business_review(incident)

    assert not workspace.business_review_path.exists()
    assert workspace.state["business_repair_count"] == 1
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["business_repair_count"] == 0
    assert not reloaded.business_review_path.exists()


def test_discard_reload_rejects_draft_mutation_after_packet_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _repair_packet_workspace(tmp_path)

    def crash_before_state(_state: Mapping[str, object], **_kwargs: object) -> None:
        raise _DiscardCrash("after packet unlink")

    monkeypatch.setattr(workspace, "_persist_state", crash_before_state)
    with pytest.raises(_DiscardCrash):
        workspace.discard_business_review(_discard_incident())

    # Simulate an unrelated writer changing the draft while the process is
    # down.  The persisted intent must reject recovery rather than silently
    # accepting a hash it did not authorize.
    workspace.draft_root.write_bytes(b'{"answer":"tampered"}\n')
    with pytest.raises(ValueError, match="discard intent draft is invalid"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def test_discard_reload_recovers_crash_after_state_persist_before_commit_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    incident = _discard_incident()
    original_write = workspace._write_business_review_discard_state
    calls = 0

    def write_then_crash(*, audit_count: int, audit_head: str | None, intent: Mapping[str, object] | None) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise _DiscardCrash("after state persist")
        return original_write(audit_count=audit_count, audit_head=audit_head, intent=intent)

    monkeypatch.setattr(workspace, "_write_business_review_discard_state", write_then_crash)
    with pytest.raises(_DiscardCrash):
        workspace.discard_business_review(incident)

    assert workspace.state["business_repair_count"] == 0
    assert not workspace.business_review_path.exists()
    reloaded = ItemWorkspace.load(workspace.context, workspace.item_id)
    assert reloaded.state["business_repair_count"] == 0
    assert not reloaded.business_review_path.exists()
    assert reloaded.business_review_discard_state_path.read_text(encoding="utf-8").find('"intent":null') >= 0


def test_discard_audit_anchor_rejects_delete_rewrite_and_valid_prefix_truncation(tmp_path: Path) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    workspace.discard_business_review(_discard_incident())
    audit_path = workspace.business_review_discard_audit_path

    audit_path.unlink()
    with pytest.raises(ValueError, match="missing its durable state anchor|does not match"):
        ItemWorkspace.load(workspace.context, workspace.item_id)

    workspace = _repair_packet_workspace(tmp_path / "rewrite")
    workspace.discard_business_review(_discard_incident())
    audit_path = workspace.business_review_discard_audit_path
    tampered = json.loads(audit_path.read_text(encoding="utf-8"))
    tampered["disposition"] = "rewritten"
    audit_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="audit hash|discard audit"):
        ItemWorkspace.load(workspace.context, workspace.item_id)

    workspace = _repair_packet_workspace(tmp_path / "truncate")
    workspace.discard_business_review(_discard_incident())
    prefix_line = workspace.business_review_discard_audit_path.read_text(encoding="utf-8")
    workspace.record_review(
        "repair_once",
        reviewer_ref="review-2",
        findings=[{"finding_id": "F-SCOPE-2", "pointers": ["/answer"], "semantic_categories": ["answer"]}],
    )
    workspace.use_business_repair(owner_ref="owner")
    workspace.discard_business_review(
        IncidentRecord(
            incident_id="INC-REVIEW-SCOPE-002",
            category="reviewer_scope",
            disposition="discarded_invalid_scope",
            admissible=False,
            item_id="Q-001",
            scope=("work/q001_result.json", "/answer"),
            source="reviewer-scope-fixture",
        )
    )
    audit_path = workspace.business_review_discard_audit_path
    audit_path.write_text(prefix_line, encoding="utf-8")
    with pytest.raises(ValueError, match="does not match|audit count"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def _tamper_discard_audit(workspace: ItemWorkspace, kind: str) -> None:
    audit_path = workspace.business_review_discard_audit_path
    if kind == "delete":
        audit_path.unlink()
    elif kind == "truncate":
        audit_path.write_bytes(b"")
    elif kind == "rewrite":
        record = json.loads(audit_path.read_text(encoding="utf-8"))
        record["incident"]["disposition"] = "tampered"
        audit_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    else:
        raise AssertionError(f"unknown audit tamper: {kind}")


def test_discard_live_workspace_allows_stable_review_and_accept(tmp_path: Path) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    workspace.discard_business_review(_discard_incident())
    workspace.record_review("accept", reviewer_ref="review-2")

    snapshot = workspace.accept()

    assert snapshot.outcome == "accepted"
    assert workspace.accepted_root.is_dir()


@pytest.mark.parametrize("kind", ("delete", "truncate", "rewrite"))
def test_discard_live_workspace_rejects_audit_tamper_before_record_review(tmp_path: Path, kind: str) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    workspace.discard_business_review(_discard_incident())
    state_before = (workspace.item_root / "item_state.json").read_bytes()
    sidecar_before = workspace.business_review_discard_state_path.read_bytes()
    _tamper_discard_audit(workspace, kind)

    with pytest.raises(ValueError, match="business review discard|audit"):
        workspace.record_review("accept", reviewer_ref="review-2")

    assert (workspace.item_root / "item_state.json").read_bytes() == state_before
    assert workspace.business_review_discard_state_path.read_bytes() == sidecar_before
    assert not workspace.business_review_path.exists()


@pytest.mark.parametrize("kind", ("delete", "truncate", "rewrite"))
def test_discard_live_workspace_rejects_audit_tamper_before_accept(tmp_path: Path, kind: str) -> None:
    workspace = _repair_packet_workspace(tmp_path)
    workspace.discard_business_review(_discard_incident())
    workspace.record_review("accept", reviewer_ref="review-2")
    state_before = (workspace.item_root / "item_state.json").read_bytes()
    packet_before = workspace.business_review_path.read_bytes()
    _tamper_discard_audit(workspace, kind)

    with pytest.raises(ValueError, match="business review discard|audit"):
        workspace.accept()

    assert (workspace.item_root / "item_state.json").read_bytes() == state_before
    assert workspace.business_review_path.read_bytes() == packet_before
    assert not workspace.accepted_root.exists()


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


@pytest.mark.parametrize("mutation", ("replace", "add", "remove"))
def test_accept_rejects_post_review_artifact_progress_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_plan({"version": 1})
    workspace.write_draft({"answer": "reviewed"})
    workspace.record_review("accept", reviewer_ref="review-1")
    packet_before = workspace.business_review_path.read_bytes()
    state_before = (workspace.item_root / "item_state.json").read_bytes()
    plan_path = workspace.work_root / "plan.json"

    # Simulate a process-independent writer changing the work tree after the
    # Business Review has completed but before acceptance snapshots refs.
    if mutation == "replace":
        plan_path.write_bytes(b'{"version":2}\n')
    elif mutation == "add":
        (workspace.work_root / "late-output.json").write_bytes(b'{"late":true}\n')
    else:
        plan_path.unlink()

    with pytest.raises(ValueError, match="exact currently reviewed artifact progress"):
        workspace.accept(accepted_refs=("work/plan.json",))

    assert not workspace.accepted_root.exists()
    assert workspace.business_review_path.read_bytes() == packet_before
    assert (workspace.item_root / "item_state.json").read_bytes() == state_before


def test_accept_reuses_unchanged_reviewed_artifacts_and_repeated_review_is_idempotent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_plan({"version": 1})
    workspace.write_draft({"answer": "reviewed"})
    workspace.record_review("accept", reviewer_ref="review-1")
    packet_before = workspace.business_review_path.read_bytes()

    # Re-recording the same final review does not alter its artifact baseline.
    workspace.record_review("accept", reviewer_ref="review-1")
    assert workspace.business_review_path.read_bytes() == packet_before
    accepted = workspace.accept(accepted_refs=("work/plan.json",))
    packet = json.loads(packet_before.decode("utf-8"))
    manifest = json.loads((workspace.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["hashes"] == packet["after_artifact_hashes"]
    assert accepted.outcome == "accepted"


def test_accept_succeeds_after_artifact_mutation_is_re_reviewed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_plan({"version": 1})
    workspace.write_draft({"answer": "reviewed"})
    workspace.record_review("accept", reviewer_ref="review-1")
    plan_path = workspace.work_root / "plan.json"
    plan_path.write_bytes(b'{"version":2}\n')

    with pytest.raises(ValueError, match="exact currently reviewed artifact progress"):
        workspace.accept(accepted_refs=("work/plan.json",))

    workspace.record_review("accept", reviewer_ref="review-2")
    accepted = workspace.accept(accepted_refs=("work/plan.json",))
    assert accepted.outcome == "accepted"
    packet = json.loads(workspace.business_review_path.read_text(encoding="utf-8"))
    manifest = json.loads((workspace.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["hashes"] == packet["after_artifact_hashes"]


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


@pytest.mark.parametrize("terminalizer", ("accept", "technical_failure"))
def test_terminalizers_linearize_against_stale_item_writer(tmp_path: Path, terminalizer: str) -> None:
    workspace = _workspace(tmp_path / "terminal-first")
    if terminalizer == "accept":
        workspace.write_draft({"answer": "bounded"})
        workspace.record_review("accept", reviewer_ref="review-1")
    stale = ItemWorkspace.load(workspace.context, workspace.item_id)
    finalizer = ItemWorkspace.load(workspace.context, workspace.item_id)
    writer_errors: list[BaseException] = []
    started = threading.Event()

    def stale_writer() -> None:
        started.set()
        try:
            stale.write_draft({"answer": "must-not-overwrite"})
        except BaseException as exc:  # noqa: BLE001 - assert linearization
            writer_errors.append(exc)

    with finalizer._state_transition_lock():  # noqa: SLF001 - deterministic lock interleaving
        thread = threading.Thread(target=stale_writer)
        thread.start()
        assert started.wait(2)
        if terminalizer == "accept":
            finalizer.accept()
        else:
            finalizer.technical_failure("runtime exhausted", recovery_exhausted=True)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert writer_errors and isinstance(writer_errors[0], ValueError)
    assert ItemWorkspace.load(workspace.context, workspace.item_id).state["lifecycle_state"] in {
        "accepted",
        "technical_failure",
    }


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
        "recovery_receipt_ref",
        "recovery_invocation_id",
        "recovery_receipt_hash",
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
