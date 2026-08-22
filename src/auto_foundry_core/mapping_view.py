"""Typed, script-consumable views over reviewed canonical identity mappings."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import CanonicalMapping, IdentityDecision


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class IdentityMappingView:
    """Deterministic lookup view built only from reviewed accepted mappings."""

    mappings: tuple[CanonicalMapping, ...]
    decisions: tuple[IdentityDecision, ...]
    source_to_canonical: Mapping[str, str]
    ambiguous_sources: Mapping[str, tuple[str, ...]]

    @classmethod
    def build(
        cls,
        mappings: Iterable[CanonicalMapping | Mapping[str, Any]],
        decisions: Iterable[IdentityDecision | Mapping[str, Any]],
    ) -> "IdentityMappingView":
        normalized_mappings = tuple(
            value if isinstance(value, CanonicalMapping) else CanonicalMapping.from_dict(value)
            for value in mappings
        )
        normalized_decisions = tuple(
            value if isinstance(value, IdentityDecision) else IdentityDecision.from_dict(value)
            for value in decisions
        )
        if len({value.canonical_id for value in normalized_mappings}) != len(normalized_mappings):
            raise ValueError("identity mapping view contains duplicate canonical_id values")
        if len({value.decision_id for value in normalized_decisions}) != len(normalized_decisions):
            raise ValueError("identity mapping view contains duplicate decision_id values")
        decision_by_id = {value.decision_id: value for value in normalized_decisions}
        candidates: dict[str, set[str]] = {}
        for mapping in normalized_mappings:
            if mapping.status != "accepted":
                raise ValueError("identity mapping view requires accepted mappings")
            decision = decision_by_id.get(mapping.decision_id)
            if decision is None or decision.review_status not in {"reviewed", "accepted"}:
                raise ValueError(f"identity mapping view has no reviewed decision: {mapping.decision_id}")
            for source_identity in mapping.source_identities:
                candidates.setdefault(source_identity, set()).add(mapping.canonical_id)
        source_to_canonical = {
            source_identity: next(iter(canonical_ids))
            for source_identity, canonical_ids in candidates.items()
            if len(canonical_ids) == 1
        }
        ambiguous_sources = {
            source_identity: tuple(sorted(canonical_ids))
            for source_identity, canonical_ids in candidates.items()
            if len(canonical_ids) > 1
        }
        return cls(
            mappings=tuple(sorted(normalized_mappings, key=lambda value: value.canonical_id)),
            decisions=tuple(sorted(normalized_decisions, key=lambda value: value.decision_id or "")),
            source_to_canonical=MappingProxyType(dict(sorted(source_to_canonical.items()))),
            ambiguous_sources=MappingProxyType(dict(sorted(ambiguous_sources.items()))),
        )

    def resolve(self, source_identity: str) -> str | None:
        """Return one reviewed canonical ID, never a guessed ambiguous match."""

        return self.source_to_canonical.get(str(source_identity))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "identity_mapping_view",
            "canonical_mappings": [value.to_dict() for value in self.mappings],
            "identity_decisions": [value.to_dict() for value in self.decisions],
            "source_to_canonical": dict(self.source_to_canonical),
            "ambiguous_sources": {
                key: list(value) for key, value in self.ambiguous_sources.items()
            },
        }

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(_json_bytes(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IdentityMappingView":
        if not isinstance(value, Mapping):
            raise TypeError("identity mapping view must be a mapping")
        expected = {
            "schema_version",
            "kind",
            "canonical_mappings",
            "identity_decisions",
            "source_to_canonical",
            "ambiguous_sources",
        }
        if set(value) != expected or value.get("schema_version") != 1 or value.get("kind") != "identity_mapping_view":
            raise ValueError("identity mapping view fields are invalid")
        rebuilt = cls.build(value["canonical_mappings"], value["identity_decisions"])
        if rebuilt.to_dict() != dict(value):
            raise ValueError("identity mapping view indexes do not match reviewed mappings")
        return rebuilt

    @classmethod
    def load(cls, path: str | Path) -> "IdentityMappingView":
        candidate = Path(path)
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("identity mapping view must be a regular file")
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("identity mapping view is invalid") from exc
        return cls.from_dict(value)


@dataclass(frozen=True)
class MappingCompletenessAdvisory:
    """Non-gating background summary derived from an ER result."""

    domain_id: str
    status: str
    canonical_mapping_count: int = 0
    mapped_source_identity_count: int = 0
    unresolved_record_count: int = 0
    exception_record_count: int = 0
    coverage: Mapping[str, Any] | None = None
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain_id": self.domain_id,
            "status": self.status,
            "canonical_mapping_count": self.canonical_mapping_count,
            "mapped_source_identity_count": self.mapped_source_identity_count,
            "unresolved_record_count": self.unresolved_record_count,
            "exception_record_count": self.exception_record_count,
            "coverage": dict(self.coverage or {}),
            "warning": self.warning,
        }
