"""Run-local Living Enterprise Model: extensible ontology plus prepared registry."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    CanonicalMapping,
    DataAssetRef,
    IdentityDecision,
    LEMRef,
    KnowledgeDelta,
    OntologyItem,
    PreparedAssetDescriptor,
)


_FORBIDDEN_ONTOLOGY_KEYS = frozenset({
    "reviewer", "reviewer_id", "reviewer_note", "reviewed_by", "lifecycle",
    "lifecycle_state", "run_state", "parser", "parser_metadata", "queue_state",
})

# Relevant bundles are bounded even when callers omit optional limits.  These
# are deliberately conservative run-local defaults, not tunable policy.
_DEFAULT_BUNDLE_LAYER_LIMIT = 256
_DEFAULT_BUNDLE_TOTAL_LIMIT = 512
_DEFAULT_BUNDLE_BYTES_LIMIT = 1_000_000

_EXPORT_CONTRACT_TYPES = (
    CanonicalMapping,
    DataAssetRef,
    IdentityDecision,
    LEMRef,
    KnowledgeDelta,
    OntologyItem,
    PreparedAssetDescriptor,
)


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items() if str(k).lower() not in _FORBIDDEN_ONTOLOGY_KEYS}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_clean(v) for v in value)
    return value


def _snapshot_value(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """Copy mutable evidence containers without copying frozen contracts.

    Contract instances are frozen dataclasses whose mapping fields are
    ``MappingProxyType`` values.  They are safe to retain by identity, while
    the surrounding evidence records still need independent mutable
    containers for rollback.  ``deepcopy`` cannot represent that combination
    because it attempts to pickle the mapping proxies, so only the container
    types that the LEM stores are traversed here.
    """

    if memo is None:
        memo = {}
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in memo:
            return memo[marker]
        copied: dict[Any, Any] = {}
        memo[marker] = copied
        for key, item in value.items():
            copied[key] = _snapshot_value(item, memo)
        return copied
    if isinstance(value, list):
        marker = id(value)
        if marker in memo:
            return memo[marker]
        copied_list: list[Any] = []
        memo[marker] = copied_list
        copied_list.extend(_snapshot_value(item, memo) for item in value)
        return copied_list
    if isinstance(value, tuple):
        marker = id(value)
        if marker in memo:
            return memo[marker]
        copied_tuple = tuple(_snapshot_value(item, memo) for item in value)
        memo[marker] = copied_tuple
        return copied_tuple
    if isinstance(value, set):
        marker = id(value)
        if marker in memo:
            return memo[marker]
        copied_set: set[Any] = set()
        memo[marker] = copied_set
        copied_set.update(_snapshot_value(item, memo) for item in value)
        return copied_set
    if isinstance(value, frozenset):
        marker = id(value)
        if marker in memo:
            return memo[marker]
        copied_frozenset = frozenset(_snapshot_value(item, memo) for item in value)
        memo[marker] = copied_frozenset
        return copied_frozenset
    if isinstance(value, bytearray):
        return bytearray(value)
    if is_dataclass(value) and getattr(getattr(type(value), "__dataclass_params__", None), "frozen", False):
        return value
    return value


def _validate_export_source(value: Any) -> None:
    """Validate an original graph before any contract serializer can coerce it."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"LEM export requires finite floats, got {value!r}")
        return
    if is_dataclass(value):
        if not getattr(getattr(type(value), "__dataclass_params__", None), "frozen", False):
            raise TypeError(f"LEM export does not support mutable dataclass: {type(value).__name__}")
        if not isinstance(value, _EXPORT_CONTRACT_TYPES):
            raise TypeError(f"LEM export does not support frozen dataclass: {type(value).__name__}")
        for field in fields(value):
            _validate_export_source(getattr(value, field.name))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"LEM export mapping keys must be strings: {type(key).__name__}")
            _validate_export_source(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _validate_export_source(item)
        return
    if isinstance(value, (bytearray, bytes)):
        raise TypeError(f"LEM export does not support {type(value).__name__} evidence")
    raise TypeError(f"LEM export does not support value type: {type(value).__name__}")


def _export_value(value: Any) -> Any:
    """Materialize LEM values into detached, JSON-native structures.

    Rollback snapshots intentionally retain frozen contract instances by
    identity.  Exported values have a different boundary: they must not leak
    those instances or their mapping proxies to callers.  Core contracts
    expose ``to_dict`` as their serialization contract; any other frozen
    dataclass is rejected rather than stringified or returned by alias.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"LEM export requires finite floats, got {value!r}")
        return value
    if is_dataclass(value):
        if not getattr(getattr(type(value), "__dataclass_params__", None), "frozen", False):
            raise TypeError(f"LEM export does not support mutable dataclass: {type(value).__name__}")
        if not isinstance(value, _EXPORT_CONTRACT_TYPES):
            raise TypeError(f"LEM export does not support frozen dataclass: {type(value).__name__}")
        _validate_export_source(value)
        serializer = getattr(value, "to_dict", None)
        if not callable(serializer):
            raise TypeError(f"LEM export has no serialization contract for frozen dataclass: {type(value).__name__}")
        materialized = serializer()
        if materialized is value:
            raise TypeError(f"LEM export serializer returned the original frozen dataclass: {type(value).__name__}")
        return _export_value(materialized)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"LEM export mapping keys must be strings: {type(key).__name__}")
            result[key] = _export_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_export_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        exported = [_export_value(item) for item in value]
        return sorted(exported, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    if isinstance(value, bytearray):
        raise TypeError("LEM export does not support bytearray evidence")
    if isinstance(value, bytes):
        raise TypeError("LEM export does not support bytes evidence")
    raise TypeError(f"LEM export does not support value type: {type(value).__name__}")


class LivingEnterpriseModel:
    """A bounded in-memory model intended to be exported at run end."""

    def __init__(self, *, run_id: str = "run") -> None:
        self.run_id = run_id
        self.ontology: dict[str, OntologyItem] = {}
        self.prepared_assets: dict[str, PreparedAssetDescriptor] = {}
        self.canonical_mappings: dict[str, CanonicalMapping] = {}
        self.identity_decisions: dict[str, IdentityDecision] = {}
        self.relationships: dict[str, dict[str, Any]] = {}
        self.knowledge: dict[str, dict[str, Any]] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.conflict_links: dict[str, set[str]] = {}
        self.supersession_links: dict[str, set[LEMRef]] = {}
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

    def register_identity_decision(self, decision: IdentityDecision | Mapping[str, Any]) -> IdentityDecision:
        decision = decision if isinstance(decision, IdentityDecision) else IdentityDecision.from_dict(decision)
        existing = self.identity_decisions.get(decision.decision_id)
        if existing is not None and existing != decision:
            raise ValueError(f"identity decision already exists with different trace: {decision.decision_id}")
        self.identity_decisions[decision.decision_id] = decision
        return decision

    def add_mapping(self, mapping: CanonicalMapping | Mapping[str, Any]) -> CanonicalMapping:
        mapping = mapping if isinstance(mapping, CanonicalMapping) else CanonicalMapping.from_dict(mapping)
        decision = self.identity_decisions.get(mapping.decision_id)
        if decision is None:
            raise ValueError(f"canonical mapping requires a registered identity decision: {mapping.decision_id}")
        if decision.review_status not in {"reviewed", "accepted"}:
            raise ValueError(f"canonical mapping requires a reviewed or accepted identity decision: {mapping.decision_id}")
        existing = self.canonical_mappings.get(mapping.canonical_id)
        if existing is not None:
            if existing != mapping:
                raise ValueError(f"canonical mapping already exists with different value: {mapping.canonical_id}")
            return existing
        self.canonical_mappings[mapping.canonical_id] = mapping
        return mapping

    def resolve_ref(self, ref: LEMRef | Mapping[str, Any]) -> Any:
        """Resolve one typed reference without searching other namespaces."""

        ref = ref if isinstance(ref, LEMRef) else LEMRef.from_dict(ref)
        registries = {
            "ontology": self.ontology,
            "prepared_asset": self.prepared_assets,
            "canonical_mapping": self.canonical_mappings,
            "knowledge_delta": self.knowledge,
        }
        registry = registries[ref.namespace]
        if ref.object_id not in registry:
            raise KeyError(f"unknown {ref.namespace} ref: {ref.object_id}")
        return registry[ref.object_id]

    @staticmethod
    def _semantic_payload(payload: Mapping[str, Any], item_type: str) -> OntologyItem:
        value = _clean(dict(payload))
        item_id = value.get("item_id") or value.get("relationship_id") or value.get("metric_id") or value.get("definition_id") or value.get("rule_id") or value.get("process_id") or value.get("id")
        if item_id is None:
            raise ValueError(f"{item_type} requires item_id (or a typed operation id)")
        label = value.get("label") or value.get("name") or value.get("title") or value.get("description") or item_id
        known = {"item_id", "item_type", "label", "properties", "source_refs", "evidence_level", "limitations", "scope", "effective_period", "status", "metadata"}
        properties = dict(value.get("properties") or {})
        properties.update({key: val for key, val in value.items() if key not in known and key not in {"metric_id", "definition_id", "rule_id", "process_id", "relationship_id", "id", "name", "title", "description"}})
        return OntologyItem(
            item_id=str(item_id),
            item_type=item_type,
            label=str(label),
            properties=properties,
            source_refs=tuple(value.get("source_refs", ())),
            evidence_level=str(value.get("evidence_level", "unassessed")),
            limitations=tuple(value.get("limitations", ())),
            scope=value.get("scope"),
            effective_period=value.get("effective_period"),
            status=str(value.get("status", "active")),
            metadata=value.get("metadata", {}),
        )

    def _add_semantic_item(self, item_type: str, item: OntologyItem | Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        payload = dict(item) if isinstance(item, Mapping) else (item.to_dict() if isinstance(item, OntologyItem) else {})
        payload.update(values)
        ontology_item = self._semantic_payload(payload, item_type)
        return self.add_ontology_item(ontology_item)

    def add_metric(self, item: OntologyItem | Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        return self._add_semantic_item("metric", item, **values)

    def add_definition(self, item: OntologyItem | Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        return self._add_semantic_item("definition", item, **values)

    def add_rule(self, item: OntologyItem | Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        return self._add_semantic_item("rule", item, **values)

    def add_process(self, item: OntologyItem | Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        return self._add_semantic_item("process", item, **values)

    def add_event(self, item: OntologyItem | Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        return self._add_semantic_item("event", item, **values)

    def add_dimension(self, item: OntologyItem | Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        return self._add_semantic_item("dimension", item, **values)

    def add_relationship(self, item: Mapping[str, Any] | None = None, **values: Any) -> OntologyItem:
        payload = dict(item or {})
        payload.update(values)
        relationship_id = payload.get("relationship_id") or payload.get("item_id") or payload.get("id")
        if relationship_id is None:
            raise ValueError("relationship requires relationship_id")
        if str(relationship_id) in self.relationships:
            raise ValueError(f"relationship already exists: {relationship_id}")
        record = _clean(dict(payload))
        record["relationship_id"] = str(relationship_id)
        self.relationships[str(relationship_id)] = record
        try:
            return self._add_semantic_item("relationship", payload)
        except Exception:
            self.relationships.pop(str(relationship_id), None)
            raise

    def lookup_prepared_asset(
        self,
        prepared_asset_id: str | None = None,
        *,
        operation_manifest_hash: str | None = None,
        source_hashes: Iterable[str] | None = None,
        core_version: str | None = None,
        prepared_content_hash: str | None = None,
    ) -> PreparedAssetDescriptor | None:
        """Find a matching descriptor in this run only."""

        candidates = list(self.prepared_assets.values())
        if prepared_asset_id is not None:
            candidates = [asset for asset in candidates if asset.prepared_asset_id == str(prepared_asset_id)]
        if operation_manifest_hash is not None:
            candidates = [asset for asset in candidates if asset.operation_manifest_hash == operation_manifest_hash]
        if source_hashes is not None:
            expected = tuple(str(value) for value in source_hashes)
            candidates = [asset for asset in candidates if asset.source_hashes == expected]
        if core_version is not None:
            candidates = [asset for asset in candidates if asset.core_version == core_version]
        if prepared_content_hash is not None:
            candidates = [asset for asset in candidates if asset.prepared_content_hash == prepared_content_hash]
        return sorted(candidates, key=lambda asset: asset.prepared_asset_id)[0] if candidates else None

    find_reusable_prepared_asset = lookup_prepared_asset

    @staticmethod
    def _hash_output(value: Any) -> str:
        if isinstance(value, Path):
            data = value.read_bytes()
        elif isinstance(value, str) and Path(value).is_file():
            data = Path(value).read_bytes()
        elif isinstance(value, bytes):
            data = value
        else:
            data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def verify_prepared_asset_reuse(
        self,
        prepared_asset_id: str,
        output: Any | None = None,
    ) -> bool:
        """Hash a stored output and reject reuse when its content changed."""

        asset = self.prepared_assets.get(str(prepared_asset_id))
        if asset is None:
            raise KeyError(prepared_asset_id)
        if not asset.prepared_content_hash:
            raise ValueError(f"prepared asset has no prepared_content_hash: {prepared_asset_id}")
        if output is None:
            if not asset.location:
                raise ValueError(f"prepared asset has no stored output location: {prepared_asset_id}")
            output = Path(asset.location)
            if not output.is_file():
                raise FileNotFoundError(output)
        observed = self._hash_output(output)
        if observed != asset.prepared_content_hash:
            raise ValueError(f"prepared asset content hash mismatch for {prepared_asset_id}: expected {asset.prepared_content_hash}, observed {observed}")
        return True

    verify_prepared_asset = verify_prepared_asset_reuse

    def relevant_bundle(
        self,
        ontology_ids: Iterable[str | LEMRef | Mapping[str, Any]] = (),
        *,
        prepared_asset_ids: Iterable[str | LEMRef | Mapping[str, Any]] = (),
        mapping_ids: Iterable[str | LEMRef | Mapping[str, Any]] = (),
        relationship_ids: Iterable[str | LEMRef | Mapping[str, Any]] = (),
        refs: Iterable[LEMRef | Mapping[str, Any]] = (),
        lem_refs: Iterable[LEMRef | Mapping[str, Any]] = (),
        max_items: int | None = None,
        max_total_items: int | None = None,
        max_bytes: int | None = None,
        max_json_bytes: int | None = None,
        per_layer_limits: Mapping[str, int] | None = None,
        layer_limits: Mapping[str, int] | None = None,
        ontology_limit: int | None = None,
        prepared_asset_limit: int | None = None,
        mapping_limit: int | None = None,
        relationship_limit: int | None = None,
        scope: str | None = None,
        effective_period: str | None = None,
    ) -> dict[str, Any]:
        """Resolve exact IDs into a bounded, scope-safe bundle.

        Every requested object is either in an explicit layer argument or an
        :class:`LEMRef`.  There is no cross-registry text-ID search.
        """

        layer_by_namespace = {
            "ontology": "ontology",
            "prepared_asset": "prepared_assets",
            "canonical_mapping": "mappings",
            "knowledge_delta": "knowledge",
        }
        ids: dict[str, list[str]] = {"ontology": [], "prepared_assets": [], "mappings": [], "relationships": []}
        typed: dict[str, list[LEMRef]] = {key: [] for key in ids}
        seen: set[tuple[str, str]] = set()

        def add_ref(raw: Any, expected_namespace: str | None = None) -> None:
            if isinstance(raw, LEMRef):
                ref = raw
            elif isinstance(raw, Mapping) and "namespace" in raw and "object_id" in raw:
                ref = LEMRef.from_dict(raw)
            else:
                if expected_namespace is None:
                    raise TypeError("relevant_bundle refs must be namespace-qualified LEMRef values")
                ref = LEMRef(expected_namespace, str(raw))
            if ref.namespace == "knowledge_delta":
                raise ValueError("knowledge_delta refs are not bundle material layers")
            if expected_namespace is not None and ref.namespace != expected_namespace:
                raise ValueError(f"wrong LEM namespace: expected {expected_namespace}, got {ref.namespace}")
            layer = layer_by_namespace[ref.namespace]
            key = (ref.namespace, ref.object_id)
            if key in seen:
                raise ValueError(f"duplicate relevant bundle reference: {ref.to_dict()}")
            seen.add(key)
            ids[layer].append(ref.object_id)
            typed[layer].append(ref)

        for raw in tuple(refs or ()) + tuple(lem_refs or ()):
            add_ref(raw)
        for raw in ontology_ids or ():
            add_ref(raw, "ontology")
        for raw in prepared_asset_ids or ():
            add_ref(raw, "prepared_asset")
        for raw in mapping_ids or ():
            add_ref(raw, "canonical_mapping")
        for raw in relationship_ids or ():
            if isinstance(raw, (LEMRef, Mapping)):
                # Relationship records are not a LEMRef namespace; a typed
                # canonical_mapping/ontology ref is therefore rejected.
                raise ValueError("relationship_ids require explicit relationship IDs, not LEMRef")
            value = str(raw)
            key = ("relationship", value)
            if key in seen:
                raise ValueError(f"duplicate relevant bundle relationship reference: {value}")
            seen.add(key)
            ids["relationships"].append(value)
            typed["relationships"].append(LEMRef("ontology", value))

        missing = {
            "ontology": [key for key in ids["ontology"] if key not in self.ontology],
            "prepared_assets": [key for key in ids["prepared_assets"] if key not in self.prepared_assets],
            "mappings": [key for key in ids["mappings"] if key not in self.canonical_mappings],
            "relationships": [key for key in ids["relationships"] if key not in self.relationships],
        }
        if any(missing.values()):
            raise KeyError(f"unknown exact IDs: {missing}")

        def matches_scope(value: str | None, *, prepared: bool = False) -> bool:
            if scope is None or value is None:
                return True
            return value == scope or (prepared and value == "reusable")

        def matches_period(value: str | None) -> bool:
            return effective_period is None or value is None or value == effective_period

        if scope is not None:
            mismatches = {
                "ontology": [key for key in ids["ontology"] if not matches_scope(self.ontology[key].scope)],
                "prepared_assets": [key for key in ids["prepared_assets"] if not matches_scope(self.prepared_assets[key].scope, prepared=True)],
                "mappings": [key for key in ids["mappings"] if not matches_scope(self.canonical_mappings[key].scope)],
                "relationships": [key for key in ids["relationships"] if not matches_scope(self.relationships[key].get("scope"))],
            }
            if any(mismatches.values()):
                raise ValueError(f"requested IDs are outside scope {scope!r}: {mismatches}")
        periods = {
            "ontology": [key for key in ids["ontology"] if not matches_period(self.ontology[key].effective_period)],
            "prepared_assets": [key for key in ids["prepared_assets"] if not matches_period(self.prepared_assets[key].effective_period)],
            "mappings": [key for key in ids["mappings"] if not matches_period(self.canonical_mappings[key].effective_period)],
            "relationships": [key for key in ids["relationships"] if not matches_period(self.relationships[key].get("effective_period"))],
        }
        if any(periods.values()):
            raise ValueError(f"requested IDs are outside effective period {effective_period!r}: {periods}")

        limits = {
            "ontology": _DEFAULT_BUNDLE_LAYER_LIMIT,
            "prepared_assets": _DEFAULT_BUNDLE_LAYER_LIMIT,
            "mappings": _DEFAULT_BUNDLE_LAYER_LIMIT,
            "relationships": _DEFAULT_BUNDLE_LAYER_LIMIT,
        }
        supplied_layer_limits: dict[str, Any] = {}
        for source in (per_layer_limits, layer_limits):
            if source:
                supplied_layer_limits.update(dict(source))
        supplied_layer_limits.update({key: value for key, value in {
            "ontology": ontology_limit,
            "prepared_assets": prepared_asset_limit,
            "mappings": mapping_limit,
            "relationships": relationship_limit,
        }.items() if value is not None})
        unknown_layers = sorted(set(supplied_layer_limits) - set(limits))
        if unknown_layers:
            raise ValueError(f"unknown relevant bundle layer limits: {unknown_layers}")

        def validate_limit(value: Any, label: str) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{label} must be a finite integer")
            if value < 0:
                raise ValueError(f"{label} cannot be negative")
            if not math.isfinite(value):
                raise ValueError(f"{label} must be finite")
            return value

        for layer, limit in supplied_layer_limits.items():
            limits[layer] = validate_limit(limit, f"{layer} limit")
        for layer, limit in limits.items():
            if len(ids[layer]) > limit:
                raise ValueError(f"{layer} layer exceeds limit {limit}: {len(ids[layer])}")

        total_limit = max_total_items if max_total_items is not None else max_items
        if total_limit is None:
            total_limit = _DEFAULT_BUNDLE_TOTAL_LIMIT
        else:
            total_limit = validate_limit(total_limit, "total bundle item limit")
        total_count = sum(len(values) for values in ids.values())
        if total_count > total_limit:
            raise ValueError(f"bundle exceeds total item limit {total_limit}: {total_count}")

        bundle = {
            "ontology": [self.ontology[key].to_dict() for key in ids["ontology"]],
            "prepared_assets": [self.prepared_assets[key].to_dict() for key in ids["prepared_assets"]],
            "mappings": [self.canonical_mappings[key].to_dict() for key in ids["mappings"]],
            "relationships": [self.relationships[key] for key in ids["relationships"]],
            "exact_ids": {layer: list(values) for layer, values in ids.items()},
            "exact_refs": {layer: [ref.to_dict() for ref in typed[layer]] for layer in ("ontology", "prepared_assets", "mappings")},
        }
        byte_limit = max_json_bytes if max_json_bytes is not None else max_bytes
        if byte_limit is None:
            byte_limit = _DEFAULT_BUNDLE_BYTES_LIMIT
        else:
            byte_limit = validate_limit(byte_limit, "bundle JSON byte limit")
        metadata = {
            "counts": {layer: len(values) for layer, values in ids.items()},
            "total_count": total_count,
            "approximate_json_bytes": 0,
            "limits": {"per_layer": limits, "total": total_limit, "bytes": byte_limit},
            "scope": scope,
            "effective_period": effective_period,
        }
        bundle["metadata"] = metadata
        # The metadata includes the byte estimate itself.  Two passes are
        # enough to reach the same integer length for this compact record.
        approximate_bytes = 0
        for _ in range(3):
            metadata["approximate_json_bytes"] = approximate_bytes
            approximate_bytes = len(json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
        if approximate_bytes > byte_limit:
            raise ValueError(f"bundle exceeds approximate JSON byte limit {byte_limit}: {approximate_bytes}")
        metadata["approximate_json_bytes"] = approximate_bytes
        return bundle

    def apply_delta(self, delta: KnowledgeDelta | Mapping[str, Any], *, accepted: bool | None = None) -> dict[str, Any]:
        """Apply one accepted delta atomically, preserving conflicts/supersession."""

        delta = delta if isinstance(delta, KnowledgeDelta) else KnowledgeDelta.from_dict(delta)
        if accepted is None:
            accepted = delta.accepted
        if not accepted:
            return {"applied": False, "delta_id": delta.delta_id, "operation": delta.operation}
        if delta.delta_id in self.knowledge:
            raise ValueError(f"knowledge delta already exists: {delta.delta_id}")
        # Contract values are frozen and contain mapping proxies, which are
        # intentionally not deepcopy/pickleable.  Copy registries by identity
        # and recursively copy only mutable evidence containers.
        snapshot_memo: dict[int, Any] = {}
        snapshot = (
            dict(self.ontology), dict(self.prepared_assets), dict(self.canonical_mappings),
            dict(self.identity_decisions), _snapshot_value(self.relationships, snapshot_memo),
            _snapshot_value(self.knowledge, snapshot_memo), _snapshot_value(self.conflicts, snapshot_memo),
            _snapshot_value(self.conflict_links, snapshot_memo), _snapshot_value(self.supersession_links, snapshot_memo),
            _snapshot_value(self.conflict_state, snapshot_memo), _snapshot_value(self.revisions, snapshot_memo),
        )
        try:
            payload = _clean(dict(delta.payload))
            operation = delta.operation
            conflict_targets = tuple(str(value) for value in (delta.conflicts_with or payload.get("conflicts_with", ())))
            supersession_targets = tuple(delta.supersedes)

            def resolve(ref: LEMRef) -> Any:
                registries = {
                    "ontology": self.ontology,
                    "prepared_asset": self.prepared_assets,
                    "canonical_mapping": self.canonical_mappings,
                    "knowledge_delta": self.knowledge,
                }
                registry = registries[ref.namespace]
                if ref.object_id not in registry:
                    raise KeyError(f"unknown {ref.namespace} ref: {ref.object_id}")
                return registry[ref.object_id]

            # Resolve every target before mutation.  A mixed valid/invalid set
            # therefore fails atomically and never leaves a partial supersession.
            resolved_targets = [(ref, resolve(ref)) for ref in supersession_targets]
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
            elif operation == "add_relationship":
                self.add_relationship(payload)
            elif operation == "add_metric":
                self.add_metric(payload)
            elif operation == "add_definition":
                self.add_definition(payload)
            elif operation == "add_rule":
                self.add_rule(payload)
            elif operation == "add_process":
                self.add_process(payload)
            elif operation == "add_event":
                self.add_event(payload)
            elif operation == "add_dimension":
                self.add_dimension(payload)
            elif operation == "add_alias":
                key = str(payload.get("canonical_id"))
                mapping = self.canonical_mappings.get(key)
                if mapping is None:
                    raise KeyError(key)
                if "aliases" in payload and isinstance(payload["aliases"], str):
                    aliases_input = (payload["aliases"],)
                else:
                    aliases_input = tuple(payload.get("aliases", ()))
                if payload.get("alias") is not None:
                    aliases_input += (str(payload["alias"]),)
                source_input = payload.get("source_identities", payload.get("source_identity", ()))
                if isinstance(source_input, str):
                    source_input = (source_input,)
                if not aliases_input and not source_input:
                    raise ValueError("add_alias requires an explicit alias or source identity")
                aliases = tuple(dict.fromkeys((*mapping.aliases, *(str(value) for value in aliases_input))))
                source_ids = tuple(dict.fromkeys((*mapping.source_identities, *(str(value) for value in source_input))))
                self.canonical_mappings[key] = replace(mapping, aliases=aliases, source_identities=source_ids)
            elif operation in {"record_limitation", "record_conflict"}:
                self.conflicts.append({"delta_id": delta.delta_id, "operation": operation, "payload": payload, "conflicts_with": list(conflict_targets), "supersedes": [ref.to_dict() for ref in supersession_targets], "evidence_refs": list(delta.evidence_refs), "unresolved": bool(payload.get("unresolved", operation == "record_conflict")), "working_definition": payload.get("working_definition")})
            elif operation == "supersede":
                for ref, target in resolved_targets:
                    if ref.namespace == "ontology":
                        self.ontology[ref.object_id] = replace(target, status="superseded")
                    elif ref.namespace == "prepared_asset":
                        self.prepared_assets[ref.object_id] = replace(target, status="superseded")
                    elif ref.namespace == "canonical_mapping":
                        self.canonical_mappings[ref.object_id] = replace(target, status="superseded")
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
            if supersession_targets:
                targets = self.supersession_links.setdefault(delta.delta_id, set())
                current_ref = LEMRef("knowledge_delta", delta.delta_id)
                for target_ref, target in resolved_targets:
                    targets.add(target_ref)
                    if target_ref.namespace == "knowledge_delta":
                        prior = [value for value in target.get("superseded_by", ()) if value != current_ref.to_dict()]
                        target["superseded_by"] = sorted((*prior, current_ref.to_dict()), key=lambda value: json.dumps(value, sort_keys=True))
            self.knowledge[delta.delta_id] = {
                "ref": LEMRef("knowledge_delta", delta.delta_id).to_dict(),
                "operation": operation,
                "payload": payload,
                "evidence_refs": list(delta.evidence_refs),
                "conflicts_with": list(conflict_targets),
                "supersedes": [ref.to_dict() for ref in supersession_targets],
                "unresolved": self.conflict_state.get(delta.delta_id, {}).get("unresolved", bool(operation == "record_conflict")),
                "working_definition": payload.get("working_definition"),
            }
            self.revisions.append({"delta_id": delta.delta_id, "operation": operation, "applied_at": datetime.now(timezone.utc).isoformat()})
            return {"applied": True, "delta_id": delta.delta_id, "operation": operation}
        except Exception:
            self.ontology, self.prepared_assets, self.canonical_mappings, self.identity_decisions, self.relationships, self.knowledge, self.conflicts, self.conflict_links, self.supersession_links, self.conflict_state, self.revisions = snapshot
            raise

    def export(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ontology": _export_value(list(sorted(self.ontology.values(), key=lambda item: item.item_id))),
            "ontology_index": _export_value(self.ontology_index),
            "prepared_assets": _export_value(list(sorted(self.prepared_assets.values(), key=lambda asset: asset.prepared_asset_id))),
            "canonical_mappings": _export_value(list(sorted(self.canonical_mappings.values(), key=lambda mapping: mapping.canonical_id))),
            "identity_decisions": _export_value(list(sorted(self.identity_decisions.values(), key=lambda decision: decision.decision_id))),
            "relationships": _export_value({key: self.relationships[key] for key in sorted(self.relationships)}),
            "knowledge": _export_value(self.knowledge),
            "conflicts": _export_value(self.conflicts),
            "conflict_links": _export_value({key: value for key, value in sorted(self.conflict_links.items())}),
            "supersession_links": _export_value({key: value for key, value in sorted(self.supersession_links.items())}),
            "conflict_state": _export_value(self.conflict_state),
            "revisions": _export_value(self.revisions),
        }


__all__ = ["LivingEnterpriseModel"]
