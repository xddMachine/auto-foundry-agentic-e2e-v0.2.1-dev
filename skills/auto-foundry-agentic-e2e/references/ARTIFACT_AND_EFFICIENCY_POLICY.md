# Artifact and efficiency policy

## Principle

Preserve work that materially supports a reviewed answer. The program owns
one data room/source catalog and one durable item workspace; do not manufacture
paperwork, empty folders, or per-capability artifacts.

## Always preserve

- original question or Requirement Mode record;
- structured run identity, mode, scope classification, and outcome;
- source-catalog and compact-index references used by the item;
- `AnalystWorkspace.brief()` availability counts and exact ontology/prepared
  IDs searched or selected before analysis;
- readiness/scouting decisions for ontology, identity mappings, relationships,
  and prepared semantics, including why no relevant descriptor applies;
- run-level identity-domain reservations, resolution-worker leases, resolving /
  ready transitions, and `waiting_on_resolution` / resume decisions;
- Entity Resolution Owner methodology, full-data-room scan evidence, reviewed
  binary `IdentityDecision`/`CanonicalMapping` records, canonical and
  source-account representation classes, identity `represents` relationships,
  versioned mapping assets, coverage, exceptions, and unresolved populations;
- run-local immutable semantic snapshots plus owner/item-bound
  `work/semantic_selections.jsonl` records with compact selection
  reference/hash/counts, purpose, snapshot/context bindings, and registry hash;
- authoritative `item_state.json` and mutable `work/` handoff;
- owner strategy and selected sources recorded before material calculation;
- reproducible Analytical Owner script and `ControlledScriptRunner` preflight
  checks; successful runtime receipts are `smoke` and `full`, with an optional
  second `full` receipt for deterministic comparison (a failed preflight emits
  only its `compile` or `dependency_check` receipt);
- artifact-progress before/after counts and no-progress/recovery decisions;
- evidence references, definitions, assumptions, limits, population, and
  denominator;
- material findings and any run-local prepared candidates (the accepted
  registry is populated only by the post-acceptance integration commit);
- materialized complete answer, Analytical Owner self-check, reviewer verdict,
  and iterative material business repairs with targeted rechecks;
- bounded specialist tasks/memos only when used, with owner synthesis;
- immutable accepted answer bytes plus a separate program-owned acceptance
  envelope;
- atomic reviewed Knowledge Delta result (`promoted`,
  `promoted_with_limits`, or `no_change` with a concrete reason);
- one post-acceptance Result Integration Agent receipt and incremental API refs;
- passive attempt/artifact telemetry event references;
- dashboard/product traceability and internal-link checks.

## Preserve when created or used

Keep Python, SQL, shell, notebook, spreadsheet formula, mapping, transformed
asset, chart specification, dashboard source, command, and material output
when it affects a result or improves reproducibility. Record purpose, material
inputs and outputs, assumptions, limits, and a reproduction command.
Python validation uses the public bytecode-free runner preflight; never run
`py_compile` in a run root. Analytical code reads exact persisted source IDs
and uses a typed identity mapping view instead of regenerating filenames or
repeating canonicalization. Prepared Registry entries must point to loadable
run-local assets with hash, location,
schema, grain, lineage/source IDs, scope, and effective period. Candidate bytes
and descriptors remain under the item `work/prepared/` path until accepted
integration validates exact path/hash/row/byte/scope/provenance and registers
once. Never overwrite raw evidence.

## Natural analysis trace

Use one concise trace per active item:

```text
Data-room source/member IDs and exact LEM/prepared IDs inspected
Brief availability counts, semantic/prepared descriptors searched, and exact IDs selected with purpose
Identity domains inspected/reserved, resolution status, coverage/exceptions, and waits/resumes
Plan and source map written before analysis
Catalog capabilities recommended/used (or gap), if any
Tools, specialists, scripts, and transformations used
Key decisions and working definitions
Population, denominator, and relationship measurements
Material findings, draft, and accepted-snapshot references
Artifact progress before/after each response
Execution-recovery and business-repair counts
Self-check and review result, including source-completeness/identity checks
Unresolved issues and limits
Approximate effort (optional)
```

The trace is observational input for the deterministic post-run evidence
collector. It does not control lifecycle state or stand in for a free-form
Optimization Agent report.

## Do not create

- empty directories or files;
- a folder for every capability that was not needed;
- a Navigator, descriptor/typed-validation role,
  business-repair finalizer, reviewer-of-reviewer, manual terminalizer,
  second integration reviewer, per-item catalog-compliance artifact, capability
  approval tree, or finalizer artifact;
- verifier scripts that inspect prose wording to decide state;
- repeated copies of unchanged artifacts;
- scripts created only to satisfy this policy;
- broad scans unrelated to the active item;
- central ontologies, cross-run caches, domain recipes, or business-term
  dictionaries;
- parallel question waves, wall-time deadline artifacts, or a third business
  repair request. Requirement Mode scheduling remains an event-driven Planner
  recommendation, not a new artifact family.
- partial identity snapshots, manual row-by-row review logs, mandatory
  crosswalks, fixed matching scripts, or an identity resolver masquerading as an
  owner specialist;
- filesystem no-progress recovery without a completed invocation receipt that
  proves lane/provider/host/process loss;

## Efficiency and reuse

Build the source catalog once, inventory sources once, then profile deeply only
for the active item. Keep item contexts to a small reference while the run
publishes multiple successive immutable semantic snapshots as commits or
refreshes change the semantic projection. Each distinct snapshot manifest is
stored once in the run-local namespace; each layer/index blob is stored once per
distinct canonical byte hash under `semantic_store/blobs/<sha256>.json` and
reused by every snapshot that references it. Snapshot directories contain only
the manifest. Before analysis, call
`AnalystWorkspace.brief()` and use `search_ontology()`/`search_prepared_assets()`
to inspect compact accepted descriptors; layers are loaded on demand by the
requested operation. Select exact IDs with `select_ontology()` or
`select_prepared_assets()` and record only the compact selection
reference/hash/counts and purpose. Reuse a prepared asset only
when source scope, effective period, hash/location, schema, grain, lineage,
evidence, transformations, and limits still apply; call
`load_prepared_asset()` only after selection and content-hash validation.
Create a requirement-scoped view when those boundaries do not apply.
If `effective_period` is present, preserve the exact value through the
candidate sidecar, operation manifest/hash inputs, accepted integration and
registry; omission remains valid and means no period constraint.
Requirement Mode preserves exact records and explicit user priority. One
run-level event-driven Planner receives exact records, compact physical catalog
metadata, and current item/resolution outcomes. Initial order/grouping is
advisory; it does not predeclare runtime semantic dependencies, which the
Analytical Owner discovers after understanding the requirement. It recommends
only the smallest useful set of owner specialists for genuinely independent
uncertainty, bounded by actual host capacity; zero is valid and it never
creates one specialist per method or checklist item. Its
`RequirementExecutionPlan` and
`RequirementExecutionGroup` values are revisionable scheduling recommendations,
not catalog-hash or lifecycle authority. A technical failure does not create
Planner dependency blocks; independent groups remain eligible and runtime
resolution state controls waiting and resume. Every item
binds directly to the same `RunContext` and shared Data Room, follows the
ordinary owner loop, and reuses only committed integration semantics or
prepared assets. Independent groups may run when host capacity permits; within
a group one owner remains per requirement. Capacity is adaptive to the actual
host: the scheduler leases the smallest useful set without oversubscription.
Every requirement has exactly one Analytical Owner, reviewers remain fresh and
independent, and Planner capacity is not counted or leased. Requested Run A/B
executions remain sequential.

The Planner consumes typed runtime snapshots and public next actions until the
run is terminal. It routes ordinary defects back to the same owner, appends a
canonical run incident, and only requests a core intervention when the public
substrate itself is defective.

If a needed domain is `resolving`, the owner records `waiting_on_resolution`,
releases its lane, and lets the Planner mark the earliest paused item
`ready_to_resume` only when ready. If all
runnable items wait, the owner lane sleeps while resolvers progress; block only
when nothing is runnable and no resolver progresses.

Result Integration publishes material reusable semantics actually established:
business objects/table mappings, grain, key fields/normalization,
relationship/cardinality/coverage/date authority/limits, and truly reusable
prepared descriptors. It does not promote every merge, result row, metric
observation, Japan/Spain filter, or question-specific aggregation. `no_change`
is reserved for an accepted item with no reusable semantic understanding or
asset, with a concrete reason; it is not the default. The current
Integration Fidelity Reviewer checks semantic correctness without adding a
role, gate, mandatory large schema, or minimum count.

The Analytical Owner records actual joins/relationships with
`source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
unique tested edge-pair count), `source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (endpoint count divided by its
population, with zero for a zero population), plus `as_of`/date authority,
limitations, and evidence. Integration publishes only reviewed tested
relationships and canonical identity mappings; it never completes a theoretical
graph or infers joins from prose. The review decision is binary per proposed
mapping (accepted or not accepted); each accepted mapping may contain one or
many source identities or representations, including bulk pattern-derived
populations. Coverage and exceptions remain job-level evidence without
downgrading proven mappings.

The inventory counters distinguish the one initial full bind, child-context
loads without re-inventory, selected-member verification, and an explicit final
`verify_source_full()` mutation check. Opaque members remain opaque and may be
copied only through safe explicit materialization. `ControlledScriptRunner`
uses a configurable 3600-second process guard by default; it is not a workflow
or agent reasoning deadline.

Entity Resolution follows the same rule: scan every row of domain-relevant
tables and relevant documents selected from the reservation hints, then expand
only for a concrete matching or conflict question. Do not repeat a full scan of
unrelated members or `verify_source_full()` for each identity domain.

## Reproducibility

For a material or repeated calculation, prefer preserved code. For a simple
calculation, record input references, formula, output, exclusions, and units.
Catalog capabilities may be recommended/used internally, but custom code is
valid when it is the clearest fit and remains reproducible. The program—not
custom question code—validates and applies reviewed Knowledge Delta records.

## Telemetry and privacy

Telemetry is append-only and passive. For each material attempt/artifact event,
store invocation lane/role/route, start/end/status/error when available,
artifact before/after/counts, execution-recovery and business-repair counts,
source/member reads, core/cache facts, literal provider/model/host/process
identity (`unavailable` when unknown), terminal reason class, and artifact
IDs. Include run-level physical-inventory operation names/counters and
canonical recovery receipt reference/hash when present—not raw business rows,
secrets, tokens, or unnecessary personal data. Do
not use telemetry to invent timing benchmarks, select a route, or alter an
answer.

## Optimizer boundary

The evidence collector may read frozen traces, telemetry, and product manifests
only after accepted snapshots and the whole-run freeze are complete. Its
evidence bundle and appendix are new read-only artifacts; it cannot mutate
code, state, LEM, prepared data, products, source files, or configuration. A
later Optimization Agent may write a separate report from that bundle, but
collector/agent failure is non-blocking. Client-business automation is out of
scope. Implementation versions are audit metadata, not reuse or workflow
locks.
