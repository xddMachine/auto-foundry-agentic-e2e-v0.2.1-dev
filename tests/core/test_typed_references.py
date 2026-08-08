from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from auto_foundry_core import (
    CoreRuntime,
    DataAssetRef,
    OperationResultRef,
    OperationSpec,
    RunContext,
    decode_explicit_reference,
    is_explicit_reference_mapping,
)
from auto_foundry_core.artifacts import build_manifest, hash_value
from auto_foundry_core.reproduction import compare_results
from auto_foundry_core.workspace import AllowedRootError


def _context(tmp_path: Path) -> tuple[RunContext, Path, Path]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "orders.csv"
    source.write_text("id,amount\na,1\n", encoding="utf-8")
    run = tmp_path / "run"
    return RunContext("typed-ref-run", run, (inputs,)), source, tmp_path / "outside.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_contracts_are_explicit_and_round_trip_nested() -> None:
    asset = DataAssetRef(uri="inputs/orders.csv", format="csv")
    result = OperationResultRef(location="products/result.json", format="json")

    encoded_asset = asset.to_dict()
    encoded_result = result.to_dict()
    assert encoded_asset["__auto_foundry_ref__"] == "data_asset"
    assert encoded_result["__auto_foundry_ref__"] == "operation_result"
    assert DataAssetRef.from_json(asset.to_json()) == asset
    assert OperationResultRef.from_json(result.to_json()) == result
    assert decode_explicit_reference(encoded_asset) == asset
    assert decode_explicit_reference(encoded_result) == result

    nested = OperationSpec(
        "artifacts.reproduce",
        parameters={"expected": {"rows": [encoded_asset]}, "actual": {"rows": [encoded_result]}},
    )
    wire = json.loads(nested.to_json())
    assert wire["parameters"]["expected"]["rows"][0]["__auto_foundry_ref__"] == "data_asset"
    assert wire["parameters"]["actual"]["rows"][0]["__auto_foundry_ref__"] == "operation_result"


def test_ordinary_mappings_are_data_in_runtime_and_direct_paths(tmp_path: Path) -> None:
    context, _, _ = _context(tmp_path)
    runtime = CoreRuntime(context)
    values = [
        {"location": "Berlin", "value": 1},
        {"uri": "segment-a", "count": 12},
        {"content_hash": "business-label", "amount": 4},
        {"location": "Berlin"},
        {"uri": "customer-facing-value"},
        {"rows": [{"location": "Berlin", "uri": "segment-a"}]},
        {"nested": [{"location": "Berlin"}, {"uri": "segment-a"}]},
    ]

    for value in values:
        assert runtime.compare_results(value, value)["equal"]
        assert compare_results(value, value)["equal"]
        execution = runtime.execute(
            OperationSpec("artifacts.reproduce", parameters={"expected": value, "actual": value})
        )
        assert execution.value["equal"]
        manifest = runtime.build_manifest(
            capability_id="artifacts.reproduce",
            operation_spec=OperationSpec("artifacts.reproduce"),
            inputs=[value],
            outputs=[value],
        )
        assert manifest["input_hashes"] == [hash_value(value)]
        assert manifest["output_hashes"] == [hash_value(value)]
        assert runtime.reproduce(manifest, lambda value=value: value)["reproduced"]
        direct = build_manifest(
            capability_id="artifacts.reproduce",
            operation_spec=OperationSpec("artifacts.reproduce"),
            inputs=[value],
            outputs=[value],
        )
        assert direct["input_hashes"] == [hash_value(value)]
        assert direct["output_hashes"] == [hash_value(value)]

    plain_string = runtime.execute(
        OperationSpec("artifacts.reproduce", parameters={"expected": "report.csv", "actual": "report.csv"})
    )
    assert plain_string.value["equal"]


def test_explicit_refs_resolve_hash_and_respect_input_and_run_roots(tmp_path: Path) -> None:
    context, source, outside = _context(tmp_path)
    runtime = CoreRuntime(context)
    asset = DataAssetRef(uri="orders.csv", format="csv", content_hash=_digest(source))
    tagged_asset = asset.to_dict()

    output = runtime.write_artifact({"ok": True}, "result.json")
    tagged_output = output.to_dict()
    assert runtime.compare_results(asset, tagged_asset)["equal"]
    assert runtime.compare_results(output, tagged_output)["equal"]
    tagged_execution = runtime.execute(
        OperationSpec("artifacts.reproduce", parameters={"expected": tagged_output, "actual": tagged_output})
    )
    assert tagged_execution.value["equal"]
    manifest = runtime.build_manifest(
        capability_id="artifacts.write",
        operation_spec=OperationSpec("artifacts.write"),
        inputs=[tagged_asset],
        outputs=[tagged_output],
    )
    assert manifest["input_hashes"] == [_digest(source)]
    assert manifest["output_hashes"] == [output.content_hash]
    assert manifest["input_refs"][0]["__auto_foundry_ref__"] == "data_asset"
    assert manifest["output_refs"][0]["__auto_foundry_ref__"] == "operation_result"
    assert runtime.reproduce(manifest, lambda: tagged_output)["reproduced"]

    outside.write_text("outside\n", encoding="utf-8")
    escaped_asset = DataAssetRef(uri="../outside.json")
    escaped_result = OperationResultRef(location="../outside.json")
    with pytest.raises(AllowedRootError):
        runtime.compare_results(escaped_asset, escaped_asset)
    with pytest.raises(AllowedRootError):
        runtime.compare_results(escaped_result, escaped_result)
    with pytest.raises(AllowedRootError):
        runtime.build_manifest(
            capability_id="artifacts.write",
            operation_spec=OperationSpec("artifacts.write"),
            inputs=[escaped_asset.to_dict()],
        )
    with pytest.raises(AllowedRootError):
        runtime.build_manifest(
            capability_id="artifacts.write",
            operation_spec=OperationSpec("artifacts.write"),
            outputs=[escaped_result.to_dict()],
        )


def test_malformed_or_unknown_reference_tags_fail_clearly() -> None:
    unknown = {"__auto_foundry_ref__": "mystery", "uri": "orders.csv"}
    missing_path = {"__auto_foundry_ref__": "data_asset", "format": "csv"}
    invalid_hash = {
        "__auto_foundry_ref__": "operation_result",
        "location": "result.json",
        "content_hash": "not-a-sha256",
    }
    extra_field = {
        "__auto_foundry_ref__": "data_asset",
        "uri": "orders.csv",
        "unexpected": True,
    }
    for value in (unknown, missing_path, invalid_hash, extra_field):
        with pytest.raises((TypeError, ValueError)):
            decode_explicit_reference(value)
    assert not is_explicit_reference_mapping({"uri": "orders.csv"})


def test_typed_source_and_result_cache_receipt_round_trip(tmp_path: Path) -> None:
    context, source, _ = _context(tmp_path)
    runtime = CoreRuntime(context)

    source_spec = OperationSpec("sources.register", parameters={"path": source.name})
    source_miss = runtime.execute(source_spec)
    source_hit = runtime.execute(source_spec)
    assert source_miss.cache_status == "miss"
    assert source_hit.cache_status == "hit"
    assert isinstance(source_miss.value, DataAssetRef)
    assert isinstance(source_hit.value, DataAssetRef)
    assert source_hit.value == source_miss.value
    assert source_hit.receipt.output_hashes == source_miss.receipt.output_hashes
    assert source_hit.receipt.to_dict()["output"] is None

    result_spec = OperationSpec("artifacts.write", parameters={"filename": "cached.json", "data": {"ok": True}})
    result_miss = runtime.execute(result_spec)
    result_hit = runtime.execute(result_spec)
    assert result_miss.cache_status == "miss"
    assert result_hit.cache_status == "hit"
    assert isinstance(result_miss.value, OperationResultRef)
    assert isinstance(result_hit.value, OperationResultRef)
    assert result_hit.value == result_miss.value
    assert result_hit.receipt.output == result_miss.receipt.output
    assert result_hit.receipt.to_dict()["output"]["__auto_foundry_ref__"] == "operation_result"
    json.dumps(result_hit.receipt.to_dict())
