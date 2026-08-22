# Question and requirement analysis playbook

This playbook describes the work of the Analytical Owner. It is a reasoning
guide, not a mandatory stage machine. The owner may move backward, test a
different interpretation, or narrow the answer as evidence changes.

## 1. Understand the decision

Start from the exact user wording. Determine:

- the decision or business use;
- requested measures, dimensions, entities, grain, period, and as-of date;
- whether the language is descriptive, predictive, comparative, or causal;
- which components would still be useful if the whole request cannot be
  supported;
- what evidence would make each important claim defensible.

In Requirement Mode preserve each exact `RequirementRecord`, including priority,
expected analytical and visual outputs, `RequirementRecord.dependencies`, data
needs, working definitions, limits, and status. One run-level event-driven
Planner receives the exact records, compact physical catalog, and current
item/resolution outcomes. Initial order and grouping are advisory; it preserves
explicit user priority/order but never declares runtime semantic dependencies.
The Analytical Owner discovers those after understanding the requirement, and
the runtime `waiting_on_resolution`/`ready_to_resume` ledger is the sole
semantic block. It may suggest zero to three owner specialists,
but does not calculate or write answers and is not a deterministic ID/hash or
lifecycle authority. `RequirementExecutionPlan` and
`RequirementExecutionGroup` are revisionable recommendations/current
scheduling, not catalog or lifecycle authority. A technical failure does not
create Planner dependency blocks; independent groups remain eligible and runtime
resolution state controls waiting and resume.

Bind every item directly to the same `RunContext` and shared Data Room. Each
item creates an item-local `RequirementAnalysisPlan` with 1..N internal
`RequirementAnalysisTask` values, then follows the ordinary loop: analysis,
review, iterative material repairs, accept or `technical_failure`, and
integration. Within a group keep one Analytical Owner per requirement;
bounded shared investigation may be reused and independent groups may run when
host capacity permits. For the exact requirement “Dashboard should show the
ratio of milk fat content to the procurement price of the raw material for that
milk.”, a valid decomposition defines the milk lot/fat measure and maps
raw-material procurement cost, then calculates/reconciles the ratio and
visualization. These are semantic tasks only: no child lifecycle workspaces or
additional planners. The host records the current owner in program audit state;
agents never emit internal paths/hashes. A replacement owner may continue any
nonterminal item.
Classify the request as `analytics_in_scope`,
`analytics_requires_missing_data`, or `out_of_analytics_scope` without a
keyword router.

The default Requirement Mode capacity is four entity-resolution workers, one
Analytical Owner, up to three owner specialists, and eight active workers; the
Planner is not counted. Hosts may configure lower or higher limits but must not
oversubscribe actual host capacity. Requested Run A and Run B executions remain
sequential.

## 2. Form an answer strategy

Choose the smallest strategy that can answer the decision. Possible routes
include direct measurement, prepared-data reuse, a clearly labelled proxy,
alternative-definition scenarios, descriptive association, a policy/process
scenario, or a bounded partial answer.

Write down the working population, denominator, grain, period, units, join
hypotheses, source authority, and assumptions. Treat them as hypotheses until
verified. Do not force every item through the same analytical recipe.

## 3. Inspect the bounded data room

Use `AnalystWorkspace.brief()` for the question and quality criteria. This is a
readiness/scouting pass: explicitly search the compact accepted semantic graph,
identity mappings, relationships, and prepared descriptors with
`search_ontology()` and `search_prepared_assets()` before searching the source
catalog with `search_sources()`. `brief()` reports ontology, relationship, and
prepared-asset availability without loading rows. When prior understanding is
useful, select exact accepted IDs with `select_ontology()` and
`select_prepared_assets()`, including a purpose; the program records the
owner/item-bound decisions in `work/semantic_selections.jsonl`. If no relevant
descriptor applies, record why and establish new semantics rather than calling
reuse optional by default.

Load prepared rows only after exact selection through
`load_prepared_asset()`. Registry location, schema, lineage, row/byte counts,
and content hash are validated before rows are returned. If no descriptor
applies, continue with bounded source inspection and establish new semantics;
do not infer reuse from a similar label.
When present, preserve a descriptor's `effective_period` through the candidate
sidecar, operation manifest/hash inputs, accepted integration and registry,
and later reuse. Omission remains valid and means no period constraint.

Inspect selected sources through bounded `sample_source()` and
`source_categories()` calls before choosing members.

Use `begin_analysis()` to record the objective and strategy, then
`select_sources()` to bind exact selected sources. The program owns catalog
paths, hashes, and source-map serialization. The Analytical Owner reasons in
terms of source IDs, fields, meanings, and business purpose.

Check material source completeness. For absence claims, search plausible
names, synonyms, identifiers, documents, and alternate representations. Do
not infer source authority or identity merely from similar labels.

If a needed identity domain is currently `resolving`, report
`waiting_on_resolution` and release the owner lane; do not guess the join. The
Planner skips to the next original-order runnable item and marks the earliest
paused item `ready_to_resume` when the domain becomes `ready`. If all runnable items wait, the
owner lane sleeps while active resolution workers progress; block only when
nothing is runnable and no resolver progresses.

## 4. Test semantics and relationships

For each material field or relationship, establish:

- business meaning and source authority;
- data type, grain, units, and effective period;
- null, duplicate, conflict, and coverage behavior;
- join cardinality, fanout, unmatched population, and alternate controls;
- whether an identity is exact, transformed, probabilistic, or unsupported;
- whether a rule or policy applies to the requested population and dates.

Escalate identity deliberately: test exact keys first, then documented aliases
or reversible transformations, then composite evidence. Reject a candidate if
the same evidence also supports incompatible matches.

When an Analytical Owner proposes a new arbitrary real-world identity domain
during scouting, reserve that exact owner-bound proposal as `resolving` and
launch an Entity Resolution Owner; the Planner cannot pre-reserve it. Strongly coupled classes may share a domain; do not assume a
hardcoded Supplier/Factory/Order scope. The owner scans every row of relevant
tables and relevant documents selected from reservation hints, expanding only
for concrete matching/conflict evidence and reusing the run-level catalog. It
owns methodology, may inspect manually or write Python/SQL/scripts/use
helpers, infer and bulk-apply justified patterns, test samples/coverage/
exceptions/population differences, and revise the method. Do not require
row-by-row review, an authoritative crosswalk, or a fixed matching script.
Pattern rules remain run knowledge; future helper-library audit is deferred.

The review decision is binary per proposed mapping (accepted or not accepted).
Each accepted `CanonicalMapping` may contain one or many source identities or
representations, including bulk pattern-derived populations. Keep unresolved or
ambiguous records source-local/outside canonical mappings with coverage and
exceptions; accepted mappings are not downgraded. A ready snapshot publishes
the canonical class, source-account representation classes, reviewed
`IdentityDecision`/`CanonicalMapping`, identity `represents` relationships, and
a versioned mapping asset/coverage where available. Owners see only resolving
or ready snapshots, never partial mappings.

Use `record_evidence()` to preserve conclusions, method, supporting references,
limitations, and compact facts. The owner writes the analytical meaning; the
program writes the evidence record.

For every actual business join or relationship used in the answer, record
`source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
unique tested edge-pair count), `source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (endpoint count divided by its
population, with zero for a zero population), plus `as_of`/date authority,
limitations, and evidence. Do not infer a relationship
from a theoretical graph or prose similarity; Integration may publish only
reviewed tested relationships established here.

## 5. Delegate only bounded uncertainties

Use zero to three specialists. Good specialist questions are separable and can
be answered without owning the parent answer, for example:

- “Measure join coverage and fanout for these two selected sources.”
- “Check whether this denominator matches the stated KPI definition.”
- “Determine whether this policy is authoritative for the requested period.”
- “Assess whether the proposed proxy is operationally meaningful.”

Record the task with `assign_specialist()` and the returned memo with
`record_specialist_memo()`. The Analytical Owner decides whether and how the
memo affects the answer. Specialists never edit the draft or lifecycle state.
Entity Resolution Owners are parallel external jobs, not extra specialists;
they own mapping methodology and never answer the parent requirement.

Read [ANALYTICAL_COLLABORATION.md](ANALYTICAL_COLLABORATION.md) for role prompts
and boundaries.

## 6. Calculate reproducibly

Use direct bounded inspection for small semantic checks. Use a reproducible
script when calculations, joins, filters, transformations, or tables are
material. Run it through `AnalystWorkspace.run_analysis()` so deterministic
program code performs compile and dependency preflight, smoke execution, full
execution, output validation, and optional deterministic comparison.

Coding errors such as `SyntaxError`, `NameError`, `TypeError`, missing imports,
or invalid output are ordinary execution feedback. The current or a replacement
owner and attempt may continue the verified work; there is no separate repair
budget.

Preserve:

- the exact population and exclusions;
- numerator and denominator for material rates;
- units and rounding;
- period and as-of date;
- join coverage and conflict counts;
- reconciliation checks;
- output tables needed by the answer and dashboard.

Never turn unavailable evidence into zero.

## 7. Interpret, challenge, and narrow

Compare results against alternate definitions and controls. Ask whether a
finding survives a reasonable population or period change. Separate:

- direct evidence;
- supported proxy;
- scenario or assumption;
- descriptive association;
- causal claim.

Preserve source-local or partial results when cross-source identity fails.
State which requested components are supported and which are not. A useful
bounded answer with explicit limits is better than a confident unsupported
answer or a total failure that discards valid work.

## 8. Submit one complete answer

Use `submit_answer()` with an `AnalystAnswer` or ordinary answer text. A
decision-ready answer normally includes:

- a direct answer;
- headline findings;
- scope, period, population, denominator, grain, and units;
- method and material assumptions;
- supported and unsupported components;
- limitations and safe next actions;
- useful visual specifications;
- evidence references.

The agent does not author the JSON representation. The facade validates strict
JSON values and the program serializes the draft. `NaN`, `Infinity`, invalid
types, duplicate identifiers, and unknown enum values fail before review or
acceptance.

## 9. Business review and iterative repair

One fresh Independent Business Reviewer checks the answer and supporting work.
The reviewer returns business-shaped findings by answer section and one or more categories,
not JSON pointers or artifact paths. The program records each finding as
semantic repair provenance and authorizes only item-local changes.

If the verdict is `repair_once`, keep the finding as semantic repair provenance
and return it to the same Analytical Owner. Once authorized, that owner may
update any item-local work and the affected answer section needed to resolve
the finding; cross-item writes are rejected. The targeted recheck examines the
repair and preserved unaffected work. Repeat this loop whenever another
material, evidence-backed correction is useful; there is no fixed repair
count. If the answer is accepted, the program preserves the
exact reviewed bytes and acceptance envelope atomically.

Only the Analytical Owner may originate a `DataInsufficiencyConclusion` naming
an unanswerable component, missing information, searches/tests performed,
evidence references, and supported components. The reviewer may only return
`confirm_data_insufficiency` after checking that conclusion. The program may
then publish `blocked_by_evidence`; presentation, calculation, evidence,
method, reviewer, program, and script defects repair or technical-fail instead.

Use `accept` when the core requested decision is answered and only normal
disclosed limits remain. Use `accept_with_limits` only when a material requested
component is missing or unreliable. Ordinary source-local results, currency
caveats, or no-causality language alone do not force with-limits. Semantic
fidelity remains independently reviewed. Resolution review decisions are binary
per mapping; each accepted mapping may contain one or many source identities,
while coverage and exceptions are reported by the resolution job.

## 10. Downstream integration and products

After acceptance, one Result Integration Agent maps the accepted answer into
typed records. It does not redo analysis. One item-only Integration Fidelity
Reviewer checks that mapping before commit. Only accepted reviewed records can
update the LEM or Prepared Data Registry.

Publish only material reusable semantics actually established: business
objects/table mappings, grain, key fields and normalization,
relationship/cardinality/coverage/date authority/limits, and truly reusable
prepared-asset descriptors. Do not publish every merge, result row, metric
observation, Japan/Spain filter, or question-specific aggregation. `no_change`
is valid only when no reusable semantic understanding or asset was established,
with a concrete reason; it is not the default for every answer. The current
Integration Fidelity Reviewer checks semantic correctness without a new role,
gate, mandatory large schema, or minimum count.

Q1 can publish order-header/order-line/delivery/customer/material objects and
relationships plus a reusable order-fulfillment core. Q2 and Q9 search,
select, and load those exact accepted IDs instead of rediscovering joins, then
compute their own requirement-specific measures and filters.

Integration publishes only reviewed tested relationships and canonical identity
mappings established by the owner; it does not complete a theoretical graph or
infer joins from prose.

After the supplied queue is terminal, freeze reviewed outputs and render the
dashboard. Rendering performs no new analytics. Every visible claim or metric
must show its scope and link to reviewed evidence.

## 11. Program-owned progress and recovery

The program creates the durable workspace, bound context, receipts, hashes,
journals, and lifecycle state. It observes materialized artifacts rather than
prose activity. The first unchanged observation yields materialization guidance;
the second yields `retry_same_attempt` so the host interrupts and retries the
same owner/attempt. This does not authorize lifecycle recovery, which still
requires a canonical persisted receipt proving actual lane/provider/host/process
loss.

Keep raw inputs read-only and remain inside the active run. Do not read sibling
runs, previous answers, hidden memory, or cross-run caches in clean-room mode.
Program defects are technical failures, never conclusions about the data.
Persisted contexts resume directly after implementation changes; no transition
or rebind ceremony is required.
