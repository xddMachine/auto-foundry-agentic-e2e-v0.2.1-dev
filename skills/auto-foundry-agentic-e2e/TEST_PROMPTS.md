# Offline test prompts

These are contract fixtures. Use synthetic sources only. Do not call a real
model, benchmark, network, external system, or prior run.

Record these current markers in program metadata:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.7.1
core_name: auto_foundry_core
core_version: 0.8.0
```

## Question Mode: analytical-owner queue

```text
Use $auto-foundry-agentic-e2e in clean-room Question Mode.

Preserve these exact questions and process one question at a time in order:
1. Count fulfilled order lines by month from one supplied table.
2. Compare invoice totals with a second source, preserving source-local results
   when the identity link is incomplete.
3. Normalize a small supplier-status field and report exclusions.

For every question, the host creates the durable item and BoundAnalysisContext,
then gives one Analytical Owner an AnalystWorkspace. The owner keeps the full
reasoning loop: interpret, search sources, define population and method, test
relationships and quality, calculate, interpret, write the answer, and perform
material repairs with targeted rechecks. A replacement owner may continue a
nonterminal item; do not split one active reasoning loop across relay roles.

Before source analysis, call `brief`, then search the compact semantic graph and
prepared descriptors with `search_ontology` and `search_prepared_assets`.
When useful, select exact accepted IDs with `select_ontology` and
`select_prepared_assets`, recording a purpose for each selection. Load prepared
rows only after selection and hash validation through `load_prepared_asset`.
The program records the owner/item-bound trace in
`work/semantic_selections.jsonl`.
If a selected prepared descriptor has `effective_period`, preserve it through
the candidate sidecar, operation manifest/hash, accepted integration and
registry, and later reuse; an omitted value remains valid with no period
constraint.

The owner may request zero to three independent specialist memos. Specialists
receive only a bounded question, selected source IDs, expected output, and
minimum context. They return conclusion, method, evidence, limitations, open
questions, and confidence. They do not write the final answer or program state.

Use AnalystWorkspace methods rather than asking analytical agents to author
JSON, paths, hashes, receipts, manifests, lifecycle state, or integration
records. Use reproducible controlled code where material. Syntax, import,
type, and output-validation errors return to the same owner and attempt.

After a complete answer, route one Independent Business Reviewer. It returns a
verdict and all material findings using target answer section, business
category, problem, evidence, and required change. It does not return JSON
pointers or artifact paths; BusinessReviewAdapter creates fail-closed program
scope. Allow additional material repairs whenever they are supported by a
concrete finding, each followed by a targeted recheck. Do not impose an
arbitrary repair-count or same-owner lock.

Accept exact reviewed answer bytes atomically. Only then route one Result
Integration Agent to create typed claims, metrics, limitations, evidence links,
prepared assets, ontology definitions, relationships, and dashboard facts.
Run mechanical validation and one fresh item-only Integration Fidelity Reviewer
before commit. Integration never repairs the business answer.

Publish only material reusable semantics actually established: business
objects/table mappings, grain, key fields/normalization,
relationship/cardinality/coverage/date authority/limits, and truly reusable
prepared descriptors. Do not publish every merge, result row, metric
observation, Japan/Spain filter, or question-specific aggregation. `no_change`
is valid only when no reusable semantic understanding or asset was established.

Continue after supported partial, limited, unsupported, blocked, or technical
outcomes. Build the reviewed-output dashboard only after the supplied queue is
terminal and frozen. Keep inputs read-only and telemetry passive.
```

## Requirement Mode: priority and reuse

```text
Use $auto-foundry-agentic-e2e in analytics-only Requirement Mode.

Give one run-level **Planner** (event-driven control plane and cognitive
scheduler) the exact RequirementRecord input, compact physical-metadata
catalog, and current item/resolution outcomes. It must preserve every
original_text and RequirementRecord field exactly. Its initial order and
grouping are advisory and preserve explicit user priority/order; it never
declares runtime semantic dependencies. The Analytical Owner discovers those
after understanding the requirement, and the runtime resolution ledger owns
semantic blocking. The
Planner may suggest zero to three bounded specialists per group and revise the
recommendation after outcomes. It does not calculate or write answers and is
not a deterministic ID/hash or lifecycle authority.

Represent the recommendation with revisionable `RequirementExecutionPlan` and
`RequirementExecutionGroup` values. They are scheduling recommendations, not
catalog-hash or lifecycle authority. Do not pass rows, samples, prior-run
state, or internal paths/hashes to analytical roles. The default capacity is
four entity-resolution workers, one Analytical Owner, up to three owner
specialists, and eight active workers total; the Planner is not counted. Host
configuration may lower or raise these limits but must never oversubscribe the
actual host. Requested Run A and Run B executions remain sequential.

Preserve this exact requirement as one parent record:
“Dashboard should show the ratio of milk fat content to the procurement price of
the raw material for that milk.” Before analysis, one Analytical Owner creates
a semantic item-local plan with 1..N internal analytical tasks, for example:
define the milk lot/fat measure and map raw-material procurement cost;
calculate/reconcile the ratio and visualization. The same owner executes and
synthesizes one parent answer.

The host/router records the current owner in program state before work starts.
Analytical agents never emit internal paths/hashes; a replacement owner may
continue any nonterminal item and the audit binding is updated.

R-001 priority=1: Decide whether late shipments are concentrated by carrier;
produce a reviewed rate table and trend chart.
R-002 priority=unset: Reconcile payment and invoice records; reuse a reviewed
identity mapping if valid and report missing fields.
R-003 priority=unset: Create a local evidence-readiness dashboard for R-001 and
R-002 without inventing new analytics.
R-004 priority=unset: Change the customer payment process automatically.

Classify each as analytics_in_scope, analytics_requires_missing_data, or
out_of_analytics_scope. Honor explicit user priority while the Planner
recommends the current order. Bind every item directly to the same RunContext
and shared Data Room; do not inherit a previous item context. Each owner begins
with a readiness/scouting pass: call `brief`, explicitly search/select
applicable ontology, identity mappings, relationships, and prepared semantics,
or record why none applies. Each item uses the ordinary loop: item-local
RequirementAnalysisPlan, analysis, review, iterative material repairs, accept
or technical_failure, then integration. If a needed identity domain is
`resolving`, the owner reports `waiting_on_resolution` and releases its lane;
the Planner skips to the next original-order runnable item and marks the
earliest paused item `ready_to_resume` when ready. If all runnable items wait, the owner lane
sleeps while active resolvers progress; block only when nothing is runnable and
no resolver progresses. Entity-resolution jobs are parallel external jobs, not
extra specialists and do not answer requirements. A technical failure does not
create Planner dependency blocks; independent groups remain eligible and the
runtime `waiting_on_resolution`/`ready_to_resume` ledger controls semantic
waiting and resume.

When an Analytical Owner proposes a new arbitrary real-world identity domain
during scouting, reserve that exact owner-bound proposal as `resolving` and
launch an Entity Resolution Owner; the Planner never pre-reserves it. No hardcoded Supplier/Factory/Order scope is assumed;
strongly coupled classes may share a domain. The owner scans every row of
domain-relevant tables and relevant documents from reservation hints, expands
only for concrete matching/conflict evidence, reuses the run-level catalog,
owns methodology, may inspect manually or write Python/SQL/scripts/
use helpers, bulk-apply justified patterns, test samples/coverage/exceptions/
population differences, and revise the method. Do not require row-by-row
review, an authoritative crosswalk, or a fixed matching script. Pattern rules
remain run knowledge; future helper-library audit is deferred.

The review decision is binary per proposed mapping (accepted or not accepted).
Each accepted CanonicalMapping may contain one or many source identities or
representations, including bulk pattern-derived populations. Unresolved or
ambiguous records stay source-local/outside canonical mappings with
coverage/exceptions; accepted mappings are not downgraded. Publish the
canonical class, source-account representation classes, reviewed
IdentityDecision/CanonicalMapping, identity `represents` relationships, and
versioned mapping asset/coverage where available. Owners see only resolving or
ready snapshots, never partial.

Route one Independent Business Reviewer and one downstream Result Integration
Agent plus item-only Integration Fidelity Reviewer. Reviewer findings are
semantic repair provenance; after authorization the same owner may update
item-local work and the affected answer section, with no cross-item writes. Use
`accept` when the core decision is answered and only normal disclosed limits
remain; use `accept_with_limits` only for a material requested component that is
missing or unreliable. Source-local, currency, or no-causality caveats alone do
not force with-limits. Resolution review decisions are binary per mapping while
each accepted mapping may contain one or many source identities and the job
reports coverage/exceptions. Semantic fidelity is independently
reviewed. Do not add a keyword router, business-term dictionary, auto
optimizer, or client-business automation.
```

## Canned replay acceptance matrix

The no-model replay must exercise real program APIs with synthetic cassette
conclusions:

- one data room and one read-only source catalog;
- `AnalystWorkspace` as the analytical-agent surface;
- Q-001: one owner, no specialist, same-attempt code correction, accepted
  business review;
- Q-002: one owner, one bounded specialist memo, `repair_once`, owner repair,
  and targeted acceptance;
- Q-003: one owner, two independent specialist memos, accepted business answer,
  typed integration records, fidelity `repair_once`, same integration-agent
  record correction, and targeted fidelity acceptance;
- strict rejection of NaN/Infinity, unknown review sections/categories,
  out-of-scope repair, incomplete fidelity checked IDs, duplicate invocation,
  commit before fidelity, stale reporting, and external calls;
- accepted-only integration, compact ontology, reviewed-output dashboard,
  final reporting, and frozen optimizer evidence;
- repeated fresh cycles with zero agent, model, and network calls and one stable
  semantic digest.

The cassette is not an agent response schema. The harness, not the role
fixtures, invokes core APIs and materializes internal artifacts.
