from pathlib import Path
import json
import subprocess
import sys

from auto_foundry_core.artifacts import build_manifest, hash_value, write_artifact
from auto_foundry_core.cache import RunCache
from auto_foundry_core.catalog import capability_catalog, get_capability, search_capabilities
from auto_foundry_core.contracts import KnowledgeDelta, OperationSpec, PreparedAssetDescriptor
from auto_foundry_core.enterprise_model import LivingEnterpriseModel
from auto_foundry_core.reproduction import compare_results, reproduce
from auto_foundry_core.telemetry import TelemetryRecorder


def test_artifacts_reproduction_cache_and_telemetry(tmp_path: Path):
    output = write_artifact([{"a": 1}, {"a": 2}], tmp_path / "out.json")
    manifest = build_manifest(capability_id="artifacts.write", operation_spec=OperationSpec("artifacts.write", parameters={"x": 1}), outputs=[output])
    assert output.content_hash and manifest["output_hashes"] == [output.content_hash]
    assert compare_results({"a": 1}, {"a": 1})["equal"]
    assert reproduce({"output_hashes": [hash_value([{"a": 1}])]}, lambda: [{"a": 1}])["reproduced"]
    recorder = TelemetryRecorder(tmp_path / "telemetry", run_id="r1")
    cache = RunCache(tmp_path / "cache", telemetry=recorder)
    spec = OperationSpec("profiling.profile", parameters={"x": 1})
    first = cache.get_or_compute(spec, ["source-a"], lambda: {"rows": 2})
    second = cache.get_or_compute(spec, ["source-a"], lambda: {"rows": 3})
    assert first.value == second.value == {"rows": 2}
    assert recorder.summary().cache_hits == 1
    assert recorder.summary().cache_misses == 1


def test_cache_invalidation_and_judgement_rejection(tmp_path: Path):
    cache = RunCache(tmp_path / "cache", core_version="0.1.0")
    spec = OperationSpec("profiling.profile", parameters={"field": "a"})
    cache.put(spec, ["source-1"], {"value": 1})
    assert cache.get(spec, ["source-1"]).value == {"value": 1}
    assert cache.get(spec, ["source-2"]) is None
    assert cache.get(OperationSpec("profiling.profile", parameters={"field": "b"}), ["source-1"]) is None
    other_version = RunCache(tmp_path / "cache", core_version="0.1.1")
    assert other_version.get(spec, ["source-1"]) is None
    try:
        cache.put(spec, ["source-1"], {"decision": "same_object"}, kind="agent_judgement")
    except ValueError:
        pass
    else:
        raise AssertionError("agent judgement was cached")


def test_generated_catalog_and_living_model_atomic_delta():
    ids = {descriptor.capability_id for descriptor in capability_catalog()}
    assert "relationships.measure" in ids
    assert get_capability("aggregation.compute").backend == "python-local"
    assert any(item.capability_id == "relationships.measure" for item in search_capabilities("join coverage"))
    assert all(item.examples and item.limitations for item in capability_catalog())
    model = LivingEnterpriseModel(run_id="r1")
    accepted = KnowledgeDelta("delta-1", "add_ontology_item", {"item_id": "item-1", "item_type": "object", "label": "Object", "metadata": {"reviewer": "hidden", "kept": True}}, accepted=True)
    assert model.apply_delta(accepted)["applied"]
    assert "reviewer" not in model.ontology["item-1"].metadata
    asset = PreparedAssetDescriptor("prepared-1", location="derived.json", scope="requirement")
    model.register_prepared_asset(asset)
    bundle = model.relevant_bundle(["item-1"], prepared_asset_ids=["prepared-1"])
    assert bundle["prepared_assets"][0]["scope"] == "requirement"


def test_cli_declared_allowed_root_is_enforced(tmp_path: Path):
    source = tmp_path / "records.json"
    source.write_text(json.dumps([{"id": "x", "value": 1}]), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"parameters": {"path": str(source), "allowed_roots": [str(tmp_path)]}}), encoding="utf-8")
    env = {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-m", "auto_foundry_core", "run", "sources.preview", "--spec", str(spec), "--output", str(tmp_path / "out")], env={**__import__("os").environ, **env}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "result.json").is_file()
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    blocked = subprocess.run([sys.executable, "-m", "auto_foundry_core", "run", "sources.preview", "--spec", str(spec), "--output", str(outside)], env={**__import__("os").environ, **env}, capture_output=True, text=True)
    assert blocked.returncode != 0
    assert not outside.exists()
