# Active item result

This is a **program-populated derived view**, not an agent response schema.
Analytical roles provide ordinary business conclusions through
`AnalystWorkspace`; the program fills internal references, hashes, receipts,
review scope, lifecycle state, and integration metadata.

## User-owned request

- Mode: `question` | `requirement`
- Item ID:
- Original user text:
- Priority/source (Requirement Mode):
- Scope classification: `analytics_in_scope` |
  `analytics_requires_missing_data` | `out_of_analytics_scope`
- Objective and decision context:
- Expected analytical outputs:
- Expected visual outputs:
- Requirement plan tasks and dependencies:
- Data needs:
- Ontology needs:
- Prepared-data needs:
- Working definitions:
- Limits:
- Status:

## Analytical Owner answer

- Direct answer:
- Headline findings:
- Scope, period, population, denominator, grain, units, and as-of date:
- Method and reproducibility summary:
- Supported components:
- Unsupported components:
- Proxies, scenarios, associations, or causal status:
- Limitations:
- Safe next actions:
- Visual specifications:
- Evidence summary:

## Specialist collaboration

- Specialist count: `0` | `1` | `2` | `3`
- Bounded task summaries:
- Memo conclusions, methods, evidence, limitations, open questions, confidence:
- Analytical Owner synthesis and any rejected specialist inference:

## Business review

- Reviewer route: Independent Business Reviewer
- Review status/strength:
- Verdict: `accept` | `accept_with_limits` | `repair_once` |
  `confirm_data_insufficiency` | `not_reviewed`
- Findings: finding ID, target answer section, one or more business categories, problem,
  evidence, and required change
- Source completeness checked: `true` | `false` | `not_applicable`
- Identity/policy route checked: `true` | `false` | `not_applicable`
- Business repair iterations completed: `<nonnegative integer>`
- Targeted recheck result:

The reviewer does not provide JSON pointers, artifact paths, dependent paths,
hashes, or packet fields. The program maps semantic finding scope through
`BusinessReviewAdapter` and populates the internal audit projection separately.
The host/router records the current `owner_ref` in program-owned audit state.
Analytical agents never emit it; a replacement owner may continue any
nonterminal item and the audit binding is updated.
Only the Analytical Owner may provide a `DataInsufficiencyConclusion` with an
unanswerable component, missing information, searches/tests performed,
evidence references, and supported components. The reviewer can only confirm
that conclusion; `blocked_by_evidence` is valid only after that confirmation.

## Program-owned workbench projection

- Data-room catalog and selected source IDs:
- Raw archive read-only:
- Plan, source selection, evidence, calculations, and prepared candidates:
- Controlled execution receipts and deterministic comparison:
- Artifact progress:
- No-progress decisions: `materialize_now` | `retry_same_attempt`
- Execution recovery is separate from no-progress decisions and requires a
  canonical persisted execution-loss receipt; it is not a no-progress decision:
- Accepted answer bytes and acceptance envelope:
- Passive telemetry references:

- Data insufficiency conclusion (owner-originated only):
- Reviewer confirmation: `confirm_data_insufficiency` | not applicable

Do not ask the Analytical Owner, specialist, or Business Reviewer to populate
this section. These are deterministic derived facts.

## Result integration

- Result Integration Agent: one post-acceptance owner
- Typed records: claims, metrics, limitations, evidence, prepared assets,
  ontology definitions, relationships, dashboard facts
- Mechanical validation:
- Item-only Integration Fidelity Reviewer verdict:
- Affected records/dependencies and one targeted repair/recheck:
- Integration commit and accepted registry publication:

Only the Result Integration Agent works with typed integration records. It does
not rewrite the accepted answer.

## Outcome and products

- Final analytical outcome:
- Knowledge Delta and concrete reason:
- Dashboard sections and reviewed evidence links:
- Visible periods, populations, denominators, units, proxies, and limitations:
- Product freeze and terminal report status:
