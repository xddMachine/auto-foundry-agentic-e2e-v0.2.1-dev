# Product Agent: one presentation transaction

The Product Agent owns business presentation choices, not infrastructure. Use
`product_workspace.ProductWorkspace(context, action)` for both preview and final
candidate actions. `context` and `action` come from the coordinator dispatch.
No model call is made by this module. The agent remains responsible for interpreting
which accepted results are useful; neither the renderer nor a title dictionary
makes that decision.

```python
from product_workspace import ProductWorkspace
workspace = ProductWorkspace(context, action)
page = workspace.inventory()          # paginate via next_offset
candidate = workspace.detail(widget_id) # exact data for one view
feedback = workspace.feedback()        # independently recorded repair findings
result = workspace.build(choices, presentation={
    "title": "Operations dashboard",
    "subtitle": "Scope and reporting period",
    "section_titles": {"REQ-001": "Sales performance"},
})
```

Each choice contains `widget_id` and, for visual entries, an eligible `recipe_id`,
`renderer_type`, and `layout` from the inventory. Supply the complete ordered
manager selection on every call. Do not copy values, hashes, evidence pointers,
paths, or lineage into choices. These fields are owned by the workspace.
Choose layouts compact, half, or full only where the recipe permits them.
Optional `presentation` contains title/subtitle, `section_titles` by requirement,
`widget_titles` by selected widget, and `overview_widget_ids` as a selected subset.
These are escaped display fields, never new numerical or analytical authority.

Inspect all inventory pages. Ensure every accepted requirement has a meaningful
decision surface or an explicit source-bound limitation. Charts should match the
actual evidence shape: time series, comparisons, composition, distributions,
relationships, or nested process stages. Do not force pie charts on overlapping
categories or arbitrary numbers. Mix chart types when supported by the evidence,
not to meet a visual quota. Inspect the exact rows and independent units first.
No new analytics, causal interpretation, estimates, cross-requirement joins, or
synthetic business facts are allowed at this boundary.

## Progressive and final assembly

Preview is a cumulative view of business-accepted answers. Ontology integration
is a separate machine-reuse boundary; technical integration failure does not
remove an accepted answer. A semantic contradiction is NOT a technical failure:
it must go back to the owning analytical/review workflow, not be bypassed here.
Preview is visibly nonfinal and does not authorize publication. The final product
is assembled when all item boundaries are terminal, then independently reviewed.
New inputs require a fresh workspace. Stale item bindings fail closed.

Terminal technical-failure and blocked-by-evidence items remain disclosed. A
limited/empty-state view is valid when no supported result exists; never invent
metrics to fill blank cards. Previous accepted generations remain immutable.
A changed already-reviewed product requires the coordinator's product-revision
action. The workspace consumes its output namespace and predecessor feedback.
It cannot grant itself review acceptance or publishing authority.

## Host-owned implementation, not an agent checklist

The workspace calls `business_presentation_preflight`, prepares complete bound
entries through `write_business_presentation_plan` or its versioned CAS successor,
then chooses `assemble_generation_preview` or `assemble_generation_product`.
It validates receipts and registers the existing ProductCandidate. Exact retries
reuse the same bound result. The agent must not call these lower-level operations
separately, edit JSON artifacts, calculate hashes, author revision IDs, or patch
the engine while working on a business run.

The independent Product Reviewer must check chart geometry and units, row/column
fidelity, limitation visibility, broken links, overflow and actual useful content.
Hashes prove identity, not analytical correctness. The coordinator retains final
review, authorization, activation, and publication policy enforcement.
