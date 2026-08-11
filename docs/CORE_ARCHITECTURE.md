# `auto_foundry_core` 0.3.2

`auto_foundry_core` is a small offline, source-agnostic deterministic substrate
for local analytics and durable item execution. It is intentionally not an
orchestration service and does not contain domain recipes, model calls, remote
adapters, or cross-run state.

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
  reasoning or workflow wall-time deadline. Use an OS/container boundary for
  hostile code.
- `integration.py` owns one Result Integration `IntegrationSession` per item.
  It stages typed claim/metric/limitation/evidence/prepared/ontology/
  relationship/dashboard records under `integration/staging/` and atomically
  commits `integration/committed/records.jsonl` plus `manifest.json` without
  parsing analytical prose. Prepared candidates are preflighted without
  registry mutation and registered exactly once at accepted commit; the durable
  intent makes crash retries converge. Mechanical checks cannot prove semantic
  completeness. Exactly one fresh item-only Integration Fidelity Reviewer runs
  after mechanical validation and before commit; the same Result Integration
  Agent may make one targeted repair and receives one targeted recheck. There
  is no prose parser, semantic compiler, or reviewer chain.
- Business repair authorization accepts explicit dependent artifact roots and
  JSON fragments as owning artifact paths; unrelated artifact mutations fail
  closed.
- `lifecycle.py` owns run-level `RunLifecycle` transitions, durable
  `AgentInvocationReceipt` ledgers, and explicit implementation transitions;
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
containment, cataloging, durable artifacts, progress, and recovery. The Lead
Analyst owns semantic interpretation, definitions, evidence sufficiency, and
the useful analytical route; the core never substitutes for that judgment.

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

The candidate is labelled **v0.2.5 / core 0.3.2 — offline program validation
complete for later Benchmark A** only when these vertical proofs and the full
offline suite pass. Benchmark A is not run here.
This remains an experimental release candidate, not a production-hardened
sandbox.

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully sandboxed by this Python package. True isolation requires a separate workspace/container or host allowlist.
