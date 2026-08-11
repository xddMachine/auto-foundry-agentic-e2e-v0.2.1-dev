# Auto Foundry Agentic E2E v0.2.6 / core v0.3.3

This repository contains the v0.2.6 reviewed-analysis skill and the
source-agnostic, deterministic `auto_foundry_core` v0.3.3 substrate. The
deliverable is offline-friendly: the skill keeps a run-local data room, durable
item workspaces, Living Enterprise Model, and reviewed outputs, while the core
provides typed local operations, bounded catalog access, and durable artifact
progress. No production dashboard, client automation, remote publication, or
benchmark execution is included.

The normal path is one immutable `RunContext` passed to one
`DataRoomWorkbench` and one `ItemWorkspace` per active item. `CoreRuntime`
remains available for deterministic operations:

```text
RunContext
  -> DataRoomWorkbench(context, archive_path) -> catalog/search/read
  -> BoundAnalysisContext.save_prepared_candidate (item-local candidate only)
  -> immutable BoundAnalysisContext -> controlled script receipts
  -> ItemWorkspace.create before the Lead Analyst attempt
  -> artifact progress -> execution recovery when needed
  -> draft -> one Independent Business Reviewer -> one scoped repair/recheck
  -> immutable accepted snapshot and reloadable state
  -> CoreRuntime.execute(OperationSpec) for deterministic mechanics
  -> reviewed fixture -> products dashboard
  -> frozen evidence collector -> terminal run export
```

The public entry points include `RunContext`, `DataRoom`,
`DataRoomWorkbench`, `DataRoomMember`, `DataRoomCatalogEntry`,
`PreparedAsset`, `PreparedAssetRegistry`, `CatalogSnapshot`,
`BoundAnalysisContext`, `ControlledScriptRunner`, `ItemWorkspace`,
`ArtifactProgress`, `ProgressDecision`, `ExecutionAttempt`, `AcceptedSnapshot`,
`AcceptedAnalysisBundle`, `IntegrationSession`, `RunLifecycle`,
`AgentInvocationReceipt`, `FreezeMarkers`, `CoreRuntime`, `CoreExecutionResult`,
`LEMRef`, and the immutable contracts exported from `auto_foundry_core`. The
old mutable `Workspace` facade is not part of the package API. The workbench
owns physical source access and durable state; the Lead Analyst owns semantic
interpretation and route selection. A normal run starts from the user's
explicit task and has no manual authorization ceremony or second confirmation
step.

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

Business repair scope honors explicit dependent artifact roots and JSON
fragments by authorizing their owning artifact paths; unrelated artifact
changes remain fail-closed.

An inadmissible reviewer-scope packet may be recovered only through
`ItemWorkspace.discard_business_review(...)`: the item-bound incident is
append-only and hash-bound, packet removal is atomic, existing work/draft bytes
are preserved, review and repair state reset, and a new full review is
required. Findings are never reinterpreted and no compatibility fallback is
provided.

The ontology is a compact enterprise map of stable objects, identities,
aliases, sources, documents, processes, definitions, rules, relationships,
limitations, and reusable metric definitions. Current counts, shares, amounts,
values, rankings, top-N rows, and dimensional observations remain accepted
results, claims, dashboard facts, evidence, or prepared assets; `add_metric`
records an observation and never promotes it into ontology.

The run binds one physical source inventory and records bounded counters for
full archive binding, member hashes, selected-member reads, and catalog
creation/reuse/load. Bound child contexts reuse that inventory rather than
re-inventorying it; an explicit final `verify_source_full()` check detects a
late source mutation. Opaque members may be copied only through the explicit
safe materialization API and are never semantically parsed. The controlled
script runner uses an explicit, configurable 3600-second process timeout by
default; this guard is not an agent reasoning or workflow wall-time deadline.
Execution recovery requires a canonical persisted invocation receipt reference
and exact attempt/lane binding; unpersisted or mismatched references fail
closed.

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
already-reviewed widget specifications to local HTML/CSS and validates stable
trace anchors. Its development-only optimizer observes frozen run-local
telemetry/traces/scripts, verifies input hashes, and writes only two report
files; it rejects client-business-automation classifications.

See [installation and migration](docs/INSTALLATION_AND_MIGRATION.md),
[implementation summary](docs/IMPLEMENTATION_SUMMARY.md), and the
[model-call ledger](docs/MODEL_CALL_LEDGER.md). Benchmark A is prepared but
not executed: see [benchmarks/benchmark_a](benchmarks/benchmark_a/README.md).

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
suite pass, the candidate status is **v0.2.6 / core 0.3.3 — offline program
validation complete for later Benchmark A**. Benchmark A remains prepared but
unexecuted in this repository; no run is claimed here.

The package script creates ignored local artifacts under `dist/`; no command
in this repository pushes or publishes them. Start a fresh Codex task after
changing or replacing the same-name skill so discovery is refreshed.
