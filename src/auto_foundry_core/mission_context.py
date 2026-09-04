"""Schema-versioned mission context for semantic launch intake.

The requirement planner owns the semantic classification of an intake.  This
module deliberately does not classify language or infer domain vocabulary; it
only provides a small, immutable, hashable representation for the context the
planner explicitly identifies alongside analytical requirements.

``RequirementRecord`` remains the durable analytical unit.  ``MissionContext``
is a sidecar that can be persisted next to the requirement plan and consumed by
later Product work without changing the requirement execution contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


MISSION_CONTEXT_SCHEMA_VERSION = 1
MISSION_INTENTS = frozenset({"discovery", "specification", "hybrid"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _canonical(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable bytes used for MissionContext content hashes."""

    return (
        json.dumps(
            _canonical(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _text(value: Any, field_name: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _string_tuple(value: Iterable[Any] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of strings")
    result = tuple(_text(item, field_name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _mapping(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return {str(key): _canonical(item) for key, item in value.items()}


@dataclass(frozen=True)
class SourceBinding:
    """Evidence binding for one context value.

    ``source_ref`` is an intake block id, a data-room document member, or an
    opaque source identifier.  ``locator`` carries format-specific page,
    sheet, section, cell, paragraph, or row information.  ``span`` is used
    for exact character offsets into an intake block when available.
    """

    source_ref: str
    locator: Mapping[str, Any] = field(default_factory=dict)
    span: Mapping[str, Any] | None = None
    content_hash: str | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ref", _text(self.source_ref, "source_ref"))
        object.__setattr__(self, "locator", _mapping(self.locator, "locator"))
        if self.span is not None:
            span = _mapping(self.span, "span")
            for name in ("start", "end"):
                if name in span and (
                    isinstance(span[name], bool) or not isinstance(span[name], int) or span[name] < 0
                ):
                    raise ValueError("source span offsets must be non-negative integers")
            if "start" in span and "end" in span and span["end"] <= span["start"]:
                raise ValueError("source span end must be greater than start")
            object.__setattr__(self, "span", span)
        if self.content_hash is not None:
            digest = _text(self.content_hash, "content_hash").lower()
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError("content_hash must be a SHA-256 hex digest")
            object.__setattr__(self, "content_hash", digest)
        if self.text is not None:
            object.__setattr__(self, "text", _text(self.text, "text"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | "SourceBinding") -> "SourceBinding":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("source binding must be an object")
        raw = dict(value)
        source_ref = raw.pop("source_ref", raw.pop("sourceRef", raw.pop("document_ref", raw.pop("documentRef", None))))
        if source_ref is None:
            source_ref = raw.pop("ref", raw.pop("blockId", None))
        locator = raw.pop("locator", raw.pop("location", {}))
        span = raw.pop("span", None)
        if span is None and {"start", "end"}.issubset(raw):
            span = {"start": raw.pop("start"), "end": raw.pop("end")}
            if "blockId" in raw:
                span["blockId"] = raw.pop("blockId")
        content_hash = raw.pop("content_hash", raw.pop("contentHash", None))
        text = raw.pop("text", None)
        if raw:
            # Preserve forward-compatible locator fields rather than dropping
            # provenance a newer planner supplied.
            locator = {**dict(locator or {}), **raw}
        return cls(source_ref=source_ref, locator=locator, span=span, content_hash=content_hash, text=text)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "source_ref": self.source_ref,
            "locator": dict(self.locator),
            "span": None if self.span is None else dict(self.span),
            "content_hash": self.content_hash,
            "text": self.text,
        }
        return value


@dataclass(frozen=True)
class ContextItem:
    """One source-grounded, non-analytical context statement."""

    text: str
    source_bindings: tuple[SourceBinding, ...] = ()
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _text(self.text, "text"))
        bindings = tuple(
            SourceBinding.from_dict(value) if isinstance(value, Mapping) else value
            for value in (self.source_bindings or ())
        )
        if any(not isinstance(value, SourceBinding) for value in bindings):
            raise TypeError("source_bindings must contain SourceBinding values")
        if not bindings:
            raise ValueError("context items require at least one source binding")
        object.__setattr__(self, "source_bindings", bindings)
        if self.reason is not None:
            object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | "ContextItem", *, default_reason: str | None = None) -> "ContextItem":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            raise TypeError("context item strings need source bindings")
        if not isinstance(value, Mapping):
            raise TypeError("context item must be an object")
        raw = dict(value)
        text = raw.pop("text", raw.pop("value", raw.pop("statement", None)))
        raw_bindings = raw.pop("source_bindings", raw.pop("sourceBindings", raw.pop("bindings", None)))
        if raw_bindings is None:
            raw_bindings = []
            spans = raw.pop("sourceSpans", raw.pop("source_spans", []))
            document_refs = raw.pop("documentRefs", raw.pop("document_refs", []))
            if isinstance(spans, Mapping):
                spans = [spans]
            for span in spans or ():
                if isinstance(span, Mapping):
                    block_id = span.get("blockId", span.get("block_id"))
                    raw_bindings.append(
                        {
                            "source_ref": block_id,
                            "span": {
                                "blockId": block_id,
                                "start": span.get("start"),
                                "end": span.get("end"),
                            },
                        }
                    )
            for ref in document_refs or ():
                raw_bindings.append({"source_ref": ref})
        if isinstance(raw_bindings, Mapping):
            raw_bindings = [raw_bindings]
        reason = raw.pop("reason", default_reason)
        metadata = raw.pop("metadata", {})
        if raw:
            metadata = {**dict(metadata or {}), **raw}
        return cls(
            text=text,
            source_bindings=tuple(SourceBinding.from_dict(item) for item in (raw_bindings or ())),
            reason=reason,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source_bindings": [item.to_dict() for item in self.source_bindings],
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


def _context_items(value: Iterable[Any] | None, field_name: str, *, default_reason: str | None = None) -> tuple[ContextItem, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{field_name} must be a list of context items")
    return tuple(ContextItem.from_dict(item, default_reason=default_reason) for item in value)


@dataclass(frozen=True)
class ProductBrief:
    """Product-facing brief fields kept separate from AO requirements."""

    audience: tuple[ContextItem, ...] = ()
    decision: tuple[ContextItem, ...] = ()
    deliverables: tuple[ContextItem, ...] = ()
    pages_or_modules: tuple[ContextItem, ...] = ()
    filters: tuple[ContextItem, ...] = ()
    visual_expectations: tuple[ContextItem, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    FIELDS = (
        "audience",
        "decision",
        "deliverables",
        "pages_or_modules",
        "filters",
        "visual_expectations",
    )

    def __post_init__(self) -> None:
        for name in self.FIELDS:
            value = getattr(self, name)
            if isinstance(value, (str, bytes)):
                raise TypeError(f"product_brief.{name} must be a list of context items")
            items = tuple(ContextItem.from_dict(item) if isinstance(item, Mapping) else item for item in (value or ()))
            if any(not isinstance(item, ContextItem) for item in items):
                raise TypeError(f"product_brief.{name} must contain ContextItem values")
            object.__setattr__(self, name, items)
        object.__setattr__(self, "metadata", _mapping(self.metadata, "product_brief.metadata"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | "ProductBrief" | None) -> "ProductBrief":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("product_brief must be an object")
        raw = dict(value)
        aliases = {
            "pagesOrModules": "pages_or_modules",
            "visualExpectations": "visual_expectations",
        }
        normalized = {aliases.get(key, key): item for key, item in raw.items()}
        metadata = normalized.pop("metadata", {})
        kwargs: dict[str, Any] = {}
        for name in cls.FIELDS:
            kwargs[name] = _context_items(normalized.pop(name, ()), f"product_brief.{name}")
        if normalized:
            metadata = {**dict(metadata or {}), **normalized}
        kwargs["metadata"] = metadata
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            **{name: [item.to_dict() for item in getattr(self, name)] for name in self.FIELDS},
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MissionContext:
    """Typed, source-grounded context sidecar for one mission intake."""

    mission_intent: str
    product_brief: ProductBrief = field(default_factory=ProductBrief)
    source_context: tuple[ContextItem, ...] = ()
    technical_constraints: tuple[ContextItem, ...] = ()
    additional_context: tuple[ContextItem, ...] = ()
    document_catalog: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = MISSION_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        intent = _text(self.mission_intent, "mission_intent").lower()
        if intent not in MISSION_INTENTS:
            raise ValueError(f"mission_intent must be one of {sorted(MISSION_INTENTS)}")
        object.__setattr__(self, "mission_intent", intent)
        if isinstance(self.product_brief, Mapping):
            object.__setattr__(self, "product_brief", ProductBrief.from_dict(self.product_brief))
        elif not isinstance(self.product_brief, ProductBrief):
            raise TypeError("product_brief must be a ProductBrief")
        for name in ("source_context", "technical_constraints", "additional_context"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)):
                raise TypeError(f"{name} must be a list of context items")
            items = tuple(ContextItem.from_dict(item) if isinstance(item, Mapping) else item for item in (value or ()))
            if any(not isinstance(item, ContextItem) for item in items):
                raise TypeError(f"{name} must contain ContextItem values")
            if name == "additional_context" and any(not item.reason for item in items):
                raise ValueError("additional_context items require a reason")
            object.__setattr__(self, name, items)
        if isinstance(self.schema_version, bool) or self.schema_version != MISSION_CONTEXT_SCHEMA_VERSION:
            raise ValueError("unsupported mission context schema version")
        object.__setattr__(self, "document_catalog", None if self.document_catalog is None else _mapping(self.document_catalog, "document_catalog"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))
        validate_mission_context_catalog(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | "MissionContext") -> "MissionContext":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("mission context must be an object")
        raw = dict(value)
        if "mission_intent" not in raw and "missionIntent" not in raw and isinstance(raw.get("context"), Mapping):
            wrapper_hash = raw.get("contextHash", raw.get("context_hash"))
            raw = dict(raw["context"])
            if wrapper_hash is not None:
                raw["context_hash"] = wrapper_hash
        supplied_hash = raw.pop("context_hash", raw.pop("contextHash", None))
        schema_version = raw.pop("schema_version", raw.pop("schemaVersion", MISSION_CONTEXT_SCHEMA_VERSION))
        mission_intent = raw.pop("mission_intent", raw.pop("missionIntent", None))
        product_brief = raw.pop("product_brief", raw.pop("productBrief", None))
        source_context = raw.pop("source_context", raw.pop("sourceContext", ()))
        technical_constraints = raw.pop("technical_constraints", raw.pop("technicalConstraints", ()))
        additional_context = raw.pop("additional_context", raw.pop("additionalContext", raw.pop("unassignedContext", ())))
        document_catalog = raw.pop("document_catalog", raw.pop("documentCatalog", None))
        metadata = raw.pop("metadata", {})
        if raw:
            metadata = {**dict(metadata or {}), **raw}
        context = cls(
            mission_intent=mission_intent,
            product_brief=ProductBrief.from_dict(product_brief),
            source_context=_context_items(source_context, "source_context"),
            technical_constraints=_context_items(technical_constraints, "technical_constraints"),
            additional_context=_context_items(additional_context, "additional_context", default_reason="additional context"),
            document_catalog=document_catalog,
            metadata=metadata,
            schema_version=schema_version,
        )
        if supplied_hash is not None and supplied_hash != context.context_hash:
            raise ValueError("mission context hash does not match content")
        return context

    def body_dict(self) -> dict[str, Any]:
        """Return the hash input, excluding the derived ``context_hash``."""

        return {
            "schema_version": self.schema_version,
            "mission_intent": self.mission_intent,
            "product_brief": self.product_brief.to_dict(),
            "source_context": [item.to_dict() for item in self.source_context],
            "technical_constraints": [item.to_dict() for item in self.technical_constraints],
            "additional_context": [item.to_dict() for item in self.additional_context],
            "document_catalog": None if self.document_catalog is None else dict(self.document_catalog),
            "metadata": dict(self.metadata),
        }

    @property
    def context_hash(self) -> str:
        return sha256_value(self.body_dict())

    @property
    def hash(self) -> str:
        """Short alias used by manifest/adaptor callers."""

        return self.context_hash

    @property
    def source_bindings(self) -> tuple[SourceBinding, ...]:
        values: list[SourceBinding] = []
        for item in (
            *self.product_brief.audience,
            *self.product_brief.decision,
            *self.product_brief.deliverables,
            *self.product_brief.pages_or_modules,
            *self.product_brief.filters,
            *self.product_brief.visual_expectations,
            *self.source_context,
            *self.technical_constraints,
            *self.additional_context,
        ):
            values.extend(item.source_bindings)
        return tuple(values)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = self.body_dict()
        if include_hash:
            value["context_hash"] = self.context_hash
        return value


@dataclass(frozen=True)
class MissionPlan:
    """Mission sidecar binding context to the analytical requirement IDs."""

    mission_context: MissionContext | Mapping[str, Any]
    requirement_ids: tuple[str, ...] = ()
    portfolio_strategy: str = "semantic mission plan"
    planner_ref: str = "semantic-intake-planner"
    schema_version: int = MISSION_CONTEXT_SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        context = MissionContext.from_dict(self.mission_context)
        object.__setattr__(self, "mission_context", context)
        object.__setattr__(self, "requirement_ids", _string_tuple(self.requirement_ids, "requirement_ids"))
        object.__setattr__(self, "portfolio_strategy", _text(self.portfolio_strategy, "portfolio_strategy"))
        object.__setattr__(self, "planner_ref", _text(self.planner_ref, "planner_ref"))
        if isinstance(self.schema_version, bool) or self.schema_version != MISSION_CONTEXT_SCHEMA_VERSION:
            raise ValueError("unsupported mission plan schema version")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | "MissionPlan") -> "MissionPlan":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("mission plan must be an object")
        raw = dict(value)
        if "mission_context" not in raw and "missionContext" not in raw and isinstance(raw.get("missionPlan"), Mapping):
            raw = dict(raw["missionPlan"])
        supplied_context_hash = raw.pop("context_hash", raw.pop("contextHash", None))
        supplied_plan_hash = raw.pop("plan_hash", raw.pop("planHash", None))
        schema_version = raw.pop("schema_version", raw.pop("schemaVersion", MISSION_CONTEXT_SCHEMA_VERSION))
        context = raw.pop("mission_context", raw.pop("missionContext", raw.pop("context", None)))
        requirement_ids = raw.pop("requirement_ids", raw.pop("requirementIds", raw.pop("requirements", ())))
        if requirement_ids and all(isinstance(item, Mapping) for item in requirement_ids):
            requirement_ids = tuple(item.get("requirement_id", item.get("requirementId", "")) for item in requirement_ids)
        portfolio_strategy = raw.pop("portfolio_strategy", raw.pop("portfolioStrategy", "semantic mission plan"))
        planner_ref = raw.pop("planner_ref", raw.pop("plannerRef", "semantic-intake-planner"))
        metadata = raw.pop("metadata", {})
        if raw:
            metadata = {**dict(metadata or {}), **raw}
        plan = cls(
            mission_context=context,
            requirement_ids=requirement_ids,
            portfolio_strategy=portfolio_strategy,
            planner_ref=planner_ref,
            schema_version=schema_version,
            metadata=metadata,
        )
        if supplied_context_hash is not None and supplied_context_hash != plan.context_hash:
            raise ValueError("mission plan context hash does not match content")
        if supplied_plan_hash is not None and supplied_plan_hash != plan.plan_hash:
            raise ValueError("mission plan hash does not match content")
        return plan

    @property
    def context_hash(self) -> str:
        return self.mission_context.context_hash

    @property
    def hash(self) -> str:
        return self.plan_hash

    @property
    def plan_hash(self) -> str:
        body = {
            "schema_version": self.schema_version,
            "mission_context_hash": self.mission_context.context_hash,
            "requirement_ids": list(self.requirement_ids),
            "portfolio_strategy": self.portfolio_strategy,
            "planner_ref": self.planner_ref,
            "metadata": dict(self.metadata),
        }
        return sha256_value(body)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "mission_context": self.mission_context.to_dict(include_hash=include_hash),
            "requirement_ids": list(self.requirement_ids),
            "portfolio_strategy": self.portfolio_strategy,
            "planner_ref": self.planner_ref,
            "metadata": dict(self.metadata),
        }
        if include_hash:
            value["context_hash"] = self.context_hash
            value["plan_hash"] = self.plan_hash
        return value


def mission_context_hash(value: MissionContext | Mapping[str, Any]) -> str:
    """Hash a context object or wire mapping after strict normalization."""

    return MissionContext.from_dict(value).context_hash


def validate_mission_context_catalog(
    context: MissionContext,
    catalog: Mapping[str, Any] | None = None,
) -> None:
    """Validate document bindings against one trusted normalized section.

    Block/input bindings are intentionally ignored here; launch-level exact
    span validation handles those.  Any binding that names a catalog document
    must identify exactly one section and carry that section's hash and text.
    A document-like binding carrying a hash but no trusted ref is rejected so
    an active sidecar cannot smuggle unverifiable evidence.
    """

    if not isinstance(context, MissionContext):
        raise TypeError("context must be a MissionContext")
    if catalog is None:
        catalog = context.document_catalog
    if catalog is None:
        return
    from auto_foundry_core.document_ingestion import DocumentCatalog

    trusted = DocumentCatalog.from_dict(catalog)
    documents = {document.document_ref: document for document in trusted.documents}
    for binding in context.source_bindings:
        document = documents.get(binding.source_ref)
        if document is None:
            if binding.span is None:
                raise ValueError("document source binding is not present in trusted catalog")
            continue
        if document.extraction != "normalized":
            raise ValueError("document source binding references a limited or opaque document")
        if not binding.locator:
            raise ValueError("document source binding needs a normalized section locator")
        matches = [
            section
            for section in document.sections
            if all(section.locator.get(key) == value for key, value in binding.locator.items())
        ]
        if len(matches) != 1:
            raise ValueError("document source binding locator does not identify one section")
        section = matches[0]
        if section.limitations:
            raise ValueError("document source binding references a limited section")
        if binding.content_hash != section.content_hash:
            raise ValueError("document source binding hash does not match its section")
        if binding.text != section.text:
            raise ValueError("document source binding text does not match its section")


def merge_mission_contexts(
    parent: MissionContext | Mapping[str, Any],
    child: MissionContext | Mapping[str, Any],
) -> MissionContext:
    """Return a deterministic cumulative context for a continuation.

    Continuations are planned from a new brief, but the durable mission
    context is cumulative.  Preserve parent items first, append genuinely new
    child items, and record a hash-bound lineage marker in metadata.  The
    document identities are merged by immutable source path/content hash.  A
    same-path/same-hash child reuses the parent ref; changed bytes receive a
    deterministic revision-qualified ref and child bindings are rewritten to
    that ref.  Parent catalog entries are never overwritten.
    """

    parent_context = MissionContext.from_dict(parent)
    child_context = MissionContext.from_dict(child)

    def merge_items(first: Iterable[ContextItem], second: Iterable[ContextItem]) -> tuple[ContextItem, ...]:
        values: list[ContextItem] = []
        seen: set[str] = set()
        for item in (*tuple(first), *tuple(second)):
            key = canonical_json_bytes(item.to_dict()).decode("utf-8")
            if key in seen:
                continue
            seen.add(key)
            values.append(item)
        return tuple(values)

    # Reconcile trusted document identities before merging any context item.
    # The ingestion helper has no dependency on this module, so importing it
    # lazily keeps the core modules independently usable.
    from auto_foundry_core.document_ingestion import revision_qualify_catalog

    qualified_catalog, child_ref_map = revision_qualify_catalog(
        parent_context.document_catalog,
        child_context.document_catalog,
    )

    def remap_item(item: ContextItem) -> ContextItem:
        bindings: list[SourceBinding] = []
        changed = False
        for binding in item.source_bindings:
            target_ref = child_ref_map.get(binding.source_ref, binding.source_ref)
            if target_ref != binding.source_ref:
                changed = True
                bindings.append(
                    SourceBinding(
                        source_ref=target_ref,
                        locator=binding.locator,
                        span=binding.span,
                        content_hash=binding.content_hash,
                        text=binding.text,
                    )
                )
            else:
                bindings.append(binding)
        if not changed:
            return item
        return ContextItem(
            text=item.text,
            source_bindings=tuple(bindings),
            reason=item.reason,
            metadata=item.metadata,
        )

    def remap_items(items: Iterable[ContextItem]) -> tuple[ContextItem, ...]:
        return tuple(remap_item(item) for item in items)

    parent_brief = parent_context.product_brief
    child_brief = child_context.product_brief
    product_brief = ProductBrief(
        audience=merge_items(parent_brief.audience, remap_items(child_brief.audience)),
        decision=merge_items(parent_brief.decision, remap_items(child_brief.decision)),
        deliverables=merge_items(parent_brief.deliverables, remap_items(child_brief.deliverables)),
        pages_or_modules=merge_items(parent_brief.pages_or_modules, remap_items(child_brief.pages_or_modules)),
        filters=merge_items(parent_brief.filters, remap_items(child_brief.filters)),
        visual_expectations=merge_items(parent_brief.visual_expectations, remap_items(child_brief.visual_expectations)),
        metadata={**dict(parent_brief.metadata), **dict(child_brief.metadata)},
    )

    lineage = parent_context.metadata.get("context_lineage", ())
    if isinstance(lineage, str):
        lineage = (lineage,)
    elif not isinstance(lineage, (list, tuple)):
        lineage = ()
    lineage_values = [str(item) for item in lineage if str(item)]
    if not lineage_values or lineage_values[-1] != parent_context.context_hash:
        lineage_values.append(parent_context.context_hash)
    lineage_values.append(child_context.context_hash)
    metadata = {
        **dict(parent_context.metadata),
        **dict(child_context.metadata),
        "parent_context_hash": parent_context.context_hash,
        "context_lineage": list(dict.fromkeys(lineage_values)),
    }
    return MissionContext(
        mission_intent=child_context.mission_intent,
        product_brief=product_brief,
        source_context=merge_items(parent_context.source_context, remap_items(child_context.source_context)),
        technical_constraints=merge_items(parent_context.technical_constraints, remap_items(child_context.technical_constraints)),
        additional_context=merge_items(parent_context.additional_context, remap_items(child_context.additional_context)),
        document_catalog=None if qualified_catalog is None else qualified_catalog.to_dict(),
        metadata=metadata,
        schema_version=child_context.schema_version,
    )


__all__ = [
    "MISSION_CONTEXT_SCHEMA_VERSION",
    "MISSION_INTENTS",
    "SourceBinding",
    "ContextItem",
    "ProductBrief",
    "MissionContext",
    "MissionPlan",
    "canonical_json_bytes",
    "sha256_value",
    "mission_context_hash",
    "validate_mission_context_catalog",
    "merge_mission_contexts",
]
