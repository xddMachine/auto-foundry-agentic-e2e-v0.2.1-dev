"""Bounded, question-supporting data profiling."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import math
from statistics import mean, median
from typing import Any, Iterable, Mapping

from .sources import read_rows
from .contracts import DataAssetRef, TableRef


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _physical_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (datetime, date)):
        return "datetime"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, (dict, list, tuple)):
        return "object"
    return "string"


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        try:
            parsed = float(cleaned)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return None
    return None


def profile_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    sample_limit: int = 1000,
    frequency_limit: int = 20,
) -> dict[str, Any]:
    """Profile rows while retaining only bounded samples and frequencies."""

    if sample_limit < 0 or frequency_limit < 0:
        raise ValueError("sample_limit and frequency_limit cannot be negative")
    materialized: list[dict[str, Any]] = []
    for row in rows:
        if len(materialized) < sample_limit:
            materialized.append(dict(row))
    # ``rows`` may be a list, in which case use its complete count.  For an
    # iterator we have intentionally only observed the bounded sample.
    total_rows = len(rows) if isinstance(rows, (list, tuple)) else len(materialized)
    columns: list[str] = []
    for row in materialized:
        for name in row:
            if name not in columns:
                columns.append(str(name))
    column_profiles: dict[str, Any] = {}
    duplicate_keys: list[dict[str, Any]] = []
    for name in columns:
        values = [row.get(name) for row in materialized]
        nonblank = [v for v in values if not _blank(v)]
        types = Counter(_physical_type(v) for v in nonblank)
        numeric_values = [n for v in nonblank if (n := _numeric(v)) is not None]
        frequencies = Counter(str(v) for v in nonblank)
        dates = []
        for value in nonblank:
            if isinstance(value, (date, datetime)):
                dates.append(value.isoformat())
        column_profiles[name] = {
            "physical_types": dict(types),
            "inferred_type": types.most_common(1)[0][0] if types else "null",
            "null_count": sum(v is None for v in values),
            "blank_count": sum(isinstance(v, str) and not v.strip() for v in values),
            "observed_count": len(values),
            "distinct_count": len({json_key(v) for v in nonblank}),
            "sample_values": [json_key(v) for v in nonblank[: min(10, len(nonblank))]],
            "frequencies": dict(frequencies.most_common(frequency_limit)),
            "numeric": {
                "count": len(numeric_values),
                "min": min(numeric_values) if numeric_values else None,
                "max": max(numeric_values) if numeric_values else None,
                "mean": mean(numeric_values) if numeric_values else None,
                "median": median(numeric_values) if numeric_values else None,
            },
            "date_coverage_candidates": {"min": min(dates) if dates else None, "max": max(dates) if dates else None},
            "suspicious_mixed_type": len(types) > 1,
        }
    # A bounded duplicate-key hint is intentionally conservative: only exact
    # duplicate complete rows are reported, never treated as a merge decision.
    row_keys = Counter(json_key(row) for row in materialized)
    duplicate_keys = [{"row": key, "count": count} for key, count in row_keys.items() if count > 1][:frequency_limit]
    return {
        "row_count": total_rows,
        "sampled_rows": len(materialized),
        "bounded": total_rows > len(materialized),
        "columns": column_profiles,
        "duplicate_row_candidates": duplicate_keys,
    }


def json_key(value: Any) -> str:
    """Stable scalar/record key used only for profiling comparisons."""

    import json
    from dataclasses import asdict, is_dataclass
    if is_dataclass(value):
        value = asdict(value)
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError:
        return repr(value)


def profile_source(
    source: DataAssetRef | TableRef | str,
    *,
    sample_limit: int = 1000,
    frequency_limit: int = 20,
    allowed_roots=None,
) -> dict[str, Any]:
    rows = read_rows(source, limit=sample_limit, allowed_roots=allowed_roots)
    return profile_rows(rows, sample_limit=sample_limit, frequency_limit=frequency_limit)


profile = profile_source

__all__ = ["profile", "profile_rows", "profile_source"]
