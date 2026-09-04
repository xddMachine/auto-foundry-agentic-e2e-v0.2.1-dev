# Changelog

## 0.7.2 / core 0.8.1 — universal Data Room ingestion

This release binds skill `0.7.2` to core `0.8.1` under the
`universal-data-room-ingestion` release slug.

1. The normal Data Room now admits every safe regular file without an
   extension or arbitrary aggregate-size gate. Parquet is cataloged and read
   natively through bounded Arrow batches; SQLite databases are opened
   read-only and contribute one deterministic catalog entry per user table.
2. Unknown, extensionless, notebook, and auxiliary files remain safe opaque
   members for explicit materialization. The core does not claim to parse
   unknown formats analytically. Selected archive members are streamed through
   resource and disk checks, while traversal, symlink, special-file,
   encryption, corruption, and compression-bomb defenses remain active.
3. PyArrow is now a base core dependency for the native Parquet path; only
   XLSX support remains in the optional `io` extra. No benchmark or run success
   claim is made. No new run was launched.

## 0.7.1 / core 0.8.0 — production skill binding and scouting contract

1. This patch advances the active skill marker to `0.7.1` while retaining core
`0.8.0` and the `entity-resolution-and-analytical-relationships` release
slug. It freezes the current Analytical Owner readiness/scouting contract:
owners search/select ontology, identity mappings, relationships, and prepared
semantics, then propose and wait on each needed identity domain before the
Planner resumes the item. The release also records the host-bound owner in the
program-owned `work/analysis_owner.json` record and keeps material business
repairs iterative with a targeted recheck for each repair. No benchmark or run
success claim is made. No new run was launched.

## 0.7.0 / core 0.8.0 — entity resolution and analytical relationships

This breaking release advances the active markers to skill `0.7.0` and core
`0.8.0` under the release slug `entity-resolution-and-analytical-relationships`.

1. The cognitive Planner coordinates runtime wait/resume around resolving
   identity domains; entity-resolution domains reserve and publish atomically,
   with pattern-based reviewed mappings retaining coverage and exceptions.
   Analytical Owners make exact semantic selections and record relationship
   evidence. Review verdicts calibrate `accept` versus `accept_with_limits`.
   No benchmark or run success claim is made. No new run was launched.
The same release now includes typed Planner runtime actions and incident
logging, pre-review Entity Resolution projection validation, exact
selected-source loading, typed identity mapping views, non-gating mapping
completeness, bytecode-free script validation, current-observation dashboard
facts, advisory semantic promotion suggestions, and the multi-page chart-led
dashboard v2 renderer. These additions do not introduce a mandatory
relationship schema or new analytical gate.

The local-run workflow is now deliberately mutable. Requirement portfolios
may be added to, updated, reordered, emptied, or reduced while active, paused,
or complete; completed runs may reopen and the local autopilot keeps watching
for later work. Persisted analytical contexts load across core/skill revisions,
owner labels are audit metadata, and material business repairs are iterative
rather than capped. Source/content hashes still detect real byte drift but no
longer act as workflow or version locks.

## 0.6.0 / core 0.7.0 — cognitive requirement supervisor and simple item flow

This breaking release advances the active markers to skill `0.6.0` and core
`0.7.0`.

1. Breaking removal of deterministic immutable Portfolio Plan/lifecycle hash
   authority and normal transition/rebind inheritance. A cognitive Requirement
   Supervisor chooses and revises advisory order and grouping; the ordinary
   per-item Analytical Owner loop binds directly to the shared Data Room.
   Runtime resolution state owns semantic blocking, only committed reuse is
   allowed, and `technical_failure` is isolated from independent groups. No benchmark or
   run success claim is made. No new run was launched.

## 0.5.9 / core 0.6.9 — inherited-item prefix/suffix provenance

This patch advances the active markers to skill `0.5.9` and core `0.6.9`.

Final SHIP fix:

1. In the current serial workflow, an inherited item validates the
   pre-creation run-transition prefix through
   signed catalog inheritance, then validates post-creation transitions through
   item-local audit/state plus a continuous bridge/final manifest. This enables
   accepted REQ02 after T1/T2 to source REQ03 after T3, with no
   analytics process change. This bounded fix does not claim concurrent
   source-chain reconciliation hardening; public concurrent reconciliation
   remains outside its scope.

## 0.5.8 / core 0.6.8 — monotonic accepted preservation and context-root rebind

This patch advances the active markers to skill `0.5.8` and core `0.6.8`.

Final SHIP fix:

1. Preservation maps grow monotonically: old accepted hashes are immutable, and
   an accepted strict predecessor is admitted only when its physical hash is
   exact; mixed inherited source/target contexts validate their own persisted
   input roots, while a one-shot rebind capability binds exact roots; a
   requirement exact retry converges append/state after a crash. This is a
   bounded transition identity/recovery closure with no analytics process
   change.

## 0.5.7 / core 0.6.7 — visual repair evidence-reference scope

This patch advances the active markers to skill `0.5.7` and core `0.6.7`.

Final SHIP fix:

1. A targeted visual repair whose categories include the `calculation+presentation`
   combination can bind its newly produced evidence ref; calc-only,
   presentation-only, and nonvisual repairs remain fail-closed. Prior scope is
   verified before atomic replacement, and a third repair remains fail-closed.
   This is a bounded visual repair evidence-reference closure with no analytics process change.

## 0.5.6 / core 0.6.6 — standalone rebindable context

This patch advances the active markers to skill `0.5.6` and core `0.6.6`.

Final SHIP fix:

1. The existing public opaque-capability
   `load_rebindable_unaccepted_context(...)` loader supports a standalone
   unaccepted bootstrap context after a validated implementation
   transition/restart. It binds the authoritative
   portfolio+catalog/source/inventory/item/ledger, blocks analysis before an
   immediate `rebind_implementation(...)`; the ordinary loader remains strict.
   This is a bounded identity/recovery closure with no analytics process change.

## 0.5.5 / core 0.6.5 — targeted review scope recompute

This patch advances the active markers to skill `0.5.5` and core `0.6.5`.

Final SHIP fix:

1. targeted `repair_once` verifies the prior repair under its old scope,
   atomically replaces the findings, and recomputes the exact narrow
   pointers/deps/artifacts authorized for the second repair while preserving
   prior attribution; a third repair request fails closed. previous unaudited
   runs may transition through lifecycle authority. This is a bounded review
   scope closure with no analytics process change.

## 0.5.4 / core 0.6.4 — accepted intermediate context and authoritative portfolio plan

This patch advances the active markers to skill `0.5.4` and core `0.6.4`.

Final SHIP fixes:

1. Accepted intermediate context: an accepted context at the exact
   implementation-ledger boundary can be loaded read-only and used to create a
   later target only after the validated suffix transition is present. Accepted
   bytes remain immutable, and a `technical_failure` predecessor is excluded
   from semantic reuse.
2. Authoritative Requirement Portfolio planning: the clean planner uses a
   version-invariant physical-catalog fingerprint and independent `run_state`
   plan/implementation authority, with a plan-to-run-to-item transaction,
   strict transition applicability/current physical identity, and crash-retry
   idempotence.

Legacy unaudited partial Requirement Mode runs are diagnostic and must restart;
no analytics process change.

## 0.5.3 / core 0.6.3 — opaque rebind capability and transitioned-source preflight

This patch advances the active markers to skill `0.5.3` and core `0.6.3`.
The public opaque-capability `load_rebindable_unaccepted_context(...)` loader
supports a fresh-process active inherited context that lags an authoritative
transition suffix while keeping the ordinary loader strict. It validates the
full ledger and capability chain and keeps analysis blocked until explicit
rebind.
Transitioned-source preflight now validates the full catalog-origin ledger
chain plus local audit-manifest continuity before accepting the exact current
tail, so a later item can proceed after a technical-failure predecessor without
semantic reuse.

## 0.5.2 / core 0.6.2 — transition and repair-context closure

This patch advances the active markers to skill `0.5.2` and core `0.6.2`.
Inherited catalog-context rebind now audits the authoritative ledger prefix,
suffix, and local transition record before accepting the inherited context.
Active repair recovery now performs crash-consistent program-context baseline
rebasing with audit/ledger validation and no repair-scope expansion.

## 0.5.1 / core 0.6.1 — requirement transition and repair-scope closure

This patch advances the active markers to skill `0.5.1` and core `0.6.1`.
Requirement Mode now propagates the authoritative lifecycle mode when creating
contexts from a transitioned catalog. The public
`load_preserved_accepted_context(...)` loader enables fresh-process recovery of
an accepted predecessor across an explicit implementation transition without
mutating accepted work. Answer-category repair now authorizes the exact
`work/business_review_packet.json` path with strict additive reconciliation of
legacy scope fields.
An explicit implementation transition can also preserve one mechanically valid,
active business-repair packet for an unaccepted item, so a patch does not erase
or duplicate the already-consumed repair budget.

This is a contract/recovery closure only: it makes no optimization, model or
network, benchmark, or business-result change.

## 0.5.0 / core 0.6.0 — run-level Requirement Portfolio Planner

Requirement Mode now permits exactly one program-owned Portfolio Planner per
run. It receives the exact `RequirementRecord` sequence and a stable compact
metadata-only catalog, then persists an immutable, auditable ordered portfolio
of parent plans. Planner order/grouping is advisory and never creates semantic
dependency edges; strict-JSON planner receipts and exact input/catalog
fingerprints make the boundary replayable. Parent execution
is serial, with one stable owner consuming each authoritative persisted
portfolio entry to create an item-local plan. The planner never reads rows,
LEM, or prior runs, calls a network/model, invokes the optimizer, or creates a
parallel wave or child workspace.

## 0.4.2 / core 0.5.2 — counted semantic-reuse release closure

The five counted Q1/Q2/Q6/Q8/Q9 runs completed with 25 accepted items and 22
integrated items. Semantic graph or prepared-data reuse was observed for all
15 relevant later questions; Q6 was unrelated and correctly produced no reuse.
The release fixes public `ontology_refs` prepared-candidate and operation-hash
sidecars, stable integration-record correction that publishes only the
corrected candidate, business-repair `/scope` and `/next_actions` plus
`work/results` scope, pointer-category exclusivity, and exact post-review
artifact continuity.

The counted3 Q8 late post-commit ontology-value finding remains test evidence;
it was not silently fixed in that run. Counted2 Q8 and counted5 Q2/Q9
technical failures remain diagnostic outcomes rather than hidden business
results.

## 0.4.1 / core 0.5.1 — prepared-data period fidelity

This patch advances the active markers to skill `0.4.1` and core `0.5.1` for
V040-LIVE-001. Public prepared-data `effective_period` is carried unchanged
through the descriptor and candidate sidecar, operation manifest/hash inputs,
accepted integration record, registry entry, and later search/select/reuse.
Omitted `effective_period` remains allowed and means no period constraint; no
period is inferred from the current date.

Diagnostic run1/run2 artifacts were invalidated before counted runs. They are
not benchmark evidence or release validation.

## 0.4.0 / core 0.5.0 — semantic graph reuse

This release advances the current markers to skill `0.4.0` and core `0.5.0`
for compact, item-bound semantic reuse. Before analysis, the Analytical Owner
checks `AnalystWorkspace.brief()` and searches accepted ontology and prepared
asset descriptors. The owner can select exact accepted IDs with a stated
purpose; prepared rows are loaded only after selection and registry hash
validation, with the item-bound `work/semantic_selections.jsonl` trace retained.

Result Integration publishes only material reusable understanding actually
established: business objects and table mappings, grain, key fields and
normalization, relationship/cardinality/coverage/date authority/limits, and
truly reusable prepared-asset descriptors. It does not promote every merge,
result row, metric observation, Japan/Spain filter, or question-specific
aggregation. The Q1→Q2/Q9 path can therefore reuse order-header, order-line,
delivery, customer, and material semantics plus a reusable order-fulfillment
core while each later question computes its own measures. `no_change` remains
valid only when no reusable semantic understanding or asset was established;
it is not the default outcome.

No new roles, gates, mandatory large schema, or minimum record counts are
introduced. The existing item-only Integration Fidelity Reviewer checks
semantic correctness using the current review boundary.

## 0.3.0 / core 0.4.0 — breaking contract closure

This release moves the current markers directly to skill `0.3.0` and core
`0.4.0`. It closes the public-contract defects identified during the prior
five-question live5 review context; it does **not** claim that a new five-
question run has happened.

Breaking changes:

- business review may return only `accept`, `accept_with_limits`, `repair_once`,
  or `confirm_data_insufficiency`; `block_specific_claims` is removed from the
  current contract;
- exactly two same-owner business repairs are available per item, each with a
  targeted recheck; a third request fails closed, while code feedback and
  execution recovery remain outside that budget;
- only the Analytical Owner may originate a `DataInsufficiencyConclusion`.
  `blocked_by_evidence` is publishable only after the reviewer confirms that
  explicit conclusion; presentation, calculation, evidence, method, reviewer,
  program, and script defects repair or technical-fail;
- Requirement Mode keeps one parent requirement and one Analytical Owner, who
  creates a semantic `RequirementAnalysisPlan` with 1..N internal tasks before
  analysis and synthesizes one parent answer. There are no child lifecycle
  workspaces, Portfolio Planner, keyword router, or extra planner agent;
- Analytical Owner and reviewer surfaces return semantic prose/values/findings.
  The program populates paths, hashes, state, receipts, and other internal
  artifacts.

## 0.2.9 — Analytical Owner and model-free analyst facade

Changed:

- current package markers move directly to skill 0.2.9 and core 0.3.6;
- one Analytical Owner now retains interpretation, source strategy,
  calculation, synthesis, final answer, and the one permitted business repair;
- zero to three optional specialists return bounded evidence memos while the
  owner remains responsible for the parent answer;
- new AnalystWorkspace and BusinessReviewAdapter APIs keep source selection,
  strict JSON serialization, artifact paths, hashes, repair scope, and
  lifecycle program-owned;
- Business Reviewers return semantic answer-section/category findings instead
  of JSON pointers or artifact paths;
- integration now rejects opaque string payloads and reload-validates durable
  fidelity checked IDs, record references, and baselines before commit;
- the three-question canned cassette exercises the new owner/specialist/review
  boundaries repeatedly with zero model, agent, and network calls.

## 0.2.8 — Later-item catalog inheritance

Changed:

- current package markers move directly to skill `0.2.8` and core `0.3.5`;
- public `BoundAnalysisContext.create_from_transitioned_catalog(...)` now
  documents immutable source inheritance for later and multi-hop items rather
  than synthetic transitions, preserving original source/catalog/stat/
  inventory identity without ZIP/member reads, catalog rebuilds, counters, or
  raw-source reads;
- recursive upstream provenance uses inherited journals oldest first, then the
  target journal, run lifecycle, and lexical source/target item locks;
- target intent/manifest/inheritance-record/state phases recover idempotently
  after a crash, exact retries emit no synthetic target transition audit, and
  `earliest_affected_item` remains a lower bound for later items.

## 0.2.7 — Resumable implementation-context transitions

Changed:

- the current package markers move directly to skill `0.2.7` and core `0.3.4`;
- public `BoundAnalysisContext.rebind_implementation(...)` now documents and
  enforces a contiguous implementation-transition ledger, preserving the
  bound source/catalog/stat/inventory and changing identity only;
- rebind uses durable intent, append-only audit, anchored heads, and crash
  recovery under journal → run → item lock order, with no ZIP/member reads,
  catalog rebuild, counter increments, or false telemetry;
- rebind is allowed only for a nonterminal item with no active attempt/review,
  accepted snapshot, or terminal intent; an invalid reviewer packet must be
  discarded first, and no new analysis or raw-source read is created.

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
