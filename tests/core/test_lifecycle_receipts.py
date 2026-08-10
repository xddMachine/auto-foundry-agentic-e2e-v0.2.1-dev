from __future__ import annotations

import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.lifecycle import (
    AgentInvocationReceipt,
    InvocationReceiptLedger,
    RunLifecycle,
    classify_terminal_reason,
)
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _context(tmp_path: Path, run_id: str = "RUN-LIFECYCLE") -> RunContext:
    return RunContext(run_id, tmp_path / "run")


def _accepted(tmp_path: Path, item_id: str = "Q-001") -> ItemWorkspace:
    workspace = ItemWorkspace.create(_context(tmp_path), item_id, original_text="bounded")
    workspace.write_draft({"answer": "reviewed", "limits": []})
    workspace.record_review("accept", reviewer_ref="reviewer-1")
    workspace.accept(accepted_refs=("work/plan.json",))
    return workspace


def _receipt(
    item_id: str = "Q-001",
    *,
    reason: str = "process_lost",
    invocation_id: str = "I-001",
    attempt_id: str = "A-001",
    lane_id: str = "lane-1",
) -> AgentInvocationReceipt:
    return AgentInvocationReceipt(
        invocation_id,
        item_id,
        attempt_id,
        lane_id,
        "Lead Analyst",
        "lead",
        provider="unavailable",
        model="unavailable",
        start="2026-01-01T00:00:00+00:00",
        first_activity="2026-01-01T00:00:01+00:00",
        finish="2026-01-01T00:00:02+00:00",
        terminal_reason=reason,
        provider_error=None,
        interrupt_reason=None,
        artifact_delta={"files": ["work/plan.json"]},
        tool_calls=0,
    )


def _append_receipt_in_process(args: tuple[str, str, str]) -> tuple[str, str]:
    """Process worker used to exercise the POSIX ledger advisory lock."""

    run_id, run_root, invocation_id = args
    context = RunContext(run_id, Path(run_root))
    receipt = _receipt(invocation_id=invocation_id)
    try:
        return "ok", InvocationReceiptLedger(context).append(receipt)
    except Exception as exc:
        return type(exc).__name__, str(exc)


def test_acceptance_publishes_exact_content_and_separate_self_contained_envelope(tmp_path: Path) -> None:
    workspace = _accepted(tmp_path)
    content = workspace.accepted_root / "answer_content.json"
    envelope_path = workspace.accepted_root / "acceptance_envelope.json"
    manifest_path = workspace.accepted_root / "manifest.json"
    assert content.read_bytes() == workspace.draft_root.read_bytes()
    assert not (workspace.item_root / "accepted.json").exists()
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert set(envelope) == {
        "item_id",
        "outcome",
        "review_status",
        "review_strength",
        "review_verdict",
        "reviewer_ref",
        "content_hash",
        "draft_hash",
        "accepted_refs",
        "knowledge_delta",
        "accepted_at",
    }
    assert envelope["item_id"] == "Q-001"
    assert envelope["outcome"] == "accepted"
    assert envelope["review_status"] == "reviewed"
    assert envelope["review_verdict"] == "accept"
    assert envelope["content_hash"] == envelope["draft_hash"] == hashlib.sha256(content.read_bytes()).hexdigest()
    assert "pending" not in json.dumps(envelope)
    assert "draft_ready" not in json.dumps(envelope)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["content_hash"] == envelope["content_hash"]
    assert manifest["envelope_hash"] == hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    assert manifest["artifact_progress"]["hashes"] == manifest["hashes"]


@pytest.mark.parametrize("name", ["answer_content.json", "acceptance_envelope.json", "manifest.json"])
def test_acceptance_tampering_fails_closed(tmp_path: Path, name: str) -> None:
    workspace = _accepted(tmp_path)
    target = workspace.accepted_root / name
    if name == "manifest.json":
        value = json.loads(target.read_text(encoding="utf-8"))
        value["outcome"] = "accepted_with_limits"
        target.write_text(json.dumps(value), encoding="utf-8")
    elif name == "acceptance_envelope.json":
        value = json.loads(target.read_text(encoding="utf-8"))
        value["reviewer_ref"] = "tampered"
        target.write_text(json.dumps(value), encoding="utf-8")
    else:
        target.write_bytes(target.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="manifest|hash|envelope|snapshot|content"):
        ItemWorkspace.load(workspace.context, workspace.item_id)


def test_integration_state_is_program_owned_and_reloadable(tmp_path: Path) -> None:
    workspace = _accepted(tmp_path)
    assert workspace.state["integration_state"] == "pending"
    with pytest.raises(ValueError, match="hash|already terminal|accepted"):
        workspace.mark_integration_committed("not-a-hash", "integration.json")
    workspace.mark_integration_committed("a" * 64, "integration/manifest.json")
    assert ItemWorkspace.load(workspace.context, workspace.item_id).state["integration_state"] == "integrated"
    with pytest.raises(ValueError, match="already terminal"):
        workspace.mark_integration_failed("b" * 64, "integration-failure.json")


def test_run_lifecycle_reconciles_objective_barriers_and_limits(tmp_path: Path) -> None:
    context = _context(tmp_path)
    lifecycle = RunLifecycle.create(context, ["Q-001", "Q-002"])
    incomplete = [
        {"item_id": "Q-001", "lifecycle_state": "work", "integration_state": "pending"},
        {"item_id": "Q-002", "lifecycle_state": "work", "integration_state": "pending"},
    ]
    assert lifecycle.reconcile(incomplete).state == "running"
    accepted = [
        {
            "item_id": "Q-001",
            "lifecycle_state": "accepted",
            "terminal_outcome": {"outcome": "accepted"},
            "integration_state": "integrated",
        },
        {
            "item_id": "Q-002",
            "lifecycle_state": "accepted",
            "terminal_outcome": {"outcome": "accepted_with_limits"},
            "integration_state": "technical_failure",
        },
    ]
    assert lifecycle.reconcile(accepted).state == "integration_complete"
    assert lifecycle.reconcile(accepted, product_terminal_status="draft").state == "integration_complete"
    assert lifecycle.reconcile(accepted, product_terminal_status="complete", optimizer_terminal={"status": "technical_failure", "nonblocking": True}).state == "complete_with_limits"
    with pytest.raises(ValueError, match="item IDs"):
        RunLifecycle.create(_context(tmp_path / "other"), ["Q-001"]).reconcile(accepted)


def test_run_lifecycle_existing_identity_is_exact(tmp_path: Path) -> None:
    context = _context(tmp_path)
    RunLifecycle.create(context, ["Q-001", "Q-002"], mode="question")
    with pytest.raises(ValueError, match="item_ids/mode"):
        RunLifecycle.create(context, ["Q-001"], mode="question")
    with pytest.raises(ValueError, match="item_ids/mode"):
        RunLifecycle.create(context, ["Q-001", "Q-002"], mode="requirement")


def test_run_lifecycle_stale_instances_reload_and_never_regress_terminal_state(tmp_path: Path) -> None:
    context = _context(tmp_path)
    first = RunLifecycle.create(context, ["Q-001", "Q-002"])
    stale = RunLifecycle.load(context)
    working = [
        {"item_id": "Q-001", "lifecycle_state": "work", "integration_state": "pending"},
        {"item_id": "Q-002", "lifecycle_state": "work", "integration_state": "pending"},
    ]
    assert first.reconcile(working).state == "running"
    assert stale.reconcile(working).state == "running"
    accepted = [
        {"item_id": "Q-001", "lifecycle_state": "accepted", "terminal_outcome": {"outcome": "accepted"}, "integration_state": "integrated"},
        {"item_id": "Q-002", "lifecycle_state": "accepted", "terminal_outcome": {"outcome": "accepted"}, "integration_state": "integrated"},
    ]
    assert stale.reconcile(accepted).state == "integration_complete"
    assert first.reconcile(accepted, product_terminal_status="complete").state == "complete"
    # The stale object receives incomplete facts, but authoritative reload
    # keeps the terminal state monotonic and prevents regression.
    assert stale.reconcile(working).state == "complete"
    disk = RunLifecycle.load(context)
    assert disk.state == "complete"
    assert disk.snapshot.generation >= first.snapshot.generation


def test_filesystem_no_progress_does_not_authorize_recovery_and_reason_classifier_is_explicit(tmp_path: Path) -> None:
    workspace = ItemWorkspace.create(_context(tmp_path), "Q-001", original_text="bounded")
    attempt = workspace.begin_attempt("lane-1", "Lead Analyst")
    assert workspace.observe_attempt(attempt.attempt_id).action == "materialize_now"
    assert workspace.observe_attempt(attempt.attempt_id).action == "await_runtime"
    with pytest.raises(ValueError, match="receipt_ref"):
        workspace.begin_recovery("lane-2", "Lead Analyst", prior_attempt_id=attempt.attempt_id, receipt_ref=None)
    for reason in ("syntax_error", "name_error", "type_error", "dependency_error"):
        assert classify_terminal_reason(reason) == "same_attempt_feedback"
    assert classify_terminal_reason("business_review_error") == "business_repair"
    assert classify_terminal_reason("process_lost") == "execution_recovery"
    assert classify_terminal_reason("core_defect") == "abort_and_new_clean_run"
    assert classify_terminal_reason(None) is None
    ledger = InvocationReceiptLedger(workspace.context)
    coding_ref = ledger.append(_receipt(reason="syntax_error", attempt_id=attempt.attempt_id, lane_id=attempt.lane_id))
    with pytest.raises(ValueError, match="does not authorize"):
        workspace.begin_recovery("lane-2", "Lead Analyst", prior_attempt_id=attempt.attempt_id, receipt_ref=coding_ref)


def test_receipt_ledger_reload_tamper_path_safety_and_passive_telemetry(tmp_path: Path) -> None:
    context = _context(tmp_path)
    ledger = InvocationReceiptLedger(context)
    receipt = _receipt()
    receipt_ref = ledger.append(receipt)
    assert ledger.get(receipt_ref).provider == "unavailable"
    assert ledger.get(receipt_ref).model == "unavailable"
    reloaded = InvocationReceiptLedger(context)
    assert reloaded.receipts == (receipt,)
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    value = json.loads(lines[0])
    value["terminal_reason"] = "host_interruption"
    ledger.path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        InvocationReceiptLedger(context)

    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    with pytest.raises(AllowedRootError):
        InvocationReceiptLedger(context, path=outside)

    recorder = TelemetryRecorder(context=context)
    assert recorder.record_invocation(_receipt(invocation_id="I-002")) is not None
    class BrokenLedger:
        def append(self, value):
            raise OSError("telemetry unavailable")
    recorder._invocation_ledger = BrokenLedger()
    assert recorder.record_invocation(_receipt(invocation_id="I-003")) is not None


def test_receipt_ledger_thread_lock_serializes_duplicate_and_distinct_appends(tmp_path: Path) -> None:
    context = _context(tmp_path, run_id="RUN-LEDGER-THREADS")

    def append_same(_: int) -> tuple[str, str]:
        try:
            return "ok", InvocationReceiptLedger(context).append(_receipt(invocation_id="I-SAME"))
        except Exception as exc:
            return type(exc).__name__, str(exc)

    with ThreadPoolExecutor(max_workers=8) as executor:
        duplicate_results = tuple(executor.map(append_same, range(8)))
    assert sum(status == "ok" for status, _ in duplicate_results) == 1
    assert sum(status == "ValueError" and message == "invocation_id is already recorded" for status, message in duplicate_results) == 7

    def append_distinct(index: int) -> tuple[str, str]:
        try:
            return "ok", InvocationReceiptLedger(context).append(_receipt(invocation_id=f"I-DISTINCT-{index}"))
        except Exception as exc:
            return type(exc).__name__, str(exc)

    with ThreadPoolExecutor(max_workers=8) as executor:
        distinct_results = tuple(executor.map(append_distinct, range(8)))
    assert all(status == "ok" for status, _ in distinct_results)
    ledger = InvocationReceiptLedger(context)
    assert {receipt.invocation_id for receipt in ledger.receipts} == {
        "I-SAME",
        *(f"I-DISTINCT-{index}" for index in range(8)),
    }


def test_receipt_ledger_process_lock_serializes_duplicate_and_distinct_appends(tmp_path: Path) -> None:
    run_id = "RUN-LEDGER-PROCESSES"
    run_root = tmp_path / "run"
    same_args = tuple((run_id, str(run_root), "I-SAME") for _ in range(2))
    with ProcessPoolExecutor(max_workers=2) as executor:
        duplicate_results = tuple(executor.map(_append_receipt_in_process, same_args))
    assert sorted(status for status, _ in duplicate_results) == ["ValueError", "ok"]
    assert sum(message == "invocation_id is already recorded" for _, message in duplicate_results) == 1

    distinct_args = tuple((run_id, str(run_root), f"I-DISTINCT-{index}") for index in range(4))
    with ProcessPoolExecutor(max_workers=4) as executor:
        distinct_results = tuple(executor.map(_append_receipt_in_process, distinct_args))
    assert all(status == "ok" for status, _ in distinct_results)
    ledger = InvocationReceiptLedger(RunContext(run_id, run_root))
    assert {receipt.invocation_id for receipt in ledger.receipts} == {
        "I-SAME",
        *(f"I-DISTINCT-{index}" for index in range(4)),
    }


def test_receipt_requires_terminal_reason_at_finish(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="terminal_reason"):
        AgentInvocationReceipt(
            "I-001", "Q-001", "A-001", "lane-1", "Lead Analyst", "lead", finish="2026-01-01T00:00:00Z"
        )
    ledger = InvocationReceiptLedger(_context(tmp_path))
    with pytest.raises(ValueError, match="completed"):
        ledger.append(AgentInvocationReceipt("I-001", "Q-001", "A-001", "lane-1", "Lead Analyst", "lead"))
