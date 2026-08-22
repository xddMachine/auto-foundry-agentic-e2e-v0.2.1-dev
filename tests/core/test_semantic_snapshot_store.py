from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import shutil
import zipfile

import pytest

import auto_foundry_core.analysis as analysis_module
from auto_foundry_core import AnalystWorkspace, BoundAnalysisContext, DataAssetRef, ItemWorkspace
from auto_foundry_core.semantic_store import SemanticSnapshotStore
from auto_foundry_core.workspace import RunContext
from auto_foundry_core.lifecycle import RunLifecycle


def _snapshot(count: int) -> dict[str, object]:
    ontology = [
        {"item_id": f"object-{index:05d}", "item_type": "entity", "label": f"Object {index}"}
        for index in range(count)
    ]
    return {
        "schema_version": "auto_foundry.semantic_reuse_snapshot.v1",
        "projection_hash": "a" * 64,
        "item_order": ["REQ-01"],
        "source_item_ids": ["REQ-01"],
        "source_resolution_domain_ids": [],
        "source_resolution_bindings": [],
        "ontology": ontology,
        "relationships": {},
        "identity_decisions": [],
        "canonical_mappings": [],
        "prepared_assets": [],
        "counts": {
            "ontology": count,
            "relationships": 0,
            "identity_decisions": 0,
            "canonical_mappings": 0,
            "prepared_assets": 0,
        },
    }


def test_content_addressed_snapshot_reuses_one_object_and_selection_is_compact(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE", tmp_path / "run")
    snapshot = _snapshot(20_000)
    ref = SemanticSnapshotStore.publish(context, snapshot)
    retry = SemanticSnapshotStore.publish(context, snapshot)

    assert retry == ref
    snapshot_root = context.run_root / "semantic_store" / "snapshots" / ref.snapshot_hash
    assert len(list((context.run_root / "semantic_store" / "snapshots").iterdir())) == 1
    assert (snapshot_root / "manifest.json").is_file()
    # Contexts persist this reference, not the records themselves.
    assert len(json.dumps(ref.to_dict(), separators=(",", ":"))) < 1024

    selection = SemanticSnapshotStore.publish_selection(
        context,
        ref,
        {"ontology_ids": [f"object-{index:05d}" for index in range(20_000)]},
    )
    selection_value = json.loads(context.resolve_run_path(selection.selection_ref).read_text(encoding="utf-8"))
    assert "selected_ids" not in selection_value
    assert selection_value["counts"]["ontology_ids"] == 20_000
    assert len(SemanticSnapshotStore.load_selection(
        context, ref, selection.selection_ref, selection.selection_hash
    )["ontology_ids"]) == 20_000


def test_incremental_snapshot_reuses_unchanged_layer_and_index_blobs(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-BLOB-DEDUP", tmp_path / "run")
    base = _snapshot(2)
    changed = copy.deepcopy(base)
    changed["ontology"].append({"item_id": "object-00002", "item_type": "entity", "label": "Object 2"})
    changed["counts"] = {**base["counts"], "ontology": 3}

    first = SemanticSnapshotStore.publish(context, base)
    first_manifest = SemanticSnapshotStore.manifest(context, first)
    first_files = {
        path.relative_to(context.run_root): path.stat().st_size
        for path in context.run_root.rglob("*")
        if path.is_file() and path.name != ".publish.lock"
    }
    first_blob_bytes = {
        path.name: path.read_bytes()
        for path in (context.run_root / "semantic_store" / "blobs").iterdir()
        if path.is_file()
    }
    second = SemanticSnapshotStore.publish(context, changed)
    second_manifest = SemanticSnapshotStore.manifest(context, second)
    second_files = {
        path.relative_to(context.run_root): path.stat().st_size
        for path in context.run_root.rglob("*")
        if path.is_file() and path.name != ".publish.lock"
    }

    assert first.snapshot_hash != second.snapshot_hash
    for layer in ("relationships", "identity_decisions", "canonical_mappings", "prepared_assets"):
        assert first_manifest["layers"][layer]["blob_ref"] == second_manifest["layers"][layer]["blob_ref"]
        assert first_manifest["layers"][layer]["index_ref"] == second_manifest["layers"][layer]["index_ref"]
    assert first_manifest["layers"]["ontology"]["blob_ref"] != second_manifest["layers"]["ontology"]["blob_ref"]
    assert first_manifest["layers"]["ontology"]["index_ref"] != second_manifest["layers"]["ontology"]["index_ref"]

    changed_ontology_paths = {
        Path(second_manifest["layers"]["ontology"][key])
        for key in ("blob_ref", "index_ref")
    }
    new_files = set(second_files) - set(first_files)
    expected_new_files = changed_ontology_paths | {Path(second.manifest_ref)}
    assert new_files == expected_new_files
    expected_delta = second_files[Path(second.manifest_ref)] + sum(
        second_files[path] for path in changed_ontology_paths
    )
    assert sum(second_files[path] for path in new_files) == expected_delta

    snapshot_root = context.run_root / "semantic_store" / "snapshots"
    assert {path.name for path in (snapshot_root / first.snapshot_hash).iterdir()} == {"manifest.json"}
    assert {path.name for path in (snapshot_root / second.snapshot_hash).iterdir()} == {"manifest.json"}
    blobs_root = context.run_root / "semantic_store" / "blobs"
    blob_bytes = {path.name: path.read_bytes() for path in blobs_root.iterdir() if path.is_file()}
    assert first_blob_bytes.items() <= blob_bytes.items()
    changed_refs = {
        Path(second_manifest["layers"][layer][key]).name
        for layer in ("ontology", "relationships", "identity_decisions", "canonical_mappings", "prepared_assets")
        for key in ("blob_ref", "index_ref")
    }
    base_refs = {
        Path(first_manifest["layers"][layer][key]).name
        for layer in ("ontology", "relationships", "identity_decisions", "canonical_mappings", "prepared_assets")
        for key in ("blob_ref", "index_ref")
    }
    assert set(blob_bytes) - set(first_blob_bytes) == changed_refs - base_refs
    for layer in ("relationships", "identity_decisions", "canonical_mappings", "prepared_assets"):
        for key in ("blob_ref", "index_ref"):
            blob_path = context.resolve_run_path(second_manifest["layers"][layer][key])
            assert sum(1 for path in second_files if path == blob_path.relative_to(context.run_root)) == 1
    assert SemanticSnapshotStore.records(context, first, "ontology")["ontology"][-1]["item_id"] == "object-00001"
    assert len(SemanticSnapshotStore.records(context, second, "ontology")["ontology"]) == 3


def test_manifest_and_ref_validation_do_not_open_layer_payloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN-STORE-MANIFEST", tmp_path / "run")
    ref = SemanticSnapshotStore.publish(context, _snapshot(2))

    def fail_if_layer_opened(*args: object, **kwargs: object) -> tuple[object, ...]:
        raise AssertionError("semantic layer opened during manifest validation")

    monkeypatch.setattr(SemanticSnapshotStore, "_read_layer", fail_if_layer_opened)
    assert SemanticSnapshotStore.read_ref(context, ref) == ref
    assert SemanticSnapshotStore.manifest(context, ref)["counts"]["ontology"] == 2


def test_exact_publish_retry_validates_manifest_without_opening_blobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RunContext("RUN-STORE-RETRY-MANIFEST", tmp_path / "run")
    snapshot = _snapshot(2)
    ref = SemanticSnapshotStore.publish(context, snapshot)

    def fail_if_blob_opened(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("semantic blob opened during exact publish retry")

    monkeypatch.setattr(SemanticSnapshotStore, "_verify_blob_bytes", fail_if_blob_opened)
    assert SemanticSnapshotStore.publish(context, snapshot) == ref


def test_layer_and_index_tampering_fails_when_that_layer_is_used(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-TAMPER", tmp_path / "run")
    ref = SemanticSnapshotStore.publish(context, _snapshot(2))
    manifest = SemanticSnapshotStore.manifest(context, ref)
    layer = context.resolve_run_path(manifest["layers"]["ontology"]["blob_ref"])
    original = layer.read_bytes()
    layer.write_bytes(original + b"tampered")
    with pytest.raises(ValueError, match="ontology hash"):
        SemanticSnapshotStore.records(context, ref, "ontology")

    # Restore the layer and tamper its ID index; exact selection validation
    # must fail closed without trusting the stale index bytes.
    layer.write_bytes(original)
    index = context.resolve_run_path(manifest["layers"]["ontology"]["index_ref"])
    index.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="ontology index"):
        SemanticSnapshotStore.validate_ids(context, ref, "ontology", ("object-00000",))


def test_duplicate_detection_uses_set_membership_for_large_layers(tmp_path: Path) -> None:
    class CountingID(str):
        comparisons = 0

        def __eq__(self, other: object) -> bool:
            type(self).comparisons += 1
            return super().__eq__(other)

        def __hash__(self) -> int:
            return hash(str(self))

    context = RunContext("RUN-STORE-DUPLICATES", tmp_path / "run")
    size = 4_000
    snapshot = _snapshot(0)
    snapshot["ontology"] = [
        {"item_id": CountingID(f"object-{index:05d}"), "item_type": "entity"}
        for index in range(size)
    ]
    snapshot["counts"] = {"ontology": size, "relationships": 0, "identity_decisions": 0, "canonical_mappings": 0, "prepared_assets": 0}
    SemanticSnapshotStore.publish(context, snapshot)
    # A list-based duplicate scan would perform quadratic equality checks;
    # unique hashes keep the set-based implementation near-linear.
    assert CountingID.comparisons < size * 4


def test_selection_publication_requires_exact_canonical_bytes(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-SELECTION-BYTES", tmp_path / "run")
    ref = SemanticSnapshotStore.publish(context, _snapshot(2))
    selection = SemanticSnapshotStore.publish_selection(
        context,
        ref,
        {"ontology_ids": ("object-00000",)},
    )
    path = context.resolve_run_path(selection.selection_ref)
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="canonical"):
        SemanticSnapshotStore.load_selection(context, ref, selection.selection_ref, selection.selection_hash)
    with pytest.raises(ValueError, match="canonical"):
        SemanticSnapshotStore.publish_selection(context, ref, {"ontology_ids": ("object-00000",)})


def test_empty_selection_still_requires_a_valid_snapshot_manifest(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-EMPTY-SELECTION", tmp_path / "run")
    ref = SemanticSnapshotStore.publish(context, _snapshot(2))
    selection = SemanticSnapshotStore.publish_selection(context, ref, {})
    manifest = context.resolve_run_path(ref.manifest_ref)
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="manifest bytes"):
        SemanticSnapshotStore.load_selection(context, ref, selection.selection_ref, selection.selection_hash)

    context_missing = RunContext("RUN-STORE-EMPTY-SELECTION-MISSING", tmp_path / "run-missing")
    missing_ref = SemanticSnapshotStore.publish(context_missing, _snapshot(2))
    missing_selection = SemanticSnapshotStore.publish_selection(context_missing, missing_ref, {})
    context_missing.resolve_run_path(missing_ref.manifest_ref).unlink()
    with pytest.raises(ValueError, match="regular file"):
        SemanticSnapshotStore.load_selection(
            context_missing,
            missing_ref,
            missing_selection.selection_ref,
            missing_selection.selection_hash,
        )


def test_zero_record_snapshot_with_provenance_keeps_a_truthful_ref(tmp_path: Path) -> None:
    from auto_foundry_core.analysis import _publish_semantic_snapshot

    context = RunContext("RUN-STORE-PROVENANCE-EMPTY", tmp_path / "run")
    empty = {
        "schema_version": "auto_foundry.semantic_reuse_snapshot.v1",
        "projection_hash": "a" * 64,
        "item_order": ["REQ-01"],
        "source_item_ids": ["REQ-01"],
        "source_resolution_domain_ids": ["customer-domain"],
        "source_resolution_bindings": [{"domain_id": "customer-domain", "status": "committed"}],
        "ontology": [],
        "relationships": {},
        "identity_decisions": [],
        "canonical_mappings": [],
        "prepared_assets": [],
        "counts": {"ontology": 0, "relationships": 0, "identity_decisions": 0, "canonical_mappings": 0, "prepared_assets": 0},
    }
    ref = _publish_semantic_snapshot(context, empty)
    assert ref is not None
    manifest = SemanticSnapshotStore.manifest(context, ref)
    assert manifest["counts"]["ontology"] == 0
    assert manifest["projection"]["source_resolution_domain_ids"] == ["customer-domain"]
    assert _publish_semantic_snapshot(
        context,
        {"counts": {"ontology": 0, "relationships": 0, "identity_decisions": 0, "canonical_mappings": 0, "prepared_assets": 0}},
    ) is None


def test_concurrent_snapshot_publishers_leave_one_complete_object(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-CONCURRENT", tmp_path / "run")
    snapshot = _snapshot(200)

    def publish() -> object:
        return SemanticSnapshotStore.publish(context, snapshot)

    with ThreadPoolExecutor(max_workers=6) as executor:
        refs = list(executor.map(lambda _: publish(), range(12)))
    assert all(ref == refs[0] for ref in refs)
    snapshots_root = context.run_root / "semantic_store" / "snapshots"
    assert [path.name for path in snapshots_root.iterdir() if not path.name.startswith(".")] == [refs[0].snapshot_hash]
    assert not any(path.name.startswith(".snapshot-") for path in snapshots_root.iterdir())
    assert (snapshots_root / refs[0].snapshot_hash / "manifest.json").is_file()


def test_concurrent_publishers_converge_on_two_deduplicated_snapshots(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-CONCURRENT-DEDUP", tmp_path / "run")
    base = _snapshot(200)
    changed = copy.deepcopy(base)
    changed["ontology"].append({"item_id": "object-00200", "item_type": "entity", "label": "Object 200"})
    changed["counts"] = {**base["counts"], "ontology": 201}

    def publish(index: int) -> object:
        return SemanticSnapshotStore.publish(context, base if index % 2 == 0 else changed)

    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(publish, range(16)))
    assert len({ref.snapshot_hash for ref in refs}) == 2
    snapshots_root = context.run_root / "semantic_store" / "snapshots"
    assert not any(path.name.startswith(".snapshot-") for path in snapshots_root.iterdir())
    for snapshot_root in snapshots_root.iterdir():
        if snapshot_root.name.startswith("."):
            continue
        assert {path.name for path in snapshot_root.iterdir()} == {"manifest.json"}
    blobs_root = context.run_root / "semantic_store" / "blobs"
    assert not any(path.name.startswith(".blob-") for path in blobs_root.iterdir())
    for ref in refs:
        assert SemanticSnapshotStore.manifest(context, ref)["schema_version"].endswith(".v3")


def test_manifest_tamper_fails_before_any_layer_access(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-MANIFEST-TAMPER", tmp_path / "run")
    ref = SemanticSnapshotStore.publish(context, _snapshot(2))
    manifest = context.resolve_run_path(ref.manifest_ref)
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="manifest bytes"):
        SemanticSnapshotStore.read_ref(context, ref)


def test_symlinked_context_payload_target_is_rejected_without_mutation(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-PAYLOAD-SYMLINK", tmp_path / "run")
    ref = SemanticSnapshotStore.publish_context_payload(context, {"value": "stable"})
    target = context.resolve_run_path(ref.payload_ref)
    backup = target.with_name("payload-original.json")
    original = target.read_bytes()
    target.rename(backup)
    outside = tmp_path / "outside-payload.json"
    outside.write_bytes(b"outside")
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink|escapes run root"):
        SemanticSnapshotStore.publish_context_payload(context, {"value": "stable"})
    with pytest.raises(ValueError, match="symlink|escapes run root"):
        SemanticSnapshotStore.read_context_payload_ref(context, ref)
    assert target.is_symlink()
    assert backup.read_bytes() == original
    assert outside.read_bytes() == b"outside"
    payload_root = target.parent
    assert not any(path.name.startswith(".context-payload-") for path in payload_root.iterdir())


def test_symlinked_selection_target_is_rejected_on_publish_and_load(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-SELECTION-SYMLINK", tmp_path / "run")
    ref = SemanticSnapshotStore.publish(context, _snapshot(2))
    selection = SemanticSnapshotStore.publish_selection(context, ref, {"ontology_ids": ("object-00000",)})
    target = context.resolve_run_path(selection.selection_ref)
    backup = target.with_name("selection-original.json")
    original = target.read_bytes()
    target.rename(backup)
    outside = tmp_path / "outside-selection.json"
    outside.write_bytes(b"outside")
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink|escapes run root"):
        SemanticSnapshotStore.publish_selection(context, ref, {"ontology_ids": ("object-00000",)})
    with pytest.raises(ValueError, match="symlink|escapes run root"):
        SemanticSnapshotStore.load_selection(context, ref, selection.selection_ref, selection.selection_hash)
    assert target.is_symlink()
    assert backup.read_bytes() == original
    assert outside.read_bytes() == b"outside"
    selection_root = target.parent
    assert not any(path.name.startswith(".selection-") for path in selection_root.iterdir())


def test_symlinked_snapshot_target_is_rejected_without_replacing_object(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-SNAPSHOT-SYMLINK", tmp_path / "run")
    snapshot = _snapshot(2)
    ref = SemanticSnapshotStore.publish(context, snapshot)
    target = context.resolve_run_path(f"semantic_store/snapshots/{ref.snapshot_hash}")
    backup = target.with_name("snapshot-original")
    original_manifest = (target / "manifest.json").read_bytes()
    target.rename(backup)
    outside = tmp_path / "outside-snapshot"
    outside.mkdir()
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|escapes run root"):
        SemanticSnapshotStore.publish(context, snapshot)
    with pytest.raises(ValueError, match="symlink|escapes run root"):
        SemanticSnapshotStore.read_ref(context, ref)
    assert target.is_symlink()
    assert (backup / "manifest.json").read_bytes() == original_manifest
    assert not any(path.name.startswith(".snapshot-") for path in target.parent.iterdir())
    shutil.rmtree(backup)


def test_symlinked_blob_target_is_rejected_without_mutating_valid_snapshot(tmp_path: Path) -> None:
    context = RunContext("RUN-STORE-BLOB-SYMLINK", tmp_path / "run")
    snapshot = _snapshot(2)
    ref = SemanticSnapshotStore.publish(context, snapshot)
    manifest = SemanticSnapshotStore.manifest(context, ref)
    target = context.resolve_run_path(manifest["layers"]["ontology"]["blob_ref"])
    backup = target.with_name("ontology-original.json")
    original = target.read_bytes()
    target.rename(backup)
    outside = tmp_path / "outside-blob.json"
    outside.write_bytes(b"outside")
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink|escapes run root"):
        SemanticSnapshotStore.records(context, ref, "ontology")
    # Exact retries validate only the canonical manifest; blob integrity is
    # enforced when the requested layer is actually used.
    assert SemanticSnapshotStore.publish(context, snapshot) == ref
    assert target.is_symlink()
    assert backup.read_bytes() == original
    assert outside.read_bytes() == b"outside"
    assert not any(path.name.startswith(".blob-") for path in target.parent.iterdir())

    target.unlink()
    backup.rename(target)
    assert SemanticSnapshotStore.records(context, ref, "ontology")["ontology"]


def test_two_large_contexts_share_snapshot_and_brief_is_layer_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("orders.csv", "order_id,region\nA-1,DE\n")
    context = RunContext("RUN-STORE-TWO-CONTEXTS", tmp_path / "run", (inputs,))
    lifecycle = RunLifecycle.create(context, ("REQ-01", "REQ-02"), mode="requirement")
    first = ItemWorkspace.create(context, "REQ-01", mode="requirement", original_text="First")
    second = ItemWorkspace.create(context, "REQ-02", mode="requirement", original_text="Second")
    synthetic = _snapshot(20_000)
    monkeypatch.setattr(analysis_module, "_semantic_reuse_snapshot", lambda *args, **kwargs: synthetic)

    first_bound = BoundAnalysisContext.create_for_requirement(context, DataAssetRef.from_path(archive), first, lifecycle)
    second_bound = BoundAnalysisContext.create_for_requirement(context, DataAssetRef.from_path(archive), second, lifecycle)
    assert first_bound.semantic_snapshot_ref == second_bound.semantic_snapshot_ref
    for item in (first, second):
        manifest_path = item.work_root / "analysis_context.json"
        raw = manifest_path.read_bytes()
        assert len(raw) < 128 * 1024
        manifest = json.loads(raw)
        assert "ontology_bundle" not in manifest
        assert "object-" not in raw.decode("utf-8")
        assert manifest["semantic_snapshot"] == first_bound.semantic_snapshot_ref.to_dict()
    snapshot_root = context.run_root / "semantic_store" / "snapshots"
    assert len([path for path in snapshot_root.iterdir() if not path.name.startswith(".")]) == 1

    first_loaded = BoundAnalysisContext.load(context, path=first_bound.manifest_path)
    second_loaded = BoundAnalysisContext.load(context, path=second_bound.manifest_path)
    monkeypatch.setattr(SemanticSnapshotStore, "_read_layer", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("layer opened")))
    first_brief = AnalystWorkspace(first_loaded, owner_ref="owner-REQ-01").brief()
    second_brief = AnalystWorkspace(second_loaded, owner_ref="owner-REQ-02").brief()
    assert first_brief.ontology_item_count == 20_000
    assert second_brief.ontology_item_count == 20_000
