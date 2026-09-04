from __future__ import annotations

import json
import time

import pytest

from auto_foundry_core.analytical_artifacts import (
    AnalyticalArtifact,
    AnalyticalArtifactValidationError,
    DataProfileArtifact,
    KpiTableArtifact,
    SegmentProfilesArtifact,
    canonical_content_hash,
)


def test_typed_artifact_is_immutable_and_hash_round_trips() -> None:
    artifact = DataProfileArtifact(
        artifact_id="profile-1",
        requirement_id="REQ-001",
        dataset_fingerprint="dataset-fingerprint",
        source_refs=("customers.csv",),
        population={"name": "customers", "size": 2},
        grain="one_row_per_customer",
        period={"start": "2024-01-01", "end": "2024-01-31"},
        feature_definitions=({"name": "age", "kind": "numeric"},),
        method="descriptive_profile",
        validation_evidence={"finite_values": True},
        profile={"row_count": 2, "column_count": 1},
        metadata={"owner": "analytical_owner"},
        created_at="2024-02-01T00:00:00+00:00",
    )
    wire = artifact.to_dict()
    assert wire["type"] == "data_profile"
    assert wire["content_hash"] == artifact.content_hash
    restored = AnalyticalArtifact.from_dict(wire)
    assert isinstance(restored, DataProfileArtifact)
    assert restored.to_dict() == wire
    assert restored.canonical_hash == canonical_content_hash({k: v for k, v in wire.items() if k != "content_hash"})
    assert hash(artifact) == hash(restored)
    with pytest.raises(TypeError):
        artifact.metadata["owner"] = "other"  # type: ignore[index]


def test_artifact_rejects_unknown_fields_hash_tampering_and_metadata_escape() -> None:
    artifact = KpiTableArtifact(
        artifact_id="kpi-1",
        requirement_id="REQ-001",
        dataset_fingerprint="dataset-fingerprint",
        rows=[{"metric": 1}],
        created_at="2024-02-01T00:00:00+00:00",
    )
    unknown = artifact.to_dict()
    unknown["unexpected"] = True
    with pytest.raises(AnalyticalArtifactValidationError, match="unknown"):
        AnalyticalArtifact.from_dict(unknown)
    tampered = artifact.to_dict()
    tampered["content_hash"] = "0" * 64
    with pytest.raises(AnalyticalArtifactValidationError, match="content_hash"):
        AnalyticalArtifact.from_dict(tampered)
    with pytest.raises(ValueError, match="escapes"):
        KpiTableArtifact(
            artifact_id="kpi-escape",
            requirement_id="REQ-001",
            dataset_fingerprint="dataset-fingerprint",
            metadata={"output_path": "../outside.json"},
        )


def test_artifact_json_is_finite_and_deterministic() -> None:
    artifact = AnalyticalArtifact(
        artifact_id="future-1",
        artifact_type="future_statistical_method",
        requirement_id="REQ-001",
        dataset_fingerprint="dataset-fingerprint",
        payload={"values": [1, 2, 3]},
        created_at="2024-02-01T00:00:00+00:00",
    )
    encoded = artifact.to_json()
    assert json.loads(encoded)["content_hash"] == artifact.content_hash
    assert encoded == artifact.to_json()
    with pytest.raises(ValueError, match="finite"):
        AnalyticalArtifact(
            artifact_id="bad",
            artifact_type="future",
            requirement_id="REQ-001",
            dataset_fingerprint="dataset-fingerprint",
            payload={"bad": float("nan")},
        )


def test_segment_profiles_artifact_round_trips_as_typed_payload() -> None:
    artifact = SegmentProfilesArtifact(
        artifact_id="profiles-round-trip",
        requirement_id="REQ-001",
        dataset_fingerprint="dataset-fingerprint",
        profiles=[{"segment": "A", "count": 3}],
        metadata={"title": "Reviewed segments"},
        created_at="2024-02-01T00:00:00+00:00",
    )
    wire = artifact.to_dict()
    restored = AnalyticalArtifact.from_dict(wire)
    assert isinstance(restored, SegmentProfilesArtifact)
    assert restored.to_dict() == wire
    assert restored.content_hash == artifact.content_hash
    assert restored.envelope_hash == artifact.envelope_hash
    restored_json = AnalyticalArtifact.from_json(artifact.to_json())
    assert isinstance(restored_json, SegmentProfilesArtifact)
    assert restored_json.to_dict() == wire


def test_observational_created_at_does_not_change_content_identity_and_is_integrity_bound() -> None:
    first = DataProfileArtifact(
        artifact_id="profile-repeatable",
        requirement_id="REQ-001",
        dataset_fingerprint="dataset-fingerprint",
        profile={"row_count": 2},
    )
    time.sleep(1.1)
    second = DataProfileArtifact(
        artifact_id="profile-repeatable",
        requirement_id="REQ-001",
        dataset_fingerprint="dataset-fingerprint",
        profile={"row_count": 2},
    )
    assert first.created_at != second.created_at
    assert first.content_hash == second.content_hash
    assert first.envelope_hash != second.envelope_hash

    tampered = first.to_dict()
    tampered["created_at"] = second.created_at
    with pytest.raises(AnalyticalArtifactValidationError, match="envelope_hash"):
        AnalyticalArtifact.from_dict(tampered)
