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
the same analyst/attempt; recovery requires a canonical persisted invocation
receipt reference/hash matching the active attempt and lane.
The workbench does not infer business meaning, and the analyst does not bypass
its path, hash, or workspace controls.

The physical binding is run-level: the initial full archive/member inventory is
counted once, child contexts reuse it, selected-member reads are counted
separately, and an explicit final `verify_source_full()` catches mutation.
Opaque members remain opaque and may be copied only by safe explicit
materialization. The runner's configurable 3600-second default is a process
guard, not an agent reasoning or workflow wall-time deadline.

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
single permitted repair; do not create a second review pipeline or a second
integration reviewer.

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
program-owned acceptance envelope and lifecycle state. Prepared data starts as
an item-local candidate under `work/prepared/`; it is absent from the accepted
registry until one post-acceptance Result Integration commit validates exact
path/hash/row/byte/scope/provenance and registers it once. Exact retries are
idempotent; conflicting same IDs fail before registry/LEM mutation, and
rejected or technical-failure items leave no accepted entry. Exactly one Result
Integration Agent incrementally uses small program APIs for claims, metrics,
limitations, evidence references, prepared assets, ontology, relationships,
and dashboard facts. It performs semantic mapping; deterministic code
validates types, paths, refs, hashes, stages, and commits. Mechanical validation
cannot prove semantic completeness. Exactly one fresh item-only Integration
Fidelity Reviewer checks the staged current item after mechanical validation and
before commit; the same Result Integration Agent may make one targeted repair
and receives one targeted recheck. The packet excludes siblings, cumulative
state, prior memory, and broad workspace context. There is no prose parser,
semantic compiler, giant mandatory JSON, or reviewer chain.
