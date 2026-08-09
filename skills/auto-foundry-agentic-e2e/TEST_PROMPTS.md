# Offline test prompts

These prompts are contract fixtures. They use fake rows or metadata only; do
not call a real model, benchmark, external system, network, or current
dataset. The offline dashboard and optimizer helpers follow the same rule:
dashboard rendering presents accepted values, while optimization is frozen,
passive, and run-local.

## Question Mode: clean-room queue

```text
Use `$auto-foundry-agentic-e2e` in Question Mode.

Start a fresh clean-room run using only the attached generic enterprise files
and these questions. Do not read sibling runs, previous-run caches, ontologies,
scripts, reports, dashboards, prompts, or prior agent outputs. Declare the
allowed roots and record any discarded-lane incident.

At run start, write these exact markers to structured state and the final
report:
skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.2
core_name: auto_foundry_core
core_version: 0.2.0

Build one program-owned data room/source catalog from the supplied archive and
member metadata. Keep the archive read-only. Process the questions in exactly
this order, preserving each original string:
1. Count fulfilled order lines by month from one supplied table.
2. Compare invoice totals with a second source, but retain source-local
   results when the identity link is incomplete.
3. Normalize a small supplier-status field and report exclusions.

Before each Lead Analyst invocation, create the item's durable `work`
workspace and authoritative `item_state.json`. The Lead Analyst must write a
plan/source map first, then append material findings. After each response, use
structured `artifact_progress`: continue on progress; request materialization
after the first no-progress response; stop that lane and recover from the
handoff after the second consecutive no-progress response. Do not use a
wall-time deadline or a terminalizer agent. Preserve scratch during execution
recovery; it does not consume the one business repair allowed only after a
reviewer `repair_once` verdict. Materialize `draft` and then one atomic
immutable `accepted` snapshot only when their contents exist.

Use one Lead Analyst and one reviewer per item. The reviewer should perform a
targeted source-catalog completeness search for material absence claims and
check any identity-escalation route without repeating the full analysis. Keep
telemetry passive: record invocation lane/role/route, status/errors,
artifact-before/after/counts, recovery and repair counts, source/member reads,
and core/cache facts, never raw rows or route control.

Continue after partial, blocked, unsupported, or technical outcomes. Build the
final accepted-snapshot-only dashboard after the complete queue and whole-run
freeze. Do not run Benchmark A, call a model, access a network, or publish.
```

## Requirement Mode: priority and reuse

```text
Use `$auto-foundry-agentic-e2e` in analytics-only Requirement Mode.

Treat each manager requirement below as a primary user-owned record. Preserve
original text, explicit priority, objective, expected analytical output,
expected visual output, internal tasks, dependencies, foundation dependencies,
data/ontology/prepared needs, working definitions, limits, and status.

R-001 (priority=1): Decide whether late shipments are concentrated by carrier;
the answer needs a reviewed rate table and a trend chart.
R-002 (priority=unset): Reconcile payment and invoice records; reuse any
reviewed identity mapping but report missing fields.
R-003 (priority=unset): Create a local evidence-readiness dashboard for the
first two answers; this is a visual analytics deliverable, not a new metric.
R-004 (priority=unset): Change the customer payment process automatically.

Preserve R-001 as first priority. Classify every record semantically as
analytics_in_scope, analytics_requires_missing_data, or out_of_analytics_scope
without a keyword dictionary, and record the rationale. Unprioritized records
may be ordered one at a time for observed dependency or safe reuse; do not add
a planner framework or parallel requirement wave. Shared foundation work is
traceable but is not a user requirement.

Build one data room/source catalog. Before each Lead Analyst call, create the
item workspace and authoritative state. The Lead Analyst selects relevant IDs
directly from compact source/LEM/prepared indexes, writes a plan/source map
first, and appends material findings and loadable run-local prepared assets.
There is no mandatory Navigator role or per-item Capability Catalog
compliance artifact; catalog operations may be used internally when they fit,
and custom reproducible code is allowed.

Use artifact_progress after each response. Recover execution separately from
the one business repair, preserve scratch, and write technical_failure only
after recovery routes are exhausted. Have one reviewer check source-catalog
completeness and identity escalation where material. Materialize draft and
then an atomic accepted snapshot. Apply reviewed Knowledge Delta in program
code; a no_change delta has a concrete reason. Build products only after the
whole-run freeze, then collect the deterministic read-only optimizer evidence
bundle. Do not run a model, network, benchmark, or client-business automation.
```

## Minimal fake acceptance matrix

The offline tests should demonstrate:

- v0.2.2 skill and v0.2.0 core markers in instructions and run-state;
- one data room/source catalog, read-only raw archive, and bounded metadata;
- item workspace/state creation before agent invocation;
- plan/source-map-first work, materialized draft, atomic accepted snapshot,
  and mutable scratch;
- artifact-based progress, two-strike execution recovery, no wall-time
  deadline, and recovery/repair separation;
- exact Question Mode wording/order and Requirement Mode priority semantics;
- one Lead Analyst, one reviewer, one business repair maximum, no terminalizer;
- no mandatory Navigator role or per-item catalog-compliance artifact;
- source-completeness search, identity escalation, scoped knowledge, concrete
  `no_change`, loadable prepared assets, and program-owned delta application;
- passive attempt/artifact telemetry without rows or route control;
- reviewed-output dashboard, whole-run freeze, and read-only optimizer boundary;
- prohibitions on planner frameworks, dictionaries, domain recipes, central or
  cross-run caches, parallel question waves, production apps, external calls,
  compatibility wrappers, and stale v0.2.1 current instructions.
