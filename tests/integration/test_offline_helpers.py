"""Offline integration checks for the bounded product helpers."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
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

from auto_foundry_core.workspace import AllowedRootError, RunContext  # noqa: E402


def _fixture() -> dict[str, object]:
    widgets: list[dict[str, object]] = [
        {"id": "kpi-1", "type": "kpi", "title": "Supplied KPI", "value": "4,032/4,093", "unit": "orders", "trace_refs": ["Q-001/final"]},
        {"id": "bar-1", "type": "bar", "title": "Bars", "bars": [{"label": "A", "value": "3", "share": "50%"}], "trace_refs": ["Q-002/final"]},
        {"id": "line-1", "type": "line", "title": "Line", "points": [{"label": "Jan", "value": "7"}], "trace_refs": ["Q-003/final"]},
        {"id": "stack-1", "type": "stacked_composition", "title": "Composition", "segments": [{"label": "A", "value": "2", "share": "25%"}], "trace_refs": ["Q-004/final"]},
        {"id": "heat-1", "type": "heatmap", "title": "Heat", "cells": [{"label": "A", "value": "high", "intensity": "high"}], "trace_refs": ["Q-005/final"]},
        {"id": "scatter-1", "type": "scatter", "title": "Scatter", "points": [{"label": "A", "x": "1", "y": "2"}], "trace_refs": ["Q-006/final"]},
        {"id": "donut-1", "type": "donut", "title": "Categories", "categories": [{"label": "A", "value": "1", "share": "100%"}], "trace_refs": ["Q-007/final"]},
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
        "domains": [
            {"id": "second", "title": "Second domain first", "order": 1, "decision_flow": [{"id": "flow", "title": "Decision flow supplied", "order": 1, "widget_ids": ["line-1"]}]},
            {"id": "first", "title": "First domain second", "order": 2, "decision_flow": [{"id": "flow", "title": "Second decision flow", "order": 1, "widget_ids": ["kpi-1", "bar-1", "stack-1", "heat-1", "scatter-1", "donut-1", "table-1"]}]},
        ],
        "widgets": widgets,
    }


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
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_assets_frozen": True,
        "dashboard_frozen": True,
        "telemetry_frozen": True,
        "run_id": "RUN-TEST",
        "review_routing": {"fresh_sol_review_available": False},
    }


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
    for marker in ("skill_name: auto-foundry-agentic-e2e", "skill_version: 0.2.1", "core_name: auto_foundry_core", "core_version: 0.1.0"):
        assert marker in commands
    assert "82e9c913bf437ac9e361d6890467a9aed9b1c6db9d887cfcf0cd659035a71ec2" in commands
