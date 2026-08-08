"""Deterministic relationship diagnostics without business interpretation."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .normalization import parse_date


def _date_formats(
    formats: Sequence[str] | Mapping[str, Sequence[str]] | str | None,
    side: str,
) -> Sequence[str] | str | None:
    if isinstance(formats, Mapping):
        return formats.get(side) or formats.get(f"{side}_time") or formats.get(f"{side}_time_field")
    return formats


def measure_relationship(
    left_rows: Iterable[Mapping[str, Any]],
    right_rows: Iterable[Mapping[str, Any]],
    *,
    left_key: str,
    right_key: str | None = None,
    left_time_field: str | None = None,
    right_time_field: str | None = None,
    date_formats: Sequence[str] | Mapping[str, Sequence[str]] | str | None = None,
    left_date_formats: Sequence[str] | str | None = None,
    right_date_formats: Sequence[str] | str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Measure overlap, coverage, cardinality, fanout and temporal alignment.

    Key pairs are streamed from the materialized sides.  Only the requested
    bounded ``sample_pairs`` are retained; temporal statistics are accumulated
    over every matched pair and are therefore marked ``temporal_scope='full'``.
    """

    if sample_limit < 0:
        raise ValueError("sample_limit cannot be negative")
    left = [dict(row) for row in left_rows]
    right = [dict(row) for row in right_rows]
    right_key = right_key or left_key
    left_keys = [row.get(left_key) for row in left]
    right_keys = [row.get(right_key) for row in right]
    left_counts = Counter(value for value in left_keys if value is not None)
    right_counts = Counter(value for value in right_keys if value is not None)
    left_set, right_set = set(left_counts), set(right_counts)
    overlap = left_set & right_set
    pair_counts = {key: (left_counts[key], right_counts[key]) for key in overlap}
    if all(left_count == 1 and right_count == 1 for left_count, right_count in pair_counts.values()):
        cardinality = "one_to_one"
    elif all(left_count == 1 for left_count, _ in pair_counts.values()):
        cardinality = "one_to_many"
    elif all(right_count == 1 for _, right_count in pair_counts.values()):
        cardinality = "many_to_one"
    elif pair_counts:
        cardinality = "many_to_many"
    else:
        cardinality = "no_overlap"

    pairs: list[dict[str, Any]] = []
    right_by_key: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in right:
        key = row.get(right_key)
        if key is not None:
            right_by_key[key].append(row)

    temporal_enabled = bool(left_time_field and right_time_field)
    left_formats = left_date_formats if left_date_formats is not None else _date_formats(date_formats, "left")
    right_formats = right_date_formats if right_date_formats is not None else _date_formats(date_formats, "right")
    deltas: list[float] = []
    invalid_dates = 0
    left_date_failures = 0
    right_date_failures = 0
    total_matched_pair_count = 0
    for row in left:
        key = row.get(left_key)
        for target in right_by_key.get(key, ()):
            total_matched_pair_count += 1
            if len(pairs) < sample_limit:
                pairs.append({"key": key, "left": row, "right": target})
            if not temporal_enabled:
                continue
            left_result = parse_date(row.get(left_time_field), formats=left_formats)
            right_result = parse_date(target.get(right_time_field), formats=right_formats)
            if not left_result.ok:
                left_date_failures += 1
            if not right_result.ok:
                right_date_failures += 1
            if not left_result.ok or not right_result.ok:
                invalid_dates += 1
                continue
            try:
                left_datetime = datetime.fromisoformat(left_result.value)
                right_datetime = datetime.fromisoformat(right_result.value)
            except (TypeError, ValueError):
                invalid_dates += 1
                continue
            deltas.append((right_datetime - left_datetime).total_seconds())

    sampled_pair_count = len(pairs)
    sample_coverage = sampled_pair_count / total_matched_pair_count if total_matched_pair_count else 0.0
    temporal: dict[str, Any] = {
        "available": temporal_enabled,
        "pairs_with_both_dates": len(deltas),
        "invalid_dates": invalid_dates,
        "delta_seconds_min": min(deltas) if deltas else None,
        "delta_seconds_max": max(deltas) if deltas else None,
        "delta_seconds_mean": sum(deltas) / len(deltas) if deltas else None,
        "total_matched_pair_count": total_matched_pair_count,
        "sampled_pair_count": sampled_pair_count,
        "sample_coverage": sample_coverage,
        "temporal_scope": "full" if temporal_enabled else "unavailable",
        "date_formats": {
            "left": list(left_formats) if left_formats and not isinstance(left_formats, str) else left_formats,
            "right": list(right_formats) if right_formats and not isinstance(right_formats, str) else right_formats,
        },
        "date_parse_failures": {"left": left_date_failures, "right": right_date_failures},
    }
    left_nonnull = sum(value is not None for value in left_keys)
    right_nonnull = sum(value is not None for value in right_keys)
    return {
        "left_rows": len(left),
        "right_rows": len(right),
        "left_key": left_key,
        "right_key": right_key,
        "left_unique": all(count == 1 for count in left_counts.values()),
        "right_unique": all(count == 1 for count in right_counts.values()),
        "left_duplicate_keys": {str(key): count for key, count in left_counts.items() if count > 1},
        "right_duplicate_keys": {str(key): count for key, count in right_counts.items() if count > 1},
        "overlap_count": len(overlap),
        "overlap_keys": [str(value) for value in sorted(overlap, key=str)],
        "left_coverage": len(overlap) / len(left_set) if left_set else 1.0,
        "right_coverage": len(overlap) / len(right_set) if right_set else 1.0,
        "left_row_coverage": sum(left_counts[key] for key in overlap) / left_nonnull if left_nonnull else 1.0,
        "right_row_coverage": sum(right_counts[key] for key in overlap) / right_nonnull if right_nonnull else 1.0,
        "left_unmatched": sum(count for key, count in left_counts.items() if key not in overlap),
        "right_unmatched": sum(count for key, count in right_counts.items() if key not in overlap),
        "fanout_max": max((right_counts[key] for key in overlap), default=0),
        "fanout_mean": sum(right_counts[key] for key in overlap) / len(overlap) if overlap else 0.0,
        "cardinality": cardinality,
        "sample_pairs": pairs,
        "total_matched_pair_count": total_matched_pair_count,
        "sampled_pair_count": sampled_pair_count,
        "sample_coverage": sample_coverage,
        "temporal_scope": "full" if temporal_enabled else "unavailable",
        "temporal": temporal,
        "limitations": ("Diagnostics measure key relationship only; an agent decides whether it is semantically valid.",),
    }


measure = measure_relationship

__all__ = ["measure", "measure_relationship"]
