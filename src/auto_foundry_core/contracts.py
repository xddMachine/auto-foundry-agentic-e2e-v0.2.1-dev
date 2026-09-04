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
from typing import Any, ClassVar, Iterable, Literal, Mapping, Sequence


def _freeze(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = dict(value or {})
    # Explicit references nested in metadata/properties must regain their
    # typed objects on contract construction.  Ordinary mappings are copied
    # unchanged by the recursive helper; field names never trigger inference.
    from .references import decode_reference_value
    decoded = decode_reference_value(raw)
    if not isinstance(decoded, Mapping):
        raise TypeError("contract mapping field must remain a mapping")
    return MappingProxyType(dict(decoded))


def _jsonable(value: Any) -> Any:
    # Filesystem references are the one intentionally tagged union in the
    # package.  Handle them before the generic dataclass branch so every
    # nested contract (receipts, operation specs, cache values, manifests)
    # preserves the explicit discriminator on the wire.
    if "DataAssetRef" in globals() and "OperationResultRef" in globals() and isinstance(value, (DataAssetRef, OperationResultRef)):
        from .references import encode_explicit_reference
        return encode_explicit_reference(value)
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


LEMNamespace = Literal[
    "ontology",
    "prepared_asset",
    "canonical_mapping",
    "relationship",
    "knowledge_delta",
]


@dataclass(frozen=True)
class LEMRef(ContractMixin):
    """A namespace-qualified reference into one run-local LEM registry.

    The object id is deliberately not resolved here.  Resolution belongs to
    :class:`LivingEnterpriseModel`, where the declared namespace is enforced
    and identical text ids in different registries remain unambiguous.
    """

    namespace: LEMNamespace
    object_id: str

    NAMESPACES: ClassVar[frozenset[str]] = frozenset({
        "ontology", "prepared_asset", "canonical_mapping", "relationship", "knowledge_delta",
    })

    def __post_init__(self) -> None:
        namespace = str(self.namespace).strip()
        if namespace not in self.NAMESPACES:
            raise ValueError(f"unsupported LEM namespace: {namespace!r}")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "object_id", _require(self.object_id, "LEM object_id"))

    @property
    def id(self) -> str:
        return self.object_id

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LEMRef":
        return cls(namespace=data["namespace"], object_id=data["object_id"])


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
        if not isinstance(data, Mapping):
            raise TypeError("DataAssetRef.from_dict expects a mapping")
        from .references import decode_explicit_reference, is_explicit_reference_mapping
        if is_explicit_reference_mapping(data):
            value = decode_explicit_reference(data)
            if not isinstance(value, cls):
                raise TypeError(f"expected data_asset reference, got {type(value).__name__}")
            return value
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
        if self.content_hash is not None:
            digest = _require(self.content_hash, "content_hash").lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("content_hash must be a SHA-256 hex digest")
            object.__setattr__(self, "content_hash", digest)
        object.__setattr__(self, "format", self.format.lower().lstrip(".") if self.format else None)
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationResultRef":
        if not isinstance(data, Mapping):
            raise TypeError("OperationResultRef.from_dict expects a mapping")
        from .references import decode_explicit_reference, is_explicit_reference_mapping
        if is_explicit_reference_mapping(data):
            value = decode_explicit_reference(data)
            if not isinstance(value, cls):
                raise TypeError(f"expected operation_result reference, got {type(value).__name__}")
            return value
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
class RequirementAnalysisTask(ContractMixin):
    """One bounded internal analytical task in a parent requirement."""

    task_id: str
    question: str
    objective: str | None = None
    dependencies: tuple[str, ...] = ()
    expected_analytical_outputs: tuple[str, ...] = ()
    expected_visual_outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _require(self.task_id, "task_id"))
        object.__setattr__(self, "question", _require(self.question, "question"))
        if self.objective is not None:
            object.__setattr__(self, "objective", _require(self.objective, "objective"))
        for name in ("dependencies", "expected_analytical_outputs", "expected_visual_outputs"):
            values = tuple(_require(value, name) for value in getattr(self, name))
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
            object.__setattr__(self, name, values)

    @property
    def expected_outputs(self) -> Mapping[str, tuple[str, ...]]:
        return {
            "analytical": self.expected_analytical_outputs,
            "visual": self.expected_visual_outputs,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RequirementAnalysisTask":
        if not isinstance(data, Mapping):
            raise TypeError("requirement analysis task must be a mapping")
        raw = dict(data)
        raw.pop("record_kind", None)
        return cls(**raw)


@dataclass(frozen=True)
class RequirementAnalysisPlan(ContractMixin):
    """Immutable semantic decomposition owned by one requirement item."""

    tasks: tuple[RequirementAnalysisTask, ...]
    synthesis_intent: str
    original_text: str = ""

    @property
    def analysis_tasks(self) -> tuple[RequirementAnalysisTask, ...]:
        return self.tasks

    @property
    def output_intent(self) -> str:
        return self.synthesis_intent

    def __post_init__(self) -> None:
        tasks = tuple(
            RequirementAnalysisTask.from_dict(task) if isinstance(task, Mapping) else task
            for task in self.tasks
        )
        if not tasks:
            raise ValueError("requirement analysis plan requires at least one task")
        if any(not isinstance(task, RequirementAnalysisTask) for task in tasks):
            raise TypeError("requirement analysis plan tasks must be RequirementAnalysisTask values")
        ids = tuple(task.task_id for task in tasks)
        if len(ids) != len(set(ids)):
            raise ValueError("requirement analysis task IDs must be unique")
        known = set(ids)
        for task in tasks:
            unknown = set(task.dependencies) - known
            if unknown:
                raise ValueError("requirement analysis task dependency is unknown")
        graph = {task.task_id: set(task.dependencies) for task in tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("requirement analysis plan dependencies must be acyclic")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for task in tasks:
            visit(task.task_id)
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "synthesis_intent", _require(self.synthesis_intent, "synthesis_intent"))
        if self.original_text:
            object.__setattr__(self, "original_text", _require(self.original_text, "original_text"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "requirement_analysis_plan",
            "original_text": self.original_text,
            "tasks": [task.to_dict() for task in self.tasks],
            "synthesis_intent": self.synthesis_intent,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RequirementAnalysisPlan":
        if not isinstance(data, Mapping):
            raise TypeError("requirement analysis plan must be a mapping")
        raw = dict(data)
        raw.pop("record_kind", None)
        return cls(**raw)


@dataclass(frozen=True)
class RequirementRecord(ContractMixin):
    requirement_id: str
    original_text: str
    explicit_priority: Any = None
    business_objective: str = ""
    expected_analytical_outputs: tuple[str, ...] = ()
    expected_visual_outputs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
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
    review: Mapping[str, Any] = field(default_factory=dict)
    outcome: Any = None
    # ``priority`` and these names are direct fields for callers that use the
    # compact requirement vocabulary; canonical storage remains explicit above.
    priority: Any = None
    objective: str | None = None
    expected_outputs: Mapping[str, Any] = field(default_factory=dict)
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
        needs = dict(self.needs or {})
        data_needs = tuple(str(v) for v in (self.data_needs or needs.get("data", ())))
        ontology_needs = tuple(str(v) for v in (self.ontology_needs or needs.get("ontology", ())))
        prepared_needs = tuple(str(v) for v in (self.prepared_data_needs or needs.get("prepared", ())))
        object.__setattr__(self, "data_needs", data_needs)
        object.__setattr__(self, "ontology_needs", ontology_needs)
        object.__setattr__(self, "prepared_data_needs", prepared_needs)
        object.__setattr__(self, "needs", {"data": data_needs, "ontology": ontology_needs, "prepared": prepared_needs})
        object.__setattr__(self, "dependencies", _tuple_values(self.dependencies))
        object.__setattr__(self, "working_definitions", _tuple_values(self.working_definitions))
        limitations = tuple(self.limitations or self.limits)
        object.__setattr__(self, "limitations", _tuple_values(limitations))
        object.__setattr__(self, "limits", _tuple_values(limitations))
        if self.scope_classification:
            object.__setattr__(self, "scope", self.scope_classification)
        object.__setattr__(self, "scope_classification", self.scope)
        object.__setattr__(self, "source_refs", _tuple_values(self.source_refs))
        object.__setattr__(self, "evidence_refs", _tuple_values(self.evidence_refs))
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
    # Integrity and provenance fields are part of the descriptor rather than
    # an external cache key.  This keeps prepared-asset reuse run-local and
    # makes a descriptor sufficient to verify a materialized output.
    prepared_content_hash: str | None = None
    operation_manifest_hash: str | None = None
    core_version: str | None = None
    row_count: int | None = None
    byte_count: int | None = None
    created_at: str | None = None
    as_of: str | None = None
    effective_period: str | None = None

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
        for name in ("prepared_content_hash", "operation_manifest_hash"):
            value = getattr(self, name)
            if value is not None:
                value = _require(value, name).lower()
                if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise ValueError(f"{name} must be a SHA-256 hex digest")
                object.__setattr__(self, name, value)
        for name in ("row_count", "byte_count"):
            value = getattr(self, name)
            if value is not None:
                if isinstance(value, bool) or int(value) != value or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")
                object.__setattr__(self, name, int(value))
        for name in ("core_version", "created_at", "as_of", "effective_period"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require(value, name))

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
    supersedes: tuple[LEMRef, ...] = ()
    reviewer_note: str | None = None
    accepted: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    ALLOWED_OPERATIONS: ClassVar[frozenset[str]] = frozenset({
        "add_ontology_item", "extend_ontology_item", "add_alias", "add_canonical_mapping",
        "add_relationship", "add_metric", "add_definition", "add_rule", "add_process", "add_event", "add_dimension",
        "add_prepared_asset", "extend_prepared_asset", "record_limitation", "record_conflict",
        "supersede", "no_change",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "delta_id", _require(self.delta_id, "delta_id"))
        object.__setattr__(self, "operation", _require(self.operation, "operation"))
        if self.operation not in self.ALLOWED_OPERATIONS:
            raise ValueError(f"unsupported knowledge delta operation: {self.operation}")
        payload = dict(self.payload or {})
        legacy_targets = {"item_ids", "ontology_item_ids", "prepared_asset_ids"}
        present_legacy = sorted(legacy_targets.intersection(payload))
        if present_legacy:
            raise ValueError("knowledge delta target IDs must be typed LEMRef.supersedes; legacy payload keys: " + ", ".join(present_legacy))
        object.__setattr__(self, "payload", _freeze(payload))
        object.__setattr__(self, "evidence_refs", _tuple_values(self.evidence_refs))
        object.__setattr__(self, "conflicts_with", _tuple_values(self.conflicts_with))
        typed_refs: list[LEMRef] = []
        for target in (self.supersedes or ()):
            if isinstance(target, LEMRef):
                typed_refs.append(target)
            elif isinstance(target, Mapping):
                typed_refs.append(LEMRef.from_dict(target))
            else:
                raise TypeError("KnowledgeDelta.supersedes accepts LEMRef values only")
        object.__setattr__(self, "supersedes", tuple(typed_refs))
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
    """One reviewed, content-addressed identity decision.

    ``candidate_id`` and ``decision`` remain the first two positional fields;
    all review-trace fields are explicit and there is exactly one reviewer
    reference.  A caller may provide a stable ``decision_id``; otherwise it
    is derived deterministically from the decision subject and semantics.
    """

    candidate_id: str
    decision: str
    decision_id: str | None = None
    review_status: str = "pending"
    reviewer_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    rationale: str = ""
    scope: str | None = None
    limitations: tuple[str, ...] = ()
    canonical_id: str | None = None
    decision_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    VOCABULARY: ClassVar[frozenset[str]] = frozenset({
        "same_object", "different_objects", "possible_match", "insufficient_evidence",
        "version_of", "parent_child", "alternate_representation",
    })
    REVIEW_STATUSES: ClassVar[frozenset[str]] = frozenset({
        "pending", "reviewed", "accepted", "rejected",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _require(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "decision", _require(self.decision, "decision"))
        if self.decision not in self.VOCABULARY:
            raise ValueError(f"unsupported identity decision: {self.decision}")
        status = _require(self.review_status, "review_status").lower()
        if status not in self.REVIEW_STATUSES:
            raise ValueError(f"unsupported identity review_status: {status}")
        object.__setattr__(self, "review_status", status)
        if self.reviewer_ref is not None:
            object.__setattr__(self, "reviewer_ref", _require(self.reviewer_ref, "reviewer_ref"))
        if self.decision_id is None:
            seed = json.dumps({
                "candidate_id": self.candidate_id,
                "decision": self.decision,
                "scope": self.scope,
                "reviewer_ref": self.reviewer_ref,
            }, sort_keys=True, separators=(",", ":"))
            object.__setattr__(self, "decision_id", "decision-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20])
        else:
            object.__setattr__(self, "decision_id", _require(self.decision_id, "decision_id"))
        evidence_refs = (self.evidence_refs,) if isinstance(self.evidence_refs, str) else _tuple_values(self.evidence_refs)
        object.__setattr__(self, "evidence_refs", tuple(str(value) for value in evidence_refs))
        object.__setattr__(self, "rationale", str(self.rationale or ""))
        if self.scope is not None:
            object.__setattr__(self, "scope", _require(self.scope, "scope"))
        limitations = (self.limitations,) if isinstance(self.limitations, str) else self.limitations
        object.__setattr__(self, "limitations", tuple(str(v) for v in limitations))
        trace = {
            "decision_id": self.decision_id,
            "candidate_id": self.candidate_id,
            "decision": self.decision,
            "review_status": self.review_status,
            "reviewer_ref": self.reviewer_ref,
            "evidence_refs": list(self.evidence_refs),
            "rationale": self.rationale,
            "scope": self.scope,
            "limitations": list(self.limitations),
            "canonical_id": self.canonical_id,
        }
        digest = hashlib.sha256(json.dumps(trace, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        object.__setattr__(self, "decision_hash", digest)
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
    scope: str | None = None
    effective_period: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("canonical_id", "object_type", "decision_id"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        source_identities = (self.source_identities,) if isinstance(self.source_identities, str) else _tuple_values(self.source_identities)
        aliases = (self.aliases,) if isinstance(self.aliases, str) else _tuple_values(self.aliases)
        object.__setattr__(self, "source_identities", tuple(str(value) for value in source_identities))
        object.__setattr__(self, "aliases", tuple(str(value) for value in aliases))
        object.__setattr__(self, "limitations", _tuple_values(self.limitations))
        if self.scope is not None:
            object.__setattr__(self, "scope", _require(self.scope, "scope"))
        if self.effective_period is not None:
            object.__setattr__(self, "effective_period", _require(self.effective_period, "effective_period"))
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
class PhaseTimingRecord(ContractMixin):
    """Observed timing for one program phase.

    Timing is deliberately facts-only.  ``start``, ``finish`` and
    ``wall_time_ms`` remain ``None`` when the host did not supply that fact;
    constructors never fill missing observations with a clock reading or a
    zero duration.
    """

    phase: str
    start: str | None = None
    finish: str | None = None
    wall_time_ms: float | None = None
    item_id: str | None = None
    attempt_id: str | None = None
    provider: str | None = None
    model: str | None = None
    receipt_ref: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        phase = _require(self.phase, "phase")
        object.__setattr__(self, "phase", phase)
        for name in ("start", "finish", "item_id", "attempt_id", "provider", "model", "receipt_ref"):
            value = getattr(self, name)
            if value is not None:
                value = str(value).strip()
                object.__setattr__(self, name, value or None)
        if self.wall_time_ms is not None:
            if isinstance(self.wall_time_ms, bool) or not isinstance(self.wall_time_ms, (int, float)):
                raise TypeError("wall_time_ms must be a number or None")
            if self.wall_time_ms < 0:
                raise ValueError("wall_time_ms cannot be negative")
            object.__setattr__(self, "wall_time_ms", float(self.wall_time_ms))
        object.__setattr__(self, "facts", _freeze(self.facts))

    @property
    def wall_ms(self) -> float | None:
        """Short alias used by report consumers."""

        return self.wall_time_ms


@dataclass(frozen=True)
class IncidentRecord(ContractMixin):
    """Normalized reviewer/program/recovery/metadata incident metadata."""

    incident_id: str
    category: str
    disposition: str
    admissible: bool
    item_id: str | None = None
    scope: tuple[str, ...] = ()
    source: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)

    CATEGORIES: ClassVar[frozenset[str]] = frozenset(
        {"reviewer_scope", "program", "recovery", "metadata"}
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "incident_id", _require(self.incident_id, "incident_id"))
        category = _require(self.category, "category").lower().replace("-", "_").replace(" ", "_")
        category = {
            "review": "reviewer_scope",
            "reviewer": "reviewer_scope",
            "execution_recovery": "recovery",
            "program_defect": "program",
            "metadata_defect": "metadata",
        }.get(category, category)
        if category not in self.CATEGORIES:
            raise ValueError(f"unsupported incident category: {category}")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "disposition", _require(self.disposition, "disposition"))
        if not isinstance(self.admissible, bool):
            raise TypeError("admissible must be a bool")
        if self.item_id is not None:
            object.__setattr__(self, "item_id", _require(self.item_id, "item_id"))
        object.__setattr__(self, "scope", tuple(_require(value, "scope") for value in self.scope))
        if self.source is not None:
            object.__setattr__(self, "source", _require(self.source, "source"))
        object.__setattr__(self, "facts", _freeze(self.facts))


@dataclass(frozen=True)
class ImplementationTransition(ContractMixin):
    """Explicit implementation patch and resumable-run checkpoint facts."""

    old_sha: str
    new_sha: str
    old_tree: str
    new_tree: str
    old_version: str
    new_version: str
    earliest_affected_item: str
    preserved_accepted_hashes: Mapping[str, str]
    unaffected_reason: str
    resume_point: str
    transition_id: str | None = None

    def __post_init__(self) -> None:
        sha_fields = ("old_sha", "new_sha", "old_tree", "new_tree")
        for name in sha_fields:
            value = _require(getattr(self, name), name).lower()
            if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be exactly 40 lowercase hexadecimal characters")
            object.__setattr__(self, name, value)
        for name in ("old_version", "new_version", "earliest_affected_item", "unaffected_reason", "resume_point"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        preserved = dict(self.preserved_accepted_hashes or {})
        for item_id, digest in preserved.items():
            item_id = _require(item_id, "preserved accepted item_id")
            digest = _require(digest, "preserved accepted hash").lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("preserved accepted hashes must be SHA-256 digests")
        object.__setattr__(self, "preserved_accepted_hashes", MappingProxyType(dict(sorted(preserved.items()))))
        if self.transition_id is not None:
            object.__setattr__(self, "transition_id", _require(self.transition_id, "transition_id"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImplementationTransition":
        if not isinstance(data, Mapping):
            raise TypeError("ImplementationTransition.from_dict expects a mapping")
        return cls(**dict(data))


@dataclass(frozen=True)
class AggregationSpec(ContractMixin):
    operation: str
    value_field: str | None = None
    group_by: tuple[str, ...] = ()
    period_field: str | None = None
    period_order: tuple[str, ...] = ()
    # Share/rate semantics are explicit values, matching aggregate_rows.
    numerator: Any = None
    denominator: Any = None
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
        object.__setattr__(self, "period_order", tuple(str(v) for v in self.period_order))
        if self.operation == "period_comparison" and (not self.period_field or not self.period_order):
            raise ValueError("period_comparison requires period_field and explicit period_order")
        parameters = self.parameters or {}
        if self.operation in {"share", "rate"}:
            has_numerator = self.numerator is not None or parameters.get("numerator") is not None
            has_denominator = self.denominator is not None or parameters.get("denominator") is not None
            if not (has_numerator and has_denominator):
                raise ValueError(f"{self.operation} requires explicit numerator and denominator semantics")
        if self.limit is not None and (isinstance(self.limit, bool) or self.limit < 0):
            raise ValueError("aggregation limit cannot be negative")
        object.__setattr__(self, "parameters", _freeze(self.parameters))
