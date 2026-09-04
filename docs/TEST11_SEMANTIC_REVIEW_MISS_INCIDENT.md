# Test11 deferred incident: semantic errors missed by Business Review

**Status:** `recorded/deferred`
**Run:** `RUN-5696ede5c4fc4e13`
**Scope:** Future investigation and detection example; this note does not modify the run or its artifacts.

## What happened

In Test11, the Analytical Owner (AO) made material semantic mistakes in both
`REQ-001` and `REQ-002`. Each item was nevertheless accepted with
`accepted_with_limits`. The independent Business Reviewer (BR) did not detect
these defects; the later Integration review surfaced them.

- **REQ-001 — date authority:** The reusable
  `req-001-erp-orders-to-billing-documents` relationship records `as_of` as
  `2024-06-30`. The accepted answer and billing evidence state that available
  billing events extend only through `2024-06-05`. Integration therefore found
  an authority/window contradiction, not merely a presentation issue.
- **REQ-002 — relationship grain:** The header-to-line relationship is typed
  as one-to-many, but its tested counts use `550` endpoints rather than the
  accepted ERP line population of `1,645`. The ERP-to-WMS relationship uses
  `1,501` aggregate order-product keys while the accepted valid WMS movement
  population is `1,502`. These are incompatible endpoint grains and coverage
  claims, not harmless rounding differences.

## Why it matters

Incorrect date authority and relationship grain can become reusable semantics,
metrics, or dashboard facts. `accepted_with_limits` did not prevent either
error from crossing the Business Review boundary. In this run, Integration was
the first effective detector for these two semantic defects, after the AO and
BR stages had already completed.

This is evidence of a detection gap in this case, not a conclusion that every
Business Review will miss the same class of issue. The case should remain
available for designing an independent, adversarial semantic-review check.

## Detection gap

The persisted BR findings for `REQ-001` and `REQ-002` cover other answer,
source-completeness, and evidence-fidelity issues, but do not identify the
later Integration findings for billing `as_of` or relationship endpoint grain.
The current workflow therefore lacks demonstrated independent coverage for:

1. checking relationship `as_of`/date authority against the actual observed
   evidence window; and
2. checking that cardinality, matched endpoints, populations, and coverage use
   one declared row or aggregate grain on both sides of a relationship.

The appropriate future work is to investigate the review contract, prompts,
evidence bindings, and adversarial fixtures. Do not encode a Test11-specific
hardcoded rule as the remedy.

## Evidence pointers

All paths below are relative to the run directory
`RUN-5696ede5c4fc4e13`:

- `requirements/REQ-001/accepted/answer_content.json` — accepted billing
  statement and the `2024-06-05` observation limit.
- `requirements/REQ-001/work/analytical_relationships.jsonl` — relationship
  `req-001-erp-orders-to-billing-documents` with `as_of: 2024-06-30`.
- `requirements/REQ-001/work/business_review.json` — persisted BR findings,
  which omit the later date-authority defect.
- `requirements/REQ-001/integration/review/result.json` — finding
  `req001-billing-relationship-as-of`.
- `requirements/REQ-002/accepted/answer_content.json` — accepted ERP/WMS
  populations and the row-level reconciliation context.
- `requirements/REQ-002/work/analytical_relationships.jsonl` —
  `REQ002-REL-ERP-HEADER-LINE` and `REQ002-REL-ERP-WMS-RECEIPT` relationship
  metadata.
- `requirements/REQ-002/work/business_review.json` — persisted BR findings,
  which omit the later relationship-grain defects.
- `requirements/REQ-002/integration/review/result.json` — findings
  `REQ002-FIDELITY-REL-HEADER-LINE-GRAIN` and
  `REQ002-FIDELITY-REL-WMS-ENDPOINT-GRAIN`.

## Deferred questions

- What independent BR check should catch a relationship date authority that
  extends beyond the evidence actually observed?
- What contract should require explicit endpoint-grain declarations and
  reconcile `matched_pairs`, populations, and coverage to that grain?
- Should acceptance require an adversarial cross-check against accepted
  ontology/prepared populations before a relationship becomes reusable?
- How can this class be tested with general fixtures and signals rather than
  hardcoding Test11 identifiers or expected answers?
- Where should a future detection result live when Integration finds a semantic
  defect that BR missed: review provenance, an acceptance block, or a
  repair/rethink transition?

## Non-goal and current disposition

This incident is **not** a semantic-review fix. The current Test11 fix scope is
only the separate **business-repair ordering contradiction**. No code, tests,
run artifacts, or publication state are changed by recording this case.

The case remains `recorded/deferred` until a future investigation defines and
validates a general detection approach.
