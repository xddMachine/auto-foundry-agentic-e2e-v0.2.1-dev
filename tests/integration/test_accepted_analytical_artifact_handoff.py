"""Focused Analytical Owner -> business review -> integration handoff proof."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auto_foundry_core import (  # noqa: E402
    AnalyticalArtifact,
    AcceptedAnalysisBundle,
    AnalystAnswer,
    AnalystWorkspace,
    BoundAnalysisContext,
    DataAssetRef,
    DataProfileArtifact,
    DataRoomWorkbench,
    EvidenceNote,
    IntegrationSession,
    ItemWorkspace,
    PreparedAssetRegistry,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.workspace import AllowedRootError  # noqa: E402


def _fixture(
    tmp_path: Path,
    *,
    artifact: AnalyticalArtifact | None = None,
    accepted_refs: tuple[str, ...] = ("work/plan.json", "work/analytical_artifact.json"),
    canonical_bytes: bool = True,
    external_output: tuple[str, bytes] | None = None,
) -> tuple[RunContext, ItemWorkspace, AnalystWorkspace, IntegrationSession, AnalyticalArtifact, Path]:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    archive = input_root / "customers.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("customers.csv", "customer_id,revenue\nc1,10\nc2,20\n")

    context = RunContext("RUN-ACCEPTED-ANALYTICS", tmp_path / "run", (input_root,))
    RunLifecycle.create(context, ("REQ-A",), mode="question")
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(
        context,
        "REQ-A",
        mode="question",
        original_text="Profile the supplied customer population.",
    )
    bound = BoundAnalysisContext.create(
        context,
        DataAssetRef.from_path(archive),
        item,
        workbench=workbench,
    )
    analyst = AnalystWorkspace(bound, owner_ref="analytical-owner")
    source = analyst.search_sources("customers", limit=1)[0]

    # Public owner/evidence APIs establish the reviewed analytical context.
    analyst.begin_analysis(
        objective="Profile the supplied customer population.",
        strategy="Use the bounded source catalog and emit one typed profile artifact.",
        expected_outputs=("data profile",),
    )
    analyst.select_sources((source.source_id,), purpose="Primary customer population")
    analyst.record_evidence(
        EvidenceNote(
            evidence_id="E-ANALYTICS-001",
            conclusion="The bounded fixture contains two customer rows.",
            method="Count the admitted customers.csv rows.",
            evidence_refs=(source.source_id,),
            limitations=("Synthetic fixture only",),
            facts={"row_count": 2},
        )
    )
    analyst.submit_answer(
        AnalystAnswer(
            answer="The supplied fixture contains two customers.",
            method="Profile the admitted customer rows.",
            scope="Synthetic fixture rows only.",
            evidence_refs=(source.source_id, "work/evidence.jsonl#E-ANALYTICS-001"),
        )
    )

    if artifact is None:
        artifact = DataProfileArtifact(
            artifact_id="artifact-accepted-profile",
            requirement_id="REQ-A",
            dataset_fingerprint="d" * 64,
            source_refs=(source.path,),
            method="fixture_profile",
            profile={"row_count": 2, "columns": [{"name": "customer_id", "missing_count": 0}]},
            created_at="2026-01-01T00:00:00+00:00",
        )
    artifact_path = item.work_root / "analytical_artifact.json"
    if canonical_bytes:
        artifact_path.write_bytes(artifact.to_json().encode("utf-8"))
    else:
        # This is valid JSON and can be accepted, but is not the strict
        # canonical serialization required by the integration handoff.
        artifact_path.write_text(json.dumps(artifact.to_dict(), indent=2), encoding="utf-8")
    if external_output is not None:
        output_ref, output_bytes = external_output
        output_path = item.work_root / output_ref
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(output_bytes)

    # Public business-review and acceptance APIs authorize the exact artifact
    # ref.  The integration method must reject any file omitted here.
    analyst.review.record("accept", reviewer_ref="independent-business-reviewer")
    item.accept(accepted_refs=accepted_refs)
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "result-integration-agent",
        invocation_id="inv-accepted-analytics",
    )
    return context, item, analyst, session, artifact, artifact_path


def test_accepted_artifact_handoff_stages_exact_bytes_and_commits_without_lem_mutation(tmp_path: Path) -> None:
    context, item, _analyst, session, artifact, artifact_path = _fixture(tmp_path)
    before_lem = session.lem.export()
    assert not hasattr(session, "add_analytical_artifact")

    record_id = session.add_accepted_analytical_artifact(
        "work/analytical_artifact.json",
        scope="REQ-A analytics output",
        evidence_refs=("work/plan.json",),
    )
    assert session.add_accepted_analytical_artifact(
        "work/analytical_artifact.json",
        scope="REQ-A analytics output",
        evidence_refs=("work/plan.json",),
    ) == record_id
    record = next(record for record in session.records if record.record_id == record_id)
    assert record.kind == "analytical_artifact"
    assert "work/analytical_artifact.json" in record.evidence_refs

    packet = session.build_fidelity_packet()
    assert packet.records_hash == session.state["records_hash"]
    session.record_fidelity_review("accept", checked_record_ids=(record_id,))
    session.commit()

    committed_path = session.committed_root / "artifacts" / Path(record.payload["artifact_ref"]).name
    assert committed_path.read_bytes() == artifact_path.read_bytes()
    assert committed_path.read_bytes() == artifact.to_json().encode("utf-8")
    assert session.lem.export() == before_lem
    assert item.integration_state == "integrated"


def test_accepted_artifact_handoff_requires_explicit_accepted_ref(tmp_path: Path) -> None:
    _context, _item, _analyst, session, _artifact, _path = _fixture(
        tmp_path,
        accepted_refs=("work/plan.json",),
    )
    with pytest.raises(ValueError, match="not in the accepted bundle"):
        session.add_accepted_analytical_artifact("work/analytical_artifact.json")
    assert not session.records


def test_accepted_artifact_handoff_rejects_post_acceptance_tamper(tmp_path: Path) -> None:
    _context, _item, _analyst, session, _artifact, path = _fixture(tmp_path)
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="evidence changed after acceptance"):
        session.add_accepted_analytical_artifact("work/analytical_artifact.json")
    assert not session.records


def test_accepted_artifact_handoff_rejects_noncanonical_bytes(tmp_path: Path) -> None:
    _context, _item, _analyst, session, _artifact, _path = _fixture(tmp_path, canonical_bytes=False)
    with pytest.raises(ValueError, match="not canonical"):
        session.add_accepted_analytical_artifact("work/analytical_artifact.json")
    assert not session.records


def test_accepted_artifact_handoff_rejects_symlinked_ref(tmp_path: Path) -> None:
    _context, _item, _analyst, session, _artifact, path = _fixture(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises((AllowedRootError, ValueError), match="symlink|evidence"):
        session.add_accepted_analytical_artifact("work/analytical_artifact.json")
    assert not session.records


def _external_output_artifact(output_bytes: bytes) -> AnalyticalArtifact:
    return DataProfileArtifact(
        artifact_id="artifact-external-jsonl",
        requirement_id="REQ-A",
        dataset_fingerprint="d" * 64,
        source_refs=("customers.csv",),
        method="fixture_profile",
        profile={"row_count": 2},
        output_refs=(
            {
                "path": "results/assignments.jsonl",
                "format": "jsonl",
                "sha256": hashlib.sha256(output_bytes).hexdigest(),
                "size_bytes": len(output_bytes),
                "row_count": len(output_bytes.splitlines()),
                "complete": True,
            },
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_external_artifact_output_is_reverified_at_fidelity_and_copied_to_commit(tmp_path: Path) -> None:
    output_bytes = b'{"population_id":"c1","segment":0}\n{"population_id":"c2","segment":1}\n'
    artifact = _external_output_artifact(output_bytes)
    _context, item, _analyst, session, _artifact, output_path = _fixture(
        tmp_path,
        artifact=artifact,
        external_output=("results/assignments.jsonl", output_bytes),
    )
    # The fixture writes both the artifact and its external output before
    # acceptance, so the accepted progress manifest binds both byte streams.
    output_path = item.work_root / "results" / "assignments.jsonl"
    assert output_path.read_bytes() == output_bytes
    record_id = session.add_accepted_analytical_artifact("work/analytical_artifact.json", scope="analytics")
    record = next(record for record in session.records if record.record_id == record_id)
    session.build_fidelity_packet()
    session.record_fidelity_review("accept", checked_record_ids=(record.record_id,))
    session.commit()
    committed = session.committed_root / "artifacts" / "results" / "assignments.jsonl"
    assert committed.read_bytes() == output_bytes


def test_external_artifact_output_tamper_after_generation_fails_fidelity_closed(tmp_path: Path) -> None:
    output_bytes = b'{"population_id":"c1","segment":0}\n'
    artifact = _external_output_artifact(output_bytes)
    _context, _item, _analyst, session, _artifact, output_path = _fixture(
        tmp_path,
        artifact=artifact,
        external_output=("results/assignments.jsonl", output_bytes),
    )
    output_path = session.item_workspace.work_root / "results" / "assignments.jsonl"
    session.add_accepted_analytical_artifact("work/analytical_artifact.json", scope="analytics")
    output_path.write_bytes(output_bytes + b'{"population_id":"tampered","segment":9}\n')
    with pytest.raises(ValueError, match="sha256|size_bytes|row_count|artifact progress"):
        session.build_fidelity_packet()


def test_external_artifact_output_verification_streams_without_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cross the validator's 1 MiB read chunk without making one oversized
    # JSONL record.  Verification and commit must stay O(one line), not
    # O(file), and may not use Path.read_bytes on the source output.
    row = b'{"population_id":"c1","segment":0}\n'
    output_bytes = row * 180_000
    artifact = _external_output_artifact(output_bytes)
    _context, _item, _analyst, session, _artifact, _path = _fixture(
        tmp_path,
        artifact=artifact,
        external_output=("results/assignments.jsonl", output_bytes),
    )
    output_path = session.item_workspace.work_root / "results" / "assignments.jsonl"
    original_read_bytes = Path.read_bytes

    def reject_source_read_bytes(path: Path) -> bytes:
        if path == output_path:
            raise AssertionError("external analytical output must be streamed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_source_read_bytes)
    record_id = session.add_accepted_analytical_artifact("work/analytical_artifact.json", scope="analytics")
    session.build_fidelity_packet()
    session.record_fidelity_review("accept", checked_record_ids=(record_id,))
    session.commit()
    committed = session.committed_root / "artifacts" / "results" / "assignments.jsonl"
    assert committed.stat().st_size == len(output_bytes)


def test_external_artifact_output_rejects_traversal(tmp_path: Path) -> None:
    payload = b'{"population_id":"c1","segment":0}\n'
    with pytest.raises(ValueError, match="path escapes|output_ref|parent traversal"):
        # Constructor-level path validation intentionally rejects traversal;
        # integration retains the same fail-closed check for parsed artifacts.
        DataProfileArtifact(
            artifact_id="artifact-external-traversal",
            requirement_id="REQ-A",
            dataset_fingerprint="d" * 64,
            source_refs=("customers.csv",),
            method="fixture_profile",
            profile={"row_count": 1},
            output_refs=(
                {
                    "path": "../escape.jsonl",
                    "format": "jsonl",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "row_count": 1,
                    "complete": True,
                },
            ),
            created_at="2026-01-01T00:00:00+00:00",
        )


def test_external_artifact_output_rejects_symlink_at_fidelity(tmp_path: Path) -> None:
    output_bytes = b'{"population_id":"c1","segment":0}\n'
    artifact = _external_output_artifact(output_bytes)
    _context, item, _analyst, session, _artifact, _path = _fixture(
        tmp_path,
        artifact=artifact,
        external_output=("results/assignments.jsonl", output_bytes),
    )
    session.add_accepted_analytical_artifact("work/analytical_artifact.json", scope="analytics")
    output_path = item.work_root / "results" / "assignments.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(output_bytes)
    output_path.unlink()
    output_path.symlink_to(outside)
    with pytest.raises((AllowedRootError, ValueError), match="symlink|output"):
        session.build_fidelity_packet()


def test_accepted_artifact_handoff_rejects_requirement_mismatch(tmp_path: Path) -> None:
    artifact = DataProfileArtifact(
        artifact_id="artifact-foreign-requirement",
        requirement_id="REQ-OTHER",
        dataset_fingerprint="d" * 64,
        source_refs=("customers.csv",),
        method="fixture_profile",
        profile={"row_count": 2},
        created_at="2026-01-01T00:00:00+00:00",
    )
    _context, _item, _analyst, session, _artifact, _path = _fixture(tmp_path, artifact=artifact)
    with pytest.raises(ValueError, match="requirement_id"):
        session.add_accepted_analytical_artifact("work/analytical_artifact.json")
    assert not session.records


def test_accepted_artifact_handoff_rejects_unsupported_artifact_type(tmp_path: Path) -> None:
    artifact = AnalyticalArtifact(
        artifact_id="artifact-unsupported-type",
        artifact_type="unsupported_type",
        requirement_id="REQ-A",
        dataset_fingerprint="d" * 64,
        source_refs=("customers.csv",),
        method="fixture_profile",
        payload={"value": 1},
        created_at="2026-01-01T00:00:00+00:00",
    )
    _context, _item, _analyst, session, _artifact, _path = _fixture(tmp_path, artifact=artifact)
    with pytest.raises(ValueError, match="unsupported"):
        session.add_accepted_analytical_artifact("work/analytical_artifact.json")
    assert not session.records


def _answer_declared_artifact_fixture(tmp_path: Path) -> tuple[RunContext, ItemWorkspace, AnalyticalArtifact, Path]:
    """Build a requirement whose answer itself names the typed artifact."""

    context = RunContext("RUN-ANSWER-DECLARED-ARTIFACT", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-A",), mode="requirement")
    item = ItemWorkspace.create(
        context,
        "REQ-A",
        mode="requirement",
        original_text="Profile the supplied customer population.",
    )
    item.write_plan({"item_id": "REQ-A", "offline": True})
    artifact = DataProfileArtifact(
        artifact_id="artifact-answer-declared",
        requirement_id="REQ-A",
        dataset_fingerprint="d" * 64,
        source_refs=("synthetic:customers",),
        method="reviewed_fixture",
        profile={"row_count": 2},
        created_at="2026-01-01T00:00:00+00:00",
    )
    path = item.work_root / "answer_declared_artifact.json"
    path.write_bytes(artifact.to_json().encode("utf-8"))
    item.write_draft(
        {
            "item_id": "REQ-A",
            "answer": "The supplied fixture contains two customers.",
            "evidence_refs": ["requirements/REQ-A/work/answer_declared_artifact.json"],
        }
    )
    item.record_review("accept", reviewer_ref="business-reviewer")
    return context, item, artifact, path


def test_answer_declared_artifact_is_auto_staged_and_commits_without_explicit_refs(tmp_path: Path) -> None:
    context, item, artifact, source_path = _answer_declared_artifact_fixture(tmp_path)
    item.accept()
    bundle = AcceptedAnalysisBundle.load(item)
    assert len(bundle.analytical_artifact_handoff) == 1
    descriptor = bundle.analytical_artifact_handoff[0]
    assert descriptor["ref"] == "work/answer_declared_artifact.json"
    assert descriptor["hash"] == hashlib.sha256(source_path.read_bytes()).hexdigest()

    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "result-integration-agent",
        invocation_id="inv-answer-declared-artifact",
    )
    assert len(session.records) == 1
    record = session.records[0]
    assert record.kind == "analytical_artifact"
    assert record.payload["artifact_id"] == artifact.artifact_id
    assert session.add_accepted_analytical_artifact(descriptor["ref"]) == record.record_id
    record_id = record.record_id
    session.build_fidelity_packet()
    session.record_fidelity_review("accept", checked_record_ids=(record_id,))
    session.commit()
    assert item.integration_state == "integrated"
    assert not (item.item_root / "integration" / "technical_failure" / "manifest.json").exists()


def test_answer_declared_generic_type_json_remains_ordinary_evidence(tmp_path: Path) -> None:
    """Generic result/projection JSON must not opt into typed-artifact parsing."""

    context = RunContext("RUN-GENERIC-TYPE-EVIDENCE", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-A",), mode="requirement")
    item = ItemWorkspace.create(
        context,
        "REQ-A",
        mode="requirement",
        original_text="Preserve ordinary evidence.",
    )
    item.write_plan({"item_id": "REQ-A", "offline": True})
    ordinary_path = item.work_root / "ordinary_result.json"
    ordinary_path.write_text(json.dumps({"type": "chart", "value": 1}), encoding="utf-8")
    item.write_draft(
        {
            "item_id": "REQ-A",
            "answer": "The ordinary chart evidence is manifest-bound.",
            "evidence_refs": ["requirements/REQ-A/work/ordinary_result.json"],
        }
    )
    item.record_review("accept", reviewer_ref="business-reviewer")
    item.accept()

    bundle = AcceptedAnalysisBundle.load(item)
    assert bundle.accepted_refs == ("work/ordinary_result.json",)
    assert bundle.analytical_artifact_handoff == ()

    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "result-integration-agent",
        invocation_id="inv-generic-type-evidence",
    )
    assert all(record.kind != "analytical_artifact" for record in session.records)


def test_answer_declared_unmanifested_ref_fails_before_acceptance(tmp_path: Path) -> None:
    context = RunContext("RUN-UNMANIFESTED-ANSWER-REF", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-A",), mode="requirement")
    item = ItemWorkspace.create(context, "REQ-A", mode="requirement", original_text="Profile")
    item.write_plan({"item_id": "REQ-A"})
    item.write_draft(
        {
            "item_id": "REQ-A",
            "answer": "No typed output.",
            "evidence_refs": ["work/not-written.json"],
        }
    )
    item.record_review("accept", reviewer_ref="business-reviewer")
    with pytest.raises(ValueError, match="not bound by reviewed manifest"):
        item.accept()
    assert item.state["lifecycle_state"] == "review"
    assert not item.accepted_root.exists()


def test_precommit_integration_failure_is_recoverable_and_not_terminalized(tmp_path: Path) -> None:
    _context, item, _analyst, session, _artifact, _path = _fixture(tmp_path, accepted_refs=("work/plan.json",))
    failure_path = item.item_root / "integration" / "technical_failure" / "manifest.json"
    result = session.mark_technical_failure("internal handoff staging failed")
    assert result["status"] == "pending"
    assert result["recoverable"] is True
    assert result["continuation"] == "same_session"
    assert result["incident"]["category"] == "recovery"
    assert session.mark_technical_failure("internal handoff staging failed") == result
    assert session.status == "open"
    assert item.integration_state == "pending"
    assert not failure_path.exists()


def test_post_fidelity_integration_failure_is_also_recoverable(tmp_path: Path) -> None:
    _context, item, _analyst, session, _artifact, _path = _fixture(tmp_path, accepted_refs=("work/plan.json",))
    # A genuinely empty/no-op integration must still carry an explicit typed
    # limitation before fidelity review.  The strict staging contract rejects
    # an untouched session as ``staging_incomplete`` rather than reviewing an
    # empty checked-record set.
    session.add_limitation(
        {"limitation": "No reusable semantic change was established for this item."},
        scope="REQ-A integration",
        evidence_refs=("work/plan.json",),
    )
    session.build_fidelity_packet()
    session.record_fidelity_review("fail", checked_record_ids=())
    staging_paths = (
        session.staging_root / "snapshot.json",
        session.staging_root / "session.json",
        session.staging_root / "records.jsonl",
    )
    before = {path: path.read_bytes() for path in staging_paths}
    failure_path = item.item_root / "integration" / "technical_failure" / "manifest.json"
    result = session.mark_technical_failure("fidelity handoff failed")
    assert result["status"] == "pending"
    assert result["recoverable"] is True
    assert result["continuation"] == "same_session"
    assert session.status == "open"
    assert item.integration_state == "pending"
    assert not failure_path.exists()
    assert {path: path.read_bytes() for path in staging_paths} == before


def test_item_workspace_integration_failure_records_recoverable_incident(tmp_path: Path) -> None:
    _context, item, _analyst, _session, _artifact, _path = _fixture(tmp_path, accepted_refs=("work/plan.json",))
    result = item.mark_integration_failed("a" * 64, "integration/technical_failure/manifest.json")
    assert result["status"] == "pending"
    assert result["recoverable"] is True
    assert result["continuation"] == "same_session"
    assert result["incident"]["source"] == "item_workspace"
    assert item.integration_state == "pending"
