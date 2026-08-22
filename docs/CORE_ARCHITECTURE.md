# `auto_foundry_core` 0.8.0

`auto_foundry_core` is a small offline, source-agnostic deterministic substrate
for local analytics and durable item execution. It is intentionally not an
orchestration service and does not contain domain recipes, model calls, remote
adapters, or cross-run state. Requirement Mode's event-driven Planner and
external resolution jobs are host-level control-plane integrations around this
substrate, not a hidden orchestration service inside the deterministic core.

## Layers

- `contracts.py` contains immutable source references and JSON-serializable
  operation, identity, ontology, prepared-asset, capability, and telemetry
  records.
- `workspace.py` provides the immutable `RunContext` and clean-room allowed
  roots before a source is read or a derived output is written. `workbench.py`
  provides the program-owned read-only data room, one immutable physical
  catalog at `data_room/catalogs/<catalog_key>.json`, derived sample/category
  views, archive/member hash checks, and atomic item-local prepared candidates.
  `prepared.py` durably registers accepted candidates only and separates
  scope/reuse visibility; a staged candidate is not registry state.
  `durable.py` provides `ItemWorkspace`, artifact progress, execution
  attempts/recovery, review, and immutable accepted/technical-failure
  snapshots under `accepted/answer_content.json` plus a separate
  `acceptance_envelope.json`. These helpers do not make semantic business
  judgments or invoke model threads.
- `sources.py` provides read-only bounded CSV/TSV, JSON/JSONL, Excel, Parquet,
  and text registration, discovery, hashing, and previews.  Excel uses
  `openpyxl` and Parquet uses `pyarrow` only when those optional dependencies are
  available.
- `profiling.py`, `normalization.py`, `identity.py`, `relationships.py`,
  `populations.py`, and `aggregation.py` implement bounded generic mechanics.
  Normalization appends derived columns and parse failures; raw values are never
  replaced.  Identity candidates provide evidence and contradictions but never
  merge objects from a string score alone.
- `artifacts.py`, `reproduction.py`, `cache.py`, and `telemetry.py` handle
  deterministic output hashes, manifests, lifecycle-independent comparison,
  immutable run-local cache entries, and passive facts-only observation.
- `runtime.py` exposes the normal `CoreRuntime` path. It resolves paths,
  computes deterministic input hashes, checks the run-local cache, dispatches a
  catalog capability, records an `OperationReceipt`, emits passive telemetry,
  and returns a `CoreExecutionResult`.
- `analysis.py` binds one source/catalog/item/LEM snapshot in a hash-validated
  `BoundAnalysisContext` and performs compile/dependency preflight checks before
  emitting `smoke` and `full` runtime receipts (plus an optional second `full`
  receipt for deterministic comparison). A failed preflight emits whichever
  failure receipt phase applies: `compile` or `dependency_check`. It is a
  path/process boundary, not a hostile-code security sandbox; its explicit
  configurable process timeout defaults to 3600 seconds and is not an agent
  reasoning or workflow wall-time deadline. `validate_script()` performs
  bytecode-free AST/dependency preflight, and exact selected source IDs are
  loadable from the persisted source map. Use an OS/container boundary for
  hostile code.
- `analyst_workspace.py` is the model-free, business-shaped facade presented to
  the Analytical Owner. It exposes catalog search/sample/category inspection,
  planning, selected sources, evidence notes, bounded specialist tasks/memos,
  controlled analysis execution, complete answer submission, and semantic
  business review. Before analysis, `brief()` exposes compact ontology,
  relationship, and prepared-asset availability. `search_ontology()` and
  `search_prepared_assets()` return descriptors; `select_ontology()` and
  `select_prepared_assets()` record exact IDs and purpose in the item-bound
  `work/semantic_selections.jsonl` trace. `load_prepared_asset()` returns rows
  only after exact selection and registry content-hash validation. It validates
  strict JSON and translates ordinary analytical values into `ItemWorkspace`
  artifacts; analytical roles do not construct paths, hashes, lifecycle
  records, or review packets.
- `mapping_view.py` projects selected reviewed identity decisions and canonical
  mappings into one compact script-facing view. Unique identities resolve to a
  canonical ID; ambiguous identities remain unresolved. Entity resolution also
  exposes a non-gating completeness advisory, while `submit_result()` runs full
  typed projection validation before independent review.
- `requirement_planning.py` exposes one run-level event-driven **Planner** for
  Requirement Mode. It is a cognitive scheduler/control-plane boundary, not a
  second analytical owner. Initial order/grouping is advisory; it preserves
  explicit user priority/order; it never declares runtime semantic
  dependencies. The Analytical Owner discovers those after understanding a
  requirement, and the runtime `waiting_on_resolution`/`ready_to_resume` ledger
  is the sole semantic block. `RequirementExecutionPlan` and
  `RequirementExecutionGroup` remain revisionable scheduling recommendations,
  separate from catalog hashes and lifecycle state. The Planner does not
  calculate, write answers, or emit lifecycle authority. A technical failure
  does not create Planner dependency blocks; independent groups remain eligible
  and runtime resolution state controls waiting and resume.
- `entity_resolution.py` owns the run-level identity-domain workspace. When an
  Analytical Owner proposes a new arbitrary real-world identity domain during
  scouting, the runtime may reserve that exact owner-bound proposal as
  `resolving` and launch an Entity Resolution Owner; the Planner cannot invent
  or pre-reserve it. A current item may wait for the commit, while an accepted
  item may leave a proposal for later reuse. The default capacity is four entity-resolution workers, one Analytical
  Owner, up to three owner specialists, and eight active workers total; the
  Planner is not counted or leased. Host configuration may lower or raise
  limits, but scheduling never oversubscribes actual host capacity. Requested
  Run A and Run B executions remain sequential.
- The Entity Resolution Owner scans all rows of domain-relevant tables and
  relevant documents selected from reservation hints, expanding only when
  matching/conflict evidence requires it. It reuses the run-level catalog
  rather than rescanning unrelated members and owns methodology. It may inspect manually, write Python/SQL/scripts, use existing
  helpers, infer and bulk-apply justified patterns, test samples/coverage/
  exceptions/population differences, and revise its method. Manual row-by-row
  review, an authoritative crosswalk, and a fixed matching script are not
  required. The review decision is binary per proposed mapping (accepted or not
  accepted); one accepted `CanonicalMapping` may contain one or many source
  identities or representations, including bulk pattern-derived populations.
  Unresolved or ambiguous records remain source-local/outside canonical
  mappings with coverage and exceptions, without downgrading proven mappings. A
  ready publication contains the canonical class, source-account representation
  classes, reviewed `IdentityDecision`/`CanonicalMapping`, identity
  `represents` relationships, and a versioned mapping asset/coverage where
  available. The ready snapshot is exposed only after an atomic reviewed
  commit. Owners see only resolving/ready snapshots, never partial state.
  A proven `no_mapping_found` result must include population, coverage,
  unresolved records, and evidence and publishes no semantic records;
  unexplained zero-mapping results fail before review.
  Pattern rules remain run knowledge; future
  helper-library audit is deferred.
- Each Analytical Owner begins with a readiness/scouting pass that explicitly
  searches/selects ontology, identity mappings, relationships, and prepared
  semantics, or records why none applies. This current-snapshot decision is
  required before both analysis planning and calculation even when the store
  is empty. If a needed domain is `resolving`,
  the owner reports `waiting_on_resolution`, releases its lane, and the Planner
  skips to the next original-order runnable item. When the domain is `ready`,
  the Planner marks the earliest paused item `ready_to_resume` and resumes it.
  One public `RequirementSupervisorWorkspace.scheduling_tick()` joins those
  item/runtime states, owner capacity, and aggregate run lifecycle after each
  event. `runtime_snapshot()` exposes that state as a typed value,
  `next_actions()` names the role action to dispatch, and
  `record_incident()` keeps coordinator interventions canonical and durable.
  If all runnable items wait, the owner lane
  sleeps while active resolvers progress; block only when nothing is runnable
  and no resolver can progress.
- Every requirement binds directly to the same `RunContext` and shared data
  room. Each item gets one item-local `RequirementAnalysisPlan` with 1..N
  internal `RequirementAnalysisTask` values and follows the ordinary loop:
  analysis, review, iterative material repairs, accept or
  `technical_failure`, then integration. Within a group there is one Analytical
  Owner per requirement; bounded shared investigation may be reused and
  independent groups may run when host capacity permits. Tasks do not create
  child lifecycle workspaces or agents. Reviewer findings remain semantic
  repair provenance; once authorized, the same owner may update item-local work
  and the affected answer section, with no cross-item writes. Business review
  exposes only `accept`, `accept_with_limits`, `repair_once`, and
  `confirm_data_insufficiency`; each material repair receives a targeted
  recheck. Only an owner-originated `DataInsufficiencyConclusion` confirmed by
  the reviewer can publish `blocked_by_evidence`.
- `integration.py` owns one Result Integration `IntegrationSession` per item.
  It stages typed claim/metric/limitation/evidence/prepared/ontology/
  relationship/dashboard records under `integration/staging/` and atomically
  commits `integration/committed/records.jsonl` plus `manifest.json` without
  parsing analytical prose. Typed record APIs require explicit mapping
  payloads and reject opaque strings. Prepared candidates are preflighted without
  registry mutation and registered exactly once at accepted commit; the durable
  intent makes crash retries converge. Mechanical checks cannot prove semantic
  completeness. Exactly one fresh item-only Integration Fidelity Reviewer runs
  after mechanical validation and before commit; the same Result Integration
  Agent may make one targeted repair and receives one targeted recheck. There
  is no prose parser, semantic compiler, or reviewer chain.
  `add_current_observation()` stores current measured values only as
  `dashboard_fact` records, never ontology definitions; `observation_as_of()`
  uses observed timestamps rather than due/target dates. Repeated semantic
  shapes can generate advisory suggestions, but cannot mutate or gate the LEM.
- `lem_projection.py` rebuilds the cumulative run-local LEM as a deterministic
  read-only projection of validated committed integration records in lifecycle
  order. Committed records remain the sole durable authority; no caller-owned
  LEM, second checkpoint, or recovery journal can drift from item commits.
- Semantic reuse remains compact and item-bound. Accepted prior snapshots carry
  ontology/relationship descriptors and reusable prepared descriptors, not
  rows or current metrics. Result Integration publishes material established
  business objects/table mappings, grain, key fields/normalization,
  relationship/cardinality/coverage/date authority/limits, and truly reusable
  prepared assets. It does not publish every merge, result row, metric
  observation, Japan/Spain filter, or question-specific aggregation. `no_change`
  is reserved for an accepted item with no reusable semantic understanding or
  asset and a concrete reason; the current item-only Integration Fidelity
  Reviewer checks semantic correctness without adding roles, gates, or minimum
  counts.

The run may publish multiple successive immutable content-addressed semantic
snapshots under `semantic_store/` as commits or refreshes change the semantic
projection. Each distinct snapshot manifest is stored once in the run-local
namespace; each layer/index blob is stored once per distinct canonical byte
hash under `semantic_store/blobs/<sha256>.json` and reused by every snapshot
that references it. Snapshot directories contain only the manifest. Item
`analysis_context.json` files keep only a manifest reference/hash/counts.
Semantic layers are loaded on demand by the requested search or selection, and
exact selection IDs are stored once in a compact content-addressed selection
asset.
  Optional prepared-data `effective_period` is propagated unchanged through
  the descriptor, candidate sidecar, operation manifest/hash inputs, accepted
  integration record, registry entry, and later reuse. Omission remains valid
  and means no period constraint.
- Analytical Owners establish actual business joins/relationships with
  `source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
  unique tested edge-pair count), `source_population`/`target_population`,
  `matched_source_count`/`matched_target_count` (distinct matched endpoints),
  and `source_coverage`/`target_coverage` (endpoint count divided by its
  population, with zero for a zero population), plus `as_of`/date authority,
  limitations, and evidence. Result Integration publishes only
  reviewed tested relationships and canonical identity mappings; it never
  completes a theoretical graph or infers joins from prose. Resolution mapping
  review decisions are binary per mapping while each accepted mapping may
  contain one or many source identities and coverage/exceptions remain job-level
  evidence. Semantic fidelity is reviewed independently, and `accept` is used
  when the core decision is answered with normal disclosed limits; use
  `accept_with_limits` only for a material missing or unreliable requested
  component.
- `BusinessReviewAdapter` accepts answer sections and business categories from
  the reviewer as semantic repair provenance. The same owner may update any
  affected item-local work and answer section after authorization; the
  reviewer never authors paths or hashes, and cross-item mutations fail closed.
- In Requirement Mode each item binds directly to the same run context and
  shared data room. Persisted contexts load after core/skill changes without a
  transition or rebind ceremony. Accepted conclusions, committed semantics,
  relationships, and prepared assets remain reusable. Plans and requirement
  membership may be revised while running, paused, or complete.
- `lifecycle.py` owns run-level `RunLifecycle` state, durable
  `AgentInvocationReceipt` ledgers, and terminalization;
  `product_contracts.py` owns exact nested `freeze_markers` and singular
  `decision_flow` product validation. `reporting.py` projects cumulative
  outcomes, record-kind/registry/LEM/receipt/timing/incident totals and writes
  an atomic report, manifest, and non-circular terminalization receipt.
- `enterprise_model.py` stores a run-local extensible ontology and prepared-data
  registry.  Accepted `KnowledgeDelta` values are applied atomically and
  conflicts/supersession are retained.
- `capabilities.py` is the executable metadata source.  `catalog.py` generates
  the discoverable catalog and `cli.py` exposes catalog list/search/describe and
  deterministic execution.

## Dependency decision

The required install has no third-party dependencies so it can be built and
tested offline. Optional `openpyxl` and `pyarrow` extras are imported lazily
for bounded Excel and Parquet reads. Date normalization uses standard-library
ISO parsing plus caller-supplied ordered `strptime` formats; it has no hidden
host parser dependency. The optional readers are listed as `io` extras in
`pyproject.toml`.
The implementation uses the standard library for contracts, hashing, CSV/JSON,
serialization, caching, and aggregation rather than adding a framework whose
validation or execution surface would be larger than this core.

## CLI

```text
python -m auto_foundry_core catalog list
python -m auto_foundry_core catalog search "coverage"
python -m auto_foundry_core catalog describe relationships.measure
python -m auto_foundry_core run sources.preview --spec operation.json --run-root ./run --input-root ./workspace --output out/
```

The integrated API is the equivalent of:

```python
from auto_foundry_core import DataRoomWorkbench, ItemWorkspace, CoreRuntime, OperationSpec, RunContext

context = RunContext("RUN-example", run_root, (input_root,))
archive_path = input_root / "supplied-fixture.zip"
workbench = DataRoomWorkbench(context, archive_path)
item = ItemWorkspace.create(context, "Q-001", original_text="Count supplied rows")
runtime = CoreRuntime(context)
execution = runtime.execute(OperationSpec("sources.preview", parameters={"path": "rows.json", "limit": 20}))
```

The program workbench owns physical source/member selection, path/hash
containment, cataloging, durable artifacts, progress, and recovery. The
Analytical Owner owns semantic interpretation, definitions, evidence
sufficiency, and the useful analytical route; the core never substitutes for
that judgment.

An operation spec is JSON with `capability_id`, optional `inputs`, and
`parameters`. The CLI requires one run root and optional repeatable input roots.
It validates the spec path, output directory, and `result.json` destination
against the same context before reading or creating anything; spec-embedded
root declarations cannot broaden the CLI roots and are ignored. When a
filesystem path is used, the effective roots are propagated through every
source read, hash, reproduction comparison, and derived write. Execution-facing
catalog operations and public manifest or reproduction path hashing require
explicit `Path`/reference values or an explicit serialized reference mapping.
The reserved `__auto_foundry_ref__` discriminator has values `data_asset` and
`operation_result`; a `DataAssetRef` is accepted directly in a typed source
slot. Arbitrary analytical mappings remain data even when they contain fields
named `location`, `uri`, `path`, `filename`, or `content_hash`.
Direct low-level source readers and hashing helpers remain usable without roots
for callers that explicitly operate outside clean-room mode.  The CLI writes
only explicitly requested derived output.  Cache roots are supplied by callers
and are never shared implicitly between runs.

## Offline vertical proofs

`tests/integration/test_vertical_acceptance.py`,
`tests/integration/test_workbench_durable_vertical.py`, and
`tests/integration/test_v023_normal_path.py` exercise the complete offline
paths, including bound script execution, accepted-byte/envelope separation,
candidate-to-accepted prepared registration, lifecycle barriers, strict
product markers, physical-inventory counters, safe opaque materialization,
and optimizer evidence. All fixtures use no model or network call.

The candidate is labelled **v0.7.1 / core 0.8.0 — offline program validation
complete for later Benchmark A** only when these vertical proofs and the full
offline suite pass. Benchmark A is not run here.
This remains an experimental release candidate, not a production-hardened
sandbox.

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully sandboxed by this Python package. True isolation requires a separate workspace/container or host allowlist.
