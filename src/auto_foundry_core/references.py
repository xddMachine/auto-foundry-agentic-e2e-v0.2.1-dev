"""Explicit serialization helpers for the two local filesystem references.

The core deliberately does not infer a reference from ordinary mapping keys.
Only a mapping carrying :data:`REFERENCE_DISCRIMINATOR` is decoded here; all
other mappings remain ordinary analytical values.  The imports of the actual
contracts are kept inside the public helpers so this small module does not
create a contracts import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


REFERENCE_DISCRIMINATOR = "__auto_foundry_ref__"
DATA_ASSET_REFERENCE = "data_asset"
OPERATION_RESULT_REFERENCE = "operation_result"

_DATA_ASSET_FIELDS = frozenset({"uri", "format", "content_hash", "size_bytes", "metadata"})
_OPERATION_RESULT_FIELDS = frozenset({"location", "content_hash", "format", "rows", "metadata"})


def is_explicit_reference_mapping(value: Any) -> bool:
    """Return whether *value* carries the reserved reference discriminator.

    Presence, rather than validity, is intentional.  A malformed or unknown
    tag is still a reference-shaped value and must fail loudly when decoded;
    silently treating it as analytical data would recreate the ambiguity this
    contract removes.
    """

    return isinstance(value, Mapping) and REFERENCE_DISCRIMINATOR in value


def _payload(value: Mapping[str, Any], *, expected: str, fields: frozenset[str]) -> dict[str, Any]:
    tag = value.get(REFERENCE_DISCRIMINATOR)
    if tag != expected:
        raise TypeError(f"expected {expected!r} reference, got {tag!r}")
    payload = dict(value)
    payload.pop(REFERENCE_DISCRIMINATOR, None)
    unknown = sorted(set(payload) - fields, key=str)
    if unknown:
        raise ValueError(f"invalid {expected} reference field(s): {', '.join(map(str, unknown))}")
    required = "uri" if expected == DATA_ASSET_REFERENCE else "location"
    if required not in payload:
        raise ValueError(f"{expected} reference requires {required!r}")
    return payload


def decode_explicit_reference(value: Mapping[str, Any]) -> Any:
    """Decode one explicitly tagged mapping into its typed reference object.

    Unknown tags, missing required fields, invalid hashes, and incompatible
    extra fields raise a clear ``TypeError`` or ``ValueError``.  Untagged
    mappings are rejected here so callers cannot accidentally use this helper
    as a structural inference path.
    """

    if not isinstance(value, Mapping):
        raise TypeError("explicit reference must be a mapping")
    if REFERENCE_DISCRIMINATOR not in value:
        raise ValueError(f"missing {REFERENCE_DISCRIMINATOR!r} reference discriminator")
    tag = value.get(REFERENCE_DISCRIMINATOR)
    # Import lazily: contracts._jsonable delegates to this module while the
    # contracts module itself is still defining its dataclasses.
    from .contracts import DataAssetRef, OperationResultRef

    if tag == DATA_ASSET_REFERENCE:
        return DataAssetRef(**_payload(value, expected=DATA_ASSET_REFERENCE, fields=_DATA_ASSET_FIELDS))
    if tag == OPERATION_RESULT_REFERENCE:
        return OperationResultRef(**_payload(value, expected=OPERATION_RESULT_REFERENCE, fields=_OPERATION_RESULT_FIELDS))
    raise ValueError(f"unknown {REFERENCE_DISCRIMINATOR} value: {tag!r}")


def encode_explicit_reference(ref: Any) -> dict[str, Any]:
    """Encode one typed reference with the reserved discriminator."""

    from .contracts import DataAssetRef, OperationResultRef

    if isinstance(ref, DataAssetRef):
        payload = {
            "uri": ref.uri,
            "format": ref.format,
            "content_hash": ref.content_hash,
            "size_bytes": ref.size_bytes,
            "metadata": dict(ref.metadata),
        }
        return {REFERENCE_DISCRIMINATOR: DATA_ASSET_REFERENCE, **payload}
    if isinstance(ref, OperationResultRef):
        payload = {
            "location": ref.location,
            "content_hash": ref.content_hash,
            "format": ref.format,
            "rows": ref.rows,
            "metadata": dict(ref.metadata),
        }
        return {REFERENCE_DISCRIMINATOR: OPERATION_RESULT_REFERENCE, **payload}
    raise TypeError(f"cannot encode unsupported reference type: {type(ref).__name__}")


def is_data_asset_mapping(value: Any) -> bool:
    """Whether a mapping is a complete direct ``DataAssetRef`` structure.

    This predicate is for semantically typed source slots only.  It is not
    used by arbitrary-data comparison, reproduction, manifest, or runtime
    paths.  Explicit tagged mappings should be handled with
    :func:`decode_explicit_reference` instead.
    """

    return (
        isinstance(value, Mapping)
        and not is_explicit_reference_mapping(value)
        and "uri" in value
        and set(value).issubset(_DATA_ASSET_FIELDS)
    )


__all__ = [
    "DATA_ASSET_REFERENCE",
    "OPERATION_RESULT_REFERENCE",
    "REFERENCE_DISCRIMINATOR",
    "decode_explicit_reference",
    "encode_explicit_reference",
    "is_data_asset_mapping",
    "is_explicit_reference_mapping",
]
