"""Contract checks for the single deterministic dashboard runtime.

These tests stay at the artifact/runtime boundary: they inspect the committed
JavaScript bytes and exercise the renderer's supplied-value chart families
without starting a server, reading a source, or making a network call.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys

import pytest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_script(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dashboard_renderer = _load_script(
    "dashboard_renderer_runtime_contract_test",
    ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_renderer.py",
)
dashboard_runtime = _load_script(
    "dashboard_runtime_contract_test",
    ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_runtime.py",
)

REGISTRY_PATH = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "dashboard_chart_registry.json"
RUNTIME_PATH = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "dashboard.js"


def _freeze_markers() -> dict[str, bool]:
    return {
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_data_registry_frozen": True,
        "dashboard_frozen": True,
        "telemetry_frozen": True,
    }


def _simple_fixture() -> dict[str, Any]:
    return {
        "title": "Business runtime contract",
        "freeze_markers": _freeze_markers(),
        "widgets": [
            {
                "id": "ops-signal",
                "type": "bar",
                "title": "Reviewed operations signal",
                "rows": [{"label": "Queue A", "value": 7, "size": "70%"}],
                "requirement_id": "REQ-OPS",
                "requirement_title": "Operational comparison",
                "reviewed_item_ref": "requirements/REQ-OPS/accepted/manifest.json",
                "reviewed_output_ref": "requirements/REQ-OPS/accepted/answer_content.json",
                "evidence_refs": ["work/evidence.json"],
                "trace_refs": ["work/evidence.json"],
            }
        ],
        "domains": [
            {
                "id": "operations",
                "title": "Operations",
                "order": 1,
                "decision_flow": [
                    {
                        "id": "operations-flow",
                        "title": "Operations",
                        "order": 1,
                        "widget_ids": ["ops-signal"],
                    }
                ],
            }
        ],
    }


def test_renderer_visual_partition_excludes_status_table_facts() -> None:
    """The renderer mirrors the assembler's exact V2 visual universe.

    A live-shaped fixture has 36 executable visual widgets plus 21 legacy
    ``status_table`` dashboard-fact projections.  The latter remain audit
    records and must not be admitted to the manager/audit visual partition.
    Explicit reviewed/limited/accepted tables remain legitimate visuals.
    """

    assert dashboard_renderer._is_partition_visual(
        {"type": "status_table", "dashboard_fact": True},
        {"type": "status_table"},
    ) is False
    assert dashboard_renderer._is_partition_visual(
        {"type": "table", "dashboard_fact": True},
        {"type": "table"},
    ) is True
    assert dashboard_renderer._is_partition_visual(
        {"type": "table", "limited_empty_state": True},
        {"type": "table"},
    ) is True
    assert dashboard_renderer._is_partition_visual(
        {"type": "table", "accepted_visual": True},
        {"type": "table"},
    ) is True
    assert dashboard_renderer._is_partition_visual(
        {"type": "table", "source_bound": True},
        {"type": "table"},
    ) is True
    assert dashboard_renderer._is_partition_visual(
        {"type": "table"},
        {"type": "table"},
    ) is False

    widgets = {
        **{
            f"visual-{index:02d}": {"type": "bar"}
            for index in range(36)
        },
        **{
            f"status-{index:02d}": {
                "type": "status_table",
                "dashboard_fact": True,
            }
            for index in range(21)
        },
    }
    charts = {
        widget_id: {"type": widget["type"]}
        for widget_id, widget in widgets.items()
    }
    visual_ids = [
        widget_id
        for widget_id, widget in widgets.items()
        if dashboard_renderer._is_partition_visual(widget, charts[widget_id])
    ]
    assert len(visual_ids) == 36
    assert all(widget_id.startswith("visual-") for widget_id in visual_ids)


def test_dashboard_runtime_asset_is_emitted_hash_bound_and_offline() -> None:
    """The site emits exactly one immutable, local runtime asset."""

    asset_bytes = RUNTIME_PATH.read_bytes()
    assert dashboard_renderer._canonical_dashboard_js() == asset_bytes
    assert hashlib.sha256(asset_bytes).hexdigest() == dashboard_runtime.sha256(asset_bytes)

    pages, manifest = dashboard_renderer.render_dashboard_site(_simple_fixture())
    emitted = pages["assets/dashboard.js"]
    assert isinstance(emitted, bytes)
    assert emitted == asset_bytes
    assert manifest["assets"] == [
        "assets/dashboard.css",
        "assets/dashboard.js",
        "assets/favicon.svg",
    ]
    assert manifest["runtime"] == {
        "asset": "assets/dashboard.js",
        "deterministic": True,
        "network": False,
    }
    index = pages["index.html"]
    index_text = index.decode("utf-8") if isinstance(index, bytes) else index
    assert '<script src="assets/dashboard.js" defer></script>' in index_text
    assert 'data-dashboard-runtime' in index_text
    assert manifest["review_status"] == "Preview"
    assert "Preview outputs only" not in index_text
    assert '<span class="status">Manager view</span>' in index_text
    assert "Static local product · Business decision workspace." in index_text
    assert "Data quality &amp; model audit" not in index_text
    assert "Ontology projection" not in index_text
    assert "Evidence &amp; audit" not in index_text
    assert "Technical audit" not in index_text
    assert "work/evidence" not in index_text
    assert "source-bound" not in index_text.lower()

    domain_text = pages["domains/operations.html"]
    domain_text = domain_text.decode("utf-8") if isinstance(domain_text, bytes) else domain_text
    assert "Data quality &amp; model audit" not in domain_text
    assert "Ontology projection" not in domain_text
    assert "Evidence &amp; audit" not in domain_text
    assert "Technical audit" not in domain_text
    assert "work/evidence" not in domain_text
    assert "source-bound" not in domain_text.lower()
    audit_text = pages["data-quality-audit.html"]
    audit_text = audit_text.decode("utf-8") if isinstance(audit_text, bytes) else audit_text
    assert "Data quality &amp; model audit" in audit_text
    assert "Ontology projection" in audit_text
    assert "Evidence &amp; audit" in audit_text

    source = asset_bytes.decode("utf-8")
    lowered = source.lower()
    for forbidden in (
        "fetch(",
        "xmlhttprequest",
        "websocket",
        "eventsource",
        "eval(",
        "new function",
        "import(",
        "http://",
        "https://",
    ):
        assert forbidden not in lowered
    for hook in (
        "DOMContentLoaded",
        "[data-runtime-search]",
        "[data-runtime-domain]",
        "[data-runtime-clear]",
        "[data-series-toggle]",
        "[data-runtime-drilldown]",
    ):
        assert hook in source
    assert 'querySelectorAll(\'[data-series-key="\' + key' not in source
    assert 'querySelector(\'[data-runtime-detail="\' + targetId' not in source
    assert 'getAttribute(attribute) === expected' in source


def test_runtime_inventory_marks_unsupported_recipes_and_blueprint_is_deterministic() -> None:
    """Recipe choices are structural and the blueprint preserves source hashes."""

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    widget = {
        "id": "funnel-1",
        "type": "funnel",
        "rows": [{"label": "Entered", "value": 10, "size": "100%"}],
        "evidence_refs": ["work/evidence.json"],
    }
    chart = {
        "id": "funnel-1",
        "type": "funnel",
        "family": "funnel",
        "fields_or_values_used": {"rows": "reviewed rows"},
    }
    recipes = {
        recipe["id"]: recipe
        for recipe in dashboard_runtime.eligible_chart_recipes(widget, chart, registry)
    }
    assert recipes["funnel"]["eligible"] is True
    assert recipes["funnel"]["renderer_types"] == ["funnel"]
    assert recipes["bullet"]["eligible"] is False
    assert recipes["bullet"]["reason"] == "unsupported renderer family"
    assert recipes["sankey_flow"]["eligible"] is False

    assert "diverging_bar" in dashboard_runtime.SUPPORTED_FAMILIES
    diverging = dashboard_runtime.eligible_chart_recipes(
        {
            "id": "diverging-1",
            "type": "diverging_bar",
            "bars": [{"label": "Delta", "value": -2, "signed_size": "-20%"}],
        },
        {
            "id": "diverging-1",
            "type": "diverging_bar",
            "family": "diverging_bar",
            "fields_or_values_used": {"bars": "reviewed signed geometry"},
        },
        registry,
    )
    diverging_by_id = {recipe["id"]: recipe for recipe in diverging}
    assert diverging_by_id["diverging_bar"]["eligible"] is True

    assert "waffle" in dashboard_runtime.SUPPORTED_FAMILIES
    waffle = dashboard_runtime.eligible_chart_recipes(
        {
            "id": "waffle-1",
            "type": "waffle",
            "categories": [
                {"label": "Open", "value": 6, "size": "60%"},
                {"label": "Closed", "value": 4, "size": "40%"},
            ],
            "denominator": 10,
            "denominator_label": "cases",
        },
        {
            "id": "waffle-1",
            "type": "waffle",
            "family": "waffle",
            "fields_or_values_used": {
                "categories": "reviewed composition",
                "denominator": "reviewed denominator",
                "denominator_label": "reviewed denominator label",
            },
        },
        registry,
    )
    waffle_by_id = {recipe["id"]: recipe for recipe in waffle}
    assert waffle_by_id["waffle"]["eligible"] is True
    assert waffle_by_id["network_ontology_graph"]["eligible"] is False

    fixture = {
        "run_id": "RUN-RUNTIME-CONTRACT",
        "generation_id": "G-0001",
        "widgets": [widget],
        "domains": [
            {
                "id": "operations",
                "title": "Operations",
                "order": 1,
                "decision_flow": [{"id": "flow", "title": "Operations", "order": 1, "widget_ids": [widget["id"]]}],
            }
        ],
    }
    chart_map = {"charts": [chart]}
    fixture_hash = hashlib.sha256(dashboard_runtime.canonical_bytes(fixture)).hexdigest()
    chart_map_hash = hashlib.sha256(dashboard_runtime.canonical_bytes(chart_map)).hexdigest()
    registry_hash = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    kwargs = {
        "fixture_ref": "products/runtime/dashboard_fixture_v4.json",
        "fixture_sha256": fixture_hash,
        "chart_map_ref": "products/runtime/dashboard_chart_map_v4.json",
        "chart_map_sha256": chart_map_hash,
        "registry_ref": "assets/dashboard_chart_registry.json",
        "registry_sha256": registry_hash,
        "blueprint_ref": "products/runtime/dashboard_blueprint_v2.json",
    }
    first = dashboard_runtime.build_blueprint(fixture, chart_map, registry, **kwargs)
    second = dashboard_runtime.build_blueprint(fixture, chart_map, registry, **kwargs)
    assert dashboard_runtime.canonical_bytes(first) == dashboard_runtime.canonical_bytes(second)
    assert first["schema_version"] == "dashboard.business_presentation_plan.v2"
    assert first["review_status"] == "Preview"
    assert first["source_bindings"]["fixture_sha256"] == fixture_hash
    assert first["source_bindings"]["chart_map_sha256"] == chart_map_hash
    assert first["source_bindings"]["chart_registry_sha256"] == registry_hash


def test_blueprint_renderer_falls_through_blank_manager_display_projection() -> None:
    """A display-only manager projection must retain the eligible table renderer."""

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    widget = {
        "id": "table-display-only",
        "type": "table",
        "rows": [{"label": "North", "value": 7}],
        "manager_presentation": {
            "widget_id": "table-display-only",
            "display_projection": {"type": "table", "rows": [{"label": "North", "value": 7}]},
        },
    }
    chart = {
        "id": "table-display-only",
        "type": "table",
        "family": "table",
        "fields_or_values_used": {"rows": "reviewed rows"},
    }
    fixture = {
        "run_id": "RUN-RUNTIME-DISPLAY-PROJECTION",
        "generation_id": "G-0001",
        "widgets": [widget],
        "domains": [
            {
                "id": "operations",
                "title": "Operations",
                "order": 1,
                "decision_flow": [
                    {"id": "flow", "title": "Operations", "order": 1, "widget_ids": [widget["id"]]}
                ],
            }
        ],
    }

    blueprint = dashboard_runtime.build_blueprint(fixture, {"charts": [chart]}, registry)

    visual = blueprint["visuals"][0]
    assert visual["renderer_type"] == "table"


def test_recipe_eligibility_is_family_specific_and_conservative() -> None:
    """Different exact containers expose only truthful recipe choices."""

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def recipes(widget: dict[str, Any], family: str) -> dict[str, dict[str, Any]]:
        chart = {
            "id": widget["id"],
            "type": widget["type"],
            "family": family,
            "fields_or_values_used": {},
        }
        return {
            value["id"]: value
            for value in dashboard_runtime.eligible_chart_recipes(widget, chart, registry)
        }

    scatter = recipes(
        {
            "id": "shape-scatter",
            "type": "scatter",
            "points": [{"label": "A", "x": 1, "y": 2}, {"label": "B", "x": 2, "y": 3}],
        },
        "scatter_bubble",
    )
    assert scatter["scatter_bubble"]["eligible"] is True
    for family in ("funnel", "donut_pie", "histogram_box", "pareto"):
        assert scatter[family]["eligible"] is False

    funnel = recipes(
        {
            "id": "shape-funnel",
            "type": "funnel",
            "stages": [{"label": "Entered", "value": 10, "size": 1.0}, {"label": "Won", "value": 4, "size": 0.4}],
        },
        "funnel",
    )
    assert funnel["funnel"]["eligible"] is True
    assert funnel["scatter_bubble"]["eligible"] is False

    composition = recipes(
        {
            "id": "shape-composition",
            "type": "donut",
            "categories": [
                {"label": "Open", "value": 6, "size": "60%"},
                {"label": "Closed", "value": 4, "size": "40%"},
            ],
            "denominator": 10,
            "denominator_label": "cases",
        },
        "donut_pie",
    )
    assert composition["donut_pie"]["eligible"] is True
    assert composition["waffle"]["eligible"] is True
    assert composition["histogram_box"]["eligible"] is False

    box = recipes(
        {
            "id": "shape-box",
            "type": "box_plot",
            "rows": [{"label": "Latency", "min": 1, "q1": 2, "median": 3, "q3": 4, "max": 5}],
        },
        "histogram_box",
    )
    assert box["histogram_box"]["eligible"] is True
    assert box["histogram_box"]["renderer_types"] == ["box_plot"]
    assert box["donut_pie"]["eligible"] is False


def test_umbrella_recipe_explicit_renderer_type_reaches_markup() -> None:
    """The Product Agent's exact renderer choice survives manager rendering."""

    points = [{"label": "Jan", "value": 4}, {"label": "Feb", "value": 6}]
    widget = {
        "id": "selectable-trend",
        "type": "line",
        "title": "Reviewed trend",
        "points": points,
        "manager_presentation": {
            "widget_id": "selectable-trend",
            "visual_type": "line",
            "chart_family": "line_area_slope",
            "recipe_id": "line_area_slope",
            "renderer_type": "area",
            "layout": "wide",
            "title_projection": {"pointer": "/widget_snapshot/title", "value": "Reviewed trend"},
            "visual_projection": {
                "type": {"pointer": "/chart_entry/type", "value": "line"},
                "family": {"pointer": "/chart_entry/family", "value": "line_area_slope"},
                "points": {"pointer": "/chart_entry/fields_or_values_used/points", "value": points},
            },
        },
        "presentation_role": "decision_view",
        "requirement_id": "REQ-TREND",
        "manager_admission": {"status": "admitted", "presentation_audience": "business_manager"},
        "presentation_audience": "business_manager",
    }
    display = dashboard_renderer._manager_surface_widget(widget)
    assert display["type"] == "area"
    assert display["renderer_type"] == "area"
    html = dashboard_renderer._site_widget(widget, [])
    assert "layout-wide" in html
    assert "viz-area" in html
    assert "Jan" in html and "Feb" in html and "4" in html and "6" in html


def test_selected_recipe_and_layout_reach_manager_markup_without_recomputing_values() -> None:
    """A validated alternate recipe is renderer-visible but source values stay exact."""

    source_rows = [{"label": "North", "value": 7, "size": "70%"}]
    widget = {
        "id": "selectable-bar",
        "type": "bar",
        "title": "Reviewed comparison",
        "manager_presentation": {
            "widget_id": "selectable-bar",
            "visual_type": "bar",
            "chart_family": "horizontal_bar",
            "recipe_id": "column",
            "layout": "half",
            "title_projection": {"pointer": "/widget_snapshot/title", "value": "Reviewed comparison"},
            "visual_projection": {
                "type": {"pointer": "/chart_entry/type", "value": "bar"},
                "family": {"pointer": "/chart_entry/family", "value": "horizontal_bar"},
                "bars": {"pointer": "/chart_entry/fields_or_values_used/bars", "value": source_rows},
            },
        },
        "presentation_role": "decision_view",
        "requirement_id": "REQ-SELECT",
        "manager_admission": {"status": "admitted", "presentation_audience": "business_manager"},
        "presentation_audience": "business_manager",
    }
    display = dashboard_renderer._manager_surface_widget(widget)
    assert display["type"] == "column"
    assert display["recipe_id"] == "column"
    assert display["bars"] == source_rows
    html = dashboard_renderer._site_widget(widget, [])
    assert "layout-half" in html
    assert "North" in html and "7" in html and "70%" in html


def test_supported_chart_families_render_and_bad_visual_falls_back_locally() -> None:
    """Several executable families render supplied values without blanking."""

    variants: dict[str, dict[str, Any]] = {
        "area": {"type": "area", "rows": [{"label": "Jan", "value": 4}, {"label": "Feb", "value": 6}]},
        "grouped_bar": {"type": "grouped_bar", "rows": [{"label": "North", "value": 4, "size": "40%"}]},
        "funnel": {"type": "funnel", "rows": [{"label": "Entered", "value": 10, "size": "100%"}, {"label": "Won", "value": 4, "size": "40%"}]},
        "histogram": {"type": "histogram", "rows": [{"label": "0-10", "count": 3, "size": "30%"}]},
        "box_plot": {"type": "box_plot", "rows": [{"label": "Latency", "min": 1, "q1": 2, "median": 3, "q3": 4, "max": 5}]},
        "pareto": {"type": "pareto", "rows": [{"label": "Exception", "value": 5, "size": "50%", "cumulative_percent": "50%"}]},
        "waterfall": {"type": "waterfall", "rows": [{"label": "Opening", "start_value": 10, "change_value": 0, "end_value": 10}]},
        "heatmap": {"type": "heatmap", "cells": [{"label": "North / Open", "value": 7, "level": "high"}]},
        "scatter": {"type": "scatter", "points": [{"label": "A", "x": 1, "y": 2}]},
        "donut": {"type": "donut", "denominator": 10, "denominator_label": "cases", "rows": [{"label": "Open", "value": 6, "size": "60%"}, {"label": "Closed", "value": 4, "size": "40%"}]},
        "table": {"type": "table", "rows": [{"label": "Reviewed row", "value": 7}]},
    }
    expected_markers = {
        "area": "Jan",
        "grouped_bar": "North",
        "funnel": "Entered",
        "histogram": "0-10",
        "box_plot": "Latency",
        "pareto": "Exception",
        "waterfall": "Opening",
        "heatmap": "North / Open",
        "scatter": "A",
        "donut": "Open",
        "table": "Reviewed row",
    }
    for family, widget in variants.items():
        rendered = dashboard_renderer._render_visual(widget)
        assert rendered.strip(), family
        assert expected_markers[family] in rendered, family

    with pytest.raises(ValueError, match="stacked_area requires"):
        dashboard_renderer._render_visual({
            "type": "stacked_area",
            "segments": [{"label": "A", "value": 4, "size": "40%"}],
        })

    rows = [{"label": "Kept exact", "value": 7}]
    fallback = dashboard_renderer._render_visual_fallback({"type": "unsupported_exotic", "rows": rows})
    assert "Visual unavailable; exact reviewed rows remain available below." in fallback
    assert "<table>" in fallback
    assert "Kept exact" in fallback and "7" in fallback
    empty = dashboard_renderer._render_visual_fallback({"type": "unsupported_exotic"})
    assert "Visual unavailable: no reviewed rows were supplied." in empty
    assert "<table>" not in empty


def test_every_executable_recipe_has_a_positive_exact_renderer_shape() -> None:
    """Each advertised registry recipe renders its supplied shape directly."""

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    cases: list[tuple[str, dict[str, Any], str, str]] = [
        ("kpi_card", {"type": "kpi", "value": 7}, "kpi", "7"),
        ("horizontal_bar", {"type": "bar", "rows": [{"label": "North", "value": 7, "size": "70%"}]}, "bar", "North"),
        ("column", {"type": "column", "rows": [{"label": "North", "value": 7, "height": "70%"}]}, "column", "North"),
        ("grouped_bar", {"type": "grouped_bar", "bars": [{"label": "North", "series": [{"label": "Open", "value": 7, "size": "70%"}]}], "unit": "cases"}, "grouped_bar", "North"),
        ("stacked_bar", {"type": "stacked_bar", "segments": [{"label": "Open", "value": 6, "size": "60%"}, {"label": "Closed", "value": 4, "size": "40%"}], "unit": "cases", "denominator": 10}, "stacked_bar", "Open"),
        ("lollipop", {"type": "lollipop", "bars": [{"label": "North", "value": 7, "size": "70%"}]}, "lollipop", "North"),
        ("diverging_bar", {"type": "diverging_bar", "bars": [{"label": "Delta", "value": -2, "signed_size": "-20%"}]}, "diverging_bar", "Delta"),
        ("waterfall", {"type": "waterfall", "steps": [{"label": "Opening", "start": 10, "change": 2, "end": 12}]}, "waterfall", "Opening"),
        ("donut_pie", {"type": "donut", "categories": [{"label": "Open", "value": 6, "size": "60%"}, {"label": "Closed", "value": 4, "size": "40%"}], "denominator": 10, "denominator_label": "cases"}, "donut", "Open"),
        ("waffle", {"type": "waffle", "categories": [{"label": "Open", "value": 6, "size": "60%"}, {"label": "Closed", "value": 4, "size": "40%"}], "denominator": 10, "denominator_label": "cases"}, "waffle", "Open"),
        ("funnel", {"type": "funnel", "stages": [{"label": "Entered", "value": 10, "size": "100%"}, {"label": "Won", "value": 4, "size": "40%"}]}, "funnel", "Entered"),
        ("line_area_slope", {"type": "line", "points": [{"label": "Jan", "value": 4}, {"label": "Feb", "value": 6}], "time": "month"}, "line", "Jan"),
        ("scatter_bubble", {"type": "scatter", "points": [{"label": "A", "x": 1, "y": 2}, {"label": "B", "x": 2, "y": 3}]}, "scatter", "A"),
        ("histogram_box", {"type": "histogram", "bins": [{"label": "0-10", "count": 3, "size": "30%"}]}, "histogram", "0-10"),
        ("histogram_box", {"type": "box_plot", "boxes": [{"label": "Latency", "min": 1, "q1": 2, "median": 3, "q3": 4, "max": 5}]}, "box_plot", "Latency"),
        ("pareto", {"type": "pareto", "rows": [{"label": "Exception", "value": 5, "size": "50%", "cumulative_percent": "50%"}]}, "pareto", "Exception"),
        ("heatmap_matrix", {"type": "heatmap", "cells": [{"row": "North", "column": "Open", "value": 7, "level": "high"}]}, "heatmap", "7"),
        ("metric_grid", {"type": "metric_grid", "tiles": [{"label": "Reviewed", "value": 7}]}, "metric_grid", "Reviewed"),
        ("table", {"type": "table", "rows": [{"label": "Reviewed", "value": 7}]}, "table", "Reviewed"),
    ]
    for index, (recipe_id, widget, renderer_type, marker) in enumerate(cases):
        chart = {
            "id": f"positive-{index}",
            "type": widget["type"],
            "family": recipe_id,
            "fields_or_values_used": {},
        }
        widget = {**widget, "id": chart["id"]}
        recipe = next(value for value in dashboard_runtime.eligible_chart_recipes(widget, chart, registry) if value["id"] == recipe_id)
        assert recipe["eligible"] is True, (recipe_id, recipe)
        assert renderer_type in recipe["renderer_types"], (recipe_id, recipe)
        rendered = dashboard_renderer._render_visual(widget)
        assert "visual-fallback" not in rendered
        assert marker in rendered, recipe_id


def test_renderer_shape_aliases_do_not_create_false_positive_recipe_choices() -> None:
    """Aliases that the renderer cannot consume remain conservatively unavailable."""

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def by_id(widget: dict[str, Any], family: str) -> dict[str, dict[str, Any]]:
        chart = {"id": widget["id"], "type": widget["type"], "family": family, "fields_or_values_used": {}}
        return {value["id"]: value for value in dashboard_runtime.eligible_chart_recipes(widget, chart, registry)}

    metric_grid = by_id({"id": "bad-grid", "type": "metric_grid", "tiles": [{"label": "Only display", "display_value": "7"}]}, "metric_grid")
    assert metric_grid["metric_grid"]["eligible"] is False

    donut = by_id(
        {"id": "bad-donut", "type": "donut", "categories": [{"label": "Open", "value": 6, "size": 0.6}, {"label": "Closed", "value": 4, "size": 0.4}], "denominator": 10, "denominator_label": "cases"},
        "donut_pie",
    )
    assert donut["donut_pie"]["eligible"] is False

    grouped = by_id(
        {"id": "bad-grouped", "type": "grouped_bar", "bars": [{"label": "North", "series": [{"label": "Open", "count": 7, "share": "70%"}]}], "unit": "cases"},
        "grouped_bar",
    )
    assert grouped["grouped_bar"]["eligible"] is False

    histogram = by_id({"id": "bad-hist", "type": "histogram", "bins": [{"range": "0-10", "frequency": 3, "height": "30%"}]}, "histogram_box")
    assert histogram["histogram_box"]["eligible"] is False
