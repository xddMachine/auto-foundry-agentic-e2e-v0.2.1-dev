# Dashboard chart-selection library

This library is a bounded selection guide for reviewed, offline dashboard
products. A chart is eligible only when the reviewed output supplies the grain,
labels, values, denominator (when needed), and any geometry required by the
renderer. Rendering is presentation; it never aggregates currencies, invents
joins, or turns an unavailable measure into zero.

| Family | Required grain / supplied fields | Honest use | Misuse to avoid | Domain applicability | Renderer status |
| --- | --- | --- | --- | --- | --- |
| KPI / card | One reviewed headline, unit, population | A bounded decision signal | Hiding denominator or limit | All domains | Supported |
| Horizontal bar | Ordered categories plus bounded `size` | Compare supplied same-grain categories | Missing size, mixed currencies, causal ranking | Operations, finance, procurement | Supported |
| Column | Ordered categories plus bounded `size`/height | Vertical category comparison | Implied time trend without periods | Operations, demand, policy | Supported |
| Grouped bar | Common category and explicit series grain | Side-by-side same-unit series | Merging source-local populations | Comparative source views | Supported |
| Stacked bar / 100% stacked | Mutually exclusive segments plus supplied shares | Composition within one denominator | Adding overlapping queues or shares | Status and composition | Supported when supplied shares are bounded |
| Lollipop / dot | Ordered rows plus bounded `size` | Ranked queues with lighter visual weight | Treating rank as causality | Carrier, supplier, controls | Supported |
| Bullet | Actual, target, and range on same unit | Target attainment when all fields reviewed | Inventing targets or service levels | KPI governance | Documented fallback |
| Diverging bar | Signed geometry and explicit zero baseline | Positive/negative source-local deltas | Inferring sign from display text | Forecast errors, variances | Supported |
| Waterfall | Ordered additive bridge with verified subtotal semantics | Reconciliations with authoritative bridge rows | Summing across currencies or inventing bridge steps | Finance controls | Supported from supplied boundaries |
| Donut / pie | 2–5 mutually exclusive categories, denominator, shares | Compact composition | Too many slices or implied denominator | Status, reason codes | Supported |
| Waffle | Same donut contract; 100 presentation cells | Larger, legible composition | Calling cells observations or recalculating shares | Status and coverage | Supported |
| Treemap | Hierarchical, non-overlapping positive values | Part-to-whole hierarchy | Flat queues or mixed units | Product / portfolio | Documented fallback |
| Funnel | Ordered stage populations with stage semantics | A verified process funnel | Calling missing stages zero | Process operations | Supported from supplied stage geometry |
| Line / area / slope | At least two ordered periods at one grain | Time movement or slope | Sparse snapshot as trend | Time series only | Supported; sparse input stays an explicit snapshot |
| Scatter / bubble | Row-level x/y (and optional reviewed size) | Relationship exploration at same grain | Plotting aggregates as row-level points | Supplier / forecast diagnostics | Supported |
| Histogram / box | Raw or binned distribution with explicit bin grain | Distribution shape | Treating category counts as a distribution | Quality / latency | Supported from supplied bins/five-number summaries |
| Pareto | Ordered categories plus cumulative share supplied or authorized | Prioritization with explicit cumulative basis | Computing cumulative share in renderer | Exception queues | Supported from supplied cumulative values |
| Heatmap / matrix | Two dimensions and cell values | Cross-state or cross-source matrix | Calling a one-dimensional list a matrix | State conflicts | Supported |
| Small multiples | Repeated panels with independent, labeled source scales | Source-local comparisons | Merging scales or implying common denominator | Ecommerce vs ERP | Layout metadata supported |
| Sankey / flow | Verified flow edges and weights | Material flow with complete edges | Inventing weights or hidden nodes | Supply / process flow | Documented fallback |
| Timeline / Gantt | Events with start/end and status | Schedule or case chronology | Sparse dates as duration | Operations / projects | Documented fallback |
| Control chart | Ordered time observations and reviewed limits | Process stability monitoring | Inferring limits from observations | Quality / operations | Documented fallback |
| Metric grid | Exact label/value tiles, no geometry | Source partitions and scorecards | Relative ordering, bars, FX totals | Currency partitions | Supported |
| Table | Reviewed rows/columns and provenance | Drill-down action queue | Full raw dump on primary page | All domains | Supported, collapsed |
| Network / ontology graph | Explicit nodes and directed labeled links | Stable object relationships | Current observations as definitions | Ontology | Supported for v4 projection |

## Selection rules

1. Start with the analytical question and the reviewed grain, not with a visual
   preference.
2. Keep source-local scales and currencies separate. A visually convenient
   comparison is still unsupported when denominators differ.
3. Use a direct label, value, period, population, unit, and limitation beside
   every mark. A legend is not a substitute for provenance.
4. If a required field is absent or malformed, fail closed or use an evidence-
   bound table/empty state. Never default geometry to 100%.
5. Keep unsupported families in this guide so future authors can make an
   explicit decision; do not add dead renderer code until a reviewed fixture
   qualifies.

## Reference material

- Microsoft Power BI visualization overview: https://learn.microsoft.com/en-us/power-bi/visuals/power-bi-visualizations-overview
- Tableau chart examples: https://help.tableau.com/current/pro/desktop/en-us/what_chart_example.htm
- Vega-Lite examples: https://vega.github.io/vega-lite/examples/
- SAP Fiori visualization guidance: https://help.sap.com/docs/SAP_FIORI_tools/17d50220bcd848aa854c9c182d65b699/a05d7fc1bbbf42a0ade9fb50f6b58b56.html?locale=en-US
- Palantir Object Explorer: https://www.palantir.com/docs/foundry/object-explorer/getting-started
- Palantir object relationships: https://www.palantir.com/docs/foundry/vertex/explore-object-relationships
- Palantir Ontology tab: https://www.palantir.com/docs/foundry/pilot/ontology-tab
- Windsor.ai template gallery: https://windsor.ai/template-gallery/
