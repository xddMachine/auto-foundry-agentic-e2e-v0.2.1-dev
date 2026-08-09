# Changelog

## 0.2.2 — Agent Workbench + Durable Execution

Added:

- exact run markers for skill `0.2.2` and core `0.2.0`;
- one program-owned data room with a bounded physical source catalog built
  from ZIP/member metadata while raw archives remain read-only;
- one durable per-item `work` workspace and authoritative `item_state.json`
  created before the Lead Analyst is invoked;
- plan/source-map-first analysis, append-only material findings, materialized
  drafts, and atomic immutable accepted snapshots;
- structured artifact-progress checks with first no-progress materialization
  request and second no-progress lane recovery;
- execution recovery that preserves scratch and is counted separately from the
  single targeted business repair;
- run-local, loadable Prepared Data Registry assets with hash, location,
  schema, grain, and lineage provenance;
- passive attempt/artifact telemetry for invocation, artifacts, recovery,
  repairs, source/member reads, and core/cache observations;
- identity escalation and focused source-catalog completeness review for
  material absence claims.

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
