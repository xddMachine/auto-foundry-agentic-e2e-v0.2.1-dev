"""Vertical contract checks for typed analytical artifacts.

These tests intentionally exercise one immutable artifact set end-to-end:
accepted requirement -> IntegrationSession staging/fidelity/commit/reload ->
read-only dashboard assembly.  The product path must consume committed typed
bytes only; it must not call the metric projection or analytics toolkit.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPT = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_assembler.py"
spec = importlib.util.spec_from_file_location("dashboard_assembler_artifact_pipeline", SCRIPT)
assert spec and spec.loader
dashboard_assembler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboard_assembler
spec.loader.exec_module(dashboard_assembler)

from auto_foundry_core.analytical_artifacts import (  # noqa: E402
    DataProfileArtifact,
    KpiTableArtifact,
    SegmentProfilesArtifact,
    SegmentationModelArtifact,
)
from auto_foundry_core.durable import ItemWorkspace  # noqa: E402
from auto_foundry_core.integration import IntegrationRecord, IntegrationSession  # noqa: E402
from auto_foundry_core.lifecycle import RunLifecycle  # noqa: E402
from auto_foundry_core.prepared import PreparedAssetRegistry  # noqa: E402
from auto_foundry_core.workspace import RunContext  # noqa: E402


def _seed_artifact_run(
    tmp_path: Path,
    *,
    commit: bool = True,
    review: bool = True,
) -> tuple[RunContext, ItemWorkspace, IntegrationSession, list[Any]]:
    root = tmp_path / "run"
    context = RunContext("RUN-ANALYTICAL-ARTIFACT-VERTICAL", root, core_version="test")
    RunLifecycle.create(context, ("REQ-A",), mode="requirement")
    (root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {"groups": [{"id": "commercial", "title": "Reviewed analytics", "requirement_ids": ["REQ-A"]}]},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    workspace = ItemWorkspace.create(
        context,
        "REQ-A",
        mode="requirement",
        original_text="Analyze the supplied customer population and publish reviewed outputs",
    )
    workspace.write_plan({"item_id": "REQ-A", "offline": True})
    common = {
        "requirement_id": "REQ-A",
        "dataset_fingerprint": "d" * 64,
        "source_refs": ("synthetic:dataset", "work/never-read.csv"),
        "method": "reviewed_fixture",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    artifacts = [
        DataProfileArtifact(
            artifact_id="artifact-data-profile",
            profile={"columns": [{"name": "customer_id", "nulls": 0}], "note": "literal 42% text"},
            **common,
        ),
        KpiTableArtifact(
            artifact_id="artifact-kpi-table",
            rows=[{"metric": "retained_customers", "value": 42, "unit": "records"}],
            **common,
        ),
        SegmentationModelArtifact(
            artifact_id="artifact-segmentation-model",
            model={
                "assignments": [{"customer_id": "C-1", "segment": "A"}],
                "candidate_k_validation": [{"k": 2, "silhouette": 0.71}],
            },
            segment_profiles={
                # Counts are deliberately greater than the bar family's
                # bounded 0..100 size domain.  The dashboard must retain
                # these exact values in the existing column family rather
                # than dropping geometry or calculating shares.
                "segment_sizes": {"A": 300, "B": 250},
                "segment_profiles": [{"segment": "A", "mean_value": 12}],
            },
            **common,
        ),
        SegmentProfilesArtifact(
            artifact_id="artifact-segment-profiles",
            profiles=[{"segment": "A", "mean_value": 12, "count": 3}],
            **common,
        ),
    ]
    # Owner-authored artifact bytes live in the item work area before review;
    # the exact refs are explicitly included in the accepted bundle below.
    artifact_refs: list[str] = []
    for artifact in artifacts:
        relative = f"work/analytical_artifacts/{artifact.artifact_id}.json"
        destination = workspace.item_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(artifact.to_json().encode("utf-8"))
        artifact_refs.append(relative)

    workspace.write_draft({"item_id": "REQ-A", "answer": "typed artifact result"})
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json", *artifact_refs))

    session = IntegrationSession.create(
        context,
        workspace,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-artifacts",
    )
    for artifact_ref in artifact_refs:
        session.add_accepted_analytical_artifact(
            artifact_ref,
            scope="analytics",
            evidence_refs=("work/plan.json",),
        )
    record_ids = tuple(record.record_id for record in session.records)
    if review:
        packet = session.build_fidelity_packet()
        assert packet.records_hash == session.state["records_hash"]
        session.record_fidelity_review("accept", checked_record_ids=record_ids)
    if commit:
        session.commit()
    return context, workspace, session, artifacts


def _artifact_record(session: IntegrationSession, artifact_id: str) -> IntegrationRecord:
    record = next(record for record in session.records if record.payload.get("artifact_id") == artifact_id)
    return record


@pytest.mark.parametrize(
    "segment_sizes",
    (
        {"A": 3, "B": 7},
        {"A": 3, "B": 150},
    ),
)
def test_segmentation_counts_always_use_lossless_raw_count_geometry(segment_sizes: dict[str, int]) -> None:
    """Counts stay counts even when their values fit a percentage domain."""

    artifact = SegmentationModelArtifact(
        artifact_id="artifact-segment-count-regression",
        model={"assignments": []},
        segment_profiles={"segment_sizes": segment_sizes},
        requirement_id="REQ-A",
        dataset_fingerprint="d" * 64,
        source_refs=("synthetic:dataset",),
        method="reviewed_fixture",
        created_at="2026-01-01T00:00:00+00:00",
    )
    artifact_bytes = artifact.to_json().encode("utf-8")
    record = {
        "record_id": "record-segment-count-regression",
        "kind": "analytical_artifact",
        "item_id": "REQ-A",
        "accepted_content_hash": "a" * 64,
        "scope": "analytics",
        "evidence_refs": ["work/plan.json"],
        "payload": {
            "artifact": artifact.to_dict(),
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "schema_version": artifact.schema_version,
            "requirement_id": artifact.requirement_id,
            "content_hash": artifact.content_hash,
            "envelope_hash": artifact.envelope_hash,
            "canonical_bytes_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "artifact_ref": "integration/committed/artifacts/artifact-segment-count-regression.json",
        },
    }
    widgets = dashboard_assembler._analytical_artifact_widgets("REQ-A", {}, record)
    column = next(widget for widget in widgets if widget.get("type") == "column")
    assert column["geometry_mode"] == "raw_counts"
    assert column["categories"] == [
        {"label": key, "series": [{"label": "Count", "value": value}]}
        for key, value in sorted(segment_sizes.items())
    ]
    assert all("size" not in series for category in column["categories"] for series in category["series"])
    assert not any(widget.get("type") == "bar" for widget in widgets)


def test_all_typed_artifacts_cross_integration_and_dashboard_with_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context, workspace, session, artifacts = _seed_artifact_run(tmp_path)
    before_lem = session.lem.export()

    # Artifact-only product assembly must not enter the metric projection path
    # or import/run the optional analytics toolkit.
    monkeypatch.setattr(
        dashboard_assembler,
        "_metric_widgets",
        lambda *_args, **_kwargs: pytest.fail("artifact assembly called _metric_widgets"),
    )
    calls: list[str] = []
    original_read_bytes = dashboard_assembler._read_bytes

    def read_bytes(context_arg: Any, reference: str | Path, *, label: str):
        calls.append(str(reference))
        return original_read_bytes(context_arg, reference, label=label)

    monkeypatch.setattr(dashboard_assembler, "_read_bytes", read_bytes)
    receipt = dashboard_assembler.assemble_dashboard(context, item_ids=["REQ-A"])

    # Reloading the committed session proves the artifact files survive the
    # atomic swap and remain record-only (the LEM export is unchanged).
    reloaded = IntegrationSession.load(
        context,
        workspace,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-artifacts",
    )
    assert before_lem == reloaded.lem.export()
    assert reloaded.status == "committed"
    assert len(reloaded.records) == 4
    for artifact in artifacts:
        record = _artifact_record(reloaded, artifact.artifact_id)
        payload = record.payload
        assert payload["artifact_id"] == artifact.artifact_id
        assert payload["artifact_type"] == artifact.artifact_type
        assert payload["schema_version"] == artifact.schema_version
        assert payload["requirement_id"] == "REQ-A"
        assert payload["content_hash"] == artifact.content_hash
        assert payload["envelope_hash"] == artifact.envelope_hash
        assert payload["canonical_bytes_sha256"] == hashlib.sha256(artifact.to_json().encode()).hexdigest()
        committed_path = session.committed_root / "artifacts" / Path(payload["artifact_ref"]).name
        assert committed_path.is_file() and not committed_path.is_symlink()

    fixture = json.loads((context.run_root / receipt["outputs"]["fixture_ref"]).read_text(encoding="utf-8"))
    chart_map = json.loads((context.run_root / receipt["outputs"]["chart_map_ref"]).read_text(encoding="utf-8"))
    artifact_ids = {artifact.artifact_id for artifact in artifacts}
    artifact_widgets = [widget for widget in fixture["widgets"] if widget.get("analytical_artifact_id") in artifact_ids]
    assert {widget["analytical_artifact_type"] for widget in artifact_widgets} == {
        "data_profile", "kpi_table", "segmentation_model", "segment_profiles",
    }
    segmentation_column = next(
        widget
        for widget in artifact_widgets
        if widget.get("analytical_artifact_type") == "segmentation_model"
        and widget["type"] == "column"
    )
    assert segmentation_column["geometry_mode"] == "raw_counts"
    assert segmentation_column["categories"] == [
        {"label": "A", "series": [{"label": "Count", "value": 300}]},
        {"label": "B", "series": [{"label": "Count", "value": 250}]},
    ]
    assert all("size" not in series for category in segmentation_column["categories"] for series in category["series"])
    assert all(widget.get("artifact_provenance", {}).get("artifact_id") in artifact_ids for widget in artifact_widgets)
    assert fixture["analytical_artifacts"] == receipt["analytical_artifacts"]
    assert receipt["freeze_inputs"]["analytical_artifacts"] == receipt["analytical_artifacts"]
    assert receipt["input_items"][0]["analytical_artifacts"] == receipt["analytical_artifacts"]
    assert receipt["source_policy"] == "accepted_and_committed_only"
    assert receipt["new_analytics"] is False
    assert set(receipt["output_hashes"]) == {
        "fixture_sha256",
        "chart_map_sha256",
        "chart_registry_sha256",
        "blueprint_sha256",
        "site_manifest_sha256",
    }
    chart_artifacts = [chart for chart in chart_map["charts"] if chart.get("fields_or_values_used", {}).get("analytical_artifact_id") in artifact_ids]
    assert chart_artifacts
    assert all(chart["provenance"]["artifact_provenance"]["artifact_id"] in artifact_ids for chart in chart_artifacts)
    audit_artifacts = [entry for entry in fixture["audit_records"] if entry.get("kind") == "analytical_artifact"]
    assert {entry["artifact_provenance"]["artifact_id"] for entry in audit_artifacts} == artifact_ids
    assert all(any("integration/committed/artifacts/" in ref for ref in entry["trace_refs"]) for entry in audit_artifacts)
    assert not any("work/never-read.csv" in reference for reference in calls)


def test_artifact_wire_and_idempotence_and_collision_are_strict(tmp_path: Path) -> None:
    _context, _workspace, session, artifacts = _seed_artifact_run(tmp_path, commit=False, review=False)
    # This test exercises the internal serializer seam only; the vertical
    # fixture above verifies the supported accepted-ref workflow. The same
    # immutable instance is an exact retry and does not append a second
    # record.
    before = tuple(record.record_id for record in session.records)
    same_id = session._add_analytical_artifact(  # noqa: SLF001 - internal contract coverage
        artifacts[0].to_dict(),
        scope="analytics",
        evidence_refs=("work/analytical_artifacts/artifact-data-profile.json", "work/plan.json"),
    )
    assert same_id == before[0]
    assert tuple(record.record_id for record in session.records) == before

    changed = DataProfileArtifact(
        artifact_id=artifacts[0].artifact_id,
        requirement_id="REQ-A",
        dataset_fingerprint="d" * 64,
        profile={"columns": [{"name": "changed"}]},
        source_refs=("synthetic:dataset",),
        method="reviewed_fixture",
        created_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="collision"):
        session._add_analytical_artifact(  # noqa: SLF001 - internal contract coverage
            changed,
            scope="analytics",
            evidence_refs=("work/analytical_artifacts/artifact-data-profile.json", "work/plan.json"),
        )

    payload = dict(_artifact_record(session, artifacts[1].artifact_id).payload)
    payload["artifact_type"] = "data_profile"
    with pytest.raises(ValueError, match="artifact_type"):
        session._stage("analytical_artifact", payload, scope="analytics", evidence_refs=("work/plan.json",))


@pytest.mark.parametrize("mutation", ["missing", "tamper", "symlink"])
def test_committed_artifact_missing_tamper_and_symlink_fail_closed(tmp_path: Path, mutation: str) -> None:
    context, _workspace, session, artifacts = _seed_artifact_run(tmp_path)
    record = _artifact_record(session, artifacts[0].artifact_id)
    path = session.committed_root / "artifacts" / Path(record.payload["artifact_ref"]).name
    if mutation == "missing":
        path.unlink()
    elif mutation == "tamper":
        path.write_bytes(path.read_bytes() + b"tampered")
    else:
        target = tmp_path / "outside.json"
        target.write_text("outside", encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
    with pytest.raises((ValueError, dashboard_assembler.AssemblyError), match="artifact|missing|symlink|bytes|drifted"):
        if mutation == "symlink":
            IntegrationSession.load(
                context,
                _workspace,
                PreparedAssetRegistry(context),
                "synthetic-integration",
                invocation_id="inv-artifacts",
            )
        else:
            dashboard_assembler.assemble_dashboard(context, item_ids=["REQ-A"])


def test_fidelity_packet_artifact_tamper_is_rejected(tmp_path: Path) -> None:
    _context, _workspace, session, artifacts = _seed_artifact_run(tmp_path, commit=False, review=True)
    packet_path = session.fidelity_packet_path
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    target = next(record for record in packet["records"] if record["payload"].get("artifact_id") == artifacts[1].artifact_id)
    target["payload"]["artifact_id"] = "tampered-artifact-id"
    packet_path.write_text(json.dumps(packet, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(ValueError, match="packet|record|hash|artifact"):
        session.fidelity_packet
