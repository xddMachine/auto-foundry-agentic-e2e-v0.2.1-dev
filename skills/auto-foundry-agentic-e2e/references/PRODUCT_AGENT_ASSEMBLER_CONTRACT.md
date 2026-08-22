# Product Agent V4 assembler contract

The Planner remains pure. Its `build_final_product` action is a routing
instruction, not permission to calculate, accept, freeze, or author a fixture.
After every supplied requirement has a terminal accepted/integrated result,
the host dispatches that action to the Product Agent.

## Host dispatch

The Product Agent invokes the program-owned assembler with the current run
context and a new run-local output namespace:

```text
python3 skills/auto-foundry-agentic-e2e/scripts/dashboard_assembler.py \
  --run-root <run-root> \
  --run-id <run-id> \
  --output-dir repro_dashboard_v4
```

The equivalent `assemble_dashboard(RunContext, ...)` API is allowed when the
host already has a bound `RunContext`. Explicit fixture, chart-map, registry,
site, and receipt refs, when supplied, must remain inside that output namespace.

The assembler loads accepted answer bundles through public core validators,
committed integration manifests/records, the read-only public LEM projection,
the prepared registry metadata, the supervisor plan, and frozen telemetry or
product metadata needed to prove presentation preconditions. It never reads
raw sources, `work/` or calculation outputs, re-runs analytics, calls a model,
or treats accepted prose/visual descriptors as calculation authority.

## Same-run cumulative generation delta

When a terminal Requirement Mode generation admits additional requirements,
the host keeps the same `run_id` and active lifecycle generation. It does not
reopen an accepted item or rebuild the parent product namespace. Complete the
normal acceptance and committed-integration barrier for every newly admitted
item, then dispatch the generation-specific delta assembler once with an
explicit route file:

```text
python3 skills/auto-foundry-agentic-e2e/scripts/dashboard_delta_assembler.py \
  --run-root <run-root> \
  --run-id <run-id> \
  --parent-receipt products/parent-dashboard/build_receipt.json \
  --route route.json
```

The equivalent `assemble_dashboard_delta(RunContext, parent_receipt_ref=...,
route=...)` API is allowed only with the same bindings. `route.json` is
program-owned metadata, never inferred from requirement prose. An existing
business section uses its stable fixture domain ID:

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
