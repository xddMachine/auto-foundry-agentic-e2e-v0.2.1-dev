# Product Agent V4 assembler contract

The Planner remains pure. Its `build_final_product` action is a routing
instruction, not permission to calculate, accept, freeze, or author a fixture.
After every supplied requirement has a terminal accepted/integrated result,
the host dispatches that action to the Product Agent. Before that final
boundary, `refresh_product_preview` is an advisory low-priority Product Agent
action that may render the currently usable committed subset while later
requirements continue independently.

The program-owned Auto Foundry Control Center theme and presentation shell are
canonical and invariant across newly generated dashboards. The Product Agent
supplies only reviewed content and requirement/evidence-driven chart
composition; it must not author arbitrary CSS or force a fixed chart set.

## Product Agent flow

Preview and final Product Agent actions use one durable, generation-scoped
presentation sequence. The Product Agent does not choose a low-level renderer
or a full-versus-delta route:

1. Call the public
   `dashboard_assembler.business_presentation_preflight(context,
   item_ids=..., generation_id=...)`. This is a read-only source inventory
   over accepted answer and committed-integration boundaries. It atomically
   refreshes only `extensions/<G>/dashboard_preflight/` when the source
   bindings change and returns fixture, chart-map, registry refs/hashes, and a
   V2 design inventory.
2. Compare the eligible local V2 chart recipes by question, grain, dimensions,
   measures, temporal shape, coverage, and limitations. Choose one coherent
   multi-section layout with explicit `recipe_id`, `layout`, and
   `renderer_type`; prefer a richer eligible source-bound chart over a table
   when the exact reviewed geometry supports it, and record a concise
   data/decision rationale when a table is deliberately selected. Choose one
   semantic representative per requirement/business metric/scope, keep
   integration echoes as drill-down/audit support, and populate the overview
   from admitted reviewed business signals. This is grounded choice from
   accepted business evidence, not network design research or analytical
   recomputation.
   The public inventory labels hash-bound accepted-evidence candidates with
   `accepted_evidence: true`, `accepted_evidence_candidate_kind` (`table` or
   `fact_sheet`), `accepted_evidence_pointer`, and source ref/hash metadata;
   use these fields directly rather than interpreting opaque evidence IDs.
3. Distinguish initial plan creation from same-source regeneration. When no
   accepted V2 plan exists, call the public
   `dashboard_assembler.write_business_presentation_plan(...)` (or its
   explicit V2 record route when the host supplies that route) with the
   preflight fixture/map refs, item IDs, reviewer ref, and a complete ordered
   manager selection. When the target already contains a V2 plan, call
   `dashboard_assembler.write_business_presentation_plan_v2(...)` with the
   complete new ordered `manager_entries` selection and then
   `dashboard_assembler.revise_business_presentation_plan_v2(...)` with the
   exact predecessor/successor SHA compare-and-swap bindings. This successor
   path is required even when preflight/input hashes are unchanged: presentation
   audience, manager membership/order, recipe, layout, and renderer may change,
   while every accepted fact, value, pointer, and provenance/hash binding must
   remain exact. The plan is the sole presentation-admission authority; do not
   inherit predecessor membership.
4. For `refresh_product_preview`, call
   `dashboard_delta_assembler.assemble_generation_preview(context, item_ids=...,
   presentation_plan_ref=..., output_dir="generations/<G>/preview")` exactly
   once. This preview entry selects only the committed subset and never
   publishes a terminal product. For a final action, call
   `dashboard_delta_assembler.assemble_generation_product(context, ...)`
   exactly once with that `presentation_plan_ref`; for a successor generation,
   pass the program-owned parent receipt and explicit route. The final entry
   selects the full root implementation or generation delta internally from
   lifecycle metadata.
5. Validate the returned receipt, Blueprint binding, output hashes, site
   manifest, offline links, and complete site tree. A preview then calls
   `persist_preview_manifest(...)`; a final build hands refs to the existing
   refs-only candidate/review/publication lifecycle.

The low-level full and delta assembly functions are program-owned internals of
the two generation entry points; the Product Agent must never call them
directly, author fixture/chart/manifest bytes, or infer a route from requirement
prose.

Technical source, join, coverage, and method cards default to the audit gallery.
They may enter the manager surface only with an explicit source-bound business
consequence or rationale. Every accepted requirement still receives a
meaningful decision surface or an explicit limitation, including when its
integration projection is terminally failed.

The assembler loads accepted answer bundles through public core validators,
committed integration manifests/records, the read-only public LEM projection,
the prepared registry metadata, the supervisor plan, and frozen telemetry or
product metadata needed to prove presentation preconditions. Public accepted-
bundle validation may read only the hash-bound `work/evidence.jsonl` ledger
named by the accepted manifest; it never scans arbitrary `work/` or
calculation files, re-runs analytics, calls a model, or treats accepted
prose/visual descriptors as calculation authority.

## Same-run cumulative generation delta

When a terminal Requirement Mode generation admits additional requirements,
the host keeps the same `run_id` and active lifecycle generation. It does not
reopen an accepted item or rebuild the parent product namespace. Complete the
normal acceptance and committed-integration barrier for every newly admitted
item, then call the same generation entry point once with the program-owned
parent receipt and explicit route:

```python
dashboard_delta_assembler.assemble_generation_product(
    context,
    parent_receipt_ref="products/<parent>/build_receipt.json",
    route=route,
    presentation_plan_ref="extensions/<G>/business_presentation_plan.json",
)
```

`route` is program-owned metadata, never inferred from requirement prose. An
existing business section uses its stable fixture domain ID:

```json
{"kind":"existing","group_id":"group-01"}
```

An appended business section declares its stable ID, title, and order. Multiple
new requirements in one active plan group may share that exact route through
the per-item form:

```json
{"routes": {
  "REQ-09":{"kind":"new","group_id":"fulfilment","title":"Fulfilment decisions","order":3},
  "REQ-10":{"kind":"new","group_id":"fulfilment","title":"Fulfilment decisions","order":3}
}}
```

Admission remains a core program operation. Call
`RequirementRunExtension.append(context, new_records, plan=cumulative_plan,
expected_parent_state_hash=..., expected_parent_plan_hash=...)` with a full
cumulative `RequirementExecutionPlan`; the prior records and group order must
remain unchanged and the revision must advance. Complete only the newly
admitted item workspaces and committed integrations. The active generation
metadata and its `plan_hash` retain immutable admission lineage, while the
currently persisted plan is the live planning input. A legitimate higher-
revision replan is therefore bound in the receipt/product manifest by both
`admission_plan_hash` and the current `active_plan_hash`; the explicit route
must still agree with the cumulative plan. If
assembly crashes before the directory swap, retry the same generation and
route; if it crashes after the swap, retry validates the receipt and writes
only the generation product manifest. A changed parent receipt, plan/state
hash, route, input, product asset, or parent-manifest lineage fails closed.
The generation lock serializes concurrent publishers, reloads the active
generation after lock acquisition, and rejects a generation transition before
staging or publication. The active generation's product-manifest leaf is
resolved lexically (including its parent components) immediately before any
staging/write; symlink aliases fail closed. Every staged file and directory is
recursively fsynced before the atomic directory rename; atomic JSON writes and
containing directories are fsynced before returning.
Every run-local reference is checked lexically for traversal and symlink
aliases before resolution or opening; an immediate parent product manifest
must canonically bind the exact parent product-manifest reference/bytes and the
separate parent receipt reference/generation/actual receipt hash. The child
manifest has an exact schema, required asset list, and equality-bound lineage;
alternate assets, parent manifests, or parent receipts fail closed.

The route must agree with the active cumulative plan: a group containing prior
items maps to one existing parent domain; a new-only group maps to one new
domain appended after the parent groups. Missing, ambiguous, conflicting, or
free-form routes fail before dashboard writes. The child is staged under
`products/generations/<generation-id>/dashboard`; the parent receipt, fixture,
map, registry, site, QA sibling, and product manifest remain byte-identical.
The receipt records parent/child generation lineage, new accepted/committed
input hashes, old/new LEM projection and export hashes, plan and route
bindings, exact affected and unchanged site paths, rollback parent, and
`new_analytics:false`. A new domain may alter nav-bearing pages; unrelated
domain assets remain byte-identical when renderer output is unchanged.

The candidate receipt is complete before the directory swap. Its top-level and
nested schema is exact; on retry/recovery the assembler reconstructs and
equality-checks parent/plan/state lineage, projection/freeze inputs, output
refs/hashes, input items, affected/unchanged paths, rollback parent, and
counts. A failure before
the swap leaves the parent active; a failure after the swap can retry only the
generation product manifest without rewriting the child tree. Exact retries
return the existing receipt only when parent, active-generation, route,
projection, and output hashes match; drift fails closed. Product completion is
valid only after the active generation's cumulative items are terminal and
integrated and all canonical freeze markers validate. The generation-specific
`products/generations/<generation-id>/product_manifest.json` is published only
after that check; an older terminal manifest never satisfies a later generation.
QA remains a sibling outside the receipt-bound site. The delta performs no raw,
source, work, or calculation reads, model calls, analytics recomputation,
publication, or optimizer collection.

## Receipt and handoff

The assembler writes a canonical V4 fixture, chart map, run-local chart-registry
copy, offline multi-page site, and `dashboard.assembler_receipt.v1` receipt
atomically under the new namespace. The receipt binds each accepted content and
manifest hash, committed integration hash, LEM projection/export hash,
prepared-registry and telemetry metadata hashes, output hashes, stable widget
ids, and the conservative freeze markers. It does not write or replace the
terminal `product_manifest`; existing Product Agent lifecycle/optimizer
boundaries remain the authority for that terminal product.

The Product Agent must validate the receipt, output hashes, site manifest,
offline links, and freeze-input bindings before handing refs to the existing
terminal product lifecycle. The authoritative site boundary is
`<output_root>/site`: it is immutable after assembly, and its complete
per-file/tree hash is receipt-bound. Browser QA is a subsequent sibling
artifact, not part of that site tree. The acceptance runner writes exactly
the agreed 14 PNG captures and `qa_report.json` under
`<output_root>/qa` (never `<output_root>/site/qa`). The assembler does not
create or bind that QA directory during the build; the receipt therefore does
not claim a nonexistent QA artifact. A host may expose an advisory
`qa_output_ref` after QA is actually present, but it must not be treated as a
site-tree input or freeze authority.

## Incremental preview lifecycle

The Planner offers `refresh_product_preview` when at least one requirement has
a validated committed integration boundary, the cumulative requirements are
not all terminal, and the canonical preview input fingerprint differs from
the durable preview manifest (or that manifest is missing or malformed). The
action does not change run lifecycle state and can coexist with the next
runnable requirement. It is resumable in the same
`product_agent:<run_id>:<generation_id>` session as
`build_product_candidate`.

The Product Agent first calls
`dashboard_assembler.business_presentation_preflight(...)` with the deterministic
item IDs and generation metadata, then reads its V2 design inventory and
eligible chart recipes. It compares question, grain, dimensions, measures,
temporal shape, coverage, and limitations, choosing explicit `recipe_id`,
`layout`, and `renderer_type` from the local chart library and accepted business
evidence only. Network design research and analytical recomputation are
forbidden. It persists that choice with
`dashboard_assembler.write_business_presentation_plan(...)` for an initial
plan, or with the explicit V2 successor/CAS sequence above when a plan already
exists, binding the preflight fixture and chart-map hashes. A same-source
successor is valid and may change presentation choices, but not the exact
accepted fact/provenance bindings. Only then does it call
`dashboard_delta_assembler.assemble_generation_preview(...)` once with that
plan ref, the committed item IDs, and `output_dir="generations/<G>/preview"`.
The preview entry never terminal-publishes a product; the Product Agent never
calls the low-level full or delta functions directly.

The assembler alone writes `dashboard_fixture_v4.json`,
`dashboard_blueprint_v2.json`, `site/`, and its receipt. After validating those
outputs, the Product Agent calls `persist_preview_manifest(...)`, which
atomically writes `products/generations/<G>/preview/preview_manifest.json`
with schema `dashboard.preview.v1`: `run_id`, `generation_id`,
`finalizable:false`, `input_fingerprint`, sorted `item_ids`, accepted/committed
public refs and hashes, assembly receipt/blueprint/site refs and hashes, and
`site_tree_sha256`. Optional `failed_items` and `limitations` are sorted string
lists. `site_tree_sha256` is the assembler's canonical hash of the direct
sorted `{relative_path: sha256}` `site_binding.files` map (including
`site_manifest.json`); the receipt `site_binding.tree_sha256` must match it.
The manifest never contains a ProductCandidate/ProductReview,
authorization, publication/freeze marker, raw/work ref, or volatile timestamp.

Terminal technical-failure, blocked, or unsupported requirements are retained as
sorted `failed_items`/`limitations` and do not block the usable subset. If every
terminal requirement is limited and no committed records remain, the preflight
and plan select a reviewed limited/empty-state layout; the resulting dashboard
is still non-blank and clearly marks its evidence limitation.

Malformed or stale preview output only re-enables this refresh. A failed
preview consumes the ordinary retry budget for that exact input fingerprint,
but exhaustion remains presentation-local and never emits run-level rethink or
lifecycle repair. A newly committed requirement in the same generation changes
the canonical fingerprint; preflight and the plan are refreshed in their
extension namespace, and stale item bindings, source hashes, or plan refs fail
closed rather than being silently reused. The preview namespace may then be
rebuilt atomically from the new plan. Once all requirements have terminal
product boundaries, incremental refresh is suppressed and the existing one-time
candidate → independent review → publication flow remains authoritative;
accepted candidate bytes are not regenerated after review.

## Retry and incident behavior

An exact retry against an existing namespace is idempotent only when the
receipt's accepted/committed input hashes, LEM projection, prepared-registry,
telemetry metadata, and output hashes still match. It returns the existing
receipt without rewriting products. A changed input, missing/corrupt output,
missing/modified file inside the receipt-bound `<output_root>/site`, stale
receipt, path escape, status/hash mismatch, or stale staging directory
fails closed; it must not trigger analytics or silently overwrite a product.
Files added or changed in the sibling `<output_root>/qa` directory do not
change the site binding and therefore do not invalidate a post-QA exact retry.
The host records the assembler error as a presentation technical incident and
routes repair/recovery through the existing Product Agent boundary. No
acceptance, freeze, publication, or optimizer authority is delegated to the
assembler.

## Acceptance QA handoff

After the Product Agent validates the pre-QA receipt and site, the acceptance
runner writes its 14 PNG captures and `qa_report.json` to the sibling `qa/`
directory. It must not place QA files under `site/`, because `site/` is the
immutable receipt-bound product. The runner then verifies the receipt-bound
site tree and may perform an exact assembler retry; that retry must return the
same receipt while sibling QA remains present. Any mutation, deletion, or
unexpected file under `site/` is a reproducibility failure and must be routed
as a presentation incident before terminal product lifecycle work continues.
