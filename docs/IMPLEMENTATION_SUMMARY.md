# Implementation summary

## Architecture

- `auto_foundry_core` v0.1.0 is a small, source-agnostic deterministic
  substrate with typed contracts, local source/profile/normalization,
  identity/relationship/population/aggregation operations, artifact and cache
  boundaries, telemetry, a Living Enterprise Model, and a capability catalog.
- The normal entry point is one immutable `RunContext` passed to one
  `CoreRuntime`. `CoreRuntime.execute()` validates the run/input boundary,
  hashes deterministic inputs, performs run-local cache lookup, dispatches the
  catalog capability, records an `OperationReceipt`, and emits passive
  telemetry. Its `CoreExecutionResult` carries the value, receipt, and cache
  status. The deprecated mutable `Workspace` facade is removed.
- `auto-foundry-agentic-e2e` v0.2.1 is a natural reviewed workflow with
  Question and analytics-only Requirement modes, progressive run-local LEM
  layers, exact-ID evidence selection, review routing, clean-room controls,
  passive telemetry, and reviewed-output-only products.
- `skills/auto-foundry-agentic-e2e/scripts/dashboard_renderer.py` is a
  stdlib-only presentation renderer. It
  accepts a reviewed widget fixture, preserves supplied values and order,
  requires per-widget reviewed-item/output and evidence/trace provenance,
  requires non-empty ordered domain/decision-flow assignments for every widget,
  emits standalone HTML/CSS, and validates internal trace links. It calculates
  no analytical metric and reads no source.
- `skills/auto-foundry-agentic-e2e/scripts/optimizer_evidence_collector.py` is
  development-only, deterministic, and read-only. It
  requires an explicit mapping with `answers_frozen`,
  `living_enterprise_model_frozen`/`lem_frozen`,
  `prepared_assets_frozen`/`prepared_data_registry_frozen`,
  `dashboard_frozen`, and `telemetry_frozen` all true. It hashes analytical
  inputs before and after observation, reports five workflow/substrate
  evidence categories and exact duplicate groups, and writes exactly two
  evidence-bundle files. Client-business automation is rejected. A separate
  fresh Optimization Agent is described but is not invoked by this helper;
  collection failure is non-blocking.

## Complete offline vertical proof

`tests/integration/test_vertical_acceptance.py` is the closure proof for the
normal path. It uses three generic local files, two analytics requirements, one
shared `FoundationTask`, and fake semantic role records only. The test reaches
terminal completion while proving source registration/profile/normalization,
cache miss then hit, real receipts/telemetry, identity and relationship
diagnostics, prepared output integrity and exact LEM namespace reuse, reviewer
unavailability disclosure, a reviewed-output-only traceable dashboard,
non-blocking optimizer evidence collection, source immutability, sibling-path
rejection, and lifecycle-independent calculations after terminal export.

Two concrete integration defects found by this proof are fixed: contract
hashing now uses `to_dict()` before `dataclasses.asdict()` (mapping proxies are
not deepcopyable), and path parameters containing a registered `DataAssetRef`
are accepted by catalog source capabilities.

## Deliverable and boundaries

The local release package contains the complete skill directory and a validated
core wheel. `benchmarks/benchmark_a/` contains a baseline and later-launch
contract for the same ten questions, but no raw source, prompt archive,
benchmark run, fake result, or model call. Dist artifacts are generated and
ignored; primary owns commits and any future publication.

The v0.2.0 baseline remains a read-only evidence reference. Its immutable
source ZIP hash, eight `answered_with_limits` outcomes, two `partial_answer`
outcomes, nine repairs, 53 ontology items, ten scripts/8,016 LOC, reviewer
limitation, product refs, Q-004 blocked reproduction, and unknown wall time
are recorded in Benchmark A's baseline JSON.

## Release candidate boundary

When the vertical proof and full offline suite pass, the status is
**v0.2.1-rc1 — ready for Benchmark A**. Benchmark A remains prepared and
unexecuted. This is an experimental release candidate, not a production-hardened
sandbox. A Coding Agent with unrestricted host shell/filesystem access cannot
be fully sandboxed by this Python package. True isolation requires a separate
workspace/container or host allowlist.
Any stronger host/container isolation remains future, nonblocking hardening;
it is not part of the normal runtime path.
