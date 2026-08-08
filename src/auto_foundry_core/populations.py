"""Population accounting with explicit denominator and exclusion reasons."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping


class PopulationLedger:
    """Track one requirement-scoped population without double counting.

    ``base`` may be an iterable of stable record IDs or an integer count.  ID
    mode provides reconciliation; count mode is useful when a source exposes
    only aggregate facts.
    """

    def __init__(
        self,
        base: int | Iterable[Any] = (),
        *,
        population_id: str = "population",
        grain: str | None = None,
        period: str | None = None,
        dimensions: Mapping[str, Any] | None = None,
        source_refs: Iterable[str] = (),
        definition_refs: Iterable[str] = (),
        eligible: Iterable[Any] = (),
        excluded: Mapping[str, Iterable[Any]] | None = None,
        unresolved: Iterable[Any] = (),
    ) -> None:
        self.population_id = str(population_id)
        self.grain = grain
        self.period = period
        self.dimensions = dict(dimensions or {})
        self.source_refs = tuple(str(v) for v in source_refs)
        self.definition_refs = tuple(str(v) for v in definition_refs)
        self._count_only = isinstance(base, int)
        self._base_count = int(base) if self._count_only else None
        self.base_ids = set() if self._count_only else {self._key(v) for v in base}
        self.eligible_ids = {self._key(v) for v in eligible}
        self.excluded_reasons: dict[str, set[str]] = defaultdict(set)
        for reason, ids in (excluded or {}).items():
            self.excluded_reasons[str(reason)].update(self._key(v) for v in ids)
        self.unresolved_ids = {self._key(v) for v in unresolved}

    @staticmethod
    def _key(value: Any) -> str:
        return str(value)

    @property
    def base_count(self) -> int:
        return self._base_count if self._count_only else len(self.base_ids)

    @property
    def excluded_ids(self) -> set[str]:
        return set().union(*self.excluded_reasons.values()) if self.excluded_reasons else set()

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_ids)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_ids)

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved_ids)

    @property
    def denominator_count(self) -> int:
        return self.eligible_count

    def add_eligible(self, ids: Iterable[Any]) -> "PopulationLedger":
        self.eligible_ids.update(self._key(v) for v in ids)
        return self

    def exclude(self, ids: Iterable[Any], reason: str) -> "PopulationLedger":
        reason = str(reason).strip()
        if not reason:
            raise ValueError("exclusion reason must not be empty")
        self.excluded_reasons[reason].update(self._key(v) for v in ids)
        return self

    def mark_unresolved(self, ids: Iterable[Any]) -> "PopulationLedger":
        self.unresolved_ids.update(self._key(v) for v in ids)
        return self

    def reconcile(self) -> dict[str, Any]:
        accounted = self.eligible_count + self.excluded_count + self.unresolved_count
        # In ID mode an ID may have been entered in more than one state.  Keep
        # the overlap visible rather than silently changing the ledger.
        states = self.eligible_ids | self.excluded_ids | self.unresolved_ids
        overlaps = (self.eligible_ids & self.excluded_ids) | (self.eligible_ids & self.unresolved_ids) | (self.excluded_ids & self.unresolved_ids)
        unique_accounted = len(states) if not self._count_only else accounted
        return {
            "population_id": self.population_id,
            "base": self.base_count,
            "eligible": self.eligible_count,
            "excluded": self.excluded_count,
            "unresolved": self.unresolved_count,
            "denominator": self.denominator_count,
            "accounted": accounted,
            "unique_accounted": unique_accounted,
            "overlap_ids": sorted(overlaps),
            "reconciles": unique_accounted == self.base_count if not self._count_only else accounted == self.base_count,
            "reason_counts": {reason: len(ids) for reason, ids in self.excluded_reasons.items()},
            "limitations": ("Excluded reason counts may overlap; excluded is a unique-ID union.",),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "population_id": self.population_id,
            "grain": self.grain,
            "period": self.period,
            "dimensions": self.dimensions,
            "source_refs": list(self.source_refs),
            "definition_refs": list(self.definition_refs),
            "base_ids": sorted(self.base_ids),
            "base_count": self.base_count,
            "eligible_ids": sorted(self.eligible_ids),
            "excluded_reasons": {k: sorted(v) for k, v in self.excluded_reasons.items()},
            "unresolved_ids": sorted(self.unresolved_ids),
            "reconciliation": self.reconcile(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PopulationLedger":
        base = data.get("base_ids")
        if base is None:
            base = data.get("base_count", 0)
        return cls(
            base,
            population_id=data.get("population_id", "population"),
            grain=data.get("grain"), period=data.get("period"),
            dimensions=data.get("dimensions"), source_refs=data.get("source_refs", ()),
            definition_refs=data.get("definition_refs", ()), eligible=data.get("eligible_ids", ()),
            excluded=data.get("excluded_reasons", {}), unresolved=data.get("unresolved_ids", ()),
        )


def reconcile_population(**kwargs: Any) -> dict[str, Any]:
    return PopulationLedger(**kwargs).reconcile()


__all__ = ["PopulationLedger", "reconcile_population"]
