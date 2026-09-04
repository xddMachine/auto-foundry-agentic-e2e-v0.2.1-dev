"""Offline integration checks for the bounded product helpers."""

from __future__ import annotations

import hashlib
from html import unescape
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPTS = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts"
BENCHMARK = ROOT / "benchmarks" / "benchmark_a"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dashboard_renderer = _load("dashboard_renderer_integration", SCRIPTS / "dashboard_renderer.py")
evidence_collector = _load("optimizer_evidence_collector_integration", SCRIPTS / "optimizer_evidence_collector.py")
release_validator = _load("validate_release_integration", ROOT / "scripts" / "validate_release.py")

from auto_foundry_core.product_contracts import FORBIDDEN_FREEZE_SIBLINGS  # noqa: E402
from auto_foundry_core.workspace import AllowedRootError, RunContext  # noqa: E402


def _fixture() -> dict[str, object]:
    widgets: list[dict[str, object]] = [
        {"id": "kpi-1", "type": "kpi", "title": "Supplied KPI", "value": "4,032/4,093", "unit": "orders", "trace_refs": ["Q-001/final"]},
        {"id": "bar-1", "type": "bar", "title": "Bars", "bars": [{"label": "A", "value": "3", "share": "50%"}], "trace_refs": ["Q-002/final"]},
        {"id": "line-1", "type": "line", "title": "Line", "points": [{"label": "Jan", "value": "7"}], "trace_refs": ["Q-003/final"]},
        {"id": "stack-1", "type": "stacked_composition", "title": "Composition", "segments": [{"label": "A", "value": "2", "share": "25%"}], "trace_refs": ["Q-004/final"]},
        {"id": "heat-1", "type": "heatmap", "title": "Heat", "cells": [{"label": "A", "value": "high", "intensity": "high"}], "trace_refs": ["Q-005/final"]},
        {"id": "scatter-1", "type": "scatter", "title": "Scatter", "points": [{"label": "A", "x": "1", "y": "2"}], "trace_refs": ["Q-006/final"]},
        {"id": "donut-1", "type": "donut", "title": "Categories", "denominator_value": 2, "denominator_label": "rows", "categories": [{"label": "A", "value": "1", "size": "50%"}, {"label": "B", "value": "1", "size": "50%"}], "trace_refs": ["Q-007/final"]},
        {"id": "table-1", "type": "table", "title": "Drill down", "columns": ["id", "status"], "rows": [{"id": "A", "status": "partial"}], "trace_refs": ["Q-008/final"]},
    ]
    for index, widget in enumerate(widgets, 1):
        widget["reviewed_item_ref"] = f"Q-{index:03d}"
        widget["reviewed_output_ref"] = f"Q-{index:03d}/final_answer.md"
        widget["evidence_refs"] = [f"Q-{index:03d}/evidence"]
    return {
        "title": "Reviewed fixture",
        "run_id": "RUN-TEST",
        "limitations": ["Proxy only; no new metric calculation."],
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "domains": [
            {"id": "second", "title": "Second domain first", "order": 1, "decision_flow": [{"id": "flow", "title": "Decision flow supplied", "order": 1, "widget_ids": ["line-1"]}]},
            {"id": "first", "title": "First domain second", "order": 2, "decision_flow": [{"id": "flow", "title": "Second decision flow", "order": 1, "widget_ids": ["kpi-1", "bar-1", "stack-1", "heat-1", "scatter-1", "donut-1", "table-1"]}]},
        ],
        "widgets": widgets,
    }


def _without_details(document: str) -> str:
    """Remove both data and context disclosures for default-visibility checks."""

    return re.sub(r"<details\b[^>]*>.*?</details>", "", document, flags=re.DOTALL)


def _widget_article(document: str, widget_id: str) -> str:
    marker = f'id="widget-{dashboard_renderer._slug(widget_id)}"'
    start = document.index(marker)
    end = document.index("</article>", start)
    return document[start:end]


def test_dashboard_renders_in_run_and_preserves_reviewed_provenance(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    context = RunContext("RUN-TEST", run_root)
    fixture_path = run_root / "reviewed_widgets.json"
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text(json.dumps(_fixture()), encoding="utf-8")
    manifest = dashboard_renderer.render_fixture(context, "reviewed_widgets.json", "dashboard.html", "dashboard_manifest.json")
    document = (run_root / "products" / "dashboard.html").read_text(encoding="utf-8")
    assert manifest["internal_links_checked"] is True
    assert manifest["new_analytics"] is False
    assert manifest["freeze_markers"] == {
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_data_registry_frozen": True,
        "dashboard_frozen": True,
        "telemetry_frozen": True,
    }
    assert manifest["organization"] == "business_domain_and_decision_flow"
    assert manifest["domain_order"] == ["second", "first"]
    assert manifest["decision_flow_order"] == [{"domain_id": "second", "flow_id": "flow"}, {"domain_id": "first", "flow_id": "flow"}]
    for kind in ("kpi", "bar", "line", "stacked_composition", "heatmap", "scatter", "donut", "table"):
        assert f"widget-{kind}" in document
    assert "4,032/4,093" in document
    assert "Q-001/final" in document
    assert "https://" not in document
    assert document.index("Second domain first") < document.index("First domain second")
    manifest_item = next(item for item in manifest["items"] if item["element_id"] == "widget-kpi-1")
    assert manifest_item["trace_refs"] == ["Q-001/final"]
    assert manifest_item["evidence_refs"] == ["Q-001/evidence"]
    assert manifest_item["trace_anchors"] == ["trace-Q-001-final"]

    evidence_only = json.loads(json.dumps(_fixture()))
    evidence_only["widgets"][0].pop("trace_refs")
    _, evidence_manifest = dashboard_renderer.render_dashboard(evidence_only)
    evidence_item = next(item for item in evidence_manifest["items"] if item["element_id"] == "widget-kpi-1")
    assert evidence_item["trace_refs"] == []
    assert evidence_item["trace_anchors"] == ["trace-Q-001-evidence"]


def test_dashboard_v2_renders_focused_pages_and_keeps_business_tables_visible(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    context = RunContext("RUN-TEST", run_root)
    fixture = _fixture()
    fixture["ontology_summary"] = {
        "ontology_items": 12,
        "relationships": 7,
        "canonical_mappings": 30,
        "identity_decisions": 30,
        "prepared_assets": 1,
    }
    fixture["ontology_nodes"] = [
        {"id": "CustomerOrder", "label": "Customer order", "kind": "business object"},
        {"id": "Delivery", "label": "Delivery", "kind": "business object"},
    ]
    fixture_path = run_root / "reviewed_widgets.json"
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    manifest = dashboard_renderer.render_site_fixture(
        context,
        "reviewed_widgets.json",
        "dashboard",
        "dashboard/manifest.json",
    )

    dashboard = run_root / "products" / "dashboard"
    assert manifest["site_version"] == 2
    assert manifest["chart_led"] is True
    assert manifest["tables_collapsed"] is True
    assert manifest["pages"] == [
        "index.html",
        "domains/second.html",
        "domains/first.html",
        "data-quality-audit.html",
        "ontology.html",
        "evidence.html",
    ]
    assert (dashboard / "assets" / "dashboard.css").is_file()
    overview = (dashboard / "index.html").read_text(encoding="utf-8")
    assert "Second domain first" in overview
    assert "Reviewed visual details" not in overview
    assert "Drill down" not in overview
    first_domain = (dashboard / "domains" / "first.html").read_text(encoding="utf-8")
    assert 'class="viz viz-bar-list"' in first_domain
    assert '<table>' in first_domain
    assert '<details class="data-detail">' not in first_domain
    # Manager domain pages keep the exact trace link on the separate evidence
    # surface; technical anchors are not part of the visible decision canvas.
    assert "../evidence.html#trace-Q-008-final" not in first_domain
    ontology = (dashboard / "ontology.html").read_text(encoding="utf-8")
    assert "Canonical mappings" in ontology
    assert "Customer order" in ontology
    evidence = (dashboard / "evidence.html").read_text(encoding="utf-8")
    assert 'id="trace-Q-001-final"' in evidence
    assert "Proxy only; no new metric calculation." in evidence

    with pytest.raises(AllowedRootError):
        dashboard_renderer.render_site_fixture(
            context,
            "reviewed_widgets.json",
            "../outside",
            "dashboard/site-manifest.json",
        )


def test_site_uses_local_favicon_and_keeps_small_tables_visible() -> None:
    fixture = _fixture()
    pages, manifest = dashboard_renderer.render_dashboard_site(fixture)
    css = pages["assets/dashboard.css"].decode("utf-8")
    index = pages["index.html"]
    first_domain = pages["domains/first.html"]
    index_text = index.decode("utf-8") if isinstance(index, bytes) else index
    domain_text = first_domain.decode("utf-8") if isinstance(first_domain, bytes) else first_domain

    assert pages["assets/favicon.svg"] == dashboard_renderer._OFFLINE_FAVICON_SVG
    assert manifest["favicon_ref"] == "assets/favicon.svg"
    assert "assets/favicon.svg" in manifest["assets"]
    assert 'rel="icon" href="assets/favicon.svg"' in index_text
    assert 'rel="icon" href="../assets/favicon.svg"' in domain_text
    assert '<table>' in domain_text
    assert '<details class="data-detail">' not in domain_text
    assert "details.data-detail:not([open]) > .table-wrap" in css
    assert "display: none" in css
    assert "overflow-x: auto" in css and "max-width: 100%" in css
    assert "body {" in css and "overflow-x: hidden" in css


def test_site_keeps_default_visuals_nonempty_and_compacts_long_scalar_values() -> None:
    fixture = _fixture()
    fixture["widgets"][0]["value"] = "0.571428571428"
    fixture["widgets"][1]["bars"][0]["value"] = "0.0000004"
    extra_widgets = [
        {
            "id": "empty-business-table",
            "type": "table",
            "title": "Empty reviewed queue",
            "columns": ["status"],
            "rows": [],
            "trace_refs": ["Q-009/final"],
            "reviewed_item_ref": "Q-009",
            "reviewed_output_ref": "Q-009/final_answer.md",
            "evidence_refs": ["Q-009/evidence"],
        },
        {
            "id": "empty-status-table",
            "type": "status_table",
            "title": "Empty reviewed status",
            "columns": ["status"],
            "rows": [],
            "trace_refs": ["Q-010/final"],
            "reviewed_item_ref": "Q-010",
            "reviewed_output_ref": "Q-010/final_answer.md",
            "evidence_refs": ["Q-010/evidence"],
        },
    ]
    fixture["widgets"].extend(extra_widgets)
    fixture["domains"][0]["decision_flow"][0]["widget_ids"].extend(widget["id"] for widget in extra_widgets)
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    domain_pages = [
        value.decode("utf-8") if isinstance(value, bytes) else value
        for name, value in pages.items()
        if name.startswith("domains/") and name.endswith(".html")
    ]
    all_domains = "\n".join(domain_pages)
    scalar_article = _widget_article(all_domains, "kpi-1")
    scalar_visible = _without_details(scalar_article)
    assert "0.571429" in scalar_visible
    # The manager card is compact; exact technical scalar payloads stay off
    # the business canvas (and are retained only when a separate audit payload
    # is supplied).
    assert "0.571428571428" not in all_domains
    bar_article = _widget_article(all_domains, "bar-1")
    assert "0.0000004" in bar_article
    assert 'aria-label="A: 0.0000004"' in bar_article
    for widget in fixture["widgets"]:
        article = _widget_article(all_domains, str(widget["id"]))
        visible = _without_details(article)
        visual = re.sub(r"<h3\b[^>]*>.*?</h3>", "", visible, count=1, flags=re.DOTALL)
        visible_text = unescape(re.sub(r"<[^>]+>", "", visual)).strip()
        assert visible_text, widget["id"]
        if widget["type"] in {"table", "status_table"}:
            assert "No reviewed rows were supplied" in visible or "<table>" in visible, widget["id"]


def test_large_audit_tables_keep_a_visible_preview_and_collapse_only_full_detail() -> None:
    fixture = _fixture()
    audit = fixture["widgets"][-1]
    audit["title"] = "Reviewed audit queue"
    audit["rows"] = [{"id": f"row-{index}", "status": "reviewed"} for index in range(20)]
    # This fixture has no V2 plan; mark the deliberately omitted widget with
    # the persisted audit admission used by a planless legacy fixture.
    audit["manager_admission"] = {
        "status": "audit_only",
        "presentation_audience": "technical_audit",
    }
    audit["presentation_audience"] = "technical_audit"
    audit["presentation_tier"] = "audit"
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    domain = pages["domains/first.html"]
    domain_text = domain.decode("utf-8") if isinstance(domain, bytes) else domain
    # An explicitly audit-named table remains on the separate technical page;
    # the manager domain never receives a full/raw audit projection.
    assert 'id="widget-table-1"' not in domain_text
    audit_page = pages["data-quality-audit.html"]
    audit_text = audit_page.decode("utf-8") if isinstance(audit_page, bytes) else audit_page
    assert "Reviewed audit queue" in audit_text and "row-19" in audit_text


def test_progress_rows_have_mobile_safe_containment_for_long_relationship_labels() -> None:
    fixture = _fixture()
    fixture["widgets"][1] = {
        **fixture["widgets"][1],
        "type": "progress",
        "title": "Relationship coverage",
        "manager_admission": {
            "status": "audit_only",
            "presentation_audience": "technical_audit",
        },
        "presentation_audience": "technical_audit",
        "presentation_tier": "audit",
        "bars": [{
            "label": "Source endpoint coverage for the exceptionally long relationship label that must wrap safely",
            "value": "104/109",
            "size": "95.41%",
        }],
    }
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    domain = pages["domains/first.html"]
    domain_text = domain.decode("utf-8") if isinstance(domain, bytes) else domain
    css = pages["assets/dashboard.css"].decode("utf-8")

    assert '<table>' in domain_text
    # The omitted widget remains exact and clickable in the technical
    # disclosure; the renderer does not infer this from its title.
    assert "104/109" not in _without_details(domain_text)
    audit_text = pages["data-quality-audit.html"]
    audit_text = audit_text.decode("utf-8") if isinstance(audit_text, bytes) else audit_text
    assert "104/109" in audit_text
    assert "Relationship 1" not in _without_details(domain_text)
    assert ".viz-progress { min-width: 0; max-width: 100%;" in css
    assert "grid-template-columns: minmax(0, 1fr) auto" in css
    assert ".progress-row { min-width: 0; max-width: 100%;" in css
    assert ".progress-track { display: block; width: 100%; min-width: 0; max-width: 100%;" in css


def test_dashboard_v3_metadata_groups_requirements_and_keeps_sparse_line_honest(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    context = RunContext("RUN-TEST", run_root)
    fixture = _fixture()
    fixture["dashboard_version"] = 3
    fixture["site_version"] = 3
    fixture["chart_map_ref"] = "products/decision_dashboard_chart_map_v3.json"
    fixture["widgets"][0].update({
        "overview": True,
        "requirement_id": "REQ-01",
        "requirement_title": "Fulfillment timing",
        "requirement_subtitle": "Accepted timing view",
        "takeaway": "Late rows remain bounded to reviewed populations.",
    })
    fixture["widgets"][2].update({"requirement_id": "REQ-01", "requirement_title": "Fulfillment timing"})
    fixture_path = run_root / "reviewed_widgets_v3.json"
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    manifest = dashboard_renderer.render_site_fixture(context, "reviewed_widgets_v3.json", "dashboard_v3", "dashboard_v3/manifest.json")
    dashboard = run_root / "products" / "dashboard_v3"
    overview = (dashboard / "index.html").read_text(encoding="utf-8")
    domain = (dashboard / "domains" / "first.html").read_text(encoding="utf-8")
    second_domain = (dashboard / "domains" / "second.html").read_text(encoding="utf-8")
    assert manifest["site_version"] == 3
    assert manifest["overview_widget_ids"] == ["kpi-1"]
    assert manifest["chart_map_ref"] == "products/decision_dashboard_chart_map_v3.json"
    assert "Priority signals" in overview
    # Internal requirement IDs remain runtime/audit metadata, not visible
    # manager copy.  Strip markup before checking the business canvas text.
    domain_visible_text = unescape(re.sub(r"<[^>]+>", " ", _without_details(domain)))
    assert "REQ-01" not in domain_visible_text
    assert "Not enough reviewed points to infer a trend." in second_domain
    canonical_css = Path(dashboard_renderer.__file__).resolve().parent.parent / "assets" / "dashboard.css"
    assert (dashboard / "assets" / "dashboard.css").read_bytes() == canonical_css.read_bytes()
    assert ".evidence-note code { overflow-wrap: anywhere; word-break: break-word; }" in canonical_css.read_text(encoding="utf-8")


def test_dashboard_v3_req02_fixture_keeps_refund_matches_nonmonetary_and_currency_partitioned() -> None:
    dashboard_root = Path(__file__).resolve().parents[3]
    run_root = dashboard_root / "benchmark_a_requirement_v070_entity_run_a_rerun_1"
    fixture_path = run_root / "reviewed_dashboard_fixture_v3.json"
    chart_map_path = run_root / "products" / "decision_dashboard_chart_map_v3.json"
    fixture_text = fixture_path.read_text(encoding="utf-8")
    chart_map_text = chart_map_path.read_text(encoding="utf-8")
    unsupported_totals = ("76,674.09", "66,206.54", "83,105.69", "218,834.10")
    for total in unsupported_totals:
        assert total not in fixture_text
        assert total not in chart_map_text
    fixture = json.loads(fixture_text)
    promo = next(widget for widget in fixture["widgets"] if widget["id"] == "req02-promo-discount-kpi")
    assert promo["title"] == "Exact refund-reference matches"
    assert promo["value"] == "300 / 308"
    assert promo["unit"] == "rows and matching coverage"
    assert "monetary aggregation" in promo["limitations"][0]
    bridge = next(widget for widget in fixture["widgets"] if widget["id"] == "req02-currency-bridge")
    bridge_values = [row["Value"] for row in bridge["rows"]]
    assert bridge_values == [
        "111,395.77",
        "91,958.92",
        "45,089.73",
        "25,833.94",
        "23,938.67",
        "5,882.88",
        "4,731.01",
        "59.55",
        "380.41",
        "141.12",
        "300 / 308 rows",
    ]
    chart_map = json.loads(chart_map_text)
    fixture_types = {widget["id"]: widget["type"] for widget in fixture["widgets"]}
    map_types = {chart["id"]: chart["type"] for chart in chart_map["charts"]}
    assert len(fixture_types) == len(map_types) == 57
    assert fixture_types == map_types
    expected_donuts = {
        "req03-complaint-status": (408, "complaint rows"),
        "req03-finance-status": (1113, "finance refund rows"),
        "req04-po-status": (523, "accepted POs"),
        "req04-receipt-reasons": (1504, "receipt rows"),
        "req05-invoice-status": (500, "AP invoices"),
        "req05-paid-timing": (455, "paid invoices"),
        "req06-physical-difference": (880, "matched count pairs"),
        "req06-movement-types": (12335, "movement rows"),
    }
    for widget_id, (denominator, label) in expected_donuts.items():
        widget = next(widget for widget in fixture["widgets"] if widget["id"] == widget_id)
        chart = next(chart for chart in chart_map["charts"] if chart["id"] == widget_id)
        assert widget["type"] == chart["type"] == "donut"
        assert (widget["denominator_value"], widget["denominator_label"]) == (denominator, label)
        assert chart["fields_or_values_used"]["denominator_value"] == denominator
        assert chart["fields_or_values_used"]["denominator_label"] == label
        assert len(widget["categories"]) in range(2, 6)
        assert abs(sum(float(category["size"].rstrip("%")) for category in widget["categories"]) - 100) <= 0.5
    movement = next(widget for widget in fixture["widgets"] if widget["id"] == "req06-movement-types")
    assert sum(int(category["value"].replace(",", "").split()[0]) for category in movement["categories"]) == 12335
    assert "presentation geometry derived from reviewed row counts" in movement["chart_notes"][0]
    movement_map = next(chart for chart in chart_map["charts"] if chart["id"] == "req06-movement-types")
    assert "presentation geometry derived from reviewed row counts" in movement_map["fields_or_values_used"]["presentation_note"]
    assert {widget_id for widget_id, kind in fixture_types.items() if kind == "progress"} == {
        "req01-mapping-coverage",
        "req05-relationship-coverage",
        "req07-fill-proxy",
    }
    assert {widget_id for widget_id, kind in fixture_types.items() if kind == "leaderboard"} == {
        "req01-carrier-late",
        "req04-supplier-late-queue",
    }
    assert {widget_id for widget_id, kind in fixture_types.items() if kind == "metric_grid"} == {
        "req05-overdue-balances",
        "req08-outstanding-by-currency",
    }
    assert sum(kind == "bar" for kind in fixture_types.values()) == 16
    for widget_id in ("req05-overdue-balances", "req08-outstanding-by-currency"):
        widget = next(widget for widget in fixture["widgets"] if widget["id"] == widget_id)
        chart = next(chart for chart in chart_map["charts"] if chart["id"] == widget_id)
        assert widget["type"] == chart["type"] == "metric_grid"
        assert chart["family"] == "currency_partition_tiles"
        assert widget["tiles"] == chart["fields_or_values_used"]["tiles"]
        assert all(not any(key in tile for key in ("size", "width", "share", "percent")) for tile in widget["tiles"])
        assert "not comparable" in widget["chart_notes"][0]
        assert "no cross-currency total" in chart["fields_or_values_used"]["presentation_note"]
    fill = next(widget for widget in fixture["widgets"] if widget["id"] == "req07-fill-proxy")
    assert [row["size"] for row in fill["bars"]] == ["69.0%", "68.71%"]
    donut = next(widget for widget in fixture["widgets"] if widget["id"] == "req03-complaint-status")
    assert 'class="donut-ring"' in dashboard_renderer._render_donut(donut)
    assert 'class="donut-legend"' in dashboard_renderer._render_donut(donut)
    assert "complaint rows" in dashboard_renderer._render_donut(donut)
    stacked = next(widget for widget in fixture["widgets"] if widget["id"] == "req08-invoice-statuses")
    stacked_html = dashboard_renderer._render_stacked(stacked)
    strip_html = stacked_html.split('<svg class="stacked-strip"', 1)[1].split("</svg>", 1)[0]
    assert strip_html.count("class=\"stack-segment stack-segment-") == 8
    assert all(f'>{row["label"]}<' not in strip_html for row in stacked["segments"])
    rects = re.findall(r"<rect[^>]+>", strip_html)
    starts = [float(re.search(r'data-start="([0-9.]+)"', rect).group(1)) for rect in rects]
    widths = [float(re.search(r'width="([0-9.]+)"', rect).group(1)) for rect in rects]
    assert starts == pytest.approx([0, 56.69, 57.66, 67.95, 99.66, 99.78, 99.90, 99.97])
    assert starts[-1] + widths[-1] == pytest.approx(99.99, abs=1e-6)
    assert all(width < 1 for width in widths[-4:])
    assert 'viewBox="0 0 100 10"' in stacked_html and 'preserveAspectRatio="none"' in stacked_html
    assert 'data-start="0" data-size="56.69%"' in stacked_html
    assert 'data-start="99.97" data-size="0.02%"' in stacked_html
    assert 'aria-label="Invoice ledger status labels: CLEARED: 2,335 (56.69%)' in stacked_html
    assert '<title id="stacked-title-req08-invoice-statuses">Invoice ledger status labels</title>' in stacked_html
    assert 'class="stack-legend"' in stacked_html
    for row in stacked["segments"]:
        assert row["label"] in stacked_html and row["value"] in stacked_html and row["size"] in stacked_html
    with pytest.raises(ValueError, match="denominator_value"):
        dashboard_renderer._render_donut({"categories": donut["categories"], "denominator_label": "rows"})
    with pytest.raises(ValueError, match="approximately 100"):
        dashboard_renderer._render_donut({"denominator_value": 10, "denominator_label": "rows", "categories": [{"label": "A", "value": "4", "size": "40%"}, {"label": "B", "value": "4", "size": "40%"}]})
    with pytest.raises(ValueError, match="2 to 5"):
        dashboard_renderer._render_donut({"denominator_value": 10, "denominator_label": "rows", "categories": [{"label": "A", "value": "10", "size": "100%"}]})
    assert '--bar-size:50%' in dashboard_renderer._render_leaderboard({"bars": [{"label": "A", "value": "1", "size": 50}]})
    assert '--bar-size:50%' in dashboard_renderer._render_bar({"bars": [{"label": "A", "value": "1", "size": 50}]})
    assert 'x="0" y="0" width="50" height="10"' in dashboard_renderer._render_stacked({"segments": [{"label": "A", "value": "1", "size": 50}]})
    assert '--progress-size:50%' in dashboard_renderer._render_progress({"bars": [{"label": "A", "value": "1", "size": 50}]})
    metric_html = dashboard_renderer._render_metric_grid({"tiles": [{"label": "USD", "value": "101.22"}]})
    assert 'class="metric-tile"' in metric_html and 'aria-label="USD: 101.22"' in metric_html
    assert "--bar-size" not in metric_html and "--segment-size" not in metric_html and "--progress-size" not in metric_html
    with pytest.raises(ValueError, match="bar row 1 requires"):
        dashboard_renderer._render_bar({"bars": [{"label": "A", "value": "1"}]})
    with pytest.raises(ValueError, match="geometry fields"):
        dashboard_renderer._render_metric_grid({"tiles": [{"label": "USD", "value": "1", "size": "50%"}]})
    for renderer_fn, payload in (
        (dashboard_renderer._render_leaderboard, {"bars": [{"label": "A", "value": "1"}]}),
        (dashboard_renderer._render_progress, {"bars": [{"label": "A", "value": "1"}]}),
        (dashboard_renderer._render_stacked, {"segments": [{"label": "A", "value": "1"}]}),
    ):
        with pytest.raises(ValueError, match="requires"):
            renderer_fn(payload)
    for renderer_fn, payload in (
        (dashboard_renderer._render_leaderboard, {"bars": [{"label": "A", "value": "1", "size": "50%;background:url(x)"}]}),
        (dashboard_renderer._render_bar, {"bars": [{"label": "A", "value": "1", "size": "50%;background:url(x)"}]}),
        (dashboard_renderer._render_progress, {"bars": [{"label": "A", "value": "1", "size": "url(x)"}]}),
        (dashboard_renderer._render_stacked, {"segments": [{"label": "A", "value": "1", "size": "-1%"}]}),
        (dashboard_renderer._render_stacked, {"segments": [{"label": "A", "value": "1", "size": "50%;background:url(x)"}]}),
    ):
        with pytest.raises(ValueError):
            renderer_fn(payload)
    pages, production_manifest = dashboard_renderer.render_dashboard_site(fixture)
    rendered_html = "".join(value.decode("utf-8") if isinstance(value, bytes) else value for name, value in pages.items() if name.endswith(".html"))
    manager_html = "".join(
        value.decode("utf-8") if isinstance(value, bytes) else value
        for name, value in pages.items()
        if name == "index.html" or name.startswith("domains/")
    )
    audit_html = "".join(
        value.decode("utf-8") if isinstance(value, bytes) else value
        for name, value in pages.items()
        if name in {"data-quality-audit.html", "ontology.html", "evidence.html"}
    )
    assert len(production_manifest["items"]) == 57
    assert rendered_html.count('class="donut-ring"') == 8
    # This legacy fixture has no Product plan.  All supplied progress bars are
    # rendered as authored; no title-based source/join filter may change the
    # count or strip their values.
    expected_progress_bars = sum(
        len(widget.get("bars") or [])
        for widget in fixture["widgets"]
        if widget.get("type") == "progress"
    )
    assert rendered_html.count('class="progress-fill"') == expected_progress_bars
    assert rendered_html.count('class="leaderboard-rank"') == 9
    assert rendered_html.count('class="metric-tile"') == 15
    assert rendered_html.count('class="stack-segment stack-segment-') == 8
    assert rendered_html.count('class="stack-legend"') == 1
    assert 'class="technical-audit"' not in manager_html
    assert "Technical audit &amp; evidence" not in manager_html
    assert "requirements/REQ-03/accepted/manifest.json" not in manager_html
    assert "Technical audit records" in audit_html
    assert "requirements/REQ-03/accepted/manifest.json" in audit_html
    assert all(total not in rendered_html for total in unsupported_totals)
    fill_start = rendered_html.index('id="widget-req07-fill-proxy"')
    fill_end = rendered_html.index("</article>", fill_start)
    fill_html = rendered_html[fill_start:fill_end]
    assert "69.0%" in fill_html and "68.71%" in fill_html
    assert "100%" not in fill_html and "99.6%" not in fill_html
    canonical_css = Path(dashboard_renderer.__file__).resolve().parent.parent / "assets" / "dashboard.css"
    assert pages["assets/dashboard.css"] == canonical_css.read_bytes()
    css_text = canonical_css.read_text(encoding="utf-8")
    assert "min-width: fit-content" not in css_text
    assert "flex: 0 0 var(--segment-size" not in css_text
    assert ".stack-segment {" in css_text and "stroke: none" in css_text


def test_dashboard_v4_chart_vocabulary_graph_and_baseline_integrity() -> None:
    dashboard_root = Path(__file__).resolve().parents[3]
    run_root = dashboard_root / "benchmark_a_requirement_v070_entity_run_a_rerun_1"
    fixture_path = run_root / "reviewed_dashboard_fixture_v4.json"
    chart_map_path = run_root / "products" / "decision_dashboard_chart_map_v4.json"
    registry_path = run_root / "products" / "decision_dashboard_chart_registry_v4.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    chart_map = json.loads(chart_map_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    # The committed historical fixture predates the persisted V4 business
    # presentation plan.  Keep this chart-vocabulary regression audit-only in
    # memory; current V4 products require pointer-bound plan entries for any
    # manager admission, and this test must not reintroduce lexical fallback.
    fixture["manager_admission"] = {
        "policy": "explicit_business_presentation_plan",
        "presentation_plan_ref": None,
        "presentation_plan_sha256": None,
    }
    fixture["overview_widget_ids"] = []
    for widget in fixture["widgets"]:
        widget["manager_admission"] = {"status": "audit_only", "reason": "historical chart vocabulary fixture"}
        widget["presentation_audience"] = "technical_audit"
        widget["presentation_tier"] = "audit"
        widget.pop("overview", None)
    fixture["audit_widgets"] = [
        {
            "widget_id": widget["id"],
            "requirement_id": widget.get("requirement_id", ""),
            "record_ids": [widget.get("integration_record_id")] if widget.get("integration_record_id") else [],
            "widget_snapshot": json.loads(json.dumps(widget)),
            "evidence_refs": list(widget.get("evidence_refs", [])),
            "trace_refs": list(widget.get("trace_refs", [])),
            "reference_union": sorted(set(widget.get("evidence_refs", [])) | set(widget.get("trace_refs", []))),
        }
        for widget in fixture["widgets"]
    ]
    fixture["audit_widget_entry_count"] = len(fixture["audit_widgets"])
    fixture_types = {widget["id"]: widget["type"] for widget in fixture["widgets"]}
    map_types = {chart["id"]: chart["type"] for chart in chart_map["charts"]}
    assert len(fixture_types) == len(map_types) == 57
    assert fixture_types == map_types
    assert fixture["chart_registry_ref"] == chart_map["chart_registry_ref"] == "products/decision_dashboard_chart_registry_v4.json"
    assert all(widget.get("reviewed_item_ref") and widget.get("reviewed_output_ref") and widget.get("evidence_refs") and widget.get("trace_refs") for widget in fixture["widgets"])
    assert {widget_id for widget_id, kind in fixture_types.items() if kind == "column"} == {
        "req01-stage-lateness", "req02-ecom-channel-bars", "req02-erp-channel-bars", "req04-tracker-queue",
        "req04-receipt-reasons", "req06-policy-actions", "req07-absolute-error", "req07-matched-keys",
    }
    assert {widget_id for widget_id, kind in fixture_types.items() if kind == "lollipop"} == {
        "req01-carrier-late", "req02-segment-bars", "req04-supplier-late-queue", "req05-control-queues",
        "req08-reconciliation-queues", "req08-workfile-controls",
    }
    assert {widget_id for widget_id, kind in fixture_types.items() if kind == "diverging_bar"} == {"req07-signed-error"}
    assert {widget_id for widget_id, kind in fixture_types.items() if kind == "waffle"} == {
        "req03-complaint-status", "req05-invoice-status", "req05-paid-timing",
    }
    assert {kind: list(fixture_types.values()).count(kind) for kind in ("column", "lollipop", "diverging_bar", "waffle")} == {
        "column": 8, "lollipop": 6, "diverging_bar": 1, "waffle": 3,
    }
    for widget_id in ("req02-ecom-channel-bars", "req02-erp-channel-bars"):
        widget = next(widget for widget in fixture["widgets"] if widget["id"] == widget_id)
        assert widget["small_multiple_group"] == "req02-channel-source"
        assert widget["scale_policy"] == "independent source-local scale"
    allowed_small_multiple_ids = {"req02-ecom-channel-bars", "req02-erp-channel-bars"}
    for widget in fixture["widgets"]:
        chart = next(chart for chart in chart_map["charts"] if chart["id"] == widget["id"])
        fields = chart["fields_or_values_used"]
        for key in ("small_multiple_group", "scale_policy"):
            assert widget.get(key) == fields.get(key)
            if key in widget or key in fields:
                assert widget["id"] in allowed_small_multiple_ids
    signed = next(widget for widget in fixture["widgets"] if widget["id"] == "req07-signed-error")
    assert [row["signed_size"] for row in signed["bars"]] == ["-100%", "-61.4%"]
    assert set(chart["family"] for chart in chart_map["charts"] if chart["id"] in {"req07-signed-error", "req03-complaint-status"}) == {"diverging_bar", "waffle"}
    family_ids = {entry["id"] for entry in registry["families"]}
    assert {"column", "lollipop", "bullet", "waterfall", "waffle", "treemap", "funnel", "pareto", "network_ontology_graph"} <= family_ids
    assert len(registry["references"]) == 8
    with pytest.raises(ValueError, match="column row 1 requires"):
        dashboard_renderer._render_column({"bars": [{"label": "A", "value": "1"}]})
    with pytest.raises(ValueError, match="non-empty label"):
        dashboard_renderer._render_column({"bars": [{"label": "", "value": "1", "size": "50%"}]})
    with pytest.raises(ValueError, match="non-empty value"):
        dashboard_renderer._render_column({"bars": [{"label": "A", "value": "", "size": "50%"}]})
    with pytest.raises(ValueError, match="lollipop row 1 requires"):
        dashboard_renderer._render_lollipop({"bars": [{"label": "A", "value": "1"}]})
    with pytest.raises(ValueError, match="non-empty label"):
        dashboard_renderer._render_lollipop({"bars": [{"label": "", "value": "1", "size": "50%"}]})
    with pytest.raises(ValueError, match="signed_size"):
        dashboard_renderer._render_diverging_bar({"bars": [{"label": "A", "value": "-1", "size": "50%"}]})
    with pytest.raises(ValueError, match="non-empty value"):
        dashboard_renderer._render_diverging_bar({"bars": [{"label": "A", "value": "", "signed_size": "-50%"}]})
    with pytest.raises(ValueError):
        dashboard_renderer._render_diverging_bar({"bars": [{"label": "A", "value": "-1", "signed_size": "-50%;background:url(x)"}]})
    with pytest.raises(ValueError, match="denominator_value"):
        dashboard_renderer._render_waffle({"categories": [{"label": "A", "value": "1", "size": "100%"}]})
    with pytest.raises(ValueError):
        dashboard_renderer._render_waffle({"denominator_value": 1, "denominator_label": "rows", "categories": [{"label": "A", "value": "1", "size": "50%;background:url(x)"}, {"label": "B", "value": "0", "size": "50%"}]})
    invalid_ontology = json.loads(json.dumps(fixture))
    invalid_ontology["ontology_objects"][0].pop("kind")
    with pytest.raises(ValueError, match="non-empty id, label, and kind"):
        dashboard_renderer._ontology_graph_body(invalid_ontology)
    invalid_ontology = json.loads(json.dumps(fixture))
    invalid_ontology["ontology_relationships"][0].pop("label")
    with pytest.raises(ValueError, match="relationships require"):
        dashboard_renderer._ontology_graph_body(invalid_ontology)
    invalid_ontology = json.loads(json.dumps(fixture))
    invalid_ontology["ontology_summary"]["ontology_items"] = "192"
    with pytest.raises(ValueError, match="numeric ontology_items"):
        dashboard_renderer._ontology_graph_body(invalid_ontology)
    invalid_ontology = json.loads(json.dumps(fixture))
    invalid_ontology.pop("ontology_groups")
    generic_ontology = dashboard_renderer._ontology_body(invalid_ontology)
    assert generic_ontology.count('class="ontology-node"') == 12
    assert generic_ontology.count('class="ontology-edge"') == 9
    assert generic_ontology.count('class="ontology-lane"') == 1
    assert "Unassigned / supplied objects" in generic_ontology
    css = (dashboard_renderer._canonical_dashboard_css()).decode("utf-8")
    assert "height: 13px" in css and "height: 20px" in css and "min-height: 220px" in css and "height: 35px" in css
    assert "gradient" not in css and "min-width: fit-content" not in css
    with pytest.raises(ValueError, match="RunContext"):
        dashboard_renderer.render_dashboard_site(fixture)
    context = RunContext(fixture["run_id"], run_root)
    chart_html, chart_manifest = dashboard_renderer.render_dashboard(fixture, context=context)
    pages, manifest = dashboard_renderer.render_dashboard_site(fixture, context=context)
    # The historical fixture is intentionally all-audit under the strict V4
    # contract, so manager domain pages omit its cards.  Chart vocabulary is
    # asserted against the standalone raw renderer output instead.
    html = chart_html
    ontology = pages["ontology.html"]
    ontology_text = ontology.decode("utf-8") if isinstance(ontology, bytes) else ontology
    assert len(chart_manifest["items"]) == 57 and manifest["site_version"] == 4
    assert manifest["chart_registry_ref"] == "products/decision_dashboard_chart_registry_v4.json"
    assert manifest["chart_registry_sha256"] == hashlib.sha256(registry_path.read_bytes()).hexdigest()
    assert html.count('class="column-item"') == sum(len(widget.get("bars", widget.get("categories", []))) for widget in fixture["widgets"] if widget["type"] == "column")
    assert html.count('class="lollipop-row"') == sum(len(widget.get("bars", [])) for widget in fixture["widgets"] if widget["type"] == "lollipop")
    assert html.count('class="waffle-cell waffle-cell-') == 300
    assert html.count('class="diverging-mark"') == 2
    assert ontology_text.count('class="ontology-node"') == 12
    assert ontology_text.count('class="ontology-edge"') == 9
    assert ontology_text.count('class="ontology-lane"') == 3
    assert "Full ontology snapshot" in ontology_text and "navigable product projection" in ontology_text
    assert all(label in ontology_text for label in ("Customer &amp; Fulfillment", "Procurement &amp; Payables", "Inventory"))
    edge_label_positions = {
        match.group(3): (match.group(1), match.group(2))
        for match in re.finditer(
            r'<text class="ontology-edge-label"[^>]* x="([^"]+)" y="([^"]+)">([^<]+)</text>',
            ontology_text,
        )
    }
    assert len(edge_label_positions) == 9
    assert edge_label_positions["completes as"] != edge_label_positions["may return"]
    assert 'data-route="lane-gutter"' in ontology_text
    assert 'points="326,116 338,116 338,268 176,268"' in ontology_text
    assert 'paint-order: stroke' in (dashboard_renderer._canonical_dashboard_css()).decode("utf-8")
    node_boxes = [
        tuple(float(match.group(index)) for index in range(1, 5))
        for match in re.finditer(
            r'<g class="ontology-node"[^>]*><rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"',
            ontology_text,
        )
    ]
    assert len(node_boxes) == 12
    # Approximate SVG text bounds using the renderer's 9px label style. This
    # catches regression into a node rectangle without relying on a browser.
    for label, (x_text, y_text) in edge_label_positions.items():
        label_width = max(5.0, len(unescape(label)) * 5.2)
        label_box = (float(x_text) - label_width / 2, float(y_text) - 11.0, label_width, 12.0)
        for node_x, node_y, node_width, node_height in node_boxes:
            horizontal_overlap = min(label_box[0] + label_box[2], node_x + node_width) - max(label_box[0], node_x)
            vertical_overlap = min(label_box[1] + label_box[3], node_y + node_height) - max(label_box[1], node_y)
            assert horizontal_overlap <= 0 or vertical_overlap <= 0, (label, label_box, (node_x, node_y, node_width, node_height))
    assert "<script" not in html and "https://" not in html
    unsupported_totals = ("76,674.09", "66,206.54", "83,105.69", "218,834.10")
    assert all(total not in html for total in unsupported_totals)
    assert (run_root / "reviewed_dashboard_fixture_v3.json").read_bytes().__len__() == 117758
    assert hashlib.sha256((run_root / "reviewed_dashboard_fixture_v3.json").read_bytes()).hexdigest() == "50a8f1c3d9114ef721297361808407f72561b3cc3fe5a1a220b4cb87dcd115f1"
    assert hashlib.sha256((run_root / "products" / "decision_dashboard_chart_map_v3.json").read_bytes()).hexdigest() == "57081674395b2a33e047d5f8810ec4d28bba46b5524e5e8bd912dd4308fd1dc4"
def test_dashboard_rejects_escape_before_probe_or_output_creation(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    context = RunContext("RUN-TEST", run_root)
    outside = tmp_path / "sibling"
    outside.mkdir()
    with pytest.raises(AllowedRootError):
        dashboard_renderer.render_fixture(context, outside / "fixture.json", "nested/dashboard.html", "nested/manifest.json")
    assert not (run_root / "products").exists()
    with pytest.raises(AllowedRootError):
        dashboard_renderer.render_fixture(context, "fixture.json", "../escape/dashboard.html")
    assert not (tmp_path / "escape").exists()

    fixture = run_root / "fixture.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(json.dumps(_fixture()), encoding="utf-8")
    output_outside = tmp_path / "output"
    with pytest.raises(AllowedRootError):
        dashboard_renderer.render_fixture(context, fixture, output_outside / "dashboard.html")
    assert not output_outside.exists()

    if hasattr(Path, "symlink_to"):
        outside_fixture = outside / "real.json"
        outside_fixture.write_text(json.dumps(_fixture()), encoding="utf-8")
        linked = run_root / "linked.json"
        linked.symlink_to(outside_fixture)
        with pytest.raises(AllowedRootError):
            dashboard_renderer.render_fixture(context, linked, "dashboard.html")


def _freeze_manifest() -> dict[str, object]:
    return {
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "run_id": "RUN-TEST",
        "review_routing": {"fresh_sol_review_available": False},
    }


def _freeze_sibling_value(name: str) -> object:
    if name in {"freeze", "preconditions", "product_freeze", "freeze_manifest", "frozen_products"}:
        return {"answers_frozen": True}
    return True


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_evidence_collector_hashes_facts_and_never_mutates_products(tmp_path: Path) -> None:
    context = RunContext("RUN-TEST", tmp_path / "run")
    products = context.run_root / "products"
    telemetry_dir = context.run_root / "telemetry"
    traces = context.run_root / "traces"
    scripts = context.run_root / "scripts"
    for path in (products, telemetry_dir, traces, scripts):
        path.mkdir(parents=True)
    manifest_path = products / "product_manifest.json"
    manifest_path.write_text(json.dumps(_freeze_manifest()), encoding="utf-8")
    telemetry_path = telemetry_dir / "events.jsonl"
    telemetry_path.write_text(json.dumps({"event_type": "review_routed", "status": "unavailable", "error_class": "cache_miss", "classification": "substrate"}) + "\n", encoding="utf-8")
    (traces / "trace.md").write_text("repeated read context; capability gap observed\n", encoding="utf-8")
    (scripts / "one.py").write_text("print('same')\n", encoding="utf-8")
    (scripts / "two.py").write_text("print('same')\n", encoding="utf-8")
    input_paths = [manifest_path, telemetry_path, traces / "trace.md", scripts / "one.py", scripts / "two.py"]
    before = {path: _digest(path) for path in input_paths}
    result = evidence_collector.collect_evidence(
        context,
        products_manifest="products/product_manifest.json",
        telemetry=["telemetry/events.jsonl"],
        traces=["traces"],
        scripts=["scripts"],
    )
    assert result["optimizer_status"] == "complete"
    assert result["analytical_complete"] is True
    assert result["input_hashes_unchanged"] is True
    assert result["output_names"] == ["optimizer_evidence_bundle.md", "optimizer_evidence_appendix.md"]
    assert result["exact_duplicate_groups"] == [["scripts/one.py", "scripts/two.py"]]
    assert sorted(path.name for path in (context.run_root / "optimizer").iterdir()) == ["optimizer_evidence_appendix.md", "optimizer_evidence_bundle.md"]
    bundle = (context.run_root / "optimizer" / "optimizer_evidence_bundle.md").read_text(encoding="utf-8")
    appendix = (context.run_root / "optimizer" / "optimizer_evidence_appendix.md").read_text(encoding="utf-8")
    for category in ("repeated_code", "repeated_reads_context", "cache_misses", "reviewer_bottleneck", "capability_gaps"):
        assert f"### {category}" in bundle
    assert "All analytical inputs unchanged: yes" in appendix
    assert before == {path: _digest(path) for path in input_paths}
    assert "Optimization Agent" in bundle


def test_evidence_collector_non_blocking_failure_preserves_analytical_completion(tmp_path: Path) -> None:
    context = RunContext("RUN-TEST", tmp_path / "run")
    result = evidence_collector.collect_evidence_non_blocking(
        context,
        products_manifest="products/missing.json",
        analytical_complete=True,
    )
    assert result["optimizer_status"] == "technical_failure"
    assert result["analytical_complete"] is True
    assert result["error_type"] in {"FileNotFoundError", "AllowedRootError"}
    assert not (context.run_root / "optimizer").exists()

    with pytest.raises(AllowedRootError):
        evidence_collector.collect_evidence(context, products_manifest=tmp_path / "sibling.json")


@pytest.mark.parametrize(
    "legacy_markers",
    [
        {sibling: _freeze_sibling_value(sibling)}
        for sibling in FORBIDDEN_FREEZE_SIBLINGS
    ],
)
def test_evidence_collector_rejects_legacy_freeze_shapes(tmp_path: Path, legacy_markers: dict[str, object]) -> None:
    context = RunContext("RUN-TEST", tmp_path / "run")
    products = context.run_root / "products"
    products.mkdir(parents=True)
    manifest_path = products / "product_manifest.json"
    manifest_path.write_text(json.dumps(legacy_markers), encoding="utf-8")

    with pytest.raises(evidence_collector.EvidenceCollectorPreconditionError) as exc_info:
        evidence_collector.collect_evidence(context, products_manifest="products/product_manifest.json")
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "freeze_markers" in str(exc_info.value)
    assert not (context.run_root / "optimizer").exists()


@pytest.mark.parametrize("sibling", FORBIDDEN_FREEZE_SIBLINGS)
def test_evidence_collector_rejects_canonical_freeze_with_every_forbidden_sibling(
    tmp_path: Path,
    sibling: str,
) -> None:
    context = RunContext("RUN-TEST", tmp_path / "run")
    products = context.run_root / "products"
    products.mkdir(parents=True)
    manifest = _freeze_manifest()
    manifest[sibling] = _freeze_sibling_value(sibling)
    (products / "product_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(evidence_collector.EvidenceCollectorPreconditionError, match=sibling):
        evidence_collector.collect_evidence(context, products_manifest="products/product_manifest.json")


def test_evidence_collector_rejects_malformed_or_false_canonical_markers(tmp_path: Path) -> None:
    context = RunContext("RUN-TEST", tmp_path / "run")
    products = context.run_root / "products"
    products.mkdir(parents=True)
    manifest_path = products / "product_manifest.json"
    marker_values = _freeze_manifest()["freeze_markers"]
    assert isinstance(marker_values, dict)

    marker_values["telemetry_frozen"] = False
    manifest_path.write_text(json.dumps({"freeze_markers": marker_values}), encoding="utf-8")
    with pytest.raises(evidence_collector.EvidenceCollectorPreconditionError, match="telemetry_frozen"):
        evidence_collector.collect_evidence(context, products_manifest="products/product_manifest.json")

    marker_values.pop("telemetry_frozen")
    manifest_path.write_text(json.dumps({"freeze_markers": marker_values}), encoding="utf-8")
    with pytest.raises(evidence_collector.EvidenceCollectorPreconditionError, match="missing fields"):
        evidence_collector.collect_evidence(context, products_manifest="products/product_manifest.json")


def test_dashboard_rejects_plural_decision_flow_metadata(tmp_path: Path) -> None:
    fixture = _fixture()
    domain = fixture["domains"][0]
    assert isinstance(domain, dict)
    domain["decision_flows"] = domain.pop("decision_flow")
    with pytest.raises(ValueError, match="singular decision_flow"):
        dashboard_renderer.render_dashboard(fixture)


@pytest.mark.parametrize("sibling", FORBIDDEN_FREEZE_SIBLINGS)
def test_dashboard_rejects_canonical_freeze_with_every_forbidden_sibling(sibling: str) -> None:
    fixture = _fixture()
    fixture[sibling] = _freeze_sibling_value(sibling)

    with pytest.raises(ValueError, match=sibling):
        dashboard_renderer.render_dashboard(fixture)


@pytest.mark.parametrize("sibling", FORBIDDEN_FREEZE_SIBLINGS)
def test_dashboard_rejects_every_legacy_freeze_shape_without_canonical(sibling: str) -> None:
    fixture = _fixture()
    fixture.pop("freeze_markers")
    fixture[sibling] = _freeze_sibling_value(sibling)

    with pytest.raises(ValueError, match=sibling):
        dashboard_renderer.render_dashboard(fixture)


def test_benchmark_a_is_preparation_only_and_launch_has_no_extra_step() -> None:
    expected = {"README.md", "questions.md", "run_config.example.json", "baseline_v0.2.0.json", "comparison_schema.json", "evaluation_checklist.md", "commands.md"}
    assert {path.name for path in BENCHMARK.iterdir() if path.is_file()} == expected
    parsed = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in BENCHMARK.glob("*.json")}
    expected_question_hash = "3a40d2f7083f0d2f0e1b216d405a0ce6c38cd4913e157b9e48a99dfa96958236"
    assert parsed["run_config.example.json"]["question_order_sha256"] == expected_question_hash
    assert parsed["baseline_v0.2.0.json"]["baseline"]["question_order_sha256"] == expected_question_hash
    required_comparison_fields = {"answer_quality", "model_tool_workload", "core_cache_use", "prepared_data_reuse", "dashboard_quality", "source_immutability"}
    assert required_comparison_fields.issubset(parsed["comparison_schema.json"]["required"])
    assert expected_question_hash in (BENCHMARK / "questions.md").read_text(encoding="utf-8")
    readme = (BENCHMARK / "README.md").read_text(encoding="utf-8")
    commands = (BENCHMARK / "commands.md").read_text(encoding="utf-8")
    assert "not executed" in readme.lower()
    assert "PREPARE" in commands and "LAUNCH LATER" in commands
    assert "explicit confirmation" not in commands.lower()
    assert "analysis_call" not in (BENCHMARK / "run_config.example.json").read_text(encoding="utf-8")
    assert expected_question_hash in commands
    for marker in ("skill_name: auto-foundry-agentic-e2e", "skill_version: 0.7.1", "core_name: auto_foundry_core", "core_version: 0.8.0"):
        assert marker in commands
    assert "82e9c913bf437ac9e361d6890467a9aed9b1c6db9d887cfcf0cd659035a71ec2" in commands


def test_release_validator_rejects_stale_core_source_bytes(tmp_path: Path) -> None:
    source_root = ROOT / "src" / "auto_foundry_core"
    source_files = release_validator._core_source_files(source_root)
    wheel_path = tmp_path / "stale.whl"
    with release_validator.zipfile.ZipFile(wheel_path, "w", compression=release_validator.zipfile.ZIP_DEFLATED) as archive:
        for name, payload in source_files.items():
            archive.writestr(name, payload + (b"\n" if name.endswith("/durable.py") else b""))
        archive.writestr(
            "auto_foundry_core-0.8.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: auto_foundry_core\nVersion: 0.8.0\n",
        )
    with pytest.raises(ValueError, match="wheel source byte mismatch"):
        release_validator._validate_wheel(wheel_path, source_root)


def test_pointer_bound_progress_manager_projection_never_leaks_raw_bars() -> None:
    raw = {
        "id": "REQ-03-progress",
        "type": "progress",
        "title": "Finance refunds PENDING",
        "value": 25,
        "bars": [{"label": "raw diagnostic bar", "value": 25, "size": "2.246181%"}],
        "requirement_id": "REQ-03",
        "presentation_role": "decision_view",
        "presentation_tier": "primary",
        "manager_admission": {"status": "admitted", "presentation_audience": "business_manager"},
        "manager_presentation": {
            "widget_id": "REQ-03-progress",
            "record_id": "REQ-03.metric.finance-pending-refunds",
            "requirement_id": "REQ-03",
            "presentation_role": "decision_view",
            "file_sha256": "a" * 64,
            "canonical_payload_sha256": "b" * 64,
            "display_projection": {
                "title": {"pointer": "/payload/label", "value": "Finance refunds PENDING"},
                "value": {"pointer": "/payload/value", "value": 25},
                "unit": {"pointer": "/payload/units", "value": "refund records"},
                "denominator": {"pointer": "/payload/population", "value": 1113},
            },
        },
        "integration_record_ids": ["REQ-03.metric.finance-pending-refunds"],
        "evidence_refs": ["work/evidence.jsonl"],
        "trace_refs": ["requirements/REQ-03/integration/committed/records.jsonl"],
    }
    manager_html = dashboard_renderer._site_widget(raw, dashboard_renderer._trace_records(raw))
    audit_html = dashboard_renderer._render_technical_audit([raw], [], scope="REQ-03")
    assert "2.246181%" not in manager_html
    assert "raw diagnostic bar" not in manager_html
    assert "2.246181%" in audit_html
