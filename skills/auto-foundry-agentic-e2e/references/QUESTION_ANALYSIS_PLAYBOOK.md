# Question and requirement analysis playbook

This playbook supports natural, bounded analysis on top of the program-owned
Agent Workbench. It is not a mandatory stage pipeline and does not create
acceptance gates.

## 1. Register the active item and workspace

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

The program first opens the shared `DataRoomWorkbench`, then creates
`questions/<id>/work` or `requirements/<id>/work`, authoritative
`item_state.json`, and an immutable `BoundAnalysisContext` before invoking the
Lead Analyst. Requirement Mode honors explicit user priority first;
unprioritized items may be ordered one at a time for observed dependencies or
safe reuse. There is no Portfolio Planner, separate planner framework,
keyword router, or business-term dictionary.

## 2. Data room and compact indexes

Build one physical source catalog from ZIP/archive and member metadata. Keep
raw archives read-only. Catalog entries contain bounded columns, samples or
values, hashes, and workbook sheet information where available. Read the
compact source, ontology, and prepared indexes first; then select the smallest
set of exact IDs relevant to the active item. The program validates each ID for
existence, current-run ownership, expected layer/type, allowed scope, effective
period, hash/location, schema, grain, lineage, and evidence references before
the Lead Analyst uses it. A failed validation is recorded and does not justify
an unbounded read or guessed ID.

There is no Navigator, descriptor/typed-validation role, or per-item Capability
Catalog lookup/compliance artifact. The Lead Analyst selects IDs directly.
Catalog capabilities may be recommended or used internally when they fit;
custom code is allowed when it is the clearest reproducible route.

## 3. Plan/source map and artifact progress

The Lead Analyst writes a concise `plan`, reproducible script, and `source_map`
into `work/` before analysis, then appends material findings, evidence
references, and any loadable prepared assets. `ControlledScriptRunner` performs
compile/dependency preflight checks; successful pipelines emit `smoke` and
`full` runtime receipts, with an optional second `full` receipt for a
deterministic comparison, while a failed preflight emits only its `compile` or
`dependency_check` receipt. Coding errors (`SyntaxError`, `NameError`, `TypeError`, or script
validation) return to the same analyst and attempt and never authorize
execution recovery. `draft` is
written only when its content is materialized; accepted answer bytes are
immutable and separate from the program-owned acceptance envelope. Work
remains mutable scratch.

After each response, the Run Director checks structured `artifact_progress`,
not prose activity:

1. progress in material artifacts or counts → continue;
2. filesystem no-progress → return `await_runtime` or
   `materialization_guidance`, preserving the handoff;
3. a completed invocation receipt explicitly proving lane/provider/host/process
   loss → authorize execution recovery from the durable handoff.

Execution recovery preserves scratch and is counted separately from the one
business repair. It requires a canonical persisted invocation `receipt_ref` and
hash proving lane/provider/host/process loss and matching the active attempt
and lane; unpersisted or mismatched references fail closed. Provider/model
identity may be literal `unavailable`; do not invent identity. The host creates
or restarts the replacement thread; the core only records state. No wall-time
deadline applies to the workflow; the runner's explicit default 3600-second
process guard is not an agent reasoning deadline, and no terminalizer/manual finalizer is
used. After recovery routes are exhausted, the program writes typed
`technical_failure`, with classifier output restricted to
`same_attempt_feedback`, `business_repair`, `execution_recovery`,
`abort_and_new_clean_run`, or `null`. The raw `terminal_reason` remains a
specific fact such as `syntax_error` or `core_defect`; this is never a data
conclusion.

## 4. Interpret the decision

Identify only what is material:

- decision or business use;
- requested measures, dimensions, entities, grain, period, and as-of date;
- expected visual output;
- descriptive versus causal language;
- cross-source attribution;
- policy, contract, or process dependence;
- evidence needed to support or limit each claim.

Use source-local definitions or working proxies when clearly labelled. When
two reasonable meanings remain, use scenarios rather than silently choosing
one.

## 5. Choose a minimal answer strategy

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

## 6. Deterministic operations and custom code

`CoreRuntime` remains available through the run's `RunContext` for bounded
deterministic operations. The exact operation ID comes from the installed
core; illustrative forms are:

```bash
python -m auto_foundry_core catalog ...
python -m auto_foundry_core run ...
```

Record a capability gap when no operation fits. Custom Python, SQL, shell,
notebook, spreadsheet, or chart code may be used when it preserves inputs,
outputs, assumptions, limits, and a reproduction command in the current run.

## 7. Semantics and relationships

For every material field, record observed name, working meaning, grain,
evidence, effective period, and limitation. For every material link, measure:

- key overlap and coverage;
- left/right uniqueness and multiplicity;
- fanout and duplicates;
- unmatched records;
- date/period alignment;
- transformations applied to keys;
- conflicts or contradictory source claims.

When exact overlap is absent but same-object representations are materially
plausible, run the identity-escalation route: enumerate candidates, collect
independent evidence and coverage, make a semantic identity decision, and let
the reviewer check it. If the route is inapplicable, explain why. Only then
declare a combined relationship unavailable; source-local results remain
usable.

## 8. Documents, rules, processes, and quality

For a document-dependent result, separate what the document says, whether it
is applicable, its effective period and precedence, operational evidence, a
possible scenario, and any unsupported compliance claim.

For a process result, define case, events, timestamps/timezone, ordering,
repeated events, incomplete cases, and exclusions. For quality, investigate
only risks that can change the active answer: missing denominators, duplicate
keys, invalid dates, impossible sequences, unit/currency mismatch, unstable
joins, coverage gaps, or stale periods.

## 9. Cleaning, population, and prepared assets

Use the least invasive transformation: normalization, explicit mapping,
evidence-supported correction, exclusion with a count, or quarantine. Preserve
raw values and write derived assets as item-local candidates below
`work/prepared/` through `BoundAnalysisContext.save_prepared_candidate(...)`.
Candidates carry a hash, location, byte/row counts, schema, grain,
lineage/source IDs, scope, effective period, transformations, evidence, and
limits, but do not enter the accepted Prepared Data Registry until one accepted
Result Integration commit validates and registers them. Exact retries are
idempotent; rejected or technical-failure items leave no accepted entry.

Record base population, eligible population, exclusions by reason, unresolved
records, denominator, grain, period, dimensions, units, and coverage.

## 10. Analysis and answer

Suitable outputs include counts, shares, trends, distributions, rankings,
concentrations, cycle times, transitions, scenario ranges, descriptive
associations, and null findings. Use causal language only when the evidence
supports it.

The draft and accepted answer should separate:

1. direct business answer;
2. headline findings and expected visual outputs;
3. scope, period, population, denominator, and units;
4. definitions, proxies, and method;
5. supported components;
6. unsupported components and missing evidence;
7. limitations and safe next actions.

## 11. Review and Knowledge Delta

One Lead Analyst self-check precedes one routed Independent Business Reviewer.
Route an independent fresh context first, an alternate independent route
second, and a fresh same-family route third. If all are unavailable, disclose
`review_status=unavailable` and `review_strength=none` and continue.

The reviewer checks the materialized draft, exact-ID evidence, definitions,
joins, populations, denominators, and reproducibility. For every material
absence claim, it performs a targeted completeness search through the physical
source catalog. It also checks the identity-escalation route when a combined
relationship is claimed or withheld. These are focused checks, not a repeated
full analysis or a new review layer.

The reviewer may accept, accept with limits, request one targeted repair, or
block specific claims. Apply at most one business repair and recheck only the
repaired points. Then the program validates and atomically applies one
reviewed Knowledge Delta (`promoted`, `promoted_with_limits`, or `no_change`);
`no_change` includes a concrete reason. Custom question code does not apply the
delta.

After acceptance, exactly one Result Integration Agent incrementally consumes
claims, metrics, limitations, evidence refs, prepared assets, ontology,
relationships, and dashboard facts through small program APIs. It performs
semantic mapping; deterministic code validates types, paths, refs, hashes,
stages, and commits. Every accepted prepared asset is registered in a
canonical catalog immutable by source hash/core/schema; scope/reuse controls
visibility only and sample/category views are derived. Mechanical validation
cannot prove semantic completeness. Exactly one fresh item-only Integration
Fidelity Reviewer checks the current item after mechanical validation and before
commit; the same Result Integration Agent may make one targeted repair and
receives one targeted recheck. The packet excludes siblings, cumulative state,
prior memory, and broad workspace context. There is no prose parser, semantic
compiler, giant mandatory JSON, or reviewer chain.

## 12. Queue continuation

Terminal outcomes include `answered`, `answered_with_limits`, `partial_answer`,
`null_finding`, `blocked_by_evidence`, `unsupported`,
`analytics_requires_missing_data`, `out_of_analytics_scope`, and
`technical_failure`. Preserve supported work and continue to the next item.
Only a global infrastructure failure may prevent remaining items. Products and
optimizer evidence are built only after accepted snapshots and the whole-run
freeze.
