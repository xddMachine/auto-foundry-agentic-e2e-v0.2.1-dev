# Auto Foundry Agentic E2E v0.7.1 / core v0.8.0

This repository contains the v0.7.1 reviewed-analysis skill and the
source-agnostic, deterministic `auto_foundry_core` v0.8.0 substrate. Its two
user-visible goals are strong evidence-backed analysis and a clear offline
dashboard. The core exists to give analytical agents bounded data access,
reproducible execution, program-owned serialization, review scope, integration,
and recovery without making them learn internal JSON or lifecycle formats.

The normal path is one immutable `RunContext` passed to one
`DataRoomWorkbench` and one `ItemWorkspace` per active item. `CoreRuntime`
remains available for deterministic operations:

```text
RunContext
  -> DataRoomWorkbench(context, archive_path) -> catalog/search/read
  -> immutable BoundAnalysisContext
  -> AnalystWorkspace -> one Analytical Owner
       -> investigate -> calculate -> interpret -> complete answer
       -> adaptive, evidence-triggered specialist checks (possibly none)
       -> controlled script receipts and prepared candidates
  -> one Independent Business Reviewer -> semantic findings
  -> BusinessReviewAdapter -> iterative material repairs/rechecks
  -> immutable accepted snapshot and reloadable state
  -> one typed Result Integration Agent + item-only fidelity review
  -> reviewed fixture -> products dashboard
  -> frozen evidence collector -> terminal run export
```

The public entry points include `RunContext`, `DataRoom`,
`DataRoomWorkbench`, `DataRoomMember`, `DataRoomCatalogEntry`,
`PreparedAsset`, `PreparedAssetRegistry`, `CatalogSnapshot`,
`BoundAnalysisContext`, `ControlledScriptRunner`, `ItemWorkspace`,
`AnalystWorkspace`, `AnalystAnswer`, `EvidenceNote`, `SpecialistTask`,
`SpecialistMemo`, `ReviewFinding`, `BusinessReviewAdapter`,
`ArtifactProgress`, `ProgressDecision`, `ExecutionAttempt`, `AcceptedSnapshot`,
`AcceptedAnalysisBundle`, `IntegrationSession`, `RunLifecycle`,
`AgentInvocationReceipt`, `FreezeMarkers`, `CoreRuntime`, `CoreExecutionResult`,
`LEMRef`, `LivingEnterpriseModelProjector`, and the immutable contracts
exported from `auto_foundry_core`. The
old mutable `Workspace` facade is not part of the package API. One Analytical
Owner owns interpretation, source strategy, calculation, synthesis, and final
answer. Specialists are bounded evidence spokes. The Business Reviewer returns
answer-section/category-set findings as semantic repair provenance; the program
authorizes item-local updates without exposing internal pointers or paths. Only
the post-acceptance Result Integration Agent works with typed internal records.

Before analysis, the owner checks `AnalystWorkspace.brief()` and searches the
compact accepted semantic graph and prepared descriptors with
`search_ontology()` and `search_prepared_assets()`. Useful exact IDs are
selected with `select_ontology()` or `select_prepared_assets()` and a stated
purpose; the item-bound trace is `work/semantic_selections.jsonl`. Prepared
rows are loaded only after selection and registry location/schema/lineage/
content-hash validation through `load_prepared_asset()`.

Semantic reuse is published in a content-addressed semantic store under each
run. A run may publish multiple successive immutable snapshots as commits or
refreshes change the semantic projection. Each distinct snapshot manifest is
stored once in the run-local namespace; each layer/index blob is stored once per
distinct canonical byte hash under `semantic_store/blobs/<sha256>.json` and
reused by every snapshot that references it. Snapshot directories contain only
their canonical manifest. Each item context keeps only a snapshot reference and
counts; semantic layers and exact selection ID sets are loaded on demand, while
the item-bound `semantic_selections.jsonl` record remains a compact
reference/decision trace.

Prepared candidates are atomically written below the current item's
`work/prepared/` directory. They are not `PreparedAssetRegistry` entries until
the accepted Result Integration commit validates the exact candidate path,
content hash, byte/row counts, scope, and provenance. Registration is
accepted-only, retains the recorded scope, is idempotent on exact retries, and
leaves no accepted entry for a rejected item or an item with an integration
technical failure. Mechanical validation cannot prove semantic completeness.
Exactly one fresh, item-only Integration Fidelity Reviewer checks the staged
candidate after mechanical validation and before commit; the same Result
Integration Agent patches only affected records and receives one targeted
recheck.

An optional prepared-data `effective_period` is carried unchanged through the
descriptor and candidate sidecar, operation manifest/hash inputs, accepted
integration record, registry entry, and later search/select/reuse. Omitted
`effective_period` remains valid and means no period constraint; never infer a
period from the current date.

Requirement Mode begins with an owner readiness/scouting pass: explicitly
search/select ontology, identity mappings, relationships, and prepared
semantics, or record why none is relevant. If a needed identity domain is
`resolving`, the owner reports `waiting_on_resolution` and releases its lane;
the Planner skips to the next original-order runnable item and marks the
earliest paused item `ready_to_resume` when the domain is `ready`. If all runnable items wait,
the owner lane sleeps while active resolvers progress. Block only when nothing
is runnable and no resolver can progress.

The host obtains that decision from one
`RequirementSupervisorWorkspace.scheduling_tick()` call after each wait,
resume, or terminal transition; the call also reconciles aggregate run status.

Committed integration records are the durable LEM authority. Each
`IntegrationSession` rebuilds the cumulative run-local LEM in lifecycle order
from validated prior commits; callers and agents never supply, restore, or
hand-edit a cumulative model. A missing, reordered, or tampered prior commit
fails before staging the next item.

The Business Reviewer never authors internal repair paths. It identifies the
answer section and one or more business categories plus problem, evidence, and
required change. The program keeps each finding as semantic repair provenance
and authorizes item-local updates. Once authorized, the same Analytical Owner
may update any item-local work and the affected answer section needed to resolve
the finding; cross-item writes fail closed. Internal paths and hashes stay
program-owned.

The current business-review verdicts are `accept`, `accept_with_limits`,
`repair_once`, and `confirm_data_insufficiency`. Material repairs may repeat,
each followed by a targeted recheck; there is no arbitrary repair budget or
same-owner lock. Only the Analytical Owner may originate a
`DataInsufficiencyConclusion`; `blocked_by_evidence` is valid only after the
reviewer confirms it. Other defects repair or technical-fail. Use `accept` when
the core decision is answered and only normal disclosed limits remain; use
`accept_with_limits` only when a material requested component is missing or
unreliable. Source-local, currency, or no-causality caveats alone do not force
with-limits. Semantic fidelity is independent; resolution mapping status is
binary per proposed mapping (accepted or not accepted); each accepted mapping
may contain one or many source identities or representations, while
coverage/exceptions remain job-level evidence.

Requirement Mode admits one run-level **Planner**, an event-driven control plane
and cognitive scheduler. It receives the exact `RequirementRecord` values,
compact physical catalog metadata, and current item/resolution outcomes. Initial
order and grouping are advisory and preserve explicit user priority/order; the
Planner never declares runtime semantic dependencies. The Analytical Owner
discovers those after understanding the requirement, and the runtime
`waiting_on_resolution`/`ready_to_resume` ledger is the sole semantic block.
The Planner may admit only the smallest useful set of owner-specialist checks
when the Analytical Owner identifies genuine uncertainty; zero specialists is
valid, and no specialist is created per method or checklist item. Specialists
do not calculate or write the owner answer, and the Planner is not a
deterministic ID/hash or lifecycle authority.
`RequirementExecutionPlan` and `RequirementExecutionGroup` are revisionable
scheduling recommendations, not catalog-hash or lifecycle authority. A
technical failure does not create Planner dependency blocks; independent groups
remain eligible and runtime resolution state controls waiting and resume.

Each requirement has exactly one Analytical Owner. Specialist checks are
admitted adaptively only for genuine uncertainty and are bounded by the actual
host capacity; zero specialists is valid. Entity-resolution and analytical
workers share that runtime ceiling, while the Planner is not counted. There is
no fixed role split or default worker total, and scheduling never oversubscribes
the host. Requested Run A and Run B executions remain sequential.

Every Requirement Mode item binds directly to the same `RunContext` and shared
`DataRoomWorkbench`; no previous item context, transition, rebind, inheritance,
or preserved-hash handoff is used in normal mode. Only committed integration
semantics and prepared assets selected through `AnalystWorkspace` are reused.

The Planner consumes typed `runtime_snapshot()` and `next_actions()` views and
dispatches the named AO, resolver, reviewer, integration, or product role until
the run is terminal. Waiting identity work does not block later runnable
requirements. Ordinary code/API/artifact errors return to the same owning role
and every intervention is appended canonically through `record_incident()`.
Each item creates an item-local `RequirementAnalysisPlan` and follows the
ordinary loop: analysis, review, iterative material repairs, accept or
`technical_failure`, then integration. Within a group, one Analytical Owner
remains per requirement; bounded shared investigation may be reused and
independent groups may run when host capacity permits. Analytical roles never
receive internal paths or hashes.

When an Analytical Owner proposes a new arbitrary real-world identity domain
during scouting, the runtime reserves that exact owner-bound proposal as
`resolving` and launches an Entity Resolution Owner. The Planner cannot invent
or pre-reserve domains. A current requirement may wait for the commit; an
accepted/integrated item may also leave a proposal for later reuse. Domain scope is not a hardcoded Supplier/Factory/Order list;
strongly coupled classes may share a domain. The owner scans every row of the
domain-relevant tables and relevant documents selected from reservation hints,
expanding only for concrete matching/conflict evidence. It reuses the run-level
catalog instead of rescanning unrelated members, owns methodology, may inspect
manually or write Python/SQL/scripts/use helpers, infer and bulk-apply justified patterns, test samples/coverage/
exceptions/population differences, and revise the method. Do not require
row-by-row review, an authoritative crosswalk, or a fixed matching script;
pattern rules remain run knowledge and future helper-library audit is deferred.

The review decision is binary per proposed mapping (accepted or not accepted).
Each accepted `CanonicalMapping` may contain one or many source identities or
representations, including bulk pattern-derived populations. Unresolved/
ambiguous records stay source-local and outside canonical mappings with
coverage and exceptions preserved; accepted mappings are not downgraded. A
ready snapshot publishes the canonical class, source-account representation
classes, reviewed `IdentityDecision`/`CanonicalMapping`, identity `represents`
relationships, and a versioned mapping asset/coverage where available. The
ready snapshot is exposed only after an atomic reviewed commit. Owners see only
resolving or ready snapshots, never partial mappings. Entity-resolution jobs are parallel
external jobs, not extra specialists and never answer requirements.

A zero-mapping result is valid only as explicit `no_mapping_found` evidence
with population, coverage, unresolved records, and evidence references. It
publishes no ontology or mapping records and allows source-local analysis to
continue. Unexplained empty results fail before review. Every Requirement Mode
calculation likewise requires a current semantic scope decision, including an
explicit `no_reuse_reason` on the first empty snapshot.

The ontology is a compact enterprise map of stable objects, identities,
aliases, sources, documents, processes, definitions, rules, relationships,
limitations, and reusable metric definitions. Current counts, shares, amounts,
values, rankings, top-N rows, and dimensional observations remain accepted
results, claims, dashboard facts, evidence, or prepared assets; `add_metric`
records an observation and never promotes it into ontology.
`add_current_observation()` publishes a current measured value only as a
dashboard fact, with `observation_as_of()` deriving authority from observed
timestamps rather than due/target dates. Repeated shapes may yield advisory
promotion suggestions, but they never mutate or gate the ontology.

Analytical scripts can consume exact selected source IDs and a materialized
`IdentityMappingView`; they do not regenerate filenames or repeat entity
matching. Mapping completeness is reported as a non-gating advisory. Entity
resolution validates the full typed candidate before review, so technical
correction stays with the same resolver and does not consume business repair.

Result Integration publishes material reusable semantics actually established:
business objects/table mappings, grain, key fields/normalization,
relationship/cardinality/coverage/date authority/limits, and truly reusable
prepared descriptors. It does not publish every merge, result row, metric
observation, Japan/Spain filter, or question-specific aggregation.
`no_change` is valid only when no reusable semantic understanding or asset was
established, with a concrete reason. For Q1→Q2/Q9, Q1 can publish
order-header/order-line/delivery/customer/material objects and relationships
plus an order-fulfillment core; Q2 and Q9 search/select/load those IDs and
compute their own requirement-specific measures.

The Analytical Owner establishes actual joins and relationships and records
`source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
number of unique tested edge pairs), `source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (each matched endpoint count divided
by its population; zero when that population is zero), plus `as_of`/date
authority, limitations, and evidence. Integration publishes only reviewed
tested relationships plus canonical identity mappings; it never completes a
theoretical graph or infers a relationship from prose.

The run binds one physical source inventory and records bounded counters for
full archive binding, member hashes, selected-member reads, and catalog
creation/reuse/load. Bound child contexts reuse that inventory rather than
re-inventorying it; an explicit final `verify_source_full()` check detects a
late source mutation. Opaque members may be copied only through the explicit
safe materialization API and are never semantically parsed. The controlled
script runner uses an explicit, configurable 3600-second process timeout by
default; this guard is not an agent reasoning or workflow wall-time deadline.
Invocation receipts remain audit evidence, not permission to continue. A new
agent, attempt, or implementation may reopen the item and keep working from the
same durable business artifacts. Catalog hashes still detect corrupted or
changed source data, but code and skill versions never lock an analytical
context.

Filesystem references are explicit. `DataAssetRef` and `OperationResultRef`
serialize with the reserved `__auto_foundry_ref__` discriminator (`data_asset`
or `operation_result`); typed source slots also accept a string path, `Path`,
or `DataAssetRef`. Other mappings are analytical data by default, including
records with `location`, `uri`, `path`, `filename`, or `content_hash` fields.

This is an experimental release candidate, not a production-hardened
sandbox. A Coding Agent with unrestricted host shell/filesystem access cannot
be fully sandboxed by this Python package. True isolation requires a separate
workspace/container or host allowlist.

The install-ready skill tree is under
`skills/auto-foundry-agentic-e2e/`. Its stdlib-only dashboard helper renders
already-reviewed widget specifications as a multi-page, chart-led local site:
a short overview, focused business-domain pages, ontology, and evidence/audit.
Detail tables are collapsed and all page/trace links are validated. The helper
also retains a compatible single-page micro-product mode. Its development-only optimizer observes frozen run-local
telemetry/traces/scripts, verifies input hashes, and writes only two report
files; it rejects client-business-automation classifications.

See [installation and migration](docs/INSTALLATION_AND_MIGRATION.md),
[implementation summary](docs/IMPLEMENTATION_SUMMARY.md), and the
[model-call ledger](docs/MODEL_CALL_LEDGER.md). Benchmark A is prepared but
not executed: see [benchmarks/benchmark_a](benchmarks/benchmark_a/README.md).

## Editing and continuously running Requirement Mode

A requirement portfolio is editable in every run state. Add a requirement,
replace its text, remove it, reorder the portfolio, temporarily make it empty,
or reopen a completed run. A revision preserves unchanged item workspaces and
archives changed or removed item histories under `history/requirements/`.

The shortest programmatic add is:

```python
extension = RequirementRunExtension.append(
    context,
    new_records,
)
```

For arbitrary add/update/remove operations, save a revised
`RequirementExecutionPlan` through `RequirementSupervisorWorkspace.save()` or
use the CLI:

```bash
python3 -m auto_foundry_core.cli lifecycle --run-root <run> pause --reason "inspect"
python3 -m auto_foundry_core.cli requirements --run-root <run> add --record req.json
python3 -m auto_foundry_core.cli requirements --run-root <run> update --record req.json
python3 -m auto_foundry_core.cli requirements --run-root <run> remove REQ-03
python3 -m auto_foundry_core.cli lifecycle --run-root <run> resume
python3 -m auto_foundry_core.cli lifecycle --run-root <run> reopen
```

Each revision is a new durable generation, but generations are history rather
than compatibility locks. Unchanged accepted conclusions, committed ontology,
relationships, and prepared assets remain reusable across revisions and code
versions. Content hashes protect bytes and detect source drift; they do not
authorize agents or freeze workflow state.

`LocalRunAutopilot` can stay alive while a run is idle or complete and will
notice a later resume or portfolio revision on its next tick:

```bash
python3 -m auto_foundry_core.cli autopilot --run-root <run> watch \
  --dispatch-command ./dispatch-planner-action
```

For a completed generation whose dashboard should be refreshed, use the
generation delta command documented in the skill workflow:

```bash
python3 skills/auto-foundry-agentic-e2e/scripts/dashboard_delta_assembler.py \
  --run-root <run-root> --run-id <run-id> \
  --parent-receipt products/parent-dashboard/build_receipt.json \
  --route route.json
```

## Offline verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/integration
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q
python3 scripts/package_release.py
python3 scripts/validate_release.py
```

The complete offline vertical proofs are
`tests/integration/test_vertical_acceptance.py`,
`tests/integration/test_workbench_durable_vertical.py`, and
`tests/integration/test_v023_normal_path.py`; together they use
generic local fixtures, real workbench/durable/cache/telemetry/filesystem
wiring, and no model or network call. When those proofs and the full offline
 suite pass, the candidate status is **v0.7.1 / core 0.8.0 — offline program
validation complete for later Benchmark A**. Benchmark A remains prepared but
unexecuted in this repository; no run is claimed here.

The package script creates ignored local artifacts under `dist/`; no command
in this repository pushes or publishes them. Start a fresh Codex task after
changing or replacing the same-name skill so discovery is refreshed.
