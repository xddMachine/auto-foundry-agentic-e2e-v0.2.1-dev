# Implementation summary

## Architecture

- `auto_foundry_core` v0.3.2 is a small, source-agnostic deterministic
  substrate with typed contracts, local source/profile/normalization,
  identity/relationship/population/aggregation operations, artifact and cache
  boundaries, telemetry, a Living Enterprise Model, a capability catalog, a
  program-owned data room, and durable item workspaces.
- The normal entry point is one immutable `RunContext` passed to one
  `DataRoomWorkbench` and one `ItemWorkspace` per active item. The workbench
  owns read-only archive/member cataloging, hashes, bounded reads, atomic
  item-local prepared candidates, item state, artifact progress, execution
  recovery, review, and immutable terminal snapshots. `CoreRuntime.execute()` remains available for
  deterministic operations: it validates the run/input boundary, hashes
  deterministic inputs, performs run-local cache lookup, dispatches the catalog
  capability, records an `OperationReceipt`, and emits passive telemetry. Its
  `CoreExecutionResult` carries the value, receipt, and cache status. The
  deprecated mutable `Workspace` facade is removed.
- `auto-foundry-agentic-e2e` v0.2.5 is a natural reviewed workflow with
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
  development-only, deterministic, and read-only. It accepts only the exact
  nested `freeze_markers` object with these five boolean fields all true:
  `answers_frozen`, `living_enterprise_model_frozen`,
  `prepared_data_registry_frozen`, `dashboard_frozen`, and
  `telemetry_frozen`. Top-level containers, aliases, and extra marker fields
  are invalid. It hashes analytical inputs before and after observation,
  reports five workflow/substrate evidence categories and exact duplicate
  groups, and writes exactly two evidence-bundle files. Client-business
  automation is rejected. A separate fresh Optimization Agent is described
  but is not invoked by this helper; collection failure is non-blocking.
- `analysis.py`, `integration.py`, `lifecycle.py`, `prepared.py`, and
  `product_contracts.py` provide the current v0.3.2 public runtime,
  integration, registry, receipt, and strict product contracts. Accepted
  answer bytes remain immutable and separate from `acceptance_envelope.json`;
  integration commits are under each item's `integration/committed/` path.

## Complete offline vertical proof

`tests/integration/test_vertical_acceptance.py` remains the broader closure
proof for source/runtime/LEM/product behavior. The companion
`tests/integration/test_workbench_durable_vertical.py` proves the normal v0.2.5
program path with a safe generic ZIP: catalog/search/read, item-local
candidate staging before acceptance, accepted-only Result Integration commit,
workspace creation before an attempt, exact-receipt execution recovery, a
separate one-time business repair, review/accept/reload, telemetry, source
hash immutability, sibling-path rejection, safe opaque materialization, and no
model/network calls. Both tests use real local filesystem wiring and generic
fixtures only.

The prelive verticals also prove one run-level physical inventory (initial full
bind plus member hashes), child bound contexts without re-inventory, selected
member verification, and explicit final verification that detects a mutation.
Prepared candidate bytes remain unchanged across validation, correction, and
commit, while an injected integration crash leaves a durable intent that
converges on retry. Mechanical validation is intentionally limited: semantic
completeness still requires exactly one fresh item-only Integration Fidelity
Reviewer after mechanical validation and before commit. The same Result
Integration Agent may make one targeted repair and receives one targeted
recheck; sibling and cumulative context is excluded.

Business repair scope honors explicit dependent artifact roots and JSON
fragments by authorizing their owning artifact paths; unrelated artifact
mutations remain fail-closed.

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

The historical v0.2.0 baseline remains a read-only evidence reference. Its immutable
source ZIP hash, eight `answered_with_limits` outcomes, two `partial_answer`
outcomes, nine repairs, 53 ontology items, ten scripts/8,016 LOC, reviewer
limitation, product refs, Q-004 blocked reproduction, and unknown wall time
are recorded in Benchmark A's baseline JSON.

## Resumable development-run boundary

Implementation transitions record old/new SHA, tree, and version, the earliest
affected item, preserved accepted hashes, why prior items are unaffected or
revalidated, and the exact resume point. A bounded fix resumes the earliest
safely affected checkpoint; a full restart is required only when impact reaches
earlier semantics/foundation or cannot be bounded. The proof is structured run
state, not a prose reinterpretation.

Business review returns all material findings in one response with stable IDs,
exact JSON-pointer/artifact paths, and dependent outputs. At most one scoped
business repair and one targeted recheck follow, preserving unchanged pointer
hashes mechanically. Phase timing stores observed start/finish/wall values and
literal unavailable identities; normalized incidents feed the cumulative
projector exactly once. Finalization binds a report hash and a manifest that
excludes itself and the terminal receipt, and is idempotent while rejecting
tampering or stale counts.

## Release candidate boundary

When the vertical proofs and full offline suite pass, the status is
**v0.2.5 / core 0.3.2 — offline program validation complete for later Benchmark A**. Benchmark A
remains prepared and unexecuted. This is an experimental release candidate, not
a production-hardened sandbox. A Coding Agent with unrestricted host
shell/filesystem access cannot
be fully sandboxed by this Python package. True isolation requires a separate
workspace/container or host allowlist.
Any stronger host/container isolation remains future, nonblocking hardening;
it is not part of the normal runtime path.
