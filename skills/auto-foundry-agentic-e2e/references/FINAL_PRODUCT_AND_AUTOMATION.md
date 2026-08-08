# Final product, dashboard, and optimizer

## Product start condition

Start product work only after every supplied question or requirement has a
terminal outcome, including limited, blocked, unsupported, or technical
outcomes. Freeze the reviewed answer references, LEM snapshot, prepared-data
registry, and closed telemetry before building products.

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

Include source manifest and allowed roots; exact Navigator IDs; population and
denominator; joins and coverage; document/rule scope; scripts and commands;
self-check and reviewer verdict; review availability; Knowledge Delta; passive
telemetry references; dashboard element lineage; and all limitations. Structured
JSON remains authoritative for status.

## Product review

Use one independent Product Reviewer when available. Check values, labels,
periods, populations, units/currencies, claim strength, visible limitations,
offline rendering, internal links, and traceability. A presentation defect is
fixed in the product. A genuine analytical defect returns to the originating
item for its single permitted repair; do not create a second review pipeline.

## Post-run optimizer

The optimizer runs only after answers, LEM, prepared registry, dashboard, and
telemetry are frozen. It studies Auto Foundry workflow/substrate evidence, not
client business-process automation. It writes exactly two report artifacts:

- `optimizer/experimental_optimizer_report.md`;
- `optimizer/evidence_appendix.md`.

Each candidate or recommendation records:

```text
Observed evidence
Hypothesis
Recommendation
Expected benefit
Risk
Generality
Evidence references
```

Separate observed repetition from speculation. A recommendation may be
classified `mechanical_now`, `deterministic_after_more_runs`, `keep_agentic`,
or `do_not_automate`. No benchmark is invented, and no code, configuration,
source, state, LEM, prepared asset, or product is mutated or auto-promoted.
