# Changelog

## 0.2.6 — Reviewer-scope packet recovery

Changed:

- the current package markers move directly to skill `0.2.6` and core `0.3.3`;
- `ItemWorkspace.discard_business_review(...)` is a strict recovery path for
  an inadmissible `reviewer_scope` packet only: it appends a hash-bound,
  append-only audit record, removes the invalid packet atomically, preserves
  the existing work and draft bytes, resets review and business-repair
  authority, and requires a new full review;
- discard recovery never reinterprets findings, mutates semantic content, or
  supplies a compatibility fallback.

## 0.2.5 — Explicit dependent artifact repair scope

Changed:

- business repair scope now honors explicit dependent artifact roots and JSON
  fragments while unrelated artifact changes remain fail-closed;
- no compatibility layer was added; the current contract and package markers
  move directly to skill `0.2.5` and core `0.3.2`.

## 0.2.4 — Resumable development-run contract

Added:

- exact run markers for skill `0.2.4` and core `0.3.1`;
- a compact enterprise ontology boundary: stable objects, identities, aliases,
  sources, documents, processes, definitions, and reusable metric definitions;
  current counts, shares, amounts, values, ranks, top-N rows, and dimensional
  observations remain accepted results or evidence, and `add_metric` is an
  observation record rather than ontology promotion;
- one Independent Business Reviewer returning all material findings with stable
  IDs, exact JSON-pointer/artifact paths, dependent outputs, and at most one
  scoped business repair followed by a targeted recheck;
- one fresh item-only Integration Fidelity Reviewer after mechanical validation
  and before commit, with same-agent targeted repair/recheck and no sibling,
  cumulative, prior-memory, or broad-workspace context;
- phase-separated timing, normalized incidents, cumulative projection,
  non-circular terminalization, and explicit implementation-transition/resume
  proofs. Benchmark A question wording/order and hash remain unchanged and no
  benchmark run is claimed.

## 0.2.3 — Program-owned integration and controlled analysis runtime

Added:

- exact run markers for skill `0.2.3` and core `0.3.0`;
- immutable canonical physical catalogs keyed by source hash, core version,
  and catalog schema, with derived sample/category views;
- durable prepared-asset registration independent of reuse visibility;
- immutable `BoundAnalysisContext` manifests and controlled script execution:
  compile/dependency preflight checks, successful `smoke`/`full` runtime
  receipts, an optional second `full` receipt for deterministic comparison,
  and whichever preflight failure phase applies (`compile` or
  `dependency_check`);
- receipt-gated lifecycle/recovery APIs and one-owner incremental result
  integration staging/commit records;
- separate accepted answer bytes, acceptance envelope, integration manifests,
  and strict nested product freeze markers.
- canonical terminal-reason classifier outputs: `same_attempt_feedback`,
  `business_repair`, `execution_recovery`, `abort_and_new_clean_run`, or
  `null`; raw terminal reasons remain specific observed facts.

Changed:

- the current normal path uses `data_room/catalogs/<catalog_key>.json`,
  `accepted/answer_content.json`, `accepted/acceptance_envelope.json`, and
  `integration/{staging,committed}/` artifacts;
- offline release validation now checks all public runtime/integration/product
  exports and the 0.3.0 core wheel.

This release is an offline program-validation package, not a benchmark run or
production isolation boundary. Hostile script isolation still requires an
OS/container boundary.

## 0.2.2 — Agent Workbench + Durable Execution

Added:

- exact run markers for skill `0.2.2` and core `0.2.0`;
- one program-owned data room with a bounded physical source catalog built
  from ZIP/member metadata while raw archives remain read-only;
- one durable per-item `work` workspace and authoritative `item_state.json`
  created before the Lead Analyst is invoked;
- plan/source-map-first analysis, append-only material findings, materialized
  drafts, and atomic immutable accepted snapshots;
- structured artifact-progress checks where filesystem no-progress yields
  `await_runtime`/materialization guidance and only a completed invocation loss
  receipt authorizes execution recovery;
- execution recovery that preserves scratch and is counted separately from the
  single targeted business repair;
- run-local, loadable Prepared Data Registry assets with hash, location,
  schema, grain, and lineage provenance;
- passive attempt/artifact telemetry for invocation, artifacts, recovery,
  repairs, source/member reads, and core/cache observations;
- identity escalation and focused source-catalog completeness review for
  material absence claims.
- explicit `BoundAnalysisContext` creation before each Lead Analyst;
- deterministic `ControlledScriptRunner` receipts for compile/import, smoke,
  full, and deterministic checks, with code errors returned to the same
  analyst and attempt;
- receipt-gated execution recovery, where filesystem no-progress yields
  `await_runtime`/materialization guidance and provider/model identity may be
  literal `unavailable`;
- immutable accepted answer bytes separated from the program-owned acceptance
  envelope, followed by exactly one incremental Result Integration Agent;
- canonical prepared-asset registration and visibility-only scope/reuse views,
  plus explicit terminal reason classes for code error, business repair,
  execution recovery, and core defect.

Changed:

- Question Mode still preserves supplied wording/order, processes one item at
  a time, and continues after bounded outcomes; Requirement Mode still honors
  explicit user priority before unprioritized work;
- compact source/LEM/prepared indexes are directly searchable by the Lead
  Analyst; catalog capabilities are optional internal recommendations and
  custom reproducible code remains allowed;
- the program validates/applies reviewed Knowledge Delta records, with
  `no_change` requiring a concrete reason;
- final products and optimizer evidence use accepted snapshots and the
  existing whole-run freeze boundary.

Removed or prohibited:

- mandatory separate Navigator role and per-item Capability Catalog
  lookup/compliance artifact;
- terminalizer agent, wall-time deadline, parallel question wave, per-question
  freeze/mutation incident, and any second review or business repair;
- Portfolio Planner, Navigator, descriptor/typed-validation role,
  business-repair finalizer, reviewer-of-reviewer, manual terminalizer,
  Integration Reviewer, and finalizer chain;
- planner framework, fixed business-term dictionary, domain recipe, central
  ontology, cross-run cache, external/model call, production application, and
  compatibility wrapper;
- presenting superseded v0.2.1 workflow instructions as current or claiming
  Benchmark A.1 completion.

The core records durable state but cannot create or restart model threads; the
Run Director/host performs execution recovery. `technical_failure` is written
only after recovery routes are exhausted and is never a data conclusion.

## 0.2.1 — superseded release

The preceding release established the initial natural-analysis, review,
run-local reuse, dashboard, and optimizer boundaries. Its workflow wording is
historical only; install one skill folder and follow the v0.2.2 contract.
