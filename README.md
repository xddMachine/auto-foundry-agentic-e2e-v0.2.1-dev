# Auto Foundry Agentic E2E v0.2.1 / core v0.1.0

This repository contains the v0.2.1 reviewed-analysis skill and the
source-agnostic, deterministic `auto_foundry_core` v0.1.0 substrate. The
deliverable is offline-friendly: the skill keeps a run-local Living Enterprise
Model and reviewed outputs, while the core provides typed local operations and
catalog discovery. No production dashboard, client automation, remote
publication, or benchmark execution is included.

The normal path is one immutable `RunContext` passed to one `CoreRuntime`:

```text
RunContext
  -> CoreRuntime.execute(OperationSpec)
  -> catalog capability and bounded source read
  -> deterministic input hashes and run-local cache
  -> OperationReceipt plus passive telemetry
  -> typed LEM/prepared-asset reuse
  -> reviewed fixture -> products dashboard
  -> frozen evidence collector -> terminal run export
```

The public entry points are `RunContext`, `CoreRuntime`,
`CoreExecutionResult`, `LEMRef`, and the immutable contracts exported from
`auto_foundry_core`. The old mutable `Workspace` facade is not part of the
package API. A normal run starts from the user's explicit task; it has no
manual authorization ceremony or second confirmation step.

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

The complete offline vertical proof is
`tests/integration/test_vertical_acceptance.py`; it uses three generic local
fixtures, fake semantic role records, real cache/telemetry/filesystem wiring,
and no model or network call. When that proof and the full offline suite pass,
the candidate status is **v0.2.1-rc1 — ready for Benchmark A**. Benchmark A
remains prepared but unexecuted in this repository.

The package script creates ignored local artifacts under `dist/`; no command
in this repository pushes or publishes them. Start a fresh Codex task after
changing or replacing the same-name skill so discovery is refreshed.
