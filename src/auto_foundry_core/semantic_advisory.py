"""Non-gating suggestions for possible future semantic promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class SemanticPromotionSuggestion:
    """A repeated fact shape worth reviewing, never an automatic promotion."""

    candidate_key: str
    candidate_kind: str
    supporting_fact_ids: tuple[str, ...]
    reason: str
    status: str = "advisory"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "candidate_kind": self.candidate_kind,
            "supporting_fact_ids": list(self.supporting_fact_ids),
            "reason": self.reason,
            "status": self.status,
        }


def suggest_semantic_promotions(
    facts: Iterable[Mapping[str, Any]],
    *,
    minimum_repetitions: int = 2,
) -> tuple[SemanticPromotionSuggestion, ...]:
    """Suggest repeated metric definitions without promoting observed values."""

    if isinstance(minimum_repetitions, bool) or not isinstance(minimum_repetitions, int) or minimum_repetitions < 2:
        raise ValueError("minimum_repetitions must be an integer of at least two")
    grouped: dict[tuple[str, str], list[str]] = {}
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        if fact.get("semantic_role") != "current_observation_not_definition":
            continue
        metric = fact.get("metric")
        unit = fact.get("unit")
        fact_id = fact.get("observation_id", fact.get("fact_id", fact.get("record_id")))
        if not all(isinstance(value, str) and value.strip() for value in (metric, unit, fact_id)):
            continue
        grouped.setdefault((metric.strip(), unit.strip()), []).append(fact_id.strip())
    suggestions = []
    for (metric, unit), fact_ids in sorted(grouped.items()):
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if len(unique_ids) < minimum_repetitions:
            continue
        suggestions.append(
            SemanticPromotionSuggestion(
                candidate_key=f"metric:{metric}|unit:{unit}",
                candidate_kind="metric_definition",
                supporting_fact_ids=unique_ids,
                reason=(
                    f"The metric '{metric}' with unit '{unit}' appears in "
                    f"{len(unique_ids)} reviewed current observations; review a reusable definition."
                ),
            )
        )
    return tuple(suggestions)
