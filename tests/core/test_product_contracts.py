"""Focused tests for the shared reviewed-product contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from auto_foundry_core.product_contracts import (
    FREEZE_MARKER_FIELDS,
    FORBIDDEN_FREEZE_SIBLINGS,
    FreezeMarkers,
    ProductContractError,
    decode_freeze_markers,
    validate_product_manifest,
)


def _markers(**overrides: object) -> dict[str, object]:
    result = {field_name: True for field_name in FREEZE_MARKER_FIELDS}
    result.update(overrides)
    return result


def test_freeze_markers_round_trip_is_frozen_and_exact() -> None:
    decoded = decode_freeze_markers(_markers())

    assert isinstance(decoded, FreezeMarkers)
    assert decoded.all_frozen is True
    assert decoded.to_dict() == _markers()
    assert tuple(decoded.to_dict()) == FREEZE_MARKER_FIELDS
    with pytest.raises(FrozenInstanceError):
        decoded.answers_frozen = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        {**_markers(), "unexpected": True},
        {key: value for key, value in _markers().items() if key != "telemetry_frozen"},
        _markers(prepared_data_registry_frozen=1),
    ],
)
def test_freeze_markers_decoder_rejects_malformed_wire_values(value: object) -> None:
    with pytest.raises(ProductContractError):
        decode_freeze_markers(value)


def test_freeze_markers_require_all_true_is_separate_from_shape_validation() -> None:
    decoded = decode_freeze_markers(_markers(telemetry_frozen=False))
    assert decoded.all_frozen is False

    with pytest.raises(ProductContractError, match="telemetry_frozen"):
        decode_freeze_markers(_markers(telemetry_frozen=False), require_all=True)


@pytest.mark.parametrize(
    "legacy",
    [
        {
            "answers_frozen": True,
            "living_enterprise_model_frozen": True,
            "prepared_assets_frozen": True,
            "dashboard_frozen": True,
            "telemetry_frozen": True,
        },
        {"freeze": _markers()},
        {"preconditions": _markers()},
        {"product_freeze": _markers()},
        {"frozen_products": _markers()},
    ],
)
def test_legacy_marker_containers_are_not_decoded(legacy: dict[str, object]) -> None:
    with pytest.raises(ProductContractError):
        decode_freeze_markers(legacy.get("freeze_markers"))


@pytest.mark.parametrize("sibling", FORBIDDEN_FREEZE_SIBLINGS)
def test_product_manifest_rejects_canonical_with_every_forbidden_sibling(sibling: str) -> None:
    value: dict[str, object] = {"freeze_markers": _markers(), "unrelated": "allowed"}
    value[sibling] = _markers() if sibling in {"freeze", "preconditions", "product_freeze", "freeze_manifest", "frozen_products"} else True

    with pytest.raises(ProductContractError, match=sibling):
        validate_product_manifest(value)


@pytest.mark.parametrize("sibling", FORBIDDEN_FREEZE_SIBLINGS)
def test_product_manifest_rejects_every_forbidden_sibling_without_canonical(sibling: str) -> None:
    value: dict[str, object] = {"unrelated": "allowed"}
    value[sibling] = _markers() if sibling in {"freeze", "preconditions", "product_freeze", "freeze_manifest", "frozen_products"} else True

    with pytest.raises(ProductContractError, match=sibling):
        validate_product_manifest(value)


def test_product_manifest_allows_unrelated_fields_with_canonical_markers() -> None:
    value = {
        "freeze_markers": _markers(),
        "run_id": "RUN-TEST",
        "review_routing": {"fresh_sol_review_available": False},
    }

    assert validate_product_manifest(value).all_frozen is True
