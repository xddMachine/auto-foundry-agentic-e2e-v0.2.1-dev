"""Population accounting with explicit denominator and exclusion reasons."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping


class PopulationLedger:
    """Track one requirement-scoped population by stable record IDs.

    Reconciliation is intentionally ID based.  An integer ``base`` cannot
    establish the exactly-once identity guarantees required here and is
    rejected rather than treated as a weaker compatibility mode.
    """

    def __init__(
        self,
        base: Iterable[Any] = (),
        *,
        population_id: str = "population",
        grain: str | None = None,
        period: str | None = None,
        dimensions: Mapping[str, Any] | None = None,
        source_refs: Iterable[str] = (),
        definition_refs: Iterable[str] = (),
        eligible: Iterable[Any] = (),
        excluded: Mapping[str, Iterable[Any] | Any] | None = None,
        unresolved: Iterable[Any] = (),
    ) -> None:
        if isinstance(base, (int, float, bool)):
            raise TypeError("PopulationLedger base must be an iterable of IDs; count-only reconciliation is unsupported")
        self.population_id = str(population_id)
        self.grain = grain
        self.period = period
        self.dimensions = dict(dimensions or {})
        self.source_refs = tuple(str(value) for value in source_refs)
        self.definition_refs = tuple(str(value) for value in definition_refs)

        base_entries = [self._key(value) for value in base]
        self._base_entries = tuple(base_entries)
        self.base_ids = set(base_entries)
        self.duplicate_base_ids = {value for value, count in Counter(base_entries).items() if count > 1}
        self.eligible_ids = self._collect_ids(eligible)
        self.excluded_reasons: dict[str, set[str]] = defaultdict(set)
        for reason, ids in (excluded or {}).items():
            self.excluded_reasons[str(reason)].update(self._collect_ids(ids))
        self.unresolved_ids = self._collect_ids(unresolved)

    @staticmethod
    def _key(value: Any) -> str:
        return str(value)

    @classmethod
    def _collect_ids(cls, values: Iterable[Any] | Any) -> set[str]:
        # A scalar string is one stable ID, not an iterable of characters.
        if isinstance(values, (str, bytes)):
            return {cls._key(values)}
        try:
            return {cls._key(value) for value in values}
        except TypeError:
            return {cls._key(values)}

    @property
    def base_count(self) -> int:
        return len(self.base_ids)

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
        self.eligible_ids.update(self._collect_ids(ids))
        return self

    def exclude(self, ids: Iterable[Any], reason: str) -> "PopulationLedger":
        reason = str(reason).strip()
        if not reason:
            raise ValueError("exclusion reason must not be empty")
        self.excluded_reasons[reason].update(self._collect_ids(ids))
        return self

    def mark_unresolved(self, ids: Iterable[Any]) -> "PopulationLedger":
        self.unresolved_ids.update(self._collect_ids(ids))
        return self

    def reconcile(self) -> dict[str, Any]:
        excluded_ids = self.excluded_ids
        states = self.eligible_ids | excluded_ids | self.unresolved_ids
        overlap_ids = sorted(
            (self.eligible_ids & excluded_ids)
            | (self.eligible_ids & self.unresolved_ids)
            | (excluded_ids & self.unresolved_ids)
        )
        out_of_base_ids = sorted(states - self.base_ids)
        missing_base_ids = sorted(self.base_ids - states)

        reason_membership: dict[str, list[str]] = defaultdict(list)
        for reason, ids in self.excluded_reasons.items():
            for value in ids:
                reason_membership[value].append(reason)
        duplicate_exclusion_reason_ids = {
            value: sorted(reasons) for value, reasons in reason_membership.items() if len(reasons) > 1
        }

        violations = {
            "overlaps": overlap_ids,
            "out_of_base": out_of_base_ids,
            "missing_base": missing_base_ids,
            "duplicate_exclusion_reasons": duplicate_exclusion_reason_ids,
            "duplicate_base_ids": sorted(self.duplicate_base_ids),
        }
        reconciles = not any(violations.values())
        accounted = self.eligible_count + self.excluded_count + self.unresolved_count
        unique_accounted = len(states)
        return {
            "population_id": self.population_id,
            "base": self.base_count,
            "base_ids": sorted(self.base_ids),
            "eligible": self.eligible_count,
            "excluded": self.excluded_count,
            "unresolved": self.unresolved_count,
            "denominator": self.denominator_count,
            "accounted": accounted,
            "unique_accounted": unique_accounted,
            # Keep the direct names easy to consume while retaining the
            # ``*_ids`` names used by the serialized ledger representation.
            "overlaps": overlap_ids,
            "overlap_ids": overlap_ids,
            "out_of_base": out_of_base_ids,
            "out_of_base_ids": out_of_base_ids,
            "missing_base": missing_base_ids,
            "missing_base_ids": missing_base_ids,
            "duplicate_exclusion_reasons": duplicate_exclusion_reason_ids,
            "duplicate_base_ids": sorted(self.duplicate_base_ids),
            "violations": violations,
            "reconciles": reconciles,
            "reason_counts": {reason: len(ids) for reason, ids in self.excluded_reasons.items()},
            "limitations": (),
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
            "excluded_reasons": {key: sorted(values) for key, values in self.excluded_reasons.items()},
            "unresolved_ids": sorted(self.unresolved_ids),
            "reconciliation": self.reconcile(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PopulationLedger":
        if "base_ids" not in data:
            raise ValueError("PopulationLedger requires explicit base_ids")
        return cls(
            data["base_ids"],
            population_id=data.get("population_id", "population"),
            grain=data.get("grain"),
            period=data.get("period"),
            dimensions=data.get("dimensions"),
            source_refs=data.get("source_refs", ()),
            definition_refs=data.get("definition_refs", ()),
            eligible=data.get("eligible_ids", ()),
            excluded=data.get("excluded_reasons", {}),
            unresolved=data.get("unresolved_ids", ()),
        )


def reconcile_population(**kwargs: Any) -> dict[str, Any]:
    return PopulationLedger(**kwargs).reconcile()


__all__ = ["PopulationLedger", "reconcile_population"]
