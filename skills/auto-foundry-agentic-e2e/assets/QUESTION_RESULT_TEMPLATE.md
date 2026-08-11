# Active item result

## Item identity

- Mode: `question` | `requirement`
- Item ID:
- Original user text:
- Priority/source (Requirement Mode):
- Scope classification (Requirement Mode): `analytics_in_scope` |
  `analytics_requires_missing_data` | `out_of_analytics_scope`
- Authoritative state: `item_state.json`
- Durable workspace: `questions/<id>/work` |
  `requirements/<id>/work`

## Workbench and progress

- Data-room source catalog:
- Raw archive read-only: `true` | `false`
- Source/member metadata and bounded reads:
- Run-level physical inventory counters (initial full bind, child loads without
  re-inventory, selected-member verification, final explicit verification):
- Opaque-member materialization (safe explicit copy only; no semantic parser):
- Compact source/LEM/prepared IDs selected directly by Lead Analyst:
- Plan/source map materialized before analysis:
- Artifact progress before/after/counts:
- No-progress decisions: `await_runtime` | `materialization_guidance`
- Execution recovery is separate from no-progress decisions and requires a
  canonical persisted execution-loss receipt; it is not a no-progress decision:
- Execution recovery count/routes and scratch preservation:
- Recovery receipt reference/hash (canonical persisted ref; attempt/lane match):

## User-owned request

- Objective and decision context:
- Expected analytical outputs:
- Expected visual outputs:
- Internal tasks/dependencies:
- Shared foundation dependencies:
- Data/ontology/prepared-data needs:

## Analysis

- Concise plan:
- Direct answer:
- Headline findings:
- Scope, period, units, and population:
- Working definitions/proxies:
- Method and reproduction references:
- Supported components:
- Unsupported components:
- Limitations and next evidence:
- Identity candidates/evidence/semantic decision (when applicable):
- Source-completeness search target/result for material absence claims:

## Review and outcome

- Lead Analyst self-check:
- Independent Business Reviewer route:
- `review_status`:
- `review_strength`:
- Reviewer verdict:
- Identity route checked: `true` | `false` | `not_applicable`
- Source completeness checked: `true` | `false` | `not_applicable`
- If no reviewer was invoked, use `review_status: unavailable`,
  `review_strength: none`, `verdict: not_reviewed`; the item may still be
  `answered_with_limits` when the Lead Analyst result is useful.
- Execution recovery count (not a business repair):
- Terminal reason classifier output: `same_attempt_feedback` |
  `business_repair` | `execution_recovery` | `abort_and_new_clean_run` |
  `null`
- Raw terminal reason (specific fact, for example `syntax_error` or
  `core_defect`):
- Provider/model/host/process identity (literal `unavailable` when unknown):
- Business repair used: `none` | `one_scoped_repair`
- Business findings (all returned together): finding IDs, exact JSON-pointer/
  artifact paths, dependent outputs, reviewed draft hash, and targeted recheck
  scope:
- Reviewer-scope packet recovery (only when the packet is inadmissible):
  `ItemWorkspace.discard_business_review(...)`, incident ID/category/source,
  discarded packet hash, draft hash, append-only audit path/hash, preserved
  work/draft confirmation, pending-review reset, zero business-repair reset,
  and the required new full-review reference. Never reinterpret findings or
  use a compatibility fallback.
- Final outcome:
- Accepted answer bytes (immutable ref/hash):
- Acceptance envelope (program-owned ref/hash/lifecycle):

## Knowledge and telemetry

- Knowledge Delta: `promoted` | `promoted_with_limits` | `no_change`
- Concrete `no_change` reason (when applicable):
- Atomic application receipt (program-owned):
- Prepared candidates (item `work/prepared/` hash/location/schema/grain/lineage;
  not registry state before accepted integration):
- Telemetry event references:

## Result integration

- Result Integration Agent: exactly one post-acceptance owner
- Incremental API facts: claims, metrics, limitations, evidence, prepared
  assets, ontology, relationships, dashboard facts
- Semantic mappings:
- Deterministic validation: types, paths, refs, hashes, stages, commits
- Integration state/receipt:
- Accepted registry publication (exactly once at accepted commit; scope retained;
  rejected/technical-failure leaves no entry):
- Integration Fidelity Reviewer: exactly one fresh item-only reviewer after
  mechanical validation and before commit; packet excludes siblings,
  cumulative state, prior memory, and broad workspace context:
- Integration fidelity findings, affected record/dependency paths, preserved
  hashes, same-agent targeted repair and one targeted recheck:
- Mechanical validation limitation and reviewer disposition:
