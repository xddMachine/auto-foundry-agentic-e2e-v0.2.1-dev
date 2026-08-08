# `auto_foundry_core` 0.1.0

`auto_foundry_core` is a small offline, source-agnostic deterministic substrate
for local analytics.  It is intentionally not an orchestration service and does
not contain domain recipes, model calls, remote adapters, or cross-run state.

## Layers

- `contracts.py` contains immutable source references and JSON-serializable
  operation, identity, ontology, prepared-asset, capability, and telemetry
  records.
- `workspace.py` enforces clean-room allowed roots before a source is read or a
  derived output is written.
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
- `enterprise_model.py` stores a run-local extensible ontology and prepared-data
  registry.  Accepted `KnowledgeDelta` values are applied atomically and
  conflicts/supersession are retained.
- `capabilities.py` is the executable metadata source.  `catalog.py` generates
  the discoverable catalog and `cli.py` exposes catalog list/search/describe and
  deterministic execution.

## Dependency decision

The required install has no third-party dependencies so it can be built and
tested offline.  The runtime already commonly provides `openpyxl`, `pyarrow`,
and `python-dateutil`; they are imported lazily for Excel, Parquet, and flexible
date parsing.  They are listed as optional `io` extras in `pyproject.toml`.
The implementation uses the standard library for contracts, hashing, CSV/JSON,
serialization, caching, and aggregation rather than adding a framework whose
validation or execution surface would be larger than this core.

## CLI

```text
python -m auto_foundry_core catalog list
python -m auto_foundry_core catalog search "coverage"
python -m auto_foundry_core catalog describe relationships.measure
python -m auto_foundry_core run sources.preview --spec operation.json --output out/
```

An operation spec is JSON with `capability_id`, optional `inputs`, and
`parameters`.  The CLI writes only explicitly requested derived output.  Cache
roots are supplied by callers and are never shared implicitly between runs.
