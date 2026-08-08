from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_foundry_core.aggregation import aggregate_rows
from auto_foundry_core.capabilities import descriptors
from auto_foundry_core.normalization import normalize_rows, parse_date
from auto_foundry_core.populations import PopulationLedger
from auto_foundry_core.relationships import measure_relationship
from auto_foundry_core.sources import discover, read_rows


def test_aggregation_explicit_distinct_ratio_ranking_and_period_order() -> None:
    rows = [
        {"group": "missing", "value": None, "id": "a"},
        {"group": "low", "value": 1, "id": "a"},
        {"group": "high", "value": 3, "id": "b"},
    ]
    assert aggregate_rows(rows, "distinct_count", value_field="id") == 2
    assert [row["group"] for row in aggregate_rows(rows, "ranking", value_field="value", group_by=["group"], ranking_order="asc")] == ["low", "high", "missing"]
    assert [row["group"] for row in aggregate_rows(rows, "ranking", value_field="value", group_by=["group"], ranking_order="desc")] == ["high", "low", "missing"]
    with pytest.raises(ValueError, match="explicit numerator and denominator"):
        aggregate_rows(rows, "share", numerator=1)
    assert aggregate_rows(rows, "rate", numerator=2, denominator=4)["value"] == 0.5

    period_rows = [{"period": "Q2", "amount": 4}, {"period": "Q1", "amount": 2}]
    comparison = aggregate_rows(period_rows, "period_comparison", period_field="period", period_order=("Q1", "Q2"), value_field="amount")
    assert list(comparison["periods"]) == ["Q1", "Q2"]
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_rows(period_rows, "period_comparison", period_field="period", period_order=("Q1", "Q1"), value_field="amount")
    with pytest.raises(ValueError, match="unknown"):
        aggregate_rows(period_rows + [{"period": "Q3", "amount": 1}], "period_comparison", period_field="period", period_order=("Q1", "Q2"), value_field="amount")
    with pytest.raises(ValueError, match="missing periods"):
        aggregate_rows([{"period": "Q1", "amount": 2}], "period_comparison", period_field="period", period_order=("Q1", "Q2"), value_field="amount")


def test_population_reconciliation_reports_exact_id_violations() -> None:
    ledger = PopulationLedger(
        ["a", "b", "c"],
        eligible=["a", "outside"],
        excluded={"reason-a": ["b"], "reason-b": ["b"]},
        unresolved=["a", "c"],
    )
    result = ledger.reconcile()
    assert result["reconciles"] is False
    assert result["overlaps"] == ["a"]
    assert result["out_of_base"] == ["outside"]
    assert result["missing_base"] == []
    assert result["duplicate_exclusion_reasons"] == {"b": ["reason-a", "reason-b"]}
    with pytest.raises(TypeError, match="iterable of IDs"):
        PopulationLedger(3)


def test_date_formats_are_explicit_and_report_attempts() -> None:
    parsed = parse_date("31/12/2024", formats=("%d/%m/%Y",))
    assert parsed.value == "2024-12-31"
    assert parsed.attempts == ("ISO-8601", "%d/%m/%Y")
    assert parsed.failures and parsed.failures[0].startswith("ISO-8601:")
    normalized = normalize_rows(
        [{"start": "31/12/2024", "end": "2024-12-31", "raw": "x"}],
        fields={"start": "date", "end": "date"},
        date_formats={"start": ("%d/%m/%Y",)},
        return_metadata=True,
    )
    assert normalized["rows"][0]["start_normalized"] == "2024-12-31"
    assert normalized["rows"][0]["end_normalized"] == "2024-12-31"
    assert normalized["raw_preserved"] is True


def test_source_boundaries_xls_rejection_json_cap_and_catalog_contract(tmp_path: Path) -> None:
    ordinary_json = tmp_path / "records.json"
    ordinary_json.write_text(json.dumps([{"id": "x" * 100}]), encoding="utf-8")
    with pytest.raises(ValueError, match="materialization boundary"):
        read_rows(ordinary_json, max_json_bytes=10)

    xls = tmp_path / "legacy.xls"
    xls.write_bytes(b"not an xls reader fixture")
    with pytest.raises(ValueError, match="unsupported source format: xls"):
        read_rows(xls)
    with pytest.raises(ValueError, match="unsupported source format: xls"):
        discover(xls)

    preview_descriptor = next(item for item in descriptors() if item.capability_id == "sources.preview")
    assert "parquet" in preview_descriptor.metadata["bounded_formats"]
    assert "xls" in preview_descriptor.metadata["unsupported_formats"]
    assert "max_bytes" in preview_descriptor.to_json() or "max_json" in preview_descriptor.to_json()


def test_relationship_temporal_stats_cover_all_pairs_but_sample_is_bounded() -> None:
    left = [
        {"key": "a", "when": "31/12/2024"},
        {"key": "a", "when": "01/01/2025"},
    ]
    right = [
        {"key": "a", "when": "2025-01-01"},
        {"key": "a", "when": "2025-01-02"},
    ]
    result = measure_relationship(
        left,
        right,
        left_key="key",
        left_time_field="when",
        right_time_field="when",
        left_date_formats=("%d/%m/%Y",),
        sample_limit=1,
    )
    assert result["total_matched_pair_count"] == 4
    assert result["sampled_pair_count"] == 1
    assert result["sample_coverage"] == 0.25
    assert result["temporal"]["temporal_scope"] == "full"
    assert result["temporal"]["pairs_with_both_dates"] == 4
    assert result["temporal"]["delta_seconds_min"] == 0
    assert result["temporal"]["delta_seconds_max"] == 172800
