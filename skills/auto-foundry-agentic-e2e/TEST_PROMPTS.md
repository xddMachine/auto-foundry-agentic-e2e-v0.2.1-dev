# Offline test prompts

These prompts are contract fixtures. They use fake rows or metadata only; do
not call a real model, benchmark, external system, or current dataset. The
offline dashboard and optimizer helper smoke fixtures follow the same rule:
dashboard rendering is presentation of reviewed values, while optimization is
explicitly frozen, passive, and run-local.

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
skill_version: 0.2.1
core_name: auto_foundry_core
core_version: 0.1.0

Process the questions in exactly this order, preserving each original string:
1. Count fulfilled order lines by month from one supplied table.
2. Compare invoice totals with a second source, but retain source-local
   results when the identity link is incomplete.
3. Normalize a small supplier-status field and report exclusions.

For each item use one Lead Analyst self-check, one routed independent review,
and at most one targeted repair. Continue after partial, blocked, unsupported,
or technical outcomes. Build the final reviewed-output-only dashboard after
the complete queue. Show periods, denominators, limitations, traceability, and
review availability. Keep telemetry passive.
```

## Requirement Mode: portfolio and reuse

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

One semantic Portfolio Planner must see the full portfolio, honor R-001's
explicit priority, classify each record as analytics_in_scope,
analytics_requires_missing_data, or out_of_analytics_scope without a keyword
dictionary, and record the rationale. It may order the unprioritized records
for dependency/reuse reasons and must replan briefly between items. Execute
one item at a time; shared foundation work is traceable but is not a user
requirement.

For each in-scope item, use Navigator-selected exact ontology/prepared IDs,
deterministic validation, a catalog inspection, concise plan, natural analysis,
optional core/custom code, self-check, one reviewer, at most one repair, final
answer, and atomically applied reviewed Knowledge Delta. Build the static
reviewed-output-only dashboard only after all items have terminal outcomes.
After answers, LEM, prepared registry, dashboard, and telemetry are frozen,
collect a deterministic optimizer evidence bundle and appendix. A separate
fresh Optimization Agent may later write a grounded free-form report; that
agent is not invoked by this offline helper. Do not mutate the run or propose
client business automation as Auto Foundry optimization.
```

## Minimal fake acceptance matrix

The offline tests should demonstrate:

- version and core markers in instructions and run-state template;
- exact Question Mode wording/order, continuation, one self-check, one review,
  and one-repair maximum;
- Requirement Mode records, semantic scope classification, explicit priority,
  whole-portfolio planning, foundation-task traceability, short replans, and
  sequential execution;
- fake-role behavior for planner, exact-ID Navigator, bounded Lead Analyst,
  unavailable-reviewer fallback, and LEM found/reuse, extend, fresh,
  conflict/supersession, and scoped-rejection cases;
- LEM layer separation, scoped preparation, conflicts/effective periods,
  `no_change`, compact indexes, and exact-ID bundles;
- catalog-first-but-optional core use and reproducible custom code;
- reviewer routing fallbacks, unavailable disclosure, and session release;
- clean-room roots, path enforcement, and discarded-lane incidents;
- dashboard reviewed-output traceability, limitations, offline assets, and no
  new analytics;
- passive telemetry plus frozen/read-only optimizer evidence-bundle fields;
- prohibitions on dictionaries, cross-run caches, approval trees, lifecycle
  prose authority, a second repair, client automation, and production apps.
