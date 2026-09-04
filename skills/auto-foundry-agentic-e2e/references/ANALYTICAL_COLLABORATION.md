# Analytical collaboration contract

Use this contract when an item benefits from more than one analytical agent.
It is a hub-and-spoke collaboration pattern, not a relay race. Requirement
Mode adds one event-driven Planner for scheduling and may launch parallel
Entity Resolution Owner jobs; the ordinary per-item owner loop remains
unchanged.

## Roles

### Planner

Use one event-driven Planner (control plane and cognitive scheduler) per
Requirement Mode run. Give it the exact user-owned `RequirementRecord` values,
compact physical catalog metadata, and current item/resolution outcomes. Its
initial order and grouping are advisory and preserve explicit user
priority/order; the Planner never declares runtime semantic dependencies. The
Analytical Owner discovers those after understanding the requirement, and the
runtime `waiting_on_resolution`/`ready_to_resume` ledger is the sole semantic
block. Keep guidance in revisionable `RequirementExecutionPlan` and
`RequirementExecutionGroup` values, not as catalog-hash, deterministic ID, or
lifecycle authority.

The Planner does not calculate, write answers, author item artifacts, or own an
item's review and repair. A technical failure does not create Planner dependency
blocks; independent groups remain eligible and runtime resolution state controls
waiting and resume. It also never fabricates an Analytical Owner proposal or
pre-reserves an identity domain. Each requirement still
has one Analytical Owner and runs directly against the same `RunContext` and
shared Data Room. Within a group, bounded shared investigation can be reused;
independent groups may run when host capacity permits. Do not give the Planner
internal paths, hashes, receipts, or filesystem controls.

After every item wait, resume, or terminal transition, call the public
`RequirementSupervisorWorkspace.runtime_snapshot()` and `next_actions()`. They
join authoritative item and resolution state, reconcile the aggregate run
status, observe capacity, and name the next role action. The Planner dispatches
those actions until the run is terminal; it does not merely recommend an order.
Keep the same role on ordinary code/API/artifact correction, record each
intervention with `record_incident()`, and do not keep the owner idle while a
later requirement is runnable. `scheduling_tick()` remains the compact
dictionary view for callers that do not need typed actions.

Capacity is adaptive to the actual host: the scheduler leases the smallest
useful set of workers for genuinely independent work and never oversubscribes
available slots. Every requirement has exactly one Analytical Owner, reviewers
remain fresh and independent, and the Planner is not counted or leased.
Requested Run A and Run B executions remain sequential.

Suggested Planner prompt:

```text
You are the event-driven Planner for one analytics-only run. Reason over the
exact RequirementRecords, compact physical catalog metadata, and current
item/resolution outcomes. Recommend or revise advisory order/grouping while
preserving explicit user priority/order. Do not predeclare runtime semantic
dependencies; the Analytical Owner discovers them after understanding the
requirement and the runtime resolution ledger owns semantic blocking. Suggest
only the smallest useful set of bounded specialists per group for genuinely
independent uncertainty; zero is valid, host capacity bounds the set, and do
not create one specialist per method or checklist item. Preserve every
RequirementRecord field and do not
calculate, write answers, mutate lifecycle state, or emit IDs, hashes, paths,
receipts, or manifests. A technical failure does not create Planner dependency
blocks; independent groups remain eligible. Use the public runtime snapshot and
next actions to dispatch the named role until terminal. Return ordinary defects
to the same owner, record every intervention as a run incident, and continue.
```

### Analytical Owner

Give one agent the original question and uninterrupted ownership of the final
answer. Ask that agent to:

1. explain the decision being supported;
2. perform a readiness/scouting pass: call `AnalystWorkspace.brief()` and
   search accepted ontology, identity mappings, relationships, and prepared
   descriptors with `search_ontology()` and `search_prepared_assets()`;
3. record one current-snapshot `select_semantic_scope()` decision before
   analysis or calculation: select exact useful IDs, or give a non-empty
   `no_reuse_reason` even when the semantic store is empty; load prepared rows only
   after `load_prepared_asset()` validates the content hash;
4. if a needed identity domain is `resolving`, report
   `waiting_on_resolution`, release the lane, and let the Planner mark the
   earliest paused item `ready_to_resume` only after a ready snapshot is
   published;
5. search and inspect the bounded source catalog;
6. choose definitions, population, denominator, grain, period, and units;
7. test material relationships, source authority, and data quality;
8. use reproducible code for calculations when useful;
9. request the smallest useful set of bounded specialist checks only for
   genuinely independent uncertainty that improves the answer;
10. synthesize specialist memos with its own evidence;
11. write the complete answer, limitations, and useful visuals;
12. apply material business repairs, each followed by a targeted
   recheck, if review requests them.

For supported tabular work, use the analytics toolkit first: `profile_data`,
`compute_kpi_table`, `segment_customers` (deterministic k-means with optional
agglomerative comparison), and `score_segments`. Choose and preserve the exact
method and parameters. Use custom owner-authored code only for unsupported
methods and run it through `ControlledScriptRunner`. Follow
[ANALYTICS_TOOLKIT.md](ANALYTICS_TOOLKIT.md) for strict artifact JSON and the
review/integration handoff.

The owner loads exact persisted source IDs and a program-built typed identity
mapping view when applicable. It does not generate source filenames or repeat
entity-normalization logic inside a requirement script. Syntax/dependency
preflight uses the bytecode-free runner validator, not `py_compile` in the run
root.

The owner does not hand the calculation or final draft to another worker. It
does not author JSON files, pointers, hashes, lifecycle state, review packets,
integration records, or manifests. The host exposes `AnalystWorkspace` methods
and converts the owner's ordinary conclusions into deterministic artifacts.
Each item binds directly to the same `RunContext` and shared Data Room; normal
Requirement Mode does not hand off a previous item's context. The host records
the current owner in program audit state; the agent never emits internal
paths/hashes, and a replacement owner may continue any nonterminal item.

Suggested owner prompt:

```text
You are the Analytical Owner for one bounded business question. You own the
entire reasoning loop and final answer. First call `brief`, search the accepted
ontology, identity mappings, relationships, and prepared descriptors, and
select exact useful IDs with a purpose. If a needed identity domain is
`resolving`, report `waiting_on_resolution` and release your lane; the Planner
marks the paused item `ready_to_resume` only after the domain is ready. Do not
guess the join.
Load prepared rows only after selection and hash validation. Investigate the
available sources,
preserving any selected descriptor `effective_period` through the candidate
sidecar, operation manifest/hash inputs, accepted integration and registry, and
later reuse; an omitted value remains valid with no period constraint.
define the population and method, run reproducible calculations where useful,
test important joins and limitations, and write one complete decision-ready
answer. Ask only for the smallest useful set of specialist checks for genuinely
independent uncertainty, bounded by actual host capacity; do not create one
specialist per method or checklist item. Treat their memos as evidence, not as
an answer. Do not emit program state, JSON pointers,
file paths, hashes, receipts, integration records, or lifecycle instructions.
If evidence is insufficient, only you may originate a
`DataInsufficiencyConclusion` naming the unanswerable component, missing
information, searches/tests performed, evidence references, and supported
components. A reviewer can only confirm that conclusion.
```

### Entity Resolution Owner

Launch one Entity Resolution Owner when the Analytical Owner proposes a new
arbitrary real-world identity domain during scouting. The runtime reserves
that exact owner-bound proposal as `resolving`; the Planner cannot pre-reserve
it. If the current requirement needs the mapping, it waits and resumes after
commit. An accepted/integrated item may also leave a proposal for later reuse.
Do not limit the contract to example business classes. Strongly
coupled classes may share one domain. This is a parallel external job, not an
Analytical Owner specialist and not a requirement-answering role.

The owner starts from the run-level catalog, the reserved domain's source hints,
and representation IDs. It scans all rows of domain-relevant tables and relevant
documents, expanding to another catalog entry only when candidate matching or
conflict evidence requires it. It does not reread every unrelated table/document
or recompute the full archive hash for every domain. The owner may inspect
manually, write Python/SQL/scripts, use existing helpers, infer and bulk-apply
patterns when justified, test samples/coverage/exceptions/population differences,
and revise its method. Do not require row-by-row review, an authoritative
crosswalk, or a fixed matching script. Pattern rules remain run knowledge;
future helper-library audit is deferred.

Publish only a reviewed result: each proposed mapping has a binary review
decision (accepted or not accepted), and an accepted `CanonicalMapping` may
contain one or many source identities or representations, including bulk
pattern-derived populations. Keep unresolved/ambiguous records source-local
and outside canonical mappings with coverage and exceptions; do not downgrade
proven mappings. Publish the canonical class, source-account representation
classes, reviewed `IdentityDecision`/`CanonicalMapping`, identity `represents`
relationships, and a versioned mapping asset or coverage where available. The
reviewed result commits atomically before the domain becomes `ready`; owners
see only `resolving` or `ready` snapshots, never partial state.
If testing finds no deterministic mapping, publish `no_mapping_found` only with
population, coverage, unresolved records, and evidence. It contributes no
ontology or mapping records and permits source-local analysis to continue.
An unexplained zero-mapping result is a technical error before review.

Suggested resolution-owner prompt:

```text
You own the identity domain reserved below. Start with its source hints and
representation IDs; scan every row of relevant tables and relevant documents,
and expand only when matching or conflict evidence requires another source.
Reuse the run-level catalog and do not repeat a full unrelated Data Room scan.
Choose, document, test, and revise the matching methodology. You may use manual
inspection, Python, SQL, scripts, or existing helpers and may bulk-apply
justified patterns. Report samples, coverage, exceptions, population
differences, unresolved/ambiguous records, and evidence. Do not require a
crosswalk or fixed script, and do not review every row by hand. Publish only
mappings with a binary review decision; each accepted mapping may contain one
or many source identities. Keep unresolved/ambiguous records source-local.
Return the canonical class, source-account representations, IdentityDecision /
CanonicalMapping records with a binary review decision per mapping, represents
relationships, and versioned asset or coverage metadata. An accepted mapping
may contain one or many source identities, including bulk pattern-derived
populations. Do not answer the parent requirement or emit lifecycle
state, paths, hashes, or internal program records.
```

### Specialist

Use a specialist only for a separable uncertainty: data quality, metric method,
business context, process/policy, or document authority. Give it a bounded
question, selected source IDs, expected output, and only the minimum context it
needs.

Require a short memo containing:

- conclusion;
- method;
- evidence used;
- limitations and open questions;
- confidence.

The specialist must not write the final answer, broaden the question, mutate
the owner's draft, operate lifecycle state, or delegate further.

Suggested specialist prompt:

```text
Investigate only the bounded subquestion below. Return a concise evidence memo
with conclusion, method, evidence, limitations, open questions, and confidence.
Do not answer the parent business question, edit another agent's work, or emit
program schemas and paths.
```

### Independent Business Reviewer

Give the reviewer the question, the complete answer, calculations, evidence,
source selections, specialist memos, and limitations. Ask it to judge business
substance independently without becoming a second author.

For each material issue, the reviewer supplies:

- target answer section;
- semantic categories (one or more, canonical order): `answer`, `calculation`, `evidence`, `method`,
  `source_completeness`, or `presentation`;
- problem;
- evidence;
- required change.

The host records this as semantic repair provenance and authorizes item-local
updates. Once authorized, the same Analytical Owner may update any item-local
work and the affected answer section needed to resolve the finding; no
cross-item write is allowed. The reviewer never authors JSON pointers,
artifact paths, dependent paths, packet hashes, or state mutations.

Suggested reviewer prompt:

```text
Independently review whether the answer addresses the decision and whether its
material claims, calculations, population, denominator, period, units, joins,
proxies, policies, and limitations are defensible. Return all material issues
together. Identify each issue by answer section and one or more business categories, explain
the evidence, and state the required change. Do not rewrite the answer and do
not emit internal program paths, pointers, packets, hashes, or lifecycle state.
```

Use `accept` when the core requested decision is answered and only normal
disclosed limits remain. Use `accept_with_limits` only when a material requested
component is missing or unreliable; ordinary source-local, currency, or
no-causality caveats alone do not force that verdict. Semantic fidelity is
reviewed independently. Resolution review decisions are binary per mapping;
each accepted mapping may contain one or many source identities, while coverage
and exceptions remain job-level reporting.

### Result Integration Agent

Invoke this role only after the reviewed answer is accepted and immutable. It
maps accepted content into small typed claims, metrics, limitations, evidence
links, prepared assets, ontology definitions, relationships, and dashboard
facts through `IntegrationSession` APIs.

For toolkit outputs, `IntegrationSession.create/load` automatically stages the
exact sealed, business-accepted typed `AnalyticalArtifact` handoff from the
accepted bundle. Do not manually re-submit or re-declare that artifact.
Preserve its artifact ID/type, schema version, content/envelope hashes, and
requirement binding while using only the public session methods that exist; do
not invent an integration method or write integration JSON directly.

The accepted Analytical Owner answer bytes and `accepted_content_hash` are
immutable. The records staged by IntegrationSession are a derived pre-commit
projection: after fidelity authorization, the same Integration Agent must use
`correct_record` for every authorized affected record (or `remove_record` when
authorized), preserve the accepted hash and business meaning, rebuild the
fidelity packet, and submit the targeted recheck before commit. A literal
difference between normalized typed fields and the accepted prose/artifact is
expected projection work, not a semantic conflict or a reason to refuse. Never
edit the accepted answer or redo its analysis.

This is the only agent-facing role that works with typed internal integration
records. It does not repair the business answer or redo analytics. Mechanical
validation and one item-only fidelity review protect this downstream mapping.

Publish only material reusable semantics actually established: business
objects/table mappings, grain, key fields/normalization,
relationship/cardinality/coverage/date authority/limits, and truly reusable
prepared-asset descriptors. Do not publish every merge, result row, metric
observation, Japan/Spain filter, or question-specific aggregation. `no_change`
is valid only when there is no reusable semantic understanding or asset, not as
the default. The existing Integration Fidelity Reviewer checks semantic
correctness; no extra role, gate, mandatory large schema, or minimum count is
added.

The Analytical Owner establishes actual joins and relationships with
`source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
unique tested edge-pair count), `source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (endpoint count divided by its
population, with zero for a zero population), plus `as_of`/date authority,
limitations, and evidence. Integration publishes only reviewed,
tested relationships and canonical identity mappings; it does not complete a
theoretical graph or infer relationships from prose.

## Program-owned translation

The deterministic host owns these translations:

```text
owner conclusion          -> plan/evidence/draft artifacts
specialist task and memo  -> bounded task/memo records
review finding            -> semantic repair provenance and item-local authorization
accepted answer           -> integration fidelity packet
typed integration records -> LEM, prepared registry, and dashboard facts
```

Reject malformed values before lifecycle transitions. Do not try to recover a
bad structure by asking the Analytical Owner to learn internal schemas. Return
a concise correction such as “the answer section is missing” or “confidence
must be high, medium, low, or unknown.” Persisted contexts resume directly
after implementation changes.

## When to use specialists

Use no specialist when the owner can resolve the question directly. Add the
smallest useful set only when each check is a genuinely independent material
uncertainty and the host has capacity; never create one specialist per method
or checklist item, and never measure workflow quality by agent count.
