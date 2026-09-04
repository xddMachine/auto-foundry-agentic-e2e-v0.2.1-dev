"""Focused product-only checks for the deterministic dashboard assembler."""

from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import copy
import multiprocessing
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPT = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_assembler.py"
spec = importlib.util.spec_from_file_location("dashboard_assembler_test", SCRIPT)
assert spec and spec.loader
dashboard_assembler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboard_assembler
spec.loader.exec_module(dashboard_assembler)
DELTA_SCRIPT = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_delta_assembler.py"
delta_spec = importlib.util.spec_from_file_location("dashboard_generation_product_test", DELTA_SCRIPT)
assert delta_spec and delta_spec.loader
dashboard_generation_product = importlib.util.module_from_spec(delta_spec)
sys.modules[delta_spec.name] = dashboard_generation_product
delta_spec.loader.exec_module(dashboard_generation_product)
RENDERER_SCRIPT = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_renderer.py"
renderer_spec = importlib.util.spec_from_file_location("dashboard_renderer_product_test", RENDERER_SCRIPT)
assert renderer_spec and renderer_spec.loader
dashboard_renderer = importlib.util.module_from_spec(renderer_spec)
sys.modules[renderer_spec.name] = dashboard_renderer
renderer_spec.loader.exec_module(dashboard_renderer)

from auto_foundry_core.durable import ItemWorkspace  # noqa: E402
from auto_foundry_core.analytical_artifacts import DataProfileArtifact  # noqa: E402
from auto_foundry_core.integration import IntegrationSession  # noqa: E402
from auto_foundry_core.lifecycle import RunLifecycle  # noqa: E402
from auto_foundry_core.prepared import PreparedAssetRegistry  # noqa: E402
from auto_foundry_core.requirement_planning import inspect_product_manifest  # noqa: E402
from auto_foundry_core.product_review import ProductCandidate, ProductReviewStore, canonical_hash  # noqa: E402
from auto_foundry_core.workspace import (  # noqa: E402
    DEFAULT_CORE_VERSION,
    DEFAULT_SKILL_VERSION,
    RunContext,
)


def _seed_run(root: Path) -> RunContext:
    context = RunContext("RUN-ASSEMBLER-TEST", root)
    RunLifecycle.create(context, ("REQ-A",), mode="requirement")
    workspace = ItemWorkspace.create(context, "REQ-A", mode="requirement", original_text="synthetic requirement")
    workspace.write_plan({"item_id": "REQ-A", "offline": True})
    workspace.write_draft({"item_id": "REQ-A", "answer": "bounded", "limitations": ["synthetic only"]})
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(context, workspace, registry, "synthetic-integration", invocation_id="inv-assembler")
    evidence = ("answer_content.json",)
    session.add_metric(
        metric_id="metric-scalar",
        scope="REQ-A",
        evidence_refs=evidence,
        label="Reviewed scalar",
        units="records",
        value=7,
        population=10,
    )
    session.add_metric(
        metric_id="metric-currency",
        scope="REQ-A",
        evidence_refs=evidence,
        label="Currency partitions",
        units="source currency partitions; no FX conversion",
        value={"EUR": 12.5, "USD": 9.25},
    )
    if session.fidelity_result is None:
        session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()
    (root / "requirement_supervisor_plan.json").write_text(
        json.dumps({"groups": [{"id": "commercial", "title": "Commercial decisions", "requirement_ids": ["REQ-A"]}]}, sort_keys=True),
        encoding="utf-8",
    )
    return context


def _seed_failed_root_run(root: Path) -> RunContext:
    """Create one terminally failed requirement for root-product rebuild tests."""

    context = RunContext("RUN-ASSEMBLER-FAILED-TEST", root)
    RunLifecycle.create(context, ("REQ-008",), mode="requirement")
    workspace = ItemWorkspace.create(
        context,
        "REQ-008",
        mode="requirement",
        original_text="Synthetic failed requirement",
    )
    workspace.technical_failure("transport exhausted", recovery_exhausted=True)
    (root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {"groups": [{"id": "synthetic", "title": "Synthetic requirements", "requirement_ids": ["REQ-008"]}]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    telemetry = root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text("", encoding="utf-8")
    (telemetry / "inventory_counters.json").write_text('{"accepted":0}\n', encoding="utf-8")
    return context


def _hold_root_assembly_lock(args: tuple[str, str, str]) -> None:
    lock_path, entered_path, release_path = (Path(value) for value in args)
    with dashboard_assembler._assembly_lock(lock_path):
        entered_path.write_text("held\n", encoding="utf-8")
        deadline = time.monotonic() + 15
        while not release_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)


def _acquire_root_assembly_lock(args: tuple[str, str]) -> None:
    lock_path, acquired_path = (Path(value) for value in args)
    with dashboard_assembler._assembly_lock(lock_path):
        acquired_path.write_text("acquired\n", encoding="utf-8")


def test_site_link_validation_scales_with_large_target_and_rejects_broken_fragment() -> None:
    """A large manager page is scanned once while cross-page links stay strict."""

    manager_content = "manager evidence " * 500_000
    evidence = f'<html><body><div id="manager-evidence">{manager_content}</div></body></html>'
    index = "<html><body>" + "".join(
        '<a href="evidence.html#manager-evidence">Open evidence</a>'
        for _ in range(2_048)
    ) + "</body></html>"
    pages: dict[str, str | bytes] = {
        "index.html": index,
        "evidence.html": evidence,
        "assets/dashboard.css": b".shell {}",
    }

    started = time.perf_counter()
    dashboard_renderer._validate_site_links(pages)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0

    broken_pages = dict(pages)
    broken_pages["index.html"] = index.replace("#manager-evidence", "#missing-evidence", 1)
    with pytest.raises(ValueError, match="broken site fragment"):
        dashboard_renderer._validate_site_links(broken_pages)


def test_failed_items_are_visible_without_raw_failure_reasons() -> None:
    """Terminal fixture entries render as explicit safe limitations."""

    fixture = {
        "title": "Failure visibility contract",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": [{
            "id": "healthy-signal",
            "type": "kpi",
            "title": "Reviewed signal",
            "value": 3,
            "requirement_id": "REQ-OK",
            "requirement_title": "Reviewed signal",
            "reviewed_item_ref": "requirements/REQ-OK/accepted/manifest.json",
            "reviewed_output_ref": "requirements/REQ-OK/accepted/answer_content.json",
            "evidence_refs": ["work/evidence.json"],
            "trace_refs": ["work/evidence.json"],
        }],
        "domains": [{
            "id": "operations",
            "title": "Operations",
            "order": 1,
            "decision_flow": [{
                "id": "operations-flow",
                "title": "Operations",
                "order": 1,
                "widget_ids": ["healthy-signal"],
            }],
        }],
        "failed_items": [{
            "item_id": "REQ-008",
            "status": "technical_failure",
            "recovery_exhausted": True,
            "manifest_ref": "requirements/REQ-008/integration/technical_failure/manifest.json",
            "manifest_hash": "a" * 64,
            "reason_hash": "b" * 64,
            "reason": "PRIVATE RAW FAILURE MUST NOT BE RENDERED",
        }],
    }

    single_document, _single_manifest = dashboard_renderer.render_dashboard(fixture)
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    html = "\n".join(
        [single_document]
        + [
        value.decode("utf-8") if isinstance(value, bytes) else value
        for name, value in pages.items()
        if name.endswith(".html")
        ]
    )
    assert '<section class="limits failed-items"' in html
    assert "<dt>item_id</dt><dd>REQ-008</dd>" in html
    assert "<dt>status</dt><dd>technical_failure</dd>" in html
    assert "<dt>recovery_exhausted</dt><dd>true</dd>" in html
    assert dashboard_renderer._FAILED_ITEM_LIMITATION in html
    assert "requirements/REQ-008/integration/technical_failure/manifest.json" in html
    assert ("a" * 64) in html and ("b" * 64) in html
    assert "PRIVATE RAW FAILURE MUST NOT BE RENDERED" not in html


def test_live_shaped_accepted_table_rows_follow_explicit_plan_membership(tmp_path: Path) -> None:
    """Plan membership controls visibility while selected rows stay exact."""

    # This mirrors the live REQ-001 source-readiness visual.  Labels and rows
    # are opaque reviewed content: only the explicit Product plan may choose
    # whether this candidate is on the manager surface.
    rows = [
        {
            "source_id": "SALES_DOCUMENTS",
            "source_class": "table",
            "rows": 411966,
            "fields": ["document_id", "customer_id"],
            "type_summary": "tabular source",
            "grain": "one row per source",
            "candidate_key": "document_id",
            "date_authority": "created_at",
            "full_duplicate_rows": 0,
            "null_summary": "reviewed profile",
        },
        {
            "source_id": "README.md",
            "source_class": "document",
            "rows": None,
            "fields": [],
            "type_summary": "not a tabular source",
            "grain": "not applicable",
            "candidate_key": "not applicable",
            "date_authority": "not applicable",
            "full_duplicate_rows": "not applicable",
            "null_summary": "no tabular null profile",
        },
    ]
    context = RunContext("RUN-LIVE-SHAPED-TABLE", tmp_path / "run")
    content = {
        "item_id": "REQ-001",
        "scope": "Source readiness and consistency review",
        "visuals": [{
            "type": "table",
            "title": "Source readiness matrix",
            "columns": list(rows[0]),
            "rows": rows,
        }],
    }
    accepted = dashboard_assembler._accepted_visual_widgets(
        context,
        "REQ-001",
        content,
        {"artifact_progress": {"hashes": {}}},
        "b" * 64,
        "a" * 64,
    )
    assert len(accepted) == 1

    widgets = dashboard_assembler._build_widgets(
        "REQ-001",
        content,
        [],
        accepted_visuals=accepted,
    )
    widget = next(value for value in widgets if value.get("accepted_visual"))
    assert widget["rows"] == rows
    assert widget["manager_admission"]["status"] == "admitted"

    dashboard_assembler._apply_explicit_manager_admission(
        [widget],
        [],
        {},
    )
    assert widget["manager_admission"]["status"] == "audit_only"
    assert widget["rows"] == rows

    dashboard_assembler._apply_explicit_manager_admission(
        [widget],
        [widget["id"]],
        {widget["id"]: {}},
    )
    assert widget["manager_admission"]["status"] == "admitted"
    assert widget["rows"] == rows

    chart = dashboard_assembler._chart_map_entry(widget, "REQ-001", {"item_id": "REQ-001"})
    registry_path = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "dashboard_chart_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    recipes = {
        recipe["id"]: recipe
        for recipe in dashboard_assembler._dashboard_runtime().eligible_chart_recipes(widget, chart, registry)
    }
    assert recipes["table"]["eligible"] is True


def test_accepted_visuals_cover_all_requirements_and_bind_reviewed_sources(tmp_path: Path) -> None:
    """Accepted visual declarations feed the inventory independently of integration."""

    context = RunContext("RUN-ACCEPTED-VISUALS", tmp_path / "run")
    item_ids = [f"REQ-{index:03d}" for index in range(1, 10)]
    widgets: list[dict[str, object]] = []
    for item_id in item_ids:
        accepted = context.run_root / "requirements" / item_id / "accepted"
        work = context.run_root / "requirements" / item_id / "work"
        accepted.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        artifact_hashes: dict[str, str] = {}
        if item_id == "REQ-005":
            visuals = [{
                "type": "funnel",
                "title": "Reviewed process funnel",
                "values": [
                    {"stage": "Started", "count": 100, "share": 1.0},
                    {"stage": "Matched", "count": 75, "share": 0.75},
                ],
            }]
        elif item_id == "REQ-003":
            reference = "work/monthly.csv"
            source = "month,orders\n2020-01,12\n2020-02,15\n"
            (work / "monthly.csv").write_text(source, encoding="utf-8")
            artifact_hashes[reference] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            visuals = [{
                "type": "line",
                "title": "Reviewed monthly trend",
                "source_ref": reference,
                "series": ["orders"],
                "x": "month",
            }]
        elif item_id == "REQ-008":
            reference = "work/watchlist.json"
            source_value = {
                "rows": [
                    {"rank": 1, "customer_name": "Customer A", "baseline_orders": 20, "recent_orders": 8},
                    {"rank": 2, "customer_name": "Customer B", "baseline_orders": 14, "recent_orders": 7},
                ],
                "metadata": {"reviewed": True},
            }
            source_bytes = (json.dumps(source_value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            (work / "watchlist.json").write_bytes(source_bytes)
            artifact_hashes[reference] = hashlib.sha256(source_bytes).hexdigest()
            visuals = [{
                "type": "ranked_table",
                "title": "Accepted customer watchlist",
                "evidence_ref": reference,
                "fields": ["rank", "customer_name", "baseline_orders", "recent_orders"],
            }, {
                "type": "paired_bar",
                "title": "Baseline versus recent orders",
                "evidence_ref": reference,
                "dimension": "customer_name",
                "series": ["baseline_orders", "recent_orders"],
            }]
        else:
            visuals = [{
                "type": "table",
                "title": f"Accepted visual {item_id}",
                "rows": [{"label": "Reviewed", "value": item_id}],
            }]
        content = {
            "item_id": item_id,
            "headline_findings": [f"Reviewed business finding for {item_id}"],
            "limitations": ["Source-local reviewed evidence only."],
            "visuals": visuals,
        }
        content_bytes = (json.dumps(content, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        manifest = {
            "item_id": item_id,
            "content_hash": hashlib.sha256(content_bytes).hexdigest(),
            "artifact_progress": {"hashes": artifact_hashes},
        }
        (accepted / "answer_content.json").write_bytes(content_bytes)
        (accepted / "manifest.json").write_bytes(
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
        accepted_manifest_hash = hashlib.sha256((accepted / "manifest.json").read_bytes()).hexdigest()
        accepted_widgets = dashboard_assembler._accepted_visual_widgets(
            context,
            item_id,
            content,
            manifest,
            manifest["content_hash"],
            accepted_manifest_hash,
        )
        assert accepted_widgets
        for widget in accepted_widgets:
            widget.update({
                "requirement_id": item_id,
                "requirement_title": f"Requirement {item_id}",
                "manager_admission": {"status": "admitted", "presentation_audience": "business_manager"},
                "presentation_audience": "business_manager",
            })
        widgets.extend(accepted_widgets)

    by_requirement = {item_id: [widget for widget in widgets if widget.get("requirement_id") == item_id] for item_id in item_ids}
    assert all(by_requirement[item_id] for item_id in item_ids)
    trend = by_requirement["REQ-003"][0]
    assert trend["type"] == "line" and len(trend["points"]) == 2
    failed = by_requirement["REQ-008"]
    assert any(widget.get("type") == "table" and widget.get("rows") for widget in failed)
    paired = next(widget for widget in failed if widget.get("accepted_visual_type") == "paired_bar")
    assert paired["type"] == "grouped_bar" and paired["bars"]
    paired_sizes = [
        [series_item["size"] for series_item in row["series"]]
        for row in paired["bars"]
    ]
    # Paired-bar geometry uses one chart-wide maximum: baseline 20 is 100%,
    # baseline 14 is 70% (not 100% from a per-row scale).
    assert paired_sizes[0][0] == "100%"
    assert paired_sizes[1][0] == "70%"
    assert all(widget.get("accepted_content_hash") and widget.get("accepted_manifest_hash") for widget in widgets)
    ambiguous_rows, ambiguous_reason = dashboard_assembler._accepted_visual_source_rows(
        {"fields": ["value"]},
        {"first": [{"value": 1}], "second": [{"value": 2}]},
    )
    assert ambiguous_rows == []
    assert ambiguous_reason == "accepted visual source table selection is ambiguous"

    domains = [{
        "id": "accepted",
        "title": "Accepted decisions",
        "order": 1,
        "decision_flow": [{
            "id": "accepted-flow",
            "title": "Accepted requirements",
            "order": 1,
            "widget_ids": [widget["id"] for widget in widgets],
        }],
    }]
    fixture = {
        "title": "Accepted visual coverage",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": widgets,
        "domains": domains,
        "failed_items": [{
            "item_id": "REQ-008",
            "status": "technical_failure",
            "recovery_exhausted": True,
            "manifest_ref": "requirements/REQ-008/integration/technical_failure/manifest.json",
            "manifest_hash": "a" * 64,
            "reason_hash": "b" * 64,
            "reason": "private integration exception must never be rendered",
        }],
    }
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    html = "\n".join(
        value.decode("utf-8") if isinstance(value, bytes) else value
        for name, value in pages.items()
        if name.endswith(".html")
    )
    assert "REQ-008" in html and "Customer A" in html
    assert dashboard_renderer._FAILED_ITEM_LIMITATION in html
    assert "private integration exception" not in html
    assert str(context.run_root) not in html

    # The source boundary is fail-closed: changing reviewed bytes without
    # changing the accepted manifest cannot silently alter a chart/table.
    (work := context.run_root / "requirements" / "REQ-008" / "work" / "watchlist.json").write_text(
        work.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )
    accepted_manifest = json.loads(
        (context.run_root / "requirements" / "REQ-008" / "accepted" / "manifest.json").read_text(encoding="utf-8")
    )
    accepted_content = json.loads(
        (context.run_root / "requirements" / "REQ-008" / "accepted" / "answer_content.json").read_text(encoding="utf-8")
    )
    with pytest.raises(dashboard_assembler.AssemblyError, match="hash mismatch"):
        dashboard_assembler._accepted_visual_widgets(
            context,
            "REQ-008",
            accepted_content,
            accepted_manifest,
            accepted_manifest["content_hash"],
            "c" * 64,
        )

    unsupported = context.run_root / "requirements" / "REQ-001" / "work" / "reviewed.txt"
    unsupported.write_text("reviewed", encoding="utf-8")
    unsupported_manifest = {
        "artifact_progress": {
            "hashes": {"work/reviewed.txt": hashlib.sha256(unsupported.read_bytes()).hexdigest()}
        }
    }
    with pytest.raises(dashboard_assembler.AssemblyError, match="format is unsupported"):
        dashboard_assembler._accepted_visual_artifact(
            context,
            "REQ-001",
            unsupported_manifest,
            "work/reviewed.txt",
        )
    with pytest.raises(dashboard_assembler.AssemblyError, match="reference is invalid"):
        dashboard_assembler._accepted_visual_artifact(
            context,
            "REQ-001",
            unsupported_manifest,
            "../REQ-002/work/reviewed.txt",
        )
    alias = context.run_root / "requirements" / "REQ-001" / "work" / "alias.json"
    alias.symlink_to(unsupported)
    alias_manifest = {
        "artifact_progress": {
            "hashes": {"work/alias.json": hashlib.sha256(unsupported.read_bytes()).hexdigest()}
        }
    }
    with pytest.raises(dashboard_assembler.AssemblyError, match="symlinked"):
        dashboard_assembler._accepted_visual_artifact(
            context,
            "REQ-001",
            alias_manifest,
            "work/alias.json",
        )


def test_accepted_visuals_do_not_require_optional_integration_projection() -> None:
    """Accepted visuals survive absent or malformed optional record hints."""

    accepted = {
        "id": "REQ-008-accepted-visual-01",
        "type": "table",
        "title": "Accepted watchlist",
        "accepted_visual": True,
        "source_bound": True,
        "rows": [{"label": "Customer A", "value": 4}],
    }
    dashboard_fact = {
        "record_id": "REQ-008-fact",
        "record_hash": "a" * 64,
        "kind": "dashboard_fact",
        "payload": {
            "title": "Reviewed technical fact",
            "type": "status_table",
            "rows": [{"label": "status", "value": "reviewed"}],
        },
    }

    widgets = dashboard_assembler._build_widgets(
        "REQ-008",
        {},
        [dashboard_fact],
        accepted_visuals=[{**accepted, "integration_record_ids": []}],
    )
    accepted_widget = next(widget for widget in widgets if widget.get("accepted_visual"))
    assert accepted_widget["rows"] == accepted["rows"]
    assert "audit_limitations" not in accepted_widget

    malformed = {**accepted, "integration_record_id": "bad\\optional-ref"}
    widgets = dashboard_assembler._build_widgets(
        "REQ-008",
        {},
        [dashboard_fact],
        accepted_visuals=[malformed],
    )
    accepted_widget = next(widget for widget in widgets if widget.get("accepted_visual"))
    assert accepted_widget["rows"] == accepted["rows"]
    assert accepted_widget["audit_limitations"] == [
        dashboard_assembler._OPTIONAL_INTEGRATION_PROJECTION_LIMITATION
    ]

    # The no-dashboard-fact consolidation path also traverses metric IDs for
    # KPI-shaped accepted visuals.  A malformed optional hint must be removed
    # before that strict path, rather than leaking a ValueError from
    # ``_widget_identity``.
    malformed_kpi = {
        **accepted,
        "type": "kpi",
        "value": 9,
        "integration_record_id": "bad\\optional-ref",
    }
    widgets = dashboard_assembler._build_widgets(
        "REQ-008",
        {},
        [],
        accepted_visuals=[malformed_kpi],
    )
    accepted_widget = next(widget for widget in widgets if widget.get("accepted_visual"))
    assert "integration_record_id" not in accepted_widget
    assert "integration_record_ids" not in accepted_widget
    assert "bad\\\\optional-ref" not in json.dumps(accepted_widget, sort_keys=True)
    assert accepted_widget["audit_limitations"] == [
        dashboard_assembler._OPTIONAL_INTEGRATION_PROJECTION_LIMITATION
    ]


@pytest.mark.parametrize("with_dashboard_fact", [False, True])
def test_preflight_quarantines_malformed_optional_visual_hints_end_to_end(
    tmp_path: Path,
    with_dashboard_fact: bool,
) -> None:
    """Preflight remains source-bound with or without a dashboard fact."""

    context = RunContext(
        f"RUN-OPTIONAL-VISUAL-{'FACT' if with_dashboard_fact else 'METRIC'}",
        tmp_path / ("with-fact" if with_dashboard_fact else "without-fact"),
    )
    RunLifecycle.create(context, ("REQ-008",), mode="requirement")
    workspace = ItemWorkspace.create(
        context,
        "REQ-008",
        mode="requirement",
        original_text="Accepted visual with optional integration hint.",
    )
    workspace.write_plan({"item_id": "REQ-008", "offline": True})
    workspace.write_draft(
        {
            "item_id": "REQ-008",
            "answer": "Reviewed accepted visual.",
            "visuals": [{
                "type": "kpi",
                "title": "Reviewed count",
                "value": 9,
                "integration_record_id": "bad\\optional-ref",
                "integration_record_ref": "bad\\optional-path",
            }],
        }
    )
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(
        context,
        workspace,
        registry,
        "synthetic-integration",
        invocation_id="inv-optional-visual",
    )
    metric_id = session.add_metric(
        metric_id="metric-optional-visual",
        scope="REQ-008",
        evidence_refs=("answer_content.json",),
        label="Reviewed metric",
        units="records",
        value=9,
        population=10,
    )
    if with_dashboard_fact:
        session.add_dashboard_fact(
            {
                "title": "Reviewed dashboard fact",
                "type": "bar",
                "bars": [{"label": "Reviewed", "value": 9, "size": "100%"}],
            },
            scope="REQ-008",
            evidence_refs=("answer_content.json",),
            fact_id="fact-optional-visual",
        )
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()
    (context.run_root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {"groups": [{"id": "commercial", "title": "Commercial decisions", "requirement_ids": ["REQ-008"]}]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    preflight = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-008"])
    accepted = [
        candidate for candidate in preflight["inventory"]["candidates"]
        if candidate.get("accepted_visual")
    ]
    assert accepted
    assert all("bad\\\\optional" not in json.dumps(candidate, sort_keys=True) for candidate in accepted)
    fixture = json.loads(context.resolve_run_path(preflight["fixture_ref"]).read_text(encoding="utf-8"))
    accepted_widgets = [widget for widget in fixture["widgets"] if widget.get("accepted_visual")]
    assert accepted_widgets
    assert all("integration_record_id" not in widget for widget in accepted_widgets)
    assert all("bad\\\\optional" not in json.dumps(widget, sort_keys=True) for widget in accepted_widgets)
    assert all(
        dashboard_assembler._OPTIONAL_INTEGRATION_PROJECTION_LIMITATION in widget.get("audit_limitations", [])
        for widget in accepted_widgets
    )
    assert metric_id in {
        record.get("record_id")
        for record in fixture.get("audit_records", [])
        if isinstance(record, Mapping)
    }


def test_quantitative_accepted_table_keeps_detail_rows_and_chart_recipe(tmp_path: Path) -> None:
    """A reviewed table can offer a source-bound chart without losing detail."""

    context = RunContext("RUN-ACCEPTED-TABLE-CHART", tmp_path / "run")
    rows = [{"label": "North", "value": 20}, {"label": "South", "value": 14}]
    content = {
        "item_id": "REQ-TABLE",
        "visuals": [{"type": "table", "title": "Reviewed counts", "rows": rows}],
    }
    widget = dashboard_assembler._accepted_visual_widgets(
        context,
        "REQ-TABLE",
        content,
        {"artifact_progress": {"hashes": {}}},
        "b" * 64,
        "a" * 64,
    )[0]
    assert widget["type"] == "table"
    assert widget["rows"] == rows
    assert widget["bars"] == [
        {"label": "North", "value": 20, "size": "100%"},
        {"label": "South", "value": 14, "size": "70%"},
    ]

    chart = dashboard_assembler._chart_map_entry(widget, "REQ-TABLE", {"item_id": "REQ-TABLE"})
    registry_path = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "dashboard_chart_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    recipes = {
        recipe["id"]: recipe
        for recipe in dashboard_assembler._dashboard_runtime().eligible_chart_recipes(widget, chart, registry)
    }
    assert recipes["horizontal_bar"]["eligible"] is True
    assert recipes["table"]["eligible"] is True

    # A Product Agent choice of the chart recipe renders the exact bars, while
    # the unselected source widget still exposes the exact detail rows.
    chart_html = dashboard_renderer._render_visual({**widget, "type": "bar"})
    detail_html = dashboard_renderer._render_site_table(widget)
    assert all(value in chart_html for value in ("North", "South", "20", "14"))
    assert all(value in detail_html for value in ("North", "South", "20", "14"))


def test_source_bound_columns_materialize_lossless_rows_and_chart_projection(tmp_path: Path) -> None:
    """Hash-bound column/matrix sources remain chart-capable without inline rows."""

    context = RunContext("RUN-ACCEPTED-SOURCE-COLUMNS", tmp_path / "run")
    work = context.run_root / "requirements" / "REQ-SOURCE" / "work"
    work.mkdir(parents=True, exist_ok=True)
    reference = "work/quantitative.json"
    source = {
        "columns": ["supplier", "open_qty"],
        "rows": [["North", 20], ["South", 14]],
    }
    source_bytes = (json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    (work / "quantitative.json").write_bytes(source_bytes)
    accepted_manifest = {
        "artifact_progress": {"hashes": {reference: hashlib.sha256(source_bytes).hexdigest()}},
    }
    content = {
        "item_id": "REQ-SOURCE",
        "visuals": [{
            "type": "table",
            "title": "Reviewed open quantity by supplier",
            "source_ref": reference,
            "columns": ["supplier", "open_qty"],
        }],
    }

    widget = dashboard_assembler._accepted_visual_widgets(
        context,
        "REQ-SOURCE",
        content,
        accepted_manifest,
        "b" * 64,
        "a" * 64,
    )[0]
    assert widget["type"] == "table"
    # The detail rows are the exact source cells after column materialization;
    # no rows were supplied inline and no value was recomputed.
    assert widget["rows"] == [
        {"supplier": "North", "open_qty": 20},
        {"supplier": "South", "open_qty": 14},
    ]
    assert widget["bars"] == [
        {
            "supplier": "North",
            "open_qty": 20,
            "label": "North",
            "value": 20,
            "size": "100%",
        },
        {
            "supplier": "South",
            "open_qty": 14,
            "label": "South",
            "value": 14,
            "size": "70%",
        },
    ]
    chart = dashboard_assembler._chart_map_entry(widget, "REQ-SOURCE", {"item_id": "REQ-SOURCE"})
    registry_path = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "dashboard_chart_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    recipes = {
        recipe["id"]: recipe
        for recipe in dashboard_assembler._dashboard_runtime().eligible_chart_recipes(widget, chart, registry)
    }
    assert recipes["horizontal_bar"]["eligible"] is True
    assert recipes["table"]["eligible"] is True


def test_source_bound_column_matrix_shape_errors_fail_closed() -> None:
    visual = {"type": "table", "columns": ["supplier", "open_qty"]}
    malformed_rows, malformed_reason = dashboard_assembler._accepted_visual_source_rows(
        visual,
        {"columns": ["supplier", "open_qty"], "rows": [["North", 20], ["South"]]},
    )
    assert malformed_rows == []
    assert malformed_reason == "accepted visual source rows do not match declared columns"

    ambiguous_rows, ambiguous_reason = dashboard_assembler._accepted_visual_source_rows(
        visual,
        {
            "primary": {"columns": ["supplier", "open_qty"], "rows": [["North", 20]]},
            "secondary": {"columns": ["supplier", "open_qty"], "rows": [["South", 14]]},
        },
    )
    assert ambiguous_rows == []
    assert ambiguous_reason == "accepted visual source table selection is ambiguous"


def test_accepted_evidence_candidates_survive_terminal_integration_without_visual_binding(tmp_path: Path) -> None:
    """Hash-bound accepted evidence remains selectable when integration fails."""

    context = RunContext("RUN-ACCEPTED-EVIDENCE-FAILURE", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-EVIDENCE",), mode="requirement")
    workspace = ItemWorkspace.create(
        context,
        "REQ-EVIDENCE",
        mode="requirement",
        original_text="Review an accepted communication load visual.",
    )
    evidence_ref = "work/evidence.jsonl"
    evidence = [
        {
            "conclusion": "The reviewed department ranking is source-bound.",
            "evidence_id": "REQ-EVIDENCE-RANKING",
            "facts": {
                "ranking": [
                    {"count": 3, "department": "Sales", "share": 0.75},
                    {"count": 1, "department": "Finance", "share": 0.25},
                ],
                "denominator": 4,
            },
            "limitations": ["Synthetic evidence only."],
            "evidence_refs": ["work/department_kpi.json"],
        },
        {
            "conclusion": "Two reviewed handoffs were observed.",
            "evidence_id": "REQ-EVIDENCE-HANDOFF",
            "facts": {"observed_count": 2, "route": "Sales -> Operations"},
            "limitations": ["The sample is sparse."],
        },
    ]
    evidence_bytes = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in evidence
    ).encode("utf-8")
    evidence_path = workspace.item_root / evidence_ref
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(evidence_bytes)
    workspace.write_plan({"item_id": "REQ-EVIDENCE", "offline": True})
    workspace.write_draft(
        {
            "item_id": "REQ-EVIDENCE",
            "answer": "The accepted answer carries a reviewed visual declaration.",
            "evidence_refs": ["REQ-EVIDENCE-RANKING", "REQ-EVIDENCE-HANDOFF"],
            # No source rows are bound to this declaration; it must remain an
            # explicit limitation rather than silently borrowing evidence rows.
            "visuals": [{"type": "horizontal_bar", "title": "Unbound reviewed visual"}],
        }
    )
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json", evidence_ref))
    session = IntegrationSession.create(
        context,
        workspace,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-accepted-evidence-failure",
    )
    session.finalize_technical_failure("integration transport exhausted")
    (context.run_root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {"groups": [{"id": "commercial", "title": "Commercial decisions", "requirement_ids": ["REQ-EVIDENCE"]}]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    preflight = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-EVIDENCE"])
    fixture = json.loads(context.resolve_run_path(preflight["fixture_ref"]).read_text(encoding="utf-8"))
    evidence_widgets = [widget for widget in fixture["widgets"] if widget.get("accepted_evidence")]
    assert {widget["accepted_evidence_id"] for widget in evidence_widgets} == {
        "REQ-EVIDENCE-RANKING",
        "REQ-EVIDENCE-HANDOFF",
    }
    ranking_widgets = [
        widget for widget in evidence_widgets
        if widget["accepted_evidence_id"].endswith("RANKING")
    ]
    ranking = next(
        widget for widget in ranking_widgets
        if widget["accepted_evidence_candidate_kind"] == "table"
    )
    assert ranking["title"] == "Ranking"
    assert ranking["accepted_evidence_pointer"] == "/facts/ranking"
    assert ranking["rows"] == evidence[0]["facts"]["ranking"]
    assert ranking["manager_rows"] == evidence[0]["facts"]["ranking"]
    assert ranking["accepted_evidence_sha256"] == hashlib.sha256(evidence_bytes).hexdigest()
    ranking_facts = next(
        widget for widget in ranking_widgets
        if widget["accepted_evidence_candidate_kind"] == "fact_sheet"
    )
    assert ranking_facts["title"] == "Business metrics"
    assert ranking_facts["accepted_evidence_pointer"] == "/facts"
    assert {tuple(sorted(row.items())) for row in ranking_facts["rows"]} >= {
        (("path", "/facts/denominator"), ("value", 4)),
    }
    assert {tuple(sorted(row.items())) for row in ranking_facts["manager_rows"]} >= {
        (("label", "Denominator"), ("value", 4)),
    }
    scalar = next(widget for widget in evidence_widgets if widget["accepted_evidence_id"].endswith("HANDOFF"))
    assert scalar["accepted_evidence_candidate_kind"] == "fact_sheet"
    assert scalar["title"] == "Business metrics"
    assert scalar["accepted_evidence_pointer"] == "/facts"
    assert {tuple(sorted(row.items())) for row in scalar["rows"]} == {
        (("path", "/facts/observed_count"), ("value", 2)),
        (("path", "/facts/route"), ("value", "Sales -> Operations")),
    }
    # The declaration has no source rows, so its accepted answer is retained
    # as a neutral source-bound decision/limitation card rather than a
    # renderer placeholder; the evidence widgets are the substantive rows.
    accepted_visual = next(widget for widget in fixture["widgets"] if widget.get("accepted_visual"))
    assert accepted_visual.get("presentation_role") == "finding_list"
    assert accepted_visual["rows"] == [{"claim": "No reviewed values were supplied for this view."}]
    assert "Accepted visual specification retained; reviewed values are unavailable for this view." not in json.dumps(accepted_visual)
    assert len(evidence_widgets) > 0

    # The same terminal-failure fixture is accepted by the V2 visual inventory
    # and keeps exact rows plus source binding metadata in each projection.
    inventory = dashboard_assembler.business_presentation_visual_inventory(
        context,
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
    )
    entries = {entry["widget_id"]: entry for entry in inventory["visual_entries"]}
    for widget in evidence_widgets:
        entry = entries[widget["id"]]
        projection = entry["visual_projection"]
        assert projection["rows"]["value"] == widget["rows"]
        assert projection["accepted_evidence_pointer"]["value"] == widget["accepted_evidence_pointer"]
        assert projection["accepted_evidence_ref"]["value"] == widget["accepted_evidence_ref"]
        assert projection["accepted_evidence_sha256"]["value"] == widget["accepted_evidence_sha256"]

    public_inventory = dashboard_assembler.business_presentation_inventory(
        context,
        fixture_ref=preflight["fixture_ref"],
        generation_id="G-0001",
        item_ids=["REQ-EVIDENCE"],
    )
    public_candidates = {
        candidate["widget_id"]: candidate
        for candidate in public_inventory["candidates"]
        if candidate.get("accepted_evidence")
    }
    assert public_candidates
    public_ranking = next(
        candidate
        for candidate in public_candidates.values()
        if candidate.get("accepted_evidence_candidate_kind") == "table"
        and candidate.get("accepted_evidence_pointer") == "/facts/ranking"
    )
    assert public_ranking["source_type"] == "accepted_evidence"
    assert public_ranking["source_bound"] is True
    assert public_ranking["title"] == "Ranking"
    assert public_ranking["rows"] == ranking["rows"]
    assert public_ranking["manager_rows"] == ranking["manager_rows"]
    assert public_ranking["file_sha256"] == ranking["accepted_evidence_sha256"]
    assert public_ranking["display_projection"]["rows"]["value"] == ranking["rows"]
    assert public_ranking["display_projection"]["rows"]["pointer"] == "/payload/facts/ranking"

    def manager_entry(candidate: Mapping[str, Any], **choices: Any) -> dict[str, Any]:
        entry = {
            key: copy.deepcopy(candidate[key])
            for key in (
                "widget_id", "record_id", "requirement_id", "presentation_role",
                "file_sha256", "canonical_payload_sha256", "display_projection",
            )
        }
        entry.update(choices)
        return entry

    handoff_candidate = next(
        candidate
        for candidate in public_candidates.values()
        if candidate.get("accepted_evidence_id", "").endswith("HANDOFF")
    )
    plan_ref = "extensions/G-0001/accepted-evidence-successor.json"
    predecessor = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[manager_entry(handoff_candidate)],
        reviewer_ref="synthetic-product-agent-predecessor",
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
        item_ids=["REQ-EVIDENCE"],
        generation_id="G-0001",
        presentation_plan_ref=plan_ref,
    )
    predecessor_hash = _digest(context.resolve_run_path(plan_ref))
    successor = dashboard_assembler.write_business_presentation_plan_v2(
        context,
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
        previous_plan_ref=plan_ref,
        manager_entries=[manager_entry(public_ranking, recipe_id="table", layout="wide", renderer_type="table")],
        reviewer_ref="synthetic-product-agent-successor",
        presentation_plan_ref=plan_ref,
    )
    successor_hash = hashlib.sha256(dashboard_assembler._canonical_bytes(successor)).hexdigest()
    dashboard_assembler.revise_business_presentation_plan_v2(
        context,
        successor_plan=successor,
        expected_current_plan_sha256=predecessor_hash,
        expected_successor_plan_sha256=successor_hash,
        presentation_plan_ref=plan_ref,
    )
    assembled = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="products/accepted-evidence-successor",
        item_ids=["REQ-EVIDENCE"],
        presentation_plan_ref=plan_ref,
    )
    rendered_site = context.resolve_run_path(assembled["outputs"]["site_ref"])
    manager_pages = [rendered_site / "index.html", *sorted((rendered_site / "domains").glob("*.html"))]
    assert manager_pages
    visible_text = "\n".join(
        html.unescape(
            re.sub(
                r"<[^>]+>",
                " ",
                re.sub(
                    r'<details\b[^>]*class=["\\\']technical-audit["\\\'][^>]*>.*?</details>',
                    "",
                    page.read_text(encoding="utf-8"),
                    flags=re.DOTALL | re.IGNORECASE,
                ),
            )
        )
        for page in manager_pages
    )
    assert "Sales" in visible_text and "Finance" in visible_text
    assert "3" in visible_text and "1" in visible_text
    assert "REQ-" not in visible_text and "EVD-" not in visible_text
    assert "/facts/" not in visible_text
    assert "Technical audit" not in visible_text
    assert "evidence.jsonl" not in visible_text
    assert "source-bound" not in visible_text.lower()
    final_fixture = json.loads(
        context.resolve_run_path(assembled["outputs"]["fixture_ref"]).read_text(encoding="utf-8")
    )
    placeholder = next(widget for widget in final_fixture["widgets"] if widget.get("accepted_visual"))
    assert placeholder["presentation_audience"] == "technical_audit"
    assert placeholder["manager_admission"]["status"] == "audit_only"
    selected = next(widget for widget in final_fixture["widgets"] if widget["id"] == public_ranking["widget_id"])
    assert selected["presentation_audience"] == "business_manager"
    assert selected["manager_admission"]["status"] == "admitted"
    assert final_fixture["manager_visual_widget_ids"] == [public_ranking["widget_id"]]


def test_geometryless_multi_visual_answer_exposes_one_requirement_fallback() -> None:
    """One answer-level finding card backs several empty visual declarations."""

    context = RunContext("RUN-GEOMETRYLESS-MULTI-VISUAL", Path("/tmp/geometryless-multi-visual"))
    content = {
        "item_id": "REQ-MULTI",
        "headline_findings": [
            "Reviewed customers carry the largest exposure.",
            "The concentration remains descriptive.",
        ],
        "scope": "Which customer segments carry the largest reviewed exposure?",
        "limitations": ["The accepted answer has no supplied visual geometry."],
        "visuals": [
            {"type": "pareto", "title": "Customer concentration"},
            {"type": "line", "title": "Concentration stability", "limitation": "No period points were supplied."},
            {"type": "heatmap", "title": "Role overlap", "note": "Visual-specific overlap detail remains audit-only."},
        ],
    }
    accepted = dashboard_assembler._accepted_visual_widgets(
        context,
        "REQ-MULTI",
        content,
        {"artifact_progress": {"hashes": {}}},
        "a" * 64,
        "b" * 64,
    )

    assert len(accepted) == 3
    primary = accepted[0]
    assert primary["presentation_role"] == "finding_list"
    assert primary["rows"] == [
        {"claim": "Reviewed customers carry the largest exposure."},
        {"claim": "The concentration remains descriptive."},
    ]
    assert primary["answer_scope"] == content["scope"]
    assert primary["limitations"] == ["The accepted answer has no supplied visual geometry."]
    assert all(
        widget.get("presentation_role") == "finding_record"
        and widget.get("presentation_tier") == "audit"
        and "rows" not in widget
        for widget in accepted[1:]
    )
    assert accepted[1]["limitations"] == ["No period points were supplied.", content["limitations"][0]]
    assert accepted[2]["limitations"] == ["Visual-specific overlap detail remains audit-only.", content["limitations"][0]]
    assert all(content["limitations"][0] in widget["limitations"] for widget in accepted[1:])
    assert all(
        "Reviewed customers carry the largest exposure." not in json.dumps(widget, ensure_ascii=False)
        for widget in accepted[1:]
    )

    rebuilt = dashboard_assembler._build_widgets(
        "REQ-MULTI",
        content,
        [],
        accepted_visuals=accepted,
    )
    fallback_cards = [widget for widget in rebuilt if widget.get("presentation_role") == "finding_list"]
    assert len(fallback_cards) == 1
    assert all(widget.get("manager_admission", {}).get("status") == "audit_only" for widget in rebuilt[1:])


def test_explicit_v2_selection_cannot_repromote_geometryless_duplicate(tmp_path: Path) -> None:
    """Explicit V2 admission keeps later empty declarations in audit."""

    context = RunContext("RUN-GEOMETRYLESS-V2", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-EMPTY",), mode="requirement")
    workspace = ItemWorkspace.create(
        context,
        "REQ-EMPTY",
        mode="requirement",
        original_text="Review the accepted customer exposure.",
    )
    workspace.write_plan({"item_id": "REQ-EMPTY", "offline": True})
    workspace.write_draft(
        {
            "item_id": "REQ-EMPTY",
            "answer": "Reviewed customer exposure remains descriptive.",
            "headline_findings": ["Customer exposure remains concentrated."],
            "limitations": ["No reviewed visual geometry was supplied."],
            "scope": "Which customer segments carry the largest reviewed exposure?",
            "visuals": [
                {"type": "line", "title": "Exposure trend"},
                {"type": "heatmap", "title": "Exposure overlap"},
            ],
        }
    )
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    session = IntegrationSession.create(
        context,
        workspace,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-geometryless-v2",
    )
    session.finalize_technical_failure("integration transport exhausted")
    (context.run_root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {"groups": [{"id": "commercial", "title": "Commercial decisions", "requirement_ids": ["REQ-EMPTY"]}]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    preflight = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-EMPTY"])
    fixture = json.loads(context.resolve_run_path(preflight["fixture_ref"]).read_text(encoding="utf-8"))
    accepted = [widget for widget in fixture["widgets"] if widget.get("accepted_visual")]
    assert len(accepted) == 2
    assert accepted[1].get("no_geometry_fallback_duplicate") is True
    public_inventory = dashboard_assembler.business_presentation_inventory(
        context,
        fixture_ref=preflight["fixture_ref"],
        generation_id="G-0001",
        item_ids=["REQ-EMPTY"],
    )
    candidates = {candidate["widget_id"]: candidate for candidate in public_inventory["candidates"]}

    def manager_entry(widget_id: str) -> dict[str, Any]:
        candidate = candidates[widget_id]
        return {
            key: copy.deepcopy(candidate.get(key))
            for key in (
                "widget_id", "record_id", "requirement_id", "presentation_role",
                "file_sha256", "canonical_payload_sha256", "display_projection",
            )
        }

    first_id, duplicate_id = (widget["id"] for widget in accepted)
    plan_ref = "extensions/G-0001/geometryless-v2.json"
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[manager_entry(first_id), manager_entry(duplicate_id)],
        reviewer_ref="synthetic-product-agent",
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
        item_ids=["REQ-EMPTY"],
        generation_id="G-0001",
        presentation_plan_ref=plan_ref,
    )
    assert plan["manager_widget_ids"] == [first_id]
    assert plan["manager_visual_widget_ids"] == [first_id]
    assert duplicate_id in plan["audit_visual_widget_ids"]

    assembled = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="products/geometryless-v2",
        item_ids=["REQ-EMPTY"],
        presentation_plan_ref=plan_ref,
    )
    final_fixture = json.loads(
        context.resolve_run_path(assembled["outputs"]["fixture_ref"]).read_text(encoding="utf-8")
    )
    by_id = {widget["id"]: widget for widget in final_fixture["widgets"]}
    assert by_id[first_id]["manager_admission"]["status"] == "admitted"
    assert by_id[duplicate_id]["manager_admission"]["status"] == "audit_only"
    assert final_fixture["manager_widget_ids"] == [first_id]


def test_accepted_evidence_candidates_enumerate_every_nested_table_and_sibling_fact() -> None:
    record = {
        "facts": {
            "ranking": [{"department": "Sales", "count": 3}],
            "nested": {
                "exceptions": [{"code": "late", "count": 1}],
                "note": "reviewed context",
                # A mixed list is a fact-sheet container (not a structural
                # table), so its canonical JSON rendering is exercised below.
                "context": ["EU", {"region": "EU"}],
            },
            "denominator": 4,
            "empty_context": [],
        }
    }

    candidates = dashboard_assembler._accepted_evidence_candidates(record)

    assert [(candidate["kind"], candidate["pointer"]) for candidate in candidates] == [
        ("table", "/facts/ranking"),
        ("table", "/facts/nested/exceptions"),
        ("fact_sheet", "/facts"),
    ]
    assert candidates[0]["rows"] == [{"department": "Sales", "count": 3}]
    assert candidates[1]["rows"] == [{"code": "late", "count": 1}]
    fact_rows = {row["path"]: row["value"] for row in candidates[2]["rows"]}
    assert fact_rows == {
        "/facts/nested/context": ["EU", {"region": "EU"}],
        "/facts/denominator": 4,
        "/facts/empty_context": [],
        "/facts/nested/note": "reviewed context",
    }
    manager_rows = dashboard_assembler._accepted_evidence_manager_rows(candidates[2]["rows"])
    manager_by_label = {row["label"]: row["value"] for row in manager_rows}
    assert manager_by_label["Denominator"] == 4
    assert manager_by_label["Nested · Context"] == '["EU",{"region":"EU"}]'
    assert all("/facts/" not in row["label"] for row in manager_rows)


def test_duplicate_accepted_evidence_ids_are_omitted_without_blocking_unique_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-DUPLICATE-ACCEPTED-EVIDENCE", tmp_path / "run")
    duplicate_records = [
        {"evidence_id": "ambiguous", "facts": {"rows": [{"value": 1}]}},
        {"evidence_id": "ambiguous", "facts": {"rows": [{"value": 2}]}},
        {"evidence_id": "unique", "facts": {"count": 3}},
    ]
    monkeypatch.setattr(
        dashboard_assembler,
        "_accepted_visual_artifact",
        lambda *_args, **_kwargs: (duplicate_records, "a" * 64),
    )

    widgets = dashboard_assembler._accepted_evidence_widgets(
        context,
        "REQ-DUPLICATE",
        {"evidence_refs": ["work/evidence.jsonl"]},
        {"artifact_progress": {"hashes": {"work/evidence.jsonl": "a" * 64}}},
        "b" * 64,
        "c" * 64,
    )

    assert widgets
    assert {widget["accepted_evidence_id"] for widget in widgets} == {"unique"}
    assert all(widget["accepted_evidence_id"] != "ambiguous" for widget in widgets)


def test_domain_label_uses_exact_markdown_scope_headings_without_dictionary() -> None:
    group = {
        "title": "A long rationale sentence that is not a presentation label",
        "scope": "**1 Data readiness**\n**2 Ontology**\nFurther rationale stays descriptive.",
    }
    flow_defs = [{"title": "Flow fallback", "scope": ""}]

    assert dashboard_assembler._presentation_domain_title(group, flow_defs) == "Data readiness · Ontology"
    assert dashboard_assembler._presentation_heading_labels(["**Important:** operational rationale follows."]) == []
    assert dashboard_assembler._presentation_heading_labels(["**2Ontology**"]) == ["Ontology"]
    assert dashboard_assembler._presentation_heading_labels(["**2026 Outlook**"]) == ["2026 Outlook"]


def test_real_shape_empty_group_scope_uses_item_original_headings() -> None:
    originals = {
        "REQ-001": "**1 Data readiness**\n\nОпредели фактическую схему и покрытие.",
        "REQ-002": "**2 Ontology**\n\nПострой проверенную онтологию.",
    }
    titles = [
        dashboard_assembler._manager_requirement_title(item_id, {}, original, [])
        for item_id, original in originals.items()
    ]
    assert titles == ["Data readiness", "Ontology"]
    assert dashboard_assembler._presentation_domain_title(
        {"title": "Портфельная rationale", "scope": ""},
        [{"title": title, "scope": ""} for title in titles],
    ) == "Data readiness · Ontology"


def test_delivery_table_aliases_keep_exact_dimension_ids_and_offer_median_chart() -> None:
    """Live-shaped KPI tables retain IDs while exposing one exact measure."""

    registry_path = ROOT / "skills" / "auto-foundry-agentic-e2e" / "assets" / "dashboard_chart_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    cases = [
        (
            "carrier",
            [{"carrier": "DB Schenker", "median_delay_days": 3.0}, {"carrier": "DHL", "median_delay_days": 2.0}],
            ["carrier", "n", "coverage_of_documented_pct", "on_time_rate_pct", "median_delay_days", "p90_delay_days"],
            "DB Schenker",
        ),
        (
            "derived_plant",
            [{"derived_plant": "0001", "median_delay_days": 3.0}, {"derived_plant": "0310", "median_delay_days": 2.0}],
            ["derived_plant", "n", "coverage_of_documented_pct", "on_time_rate_pct", "median_delay_days", "p90_delay_days"],
            "0001",
        ),
        (
            "customer",
            [
                {"customer_id": "0035325960", "customer_name": "Smith Industrial LLC", "median_delay_days": 5.0},
                {"customer_id": "0086140745", "customer_name": "National Wholesale Company", "median_delay_days": 3.0},
            ],
            ["customer_id", "customer_name", "n", "coverage_of_documented_pct", "on_time_rate_pct", "median_delay_days", "p90_delay_days"],
            "Smith Industrial LLC",
        ),
    ]
    for dimension, source_rows, fields, first_label in cases:
        visual = {"type": "table", "fields": fields, "title": f"Delivery performance by {dimension}"}
        # The artifact also carries other list-of-object metadata arrays.  The
        # canonical payload.rows path is the only source table and is selected
        # without reading or recalculating any source bytes.
        artifact = {
            "metric_definitions": [{"name": "observations"}],
            "payload": {"rows": source_rows},
            "tables": [{"name": "kpi", "row_count": len(source_rows)}],
        }
        rows, reason = dashboard_assembler._accepted_visual_source_rows(visual, artifact)
        assert reason == "accepted visual source does not expose all requested fields"
        assert rows == source_rows
        projection = dashboard_assembler._accepted_visual_table_chart_projection(visual, rows)
        assert projection is not None and projection[0] == "bar"
        chart_rows = projection[1]
        assert chart_rows[0]["label"] == first_label
        assert chart_rows[0]["value"] == source_rows[0]["median_delay_days"]
        assert chart_rows[0]["size"] == "100%"
        if dimension == "customer":
            assert chart_rows[0]["customer_id"] == source_rows[0]["customer_id"]
        widget = {"id": f"delivery-{dimension}", "type": "table", "rows": rows, "bars": chart_rows}
        chart = dashboard_assembler._chart_map_entry(widget, "REQ-006", {"item_id": "REQ-006"})
        recipes = {
            recipe["id"]: recipe
            for recipe in dashboard_assembler._dashboard_runtime().eligible_chart_recipes(widget, chart, registry)
        }
        assert recipes["horizontal_bar"]["eligible"] is True
        assert recipes["table"]["eligible"] is True


def test_terminal_integration_failure_visual_flows_through_preflight_plan_and_root_assembly(tmp_path: Path) -> None:
    """Accepted REQ-008 visuals remain selectable after terminal integration failure."""

    context = RunContext("RUN-ACCEPTED-VISUAL-FAILURE", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-008",), mode="requirement")
    workspace = ItemWorkspace.create(
        context,
        "REQ-008",
        mode="requirement",
        original_text="Review customer order changes.",
    )
    source_ref = "work/watchlist.jsonl"
    source_bytes = (
        b'{"customer_name":"Customer A","baseline_orders":20,"recent_orders":8,"order_coverage":"80%"}\n'
        b'{"customer_name":"Customer B","baseline_orders":14,"recent_orders":7,"order_coverage":"70%"}\n'
    )
    (workspace.item_root / source_ref).parent.mkdir(parents=True, exist_ok=True)
    (workspace.item_root / source_ref).write_bytes(source_bytes)
    workspace.write_plan({"item_id": "REQ-008", "offline": True})
    workspace.write_draft(
        {
            "item_id": "REQ-008",
            "answer": "Reviewed customer watchlist.",
            "headline_findings": ["Customer A shows the largest reviewed order decline."],
            "limitations": ["Accepted source remains requirement-local and reviewed."],
            "evidence_refs": [source_ref],
            "visuals": [
                {
                    "type": "paired_bar",
                    "title": "Baseline versus recent orders",
                    "evidence_ref": source_ref,
                    "dimension": "customer_name",
                    "series": ["baseline_orders", "recent_orders"],
                    "scale_groups": {
                        "baseline": {
                            "series": ["baseline_orders"],
                            "scale_domain": "orders per baseline period",
                            "scale_basis": "accepted source-local baseline orders",
                        },
                        "recent": {
                            "series": ["recent_orders"],
                            "scale_domain": "orders per recent period",
                            "scale_basis": "accepted source-local recent orders",
                        },
                    },
                },
                {
                    "type": "table",
                    "title": "Source readiness matrix",
                    "evidence_ref": source_ref,
                    "columns": ["customer_name", "baseline_orders", "recent_orders"],
                    "rows": [
                        {"customer_name": "Customer A", "baseline_orders": 20, "recent_orders": 8},
                        {"customer_name": "Customer B", "baseline_orders": 14, "recent_orders": 7},
                    ],
                },
                {
                    "type": "table",
                    "title": "Order coverage",
                    "evidence_ref": source_ref,
                    "columns": ["customer_name", "order_coverage"],
                    "rows": [
                        {"customer_name": "Customer A", "order_coverage": "80%"},
                        {"customer_name": "Customer B", "order_coverage": "70%"},
                    ],
                },
                {
                    "type": "table",
                    "title": "Execution traces",
                    "evidence_ref": source_ref,
                    "columns": ["customer_name", "baseline_orders"],
                    "rows": [
                        {"customer_name": "Customer A", "baseline_orders": 20},
                        {"customer_name": "Customer B", "baseline_orders": 14},
                    ],
                },
                {
                    "type": "table",
                    "title": "Files inventory",
                    "evidence_ref": source_ref,
                    "columns": ["customer_name", "recent_orders"],
                    "rows": [
                        {"customer_name": "Customer A", "recent_orders": 8},
                        {"customer_name": "Customer B", "recent_orders": 7},
                    ],
                },
            ],
        }
    )
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json", source_ref))
    session = IntegrationSession.create(
        context,
        workspace,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-req008-failure",
    )
    session.finalize_technical_failure("integration transport exhausted",)
    (context.run_root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {"groups": [{"id": "commercial", "title": "Commercial decisions", "requirement_ids": ["REQ-008"]}]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    preflight = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-008"])
    candidate = next(
        value for value in preflight["inventory"]["candidates"] if value.get("accepted_visual")
        and value.get("accepted_visual_index") == 0
    )
    technical_candidate = next(
        value for value in preflight["inventory"]["candidates"]
        if value.get("accepted_visual") and value.get("accepted_visual_index") == 1
    )
    business_candidate = next(
        value for value in preflight["inventory"]["candidates"]
        if value.get("accepted_visual") and value.get("accepted_visual_index") == 2
    )
    traces_candidate = next(
        value for value in preflight["inventory"]["candidates"]
        if value.get("accepted_visual") and value.get("accepted_visual_index") == 3
    )
    files_candidate = next(
        value for value in preflight["inventory"]["candidates"]
        if value.get("accepted_visual") and value.get("accepted_visual_index") == 4
    )
    assert candidate["accepted_visual_pointer"] == "/accepted/visuals/0"
    assert candidate["accepted_artifact_ref"] == source_ref
    plan_ref = "extensions/G-0001/business_presentation_plan.json"
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        # The Product Agent chooses the two decision-useful accepted visuals;
        # omitted candidates remain available only on the audit surface.  The
        # assembler/renderer do not reinterpret their titles or row content.
        manager_entries=[
            candidate,
            business_candidate,
        ],
        reviewer_ref="synthetic-product-agent",
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
        item_ids=["REQ-008"],
        generation_id="G-0001",
        presentation_plan_ref=plan_ref,
    )
    assert plan["manager_visual_widget_ids"] == [candidate["widget_id"], business_candidate["widget_id"]]
    assert technical_candidate["widget_id"] in plan["audit_visual_widget_ids"]
    assert traces_candidate["widget_id"] in plan["audit_visual_widget_ids"]
    assert files_candidate["widget_id"] in plan["audit_visual_widget_ids"]
    assembled = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="products/accepted-failure-dashboard",
        item_ids=["REQ-008"],
        presentation_plan_ref=plan_ref,
    )
    fixture = json.loads(
        context.resolve_run_path(assembled["outputs"]["fixture_ref"]).read_text(encoding="utf-8")
    )
    accepted_widget = next(
        widget for widget in fixture["widgets"] if widget.get("accepted_visual")
    )
    assert accepted_widget["type"] == "grouped_bar"
    assert accepted_widget["bars"][1]["series"][0]["size"] == "70%"
    assert accepted_widget["scale_groups"]["baseline"]["series"] == ["baseline_orders"]
    assert accepted_widget["bars"][1]["series"][1]["size"] == "87.5%"
    technical_widget = next(
        widget for widget in fixture["widgets"] if widget["id"] == technical_candidate["widget_id"]
    )
    traces_widget = next(
        widget for widget in fixture["widgets"] if widget["id"] == traces_candidate["widget_id"]
    )
    files_widget = next(
        widget for widget in fixture["widgets"] if widget["id"] == files_candidate["widget_id"]
    )
    business_widget = next(
        widget for widget in fixture["widgets"] if widget["id"] == business_candidate["widget_id"]
    )
    assert technical_widget["manager_admission"]["status"] == "audit_only"
    assert technical_widget["presentation_audience"] == "technical_audit"
    assert traces_widget["manager_admission"]["status"] == "audit_only"
    assert traces_widget["presentation_audience"] == "technical_audit"
    assert files_widget["manager_admission"]["status"] == "audit_only"
    assert files_widget["presentation_audience"] == "technical_audit"
    assert business_widget["manager_admission"]["status"] == "admitted"
    assert business_widget["presentation_audience"] == "business_manager"
    assert business_widget["rows"] == [
        {"customer_name": "Customer A", "order_coverage": "80%"},
        {"customer_name": "Customer B", "order_coverage": "70%"},
    ]
    assert fixture["failed_items"][0]["item_id"] == "REQ-008"
    site_root = context.resolve_run_path(assembled["outputs"]["site_ref"])
    chart_map = json.loads(
        context.resolve_run_path(assembled["outputs"]["chart_map_ref"]).read_text(encoding="utf-8")
    )
    chart_entry = next(entry for entry in chart_map["charts"] if entry["id"] == accepted_widget["id"])
    assert chart_entry["fields_or_values_used"]["scale_groups"] == accepted_widget["scale_groups"]
    html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(site_root.rglob("*.html"))
    )
    manager_html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [site_root / "index.html", *sorted((site_root / "domains").glob("*.html"))]
    )
    assert "Customer A" in html
    assert "Order coverage" in manager_html and "80%" in manager_html
    assert "Source readiness matrix" not in manager_html
    assert "Execution traces" not in manager_html
    assert "Files inventory" not in manager_html
    assert html.count("scale-group-panel") >= 2
    assert "Baseline" in html and "Recent" in html
    assert "orders per baseline period" in html
    assert "orders per recent period" in html
    assert "20" in html and "8" in html
    assert dashboard_renderer._FAILED_ITEM_LIMITATION in html
    assert "integration transport exhausted" not in html


def test_root_assembly_recovers_keyboard_interrupt_and_orphan_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed root build releases its lock and retries past old staging."""

    source = tmp_path / "source"
    context = _seed_run(source)
    original_replace_prefix = dashboard_assembler._replace_prefix

    def interrupt_before_publish(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(dashboard_assembler, "_replace_prefix", interrupt_before_publish)
    with pytest.raises(KeyboardInterrupt):
        dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    staging = source / "products" / ".repro_dashboard_v4.staging"
    assert not staging.exists()

    monkeypatch.setattr(dashboard_assembler, "_replace_prefix", original_replace_prefix)
    recovered = dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    assert recovered["status"] == "complete"
    assert (source / "products" / "repro_dashboard_v4" / "build_receipt.json").is_file()

    # Simulate a hard process death after the source-bound files were staged.
    # The final namespace is intentionally different, so the recovery path
    # must remove only its exact orphan before rebuilding.
    source_output = source / "products" / "repro_dashboard_v4"
    orphan = source / "products" / ".orphan-target.staging"
    orphan.mkdir()
    for name in (
        "dashboard_fixture_v4.json",
        "dashboard_chart_map_v4.json",
        "dashboard_chart_registry_v4.json",
        "dashboard_blueprint_v2.json",
    ):
        shutil.copy2(source_output / name, orphan / name)
    retried = dashboard_assembler.assemble_dashboard(context, output_dir="orphan-target")
    assert retried["status"] == "complete"
    assert not orphan.exists()


def test_root_assembly_lock_serializes_same_namespace_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent root callers share one staging owner and converge."""

    source = tmp_path / "source"
    context = _seed_run(source)
    entered = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    max_active = 0
    calls = 0
    original_build_registry = dashboard_assembler._build_registry

    def blocking_build_registry(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal active, max_active, calls
        with guard:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        entered.set()
        try:
            if not release.wait(timeout=10):
                raise RuntimeError("timed out waiting for concurrent assembly")
            return original_build_registry(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(dashboard_assembler, "_build_registry", blocking_build_registry)
    receipts: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def build() -> None:
        try:
            receipts.append(dashboard_assembler.assemble_dashboard(context, output_dir="concurrent-root"))
        except BaseException as exc:  # pragma: no cover - assertion below reports any failure
            failures.append(exc)

    first = threading.Thread(target=build)
    second = threading.Thread(target=build)
    first.start()
    assert entered.wait(timeout=10)
    second.start()
    # The second thread must wait on the external lock rather than entering
    # the same deterministic staging namespace concurrently.
    time.sleep(0.1)
    with guard:
        assert calls == 1
        assert max_active == 1
    release.set()
    first.join(timeout=30)
    second.join(timeout=30)
    assert not first.is_alive() and not second.is_alive()
    assert not failures
    assert len(receipts) == 2
    assert receipts[0] == receipts[1]
    assert not (source / "products" / ".concurrent-root.staging").exists()


def test_publish_staged_output_restores_old_namespace_on_first_rename_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    output_root.mkdir()
    staging_root.mkdir()
    (output_root / "value.txt").write_text("old\n", encoding="utf-8")
    (staging_root / "value.txt").write_text("new\n", encoding="utf-8")
    original_replace = dashboard_assembler.os.replace
    calls = 0

    def interrupt_after_old_move(source: str | bytes, target: str | bytes) -> None:
        nonlocal calls
        calls += 1
        original_replace(source, target)
        if calls == 1:
            raise KeyboardInterrupt()

    monkeypatch.setattr(dashboard_assembler.os, "replace", interrupt_after_old_move)
    with pytest.raises(KeyboardInterrupt):
        dashboard_assembler._publish_staged_output(staging_root, output_root)

    assert (output_root / "value.txt").read_text(encoding="utf-8") == "old\n"
    assert staging_root.is_dir()
    assert not (tmp_path / ".output.previous").exists()


def test_publish_staged_output_keeps_new_namespace_when_second_rename_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    output_root.mkdir()
    staging_root.mkdir()
    (output_root / "value.txt").write_text("old\n", encoding="utf-8")
    (staging_root / "value.txt").write_text("new\n", encoding="utf-8")
    original_replace = dashboard_assembler.os.replace
    calls = 0

    def interrupt_after_new_move(source: str | bytes, target: str | bytes) -> None:
        nonlocal calls
        calls += 1
        original_replace(source, target)
        if calls == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(dashboard_assembler.os, "replace", interrupt_after_new_move)
    with pytest.raises(KeyboardInterrupt):
        dashboard_assembler._publish_staged_output(staging_root, output_root)

    assert (output_root / "value.txt").read_text(encoding="utf-8") == "new\n"
    assert not staging_root.exists()
    assert not (tmp_path / ".output.previous").exists()


def test_root_assembly_lock_contends_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "products" / ".root.assembly.lock"
    entered_path = tmp_path / "entered"
    release_path = tmp_path / "release"
    acquired_path = tmp_path / "acquired"
    process_context = multiprocessing.get_context("fork")
    holder = process_context.Process(
        target=_hold_root_assembly_lock,
        args=((str(lock_path), str(entered_path), str(release_path)),),
    )
    contender = process_context.Process(
        target=_acquire_root_assembly_lock,
        args=((str(lock_path), str(acquired_path)),),
    )
    holder.start()
    try:
        deadline = time.monotonic() + 10
        while not entered_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered_path.is_file()
        contender.start()
        time.sleep(0.1)
        assert not acquired_path.exists()
        release_path.write_text("release\n", encoding="utf-8")
        contender.join(timeout=10)
        holder.join(timeout=10)
        assert contender.exitcode == 0
        assert holder.exitcode == 0
        assert acquired_path.is_file()
    finally:
        if holder.is_alive():
            release_path.write_text("release\n", encoding="utf-8")
            holder.join(timeout=5)
        if contender.is_alive():
            contender.terminate()
            contender.join(timeout=5)


def _commit_preview_item(
    context: RunContext,
    registry: PreparedAssetRegistry,
    item_id: str,
    *,
    invocation_id: str,
    value: int,
) -> ItemWorkspace:
    """Commit one small synthetic item for preview-refresh coverage."""

    workspace = ItemWorkspace.create(
        context,
        item_id,
        mode="requirement",
        original_text=f"synthetic requirement {item_id}",
    )
    workspace.write_plan({"item_id": item_id, "offline": True})
    workspace.write_draft({"item_id": item_id, "answer": f"bounded-{item_id}", "limitations": ["synthetic only"]})
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    session = IntegrationSession.create(context, workspace, registry, "synthetic-integration", invocation_id=invocation_id)
    session.add_metric(
        metric_id=f"metric-{item_id.lower()}",
        scope=item_id,
        evidence_refs=("answer_content.json",),
        label=f"Reviewed {item_id}",
        units="records",
        value=value,
        population=10,
    )
    if session.fidelity_result is None:
        session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()
    return workspace


def _seed_preview_refresh_run(root: Path) -> tuple[RunContext, PreparedAssetRegistry]:
    """Create two lifecycle items while leaving the second uncommitted."""

    context = RunContext("RUN-ASSEMBLER-PREVIEW-REFRESH", root)
    RunLifecycle.create(context, ("REQ-A", "REQ-B"), mode="requirement")
    registry = PreparedAssetRegistry(context)
    _commit_preview_item(context, registry, "REQ-A", invocation_id="inv-preview-a", value=7)
    ItemWorkspace.create(
        context,
        "REQ-B",
        mode="requirement",
        original_text="synthetic requirement REQ-B",
    )
    (root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "id": "commercial",
                        "title": "Commercial decisions",
                        "requirement_ids": ["REQ-A", "REQ-B"],
                    }
                ]
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return context, registry


def _seed_all_terminal_failure_run(root: Path, item_ids: tuple[str, ...]) -> RunContext:
    """Create a requirement run whose selected inputs have no accepted data."""

    context = RunContext("RUN-ASSEMBLER-ALL-FAILED", root)
    RunLifecycle.create(context, item_ids, mode="requirement")
    for item_id in item_ids:
        workspace = ItemWorkspace.create(
            context,
            item_id,
            mode="requirement",
            original_text=f"synthetic failed requirement {item_id}",
        )
        workspace.technical_failure("analysis transport exhausted", recovery_exhausted=True)
    (root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "id": "failed",
                        "title": "Failed requirements",
                        "requirement_ids": list(item_ids),
                    }
                ]
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return context


@pytest.mark.parametrize("item_ids", [("REQ-FAILED",), ("REQ-FAILED-A", "REQ-FAILED-B")])
def test_all_terminal_failures_have_source_bound_preflight_and_nonblank_dashboard(
    tmp_path: Path,
    item_ids: tuple[str, ...],
) -> None:
    """Technical failures can produce a planned limited dashboard without fake metrics."""

    context = _seed_all_terminal_failure_run(tmp_path / "all-failed", item_ids)
    preflight = dashboard_assembler.business_presentation_preflight(context, item_ids=list(item_ids))
    assert preflight["item_ids"] == list(item_ids)
    assert preflight["inventory"]["schema_version"] == dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA
    assert preflight["inventory"]["candidates"]
    limited_candidate = next(
        candidate
        for candidate in preflight["inventory"]["candidates"]
        if candidate.get("widget_id") == "business-availability-empty-state"
    )
    plan_ref = "extensions/G-0001/business_presentation_plan.json"
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[limited_candidate],
        reviewer_ref="tests/all-terminal",
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
        item_ids=list(item_ids),
        generation_id="G-0001",
        presentation_plan_ref=plan_ref,
    )
    assert plan["manager_widget_ids"] == ["business-availability-empty-state"]
    assert plan["manager_visual_widget_ids"] == ["business-availability-empty-state"]
    assert plan["audit_visual_widget_ids"] == []
    assembled = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="generations/G-0001/preview",
        item_ids=list(item_ids),
        presentation_plan_ref=plan_ref,
    )
    fixture = json.loads(context.resolve_run_path(assembled["outputs"]["fixture_ref"]).read_text(encoding="utf-8"))
    assert fixture["limited_dashboard"] is True
    assert fixture["limited_dashboard_reason"] == "all_selected_requirements_terminal"
    assert len(fixture["widgets"]) == 1
    widget = fixture["widgets"][0]
    assert widget["limited_empty_state"] is True
    assert widget["rows"] == [{"status": "No accepted business visual is available for this run."}]
    assert all(ref.startswith("requirements/") and ref.endswith("/accepted/manifest.json") for ref in widget["evidence_refs"])
    assert all("item_state.json" not in ref and "run_state.json" not in ref for ref in widget["evidence_refs"] + widget["trace_refs"])
    assert fixture["presentation_plan_ref"] == plan_ref
    assert fixture["manager_widget_ids"] == plan["manager_widget_ids"]
    assert fixture["manager_visual_widget_ids"] == plan["manager_visual_widget_ids"]
    assert fixture["audit_visual_widget_ids"] == plan["audit_visual_widget_ids"]
    assert fixture["manager_entries"] == plan["manager_entries"]
    assert fixture["visual_entries"] == plan["visual_entries"]
    blueprint = json.loads(
        context.resolve_run_path(assembled["outputs"]["blueprint_ref"]).read_text(encoding="utf-8")
    )
    assert blueprint["review_status"] == "Preview"
    assert blueprint["source_bindings"]["presentation_plan_ref"] == plan_ref
    selected_visual = next(
        visual for visual in blueprint["visuals"] if visual["id"] == "business-availability-empty-state"
    )
    assert selected_visual["recipe_id"] == limited_candidate["recipe_id"]
    assert selected_visual["layout"] == limited_candidate["layout"]
    assert selected_visual["renderer_type"] == limited_candidate["renderer_type"]
    receipt = json.loads(
        context.resolve_run_path(assembled["outputs"]["receipt_ref"]).read_text(encoding="utf-8")
    )
    assert receipt["presentation_plan_ref"] == plan_ref
    assert receipt["presentation_plan_sha256"] == _digest(context.resolve_run_path(plan_ref))
    assert receipt["outputs"]["blueprint_ref"] == "products/generations/G-0001/preview/dashboard_blueprint_v2.json"
    assert receipt["output_hashes"]["blueprint_sha256"] == hashlib.sha256(
        context.resolve_run_path(assembled["outputs"]["blueprint_ref"]).read_bytes()
    ).hexdigest()
    html = context.resolve_run_path(assembled["outputs"]["site_ref"] + "/domains/business-availability.html").read_text(encoding="utf-8")
    assert "No accepted business visual is available for this run." in html
    assert "<article" in html
    assert 'data-runtime-card' in html


def test_canonical_preview_refresh_allows_new_committed_input_but_rejects_tamper_and_ordinary_drift(tmp_path: Path) -> None:
    """Only the lifecycle-owned preview namespace may refresh in place."""

    source = tmp_path / "preview"
    context, registry = _seed_preview_refresh_run(source)
    preview_dir = "generations/G-0001/preview"
    first = dashboard_assembler.assemble_dashboard(context, output_dir=preview_dir, item_ids=["REQ-A"])
    first_root = source / "products" / "generations" / "G-0001" / "preview"
    first_blueprint = json.loads((first_root / dashboard_assembler.BLUEPRINT_FILENAME).read_text(encoding="utf-8"))

    _commit_preview_item(context, registry, "REQ-B", invocation_id="inv-preview-b", value=11)
    refreshed = dashboard_assembler.assemble_dashboard(context, output_dir=preview_dir, item_ids=["REQ-A", "REQ-B"])
    refreshed_root = source / "products" / "generations" / "G-0001" / "preview"
    refreshed_fixture = json.loads((refreshed_root / "dashboard_fixture_v4.json").read_text(encoding="utf-8"))
    refreshed_blueprint = json.loads((refreshed_root / dashboard_assembler.BLUEPRINT_FILENAME).read_text(encoding="utf-8"))
    assert [entry["item_id"] for entry in refreshed["input_items"]] == ["REQ-A", "REQ-B"]
    assert "REQ-B" in {widget["requirement_id"] for widget in refreshed_fixture["widgets"]}
    assert first["output_hashes"] != refreshed["output_hashes"]
    assert first_blueprint["datasets"] != refreshed_blueprint["datasets"]
    assert refreshed["outputs"]["blueprint_ref"] == "products/generations/G-0001/preview/dashboard_blueprint_v2.json"

    tampered_source = tmp_path / "tampered-preview"
    tampered_context, tampered_registry = _seed_preview_refresh_run(tampered_source)
    dashboard_assembler.assemble_dashboard(tampered_context, output_dir=preview_dir, item_ids=["REQ-A"])
    tampered_root = tampered_source / "products" / "generations" / "G-0001" / "preview"
    tampered_site = tampered_root / "site" / "index.html"
    tampered_site.write_text(tampered_site.read_text(encoding="utf-8") + "\n<!-- tampered -->\n", encoding="utf-8")
    tampered_bytes = tampered_site.read_bytes()
    _commit_preview_item(tampered_context, tampered_registry, "REQ-B", invocation_id="inv-tampered-b", value=11)
    with pytest.raises(dashboard_assembler.AssemblyError, match="existing output site file hash mismatch"):
        dashboard_assembler.assemble_dashboard(tampered_context, output_dir=preview_dir, item_ids=["REQ-A", "REQ-B"])
    assert tampered_site.read_bytes() == tampered_bytes

    ordinary_source = tmp_path / "ordinary-output"
    ordinary_context, ordinary_registry = _seed_preview_refresh_run(ordinary_source)
    dashboard_assembler.assemble_dashboard(ordinary_context, output_dir="ordinary-dashboard", item_ids=["REQ-A"])
    _commit_preview_item(ordinary_context, ordinary_registry, "REQ-B", invocation_id="inv-ordinary-b", value=11)
    with pytest.raises(dashboard_assembler.AssemblyError, match="existing output namespace input hashes"):
        dashboard_assembler.assemble_dashboard(ordinary_context, output_dir="ordinary-dashboard", item_ids=["REQ-A", "REQ-B"])


def test_preflight_inventory_is_non_rendering_and_refreshable_for_preview_selection(tmp_path: Path) -> None:
    """A first preview has a bounded source inventory before final assembly."""

    source = tmp_path / "preflight"
    context, registry = _seed_preview_refresh_run(source)
    first = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])
    assert first["fixture_ref"] == "extensions/G-0001/dashboard_preflight/dashboard_fixture_v4.json"
    assert first["chart_map_ref"] == "extensions/G-0001/dashboard_preflight/dashboard_chart_map_v4.json"
    assert first["inventory"]["schema_version"] == dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA
    assert not (source / "products" / "generations" / "G-0001" / ".dashboard-preflight").exists()
    first_hash = first["fixture_sha256"]
    assert first["inventory"]["candidates"]
    second = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])
    assert second["fixture_sha256"] == first_hash

    # Author a valid V2 plan against the first preflight frontier.  Its bytes
    # are intentionally kept so the refresh regression can prove that stale
    # source bindings are rejected rather than silently reused.
    first_candidate = copy.deepcopy(first["inventory"]["candidates"][0])
    first_plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[first_candidate],
        reviewer_ref="synthetic-reviewer",
        fixture_ref=first["fixture_ref"],
        chart_map_ref=first["chart_map_ref"],
        item_ids=["REQ-A"],
        generation_id="G-0001",
        presentation_plan_ref="extensions/G-0001/business_presentation_plan.json",
    )
    assert first_plan["schema_version"] == dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA
    stale_plan_bytes = context.resolve_run_path("extensions/G-0001/business_presentation_plan.json").read_bytes()

    _commit_preview_item(context, registry, "REQ-B", invocation_id="inv-preflight-b", value=11)
    refreshed = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A", "REQ-B"])
    assert refreshed["fixture_sha256"] != first_hash
    assert refreshed["item_ids"] == ["REQ-A", "REQ-B"]
    manifest = json.loads(
        context.resolve_run_path("extensions/G-0001/dashboard_preflight/preflight_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["input_fingerprint"] == refreshed["input_fingerprint"]

    stale_ref = "extensions/G-0001/stale_business_presentation_plan.json"
    stale_path = context.resolve_run_path(stale_ref)
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(stale_plan_bytes)
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="input bindings drifted"):
        dashboard_assembler.assemble_dashboard(
            context,
            output_dir="generations/G-0001/preview",
            item_ids=["REQ-A", "REQ-B"],
            presentation_plan_ref=stale_ref,
        )

    # The Product Agent's explicit V2 choice is authored from the preflight
    # inventory, then consumed once by canonical assembly.  Blueprint bytes
    # are present before rendering (the renderer validates that binding).
    candidate = copy.deepcopy(refreshed["inventory"]["candidates"][0])
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[candidate],
        reviewer_ref="synthetic-reviewer",
        fixture_ref=refreshed["fixture_ref"],
        chart_map_ref=refreshed["chart_map_ref"],
        item_ids=["REQ-A", "REQ-B"],
        generation_id="G-0001",
        presentation_plan_ref="extensions/G-0001/business_presentation_plan.json",
    )
    assert plan["schema_version"] == dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA
    assembled = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="generations/G-0001/preview",
        item_ids=["REQ-A", "REQ-B"],
        presentation_plan_ref="extensions/G-0001/business_presentation_plan.json",
    )
    blueprint = json.loads(
        context.resolve_run_path(assembled["outputs"]["blueprint_ref"]).read_text(encoding="utf-8")
    )
    selected = next(value for value in blueprint["visuals"] if value["id"] == candidate["widget_id"])
    assert selected["recipe_id"] == candidate["recipe_id"]
    assert selected["layout"] == candidate["layout"]
    assert selected["renderer_type"] == candidate["renderer_type"]


def test_preflight_item_order_is_canonical_and_idempotent(tmp_path: Path) -> None:
    """Caller permutations produce one stable source-inventory namespace."""

    source = tmp_path / "preflight-permutation"
    context, registry = _seed_preview_refresh_run(source)
    _commit_preview_item(context, registry, "REQ-B", invocation_id="inv-preflight-permutation-b", value=11)

    first = dashboard_assembler.business_presentation_preflight(
        context,
        item_ids=["REQ-B", "REQ-A"],
    )
    manifest_path = context.resolve_run_path(
        "extensions/G-0001/dashboard_preflight/preflight_manifest.json"
    )
    fixture_path = context.resolve_run_path(first["fixture_ref"])
    manifest_before = manifest_path.read_bytes()
    fixture_before = fixture_path.read_bytes()

    second = dashboard_assembler.business_presentation_preflight(
        context,
        item_ids=["REQ-A", "REQ-B"],
    )
    assert first["item_ids"] == second["item_ids"] == ["REQ-A", "REQ-B"]
    assert first["input_items"] == second["input_items"]
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["fixture_sha256"] == second["fixture_sha256"]
    assert first["chart_map_sha256"] == second["chart_map_sha256"]
    assert manifest_path.read_bytes() == manifest_before
    assert fixture_path.read_bytes() == fixture_before
    assert not manifest_path.parent.parent.joinpath(".dashboard_preflight.staging").exists()
    assert not manifest_path.parent.parent.joinpath(".dashboard_preflight.previous").exists()


def test_preflight_rebuilds_when_rendering_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renderer/source repair invalidates a same-input preflight cache hit."""

    context, _registry = _seed_preview_refresh_run(tmp_path / "preflight-identity")
    first = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])
    manifest_ref = "extensions/G-0001/dashboard_preflight/preflight_manifest.json"
    manifest_path = context.resolve_run_path(manifest_ref)
    first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert first["rendering_identity"] == first_manifest["rendering_identity"]
    assert first["input_fingerprint"] == dashboard_assembler._preflight_input_fingerprint(
        first["input_items"], first["rendering_identity"]
    )

    changed_identity = copy.deepcopy(first["rendering_identity"])
    changed_identity["skill_tree_sha256"] = "f" * 64
    monkeypatch.setattr(
        dashboard_assembler,
        "_rendering_identity",
        lambda _context: copy.deepcopy(changed_identity),
    )
    rebuilt = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])
    rebuilt_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert rebuilt["input_fingerprint"] != first["input_fingerprint"]
    assert rebuilt["rendering_identity"] == changed_identity
    assert rebuilt_manifest["rendering_identity"] == changed_identity
    assert rebuilt_manifest["input_fingerprint"] == rebuilt["input_fingerprint"]
    # The source inventory remains populated; only its renderer identity
    # changed, so no accepted business visual is silently lost on refresh.
    assert rebuilt["inventory"]["candidates"]


@pytest.mark.parametrize("interrupt_call", [1, 2])
def test_preflight_identity_refresh_recovers_atomic_rename_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_call: int,
) -> None:
    """Both preflight rename boundaries leave one recoverable namespace."""

    context, _registry = _seed_preview_refresh_run(tmp_path / f"preflight-interrupt-{interrupt_call}")
    first = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])
    manifest_path = context.resolve_run_path("extensions/G-0001/dashboard_preflight/preflight_manifest.json")
    old_manifest_bytes = manifest_path.read_bytes()
    changed_identity = copy.deepcopy(first["rendering_identity"])
    changed_identity["skill_tree_sha256"] = "e" * 64
    monkeypatch.setattr(
        dashboard_assembler,
        "_rendering_identity",
        lambda _context: copy.deepcopy(changed_identity),
    )

    original_replace = dashboard_assembler.os.replace
    calls = 0

    def interrupt_after_boundary(source: str | bytes, target: str | bytes) -> None:
        nonlocal calls
        calls += 1
        original_replace(source, target)
        if calls == interrupt_call:
            raise KeyboardInterrupt()

    monkeypatch.setattr(dashboard_assembler.os, "replace", interrupt_after_boundary)
    with pytest.raises(KeyboardInterrupt):
        dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])

    root = context.resolve_run_path("extensions/G-0001/dashboard_preflight")
    staging = root.parent / ".dashboard_preflight.staging"
    previous = root.parent / ".dashboard_preflight.previous"
    assert not staging.exists()
    assert not previous.exists()
    if interrupt_call == 1:
        assert manifest_path.read_bytes() == old_manifest_bytes
    else:
        recovered_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert recovered_manifest["rendering_identity"] == changed_identity

    # The restored old namespace (first boundary) and the already-published
    # candidate (second boundary) are both safe inputs to the next retry.
    monkeypatch.setattr(dashboard_assembler.os, "replace", original_replace)
    retry = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])
    assert retry["rendering_identity"] == changed_identity
    assert not staging.exists()
    assert not previous.exists()


def test_public_generation_preview_supports_partial_selection_and_refresh_with_v2_plan(
    tmp_path: Path,
) -> None:
    """The generation-aware preview API handles partial input and same-G refresh."""

    source = tmp_path / "public-preview"
    context, registry = _seed_preview_refresh_run(source)
    plan_ref = "extensions/G-0001/business_presentation_plan.json"

    first_preflight = dashboard_assembler.business_presentation_preflight(
        context,
        item_ids=["REQ-A"],
    )
    first_candidate = copy.deepcopy(first_preflight["inventory"]["candidates"][0])
    dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[first_candidate],
        reviewer_ref="tests/public-generation-preview",
        fixture_ref=first_preflight["fixture_ref"],
        chart_map_ref=first_preflight["chart_map_ref"],
        item_ids=["REQ-A"],
        generation_id="G-0001",
        presentation_plan_ref=plan_ref,
    )
    first_plan_bytes = context.resolve_run_path(plan_ref).read_bytes()

    first_receipt = dashboard_generation_product.assemble_generation_preview(
        context,
        item_ids=["REQ-A"],
        presentation_plan_ref=plan_ref,
    )
    assert [item["item_id"] for item in first_receipt["input_items"]] == ["REQ-A"]
    assert first_receipt["outputs"]["receipt_ref"] == (
        "products/generations/G-0001/preview/build_receipt.json"
    )
    assert not (source / "products" / "product_manifest.json").exists()
    first_fixture = json.loads(
        context.resolve_run_path(first_receipt["outputs"]["fixture_ref"]).read_text(encoding="utf-8")
    )
    assert first_fixture["presentation_plan_ref"] == plan_ref
    assert {widget["requirement_id"] for widget in first_fixture["widgets"]} == {"REQ-A"}

    # A second committed item refreshes only the mutable canonical preview
    # namespace; the terminal root product remains unpublished.
    _commit_preview_item(context, registry, "REQ-B", invocation_id="inv-public-preview-b", value=11)
    second_preflight = dashboard_assembler.business_presentation_preflight(
        context,
        item_ids=["REQ-A", "REQ-B"],
    )
    second_candidate = copy.deepcopy(second_preflight["inventory"]["candidates"][0])
    dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[second_candidate],
        reviewer_ref="tests/public-generation-preview",
        fixture_ref=second_preflight["fixture_ref"],
        chart_map_ref=second_preflight["chart_map_ref"],
        item_ids=["REQ-A", "REQ-B"],
        generation_id="G-0001",
        presentation_plan_ref=plan_ref,
    )
    second_receipt = dashboard_generation_product.assemble_generation_preview(
        context,
        item_ids=["REQ-A", "REQ-B"],
        presentation_plan_ref=plan_ref,
    )
    assert [item["item_id"] for item in second_receipt["input_items"]] == ["REQ-A", "REQ-B"]
    second_fixture = json.loads(
        context.resolve_run_path(second_receipt["outputs"]["fixture_ref"]).read_text(encoding="utf-8")
    )
    assert {widget["requirement_id"] for widget in second_fixture["widgets"]} == {"REQ-A", "REQ-B"}
    assert second_receipt["outputs"] == first_receipt["outputs"]
    assert not (source / "products" / "product_manifest.json").exists()

    # A stale plan from the partial frontier cannot be used for the expanded
    # subset, even though the preview namespace itself is refreshable.
    stale_ref = "extensions/G-0001/stale_public_generation_preview_plan.json"
    stale_path = context.resolve_run_path(stale_ref)
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(first_plan_bytes)
    with pytest.raises(dashboard_generation_product.DashboardDeltaError, match="input bindings drifted"):
        dashboard_generation_product.assemble_generation_preview(
            context,
            item_ids=["REQ-A", "REQ-B"],
            presentation_plan_ref=stale_ref,
        )


def test_validate_bound_blueprint_binds_manager_and_audit_selection_dataset_and_provenance(
    tmp_path: Path,
) -> None:
    """The render boundary binds every Blueprint visual, including audit-only visuals.

    The Product Agent selects only the manager entry, while the remaining exact
    visual stays in the technical-audit gallery.  Both selections must still
    come from the persisted V2 plan, and the Blueprint's dataset/provenance
    payloads must remain byte-equivalent to the fixture/chart-map projection.
    """

    context = _seed_run(tmp_path / "bound-blueprint")
    preflight = dashboard_assembler.business_presentation_preflight(context, item_ids=["REQ-A"])
    manager_candidate = next(
        value
        for value in preflight["inventory"]["candidates"]
        if value.get("record_id") == "metric-scalar"
    )
    plan_ref = "extensions/G-0001/bound-blueprint-plan.json"
    dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[manager_candidate],
        reviewer_ref="tests/bound-blueprint",
        fixture_ref=preflight["fixture_ref"],
        chart_map_ref=preflight["chart_map_ref"],
        item_ids=["REQ-A"],
        generation_id="G-0001",
        presentation_plan_ref=plan_ref,
    )
    receipt = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="bound-blueprint",
        item_ids=["REQ-A"],
        presentation_plan_ref=plan_ref,
    )
    blueprint_path = context.resolve_run_path(receipt["outputs"]["blueprint_ref"])
    baseline = json.loads(blueprint_path.read_text(encoding="utf-8"))
    fixture_ref = receipt["outputs"]["fixture_ref"]

    # A pristine Blueprint validates and renders through the same public
    # boundary used by a site rebuild.
    dashboard_renderer.render_site_fixture(
        context,
        fixture_ref,
        "bound-blueprint-validation",
        "bound-blueprint-validation/site_manifest.json",
        blueprint_ref=receipt["outputs"]["blueprint_ref"],
    )

    manager_id = manager_candidate["widget_id"]
    audit_id = next(
        visual["id"]
        for visual in baseline["visuals"]
        if visual["id"] != manager_id
    )

    def assert_rejected(name: str, mutate: object, message: str) -> None:
        tampered = copy.deepcopy(baseline)
        mutate(tampered)
        blueprint_path.write_bytes(dashboard_assembler._canonical_bytes(tampered))
        try:
            with pytest.raises(ValueError, match=message):
                dashboard_renderer.render_site_fixture(
                    context,
                    fixture_ref,
                    f"bound-blueprint-tamper-{name}",
                    f"bound-blueprint-tamper-{name}/site_manifest.json",
                    blueprint_ref=receipt["outputs"]["blueprint_ref"],
                )
        finally:
            blueprint_path.write_bytes(dashboard_assembler._canonical_bytes(baseline))

    # Manager choices remain plan-bound.  Audit-only visuals are retained in
    # the exact technical gallery but do not require manager recipe metadata.
    assert_rejected(
        "manager-selection",
        lambda value: next(visual for visual in value["visuals"] if visual["id"] == manager_id).__setitem__(
            "recipe_id", "metric_grid"
        ),
        "selection drifted",
    )
    audit_mutation = copy.deepcopy(baseline)
    next(visual for visual in audit_mutation["visuals"] if visual["id"] == audit_id)["recipe_id"] = "kpi_card"
    blueprint_path.write_bytes(dashboard_assembler._canonical_bytes(audit_mutation))
    try:
        # Audit-only presentation metadata is not part of the manager plan and
        # may remain renderer-neutral; immutable dataset/provenance checks
        # below still bind its exact source payload.
        dashboard_renderer.render_site_fixture(
            context,
            fixture_ref,
            "bound-blueprint-audit-metadata",
            "bound-blueprint-audit-metadata/site_manifest.json",
            blueprint_ref=receipt["outputs"]["blueprint_ref"],
        )
    finally:
        blueprint_path.write_bytes(dashboard_assembler._canonical_bytes(baseline))
    # The complete normalized dataset payload is source-bound, not just its
    # visual ID/order.  A changed reviewed row must fail closed.
    assert_rejected(
        "audit-dataset",
        lambda value: next(
            dataset for dataset in value["datasets"] if dataset["id"] == f"dataset-{audit_id}"
        )["rows"].__setitem__(0, {"label": "tampered", "source_key": "tampered", "value": 0}),
        "dataset payload drifted",
    )
    # Provenance refs are independently bound for audit visuals as well.
    assert_rejected(
        "audit-provenance",
        lambda value: next(visual for visual in value["visuals"] if visual["id"] == audit_id)[
            "provenance"
        ].__setitem__("trace_refs", ["tampered/ref"]),
        "visual provenance drifted",
    )


def _seed_run_with_analytical_artifact(root: Path) -> RunContext:
    """Create a root run whose accepted business output includes one artifact."""

    context = RunContext("RUN-ASSEMBLER-ARTIFACT-TEST", root)
    RunLifecycle.create(context, ("REQ-A",), mode="requirement")
    workspace = ItemWorkspace.create(
        context,
        "REQ-A",
        mode="requirement",
        original_text="Profile the supplied customer population.",
    )
    workspace.write_plan({"item_id": "REQ-A", "offline": True})
    artifact = DataProfileArtifact(
        artifact_id="artifact-accepted-profile",
        requirement_id="REQ-A",
        dataset_fingerprint="d" * 64,
        source_refs=("synthetic:customers",),
        method="reviewed_fixture",
        profile={"row_count": 2, "columns": [{"name": "customer_id", "missing_count": 0}]},
        created_at="2026-01-01T00:00:00+00:00",
    )
    artifact_path = workspace.work_root / "analytical_artifact.json"
    artifact_path.write_bytes(artifact.to_json().encode("utf-8"))
    workspace.write_draft({
        "item_id": "REQ-A",
        "answer": "The supplied fixture contains two customers.",
        "evidence_refs": ["work/analytical_artifact.json"],
    })
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json", "work/analytical_artifact.json"))
    registry = PreparedAssetRegistry(context)
    session = IntegrationSession.create(
        context,
        workspace,
        registry,
        "synthetic-integration",
        invocation_id="inv-assembler-artifact",
    )
    session.add_metric(
        metric_id="metric-scalar",
        scope="REQ-A",
        evidence_refs=("answer_content.json",),
        label="Reviewed scalar",
        units="records",
        value=7,
        population=10,
    )
    if session.fidelity_result is None:
        session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()
    (root / "requirement_supervisor_plan.json").write_text(
        json.dumps({"groups": [{"id": "commercial", "title": "Commercial decisions", "requirement_ids": ["REQ-A"]}]}, sort_keys=True),
        encoding="utf-8",
    )
    return context


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {_path.relative_to(root).as_posix(): _digest(_path) for _path in sorted(root.rglob("*")) if _path.is_file()}


def _rebind_run_root(root: Path) -> None:
    """Make a byte-for-byte copied synthetic run valid at its new root."""

    path = root / "run_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["run_root"] = str(root)
    if isinstance(state.get("terminal_outcome"), dict):
        state["terminal_outcome"]["manifest_path"] = str(root / "requirements" / "REQ-A" / "accepted" / "manifest.json")
    unsigned = {key: value for key, value in state.items() if key != "manifest_hash"}
    state["manifest_hash"] = hashlib.sha256(
        (json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    item_state_path = root / "requirements" / "REQ-A" / "item_state.json"
    item_state = json.loads(item_state_path.read_text(encoding="utf-8"))
    if isinstance(item_state.get("terminal_outcome"), dict):
        item_state["terminal_outcome"]["manifest_path"] = str(root / "requirements" / "REQ-A" / "accepted" / "manifest.json")
    item_state_path.write_text(json.dumps(item_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def test_default_context_release_metadata_is_bound_to_dashboard_fixture(tmp_path: Path) -> None:
    """Product Agent defaults must emit the current core/skill release pair."""

    context = _seed_run(tmp_path / "source")
    assert context.core_version == DEFAULT_CORE_VERSION == "0.9.0"
    assert context.skill_version == DEFAULT_SKILL_VERSION == "0.8.0"

    receipt = dashboard_assembler.assemble_dashboard(context, output_dir="default-release")
    fixture_path = context.resolve_run_path(receipt["outputs"]["fixture_ref"])
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["core_name"] == dashboard_assembler.CORE_NAME
    assert fixture["core_version"] == "0.9.0"
    assert fixture["skill_name"] == dashboard_assembler.SKILL_NAME
    assert fixture["skill_version"] == "0.8.0"


def test_assembler_is_deterministic_and_preserves_typed_currency_partitions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    first = dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    first_root = source / "products" / "repro_dashboard_v4"
    first_hashes = _tree_hashes(first_root)

    second_source = tmp_path / "source_copy"
    shutil.copytree(source, second_source)
    _rebind_run_root(second_source)
    second_context = RunContext(context.run_id, second_source)
    second = dashboard_assembler.assemble_dashboard(second_context, output_dir="repro_dashboard_v4")
    second_hashes = _tree_hashes(second_source / "products" / "repro_dashboard_v4")
    assert first_hashes == second_hashes
    assert first["output_hashes"] == second["output_hashes"]

    fixture = json.loads((first_root / "dashboard_fixture_v4.json").read_text(encoding="utf-8"))
    assert fixture["skill_name"] == dashboard_assembler.SKILL_NAME
    assert fixture["skill_version"] == context.skill_version
    assert fixture["core_name"] == dashboard_assembler.CORE_NAME
    assert fixture["core_version"] == context.core_version
    types = {widget["id"]: widget["type"] for widget in fixture["widgets"]}
    assert types["REQ-A-metric-currency"] == "metric_grid"
    currency = next(widget for widget in fixture["widgets"] if widget["id"] == "REQ-A-metric-currency")
    assert all("size" not in tile for tile in currency["tiles"])
    assert first["new_analytics"] is False
    assert (first_root / "site" / "index.html").is_file()
    assert first["site_binding"]["file_count"] == len(first["site_binding"]["files"])
    assert "site_manifest.json" in first["site_binding"]["files"]
    site_manifest = json.loads((first_root / "site" / "site_manifest.json").read_text(encoding="utf-8"))
    assert site_manifest["site_file_hashes"]
    assert site_manifest["site_tree_file_count"] == len(site_manifest["site_file_hashes"])


def test_generation_product_validator_distinguishes_complete_and_self_excluding_site_trees(tmp_path: Path) -> None:
    """The Product Agent receives one validated binding for the 12/13 case.

    ``site_manifest.json`` describes the other twelve files, while the receipt
    binds all thirteen files.  The program-owned validator accepts that pair,
    returns candidate-ready artifact bindings, and rejects either a tampered
    page or an agent-style cross-comparison of the two tree domains.
    """

    context = _seed_run(tmp_path / "source")
    RunLifecycle.load(context).reconcile_from_run()
    telemetry = context.run_root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text('{"event":"synthetic"}\n', encoding="utf-8")
    (telemetry / "inventory_counters.json").write_text('{"accepted":1}\n', encoding="utf-8")
    receipt = dashboard_generation_product.assemble_generation_product(context, output_dir="validation")
    receipt_path = context.resolve_run_path(receipt["outputs"]["receipt_ref"])
    site_root = context.resolve_run_path(receipt["outputs"]["site_ref"])
    # The normal synthetic site has eight files.  Add five reviewed-looking
    # pages so the regression exercises the exact twelve (manifest-excluded)
    # versus thirteen (receipt-complete) relationship from the live product.
    for index in range(5):
        extra = site_root / "extra" / f"section-{index}.html"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text(f"<html><body><h1>Section {index}</h1></body></html>\n", encoding="utf-8")

    site_manifest_path = site_root / "site_manifest.json"
    site_manifest = json.loads(site_manifest_path.read_text(encoding="utf-8"))
    non_manifest = dashboard_assembler._site_tree_binding(site_root, exclude={"site_manifest.json"})
    site_manifest["site_file_hashes"] = non_manifest["files"]
    site_manifest["site_tree_sha256"] = non_manifest["tree_sha256"]
    site_manifest["site_tree_file_count"] = non_manifest["file_count"]
    site_manifest_path.write_bytes(dashboard_assembler._canonical_bytes(site_manifest))
    receipt = dict(receipt)
    receipt["output_hashes"] = {
        **dict(receipt["output_hashes"]),
        "site_manifest_sha256": hashlib.sha256(site_manifest_path.read_bytes()).hexdigest(),
    }
    receipt["site_binding"] = dashboard_assembler._site_tree_binding(site_root)
    receipt_path.write_bytes(dashboard_assembler._canonical_bytes(receipt))
    manifest_path = context.resolve_product_path("product_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dashboard"]["receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    for asset in manifest["assets"]:
        if asset["ref"] == receipt["outputs"]["receipt_ref"]:
            asset["sha256"] = manifest["dashboard"]["receipt_sha256"]
        elif asset["ref"] == f"{receipt['outputs']['site_ref'].rstrip('/')}/site_manifest.json":
            asset["sha256"] = receipt["output_hashes"]["site_manifest_sha256"]
    valid_manifest_bytes = dashboard_assembler._canonical_bytes(manifest)
    manifest_path.write_bytes(valid_manifest_bytes)

    validated = dashboard_generation_product.validate_generation_product(
        context,
        receipt=receipt,
        product_manifest_ref="products/product_manifest.json",
    )
    assert validated["valid"] is True
    assert validated["site_binding"]["file_count"] == 13
    assert validated["site_manifest_binding"]["file_count"] == 12
    assert set(validated["artifact_bindings"]) == {
        "manifest",
        "fixture",
        "chart_map",
        "chart_registry",
        "blueprint",
        "site",
        "receipt",
    }
    assert validated["artifact_bindings"]["site"]["sha256"] == dashboard_generation_product._json_hash(
        {"files": validated["site_binding"]["files"]}
    )

    malformed_manifest = copy.deepcopy(manifest)
    malformed_manifest["dashboard"]["receipt_sha256"] = "0" * 64
    manifest_path.write_bytes(dashboard_assembler._canonical_bytes(malformed_manifest))
    with pytest.raises(dashboard_generation_product.DashboardDeltaError, match="manifest"):
        dashboard_generation_product.validate_generation_product(
            context,
            receipt=receipt,
            product_manifest_ref="products/product_manifest.json",
        )
    tampered_assets_manifest = copy.deepcopy(manifest)
    tampered_assets_manifest["assets"][0]["sha256"] = "0" * 64
    manifest_path.write_bytes(dashboard_assembler._canonical_bytes(tampered_assets_manifest))
    with pytest.raises(dashboard_generation_product.DashboardDeltaError, match="asset bindings"):
        dashboard_generation_product.validate_generation_product(
            context,
            receipt=receipt,
            product_manifest_ref="products/product_manifest.json",
        )
    manifest_path.write_bytes(valid_manifest_bytes)

    valid_receipt_bytes = receipt_path.read_bytes()
    symlink = site_root / "extra" / "symlinked-page.html"
    symlink.symlink_to(site_root / "index.html")
    with pytest.raises(dashboard_generation_product.DashboardDeltaError, match="symlink"):
        dashboard_generation_product.validate_generation_product(context, receipt=receipt)
    symlink.unlink()

    traversal_receipt = copy.deepcopy(receipt)
    traversal_receipt["outputs"] = {
        **dict(traversal_receipt["outputs"]),
        "fixture_ref": "../outside-fixture.json",
    }
    receipt_path.write_bytes(dashboard_assembler._canonical_bytes(traversal_receipt))
    with pytest.raises(dashboard_generation_product.DashboardDeltaError, match="outside|escape|boundary"):
        dashboard_generation_product.validate_generation_product(context, receipt=traversal_receipt)
    receipt_path.write_bytes(valid_receipt_bytes)

    (site_root / "extra" / "section-0.html").write_text("<html><body>tampered</body></html>\n", encoding="utf-8")
    with pytest.raises(dashboard_generation_product.DashboardDeltaError, match="site binding|output hash|site tree"):
        dashboard_generation_product.validate_generation_product(context, receipt=receipt)

    # Restore the page and replace the receipt's complete tree with the
    # manifest's self-excluding tree.  This is the exact false-positive check
    # the Product Agent previously performed; the validator rejects it.
    (site_root / "extra" / "section-0.html").write_text("<html><body><h1>Section 0</h1></body></html>\n", encoding="utf-8")
    wrong_receipt = dict(receipt)
    wrong_receipt["site_binding"] = {
        key: non_manifest[key]
        for key in ("files", "tree_sha256", "file_count")
    }
    receipt_path.write_bytes(dashboard_assembler._canonical_bytes(wrong_receipt))
    with pytest.raises(dashboard_generation_product.DashboardDeltaError, match="complete site binding"):
        dashboard_generation_product.validate_generation_product(context, receipt=wrong_receipt)


def test_root_assembler_receipt_is_inspectable_and_forgery_bound(tmp_path: Path) -> None:
    """Exercise the real G-0001 producer against the phase inspector.

    The receipt is not reconstructed by the test: it is the bytes emitted by
    ``assemble_dashboard``.  Only the minimal root product manifest envelope
    is supplied so the inspector can verify the receipt's identity, raw plan
    hash, output references, and explicit root-parent marker.
    """

    context = _seed_run(tmp_path / "source")
    receipt = dashboard_assembler.assemble_dashboard(context, output_dir="products/inspect-dashboard")
    receipt_path = context.resolve_run_path(receipt["outputs"]["receipt_ref"])
    receipt_bytes = receipt_path.read_bytes()
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    assert receipt["run_id"] == context.run_id
    assert receipt["generation_id"] == "G-0001"
    assert receipt["parent"] == {
        "root_generation": True,
        "parent_generation_id": None,
        "parent_manifest_ref": None,
        "parent_manifest_hash": None,
    }
    plan_path = context.resolve_run_path("requirement_supervisor_plan.json")
    plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    assert receipt["plan_binding"] == {
        "ref": "requirement_supervisor_plan.json",
        "sha256": plan_hash,
        "admission_sha256": plan_hash,
        "generation_id": "G-0001",
    }

    manifest_path = context.resolve_run_path("products/product_manifest.json")
    manifest = {
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "run_id": context.run_id,
        "status": "complete",
        "terminal": True,
        "source_status": "reviewed_outputs_only",
        "new_analytics": False,
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "lifecycle": {"generation_id": "G-0001"},
        "dashboard": {
            "receipt_ref": receipt["outputs"]["receipt_ref"],
            "receipt_sha256": receipt_hash,
        },
        "lineage": {"root_generation": True},
    }

    def write_manifest_and_receipt(value: dict[str, object]) -> None:
        receipt_path.write_bytes(
            (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
        manifest["dashboard"]["receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        manifest_path.write_bytes(
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )

    baseline = copy.deepcopy(receipt)
    write_manifest_and_receipt(baseline)
    assert inspect_product_manifest(context, "G-0001", "products/product_manifest.json")["valid"] is True

    for field, mutate, diagnostic in (
        ("run_id", lambda value: value.update(run_id="FORGED-RUN"), "run/generation lineage"),
        ("generation_id", lambda value: value.update(generation_id="G-0002"), "run/generation lineage"),
        (
            "plan_binding",
            lambda value: value["plan_binding"].update(sha256="0" * 64),
            "plan binding is stale",
        ),
        (
            "parent",
            lambda value: value["parent"].update(parent_generation_id="G-0000"),
            "root parent binding is invalid",
        ),
    ):
        forged = copy.deepcopy(baseline)
        mutate(forged)
        write_manifest_and_receipt(forged)
        inspected = inspect_product_manifest(context, "G-0001", "products/product_manifest.json")
        assert inspected["valid"] is False, field
        assert any(diagnostic in value for value in inspected["diagnostics"]), (field, inspected)

    write_manifest_and_receipt(baseline)


def test_generation_product_publishes_root_terminal_manifest(tmp_path: Path) -> None:
    context = _seed_run(tmp_path / "source")
    RunLifecycle.load(context).reconcile_from_run()
    telemetry = context.run_root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text('{"event":"synthetic"}\n', encoding="utf-8")
    (telemetry / "inventory_counters.json").write_text('{"accepted":1}\n', encoding="utf-8")

    first = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
    )
    manifest_path = context.resolve_product_path("product_manifest.json")
    first_manifest_bytes = manifest_path.read_bytes()
    inspected = inspect_product_manifest(context, "G-0001", "products/product_manifest.json")

    assert inspected["valid"] is True
    assert inspected["manifest"]["status"] == "complete"
    assert first["freeze_inputs"]["freeze_markers"]["prepared_data_registry_frozen"] is True
    assert inspected["manifest"]["dashboard"]["receipt_ref"] == first["outputs"]["receipt_ref"]
    assert inspected["manifest"]["lifecycle"]["state_at_product_freeze"] == "integration_complete"
    fixture = json.loads(context.resolve_run_path(first["outputs"]["fixture_ref"]).read_text(encoding="utf-8"))
    assert fixture["domains"]
    assert inspected["manifest"]["dashboard"]["domain_count"] == len(fixture["domains"])
    assert inspected["manifest"]["dashboard"]["domain_count"] != len(fixture.get("ontology_groups", []))

    second = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
    )
    assert second == first
    assert manifest_path.read_bytes() == first_manifest_bytes


def test_generation_product_rebuilds_root_when_presentation_plan_changes(tmp_path: Path) -> None:
    """A newly recorded manager plan must replace, not hide behind, an old root product."""

    context = _seed_run(tmp_path / "source")
    RunLifecycle.load(context).reconcile_from_run()
    telemetry = context.run_root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text('{"event":"synthetic"}\n', encoding="utf-8")
    (telemetry / "inventory_counters.json").write_text('{"accepted":1}\n', encoding="utf-8")
    baseline = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
    )
    old_namespace = context.resolve_product_path("root-generation-product")
    fixture_ref = "products/root-generation-product/dashboard_fixture_v4.json"
    inventory = dashboard_assembler.business_presentation_inventory(context, fixture_ref=fixture_ref)
    candidate = next(value for value in inventory["candidates"] if value.get("record_id") == "metric-scalar")
    entry = {
        "widget_id": candidate["widget_id"],
        "record_id": candidate["record_id"],
        "requirement_id": candidate["requirement_id"],
        "presentation_role": candidate["presentation_role"],
        "file_sha256": candidate["file_sha256"],
        "canonical_payload_sha256": candidate["canonical_payload_sha256"],
        "display_projection": candidate["display_projection"],
    }
    plan_ref = "extensions/G-0001/root-manager-plan.json"
    dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[entry],
        reviewer_ref="tests/root-rebuild",
        fixture_ref=fixture_ref,
        presentation_plan_ref=plan_ref,
    )

    rebuilt = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
        presentation_plan_ref=plan_ref,
    )
    manifest = json.loads(context.resolve_product_path("product_manifest.json").read_text(encoding="utf-8"))
    plan_hash = _digest(context.resolve_run_path(plan_ref))
    assert manifest["presentation_plan_ref"] == plan_ref
    assert manifest["presentation_plan_sha256"] == plan_hash
    assert rebuilt["outputs"]["receipt_ref"] != baseline["outputs"]["receipt_ref"]
    assert old_namespace.is_dir()
    assert context.resolve_run_path(rebuilt["outputs"]["receipt_ref"]).is_file()

    # The same recorded plan is idempotent and reuses the newly published root.
    second = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
        presentation_plan_ref=plan_ref,
    )
    assert second == rebuilt


def test_product_revision_assembly_owns_distinct_bundle_and_preserves_prior_revision(
    tmp_path: Path,
) -> None:
    """A real assembler regeneration writes a new immutable revision bundle."""

    context = _seed_run(tmp_path / "source")
    RunLifecycle.load(context).reconcile_from_run()
    telemetry = context.run_root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text('{"event":"synthetic"}\n', encoding="utf-8")
    (telemetry / "inventory_counters.json").write_text('{"accepted":1}\n', encoding="utf-8")

    first = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="revision-root-product",
    )
    first_validation = dashboard_generation_product.validate_generation_product(
        context,
        receipt=first,
        product_manifest_ref="products/product_manifest.json",
    )
    assert first_validation["valid"] is True
    store = ProductReviewStore(context, "G-0001")
    plan_path = context.resolve_run_path(first["plan_binding"]["ref"])
    candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
        product_owner="product-agent",
        parent_lineage={
            "root_generation": True,
            "parent_generation_id": None,
            "parent_manifest_ref": None,
            "parent_manifest_hash": None,
        },
        plan_binding={
            "plan_ref": first["plan_binding"]["ref"],
            "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        publication_policy_hash=canonical_hash({"enabled": False}),
        artifact_bindings=first_validation["artifact_bindings"],
    )
    store.record_candidate(candidate)
    store.record_review(
        reviewer_ref="product-reviewer",
        verdict="accept",
        reviewed_at="2026-01-01T00:00:00Z",
    )
    initial_pointer = store.load_active_revision()
    assert initial_pointer is not None and initial_pointer.revision_id == "rev-0001"
    initial_bound = {
        name: context.resolve_run_path(first_validation["artifact_bindings"][name]["ref"]).read_bytes()
        for name in ("manifest", "fixture", "chart_map", "chart_registry", "blueprint", "receipt")
    }
    initial_site = context.resolve_run_path(first_validation["artifact_bindings"]["site"]["ref"])
    initial_site_files = {
        str(path.relative_to(initial_site)): path.read_bytes()
        for path in initial_site.rglob("*")
        if path.is_file()
    }

    target = store.begin_revision(
        request_id="actual-regeneration",
        input_fingerprint="a" * 64,
        implementation_identity="b" * 64,
    )
    assert target.revision_id == "rev-0002"
    second = dashboard_generation_product.assemble_generation_product(
        context,
        revision_id=target.revision_id,
        output_root_ref=target.output_root_ref,
        output_dir=target.output_root_ref,
    )
    second_validation = dashboard_generation_product.validate_generation_product(
        context,
        receipt=second,
        product_manifest_ref=f"{target.output_root_ref}/product_manifest.json",
        revision_id=target.revision_id,
        output_root_ref=target.output_root_ref,
    )
    assert second_validation["valid"] is True
    assert second["outputs"]["receipt_ref"] != first["outputs"]["receipt_ref"]
    target_manifest = context.resolve_run_path(second_validation["artifact_bindings"]["manifest"]["ref"])
    assert target_manifest.is_file()
    target_fixture = context.resolve_run_path(second_validation["artifact_bindings"]["fixture"]["ref"])
    target_site = context.resolve_run_path(second_validation["artifact_bindings"]["site"]["ref"])
    assert target_manifest.read_bytes() != initial_bound["manifest"]
    assert target_fixture.read_bytes() != initial_bound["fixture"]
    assert (target_site / "site_manifest.json").read_bytes() != initial_site_files["site_manifest.json"]
    target_candidate = ProductCandidate(
        run_id=context.run_id,
        generation_id="G-0001",
        product_owner="product-agent",
        parent_lineage={
            "root_generation": True,
            "parent_generation_id": None,
            "parent_manifest_ref": None,
            "parent_manifest_hash": None,
        },
        plan_binding={
            "plan_ref": first["plan_binding"]["ref"],
            "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        publication_policy_hash=canonical_hash({"enabled": False}),
        artifact_bindings=second_validation["artifact_bindings"],
    )
    store.record_candidate(target_candidate, revision_id=target.revision_id)
    store.record_review(
        reviewer_ref="product-reviewer-2",
        verdict="accept_with_limits",
        reviewed_at="2026-01-01T00:00:01Z",
        revision_id=target.revision_id,
    )
    activated = store.activate_revision(target.revision_id)
    assert activated.revision_id == target.revision_id
    assert store.load_revision_candidate("rev-0001").computed_hash == candidate.computed_hash
    assert store.load_revision_review("rev-0001").candidate_hash == candidate.computed_hash
    for name, content in initial_bound.items():
        assert context.resolve_run_path(first_validation["artifact_bindings"][name]["ref"]).read_bytes() == content
    for relative, content in initial_site_files.items():
        assert (initial_site / relative).read_bytes() == content

    future = store.begin_revision(
        request_id="future-regeneration",
        input_fingerprint="c" * 64,
        implementation_identity="d" * 64,
    )
    assert future.revision_id == "rev-0003"
    store.fail_revision(future.revision_id)
    assert store.load_active_revision().revision_id == target.revision_id  # type: ignore[union-attr]


def test_generation_product_rebuilds_root_when_rendering_identity_changes_and_keeps_failed_item_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renderer repair invalidates reuse without changing frozen inputs."""

    context = _seed_failed_root_run(tmp_path / "source")
    baseline = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
    )
    baseline_manifest = json.loads(
        context.resolve_product_path("product_manifest.json").read_text(encoding="utf-8")
    )
    assembler = dashboard_generation_product._assembler()
    changed_identity = copy.deepcopy(assembler._rendering_identity(context))
    changed_identity["skill_tree_sha256"] = "f" * 64
    monkeypatch.setattr(assembler, "_rendering_identity", lambda _context: copy.deepcopy(changed_identity))

    rebuilt = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
    )
    manifest = json.loads(
        context.resolve_product_path("product_manifest.json").read_text(encoding="utf-8")
    )
    assert rebuilt["outputs"]["receipt_ref"] != baseline["outputs"]["receipt_ref"]
    assert manifest["dashboard"]["rendering_identity"] == changed_identity
    assert baseline_manifest["dashboard"]["rendering_identity"] != changed_identity
    assert rebuilt["input_items"] == baseline["input_items"]

    site_root = context.resolve_run_path(rebuilt["outputs"]["site_ref"])
    rendered_html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in site_root.rglob("*.html")
    )
    assert "REQ-008" in rendered_html
    assert "technical_failure" in rendered_html
    assert "recovery_exhausted" in rendered_html
    assert "Failed requirements are listed explicitly" in rendered_html
    failed_section = rendered_html.split('<section class="limits failed-items"', 1)[1].split("</section>", 1)[0]
    assert "transport exhausted" not in failed_section


def test_root_manifest_rebuild_compare_and_publish_is_serialized(tmp_path: Path) -> None:
    """Concurrent presentation rebuilds cannot both rebind one old root."""

    context = _seed_run(tmp_path / "source")
    RunLifecycle.load(context).reconcile_from_run()
    telemetry = context.run_root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text('{"event":"synthetic"}\n', encoding="utf-8")
    (telemetry / "inventory_counters.json").write_text('{"accepted":1}\n', encoding="utf-8")
    baseline = dashboard_generation_product.assemble_generation_product(
        context,
        output_dir="root-generation-product",
    )
    manifest_path = context.resolve_product_path("product_manifest.json")
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt_path = context.resolve_run_path(baseline["outputs"]["receipt_ref"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipts = []
    for label in ("one", "two"):
        candidate = copy.deepcopy(receipt)
        candidate["presentation_plan_ref"] = f"extensions/G-0001/plan-{label}.json"
        candidate["presentation_plan_sha256"] = hashlib.sha256(label.encode("utf-8")).hexdigest()
        candidate["manager_widget_ids"] = [f"widget-{label}"]
        receipts.append(candidate)

    barrier = threading.Barrier(2)
    successes: list[str] = []
    failures: list[Exception] = []

    def contender(candidate: Mapping[str, Any]) -> None:
        try:
            barrier.wait(timeout=5)
            dashboard_generation_product._publish_root_product_manifest(
                context,
                RunLifecycle.load(context),
                candidate,
                replace_existing=True,
                expected_existing=old_manifest,
            )
            successes.append(str(candidate["presentation_plan_ref"]))
        except Exception as exc:  # one contender must lose the manifest CAS
            failures.append(exc)

    workers = [threading.Thread(target=contender, args=(candidate,)) for candidate in receipts]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not any(worker.is_alive() for worker in workers)
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], dashboard_generation_product.DashboardDeltaError)
    assert "root product manifest changed during presentation rebuild" in str(failures[0])
    final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert final_manifest["presentation_plan_ref"] == successes[0]


def test_business_presentation_plan_binds_exact_pointer_projection_and_rejects_tamper(tmp_path: Path) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    dashboard_assembler.assemble_dashboard(context, output_dir="inventory_source")
    fixture_ref = "products/inventory_source/dashboard_fixture_v4.json"
    inventory = dashboard_assembler.business_presentation_inventory(context, fixture_ref=fixture_ref)
    candidate = next(value for value in inventory["candidates"] if value.get("record_id") == "metric-scalar")
    entry = {
        "widget_id": candidate["widget_id"],
        "record_id": candidate["record_id"],
        "requirement_id": candidate["requirement_id"],
        "presentation_role": candidate["presentation_role"],
        "file_sha256": candidate["file_sha256"],
        "canonical_payload_sha256": candidate["canonical_payload_sha256"],
        "display_projection": candidate["display_projection"],
    }
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[entry],
        reviewer_ref="tests/pointer-plan",
        fixture_ref=fixture_ref,
        presentation_plan_ref="extensions/G-0001/business_presentation_plan.json",
    )
    assert plan["manager_widget_ids"] == [candidate["widget_id"]]
    assert plan["manager_entries"][0]["display_projection"] == candidate["display_projection"]
    dashboard_assembler.assemble_dashboard(
        context,
        output_dir="pointer_plan_dashboard",
        presentation_plan_ref="extensions/G-0001/business_presentation_plan.json",
    )

    def write_bad(name: str, mutate: object) -> None:
        bad = json.loads(json.dumps(entry))
        mutate(bad)
        with pytest.raises(dashboard_assembler.BusinessPresentationPlanError):
            dashboard_assembler.write_business_presentation_plan(
                context,
                manager_entries=[bad],
                reviewer_ref="tests/pointer-plan",
                fixture_ref=fixture_ref,
                presentation_plan_ref=f"extensions/G-0001/{name}.json",
            )

    write_bad("pointer_missing", lambda value: value["display_projection"]["title"].update({"pointer": "/payload/not_present"}))
    write_bad("invented_text", lambda value: value["display_projection"]["title"].update({"value": "invented"}))
    write_bad("mixed_record", lambda value: value["display_projection"]["title"].update({"value": "7"}))


def test_actual_assembler_preserves_all_claim_and_scalar_candidates_in_product_order(tmp_path: Path) -> None:
    """The Product plan alone selects and orders independent claim/metric views."""

    context = RunContext("RUN-PLAN-ORDER-INDEPENDENT-VIEWS", tmp_path / "source")
    RunLifecycle.create(context, ("REQ-A",), mode="requirement")
    workspace = ItemWorkspace.create(
        context,
        "REQ-A",
        mode="requirement",
        original_text="Review the supplied claims and scalar signals.",
    )
    workspace.write_plan({"item_id": "REQ-A", "offline": True})
    workspace.write_draft({"item_id": "REQ-A", "answer": "bounded", "limitations": ["synthetic only"]})
    workspace.record_review("accept", reviewer_ref="synthetic-reviewer")
    workspace.accept(accepted_refs=("work/plan.json",))
    session = IntegrationSession.create(
        context,
        workspace,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id="inv-plan-order",
    )
    evidence = ("answer_content.json",)
    session.add_claim(
        {"claim": "First reviewed claim"},
        scope="REQ-A",
        evidence_refs=evidence,
        claim_id="claim-one",
        status="accepted",
        period="2026-08-01",
    )
    session.add_claim(
        {"claim": "Second reviewed claim"},
        scope="REQ-A",
        evidence_refs=evidence,
        claim_id="claim-two",
        status="accepted",
        period="2026-08-02",
    )
    session.add_metric(
        metric_id="metric-one",
        scope="REQ-A",
        evidence_refs=evidence,
        label="Reviewed scalar one",
        units="records",
        value=7,
        population=10,
    )
    session.add_metric(
        metric_id="metric-two",
        scope="REQ-A",
        evidence_refs=evidence,
        label="Reviewed scalar two",
        units="records",
        value=4,
        population=10,
    )
    assert session.fidelity_result is None
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()
    (context.run_root / "requirement_supervisor_plan.json").write_text(
        json.dumps(
            {"groups": [{"id": "commercial", "title": "Commercial decisions", "requirement_ids": ["REQ-A"]}]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    # Build the ordinary candidate fixture, then let Product select every
    # candidate in reverse order.  This is the actual assembler path used by
    # the durable product, not a direct renderer-only fixture.
    baseline = dashboard_assembler.assemble_dashboard(context, output_dir="candidate-source")
    baseline_fixture_path = context.resolve_run_path(baseline["outputs"]["fixture_ref"])
    baseline_fixture = json.loads(baseline_fixture_path.read_text(encoding="utf-8"))
    inventory = dashboard_assembler.business_presentation_inventory(
        context,
        fixture_ref=baseline["outputs"]["fixture_ref"],
    )
    candidates = list(inventory["candidates"])
    assert len(candidates) >= 4
    assert sum(candidate.get("record_id") in {"claim-one", "claim-two"} for candidate in candidates) == 2
    assert sum(candidate.get("record_id") in {"metric-one", "metric-two"} for candidate in candidates) == 2

    entry_keys = (
        "widget_id",
        "record_id",
        "requirement_id",
        "presentation_role",
        "file_sha256",
        "canonical_payload_sha256",
        "display_projection",
    )

    def manager_entry(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {key: copy.deepcopy(candidate[key]) for key in entry_keys}

    entries = [manager_entry(candidate) for candidate in candidates]
    plan_ref = "extensions/G-0001/all-views-reversed.json"
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=list(reversed(entries)),
        reviewer_ref="tests/all-views-reversed",
        fixture_ref=baseline["outputs"]["fixture_ref"],
        presentation_plan_ref=plan_ref,
    )
    assert plan["manager_widget_ids"] == [entry["widget_id"] for entry in reversed(entries)]

    assembled = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="all-views-reversed",
        presentation_plan_ref=plan_ref,
    )
    fixture = json.loads(
        context.resolve_run_path(assembled["outputs"]["fixture_ref"]).read_text(encoding="utf-8")
    )
    before_by_id = {widget["id"]: widget for widget in baseline_fixture["widgets"]}
    after_by_id = {widget["id"]: widget for widget in fixture["widgets"]}
    assert set(after_by_id) == set(before_by_id) == set(plan["manager_widget_ids"])
    for widget_id, before in before_by_id.items():
        after = after_by_id[widget_id]
        assert after.get("title") == before.get("title")
        assert after.get("type") == before.get("type")
        assert after.get("rows") == before.get("rows")
        assert after.get("value") == before.get("value")
        assert after.get("presentation_role") == before.get("presentation_role")
        assert after.get("manager_admission", {}).get("status") == "admitted"
        assert "aggregated_metric_ids" not in after
    assert not any(widget.get("title") in {"Reviewed findings", "Key signals"} for widget in fixture["widgets"])
    assert fixture["manager_widget_ids"] == plan["manager_widget_ids"]

    site_root = context.resolve_run_path(assembled["outputs"]["site_ref"])
    site_manifest = json.loads((site_root / "site_manifest.json").read_text(encoding="utf-8"))
    manifest_ids = [
        str(item["element_id"]).removeprefix("widget-")
        for item in site_manifest["items"]
    ]
    assert manifest_ids == plan["manager_widget_ids"]
    domain_html = (site_root / "domains" / "commercial.html").read_text(encoding="utf-8")
    visible = re.sub(r"<details\b[^>]*>.*?</details>", "", domain_html, flags=re.DOTALL)
    rendered_ids = re.findall(r'data-widget-id="([^"]+)"', visible)
    assert rendered_ids == plan["manager_widget_ids"]
    assert "Reviewed findings" not in visible
    assert "Key signals" not in visible
    assert "First reviewed claim" in visible
    assert "Second reviewed claim" in visible
    assert ">7<" in visible and ">4<" in visible


def test_presentation_plan_binds_analytical_artifacts_and_rejects_artifact_drift(tmp_path: Path) -> None:
    """A typed accepted artifact survives plan write and canonical assembly."""

    source = tmp_path / "source"
    context = _seed_run_with_analytical_artifact(source)
    dashboard_assembler.assemble_dashboard(context, output_dir="inventory_source")
    fixture_ref = "products/inventory_source/dashboard_fixture_v4.json"
    inventory = dashboard_assembler.business_presentation_inventory(context, fixture_ref=fixture_ref)
    input_item = next(value for value in inventory["input_items"] if value["item_id"] == "REQ-A")
    artifact_binding = input_item["analytical_artifacts"][0]
    assert set(artifact_binding) == dashboard_assembler._PRESENTATION_ARTIFACT_KEYS
    assert artifact_binding["artifact_id"] == "artifact-accepted-profile"
    assert artifact_binding["artifact_type"] == "data_profile"
    assert artifact_binding["schema_version"] == "1.0"
    assert artifact_binding["requirement_id"] == "REQ-A"
    candidate = next(value for value in inventory["candidates"] if value.get("record_id") == "metric-scalar")
    entry = {
        "widget_id": candidate["widget_id"],
        "record_id": candidate["record_id"],
        "requirement_id": candidate["requirement_id"],
        "presentation_role": candidate["presentation_role"],
        "file_sha256": candidate["file_sha256"],
        "canonical_payload_sha256": candidate["canonical_payload_sha256"],
        "display_projection": candidate["display_projection"],
    }
    plan_ref = "extensions/G-0001/artifact-plan.json"
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[entry],
        reviewer_ref="tests/artifact-plan",
        fixture_ref=fixture_ref,
        presentation_plan_ref=plan_ref,
    )
    assert plan["input_items"] == inventory["input_items"]
    receipt = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="artifact-plan-dashboard",
        presentation_plan_ref=plan_ref,
    )
    assert receipt["input_items"] == plan["input_items"]
    fixture = json.loads(
        context.resolve_run_path(receipt["outputs"]["fixture_ref"]).read_text(encoding="utf-8")
    )
    assert fixture["analytical_artifacts"] == [artifact_binding]

    # A plan may not substitute another typed artifact while retaining the
    # accepted/integration hashes.  The strict binding check rejects it before
    # any output namespace is staged.
    tampered = copy.deepcopy(plan)
    tampered["input_items"][0]["analytical_artifacts"][0]["content_hash"] = "0" * 64
    tampered_ref = "extensions/G-0001/artifact-plan-tampered.json"
    tampered_path = context.resolve_run_path(tampered_ref)
    tampered_path.parent.mkdir(parents=True, exist_ok=True)
    tampered_path.write_bytes(dashboard_assembler._canonical_bytes(tampered))
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="input bindings drifted"):
        dashboard_assembler.assemble_dashboard(
            context,
            output_dir="artifact-plan-tampered-dashboard",
            presentation_plan_ref=tampered_ref,
        )


def test_presentation_plan_input_bindings_are_order_insensitive_but_exact(tmp_path: Path) -> None:
    """Supervisor/display order must not weaken per-item input binding checks."""

    source = tmp_path / "source"
    context = _seed_run(source)
    dashboard_assembler.assemble_dashboard(context, output_dir="inventory_source")
    fixture_ref = "products/inventory_source/dashboard_fixture_v4.json"
    inventory = dashboard_assembler.business_presentation_inventory(context, fixture_ref=fixture_ref)
    candidate = next(value for value in inventory["candidates"] if value.get("record_id") == "metric-scalar")
    entry = {
        "widget_id": candidate["widget_id"],
        "record_id": candidate["record_id"],
        "requirement_id": candidate["requirement_id"],
        "presentation_role": candidate["presentation_role"],
        "file_sha256": candidate["file_sha256"],
        "canonical_payload_sha256": candidate["canonical_payload_sha256"],
        "display_projection": candidate["display_projection"],
    }
    plan = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[entry],
        reviewer_ref="tests/order-insensitive-plan",
        fixture_ref=fixture_ref,
        presentation_plan_ref="extensions/G-0001/order-insensitive-plan.json",
    )

    # Add a second exact binding solely to exercise the cumulative parent
    # order boundary.  The public writer remains responsible for the real
    # supervisor item set; this direct validator test isolates its binding
    # invariant without manufacturing another accepted run.
    first = dict(plan["input_items"][0])
    second = dict(first)
    second["item_id"] = "REQ-B"
    plan = json.loads(json.dumps(plan))
    plan["item_order"] = ["REQ-A", "REQ-B"]
    plan["input_items"] = [first, second]
    reversed_parent_order = [second, first]
    dashboard_assembler._validate_v2_plan_lineage(
        context,
        plan,
        generation_id=plan["generation_id"],
        supervisor_ref=plan["supervisor_plan_ref"],
        input_items=reversed_parent_order,
        parent=plan["parent"],
    )
    # An exact retry is stable, including when the parent presents the same
    # bindings in its other valid order.
    dashboard_assembler._validate_v2_plan_lineage(
        context,
        plan,
        generation_id=plan["generation_id"],
        supervisor_ref=plan["supervisor_plan_ref"],
        input_items=reversed_parent_order,
        parent=plan["parent"],
    )

    invalid_cases = {
        "duplicate": [first, first],
        "missing": [first],
        "foreign": [first, {**second, "item_id": "REQ-FOREIGN"}],
        "hash": [first, {**second, "accepted_content_hash": "0" * 64}],
        "count": [first, {**second, "record_count": second["record_count"] + 1}],
    }
    for name, bindings in invalid_cases.items():
        with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="input bindings drifted"):
            dashboard_assembler._validate_v2_plan_lineage(
                context,
                plan,
                generation_id=plan["generation_id"],
                supervisor_ref=plan["supervisor_plan_ref"],
                input_items=bindings,
                parent=plan["parent"],
            )


def test_presentation_json_pointer_escapes_are_strict() -> None:
    root = {"payload": {"a/b": {"m~n": "exact"}}}
    assert dashboard_assembler._presentation_pointer_value(root, "/payload/a~1b/m~0n") == "exact"
    for pointer in ("/payload/a~0b~2", "/payload/a~", "/payload/a~x", "/payload"):
        with pytest.raises(dashboard_assembler.BusinessPresentationPlanError):
            dashboard_assembler._presentation_pointer_value(root, pointer)


def test_dynamic_v2_visual_universe_and_predecessor_contract_are_strict() -> None:
    """Successor plans derive visuals from the fixture and bind both prior envelopes."""

    widgets = {
        "fact-bar": {"id": "fact-bar", "type": "bar", "dashboard_fact": True},
        "fact-table": {"id": "fact-table", "type": "table", "dashboard_fact": True},
        "raw-table": {"id": "raw-table", "type": "table"},
    }
    charts = {
        "fact-bar": {"id": "fact-bar", "type": "bar"},
        "fact-table": {"id": "fact-table", "type": "table"},
        "raw-table": {"id": "raw-table", "type": "table"},
    }
    # Ordinary tables remain audit records; an explicitly reviewed table fact
    # is a visual contract and joins the generation's dynamic visual universe.
    assert dashboard_assembler._true_visual_ids(widgets, charts) == ["fact-bar", "fact-table"]

    def visual(widget_id: str, audience: str, visual_type: str, family: str, digest: str) -> dict[str, object]:
        return {
            "widget_id": widget_id,
            "requirement_id": "REQ-A",
            "record_ids": [f"{widget_id}-record"],
            "presentation_audience": audience,
            "visual_type": visual_type,
            "chart_family": family,
            "widget_snapshot_sha256": digest * 64,
            "chart_entry_sha256": ("a" if digest == "b" else "b") * 64,
            "allowed_visual_fields": ["type", "family"],
            "title_projection": {"pointer": "/widget_snapshot/title", "value": widget_id},
            "visual_projection": {
                "type": {"pointer": "/chart_entry/type", "value": visual_type},
                "family": {"pointer": "/chart_entry/family", "value": family},
            },
        }

    bar_visual = visual("fact-bar", "business_manager", "bar", "horizontal_bar", "b")
    audit_visual = visual("fact-table", "technical_audit_gallery", "table", "table", "c")
    legacy_entry = {
        "widget_id": "legacy-claim",
        "record_id": "legacy-claim-record",
        "requirement_id": "REQ-A",
        "presentation_role": "finding_list",
        "file_sha256": "d" * 64,
        "canonical_payload_sha256": "e" * 64,
        "display_projection": {
            "title": {"pointer": "/payload/title", "value": "Reviewed claim"},
            "body": {"pointer": "/payload/body", "value": "Reviewed body"},
        },
    }
    bar_manager = dashboard_assembler._v2_manager_entry_from_visual(bar_visual)
    plan = {
        "schema_version": dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": "RUN-ASSEMBLER-TEST",
        "generation_id": "G-0002",
        "supervisor_plan_ref": "extensions/G-0002/requirement_supervisor_plan.json",
        "supervisor_plan_sha256": "f" * 64,
        "item_order": ["REQ-A"],
        "input_items": [{
            "item_id": "REQ-A",
            "accepted_content_hash": "1" * 64,
            "accepted_manifest_hash": "2" * 64,
            "integration_manifest_hash": "3" * 64,
            "record_count": 2,
            "analytical_artifacts": [],
        }],
        "parent": None,
        "reviewer_ref": "tests/dynamic-v2",
        "manager_widget_ids": ["legacy-claim", "fact-bar"],
        "manager_entries": [legacy_entry, bar_manager],
        "manager_visual_widget_ids": ["fact-bar"],
        "audit_visual_widget_ids": ["fact-table"],
        "visual_entries": [bar_visual, audit_visual],
        "source_bindings": {
            "fixture_ref": "products/G-0002/dashboard/dashboard_fixture_v4.json",
            "fixture_sha256": "4" * 64,
            "chart_map_ref": "products/G-0002/dashboard/dashboard_chart_map_v4.json",
            "chart_map_sha256": "5" * 64,
            # Full V2 predecessor contract, including visual envelopes.
            "previous_plan_manager_widget_ids": ["legacy-claim", "fact-bar"],
            "previous_plan_manager_entries": [copy.deepcopy(legacy_entry), copy.deepcopy(bar_manager)],
            "previous_manager_visual_widget_ids": ["fact-bar"],
            "previous_audit_visual_widget_ids": ["fact-table"],
            "previous_visual_entries": [copy.deepcopy(bar_visual), copy.deepcopy(audit_visual)],
        },
    }
    dashboard_assembler._validate_presentation_plan_v2_shape(plan)

    # A successor may deliberately replace/reorder the manager surface.  The
    # predecessor manager envelope remains metadata for CAS binding, not a
    # membership/order constraint on the candidate.
    successor_selection = copy.deepcopy(plan)
    successor_selection["manager_widget_ids"] = ["fact-bar"]
    successor_selection["manager_entries"] = [copy.deepcopy(bar_manager)]
    dashboard_assembler._validate_presentation_plan_v2_shape(successor_selection)

    tampered_manager = copy.deepcopy(plan)
    tampered_manager["source_bindings"]["previous_plan_manager_entries"][1]["chart_entry_sha256"] = "9" * 64
    dashboard_assembler._validate_presentation_plan_v2_shape(tampered_manager)

    tampered_visual = copy.deepcopy(plan)
    tampered_visual["source_bindings"]["previous_visual_entries"][0]["visual_projection"]["type"]["value"] = "column"
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="predecessor visual drifted"):
        dashboard_assembler._validate_presentation_plan_v2_shape(tampered_visual)


def test_v2_successor_selection_repartitions_visuals_and_changes_recipe(tmp_path: Path) -> None:
    """A real successor selection can replace the manager surface under CAS."""

    root = tmp_path / "run"
    context = _seed_run(root)
    dashboard_assembler.assemble_dashboard(context, output_dir="inventory_source")
    fixture_ref = "products/inventory_source/dashboard_fixture_v4.json"
    chart_map_ref = "products/inventory_source/dashboard_chart_map_v4.json"
    fixture_path = context.resolve_run_path(fixture_ref)
    chart_map_path = context.resolve_run_path(chart_map_ref)

    # Add one exact source-bound table visual with several eligible recipes so
    # the successor can choose a different recipe/layout/renderer without
    # changing any underlying record or chart value.
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    chart_map = json.loads(chart_map_path.read_text(encoding="utf-8"))
    original_widget = next(widget for widget in fixture["widgets"] if widget["id"] == "REQ-A-metric-scalar")
    alternate_widget = copy.deepcopy(original_widget)
    alternate_widget.update({
        "id": "REQ-A-metric-scalar-alt",
        "type": "table",
        "title": "Alternate scalar",
        "dashboard_fact": True,
        "presentation_role": "decision_view",
        "rows": [{"label": "Reviewed scalar", "value": 7, "width": 7}],
    })
    fixture["widgets"].append(alternate_widget)
    original_chart = next(chart for chart in chart_map["charts"] if chart["id"] == original_widget["id"])
    alternate_chart = copy.deepcopy(original_chart)
    alternate_chart.update({"id": alternate_widget["id"], "type": "table", "family": "table"})
    alternate_fields = copy.deepcopy(alternate_chart["fields_or_values_used"])
    alternate_fields["rows"] = copy.deepcopy(alternate_widget["rows"])
    alternate_chart["fields_or_values_used"] = alternate_fields
    chart_map["charts"].append(alternate_chart)
    fixture_path.write_bytes(dashboard_assembler._canonical_bytes(fixture))
    chart_map_path.write_bytes(dashboard_assembler._canonical_bytes(chart_map))

    inventory = dashboard_assembler.business_presentation_inventory(context, fixture_ref=fixture_ref)
    candidates = {entry["widget_id"]: entry for entry in inventory["candidates"]}

    def manager_entry(widget_id: str, **choices: Any) -> dict[str, Any]:
        candidate = candidates[widget_id]
        result = {
            key: copy.deepcopy(candidate[key])
            for key in (
                "widget_id", "record_id", "requirement_id", "presentation_role",
                "file_sha256", "canonical_payload_sha256", "display_projection",
            )
        }
        result.update(choices)
        return result

    predecessor_ref = "extensions/G-0001/successor-selection.json"
    predecessor = dashboard_assembler.write_business_presentation_plan(
        context,
        manager_entries=[manager_entry("REQ-A-metric-scalar")],
        reviewer_ref="tests/successor-predecessor",
        fixture_ref=fixture_ref,
        presentation_plan_ref=predecessor_ref,
    )
    predecessor_path = context.resolve_run_path(predecessor_ref)
    predecessor_hash = _digest(predecessor_path)
    predecessor_visuals = {
        entry["widget_id"]: copy.deepcopy(entry)
        for entry in predecessor["visual_entries"]
    }
    old_manager_entry = next(
        entry for entry in predecessor["manager_entries"] if entry["widget_id"] == "REQ-A-metric-scalar"
    )

    successor_selection = manager_entry(
        "REQ-A-metric-scalar-alt",
        recipe_id="table",
        layout="wide",
        renderer_type="table",
    )
    successor = dashboard_assembler.write_business_presentation_plan_v2(
        context,
        fixture_ref=fixture_ref,
        chart_map_ref=chart_map_ref,
        previous_plan_ref=predecessor_ref,
        manager_entries=[successor_selection],
        reviewer_ref="tests/successor-manager",
        presentation_plan_ref=predecessor_ref,
    )
    assert successor["manager_widget_ids"] == ["REQ-A-metric-scalar-alt"]
    assert successor["manager_visual_widget_ids"] == ["REQ-A-metric-scalar-alt"]
    assert "REQ-A-metric-scalar" in successor["audit_visual_widget_ids"]
    assert successor["source_bindings"]["previous_plan_ref"] == predecessor_ref
    assert successor["source_bindings"]["previous_plan_sha256"] == predecessor_hash
    new_manager_entry = successor["manager_entries"][0]
    assert (old_manager_entry.get("recipe_id"), old_manager_entry.get("layout"), old_manager_entry.get("renderer_type")) == (
        "kpi_card", "compact", "kpi",
    )
    assert (new_manager_entry["recipe_id"], new_manager_entry["layout"], new_manager_entry["renderer_type"]) == (
        "table", "wide", "table",
    )

    successor_visuals = {entry["widget_id"]: entry for entry in successor["visual_entries"]}
    immutable_fields = (
        "requirement_id", "record_ids", "visual_type", "chart_family",
        "widget_snapshot_sha256", "chart_entry_sha256", "allowed_visual_fields",
        "title_projection", "visual_projection",
    )
    for widget_id, predecessor_visual in predecessor_visuals.items():
        current_visual = successor_visuals[widget_id]
        for field in immutable_fields:
            assert current_visual[field] == predecessor_visual[field]
    assert successor_visuals["REQ-A-metric-scalar"]["presentation_audience"] == "technical_audit_gallery"
    assert successor_visuals["REQ-A-metric-scalar-alt"]["presentation_audience"] == "business_manager"

    # The source-side predecessor snapshot is CAS-bound too; changing its
    # audience metadata must not be enough to smuggle a different predecessor
    # contract through an otherwise valid successor candidate.
    tampered_source = copy.deepcopy(successor)
    prior_audience = tampered_source["source_bindings"]["previous_visual_entries"][0]["presentation_audience"]
    tampered_source["source_bindings"]["previous_visual_entries"][0]["presentation_audience"] = (
        "technical_audit_gallery" if prior_audience == "business_manager" else "business_manager"
    )
    tampered_payload = dashboard_assembler._canonical_bytes(tampered_source)
    tampered_hash = hashlib.sha256(tampered_payload).hexdigest()
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="previous-plan visual binding"):
        dashboard_assembler.revise_business_presentation_plan_v2(
            context,
            successor_plan=tampered_source,
            expected_current_plan_sha256=predecessor_hash,
            expected_successor_plan_sha256=tampered_hash,
            presentation_plan_ref=predecessor_ref,
        )

    successor_payload = dashboard_assembler._canonical_bytes(successor)
    successor_hash = hashlib.sha256(successor_payload).hexdigest()
    revised = dashboard_assembler.revise_business_presentation_plan_v2(
        context,
        successor_plan=successor,
        expected_current_plan_sha256=predecessor_hash,
        expected_successor_plan_sha256=successor_hash,
        presentation_plan_ref=predecessor_ref,
    )
    assert revised["manager_widget_ids"] == ["REQ-A-metric-scalar-alt"]
    assert json.loads(predecessor_path.read_text(encoding="utf-8"))["manager_widget_ids"] == ["REQ-A-metric-scalar-alt"]

    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="requires explicit"):
        dashboard_assembler.write_business_presentation_plan_v2(
            context,
            fixture_ref=fixture_ref,
            chart_map_ref=chart_map_ref,
            previous_plan_ref=predecessor_ref,
            manager_entries=[manager_entry("REQ-A-metric-scalar-alt")],
            reviewer_ref="tests/missing-choice",
            presentation_plan_ref=predecessor_ref,
        )


def test_direct_v2_recorder_creates_exactly_once_and_rejects_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The active-generation V2 recorder is strict without touching live runs."""

    root = tmp_path / "run"
    context = RunContext("RUN-DIRECT-V2", root)
    generation = "G-0002"
    parent = "G-0001"
    fixture_ref = f"products/generations/{generation}/dashboard/dashboard_fixture_v4.json"
    chart_map_ref = f"products/generations/{generation}/dashboard/dashboard_chart_map_v4.json"
    previous_ref = f"extensions/{parent}/business_presentation_plan.json"
    fixture_path = root / fixture_ref
    chart_map_path = root / chart_map_ref
    previous_path = root / previous_ref
    for path in (fixture_path, chart_map_path, previous_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path == previous_path:
            path.write_bytes(
                json.dumps(
                    {"schema_version": dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA},
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
        elif path == fixture_path:
            path.write_bytes(b'{"widgets":[]}\n')
        elif path == chart_map_path:
            path.write_bytes(b'{"charts":[]}\n')
        else:
            path.write_bytes((path.name + "\n").encode("utf-8"))
    expected_fixture = _digest(fixture_path)
    expected_chart_map = _digest(chart_map_path)
    expected_previous = _digest(previous_path)
    target_ref = f"extensions/{generation}/business_presentation_plan.json"
    candidate = {
        "schema_version": dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA,
        "generation_id": generation,
        "reviewer_ref": "business-presentation-reviewer-G-0002-sol-final",
        "source_bindings": {
            "fixture_ref": fixture_ref,
            "fixture_sha256": expected_fixture,
            "chart_map_ref": chart_map_ref,
            "chart_map_sha256": expected_chart_map,
            "previous_plan_ref": previous_ref,
            "previous_plan_sha256": expected_previous,
        },
    }

    metadata = SimpleNamespace(generation_id=generation, parent_generation_id=parent)
    monkeypatch.setattr(RunLifecycle, "_read_generation_pointer_unlocked", staticmethod(lambda _context: {}))
    monkeypatch.setattr(RunLifecycle, "_load_generation_unlocked", staticmethod(lambda _context, _pointer: metadata))
    monkeypatch.setattr(dashboard_assembler, "write_business_presentation_plan_v2", lambda *_args, **_kwargs: copy.deepcopy(candidate))
    # The candidate shape is exercised by the existing dynamic V2 contract
    # test; this test isolates the public writer's lock/CAS/source behavior.
    monkeypatch.setattr(dashboard_assembler, "_validate_presentation_plan_v2_shape", lambda _plan: None)
    monkeypatch.setattr(dashboard_assembler, "_validate_business_presentation_plan_v2", lambda *_args, **_kwargs: None)

    expected_payload_hash = hashlib.sha256(dashboard_assembler._canonical_bytes(candidate)).hexdigest()
    kwargs = {
        "fixture_ref": fixture_ref,
        "chart_map_ref": chart_map_ref,
        "previous_plan_ref": previous_ref,
        "manager_entries": [],
        "reviewer_ref": candidate["reviewer_ref"],
        "expected_fixture_sha256": expected_fixture,
        "expected_chart_map_sha256": expected_chart_map,
        "expected_previous_plan_sha256": expected_previous,
        "expected_successor_plan_sha256": expected_payload_hash,
        "presentation_plan_ref": target_ref,
    }
    invalid_successor = dict(kwargs)
    invalid_successor["expected_successor_plan_sha256"] = "not-a-sha256"
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="expected successor plan hash is invalid"):
        dashboard_assembler.record_business_presentation_plan_v2(context, **invalid_successor)
    assert not (root / target_ref).exists()

    missing_successor = dict(kwargs)
    missing_successor.pop("expected_successor_plan_sha256")
    with pytest.raises(TypeError, match="expected_successor_plan_sha256"):
        dashboard_assembler.record_business_presentation_plan_v2(context, **missing_successor)
    assert not (root / target_ref).exists()

    first = dashboard_assembler.record_business_presentation_plan_v2(
        context, **kwargs
    )
    target_path = root / target_ref
    assert first == candidate
    assert target_path.read_bytes() == dashboard_assembler._canonical_bytes(candidate)
    first_mtime = target_path.stat().st_mtime_ns
    second = dashboard_assembler.record_business_presentation_plan_v2(
        context, **kwargs
    )
    assert second == candidate
    assert target_path.stat().st_mtime_ns == first_mtime

    target_path.write_bytes(b"divergent\n")
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="conflicts"):
        dashboard_assembler.record_business_presentation_plan_v2(context, **kwargs)
    assert target_path.read_bytes() == b"divergent\n"
    target_path.unlink()
    fixture_path.write_bytes(b"source drift\n")
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="fixture hash drifted"):
        dashboard_assembler.record_business_presentation_plan_v2(context, **kwargs)
    assert not target_path.exists()
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="canonical"):
        dashboard_assembler.record_business_presentation_plan_v2(
            context, **{**kwargs, "fixture_ref": "../escape.json"}
        )
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="reviewer_ref"):
        dashboard_assembler.record_business_presentation_plan_v2(
            context, **{**kwargs, "reviewer_ref": "bad reviewer"}
        )

    # The direct path is a V2 successor admission only; it must not bootstrap
    # from a V1 predecessor even when the caller supplies its current hash.
    target_path.unlink(missing_ok=True)
    fixture_path.write_bytes(b'{"widgets":[]}\n')
    previous_path.write_bytes(b'{"schema_version":"dashboard.business_presentation_plan.v1"}\n')
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="predecessor must be a V2"):
        dashboard_assembler.record_business_presentation_plan_v2(
            context,
            **{**kwargs, "expected_previous_plan_sha256": _digest(previous_path)},
        )

    # Canonical references are lexical as well as resolved: an in-root
    # symlink cannot smuggle a source or target through the run boundary.
    previous_path.write_bytes(
        json.dumps({"schema_version": dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA}, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    fixture_path.unlink()
    fixture_path.symlink_to(root / "fixture-source.json")
    (root / "fixture-source.json").write_bytes(b'{"widgets":[]}\n')
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="cannot be symlinked"):
        dashboard_assembler.record_business_presentation_plan_v2(context, **kwargs)
    fixture_path.unlink()
    fixture_path.write_bytes(b'{"widgets":[]}\n')
    target_path.symlink_to(root / "target-source.json")
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="target cannot be symlinked"):
        dashboard_assembler.record_business_presentation_plan_v2(context, **kwargs)

    target_path.unlink()
    manager_entry_path = root / "manager-entry.json"
    manager_entry_path.write_text("{}", encoding="utf-8")
    cli_args = [
        "--run-root",
        str(root),
        "--run-id",
        context.run_id,
        "--record-presentation-plan-v2",
        "--presentation-fixture-ref",
        fixture_ref,
        "--presentation-chart-map-ref",
        chart_map_ref,
        "--presentation-previous-plan-ref",
        previous_ref,
        "--reviewer-ref",
        kwargs["reviewer_ref"],
        "--expected-fixture-sha256",
        expected_fixture,
        "--expected-chart-map-sha256",
        expected_chart_map,
        "--expected-previous-plan-sha256",
        expected_previous,
        "--presentation-plan-ref",
        target_ref,
        "--manager-entry-json",
        str(manager_entry_path),
    ]
    missing_cli_hash = list(cli_args)
    # argparse does not own this validation; main returns its stable process
    # error code after the direct recorder rejects missing input.
    assert dashboard_assembler.main(missing_cli_hash) == 2
    assert not target_path.exists()
    cli_args.extend(["--expected-successor-plan-sha256", expected_payload_hash])
    rc = dashboard_assembler.main(cli_args)
    assert rc == 0
    assert json.loads(target_path.read_text(encoding="utf-8")) == candidate



def test_current_g3_dashboard_audit_is_lossless_record_level_union() -> None:
    run_root = ROOT.parent / "benchmark_a_requirement_v070_entity_run_a_rerun_1"
    state_path = run_root / "run_state.json"
    fixture_path = run_root / "products/generations/G-0003/dashboard/dashboard_fixture_v4.json"
    if not state_path.is_file() or not fixture_path.is_file():
        pytest.skip("current benchmark G3 fixture is not available in this checkout")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    context = RunContext(state["run_id"], run_root)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    records_by_item: dict[str, list[dict[str, object]]] = {}
    for item_id in context.resolve_run_path("requirements").iterdir():
        if not item_id.is_dir():
            continue
        item = item_id.name
        try:
            content, accepted_manifest, accepted_meta = dashboard_assembler._load_public_accepted_bundle(context, item)
            _manifest, records = dashboard_assembler._load_committed_records(context, item, accepted_manifest, accepted_meta["bundle"])
        except dashboard_assembler.AssemblyError:
            continue
        records_by_item[item] = [dict(record) for record in records]
    audit = dashboard_assembler._audit_record_entries(records_by_item, fixture["widgets"])
    assert len(audit) == 291
    assert len({entry["record_id"] for entry in audit}) == len(audit)
    for entry in audit:
        payload = entry["payload"]
        assert json.loads(entry["payload_json"]) == payload
        assert entry["committed_record_payload"] == payload
        assert "widget_snapshot" not in entry
        assert set(entry["reference_union"]) == set(entry["evidence_refs"]) | set(entry["trace_refs"])
        assert entry["widget_ids"]
    widget_audit = dashboard_assembler._audit_widget_entries(fixture["widgets"])
    assert len(widget_audit) == 276
    assert len({entry["widget_id"] for entry in widget_audit}) == len(widget_audit)
    for entry in widget_audit:
        assert entry["widget_snapshot"]["id"] == entry["widget_id"]
        assert set(entry["record_ids"]) == {
            str(value)
            for value in (entry["widget_snapshot"].get("integration_record_ids") or [entry["widget_snapshot"].get("integration_record_id")])
            if value
        }


def test_actual_six_plan_candidate_renders_each_manager_widget_once() -> None:
    candidates = sorted(Path("/tmp").glob("g3_plan_candidate.*/run/products/generations/G-0003/dashboard_manager_candidate_v2"))
    if not candidates:
        pytest.skip("actual six-entry plan candidate is not present")
    output = candidates[-1]
    fixture = json.loads((output / "dashboard_fixture_v4.json").read_text(encoding="utf-8"))
    site = output / "site"
    assert len(fixture["manager_widget_ids"]) == 6
    assert len(fixture["audit_records"]) == 291
    assert len(fixture["audit_widgets"]) == 276
    manager_pages = [site / "index.html", *sorted((site / "domains").glob("*.html"))]
    html = "".join(path.read_text(encoding="utf-8") for path in site.rglob("*.html"))
    manager_html = "".join(path.read_text(encoding="utf-8") for path in manager_pages)
    manager_ids = re.findall(r'<article class="widget manager-widget[^>]* id="([^"]+)"', html)
    assert len(manager_ids) == 6
    assert len(set(manager_ids)) == 6
    selected = {
        str(widget["id"]): widget
        for widget in fixture["widgets"]
        if widget.get("id") in fixture["manager_widget_ids"]
    }
    expected = {
        f"widget-{dashboard_renderer._slug(widget.get('manager_anchor') or widget.get('id'))}"
        for widget in selected.values()
    }
    assert set(manager_ids) == expected
    assert html.count('class="takeaway"') == 0
    assert html.count('class="requirement-subtitle"') == 0
    assert html.count("audit-record-entry") == 291
    assert html.count("audit-widget-entry") == 276
    assert html.count("class=\"raw-audit widget-snapshot\"") == 276
    visible = re.sub(r'<details\b[^>]*>.*?</details>', "", manager_html, flags=re.DOTALL | re.IGNORECASE)
    allowed_projection_values = {
        str(binding.get("value"))
        for entry in fixture["manager_entries"]
        for binding in (entry.get("display_projection") or {}).values()
        if isinstance(binding, dict) and binding.get("value") not in (None, "")
    }
    for domain in fixture["domains"]:
        for flow in domain.get("decision_flow", []):
            takeaway = flow.get("takeaway")
            if takeaway and not any(str(takeaway) in value for value in allowed_projection_values):
                assert str(takeaway) not in visible
            for limitation in flow.get("limitations", []):
                if limitation:
                    assert str(limitation) not in visible
    for limitation in fixture.get("limitations", []):
        if limitation:
            assert str(limitation) not in visible


def test_assembler_rejects_committed_status_or_hash_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    manifest_path = source / "requirements" / "REQ-A" / "integration" / "committed" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "staged"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(dashboard_assembler.AssemblyError, match="committed integration manifest"):
        dashboard_assembler.assemble_dashboard(context, output_dir="repro_bad")


def test_assembler_idempotency_rejects_output_or_input_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    fixture_path = source / "products" / "repro_dashboard_v4" / "dashboard_fixture_v4.json"
    fixture_path.write_text(fixture_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(dashboard_assembler.AssemblyError, match="output hash mismatch"):
        dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")

    second_source = tmp_path / "source_input_drift"
    second_context = _seed_run(second_source)
    dashboard_assembler.assemble_dashboard(second_context, output_dir="repro_dashboard_v4")
    answer_path = second_source / "requirements" / "REQ-A" / "accepted" / "answer_content.json"
    answer_path.write_text(answer_path.read_text(encoding="utf-8").replace("bounded", "changed"), encoding="utf-8")
    with pytest.raises(dashboard_assembler.AssemblyError, match="accepted bundle validation"):
        dashboard_assembler.assemble_dashboard(second_context, output_dir="repro_dashboard_v4")

    third_source = tmp_path / "source_site_drift"
    third_context = _seed_run(third_source)
    dashboard_assembler.assemble_dashboard(third_context, output_dir="repro_dashboard_v4")
    css_path = third_source / "products" / "repro_dashboard_v4" / "site" / "assets" / "dashboard.css"
    css_path.write_text(css_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(dashboard_assembler.AssemblyError, match="site file hash mismatch"):
        dashboard_assembler.assemble_dashboard(third_context, output_dir="repro_dashboard_v4")

    fourth_source = tmp_path / "source_plan_drift"
    fourth_context = _seed_run(fourth_source)
    dashboard_assembler.assemble_dashboard(fourth_context, output_dir="repro_dashboard_v4")
    plan_path = fourth_source / "requirement_supervisor_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["groups"][0]["title"] = "Changed grouping"
    plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    with pytest.raises(dashboard_assembler.AssemblyError, match="supervisor plan/grouping hash"):
        dashboard_assembler.assemble_dashboard(fourth_context, output_dir="repro_dashboard_v4")


def test_product_namespace_rebuild_replaces_changed_candidate_and_rolls_back_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reviewed product can be rebuilt in place without mutating its inputs."""

    source = tmp_path / "source"
    context = _seed_run(source)
    first = dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    output_root = source / "products" / "repro_dashboard_v4"
    first_tree = _tree_hashes(output_root)

    # Product review legitimately advances append-only telemetry and writes
    # the downstream manifest that points back at the first receipt.  Neither
    # may deadlock a presentation-only rebuild of unchanged reviewed inputs.
    telemetry_root = source / "telemetry"
    telemetry_root.mkdir(parents=True, exist_ok=True)
    (telemetry_root / "events.jsonl").write_text('{"event":"product_reviewed"}\n', encoding="utf-8")
    (telemetry_root / "inventory_counters.json").write_text('{}\n', encoding="utf-8")
    product_manifest = {
        "run_id": context.run_id,
        "assets": [],
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "lem": {"projection_hash": first["freeze_inputs"]["projection_hash"], "prepared_asset_count": 0},
    }
    product_manifest_path = source / "products" / "product_manifest.json"
    product_manifest_path.write_text(json.dumps(product_manifest, sort_keys=True) + "\n", encoding="utf-8")

    # Simulate a corrected presentation candidate (the accepted/integration
    # inputs are unchanged).  The product namespace is the only replacement.
    # The synthetic seed has no business subject, so force the presentation
    # admission result to change rather than relying on overview selection.
    original_admission = dashboard_assembler._manager_admission

    def admit_synthetic(item_id: str, widget: dict[str, object], *, subject_context: str = "") -> dict[str, object]:
        result = dict(original_admission(item_id, widget, subject_context=subject_context))
        result.update({"status": "admitted", "presentation_audience": "business_manager", "role": "business_outcome"})
        return result

    monkeypatch.setattr(dashboard_assembler, "_manager_admission", admit_synthetic)
    corrected = dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    corrected_tree = _tree_hashes(output_root)
    assert corrected_tree != first_tree
    assert corrected["output_hashes"] != first["output_hashes"]
    assert corrected["freeze_inputs"]["product_manifest_sha256"] == _digest(product_manifest_path)
    assert corrected["freeze_inputs"]["telemetry"]["assets"]
    assert not (source / "products" / ".repro_dashboard_v4.previous").exists()

    evidence = (output_root / "site" / "evidence.html").read_text(encoding="utf-8")
    assert "Open exact frozen artifact" in evidence
    assert "Open reviewed committed record details" in evidence
    ontology = (output_root / "site" / "ontology.html").read_text(encoding="utf-8")
    assert "Total ontology items" in ontology
    manager_pages = [output_root / "site" / "index.html", *(output_root / "site" / "domains").glob("*.html")]
    for page in manager_pages:
        page_text = page.read_text(encoding="utf-8")
        assert "Ontology projection" not in page_text
        assert "Data quality &amp; model audit" not in page_text
        assert "Evidence &amp; audit" not in page_text
    assert "Ontology projection" in (output_root / "site" / "data-quality-audit.html").read_text(encoding="utf-8")

    # Exact retry remains byte-stable and does not create another replacement.
    retry = dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    assert retry == corrected
    assert _tree_hashes(output_root) == corrected_tree

    # A candidate failure occurs before publication and leaves the corrected
    # product bytes/receipt untouched.
    before_failure = _tree_hashes(output_root)
    def fail_registry(*args: object, **kwargs: object) -> dict[str, object]:
        raise dashboard_assembler.AssemblyError("synthetic renderer failure")
    monkeypatch.setattr(dashboard_assembler, "_build_registry", fail_registry)
    with pytest.raises(dashboard_assembler.AssemblyError, match="synthetic renderer failure"):
        dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    assert _tree_hashes(output_root) == before_failure


def test_structured_metrics_and_dashboard_facts_render_without_null_kpis() -> None:
    """REQ16-shaped records remain visible as tables/status views and facts."""

    def record(record_id: str, kind: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "record_id": record_id,
            "record_hash": "a" * 64,
            "accepted_content_hash": "b" * 64,
            "evidence_refs": ["work/results/req16_analysis.json"],
            "kind": kind,
            "payload": payload,
        }

    metric_payload = {
        "metric": "sales",
        "source": "ERP",
        "as_of": "2026-08-16",
        "date_authority": "fixture-controlled snapshot",
        "distinct_unit": "material_no",
        "totals": {"accepted": 12889, "conflicts": 4},
        "distinct_material_no322": 322,
        "not_proven_conflict": True,
        "components": [{"component": "accepted", "value": 12889}, {"component": "conflict", "value": 4}],
    }
    metric = dashboard_assembler._metric_widget(
        "REQ-16",
        {},
        record("REQ16-metric-sales", "metric", metric_payload),
    )
    fact = dashboard_assembler._fact_widget(
        "REQ-16",
        {},
        record(
            "REQ16-fact-diagnostic",
            "dashboard_fact",
            {
                "title": "Corrected diagnostic categories",
                "type": "exception_table",
                "series": ["actual conflicts: 148", "unresolved: 19", "namespace differences: 606"],
                "visual_id": "REQ16-V-DIAGNOSTIC-CATEGORIES",
            },
        ),
    )
    assert metric["type"] == "table" and metric["rows"]
    context_rows = {
        row["field"]: row["value"]
        for row in metric["rows"]
        if row.get("row_kind") == "context"
    }
    for key in ("metric", "source", "as_of", "date_authority", "distinct_unit", "totals", "distinct_material_no322", "not_proven_conflict"):
        assert context_rows[key] == metric_payload[key]
    nested_rows = [row for row in metric["rows"] if row.get("row_kind") == "nested"]
    assert [(row["component"], row["value"]) for row in nested_rows] == [("accepted", 12889), ("conflict", 4)]
    assert fact is not None and fact["type"] == "status_table" and len(fact["rows"]) == 3
    widgets = [metric, fact]
    for index, widget in enumerate(widgets, 1):
        widget.update({"requirement_id": "REQ-16", "domain_id": "inventory", "requirement_order": 1})
    fixture = {
        "title": "REQ16 product regression",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": widgets,
        "domains": [{
            "id": "inventory",
            "title": "Inventory",
            "order": 1,
            "decision_flow": [{"id": "inventory-req16", "title": "REQ-16", "order": 1, "widget_ids": [widget["id"] for widget in widgets]}],
        }],
        "ontology_summary": {"ontology_items": 1, "relationships": 0, "canonical_mappings": 300, "identity_decisions": 300, "resolution_bindings": 0, "item_bindings": 1, "prepared_assets": 0, "knowledge": 101},
    }
    document, manifest = dashboard_renderer.render_dashboard(fixture)
    assert "12889" in document and "4" in document
    assert "actual conflicts: 148" in document
    assert all(item["kind"] in {"table", "status_table"} for item in manifest["items"])


def test_new_dashboard_facts_preserve_explicit_group_measure_series_points_and_rows() -> None:
    """Fact cards use reviewed semantic fields, never the first row scalar."""

    def record(record_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "record_id": record_id,
            "record_hash": "a" * 64,
            "accepted_content_hash": "b" * 64,
            "evidence_refs": ["work/results/reviewed.json"],
            "kind": "dashboard_fact",
            "payload": payload,
        }

    cases = [
        (
            "REQ-18",
            {
                "title": "Carrier delivery exposure",
                "type": "bar",
                "grouping": "carrier",
                "measure": "late_rate",
                "rows": [{"carrier": "FedEx", "late_count": 2, "late_rate": "0.2500", "population": 8}],
            },
        ),
        (
            "REQ-21",
            {
                "title": "Stock by warehouse",
                "type": "column",
                "data": [{"warehouse": "LOC-WH-01", "available": "10.00", "reserved": "2.00", "stock_rows": 1}],
            },
        ),
        (
            "REQ-18",
            {
                "title": "Cost versus lateness",
                "type": "scatter",
                "x": "transport_cost_total_source_local",
                "y": "late_rate",
                "rows": [{"carrier": "FedEx", "transport_cost_total_source_local": "42.50", "late_rate": "0.2500"}],
            },
        ),
        (
            "REQ-22",
            {
                "title": "Ordered versus shipped quantity",
                "type": "bar",
                "chart_contract": "Grouped bars compare exact ordered_quantity and shipped_quantity values.",
                "rows": [{"category": "Books", "ordered_quantity": "3092", "shipped_quantity": "2136"}],
            },
        ),
        (
            "REQ-20",
            {
                "title": "Commercial review queue",
                "type": "table",
                "rows": [{"campaign_name": "Welcome", "channel": "WEB", "currency": "AUD", "decision_signal": "Review", "source_ref": "work/results/raw.json"}],
            },
        ),
    ]
    rendered = []
    for index, (item_id, payload) in enumerate(cases, 1):
        widget = dashboard_assembler._fact_widget(item_id, {}, record(f"fact-{index}", payload))
        assert widget is not None
        rendered.append(widget)
        if payload["type"] == "bar" and payload.get("measure"):
            assert widget["bars"][0]["value"] == "0.2500"
            assert "late_count" not in widget["bars"][0]
        elif payload["type"] == "bar" and payload.get("chart_contract"):
            assert [
                {key: item[key] for key in ("label", "value")}
                for item in widget["bars"][0]["series"]
            ] == [
                {"label": "Ordered Quantity", "value": "3092"},
                {"label": "Shipped Quantity", "value": "2136"},
            ]
            assert all("size" in item for item in widget["bars"][0]["series"])
        elif payload["type"] == "column":
            assert widget["bars"][0]["label"] == "Warehouse 01"
            assert {entry["label"] for entry in widget["bars"][0]["series"]} == {"Available", "Reserved"}
        elif payload["type"] == "scatter":
            assert widget["points"] == [{"label": "FedEx", "x": "42.50", "y": "0.2500"}]
        else:
            assert widget["rows"] and "Source Ref" not in widget["rows"][0]
    bar_html = dashboard_renderer._render_visual({"type": "bar", "dashboard_fact": True, "bars": rendered[3]["bars"]})
    assert "Ordered Quantity" in bar_html and "Shipped Quantity" in bar_html
    assert "viz-row-no-geometry" not in bar_html and "viz-track" in bar_html
    assert "0.2500" in dashboard_renderer._render_visual({"type": "bar", "dashboard_fact": True, "bars": rendered[0]["bars"]})
    assert "LOC-WH-01" not in dashboard_renderer._render_visual({"type": "column", "dashboard_fact": True, "bars": rendered[1]["bars"]})


def test_dashboard_fact_geometry_is_explicit_facet_local_and_required() -> None:
    """Reviewed fact values receive deterministic geometry without recalculation."""

    def record(record_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "record_id": record_id,
            "record_hash": "a" * 64,
            "accepted_content_hash": "b" * 64,
            "evidence_refs": ["work/results/reviewed.json"],
            "kind": "dashboard_fact",
            "payload": payload,
        }

    faceted = dashboard_assembler._fact_widget(
        "REQ-FACET",
        {},
        record(
            "fact-facet",
            {
                "title": "Currency exposure",
                "type": "bar",
                "facet_by": "currency",
                "scale": "independent_per_currency",
                "rows": [
                    {"currency": "EUR", "label": "A", "value": "10"},
                    {"currency": "EUR", "label": "B", "value": "5"},
                    {"currency": "USD", "label": "A", "value": "100"},
                    {"currency": "USD", "label": "B", "value": "50"},
                    {"currency": "JPY", "label": "No exposure", "value": "0"},
                ],
            },
        ),
    )
    assert faceted is not None
    assert [(row["label"], row["value"], row["size"]) for row in faceted["bars"]] == [
        ("A", "10", "100%"),
        ("B", "5", "50%"),
        ("A", "100", "100%"),
        ("B", "50", "50%"),
        ("No exposure", "0", "0%"),
    ]
    assert all(row["geometry_basis"] == "independent facet max normalization" for row in faceted["bars"])
    assert "A: 10" in dashboard_renderer._render_visual(faceted)
    assert "viz-row-no-geometry" not in dashboard_renderer._render_visual(faceted)

    grouped = dashboard_assembler._fact_widget(
        "REQ-GROUPED",
        {},
        record(
            "fact-grouped",
            {
                "title": "Ordered versus shipped",
                "type": "column",
                "rows": [{"category": "Books", "ordered": "10", "shipped": "5"}],
            },
        ),
    )
    assert grouped is not None
    series = grouped["bars"][0]["series"]
    assert [(entry["label"], entry["value"], entry["size"]) for entry in series] == [
        ("Ordered", "10", "100%"),
        ("Shipped", "5", "50%"),
    ]
    grouped_html = dashboard_renderer._render_visual(grouped)
    assert "column-series-track" in grouped_html
    assert "Ordered" in grouped_html and "Shipped" in grouped_html
    # Mixed reviewed measures carry explicit scale groups.  The renderer must
    # expose one labeled, independently scaled panel per group even when no
    # prose limitation mentions separate axes.
    mixed = {
        "id": "mixed-scales",
        "type": "bar",
        "bars": [{
            "label": "Reviewed totals",
            "series": [
                {"label": "Orders", "value": "10", "size": "100%", "scale_group": "count", "scale_domain": "count-local"},
                {"label": "Revenue", "value": "1000", "size": "100%", "scale_group": "amount", "scale_domain": "currency-local"},
            ],
        }],
    }
    mixed_html = dashboard_renderer._render_visual(mixed)
    assert mixed_html.count("scale-group-panel") == 2
    assert 'data-scale-group="count"' in mixed_html
    assert 'data-scale-group="amount"' in mixed_html
    assert "Count" in mixed_html and "Amount" in mixed_html
    assert "Orders" in mixed_html and "10" in mixed_html
    assert "Revenue" in mixed_html and "1000" in mixed_html
    declared = {
        "id": "declared-scales",
        "type": "column",
        "scale_groups": {"orders": ["Orders"], "revenue": ["Revenue"]},
        "bars": [{
            "label": "Reviewed totals",
            "series": [
                {"label": "Orders", "value": "10", "size": "100%"},
                {"label": "Revenue", "value": "1000", "size": "100%"},
            ],
        }],
    }
    declared_html = dashboard_renderer._render_visual(declared)
    assert declared_html.count("scale-group-panel") == 2
    assert 'data-scale-group="orders"' in declared_html
    assert 'data-scale-group="revenue"' in declared_html
    with pytest.raises(ValueError, match="requires supplied size"):
        dashboard_renderer._render_visual(
            {"id": "missing-geometry", "type": "bar", "dashboard_fact": True, "bars": [{"label": "A", "value": 1}]}
        )


def test_req21_mixed_fact_scale_groups_are_unit_safe_and_unknown_mixes_fail_closed() -> None:
    """REQ21-shaped mixed series never pool rates, amounts, counts, and quantities."""

    def record(record_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "record_id": record_id,
            "record_hash": "a" * 64,
            "accepted_content_hash": "b" * 64,
            "evidence_refs": ["work/results/reviewed.json"],
            "kind": "dashboard_fact",
            "payload": payload,
        }

    cases = [
        (
            "fact01",
            {
                "type": "bar",
                "facet_scale": "independent_per_currency",
                "data": [
                    {"category": "A", "channel": "WEB", "currency": "EUR", "fill_rate": "0.4508", "line_value_source_local": "100", "qty_ordered": "10", "qty_shipped": "5"},
                    {"category": "B", "channel": "WEB", "currency": "EUR", "fill_rate": "0.9000", "line_value_source_local": "50", "qty_ordered": "20", "qty_shipped": "10"},
                    {"category": "A", "channel": "WEB", "currency": "USD", "fill_rate": "0.2500", "line_value_source_local": "10", "qty_ordered": "2", "qty_shipped": "1"},
                ],
            },
        ),
        (
            "fact04",
            {
                "type": "bar",
                "data": [
                    {"vendor": "VEND-000003", "currency": "USD", "late_or_open_count": 16, "open_qty": "1127", "ordered_qty": "5441", "po_amount_source_local": "719162.35", "received_qty": "4314"},
                    {"vendor": "VEND-000010", "currency": "USD", "late_or_open_count": 8, "open_qty": "500", "ordered_qty": "3000", "po_amount_source_local": "300000.00", "received_qty": "2500"},
                ],
            },
        ),
        (
            "fact05",
            {
                "type": "bar",
                "data": [
                    {"carrier": "DHL", "currency": "unavailable_in_source", "freight_cost_source_local": "12883.86", "late_count": 23, "on_time_count": 986, "on_time_rate": "0.9772", "rate_denominator": 1009},
                    {"carrier": "UPS", "currency": "unavailable_in_source", "freight_cost_source_local": "12714.51", "late_count": 11, "on_time_count": 998, "on_time_rate": "0.9891", "rate_denominator": 1009},
                ],
            },
        ),
        (
            "fact06",
            {
                "type": "column",
                "facet_scale": "independent_per_currency",
                "data": [
                    {"currency": "EUR", "invoice_rows_with_receipts": 1130, "invoice_total_source_local": "200", "outstanding_amount_source_local": "50", "paid_amount_source_local": "150"},
                    {"currency": "USD", "invoice_rows_with_receipts": 571, "invoice_total_source_local": "100", "outstanding_amount_source_local": "20", "paid_amount_source_local": "80"},
                ],
            },
        ),
    ]
    widgets = [dashboard_assembler._fact_widget("REQ-21", {}, record(record_id, payload)) for record_id, payload in cases]
    assert all(widget is not None for widget in widgets)

    fact01_series = widgets[0]["bars"][0]["series"]
    assert {entry["scale_group"] for entry in fact01_series} == {"rate", "amount", "quantity"}
    assert next(entry for entry in fact01_series if entry["scale_group"] == "rate")["size"] == "45.08%"
    assert next(entry for entry in fact01_series if entry["scale_group"] == "amount")["size"] == "100%"
    assert next(entry for entry in fact01_series if entry["scale_group"] == "quantity")["size"] == "50%"

    fact04_series = widgets[1]["bars"][0]["series"]
    assert {entry["scale_group"] for entry in fact04_series} == {"count", "quantity", "amount"}
    assert next(entry for entry in fact04_series if entry["scale_group"] == "count")["size"] == "100%"
    assert next(entry for entry in fact04_series if entry["scale_group"] == "amount")["scale_facet"] == "USD"

    fact05_series = widgets[2]["bars"][0]["series"]
    assert next(entry for entry in fact05_series if entry["scale_group"] == "rate")["size"] == "97.72%"
    assert next(entry for entry in fact05_series if entry["scale_group"] == "amount")["scale_group"] == "amount"
    assert next(entry for entry in fact05_series if entry["scale_group"] == "count")["scale_group"] == "count"

    fact06_series = widgets[3]["bars"][0]["series"]
    assert {entry["scale_group"] for entry in fact06_series} == {"count", "amount"}
    assert next(entry for entry in fact06_series if entry["scale_group"] == "count")["size"] == "100%"
    amount = [entry for entry in fact06_series if entry["scale_group"] == "amount"]
    assert {entry["scale_facet"] for entry in amount} == {"EUR"}

    with pytest.raises(ValueError, match="without an accepted scale classification"):
        dashboard_assembler._fact_widget(
            "REQ-21",
            {},
            record(
                "unknown-mixed",
                {"type": "bar", "rows": [{"label": "A", "foo": 2, "bar": 3}]},
            ),
        )


def test_accepted_claims_render_exact_text_with_evidence_and_provenance() -> None:
    claim_text = (
        "Supplier differences are 606 rows across 322 distinct material_no values: "
        "581/297 PORTAL-MAT plus 25/25 OLD-SKU; both are unresolved namespace/legacy "
        "differences, not proven conflicts; total diagnostic population is 773 rows."
    )
    claim_record = {
        "record_id": "REQ16-claim-04",
        "record_hash": "a" * 64,
        "accepted_content_hash": "b" * 64,
        "evidence_refs": [
            "work/results/req16_analysis.json",
            "work/results/req16_exception_queue.json",
            "work/results/req16_source_local_summary.json",
        ],
        "kind": "claim",
        "payload": {
            "claim_id": "REQ16-claim-04",
            "claim": claim_text,
            "status": "accepted",
            "period": "2026-08-16",
            "population": 773,
            "source": "reviewed diagnostic output",
        },
    }
    widgets = dashboard_assembler._build_widgets("REQ16", {}, [claim_record])
    assert len(widgets) == 1
    claim_widget = widgets[0]
    assert claim_widget["type"] == "table"
    assert claim_widget["rows"] == [{
        "claim": claim_text,
        "period": "2026-08-16",
        "status": "accepted",
    }]
    assert claim_widget["audit_payload"]["claim"] == claim_text
    assert claim_widget["integration_record_id"] == "REQ16-claim-04"
    assert claim_widget["evidence_refs"] == [
        "work/results/req16_analysis.json",
        "work/results/req16_exception_queue.json",
        "work/results/req16_source_local_summary.json",
    ]
    claim_widget.update({"requirement_id": "REQ16", "domain_id": "diagnostics", "requirement_order": 1})
    fixture = {
        "title": "REQ16 claim visibility",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": [claim_widget],
        "domains": [{
            "id": "diagnostics",
            "title": "Diagnostics",
            "order": 1,
            "decision_flow": [{"id": "diagnostics-req16", "title": "REQ16", "order": 1, "widget_ids": [claim_widget["id"]]}],
        }],
    }
    document, manifest = dashboard_renderer.render_dashboard(fixture)
    assert claim_text in document
    audit_entry = dashboard_assembler._audit_widget_entries([claim_widget])
    audit_html, _ = dashboard_renderer._render_widget_audit(audit_entry)
    assert claim_text in audit_html
    assert manifest["items"][0]["kind"] == "table"
    assert manifest["items"][0]["evidence_refs"] == [
        "work/results/req16_analysis.json",
        "work/results/req16_exception_queue.json",
        "work/results/req16_source_local_summary.json",
    ]


def test_renderer_renders_each_selected_claim_shaped_widget_without_aggregation() -> None:
    claims = [
        {
            "id": "claim-one",
            "type": "table",
            "title": "Claim one",
            "requirement_id": "REQ-CLAIMS",
            "presentation_role": "decision_view",
            "presentation_tier": "primary",
            "presentation_audience": "business_manager",
            "manager_admission": {
                "status": "admitted",
                "presentation_audience": "business_manager",
            },
            "rows": [{"claim": "First reviewed claim", "status": "accepted"}],
            "reviewed_item_ref": "requirements/REQ-CLAIMS/accepted/manifest.json",
            "reviewed_output_ref": "requirements/REQ-CLAIMS/accepted/answer_content.json",
            "evidence_refs": ["requirements/REQ-CLAIMS/accepted/answer_content.json"],
            "trace_refs": ["requirements/REQ-CLAIMS/accepted/answer_content.json"],
        },
        {
            "id": "claim-two",
            "type": "table",
            "title": "Claim two",
            "requirement_id": "REQ-CLAIMS",
            "presentation_role": "decision_view",
            "presentation_tier": "primary",
            "presentation_audience": "business_manager",
            "manager_admission": {
                "status": "admitted",
                "presentation_audience": "business_manager",
            },
            "rows": [{"claim": "Second reviewed claim", "status": "accepted"}],
            "reviewed_item_ref": "requirements/REQ-CLAIMS/accepted/manifest.json",
            "reviewed_output_ref": "requirements/REQ-CLAIMS/accepted/answer_content.json",
            "evidence_refs": ["requirements/REQ-CLAIMS/accepted/answer_content.json"],
            "trace_refs": ["requirements/REQ-CLAIMS/accepted/answer_content.json"],
        },
    ]
    fixture = {
        "title": "Selected claims",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": claims,
        "domains": [{
            "id": "claims",
            "title": "Claims",
            "order": 1,
            "decision_flow": [{
                "id": "claims-flow",
                "title": "Claims",
                "order": 1,
                "widget_ids": ["claim-one", "claim-two"],
            }],
        }],
        "audit_records": [],
    }
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    domain = pages["domains/claims.html"]
    domain_text = domain.decode("utf-8") if isinstance(domain, bytes) else domain
    visible = re.sub(r"<details\b[^>]*>.*?</details>", "", domain_text, flags=re.DOTALL)

    assert 'id="widget-claim-one"' in visible
    assert 'id="widget-claim-two"' in visible
    assert visible.count("Claim one") == 1
    assert visible.count("Claim two") == 1
    assert "First reviewed claim" in visible
    assert "Second reviewed claim" in visible
    assert "Reviewed findings" not in visible


def test_req16_claims_do_not_evict_metrics_facts_or_relationship() -> None:
    claim_04 = (
        "Supplier differences are 606 rows across 322 distinct material_no values: "
        "581/297 PORTAL-MAT plus 25/25 OLD-SKU; both are unresolved namespace/legacy "
        "differences, not proven conflicts; total diagnostic population is 773 rows."
    )

    def record(record_id: str, kind: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "record_id": record_id,
            "record_hash": "a" * 64,
            "accepted_content_hash": "b" * 64,
            "evidence_refs": ["work/results/req16_analysis.json"],
            "kind": kind,
            "payload": payload,
        }

    records = [
        record(
            f"REQ16-metric-{index}",
            "metric",
            {"label": f"Metric {index}", "components": [{"label": "reviewed", "value": index}]},
        )
        for index in range(1, 5)
    ]
    records.extend(
        record(
            f"REQ16-fact-{index}",
            "dashboard_fact",
            {"title": f"Fact {index}", "type": "status_table", "rows": [{"label": "status", "value": "reviewed"}]},
        )
        for index in range(1, 4)
    )
    for index in range(1, 6):
        records.append(
            record(
                f"REQ16-claim-{index:02d}",
                "claim",
                {"claim_id": f"REQ16-claim-{index:02d}", "claim": claim_04 if index == 4 else f"Reviewed conclusion {index}.", "status": "accepted"},
            )
        )
    records.append(
        record(
            "REQ16-relationship",
            "relationship",
            {"relationship_id": "REQ16-relationship", "source_id": "material", "target_id": "namespace", "limitations": ["reviewed only"]},
        )
    )

    widgets = dashboard_assembler._build_widgets("REQ16", {}, records)
    assert len(widgets) == 13
    assert {widget["integration_record_id"] for widget in widgets} == {
        *(f"REQ16-metric-{index}" for index in range(1, 5)),
        *(f"REQ16-fact-{index}" for index in range(1, 4)),
        *(f"REQ16-claim-{index:02d}" for index in range(1, 6)),
        "REQ16-relationship",
    }
    assert {widget["integration_record_id"] for widget in widgets if widget.get("integration_record_id", "").startswith("REQ16-metric-")} == {
        f"REQ16-metric-{index}" for index in range(1, 5)
    }
    assert {widget["integration_record_id"] for widget in widgets if widget.get("dashboard_fact") is True} == {
        f"REQ16-fact-{index}" for index in range(1, 4)
    }
    assert any(widget["id"] == "REQ16-relationship-coverage" for widget in widgets)
    assert any(
        widget.get("audit_payload", {}).get("claim") == claim_04
        for widget in widgets
        if widget.get("integration_record_id") == "REQ16-claim-04"
    )
    for widget in widgets:
        widget.update({"requirement_id": "REQ16", "domain_id": "diagnostics", "requirement_order": 1})
    fixture = {
        "title": "REQ16 complete reviewed outputs",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": widgets,
        "domains": [{
            "id": "diagnostics",
            "title": "Diagnostics",
            "order": 1,
            "decision_flow": [{"id": "diagnostics-req16", "title": "REQ16", "order": 1, "widget_ids": [widget["id"] for widget in widgets]}],
        }],
    }
    document, manifest = dashboard_renderer.render_dashboard(fixture)
    assert len(manifest["items"]) == len(widgets) == len({item["element_id"] for item in manifest["items"]})
    assert claim_04 in document
    assert "http://" not in document and "https://" not in document

    pages, _site_manifest = dashboard_renderer.render_dashboard_site(fixture)
    audit_page = pages["data-quality-audit.html"]
    audit_text = audit_page.decode("utf-8") if isinstance(audit_page, bytes) else audit_page
    # This planless fixture renders every supplied widget on the manager page;
    # the audit page remains available for provenance but does not invent an
    # omitted-plan partition.
    assert "Technical audit records" in audit_text


def test_sibling_qa_does_not_change_site_binding_but_site_mutation_rejects_retry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    first = dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    output_root = source / "products" / "repro_dashboard_v4"
    qa_root = output_root / "qa"
    qa_root.mkdir()
    for index in range(14):
        (qa_root / f"capture-{index:02d}.png").write_bytes(b"synthetic-png")
    (qa_root / "qa_report.json").write_text(
        json.dumps({"status": "pass", "captures": 14}, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    retry = dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")
    assert retry["output_hashes"] == first["output_hashes"]
    assert retry["site_binding"] == first["site_binding"]
    assert len(list(qa_root.glob("*.png"))) == 14
    assert (qa_root / "qa_report.json").is_file()
    assert not (output_root / "site" / "qa").exists()

    index_path = output_root / "site" / "index.html"
    index_path.write_text(index_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(dashboard_assembler.AssemblyError, match="site file hash mismatch"):
        dashboard_assembler.assemble_dashboard(context, output_dir="repro_dashboard_v4")


def test_assembler_honors_custom_refs_and_never_reads_work_or_calculation_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def guarded_read_bytes(path: Path) -> bytes:
        if (path.is_relative_to(context.run_root) and any(part in {"work", "calculations"} for part in path.relative_to(context.run_root).parts)):
            raise AssertionError(f"assembler attempted forbidden read: {path}")
        return original_read_bytes(path)

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if (path.is_relative_to(context.run_root) and any(part in {"work", "calculations"} for part in path.relative_to(context.run_root).parts)):
            raise AssertionError(f"assembler attempted forbidden read: {path}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    receipt = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="repro_dashboard_custom",
        fixture_ref="repro_dashboard_custom/custom/fixture.json",
        chart_map_ref="repro_dashboard_custom/custom/map.json",
        chart_registry_ref="repro_dashboard_custom/custom/registry.json",
        site_ref="repro_dashboard_custom/custom/site",
        receipt_ref="repro_dashboard_custom/custom/receipt.json",
    )
    root = source / "products" / "repro_dashboard_custom" / "custom"
    assert (root / "fixture.json").is_file()
    assert (root / "map.json").is_file()
    assert (root / "registry.json").is_file()
    assert (root / "site" / "index.html").is_file()
    assert receipt["outputs"]["fixture_ref"] == "products/repro_dashboard_custom/custom/fixture.json"


def test_assembler_normalizes_optional_products_prefix_without_doubling(tmp_path: Path) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    receipt = dashboard_assembler.assemble_dashboard(context, output_dir="products/repro_dashboard_prefixed")
    assert receipt["outputs"]["fixture_ref"] == "products/repro_dashboard_prefixed/dashboard_fixture_v4.json"
    assert (source / "products" / "repro_dashboard_prefixed").is_dir()
    assert not (source / "products" / "products" / "repro_dashboard_prefixed").exists()


def test_dashboard_fact_only_merges_whitelisted_presentation_fields() -> None:
    content = {"limitations": ["authoritative limitation"]}
    record = {
        "record_id": "fact-record",
        "record_hash": "e" * 64,
        "accepted_content_hash": "f" * 64,
        "evidence_refs": ["accepted/evidence.json"],
        "payload": {
            "label": "Authoritative fact",
            "units": "rows",
            "period": "2024",
            "population": 10,
            "limitations": ["record limitation"],
            "widget": {
                "id": "evil-id",
                "type": "kpi",
                "title": "Safe title",
                "value": 9,
                "reviewed_item_ref": "evil-item",
                "reviewed_output_ref": "evil-output",
                "evidence_refs": ["evil-evidence"],
                "trace_refs": ["evil-trace"],
                "integration_record_hash": "evil-hash",
                "review_status": "rejected",
                "limitations": ["evil limitation"],
                "unit": "evil units",
                "period": "evil period",
                "population": 999,
            },
        },
    }
    widget = dashboard_assembler._fact_widget("REQ-A", content, record)
    assert widget is not None
    assert widget["id"] == "REQ-A-fact-record"
    assert widget["title"] == "Safe title"
    assert widget["value"] == 9
    assert widget["reviewed_item_ref"] == "requirements/REQ-A/accepted/manifest.json"
    assert widget["reviewed_output_ref"] == "requirements/REQ-A/accepted/answer_content.json"
    assert widget["evidence_refs"] == ["accepted/evidence.json"]
    assert widget["trace_refs"] == [
        "requirements/REQ-A/accepted/manifest.json",
        "requirements/REQ-A/accepted/answer_content.json",
        "requirements/REQ-A/integration/committed/manifest.json",
        "requirements/REQ-A/integration/committed/records.jsonl",
    ]
    assert widget["integration_record_hash"] == "e" * 64
    assert widget["review_status"] == "accepted_and_integrated"
    assert widget["limitations"] == ["authoritative limitation", "record limitation"]
    assert widget["unit"] == "rows" and widget["period"] == "2024" and widget["population"] == 10


def test_collision_safe_ids_use_exact_raw_identity_and_remain_deterministic() -> None:
    records = [
        {"record_id": "a/b", "kind": "metric", "payload": {"value": 1}},
        {"record_id": "a-b", "kind": "metric", "payload": {"value": 2}},
    ]
    original = [
        {"id": "REQ-A-a-b", "integration_record_id": "a/b"},
        {"id": "REQ-A-a-b", "integration_record_id": "a-b"},
    ]
    first = dashboard_assembler._collision_safe_widget_ids("REQ-A", "group-a", [dict(item) for item in original], records)
    second = dashboard_assembler._collision_safe_widget_ids("REQ-A", "group-a", [dict(item) for item in original], records)
    assert [widget["id"] for widget in first] == [widget["id"] for widget in second]
    assert len({widget["id"] for widget in first}) == 2
    assert all(widget["id"].startswith("REQ-A-a-b--") for widget in first)
    different_group = dashboard_assembler._collision_safe_widget_ids("REQ-A", "group-b", [dict(item) for item in original], records)
    assert [widget["id"] for widget in first] != [widget["id"] for widget in different_group]


def test_product_manifest_forbidden_asset_rejected_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    context = _seed_run(source)
    manifest_path = source / "products" / "product_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        "run_id": context.run_id,
        "assets": [{"ref": "work/forbidden.json", "sha256": "a" * 64}],
    }), encoding="utf-8")
    original_read_bytes = dashboard_assembler._read_bytes

    def guarded_read_bytes(context_arg: RunContext, reference: str | Path, *, label: str):
        if "forbidden" in str(reference):
            pytest.fail("forbidden asset was opened")
        return original_read_bytes(context_arg, reference, label=label)

    monkeypatch.setattr(dashboard_assembler, "_read_bytes", guarded_read_bytes)
    with pytest.raises(dashboard_assembler.AssemblyError, match="forbidden product asset reference"):
        dashboard_assembler.assemble_dashboard(context, output_dir="repro_forbidden_asset")


def test_typed_metric_classification_is_conservative_and_geometry_is_tagged() -> None:
    content = {"limitations": ["synthetic"]}
    record = {
        "record_id": "parts",
        "record_hash": "a" * 64,
        "accepted_content_hash": "b" * 64,
        "evidence_refs": ["accepted/answer_content.json"],
        "payload": {"label": "Parts", "units": "orders", "value": {"A": 3, "B": 2}, "population": 5},
    }
    composition = dashboard_assembler._metric_widget("REQ-A", content, record)
    assert composition["type"] in {"donut", "waffle", "stacked_composition"}
    assert composition["presentation_geometry_only"] is True
    categories = composition.get("categories") or composition.get("segments")
    assert all("size" in category for category in categories)

    malformed = dict(record)
    malformed["record_id"] = "mixed"
    malformed["payload"] = {"label": "Malformed", "value": {"A": 3, "B": "unknown"}, "population": 5}
    table = dashboard_assembler._metric_widget("REQ-A", content, malformed)
    assert table["type"] == "table"
    assert all("size" not in row for row in table["rows"])

    mixed_units = dict(record)
    mixed_units["record_id"] = "mixed-units"
    mixed_units["payload"] = {"label": "Mixed units", "value": {"A": 3, "B": 2}, "units": {"A": "orders", "B": "currency"}}
    assert dashboard_assembler._metric_widget("REQ-A", content, mixed_units)["type"] == "table"


def test_nested_mixed_metrics_become_bounded_charts_and_entity_maps_collapse() -> None:
    content = {"limitations": ["synthetic"]}
    record = {
        "record_id": "mixed-nested",
        "record_hash": "c" * 64,
        "accepted_content_hash": "d" * 64,
        "evidence_refs": ["accepted/answer_content.json"],
        "payload": {
            "label": "Control summaries",
            "units": "source currency partitions; no FX conversion",
            "value": {
                "status_counts": {"OPEN": 2, "PAID": 8},
                "currency_partitions": {"USD": {"total": "10.0"}, "EUR": {"total": "9.0"}},
                "preview": [{"id": "x"}],
            },
            "status_rows": 10,
        },
    }
    widgets = dashboard_assembler._metric_widgets("REQ-A", content, record)
    status = next(widget for widget in widgets if widget.get("source_metric_key") == "status_counts")
    assert status["type"] in {"donut", "waffle", "stacked_composition", "column", "lollipop", "bar"}
    if status["type"] in {"donut", "waffle", "stacked_composition"}:
        assert status["presentation_geometry_only"] is True
        assert status.get("denominator_value") == 10
    assert all(widget["type"] != "kpi" for widget in widgets)

    entity = dict(record)
    entity["record_id"] = "supplier-late"
    entity["payload"] = {
        "label": "Supplier queues",
        "units": "distinct POs",
        "value": {
            "Supplier:VEND-1": {"accepted": 20, "late": 4},
            "Supplier:VEND-2": {"accepted": 15, "late": 1},
            "Supplier:VEND-3": {"accepted": 18, "late": 7},
        },
    }
    entity_widgets = dashboard_assembler._metric_widgets("REQ-A", content, entity)
    assert len(entity_widgets) == 1
    assert entity_widgets[0]["source_metric_key"] == "late"
    assert entity_widgets[0]["type"] in {"column", "lollipop", "bar"}

    value_sibling = dict(record)
    value_sibling["record_id"] = "value-sibling"
    value_sibling["payload"] = {
        "label": "Actions",
        "units": "rows",
        "value": {"watchlist_action_counts": {"MONITOR": 6, "REORDER": 4}, "watchlist_rows": 10},
    }
    composition = dashboard_assembler._metric_widgets("REQ-A", content, value_sibling)[0]
    assert composition["type"] in {"donut", "waffle", "stacked_composition"}
    assert composition["denominator_value"] == 10

    currency = dict(record)
    currency["record_id"] = "currency-unknown"
    currency["payload"] = {
        "label": "Source currency amounts",
        "units": "source currency partitions; no FX conversion",
        "value": {"": 2.0, "UNKNOWN": 3.0, "USD": 4.0},
    }
    currency_widget = dashboard_assembler._metric_widget("REQ-A", content, currency)
    assert currency_widget["type"] == "metric_grid"
    blank_tile = next(tile for tile in currency_widget["tiles"] if tile["source_key"] == "")
    assert blank_tile["label"] == "(blank source currency)"


def test_lossless_metric_routing_preserves_blank_and_provenance_shapes() -> None:
    """Blank values and provenance-bearing maps never become lossy charts."""

    def metric(record_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "record_id": record_id,
            "record_hash": "a" * 64,
            "accepted_content_hash": "b" * 64,
            "evidence_refs": ["accepted/answer_content.json"],
            "payload": payload,
        }

    blank_payload = {
        "label": "Blank reviewed value",
        "value": "",
        "source": "ERP",
        "as_of": "2026-08-17",
        "date_authority": "fixture-controlled snapshot",
        "distinct_unit": "material_no",
    }
    blank = dashboard_assembler._metric_widget("REQ-A", {}, metric("blank", blank_payload))
    assert blank["type"] == "table"
    blank_context = {row["field"]: row["value"] for row in blank["rows"] if row.get("row_kind") == "context"}
    assert blank_context["value"] == ""
    for key in ("source", "as_of", "date_authority", "distinct_unit"):
        assert blank_context[key] == blank_payload[key]
    assert dashboard_assembler._apply_overview_selection([blank]) == []

    mapped_payload = {
        "label": "Flat reviewed map",
        "value": {"A": 3, "B": 2},
        "source": "ERP",
        "as_of": "2026-08-17",
        "date_authority": "fixture-controlled snapshot",
        "distinct_unit": "material_no",
    }
    mapped = dashboard_assembler._metric_widget("REQ-A", {}, metric("mapped", mapped_payload))
    assert mapped["type"] == "table"
    mapped_context = {row["field"]: row["value"] for row in mapped["rows"] if row.get("row_kind") == "context"}
    for key, value in mapped_payload.items():
        assert mapped_context[key] == value

    scalar_payload = {
        "label": "Scalar with provenance",
        "value": 7,
        "source": "ERP",
        "as_of": "2026-08-17",
        "date_authority": "fixture-controlled snapshot",
        "distinct_unit": "material_no",
    }
    scalar = dashboard_assembler._metric_widget("REQ-A", {}, metric("scalar-provenance", scalar_payload))
    assert scalar["type"] == "kpi" and scalar["value"] == 7
    for key in ("source", "as_of", "date_authority", "distinct_unit"):
        assert scalar[key] == scalar_payload[key]


def test_overview_selection_preserves_all_plan_selected_widgets_in_plan_order() -> None:
    # Product plan order, not fixture storage order or widget shape, drives the
    # overview.  There is no implicit four-widget cap.
    widgets = [
        {"id": "stored-last", "type": "table", "rows": []},
        {"id": "stored-first", "type": "bar", "bars": []},
        {"id": "stored-middle", "type": "kpi", "value": ""},
        {"id": "stored-four", "type": "line", "points": []},
        {"id": "stored-five", "type": "table", "rows": []},
        {"id": "stored-six", "type": "kpi", "value": None},
    ]
    plan_order = [
        "stored-middle", "stored-first", "stored-last",
        "stored-six", "stored-four", "stored-five",
    ]
    assert dashboard_assembler._apply_overview_selection(widgets, plan_order) == plan_order
    assert [widget["id"] for widget in widgets if widget.get("overview") is True] == [
        "stored-last", "stored-first", "stored-middle", "stored-four", "stored-five", "stored-six",
    ]


def test_overview_selection_uses_explicit_plan_membership_only() -> None:
    """The overview follows Product-plan membership, not semantic labels."""

    widgets = [
        {
            "id": "join-check-count",
            "type": "kpi",
            "title": "Tested join check count",
            "value": 4,
            "presentation_role": "support_metric",
            "dashboard_fact": True,
            "manager_presentation": {
                "business_consequence": "The checked join supports a business decision."
            },
            "manager_admission": {"status": "admitted"},
            "presentation_audience": "business_manager",
        },
        {
            "id": "source-count",
            "type": "kpi",
            "title": "Selected source count",
            "value": 2,
            "presentation_role": "support_metric",
            "dashboard_fact": True,
            "manager_presentation": {
                "rationale": "The selected source changes the business decision."
            },
            "manager_admission": {"status": "admitted"},
            "presentation_audience": "business_manager",
        },
        {
            "id": "tabular-source-count",
            "type": "kpi",
            "title": "Tabular source count",
            "value": 9,
            "presentation_role": "support_metric",
            "dashboard_fact": True,
            "manager_presentation": {
                "decision_rationale": "The table count informs the business decision."
            },
            "manager_admission": {"status": "admitted"},
            "presentation_audience": "business_manager",
        },
        {
            "id": "orders-at-risk",
            "type": "kpi",
            "title": "Orders at risk",
            "value": 7,
            "manager_admission": {"status": "admitted"},
            "presentation_audience": "business_manager",
        },
    ]

    dashboard_assembler._apply_explicit_manager_admission(
        widgets,
        ["orders-at-risk"],
        {"orders-at-risk": {}},
    )
    assert dashboard_assembler._apply_overview_selection(widgets, ["orders-at-risk"]) == ["orders-at-risk"]
    assert all(widget.get("overview") is not True for widget in widgets[:3])
    assert widgets[3]["overview"] is True


def test_plan_defaults_to_richer_eligible_chart_over_table() -> None:
    candidate = {
        "widget_id": "orders-by-customer",
        "chart_family": "table",
        "recipes": [
            {"id": "table", "eligible": True},
            {"id": "horizontal_bar", "eligible": True},
        ],
    }
    assert dashboard_assembler._default_recipe_id(candidate) == "horizontal_bar"


def test_overview_selection_does_not_filter_chart_or_table_surfaces() -> None:
    widgets = [
        {
            "id": "headline",
            "type": "kpi",
            "title": "Orders reviewed",
            "value": 7,
            "requirement_id": "REQ-000",
            "manager_admission": {"status": "admitted"},
            "presentation_audience": "business_manager",
            "presentation_tier": "primary",
        },
        {
            "id": "orders-chart",
            "type": "bar",
            "title": "Orders at risk",
            "requirement_id": "REQ-001",
            "bars": [{"label": "Customer A", "value": 7, "size": "100%"}],
            "reviewed_item_ref": "requirements/REQ-001/accepted/manifest.json",
            "reviewed_output_ref": "requirements/REQ-001/accepted/answer_content.json",
            "evidence_refs": ["requirements/REQ-001/accepted/answer_content.json"],
            "trace_refs": ["requirements/REQ-001/accepted/answer_content.json"],
            "manager_admission": {"status": "admitted"},
            "presentation_audience": "business_manager",
            "presentation_tier": "primary",
        },
        {
            "id": "orders-detail",
            "type": "table",
            "title": "Orders detail",
            "requirement_id": "REQ-002",
            "rows": [{"label": "Reviewed", "value": "open"}],
            "manager_admission": {"status": "admitted"},
            "presentation_audience": "business_manager",
            "presentation_tier": "primary",
        },
    ]
    plan_order = ["orders-detail", "headline", "orders-chart"]
    assert dashboard_assembler._apply_overview_selection(widgets, plan_order) == plan_order
    assert all(widget.get("overview") is True for widget in widgets)


def test_renderer_honors_chart_or_table_overview_selection() -> None:
    fixture = {
        "title": "Reviewed decisions",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": [{
            "id": "orders-chart",
            "type": "bar",
            "title": "Orders at risk",
            "requirement_id": "REQ-001",
            "bars": [{"label": "Customer A", "value": 7, "size": "100%"}],
            "reviewed_item_ref": "requirements/REQ-001/accepted/manifest.json",
            "reviewed_output_ref": "requirements/REQ-001/accepted/answer_content.json",
            "evidence_refs": ["requirements/REQ-001/accepted/answer_content.json"],
            "trace_refs": ["requirements/REQ-001/accepted/answer_content.json"],
            "manager_admission": {
                "status": "admitted",
                "presentation_audience": "business_manager",
            },
            "presentation_audience": "business_manager",
            "presentation_tier": "primary",
            "overview": True,
        }],
        "overview_widget_ids": ["orders-chart"],
        "domains": [{
            "id": "operations",
            "title": "Operations",
            "order": 1,
            "decision_flow": [{
                "id": "operations-flow",
                "title": "Operations",
                "order": 1,
                "widget_ids": ["orders-chart"],
            }],
        }],
        "audit_records": [],
    }
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    index = pages["index.html"]
    index_text = index.decode("utf-8") if isinstance(index, bytes) else index
    assert "Orders at risk" in index_text
    assert "Customer A" in index_text
    assert ">7<" in index_text


def test_renderer_preserves_portfolio_title_with_compact_overview_widget() -> None:
    """The overview H1 remains the portfolio title after widget iteration."""

    fixture = {
        "title": "Portfolio decisions",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": [{
            "id": "orders-kpi",
            "type": "kpi",
            "title": "Orders reviewed",
            "value": 7,
            "requirement_id": "REQ-001",
            "reviewed_item_ref": "requirements/REQ-001/accepted/manifest.json",
            "reviewed_output_ref": "requirements/REQ-001/accepted/answer_content.json",
            "evidence_refs": ["requirements/REQ-001/accepted/answer_content.json"],
            "trace_refs": ["requirements/REQ-001/accepted/answer_content.json"],
            "manager_admission": {"status": "admitted", "presentation_audience": "business_manager"},
            "presentation_audience": "business_manager",
            "presentation_tier": "primary",
        }],
        "overview_widget_ids": ["orders-kpi"],
        "domains": [{
            "id": "operations",
            "title": "Operations",
            "order": 1,
            "decision_flow": [{"id": "operations-flow", "title": "Operations", "order": 1, "widget_ids": ["orders-kpi"]}],
        }],
        "audit_records": [],
    }
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    index = pages["index.html"]
    index_text = index.decode("utf-8") if isinstance(index, bytes) else index
    assert "<h1>Portfolio decisions</h1>" in index_text
    assert "Orders reviewed" in index_text
    assert index_text.count("<h1>") == 1


def test_renderer_line_chart_preserves_series_periods_values_and_separate_units() -> None:
    points = [
        {"period": "2020-01", "series": "orders", "value": 10},
        {"period": "2020-02", "series": "orders", "value": 12},
        {"period": "2020-01", "series": "order lines", "value": 20},
        {"period": "2020-02", "series": "order lines", "value": 18},
    ]
    rendered = dashboard_renderer._render_line(
        {
            "id": "activity-trend",
            "type": "line",
            "title": "Monthly activity",
            "points": points,
            "limitations": ["Use separate axes because the units differ."],
        }
    )
    assert "<svg" in rendered
    assert rendered.count("line-chart-panel") == 2
    assert "orders" in rendered and "order lines" in rendered
    assert all(str(value) in rendered for value in (10, 12, 20, 18))
    assert all(period in rendered for period in ("2020-01", "2020-02"))
    assert "viz-line-list" not in rendered


def test_renderer_manager_meta_shows_reviewed_context_adjacent_to_chart() -> None:
    points = [{"period": "Jan", "series": "orders", "value": 4}, {"period": "Feb", "series": "orders", "value": 6}]
    widget = {
        "id": "context-trend",
        "type": "line",
        "title": "Reviewed activity",
        "manager_admission": {"status": "admitted", "presentation_audience": "business_manager"},
        "presentation_audience": "business_manager",
        "manager_presentation": {
            "widget_id": "context-trend",
            "visual_type": "line",
            "chart_family": "line_area_slope",
            "recipe_id": "line_area_slope",
            "renderer_type": "line",
            "title_projection": {"pointer": "/widget_snapshot/title", "value": "Reviewed activity"},
            "visual_projection": {
                "type": {"pointer": "/chart_entry/type", "value": "line"},
                "family": {"pointer": "/chart_entry/family", "value": "line_area_slope"},
                "points": {"pointer": "/chart_entry/fields_or_values_used/points", "value": points},
                "period": {"pointer": "/chart_entry/fields_or_values_used/period", "value": "Jan–Feb"},
                "population": {"pointer": "/chart_entry/fields_or_values_used/population", "value": "Reviewed orders"},
                "denominator": {"pointer": "/chart_entry/fields_or_values_used/denominator", "value": 10},
                "unit": {"pointer": "/chart_entry/fields_or_values_used/unit", "value": "orders"},
                "grain": {"pointer": "/chart_entry/fields_or_values_used/grain", "value": "one row per month"},
                "proxy": {"pointer": "/chart_entry/fields_or_values_used/proxy", "value": "descriptive proxy"},
                "descriptive_only": {"pointer": "/chart_entry/fields_or_values_used/descriptive_only", "value": True},
                "limitations": {"pointer": "/chart_entry/fields_or_values_used/limitations", "value": ["Descriptive only; proxy is not causal."]},
            },
        },
    }
    rendered = dashboard_renderer._site_widget(widget, [])
    for label in ("Period", "Population", "Denominator", "Unit", "Grain", "Proxy / limit", "Descriptive", "Limitations"):
        assert f"<b>{label}</b>" in rendered
    assert "Jan–Feb" in rendered and "one row per month" in rendered
    assert "descriptive proxy" in rendered and "Descriptive only; proxy is not causal." in rendered


@pytest.mark.parametrize(
    ("item_id", "scope"),
    [
        (
            "REQ-006",
            "Where warehouse stock balances and movement records diverge from physical counts or approved adjustments.",
        ),
        (
            "REQ-008",
            "Which customer receivables are overdue or disputed within the reviewed population.",
        ),
    ],
)
def test_answer_scope_is_preserved_and_rendered_as_manager_context(item_id: str, scope: str) -> None:
    context = RunContext(f"RUN-{item_id}-SCOPE", Path("/tmp") / f"{item_id.lower()}-scope")
    content = {
        "item_id": item_id,
        "scope": scope,
        "visuals": [{"type": "table", "title": "Reviewed values", "rows": [{"label": "Reviewed", "value": 4}]}],
    }
    widget = dashboard_assembler._accepted_visual_widgets(
        context,
        item_id,
        content,
        {"artifact_progress": {"hashes": {}}},
        "b" * 64,
        "a" * 64,
    )[0]
    assert widget["answer_scope"] == scope
    chart = dashboard_assembler._chart_map_entry(widget, item_id, {"item_id": item_id})
    assert chart["fields_or_values_used"]["answer_scope"] == scope
    widget.update({
        "manager_admission": {"status": "admitted", "presentation_audience": "business_manager"},
        "presentation_audience": "business_manager",
    })
    rendered = dashboard_renderer._site_widget(widget, [])
    assert "<b>Scope</b>" in rendered
    assert scope in rendered


def test_product_selected_previously_demoted_surface_remains_admitted_and_rendered() -> None:
    accepted = {
        "id": "accepted-orders",
        "type": "bar",
        "title": "Orders at risk",
        "requirement_id": "REQ-001",
        "accepted_visual": True,
        "source_metric_key": "orders",
        "scope": "monthly",
        "grain": "customer",
        "dimension": "customer_name",
        "bars": [{"label": "Customer A", "value": 7, "size": "100%"}],
        "presentation_role": "decision_view",
        "presentation_tier": "primary",
    }
    fact = {
        "id": "fact-orders",
        "type": "bar",
        "title": "Orders at risk",
        "requirement_id": "REQ-001",
        "dashboard_fact": True,
        "source_metric_key": "orders",
        "scope": "monthly",
        "grain": "customer",
        "dimension": "customer_name",
        "bars": [{"label": "Customer A", "value": 7, "size": "100%"}],
        "presentation_role": "decision_view",
        "presentation_tier": "primary",
        # Stale metadata from an older assembler must not veto Product plan
        # membership or hide this exact selected projection.
        "presentation_deduplication": {"status": "demoted", "retained_widget_id": "accepted-orders"},
    }
    dashboard_assembler._apply_explicit_manager_admission(
        [accepted, fact],
        ["accepted-orders", "fact-orders"],
        {"accepted-orders": {}, "fact-orders": {}},
    )
    assert accepted["manager_admission"]["status"] == "admitted"
    assert fact["manager_admission"]["status"] == "admitted"
    assert dashboard_renderer._manager_widget_allowed(fact) is True
    # This focused regression exercises the raw exact widget renderer after
    # admission; persisted V2 plans carry the equivalent pointer-bound
    # projection in ``manager_presentation``.
    fact.pop("manager_presentation", None)
    rendered = dashboard_renderer._site_widget(fact, [])
    assert "Orders at risk" in rendered and "Customer A" in rendered and ">7<" in rendered


def test_selected_same_title_surfaces_remain_manager_visible_without_dedupe() -> None:
    first = {
        "id": "accepted-orders-current",
        "type": "bar",
        "title": "Orders at risk",
        "requirement_id": "REQ-001",
        "accepted_visual": True,
        "source_metric_key": "orders_current",
        "scope": "monthly",
        "grain": "customer",
        "dimension": "customer_name",
        "bars": [{"label": "Customer A", "value": 7, "size": "100%"}],
        "presentation_role": "decision_view",
        "presentation_tier": "primary",
    }
    second = {
        **first,
        "id": "accepted-orders-prior",
        "source_metric_key": "orders_prior",
        "bars": [{"label": "Customer A", "value": 9, "size": "100%"}],
    }
    widgets = [first, second]
    dashboard_assembler._apply_explicit_manager_admission(
        widgets,
        ["accepted-orders-current", "accepted-orders-prior"],
        {"accepted-orders-current": {}, "accepted-orders-prior": {}},
    )
    assert all(widget["manager_admission"]["status"] == "admitted" for widget in widgets)


def test_selected_same_title_fact_remains_manager_visible_when_scope_or_grain_differs() -> None:
    accepted = {
        "id": "accepted-orders",
        "type": "bar",
        "title": "Orders at risk",
        "requirement_id": "REQ-001",
        "accepted_visual": True,
        "source_metric_key": "orders",
        "scope": "monthly",
        "grain": "customer",
        "dimension": "customer_name",
        "bars": [{"label": "Customer A", "value": 7, "size": "100%"}],
    }
    fact = {
        "id": "fact-orders",
        "type": "bar",
        "title": "Orders at risk",
        "requirement_id": "REQ-001",
        "dashboard_fact": True,
        "source_metric_key": "orders",
        "scope": "weekly",
        "grain": "order",
        "dimension": "customer_name",
        "bars": [{"label": "Customer A", "value": 7, "size": "100%"}],
    }
    widgets = [accepted, fact]
    dashboard_assembler._apply_explicit_manager_admission(
        widgets,
        ["accepted-orders", "fact-orders"],
        {"accepted-orders": {}, "fact-orders": {}},
    )
    assert all(widget["manager_admission"]["status"] == "admitted" for widget in widgets)


def test_build_widgets_keeps_source_bound_accepted_and_fact_candidates(tmp_path: Path) -> None:
    """Normal accepted/fact constructors remain independent candidates."""

    context = RunContext("RUN-SEMANTIC-CONSTRUCTOR", tmp_path / "run")
    content = {
        "item_id": "REQ-001",
        "visuals": [{
            "type": "bar",
            "title": "Orders at risk",
            "source_metric_key": "orders",
            "scope": "monthly",
            "grain": "customer",
            "dimension": "customer_name",
            "rows": [{"label": "Customer A", "value": 7}],
        }],
    }
    accepted_visuals = dashboard_assembler._accepted_visual_widgets(
        context,
        "REQ-001",
        content,
        {"artifact_progress": {"hashes": {}}},
        "a" * 64,
        "b" * 64,
    )
    fact_record = {
        "record_id": "fact-orders",
        "record_hash": "c" * 64,
        "kind": "dashboard_fact",
        "payload": {
            "widget": {
                "type": "bar",
                "title": "Orders at risk",
                "source_metric_key": "orders",
                "scope": "monthly",
                "grain": "customer",
                "dimension": "customer_name",
                "rows": [{"label": "Customer A", "value": 7}],
            },
        },
    }
    widgets = dashboard_assembler._build_widgets(
        "REQ-001",
        {"__manager_requirement_scope": "monthly"},
        [fact_record],
        accepted_visuals=accepted_visuals,
    )
    accepted = next(widget for widget in widgets if widget.get("accepted_visual") is True)
    fact = next(widget for widget in widgets if widget.get("dashboard_fact") is True)
    assert accepted["requirement_id"] == fact["requirement_id"] == "REQ-001"
    assert accepted["requirement_scope"] == fact["requirement_scope"] == "monthly"
    assert accepted["manager_admission"]["status"] == "admitted"
    assert fact["manager_admission"]["status"] == "admitted"


def test_product_plan_membership_is_only_manager_gate() -> None:
    """The plan may select any supplied widget and the renderer stays exact."""

    selected = {
        "id": "selected-traces",
        "type": "table",
        "kind": "audit_log",
        "title": "Execution traces",
        "technical_surface": True,
        "technical_surface_reason": "origin metadata",
        "presentation_role": "technical_audit",
        "rows": [{"trace_id": "trace-1", "status": "open", "count": 3}],
    }
    omitted = {
        **selected,
        "id": "omitted-traces",
        "technical_surface": False,
        "rows": [{"trace_id": "trace-2", "status": "closed", "count": 1}],
    }
    manager_projection = {
        "display_projection": {
            "title": {"pointer": "/widget_snapshot/title", "value": "Execution traces"},
            "rows": {
                "pointer": "/widget_snapshot/rows",
                "value": [{"trace_id": "trace-1", "status": "open", "count": 3}],
            },
        },
        "rationale": "Explicit Product selection for a reviewed decision.",
        "renderer_type": "table",
    }
    dashboard_assembler._apply_explicit_manager_admission(
        [selected, omitted],
        ["selected-traces"],
        {"selected-traces": manager_projection},
    )
    assert selected["manager_admission"]["status"] == "admitted"
    assert selected["technical_surface"] is True
    assert omitted["manager_admission"]["status"] == "audit_only"
    assert dashboard_renderer._manager_widget_allowed(selected) is True
    assert dashboard_renderer._manager_widget_allowed(omitted) is False

    rendered = dashboard_renderer._site_widget(selected, [])
    assert "Execution traces" in rendered
    assert "trace-1" in rendered
    assert "open" in rendered
    assert ">3<" in rendered


def test_manager_cells_humanize_short_schema_tokens_without_touching_audit() -> None:
    manager = dashboard_renderer._render_table(
        {
            "rows": [{"status": "accepted_with_limits", "cardinality": "one_to_one", "raw": "free prose"}],
        },
        manager_view=True,
    )
    assert "Accepted with limits" in manager
    assert "One to one" in manager
    assert "accepted_with_limits" not in manager
    assert "one_to_one" not in manager
    assert "free prose" in manager


def test_no_fact_metrics_remain_independent_candidates_without_auto_demotion() -> None:
    run_root = ROOT.parent / "benchmark_a_requirement_v070_entity_run_a_rerun_1"
    records_root = run_root / "requirements"
    if not records_root.is_dir():
        pytest.skip("benchmark reviewed records are not present in this checkout")
    for item_id in ("REQ-05", "REQ-06", "REQ-08"):
        records_path = records_root / item_id / "integration" / "committed" / "records.jsonl"
        records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
        widgets = dashboard_assembler._build_widgets(item_id, {}, records)
        metric_record_ids = {
            str(record.get("record_id"))
            for record in records
            if record.get("kind") == "metric"
        }
        metric_widgets = [
            widget
            for widget in widgets
            if str(widget.get("integration_record_id")) in metric_record_ids
        ]
        assert metric_widgets
        # Every metric-derived candidate remains independently available.  No
        # record-level winner, synthetic aggregate, or schema-only demotion is
        # applied before Product selection.
        assert all(widget.get("presentation_tier") != "audit" for widget in metric_widgets)
        assert not any(widget.get("title") == "Key signals" for widget in metric_widgets)
        assert all("aggregated_metric_ids" not in widget for widget in metric_widgets)


def test_requirement_titles_reject_schema_tokens_and_raw_singleton_routes() -> None:
    title = dashboard_assembler._manager_requirement_title(
        "REQ-05",
        {"title": "invoice_ledger"},
        "Determine whether supplier invoices are matched and paid according to approved terms.",
        [],
    )
    assert title == "Whether supplier invoices are matched and paid according to approved terms"
    assert title != "invoice_ledger"
    assert dashboard_assembler._manager_requirement_title(
        "REQ-08",
        {"title": "workfile"},
        "Show which customer receivables are overdue or disputed.",
        [],
    ) == "Which customer receivables are overdue or disputed"
    fixture = {
        "freeze_markers": {"answers_frozen": True, "living_enterprise_model_frozen": True, "prepared_data_registry_frozen": True, "dashboard_frozen": True, "telemetry_frozen": True},
        "widgets": [{
            "id": "req-09-signal",
            "type": "kpi",
            "value": 1,
            "requirement_id": "REQ-09",
            "requirement_title": "Governed order-to-cash handoff coverage",
            "reviewed_item_ref": "requirements/REQ-09/accepted/manifest.json",
            "reviewed_output_ref": "requirements/REQ-09/accepted/answer_content.json",
            "trace_refs": ["requirements/REQ-09/accepted/manifest.json"],
            "evidence_refs": ["work/evidence.jsonl"],
        }],
        "domains": [{
            "id": "REQ-09",
            "title": "REQ-09",
            "order": 1,
            "decision_flow": [{"id": "REQ-09-flow", "title": "REQ-09", "order": 1, "widget_ids": ["req-09-signal"]}],
        }],
    }
    pages, _manifest = dashboard_renderer.render_dashboard_site(fixture)
    domain = pages["domains/REQ-09.html"]
    if isinstance(domain, bytes):
        domain = domain.decode("utf-8")
    assert ">REQ-09</a>" not in domain
    assert "Governed order-to-cash handoff coverage" in domain


def test_requirement_titles_use_complete_scope_words_and_metric_tiles_keep_denominators() -> None:
    scopes = {
        "REQ-01": "Provide a management view of how reliably customer orders move from placement through shipment and delivery.",
        "REQ-04": "Show which suppliers and purchase orders are most at risk of missing accepted delivery promises.",
        "REQ-06": "Show where warehouse stock balances and movement records diverge from physical counts or approved adjustments.",
        "REQ-08": "Show which customer receivables are overdue or disputed, how collection notes and posted cash reconcile to the ledger.",
        "REQ-17": "At the operating close, use the cumulative enterprise ontology to construct an end-to-end material-flow and cash-conversion risk diagnostic.",
    }
    dangling = dashboard_assembler._DANGLING_SCOPE_TOKENS
    for item_id, scope in scopes.items():
        title = dashboard_assembler._manager_requirement_title(
            item_id,
            {"title": "metric_schema_label"},
            scope,
            [{"kind": "metric", "payload": {"label": "Scalar signal"}}],
        )
        assert len(title) <= 130
        assert title.split()[-1].rstrip(".,;:").lower() not in dangling
        assert "…" not in title
        assert title.count("(") == title.count(")")
        assert "Scalar signal" not in title
    assert dashboard_assembler._manager_requirement_title("REQ-06", {}, scopes["REQ-06"], []) == "Where warehouse stock balances and movement records diverge from physical counts or approved adjustments"
    assert dashboard_assembler._manager_requirement_title("REQ-08", {}, scopes["REQ-08"], []) == "Which customer receivables are overdue or disputed"

    def metric(record_id: str, label: str, value: int, denominator: int) -> dict[str, object]:
        return {
            "record_id": record_id,
            "record_hash": "a" * 64,
            "accepted_content_hash": "b" * 64,
            "evidence_refs": ["work/results/review.json"],
            "kind": "metric",
            "payload": {"label": label, "value": value, "denominator": denominator, "units": "tickets"},
        }

    fact = {
        "record_id": "REQ-12-fact",
        "record_hash": "c" * 64,
        "accepted_content_hash": "d" * 64,
        "evidence_refs": ["work/results/review.json"],
        "kind": "dashboard_fact",
        "payload": {"title": "Reviewed status", "type": "status_table", "rows": [{"label": "status", "value": "reviewed"}]},
    }
    records = [
        metric("REQ-12-closed", "Closed tickets", 460, 1142),
        metric("REQ-12-mapped", "Mapped orders", 785, 1142),
        fact,
    ]
    widgets = dashboard_assembler._build_widgets("REQ-12", {}, records)
    assert not any(widget.get("title") == "Key signals" for widget in widgets)
    closed_widget = next(widget for widget in widgets if widget.get("title") == "Closed tickets")
    mapped_widget = next(widget for widget in widgets if widget.get("title") == "Mapped orders")
    assert closed_widget["value"] == 460
    assert closed_widget["denominator"] == 1142
    assert mapped_widget["value"] == 785
    assert mapped_widget["denominator"] == 1142
    mapped_widget = next(widget for widget in widgets if widget.get("integration_record_id") == "REQ-12-mapped")
    assert mapped_widget.get("audit_payload", {}).get("denominator") == 1142
    assert "aggregated_metric_ids" not in closed_widget
    assert "aggregated_metric_ids" not in mapped_widget
    closed_html = dashboard_renderer._site_widget(closed_widget, [])
    mapped_html = dashboard_renderer._site_widget(mapped_widget, [])
    assert "460" in closed_html and "1142" in closed_html
    assert "785" in mapped_html and "1142" in mapped_html


def test_manager_prose_humanizes_bounded_tokens_and_slash_pairs_without_rewriting_refs() -> None:
    text = (
        "source_population/source_coverage open_or_unknown closed_at case_status "
        "as_of order_created_at promised_ship_by qty_delta start_date/end_date "
        "requirements/REQ-12/accepted/answer_content.json record_id"
    )
    rendered = dashboard_renderer._manager_prose_text(text)
    for token in ("source_population", "source_coverage", "open_or_unknown", "closed_at", "case_status", "as_of", "order_created_at", "promised_ship_by", "qty_delta", "start_date", "end_date"):
        assert token not in rendered
    assert "Source population/Source coverage" in rendered
    assert "Open or unknown" in rendered
    assert "Closed at" in rendered and "Case status" in rendered
    assert "requirements/REQ-12/accepted/answer_content.json" in rendered
    assert "record_id" in rendered
    assert dashboard_renderer._manager_prose_text(
        "SALE_RETURN PO_RECEIPT AVAILABLE_GT_0 BOTH_AVAILABLE_GT_0_AND_DAMAGED_GT_0"
    ) == "Sale return PO receipt Available > 0 Both available > 0 and damaged > 0"


def test_actual_rendered_site_has_no_snake_tokens_outside_technical_audit() -> None:
    candidates = sorted(Path("/tmp").glob("dashboard-manager-g3-*/products/generations/G-0003/manager_candidate_token_fix/site"))
    if not candidates:
        pytest.skip("actual manager candidate site is not present")
    site = candidates[-1]
    pages = [site / "index.html", site / "ontology.html", *sorted((site / "domains").glob("*.html"))]
    token_pattern = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)(?![A-Za-z0-9_])")
    for page in pages:
        source = page.read_text(encoding="utf-8")
        visible_source = re.sub(r'<details\b[^>]*class=["\']technical-audit["\'][^>]*>.*?</details>', "", source, flags=re.DOTALL | re.IGNORECASE)
        visible_text = html.unescape(re.sub(r"<[^>]+>", " ", visible_source))
        assert not token_pattern.search(visible_text), (page, sorted(set(match.group(1) for match in token_pattern.finditer(visible_text))))
    raw_domain_html = "".join(page.read_text(encoding="utf-8") for page in (site / "domains").glob("*.html"))
    for token in (
        "SALE_RETURN", "PO_RECEIPT", "AVAILABLE_GT_0", "DAMAGED_GT_0",
        "BOTH_AVAILABLE_GT_0_AND_DAMAGED_GT_0", "source_population", "source_coverage",
        "watchlist_rows", "open_or_unknown",
    ):
        assert token in raw_domain_html


def test_manager_renderer_preserves_explicit_chart_geometry_with_raw_rows_present() -> None:
    widget = {
        "id": "legacy-carrier-late",
        "type": "lollipop",
        "title": "Carrier late queue",
        "bars": [{"label": "DHL", "value": 23, "size": "100%"}],
        "manager_rows": [{"Label": "Name", "Value": "schema-only"}],
        "trace_refs": ["requirements/REQ-01/accepted/manifest.json"],
        "evidence_refs": ["work/evidence.jsonl"],
    }
    html = dashboard_renderer._site_widget(widget, dashboard_renderer._trace_records(widget))
    assert 'class="viz viz-lollipop"' in html
    assert 'class="table-wrap"' not in html
    assert "23" in html and "100%" in html


def test_accepted_evidence_fact_sheet_renders_labels_and_containers_without_pointers() -> None:
    widget = {
        "id": "fact-sheet",
        "type": "table",
        "title": "Business metrics",
        "accepted_evidence": True,
        "accepted_evidence_fact_sheet": True,
        "accepted_evidence_id": "REQ-EVIDENCE-CONTEXT",
        "rows": [{"path": "/facts/context", "value": {"channels": ["web"], "region": "EU"}}],
        "manager_rows": [{"label": "Context", "value": '{"channels":["web"],"region":"EU"}'}],
    }
    html = dashboard_renderer._site_widget(widget, [])

    assert "Context" in html
    assert '{&quot;channels&quot;:[&quot;web&quot;],&quot;region&quot;:&quot;EU&quot;}' in html
    assert "/facts/context" not in html
    assert "REQ-EVIDENCE-CONTEXT" not in html


def test_accepted_evidence_without_conclusion_keeps_id_out_of_manager_title() -> None:
    widget = {
        "title": "Business metrics",
        "accepted_evidence": True,
        "accepted_evidence_candidate_kind": "fact_sheet",
        "accepted_evidence_pointer": "/facts",
        "audit_payload": {
            "evidence_id": "EVD-PRIVATE-001",
            "facts": {"denominator": 4},
        },
    }

    projection = dashboard_assembler._accepted_evidence_display_projection(widget)

    assert projection is not None
    assert projection["title"] == {
        "pointer": "/widget_snapshot/title",
        "value": "Business metrics",
    }
    assert "EVD-PRIVATE-001" not in json.dumps(projection)


def test_manager_surface_hides_entity_ids_and_data_units_but_keeps_business_values() -> None:
    widget = {
        "id": "recovery-queue",
        "type": "table",
        "title": "Recovery queue",
        "unit": "records",
        "rows": [{
            "Customer Order Id": "customer-order:SO-005893",
            "Supplier": "Supplier:VEND-000001",
            "Status": "open_or_unknown",
            "Amount": 4,
        }],
        "trace_refs": ["requirements/REQ-12/accepted/manifest.json"],
        "evidence_refs": ["work/evidence.jsonl"],
    }
    html = dashboard_renderer._site_widget(widget, dashboard_renderer._trace_records(widget))
    assert "SO-005893" not in html
    assert "VEND-000001" not in html
    assert "Customer Order Id" not in html
    # Reviewed units are business context and remain adjacent to the value;
    # only the entity identifiers themselves are hidden from manager copy.
    assert "records" in html
    assert "Open or unknown" in html and ">4<" in html


def test_actual_prior_chart_hints_preserve_shape_and_values_for_representative_requirements() -> None:
    run_root = ROOT.parent / "benchmark_a_requirement_v070_entity_run_a_rerun_1"
    prior_path = run_root / "products" / "generations" / "G-0003" / "dashboard" / "dashboard_fixture_v4.json"
    records_root = run_root / "requirements"
    if not prior_path.is_file() or not records_root.is_dir():
        pytest.skip("benchmark prior generation fixture is not present in this checkout")
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    hints = {widget["id"]: widget for widget in prior["widgets"]}
    visual_fields = ("type", "value", "bars", "categories", "tiles", "segments", "presentation_geometry_only", "geometry_basis", "denominator_value", "denominator_label")
    for item_id in ("REQ-01", "REQ-05", "REQ-06", "REQ-08"):
        records_path = records_root / item_id / "integration" / "committed" / "records.jsonl"
        records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
        rebuilt = {widget["id"]: widget for widget in dashboard_assembler._build_widgets(item_id, {}, records, legacy_hints=hints)}
        for widget_id, old in hints.items():
            if not widget_id.startswith(item_id) or old.get("type") not in {"bar", "lollipop", "column", "donut", "metric_grid"}:
                continue
            assert widget_id in rebuilt
            assert all(rebuilt[widget_id].get(field) == old.get(field) for field in visual_fields)


def test_manager_surface_actual_req12_req15_req17_shapes_keep_raw_audit_only() -> None:
    """Actual-shaped reviewed records remain independently selectable."""

    run_root = ROOT.parent / "benchmark_a_requirement_v070_entity_run_a_rerun_1"
    records_root = run_root / "requirements"
    if not records_root.is_dir():
        pytest.skip("benchmark reviewed records are not present in this checkout")
    widgets: list[dict[str, object]] = []
    requirement_ids = ("REQ-12", "REQ-15", "REQ-17")
    primary_counts: dict[str, int] = {}
    audit_counts: dict[str, int] = {}
    for order, item_id in enumerate(requirement_ids, 1):
        records_path = records_root / item_id / "integration" / "committed" / "records.jsonl"
        records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
        state = json.loads((records_root / item_id / "item_state.json").read_text(encoding="utf-8"))
        accepted = json.loads((records_root / item_id / "accepted" / "answer_content.json").read_text(encoding="utf-8"))
        scope = str(state.get("original_text") or accepted.get("scope") or item_id)
        item_content = dict(accepted)
        item_content["__manager_requirement_scope"] = scope
        item_widgets = dashboard_assembler._build_widgets(item_id, item_content, records)
        assert item_widgets
        primary_counts[item_id] = sum(widget.get("presentation_tier") != "audit" for widget in item_widgets)
        audit_counts[item_id] = sum(widget.get("presentation_tier") == "audit" for widget in item_widgets)
        assert audit_counts[item_id] > 0
        assert primary_counts[item_id] > 0
        assert not any(widget.get("title") == "Key signals" for widget in item_widgets)
        assert all("aggregated_metric_ids" not in widget for widget in item_widgets)
        assert len({widget.get("id") for widget in item_widgets}) == len(item_widgets)
        title = dashboard_assembler._manager_requirement_title(item_id, item_content, scope, records)
        assert title != f"{item_id} decision view"
        for index, widget in enumerate(item_widgets, 1):
            widget.update({
                "requirement_id": item_id,
                "requirement_title": title,
                "requirement_order": order,
                "domain_id": "manager",
                "manager_anchor": f"{item_id}-{index}",
            })
        widgets.extend(item_widgets)

    fixture = {
        "title": "Manager-facing reviewed workspace",
        "freeze_markers": {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_data_registry_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        "widgets": widgets,
        "domains": [{
            "id": "manager",
            "title": "Manager decisions",
            "order": 1,
            "decision_flow": [{"id": "manager-flow", "title": "Manager decisions", "order": 1, "widget_ids": [widget["id"] for widget in widgets]}],
        }],
    }
    pages, manifest = dashboard_renderer.render_dashboard_site(fixture)
    domain = pages["domains/manager.html"]
    domain_text = domain.decode("utf-8") if isinstance(domain, bytes) else domain
    visible = re.sub(r"<details\b[^>]*>.*?</details>", "", domain_text, flags=re.DOTALL)

    assert manifest["manager_surface"] == "summary_first"
    assert manifest["technical_audit_collapsed"] is True
    assert manifest["primary_widget_count"] > 0
    assert manifest["audit_widget_count"] > 0
    # Technical snapshots are kept on the separate audit page, never on the
    # default manager domain canvas.
    assert domain_text.count('class="technical-audit"') == 0
    assert "Reviewed findings" not in visible
    # ``source-local`` is part of the reviewed business cost measure here, not
    # an audit/source-inventory projection, so its exact title remains visible.
    assert "Source-local native cost distribution" in visible
    assert "REQ-15 decision view" not in domain_text
    assert "REQ-15 ·" not in domain_text
    assert re.search(r'<span class="eyebrow">Decision</span><h2>[^<]+</h2>', domain_text)
    assert "Key signals" not in visible
    # Legacy (planless) fixtures may humanize schema tokens for readability;
    # this is presentation formatting, not manager admission or field removal.
    assert "One to one" in visible
    assert "REQ-12.reused.REL" not in visible
    assert "row_kind" not in visible and "value_json" not in visible
    assert not re.search(r"\b[0-9a-f]{64}\b", visible, flags=re.IGNORECASE)
    assert not re.search(r"(?:requirements|products|extensions|telemetry|work)/[^ <\"&]+", visible)
    audit_page = pages["data-quality-audit.html"]
    audit_text = audit_page.decode("utf-8") if isinstance(audit_page, bytes) else audit_page
    assert "Technical audit records" in audit_text
    assert "Technical audit records" in audit_text
    assert all(item["widget_ids"] for item in manifest["requirement_groups"])
    for group in manifest["requirement_groups"]:
        assert set(group["widget_ids"]) == set(group["primary_widget_ids"]) | set(group["audit_widget_ids"])


def test_canonical_control_center_theme_preserves_shell_and_widget_vocabulary() -> None:
    """The committed shell owns visual tokens while reviewed widgets stay dynamic."""

    css = dashboard_renderer._canonical_dashboard_css().decode("utf-8")
    # Strip comments, then require a declaration to begin at a CSS block or
    # declaration boundary.  Substring membership would also accept prose or
    # a similarly named custom property.
    declaration_css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    tokens = {
        "--canvas": "#f4f7f9",
        "--panel": "#ffffff",
        "--raised": "#f8fbfc",
        "--control": "#eef3f6",
        "--ink": "#12212f",
        "--muted": "#5b6b79",
        "--line": "#d6e0e6",
        "--border-strong": "#aabac4",
        "--teal": "#076f75",
        "--positive": "#217452",
        "--negative": "#a43b47",
        "--panel-shadow": "0 8px 24px rgba(18, 33, 47, .07)",
    }
    for name, value in tokens.items():
        declaration = re.compile(
            rf"(?:^|[{{;])\s*{re.escape(name)}\s*:\s*{re.escape(value)}\s*;"
        )
        assert declaration.search(declaration_css), (name, value)
    assert "gradient" not in css.lower()

    # Every authored pixel font declaration remains legible at the 12px floor,
    # including SVG labels and compact audit metadata.
    for declaration in re.findall(r"(?:font-size|font)\s*:\s*[^;}]+", css):
        values = [float(value) for value in re.findall(r"(?<![\w.])([0-9]+(?:\.[0-9]+)?)px", declaration)]
        assert all(value >= 12 for value in values), declaration

    for widget_class in (
        "viz-bar",
        "column-bar",
        "lollipop-row",
        "viz-progress",
        "viz-leaderboard",
        "viz-metric-grid",
        "viz-stacked",
        "viz-diverging",
        "viz-heatmap",
        "viz-donut",
        "viz-waffle",
        "ontology-graph",
        "network",
        "table-wrap",
    ):
        selector = re.compile(rf"\.{re.escape(widget_class)}(?=$|[\s,{{:.\[>+~])")
        assert selector.search(css), widget_class

    shell = dashboard_renderer._site_page(
        title="Canonical shell",
        current="index.html",
        domains=[],
        body="<p>Reviewed output</p>",
    )
    assert "AUTO FOUNDRY" in shell
    assert "Auto Foundry Control Center" in shell
    assert "DECISION//ROOM" not in shell
    assert b"Auto Foundry" in dashboard_renderer._OFFLINE_FAVICON_SVG
