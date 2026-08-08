# `auto_foundry_core` 0.1.0

`auto_foundry_core` is a small offline, source-agnostic deterministic substrate
for local analytics.  It is intentionally not an orchestration service and does
not contain domain recipes, model calls, remote adapters, or cross-run state.

## Layers

- `contracts.py` contains immutable source references and JSON-serializable
  operation, identity, ontology, prepared-asset, capability, and telemetry
  records.
- `workspace.py` provides the immutable `RunContext` and clean-room allowed
  roots before a source is read or a derived output is written. There is no
  second mutable workspace facade.
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
from auto_foundry_core import CoreRuntime, OperationSpec, RunContext

context = RunContext("RUN-example", run_root, (input_root,))
runtime = CoreRuntime(context)
execution = runtime.execute(OperationSpec("sources.preview", parameters={"path": "rows.json", "limit": 20}))
```

An operation spec is JSON with `capability_id`, optional `inputs`, and
`parameters`. The CLI requires one run root and optional repeatable input roots.
It validates the spec path, output directory, and `result.json` destination
against the same context before reading or creating anything; spec-embedded
root declarations cannot broaden the CLI roots and are ignored. When a
filesystem path is used, the effective roots are propagated through every
source read, hash, reproduction comparison, and derived write. Execution-facing
catalog operations and public manifest or reproduction path hashing require
explicit `Path`/reference values (or tagged `{"uri": ...}`/`{"location": ...}`
mappings); ordinary strings remain values.
Direct low-level source readers and hashing helpers remain usable without roots
for callers that explicitly operate outside clean-room mode.  The CLI writes
only explicitly requested derived output.  Cache roots are supplied by callers
and are never shared implicitly between runs.

## One offline vertical proof

`tests/integration/test_vertical_acceptance.py` exercises this same path with
three generic files and two typed analytics requirements sharing one foundation
task. It proves source registration/profile/normalization, cache miss then hit,
receipts and telemetry, reviewed identity and relationship evidence, prepared
asset hash verification, namespace-safe ontology/prepared reuse, a traceable
reviewed fixture dashboard, non-blocking optimizer evidence collection, and a
completed terminal export. The fixture uses no model or network call, and its
source hashes are checked before and after the run.

The candidate is labelled **v0.2.1-rc1 — ready for Benchmark A** only when this
vertical proof and the full offline suite pass. Benchmark A is not run here.
This remains an experimental release candidate, not a production-hardened
sandbox.

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully sandboxed by this Python package. True isolation requires a separate workspace/container or host allowlist.
