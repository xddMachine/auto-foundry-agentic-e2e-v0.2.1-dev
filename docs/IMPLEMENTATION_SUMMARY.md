# Implementation summary

## Architecture

- `auto_foundry_core` v0.8.0 is a small, source-agnostic deterministic
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
- `auto-foundry-agentic-e2e` v0.7.1 is a natural reviewed workflow with
  one Analytical Owner per item, optional bounded specialist evidence memos,
  one semantic Business Reviewer, Question and analytics-only Requirement
  modes, progressive run-local LEM layers, clean-room controls, passive
  telemetry, and reviewed-output-only products.
- Requirement Mode now uses one run-level event-driven **Planner** as cognitive
  scheduler/control plane. It reasons over exact `RequirementRecord` values,
  compact physical catalog metadata, and current item/resolution outcomes. Its
  initial order/grouping is advisory; it preserves explicit user
  priority/order; it never declares runtime semantic dependencies, which the
  Analytical Owner discovers after understanding the requirement. The runtime
  `waiting_on_resolution`/`ready_to_resume` ledger is the sole semantic block.
  `RequirementExecutionPlan` and
  `RequirementExecutionGroup` remain revisionable scheduling recommendations,
  separate from catalog hashes and lifecycle state. A technical failure does
  not create Planner dependency blocks; independent groups remain eligible and
  runtime resolution state controls waiting and resume.
- The default capacity contract is four entity-resolution workers, one
  Analytical Owner, up to three owner specialists, and eight active workers
  total; the Planner is not counted. Hosts may configure lower or higher limits
  but never oversubscribe actual host capacity. Requested Run A and Run B
  executions remain sequential.
- When an Analytical Owner proposes a new arbitrary real-world identity domain
  during scouting, that exact owner-bound proposal is reserved as `resolving`
  and one Entity
  Resolution Owner is launched. The owner scans all rows of domain-relevant
  tables and relevant documents from reservation hints, expands only for
  concrete matching/conflict evidence, and reuses the run-level catalog. It
  owns methodology, may inspect manually or write Python/SQL/scripts/use
  helpers, bulk-apply justified patterns, test samples/coverage/exceptions/
  population differences, and revise the method. Row-by-row review, an
  authoritative crosswalk, and a fixed matching script are not required.
  Pattern rules remain run knowledge; future helper-library audit is deferred.
  The review decision is binary per proposed mapping (accepted or not accepted);
  each accepted `CanonicalMapping` may contain one or many source identities or
  representations, including bulk pattern-derived populations. Unresolved or
  ambiguous records stay source-local with coverage/exceptions and do not
  downgrade proven mappings. Ready snapshots publish the canonical class,
  source-account representations, reviewed `IdentityDecision`/`CanonicalMapping`,
  identity `represents` relationships, and versioned mapping asset/coverage
  where available. Ready is exposed only after an atomic reviewed commit.
- Every item binds directly to the same `RunContext` and shared Data Room. Each
  item uses an item-local `RequirementAnalysisPlan` and ordinary loop:
  analysis, review, iterative material repairs, accept or
  `technical_failure`, then integration. Within a group one owner remains per
  requirement; bounded shared investigation may be reused and independent
  groups may run when host capacity permits. Reviewer findings are semantic
  repair provenance; after authorization the same owner may update item-local
  work and the affected answer section, with no cross-item writes. No prior
  item context or internal paths/hashes are exposed to analytical roles.
- Each Analytical Owner performs a readiness/scouting pass that explicitly
  searches/selects ontology, identity mappings, relationships, and prepared
  semantics, or records why none applies. If a needed domain is `resolving`, it
  reports `waiting_on_resolution`, releases its lane, and lets the Planner skip
  to the next original-order runnable item; the Planner marks the earliest
  paused item `ready_to_resume` and resumes it on `ready`. If all runnable items wait, the owner lane sleeps while active
  resolvers progress; block only when nothing is runnable and no resolver can
  progress. Entity-resolution jobs are parallel external jobs, not extra
  specialists and never answer requirements.
  The host calls public `scheduling_tick()` after every wait, resume, or
  terminal transition instead of manually joining three state stores.
- `analyst_workspace.py` keeps the cognitive loop with that owner while the
  program owns source serialization, strict JSON, hashes, receipts, semantic
  repair provenance, and lifecycle. Reviewers do not emit internal pointers,
  paths, or hashes; authorized changes stay item-local.
- Before analysis, the owner calls `brief()` and searches compact accepted
  ontology/prepared descriptors, selecting exact useful IDs with a purpose.
  The program records those decisions in the item-bound
  `work/semantic_selections.jsonl`; prepared rows load only after selection and
  content-hash validation. Result Integration publishes material reusable
  objects/table mappings, grain, key fields/normalization,
  relationship/cardinality/coverage/date authority/limits, and truly reusable
  prepared descriptors, not every merge, result row, metric observation,
  Japan/Spain filter, or question-specific aggregation.
  `no_change` is reserved for an accepted item with no reusable semantic
  understanding or asset and a concrete reason; it is not the default. The
  existing item-only Integration Fidelity Reviewer checks semantic correctness
  without adding a role, gate, mandatory large schema, or minimum count. In the
  Q1→Q2/Q9 example, later items search/select/load Q1's exact order-fulfillment
  semantics and compute their own requirement-specific measures.
  Optional prepared-data `effective_period` is carried unchanged through the
  descriptor/sidecar, operation manifest/hash inputs, accepted integration and
  registry, and later search/select/reuse; omission remains valid with no period
  constraint.
- Analytical Owners establish actual joins/relationships with
  `source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
  unique tested edge-pair count), `source_population`/`target_population`,
  `matched_source_count`/`matched_target_count` (distinct matched endpoints),
  and `source_coverage`/`target_coverage` (endpoint count divided by its
  population, with zero for a zero population), plus `as_of`/date authority,
  limitations, and evidence. Integration publishes only reviewed tested
  relationships and canonical identity mappings; it never completes a
  theoretical graph or infers joins from prose. Resolution mappings are binary
  review decisions are binary per mapping while each accepted mapping may
  contain one or many source identities and coverage/exceptions remain job-level
  evidence. `accept`
  applies when the core decision is answered with normal disclosed limits;
  `accept_with_limits` is reserved for a material missing or unreliable
  requested component. Semantic fidelity is reviewed independently.
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
  `product_contracts.py` provide the current v0.8.0 public runtime,
  integration, registry, receipt, and strict product contracts. Accepted
  answer bytes remain immutable and separate from `acceptance_envelope.json`;
  integration commits are under each item's `integration/committed/` path.

## Complete offline vertical proof

`tests/integration/test_vertical_acceptance.py` remains the broader closure
proof for source/runtime/LEM/product behavior. The companion
  `tests/integration/test_workbench_durable_vertical.py` proves the normal v0.7.1
program path with a safe generic ZIP: catalog/search/read, item-local
candidate staging before acceptance, accepted-only Result Integration commit,
workspace creation before an attempt, iterative business repairs with targeted
rechecks, review/accept/reload, telemetry, source
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

Business review findings remain semantic repair provenance. Once authorized, the
same owner may update any affected item-local work and answer section; internal
paths, hashes, and artifact roots remain program-owned, and cross-item writes
fail closed.

Each Requirement Mode item binds directly to the same run context and shared
Data Room. Persisted contexts and accepted business artifacts are reusable
across implementation revisions without a transition/rebind workflow.
Committed semantics, relationships, and prepared assets remain available, and
the requirement portfolio may be revised while running, paused, or complete.

Two concrete integration defects found by this proof are fixed: contract
hashing now uses `to_dict()` before `dataclasses.asdict()` (mapping proxies are
not deepcopyable), and path parameters containing a registered `DataAssetRef`
are accepted by catalog source capabilities.

## Deliverable and boundaries

`scripts/package_release.py` would generate a local release package containing
the complete skill directory and a core wheel; `scripts/validate_release.py`
must validate those artifacts before installation. This repository currently
claims only offline test and static-check evidence, not an existing ZIP or
validated wheel. `benchmarks/benchmark_a/` contains a baseline and
later-launch contract for the same ten questions, but no raw source, prompt
archive, benchmark run, fake result, or model call. Dist artifacts are
generated and ignored; primary owns commits and any future publication.

The historical v0.2.0 baseline remains a read-only evidence reference. Its immutable
source ZIP hash, eight `answered_with_limits` outcomes, two `partial_answer`
outcomes, nine repairs, 53 ontology items, ten scripts/8,016 LOC, reviewer
limitation, product refs, Q-004 blocked reproduction, and unknown wall time
are recorded in Benchmark A's baseline JSON.

## Development-run boundary

Run state, receipts, and terminalization are program-owned evidence. A
Planner recommendations can be revised from current outcomes, but they never
become catalog or lifecycle authority. Runtime semantic dependencies are
discovered by the Analytical Owner after understanding each requirement. The
ordinary item loop remains the source of analytical truth; the proof is
structured run state, not a prose reinterpretation.

Business review returns all material findings in one response with stable IDs,
semantic answer sections and business categories. Findings remain semantic
repair provenance; after authorization the same owner may update item-local
work and the affected answer section, with no cross-item writes. Exactly two
same-owner business repairs and targeted rechecks are available; a third
request fails closed.
Phase timing stores observed start/finish/wall values and
literal unavailable identities; normalized incidents feed the cumulative
projector exactly once. Finalization binds a report hash and a manifest that
excludes itself and the terminal receipt, and is idempotent while rejecting
tampering or stale counts.

## Release candidate boundary

When the vertical proofs and full offline suite pass, the status is
**v0.7.1 / core 0.8.0 — offline program validation complete for later Benchmark A**. Benchmark A
remains prepared and unexecuted. This is an experimental release candidate, not
a production-hardened sandbox. A Coding Agent with unrestricted host
shell/filesystem access cannot
be fully sandboxed by this Python package. True isolation requires a separate
workspace/container or host allowlist.
Any stronger host/container isolation remains future, nonblocking hardening;
it is not part of the normal runtime path.
