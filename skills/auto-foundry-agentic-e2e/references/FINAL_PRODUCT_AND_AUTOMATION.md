# Final product, dashboard, and optimizer

## Product start condition

Start product work only after every supplied question or requirement has a
terminal outcome, including limited, blocked, unsupported, or technical
outcomes. Freeze the reviewed answer references, LEM snapshot, prepared-data
registry, and closed telemetry before building products.

## Normal workflow boundary

The program-owned workbench controls physical evidence access and durable
execution: it opens the supplied archive read-only, builds one bounded source
catalog, creates each item workspace and immutable `BoundAnalysisContext`
before an attempt, records artifact progress and controlled-script receipts,
and preserves recovery handoffs. The Lead Analyst owns semantic judgment:
selecting useful source/member IDs, defining the analytical route,
interpreting evidence, and writing a bounded draft. Coding defects return to
the same analyst/attempt; recovery requires a completed invocation loss receipt.
The workbench does not infer business meaning, and the analyst does not bypass
its path, hash, or workspace controls.

## Cross-item synthesis

Synthesize reviewed results only. Show common findings, compatible trends,
important concentrations, contradictions, unanswered components, evidence
gaps, limits, and safe next actions. Do not introduce a new metric or silently
re-run source analysis during synthesis.

## Local dashboard prototype

Build a static, offline local prototype rather than a production application.
Organize the information architecture by business domain and decision flow,
not by input file or requirement order. Where the reviewed outputs support it,
include:

- overview and decision KPI cards;
- trends, distributions, and concentration charts;
- segment/entity tables with periods, units, and populations;
- partial/blocked findings and evidence-readiness gaps;
- visible working-definition, proxy, and limitation callouts;
- links from each claim/metric to the reviewed item, output, and evidence.

The prototype may render static JSON/CSV/HTML supplied by the run, but it must
not calculate new business results. Keep all images, styles, and scripts local;
validate internal links and anchors without network access. Use the reusable
[dashboard contract](../assets/DASHBOARD_PROTOTYPE_TEMPLATE.md) and
[offline style asset](../assets/dashboard.css).

## Audit/trace view

Include source catalog/member IDs and allowed roots; population and denominator;
joins and coverage; document/rule scope; scripts and commands;
self-check and reviewer verdict; review availability; Knowledge Delta; passive
telemetry references; dashboard element lineage; and all limitations. Structured
JSON remains authoritative for status.

## Product check

The program checks values, labels, periods, populations, units/currencies,
claim strength, visible limitations, offline rendering, internal links, and
traceability while integrating products. A presentation defect is fixed in the
product. A genuine analytical defect returns to the originating item for its
single permitted repair; do not create a second review pipeline or an
Integration Reviewer.

## Post-run evidence and optimization

The deterministic collector runs only after answers, LEM, prepared registry,
dashboard, and telemetry are frozen. It studies Auto Foundry
workflow/substrate evidence, not client business-process automation, and writes
exactly two run-local artifacts:

- `optimizer/optimizer_evidence_bundle.md`;
- `optimizer/optimizer_evidence_appendix.md`.

The bundle contains observed facts, exact duplicate groups, and before/after
hashes. It does not make hypotheses or recommendations. One fresh Optimization
Agent may later consume the bundle and write a grounded free-form report using:

```text
Observed evidence
Hypothesis
Recommendation
Expected benefit
Risk
Generality
Evidence references
```

No benchmark is invented, and no code, configuration, source, state, LEM,
prepared asset, or product is mutated or auto-promoted. Collector or agent
failure is recorded as `optimizer_status: technical_failure` without changing
the analytical completion state.

## Result integration boundary

Accepted answer bytes are immutable and stored separately from the
program-owned acceptance envelope and lifecycle state. After acceptance,
exactly one Result Integration Agent incrementally uses small program APIs for
claims, metrics, limitations, evidence references, prepared assets, ontology,
relationships, and dashboard facts. It performs semantic mapping; deterministic
code validates types, paths, refs, hashes, stages, and commits. Every accepted
prepared asset is registered in a canonical catalog immutable by source hash,
core version, and schema. Scope and reuse eligibility control visibility only;
samples and categories are derived views. There is no prose parser, giant
mandatory JSON, Integration Reviewer, or finalizer chain.
