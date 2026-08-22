# Offline dashboard prototype contract

This small contract is enough to build a deterministic local static prototype.
It is not a production application and does not define new analytics.

## Manifest

Write a structured `dashboard_manifest.json` beside the prototype:

```json
{
  "product_type": "offline_static_dashboard_site",
  "site_version": 4,
  "pages": [
    "index.html",
    "domains/fulfillment.html",
    "data-quality-audit.html",
    "ontology.html",
    "evidence.html"
  ],
  "chart_led": true,
  "tables_collapsed": true,
  "overview_widget_ids": ["kpi-orders-on-time"],
  "chart_map_ref": "products/decision_dashboard_chart_map_v4.json",
  "chart_registry_ref": "products/decision_dashboard_chart_registry_v4.json",
  "chart_registry_sha256": "<sha256 of the frozen run-local registry>",
  "source_status": "reviewed_outputs_only",
  "new_analytics": false,
  "organization": "business_domain_and_decision_flow",
  "assets_local": true,
  "internal_links_checked": true,
  "freeze_markers": {
    "answers_frozen": true,
    "living_enterprise_model_frozen": true,
    "prepared_data_registry_frozen": true,
    "dashboard_frozen": true,
    "telemetry_frozen": true
  },
  "items": [
    {
      "element_id": "kpi-orders-on-time",
      "kind": "kpi|chart|table|limitation|traceability",
      "title": "...",
      "reviewed_item_ref": "Q-...|R-...",
      "reviewed_output_ref": "...",
      "evidence_refs": ["..."],
      "trace_refs": ["..."],
      "period": "...",
      "population": "...",
      "unit": "...",
      "proxy_or_limit": "..."
    }
  ]
}
```

`tables_collapsed: true` means that only complete technical/audit detail is
collapsed.  The manager surface is summary-first: one reviewed findings block,
scalar signals, semantic dashboard-fact decision views, one relationship
coverage matrix (when an explicit business implication is reviewed), and a concise limitations callout.  Internal record IDs,
field/path/row-kind/value-json projections, hashes, and evidence paths do not
appear in the default manager surface.  One `Technical audit & evidence`
disclosure per requirement retains the exact payload, trace IDs, paths, hashes,
and full rows.  Data-quality/model/ontology mechanics and relationship matrices
are additionally collected on `data-quality-audit.html`; they are not business
conclusions or default navigation cards.  No reviewed record is discarded; the
fixture and manifest keep every stable widget ID/provenance assignment exactly once.

Every v4 fixture declares one explicit `manager_admission` policy and a
generation-scoped `presentation_plan_ref`/SHA.  The plan is the sole admission
authority; there is no renderer-side lexical fallback.  Each admitted entry
binds one stable `widget_id` to exactly one committed `record_id`, the raw
`file_sha256`, the canonical payload SHA, and a `display_projection` whose
`title`, `body`/`value`, unit, denominator, period, status, or rows are
`{pointer, value}` pairs.  Pointers target only validated `/payload/...` or
`/accepted/...` JSON fields, and the recorder rejects missing pointers,
cross-record values, and invented text.  The fixture stores the exact selected
projection; the renderer displays that projection and keeps the complete raw
record in audit.  Missing/unknown plans fail closed to all-audit rather than
guessing.  Mapping, coverage, source, schema, row, identity, key, join,
namespace, ontology, connectivity, relationship, and model diagnostics remain
audit-only unless a plan explicitly binds a separable reviewed business field.

The reviewed widget fixture must also supply the exact nested `freeze_markers`
object above and non-empty, ordered `domains`.
Each domain has a unique `id`, `title`, positive contiguous `order`, and a
non-empty `decision_flow` list. Each decision-flow record has a unique `id`,
`title`, positive contiguous `order`, and non-empty `widget_ids`. Every widget
must appear exactly once in those ordered assignments. Missing, duplicate, or
unknown assignments are validation errors; the renderer does not synthesize a
domain or flow.

For chart-led v4 products, widgets may additionally carry `requirement_id`,
`requirement_title`, `requirement_order`, `requirement_subtitle`, `takeaway`,
`requirement_scope`, `requirement_limitations`, `presentation_role`,
`presentation_tier` (`primary` or `audit`), `manager_findings`, `manager_rows`,
`audit_payload`, `layout`/`span`, `overview`, `chart_notes`, and `preview`.
Requirement pages group widgets by those assignments.  Claims are aggregated
into one `finding_list`-equivalent primary widget while each original claim
widget remains an audit-tier record.  Structured metric dumps become audit-tier
support when explicit dashboard facts exist; scalar KPI/progress cards remain
primary.  Relationship records become one-row-per-relationship matrices with
source/target coverage side by side.  Only explicit `overview: true` KPIs or
reviewed headline signals appear on the overview.  A companion chart-map
artifact records the exact source/evidence paths, values used, population,
unit, period, limitations, preview scope, and palette policy for every shipped
visual; it does not add analytics.

The visual contract includes `bar`, `stacked_composition`, `heatmap`, `scatter`,
`line`, `table`, `progress`, `leaderboard`, strict `donut`,
`column`, `lollipop`, `diverging_bar`, `waffle`, and
non-comparative `metric_grid` widgets. Bar rows require an explicitly supplied,
bounded percent size; the renderer never defaults an unscaled row to 100%. A
metric grid takes exact `{label, value}` tiles and emits no bar length, area,
angle, or relative ordering. It is intended for source-currency partitions,
where labels and values remain separate and no total or FX comparison is shown.
A donut
must supply `denominator_value`, `denominator_label`, and 2–5 `categories`; each
category must contain a reviewed `label`, `value`, and percent-string `size`,
and supplied sizes must total approximately 100%. The renderer only lays out
the supplied percentages; it does not infer denominators, merge labels, or
calculate business shares. Progress widgets require supplied percent sizes;
leaderboards preserve supplied row order and labels while adding visual ranks.
Column and lollipop rows likewise require supplied bounded geometry. Diverging
bars require explicit signed percent geometry and render around a true zero
baseline; the renderer never infers sign from a display label. Waffles require
the same explicit denominator and mutually exclusive category percentages as a
donut, but use 100 presentation cells with an exact external legend.

The reusable chart-selection knowledge base is committed in
`DASHBOARD_CHART_LIBRARY.md` with its machine-readable registry in
`dashboard_chart_registry.json`; unsupported families remain documented
fallback guidance rather than dead renderer implementations.

## Required visible regions

1. a short `index.html` overview linking only to focused business-domain pages;
2. one page per business domain and decision flow, with charts preferred over
   prose blocks containing many numbers;
3. a separate `data-quality-audit.html` page for technical/data/model/ontology
   records that are not admitted to the manager surface;
4. a separate `ontology.html` page for stable definitions and a compact
   business-object graph, linked from the audit surface rather than business
   navigation;
5. a separate `evidence.html` page for provenance, limits, blocked components,
   and evidence readiness;
6. each admitted requirement has one visible reviewed-findings block when claims exist,
   a scalar signal strip, semantic decision views, one relationship matrix,
   and one `What this analysis does not prove` limitations block;
7. technical-only requirements may have no manager cards; their exact records
   remain collapsed and clickable on the data-quality audit page;
8. technical IDs, hashes, paths, field/path/row-kind/value-json columns, and
   complete raw payloads remain exact and accessible inside one collapsed
   `Technical audit & evidence` disclosure per requirement;
9. visible manager cards retain period, population, unit, denominator, and
   other supplied context without inferring rates, currency, or aggregates;
10. every original widget ID/provenance is assigned exactly once in the
   machine-readable manifest and remains traceable from the audit surface.

## Build and QA contract

- Require every widget's non-empty `reviewed_item_ref`,
  `reviewed_output_ref`, and at least one non-empty `evidence_refs` or
  `trace_refs` provenance reference before claiming the manifest is valid.
- Read the frozen reviewed-output manifest; never query raw sources or calculate
  a new metric while rendering. Top-level marker aliases and alternate freeze
  containers are invalid.
- Keep HTML, CSS, images, and scripts local and usable offline.
- Use stable internal anchors such as `#trace-Q-001` and check every page and
  fragment link.
- Render with `dashboard_renderer.py --site-output-dir dashboard`; retain the
  single-page `--output` form only for compatible micro-products.
- Show `review_status: unavailable`, `review_strength: none`, and
  `verdict: not_reviewed` when no reviewer was invoked; disclose the limit.
- Keep blocked/unsupported claims visible as limitations, not as zeros.
- Record the manifest and link-check result in structured run state.
