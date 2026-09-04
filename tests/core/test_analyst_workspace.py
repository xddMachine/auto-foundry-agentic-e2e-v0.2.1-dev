from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import textwrap
import zipfile
from typing import Any, Iterable

import pytest

import auto_foundry_core.analysis as analysis_module

from auto_foundry_core import (
    AnalystAnswer,
    AnalystWorkspace,
    BoundAnalysisContext,
    BusinessReviewAdapter,
    DataAssetRef,
    DataInsufficiencyConclusion,
    DataRoomWorkbench,
    EvidenceNote,
    ItemWorkspace,
    KnowledgeDelta,
    LEMRef,
    load_bound_analysis_context,
    OntologyItem,
    PreparedAssetRegistry,
    ReviewFinding,
    RequirementAnalysisPlan,
    RequirementAnalysisTask,
    RunContext,
    SpecialistMemo,
    SpecialistTask,
    SemanticSnapshotStore,
)
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.lifecycle import AgentInvocationReceipt, InvocationReceiptLedger, RunLifecycle
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.analyst_workspace import AnalyticalRelationshipEvidence, IdentityDomainProposal


def _workspace(tmp_path: Path) -> tuple[AnalystWorkspace, ItemWorkspace]:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,status,amount\nO-1,closed,10\nO-2,open,20\n")
        output.writestr("policy.txt", "Synthetic policy: report only supplied observations.\n")
    context = RunContext("RUN-ANALYST-FACADE", tmp_path / "run", (input_root,))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, "Q-001", original_text="Summarize the supplied synthetic orders.")
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        workbench=workbench,
    )
    return AnalystWorkspace(bound, owner_ref="owner-Q-001"), item


def _workspace_many_sources(tmp_path: Path, count: int = 24) -> tuple[AnalystWorkspace, ItemWorkspace]:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture-many-sources.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for index in range(count):
            output.writestr(
                f"source-{index:02d}.csv",
                f"source_id,value\nS-{index:02d},{index}\n",
            )
    context = RunContext("RUN-ANALYST-SOURCE-REPLACEMENT", tmp_path / "run", (input_root,))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, "Q-SOURCE-REPLACEMENT", original_text="Replace source selections.")
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        workbench=workbench,
    )
    return AnalystWorkspace(bound, owner_ref="owner-source-replacement"), item


def _active_repair(tmp_path: Path) -> tuple[ReviewFinding, ItemWorkspace]:
    """Create a current provenance-bearing active packet for concurrency tests."""

    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial narrative.", method="Initial method."))
    finding = ReviewFinding(
        finding_id="Q001-BR-RACE",
        target_sections=("method",),
        semantic_categories=("method",),
        problem="The method needs a controlled recomputation.",
        evidence="The original run has no durable execution receipt.",
        required_change="Run the bounded calculation and revise the coherent narrative.",
    )
    analyst.review.record("repair_once", reviewer_ref="current-reviewer", findings=(finding,))
    analyst.review.begin_repair()
    return finding, item


def test_analysis_owner_binding_is_stable_and_replacement_facade_adopts_it(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    owner_path = item.analysis_owner_path
    owner_bytes = owner_path.read_bytes()
    reloaded = ItemWorkspace.load(item.context, item.item_id)
    assert reloaded.bind_analysis_owner("owner-Q-001") == "owner-Q-001"
    assert owner_path.read_bytes() == owner_bytes

    assert reloaded.bind_analysis_owner("owner-Q-002") == "owner-Q-001"
    assert owner_path.read_bytes() == owner_bytes
    replacement = AnalystWorkspace(analyst.context, owner_ref="owner-Q-002")
    assert replacement.owner_ref == "owner-Q-001"
    assert reloaded.analysis_owner_ref() == "owner-Q-001"
    with reloaded._state_transition_lock():  # noqa: SLF001
        reloaded._reload_authoritative_state_locked()  # noqa: SLF001
        with pytest.raises(ValueError, match="does not match the bound item owner"):
            reloaded._verify_analysis_owner_locked("owner-Q-002")  # noqa: SLF001
    assert item.state["business_repair_count"] == 0
    assert analyst.owner_ref == "owner-Q-001"


def test_analyst_facade_owns_question_sources_specialists_and_answer(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)

    brief = analyst.brief()
    assert brief.item_id == "Q-001"
    assert brief.question == "Summarize the supplied synthetic orders."
    assert "data_quality" in brief.available_specialties

    orders = analyst.search_sources("orders", limit=1)[0]
    assert orders.path == "orders.csv"
    assert orders.columns == ("order_id", "status", "amount")
    assert analyst.sample_source(orders.source_id, limit=1)[0]["order_id"] == "O-1"
    assert analyst.source_categories(orders.source_id, "status") == ("closed", "open")
    candidate = analyst.prepare_data(
        "orders-summary",
        next(entry for entry in analyst.context.source_catalog.entries if entry.path == "orders.csv"),
        scope="reusable",
        transformations=("bounded_fixture_copy",),
    )
    assert candidate.prepared_asset_id == "orders-summary"
    assert candidate.row_count == 2

    analyst.begin_analysis(
        objective="Explain the bounded order population.",
        strategy="Measure supplied rows and disclose the synthetic scope.",
        expected_outputs=("business answer", "status comparison"),
    )
    analyst.select_sources((orders.source_id,), purpose="Primary order population")
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-001",
            conclusion="The fixture contains two orders.",
            method="Count the supplied CSV rows after its header.",
            evidence_refs=(orders.source_id,),
            limitations=("Synthetic fixture only",),
            facts={"order_count": 2},
        )
    )
    analyst.assign_specialist(
        SpecialistTask(
            task_id="S-001",
            specialty="data_quality",
            question="Check order ID uniqueness and status completeness.",
            expected_output="A short evidence memo, not the final answer.",
            source_ids=(orders.source_id,),
        )
    )
    analyst.record_specialist_memo(
        SpecialistMemo(
            memo_id="M-001",
            task_id="S-001",
            conclusion="Order IDs are unique and both statuses are populated.",
            method="Inspect both bounded fixture rows.",
            evidence_refs=(orders.source_id,),
            confidence="high",
        )
    )
    payload = analyst.submit_answer(
        AnalystAnswer(
            answer="The supplied fixture contains two orders: one open and one closed.",
            headline_findings=("Two supplied orders", "One open and one closed"),
            scope="Synthetic fixture rows only.",
            method="Count rows and group by status.",
            limitations=("No causal or enterprise-wide inference",),
            evidence_refs=("work/evidence.jsonl#E-001", "work/specialist_memos.jsonl#M-001"),
        )
    )

    assert payload["answer"].startswith("The supplied fixture")
    assert (item.work_root / "evidence.jsonl").is_file()
    assert (item.work_root / "specialist_tasks.jsonl").is_file()
    assert (item.work_root / "specialist_memos.jsonl").is_file()
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["schema_version"] == "auto_foundry.analyst_answer.v1"


def test_selected_sources_reads_persisted_array_and_rejects_malformed_map(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    orders = analyst.search_sources("orders", limit=1)[0]
    selected = analyst.select_sources((orders.source_id,), purpose="Primary order population")

    persisted = json.loads((item.work_root / "source_map.json").read_text(encoding="utf-8"))
    assert isinstance(persisted, list)
    assert len(persisted) == 1
    assert persisted[0]["source_id"] == orders.source_id
    assert analyst.selected_sources() == selected

    (item.work_root / "source_map.json").write_text(json.dumps({"source_id": orders.source_id}), encoding="utf-8")
    with pytest.raises(ValueError, match="source_map.json is invalid"):
        analyst.selected_sources()


def test_replace_selected_sources_collapses_duplicate_rows_with_cas_and_exact_retry(tmp_path: Path) -> None:
    analyst, item = _workspace_many_sources(tmp_path)
    sources = analyst.search_sources("", limit=100)
    assert len(sources) == 24
    analyst.select_sources(tuple(source.source_id for source in sources), purpose="Initial source scope")

    source_map = item.work_root / "source_map.json"
    rows = json.loads(source_map.read_text(encoding="utf-8"))
    assert len(rows) == 24
    rows.append({**rows[0], "purpose": "Duplicate source scope to be removed"})
    source_map.write_text(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    assert len(json.loads(source_map.read_text(encoding="utf-8"))) == 25

    unique_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row["source_id"] in seen:
            continue
        unique_rows.append(row)
        seen.add(row["source_id"])
    expected = hashlib.sha256(source_map.read_bytes()).hexdigest()
    replaced = analyst.replace_selected_sources(unique_rows, expected_artifact_hash=expected)
    assert tuple(source.source_id for source in replaced) == tuple(source.source_id for source in sources)
    assert len(json.loads(source_map.read_text(encoding="utf-8"))) == 24
    replacement_bytes = source_map.read_bytes()

    state_path = item.item_root / "item_state.json"
    state_bytes = state_path.read_bytes()
    source_mtime = source_map.stat().st_mtime_ns
    replacement_hash = hashlib.sha256(replacement_bytes).hexdigest()
    retried = analyst.replace_selected_sources(unique_rows, expected_artifact_hash=replacement_hash)
    assert tuple(source.source_id for source in retried) == tuple(source.source_id for source in sources)
    assert source_map.read_bytes() == replacement_bytes
    assert state_path.read_bytes() == state_bytes
    assert source_map.stat().st_mtime_ns == source_mtime
    assert tuple(source.source_id for source in analyst.selected_sources()) == tuple(source.source_id for source in sources)


def test_replace_selected_sources_rejects_cas_owner_and_identity_drift_without_mutation(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    orders = analyst.search_sources("orders", limit=1)[0]
    analyst.select_sources((orders.source_id,), purpose="Primary order population")
    source_map = item.work_root / "source_map.json"
    current = source_map.read_bytes()
    expected = hashlib.sha256(current).hexdigest()
    row = json.loads(current)[0]

    with pytest.raises(ValueError, match="expected_artifact_hash"):
        analyst.replace_selected_sources((row,), expected_artifact_hash="0" * 64)
    assert source_map.read_bytes() == current

    with pytest.raises(ValueError, match="source map source_id values must be unique"):
        analyst.replace_selected_sources((row, {**row, "purpose": "Conflicting duplicate"}), expected_artifact_hash=expected)
    assert source_map.read_bytes() == current

    foreign = {**row, "source_id": "foreign.csv"}
    with pytest.raises(ValueError, match="not in the bound catalog"):
        analyst.replace_selected_sources((foreign,), expected_artifact_hash=expected)
    assert source_map.read_bytes() == current

    tampered = {**row, "content_hash": "0" * 64}
    with pytest.raises(ValueError, match="does not match the bound catalog member"):
        analyst.replace_selected_sources((tampered,), expected_artifact_hash=expected)
    assert source_map.read_bytes() == current

    with pytest.raises(ValueError, match="owner"):
        item.replace_source_map((row,), owner_ref="different-owner", expected_artifact_hash=expected)
    assert source_map.read_bytes() == current


def test_replace_selected_sources_revalidates_bound_catalog_before_mutation(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    orders = analyst.search_sources("orders", limit=1)[0]
    analyst.select_sources((orders.source_id,), purpose="Primary order population")
    source_map = item.work_root / "source_map.json"
    current = source_map.read_bytes()
    expected = hashlib.sha256(current).hexdigest()
    state_path = item.item_root / "item_state.json"
    state_before = state_path.read_bytes()
    source_mtime = source_map.stat().st_mtime_ns
    row = json.loads(current)[0]

    catalog_path = analyst.context.source_catalog.path
    catalog_before = catalog_path.read_bytes()
    catalog_path.write_bytes(catalog_before + b"tampered")
    try:
        with pytest.raises(ValueError, match="analysis catalog changed"):
            analyst.replace_selected_sources((row,), expected_artifact_hash=expected)
    finally:
        catalog_path.write_bytes(catalog_before)

    assert source_map.read_bytes() == current
    assert state_path.read_bytes() == state_before
    assert source_map.stat().st_mtime_ns == source_mtime


def test_business_review_uses_semantic_provenance_and_item_local_paths(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.begin_analysis(objective="Count orders.", strategy="Use the bounded CSV.")
    calculation = item.work_root / "calculations" / "count.py"
    calculation.parent.mkdir(parents=True)
    calculation.write_text("ORDER_COUNT = 1\n", encoding="utf-8")
    analyst.submit_answer(AnalystAnswer(answer="There is one supplied order.", method="Count rows."))

    initial = analyst.review.record(
        "repair_once",
        reviewer_ref="independent-business-reviewer",
        findings=(
            ReviewFinding(
                finding_id="BR-001",
                target_sections=("answer",),
                semantic_categories=("calculation",),
                problem="The answer counts one row instead of two.",
                evidence="orders.csv contains O-1 and O-2.",
                required_change="Correct the calculation and answer to two orders.",
            ),
        ),
    )
    assert initial["finding_count"] == 1
    packet = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    assert packet["allowed_pointers"] == ["/answer", "/next_actions", "/scope"]
    assert packet["allowed_artifact_paths"] == ["work/results"]
    assert "work/calculations" in packet["allowed_dependencies"]
    assert packet["findings"][0]["semantic_categories"] == ["calculation"]
    reloaded_packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert reloaded_packet["findings"][0]["semantic_categories"] == ["calculation"]

    analyst.review.begin_repair()
    item.write_open_issues({"unrelated": True})
    assert json.loads((item.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": True
    }
    calculation.write_text("ORDER_COUNT = 2\n", encoding="utf-8")
    analyst.submit_answer(AnalystAnswer(answer="There are two supplied orders.", method="Count rows."))
    targeted = analyst.review.record("accept", reviewer_ref="targeted-business-reviewer")
    assert targeted["targeted_recheck"] is True
    assert targeted["changed_pointers"] == ["/answer"]
    snapshot = item.accept(accepted_refs=("work/plan.json", "work/calculations/count.py"))
    assert snapshot.outcome == "accepted"


def test_business_review_rejects_scope_pointer_for_evidence_category(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial bounded answer."))
    state_path = item.item_root / "item_state.json"
    state_before = state_path.read_bytes()
    with pytest.raises(ValueError, match="canonical answer pointers"):
        analyst.review.record(
            "repair_once",
            reviewer_ref="reviewer",
            findings=(
                ReviewFinding(
                    finding_id="BR-EVIDENCE-SCOPE-POINTER",
                    target_sections=("scope",),
                    semantic_categories=("evidence",),
                    problem="Evidence needs correction.",
                    evidence="The current evidence route is incomplete.",
                    required_change="Record the missing evidence.",
                ),
            ),
        )
    assert state_path.read_bytes() == state_before
    assert not item.business_review_path.exists()


def test_direct_business_review_rejects_scope_dependency_pointer_for_evidence_category(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial bounded answer."))
    state_path = item.item_root / "item_state.json"
    state_before = state_path.read_bytes()
    with pytest.raises(ValueError, match="canonical answer pointers"):
        item.record_review(
            "repair_once",
            reviewer_ref="reviewer",
            findings=(
                {
                    "finding_id": "BR-EVIDENCE-SCOPE-DEPENDENCY",
                    "pointers": ["/answer"],
                    "dependent_outputs": ["/scope/period"],
                    "semantic_categories": ["evidence"],
                },
            ),
        )
    assert state_path.read_bytes() == state_before
    assert not item.business_review_path.exists()


@pytest.mark.parametrize("pointer", ("/scope", "/scope/period", "/next_actions", "/next_actions/0"))
def test_direct_rehashed_evidence_packet_cannot_authorize_canonical_answer_pointer(
    tmp_path: Path,
    pointer: str,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial bounded answer."))
    analyst.review.record(
        "repair_once",
        reviewer_ref="reviewer",
        findings=(
            ReviewFinding(
                finding_id="BR-EVIDENCE-REHASHED-POINTER",
                target_sections=("method",),
                semantic_categories=("evidence",),
                problem="Evidence needs correction.",
                evidence="The current evidence route is incomplete.",
                required_change="Record the missing evidence.",
            ),
        ),
    )
    packet_path = item.business_review_path
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["findings"][0]["pointers"].append(pointer)
    packet["findings"][0]["pointers"].sort()
    packet["allowed_pointers"].append(pointer)
    packet["allowed_pointers"].sort()
    packet_path.write_text(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    packet_before = packet_path.read_bytes()
    state_path = item.item_root / "item_state.json"
    state_before = state_path.read_bytes()
    reloaded = ItemWorkspace.load(item.context, item.item_id)
    with pytest.raises(ValueError, match="canonical answer pointers"):
        reloaded.use_business_repair(owner_ref="owner-Q-001")
    assert packet_path.read_bytes() == packet_before
    assert state_path.read_bytes() == state_before


def test_evidence_repair_can_bind_new_evidence_ref_and_other_item_local_outputs(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded result.",
            method="Initial method.",
            evidence_refs=("work/evidence.jsonl#E-OLD",),
        )
    )
    finding = ReviewFinding(
        finding_id="BR-EVIDENCE-BIND",
        target_sections=("method",),
        semantic_categories=("evidence", "source_completeness"),
        problem="The answer must bind the newly collected evidence.",
        evidence="The prior evidence route was incomplete.",
        required_change="Record the missing evidence and cite it in the answer.",
    )
    review = analyst.review.record("repair_once", reviewer_ref="reviewer-evidence", findings=(finding,))
    assert review["verdict"] == "repair_once"
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert packet["allowed_pointers"] == ["/answer", "/evidence_refs", "/method"]
    assert packet["findings"][0]["pointers"] == packet["allowed_pointers"]

    analyst.review.begin_repair()
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-NEW",
            conclusion="The bounded source contains the missing observation.",
            method="Inspect the supplied evidence record.",
            evidence_refs=("orders.csv",),
        )
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="Corrected bounded result.",
            method="Corrected method.",
            limitations=("Unrelated limitation change.",),
            evidence_refs=("work/evidence.jsonl#E-NEW",),
        )
    )
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["limitations"] == [
        "Unrelated limitation change."
    ]
    item.write_open_issues({"unrelated": True})
    assert json.loads((item.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": True
    }
    analyst.submit_answer(
        AnalystAnswer(
            answer="Corrected bounded result.",
            method="Corrected method.",
            evidence_refs=("work/evidence.jsonl#E-NEW",),
        )
    )
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["evidence_refs"] == [
        "work/evidence.jsonl#E-NEW"
    ]


def test_answer_category_scope_covers_canonical_fields_and_item_local_results(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.begin_analysis(objective="Recompute the bounded order answer.", strategy="Run the controlled calculation.")
    script = item.work_root / "calculations" / "result.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "from pathlib import Path\n"
        "result = Path('results/result.json')\n"
        "result.parent.mkdir(parents=True, exist_ok=True)\n"
        "result.write_text('{\"order_count\": 2}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="The initial bounded answer is provisional.",
            scope="Initial fixture scope.",
            next_actions=("Recompute the supplied order count.",),
            method="Initial method.",
        )
    )
    finding = ReviewFinding(
        finding_id="BR-ANSWER-RESULTS-ROOT",
        target_sections=("answer", "scope", "next_actions"),
        semantic_categories=("answer", "calculation", "presentation"),
        problem="The answer and controlled result need one bounded correction.",
        evidence="The initial calculation is provisional.",
        required_change="Recompute the result and revise the canonical answer fields.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer", findings=(finding,))
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert packet["findings"][0]["pointers"] == ["/answer", "/next_actions", "/scope"]
    assert packet["allowed_pointers"] == ["/answer", "/next_actions", "/scope"]
    assert packet["allowed_artifact_paths"] == ["work/results"]

    analyst.review.begin_repair()
    result_path = item.work_root / "results" / "result.json"
    report = analyst.run_analysis(script, outputs=(result_path,))
    assert report.succeeded
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-ANSWER-RESULTS-ROOT",
            conclusion="The controlled result contains two supplied orders.",
            method="Run the bounded calculation.",
            evidence_refs=("work/results/result.json",),
        )
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="The supplied fixture contains two orders.",
            scope="Supplied fixture rows only.",
            next_actions=("Reuse only this bounded result.",),
            method="Initial method.",
        )
    )
    item.write_open_issues({"unrelated": True})
    assert json.loads((item.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": True
    }
    targeted = analyst.review.record("accept", reviewer_ref="targeted-reviewer")
    assert targeted["targeted_recheck"] is True
    assert "/next_actions/0" in targeted["changed_pointers"]
    assert "/scope" in targeted["changed_pointers"]
    assert result_path.is_file()


def test_active_evidence_repair_reconciliation_preserves_current_scope(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(
        AnalystAnswer(answer="Initial bounded result.", method="Initial method.")
    )
    finding = ReviewFinding(
        finding_id="BR-EVIDENCE-LEGACY",
        target_sections=("method",),
        semantic_categories=("evidence", "source_completeness"),
        problem="The source route needs one more evidence binding.",
        evidence="The current packet binds evidence references programmatically.",
        required_change="Record and cite the missing evidence.",
    )
    legacy_core = {
        "finding_id": finding.finding_id,
        "message": (
            f"Problem: {finding.problem}\n"
            f"Evidence: {finding.evidence}\n"
            f"Required change: {finding.required_change}"
        ),
        "pointers": ["/answer", "/method"],
        "artifact_paths": [],
        "dependent_outputs": [
            "work/evidence.jsonl",
            "work/source_map.json",
            "work/specialist_memos.jsonl",
        ],
        "material": True,
    }
    analyst.review.record("repair_once", reviewer_ref="current-reviewer", findings=(finding,))
    analyst.review.begin_repair()
    packet_before = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    baseline = {
        field: packet_before[field]
        for field in (
            "before_snapshot",
            "before_hash",
            "before_pointer_hashes",
            "before_artifact_hashes",
            "reviewed_draft_hash",
            "changed_pointers",
            "unchanged_paths",
            "unchanged_aggregate_hash",
        )
    }
    count_before = item.state["business_repair_count"]
    upgraded = analyst.review.reconcile_active_repair_scope((finding,))
    assert upgraded["allowed_pointers"] == ["/answer", "/evidence_refs", "/method"]
    assert list(upgraded["findings"][0]["pointers"]) == upgraded["allowed_pointers"]
    assert item.state["business_repair_count"] == count_before == 1
    packet_after = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert all(packet_after[field] == value for field, value in baseline.items())
    assert packet_after["allowed_pointers"] == packet_before["allowed_pointers"]
    assert packet_after["allowed_dependencies"] == packet_before["allowed_dependencies"]
    assert packet_after["allowed_artifact_paths"] == packet_before["allowed_artifact_paths"]
    assert packet_after["findings"][0]["semantic_categories"] == list(finding.semantic_categories)


def test_current_q001_multi_category_reconciliation_is_exact_and_tamper_atomic(tmp_path: Path) -> None:
    """Current Q001-shaped multi-category scope is immutable and exact."""

    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded answer.",
            headline_findings=("Initial finding",),
            scope="Supplied fixture only.",
            method="Initial method.",
            supported_components=("Initial support",),
            unsupported_components=("Initial limitation",),
            limitations=("Initial limits",),
            next_actions=("Initial next action",),
            visuals=({"title": "Initial visual"},),
        )
    )
    finding = ReviewFinding(
        finding_id="Q001-R1-LIVE-MULTI",
        target_sections=(
            "answer",
            "headline_findings",
            "scope",
            "method",
            "supported_components",
            "unsupported_components",
            "limitations",
            "next_actions",
            "visuals",
        ),
        semantic_categories=(
            "answer",
            "calculation",
            "evidence",
            "method",
            "source_completeness",
            "presentation",
        ),
        problem="The Q001 answer needs a complete bounded correction.",
        evidence="The live packet combines calculation, evidence, method, and presentation findings.",
        required_change="Recompute the result, bind evidence, and revise the complete answer.",
    )
    analyst.review.record("repair_once", reviewer_ref="live-reviewer", findings=(finding,))
    analyst.review.begin_repair()

    packet_path = item.business_review_path
    state_path = item.item_root / "item_state.json"
    owner_path = item.analysis_owner_path
    packet_before = json.loads(packet_path.read_text(encoding="utf-8"))
    state_before = item.state
    owner_before = owner_path.read_bytes()
    baseline = {
        field: packet_before[field]
        for field in (
            "before_snapshot",
            "before_hash",
            "before_pointer_hashes",
            "before_artifact_hashes",
            "reviewed_draft_hash",
            "changed_pointers",
            "unchanged_paths",
            "unchanged_aggregate_hash",
        )
    }
    expected_scope = {
        "allowed_pointers": packet_before["allowed_pointers"],
        "allowed_artifact_paths": packet_before["allowed_artifact_paths"],
        "allowed_dependencies": packet_before["allowed_dependencies"],
    }
    assert packet_before["findings"][0]["semantic_categories"] == list(finding.semantic_categories)
    assert "/evidence_refs" in packet_before["allowed_pointers"]

    upgraded = analyst.review.reconcile_active_repair_scope((finding,))
    assert tuple(upgraded["findings"][0]["semantic_categories"]) == finding.semantic_categories
    assert upgraded["allowed_pointers"] == expected_scope["allowed_pointers"]
    assert upgraded["allowed_artifact_paths"] == expected_scope["allowed_artifact_paths"]
    assert upgraded["allowed_dependencies"] == expected_scope["allowed_dependencies"]
    packet_after = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet_after["findings"][0]["semantic_categories"] == list(finding.semantic_categories)
    assert all(packet_after[field] == value for field, value in baseline.items())
    assert item.state["business_repair_count"] == state_before["business_repair_count"] == 1
    for field in state_before:
        if field != "updated_at":
            assert item.state[field] == state_before[field]
    assert owner_path.read_bytes() == owner_before

    packet_bytes_before = packet_path.read_bytes()
    state_bytes_before = state_path.read_bytes()
    invalid_findings = (
        ReviewFinding(
            finding_id=finding.finding_id,
            target_sections=finding.target_sections,
            semantic_categories=("answer", "calculation", "evidence", "method", "source_completeness"),
            problem=finding.problem,
            evidence=finding.evidence,
            required_change=finding.required_change,
        ),
        ReviewFinding(
            finding_id=finding.finding_id,
            target_sections=tuple(section for section in finding.target_sections if section != "method"),
            semantic_categories=finding.semantic_categories,
            problem=finding.problem,
            evidence=finding.evidence,
            required_change=finding.required_change,
        ),
        ReviewFinding(
            finding_id=finding.finding_id,
            target_sections=finding.target_sections,
            semantic_categories=finding.semantic_categories,
            problem="Changed Q001 finding.",
            evidence=finding.evidence,
            required_change=finding.required_change,
        ),
    )
    for invalid in invalid_findings:
        with pytest.raises(ValueError):
            analyst.review.reconcile_active_repair_scope((invalid,))
        assert packet_path.read_bytes() == packet_bytes_before
        assert state_path.read_bytes() == state_bytes_before

    for field in ("dependent_outputs", "pointers", "artifact_paths"):
        tampered = dict(analyst.review._core_finding(finding))  # noqa: SLF001 - scope tamper regression
        if field == "dependent_outputs":
            tampered[field] = [*tampered[field], "work/tampered"]
        elif field == "pointers":
            tampered[field] = [path for path in tampered[field] if path != "/method"]
        else:
            tampered[field] = ["work/tampered-artifact"]
        with pytest.raises(ValueError):
            item._reconcile_active_business_repair_scope(  # noqa: SLF001 - aggregate tamper regression
                (tampered,),
                semantic_categories=(finding.semantic_categories,),
                owner_ref="owner-Q-001",
            )
        assert packet_path.read_bytes() == packet_bytes_before
        assert state_path.read_bytes() == state_bytes_before


def _legacy_active_q001_packet(tmp_path: Path) -> tuple[AnalystWorkspace, ItemWorkspace, ReviewFinding]:
    """Create a pre-fix active packet whose answer scope lacks the packet dependency."""

    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded answer.",
            headline_findings=("Initial finding",),
            scope="Supplied fixture only.",
            method="Initial method.",
            supported_components=("Initial support",),
            next_actions=("Initial next action",),
            visuals=({"title": "Initial visual"},),
        )
    )
    finding = ReviewFinding(
        finding_id="Q001-PACKET-REPAIR",
        target_sections=(
            "answer",
            "headline_findings",
            "scope",
            "method",
            "supported_components",
            "next_actions",
            "visuals",
        ),
        semantic_categories=(
            "answer",
            "calculation",
            "evidence",
            "method",
            "source_completeness",
            "presentation",
        ),
        problem="The bounded answer needs the complete reviewer correction.",
        evidence="The pre-fix packet predates the owner-produced review packet dependency.",
        required_change="Recompute, bind evidence, and revise the coherent answer.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer", findings=(finding,))
    analyst.review.begin_repair()

    # Model the packet emitted by the pre-fix program: the category finding
    # is otherwise identical, but it lacks only the newly derived exact file
    # dependency.  Keep the immutable before hashes untouched.
    review_packet_path = item.business_review_path
    review_packet = json.loads(review_packet_path.read_text(encoding="utf-8"))
    row = review_packet["findings"][0]
    row["dependent_outputs"].remove("work/business_review_packet.json")
    review_packet["allowed_dependencies"].remove("work/business_review_packet.json")
    review_packet_path.write_text(
        json.dumps(review_packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return analyst, item, finding


def test_active_q001_repair_reconciles_changed_business_review_packet_before_writes(
    tmp_path: Path,
) -> None:
    """Only explicit reconciliation may authorize the changed owner packet."""

    analyst, item, finding = _legacy_active_q001_packet(tmp_path)
    owner_packet = item.work_root / "business_review_packet.json"
    owner_packet.write_text('{"phase":"corrected"}\n', encoding="utf-8")
    draft_before = item.draft_root.read_bytes()
    review_before = item.business_review_path.read_bytes()
    state_before = (item.item_root / "item_state.json").read_bytes()

    # Ordinary reads/writes remain strict while the active packet is still
    # pre-fix; even the unchanged draft cannot be published through repair.
    with pytest.raises(ValueError, match="business review"):
        analyst.submit_answer(AnalystAnswer(answer="Must wait for reconciliation."))
    assert item.draft_root.read_bytes() == draft_before
    assert item.business_review_path.read_bytes() == review_before
    assert (item.item_root / "item_state.json").read_bytes() == state_before

    upgraded = analyst.review.reconcile_active_repair_scope((finding,))
    assert "work/business_review_packet.json" in upgraded["allowed_dependencies"]
    assert list(upgraded["findings"][0]["dependent_outputs"]) == [
        "work/.analysis-run",
        "work/analysis.json",
        "work/analysis.py",
        "work/business_review_packet.json",
        "work/calculations",
        "work/evidence.jsonl",
        "work/plan.json",
        "work/prepared",
        "work/script_receipts",
        "work/source_map.json",
        "work/specialist_memos.jsonl",
    ]

    # The same controlled repair can now record evidence, publish the revised
    # answer, and update any artifact under this item workspace.
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-Q001-PACKET",
            conclusion="The corrected bounded result is recorded.",
            method="Use the repaired deterministic source route.",
            evidence_refs=("work/analysis.json",),
        )
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="Corrected bounded answer.",
            headline_findings=("Corrected finding",),
            scope="Supplied fixture only.",
            method="Recomputed with the controlled source route.",
            supported_components=("Corrected support",),
            next_actions=("Validate the bounded exception queue.",),
            visuals=({"title": "Corrected visual"},),
        )
    )
    item.write_open_issues({"unrelated": True})
    assert json.loads((item.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": True
    }


@pytest.mark.parametrize("tamper", ("wrong_category", "extra_path", "rehashed_packet"))
def test_active_q001_reconciliation_rejects_scope_tamper_atomically(
    tmp_path: Path,
    tamper: str,
) -> None:
    """Legacy upgrade never accepts category, path, or coordinated rehash tampering."""

    case_root = tmp_path / tamper
    case_root.mkdir()
    analyst, item, finding = _legacy_active_q001_packet(case_root)
    review_path = item.business_review_path
    state_path = item.item_root / "item_state.json"
    owner_packet = item.work_root / "business_review_packet.json"
    owner_packet.write_text('{"phase":"corrected"}\n', encoding="utf-8")

    if tamper == "rehashed_packet":
        packet = json.loads(review_path.read_text(encoding="utf-8"))
        packet["findings"][0]["dependent_outputs"].append("work/tampered")
        packet["findings"][0]["dependent_outputs"].sort()
        packet["allowed_dependencies"].append("work/tampered")
        packet["allowed_dependencies"].sort()
        review_path.write_text(
            json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    review_before = review_path.read_bytes()
    state_before = state_path.read_bytes()
    owner_before = owner_packet.read_bytes()
    invalid_finding = finding
    if tamper == "wrong_category":
        invalid_finding = ReviewFinding(
            finding_id=finding.finding_id,
            target_sections=finding.target_sections,
            semantic_categories=("evidence",),
            problem=finding.problem,
            evidence=finding.evidence,
            required_change=finding.required_change,
        )
    elif tamper == "extra_path":
        tampered = dict(analyst.review._core_finding(finding))  # noqa: SLF001 - scope tamper regression
        tampered["dependent_outputs"] = [*tampered["dependent_outputs"], "work/tampered"]
        with pytest.raises(ValueError):
            item._reconcile_active_business_repair_scope(  # noqa: SLF001 - aggregate tamper regression
                (tampered,),
                semantic_categories=(finding.semantic_categories,),
                owner_ref="owner-Q-001",
            )
        assert review_path.read_bytes() == review_before
        assert state_path.read_bytes() == state_before
        assert owner_packet.read_bytes() == owner_before
        return

    with pytest.raises(ValueError):
        analyst.review.reconcile_active_repair_scope((invalid_finding,))
    assert review_path.read_bytes() == review_before
    assert state_path.read_bytes() == state_before
    assert owner_packet.read_bytes() == owner_before


def test_business_review_multi_category_union_is_exact_and_deterministic(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.begin_analysis(objective="Recompute the bounded order result.", strategy="Use the selected source and controlled calculation.")
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded result.",
            method="Initial method.",
            limitations=("Initial limitation.",),
        )
    )
    finding = ReviewFinding(
        finding_id="BR-MULTI-SCOPE",
        target_sections=("limitations", "method"),
        semantic_categories=("source_completeness", "calculation"),
        problem="The answer needs both source-completeness and recalculation review.",
        evidence="The source map is incomplete and the prior calculation is stale.",
        required_change="Complete the evidence route, recompute, and revise the named sections.",
    )
    assert finding.semantic_categories == ("calculation", "source_completeness")
    analyst.review.record("repair_once", reviewer_ref="independent-business-reviewer", findings=(finding,))
    packet = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    assert packet["findings"][0]["semantic_categories"] == ["calculation", "source_completeness"]
    reloaded = ItemWorkspace.load(item.context, item.item_id)
    reloaded_packet = json.loads(reloaded.business_review_path.read_text(encoding="utf-8"))
    assert reloaded_packet["findings"][0]["semantic_categories"] == [
        "calculation",
        "source_completeness",
    ]
    assert packet["allowed_pointers"] == ["/answer", "/evidence_refs", "/limitations", "/method", "/next_actions", "/scope"]
    assert packet["allowed_artifact_paths"] == ["work/results"]
    assert packet["allowed_dependencies"] == [
        "work/.analysis-run",
        "work/analysis.json",
        "work/analysis.py",
        "work/calculations",
        "work/evidence.jsonl",
        "work/prepared",
        "work/script_receipts",
        "work/source_map.json",
        "work/specialist_memos.jsonl",
    ]
    analyst.review.begin_repair()
    calculation = item.work_root / "calculations" / "recompute.py"
    calculation.parent.mkdir(parents=True)
    calculation.write_text(
        "from pathlib import Path\nPath('analysis.json').write_text('{\"status\": \"recomputed\"}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    assert analyst.run_analysis(calculation, outputs=(item.work_root / "analysis.json",)).succeeded
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-MULTI-SCOPE",
            conclusion="The bounded result was recomputed from the completed source route.",
            method="Run the controlled script and bind the evidence record to the supplied source.",
            evidence_refs=("work/analysis.json",),
        )
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="Corrected bounded result.",
            method="Recomputed with the controlled script.",
            limitations=("Source completeness remains bounded to the supplied fixture.",),
        )
    )
    targeted = analyst.review.record("accept", reviewer_ref="targeted-business-reviewer")
    assert targeted["targeted_recheck"] is True
    assert targeted["changed_pointers"] == ["/answer", "/limitations/0", "/method"]


def test_categoryless_repair_packet_fails_closed_before_mutation(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial answer."))
    categoryless = {
        "finding_id": "F-CATEGORYLESS",
        "message": "Missing semantic provenance",
        "pointers": ["/answer"],
        "artifact_paths": [],
        "dependent_outputs": [],
        "material": True,
    }
    state_path = item.item_root / "item_state.json"
    state_before = state_path.read_bytes()
    with pytest.raises(ValueError, match="semantic category provenance"):
        item.record_review("repair_once", reviewer_ref="reviewer", findings=(categoryless,))
    assert state_path.read_bytes() == state_before
    assert not item.business_review_path.exists()


@pytest.mark.parametrize("tampered_field", ("dependent_outputs", "artifact_paths", "pointers"))
def test_persisted_coordinated_scope_tamper_rejects_before_mutation(
    tmp_path: Path,
    tampered_field: str,
) -> None:
    """A rehashed/canonical-looking packet cannot expand or evade its provenance scope."""

    case_root = tmp_path / tampered_field
    case_root.mkdir()
    analyst, item = _workspace(case_root)
    analyst.submit_answer(AnalystAnswer(answer="Initial answer."))
    finding = ReviewFinding(
        finding_id=f"F-TAMPER-{tampered_field}",
        target_sections=("answer",),
        semantic_categories=("evidence",),
        problem="The answer needs a bounded evidence correction.",
        evidence="The current evidence route is incomplete.",
        required_change="Record the missing evidence and revise the answer.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer", findings=(finding,))

    packet_path = item.business_review_path
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    row = packet["findings"][0]
    if tampered_field == "dependent_outputs":
        row["dependent_outputs"].append("work/tampered-dependency")
        packet["allowed_dependencies"].append("work/tampered-dependency")
    elif tampered_field == "artifact_paths":
        row["artifact_paths"].append("work/tampered-artifact")
        packet["allowed_artifact_paths"].append("work/tampered-artifact")
    else:
        row["pointers"].remove("/evidence_refs")
        packet["allowed_pointers"].remove("/evidence_refs")
    # Persist a deterministic, coordinated aggregate rewrite.  The current
    # packet schema has no separate packet hash; finding provenance is the
    # authoritative integrity boundary.
    for field in ("pointers", "artifact_paths", "dependent_outputs"):
        row[field] = sorted(set(row[field]))
    for field in ("allowed_pointers", "allowed_artifact_paths", "allowed_dependencies"):
        packet[field] = sorted(set(packet[field]))
    packet_path.write_text(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    state_path = item.item_root / "item_state.json"
    packet_bytes = packet_path.read_bytes()
    state_bytes = state_path.read_bytes()
    journal_paths = (item.business_review_discard_audit_path, item.business_review_discard_state_path)
    journal_snapshot = {
        path: (path.exists(), path.read_bytes() if path.exists() else None)
        for path in journal_paths
    }

    reloaded = ItemWorkspace.load(item.context, item.item_id)
    with pytest.raises(ValueError, match="business review"):
        reloaded.use_business_repair(owner_ref="owner-Q-001")

    assert packet_path.read_bytes() == packet_bytes
    assert state_path.read_bytes() == state_bytes
    assert {
        path: (path.exists(), path.read_bytes() if path.exists() else None)
        for path in journal_paths
    } == journal_snapshot


def test_persisted_calculation_scope_rejects_coordinated_evidence_pointer_tamper(
    tmp_path: Path,
) -> None:
    """A calculation-only finding cannot gain an evidence binding by packet rewrite."""

    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial answer."))
    finding = ReviewFinding(
        finding_id="F-CALCULATION-TAMPER",
        target_sections=("answer",),
        semantic_categories=("calculation",),
        problem="The answer needs a bounded recalculation.",
        evidence="The current calculation is stale.",
        required_change="Recompute the bounded result and revise the answer.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer", findings=(finding,))
    packet_path = item.business_review_path
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert "/evidence_refs" not in packet["findings"][0]["pointers"]
    assert "/evidence_refs" not in packet["allowed_pointers"]

    packet["findings"][0]["pointers"].append("/evidence_refs")
    packet["findings"][0]["pointers"].sort()
    packet["allowed_pointers"].append("/evidence_refs")
    packet["allowed_pointers"].sort()
    packet_path.write_text(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    state_path = item.item_root / "item_state.json"
    packet_bytes = packet_path.read_bytes()
    state_bytes = state_path.read_bytes()
    journal_paths = (item.business_review_discard_audit_path, item.business_review_discard_state_path)
    journal_snapshot = {
        path: (path.exists(), path.read_bytes() if path.exists() else None)
        for path in journal_paths
    }
    reloaded = ItemWorkspace.load(item.context, item.item_id)
    with pytest.raises(ValueError, match="semantic categories"):
        reloaded.use_business_repair(owner_ref="owner-Q-001")

    assert packet_path.read_bytes() == packet_bytes
    assert state_path.read_bytes() == state_bytes
    assert {
        path: (path.exists(), path.read_bytes() if path.exists() else None)
        for path in journal_paths
    } == journal_snapshot


@pytest.mark.parametrize(
    "categories",
    ((), ("calculation", "calculation"), ("unknown",)),
)
def test_review_finding_rejects_empty_duplicate_and_unknown_semantic_categories(
    categories: tuple[str, ...],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ReviewFinding(
            finding_id="BR-INVALID-CATEGORIES",
            target_sections=("answer",),
            semantic_categories=categories,
            problem="A bounded problem.",
            evidence="A bounded fact.",
            required_change="A bounded correction.",
        )


def test_single_category_repairs_allow_complementary_item_local_mutations(tmp_path: Path) -> None:
    source_root = tmp_path / "source-only"
    source_root.mkdir()
    source_analyst, source_item = _workspace(source_root)
    source_analyst.submit_answer(AnalystAnswer(answer="Initial result."))
    source_analyst.review.record(
        "repair_once",
        reviewer_ref="independent-business-reviewer",
        findings=(
            ReviewFinding(
                finding_id="BR-SOURCE-ONLY",
                target_sections=("answer",),
                semantic_categories=("source_completeness",),
                problem="The source route is incomplete.",
                evidence="The source map omits a required input.",
                required_change="Complete the bounded source route.",
            ),
        ),
    )
    source_analyst.review.begin_repair()
    source_item._write_json_artifact("work/calculations/unrelated.json", {"status": "authorized"})  # noqa: SLF001
    assert json.loads(
        (source_item.work_root / "calculations" / "unrelated.json").read_text(encoding="utf-8")
    ) == {"status": "authorized"}

    calculation_root = tmp_path / "calculation-only"
    calculation_root.mkdir()
    calculation_analyst, _ = _workspace(calculation_root)
    calculation_analyst.submit_answer(AnalystAnswer(answer="Initial result."))
    calculation_analyst.review.record(
        "repair_once",
        reviewer_ref="independent-business-reviewer",
        findings=(
            ReviewFinding(
                finding_id="BR-CALC-ONLY",
                target_sections=("answer",),
                semantic_categories=("calculation",),
                problem="The calculation is stale.",
                evidence="The controlled output was not recomputed.",
                required_change="Recompute the bounded result.",
            ),
        ),
    )
    calculation_analyst.review.begin_repair()
    calculation_analyst.item_workspace.append_source_map({"source_id": "item-local"})
    assert json.loads(
        (calculation_analyst.item_workspace.work_root / "source_map.json").read_text(encoding="utf-8")
    ) == [{"source_id": "item-local"}]


def test_business_review_multi_section_scope_is_canonical_and_rechecks_exactly(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.begin_analysis(
        objective="Recompute the bounded stage result.",
        strategy="Use the controlled calculation and revise every named semantic section.",
    )
    initial = AnalystAnswer(
        answer="Initial stage result.",
        headline_findings=("Initial headline",),
        method="Initial method.",
        supported_components=("Initial support",),
        unsupported_components=("Initial unsupported",),
        limitations=("Initial limitation",),
        next_actions=("Initial next action",),
        visuals=({"title": "Initial visual"},),
    )
    analyst.submit_answer(initial)
    finding = ReviewFinding(
        finding_id="BR-MULTI-CALC",
        target_sections=("visuals", "method", "headline_findings", "supported_components"),
        semantic_categories=("calculation",),
        problem="The stage comparison needs a controlled recomputation.",
        evidence="The initial calculation omitted the within-system controls.",
        required_change="Recompute the controls and revise the named answer sections.",
    )
    assert finding.target_sections == ("headline_findings", "method", "supported_components", "visuals")
    review = analyst.review.record(
        "repair_once",
        reviewer_ref="independent-business-reviewer",
        findings=(finding,),
    )
    assert review["verdict"] == "repair_once"
    packet = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    assert packet["allowed_pointers"] == [
        "/answer",
        "/headline_findings",
        "/method",
        "/next_actions",
        "/scope",
        "/supported_components",
        "/visuals",
    ]
    assert packet["allowed_artifact_paths"] == ["work/results"]
    assert packet["findings"][0]["pointers"] == packet["allowed_pointers"]
    assert "work/calculations" in packet["allowed_dependencies"]
    assert "work/evidence.jsonl" in packet["allowed_dependencies"]
    assert "work/.analysis-run" in packet["allowed_dependencies"]
    assert "work/script_receipts" in packet["allowed_dependencies"]

    analyst.review.begin_repair()
    analyst.submit_answer(
        AnalystAnswer(
            answer="Changed answer.",
            headline_findings=("Changed headline",),
            method="Changed method.",
            supported_components=("Changed support",),
            limitations=("Changed unrelated limitation",),
            visuals=({"title": "Changed visual"},),
        )
    )
    item.write_open_issues({"unrelated": "artifact"})
    assert json.loads((item.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": "artifact"
    }

    analyst.submit_answer(
        AnalystAnswer(
            answer="Corrected answer.",
            headline_findings=("Corrected headline",),
            method="Corrected method.",
            supported_components=("Corrected support",),
            unsupported_components=("Initial unsupported",),
            limitations=("Initial limitation",),
            next_actions=("Initial next action",),
            visuals=({"title": "Corrected visual"},),
        )
    )
    targeted = analyst.review.record("accept", reviewer_ref="targeted-business-reviewer")
    assert targeted["targeted_recheck"] is True
    assert targeted["changed_pointers"] == [
        "/answer",
        "/headline_findings/0",
        "/method",
        "/supported_components/0",
        "/visuals/0/title",
    ]
    with pytest.raises(ValueError, match="repair_once review verdict"):
        analyst.review.begin_repair()

    method_root = tmp_path / "method-case"
    method_root.mkdir()
    method_analyst, method_item = _workspace(method_root)
    method_analyst.submit_answer(
        AnalystAnswer(
            answer="Initial method result.",
            method="Initial method.",
            limitations=("Initial limitation",),
            unsupported_components=("Initial unsupported",),
        )
    )
    method_finding = ReviewFinding(
        finding_id="BR-MULTI-METHOD",
        target_sections=("unsupported_components", "method", "limitations"),
        semantic_categories=("method",),
        problem="The handoff validation method is too weak.",
        evidence="The sequence check is not ordered by event sequence.",
        required_change="Correct the method and disclose the resulting limits.",
    )
    method_analyst.review.record(
        "repair_once",
        reviewer_ref="independent-business-reviewer",
        findings=(method_finding,),
    )
    method_packet = json.loads((method_item.work_root / "business_review.json").read_text(encoding="utf-8"))
    assert method_packet["findings"][0]["pointers"] == [
        "/answer",
        "/limitations",
        "/method",
        "/unsupported_components",
    ]
    assert "work/source_map.json" in method_packet["allowed_dependencies"]
    assert "work/.analysis-run" in method_packet["allowed_dependencies"]
    assert "work/script_receipts" in method_packet["allowed_dependencies"]


@pytest.mark.parametrize("target_sections", ((), ("method", "method"), ("not_a_section",), ("/answer",)))
def test_review_finding_rejects_empty_duplicate_unknown_and_raw_pointer_sections(
    target_sections: tuple[str, ...],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ReviewFinding(
            finding_id="BR-INVALID-SECTIONS",
            target_sections=target_sections,
            semantic_categories=("answer",),
            problem="A bounded problem.",
            evidence="A bounded fact.",
            required_change="A bounded correction.",
        )


@pytest.mark.parametrize(
    ("section_name", "category"),
    (("method", "method"), ("headline_findings", "calculation")),
)
def test_business_repair_allows_controlled_receipts_evidence_and_answer_recheck(
    tmp_path: Path,
    section_name: str,
    category: str,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.begin_analysis(objective="Recompute the bounded order result.", strategy="Run the bounded calculation and disclose scope.")
    calculation = item.work_root / "calculations" / "recompute.py"
    calculation.parent.mkdir(parents=True)
    calculation.write_text(
        "from pathlib import Path\nPath('analysis.json').write_text('{\\\"status\\\": \\\"recomputed\\\"}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="The initial bounded result is provisional.",
            headline_findings=("Initial finding",),
            method="Initial method.",
        )
    )

    initial = analyst.review.record(
        "repair_once",
        reviewer_ref="independent-business-reviewer",
        findings=(
            ReviewFinding(
                finding_id=f"BR-{category}",
                target_sections=(section_name,),
                semantic_categories=(category,),
                problem="The bounded result needs a scoped correction.",
                evidence="The first calculation is provisional.",
                required_change="Recompute the result and revise its narrative.",
            ),
        ),
    )
    assert initial["verdict"] == "repair_once"
    packet = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    assert "/answer" in packet["allowed_pointers"]
    assert f"/{section_name}" in packet["allowed_pointers"]
    assert "work/.analysis-run" in packet["allowed_dependencies"]
    assert "work/script_receipts" in packet["allowed_dependencies"]

    analyst.review.begin_repair()
    item.write_open_issues({"unrelated": True})
    assert json.loads((item.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": True
    }
    analyst.submit_answer(
        AnalystAnswer(
            answer="The revised bounded result.",
            headline_findings=("Revised finding",),
            method="Revised method.",
            limitations=("This unrelated structured section is retained as item-local work.",),
        )
    )

    report = analyst.run_analysis(
        calculation,
        outputs=(item.work_root / "analysis.json",),
    )
    assert report.succeeded
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-REPAIR",
            conclusion="The bounded result was recomputed.",
            method="Run the controlled calculation once through the bound context.",
            evidence_refs=("work/analysis.json",),
        )
    )
    revised = AnalystAnswer(
        answer="The revised bounded result is supported by the recomputation.",
        headline_findings=("Revised finding",) if section_name == "headline_findings" else ("Initial finding",),
        method="Revised method." if section_name == "method" else "Initial method.",
    )
    analyst.submit_answer(revised)
    targeted = analyst.review.record("accept", reviewer_ref="targeted-business-reviewer")
    assert targeted["targeted_recheck"] is True
    assert "/answer" in targeted["changed_pointers"]
    with pytest.raises(ValueError, match="repair_once review verdict"):
        analyst.review.begin_repair()


def test_business_repair_preserves_named_pointer_and_allows_item_local_section_and_artifact(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial narrative.",
            headline_findings=("Initial finding",),
            limitations=("Initial limitation",),
        )
    )
    analyst.review.record(
        "repair_once",
        reviewer_ref="independent-business-reviewer",
        findings=(
            ReviewFinding(
                finding_id="BR-NARRATIVE",
                target_sections=("headline_findings",),
                semantic_categories=("calculation",),
                problem="The headline finding needs correction.",
                evidence="The bounded calculation changed.",
                required_change="Revise the headline finding and coherent narrative.",
            ),
        ),
    )
    packet = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    assert packet["allowed_pointers"] == ["/answer", "/headline_findings", "/next_actions", "/scope"]
    assert packet["allowed_artifact_paths"] == ["work/results"]
    analyst.review.begin_repair()
    analyst.submit_answer(
        AnalystAnswer(
            answer="Changed narrative.",
            headline_findings=("Changed finding",),
            limitations=("Unrelated changed limitation",),
        )
    )
    item.write_open_issues({"unrelated": True})
    assert json.loads((item.work_root / "open_issues.json").read_text(encoding="utf-8")) == {
        "unrelated": True
    }
    analyst.submit_answer(
        AnalystAnswer(
            answer="Changed narrative.",
            headline_findings=("Changed finding",),
            limitations=("Initial limitation",),
        )
    )
    targeted = analyst.review.record("accept", reviewer_ref="targeted-business-reviewer")
    assert targeted["changed_pointers"] == ["/answer", "/headline_findings/0"]


def test_active_repair_scope_reconciliation_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.begin_analysis(
        objective="Recompute the bounded order result.",
        strategy="Run the controlled calculation and revise the narrative.",
    )
    calculation = item.work_root / "calculations" / "recompute.py"
    calculation.parent.mkdir(parents=True)
    calculation.write_text(
        "from pathlib import Path\nPath('analysis.json').write_text('{\"status\":\"recomputed\"}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="The initial bounded result is provisional.",
            method="Initial method.",
            headline_findings=("Initial finding",),
        )
    )
    semantic = ReviewFinding(
        finding_id="Q001-BR-01",
        target_sections=("method",),
        semantic_categories=("method",),
        problem="The method needs a controlled recomputation.",
        evidence="The original run has no durable execution receipt.",
        required_change="Run the bounded calculation and revise the coherent narrative.",
    )
    analyst.review.record("repair_once", reviewer_ref="current-reviewer", findings=(semantic,))
    analyst.review.begin_repair()
    packet_before = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    state_before = item.state
    baseline_fields = (
        "before_snapshot",
        "before_hash",
        "before_pointer_hashes",
        "after_pointer_hashes",
        "before_artifact_hashes",
        "after_artifact_hashes",
        "reviewed_draft_hash",
        "changed_pointers",
        "unchanged_paths",
        "unchanged_aggregate_hash",
    )
    baseline = {field: packet_before[field] for field in baseline_fields}

    report = analyst.run_analysis(calculation, outputs=(item.work_root / "analysis.json",))
    assert report.succeeded
    assert (item.work_root / "script_receipts").is_dir()
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-REPAIR-UPGRADE",
            conclusion="The bounded result was recomputed.",
            method="Run the controlled calculation once.",
            evidence_refs=("work/analysis.json",),
        )
    )
    # Simulate the already-written coherent answer; the current adapter scope
    # must validate it before publishing.
    item.draft_root.write_text(
        json.dumps(
            {
                "schema_version": "auto_foundry.analyst_answer.v1",
                "item_id": item.item_id,
                "answer": "The recomputed bounded result is supported.",
                "headline_findings": ["Initial finding"],
                "scope": None,
                "method": "Recomputed with the controlled script.",
                "supported_components": [],
                "unsupported_components": [],
                "limitations": [],
                "next_actions": [],
                "visuals": [],
                "evidence_refs": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert item._repair_scope_check() is not None  # noqa: SLF001 - current scope remains closed

    # All semantic mismatches fail without changing either packet or state.
    packet_path = item.work_root / "business_review.json"
    state_path = item.item_root / "item_state.json"
    packet_bytes_before_rejections = packet_path.read_bytes()
    state_bytes_before_rejections = state_path.read_bytes()
    for invalid in (
        ReviewFinding(
            finding_id="Q001-BR-OTHER",
            target_sections=semantic.target_sections,
            semantic_categories=semantic.semantic_categories,
            problem=semantic.problem,
            evidence=semantic.evidence,
            required_change=semantic.required_change,
        ),
        ReviewFinding(
            finding_id=semantic.finding_id,
            target_sections=semantic.target_sections,
            semantic_categories=semantic.semantic_categories,
            problem="Changed problem.",
            evidence=semantic.evidence,
            required_change=semantic.required_change,
        ),
        ReviewFinding(
            finding_id=semantic.finding_id,
            target_sections=semantic.target_sections,
            semantic_categories=("calculation",),
            problem=semantic.problem,
            evidence=semantic.evidence,
            required_change=semantic.required_change,
        ),
        ReviewFinding(
            finding_id=semantic.finding_id,
            target_sections=("limitations",),
            semantic_categories=semantic.semantic_categories,
            problem=semantic.problem,
            evidence=semantic.evidence,
            required_change=semantic.required_change,
        ),
    ):
        with pytest.raises(ValueError):
            analyst.review.reconcile_active_repair_scope((invalid,))
        assert packet_path.read_bytes() == packet_bytes_before_rejections
        assert state_path.read_bytes() == state_bytes_before_rejections

    upgraded = analyst.review.reconcile_active_repair_scope((semantic,))
    assert upgraded["review_scope"] == "full"
    assert upgraded["repair_active"] is True
    assert upgraded["targeted_recheck"] is False
    assert "/method" in upgraded["allowed_pointers"]
    assert "/answer" in upgraded["allowed_pointers"]
    assert "work/.analysis-run" in upgraded["allowed_dependencies"]
    assert "work/script_receipts" in upgraded["allowed_dependencies"]
    packet_after = json.loads(packet_path.read_text(encoding="utf-8"))
    for field, value in baseline.items():
        assert packet_after[field] == value
    assert packet_after["findings"][0]["finding_id"] == semantic.finding_id
    assert packet_after["findings"][0]["message"] == analyst.review._core_finding(semantic)["message"]  # noqa: SLF001
    assert packet_after["findings"][0]["material"] is True
    assert item.state["business_repair_count"] == 1
    assert item.state["lifecycle_state"] == "work"

    analyst.submit_answer(
        AnalystAnswer(
            answer="The recomputed bounded result is supported.",
            headline_findings=("Initial finding",),
            method="Recomputed with the controlled script.",
        )
    )

    packet_bytes_before_atomic_failure = packet_path.read_bytes()
    state_bytes_before_atomic_failure = state_path.read_bytes()

    def fail_state_persist(_state: dict[str, object]) -> None:
        raise OSError("injected state persistence failure")

    original_persist_state = item._persist_state_unlocked
    monkeypatch.setattr(item, "_persist_state_unlocked", fail_state_persist)
    with pytest.raises(OSError, match="injected"):
        analyst.review.reconcile_active_repair_scope((semantic,))
    assert packet_path.read_bytes() == packet_bytes_before_atomic_failure
    assert state_path.read_bytes() == state_bytes_before_atomic_failure
    monkeypatch.setattr(item, "_persist_state_unlocked", original_persist_state)

    targeted = analyst.review.record("accept", reviewer_ref="targeted-business-reviewer")
    assert targeted["targeted_recheck"] is True
    assert item.state["business_repair_count"] == 1


def obsolete_two_repairs_reset_the_review_baseline_and_reject_a_third_before_mutation(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial narrative."))

    def finding(finding_id: str) -> ReviewFinding:
        return ReviewFinding(
            finding_id=finding_id,
            target_sections=("answer",),
            semantic_categories=("answer",),
            problem="The answer needs a material correction.",
            evidence="The bounded fixture is insufficient for the current wording.",
            required_change="Revise the parent answer.",
        )

    analyst.review.record("repair_once", reviewer_ref="reviewer-1", findings=(finding("F-1"),))
    analyst.review.begin_repair()
    analyst.submit_answer(AnalystAnswer(answer="First corrected narrative."))
    analyst.review.record("repair_once", reviewer_ref="reviewer-2", findings=(finding("F-2"),))
    analyst.review.begin_repair()
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert packet["before_snapshot"]["answer"] == "First corrected narrative."
    analyst.submit_answer(AnalystAnswer(answer="Second corrected narrative."))
    analyst.review.record("accept", reviewer_ref="reviewer-3")
    state_before = (item.item_root / "item_state.json").read_bytes()
    with pytest.raises(ValueError, match="only two business repairs"):
        analyst.review.begin_repair()
    assert (item.item_root / "item_state.json").read_bytes() == state_before


def obsolete_targeted_business_repair_replaces_scope_with_exact_narrow_union_end_to_end(
    tmp_path: Path,
) -> None:
    """A second same-owner repair closes the old scope before binding the new one."""

    analyst, item = _workspace(tmp_path)
    telemetry = TelemetryRecorder(context=item.context)
    # ``ItemWorkspace`` and its bound facade share the same passive ledger;
    # this makes the first broad review result durable even after the current
    # packet is replaced by the targeted packet.
    item.telemetry = telemetry
    analyst.context.telemetry = telemetry
    analyst.begin_analysis(
        objective="Recompute the bounded stage result.",
        strategy="Run the controlled calculation, bind evidence, and revise the complete answer.",
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded answer.",
            headline_findings=("Initial headline.",),
            scope="Initial scope.",
            method="Initial method.",
            supported_components=("Initial support.",),
            unsupported_components=("Initial unsupported component.",),
            limitations=("Initial limitation.",),
            next_actions=("Initial next action.",),
            visuals=({"title": "Initial visual"},),
            evidence_refs=("work/plan.json",),
        )
    )

    def finding(finding_id: str, section: str, category: str) -> ReviewFinding:
        return ReviewFinding(
            finding_id=finding_id,
            target_sections=(section,),
            semantic_categories=(category,),
            problem=f"The {section} needs a bounded correction.",
            evidence=f"The current {section} does not match the controlled fixture.",
            required_change=f"Revise the {section} while preserving the bounded scope.",
        )

    # Seven independent fields/categories are present in the initial packet.
    broad_findings = (
        finding("BR-FULL-ANSWER", "answer", "answer"),
        finding("BR-FULL-HEADLINE", "headline_findings", "calculation"),
        finding("BR-FULL-EVIDENCE", "evidence_refs", "evidence"),
        finding("BR-FULL-METHOD", "method", "method"),
        finding("BR-FULL-SCOPE", "unsupported_components", "source_completeness"),
        finding("BR-FULL-VISUAL", "visuals", "presentation"),
        finding("BR-FULL-LIMIT", "limitations", "answer"),
    )
    initial_result = analyst.review.record(
        "repair_once",
        reviewer_ref="full-business-reviewer",
        findings=broad_findings,
    )
    assert initial_result["review_scope"] == "full"
    assert initial_result["finding_count"] == 7
    initial_packet_bytes = item.business_review_path.read_bytes()
    initial_packet_hash = hashlib.sha256(initial_packet_bytes).hexdigest()
    initial_packet = json.loads(initial_packet_bytes)
    assert len(initial_packet["findings"]) == 7
    analyst.review.begin_repair()

    # The owner changes several authorized work artifacts and all seven
    # initially reviewed answer fields before the targeted reviewer responds.
    calculation = item.work_root / "calculations" / "recompute.py"
    calculation.parent.mkdir(parents=True)
    calculation.write_text(
        "from pathlib import Path\n"
        "Path('analysis.json').write_text('{\"status\":\"broad-repaired\"}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    assert analyst.run_analysis(calculation, outputs=(item.work_root / "analysis.json",)).succeeded
    item.append_source_map({"source_id": "repaired-source", "support": "bounded"})
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-TARGETED-SCOPE",
            conclusion="The broad repair was recomputed from the supplied fixture.",
            method="Run the controlled calculation and bind its output.",
            evidence_refs=("work/analysis.json",),
        )
    )
    first_corrected = AnalystAnswer(
        answer="First corrected bounded answer.",
        headline_findings=("Corrected headline.",),
        scope="Corrected scope.",
        method="Corrected method.",
        supported_components=("Initial support.",),
        unsupported_components=("Initial unsupported component.",),
        limitations=("Corrected limitation.",),
        next_actions=("Corrected next action.",),
        visuals=({"title": "Corrected visual"},),
        evidence_refs=("work/analysis.json",),
    )
    analyst.submit_answer(first_corrected)

    narrow_findings = (
        finding("BR-TARGET-ANSWER", "answer", "answer"),
        finding("BR-TARGET-VISUAL", "visuals", "presentation"),
    )
    targeted_result = analyst.review.record(
        "repair_once",
        reviewer_ref="targeted-business-reviewer",
        findings=narrow_findings,
    )
    assert targeted_result["review_scope"] == "targeted"
    assert targeted_result["targeted_recheck"] is True
    assert targeted_result["finding_count"] == 2
    targeted_packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert [row["finding_id"] for row in targeted_packet["findings"]] == [
        "BR-TARGET-ANSWER",
        "BR-TARGET-VISUAL",
    ]
    assert targeted_packet["allowed_pointers"] == ["/answer", "/next_actions", "/scope", "/visuals"]
    assert targeted_packet["allowed_artifact_paths"] == ["work/results"]
    assert targeted_packet["allowed_dependencies"] == ["work/business_review_packet.json"]
    # The prior broad result remains in the append-only telemetry ledger even
    # though the current item-local packet now contains only the two new rows.
    review_events = [event for event in telemetry.events if event.event_type == "item_review_recorded"]
    assert [(event.facts["verdict"], event.facts["finding_count"]) for event in review_events] == [
        ("repair_once", 7),
        ("repair_once", 2),
    ]
    assert initial_packet_hash == hashlib.sha256(initial_packet_bytes).hexdigest()
    event_lines = [json.loads(line) for line in telemetry.event_path.read_text(encoding="utf-8").splitlines()]
    persisted_review_events = [row for row in event_lines if row["event_type"] == "item_review_recorded"]
    assert [row["facts"]["finding_count"] for row in persisted_review_events] == [7, 2]

    second_packet = analyst.review.begin_repair()
    assert second_packet["review_scope"] == "full"
    assert second_packet["repair_active"] is True
    assert second_packet["allowed_pointers"] == ["/answer", "/next_actions", "/scope", "/visuals"]
    assert second_packet["allowed_artifact_paths"] == ["work/results"]
    assert second_packet["allowed_dependencies"] == ["work/business_review_packet.json"]
    assert item.state["business_repair_count"] == 2

    # The second same-owner repair may revise an answer section that was not
    # named by its narrower reviewer findings; the item-local boundary, not
    # category-derived pointers, remains the authority.
    analyst.submit_answer(
        AnalystAnswer(
            answer=first_corrected.answer,
            headline_findings=first_corrected.headline_findings,
            scope=first_corrected.scope,
            method="Unauthorized old method pointer.",
            supported_components=first_corrected.supported_components,
            unsupported_components=first_corrected.unsupported_components,
            limitations=first_corrected.limitations,
            next_actions=first_corrected.next_actions,
            visuals=first_corrected.visuals,
            evidence_refs=first_corrected.evidence_refs,
        )
    )
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["method"] == "Unauthorized old method pointer."

    second_corrected = AnalystAnswer(
        answer="Second corrected bounded answer.",
        headline_findings=first_corrected.headline_findings,
        scope="Second corrected scope.",
        method=first_corrected.method,
        supported_components=first_corrected.supported_components,
        unsupported_components=first_corrected.unsupported_components,
        limitations=first_corrected.limitations,
        next_actions=("Second corrected next action.",),
        visuals=({"title": "Second corrected visual"},),
        evidence_refs=first_corrected.evidence_refs,
    )
    analyst.submit_answer(second_corrected)
    terminal_targeted = analyst.review.record("accept", reviewer_ref="targeted-terminal-reviewer")
    assert terminal_targeted["review_scope"] == "targeted"
    assert terminal_targeted["targeted_recheck"] is True
    assert terminal_targeted["changed_pointers"] == [
        "/answer",
        "/next_actions/0",
        "/scope",
        "/visuals/0/title",
    ]
    terminal_packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert terminal_packet["findings"] == targeted_packet["findings"]
    assert terminal_packet["allowed_pointers"] == ["/answer", "/next_actions", "/scope", "/visuals"]
    assert terminal_packet["repair_active"] is False

    state_before_third = (item.item_root / "item_state.json").read_bytes()
    packet_before_third = item.business_review_path.read_bytes()
    with pytest.raises(ValueError, match="only two business repairs"):
        analyst.review.begin_repair()
    assert (item.item_root / "item_state.json").read_bytes() == state_before_third
    assert item.business_review_path.read_bytes() == packet_before_third

    accepted = item.accept(accepted_refs=("work/analysis.json", "work/evidence.jsonl"))
    assert accepted.outcome == "accepted"
    assert item.state["business_repair_count"] == 2


def obsolete_second_targeted_visual_repair_can_bind_new_evidence_ref_and_other_answer_section(
    tmp_path: Path,
) -> None:
    """A recomputed visual may cite its new evidence without opening other answer fields."""

    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded answer.",
            method="Initial method.",
            visuals=({"title": "Initial promotion series"},),
            evidence_refs=("work/plan.json",),
        )
    )
    first_finding = ReviewFinding(
        finding_id="BR02-R0",
        target_sections=("visuals",),
        semantic_categories=("presentation",),
        problem="The initial visual needs a bounded correction.",
        evidence="The first promotion series is stale.",
        required_change="Replace the stale visual.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer-r0", findings=(first_finding,))
    analyst.review.begin_repair()
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded answer.",
            method="Initial method.",
            visuals=({"title": "First corrected promotion series"},),
            evidence_refs=("work/plan.json",),
        )
    )
    analyst.review.record("accept", reviewer_ref="reviewer-r0-targeted")

    second_finding = ReviewFinding(
        finding_id="BR02-R1",
        target_sections=("visuals",),
        semantic_categories=("answer", "calculation", "presentation"),
        problem="The repaired promotion series needs the recomputed result.",
        evidence="The controlled result and public evidence were refreshed.",
        required_change="Publish the refreshed visual and bind its evidence record.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer-r1", findings=(second_finding,))
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert packet["findings"][0]["pointers"] == [
        "/answer",
        "/evidence_refs",
        "/next_actions",
        "/scope",
        "/visuals",
    ]
    assert packet["allowed_dependencies"] == [
        "work/.analysis-run",
        "work/analysis.json",
        "work/analysis.py",
        "work/business_review_packet.json",
        "work/calculations",
        "work/evidence.jsonl",
        "work/prepared",
        "work/script_receipts",
    ]

    analyst.review.begin_repair()
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-REQ02-VISUALS-R2",
            conclusion="The refreshed promotion series uses the recomputed result.",
            method="Run the controlled visual calculation.",
            evidence_refs=("work/results/promotion.json",),
        )
    )
    corrected = AnalystAnswer(
        answer="Initial bounded answer.",
        method="Initial method.",
        visuals=({"title": "Second corrected promotion series"},),
        evidence_refs=("work/evidence.jsonl#E-REQ02-VISUALS-R2",),
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer=corrected.answer,
            method="Unrelated method mutation.",
            visuals=corrected.visuals,
            evidence_refs=corrected.evidence_refs,
        )
    )
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["method"] == "Unrelated method mutation."

    analyst.submit_answer(corrected)
    targeted = analyst.review.record("accept", reviewer_ref="reviewer-r1-targeted")
    assert targeted["changed_pointers"] == [
        "/evidence_refs/0",
        "/visuals/0/title",
    ]
    with pytest.raises(ValueError, match="only two business repairs"):
        analyst.review.begin_repair()


@pytest.mark.parametrize("tamper", ("rehashed_packet", "unknown_category", "drift"))
def test_targeted_business_repair_negative_paths_are_byte_preserving(
    tmp_path: Path,
    tamper: str,
) -> None:
    """Rehashed/unknown/drifted targeted state cannot authorize a repair."""

    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial answer.", method="Initial method."))
    initial = ReviewFinding(
        finding_id="BR-NEG-INITIAL",
        target_sections=("method",),
        semantic_categories=("evidence",),
        problem="The evidence route is incomplete.",
        evidence="The source map is missing.",
        required_change="Repair the bounded evidence route.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer-1", findings=(initial,))
    analyst.review.begin_repair()
    analyst.submit_answer(AnalystAnswer(answer="First corrected answer.", method="First corrected method."))
    narrow = ReviewFinding(
        finding_id="BR-NEG-TARGET",
        target_sections=("answer",),
        semantic_categories=("evidence",),
        problem="The answer needs one final correction.",
        evidence="The targeted reviewer found a bounded wording issue.",
        required_change="Revise only the parent answer.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer-2", findings=(narrow,))

    packet_path = item.business_review_path
    state_path = item.item_root / "item_state.json"
    packet_before = packet_path.read_bytes()
    state_before = state_path.read_bytes()
    packet = json.loads(packet_before)
    if tamper == "rehashed_packet":
        # Coordinate a canonical-looking aggregate rewrite while removing the
        # category-required evidence pointer; the semantic validator must
        # reject this despite the aggregate being recomputed.
        packet["findings"][0]["pointers"] = ["/answer"]
        packet["allowed_pointers"] = ["/answer"]
    elif tamper == "unknown_category":
        packet["findings"][0]["semantic_categories"] = ["unknown"]
    else:
        # Drift the currently reviewed draft behind the packet's reviewed hash.
        item.draft_root.write_text(
            json.dumps({"answer": "drifted outside the reviewed draft", "method": "First corrected method."}),
            encoding="utf-8",
        )
    if tamper != "drift":
        packet_path.write_text(
            json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    packet_after_tamper = packet_path.read_bytes()
    state_after_tamper = state_path.read_bytes()
    with pytest.raises(ValueError, match="business review|semantic category|exact currently reviewed draft"):
        analyst.review.begin_repair()
    assert packet_path.read_bytes() == packet_after_tamper
    assert state_path.read_bytes() == state_after_tamper
    # The deliberate input tamper is not itself rolled back; the failed
    # transition remains byte-preserving relative to the bytes it observed.
    assert packet_after_tamper != packet_before or tamper == "drift"
    assert state_after_tamper == state_before


def test_targeted_business_repair_failed_record_preserves_packet_and_state(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial answer."))
    broad = ReviewFinding(
        finding_id="BR-FAILED-INITIAL",
        target_sections=("answer",),
        semantic_categories=("answer",),
        problem="The answer needs a bounded correction.",
        evidence="The first answer is provisional.",
        required_change="Revise the parent answer.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer-1", findings=(broad,))
    analyst.review.begin_repair()
    analyst.submit_answer(AnalystAnswer(answer="First corrected answer."))
    packet_before = item.business_review_path.read_bytes()
    state_before = (item.item_root / "item_state.json").read_bytes()
    draft_before = item.draft_root.read_bytes()
    # Bypass the normal writer to model a drifted draft reaching the reviewer.
    item.draft_root.write_text(
        json.dumps({"answer": "unauthorized drift", "old_field": "must remain closed"}),
        encoding="utf-8",
    )
    second = ReviewFinding(
        finding_id="BR-FAILED-TARGET",
        target_sections=("answer",),
        semantic_categories=("answer",),
        problem="The targeted answer still needs a correction.",
        evidence="The targeted review is bounded to the answer.",
        required_change="Revise only the answer.",
    )
    with pytest.raises(ValueError, match="outside reviewed scope"):
        analyst.review.record("repair_once", reviewer_ref="reviewer-2", findings=(second,))
    assert item.business_review_path.read_bytes() == packet_before
    assert (item.item_root / "item_state.json").read_bytes() == state_before
    assert item.draft_root.read_bytes() != draft_before


def test_requirement_plan_is_parent_scoped_exactly_idempotent_and_required_before_analysis(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("milk.csv", "milk_fat,price\n3.5,10\n")
    original = "dashboard should show the ratio of milk fat content to procurement price of the raw material for that milk"
    context = RunContext("RUN-REQUIREMENT-PLAN", tmp_path / "run", (input_root,))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, "R-001", mode="requirement", original_text=original)
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item, workbench=workbench)
    analyst = AnalystWorkspace(bound, owner_ref="owner-R-001")
    with pytest.raises(ValueError, match="plan"):
        analyst.begin_analysis(objective="ratio", strategy="bounded")

    plan = RequirementAnalysisPlan(
        tasks=(
            RequirementAnalysisTask(
                task_id="T-1",
                question="Measure milk fat content for the supplied milk population.",
                expected_analytical_outputs=("milk-fat numerator",),
            ),
            RequirementAnalysisTask(
                task_id="T-2",
                question="Measure procurement price for the same population.",
                dependencies=("T-1",),
                expected_visual_outputs=("ratio chart",),
            ),
        ),
        synthesis_intent="Synthesize one parent answer with a clearly defined ratio.",
    )
    first = analyst.plan_requirement(plan)
    second = analyst.plan_requirement(plan)
    assert first == second
    assert analyst.brief().requirement_plan == first
    with pytest.raises(ValueError, match="semantic scope decision"):
        analyst.begin_analysis(objective="ratio", strategy="bounded")
    with pytest.raises(ValueError, match="calculation requires an explicit semantic scope decision"):
        analyst.run_analysis(item.work_root / "calculations" / "not-run.py")
    analyst.select_semantic_scope(
        no_reuse_reason="The first requirement snapshot contains no accepted reusable semantics.",
        purpose="Record the required first-run semantic decision.",
    )
    analyst.begin_analysis(objective="ratio", strategy="bounded")
    analyst.submit_answer("One parent requirement answer.")
    assert item.draft_root.is_file()
    assert item.state["review"]["status"] == "pending"


def test_requirement_plan_binds_during_readiness_attempt_before_material_analysis(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("milk.csv", "milk_fat,price\n3.5,10\n")
    original = "dashboard should show the ratio of milk fat content to procurement price"
    context = RunContext("RUN-REQUIREMENT-PLAN-ACTIVE-ATTEMPT", tmp_path / "run", (input_root,))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, "R-002", mode="requirement", original_text=original)
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item, workbench=workbench)
    analyst = AnalystWorkspace(bound, owner_ref="owner-R-002")

    attempt = item.begin_attempt("ao-R-002", "Analytical Owner", route="requirement")
    plan = RequirementAnalysisPlan(
        tasks=(
            RequirementAnalysisTask(
                task_id="T-1",
                question="Measure the supplied milk-fat population.",
                expected_analytical_outputs=("milk-fat numerator",),
            ),
        ),
        synthesis_intent="Synthesize one bounded ratio answer.",
    )
    first = analyst.plan_requirement(plan)
    assert first == analyst.plan_requirement(plan)
    assert item.work_root.joinpath("requirement_plan.json").is_file()

    changed = RequirementAnalysisPlan(
        tasks=plan.tasks,
        synthesis_intent="Revise the ratio intent while research is active.",
    )
    revised = analyst.plan_requirement(changed)
    assert revised.synthesis_intent == changed.synthesis_intent
    assert analyst.brief().requirement_plan == revised

    with pytest.raises(ValueError, match="semantic scope decision"):
        analyst.begin_analysis(objective="ratio", strategy="bounded")
    analyst.select_semantic_scope(
        no_reuse_reason="The readiness snapshot contains no accepted reusable semantics.",
        purpose="Record the first-run semantic decision.",
    )
    analyst.begin_analysis(objective="ratio", strategy="bounded")
    item.finish_attempt(attempt.attempt_id, status="completed")


def test_requirement_material_paths_require_plan_but_question_mode_prepare_is_unchanged(tmp_path: Path) -> None:
    input_root = tmp_path / "requirement-inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,amount\nO-1,10\n")
    context = RunContext("RUN-REQUIREMENT-MATERIAL-GATES", tmp_path / "requirement-run", (input_root,))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, "R-003", mode="requirement", original_text="Analyze supplied orders.")
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item, workbench=workbench)
    analyst = AnalystWorkspace(bound, owner_ref="owner-R-003")
    analyst.select_semantic_scope(
        no_reuse_reason="The requirement snapshot contains no accepted reusable semantics.",
        purpose="Record the semantic decision before material execution.",
    )

    with pytest.raises(ValueError, match="persisted semantic plan"):
        analyst.run_analysis(tmp_path / "not-run.py")
    with pytest.raises(ValueError, match="persisted semantic plan"):
        analyst.prepare_data(
            "requirement-candidate",
            next(entry for entry in analyst.context.source_catalog.entries if entry.path == "orders.csv"),
        )

    question_root = tmp_path / "question"
    question_root.mkdir()
    question_analyst, _question_item = _workspace(question_root)
    question_entry = next(
        entry for entry in question_analyst.context.source_catalog.entries if entry.path == "orders.csv"
    )
    candidate = question_analyst.prepare_data(
        "question-candidate",
        question_entry,
        scope="reusable",
        transformations=("bounded_fixture_copy",),
    )
    assert candidate.prepared_asset_id == "question-candidate"


def test_active_repair_reconciliation_rejects_category_changes(
    tmp_path: Path,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial bounded result.", method="Initial method."))
    semantic = ReviewFinding(
        finding_id="Q001-BR-LEGACY-UNION",
        target_sections=("method",),
        semantic_categories=("source_completeness", "calculation"),
        problem="The source route and deterministic calculation need correction.",
        evidence="The pre-fix packet authorized only source-completeness roots.",
        required_change="Complete the source route, recompute, and revise the method narrative.",
    )
    analyst.review.record("repair_once", reviewer_ref="current-reviewer", findings=(semantic,))
    analyst.review.begin_repair()
    packet_path = item.work_root / "business_review.json"
    packet_before = json.loads(packet_path.read_text(encoding="utf-8"))
    baseline = {
        field: packet_before[field]
        for field in (
            "before_snapshot",
            "before_hash",
            "before_pointer_hashes",
            "before_artifact_hashes",
            "after_pointer_hashes",
            "after_artifact_hashes",
            "reviewed_draft_hash",
            "changed_pointers",
            "unchanged_paths",
            "unchanged_aggregate_hash",
        )
    }
    packet_bytes_before = packet_path.read_bytes()
    state_bytes_before = (item.item_root / "item_state.json").read_bytes()
    upgraded = analyst.review.reconcile_active_repair_scope((semantic,))
    assert upgraded["allowed_pointers"] == packet_before["allowed_pointers"]
    assert packet_path.read_bytes() == packet_bytes_before
    state_after = json.loads((item.item_root / "item_state.json").read_text(encoding="utf-8"))
    state_before_json = json.loads(state_bytes_before.decode("utf-8"))
    state_after.pop("updated_at", None)
    state_before_json.pop("updated_at", None)
    assert state_after == state_before_json
    assert item.state["business_repair_count"] == 1

    exact = ReviewFinding(
        finding_id=semantic.finding_id,
        target_sections=semantic.target_sections,
        semantic_categories=("evidence", "source_completeness"),
        problem=semantic.problem,
        evidence=semantic.evidence,
        required_change=semantic.required_change,
    )
    with pytest.raises(ValueError, match="semantic category set changed"):
        analyst.review.reconcile_active_repair_scope((exact,))
    assert packet_path.read_bytes() == packet_bytes_before
    rejected_state = json.loads((item.item_root / "item_state.json").read_text(encoding="utf-8"))
    rejected_state.pop("updated_at", None)
    assert rejected_state == state_before_json
    assert item.state["business_repair_count"] == 1


def obsolete_wrong_owner_reconciliation_rejects_before_discard_recovery_and_preserves_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial bounded result.", method="Initial method."))
    finding = ReviewFinding(
        finding_id="BR-OWNER-DISCARD-ORDER",
        target_sections=("method",),
        semantic_categories=("evidence", "source_completeness"),
        problem="The source route needs one more evidence binding.",
        evidence="The legacy packet predates evidence reference binding.",
        required_change="Record and cite the missing evidence.",
    )
    analyst.review.record("repair_once", reviewer_ref="reviewer-evidence", findings=(finding,))
    analyst.review.begin_repair()
    wrong_item = ItemWorkspace.load(item.context, item.item_id)
    wrong_owner = BusinessReviewAdapter(wrong_item, owner_ref="owner-Q-002")
    paths = (
        item.item_root / "item_state.json",
        item.business_review_path,
        item.draft_root,
        item.analysis_owner_path,
        item.business_review_discard_state_path,
        item.business_review_discard_audit_path,
    )
    before = {
        path: (path.exists(), path.read_bytes() if path.exists() else None)
        for path in paths
    }

    def mutate_if_called() -> None:
        item.business_review_discard_audit_path.write_text("unexpected\n", encoding="utf-8")
        raise AssertionError("discard reconciliation must not run before owner verification")

    monkeypatch.setattr(wrong_item, "_reconcile_business_review_discard", mutate_if_called)
    with pytest.raises(ValueError, match="owner_ref"):
        wrong_owner.reconcile_active_repair_scope((finding,))
    after = {
        path: (path.exists(), path.read_bytes() if path.exists() else None)
        for path in paths
    }
    assert after == before
    assert item.state["business_repair_count"] == 1


@pytest.mark.parametrize("winner", ("attempt", "reconciliation"))
def test_active_repair_scope_reconciliation_linearizes_with_stale_workspace_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winner: str,
) -> None:
    finding, item = _active_repair(tmp_path)
    item_for_reconciliation = ItemWorkspace.load(item.context, item.item_id)
    item_for_attempt = ItemWorkspace.load(item.context, item.item_id)
    adapter = BusinessReviewAdapter(item_for_reconciliation, owner_ref="owner-Q-001")
    packet_path = item.work_root / "business_review.json"
    state_path = item.item_root / "item_state.json"
    packet_before = packet_path.read_bytes()

    result: list[object] = []
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            result.append(adapter.reconcile_active_repair_scope((finding,)))
        except BaseException as exc:  # keep the worker exception deterministic for the assertion below
            errors.append(exc)

    def begin_attempt() -> None:
        try:
            result.append(item_for_attempt.begin_attempt("race-lane", "Analytical Owner"))
        except BaseException as exc:
            errors.append(exc)

    if winner == "attempt":
        attempt_entered = threading.Event()
        release_attempt = threading.Event()
        original_persist = item_for_attempt._persist_state_unlocked

        def paused_persist(state: dict[str, object], *, touch: bool = True) -> None:
            attempt_entered.set()
            if not release_attempt.wait(5):
                raise AssertionError("attempt did not receive release signal")
            original_persist(state, touch=touch)

        monkeypatch.setattr(item_for_attempt, "_persist_state_unlocked", paused_persist)
        attempt_thread = threading.Thread(target=begin_attempt)
        reconciliation_thread = threading.Thread(target=reconcile)
        attempt_thread.start()
        assert attempt_entered.wait(5)
        reconciliation_thread.start()
        # The attempt owns the transition lock; reconciliation must wait and
        # then reject against the authoritative active-attempt state.
        release_attempt.set()
        attempt_thread.join(5)
        reconciliation_thread.join(5)
        assert not attempt_thread.is_alive()
        assert not reconciliation_thread.is_alive()
        assert len(result) == 1
        assert len(errors) == 1
        assert "no active attempt" in str(errors[0])
        assert packet_path.read_bytes() == packet_before
        state_after_attempt_bytes = state_path.read_bytes()
        state_after_attempt = json.loads(state_after_attempt_bytes)
        assert state_after_attempt["active_attempt_id"] == "A-001"
        assert state_path.read_bytes() == state_after_attempt_bytes
        return

    reconciliation_entered = threading.Event()
    release_reconciliation = threading.Event()
    original_read_packet = item_for_reconciliation._read_business_review

    def paused_read_packet() -> dict[str, object] | None:
        reconciliation_entered.set()
        if not release_reconciliation.wait(5):
            raise AssertionError("reconciliation did not receive release signal")
        return original_read_packet()

    monkeypatch.setattr(item_for_reconciliation, "_read_business_review", paused_read_packet)
    reconciliation_thread = threading.Thread(target=reconcile)
    attempt_thread = threading.Thread(target=begin_attempt)
    reconciliation_thread.start()
    assert reconciliation_entered.wait(5)
    attempt_thread.start()
    release_reconciliation.set()
    reconciliation_thread.join(5)
    attempt_thread.join(5)
    assert not reconciliation_thread.is_alive()
    assert not attempt_thread.is_alive()
    assert not errors
    assert len(result) == 2
    upgraded = json.loads(packet_path.read_text(encoding="utf-8"))
    assert upgraded["repair_active"] is True
    assert "work/.analysis-run" in upgraded["allowed_dependencies"]
    state_after_both = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_after_both["business_repair_count"] == 1
    assert state_after_both["active_attempt_id"] == "A-001"


def test_facade_rejects_duplicate_or_unassigned_evidence_and_specialist_records(tmp_path: Path) -> None:
    analyst, _item = _workspace(tmp_path)
    source_id = analyst.search_sources("orders", limit=1)[0].source_id
    with pytest.raises(ValueError, match="duplicates"):
        analyst.select_sources((source_id, source_id), purpose="duplicate")

    evidence = EvidenceNote(
        evidence_id="E-001",
        conclusion="Two bounded rows exist.",
        method="Count rows.",
        evidence_refs=(source_id,),
    )
    analyst.record_evidence(evidence)
    with pytest.raises(ValueError, match="already recorded"):
        analyst.record_evidence(evidence)

    orphan = SpecialistMemo(
        memo_id="M-ORPHAN",
        task_id="S-UNKNOWN",
        conclusion="Orphan memo.",
        method="None.",
        evidence_refs=(source_id,),
    )
    with pytest.raises(ValueError, match="unknown task_id"):
        analyst.record_specialist_memo(orphan)

    tasks = [
        SpecialistTask(
            task_id=f"S-{index}",
            specialty="data_quality",
            question=f"Check bounded condition {index}.",
            expected_output="Evidence memo.",
            source_ids=(source_id,),
        )
        for index in range(1, 5)
    ]
    analyst.assign_specialist(tasks[0])
    with pytest.raises(ValueError, match="already recorded"):
        analyst.assign_specialist(tasks[0])
    analyst.record_specialist_memo(
        SpecialistMemo(
            memo_id="M-1",
            task_id="S-1",
            conclusion="Bounded check passed.",
            method="Inspect rows.",
            evidence_refs=(source_id,),
        )
    )
    with pytest.raises(ValueError, match="already recorded|already has"):
        analyst.record_specialist_memo(
            SpecialistMemo(
                memo_id="M-1",
                task_id="S-1",
                conclusion="Duplicate.",
                method="Inspect rows.",
                evidence_refs=(source_id,),
            )
        )
    analyst.assign_specialist(tasks[1])
    analyst.assign_specialist(tasks[2])
    analyst.assign_specialist(tasks[3])
    assert tuple(task.task_id for task in analyst.specialist_tasks()) == ("S-1", "S-2", "S-3", "S-4")


def test_analyst_facade_runs_deterministic_code_without_transferring_ownership(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    attempt = item.begin_attempt("analytical-owner", "Analytical Owner")
    script = item.work_root / "calculations" / "summary.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        textwrap.dedent(
            """
            import json
            from pathlib import Path

            Path("analysis.json").write_text(json.dumps({"status": "bounded"}), encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    report = analyst.run_analysis(
        script,
        outputs=(item.work_root / "analysis.json",),
        deterministic_outputs=(item.work_root / "analysis.json",),
    )
    assert report.succeeded
    assert [receipt.phase for receipt in report.receipts] == ["smoke", "full", "full"]
    item.finish_attempt(attempt.attempt_id, status="completed")


def test_strict_draft_serialization_rejects_nonfinite_numbers_before_review(tmp_path: Path) -> None:
    _, item = _workspace(tmp_path)
    with pytest.raises(ValueError, match="Out of range float values"):
        item.write_draft({"answer": float("nan")})
    assert not item.draft_root.exists()


def test_later_item_reuses_accepted_lem_and_prepared_asset(tmp_path: Path) -> None:
    """Accepted Q1 semantics are discoverable by Q2, without observations."""

    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,status\nO-1,closed\nO-2,open\n")

    context = RunContext("RUN-SEMANTIC-REUSE", tmp_path / "run", (input_root,))
    lifecycle = RunLifecycle.create(context, ("Q-001", "Q-002"))
    q1 = ItemWorkspace.create(context, "Q-001", original_text="Describe the order semantics.")
    q1_bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), q1)
    q1_analyst = AnalystWorkspace(q1_bound, owner_ref="owner-Q-001")
    q1_analyst.begin_analysis(objective="Describe reusable order semantics.", strategy="Use accepted definitions.")
    effective_period = "2023-12-19/2024-07-04"
    prepared = q1_analyst.prepare_data(
        "orders-reusable",
        [{"order_id": "O-1", "status": "closed"}, {"order_id": "O-2", "status": "open"}],
        scope="reusable",
        effective_period=effective_period,
    )
    assert prepared.effective_period == effective_period
    prepared_sidecar = json.loads((Path(prepared.location).parent / "orders-reusable.descriptor.json").read_text(encoding="utf-8"))
    assert prepared_sidecar["effective_period"] == effective_period
    assert prepared_sidecar == prepared.to_dict()
    assert q1_analyst.prepare_data(
        "orders-reusable",
        [{"order_id": "O-1", "status": "closed"}, {"order_id": "O-2", "status": "open"}],
        scope="reusable",
        effective_period=effective_period,
    ) == prepared
    relationship = q1_analyst.record_analytical_relationship(
        relationship_id="order-status",
        source_id="order",
        target_id="order",
        cardinality="one_to_one",
        join_keys=({"source_field": "order_id", "target_field": "order_id"},),
        matched_pairs=2,
        source_population=2,
        target_population=2,
        matched_source_count=2,
        matched_target_count=2,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        as_of=None,
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/plan.json",),
        publishable=True,
    )
    relationship_payload = relationship.to_dict()
    q1_analyst.submit_answer("Two bounded order rows were supplied.")
    q1.record_review("accept", reviewer_ref="reviewer-Q-001")
    q1.accept(accepted_refs=("work/plan.json", "work/analytical_relationships.jsonl"))

    session = IntegrationSession.create(
        context,
        q1,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="integration-Q-001",
    )
    session.add_ontology_item(
        {"item_id": "order", "item_type": "entity", "label": "Order"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.add_relationship(
        {
            "relationship_id": relationship_payload["relationship_id"],
            "analysis_relationship_id": relationship_payload["relationship_id"],
            "source_id": relationship_payload["source_id"],
            "target_id": relationship_payload["target_id"],
            "cardinality": relationship_payload["cardinality"],
            "join_keys": relationship_payload["join_keys"],
            "matched_pairs": relationship_payload["matched_pairs"],
            "source_population": relationship_payload["source_population"],
            "target_population": relationship_payload["target_population"],
            "matched_source_count": relationship_payload["matched_source_count"],
            "matched_target_count": relationship_payload["matched_target_count"],
            "source_coverage": relationship_payload["source_coverage"],
            "target_coverage": relationship_payload["target_coverage"],
            "date_authority": relationship_payload["date_authority"],
            "as_of": relationship_payload["as_of"],
            "limitations": relationship_payload["limitations"],
            "evidence_refs": relationship_payload["evidence_refs"],
        },
        scope="question",
        evidence_refs=("work/analytical_relationships.jsonl", "work/plan.json"),
    )
    session.add_metric(
        {"metric_id": "observed-count", "label": "Observed count", "value": 2},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.add_claim(
        {"claim_id": "observed-claim", "claim": "two supplied rows"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.add_dashboard_fact(
        {"fact_id": "dashboard-fact", "value": 2},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    session.register_prepared_asset(prepared)
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()

    q2 = ItemWorkspace.create(context, "Q-002", original_text="Reuse the prior order semantics.")
    q2_bound = BoundAnalysisContext.create_from_transitioned_catalog(context, q2, q1_bound, lifecycle)
    context_manifest = json.loads(q2_bound.manifest_path.read_text(encoding="utf-8"))
    assert len(q2_bound.manifest_path.read_bytes()) < 128 * 1024
    assert "ontology_bundle" not in context_manifest
    assert set(context_manifest["semantic_snapshot"]) == {
        "schema_version",
        "snapshot_hash",
        "manifest_ref",
        "manifest_hash",
        "counts",
    }
    assert len(list((context.run_root / "semantic_store" / "snapshots").iterdir())) == 1
    q2_analyst = AnalystWorkspace(q2_bound, owner_ref="owner-Q-002")
    brief = q2_analyst.brief()
    assert brief.ontology_item_count == 2  # order + generated relationship item
    assert brief.ontology_relationship_count == 1
    assert brief.prepared_asset_count == 1
    assert {item.item_id for item in q2_analyst.search_ontology()} == {"order", "order-status"}
    assert q2_analyst.search_ontology(item_type="relationship")[0].item_id == "order-status"
    assert q2_analyst.search_prepared_assets()[0].prepared_asset_id == "orders-reusable"
    assert q2_analyst.search_prepared_assets()[0].effective_period == effective_period
    assert q2_analyst.select_ontology(("order", "order-status"), purpose="join order semantics")
    loaded = q2_analyst.load_prepared_asset("orders-reusable")
    loaded_again = q2_analyst.load_prepared_asset("orders-reusable")
    selected_prepared = q2_analyst.select_prepared_assets(("orders-reusable",), purpose="reuse prepared order rows")
    assert selected_prepared[0].effective_period == effective_period
    assert [dict(row) for row in loaded] == [
        {"order_id": "O-1", "status": "closed"},
        {"order_id": "O-2", "status": "open"},
    ]
    assert [dict(row) for row in loaded_again] == [
        {"order_id": "O-1", "status": "closed"},
        {"order_id": "O-2", "status": "open"},
    ]
    with pytest.raises(ValueError, match="duplicates"):
        q2_analyst.select_ontology(("order", "order"), purpose="duplicate")
    with pytest.raises(KeyError, match="unknown ontology"):
        q2_analyst.select_ontology(("missing",), purpose="unknown")
    trace = (q2.work_root / "semantic_selections.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(trace) == 3
    assert sum(json.loads(line)["purpose"] == "load prepared asset" for line in trace) == 1
    assert all(json.loads(line)["owner_ref"] == "owner-Q-002" for line in trace)
    assert all("selected_ids" not in json.loads(line) for line in trace)
    assert all("semantic_scope" not in json.loads(line) for line in trace)
    assert all(json.loads(line)["selection_ref"] for line in trace)

    registry = PreparedAssetRegistry(context)
    registry_records = [json.loads(line) for line in registry.registry_path.read_text(encoding="utf-8").splitlines()]
    registry_records[0]["scope"] = "requirement_scoped"
    registry.registry_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for record in registry_records),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="descriptor changed"):
        q2_analyst.search_prepared_assets()


def test_prepared_fidelity_repair_materializes_new_public_candidate_and_reuses_exact_refs(tmp_path: Path) -> None:
    """A prepared-record repair replaces its descriptor without stale candidate identity."""

    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,status\nO-1,closed\nO-2,open\n")

    context = RunContext("RUN-PREPARED-REPAIR", tmp_path / "run", (input_root,))
    lifecycle = RunLifecycle.create(context, ("Q-001", "Q-002"))
    q1 = ItemWorkspace.create(context, "Q-001", original_text="Prepare reusable orders.")
    q1_bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), q1)
    q1_analyst = AnalystWorkspace(q1_bound, owner_ref="owner-Q-001")
    rows = [{"order_id": "O-1", "status": "closed"}, {"order_id": "O-2", "status": "open"}]
    initial = q1_analyst.prepare_data(
        "orders-initial",
        rows,
        scope="reusable",
        ontology_refs=("ontology:wrong",),
    )
    q1_analyst.submit_answer("The initial reusable order candidate is provisional.")
    q1.record_review("accept", reviewer_ref="reviewer-Q-001")
    q1.accept(accepted_refs=("work/plan.json",))

    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(
        context,
        q1,
        registry,
        "integration-owner",
        invocation_id="integration-Q-001",
    )
    staged_record_id = session.register_prepared_asset(initial, asset_record_id="Q1-PREPARED-ORDERS")
    session.record_fidelity_review(
        "repair_once",
        findings=[{"message": "correct ontology references", "record_id": staged_record_id, "parts": ["payload"]}],
        checked_record_ids=(staged_record_id,),
    )

    corrected = q1_analyst.prepare_data(
        "orders-corrected",
        rows,
        scope="reusable",
        ontology_refs=("ontology:orders", "ontology:status"),
    )
    assert corrected.prepared_asset_id != initial.prepared_asset_id
    assert corrected.ontology_refs == ("ontology:orders", "ontology:status")
    sidecar = Path(corrected.location).parent / "orders-corrected.descriptor.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == corrected.to_dict()

    session.correct_record(staged_record_id, corrected.to_dict())
    replacement = next(record for record in session.records if record.record_id == staged_record_id)
    assert replacement.record_id == staged_record_id
    assert replacement.payload["prepared_asset_id"] == corrected.prepared_asset_id
    assert replacement.payload["location"] == corrected.location
    session.build_fidelity_packet()
    targeted = session.record_fidelity_review("accept", checked_record_ids=(staged_record_id,))
    assert targeted.review_kind == "targeted"
    session.commit()

    assert registry.search(prepared_asset_id="orders-initial") == ()
    accepted = registry.search(prepared_asset_id="orders-corrected")
    assert accepted == (corrected,)

    q2 = ItemWorkspace.create(context, "Q-002", original_text="Reuse corrected order semantics.")
    q2_bound = BoundAnalysisContext.create_from_transitioned_catalog(context, q2, q1_bound, lifecycle)
    q2_analyst = AnalystWorkspace(q2_bound, owner_ref="owner-Q-002")
    found = q2_analyst.search_prepared_assets(query="ontology:orders")
    assert [asset.prepared_asset_id for asset in found] == ["orders-corrected"]
    assert found[0].ontology_refs == ("ontology:orders", "ontology:status")
    selected = q2_analyst.select_prepared_assets(("orders-corrected",), purpose="reuse corrected order rows")
    assert selected[0].ontology_refs == ("ontology:orders", "ontology:status")
    loaded = q2_analyst.load_prepared_asset("orders-corrected")
    assert tuple(dict(row) for row in loaded) == tuple(rows)


def test_reserved_semantic_snapshot_schema_cannot_be_injected_into_fresh_context(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    with pytest.raises(ValueError, match="program-owned"):
        BoundAnalysisContext.create(
            analyst.context.context,
            analyst.context.source_identity,
            item,
            ontology_bundle={
                "schema_version": "auto_foundry.semantic_reuse_snapshot.v1",
                "snapshot_hash": "forged",
                "ontology": [{"item_id": "forged", "item_type": "entity", "label": "Forged"}],
            },
        )


def test_reserved_semantic_snapshot_fresh_manifest_reload_requires_inheritance(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    manifest = json.loads(analyst.context.manifest_path.read_text(encoding="utf-8"))
    manifest["ontology_bundle"] = {
        "schema_version": "auto_foundry.semantic_reuse_snapshot.v1",
        "projection_hash": "0" * 64,
        "item_order": [],
        "source_item_ids": [],
        "ontology": [],
        "relationships": {},
        "prepared_assets": [],
        "counts": {"ontology": 0, "relationships": 0, "prepared_assets": 0},
        "snapshot_hash": "0" * 64,
    }
    unsigned = dict(manifest)
    unsigned.pop("manifest_hash")
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    analyst.context.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="target-bound catalog inheritance"):
        load_bound_analysis_context(analyst.context.context, path=analyst.context.manifest_path, item_workspace=item)


def _seed_historical_integration_failure(item: ItemWorkspace, session: IntegrationSession) -> None:
    """Seed a pre-existing failure snapshot for historical projection replay.

    New accepted integrations cannot create this state.  This fixture models
    an older run so the projection reader remains covered without reopening a
    production terminalization path.
    """

    failure_root = item.item_root / "integration" / "technical_failure"
    failure_root.mkdir(parents=True)
    unsigned = {
        "schema_version": "1",
        "session_id": session.session_id,
        "item_id": item.item_id,
        "owner_id": session.owner_id,
        "status": "technical_failure",
        "accepted_content_hash": session.bundle.content_hash,
        "reason": "historical fixture failure",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    manifest = {
        **unsigned,
        "manifest_hash": hashlib.sha256(
            (
                json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        ).hexdigest(),
    }
    (failure_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    state_path = item.item_root / "item_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["integration_state"] = "technical_failure"
    state["integration_manifest_hash"] = manifest["manifest_hash"]
    state["integration_manifest_ref"] = "integration/technical_failure/manifest.json"
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_snapshot_skips_pending_integration_and_keeps_later_commits(tmp_path: Path) -> None:
    """A recoverable predecessor stays pending; later commits remain visible."""

    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id\nO-1\n")

    context = RunContext("RUN-SEMANTIC-FAILURE-FRONTIER", tmp_path / "run", (input_root,))
    lifecycle = RunLifecycle.create(context, ("Q-001", "Q-002", "Q-003", "Q-004"))
    q1 = ItemWorkspace.create(context, "Q-001", original_text="Q1")
    q1_bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), q1)

    def accept(item: ItemWorkspace, label: str) -> None:
        item.write_plan({"item_id": item.item_id, "objective": label})
        item.write_draft({"answer": label})
        item.record_review("accept", reviewer_ref=f"reviewer-{item.item_id}")
        item.accept(accepted_refs=("work/plan.json",))

    accept(q1, "Q1")
    q1_session = IntegrationSession.create(
        context, q1, PreparedAssetRegistry(context), "integration-owner", invocation_id="integration-Q1"
    )
    q1_session.add_ontology_item(
        {"item_id": "q1-definition", "item_type": "entity", "label": "Q1 definition"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    q1_session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in q1_session.records))
    q1_session.commit()

    q2 = ItemWorkspace.create(context, "Q-002", original_text="Q2")
    q2_bound = BoundAnalysisContext.create_from_transitioned_catalog(context, q2, q1_bound, lifecycle)
    accept(q2, "Q2")
    q2_session = IntegrationSession.create(
        context, q2, PreparedAssetRegistry(context), "integration-owner", invocation_id="integration-Q2"
    )
    q2_session.add_ontology_item(
        {"item_id": "failed-definition", "item_type": "entity", "label": "Must not be reused"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    q2_state_path = q2.item_root / "item_state.json"
    before_q2_state = q2_state_path.read_bytes()
    failure_path = q2_session.staging_root.parent / "technical_failure" / "manifest.json"
    incident = q2_session.mark_technical_failure("integration unavailable")
    assert incident["status"] == "pending"
    assert incident["recoverable"] is True
    assert incident["continuation"] == "same_session"
    assert q2_session.status == "open"
    assert q2.integration_state == "pending"
    assert not failure_path.exists()
    assert q2_state_path.read_bytes() == before_q2_state

    # Preserve historical-reader coverage with an explicit old-run fixture,
    # never through the now-forbidden production failure transition.
    _seed_historical_integration_failure(q2, q2_session)
    q2_session.release()

    q3 = ItemWorkspace.create(context, "Q-003", original_text="Q3")
    q3_bound = BoundAnalysisContext.create_from_transitioned_catalog(context, q3, q2_bound, lifecycle)
    accept(q3, "Q3")
    q3_session = IntegrationSession.create(
        context, q3, PreparedAssetRegistry(context), "integration-owner", invocation_id="integration-Q3"
    )
    q3_session.add_ontology_item(
        {"item_id": "q3-definition", "item_type": "entity", "label": "Q3 definition"},
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    q3_session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in q3_session.records))
    q3_session.commit()

    q4 = ItemWorkspace.create(context, "Q-004", original_text="Q4")
    q4_bound = BoundAnalysisContext.create_from_transitioned_catalog(context, q4, q3_bound, lifecycle)
    ids = {item.item_id for item in AnalystWorkspace(q4_bound, owner_ref="owner-Q-004").search_ontology()}
    assert ids == {"q1-definition", "q3-definition"}


def test_requirement_refresh_snapshot_uses_archived_target_frontier_and_current_views(tmp_path: Path) -> None:
    """A reopened owner sees its reviewed predecessor and live successors only."""

    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive_path = input_root / "fixture.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "id\nA-1\n")

    context = RunContext("RUN-SEMANTIC-REFRESH-FRONTIER", tmp_path / "run", (input_root,))
    lifecycle = RunLifecycle.create(context, ("REQ-1", "REQ-2"), mode="requirement")

    def accepted(item_id: str, *, relationship_id: str | None = None, relation_object_id: str | None = None) -> ItemWorkspace:
        item = ItemWorkspace.create(context, item_id, mode="requirement", original_text=item_id)
        item.write_plan({"item_id": item_id, "offline": True})
        accepted_refs = ["work/plan.json"]
        if relationship_id is not None:
            assert relation_object_id is not None
            bound = BoundAnalysisContext.create_for_requirement(
                context,
                DataAssetRef.from_path(archive_path),
                item,
                lifecycle,
            )
            AnalystWorkspace(bound, owner_ref=f"owner-{item_id}").record_analytical_relationship(
                relationship_id=relationship_id,
                source_id=relation_object_id,
                target_id=relation_object_id,
                cardinality="one_to_one",
                join_keys=({"source_field": "id", "target_field": "id"},),
                matched_pairs=1,
                source_population=1,
                target_population=1,
                matched_source_count=1,
                matched_target_count=1,
                source_coverage=1.0,
                target_coverage=1.0,
                date_authority="fixture",
                limitations=(),
                evidence_refs=("work/plan.json",),
                publishable=True,
            )
            accepted_refs.append("work/analytical_relationships.jsonl")
        item.write_draft({"answer": item_id})
        item.record_review("accept", reviewer_ref=f"reviewer-{item_id}")
        item.accept(accepted_refs=tuple(accepted_refs))
        return item

    def commit_records(item: ItemWorkspace, records: tuple[tuple[str, str, str], ...]) -> None:
        session = IntegrationSession.create(
            context,
            item,
            PreparedAssetRegistry(context),
            "integration-owner",
            invocation_id=f"inv-{item.item_id}",
        )
        for kind, object_id, label in records:
            if kind == "ontology":
                session.add_ontology_item(
                    OntologyItem(
                        item_id=object_id,
                        item_type="entity",
                        label=label,
                        source_refs=(f"evidence://{object_id}",),
                    ),
                    scope="requirement",
                    evidence_refs=("work/plan.json",),
                )
            else:
                session.add_relationship(
                        {
                            "relationship_id": object_id,
                            "analysis_relationship_id": object_id,
                            "source_id": label,
                            "target_id": label,
                            "label": object_id,
                            "cardinality": "one_to_one",
                                "join_keys": [{"source_field": "id", "target_field": "id"}],
                            "matched_pairs": 1,
                            "source_population": 1,
                            "target_population": 1,
                            "matched_source_count": 1,
                            "matched_target_count": 1,
                            "source_coverage": 1.0,
                            "target_coverage": 1.0,
                            "date_authority": "fixture",
                            "as_of": None,
                            "limitations": [],
                            "evidence_refs": ["work/plan.json"],
                        },
                        scope="requirement",
                        evidence_refs=("work/plan.json",),
                )
        session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
        session.commit()

    def archive_item(item_id: str, generation_id: str) -> None:
        source = context.run_root / "requirements" / item_id
        history = context.run_root / "history" / "requirements" / item_id / generation_id
        history.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, history)

    # First generation: REQ-1 is the target's reviewed predecessor; REQ-2 is
    # the other item's predecessor that the next generation will supersede.
    req1_old = accepted("REQ-1", relationship_id="req1-rel-v1", relation_object_id="req1-v1")
    commit_records(req1_old, (("ontology", "req1-v1", "REQ-1"), ("relationship", "req1-rel-v1", "req1-v1")))
    req2_old = accepted("REQ-2", relationship_id="req2-rel-v1", relation_object_id="req2-v1")
    commit_records(req2_old, (("ontology", "req2-v1", "REQ-2"), ("relationship", "req2-rel-v1", "req2-v1")))
    archive_item("REQ-1", "G-0002")
    archive_item("REQ-2", "G-0003")

    # REQ-2's current successor explicitly supersedes both prior objects.  It
    # is committed before the REQ-1 fresh head is opened, so it is in the
    # target's before-current-candidate frontier.
    req2_new = accepted("REQ-2")
    session = IntegrationSession.create(
        context,
        req2_new,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-REQ-2-successor",
    )
    session.add_knowledge_delta(
        KnowledgeDelta(
            "req2-ontology-v2",
            "add_ontology_item",
            {
                "item_id": "req2-v2",
                "item_type": "entity",
                "label": "REQ-2 current",
                "source_refs": ["evidence://req2-v2"],
            },
            evidence_refs=("work/plan.json",),
            supersedes=(LEMRef("ontology", "req2-v1"),),
            accepted=True,
        ),
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )
    session.add_knowledge_delta(
        KnowledgeDelta(
            "req2-relationship-v2",
            "add_relationship",
            {
                "relationship_id": "req2-rel-v2",
                "analysis_relationship_id": "req2-rel-v2",
                "source_id": "req2-v2",
                "target_id": "req2-v2",
                "label": "REQ-2 current relationship",
                "cardinality": "one_to_one",
                "join_keys": [{"source_field": "id", "target_field": "id"}],
                "matched_pairs": 1,
                "source_population": 1,
                "target_population": 1,
                "matched_source_count": 1,
                "matched_target_count": 1,
                "source_coverage": 1.0,
                "target_coverage": 1.0,
                "date_authority": "fixture",
                "as_of": None,
                "limitations": [],
                "evidence_refs": ["work/plan.json"],
            },
            evidence_refs=("work/plan.json",),
            supersedes=(LEMRef("relationship", "req2-rel-v1"),),
            accepted=True,
        ),
        scope="requirement",
        evidence_refs=("work/plan.json",),
    )
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()

    # The fresh REQ-1 head has an uncommitted candidate in staging.  It must
    # remain invisible while its archived accepted predecessor is reusable.
    req1_current = ItemWorkspace.create(context, "REQ-1", mode="requirement", original_text="REQ-1 refreshed")
    req1_current.write_plan({"item_id": "REQ-1", "candidate_ontology": "req1-uncommitted"})
    req1_current.write_draft({"answer": "uncommitted candidate"})

    bound = BoundAnalysisContext.create_for_requirement(
        context,
        DataAssetRef.from_path(archive_path),
        req1_current,
        lifecycle,
    )
    analyst = AnalystWorkspace(bound, owner_ref="owner-REQ-1")
    found = analyst.search_ontology()
    found_ids = {item.item_id for item in found}
    assert {"req1-v1", "req1-rel-v1", "req2-v2", "req2-rel-v2"}.issubset(found_ids)
    assert {"req2-v1", "req2-rel-v1", "req1-uncommitted"}.isdisjoint(found_ids)

    snapshot_ontology = SemanticSnapshotStore.records(context, bound.semantic_snapshot_ref, "ontology")["ontology"]
    snapshot_relationships = SemanticSnapshotStore.records(context, bound.semantic_snapshot_ref, "relationships")["relationships"]
    req1_item = next(item for item in snapshot_ontology if item["item_id"] == "req1-v1")
    req1_relationship = next(item for item in snapshot_relationships if item["relationship_id"] == "req1-rel-v1")
    assert req1_item["source_refs"] == ["evidence://req1-v1"]
    assert req1_relationship["evidence_refs"] == ["work/plan.json"]


@pytest.mark.parametrize(
    ("field", "value"),
    (("target_sections", ("item_state",)), ("semantic_categories", ("lifecycle",))),
)
def test_review_finding_rejects_program_internal_targets(field: str, value: str) -> None:
    values = {
        "finding_id": "BR-001",
        "target_sections": ("answer",),
        "semantic_categories": ("answer",),
        "problem": "A bounded problem.",
        "evidence": "A bounded fact.",
        "required_change": "A bounded correction.",
    }
    values[field] = value
    with pytest.raises(ValueError):
        ReviewFinding(**values)


def test_identity_domain_proposals_are_typed_idempotent_and_item_local(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    proposal = analyst.propose_identity_domain(
        "customer-domain",
        "customer",
        "The two source representations describe one business object.",
        ("orders.csv:customer_id",),
        ("orders", "customers"),
    )
    assert isinstance(proposal, IdentityDomainProposal)
    analyst.propose_identity_domain(
        "customer-domain",
        "customer",
        "The two source representations describe one business object.",
        ("orders.csv:customer_id",),
        ("orders", "customers"),
    )
    assert len(analyst.read_identity_domain_proposals()) == 1

    # Simulate a proposal admitted by the pre-fix owner-rotation behavior.
    # Retrying the same semantic proposal must be idempotent without rewriting
    # its append-only provenance row.
    proposal_path = item.work_root / "identity_domain_proposals.jsonl"
    historical = json.loads(proposal_path.read_text(encoding="utf-8"))
    historical["owner_ref"] = "owner-legacy-transport-label"
    proposal_path.write_text(
        json.dumps(historical, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    historical_bytes = proposal_path.read_bytes()
    analyst.propose_identity_domain(
        "customer-domain",
        "customer",
        "The two source representations describe one business object.",
        ("orders.csv:customer_id",),
        ("orders", "customers"),
    )
    assert proposal_path.read_bytes() == historical_bytes
    assert len(analyst.read_identity_domain_proposals()) == 1

    with pytest.raises(ValueError, match="dedupe identity conflicts"):
        analyst.propose_identity_domain(
            "customer-domain",
            "customer",
            "A conflicting rationale is not an idempotent retry.",
            ("orders.csv:customer_id",),
            ("orders", "customers"),
        )

    other = ItemWorkspace.create(analyst.context.context, "Q-002", original_text="Other item")
    other_bound = BoundAnalysisContext.create(
        analyst.context.context,
        analyst.context.source_identity,
        other,
        workbench=analyst.context.workbench,
    )
    other_analyst = AnalystWorkspace(other_bound, owner_ref="owner-Q-002")
    other_analyst.propose_identity_domain(
        "customer-domain",
        "customer",
        "Other item evidence.",
        ("orders.csv:customer_id",),
        ("orders",),
    )
    assert len(other_analyst.read_identity_domain_proposals()) == 1
    assert len(analyst.read_identity_domain_proposals()) == 1


def test_identity_domain_successor_is_append_only_cas_bound_and_effective(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    predecessor = analyst.propose_identity_domain(
        "shared-product",
        "product_material_sku",
        "The first proposal used the narrower material representation.",
        ("materials.csv:sku",),
        ("materials",),
    )
    proposal_path = item.work_root / "identity_domain_proposals.jsonl"
    predecessor_bytes = proposal_path.read_bytes()

    successor = analyst.supersede_identity_domain_proposal(
        "shared-product",
        "product",
        "The shared domain is one product class and retains the material scope.",
        ("materials.csv:sku", "catalog.csv:product_id"),
        ("materials", "catalog"),
        expected_predecessor_hash=predecessor.digest,
    )
    assert successor.revision == 2
    assert successor.supersedes_hash == predecessor.digest
    assert successor.superseded_object_type == "product_material_sku"
    assert successor.object_type == "product"
    assert proposal_path.read_bytes().startswith(predecessor_bytes)

    effective = analyst.read_identity_domain_proposals()
    history = analyst.read_identity_domain_proposal_history()
    assert len(effective) == 1
    assert effective[0] == successor
    assert [proposal.revision for proposal in history] == [1, 2]
    assert history[0].digest == predecessor.digest
    assert history[0].object_type == predecessor.object_type
    assert history[0].rationale == predecessor.rationale
    assert history[1].superseded_object_type == predecessor.object_type

    # Retrying the exact CAS is idempotent and does not append another row.
    successor_bytes = proposal_path.read_bytes()
    retried = analyst.supersede_identity_domain_proposal(
        "shared-product",
        "product",
        "The shared domain is one product class and retains the material scope.",
        ("materials.csv:sku", "catalog.csv:product_id"),
        ("materials", "catalog"),
        expected_predecessor_hash=predecessor.digest,
    )
    assert retried == successor
    assert proposal_path.read_bytes() == successor_bytes

    with pytest.raises(ValueError, match="successor conflicts"):
        analyst.supersede_identity_domain_proposal(
            "shared-product",
            "supplier",
            "A divergent branch is not a retry.",
            ("suppliers.csv:id",),
            ("suppliers",),
            expected_predecessor_hash=predecessor.digest,
        )
    with pytest.raises(ValueError, match="predecessor is unknown"):
        analyst.supersede_identity_domain_proposal(
            "shared-product",
            "product",
            "The shared domain is one product class and retains the material scope.",
            ("materials.csv:sku", "catalog.csv:product_id"),
            ("materials", "catalog"),
            expected_predecessor_hash="a" * 64,
        )


def test_mark_waiting_on_resolution_delegates_without_runtime_import(tmp_path: Path) -> None:
    analyst, _item = _workspace(tmp_path)

    class RuntimeStub:
        def __init__(self) -> None:
            self.calls = []

        def mark_waiting_on_resolution(
            self,
            *,
            requirement_id: str,
            domain_ids: tuple[str, ...],
            reason: str,
            owner_ref: str,
        ) -> str:
            self.calls.append((requirement_id, domain_ids, reason, owner_ref))
            return "released"

    runtime = RuntimeStub()
    assert analyst.mark_waiting_on_resolution(runtime, ("customer-domain",), "Need reviewed identity mapping") == "released"
    assert runtime.calls == [("Q-001", ("customer-domain",), "Need reviewed identity mapping", "owner-Q-001")]

    with pytest.raises(TypeError, match="public mark_waiting_on_resolution"):
        analyst.mark_waiting_on_resolution(object(), ("customer-domain",), "Missing runtime API")


def test_analytical_relationship_evidence_records_positive_and_negative_audits(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    with pytest.raises(ValueError, match="date_authority or as_of"):
        analyst.record_analytical_relationship(
            relationship_id="orders-customers-no-time",
            source_id="orders",
            target_id="customers",
            cardinality="many_to_one",
            join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
            matched_pairs=2,
            source_population=2,
            target_population=1,
            matched_source_count=2,
            matched_target_count=1,
            source_coverage=1.0,
            target_coverage=1.0,
            limitations=("Synthetic fixture only",),
            evidence_refs=("work/evidence.jsonl#E-no-time",),
            publishable=True,
        )
    positive = analyst.record_analytical_relationship(
        relationship_id="orders-customers",
        source_id="orders",
        target_id="customers",
        cardinality="many_to_one",
        join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
        matched_pairs=2,
        source_population=2,
        target_population=1,
        matched_source_count=2,
        matched_target_count=1,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        as_of=None,
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/evidence.jsonl#E-1",),
        publishable=True,
    )
    negative = analyst.record_analytical_relationship(
        relationship_id="orders-products-no-join",
        source_id="orders",
        target_id="products",
        audit_id="orders-products-no-join-audit",
        no_relationship_reason="No authoritative key or reviewed identity mapping was found.",
        evidence_refs=("work/evidence.jsonl#E-2",),
        publishable=False,
    )
    analyst.record_analytical_relationship(
        relationship_id="orders-customers",
        source_id="orders",
        target_id="customers",
        cardinality="many_to_one",
        join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
        matched_pairs=2,
        source_population=2,
        target_population=1,
        matched_source_count=2,
        matched_target_count=1,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        as_of=None,
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/evidence.jsonl#E-1",),
        publishable=True,
    )
    records = analyst.read_analytical_relationships()
    assert [record.relationship_id for record in records] == [positive.relationship_id, negative.relationship_id]
    assert records[0].publishable is True
    assert records[1].no_relationship_reason
    assert len(item.read_analytical_relationships()) == 2
    payloads = [json.loads(line) for line in (item.work_root / "analytical_relationships.jsonl").read_text(encoding="utf-8").splitlines()]
    assert payloads[0]["record_kind"] == "analytical_relationship"
    assert payloads[1]["record_kind"] == "relationship_audit"
    assert payloads[0]["join_keys"] == [{"source_field": "customer_id", "target_field": "customer_id"}]
    assert "source_ontology_id" not in payloads[0]
    assert "join_key_mappings" not in payloads[0]

    with pytest.raises(TypeError):
        analyst.record_analytical_relationship(
            relationship_id="legacy-fields",
            source_ontology_id="orders",
            target_ontology_id="customers",
        )

    analyst.submit_answer("Terminal answer")
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    with pytest.raises(ValueError, match="terminal"):
        analyst.record_analytical_relationship(
            relationship_id="after-terminal",
            source_id="orders",
            target_id="customers",
            cardinality="one_to_one",
            join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
            matched_pairs=1,
            source_population=1,
            target_population=1,
            matched_source_count=1,
            matched_target_count=1,
            source_coverage=1.0,
            target_coverage=1.0,
            date_authority="fixture-controlled snapshot",
            evidence_refs=("work/evidence.jsonl#E-terminal",),
            publishable=True,
        )


def _repair_relationship(
    relationship_id: str,
    *,
    evidence_ref: str = "work/evidence.jsonl#relationship",
    audit_id: str | None = None,
) -> AnalyticalRelationshipEvidence:
    return AnalyticalRelationshipEvidence(
        relationship_id=relationship_id,
        source_id="orders",
        target_id="customers",
        cardinality="many_to_one",
        join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
        matched_pairs=2,
        source_population=2,
        target_population=1,
        matched_source_count=2,
        matched_target_count=1,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        limitations=("Synthetic fixture only",),
        evidence_refs=(evidence_ref,),
        publishable=True,
        audit_id=audit_id,
    )


def _begin_relationship_repair(
    analyst: AnalystWorkspace,
    *,
    finding_id: str = "BR-RELATIONSHIPS",
) -> None:
    analyst.submit_answer(AnalystAnswer(answer="Initial relationship narrative.", method="Initial method."))
    analyst.review.record(
        "repair_once",
        reviewer_ref="relationship-reviewer",
        findings=(
            ReviewFinding(
                finding_id=finding_id,
                target_sections=("method",),
                semantic_categories=("method",),
                problem="The relationship evidence requires correction.",
                evidence="The first relationship row is stale.",
                required_change="Replace the owner-local relationship evidence.",
            ),
        ),
    )
    analyst.review.begin_repair()


def test_evidence_and_relationships_are_editable_by_replacement_using_stable_owner(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_evidence(_evidence_note_for_replacement("E-OLD", evidence_ref="orders.csv"))
    analyst.record_analytical_relationship(_repair_relationship("rel-old"))
    attempt = item.begin_attempt("lane-continued", "analytical_owner")
    assert item.state["active_attempt_id"] == attempt.attempt_id

    # A replacement process continues through the same logical owner binding.
    continued = AnalystWorkspace(analyst.context, owner_ref="continued-owner")
    assert continued.owner_ref == "owner-Q-001"
    evidence = (_evidence_note_for_replacement("E-NEW", evidence_ref="orders.csv"),)
    relationships = (_repair_relationship("rel-new"),)
    assert continued.replace_evidence_notes(evidence) == evidence
    assert continued.replace_analytical_relationships(relationships) == relationships
    assert item._read_analysis_owner()["owner_ref"] == "owner-Q-001"  # noqa: SLF001 - audit assertion

    evidence_path = item.work_root / "evidence.jsonl"
    relationship_path = item.work_root / "analytical_relationships.jsonl"
    evidence_bytes = evidence_path.read_bytes()
    relationship_bytes = relationship_path.read_bytes()
    evidence_mtime = evidence_path.stat().st_mtime_ns
    relationship_mtime = relationship_path.stat().st_mtime_ns
    assert continued.replace_evidence_notes(evidence) == evidence
    assert continued.replace_analytical_relationships(relationships) == relationships
    assert evidence_path.read_bytes() == evidence_bytes
    assert relationship_path.read_bytes() == relationship_bytes
    assert evidence_path.stat().st_mtime_ns == evidence_mtime
    assert relationship_path.stat().st_mtime_ns == relationship_mtime


def test_business_repairs_are_not_limited_to_two_cycles(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.submit_answer(AnalystAnswer(answer="Initial answer."))

    for index in range(1, 4):
        finding = ReviewFinding(
            finding_id=f"BR-FLEX-{index}",
            target_sections=("answer",),
            semantic_categories=("answer",),
            problem=f"Iteration {index} still needs work.",
            evidence="The reviewer supplied another concrete correction.",
            required_change="Revise the answer again.",
        )
        analyst.review.record(
            "repair_once",
            reviewer_ref=f"reviewer-{index}",
            findings=(finding,),
        )
        analyst.review.begin_repair()
        analyst.submit_answer(AnalystAnswer(answer=f"Corrected answer {index}."))

    assert item.state["business_repair_count"] == 3
    assert item.state["lifecycle_state"] == "work"
    assert item.state["review"]["status"] == "pending"


def obsolete_replace_analytical_relationships_full_subset_and_exact_retry(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    first = analyst.record_analytical_relationship(_repair_relationship("rel-1"))
    second = analyst.record_analytical_relationship(_repair_relationship("rel-2", evidence_ref="work/evidence.jsonl#two"))
    _begin_relationship_repair(analyst)

    replacement = _repair_relationship("rel-1", evidence_ref="work/evidence.jsonl#corrected")
    replaced = analyst.replace_analytical_relationships((replacement,), replace_ids=(first.relationship_id,))
    assert replaced == (replacement, second)
    assert analyst.read_analytical_relationships() == replaced
    path = item.work_root / "analytical_relationships.jsonl"
    content = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    packet = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(content).hexdigest()
    assert packet["after_artifact_hashes"]["work/analytical_relationships.jsonl"] == digest

    # An exact retry is a read-only operation and does not perturb the bytes
    # or mtime of the artifact (or the review packet).
    packet_bytes = (item.work_root / "business_review.json").read_bytes()
    assert analyst.replace_analytical_relationships((replacement,), replace_ids=(first.relationship_id,)) == replaced
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == mtime
    assert (item.work_root / "business_review.json").read_bytes() == packet_bytes

    full = _repair_relationship("rel-3", evidence_ref="work/evidence.jsonl#three")
    assert analyst.replace_analytical_relationships((full,)) == (full,)
    assert analyst.read_analytical_relationships() == (full,)


def obsolete_replace_analytical_relationships_rejects_stale_invalid_duplicate_and_unauthorized(
    tmp_path: Path,
) -> None:
    analyst, item = _workspace(tmp_path)
    original = analyst.record_analytical_relationship(_repair_relationship("rel-1"))
    _begin_relationship_repair(analyst, finding_id="BR-RELATIONSHIPS-NEGATIVE")
    artifact = item.work_root / "analytical_relationships.jsonl"
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    corrected = _repair_relationship("rel-1", evidence_ref="work/evidence.jsonl#corrected")

    with pytest.raises(ValueError, match="changed since"):
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        analyst.replace_analytical_relationships((corrected,), expected_artifact_hash=expected)
    # Restore the exact reviewed bytes before exercising the public repair API.
    artifact.write_bytes(_jsonl_bytes((original,), item_id=item.item_id, owner_ref=analyst.owner_ref))

    duplicate = _repair_relationship("rel-1", evidence_ref="work/evidence.jsonl#duplicate")
    with pytest.raises(ValueError, match="relationship_id values must be unique"):
        analyst.replace_analytical_relationships((corrected, duplicate))
    with pytest.raises(ValueError, match="replace_ids must identify"):
        analyst.replace_analytical_relationships((corrected,), replace_ids=("missing",))

    with pytest.raises(ValueError, match="owner_ref"):
        item.replace_analytical_relationships(
            (
                {
                    **corrected.to_dict(),
                    "item_id": item.item_id,
                    "owner_ref": "other-owner",
                },
            ),
            owner_ref="other-owner",
        )


def obsolete_replace_analytical_relationships_rolls_back_on_packet_failure_and_rejects_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyst, item = _workspace(tmp_path)
    original = analyst.record_analytical_relationship(_repair_relationship("rel-1"))
    _begin_relationship_repair(analyst, finding_id="BR-RELATIONSHIPS-ROLLBACK")
    artifact = item.work_root / "analytical_relationships.jsonl"
    prior_artifact = artifact.read_bytes()
    prior_packet = (item.work_root / "business_review.json").read_bytes()
    prior_state = (item.item_root / "item_state.json").read_bytes()
    corrected = _repair_relationship("rel-1", evidence_ref="work/evidence.jsonl#corrected")

    original_write = item._write_business_review

    def fail_packet(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected packet failure")

    monkeypatch.setattr(item, "_write_business_review", fail_packet)
    with pytest.raises(OSError, match="injected packet failure"):
        analyst.replace_analytical_relationships((corrected,))
    assert artifact.read_bytes() == prior_artifact
    assert (item.work_root / "business_review.json").read_bytes() == prior_packet
    assert (item.item_root / "item_state.json").read_bytes() == prior_state
    monkeypatch.setattr(item, "_write_business_review", original_write)

    target = tmp_path / "outside.jsonl"
    target.write_bytes(prior_artifact)
    artifact.unlink()
    artifact.symlink_to(target)
    with pytest.raises(Exception, match="symlink"):
        analyst.replace_analytical_relationships((corrected,))


def obsolete_replace_relationships_expands_answer_semantic_scope_but_rejects_unrelated_drift(
    tmp_path: Path,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_analytical_relationship(_repair_relationship("rel-answer-scope"))
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial bounded answer.",
            headline_findings=("Initial headline",),
            scope="Initial scope",
            limitations=("Initial limitation",),
            visuals=({"title": "Initial visual"},),
            evidence_refs=("work/plan.json",),
        )
    )
    finding = ReviewFinding(
        finding_id="BR-ANSWER-SCOPE-RELATIONSHIP",
        target_sections=("answer", "scope", "limitations", "visuals"),
        semantic_categories=("answer", "calculation", "presentation"),
        problem="The answer and relationship presentation need one coherent correction.",
        evidence="The prior relationship row and narrative are stale.",
        required_change="Correct the relationship and revise the bounded answer body.",
    )
    analyst.review.record("repair_once", reviewer_ref="answer-scope-reviewer", findings=(finding,))
    analyst.review.begin_repair()
    analyst.submit_answer(
        AnalystAnswer(
            answer="Corrected bounded answer.",
            headline_findings=("Corrected headline",),
            scope="Corrected scope",
            limitations=("Corrected limitation",),
            visuals=({"title": "Corrected visual"},),
            evidence_refs=("work/evidence.jsonl#corrected",),
        )
    )
    corrected = _repair_relationship(
        "rel-answer-scope",
        evidence_ref="work/evidence.jsonl#corrected",
    )
    assert analyst.replace_analytical_relationships((corrected,)) == (corrected,)

    # Unrelated answer-envelope drift remains closed even though the answer
    # semantic finding owns the mutable AnalystAnswer body.
    draft_before = item.draft_root.read_bytes()
    draft_value = json.loads(draft_before.decode("utf-8"))
    draft_value["objective"] = "must remain outside the answer body"
    item.draft_root.write_text(
        json.dumps(draft_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="changed answer outside authorized scope"):
        analyst.replace_analytical_relationships((corrected,))
    item.draft_root.write_bytes(draft_before)

    # A source-selection artifact is not an answer-body field or a permitted
    # calculation/presentation dependency, so it must also fail closed.
    source_map = item.work_root / "source_map.json"
    source_map_before = source_map.read_bytes() if source_map.exists() else None
    source_id = analyst.search_sources("orders", limit=1)[0].source_id
    analyst.select_sources((source_id,), purpose="unrelated source drift")
    with pytest.raises(ValueError, match="outside authorized scope"):
        analyst.replace_analytical_relationships((corrected,))
    if source_map_before is None:
        source_map.unlink()
    else:
        source_map.write_bytes(source_map_before)


def _evidence_note_for_replacement(evidence_id: str, *, evidence_ref: str) -> EvidenceNote:
    return EvidenceNote(
        evidence_id=evidence_id,
        conclusion=f"Conclusion for {evidence_id}.",
        method="Inspect the bounded evidence record.",
        evidence_refs=(evidence_ref,),
        limitations=("Synthetic fixture only",),
        facts={"evidence_id": evidence_id},
    )


def _begin_evidence_only_repair(analyst: AnalystWorkspace, *, finding_id: str = "BR-EVIDENCE-REPLACE") -> None:
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial evidence answer.",
            evidence_refs=("work/evidence.jsonl#old",),
        )
    )
    analyst.review.record(
        "repair_once",
        reviewer_ref="evidence-reviewer",
        findings=(
            ReviewFinding(
                finding_id=finding_id,
                target_sections=("answer",),
                semantic_categories=("evidence",),
                problem="The evidence rows are stale.",
                evidence="The current evidence artifact contains obsolete rows.",
                required_change="Replace the complete owner-local evidence artifact.",
            ),
        ),
    )
    analyst.review.begin_repair()


def _begin_broad_evidence_repair(
    analyst: AnalystWorkspace,
    *,
    finding_id: str = "BR-BROAD-EVIDENCE-REPLACE",
    include_evidence: bool = True,
) -> None:
    categories = (
        ("answer", "calculation")
        + (("evidence",) if include_evidence else ())
        + ("method", "source_completeness", "presentation")
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial broad answer.",
            method="Initial method.",
            visuals=({"title": "Initial visual"},),
            evidence_refs=("work/evidence.jsonl#old",),
        )
    )
    analyst.review.record(
        "repair_once",
        reviewer_ref="broad-reviewer",
        findings=(
            ReviewFinding(
                finding_id=finding_id,
                target_sections=("answer", "method", "visuals", "evidence_refs"),
                semantic_categories=categories,
                problem="The broad answer packet contains stale evidence.",
                evidence="The evidence rows and dependent narrative require one bounded correction.",
                required_change="Replace the corrected evidence while preserving the broad repair scope.",
            ),
        ),
    )
    analyst.review.begin_repair()


def _begin_narrow_evidence_presentation_repair(
    analyst: AnalystWorkspace,
    *,
    finding_id: str = "BR-A3-EVIDENCE-REPLACE",
) -> None:
    """Start the live-shaped evidence+presentation repair scope."""

    analyst.submit_answer(
        AnalystAnswer(
            answer="Initial narrow answer.",
            visuals=({"title": "Initial narrow visual"},),
            evidence_refs=("work/evidence.jsonl#old",),
        )
    )
    analyst.review.record(
        "repair_once",
        reviewer_ref="narrow-reviewer",
        findings=(
            ReviewFinding(
                finding_id=finding_id,
                target_sections=("answer", "visuals", "evidence_refs"),
                semantic_categories=("evidence", "presentation"),
                problem="The evidence rows and visual are stale.",
                evidence="The current evidence artifact contains obsolete rows.",
                required_change="Replace the evidence and refresh its dependent presentation.",
            ),
        ),
    )
    analyst.review.begin_repair()


def obsolete_replace_evidence_notes_full_replacement_and_exact_retry(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    for index in range(9):
        analyst.record_evidence(
            _evidence_note_for_replacement(
                f"E-OLD-{index}",
                evidence_ref="orders.csv",
            )
        )
    _begin_evidence_only_repair(analyst)
    corrected = tuple(
        _evidence_note_for_replacement(
            f"E-CORRECTED-{index}",
            evidence_ref=f"work/evidence.jsonl#E-CORRECTED-{index}",
        )
        for index in range(5)
    )

    replaced = analyst.replace_evidence_notes(corrected)
    assert replaced == corrected
    artifact = item.work_root / "evidence.jsonl"
    records = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [record["evidence_id"] for record in records] == [value.evidence_id for value in corrected]
    content = artifact.read_bytes()
    mtime = artifact.stat().st_mtime_ns
    packet_bytes = item.business_review_path.read_bytes()
    packet = json.loads(packet_bytes)
    assert packet["after_artifact_hashes"]["work/evidence.jsonl"] == hashlib.sha256(content).hexdigest()

    assert analyst.replace_evidence_notes(corrected) == corrected
    assert artifact.read_bytes() == content
    assert artifact.stat().st_mtime_ns == mtime
    assert item.business_review_path.read_bytes() == packet_bytes


def obsolete_replace_evidence_notes_accepts_broad_repair_with_explicit_evidence_scope(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    for index in range(9):
        analyst.record_evidence(
            _evidence_note_for_replacement(f"E-BROAD-OLD-{index}", evidence_ref="orders.csv")
        )
    _begin_broad_evidence_repair(analyst)
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert set(packet["findings"][0]["semantic_categories"]) == {
        "answer",
        "calculation",
        "evidence",
        "method",
        "source_completeness",
        "presentation",
    }
    assert "work/evidence.jsonl" in packet["allowed_dependencies"]
    corrected = tuple(
        _evidence_note_for_replacement(
            f"E-BROAD-CORRECTED-{index}",
            evidence_ref=f"work/evidence.jsonl#E-BROAD-CORRECTED-{index}",
        )
        for index in range(5)
    )
    assert analyst.replace_evidence_notes(corrected) == corrected
    records = [
        json.loads(line)
        for line in (item.work_root / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["evidence_id"] for record in records] == [value.evidence_id for value in corrected]


def _a3_evidence_replacement_fixture(
    tmp_path: Path,
    *,
    narrow: bool = False,
) -> tuple[AnalystWorkspace, ItemWorkspace, tuple[EvidenceNote, ...]]:
    analyst, item = _workspace(tmp_path)
    for index in range(9):
        analyst.record_evidence(
            _evidence_note_for_replacement(f"E-A3-OLD-{index}", evidence_ref="orders.csv")
        )
    item.write_handoff({"previous": "handoff"})
    if narrow:
        _begin_narrow_evidence_presentation_repair(analyst)
    else:
        _begin_broad_evidence_repair(analyst, finding_id="BR-A3-EVIDENCE-REPLACE")
    attempt = item.begin_attempt(analyst.owner_ref, "Analytical Owner", route="requirement")
    handoff = _repair_handoff_payload(
        item,
        analyst,
        attempt.attempt_id,
        repair_finding_id="BR-A3-EVIDENCE-REPLACE",
    )
    handoff["evidence_refs"] = [
        ref for ref in handoff["evidence_refs"] if ref != "work/analytical_relationships.jsonl"
    ]
    item.write_handoff(handoff)
    item.finish_attempt(attempt.attempt_id, status="completed")
    corrected = tuple(
        _evidence_note_for_replacement(
            f"E-A3-CORRECTED-{index}",
            evidence_ref=f"work/evidence.jsonl#E-A3-CORRECTED-{index}",
        )
        for index in range(5)
    )

    # Model the first public replacement having reconciled only the evidence
    # artifact hash.  The completed attempt receipts remain absent from the
    # packet map until that replacement commits its full current progress;
    # the subsequent exact retry must then accept those authorized additions.
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    packet["after_artifact_hashes"]["work/evidence.jsonl"] = hashlib.sha256(
        (item.work_root / "evidence.jsonl").read_bytes()
    ).hexdigest()
    item._write_business_review(packet, touch_state=False, emit=False)
    return analyst, item, corrected


def _a4_failed_a5_baseline_fixture(
    tmp_path: Path,
    *,
    narrow: bool = False,
) -> tuple[AnalystWorkspace, ItemWorkspace, tuple[EvidenceNote, ...]]:
    """Add an honest failed attempt whose receipts seed the completed A-005 baseline."""

    analyst, item, corrected = _a3_evidence_replacement_fixture(tmp_path, narrow=narrow)
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    packet["after_artifact_hashes"]["work/handoff.json"] = hashlib.sha256(
        (item.work_root / "handoff.json").read_bytes()
    ).hexdigest()
    item._write_business_review(packet, touch_state=False, emit=False)
    failed = item.begin_attempt(analyst.owner_ref, "Analytical Owner", route="requirement")
    failed_stdout = item.work_root / ".analysis-run" / "receipt-a4.stdout"
    failed_stderr = item.work_root / ".analysis-run" / "receipt-a4.stderr"
    failed_script_receipt = item.work_root / "script_receipts" / "receipt-a4.json"
    failed_stdout.parent.mkdir(parents=True, exist_ok=True)
    failed_script_receipt.parent.mkdir(parents=True, exist_ok=True)
    failed_stdout.write_bytes(b"A-004 failed stdout")
    failed_stderr.write_bytes(b"A-004 failed stderr")
    context_manifest_path = item.work_root / "analysis_context.json"
    context_manifest = json.loads(context_manifest_path.read_text(encoding="utf-8"))
    script_path = item.work_root / "calculations" / "recompute.py"
    receipt = {
        "context_hash": context_manifest["manifest_hash"],
        "context_path": str(context_manifest_path),
        "error_category": "script_failure",
        "error_type": "ScriptError",
        "exit_code": 1,
        "finished_at": "2026-08-16T00:00:01Z",
        "output_hashes": {},
        "output_limited": False,
        "phase": "full",
        "receipt_id": "receipt-a4",
        "receipt_path": str(failed_script_receipt),
        "script_hash": hashlib.sha256(script_path.read_bytes()).hexdigest(),
        "script_path": str(script_path),
        "source_hash": context_manifest["source_identity"]["content_hash"],
        "started_at": "2026-08-16T00:00:00Z",
        "stderr": "A-004 failed stderr",
        "stderr_truncated": False,
        "stdout": "A-004 failed stdout",
        "stdout_truncated": False,
        "timed_out": False,
        "traceback": "synthetic A-004 failure",
        "wall_seconds": 1.0,
    }
    failed_script_receipt.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    item.finish_attempt(failed.attempt_id, status="failed", error="synthetic A-004 failure")

    completed = item.begin_attempt(analyst.owner_ref, "Analytical Owner", route="requirement")
    handoff = _repair_handoff_payload(
        item,
        analyst,
        completed.attempt_id,
        repair_finding_id="BR-A3-EVIDENCE-REPLACE",
        evidence_id="E-HANDOFF-A5",
    )
    handoff["evidence_refs"] = [
        ref
        for ref in handoff["evidence_refs"]
        if ref not in {
            "work/analytical_relationships.jsonl",
            "work/script_receipts/receipt-a4.json",
        }
    ]
    handoff["receipt_hashes"] = {
        ref: digest
        for ref, digest in handoff["receipt_hashes"].items()
        if ref != "work/script_receipts/receipt-a4.json"
    }
    item.write_handoff(handoff)
    item.finish_attempt(completed.attempt_id, status="completed")

    # The packet still carries the pre-attempt evidence hash until the first
    # public replacement reconciles current progress.
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    packet["after_artifact_hashes"]["work/evidence.jsonl"] = hashlib.sha256(
        (item.work_root / "evidence.jsonl").read_bytes()
    ).hexdigest()
    item._write_business_review(packet, touch_state=False, emit=False)
    return analyst, item, corrected


def _force_stale_a5_execution_packet(item: ItemWorkspace) -> None:
    """Leave current A5 execution bytes at its baseline while packet maps lag."""

    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    for ref in (
        "work/calculations/recompute.py",
        "work/results/result.json",
    ):
        packet["after_artifact_hashes"][ref] = "0" * 64
    item._write_business_review(packet, touch_state=False, emit=False)


def _force_handoff_drift_after_evidence_replacement(item: ItemWorkspace) -> None:
    """Model a post-replacement handoff write while packet maps stay stale."""

    state = json.loads((item.item_root / "item_state.json").read_text(encoding="utf-8"))
    baseline_hash = next(
        attempt["baseline"]["hashes"]["work/handoff.json"]
        for attempt in reversed(state["attempts"])
        if attempt.get("status") == "completed" and "work/handoff.json" in attempt["baseline"]["hashes"]
    )
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    packet["after_artifact_hashes"]["work/handoff.json"] = baseline_hash
    item._write_business_review(packet, touch_state=False, emit=False)
    handoff = json.loads((item.work_root / "handoff.json").read_text(encoding="utf-8"))
    handoff["freeze_note"] = handoff.get("freeze_note", "") + " post-replacement handoff update"
    item.write_handoff(handoff)


def obsolete_replace_evidence_notes_exact_retry_accepts_authorized_completed_attempt_receipts(
    tmp_path: Path,
) -> None:
    analyst, item, corrected = _a3_evidence_replacement_fixture(tmp_path)

    assert analyst.replace_evidence_notes(corrected) == corrected
    _force_handoff_drift_after_evidence_replacement(item)
    artifact = item.work_root / "evidence.jsonl"
    content = artifact.read_bytes()
    mtime = artifact.stat().st_mtime_ns
    packet_bytes = item.business_review_path.read_bytes()
    assert analyst.replace_evidence_notes(corrected) == corrected
    assert artifact.read_bytes() == content
    assert artifact.stat().st_mtime_ns == mtime
    assert item.business_review_path.read_bytes() != packet_bytes
    packet_after_retry = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert packet_after_retry["after_artifact_hashes"]["work/handoff.json"] == hashlib.sha256(
        (item.work_root / "handoff.json").read_bytes()
    ).hexdigest()


def obsolete_replace_evidence_notes_accepts_unchanged_failed_attempt_receipts_in_latest_baseline(
    tmp_path: Path,
) -> None:
    analyst, item, corrected = _a4_failed_a5_baseline_fixture(tmp_path)

    assert analyst.replace_evidence_notes(corrected) == corrected
    artifact = item.work_root / "evidence.jsonl"
    content = artifact.read_bytes()
    mtime = artifact.stat().st_mtime_ns
    assert analyst.replace_evidence_notes(corrected) == corrected
    assert artifact.read_bytes() == content
    assert artifact.stat().st_mtime_ns == mtime


def obsolete_replace_evidence_notes_narrow_scope_accepts_a4_a5_exact_retry(
    tmp_path: Path,
) -> None:
    """The live-shaped evidence+presentation scope admits the prior family."""

    analyst, item, corrected = _a4_failed_a5_baseline_fixture(tmp_path, narrow=True)
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    finding = packet["findings"][0]
    assert set(finding["semantic_categories"]) == {"evidence", "presentation"}
    assert set(packet["allowed_artifact_paths"]) == {"work/results"}
    assert set(packet["allowed_dependencies"]) == {
        "work/evidence.jsonl",
        "work/source_map.json",
        "work/specialist_memos.jsonl",
    }

    assert analyst.replace_evidence_notes(corrected) == corrected
    artifact = item.work_root / "evidence.jsonl"
    content = artifact.read_bytes()
    mtime = artifact.stat().st_mtime_ns
    assert analyst.replace_evidence_notes(corrected) == corrected
    assert artifact.read_bytes() == content
    assert artifact.stat().st_mtime_ns == mtime


def obsolete_replace_evidence_notes_narrow_scope_rejects_unvalidated_extra_receipt(
    tmp_path: Path,
) -> None:
    analyst, item, corrected = _a4_failed_a5_baseline_fixture(tmp_path, narrow=True)
    extra = item.work_root / ".analysis-run" / "unexpected.stdout"
    extra.write_bytes(b"foreign receipt family member")
    artifact = item.work_root / "evidence.jsonl"
    prior_artifact = artifact.read_bytes()
    prior_packet = item.business_review_path.read_bytes()

    with pytest.raises(ValueError, match="outside authorized scope"):
        analyst.replace_evidence_notes(corrected)
    assert artifact.read_bytes() == prior_artifact
    assert item.business_review_path.read_bytes() == prior_packet


def obsolete_replace_evidence_notes_accepts_stale_packet_for_current_handoff_execution(
    tmp_path: Path,
) -> None:
    """A5's exact script/results refs may bridge a stale packet map."""

    analyst, item, corrected = _a4_failed_a5_baseline_fixture(tmp_path)
    _force_stale_a5_execution_packet(item)

    assert analyst.replace_evidence_notes(corrected) == corrected
    artifact = item.work_root / "evidence.jsonl"
    content = artifact.read_bytes()
    mtime = artifact.stat().st_mtime_ns
    assert analyst.replace_evidence_notes(corrected) == corrected
    assert artifact.read_bytes() == content
    assert artifact.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("tamper", ("undeclared", "changed"))
def obsolete_replace_evidence_notes_rejects_stale_packet_for_unauthorized_execution(
    tmp_path: Path,
    tamper: str,
) -> None:
    analyst, item, corrected = _a4_failed_a5_baseline_fixture(tmp_path)
    _force_stale_a5_execution_packet(item)
    if tamper == "undeclared":
        foreign = item.work_root / "results" / "foreign.json"
        foreign.write_bytes(b"foreign result")
        foreign_ref = "work/results/foreign.json"
        state = json.loads((item.item_root / "item_state.json").read_text(encoding="utf-8"))
        latest_baseline = state["attempts"][-1]["baseline"]
        latest_baseline["files"].append(foreign_ref)
        latest_baseline["files"].sort()
        latest_baseline["hashes"][foreign_ref] = hashlib.sha256(foreign.read_bytes()).hexdigest()
        item._persist_state(state, touch=False)
        item._state = state
        packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
        packet["after_artifact_hashes"][foreign_ref] = "0" * 64
        item._write_business_review(packet, touch_state=False, emit=False)
    else:
        result = item.work_root / "results" / "result.json"
        result.write_bytes(b"tampered A5 result")

    artifact = item.work_root / "evidence.jsonl"
    prior_artifact = artifact.read_bytes()
    prior_packet = item.business_review_path.read_bytes()
    with pytest.raises(ValueError, match="handoff|attempt is not bound|outside authorized scope"):
        analyst.replace_evidence_notes(corrected)
    assert artifact.read_bytes() == prior_artifact
    assert item.business_review_path.read_bytes() == prior_packet


@pytest.mark.parametrize("tamper", ("missing", "changed", "outside"))
def obsolete_replace_evidence_notes_rejects_tampered_or_unrelated_baseline_receipts(
    tmp_path: Path,
    tamper: str,
) -> None:
    analyst, item, corrected = _a4_failed_a5_baseline_fixture(tmp_path)
    receipt = item.work_root / ".analysis-run" / "receipt-a4.stdout"
    if tamper == "missing":
        receipt.unlink()
    elif tamper == "changed":
        receipt.write_bytes(b"tampered A-004 stdout")
    else:
        unrelated = item.work_root / "unrelated-receipt.stdout"
        unrelated.write_bytes(b"foreign receipt")
        packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
        packet["after_artifact_hashes"]["work/unrelated-receipt.stdout"] = hashlib.sha256(
            unrelated.read_bytes()
        ).hexdigest()
        item._write_business_review(packet, touch_state=False, emit=False)

    artifact = item.work_root / "evidence.jsonl"
    prior_artifact = artifact.read_bytes()
    prior_packet = item.business_review_path.read_bytes()
    with pytest.raises(ValueError, match="handoff|attempt is not bound|outside authorized scope"):
        analyst.replace_evidence_notes(corrected)
    assert artifact.read_bytes() == prior_artifact
    assert item.business_review_path.read_bytes() == prior_packet


@pytest.mark.parametrize(
    "tamper",
    ("unbound", "other_item", "other_owner", "other_attempt", "nonterminal", "extra"),
)
def obsolete_replace_evidence_notes_rejects_unbound_prior_execution_history(
    tmp_path: Path,
    tamper: str,
) -> None:
    analyst, item, corrected = _a4_failed_a5_baseline_fixture(tmp_path)
    receipt = item.work_root / "script_receipts" / "receipt-a4.json"
    if tamper in {"unbound", "other_item"}:
        value = json.loads(receipt.read_text(encoding="utf-8"))
        if tamper == "unbound":
            value["receipt_id"] = "receipt-foreign"
        else:
            value["script_path"] = str(tmp_path / "other-item" / "work" / "calculations" / "recompute.py")
        receipt.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    elif tamper in {"other_owner", "other_attempt", "nonterminal"}:
        state = json.loads((item.item_root / "item_state.json").read_text(encoding="utf-8"))
        prior = state["attempts"][-2]
        if tamper == "other_owner":
            prior["lane_id"] = "foreign-owner"
        elif tamper == "other_attempt":
            prior["route"] = "question"
        else:
            prior["status"] = "active"
            state["active_attempt_id"] = prior["attempt_id"]
        item._persist_state(state, touch=False)
        item._state = state
    else:
        extra = item.work_root / ".analysis-run" / "receipt-foreign.stdout"
        extra.write_bytes(b"foreign receipt family member")

    artifact = item.work_root / "evidence.jsonl"
    prior_artifact = artifact.read_bytes()
    prior_packet = item.business_review_path.read_bytes()
    with pytest.raises(ValueError, match="handoff|attempt is not bound|outside authorized scope|receipt|active attempt"):
        analyst.replace_evidence_notes(corrected)
    assert artifact.read_bytes() == prior_artifact
    assert item.business_review_path.read_bytes() == prior_packet


def obsolete_replace_evidence_notes_rejects_unrelated_or_mismatched_attempt_receipt_delta(
    tmp_path: Path,
) -> None:
    analyst, item, corrected = _a3_evidence_replacement_fixture(tmp_path)
    assert analyst.replace_evidence_notes(corrected) == corrected
    _force_handoff_drift_after_evidence_replacement(item)
    artifact = item.work_root / "evidence.jsonl"
    prior_artifact = artifact.read_bytes()
    prior_packet = item.business_review_path.read_bytes()

    unrelated = item.work_root / "unrelated-receipt.stdout"
    unrelated.write_bytes(b"foreign receipt")
    with pytest.raises(ValueError, match="outside authorized scope"):
        analyst.replace_evidence_notes(corrected)
    assert artifact.read_bytes() == prior_artifact
    assert item.business_review_path.read_bytes() == prior_packet
    unrelated.unlink()

    authorized_delta = item.work_root / ".analysis-run" / "late.stdout"
    authorized_delta.write_bytes(b"late authorized-root receipt")
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    packet["after_artifact_hashes"]["work/.analysis-run/late.stdout"] = "0" * 64
    item._write_business_review(packet, touch_state=False, emit=False)
    with pytest.raises(ValueError, match="attempt is not bound to the repair baseline"):
        analyst.replace_evidence_notes(corrected)
    assert artifact.read_bytes() == prior_artifact


def obsolete_replace_evidence_notes_rejects_broad_scope_without_evidence_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_evidence(_evidence_note_for_replacement("E-BROAD-ORIGINAL", evidence_ref="orders.csv"))
    _begin_broad_evidence_repair(analyst, finding_id="BR-BROAD-NO-EVIDENCE", include_evidence=False)
    corrected = _evidence_note_for_replacement(
        "E-BROAD-CORRECTED",
        evidence_ref="work/evidence.jsonl#E-BROAD-CORRECTED",
    )
    with pytest.raises(ValueError, match="requires an evidence finding"):
        analyst.replace_evidence_notes((corrected,))

    # An evidence category without the evidence dependency is rejected even
    # when a caller presents a self-consistent in-memory packet to the durable
    # boundary.  The persisted packet remains untouched by the failed call.
    missing_path_root = tmp_path / "missing-path"
    missing_path_root.mkdir()
    analyst2, item2 = _workspace(missing_path_root)
    analyst2.record_evidence(_evidence_note_for_replacement("E-MISSING-PATH", evidence_ref="orders.csv"))
    _begin_evidence_only_repair(analyst2, finding_id="BR-EVIDENCE-MISSING-PATH")
    packet = json.loads(item2.business_review_path.read_text(encoding="utf-8"))
    packet["findings"][0]["dependent_outputs"].remove("work/evidence.jsonl")
    packet["allowed_dependencies"].remove("work/evidence.jsonl")
    packet_before = item2.business_review_path.read_bytes()
    monkeypatch.setattr(item2, "_read_business_review", lambda: packet)
    with pytest.raises(ValueError, match="does not authorize evidence refs"):
        analyst2.replace_evidence_notes((corrected,))
    assert item2.business_review_path.read_bytes() == packet_before


def obsolete_replace_evidence_notes_rejects_scope_stale_invalid_and_active_attempt(
    tmp_path: Path,
) -> None:
    analyst, item = _workspace(tmp_path)
    original = _evidence_note_for_replacement("E-ORIGINAL", evidence_ref="orders.csv")
    analyst.record_evidence(original)
    _begin_evidence_only_repair(analyst, finding_id="BR-EVIDENCE-REPLACE-NEGATIVE")
    artifact = item.work_root / "evidence.jsonl"
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    corrected = _evidence_note_for_replacement(
        "E-CORRECTED",
        evidence_ref="work/evidence.jsonl#E-CORRECTED",
    )

    with pytest.raises(ValueError, match="expected_artifact_hash"):
        analyst.replace_evidence_notes((corrected,), expected_artifact_hash="0" * 64)
    with pytest.raises(ValueError, match="canonical evidence note"):
        analyst.replace_evidence_notes(({**corrected.to_dict(), "extra": True},))
    with pytest.raises(ValueError, match="evidence_id values must be unique"):
        analyst.replace_evidence_notes((corrected, corrected))
    with pytest.raises(ValueError, match="owner_ref"):
        item.replace_evidence_notes((corrected.to_dict(),), owner_ref="other-owner")
    assert artifact.read_bytes() == json.dumps(
        original.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"

    artifact.write_bytes(artifact.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed since"):
        analyst.replace_evidence_notes((corrected,), expected_artifact_hash=expected)
    artifact.write_bytes(
        json.dumps(original.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )

    active_root = tmp_path / "active-attempt"
    active_root.mkdir()
    analyst2, item2 = _workspace(active_root)
    analyst2.record_evidence(original)
    _begin_evidence_only_repair(analyst2, finding_id="BR-EVIDENCE-REPLACE-ACTIVE")
    attempt = item2.begin_attempt("lane-evidence", "analytical_owner", route="requirement")
    with pytest.raises(ValueError, match="no active attempt"):
        analyst2.replace_evidence_notes((corrected,))
    item2.finish_attempt(attempt.attempt_id, status="completed")


def obsolete_replace_evidence_notes_rejects_non_evidence_repair_scope(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_evidence(_evidence_note_for_replacement("E-ORIGINAL", evidence_ref="orders.csv"))
    analyst.submit_answer(AnalystAnswer(answer="Initial answer."))
    analyst.review.record(
        "repair_once",
        reviewer_ref="method-reviewer",
        findings=(
            ReviewFinding(
                finding_id="BR-METHOD-NOT-EVIDENCE",
                target_sections=("method",),
                semantic_categories=("method",),
                problem="The method needs correction.",
                evidence="The method was not reproducible.",
                required_change="Re-run the method.",
            ),
        ),
    )
    analyst.review.begin_repair()
    with pytest.raises(ValueError, match="requires an evidence finding"):
        analyst.replace_evidence_notes(
            (_evidence_note_for_replacement("E-CORRECTED", evidence_ref="work/evidence.jsonl#E-CORRECTED"),)
        )


def obsolete_replace_evidence_notes_rolls_back_and_rejects_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    analyst, item = _workspace(tmp_path)
    original = _evidence_note_for_replacement("E-ORIGINAL", evidence_ref="orders.csv")
    analyst.record_evidence(original)
    _begin_evidence_only_repair(analyst, finding_id="BR-EVIDENCE-REPLACE-ROLLBACK")
    corrected = _evidence_note_for_replacement(
        "E-CORRECTED",
        evidence_ref="work/evidence.jsonl#E-CORRECTED",
    )
    artifact = item.work_root / "evidence.jsonl"
    prior_artifact = artifact.read_bytes()
    prior_packet = item.business_review_path.read_bytes()
    prior_state = (item.item_root / "item_state.json").read_bytes()

    def fail_packet(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected packet failure")

    original_write = item._write_business_review
    monkeypatch.setattr(item, "_write_business_review", fail_packet)
    with pytest.raises(OSError, match="injected packet failure"):
        analyst.replace_evidence_notes((corrected,))
    assert artifact.read_bytes() == prior_artifact
    assert item.business_review_path.read_bytes() == prior_packet
    assert (item.item_root / "item_state.json").read_bytes() == prior_state
    monkeypatch.setattr(item, "_write_business_review", original_write)

    outside = tmp_path / "outside-evidence.jsonl"
    outside.write_bytes(prior_artifact)
    artifact.unlink()
    artifact.symlink_to(outside)
    with pytest.raises(Exception, match="symlink"):
        analyst.replace_evidence_notes((corrected,))


def obsolete_active_repair_progress_reconciles_public_writes_and_rejects_out_of_scope(
    tmp_path: Path,
) -> None:
    analyst, item = _workspace(tmp_path)
    original = analyst.record_analytical_relationship(_repair_relationship("rel-1"))
    _begin_relationship_repair(analyst, finding_id="BR-RELATIONSHIPS-PROGRESS")
    analyst.begin_analysis(
        objective="Recompute relationship evidence.",
        strategy="Use the bounded fixture and preserve the reviewed narrative.",
    )
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-PROGRESS",
            conclusion="The relationship population is bounded.",
            method="Count supplied fixture rows.",
            evidence_refs=("orders.csv",),
        )
    )
    analyst.submit_answer("Corrected relationship narrative.")
    corrected = _repair_relationship("rel-1", evidence_ref="work/evidence.jsonl#corrected")
    replaced = analyst.replace_analytical_relationships((corrected,))
    assert replaced == (corrected,)
    packet = json.loads((item.work_root / "business_review.json").read_text(encoding="utf-8"))
    progress = item.artifact_progress()
    assert packet["after_artifact_hashes"] == dict(progress.hashes)

    unauthorized = item.work_root / "unauthorized-progress.bin"
    unauthorized.write_bytes(b"outside the reviewer-derived scope")
    packet_bytes = (item.work_root / "business_review.json").read_bytes()
    with pytest.raises(ValueError, match="outside authorized scope"):
        analyst.replace_analytical_relationships((corrected,))
    assert (item.work_root / "analytical_relationships.jsonl").read_bytes() == _jsonl_bytes(
        (corrected,), item_id=item.item_id, owner_ref=analyst.owner_ref
    )
    assert (item.work_root / "business_review.json").read_bytes() == packet_bytes


def _repair_handoff_payload(
    item: ItemWorkspace,
    analyst: AnalystWorkspace,
    attempt_id: str,
    *,
    item_id: str | None = None,
    owner_ref: str | None = None,
    attempt_override: str | None = None,
    repair_finding_id: str = "BR-RELATIONSHIPS",
    evidence_id: str = "E-HANDOFF",
) -> dict[str, Any]:
    """Build the current public analyst-handoff contract from durable bytes."""

    script = item.work_root / "calculations" / "recompute.py"
    result = item.work_root / "results" / "result.json"
    script.parent.mkdir(parents=True, exist_ok=True)
    result.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "from pathlib import Path\n"
        "Path('results').mkdir(parents=True, exist_ok=True)\n"
        "Path('results/result.json').write_text('{\"status\":\"recomputed\"}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    report = analyst.run_analysis(
        script,
        outputs=(result,),
        deterministic_outputs=(result,),
    )
    assert report.succeeded
    analyst.record_evidence(
        EvidenceNote(
            evidence_id=evidence_id,
            conclusion="The deterministic repair output is present.",
            method="Run the bounded repair calculation.",
            evidence_refs=("work/results/result.json",),
        )
    )
    script_ref = "work/calculations/recompute.py"
    result_ref = "work/results/result.json"
    receipt_refs = tuple(
        sorted(
            path.relative_to(item.item_root).as_posix()
            for path in (item.work_root / "script_receipts").glob("*.json")
        )
    )
    evidence_refs = (
        "work/analytical_relationships.jsonl",
        "work/evidence.jsonl",
        result_ref,
        *receipt_refs,
    )
    return {
        "analysis_output_summary": {"status": "recomputed", "receipt_count": len(receipt_refs)},
        "analysis_status": "completed",
        "attempt_id": attempt_override or attempt_id,
        "business_repair_count": 1,
        "calculation_outputs": [result_ref],
        "calculation_script": script_ref,
        "evidence_refs": list(evidence_refs),
        "freeze_note": "Corrected owner packet is ready for targeted re-review.",
        "item_id": item_id or item.item_id,
        "limits": ["Synthetic fixture only."],
        "output_hashes": {
            script_ref: hashlib.sha256(script.read_bytes()).hexdigest(),
            result_ref: hashlib.sha256(result.read_bytes()).hexdigest(),
        },
        "owner_ref": owner_ref or analyst.owner_ref,
        "receipt_hashes": {
            ref: hashlib.sha256((item.item_root / ref).read_bytes()).hexdigest()
            for ref in receipt_refs
        },
        "repair_finding_id": repair_finding_id,
        "requirement": item.original_text,
        "review_status": "pending_targeted_business_recheck",
        "schema_version": "auto_foundry.analyst_handoff.v1",
    }


def obsolete_replace_relationships_accepts_completed_current_repair_handoff(
    tmp_path: Path,
) -> None:
    analyst, item = _workspace(tmp_path)
    original = analyst.record_analytical_relationship(_repair_relationship("rel-handoff"))
    _begin_relationship_repair(analyst)
    attempt = item.begin_attempt(analyst.owner_ref, "Analytical Owner", route="requirement")
    handoff = _repair_handoff_payload(item, analyst, attempt.attempt_id)
    item.write_handoff(handoff)
    item.finish_attempt(attempt.attempt_id, status="completed")

    corrected = _repair_relationship("rel-handoff", evidence_ref="work/results/result.json")
    assert analyst.replace_analytical_relationships((corrected,)) == (corrected,)
    persisted = item.read_analytical_relationships()
    assert len(persisted) == 1
    assert persisted[0]["relationship_id"] == corrected.relationship_id
    assert persisted[0]["evidence_refs"] == ["work/results/result.json"]
    packet = json.loads(item.business_review_path.read_text(encoding="utf-8"))
    assert packet["after_artifact_hashes"]["work/handoff.json"] == hashlib.sha256(
        (item.work_root / "handoff.json").read_bytes()
    ).hexdigest()
    assert packet["after_artifact_hashes"]["work/analytical_relationships.jsonl"] == hashlib.sha256(
        (item.work_root / "analytical_relationships.jsonl").read_bytes()
    ).hexdigest()
    assert original.relationship_id == corrected.relationship_id


@pytest.mark.parametrize(
    "tamper",
    ("item", "owner", "attempt", "output_hash", "noncanonical", "failed_attempt"),
)
def obsolete_replace_relationships_rejects_invalid_completed_repair_handoff(
    tmp_path: Path,
    tamper: str,
) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_analytical_relationship(_repair_relationship("rel-handoff-negative"))
    _begin_relationship_repair(analyst)
    attempt = item.begin_attempt(analyst.owner_ref, "Analytical Owner", route="requirement")
    handoff = _repair_handoff_payload(item, analyst, attempt.attempt_id)
    if tamper == "item":
        handoff["item_id"] = "REQ-OTHER"
    elif tamper == "owner":
        handoff["owner_ref"] = "other-owner"
    elif tamper == "attempt":
        handoff["attempt_id"] = "A-999"
    elif tamper == "output_hash":
        handoff["output_hashes"]["work/results/result.json"] = "0" * 64
    item.write_handoff(handoff)
    if tamper == "noncanonical":
        item.work_root.joinpath("handoff.json").write_text(
            json.dumps(handoff, indent=2) + "\n",
            encoding="utf-8",
        )
    item.finish_attempt(
        attempt.attempt_id,
        status="failed" if tamper == "failed_attempt" else "completed",
    )
    corrected = _repair_relationship("rel-handoff-negative", evidence_ref="work/results/result.json")
    with pytest.raises(ValueError, match="handoff"):
        analyst.replace_analytical_relationships((corrected,))


def obsolete_replace_relationships_rejects_untyped_handoff_drift(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_analytical_relationship(_repair_relationship("rel-handoff-untyped"))
    _begin_relationship_repair(analyst)
    item.write_handoff({"next": "resume from a generic handoff"})

    corrected = _repair_relationship("rel-handoff-untyped", evidence_ref="work/evidence.jsonl#corrected")
    with pytest.raises(ValueError, match="handoff"):
        analyst.replace_analytical_relationships((corrected,))


def obsolete_replace_relationships_rejects_completed_lead_route_handoff(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_analytical_relationship(_repair_relationship("rel-handoff-lead"))
    _begin_relationship_repair(analyst)
    attempt = item.begin_attempt(analyst.owner_ref, "Analytical Owner", route="lead")
    item.write_handoff(_repair_handoff_payload(item, analyst, attempt.attempt_id))
    item.finish_attempt(attempt.attempt_id, status="completed")

    corrected = _repair_relationship("rel-handoff-lead", evidence_ref="work/results/result.json")
    with pytest.raises(ValueError, match="route"):
        analyst.replace_analytical_relationships((corrected,))


def obsolete_replace_relationships_rejects_completed_recovery_route_handoff(tmp_path: Path) -> None:
    analyst, item = _workspace(tmp_path)
    analyst.record_analytical_relationship(_repair_relationship("rel-handoff-recovery"))
    _begin_relationship_repair(analyst)
    prior = item.begin_attempt(analyst.owner_ref, "Analytical Owner", route="requirement")
    receipt = AgentInvocationReceipt(
        "I-HANDOFF-RECOVERY",
        item.item_id,
        prior.attempt_id,
        prior.lane_id,
        prior.role,
        prior.route,
        finish="2026-08-16T00:00:01Z",
        terminal_reason="process_lost",
    )
    receipt_ref = InvocationReceiptLedger(item.context).append(receipt)
    recovery = item.begin_recovery(
        analyst.owner_ref,
        "Analytical Owner",
        prior_attempt_id=prior.attempt_id,
        receipt_ref=receipt_ref,
    )
    item.write_handoff(_repair_handoff_payload(item, analyst, recovery.attempt_id))
    item.finish_attempt(recovery.attempt_id, status="completed")

    corrected = _repair_relationship("rel-handoff-recovery", evidence_ref="work/results/result.json")
    with pytest.raises(ValueError, match="route"):
        analyst.replace_analytical_relationships((corrected,))


def _jsonl_bytes(
    rows: Iterable[AnalyticalRelationshipEvidence],
    *,
    item_id: str,
    owner_ref: str,
) -> bytes:
    return b"".join(
        (
            json.dumps(
                {**row.to_dict(), "item_id": item_id, "owner_ref": owner_ref},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


def test_relationship_cardinality_uses_edges_and_distinct_endpoint_coverage(tmp_path: Path) -> None:
    """One-to-many counts do not pretend that edge count is a population."""

    ecom = AnalyticalRelationshipEvidence(
        relationship_id="orders-items",
        source_id="orders",
        target_id="items",
        cardinality="one_to_many",
        join_keys=({"source_field": "order_id", "target_field": "order_id"},),
        matched_pairs=7668,
        source_population=3584,
        target_population=7668,
        matched_source_count=3584,
        matched_target_count=7668,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/evidence.jsonl#orders-items",),
        publishable=True,
    )
    assert ecom.to_dict()["matched_pairs"] == 7668
    assert "coverage" not in ecom.to_dict()

    inverse = AnalyticalRelationshipEvidence(
        relationship_id="items-orders",
        source_id="items",
        target_id="orders",
        cardinality="many_to_one",
        join_keys=({"source_field": "order_id", "target_field": "order_id"},),
        matched_pairs=7668,
        source_population=7668,
        target_population=3584,
        matched_source_count=7668,
        matched_target_count=3584,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/evidence.jsonl#items-orders",),
        publishable=True,
    )
    assert inverse.matched_source_count == 7668

    billing = AnalyticalRelationshipEvidence(
        relationship_id="billing-partial",
        source_id="billing-lines",
        target_id="accounts",
        cardinality="many_to_one",
        join_keys=({"source_field": "account_id", "target_field": "account_id"},),
        matched_pairs=4111,
        source_population=4119,
        target_population=5000,
        matched_source_count=4111,
        matched_target_count=3000,
        source_coverage=4111 / 4119,
        target_coverage=0.6,
        date_authority="fixture-controlled snapshot",
        limitations=("Partial linkage",),
        evidence_refs=("work/evidence.jsonl#billing",),
        publishable=True,
    )
    assert billing.source_coverage == 4111 / 4119

    one_to_one = AnalyticalRelationshipEvidence(
        relationship_id="customer-profile",
        source_id="customers",
        target_id="profiles",
        cardinality="one_to_one",
        join_keys=({"source_field": "customer_id", "target_field": "customer_id"},),
        matched_pairs=2,
        source_population=2,
        target_population=3,
        matched_source_count=2,
        matched_target_count=2,
        source_coverage=1.0,
        target_coverage=2 / 3,
        date_authority="fixture-controlled snapshot",
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/evidence.jsonl#customer-profile",),
        publishable=True,
    )
    assert one_to_one.matched_pairs == one_to_one.matched_source_count == one_to_one.matched_target_count

    many_to_many = AnalyticalRelationshipEvidence(
        relationship_id="many-to-many-positive",
        source_id="source-groups",
        target_id="target-groups",
        cardinality="many_to_many",
        join_keys=({"source_field": "group_id", "target_field": "group_id"},),
        matched_pairs=3,
        source_population=2,
        target_population=2,
        matched_source_count=2,
        matched_target_count=2,
        source_coverage=1.0,
        target_coverage=1.0,
        date_authority="fixture-controlled snapshot",
        limitations=("Synthetic fixture only",),
        evidence_refs=("work/evidence.jsonl#many-to-many",),
        publishable=True,
    )
    assert many_to_many.matched_pairs == 3
    with pytest.raises(ValueError, match="Cartesian bound"):
        AnalyticalRelationshipEvidence(
            relationship_id="many-to-many-too-many-edges",
            source_id="source-groups",
            target_id="target-groups",
            cardinality="many_to_many",
            join_keys=({"source_field": "group_id", "target_field": "group_id"},),
            matched_pairs=2,
            source_population=1,
            target_population=1,
            matched_source_count=1,
            matched_target_count=1,
            source_coverage=1.0,
            target_coverage=1.0,
            date_authority="fixture-controlled snapshot",
            limitations=("Synthetic fixture only",),
            evidence_refs=("work/evidence.jsonl#many-to-many-invalid",),
            publishable=True,
        )

    invalid = [
        {"matched_pairs": 3, "matched_source_count": 2, "matched_target_count": 2, "source_coverage": 1.0, "target_coverage": 1.0},
        {"matched_pairs": 2, "matched_source_count": 2, "matched_target_count": 1, "source_coverage": 0.5, "target_coverage": 1.0},
        {"matched_pairs": 0, "matched_source_count": 1, "matched_target_count": 0, "source_coverage": 1.0, "target_coverage": 0.0},
        {"matched_pairs": 3, "matched_source_count": 3, "matched_target_count": 2, "source_coverage": 1.5, "target_coverage": 1.0},
        {"matched_pairs": 2, "matched_source_count": 2, "matched_target_count": 1, "source_coverage": 1.0, "target_coverage": 1.0, "cardinality": "one_to_one"},
    ]
    for values in invalid:
        with pytest.raises(ValueError):
            AnalyticalRelationshipEvidence(
                relationship_id="invalid",
                source_id="source",
                target_id="target",
                cardinality=values.pop("cardinality", "many_to_one"),
                join_keys=({"source_field": "id", "target_field": "id"},),
                matched_pairs=values["matched_pairs"],
                source_population=2,
                target_population=2,
                matched_source_count=values["matched_source_count"],
                matched_target_count=values["matched_target_count"],
                source_coverage=values["source_coverage"],
                target_coverage=values["target_coverage"],
                date_authority="fixture-controlled snapshot",
                limitations=("Synthetic fixture only",),
                evidence_refs=("work/evidence.jsonl#invalid",),
                publishable=True,
            )

    with pytest.raises(TypeError):
        AnalyticalRelationshipEvidence.from_dict({**ecom.to_dict(), "coverage": 1.0})
