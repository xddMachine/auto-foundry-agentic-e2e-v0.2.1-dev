from __future__ import annotations

import json
from pathlib import Path
import threading
import zipfile

import pytest

from auto_foundry_core import (
    AnalystAnswer,
    AnalystWorkspace,
    BoundAnalysisContext,
    DataAssetRef,
    DataInsufficiencyConclusion,
    DataRoomWorkbench,
    ItemWorkspace,
    ReviewFinding,
    RunContext,
    load_bound_analysis_context,
)
from auto_foundry_core.contracts import ImplementationTransition
from auto_foundry_core.integration import AcceptedAnalysisBundle
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.reporting import project_run_report
from auto_foundry_core.workspace import AllowedRootError


def _analyst(tmp_path: Path, item_id: str = "Q-001") -> tuple[AnalystWorkspace, ItemWorkspace]:
    input_root = tmp_path / "inputs"
    input_root.mkdir(parents=True)
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,amount\nO-1,10\n")
    context = RunContext("RUN-BLOCKED", tmp_path / "run", (input_root,))
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, item_id, original_text="Summarize the bounded fixture.")
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        workbench=workbench,
    )
    analyst = AnalystWorkspace(bound, owner_ref=f"owner-{item_id}")
    analyst.submit_answer(AnalystAnswer(answer="Initial bounded answer.", method="Initial method."))
    return analyst, item


def _finding() -> ReviewFinding:
    return ReviewFinding(
        finding_id="Q001-BR-BLOCK",
        target_sections=("method",),
        semantic_categories=("method",),
        problem="The method cannot support the specific claim.",
        evidence="The supplied fixture has no authoritative control.",
        required_change="Remove the unsupported claim and disclose the block.",
    )


def _prepare_blocked(analyst: AnalystWorkspace) -> None:
    finding = _finding()
    analyst.review.record("repair_once", reviewer_ref="business-reviewer", findings=(finding,))
    analyst.review.begin_repair()
    analyst.submit_answer(AnalystAnswer(answer="Revised bounded answer.", method="Revised method."))
    analyst.conclude_data_insufficiency(
        DataInsufficiencyConclusion(
            reason="The supplied fixture has no authoritative control.",
            direct_answer_component="the unsupported claim",
            missing_data=("authoritative control source",),
            searches_tests=("searched the supplied catalog", "checked the bounded fixture"),
            evidence_refs=("work/business_review.json",),
            supported_components=("bounded fixture observations",),
        )
    )
    analyst.review.record("confirm_data_insufficiency", reviewer_ref="targeted-reviewer")


def _transitioned_source(tmp_path: Path) -> tuple[RunContext, BoundAnalysisContext, RunLifecycle, Path]:
    input_root = tmp_path / "inputs"
    input_root.mkdir(parents=True)
    archive = input_root / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", "order_id,amount\nO-1,10\n")
    run_root = tmp_path / "run"
    old = RunContext("RUN-BLOCKED-INHERIT", run_root, (input_root,), core_version="0.3.1", skill_version="0.2.4")
    source_item = ItemWorkspace.create(old, "Q-001", original_text="Summarize the bounded fixture.")
    source_bound = BoundAnalysisContext.create(old, DataAssetRef.from_path(archive), source_item)
    initial_manifest = json.loads(source_bound.manifest_path.read_text(encoding="utf-8"))
    initial_sha = initial_manifest["implementation_sha"]
    initial_tree = initial_manifest["implementation_tree"]
    lifecycle = RunLifecycle.create(old, ("Q-001", "Q-002"))
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-BLOCKED-1",
            old_sha=initial_sha,
            new_sha="b" * 40,
            old_tree=initial_tree,
            new_tree="d" * 40,
            old_version="skill0.2.4/core0.3.1",
            new_version="skill0.2.5/core0.3.2",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="test transition",
            resume_point="analysis_context_rebind",
        )
    )
    lifecycle.record_implementation_transition(
        ImplementationTransition(
            transition_id="T-BLOCKED-2",
            old_sha="b" * 40,
            new_sha="e" * 40,
            old_tree="d" * 40,
            new_tree="f" * 40,
            old_version="skill0.2.5/core0.3.2",
            new_version="skill0.3.0/core0.4.0",
            earliest_affected_item="Q-001",
            preserved_accepted_hashes={},
            unaffected_reason="test transition",
            resume_point="analysis_context_rebind",
        )
    )
    current = RunContext("RUN-BLOCKED-INHERIT", run_root, (input_root,), core_version="0.4.0", skill_version="0.3.0")
    rebound = load_bound_analysis_context(
        current,
        path=source_bound.manifest_path,
        item_workspace=ItemWorkspace.load(current, "Q-001"),
    )
    return current, rebound, lifecycle, archive


def test_blocked_review_publishes_immutable_no_answer_snapshot_and_reloads(tmp_path: Path) -> None:
    analyst, item = _analyst(tmp_path)
    _prepare_blocked(analyst)

    snapshot = analyst.review.finalize_blocked_by_evidence()
    assert snapshot.outcome == "blocked_by_evidence"
    assert item.state["lifecycle_state"] == "blocked_by_evidence"
    assert item.state["integration_state"] == "pending"
    assert {path.name for path in item.accepted_root.iterdir()} == {
        "manifest.json",
        "reviewed_draft.json",
        "business_review.json",
        "data_insufficiency_conclusion.json",
    }
    assert not (item.accepted_root / "answer_content.json").exists()
    manifest = json.loads((item.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["outcome"] == "blocked_by_evidence"
    assert manifest["review_verdict"] == "confirm_data_insufficiency"
    assert manifest["review_scope"] in {"full", "targeted"}
    assert manifest["targeted_recheck"] is True
    assert manifest["repair_active"] is False
    assert manifest["finding_count"] == 0
    assert manifest["refs"] == ["draft.json", "work/business_review.json", "work/data_insufficiency_conclusion.json"]
    assert (item.accepted_root / "reviewed_draft.json").read_bytes() == item.draft_root.read_bytes()
    assert (item.accepted_root / "business_review.json").read_bytes() == item.business_review_path.read_bytes()
    assert (item.accepted_root / "data_insufficiency_conclusion.json").read_bytes() == item.data_insufficiency_path.read_bytes()

    with pytest.raises(ValueError, match="accepted outcome"):
        AcceptedAnalysisBundle.load(item)
    with pytest.raises((ValueError, FileExistsError)):
        item.accept()
    with pytest.raises((ValueError, FileExistsError)):
        analyst.review.finalize_blocked_by_evidence()

    reloaded = ItemWorkspace.load(item.context, item.item_id)
    assert reloaded.state["lifecycle_state"] == "blocked_by_evidence"
    (item.draft_root).write_bytes(b'{"tampered":true}\n')
    with pytest.raises(ValueError, match="blocked source draft hash"):
        ItemWorkspace.load(item.context, item.item_id)


def test_blocked_review_requires_one_repair_and_targeted_recheck(tmp_path: Path) -> None:
    analyst, _item = _analyst(tmp_path)
    finding = _finding()
    with pytest.raises(ValueError, match="owner conclusion"):
        analyst.review.record("confirm_data_insufficiency", reviewer_ref="reviewer")
    with pytest.raises(ValueError, match="lifecycle_state"):
        analyst.review.finalize_blocked_by_evidence()

    analyst2, _item2 = _analyst(tmp_path / "second")
    analyst2.review.record("repair_once", reviewer_ref="reviewer", findings=(finding,))
    analyst2.review.begin_repair()
    with pytest.raises(ValueError, match="lifecycle_state"):
        analyst2.review.finalize_blocked_by_evidence()


def test_blocked_review_rejects_active_attempt_and_nonpending_integration(tmp_path: Path) -> None:
    analyst, item = _analyst(tmp_path)
    item.begin_attempt("blocked-race", "host")
    with pytest.raises(ValueError, match="active attempt"):
        analyst.review.finalize_blocked_by_evidence()

    analyst2, item2 = _analyst(tmp_path / "integration")
    _prepare_blocked(analyst2)
    state = item2.state
    state["integration_state"] = "integrated"
    state["integration_manifest_hash"] = "a" * 64
    state["integration_manifest_ref"] = "integration/manifest.json"
    item2._persist_state(state)  # noqa: SLF001 - inject a nonpending precondition
    with pytest.raises(ValueError, match="pending integration"):
        analyst2.review.finalize_blocked_by_evidence()


def test_writer_and_block_finalizer_linearize_without_stale_draft_corruption(tmp_path: Path) -> None:
    analyst, item = _analyst(tmp_path)
    _prepare_blocked(analyst)
    writer = ItemWorkspace.load(item.context, item.item_id)
    finalizer = ItemWorkspace.load(item.context, item.item_id)

    # Writer-first: the writer owns the lock, invalidates the review, and the
    # later finalizer rejects before publishing a terminal directory.
    writer.write_draft({"answer": "writer-first"})
    with pytest.raises(ValueError, match="lifecycle_state"):
        finalizer.finalize_blocked_by_evidence()
    assert not finalizer.accepted_root.exists()
    assert json.loads(item.draft_root.read_text(encoding="utf-8"))["answer"] == "writer-first"

    # Recreate a clean post-repair targeted block for the opposite ordering.
    analyst2, item2 = _analyst(tmp_path / "finalizer-first")
    _prepare_blocked(analyst2)
    writer2 = ItemWorkspace.load(item2.context, item2.item_id)
    finalizer2 = ItemWorkspace.load(item2.context, item2.item_id)
    before = writer2.draft_root.read_bytes()
    writer_errors: list[BaseException] = []
    writer_started = threading.Event()

    def stale_writer() -> None:
        writer_started.set()
        try:
            writer2.write_draft({"answer": "must-be-rejected"})
        except BaseException as exc:  # noqa: BLE001 - assert the linearization result
            writer_errors.append(exc)

    with finalizer2._state_transition_lock():  # noqa: SLF001 - deterministic interleaving
        thread = threading.Thread(target=stale_writer)
        thread.start()
        assert writer_started.wait(2)
        finalizer2.finalize_blocked_by_evidence()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert writer_errors and isinstance(writer_errors[0], ValueError)
    assert writer2.draft_root.read_bytes() == before
    assert ItemWorkspace.load(item2.context, item2.item_id).state["lifecycle_state"] == "blocked_by_evidence"


def test_reviewer_findings_cannot_confirm_data_insufficiency(tmp_path: Path) -> None:
    analyst, item = _analyst(tmp_path)
    initial = _finding()
    analyst.review.record("repair_once", reviewer_ref="business-reviewer", findings=(initial,))
    analyst.review.begin_repair()
    analyst.submit_answer(AnalystAnswer(answer="Revised bounded answer.", method="Revised method."))
    packet_path = item.business_review_path
    state_path = item.item_root / "item_state.json"
    packet_before = packet_path.read_bytes()
    state_before = state_path.read_bytes()
    with pytest.raises(ValueError, match="owner conclusion"):
        analyst.review.record("confirm_data_insufficiency", reviewer_ref="targeted-reviewer", findings=())
    assert packet_path.read_bytes() == packet_before
    assert state_path.read_bytes() == state_before
    with pytest.raises(ValueError, match="cannot carry reviewer findings"):
        analyst.review.record(
            "confirm_data_insufficiency",
            reviewer_ref="targeted-reviewer",
            findings=(
                ReviewFinding(
                    finding_id="Q001-BR-NONMATERIAL",
                    target_sections=("method",),
                    semantic_categories=("method",),
                    problem="Cosmetic only.",
                    evidence="No material impact.",
                    required_change="No change.",
                    material=False,
                ),
            ),
        )
    assert packet_path.read_bytes() == packet_before
    assert state_path.read_bytes() == state_before

    analyst.conclude_data_insufficiency(
        DataInsufficiencyConclusion(
            reason="The specific claim is unsupported.",
            direct_answer_component="the specific claim",
            missing_data=("authoritative control source",),
            searches_tests=("searched the supplied catalog",),
            evidence_refs=("work/business_review.json",),
            supported_components=("bounded fixture observations",),
        )
    )
    analyst.review.record("confirm_data_insufficiency", reviewer_ref="targeted-reviewer")
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["findings"] == []
    analyst.review.finalize_blocked_by_evidence()
    terminal_packet = json.loads((item.accepted_root / "business_review.json").read_text(encoding="utf-8"))
    assert terminal_packet["findings"] == []


@pytest.mark.parametrize("tamper", ("copy", "packet", "source_packet", "manifest", "symlink"))
def test_blocked_snapshot_tamper_and_symlink_fail_closed(tmp_path: Path, tamper: str) -> None:
    analyst, item = _analyst(tmp_path)
    _prepare_blocked(analyst)
    analyst.review.finalize_blocked_by_evidence()
    if tamper == "copy":
        path = item.accepted_root / "reviewed_draft.json"
        path.write_bytes(path.read_bytes() + b"tampered\n")
    elif tamper == "packet":
        path = item.accepted_root / "business_review.json"
        path.write_bytes(path.read_bytes() + b"tampered\n")
    elif tamper == "source_packet":
        path = item.business_review_path
        path.write_bytes(path.read_bytes() + b"tampered\n")
    elif tamper == "manifest":
        path = item.accepted_root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["reason"] = "tampered"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        path = item.accepted_root / "business_review.json"
        path.unlink()
        path.symlink_to(item.business_review_path)
    with pytest.raises((ValueError, AllowedRootError), match="hash|manifest|symlink|snapshot|business review"):
        ItemWorkspace.load(item.context, item.item_id)


def test_blocked_terminal_recovery_converges_after_publication_and_state_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    analyst, item = _analyst(tmp_path)
    _prepare_blocked(analyst)
    original_publish = item._publish_accepted_directory

    def fail_publish(*args: object, **kwargs: object) -> Path:
        raise OSError("injected publication interruption")

    monkeypatch.setattr(item, "_publish_accepted_directory", fail_publish)
    with pytest.raises(OSError, match="publication interruption"):
        analyst.review.finalize_blocked_by_evidence()
    assert item.state["terminal_intent"]["outcome"] == "blocked_by_evidence"
    monkeypatch.setattr(item, "_publish_accepted_directory", original_publish)
    recovered = ItemWorkspace.load(item.context, item.item_id)
    assert recovered.state["terminal_intent"] is None
    assert recovered.state["lifecycle_state"] == "review"

    calls = 0
    original_persist = item._persist_state_unlocked

    def fail_final(state: dict[str, object], **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected state interruption")
        original_persist(state, **kwargs)

    monkeypatch.setattr(item, "_persist_state_unlocked", fail_final)
    with pytest.raises(OSError, match="state interruption"):
        analyst.review.finalize_blocked_by_evidence()
    assert item.accepted_root.is_dir()
    monkeypatch.setattr(item, "_persist_state_unlocked", original_persist)
    converged = ItemWorkspace.load(item.context, item.item_id)
    assert converged.state["lifecycle_state"] == "blocked_by_evidence"
    assert converged.state["terminal_outcome"]["outcome"] == "blocked_by_evidence"


def test_run_lifecycle_and_reporting_treat_blocked_as_limited_business_terminal(tmp_path: Path) -> None:
    context = RunContext("RUN-LIMITED", tmp_path / "run")
    lifecycle = RunLifecycle.create(context, ("Q-001", "Q-002"))
    blocked = {
        "item_id": "Q-001",
        "lifecycle_state": "blocked_by_evidence",
        "terminal_outcome": {"outcome": "blocked_by_evidence"},
        "integration_state": "pending",
        # A blocked item has no committed records, but its no-op integration
        # boundary is still explicit and typed rather than inferred from the
        # pending label.
        "blocked_integration_validation": {
            "valid": True,
            "stage": "not_committed",
            "verdict": None,
            "diagnostics": [],
        },
    }
    accepted = {
        "item_id": "Q-002",
        "lifecycle_state": "accepted",
        "terminal_outcome": {"outcome": "accepted"},
        "integration_state": "integrated",
        "committed_integration_validation": {
            "valid": True,
            "stage": "committed",
            "verdict": "committed",
            "diagnostics": [],
            "session_id": "session-Q-002",
            "records_count": 1,
            "records_hash": "b" * 64,
            "manifest_hash": "c" * 64,
        },
    }
    assert lifecycle.reconcile((blocked, accepted), product_terminal_status=True).state == "complete_with_limits"
    report = project_run_report(
        [
            {
                "item_id": "Q-001",
                "outcome": "blocked_by_evidence",
                "lifecycle_state": "blocked_by_evidence",
                "record_kind_totals": {},
                "implementation": {"sha": "a" * 40, "tree": "b" * 40, "version": "test"},
            }
        ],
        lifecycle_status="analytical_complete",
    )
    assert report["item_outcomes"] == {"Q-001": "blocked_by_evidence"}


@pytest.mark.parametrize(
    ("lifecycle_state", "outcome"),
    (
        ("accepted", "accepted"),
        ("accepted", "accepted_with_limits"),
        ("technical_failure", "technical_failure"),
        ("blocked_by_evidence", "blocked_by_evidence"),
    ),
)
def test_reporting_accepts_only_exact_terminal_lifecycle_outcome_pairs(
    lifecycle_state: str,
    outcome: str,
) -> None:
    report = project_run_report(
        [
            {
                "item_id": "Q-001",
                "outcome": outcome,
                "lifecycle_state": lifecycle_state,
                "record_kind_totals": {},
                "implementation": {"sha": "a" * 40, "tree": "b" * 40, "version": "test"},
            }
        ],
        lifecycle_status="analytical_complete",
    )
    assert report["item_outcomes"] == {"Q-001": outcome}


@pytest.mark.parametrize(
    ("lifecycle_state", "outcome"),
    (
        ("accepted", "technical_failure"),
        ("accepted", "blocked_by_evidence"),
        ("technical_failure", "accepted"),
        ("technical_failure", "blocked_by_evidence"),
        ("blocked_by_evidence", "accepted"),
        ("blocked_by_evidence", "technical_failure"),
    ),
)
def test_reporting_rejects_crossed_terminal_lifecycle_outcome_pairs(
    lifecycle_state: str,
    outcome: str,
) -> None:
    with pytest.raises(ValueError, match="lifecycle and outcome"):
        project_run_report(
            [
                {
                    "item_id": "Q-001",
                    "outcome": outcome,
                    "lifecycle_state": lifecycle_state,
                    "record_kind_totals": {},
                    "implementation": {"sha": "a" * 40, "tree": "b" * 40, "version": "test"},
                }
            ],
            lifecycle_status="analytical_complete",
        )


@pytest.mark.parametrize("lifecycle_state", ("work", "review", "recovering", "unknown"))
@pytest.mark.parametrize(
    "outcome",
    ("accepted", "accepted_with_limits", "technical_failure", "blocked_by_evidence"),
)
def test_reporting_rejects_nonterminal_or_unknown_lifecycle_with_terminal_outcome(
    lifecycle_state: str,
    outcome: str,
) -> None:
    with pytest.raises(ValueError, match="lifecycle and outcome"):
        project_run_report(
            [
                {
                    "item_id": "Q-001",
                    "outcome": outcome,
                    "lifecycle_state": lifecycle_state,
                    "record_kind_totals": {},
                    "implementation": {"sha": "a" * 40, "tree": "b" * 40, "version": "test"},
                }
            ],
            lifecycle_status="analytical_complete",
        )


def test_reporting_allows_manifest_without_lifecycle_metadata() -> None:
    report = project_run_report(
        [
            {
                "item_id": "Q-001",
                "outcome": "blocked_by_evidence",
                "lifecycle_state": "blocked_by_evidence",
                "record_kind_totals": {},
                "implementation": {"sha": "a" * 40, "tree": "b" * 40, "version": "test"},
            }
        ],
        item_manifests=[
            {
                "item_id": "Q-001",
                "outcome": "blocked_by_evidence",
                "record_kind_totals": {},
            }
        ],
        lifecycle_status="analytical_complete",
    )
    assert report["item_outcomes"] == {"Q-001": "blocked_by_evidence"}


def obsolete_blocked_source_transition_reuses_catalog_for_next_item_without_raw_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, rebound, lifecycle, _archive = _transitioned_source(tmp_path)
    analyst = AnalystWorkspace(rebound, owner_ref="owner-Q-001")
    analyst.submit_answer(AnalystAnswer(answer="Initial source answer.", method="Initial method."))
    _prepare_blocked(analyst)
    analyst.review.finalize_blocked_by_evidence()
    target = ItemWorkspace.create(current, "Q-002", original_text="Analyze the next bounded question.")
    original_infolist = zipfile.ZipFile.infolist
    original_read = zipfile.ZipFile.read

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("blocked-source inheritance must not reread the archive")

    monkeypatch.setattr(zipfile.ZipFile, "infolist", fail)
    monkeypatch.setattr(zipfile.ZipFile, "read", fail)
    try:
        created = BoundAnalysisContext.create_from_transitioned_catalog(current, target, rebound, lifecycle)
    finally:
        monkeypatch.setattr(zipfile.ZipFile, "infolist", original_infolist)
        monkeypatch.setattr(zipfile.ZipFile, "read", original_read)
    assert created.item_workspace.item_id == "Q-002"
    assert created.source_catalog.catalog_key == rebound.source_catalog.catalog_key


def test_owner_data_insufficiency_confirmation_is_the_only_blocked_terminal_authority(tmp_path: Path) -> None:
    analyst, item = _analyst(tmp_path)
    with pytest.raises(ValueError, match="owner conclusion"):
        analyst.review.record("confirm_data_insufficiency", reviewer_ref="reviewer")
    conclusion = DataInsufficiencyConclusion(
        reason="No authoritative procurement price source is present.",
        direct_answer_component="the milk-fat-to-price ratio",
        missing_data=("procurement price by milk source and period",),
        searches_tests=("searched the supplied catalog", "checked available source columns"),
        evidence_refs=("work/source_map.json",),
        supported_components=("supplied milk-fat observations",),
    )
    analyst.conclude_data_insufficiency(conclusion)
    with pytest.raises(ValueError, match="cannot carry reviewer findings"):
        analyst.review.record(
            "confirm_data_insufficiency",
            reviewer_ref="reviewer",
            findings=(_finding(),),
        )
    analyst.review.confirm_data_insufficiency(reviewer_ref="reviewer")
    snapshot = analyst.review.finalize_blocked_by_evidence()
    assert snapshot.outcome == "blocked_by_evidence"
    assert item.state["lifecycle_state"] == "blocked_by_evidence"
    manifest = json.loads((item.accepted_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["review_verdict"] == "confirm_data_insufficiency"
    assert (item.accepted_root / "data_insufficiency_conclusion.json").is_file()
    with pytest.raises(ValueError, match="accepted outcome"):
        AcceptedAnalysisBundle.load(item)
