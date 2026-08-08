"""Stable, deliberately small data contracts used by :mod:`auto_foundry_core`.

Contracts are ordinary dataclasses rather than a schema framework.  They are
strict about identifiers and impossible values while retaining an ``metadata``
escape hatch for forward-compatible extensions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Mapping, Sequence


def _freeze(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (Path,)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class ContractMixin:
    """JSON helpers shared by all public contracts."""

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    def to_json(self, *, sort_keys: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=sort_keys, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str):
        return cls.from_dict(json.loads(text))


def _require(value: str, label: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _tuple_values(value: Iterable[Any] | None) -> tuple[Any, ...]:
    return tuple(value or ())


@dataclass(frozen=True)
class DataAssetRef(ContractMixin):
    """Immutable content-addressed reference to one local source asset."""

    uri: str
    format: str | None = None
    content_hash: str | None = None
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "uri", _require(self.uri, "uri"))
        if self.content_hash is not None:
            h = _require(self.content_hash, "content_hash").lower()
            if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
                raise ValueError("content_hash must be a SHA-256 hex digest")
            object.__setattr__(self, "content_hash", h)
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        object.__setattr__(self, "format", self.format.lower().lstrip(".") if self.format else None)
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def path(self) -> Path:
        return Path(self.uri)

    @property
    def id(self) -> str:
        return self.content_hash or hashlib.sha256(self.uri.encode()).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DataAssetRef":
        return cls(**dict(data))

    @classmethod
    def from_path(cls, path: str | Path, *, format: str | None = None, metadata: Mapping[str, Any] | None = None) -> "DataAssetRef":
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return cls(uri=str(candidate), format=format or candidate.suffix.lstrip("."), content_hash=digest.hexdigest(), size_bytes=candidate.stat().st_size, metadata=metadata or {})


@dataclass(frozen=True)
class TableRef(ContractMixin):
    asset: DataAssetRef | str
    name: str
    kind: str = "table"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require(self.name, "table name"))
        if isinstance(self.asset, Mapping):
            object.__setattr__(self, "asset", DataAssetRef.from_dict(self.asset))
        else:
            object.__setattr__(self, "asset", self.asset)
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def asset_ref(self) -> DataAssetRef | str:
        return self.asset

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TableRef":
        return cls(**dict(data))


@dataclass(frozen=True)
class FieldRef(ContractMixin):
    table: TableRef | str
    name: str
    physical_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require(self.name, "field name"))
        if isinstance(self.table, Mapping):
            object.__setattr__(self, "table", TableRef.from_dict(self.table))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def table_ref(self) -> TableRef | str:
        return self.table

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FieldRef":
        return cls(**dict(data))


@dataclass(frozen=True)
class DocumentRef(ContractMixin):
    asset: DataAssetRef | str
    title: str | None = None
    mime_type: str | None = None
    extract_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.asset, Mapping):
            object.__setattr__(self, "asset", DataAssetRef.from_dict(self.asset))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def asset_ref(self) -> DataAssetRef | str:
        return self.asset

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DocumentRef":
        return cls(**dict(data))


@dataclass(frozen=True)
class PreparedAssetRef(ContractMixin):
    prepared_asset_id: str
    location: str
    source_refs: tuple[DataAssetRef | str, ...] = ()
    content_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "prepared_asset_id", _require(self.prepared_asset_id, "prepared_asset_id"))
        object.__setattr__(self, "location", _require(self.location, "location"))
        refs = tuple(DataAssetRef.from_dict(r) if isinstance(r, Mapping) else r for r in self.source_refs)
        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def id(self) -> str:
        return self.prepared_asset_id

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PreparedAssetRef":
        return cls(**dict(data))


@dataclass(frozen=True)
class OperationSpec(ContractMixin):
    capability_id: str
    inputs: tuple[Any, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    version: str = "0.1.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    allowed_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _require(self.capability_id, "capability_id"))
        object.__setattr__(self, "inputs", _tuple_values(self.inputs))
        object.__setattr__(self, "parameters", _freeze(self.parameters))
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "allowed_roots", tuple(str(value) for value in self.allowed_roots))

    @property
    def normalized(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)

    @property
    def spec_hash(self) -> str:
        return hashlib.sha256(self.normalized.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationSpec":
        value = dict(data)
        if "input" in value and "inputs" not in value:
            value["inputs"] = value.pop("input")
        return cls(**value)


@dataclass(frozen=True)
class OperationResultRef(ContractMixin):
    location: str
    content_hash: str | None = None
    format: str | None = None
    rows: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "location", _require(self.location, "result location"))
        object.__setattr__(self, "format", self.format.lower().lstrip(".") if self.format else None)
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationResultRef":
        return cls(**dict(data))


@dataclass(frozen=True)
class OperationReceipt(ContractMixin):
    capability_id: str
    spec_hash: str
    input_hashes: tuple[str, ...] = ()
    output: OperationResultRef | None = None
    output_hashes: tuple[str, ...] = ()
    backend: str = "python"
    duration_ms: float | None = None
    cache_status: str = "miss"
    limitations: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_hashes", _tuple_values(self.input_hashes))
        object.__setattr__(self, "output_hashes", _tuple_values(self.output_hashes))
        object.__setattr__(self, "limitations", tuple(str(v) for v in self.limitations))
        object.__setattr__(self, "errors", tuple(str(v) for v in self.errors))
        if isinstance(self.output, Mapping):
            object.__setattr__(self, "output", OperationResultRef.from_dict(self.output))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationReceipt":
        return cls(**dict(data))


@dataclass(frozen=True)
class RequirementRecord(ContractMixin):
    requirement_id: str
    original_text: str
    explicit_priority: Any = None
    business_objective: str = ""
    expected_analytical_outputs: tuple[str, ...] = ()
    expected_visual_outputs: tuple[str, ...] = ()
    internal_tasks: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    shared_foundation_dependencies: tuple[str, ...] = ()
    data_needs: tuple[str, ...] = ()
    ontology_needs: tuple[str, ...] = ()
    prepared_data_needs: tuple[str, ...] = ()
    working_definitions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    priority_source: str | None = None
    priority_explicit: bool | None = None
    priority_tie_handling: str = ""
    scope: str = "analytics"
    status: str = "queued"
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    planner: Mapping[str, Any] = field(default_factory=dict)
    review: Mapping[str, Any] = field(default_factory=dict)
    outcome: Any = None
    # ``priority`` and these names are direct fields for callers that use the
    # compact requirement vocabulary; canonical storage remains explicit above.
    priority: Any = None
    objective: str | None = None
    expected_outputs: Mapping[str, Any] = field(default_factory=dict)
    foundation_dependencies: tuple[str, ...] = ()
    needs: Mapping[str, Any] = field(default_factory=dict)
    limits: tuple[str, ...] = ()
    scope_classification: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _require(self.requirement_id, "requirement_id"))
        object.__setattr__(self, "original_text", _require(self.original_text, "original_text"))
        priority = self.explicit_priority if self.explicit_priority is not None else self.priority
        if isinstance(priority, Mapping):
            priority_source = self.priority_source or priority.get("source")
            priority_explicit = self.priority_explicit if self.priority_explicit is not None else priority.get("explicit")
            if not self.priority_tie_handling:
                object.__setattr__(self, "priority_tie_handling", str(priority.get("tie_handling", "")))
            priority = priority.get("value")
            object.__setattr__(self, "priority_source", priority_source)
            object.__setattr__(self, "priority_explicit", priority_explicit)
        object.__setattr__(self, "explicit_priority", priority)
        object.__setattr__(self, "priority", priority)
        objective = self.business_objective or self.objective or ""
        object.__setattr__(self, "business_objective", objective)
        object.__setattr__(self, "objective", objective)
        expected = dict(self.expected_outputs or {})
        analytical = tuple(str(v) for v in (self.expected_analytical_outputs or expected.get("analytical", ())))
        visual = tuple(str(v) for v in (self.expected_visual_outputs or expected.get("visual", ())))
        object.__setattr__(self, "expected_analytical_outputs", analytical)
        object.__setattr__(self, "expected_visual_outputs", visual)
        object.__setattr__(self, "expected_outputs", {"analytical": analytical, "visual": visual})
        foundation = tuple(self.shared_foundation_dependencies or self.foundation_dependencies)
        object.__setattr__(self, "shared_foundation_dependencies", _tuple_values(foundation))
        object.__setattr__(self, "foundation_dependencies", _tuple_values(foundation))
        needs = dict(self.needs or {})
        data_needs = tuple(str(v) for v in (self.data_needs or needs.get("data", ())))
        ontology_needs = tuple(str(v) for v in (self.ontology_needs or needs.get("ontology", ())))
        prepared_needs = tuple(str(v) for v in (self.prepared_data_needs or needs.get("prepared", ())))
        object.__setattr__(self, "data_needs", data_needs)
        object.__setattr__(self, "ontology_needs", ontology_needs)
        object.__setattr__(self, "prepared_data_needs", prepared_needs)
        object.__setattr__(self, "needs", {"data": data_needs, "ontology": ontology_needs, "prepared": prepared_needs})
        limitations = tuple(self.limitations or self.limits)
        object.__setattr__(self, "limitations", _tuple_values(limitations))
        object.__setattr__(self, "limits", _tuple_values(limitations))
        if self.scope_classification:
            object.__setattr__(self, "scope", self.scope_classification)
        object.__setattr__(self, "scope_classification", self.scope)
        object.__setattr__(self, "source_refs", _tuple_values(self.source_refs))
        object.__setattr__(self, "evidence_refs", _tuple_values(self.evidence_refs))
        object.__setattr__(self, "planner", _freeze(self.planner))
        object.__setattr__(self, "review", _freeze(self.review))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RequirementRecord":
        value = dict(data)
        if "original_text" not in value and "text" in value:
            value["original_text"] = value.pop("text")
        if "business_objective" not in value and "objective" in value:
            value["business_objective"] = value["objective"]
        if "expected_outputs" in value:
            outputs = value["expected_outputs"] or {}
            value.setdefault("expected_analytical_outputs", outputs.get("analytical", ()))
            value.setdefault("expected_visual_outputs", outputs.get("visual", ()))
        if "shared_foundation_dependencies" not in value and "foundation_dependencies" in value:
            value["shared_foundation_dependencies"] = value["foundation_dependencies"]
        if "needs" in value:
            needs = value["needs"] or {}
            value.setdefault("data_needs", needs.get("data", ()))
            value.setdefault("ontology_needs", needs.get("ontology", ()))
            value.setdefault("prepared_data_needs", needs.get("prepared", ()))
        if "limitations" not in value and "limits" in value:
            value["limitations"] = value["limits"]
        if "scope" not in value and "scope_classification" in value:
            value["scope"] = value["scope_classification"]
        return cls(**value)


@dataclass(frozen=True)
class FoundationTask(ContractMixin):
    task_id: str
    description: str
    supports_requirements: tuple[str, ...] = ()
    capability_ids: tuple[str, ...] = ()
    status: str = "planned"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _require(self.task_id, "task_id"))
        object.__setattr__(self, "description", _require(self.description, "description"))
        object.__setattr__(self, "supports_requirements", _tuple_values(self.supports_requirements))
        object.__setattr__(self, "capability_ids", _tuple_values(self.capability_ids))
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class RequirementPortfolioPlan(ContractMixin):
    plan_id: str
    requirement_ids: tuple[str, ...] = ()
    foundation_tasks: tuple[FoundationTask, ...] = ()
    execution_order: tuple[str, ...] = ()
    rationale: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _require(self.plan_id, "plan_id"))
        object.__setattr__(self, "requirement_ids", _tuple_values(self.requirement_ids))
        tasks = tuple(FoundationTask(**t) if isinstance(t, Mapping) else t for t in self.foundation_tasks)
        object.__setattr__(self, "foundation_tasks", tasks)
        object.__setattr__(self, "execution_order", _tuple_values(self.execution_order))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RequirementPortfolioPlan":
        return cls(**dict(data))


@dataclass(frozen=True)
class OntologyItem(ContractMixin):
    item_id: str
    item_type: str
    label: str
    properties: Mapping[str, Any] = field(default_factory=dict)
    source_refs: tuple[str, ...] = ()
    evidence_level: str = "unassessed"
    limitations: tuple[str, ...] = ()
    scope: str | None = None
    effective_period: str | None = None
    status: str = "active"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _require(self.item_id, "item_id"))
        object.__setattr__(self, "item_type", _require(self.item_type, "item_type"))
        object.__setattr__(self, "label", _require(self.label, "label"))
        object.__setattr__(self, "properties", _freeze(self.properties))
        object.__setattr__(self, "source_refs", _tuple_values(self.source_refs))
        object.__setattr__(self, "limitations", tuple(str(v) for v in self.limitations))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OntologyItem":
        return cls(**dict(data))


@dataclass(frozen=True)
class PreparedAssetDescriptor(ContractMixin):
    prepared_asset_id: str
    source_refs: tuple[DataAssetRef | str, ...] = ()
    source_hashes: tuple[str, ...] = ()
    location: str = ""
    schema: Mapping[str, str] = field(default_factory=dict)
    grain: str | None = None
    transformations: tuple[str, ...] = ()
    identity_mappings: tuple[str, ...] = ()
    relationship_mappings: tuple[str, ...] = ()
    quality_findings: tuple[str, ...] = ()
    unresolved_records: int | None = None
    lineage: Mapping[str, Any] = field(default_factory=dict)
    freshness: str | None = None
    scope: str = "reusable"
    ontology_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    creation_requirement: str | None = None
    status: str = "active"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "prepared_asset_id", _require(self.prepared_asset_id, "prepared_asset_id"))
        refs = tuple(DataAssetRef.from_dict(r) if isinstance(r, Mapping) else r for r in self.source_refs)
        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(self, "source_hashes", _tuple_values(self.source_hashes))
        object.__setattr__(self, "schema", _freeze(self.schema))
        for name in ("transformations", "identity_mappings", "relationship_mappings", "quality_findings", "ontology_refs", "limitations"):
            object.__setattr__(self, name, tuple(str(v) for v in getattr(self, name)))
        object.__setattr__(self, "lineage", _freeze(self.lineage))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PreparedAssetDescriptor":
        return cls(**dict(data))


@dataclass(frozen=True)
class KnowledgeDelta(ContractMixin):
    delta_id: str
    operation: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    reviewer_note: str | None = None
    accepted: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    ALLOWED_OPERATIONS: ClassVar[frozenset[str]] = frozenset({
        "add_ontology_item", "extend_ontology_item", "add_alias", "add_canonical_mapping",
        "add_relationship", "add_metric", "add_definition", "add_rule", "add_process",
        "add_prepared_asset", "extend_prepared_asset", "record_limitation", "record_conflict",
        "supersede", "no_change",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "delta_id", _require(self.delta_id, "delta_id"))
        object.__setattr__(self, "operation", _require(self.operation, "operation"))
        if self.operation not in self.ALLOWED_OPERATIONS:
            raise ValueError(f"unsupported knowledge delta operation: {self.operation}")
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "evidence_refs", _tuple_values(self.evidence_refs))
        object.__setattr__(self, "conflicts_with", _tuple_values(self.conflicts_with))
        object.__setattr__(self, "supersedes", _tuple_values(self.supersedes))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KnowledgeDelta":
        return cls(**dict(data))


@dataclass(frozen=True)
class IdentityEvidence(ContractMixin):
    kind: str
    left_value: Any = None
    right_value: Any = None
    strength: float | None = None
    source: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _require(self.kind, "evidence kind"))
        if self.strength is not None and not 0 <= self.strength <= 1:
            raise ValueError("evidence strength must be between 0 and 1")
        object.__setattr__(self, "details", _freeze(self.details))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IdentityEvidence":
        return cls(**dict(data))


@dataclass(frozen=True)
class IdentityCandidate(ContractMixin):
    candidate_id: str
    object_type: str
    left_id: str
    right_id: str
    evidence: tuple[IdentityEvidence, ...] = ()
    contradictions: tuple[IdentityEvidence, ...] = ()
    similarity: float | None = None
    coverage: float | None = None
    status: str = "unresolved"
    limitations: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("candidate_id", "object_type", "left_id", "right_id"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        object.__setattr__(self, "evidence", tuple(IdentityEvidence.from_dict(v) if isinstance(v, Mapping) else v for v in self.evidence))
        object.__setattr__(self, "contradictions", tuple(IdentityEvidence.from_dict(v) if isinstance(v, Mapping) else v for v in self.contradictions))
        object.__setattr__(self, "limitations", tuple(str(v) for v in self.limitations))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IdentityCandidate":
        return cls(**dict(data))


@dataclass(frozen=True)
class IdentityDecision(ContractMixin):
    candidate_id: str
    decision: str
    decided_by: str | None = None
    rationale: str = ""
    evidence_refs: tuple[str, ...] = ()
    canonical_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    VOCABULARY: ClassVar[frozenset[str]] = frozenset({
        "same_object", "different_objects", "possible_match", "insufficient_evidence",
        "version_of", "parent_child", "alternate_representation",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _require(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "decision", _require(self.decision, "decision"))
        if self.decision not in self.VOCABULARY:
            raise ValueError(f"unsupported identity decision: {self.decision}")
        object.__setattr__(self, "evidence_refs", _tuple_values(self.evidence_refs))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IdentityDecision":
        return cls(**dict(data))


@dataclass(frozen=True)
class CanonicalMapping(ContractMixin):
    canonical_id: str
    object_type: str
    source_identities: tuple[str, ...]
    decision_id: str
    status: str = "accepted"
    aliases: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("canonical_id", "object_type", "decision_id"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        object.__setattr__(self, "source_identities", _tuple_values(self.source_identities))
        object.__setattr__(self, "aliases", _tuple_values(self.aliases))
        object.__setattr__(self, "limitations", _tuple_values(self.limitations))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CanonicalMapping":
        return cls(**dict(data))


@dataclass(frozen=True)
class CapabilityDescriptor(ContractMixin):
    capability_id: str
    version: str
    purpose: str
    when_to_use: str
    when_not_to_use: str
    input_contract: Mapping[str, Any]
    output_contract: Mapping[str, Any]
    limitations: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    cache_behavior: str = "not_cacheable"
    examples: tuple[str, ...] = ()
    backend: str = "python"
    handler: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _require(self.capability_id, "capability_id"))
        object.__setattr__(self, "input_contract", _freeze(self.input_contract))
        object.__setattr__(self, "output_contract", _freeze(self.output_contract))
        object.__setattr__(self, "limitations", _tuple_values(self.limitations))
        object.__setattr__(self, "side_effects", _tuple_values(self.side_effects))
        object.__setattr__(self, "examples", _tuple_values(self.examples))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityDescriptor":
        return cls(**dict(data))


@dataclass(frozen=True)
class TelemetryEvent(ContractMixin):
    event_type: str
    timestamp: str = ""
    capability_id: str | None = None
    spec_hash: str | None = None
    input_hashes: tuple[str, ...] = ()
    output_hashes: tuple[str, ...] = ()
    duration_ms: float | None = None
    rows: int | None = None
    bytes_processed: int | None = None
    cache_status: str | None = None
    error: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", _require(self.event_type, "event_type"))
        if not self.timestamp:
            object.__setattr__(self, "timestamp", datetime.now().astimezone().isoformat())
        else:
            object.__setattr__(self, "timestamp", _require(self.timestamp, "timestamp"))
        object.__setattr__(self, "input_hashes", _tuple_values(self.input_hashes))
        object.__setattr__(self, "output_hashes", _tuple_values(self.output_hashes))
        object.__setattr__(self, "facts", _freeze(self.facts))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TelemetryEvent":
        return cls(**dict(data))


@dataclass(frozen=True)
class RunTelemetrySummary(ContractMixin):
    run_id: str
    started_at: str = ""
    ended_at: str | None = None
    wall_time_ms: float | None = None
    model_calls: int | str = "unavailable"
    model_wall_ms: float | str = "unavailable"
    tool_calls: int | str = "unavailable"
    files_read: int | str = "unavailable"
    bytes_read: int | str = "unavailable"
    cache_hits: int = 0
    cache_misses: int = 0
    capability_usage: Mapping[str, int] = field(default_factory=dict)
    custom_script_count: int | str = "unavailable"
    custom_script_loc: int | str = "unavailable"
    question_outcomes: Mapping[str, str] = field(default_factory=dict)
    ontology_items_created: int | str = "unavailable"
    prepared_assets_created: int | str = "unavailable"
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_usage", _freeze(self.capability_usage))
        object.__setattr__(self, "question_outcomes", _freeze(self.question_outcomes))
        object.__setattr__(self, "facts", _freeze(self.facts))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunTelemetrySummary":
        return cls(**dict(data))


@dataclass(frozen=True)
class AggregationSpec(ContractMixin):
    operation: str
    value_field: str | None = None
    group_by: tuple[str, ...] = ()
    distinct: bool = False
    period_field: str | None = None
    currency_field: str | None = None
    ranking_order: str = "desc"
    limit: int | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    ALLOWED: ClassVar[frozenset[str]] = frozenset({
        "count", "distinct_count", "share", "rate", "sum", "average", "min", "max",
        "distribution", "group", "ranking", "period_comparison", "currency_totals",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", _require(self.operation, "aggregation operation"))
        if self.operation not in self.ALLOWED:
            raise ValueError(f"unsupported aggregation operation: {self.operation}")
        object.__setattr__(self, "group_by", _tuple_values(self.group_by))
        object.__setattr__(self, "parameters", _freeze(self.parameters))
