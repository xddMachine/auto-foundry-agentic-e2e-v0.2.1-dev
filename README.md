# Auto Foundry Agentic E2E v0.2.1 / core v0.1.0

This repository contains the v0.2.1 reviewed-analysis skill and the
source-agnostic, deterministic `auto_foundry_core` v0.1.0 substrate. The
deliverable is offline-friendly: the skill keeps a run-local Living Enterprise
Model and reviewed outputs, while the core provides typed local operations and
catalog discovery. No production dashboard, client automation, remote
publication, or benchmark execution is included.

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

The package script creates ignored local artifacts under `dist/`; no command
in this repository pushes or publishes them. Start a fresh Codex task after
changing or replacing the same-name skill so discovery is refreshed.
