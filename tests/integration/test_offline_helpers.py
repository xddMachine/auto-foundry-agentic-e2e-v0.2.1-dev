"""Offline integration checks for the v0.2.1 deliverable."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
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
experimental_optimizer = _load("experimental_optimizer_integration", SCRIPTS / "experimental_optimizer.py")


def test_dashboard_renders_supplied_widgets_without_external_assets(tmp_path: Path) -> None:
    fixture = {
        "title": "Reviewed fixture",
        "run_id": "RUN-TEST",
        "limitations": ["Proxy only; no new metric calculation."],
        "domains": [
            {
                "id": "second",
                "title": "Second domain first",
                "order": 1,
                "decision_flow": [{"id": "flow", "title": "Decision flow supplied", "order": 1, "widget_ids": ["line-1"]}],
            },
            {"id": "first", "title": "First domain second", "order": 2, "decision_flow": [{"id": "flow", "title": "Second decision flow", "order": 1, "widget_ids": ["kpi-1", "bar-1", "stack-1", "heat-1", "scatter-1", "donut-1", "table-1"]}]},
        ],
        "widgets": [
            {"id": "kpi-1", "type": "kpi", "title": "Supplied KPI", "value": "4,032/4,093", "unit": "orders", "trace_refs": ["Q-001/final"], "reviewed_item_id": "Q-001"},
            {"id": "bar-1", "type": "bar", "title": "Bars", "bars": [{"label": "A", "value": "3", "share": "50%"}], "trace_refs": ["Q-002/final"]},
            {"id": "line-1", "type": "line", "title": "Line", "points": [{"label": "Jan", "value": "7"}], "trace_refs": ["Q-003/final"]},
            {"id": "stack-1", "type": "stacked_composition", "title": "Composition", "segments": [{"label": "A", "value": "2", "share": "25%"}], "trace_refs": ["Q-004/final"]},
            {"id": "heat-1", "type": "heatmap", "title": "Heat", "cells": [{"label": "A", "value": "high", "intensity": "high"}], "trace_refs": ["Q-005/final"]},
            {"id": "scatter-1", "type": "scatter", "title": "Scatter", "points": [{"label": "A", "x": "1", "y": "2"}], "trace_refs": ["Q-006/final"]},
            {"id": "donut-1", "type": "donut", "title": "Supplied small categories", "categories": [{"label": "A", "value": "1", "share": "100%"}], "trace_refs": ["Q-007/final"]},
            {"id": "table-1", "type": "table", "title": "Drill down", "columns": ["id", "status"], "rows": [{"id": "A", "status": "partial"}], "trace_refs": ["Q-008/final"]},
        ],
    }
    for index, widget in enumerate(fixture["widgets"], 1):
        question_ref = f"Q-{index:03d}/final_answer.md"
        widget["reviewed_item_ref"] = f"Q-{index:03d}"
        widget["reviewed_output_ref"] = question_ref
        widget["evidence_refs"] = [f"Q-{index:03d}/evidence"]
    fixture_path = tmp_path / "fixture.json"
    html_path = tmp_path / "dashboard.html"
    manifest_path = tmp_path / "dashboard_manifest.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    manifest = dashboard_renderer.render_fixture(fixture_path, html_path, manifest_path)
    document = html_path.read_text(encoding="utf-8")
    assert manifest["internal_links_checked"] is True
    assert manifest["new_analytics"] is False
    assert manifest["organization"] == "business_domain_and_decision_flow"
    assert manifest["domain_order"] == ["second", "first"]
    assert manifest["decision_flow_order"] == [
        {"domain_id": "second", "flow_id": "flow"},
        {"domain_id": "first", "flow_id": "flow"},
    ]
    for kind in ("kpi", "bar", "line", "stacked_composition", "heatmap", "scatter", "donut", "table"):
        assert f"widget-{kind}" in document
    assert "4,032/4,093" in document
    assert "Proxy only; no new metric calculation." in document
    assert "Q-001/final" in document
    assert "href=\"#trace-Q-001-final\"" in document
    assert "https://" not in document
    assert document.index("Second domain first") < document.index("First domain second")
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["internal_links_checked"] is True
    manifest_item = next(item for item in manifest["items"] if item["element_id"] == "widget-kpi-1")
    assert manifest_item["trace_refs"] == ["Q-001/final"]
    assert manifest_item["evidence_refs"] == ["Q-001/evidence"]
    assert manifest_item["trace_anchors"] == ["trace-Q-001-final"]
    evidence_only = json.loads(json.dumps(fixture))
    evidence_only["widgets"][0].pop("trace_refs")
    _, evidence_only_manifest = dashboard_renderer.render_dashboard(evidence_only)
    evidence_only_item = next(item for item in evidence_only_manifest["items"] if item["element_id"] == "widget-kpi-1")
    assert evidence_only_item["trace_refs"] == []
    assert evidence_only_item["evidence_refs"] == ["Q-001/evidence"]
    assert evidence_only_item["trace_anchors"] == ["trace-Q-001-evidence"]
    assert "unspecified" not in document.lower()
    assert "trace reference not supplied" not in document.lower()

    incomplete = json.loads(json.dumps(fixture))
    del incomplete["widgets"][0]["reviewed_item_ref"]
    with pytest.raises(ValueError, match="reviewed_item_ref"):
        dashboard_renderer.render_dashboard(incomplete)

    empty_output_ref = json.loads(json.dumps(fixture))
    empty_output_ref["widgets"][0]["reviewed_output_ref"] = {}
    with pytest.raises(ValueError, match="reviewed_output_ref"):
        dashboard_renderer.render_dashboard(empty_output_ref)

    no_provenance = json.loads(json.dumps(fixture))
    no_provenance["widgets"][0].pop("evidence_refs")
    no_provenance["widgets"][0].pop("trace_refs")
    with pytest.raises(ValueError, match="evidence_refs or trace_refs"):
        dashboard_renderer.render_dashboard(no_provenance)

    absent_domains = json.loads(json.dumps(fixture))
    absent_domains.pop("domains")
    with pytest.raises(ValueError, match="domains"):
        dashboard_renderer.render_dashboard(absent_domains)

    unknown_domain = json.loads(json.dumps(fixture))
    unknown_domain["widgets"][0]["domain_id"] = "not-a-domain"
    with pytest.raises(ValueError, match="domain assignment"):
        dashboard_renderer.render_dashboard(unknown_domain)

    missing_order = json.loads(json.dumps(fixture))
    missing_order["domains"][0].pop("order")
    with pytest.raises(ValueError, match="order"):
        dashboard_renderer.render_dashboard(missing_order)

    invalid_flow = json.loads(json.dumps(fixture))
    invalid_flow["domains"][0]["decision_flow"] = []
    with pytest.raises(ValueError, match="decision_flow"):
        dashboard_renderer.render_dashboard(invalid_flow)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _complete_freeze() -> dict[str, bool]:
    return {
        "answers_frozen": True,
        "living_enterprise_model_frozen": True,
        "prepared_assets_frozen": True,
        "dashboard_frozen": True,
        "telemetry_frozen": True,
    }


def test_optimizer_requires_freeze_reports_categories_and_preserves_hashes(tmp_path: Path) -> None:
    products = tmp_path / "product_manifest.json"
    telemetry = tmp_path / "events.jsonl"
    traces = tmp_path / "traces"
    scripts = tmp_path / "scripts"
    optimizer = tmp_path / "optimizer"
    traces.mkdir()
    scripts.mkdir()
    script_one = scripts / "one.py"
    script_two = scripts / "two.py"
    script_one.write_text("print('same')\n", encoding="utf-8")
    script_two.write_text("print('same')\n", encoding="utf-8")
    trace = traces / "trace.md"
    trace.write_text("repeated read context; capability gap observed\n", encoding="utf-8")
    telemetry.write_text(
        json.dumps({"event_type": "review_routed", "status": "unavailable", "error_class": "cache_miss", "classification": "substrate"}) + "\n",
        encoding="utf-8",
    )
    products.write_text(json.dumps({**_complete_freeze(), "run_id": "RUN-TEST", "review_routing": {"fresh_sol_review_available": False}}), encoding="utf-8")
    inputs = [products, telemetry, trace, script_one, script_two]
    before = {_path: _digest(_path) for _path in inputs}
    result = experimental_optimizer.run_optimizer(products_manifest=products, optimizer_dir=optimizer, telemetry=[telemetry], traces=[traces], scripts=[scripts])
    after = {_path: _digest(_path) for _path in inputs}
    assert before == after
    assert result["input_hashes_unchanged"] is True
    assert result["output_names"] == ["experimental_optimizer_report.md", "experimental_optimizer_evidence_appendix.md"]
    assert sorted(path.name for path in optimizer.iterdir()) == ["experimental_optimizer_evidence_appendix.md", "experimental_optimizer_report.md"]
    report = (optimizer / "experimental_optimizer_report.md").read_text(encoding="utf-8")
    appendix = (optimizer / "experimental_optimizer_evidence_appendix.md").read_text(encoding="utf-8")
    for category in ("repeated_code", "repeated_reads_context", "cache_misses", "reviewer_bottleneck", "capability_gaps"):
        assert f"### {category}" in report
    for field in ("Observed evidence", "Hypothesis", "Recommendation", "Expected benefit", "Risk", "Generality"):
        assert f"**{field}:**" in report
    assert "All analytical inputs unchanged: yes" in appendix

    generic = tmp_path / "generic.json"
    generic.write_text(json.dumps({"frozen": True}), encoding="utf-8")
    with pytest.raises(experimental_optimizer.OptimizerPreconditionError):
        experimental_optimizer.run_optimizer(products_manifest=generic, optimizer_dir=tmp_path / "generic-output")

    products_only = tmp_path / "products-only.json"
    products_only.write_text(json.dumps({"products_frozen": True}), encoding="utf-8")
    with pytest.raises(experimental_optimizer.OptimizerPreconditionError):
        experimental_optimizer.run_optimizer(products_manifest=products_only, optimizer_dir=tmp_path / "products-only-output")

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**_complete_freeze(), "automation_classification": "client_business_automation"}), encoding="utf-8")
    with pytest.raises(experimental_optimizer.OptimizerPreconditionError):
        experimental_optimizer.run_optimizer(products_manifest=bad, optimizer_dir=tmp_path / "bad-output")

    with pytest.raises(FileNotFoundError):
        experimental_optimizer.run_optimizer(
            products_manifest=products,
            optimizer_dir=tmp_path / "missing-output",
            telemetry=[tmp_path / "missing-events.jsonl"],
        )


def test_benchmark_a_is_preparation_only_and_links_resolve() -> None:
    expected = {"README.md", "questions.md", "run_config.example.json", "baseline_v0.2.0.json", "comparison_schema.json", "evaluation_checklist.md", "commands.md"}
    assert {path.name for path in BENCHMARK.iterdir() if path.is_file()} == expected
    parsed = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in BENCHMARK.glob("*.json")}
    expected_question_hash = "3a40d2f7083f0d2f0e1b216d405a0ce6c38cd4913e157b9e48a99dfa96958236"
    assert parsed["run_config.example.json"]["question_order_sha256"] == expected_question_hash
    assert parsed["baseline_v0.2.0.json"]["baseline"]["question_order_sha256"] == expected_question_hash
    required_comparison_fields = {
        "answer_quality",
        "model_tool_workload",
        "core_cache_use",
        "prepared_data_reuse",
        "dashboard_quality",
        "source_immutability",
    }
    assert required_comparison_fields.issubset(parsed["comparison_schema.json"]["required"])
    assert expected_question_hash in (BENCHMARK / "questions.md").read_text(encoding="utf-8")
    for path in BENCHMARK.glob("*.md"):
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            target_path = target.split("#", 1)[0]
            if not target_path or "://" in target_path or target_path.startswith("mailto:"):
                continue
            assert (path.parent / target_path).is_file(), f"broken Benchmark A link in {path}: {target}"
    readme = (BENCHMARK / "README.md").read_text(encoding="utf-8")
    commands = (BENCHMARK / "commands.md").read_text(encoding="utf-8")
    assert "not executed" in readme.lower()
    assert "PREPARE" in commands and "LAUNCH LATER" in commands
    assert "explicit confirmation" in commands.lower()
    assert expected_question_hash in commands
    for marker in ("skill_name: auto-foundry-agentic-e2e", "skill_version: 0.2.1", "core_name: auto_foundry_core", "core_version: 0.1.0"):
        assert marker in commands
    assert "82e9c913bf437ac9e361d6890467a9aed9b1c6db9d887cfcf0cd659035a71ec2" in commands
    assert not (BENCHMARK / "RUN-20260807-agentic-e2e-cleanroom-019fdd6a").exists()
