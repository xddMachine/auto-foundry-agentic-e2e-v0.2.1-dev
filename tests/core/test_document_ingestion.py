from __future__ import annotations

import io
from pathlib import Path
import zipfile

import pytest

from auto_foundry_core.document_ingestion import (
    DocumentCatalog,
    UnsafeDocumentArchiveError,
    ingest_document_catalog,
    normalize_document_bytes,
)


def test_text_csv_and_xlsx_have_stable_provenance(tmp_path: Path) -> None:
    text = tmp_path / "brief.txt"
    text.write_text("Audience\nOperations", encoding="utf-8")
    catalog = ingest_document_catalog(text)
    assert isinstance(catalog, DocumentCatalog)
    assert catalog[0].sections[0].locator["paragraph"] == 1
    assert catalog[0].sections[0].content_hash

    csv_doc = normalize_document_bytes(b"name,value\nA,10\n", document_ref="table.csv")
    assert csv_doc.sections[0].kind == "table_excerpt"
    assert csv_doc.sections[0].locator["cells"] == ["A1", "B1"]

    try:
        import openpyxl
    except ImportError:
        pytest.skip("optional openpyxl is unavailable")
    workbook = openpyxl.Workbook()
    workbook.active.title = "Sheet A"
    workbook.active.append(["name", "value"])
    workbook.active.append(["A", 10])
    stream = io.BytesIO()
    workbook.save(stream)
    xlsx_doc = normalize_document_bytes(stream.getvalue(), document_ref="book.xlsx")
    assert any(section.locator.get("sheet") == "Sheet A" for section in xlsx_doc.sections)


def test_docx_odt_and_pdf_extraction_or_explicit_limitation() -> None:
    # Tiny constrained OOXML fixtures exercise the fallback ZIP/XML reader.
    docx_stream = io.BytesIO()
    with zipfile.ZipFile(docx_stream, "w") as archive:
        archive.writestr(
            "word/document.xml",
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>',
        )
    docx = normalize_document_bytes(docx_stream.getvalue(), document_ref="brief.docx")
    assert docx.sections[0].text == "Hello"

    odt_stream = io.BytesIO()
    with zipfile.ZipFile(odt_stream, "w") as archive:
        archive.writestr(
            "content.xml",
            b'<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text><text:p>Hello ODT</text:p></office:text></office:body></office:document-content>',
        )
    odt = normalize_document_bytes(odt_stream.getvalue(), document_ref="brief.odt")
    assert odt.sections[0].text == "Hello ODT"

    pdf = normalize_document_bytes(b"not a PDF", document_ref="brief.pdf")
    assert pdf.extraction in {"opaque", "limited"}
    assert pdf.limitations


@pytest.mark.parametrize("fmt,member", (("docx", "word/document.xml"), ("odt", "content.xml")))
def test_office_xml_rejects_dtd_and_external_entities(fmt: str, member: str, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("should-not-be-extracted", encoding="utf-8")
    if fmt == "docx":
        xml = (
            b'<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file://'
            + str(secret).encode("utf-8")
            + b'">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:document>"
        )
    else:
        xml = (
            b'<!DOCTYPE office:document-content [<!ENTITY xxe SYSTEM "file://'
            + str(secret).encode("utf-8")
            + b'">]><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            b'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text>'
            b"<text:p>&xxe;</text:p></office:text></office:body></office:document-content>"
        )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(member, xml)

    document = normalize_document_bytes(stream.getvalue(), document_ref=f"hostile.{fmt}")
    assert document.extraction == "opaque"
    assert not any("should-not-be-extracted" in section.text for section in document.sections)
    assert any("DTD/entity" in limitation or "extraction failed" in limitation for limitation in document.limitations)


def test_unsafe_archive_path_is_fail_closed() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../escape.txt", b"unsafe")
    path = _write(tmp_path=None, payload=stream.getvalue())
    with pytest.raises(UnsafeDocumentArchiveError):
        ingest_document_catalog(path)
    with pytest.raises(UnsafeDocumentArchiveError):
        ingest_document_catalog(
            path,
            include_opaque_members=False,
            strict_archive_resource_limits=False,
        )


def test_pdf_extraction_uses_spawn_and_wall_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import auto_foundry_core.document_ingestion as ingestion
    from pypdf import PdfWriter

    stream = io.BytesIO()
    PdfWriter().write(stream)
    original_get_context = ingestion.multiprocessing.get_context
    methods: list[str | None] = []

    def recording_get_context(method: str | None = None):
        methods.append(method)
        return original_get_context(method)

    monkeypatch.setattr(ingestion.multiprocessing, "get_context", recording_get_context)
    result = normalize_document_bytes(
        stream.getvalue(),
        document_ref="bounded.pdf",
        pdf_timeout_seconds=0.0001,
    )
    assert methods and methods[0] == "spawn"
    assert result.extraction == "limited"
    assert any("wall-time" in limitation for limitation in result.limitations)


def test_catalog_keeps_all_admitted_document_metadata_beyond_planner_excerpt_cap(tmp_path: Path) -> None:
    archive_path = tmp_path / "many.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for index in range(140):
            archive.writestr(f"brief-{index:03d}.txt", f"statement {index}")
    catalog = ingest_document_catalog(archive_path, max_documents=4096, max_excerpt_bytes=128)
    assert len(catalog.documents) == 140
    projection = catalog.planner_payload(max_excerpts=8, max_excerpt_bytes=128)
    assert len(projection["documents"]) == 140
    assert sum(len(document["sections"]) for document in projection["documents"]) <= 8


def test_default_archive_entry_count_is_not_a_256_document_blocker(tmp_path: Path) -> None:
    archive_path = tmp_path / "over-256-documents.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(300):
            archive.writestr(f"brief-{index:03d}.txt", f"statement {index}")

    catalog = ingest_document_catalog(archive_path)

    assert len(catalog.documents) == 300
    assert catalog.limitations == ()


def test_default_archive_entry_count_is_unbounded_but_explicit_cap_remains(tmp_path: Path) -> None:
    archive_path = tmp_path / "over-legacy-entry-count.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(4097):
            archive.writestr(f"brief-{index:04d}.txt", b"x")

    catalog = ingest_document_catalog(archive_path)
    assert len(catalog.documents) == 4097
    with pytest.raises(UnsafeDocumentArchiveError, match="too many members"):
        ingest_document_catalog(archive_path, max_entries=2)


def test_blank_document_sections_are_omitted_from_catalog_and_planner_payload() -> None:
    document = normalize_document_bytes(b" \n\t\r\n", document_ref="blank.txt")

    assert document.sections == ()
    assert document.extraction == "limited"
    assert any("no extractable text" in limitation for limitation in document.limitations)
    catalog = DocumentCatalog((document,))
    projection = catalog.planner_payload()
    assert projection["documents"][0]["sections"] == []


def test_zip_catalog_streams_path_without_reading_outer_archive_into_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "path-backed.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("brief.txt", b"path-backed requirement")

    original_read_bytes = Path.read_bytes

    def reject_outer_read(self: Path) -> bytes:
        if self == archive_path:
            raise AssertionError("outer ZIP must be opened path-backed")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", reject_outer_read)
    catalog = ingest_document_catalog(archive_path)
    assert [document.document_ref for document in catalog.documents] == ["brief.txt"]


def test_corrupt_archive_member_fails_closed_without_empty_content_hash(tmp_path: Path) -> None:
    archive_path = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("brief.txt", b"verified source bytes")
    payload = bytearray(archive_path.read_bytes())
    marker = payload.find(b"verified source bytes")
    assert marker >= 0
    payload[marker] ^= 0x01
    archive_path.write_bytes(payload)

    with pytest.raises(UnsafeDocumentArchiveError, match="integrity check failed"):
        ingest_document_catalog(archive_path)


def test_admitted_data_room_does_not_reject_structured_or_large_document_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "mixed-data-room.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("erp_transactions.parquet", b"p" * 64)
        archive.writestr("communications.sqlite", b"s" * 64)
        archive.writestr("brief.md", b"business requirement")

    catalog = ingest_document_catalog(
        archive_path,
        max_member_bytes=8,
        max_total_bytes=8,
        include_opaque_members=False,
        strict_archive_resource_limits=False,
    )

    assert [document.document_ref for document in catalog.documents] == ["brief.md"]
    assert catalog.documents[0].extraction == "limited"
    assert catalog.documents[0].content_hash
    assert any("bounded extraction budget" in value for value in catalog.documents[0].limitations)


def test_catalog_aggregate_pdf_budget_limits_calls_and_keeps_limited_remainder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import auto_foundry_core.document_ingestion as ingestion

    calls: list[str] = []

    def fake_pdf(document_ref: str, data: bytes, **kwargs):
        calls.append(document_ref)
        return (
            (ingestion._section(document_ref, "pdf", {"page": 1}, document_ref),),
            (),
            "normalized",
        )

    monkeypatch.setattr(ingestion, "_normalize_pdf", fake_pdf)
    archive_path = tmp_path / "many-pdf.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for index in range(7):
            archive.writestr(f"brief-{index}.pdf", b"%PDF synthetic")
    catalog = ingest_document_catalog(
        archive_path,
        max_documents=128,
        max_parsed_pdfs=2,
        max_pdf_total_wall_seconds=60,
        max_pdf_total_output_bytes=1024,
    )
    assert calls == ["brief-0.pdf", "brief-1.pdf"]
    assert len(catalog.documents) == 7
    assert [document.extraction for document in catalog.documents[2:]] == ["limited"] * 5
    assert all(document.content_hash for document in catalog.documents[2:])


def test_non_zip_catalog_applies_normalized_text_budget(tmp_path: Path) -> None:
    source = tmp_path / "brief.txt"
    source.write_text("abcdefghij", encoding="utf-8")
    catalog = ingest_document_catalog(
        source,
        max_excerpt_bytes=1024,
        max_total_normalized_text_bytes=4,
    )
    document = catalog.documents[0]
    assert document.source_path == "brief.txt"
    assert document.content_hash
    assert sum(len(section.text.encode("utf-8")) for section in document.sections) <= 4
    assert document.extraction == "limited"
    assert any("normalized-text" in limitation for limitation in document.limitations)


def test_pdf_stubborn_child_is_killed_and_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    import auto_foundry_core.document_ingestion as ingestion

    class Connection:
        def close(self):
            return None

        def poll(self, _timeout):
            return False

    class StubbornProcess:
        def __init__(self, **_kwargs):
            self.alive = True
            self.terminated = False
            self.killed = False
            self.joins: list[float] = []
            self.closed = False

        def start(self):
            return None

        def close(self):
            self.closed = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.alive = False

        def join(self, timeout):
            self.joins.append(float(timeout))

    class Context:
        def __init__(self):
            self.process: StubbornProcess | None = None

        def Pipe(self, duplex=False):
            assert duplex is False
            return Connection(), Connection()

        def Process(self, **kwargs):
            self.process = StubbornProcess(**kwargs)
            return self.process

    context = Context()
    monkeypatch.setattr(ingestion.multiprocessing, "get_context", lambda method: context)
    result = normalize_document_bytes(
        b"not parsed by fake worker",
        document_ref="stubborn.pdf",
        pdf_timeout_seconds=0.001,
    )
    assert result.extraction == "limited"
    assert context.process is not None
    assert context.process.terminated is True
    assert context.process.killed is True
    assert context.process.alive is False
    assert context.process.closed is True


def _write(tmp_path: Path | None, payload: bytes) -> Path:
    # Keep the helper independent of pytest's fixture plumbing for concise
    # archive construction above.
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="document-ingestion-test-")) if tmp_path is None else tmp_path
    path = root / "unsafe.zip"
    path.write_bytes(payload)
    return path
