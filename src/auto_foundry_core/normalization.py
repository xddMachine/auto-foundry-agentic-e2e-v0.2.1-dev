"""Provenance-preserving, explicit normalization primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class ParseResult:
    raw: Any
    value: Any = None
    ok: bool = True
    error: str | None = None
    kind: str = "value"

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "value": self.value, "ok": self.ok, "error": self.error, "kind": self.kind}


_CURRENCY_MARKS = re.compile(r"^[\s\$€£¥₹]+|[\s\$€£¥₹]+$")


def _failed(raw: Any, kind: str, message: str) -> ParseResult:
    return ParseResult(raw=raw, value=None, ok=False, error=message, kind=kind)


def parse_date(value: Any, *, dayfirst: bool = False) -> ParseResult:
    """Parse common date representations and return an explicit failure."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return _failed(value, "date", "blank value")
    if isinstance(value, datetime):
        return ParseResult(value, value.date().isoformat(), True, None, "date")
    if isinstance(value, date):
        return ParseResult(value, value.isoformat(), True, None, "date")
    text = str(value).strip()
    try:
        parsed: datetime | date
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            from dateutil import parser as date_parser
            parsed = date_parser.parse(text, dayfirst=dayfirst, fuzzy=False)
        output = parsed.date().isoformat() if isinstance(parsed, datetime) else parsed.isoformat()
        return ParseResult(value, output, True, None, "date")
    except Exception as exc:
        return _failed(value, "date", f"unparseable date: {exc}")


def parse_number(value: Any) -> ParseResult:
    """Parse a finite decimal number, preserving the original representation."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return _failed(value, "number", "blank value")
    if isinstance(value, bool):
        return _failed(value, "number", "boolean is not a number")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return _failed(value, "number", "number is not finite")
        return ParseResult(value, value, True, None, "number")
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = _CURRENCY_MARKS.sub("", text).replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return _failed(value, "number", "unparseable number")
    if not math.isfinite(number):
        return _failed(value, "number", "number is not finite")
    return ParseResult(value, -number if negative else number, True, None, "number")


def normalize_string(
    value: Any,
    *,
    case: str | None = "lower",
    unicode_form: str = "NFKC",
    collapse_whitespace: bool = True,
) -> Any:
    if value is None:
        return None
    text = unicodedata.normalize(unicode_form, str(value))
    if collapse_whitespace:
        text = " ".join(text.split())
    if case == "lower":
        text = text.lower()
    elif case == "upper":
        text = text.upper()
    elif case not in {None, "preserve"}:
        raise ValueError("case must be lower, upper, preserve, or None")
    return text


def normalize_identifier(value: Any, *, case: str | None = "lower") -> Any:
    """Normalize safe presentation noise without asserting object identity."""

    text = normalize_string(value, case=case)
    if text is None:
        return None
    # Delimiters are made comparable, but no punctuation is discarded and no
    # identity conclusion is drawn from the resulting value.
    return re.sub(r"[\s_/\\]+", "-", text).strip("-")


def normalize_value(value: Any, *, kind: str = "string", case: str | None = "lower", **options: Any) -> Any:
    if kind in {"string", "text"}:
        return normalize_string(value, case=case, **{k: v for k, v in options.items() if k in {"unicode_form", "collapse_whitespace"}})
    if kind in {"identifier", "id"}:
        return normalize_identifier(value, case=case)
    if kind in {"date", "datetime"}:
        return parse_date(value, dayfirst=bool(options.get("dayfirst", False))).value
    if kind in {"number", "numeric", "decimal"}:
        return parse_number(value).value
    if kind == "currency":
        return normalize_string(value, case="upper")
    if kind in {"unit", "units"}:
        return normalize_string(value, case="preserve")
    raise ValueError(f"unsupported normalization kind: {kind}")


def normalize_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    fields: Mapping[str, str] | Sequence[str] | None = None,
    case: str | None = "lower",
    suffix: str = "_normalized",
    error_suffix: str = "_parse_error",
    return_metadata: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Add normalized columns while retaining every raw column unchanged."""

    materialized = [dict(row) for row in rows]
    if fields is None:
        kinds: dict[str, str] = {key: "string" for row in materialized for key in row}
    elif isinstance(fields, Mapping):
        kinds = {str(k): str(v) for k, v in fields.items()}
    else:
        kinds = {str(k): "string" for k in fields}
    lineage: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for row_number, row in enumerate(materialized):
        result = dict(row)
        for name, kind in kinds.items():
            if name not in row:
                continue
            raw = row[name]
            if kind in {"date", "datetime"}:
                parsed = parse_date(raw)
            elif kind in {"number", "numeric", "decimal"}:
                parsed = parse_number(raw)
            else:
                parsed = ParseResult(raw, normalize_value(raw, kind=kind, case=case), True, None, kind)
            result[f"{name}{suffix}"] = parsed.value
            if not parsed.ok:
                result[f"{name}{error_suffix}"] = parsed.error
                failures.append({"row": row_number, "field": name, "raw": raw, "error": parsed.error})
            lineage.append({"row": row_number, "field": name, "raw": raw, "normalized": parsed.value, "kind": kind, "ok": parsed.ok})
        output.append(result)
    if not return_metadata:
        return output
    return {"rows": output, "lineage": lineage, "failures": failures, "raw_preserved": True}


normalize = normalize_rows

__all__ = ["ParseResult", "normalize", "normalize_identifier", "normalize_rows", "normalize_string", "normalize_value", "parse_date", "parse_number"]
