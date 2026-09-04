---
name: auto-foundry-agentic-e2e
description: Runs reviewed enterprise analytics for supplied questions or analytics requirements. Use it when one Analytical Owner should investigate evidence, optionally delegate bounded specialist checks, produce the final business answer, and build a traceable offline dashboard while deterministic program code owns data access, execution, state, review normalization, integration, and recovery.
metadata:
  author: auto-foundry
  version: "0.8.0"
  core_name: auto_foundry_core
  core_version: "0.9.0"
  architecture: analytical-owner-deterministic-workbench
  release: reliable-analytics-dashboard
---

# Auto Foundry Agentic E2E — v0.8.0

## 1. Objective

Optimize for two user-visible outcomes:

1. a strong, evidence-backed business analysis;
2. a clear, traceable local dashboard built only from reviewed results.

Treat lifecycle, JSON, hashes, receipts, manifests, integration records, and
recovery journals as program-owned infrastructure. Use them to improve
analytical quality and reproducibility; never make an agent spend its reasoning
budget authoring or interpreting those internal formats.

Record these release markers in program metadata and the final report:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.8.0
core_name: auto_foundry_core
core_version: 0.9.0
```

## 2. One Analytical Owner

Assign exactly one **Analytical Owner** to each active question or requirement.
The owner keeps uninterrupted responsibility for the complete cognitive loop:

```text
interpret the decision
  → choose the answer strategy
  → inspect sources and semantics
  → test relationships and data quality
  → define population, denominator, period, and units
  → calculate and explore alternatives
  → interpret findings and limitations
  → write the complete business answer
  → apply material business repairs, each followed by a targeted recheck, if requested
```

Do not split this cognitive loop into a planning worker and an execution owner
who inherits a technical handoff. In Requirement Mode the event-driven Planner
recommends scheduling, but it does not calculate, interpret, or own an answer.
Never instruct the Analytical Owner
to avoid the final calculation or draft. Deterministic execution and specialists
are tools of the same owner; they do not receive ownership of the answer.

Use the owner to:

- preserve the exact supplied wording and decision context;
- choose the smallest useful analytical route rather than a mandatory stage
  pipeline;
- revise the strategy when source semantics, joins, quality, or coverage differ
  from the initial hypothesis;
- preserve useful source-local or partial answers when another component is
  unsupported;
- distinguish direct evidence, proxy, scenario, association, and causal claim;
- submit one coherent answer with evidence, method, scope, limitations, and
  safe next actions.

For the detailed analytical loop, read
[QUESTION_ANALYSIS_PLAYBOOK.md](references/QUESTION_ANALYSIS_PLAYBOOK.md).
For concrete owner, specialist, reviewer, and integration prompts, read
[ANALYTICAL_COLLABORATION.md](references/ANALYTICAL_COLLABORATION.md).
For the supported profile, KPI, segmentation, and scoring workflow, read
[ANALYTICS_TOOLKIT.md](references/ANALYTICS_TOOLKIT.md). The package installs
the core analytics dependencies (pandas, NumPy, SciPy, scikit-learn, and
PyArrow); use the optional `io` extra only for XLSX (`openpyxl`).

The Data Room admits every safe regular file. CSV/TSV/JSON, native Parquet,
and SQLite databases (`.db`, `.sqlite`, `.sqlite3`) receive bounded catalog
and read support; SQLite contributes one table entry per user table. Unknown,
extensionless, notebook, and other auxiliary files remain catalogable opaque
members for explicit materialization, but are not analytically parsed by the
core.

## 3. Optional specialist spokes

Use the smallest useful set of specialists only when genuinely independent
bounded investigations can materially improve the active answer. The set may
be empty and must be bounded by actual host capacity; never create one
specialist per method or checklist item. Keep the Analytical Owner at the
center and parallelize only disjoint scopes.

Supported specialist shapes include:

- **Data Quality Specialist** — keys, duplicates, joins, coverage, fanout,
  missingness, dates, and conflicting records;
- **Metric and Method Specialist** — definitions, population, denominator,
  grain, period, scenarios, units, and calculation checks;
- **Business Context Specialist** — operational meaning, decision relevance,
  source authority, and safe interpretation;
- **Document or Process Specialist** — policies, contracts, SLA applicability,
  events, ordering, incomplete cases, and process evidence.

Give each specialist a narrow question, bounded source IDs, and an expected
evidence memo. Require only:

```text
conclusion
evidence
method
limitations
open questions
confidence
```

Do not ask a specialist to produce the final answer, item state, JSON paths,
review packets, integration records, or lifecycle decisions. The program
stores the assignment and memo; the Analytical Owner evaluates and synthesizes
them. Read [ANALYTICAL_COLLABORATION.md](references/ANALYTICAL_COLLABORATION.md)
before routing a specialist.

## 4. Program-owned analyst interface

Before the Analytical Owner starts, let the program create one `RunContext`,
one logical run-local data room, one `ItemWorkspace`, and one item-bound
analysis context for the item. In normal Requirement Mode every item binds
directly to the same run context and shared data room logically, while the program binds its physical inputs to
the active generation's immutable data revision and catalog. A context is
immutable within its attempt; a later upload publishes a successor D revision
at a safe generation boundary rather than changing an active context. Old
exact D/G bindings remain loadable for replay. Persisted contexts and their
business artifacts remain loadable after core or skill changes. The host/router
records the current owner for audit; a replacement owner may continue any
nonterminal item. Analytical agents never emit internal paths/hashes. Use
`AnalystWorkspace` as the normal agent-facing interface.

The supervisor must not pre-create analysis contexts for every requirement at
bootstrap. Create the ordinary context when an item starts; if a waiting item
has already-bound work, the owner refreshes its semantic snapshot through the
public Requirement refresh API after `ready_to_resume`, then repeats exact
selection and the final deterministic analysis against the refreshed context.

Expose semantic operations such as:

```text
brief
search_sources
sample_source
source_categories
begin_analysis
select_sources
record_evidence
assign_specialist
record_specialist_memo
run_analysis
prepare_data
submit_answer
```

Let the program translate these operations into plan, source-map, evidence,
specialist, script, draft, state, receipt, and hash artifacts. Agents must not
hand-edit `run_state.json`, `item_state.json`, acceptance envelopes, review
packets, integration state, LEM records, registry records, telemetry, or
terminal manifests.

Run custom Python, SQL, shell, notebook, spreadsheet, or chart code through the
bound deterministic execution boundary when useful. Compile or runtime coding
errors return to the same Analytical Owner and attempt. Do not treat a coding
error as an analytical conclusion or as permission to transfer ownership.
For supported tabular work, use the analytics toolkit first: `profile_data` for
descriptive profiles, `compute_kpi_table` for explicit KPI aggregations,
`segment_customers` for deterministic k-means with optional agglomerative
comparison, and `score_segments` for assigning new rows with the serialized
k-means model. Choose and record the exact method and parameters. Use custom
owner-authored code only for a method the toolkit does not support, still
through `ControlledScriptRunner`; read [ANALYTICS_TOOLKIT.md](references/ANALYTICS_TOOLKIT.md)
for the artifact and evidence handoff.
Use `ControlledScriptRunner.validate_script()` for bytecode-free syntax and
dependency preflight; do not run `py_compile` inside a run root. Load exact
source IDs from the persisted selection with `selected_sources()` or
`load_selected_source_ids()` rather than rebuilding filenames in analytical
code. When identity resolution is relevant, materialize one compact
`IdentityMappingView` and let the calculation resolve reviewed canonical IDs
through that view instead of reimplementing normalization or matching.

Reject non-finite JSON values, unsafe paths, unknown source IDs, changed source
bytes, and outputs outside the active item before review or acceptance.

Before material analysis, call `AnalystWorkspace.brief()` and search the compact
accepted semantic graph and prepared descriptors:

```text
search_ontology
search_prepared_assets
```

Use exact accepted IDs when they are useful. Select them with an explicit
purpose through:

```text
select_ontology
select_prepared_assets
```

The program records each owner/item-bound selection in
`work/semantic_selections.jsonl`. Carry forward the selected semantics,
conflicts, date authority, coverage, grain, limits, and source scope into the
working method. A prepared asset's rows are loaded only after its exact ID is
selected and the registry validates its location, schema, lineage, and content
hash through `load_prepared_asset`. Search and selection do not copy rows or
silently broaden a descriptor's scope. If no accepted descriptor applies,
continue with bounded source inspection and establish new semantics; do not
claim reuse merely because labels look similar.

The run may publish multiple successive immutable content-addressed semantic
snapshots under the run root as commits or refreshes change the semantic
projection. Each distinct snapshot manifest is stored once in the run-local
namespace; each layer/index blob is stored once per distinct canonical byte
hash under `semantic_store/blobs/<sha256>.json` and reused by every snapshot
that references it. Snapshot directories contain only the manifest. Each item
context stores only its snapshot reference, manifest hash, and layer counts;
semantic records are not embedded in `analysis_context.json`.
Owners load semantic layers on demand: ontology searches open ontology and
relationship layers, identity searches open their own layers, and prepared
descriptors open only the prepared layer. Exact selections are written once to
a content-addressed selection asset; the item trace stores only its reference,
hash, counts, purpose, and reuse/no-reuse decision.

If a prepared descriptor has an `effective_period`, carry that value unchanged
through the candidate sidecar, operation manifest/hash inputs, accepted
integration record, registry entry, and later search/select/reuse. Omitted
`effective_period` remains valid and means no period constraint; do not infer a
period from the current date.

In Requirement Mode this is a readiness/scouting pass, not a promise that the
initial plan has found every semantic relationship. The owner explicitly searches and
selects relevant ontology, identity mappings, relationships, and prepared
semantics, or records why none is relevant. If a needed identity domain is
currently `resolving`, the owner reports `waiting_on_resolution`, releases its
lane, and does not guess a join. The Planner skips to the next original-order
runnable item; when the domain becomes `ready`, it marks the earliest paused
item `ready_to_resume` and resumes it. If every runnable item is waiting, the owner lane sleeps while the
Planner awaits active resolution workers. The run is blocked only when nothing
is runnable and no resolver can make progress.

The host binds the owner in the program-owned `work/analysis_owner.json`
record; analytical agents return only semantic content and never emit that
record or its internal path. Each item stays with the same bound owner through
its plan, draft, and material repairs.

After every wait, resume, or terminal transition, the host calls
`RequirementSupervisorWorkspace.scheduling_tick()` rather than assembling item
and resolver status by hand. The tick reconciles top-level run state and returns
the next requirement only when the Analytical Owner slot is available.

On `ready_to_resume`, refresh an already-bound ordinary Requirement context
through the public semantic-refresh API before exact ontology/identity/prepared
selection and the final deterministic rerun. A context is otherwise created
only when its item starts; bootstrap does not pre-create all item contexts.

## 5. Question and requirement modes

Use exactly one mode per run.

### Question Mode

- Preserve supplied question wording and order.
- Activate one question at a time.
- Keep one Analytical Owner through investigation, calculation, draft, and
  repair.
- Continue after supported partial, limited, null, blocked, unsupported, or
  technical outcomes when later questions remain runnable.
- Build shared products only after the complete supplied queue is terminal.

### Requirement Mode

- Preserve each exact user-owned `RequirementRecord`, including original text,
  priority, objective, expected analytical and visual outputs, dependencies,
  data needs, definitions, limits, and status.
- Admit one run-level **Planner**: an event-driven control plane and cognitive
  scheduler. It reasons over the exact `RequirementRecord` values, compact
  physical catalog metadata, and current item/resolution outcomes. Initial
  order and grouping are advisory; the Planner preserves explicit user
  priority/order but never declares runtime semantic dependencies. The
  Analytical Owner discovers those only after understanding the requirement,
  and the runtime `waiting_on_resolution`/`ready_to_resume` ledger is the sole
  semantic block. `RequirementExecutionPlan` and
  `RequirementExecutionGroup` are revisionable scheduling guidance, separate
  from catalog hashes, implementation identity, and lifecycle authority. The
  Planner does not calculate, write answers, or become a specialist. The host
  gives it typed `runtime_snapshot()` and `next_actions()` views. It keeps
  dispatching the named role until the run is terminal, routes ordinary code,
  API, and artifact errors back to the same owning role, and records every
  intervention through `record_incident()`. A waiting requirement never keeps
  a later runnable requirement idle.
- Before `begin_analysis()` or `run_analysis()`, every Requirement Mode owner
  records one current-snapshot semantic decision: exact reusable IDs or an
  explicit `no_reuse_reason`. An empty semantic store does not waive this
  decision, and recovery keeps the same gate.
- Capacity is adaptive: the host reports its actual available worker slots and
  the scheduler leases the smallest useful set for the current work without
  oversubscription. Every requirement has exactly one Analytical Owner;
  reviewers remain fresh and independent. Entity-resolution jobs and bounded
  specialists use only capacity that is available, and the Planner is not
  counted or leased. If a caller requests Run A and Run B, those runs remain
  sequential.
- Bind every requirement directly to the same `RunContext` and shared data
  room. Each item creates an item-local `RequirementAnalysisPlan` with one or
  more internal `RequirementAnalysisTask` values, then follows the ordinary
  owner loop: analysis, review, iterative material repairs, accept or
  `technical_failure`, and downstream integration. Tasks are reasoning
  decomposition only; they do not create child lifecycle workspaces or agents.
  Within a group, keep one Analytical Owner per requirement. Bounded shared
  investigation may be reused, and independent groups may run when host
  capacity permits. Entity-resolution jobs are parallel external jobs, not
  extra specialists and never answer the requirement.
- For example, preserve the exact requirement “Dashboard should show the ratio
  of milk fat content to the procurement price of the raw material for that
  milk.” The owner may plan (1) define the milk lot and fat measure and map
  raw-material procurement cost, then (2) calculate/reconcile the ratio and
  specify the visualization. The same owner executes the tasks and synthesizes
  one parent answer.
- Classify each record as `analytics_in_scope`,
  `analytics_requires_missing_data`, or `out_of_analytics_scope`, and honor
  explicit priority while the Planner recommends the current order.

When the Analytical Owner proposes a new arbitrary real-world identity domain
during scouting, the runtime may reserve that exact owner-bound proposal as
`resolving` and launch one Entity Resolution Owner. The Planner never invents
or pre-reserves identity domains. If the current requirement needs the result,
it waits and later resumes; an accepted/integrated item may also leave a
proposal for later reuse. Domain scope is not a hardcoded list of examples;
strongly coupled classes may share one domain. The Entity Resolution Owner
scans all rows of domain-relevant tables and relevant documents selected from
reservation hints, expands only for concrete matching/conflict evidence, reuses
the run-level catalog, owns the matching methodology, and may inspect manually,
write Python/SQL/scripts, use existing helpers, infer and bulk-apply
patterns when justified, test samples/coverage/exceptions/population
differences, and revise the method. Manual row-by-row review is not required,
and an absent authoritative crosswalk is not by itself insufficient evidence.
Never force a fixed matching script. Pattern rules remain run knowledge; a
future helper-library audit is deferred and is not implemented here.

The review decision is binary per proposed mapping (accepted or not accepted).
Each accepted `CanonicalMapping` may contain one or many source identities or
representations, including bulk pattern-derived populations. Unresolved or
ambiguous records stay source-local and outside canonical mappings, with their
coverage and exceptions preserved; they do not downgrade proven mappings. A
ready publication with accepted mappings contains the canonical class, source-account representation
classes, reviewed `IdentityDecision`/`CanonicalMapping` records, identity
`represents` relationships, and a versioned mapping asset with coverage when
available. The `ready` snapshot is exposed only after the reviewed result is
committed atomically. Analytical Owners see only `resolving` or `ready`
snapshots, never a partial mapping.
If no deterministic mapping exists, the resolver may instead publish the
explicit outcome `no_mapping_found` with non-empty population, coverage,
unresolved records, and evidence. That outcome publishes no ontology,
decisions, mappings, or relationships and lets the requirement resume
source-local; an unexplained empty result is a technical error.
Before review, `submit_result()` performs the same typed projection validation
used by commit. A malformed candidate remains with the same resolver and lease
for technical correction; it does not consume a business `repair_once` slot.
The Planner may show `mapping_completeness_advisory()` to owners and reviewers,
but this report is non-gating while it matures and advisory failure never stops
analysis or commit.

## 6. Business review

Route one fresh **Independent Business Reviewer** after the Analytical Owner has
submitted a coherent draft. Give the reviewer the original question, answer,
evidence, method, scripts/outputs, population, relationships, assumptions,
specialist memos, and limitations.

Ask the reviewer to check business substance:

- whether the answer addresses the decision;
- whether material numbers and claims are evidence-backed;
- whether scope, period, population, denominator, grain, and units are clear;
- whether joins, identity routes, policies, proxies, scenarios, and causal
  language are defensible;
- whether useful supported components are preserved;
- whether material source-completeness gaps remain.

The reviewer returns `accept`, `accept_with_limits`, `repair_once`, or
`confirm_data_insufficiency`. For every material finding, require only:

```text
target answer section
semantic categories (one or more, canonical order): answer | calculation | evidence | method | source_completeness | presentation
problem
evidence
required change
```

Do not require the reviewer to author JSON pointers, artifact paths, dependent
output paths, state, or a review packet. Keep each finding as semantic repair
provenance. Once `repair_once` is authorized, return it to the same Analytical
Owner; that owner may update any item-local work and the affected answer
section needed to resolve the finding. The program rejects cross-item writes
and rechecks the repaired points. Material repairs may repeat and each receives
its own targeted recheck. There is no arbitrary repair-count or same-owner
lock; coding feedback and execution recovery are ordinary work.

Only the Analytical Owner may originate a `DataInsufficiencyConclusion`. It
must name an unanswerable component, missing information, searches/tests
performed, evidence references, and any supported components. The reviewer
does not author that conclusion; it may only return
`confirm_data_insufficiency` after checking the owner's conclusion. Use
`blocked_by_evidence` only after that explicit owner conclusion and reviewer
confirmation. Presentation, calculation, evidence, method, reviewer, program,
and script defects require repair or a technical failure, never a blocked
business outcome.

Calibrate verdicts consistently: use `accept` when the core requested decision
is answered and only normal disclosed limits remain. Use `accept_with_limits`
only when a material requested component is missing or unreliable. Ordinary
source-local results, currency caveats, or no-causality language alone do not
force `accept_with_limits`. Semantic fidelity remains independently reviewed.
For resolution work, the review decision remains binary per mapping (accepted
or not accepted), while each accepted mapping can cover one or many source
identities and the resolution job reports coverage and exceptions separately.

If no reviewer route is available, preserve the bounded answer with an explicit
unavailable-review limitation. Read
[REVIEW_PROTOCOL.md](references/REVIEW_PROTOCOL.md) for recovery and strict
scope details.

## 7. Acceptance and result integration

After business acceptance, let the program atomically preserve the exact
reviewed answer bytes and acceptance envelope. Then use exactly one **Result
Integration Agent** to map the accepted answer into small typed claims,
metrics, limitations, evidence links, prepared assets, ontology definitions,
relationships, and dashboard facts.

Require explicit mapping payloads for typed integration records. Do not pass
free-form owner or reviewer prose directly into `IntegrationSession`.
Do not pass a caller-owned cumulative LEM into the session. The program
rebuilds that read-only view in lifecycle order from validated prior committed
integration records, which remain the sole durable authority.

Keep integration downstream:

- it cannot repair an unaccepted business answer;
- it cannot replace the Analytical Owner or Business Reviewer;
- it must not reopen analysis or read sibling/prior contexts;
- an integration defect is technical and must not erase a valid accepted
  answer.

For toolkit results, `IntegrationSession.create/load` automatically stages the
exact sealed, business-accepted typed `AnalyticalArtifact` handoff and its
evidence references. The Integration Agent must not manually re-submit or
re-declare those artifacts; it uses the existing validation/fidelity review
and commit APIs and does not invent an integration method or write integration
JSON directly.

Keep the accepted business answer and its `accepted_content_hash` immutable.
Integration records are a derived pre-commit projection, so the same
Integration Agent may correct or remove only the record IDs authorized by the
fidelity review. Preserve the accepted hash and business meaning, rebuild the
fidelity packet, and submit the targeted recheck before committing. Differences
between normalized typed fields and the accepted prose/artifact are ordinary
projection work, not a semantic conflict or a reason to refuse; never edit the
accepted answer or redo the analysis.
See [ANALYTICS_TOOLKIT.md](references/ANALYTICS_TOOLKIT.md).

Run mechanical validation, then one fresh item-only **Integration Fidelity
Reviewer**. Allow the same Integration Agent one affected-record repair and one
targeted recheck. Apply only reviewed accepted records to the LEM and Prepared
Data Registry.

Publish material reusable semantics actually established by the accepted item:
business objects and table mappings; grain; key fields and normalization;
relationship/cardinality/coverage/date authority/limits; and descriptors for
truly reusable prepared assets. Do not publish every merge, every result row,
metric observation, Japan/Spain filter, or question-specific aggregation as
ontology or reusable preparation. The existing Integration Fidelity Reviewer
uses the current review boundary to check semantic correctness; this feature
adds no role, gate, mandatory large schema, or minimum record count.
Use `add_current_observation()` for a currently measured value that should be
available to the dashboard without becoming an ontology definition. Determine
its `as_of` only from observed timestamps with `observation_as_of()`; planned,
due, target, or obligation dates are not current-state authority. Repeated
observation shapes may produce `suggest_semantic_promotions()` records, but
those suggestions are advisory only and never mutate or gate the ontology.
Pilot a prepared asset only when the same typed rows are genuinely reusable by
a later requirement; the absence of a prepared asset is never a failure.
A `no_change` result is valid only when there is genuinely no reusable semantic
understanding or asset from the accepted item, with a concrete reason; it is
not the default for every answer. For Q1→Q2/Q9, Q1 may publish order-header,
order-line, delivery, customer, and material objects and relationships plus a
reusable order-fulfillment core. Q2 and Q9 search/select/load those exact IDs
instead of rediscovering joins, then compute their own requirement-specific
measures.

A Result Integration Agent publishes only reviewed, tested relationships that
the Analytical Owner established with `source_id`/`target_id`, `join_keys`,
grain, cardinality, `matched_pairs` (the unique tested edge-pair count),
`source_population`/`target_population`, `matched_source_count`/
`matched_target_count` (distinct matched endpoints), and
`source_coverage`/`target_coverage` (endpoint count divided by its population,
with zero for a zero population), plus `as_of`/date authority, limitations,
and evidence. It also
publishes reviewed canonical identity mappings. It never completes a
theoretical graph or infers a relationship from prose.

Read [KNOWLEDGE_AND_REUSE.md](references/KNOWLEDGE_AND_REUSE.md) for the typed
post-acceptance boundary.

## 8. Dashboard and final products

After all item outcomes and integration states are terminal, freeze accepted
answer references, LEM, prepared registry, and telemetry. Build a local static
dashboard from reviewed outputs only. Do not perform new analytics during
rendering.

Every business-accepted Analytical Owner answer is a presentation input,
whether or not its separate integration projection committed successfully.
Use the answer's reviewed ``visuals``, headline findings, and limitations as
source-bound presentation candidates; integration remains an ontology and
machine-reuse concern, never a prerequisite for showing an accepted business
view. Ensure every accepted requirement receives a meaningful decision surface
or an explicit limitation. Select one semantic representative per
requirement/business metric/scope, prefer a richer eligible source-bound chart
over a table when the exact reviewed geometry supports it, and record a
concise data/decision rationale when a table is deliberately selected.
Populate the executive overview from admitted reviewed business signals when
available, demote integration echoes/support cards instead of duplicating an
accepted visual, and keep technical source/join/count evidence in the audit
surface unless the Product Agent deliberately presents an explicit
source-bound business consequence. Use concise manager-facing titles and never
expose raw failure reasons or internal paths in manager HTML.

All Product Agent presentation actions use `ProductWorkspace(context, action)`.
Inspect `inventory()` with pagination, inspect selected `detail(widget_id)` and
independent `feedback()`, and call `build(choices, presentation=...)` once with
complete ordered business choices. The workspace derives source bindings, CAS,
revision namespaces, generation routes, candidate receipts and retry identity.
Do not manually operate the lower-level assembler/plan APIs or edit artifacts.

This hybrid strategy publishes cumulative nonfinal previews from accepted
requirements as work finishes, and composes a final cross-requirement dashboard
once all boundaries are terminal. Reuse accepted numbers and analytical artifacts;
never rerun analytics to render, invent statistics, or re-author identical results.
Integration remains the separate ontology/machine-reuse boundary, not a condition
for a technically limited accepted answer to be visible. Semantic review defects
must be repaired by the analytical owner; presentation is not an escape hatch.

Use the reference design's visual principles: compact KPI strip, readable cards,
responsive multi-section chart grid, meaningful legends/units, and an evidence
surface separate from the manager overview. Select diverse chart types only when
the evidence supports them. Preserve missing-value gaps and independent scales.
The renderer renders accepted answer visuals together with any committed facts.
No externally loaded assets, analytics trackers, or invented decorative data.

Read [PRODUCT_AGENT_ASSEMBLER_CONTRACT.md](references/PRODUCT_AGENT_ASSEMBLER_CONTRACT.md)
for the single interface and the candidate → independent review → authorization
boundary. Frozen accepted generations are never overwritten by a new design.

The portfolio revision is explicit program API work before this dispatch. Use
`RequirementRunExtension.append(context, new_records)` for a simple add, or
save a revised `RequirementExecutionPlan` for add/update/remove/reorder. The
revision may be empty and may later grow again. Unchanged items are reused;
changed and removed item histories are archived.
The generation metadata `plan_hash` remains immutable admission lineage, while
the active plan may receive a legitimate higher-revision replan before
assembly. Bind both the admission plan hash and current live plan hash; the
current explicit route must still agree with the cumulative plan. Parent state
and plan hashes are checked against their immediate referenced files. Parent
product-manifest bytes/ref and parent receipt bytes/ref are separate lineage
fields. A pre-swap crash retries the same generation from the immutable
parent; a post-swap crash retries receipt validation and product-manifest
completion without rewriting the dashboard tree. Concurrent publishers
reload the active pointer after a generation-scoped process/thread lock and
reject transitions; every staged file/directory is recursively fsynced before
the atomic directory rename.
Before staging or writing, resolve the generation-specific product-manifest
leaf through the lexical product boundary; any symlink component or leaf is a
hard failure. The delta receipt schema is exact at every nested binding. On
existing-output or post-swap recovery, reconstruct and compare every receipt
field (inputs/outputs, parent and plan/state lineage, projections/freeze,
affected/unchanged paths, rollback parent, and counts) rather than trusting
the stored JSON.

The generation entry point hands off a fresh root product manifest for G-0001;
only its internal delta path writes the active generation's
`products/generations/<generation-id>/product_manifest.json`.
Run-local references are lexically symlink-free before resolution/open, the
immediate parent manifest must bind the exact receipt hash and generation, and
the child manifest's schema/assets/lineage are exact retry bindings.

The receipt-bound authoritative site is `<output_root>/site` and is immutable
after assembly. Acceptance/browser QA is a later sibling artifact: the runner
writes exactly 14 PNG captures plus `qa_report.json` under
`<output_root>/qa`, never under `site/qa`. The assembler does not create or
bind nonexistent QA during the build; a later `qa_output_ref`, when supplied
by the host, is advisory and does not change the site binding. An exact retry
after sibling QA must remain idempotent, while any site-file mutation fails
closed.
The assembler uses the offline multi-page renderer internally. Keep the
overview short, give each business domain its own page, keep ontology and
evidence/audit on separate pages, and collapse detail tables. Prefer the most
legible reviewed chart form over prose; do not turn a paragraph containing many
numbers into a table-shaped paragraph.
Show supported KPI cards and charts with visible:

- period, population, denominator, units, and grain;
- proxy, scenario, and causal-status labels;
- limitations, blocked components, and evidence gaps;
- links from every metric or claim to its accepted item and evidence.

Do not add a mandatory universal relationship form to the Analytical Owner's
work. Relationship evidence remains business-shaped and only the downstream
typed integration boundary normalizes the fields it actually needs.

Use offline assets and validate internal links. Read
[FINAL_PRODUCT_AND_AUTOMATION.md](references/FINAL_PRODUCT_AND_AUTOMATION.md)
for freeze and optimizer contracts.

## 9. Recovery, clean-room, and lifecycle

Keep supplied sources read-only and all derived work under the active run root.
Do not read sibling runs, previous caches, prior answers, hidden prompts, or
cross-run knowledge in clean-room mode.

Use `run_state.json` and `item_state.json` as durable observations; prose never
silently mutates lifecycle. A user may pause, resume, or reopen the run, and a
replacement owner or attempt may continue verified item work. Receipts retain
execution evidence but are not authorization tokens.

Binding, telemetry, terminalization, and recovery remain deterministic program
concerns; do not place their internal paths or hashes in an analytical-agent
prompt. Code/skill versions do not lock contexts. Reuse accepted business
conclusions, committed semantics, relationships, and prepared assets through
the normal workspace APIs. The Planner may revise the portfolio and scheduling
at any time, including after a terminal outcome.

## 10. Constraints

- Do not use a handoff-only Lead Analyst or transfer final-answer ownership to a
  coding worker.
- Do not expose internal state schemas, paths, hashes, receipts, or manifests to
  analytical roles unless diagnosing a program defect.
- Do not make specialists mandatory or let them own another item lifecycle.
- Do not treat an Entity Resolution Owner as an owner specialist; resolution
  jobs run in parallel, publish only mappings with a binary review decision
  (each may contain one or many source identities), and do not answer the
  requirement.
- Do not make the Business Reviewer repeat the entire analysis, create pointers,
  or become a second author.
- Do not let Integration or Fidelity review block preservation of an already
  accepted business answer.
- Do not add parallel question waves, a reviewer-of-reviewer, manual
  terminalizer, second integration reviewer, central ontology, cross-run cache,
  production app, or client-business automation. Requirement groups may run
  independently when host capacity permits; this does not change one owner per
  requirement or the ordinary item loop.
- Do not require row-by-row identity review, an authoritative crosswalk, or a
  fixed matching script. Do not expose partial resolution snapshots or promote
  unresolved/ambiguous records into canonical mappings.
- Do not treat artifact count as analytical quality. Evidence and reproducible
  work demonstrate progress; the user-visible answer and dashboard remain the
  objective.

This skill is an offline-friendly analytical workflow, not a claim of host
sandboxing, benchmark completion, or production hardening.
