# `auto_foundry_core` 0.2.0

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
  provides the program-owned read-only data room, bounded source catalog,
  archive/member hash checks, and run-local prepared assets. `durable.py`
  provides `ItemWorkspace`, artifact progress, execution attempts/recovery,
  review, and immutable accepted/technical-failure snapshots. These helpers do
  not make semantic business judgments or invoke model threads.
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

`tests/integration/test_vertical_acceptance.py` and
`tests/integration/test_workbench_durable_vertical.py` exercise the complete
offline paths. The first covers source registration/profile/normalization,
cache miss then hit, receipts and telemetry, reviewed identity and relationship
evidence, prepared-asset hash verification, namespace-safe ontology/prepared
reuse, a traceable reviewed fixture dashboard, non-blocking optimizer evidence
collection, and terminal export. The second covers a safe source ZIP, the
program-owned catalog/search/read/save-prepared path, item creation before an
attempt, materialization then execution recovery, a separate bounded business
repair, review/accept/reload, telemetry, immutable source hashing, and sibling
path rejection. Both fixtures use no model or network call.

The candidate is labelled **v0.2.2 — offline acceptance ready for later
Benchmark A** only when these vertical proofs and the full offline suite pass.
Benchmark A is not run here.
This remains an experimental release candidate, not a production-hardened
sandbox.

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully sandboxed by this Python package. True isolation requires a separate workspace/container or host allowlist.
