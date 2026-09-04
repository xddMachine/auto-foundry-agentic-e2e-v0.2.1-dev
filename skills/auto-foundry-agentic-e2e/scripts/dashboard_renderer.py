#!/usr/bin/env python3
"""Render a reviewed widget fixture as a deterministic offline dashboard.

The renderer is deliberately a presentation helper.  It reads values and
labels already present in the reviewed fixture and never queries a source or
derives an analytical metric.  All visual forms are small HTML/CSS forms so a
generated page works without JavaScript, a CDN, or an internet connection.

Example::

    python3 dashboard_renderer.py \
      --input reviewed_widgets.json \
      --output dashboard.html \
      --manifest-output dashboard_manifest.json

The fixture contract is documented in ``skills/auto-foundry-agentic-e2e/README.md``.
"""

from __future__ import annotations

import argparse
import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import html
import importlib.util
import json
import math
import posixpath
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from auto_foundry_core.product_contracts import validate_product_manifest
    from auto_foundry_core.workspace import AllowedRootError, RunContext
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    # The skill is installable independently of the source checkout.  When a
    # developer invokes this script directly from the repository, make the
    # committed local core importable without changing the caller's paths.
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    from auto_foundry_core.product_contracts import validate_product_manifest
    from auto_foundry_core.workspace import AllowedRootError, RunContext


SUPPORTED_WIDGETS = {
    "kpi",
    "bar",
    "column",
    "lollipop",
    "diverging_bar",
    "waffle",
    "line",
    "stacked_composition",
    "heatmap",
    "scatter",
    "donut",
    "progress",
    "leaderboard",
    "metric_grid",
    "kpi_grid",
    "status_table",
    "table",
    # Declarative V2 chart-family variants.  Each variant consumes only the
    # geometry/rows supplied by the fixture; unsupported exotic families stay
    # unavailable in the design inventory instead of being faked here.
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

# Partition membership is delegated to the shared dashboard runtime.  The
# helper derives the universe from the current fixture rather than freezing
# one generation's counts, while retaining ordinary table/status-table
# projections in the authoritative audit inventory.

_V4_REGISTRY_SCHEMA = "dashboard.chart_registry.v1"
_V4_SMALL_MULTIPLE_IDS = frozenset({"req02-ecom-channel-bars", "req02-erp-channel-bars"})
_V4_REGISTRY_FAMILY_BY_TYPE = {
    "kpi": "kpi_card",
    "bar": "horizontal_bar",
    "column": "column",
    "lollipop": "lollipop",
    "diverging_bar": "diverging_bar",
    "waffle": "waffle",
    "line": "line_area_slope",
    "stacked_composition": "stacked_bar",
    "heatmap": "heatmap_matrix",
    "scatter": "scatter_bubble",
    "donut": "donut_pie",
    "progress": "horizontal_bar",
    "leaderboard": "lollipop",
    "metric_grid": "metric_grid",
    # The assembler keeps these local presentation names distinct so a
    # reviewed dashboard fact can be rendered faithfully.  They intentionally
    # reuse the existing registry families rather than inventing chart
    # families that the committed V4 registry does not expose.
    "kpi_grid": "metric_grid",
    "status_table": "table",
    "table": "table",
    "area": "line_area_slope",
    "stacked_area": "line_area_slope",
    "grouped_bar": "grouped_bar",
    "stacked_bar": "stacked_bar",
    "normalized_stacked_bar": "stacked_bar",
    "funnel": "funnel",
    "histogram": "histogram_box",
    "box_plot": "histogram_box",
    "pareto": "pareto",
    "waterfall": "waterfall",
    "pie": "donut_pie",
}

# A V2 Product Agent selection names a registry recipe.  The raw widget type
# and chart-map family remain immutable source bindings; this bounded map
# tells the manager-only renderer which existing supplied-value renderer to
# use for the selected recipe.
_RECIPE_RENDER_TYPE = {
    "kpi_card": "kpi",
    "horizontal_bar": "bar",
    "column": "column",
    "grouped_bar": "grouped_bar",
    "stacked_bar": "stacked_bar",
    "diverging_bar": "diverging_bar",
    "waffle": "waffle",
    "funnel": "funnel",
    "histogram": "histogram",
    "histogram_box": "histogram",
    "box_plot": "box_plot",
    "pareto": "pareto",
    "waterfall": "waterfall",
    "heatmap_matrix": "heatmap",
    "scatter_bubble": "scatter",
    "line_area_slope": "line",
    "lollipop": "lollipop",
    "donut_pie": "donut",
    "metric_grid": "metric_grid",
    "table": "table",
}

_DASHBOARD_RUNTIME_MODULE: Any | None = None


def _dashboard_runtime_module() -> Any:
    """Load the canonical V2 eligibility runtime lazily.

    The renderer remains directly executable as a standalone skill script, so
    importing the sibling module through a small cached loader keeps the
    renderer and Product Agent on one family/shape contract without requiring
    a package install or a second implementation of eligibility predicates.
    """

    global _DASHBOARD_RUNTIME_MODULE
    if _DASHBOARD_RUNTIME_MODULE is not None:
        return _DASHBOARD_RUNTIME_MODULE
    runtime_path = Path(__file__).resolve().with_name("dashboard_runtime.py")
    spec = importlib.util.spec_from_file_location("dashboard_runtime_for_renderer", runtime_path)
    if spec is None or spec.loader is None:
        raise ValueError("canonical dashboard runtime cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _DASHBOARD_RUNTIME_MODULE = module
    return module

_OFFLINE_FAVICON_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Auto Foundry"><rect width="64" height="64" rx="12" fill="#0e1b2a"/><path d="M16 43V21h9v22h-9Zm12 0V14h9v29h-9Zm12 0V27h9v16h-9Z" fill="#076f75"/></svg>\n'''


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _slug(value: Any) -> str:
    raw = _text(value, "trace")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    return slug or "trace"


def _escape(value: Any, default: str = "") -> str:
    return html.escape(_text(value, default), quote=True)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _is_partition_visual(
    widget: Mapping[str, Any],
    chart: Mapping[str, Any],
) -> bool:
    """Delegate partition membership to the shared dashboard runtime."""

    return _dashboard_runtime_module().is_partition_visual(widget, chart)


def _trace_records(widget: Mapping[str, Any]) -> list[dict[str, str]]:
    """Normalize trace references without changing their reviewed meaning."""

    raw = widget.get("trace_refs")
    if not _as_list(raw):
        raw = widget.get("trace_ref")
    if not _as_list(raw):
        raw = widget.get("evidence_refs")
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in _as_list(raw):
        if isinstance(value, Mapping):
            ref = _text(value.get("id") or value.get("trace_id") or value.get("anchor") or value.get("href") or value.get("ref") or value.get("path"))
            label = _text(value.get("label") or value.get("title") or ref)
            href = _text(value.get("href"))
        else:
            ref, label, href = _text(value), _text(value), ""
        if not ref and not href:
            continue
        if href.startswith("#"):
            anchor = href[1:]
            if not anchor:
                raise ValueError("trace reference cannot use an empty fragment")
        else:
            # A reviewed evidence reference is represented by a stable local
            # anchor.  The file/path remains visible as the link label.
            anchor = "trace-" + _slug(ref or href)
        if not anchor.startswith("trace-"):
            anchor = "trace-" + _slug(anchor)
        if anchor not in seen:
            records.append({"anchor": anchor, "label": label or ref or href, "ref": ref or href})
            seen.add(anchor)
    if not records:
        widget_id = _text(widget.get("id") or widget.get("title"), "widget")
        raise ValueError(f"widget {widget_id} has no usable evidence or trace provenance")
    return records


def _reference_values(value: Any) -> list[str]:
    """Return non-empty reviewed references without inventing placeholders."""

    values: list[str] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            candidate = item.get("id") or item.get("trace_id") or item.get("anchor") or item.get("href") or item.get("ref") or item.get("path")
        else:
            candidate = item
        if candidate is None or not _text(candidate).strip():
            continue
        values.append(_text(candidate).strip())
    return values


def _widget_trace_refs(widget: Mapping[str, Any]) -> list[str]:
    refs = _reference_values(widget.get("trace_refs"))
    if not refs:
        refs = _reference_values(widget.get("trace_ref"))
    return refs


def _validate_widget_provenance(widget: Mapping[str, Any]) -> None:
    widget_id = _text(widget.get("id"), "widget")
    reviewed_item_ref = widget.get("reviewed_item_ref")
    reviewed_output_ref = widget.get("reviewed_output_ref")
    if not _reference_values(reviewed_item_ref):
        raise ValueError(f"widget {widget_id} requires reviewed_item_ref")
    if not _reference_values(reviewed_output_ref):
        raise ValueError(f"widget {widget_id} requires reviewed_output_ref")
    evidence_refs = _reference_values(widget.get("evidence_refs"))
    trace_refs = _reference_values(widget.get("trace_refs"))
    if not trace_refs:
        trace_refs = _reference_values(widget.get("trace_ref"))
    if not evidence_refs and not trace_refs:
        raise ValueError(f"widget {widget_id} requires evidence_refs or trace_refs")


def _is_v4_fixture(fixture: Mapping[str, Any]) -> bool:
    try:
        return int(fixture.get("dashboard_version")) == 4
    except (TypeError, ValueError):
        return False


def _load_v4_json_asset(context: RunContext, reference: Any, label: str) -> tuple[Path, Mapping[str, Any]]:
    if not isinstance(context, RunContext):
        raise ValueError(f"v4 {label} validation requires a RunContext")
    relative = _text(reference).strip()
    if not relative:
        raise ValueError(f"v4 fixture requires {label}")
    resolved = context.resolve_run_path(relative)
    if not resolved.is_file():
        raise FileNotFoundError(f"v4 {label} not found: {relative}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"v4 {label} must contain a JSON object")
    return resolved, payload


def _validate_v4_chart_assets(
    fixture: Mapping[str, Any],
    widgets: list[Mapping[str, Any]],
    context: RunContext | None,
) -> dict[str, str] | None:
    if not _is_v4_fixture(fixture):
        return None
    registry_path, registry = _load_v4_json_asset(context, fixture.get("chart_registry_ref"), "chart_registry_ref")
    if _text(registry.get("schema_version")) != _V4_REGISTRY_SCHEMA:
        raise ValueError(f"v4 chart registry schema must be {_V4_REGISTRY_SCHEMA}")
    raw_families = registry.get("families")
    if not isinstance(raw_families, list) or not raw_families:
        raise ValueError("v4 chart registry requires non-empty families")
    family_ids: set[str] = set()
    for family in raw_families:
        if not isinstance(family, Mapping):
            raise ValueError("v4 chart registry families must be objects")
        family_id = _text(family.get("id")).strip()
        if not family_id or family_id in family_ids:
            raise ValueError("v4 chart registry families require unique non-empty ids")
        family_ids.add(family_id)

    chart_map_path, chart_map = _load_v4_json_asset(context, fixture.get("chart_map_ref"), "chart_map_ref")
    if _text(chart_map.get("chart_registry_ref")) != _text(fixture.get("chart_registry_ref")):
        raise ValueError("v4 chart map and fixture chart_registry_ref must match")
    raw_charts = chart_map.get("charts")
    if not isinstance(raw_charts, list) or not raw_charts:
        raise ValueError("v4 chart map requires non-empty charts")
    charts_by_id: dict[str, Mapping[str, Any]] = {}
    for chart in raw_charts:
        if not isinstance(chart, Mapping):
            raise ValueError("v4 chart map entries must be objects")
        chart_id = _text(chart.get("id")).strip()
        if not chart_id or chart_id in charts_by_id:
            raise ValueError("v4 chart map requires unique non-empty ids")
        charts_by_id[chart_id] = chart

    widget_ids = {_text(widget.get("id")) for widget in widgets}
    if set(charts_by_id) != widget_ids:
        raise ValueError("v4 chart map ids must exactly match fixture widget ids")
    required_families: set[str] = set()
    for widget in widgets:
        widget_id = _text(widget.get("id"))
        widget_type = _text(widget.get("type") or widget.get("kind"), "kpi").lower()
        chart = charts_by_id[widget_id]
        chart_type = _text(chart.get("type")).strip().lower()
        if chart_type != widget_type:
            raise ValueError(f"v4 chart map type mismatch for {widget_id}")
        required_family = _V4_REGISTRY_FAMILY_BY_TYPE.get(widget_type)
        if required_family is None:
            raise ValueError(f"v4 widget type has no chart registry family: {widget_type}")
        required_families.add(required_family)
        fields = chart.get("fields_or_values_used")
        if not isinstance(fields, Mapping):
            raise ValueError(f"v4 chart map fields_or_values_used missing for {widget_id}")
        for key in ("small_multiple_group", "scale_policy"):
            widget_has = key in widget and _text(widget.get(key)).strip() != ""
            map_has = key in fields and _text(fields.get(key)).strip() != ""
            if widget_has != map_has or (widget_has and _text(widget.get(key)) != _text(fields.get(key))):
                raise ValueError(f"v4 chart metadata mismatch for {widget_id}/{key}")
            if (widget_has or map_has) and widget_id not in _V4_SMALL_MULTIPLE_IDS:
                raise ValueError(f"v4 small-multiple metadata is not allowed for {widget_id}")
        if widget_id in _V4_SMALL_MULTIPLE_IDS and not ("small_multiple_group" in widget and "scale_policy" in widget):
            raise ValueError(f"v4 small-multiple metadata is incomplete for {widget_id}")
    if any(widget.get("small_multiple_group") for widget in widgets):
        required_families.add("small_multiples")
    if fixture.get("ontology_groups") or fixture.get("ontology_objects"):
        required_families.add("network_ontology_graph")
    missing_families = sorted(required_families - family_ids)
    if missing_families:
        raise ValueError(f"v4 chart registry is missing used families: {missing_families}")
    return {
        "chart_registry_ref": _text(fixture.get("chart_registry_ref")),
        "chart_registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        "chart_registry_version": _text(registry.get("schema_version")),
        "chart_map_sha256": hashlib.sha256(chart_map_path.read_bytes()).hexdigest(),
    }


def _required_visual_row(row: Mapping[str, Any], prefix: str) -> tuple[str, str]:
    label = row.get("label")
    if label is None or not _text(label).strip():
        raise ValueError(f"{prefix} requires non-empty label")
    # Chart families use a few explicit, source-local names for the same
    # displayed fact (for example ``count`` for a histogram/funnel bin).  Do
    # not derive a value; accept only a field that the fixture supplied.
    value_key = next(
        (
            key
            for key in ("value", "display_value", "count", "amount", "measure")
            if key in row and row.get(key) is not None and _text(row.get(key)).strip()
        ),
        None,
    )
    if value_key is None:
        raise ValueError(f"{prefix} requires non-empty value")
    return _text(label), _display_value(row.get("display_value", row.get(value_key)))


def _fact_row_label(row: Mapping[str, Any], index: int) -> str:
    """Return a supplied/humanized label for an explicit dashboard fact row."""

    label = row.get("label") or row.get("name")
    if label is not None and _text(label).strip():
        return _text(label)
    parts = [
        _text(value).strip()
        for key, value in row.items()
        if isinstance(value, str)
        and key not in {"unit", "period", "status", "measure"}
        and _text(value).strip()
    ]
    return " · ".join(parts) if parts else f"Reviewed row {index}"


def _fact_row_value(row: Mapping[str, Any]) -> str:
    """Show supplied scalar fields when a fact has no geometry field."""

    series = row.get("series")
    if isinstance(series, list):
        values = []
        for item in series:
            if not isinstance(item, Mapping):
                continue
            label = _text(item.get("label") or item.get("name") or "Reviewed measure")
            value = item.get("display_value", item.get("value"))
            if value is not None:
                values.append(f"{label}: {_display_value(value)}")
        if values:
            return " · ".join(values)
    value = row.get("display_value", row.get("value"))
    if value is not None and _text(value).strip():
        return _display_value(value)
    parts = [
        f"{_humanize_label(key)}: {_display_value(value)}"
        for key, value in row.items()
        if key not in {"label", "name"}
        and not isinstance(value, (Mapping, list, tuple))
        and value is not None
    ]
    return " · ".join(parts) if parts else "Reviewed value unavailable"


def _fact_series_markup(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """Render explicit multi-series values without inventing a total."""

    series = row.get("series")
    if not isinstance(series, list):
        return None
    rendered: list[str] = []
    aria: list[str] = []
    for item in series:
        if not isinstance(item, Mapping):
            continue
        label = _text(item.get("label") or item.get("name") or "Reviewed measure").strip()
        value = item.get("display_value", item.get("value"))
        if not label or value is None:
            continue
        rendered.append(
            f'<span class="viz-series-value"><b>{_escape(label)}</b> {_escape(_display_value(value))}</span>'
        )
        aria.append(f"{label}: {_display_value(value)}")
    if not rendered:
        return None
    return "".join(rendered), "; ".join(aria)


def _fact_series_geometry_markup(
    row: Mapping[str, Any],
    *,
    column: bool = False,
    raw_values: bool = False,
    scale_groups: Any = None,
) -> tuple[str, str] | None:
    """Render explicit series tracks from assembler-bound geometry.

    Series values are never recalculated here.  The assembler supplies a
    bounded ``size`` (and, for signed measures, ``signed_size``) for every
    numeric series item; absence is a contract error rather than permission
    to fall back to a text-only row.  A typed artifact column may opt into
    ``raw_values`` when its supplied counts exceed the bounded percent domain.
    In that mode the exact values remain labels on the column tracks and no
    ratio, maximum, or other derived geometry is introduced by the renderer.
    """

    series = row.get("series")
    if not isinstance(series, list) or not series:
        return None
    if scale_groups is None:
        scale_groups = row.get("scale_groups")

    # A mixed reviewed series carries the assembler's explicit scale group on
    # each item.  Keep those units visibly separate instead of placing unlike
    # measures on one shared track.  ``scale_groups`` is an optional
    # declaration map for payloads that bind group membership by exact series
    # label; it is never interpreted as a value or used to derive geometry.
    def declared_group(item: Mapping[str, Any]) -> str:
        value = _text(item.get("scale_group")).strip()
        if value:
            return value
        label = _text(item.get("label") or item.get("name")).strip()
        if not label or not isinstance(scale_groups, Mapping):
            return ""
        for raw_group, members in scale_groups.items():
            group = _text(raw_group).strip()
            if not group:
                continue
            if isinstance(members, Mapping):
                members = (
                    members.get("series")
                    or members.get("labels")
                    or members.get("measures")
                    or members.get("fields")
                    or []
                )
            if isinstance(members, (list, tuple, set, frozenset)):
                if any(label == _text(member).strip() for member in members):
                    return group
            elif label == _text(members).strip():
                return group
        return ""

    def declared_context(group: str) -> list[str]:
        """Read optional domain/basis labels from the exact group declaration."""

        if not isinstance(scale_groups, Mapping):
            return []
        raw = scale_groups.get(group)
        if raw is None:
            raw = next(
                (value for key, value in scale_groups.items() if _text(key).casefold() == group.casefold()),
                None,
            )
        if not isinstance(raw, Mapping):
            return []
        context: list[str] = []
        for key, label in (("scale_domain", "Domain"), ("domain", "Domain"), ("scale_basis", "Basis"), ("basis", "Basis")):
            value = _text(raw.get(key)).strip()
            if value and f"{label}: {value}" not in context:
                context.append(f"{label}: {value}")
        return context

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in series:
        if not isinstance(item, Mapping):
            raise ValueError("fact series items must be objects")
        group = declared_group(item)
        if group:
            grouped.setdefault(group, []).append(item)
    if len(grouped) > 1:
        # Preserve an item that lacks an explicit group without silently
        # dropping it.  The neutral label makes the missing declaration
        # visible while retaining the exact supplied value.
        ungrouped = [item for item in series if not declared_group(item)]
        if ungrouped:
            grouped.setdefault("Unspecified scale", []).extend(ungrouped)
        panels: list[str] = []
        panel_aria: list[str] = []
        for group, items in grouped.items():
            group_markup = _fact_series_geometry_markup(
                {"series": items},
                column=column,
                raw_values=raw_values,
                scale_groups=scale_groups,
            )
            if group_markup is None:
                continue
            markup, aria_value = group_markup
            group_label = _humanize_label(group)
            context: list[str] = declared_context(group)
            for key, label in (("scale_domain", "Domain"), ("scale_basis", "Basis")):
                values = []
                for item in items:
                    value = _text(item.get(key)).strip()
                    if value and value not in values:
                        values.append(value)
                if values:
                    rendered = f"{label}: {', '.join(values)}"
                    if rendered not in context:
                        context.append(rendered)
            context_html = (
                f'<small class="viz-series-panel-context">{_escape(" · ".join(context))}</small>'
                if context else ""
            )
            panels.append(
                f'<span class="viz-series-panel scale-group-panel" role="group" '
                f'data-scale-group="{_escape(group)}" aria-label="{_escape(f"Scale group: {group_label}")}">'
                f'<span class="viz-series-panel-title"><b>{_escape(group_label)}</b>'
                f'<small>Independent scale</small>{context_html}</span>'
                f'<span class="viz-series-panel-values">{markup}</span></span>'
            )
            panel_aria.append(f"{group_label}: {aria_value}")
        if panels:
            return "".join(panels), "; ".join(panel_aria)

    rendered: list[str] = []
    aria: list[str] = []
    for index, item in enumerate(series, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"fact series item {index} must be an object")
        label = _text(item.get("label") or item.get("name") or "Reviewed measure").strip()
        value = item.get("display_value", item.get("value"))
        if not label or value is None:
            raise ValueError(f"fact series item {index} requires label and value")
        if "size" not in item:
            if not raw_values or not column:
                raise ValueError(f"fact series item {index} requires supplied size")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"fact series item {index} raw value must be a finite non-negative number")
            escaped_label = _escape(label)
            escaped_value = _escape(_display_value(value))
            # Keep the column-family geometry and exact count visible without
            # pretending the count is a percentage.  The track is deliberately
            # unfilled; its data attribute/ARIA label carries the raw supplied
            # value for accessible and deterministic inspection.
            rendered.append(
                f'<span class="column-series-item column-series-item-raw"><b>{escaped_label}</b>'
                f'<span class="column-track column-series-track column-series-track-raw" '
                f'data-raw-value="{escaped_value}" aria-label="{escaped_label}: {escaped_value}" aria-hidden="true"></span>'
                f'<span class="column-series-value">{escaped_value}</span></span>'
            )
            aria.append(f"{label}: {_display_value(value)}")
            continue
        normalized_size = _normalize_percent(item.get("size"), f"fact series item {index}")
        signed = item.get("signed_size")
        negative = False
        if signed is not None:
            signed_value = _normalize_signed_percent(signed, f"fact series item {index}")
            negative = float(signed_value[:-1]) < 0
        escaped_label = _escape(label)
        escaped_value = _escape(_display_value(value))
        bar_class = " viz-bar-negative" if negative else ""
        if column:
            rendered.append(
                f'<span class="column-series-item"><b>{escaped_label}</b>'
                f'<span class="column-track column-series-track"><span class="column-bar{bar_class}" '
                f'style="--column-size:{_escape(normalized_size)}"></span></span>'
                f'<span class="column-series-value">{escaped_value}</span></span>'
            )
        else:
            rendered.append(
                f'<span class="viz-series-line"><b>{escaped_label}</b>'
                f'<span class="viz-track viz-series-track"><span class="viz-bar{bar_class}" '
                f'style="--bar-size:{_escape(normalized_size)}"></span></span>'
                f'<span class="viz-series-value">{escaped_value}</span></span>'
            )
        aria.append(f"{label}: {_display_value(value)}")
    return "".join(rendered), "; ".join(aria)


_NUMERIC_SCALAR_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")


def _raw_display_value(value: Any) -> str:
    """Return the exact supplied scalar representation for provenance/detail."""

    return _text(value, "—")


def _display_value(value: Any) -> str:
    """Return the exact supplied value for charts, tables, and metadata."""

    return _raw_display_value(value)


def _compact_scalar_display(value: Any) -> str:
    """Compact a long scalar for KPI cards without changing its semantic unit.

    This presentation-only rounding is deliberately limited to KPI/overview
    cards.  Charts, tables, ontology summaries, and ARIA labels retain the
    exact supplied scalar so a tiny value can never appear as a misleading
    zero.  No percent/currency conversion, aggregation, or inferred
    denominator is introduced.
    """

    raw = _raw_display_value(value)
    if value is None or isinstance(value, (bool, Mapping, list, tuple)):
        return raw
    candidate = raw.strip()
    if not _NUMERIC_SCALAR_RE.fullmatch(candidate):
        return raw
    try:
        numeric = Decimal(candidate)
    except InvalidOperation:
        return raw
    if not numeric.is_finite() or "." not in candidate:
        return raw
    fractional = candidate.partition(".")[2]
    if len(fractional) <= 6 and len(candidate) <= 16:
        return raw
    try:
        compact = numeric.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return raw
    rendered = format(compact, "f").rstrip("0").rstrip(".")
    if rendered in {"", "-0", "+0"}:
        rendered = "0"
    return rendered


def _raw_value_detail(widget: Mapping[str, Any]) -> str:
    """Expose an exact long scalar whenever its card uses a compact display."""

    raw_value = widget.get("value")
    if raw_value is None:
        raw_value = widget.get("display_value")
    if raw_value is None:
        return ""
    raw = _raw_display_value(raw_value)
    compact = _compact_scalar_display(raw_value)
    if raw == compact:
        return ""
    return f'<dt>Exact supplied value</dt><dd>{_escape(raw)}</dd>'


def _meta_lines(widget: Mapping[str, Any]) -> str:
    fields = (
        ("Period", widget.get("period")),
        ("Population", widget.get("population")),
        ("Denominator", widget.get("denominator")),
        ("Unit", widget.get("unit")),
        ("Proxy / limit", widget.get("proxy_or_limit") or widget.get("limit")),
    )
    rows = [
        f'<dt>{html.escape(label)}</dt><dd>{_escape(value)}</dd>'
        for label, value in fields
        if value is not None and _text(value) != ""
    ]
    assumptions = _as_list(widget.get("assumptions"))
    limitations = _as_list(widget.get("limitations"))
    if assumptions:
        rows.append(f'<dt>Assumptions</dt><dd>{_escape("; ".join(_text(v) for v in assumptions))}</dd>')
    if limitations:
        rows.append(f'<dt>Limitations</dt><dd>{_escape("; ".join(_text(v) for v in limitations))}</dd>')
    exact_value = _raw_value_detail(widget)
    if exact_value:
        rows.append(exact_value)
    return f'<dl class="widget-meta">{"".join(rows)}</dl>' if rows else ""


def _trace_links(records: Iterable[Mapping[str, str]]) -> str:
    links = [
        f'<a class="trace-link" href="#{_escape(record["anchor"])}">{_escape(record["label"])}</a>'
        for record in records
    ]
    return '<div class="trace-links"><span>Trace:</span> ' + ", ".join(links) + "</div>"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for row in _as_list(value):
        if isinstance(row, Mapping):
            result.append(row)
        else:
            result.append({"label": row, "value": row})
    return result


def _humanize_label(value: Any) -> str:
    """Humanize a presentation field without changing its value semantics."""

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", _text(value)).strip()
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Reviewed value"
    words = []
    for word in text.split(" "):
        words.append(word if word.isupper() or any(char.isdigit() for char in word) else word[:1].upper() + word[1:])
    return " ".join(words)


_MANAGER_ENUM_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_MANAGER_ENUM_LABELS = {
    "one_to_one": "One to one",
    "accepted_with_limits": "Accepted with limits",
    "open_or_unknown": "Open or unknown",
}
_MANAGER_PROSE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)(?![A-Za-z0-9_])")
_MANAGER_PROSE_REFERENCE_TOKENS = frozenset({
    "record_id", "record_hash", "record_ref", "record_refs",
    "integration_record_id", "integration_record_hash", "integration_record_ref", "integration_record_refs",
    "accepted_content_hash", "accepted_manifest_hash", "evidence_ref", "evidence_refs",
    "trace_ref", "trace_refs", "source_ref", "source_refs", "source_hash", "run_id",
})
_MANAGER_PROSE_KNOWN_ACRONYMS = frozenset({
    "API", "AR", "CSV", "ERP", "EUR", "FX", "ID", "JSON", "PO", "REQ", "SAP", "SKU", "SLA", "TMS", "URL", "USD", "VAT", "WMS",
})
_MANAGER_PROSE_OPERATORS = {"GT": ">", "LT": "<", "EQ": "=", "GTE": ">=", "LTE": "<=", "AND": "and", "OR": "or"}
_MANAGER_PROSE_DATE_RE = re.compile(r"^\d{4}_\d{1,2}_\d{1,2}$")
_MANAGER_PROSE_ID_RE = re.compile(r"^(?:REQ|REL|RUN|GEN|G|Q|ITEM|WIDGET)(?:[_-]?\d+)(?:[_-][A-Za-z0-9]+)*$", flags=re.IGNORECASE)
_MANAGER_ENTITY_REF_RE = re.compile(
    r"\b((?:supplier|vendor|customer|warehouse|material|product|invoice|order|delivery|ticket|recovery|case|shipment|return|sku|po)(?:[- ]?[A-Za-z]+)?)"
    r"\s*[:#]\s*[A-Za-z0-9][A-Za-z0-9-]*\b",
    flags=re.IGNORECASE,
)

def _manager_cell_value(value: Any) -> str:
    """Humanize short schema tokens on the manager surface only.

    Internal identifiers, paths, hashes, free prose, and numeric strings stay
    byte-for-byte readable.  The exact value remains in the technical audit;
    this helper is intentionally limited to table/meta/status cells.
    """

    text = _text(value)
    token = text.strip()
    if (
        token
        and len(token) <= 64
        and _MANAGER_PROSE_TOKEN_RE.fullmatch(token)
        and not any(marker in token for marker in ("/", ".", "#"))
    ):
        return _manager_token_label(token)
    return text


def _manager_token_label(token: str) -> str:
    """Render one bounded schema token as sentence-case manager text."""

    lowered = token.lower()
    if lowered in _MANAGER_ENUM_LABELS:
        return _MANAGER_ENUM_LABELS[lowered]
    parts = token.split("_")
    rendered: list[str] = []
    for part in parts:
        upper = part.upper()
        if upper in _MANAGER_PROSE_OPERATORS:
            rendered.append(_MANAGER_PROSE_OPERATORS[upper])
        elif upper in _MANAGER_PROSE_KNOWN_ACRONYMS:
            rendered.append(upper)
        elif part.isdigit():
            rendered.append(part)
        else:
            word = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", part).lower()
            rendered.append(word)
    if not rendered:
        return token
    first_word = next((index for index, value in enumerate(rendered) if value and value[0].isalnum()), None)
    if first_word is not None and rendered[first_word] not in _MANAGER_PROSE_OPERATORS.values() and rendered[first_word] not in _MANAGER_PROSE_KNOWN_ACRONYMS:
        rendered[first_word] = rendered[first_word][:1].upper() + rendered[first_word][1:]
    return " ".join(rendered)


def _manager_prose_text(value: Any) -> str:
    """Humanize bounded schema tokens embedded in visible prose.

    This is presentation-only.  It deliberately leaves path/URL fragments,
    hashes, dates, and record/evidence references untouched; the exact source
    payload remains available in the collapsed technical audit.
    """

    text = _text(value)

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        lowered = token.lower()
        start, end = match.span(1)
        before = text[max(0, start - 80):start]
        after = text[end:end + 16]
        if len(token) > 64 or lowered in _MANAGER_PROSE_REFERENCE_TOKENS or _MANAGER_PROSE_DATE_RE.fullmatch(token) or _MANAGER_PROSE_ID_RE.fullmatch(token):
            return token
        # Preserve full URL/path/file references, but not semantic
        # slash-separated field pairs such as ``source_population/source_coverage``.
        if "://" in before[-80:] or re.search(r"(?:^|[/\\])(?:requirements|work|products|assets|src|tmp|runs?)[/\\]", before, flags=re.IGNORECASE):
            return token
        if after.startswith(".") and re.match(r"\.[A-Za-z0-9]{1,12}(?:\b|$)", after):
            return token
        if "sha256" in before[-80:].lower() or re.search(r"\b(?:hash|digest)\s*[:=]", before[-40:], flags=re.IGNORECASE):
            return token
        return _manager_token_label(token)

    rendered = _MANAGER_PROSE_TOKEN_RE.sub(replace, text)
    # Entity labels frequently arrive as ``Supplier:VEND-000001`` or
    # ``customer-order:SO-005893``.  Keep the supplied business class while
    # removing only the run-local identifier; the exact label remains in the
    # technical audit and no synthetic ordinal/name is introduced.
    return _MANAGER_ENTITY_REF_RE.sub(lambda match: _humanize_label(match.group(1)), rendered)


# Projection fields are copied into a fresh manager-only widget by the V2
# renderer.  They are still immutable reviewed values, but their labels must
# use the same presentation normalizer as every other manager string.  Keep
# raw snapshots untouched: this helper only walks the copy returned by
# ``_manager_surface_widget`` and deliberately preserves references and chart
# geometry/value fields byte-for-byte.
_MANAGER_PROJECTION_REFERENCE_KEYS = frozenset({
    "id", "widget_id", "record_id", "record_ids", "integration_record_id",
    "integration_record_ids", "integration_record_ref", "integration_record_refs",
    "evidence_ref", "evidence_refs", "trace_ref", "trace_refs", "source_ref",
    "source_refs", "path", "paths", "url", "href", "hash", "sha256", "digest",
    "run_id", "generation_id", "requirement_id", "domain_id", "anchor",
})
_MANAGER_PROJECTION_RAW_VALUE_KEYS = frozenset({
    # Numeric/value geometry is never reformatted.  String values such as
    # SALE_RETURN remain readable through the normalizer, while numeric text
    # remains unchanged by _manager_prose_text itself.
    "value", "values", "size", "width", "height", "share", "percent", "rate",
    "ratio", "numerator", "denominator", "count", "total", "amount", "quantity",
    "geometry", "coordinates", "points", "x", "y", "start", "end",
})
_MANAGER_PROJECTION_CONTROL_KEYS = frozenset({
    "type", "kind", "presentation_role", "presentation_tier", "presentation_audience",
    "manager_admission", "manager_anchor", "visual_type", "chart_family",
    "recipe_id", "layout", "renderer_type", "manager_presentation", "explicit_plan_projection", "explicit_visual_projection",
    "explicit_projection_fields", "allowed_visual_fields", "family",
    # Scope is reviewed prose, not a schema token. Preserve it exactly in the
    # manager projection so the context beside a chart remains faithful.
    "scope", "answer_scope", "requirement_scope",
})


def _humanize_manager_projection(value: Any, *, key: str = "") -> Any:
    """Humanize strings in an explicit display projection only.

    The plan/validator has already bound every projection value to an
    authoritative committed payload.  This is therefore a presentation-only
    copy operation: IDs, paths, refs, hashes and numeric/geometry values stay
    exact, while bounded schema tokens in labels, titles, units, statuses and
    prose become readable (including mixed/upper-case snake tokens).
    """

    normalized_key = re.sub(r"[^a-z0-9]+", "_", _text(key).lower()).strip("_")
    if normalized_key in _MANAGER_PROJECTION_CONTROL_KEYS:
        return copy.deepcopy(value)
    if isinstance(value, Mapping):
        return {
            field: _humanize_manager_projection(item, key=_text(field))
            for field, item in value.items()
        }
    if isinstance(value, list):
        return [_humanize_manager_projection(item, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(_humanize_manager_projection(item, key=key) for item in value)
    if not isinstance(value, str):
        return value
    if normalized_key in _MANAGER_PROJECTION_REFERENCE_KEYS:
        return value
    # Numeric and geometry strings are retained exactly.  A semantic enum
    # string (for example SALE_RETURN) is not numeric and is intentionally
    # normalized below when it is supplied as a value/code.
    if normalized_key in _MANAGER_PROJECTION_RAW_VALUE_KEYS and re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?", value.strip()
    ):
        return value
    return _manager_prose_text(value)


def _manager_surface_widget(widget: Mapping[str, Any]) -> dict[str, Any]:
    """Copy manager-facing labels without changing the raw audit widget."""

    manager_presentation = widget.get("manager_presentation")
    explicit_projection = isinstance(manager_presentation, Mapping)
    if isinstance(manager_presentation, Mapping):
        # A selected manager card is a fresh projection, not a sanitized copy
        # of the raw widget.  Keep only immutable envelope/provenance fields;
        # all visible values and geometry must arrive through the plan's
        # pointer-bound display_projection.  The original widget remains
        # available to _render_technical_audit for exact raw review.
        envelope_keys = {
            "id", "type", "kind", "presentation_role", "presentation_tier",
            "manager_admission", "presentation_audience", "requirement_id",
            "requirement_title", "requirement_order", "domain_id", "manager_anchor",
            "integration_record_id", "integration_record_ids",
            "integration_record_ref", "integration_record_refs", "evidence_refs",
            "trace_refs", "manager_presentation", "dashboard_fact",
            "accepted_evidence_fact_sheet", "technical_surface",
            "technical_surface_reason", "accepted_visual", "accepted_evidence",
        }
        display = {key: copy.deepcopy(widget[key]) for key in envelope_keys if key in widget}
    else:
        display = dict(widget)
    # Explicit plan projections are the only source of manager-facing values
    # for admitted widgets.  They are exact values already validated against
    # committed records by the assembler; this overlay prevents a renderer
    # refactor from accidentally falling back to a raw/derived widget field.
    manager_presentation = display.get("manager_presentation")
    if isinstance(manager_presentation, Mapping):
        visual_projection = manager_presentation.get("visual_projection")
        title_projection = manager_presentation.get("title_projection")
        if isinstance(visual_projection, Mapping) and isinstance(title_projection, Mapping) and "value" in title_projection:
            # V2 visual manager entries are projection-only.  Start with the
            # immutable envelope above, then copy only values bound by the
            # chart-map pointers; never inherit raw bars/tiles/geometry from
            # the source widget.
            display["__explicit_plan_projection"] = True
            display["__explicit_visual_projection"] = True
            display["__explicit_projection_fields"] = tuple(visual_projection)
            display["display_title"] = copy.deepcopy(title_projection["value"])
            display["title"] = copy.deepcopy(title_projection["value"])
            for field, binding in visual_projection.items():
                if not isinstance(binding, Mapping) or "value" not in binding:
                    continue
                value = copy.deepcopy(binding["value"])
                if field in {"family", "type"}:
                    if field == "type":
                        display["type"] = value
                    continue
                display[field] = value
            display["visual_type"] = manager_presentation.get("visual_type")
            display["chart_family"] = manager_presentation.get("chart_family")
            recipe_id = _text(manager_presentation.get("recipe_id")).strip()
            renderer_type = _text(manager_presentation.get("renderer_type")).strip()
            if recipe_id:
                display["recipe_id"] = recipe_id
                display["type"] = renderer_type or _RECIPE_RENDER_TYPE.get(recipe_id, display.get("type"))
            if renderer_type:
                display["renderer_type"] = renderer_type
            if _text(manager_presentation.get("layout")).strip():
                display["layout"] = _text(manager_presentation.get("layout"))
            # A table recipe may be selected for a chart whose exact
            # projection is still represented by bars/categories/points.
            # Reuse that supplied collection as rows for the table renderer;
            # this is a presentation copy and never changes the source widget.
            if display.get("type") == "table" and not display.get("rows"):
                for collection in (
                    "bars", "categories", "segments", "points", "series", "values", "data",
                    "cells", "tiles", "stages", "bins", "boxes", "steps",
                ):
                    if isinstance(display.get(collection), list):
                        display["rows"] = copy.deepcopy(display[collection])
                        break
            # A visual projection carries exact chart fields directly (bars,
            # tiles, values, etc.), so no unbound raw collection may leak in.
            # Fact-sheet manager rows already carry syntactic labels and
            # canonical JSON text.  Keep those exact presentation values
            # intact; normalizing JSON keys would make a lossless container
            # unreadable and would no longer match its source projection.
            # The Product plan's pointer-bound projection is already the
            # reviewed manager copy.  Keep titles, columns, rows, values, and
            # geometry byte-for-byte; the renderer may only apply the chosen
            # recipe/layout envelope.
            return display
        projection = manager_presentation.get("display_projection")
        if isinstance(projection, Mapping):
            display["__explicit_plan_projection"] = True
            display["__explicit_projection_fields"] = tuple(projection)
            for field, binding in projection.items():
                if not isinstance(binding, Mapping) or "value" not in binding:
                    continue
                value = copy.deepcopy(binding.get("value"))
                if field == "title":
                    display["display_title"] = value
                    display["title"] = value
                elif field == "body":
                    display["manager_findings"] = [{"finding": value}]
                    display["rows"] = [{"claim": value}]
                elif field in {"value", "display_value", "denominator", "unit", "period", "as_of", "status", "subtitle", "note", "label", "rows", "scope", "answer_scope"}:
                    display[field] = value
            # A denominator is displayed only when it is explicitly supplied
            # by the selected projection.  This is a direct presentation of
            # reviewed fields, never a ratio or inferred population.
            if "value" in projection and "denominator" in projection:
                display["manager_display_value"] = (
                    f"{_text(display.get('value'))} of {_text(display.get('denominator'))}"
                )
        # A scalar plan projection for a legacy progress card is rendered as a
        # KPI snapshot.  The raw progress bars stay solely in the technical
        # audit; no bar size or ratio is inferred from the scalar.
        if _text(display.get("type")).lower() == "progress" and "value" in display and not (
            isinstance(projection, Mapping) and "rows" in projection
        ):
            display["type"] = "kpi"
        recipe_id = _text(manager_presentation.get("recipe_id")).strip()
        renderer_type = _text(manager_presentation.get("renderer_type")).strip()
        if recipe_id:
            display["recipe_id"] = recipe_id
            display["type"] = renderer_type or _RECIPE_RENDER_TYPE.get(recipe_id, display.get("type"))
        if renderer_type:
            display["renderer_type"] = renderer_type
        if _text(manager_presentation.get("layout")).strip():
            display["layout"] = _text(manager_presentation.get("layout"))
        # Explicit plan fields are authoritative and remain exact.  The raw
        # widget, chart map, and audit snapshot are never modified.
    kind = _text(display.get("type") or display.get("kind")).lower()
    if not explicit_projection and kind not in {"table", "status_table"}:
        # Legacy chart envelopes sometimes carry the raw Label/Name/Units
        # projection alongside the real bars/columns/tiles.  It is an audit
        # artifact, not chart geometry; omit it from the manager copy while
        # retaining every exact field in the original widget/audit payload.
        for key in ("rows", "manager_rows"):
            raw_rows = display.get(key)
            if isinstance(raw_rows, list) and raw_rows and all(
                isinstance(row, Mapping)
                and set(_text(k) for k in row) <= {"Label", "Name", "Units", "Value"}
                for row in raw_rows
            ):
                display.pop(key, None)
    if not explicit_projection:
        for key in ("title", "display_title", "label", "denominator_label", "unit", "distinct_unit", "grain"):
            if key in display and display[key] not in (None, ""):
                display[key] = _manager_prose_text(display[key])
    # Visual families use these supplied label-bearing collections.  Copy only
    # their presentation labels; values, geometry, IDs, paths and provenance
    # stay exactly as supplied and the original widget is retained for audit.
    for key in ("bars", "categories", "segments", "points", "series", "values", "data", "tiles"):
        if explicit_projection:
            # No unbound raw collection may enter an admitted manager card.
            # A projection can opt into rows explicitly, but never inherits
            # bars/categories/geometry from the source widget.
            continue
        raw = display.get(key)
        if not isinstance(raw, list):
            continue
        copied: list[Any] = []
        for row in raw:
            if not isinstance(row, Mapping):
                copied.append(row)
                continue
            item = dict(row)
            for label_key in ("label", "name", "title", "x", "period"):
                if label_key in item and item[label_key] not in (None, ""):
                    item[label_key] = _manager_prose_text(item[label_key])
            copied.append(item)
        display[key] = copied
    return display


def _manager_title(widget: Mapping[str, Any]) -> str:
    """Return a short human heading; internal IDs remain audit-only."""

    supplied = _text(widget.get("display_title") or widget.get("title") or widget.get("label")).strip()
    # Requirement identifiers are audit keys, not manager copy.  Strip only a
    # leading identifier plus its separator; preserve the reviewed title text.
    supplied = re.sub(r"^\s*REQ[-_]?\d+\s*(?:[·:|/-]\s*)?", "", supplied, flags=re.IGNORECASE).strip()
    if widget.get("__explicit_plan_projection") is True:
        return supplied or "Reviewed decision view"
    # Preserve an already human-authored phrase (including intentional
    # punctuation/casing such as ``Source-local native cost distribution``).
    # Only normalize schema-like labels below; this keeps reviewed titles
    # faithful while still hiding internal identifiers.
    if supplied and " " in supplied and "_" not in supplied:
        return supplied
    return _humanize_label(supplied or "Reviewed decision view")


def _manager_heading_text(value: Any, default: str = "Decision") -> str:
    """Humanize a visible heading while keeping requirement IDs audit-only."""

    text = _manager_prose_text(value).strip()
    text = re.sub(r"^\s*REQ[-_]?\d+\s*(?:[·:|/-]\s*)?", "", text, flags=re.IGNORECASE).strip()
    return text or default


def _manager_meta_lines(widget: Mapping[str, Any]) -> str:
    explicit_projection = bool(widget.get("__explicit_plan_projection"))
    def first_value(*keys: str) -> Any:
        for key in keys:
            value = widget.get(key)
            if value not in (None, ""):
                return value
        return None

    fields = (
        ("Scope", first_value("scope", "answer_scope", "requirement_scope")),
        ("Period", widget.get("period")),
        ("Population", widget.get("population")),
        ("Denominator", widget.get("denominator")),
        ("Unit", first_value("unit", "units", "distinct_unit")),
        ("Grain", first_value("grain", "data_grain", "row_grain")),
        ("Proxy / limit", first_value("proxy_or_limit", "proxy", "limit")),
        ("Descriptive", first_value("descriptive", "descriptive_only", "causal_status")),
        ("As of", widget.get("as_of")),
        ("Date authority", widget.get("date_authority")),
    )
    rows = []
    for label, value in fields:
        if value is None:
            continue
        if label == "Population" and not explicit_projection:
            # Row-count populations and provenance/date-authority mechanics
            # belong to the audit surface.  ``Period``/``As of`` remain the
            # compact manager context when explicitly supplied.
            continue
        if label == "Scope":
            text = _text(value)
        else:
            text = (
                _text(value)
                if explicit_projection
                else _manager_prose_text(_manager_cell_value(_manager_public_text(value)))
            )
        if text:
            rows.append(f'<span class="manager-meta-item"><b>{_escape(label)}</b> {_escape(text)}</span>')
    limitations: list[str] = []
    for value in _as_list(first_value("limitations", "business_limitations", "limitation")):
        if value in (None, ""):
            continue
        text = _text(value) if explicit_projection else _manager_prose_text(_manager_public_text(value))
        if text and text not in limitations:
            limitations.append(text)
    if limitations:
        rows.append(
            f'<span class="manager-meta-item manager-meta-limitations"><b>Limitations</b> {_escape(" · ".join(limitations))}</span>'
        )
    if not rows:
        return ""
    return '<div class="manager-meta">' + "".join(rows) + "</div>"


def _render_finding_list(widget: Mapping[str, Any]) -> str:
    findings = widget.get("manager_findings")
    if not isinstance(findings, list):
        findings = []
        for row in _rows(widget.get("rows")):
            claim = row.get("claim")
            if claim is None:
                claim = row.get("finding")
            if claim is not None:
                findings.append({"finding": claim, "status": row.get("status"), "period": row.get("period")})
    rendered = []
    for finding in findings:
        if not isinstance(finding, Mapping) or not _text(finding.get("finding")).strip():
            continue
        meta_values: list[str] = []
        for value in (finding.get("status"), finding.get("period")):
            if value in (None, "") or isinstance(value, (Mapping, list, tuple)):
                continue
            rendered_meta = _manager_prose_text(_manager_cell_value(value))
            if rendered_meta:
                meta_values.append(rendered_meta)
        meta = " · ".join(meta_values)
        meta_html = f'<small>{_escape(meta)}</small>' if meta else ""
        finding_text = _manager_prose_text(finding.get("finding"))
        rendered.append(f'<li><span class="finding-mark" aria-hidden="true">•</span><div><p>{_escape(finding_text)}</p>{meta_html}</div></li>')
    if not rendered:
        return '<p class="viz-note">No reviewed findings were supplied.</p>'
    return '<ul class="finding-list">' + "".join(rendered) + "</ul>"


def _manager_public_text(value: Any) -> str:
    """Remove internal hashes/refs from default-visible metadata only.

    Exact reviewed payloads remain available in ``Technical audit``.  This
    presentation boundary prevents source hashes and run-local paths from
    leaking into the manager surface while leaving business prose untouched.
    """

    text = _text(value).strip()
    if not text:
        return ""
    text = re.sub(r"\b(?:source archive|run source|source)\s+(?:sha256|hash)\s*[:=]?\s*[0-9a-f]{64}\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsha256\s*[:=]?\s*[0-9a-f]{64}\b", "", text, flags=re.IGNORECASE)
    return text.strip(" ;,·")


def _manager_key_visible(key: Any) -> bool:
    """Return whether a row key is meaningful on the manager surface."""

    normalized = re.sub(r"[_-]+", " ", _text(key)).strip().lower()
    if not normalized:
        return False
    # Entity/identity keys are mechanics, even when the business entity name
    # itself is useful context.  Do not rely on the broad technical prose
    # matcher for this bounded schema check: a legitimate business field such
    # as ``Order source mix`` must remain eligible.
    if re.search(r"(?:^|\s)(?:id|ids|identifier|identifiers)(?:$|\s)", normalized):
        return False
    return normalized not in {
        "field",
        "path",
        "row kind",
        "value json",
        "data ref",
        "evidence ref",
        "source ref",
        "record id",
        "record hash",
        "accepted content hash",
        "accepted manifest hash",
        "integration record id",
        "integration record hash",
        "trace ref",
    } and not normalized.endswith(" path")


def _manager_value_visible(value: Any) -> bool:
    """Hide run-local asset references from default-visible row cells."""

    if isinstance(value, (Mapping, list, tuple)):
        return False
    text = _text(value).strip()
    if not text:
        return True
    if re.match(r"^(?:requirements|products|extensions|telemetry|work)/", text):
        return False
    if re.search(r"(?:^|/)[^/\s]+\.(?:json|jsonl|csv|py)(?:#|$)", text, flags=re.IGNORECASE):
        return False
    return not bool(re.search(r"\b(?:record|integration)_[a-z_]+\b", text, flags=re.IGNORECASE))


def _manager_row_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, value in row.items():
        if not _manager_key_visible(key) or not _manager_value_visible(value):
            continue
        if value is not None:
            projected[_humanize_label(key)] = value
    return projected


def _manager_table_rows(widget: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """Return an explicit manager projection or sanitize legacy raw rows."""

    # V2 plan projections are source-bound and already selected by the
    # Product Agent.  Return their rows unchanged so no renderer-side field,
    # key, value, or technical-word filter can alter the admitted table.
    if widget.get("__explicit_plan_projection") is True:
        explicit_rows = widget.get("manager_rows")
        if not isinstance(explicit_rows, list):
            explicit_rows = widget.get("rows") or widget.get("data")
        if isinstance(explicit_rows, list):
            return [row if isinstance(row, Mapping) else {"value": row} for row in explicit_rows]
        return None

    explicit = widget.get("manager_rows")
    if isinstance(explicit, list):
        if widget.get("accepted_evidence_fact_sheet") is True:
            rows: list[dict[str, Any]] = []
            for raw in explicit:
                if not isinstance(raw, Mapping):
                    continue
                label = raw.get("label") if "label" in raw else raw.get("Label")
                if label in (None, ""):
                    continue
                if "value" in raw:
                    value = raw.get("value")
                elif "Value" in raw:
                    value = raw.get("Value")
                else:
                    continue
                rows.append({"Label": _humanize_label(label), "Value": value})
            return rows or None
        rows = [_manager_row_projection(row) for row in _rows(explicit)]
        return [row for row in rows if row]
    raw = _rows(widget.get("rows") or widget.get("data"))
    if not raw:
        return None
    forbidden = {"field", "path", "row_kind", "value_json", "source_field", "source_index"}
    if not any(forbidden.intersection(row) for row in raw):
        rows = [_manager_row_projection(row) for row in raw]
        return [row for row in rows if row]
    rows: list[dict[str, Any]] = []
    for row in raw:
        row_kind = _text(row.get("row_kind")).lower()
        if row_kind == "context":
            field = row.get("field")
            value = row.get("value")
            if (
                field in (None, "", "path", "field", "row_kind", "value_json")
                or not _manager_key_visible(field)
                or not _manager_value_visible(value)
                or isinstance(value, (Mapping, list, tuple))
            ):
                continue
            field_text = _text(field)
            if any(token in field_text.lower() for token in ("hash", "evidence", "record_id", "integration", "trace", "path")):
                continue
            rows.append({"Label": _manager_prose_text(_humanize_label(field)), "Value": value})
            continue
        projected = _manager_row_projection(row)
        if projected:
            rows.append(projected)
    return rows or [{"Label": "Reviewed detail", "Value": "See technical audit"}]


def _relationship_manager_rows(widget: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """Group legacy source/target progress bars into one matrix row each."""

    bars = _rows(widget.get("bars"))
    if not bars:
        return None
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index, bar in enumerate(bars, 1):
        label = _text(bar.get("label") or bar.get("name"))
        match = re.match(r"^.*?\s*[·:]\s*(source|target)\s+coverage$", label, flags=re.IGNORECASE)
        side = match.group(1).lower() if match else ("source" if index % 2 else "target")
        if side == "source" or current is None:
            current = {"Relationship": f"Relationship {len(rows) + 1}"}
            rows.append(current)
        current[f"{side.title()} coverage"] = bar.get("display_value", bar.get("value"))
    return rows


def _audit_payload(widget: Mapping[str, Any]) -> Any:
    payload = widget.get("audit_payload")
    if payload is not None:
        return payload
    rows = widget.get("rows") or widget.get("data") or widget.get("bars") or widget.get("tiles")
    if rows is not None:
        return rows
    # Hand-authored/legacy fixtures may not carry an explicit audit_payload.
    # Keep their exact scalar and presentation metadata available in the
    # collapsed audit surface instead of dropping the raw value when the
    # manager card uses a compact display value.
    scalar_fields = (
        "value",
        "display_value",
        "manager_display_value",
        "numerator",
        "denominator",
        "unit",
        "period",
        "population",
        "as_of",
        "date_authority",
        "source",
        "distinct_unit",
    )
    return {
        key: widget[key]
        for key in scalar_fields
        if key in widget and widget[key] is not None
    }


def _render_technical_audit(
    widgets: list[Mapping[str, Any]],
    trace_records: list[dict[str, str]],
    *,
    scope: str = "",
    evidence_prefix: str = "../",
    committed_records: Mapping[str, Mapping[str, Any]] | None = None,
    audit_widget_ids: set[str] | None = None,
) -> str:
    """Render one collapsed technical link surface per requirement.

    Exact committed payloads and raw widget snapshots are rendered once on the
    run-level Data quality & model audit page. Requirement-local blocks link
    into that authoritative inventory instead of duplicating large payloads.
    """

    entries: list[str] = []
    if scope:
        entries.append(f'<section class="audit-scope"><h4>Original reviewed scope</h4><p>{_escape(scope)}</p></section>')
    for widget in widgets:
        record_id = _text(widget.get("integration_record_id") or widget.get("id"))
        widget_id = _text(widget.get("id") or record_id, "widget")
        record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
            if _text(value).strip()
        })
        refs = " ".join(
            f'<a class="trace-link" href="{_escape(evidence_prefix)}evidence.html#{_escape(record["anchor"])}">{_escape(record["label"])}</a>'
            for record in _trace_records(widget)
        )
        if audit_widget_ids is None:
            # Non-V4 hand-authored fixtures predate the separate inventory.
            # Keep their exact local snapshot usable while current V4 sites
            # always pass the explicit inventory IDs and take the link-only
            # path below.
            audit_sections = {
                "committed_record_payload": _audit_payload(widget),
                "widget_snapshot": copy.deepcopy(widget),
            }
            entries.append(
                f'<article class="audit-entry"><h4>{_escape(widget_id)}</h4>'
                f'<div class="audit-record-meta">{_escape(_text(widget.get("presentation_role") or "reviewed output"))} · {refs}</div>'
                f'<pre class="raw-audit">{_escape(json.dumps(audit_sections, ensure_ascii=False, sort_keys=True, indent=2))}</pre></article>'
            )
        else:
            if widget_id not in audit_widget_ids:
                raise ValueError(f"technical audit widget is absent from inventory: {widget_id}")
            entries.append(
                f'<article class="audit-entry audit-widget-ref"><h4>{_escape(widget_id)}</h4>'
                f'<div class="audit-record-meta">{_escape(_text(widget.get("presentation_role") or "reviewed output"))} · '
                f'Records: {_escape(", ".join(record_ids) or "none")} · {refs}</div>'
                f'<a class="audit-widget-link" href="{_escape(evidence_prefix)}data-quality-audit.html#audit-widget-{_escape(_slug(widget_id))}">Open exact widget snapshot</a></article>'
            )
    if not entries and not trace_records:
        return ""
    if trace_records and not entries:
        entries.append('<ul class="audit-trace-list">' + "".join(
            f'<li><a class="trace-link" href="{_escape(evidence_prefix)}evidence.html#{_escape(record["anchor"])}">{_escape(record["label"])}</a><span>{_escape(record["ref"])}</span></li>'
            for record in trace_records
        ) + "</ul>")
    return '<details class="technical-audit"><summary>Technical audit &amp; evidence</summary>' + "".join(entries) + "</details>"


def _visual_gallery_widget(widget: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    """Build a fresh chart widget from one validated V2 visual projection."""

    title_projection = entry.get("title_projection")
    projection = entry.get("visual_projection")
    if not isinstance(title_projection, Mapping) or not isinstance(projection, Mapping):
        raise ValueError(f"visual gallery entry is missing projection: {_text(entry.get('widget_id'))}")
    envelope_keys = {
        "id", "requirement_id", "integration_record_id", "integration_record_ids",
        "integration_record_ref", "integration_record_refs", "evidence_refs", "trace_refs",
        "reviewed_item_ref", "reviewed_output_ref", "presentation_role", "review_status",
        "domain_id", "requirement_title",
    }
    display = {key: copy.deepcopy(widget[key]) for key in envelope_keys if key in widget}
    display["id"] = entry["widget_id"]
    display["type"] = entry["visual_type"]
    display["title"] = copy.deepcopy(title_projection.get("value"))
    display["presentation_role"] = "audit_visual_gallery"
    for field, binding in projection.items():
        if not isinstance(binding, Mapping) or "value" not in binding:
            raise ValueError(f"visual gallery projection is invalid: {entry['widget_id']}:{field}")
        if field not in {"type", "family"}:
            display[field] = copy.deepcopy(binding["value"])
    return display


def _render_visual_gallery(
    widgets: Sequence[Mapping[str, Any]],
    visual_entries: Sequence[Mapping[str, Any]],
    *,
    audience: str,
) -> str:
    """Render every selected visual as an actual chart on one audit gallery."""

    by_id = {_text(widget.get("id")): widget for widget in widgets}
    cards: list[str] = []
    seen: set[str] = set()
    for entry in visual_entries:
        if not isinstance(entry, Mapping) or entry.get("presentation_audience") != audience:
            continue
        widget_id = _text(entry.get("widget_id"))
        if not widget_id or widget_id in seen or widget_id not in by_id:
            raise ValueError(f"visual gallery entry is duplicate or unknown: {widget_id}")
        seen.add(widget_id)
        display = _visual_gallery_widget(by_id[widget_id], entry)
        title = _text(entry.get("title_projection", {}).get("value"), widget_id)
        try:
            content = _render_visual(display)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            content = _render_visual_fallback(display)
        meta = _meta_lines(display)
        cards.append(
            f'<article class="audit-visual-card widget widget-{_escape(_text(entry.get("visual_type")))}" '
            f'data-runtime-card data-runtime-requirement="{_escape(_text(entry.get("requirement_id")))}" data-runtime-domain="{_escape(_slug(display.get("domain_id") or ""))}" data-widget-id="{_escape(widget_id)}" id="audit-visual-{_escape(_slug(widget_id))}"><span class="eyebrow">{_escape(_text(entry.get("requirement_id")))}</span>'
            f'<h3>{_escape(title)}</h3>{content}{meta}</article>'
        )
    if not cards:
        return ""
    return '<section class="visual-gallery" id="technical-visual-gallery"><div class="section-head"><h2>Technical visual gallery</h2><p>Exact reviewed charts retained for model and data-quality review.</p></div><div class="chart-grid">' + "".join(cards) + "</div></section>"


def _render_record_audit(records: Sequence[Mapping[str, Any]], *, evidence_prefix: str = "") -> tuple[str, list[dict[str, str]]]:
    """Render the authoritative committed-record audit and its trace links."""

    entries: list[str] = []
    traces: list[dict[str, str]] = []
    seen_trace: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        record_id = _text(record.get("record_id") or record.get("item_id"), "Reviewed record")
        payload = record.get("committed_record_payload", record.get("payload"))
        if payload is None and record.get("payload_json") is not None:
            try:
                payload = json.loads(_text(record.get("payload_json")))
            except (TypeError, json.JSONDecodeError):
                payload = record.get("payload_json")
        refs = _reference_values(record.get("reference_union"))
        if not refs:
            refs = _reference_values(record.get("evidence_refs")) + _reference_values(record.get("trace_refs"))
        widget_ids = _reference_values(record.get("widget_ids"))
        trace_links: list[str] = []
        for trace in _trace_records({"id": record_id, "trace_refs": refs}):
            if trace["anchor"] not in seen_trace:
                traces.append(trace)
                seen_trace.add(trace["anchor"])
            trace_links.append(
                f'<a class="trace-link" href="{_escape(evidence_prefix)}evidence.html#{_escape(trace["anchor"])}">{_escape(trace["label"])}</a>'
            )
        metadata = " · ".join(([_text(record.get("item_id")), _text(record.get("kind")), ", ".join(widget_ids)])).strip(" ·,")
        entries.append(
            f'<article class="audit-entry audit-record-entry"><h4>{_escape(record_id)}</h4>'
            f'<div class="audit-record-meta">{_escape(metadata)} · {" ".join(trace_links)}</div>'
            f'<h5>Committed record payload (exact)</h5>'
            f'<pre class="raw-audit committed-record-payload">{_escape(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))}</pre></article>'
        )
    if not entries:
        return "", traces
    return '<details class="technical-audit"><summary>Committed integration records (exact)</summary>' + "".join(entries) + "</details>", traces


def _render_widget_audit(
    entries: Sequence[Mapping[str, Any]],
    *,
    evidence_prefix: str = "",
) -> tuple[str, list[dict[str, str]]]:
    """Render one exact raw snapshot for each dashboard widget."""

    rendered: list[str] = []
    traces: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_trace: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("audit_widgets entries must be objects")
        widget_id = _text(entry.get("widget_id")).strip()
        snapshot = entry.get("widget_snapshot")
        if not widget_id or widget_id in seen_ids or not isinstance(snapshot, Mapping):
            raise ValueError(f"audit widget entry is invalid: {widget_id or '<missing>'}")
        if _text(snapshot.get("id")) != widget_id:
            raise ValueError(f"audit widget snapshot ID mismatch: {widget_id}")
        seen_ids.add(widget_id)
        record_ids = sorted({
            _text(value).strip()
            for value in _as_list(entry.get("record_ids"))
            if _text(value).strip()
        })
        refs = _reference_values(entry.get("reference_union"))
        if not refs:
            refs = _reference_values(entry.get("evidence_refs")) + _reference_values(entry.get("trace_refs"))
        links: list[str] = []
        for trace in _trace_records({"id": widget_id, "trace_refs": refs}):
            if trace["anchor"] not in seen_trace:
                traces.append(trace)
                seen_trace.add(trace["anchor"])
            links.append(
                f'<a class="trace-link" href="{_escape(evidence_prefix)}evidence.html#{_escape(trace["anchor"])}">{_escape(trace["label"])}</a>'
            )
        rendered.append(
            f'<article class="audit-entry audit-widget-entry" id="audit-widget-{_escape(_slug(widget_id))}">'
            f'<h4>{_escape(widget_id)}</h4>'
            f'<div class="audit-record-meta">Requirement: {_escape(_text(entry.get("requirement_id")))} · '
            f'Records: {_escape(", ".join(record_ids) or "none")} · {" ".join(links)}</div>'
            f'<h5>Widget snapshot (exact)</h5>'
            f'<pre class="raw-audit widget-snapshot">{_escape(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2))}</pre></article>'
        )
    if not rendered:
        return "", traces
    return '<details class="technical-audit"><summary>Dashboard widgets (exact)</summary>' + "".join(rendered) + "</details>", traces


def _render_kpi(widget: Mapping[str, Any]) -> str:
    value = widget.get("value")
    if value is None:
        value = widget.get("display_value")
    if widget.get("manager_display_value") not in (None, ""):
        value = widget.get("manager_display_value")
    return f'<div class="kpi-value">{_escape(_compact_scalar_display(value))}</div>'


def _render_bar(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("bars") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if not rows:
        return '<p class="viz-note">No reviewed values were supplied for this bar view.</p>'
    rendered = []
    for index, row in enumerate(rows):
        series_geometry = (
            _fact_series_geometry_markup(
                row,
                scale_groups=widget.get("scale_groups"),
            )
            if isinstance(row.get("series"), list)
            else None
        )
        if series_geometry is not None:
            label = _fact_row_label(row, index + 1)
            series_html, aria_value = series_geometry
            rendered.append(
                f'<div class="viz-row viz-row-series" role="img" aria-label="{_escape(label)}: {_escape(aria_value)}">'
                f'<span class="viz-label">{_escape(label)}</span>'
                f'<span class="viz-series-list">{series_html}</span>'
                f'<span class="viz-value">{_escape(aria_value)}</span></div>'
            )
            continue
        supplied_size = next((row[key] for key in ("size", "width", "share", "percent") if key in row), None)
        if supplied_size is None:
            raise ValueError(f"bar row {index + 1} requires supplied size: {_text(widget.get('id'), 'dashboard fact')}")
        normalized_size = _normalize_percent(supplied_size, f"bar row {index + 1}")
        style = f' style="--bar-size:{_escape(normalized_size)}"'
        label = row.get("label") or row.get("name")
        value = _display_value(row.get("display_value", row.get("value")))
        signed = row.get("signed_size")
        bar_class = ""
        if signed is not None:
            signed_value = _normalize_signed_percent(signed, f"bar row {index + 1}")
            if float(signed_value[:-1]) < 0:
                bar_class = " viz-bar-negative"
        rendered.append(
            '<div class="viz-row" role="img" aria-label="{label}: {value}"><span class="viz-label">{label}</span>'
            '<span class="viz-track"><span class="viz-bar{bar_class}"{style}></span></span>'
            '<span class="viz-value">{value}</span></div>'.format(
                label=_escape(label),
                bar_class=bar_class,
                style=style,
                value=_escape(value),
            )
        )
    return '<div class="viz viz-bar-list">' + "".join(rendered) + "</div>"


def _render_column(widget: Mapping[str, Any]) -> str:
    raw_rows = _as_list(widget.get("bars") or widget.get("rows") or widget.get("categories") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("column rows must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        raise ValueError("column requires non-empty rows")
    rendered = []
    for index, row in enumerate(rows):
        series_geometry = (
            _fact_series_geometry_markup(
                row,
                column=True,
                raw_values=_text(widget.get("geometry_mode")).strip().lower() == "raw_counts",
                scale_groups=widget.get("scale_groups"),
            )
            if isinstance(row.get("series"), list)
            else None
        )
        if series_geometry is not None:
            label = _fact_row_label(row, index + 1)
            series_html, aria_value = series_geometry
            rendered.append(
                f'<div class="column-item column-item-series" role="img" aria-label="{_escape(label)}: {_escape(aria_value)}">'
                f'<span class="column-series-list">{series_html}</span>'
                f'<span class="column-label">{_escape(label)}</span></div>'
            )
            continue
        supplied_size = next((row[key] for key in ("size", "height", "share", "percent") if key in row), None)
        if supplied_size is None:
            raise ValueError(f"column row {index + 1} requires supplied size: {_text(widget.get('id'), 'dashboard fact')}")
        normalized_size = _normalize_percent(supplied_size, f"column row {index + 1}")
        label, value = _required_visual_row(row, f"column row {index + 1}")
        signed = row.get("signed_size")
        bar_class = ""
        if signed is not None:
            signed_value = _normalize_signed_percent(signed, f"column row {index + 1}")
            if float(signed_value[:-1]) < 0:
                bar_class = " column-bar-negative"
        rendered.append(
            f'<div class="column-item" role="img" aria-label="{_escape(label)}: {_escape(value)}">'
            f'<span class="column-value">{_escape(value)}</span>'
            f'<span class="column-track"><span class="column-bar{bar_class}" style="--column-size:{_escape(normalized_size)}"></span></span>'
            f'<span class="column-label">{_escape(label)}</span></div>'
        )
    return '<div class="viz viz-column" role="list">' + "".join(rendered) + "</div>"


def _render_lollipop(widget: Mapping[str, Any]) -> str:
    raw_rows = _as_list(widget.get("bars") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("lollipop rows must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        raise ValueError("lollipop requires non-empty rows")
    rendered = []
    for index, row in enumerate(rows):
        supplied_size = next((row[key] for key in ("size", "width", "share", "percent") if key in row), None)
        if supplied_size is None:
            raise ValueError(f"lollipop row {index + 1} requires supplied size")
        normalized_size = _normalize_percent(supplied_size, f"lollipop row {index + 1}")
        label, value = _required_visual_row(row, f"lollipop row {index + 1}")
        rendered.append(
            f'<div class="lollipop-row" role="img" aria-label="{_escape(label)}: {_escape(value)}">'
            f'<span class="lollipop-label">{_escape(label)}</span>'
            f'<span class="lollipop-track"><span class="lollipop-stem" style="--lollipop-size:{_escape(normalized_size)}"><span class="lollipop-dot" aria-hidden="true"></span></span></span>'
            f'<span class="lollipop-value">{_escape(value)}</span></div>'
        )
    return '<div class="viz viz-lollipop" role="list">' + "".join(rendered) + "</div>"


def _render_line(widget: Mapping[str, Any]) -> str:
    """Render supplied temporal points as a labelled, accessible SVG chart.

    Accepted visual rows are deliberately kept flat and lossless.  The small
    amount of normalization below only identifies the period/series/value
    fields that the declaration already supplied; numeric conversion is used
    solely for SVG coordinates.  The exact values remain in the adjacent data
    table so a multi-series chart never becomes an unlabeled list or silently
    drops a period.
    """

    def numeric(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(float(value)) else None
        text = _text(value).strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def period_value(row: Mapping[str, Any]) -> Any:
        for key in ("period", "time", "month", "date", "year", "x", "label", "name"):
            value = row.get(key)
            if value not in (None, ""):
                return value
        return ""

    def value_value(row: Mapping[str, Any]) -> Any:
        for key in ("display_value", "y", "value", "count", "amount", "measure"):
            value = row.get(key)
            if value not in (None, ""):
                return value
        return None

    def series_value(row: Mapping[str, Any], default: Any = None) -> Any:
        supplied = row.get("series")
        if supplied not in (None, "") and not isinstance(supplied, (list, Mapping)):
            return supplied
        for key in ("series_name", "measure_name", "name"):
            value = row.get(key)
            if value not in (None, ""):
                return value
        return default if default not in (None, "") else "Value"

    # Prefer explicit point/row containers.  A top-level series declaration is
    # also accepted when it contains nested point arrays; string declarations
    # are used only as names for the supplied row fields.
    raw_container: Any = None
    for key in ("points", "rows", "values", "data"):
        if isinstance(widget.get(key), list):
            raw_container = widget.get(key)
            break
    declared_series = [
        _text(value).strip()
        for value in _as_list(widget.get("series"))
        if isinstance(value, str) and value.strip()
    ]
    if raw_container is None and isinstance(widget.get("series"), list):
        nested = [value for value in widget.get("series", []) if isinstance(value, Mapping)]
        if nested and any(isinstance(value.get("points") or value.get("rows") or value.get("values"), list) for value in nested):
            expanded: list[dict[str, Any]] = []
            for series in nested:
                series_name = series.get("name") or series.get("label") or series.get("series") or "Value"
                points = series.get("points") or series.get("rows") or series.get("values") or []
                for point in points:
                    if isinstance(point, Mapping):
                        expanded.append({**dict(point), "series": series_name})
            raw_container = expanded
        elif nested:
            raw_container = nested
    raw_rows = _as_list(raw_container)
    points: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            points.append({"period": raw, "series": "Value", "value": raw})
            continue
        nested_series = raw.get("series")
        if isinstance(nested_series, list) and nested_series and all(isinstance(item, Mapping) for item in nested_series):
            for item in nested_series:
                item_map = dict(item)
                period = period_value(raw)
                value = value_value(item_map)
                if period in (None, "") or value is None:
                    continue
                points.append({"period": period, "series": series_value(item_map), "value": value})
            continue
        # A declared list of measure names expands one row into one point per
        # measure, preserving the exact row order and supplied field values.
        if declared_series and value_value(raw) is None:
            period = period_value(raw)
            for name in declared_series:
                if name in raw and raw.get(name) not in (None, ""):
                    points.append({"period": period, "series": name, "value": raw.get(name)})
            continue
        points.append({
            "period": period_value(raw),
            "series": series_value(raw, declared_series[0] if len(declared_series) == 1 else None),
            "value": value_value(raw),
        })
    points = [point for point in points if point.get("period") not in (None, "") and point.get("value") is not None]

    # A single supplied point is a snapshot, not a trend.  Keep the value
    # visible while explicitly declining to draw a misleading line.
    if len(points) < 2:
        if not points:
            return '<p class="viz-note">Trend unavailable: no reviewed points were supplied.</p>'
        point = points[0]
        return (
            '<div class="viz-snapshot"><span class="eyebrow">Snapshot only</span>'
            f'<strong>{_escape(_display_value(point.get("value")))}</strong>'
            f'<span>{_escape(point.get("period"))}</span><p class="viz-note">Not enough reviewed points to infer a trend.</p></div>'
        )

    periods: list[str] = []
    for point in points:
        period = _text(point.get("period"))
        if period not in periods:
            periods.append(period)
    series_names: list[str] = []
    for point in points:
        name = _text(point.get("series"), "Value") or "Value"
        if name not in series_names:
            series_names.append(name)
    numeric_points = [numeric(point.get("value")) for point in points]
    numeric_values = [value for value in numeric_points if value is not None]
    rows_for_table = [
        {"Period": point.get("period"), "Series": point.get("series"), "Value": point.get("value")}
        for point in points
    ]
    unit = _text(widget.get("unit") or widget.get("units")).strip()
    table_widget = {"type": "table", "rows": rows_for_table}
    data_table = _render_table(table_widget, rows_override=rows_for_table)
    unit_caption = f'<caption>{_escape(unit)}</caption>' if unit else ""
    # ``_render_table`` intentionally leaves columns generic and exact.  Add
    # a caption only when the declaration supplied a unit; no unit is guessed.
    if unit_caption:
        data_table = data_table.replace("<table>", f"<table>{unit_caption}", 1)
    data_table = f'<div class="line-data">{data_table}</div>'
    if len(numeric_values) < 2:
        return (
            '<div class="viz viz-line viz-line-table">'
            '<p class="viz-note">Reviewed points are shown as a labelled table; numeric coordinates were not supplied for a line plot.</p>'
            f'{data_table}</div>'
        )

    # A reviewed limitation can explicitly require separate scales (for
    # example when orders and order-lines use different units).  Honor that
    # declaration with one labelled panel per series; no index, ratio, or
    # other comparison is calculated here.
    independent_axes = any(
        re.search(r"\bseparate axes?\b", _text(value), flags=re.IGNORECASE)
        for value in _as_list(widget.get("limitations"))
    )
    width, height = 720, 300
    left, right, top, bottom = 56, 18, 26, 58
    plot_width = width - left - right
    plot_height = height - top - bottom
    low, high = min(numeric_values), max(numeric_values)
    if low == high:
        low -= 1.0
        high += 1.0

    def x_for(period: str) -> float:
        if len(periods) <= 1:
            return float(left)
        return left + periods.index(period) * plot_width / (len(periods) - 1)

    def y_for(value: float) -> float:
        return top + (high - value) * plot_height / (high - low)

    palette = ("#076f75", "#1f6f9d", "#b97811", "#a43b47", "#78909e", "#4f7391")
    label_step = max(1, math.ceil(len(periods) / 8))
    chart_title = _text(widget.get("title"), "Reviewed trend")
    chart_id = _slug(widget.get("id") or chart_title)

    def x_label_markup(panel_height: int, x_for_period: Any) -> str:
        labels: list[str] = []
        for index, period in enumerate(periods):
            if index not in {0, len(periods) - 1} and index % label_step:
                continue
            labels.append(
                f'<text x="{x_for_period(period):.2f}" y="{panel_height - 22}" text-anchor="middle" fill="#536170" font-size="11">{_escape(period)}</text>'
            )
        return "".join(labels)

    if independent_axes and len(series_names) > 1:
        panel_height = 236
        panel_top, panel_bottom = 22, 50
        panel_plot_height = panel_height - panel_top - panel_bottom
        panel_plot_width = width - left - right
        panels: list[str] = []
        for series_index, name in enumerate(series_names):
            color = palette[series_index % len(palette)]
            series_points = [
                point for point in points
                if _text(point.get("series"), "Value") == name and numeric(point.get("value")) is not None
            ]
            series_values = [numeric(point.get("value")) for point in series_points]
            if not series_values:
                continue
            panel_low, panel_high = min(series_values), max(series_values)
            if panel_low == panel_high:
                panel_low -= 1.0
                panel_high += 1.0

            def panel_x(period: str) -> float:
                if len(periods) <= 1:
                    return float(left)
                return left + periods.index(period) * panel_plot_width / (len(periods) - 1)

            def panel_y(value: float) -> float:
                return panel_top + (panel_high - value) * panel_plot_height / (panel_high - panel_low)

            grid_markup: list[str] = []
            for grid_index in range(3):
                grid_value = panel_high - (panel_high - panel_low) * grid_index / 2
                y = panel_y(grid_value)
                grid_markup.append(
                    f'<line x1="{left}" x2="{width - right}" y1="{y:.2f}" y2="{y:.2f}" stroke="#d9e1e8" stroke-width="1" />'
                    f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" fill="#536170" font-size="11">{_escape(f"{grid_value:g}")}</text>'
                )
            coordinates = " ".join(
                f"{panel_x(_text(point.get('period'))):.2f},{panel_y(numeric(point.get('value'))):.2f}"
                for point in series_points
            )
            line_markup = (
                f'<polyline class="line-series" points="{coordinates}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" data-series="{_escape(name)}" />'
                if len(series_points) >= 2 else ""
            )
            point_markup = "".join(
                f'<circle class="line-point" cx="{panel_x(_text(point.get("period"))):.2f}" cy="{panel_y(numeric(point.get("value"))):.2f}" r="3.5" fill="{color}" data-series="{_escape(name)}" data-period="{_escape(point.get("period"))}" aria-label="{_escape(f"{name} · {_text(point.get('period'))}: {_display_value(point.get('value'))}")}"><title>{_escape(f"{name} · {_text(point.get('period'))}: {_display_value(point.get('value'))}")}</title></circle>'
                for point in series_points
            )
            panel_id = f"{chart_id}-{series_index + 1}"
            panel_title = f"{chart_title} · {name}" if chart_title else name
            unit_label = _escape(unit) if unit else "supplied units"
            panels.append(
                f'<div class="line-panel"><h4>{_escape(panel_title)}</h4>'
                f'<svg class="line-chart line-chart-panel" viewBox="0 0 {width} {panel_height}" role="img" aria-labelledby="line-title-{_slug(panel_id)} line-summary-{_slug(panel_id)}" style="width:100%;height:auto">'
                f'<title id="line-title-{_slug(panel_id)}">{_escape(panel_title)}</title><desc id="line-summary-{_slug(panel_id)}">{_escape(panel_title)}; values retain their supplied {unit_label}.</desc>'
                f'<g aria-hidden="true">{"".join(grid_markup)}<line x1="{left}" x2="{left}" y1="{panel_top}" y2="{panel_height - panel_bottom}" stroke="#aabac4" /><line x1="{left}" x2="{width - right}" y1="{panel_height - panel_bottom}" y2="{panel_height - panel_bottom}" stroke="#aabac4" /></g>'
                f'{line_markup}{point_markup}{x_label_markup(panel_height, panel_x)}</svg></div>'
            )
        if panels:
            return f'<div class="viz viz-line"><div class="line-panels">{"".join(panels)}</div>{data_table}</div>'

    geometry: list[str] = []
    legend: list[str] = []
    grid: list[str] = []
    for index in range(5):
        value = high - (high - low) * index / 4
        y = y_for(value)
        label = _escape(f"{value:g}")
        grid.append(
            f'<line x1="{left}" x2="{width - right}" y1="{y:.2f}" y2="{y:.2f}" stroke="#d9e1e8" stroke-width="1" />'
            f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" fill="#536170" font-size="11">{label}</text>'
        )
    x_labels: list[str] = []
    for index, period in enumerate(periods):
        if index not in {0, len(periods) - 1} and index % label_step:
            continue
        x = x_for(period)
        x_labels.append(
            f'<text x="{x:.2f}" y="{height - 22}" text-anchor="middle" fill="#536170" font-size="11">{_escape(period)}</text>'
        )
    for series_index, name in enumerate(series_names):
        color = palette[series_index % len(palette)]
        series_points = [point for point in points if _text(point.get("series"), "Value") == name and numeric(point.get("value")) is not None]
        if len(series_points) >= 2:
            coordinates = " ".join(f"{x_for(_text(point.get('period'))):.2f},{y_for(numeric(point.get('value'))):.2f}" for point in series_points)
            geometry.append(
                f'<polyline class="line-series" points="{coordinates}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" data-series="{_escape(name)}" />'
            )
        for point in series_points:
            x = x_for(_text(point.get("period")))
            y = y_for(numeric(point.get("value")))
            exact = f"{name} · {_text(point.get('period'))}: {_display_value(point.get('value'))}"
            geometry.append(
                f'<circle class="line-point" cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="{color}" data-series="{_escape(name)}" data-period="{_escape(point.get("period"))}" aria-label="{_escape(exact)}"><title>{_escape(exact)}</title></circle>'
            )
        legend.append(
            f'<li><span class="line-key" style="background:{color}" aria-hidden="true"></span><span>{_escape(name)}</span></li>'
        )
    title = chart_title
    unit_suffix = f" ({unit})" if unit else ""
    summary = "; ".join(
        f"{_text(point.get('series'), 'Value')} {_text(point.get('period'))}: {_display_value(point.get('value'))}"
        for point in points
    )
    aria = f"{title}{unit_suffix}: {summary}"
    svg = (
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" aria-labelledby="line-title-{chart_id} line-summary-{chart_id}" style="width:100%;height:auto">'
        f'<title id="line-title-{chart_id}">{_escape(title)}</title><desc id="line-summary-{chart_id}">{_escape(aria)}</desc>'
        f'<g aria-hidden="true">{"".join(grid)}<line x1="{left}" x2="{left}" y1="{top}" y2="{height - bottom}" stroke="#aabac4" /><line x1="{left}" x2="{width - right}" y1="{height - bottom}" y2="{height - bottom}" stroke="#aabac4" /></g>'
        f'{"".join(geometry)}{"".join(x_labels)}</svg>'
    )
    legend_html = f'<ul class="line-legend" role="list">{"".join(legend)}</ul>'
    return f'<div class="viz viz-line"><div class="line-chart-wrap">{svg}</div>{legend_html}{data_table}</div>'


def _render_stacked(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("segments") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if not rows:
        return '<p class="viz-note">No reviewed values were supplied for this composition.</p>'
    geometry = []
    legend = []
    aria_parts = []
    title = _text(widget.get("title"), "Stacked composition")
    cumulative = 0.0
    for index, row in enumerate(rows):
        supplied_size = next((row[key] for key in ("size", "share", "percent") if key in row), None)
        if supplied_size is None:
            raise ValueError(f"stacked row {index + 1} requires supplied size")
        normalized_size = _normalize_percent(supplied_size, f"stacked row {index + 1}")
        percent = float(normalized_size[:-1])
        start = cumulative
        cumulative += percent
        if cumulative > 100.000001:
            raise ValueError("stacked segment percentages cannot exceed 100")
        start_text = f"{start:.6f}".rstrip("0").rstrip(".") or "0"
        width_text = f"{percent:.6f}".rstrip("0").rstrip(".") or "0"
        label, value = _required_visual_row(row, f"stacked row {index + 1}")
        aria_parts.append(f"{label}: {value} ({normalized_size})")
        geometry.append(
            f'<rect class="stack-segment stack-segment-{index}" x="{_escape(start_text)}" y="0" '
            f'width="{_escape(width_text)}" height="10" data-start="{_escape(start_text)}" '
            f'data-size="{_escape(normalized_size)}" data-series-key="stack-{index}" aria-hidden="true"></rect>'
        )
        legend.append(
            f'<li><span class="stack-key stack-key-{index}" aria-hidden="true"></span>'
            f'<button type="button" class="series-toggle" data-series-toggle="stack-{index}" aria-pressed="true"><span class="stack-label">{_escape(label)}</span></button>'
            f'<strong>{_escape(value)}</strong><small>{_escape(normalized_size)}</small></li>'
        )
    aria = f"{title}: " + "; ".join(aria_parts)
    stack_id = _slug(widget.get("id") or title)
    return (
        f'<div class="viz viz-stacked">'
        f'<svg class="stacked-strip" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" '
        f'aria-label="{_escape(aria)}" aria-labelledby="stacked-title-{stack_id} stacked-summary-{stack_id}">'
        f'<title id="stacked-title-{stack_id}">{_escape(title)}</title>'
        f'<desc id="stacked-summary-{stack_id}">{_escape(aria)}</desc>'
        f'<g aria-hidden="true">{"".join(geometry)}</g></svg>'
        f'<ul class="stack-legend" role="list">{"".join(legend)}</ul></div>'
    )


def _render_heatmap(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("cells") or widget.get("values") or widget.get("data"))
    if not rows:
        return '<p class="viz-note">No reviewed values were supplied for this matrix.</p>'
    rendered = []
    for row in rows:
        intensity = row.get("intensity") or row.get("level") or row.get("class")
        classes = "heat-cell" + (" " + _slug(intensity) if intensity else "")
        rendered.append(
            f'<span class="{_escape(classes)}" title="{_escape(row.get("label") or row.get("name"))}">'
            f'{_escape(_display_value(row.get("display_value", row.get("value"))))}</span>'
        )
    return '<div class="viz viz-heatmap" role="img" aria-label="reviewed matrix">' + "".join(rendered) + "</div>"


def _render_scatter(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("points") or widget.get("rows") or widget.get("data"))
    if not rows:
        return '<p class="viz-note">No reviewed points were supplied for this view.</p>'
    rendered = []
    for row in rows:
        rendered.append(
            '<li><span class="scatter-dot" aria-hidden="true"></span>'
            '<span>{label}</span><span>x={x}; y={y}</span></li>'.format(
                label=_escape(row.get("label") or row.get("name")),
                x=_escape(_display_value(row.get("x"))),
                y=_escape(_display_value(row.get("y"))),
            )
        )
    return '<ul class="viz viz-scatter">' + "".join(rendered) + "</ul>"


def _normalize_percent(value: Any, label: str, *, require_percent_string: bool = False) -> str:
    """Return a safe CSS percentage from one bounded supplied value."""

    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} requires a bounded percent")
    if isinstance(value, str):
        text = value.strip()
        if not text.endswith("%"):
            raise ValueError(f"{label} requires a supplied percent string")
        raw: Any = text[:-1].strip()
    elif not require_percent_string and isinstance(value, (int, float)):
        raw = value
    else:
        raise ValueError(f"{label} requires a supplied percent string")
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} requires a numeric percent") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > 100:
        raise ValueError(f"{label} percent must be between 0 and 100")
    normalized = f"{parsed:.6f}".rstrip("0").rstrip(".") or "0"
    return normalized + "%"


def _supplied_percent(value: Any, label: str) -> float:
    """Parse one explicitly supplied percentage without deriving a metric."""

    normalized = _normalize_percent(value, label, require_percent_string=True)
    return float(normalized[:-1])


def _normalize_signed_percent(value: Any, label: str) -> str:
    """Return a safe signed presentation percentage for diverging geometry."""

    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} requires a bounded signed percent")
    if isinstance(value, str):
        text = value.strip()
        if not text.endswith("%"):
            raise ValueError(f"{label} requires a supplied signed percent string")
        raw: Any = text[:-1].strip()
    elif isinstance(value, (int, float)):
        raw = value
    else:
        raise ValueError(f"{label} requires a supplied signed percent string")
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} requires a numeric signed percent") from exc
    if not math.isfinite(parsed) or parsed < -100 or parsed > 100:
        raise ValueError(f"{label} signed percent must be between -100 and 100")
    normalized = f"{parsed:.6f}".rstrip("0").rstrip(".") or "0"
    return normalized + "%"


def _donut_categories(widget: Mapping[str, Any]) -> tuple[Any, str, list[tuple[Mapping[str, Any], float]]]:
    denominator_key = next(
        (
            key
            for key in ("denominator_value", "denominator", "total")
            if key in widget and widget.get(key) not in (None, "")
        ),
        None,
    )
    if denominator_key is None:
        raise ValueError("donut requires explicit denominator_value")
    denominator_label = _text(widget.get("denominator_label") or widget.get("denominator_name"))
    if not denominator_label:
        raise ValueError("donut requires explicit denominator_label")
    categories = widget.get("categories") or widget.get("rows") or widget.get("segments")
    if not isinstance(categories, list) or not 2 <= len(categories) <= 5:
        raise ValueError("donut requires 2 to 5 categories")
    validated: list[tuple[Mapping[str, Any], float]] = []
    total = 0.0
    for index, category in enumerate(categories):
        if not isinstance(category, Mapping):
            raise ValueError(f"donut category {index + 1} must be an object")
        label = _text(category.get("label") or category.get("name"))
        value_key = next(
            (
                key
                for key in ("value", "display_value", "count", "amount", "measure")
                if key in category and category.get(key) not in (None, "")
            ),
            None,
        )
        size_key = next(
            (
                key
                for key in ("size", "percent", "share")
                if key in category and category.get(key) not in (None, "")
            ),
            None,
        )
        if not label or value_key is None or size_key is None:
            raise ValueError(f"donut category {index + 1} requires label, value, and size")
        normalized = dict(category)
        normalized.setdefault("label", label)
        normalized.setdefault("value", category[value_key])
        normalized.setdefault("size", category[size_key])
        percent = _supplied_percent(normalized["size"], f"donut category {index + 1}")
        validated.append((normalized, percent))
        total += percent
    if abs(total - 100.0) > 0.5:
        raise ValueError(f"donut category percentages must total approximately 100 (got {total:g})")
    return widget.get(denominator_key), denominator_label, validated


def _render_donut(widget: Mapping[str, Any]) -> str:
    denominator, denominator_label, categories = _donut_categories(widget)
    radius = 44.0
    circumference = 2.0 * math.pi * radius
    palette = ("#087f86", "#1f6f9d", "#b97811", "#168aa5", "#78909e")
    offset = 0.0
    segments = []
    legend = []
    for index, (category, percent) in enumerate(categories):
        length = circumference * percent / 100.0
        segments.append(
            f'<circle class="donut-segment donut-segment-{index}" cx="60" cy="60" r="{radius:g}" '
            f'stroke="{palette[index]}" stroke-dasharray="{length:.3f} {circumference:.3f}" '
            f'stroke-dashoffset="{-offset:.3f}" data-series-key="donut-{index}"></circle>'
        )
        label = _text(category.get("label"))
        value = _display_value(category.get("display_value", category.get("value")))
        size = _text(category.get("size"))
        legend.append(
            f'<li><span class="donut-key donut-key-{index}" aria-hidden="true"></span>'
            f'<button type="button" class="series-toggle" data-series-toggle="donut-{index}" aria-pressed="true"><span class="donut-label">{_escape(label)}</span></button><strong>{_escape(value)}</strong>'
            f'<small>{_escape(size)}</small></li>'
        )
        offset += length
    aria = f"{_text(widget.get('title'), 'Reviewed composition')}: {denominator} {denominator_label}"
    return (
        f'<div class="viz viz-donut" data-supplied="true">'
        f'<div class="donut-visual"><svg class="donut-ring" viewBox="0 0 120 120" role="img" '
        f'aria-label="{_escape(aria)}"><title>{_escape(aria)}</title>'
        f'<circle class="donut-track" cx="60" cy="60" r="{radius:g}"></circle>'
        f'<g transform="rotate(-90 60 60)">{"".join(segments)}</g></svg>'
        f'<div class="donut-center"><strong>{_escape(_display_value(denominator))}</strong>'
        f'<span>{_escape(denominator_label)}</span></div></div>'
        f'<ul class="donut-legend">{"".join(legend)}</ul></div>'
    )


def _render_waffle(widget: Mapping[str, Any]) -> str:
    denominator, denominator_label, categories = _donut_categories(widget)
    title = _text(widget.get("title"), "Reviewed composition")
    bounds = []
    cumulative = 0.0
    cells = []
    legend = []
    for index, (category, percent) in enumerate(categories):
        cumulative += percent
        bounds.append(cumulative)
        legend.append(
            f'<li><span class="waffle-key waffle-key-{index}" aria-hidden="true"></span>'
            f'<span class="waffle-label">{_escape(category.get("label"))}</span>'
            f'<strong>{_escape(_display_value(category.get("display_value", category.get("value"))))}</strong>'
            f'<small>{_escape(category.get("size"))}</small></li>'
        )
    for cell_index in range(100):
        midpoint = cell_index + 0.5
        category_index = next((index for index, bound in enumerate(bounds) if midpoint <= bound), len(categories) - 1)
        cells.append(f'<span class="waffle-cell waffle-cell-{category_index}" aria-hidden="true" data-cell="{cell_index}"></span>')
    summary = "; ".join(
        f"{_text(category.get('label'))}: {_display_value(category.get('display_value', category.get('value')))} ({_text(category.get('size'))})"
        for category, _percent in categories
    )
    aria = f"{title}: {denominator} {denominator_label}; {summary}"
    waffle_id = _slug(widget.get("id") or title)
    return (
        f'<div class="viz viz-waffle">'
        f'<div class="waffle-grid" role="img" aria-label="{_escape(aria)}" aria-labelledby="waffle-title-{waffle_id} waffle-summary-{waffle_id}">'
        f'<span id="waffle-title-{waffle_id}" class="sr-only">{_escape(title)}</span>'
        f'<span id="waffle-summary-{waffle_id}" class="sr-only">{_escape(aria)}</span>{"".join(cells)}</div>'
        f'<ul class="waffle-legend" role="list">{"".join(legend)}</ul></div>'
    )


def _render_diverging_bar(widget: Mapping[str, Any]) -> str:
    raw_rows = _as_list(widget.get("bars") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("diverging_bar rows must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        raise ValueError("diverging_bar requires non-empty rows")
    geometry = []
    legend = []
    aria_parts = []
    row_height = 28
    for index, row in enumerate(rows):
        if "signed_size" not in row:
            raise ValueError(f"diverging row {index + 1} requires supplied signed_size")
        signed = _normalize_signed_percent(row.get("signed_size"), f"diverging row {index + 1}")
        signed_value = float(signed[:-1])
        magnitude = abs(signed_value) / 2.0
        x = 50.0 - magnitude if signed_value < 0 else 50.0
        x_text = f"{x:.6f}".rstrip("0").rstrip(".") or "0"
        width_text = f"{magnitude:.6f}".rstrip("0").rstrip(".") or "0"
        label, value = _required_visual_row(row, f"diverging row {index + 1}")
        aria_parts.append(f"{_text(label)}: {value} ({signed})")
        geometry.append(
            f'<rect class="diverging-mark" x="{_escape(x_text)}" y="{index * row_height + 9}" '
            f'width="{_escape(width_text)}" height="10" data-signed-size="{_escape(signed)}" aria-hidden="true"></rect>'
        )
        legend.append(
            f'<li><span class="diverging-key" aria-hidden="true"></span><span>{_escape(label)}</span>'
            f'<strong>{_escape(value)}</strong><small>{_escape(signed)}</small></li>'
        )
    title = _text(widget.get("title"), "Signed comparison")
    aria = f"{title}: " + "; ".join(aria_parts)
    widget_id = _slug(widget.get("id") or title)
    height = max(28, row_height * len(rows))
    return (
        f'<div class="viz viz-diverging"><svg class="diverging-svg" viewBox="0 0 100 {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="{_escape(aria)}" aria-labelledby="diverging-title-{widget_id} diverging-summary-{widget_id}">'
        f'<title id="diverging-title-{widget_id}">{_escape(title)}</title><desc id="diverging-summary-{widget_id}">{_escape(aria)}</desc>'
        f'<line class="diverging-zero" x1="50" x2="50" y1="0" y2="{height}"></line>{"".join(geometry)}</svg>'
        f'<ul class="diverging-legend" role="list">{"".join(legend)}</ul></div>'
    )


def _progress_rows(widget: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], float]]:
    rows = _rows(widget.get("bars"))
    if not rows:
        raise ValueError("progress requires non-empty bars")
    validated = []
    for index, row in enumerate(rows):
        if "size" not in row:
            raise ValueError(f"progress row {index + 1} requires supplied size")
        validated.append((row, float(_normalize_percent(row.get("size"), f"progress row {index + 1}")[:-1])))
    return validated


def _render_progress(widget: Mapping[str, Any]) -> str:
    rendered = []
    for row, _percent in _progress_rows(widget):
        label = row.get("label") or row.get("name")
        value = _display_value(row.get("display_value", row.get("value")))
        size = _normalize_percent(row.get("size"), f"progress row {len(rendered) + 1}")
        rendered.append(
            f'<div class="progress-row" role="img" aria-label="{_escape(label)}: {_escape(value)} ({_escape(size)})">'
            f'<div class="progress-heading"><span class="viz-label">{_escape(label)}</span>'
            f'<span class="viz-value">{_escape(value)} · {_escape(size)}</span></div>'
            f'<span class="progress-track"><span class="progress-fill" style="--progress-size:{_escape(size)}"></span></span></div>'
        )
    return '<div class="viz viz-progress">' + "".join(rendered) + "</div>"


def _render_leaderboard(widget: Mapping[str, Any]) -> str:
    rows = _rows(widget.get("bars"))
    if not rows:
        raise ValueError("leaderboard requires non-empty bars")
    rendered = []
    for rank, row in enumerate(rows, start=1):
        size = _normalize_percent(row.get("size"), f"leaderboard row {rank}")
        style = f' style="--bar-size:{_escape(size)}"'
        label = row.get("label") or row.get("name")
        value = _display_value(row.get("display_value", row.get("value")))
        rendered.append(
            f'<div class="leaderboard-row"><span class="leaderboard-rank" aria-label="Rank {rank}">{rank}</span>'
            f'<span class="leaderboard-label">{_escape(label)}</span><span class="viz-track"><span class="viz-bar"{style}></span></span>'
            f'<span class="viz-value">{_escape(value)}</span></div>'
        )
    return '<div class="viz viz-leaderboard" role="list">' + "".join(rendered) + "</div>"


def _render_metric_grid(widget: Mapping[str, Any]) -> str:
    """Render exact source-partition tiles without comparative geometry."""

    rows = widget.get("tiles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("metric_grid requires non-empty tiles")
    rendered = []
    geometry_keys = ("size", "width", "share", "percent")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"metric_grid tile {index + 1} must be an object")
        if any(key in row for key in geometry_keys):
            raise ValueError(f"metric_grid tile {index + 1} cannot include geometry fields")
        label = _text(row.get("label") or row.get("name"))
        if not label or "value" not in row or row.get("value") in (None, ""):
            raise ValueError(f"metric_grid tile {index + 1} requires label and value")
        value = _display_value(row.get("display_value", row.get("value")))
        denominator = row.get("denominator")
        # A reviewed numerator/denominator may be shown as a compact
        # ``value of denominator`` label.  This is a direct presentation of
        # supplied fields, never a derived ratio or percentage.
        if denominator not in (None, "") and " of " not in value:
            value = f"{value} of {_display_value(denominator)}"
        context: list[str] = []
        if row.get("unit") not in (None, ""):
            context.append(_manager_prose_text(_manager_cell_value(row.get("unit"))))
        if row.get("period") not in (None, ""):
            context.append(_manager_prose_text(_manager_cell_value(row.get("period"))))
        context_html = f'<small class="metric-context">{_escape(" · ".join(context))}</small>' if context else ""
        aria = f"{label}: {value}" + (f" ({' · '.join(context)})" if context else "")
        rendered.append(
            f'<div class="metric-tile" role="listitem" aria-label="{_escape(aria)}">'
            f'<span class="metric-label">{_escape(label)}</span>'
            f'<strong class="metric-value">{_escape(value)}</strong>{context_html}</div>'
        )
    return '<div class="viz viz-metric-grid" role="list">' + "".join(rendered) + "</div>"


def _render_table(
    widget: Mapping[str, Any],
    *,
    rows_override: list[Mapping[str, Any]] | None = None,
    manager_view: bool = False,
) -> str:
    """Render the supplied rows without changing their meaning.

    The site renderer may ask for a short preview of a large audit table, so
    this low-level helper accepts an explicit row slice.  The single-page
    renderer continues to call it without an override and therefore keeps
    the complete reviewed table visible.
    """

    rows = _table_input_rows(widget, manager_view=manager_view) if rows_override is None else rows_override
    if not rows:
        return '<p class="table-empty">No reviewed rows were supplied for this table.</p>'
    columns = _as_list(widget.get("manager_columns" if manager_view else "columns"))
    if not columns and rows:
        columns = list(dict.fromkeys(key for row in rows for key in row.keys()))
    columns = [_text(column) for column in columns]
    exact_projection = manager_view and widget.get("__explicit_plan_projection") is True
    head = "".join(f"<th>{_escape(column if exact_projection else _humanize_label(column) if manager_view else column)}</th>" for column in columns)
    body = []
    for row in rows:
        def cell_value(column: str) -> str:
            value = row.get(column)
            if exact_projection:
                return _text(value)
            if manager_view and widget.get("accepted_evidence_fact_sheet") is True:
                return _display_value(value)
            return _manager_prose_text(_manager_cell_value(value)) if manager_view else _text(value)

        body.append(
            "<tr>"
            + "".join(
                f"<td>{_escape(cell_value(column))}</td>"
                for column in columns
            )
            + "</tr>"
        )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _render_funnel(widget: Mapping[str, Any]) -> str:
    """Render supplied process stages without deriving conversion rates."""

    raw_rows = _as_list(widget.get("stages") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("funnel rows must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        return '<p class="viz-note">No reviewed stages were supplied for this funnel.</p>'
    rendered: list[str] = []
    for index, row in enumerate(rows, 1):
        supplied_size = next((row[key] for key in ("size", "width", "share", "percent") if key in row), None)
        if supplied_size is None:
            raise ValueError(f"funnel stage {index} requires supplied size")
        size = _normalize_percent(supplied_size, f"funnel stage {index}")
        label, value = _required_visual_row(row, f"funnel stage {index}")
        rendered.append(
            f'<div class="funnel-step" role="img" aria-label="{_escape(label)}: {_escape(value)}">'
            f'<span class="funnel-label">{_escape(label)}</span>'
            f'<span class="funnel-track"><span class="funnel-fill" style="--funnel-size:{_escape(size)}"></span></span>'
            f'<strong class="funnel-value">{_escape(value)}</strong></div>'
        )
    return '<div class="viz viz-funnel" role="list">' + "".join(rendered) + "</div>"


def _render_histogram(widget: Mapping[str, Any]) -> str:
    """Render supplied bins; counts are never normalized by the renderer."""

    raw_rows = _as_list(widget.get("bins") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("histogram bins must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        return '<p class="viz-note">No reviewed bins were supplied for this histogram.</p>'
    rendered: list[str] = []
    for index, row in enumerate(rows, 1):
        supplied_size = next((row[key] for key in ("size", "height", "share", "percent") if key in row), None)
        if supplied_size is None:
            raise ValueError(f"histogram bin {index} requires supplied size")
        size = _normalize_percent(supplied_size, f"histogram bin {index}")
        label = _text(row.get("label") or row.get("bin") or row.get("name"), f"Bin {index}")
        value = _display_value(row.get("display_value", row.get("count", row.get("value"))))
        rendered.append(
            f'<div class="histogram-bin" role="img" aria-label="{_escape(label)}: {_escape(value)}">'
            f'<span class="histogram-value">{_escape(value)}</span>'
            f'<span class="histogram-track"><span class="histogram-fill" style="--histogram-size:{_escape(size)}"></span></span>'
            f'<span class="histogram-label">{_escape(label)}</span></div>'
        )
    return '<div class="viz viz-histogram" role="list">' + "".join(rendered) + "</div>"


def _render_box_plot(widget: Mapping[str, Any]) -> str:
    """Render exact five-number summaries as a compact evidence-bound table."""

    raw_rows = _as_list(widget.get("boxes") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("box plot rows must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        return '<p class="viz-note">No reviewed distributions were supplied for this box plot.</p>'
    required = ("min", "q1", "median", "q3", "max")
    for index, row in enumerate(rows, 1):
        if any(key not in row or row.get(key) in (None, "") for key in required):
            raise ValueError(f"box plot row {index} requires min, q1, median, q3, and max")
    columns = ("label", *required)
    head = "".join(f"<th>{_escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(
            f'<td>{_escape(row.get(column) if column == "label" else _display_value(row.get(column)))}</td>'
            for column in columns
        ) + "</tr>"
        for index, row in enumerate(rows, 1)
    )
    return f'<div class="viz viz-box-plot table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _render_pareto(widget: Mapping[str, Any]) -> str:
    """Render bars plus reviewer-supplied cumulative values."""

    raw_rows = _as_list(widget.get("bars") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("pareto rows must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        return '<p class="viz-note">No reviewed categories were supplied for this Pareto view.</p>'
    rendered: list[str] = []
    for index, row in enumerate(rows, 1):
        supplied_size = next((row[key] for key in ("size", "width", "share", "percent") if key in row), None)
        cumulative = next((row[key] for key in ("cumulative_size", "cumulative_percent", "cumulative_share") if key in row), None)
        if supplied_size is None or cumulative is None:
            raise ValueError(f"pareto row {index} requires supplied size and cumulative value")
        size = _normalize_percent(supplied_size, f"pareto row {index}")
        cumulative_display = _display_value(cumulative)
        label, value = _required_visual_row(row, f"pareto row {index}")
        rendered.append(
            f'<div class="pareto-row" role="img" aria-label="{_escape(label)}: {_escape(value)}; cumulative {_escape(cumulative_display)}">'
            f'<span class="pareto-label">{_escape(label)}</span><span class="pareto-track"><span class="pareto-bar" style="--pareto-size:{_escape(size)}"></span></span>'
            f'<span class="pareto-value">{_escape(value)}</span><small class="pareto-cumulative">{_escape(cumulative_display)}</small></div>'
        )
    return '<div class="viz viz-pareto" role="list">' + "".join(rendered) + "</div>"


def _render_waterfall(widget: Mapping[str, Any]) -> str:
    """Render an additive bridge only when every supplied boundary is present."""

    raw_rows = _as_list(widget.get("steps") or widget.get("rows") or widget.get("values") or widget.get("data"))
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("waterfall rows must be objects")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if not rows:
        return '<p class="viz-note">No reviewed bridge steps were supplied for this waterfall.</p>'
    # ``start``/``end`` are the canonical fields.  The explicit aliases are
    # accepted for source-local chart maps, but no subtotal or change is
    # calculated from them.
    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        start_key = next((key for key in ("start", "start_value", "start_size") if key in row and row.get(key) not in (None, "")), None)
        end_key = next((key for key in ("end", "end_value", "end_size") if key in row and row.get(key) not in (None, "")), None)
        if start_key is None or end_key is None:
            raise ValueError(f"waterfall step {index} requires supplied start and end")
        normalized = dict(row)
        normalized["start"] = row[start_key]
        normalized["end"] = row[end_key]
        if "change" not in normalized:
            for key in ("delta", "change_value", "change_size"):
                if key in row and row.get(key) not in (None, ""):
                    normalized["change"] = row[key]
                    break
        normalized_rows.append(normalized)
    rows = normalized_rows
    columns = ("label", "start", "change", "end")
    head = "".join(f"<th>{_escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(
            f'<td>{_escape(row.get(column) if column == "label" else _display_value(row.get(column)))}</td>'
            for column in columns
        ) + "</tr>"
        for index, row in enumerate(rows, 1)
    )
    return f'<div class="viz viz-waterfall table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _table_input_rows(widget: Mapping[str, Any], *, manager_view: bool = False) -> list[Mapping[str, Any]]:
    """Select an already supplied row container without reshaping values.

    A table fallback is the local degradation path for an unsupported or
    malformed chart.  It must retain the actual reviewed container (for
    example ``points`` or ``bins``), rather than claiming that no rows exist
    merely because the source did not call them ``rows``.
    """

    keys = (
        ("manager_rows",) if manager_view else ()
    ) + (
        "rows", "data", "bars", "categories", "segments", "points", "series",
        "cells", "tiles", "stages", "bins", "boxes", "steps", "values",
        "manager_rows",
    )
    for key in keys:
        value = widget.get(key)
        if isinstance(value, list):
            rows = _rows(value)
            if rows:
                return rows
    return []


def _render_visual_fallback(widget: Mapping[str, Any], *, manager_view: bool = False) -> str:
    """Keep one bad visual local to an evidence-bound table/empty state."""

    rows = _table_input_rows(widget, manager_view=manager_view)
    if rows:
        table_widget = dict(widget)
        table_widget["type"] = "table"
        table_widget["rows"] = rows
        return (
            '<div class="visual-fallback"><p class="viz-note">Visual unavailable; exact reviewed rows remain available below.</p>'
            + _render_table(table_widget, rows_override=rows, manager_view=manager_view)
            + "</div>"
        )
    return '<div class="visual-fallback"><p class="viz-note">Visual unavailable: no reviewed rows were supplied.</p></div>'


def _is_large_audit_table(widget: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> bool:
    """Identify presentation-heavy audit tables without interpreting values.

    Explicit fixture hints win, while the row-count threshold is a bounded
    presentation guard for raw dumps that do not carry a hint.  Business
    status tables and reviewed claims remain directly visible when they are
    small; only their large/raw form receives a collapsed full-detail copy.
    """

    if len(rows) > 12:
        return True
    for key in ("audit_detail", "full_detail", "detail_only", "raw_table", "raw_rows"):
        if widget.get(key) is True:
            return True
    if widget.get("sample_policy") is not None or widget.get("sample_rows") is not None:
        return True
    return False


def _render_site_table(widget: Mapping[str, Any]) -> str:
    """Render a useful table preview and keep only full audit detail collapsed."""

    manager_rows = _manager_table_rows(widget)
    rows = _rows(manager_rows)
    # A Product-plan projection is the exact selected manager surface.  Never
    # truncate or collapse its rows; audit-only/legacy tables may still use a
    # compact preview below.
    if widget.get("__explicit_plan_projection") is True:
        return _render_table(widget, rows_override=rows, manager_view=manager_rows is not None)
    if not _is_large_audit_table(widget, rows):
        return _render_table(widget, rows_override=rows, manager_view=manager_rows is not None)

    preview_count = min(8, len(rows))
    preview = _render_table(widget, rows_override=rows[:preview_count], manager_view=manager_rows is not None)
    full = _render_table(widget, rows_override=rows, manager_view=manager_rows is not None)
    total = len(rows)
    return (
        f'<div class="table-preview" data-preview-rows="{preview_count}" data-total-rows="{total}">'
        f'{preview}</div>'
        f'<details class="data-detail"><summary>Open full reviewed detail ({total} rows)</summary>{full}</details>'
    )


def _render_visual(widget: Mapping[str, Any]) -> str:
    kind = _text(widget.get("type") or widget.get("kind"), "kpi").lower()
    if kind == "kpi":
        return _render_kpi(widget)
    if kind == "bar":
        return _render_bar(widget)
    if kind == "column":
        return _render_column(widget)
    if kind == "lollipop":
        return _render_lollipop(widget)
    if kind == "diverging_bar":
        return _render_diverging_bar(widget)
    if kind == "waffle":
        return _render_waffle(widget)
    if kind == "line":
        return _render_line(widget)
    if kind == "area":
        return '<div class="viz viz-area">' + _render_line(widget) + "</div>"
    if kind == "stacked_area":
        return '<div class="viz viz-stacked-area">' + _render_stacked(widget) + "</div>"
    if kind in {"grouped_bar", "stacked_bar", "normalized_stacked_bar"}:
        if kind == "grouped_bar":
            return _render_bar(widget)
        return _render_stacked(widget)
    if kind == "stacked_composition":
        return _render_stacked(widget)
    if kind == "heatmap":
        return _render_heatmap(widget)
    if kind == "scatter":
        return _render_scatter(widget)
    if kind == "donut":
        return _render_donut(widget)
    if kind == "pie":
        return _render_donut(widget)
    if kind == "funnel":
        return _render_funnel(widget)
    if kind == "histogram":
        return _render_histogram(widget)
    if kind == "box_plot":
        return _render_box_plot(widget)
    if kind == "pareto":
        return _render_pareto(widget)
    if kind == "waterfall":
        return _render_waterfall(widget)
    if kind == "progress":
        return _render_progress(widget)
    if kind == "leaderboard":
        return _render_leaderboard(widget)
    if kind in {"metric_grid", "kpi_grid"}:
        if not isinstance(widget.get("tiles"), list) or not widget.get("tiles"):
            # Technical-only projections can legitimately have no manager
            # tiles after admission filtering.  Keep the audit card usable
            # without manufacturing a placeholder metric.
            return '<p class="viz-note">Technical detail is available in the audit.</p>'
        return _render_metric_grid(widget)
    if kind in {"table", "status_table"}:
        return _render_table(widget)
    raise ValueError(f"unsupported widget type: {kind}")


def _ordered_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} requires a positive integer order")
    return value


def _normalize_domains(fixture: Mapping[str, Any], widgets: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    supplied = fixture.get("domains")
    if not isinstance(supplied, list) or not supplied:
        raise ValueError("fixture requires non-empty ordered domains metadata")
    widget_ids = {_text(widget.get("id")) for widget in widgets}
    widget_by_id = {_text(widget.get("id")): widget for widget in widgets}
    seen_widget_ids: dict[str, tuple[str, str]] = {}
    domains: list[dict[str, Any]] = []
    domain_ids: set[str] = set()
    domain_orders: list[int] = []
    for domain in supplied:
        if not isinstance(domain, Mapping):
            raise ValueError("each domain must be an object")
        domain_id = _text(domain.get("id"))
        if not domain_id or domain_id in domain_ids:
            raise ValueError("domains require unique non-empty ids")
        if "decision_flows" in domain:
            raise ValueError(f"domain {domain_id or '<unknown>'} must use singular decision_flow")
        domain_ids.add(domain_id)
        domain_order = _ordered_positive_int(domain.get("order"), f"domain {domain_id}")
        domain_orders.append(domain_order)
        title = _text(domain.get("title"))
        if not title:
            raise ValueError(f"domain {domain_id} requires a title")
        decisions = domain.get("decision_flow")
        if not isinstance(decisions, list) or not decisions:
            raise ValueError(f"domain {domain_id} requires non-empty decision_flow metadata")
        flow_ids: set[str] = set()
        flow_orders: list[int] = []
        normalized_decisions: list[dict[str, Any]] = []
        for flow in decisions:
            if not isinstance(flow, Mapping):
                raise ValueError(f"domain {domain_id} has invalid decision_flow entry")
            if "widgets" in flow:
                raise ValueError(f"decision flow {domain_id} requires widget_ids")
            flow_id = _text(flow.get("id"))
            if not flow_id or flow_id in flow_ids:
                raise ValueError(f"domain {domain_id} requires unique decision-flow ids")
            flow_ids.add(flow_id)
            flow_order = _ordered_positive_int(flow.get("order"), f"decision flow {domain_id}/{flow_id}")
            flow_orders.append(flow_order)
            flow_title = _text(flow.get("title"))
            if not flow_title:
                raise ValueError(f"decision flow {domain_id}/{flow_id} requires a title")
            assigned = flow.get("widget_ids")
            if not isinstance(assigned, list) or not assigned or any(not isinstance(item, str) or not item.strip() for item in assigned):
                raise ValueError(f"decision flow {domain_id}/{flow_id} requires non-empty widget_ids")
            for widget_id in assigned:
                if widget_id not in widget_ids:
                    raise ValueError(f"decision flow {domain_id}/{flow_id} references unknown widget {widget_id}")
                if widget_id in seen_widget_ids:
                    raise ValueError(f"widget {widget_id} is assigned more than once")
                seen_widget_ids[widget_id] = (domain_id, flow_id)
            normalized_decisions.append({**flow, "id": flow_id, "order": flow_order, "title": flow_title, "widget_ids": list(assigned)})
        if sorted(flow_orders) != list(range(1, len(flow_orders) + 1)):
            raise ValueError(f"domain {domain_id} decision-flow orders must be contiguous from 1")
        ordered_decisions = sorted(normalized_decisions, key=lambda flow: flow["order"])
        if _raw_requirement_title(title) and len(ordered_decisions) == 1:
            flow = ordered_decisions[0]
            candidates = [flow.get("title")]
            candidates.extend(
                widget_by_id.get(widget_id, {}).get("requirement_title")
                for widget_id in _as_list(flow.get("widget_ids"))
            )
            replacement = next((candidate for candidate in candidates if _usable_manager_title(candidate)), "")
            if replacement:
                title = replacement
        domains.append({**domain, "id": domain_id, "order": domain_order, "title": title, "decision_flow": ordered_decisions})
    if sorted(domain_orders) != list(range(1, len(domain_orders) + 1)):
        raise ValueError("domain orders must be contiguous from 1")
    if set(seen_widget_ids) != widget_ids:
        missing = sorted(widget_ids - set(seen_widget_ids))
        raise ValueError(f"widgets require valid domain/decision-flow assignment: {missing}")
    for widget in widgets:
        widget_id = _text(widget.get("id"))
        domain_id, flow_id = seen_widget_ids[widget_id]
        if "domain_id" in widget and _text(widget.get("domain_id")) != domain_id:
            raise ValueError(f"widget {widget_id} has unknown or mismatched domain assignment")
        if "decision_flow_id" in widget and _text(widget.get("decision_flow_id")) != flow_id:
            raise ValueError(f"widget {widget_id} has unknown or mismatched decision-flow assignment")
    return sorted(domains, key=lambda domain: domain["order"])


def _raw_requirement_title(value: Any) -> bool:
    text = _text(value).strip()
    return bool(re.fullmatch(r"REQ[-_]?\d+", text, flags=re.IGNORECASE))


def _usable_manager_title(value: Any) -> bool:
    text = _text(value).strip()
    if not text or _raw_requirement_title(text):
        return False
    return not bool(re.fullmatch(r"[a-z][a-z0-9_]*", text))


def _ordered_widgets(widgets: list[Mapping[str, Any]], domains: list[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]]:
    by_id = {_text(widget.get("id")): widget for widget in widgets}
    emitted: set[str] = set()
    output: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    for domain in domains:
        for flow in _as_list(domain.get("decision_flow")):
            if not isinstance(flow, Mapping):
                continue
            ids = flow.get("widget_ids")
            for widget_id in ids:
                key = _text(widget_id.get("id") if isinstance(widget_id, Mapping) else widget_id)
                widget = by_id.get(key)
                if widget is not None and key not in emitted:
                    output.append((domain, flow, widget))
                    emitted.add(key)
    if len(emitted) != len(widgets):
        raise ValueError("every widget requires a valid ordered domain/decision-flow assignment")
    return output


_FAILED_ITEM_LIMITATION = (
    "This requirement did not contribute analytical values to the dashboard. "
    "Its accepted output remains preserved, and no analytics are fabricated from this item."
)


def _render_failed_items(fixture: Mapping[str, Any]) -> str:
    """Render every terminal item state without exposing its raw failure reason.

    ``failed_items`` is a fixture-owned lifecycle projection, not an analytical
    widget.  Keep the status and recovery state visible on the audit surface,
    while allowing only stable hash/reference metadata through for diagnosis.
    Unknown fields (including a raw ``reason``) are deliberately ignored.
    """

    raw_items = fixture.get("failed_items")
    if raw_items is None:
        return ""
    if not isinstance(raw_items, (list, tuple)):
        raise ValueError("fixture failed_items must be a list")
    if not raw_items:
        return ""

    entries: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise ValueError(f"fixture failed_items entry {index} must be an object")
        item_id = _text(item.get("item_id")).strip()
        status = _text(item.get("status")).strip()
        recovery_exhausted = item.get("recovery_exhausted")
        if not item_id or not status or not isinstance(recovery_exhausted, bool):
            raise ValueError(
                "fixture failed_items entries require item_id, status, and boolean recovery_exhausted"
            )

        metadata: list[str] = []
        manifest_ref = _text(item.get("manifest_ref")).strip()
        if manifest_ref:
            metadata.append(f'<dt>manifest_ref</dt><dd><code>{_escape(manifest_ref)}</code></dd>')
        manifest_hash = _text(item.get("manifest_hash")).strip()
        if manifest_hash:
            metadata.append(f'<dt>manifest_hash</dt><dd><code>{_escape(manifest_hash)}</code></dd>')
        reason_hash = _text(item.get("reason_hash")).strip()
        if reason_hash:
            metadata.append(f'<dt>reason_hash</dt><dd><code>{_escape(reason_hash)}</code></dd>')
        metadata_html = f'<dl class="failed-item-metadata">{"".join(metadata)}</dl>' if metadata else ""
        entries.append(
            f'<li class="failed-item" data-failed-item="{_escape(item_id)}">'
            f'<dl class="failed-item-state">'
            f'<dt>item_id</dt><dd>{_escape(item_id)}</dd>'
            f'<dt>status</dt><dd>{_escape(status)}</dd>'
            f'<dt>recovery_exhausted</dt><dd>{_escape(json.dumps(recovery_exhausted))}</dd>'
            f'</dl><p class="failed-item-limitation">{_escape(_FAILED_ITEM_LIMITATION)}</p>'
            f'{metadata_html}</li>'
        )
    return (
        '<section class="limits failed-items" aria-labelledby="failed-items-heading">'
        '<h2 id="failed-items-heading">Failed requirements and limitations</h2>'
        '<p>Terminal item states are shown for completeness; they are not analytical results.</p>'
        f'<ul>{"".join(entries)}</ul>'
        '</section>'
    )


_HTML_ID_RE = re.compile(r'\bid=["\']([^"\']+)["\']')
_HTML_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']')


def _scan_html_links(document: str | bytes) -> tuple[set[str], list[str]]:
    """Extract anchors and links with one bounded pass per attribute type.

    Site validation used to rescan every target document once for every
    fragment link pointing at it.  Keeping the two existing attribute
    patterns, but materializing their results once per page, preserves the
    renderer's link semantics while making validation proportional to the
    total serialized site size and number of links.
    """

    if isinstance(document, bytes):
        document = document.decode("utf-8")
    return set(_HTML_ID_RE.findall(document)), _HTML_HREF_RE.findall(document)


def _validate_link_values(anchors: set[str], links: Iterable[str]) -> None:
    for href in links:
        if href.startswith("#"):
            if href[1:] not in anchors:
                raise ValueError(f"broken internal dashboard link: {href}")
        elif href.startswith(("http://", "https://", "//")):
            raise ValueError(f"offline dashboard cannot contain external link: {href}")


def _validate_links(document: str | bytes) -> None:
    anchors, links = _scan_html_links(document)
    _validate_link_values(anchors, links)


def render_dashboard(
    fixture: Mapping[str, Any],
    *,
    context: RunContext | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return ``(html, manifest)`` for a reviewed fixture."""

    freeze_markers = validate_product_manifest(fixture)
    raw_widgets = fixture.get("widgets") or fixture.get("items")
    if not isinstance(raw_widgets, list) or not raw_widgets:
        raise ValueError("fixture must contain a non-empty widgets list")
    widgets: list[Mapping[str, Any]] = []
    for widget in raw_widgets:
        if not isinstance(widget, Mapping):
            raise ValueError("each widget must be an object")
        kind = _text(widget.get("type") or widget.get("kind"), "kpi").lower()
        if kind not in SUPPORTED_WIDGETS:
            raise ValueError(f"unsupported widget type: {kind}")
        if not widget.get("id"):
            raise ValueError("every widget requires a stable id")
        _validate_widget_provenance(widget)
        widgets.append(widget)
    registry_info = _validate_v4_chart_assets(fixture, widgets, context)
    domains = _normalize_domains(fixture, widgets)
    ordered = _ordered_widgets(widgets, domains)
    accepted_review = fixture.get("accepted_final_product_review")
    surface_status = (
        "Reviewed"
        if isinstance(accepted_review, Mapping)
        and _text(accepted_review.get("status")).lower() in {"accepted", "approved", "final"}
        else "Preview"
    )

    trace_records: list[dict[str, str]] = []
    trace_seen: set[str] = set()
    cards: list[str] = []
    sections: list[str] = []
    manifest_items: list[dict[str, Any]] = []
    for domain, flow, widget in ordered:
        widget_id = _slug(widget.get("id"))
        kind = _text(widget.get("type") or widget.get("kind"), "kpi").lower()
        records = _trace_records(widget)
        for record in records:
            if record["anchor"] not in trace_seen:
                trace_records.append(record)
                trace_seen.add(record["anchor"])
        trace = _trace_links(records)
        title = _text(widget.get("title") or widget.get("label"), widget_id)
        try:
            content = _render_visual(widget)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            # A malformed or unsupported visual must not blank the full
            # dashboard. Keep exact supplied rows visible as a fallback.
            content = _render_visual_fallback(widget)
        meta = _meta_lines(widget)
        review_status = surface_status
        # Keep the machine review state in the manifest while using a neutral
        # visible label.  Candidate bytes are immutable before Product Review
        # and may be promoted later; a literal ``Preview`` badge would become
        # stale in that accepted artifact.
        review = '<p class="review-status">Source-bound output</p>' if review_status else ""
        block = (
            f'<article class="widget widget-{_escape(kind)}" id="widget-{_escape(widget_id)}">'
            f'<h3>{_escape(title)}</h3>{content}{meta}{review}{trace}</article>'
        )
        if kind == "kpi":
            cards.append(block)
        else:
            sections.append(block)
        manifest_items.append(
            {
                "element_id": f"widget-{widget_id}",
                "kind": "kpi" if kind == "kpi" else kind,
                "title": title,
                "reviewed_item_ref": _text(widget.get("reviewed_item_ref")),
                "reviewed_output_ref": _text(widget.get("reviewed_output_ref")),
                "evidence_refs": _reference_values(widget.get("evidence_refs")),
                "trace_refs": _widget_trace_refs(widget),
                "trace_anchors": [record["anchor"] for record in records],
                "period": _text(widget.get("period")),
                "population": _text(widget.get("population")),
                "unit": _text(widget.get("unit")),
                "proxy_or_limit": _text(widget.get("proxy_or_limit") or widget.get("limit")),
            }
        )

    domain_blocks = []
    for domain in domains:
        domain_id = _slug(domain.get("id") or domain.get("title"))
        flow_blocks = []
        for flow in _as_list(domain.get("decision_flow")):
            if not isinstance(flow, Mapping):
                continue
            wanted = {_text(v.get("id") if isinstance(v, Mapping) else v) for v in _as_list(flow.get("widget_ids"))}
            # Use the already ordered blocks by their explicit IDs.  The
            # marker keeps domain/decision-flow order from the reviewed input.
            selected = []
            for _, _, widget in ordered:
                if _text(widget.get("id")) in wanted or (_text(widget.get("domain_id")) == _text(domain.get("id")) and not wanted):
                    selected.append(f'<a class="widget-jump" href="#widget-{_escape(_slug(widget.get("id")))}">{_escape(widget.get("title") or widget.get("id"))}</a>')
            flow_blocks.append(
                f'<div class="decision-flow"><h3>{_escape(flow.get("title") or flow.get("id"), "Decision view")}</h3>'
                f'<div class="flow-links">{"".join(selected)}</div></div>'
            )
        domain_blocks.append(
            f'<section class="domain" id="domain-{_escape(domain_id)}"><h2>{_escape(domain.get("title") or domain_id)}</h2>{"".join(flow_blocks)}</section>'
        )

    limitations = _as_list(fixture.get("limitations"))
    limitations_block = "".join(f"<li>{_escape(value)}</li>" for value in limitations)
    if not limitations_block:
        limitations_block = "<li>Only reviewed values supplied in the fixture are shown; no new analytics were calculated.</li>"
    failed_items_html = _render_failed_items(fixture)
    trace_block = "".join(
        f'<li id="{_escape(record["anchor"])}"><strong>{_escape(record["label"])}</strong>'
        f'<span class="trace-ref">{_escape(record["ref"])}</span></li>'
        for record in trace_records
    )
    css = """
    :root{color-scheme:light;--ink:#18222d;--muted:#536170;--line:#d9e1e8;--accent:#146c94;--panel:#fff;--soft:#f2f6f8}
    *{box-sizing:border-box}body{margin:0;background:var(--soft);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow-x:hidden}
    main{max-width:1180px;margin:0 auto;padding:24px}header{background:var(--panel);border:1px solid var(--line);padding:20px;border-radius:12px}
    h1,h2,h3{margin:0 0 8px}h1{font-size:1.8rem}h2{margin-top:26px;font-size:1.25rem}h3{font-size:1rem}.eyebrow{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:.72rem}
    .kpi-grid,.widget-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:14px}.widget{background:var(--panel);border:1px solid var(--line);padding:16px;border-radius:10px;min-width:0}
    .kpi-value{font-size:2rem;font-weight:700;color:var(--accent);margin:10px 0}.widget-meta{display:grid;grid-template-columns:max-content 1fr;gap:2px 10px;color:var(--muted);font-size:.82rem}.widget-meta dt{font-weight:600}.widget-meta dd{margin:0}
    .trace-links{margin-top:12px;border-top:1px solid var(--line);padding-top:8px;font-size:.78rem}.trace-links span{color:var(--muted)}.trace-link,.widget-jump{color:var(--accent);margin-right:8px}
    .review-status{font-size:.78rem;color:#7c4d00;background:#fff6df;border-radius:5px;padding:5px 7px;display:inline-block}.viz{margin:10px 0}.viz-row{display:grid;grid-template-columns:minmax(80px,1fr) 2fr max-content;gap:7px;align-items:center;margin:6px 0}.viz-track{height:9px;border-radius:9px;background:#e4ebef;overflow:hidden}.viz-bar{display:block;height:100%;width:var(--bar-size,0%);background:var(--accent)}.viz-value{font-variant-numeric:tabular-nums}.viz-line-list,.viz-scatter,.viz-donut{padding-left:20px}.viz-line-list li,.viz-scatter li,.viz-donut li{display:flex;gap:12px;justify-content:space-between;border-bottom:1px solid var(--line);padding:5px 0}.viz-stacked{display:flex;min-height:28px;border-radius:5px;overflow:hidden;background:#e4ebef}.stack-segment{width:var(--segment-size,auto);padding:5px 8px;border-right:1px solid var(--panel);background:#77b6c9;white-space:nowrap}.stack-segment:nth-child(2n){background:#e2a65e}.viz-heatmap{display:grid;grid-template-columns:repeat(auto-fit,minmax(64px,1fr));gap:4px}.heat-cell{padding:15px 5px;background:#d9edf2;text-align:center;border-radius:3px}.heat-cell.high,.heat-cell.critical{background:#dc8c78;color:#fff}.heat-cell.medium{background:#f2ce8f}.scatter-dot,.donut-key{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--accent);flex:none}.table-wrap{width:100%;max-width:100%;min-width:0;overflow-x:auto;overflow-y:hidden}.table-wrap table{border-collapse:collapse;width:max-content;min-width:100%;max-width:none;font-size:.84rem}.table-wrap th,.table-wrap td{padding:6px;border:1px solid var(--line);text-align:left;overflow-wrap:anywhere}.table-wrap th{background:#edf3f6}.limitations{margin-top:24px;background:#fff8e7;border:1px solid #eed9a8;padding:14px;border-radius:10px}.trace-panel{margin-top:24px;background:var(--panel);border:1px solid var(--line);padding:16px;border-radius:10px}.trace-panel ul{padding-left:20px}.trace-ref{color:var(--muted);font-size:.8rem;margin-left:8px}.decision-flow{margin:8px 0 10px}.flow-links{display:flex;flex-wrap:wrap;gap:5px}.widget-jump{border:1px solid #b7d3df;border-radius:99px;padding:3px 9px;text-decoration:none;font-size:.8rem}details.data-detail{min-width:0;max-width:100%}details.data-detail:not([open])>.table-wrap{display:none}.widget{min-width:0;overflow:hidden}
    @media(max-width:640px){main{padding:12px}.viz-row{grid-template-columns:1fr max-content}.viz-track{grid-column:1/-1}.viz-value{grid-column:2;grid-row:1}}
    """
    title = _text(fixture.get("title"), "Reviewed offline dashboard")
    subtitle = _text(fixture.get("subtitle"), "Static presentation of already-reviewed widget specifications")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_escape(title)}</title><style>{css}</style></head>
<body><main><header><p class="eyebrow">Offline · Source-bound outputs only</p><h1>{_escape(title)}</h1><p>{_escape(subtitle)}</p><p>No external assets, JavaScript, CDN, source reads, or new calculations are used by this renderer.</p></header>
<section aria-labelledby="kpi-heading"><h2 id="kpi-heading">Key indicators</h2><div class="kpi-grid">{"".join(cards) or '<p>No KPI cards were supplied.</p>'}</div></section>
<section aria-labelledby="flow-heading"><h2 id="flow-heading">Decision flow</h2>{"".join(domain_blocks)}</section>
<section aria-labelledby="detail-heading"><h2 id="detail-heading">Reviewed visual details</h2><div class="widget-grid">{"".join(sections) or '<p>No non-KPI widgets were supplied.</p>'}</div></section>
{failed_items_html}
<section class="limitations" aria-labelledby="limits-heading"><h2 id="limits-heading">Assumptions and limitations</h2><ul>{limitations_block}</ul></section>
<section class="trace-panel" aria-labelledby="trace-heading"><h2 id="trace-heading">Audit and trace references</h2><ul>{trace_block}</ul></section>
</main></body></html>"""
    _validate_links(document)
    manifest = {
        "product_type": "offline_static_dashboard",
        "source_status": "reviewed_outputs_only",
        "status": surface_status,
        "review_status": surface_status,
        "new_analytics": False,
        "organization": "business_domain_and_decision_flow",
        "assets_local": True,
        "internal_links_checked": True,
        "freeze_markers": freeze_markers.to_dict(),
        "run_id": _text(fixture.get("run_id")),
        "skill_version": _text(fixture.get("skill_version"), "0.7.2"),
        "domain_order": [domain["id"] for domain in domains],
        "decision_flow_order": [
            {"domain_id": domain["id"], "flow_id": flow["id"]}
            for domain in domains
            for flow in domain["decision_flow"]
        ],
        "items": manifest_items,
        "audit_record_count": len(fixture.get("audit_records") or []) if isinstance(fixture.get("audit_records") or [], list) else 0,
        "limitations": [_text(value) for value in limitations],
    }
    if registry_info:
        manifest.update(registry_info)
    return document, manifest


def _canonical_dashboard_css() -> bytes:
    """Load the single committed stylesheet used by the static site."""

    css_path = Path(__file__).resolve().parent.parent / "assets" / "dashboard.css"
    return css_path.read_bytes()


def _canonical_dashboard_js() -> bytes:
    """Load the single deterministic interaction runtime used by the site."""

    js_path = Path(__file__).resolve().parent.parent / "assets" / "dashboard.js"
    return js_path.read_bytes()




def _site_nav(
    current: str,
    domains: list[Mapping[str, Any]],
    *,
    prefix: str = "",
    include_ontology: bool = True,
    manager_surface: bool = True,
) -> str:
    if manager_surface:
        # The default manager canvas links only to business pages. Provenance,
        # ontology, and data-quality surfaces remain available as separate
        # technical pages with their own navigation.
        links = [
            ("index.html", "Overview"),
            *[(f"domains/{_slug(domain['id'])}.html", _manager_prose_text(domain["title"])) for domain in domains],
        ]
    else:
        links = [
            ("index.html", "Overview"),
            ("data-quality-audit.html", "Data quality & model audit"),
            ("evidence.html", "Evidence & audit"),
        ]
        if include_ontology:
            links.insert(-1, ("ontology.html", "Ontology projection"))
    rendered = []
    for target, label in links:
        href = posixpath.normpath(posixpath.join(prefix, target))
        current_attr = ' aria-current="page"' if target == current else ""
        rendered.append(f'<a href="{_escape(href)}"{current_attr}>{_escape(label)}</a>')
    return "".join(rendered)


def _site_page(
    *,
    title: str,
    current: str,
    domains: list[Mapping[str, Any]],
    body: str,
    prefix: str = "",
    subtitle: str = "",
    include_ontology: bool = True,
    status: str = "Preview",
    manager_surface: bool = True,
) -> str:
    nav = _site_nav(
        current,
        domains,
        prefix=prefix,
        include_ontology=include_ontology,
        manager_surface=manager_surface,
    )
    css_href = posixpath.normpath(posixpath.join(prefix, "assets/dashboard.css"))
    runtime_href = posixpath.normpath(posixpath.join(prefix, "assets/dashboard.js"))
    favicon_href = posixpath.normpath(posixpath.join(prefix, "assets/favicon.svg"))
    index_href = posixpath.normpath(posixpath.join(prefix, "index.html"))
    subtitle_html = f'<p>{_escape(subtitle)}</p>' if subtitle else ""
    domain_options = "".join(
        f'<option value="{_escape(_slug(domain.get("id")))}">{_escape(_manager_prose_text(domain.get("title")))}</option>'
        for domain in domains
    )
    toolbar = (
        '<section class="runtime-toolbar" aria-label="Dashboard exploration controls">'
        '<label>Find a view <input type="search" data-runtime-search placeholder="Search visible views"></label>'
        f'<label>Decision domain <select data-runtime-domain><option value="">All domains</option>{domain_options}</select></label>'
        '<button type="button" data-runtime-clear>Reset</button><span class="runtime-count" data-runtime-count></span>'
        '</section>'
    )
    brand_subtitle = "business decision workspace" if manager_surface else "source-bound business analytics"
    rail_note = "Manager decisions<br>Business information only" if manager_surface else "Offline · source-bound outputs only<br>Manager decisions and evidence"
    hero_status = "Manager view" if manager_surface else "Source-bound"
    footer_note = "Static local product · Business decision workspace." if manager_surface else "Static local product · Source-bound manager workspace."
    status_text = _escape(hero_status if manager_surface else status)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_escape(title)}</title><link rel="stylesheet" href="{_escape(css_href)}"><link rel="icon" href="{_escape(favicon_href)}" type="image/svg+xml"><script src="{_escape(runtime_href)}" defer></script></head>
<body><div class="shell"><aside class="rail"><a class="brand" href="{_escape(index_href)}"><strong>AUTO FOUNDRY</strong><small>{_escape(brand_subtitle)}</small></a><nav class="nav" aria-label="Dashboard pages">{nav}</nav><p class="rail-note">{rail_note}</p></aside><main class="content" data-dashboard-runtime><header class="hero"><div><span class="eyebrow">Auto Foundry Control Center</span><h1>{_escape(title)}</h1>{subtitle_html}</div><span class="status">{status_text}</span></header>{toolbar}{body}<footer class="footer">{footer_note}</footer></main></div></body></html>"""


def _layout_class(widget: Mapping[str, Any]) -> str:
    """Return a bounded layout class from optional fixture metadata."""

    manager = widget.get("manager_presentation")
    layout = _slug(widget.get("layout") or (manager.get("layout") if isinstance(manager, Mapping) else "") or "")
    span = _text(widget.get("span") or "")
    if span in {"1", "2", "3", "4", "6", "8", "12"}:
        return f" span-{span}"
    if layout in {"wide", "full", "half", "hero", "compact", "kpi", "chart", "detail"}:
        return f" layout-{layout}"
    return ""


def _requirement_groups(
    widgets: list[Mapping[str, Any]],
    domain: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Group widgets by reviewed requirement metadata, preserving fixture order."""

    definitions = {
        _text(item.get("id") or item.get("requirement_id")): item
        for item in _as_list(domain.get("requirements"))
        if isinstance(item, Mapping) and _text(item.get("id") or item.get("requirement_id"))
    }
    flow_definitions: dict[str, Mapping[str, Any]] = {}
    for flow in _as_list(domain.get("decision_flow")):
        if not isinstance(flow, Mapping):
            continue
        for widget_id in _as_list(flow.get("widget_ids")):
            key = _text(widget_id.get("id") if isinstance(widget_id, Mapping) else widget_id)
            if key:
                flow_definitions[key] = flow
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for widget in widgets:
        requirement_id = _text(widget.get("requirement_id") or widget.get("id"), "requirement")
        if requirement_id not in groups:
            definition = definitions.get(requirement_id, {})
            flow = next((flow_definitions.get(_text(item.get("id"))) for item in widgets if _text(item.get("requirement_id") or item.get("id")) == requirement_id), None)
            flow = flow or {}
            groups[requirement_id] = {
                "id": requirement_id,
                "title": _text(widget.get("requirement_title") or definition.get("title") or flow.get("title") or requirement_id),
                "order": definition.get("order", widget.get("requirement_order", len(order) + 1)),
                "subtitle": _text(widget.get("requirement_subtitle") or definition.get("subtitle") or flow.get("subtitle")),
                "takeaway": _text(widget.get("takeaway") or definition.get("takeaway") or flow.get("takeaway")),
                "scope": _text(widget.get("requirement_scope") or definition.get("scope") or flow.get("scope")),
                "limitations": list(_as_list(widget.get("requirement_limitations") or definition.get("limitations") or flow.get("limitations"))),
                "manager_admission": dict(widget.get("manager_admission") or flow.get("manager_admission") or {}),
                "presentation_audience": _text(widget.get("presentation_audience") or flow.get("presentation_audience")),
                "widgets": [],
            }
            order.append(requirement_id)
        group = groups[requirement_id]
        if not group["subtitle"] and _text(widget.get("requirement_subtitle")):
            group["subtitle"] = _text(widget.get("requirement_subtitle"))
        if not group["takeaway"] and _text(widget.get("takeaway")):
            group["takeaway"] = _text(widget.get("takeaway"))
        if not group["scope"] and _text(widget.get("requirement_scope")):
            group["scope"] = _text(widget.get("requirement_scope"))
        for limitation in _as_list(widget.get("requirement_limitations")):
            value = _text(limitation).strip()
            if value and value not in group["limitations"]:
                group["limitations"].append(value)
        group["widgets"].append(widget)
    try:
        return sorted((groups[key] for key in order), key=lambda item: (int(item["order"]), order.index(item["id"])))
    except (TypeError, ValueError):
        return [groups[key] for key in order]


def _requirement_anchor(requirement_id: Any) -> str:
    return "requirement-" + _slug(requirement_id)


def _manager_widget_allowed(widget: Mapping[str, Any], *, strict: bool = False) -> bool:
    """Return plan-bound manager visibility without semantic inference.

    The assembler has already applied the Product Agent's explicit plan.  The
    renderer validates only the status/audience contract and never examines a
    title, table key/value, role, kind, or technical annotation to veto a
    selected widget.
    """

    admission = widget.get("manager_admission")
    audience = _text(widget.get("presentation_audience"))
    if not isinstance(admission, Mapping):
        if strict:
            raise ValueError(f"widget {_text(widget.get('id'), 'widget')} lacks manager_admission")
        # Legacy/manual fixtures without the V4 admission envelope remain
        # visible to direct callers; V4 fixtures are strict and must carry the
        # persisted Product plan metadata.
        return _text(widget.get("presentation_tier"), "primary").lower() != "audit"
    status = _text(admission.get("status"))
    if status not in {"admitted", "audit_only"} or audience not in {"business_manager", "technical_audit"}:
        raise ValueError(f"widget {_text(widget.get('id'), 'widget')} has unknown manager admission")
    if (status == "admitted") != (audience == "business_manager"):
        raise ValueError(f"widget {_text(widget.get('id'), 'widget')} has inconsistent manager admission")
    return status == "admitted"


def _validate_manager_admission_contract(fixture: Mapping[str, Any], widgets: list[Mapping[str, Any]]) -> None:
    """Fail closed for V4 products missing the explicit admission policy."""

    if not _is_v4_fixture(fixture):
        return
    policy = fixture.get("manager_admission")
    expected_policy = "terminal_limited_dashboard" if fixture.get("limited_dashboard") is True else "explicit_business_presentation_plan"
    if not isinstance(policy, Mapping) or _text(policy.get("policy")) != expected_policy:
        raise ValueError("v4 fixture requires explicit manager_admission policy")
    for widget in widgets:
        _manager_widget_allowed(widget, strict=True)


def _validate_fixture_presentation_plan_v2(
    fixture: Mapping[str, Any],
    widgets: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    *,
    plan_path: Path,
    context: RunContext,
) -> None:
    """Validate the V2 visual partition and projection-only widget binding."""

    def snapshot_hash(widget: Mapping[str, Any]) -> str:
        # Plan-derived admission metadata is attached after the visual
        # snapshot is selected (``manager_presentation`` also carries the
        # snapshot hash itself).  Hash the immutable visual/envelope payload;
        # the exact full widget remains in the audit inventory.
        snapshot = {
            key: copy.deepcopy(value)
            for key, value in widget.items()
            if key not in {
                "manager_admission", "manager_presentation", "presentation_audience",
                "presentation_tier", "manager_anchor", "presentation_plan_ref",
                "presentation_plan_sha256", "overview",
            }
        }
        return hashlib.sha256(
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        ).hexdigest()

    def chart_hash(chart: Mapping[str, Any]) -> str:
        snapshot = copy.deepcopy(dict(chart))
        fields = snapshot.get("fields_or_values_used")
        if isinstance(fields, Mapping):
            snapshot["fields_or_values_used"] = {
                key: copy.deepcopy(value)
                for key, value in fields.items()
                if key not in {"presentation_role", "presentation_tier"}
            }
        return hashlib.sha256(
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        ).hexdigest()

    plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    manager_ids = fixture.get("manager_widget_ids")
    if fixture.get("presentation_plan_sha256") != plan_hash or plan.get("manager_widget_ids") != manager_ids:
        raise ValueError("v2 presentation plan hash or manager IDs do not match")
    manager_visual = fixture.get("manager_visual_widget_ids")
    audit_visual = fixture.get("audit_visual_widget_ids")
    if manager_visual != plan.get("manager_visual_widget_ids") or audit_visual != plan.get("audit_visual_widget_ids"):
        raise ValueError("v2 fixture visual partition does not match plan")
    if (
        not isinstance(manager_visual, list)
        or not isinstance(audit_visual, list)
        or len(set(manager_visual)) != len(manager_visual)
        or len(set(audit_visual)) != len(audit_visual)
        or set(manager_visual).intersection(audit_visual)
        # A small blueprint may legitimately contain only manager visuals or
        # only technical visuals; an empty partition is represented explicitly
        # rather than forcing a fabricated chart into the other audience.
    ):
        raise ValueError("v2 fixture visual partition must contain disjoint unique IDs")
    entries = fixture.get("visual_entries")
    plan_entries = plan.get("visual_entries")
    if entries != plan_entries or not isinstance(entries, list) or [entry.get("widget_id") for entry in entries if isinstance(entry, Mapping)] != manager_visual + audit_visual:
        raise ValueError("v2 fixture visual entries do not match plan order")
    manager_entries = fixture.get("manager_entries")
    if manager_entries != plan.get("manager_entries") or [entry.get("widget_id") for entry in manager_entries if isinstance(entry, Mapping)] != manager_ids:
        raise ValueError("v2 fixture manager entries do not match plan")
    by_id = {_text(widget.get("id")): widget for widget in widgets}
    selected = set(manager_ids or [])
    for widget in widgets:
        widget_id = _text(widget.get("id"))
        admission = widget.get("manager_admission")
        admitted = widget_id in selected
        if not isinstance(admission, Mapping) or (admission.get("status") == "admitted") != admitted or (admission.get("presentation_audience") == "business_manager") != admitted:
            raise ValueError(f"v2 widget admission overrides plan membership: {widget_id}")
    chart_ref = _text(fixture.get("chart_map_ref"))
    chart_path = context.resolve_run_path(chart_ref)
    if chart_path.is_symlink() or not chart_path.is_file():
        raise ValueError("v2 fixture chart map is missing or symlinked")
    try:
        chart_map = json.loads(chart_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("v2 fixture chart map is invalid") from exc
    charts = {entry.get("id"): entry for entry in chart_map.get("charts", []) if isinstance(entry, Mapping)}
    if set(charts) != set(by_id):
        raise ValueError("v2 fixture chart map IDs do not cover widgets")
    current_visual_ids = [
        widget_id
        for widget_id, widget in by_id.items()
        if _is_partition_visual(widget, charts[widget_id])
    ]
    if set(manager_visual).union(audit_visual) != set(current_visual_ids):
        raise ValueError("v2 fixture visual partition does not cover current chart universe")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("v2 visual entry is invalid")
        widget_id = _text(entry.get("widget_id"))
        widget = by_id.get(widget_id)
        chart = charts.get(widget_id)
        if widget is None or chart is None:
            raise ValueError(f"v2 visual entry references unknown widget: {widget_id}")
        if entry.get("visual_type") != _text(widget.get("type") or widget.get("kind")) or entry.get("visual_type") != _text(chart.get("type")):
            raise ValueError(f"v2 visual entry type drifted: {widget_id}")
        if entry.get("chart_family") != _text(chart.get("family")):
            raise ValueError(f"v2 visual entry family drifted: {widget_id}")
        if "recipe_id" in entry and (not isinstance(entry.get("recipe_id"), str) or not entry["recipe_id"].strip()):
            raise ValueError(f"v2 visual entry recipe selection is invalid: {widget_id}")
        if "layout" in entry and entry.get("layout") not in {"full", "wide", "half", "compact"}:
            raise ValueError(f"v2 visual entry layout is invalid: {widget_id}")
        if "renderer_type" in entry and entry.get("renderer_type") not in {
            "kpi", "bar", "column", "grouped_bar", "stacked_bar", "diverging_bar", "waffle",
            "funnel", "histogram", "box_plot", "pareto", "waterfall", "heatmap", "scatter",
            "line", "area", "stacked_area", "lollipop", "donut", "metric_grid", "table",
        }:
            raise ValueError(f"v2 visual entry renderer selection is invalid: {widget_id}")
        if snapshot_hash(widget) != entry.get("widget_snapshot_sha256") or chart_hash(chart) != entry.get("chart_entry_sha256"):
            raise ValueError(f"v2 visual snapshot/chart hash drifted: {widget_id}")
        title = entry.get("title_projection")
        if not isinstance(title, Mapping) or title.get("pointer") != "/widget_snapshot/title" or title.get("value") != widget.get("title"):
            raise ValueError(f"v2 visual title projection drifted: {widget_id}")
        for field, binding in (entry.get("visual_projection") or {}).items():
            pointer = binding.get("pointer") if isinstance(binding, Mapping) else None
            if not isinstance(pointer, str) or not pointer.startswith("/chart_entry/"):
                raise ValueError(f"v2 visual projection pointer is invalid: {widget_id}:{field}")
            current: Any = {"chart_entry": chart}
            try:
                for part in pointer[1:].split("/"):
                    if re.search(r"~(?![01])", part):
                        raise KeyError(part)
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(current, Mapping):
                        current = current[part]
                    elif isinstance(current, list):
                        current = current[int(part)]
                    else:
                        raise KeyError(part)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError(f"v2 visual projection pointer is missing: {widget_id}:{field}") from exc
            if json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(binding.get("value"), ensure_ascii=False, sort_keys=True, separators=(",", ":")):
                raise ValueError(f"v2 visual projection value drifted: {widget_id}:{field}")
    manager_entry_by_id = {entry.get("widget_id"): entry for entry in manager_entries if isinstance(entry, Mapping)}
    visual_by_id = {entry.get("widget_id"): entry for entry in entries if isinstance(entry, Mapping)}
    for widget_id in manager_visual:
        manager_entry = manager_entry_by_id.get(widget_id)
        visual_entry = visual_by_id.get(widget_id)
        if not isinstance(manager_entry, Mapping) or not isinstance(visual_entry, Mapping):
            raise ValueError(f"v2 manager visual entry is missing: {widget_id}")
        visual_keys = ("visual_type", "chart_family", "widget_snapshot_sha256", "chart_entry_sha256", "allowed_visual_fields", "title_projection", "visual_projection")
        if "recipe_id" in visual_entry or "recipe_id" in manager_entry:
            visual_keys += ("recipe_id",)
        if "layout" in visual_entry or "layout" in manager_entry:
            visual_keys += ("layout",)
        if "renderer_type" in visual_entry or "renderer_type" in manager_entry:
            visual_keys += ("renderer_type",)
        for key in visual_keys:
            if manager_entry.get(key) != visual_entry.get(key):
                raise ValueError(f"v2 manager visual entry drifted: {widget_id}:{key}")


def _validate_fixture_presentation_plan(
    fixture: Mapping[str, Any],
    widgets: list[Mapping[str, Any]],
    *,
    context: RunContext | None,
) -> None:
    """Require manager visibility to match the persisted plan exactly."""

    if not _is_v4_fixture(fixture):
        return
    plan_ref = fixture.get("presentation_plan_ref")
    manager_ids = fixture.get("manager_widget_ids", [])
    if plan_ref in (None, ""):
        if fixture.get("limited_dashboard") is True:
            allowed = {
                _text(widget.get("id"))
                for widget in widgets
                if isinstance(widget, Mapping)
                and widget.get("limited_empty_state") is True
                and isinstance(widget.get("manager_admission"), Mapping)
                and _text(widget.get("manager_admission", {}).get("status")) == "admitted"
            }
            if not isinstance(manager_ids, list) or set(_text(value) for value in manager_ids) != allowed:
                raise ValueError("limited dashboard manager IDs do not match its empty-state widget")
            return
        if manager_ids not in (None, []) or any(
            isinstance(widget.get("manager_admission"), Mapping)
            and _text(widget.get("manager_admission", {}).get("status")) == "admitted"
            for widget in widgets
        ):
            raise ValueError("v4 fixture admits manager widgets without a presentation plan")
        return
    if context is None:
        raise ValueError("v4 fixture presentation plan requires a RunContext")
    if not isinstance(manager_ids, list) or len(set(_text(value) for value in manager_ids)) != len(manager_ids):
        raise ValueError("fixture manager_widget_ids are invalid")
    plan_path = context.resolve_run_path(plan_ref)
    if plan_path.is_symlink() or not plan_path.is_file():
        raise ValueError("fixture presentation plan is missing or symlinked")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("fixture presentation plan is invalid") from exc
    if not isinstance(plan, Mapping) or plan.get("schema_version") != "dashboard.business_presentation_plan.v2":
        raise ValueError("V1 presentation plans are rejected at the renderer boundary")
    _validate_fixture_presentation_plan_v2(fixture, widgets, plan, plan_path=plan_path, context=context)
    return


def _validate_audit_inventory(
    fixture: Mapping[str, Any],
    widgets: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> None:
    """Validate the separate record/widget audit authorities for V4 sites."""

    if not _is_v4_fixture(fixture):
        return
    raw_entries = fixture.get("audit_widgets")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(widgets):
        raise ValueError("v4 fixture audit_widgets must contain exactly one entry per widget")
    by_widget = {_text(widget.get("id")): widget for widget in widgets}
    if len(by_widget) != len(widgets):
        raise ValueError("v4 fixture widgets require unique IDs")
    seen_widget_ids: list[str] = []
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            raise ValueError("v4 fixture audit_widgets entries must be objects")
        widget_id = _text(entry.get("widget_id")).strip()
        snapshot = entry.get("widget_snapshot")
        if not widget_id or widget_id in seen_widget_ids or widget_id not in by_widget or not isinstance(snapshot, Mapping):
            raise ValueError(f"v4 fixture audit widget entry is invalid: {widget_id or '<missing>'}")
        if json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(by_widget[widget_id], ensure_ascii=False, sort_keys=True, separators=(",", ":")):
            raise ValueError(f"v4 fixture audit widget snapshot drifted: {widget_id}")
        expected_record_ids = sorted({
            _text(value).strip()
            for value in (_as_list(by_widget[widget_id].get("integration_record_ids")) or [by_widget[widget_id].get("integration_record_id")])
            if _text(value).strip()
        })
        actual_record_ids = sorted({
            _text(value).strip()
            for value in _as_list(entry.get("record_ids"))
            if _text(value).strip()
        })
        if actual_record_ids != expected_record_ids:
            raise ValueError(f"v4 fixture audit widget record links drifted: {widget_id}")
        seen_widget_ids.append(widget_id)
    if seen_widget_ids != [_text(widget.get("id")) for widget in widgets]:
        raise ValueError("v4 fixture audit widget entries must preserve widget order")
    record_ids: list[str] = []
    known_widgets = set(seen_widget_ids)
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("v4 fixture audit_records entries must be objects")
        record_id = _text(record.get("record_id")).strip()
        if not record_id or record_id in record_ids:
            raise ValueError(f"v4 fixture audit record IDs must be unique: {record_id or '<missing>'}")
        if "widget_snapshot" in record:
            raise ValueError(f"v4 fixture record audit must not embed widget snapshots: {record_id}")
        linked = set(_reference_values(record.get("widget_ids")))
        if not linked or not linked <= known_widgets:
            raise ValueError(f"v4 fixture audit record widget links are invalid: {record_id}")
        record_ids.append(record_id)
    expected_count = fixture.get("audit_widget_entry_count")
    if expected_count is not None and expected_count != len(raw_entries):
        raise ValueError("v4 fixture audit_widget_entry_count does not match audit_widgets")


def _site_widget(
    widget: Mapping[str, Any],
    records: list[dict[str, str]],
    *,
    manager_surface: bool = True,
) -> str:
    display_widget = _manager_surface_widget(widget)
    kind = _text(display_widget.get("type") or display_widget.get("kind"), "kpi").lower()
    explicit_projection = bool(display_widget.get("__explicit_plan_projection"))
    projection_fields = set(display_widget.get("__explicit_projection_fields") or ())
    title = (
        _text(display_widget.get("display_title") or display_widget.get("title") or display_widget.get("label"))
        if explicit_projection
        else _manager_prose_text(_manager_title(display_widget))
    )
    role = _text(display_widget.get("presentation_role") or "decision_view")
    if explicit_projection and "body" in projection_fields:
        # Pointer-bound business prose is authoritative.  Keep it exact and
        # do not route it through any semantic reinterpretation path.
        findings_value = display_widget.get("manager_findings")
        first_finding = findings_value[0] if isinstance(findings_value, list) and findings_value else None
        body = _text(first_finding.get("finding")) if isinstance(first_finding, Mapping) else _text(display_widget.get("body"))
        visual = f'<div class="manager-projection-body"><p>{_escape(body)}</p></div>'
    elif not explicit_projection and role == "finding_list":
        visual = _render_finding_list(display_widget)
    elif not explicit_projection and role == "relationship_matrix":
        relationship_rows = _relationship_manager_rows(display_widget)
        relationship_widget = dict(display_widget)
        relationship_widget["type"] = "table"
        relationship_widget["manager_rows"] = relationship_rows or _manager_table_rows(display_widget) or []
        visual = _render_site_table(relationship_widget)
    elif kind in {"table", "status_table"}:
        visual = _render_site_table(display_widget)
    else:
        try:
            visual = _render_visual(display_widget)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            visual = _render_visual_fallback(display_widget, manager_view=True)
    notes = _as_list(display_widget.get("chart_notes") or display_widget.get("notes"))
    note = next((
        _text(value) if explicit_projection else _manager_prose_text(value)
        for value in notes if _text(value)
    ), "")
    note_html = f'<p class="chart-note">{_escape(note)}</p>' if note else ""
    manager_meta = _manager_meta_lines(display_widget)
    small_multiple = _text(display_widget.get("small_multiple_group"))
    panel_attr = f' data-small-multiple-group="{_escape(small_multiple)}"' if small_multiple else ""
    panel_class = " small-multiple-panel" if small_multiple else ""
    panel_label = _text(display_widget.get("small_multiple_label")) if explicit_projection else _manager_prose_text(display_widget.get("small_multiple_label"))
    panel_note = f'<span class="small-multiple-label">{_escape(panel_label)}</span>' if panel_label else ""
    if role in {"finding_list", "relationship_matrix"}:
        anchor = _text(widget.get("manager_anchor") or widget.get("display_anchor") or f'{widget.get("requirement_id", "requirement")}-{role}')
    else:
        anchor = _text(widget.get("manager_anchor") or widget.get("display_anchor") or widget.get("id") or f'{widget.get("requirement_id", "requirement")}-{role}-{title}')
    # Requirement/widget IDs are stable DOM/runtime identity, not visible
    # manager content. Keep them for deterministic navigation and the offline
    # interaction runtime; visible headings/eyebrows are cleaned separately.
    widget_id_attr = _text(widget.get("id")).strip()
    data_widget = f' data-widget-id="{_escape(widget_id_attr)}"' if widget_id_attr else ""
    requirement_attr = _escape(_text(widget.get("requirement_id") or ""))
    domain_attr = _escape(_slug(widget.get("domain_id") or ""))
    detail_key = _slug(anchor) + "-detail"
    detail = "" if manager_surface else (
        f'<button type="button" class="runtime-drilldown" data-runtime-drilldown="{_escape(detail_key)}" aria-expanded="false">Explore detail</button>'
        f'<div class="runtime-detail" data-runtime-detail="{_escape(detail_key)}" hidden><p>Exact reviewed rows and provenance remain available in the technical audit.</p></div>'
    )
    requirement_data = f' data-runtime-requirement="{requirement_attr}"' if requirement_attr else ""
    return f'<article class="widget manager-widget widget-{_escape(kind)} role-{_escape(_slug(role))}{_layout_class(widget)}{panel_class}" data-runtime-card{requirement_data} data-runtime-domain="{domain_attr}" data-runtime-role="{_escape(_slug(role))}"{panel_attr}{data_widget} id="widget-{_escape(_slug(anchor))}"><h3>{_escape(title)}</h3>{panel_note}{note_html}{manager_meta}{visual}{detail}</article>'


def _ontology_graph_body_generic(
    fixture: Mapping[str, Any],
    raw_nodes: list[Any],
    raw_relationships: list[Any],
    raw_groups: list[Any],
) -> str:
    """Render an arbitrary explicit ontology projection.

    The V4 fixture that shipped with the first chart-led dashboard has a
    deliberately stable 12/9/3 projection.  That path remains below so its
    layout and bytes stay unchanged.  New products may instead supply any
    number of explicitly identified nodes and relationships.  This helper is
    intentionally presentation-only: it validates the supplied graph,
    assigns a deterministic neutral lane when groups are absent, and never
    invents nodes, links, or summary counts.
    """

    if any(not isinstance(node, Mapping) for node in raw_nodes):
        raise ValueError("ontology objects must be objects")
    if any(not isinstance(item, Mapping) for item in raw_relationships):
        raise ValueError("ontology relationships must be objects")
    if any(not isinstance(group, Mapping) for group in raw_groups):
        raise ValueError("ontology groups must be objects")

    nodes: list[Mapping[str, Any]] = []
    node_by_id: dict[str, Mapping[str, Any]] = {}
    for node in raw_nodes:
        assert isinstance(node, Mapping)
        node_id = _text(node.get("id")).strip()
        label = _text(node.get("label")).strip()
        kind = _text(node.get("kind") or node.get("type")).strip()
        if not node_id or not label or not kind:
            raise ValueError("ontology objects require non-empty id, label, and kind")
        if node_id in node_by_id:
            raise ValueError("ontology objects require unique object ids")
        node_by_id[node_id] = node
        nodes.append(node)

    relationships: list[Mapping[str, Any]] = []
    relationship_keys: set[tuple[str, str, str]] = set()
    for item in raw_relationships:
        assert isinstance(item, Mapping)
        source = _text(item.get("source") or item.get("from")).strip()
        target = _text(item.get("target") or item.get("to")).strip()
        label = _text(item.get("label") or item.get("relationship")).strip()
        if not source or not target or not label:
            raise ValueError("ontology relationships require non-empty source, target, and label")
        if source not in node_by_id or target not in node_by_id:
            raise ValueError("ontology relationship references an unknown object")
        key = (source, target, label)
        if key in relationship_keys:
            raise ValueError("ontology relationships require unique links")
        relationship_keys.add(key)
        relationships.append(item)
    relationships.sort(key=lambda item: (
        _text(item.get("source") or item.get("from")),
        _text(item.get("target") or item.get("to")),
        _text(item.get("label") or item.get("relationship")),
    ))

    groups: list[dict[str, Any]] = []
    if raw_groups:
        seen_group_ids: set[str] = set()
        covered: set[str] = set()
        for index, group in enumerate(raw_groups):
            assert isinstance(group, Mapping)
            group_id = _text(group.get("id")).strip()
            group_label = _text(group.get("label") or group.get("title")).strip()
            node_ids = [_text(node_id).strip() for node_id in _as_list(group.get("node_ids"))]
            if not group_id or not group_label or not node_ids:
                raise ValueError("ontology groups require non-empty id, label, and node_ids")
            if group_id in seen_group_ids:
                raise ValueError("ontology groups require unique ids")
            seen_group_ids.add(group_id)
            if len(set(node_ids)) != len(node_ids):
                raise ValueError("ontology groups cannot repeat an object")
            if any(node_id not in node_by_id for node_id in node_ids):
                raise ValueError("ontology group references an unknown object")
            if covered.intersection(node_ids):
                raise ValueError("ontology groups must partition objects")
            covered.update(node_ids)
            entry = {"id": group_id, "label": group_label, "node_ids": node_ids, "_index": index}
            if isinstance(group.get("order"), (int, float)) and not isinstance(group.get("order"), bool):
                entry["order"] = group.get("order")
            groups.append(entry)
        if covered != set(node_by_id):
            raise ValueError("ontology groups must cover exactly the supplied objects")
        groups.sort(key=lambda group: (
            0 if isinstance(group.get("order"), (int, float)) and not isinstance(group.get("order"), bool) else 1,
            group.get("order", 0) if isinstance(group.get("order"), (int, float)) and not isinstance(group.get("order"), bool) else group["_index"],
            group["id"],
        ))
    else:
        # A neutral lane is an honest projection when nodes exist but no
        # business grouping was supplied.  No canonical groups are invented.
        if nodes:
            groups = [{"id": "neutral", "label": "Unassigned / supplied objects", "node_ids": sorted(node_by_id)}]

    if not nodes:
        raise ValueError("ontology projection requires at least one supplied object")

    node_width = 212
    node_height = 48
    lane_width = 260
    lane_gap = 24
    lane_x = {group["id"]: 20 + index * (lane_width + lane_gap) for index, group in enumerate(groups)}
    edge_corridor_top = 88
    edge_corridor_step = 15
    node_start_y = edge_corridor_top + max(0, len(relationships)) * edge_corridor_step + 20
    positions: dict[str, tuple[float, float, str]] = {}
    group_for_node: dict[str, str] = {}
    for group in groups:
        group_id = group["id"]
        for local_index, node_id in enumerate(group["node_ids"]):
            x = lane_x[group_id] + (lane_width - node_width) / 2
            y = node_start_y + local_index * 72
            positions[node_id] = (x, y, group_id)
            group_for_node[node_id] = group_id

    edge_parts: list[str] = []
    edge_labels: list[str] = []
    for edge_index, relationship in enumerate(relationships):
        source = _text(relationship.get("source") or relationship.get("from"))
        target = _text(relationship.get("target") or relationship.get("to"))
        label = _text(relationship.get("label") or relationship.get("relationship"))
        source_x, source_y, _ = positions[source]
        target_x, target_y, _ = positions[target]
        source_right = source_x + node_width
        target_left = target_x
        if target_x < source_x:
            source_right = source_x
            target_left = target_x + node_width
        source_anchor_y = source_y + node_height / 2
        target_anchor_y = target_y + node_height / 2
        corridor_y = edge_corridor_top + edge_index * edge_corridor_step
        points = (
            f"{source_right:g},{source_anchor_y:g} {source_right:g},{corridor_y:g} "
            f"{target_left:g},{corridor_y:g} {target_left:g},{target_anchor_y:g}"
        )
        edge_parts.append(
            f'<polyline class="ontology-edge" data-edge-index="{edge_index}" data-route="top-corridor" '
            f'data-source="{_escape(source)}" data-target="{_escape(target)}" points="{points}" '
            f'marker-end="url(#ontology-arrow-generic)"><title>{_escape(_manager_prose_text(label))}</title></polyline>'
        )
        edge_labels.append(
            f'<text class="ontology-edge-label" data-edge-index="{edge_index}" data-source="{_escape(source)}" '
            f'data-target="{_escape(target)}" x="{((source_right + target_left) / 2):g}" y="{(corridor_y - 4):g}">{_escape(_manager_prose_text(label))}</text>'
        )

    object_nodes: list[str] = []
    for group in groups:
        for node_id in group["node_ids"]:
            node = node_by_id[node_id]
            x, y, _ = positions[node_id]
            label = _text(node.get("label"))
            kind = _text(node.get("kind") or node.get("type"))
            object_nodes.append(
                f'<g class="ontology-node" role="img" aria-label="{_escape(_manager_prose_text(label))} · {_escape(_manager_prose_text(kind))}">'
                f'<rect x="{x:g}" y="{y:g}" width="{node_width:g}" height="{node_height:g}" rx="9"></rect>'
                f'<text class="ontology-node-label" x="{x + 10:g}" y="{y + 21:g}">{_escape(_manager_prose_text(label))}</text>'
                f'<text class="ontology-node-kind" x="{x + 10:g}" y="{y + 37:g}">{_escape(_manager_prose_text(kind))}</text></g>'
            )

    lanes = [
        f'<g class="ontology-lane"><rect x="{lane_x[group["id"]]:g}" y="20" width="{lane_width:g}" '
        f'height="{max(120, node_start_y + max(len(group["node_ids"]) - 1, 0) * 72 + node_height + 24):g}" rx="12"></rect>'
        f'<text class="ontology-lane-label" x="{lane_x[group["id"]] + 18:g}" y="50">{_escape(_manager_prose_text(group["label"]))}</text></g>'
        for group in groups
    ]
    max_nodes = max(len(group["node_ids"]) for group in groups)
    lane_height = max(120, node_start_y + max_nodes * 72 + 24)
    width = max(360, 20 + len(groups) * lane_width + max(0, len(groups) - 1) * lane_gap + 20)
    summary = fixture.get("ontology_summary") if isinstance(fixture.get("ontology_summary"), Mapping) else {}
    summary_labels = (
        ("ontology_items", "Full objects"),
        ("relationships", "Full relationships"),
        ("canonical_mappings", "Mappings"),
        ("identity_decisions", "Decisions"),
        ("resolution_bindings", "Resolution bindings"),
        ("item_bindings", "Item bindings"),
        ("prepared_assets", "Prepared assets"),
        ("knowledge", "Knowledge deltas"),
    )
    summary_values = " · ".join(
        f"{label} {_display_value(summary.get(key))}"
        for key, label in summary_labels
        if key in summary and summary.get(key) is not None
    )
    summary_cards = "".join(
        f'<div class="ontology-card"><span class="eyebrow">{_escape(label)}</span><strong>{_escape(_display_value(summary.get(key)))}</strong></div>'
        for key, label in summary_labels
        if key in summary and summary.get(key) is not None
    ) or '<p>No ontology counts were supplied.</p>'
    relationship_html = "".join(
        f'<li><strong>{_escape(item.get("source") or item.get("from"))}</strong><span class="relationship-arrow"> → </span>'
        f'{_escape(item.get("target") or item.get("to"))}<small>{_escape(item.get("label") or item.get("relationship"))}</small></li>'
        for item in relationships
    ) or '<li class="network-empty">No explicit relationships were supplied.</li>'
    return (
        f'<section class="ontology-summary"><span class="eyebrow">Full ontology snapshot</span><strong>{_escape(summary_values, "Supplied summary only")}</strong>'
        f'<p>Counts above are the frozen supplied summary. The graph below is a navigable product projection of {_escape(len(nodes))} supplied objects and {_escape(len(relationships))} explicit links.</p></section>'
        f'<div class="section-head"><h2>Object relationship graph</h2><p>Static projection · definitions only · no current observations</p></div>'
        f'<section class="ontology-graph-wrap"><svg class="ontology-graph" viewBox="0 0 {width:g} {lane_height:g}" role="img" aria-label="Ontology projection with {_escape(len(nodes))} business objects and {_escape(len(relationships))} labeled relationships">'
        f'<defs><marker id="ontology-arrow-generic" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker></defs>'
        f'{"".join(lanes)}{"".join(edge_parts)}{"".join(edge_labels)}{"".join(object_nodes)}</svg>'
        f'<div class="ontology-legend"><span><i class="legend-swatch object-swatch"></i>Business object</span><span><i class="legend-swatch edge-swatch"></i>Accepted relationship</span><span>{_escape(len(nodes))} objects · {_escape(len(relationships))} links</span></div></section>'
        f'<div class="section-head"><h2>Relationship details</h2><p>Explicit links only</p></div><ul class="relationship-list">{relationship_html}</ul>'
    )


def _ontology_graph_body(fixture: Mapping[str, Any]) -> str:
    raw_nodes = _as_list(fixture.get("ontology_objects"))
    raw_relationships = _as_list(fixture.get("ontology_relationships"))
    raw_groups = _as_list(fixture.get("ontology_groups"))
    if any(not isinstance(node, Mapping) for node in raw_nodes) or any(not isinstance(item, Mapping) for item in raw_relationships) or any(not isinstance(group, Mapping) for group in raw_groups):
        raise ValueError("v4 ontology objects, relationships, and groups must be objects")
    nodes = [node for node in raw_nodes if isinstance(node, Mapping)]
    relationships = [item for item in raw_relationships if isinstance(item, Mapping)]
    groups = [group for group in raw_groups if isinstance(group, Mapping)]
    if len(nodes) != 12 or len(relationships) != 9 or len(groups) != 3:
        return _ontology_graph_body_generic(fixture, raw_nodes, raw_relationships, raw_groups)
    summary = fixture.get("ontology_summary")
    summary_keys = ("ontology_items", "relationships", "canonical_mappings", "identity_decisions", "resolution_bindings", "item_bindings", "prepared_assets")
    if not isinstance(summary, Mapping):
        raise ValueError("v4 ontology projection requires ontology_summary")
    for key in summary_keys:
        value = summary.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"v4 ontology summary requires numeric {key}")
    if "knowledge" in summary and (isinstance(summary.get("knowledge"), bool) or not isinstance(summary.get("knowledge"), int) or summary.get("knowledge") < 0):
        raise ValueError("v4 ontology summary requires numeric knowledge")
    if any(not _text(node.get("id")).strip() or not _text(node.get("label")).strip() or not _text(node.get("kind")).strip() for node in nodes):
        raise ValueError("v4 ontology objects require non-empty id, label, and kind")
    if any(not _text(item.get("source")).strip() or not _text(item.get("target")).strip() or not _text(item.get("label")).strip() for item in relationships):
        raise ValueError("v4 ontology relationships require non-empty source, target, and label")
    node_by_id = {_text(node.get("id")): node for node in nodes}
    if len(node_by_id) != 12 or not all(node_by_id):
        raise ValueError("v4 ontology projection requires unique object ids")
    group_nodes: dict[str, tuple[Mapping[str, Any], int, int]] = {}
    group_lane_index: dict[str, int] = {}
    lane_x = (20, 365, 710)
    lane_width = 330
    lane_height = 390
    node_width = 132
    node_height = 48
    for group_index, group in enumerate(groups):
        group_id = _text(group.get("id"))
        group_label = _text(group.get("label"))
        node_ids = [_text(node_id) for node_id in _as_list(group.get("node_ids"))]
        if not group_id or group_id in group_lane_index or not group_label or not node_ids or any(not node_id or node_id not in node_by_id for node_id in node_ids):
            raise ValueError("v4 ontology group references an unknown or missing object")
        group_lane_index[group_id] = group_index
        for local_index, node_id in enumerate(node_ids):
            if node_id in group_nodes:
                raise ValueError("v4 ontology groups must partition objects")
            group_nodes[node_id] = (group, lane_x[group_index] + 24 + (local_index % 2) * 150, 92 + (local_index // 2) * 76)
    if set(group_nodes) != set(node_by_id):
        raise ValueError("v4 ontology groups must cover exactly the supplied objects")
    edge_parts = []
    edge_labels = []
    for edge_index, relationship in enumerate(relationships):
        source = _text(relationship.get("source"))
        target = _text(relationship.get("target"))
        label = _text(relationship.get("label"))
        if not source or not target or not label or source not in group_nodes or target not in group_nodes:
            raise ValueError("v4 ontology relationship references an unknown object")
        source_group, source_x, source_y = group_nodes[source]
        target_group, target_x, target_y = group_nodes[target]
        source_group_id = _text(source_group.get("id") or source_group.get("label"))
        target_group_id = _text(target_group.get("id") or target_group.get("label"))
        row_tops = sorted({position[2] for position in group_nodes.values()})
        source_row = row_tops.index(source_y)
        target_row = row_tops.index(target_y)
        sx, sy = source_x + node_width, source_y + node_height / 2
        tx, ty = target_x, target_y + node_height / 2
        if tx < sx:
            sx, tx = source_x, target_x + node_width
        # Keep every relationship label in a deterministic free corridor:
        # above the first row, between rows, or below the last row. A long
        # same-lane jump gets an orthogonal gutter route so its label and line
        # never pass through a node rectangle.
        same_lane = source_group_id == target_group_id and source_group_id in group_lane_index
        long_same_lane = same_lane and abs(source_row - target_row) > 1
        if long_same_lane:
            gutter = lane_x[group_lane_index[source_group_id]] + lane_width - 12
            sx, tx = source_x + node_width, target_x + node_width
            edge_parts.append(
        f'<polyline class="ontology-edge" data-edge-index="{edge_index}" data-route="lane-gutter" data-source="{_escape(source)}" data-target="{_escape(target)}" x1="{sx:g}" y1="{sy:g}" x2="{tx:g}" y2="{ty:g}" points="{sx:g},{sy:g} {gutter:g},{sy:g} {gutter:g},{ty:g} {tx:g},{ty:g}" marker-end="url(#ontology-arrow-v4)"><title>{_escape(_manager_prose_text(label))}</title></polyline>'
            )
            label_x = gutter - 5
        else:
            edge_parts.append(
                f'<line class="ontology-edge" data-edge-index="{edge_index}" data-source="{_escape(source)}" data-target="{_escape(target)}" x1="{sx:g}" y1="{sy:g}" x2="{tx:g}" y2="{ty:g}" marker-end="url(#ontology-arrow-v4)"><title>{_escape(_manager_prose_text(label))}</title></line>'
            )
            label_x = (sx + tx) / 2
        if source_row == target_row and source_row == 0:
            label_y = row_tops[source_row] - 16
        else:
            label_y = row_tops[min(source_row, target_row)] + node_height + 14
        edge_labels.append(
            f'<text class="ontology-edge-label" data-edge-index="{edge_index}" data-source="{_escape(source)}" data-target="{_escape(target)}" x="{label_x:g}" y="{label_y:g}">{_escape(_manager_prose_text(label))}</text>'
        )
    lanes = []
    for group_index, group in enumerate(groups):
        label = _text(group.get("label") or group.get("id"))
        lanes.append(
            f'<g class="ontology-lane"><rect x="{lane_x[group_index]}" y="20" width="{lane_width}" height="{lane_height}" rx="12"></rect>'
            f'<text class="ontology-lane-label" x="{lane_x[group_index] + 18}" y="50">{_escape(_manager_prose_text(label))}</text></g>'
        )
    object_nodes = []
    for node_id, (_group, x, y) in group_nodes.items():
        node = node_by_id[node_id]
        label = _text(node.get("label"))
        kind = _text(node.get("kind"))
        object_nodes.append(
            f'<g class="ontology-node" role="img" aria-label="{_escape(_manager_prose_text(label))} · {_escape(_manager_prose_text(kind))}">'
            f'<rect x="{x:g}" y="{y:g}" width="{node_width}" height="{node_height}" rx="9"></rect>'
            f'<text class="ontology-node-label" x="{x + 10:g}" y="{y + 21:g}">{_escape(_manager_prose_text(label))}</text>'
            f'<text class="ontology-node-kind" x="{x + 10:g}" y="{y + 37:g}">{_escape(_manager_prose_text(kind))}</text></g>'
        )
    summary_values = " · ".join(
        f"{label} {_display_value(summary.get(key))}"
        for key, label in (("ontology_items", "Full objects"), ("relationships", "Full relationships"), ("canonical_mappings", "Mappings"), ("identity_decisions", "Decisions"), ("resolution_bindings", "Resolution bindings"), ("item_bindings", "Item bindings"), ("prepared_assets", "Prepared assets"))
        if key in summary
    )
    summary_triplet = "/".join(f"{int(summary[key]):,}" for key in ("ontology_items", "relationships", "canonical_mappings"))
    relationship_html = "".join(
        f'<li><strong>{_escape(item.get("source"))}</strong><span class="relationship-arrow"> → </span>{_escape(item.get("target"))}'
        f'<small>{_escape(_manager_prose_text(item.get("label")))}</small></li>'
        for item in relationships
    )
    return (
        f'<section class="ontology-summary"><span class="eyebrow">Full ontology snapshot</span><strong>{_escape(summary_values)}</strong>'
        f'<p>Counts above are the frozen {_escape(summary_triplet)}-class summary. The graph below is a navigable product projection of exactly 12 supplied objects and 9 explicit links.</p></section>'
        f'<div class="section-head"><h2>Object relationship graph</h2><p>Static projection · definitions only · no current observations</p></div>'
        f'<section class="ontology-graph-wrap"><svg class="ontology-graph" viewBox="0 0 1060 440" role="img" aria-label="Ontology projection with 12 business objects and 9 labeled relationships">'
        f'<defs><marker id="ontology-arrow-v4" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker></defs>'
        f'{"".join(lanes)}{"".join(edge_parts)}{"".join(edge_labels)}{"".join(object_nodes)}</svg>'
        f'<div class="ontology-legend"><span><i class="legend-swatch object-swatch"></i>Business object</span><span><i class="legend-swatch edge-swatch"></i>Accepted relationship</span><span>12 objects · 9 links</span></div></section>'
        f'<div class="section-head"><h2>Relationship details</h2><p>Explicit links only</p></div><ul class="relationship-list">{relationship_html}</ul>'
    )


def _ontology_body(fixture: Mapping[str, Any]) -> str:
    if _is_v4_fixture(fixture):
        if fixture.get("ontology_groups") or fixture.get("ontology_objects") or fixture.get("ontology_relationships"):
            return _ontology_graph_body(fixture)
    if fixture.get("ontology_groups") or fixture.get("ontology_objects"):
        return _ontology_graph_body(fixture)
    raw_summary = fixture.get("ontology_summary")
    summary = raw_summary if isinstance(raw_summary, Mapping) else {}
    labels = (
        ("ontology_items", "Total ontology items"),
        ("relationships", "Relationships"),
        ("canonical_mappings", "Canonical mappings"),
        ("identity_decisions", "Identity decisions"),
        ("resolution_bindings", "Resolution bindings"),
        ("item_bindings", "Item bindings"),
        ("prepared_assets", "Prepared assets"),
        ("knowledge", "Knowledge deltas"),
    )
    cards = "".join(
        f'<div class="ontology-card"><span class="eyebrow">{_escape(label)}</span><strong>{_escape(_display_value(summary.get(key)))}</strong></div>'
        for key, label in labels
        if key in summary
    )
    nodes = [node for node in _as_list(fixture.get("ontology_nodes") or fixture.get("ontology_objects")) if isinstance(node, Mapping)]
    node_html = "".join(
        f'<div class="network-node"><strong>{_escape(_manager_prose_text(node.get("label") or node.get("id")))}</strong><small>{_escape(_manager_prose_text(_text(node.get("kind") or node.get("type"), "semantic object")))}</small></div>'
        for node in nodes[:40]
    )
    if not node_html:
        node_html = '<p class="network-empty">No ontology node summary was supplied for this reviewed product.</p>'
    cards_html = cards or "<p>No ontology counts were supplied.</p>"
    relationships = [item for item in _as_list(fixture.get("ontology_relationships")) if isinstance(item, Mapping)]
    relationship_html = "".join(
        f'<li><strong>{_escape(item.get("source") or item.get("from"))}</strong>'
        f'<span class="relationship-arrow"> → </span>{_escape(item.get("target") or item.get("to"))}'
        f'<small>{_escape(_manager_prose_text(item.get("label") or item.get("relationship") or "accepted relationship"))}</small></li>'
        for item in relationships[:30]
    )
    if relationship_html:
        relationship_block = f'<div class="section-head"><h2>Accepted relationships</h2><p>Ontology structure only; no current observations</p></div><ul class="relationship-list">{relationship_html}</ul>'
    else:
        relationship_block = ""
    return f'<section class="ontology-grid">{cards_html}</section><div class="section-head"><h2>Business object network</h2><p>Definitions and identity classes, not current measured values</p></div><section class="network">{node_html}</section>{relationship_block}'


def _validate_site_links(pages: Mapping[str, str | bytes]) -> None:
    html_pages = {name for name in pages if name.endswith(".html")}
    local_assets = set(pages)
    # Parse every HTML page once.  In particular, a large evidence page can
    # receive thousands of fragment links from audit pages; rescanning its
    # serialized bytes for each link turns validation into an accidental
    # quadratic operation.
    scanned_pages = {
        name: _scan_html_links(document)
        for name, document in pages.items()
        if name.endswith(".html")
    }
    for name, (anchors, links) in scanned_pages.items():
        _validate_link_values(anchors, links)
        for href in links:
            if href.startswith("#"):
                if href[1:] not in anchors:
                    raise ValueError(f"broken site fragment in {name}: {href}")
                continue
            path_part, _, fragment = href.partition("#")
            if not path_part:
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), path_part))
            if target in local_assets and target not in html_pages:
                if fragment:
                    raise ValueError(f"asset link cannot contain a fragment in {name}: {href}")
                continue
            if target not in html_pages:
                raise ValueError(f"broken site page link in {name}: {href}")
            if fragment:
                target_anchors = scanned_pages[target][0]
                if fragment not in target_anchors:
                    raise ValueError(f"broken site fragment in {name}: {href}")


def _evidence_detail(
    context: RunContext | None,
    record: Mapping[str, str],
    audit_records: Iterable[Mapping[str, Any]],
) -> str:
    """Render actionable, reviewed detail without consulting work artifacts."""

    reference = _text(record.get("ref"))
    sections: list[str] = []
    if context is not None and re.fullmatch(
        r"requirements/[A-Za-z0-9_.-]+/(?:accepted/(?:manifest|answer_content)\.json|integration/committed/(?:manifest\.json|records\.jsonl))",
        reference,
    ):
        path = context.resolve_run_path(reference)
        if path.is_file() and not path.is_symlink():
            exact = path.read_text(encoding="utf-8")
            sections.append(
                '<details class="evidence-detail"><summary>Open exact frozen artifact</summary>'
                f'<pre>{_escape(exact)}</pre></details>'
            )
    related = [
        dict(value)
        for value in audit_records
        if isinstance(value, Mapping)
        and reference in {_text(item) for item in _as_list(value.get("reference_union"))}
    ]
    if related:
        exact_records = json.dumps(related, ensure_ascii=False, sort_keys=True, indent=2)
        sections.append(
            f'<details class="evidence-detail"><summary>Open reviewed committed record details ({len(related)})</summary>'
            f'<pre>{_escape(exact_records)}</pre></details>'
        )
    return "".join(sections)


def render_dashboard_site(
    fixture: Mapping[str, Any],
    *,
    context: RunContext | None = None,
) -> tuple[dict[str, str | bytes], dict[str, Any]]:
    """Return a chart-led multi-page offline site from one reviewed fixture."""

    _, manifest = render_dashboard(fixture, context=context)
    raw_widgets = fixture.get("widgets") or fixture.get("items")
    assert isinstance(raw_widgets, list)
    widgets = [widget for widget in raw_widgets if isinstance(widget, Mapping)]
    raw_audit_records = fixture.get("audit_records") or []
    if not isinstance(raw_audit_records, list):
        raise ValueError("fixture audit_records must be a list")
    _validate_audit_inventory(fixture, widgets, raw_audit_records)
    _validate_fixture_presentation_plan(fixture, widgets, context=context)
    _validate_manager_admission_contract(fixture, widgets)
    domains = _normalize_domains(fixture, widgets)
    by_id = {_text(widget.get("id")): widget for widget in widgets}
    title = _manager_prose_text(_text(fixture.get("title"), "Reviewed decision workspace"))
    # Keep pipeline/provenance wording out of the manager canvas. Technical
    # details remain available on the dedicated audit pages below.
    subtitle = "Business signals and decisions."
    accepted_review = fixture.get("accepted_final_product_review")
    surface_status = (
        "Reviewed"
        if isinstance(accepted_review, Mapping)
        and _text(accepted_review.get("status")).lower() in {"accepted", "approved", "final"}
        else "Preview"
    )
    domain_cards: list[str] = []
    domain_pages: dict[str, str] = {}
    all_trace: list[dict[str, str]] = []
    trace_seen: set[str] = set()
    requirement_manifest: list[dict[str, Any]] = []
    committed_records_by_id = {
        _text(record.get("record_id")): record
        for record in raw_audit_records
        if isinstance(record, Mapping) and _text(record.get("record_id"))
    }
    record_audit_html, record_audit_traces = _render_record_audit(raw_audit_records, evidence_prefix="")
    raw_audit_widgets = fixture.get("audit_widgets") or []
    if not isinstance(raw_audit_widgets, list):
        raise ValueError("fixture audit_widgets must be a list")
    widget_audit_html, widget_audit_traces = _render_widget_audit(raw_audit_widgets, evidence_prefix="")
    for record in widget_audit_traces:
        if record["anchor"] not in trace_seen:
            all_trace.append(record)
            trace_seen.add(record["anchor"])
    for record in record_audit_traces:
        if record["anchor"] not in trace_seen:
            all_trace.append(record)
            trace_seen.add(record["anchor"])
    raw_overview_ids = fixture.get("overview_widget_ids")

    # Product's persisted manager order is the only ordering authority for
    # selected widgets.  Keep a lookup for domain rendering and manifest
    # projection; fixtures without an explicit plan retain their reviewed
    # structural order as a legacy/manual fallback.
    raw_manager_ids = fixture.get("manager_widget_ids")
    manager_order = {
        _text(widget_id).strip(): index
        for index, widget_id in enumerate(raw_manager_ids, 1)
        if _text(widget_id).strip()
    } if isinstance(raw_manager_ids, list) else {}

    if raw_overview_ids is not None:
        if not isinstance(raw_overview_ids, list) or len(set(_text(value) for value in raw_overview_ids)) != len(raw_overview_ids):
            raise ValueError("overview_widget_ids must be a unique list")
        overview_widgets = []
        for raw_id in raw_overview_ids:
            widget_id = _text(raw_id)
            widget = by_id.get(widget_id)
            if widget is None:
                raise ValueError(f"overview_widget_ids references unknown widget: {widget_id}")
            overview_widgets.append(widget)
    else:
        overview_widgets = [widget for widget in widgets if widget.get("overview") is True]
    # Build the complete requirement grouping once, then expose only groups
    # with explicitly admitted business widgets on domain pages.  Technical
    # groups remain assigned and traceable for the separate audit page.
    domain_cache: dict[str, tuple[list[Mapping[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]] = {}
    business_domains: list[Mapping[str, Any]] = []
    technical_groups: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    manager_requirement_targets: dict[str, str] = {}
    for domain in domains:
        domain_widgets: list[Mapping[str, Any]] = []
        for flow in domain["decision_flow"]:
            domain_widgets.extend(by_id[widget_id] for widget_id in flow["widget_ids"])
        groups = _requirement_groups(domain_widgets, domain)
        manager_groups = [
            group for group in groups
            if any(_manager_widget_allowed(widget, strict=_is_v4_fixture(fixture)) for widget in group["widgets"])
        ]
        domain_cache[_text(domain.get("id"))] = (domain_widgets, groups, manager_groups)
        if manager_groups:
            business_domains.append(domain)
        for group in groups:
            if group not in manager_groups:
                technical_groups.append((domain, group))
        for group in groups:
            for widget in group["widgets"]:
                records = _trace_records(widget)
                for record in records:
                    if record["anchor"] not in trace_seen:
                        all_trace.append(record)
                        trace_seen.add(record["anchor"])
    for domain in business_domains:
        domain_widgets, all_groups, groups = domain_cache[_text(domain.get("id"))]
        domain_path = f'domains/{_slug(domain["id"])}.html'
        # Domain headers stay neutral: accepted takeaways/subtitles are not
        # manager projections and therefore cannot leak unplanned values.
        domain_summary = "Business decisions and stated limits."
        domain_cards.append(
            f'<a class="domain-card" href="{_escape(domain_path)}">'
            f'<span class="eyebrow">Business domain {domain["order"]}</span>'
            f'<h2>{_escape(_manager_heading_text(domain["title"], "Business decisions"))}</h2>'
            f'<p>{_escape(domain_summary)}</p>'
            f'<span class="count">{len(groups)} reviewed decisions · Open domain →</span></a>'
        )
        quicklinks = []
        requirement_blocks = []
        for group_index, group in enumerate(groups, 1):
            requirement_id = group["id"]
            # Internal requirement IDs remain in the audit manifest only. The
            # manager page uses a neutral, stable anchor derived from the
            # visible title and group position.
            requirement_anchor = f"decision-{_slug(group.get('title') or 'view')}-{group_index}"
            manager_requirement_targets[requirement_id] = f"domains/{_slug(domain['id'])}.html#{requirement_anchor}"
            quicklinks.append(
                f'<a class="requirement-link" href="#{_escape(requirement_anchor)}">'
                f'<span class="requirement-link-title">{_escape(_manager_heading_text(group["title"]))}</span></a>'
            )
            manager_widgets = [
                widget for widget in group["widgets"]
                if _manager_widget_allowed(widget, strict=_is_v4_fixture(fixture))
            ]
            if manager_order:
                manager_widgets = sorted(
                    manager_widgets,
                    key=lambda widget: manager_order.get(
                        _text(widget.get("id")).strip(),
                        len(manager_order) + 1,
                    ),
                )
            audit_widgets = [widget for widget in group["widgets"] if widget not in manager_widgets]
            group_records: list[str] = []
            for widget in group["widgets"]:
                records = _trace_records(widget)
                for record in records:
                    if record["anchor"] not in trace_seen:
                        all_trace.append(record)
                        trace_seen.add(record["anchor"])
                group_records.extend(record["anchor"] for record in records)
            consumed_manager_ids: set[str] = set()
            manager_ids = [_text(widget.get("id")).strip() for widget in manager_widgets]
            if len(set(manager_ids)) != len(manager_ids) or any(not widget_id for widget_id in manager_ids):
                raise ValueError(f"requirement {requirement_id} has duplicate or missing manager widget IDs")
            rendered_decisions: list[str] = []
            for widget in manager_widgets:
                widget_id = _text(widget.get("id"))
                records = _trace_records(widget)
                rendered_decisions.append(_site_widget(widget, records))
                consumed_manager_ids.add(widget_id)
            if consumed_manager_ids != set(manager_ids):
                raise ValueError(f"requirement {requirement_id} manager widgets were not rendered exactly once")
            # Header values are deliberately limited to the structural
            # requirement title.  Unplanned takeaways/subtitles remain exact
            # in the collapsed audit scope and committed record payloads.
            takeaway = ""
            subtitle_html = ""
            decision_html = (
                f'<div class="decision-widget-container">{"".join(rendered_decisions)}</div>'
                if rendered_decisions
                else ""
            )
            limitation_values = [] if _is_v4_fixture(fixture) else [
                value
                for value in group.get("limitations", [])
                if _text(value).strip()
            ]
            limit_html = (
                '<section class="requirement-limitations"><h3>What this analysis does not prove</h3><ul>'
                + "".join(f'<li>{_escape(_manager_prose_text(value))}</li>' for value in limitation_values)
                + '</ul></section>'
                if limitation_values else ""
            )
            # Technical snapshots and provenance are intentionally kept on the
            # separate audit pages, never collapsed into the manager canvas.
            audit_html = ""
            requirement_blocks.append(
                f'<section class="requirement-section" data-runtime-section id="{_escape(requirement_anchor)}">'
                f'<header class="requirement-header"><div><span class="eyebrow">Decision</span><h2>{_escape(_manager_heading_text(group["title"]))}</h2>{subtitle_html}</div>{takeaway}</header>'
                f'{decision_html}{limit_html}{audit_html}</section>'
            )
            requirement_manifest.append({
                "domain_id": _text(domain.get("id")),
                "requirement_id": requirement_id,
                "title": group["title"],
                "widget_ids": [_text(widget.get("id")) for widget in group["widgets"]],
                "primary_widget_ids": [_text(widget.get("id")) for widget in manager_widgets],
                "audit_widget_ids": [_text(widget.get("id")) for widget in audit_widgets],
                "assignment_count": len(group["widgets"]),
                "business_surface": bool(manager_widgets),
                "presentation_audience": "business_manager" if manager_widgets else "technical_audit",
                "trace_anchors": sorted(set(group_records)),
            })
        quicklinks_html = f'<nav class="requirement-nav" aria-label="Requirements">{"".join(quicklinks)}</nav>' if quicklinks else ""
        body = f'{quicklinks_html}{"".join(requirement_blocks)}'
        limits_html = "" if _is_v4_fixture(fixture) else "".join(
            f'<li>{_escape(_manager_prose_text(value))}</li>'
            for value in _as_list(fixture.get("limitations"))
            if _text(value).strip()
        )
        if limits_html:
            body += f'<section class="limits"><h2>Read with these limits</h2><ul>{limits_html}</ul></section>'
        domain_pages[domain_path] = _site_page(
            title=_manager_heading_text(domain["title"], "Business decisions"),
            current=domain_path,
            domains=business_domains,
            body=body,
            prefix="../",
            subtitle=domain_summary,
            status=surface_status,
            manager_surface=True,
        )
    # Technical-only requirements intentionally have no business domain page.
    # They are still complete, clickable, and exact on one separate audit
    # surface so model/data-quality work remains reusable without masquerading
    # as a business decision.
    technical_blocks: list[str] = []
    for domain, group in technical_groups:
        requirement_id = _text(group.get("id"))
        technical_blocks.append(
            f'<section class="audit-requirement" data-runtime-section id="{_escape(_requirement_anchor(requirement_id))}">'
            f'<header><span class="eyebrow requirement-id-badge">{_escape(requirement_id)}</span>'
            f'<h2>{_escape(_manager_prose_text(group.get("title")))}</h2></header>'
            f'{_render_technical_audit(group["widgets"], [], scope=group.get("scope", ""), evidence_prefix="", committed_records=committed_records_by_id, audit_widget_ids={_text(entry.get("widget_id")) for entry in raw_audit_widgets if isinstance(entry, Mapping)} if _is_v4_fixture(fixture) else None)}'
            f'</section>'
        )
        requirement_manifest.append({
            "domain_id": _text(domain.get("id")),
            "requirement_id": requirement_id,
            "title": group.get("title"),
            "widget_ids": [_text(widget.get("id")) for widget in group["widgets"]],
            "primary_widget_ids": [],
            "audit_widget_ids": [_text(widget.get("id")) for widget in group["widgets"]],
            "assignment_count": len(group["widgets"]),
            "business_surface": False,
            "presentation_audience": "technical_audit",
            "trace_anchors": sorted({record["anchor"] for widget in group["widgets"] for record in _trace_records(widget)}),
        })
    data_quality_body = (
        '<section class="section-head"><h2>Technical audit records</h2>'
        '<p>Data quality, model, mapping, coverage, and ontology mechanics remain exact and traceable here; they are not business conclusions.</p></section>'
        + _render_visual_gallery(
            widgets,
            [entry for entry in _as_list(fixture.get("visual_entries")) if isinstance(entry, Mapping)],
            audience="technical_audit_gallery",
        )
        + (record_audit_html or "")
        + (widget_audit_html or "")
        + ("".join(technical_blocks) or '<p class="network-empty">No technical-only requirement records were supplied.</p>')
    )
    overview_cards = []
    portfolio_title = title
    for widget in overview_widgets:
        # Domain pages render the validated manager projection through
        # ``_site_widget``.  Reuse that same projection here instead of
        # reading raw fixture geometry: a stale/raw bar must never disagree
        # with the pointer-bound plan value shown on the requirement page.
        display_widget = _manager_surface_widget(widget)
        kind = _text(display_widget.get("type") or display_widget.get("kind"), "kpi").lower()
        target = manager_requirement_targets.get(_text(widget.get("requirement_id") or ""), "")
        widget_title = (
            _manager_title(display_widget)
            if display_widget.get("__explicit_plan_projection") is True
            else _manager_prose_text(_manager_title(display_widget))
        )
        if kind == "kpi":
            value = display_widget.get("manager_display_value") or (display_widget.get("value") if display_widget.get("value") is not None else display_widget.get("display_value"))
            unit = _manager_prose_text(display_widget.get("unit"))
            unit_html = f'<small>{_escape(unit)}</small>' if unit else ""
            card = f'<article class="overview-kpi"><span class="eyebrow">Signal</span><h2>{_escape(widget_title)}</h2><strong>{_escape(_compact_scalar_display(value))}</strong>{unit_html}'
        else:
            try:
                if kind in {"table", "status_table"}:
                    visual = _render_site_table(display_widget)
                else:
                    visual = _render_visual(display_widget)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                visual = _render_visual_fallback(display_widget, manager_view=True)
            card = f'<article class="overview-surface overview-{_escape(_slug(kind))}"><span class="eyebrow">Signal</span><h2>{_escape(widget_title)}</h2>{visual}'
        if target:
            card += f'<a href="{_escape(target)}">Open requirement →</a>'
        card += "</article>"
        overview_cards.append(card)
    overview_kpi_html = f'<section class="overview-kpis"><div class="section-head"><h2>Priority signals</h2><p>Explicitly selected reviewed business signals; details stay on requirement pages</p></div><div class="kpi-strip">{"".join(overview_cards)}</div></section>' if overview_cards else ""
    overview = _site_page(
        title=portfolio_title,
        current="index.html",
        domains=business_domains,
        body=f'{overview_kpi_html}<section class="section-head overview-heading"><h2>Decision domains</h2><p>Short navigation into requirement-level decisions</p></section><section class="domain-grid">{"".join(domain_cards)}</section>',
        subtitle=subtitle,
        status=surface_status,
        manager_surface=True,
    )
    evidence_items = "".join(
        f'<li id="{_escape(record["anchor"])}"><strong>{_escape(record["label"])}</strong><span>{_escape(record["ref"])}</span>'
        f'{_evidence_detail(context, record, raw_audit_records)}</li>'
        for record in all_trace
    )
    failed_items_html = _render_failed_items(fixture)
    limitations = "".join(f'<li>{_escape(value)}</li>' for value in _as_list(fixture.get("limitations")) if _text(value))
    chart_map_ref = _text(fixture.get("chart_map_ref"))
    provenance_note = f'<p class="evidence-note">Chart definitions and exact values: <code>{_escape(chart_map_ref)}</code></p>' if chart_map_ref else ""
    evidence_body = f'<section><div class="section-head"><h2>Reviewed evidence references</h2><p>Links preserved from accepted outputs</p></div>{provenance_note}<ul class="evidence-list">{evidence_items}</ul></section>{failed_items_html}'
    if limitations:
        evidence_body += f'<section class="limits"><h2>Assumptions and limitations</h2><ul>{limitations}</ul></section>'
    pages = {
        "index.html": overview,
        **domain_pages,
        "data-quality-audit.html": _site_page(
            title="Data quality & model audit",
            current="data-quality-audit.html",
            domains=business_domains,
            body=data_quality_body,
            subtitle="Technical mechanics remain exact, collapsed, and traceable outside the business manager surface.",
            status=surface_status,
            manager_surface=False,
        ),
        "ontology.html": _site_page(title="Ontology", current="ontology.html", domains=[], body=_ontology_body(fixture), subtitle="Reusable definitions and model audit; not a business decision surface.", status=surface_status, manager_surface=False),
        "evidence.html": _site_page(title="Evidence & audit", current="evidence.html", domains=business_domains, body=evidence_body, subtitle="Compact provenance, limitations and reviewed source references.", status=surface_status, manager_surface=False),
        "assets/dashboard.css": _canonical_dashboard_css(),
        "assets/dashboard.js": _canonical_dashboard_js(),
        "assets/favicon.svg": _OFFLINE_FAVICON_SVG,
    }
    _validate_site_links(pages)
    # Keep the serialized manifest's item order aligned with the same
    # Product-selected sequence used by domain pages.  Unselected/audit items
    # retain their original fixture order after the selected manager items.
    if manager_order and isinstance(manifest.get("items"), list):
        original_items = list(manifest["items"])
        manifest["items"] = [
            item
            for _index, item in sorted(
                enumerate(original_items),
                key=lambda pair: (
                    0,
                    manager_order.get(
                        _text(pair[1].get("element_id", "")).removeprefix("widget-").strip(),
                        len(manager_order) + 1,
                    ),
                ) if _text(pair[1].get("element_id", "")).removeprefix("widget-").strip() in manager_order
                else (1, pair[0]),
            )
        ]
    supplied_site_version = fixture.get("site_version") or fixture.get("dashboard_version")
    try:
        site_version = int(supplied_site_version) if supplied_site_version is not None else 2
    except (TypeError, ValueError):
        site_version = 2
    site_manifest = {
        **manifest,
        "product_type": "offline_static_dashboard_site",
        "status": surface_status,
        "review_status": surface_status,
        "site_version": site_version,
        "pages": ["index.html", *domain_pages, "data-quality-audit.html", "ontology.html", "evidence.html"],
        "assets": ["assets/dashboard.css", "assets/dashboard.js", "assets/favicon.svg"],
        "runtime": {"asset": "assets/dashboard.js", "deterministic": True, "network": False},
        "favicon_ref": "assets/favicon.svg",
        "chart_led": True,
        "tables_collapsed": True,
        "manager_surface": "summary_first",
        "technical_audit_collapsed": True,
        "primary_widget_count": sum(1 for widget in widgets if _text(widget.get("presentation_tier"), "primary").lower() != "audit"),
        "audit_widget_count": sum(1 for widget in widgets if _text(widget.get("presentation_tier"), "primary").lower() == "audit"),
        "business_requirement_count": sum(1 for entry in requirement_manifest if entry.get("business_surface") is not False and entry.get("primary_widget_ids")),
        "technical_requirement_count": sum(1 for entry in requirement_manifest if not entry.get("primary_widget_ids")),
        "manager_admission_policy": _text((fixture.get("manager_admission") or {}).get("policy")),
        "presentation_plan_ref": fixture.get("presentation_plan_ref"),
        "presentation_plan_sha256": fixture.get("presentation_plan_sha256"),
        "presentation_plan_schema": fixture.get("presentation_plan_schema"),
        "manager_widget_ids": list(fixture.get("manager_widget_ids") or []),
        "manager_visual_widget_ids": list(fixture.get("manager_visual_widget_ids") or []),
        "audit_visual_widget_ids": list(fixture.get("audit_visual_widget_ids") or []),
        "visual_widget_count": len(_as_list(fixture.get("visual_entries"))),
        "visual_gallery_widget_ids": [
            _text(entry.get("widget_id"))
            for entry in _as_list(fixture.get("visual_entries"))
            if isinstance(entry, Mapping) and entry.get("presentation_audience") == "technical_audit_gallery"
        ],
        "overview_widget_ids": [_text(widget.get("id")) for widget in overview_widgets],
        "audit_record_count": len(raw_audit_records),
        "audit_widget_entry_count": len(raw_audit_widgets),
        "audit_widget_entry_ids": [_text(entry.get("widget_id")) for entry in raw_audit_widgets if isinstance(entry, Mapping)],
        "audit_record_ids": [
            f"{_text(record.get('item_id'))}:{_text(record.get('record_id'))}"
            for record in raw_audit_records
            if isinstance(record, Mapping) and _text(record.get("record_id"))
        ],
        "requirement_groups": requirement_manifest,
        "chart_map_ref": chart_map_ref,
    }
    return pages, site_manifest


def _prune_renderer_controlled_files(
    output_root: Path,
    *,
    expected_pages: Iterable[str],
    expected_assets: Iterable[str],
) -> None:
    """Remove stale files only from the renderer-owned site namespaces.

    Delta builds start from a copy of the parent site so that unchanged pages
    retain their bytes.  The renderer's expected page/asset set is therefore
    authoritative for its own namespaces; an old domain page must not survive
    a corrected fixture.  We reject symlinks before touching anything and only
    remove regular files under the fixed page/assets names or ``domains/``.
    Files outside those renderer-owned paths are left untouched.
    """

    if output_root.is_symlink() or (output_root.exists() and not output_root.is_dir()):
        raise ValueError("site output root is missing, non-directory, or symlinked")
    output_root.mkdir(parents=True, exist_ok=True)
    for path in output_root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"site output contains symlink: {path.relative_to(output_root).as_posix()}")
    expected = {str(value).replace("\\", "/").lstrip("./") for value in (*expected_pages, *expected_assets)}
    expected.add("site_manifest.json")
    fixed_owned = {"index.html", "ontology.html", "evidence.html", "data-quality-audit.html", "site_manifest.json", "assets/dashboard.css", "assets/dashboard.js", "assets/favicon.svg"}

    def renderer_owned(relative: str) -> bool:
        return relative in fixed_owned or relative.startswith("domains/")

    for path in sorted(output_root.rglob("*"), key=lambda value: (len(value.parts), value.as_posix()), reverse=True):
        relative = path.relative_to(output_root).as_posix()
        if relative in expected or not renderer_owned(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"site output contains symlink: {relative}")
        if path.is_file():
            path.unlink()
        elif path.is_dir() and relative.startswith("domains/"):
            # Empty renderer-owned domain directories are safe to prune; a
            # directory containing an unowned file is retained.
            try:
                path.rmdir()
            except OSError:
                pass


def _bound_dataset_for_widget(widget: Mapping[str, Any], chart: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the runtime's metadata-only dataset binding exactly.

    Blueprint validation must check the rows and provenance that the runtime
    bound, rather than accepting a dataset with only a matching ID.  This is
    deliberately the same lossless field selection used by
    ``dashboard_runtime._dataset_for_widget``: it never derives a measure or
    alters reviewed values.
    """

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
    dimensions = widget.get("dimensions", fields.get("dimensions"))
    measures = widget.get("measures", fields.get("measures"))
    source_record_ids = sorted(
        {
            _text(value).strip()
            for value in _as_list(widget.get("integration_record_ids"))
            if _text(value).strip()
        }
        | ({_text(widget.get("integration_record_id")).strip()} if _text(widget.get("integration_record_id")).strip() else set())
    )
    return {
        "id": f"dataset-{_text(widget.get('id'), 'visual')}",
        "requirement_id": _text(widget.get("requirement_id")),
        "source_record_ids": source_record_ids,
        "rows": rows,
        "row_count": len(rows) if isinstance(rows, list) else (1 if rows is not None else 0),
        "grain": copy.deepcopy(widget.get("grain", fields.get("grain"))),
        "dimensions": copy.deepcopy(dimensions),
        "measures": copy.deepcopy(measures),
        "time": copy.deepcopy(widget.get("time", fields.get("time"))),
        "denominator": copy.deepcopy(widget.get("denominator", fields.get("denominator"))),
        "coverage": copy.deepcopy(widget.get("coverage", fields.get("coverage"))),
        "limitations": copy.deepcopy(widget.get("limitations", fields.get("limitations", []))),
    }


def _validate_bound_blueprint(
    context: RunContext,
    fixture_path: Path,
    fixture: Mapping[str, Any],
    blueprint_ref: str | Path,
) -> None:
    """Validate the staged V2 Blueprint consumed by the renderer.

    Assembly creates this file before rendering.  Keeping the check here
    prevents a renderer call from silently selecting a family/layout from
    fixture metadata when the Product Agent supplied a different, validated
    choice in its Blueprint.
    """

    resolved = context.resolve_run_path(blueprint_ref)
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("bound dashboard blueprint is missing or symlinked")
    try:
        blueprint = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("bound dashboard blueprint is invalid") from exc
    if not isinstance(blueprint, Mapping) or blueprint.get("schema_version") != "dashboard.business_presentation_plan.v2":
        raise ValueError("bound dashboard blueprint must be V2")
    if blueprint.get("kind") != "dashboard_blueprint":
        raise ValueError("bound dashboard blueprint kind is invalid")
    if blueprint.get("run_id") != fixture.get("run_id") or blueprint.get("generation_id") != fixture.get("generation_id"):
        raise ValueError("bound dashboard blueprint run/generation binding drifted")
    if blueprint.get("source_policy") != "accepted_and_committed_only":
        raise ValueError("bound dashboard blueprint source policy is invalid")
    source = blueprint.get("source_bindings")
    expected_source_keys = {
        "fixture_ref", "fixture_sha256", "chart_map_ref", "chart_map_sha256",
        "chart_registry_ref", "chart_registry_sha256", "blueprint_ref",
        "presentation_plan_ref", "presentation_plan_sha256",
    }
    if not isinstance(source, Mapping) or set(source) != expected_source_keys:
        raise ValueError("bound dashboard blueprint source bindings are missing")
    try:
        fixture_ref = fixture_path.relative_to(context.run_root).as_posix()
    except ValueError as exc:
        raise ValueError("bound dashboard fixture escaped the run root") from exc
    try:
        expected_blueprint_ref = context.resolve_run_path(blueprint_ref).relative_to(context.run_root).as_posix()
    except (AllowedRootError, ValueError) as exc:
        raise ValueError("bound dashboard blueprint reference escaped the run root") from exc
    if source.get("blueprint_ref") != expected_blueprint_ref:
        raise ValueError("bound dashboard blueprint self reference drifted")
    if source.get("fixture_ref") != fixture_ref or source.get("fixture_sha256") != hashlib.sha256(fixture_path.read_bytes()).hexdigest():
        raise ValueError("bound dashboard blueprint fixture binding drifted")
    chart_map_ref = _text(fixture.get("chart_map_ref")).strip()
    if not chart_map_ref:
        raise ValueError("bound dashboard fixture chart map reference is missing")
    chart_path = context.resolve_run_path(chart_map_ref)
    if chart_path.is_symlink() or not chart_path.is_file():
        raise ValueError("bound dashboard chart map is missing or symlinked")
    if source.get("chart_map_ref") != chart_map_ref or source.get("chart_map_sha256") != hashlib.sha256(chart_path.read_bytes()).hexdigest():
        raise ValueError("bound dashboard blueprint chart-map binding drifted")
    registry_ref = _text(fixture.get("chart_registry_ref")).strip()
    if not registry_ref:
        raise ValueError("bound dashboard fixture chart registry reference is missing")
    registry_path = context.resolve_run_path(registry_ref)
    if registry_path.is_symlink() or not registry_path.is_file():
        raise ValueError("bound dashboard chart registry is missing or symlinked")
    registry_hash = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    if source.get("chart_registry_ref") != registry_ref or source.get("chart_registry_sha256") != registry_hash:
        raise ValueError("bound dashboard blueprint registry binding drifted")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("bound dashboard chart registry is invalid") from exc
    if not isinstance(registry, Mapping) or registry.get("schema_version") != _V4_REGISTRY_SCHEMA or not isinstance(registry.get("families"), list):
        raise ValueError("bound dashboard chart registry is invalid")

    # Presentation-plan refs are optional only for the pre-plan/audit-only
    # path (including the terminal limited dashboard).  If either side is
    # present, both refs and hashes must resolve to the exact immutable V2
    # plan that produced the fixture's manager projections.
    fixture_plan_ref = fixture.get("presentation_plan_ref")
    fixture_plan_hash = fixture.get("presentation_plan_sha256")
    if (fixture_plan_ref is None) != (fixture_plan_hash is None):
        raise ValueError("bound dashboard fixture presentation-plan binding is incomplete")
    if source.get("presentation_plan_ref") != fixture_plan_ref or source.get("presentation_plan_sha256") != fixture_plan_hash:
        raise ValueError("bound dashboard blueprint presentation-plan binding drifted")
    presentation_plan: Mapping[str, Any] | None = None
    presentation_plan_path: Path | None = None
    if fixture_plan_ref is not None:
        if not isinstance(fixture_plan_ref, str) or not fixture_plan_ref.strip() or not isinstance(fixture_plan_hash, str):
            raise ValueError("bound dashboard presentation-plan binding is invalid")
        plan_path = context.resolve_run_path(fixture_plan_ref)
        if plan_path.is_symlink() or not plan_path.is_file() or hashlib.sha256(plan_path.read_bytes()).hexdigest() != fixture_plan_hash:
            raise ValueError("bound dashboard presentation-plan hash mismatch")
        try:
            presentation_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("bound dashboard presentation plan is invalid") from exc
        if not isinstance(presentation_plan, Mapping) or presentation_plan.get("schema_version") != "dashboard.business_presentation_plan.v2":
            raise ValueError("bound dashboard presentation plan must be V2")
        if presentation_plan.get("run_id") != fixture.get("run_id") or presentation_plan.get("generation_id") != fixture.get("generation_id"):
            raise ValueError("bound dashboard presentation-plan lineage drifted")
        presentation_plan_path = plan_path
    try:
        chart_map = json.loads(chart_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("bound dashboard chart map is invalid") from exc
    if not isinstance(chart_map, Mapping) or not isinstance(chart_map.get("charts"), list):
        raise ValueError("bound dashboard chart map is invalid")
    widgets = {
        _text(widget.get("id")): widget
        for widget in _as_list(fixture.get("widgets"))
        if isinstance(widget, Mapping) and _text(widget.get("id"))
    }
    raw_charts = chart_map.get("charts", [])
    chart_ids = [
        _text(chart.get("id")).strip()
        for chart in raw_charts
        if isinstance(chart, Mapping) and _text(chart.get("id")).strip()
    ]
    if len(chart_ids) != len(raw_charts) or len(set(chart_ids)) != len(chart_ids):
        raise ValueError("bound dashboard chart map IDs are invalid")
    if chart_map.get("fixture_ref") != fixture_ref or chart_map.get("chart_registry_ref") != registry_ref:
        raise ValueError("bound dashboard chart-map source bindings drifted")
    charts = {
        _text(chart.get("id")): chart
        for chart in raw_charts
        if isinstance(chart, Mapping) and _text(chart.get("id"))
    }
    fixture_widgets = [widget for widget in _as_list(fixture.get("widgets")) if isinstance(widget, Mapping)]
    by_id = {_text(widget.get("id")): widget for widget in fixture_widgets}
    fixture_ids = [_text(widget.get("id")).strip() for widget in fixture_widgets]
    if len(fixture_ids) != len(fixture_widgets) or any(not value for value in fixture_ids) or len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("bound dashboard fixture widget IDs are invalid")
    if set(charts) != set(fixture_ids):
        raise ValueError("bound dashboard chart map/widget coverage drifted")
    if presentation_plan is not None:
        if presentation_plan_path is None:
            raise ValueError("bound dashboard presentation-plan path is missing")
        # Reuse the renderer's exact fixture/plan projection validator before
        # checking the Blueprint.  This binds every manager and audit visual
        # to the same V2 visual-entry snapshots, not just manager metadata.
        _validate_fixture_presentation_plan_v2(
            fixture,
            fixture_widgets,
            presentation_plan,
            plan_path=presentation_plan_path,
            context=context,
        )
    visuals = blueprint.get("visuals")
    expected_visual_ids = sorted(fixture_ids)
    if not isinstance(visuals, list) or not visuals:
        raise ValueError("bound dashboard blueprint visuals are invalid")
    visual_ids = [_text(visual.get("id")).strip() if isinstance(visual, Mapping) else "" for visual in visuals]
    if visual_ids != expected_visual_ids or len(set(visual_ids)) != len(visual_ids):
        raise ValueError("bound dashboard blueprint visual coverage/order is invalid")
    datasets = blueprint.get("datasets")
    if not isinstance(datasets, list) or [
        _text(dataset.get("id")).strip() if isinstance(dataset, Mapping) else ""
        for dataset in datasets
    ] != [f"dataset-{widget_id}" for widget_id in expected_visual_ids]:
        raise ValueError("bound dashboard blueprint dataset coverage/order is invalid")
    dataset_by_id = {
        _text(dataset.get("id")).strip(): dataset
        for dataset in datasets
        if isinstance(dataset, Mapping) and _text(dataset.get("id")).strip()
    }
    if len(dataset_by_id) != len(datasets):
        raise ValueError("bound dashboard blueprint datasets are invalid")
    for visual_id in expected_visual_ids:
        expected_dataset = _bound_dataset_for_widget(by_id[visual_id], charts[visual_id])
        if dataset_by_id.get(f"dataset-{visual_id}") != expected_dataset:
            raise ValueError(f"bound dashboard blueprint dataset payload drifted: {visual_id}")
    pages = blueprint.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("bound dashboard blueprint pages are invalid")
    page_ids = { _text(page.get("id")).strip() for page in pages if isinstance(page, Mapping) }
    section_ids: set[str] = set()
    page_visual_ids: list[str] = []
    for page in pages:
        if not isinstance(page, Mapping) or not isinstance(page.get("sections"), list):
            raise ValueError("bound dashboard blueprint page sections are invalid")
        for section in page["sections"]:
            if not isinstance(section, Mapping) or not isinstance(section.get("visual_ids"), list):
                raise ValueError("bound dashboard blueprint section is invalid")
            section_id = _text(section.get("id")).strip()
            if not section_id or section_id in section_ids:
                raise ValueError("bound dashboard blueprint section IDs are invalid")
            section_ids.add(section_id)
            for visual_id in section["visual_ids"]:
                visual_id = _text(visual_id).strip()
                if visual_id not in expected_visual_ids or visual_id in page_visual_ids:
                    raise ValueError("bound dashboard blueprint page visual coverage is invalid")
                page_visual_ids.append(visual_id)
    if set(page_visual_ids) != set(expected_visual_ids) or len(page_visual_ids) != len(expected_visual_ids):
        raise ValueError("bound dashboard blueprint pages omit or duplicate visuals")
    bp_by_id = { _text(visual.get("id")): visual for visual in visuals }
    plan_manager_visual_ids = (
        set(presentation_plan.get("manager_visual_widget_ids") or [])
        if isinstance(presentation_plan, Mapping)
        else None
    )
    for visual_id in expected_visual_ids:
        visual = bp_by_id[visual_id]
        widget = by_id[visual_id]
        chart = charts[visual_id]
        if visual.get("dataset_id") != f"dataset-{visual_id}":
            raise ValueError(f"bound dashboard blueprint dataset binding drifted: {visual_id}")
        if visual.get("family") != _text(chart.get("family")) or visual.get("type") != _text(widget.get("type") or widget.get("kind")):
            raise ValueError(f"bound dashboard blueprint visual identity drifted: {visual_id}")
        if visual.get("page_id") not in page_ids or visual.get("section_id") not in section_ids:
            raise ValueError(f"bound dashboard blueprint visual page binding drifted: {visual_id}")
        expected_chart_intent = _text(widget.get("chart_intent") or widget.get("title") or visual_id)
        if visual.get("chart_intent") != expected_chart_intent:
            raise ValueError(f"bound dashboard blueprint chart intent drifted: {visual_id}")
        expected_encodings = copy.deepcopy(widget.get("encodings", chart.get("encodings", {})))
        expected_filters = copy.deepcopy(widget.get("filters", []))
        expected_drilldown = copy.deepcopy(widget.get("drilldown", {"enabled": True}))
        expected_empty_state = copy.deepcopy(widget.get("empty_state", "No reviewed values are available for this view."))
        for key, expected in (
            ("encodings", expected_encodings),
            ("filters", expected_filters),
            ("drilldown", expected_drilldown),
            ("empty_state", expected_empty_state),
        ):
            if visual.get(key) != expected:
                raise ValueError(f"bound dashboard blueprint visual metadata drifted: {visual_id}:{key}")
        expected_provenance = {
            key: copy.deepcopy(widget[key])
            for key in (
                "integration_record_id",
                "integration_record_ids",
                "integration_record_ref",
                "integration_record_refs",
                "evidence_refs",
                "trace_refs",
            )
            if key in widget
        }
        if visual.get("provenance") != expected_provenance:
            raise ValueError(f"bound dashboard blueprint visual provenance drifted: {visual_id}")
        if plan_manager_visual_ids is None or visual_id in plan_manager_visual_ids:
            for key in ("recipe_id", "layout", "renderer_type"):
                if not isinstance(visual.get(key), str) or not visual[key].strip():
                    raise ValueError(f"bound dashboard blueprint visual selection is missing: {visual_id}:{key}")
        selection = widget.get("manager_presentation")
        if isinstance(selection, Mapping):
            for key in ("recipe_id", "layout", "renderer_type"):
                if key in selection and selection.get(key) != visual.get(key):
                    raise ValueError(f"bound dashboard blueprint selection drifted: {visual_id}:{key}")
    if presentation_plan is not None:
        manager_visual_ids = presentation_plan.get("manager_visual_widget_ids")
        audit_visual_ids = presentation_plan.get("audit_visual_widget_ids")
        if not isinstance(manager_visual_ids, list) or not isinstance(audit_visual_ids, list) or len(set(manager_visual_ids + audit_visual_ids)) != len(manager_visual_ids + audit_visual_ids):
            raise ValueError("bound dashboard presentation-plan visual partition is invalid")
        plan_visual_ids = list(manager_visual_ids) + list(audit_visual_ids)
        if any(visual_id not in expected_visual_ids for visual_id in plan_visual_ids):
            raise ValueError("bound dashboard presentation-plan visuals are not in fixture")
        plan_entries = presentation_plan.get("visual_entries")
        if not isinstance(plan_entries, list) or [
            _text(entry.get("widget_id")).strip() if isinstance(entry, Mapping) else ""
            for entry in plan_entries
        ] != plan_visual_ids:
            raise ValueError("bound dashboard presentation-plan visual order is invalid")
        rendered_visual_ids = [
            widget_id
            for widget_id in fixture_ids
            if _is_partition_visual(by_id[widget_id], charts[widget_id])
        ]
        if len(plan_visual_ids) != len(rendered_visual_ids) or set(plan_visual_ids) != set(rendered_visual_ids):
            raise ValueError("bound dashboard presentation-plan visual coverage is incomplete")
        plan_visual_by_id = {
            _text(entry.get("widget_id")).strip(): entry
            for entry in plan_entries
            if isinstance(entry, Mapping) and _text(entry.get("widget_id")).strip()
        }
        if len(plan_visual_by_id) != len(plan_entries):
            raise ValueError("bound dashboard presentation-plan visual entries are invalid")
        runtime = _dashboard_runtime_module()
        manager_visual_set = set(manager_visual_ids)
        for visual_id in plan_visual_ids:
            visual = bp_by_id[visual_id]
            widget = by_id[visual_id]
            chart = charts[visual_id]
            plan_entry = plan_visual_by_id.get(visual_id)
            if not isinstance(plan_entry, Mapping):
                raise ValueError(f"bound dashboard presentation-plan visual is missing: {visual_id}")
            # Manager visuals must bind the Product Agent's explicit recipe
            # choice.  Audit-only visuals may have no executable geometry (for
            # example a later empty accepted declaration), so their source
            # chart family remains audit evidence without requiring an
            # ineligible manager recipe.
            if visual_id in manager_visual_set:
                for key in ("recipe_id", "layout", "renderer_type"):
                    if visual.get(key) != plan_entry.get(key):
                        raise ValueError(f"bound dashboard blueprint selection drifted from plan: {visual_id}:{key}")
            if (
                visual.get("requirement_id") != plan_entry.get("requirement_id")
                or visual.get("type") != plan_entry.get("visual_type")
                or visual.get("family") != plan_entry.get("chart_family")
            ):
                raise ValueError(f"bound dashboard blueprint visual envelope drifted from plan: {visual_id}")
            actual_record_ids = sorted({
                _text(value).strip()
                for value in (_as_list(widget.get("integration_record_ids")) or [widget.get("integration_record_id")])
                if _text(value).strip()
            })
            if plan_entry.get("record_ids") != actual_record_ids:
                raise ValueError(f"bound dashboard presentation-plan record binding drifted: {visual_id}")
            if visual_id in manager_visual_set:
                recipes = runtime.eligible_chart_recipes(widget, chart, registry)
                selected_recipe = _text(plan_entry.get("recipe_id")).strip()
                selected = next(
                    (recipe for recipe in recipes if isinstance(recipe, Mapping) and recipe.get("id") == selected_recipe),
                    None,
                )
                if not isinstance(selected, Mapping) or selected.get("eligible") is not True:
                    raise ValueError(f"bound dashboard presentation-plan recipe is ineligible: {visual_id}")
                renderer_types = selected.get("renderer_types")
                if not isinstance(renderer_types, list) or plan_entry.get("renderer_type") not in renderer_types:
                    raise ValueError(f"bound dashboard presentation-plan renderer is ineligible: {visual_id}")
    for visual in visuals:
        if not isinstance(visual, Mapping):
            raise ValueError("bound dashboard blueprint visual is invalid")


def render_site_fixture(
    context: RunContext,
    fixture_path: str | Path,
    output_dir: str | Path,
    manifest_path: str | Path | None = None,
    *,
    blueprint_ref: str | Path | None = None,
) -> dict[str, Any]:
    """Render one reviewed fixture as a bounded multi-page product site."""

    if not isinstance(context, RunContext):
        raise TypeError("render_site_fixture requires one RunContext")
    resolved_fixture = context.resolve_run_path(fixture_path)
    resolved_output = context.resolve_product_path(output_dir)
    resolved_manifest = context.resolve_product_path(manifest_path) if manifest_path is not None else None
    if not resolved_fixture.is_file():
        raise FileNotFoundError(resolved_fixture)
    fixture = json.loads(resolved_fixture.read_text(encoding="utf-8"))
    if not isinstance(fixture, Mapping):
        raise ValueError("dashboard fixture root must be a JSON object")
    if blueprint_ref is not None:
        _validate_bound_blueprint(context, resolved_fixture, fixture, blueprint_ref)
    pages, manifest = render_dashboard_site(fixture, context=context)
    _prune_renderer_controlled_files(
        resolved_output,
        expected_pages=manifest.get("pages", pages.keys()),
        expected_assets=manifest.get("assets", ()),
    )
    for relative, content in pages.items():
        destination = context.resolve_product_path(Path(output_dir) / relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            destination.write_bytes(content)
        else:
            destination.write_text(content.rstrip() + "\n", encoding="utf-8")
    if resolved_manifest is not None:
        resolved_manifest.parent.mkdir(parents=True, exist_ok=True)
        resolved_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def render_fixture(
    context: RunContext,
    fixture_path: str | Path,
    output_path: str | Path,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Render one reviewed fixture through one bounded :class:`RunContext`.

    ``fixture_path`` is run-relative (the reviewed fixture is already a
    system-owned run artifact); output and manifest paths are products-relative.
    Every path is resolved before the fixture is probed/read or any directory
    is created.  The pure :func:`render_dashboard` function remains available
    for callers that already hold a validated in-memory fixture.
    """

    if not isinstance(context, RunContext):
        raise TypeError("render_fixture requires one RunContext")
    # Resolve all paths up front.  In particular, do not read the fixture
    # before a malicious output/manifest path has been rejected.
    resolved_fixture = context.resolve_run_path(fixture_path)
    resolved_product_root = context.resolve_product_path("")
    if resolved_product_root != context.run_root / "products":
        raise AllowedRootError("products root must be the current run's products directory")
    resolved_output = context.resolve_product_path(output_path)
    resolved_manifest = context.resolve_product_path(manifest_path) if manifest_path is not None else None
    if not resolved_fixture.is_file():
        raise FileNotFoundError(resolved_fixture)
    fixture = json.loads(resolved_fixture.read_text(encoding="utf-8"))
    if not isinstance(fixture, Mapping):
        raise ValueError("dashboard fixture root must be a JSON object")
    document, manifest = render_dashboard(fixture)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(document.rstrip() + "\n", encoding="utf-8")
    if resolved_manifest is not None:
        resolved_manifest.parent.mkdir(parents=True, exist_ok=True)
        resolved_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True, help="current run root")
    parser.add_argument("--run-id", required=True, help="simple current run identifier")
    parser.add_argument("--input", "--fixture", dest="fixture_path", required=True, help="run-relative reviewed widget fixture JSON")
    outputs = parser.add_mutually_exclusive_group(required=True)
    outputs.add_argument("--output", help="products-relative single-page offline HTML output")
    outputs.add_argument("--site-output-dir", help="products-relative directory for the multi-page offline site")
    parser.add_argument("--manifest-output", help="optional products-relative dashboard manifest JSON")
    args = parser.parse_args(argv)
    try:
        context = RunContext(run_id=args.run_id, run_root=args.run_root)
        if args.site_output_dir:
            manifest = render_site_fixture(context, args.fixture_path, args.site_output_dir, args.manifest_output)
            output = str(context.resolve_product_path(Path(args.site_output_dir) / "index.html"))
        else:
            manifest = render_fixture(context, args.fixture_path, args.output, args.manifest_output)
            output = str(context.resolve_product_path(args.output))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"dashboard renderer: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": output, "internal_links_checked": manifest["internal_links_checked"], "widget_count": len(manifest["items"])}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
