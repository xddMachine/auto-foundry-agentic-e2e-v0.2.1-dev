#!/usr/bin/env python3
"""Small, portable runtime helpers for the canonical V2 dashboard blueprint.

The Product Agent owns the blueprint bytes; this module only normalises the
already reviewed fixture/chart-map values into a source-bound artifact and
reports which registry recipes can honestly consume the supplied fields.  It
never aggregates rows, calculates measures, or reads a source.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


# V2 is the single business contract.  Keep the historical identifier so
# existing receipts and lifecycle references remain byte-addressable without
# retaining a V1 parser or fallback.
BLUEPRINT_SCHEMA = "dashboard.business_presentation_plan.v2"
BLUEPRINT_KIND = "dashboard_blueprint"
BLUEPRINT_STATUS_PREVIEW = "Preview"

_EXACT_FIELD_KEYS = (
    "id",
    "title",
    "label",
    "chart_intent",
    "encodings",
    "type",
    "kind",
    "value",
    "display_value",
    "manager_display_value",
    "bars",
    "categories",
    "segments",
    "points",
    "series",
    "rows",
    "stages",
    "bins",
    "boxes",
    "steps",
    "data",
    "values",
    "tiles",
    "cells",
    "population",
    "denominator",
    "denominator_value",
    "denominator_label",
    "unit",
    "period",
    "grain",
    "dimensions",
    "measures",
    "time",
    "coverage",
    "limitations",
    "filters",
    "drilldown",
    "empty_state",
    "columns",
    "manager_columns",
    "presentation_role",
    "presentation_tier",
    "domain_id",
    "requirement_id",
)

# Registry families with an executable renderer.  The rest remain visible as
# explicit unavailable recipes in inventory rather than being silently drawn
# as a different chart.
SUPPORTED_FAMILIES = frozenset(
    {
        "kpi_card",
        "horizontal_bar",
        "column",
        "grouped_bar",
        "stacked_bar",
        "stacked_area",
        "line_area_slope",
        "diverging_bar",
        "waffle",
        "funnel",
        "histogram",
        "histogram_box",
        "box_plot",
        "pareto",
        "waterfall",
        "heatmap_matrix",
        "scatter_bubble",
        "lollipop",
        "donut_pie",
        "metric_grid",
        "table",
    }
)

# Product Agent choices are deliberately bounded.  Layout is presentation
# metadata, not a free-form CSS hook; the renderer owns the corresponding
# classes.  ``half`` is useful for a compact two-column decision view while
# the other values retain the existing dashboard layout vocabulary.
SUPPORTED_LAYOUTS = frozenset({"full", "wide", "half", "compact"})

# Renderer types are deliberately separate from registry recipe IDs.  A
# registry family can describe more than one honest renderer (notably the
# ``histogram_box`` and ``line_area_slope`` families), while the renderer must
# receive one exact type after Product Agent selection.  This is a closed set
# so a plan cannot smuggle arbitrary widget names into the runtime.
SUPPORTED_RENDERER_TYPES = frozenset(
    {
        "kpi",
        "bar",
        "column",
        "grouped_bar",
        "stacked_bar",
        "diverging_bar",
        "waffle",
        "funnel",
        "histogram",
        "box_plot",
        "pareto",
        "waterfall",
        "heatmap",
        "scatter",
        "line",
        "area",
        "stacked_area",
        "lollipop",
        "donut",
        "metric_grid",
        "table",
    }
)

# Widget type names used by the legacy fixture contract.  This set is shared
# by the assembler and renderer through ``is_partition_visual`` so the V2
# manager/audit partition cannot drift between planning and validation.
_LEGACY_VISUAL_WIDGET_TYPES = frozenset(
    {
        "kpi",
        "bar",
        "column",
        "lollipop",
        "donut",
        "metric_grid",
        "kpi_grid",
        "waffle",
        "diverging_bar",
        "stacked_composition",
        "line",
        "heatmap",
        "scatter",
        "leaderboard",
        "progress",
        "area",
        "stacked_area",
        "grouped_bar",
        "stacked_bar",
        "normalized_stacked_bar",
        "funnel",
        "histogram",
        "box_plot",
        "pareto",
        "waterfall",
        "pie",
    }
)

_RECIPE_RENDERER_TYPES: dict[str, tuple[str, ...]] = {
    "kpi_card": ("kpi",),
    "horizontal_bar": ("bar",),
    "column": ("column",),
    "grouped_bar": ("grouped_bar",),
    "stacked_bar": ("stacked_bar",),
    "diverging_bar": ("diverging_bar",),
    "waffle": ("waffle",),
    "funnel": ("funnel",),
    "histogram": ("histogram",),
    "box_plot": ("box_plot",),
    "pareto": ("pareto",),
    "waterfall": ("waterfall",),
    "heatmap_matrix": ("heatmap",),
    "scatter_bubble": ("scatter",),
    "lollipop": ("lollipop",),
    "donut_pie": ("donut",),
    "metric_grid": ("metric_grid",),
    "table": ("table",),
}


def default_layout_for_recipe(recipe_id: str) -> str:
    """Return a deterministic layout default for an initial plan.

    This is used only when a Product Agent has not selected layout metadata.
    It never depends on record IDs or values and therefore cannot make the
    same source bytes render differently across runs.
    """

    family = _text(recipe_id).strip()
    if family in {"kpi_card", "metric_grid"}:
        return "compact"
    if family in {"table", "heatmap_matrix", "scatter_bubble", "waterfall", "funnel"}:
        return "wide"
    return "full"


def renderer_types_for_recipe(
    recipe_id: str,
    widget: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return exact renderer types that can consume one recipe's shape.

    ``histogram_box`` is a registry umbrella: bins are rendered as a
    histogram and five-number summaries as a box plot.  ``line_area_slope``
    likewise permits a line or area view over the same ordered points.  All
    other families have one renderer type.  The helper never changes widget
    values or derives geometry; it only narrows already-supplied shape.
    """

    family = _text(recipe_id).strip()
    supplied = widget if isinstance(widget, Mapping) else {}
    if family == "histogram_box":
        bins = _rows_from(supplied, "bins", "rows")
        if bins and all(
            _has_value(row, "label", "bin", "name")
            and _has_value(row, "count", "display_value", "value")
            and _has_value(row, "size", "height", "share", "percent")
            for row in bins
        ):
            # Preserve registry order so defaults are deterministic when a
            # source happens to expose both containers.
            return ("histogram",)
        boxes = _rows_from(supplied, "boxes", "rows")
        keys = ("min", "q1", "median", "q3", "max")
        if boxes and all(_has_value(row, *keys) for row in boxes):
            return ("box_plot",)
        return ()
    if family == "line_area_slope":
        points = _rows_from(supplied, "points", "series", "rows")
        base = list(("line", "area"))
        # Stacked area uses the same registry family only when every row is an
        # explicit segment with bounded supplied geometry.  It is not offered
        # for ordinary point lists because the renderer would need series
        # reshaping that the runtime is forbidden to perform.
        if points and all(
            _has_value(row, "period", "time", "x", "label")
            and _has_value(row, "value", "y")
            for row in points
        ):
            return tuple(base)
        return ()
    if family == "stacked_bar":
        segments = _rows_from(supplied, "segments", "rows", "values", "data")
        if segments and all(
            _has_value(row, "label")
            and _has_value(row, "value", "display_value", "count", "amount", "measure")
            and _has_value(row, "size", "share", "percent")
            for row in segments
        ):
            # The committed registry uses one composition recipe for both
            # horizontal stacked bars and stacked-area strips.  The exact
            # renderer type remains a Product Agent choice, while the
            # selection is still bounded to these supplied segments.
            return ("stacked_bar", "stacked_area")
        return ()
    return _RECIPE_RENDERER_TYPES.get(family, ())


def default_renderer_type_for_recipe(
    recipe_id: str,
    widget: Mapping[str, Any] | None = None,
) -> str | None:
    """Return one deterministic renderer type for an eligible recipe."""

    values = renderer_types_for_recipe(recipe_id, widget)
    return values[0] if values else None


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def is_partition_visual(
    widget: Mapping[str, Any],
    chart: Mapping[str, Any],
) -> bool:
    """Return whether a widget belongs to the canonical V2 visual universe.

    Legacy chart families are visual only when their widget and chart types
    agree.  Table projections join the universe only when they carry an
    explicit reviewed fact, limited state, or accepted source-bound visual
    contract.  Ordinary ``status_table`` dashboard facts therefore remain
    audit records rather than becoming partition entries through metadata
    alone.
    """

    widget_type = _text(widget.get("type") or widget.get("kind")).strip().lower()
    chart_type = _text(chart.get("type")).strip().lower()
    if not widget_type or widget_type != chart_type:
        return False
    if chart_type in _LEGACY_VISUAL_WIDGET_TYPES:
        return True
    return (
        widget_type == chart_type == "table"
        and bool(
            widget.get("dashboard_fact")
            or widget.get("limited_empty_state")
            or widget.get("accepted_visual")
            or widget.get("source_bound")
        )
    )


def _registry_by_id(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    families = registry.get("families") if isinstance(registry, Mapping) else None
    if not isinstance(families, list):
        return {}
    return {
        _text(entry.get("id")): entry
        for entry in families
        if isinstance(entry, Mapping) and _text(entry.get("id")).strip()
    }


def _available_fields(widget: Mapping[str, Any], chart: Mapping[str, Any]) -> set[str]:
    fields: set[str] = set()
    for source in (widget, chart.get("fields_or_values_used")):
        if not isinstance(source, Mapping):
            continue
        fields.update(str(key) for key, value in source.items() if value not in (None, "", [], {}))
    return fields


def _default_recipe_id(eligible_ids: Sequence[str], current_family: Any = "") -> str:
    """Choose the deterministic initial recipe for one exact visual shape.

    The registry order is the committed semantic order.  If a reviewed
    declaration currently says ``table`` but also exposes exact chart
    geometry, surface the first executable non-table family so the Product
    Agent can make an informed business choice.  Keep an explicitly selected
    chart family, and use a table only when no chart family is eligible.
    """

    ordered = [str(value).strip() for value in eligible_ids if str(value).strip()]
    current = str(current_family or "").strip()
    richer = [value for value in ordered if value != "table"]
    if current in ordered and current != "table":
        return current
    if richer:
        return richer[0]
    if current in ordered:
        return current
    if "table" in ordered:
        return "table"
    return ordered[0] if ordered else "table"


def _recipe_requirements(family: str) -> tuple[str, ...]:
    """Expose the registry's coarse required-field summary.

    The summary is useful to Product Agent inventory consumers, but it is not
    the eligibility decision by itself.  ``_recipe_shape_reason`` below also
    inspects the exact supplied containers and semantics for each family.
    """

    if family in {"horizontal_bar", "column", "lollipop"}:
        return ("rows",)
    if family in {"grouped_bar", "stacked_bar", "stacked_area"}:
        return ("series",)
    if family == "diverging_bar":
        return ("bars",)
    if family == "funnel":
        return ("stages",)
    if family in {"histogram", "histogram_box"}:
        return ("bins",)
    if family == "box_plot":
        return ("boxes",)
    if family == "pareto":
        return ("rows", "cumulative_percent")
    if family == "waterfall":
        return ("steps",)
    if family == "heatmap_matrix":
        return ("cells",)
    if family == "scatter_bubble":
        return ("points",)
    if family in {"donut_pie", "waffle"}:
        return ("categories", "denominator")
    if family == "line_area_slope":
        return ("points", "time")
    if family == "kpi_card":
        return ("value",)
    if family == "metric_grid":
        return ("tiles",)
    if family == "table":
        return ("rows",)
    return ()


def _rows_from(widget: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    """Return one explicit row container without flattening or reshaping it."""

    for key in keys:
        value = widget.get(key)
        if isinstance(value, list) and value and all(isinstance(row, Mapping) for row in value):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _has_value(row: Mapping[str, Any], *keys: str) -> bool:
    return any(key in row and row.get(key) not in (None, "") for key in keys)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1]
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _all_numeric(rows: Sequence[Mapping[str, Any]], *keys: str) -> bool:
    return bool(rows) and all(_number(next((row.get(key) for key in keys if _has_value(row, key)), None)) is not None for row in rows)


def _composition_rows(widget: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return _rows_from(widget, "categories", "segments", "rows", "bars")


def _category_row_has_measure(
    row: Mapping[str, Any],
    *,
    geometry_keys: tuple[str, ...] = ("size", "width", "height", "share", "percent"),
) -> bool:
    """Return whether a category row carries direct or nested exact measures."""

    # Renderer geometry is a separate supplied field.  ``size`` alone is not
    # a displayed measure and therefore cannot make a bar/lollipop eligible.
    if not _has_value(row, "label"):
        return False
    if not _has_value(row, "value", "display_value", "count", "amount", "measure"):
        return False
    if not _has_value(row, *geometry_keys):
        return False
    return True


def _series_geometry_valid(row: Mapping[str, Any]) -> bool:
    series = row.get("series")
    if not isinstance(series, list) or not series:
        return False
    return all(
        isinstance(item, Mapping)
        and _has_value(item, "label", "name")
        # ``_fact_series_geometry_markup`` consumes literal value/display_value
        # and literal size.  Count/amount aliases or share/percent geometry
        # would be accepted here but then silently fall back in the renderer.
        and _has_value(item, "value", "display_value")
        and _has_value(item, "size")
        for item in series
    )


def _geometry_row_valid(
    row: Mapping[str, Any],
    *,
    signed: bool = False,
    geometry_keys: tuple[str, ...] = ("size", "width", "height", "share", "percent"),
) -> bool:
    if _series_geometry_valid(row):
        return True
    if not _category_row_has_measure(row, geometry_keys=geometry_keys):
        return False
    return not signed or _has_value(row, "signed_size")


def _composition_denominator(widget: Mapping[str, Any]) -> Any:
    return next(
        (
            widget.get(key)
            for key in ("denominator_value", "denominator", "population", "total")
            if _has_value(widget, key)
        ),
        None,
    )


def _shares_rows_reconcile(rows: Sequence[Mapping[str, Any]], widget: Mapping[str, Any]) -> bool:
    denominator = _composition_denominator(widget)
    if denominator is None:
        return False
    return _shares_reconcile(rows, denominator)


def _shares_reconcile(rows: Sequence[Mapping[str, Any]], denominator: Any) -> bool:
    """Validate explicit non-negative shares against an explicit denominator.

    Only already supplied values are checked.  No shares, totals, or display
    geometry are generated here.
    """

    denominator_number = _number(denominator)
    if denominator_number is None or denominator_number <= 0 or not (2 <= len(rows) <= 5):
        return False
    if not all(_has_value(row, "label", "name", "category") for row in rows):
        return False
    values: list[float] = []
    shares: list[float] = []
    for row in rows:
        value = _number(next((row.get(key) for key in ("value", "display_value", "count", "amount", "measure") if _has_value(row, key)), None))
        share_value = next((row.get(key) for key in ("share", "percent", "size") if _has_value(row, key)), None)
        # Donut/waffle renderers intentionally require a supplied percent
        # string.  Numeric sizes can be accepted by stacked/bar renderers but
        # would raise in ``_supplied_percent`` for composition views.
        if not isinstance(share_value, str) or not share_value.strip().endswith("%"):
            return False
        share = _number(share_value)
        if value is None or value < 0 or share is None or share < 0:
            return False
        values.append(value)
        shares.append(share)
    # A source may express percentages as fractions or as 0-100 values.  The
    # denominator and supplied shares must agree; tolerate only rounding.
    share_total = sum(shares)
    if share_total <= 1.000001:
        share_total *= 100.0
    if abs(share_total - 100.0) > 0.0001:
        return False
    value_total = sum(values)
    return abs(value_total - denominator_number) <= max(0.000001, abs(denominator_number) * 0.000001)


def _recipe_shape_reason(family: str, widget: Mapping[str, Any], chart: Mapping[str, Any]) -> str | None:
    """Return a conservative reason when exact supplied shape is ineligible."""

    rows = _rows_from(widget, "rows")
    bars = _rows_from(widget, "bars", "rows")
    if family in {"horizontal_bar", "column", "lollipop"}:
        if not rows and not bars:
            return "requires explicit category rows"
        candidates = rows or bars
        geometry_keys = (
            ("size", "height", "share", "percent")
            if family == "column"
            else ("size", "width", "share", "percent")
        )
        # Accepted tables retain their exact ``rows`` for the expandable
        # detail surface and may additionally expose source-bound ``bars``.
        # Prefer that explicit chart projection when it carries one scalar
        # category/value geometry; otherwise preserve the conservative table
        # result for unprojected rows.  Grouped series remain owned by the
        # grouped-bar recipe and are not silently flattened here.
        if bars and all(
            not isinstance(row.get("series"), list)
            and _category_row_has_measure(row, geometry_keys=geometry_keys)
            for row in bars
        ):
            candidates = bars
        if not all(_geometry_row_valid(row, geometry_keys=geometry_keys) for row in candidates):
            return "requires category labels, values, and supplied geometry"
        return None
    if family == "diverging_bar":
        signed_rows = _rows_from(widget, "bars", "values", "data")
        if not signed_rows or not all(
            _has_value(row, "label")
            and _has_value(row, "value", "display_value", "count", "amount", "measure")
            and _has_value(row, "signed_size")
            for row in signed_rows
        ):
            return "requires signed bars with supplied geometry"
        if not _all_numeric(signed_rows, "value", "display_value", "count", "amount", "measure"):
            return "requires numeric signed values"
        return None
    if family in {"donut_pie", "waffle"}:
        if not _has_value(widget, "denominator_label"):
            return "requires explicit denominator label"
        if not _shares_rows_reconcile(_composition_rows(widget), widget):
            return "requires reconciled non-negative categories, shares, and denominator"
        return None
    if family == "stacked_bar":
        segments = _rows_from(widget, "segments", "rows", "values", "data")
        if not segments:
            return "requires explicit segments"
        if not _has_value(widget, "unit"):
            return "requires same-unit metadata"
        if not _shares_rows_reconcile(segments, widget):
            return "requires reconciled segments and denominator"
        if not all(_geometry_row_valid(row, geometry_keys=("size", "share", "percent")) for row in segments):
            return "requires segment labels, values, and supplied geometry"
        return None
    if family == "grouped_bar":
        grouped = _rows_from(widget, "bars", "rows", "values", "data")
        if not grouped:
            return "requires explicit series rows"
        if not all(_series_geometry_valid(row) for row in grouped):
            return "requires category, series, supplied values, and geometry"
        if not _has_value(widget, "unit"):
            return "requires same-unit metadata"
        return None
    if family == "stacked_area":
        segments = _rows_from(widget, "segments", "rows", "values", "data")
        if not segments:
            return "requires explicit area segments"
        if not _has_value(widget, "unit"):
            return "requires same-unit metadata"
        if not _shares_rows_reconcile(segments, widget):
            return "requires reconciled area segments and denominator"
        return None if all(_geometry_row_valid(row, geometry_keys=("size", "share", "percent")) for row in segments) else "requires segment labels, values, and supplied geometry"
    if family == "funnel":
        # ``rows`` is accepted as the source-local alias when each row still
        # carries explicit stage labels/populations; no ordering is inferred
        # or changed by the inventory.
        stages = _rows_from(widget, "stages", "rows")
        if not stages:
            return "requires ordered stages"
        if not all(
            _has_value(row, "label")
            and _has_value(row, "value", "display_value", "count", "population", "amount")
            and _has_value(row, "size", "width", "share", "percent")
            for row in stages
        ):
            return "requires stage labels, populations, and supplied geometry"
        if not _all_numeric(stages, "value", "count", "population"):
            return "requires numeric stage populations"
        return None
    if family == "scatter_bubble":
        points = _rows_from(widget, "points")
        if len(points) < 2 or not all(_has_value(row, "x") and _has_value(row, "y") for row in points):
            return "requires at least two supplied x/y points"
        if not all(_number(row.get("x")) is not None and _number(row.get("y")) is not None for row in points):
            return "requires numeric x/y points"
        return None
    if family == "box_plot":
        boxes = _rows_from(widget, "boxes", "rows")
        keys = ("min", "q1", "median", "q3", "max")
        if not boxes or not all(_has_value(row, *keys) for row in boxes):
            return "requires supplied min/q1/median/q3/max boxes"
        if not all(all(_number(row.get(key)) is not None for key in keys) for row in boxes):
            return "requires numeric box statistics"
        return None
    if family in {"histogram", "histogram_box"}:
        bins = _rows_from(widget, "bins", "rows")
        if bins and all(
            _has_value(row, "label", "bin", "name")
            and _has_value(row, "count", "display_value", "value")
            and _has_value(row, "size", "height", "share", "percent")
            for row in bins
        ):
            return None
        if family == "histogram_box":
            boxes = _rows_from(widget, "boxes", "rows")
            keys = ("min", "q1", "median", "q3", "max")
            if boxes and all(_has_value(row, *keys) and all(_number(row.get(key)) is not None for key in keys) for row in boxes):
                return None
        return "requires labeled bins or supplied box statistics"
        return None
    if family == "pareto":
        pareto_rows = _rows_from(widget, "rows", "bars")
        if not pareto_rows or not all(
            _has_value(row, "label")
            and _has_value(row, "value", "display_value", "count", "amount", "measure")
            and _has_value(row, "size", "width", "share", "percent")
            and _has_value(row, "cumulative_size", "cumulative_percent", "cumulative_share")
            for row in pareto_rows
        ):
            return "requires supplied bar geometry and cumulative values"
        return None
    if family == "waterfall":
        steps = _rows_from(widget, "steps", "rows")
        if not steps or not all(
            _has_value(row, "label", "name")
            and _has_value(row, "start", "start_value", "start_size")
            and _has_value(row, "change", "delta", "change_value", "change_size")
            and _has_value(row, "end", "end_value", "end_size")
            for row in steps
        ):
            return "requires authoritative start/change/end steps"
        return None
    if family == "heatmap_matrix":
        cells = _rows_from(widget, "cells")
        if not cells or not all(
            _has_value(row, "row", "row_label", "y")
            and _has_value(row, "column", "column_label", "x")
            and _has_value(row, "value", "display_value", "intensity", "level")
            for row in cells
        ):
            return "requires row/column/value cells"
        return None
    if family == "line_area_slope":
        points = _rows_from(widget, "points", "series", "rows")
        if len(points) < 2 or not _has_value(widget, "time", "period", "grain"):
            return "requires ordered time points and grain metadata"
        if not all(_has_value(row, "period", "time", "x", "label") and _has_value(row, "value", "y") for row in points):
            return "requires period and supplied values"
        return None
    if family == "kpi_card":
        value = widget.get("value")
        if value in (None, "") or isinstance(value, (Mapping, list, tuple)):
            return "requires one reviewed scalar value"
        return None
    if family == "metric_grid":
        tiles = _rows_from(widget, "tiles")
        if not tiles or not all(
            _has_value(row, "label", "name")
            # ``_render_metric_grid`` requires the literal reviewed value;
            # display_value alone is a presentation alias and is not an
            # executable metric-grid shape.
            and _has_value(row, "value")
            for row in tiles
        ):
            return "requires exact label/value tiles"
        return None
    if family == "table":
        return None if rows or _rows_from(widget, "manager_rows", "data", "values", "bars", "categories", "segments", "points", "series", "cells", "stages", "bins", "boxes", "steps") else "requires reviewed rows"
    return "no eligibility predicate is available"


def eligible_chart_recipes(
    widget: Mapping[str, Any],
    chart: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return deterministic executable/unavailable recipes for one visual.

    Eligibility is conservative and family-specific: a recipe is executable
    only when the fixture exposes the exact reviewed container and semantic
    fields that renderer family consumes.  No values are aggregated,
    normalized, or recalculated here.
    """

    by_id = _registry_by_id(registry)
    current_family = _text(chart.get("family")).strip()
    available = _available_fields(widget, chart)
    recipes: list[dict[str, Any]] = []
    for family_id, entry in by_id.items():
        required = _recipe_requirements(family_id)
        shape_reason = _recipe_shape_reason(family_id, widget, chart)
        supported = family_id in SUPPORTED_FAMILIES
        selected = family_id == current_family
        eligible = supported and shape_reason is None
        renderer_types = renderer_types_for_recipe(family_id, widget) if eligible else ()
        eligible = eligible and bool(renderer_types)
        reason = "eligible" if eligible else (
            "unsupported renderer family"
            if not supported
            else (shape_reason or "no exact renderer contract for supplied shape")
        )
        recipes.append(
            {
                "id": family_id,
                "label": _text(entry.get("label"), family_id),
                "selected": selected,
                "eligible": eligible,
                "renderer_status": _text(entry.get("renderer_status"), "unavailable"),
                "reason": reason,
                "required_fields": list(required),
                "available_fields": sorted(available),
                "renderer_types": list(renderer_types),
                "default_renderer_type": renderer_types[0] if renderer_types else None,
            }
        )
    return recipes


def design_inventory(
    fixture: Mapping[str, Any],
    chart_map: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine exact visual facts with honest candidate recipes.

    Values in ``exact`` are copied from the fixture/chart map.  ``recipes``
    are executable choices only when the registry and available fields agree.
    """

    widgets = [widget for widget in _as_list(fixture.get("widgets")) if isinstance(widget, Mapping)]
    charts = [chart for chart in _as_list(chart_map.get("charts")) if isinstance(chart, Mapping)]
    widgets_by_id = {_text(value.get("id")): value for value in widgets if _text(value.get("id"))}
    charts_by_id = {_text(value.get("id")): value for value in charts if _text(value.get("id"))}
    visuals: list[dict[str, Any]] = []
    for widget_id in sorted(set(widgets_by_id).intersection(charts_by_id)):
        widget = widgets_by_id[widget_id]
        chart = charts_by_id[widget_id]
        recipes = eligible_chart_recipes(widget, chart, registry)
        eligible_ids = [
            _text(recipe.get("id"))
            for recipe in recipes
            if isinstance(recipe, Mapping) and recipe.get("eligible") is True
        ]
        manager_presentation = widget.get("manager_presentation")
        explicit_recipe = (
            manager_presentation.get("recipe_id")
            if isinstance(manager_presentation, Mapping) and manager_presentation.get("recipe_id")
            else widget.get("recipe_id")
        )
        if explicit_recipe:
            selected_recipe = _text(explicit_recipe).strip()
            if selected_recipe not in eligible_ids:
                selected_recipe = _default_recipe_id(eligible_ids, chart.get("family"))
        else:
            selected_recipe = _default_recipe_id(eligible_ids, chart.get("family"))
        recipe_entry = next(
            (
                recipe for recipe in recipes
                if isinstance(recipe, Mapping) and _text(recipe.get("id")) == selected_recipe
            ),
            None,
        )
        renderer_types = (
            tuple(_text(value) for value in recipe_entry.get("renderer_types", []) if _text(value).strip())
            if isinstance(recipe_entry, Mapping)
            else renderer_types_for_recipe(selected_recipe, widget)
        )
        selected_renderer_type = _text(
            (widget.get("manager_presentation") or {}).get("renderer_type")
            if isinstance(widget.get("manager_presentation"), Mapping)
            else widget.get("renderer_type")
        ).strip()
        if selected_renderer_type not in renderer_types:
            selected_renderer_type = renderer_types[0] if renderer_types else ("table" if selected_recipe == "table" else "")
        selected_layout = _text(
            (widget.get("manager_presentation") or {}).get("layout")
            if isinstance(widget.get("manager_presentation"), Mapping)
            else widget.get("layout")
        ).strip() or default_layout_for_recipe(selected_recipe)
        exact = {
            key: copy.deepcopy(widget.get(key))
            for key in _EXACT_FIELD_KEYS
            if key in widget
        }
        chart_fields = chart.get("fields_or_values_used")
        if isinstance(chart_fields, Mapping):
            exact["chart_fields_or_values_used"] = copy.deepcopy(dict(chart_fields))
        visuals.append(
            {
                "widget_id": widget_id,
                "requirement_id": _text(widget.get("requirement_id")),
                "type": _text(widget.get("type") or widget.get("kind")),
                "family": _text(chart.get("family")),
                "exact": exact,
                "provenance": {
                    key: copy.deepcopy(widget.get(key))
                    for key in (
                        "integration_record_id",
                        "integration_record_ids",
                        "integration_record_ref",
                        "integration_record_refs",
                        "evidence_refs",
                        "trace_refs",
                        "reviewed_item_ref",
                        "reviewed_output_ref",
                    )
                    if key in widget
                },
                "recipes": recipes,
                "selected_recipe_id": selected_recipe,
                "selected_layout": selected_layout,
                "selected_renderer_type": selected_renderer_type,
            }
        )
    return {
        "schema_version": BLUEPRINT_SCHEMA,
        "kind": "design_inventory",
        "widget_count": len(widgets),
        "visuals": visuals,
    }


def _dataset_for_widget(widget: Mapping[str, Any], chart: Mapping[str, Any]) -> dict[str, Any]:
    """Build metadata-only dataset binding; rows remain exact supplied data."""

    rows: Any = None
    for key in (
        "rows",
        "bars",
        "categories",
        "segments",
        "points",
        "series",
        "cells",
        "tiles",
        "stages",
        "bins",
        "boxes",
        "steps",
        "manager_rows",
        "data",
        "values",
    ):
        if key in widget:
            rows = copy.deepcopy(widget.get(key))
            break
    fields = chart.get("fields_or_values_used") if isinstance(chart, Mapping) else {}
    if not isinstance(fields, Mapping):
        fields = {}
    dimensions = widget.get("dimensions", fields.get("dimensions") if isinstance(fields, Mapping) else None)
    measures = widget.get("measures", fields.get("measures") if isinstance(fields, Mapping) else None)
    return {
        "id": f"dataset-{_text(widget.get('id'), 'visual')}",
        "requirement_id": _text(widget.get("requirement_id")),
        "source_record_ids": sorted({
            _text(item).strip()
            for item in _as_list(widget.get("integration_record_ids"))
            if _text(item).strip()
        } | ({_text(widget.get("integration_record_id")).strip()} if _text(widget.get("integration_record_id")).strip() else set())),
        "rows": rows,
        "row_count": len(rows) if isinstance(rows, list) else (1 if rows is not None else 0),
        "grain": copy.deepcopy(widget.get("grain", fields.get("grain") if isinstance(fields, Mapping) else None)),
        "dimensions": copy.deepcopy(dimensions),
        "measures": copy.deepcopy(measures),
        "time": copy.deepcopy(widget.get("time", fields.get("time") if isinstance(fields, Mapping) else None)),
        "denominator": copy.deepcopy(widget.get("denominator", fields.get("denominator"))),
        "coverage": copy.deepcopy(widget.get("coverage", fields.get("coverage"))),
        "limitations": copy.deepcopy(widget.get("limitations", fields.get("limitations", []))),
    }


def build_blueprint(
    fixture: Mapping[str, Any],
    chart_map: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    fixture_ref: str = "",
    fixture_sha256: str = "",
    chart_map_ref: str = "",
    chart_map_sha256: str = "",
    registry_ref: str = "",
    registry_sha256: str = "",
    blueprint_ref: str = "",
    presentation_plan_ref: str | None = None,
    presentation_plan_sha256: str | None = None,
    review_status: str = BLUEPRINT_STATUS_PREVIEW,
) -> dict[str, Any]:
    """Create the single canonical, source-bound business blueprint."""

    widgets = [widget for widget in _as_list(fixture.get("widgets")) if isinstance(widget, Mapping)]
    charts_by_id = {
        _text(chart.get("id")): chart
        for chart in _as_list(chart_map.get("charts"))
        if isinstance(chart, Mapping) and _text(chart.get("id"))
    }
    by_id = {_text(widget.get("id")): widget for widget in widgets if _text(widget.get("id"))}
    section_by_visual: dict[str, str] = {}
    for domain in _as_list(fixture.get("domains")):
        if not isinstance(domain, Mapping):
            continue
        domain_id = _text(domain.get("id"), "domain")
        for flow in _as_list(domain.get("decision_flow")):
            if not isinstance(flow, Mapping):
                continue
            section_id = f"flow-{_text(flow.get('id'), domain_id)}"
            for visual_id in _as_list(flow.get("widget_ids")):
                visual_id = _text(visual_id)
                if visual_id.strip():
                    section_by_visual.setdefault(visual_id, section_id)
    inventory = design_inventory(fixture, chart_map, registry)
    inventory_by_id = {
        _text(value.get("widget_id")): value
        for value in inventory.get("visuals", [])
        if isinstance(value, Mapping) and _text(value.get("widget_id")).strip()
    }
    datasets = []
    visuals = []
    for widget_id in sorted(by_id):
        widget = by_id[widget_id]
        chart = charts_by_id.get(widget_id, {})
        dataset = _dataset_for_widget(widget, chart)
        datasets.append(dataset)
        inventory_visual = inventory_by_id.get(widget_id, {})
        visuals.append(
            {
                "id": widget_id,
                "dataset_id": dataset["id"],
                "requirement_id": _text(widget.get("requirement_id")),
                "page_id": f"domain-{_text(widget.get('domain_id'), 'default')}",
                "section_id": section_by_visual.get(
                    widget_id,
                    f"flow-{_text(widget.get('domain_id'), 'default')}",
                ),
                "chart_intent": _text(widget.get("chart_intent") or widget.get("title") or widget_id),
                "family": _text(chart.get("family"), "table"),
                "type": _text(widget.get("type") or widget.get("kind"), "table"),
                # The source chart family remains immutable above; recipe_id
                # is the validated Product Agent choice consumed by the
                # manager renderer.  Unplanned/non-manager fixtures retain a
                # deterministic source-family default.
                "recipe_id": _text(
                    (widget.get("manager_presentation") or {}).get("recipe_id")
                    if isinstance(widget.get("manager_presentation"), Mapping)
                    else widget.get("recipe_id") or chart.get("family"),
                    _text(chart.get("family"), "table"),
                ),
                "layout": _text(
                    (widget.get("manager_presentation") or {}).get("layout")
                    if isinstance(widget.get("manager_presentation"), Mapping)
                    else widget.get("layout") or default_layout_for_recipe(_text(chart.get("family"), "table")),
                    default_layout_for_recipe(_text(chart.get("family"), "table")),
                ),
                "renderer_type": (
                    _text(
                        (widget.get("manager_presentation") or {}).get("renderer_type")
                        if isinstance(widget.get("manager_presentation"), Mapping)
                        else widget.get("renderer_type")
                    ).strip()
                    or _text(widget.get("renderer_type")).strip()
                    or _text(inventory_visual.get("selected_renderer_type")).strip()
                    or None
                ),
                "encodings": copy.deepcopy(widget.get("encodings", chart.get("encodings", {}))),
                "filters": copy.deepcopy(widget.get("filters", [])),
                "drilldown": copy.deepcopy(widget.get("drilldown", {"enabled": True})),
                "empty_state": copy.deepcopy(widget.get("empty_state", "No reviewed values are available for this view.")),
                "provenance": {
                    key: copy.deepcopy(widget.get(key))
                    for key in (
                        "integration_record_id",
                        "integration_record_ids",
                        "integration_record_ref",
                        "integration_record_refs",
                        "evidence_refs",
                        "trace_refs",
                    )
                    if key in widget
                },
            }
        )

    pages: list[dict[str, Any]] = []
    for domain in _as_list(fixture.get("domains")):
        if not isinstance(domain, Mapping):
            continue
        domain_id = _text(domain.get("id"), "domain")
        sections = []
        for flow in _as_list(domain.get("decision_flow")):
            if not isinstance(flow, Mapping):
                continue
            section_id = f"flow-{_text(flow.get('id'), domain_id)}"
            visual_ids = [
                _text(item)
                for item in _as_list(flow.get("widget_ids"))
                if _text(item).strip()
            ]
            for visual_id in visual_ids:
                section_by_visual.setdefault(visual_id, section_id)
            sections.append(
                {
                    "id": section_id,
                    "title": _text(flow.get("title") or flow.get("id"), "Decision view"),
                    "layout": copy.deepcopy(flow.get("layout", "responsive")),
                    "visual_ids": visual_ids,
                }
            )
        pages.append(
            {
                "id": f"domain-{domain_id}",
                "title": _text(domain.get("title"), domain_id),
                "order": domain.get("order"),
                "sections": sections,
            }
        )
    return {
        "schema_version": BLUEPRINT_SCHEMA,
        "kind": BLUEPRINT_KIND,
        "run_id": _text(fixture.get("run_id")),
        "generation_id": _text(fixture.get("generation_id")),
        "review_status": review_status if review_status in {"Preview", "Reviewed"} else BLUEPRINT_STATUS_PREVIEW,
        "source_policy": "accepted_and_committed_only",
        "source_bindings": {
            "fixture_ref": fixture_ref,
            "fixture_sha256": fixture_sha256,
            "chart_map_ref": chart_map_ref,
            "chart_map_sha256": chart_map_sha256,
            "chart_registry_ref": registry_ref,
            "chart_registry_sha256": registry_sha256,
            "blueprint_ref": blueprint_ref,
            "presentation_plan_ref": presentation_plan_ref,
            "presentation_plan_sha256": presentation_plan_sha256,
        },
        "datasets": datasets,
        "pages": pages,
        "visuals": visuals,
        "filters": copy.deepcopy(fixture.get("filters", [{"id": "text", "kind": "search", "label": "Search reviewed views"}])),
        "limitations": copy.deepcopy(fixture.get("limitations", [])),
        "design_inventory": inventory,
    }


__all__ = [
    "BLUEPRINT_KIND",
    "BLUEPRINT_SCHEMA",
    "BLUEPRINT_STATUS_PREVIEW",
    "SUPPORTED_FAMILIES",
    "SUPPORTED_LAYOUTS",
    "SUPPORTED_RENDERER_TYPES",
    "build_blueprint",
    "canonical_bytes",
    "default_layout_for_recipe",
    "default_renderer_type_for_recipe",
    "design_inventory",
    "eligible_chart_recipes",
    "renderer_types_for_recipe",
    "sha256",
    "is_partition_visual",
]
