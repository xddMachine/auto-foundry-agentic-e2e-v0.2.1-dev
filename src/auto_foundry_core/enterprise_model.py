"""Run-local Living Enterprise Model: extensible ontology plus prepared registry."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping

from .contracts import (
    CanonicalMapping,
    DataAssetRef,
    KnowledgeDelta,
    OntologyItem,
    PreparedAssetDescriptor,
)


_FORBIDDEN_ONTOLOGY_KEYS = frozenset({
    "reviewer", "reviewer_id", "reviewer_note", "reviewed_by", "lifecycle",
    "lifecycle_state", "run_state", "parser", "parser_metadata", "queue_state",
})


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items() if str(k).lower() not in _FORBIDDEN_ONTOLOGY_KEYS}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_clean(v) for v in value)
    return value


class LivingEnterpriseModel:
    """A bounded in-memory model intended to be exported at run end."""

    def __init__(self, *, run_id: str = "run") -> None:
        self.run_id = run_id
        self.ontology: dict[str, OntologyItem] = {}
        self.prepared_assets: dict[str, PreparedAssetDescriptor] = {}
        self.canonical_mappings: dict[str, CanonicalMapping] = {}
        self.relationships: dict[str, dict[str, Any]] = {}
        self.knowledge: dict[str, dict[str, Any]] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.revisions: list[dict[str, Any]] = []

    @property
    def ontology_index(self) -> list[dict[str, Any]]:
        return [
            {"item_id": item.item_id, "item_type": item.item_type, "label": item.label, "scope": item.scope, "status": item.status}
            for item in sorted(self.ontology.values(), key=lambda item: item.item_id)
        ]

    def add_ontology_item(self, item: OntologyItem | Mapping[str, Any]) -> OntologyItem:
        item = item if isinstance(item, OntologyItem) else OntologyItem.from_dict(_clean(item))
        if item.item_id in self.ontology:
            raise ValueError(f"ontology item already exists: {item.item_id}")
        self.ontology[item.item_id] = item
        return item

    def extend_ontology_item(self, item_id: str, patch: Mapping[str, Any]) -> OntologyItem:
        if item_id not in self.ontology:
            raise KeyError(item_id)
        current = self.ontology[item_id]
        clean = _clean(patch)
        properties = dict(current.properties)
        properties.update(clean.pop("properties", {}))
        values = {"properties": properties, **clean}
        updated = replace(current, **values)
        self.ontology[item_id] = updated
        return updated

    def register_prepared_asset(self, asset: PreparedAssetDescriptor | Mapping[str, Any]) -> PreparedAssetDescriptor:
        asset = asset if isinstance(asset, PreparedAssetDescriptor) else PreparedAssetDescriptor.from_dict(asset)
        existing = self.prepared_assets.get(asset.prepared_asset_id)
        if existing is not None and existing != asset:
            raise ValueError(f"prepared asset already exists with different descriptor: {asset.prepared_asset_id}")
        self.prepared_assets[asset.prepared_asset_id] = asset
        return asset

    def add_mapping(self, mapping: CanonicalMapping | Mapping[str, Any]) -> CanonicalMapping:
        mapping = mapping if isinstance(mapping, CanonicalMapping) else CanonicalMapping.from_dict(mapping)
        self.canonical_mappings[mapping.canonical_id] = mapping
        return mapping

    def relevant_bundle(
        self,
        ontology_ids: Iterable[str],
        *,
        prepared_asset_ids: Iterable[str] = (),
        mapping_ids: Iterable[str] = (),
        relationship_ids: Iterable[str] = (),
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """Resolve exact IDs into a bounded bundle; no keyword routing occurs."""

        ontology_ids = tuple(str(v) for v in ontology_ids)
        prepared_asset_ids = tuple(str(v) for v in prepared_asset_ids)
        mapping_ids = tuple(str(v) for v in mapping_ids)
        relationship_ids = tuple(str(v) for v in relationship_ids)
        missing = {
            "ontology": [key for key in ontology_ids if key not in self.ontology],
            "prepared_assets": [key for key in prepared_asset_ids if key not in self.prepared_assets],
            "mappings": [key for key in mapping_ids if key not in self.canonical_mappings],
            "relationships": [key for key in relationship_ids if key not in self.relationships],
        }
        if any(missing.values()):
            raise KeyError(f"unknown exact IDs: {missing}")
        if max_items is not None and max_items < 0:
            raise ValueError("max_items cannot be negative")
        selected_ontology = list(ontology_ids)
        selected_assets = list(prepared_asset_ids)
        if max_items is not None:
            selected_ontology = selected_ontology[:max_items]
            remaining = max(0, max_items - len(selected_ontology))
            selected_assets = selected_assets[:remaining]
        return {
            "ontology": [self.ontology[key].to_dict() for key in selected_ontology],
            "prepared_assets": [self.prepared_assets[key].to_dict() for key in selected_assets],
            "mappings": [self.canonical_mappings[key].to_dict() for key in mapping_ids],
            "relationships": [self.relationships[key] for key in relationship_ids],
            "exact_ids": {"ontology": selected_ontology, "prepared_assets": selected_assets, "mappings": list(mapping_ids), "relationships": list(relationship_ids)},
        }

    def apply_delta(self, delta: KnowledgeDelta | Mapping[str, Any], *, accepted: bool | None = None) -> dict[str, Any]:
        """Apply one accepted delta atomically, preserving conflicts/supersession."""

        delta = delta if isinstance(delta, KnowledgeDelta) else KnowledgeDelta.from_dict(delta)
        if accepted is None:
            accepted = delta.accepted
        if not accepted:
            return {"applied": False, "delta_id": delta.delta_id, "operation": delta.operation}
        snapshot = (deepcopy(self.ontology), deepcopy(self.prepared_assets), deepcopy(self.canonical_mappings), deepcopy(self.relationships), deepcopy(self.knowledge), deepcopy(self.conflicts), deepcopy(self.revisions))
        try:
            payload = _clean(dict(delta.payload))
            operation = delta.operation
            if operation == "add_ontology_item":
                self.add_ontology_item(payload)
            elif operation == "extend_ontology_item":
                item_id = str(payload.pop("item_id"))
                self.extend_ontology_item(item_id, payload)
            elif operation == "add_prepared_asset":
                self.register_prepared_asset(payload)
            elif operation == "extend_prepared_asset":
                asset_id = str(payload.pop("prepared_asset_id"))
                if asset_id not in self.prepared_assets:
                    raise KeyError(asset_id)
                current = self.prepared_assets[asset_id]
                merged = current.to_dict()
                merged.update(payload)
                self.prepared_assets[asset_id] = PreparedAssetDescriptor.from_dict(merged)
            elif operation == "add_canonical_mapping":
                self.add_mapping(payload)
            elif operation in {"add_relationship", "add_metric", "add_definition", "add_rule", "add_process"}:
                key = str(payload.get("relationship_id") or payload.get("item_id") or payload.get("id") or hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16])
                self.relationships[key] = payload if operation == "add_relationship" else {"kind": operation[4:], **payload}
            elif operation == "add_alias":
                key = str(payload.get("canonical_id"))
                mapping = self.canonical_mappings.get(key)
                if mapping is None:
                    raise KeyError(key)
                aliases = tuple(dict.fromkeys((*mapping.aliases, *payload.get("aliases", ()), str(payload.get("alias")) if payload.get("alias") is not None else "")))
                self.canonical_mappings[key] = replace(mapping, aliases=tuple(alias for alias in aliases if alias))
            elif operation in {"record_limitation", "record_conflict"}:
                self.conflicts.append({"delta_id": delta.delta_id, "operation": operation, "payload": payload, "evidence_refs": list(delta.evidence_refs)})
            elif operation == "supersede":
                for item_id in delta.supersedes or tuple(payload.get("item_ids", ())):
                    if item_id in self.ontology:
                        self.ontology[item_id] = replace(self.ontology[item_id], status="superseded")
                    if item_id in self.prepared_assets:
                        self.prepared_assets[item_id] = replace(self.prepared_assets[item_id], status="superseded")
            elif operation == "no_change":
                pass
            self.knowledge[delta.delta_id] = {"operation": operation, "payload": payload, "evidence_refs": list(delta.evidence_refs)}
            self.revisions.append({"delta_id": delta.delta_id, "operation": operation, "applied_at": datetime.now(timezone.utc).isoformat()})
            return {"applied": True, "delta_id": delta.delta_id, "operation": operation}
        except Exception:
            self.ontology, self.prepared_assets, self.canonical_mappings, self.relationships, self.knowledge, self.conflicts, self.revisions = snapshot
            raise

    def export(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ontology": [item.to_dict() for item in sorted(self.ontology.values(), key=lambda item: item.item_id)],
            "ontology_index": self.ontology_index,
            "prepared_assets": [asset.to_dict() for asset in sorted(self.prepared_assets.values(), key=lambda asset: asset.prepared_asset_id)],
            "canonical_mappings": [mapping.to_dict() for mapping in sorted(self.canonical_mappings.values(), key=lambda mapping: mapping.canonical_id)],
            "relationships": {key: self.relationships[key] for key in sorted(self.relationships)},
            "conflicts": deepcopy(self.conflicts),
            "revisions": deepcopy(self.revisions),
        }


__all__ = ["LivingEnterpriseModel"]
