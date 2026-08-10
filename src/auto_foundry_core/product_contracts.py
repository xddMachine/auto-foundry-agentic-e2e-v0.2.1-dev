"""Strict contracts shared by the reviewed products and optimizer helpers.

The product pipeline has one authority for run-freeze state.  Keeping the
decoder here prevents the dashboard and the optimizer from quietly accepting
different legacy marker containers or coercing values that only look boolean.
This module validates structure only; it does not inspect or recalculate
analytical content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


class ProductContractError(ValueError):
    """Raised when a reviewed-product contract is malformed."""


FREEZE_MARKER_FIELDS: tuple[str, ...] = (
    "answers_frozen",
    "living_enterprise_model_frozen",
    "prepared_data_registry_frozen",
    "dashboard_frozen",
    "telemetry_frozen",
)

# These names are rejected when they appear beside the canonical nested
# object.  The canonical field names are included as well: a manifest/fixture
# must have one authoritative container, even when a sibling happens to use a
# current spelling rather than an older alias.
FORBIDDEN_FREEZE_SIBLINGS: tuple[str, ...] = (
    *FREEZE_MARKER_FIELDS,
    "lem_frozen",
    "prepared_assets_frozen",
    "freeze",
    "preconditions",
    "product_freeze",
    "freeze_manifest",
    "frozen_products",
)


@dataclass(frozen=True)
class FreezeMarkers:
    """The canonical nested freeze marker object.

    Every marker is deliberately required to be an actual :class:`bool`.
    ``bool`` is checked by identity rather than ``isinstance`` so integers
    such as ``1`` cannot be accepted as frozen state by accident.
    """

    answers_frozen: bool
    living_enterprise_model_frozen: bool
    prepared_data_registry_frozen: bool
    dashboard_frozen: bool
    telemetry_frozen: bool

    FIELDS: ClassVar[tuple[str, ...]] = FREEZE_MARKER_FIELDS

    def __post_init__(self) -> None:
        for field_name in self.FIELDS:
            value = getattr(self, field_name)
            if type(value) is not bool:
                raise ProductContractError(f"freeze_markers.{field_name} must be a boolean")

    @property
    def all_frozen(self) -> bool:
        """Whether every required product marker is true."""

        return all(getattr(self, field_name) is True for field_name in self.FIELDS)

    def to_dict(self) -> dict[str, bool]:
        """Return the exact canonical wire representation."""

        return {field_name: getattr(self, field_name) for field_name in self.FIELDS}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, require_all: bool = False) -> "FreezeMarkers":
        """Decode the canonical JSON object form."""

        return cls.from_mapping(value, require_all=require_all)

    @classmethod
    def from_mapping(cls, value: Any, *, require_all: bool = False) -> "FreezeMarkers":
        """Decode one canonical marker mapping without aliases or coercion."""

        if not isinstance(value, Mapping):
            raise ProductContractError("freeze_markers must be an object")
        actual = set(value)
        expected = set(cls.FIELDS)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing:
            raise ProductContractError(f"freeze_markers is missing fields: {', '.join(missing)}")
        if extra:
            raise ProductContractError(f"freeze_markers has unsupported fields: {', '.join(extra)}")
        markers = cls(**{field_name: value[field_name] for field_name in cls.FIELDS})
        if require_all and not markers.all_frozen:
            false_fields = [field_name for field_name in cls.FIELDS if getattr(markers, field_name) is False]
            raise ProductContractError(
                "all freeze_markers must be true before optimizer collection: "
                + ", ".join(false_fields)
            )
        return markers


def decode_freeze_markers(value: Any, *, require_all: bool = False) -> FreezeMarkers:
    """Decode canonical ``freeze_markers`` and optionally require all true."""

    return FreezeMarkers.from_mapping(value, require_all=require_all)


def validate_freeze_markers(value: Any, *, require_all: bool = False) -> FreezeMarkers:
    """Validate and return canonical freeze markers.

    This named wrapper keeps call sites explicit while sharing exactly the
    same decoder and error semantics.
    """

    return decode_freeze_markers(value, require_all=require_all)


def validate_product_manifest(value: Any, *, require_all: bool = False) -> FreezeMarkers:
    """Validate one whole product manifest/fixture freeze boundary.

    The canonical marker object is the only accepted freeze authority. Other
    manifest fields remain valid and are intentionally ignored here; only
    known marker aliases/containers are rejected when they coexist with, or
    replace, ``freeze_markers``.
    """

    if not isinstance(value, Mapping):
        raise ProductContractError("product manifest/fixture must be an object")
    forbidden = sorted(set(value).intersection(FORBIDDEN_FREEZE_SIBLINGS))
    if forbidden:
        raise ProductContractError(
            "freeze_markers must be the only freeze authority; unsupported "
            "sibling fields: " + ", ".join(forbidden)
        )
    return decode_freeze_markers(value.get("freeze_markers"), require_all=require_all)


__all__ = [
    "FreezeMarkers",
    "FREEZE_MARKER_FIELDS",
    "FORBIDDEN_FREEZE_SIBLINGS",
    "ProductContractError",
    "decode_freeze_markers",
    "validate_freeze_markers",
    "validate_product_manifest",
]
