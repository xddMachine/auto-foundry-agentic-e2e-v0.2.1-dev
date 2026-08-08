from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_foundry_core.normalization import normalize_rows, parse_date, parse_number
from auto_foundry_core.profiling import profile_rows
from auto_foundry_core.artifacts import write_artifact, write_manifest
from auto_foundry_core.sources import discover, preview, read_rows, register_document, register_source
from auto_foundry_core.workspace import AllowedRootError


def test_source_registration_hash_preview_and_path_enforcement(tmp_path: Path):
    source = tmp_path / "objects.csv"
    source.write_text("object_id,label,amount\n1, Alpha ,10\n2,Beta,20\n", encoding="utf-8")
    ref = register_source(source, allowed_roots=(tmp_path,))
    assert ref.content_hash and len(ref.content_hash) == 64
    assert read_rows(ref, limit=1)[0]["object_id"] == "1"
    assert preview(ref, limit=1)["columns"] == ["object_id", "label", "amount"]
    assert discover(ref)[0].name == "objects"
    with pytest.raises(AllowedRootError):
        register_source(source, allowed_roots=(tmp_path / "other",))


def test_jsonl_excel_and_parquet_are_generic(tmp_path: Path):
    jsonl = tmp_path / "events.jsonl"
    jsonl.write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
    assert len(read_rows(jsonl)) == 2
    json_list = tmp_path / "records.json"
    json_list.write_text(json.dumps([{"id": 1}, {"id": 2}]), encoding="utf-8")
    assert len(read_rows(json_list)) == 2
    json_object = tmp_path / "object.json"
    json_object.write_text(json.dumps({"id": 1, "label": "one"}), encoding="utf-8")
    assert read_rows(json_object) == [{"id": 1, "label": "one"}]
    text = tmp_path / "notes.txt"
    text.write_text("first\nsecond\n", encoding="utf-8")
    document = register_document(text, allowed_roots=(tmp_path,))
    assert document.asset.content_hash
    assert read_rows(text, limit=1) == [{"text": "first"}]
    try:
        import openpyxl
    except ImportError:
        openpyxl = None
    if openpyxl:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "records"
        sheet.append(["id", "label"])
        sheet.append([1, "x"])
        xlsx = tmp_path / "records.xlsx"
        workbook.save(xlsx)
        assert discover(xlsx)[0].name == "records"
        assert read_rows(xlsx, table="records")[0]["id"] == 1
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        pa = pq = None
    if pa:
        parquet = tmp_path / "records.parquet"
        pq.write_table(pa.Table.from_pylist([{"id": 1}, {"id": 2}]), parquet)
        assert len(read_rows(parquet)) == 2


def test_profile_and_provenance_preserving_normalization():
    rows = [{"id": " A/1 ", "when": "2024-02-03", "amount": 1200}, {"id": "B", "when": "not-a-date", "amount": "x"}]
    profile = profile_rows(rows)
    assert profile["row_count"] == 2
    assert profile["columns"]["amount"]["suspicious_mixed_type"]
    result = normalize_rows(rows, fields={"id": "identifier", "when": "date", "amount": "number"}, return_metadata=True)
    assert result["rows"][0]["id"] == " A/1 "
    assert result["rows"][0]["id_normalized"] == "a-1"
    assert result["rows"][1]["when_parse_error"]
    assert len(result["failures"]) == 2
    assert parse_date("not-a-date").ok is False
    assert parse_number("(1,200)").value == -1200.0


def test_source_hash_is_immutable_and_artifact_roots_are_enforced(tmp_path: Path):
    source = tmp_path / "stable.csv"
    source.write_text("id\n1\n", encoding="utf-8")
    ref = register_source(source, allowed_roots=(tmp_path,))
    original = source.read_bytes()
    assert read_rows(ref) == [{"id": "1"}]
    assert source.read_bytes() == original
    source.write_text("id\n2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        read_rows(ref)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    write_artifact({"ok": True}, output_root / "result.json", allowed_roots=(output_root,))
    with pytest.raises(AllowedRootError):
        write_artifact({"ok": True}, tmp_path / "outside.json", allowed_roots=(output_root,))
    with pytest.raises(AllowedRootError):
        write_manifest(tmp_path / "outside-manifest.json", {"ok": True}, allowed_roots=(output_root,))
