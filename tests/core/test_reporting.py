from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.reporting import RunReportFinalizer, RunReportProjector
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import RunContext


def test_business_review_scopes_multiple_findings_and_targeted_rereview(tmp_path: Path) -> None:
    workspace = ItemWorkspace.create(RunContext("RUN-REVIEW", tmp_path / "run"), "Q-001", original_text="bounded")
    workspace.write_draft({"answer": {"late": 1, "partial": 2}, "unrelated": "keep"})
    review = workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[
            {"finding_id": "F-LATE", "pointers": ["/answer/late"], "dependent_outputs": ["/derived/late"]},
            {"finding_id": "F-PARTIAL", "pointers": ["/answer/partial"], "artifact_paths": ["work/analysis.json"]},
        ],
    )
    assert review["finding_count"] == 2
    reviewed_hash = review["draft_hash"]
    scope = workspace.use_business_repair()
    assert scope["before_hash"] == reviewed_hash
    with pytest.raises(ValueError, match="outside reviewed scope"):
        workspace.write_draft({"answer": {"late": 3, "partial": 2}, "unrelated": "changed"})
    workspace.write_draft({"answer": {"late": 3, "partial": 2}, "unrelated": "keep"})
    targeted = workspace.record_review("accept", reviewer_ref="review-2")
    assert targeted["review_scope"] == "targeted"
    assert targeted["targeted_recheck"] is True
    assert targeted["changed_pointers"] == ["/answer/late"]


def test_phase_timing_keeps_unavailable_facts_and_incidents_are_exactly_once(tmp_path: Path) -> None:
    recorder = TelemetryRecorder(RunContext("RUN-TIMING", tmp_path / "run"))
    observed = recorder.record_phase(
        "analyst/model work",
        start="2026-01-01T00:00:00+00:00",
        finish="2026-01-01T00:00:01+00:00",
        provider="provider-a",
        model="model-a",
    )
    unavailable = recorder.record_phase("optimizer")
    assert observed.wall_time_ms == 1000
    assert unavailable.start is None and unavailable.finish is None and unavailable.wall_time_ms is None
    incident = {"incident_id": "I-1", "category": "recovery", "disposition": "replayed", "admissible": True}
    recorder.record_incident(incident)
    recorder.record_incident(incident)
    summary = recorder.summary()
    assert summary.facts["incident_count"] == 1
    assert summary.facts["incidents"] == [
        {
            "incident_id": "I-1",
            "category": "recovery",
            "disposition": "replayed",
            "admissible": True,
            "item_id": None,
            "scope": [],
            "source": None,
            "facts": {},
        }
    ]
    assert summary.facts["incident_totals"] == {"recovery": 1}
    assert summary.facts["phase_timing_totals"]["analyst_model"]["wall_time_ms"] == 1000
    assert summary.facts["phase_timing_totals"]["optimizer"] == {
        "count": 1,
        "wall_time_ms": None,
        "start": None,
        "finish": None,
    }


def test_report_projector_rejects_bad_sha_and_finalizer_is_idempotent_and_tamper_evident(tmp_path: Path) -> None:
    projector = RunReportProjector(run_id="RUN-REPORT")
    report = projector.project(
        [
            {
                "item_id": "Q-001",
                "outcome": "accepted",
                "lifecycle_state": "accepted",
                "record_kind_totals": {"claim": 1},
                "implementation_sha": "a" * 40,
                "implementation_tree": "b" * 40,
                "implementation_version": "0.3.5",
            }
        ],
        receipts=[
            {
                "invocation_id": "I-1",
                "item_id": "Q-001",
                "attempt_id": "A-1",
                "lane_id": "lane-1",
                "role": "Evidence Collector",
                "route": "evidence",
                "provider": "provider-a",
                "model": "model-a",
                "start": "2026-01-01T00:00:00+00:00",
                "first_activity": "2026-01-01T00:00:00+00:00",
                "finish": "2026-01-01T00:00:01+00:00",
                "terminal_reason": "completed",
                "provider_error": None,
                "interrupt_reason": None,
                "artifact_delta": {},
                "tool_calls": 1,
            }
        ],
        timings=[
            {
                "timing_id": "TIM-1",
                "phase": "products",
                "item_id": "Q-001",
                "start": "2026-01-01T00:00:00+00:00",
                "finish": "2026-01-01T00:00:01+00:00",
                "wall_time_ms": 1000,
                "provider": "provider-a",
                "model": "model-a",
                "receipt_ref": "telemetry/invocation_receipts.jsonl#I-1",
            }
        ],
        incidents=[{"incident_id": "INC-1", "category": "program", "disposition": "fixed", "admissible": True}],
        lifecycle_status="complete",
    )
    finalizer = RunReportFinalizer(tmp_path / "run")
    receipt = finalizer.finalize(report, authoritative_incidents=report["incidents"])
    assert finalizer.finalize(report, authoritative_incidents=report["incidents"]) == receipt
    report_path = tmp_path / "run" / "reporting" / "final_report.json"
    manifest_path = tmp_path / "run" / "reporting" / "run_manifest.json"
    receipt_path = tmp_path / "run" / "reporting" / "terminalization_receipt.json"
    persisted_report = json.loads(report_path.read_text(encoding="utf-8"))
    persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    persisted_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    report_unsigned = dict(persisted_report)
    report_unsigned.pop("report_hash")
    expected_report_hash = hashlib.sha256(
        (json.dumps(report_unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    assert persisted_report["report_hash"] == expected_report_hash
    assert persisted_manifest["report_hash"] == expected_report_hash
    assert persisted_receipt["report_hash"] == expected_report_hash
    assert persisted_receipt["manifest_hash"] == persisted_manifest["manifest_hash"]
    manifest_unsigned = dict(persisted_manifest)
    manifest_unsigned.pop("manifest_hash")
    expected_manifest_hash = hashlib.sha256(
        (json.dumps(manifest_unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    receipt_unsigned = dict(persisted_receipt)
    receipt_unsigned.pop("receipt_hash")
    expected_receipt_hash = hashlib.sha256(
        (json.dumps(receipt_unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    assert persisted_manifest["manifest_hash"] == expected_manifest_hash
    assert persisted_receipt["receipt_hash"] == expected_receipt_hash
    assert len(persisted_report["incidents"]) == persisted_report["incident_count"] == 1
    assert all(value is not None for value in persisted_receipt.values())
    stale_incident_report = dict(report)
    stale_incident_report["incidents"] = []
    stale_incident_report["incident_count"] = 0
    with pytest.raises(ValueError, match="incident .* stale"):
        finalizer.finalize(stale_incident_report, authoritative_incidents=report["incidents"])
    tampered_incident_report = dict(report)
    tampered_incident_report["incidents"] = [dict(report["incidents"][0], disposition="reopened")]
    with pytest.raises(ValueError, match="incident facts are stale"):
        finalizer.finalize(tampered_incident_report, authoritative_incidents=report["incidents"])
    stale_report = dict(report)
    stale_report["outcome_counts"] = {"technical_failure": 1}
    with pytest.raises(ValueError, match="outcome_counts are stale"):
        finalizer.finalize(stale_report, authoritative_incidents=report["incidents"])
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["incident_count"] = 0
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="hash|stale"):
        finalizer.finalize(report, authoritative_incidents=report["incidents"])
    with pytest.raises(ValueError, match="40 lowercase"):
        projector.project(
            [
                {
                    "item_id": "Q-001",
                    "outcome": "accepted",
                    "implementation_sha": "a" * 41,
                    "implementation_tree": "b" * 40,
                "implementation_version": "0.3.5",
                }
            ]
        )
    with pytest.raises(ValueError, match="requires exact sha, tree, and version"):
        projector.project([{"item_id": "Q-001", "outcome": "accepted"}])


def test_report_projector_roundtrips_authoritative_reviews_receipts_timings_and_transition(tmp_path: Path) -> None:
    projector = RunReportProjector(run_id="RUN-ROUNDTRIP")
    item = {
        "item_id": "Q-001",
        "outcome": "accepted",
        "lifecycle_state": "accepted",
        "record_kind_totals": {"claim": 2},
        "implementation_sha": "a" * 40,
        "implementation_tree": "b" * 40,
        "implementation_version": "0.3.5",
    }
    receipts = [
        {
            "invocation_id": "I-1",
            "item_id": "Q-001",
            "attempt_id": "A-1",
            "lane_id": "lane-1",
            "role": "Lead Analyst",
            "route": "lead",
            "provider": "provider-a",
            "model": "model-a",
            "start": "2026-01-01T00:00:00+00:00",
            "first_activity": None,
            "finish": "2026-01-01T00:00:01+00:00",
            "terminal_reason": "completed",
            "provider_error": None,
            "interrupt_reason": None,
            "artifact_delta": {"files": ["work/answer.json"]},
            "tool_calls": 2,
        },
        {
            "invocation_id": "I-2",
            "item_id": "Q-001",
            "attempt_id": "A-2",
            "lane_id": "lane-2",
            "role": "Independent Business Reviewer",
            "route": "review",
            "provider": "unavailable",
            "model": "unavailable",
            "start": None,
            "first_activity": None,
            "finish": None,
            "terminal_reason": None,
            "provider_error": None,
            "interrupt_reason": "host_unavailable",
            "artifact_delta": {},
            "tool_calls": "unavailable",
        },
    ]
    timings = [
        {
            "timing_id": "TIM-BR",
            "phase": "business_review",
            "item_id": "Q-001",
            "start": "2026-01-01T00:00:01+00:00",
            "finish": "2026-01-01T00:00:02+00:00",
            "wall_time_ms": 1000,
            "provider": "provider-a",
            "model": "model-a",
            "receipt_ref": "telemetry/invocation_receipts.jsonl#I-1",
        },
        {
            "timing_id": "TIM-FID",
            "phase": "fidelity_integration_review",
            "item_id": "Q-001",
            "start": None,
            "finish": None,
            "wall_time_ms": None,
            "provider": None,
            "model": None,
            "receipt_ref": "I-2",
        },
    ]
    business_reviews = [
        {
            "review_id": "BR-1",
            "item_id": "Q-001",
            "verdict": "repair_once",
            "findings": [
                {"finding_id": "F-2", "pointers": ["/answer/late"], "dependent_outputs": ["/derived/late"]},
                {"finding_id": "F-1", "artifact_paths": ["work/analysis.json"]},
            ],
            "repairs": [
                {
                    "repair_id": "R-1",
                    "allowed_pointers": ["/answer/late"],
                    "changed_pointers": ["/answer/late"],
                    "before_hash": "c" * 64,
                    "after_hash": "d" * 64,
                    "unchanged_aggregate_hash": "e" * 64,
                }
            ],
            "targeted_rechecks": [
                {"recheck_id": "RR-1", "scope": ["/answer/late"], "verdict": "accept"}
            ],
            "unchanged_proofs": [{"scope": "/unrelated", "hash": "f" * 64}],
        }
    ]
    fidelity_reviews = [
        {
            "review_id": "FR-1",
            "item_id": "Q-001",
            "verdict": "pass",
            "findings": [{"finding_id": "FF-1", "pointers": ["/records/0"]}],
            "repairs": [],
            "targeted_rechecks": [],
            "unchanged_proofs": [{"scope": "/records/1", "hash": "1" * 64}],
        }
    ]
    transitions = [
        {
            "transition_id": "T-1",
            "old_sha": "1" * 40,
            "new_sha": "2" * 40,
            "old_tree": "3" * 40,
            "new_tree": "4" * 40,
            "old_version": "0.3.0",
            "new_version": "0.3.1",
            "earliest_affected_item": "Q-001",
            "preserved_accepted_hashes": {"Q-000": "5" * 64},
            "unaffected_reason": "prior accepted item is outside changed scope",
            "resume_point": "Q-001:business_review",
        }
    ]
    metadata = {
        "initial": {"sha": "6" * 40, "tree": "7" * 40, "version": "0.3.0"},
        "intermediate": [{"sha": "8" * 40, "tree": "9" * 40, "version": "0.3.0+repair"}],
        "final": {"sha": "a" * 40, "tree": "b" * 40, "version": "0.3.1"},
    }
    incidents = [
        {"incident_id": "INC-1", "category": "reviewer_scope", "disposition": "repaired", "admissible": True, "item_id": "Q-001"},
        {"incident_id": "INC-2", "category": "program", "disposition": "none", "admissible": False, "item_id": "Q-001"},
    ]
    report = projector.project(
        [item],
        receipts=receipts,
        timings=timings,
        incidents=incidents,
        business_reviews=business_reviews,
        fidelity_reviews=fidelity_reviews,
        implementation_transitions=transitions,
        implementation_metadata=metadata,
        lifecycle_status="complete",
    )
    assert report["receipts"] == receipts
    assert report["timings"] == timings
    assert report["business_reviews"][0]["finding_count"] == 2
    assert report["business_reviews"][0]["repair_count"] == 1
    assert report["fidelity_reviews"][0]["review_id"] == "FR-1"
    assert report["business_review_count"] == report["fidelity_review_count"] == 1
    assert report["implementation_transitions"] == transitions
    assert report["implementation_metadata"] == metadata
    finalizer = RunReportFinalizer(tmp_path / "run")
    authoritative = dict(
        authoritative_receipts=receipts,
        authoritative_timings=timings,
        authoritative_incidents=incidents,
        authoritative_business_reviews=business_reviews,
        authoritative_fidelity_reviews=fidelity_reviews,
        authoritative_implementation_transitions=transitions,
        authoritative_implementation_metadata=metadata,
    )
    receipt = finalizer.finalize(report, **authoritative)
    assert finalizer.finalize(report, **authoritative) == receipt
    assert receipt["business_review_count"] == 1
    stale = dict(report)
    stale["business_reviews"] = []
    stale["business_review_count"] = 0
    with pytest.raises(ValueError, match="business review records are stale"):
        finalizer.finalize(stale, authoritative_business_reviews=business_reviews)
    stale_receipts = dict(report)
    stale_receipts["receipts"] = receipts[:1]
    stale_receipts["receipt_count"] = 1
    with pytest.raises(ValueError, match="stale"):
        finalizer.finalize(stale_receipts, authoritative_receipts=receipts)
    tampered_timing = dict(report)
    tampered_timing["timings"] = [dict(timings[0], provider="tampered"), timings[1]]
    with pytest.raises(ValueError, match="timings are stale"):
        finalizer.finalize(tampered_timing, authoritative_timings=timings)
    tampered = dict(report)
    tampered["implementation_transitions"] = [dict(transitions[0], resume_point="Q-001:products")]
    with pytest.raises(ValueError, match="implementation transitions are stale"):
        finalizer.finalize(tampered, authoritative_implementation_transitions=transitions)
    with pytest.raises(ValueError, match="appears more than once"):
        projector.project([item], receipts=[receipts[0], receipts[0]], lifecycle_status="complete")
    with pytest.raises(ValueError, match="not linked"):
        projector.project([item], receipts=[receipts[0]], timings=[dict(timings[0], receipt_ref="I-missing")], lifecycle_status="complete")
    with pytest.raises(ValueError, match="requires exact sha, tree, and version"):
        projector.project(
            [item],
            implementation_metadata={"initial": {"sha": "a" * 40, "version": "0.3.0"}},
            lifecycle_status="complete",
        )
    with pytest.raises(ValueError, match="40 lowercase"):
        projector.project(
            [item],
            implementation_metadata={
                "final": {"sha": "a" * 40, "tree": "b" * 41, "version": "0.3.1"}
            },
            lifecycle_status="complete",
        )


def test_implementation_transition_preserves_hashes_and_resume_point(tmp_path: Path) -> None:
    context = RunContext("RUN-TRANSITION", tmp_path / "run")
    lifecycle = RunLifecycle.create(context, ["Q-001"])
    transition = lifecycle.record_implementation_transition(
        old_sha="a" * 40,
        new_sha="b" * 40,
        old_tree="c" * 40,
        new_tree="d" * 40,
        old_version="0.3.0",
        new_version="0.3.1",
        earliest_affected_item="Q-001",
        preserved_accepted_hashes={"Q-000": "e" * 64},
        unaffected_reason="prior accepted items use unaffected code",
        resume_point="Q-001:business_review",
    )
    assert transition.resume_point == "Q-001:business_review"
    assert lifecycle.implementation_transitions == (transition,)
    with pytest.raises(ValueError, match="40 lowercase"):
        lifecycle.record_implementation_transition(
            old_sha="a" * 41,
            new_sha="b" * 40,
            old_tree="c" * 40,
            new_tree="d" * 40,
            old_version="0.3.0",
            new_version="0.3.1",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="reason",
            resume_point="Q-001",
        )
