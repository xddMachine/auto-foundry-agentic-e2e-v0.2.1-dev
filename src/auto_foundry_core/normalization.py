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
    attempts: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "value": self.value,
            "ok": self.ok,
            "error": self.error,
            "kind": self.kind,
            "attempts": list(self.attempts),
            "failures": list(self.failures),
        }


_CURRENCY_MARKS = re.compile(r"^[\s\$€£¥₹]+|[\s\$€£¥₹]+$")


def _failed(
    raw: Any,
    kind: str,
    message: str,
    *,
    attempts: Sequence[str] = (),
    failures: Sequence[str] | None = None,
) -> ParseResult:
    return ParseResult(
        raw=raw,
        value=None,
        ok=False,
        error=message,
        kind=kind,
        attempts=tuple(str(item) for item in attempts),
        failures=tuple(str(item) for item in (failures if failures is not None else (message,))),
    )


def _format_sequence(formats: Sequence[str] | str | None) -> tuple[str, ...]:
    if formats is None:
        return ()
    if isinstance(formats, str):
        return (formats,)
    return tuple(str(fmt) for fmt in formats)


def parse_date(
    value: Any,
    *,
    formats: Sequence[str] | str | None = (),
    dayfirst: bool = False,
) -> ParseResult:
    """Parse ISO dates first, then caller-supplied formats.

    No ambient date parser is consulted.  ``attempts`` records the ISO/native
    and explicit ``strptime`` formats tried; ``failures`` records each failed
    attempt so an agent can choose an unambiguous format on a subsequent pass.
    ``dayfirst`` is retained only as an explicit convenience that adds the two
    corresponding numeric formats; it never enables fuzzy parsing.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        return _failed(value, "date", "blank value")
    if isinstance(value, datetime):
        return ParseResult(value, value.date().isoformat(), True, None, "date", ("native datetime",), ())
    if isinstance(value, date):
        return ParseResult(value, value.isoformat(), True, None, "date", ("native date",), ())

    text = str(value).strip()
    attempts: list[str] = ["ISO-8601"]
    failures: list[str] = []
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return ParseResult(value, parsed.date().isoformat(), True, None, "date", tuple(attempts), tuple(failures))
    except ValueError as exc:
        failures.append(f"ISO-8601: {exc}")

    requested_formats = list(_format_sequence(formats))
    if dayfirst:
        # This is still an explicit caller choice, not locale-dependent
        # guessing.  Caller-provided formats remain first in their declared
        # order and the convenience formats are tried afterwards.
        requested_formats.extend(fmt for fmt in ("%d/%m/%Y", "%d-%m-%Y") if fmt not in requested_formats)
    for fmt in requested_formats:
        attempts.append(fmt)
        try:
            parsed = datetime.strptime(text, fmt)
            return ParseResult(value, parsed.date().isoformat(), True, None, "date", tuple(attempts), tuple(failures))
        except (TypeError, ValueError) as exc:
            failures.append(f"{fmt}: {exc}")

    message = f"unparseable date: {text}"
    return _failed(value, "date", message, attempts=attempts, failures=failures)


def observation_as_of(values: Iterable[Any]) -> str | None:
    """Return the latest valid *observed* date supplied by the caller.

    Planned, due, target, forecast, and policy dates are intentionally not a
    second argument and cannot silently extend the evidence window.  A caller
    that has no observed timestamp receives ``None`` and must disclose that
    limitation instead of substituting a future obligation date.
    """

    parsed = {
        result.value
        for value in values
        for result in (parse_date(value),)
        if result.ok and isinstance(result.value, str)
    }
    return max(parsed) if parsed else None


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
        return normalize_string(value, case=case, **{key: option for key, option in options.items() if key in {"unicode_form", "collapse_whitespace"}})
    if kind in {"identifier", "id"}:
        return normalize_identifier(value, case=case)
    if kind in {"date", "datetime"}:
        date_formats = options.get("formats", options.get("date_formats", ()))
        return parse_date(value, formats=date_formats, dayfirst=bool(options.get("dayfirst", False))).value
    if kind in {"number", "numeric", "decimal"}:
        return parse_number(value).value
    if kind == "currency":
        return normalize_string(value, case="upper")
    if kind in {"unit", "units"}:
        return normalize_string(value, case="preserve")
    raise ValueError(f"unsupported normalization kind: {kind}")


def _field_kind_and_formats(
    name: str,
    specification: Any,
    date_formats: Mapping[str, Sequence[str]] | Sequence[str] | str | None,
    formats: Mapping[str, Sequence[str]] | Sequence[str] | str | None,
) -> tuple[str, Sequence[str] | str | None]:
    if isinstance(specification, Mapping):
        kind = str(specification.get("kind", specification.get("type", "string")))
        local_formats = specification.get("formats", specification.get("date_formats"))
    else:
        kind = str(specification)
        local_formats = None
    selected = local_formats
    global_formats = formats if formats is not None else date_formats
    if selected is None and isinstance(global_formats, Mapping):
        selected = global_formats.get(name)
    elif selected is None:
        selected = global_formats
    return kind, selected


def normalize_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    fields: Mapping[str, Any] | Sequence[str] | None = None,
    case: str | None = "lower",
    suffix: str = "_normalized",
    error_suffix: str = "_parse_error",
    date_formats: Mapping[str, Sequence[str]] | Sequence[str] | str | None = None,
    formats: Mapping[str, Sequence[str]] | Sequence[str] | str | None = None,
    return_metadata: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Add normalized columns while retaining every raw column unchanged.

    ``date_formats``/``formats`` can be a sequence applied to all date fields,
    a field-to-sequence mapping, or a field specification can carry its own
    ``{"kind": "date", "formats": (...)}`` mapping.
    """

    materialized = [dict(row) for row in rows]
    if fields is None:
        specifications: dict[str, Any] = {key: "string" for row in materialized for key in row}
    elif isinstance(fields, Mapping):
        specifications = {str(key): value for key, value in fields.items()}
    else:
        specifications = {str(key): "string" for key in fields}
    lineage: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for row_number, row in enumerate(materialized):
        result = dict(row)
        for name, specification in specifications.items():
            if name not in row:
                continue
            kind, field_formats = _field_kind_and_formats(name, specification, date_formats, formats)
            raw = row[name]
            if kind in {"date", "datetime"}:
                parsed = parse_date(raw, formats=field_formats)
            elif kind in {"number", "numeric", "decimal"}:
                parsed = parse_number(raw)
            else:
                parsed = ParseResult(raw, normalize_value(raw, kind=kind, case=case), True, None, kind)
            result[f"{name}{suffix}"] = parsed.value
            if not parsed.ok:
                result[f"{name}{error_suffix}"] = parsed.error
                failures.append(
                    {
                        "row": row_number,
                        "field": name,
                        "raw": raw,
                        "error": parsed.error,
                        "attempts": list(parsed.attempts),
                        "failures": list(parsed.failures),
                    }
                )
            lineage.append(
                {
                    "row": row_number,
                    "field": name,
                    "raw": raw,
                    "normalized": parsed.value,
                    "kind": kind,
                    "ok": parsed.ok,
                    "attempts": list(parsed.attempts),
                    "failures": list(parsed.failures),
                }
            )
        output.append(result)
    if not return_metadata:
        return output
    return {"rows": output, "lineage": lineage, "failures": failures, "raw_preserved": True}


normalize = normalize_rows

__all__ = ["ParseResult", "normalize", "normalize_identifier", "normalize_rows", "normalize_string", "normalize_value", "parse_date", "parse_number"]
