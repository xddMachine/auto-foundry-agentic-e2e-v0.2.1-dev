from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from auto_foundry_core.contracts import DataAssetRef
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workbench import CatalogCounts, DataRoom, DataRoomWorkbench
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _xlsx_bytes() -> bytes | None:
    try:
        import openpyxl
    except ImportError:
        return None
    workbook = openpyxl.Workbook()
    workbook.active.title = "Orders"
    workbook.active.append(["order_id", "region"])
    workbook.active.append(["A-1", "DE"])
    workbook.create_sheet("Notes").append(["note"])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _room(tmp_path: Path, *, with_xlsx: bool = False) -> tuple[RunContext, Path]:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "room.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("orders.csv", b"order_id,region\nA-1,DE\nA-2,FR\n")
        output.writestr("notes.txt", b"opaque note\n")
        if with_xlsx:
            payload = _xlsx_bytes()
            if payload is None:
                pytest.skip("openpyxl is not installed")
            output.writestr("workbook.xlsx", payload)
    return RunContext("RUN-CATALOG-REGISTRY", run_root, (input_root,), core_version="0.3.0-test"), archive


def test_canonical_catalog_is_parameter_free_and_single_winner(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    telemetry = TelemetryRecorder(context=context)
    rooms = [DataRoom.open(context, DataAssetRef.from_path(archive), telemetry=telemetry) for _ in range(2)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        catalogs = tuple(executor.map(lambda room: room.build_catalog(), rooms))

    assert catalogs[0] == catalogs[1]
    catalog_path = rooms[0].catalog_path
    assert catalog_path.parent.name == "catalogs"
    assert catalog_path.is_file()
    before = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    assert len(list(catalog_path.parent.glob("*.json"))) == 1
    assert sum(event.event_type == "data_room_catalog_created" for event in telemetry.events) == 1
    assert sum(event.event_type == "data_room_catalog_reused" for event in telemetry.events) >= 1

    entry = next(item for item in catalogs[0] if item.path == "orders.csv")
    assert dict(rooms[0].sample(entry, limit=1)[0]) == {"order_id": "A-1", "region": "DE"}
    assert rooms[0].categories(entry, "region", limit=1) == ("DE",)
    assert hashlib.sha256(catalog_path.read_bytes()).hexdigest() == before

    # Once canonical metadata exists, view/search calls must not rescan the
    # archive or invoke the canonical member builders.
    rooms[0]._catalog_for_member = lambda member: (_ for _ in ()).throw(AssertionError("canonical rescan"))  # type: ignore[method-assign]
    assert rooms[0].search("orders")
    assert rooms[0].build_catalog() == catalogs[0]
    with pytest.raises(TypeError):
        rooms[0].build_catalog(sample_rows=1)  # type: ignore[call-arg]

    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert payload["catalog_schema_version"] == "1"
    assert payload["core_version"] == context.core_version
    assert payload["source_hash"] == rooms[0].archive_ref.content_hash
    assert all(not entry.get("sample_values") and not entry.get("sample_rows") for entry in payload["entries"])


def test_canonical_catalog_ignores_caller_catalog_row_limit(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    constrained = DataRoom.open(context, archive, max_catalog_rows=1)
    unconstrained = DataRoom.open(context, archive, max_catalog_rows=2)
    first = constrained.build_catalog()
    before = hashlib.sha256(constrained.catalog_path.read_bytes()).hexdigest()
    second = unconstrained.build_catalog()
    assert first == second
    assert hashlib.sha256(unconstrained.catalog_path.read_bytes()).hexdigest() == before
    orders = next(entry for entry in first if entry.path == "orders.csv")
    assert orders.row_count == 2
    assert orders.row_count_exact is True


def test_catalog_identity_changes_with_core_version_without_overwrite(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    first = DataRoom.open(context, archive)
    first.build_catalog()
    other_context = RunContext(context.run_id, context.run_root, context.input_roots, core_version="0.4.0-test")
    second = DataRoom.open(other_context, archive)
    second.build_catalog()
    assert first.catalog_key != second.catalog_key
    assert first.catalog_path != second.catalog_path
    assert sorted(path.name for path in first.catalog_root.glob("*.json")) == sorted(
        (first.catalog_path.name, second.catalog_path.name)
    )


def test_catalog_counts_keep_physical_and_expanded_xlsx_entries_distinct(tmp_path: Path) -> None:
    context, archive = _room(tmp_path, with_xlsx=True)
    room = DataRoom.open(context, archive)
    catalog = room.build_catalog()
    counts = room.catalog_counts(catalog)
    assert counts == CatalogCounts(archive_members=3, catalog_entries=4, table_members=2, sheet_entries=2)
    assert counts.archive_members != counts.catalog_entries
    # Equal numeric values are valid (this fixture has two physical tabular
    # members and two expanded worksheets); callers must use the named fields
    # rather than infer one count from another.
    assert isinstance(counts.table_members, int)
    assert isinstance(counts.sheet_entries, int)
    with pytest.raises(TypeError):
        CatalogCounts(archive_members="3", catalog_entries=4, table_members=2, sheet_entries=2)  # type: ignore[arg-type]


def test_prepared_registry_records_scope_independently_of_reuse(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    requirement = workbench.save_prepared("requirement", [{"value": 1}])
    reusable = workbench.save_prepared("reusable", [{"value": 2}], scope="reusable")
    exploratory = workbench.save_prepared("exploratory", [{"value": 3}], scope="exploratory")
    superseded = workbench.save_prepared("superseded", [{"value": 4}], scope="superseded")

    assert requirement.scope == "requirement_scoped"
    assert tuple(item.prepared_asset_id for item in workbench.prepared_registry.search()) == (
        "exploratory",
        "requirement",
        "reusable",
    )
    assert tuple(item.prepared_asset_id for item in workbench.prepared_registry.search(reusable_only=True)) == ("reusable",)
    assert workbench.prepared_registry.search(scope="requirement_scoped") == (requirement,)
    assert workbench.prepared_registry.search(include_superseded=True)[-1] == superseded
    assert workbench.prepared_registry.load("reusable").descriptor == reusable
    assert workbench.prepared_registry.load("requirement").descriptor.source_refs == requirement.source_refs

    registry_lines = (context.run_root / "lem" / "prepared_data_registry.jsonl").read_text(encoding="utf-8").splitlines()
    index = json.loads((context.run_root / "indexes" / "prepared_index.json").read_text(encoding="utf-8"))
    assert len(registry_lines) == 4
    assert len(index["entries"]) == 4
    assert {json.loads(line)["prepared_asset_id"] for line in registry_lines} == {"requirement", "reusable", "exploratory", "superseded"}

    # Exact duplicate registration is idempotent, while same-ID descriptor
    # collisions fail before changing registry state.
    assert workbench.prepared_registry.preflight_register(requirement) == requirement
    workbench.prepared_registry.register_accepted(requirement)
    before_collision = (context.run_root / "lem" / "prepared_data_registry.jsonl").read_bytes()
    with pytest.raises(ValueError, match="different descriptor"):
        workbench.save_prepared("requirement", [{"value": 999}])
    assert (context.run_root / "lem" / "prepared_data_registry.jsonl").read_bytes() == before_collision


def test_prepared_registry_rejects_tamper_and_path_escape(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    workbench = DataRoomWorkbench(context, archive)
    descriptor = workbench.save_prepared("tamper", [{"value": 1}], scope="reusable")
    prepared_path = context.resolve_run_path("prepared/tamper.jsonl")
    prepared_path.write_text(prepared_path.read_text(encoding="utf-8") + '{"value":2}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="content changed"):
        workbench.prepared_registry.load("tamper")

    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"value":1}\n', encoding="utf-8")
    escaped = replace(descriptor, prepared_asset_id="escaped", location=str(outside))
    with pytest.raises(AllowedRootError):
        workbench.prepared_registry.register_accepted(escaped)
    assert workbench.prepared_registry.search(prepared_asset_id="escaped", include_superseded=True) == ()


def test_prepared_materialization_is_one_locked_transaction_for_two_writers(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    workbenches = tuple(
        DataRoomWorkbench(context, DataAssetRef.from_path(archive))
        for _ in range(2)
    )

    def save(workbench: DataRoomWorkbench, value: int) -> tuple[str, object]:
        try:
            return "ok", workbench.save_prepared(
                "same-id",
                [{"value": value}],
                scope="reusable",
            )
        except Exception as exc:  # one writer must lose the same-ID decision
            return "error", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(save, workbenches, (1, 2)))

    assert sum(status == "ok" for status, _ in results) == 1
    errors = [value for status, value in results if status == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "different descriptor" in str(errors[0])

    payload_path = context.resolve_run_path("prepared/same-id.jsonl")
    sidecar_path = context.resolve_run_path("prepared/same-id.descriptor.json")
    registry_path = context.resolve_run_path("lem/prepared_data_registry.jsonl")
    index_path = context.resolve_run_path("indexes/prepared_index.json")
    descriptor_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    registry_payload = json.loads(registry_path.read_text(encoding="utf-8").splitlines()[0])
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(payload_path.read_bytes()).hexdigest()
    assert descriptor_payload["prepared_content_hash"] == expected_hash
    assert registry_payload == descriptor_payload
    assert index_payload["entries"] == [
        {
            "byte_count": descriptor_payload["byte_count"],
            "core_version": descriptor_payload["core_version"],
            "effective_period": descriptor_payload["effective_period"],
            "location": descriptor_payload["location"],
            "operation_manifest_hash": descriptor_payload["operation_manifest_hash"],
            "prepared_asset_id": "same-id",
            "prepared_content_hash": expected_hash,
            "row_count": descriptor_payload["row_count"],
            "scope": descriptor_payload["scope"],
            "source_hashes": descriptor_payload["source_hashes"],
            "source_refs": descriptor_payload["source_refs"],
            "status": descriptor_payload["status"],
        }
    ]


def test_prepared_materialization_equal_concurrent_writers_are_idempotent(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    workbenches = tuple(
        DataRoomWorkbench(context, DataAssetRef.from_path(archive))
        for _ in range(2)
    )

    def save(workbench: DataRoomWorkbench) -> object:
        return workbench.save_prepared(
            "same-id",
            [{"value": 7}],
            scope="reusable",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        descriptors = tuple(executor.map(save, workbenches))
    assert descriptors[0] == descriptors[1]
    assert workbenches[0].prepared_registry.search(prepared_asset_id="same-id") == (descriptors[0],)


def test_prepared_materialization_retry_repairs_registry_index_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, archive = _room(tmp_path)
    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    import auto_foundry_core.prepared as prepared_module

    original_atomic_write = prepared_module._atomic_write
    calls = 0

    def fail_before_index(path: Path, content: str) -> None:
        nonlocal calls
        calls += 1
        # Sidecar is call one, registry projection call two, index call three.
        if calls == 3:
            raise RuntimeError("injected publication crash")
        original_atomic_write(path, content)

    monkeypatch.setattr(prepared_module, "_atomic_write", fail_before_index)
    with pytest.raises(RuntimeError, match="publication crash"):
        workbench.save_prepared("crash-resume", [{"value": 1}], scope="reusable")
    assert context.resolve_run_path("prepared/crash-resume.jsonl").is_file()
    assert context.resolve_run_path("prepared/crash-resume.descriptor.json").is_file()
    assert context.resolve_run_path("lem/prepared_data_registry.jsonl").is_file()
    assert not context.resolve_run_path("indexes/prepared_index.json").exists()

    monkeypatch.setattr(prepared_module, "_atomic_write", original_atomic_write)
    descriptor = workbench.save_prepared("crash-resume", [{"value": 1}], scope="reusable")
    assert descriptor.prepared_asset_id == "crash-resume"
    assert context.resolve_run_path("indexes/prepared_index.json").is_file()
    assert workbench.prepared_registry.load("crash-resume").descriptor == descriptor
