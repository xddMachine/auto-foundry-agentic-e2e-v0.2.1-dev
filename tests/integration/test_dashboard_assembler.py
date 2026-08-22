"""Focused product-only checks for the deterministic dashboard assembler."""

from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import copy
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

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
RENDERER_SCRIPT = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_renderer.py"
renderer_spec = importlib.util.spec_from_file_location("dashboard_renderer_product_test", RENDERER_SCRIPT)
assert renderer_spec and renderer_spec.loader
dashboard_renderer = importlib.util.module_from_spec(renderer_spec)
sys.modules[renderer_spec.name] = dashboard_renderer
renderer_spec.loader.exec_module(dashboard_renderer)

from auto_foundry_core.durable import ItemWorkspace  # noqa: E402
from auto_foundry_core.integration import IntegrationSession  # noqa: E402
from auto_foundry_core.lifecycle import RunLifecycle  # noqa: E402
from auto_foundry_core.prepared import PreparedAssetRegistry  # noqa: E402
from auto_foundry_core.requirement_planning import inspect_product_manifest  # noqa: E402
from auto_foundry_core.workspace import RunContext  # noqa: E402


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
    assert dashboard_assembler._validate_business_presentation_plan(
        context,
        plan,
        generation_id=plan["generation_id"],
        supervisor_ref=plan["supervisor_plan_ref"],
        supervisor_plan=json.loads((source / "requirement_supervisor_plan.json").read_text(encoding="utf-8")),
        input_items=reversed_parent_order,
        parent=plan["parent"],
    ) == plan["manager_widget_ids"]
    # An exact retry is stable, including when the parent presents the same
    # bindings in its other valid order.
    assert dashboard_assembler._validate_business_presentation_plan(
        context,
        plan,
        generation_id=plan["generation_id"],
        supervisor_ref=plan["supervisor_plan_ref"],
        supervisor_plan=json.loads((source / "requirement_supervisor_plan.json").read_text(encoding="utf-8")),
        input_items=reversed_parent_order,
        parent=plan["parent"],
    ) == plan["manager_widget_ids"]

    invalid_cases = {
        "duplicate": [first, first],
        "missing": [first],
        "foreign": [first, {**second, "item_id": "REQ-FOREIGN"}],
        "hash": [first, {**second, "accepted_content_hash": "0" * 64}],
        "count": [first, {**second, "record_count": second["record_count"] + 1}],
    }
    for name, bindings in invalid_cases.items():
        with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="input bindings drifted"):
            dashboard_assembler._validate_business_presentation_plan(
                context,
                plan,
                generation_id=plan["generation_id"],
                supervisor_ref=plan["supervisor_plan_ref"],
                supervisor_plan=json.loads((source / "requirement_supervisor_plan.json").read_text(encoding="utf-8")),
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
            # One-time V1 migration contract.
            "previous_manager_widget_ids": ["legacy-claim"],
            "previous_manager_entries": [copy.deepcopy(legacy_entry)],
            # Full V2 predecessor contract, including visual envelopes.
            "previous_plan_manager_widget_ids": ["legacy-claim", "fact-bar"],
            "previous_plan_manager_entries": [copy.deepcopy(legacy_entry), copy.deepcopy(bar_manager)],
            "previous_manager_visual_widget_ids": ["fact-bar"],
            "previous_audit_visual_widget_ids": ["fact-table"],
            "previous_visual_entries": [copy.deepcopy(bar_visual), copy.deepcopy(audit_visual)],
        },
    }
    dashboard_assembler._validate_presentation_plan_v2_shape(plan)

    tampered_manager = copy.deepcopy(plan)
    tampered_manager["source_bindings"]["previous_plan_manager_entries"][1]["chart_entry_sha256"] = "9" * 64
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="predecessor plan-manager drifted"):
        dashboard_assembler._validate_presentation_plan_v2_shape(tampered_manager)

    tampered_visual = copy.deepcopy(plan)
    tampered_visual["source_bindings"]["previous_visual_entries"][0]["visual_projection"]["type"]["value"] = "column"
    with pytest.raises(dashboard_assembler.BusinessPresentationPlanError, match="predecessor visual drifted"):
        dashboard_assembler._validate_presentation_plan_v2_shape(tampered_visual)


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
    assert not (source / "products" / ".repro_dashboard_v4.previous").exists()

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
    assert claim_widget["rows"] == []
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
    assert claim_text not in document
    audit_entry = dashboard_assembler._audit_widget_entries([claim_widget])
    audit_html, _ = dashboard_renderer._render_widget_audit(audit_entry)
    assert claim_text in audit_html
    assert manifest["items"][0]["kind"] == "table"
    assert manifest["items"][0]["evidence_refs"] == [
        "work/results/req16_analysis.json",
        "work/results/req16_exception_queue.json",
        "work/results/req16_source_local_summary.json",
    ]


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
    assert claim_04 not in document
    assert "http://" not in document and "https://" not in document

    pages, _site_manifest = dashboard_renderer.render_dashboard_site(fixture)
    audit_page = pages["data-quality-audit.html"]
    audit_text = audit_page.decode("utf-8") if isinstance(audit_page, bytes) else audit_page
    assert claim_04 in audit_text
    visible = re.sub(r"<details\b[^>]*>.*?</details>", "", audit_text, flags=re.DOTALL)
    assert claim_04 not in visible
    assert "REQ16-claim-" not in visible


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
        if any(part in {"work", "calculations"} for part in path.parts):
            raise AssertionError(f"assembler attempted forbidden read: {path}")
        return original_read_bytes(path)

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if any(part in {"work", "calculations"} for part in path.parts):
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


def test_overview_selection_skips_preexisting_blank_kpis_deterministically() -> None:
    widgets = [
        {"id": "blank", "type": "kpi", "value": "   "},
        {"id": "first", "type": "kpi", "value": 7},
        {"id": "second", "type": "kpi", "value": 8},
    ]
    assert dashboard_assembler._apply_overview_selection(widgets) == ["first", "second"]
    assert widgets[0].get("overview") is None
    assert widgets[1]["overview"] is True and widgets[2]["overview"] is True


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


def test_no_fact_metrics_keep_one_primary_per_record_and_hide_schema_only_projection() -> None:
    run_root = ROOT.parent / "benchmark_a_requirement_v070_entity_run_a_rerun_1"
    records_root = run_root / "requirements"
    if not records_root.is_dir():
        pytest.skip("benchmark reviewed records are not present in this checkout")
    for item_id in ("REQ-05", "REQ-06", "REQ-08"):
        records_path = records_root / item_id / "integration" / "committed" / "records.jsonl"
        records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
        widgets = dashboard_assembler._build_widgets(item_id, {}, records)
        assert any(widget.get("presentation_tier") == "audit" for widget in widgets)
        by_record: dict[str, list[dict[str, object]]] = {}
        for widget in widgets:
            if widget.get("presentation_role") in {"finding_list", "finding_record", "relationship_matrix"}:
                continue
            for record_id in dashboard_assembler._metric_record_ids(widget):
                by_record.setdefault(record_id, []).append(widget)
        assert all(sum(widget.get("presentation_tier") != "audit" for widget in group) <= 1 for group in by_record.values())
        assert all(
            not (
                widget.get("presentation_tier") != "audit"
                and dashboard_assembler._schema_only_metric_widget(widget)
            )
            for widget in widgets
        )


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
    key_signals = next(widget for widget in widgets if widget.get("title") == "Key signals")
    tiles = {tile["label"]: tile for tile in key_signals["tiles"]}
    assert tiles["Closed tickets"]["denominator"] == 1142
    assert "Mapped orders" not in tiles
    mapped_widget = next(widget for widget in widgets if widget.get("integration_record_id") == "REQ-12-mapped")
    assert mapped_widget.get("audit_payload", {}).get("denominator") == 1142
    html = dashboard_renderer._render_metric_grid(key_signals)
    assert "460 of 1142" in html and "785 of 1142" not in html


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
    assert "records" not in html
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
    """Actual-shaped reviewed records become summary-first manager pages."""

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
        limits = {"REQ-12": 10, "REQ-15": 7, "REQ-17": 7}
        assert primary_counts[item_id] <= limits[item_id]
        if any(record.get("kind") == "dashboard_fact" for record in records):
            key_signals = [widget for widget in item_widgets if widget.get("title") == "Key signals"]
            assert all(widget.get("presentation_tier") == "audit" or widget.get("presentation_role") == "decision_view" for widget in key_signals)
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
    assert domain_text.count('class="technical-audit"') >= 1
    if any(widget.get("presentation_role") == "finding_list" and widget.get("presentation_tier") != "audit" for widget in widgets):
        assert "Reviewed findings" in visible
    assert "Source-local native cost distribution" not in visible
    assert "REQ-15 decision view" not in domain_text
    assert "REQ-15 ·" not in domain_text
    assert re.search(r'<span class="eyebrow requirement-id-badge">REQ-15</span><h2>[^<]+</h2>', domain_text)
    assert "Key signals" in visible
    assert "One to one" not in visible
    assert "one_to_one" in domain_text
    assert "REQ-12.reused.REL" not in visible
    assert "row_kind" not in visible and "value_json" not in visible
    assert not re.search(r"\b[0-9a-f]{64}\b", visible, flags=re.IGNORECASE)
    assert not re.search(r"(?:requirements|products|extensions|telemetry|work)/[^ <\"&]+", visible)
    assert all(item["widget_ids"] for item in manifest["requirement_groups"])
    for group in manifest["requirement_groups"]:
        assert set(group["widget_ids"]) == set(group["primary_widget_ids"]) | set(group["audit_widget_ids"])
