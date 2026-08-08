"""Generic typed aggregations; no domain-specific metric recipes."""

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


def _metric(rows: list[Mapping[str, Any]], operation: str, value_field: str | None, *, distinct: bool = False) -> Any:
    if operation == "count":
        return len(rows)
    values = [row.get(value_field) for row in rows] if value_field else []
    if operation == "distinct_count":
        return len({repr(v) for v in values if v is not None})
    numeric = [n for value in values if (n := _number(value)) is not None]
    if operation in {"sum", "share", "rate"}:
        return sum(numeric)
    if operation == "average":
        return mean(numeric) if numeric else None
    if operation == "min":
        return min(numeric) if numeric else None
    if operation == "max":
        return max(numeric) if numeric else None
    if operation == "distribution":
        return dict(Counter(repr(v) for v in values if v is not None))
    raise ValueError(f"unsupported aggregation operation: {operation}")


def aggregate_rows(
    rows: Iterable[Mapping[str, Any]],
    operation: AggregationSpec | str,
    *,
    value_field: str | None = None,
    group_by: Sequence[str] = (),
    distinct: bool = False,
    period_field: str | None = None,
    currency_field: str | None = None,
    numerator: float | int | None = None,
    denominator: float | int | None = None,
    limit: int | None = None,
    ranking_order: str = "desc",
    parameters: Mapping[str, Any] | None = None,
) -> Any:
    """Run a generic aggregation over materialized local rows."""

    materialized = [dict(row) for row in rows]
    if isinstance(operation, AggregationSpec):
        spec = operation
        operation, value_field, group_by, distinct = spec.operation, spec.value_field, spec.group_by, spec.distinct
        period_field, currency_field, limit, ranking_order = spec.period_field, spec.currency_field, spec.limit, spec.ranking_order
        parameters = {**dict(spec.parameters), **dict(parameters or {})}
    else:
        operation = str(operation)
        parameters = dict(parameters or {})
    value_field = value_field or parameters.get("value_field")
    group_by = tuple(group_by or parameters.get("group_by", ()))
    if operation in {"share", "rate"} and numerator is None:
        numerator = parameters.get("numerator")
    if operation in {"share", "rate"} and denominator is None:
        denominator = parameters.get("denominator")
    if operation in {"share", "rate"} and numerator is not None and denominator is not None:
        return {"value": numerator / denominator if denominator else None, "numerator": numerator, "denominator": denominator}
    if operation in {"share", "rate"}:
        numerator = _metric(materialized, "sum", value_field)
        denominator = parameters.get("denominator", len(materialized))
        return {"value": numerator / denominator if denominator else None, "numerator": numerator, "denominator": denominator}
    if operation == "currency_totals" or (operation == "sum" and currency_field):
        if not currency_field:
            raise ValueError("currency_field is required for currency-separated totals")
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
            value = _metric(subset, "count" if operation == "group" else operation, value_field, distinct=distinct)
            record = {field: value_part for field, value_part in zip(group_by, key)}
            record["value"] = value
            result.append(record)
        return result
    if operation == "ranking":
        group_fields = tuple(group_by or parameters.get("group_by", ()))
        if not group_fields:
            raise ValueError("ranking requires group_by")
        metric_operation = str(parameters.get("metric", "sum"))
        grouped = _groups(materialized, group_fields)
        result = []
        for key, subset in grouped.items():
            record = {field: value_part for field, value_part in zip(group_fields, key)}
            record["value"] = _metric(subset, metric_operation, value_field)
            result.append(record)
        result.sort(key=lambda item: (item["value"] is None, item["value"]), reverse=ranking_order.lower() == "desc")
        return result[:limit] if limit is not None else result
    if operation == "period_comparison":
        period_field = period_field or parameters.get("period_field")
        if not period_field:
            raise ValueError("period_comparison requires period_field")
        grouped = _groups(materialized, (period_field,))
        values = {str(key[0]): _metric(subset, str(parameters.get("metric", "sum")), value_field) for key, subset in grouped.items()}
        ordered = sorted(values)
        comparisons = []
        for previous, current in zip(ordered, ordered[1:]):
            old, new = values[previous], values[current]
            comparisons.append({"from": previous, "to": current, "from_value": old, "to_value": new, "delta": new - old if old is not None and new is not None else None})
        return {"periods": values, "comparisons": comparisons}
    return _metric(materialized, operation, value_field, distinct=distinct)


compute = aggregate_rows

aggregate = aggregate_rows

__all__ = ["aggregate", "aggregate_rows", "compute"]
