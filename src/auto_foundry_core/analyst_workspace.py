"""A small semantic facade for analyst-owned work.

The deterministic core still owns files, hashes, receipts, review scopes, and
lifecycle state.  This module exposes business-shaped operations so an
analytical agent can investigate, delegate bounded questions, and submit a
coherent answer without constructing core JSON paths or authority records.
It never invokes a model or chooses an analytical conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .analysis import BoundAnalysisContext, ScriptRunReport
from .contracts import (
    CanonicalMapping,
    IdentityDecision,
    OntologyItem,
    PreparedAssetDescriptor,
    RequirementAnalysisPlan,
    RequirementAnalysisTask,
)
from .durable import AcceptedSnapshot, ItemWorkspace
from .lifecycle import RunLifecycle
from .mapping_view import IdentityMappingView
from .semantic_store import SemanticSnapshotRef, SemanticSnapshotStore
from .workbench import DataRoomCatalogEntry, DataRoomMember


_ANSWER_SECTION_ORDER = (
    "answer",
    "headline_findings",
    "scope",
    "method",
    "supported_components",
    "unsupported_components",
    "limitations",
    "next_actions",
    "visuals",
    "evidence_refs",
)
_ANSWER_SECTIONS = frozenset(_ANSWER_SECTION_ORDER)
_REVIEW_CATEGORY_ORDER = (
    "answer",
    "calculation",
    "evidence",
    "method",
    "source_completeness",
    "presentation",
)
_REVIEW_CATEGORIES = frozenset(_REVIEW_CATEGORY_ORDER)
_EVIDENCE_BINDING_CATEGORIES = frozenset({"evidence", "source_completeness"})
# A visual repair that recomputes a result may publish a new evidence record
# for that visual.  Keep the evidence-reference pointer derived only for the
# typed calculation+presentation visual combination; ordinary calculation,
# presentation, and evidence repairs retain their narrower scopes.
_VISUAL_EVIDENCE_BINDING_CATEGORIES = frozenset({"calculation", "presentation"})
_SPECIALTIES = frozenset(
    {"data_quality", "metric_method", "business_context", "process", "documents", "custom"}
)
_CONFIDENCE = frozenset({"high", "medium", "low", "unknown"})
_EVIDENCE_FILENAME = "evidence.jsonl"
_SPECIALIST_TASKS_FILENAME = "specialist_tasks.jsonl"
_SPECIALIST_MEMOS_FILENAME = "specialist_memos.jsonl"
_REQUIREMENT_PLAN_FILENAME = "requirement_plan.json"
_REQUIREMENT_PLAN_REVISIONS_DIRNAME = "requirement_plan_revisions"
_REQUIREMENT_PLAN_REVISION_KIND = "requirement_plan_revision"
_REQUIREMENT_PLAN_REVISION_SCHEMA = "auto_foundry.requirement_plan_revision.v1"
_REQUIREMENT_PLAN_REVISION_NAME = re.compile(r"^rev-(\d{4,})\.json$")
_REQUIREMENT_PLAN_REVISION_FIELDS = frozenset(
    {
        "kind",
        "schema",
        "item_id",
        "revision",
        "plan_payload",
        "plan_hash",
        "parent_plan_hash",
        "analysis_context_manifest_hash",
        "active_generation_id",
        "active_generation_manifest_hash",
        "created_at",
        "record_hash",
    }
)
_MISSING_EXPECTED_PLAN_HEAD = object()
_SEMANTIC_SELECTIONS_FILENAME = "semantic_selections.jsonl"
_IDENTITY_DOMAIN_PROPOSALS_FILENAME = "identity_domain_proposals.jsonl"
_IDENTITY_DOMAIN_PROPOSAL_OPTIONAL_FIELDS = frozenset(
    {"revision", "supersedes_hash", "proposal_hash", "superseded_object_type"}
)
_ANALYTICAL_RELATIONSHIPS_FILENAME = "analytical_relationships.jsonl"
_RELATIONSHIP_CARDINALITIES = frozenset(
    {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
)


def _required_text(value: Any, *, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    return text


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name=field_name)


def _text_tuple(values: Iterable[Any], *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of strings")
    result = tuple(_required_text(value, field_name=field_name) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _strict_json(value: Any, *, field_name: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be strict JSON") from exc


def _canonical_hash(value: Any) -> str:
    def native(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): native(value) for key, value in item.items()}
        if isinstance(item, (list, tuple, set, frozenset)):
            return [native(value) for value in item]
        return item

    encoded = json.dumps(native(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def validate_analytical_relationship_measurement(
    *,
    cardinality: str,
    matched_pairs: Any,
    source_population: Any,
    target_population: Any,
    matched_source_count: Any,
    matched_target_count: Any,
    source_coverage: Any,
    target_coverage: Any,
    publishable: bool = True,
) -> None:
    """Validate exact edge and distinct-endpoint relationship measurements.

    ``matched_pairs`` counts unique tested source/target edges.  The two
    ``matched_*_count`` values count distinct source and target endpoints, so
    one-to-many and many-to-one joins are represented without pretending that
    an edge count is a population count.  Coverage is endpoint coverage and
    is defined as zero when its population is zero.
    """

    allowed_cardinalities = _RELATIONSHIP_CARDINALITIES | {"none"}
    if not isinstance(cardinality, str) or cardinality not in allowed_cardinalities:
        raise ValueError(f"analytical relationship cardinality is unsupported: {cardinality!r}")
    if publishable and cardinality not in _RELATIONSHIP_CARDINALITIES:
        raise ValueError(f"publishable analytical relationships require supported cardinality: {cardinality!r}")

    values = {
        "matched_pairs": matched_pairs,
        "source_population": source_population,
        "target_population": target_population,
        "matched_source_count": matched_source_count,
        "matched_target_count": matched_target_count,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"analytical relationship {name} must be a non-negative integer")
    source_coverage_values = {"source_coverage": source_coverage, "target_coverage": target_coverage}
    for name, value in source_coverage_values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"analytical relationship {name} must be numeric")
        if not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
            raise ValueError(f"analytical relationship {name} must be between zero and one")

    if matched_source_count > source_population:
        raise ValueError("analytical relationship matched_source_count cannot exceed source_population")
    if matched_target_count > target_population:
        raise ValueError("analytical relationship matched_target_count cannot exceed target_population")
    if matched_pairs == 0:
        if matched_source_count != 0 or matched_target_count != 0:
            raise ValueError("analytical relationship matched_pairs=0 requires both matched endpoint counts to be zero")
    elif matched_source_count == 0 or matched_target_count == 0:
        raise ValueError("analytical relationship positive matched_pairs require positive endpoint counts")
    elif matched_pairs < matched_source_count or matched_pairs < matched_target_count:
        raise ValueError("analytical relationship matched_pairs cannot be below either matched endpoint count")
    if matched_pairs > matched_source_count * matched_target_count:
        raise ValueError(
            "analytical relationship matched_pairs cannot exceed the matched endpoint Cartesian bound"
        )

    expected_source_coverage = 0.0 if source_population == 0 else matched_source_count / source_population
    expected_target_coverage = 0.0 if target_population == 0 else matched_target_count / target_population
    if float(source_coverage) != expected_source_coverage:
        raise ValueError("analytical relationship source_coverage is inconsistent with matched_source_count/source_population")
    if float(target_coverage) != expected_target_coverage:
        raise ValueError("analytical relationship target_coverage is inconsistent with matched_target_count/target_population")

    if cardinality == "one_to_one":
        if not (matched_pairs == matched_source_count == matched_target_count):
            raise ValueError("one_to_one analytical relationships require all matched counts to be equal")
    elif cardinality == "one_to_many":
        if matched_pairs != matched_target_count or matched_pairs < matched_source_count:
            raise ValueError("one_to_many analytical relationships require matched_pairs=matched_target_count >= matched_source_count")
    elif cardinality == "many_to_one":
        if matched_pairs != matched_source_count or matched_pairs < matched_target_count:
            raise ValueError("many_to_one analytical relationships require matched_pairs=matched_source_count >= matched_target_count")
    elif cardinality == "many_to_many":
        if matched_pairs < matched_source_count or matched_pairs < matched_target_count:
            raise ValueError("many_to_many analytical relationships require matched_pairs >= both matched endpoint counts")


def _source_id(entry: DataRoomCatalogEntry) -> str:
    return entry.source_id


@dataclass(frozen=True)
class AnalystSource:
    source_id: str
    path: str
    kind: str
    format: str
    columns: tuple[str, ...]
    row_count: int | None
    row_count_exact: bool
    table_name: str | None = None
    sheet_name: str | None = None
    size_bytes: int | None = None

    @classmethod
    def from_entry(cls, entry: DataRoomCatalogEntry) -> "AnalystSource":
        return cls(
            source_id=_source_id(entry),
            path=entry.path,
            kind=entry.kind,
            format=entry.format,
            columns=tuple(entry.columns),
            row_count=entry.row_count,
            row_count_exact=entry.row_count_exact,
            table_name=entry.table_name,
            sheet_name=entry.sheet_name,
            size_bytes=entry.size_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "path": self.path,
            "kind": self.kind,
            "format": self.format,
            "columns": list(self.columns),
            "row_count": self.row_count,
            "row_count_exact": self.row_count_exact,
            "table_name": self.table_name,
            "sheet_name": self.sheet_name,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class AnalystBrief:
    item_id: str
    mode: str
    question: str
    catalog_entry_count: int
    quality_criteria: tuple[str, ...]
    available_specialties: tuple[str, ...]
    requirement_plan: Any = None
    ontology_item_count: int = 0
    ontology_relationship_count: int = 0
    prepared_asset_count: int = 0
    has_ontology: bool = False
    has_relationships: bool = False
    has_prepared_assets: bool = False

    @property
    def ontology_count(self) -> int:
        """Short alias for the number of reusable ontology entries."""

        return self.ontology_item_count

    @property
    def relationship_count(self) -> int:
        return self.ontology_relationship_count

    @property
    def prepared_assets_count(self) -> int:
        return self.prepared_asset_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "mode": self.mode,
            "question": self.question,
            "catalog_entry_count": self.catalog_entry_count,
            "quality_criteria": list(self.quality_criteria),
            "available_specialties": list(self.available_specialties),
            "requirement_plan": self.requirement_plan.to_dict() if self.requirement_plan is not None else None,
            "ontology_item_count": self.ontology_item_count,
            "ontology_relationship_count": self.ontology_relationship_count,
            "prepared_asset_count": self.prepared_asset_count,
            "has_ontology": self.has_ontology,
            "has_relationships": self.has_relationships,
            "has_prepared_assets": self.has_prepared_assets,
        }


@dataclass(frozen=True)
class IdentityDomainProposal:
    """Analytical Owner proposal for one arbitrary identity domain.

    This is intentionally a proposal rather than a reservation or an
    accepted entity mapping.  The runtime/entity-resolution owner decides
    whether the domain can be admitted and when it is released.
    """

    domain_id: str
    object_type: str
    rationale: str
    source_hints: tuple[str, ...]
    representation_item_ids: tuple[str, ...]
    # Proposal revisions are item-local append-only successors.  Legacy rows
    # omit these fields and are interpreted as revision one without rewriting
    # their original bytes.
    revision: int = 1
    supersedes_hash: str | None = None
    proposal_hash: str | None = None
    # The immediate predecessor's requested type is retained as audit
    # metadata.  The effective successor type is still checked by the
    # run-level Entity Resolution reservation boundary.
    superseded_object_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_id", _required_text(self.domain_id, field_name="domain_id"))
        object.__setattr__(self, "object_type", _required_text(self.object_type, field_name="object_type"))
        object.__setattr__(self, "rationale", _required_text(self.rationale, field_name="rationale"))
        source_hints = _text_tuple(self.source_hints, field_name="source_hints")
        representation_item_ids = _text_tuple(
            self.representation_item_ids,
            field_name="representation_item_ids",
        )
        if not source_hints:
            raise ValueError("source_hints cannot be empty")
        if not representation_item_ids:
            raise ValueError("representation_item_ids cannot be empty")
        object.__setattr__(self, "source_hints", source_hints)
        object.__setattr__(self, "representation_item_ids", representation_item_ids)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        supersedes_hash = _optional_text(self.supersedes_hash, field_name="supersedes_hash")
        if self.revision == 1 and supersedes_hash is not None:
            raise ValueError("revision one cannot supersede a predecessor")
        if self.revision > 1 and not _is_sha256(supersedes_hash):
            raise ValueError("successor revision requires a valid supersedes_hash")
        object.__setattr__(self, "supersedes_hash", supersedes_hash)
        superseded_object_type = _optional_text(
            self.superseded_object_type,
            field_name="superseded_object_type",
        )
        if self.revision == 1 and superseded_object_type is not None:
            raise ValueError("revision one cannot carry superseded_object_type")
        if self.revision > 1 and superseded_object_type is None:
            raise ValueError("successor revision requires superseded_object_type")
        object.__setattr__(self, "superseded_object_type", superseded_object_type)
        proposal_hash = _optional_text(self.proposal_hash, field_name="proposal_hash")
        if proposal_hash is not None:
            if not _is_sha256(proposal_hash):
                raise ValueError("proposal_hash must be a SHA-256 digest")
            if proposal_hash != _canonical_hash(self._hash_payload()):
                raise ValueError("proposal_hash does not match the canonical proposal")
        object.__setattr__(self, "proposal_hash", proposal_hash)

    def _hash_payload(self) -> dict[str, Any]:
        """Return the canonical proposal payload covered by ``proposal_hash``."""

        payload: dict[str, Any] = {
            "record_kind": "identity_domain_proposal",
            "domain_id": self.domain_id,
            "object_type": self.object_type,
            "rationale": self.rationale,
            "source_hints": list(self.source_hints),
            "representation_item_ids": list(self.representation_item_ids),
        }
        if self.revision != 1:
            payload.update(
                {
                    "revision": self.revision,
                    "supersedes_hash": self.supersedes_hash,
                    "superseded_object_type": self.superseded_object_type,
                }
            )
        return payload

    @property
    def digest(self) -> str:
        """Return the stable predecessor digest used by revision CAS."""

        return self.proposal_hash or _canonical_hash(self._hash_payload())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "record_kind": "identity_domain_proposal",
            "domain_id": self.domain_id,
            "object_type": self.object_type,
            "rationale": self.rationale,
            "source_hints": list(self.source_hints),
            "representation_item_ids": list(self.representation_item_ids),
        }
        if self.revision != 1:
            payload.update(
                {
                    "revision": self.revision,
                    "supersedes_hash": self.supersedes_hash,
                    "superseded_object_type": self.superseded_object_type,
                }
            )
        if self.proposal_hash is not None:
            payload["proposal_hash"] = self.proposal_hash
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IdentityDomainProposal":
        if not isinstance(value, Mapping):
            raise TypeError("identity domain proposal must be a mapping")
        raw = dict(value)
        record_kind = raw.pop("record_kind", None)
        raw.pop("item_id", None)
        raw.pop("owner_ref", None)
        result = cls(**raw)
        if record_kind is not None and record_kind != result.to_dict()["record_kind"]:
            raise ValueError("identity domain proposal record_kind is not canonical")
        return result


@dataclass(frozen=True)
class AnalyticalRelationshipEvidence:
    """Evidence for an observed relationship or an explicit no-join audit."""

    relationship_id: str
    source_id: str
    target_id: str
    cardinality: str = "none"
    join_keys: tuple[Mapping[str, str], ...] = ()
    matched_pairs: int | None = None
    source_population: int | None = None
    target_population: int | None = None
    matched_source_count: int | None = None
    matched_target_count: int | None = None
    source_coverage: float | int | None = None
    target_coverage: float | int | None = None
    date_authority: str | None = None
    as_of: str | None = None
    limitations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    publishable: bool = False
    no_relationship_reason: str | None = None
    audit_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "relationship_id", _required_text(self.relationship_id, field_name="relationship_id"))
        object.__setattr__(self, "source_id", _required_text(self.source_id, field_name="source_id"))
        object.__setattr__(self, "target_id", _required_text(self.target_id, field_name="target_id"))
        object.__setattr__(self, "cardinality", _required_text(self.cardinality, field_name="cardinality"))
        if isinstance(self.join_keys, (str, bytes)):
            raise TypeError("join_keys must be an iterable of source_field/target_field mappings")
        try:
            raw_join_keys = tuple(self.join_keys)
        except TypeError as exc:
            raise TypeError("join_keys must be an iterable of source_field/target_field mappings") from exc
        normalized_join_keys: list[Mapping[str, str]] = []
        for index, value in enumerate(raw_join_keys):
            if not isinstance(value, Mapping):
                raise TypeError(f"join_keys[{index}] must be a mapping")
            if set(value) != {"source_field", "target_field"}:
                raise ValueError("join_keys entries must contain exactly source_field and target_field")
            normalized_join_keys.append(
                MappingProxyType(
                    {
                        "source_field": _required_text(value["source_field"], field_name="source_field"),
                        "target_field": _required_text(value["target_field"], field_name="target_field"),
                    }
                )
            )
        object.__setattr__(self, "join_keys", tuple(normalized_join_keys))
        measurement_names = (
            "matched_pairs",
            "source_population",
            "target_population",
            "matched_source_count",
            "matched_target_count",
        )
        coverage_names = ("source_coverage", "target_coverage")
        for name in ("date_authority", "as_of", "no_relationship_reason", "audit_id"):
            value = getattr(self, name)
            if value is not None:
                if name in {"date_authority", "as_of"} and not isinstance(value, str):
                    raise TypeError(f"{name} must be a string when known")
                object.__setattr__(self, name, _required_text(value, field_name=name))
        object.__setattr__(self, "limitations", _text_tuple(self.limitations, field_name="limitations"))
        object.__setattr__(self, "evidence_refs", _text_tuple(self.evidence_refs, field_name="evidence_refs"))
        if not isinstance(self.publishable, bool):
            raise TypeError("publishable must be boolean")
        has_no_relationship_reason = self.no_relationship_reason is not None
        if self.publishable == has_no_relationship_reason:
            raise ValueError("relationship evidence must be publishable or carry no_relationship_reason")
        if has_no_relationship_reason:
            if self.cardinality != "none":
                raise ValueError("relationship audits require cardinality=none")
            if self.join_keys:
                raise ValueError("relationship audits cannot contain join_keys")
            measurement_values = tuple(getattr(self, name) for name in (*measurement_names, *coverage_names))
            if any(value is not None for value in measurement_values):
                if any(value is None for value in measurement_values):
                    raise ValueError("relationship audits may omit measurements or carry all exact zeros")
                validate_analytical_relationship_measurement(
                    cardinality="none",
                    matched_pairs=self.matched_pairs,
                    source_population=self.source_population,
                    target_population=self.target_population,
                    matched_source_count=self.matched_source_count,
                    matched_target_count=self.matched_target_count,
                    source_coverage=self.source_coverage,
                    target_coverage=self.target_coverage,
                    publishable=False,
                )
                if any(
                    value != 0
                    for value in (
                        self.matched_pairs,
                        self.source_population,
                        self.target_population,
                        self.matched_source_count,
                        self.matched_target_count,
                        self.source_coverage,
                        self.target_coverage,
                    )
                ):
                    raise ValueError("relationship audits may carry only exact zero measurements")
        else:
            validate_analytical_relationship_measurement(
                cardinality=self.cardinality,
                matched_pairs=self.matched_pairs,
                source_population=self.source_population,
                target_population=self.target_population,
                matched_source_count=self.matched_source_count,
                matched_target_count=self.matched_target_count,
                source_coverage=self.source_coverage,
                target_coverage=self.target_coverage,
                publishable=True,
            )
            if not self.date_authority and not self.as_of:
                raise ValueError("publishable analytical relationship requires date_authority or as_of")
            if not self.join_keys:
                raise ValueError("publishable analytical relationships require join_keys")
            if not self.evidence_refs:
                raise ValueError("publishable analytical relationships require evidence_refs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "relationship_audit" if self.no_relationship_reason is not None else "analytical_relationship",
            "relationship_id": self.relationship_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "cardinality": self.cardinality,
            "join_keys": [dict(value) for value in self.join_keys],
            "matched_pairs": self.matched_pairs,
            "source_population": self.source_population,
            "target_population": self.target_population,
            "matched_source_count": self.matched_source_count,
            "matched_target_count": self.matched_target_count,
            "source_coverage": self.source_coverage,
            "target_coverage": self.target_coverage,
            "date_authority": self.date_authority,
            "as_of": self.as_of,
            "limitations": list(self.limitations),
            "evidence_refs": list(self.evidence_refs),
            "publishable": self.publishable,
            "no_relationship_reason": self.no_relationship_reason,
            "audit_id": self.audit_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnalyticalRelationshipEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("analytical relationship must be a mapping")
        raw = dict(value)
        record_kind = raw.pop("record_kind", None)
        raw.pop("item_id", None)
        raw.pop("owner_ref", None)
        result = cls(**raw)
        if record_kind is not None and record_kind != result.to_dict()["record_kind"]:
            raise ValueError("analytical relationship record_kind is not canonical")
        return result


@dataclass(frozen=True)
class EvidenceNote:
    evidence_id: str
    conclusion: str
    method: str
    evidence_refs: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _required_text(self.evidence_id, field_name="evidence_id"))
        object.__setattr__(self, "conclusion", _required_text(self.conclusion, field_name="conclusion"))
        object.__setattr__(self, "method", _required_text(self.method, field_name="method"))
        evidence_refs = _text_tuple(self.evidence_refs, field_name="evidence_refs")
        if not evidence_refs:
            raise ValueError("evidence_refs cannot be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "limitations", _text_tuple(self.limitations, field_name="limitations"))
        facts = _strict_json(dict(self.facts), field_name="facts")
        if not isinstance(facts, dict):
            raise ValueError("facts must be an object")
        object.__setattr__(self, "facts", MappingProxyType(facts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "analytical_evidence",
            "evidence_id": self.evidence_id,
            "conclusion": self.conclusion,
            "method": self.method,
            "evidence_refs": list(self.evidence_refs),
            "limitations": list(self.limitations),
            "facts": dict(self.facts),
        }


@dataclass(frozen=True)
class SpecialistTask:
    task_id: str
    specialty: str
    question: str
    expected_output: str
    source_ids: tuple[str, ...] = ()
    context: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _required_text(self.task_id, field_name="task_id"))
        specialty = _required_text(self.specialty, field_name="specialty")
        if specialty not in _SPECIALTIES:
            raise ValueError("specialty is invalid")
        object.__setattr__(self, "specialty", specialty)
        object.__setattr__(self, "question", _required_text(self.question, field_name="question"))
        object.__setattr__(self, "expected_output", _required_text(self.expected_output, field_name="expected_output"))
        source_ids = _text_tuple(self.source_ids, field_name="source_ids")
        if not source_ids:
            raise ValueError("specialist source_ids cannot be empty")
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(self, "context", _optional_text(self.context, field_name="context"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "specialist_task",
            "task_id": self.task_id,
            "specialty": self.specialty,
            "question": self.question,
            "expected_output": self.expected_output,
            "source_ids": list(self.source_ids),
            "context": self.context,
        }


@dataclass(frozen=True)
class SpecialistMemo:
    memo_id: str
    task_id: str
    conclusion: str
    method: str
    evidence_refs: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    confidence: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "memo_id", _required_text(self.memo_id, field_name="memo_id"))
        object.__setattr__(self, "task_id", _required_text(self.task_id, field_name="task_id"))
        object.__setattr__(self, "conclusion", _required_text(self.conclusion, field_name="conclusion"))
        object.__setattr__(self, "method", _required_text(self.method, field_name="method"))
        evidence_refs = _text_tuple(self.evidence_refs, field_name="evidence_refs")
        if not evidence_refs:
            raise ValueError("specialist evidence_refs cannot be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "limitations", _text_tuple(self.limitations, field_name="limitations"))
        object.__setattr__(self, "open_questions", _text_tuple(self.open_questions, field_name="open_questions"))
        confidence = _required_text(self.confidence, field_name="confidence")
        if confidence not in _CONFIDENCE:
            raise ValueError("confidence is invalid")
        object.__setattr__(self, "confidence", confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "specialist_memo",
            "memo_id": self.memo_id,
            "task_id": self.task_id,
            "conclusion": self.conclusion,
            "method": self.method,
            "evidence_refs": list(self.evidence_refs),
            "limitations": list(self.limitations),
            "open_questions": list(self.open_questions),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class AnalystAnswer:
    answer: str
    headline_findings: tuple[str, ...] = ()
    scope: str | None = None
    method: str | None = None
    supported_components: tuple[str, ...] = ()
    unsupported_components: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    visuals: tuple[Mapping[str, Any], ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "answer", _required_text(self.answer, field_name="answer"))
        for name in (
            "headline_findings",
            "supported_components",
            "unsupported_components",
            "limitations",
            "next_actions",
            "evidence_refs",
        ):
            object.__setattr__(self, name, _text_tuple(getattr(self, name), field_name=name))
        object.__setattr__(self, "scope", _optional_text(self.scope, field_name="scope"))
        object.__setattr__(self, "method", _optional_text(self.method, field_name="method"))
        visuals = _strict_json([dict(value) for value in self.visuals], field_name="visuals")
        if not isinstance(visuals, list) or any(not isinstance(value, dict) for value in visuals):
            raise ValueError("visuals must contain objects")
        object.__setattr__(self, "visuals", tuple(MappingProxyType(value) for value in visuals))

    def to_dict(self, *, item_id: str) -> dict[str, Any]:
        return {
            "schema_version": "auto_foundry.analyst_answer.v1",
            "item_id": item_id,
            "answer": self.answer,
            "headline_findings": list(self.headline_findings),
            "scope": self.scope,
            "method": self.method,
            "supported_components": list(self.supported_components),
            "unsupported_components": list(self.unsupported_components),
            "limitations": list(self.limitations),
            "next_actions": list(self.next_actions),
            "visuals": [dict(value) for value in self.visuals],
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, init=False)
class DataInsufficiencyConclusion:
    """Semantic conclusion that a material answer component lacks evidence."""

    reason: str
    unanswerable_component: str
    missing_information: tuple[str, ...]
    searches_performed: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    supported_components: tuple[str, ...] = ()

    def __init__(
        self,
        reason: str,
        unanswerable_component: str | None = None,
        missing_information: Iterable[str] | None = None,
        searches_performed: Iterable[str] | None = None,
        evidence_refs: Iterable[str] = (),
        supported_components: Iterable[str] = (),
        *,
        direct_answer_component: str | None = None,
        missing_data: Iterable[str] | None = None,
        searches_and_tests: Iterable[str] | None = None,
        searches_tests: Iterable[str] | None = None,
    ) -> None:
        if unanswerable_component is None:
            unanswerable_component = direct_answer_component
        if missing_information is None:
            missing_information = missing_data
        if searches_performed is None:
            searches_performed = searches_and_tests if searches_and_tests is not None else searches_tests
        if unanswerable_component is None or missing_information is None or searches_performed is None:
            raise TypeError(
                "DataInsufficiencyConclusion requires an unanswerable component, missing information, and searches"
            )
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "unanswerable_component", unanswerable_component)
        object.__setattr__(self, "missing_information", tuple(missing_information))
        object.__setattr__(self, "searches_performed", tuple(searches_performed))
        object.__setattr__(self, "evidence_refs", tuple(evidence_refs))
        object.__setattr__(self, "supported_components", tuple(supported_components))
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _required_text(self.reason, field_name="reason"))
        object.__setattr__(
            self,
            "unanswerable_component",
            _required_text(self.unanswerable_component, field_name="unanswerable_component"),
        )
        for name in ("missing_information", "searches_performed", "evidence_refs", "supported_components"):
            values = _text_tuple(getattr(self, name), field_name=name)
            if name != "supported_components" and not values:
                raise ValueError(f"{name} cannot be empty")
            object.__setattr__(self, name, values)

    @property
    def direct_answer_component(self) -> str:
        """Readable alias for the component that cannot be answered."""

        return self.unanswerable_component

    @property
    def missing_data(self) -> tuple[str, ...]:
        return self.missing_information

    @property
    def searches_and_tests(self) -> tuple[str, ...]:
        return self.searches_performed

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "unanswerable_component": self.unanswerable_component,
            "missing_information": list(self.missing_information),
            "searches_performed": list(self.searches_performed),
            "evidence_refs": list(self.evidence_refs),
            "supported_components": list(self.supported_components),
        }


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    target_sections: tuple[str, ...]
    semantic_categories: tuple[str, ...]
    problem: str
    evidence: str
    required_change: str
    material: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _required_text(self.finding_id, field_name="finding_id"))
        raw_sections = self.target_sections
        if isinstance(raw_sections, (str, bytes)):
            raise TypeError("target_sections must be a nonempty iterable of semantic section names")
        if raw_sections is None:
            raise ValueError("target_sections must be non-empty")
        try:
            supplied = tuple(_required_text(value, field_name="target_sections") for value in raw_sections)
        except TypeError as exc:
            raise TypeError("target_sections must be a nonempty iterable of semantic section names") from exc
        if not supplied:
            raise ValueError("target_sections must be non-empty")
        if len(supplied) != len(set(supplied)):
            raise ValueError("target_sections must not contain duplicates")
        invalid = tuple(section for section in supplied if section not in _ANSWER_SECTIONS)
        if invalid:
            raise ValueError("target_sections contains an invalid semantic section")
        canonical_sections = tuple(section for section in _ANSWER_SECTION_ORDER if section in supplied)
        object.__setattr__(self, "target_sections", canonical_sections)
        raw_categories = self.semantic_categories
        if isinstance(raw_categories, (str, bytes)):
            raise TypeError("semantic_categories must be a nonempty iterable of category names")
        if raw_categories is None:
            raise ValueError("semantic_categories must be non-empty")
        try:
            supplied_categories = tuple(
                _required_text(value, field_name="semantic_categories") for value in raw_categories
            )
        except TypeError as exc:
            raise TypeError("semantic_categories must be a nonempty iterable of category names") from exc
        if not supplied_categories:
            raise ValueError("semantic_categories must be non-empty")
        if len(supplied_categories) != len(set(supplied_categories)):
            raise ValueError("semantic_categories must not contain duplicates")
        invalid_categories = tuple(
            category for category in supplied_categories if category not in _REVIEW_CATEGORIES
        )
        if invalid_categories:
            raise ValueError("semantic_categories contains an invalid category")
        canonical_categories = tuple(
            category for category in _REVIEW_CATEGORY_ORDER if category in supplied_categories
        )
        object.__setattr__(self, "semantic_categories", canonical_categories)
        object.__setattr__(self, "problem", _required_text(self.problem, field_name="problem"))
        object.__setattr__(self, "evidence", _required_text(self.evidence, field_name="evidence"))
        object.__setattr__(self, "required_change", _required_text(self.required_change, field_name="required_change"))
        if not isinstance(self.material, bool):
            raise TypeError("material must be boolean")


class BusinessReviewAdapter:
    """Translate business-shaped findings into the durable review boundary.

    Semantic categories and the derived paths below are retained as reviewer
    provenance.  They are not filesystem capabilities: once a same-owner
    repair is authorized, :class:`ItemWorkspace` permits any answer section
    and any artifact under that item's ``work/`` directory.
    """

    _CATEGORY_DEPENDENCIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
        {
            # These paths describe the reviewer's evidence/dependency
            # reasoning only.  They are not an allowlist for active repair
            # writes (the durable item boundary enforces that separately).
            "answer": ("work/business_review_packet.json",),
            "presentation": (),
            "calculation": (
                "work/calculations",
                "work/analysis.py",
                "work/analysis.json",
                "work/prepared",
                "work/evidence.jsonl",
                "work/.analysis-run",
                "work/script_receipts",
            ),
            "evidence": ("work/evidence.jsonl", "work/source_map.json", "work/specialist_memos.jsonl"),
            "source_completeness": ("work/source_map.json", "work/evidence.jsonl", "work/specialist_memos.jsonl"),
            "method": (
                "work/plan.json",
                "work/calculations",
                "work/analysis.py",
                "work/analysis.json",
                "work/prepared",
                "work/evidence.jsonl",
                "work/source_map.json",
                "work/specialist_memos.jsonl",
                "work/.analysis-run",
                "work/script_receipts",
            ),
        }
    )
    _CATEGORY_ARTIFACT_PATHS: Mapping[str, tuple[str, ...]] = MappingProxyType(
        {
            # Retained as review provenance for targeted re-checks.  Active
            # repair writes are intentionally not constrained to these roots.
            "answer": ("work/results",),
            "presentation": ("work/results",),
            "calculation": ("work/results",),
            "evidence": (),
            "source_completeness": (),
            "method": (),
        }
    )

    def __init__(self, item_workspace: ItemWorkspace, *, owner_ref: str) -> None:
        if not isinstance(item_workspace, ItemWorkspace):
            raise TypeError("item_workspace must be an ItemWorkspace")
        owner_ref = _required_text(owner_ref, field_name="owner_ref")
        self.item_workspace = item_workspace
        self.owner_ref = owner_ref

    @classmethod
    def _core_finding(cls, finding: ReviewFinding) -> dict[str, Any]:
        message = (
            f"Problem: {finding.problem}\n"
            f"Evidence: {finding.evidence}\n"
            f"Required change: {finding.required_change}"
        )
        # A material repair to any named answer section may require the
        # Analytical Owner to revise the coherent narrative in ``answer`` as
        # well.  Keep every named semantic section, and add the narrative
        # root exactly once as program-owned scope.  The typed finding never
        # accepts raw JSON pointers or wildcard paths.
        pointers = {f"/{section}" for section in finding.target_sections} | {"/answer"}
        # These answer-facing categories can require the canonical scope and
        # next-actions fields to remain coherent with the corrected answer.
        # The fields are program-derived from semantic categories; callers
        # cannot widen scope by supplying arbitrary pointers.
        if {"answer", "presentation", "calculation"}.intersection(finding.semantic_categories):
            pointers.update({"/scope", "/next_actions"})
        # Evidence/source-completeness repairs may add a newly recorded
        # evidence reference to the coherent answer.  This is a narrow,
        # program-derived pointer; the analytical owner never supplies raw
        # pointer scope in the semantic finding.
        if _EVIDENCE_BINDING_CATEGORIES.intersection(finding.semantic_categories):
            pointers.add("/evidence_refs")
        if (
            "visuals" in finding.target_sections
            and _VISUAL_EVIDENCE_BINDING_CATEGORIES.issubset(finding.semantic_categories)
        ):
            pointers.add("/evidence_refs")
        dependent_outputs = sorted(
            {
                dependency
                for category in finding.semantic_categories
                for dependency in cls._CATEGORY_DEPENDENCIES[category]
            }
        )
        artifact_paths = sorted(
            {
                artifact_path
                for category in finding.semantic_categories
                for artifact_path in cls._CATEGORY_ARTIFACT_PATHS[category]
            }
        )
        return {
            "finding_id": finding.finding_id,
            "message": message,
            "pointers": sorted(pointers),
            "artifact_paths": artifact_paths,
            "dependent_outputs": dependent_outputs,
            # The semantic adapter is the program-owned boundary for this
            # provenance.  The owner supplies typed categories, while the
            # durable packet stores the canonical tuple used by recovery and
            # targeted re-checks; raw JSON scope remains unavailable to the
            # owner.
            "semantic_categories": list(finding.semantic_categories),
            "material": finding.material,
        }

    def record(
        self,
        verdict: str,
        *,
        findings: Iterable[ReviewFinding] = (),
        reviewer_ref: str | None = None,
        review_status: str = "reviewed",
    ) -> dict[str, Any]:
        values = tuple(findings)
        if any(not isinstance(value, ReviewFinding) for value in values):
            raise TypeError("findings must contain ReviewFinding values")
        return self.item_workspace.record_review(
            verdict,
            reviewer_ref=reviewer_ref,
            review_status=review_status,
            findings=[self._core_finding(value) for value in values],
        )

    def begin_repair(self, *, owner_ref: str | None = None) -> dict[str, Any]:
        effective_owner = self.owner_ref if owner_ref is None else _required_text(owner_ref, field_name="owner_ref")
        if effective_owner != self.owner_ref:
            raise ValueError("owner_ref does not match this Analytical Owner facade")
        return self.item_workspace.use_business_repair(owner_ref=effective_owner)

    def confirm_data_insufficiency(self, *, reviewer_ref: str | None = None) -> dict[str, Any]:
        """Record the reviewer verdict that confirms the owner's conclusion."""

        return self.record("confirm_data_insufficiency", reviewer_ref=reviewer_ref)

    def finalize_blocked_by_evidence(self) -> AcceptedSnapshot:
        """Finalize reviewer-confirmed owner data insufficiency."""

        return self.item_workspace.finalize_blocked_by_evidence()

    def reconcile_active_repair_scope(self, findings: Iterable[ReviewFinding]) -> dict[str, Any]:
        """Refresh reviewer provenance on an already-open repair packet.

        The durable core preserves the original baseline and reviewer
        categories for targeted re-checks.  It does not turn those categories
        into capabilities for the item-local repair writer.
        """

        values = tuple(findings)
        if any(not isinstance(value, ReviewFinding) for value in values):
            raise TypeError("findings must contain ReviewFinding values")
        return self.item_workspace._reconcile_active_business_repair_scope(  # noqa: SLF001
            [self._core_finding(value) for value in values],
            semantic_categories=tuple(value.semantic_categories for value in values),
            owner_ref=self.owner_ref,
        )


class AnalystWorkspace:
    """Business-shaped access to one bound deterministic analysis context."""

    _QUALITY_CRITERIA = (
        "answer the original decision question",
        "bind material claims to evidence",
        "state period, population, denominator, and units",
        "measure material joins and disclose coverage",
        "separate supported, unsupported, proxy, and causal claims",
        "preserve reproducible calculations and visible limitations",
    )

    def __init__(self, context: BoundAnalysisContext, *, owner_ref: str) -> None:
        if not isinstance(context, BoundAnalysisContext):
            raise TypeError("context must be a BoundAnalysisContext")
        owner_ref = _required_text(owner_ref, field_name="owner_ref")
        context.ensure_valid()
        self.context = context
        self.item_workspace = context.item_workspace
        self.owner_ref = self.item_workspace.bind_analysis_owner(owner_ref)
        self.review = BusinessReviewAdapter(self.item_workspace, owner_ref=self.owner_ref)

    def _assert_owner(self) -> None:
        """Recheck the immutable owner binding before semantic writes."""

        with self.item_workspace._state_transition_lock():  # noqa: SLF001
            self.item_workspace._reload_authoritative_state_locked()  # noqa: SLF001
            self.item_workspace._verify_analysis_owner_locked(self.owner_ref)  # noqa: SLF001
            self.item_workspace._ensure_execution_state()  # noqa: SLF001
            self.item_workspace._reconcile_business_review_discard()  # noqa: SLF001

    def refresh_semantic_scope(self, lifecycle: Any) -> BoundAnalysisContext:
        """Refresh this owner's ordinary Requirement semantic snapshot.

        The core operation reloads the authoritative item and lifecycle under
        their existing locks, then atomically replaces only the manifest-bound
        semantic bundle.  This facade keeps the same owner binding and updates
        itself to the validated context returned by the core; prior script
        receipts remain durable, while the owner must rerun against the new
        context before final submission.
        """

        self._assert_owner()
        refreshed = BoundAnalysisContext.refresh_requirement_semantics(
            self.context.context,
            self.item_workspace,
            lifecycle,
            telemetry=self.context.telemetry,
        )
        self.context = refreshed
        self.item_workspace = refreshed.item_workspace
        self.review = BusinessReviewAdapter(self.item_workspace, owner_ref=self.owner_ref)
        return refreshed

    def brief(self) -> AnalystBrief:
        state = self.item_workspace.state
        plan = self._read_requirement_plan() if self.item_workspace.mode == "requirement" else None
        # ``brief`` is intentionally manifest-only.  It exposes exact layer
        # counts without opening ontology, relationship, identity, or
        # prepared payloads; owners load a requested layer through search.
        counts = self._semantic_counts()
        ontology_count = counts["ontology"]
        relationship_count = counts["relationships"]
        prepared_count = counts["prepared_assets"]
        return AnalystBrief(
            item_id=self.item_workspace.item_id,
            mode=self.item_workspace.mode,
            question=str(state["original_text"]),
            catalog_entry_count=len(self.context.source_catalog.entries),
            quality_criteria=self._QUALITY_CRITERIA,
            available_specialties=tuple(sorted(_SPECIALTIES)),
            requirement_plan=plan,
            ontology_item_count=ontology_count,
            ontology_relationship_count=relationship_count,
            prepared_asset_count=prepared_count,
            has_ontology=bool(ontology_count),
            has_relationships=bool(relationship_count),
            has_prepared_assets=bool(prepared_count),
        )

    def _read_requirement_plan(self) -> RequirementAnalysisPlan | None:
        revisions = self._load_requirement_plan_revisions()
        current, _current_hash = self._read_current_requirement_plan()
        if not revisions:
            if current is None:
                return None
            raise ValueError("requirement plan revision chain is missing")
        if current is None:
            raise ValueError("requirement plan current file is missing")
        latest = revisions[-1]
        if current.to_dict() != latest["plan_payload"]:
            raise ValueError("requirement plan current file does not match latest revision")
        return current

    def _requirement_plan_revisions_root(self) -> Path:
        return self.item_workspace.work_root / _REQUIREMENT_PLAN_REVISIONS_DIRNAME

    def _read_current_requirement_plan(self) -> tuple[RequirementAnalysisPlan | None, str | None]:
        """Read and validate the compatibility current-plan representation."""

        path = self.item_workspace.work_root / _REQUIREMENT_PLAN_FILENAME
        if path.is_symlink():
            raise ValueError("requirement plan cannot be a symlink")
        if not path.exists():
            return None, None
        if not path.is_file():
            raise ValueError("requirement plan must be a regular file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("requirement plan is invalid") from exc
        if not isinstance(value, Mapping):
            raise ValueError("requirement plan is invalid")
        try:
            plan = RequirementAnalysisPlan.from_dict(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("requirement plan is invalid") from exc
        if plan.to_dict() != dict(value):
            raise ValueError("requirement plan is not canonical")
        if plan.original_text != self.item_workspace.original_text:
            raise ValueError("requirement plan original_text does not match requirement")
        return plan, _canonical_hash(value)

    def _published_requirement_plan_hash(self) -> str | None:
        """Return the current-file head used for optimistic publication CAS."""

        _plan, plan_hash = self._read_current_requirement_plan()
        return plan_hash

    def _load_requirement_plan_revisions(self) -> tuple[Mapping[str, Any], ...]:
        """Validate every immutable plan revision and its hash chain."""

        root = self._requirement_plan_revisions_root()
        if root.is_symlink():
            raise ValueError("requirement plan revisions cannot be a symlink")
        if not root.exists():
            return ()
        if not root.is_dir():
            raise ValueError("requirement plan revisions must be a directory")
        try:
            paths = tuple(root.iterdir())
        except OSError as exc:
            raise ValueError("requirement plan revisions are unreadable") from exc

        by_revision: dict[int, Path] = {}
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError("requirement plan revisions contain a non-regular path")
            match = _REQUIREMENT_PLAN_REVISION_NAME.fullmatch(path.name)
            if match is None:
                raise ValueError("requirement plan revisions contain an unexpected path")
            revision = int(match.group(1))
            if revision in by_revision:
                raise ValueError("requirement plan revisions contain a duplicate ordinal")
            by_revision[revision] = path
        if not by_revision:
            raise ValueError("requirement plan revisions directory is empty")
        ordinals = sorted(by_revision)
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("requirement plan revision ordinals are not contiguous")

        records: list[Mapping[str, Any]] = []
        previous_plan_hash: str | None = None
        for revision in ordinals:
            path = by_revision[revision]
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("requirement plan revision is invalid") from exc
            if not isinstance(value, Mapping) or set(value) != _REQUIREMENT_PLAN_REVISION_FIELDS:
                raise ValueError("requirement plan revision fields are invalid")
            record = dict(value)
            if record["kind"] != _REQUIREMENT_PLAN_REVISION_KIND:
                raise ValueError("requirement plan revision kind is invalid")
            if record["schema"] != _REQUIREMENT_PLAN_REVISION_SCHEMA:
                raise ValueError("requirement plan revision schema is invalid")
            if record["item_id"] != self.item_workspace.item_id:
                raise ValueError("requirement plan revision item_id is invalid")
            if isinstance(record["revision"], bool) or not isinstance(record["revision"], int) or record["revision"] != revision:
                raise ValueError("requirement plan revision ordinal is invalid")
            payload = record["plan_payload"]
            if not isinstance(payload, Mapping):
                raise ValueError("requirement plan revision plan_payload is invalid")
            try:
                plan = RequirementAnalysisPlan.from_dict(payload)
            except (TypeError, ValueError) as exc:
                raise ValueError("requirement plan revision plan_payload is invalid") from exc
            if plan.to_dict() != dict(payload):
                raise ValueError("requirement plan revision plan_payload is not canonical")
            if plan.original_text != self.item_workspace.original_text:
                raise ValueError("requirement plan revision original_text does not match requirement")
            if not _is_sha256(record["plan_hash"]) or record["plan_hash"] != _canonical_hash(payload):
                raise ValueError("requirement plan revision plan_hash is invalid")
            parent_plan_hash = record["parent_plan_hash"]
            if revision == 1:
                if parent_plan_hash is not None:
                    raise ValueError("requirement plan revision one must not have a parent")
            elif not _is_sha256(parent_plan_hash) or parent_plan_hash != previous_plan_hash:
                raise ValueError("requirement plan revision parent is invalid")
            context_manifest_hash = record["analysis_context_manifest_hash"]
            if not _is_sha256(context_manifest_hash):
                raise ValueError("requirement plan revision analysis context hash is invalid")
            generation_id = record["active_generation_id"]
            generation_manifest_hash = record["active_generation_manifest_hash"]
            if generation_id is None:
                if generation_manifest_hash is not None:
                    raise ValueError("requirement plan revision active generation hash is invalid")
            elif (
                not isinstance(generation_id, str)
                or not generation_id.strip()
                or not _is_sha256(generation_manifest_hash)
            ):
                raise ValueError("requirement plan revision active generation metadata is invalid")
            if not isinstance(record["created_at"], str) or not record["created_at"].strip():
                raise ValueError("requirement plan revision created_at is invalid")
            unsigned = {key: record[key] for key in _REQUIREMENT_PLAN_REVISION_FIELDS if key != "record_hash"}
            if not _is_sha256(record["record_hash"]) or record["record_hash"] != _canonical_hash(unsigned):
                raise ValueError("requirement plan revision record_hash is invalid")
            records.append(record)
            previous_plan_hash = record["plan_hash"]
        return tuple(records)

    def _active_generation_binding(self) -> tuple[str | None, str | None]:
        """Return validated lifecycle generation metadata, without fabrication."""

        with RunLifecycle._run_lock(self.context.context):  # noqa: SLF001 - shared run authority boundary
            return self._active_generation_binding_unlocked()

    def _active_generation_binding_unlocked(self) -> tuple[str | None, str | None]:
        """Read generation metadata while the caller owns the run lock."""

        pointer = RunLifecycle._read_generation_pointer_unlocked(self.context.context)  # noqa: SLF001
        if pointer is None:
            return None, None
        metadata = RunLifecycle._load_generation_unlocked(self.context.context, pointer)  # noqa: SLF001
        if metadata is None:
            return None, None
        return metadata.generation_id, metadata.manifest_hash

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                pass
        finally:
            os.close(descriptor)

    @classmethod
    def _revision_bytes(cls, record: Mapping[str, Any]) -> bytes:
        return (
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")

    @classmethod
    def _publish_requirement_revision(cls, path: Path, record: Mapping[str, Any]) -> None:
        """Create one immutable revision file without replacing existing bytes."""

        if path.parent.is_symlink() or (path.parent.exists() and not path.parent.is_dir()):
            raise ValueError("requirement plan revisions directory is invalid")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError("requirement plan revisions directory is invalid") from exc
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise ValueError("requirement plan revisions directory is invalid")
        payload = cls._revision_bytes(record)
        created = False
        try:
            with path.open("xb") as stream:
                created = True
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            cls._fsync_directory(path.parent)
        except Exception:
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    def _require_requirement_plan(self, *, operation: str) -> RequirementAnalysisPlan | None:
        """Require the current validated requirement plan before material work."""

        if self.item_workspace.mode != "requirement":
            return None
        plan = self._read_requirement_plan()
        if plan is None:
            raise ValueError(f"requirement {operation} requires a persisted semantic plan first")
        return plan

    def plan_requirement(
        self,
        plan: RequirementAnalysisPlan | None = None,
        *,
        tasks: Iterable[RequirementAnalysisTask] = (),
        synthesis_intent: str | None = None,
    ) -> RequirementAnalysisPlan:
        """Persist one semantic decomposition for a requirement item."""

        expected_current_hash = self._published_requirement_plan_hash()
        if self.item_workspace.mode != "requirement":
            raise ValueError("requirement planning is available only in requirement mode")
        if plan is not None and (tuple(tasks) or synthesis_intent is not None):
            raise ValueError("plan cannot be combined with task arguments")
        if plan is None:
            values = tuple(tasks)
            if any(not isinstance(value, RequirementAnalysisTask) for value in values):
                raise TypeError("tasks must contain RequirementAnalysisTask values")
            if synthesis_intent is None:
                raise ValueError("synthesis_intent must be provided")
            plan = RequirementAnalysisPlan(tasks=values, synthesis_intent=synthesis_intent)
        elif not isinstance(plan, RequirementAnalysisPlan):
            raise TypeError("plan must be a RequirementAnalysisPlan")
        if plan.original_text and plan.original_text != self.item_workspace.original_text:
            raise ValueError("requirement plan must preserve the exact original requirement text")
        bound_plan = RequirementAnalysisPlan(
            tasks=plan.tasks,
            synthesis_intent=plan.synthesis_intent,
            original_text=self.item_workspace.original_text,
        )
        # Refresh/rebind takes the run lock before any item transition lock.
        # Keep the same order here and capture the generation binding while
        # the run authority is stable; model/material work above remains
        # outside both locks.
        with RunLifecycle._run_lock(self.context.context):  # noqa: SLF001 - shared run authority boundary
            self._assert_owner()
            active_generation_binding = self._active_generation_binding_unlocked()
            with self.item_workspace._state_transition_lock():  # noqa: SLF001 - one item authority boundary
                return self._persist_requirement_plan_locked(
                    bound_plan,
                    _expected_current_plan_hash=expected_current_hash,
                    _active_generation_binding=active_generation_binding,
                )

    def _persist_requirement_plan_locked(
        self,
        bound_plan: RequirementAnalysisPlan,
        *,
        _state_loaded: bool = False,
        _expected_current_plan_hash: str | None | object = _MISSING_EXPECTED_PLAN_HEAD,
        _active_generation_binding: tuple[str | None, str | None] | object = _MISSING_EXPECTED_PLAN_HEAD,
    ) -> RequirementAnalysisPlan:
        """Publish a requirement plan while the item transition lock is held."""

        if not _state_loaded:
            self.item_workspace._reload_authoritative_for_artifact_mutation_locked()  # noqa: SLF001
        self.item_workspace._ensure_not_terminal()  # noqa: SLF001
        revisions = self._load_requirement_plan_revisions()
        current, current_hash = self._read_current_requirement_plan()
        if (
            _expected_current_plan_hash is not _MISSING_EXPECTED_PLAN_HEAD
            and current_hash != _expected_current_plan_hash
        ):
            raise ValueError("concurrent requirement plan publication conflict")

        staged = False
        if not revisions:
            if current is not None:
                raise ValueError("requirement plan revision chain is missing")
        elif current is None:
            if len(revisions) != 1:
                raise ValueError("requirement plan has multiple unpublished revisions")
            staged = True
        else:
            current_index = next(
                (
                    index
                    for index, record in enumerate(revisions)
                    if record["plan_hash"] == current_hash and record["plan_payload"] == current.to_dict()
                ),
                None,
            )
            if current_index is None:
                raise ValueError("requirement plan current file does not match revision chain")
            if current_index != len(revisions) - 1:
                if current_index != len(revisions) - 2:
                    raise ValueError("requirement plan has multiple unpublished revisions")
                staged = True

        if staged:
            latest = revisions[-1]
            if latest["plan_payload"] != bound_plan.to_dict():
                raise ValueError("staged requirement plan revision conflicts with requested plan")
            self.item_workspace._write_json_artifact(  # noqa: SLF001 - item owns path and atomicity
                Path("work") / _REQUIREMENT_PLAN_FILENAME,
                latest["plan_payload"],
            )
            return RequirementAnalysisPlan.from_dict(latest["plan_payload"])

        if current is not None and current == bound_plan:
            if not revisions:
                raise ValueError("requirement plan revision chain is missing")
            return current

        revision = len(revisions) + 1
        plan_payload = bound_plan.to_dict()
        plan_hash = _canonical_hash(plan_payload)
        parent_plan_hash = revisions[-1]["plan_hash"] if revisions else None
        if _active_generation_binding is _MISSING_EXPECTED_PLAN_HEAD:
            generation_id, generation_manifest_hash = self._active_generation_binding()
        else:
            generation_id, generation_manifest_hash = _active_generation_binding  # type: ignore[misc]
        unsigned = {
            "kind": _REQUIREMENT_PLAN_REVISION_KIND,
            "schema": _REQUIREMENT_PLAN_REVISION_SCHEMA,
            "item_id": self.item_workspace.item_id,
            "revision": revision,
            "plan_payload": plan_payload,
            "plan_hash": plan_hash,
            "parent_plan_hash": parent_plan_hash,
            "analysis_context_manifest_hash": self.context.manifest_hash,
            "active_generation_id": generation_id,
            "active_generation_manifest_hash": generation_manifest_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not _is_sha256(unsigned["analysis_context_manifest_hash"]):
            raise ValueError("analysis context manifest hash is invalid")
        record = {**unsigned, "record_hash": _canonical_hash(unsigned)}
        revision_path = self._requirement_plan_revisions_root() / f"rev-{revision:04d}.json"
        self._publish_requirement_revision(revision_path, record)
        # The immutable record is durable before the compatibility current file
        # advances.  If this write fails, the next exact retry repairs the
        # staged successor without replacing its bytes.
        self.item_workspace._write_json_artifact(  # noqa: SLF001 - item owns path and atomicity
            Path("work") / _REQUIREMENT_PLAN_FILENAME,
            plan_payload,
        )
        return bound_plan

    def _entry(self, source_id: str) -> DataRoomCatalogEntry:
        source_id = _required_text(source_id, field_name="source_id")
        matches = [entry for entry in self.context.source_catalog.entries if _source_id(entry) == source_id]
        if len(matches) != 1:
            raise KeyError(f"unknown or ambiguous source_id: {source_id}")
        return matches[0]

    def _work_records(self, filename: str) -> tuple[Mapping[str, Any], ...]:
        path = self.item_workspace.work_root / filename
        if path.is_symlink():
            raise ValueError(f"{filename} cannot be a symlink")
        if not path.exists():
            return ()
        records: list[Mapping[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise ValueError
                records.append(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{filename} is invalid") from exc
        return tuple(records)

    def _semantic_ref(self) -> SemanticSnapshotRef | None:
        return self.context.semantic_snapshot_ref

    def _semantic_manifest(self) -> Mapping[str, Any] | None:
        ref = self._semantic_ref()
        if ref is None:
            return None
        return SemanticSnapshotStore.manifest(self.context.context, ref)

    def _semantic_counts(self) -> Mapping[str, int]:
        ref = self._semantic_ref()
        if ref is None:
            return MappingProxyType(
                {
                    "ontology": 0,
                    "relationships": 0,
                    "identity_decisions": 0,
                    "canonical_mappings": 0,
                    "prepared_assets": 0,
                }
            )
        return ref.counts

    def _layer_records(self, *layers: str) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
        ref = self._semantic_ref()
        if ref is None:
            return {layer: () for layer in layers}
        return SemanticSnapshotStore.records(self.context.context, ref, layers)

    def _ontology_entries(self) -> tuple[OntologyItem, ...]:
        """Load only ontology and relationship layers for this search."""

        records = self._layer_records("ontology", "relationships")
        # Semantic snapshots are produced from the LEM current_* views, but
        # keep this read boundary defensive: a historical/superseded object
        # must never become selectable merely because an older snapshot or a
        # hand-authored payload still contains it.
        entries = [
            OntologyItem.from_dict(item)
            for item in records["ontology"]
            if str(item.get("status", "active")) != "superseded"
        ]
        known = {item.item_id for item in entries}
        for record in records["relationships"]:
            if str(record.get("status", "active")) == "superseded":
                continue
            relationship_id = str(record["relationship_id"])
            if relationship_id in known:
                continue
            entries.append(
                OntologyItem(
                    item_id=relationship_id,
                    item_type="relationship",
                    label=str(record.get("label") or relationship_id),
                    properties=dict(record),
                    source_refs=tuple(record.get("source_refs", ())),
                    scope=record.get("scope"),
                    effective_period=record.get("effective_period"),
                    status=str(record.get("status", "active")),
                )
            )
        return tuple(sorted(entries, key=lambda item: item.item_id))

    def _semantic_snapshot_hash(self) -> str:
        ref = self._semantic_ref()
        return ref.snapshot_hash if ref is not None else ""

    def _prepared_snapshot_ids(self) -> frozenset[str]:
        ref = self._semantic_ref()
        if ref is None:
            return frozenset()
        return frozenset(SemanticSnapshotStore.layer_ids(self.context.context, ref, "prepared_assets"))

    def _identity_mappings(self) -> tuple[CanonicalMapping, ...]:
        records = self._layer_records("canonical_mappings")["canonical_mappings"]
        return tuple(CanonicalMapping.from_dict(item) for item in records)

    def _identity_decisions(self) -> tuple[IdentityDecision, ...]:
        records = self._layer_records("identity_decisions")["identity_decisions"]
        return tuple(IdentityDecision.from_dict(item) for item in records)

    def _semantic_layers_present(self) -> bool:
        return any(self._semantic_counts().values())

    def _bound_prepared_assets(self) -> tuple[PreparedAssetDescriptor, ...]:
        """Load and validate only prepared descriptors when requested."""

        snapshot_assets = tuple(
            PreparedAssetDescriptor.from_dict(item)
            for item in self._layer_records("prepared_assets")["prepared_assets"]
        )
        registry = self.context.prepared_assets
        bound: list[PreparedAssetDescriptor] = []
        for snapshot in snapshot_assets:
            matches = registry.search(
                prepared_asset_id=snapshot.prepared_asset_id,
                include_superseded=True,
            )
            if len(matches) != 1:
                raise ValueError(
                    f"prepared asset registry does not contain exactly one snapshot asset: {snapshot.prepared_asset_id}"
                )
            current = matches[0]
            if current.to_dict() != snapshot.to_dict():
                raise ValueError(
                    f"prepared asset descriptor changed after semantic snapshot: {snapshot.prepared_asset_id}"
                )
            bound.append(current)
        return tuple(sorted(bound, key=lambda asset: asset.prepared_asset_id))

    def _prepared_registry_hash(self) -> str:
        descriptors = self.context.prepared_assets.search(include_superseded=True)
        return _canonical_hash([descriptor.to_dict() for descriptor in descriptors])

    def _append_semantic_selection(
        self,
        kind: str,
        ids: tuple[str, ...],
        purpose: str,
        *,
        selection_sets: Mapping[str, Iterable[str]] | None = None,
        decision: str | None = None,
        no_reuse_reason: str | None = None,
    ) -> Mapping[str, Any]:
        ref = self._semantic_ref()
        if selection_sets is None:
            selection_sets = {
                "ontology_ids": ids if kind == "ontology" else (),
                "relationship_ids": (),
                "mapping_ids": ids if kind == "identity_mapping" else (),
                "identity_decision_ids": (),
                "prepared_asset_ids": ids if kind == "prepared_asset" else (),
            }
        normalized_sets = {str(key): tuple(str(value) for value in values) for key, values in selection_sets.items()}
        counts = {
            key: len(values)
            for key, values in normalized_sets.items()
            if key in {"ontology_ids", "relationship_ids", "mapping_ids", "identity_decision_ids", "prepared_asset_ids"}
        }
        for key in ("ontology_ids", "relationship_ids", "mapping_ids", "identity_decision_ids", "prepared_asset_ids"):
            counts.setdefault(key, 0)
        selection_ref: str | None = None
        selection_hash: str | None = None
        if any(counts.values()):
            if ref is None:
                raise ValueError("semantic selection requires a bound semantic snapshot")
            selected = SemanticSnapshotStore.publish_selection(
                self.context.context,
                ref,
                normalized_sets,
            )
            selection_ref = selected.selection_ref
            selection_hash = selected.selection_hash
        payload = {
            "record_kind": "semantic_selection",
            "item_id": self.item_workspace.item_id,
            "owner_ref": self.owner_ref,
            "selection_kind": kind,
            "selection_ref": selection_ref,
            "selection_hash": selection_hash,
            "selection_counts": counts,
            "purpose": purpose,
            "snapshot_hash": self._semantic_snapshot_hash(),
            "context_manifest_hash": self.context.manifest_hash,
            "registry_hash": self._prepared_registry_hash() if counts.get("prepared_asset_ids", 0) else None,
        }
        if decision is not None:
            payload["decision"] = _required_text(decision, field_name="decision")
        if no_reuse_reason is not None:
            payload["no_reuse_reason"] = _required_text(no_reuse_reason, field_name="no_reuse_reason")
        payload["selection_id"] = _canonical_hash(payload)
        self.item_workspace.append_semantic_selection(payload)
        return {
            "selection_ref": selection_ref,
            "selection_hash": selection_hash,
            "selection_counts": dict(counts),
        }

    def _ontology_selection_sets(self, ids: Iterable[str]) -> Mapping[str, tuple[str, ...]]:
        values = tuple(ids)
        ref = self._semantic_ref()
        if ref is None:
            raise ValueError("semantic selection requires a bound semantic snapshot")
        ontology_ids = set(SemanticSnapshotStore.layer_ids(self.context.context, ref, "ontology"))
        relationship_ids = set(SemanticSnapshotStore.layer_ids(self.context.context, ref, "relationships"))
        unknown = [item_id for item_id in values if item_id not in ontology_ids and item_id not in relationship_ids]
        if unknown:
            raise KeyError(f"unknown ontology or relationship item_id: {unknown[0]}")
        return {
            "ontology_ids": tuple(item_id for item_id in values if item_id in ontology_ids),
            "relationship_ids": tuple(item_id for item_id in values if item_id in relationship_ids),
            "mapping_ids": (),
            "identity_decision_ids": (),
            "prepared_asset_ids": (),
        }

    def _has_current_semantic_scope_decision(self) -> bool:
        """Return whether this owner made an explicit decision for this snapshot."""

        snapshot_hash = self._semantic_snapshot_hash()
        for record in self._work_records(_SEMANTIC_SELECTIONS_FILENAME):
            if (
                record.get("record_kind") == "semantic_selection"
                and record.get("owner_ref") == self.owner_ref
                and record.get("snapshot_hash") == snapshot_hash
                and record.get("selection_kind") in {"semantic_scope", "ontology", "identity_mapping", "prepared_asset"}
            ):
                # Exact layer selections are substantive reuse decisions.  A
                # dedicated semantic_scope record can additionally document a
                # no-reuse reason or an exact multi-layer selection.
                return True
        return False

    @staticmethod
    def _bounded_limit(limit: int, *, field_name: str = "limit") -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return limit

    def search_ontology(
        self,
        query: str = "",
        *,
        item_type: str | None = None,
        limit: int = 20,
    ) -> tuple[OntologyItem, ...]:
        """Search accepted prior ontology and relationship descriptors."""

        self.context.ensure_valid()
        limit = self._bounded_limit(limit)
        wanted_type = None if item_type is None else _required_text(item_type, field_name="item_type")
        entries = [
            item
            for item in self._ontology_entries()
            if item.status != "superseded" and (wanted_type is None or item.item_type == wanted_type)
        ]
        tokens = tuple(token for token in str(query).strip().lower().split() if token)
        if not tokens:
            return tuple(entries[:limit])
        ranked: list[tuple[int, OntologyItem]] = []
        for item in entries:
            haystack = json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True, default=str).lower()
            score = sum(1 for token in tokens if token in haystack)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].item_id))
        return tuple(item for _score, item in ranked[:limit])

    def select_ontology(self, item_ids: Iterable[str], *, purpose: str) -> tuple[OntologyItem, ...]:
        """Select exact ontology/relationship IDs and append an owner trace."""

        self._assert_owner()
        purpose = _required_text(purpose, field_name="purpose")
        ids = _text_tuple(item_ids, field_name="item_ids")
        if not ids:
            raise ValueError("at least one item_id is required")
        by_id = {item.item_id: item for item in self._ontology_entries()}
        unknown = [item_id for item_id in ids if item_id not in by_id]
        if unknown:
            raise KeyError(f"unknown ontology item_id: {unknown[0]}")
        selected = tuple(by_id[item_id] for item_id in ids)
        self._append_semantic_selection(
            "ontology",
            ids,
            purpose,
            selection_sets=self._ontology_selection_sets(ids),
        )
        return selected

    def search_identity_mappings(
        self,
        query: str = "",
        *,
        object_type: str | None = None,
        limit: int = 20,
    ) -> tuple[CanonicalMapping, ...]:
        """Search canonical identity mappings in the current snapshot."""

        self.context.ensure_valid()
        limit = self._bounded_limit(limit)
        wanted_type = None if object_type is None else _required_text(object_type, field_name="object_type")
        entries = [
            mapping
            for mapping in self._identity_mappings()
            if wanted_type is None or mapping.object_type == wanted_type
        ]
        tokens = tuple(token for token in str(query).strip().lower().split() if token)
        if tokens:
            ranked: list[tuple[int, CanonicalMapping]] = []
            for mapping in entries:
                haystack = json.dumps(mapping.to_dict(), ensure_ascii=False, sort_keys=True, default=str).lower()
                score = sum(1 for token in tokens if token in haystack)
                if score:
                    ranked.append((score, mapping))
            ranked.sort(key=lambda pair: (-pair[0], pair[1].canonical_id))
            entries = [mapping for _score, mapping in ranked]
        return tuple(entries[:limit])

    def search_identity_decisions(self, query: str = "", *, limit: int = 20) -> tuple[IdentityDecision, ...]:
        """Search reviewed identity decisions exposed by the snapshot."""

        self.context.ensure_valid()
        limit = self._bounded_limit(limit)
        entries = list(self._identity_decisions())
        tokens = tuple(token for token in str(query).strip().lower().split() if token)
        if tokens:
            ranked: list[tuple[int, IdentityDecision]] = []
            for decision in entries:
                haystack = json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True, default=str).lower()
                score = sum(1 for token in tokens if token in haystack)
                if score:
                    ranked.append((score, decision))
            ranked.sort(key=lambda pair: (-pair[0], pair[1].decision_id))
            entries = [decision for _score, decision in ranked]
        return tuple(entries[:limit])

    def select_identity_mappings(
        self,
        mapping_ids: Iterable[str],
        *,
        purpose: str,
    ) -> tuple[CanonicalMapping, ...]:
        """Select exact canonical mapping IDs and persist the owner trace."""

        self._assert_owner()
        purpose = _required_text(purpose, field_name="purpose")
        ids = _text_tuple(mapping_ids, field_name="mapping_ids")
        if not ids:
            raise ValueError("at least one mapping_id is required")
        by_id = {mapping.canonical_id: mapping for mapping in self._identity_mappings()}
        unknown = [mapping_id for mapping_id in ids if mapping_id not in by_id]
        if unknown:
            raise KeyError(f"unknown identity mapping: {unknown[0]}")
        selected = tuple(by_id[mapping_id] for mapping_id in ids)
        self._append_semantic_selection(
            "identity_mapping",
            ids,
            purpose,
            selection_sets={
                "ontology_ids": (),
                "relationship_ids": (),
                "mapping_ids": ids,
                "identity_decision_ids": (),
                "prepared_asset_ids": (),
            },
        )
        return selected

    def materialize_identity_mapping_view(
        self,
        *,
        mapping_ids: Iterable[str] = (),
        object_types: Iterable[str] = (),
        purpose: str,
    ) -> IdentityMappingView:
        """Write one typed reviewed mapping lookup for analyst scripts.

        The output is advisory input, not a new semantic authority.  A script
        resolves exact source identities through ``IdentityMappingView`` and
        receives ``None`` for an unmapped or ambiguous source instead of
        repeating owner-specific normalization rules.
        """

        self._assert_owner()
        purpose = _required_text(purpose, field_name="purpose")
        ids = _text_tuple(mapping_ids, field_name="mapping_ids")
        types = _text_tuple(object_types, field_name="object_types")
        if not ids and not types:
            raise ValueError("mapping_ids or object_types must be provided")
        available = self._identity_mappings()
        by_id = {mapping.canonical_id: mapping for mapping in available}
        unknown = [mapping_id for mapping_id in ids if mapping_id not in by_id]
        if unknown:
            raise KeyError(f"unknown identity mapping: {unknown[0]}")
        selected_by_id = {mapping_id: by_id[mapping_id] for mapping_id in ids}
        for mapping in available:
            if mapping.object_type in types:
                selected_by_id.setdefault(mapping.canonical_id, mapping)
        if not selected_by_id:
            raise ValueError("identity mapping view selection is empty")
        selected = tuple(selected_by_id[key] for key in sorted(selected_by_id))
        decision_ids = tuple(dict.fromkeys(mapping.decision_id for mapping in selected))
        decisions_by_id = {decision.decision_id: decision for decision in self._identity_decisions()}
        missing = [decision_id for decision_id in decision_ids if decision_id not in decisions_by_id]
        if missing:
            raise ValueError(f"selected mapping has no snapshot decision: {missing[0]}")
        decisions = tuple(decisions_by_id[decision_id] for decision_id in decision_ids)
        view = IdentityMappingView.build(selected, decisions)
        self._append_semantic_selection(
            "identity_mapping",
            tuple(mapping.canonical_id for mapping in selected),
            purpose,
            selection_sets={
                "ontology_ids": (),
                "relationship_ids": (),
                "mapping_ids": tuple(mapping.canonical_id for mapping in selected),
                "identity_decision_ids": decision_ids,
                "prepared_asset_ids": (),
            },
        )
        self.item_workspace._write_json_artifact(  # noqa: SLF001 - item owns atomic publication
            Path("work") / "identity_mapping_view.json",
            view.to_dict(),
        )
        return view

    def select_semantic_scope(
        self,
        *,
        ontology_ids: Iterable[str] = (),
        relationship_ids: Iterable[str] = (),
        mapping_ids: Iterable[str] = (),
        identity_decision_ids: Iterable[str] = (),
        prepared_asset_ids: Iterable[str] = (),
        purpose: str = "semantic scope decision",
        no_reuse_reason: str | None = None,
    ) -> Mapping[str, Any]:
        """Record one exact current-snapshot semantic reuse decision.

        A caller either names exact reusable IDs or explicitly records why no
        prior semantic layer applies.  This decision is owner evidence, not a
        hash-only acknowledgement; the snapshot hash merely binds the choice
        to the immutable context visible to this item.
        """

        self._assert_owner()
        purpose = _required_text(purpose, field_name="purpose")
        selected = {
            "ontology_ids": _text_tuple(ontology_ids, field_name="ontology_ids"),
            "relationship_ids": _text_tuple(relationship_ids, field_name="relationship_ids"),
            "mapping_ids": _text_tuple(mapping_ids, field_name="mapping_ids"),
            "identity_decision_ids": _text_tuple(identity_decision_ids, field_name="identity_decision_ids"),
            "prepared_asset_ids": _text_tuple(prepared_asset_ids, field_name="prepared_asset_ids"),
        }
        selected_count = sum(len(values) for values in selected.values())
        normalized_no_reuse = None if no_reuse_reason is None else _required_text(no_reuse_reason, field_name="no_reuse_reason")
        if normalized_no_reuse is not None and selected_count:
            raise ValueError("no_reuse_reason cannot be combined with exact semantic selections")
        if not selected_count and normalized_no_reuse is None:
            raise ValueError("semantic scope requires exact selections or a non-empty no_reuse_reason")

        ref = self._semantic_ref()
        if selected_count and ref is None:
            raise ValueError("semantic scope selection requires a bound semantic snapshot")
        if ref is not None:
            for layer, key in (
                ("ontology", "ontology_ids"),
                ("relationships", "relationship_ids"),
                ("canonical_mappings", "mapping_ids"),
                ("identity_decisions", "identity_decision_ids"),
                ("prepared_assets", "prepared_asset_ids"),
            ):
                if selected[key]:
                    SemanticSnapshotStore.validate_ids(self.context.context, ref, layer, selected[key])

        decision_name = "no_reuse" if normalized_no_reuse is not None else "reuse_exact"
        ids = tuple(
            value
            for key in ("ontology_ids", "relationship_ids", "mapping_ids", "identity_decision_ids", "prepared_asset_ids")
            for value in selected[key]
        )
        selection_info = self._append_semantic_selection(
            "semantic_scope",
            ids,
            purpose,
            selection_sets=selected,
            decision=decision_name,
            no_reuse_reason=normalized_no_reuse,
        )
        return MappingProxyType(
            {
                "decision": decision_name,
                "no_reuse_reason": normalized_no_reuse,
                **selection_info,
            }
        )

    def load_semantic_selection(
        self,
        selection_ref: str,
        selection_hash: str,
    ) -> Mapping[str, tuple[str, ...]]:
        """Load one exact selection set through its content-addressed ref.

        The selection object contains IDs only.  Callers then request the
        corresponding typed layer search when they need semantic records.
        """

        self._assert_owner()
        ref = self._semantic_ref()
        if ref is None:
            raise ValueError("semantic selection is unavailable without a semantic snapshot")
        return MappingProxyType(
            dict(
                SemanticSnapshotStore.load_selection(
                    self.context.context,
                    ref,
                    selection_ref,
                    selection_hash,
                )
            )
        )

    def search_prepared_assets(
        self,
        query: str = "",
        *,
        reusable_only: bool = True,
        limit: int = 20,
    ) -> tuple[PreparedAssetDescriptor, ...]:
        """Search accepted, run-local prepared descriptors without loading bytes."""

        self.context.ensure_valid()
        limit = self._bounded_limit(limit)
        descriptors = self._bound_prepared_assets()
        if reusable_only:
            descriptors = tuple(
                descriptor
                for descriptor in descriptors
                if descriptor.scope == "reusable" and descriptor.status != "superseded"
            )
        else:
            descriptors = tuple(descriptor for descriptor in descriptors if descriptor.status != "superseded")
        tokens = tuple(token for token in str(query).strip().lower().split() if token)
        if tokens:
            ranked: list[tuple[int, PreparedAssetDescriptor]] = []
            for descriptor in descriptors:
                haystack = " ".join(
                    (
                        descriptor.prepared_asset_id,
                        descriptor.location,
                        descriptor.scope,
                        descriptor.status,
                        *(str(value) for value in descriptor.source_hashes),
                        *(str(value) for value in descriptor.source_refs),
                        *(str(value) for value in descriptor.ontology_refs),
                    )
                ).lower()
                score = sum(1 for token in tokens if token in haystack)
                if score:
                    ranked.append((score, descriptor))
            ranked.sort(key=lambda pair: (-pair[0], pair[1].prepared_asset_id))
            descriptors = tuple(descriptor for _score, descriptor in ranked)
        return descriptors[:limit]

    def select_prepared_assets(self, asset_ids: Iterable[str], *, purpose: str) -> tuple[PreparedAssetDescriptor, ...]:
        """Select exact reusable prepared IDs and append an owner trace."""

        self._assert_owner()
        purpose = _required_text(purpose, field_name="purpose")
        ids = _text_tuple(asset_ids, field_name="asset_ids")
        if not ids:
            raise ValueError("at least one asset_id is required")
        bound = {asset.prepared_asset_id: asset for asset in self._bound_prepared_assets()}
        selected: list[PreparedAssetDescriptor] = []
        for asset_id in ids:
            if asset_id not in bound:
                known = self.context.prepared_assets.search(prepared_asset_id=asset_id, include_superseded=True)
                if known:
                    raise ValueError(f"prepared asset is not available in this semantic snapshot: {asset_id}")
                raise KeyError(f"unknown prepared asset_id: {asset_id}")
            if bound[asset_id].scope != "reusable" or bound[asset_id].status == "superseded":
                raise ValueError(f"prepared asset is not reusable: {asset_id}")
            selected.append(bound[asset_id])
        result = tuple(selected)
        self._append_semantic_selection(
            "prepared_asset",
            ids,
            purpose,
            selection_sets={
                "ontology_ids": (),
                "relationship_ids": (),
                "mapping_ids": (),
                "identity_decision_ids": (),
                "prepared_asset_ids": ids,
            },
        )
        return result

    def load_prepared_asset(self, asset_id: str) -> Any:
        """Load one selected reusable asset through registry hash validation."""

        self._assert_owner()
        asset_id = _required_text(asset_id, field_name="asset_id")
        # Resolve exact current-run authority first; ``registry.load`` then
        # revalidates location, row/byte counts, and the prepared content hash.
        self.context.ensure_valid()
        # Loading is an exact selection retry with a program-owned purpose;
        # the item-local selection identity makes repeated loads idempotent.
        self.select_prepared_assets((asset_id,), purpose="load prepared asset")
        return self.context.prepared_assets.load(asset_id)

    def propose_identity_domain(
        self,
        domain_id: str,
        object_type: str,
        rationale: str,
        source_hints: Iterable[str],
        representation_item_ids: Iterable[str],
    ) -> IdentityDomainProposal:
        """Record a typed identity-domain proposal for runtime resolution."""

        self._assert_owner()
        proposal = IdentityDomainProposal(
            domain_id=domain_id,
            object_type=object_type,
            rationale=rationale,
            source_hints=tuple(source_hints),
            representation_item_ids=tuple(representation_item_ids),
        )
        payload = {
            **proposal.to_dict(),
            "item_id": self.item_workspace.item_id,
            "owner_ref": self.owner_ref,
        }
        self.item_workspace.append_identity_domain_proposal(payload)
        return proposal

    def supersede_identity_domain_proposal(
        self,
        domain_id: str,
        object_type: str,
        rationale: str,
        source_hints: Iterable[str],
        representation_item_ids: Iterable[str],
        *,
        expected_predecessor_hash: str,
    ) -> IdentityDomainProposal:
        """Append an owner-bound successor through the public CAS API.

        The caller supplies the semantic successor, while the durable item
        workspace assigns the next revision, predecessor audit metadata, and
        proposal digest.  Existing proposal rows remain immutable.
        """

        self._assert_owner()
        proposal = IdentityDomainProposal(
            domain_id=domain_id,
            object_type=object_type,
            rationale=rationale,
            source_hints=tuple(source_hints),
            representation_item_ids=tuple(representation_item_ids),
        )
        payload = {
            **proposal.to_dict(),
            "item_id": self.item_workspace.item_id,
            "owner_ref": self.owner_ref,
        }
        successor = self.item_workspace.supersede_identity_domain_proposal(
            payload,
            expected_predecessor_hash=expected_predecessor_hash,
            owner_ref=self.owner_ref,
        )
        return IdentityDomainProposal.from_dict(successor)

    def read_identity_domain_proposals(
        self,
        *,
        include_history: bool = False,
    ) -> tuple[IdentityDomainProposal, ...]:
        """Read effective heads or the immutable item-local proposal history."""

        self._assert_owner()
        if type(include_history) is not bool:
            raise TypeError("include_history must be a bool")
        records = self.item_workspace.read_identity_domain_proposals(
            include_superseded=include_history,
        )
        proposals: list[IdentityDomainProposal] = []
        for record in records:
            owner_ref = record.get("owner_ref")
            if (
                record.get("item_id") != self.item_workspace.item_id
                or not isinstance(owner_ref, str)
                or not owner_ref.strip()
            ):
                raise ValueError("identity domain proposal owner binding is invalid")
            proposals.append(IdentityDomainProposal.from_dict(record))
        return tuple(proposals)

    def read_identity_domain_proposal_history(self) -> tuple[IdentityDomainProposal, ...]:
        """Return every validated proposal revision for audit/recovery."""

        return self.read_identity_domain_proposals(include_history=True)

    def mark_waiting_on_resolution(
        self,
        runtime_workspace: Any,
        domain_ids: Iterable[str],
        reason: str,
    ) -> Any:
        """Delegate runtime wait/release handling through the exact public API.

        The Analytical Owner does not import or instantiate a concrete
        runtime/entity-resolution class.  The runtime method owns lifecycle
        release and scheduling state; this facade only validates its inputs
        and supplies the item/owner context.
        """

        self._assert_owner()
        if runtime_workspace is None:
            raise TypeError("runtime_workspace is required")
        method = getattr(runtime_workspace, "mark_waiting_on_resolution", None)
        if not callable(method):
            raise TypeError("runtime_workspace must expose public mark_waiting_on_resolution")
        domains = _text_tuple(domain_ids, field_name="domain_ids")
        if not domains:
            raise ValueError("domain_ids cannot be empty")
        reason = _required_text(reason, field_name="reason")

        return method(
            requirement_id=self.item_workspace.item_id,
            domain_ids=domains,
            reason=reason,
            owner_ref=self.owner_ref,
        )

    def record_analytical_relationship(
        self,
        relationship: AnalyticalRelationshipEvidence | Mapping[str, Any] | None = None,
        *,
        relationship_id: str | None = None,
        source_id: str | None = None,
        target_id: str | None = None,
        cardinality: str = "none",
        join_keys: Iterable[Mapping[str, Any]] = (),
        matched_pairs: int | None = None,
        source_population: int | None = None,
        target_population: int | None = None,
        matched_source_count: int | None = None,
        matched_target_count: int | None = None,
        source_coverage: float | int | None = None,
        target_coverage: float | int | None = None,
        date_authority: str | None = None,
        as_of: str | None = None,
        limitations: Iterable[str] = (),
        evidence_refs: Iterable[str] = (),
        publishable: bool = False,
        no_relationship_reason: str | None = None,
        audit_id: str | None = None,
    ) -> AnalyticalRelationshipEvidence:
        """Persist relationship evidence observed during actual analysis.

        This operation records evidence only.  It does not infer a join from
        similarly named fields and does not publish an Integration Agent
        relationship.  ``no_relationship_reason`` is an explicit negative
        audit for the same source/target pair.
        """

        self._assert_owner()
        if relationship is not None:
            provided_join_keys = tuple(join_keys)
            provided_limitations = tuple(limitations)
            provided_evidence_refs = tuple(evidence_refs)
            if any(
                value is not None
                for value in (
                    relationship_id,
                    source_id,
                    target_id,
                    date_authority,
                    as_of,
                    no_relationship_reason,
                    audit_id,
                    matched_pairs,
                    source_population,
                    target_population,
                    matched_source_count,
                    matched_target_count,
                    source_coverage,
                    target_coverage,
                )
            ) or cardinality != "none" or provided_join_keys or provided_limitations or provided_evidence_refs or publishable:
                raise ValueError("relationship cannot be combined with relationship fields")
            if isinstance(relationship, AnalyticalRelationshipEvidence):
                value = relationship
            elif isinstance(relationship, Mapping):
                value = AnalyticalRelationshipEvidence.from_dict(relationship)
            else:
                raise TypeError("relationship must be AnalyticalRelationshipEvidence or a mapping")
        else:
            if relationship_id is None:
                raise ValueError("relationship_id is required")
            if source_id is None or target_id is None:
                raise ValueError("source_id and target_id are required")
            value = AnalyticalRelationshipEvidence(
                relationship_id=relationship_id,
                source_id=source_id,
                target_id=target_id,
                cardinality=cardinality,
                join_keys=tuple(join_keys),
                matched_pairs=matched_pairs,
                source_population=source_population,
                target_population=target_population,
                matched_source_count=matched_source_count,
                matched_target_count=matched_target_count,
                source_coverage=source_coverage,
                target_coverage=target_coverage,
                date_authority=date_authority,
                as_of=as_of,
                limitations=tuple(limitations),
                evidence_refs=tuple(evidence_refs),
                publishable=publishable,
                no_relationship_reason=no_relationship_reason,
                audit_id=audit_id,
            )
        payload = {
            **value.to_dict(),
            "item_id": self.item_workspace.item_id,
            "owner_ref": self.owner_ref,
        }
        self.item_workspace.append_analytical_relationship(payload)
        return value

    def replace_analytical_relationships(
        self,
        relationships: Iterable[AnalyticalRelationshipEvidence | Mapping[str, Any]],
        *,
        replace_ids: Iterable[str] | None = None,
        expected_artifact_hash: str | None = None,
    ) -> tuple[AnalyticalRelationshipEvidence, ...]:
        """Replace analytical relationship evidence during an active repair.

        The default replaces the complete owner-local JSONL artifact.  Passing
        ``replace_ids`` performs a stable-ID subset replacement while retaining
        every other row and its order.  Both forms are validated as the same
        typed :class:`AnalyticalRelationshipEvidence` used by the append API;
        the durable item boundary performs the owner, lifecycle, hash, lock,
        and rollback checks.
        """

        self._assert_owner()
        if isinstance(relationships, (str, bytes)):
            raise TypeError("relationships must be an iterable of evidence values")
        try:
            raw_values = tuple(relationships)
        except TypeError as exc:
            raise TypeError("relationships must be an iterable of evidence values") from exc
        values: list[AnalyticalRelationshipEvidence] = []
        for index, relationship in enumerate(raw_values):
            if isinstance(relationship, AnalyticalRelationshipEvidence):
                value = relationship
            elif isinstance(relationship, Mapping):
                raw = dict(relationship)
                for field_name, expected in (
                    ("item_id", self.item_workspace.item_id),
                    ("owner_ref", self.owner_ref),
                ):
                    if field_name in raw and raw[field_name] != expected:
                        raise ValueError(f"relationships[{index}] {field_name} is invalid")
                value = AnalyticalRelationshipEvidence.from_dict(raw)
            else:
                raise TypeError("relationships must contain AnalyticalRelationshipEvidence or mappings")
            values.append(value)
        relationship_ids = [value.relationship_id for value in values]
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("relationship_id values must be unique")
        audit_ids = [value.audit_id for value in values if value.audit_id is not None]
        if len(audit_ids) != len(set(audit_ids)):
            raise ValueError("audit_id values must be unique")
        target_ids: tuple[str, ...] | None
        if replace_ids is None:
            target_ids = None
        else:
            target_ids = _text_tuple(replace_ids, field_name="replace_ids")
            if not target_ids:
                raise ValueError("replace_ids cannot be empty; omit it for a full replacement")
        rows = self.item_workspace.replace_analytical_relationships(
            tuple(
                {
                    **value.to_dict(),
                    "item_id": self.item_workspace.item_id,
                    "owner_ref": self.owner_ref,
                }
                for value in values
            ),
            owner_ref=self.owner_ref,
            replace_ids=target_ids,
            expected_artifact_hash=expected_artifact_hash,
        )
        return tuple(AnalyticalRelationshipEvidence.from_dict(row) for row in rows)

    def read_analytical_relationships(self) -> tuple[AnalyticalRelationshipEvidence, ...]:
        """Read this owner's relationship evidence and no-join audits."""

        self._assert_owner()
        records = self.item_workspace.read_analytical_relationships()
        values: list[AnalyticalRelationshipEvidence] = []
        for record in records:
            if record.get("item_id") != self.item_workspace.item_id or record.get("owner_ref") != self.owner_ref:
                raise ValueError("analytical relationship owner binding is invalid")
            values.append(AnalyticalRelationshipEvidence.from_dict(record))
        return tuple(values)

    def search_sources(self, query: str = "", *, limit: int = 20) -> tuple[AnalystSource, ...]:
        self.context.ensure_valid()
        entries = self.context.data_room.search(query, catalog=self.context.source_catalog.entries, limit=limit)
        return tuple(AnalystSource.from_entry(entry) for entry in entries)

    def sample_source(self, source_id: str, *, limit: int = 20) -> tuple[Mapping[str, Any], ...]:
        self.context.ensure_valid()
        return self.context.data_room.sample(self._entry(source_id), limit=limit)

    def source_categories(self, source_id: str, column: str, *, limit: int = 20) -> tuple[Any, ...]:
        self.context.ensure_valid()
        return self.context.data_room.categories(self._entry(source_id), column, limit=limit)

    def begin_analysis(
        self,
        *,
        objective: str,
        strategy: str,
        expected_outputs: Iterable[str] = (),
        assumptions: Iterable[str] = (),
    ) -> None:
        self._assert_owner()
        if self.item_workspace.mode == "requirement":
            self._require_requirement_plan(operation="analysis")
            if not self._has_current_semantic_scope_decision():
                raise ValueError(
                    "requirement analysis requires an explicit semantic scope decision for the current snapshot"
                )
        self.item_workspace.write_plan(
            {
                "record_kind": "analyst_plan",
                "item_id": self.item_workspace.item_id,
                "requirement_plan": _REQUIREMENT_PLAN_FILENAME if self.item_workspace.mode == "requirement" else None,
                "objective": _required_text(objective, field_name="objective"),
                "strategy": _required_text(strategy, field_name="strategy"),
                "expected_outputs": list(_text_tuple(expected_outputs, field_name="expected_outputs")),
                "assumptions": list(_text_tuple(assumptions, field_name="assumptions")),
            }
        )

    def select_sources(self, source_ids: Iterable[str], *, purpose: str) -> tuple[AnalystSource, ...]:
        self._assert_owner()
        purpose = _required_text(purpose, field_name="purpose")
        ids = _text_tuple(source_ids, field_name="source_ids")
        if not ids:
            raise ValueError("at least one source_id is required")
        selected = tuple(AnalystSource.from_entry(self._entry(source_id)) for source_id in ids)
        for source in selected:
            entry = self._entry(source.source_id)
            self.item_workspace.append_source_map(
                {
                    "record_kind": "analyst_source_selection",
                    "source_id": source.source_id,
                    "purpose": purpose,
                    "path": source.path,
                    "content_hash": entry.content_hash,
                    "columns": list(source.columns),
                    "row_count": source.row_count,
                    "row_count_exact": source.row_count_exact,
                }
            )
        return selected

    def replace_selected_sources(
        self,
        source_entries: Iterable[Mapping[str, Any]],
        *,
        expected_artifact_hash: str,
    ) -> tuple[AnalystSource, ...]:
        """Replace the complete source-selection map through the public facade.

        Entries must be the canonical rows produced by :meth:`select_sources`.
        The bound data-room catalog is the authority for source identity,
        member content hash, columns, and row-count metadata; callers cannot
        introduce an arbitrary path or counterfeit a catalog member.  The
        durable layer performs the owner-scoped CAS and atomic replacement.
        """

        self.context.ensure_valid()
        self._assert_owner()
        if isinstance(source_entries, (str, bytes)):
            raise TypeError("source_entries must be an iterable of mappings")
        try:
            rows = tuple(source_entries)
        except TypeError as exc:
            raise TypeError("source_entries must be an iterable of mappings") from exc
        if any(not isinstance(row, Mapping) for row in rows):
            raise TypeError("source_entries must contain mappings")

        # The durable validator enforces the complete canonical row shape and
        # unique source_id identities before any lock/write is attempted.
        canonical_rows = self.item_workspace._validate_source_map_rows(rows, unique=True)  # noqa: SLF001
        for index, row in enumerate(canonical_rows):
            try:
                entry = self._entry(row["source_id"])
            except (KeyError, ValueError) as exc:
                raise ValueError(f"source_entries[{index}] source_id is not in the bound catalog") from exc
            source = AnalystSource.from_entry(entry)
            if (
                row["path"] != source.path
                or row["content_hash"] != entry.content_hash
                or tuple(row["columns"]) != source.columns
                or row["row_count"] != source.row_count
                or row["row_count_exact"] != source.row_count_exact
            ):
                raise ValueError(f"source_entries[{index}] does not match the bound catalog member")

        replaced = self.item_workspace.replace_source_map(
            canonical_rows,
            owner_ref=self.owner_ref,
            expected_artifact_hash=expected_artifact_hash,
        )
        return tuple(AnalystSource.from_entry(self._entry(row["source_id"])) for row in replaced)

    def selected_sources(self) -> tuple[AnalystSource, ...]:
        """Return exact persisted source IDs in selection order."""

        self.context.ensure_valid()
        result: list[AnalystSource] = []
        seen: set[str] = set()
        path = self.item_workspace.work_root / "source_map.json"
        if path.is_symlink():
            raise ValueError("source_map.json cannot be a symlink")
        if not path.exists():
            return ()
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list) or any(not isinstance(record, Mapping) for record in records):
                raise ValueError
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("source_map.json is invalid") from exc
        for record in records:
            source_id = _required_text(record.get("source_id"), field_name="source_id")
            if source_id in seen:
                continue
            result.append(AnalystSource.from_entry(self._entry(source_id)))
            seen.add(source_id)
        return tuple(result)

    def record_evidence(self, note: EvidenceNote) -> None:
        self._assert_owner()
        if not isinstance(note, EvidenceNote):
            raise TypeError("note must be an EvidenceNote")
        existing = self._work_records(_EVIDENCE_FILENAME)
        if note.evidence_id in {str(value.get("evidence_id")) for value in existing}:
            raise ValueError("evidence_id is already recorded")
        self.item_workspace.append_evidence(note.to_dict())

    def replace_evidence_notes(
        self,
        notes: Iterable[EvidenceNote | Mapping[str, Any]],
        *,
        expected_artifact_hash: str | None = None,
    ) -> tuple[EvidenceNote, ...]:
        """Replace the complete evidence artifact during an evidence repair."""

        self._assert_owner()
        if isinstance(notes, (str, bytes)):
            raise TypeError("notes must be an iterable of evidence values")
        try:
            raw_values = tuple(notes)
        except TypeError as exc:
            raise TypeError("notes must be an iterable of evidence values") from exc
        if not raw_values:
            raise ValueError("notes replacement cannot be empty")

        required_fields = {
            "record_kind",
            "evidence_id",
            "conclusion",
            "method",
            "evidence_refs",
            "limitations",
            "facts",
        }
        values: list[EvidenceNote] = []
        for index, note in enumerate(raw_values):
            if isinstance(note, EvidenceNote):
                value = note
            elif isinstance(note, Mapping):
                raw = dict(note)
                if set(raw) != required_fields or raw.get("record_kind") != "analytical_evidence":
                    raise ValueError(f"notes[{index}] is not a canonical evidence note")
                raw.pop("record_kind", None)
                try:
                    value = EvidenceNote(**raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"notes[{index}] is invalid") from exc
                if dict(note) != value.to_dict():
                    raise ValueError(f"notes[{index}] is not canonical")
            else:
                raise TypeError("notes must contain EvidenceNote values or canonical mappings")
            values.append(value)
        evidence_ids = [value.evidence_id for value in values]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_id values must be unique")

        rows = self.item_workspace.replace_evidence_notes(
            tuple(value.to_dict() for value in values),
            owner_ref=self.owner_ref,
            expected_artifact_hash=expected_artifact_hash,
        )
        result: list[EvidenceNote] = []
        for row in rows:
            if set(row) != required_fields or row.get("record_kind") != "analytical_evidence":
                raise ValueError("durable evidence replacement returned an invalid row")
            raw = dict(row)
            raw.pop("record_kind", None)
            result.append(EvidenceNote(**raw))
        return tuple(result)

    def assign_specialist(self, task: SpecialistTask) -> None:
        self._assert_owner()
        if not isinstance(task, SpecialistTask):
            raise TypeError("task must be a SpecialistTask")
        existing = self._work_records(_SPECIALIST_TASKS_FILENAME)
        if task.task_id in {str(value.get("task_id")) for value in existing}:
            raise ValueError("specialist task_id is already recorded")
        for source_id in task.source_ids:
            self._entry(source_id)
        self.item_workspace.append_specialist_task(task.to_dict())

    @staticmethod
    def _decode_specialist_task(value: Mapping[str, Any]) -> SpecialistTask:
        """Decode one canonical durable specialist-task row."""

        if not isinstance(value, Mapping) or value.get("record_kind") != "specialist_task":
            raise ValueError("specialist task row is not canonical")
        raw = dict(value)
        raw.pop("record_kind", None)
        try:
            task = SpecialistTask(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("specialist task row is invalid") from exc
        if task.to_dict() != dict(value):
            raise ValueError("specialist task row is not canonical")
        return task

    @staticmethod
    def _decode_specialist_memo(value: Mapping[str, Any]) -> SpecialistMemo:
        """Decode one canonical durable specialist-memo row."""

        if not isinstance(value, Mapping) or value.get("record_kind") != "specialist_memo":
            raise ValueError("specialist memo row is not canonical")
        raw = dict(value)
        raw.pop("record_kind", None)
        try:
            memo = SpecialistMemo(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("specialist memo row is invalid") from exc
        if memo.to_dict() != dict(value):
            raise ValueError("specialist memo row is not canonical")
        return memo

    @classmethod
    def _read_specialist_tasks_for_item(cls, item_workspace: ItemWorkspace) -> tuple[SpecialistTask, ...]:
        if not isinstance(item_workspace, ItemWorkspace):
            raise TypeError("item_workspace must be an ItemWorkspace")
        rows = item_workspace._read_work_records(  # noqa: SLF001 - public facade owns validation
            _SPECIALIST_TASKS_FILENAME,
            label="specialist task",
        )
        values = tuple(cls._decode_specialist_task(row) for row in rows)
        task_ids = [value.task_id for value in values]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("specialist task_id values must be unique")
        return values

    @classmethod
    def _read_specialist_memos_for_item(cls, item_workspace: ItemWorkspace) -> tuple[SpecialistMemo, ...]:
        if not isinstance(item_workspace, ItemWorkspace):
            raise TypeError("item_workspace must be an ItemWorkspace")
        rows = item_workspace._read_work_records(  # noqa: SLF001 - public facade owns validation
            _SPECIALIST_MEMOS_FILENAME,
            label="specialist memo",
        )
        values = tuple(cls._decode_specialist_memo(row) for row in rows)
        memo_ids = [value.memo_id for value in values]
        if len(memo_ids) != len(set(memo_ids)):
            raise ValueError("specialist memo_id values must be unique")
        task_ids = [value.task_id for value in values]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("each specialist task may have at most one memo")
        return values

    def specialist_tasks(self) -> tuple[SpecialistTask, ...]:
        """Read the current typed specialist assignments for this item."""

        self._assert_owner()
        return self._read_specialist_tasks_for_item(self.item_workspace)

    def specialist_memos(self) -> tuple[SpecialistMemo, ...]:
        """Read the current typed specialist memos for this item."""

        self._assert_owner()
        return self._read_specialist_memos_for_item(self.item_workspace)

    def unresolved_specialist_tasks(self) -> tuple[SpecialistTask, ...]:
        """Return assigned tasks that have not yet received a memo."""

        tasks = self.specialist_tasks()
        memo_task_ids = {memo.task_id for memo in self.specialist_memos()}
        return tuple(task for task in tasks if task.task_id not in memo_task_ids)

    @classmethod
    def read_specialist_tasks_for_item(cls, item_workspace: ItemWorkspace) -> tuple[SpecialistTask, ...]:
        """Read typed specialist tasks without binding a second owner.

        Planner/coordinator status views use this read-only public facade while
        the Analytical Owner remains the sole writer of task assignments.
        """

        return cls._read_specialist_tasks_for_item(item_workspace)

    @classmethod
    def read_specialist_memos_for_item(cls, item_workspace: ItemWorkspace) -> tuple[SpecialistMemo, ...]:
        """Read typed specialist memos without binding a second owner."""

        return cls._read_specialist_memos_for_item(item_workspace)

    def record_specialist_memo(self, memo: SpecialistMemo) -> None:
        self._assert_owner()
        if not isinstance(memo, SpecialistMemo):
            raise TypeError("memo must be a SpecialistMemo")
        tasks = self._read_specialist_tasks_for_item(self.item_workspace)
        if memo.task_id not in {value.task_id for value in tasks}:
            raise ValueError("specialist memo references an unknown task_id")
        existing = self._read_specialist_memos_for_item(self.item_workspace)
        if memo.memo_id in {value.memo_id for value in existing}:
            raise ValueError("specialist memo_id is already recorded")
        if memo.task_id in {value.task_id for value in existing}:
            raise ValueError("specialist task already has a memo")
        self.item_workspace.append_specialist_memo(memo.to_dict())

    def run_analysis(
        self,
        script: str | Path,
        *,
        outputs: Iterable[str | Path] = (),
        deterministic_outputs: Iterable[str | Path] | Mapping[str | Path, str] = (),
        sample_limit: int = 20,
        timeout_seconds: float | None = None,
        output_bytes: int | None = None,
    ) -> ScriptRunReport:
        self._assert_owner()
        if self.item_workspace.mode == "requirement":
            self._require_requirement_plan(operation="calculation")
            if not self._has_current_semantic_scope_decision():
                raise ValueError(
                    "requirement calculation requires an explicit semantic scope decision for the current snapshot"
                )
        return self.context.script_runner.run_pipeline(
            script,
            allowed_outputs=outputs,
            deterministic_outputs=deterministic_outputs,
            sample_limit=sample_limit,
            timeout_seconds=timeout_seconds,
            output_bytes=output_bytes,
        )

    def prepare_data(
        self,
        prepared_asset_id: str,
        rows: Iterable[Mapping[str, Any]] | DataRoomMember | DataRoomCatalogEntry | str | Path,
        **metadata: Any,
    ) -> Any:
        """Materialize an item-local prepared candidate through the bound context."""

        self._assert_owner()
        self._require_requirement_plan(operation="data preparation")
        prepared_asset_id = _required_text(prepared_asset_id, field_name="prepared_asset_id")
        return self.context.save_prepared_candidate(prepared_asset_id, rows, **metadata)

    def submit_answer(self, answer: AnalystAnswer | str) -> dict[str, Any]:
        self._assert_owner()
        value = AnalystAnswer(answer=answer) if isinstance(answer, str) else answer
        if not isinstance(value, AnalystAnswer):
            raise TypeError("answer must be an AnalystAnswer or string")
        payload = value.to_dict(item_id=self.item_workspace.item_id)
        self.item_workspace.write_draft(payload)
        return payload

    def conclude_data_insufficiency(
        self,
        conclusion: DataInsufficiencyConclusion | None = None,
        **semantic_fields: Any,
    ) -> dict[str, Any]:
        """Record the Analytical Owner's explicit material data insufficiency."""

        self._assert_owner()
        if conclusion is None:
            conclusion = DataInsufficiencyConclusion(**semantic_fields)
        elif semantic_fields:
            raise ValueError("conclusion cannot be combined with semantic fields")
        if not isinstance(conclusion, DataInsufficiencyConclusion):
            raise TypeError("conclusion must be a DataInsufficiencyConclusion")
        return self.item_workspace.record_data_insufficiency_conclusion(
            conclusion.to_dict(),
            owner_ref=self.owner_ref,
        )

__all__ = [
    "AnalystAnswer",
    "AnalystBrief",
    "AnalystSource",
    "AnalystWorkspace",
    "AnalyticalRelationshipEvidence",
    "BusinessReviewAdapter",
    "DataInsufficiencyConclusion",
    "EvidenceNote",
    "IdentityDomainProposal",
    "RequirementAnalysisPlan",
    "RequirementAnalysisTask",
    "ReviewFinding",
    "SpecialistMemo",
    "SpecialistTask",
]
