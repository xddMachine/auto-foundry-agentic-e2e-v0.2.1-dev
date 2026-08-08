# Offline dashboard prototype contract

This small contract is enough to build a deterministic local static prototype.
It is not a production application and does not define new analytics.

## Manifest

Write a structured `dashboard_manifest.json` beside the prototype:

```json
{
  "product_type": "offline_static_dashboard",
  "source_status": "reviewed_outputs_only",
  "new_analytics": false,
  "organization": "business_domain_and_decision_flow",
  "assets_local": true,
  "internal_links_checked": true,
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

The reviewed widget fixture must also supply non-empty, ordered `domains`.
Each domain has a unique `id`, `title`, positive contiguous `order`, and a
non-empty `decision_flow` list. Each decision-flow record has a unique `id`,
`title`, positive contiguous `order`, and non-empty `widget_ids`. Every widget
must appear exactly once in those ordered assignments. Missing, duplicate, or
unknown assignments are validation errors; the renderer does not synthesize a
domain or flow.

## Required visible regions

1. overview with at least the supported KPI cards;
2. decision-flow sections grouped by business domain;
3. charts/tables only when backed by reviewed outputs;
4. partial, blocked, and evidence-readiness panels;
5. definition, period, population, unit, proxy, and limitation callouts;
6. an audit/trace link for each metric or claim.

## Build and QA contract

- Require every widget's non-empty `reviewed_item_ref`,
  `reviewed_output_ref`, and at least one non-empty `evidence_refs` or
  `trace_refs` provenance reference before claiming the manifest is valid.
- Read the frozen reviewed-output manifest; never query raw sources or calculate
  a new metric while rendering.
- Keep HTML, CSS, images, and scripts local and usable offline.
- Use stable internal anchors such as `#trace-Q-001` and check every link.
- Show `review_status: unavailable`, `review_strength: none`, and
  `verdict: not_reviewed` when no reviewer was invoked; disclose the limit.
- Keep blocked/unsupported claims visible as limitations, not as zeros.
- Record the manifest and link-check result in structured run state.
