"""Deterministic relationship diagnostics without business interpretation."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .normalization import parse_date


def _values(rows: Iterable[Mapping[str, Any]], key: str) -> list[Any]:
    return [row.get(key) for row in rows]


def measure_relationship(
    left_rows: Iterable[Mapping[str, Any]],
    right_rows: Iterable[Mapping[str, Any]],
    *,
    left_key: str,
    right_key: str | None = None,
    left_time_field: str | None = None,
    right_time_field: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Measure overlap, coverage, cardinality, fanout and temporal alignment."""

    left = [dict(row) for row in left_rows]
    right = [dict(row) for row in right_rows]
    right_key = right_key or left_key
    left_keys = [row.get(left_key) for row in left]
    right_keys = [row.get(right_key) for row in right]
    left_counts = Counter(v for v in left_keys if v is not None)
    right_counts = Counter(v for v in right_keys if v is not None)
    left_set, right_set = set(left_counts), set(right_counts)
    overlap = left_set & right_set
    left_unique = sum(v == 1 for v in left_counts.values()) == len(left_counts)
    right_unique = sum(v == 1 for v in right_counts.values()) == len(right_counts)
    pair_counts = {key: (left_counts[key], right_counts[key]) for key in overlap}
    if all(l == 1 and r == 1 for l, r in pair_counts.values()):
        cardinality = "one_to_one"
    elif all(l == 1 for l, r in pair_counts.values()):
        cardinality = "one_to_many"
    elif all(r == 1 for l, r in pair_counts.values()):
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
    for row in left:
        key = row.get(left_key)
        for target in right_by_key.get(key, ()):
            if len(pairs) < sample_limit:
                pairs.append({"key": key, "left": row, "right": target})
    temporal: dict[str, Any] = {"available": False, "pairs_with_both_dates": 0, "invalid_dates": 0}
    if left_time_field and right_time_field:
        deltas: list[float] = []
        invalid = 0
        for pair in pairs:
            lp = parse_date(pair["left"].get(left_time_field))
            rp = parse_date(pair["right"].get(right_time_field))
            if not lp.ok or not rp.ok:
                invalid += 1
                continue
            try:
                ldt = datetime.fromisoformat(lp.value)
                rdt = datetime.fromisoformat(rp.value)
            except (TypeError, ValueError):
                invalid += 1
                continue
            deltas.append((rdt - ldt).total_seconds())
        temporal = {
            "available": True,
            "pairs_with_both_dates": len(deltas),
            "invalid_dates": invalid,
            "delta_seconds_min": min(deltas) if deltas else None,
            "delta_seconds_max": max(deltas) if deltas else None,
            "delta_seconds_mean": sum(deltas) / len(deltas) if deltas else None,
        }
    left_nonnull = sum(v is not None for v in left_keys)
    right_nonnull = sum(v is not None for v in right_keys)
    return {
        "left_rows": len(left),
        "right_rows": len(right),
        "left_key": left_key,
        "right_key": right_key,
        "left_unique": left_unique,
        "right_unique": right_unique,
        "left_duplicate_keys": {str(k): n for k, n in left_counts.items() if n > 1},
        "right_duplicate_keys": {str(k): n for k, n in right_counts.items() if n > 1},
        "overlap_count": len(overlap),
        "overlap_keys": [str(v) for v in sorted(overlap, key=str)],
        "left_coverage": len(overlap) / len(left_set) if left_set else 1.0,
        "right_coverage": len(overlap) / len(right_set) if right_set else 1.0,
        "left_row_coverage": sum(left_counts[k] for k in overlap) / left_nonnull if left_nonnull else 1.0,
        "right_row_coverage": sum(right_counts[k] for k in overlap) / right_nonnull if right_nonnull else 1.0,
        "left_unmatched": sum(n for k, n in left_counts.items() if k not in overlap),
        "right_unmatched": sum(n for k, n in right_counts.items() if k not in overlap),
        "fanout_max": max((right_counts[k] for k in overlap), default=0),
        "fanout_mean": sum(right_counts[k] for k in overlap) / len(overlap) if overlap else 0.0,
        "cardinality": cardinality,
        "sample_pairs": pairs,
        "temporal": temporal,
        "limitations": ("Diagnostics measure key relationship only; an agent decides whether it is semantically valid.",),
    }


measure = measure_relationship

__all__ = ["measure", "measure_relationship"]
