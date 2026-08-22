from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_foundry_core import (
    CanonicalMapping,
    IdentityDecision,
    IdentityMappingView,
    load_selected_source_ids,
    suggest_semantic_promotions,
)


def _decision(decision_id: str) -> IdentityDecision:
    return IdentityDecision(
        candidate_id=f"candidate:{decision_id}",
        decision="same_object",
        decision_id=decision_id,
        review_status="accepted",
        reviewer_ref="independent-reviewer",
    )


def test_identity_mapping_view_resolves_only_unique_reviewed_sources() -> None:
    view = IdentityMappingView.build(
        (
            CanonicalMapping("Customer:C-1", "Customer", ("erp:C-1", "portal:P-1"), "D-1"),
            CanonicalMapping("Customer:C-2", "Customer", ("erp:C-2", "shared"), "D-2"),
            CanonicalMapping("Customer:C-3", "Customer", ("erp:C-3", "shared"), "D-3"),
        ),
        (_decision("D-1"), _decision("D-2"), _decision("D-3")),
    )

    assert view.resolve("erp:C-1") == "Customer:C-1"
    assert view.resolve("shared") is None
    assert view.ambiguous_sources["shared"] == ("Customer:C-2", "Customer:C-3")
    assert IdentityMappingView.from_dict(view.to_dict()).content_hash == view.content_hash

    tampered = view.to_dict()
    tampered["source_to_canonical"]["erp:C-1"] = "Customer:C-3"
    with pytest.raises(ValueError, match="indexes do not match"):
        IdentityMappingView.from_dict(tampered)


def test_selected_source_ids_load_exact_persisted_values_without_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_map = tmp_path / "source_map.json"
    source_map.write_text(
        json.dumps(
            [
                {"source_id": "EMAIL-0046_AR-INV-000001.txt"},
                {"source_id": "EMAIL-0047_AR-INV-000002.txt"},
                {"source_id": "EMAIL-0046_AR-INV-000001.txt"},
            ],
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_FOUNDRY_SOURCE_MAP", str(source_map))
    assert load_selected_source_ids() == (
        "EMAIL-0046_AR-INV-000001.txt",
        "EMAIL-0047_AR-INV-000002.txt",
    )


def test_selected_source_ids_reject_malformed_non_array_map(tmp_path: Path) -> None:
    source_map = tmp_path / "source_map.json"
    source_map.write_text(json.dumps({"source_id": "EMAIL-0046_AR-INV-000001.txt"}), encoding="utf-8")

    with pytest.raises(ValueError, match="selected source map is invalid"):
        load_selected_source_ids(source_map)


def test_semantic_promotion_is_advisory_and_never_copies_current_values() -> None:
    facts = (
        {
            "observation_id": "REQ-01.late",
            "semantic_role": "current_observation_not_definition",
            "metric": "late deliveries",
            "unit": "deliveries",
            "value": 59,
        },
        {
            "observation_id": "REQ-04.late",
            "semantic_role": "current_observation_not_definition",
            "metric": "late deliveries",
            "unit": "deliveries",
            "value": 325,
        },
    )
    suggestions = suggest_semantic_promotions(facts)
    assert len(suggestions) == 1
    assert suggestions[0].candidate_kind == "metric_definition"
    assert suggestions[0].status == "advisory"
    assert "value" not in suggestions[0].to_dict()
