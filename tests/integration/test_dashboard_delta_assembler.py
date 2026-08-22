"""Focused same-run dashboard/product delta contract checks.

These tests build small synthetic Requirement Mode runs in temporary roots.  A
parent dashboard is assembled once, requirements are admitted through the
normal generation extension API, and the delta assembler is then exercised
against the immutable parent receipt.  No frozen rerun or source/work data is
used by this suite.
"""

from __future__ import annotations

import hashlib
import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Mapping

import pytest


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


dashboard_assembler = _load_script(
    "dashboard_assembler_delta_test",
    ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_assembler.py",
)
dashboard_delta = _load_script(
    "dashboard_delta_assembler_test",
    ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_delta_assembler.py",
)
dashboard_renderer = _load_script(
    "dashboard_renderer_delta_test",
    ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts" / "dashboard_renderer.py",
)

from auto_foundry_core import (  # noqa: E402
    ItemWorkspace,
    PreparedAssetRegistry,
    RequirementExecutionGroup,
    RequirementExecutionPlan,
    RequirementRecord,
    RequirementRunExtension,
    RequirementSupervisorWorkspace,
    RunContext,
    RunLifecycle,
)
from auto_foundry_core.integration import IntegrationSession  # noqa: E402


def _record(item_id: str) -> RequirementRecord:
    return RequirementRecord(
        requirement_id=item_id,
        original_text=f"Investigate {item_id}.",
        business_objective=f"Support {item_id}",
        expected_analytical_outputs=(f"output-{item_id}",),
    )


def _complete_item(context: RunContext, item: ItemWorkspace, invocation: str) -> None:
    """Create the normal accepted bundle and one typed committed metric."""

    item.write_plan({"item_id": item.item_id, "offline": True})
    item.write_draft(
        {
            "item_id": item.item_id,
            "answer": "bounded",
            "limitations": ["synthetic only"],
        }
    )
    item.record_review("accept", reviewer_ref="synthetic-reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "synthetic-integration",
        invocation_id=invocation,
    )
    session.add_metric(
        metric_id=f"metric-{item.item_id.lower()}",
        scope=item.item_id,
        evidence_refs=("answer_content.json",),
        label=f"Reviewed {item.item_id}",
        units="records",
        value=7,
        population=10,
    )
    if session.fidelity_result is None:
        session.record_fidelity_review(
            "accept",
            checked_record_ids=tuple(record.record_id for record in session.records),
        )
    assert session.validate().valid
    session.commit()


def _telemetry(context: RunContext) -> None:
    telemetry = context.run_root / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    (telemetry / "events.jsonl").write_text('{"event":"synthetic"}\n', encoding="utf-8")
    (telemetry / "inventory_counters.json").write_text(
        '{"accepted":1}\n', encoding="utf-8"
    )


def _canonical_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    )


def _rewrite_child_receipt_product_binding(context: RunContext, receipt_path: Path, receipt: Mapping[str, Any]) -> None:
    """Keep the generation product manifest bound to an intentionally edited receipt."""

    _canonical_write(receipt_path, receipt)
    receipt_ref = "products/generations/G-0002/dashboard/build_receipt.json"
    receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    product_path = context.resolve_product_path("generations/G-0002/product_manifest.json")
    product = json.loads(product_path.read_text(encoding="utf-8"))
    product["lem"] = receipt["freeze_inputs"]["summary"]
    product["dashboard"]["receipt_sha256"] = receipt_sha256
    for asset in product.get("assets", []):
        if isinstance(asset, Mapping) and asset.get("ref") == receipt_ref:
            asset["sha256"] = receipt_sha256
    _canonical_write(product_path, product)


def test_planless_legacy_receipt_is_validated_before_plan_field_normalization(tmp_path: Path) -> None:
    """The old canonical shape is valid only as an explicit migration target."""

    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})

    receipt_path = context.resolve_product_path("generations/G-0002/dashboard/build_receipt.json")
    legacy = json.loads(receipt_path.read_text(encoding="utf-8"))
    for field in ("presentation_plan_ref", "presentation_plan_sha256", "manager_widget_ids"):
        legacy.pop(field, None)
        legacy["request_binding"].pop(field, None)
    _rewrite_child_receipt_product_binding(context, receipt_path, legacy)
    raw_bytes = receipt_path.read_bytes()
    normalized = dashboard_delta._normalize_existing_presentation_receipt(legacy)
    assert raw_bytes == dashboard_delta._canonical_bytes(legacy)
    # This is the exact production failure mode: normalizing in memory adds
    # fields that are absent from the historical raw receipt, so comparing the
    # raw bytes against the normalized object is necessarily wrong.
    assert raw_bytes != dashboard_delta._canonical_bytes(normalized)

    dashboard_delta._validate_delta_output(
        context,
        receipt_path,
        normalized,
        legacy_raw_receipt=legacy,
        legacy_raw_bytes=raw_bytes,
    )
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="not canonical"):
        dashboard_delta._validate_delta_output(context, receipt_path, normalized)

    tampered = json.loads(json.dumps(legacy))
    tampered["output_hashes"]["fixture_sha256"] = "0" * 64
    tampered_bytes = dashboard_delta._canonical_bytes(tampered)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="output hash mismatch"):
        dashboard_delta._validate_delta_output(
            context,
            receipt_path,
            dashboard_delta._normalize_existing_presentation_receipt(tampered),
            legacy_raw_receipt=tampered,
            legacy_raw_bytes=tampered_bytes,
        )

    plan_bearing = json.loads(json.dumps(legacy))
    plan_bearing["presentation_plan_ref"] = None
    plan_bearing_bytes = dashboard_delta._canonical_bytes(plan_bearing)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="schema is not exact"):
        dashboard_delta._validate_planless_legacy_receipt_shape(plan_bearing, raw_bytes=plan_bearing_bytes)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        key: hashlib.sha256(value).hexdigest()
        for key, value in _tree_bytes(root).items()
    }


def _tree_state(root: Path) -> dict[str, tuple[str, int, int, bytes | str | None]]:
    """Capture every product-tree node, including directory metadata."""

    if not root.exists() and not root.is_symlink():
        return {}
    paths = [root, *sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix())]
    state: dict[str, tuple[str, int, int, bytes | str | None]] = {}
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        stat = path.lstat()
        if path.is_symlink():
            payload: bytes | str | None = os.readlink(path)
            kind = "symlink"
        elif path.is_file():
            payload = path.read_bytes()
            kind = "file"
        elif path.is_dir():
            payload = None
            kind = "dir"
        else:
            payload = None
            kind = "other"
        state[relative] = (kind, stat.st_mode, stat.st_mtime_ns, payload)
    return state


def test_v1_presentation_receipt_migrates_to_v2_and_exact_retry_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound V1 child upgrades once to V2 without touching an exact retry."""

    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}

    # Seed the generation product and use its authoritative inventory to build
    # a real V1 plan.  The parent generation manifest is needed by the public
    # presentation inventory lineage check in this synthetic run.
    _assemble(context, route)
    generation_root = context.resolve_product_path("generations/G-0002")
    generation_manifest = context.resolve_product_path("generations/G-0001/product_manifest.json")
    generation_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(context.resolve_product_path("product_manifest.json"), generation_manifest)
    # The small synthetic parent manifest intentionally omits the legacy bridge
    # envelope; keep this test focused on V1->V2 migration while preserving the
    # exact parent hash/receipt bindings returned by the production validator.
    parent_manifest = context.resolve_product_path("product_manifest.json")
    parent_receipt_path = context.resolve_product_path("parent-dashboard/build_receipt.json")
    monkeypatch.setattr(
        dashboard_delta,
        "_validate_parent_product_manifest",
        lambda *_args, **_kwargs: (
            "products/product_manifest.json",
            hashlib.sha256(parent_manifest.read_bytes()).hexdigest(),
            "products/parent-dashboard/build_receipt.json",
            hashlib.sha256(parent_receipt_path.read_bytes()).hexdigest(),
        ),
    )
    fixture_ref = "products/generations/G-0002/dashboard/dashboard_fixture_v4.json"
    chart_map_ref = "products/generations/G-0002/dashboard/dashboard_chart_map_v4.json"
    inventory = dashboard_assembler.business_presentation_inventory(
        context,
        fixture_ref=fixture_ref,
        generation_id="G-0002",
    )
    first_candidate = inventory["candidates"][0]
    v1_entry = {
        key: first_candidate[key]
        for key in (
            "widget_id",
            "record_id",
            "requirement_id",
            "presentation_role",
            "file_sha256",
            "canonical_payload_sha256",
            "display_projection",
        )
    }
    v1_ref = "extensions/G-0002/predecessor.json"
    v1 = {
        "schema_version": dashboard_assembler.PRESENTATION_PLAN_SCHEMA,
        "run_id": context.run_id,
        "generation_id": "G-0002",
        "supervisor_plan_ref": inventory["supervisor_plan_ref"],
        "supervisor_plan_sha256": inventory["supervisor_plan_sha256"],
        "item_order": inventory["item_order"],
        "input_items": inventory["input_items"],
        "parent": inventory["parent"],
        "reviewer_ref": "synthetic-v1-reviewer",
        "manager_widget_ids": [v1_entry["widget_id"]],
        "manager_entries": [v1_entry],
    }
    dashboard_assembler._validate_presentation_plan_shape(v1)
    v1_path = context.resolve_run_path(v1_ref)
    _canonical_write(v1_path, v1)
    v1_receipt = _assemble(context, route, presentation_plan_ref=v1_ref)
    assert v1_receipt["presentation_plan_ref"] == v1_ref
    assert v1_receipt["presentation_plan_sha256"] == hashlib.sha256(v1_path.read_bytes()).hexdigest()
    v1_fixture = json.loads((generation_root / "dashboard/dashboard_fixture_v4.json").read_text(encoding="utf-8"))
    assert v1_fixture.get("presentation_plan_schema") is None
    assert v1_fixture["manager_entries"] == [v1_entry]

    # Construct a shape-valid V2 successor that explicitly preserves the V1
    # predecessor envelope and partitions the real fixture's visual universe.
    visual_inventory = dashboard_assembler.business_presentation_visual_inventory(
        context,
        fixture_ref=fixture_ref,
        chart_map_ref=chart_map_ref,
    )
    first_id = v1_entry["widget_id"]
    first_visual = next(entry for entry in visual_inventory["visual_entries"] if entry["widget_id"] == first_id)
    audit_visual = next(entry for entry in visual_inventory["visual_entries"] if entry["widget_id"] != first_id)
    v2_manager_entry = dashboard_assembler._v2_manager_entry_from_visual(first_visual)
    v2_manager_entry.update(copy.deepcopy(v1_entry))
    v2_ref = "extensions/G-0002/business_presentation_plan.json"
    v2 = {
        "schema_version": dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": context.run_id,
        "generation_id": "G-0002",
        "supervisor_plan_ref": inventory["supervisor_plan_ref"],
        "supervisor_plan_sha256": inventory["supervisor_plan_sha256"],
        "item_order": inventory["item_order"],
        "input_items": inventory["input_items"],
        "parent": inventory["parent"],
        "reviewer_ref": "synthetic-v2-reviewer",
        "manager_widget_ids": [first_id],
        "manager_entries": [v2_manager_entry],
        "manager_visual_widget_ids": [first_id],
        "audit_visual_widget_ids": [audit_visual["widget_id"]],
        "visual_entries": [first_visual, audit_visual],
        "source_bindings": {
            "fixture_ref": visual_inventory["fixture_ref"],
            "fixture_sha256": visual_inventory["fixture_sha256"],
            "chart_map_ref": visual_inventory["chart_map_ref"],
            "chart_map_sha256": visual_inventory["chart_map_sha256"],
            "previous_plan_ref": v1_ref,
            "previous_plan_sha256": hashlib.sha256(v1_path.read_bytes()).hexdigest(),
            "previous_manager_widget_ids": [first_id],
            "previous_manager_entries": [v1_entry],
            "previous_manager_visual_widget_ids": [first_id],
            "previous_audit_visual_widget_ids": [audit_visual["widget_id"]],
            "previous_visual_entries": [first_visual, audit_visual],
        },
    }
    dashboard_assembler._validate_presentation_plan_v2_shape(v2)
    v2_path = context.resolve_run_path(v2_ref)
    _canonical_write(v2_path, v2)

    migrated = _assemble(context, route, presentation_plan_ref=v2_ref)
    assert migrated["presentation_plan_ref"] == v2_ref
    assert migrated["presentation_plan_sha256"] == hashlib.sha256(v2_path.read_bytes()).hexdigest()
    migrated_fixture = json.loads((generation_root / "dashboard/dashboard_fixture_v4.json").read_text(encoding="utf-8"))
    assert migrated_fixture["presentation_plan_schema"] == dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA
    assert migrated_fixture["manager_visual_widget_ids"] == [first_id]
    assert migrated_fixture["audit_visual_widget_ids"] == [audit_visual["widget_id"]]

    product_tree = context.resolve_product_path("")
    before_retry = _tree_state(product_tree)
    retry = _assemble(context, route, presentation_plan_ref=v2_ref)
    assert retry == migrated
    assert _tree_state(product_tree) == before_retry
    assert not any(
        name.endswith(".dashboard_transaction.json")
        or name == ".dashboard_transaction.previous"
        or name == ".dashboard.staging"
        or name == ".dashboard.previous"
        or ".product.staging-" in name
        or ".product.previous-" in name
        for name in (path.name for path in product_tree.rglob("*"))
    )


def _seed_parent(
    tmp_path: Path,
    *,
    item_ids: tuple[str, ...] = ("REQ-A",),
    groups: tuple[tuple[str, ...], ...] | None = None,
) -> tuple[RunContext, tuple[RequirementRecord, ...], dict[str, Any], dict[str, bytes]]:
    """Return a terminal parent run, parent receipt, and immutable parent bytes."""

    context = RunContext("RUN-DASHBOARD-DELTA", tmp_path / "run")
    records = tuple(_record(item_id) for item_id in item_ids)
    lifecycle = RunLifecycle.create(context, item_ids, mode="requirement")
    for item_id, record in zip(item_ids, records):
        item = ItemWorkspace.create(
            context,
            item_id,
            mode="requirement",
            original_text=record.original_text,
        )
        _complete_item(context, item, f"parent-{item_id}")
    group_values = groups or (tuple(item_ids),)
    plan = RequirementExecutionPlan(
        input_records=records,
        groups=tuple(
            RequirementExecutionGroup(group, f"Synthetic section {index}")
            for index, group in enumerate(group_values, 1)
        ),
        planner_ref="synthetic-planner",
        portfolio_strategy="synthetic-strategy",
        revision=1,
    )
    RequirementSupervisorWorkspace(context).save(plan)
    lifecycle.reconcile_from_run(product_terminal_status="complete")
    assert RunLifecycle.load(context).state == "complete"
    parent_receipt = dashboard_assembler.assemble_dashboard(
        context,
        output_dir="products/parent-dashboard",
    )
    parent_manifest = {
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "run_id": context.run_id,
        "status": "complete",
        "terminal": True,
        "generation_id": "G-0001",
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
            "receipt_ref": "products/parent-dashboard/build_receipt.json",
            "receipt_sha256": hashlib.sha256(
                (context.resolve_product_path("parent-dashboard/build_receipt.json")).read_bytes()
            ).hexdigest(),
        },
        "assets": [
            {
                "ref": "products/parent-dashboard/build_receipt.json",
                "role": "dashboard_receipt",
                "sha256": hashlib.sha256(
                    (context.resolve_product_path("parent-dashboard/build_receipt.json")).read_bytes()
                ).hexdigest(),
            }
        ],
    }
    _canonical_write(context.resolve_product_path("product_manifest.json"), parent_manifest)
    parent_root = context.resolve_product_path("parent-dashboard")
    parent_bytes = _tree_bytes(parent_root)
    return context, records, parent_receipt, parent_bytes


def _append(
    context: RunContext,
    parent_records: tuple[RequirementRecord, ...],
    new_ids: tuple[str, ...],
    groups: tuple[tuple[str, ...], ...],
    *,
    revision: int = 2,
) -> tuple[RequirementRecord, ...]:
    new_records = tuple(_record(item_id) for item_id in new_ids)
    plan = RequirementExecutionPlan(
        input_records=parent_records + new_records,
        groups=tuple(
            RequirementExecutionGroup(group, f"Synthetic section {index}")
            for index, group in enumerate(groups, 1)
        ),
        planner_ref="synthetic-planner",
        portfolio_strategy="synthetic-strategy",
        revision=revision,
    )
    RequirementRunExtension.append(context, new_records, plan=plan)
    for item_id in new_ids:
        _complete_item(
            context,
            ItemWorkspace.load(context, item_id, mode="requirement"),
            f"delta-{item_id}",
        )
    return new_records


def _assemble(
    context: RunContext,
    route: Mapping[str, Any],
    *,
    failpoint: str | None = None,
    parent_receipt_ref: str = "products/parent-dashboard/build_receipt.json",
    presentation_plan_ref: str | None = None,
) -> dict[str, Any]:
    return dashboard_delta.assemble_dashboard_delta(
        context,
        parent_receipt_ref=parent_receipt_ref,
        route=route,
        failpoint=failpoint,
        presentation_plan_ref=presentation_plan_ref,
    )


def _process_delta_publish(args: tuple[str, str, Mapping[str, Any]]) -> tuple[str, str]:
    """Publish from a separate process to exercise the advisory file lock."""

    run_root, run_id, route = args
    context = RunContext(run_id, Path(run_root))
    try:
        result = dashboard_delta.assemble_dashboard_delta(
            context,
            parent_receipt_ref="products/parent-dashboard/build_receipt.json",
            route=route,
        )
        return "ok", str(result["generation_id"])
    except Exception as exc:  # pragma: no cover - asserted by caller
        return "error", f"{type(exc).__name__}: {exc}"


def _process_rebuild_crash(args: tuple[str, str, Mapping[str, Any]]) -> None:
    """Simulate process death immediately after a changed dashboard publish."""

    run_root, run_id, route = args
    context = RunContext(run_id, Path(run_root))
    assembler = dashboard_delta._assembler()
    original = assembler._build_widgets

    def changed(*inner_args: Any, **inner_kwargs: Any) -> list[dict[str, Any]]:
        widgets = original(*inner_args, **inner_kwargs)
        if widgets:
            widgets[0]["title"] = f"{widgets[0].get('title', '')} · process-crash-candidate"
        return widgets

    assembler._build_widgets = changed
    try:
        dashboard_delta.assemble_dashboard_delta(
            context,
            parent_receipt_ref="products/parent-dashboard/build_receipt.json",
            route=route,
            failpoint="after_dashboard_publish",
        )
    except RuntimeError:
        os._exit(0)
    os._exit(1)


def _process_product_copy_crash(args: tuple[str, str, Mapping[str, Any]]) -> None:
    """Die after a replacement copy, before its preparing intent is promoted."""

    run_root, run_id, route = args
    context = RunContext(run_id, Path(run_root))
    assembler = dashboard_delta._assembler()
    original_build = assembler._build_widgets
    original_phase = dashboard_delta._product_transaction_phase

    def changed(*inner_args: Any, **inner_kwargs: Any) -> list[dict[str, Any]]:
        widgets = original_build(*inner_args, **inner_kwargs)
        if widgets:
            widgets[0]["title"] = f"{widgets[0].get('title', '')} · preparing-copy-crash"
        return widgets

    def die_after_copy(path: Path, intent: Mapping[str, Any], phase: str) -> dict[str, Any]:
        if phase == "prepared" and intent.get("schema_version") == dashboard_delta._PRODUCT_TRANSACTION_SCHEMA:
            os._exit(0)
        return original_phase(path, intent, phase)

    assembler._build_widgets = changed
    dashboard_delta._product_transaction_phase = die_after_copy
    try:
        dashboard_delta.assemble_dashboard_delta(
            context,
            parent_receipt_ref="products/parent-dashboard/build_receipt.json",
            route=route,
        )
    except Exception:
        os._exit(2)
    os._exit(1)


def _process_first_write_copy_crash(args: tuple[str, str, Mapping[str, Any]]) -> None:
    """Die when first-write copy enters the owned dashboard staging path."""

    run_root, run_id, route = args
    context = RunContext(run_id, Path(run_root))
    assembler = dashboard_delta._assembler()
    original_copytree = dashboard_delta.shutil.copytree

    def die_on_owned_stage(source: Path, destination: Path, *inner_args: Any, **inner_kwargs: Any) -> Any:
        if Path(destination).name == ".dashboard.staging":
            os._exit(0)
        return original_copytree(source, destination, *inner_args, **inner_kwargs)

    dashboard_delta.shutil.copytree = die_on_owned_stage
    try:
        dashboard_delta.assemble_dashboard_delta(
            context,
            parent_receipt_ref="products/parent-dashboard/build_receipt.json",
            route=route,
        )
    except Exception:
        os._exit(2)
    os._exit(1)


def _process_replan(args: tuple[str, str, float]) -> tuple[str, str]:
    """Persist a higher-revision plan while holding the lifecycle lock."""

    run_root, run_id, hold_seconds = args
    context = RunContext(run_id, Path(run_root))
    try:
        workspace = RequirementSupervisorWorkspace(context)
        current = workspace.load()
        payload = current.to_dict()
        payload["revision"] = int(payload["revision"]) + 1
        payload["portfolio_strategy"] = "process-replan-race"
        revised = RequirementExecutionPlan.from_dict(payload)
        plan_path = workspace.plan_path
        with RunLifecycle._run_lock(context):  # noqa: SLF001 - test same-lock owner
            time.sleep(hold_seconds)
            # The production planner's save path takes this same lifecycle
            # lock internally; this test writes the already-validated typed
            # payload while holding it to model the owner without nesting the
            # non-reentrant advisory lock.
            _canonical_write(plan_path, revised.to_dict())
        return "ok", hashlib.sha256(plan_path.read_bytes()).hexdigest()
    except Exception as exc:  # pragma: no cover - asserted by caller
        return "error", f"{type(exc).__name__}: {exc}"


def test_existing_section_delta_preserves_parent_and_unrelated_assets(tmp_path: Path) -> None:
    context, parent_records, parent_receipt, parent_bytes = _seed_parent(
        tmp_path,
        item_ids=("REQ-A", "REQ-C"),
        groups=(("REQ-A",), ("REQ-C",)),
    )
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"), ("REQ-C",)))
    _telemetry(context)

    receipt = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    fixture = json.loads((child_root / "dashboard_fixture_v4.json").read_text(encoding="utf-8"))
    parent_fixture = json.loads(
        (context.resolve_product_path("parent-dashboard/dashboard_fixture_v4.json")).read_text(encoding="utf-8")
    )
    parent_map = json.loads(
        (context.resolve_product_path("parent-dashboard/dashboard_chart_map_v4.json")).read_text(encoding="utf-8")
    )
    child_map = json.loads((child_root / "dashboard_chart_map_v4.json").read_text(encoding="utf-8"))
    assert receipt["new_analytics"] is False
    assert receipt["generation_id"] == "G-0002"
    assert len(fixture["widgets"]) == 3
    rebuilt_by_id = {widget["id"]: widget for widget in fixture["widgets"]}
    for parent_widget in parent_fixture["widgets"]:
        assert rebuilt_by_id[parent_widget["id"]]["id"] == parent_widget["id"]
    assert {widget["id"] for widget in parent_fixture["widgets"]} <= set(rebuilt_by_id)
    assert child_map["charts"][: len(parent_map["charts"])] == parent_map["charts"]
    assert (child_root / "build_receipt.json").is_file()
    assert context.resolve_product_path("generations/G-0002/product_manifest.json").is_file()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes
    assert json.loads(
        context.resolve_product_path("product_manifest.json").read_text(encoding="utf-8")
    )["generation_id"] == "G-0001"
    assert receipt["affected_paths"]
    # Conservative manager admission may leave this synthetic domain page
    # unchanged; the cumulative audit surface is the changed output.
    assert any(entry["path"] == "data-quality-audit.html" for entry in receipt["affected_paths"])
    unchanged = {entry["path"]: entry["sha256"] for entry in receipt["unchanged_paths"]}
    assert "index.html" in unchanged
    assert hashlib.sha256(
        (context.resolve_product_path("parent-dashboard/site/index.html")).read_bytes()
    ).hexdigest() == unchanged["index.html"]

    product_tree = context.resolve_product_path("")
    product_tree_state = _tree_state(product_tree)
    assert not any(
        name.endswith(".dashboard_transaction.json")
        or name == ".dashboard_transaction.previous"
        or name == ".dashboard.staging"
        or name == ".dashboard.previous"
        or ".product.staging-" in name
        for name in product_tree_state
    )
    child_hashes = _tree_hashes(child_root)
    child_mtimes = {
        key: (child_root / key).stat().st_mtime_ns for key in child_hashes
    }
    product_manifest_path = context.resolve_product_path("generations/G-0002/product_manifest.json")
    product_manifest_hash = hashlib.sha256(product_manifest_path.read_bytes()).hexdigest()
    product_manifest_mtime = product_manifest_path.stat().st_mtime_ns
    retry = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert retry == receipt
    assert _tree_hashes(child_root) == child_hashes
    assert {
        key: (child_root / key).stat().st_mtime_ns for key in child_hashes
    } == child_mtimes
    assert hashlib.sha256(product_manifest_path.read_bytes()).hexdigest() == product_manifest_hash
    assert product_manifest_path.stat().st_mtime_ns == product_manifest_mtime
    assert _tree_state(product_tree) == product_tree_state
    assert not any(
        name.endswith(".dashboard_transaction.json")
        or name == ".dashboard_transaction.previous"
        or name == ".dashboard.staging"
        or name == ".dashboard.previous"
        or ".product.staging-" in name
        for name in _tree_state(product_tree)
    )

    fixture_path = child_root / "dashboard_fixture_v4.json"
    fixture_path.write_bytes(fixture_path.read_bytes() + b"\n")
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="output hash mismatch"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes
    assert parent_receipt["outputs"]["receipt_ref"] == "products/parent-dashboard/build_receipt.json"


def test_presentation_plan_change_during_scratch_render_rejects_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid V2 plan edited while scratch rendering is in flight cannot publish."""

    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    # Build a real cumulative fixture/visual inventory once, then remove the
    # product target so the guarded call exercises first publication without a
    # V1->V2 migration.  The plan bytes themselves remain fully V2-shape-valid.
    _assemble(context, route)
    generation_root = context.resolve_product_path("generations/G-0002")
    child_fixture_ref = "products/generations/G-0002/dashboard/dashboard_fixture_v4.json"
    child_chart_map_ref = "products/generations/G-0002/dashboard/dashboard_chart_map_v4.json"
    generation_manifest = context.resolve_product_path("generations/G-0001/product_manifest.json")
    generation_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(context.resolve_product_path("product_manifest.json"), generation_manifest)
    inventory = dashboard_assembler.business_presentation_inventory(
        context,
        fixture_ref=child_fixture_ref,
        generation_id="G-0002",
    )
    visual_inventory = dashboard_assembler.business_presentation_visual_inventory(
        context,
        fixture_ref=child_fixture_ref,
        chart_map_ref=child_chart_map_ref,
    )
    first_visual = copy.deepcopy(visual_inventory["visual_entries"][0])
    audit_visual = copy.deepcopy(visual_inventory["visual_entries"][1])
    audit_visual["presentation_audience"] = "technical_audit_gallery"
    first_id = first_visual["widget_id"]
    second_id = audit_visual["widget_id"]
    candidate = next(value for value in inventory["candidates"] if value["widget_id"] == first_id)
    v1_entry = {
        key: candidate[key]
        for key in (
            "widget_id",
            "record_id",
            "requirement_id",
            "presentation_role",
            "file_sha256",
            "canonical_payload_sha256",
            "display_projection",
        )
    }
    v1_ref = "extensions/G-0002/predecessor.json"
    v1 = {
        "schema_version": dashboard_assembler.PRESENTATION_PLAN_SCHEMA,
        "run_id": context.run_id,
        "generation_id": "G-0002",
        "supervisor_plan_ref": inventory["supervisor_plan_ref"],
        "supervisor_plan_sha256": inventory["supervisor_plan_sha256"],
        "item_order": inventory["item_order"],
        "input_items": inventory["input_items"],
        "parent": inventory["parent"],
        "reviewer_ref": "synthetic-v1-predecessor",
        "manager_widget_ids": [first_id],
        "manager_entries": [v1_entry],
    }
    dashboard_assembler._validate_presentation_plan_shape(v1)
    v1_path = context.resolve_run_path(v1_ref)
    _canonical_write(v1_path, v1)
    v2_manager_entry = dashboard_assembler._v2_manager_entry_from_visual(first_visual)
    v2_manager_entry.update(v1_entry)
    v2_ref = "extensions/G-0002/business_presentation_plan.json"
    v2 = {
        "schema_version": dashboard_assembler.PRESENTATION_PLAN_V2_SCHEMA,
        "run_id": context.run_id,
        "generation_id": "G-0002",
        "supervisor_plan_ref": inventory["supervisor_plan_ref"],
        "supervisor_plan_sha256": inventory["supervisor_plan_sha256"],
        "item_order": inventory["item_order"],
        "input_items": inventory["input_items"],
        "parent": inventory["parent"],
        "reviewer_ref": "synthetic-v2-reviewer",
        "manager_widget_ids": [first_id],
        "manager_entries": [v2_manager_entry],
        "manager_visual_widget_ids": [first_id],
        "audit_visual_widget_ids": [second_id],
        "visual_entries": [first_visual, audit_visual],
        "source_bindings": {
            "fixture_ref": visual_inventory["fixture_ref"],
            "fixture_sha256": visual_inventory["fixture_sha256"],
            "chart_map_ref": visual_inventory["chart_map_ref"],
            "chart_map_sha256": visual_inventory["chart_map_sha256"],
            "previous_plan_ref": v1_ref,
            "previous_plan_sha256": hashlib.sha256(v1_path.read_bytes()).hexdigest(),
            "previous_manager_widget_ids": [first_id],
            "previous_manager_entries": [v1_entry],
            "previous_manager_visual_widget_ids": [first_id],
            "previous_audit_visual_widget_ids": [second_id],
            "previous_visual_entries": [first_visual, audit_visual],
        },
    }
    dashboard_assembler._validate_presentation_plan_v2_shape(v2)
    plan_path = context.resolve_run_path(v2_ref)
    _canonical_write(plan_path, v2)
    shutil.rmtree(generation_root)

    assembler = dashboard_delta._assembler()
    monkeypatch.setattr(assembler, "_presentation_parent_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(assembler, "_validate_v2_plan_lineage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(assembler, "_validate_business_presentation_plan_v2", lambda *_args, **_kwargs: None)
    parent_manifest = context.resolve_product_path("product_manifest.json")
    parent_receipt_path = context.resolve_product_path("parent-dashboard/build_receipt.json")
    monkeypatch.setattr(
        dashboard_delta,
        "_validate_parent_product_manifest",
        lambda *_args, **_kwargs: (
            "products/product_manifest.json",
            hashlib.sha256(parent_manifest.read_bytes()).hexdigest(),
            "products/parent-dashboard/build_receipt.json",
            hashlib.sha256(parent_receipt_path.read_bytes()).hexdigest(),
        ),
    )
    # The plan's visual snapshots are real and shape-valid; the temporary
    # first-write fixture is intentionally not the old child fixture, so only
    # suppress the renderer's duplicate visual-source assertion here.
    original_spec_from_file_location = dashboard_delta.importlib.util.spec_from_file_location

    def wrapped_spec_from_file_location(name: str, location: Any, *args: Any, **kwargs: Any) -> Any:
        spec = original_spec_from_file_location(name, location, *args, **kwargs)
        if name == "dashboard_renderer_for_delta" and spec is not None and spec.loader is not None:
            original_exec = spec.loader.exec_module

            def exec_module(module: Any) -> None:
                original_exec(module)
                module._validate_fixture_presentation_plan_v2 = lambda *_args, **_kwargs: None

            spec.loader.exec_module = exec_module
        return spec

    monkeypatch.setattr(dashboard_delta.importlib.util, "spec_from_file_location", wrapped_spec_from_file_location)
    original_manifest_update = dashboard_delta._site_manifest_update
    changed = False

    def mutate_plan_after_scratch(site_path: Path, chart_map_path: Path) -> dict[str, Any]:
        nonlocal changed
        if not changed:
            changed = True
            updated = json.loads(plan_path.read_text(encoding="utf-8"))
            updated["reviewer_ref"] = "concurrent-valid-v2-reviewer"
            dashboard_assembler._validate_presentation_plan_v2_shape(updated)
            _canonical_write(plan_path, updated)
        return original_manifest_update(site_path, chart_map_path)

    monkeypatch.setattr(dashboard_delta, "_site_manifest_update", mutate_plan_after_scratch)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="presentation plan changed"):
        _assemble(context, route, presentation_plan_ref=v2_ref)
    assert changed
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes
    assert not any(
        name.startswith(".G-0002.product.staging-")
        or name == ".dashboard.staging"
        or name.endswith(".dashboard_transaction.json")
        for name in (path.name for path in context.resolve_product_path("generations").iterdir())
    )


def test_same_generation_delta_rebuilds_all_cumulative_items_from_authoritative_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child delta refreshes the parent presentation instead of copying it."""

    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(
        tmp_path,
        item_ids=("REQ-A", "REQ-C"),
        groups=(("REQ-A",), ("REQ-C",)),
    )
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"), ("REQ-C",)))
    _telemetry(context)

    assembler = dashboard_delta._assembler()
    original_build_widgets = assembler._build_widgets
    calls: list[str] = []

    def rebuild_all(item_id: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(item_id)
        return original_build_widgets(item_id, *args, **kwargs)

    monkeypatch.setattr(assembler, "_build_widgets", rebuild_all)
    receipt = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert set(calls) == {"REQ-A", "REQ-B", "REQ-C"}
    assert calls.count("REQ-A") == calls.count("REQ-B") == calls.count("REQ-C") == 1

    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    fixture = json.loads((child_root / "dashboard_fixture_v4.json").read_text(encoding="utf-8"))
    parent_fixture = json.loads(
        (context.resolve_product_path("parent-dashboard/dashboard_fixture_v4.json")).read_text(encoding="utf-8")
    )
    child_ids = {widget["id"] for widget in fixture["widgets"]}
    parent_ids = {widget["id"] for widget in parent_fixture["widgets"]}
    assert parent_ids <= child_ids
    assert len(fixture["widgets"]) == len(parent_ids) + 1
    assert receipt["widget_count"] == len(fixture["widgets"])
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes

    before_retry = _tree_hashes(child_root)
    retry = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert retry == receipt
    assert _tree_hashes(child_root) == before_retry


def test_new_section_route_supports_two_siblings_and_global_nav_delta(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(
        context,
        parent_records,
        ("REQ-B", "REQ-C"),
        (("REQ-A",), ("REQ-B", "REQ-C")),
    )
    _telemetry(context)
    receipt = _assemble(
        context,
        {
            "routes": {
                "REQ-B": {"kind": "new", "group_id": "new-section", "title": "Added decisions", "order": 2},
                "REQ-C": {"kind": "new", "group_id": "new-section", "title": "Added decisions", "order": 2},
            }
        },
    )
    fixture = json.loads(
        (context.resolve_product_path("generations/G-0002/dashboard/dashboard_fixture_v4.json")).read_text(
            encoding="utf-8"
        )
    )
    domain = next(value for value in fixture["domains"] if value["id"] == "new-section")
    assert [flow["id"] for flow in domain["decision_flow"]] == ["new-section-REQ-B", "new-section-REQ-C"]
    assert receipt["domain_count"] == 2
    affected = {entry["path"] for entry in receipt["affected_paths"]}
    unchanged = {entry["path"] for entry in receipt["unchanged_paths"]}
    assert {"data-quality-audit.html", "ontology.html", "evidence.html"}.issubset(affected)
    assert "index.html" not in affected
    assert "index.html" in unchanged


@pytest.mark.parametrize(
    ("route", "message"),
    [
        ({}, "requires an explicit safe group_id"),
        ({"kind": "existing", "group_id": "missing"}, "route for REQ-B disagrees"),
        ({"kind": "new", "group_id": "group-01", "title": "Wrong", "order": 2}, "requires an existing route"),
        ({"kind": "existing", "group_id": "free form"}, "requires an explicit safe group_id"),
    ],
)
def test_route_validation_rejects_before_child_writes(
    tmp_path: Path,
    route: Mapping[str, Any],
    message: str,
) -> None:
    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match=message):
        _assemble(context, route)
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()
    assert not context.resolve_product_path("generations/G-0002/product_manifest.json").exists()
    assert context.resolve_product_path("parent-dashboard/build_receipt.json").is_file()


def test_lexical_symlink_parent_reference_rejects_before_external_open(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    external = tmp_path / "external-parent-receipt.json"
    source_receipt = context.resolve_product_path("parent-dashboard/build_receipt.json")
    external.write_bytes(source_receipt.read_bytes())
    before = (external.read_bytes(), external.stat().st_mtime_ns)
    alias = context.run_root / "products" / "parent-receipt-alias.json"
    alias.symlink_to(external)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="symlink alias"):
        _assemble(
            context,
            {"kind": "existing", "group_id": "group-01"},
            parent_receipt_ref="products/parent-receipt-alias.json",
        )
    assert (external.read_bytes(), external.stat().st_mtime_ns) == before
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()


def test_active_generation_product_manifest_symlink_rejects_before_staging(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    external = tmp_path / "immutable-product-manifest.json"
    external.write_bytes(b'{"immutable":true}\n')
    manifest_path = context.resolve_product_path("generations/G-0002/product_manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.symlink_to(external)
    before = (external.read_bytes(), external.stat().st_mtime_ns)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="active generation product manifest.*symlink alias"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert (external.read_bytes(), external.stat().st_mtime_ns) == before
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_parent_lineage_and_plan_tamper_fail_before_child_or_retry(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    parent_manifest_path = context.resolve_product_path("product_manifest.json")
    parent_manifest = json.loads(parent_manifest_path.read_text(encoding="utf-8"))
    parent_manifest["dashboard"]["receipt_ref"] = "products/parent-dashboard/alternate.json"
    _canonical_write(parent_manifest_path, parent_manifest)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="exact parent receipt"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes

    # Restore the parent manifest, then tamper with the admitted active plan.
    _seed_parent_manifest = {
        "schema_version": "1",
        "product_type": "reviewed_run_product_bundle",
        "run_id": context.run_id,
        "status": "complete",
        "terminal": True,
        "generation_id": "G-0001",
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
            "receipt_ref": "products/parent-dashboard/build_receipt.json",
            "receipt_sha256": hashlib.sha256(
                context.resolve_product_path("parent-dashboard/build_receipt.json").read_bytes()
            ).hexdigest(),
        },
        "assets": [
            {
                "ref": "products/parent-dashboard/build_receipt.json",
                "role": "dashboard_receipt",
                "sha256": hashlib.sha256(
                    context.resolve_product_path("parent-dashboard/build_receipt.json").read_bytes()
                ).hexdigest(),
            }
        ],
    }
    _canonical_write(parent_manifest_path, _seed_parent_manifest)
    plan_path = RunLifecycle.load(context).plan_path
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    # A higher-revision replan is legitimate, but a plan that removes the
    # prior item from the active group no longer agrees with the explicit
    # existing-section route and must fail closed.
    plan["groups"][0]["requirement_ids"] = ["REQ-B"]
    _canonical_write(plan_path, plan)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="requires a new route|disagrees"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()


def test_higher_revision_active_plan_replan_binds_live_hash_and_admission_hash(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    lifecycle = RunLifecycle.load(context)
    metadata = lifecycle.generation_metadata
    assert metadata is not None
    plan_path = lifecycle.plan_path
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["revision"] = int(plan["revision"]) + 1
    plan["portfolio_strategy"] = "replanned-without-route-change"
    _canonical_write(plan_path, plan)
    live_plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    _telemetry(context)
    receipt = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert receipt["plan_binding"]["sha256"] == live_plan_hash
    assert receipt["plan_binding"]["admission_sha256"] == metadata.plan_hash
    product = json.loads(
        context.resolve_product_path("generations/G-0002/product_manifest.json").read_text(encoding="utf-8")
    )
    assert product["lineage"]["active_plan_hash"] == live_plan_hash
    assert product["lineage"]["admission_plan_hash"] == metadata.plan_hash
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_ambiguous_active_plan_route_rejects_before_child_writes(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    plan_path = RunLifecycle.load(context).plan_path
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["groups"].append(dict(plan["groups"][0]))
    plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="ambiguous|plan hash drifted"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()


def test_dashboard_delta_failpoints_recover_without_rewriting_parent(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    with pytest.raises(RuntimeError, match="before_dashboard_publish"):
        _assemble(context, route, failpoint="before_dashboard_publish")
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    assert not child_root.exists()
    assert RunLifecycle.load(context).generation_id == "G-0002"
    assert RunLifecycle.load(context).state == "running"
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes
    with pytest.raises(RuntimeError, match="after_dashboard_publish"):
        _assemble(context, route, failpoint="after_dashboard_publish")
    assert child_root.is_dir()
    assert (child_root / "build_receipt.json").is_file()
    assert not context.resolve_product_path("generations/G-0002/product_manifest.json").exists()
    before = _tree_hashes(child_root)
    _assemble(context, route)
    assert _tree_hashes(child_root) == before
    assert context.resolve_product_path("generations/G-0002/product_manifest.json").is_file()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


@pytest.mark.parametrize("failpoint", ("after_dashboard_publish", "before_product_manifest", "after_lifecycle_reconciliation"))
def test_same_generation_rebuild_transaction_recovers_with_preexisting_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    """A changed same-generation candidate never leaves tree/manifest/state split."""

    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    first = _assemble(context, route)
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    generation_root = child_root.parent
    generations_root = generation_root.parent
    manifest_path = generation_root / "product_manifest.json"
    assert manifest_path.is_file()
    old_manifest = manifest_path.read_bytes()
    old_product_binding = dashboard_delta._generation_product_binding(generation_root)
    old_state = (context.run_root / "extensions/G-0002/run_state.json").read_bytes()
    old_parent = _tree_bytes(context.resolve_product_path("parent-dashboard"))
    delta_assembler = dashboard_delta._assembler()
    original_build_widgets = delta_assembler._build_widgets

    def changed_widgets(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        widgets = original_build_widgets(*args, **kwargs)
        if widgets:
            widgets[0]["title"] = f"{widgets[0].get('title', '')} · corrected"
        return widgets

    monkeypatch.setattr(delta_assembler, "_build_widgets", changed_widgets)
    with pytest.raises(RuntimeError, match=failpoint):
        _assemble(context, route, failpoint=failpoint)
    intent_path = generations_root / ".G-0002.dashboard_transaction.json"
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert intent["schema_version"] == dashboard_delta._PRODUCT_TRANSACTION_SCHEMA
    backup_path = context.resolve_product_path(intent["backup_path"])
    candidate_path = context.resolve_product_path(intent["candidate_path"])
    target_binding = dashboard_delta._generation_product_binding(generation_root)
    assert target_binding in (intent["old_binding"], intent["new_binding"])
    if failpoint == "before_product_manifest":
        assert target_binding == intent["old_binding"] == old_product_binding
        assert manifest_path.read_bytes() == old_manifest
        assert candidate_path.is_dir() and not backup_path.exists()
    else:
        assert target_binding == intent["new_binding"]
        assert backup_path.is_dir()
        assert manifest_path.read_bytes() != old_manifest
    if failpoint in {"after_dashboard_publish", "after_lifecycle_reconciliation"}:
        assert (context.run_root / "extensions/G-0002/run_state.json").read_bytes() == old_state
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == old_parent == parent_bytes

    recovered = _assemble(context, route)
    assert recovered != first
    assert not intent_path.exists()
    assert not backup_path.exists()
    assert not candidate_path.exists()
    assert manifest_path.is_file()
    recovered_tree = _tree_bytes(child_root)
    recovered_manifest = manifest_path.read_bytes()
    recovered_state = (context.run_root / "extensions/G-0002/run_state.json").read_bytes()
    assert recovered_manifest != old_manifest
    assert recovered_state == old_state
    retry = _assemble(context, route)
    assert retry == recovered
    assert _tree_bytes(child_root) == recovered_tree
    assert manifest_path.read_bytes() == recovered_manifest
    assert (context.run_root / "extensions/G-0002/run_state.json").read_bytes() == recovered_state
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_delta_candidate_prunes_stale_renderer_pages_but_keeps_unowned_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    delta_assembler = dashboard_delta._assembler()
    original_build_widgets = delta_assembler._build_widgets
    original_copy_bound_parent = dashboard_delta._copy_bound_parent

    def copy_with_stale_candidate(parent_root: Path, parent_refs: Mapping[str, Path], destination: Path) -> None:
        original_copy_bound_parent(parent_root, parent_refs, destination)
        candidate_site = destination / parent_refs["site_ref"].relative_to(parent_root)
        (candidate_site / "domains").mkdir(parents=True, exist_ok=True)
        (candidate_site / "domains" / "stale-old.html").write_text(
            "<html><body>stale renderer page</body></html>\n", encoding="utf-8"
        )
        (candidate_site / "unowned-local-note.txt").write_text("keep this local note\n", encoding="utf-8")

    monkeypatch.setattr(dashboard_delta, "_copy_bound_parent", copy_with_stale_candidate)

    def changed_widgets(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        widgets = original_build_widgets(*args, **kwargs)
        if widgets:
            widgets[0]["title"] = f"{widgets[0].get('title', '')} · stale-page cleanup"
        return widgets

    monkeypatch.setattr(delta_assembler, "_build_widgets", changed_widgets)
    _assemble(context, route)
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    site_root = child_root / "site"
    stale = site_root / "domains" / "stale-old.html"
    unowned = site_root / "unowned-local-note.txt"
    assert not stale.exists()
    assert unowned.read_text(encoding="utf-8") == "keep this local note\n"
    manifest = json.loads((site_root / "site_manifest.json").read_text(encoding="utf-8"))
    assert "domains/stale-old.html" not in manifest["pages"]
    for page in manifest["pages"]:
        assert "stale-old.html" not in (site_root / page).read_text(encoding="utf-8")


def test_process_death_after_changed_publish_is_reconciled_on_retry(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    first = _assemble(context, route)
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    generation_root = child_root.parent
    manifest_path = generation_root / "product_manifest.json"
    old_manifest = manifest_path.read_bytes()
    process_context = multiprocessing.get_context("fork")
    process = process_context.Process(
        target=_process_rebuild_crash,
        args=((str(context.run_root), context.run_id, route),),
    )
    process.start()
    process.join(30)
    assert process.exitcode == 0
    generations_root = generation_root.parent
    intent_path = generations_root / ".G-0002.dashboard_transaction.json"
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    backup_path = context.resolve_product_path(intent["backup_path"])
    candidate_path = context.resolve_product_path(intent["candidate_path"])
    assert backup_path.is_dir()
    assert dashboard_delta._generation_product_binding(generation_root) == intent["new_binding"]
    assert manifest_path.read_bytes() != old_manifest

    delta_assembler = dashboard_delta._assembler()
    original_build_widgets = delta_assembler._build_widgets

    def changed_widgets(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        widgets = original_build_widgets(*args, **kwargs)
        if widgets:
            widgets[0]["title"] = f"{widgets[0].get('title', '')} · process-crash-candidate"
        return widgets

    delta_assembler._build_widgets = changed_widgets
    recovered = _assemble(context, route)
    assert recovered != first
    assert not intent_path.exists()
    assert not backup_path.exists()
    assert not candidate_path.exists()
    tree = _tree_hashes(child_root)
    retry = _assemble(context, route)
    assert retry == recovered
    assert _tree_hashes(child_root) == tree
    delta_assembler._build_widgets = original_build_widgets


def test_process_death_after_product_copy_prepared_intent_is_cleaned_on_retry(tmp_path: Path) -> None:
    """A preparing orphan is owned, removed, and never mistaken for a publish."""

    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    first = _assemble(context, route)
    generation_root = context.resolve_product_path("generations/G-0002")
    old_binding = dashboard_delta._generation_product_binding(generation_root)
    process_context = multiprocessing.get_context("fork")
    process = process_context.Process(
        target=_process_product_copy_crash,
        args=((str(context.run_root), context.run_id, route),),
    )
    process.start()
    process.join(30)
    assert process.exitcode == 0

    intent_path = generation_root.parent / ".G-0002.dashboard_transaction.json"
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert intent["phase"] == "preparing"
    candidate_path = context.resolve_product_path(intent["candidate_path"])
    assert candidate_path.is_dir()
    assert dashboard_delta._generation_product_binding(generation_root) == old_binding

    recovered = _assemble(context, route)
    assert recovered == first
    assert not intent_path.exists()
    assert not candidate_path.exists()
    assert not any(
        path.name.startswith(".G-0002.product.staging-")
        or path.name.startswith(".G-0002.product.previous-")
        for path in generation_root.parent.iterdir()
    )


def test_product_copy_exception_cleans_owned_intent_and_partial_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    _assemble(context, route)
    generation_root = context.resolve_product_path("generations/G-0002")
    before = dashboard_delta._generation_product_binding(generation_root)

    def fail_copy(_source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "partial.bin").write_bytes(b"partial")
        raise RuntimeError("synthetic copy failure")

    monkeypatch.setattr(dashboard_delta, "_copy_generation_product", fail_copy)
    with pytest.raises(RuntimeError, match="synthetic copy failure"):
        _assemble(context, route)
    assert dashboard_delta._generation_product_binding(generation_root) == before
    assert not (generation_root.parent / ".G-0002.dashboard_transaction.json").exists()
    assert not any(path.name.startswith(".G-0002.product.staging-") for path in generation_root.parent.iterdir())


def test_first_write_process_death_during_preparing_copy_recovers_without_residue(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    process_context = multiprocessing.get_context("fork")
    process = process_context.Process(
        target=_process_first_write_copy_crash,
        args=((str(context.run_root), context.run_id, route),),
    )
    process.start()
    process.join(30)
    assert process.exitcode == 0
    generation_root = context.resolve_product_path("generations/G-0002")
    intent_path = generation_root / ".dashboard_transaction.json"
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert intent["phase"] == "preparing"
    assert not (generation_root / ".dashboard.staging").exists()
    assert not (generation_root / "dashboard").exists()

    recovered = _assemble(context, route)
    assert recovered["generation_id"] == "G-0002"
    assert (generation_root / "dashboard" / "build_receipt.json").is_file()
    assert not intent_path.exists()
    assert not (generation_root / ".dashboard.staging").exists()
    assert not (generation_root / ".dashboard_transaction.previous").exists()
    assert not any(path.name.startswith(".G-0002.product.staging-") for path in generation_root.parent.iterdir())


@pytest.mark.parametrize(
    ("failpoint", "expected_target"),
    (
        ("before_product_manifest", "old"),
        ("between_renames", "old"),
        ("after_dashboard_publish", "new"),
    ),
)
def test_recovery_race_with_next_generation_never_publishes_stale_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
    expected_target: str,
) -> None:
    """Admission of G-0003 during G-0002 recovery cannot stale-publish.

    The first attempt leaves a durable G-0002 intent at three transaction
    phases.  A synthetic append runs after the wrapper's initial reload but
    before recovery's lock-bound reload.  Old/missing targets restore or
    abort; a published new target is retained only because G-0003's parent
    state/plan hashes bind G-0002.
    """

    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    records_g2 = _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    first = _assemble(context, route)
    assert first["generation_id"] == "G-0002"
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    generation_root = child_root.parent
    delta_assembler_module = dashboard_delta._assembler()
    original_build_widgets = delta_assembler_module._build_widgets

    def changed_widgets(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        widgets = original_build_widgets(*args, **kwargs)
        if widgets:
            widgets[0]["title"] = f"{widgets[0].get('title', '')} · race-candidate"
        return widgets

    monkeypatch.setattr(delta_assembler_module, "_build_widgets", changed_widgets)
    with pytest.raises(RuntimeError, match=failpoint):
        _assemble(context, route, failpoint=failpoint)
    intent_path = generation_root.parent / ".G-0002.dashboard_transaction.json"
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    old_binding = intent["old_binding"]
    new_binding = intent["new_binding"]
    visible_binding = dashboard_delta._generation_product_binding(generation_root)
    if failpoint == "between_renames":
        assert visible_binding is None
    else:
        assert visible_binding in (old_binding, new_binding)

    original_load_plan = dashboard_delta._load_plan
    appended = False

    def load_plan_then_append(*args: Any, **kwargs: Any) -> tuple[Mapping[str, Any], Path, str]:
        nonlocal appended
        result = original_load_plan(*args, **kwargs)
        if not appended:
            appended = True
            _append(
                context,
                parent_records + records_g2,
                ("REQ-C",),
                (("REQ-A", "REQ-B", "REQ-C"),),
                revision=3,
            )
        return result

    monkeypatch.setattr(dashboard_delta, "_load_plan", load_plan_then_append)
    # Automatic parent resolution selects G-0001 for the stale G-0002
    # attempt, then G-0002 after the wrapper retries against newly active
    # G-0003.
    recovered = _assemble(context, route, parent_receipt_ref=None)
    assert recovered["generation_id"] == "G-0003"
    assert RunLifecycle.load(context).generation_id == "G-0003"
    assert not intent_path.exists()
    assert not any(
        path.name.startswith(".G-0002.product.staging-") or path.name.startswith(".G-0002.product.previous-")
        for path in generation_root.parent.iterdir()
    )
    target_binding = dashboard_delta._generation_product_binding(generation_root)
    assert target_binding == (old_binding if expected_target == "old" else new_binding)
    assert (context.resolve_product_path("generations/G-0003/product_manifest.json")).is_file()


@pytest.mark.parametrize(
    ("failpoint", "expected_target"),
    (
        ("before_product_manifest", "old"),
        ("between_renames", "old"),
        ("after_dashboard_publish", "new"),
    ),
)
def test_separate_next_generation_call_reconciles_parent_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
    expected_target: str,
) -> None:
    """A fresh G-0003 invocation resolves a crashed G-0002 transaction."""

    context, parent_records, _parent_receipt, _parent_bytes = _seed_parent(tmp_path)
    records_g2 = _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    _assemble(context, route)
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    generation_root = child_root.parent
    delta_assembler_module = dashboard_delta._assembler()
    original_build_widgets = delta_assembler_module._build_widgets

    def changed_widgets(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        widgets = original_build_widgets(*args, **kwargs)
        if widgets:
            widgets[0]["title"] = f"{widgets[0].get('title', '')} · separate-g3"
        return widgets

    monkeypatch.setattr(delta_assembler_module, "_build_widgets", changed_widgets)
    with pytest.raises(RuntimeError, match=failpoint):
        _assemble(context, route, failpoint=failpoint)
    intent_path = generation_root.parent / ".G-0002.dashboard_transaction.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    old_binding = intent["old_binding"]
    new_binding = intent["new_binding"]
    _append(
        context,
        parent_records + records_g2,
        ("REQ-C",),
        (("REQ-A", "REQ-B", "REQ-C"),),
        revision=3,
    )

    # This is a separate process-equivalent call: startup runs under the
    # active G-0003 lifecycle lock, resolves only G-0002's intent, then builds
    # the child against whichever parent bytes were proven authoritative.
    child_receipt = _assemble(context, route, parent_receipt_ref=None)
    assert child_receipt["generation_id"] == "G-0003"
    assert RunLifecycle.load(context).generation_id == "G-0003"
    assert not intent_path.exists()
    assert not any(
        path.name.startswith(".G-0002.product.staging-") or path.name.startswith(".G-0002.product.previous-")
        for path in generation_root.parent.iterdir()
    )
    target_binding = dashboard_delta._generation_product_binding(generation_root)
    assert target_binding == (old_binding if expected_target == "old" else new_binding)
    g3_manifest = json.loads(
        context.resolve_product_path("generations/G-0003/product_manifest.json").read_text(encoding="utf-8")
    )
    assert g3_manifest["lineage"]["parent_generation_id"] == "G-0002"
    assert g3_manifest["lineage"]["parent_product_manifest_sha256"] == hashlib.sha256(
        (context.resolve_product_path("generations/G-0002/product_manifest.json")).read_bytes()
    ).hexdigest()

def test_generation_delta_publish_is_thread_serialized_and_conflicts_fail_closed(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _index: _assemble(context, route), range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    assert not (child_root.parent / ".dashboard.staging").exists()
    assert (child_root.parent / ".dashboard_delta.lock").is_file()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="disagrees|does not exist"):
        _assemble(context, {"kind": "existing", "group_id": "wrong-section"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_generation_delta_publish_is_process_serialized_and_conflicts_fail_closed(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    route = {"kind": "existing", "group_id": "group-01"}
    args = (str(context.run_root), context.run_id, route)
    process_context = multiprocessing.get_context("fork")
    with process_context.Pool(2) as pool:
        results = pool.map(_process_delta_publish, (args, args))
    assert results == [("ok", "G-0002"), ("ok", "G-0002")]
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    assert (child_root / "build_receipt.json").is_file()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes

    # A conflicting publisher is rejected without touching either product
    # generation, even when run from another process.
    conflict = (str(context.run_root), context.run_id, {"kind": "existing", "group_id": "wrong-section"})
    with process_context.Pool(1) as pool:
        conflict_result = pool.map(_process_delta_publish, (conflict,))[0]
    assert conflict_result[0] == "error"
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_process_replan_race_never_publishes_stale_live_plan_lineage(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    process_context = multiprocessing.get_context("fork")
    replan = process_context.Process(
        target=_process_replan,
        args=((str(context.run_root), context.run_id, 0.35),),
    )
    delta_queue = process_context.Queue()

    def run_delta() -> None:
        try:
            result = dashboard_delta.assemble_dashboard_delta(
                RunContext(context.run_id, context.run_root),
                parent_receipt_ref="products/parent-dashboard/build_receipt.json",
                route={"kind": "existing", "group_id": "group-01"},
            )
            delta_queue.put(("ok", result["generation_id"]))
        except Exception as exc:  # pragma: no cover - asserted by caller
            delta_queue.put(("error", f"{type(exc).__name__}: {exc}"))

    delta = process_context.Process(target=run_delta)
    delta.start()
    time.sleep(0.05)
    replan.start()
    delta.join(30)
    replan.join(30)
    assert delta.exitcode == 0
    assert replan.exitcode == 0
    delta_result = delta_queue.get(timeout=5)
    assert delta_result[0] in {"ok", "error"}
    replan_plan_hash = hashlib.sha256(RunLifecycle.load(context).plan_path.read_bytes()).hexdigest()
    child_root = context.resolve_product_path("generations/G-0002/dashboard")
    if delta_result[0] == "ok":
        receipt = json.loads((child_root / "build_receipt.json").read_text(encoding="utf-8"))
        if receipt["plan_binding"]["sha256"] == replan_plan_hash:
            product = json.loads(
                context.resolve_product_path("generations/G-0002/product_manifest.json").read_text(encoding="utf-8")
            )
            assert product["lineage"]["active_plan_hash"] == replan_plan_hash
        else:
            # A replan that wins after the atomic product swap is not folded
            # into the already-published generation; exact retry must reject
            # the now-drifted active plan instead of rewriting stale output.
            with pytest.raises(dashboard_delta.DashboardDeltaError, match="conflicts|plan binding|changed"):
                _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_product_manifest_asset_and_lineage_tamper_reject_exact_retry(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})
    product_path = context.resolve_product_path("generations/G-0002/product_manifest.json")
    product = json.loads(product_path.read_text(encoding="utf-8"))
    product["assets"] = product["assets"][:-1]
    _canonical_write(product_path, product)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="asset list"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


@pytest.mark.parametrize(
    "tamper",
    (
        "old_projection",
        "new_projection",
        "affected_paths",
        "unchanged_paths",
        "rollback_parent",
        "freeze_inputs",
        "input_items",
        "outputs",
        "output_hashes",
        "parent",
        "request_binding",
        "plan_binding",
        "extra_field",
        "missing_field",
    ),
)
def test_delta_receipt_reconstructs_every_field_and_rejects_tamper(tmp_path: Path, tamper: str) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path / tamper)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})
    receipt_path = context.resolve_product_path("generations/G-0002/dashboard/build_receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if tamper in {"old_projection", "new_projection"}:
        receipt[tamper]["projection_hash"] = "0" * 64
    elif tamper in {"affected_paths", "unchanged_paths", "input_items"}:
        receipt[tamper] = []
    elif tamper == "rollback_parent":
        receipt[tamper]["generation_id"] = "G-9999"
    elif tamper == "freeze_inputs":
        receipt[tamper]["projection_hash"] = "0" * 64
    elif tamper == "outputs":
        receipt[tamper]["receipt_ref"] = "products/generations/G-0002/dashboard/alternate.json"
    elif tamper == "output_hashes":
        receipt[tamper]["fixture_sha256"] = "0" * 64
    elif tamper == "parent":
        receipt[tamper]["product_manifest_sha256"] = "0" * 64
    elif tamper == "request_binding":
        receipt[tamper]["projection_hash"] = "0" * 64
    elif tamper == "plan_binding":
        receipt[tamper]["revision"] = int(receipt[tamper]["revision"]) + 1
    elif tamper == "extra_field":
        receipt["unexpected"] = True
    elif tamper == "missing_field":
        receipt.pop("old_projection")
    _canonical_write(receipt_path, receipt)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="receipt|output hash|lineage|conflicts"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_same_generation_rebuild_accepts_older_summary_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt made before a derived summary field was exposed remains reusable."""

    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})

    receipt_path = context.resolve_product_path("generations/G-0002/dashboard/build_receipt.json")
    old_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    old_summary = dict(old_receipt["freeze_inputs"]["summary"])
    old_summary.pop("knowledge", None)
    old_receipt["freeze_inputs"]["summary"] = old_summary
    _rewrite_child_receipt_product_binding(context, receipt_path, old_receipt)
    old_receipt_bytes = receipt_path.read_bytes()

    original_projection_loader = dashboard_delta._load_delta_projection_metadata

    def projection_with_new_knowledge(run_context: RunContext, item_ids: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        summary, metadata = original_projection_loader(run_context, item_ids)
        summary = dict(summary)
        summary["knowledge"] = 101
        metadata = dict(metadata)
        metadata["summary"] = summary
        return summary, metadata

    monkeypatch.setattr(dashboard_delta, "_load_delta_projection_metadata", projection_with_new_knowledge)
    corrected = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert corrected["freeze_inputs"]["summary"]["knowledge"] == 101
    assert receipt_path.read_bytes() != old_receipt_bytes
    assert context.resolve_product_path("generations/G-0002/product_manifest.json").is_file()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes

    child_hashes = _tree_hashes(context.resolve_product_path("generations/G-0002"))
    retry = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert retry == corrected
    assert _tree_hashes(context.resolve_product_path("generations/G-0002")) == child_hashes


@pytest.mark.parametrize("tamper", ("input_item", "route", "parent_manifest"))
def test_same_generation_rebuild_rejects_tampered_stable_receipt_bindings(
    tmp_path: Path,
    tamper: str,
) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path / tamper)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})

    receipt_path = context.resolve_product_path("generations/G-0002/dashboard/build_receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if tamper == "input_item":
        receipt["input_items"][-1]["accepted_content_hash"] = "0" * 64
    elif tamper == "route":
        receipt["plan_binding"]["route"][0]["group_id"] = "tampered-group"
    else:
        receipt["parent"]["product_manifest_sha256"] = "0" * 64
    _rewrite_child_receipt_product_binding(context, receipt_path, receipt)

    with pytest.raises(dashboard_delta.DashboardDeltaError, match="binding|lineage|conflicts"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_same_generation_rebuild_rejects_self_consistent_retry_tamper(
    tmp_path: Path,
) -> None:
    """A tampered retry marker is rejected even when receipt bindings are rehashed."""

    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})
    receipt_path = context.resolve_product_path("generations/G-0002/dashboard/build_receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["retry"] = "tampered retry marker"
    _rewrite_child_receipt_product_binding(context, receipt_path, receipt)
    child_root = context.resolve_product_path("generations/G-0002")
    before = _tree_bytes(child_root)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="stable binding"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(child_root) == before
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_parent_product_manifest_drift_rejects_exact_child_retry(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})
    parent_manifest_path = context.resolve_product_path("product_manifest.json")
    parent_manifest = json.loads(parent_manifest_path.read_text(encoding="utf-8"))
    parent_manifest["limitations"] = ["parent manifest drift"]
    _canonical_write(parent_manifest_path, parent_manifest)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="conflicts|parent manifest|parent lineage"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_generation_parent_state_and_plan_hashes_bind_immediate_files(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)

    parent_state_path = context.run_root / "run_state.json"
    original_state = parent_state_path.read_bytes()
    state = json.loads(original_state.decode("utf-8"))
    state["updated_at"] = "tampered-parent-state"
    state["manifest_hash"] = dashboard_delta._state_manifest_hash(state)
    _canonical_write(parent_state_path, state)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="parent_state_hash"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    parent_state_path.write_bytes(original_state)

    parent_plan_path = context.run_root / "requirement_supervisor_plan.json"
    original_plan = parent_plan_path.read_bytes()
    plan = json.loads(original_plan.decode("utf-8"))
    plan["portfolio_strategy"] = "tampered-parent-plan"
    _canonical_write(parent_plan_path, plan)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="parent_plan_hash"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    parent_plan_path.write_bytes(original_plan)
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_staged_tree_flushes_all_files_and_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "staged"
    (root / "nested" / "deeper").mkdir(parents=True)
    (root / "index.html").write_bytes(b"index")
    (root / "nested" / "page.html").write_bytes(b"page")
    (root / "nested" / "deeper" / "asset.js").write_bytes(b"asset")
    flushed: list[int] = []
    original_fsync = dashboard_delta.os.fsync

    def spy(fd: int) -> None:
        flushed.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(dashboard_delta.os, "fsync", spy)
    dashboard_delta._fsync_tree(root)
    # Three files plus nested/deeper/nested/root directory entries are all
    # flushed, not merely the top-level staging directory.
    assert len(flushed) >= 6


def test_active_state_tamper_rejects_retry_before_parent_mutation(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})
    state_path = RunLifecycle.load(context).state_path
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "running"
    _canonical_write(state_path, state)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="active lifecycle cannot be loaded"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_product_gate_does_not_reuse_parent_terminal_marker_and_read_boundary_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))

    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def guarded_read_bytes(path: Path) -> bytes:
        if any(part.lower() in {"work", "calculations", "source", "raw", "data-room"} for part in path.parts):
            raise AssertionError(f"delta attempted forbidden read: {path}")
        return original_read_bytes(path)

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if any(part.lower() in {"work", "calculations", "source", "raw", "data-room"} for part in path.parts):
            raise AssertionError(f"delta attempted forbidden read: {path}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    with pytest.raises(dashboard_delta.DashboardDeltaError, match="freeze is incomplete"):
        _assemble(context, {"kind": "existing", "group_id": "group-01"})
    assert not context.resolve_product_path("generations/G-0002/product_manifest.json").exists()
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes

    # Freeze is checked before any child output is created.  Once telemetry is
    # frozen, the same active generation/route retries without parent drift.
    assert not context.resolve_product_path("generations/G-0002/dashboard").exists()
    _telemetry(context)
    _assemble(context, {"kind": "existing", "group_id": "group-01"})
    generation_manifest = json.loads(
        context.resolve_product_path("generations/G-0002/product_manifest.json").read_text(encoding="utf-8")
    )
    active_lifecycle = RunLifecycle.load(context)
    state_path, _state_value, state_manifest_hash, state_bytes_hash = dashboard_delta._authoritative_state_binding(context, active_lifecycle)
    assert generation_manifest["lineage"]["active_state_hash"] == state_manifest_hash
    assert generation_manifest["lineage"]["active_state_sha256"] == state_bytes_hash
    assert generation_manifest["lineage"]["active_state_hash"] != active_lifecycle.generation_metadata.state_manifest_hash
    assert generation_manifest["lineage"]["parent_generation_id"] == "G-0001"
    assert generation_manifest["new_analytics"] is False
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes


def test_chained_generation_delta_reuses_g2_as_immutable_parent(tmp_path: Path) -> None:
    context, parent_records, _parent_receipt, parent_bytes = _seed_parent(tmp_path)
    records_g2 = _append(context, parent_records, ("REQ-B",), (("REQ-A", "REQ-B"),))
    _telemetry(context)
    receipt_g2 = _assemble(context, {"kind": "existing", "group_id": "group-01"})
    g2_root = context.resolve_product_path("generations/G-0002/dashboard")
    g2_before = _tree_bytes(g2_root)
    g2_fixture = json.loads(
        context.resolve_product_path("generations/G-0002/dashboard/dashboard_fixture_v4.json").read_text(
            encoding="utf-8"
        )
    )
    g2_widget_ids = {str(widget["id"]) for widget in g2_fixture["widgets"]}
    g2_product_path = context.resolve_product_path("generations/G-0002/product_manifest.json")
    g2_product_before = g2_product_path.read_bytes()

    cumulative_g2 = parent_records + records_g2
    records_g3 = _append(
        context,
        cumulative_g2,
        ("REQ-C",),
        (("REQ-A", "REQ-B", "REQ-C"),),
        revision=3,
    )
    receipt_g3 = _assemble(
        context,
        {"kind": "existing", "group_id": "group-01"},
        parent_receipt_ref="products/generations/G-0002/dashboard/build_receipt.json",
    )
    assert receipt_g3["generation_id"] == "G-0003"
    assert receipt_g3["parent_generation_id"] == "G-0002"
    g3_fixture = json.loads(
        context.resolve_product_path("generations/G-0003/dashboard/dashboard_fixture_v4.json").read_text(
            encoding="utf-8"
        )
    )
    assert {widget["requirement_id"] for widget in g3_fixture["widgets"]} == {"REQ-A", "REQ-B", "REQ-C"}
    g3_widget_ids = {str(widget["id"]) for widget in g3_fixture["widgets"]}
    assert g2_widget_ids <= g3_widget_ids
    # The cumulative child must retain the earlier generations' visible
    # signatures, not merely their IDs in JSON.  Strip the intentionally
    # collapsed context/evidence disclosure and inspect each card's content.
    g3_pages, _g3_site_manifest = dashboard_renderer.render_dashboard_site(g3_fixture, context=context)
    g3_domain_pages = [
        value.decode("utf-8") if isinstance(value, bytes) else value
        for name, value in g3_pages.items()
        if name.startswith("domains/") and name.endswith(".html")
    ]
    if not g3_domain_pages:
        # Synthetic records with no explicit business implication belong on
        # the separate audit surface; do not manufacture manager conclusions.
        g3_audit_page = g3_pages["data-quality-audit.html"]
        g3_audit_text = g3_audit_page.decode("utf-8") if isinstance(g3_audit_page, bytes) else g3_audit_page
        # Stable fixture IDs remain assigned exactly once in the site
        # manifest; the human-facing audit heading uses the reviewed
        # integration record ID rather than the internal widget anchor.
        manifest_ids = {
            widget_id
            for group in _g3_site_manifest["requirement_groups"]
            for widget_id in group["widget_ids"]
        }
        assert manifest_ids == {str(widget["id"]) for widget in g3_fixture["widgets"]}
        for widget in g3_fixture["widgets"]:
            record_id = str(widget.get("integration_record_id") or widget["id"])
            assert record_id in g3_audit_text
    else:
        g3_domain_page = g3_domain_pages[0]
        for widget in g3_fixture["widgets"]:
            # Manager cards use a stable human-facing anchor; the original
            # widget ID remains assigned/provenanced in the fixture/audit.
            widget_anchor = dashboard_renderer._slug(widget.get("manager_anchor") or widget["id"])
            start = g3_domain_page.index(f'id="widget-{widget_anchor}"')
            end = g3_domain_page.index("</article>", start)
            article = g3_domain_page[start:end]
            visible = re.sub(r"<details\b[^>]*>.*?</details>", "", article, flags=re.DOTALL)
            visible_text = re.sub(r"<[^>]+>", "", visible)
            assert visible_text.strip(), widget["id"]
            if widget["id"] in g2_widget_ids:
                kind = str(widget.get("type") or widget.get("kind") or "kpi").lower()
                signature = "kpi-value" if kind == "kpi" else ("<table>" if kind in {"table", "status_table"} else "class=\"viz")
                assert signature in visible, widget["id"]
    assert [item["item_id"] for item in receipt_g3["input_items"]] == ["REQ-A", "REQ-B", "REQ-C"]
    assert _tree_bytes(g2_root) == g2_before
    assert g2_product_path.read_bytes() == g2_product_before
    g3_manifest_path = context.resolve_product_path("generations/G-0003/product_manifest.json")
    g3_manifest = json.loads(g3_manifest_path.read_text(encoding="utf-8"))
    assert g3_manifest["lineage"]["parent_generation_id"] == "G-0002"
    assert RunLifecycle.load(context).product_manifest_path == g3_manifest_path
    assert _tree_bytes(context.resolve_product_path("parent-dashboard")) == parent_bytes
