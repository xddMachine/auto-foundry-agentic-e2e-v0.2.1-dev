from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
import auto_foundry_core.analysis as analysis_module

from auto_foundry_core import (
    BoundAnalysisContext,
    DataAssetRef,
    ItemWorkspace,
    RunContext,
    RunLifecycle,
    load_bound_analysis_context,
)


def _fixture(tmp_path: Path) -> tuple[RunContext, ItemWorkspace, BoundAnalysisContext]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("orders.csv", "order_id,status\nO-1,closed\n")
    original = RunContext(
        "RUN-CONTEXT-REUSE",
        tmp_path / "run",
        (inputs,),
        core_version="old-core",
        skill_version="old-skill",
    )
    lifecycle = RunLifecycle.create(original, ("REQ-01",), mode="requirement")
    item = ItemWorkspace.create(
        original,
        "REQ-01",
        mode="requirement",
        original_text="Reuse this business analysis after local code changes.",
    )
    bound = BoundAnalysisContext.create_for_requirement(
        original,
        DataAssetRef.from_path(archive),
        item,
        lifecycle,
    )
    return original, item, bound


def test_saved_analysis_loads_under_different_core_and_skill_versions(tmp_path: Path) -> None:
    original, _item, bound = _fixture(tmp_path)
    current = RunContext(
        original.run_id,
        original.run_root,
        original.input_roots,
        core_version="new-core",
        skill_version="new-skill",
    )
    current_item = ItemWorkspace.load(current, "REQ-01", mode="requirement")
    counters = current.run_root / "telemetry" / "inventory_counters.json"
    counter_bytes = counters.read_bytes()
    counter_mtime = counters.stat().st_mtime_ns

    loaded = load_bound_analysis_context(current, path=bound.manifest_path, item_workspace=current_item)

    assert loaded.manifest_hash == bound.manifest_hash
    assert loaded.source_catalog.content_hash == bound.source_catalog.content_hash
    assert loaded.context.core_version == "new-core"
    assert counters.read_bytes() == counter_bytes
    assert counters.stat().st_mtime_ns == counter_mtime
    assert not (current_item.work_root / "analysis_context_repair_upgrades.jsonl").exists()
    assert not (current_item.work_root / "analysis_context_transitions.jsonl").exists()


def test_historical_catalog_lineage_does_not_require_transition_for_new_core() -> None:
    lifecycle = SimpleNamespace(item_ids=("REQ-01",))

    assert (
        analysis_module._implementation_transition_chain(
            (),
            lifecycle,
            catalog_core="core0.8.0",
            target_core="0.8.1",
            target_skill="new-skill",
            target_item_id="REQ-01",
        )
        == {}
    )


def test_saved_analysis_still_rejects_corrupted_durable_catalog(tmp_path: Path) -> None:
    original, item, bound = _fixture(tmp_path)
    catalog = bound.source_catalog.path
    value = json.loads(catalog.read_text(encoding="utf-8"))
    value["entries"] = []
    catalog.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="catalog content hash"):
        load_bound_analysis_context(original, path=bound.manifest_path, item_workspace=item)


def test_version_upgrade_and_rebind_escape_hatches_are_not_public_api() -> None:
    assert not hasattr(BoundAnalysisContext, "rebind_implementation")
    assert not hasattr(analysis_module, "upgrade_active_repair_implementation")
    assert not hasattr(analysis_module, "load_preserved_accepted_context")
    assert not hasattr(analysis_module, "load_rebindable_unaccepted_context")
