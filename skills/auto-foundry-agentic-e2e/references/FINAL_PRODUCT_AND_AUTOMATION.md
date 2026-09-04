# Final product, dashboard, and optimizer

## Product start condition

Start product work only after every supplied question or requirement has a
terminal outcome, including limited, blocked, unsupported, or technical
outcomes. Freeze the reviewed answer references, LEM snapshot, prepared-data
registry, and closed telemetry before building products.

While requirements are still running, the Planner may offer the separate
low-priority `refresh_product_preview` action once one requirement has a
validated committed integration boundary. It renders only that usable subset
under `products/generations/<G>/preview` and never changes run lifecycle,
freezes inputs, or gates later requirements. The Product Agent chooses a
coherent multi-section layout by comparing valid local V2 chart designs across
question, grain, dimensions, measures, temporal shape, coverage, and
limitations; this grounded choice is not network design research and never
recomputes analytical facts. The canonical `dashboard.preview.v1`
`preview_manifest.json` binds only accepted/committed public refs and hashes,
assembler receipt/blueprint/site refs and hashes, and the input fingerprint.
Its `site_tree_sha256` is the assembler's direct sorted
`{relative_path: sha256}` site-binding map hash, including `site_manifest.json`.
Malformed or failed previews remain presentation-local; a changed committed
input re-enables refresh. Once every requirement is terminal, incremental
refresh is suppressed and the one-time candidate → independent review →
publication flow below is authoritative.

## Product Agent presentation flow

Both `refresh_product_preview` and the final Product Agent actions use the same
generation-scoped presentation sequence:

1. Call the public
   `dashboard_assembler.business_presentation_preflight(context,
   item_ids=..., generation_id=...)` to build or validate the deterministic
   accepted/committed V2 inventory under `extensions/<G>/dashboard_preflight/`.
2. Compare valid local chart recipes by question, grain, dimensions, measures,
   temporal shape, coverage, and limitations. Choose a coherent multi-section
   layout with explicit `recipe_id`, `layout`, and `renderer_type`; this is
   grounded selection from accepted business evidence, never network design
   research or analytical recomputation.
3. Distinguish initial creation from same-source regeneration. If the
   `extensions/<G>/business_presentation_plan.json` target is absent, call
   `dashboard_assembler.write_business_presentation_plan(...)` (or the
   explicit V2 record route when required) with a complete ordered
   `manager_entries` selection. If it already contains a V2 plan, call
   `dashboard_assembler.write_business_presentation_plan_v2(...)` with a
   complete new ordered selection, then
   `dashboard_assembler.revise_business_presentation_plan_v2(...)` with exact
   predecessor/successor SHA CAS, even when preflight/input hashes are
   unchanged. Presentation audience, manager membership/order, recipe, layout,
   and renderer may change; accepted facts, pointers, values, and provenance
   bindings must remain exact. `record_business_presentation_plan_v2` is an
   absent-target recorder only. This plan is the sole presentation-admission
   authority.
4. For `refresh_product_preview`, call
   `dashboard_delta_assembler.assemble_generation_preview(context, item_ids=...,
   presentation_plan_ref=..., output_dir="generations/<G>/preview")` exactly
   once. This entry renders only the committed subset and never terminal-
   publishes a product. For final actions, call
   `dashboard_delta_assembler.assemble_generation_product(context, ...)` once
   with the plan ref; successor generations pass the program-owned parent
   receipt and explicit route. The final entry selects its internal full or
   delta implementation from lifecycle metadata.
5. Validate receipt, standalone Blueprint v2 binding, output hashes, site
   manifest, offline links, and complete site-tree hash. A preview then writes
   `preview_manifest.json`; a final build hands refs to the existing refs-only
   candidate → independent review → publication flow. Candidate bytes are
   never regenerated after review.

The Product Agent never calls the low-level full/delta functions directly,
edits fixture/chart/manifest bytes, infers routes from requirement prose, or
freezes/authorizes/publishes outside the existing lifecycle.

When a newly committed item grows the same active generation, its canonical
input fingerprint changes. Re-run preflight and replace only the
generation-scoped extension inventory/plan; stale item bindings, source hashes,
or plan refs fail closed rather than being reused. The preview namespace may
then be rebuilt atomically from the new plan. Once all requirements are
terminal, no incremental refresh is offered.

Terminal technical-failure, blocked, unsupported, or no-record requirements are
kept as explicit limitations. If all terminal requirements are limited, the
preflight/plan chooses a reviewed limited/empty-state view so the dashboard is
non-blank without fabricating analytical facts.

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
