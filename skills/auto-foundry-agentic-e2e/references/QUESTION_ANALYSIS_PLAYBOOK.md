# Question and requirement analysis playbook

This playbook supports natural, bounded analysis. It is not a mandatory stage
pipeline and does not create acceptance gates.

## 1. Register the active item

In Question Mode, retain the exact user wording and queue position. In
Requirement Mode, retain the full user-owned record:

```text
requirement_id
original_text
priority and priority_source
objective and decision context
expected analytical outputs
expected visual outputs
internal tasks and dependencies
shared foundation dependencies
data, ontology, and prepared-data needs
working definitions and limits
status and outcome
```

The Portfolio Planner sees the full Requirement Mode portfolio, classifies each
item semantically as `analytics_in_scope`,
`analytics_requires_missing_data`, or `out_of_analytics_scope`, and records its
rationale. Do not use a keyword dictionary or keyword router. Honor explicit
priority; order unprioritized items only when dependency or safe reuse evidence
supports the ordering. Replan briefly between items and execute one item at a
time. Trace shared foundation work separately from user requirements.

## 2. Navigator bundle

Read compact ontology and prepared-data indexes before reading full records.
Select only the bounded exact IDs relevant to the active item. Validate each
ID deterministically for existence, layer/type, current-run ownership,
allowed-root scope, effective period, and evidence references. A failed
validation is recorded and does not justify an unbounded read.

## 3. Interpret the decision

Identify only what is material:

- decision or business use;
- requested measures, dimensions, entities, grain, period, and as-of date;
- expected visual output;
- descriptive versus causal language;
- cross-source attribution;
- policy, contract, or process dependence;
- evidence needed to support or limit each claim.

Use source-local definitions or working proxies when clearly labelled. When two
reasonable meanings remain, use scenarios rather than silently choosing one.

## 4. Choose a minimal answer strategy

Choose one or more routes:

- direct source-local measurement;
- prepared-data reuse;
- source-local proxy;
- alternative-definition scenario;
- descriptive association;
- policy or process scenario;
- partial answer with an evidence blocker;
- `analytics_requires_missing_data` response;
- `out_of_analytics_scope` response.

Do not dispatch a specialist or create an artifact merely because a capability
exists. Stop when the evidence is sufficient for a bounded answer.

## 5. Inspect the Capability Catalog

Record a catalog inspection before using deterministic core operations. Use a
catalog capability only when it fits the evidence and question. Generic forms
are:

```bash
python -m auto_foundry_core catalog ...
python -m auto_foundry_core run ...
```

The exact operation ID comes from the current catalog. If no capability fits,
record a capability gap and use preserved custom code when useful. Custom code
must retain inputs, outputs, assumptions, and reproduction details.

## 6. Semantics and relationships

For every material field, record observed name, working meaning, grain,
evidence, effective period, and limitation. For every material link, measure:

- key overlap and coverage;
- left/right uniqueness and multiplicity;
- fanout and duplicates;
- unmatched records;
- date/period alignment;
- transformations applied to keys;
- conflicts or contradictory source claims.

Use a relationship for quantitative claims only at an evidence level supported
for that item. State coverage and exclusions. Missing identity may block a
combined ranking while leaving source-local counts usable.

## 7. Documents, rules, processes, and quality

For a document-dependent result, separate what the document says, whether it is
applicable, its effective period and precedence, operational evidence, a
possible scenario, and any unsupported compliance claim.

For a process result, define case, events, timestamps/timezone, ordering,
repeated events, incomplete cases, and exclusions. For quality, investigate
only risks that can change the active answer: missing denominators, duplicate
keys, invalid dates, impossible sequences, unit/currency mismatch, unstable
joins, coverage gaps, or stale periods.

## 8. Cleaning and population

Use the least invasive transformation: normalization, explicit mapping,
evidence-supported correction, exclusion with a count, or quarantine. Preserve
raw values and write derived assets to the Prepared Data Registry.

Record base population, eligible population, exclusions by reason, unresolved
records, denominator, grain, period, dimensions, units, and coverage.

## 9. Analysis and answer

Suitable outputs include counts, shares, trends, distributions, rankings,
concentrations, cycle times, transitions, scenario ranges, descriptive
associations, and null findings. Use causal language only when the evidence
supports it.

The draft and final answer should separate:

1. direct business answer;
2. headline findings and expected visual outputs;
3. scope, period, population, denominator, and units;
4. definitions, proxies, and method;
5. supported components;
6. unsupported components and missing evidence;
7. limitations and safe next actions.

## 10. Review and Knowledge Delta

One Lead Analyst self-check precedes one routed Independent Reviewer. Route an
independent fresh context first, an alternate independent route second, and a
fresh same-family route third. If all are unavailable, disclose
`review_status=unavailable` and `review_strength=none` and continue.

The reviewer may accept, accept with limits, request one targeted repair, or
block specific claims. Apply at most one repair and recheck only the repaired
points. Then apply one reviewed Knowledge Delta atomically by code to the
appropriate LEM layer, or record `no_change`.

## 11. Queue continuation

Terminal outcomes include `answered`, `answered_with_limits`, `partial_answer`,
`null_finding`, `blocked_by_evidence`, `unsupported`,
`analytics_requires_missing_data`, `out_of_analytics_scope`, and
`technical_failure`. Preserve supported work and continue to the next item.
Only a global infrastructure failure may prevent remaining items.
