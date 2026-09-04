from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from auto_foundry_core.analytics_toolkit import (
    AnalyticsDependencyError,
    AnalyticsInputError,
    compute_kpi_table,
    ingest_tabular,
    MetricDefinition,
    profile_data,
    segment_customers,
)
from auto_foundry_core.contracts import TableRef


def test_profile_and_kpis_are_source_backed_and_finite(tmp_path: Path) -> None:
    source = tmp_path / "customers.csv"
    source.write_text(
        "customer_id,segment,revenue,active\n"
        "c1,A,10,True\n"
        "c2,A,20,False\n"
        "c3,B,,True\n",
        encoding="utf-8",
    )
    profile = profile_data(source, requirement_id="REQ-001", allowed_roots=(tmp_path,))
    assert profile.artifact_type == "data_profile"
    assert profile.profile["row_count"] == 3
    assert list(profile.profile["selected_columns"]) == ["customer_id", "segment", "revenue", "active"]
    assert profile.profile["columns"]["revenue"]["missing_count"] == 1
    assert profile.source_refs and profile.source_fingerprints["dataset"]

    kpi = compute_kpi_table(
        source,
        [
            {"name": "revenue_sum", "aggregation": "sum", "value_column": "revenue", "group_by": ["segment"]},
            {"name": "active_count", "aggregation": "count_true", "value_column": "active", "group_by": ["segment"]},
        ],
        requirement_id="REQ-001",
        allowed_roots=(tmp_path,),
    )
    assert kpi.artifact_type == "kpi_table"
    assert [row["segment"] for row in kpi.rows] == ["A", "B"]
    assert kpi.rows[0]["revenue_sum"] == 30
    assert kpi.rows[0]["active_count"] == 1
    assert list(kpi.payload["selected_columns"]) == ["customer_id", "segment", "revenue", "active"]


def test_kpi_aggregation_contract_requires_values_and_never_uses_implicit_index() -> None:
    frame = pd.DataFrame({"id": [10, 20, 30], "value": [1.0, None, 3.0], "flag": [True, False, True]})
    with pytest.raises(ValueError, match="value_column is mandatory"):
        MetricDefinition(name="bad_mean", aggregation="mean")
    with pytest.raises(ValueError, match="value_column is mandatory"):
        compute_kpi_table(frame, [{"name": "bad_nunique", "aggregation": "nunique"}])
    rows = compute_kpi_table(
        frame,
        [
            {"name": "row_count", "aggregation": "count"},
            {"name": "value_count", "aggregation": "count", "value_column": "value"},
        ],
    ).rows
    assert rows == ({"row_count": 3, "value_count": 2},)
    with pytest.raises(AnalyticsInputError, match="numeric value_column"):
        compute_kpi_table(frame.assign(value=["1", "2", "3"]), [{"name": "bad_sum", "aggregation": "sum", "value_column": "value"}])
    with pytest.raises(AnalyticsInputError, match="non-finite"):
        compute_kpi_table(frame.assign(value=[1.0, float("inf"), 3.0]), [{"name": "bad_sum", "aggregation": "sum", "value_column": "value"}])


def test_ingestion_rejects_remote_paths_and_missing_columns() -> None:
    with pytest.raises(AnalyticsInputError, match="network"):
        ingest_tabular("https://example.test/customers.csv")
    with pytest.raises(AnalyticsInputError, match="missing"):
        ingest_tabular(pd.DataFrame({"id": [1]}), columns=["missing"])


def test_parquet_and_sqlite_table_refs_materialize_equivalent_frames(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": ["c1", "c2", "c3"],
            "spend": [10.5, 20.0, 0.0],
            "active": [1, 0, 1],
        }
    )
    parquet_path = tmp_path / "customers.parquet"
    frame.to_parquet(parquet_path, index=False)
    sqlite_path = tmp_path / "customers.sqlite3"
    connection = sqlite3.connect(sqlite_path)
    try:
        frame.to_sql("customers", connection, index=False)
        connection.commit()
    finally:
        connection.close()

    parquet_input = ingest_tabular(parquet_path, allowed_roots=(tmp_path,))
    sqlite_input = ingest_tabular(
        TableRef(asset=str(sqlite_path), name="customers"),
        allowed_roots=(tmp_path,),
    )
    pd.testing.assert_frame_equal(parquet_input.frame, sqlite_input.frame)
    assert parquet_input.source_fingerprints["selected"] == sqlite_input.source_fingerprints["selected"]
    assert isinstance(sqlite_input.source_refs[0], TableRef)
    assert sqlite_input.source_refs[0].name == "customers"


def test_sqlite_table_membership_is_validated_before_identifier_quoting(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "customers.db"
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute('CREATE TABLE "safe name" (id INTEGER)')
        connection.execute('INSERT INTO "safe name" VALUES (1)')
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AnalyticsInputError, match="unknown SQLite table"):
        ingest_tabular(TableRef(asset=str(sqlite_path), name="missing"), allowed_roots=(tmp_path,))
    with pytest.raises(AnalyticsInputError, match="unknown SQLite table"):
        ingest_tabular(
            TableRef(asset=str(sqlite_path), name='safe name"; DROP TABLE "safe name"; --'),
            allowed_roots=(tmp_path,),
        )
    with pytest.raises(AnalyticsInputError, match="requires a TableRef"):
        ingest_tabular(sqlite_path, allowed_roots=(tmp_path,))


def test_segmentation_reports_actionable_core_dependency_error_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = pd.DataFrame({"customer_id": ["c1", "c2", "c3"], "spend": [1.0, 2.0, 3.0], "channel": ["web", "store", "web"]})
    original_import_module = importlib.import_module

    def missing_sklearn(name: str, *args: object, **kwargs: object):
        if name in {"scipy", "sklearn"}:
            raise ImportError("test dependency omission")
        return original_import_module(name, *args, **kwargs)

    monkeypatch.setattr("auto_foundry_core.analytics_toolkit.importlib.import_module", missing_sklearn)
    with pytest.raises(AnalyticsDependencyError, match="pip install -e \\."):
        segment_customers(frame, population_id="customer_id", numeric_features=["spend"], categorical_features=["channel"])


@pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("numpy", "scipy", "sklearn")),
    reason="core analytics dependencies are not installed",
)
def test_segmentation_is_deterministic_and_emits_profiles() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"c{i}" for i in range(8)],
            "spend": [1.0, 1.1, 1.2, 1.3, 9.0, 9.1, 9.2, 9.3],
            "orders": [1, 1, 2, 2, 8, 9, 8, 9],
            "channel": ["web", "web", "store", "store", "web", "web", "store", "store"],
        }
    )
    first = segment_customers(
        frame,
        population_id="customer_id",
        numeric_features=["spend", "orders"],
        categorical_features=["channel"],
        requested_k=2,
        random_seed=7,
        requirement_id="REQ-001",
    )
    second = segment_customers(
        frame,
        population_id="customer_id",
        numeric_features=["spend", "orders"],
        categorical_features=["channel"],
        requested_k=2,
        random_seed=7,
        requirement_id="REQ-001",
    )
    assert first.content_hash == second.content_hash
    assert first.model["assignable"] is True
    assert first.model["assignment_complete"] is True
    assert len(first.model["assignments"]) == len(frame)
    assert first.segment_profiles["segment_profiles"]
    size_intent = next(intent for intent in first.visualization_intents if intent["table"] == "segment_sizes")
    assert size_intent == {
        "type": "segment_size_column",
        "table": "segment_sizes",
        "encoding": {"x": "segment", "y": "count", "value_kind": "raw_count"},
    }
    assert all(value is None or value == value for row in first.segment_profiles["segment_profiles"] for value in row.values())
    assert any(row["feature_kind"] == "categorical" and row["index"] is not None for row in first.segment_profiles["segment_profiles"])


@pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("numpy", "scipy", "sklearn")),
    reason="core analytics dependencies are not installed",
)
def test_large_segmentation_externalizes_complete_hashed_assignments(tmp_path: Path) -> None:
    row_count = 100_005
    frame = pd.DataFrame(
        {
            "customer_id": [f"c{i}" for i in range(row_count)],
            "spend": [float(i % 17) for i in range(row_count)],
        }
    )
    output_path = tmp_path / "assignments.jsonl"
    artifact = segment_customers(
        frame,
        population_id="customer_id",
        numeric_features=["spend"],
        requested_k=2,
        random_seed=7,
        n_init=1,
        assignment_output_ref=output_path,
        allowed_roots=(tmp_path,),
    )

    descriptor = artifact.model["assignment_output_ref"]
    encoded = output_path.read_bytes()
    assert artifact.model["assignment_complete"] is True
    assert artifact.model["assignment_row_count"] == row_count
    assert artifact.model["assignments"] == ()
    assert descriptor["path"] == "assignments.jsonl"
    assert descriptor["row_count"] == row_count
    assert descriptor["complete"] is True
    assert descriptor["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert descriptor["size_bytes"] == len(encoded)
    assert len(encoded.splitlines()) == row_count
    first_row = json.loads(encoded.splitlines()[0])
    last_row = json.loads(encoded.splitlines()[-1])
    assert first_row["population_id"] == "c0"
    assert last_row["population_id"] == f"c{row_count - 1}"
    assert artifact.tables[0]["row_count"] == row_count
    assert artifact.output_refs[0]["sha256"] == descriptor["sha256"]

    second = segment_customers(
        frame,
        population_id="customer_id",
        numeric_features=["spend"],
        requested_k=2,
        random_seed=7,
        n_init=1,
        assignment_output_ref=output_path,
        allowed_roots=(tmp_path,),
    )
    assert second.content_hash == artifact.content_hash
    assert second.output_refs == artifact.output_refs


@pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("numpy", "scipy", "sklearn")),
    reason="core analytics dependencies are not installed",
)
def test_large_segmentation_rejects_unsafe_assignment_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"c{i}" for i in range(8)],
            "spend": [float(i) for i in range(8)],
        }
    )
    monkeypatch.setattr("auto_foundry_core.analytics_toolkit._MAX_ASSIGNMENT_ROWS", 3)
    with pytest.raises(AnalyticsInputError, match="parent traversal"):
        segment_customers(
            frame,
            population_id="customer_id",
            numeric_features=["spend"],
            requested_k=2,
            assignment_output_ref="../assignments.jsonl",
        )


@pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("numpy", "scipy", "sklearn")),
    reason="core analytics dependencies are not installed",
)
def test_large_segmentation_without_output_authority_keeps_complete_assignments_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"c{i}" for i in range(8)],
            "spend": [float(i) for i in range(8)],
        }
    )
    monkeypatch.delenv("AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT", raising=False)
    monkeypatch.setattr("auto_foundry_core.analytics_toolkit._MAX_ASSIGNMENT_ROWS", 3)
    artifact = segment_customers(
        frame,
        population_id="customer_id",
        numeric_features=["spend"],
        requested_k=2,
    )
    assert len(artifact.model["assignments"]) == len(frame)
    assert artifact.model["assignment_complete"] is True
    assert artifact.model.get("assignment_output_ref") is None
    assert artifact.output_refs == ()
    assert "fully embedded" in " ".join(artifact.limitations)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("numpy", "scipy", "sklearn")),
    reason="core analytics dependencies are not installed",
)
def test_large_segmentation_rejects_symlink_assignment_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"c{i}" for i in range(8)],
            "spend": [float(i) for i in range(8)],
        }
    )
    target = tmp_path / "target.jsonl"
    target.write_text("existing\n", encoding="utf-8")
    symlink = tmp_path / "assignments.jsonl"
    symlink.symlink_to(target)
    monkeypatch.setattr("auto_foundry_core.analytics_toolkit._MAX_ASSIGNMENT_ROWS", 3)
    with pytest.raises(AnalyticsInputError, match="symlink"):
        segment_customers(
            frame,
            population_id="customer_id",
            numeric_features=["spend"],
            requested_k=2,
            assignment_output_ref=symlink,
            allowed_roots=(tmp_path,),
        )
    assert target.read_text(encoding="utf-8") == "existing\n"


@pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("numpy", "scipy", "sklearn")),
    reason="core analytics dependencies are not installed",
)
def test_large_segmentation_uses_controlled_output_root_for_default_externalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"c{i}" for i in range(8)],
            "spend": [float(i) for i in range(8)],
        }
    )
    output_root = tmp_path / "analysis-output"
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    monkeypatch.setenv("AUTO_FOUNDRY_ANALYSIS_OUTPUT_ROOT", str(output_root))
    monkeypatch.setattr("auto_foundry_core.analytics_toolkit._MAX_ASSIGNMENT_ROWS", 3)
    artifact = segment_customers(
        frame,
        population_id="customer_id",
        numeric_features=["spend"],
        requested_k=2,
        allowed_roots=(input_root,),
    )
    descriptor = artifact.model["assignment_output_ref"]
    assert descriptor["path"].startswith("segment_assignments-")
    assert not Path(descriptor["path"]).is_absolute()
    output_path = output_root / descriptor["path"]
    assert output_path.is_file()
    assert descriptor["size_bytes"] == output_path.stat().st_size
