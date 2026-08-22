"""Focused arbitrary-ontology projection checks for the offline renderer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "auto-foundry-agentic-e2e" / "scripts"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("dashboard_renderer_generic", SCRIPTS / "dashboard_renderer.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dashboard_renderer = _load_renderer()


def _fixture(*, groups: list[dict[str, object]] | None = None, relationships: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "dashboard_version": 4,
        "ontology_summary": {"ontology_items": 3, "relationships": 2},
        "ontology_objects": [
            {"id": "customer", "label": "Customer", "kind": "entity"},
            {"id": "order", "label": "Order", "kind": "transaction"},
            {"id": "delivery", "label": "Delivery", "kind": "event"},
        ],
        "ontology_relationships": relationships if relationships is not None else [
            {"source": "customer", "target": "order", "label": "places"},
            {"source": "order", "target": "delivery", "label": "ships as"},
        ],
        **({"ontology_groups": groups} if groups is not None else {}),
    }


def test_arbitrary_graph_is_deterministic_and_uses_neutral_lane_when_groups_absent() -> None:
    fixture = _fixture()
    first = dashboard_renderer._ontology_body(fixture)
    second = dashboard_renderer._ontology_body(json.loads(json.dumps(fixture)))
    assert first == second
    assert first.count('class="ontology-node"') == 3
    assert first.count('class="ontology-edge"') == 2
    assert first.count('class="ontology-lane"') == 1
    assert "Unassigned / supplied objects" in first
    assert "3 supplied objects and 2 explicit links" in first


def test_arbitrary_graph_validates_endpoints_duplicates_and_group_partition() -> None:
    groups = [
        {"id": "commercial", "label": "Commercial", "node_ids": ["customer", "order"]},
        {"id": "operations", "label": "Operations", "node_ids": ["delivery"]},
    ]
    rendered = dashboard_renderer._ontology_body(_fixture(groups=groups))
    assert rendered.count('class="ontology-node"') == 3
    assert rendered.count('class="ontology-lane"') == 2

    duplicate_node = _fixture()
    duplicate_node["ontology_objects"] = [*duplicate_node["ontology_objects"], {"id": "order", "label": "Order copy", "kind": "transaction"}]
    with pytest.raises(ValueError, match="unique object ids"):
        dashboard_renderer._ontology_body(duplicate_node)

    unknown_endpoint = _fixture(relationships=[{"source": "customer", "target": "missing", "label": "places"}])
    with pytest.raises(ValueError, match="unknown object"):
        dashboard_renderer._ontology_body(unknown_endpoint)

    duplicate_edge = _fixture(relationships=[
        {"source": "customer", "target": "order", "label": "places"},
        {"source": "customer", "target": "order", "label": "places"},
    ])
    with pytest.raises(ValueError, match="unique links"):
        dashboard_renderer._ontology_body(duplicate_edge)

    overlapping_groups = _fixture(groups=[
        {"id": "a", "label": "A", "node_ids": ["customer", "order"]},
        {"id": "b", "label": "B", "node_ids": ["order", "delivery"]},
    ])
    with pytest.raises(ValueError, match="partition"):
        dashboard_renderer._ontology_body(overlapping_groups)


def test_arbitrary_empty_relationships_and_no_projection_are_truthful() -> None:
    no_edges = _fixture(relationships=[])
    rendered = dashboard_renderer._ontology_body(no_edges)
    assert rendered.count('class="ontology-node"') == 3
    assert 'class="ontology-edge"' not in rendered
    assert "No explicit relationships were supplied." in rendered

    summary_only = dashboard_renderer._ontology_body({
        "dashboard_version": 4,
        "ontology_summary": {"ontology_items": 7, "relationships": 11},
    })
    assert 'class="ontology-node"' not in summary_only
    assert "7" in summary_only and "11" in summary_only
    assert "No ontology node summary was supplied" in summary_only
