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
        self.conflict_links: dict[str, set[str]] = {}
        self.supersession_links: dict[str, set[str]] = {}
        self.conflict_state: dict[str, dict[str, Any]] = {}
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
        scope: str | None = None,
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
        if scope is not None:
            mismatches = {
                "ontology": [key for key in ontology_ids if self.ontology[key].scope not in {None, scope}],
                "prepared_assets": [key for key in prepared_asset_ids if self.prepared_assets[key].scope not in {"reusable", scope}],
            }
            if any(mismatches.values()):
                raise ValueError(f"requested IDs are outside scope {scope!r}: {mismatches}")
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
        # Contract values are frozen and contain mapping proxies, which are
        # intentionally not deepcopy/pickleable.  Copy the registries by
        # identity and deep-copy only the mutable evidence containers.
        snapshot = (dict(self.ontology), dict(self.prepared_assets), dict(self.canonical_mappings), deepcopy(self.relationships), deepcopy(self.knowledge), deepcopy(self.conflicts), {key: set(value) for key, value in self.conflict_links.items()}, {key: set(value) for key, value in self.supersession_links.items()}, deepcopy(self.conflict_state), deepcopy(self.revisions))
        try:
            payload = _clean(dict(delta.payload))
            operation = delta.operation
            conflict_targets = tuple(str(value) for value in (delta.conflicts_with or payload.get("conflicts_with", ())))
            # ``delta.supersedes`` addresses prior knowledge records.  Payload
            # item IDs address current ontology/prepared registries; keeping
            # these domains separate prevents an ontology ID from being
            # mistaken for a delta ID (or vice versa).
            record_supersession_targets = tuple(str(value) for value in (delta.supersedes or ()))
            item_values: list[str] = []
            for key in ("item_ids", "ontology_item_ids", "prepared_asset_ids"):
                value = payload.get(key, ())
                if isinstance(value, (str, bytes)):
                    value = (value,)
                item_values.extend(str(item_id) for item_id in (value or ()))
            item_supersession_targets = tuple(dict.fromkeys(item_values)) if operation == "supersede" else ()
            missing_records = [item_id for item_id in record_supersession_targets if item_id not in self.knowledge]
            if missing_records:
                raise KeyError(f"unknown knowledge supersession targets: {missing_records}")
            if operation == "supersede":
                missing_items = [
                    item_id
                    for item_id in item_supersession_targets
                    if item_id not in self.ontology and item_id not in self.prepared_assets
                ]
                if missing_items:
                    raise KeyError(f"unknown ontology/prepared supersession targets: {missing_items}")
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
                self.conflicts.append({"delta_id": delta.delta_id, "operation": operation, "payload": payload, "conflicts_with": list(conflict_targets), "supersedes": list(record_supersession_targets), "evidence_refs": list(delta.evidence_refs), "unresolved": bool(payload.get("unresolved", operation == "record_conflict")), "working_definition": payload.get("working_definition")})
            elif operation == "supersede":
                for item_id in item_supersession_targets:
                    if item_id in self.ontology:
                        self.ontology[item_id] = replace(self.ontology[item_id], status="superseded")
                    if item_id in self.prepared_assets:
                        self.prepared_assets[item_id] = replace(self.prepared_assets[item_id], status="superseded")
            elif operation == "no_change":
                pass
            if conflict_targets:
                links = self.conflict_links.setdefault(delta.delta_id, set())
                for target in conflict_targets:
                    links.add(str(target))
                    self.conflict_links.setdefault(str(target), set()).add(delta.delta_id)
                    self.conflict_state.setdefault(delta.delta_id, {"unresolved": True, "working_definition": payload.get("working_definition"), "scope": payload.get("scope")})
                    self.conflict_state.setdefault(str(target), {"unresolved": True, "working_definition": None, "scope": None})
                    target_record = self.knowledge.get(str(target))
                    if target_record is not None:
                        target_record["conflicts_with"] = sorted(set(target_record.get("conflicts_with", ())) | {delta.delta_id})
                        target_record["unresolved"] = True
            if record_supersession_targets:
                targets = self.supersession_links.setdefault(delta.delta_id, set())
                for target in record_supersession_targets:
                    targets.add(str(target))
                    target_record = self.knowledge.get(str(target))
                    if target_record is not None:
                        target_record["superseded_by"] = sorted(set(target_record.get("superseded_by", ())) | {delta.delta_id})
            self.knowledge[delta.delta_id] = {"operation": operation, "payload": payload, "evidence_refs": list(delta.evidence_refs), "conflicts_with": list(conflict_targets), "supersedes": list(record_supersession_targets), "superseded_items": list(item_supersession_targets), "unresolved": self.conflict_state.get(delta.delta_id, {}).get("unresolved", bool(operation == "record_conflict")), "working_definition": payload.get("working_definition")}
            self.revisions.append({"delta_id": delta.delta_id, "operation": operation, "applied_at": datetime.now(timezone.utc).isoformat()})
            return {"applied": True, "delta_id": delta.delta_id, "operation": operation}
        except Exception:
            self.ontology, self.prepared_assets, self.canonical_mappings, self.relationships, self.knowledge, self.conflicts, self.conflict_links, self.supersession_links, self.conflict_state, self.revisions = snapshot
            raise

    def export(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ontology": [item.to_dict() for item in sorted(self.ontology.values(), key=lambda item: item.item_id)],
            "ontology_index": self.ontology_index,
            "prepared_assets": [asset.to_dict() for asset in sorted(self.prepared_assets.values(), key=lambda asset: asset.prepared_asset_id)],
            "canonical_mappings": [mapping.to_dict() for mapping in sorted(self.canonical_mappings.values(), key=lambda mapping: mapping.canonical_id)],
            "relationships": {key: self.relationships[key] for key in sorted(self.relationships)},
            "knowledge": deepcopy(self.knowledge),
            "conflicts": deepcopy(self.conflicts),
            "conflict_links": {key: sorted(value) for key, value in sorted(self.conflict_links.items())},
            "supersession_links": {key: sorted(value) for key, value in sorted(self.supersession_links.items())},
            "conflict_state": deepcopy(self.conflict_state),
            "revisions": deepcopy(self.revisions),
        }


__all__ = ["LivingEnterpriseModel"]
