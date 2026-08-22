from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import zipfile

import pytest

from auto_foundry_core.contracts import DataAssetRef
from auto_foundry_core.analysis import BoundAnalysisContext
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.telemetry import TelemetryRecorder
from auto_foundry_core.workbench import CatalogCounts, DataRoom, DataRoomCatalogEntry
from auto_foundry_core.workspace import AllowedRootError, RunContext


def _xlsx_bytes() -> bytes | None:
    try:
        import openpyxl
    except ImportError:
        return None
    book = openpyxl.Workbook()
    first = book.active
    first.title = "Orders"
    first.append(["order_id", "amount"])
    first.append(["A-1", 10])
    first.append(["A-2", 20])
    second = book.create_sheet("Notes")
    second.append(["note"])
    second.append(["hello"])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


@pytest.fixture()
def room_fixture(tmp_path: Path) -> tuple[RunContext, Path, dict[str, bytes]]:
    input_root = tmp_path / "nested_inputs"
    run_root = tmp_path / "nested_run"
    input_root.mkdir()
    run_root.mkdir()
    payloads: dict[str, bytes] = {
        "sales.csv": b"order_id,amount\nA-1,10\nA-2,20\n",
        "sales.tsv": b"order_id\tregion\nA-1\tDE\nA-2\tFR\n",
        "records.json": b'[{"order_id":"A-1","amount":10},{"order_id":"A-2","amount":20}]',
        "events.jsonl": b'{"event":"open"}\n\n{"event":"close"}\n',
        "readme.txt": "Data room notes\nsecond line\n".encode(),
    }
    xlsx = _xlsx_bytes()
    if xlsx is not None:
        payloads["workbook.xlsx"] = xlsx
    archive = input_root / "room.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in payloads.items():
            output.writestr(name, data)
    context = RunContext("RUN-TEST", run_root, (input_root,), core_version="0.3.0-test")
    return context, archive, payloads


def test_inventory_hash_search_and_read_formats(room_fixture: tuple[RunContext, Path, dict[str, bytes]]) -> None:
    context, archive, payloads = room_fixture
    telemetry = TelemetryRecorder(context=context)
    room = DataRoom.open(context, archive, telemetry=telemetry)

    members = room.members()
    assert [member.path for member in members] == sorted(payloads)
    csv_member = next(member for member in members if member.path == "sales.csv")
    assert csv_member.content_hash == hashlib.sha256(payloads["sales.csv"]).hexdigest()
    assert csv_member.size_bytes == len(payloads["sales.csv"])
    assert room.read_rows(csv_member, limit=1) == [{"order_id": "A-1", "amount": "10"}]
    assert room.read_rows("sales.tsv", offset=1) == [{"order_id": "A-2", "region": "FR"}]
    assert room.read_rows("records.json")[-1]["order_id"] == "A-2"
    assert room.read_rows("events.jsonl", offset=1) == [{"event": "close"}]
    assert room.document_excerpt("readme.txt", max_bytes=8) == "Data roo"

    catalog = room.build_catalog()
    assert all(isinstance(entry, DataRoomCatalogEntry) for entry in catalog)
    assert any(entry.path == "sales.csv" and entry.columns == ("order_id", "amount") for entry in catalog)
    assert all(not entry.sample_values and not entry.sample_rows for entry in catalog)
    assert room.search("sales.csv", catalog=catalog, limit=1)[0].path == "sales.csv"
    assert room.search("region", catalog=catalog)[0].path == "sales.tsv"
    sales_entry = next(entry for entry in catalog if entry.path == "sales.csv")
    assert room.sample(sales_entry, limit=1) == ({"order_id": "A-1", "amount": "10"},)
    assert room.categories(sales_entry, "order_id", limit=2) == ("A-1", "A-2")
    counts = room.catalog_counts(catalog)
    assert isinstance(counts, CatalogCounts)
    assert counts.archive_members == len(payloads)
    assert counts.catalog_entries == len(catalog)
    assert room.catalog_path.is_file()
    assert room.catalog_path.parent.name == "catalogs"
    assert room.build_catalog() == catalog
    assert {event.event_type for event in telemetry.events} >= {
        "data_room_archive_read",
        "data_room_member_read",
        "data_room_catalog_created",
    }


def test_macos_metadata_is_ignored_without_mutating_archive(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "room.zip"
    csv_payload = b"order_id,amount\nA-1,10\n"
    metadata = {
        "__MACOSX/._room.csv": b"apple-double metadata",
        "__MACOSX/nested/._.DS_Store": b"finder metadata",
        "nested/.DS_Store": b"finder metadata",
    }
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("room.csv", csv_payload)
        for name, payload in metadata.items():
            output.writestr(name, payload)

    original_bytes = archive.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    context = RunContext("RUN-MACOS-METADATA", run_root, (input_root,))
    room = DataRoom.open(context, archive)

    assert [member.path for member in room.members()] == ["room.csv"]
    assert room.read_rows("room.csv") == [{"order_id": "A-1", "amount": "10"}]
    assert room.build_catalog()[0].path == "room.csv"
    assert archive.read_bytes() == original_bytes
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize(
    ("name", "payload", "kwargs", "message"),
    [
        ("__MACOSX/../.DS_Store", b"unsafe", {}, "unsafe ZIP member name"),
        ("__MACOSX/large", b"x" * 32, {"max_member_bytes": 8}, "max_member_bytes"),
        ("__MACOSX/large", b"x" * 1024, {"max_compression_ratio": 1.0}, "max_compression_ratio"),
        ("nested/.DS_Store", b"x" * 32, {"max_total_bytes": 8}, "max_total_bytes"),
    ],
)
def test_ignored_metadata_still_obeys_archive_safety_checks(
    tmp_path: Path,
    name: str,
    payload: bytes,
    kwargs: dict[str, object],
    message: str,
) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "unsafe.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, payload)
    context = RunContext("RUN-MACOS-METADATA-SAFETY", run_root, (input_root,))

    with pytest.raises(ValueError, match=message):
        DataRoom.open(context, archive, **kwargs)


def test_duplicate_ignored_metadata_names_are_rejected(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("__MACOSX/.DS_Store", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            output.writestr("__MACOSX/.DS_Store", b"second")
    context = RunContext("RUN-MACOS-METADATA-DUPLICATE", run_root, (input_root,))

    with pytest.raises(ValueError, match="duplicate ZIP member name"):
        DataRoom.open(context, archive)


def test_symlink_ignored_metadata_is_rejected(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "symlink.zip"
    symlink = zipfile.ZipInfo("__MACOSX/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(symlink, b"target")
    context = RunContext("RUN-MACOS-METADATA-SYMLINK", run_root, (input_root,))

    with pytest.raises(ValueError, match="symlink"):
        DataRoom.open(context, archive)


def test_pdf_members_are_opaque_catalog_documents_without_excerpt_decoding(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "documents.zip"
    pdf_payload = b"%PDF-1.4\nopaque test bytes\n%%EOF\n"
    csv_payload = b"id,status\nA-1,open\n"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("policy.pdf", pdf_payload)
        output.writestr("records.csv", csv_payload)

    original_bytes = archive.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    context = RunContext("RUN-PDF-OPAQUE", run_root, (input_root,))
    room = DataRoom.open(context, archive)

    pdf_member = next(member for member in room.members() if member.path == "policy.pdf")
    assert pdf_member.format == "pdf"
    assert pdf_member.kind == "document"
    assert pdf_member.size_bytes == len(pdf_payload)
    assert pdf_member.content_hash == hashlib.sha256(pdf_payload).hexdigest()

    catalog = room.build_catalog()
    pdf_entry = next(entry for entry in catalog if entry.path == "policy.pdf")
    assert pdf_entry.member == pdf_member
    assert pdf_entry.columns == ()
    assert pdf_entry.sample_values == {}
    assert pdf_entry.sample_rows == ()
    assert pdf_entry.row_count is None
    assert pdf_entry.row_count_exact is False
    assert pdf_entry.metadata == {"extraction": "opaque", "requires_custom_code": True}
    assert room.search("policy.pdf", catalog=catalog) == (pdf_entry,)
    with pytest.raises(ValueError, match="PDF document excerpts require custom code"):
        room.document_excerpt(pdf_member)

    assert archive.read_bytes() == original_bytes
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_hash


def test_pdf_members_still_obey_archive_bounds(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "bounded-documents.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("policy.pdf", b"x" * 1024)
    context = RunContext("RUN-PDF-BOUNDS", run_root, (input_root,))

    with pytest.raises(ValueError, match="max_member_bytes"):
        DataRoom.open(context, archive, max_member_bytes=8)
    with pytest.raises(ValueError, match="max_compression_ratio"):
        DataRoom.open(context, archive, max_compression_ratio=1.0)


def test_xlsx_sheet_reads_and_catalog(room_fixture: tuple[RunContext, Path, dict[str, bytes]]) -> None:
    context, archive, payloads = room_fixture
    if "workbook.xlsx" not in payloads:
        pytest.skip("openpyxl is not installed")
    room = DataRoom.open(context, archive)
    assert room.read_rows("workbook.xlsx", sheet="Orders") == [
        {"order_id": "A-1", "amount": 10},
        {"order_id": "A-2", "amount": 20},
    ]
    entries = room.build_catalog()
    sheets = {(entry.path, entry.sheet_name) for entry in entries if entry.path == "workbook.xlsx"}
    assert sheets == {("workbook.xlsx", "Orders"), ("workbook.xlsx", "Notes")}
    orders = next(entry for entry in entries if entry.path == "workbook.xlsx" and entry.sheet_name == "Orders")
    assert room.read_rows(orders) == room.read_rows("workbook.xlsx", sheet="Orders")


@pytest.mark.parametrize("name", ["../escape.csv", "/absolute.csv", "folder\\escape.csv", "folder/../escape.csv"])
def test_unsafe_member_names_are_rejected(tmp_path: Path, name: str) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"a\n1\n")
    context = RunContext("RUN-UNSAFE", run_root, (input_root,))
    with pytest.raises(ValueError, match="unsafe ZIP member name"):
        DataRoom.open(context, archive)


def test_symlink_rejected_but_safe_unsupported_member_is_opaque(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    symlink = zipfile.ZipInfo("link.txt")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = input_root / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(symlink, b"target")
    context = RunContext("RUN-SYMLINK", run_root, (input_root,))
    with pytest.raises(ValueError, match="symlink"):
        DataRoom.open(context, archive)

    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("legacy.xls", b"not supported")
    room = DataRoom.open(context, archive)
    member = room.members()[0]
    assert member.kind == "opaque"
    entry = room.build_catalog()[0]
    assert entry.metadata["extraction"] == "opaque"
    destination = room.materialize_opaque(member, "opaque/legacy.xls")
    assert destination.read_bytes() == b"not supported"
    with pytest.raises(ValueError, match="explicit materialization"):
        room.read_rows(member)


def test_opaque_materialization_rejects_lexical_symlink_destinations(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "opaque.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("legacy.xls", b"not supported")
    context = RunContext("RUN-OPAQUE-SYMLINK-DESTINATION", run_root, (input_root,))
    room = DataRoom.open(context, archive)
    member = room.members()[0]

    real_target = run_root / "real-target.xls"
    real_target.write_bytes(b"sentinel")
    direct_alias = run_root / "direct-alias.xls"
    direct_alias.symlink_to(real_target)
    with pytest.raises(AllowedRootError, match="symlink"):
        room.materialize_opaque(member, direct_alias)
    assert real_target.read_bytes() == b"sentinel"

    real_parent = run_root / "real-parent"
    real_parent.mkdir()
    parent_alias = run_root / "parent-alias"
    parent_alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(AllowedRootError, match="symlink"):
        room.materialize_opaque(member, parent_alias / "nested.xls")
    assert not (real_parent / "nested.xls").exists()

    normal = room.materialize_opaque(member, run_root / "opaque" / "normal.xls")
    assert normal.read_bytes() == b"not supported"


def test_bounded_json_and_archive_immutability(room_fixture: tuple[RunContext, Path, dict[str, bytes]]) -> None:
    context, archive, _ = room_fixture
    room = DataRoom.open(context, archive, max_json_bytes=8)
    with pytest.raises(ValueError, match="max_bytes"):
        room.read_rows("records.json")

    original = archive.read_bytes()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("sales.csv", b"order_id,amount\nCHANGED,1\n")
    assert archive.read_bytes() != original
    with pytest.raises(ValueError, match="archive changed"):
        room.members()


def test_zip_bomb_and_member_bounds(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "large.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("large.txt", b"x" * 1024)
    context = RunContext("RUN-BOUNDS", run_root, (input_root,))
    with pytest.raises(ValueError, match="max_member_bytes"):
        DataRoom.open(context, archive, max_member_bytes=100)
    with pytest.raises(ValueError, match="max_compression_ratio"):
        DataRoom.open(context, archive, max_compression_ratio=1.0)


def test_prepared_candidate_materialization_descriptor_and_registry_boundary(room_fixture: tuple[RunContext, Path, dict[str, bytes]]) -> None:
    context, archive, _ = room_fixture
    item = ItemWorkspace.create(context, "Q-PREPARED", original_text="prepared")
    bound = BoundAnalysisContext.create(context, DataAssetRef.from_path(archive), item)
    descriptor = bound.save_prepared_candidate(
        "sales-prepared",
        "sales.csv",
        format="jsonl",
        grain="one row per order",
        effective_period="2023-12-19/2024-07-04",
        ontology_refs=(ref for ref in ("ontology:order", "ontology:status")),
        transformations=(step for step in ("read_csv", "bounded")),
        source_refs=(ref for ref in (DataAssetRef.from_path(archive),)),
        lineage={"purpose": "test"},
    )
    prepared_path = Path(descriptor.location)
    descriptor_path = prepared_path.parent / "sales-prepared.descriptor.json"
    assert prepared_path.is_file() and descriptor_path.is_file()
    assert descriptor.location == str(prepared_path)
    assert descriptor.prepared_content_hash == hashlib.sha256(prepared_path.read_bytes()).hexdigest()
    assert descriptor.row_count == 2
    assert descriptor.byte_count == prepared_path.stat().st_size
    assert descriptor.core_version == context.core_version
    assert descriptor.scope == "requirement_scoped"
    assert descriptor.effective_period == "2023-12-19/2024-07-04"
    assert descriptor.source_hashes
    assert descriptor.lineage["members"][0]["path"] == "sales.csv"
    assert descriptor.transformations == ("read_csv", "bounded")
    assert descriptor.ontology_refs == ("ontology:order", "ontology:status")
    sidecar = json.loads(descriptor_path.read_text(encoding="utf-8"))
    assert sidecar["effective_period"] == "2023-12-19/2024-07-04"
    assert sidecar["ontology_refs"] == ["ontology:order", "ontology:status"]
    assert sidecar == descriptor.to_dict()

    assert bound.prepared_assets.search(prepared_asset_id="sales-prepared") == ()
    assert bound.save_prepared_candidate(
        "sales-prepared",
        "sales.csv",
        format="jsonl",
        grain="one row per order",
        effective_period="2023-12-19/2024-07-04",
        ontology_refs=(ref for ref in ("ontology:order", "ontology:status")),
        transformations=(step for step in ("read_csv", "bounded")),
        source_refs=(ref for ref in (DataAssetRef.from_path(archive),)),
        lineage={"purpose": "test"},
    ) == descriptor

    with pytest.raises(ValueError, match="different descriptor"):
        bound.save_prepared_candidate(
            "sales-prepared",
            "sales.csv",
            format="jsonl",
            grain="one row per order",
            effective_period="2024-01-01/2024-12-31",
            ontology_refs=("ontology:order", "ontology:status"),
            transformations=(step for step in ("read_csv", "bounded")),
            source_refs=(ref for ref in (DataAssetRef.from_path(archive),)),
            lineage={"purpose": "test"},
        )

    no_period = bound.save_prepared_candidate("sales-without-period", "sales.csv")
    assert no_period.effective_period is None

    prepared_path.write_text(prepared_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="different descriptor|incomplete"):
        bound.save_prepared_candidate("sales-prepared", "sales.csv")


def _nested_zip(*entries: tuple[zipfile.ZipInfo | str, bytes], compression: int = zipfile.ZIP_STORED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize(
    ("payload", "kwargs", "message"),
    [
        (_nested_zip(("../escape.xml", b"bad")), {}, "unsafe ZIP member name"),
        (
            _nested_zip(
                (
                    (lambda info: (setattr(info, "create_system", 3), setattr(info, "external_attr", (stat.S_IFLNK | 0o777) << 16), info)[-1])(
                        zipfile.ZipInfo("xl/link.xml")
                    ),
                    b"target",
                )
            ),
            {},
            "symlink",
        ),
        (_nested_zip(("xl/data.xml", b"x" * 2048), compression=zipfile.ZIP_DEFLATED), {"max_xlsx_compression_ratio": 1.0}, "compression_ratio"),
        (_nested_zip(("xl/a.xml", b"a" * 40), ("xl/b.xml", b"b" * 40)), {"max_xlsx_total_bytes": 50}, "max_xlsx_total_bytes"),
    ],
)
def test_xlsx_nested_zip_preflight_rejects_abuse(tmp_path: Path, payload: bytes, kwargs: dict[str, object], message: str) -> None:
    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    archive = input_root / "bad.xlsx.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("bad.xlsx", payload)
    context = RunContext("RUN-XLSX-BOUNDS", run_root, (input_root,))
    with pytest.raises(ValueError, match=message):
        DataRoom.open(context, archive, **kwargs)


def test_xlsx_entry_bound_counts_directories_and_exact_valid_bound(room_fixture: tuple[RunContext, Path, dict[str, bytes]], tmp_path: Path) -> None:
    context, archive, payloads = room_fixture
    if "workbook.xlsx" not in payloads:
        pytest.skip("openpyxl is not installed")
    with zipfile.ZipFile(io.BytesIO(payloads["workbook.xlsx"]), "r") as nested:
        exact_count = len(nested.infolist())
    DataRoom.open(context, archive, max_xlsx_entries=exact_count)
    with pytest.raises(ValueError, match="max_xlsx_entries"):
        DataRoom.open(context, archive, max_xlsx_entries=exact_count - 1)

    input_root = tmp_path / "inputs"
    run_root = tmp_path / "run"
    input_root.mkdir()
    run_root.mkdir()
    directories_only = _nested_zip(*[(f"xl/dir-{index}/", b"") for index in range(3)])
    directory_archive = input_root / "directories.zip"
    with zipfile.ZipFile(directory_archive, "w") as output:
        output.writestr("directories.xlsx", directories_only)
    directory_context = RunContext("RUN-XLSX-DIRECTORIES", run_root, (input_root,))
    with pytest.raises(ValueError, match="max_xlsx_entries"):
        DataRoom.open(directory_context, directory_archive, max_xlsx_entries=2)


def test_prepared_encoded_byte_cap_and_provenance_integrity(room_fixture: tuple[RunContext, Path, dict[str, bytes]]) -> None:
    context, archive, _ = room_fixture
    item = ItemWorkspace.create(context, "Q-PREPARED-CAPS", original_text="prepared")
    bound = BoundAnalysisContext.create(context, archive, item)
    with pytest.raises(ValueError, match="max_bytes"):
        bound.save_prepared_candidate("too-large", [{"value": "0123456789"}], max_bytes=1)
    assert not (item.work_root / "prepared" / "too-large").exists()

    forged = DataAssetRef(uri="room.zip", format="zip", content_hash="0" * 64)
    with pytest.raises(ValueError, match="source changed after registration"):
        bound.save_prepared_candidate("forged", [{"value": 1}], source_refs=(forged,))
    assert not (item.work_root / "prepared" / "forged").exists()

    with pytest.raises(ValueError, match="reserved keys"):
        bound.save_prepared_candidate("lineage-override", [{"value": 1}], lineage={"archive": {"forged": True}})
    assert not (item.work_root / "prepared" / "lineage-override").exists()


def test_json_override_does_not_mutate_room_limits(room_fixture: tuple[RunContext, Path, dict[str, bytes]]) -> None:
    context, archive, _ = room_fixture
    room = DataRoom.open(context, archive, max_json_bytes=1024)
    original_limits = room.limits
    with pytest.raises(ValueError, match="max_bytes"):
        room.read_rows("records.json", max_json_bytes=8)
    assert room.limits is original_limits
    assert room.limits.max_json_bytes == 1024
    assert room.read_rows("records.json", max_json_bytes=1024)[0]["order_id"] == "A-1"
    assert room.limits is original_limits


def test_prepared_rejects_archive_mutation_before_manual_materialization(room_fixture: tuple[RunContext, Path, dict[str, bytes]]) -> None:
    context, archive, _ = room_fixture
    item = ItemWorkspace.create(context, "Q-PREPARED-MUTATION", original_text="prepared")
    bound = BoundAnalysisContext.create(context, archive, item)
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("sales.csv", b"order_id,amount\nCHANGED,1\n")
    with pytest.raises(ValueError, match="archive changed"):
        bound.save_prepared_candidate("mutated", [{"value": 1}])
    assert not (item.work_root / "prepared" / "mutated").exists()
