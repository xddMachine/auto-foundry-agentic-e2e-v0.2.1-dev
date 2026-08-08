"""Result comparison and lifecycle-independent deterministic reproduction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import hash_value
from .sources import hash_file


def _fingerprint(value: Any) -> str:
    if isinstance(value, (str, Path)) and Path(value).is_file():
        return hash_file(value)
    if isinstance(value, Mapping) and "content_hash" in value:
        return str(value["content_hash"])
    return hash_value(value)


def compare_results(expected: Any, actual: Any) -> dict[str, Any]:
    expected_hash = _fingerprint(expected)
    actual_hash = _fingerprint(actual)
    equal = expected_hash == actual_hash
    report: dict[str, Any] = {"equal": equal, "expected_hash": expected_hash, "actual_hash": actual_hash}
    if not equal and isinstance(expected, Mapping) and isinstance(actual, Mapping):
        keys = sorted(set(expected) | set(actual))
        report["differences"] = [{"key": key, "expected": expected.get(key), "actual": actual.get(key)} for key in keys if expected.get(key) != actual.get(key)]
    return report


def reproduce(
    manifest: Mapping[str, Any],
    operation: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Re-run a deterministic operation and compare against manifest outputs.

    ``operation`` receives the supplied args/kwargs and is deliberately
    independent of question/run lifecycle state.
    """

    actual = operation(*args, **kwargs)
    expected = manifest.get("output_hashes", [])
    if isinstance(actual, (str, Path)) and Path(actual).is_file():
        actual_hashes = [_fingerprint(actual)]
    elif isinstance(actual, (list, tuple)) and actual and all(isinstance(v, (str, Path)) and Path(v).is_file() for v in actual):
        actual_hashes = [_fingerprint(v) for v in actual]
    else:
        actual_hashes = [_fingerprint(actual)]
    expected_hashes = [str(v) for v in expected]
    return {
        "reproduced": actual_hashes == expected_hashes,
        "expected_hashes": expected_hashes,
        "actual_hashes": actual_hashes,
        "comparison": compare_results(expected_hashes, actual_hashes),
    }


def reproduction_report(manifest: Mapping[str, Any], actual: Any) -> str:
    result = compare_results(manifest.get("output_hashes", []), actual)
    status = "PASS" if result["equal"] else "DIFFERENT"
    return f"reproduction={status} expected={result['expected_hash']} actual={result['actual_hash']}"


__all__ = ["compare_results", "reproduce", "reproduction_report"]
