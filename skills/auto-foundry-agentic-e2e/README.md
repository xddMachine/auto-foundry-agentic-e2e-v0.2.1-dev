# Auto Foundry Agentic E2E Skill v0.7.2

This skill produces two things: strong reviewed business analysis and a clear
offline dashboard built from reviewed results. Deterministic core code exists
to make that work easier and safer, not to turn analytical agents into state or
JSON operators.

## Current markers

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.7.2
core_name: auto_foundry_core
core_version: 0.8.1
```

## Architecture

```text
user question
  -> one Analytical Owner
       -> bounded data room
       -> reproducible calculations
       -> smallest useful set of specialist memos for independent uncertainty
       -> complete business answer
       -> iterative material repairs, each with a targeted recheck
  -> one Independent Business Reviewer
  -> immutable accepted answer
  -> one Result Integration Agent
  -> one item-only Integration Fidelity Reviewer
  -> reviewed-output dashboard

Requirement Mode adds an event-driven Planner and parallel Entity Resolution
Owner jobs. The Planner schedules runnable items; it does not answer them or
predeclare runtime semantic dependencies.
```

The host binds each Analytical Owner in the program-owned
`work/analysis_owner.json` record. Agents return semantic content only and do
not author that internal record or path.

The Analytical Owner keeps the whole cognitive loop. It interprets the
question, chooses the strategy, inspects sources, defines the population and
method, tests data quality and relationships, calculates, interprets, and
writes the final answer. It does not hand the answer to a separate coding
worker.

Specialists are optional bounded spokes. They return evidence memos; they do
not own the parent answer. The Business Reviewer checks business substance and
describes findings by answer section and one or more semantic categories. It does not author JSON
pointers, paths, hashes, or review packets.

`AnalystWorkspace` is the analytical-agent surface. It exposes business-shaped
operations such as:

```text
brief
search_sources / sample_source / source_categories
search_ontology / select_ontology
search_prepared_assets / select_prepared_assets / load_prepared_asset
begin_analysis / select_sources / record_evidence
assign_specialist / record_specialist_memo
run_analysis
prepare_data
submit_answer
review through BusinessReviewAdapter
accept
```

The core translates these calls into strict JSON artifacts, hashes, receipts,
semantic repair provenance, and lifecycle state. `NaN`, `Infinity`, unknown sections,
invalid categories, duplicate IDs, and out-of-scope repair fail before
acceptance.

Before analysis, the owner checks `brief` and searches the compact accepted
semantic graph and prepared descriptors. When useful, it selects exact
accepted ontology or prepared-asset IDs with a purpose. The program records
those owner/item-bound decisions in `work/semantic_selections.jsonl`; prepared
rows are loaded only after selection and exact registry content-hash
validation. A missing or inapplicable descriptor sends the owner back to
bounded source inspection rather than to a guessed join.

When present, a prepared descriptor's optional `effective_period` is carried
unchanged through its candidate sidecar, operation manifest/hash inputs,
accepted integration and registry, and later reuse. Omission remains valid and
means no period constraint; never infer a period from the current date.

Requirement Mode starts with an owner readiness/scouting pass: explicitly
search and select applicable ontology, identity mappings, relationships, and
prepared semantics, or record why none applies. If a needed identity domain is
`resolving`, the owner reports `waiting_on_resolution` and releases its lane;
the Planner skips to the next original-order runnable item and marks the
earliest paused item `ready_to_resume` when the domain is `ready`. If all
runnable items wait,
the owner lane sleeps while active resolvers progress. Block only when nothing
is runnable and no resolver can progress.

Use one `RequirementSupervisorWorkspace.scheduling_tick()` after each wait,
resume, or terminal transition so item/resolution state and owner capacity are
joined once and the aggregate run status stays current.

Result Integration publishes material reusable semantics actually established:
business objects and table mappings, grain, key fields and normalization,
relationship/cardinality/coverage/date authority/limits, and truly reusable
prepared-asset descriptors. It does not publish every merge, result row,
metric observation, Japan/Spain filter, or question-specific aggregation.
`no_change` is valid only when the item established no reusable semantic
understanding or asset; it is not a default for every answer. For example, Q1
can publish order-header/order-line/delivery/customer/material objects and
relationships plus a reusable order-fulfillment core; Q2 and Q9 search,
select, and load those exact IDs, then compute their own requirement-specific
measures.

After acceptance, `IntegrationSession` is deliberately typed. The Result
Integration Agent maps the accepted answer into small claims, metrics,
limitations, evidence links, prepared assets, ontology definitions,
relationships, and dashboard facts. Integration is downstream and cannot
repair or replace the business analysis.

The Analytical Owner establishes actual joins and relationships and records
`source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
unique tested edge-pair count), `source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (endpoint count divided by its
population, with zero for a zero population), plus `as_of`/date authority,
limitations, and evidence. Integration publishes only those
reviewed tested relationships plus canonical identity mappings; it never
completes a theoretical graph or infers one from prose.

The program, not the agent or caller, restores cumulative LEM context. It
projects validated prior committed integration records in lifecycle order, so
there is no separate mutable LEM checkpoint to reconstruct or reconcile.

## Data Room admission

The Data Room accepts every safe regular file. Native catalog/read paths cover
CSV/TSV/JSON, Parquet, and SQLite (`.db`, `.sqlite`, `.sqlite3`) with one
catalog entry per SQLite user table. Unknown, extensionless, notebook, and
other auxiliary files are retained as opaque members for explicit
materialization; they are not analytically parsed by the core.

## Modes

Question Mode preserves supplied wording and order, activates one question at
a time, and continues after bounded partial or technical outcomes when later
questions remain runnable.

Requirement Mode preserves each exact user-owned `RequirementRecord`, including
priority, objective, expected outputs, dependencies, data needs, definitions,
limits, and status. One run-level **Planner** is an event-driven control plane
and cognitive scheduler. It receives those records, compact physical metadata,
and current item/resolution outcomes. Initial order and grouping are advisory;
the Planner preserves explicit user priority/order but never declares runtime
semantic dependencies. The Analytical Owner discovers those only after
understanding the requirement, and the runtime
`waiting_on_resolution`/`ready_to_resume` ledger is the sole semantic block.

`RequirementExecutionPlan` and `RequirementExecutionGroup` are revisionable
recommendations/current scheduling, not catalog-hash or lifecycle authority.
The Planner consumes typed runtime snapshots and next actions, dispatches the
named roles until terminal, and records every intervention as a canonical run
incident. It routes ordinary errors back to the same owner. It can replan after
outcomes change, but a technical failure does not create Planner dependency
blocks; independent groups remain eligible and runtime resolution state
controls waiting and resume. Every requirement binds
directly to the same `RunContext` and shared `DataRoomWorkbench`.
Capacity is adaptive to the actual host capacity: the scheduler leases the smallest
useful set for genuinely independent work without oversubscription. Every
requirement has exactly one Analytical Owner, reviewers remain fresh and
independent, and the Planner is not counted or leased. Requested Run A and Run
B executions remain sequential.
Within a group, one Analytical Owner remains responsible for each requirement;
bounded shared investigation may be reused and independent groups may run when
host capacity permits. Each item follows the ordinary loop: item-local
`RequirementAnalysisPlan`, analysis, review, as many evidence-backed repair
cycles as are useful, accept or `technical_failure`, then integration. `RequirementAnalysisTask`
values are reasoning decomposition only and never child lifecycle workspaces.
Entity-resolution jobs are parallel external jobs, not extra owner specialists
and never answer a requirement.
For the exact requirement “Dashboard should show the ratio of milk fat content
to the procurement price of the raw material for that milk.”, example tasks are
to define the milk lot/fat measure and map raw-material procurement cost, then
calculate/reconcile the ratio and visualization. Requirement Mode is
analytics-only and does not automate a client business process.

The host/router records the current owner in program-owned audit state;
analytical agents never emit internal paths or hashes. Another owner may later
continue the same nonterminal item, at which point the audit binding is
updated. A repair finding is provenance, not a filesystem capability: the
owner may update any item-local work and coherent answer section needed to
resolve it. Cross-item writes remain rejected.

The current review verdicts are `accept`, `accept_with_limits`, `repair_once`,
and `confirm_data_insufficiency`. Each material repair receives a targeted
recheck, but there is no arbitrary repair-count limit. Code feedback and
execution recovery are ordinary work, not a separate repair budget. Only the
owner may write a `DataInsufficiencyConclusion`; the reviewer
can only confirm it, after which the program may terminalize
`blocked_by_evidence`. Other defects repair or technical-fail.

When an Analytical Owner proposes a new arbitrary real-world identity domain
during scouting, reserve that exact owner-bound proposal as `resolving` and
launch an Entity Resolution Owner; the Planner cannot pre-reserve it. Strongly coupled classes may share a domain; there is no
hardcoded Supplier/Factory/Order scope. The owner scans every row of
domain-relevant tables and relevant documents selected from reservation hints,
expanding only for concrete matching/conflict evidence. It reuses the run-level
catalog instead of rescanning unrelated members, owns methodology, may inspect
manually or write Python/SQL/scripts/use helpers, infer and bulk-apply justified patterns, test samples/coverage/
exceptions/population differences, and revise the method. Manual row review,
an authoritative crosswalk, or a fixed matching script is not required.
Pattern rules remain run knowledge; future helper-library audit is deferred.

The review decision is binary per proposed mapping (accepted or not accepted).
Each accepted `CanonicalMapping` may contain one or many source identities or
representations, including bulk pattern-derived populations. Unresolved or
ambiguous records remain source-local and outside canonical mappings, with
coverage and exceptions preserved; they do not downgrade proven mappings. A
ready snapshot publishes the canonical class, source-account representation
classes, reviewed `IdentityDecision` and `CanonicalMapping`, identity
`represents` relationships, and a versioned mapping asset/coverage when
available. Owners never see partial snapshots.
The ready snapshot is exposed only after the reviewed result is committed
atomically.

Use `accept` when the core decision is answered and only normal disclosed limits
remain. Use `accept_with_limits` only for a material requested component that
is missing or unreliable; source-local, currency, or no-causality caveats alone
do not force it. Resolution review decisions are binary per mapping; each
accepted mapping may contain one or many source identities, while coverage and
exceptions are reported at the job level. Semantic fidelity is reviewed
independently.

## Clean-room and recovery

Inputs stay read-only. A clean-room run does not read sibling runs, prior
answers, cross-run caches, or hidden memory. Program-owned state and receipts
retain progress and audit evidence. Coding errors may be continued by the same
or a different Analytical Owner and attempt; receipts do not grant permission
or consume a repair budget.

Every Requirement Mode item binds directly to the same run context and shared
data room. Persisted analysis contexts load across core and skill revisions
without transition or rebind ceremonies. Source/catalog hashes still detect
real input corruption. Committed integration semantics, business conclusions,
relationships, and prepared assets remain reusable. The portfolio and
scheduling plan may be revised while running, paused, or complete.

## Products

Freeze reviewed accepted answers, integration state, prepared registry, LEM,
and telemetry before rendering. The dashboard performs no new analytics. It
uses a short overview plus separate business-domain, ontology, and
evidence/audit pages. Charts lead; detail tables are collapsed. Every page
shows the relevant periods, populations, denominators, units, proxies,
limitations, and clickable evidence links. The optimizer observes frozen evidence read-only and
never performs client-business automation.

## Reference map

- [Main skill contract](SKILL.md)
- [Analytical playbook](references/QUESTION_ANALYSIS_PLAYBOOK.md)
- [Analytical collaboration and role prompts](references/ANALYTICAL_COLLABORATION.md)
- [Business and fidelity review protocol](references/REVIEW_PROTOCOL.md)
- [Knowledge and prepared-data reuse](references/KNOWLEDGE_AND_REUSE.md)
- [Artifact and efficiency policy](references/ARTIFACT_AND_EFFICIENCY_POLICY.md)
- [Analytics toolkit](references/ANALYTICS_TOOLKIT.md)
- [Final products and optimizer](references/FINAL_PRODUCT_AND_AUTOMATION.md)
- [Offline contract prompts](TEST_PROMPTS.md)
- [Program-populated item result view](assets/QUESTION_RESULT_TEMPLATE.md)

## Validation

The canned replay is a synthetic zero-model cassette that exercises the real
core path for three questions, optional specialists, business review and
repair, integration fidelity repair, products, reporting, optimizer, and
fail-closed probes. It is not an agent response schema and does not prove live
model quality.

This package is offline-friendly and experimental. It is not a production
sandbox or a claim that Benchmark A has been completed.
