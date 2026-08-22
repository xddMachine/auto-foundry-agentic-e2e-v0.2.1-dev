from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from auto_foundry_core.durable import ItemWorkspace, _json_bytes as _durable_json_bytes, _pointer_hashes
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.reporting import (
    ReportPreflightError,
    RunReportEventBindings,
    RunReportFinalizer,
    RunReportInputGatherer,
    RunReportProjector,
    inspect_report_artifacts,
)
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import RunContext


def _collector_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _collector_hash(value: object) -> str:
    return hashlib.sha256(
        (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _collector_fixture(tmp_path: Path) -> tuple[RunContext, Path]:
    context = RunContext("RUN-COLLECTOR", tmp_path / "run")
    lifecycle = RunLifecycle.create(context, ["REQ-01"], mode="requirement")
    state_path = context.run_root / "run_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "complete"
    state["manifest_hash"] = _collector_hash({key: value for key, value in state.items() if key != "manifest_hash"})
    _collector_json(state_path, state)

    product = {
        "run_id": context.run_id,
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "status": "complete",
        "terminal": True,
        "lifecycle": {
            "generation_id": lifecycle.generation_id,
            "all_items_terminal": True,
            "all_items_integrated": True,
        },
        "lem": {"prepared_assets": 0, "relationships": 0, "item_bindings": 1},
        "assets": [],
    }
    product_path = context.run_root / "products" / "product_manifest.json"
    _collector_json(product_path, product)

    item_root = context.run_root / "requirements" / "REQ-01"
    (item_root / "work").mkdir(parents=True, exist_ok=True)
    content_path = item_root / "accepted" / "answer_content.json"
    content = {"answer": "bounded"}
    _collector_json(content_path, content)
    content_hash = hashlib.sha256(content_path.read_bytes()).hexdigest()
    envelope = {
        "item_id": "REQ-01",
        "outcome": "accepted",
        "review_status": "reviewed",
        "review_strength": "independent",
        "review_verdict": "accept",
        "reviewer_ref": "reviewer-1",
        "content_hash": content_hash,
        "draft_hash": content_hash,
        "accepted_refs": [],
        "knowledge_delta": "no_change",
        "accepted_at": "2026-01-01T00:00:00+00:00",
    }
    envelope_path = item_root / "accepted" / "acceptance_envelope.json"
    _collector_json(envelope_path, envelope)
    envelope_hash = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    receipt_path = item_root / "work" / "script_receipts" / "receipt-1.json"
    script_path = item_root / "work" / "calc.py"
    context_path = item_root / "work" / "analysis_context.json"
    script_path.write_text("print('ok')\n", encoding="utf-8")
    _collector_json(context_path, {"item_id": "REQ-01"})
    receipt = {
        "receipt_id": "receipt-1",
        "receipt_path": str(receipt_path),
        "script_path": str(script_path),
        "context_path": str(context_path),
        "phase": "full",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "wall_seconds": 1.0,
        "exit_code": 0,
        "output_hashes": {},
        "script_hash": hashlib.sha256(script_path.read_bytes()).hexdigest(),
        "source_hash": "a" * 64,
        "context_hash": hashlib.sha256(context_path.read_bytes()).hexdigest(),
    }
    _collector_json(receipt_path, receipt)
    accepted = {
        "item_id": "REQ-01",
        "outcome": "accepted",
        "content_path": "answer_content.json",
        "content_hash": content_hash,
        "envelope_path": "acceptance_envelope.json",
        "envelope_hash": envelope_hash,
        "artifact_progress": {"hashes": {"work/script_receipts/receipt-1.json": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}},
    }
    accepted["manifest_hash"] = _collector_hash(accepted)
    _collector_json(item_root / "accepted" / "manifest.json", accepted)
    records_path = item_root / "integration" / "committed" / "records.jsonl"
    records_path.parent.mkdir(parents=True, exist_ok=True)
    records_path.write_text("", encoding="utf-8")
    committed = {
        "schema_version": "1",
        "session_id": "S-1",
        "item_id": "REQ-01",
        "owner_id": "integration-owner",
        "invocation_id": "I-1",
        "status": "committed",
        "accepted_content_hash": content_hash,
        "accepted_manifest_hash": accepted["manifest_hash"],
        "records_path": "records.jsonl",
        "records_hash": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        "records_count": 0,
        "counts": {},
        "created_at": "2026-01-01T00:00:00+00:00",
        "committed_at": "2026-01-01T00:00:01+00:00",
    }
    committed["manifest_hash"] = _collector_hash(committed)
    _collector_json(item_root / "integration" / "committed" / "manifest.json", committed)
    before_snapshot = content
    before_hash = hashlib.sha256(_durable_json_bytes(before_snapshot)).hexdigest()
    business = {
        "item_id": "REQ-01",
        "review_scope": "full",
        "reviewed_draft_hash": content_hash,
        "before_hash": before_hash,
        "before_snapshot": before_snapshot,
        "before_pointer_hashes": _pointer_hashes(before_snapshot),
        "before_artifact_hashes": {},
        "after_pointer_hashes": _pointer_hashes(before_snapshot),
        "after_artifact_hashes": {},
        "findings": [{
            "finding_id": "F-1",
            "message": "bounded finding",
            "pointers": ["/answer"],
            "artifact_paths": [],
            "dependent_outputs": [],
            "material": True,
            "semantic_categories": ["answer"],
        }],
        "allowed_pointers": ["/answer"],
        "allowed_artifact_paths": [],
        "allowed_dependencies": [],
        "changed_pointers": [],
        "unchanged_paths": [],
        "unchanged_aggregate_hash": "d" * 64,
        "repair_active": False,
        "targeted_recheck": False,
    }
    _collector_json(item_root / "work" / "business_review.json", business)
    session = {
        "schema_version": "1", "session_id": "S-1", "item_id": "REQ-01", "owner_id": "integration-owner",
        "invocation_id": "I-1", "status": "committed", "accepted_content_hash": content_hash,
        "accepted_manifest_hash": accepted["manifest_hash"], "records_count": 0,
        "records_hash": committed["records_hash"], "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
    }
    session["state_hash"] = _collector_hash(session)
    session_path = item_root / "integration" / "staging" / "session.json"
    _collector_json(session_path, session)
    _collector_json(
        item_root / "integration" / "staging" / "snapshot.json",
        {"schema_version": "1", "state": session, "records": []},
    )
    (item_root / "integration" / "staging" / "records.jsonl").write_text("", encoding="utf-8")
    snapshot_path = item_root / "integration" / "staging" / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["snapshot_hash"] = _collector_hash({key: value for key, value in snapshot.items() if key != "snapshot_hash"})
    _collector_json(snapshot_path, snapshot)
    fidelity = {
        "schema_version": "1", "item_id": "REQ-01", "session_id": "S-1", "invocation_id": "I-1",
        "review_kind": "initial", "verdict": "accept", "packet_hash": "b" * 64, "records_hash": committed["records_hash"],
        "findings": [], "affected_record_ids": [], "dependency_ids": [], "checked_record_ids": [], "created_at": "2026-01-01T00:00:00+00:00",
    }
    fidelity["result_hash"] = hashlib.sha256(json.dumps(fidelity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    _collector_json(item_root / "integration" / "review" / "result.json", fidelity)
    _collector_json(
        item_root / "item_state.json",
        {
            "item_id": "REQ-01", "mode": "requirement", "original_text": "bounded", "lifecycle_state": "accepted",
            "integration_state": "integrated", "integration_manifest_hash": committed["manifest_hash"],
            "integration_manifest_ref": "integration/committed/manifest.json",
            "terminal_outcome": {
                "status": "accepted", "item_id": "REQ-01", "outcome": "accepted",
                "manifest_path": str(item_root / "accepted" / "manifest.json"), "content_hash": content_hash,
            },
            "terminal_intent": {"outcome": "accepted", "manifest_hash": accepted["manifest_hash"]},
            "review": {"draft_hash": content_hash, "reviewer_ref": "reviewer-1", "verdict": "accept", "status": "reviewed", "strength": "independent"},
        },
    )
    _collector_jsonl = context.run_root / "telemetry" / "events.jsonl"
    _collector_jsonl.parent.mkdir(parents=True, exist_ok=True)
    _collector_jsonl.write_text("{\"event_type\":\"data_room_member_read\",\"facts\":{}}\n", encoding="utf-8")
    return context, product_path


def test_gather_from_run_discovers_canonical_inputs_and_finalizes(tmp_path: Path) -> None:
    context, product_path = _collector_fixture(tmp_path)
    # A stale root LEM projection must not replace the active product's
    # generation-scoped counts.
    _collector_json(context.run_root / "products" / "living_enterprise_model_snapshot.json", {"counts": {"relationships": 999}})
    first = RunReportInputGatherer.gather_from_run(context)
    second = RunReportInputGatherer.gather_from_run(context)
    assert first.to_dict() == second.to_dict()
    assert first.projected_report["lem_counts"]["counts"]["relationships"] == 0
    assert first.projected_report["receipt_count"] == first.projected_report["timing_count"] == 1
    assert first.projected_report["business_review_count"] == first.projected_report["fidelity_review_count"] == 1
    finalizer = RunReportFinalizer(context.run_root)
    finalizer.finalize(first)
    inspected = inspect_report_artifacts(context.run_root, run_id=context.run_id)
    assert inspected["valid"] is True and inspected["stage"] == "finalized"
    assert product_path.is_file()


@pytest.mark.parametrize("tamper", ["missing", "hash"])
def test_gather_from_run_fails_closed_on_missing_or_tampered_receipt(tmp_path: Path, tamper: str) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    receipt_path = context.run_root / "requirements" / "REQ-01" / "work" / "script_receipts" / "receipt-1.json"
    if tamper == "missing":
        receipt_path.unlink()
    else:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
        value["exit_code"] = 1
        _collector_json(receipt_path, value)
    with pytest.raises(ReportPreflightError, match="(receipt|script receipts)"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


def test_gather_from_run_rejects_injected_well_formed_receipt(tmp_path: Path) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    receipt_dir = context.run_root / "requirements" / "REQ-01" / "work" / "script_receipts"
    original = json.loads((receipt_dir / "receipt-1.json").read_text(encoding="utf-8"))
    injected = dict(original)
    injected["receipt_id"] = "receipt-injected"
    injected["receipt_path"] = str(receipt_dir / "receipt-injected.json")
    _collector_json(receipt_dir / "receipt-injected.json", injected)
    with pytest.raises(ReportPreflightError, match="accepted artifact bindings"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


def _rewrite_hashed_json(path: Path, field: str) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    value[field] = _collector_hash({key: item for key, item in value.items() if key != field})
    _collector_json(path, value)
    return value


def _rewrite_fidelity_result(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    unsigned = {key: item for key, item in value.items() if key != "result_hash"}
    value["result_hash"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _collector_json(path, value)
    return value


@pytest.mark.parametrize("field", ["accepted_manifest_hash", "accepted_content_hash"])
def test_gather_from_run_rejects_rehashed_committed_bundle_cross_links(tmp_path: Path, field: str) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    path = context.run_root / "requirements" / "REQ-01" / "integration" / "committed" / "manifest.json"
    committed = json.loads(path.read_text(encoding="utf-8"))
    committed[field] = "f" * 64
    _collector_json(path, {**committed, "manifest_hash": _collector_hash({key: item for key, item in committed.items() if key != "manifest_hash"})})
    with pytest.raises(ReportPreflightError, match="committed accepted_.* binding"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


@pytest.mark.parametrize("field,value", [
    ("records_hash", "f" * 64),
    ("session_id", "S-foreign"),
    ("invocation_id", "I-foreign"),
])
def test_gather_from_run_rejects_rehashed_fidelity_cross_links(tmp_path: Path, field: str, value: str) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    path = context.run_root / "requirements" / "REQ-01" / "integration" / "review" / "result.json"
    fidelity = json.loads(path.read_text(encoding="utf-8"))
    fidelity[field] = value
    _collector_json(path, {**fidelity, "result_hash": hashlib.sha256(
        json.dumps({key: item for key, item in fidelity.items() if key != "result_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()})
    with pytest.raises(ReportPreflightError, match="fidelity review binding"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


@pytest.mark.parametrize("field,value", [("session_id", "S-foreign"), ("invocation_id", "I-foreign")])
def test_gather_from_run_rejects_rehashed_session_cross_links(tmp_path: Path, field: str, value: str) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    item_root = context.run_root / "requirements" / "REQ-01"
    session_path = item_root / "integration" / "staging" / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session[field] = value
    session = {**session, "state_hash": _collector_hash({key: item for key, item in session.items() if key != "state_hash"})}
    _collector_json(session_path, session)
    snapshot_path = item_root / "integration" / "staging" / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["state"] = session
    snapshot["snapshot_hash"] = _collector_hash({key: item for key, item in snapshot.items() if key != "snapshot_hash"})
    _collector_json(snapshot_path, snapshot)
    with pytest.raises(ReportPreflightError, match="integration session binding"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


@pytest.mark.parametrize("location,field,value,pattern", [
    ("intent", "manifest_hash", "f" * 64, "terminal intent"),
    ("intent", "outcome", "accepted_with_limits", "terminal intent"),
    ("outcome", "content_hash", "f" * 64, "terminal outcome"),
    ("outcome", "outcome", "accepted_with_limits", "terminal outcome"),
])
def test_gather_from_run_rejects_terminal_state_cross_links(tmp_path: Path, location: str, field: str, value: str, pattern: str) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    path = context.run_root / "requirements" / "REQ-01" / "item_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["terminal_intent" if location == "intent" else "terminal_outcome"][field] = value
    _collector_json(path, state)
    with pytest.raises(ReportPreflightError, match=pattern):
        RunReportInputGatherer.gather_from_run(context, persist=False)


@pytest.mark.parametrize("field,value", [
    ("outcome", "accepted_with_limits"),
    ("reviewer_ref", "reviewer-foreign"),
    ("draft_hash", "f" * 64),
    ("content_hash", "e" * 64),
])
def test_gather_from_run_rejects_rehashed_acceptance_envelope_cross_links(tmp_path: Path, field: str, value: str) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    item_root = context.run_root / "requirements" / "REQ-01"
    envelope_path = item_root / "accepted" / "acceptance_envelope.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope[field] = value
    _collector_json(envelope_path, envelope)
    accepted_path = item_root / "accepted" / "manifest.json"
    accepted = json.loads(accepted_path.read_text(encoding="utf-8"))
    accepted["envelope_hash"] = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    accepted["manifest_hash"] = _collector_hash({key: item for key, item in accepted.items() if key != "manifest_hash"})
    _collector_json(accepted_path, accepted)
    committed_path = item_root / "integration" / "committed" / "manifest.json"
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    committed["accepted_manifest_hash"] = accepted["manifest_hash"]
    committed["manifest_hash"] = _collector_hash({key: item for key, item in committed.items() if key != "manifest_hash"})
    _collector_json(committed_path, committed)
    session_path = item_root / "integration" / "staging" / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["accepted_manifest_hash"] = accepted["manifest_hash"]
    session["state_hash"] = _collector_hash({key: item for key, item in session.items() if key != "state_hash"})
    _collector_json(session_path, session)
    snapshot_path = item_root / "integration" / "staging" / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["state"] = session
    snapshot["snapshot_hash"] = _collector_hash({key: item for key, item in snapshot.items() if key != "snapshot_hash"})
    _collector_json(snapshot_path, snapshot)
    state_path = item_root / "item_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["terminal_intent"]["manifest_hash"] = accepted["manifest_hash"]
    _collector_json(state_path, state)
    with pytest.raises(ReportPreflightError, match="acceptance envelope binding"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


@pytest.mark.parametrize("tamper", ["scope", "hash"])
def test_gather_from_run_uses_complete_business_review_validation(tmp_path: Path, tamper: str) -> None:
    context, _product_path = _collector_fixture(tmp_path)
    business_path = context.run_root / "requirements" / "REQ-01" / "work" / "business_review.json"
    business = json.loads(business_path.read_text(encoding="utf-8"))
    if tamper == "scope":
        business["findings"][0]["pointers"] = ["/scope"]
    else:
        business["before_hash"] = "0" * 64
    _collector_json(business_path, business)
    with pytest.raises(ReportPreflightError, match="business review"):
        RunReportInputGatherer.gather_from_run(context, persist=False)


def _persist_preflight(root: Path, run_id: str, report: dict) -> object:
    identity = dict(report.get("implementation") or {})
    if "sha" not in identity and len(identity) == 1:
        candidate = next(iter(identity.values()))
        if isinstance(candidate, dict):
            identity = dict(candidate)
    bindings = RunReportEventBindings(
        item_manifests=tuple(
            {
                "item_id": item["item_id"],
                "outcome": item["outcome"],
                "lifecycle_state": item.get("lifecycle_state"),
                "record_kind_totals": item.get("record_kind_totals", {}),
            }
            for item in report["items"]
        ),
        registry_snapshot={},
        lem_snapshot={},
        invocation_receipts=tuple(report.get("receipts", ())),
        timings=tuple(report.get("timings", ())),
        incidents=tuple(report.get("incidents", ())),
        business_reviews=tuple(report.get("business_reviews", ())),
        fidelity_reviews=tuple(report.get("fidelity_reviews", ())),
        implementation_transitions=tuple(report.get("implementation_transitions", ())),
        implementation_metadata=dict(report.get("implementation_metadata") or {"final": identity}),
        implementation_identity=identity,
    )
    return RunReportInputGatherer(root, run_id=run_id).gather(
        report["items"],
        event_bindings=bindings,
        lifecycle_status=report["lifecycle_status"],
    )


def test_report_projector_requires_lifecycle_on_authoritative_item_report() -> None:
    with pytest.raises(ValueError, match="lifecycle and outcome"):
        RunReportProjector(run_id="RUN-MISSING-LIFECYCLE").project(
            [
                {
                    "item_id": "Q-001",
                    "outcome": "blocked_by_evidence",
                    "record_kind_totals": {},
                    "implementation_sha": "a" * 40,
                    "implementation_tree": "b" * 40,
                    "implementation_version": "0.6.3",
                }
            ],
            lifecycle_status="analytical_complete",
        )


def test_report_finalizer_requires_lifecycle_on_projected_item() -> None:
    report = RunReportProjector(run_id="RUN-PROJECTED-MISSING-LIFECYCLE").project(
        [
            {
                "item_id": "Q-001",
                "outcome": "blocked_by_evidence",
                "lifecycle_state": "blocked_by_evidence",
                "record_kind_totals": {},
                "implementation_sha": "a" * 40,
                "implementation_tree": "b" * 40,
                "implementation_version": "0.6.3",
            }
        ],
        lifecycle_status="analytical_complete",
    )
    report["items"][0].pop("lifecycle_state")
    with pytest.raises(ValueError, match="lifecycle and outcome"):
        RunReportFinalizer().finalize(report)


def test_business_review_scopes_multiple_findings_and_targeted_rereview(tmp_path: Path) -> None:
    workspace = ItemWorkspace.create(RunContext("RUN-REVIEW", tmp_path / "run"), "Q-001", original_text="bounded")
    workspace.write_draft({"answer": {"late": 1, "partial": 2}, "unrelated": "keep"})
    review = workspace.record_review(
        "repair_once",
        reviewer_ref="review-1",
        findings=[
            {"finding_id": "F-LATE", "pointers": ["/answer/late", "/derived/late"], "semantic_categories": ["answer"]},
            {"finding_id": "F-PARTIAL", "pointers": ["/answer/partial"], "semantic_categories": ["answer"]},
        ],
    )
    assert review["finding_count"] == 2
    reviewed_hash = review["draft_hash"]
    scope = workspace.use_business_repair(owner_ref="owner")
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
                "implementation_version": "0.6.3",
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
        business_reviews=[{"review_id": "BR-1", "item_id": "Q-001", "verdict": "accept", "findings": [], "repairs": [], "targeted_rechecks": []}],
        fidelity_reviews=[{"review_id": "FR-1", "item_id": "Q-001", "verdict": "pass", "findings": [], "repairs": [], "targeted_rechecks": []}],
        lifecycle_status="complete",
    )
    root = tmp_path / "run"
    preflight = _persist_preflight(root, "RUN-REPORT", report)
    finalizer = RunReportFinalizer(root)
    receipt = finalizer.finalize(preflight)
    assert finalizer.finalize(preflight) == receipt
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
        RunReportFinalizer().finalize(stale_incident_report, authoritative_incidents=report["incidents"])
    tampered_incident_report = dict(report)
    tampered_incident_report["incidents"] = [dict(report["incidents"][0], disposition="reopened")]
    with pytest.raises(ValueError, match="incident facts are stale"):
        RunReportFinalizer().finalize(tampered_incident_report, authoritative_incidents=report["incidents"])
    stale_report = dict(report)
    stale_report["outcome_counts"] = {"technical_failure": 1}
    with pytest.raises(ValueError, match="outcome_counts are stale"):
        RunReportFinalizer().finalize(stale_report, authoritative_incidents=report["incidents"])
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["incident_count"] = 0
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="hash|stale|preflight"):
        finalizer.finalize(report, authoritative_incidents=report["incidents"])
    with pytest.raises(ValueError, match="40 lowercase"):
        projector.project(
            [
                    {
                        "item_id": "Q-001",
                        "outcome": "accepted",
                        "lifecycle_state": "accepted",
                        "implementation_sha": "a" * 41,
                    "implementation_tree": "b" * 40,
                    "implementation_version": "0.6.3",
                }
            ]
        )
        with pytest.raises(ValueError, match="requires exact sha, tree, and version"):
            projector.project([{"item_id": "Q-001", "outcome": "accepted", "lifecycle_state": "accepted"}])


def test_report_projector_roundtrips_authoritative_reviews_receipts_timings_and_transition(tmp_path: Path) -> None:
    projector = RunReportProjector(run_id="RUN-ROUNDTRIP")
    item = {
        "item_id": "Q-001",
        "outcome": "accepted",
        "lifecycle_state": "accepted",
        "record_kind_totals": {"claim": 2},
        "implementation_sha": "a" * 40,
        "implementation_tree": "b" * 40,
        "implementation_version": "0.6.3",
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
                {"finding_id": "F-2", "pointers": ["/answer/late"], "dependent_outputs": ["/derived/late"], "semantic_categories": ["answer"]},
                {"finding_id": "F-1", "artifact_paths": ["work/analysis.json"], "semantic_categories": ["answer"]},
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
    root = tmp_path / "run"
    preflight = _persist_preflight(root, "RUN-ROUNDTRIP", report)
    finalizer = RunReportFinalizer(root)
    receipt = finalizer.finalize(preflight)
    assert finalizer.finalize(preflight) == receipt
    assert receipt["business_review_count"] == 1
    stale = dict(report)
    stale["business_reviews"] = []
    stale["business_review_count"] = 0
    with pytest.raises(ValueError, match="business review records are stale"):
        RunReportFinalizer().finalize(stale, authoritative_business_reviews=business_reviews)
    stale_receipts = dict(report)
    stale_receipts["receipts"] = receipts[:1]
    stale_receipts["receipt_count"] = 1
    with pytest.raises(ValueError, match="stale"):
        RunReportFinalizer().finalize(stale_receipts, authoritative_receipts=receipts)
    tampered_timing = dict(report)
    tampered_timing["timings"] = [dict(timings[0], provider="tampered"), timings[1]]
    with pytest.raises(ValueError, match="timings are stale"):
        RunReportFinalizer().finalize(tampered_timing, authoritative_timings=timings)
    tampered = dict(report)
    tampered["implementation_transitions"] = [dict(transitions[0], resume_point="Q-001:products")]
    with pytest.raises(ValueError, match="implementation transitions are stale"):
        RunReportFinalizer().finalize(tampered, authoritative_implementation_transitions=transitions)
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
        preserved_accepted_hashes={},
        unaffected_reason="no prior accepted items in this standalone lifecycle",
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
