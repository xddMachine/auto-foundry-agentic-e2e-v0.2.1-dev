"""Generic typed aggregations; no domain-specific metric recipes.

The public API intentionally stays small.  In particular, a distinct count is
an explicit operation and shares/rates are supplied as explicit numerator and
denominator values; an arbitrary value column is never interpreted as a
boolean indicator.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from .contracts import AggregationSpec


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip().replace(",", "")
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _groups(rows: list[Mapping[str, Any]], fields: Sequence[str]) -> dict[tuple[Any, ...], list[Mapping[str, Any]]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(field) for field in fields)].append(row)
    return groups


def _distinct_key(value: Any) -> tuple[str, str]:
    """Return a deterministic key for hashable and unhashable values alike."""

    return (type(value).__name__, repr(value))


def _metric(rows: list[Mapping[str, Any]], operation: str, value_field: str | None) -> Any:
    if operation == "count":
        return len(rows)
    values = [row.get(value_field) for row in rows] if value_field else []
    if operation == "distinct_count":
        return len({_distinct_key(value) for value in values if value is not None})
    numeric = [number for value in values if (number := _number(value)) is not None]
    if operation == "sum":
        # A group whose value is entirely missing is missing, not a numeric
        # zero.  This distinction lets ranking place such groups last in both
        # directions while retaining the normal sum for actual zero values.
        return sum(numeric) if numeric else None
    if operation == "average":
        return mean(numeric) if numeric else None
    if operation == "min":
        return min(numeric) if numeric else None
    if operation == "max":
        return max(numeric) if numeric else None
    if operation == "distribution":
        return dict(Counter(repr(value) for value in values if value is not None))
    raise ValueError(f"unsupported aggregation operation: {operation}")


def _rank(result: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    order = str(order).lower()
    if order not in {"asc", "desc"}:
        raise ValueError("ranking_order must be 'asc' or 'desc'")

    # Sort present values independently so None is always appended.  The
    # fallback makes mixed, non-comparable metric values deterministic while
    # retaining insertion order for ties.
    present = [item for item in result if item["value"] is not None]
    missing = [item for item in result if item["value"] is None]
    try:
        present.sort(key=lambda item: item["value"], reverse=order == "desc")
    except TypeError:
        present.sort(
            key=lambda item: (type(item["value"]).__name__, repr(item["value"])),
            reverse=order == "desc",
        )
    return present + missing


def _explicit_ratio(numerator: Any, denominator: Any) -> dict[str, Any]:
    if numerator is None or denominator is None:
        raise ValueError("share/rate requires explicit numerator and denominator")
    numerator_number = _number(numerator)
    denominator_number = _number(denominator)
    if numerator_number is None or denominator_number is None:
        raise ValueError("share/rate numerator and denominator must be numeric")
    return {
        "value": numerator_number / denominator_number if denominator_number else None,
        "numerator": numerator_number,
        "denominator": denominator_number,
    }


def aggregate_rows(
    rows: Iterable[Mapping[str, Any]],
    operation: AggregationSpec | str,
    *,
    value_field: str | None = None,
    group_by: Sequence[str] = (),
    period_field: str | None = None,
    period_order: Sequence[Any] | None = None,
    currency_field: str | None = None,
    numerator: float | int | None = None,
    denominator: float | int | None = None,
    limit: int | None = None,
    ranking_order: str = "desc",
    parameters: Mapping[str, Any] | None = None,
) -> Any:
    """Run a deterministic aggregation over materialized local rows.

    Materializing the caller's iterable is deliberate: this function returns
    grouped/scalar values, not a streaming result.  Source readers enforce
    their own bounded input/materialization contracts before this layer.
    """

    materialized = [dict(row) for row in rows]
    if isinstance(operation, AggregationSpec):
        spec = operation
        operation = spec.operation
        # A typed spec is the source of operation defaults.  Explicit function
        # arguments still provide a useful override for generic string calls.
        value_field = spec.value_field if value_field is None else value_field
        group_by = spec.group_by if not group_by else tuple(group_by)
        period_field = spec.period_field if period_field is None else period_field
        period_order = spec.period_order if period_order is None else period_order
        currency_field = spec.currency_field if currency_field is None else currency_field
        limit = spec.limit if limit is None else limit
        ranking_order = spec.ranking_order if ranking_order == "desc" else ranking_order
        numerator = spec.numerator if numerator is None else numerator
        denominator = spec.denominator if denominator is None else denominator
        parameters = {**dict(spec.parameters), **dict(parameters or {})}
    else:
        operation = str(operation)
        parameters = dict(parameters or {})

    value_field = value_field or parameters.get("value_field")
    group_by = tuple(group_by or parameters.get("group_by", ()))
    period_field = period_field or parameters.get("period_field")
    if period_order is None:
        period_order = parameters.get("period_order")
    if numerator is None:
        numerator = parameters.get("numerator")
    if denominator is None:
        denominator = parameters.get("denominator")
    if limit is not None and (isinstance(limit, bool) or limit < 0):
        raise ValueError("aggregation limit cannot be negative")

    if operation in {"share", "rate"}:
        # No row-derived fallback is permitted.  This is intentionally checked
        # before any other operation-specific handling.
        return _explicit_ratio(numerator, denominator)

    if operation == "currency_totals" or (operation == "sum" and currency_field):
        if not currency_field:
            raise ValueError("currency_field is required for currency-separated totals")
        if not value_field:
            raise ValueError("value_field is required for currency-separated totals")
        totals: dict[str, float | int] = defaultdict(float)
        for row in materialized:
            currency = row.get(currency_field)
            number = _number(row.get(value_field))
            if currency is not None and number is not None:
                totals[str(currency)] += number
        return dict(sorted(totals.items()))

    if operation in {"group", "count", "distinct_count", "sum", "average", "min", "max", "distribution"} and group_by:
        grouped = _groups(materialized, group_by)
        result = []
        for key, subset in grouped.items():
            value = _metric(subset, "count" if operation == "group" else operation, value_field)
            record = {field: value_part for field, value_part in zip(group_by, key)}
            record["value"] = value
            result.append(record)
        return result

    if operation == "ranking":
        group_fields = tuple(group_by or parameters.get("group_by", ()))
        if not group_fields:
            raise ValueError("ranking requires group_by")
        metric_operation = str(parameters.get("metric", "sum"))
        if metric_operation in {"share", "rate"}:
            raise ValueError("ranking metric share/rate requires explicit ratio values")
        grouped = _groups(materialized, group_fields)
        result = []
        for key, subset in grouped.items():
            record = {field: value_part for field, value_part in zip(group_fields, key)}
            record["value"] = _metric(subset, metric_operation, value_field)
            result.append(record)
        ranked = _rank(result, ranking_order)
        return ranked[:limit] if limit is not None else ranked

    if operation == "period_comparison":
        if not period_field:
            raise ValueError("period_comparison requires period_field")
        if period_order is None:
            raise ValueError("period_comparison requires explicit period_order")
        ordered = tuple(str(value) for value in period_order)
        if not ordered:
            raise ValueError("period_comparison requires explicit period_order")
        if len(set(ordered)) != len(ordered):
            raise ValueError("period_order contains duplicate periods")

        observed: list[str] = []
        missing_rows = 0
        for row in materialized:
            raw = row.get(period_field)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                missing_rows += 1
            else:
                observed.append(str(raw))
        if missing_rows:
            raise ValueError("period_comparison contains rows with missing period values")
        expected = set(ordered)
        unknown = sorted(set(observed) - expected)
        if unknown:
            raise ValueError(f"period_comparison contains unknown periods: {unknown}")
        missing = [period for period in ordered if period not in set(observed)]
        if missing:
            raise ValueError(f"period_comparison is missing periods: {missing}")

        metric_operation = str(parameters.get("metric", "sum"))
        grouped = _groups(materialized, (period_field,))
        values_by_period: dict[str, Any] = {}
        for period in ordered:
            subset = grouped.get((period,), [])
            # String-normalized period values are canonical for the explicit
            # order.  Handle non-string source values (e.g. integer years).
            if not subset:
                subset = [row for row in materialized if str(row.get(period_field)) == period]
            values_by_period[period] = _metric(subset, metric_operation, value_field)
        comparisons = []
        for previous, current in zip(ordered, ordered[1:]):
            old, new = values_by_period[previous], values_by_period[current]
            comparisons.append(
                {
                    "from": previous,
                    "to": current,
                    "from_value": old,
                    "to_value": new,
                    "delta": new - old if old is not None and new is not None else None,
                }
            )
        return {"periods": values_by_period, "comparisons": comparisons}

    return _metric(materialized, operation, value_field)


compute = aggregate_rows

aggregate = aggregate_rows

__all__ = ["aggregate", "aggregate_rows", "compute"]
