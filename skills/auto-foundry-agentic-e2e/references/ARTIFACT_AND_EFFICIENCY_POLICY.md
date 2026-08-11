# Artifact and efficiency policy

## Principle

Preserve work that materially supports a reviewed answer. The program owns
one data room/source catalog and one durable item workspace; do not manufacture
paperwork, empty folders, or per-capability artifacts.

## Always preserve

- original question or Requirement Mode record;
- structured run identity, mode, scope classification, and outcome;
- source-catalog and compact-index references used by the item;
- authoritative `item_state.json` and mutable `work/` handoff;
- plan and source map written before Lead Analyst analysis;
- reproducible Lead Analyst script and `ControlledScriptRunner` preflight
  checks; successful runtime receipts are `smoke` and `full`, with an optional
  second `full` receipt for deterministic comparison (a failed preflight emits
  only its `compile` or `dependency_check` receipt);
- artifact-progress before/after counts and no-progress/recovery decisions;
- evidence references, definitions, assumptions, limits, population, and
  denominator;
- material findings and any run-local prepared candidates (the accepted
  registry is populated only by the post-acceptance integration commit);
- materialized draft, Lead Analyst self-check, reviewer verdict, and any one
  business repair;
- immutable accepted answer bytes plus a separate program-owned acceptance
  envelope;
- atomic reviewed Knowledge Delta result (`promoted`,
  `promoted_with_limits`, or `no_change` with a concrete reason);
- one post-acceptance Result Integration Agent receipt and incremental API refs;
- passive attempt/artifact telemetry event references;
- dashboard/product traceability and internal-link checks.

## Preserve when created or used

Keep Python, SQL, shell, notebook, spreadsheet formula, mapping, transformed
asset, chart specification, dashboard source, command, and material output
when it affects a result or improves reproducibility. Record purpose, material
inputs and outputs, assumptions, limits, and a reproduction command. Prepared
Registry entries must point to loadable run-local assets with hash, location,
schema, grain, lineage/source IDs, scope, and effective period. Candidate bytes
and descriptors remain under the item `work/prepared/` path until accepted
integration validates exact path/hash/row/byte/scope/provenance and registers
once. Never overwrite raw evidence.

## Natural analysis trace

Use one concise trace per active item:

```text
Data-room source/member IDs and exact LEM/prepared IDs inspected
Plan and source map written before analysis
Catalog capabilities recommended/used (or gap), if any
Tools, specialists, scripts, and transformations used
Key decisions and working definitions
Population, denominator, and relationship measurements
Material findings, draft, and accepted-snapshot references
Artifact progress before/after each response
Execution-recovery and business-repair counts
Self-check and review result, including source-completeness/identity checks
Unresolved issues and limits
Approximate effort (optional)
```

The trace is observational input for the deterministic post-run evidence
collector. It does not control lifecycle state or stand in for a free-form
Optimization Agent report.

## Do not create

- empty directories or files;
- a folder for every capability that was not needed;
- a Portfolio Planner, Navigator, descriptor/typed-validation role,
  business-repair finalizer, reviewer-of-reviewer, manual terminalizer,
  second integration reviewer, per-item catalog-compliance artifact, capability
  approval tree, or finalizer artifact;
- verifier scripts that inspect prose wording to decide state;
- repeated copies of unchanged artifacts;
- scripts created only to satisfy this policy;
- broad scans unrelated to the active item;
- central ontologies, cross-run caches, planner frameworks, domain recipes, or
  business-term dictionaries;
- parallel question waves, wall-time deadline artifacts, or a second repair.
- filesystem no-progress recovery without a completed invocation receipt that
  proves lane/provider/host/process loss;

## Efficiency and reuse

Build the source catalog once, inventory sources once, then profile deeply only
for the active item. Use compact source/LEM/prepared indexes and exact IDs to
bound reads. Reuse a prepared asset only when source scope, effective period,
hash/location, schema, grain, lineage, evidence, transformations, and limits
still apply; create a requirement-scoped view when they do not. Requirement
Mode preserves explicit user priority and executes one item at a time; no
second planning workflow is needed.

The inventory counters distinguish the one initial full bind, child-context
loads without re-inventory, selected-member verification, and an explicit final
`verify_source_full()` mutation check. Opaque members remain opaque and may be
copied only through safe explicit materialization. `ControlledScriptRunner`
uses a configurable 3600-second process guard by default; it is not a workflow
or agent reasoning deadline.

## Reproducibility

For a material or repeated calculation, prefer preserved code. For a simple
calculation, record input references, formula, output, exclusions, and units.
Catalog capabilities may be recommended/used internally, but custom code is
valid when it is the clearest fit and remains reproducible. The program—not
custom question code—validates and applies reviewed Knowledge Delta records.

## Telemetry and privacy

Telemetry is append-only and passive. For each material attempt/artifact event,
store invocation lane/role/route, start/end/status/error when available,
artifact before/after/counts, execution-recovery and business-repair counts,
source/member reads, core/cache facts, literal provider/model/host/process
identity (`unavailable` when unknown), terminal reason class, and artifact
IDs. Include run-level physical-inventory operation names/counters and
canonical recovery receipt reference/hash when present—not raw business rows,
secrets, tokens, or unnecessary personal data. Do
not use telemetry to invent timing benchmarks, select a route, or alter an
answer.

## Optimizer boundary

The evidence collector may read frozen traces, telemetry, and product manifests
only after accepted snapshots and the whole-run freeze are complete. Its
evidence bundle and appendix are new read-only artifacts; it cannot mutate
code, state, LEM, prepared data, products, source files, or configuration. A
later Optimization Agent may write a separate report from that bundle, but
collector/agent failure is non-blocking. Client-business automation is out of
scope.
