"""Object-generic identity candidate evidence and reviewed mapping support."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence

from .contracts import CanonicalMapping, IdentityCandidate, IdentityDecision, IdentityEvidence
from .normalization import normalize_identifier, normalize_string


def _id(row: Mapping[str, Any], index: int, id_field: str | None) -> str:
    if id_field and row.get(id_field) is not None:
        return str(row[id_field])
    for key in ("id", "_id", "key", "identifier"):
        if row.get(key) is not None:
            return str(row[key])
    return f"row-{index + 1}"


def _text(value: Any) -> str:
    return normalize_string(value, case="lower") or ""


def _score(left: Any, right: Any) -> tuple[float, list[IdentityEvidence]]:
    left_text = _text(left)
    right_text = _text(right)
    if not left_text or not right_text:
        return 0.0, []
    left_norm = normalize_identifier(left_text)
    right_norm = normalize_identifier(right_text)
    exact = left_norm == right_norm
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = set(re.findall(r"[\w]+", left_norm))
    right_tokens = set(re.findall(r"[\w]+", right_norm))
    token_score = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    evidence: list[IdentityEvidence] = [
        IdentityEvidence("normalized_exact", left, right, 1.0 if exact else 0.0),
        IdentityEvidence("edit_similarity", left, right, ratio),
        IdentityEvidence("token_similarity", left, right, token_score),
    ]
    return max(ratio, token_score), evidence


def generate_candidates(
    left_rows: Iterable[Mapping[str, Any]],
    right_rows: Iterable[Mapping[str, Any]],
    *,
    object_type: str = "object",
    left_id_field: str | None = None,
    right_id_field: str | None = None,
    compare_fields: Sequence[str] | None = None,
    threshold: float = 0.55,
    max_candidates: int | None = None,
) -> list[IdentityCandidate]:
    """Generate deterministic candidates and evidence, never semantic merges."""

    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between 0 and 1")
    left = [dict(row) for row in left_rows]
    right = [dict(row) for row in right_rows]
    output: list[IdentityCandidate] = []
    for li, lrow in enumerate(left):
        left_id = _id(lrow, li, left_id_field)
        for ri, rrow in enumerate(right):
            right_id = _id(rrow, ri, right_id_field)
            fields = tuple(compare_fields or sorted(set(lrow) & set(rrow)))
            if not fields:
                continue
            evidence: list[IdentityEvidence] = []
            contradictions: list[IdentityEvidence] = []
            scores: list[float] = []
            for field in fields:
                lv, rv = lrow.get(field), rrow.get(field)
                if lv is None or rv is None or (isinstance(lv, str) and not lv.strip()) or (isinstance(rv, str) and not rv.strip()):
                    continue
                score, field_evidence = _score(lv, rv)
                scores.append(score)
                evidence.extend(replace(ev, details={"field": field}) for ev in field_evidence)
                # A clearly different exact comparable value is a contradiction;
                # a low string score alone is not sufficient to reject anything.
                if isinstance(lv, (int, float)) and isinstance(rv, (int, float)):
                    if lv != rv:
                        contradictions.append(IdentityEvidence("contradictory_attribute", lv, rv, 1.0, details={"field": field}))
                elif _text(lv) and _text(rv) and score < 0.25:
                    contradictions.append(IdentityEvidence("contradictory_attribute", lv, rv, 1.0, details={"field": field}))
            if not scores:
                continue
            similarity = max(scores)
            exact = any(ev.kind == "normalized_exact" and ev.strength == 1.0 for ev in evidence)
            if not exact and similarity < threshold:
                continue
            if exact:
                evidence.append(IdentityEvidence("shared_attribute", left_id, right_id, 1.0))
            candidate_id = hashlib.sha256(f"{object_type}\0{left_id}\0{right_id}".encode()).hexdigest()[:20]
            output.append(IdentityCandidate(
                candidate_id=candidate_id,
                object_type=object_type,
                left_id=left_id,
                right_id=right_id,
                evidence=tuple(evidence),
                contradictions=tuple(contradictions),
                similarity=similarity,
                coverage=len(scores) / len(fields),
                limitations=("Similarity and evidence do not prove same_object; reviewed semantic decision required.",),
            ))
            if max_candidates is not None and len(output) >= max_candidates:
                return output
    return output


def apply_decision(
    candidate: IdentityCandidate,
    decision: IdentityDecision,
    *,
    canonical_id: str | None = None,
    rows: Iterable[Mapping[str, Any]] | None = None,
    id_field: str | None = None,
) -> CanonicalMapping | dict[str, Any]:
    """Apply an explicit reviewed decision to a derived mapping/view.

    Raw source rows are copied and augmented; no input mapping or source value
    is modified.  Non-merging decisions remain represented in the returned
    mapping metadata and do not fabricate a canonical object.
    """

    if decision.candidate_id != candidate.candidate_id:
        raise ValueError("identity decision does not refer to candidate")
    merge_decision = decision.decision in {"same_object", "alternate_representation"}
    reviewed = decision.review_status in {"reviewed", "accepted"} and bool(str(decision.reviewer_ref or "").strip())
    if merge_decision and not reviewed:
        raise ValueError("a reviewed reviewer_ref and review_status are required for a merge-producing identity decision")
    selected = canonical_id or decision.canonical_id
    reviewed_trace = {
        "decision_id": decision.decision_id,
        "decision_hash": decision.decision_hash,
        "candidate_id": decision.candidate_id,
        "decision": decision.decision,
        "review_status": decision.review_status,
        "reviewer_ref": decision.reviewer_ref,
        "evidence_refs": list(decision.evidence_refs),
        "rationale": decision.rationale,
        "scope": decision.scope,
        "limitations": list(decision.limitations),
    }
    if merge_decision:
        selected = selected or "obj-" + hashlib.sha256(f"{candidate.object_type}\0{candidate.left_id}\0{candidate.right_id}".encode()).hexdigest()[:16]
        mapping = CanonicalMapping(
            canonical_id=selected,
            object_type=candidate.object_type,
            source_identities=(candidate.left_id, candidate.right_id),
            decision_id=decision.decision_id,
            aliases=(candidate.left_id, candidate.right_id),
            limitations=("Derived from reviewed decision; raw identities remain preserved.", *decision.limitations),
            scope=decision.scope,
            metadata={"reviewed_trace": reviewed_trace},
        )
    else:
        mapping = CanonicalMapping(
            canonical_id=selected or f"unresolved:{candidate.candidate_id}",
            object_type=candidate.object_type,
            source_identities=(candidate.left_id, candidate.right_id),
            decision_id=decision.decision_id,
            status=decision.decision,
            aliases=(candidate.left_id, candidate.right_id),
            limitations=("No canonical merge was applied.", *decision.limitations),
            scope=decision.scope,
            metadata={"reviewed_trace": reviewed_trace},
        )
    if rows is None:
        return mapping
    derived: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        identity = str(copied.get(id_field or "id", copied.get("_id", "")))
        if mapping.status == "accepted" and identity in mapping.source_identities:
            copied["canonical_id"] = mapping.canonical_id
        derived.append(copied)
    return {"mapping": mapping, "rows": derived, "raw_preserved": True}


def mapping_coverage(mappings: Iterable[CanonicalMapping], source_ids: Iterable[str]) -> dict[str, Any]:
    ids = tuple(str(v) for v in source_ids)
    covered = {sid for mapping in mappings if mapping.status == "accepted" for sid in mapping.source_identities}
    matched = [sid for sid in ids if sid in covered]
    return {
        "source_count": len(ids),
        "mapped_count": len(matched),
        "unmapped_count": len(ids) - len(matched),
        "coverage": len(matched) / len(ids) if ids else 1.0,
        "unmapped": [sid for sid in ids if sid not in covered],
    }


candidates = generate_candidates
apply = apply_decision

__all__ = ["apply", "apply_decision", "candidates", "generate_candidates", "mapping_coverage"]
