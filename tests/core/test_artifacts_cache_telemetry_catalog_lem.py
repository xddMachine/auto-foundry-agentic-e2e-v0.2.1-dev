from pathlib import Path
import json
import subprocess
import sys

import pytest

from auto_foundry_core.artifacts import build_manifest, hash_value, write_artifact, write_manifest
from auto_foundry_core.capabilities import execute
from auto_foundry_core.cache import RunCache
from auto_foundry_core.catalog import capability_catalog, get_capability, search_capabilities
from auto_foundry_core.contracts import KnowledgeDelta, OperationSpec, PreparedAssetDescriptor
from auto_foundry_core.enterprise_model import LivingEnterpriseModel
from auto_foundry_core.reproduction import compare_results, reproduce
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workspace import AllowedRootError


def test_artifacts_reproduction_cache_and_telemetry(tmp_path: Path):
    output = write_artifact([{"a": 1}, {"a": 2}], tmp_path / "out.json")
    manifest = build_manifest(capability_id="artifacts.write", operation_spec=OperationSpec("artifacts.write", parameters={"x": 1}), outputs=[output], allowed_roots=(tmp_path,))
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


def test_telemetry_storage_failures_are_passive_for_success_and_failure(tmp_path: Path):
    storage_file = tmp_path / "not-a-directory"
    storage_file.write_text("x", encoding="utf-8")
    recorder = TelemetryRecorder(storage_file, run_id="r-passive")
    with recorder.operation("profiling.profile"):
        completed = True
    assert completed is True
    try:
        with recorder.operation("profiling.profile"):
            raise RuntimeError("operation failure")
    except RuntimeError:
        pass
    else:
        raise AssertionError("operation failure was swallowed")
    summary = recorder.summary()
    assert summary.facts["telemetry_storage"] == "unavailable"
    assert summary.facts["telemetry_write_errors"] >= 1
    assert summary.facts["telemetry_dropped_events"] >= 1


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


def test_lem_delta_links_conflicts_supersession_and_scope(tmp_path: Path):
    model = LivingEnterpriseModel(run_id="r2")
    add = KnowledgeDelta("d-add", "add_ontology_item", {"item_id": "item-a", "item_type": "object", "label": "A", "scope": "shared"}, accepted=True)
    assert model.apply_delta(add)["applied"]
    extend = KnowledgeDelta("d-extend", "extend_ontology_item", {"item_id": "item-a", "properties": {"kind": "extended"}}, accepted=True)
    assert model.apply_delta(extend)["applied"]
    no_change = KnowledgeDelta("d-none", "no_change", accepted=True)
    assert model.apply_delta(no_change)["applied"]
    conflict = KnowledgeDelta(
        "d-conflict",
        "record_conflict",
        {"working_definition": "definition-a", "scope": "shared", "unresolved": True},
        evidence_refs=("evidence-a",),
        conflicts_with=("d-add",),
        accepted=True,
    )
    assert model.apply_delta(conflict)["applied"]
    model.register_prepared_asset(PreparedAssetDescriptor("prepared-a", location="prepared.json"))
    supersede = KnowledgeDelta("d-new", "supersede", {"item_ids": ("item-a", "prepared-a")}, supersedes=("d-add",), accepted=True)
    assert model.apply_delta(supersede)["applied"]
    exported = model.export()
    assert exported["knowledge"]["d-conflict"]["conflicts_with"] == ["d-add"]
    assert exported["knowledge"]["d-add"]["conflicts_with"] == ["d-conflict"]
    assert "d-conflict" in exported["conflict_links"]["d-add"]
    assert exported["conflict_state"]["d-conflict"]["unresolved"] is True
    assert exported["knowledge"]["d-conflict"]["working_definition"] == "definition-a"
    assert exported["supersession_links"]["d-new"] == ["d-add"]
    assert exported["knowledge"]["d-add"]["superseded_by"] == ["d-new"]
    assert exported["ontology"][0]["status"] == "superseded"
    assert exported["prepared_assets"][0]["status"] == "superseded"
    before_rejected = model.export()
    with __import__("pytest").raises(KeyError):
        model.apply_delta(KnowledgeDelta("d-bad", "supersede", {"item_ids": ("missing-item",)}, supersedes=("d-add",), accepted=True))
    assert model.export() == before_rejected
    with __import__("pytest").raises(ValueError):
        model.relevant_bundle(["item-a"], scope="other")
    with __import__("pytest").raises(KeyError):
        model.relevant_bundle(["missing-id"])


def test_cli_declared_allowed_root_is_enforced(tmp_path: Path):
    source = tmp_path / "records.json"
    source.write_text(json.dumps([{"id": "x", "value": 1}]), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"parameters": {"path": str(source), "allowed_roots": [str(tmp_path)]}}), encoding="utf-8")
    env = {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-m", "auto_foundry_core", "run", "sources.preview", "--spec", str(spec), "--output", str(tmp_path / "out"), "--allowed-root", str(tmp_path)], env={**__import__("os").environ, **env}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "result.json").is_file()
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    blocked = subprocess.run([sys.executable, "-m", "auto_foundry_core", "run", "sources.preview", "--spec", str(spec), "--output", str(outside), "--allowed-root", str(tmp_path)], env={**__import__("os").environ, **env}, capture_output=True, text=True)
    assert blocked.returncode != 0
    assert not outside.exists()


def test_every_path_reading_catalog_capability_rejects_escape(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("key,value\na,1\n", encoding="utf-8")
    roots = (allowed,)
    specs = [
        OperationSpec("sources.register", parameters={"path": str(outside)}, allowed_roots=roots),
        OperationSpec("sources.preview", parameters={"path": str(outside), "allowed_roots": roots}),
        OperationSpec("profiling.profile", parameters={"path": str(outside), "allowed_roots": roots}),
        OperationSpec("normalization.normalize", parameters={"path": str(outside), "allowed_roots": roots}),
        OperationSpec("identity.candidates", inputs=(str(outside), str(outside)), parameters={"compare_fields": ["key"], "allowed_roots": roots}),
        OperationSpec("relationships.measure", inputs=(str(outside), str(outside)), parameters={"left_key": "key", "allowed_roots": roots}),
        OperationSpec("aggregation.compute", inputs=(str(outside),), parameters={"operation": "count", "allowed_roots": roots}),
        OperationSpec("artifacts.write", parameters={"data": [{"ok": True}], "source_refs": [str(outside)], "allowed_roots": roots}),
        OperationSpec("artifacts.reproduce", parameters={"expected": {"location": str(outside)}, "actual": {"location": str(outside)}, "allowed_roots": roots}),
    ]
    for spec in specs:
        with __import__("pytest").raises(AllowedRootError):
            execute(spec, output_dir=str(allowed), allowed_roots=roots)
    assert not (allowed / "result.json").exists()


def test_execution_and_public_path_boundaries_require_roots(tmp_path: Path):
    source = tmp_path / "source.json"
    source.write_text("[{\"id\": 1}]", encoding="utf-8")
    with pytest.raises(AllowedRootError):
        execute(OperationSpec("sources.preview", parameters={"path": str(source)}))
    assert execute(OperationSpec("normalization.normalize", parameters={"rows": [{"label": " A "}]}))["rows"]
    output = write_artifact({"value": 1}, tmp_path / "output.json")
    with pytest.raises(AllowedRootError):
        build_manifest(capability_id="artifacts.write", operation_spec=OperationSpec("artifacts.write"), outputs=[output])
    with pytest.raises(AllowedRootError):
        write_manifest(tmp_path / "manifest.json", {"ok": True})
    with pytest.raises(AllowedRootError):
        compare_results(source, source)
    assert compare_results(source, source, allowed_roots=(tmp_path,))["equal"]
    assert compare_results([source], [source], allowed_roots=(tmp_path,))["equal"]
    assert compare_results("v1.0", "v1.0")["equal"]
    assert compare_results("example.com", "example.com")["equal"]
    assert compare_results("report.csv", "report.csv")["equal"]
    with pytest.raises(AllowedRootError):
        compare_results({"location": str(source)}, {"location": str(source)})
    manifest = build_manifest(
        capability_id="artifacts.write",
        operation_spec=OperationSpec("artifacts.write"),
        inputs=["report.csv"],
        outputs=[{"location": source}],
        allowed_roots=(tmp_path,),
    )
    assert manifest["input_hashes"] == [hash_value("report.csv")]
    assert manifest["output_hashes"]


def test_cli_requires_roots_because_it_always_writes_result(tmp_path: Path):
    spec = tmp_path / "inline.json"
    spec.write_text(json.dumps({"parameters": {"rows": [{"value": 1}], "operation": "count"}}), encoding="utf-8")
    output = tmp_path / "out"
    env = {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1", **__import__("os").environ}
    result = subprocess.run([sys.executable, "-m", "auto_foundry_core", "run", "aggregation.compute", "--spec", str(spec), "--output", str(output)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()


def test_cli_spec_and_embedded_roots_cannot_escape_out_of_band_root(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_spec = outside / "spec.json"
    outside_spec.write_text(json.dumps({"parameters": {"rows": [{"value": 1}], "operation": "count"}}), encoding="utf-8")
    env = {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1", **__import__("os").environ}
    blocked_spec = subprocess.run([sys.executable, "-m", "auto_foundry_core", "run", "aggregation.compute", "--spec", str(outside_spec), "--output", str(allowed / "out"), "--allowed-root", str(allowed)], env=env, capture_output=True, text=True)
    assert blocked_spec.returncode != 0
    assert not (allowed / "out").exists()

    spec = allowed / "spec.json"
    spec.write_text(json.dumps({"allowed_roots": [str(outside)], "parameters": {"rows": [{"value": 1}], "operation": "count"}}), encoding="utf-8")
    blocked_embedded = subprocess.run([sys.executable, "-m", "auto_foundry_core", "run", "aggregation.compute", "--spec", str(spec), "--output", str(allowed / "out"), "--allowed-root", str(allowed)], env=env, capture_output=True, text=True)
    assert blocked_embedded.returncode != 0
    assert not (allowed / "out").exists()
