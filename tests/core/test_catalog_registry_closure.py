from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

import pytest

from auto_foundry_core.contracts import DataAssetRef
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workbench import CATALOG_SCHEMA_VERSION, CatalogCounts, DataRoom
from auto_foundry_core.workspace import RunContext


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
    assert payload["catalog_schema_version"] == CATALOG_SCHEMA_VERSION
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


def test_bound_child_reuses_persisted_inventory_and_catalog_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, archive = _room(tmp_path)
    from auto_foundry_core.analysis import BoundAnalysisContext, load_bound_analysis_context
    from auto_foundry_core.durable import ItemWorkspace

    item = ItemWorkspace.create(context, "Q-001", original_text="catalog")
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item)
    counter_path = context.run_root / "telemetry" / "inventory_counters.json"
    counter_bytes = counter_path.read_bytes()
    counter_mtime = counter_path.stat().st_mtime_ns
    for _ in range(2):
        monkeypatch.setattr(
            zipfile.ZipFile,
            "infolist",
            lambda _zip: (_ for _ in ()).throw(AssertionError("bound reload enumerated ZipInfo")),
        )
        loaded = load_bound_analysis_context(context, path=bound.manifest_path)
        assert loaded.source_catalog.entries == bound.source_catalog.entries
        monkeypatch.undo()

    counters = bound.data_room.instrumentation_counters
    assert counter_path.read_bytes() == counter_bytes
    assert counter_path.stat().st_mtime_ns == counter_mtime
    assert counters["archive_full_hash"]["count"] == 1
    assert counters["catalog_created"]["count"] == 1
    assert "catalog_loaded" not in counters
    assert "catalog_reused" not in counters
    assert counters["central_directory_fingerprint"]["count"] == 1
    assert counters["member_content_hash"]["bytes"] >= sum(member.size_bytes for member in bound.data_room.members())
    assert "selected_member_read" not in counters

    loaded.data_room.read_rows("orders.csv", limit=1)
    counters = loaded.data_room.instrumentation_counters
    assert counters["selected_member_read"]["count"] == 1
    assert counters["selected_member_read"]["bytes"] == len(b"order_id,region\nA-1,DE\nA-2,FR\n")
    loaded.data_room.verify_source_full()
    assert loaded.data_room.instrumentation_counters["verify_source_full"]["count"] == 1


def test_candidate_materialization_stays_out_of_accepted_registry(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    from auto_foundry_core.analysis import BoundAnalysisContext
    from auto_foundry_core.durable import ItemWorkspace

    item = ItemWorkspace.create(context, "Q-001", original_text="prepared")
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item)
    descriptor = bound.save_prepared_candidate("candidate", [{"value": 1}], scope="reusable")
    assert Path(descriptor.location).is_file()
    assert Path(descriptor.location).parent.parent == item.work_root / "prepared"
    assert bound.prepared_assets.search(prepared_asset_id="candidate") == ()
    assert bound.save_prepared_candidate("candidate", [{"value": 1}], scope="reusable") == descriptor
    with pytest.raises(ValueError, match="different descriptor"):
        bound.save_prepared_candidate("candidate", [{"value": 2}], scope="reusable")


def test_bound_reload_rejects_central_directory_mutation_with_same_stat(tmp_path: Path) -> None:
    context, archive = _room(tmp_path)
    from auto_foundry_core.analysis import BoundAnalysisContext, load_bound_analysis_context
    from auto_foundry_core.durable import ItemWorkspace

    item = ItemWorkspace.create(context, "Q-CENTRAL", original_text="central")
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item)
    original_stat = archive.stat()
    changed = archive.read_bytes().replace(b"notes.txt", b"notes.bin")
    assert changed != archive.read_bytes() and len(changed) == len(archive.read_bytes())
    archive.write_bytes(changed)
    os.utime(archive, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(ValueError, match="central directory"):
        load_bound_analysis_context(context, path=bound.manifest_path)
