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
      "reviewed_item_id": "Q-...|R-...",
      "reviewed_output_ref": "...",
      "evidence_refs": ["..."],
      "period": "...",
      "population": "...",
      "unit": "...",
      "proxy_or_limit": "..."
    }
  ]
}
```

## Required visible regions

1. overview with at least the supported KPI cards;
2. decision-flow sections grouped by business domain;
3. charts/tables only when backed by reviewed outputs;
4. partial, blocked, and evidence-readiness panels;
5. definition, period, population, unit, proxy, and limitation callouts;
6. an audit/trace link for each metric or claim.

## Build and QA contract

- Read the frozen reviewed-output manifest; never query raw sources or calculate
  a new metric while rendering.
- Keep HTML, CSS, images, and scripts local and usable offline.
- Use stable internal anchors such as `#trace-Q-001` and check every link.
- Show `review_status` and `review_strength` when review was unavailable.
- Keep blocked/unsupported claims visible as limitations, not as zeros.
- Record the manifest and link-check result in structured run state.
