# Final product, dashboard, and optimizer

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

## Normal workflow boundary

The program-owned workbench controls physical evidence access and durable
execution: it opens the supplied archive read-only, builds one bounded source
catalog, creates each item workspace and immutable `BoundAnalysisContext`
before an attempt, records artifact progress and controlled-script receipts,
and preserves recovery handoffs. One Analytical Owner owns semantic judgment:
selecting useful source/member IDs, defining and revising the analytical route,
running calculations, synthesizing optional specialist memos, interpreting
evidence, and writing the complete answer. Coding defects return to the same
owner/attempt; recovery requires a canonical persisted invocation
receipt reference/hash matching the active attempt and lane.
The workbench does not infer business meaning, and the analyst does not bypass
its path, hash, or workspace controls.

Physical binding is generation-level: each active generation reuses the
archive/member inventory and catalog from its immutable D revision, while
selected-member reads are counted separately. Contexts remain immutable within
an attempt; later uploads publish D successors at a safe boundary, and active
calculations continue against their original D. Old exact D/G bindings remain
replayable. An explicit final `verify_source_full()` catches mutation. Opaque
members remain opaque and may be copied only by safe explicit materialization.
The runner's configurable 3600-second default is a process guard, not an agent
reasoning or workflow wall-time deadline.

## Cross-item synthesis

Synthesize reviewed results only. Show common findings, compatible trends,
important concentrations, contradictions, unanswered components, evidence
gaps, limits, and safe next actions. Do not introduce a new metric or silently
re-run source analysis during synthesis.

## Local dashboard prototype

Build a static, offline local multi-page site rather than a production
application or one long report page. The Product Agent reaches the deterministic
V4 dashboard through the appropriate generation entry after the presentation
preflight and explicit V2 plan: previews use
`dashboard_delta_assembler.assemble_generation_preview`, while final products
use `dashboard_delta_assembler.assemble_generation_product`. Do not author
fixtures manually in the normal path. `index.html` is a short decision
overview; each business domain gets a focused page; ontology and evidence/audit
each get their own page. Organize by business domain and decision flow, not by
input file or requirement order. Where the reviewed outputs support it, include:

For a later append to the same logical Requirement Mode run, the active
generation is cumulative. After the new items reach the ordinary accepted and
committed-integration barrier, call
`dashboard_delta_assembler.assemble_generation_product(...)` once with the
parent receipt, explicit route, and generation-scoped V2 plan. The route names
either an existing fixture domain or a stable new domain ID/title/order; it is
never inferred from prose. The generation entry point selects the internal
delta implementation, stages a new generation namespace, copies
receipt-bound parent inputs, adds only widgets and map entries for the new
accepted/committed records, and atomically publishes a generation-specific
product manifest after the active cumulative lifecycle and all freeze markers
pass. The old
dashboard/product manifest is a rollback target and is not rewritten. A
receipt-bound site remains immutable; exact retry, affected/unchanged path
hashes, and failpoint recovery are part of the same contract. No raw/source/
work reads, analytics recomputation, model call, publication, or optimizer work
is allowed during this delta.

The host first calls the program-owned
`RequirementRunExtension.append(...)` with the full cumulative plan and the
parent state/plan hashes, then waits for the newly admitted items' accepted
bundles and committed integrations. The generation's admission plan hash is
immutable lineage; a higher-revision active replan is allowed when the receipt
and product manifest bind both admission and current live plan hashes and the
route remains explicit/cumulative. Parent state/plan files are revalidated,
and parent product-manifest bytes/ref are bound separately from parent receipt
bytes/ref. These lineage, route, state, and product asset hashes are exact
bindings.
The generation entry point hands off a fresh root product manifest for G-0001;
only its internal delta path publishes the active generation-specific product
manifest for a successor.
Pre-swap and post-swap retries are deterministic and lock-serialized. The
assembler reloads the active generation after acquiring its process/thread
lock, recursively fsyncs staged files/directories before the atomic rename,
and rejects a concurrent generation transition. The generation product-
manifest leaf is resolved lexically before staging/writing, so symlink
components fail closed. The delta receipt schema is exact and recovery
reconstructs/equality-checks every nested field, including projections,
freeze metadata, affected/unchanged paths, rollback parent, inputs, outputs,
and lineage. A post-swap retry never rewrites the receipt-bound site.

- overview and decision KPI cards;
- trends, distributions, and concentration charts;
- segment/entity tables with periods, units, and populations, collapsed by
  default as drill-down rather than primary presentation;
- partial/blocked findings and evidence-readiness gaps;
- visible working-definition, proxy, and limitation callouts;
- links from each claim/metric to the reviewed item, output, and evidence.

Prefer a chart, KPI, flow, distribution, or compact exception queue whenever a
prose block would contain several quantitative comparisons. Keep narrative to
the decision, implication, action, and essential limit. The assembler may render
static JSON/HTML supplied by accepted and committed run-local records, but it
must not calculate new business results or read raw, work, or calculation
outputs. Keep all images, styles, and scripts local; validate every page,
relative link, and anchor without network access. Use the reusable
[dashboard contract](../assets/DASHBOARD_PROTOTYPE_TEMPLATE.md), the
[Product Agent V4 assembler contract](PRODUCT_AGENT_ASSEMBLER_CONTRACT.md), and
the renderer only through the assembler's validated site path.

## Audit/trace view

Include source catalog/member IDs and allowed roots; population and denominator;
joins and coverage; document/rule scope; scripts and commands;
self-check and reviewer verdict; review availability; Knowledge Delta; passive
telemetry references; dashboard element lineage; and all limitations. Structured
JSON remains authoritative for status.

## Product check

The Product Agent first validates the V4 assembler receipt, accepted/committed
input hashes, LEM projection/export hash, prepared registry/index metadata,
telemetry metadata, output hashes, offline links, and conservative freeze
markers. `<output_root>/site` is the immutable receipt-bound site tree. After
that pre-QA check, the acceptance runner writes exactly 14 PNG captures and
`qa_report.json` to the sibling `<output_root>/qa` directory; it must never
write `site/qa`. The assembler does not create or bind QA during its build.
If the host later exposes `qa_output_ref`, it is an advisory ref to that
existing sibling artifact, not a replacement for the site binding. A
post-QA exact retry must remain idempotent while sibling QA is present, while
any mutation inside `site/` must fail closed. The Product Agent then hands only
those validated refs to the existing terminal product lifecycle. For an active
generation delta, the generation-specific product manifest is the only terminal
product marker; the parent generation's manifest cannot complete the new
generation. The program checks values, labels, periods, populations, units/currencies,
claim strength, visible limitations, offline rendering, internal links, and
traceability while integrating products. A presentation defect is fixed in the
product. A genuine analytical defect returns to the originating item. The
current or a replacement owner applies the item-local change and receives a
targeted recheck. Repeat only for material findings; do not create a second
review pipeline or a second integration reviewer.

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
