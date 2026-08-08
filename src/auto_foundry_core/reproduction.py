"""Result comparison and lifecycle-independent deterministic reproduction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import hash_value
from .contracts import DataAssetRef, OperationResultRef
from .references import decode_explicit_reference, is_explicit_reference_mapping
from .sources import hash_file
from .workspace import RunContext, require_allowed_roots, validate_allowed_path


def _fingerprint(value: Any, *, allowed_roots=None, context: RunContext | None = None) -> str:
    if isinstance(value, (list, tuple)):
        if any(_contains_file_ref(item) for item in value):
            return hash_value([_fingerprint(item, allowed_roots=allowed_roots, context=context) for item in value])
        return hash_value(value)
    if isinstance(value, Path):
        roots = require_allowed_roots(tuple(str(root) for root in context.read_roots) if context is not None else allowed_roots, context="reproduction path hashing")
        path = context.resolve_input(value) if context is not None else validate_allowed_path(value, roots)
        return hash_file(path, allowed_roots=roots)
    if isinstance(value, DataAssetRef):
        roots = require_allowed_roots(tuple(str(root) for root in context.read_roots) if context is not None else allowed_roots, context="reproduction source hashing")
        path = context.resolve_input(value.uri) if context is not None else value.uri
        return hash_file(path, allowed_roots=roots)
    if isinstance(value, OperationResultRef):
        roots = require_allowed_roots((str(context.run_root),) if context is not None else allowed_roots, context="reproduction result hashing")
        path = context.resolve_run_path(value.location) if context is not None else value.location
        return hash_file(path, allowed_roots=roots)
    if isinstance(value, Mapping):
        if is_explicit_reference_mapping(value):
            return _fingerprint(decode_explicit_reference(value), allowed_roots=allowed_roots, context=context)
        # An ordinary mapping is value data.  Recurse only when a nested value
        # is itself an actual/explicit reference; keys such as ``location``,
        # ``uri``, and ``content_hash`` never trigger filesystem access.
        if any(_contains_file_ref(item) for item in value.values()):
            return hash_value({key: _fingerprint(item, allowed_roots=allowed_roots, context=context) if _contains_file_ref(item) else item for key, item in value.items()})
    return hash_value(value)


def _contains_file_ref(value: Any) -> bool:
    if isinstance(value, Path):
        return True
    if isinstance(value, str):
        return False
    if isinstance(value, (DataAssetRef, OperationResultRef)):
        return True
    if isinstance(value, Mapping):
        if is_explicit_reference_mapping(value):
            decode_explicit_reference(value)  # validate malformed/unknown tags
            return True
        return any(_contains_file_ref(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_file_ref(item) for item in value)
    return False


def _is_file_reference_value(value: Any) -> bool:
    """Identify one top-level output reference for multi-output manifests."""

    if isinstance(value, (Path, DataAssetRef, OperationResultRef)):
        return True
    if isinstance(value, Mapping) and is_explicit_reference_mapping(value):
        decode_explicit_reference(value)  # validate malformed/unknown tags
        return True
    return False


def compare_results(expected: Any, actual: Any, *, allowed_roots=None, context: RunContext | None = None) -> dict[str, Any]:
    expected_hash = _fingerprint(expected, allowed_roots=allowed_roots, context=context)
    actual_hash = _fingerprint(actual, allowed_roots=allowed_roots, context=context)
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
    allowed_roots=None,
    context: RunContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Re-run a deterministic operation and compare against manifest outputs.

    ``operation`` receives the supplied args/kwargs and is deliberately
    independent of question/run lifecycle state.
    """

    actual = operation(*args, **kwargs)
    expected = manifest.get("output_hashes", [])
    if isinstance(actual, (Path, DataAssetRef, OperationResultRef)):
        actual_hashes = [_fingerprint(actual, allowed_roots=allowed_roots, context=context)]
    elif isinstance(actual, (list, tuple)) and actual and all(_is_file_reference_value(v) for v in actual):
        actual_hashes = [_fingerprint(v, allowed_roots=allowed_roots, context=context) for v in actual]
    else:
        actual_hashes = [_fingerprint(actual, allowed_roots=allowed_roots, context=context)]
    expected_hashes = [str(v) for v in expected]
    return {
        "reproduced": actual_hashes == expected_hashes,
        "expected_hashes": expected_hashes,
        "actual_hashes": actual_hashes,
        "comparison": compare_results(expected_hashes, actual_hashes, allowed_roots=allowed_roots, context=context),
    }


def reproduction_report(manifest: Mapping[str, Any], actual: Any, *, allowed_roots=None, context: RunContext | None = None) -> str:
    result = compare_results(manifest.get("output_hashes", []), actual, allowed_roots=allowed_roots, context=context)
    status = "PASS" if result["equal"] else "DIFFERENT"
    return f"reproduction={status} expected={result['expected_hash']} actual={result['actual_hash']}"


__all__ = ["compare_results", "reproduce", "reproduction_report"]
