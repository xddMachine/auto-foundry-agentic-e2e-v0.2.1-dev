from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from auto_foundry_core.contracts import DataAssetRef, OperationSpec
from auto_foundry_core.runtime import CoreRuntime
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _context(tmp_path: Path) -> tuple[RunContext, Path, Path]:
    run = tmp_path / "run"
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "rows.json"
    source.write_text(json.dumps([{"id": "a", "value": 1}, {"id": "b", "value": 2}]), encoding="utf-8")
    return RunContext("run-1", run, (inputs,)), source, tmp_path / "sibling"


def test_context_is_immutable_and_resolves_read_and_write_boundaries(tmp_path: Path):
    context, source, sibling = _context(tmp_path)
    assert context.resolve_input(source) == source.resolve()
    inside = tmp_path / "run" / "inside.json"
    assert context.resolve_input(inside) == inside
    assert context.resolve_run_path("products/result.json") == tmp_path / "run/products/result.json"
    assert context.resolve_product_path("result.json") == tmp_path / "run/products/result.json"
    assert context.resolve_optimizer_path("evidence.json") == tmp_path / "run/optimizer/evidence.json"
    with pytest.raises((AttributeError, TypeError)):
        context.run_id = "changed"  # type: ignore[misc]
    with pytest.raises(AllowedRootError):
        context.resolve_input(sibling / "source.json")


def test_symlink_escape_rejected_before_directory_creation(tmp_path: Path):
    context, _, _ = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    context.run_root.mkdir()
    (context.run_root / "products").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AllowedRootError):
        context.resolve_product_path("result.json")
    assert not (outside / "result.json").exists()


def test_runtime_derives_cache_telemetry_and_products_from_context(tmp_path: Path):
    context, source, _ = _context(tmp_path)
    runtime = CoreRuntime(context)
    assert runtime.cache.root == context.cache_root
    assert runtime.telemetry.root == context.telemetry_root
    result = runtime.execute(OperationSpec("sources.preview", parameters={"path": str(source), "limit": 1}))
    assert result.cache_status == "miss"
    assert context.cache_root.is_dir()
    assert context.telemetry_root.is_dir()
    assert result.receipt.input_hashes
    assert result.receipt.errors == ()


def test_preview_capability_has_no_implicit_json_byte_cap(tmp_path: Path):
    context, _, _ = _context(tmp_path)
    source = context.input_roots[0] / "large.json"
    value = "x" * (16 * 1024 * 1024 + 1024)
    source.write_text(json.dumps({"value": value}), encoding="utf-8")
    runtime = CoreRuntime(context)

    default = runtime.execute(OperationSpec("sources.preview", parameters={"path": source.name, "limit": 1}))
    assert default.value["sample"] == [{"value": value}]
    with pytest.raises(ValueError, match="max_json_bytes"):
        runtime.execute(
            OperationSpec(
                "sources.preview",
                parameters={"path": source.name, "limit": 1, "max_json_bytes": 1024},
            )
        )


def test_runtime_cache_miss_then_hit_and_passive_receipt_telemetry(tmp_path: Path):
    context, source, _ = _context(tmp_path)
    runtime = CoreRuntime(context)
    spec = OperationSpec("sources.preview", parameters={"path": str(source), "limit": 1})
    first = runtime.execute(spec)
    second = runtime.execute(spec)
    assert first.cache_status == "miss"
    assert second.cache_status == "hit"
    assert first.value == second.value
    operation_events = [event for event in runtime.telemetry.events if event.event_type == "operation"]
    assert len(operation_events) == 2
    assert operation_events[-1].cache_status == "hit"
    assert runtime.telemetry.summary().cache_hits == 1
    assert runtime.telemetry.summary().cache_misses == 1


def test_runtime_preserves_typed_register_result_across_cache_hit(tmp_path: Path):
    context, source, _ = _context(tmp_path)
    runtime = CoreRuntime(context)
    spec = OperationSpec("sources.register", parameters={"path": str(source)})
    first = runtime.execute(spec)
    second = runtime.execute(spec)
    assert first.cache_status == "miss"
    assert second.cache_status == "hit"
    assert isinstance(first.value, DataAssetRef)
    assert isinstance(second.value, DataAssetRef)
    assert second.value == first.value
    assert second.value.content_hash == first.value.content_hash


def test_inline_business_uri_and_location_fields_are_not_paths(tmp_path: Path):
    context, _, _ = _context(tmp_path)
    runtime = CoreRuntime(context)
    rows = [{"location": "Berlin", "uri": "segment-a", "value": 1}]
    result = runtime.execute(OperationSpec("aggregation.compute", parameters={"rows": rows, "operation": "count"}))
    assert result.value == 1


def test_declared_source_path_slot_remains_context_validated(tmp_path: Path):
    context, source, _ = _context(tmp_path)
    runtime = CoreRuntime(context)
    valid = runtime.execute(OperationSpec("sources.preview", parameters={"path": source.name, "limit": 1}))
    assert valid.value["sample"]
    outside = tmp_path / "outside" / "rows.json"
    with pytest.raises(AllowedRootError):
        runtime.execute(OperationSpec("sources.preview", parameters={"path": str(outside)}))


def test_context_telemetry_summary_stays_inside_run(tmp_path: Path):
    context, _, _ = _context(tmp_path)
    recorder = TelemetryRecorder(context=context)
    with pytest.raises(AllowedRootError):
        recorder.write_summary(tmp_path / "outside" / "summary.json")
    assert not (tmp_path / "outside" / "summary.json").exists()
    recorder.write_summary("summaries/summary.json")
    assert (context.run_root / "summaries/summary.json").is_file()


def test_runtime_technical_path_error_is_not_masked(tmp_path: Path):
    context, _, sibling = _context(tmp_path)
    runtime = CoreRuntime(context)
    with pytest.raises(AllowedRootError) as error:
        runtime.execute(OperationSpec("sources.preview", parameters={"path": str(sibling / "outside.json")}))
    assert "escapes run context" in str(error.value)
    assert runtime.telemetry.events[-1].error
    assert getattr(error.value, "receipt").errors


def test_context_bound_artifact_and_reproduction_usage(tmp_path: Path):
    context, _, _ = _context(tmp_path)
    runtime = CoreRuntime(context)
    output = runtime.write_artifact({"ok": True}, "answer.json")
    assert Path(output.location) == context.product_root / "answer.json"
    manifest = runtime.build_manifest(
        capability_id="artifacts.write",
        operation_spec=OperationSpec("artifacts.write"),
        outputs=[output],
    )
    manifest_path = runtime.write_manifest("manifests/answer.json", manifest)
    assert (context.run_root / "manifests/answer.json").is_file()
    assert runtime.reproduce(manifest, lambda: output)["reproduced"]
    with pytest.raises(AllowedRootError):
        runtime.write_artifact({"bad": True}, "../escape.json")


def test_cli_validates_spec_before_read_or_run_directory_creation(tmp_path: Path):
    context, _, _ = _context(tmp_path)
    missing = tmp_path / "not-an-input" / "spec.json"
    environment = {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
    command = [
        sys.executable,
        "-m",
        "auto_foundry_core",
        "run",
        "aggregation.compute",
        "--spec",
        str(missing),
        "--run-root",
        str(context.run_root),
        "--input-root",
        str(context.input_roots[0]),
    ]
    result = subprocess.run(command, env={**__import__("os").environ, **environment}, capture_output=True, text=True)
    assert result.returncode != 0
    assert not context.run_root.exists()
