from __future__ import annotations

import io
import sqlite3
from pathlib import Path
import struct
import zipfile
from types import SimpleNamespace

import pytest

import json

from auto_foundry_core.analysis import BoundAnalysisContext
from auto_foundry_core.analyst_workspace import AnalystWorkspace
from auto_foundry_core.contracts import DataAssetRef, TableRef
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.sources import discover, read_rows as source_read_rows, register_source
from auto_foundry_core.profiling import profile_source
from auto_foundry_core.workbench import DataRoom, DataRoomMember
import auto_foundry_core.workbench as workbench_module
from auto_foundry_core.workspace import RunContext


def _context(tmp_path: Path, run_id: str = "RUN-FORMATS") -> tuple[RunContext, Path]:
    inputs = tmp_path / "inputs"
    run = tmp_path / "run"
    inputs.mkdir()
    run.mkdir()
    return RunContext(run_id, run, (inputs,), core_version="formats-test"), inputs


def _archive_with_files(inputs: Path, files: dict[str, Path | bytes]) -> Path:
    archive = inputs / "room.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, value in files.items():
            if isinstance(value, Path):
                output.write(value, name)
            else:
                output.writestr(name, value)
    return archive


def _zip64_one_member_payload() -> bytes:
    """Construct a small archive with genuine ZIP64 end records."""

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("safe.csv", b"id\n1\n")
    payload = archive.getvalue()
    eocd = payload.rfind(b"PK\x05\x06")
    _, _, _, _, _, central_size, central_offset, _ = struct.unpack_from("<4s4H2LH", payload, eocd)
    prefix = payload[:eocd]
    zip64_offset = len(prefix)
    zip64 = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        1,
        1,
        central_size,
        central_offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1)
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return prefix + zip64 + locator + end


def _sqlite_file(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE "z table" ("id" INTEGER, "value" TEXT)')
    connection.executemany('INSERT INTO "z table" VALUES (?, ?)', [(2, "b"), (1, "a")])
    connection.execute('CREATE TABLE alpha ("select" TEXT)')
    connection.execute("INSERT INTO alpha VALUES ('x')")
    connection.commit()
    connection.close()
    return path


def test_data_room_catalogs_and_reads_parquet_without_whole_row_load(tmp_path: Path) -> None:
    pyarrow = pytest.importorskip("pyarrow")
    import pyarrow.parquet as parquet

    context, inputs = _context(tmp_path, "RUN-PARQUET")
    path = inputs / "orders.parquet"
    parquet.write_table(pyarrow.table({"id": [1, 2], "amount": [10.5, 20.0]}), path)
    room = DataRoom.open(context, _archive_with_files(inputs, {"orders.parquet": path}))

    entry = room.build_catalog()[0]
    assert entry.kind == "table"
    assert entry.format == "parquet"
    assert entry.columns == ("id", "amount")
    assert entry.row_count == 2
    assert entry.row_count_exact is True
    assert room.read_rows(entry, limit=1) == [{"id": 1, "amount": 10.5}]
    assert room.sample(entry, limit=2)[1]["id"] == 2
    assert room.categories(entry, "id") == (1, 2)


def test_data_room_catalogs_each_sqlite_user_table_and_ids_are_distinct(tmp_path: Path) -> None:
    context, inputs = _context(tmp_path, "RUN-SQLITE")
    database = _sqlite_file(inputs / "multi.sqlite3")
    room = DataRoom.open(context, _archive_with_files(inputs, {"multi.sqlite3": database}))

    entries = room.build_catalog()
    assert [entry.table_name for entry in entries] == ["alpha", "z table"]
    assert [entry.source_id for entry in entries] == ["multi.sqlite3::table=alpha", "multi.sqlite3::table=z table"]
    assert entries[1].columns == ("id", "value")
    assert entries[1].row_count is None and entries[1].row_count_exact is False
    assert entries[1].metadata["row_count_kind"] == "unknown"
    assert room.read_rows(entries[1], limit=1, offset=1) == [{"id": 1, "value": "a"}]
    assert room.sample(entries[0], limit=1) == ({"select": "x"},)
    assert room.search("z table", catalog=entries)[0] is entries[1]


def test_safe_unknown_and_extensionless_members_remain_opaque_and_catalogable(tmp_path: Path) -> None:
    context, inputs = _context(tmp_path, "RUN-OPAQUE-UNKNOWN")
    archive = _archive_with_files(inputs, {"notebook.ipynb": b"{}", "README": b"opaque bytes"})
    room = DataRoom.open(context, archive)
    entries = room.build_catalog()
    assert {(entry.path, entry.kind) for entry in entries} == {
        ("README", "opaque"),
        ("notebook.ipynb", "opaque"),
    }
    assert all(entry.metadata["requires_custom_code"] for entry in entries)
    assert room.materialize_opaque("README", "opaque/README").read_bytes() == b"opaque bytes"


def test_data_room_accepts_zip64_central_directory_and_fingerprints_it(tmp_path: Path) -> None:
    context, inputs = _context(tmp_path, "RUN-ZIP64")
    archive = inputs / "zip64.zip"
    archive.write_bytes(_zip64_one_member_payload())

    room = DataRoom.open(context, archive)

    assert [member.path for member in room.members()] == ["safe.csv"]
    assert room.central_directory_fingerprint["entry_count"] == 1
    assert room.central_directory_fingerprint["size_bytes"] > 0


def test_data_room_rejects_malformed_zip64_locator_before_zipfile_reads(tmp_path: Path) -> None:
    context, inputs = _context(tmp_path, "RUN-ZIP64-MALFORMED")
    payload = bytearray(_zip64_one_member_payload())
    eocd = payload.rfind(b"PK\x05\x06")
    locator = eocd - 20
    payload[locator + 8 : locator + 16] = (999).to_bytes(8, "little")
    archive = inputs / "zip64-mismatch.zip"
    archive.write_bytes(payload)

    with pytest.raises(ValueError, match="ZIP64 locator offset"):
        DataRoom.open(context, archive)


def test_large_sqlite_member_bypasses_default_64mib_cap_with_streaming_materialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context, inputs = _context(tmp_path, "RUN-LARGE-SQLITE")
    database = inputs / "large.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE payload (value BLOB)")
    chunk = sqlite3.Binary(b"x" * 1024)
    connection.executemany("INSERT INTO payload VALUES (?)", ((chunk,) for _ in range(65 * 1024)))
    connection.commit()
    connection.close()
    archive = _archive_with_files(inputs, {"large.db": database})
    room = DataRoom.open(context, archive)
    assert next(member for member in room.members() if member.path == "large.db").size_bytes > 64 * 1024 * 1024

    # The SQLite path must not fall back to the in-memory byte reader.
    original_reader = room._read_member_bytes
    monkeypatch.setattr(room, "_read_member_bytes", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("whole-member reader used")))
    entry = room.build_catalog()[0]
    assert entry.table_name == "payload"
    assert room.read_rows(entry, limit=1)[0]["value"] == b"x" * 1024
    monkeypatch.setattr(room, "_read_member_bytes", original_reader)


def test_archive_total_is_unbounded_by_default_but_explicit_cap_still_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal inventory accepts large declared totals without allocating them."""

    context, inputs = _context(tmp_path, "RUN-TOTAL-LIMIT")
    archive = _archive_with_files(inputs, {"payload.bin": b"x"})
    declared_size = 256 * 1024 * 1024 + 1

    def oversized_inventory_member(
        source: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        *,
        limits: object,
        seen_names: set[str],
    ) -> DataRoomMember:
        del source, limits
        seen_names.add(info.filename)
        return DataRoomMember(
            path=info.filename,
            format="binary",
            kind="opaque",
            size_bytes=declared_size,
            compressed_size_bytes=1,
            content_hash="a" * 64,
        )

    monkeypatch.setattr(workbench_module, "_inspect_archive_member", oversized_inventory_member)

    room = DataRoom.open(context, archive)
    assert room.members()[0].size_bytes == declared_size

    with pytest.raises(ValueError, match="max_total_bytes"):
        DataRoom.open(context, archive, max_total_bytes=256 * 1024 * 1024)

    with pytest.raises(ValueError, match="cannot be negative"):
        DataRoom.open(context, archive, max_total_bytes=-1)


def test_selected_member_materialization_checks_actual_temp_disk_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context, inputs = _context(tmp_path, "RUN-DISK-CHECK")
    database = _sqlite_file(inputs / "small.db")
    room = DataRoom.open(context, _archive_with_files(inputs, {"small.db": database}))
    monkeypatch.setattr(workbench_module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="temporary disk space"):
        room.build_catalog()


def test_direct_sqlite_source_discovery_read_and_profile_are_bounded(tmp_path: Path) -> None:
    database = _sqlite_file(tmp_path / "direct.db")
    registered = register_source(database)
    tables = discover(registered)
    assert [table.name for table in tables] == ["alpha", "z table"]
    table = next(item for item in tables if isinstance(item, TableRef) and item.name == "z table")
    assert source_read_rows(table, limit=1) == [{"id": 2, "value": "b"}]
    profile = profile_source(table, sample_limit=1)
    assert profile["sampled_rows"] == 1
    assert profile["row_count_exact"] is False


def test_analyst_source_selection_distinguishes_sqlite_tables(tmp_path: Path) -> None:
    context, inputs = _context(tmp_path, "RUN-SQLITE-ANALYST")
    database = _sqlite_file(inputs / "multi.db")
    archive = _archive_with_files(inputs, {"multi.db": database})
    from auto_foundry_core.workbench import DataRoomWorkbench

    workbench = DataRoomWorkbench(context, DataAssetRef.from_path(archive))
    item = ItemWorkspace.create(context, "Q-SQLITE", original_text="Inspect both SQLite tables.")
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item, workbench=workbench)
    analyst = AnalystWorkspace(bound, owner_ref="owner-sqlite")

    sources = analyst.search_sources("multi.db", limit=10)
    source_ids = tuple(source.source_id for source in sources)
    assert source_ids == ("multi.db::table=alpha", "multi.db::table=z table")
    assert analyst.sample_source(source_ids[0], limit=1)[0]["select"] == "x"
    analyst.select_sources(source_ids, purpose="SQLite table coverage")
    persisted = json.loads((item.work_root / "source_map.json").read_text(encoding="utf-8"))
    assert [row["source_id"] for row in persisted] == list(source_ids)
    selected_entry = bound.source_catalog.entries[1]
    materialized, _, _ = workbench._materialize_rows(selected_entry, sheet=None, max_rows=10)
    assert materialized == [{"id": 2, "value": "b"}, {"id": 1, "value": "a"}]
