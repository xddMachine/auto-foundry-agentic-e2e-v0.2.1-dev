---
name: auto-foundry-agentic-e2e
description: Runs a natural, reviewed, offline-friendly enterprise analysis workflow for supplied questions or analytics-only manager requirements using a program-owned data room, durable item workspaces, artifact progress, and run-local prepared assets.
metadata:
  author: auto-foundry
  version: "0.2.2"
  core_name: auto_foundry_core
  core_version: "0.2.0"
  architecture: agent-workbench-durable-execution
  release: program-owned-data-room-and-durable-item-workspaces
---

# Auto Foundry Agentic E2E — v0.2.2

## 0. Run identity and authority

At the beginning of every run, write these exact markers to structured run
state and repeat them in the final run report:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.2
core_name: auto_foundry_core
core_version: 0.2.0
```

`run_state.json` is the lifecycle authority. `item_state.json` is the
authoritative state for one question or requirement. The data room and item
workspace are program-owned; Markdown is a human-readable view only. Never
infer lifecycle state, completion, recovery, or the next item from prose.
Keep raw sources and supplied archives read-only and put all derived work
below the current run root.

The normal program path is one `RunContext`, one `DataRoomWorkbench`, and one
`ItemWorkspace` per active item. `CoreRuntime` remains available for
deterministic catalog operations. The core records state and receipts but
cannot itself create or restart model threads; the Run Director or its host
executes the recovery decision and invokes a replacement lane when required.

## 1. Mission and modes — Agent Workbench + Durable Execution

The skill turns supplied enterprise evidence into useful, bounded analysis,
reviewed answers, reusable run-local knowledge, and a local management-facing
prototype. Analysis stays natural: choose the smallest route that answers the
active item, and do not manufacture stages or artifacts merely to satisfy a
checklist.

Use exactly one mode per run:

### Question Mode

Use when the user supplies business questions. Treat each supplied question as
a user-owned record. Preserve its original wording and order, activate one
question at a time, and do not discover extra questions or run a parallel
question wave. For every question:

1. let the program build or open the shared data room and source catalog;
2. create the question's durable `work` workspace and authoritative item state
   before invoking the Lead Analyst;
3. let one Lead Analyst choose and perform the useful work, writing a plan and
   source map before analysis and appending material findings while working;
4. use artifact progress to decide whether the lane is advancing;
5. materialize a draft, route it to one reviewer, and make at most one targeted
   business repair after a `repair_once` verdict;
6. atomically materialize an accepted snapshot, record the outcome, and
   continue to the next supplied question.

Continue after `answered_with_limits`, `partial_answer`, `null_finding`,
`blocked_by_evidence`, `unsupported`, or `technical_failure` when the next
item remains runnable. Stop the queue only for a global infrastructure failure
that makes every remaining item impossible. Build the final dashboard after
the complete supplied queue and a whole-run freeze.

### Requirement Mode (analytics-only)

Use only when the user supplies manager-style requirements. A requirement is
not a prompt to automate a client business process: it is an analytics request
whose records remain primary user-owned inputs. Record, without rewriting:

- `requirement_id` and `original_text`;
- explicit `priority` (including its source and tie handling);
- `objective` and decision context;
- expected analytical outputs and expected visual outputs;
- internal analytical tasks and their dependencies;
- shared foundation dependencies;
- data needs, ontology needs, and prepared-data needs;
- working definitions, assumptions, and limits;
- `status`, outcome, review fields, and evidence references.

Classify each record semantically as exactly one of:

- `analytics_in_scope` — evidence and an analytical path can support the
  objective;
- `analytics_requires_missing_data` — the objective is analytical, but a
  material source, field, period, definition, or prepared view is missing;
- `out_of_analytics_scope` — the request is not an analytics deliverable for
  this skill.

Preserve explicit user priority first. Among unprioritized records, the
  program may order one-at-a-time work to satisfy an observed dependency or
  safe reuse opportunity and records the rationale. Shared foundation work is
  traceable and reusable, but is never silently promoted to a user requirement.
Requirement items execute one item at a time. There is no separate planner
framework, keyword router, or business-term dictionary.

## 2. Program-owned data room and item workspaces

The program creates one physical data room for the run. It builds one
searchable source catalog from supplied ZIP/archive and member metadata before
analysis. The catalog is bounded and records, as available:

- source and member IDs, archive/member locations, formats, and byte counts;
- archive and member hashes and read timestamps;
- bounded column names/types, row and column bounds, and bounded samples or
  distinct values;
- workbook sheet names and bounded sheet metadata when applicable;
- read/error status and evidence references.

The catalog is a map, not a second raw-data store. Do not copy raw rows into it
or mutate the raw archive. A source/member read is recorded as passive
telemetry, and all later source selection uses catalog IDs and the original
read-only input.

Before invoking the Lead Analyst, the program creates exactly one durable item
workspace and authoritative state:

```text
questions/<id>/item_state.json
questions/<id>/work/
requirements/<id>/item_state.json
requirements/<id>/work/
```

`work/` is mutable scratch and the durable handoff. The Lead Analyst writes a
plan and source map first, then appends material findings, evidence references,
and run-local prepared assets during analysis. A `draft` is written only when
the answer is materially serialized; `accepted` is an atomic immutable
snapshot written only after review and any one business repair. There is no
per-question freeze or mutation incident: work remains mutable until the
accepted snapshot, while the final whole-run freeze still precedes products and
optimizer evidence collection.

## 3. Durable execution and artifact progress

After every agent response, the Run Director reads structured
`artifact_progress` from `item_state.json` and the workspace. Prose activity,
plans without files, and claims of progress do not count. The decision is:

- progress in material artifacts or counts → continue the current lane;
- first consecutive no-progress response → require materialization of the
  plan/source map or a material finding before another response;
- second consecutive no-progress response → stop that lane and recover from
  its durable handoff.

There is no wall-time deadline. Execution recovery happens before a draft is
accepted or sent for review, preserves the existing scratch and handoff, and
increments an execution-recovery count separate from the business-repair
count. The host/Run Director, not the core, creates or restarts the replacement
model thread. Recovery can resume the same item from the plan, source map,
findings, and prepared assets; it must not create a new item or rerun verified
work.

There is no terminalizer agent. Only after the allowed recovery routes are
exhausted does the program write a typed `technical_failure`; this is an
execution/tool outcome, never a conclusion about the supplied data. The one
targeted business repair remains available only after the reviewer returns
`repair_once`; execution recovery does not consume the business repair and
never pays for it.

## 4. Roles and item workflow

The Run Director owns program state, source-catalog construction, workspaces,
progress checks, recovery, review routing, accepted-snapshot materialization,
and Knowledge Delta application.

- **Lead Analyst** owns one active question or requirement, selects relevant
  IDs directly from compact source/LEM/prepared indexes, writes the plan and
  source map, and appends material findings.
- **Independent Reviewer** checks one materialized draft at the business-result
  boundary and performs the focused source-completeness and identity checks
  described below.
- **Specialists** may advise a bounded item when useful; they do not create a
  second item lifecycle.
- **Product Builder** assembles final products only from accepted snapshots.
- **Evidence Collector** is a post-run deterministic observer of workflow and
  substrate evidence. A separate fresh Optimization Agent may later reason
  from its bounded bundle.

There is no mandatory Navigator role and no per-item Capability Catalog lookup/compliance artifact. Compact indexes remain searchable; the Lead
Analyst selects exact source, ontology, and prepared IDs directly, and the
program validates those IDs before use. Catalog capabilities may be
recommended or used internally when they fit, but custom reproducible code
remains allowed.

For each item, use this progressive sequence:

```text
program builds data room/source catalog
  → program creates item_state.json and mutable work/
  → Lead Analyst writes plan and source map first
  → Lead Analyst appends findings, evidence, and loadable prepared assets
  → Run Director checks artifact_progress after each response
  → optional execution recovery from the durable handoff
  → materialized draft
  → one Independent Reviewer (source completeness and identity route included)
  → at most one targeted business repair
  → atomic accepted snapshot and final outcome
  → program validates and applies the reviewed Knowledge Delta
```

An item may finish early when evidence is sufficient. Preserve supported parts
when another part is blocked. Every plan and accepted answer should distinguish
the direct answer, expected output shape, scope and period, working
definitions, population and denominator, method, evidence references,
supported components, unsupported components, limitations, and next evidence.
Use `technical_failure` for workflow or tool defects; never turn it into a
claim about the data.

## 5. Progressive knowledge and prepared assets

The Living Enterprise Model (LEM) is run-local and progressive. It has two
linked, separately addressable layers:

1. **Enterprise Ontology** — an extensible map of business objects, fields,
   grains, relationships, rules, processes, metrics, conflicts, and known
   limitations. It is reusable understanding, not a transaction copy or a
   central ontology.
2. **Prepared Data Registry** — reusable derived assets, profiles, mappings,
   normalized values, relationship measurements, and prepared views with exact
   source/evidence references. Entries may be source-scoped or
   requirement-scoped.

Prepared Registry entries must refer to loadable run-local assets and record an
asset hash, location, schema, grain, lineage/source IDs, effective period,
transformations, evidence, and limits. A prepared asset is reusable only in
its recorded scope and period. Keep reusable preparation distinct from a
requirement-scoped view and preserve conflicting definitions rather than
overwriting them.

The compact source, ontology, and prepared indexes are searchable; both LEM
layers start empty in clean-room mode. Before reuse, check source scope,
effective period, evidence, transformations, hash/location, schema, grain,
lineage, limits, and conflicts. A reviewed Knowledge Delta is one of
`promoted`, `promoted_with_limits`, or `no_change`; `no_change` always records
a concrete reason. The program, not custom question code, validates and
applies the reviewed delta atomically.

When exact key overlap is absent but same-object representations are materially
plausible, do not immediately declare a combined relationship unavailable.
Escalate through candidate representations, evidence and coverage measures, a
semantic identity decision, and the reviewer check. If that route is
inapplicable, record why. Source-local answers remain valid when a combined
relationship is not supported.

## 6. Deterministic operations and custom work

The normal local integration path uses one immutable `RunContext`, one
`DataRoomWorkbench`, and one `ItemWorkspace`. `CoreRuntime` remains available
for deterministic operations:

```python
from auto_foundry_core import CoreRuntime, DataRoomWorkbench, ItemWorkspace, RunContext

archive_path = input_root / "supplied-fixture.zip"
context = RunContext(
    "RUN-example",
    run_root,
    (input_root,),
    core_version="0.2.0",
    skill_version="0.2.2",
)
workbench = DataRoomWorkbench(context, archive_path)
room = workbench.data_room
item = ItemWorkspace.create(
    context,
    "Q-001",
    mode="question",
    original_text="Summarize the supplied generic fixture.",
)
# CoreRuntime is optional when a deterministic operation spec is needed:
# execution = CoreRuntime(context).execute(operation_spec)
```

The exact import/operation surface comes from the installed core. It resolves
and validates run-relative paths, computes deterministic hashes, checks the
current run cache when used, invokes a bounded operation, records a receipt,
and emits passive telemetry. Core/cache facts are observed, not route control.

Custom Python, SQL, shell, notebook, spreadsheet formula, or chart code is
allowed when it is the clearest route. Preserve material code, inputs, outputs,
assumptions, and a reproduction command in the current run. Record a
capability gap when a useful catalog operation is absent; do not distort the
question, silently substitute an unrelated operation, or auto-promote custom
code.

## 7. Clean-room and path controls

For a fresh or clean-room run, create an empty run root, one empty run-local
data room/LEM, and an empty run cache before reading supplied evidence. Declare
explicit allowed roots (current run root, supplied input roots, and approved
core/tool roots) in structured state. The program and custom tools enforce
those paths. Do not read or reuse sibling runs, previous-run caches,
ontologies, scripts, reports, dashboards, hidden prompts, or prior agent
outputs. Do not copy raw data into products when a reference or derived summary
is sufficient.

If a worker or specialist reads or writes outside its allowlist, preserve the
current durable handoff, record a clean-room incident with attempted path and
disposition, and continue only with a clean replacement when safe. Prose
assertions alone are not evidence of host-level sandboxing:

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully
> sandboxed by this Python package. True isolation requires a separate
> workspace/container or host allowlist.

## 8. Review routing and disclosure

Route one reviewer per materialized item. Prefer an independent reviewer in a
fresh context; if unavailable, try an alternate independent route; if that is
unavailable, use a fresh same-family context. Do not hardcode model or
provider names. Where the host supports it, release reviewer sessions after
the verdict.

In addition to the normal answer checks, the reviewer performs a targeted source-completeness search through the physical source catalog for every
material absence claim and checks the identity-escalation route. This search
is bounded to the claim; the reviewer does not repeat the full analysis or
add a review layer.

If no reviewer can be obtained, continue with:

```json
{"review_status":"unavailable","review_strength":"none","verdict":"not_reviewed"}
```

Disclose this limitation in the item result and final report. An available
reviewer may return `accept`, `accept_with_limits`, `repair_once`, or
`block_specific_claims`; only the single permitted business repair may follow.

## 9. Final products and dashboard prototype

After every supplied question or requirement has a terminal outcome, freeze
accepted snapshot references, the LEM snapshot, loadable prepared assets, and
telemetry for product construction. The Product Builder creates a local static
dashboard prototype (not a production application) and an audit/trace view. It
must:

- use only accepted snapshots and their evidence links;
- add no new analytics or unreviewed calculations;
- organize views by business domain and decision flow, not input order;
- include multiple KPI cards, charts, and tables where supported;
- show periods, populations, units, proxy labels, limitations, blocked
  components, and evidence-readiness gaps visibly;
- use offline local assets only and validate internal links/anchors;
- expose traceability from every displayed metric or claim to its accepted
  item, output, and evidence reference.

Use the reusable [dashboard prototype contract](assets/DASHBOARD_PROTOTYPE_TEMPLATE.md)
and [offline dashboard asset](assets/dashboard.css) as a small deterministic
starting point. This is a product-building aid, not a request to build a
production dashboard.

For an executable local presentation helper, pass one `RunContext`, one
run-relative reviewed widget fixture, and products-relative output and
manifest paths to `scripts/dashboard_renderer.py`. The fixture owns display
values, trace refs, non-empty `reviewed_item_ref` and `reviewed_output_ref`
values, and at least one evidence/trace provenance reference per widget, plus
limitations and non-empty ordered domain/decision-flow metadata assigning
every widget exactly once. Every path is validated before a probe/read/mkdir/
write; the helper never reads raw sources or calculates a new metric. Its CLI
accepts `--run-root` and `--run-id`, with the fixture path relative to the run
and both outputs relative to `run_root/products`.

The development-only `scripts/optimizer_evidence_collector.py` is a
deterministic, read-only evidence collector. It requires one current
`RunContext` and a frozen products manifest with all five markers true:
`answers_frozen`, `living_enterprise_model_frozen` (or `lem_frozen`),
`prepared_assets_frozen` (or `prepared_data_registry_frozen`),
`dashboard_frozen`, and `telemetry_frozen`. A generic `frozen: true` or
products-only marker fails. It hashes run-local analytical inputs before and
after reading, records exact duplicate files, and summarizes cache/read,
reviewer, and capability facts. It writes only
`optimizer/optimizer_evidence_bundle.md` and
`optimizer/optimizer_evidence_appendix.md`; it never modifies products. The
collector makes no model call and is not the free-thinking Optimization Agent.
Use its non-blocking result in run state: a collector or later Optimization
Agent failure is `optimizer_status: technical_failure` and leaves
`analytical_complete` unchanged.

## 10. Passive attempt and artifact telemetry

Record append-only, passive telemetry for every material attempt and artifact
transition. Each event records, when available: invocation lane, role, route,
start/end timestamps, status, error class/message, item and artifact refs;
artifact progress before/after and material-artifact counts; execution-recovery
count and business-repair count; source/member reads; and core/cache facts.
Telemetry must not contain raw rows, secrets, tokens, or unnecessary personal
data. It observes the run; it never selects a route, creates a gate, restarts
a thread, or changes an answer.

Only after accepted snapshots and outcomes are frozen, the LEM and prepared
registry are frozen, the dashboard prototype is complete, and telemetry is
closed may the collector inspect workflow/substrate evidence:

```text
frozen run
  → deterministic optimizer evidence bundle
  → one fresh Optimization Agent
  → grounded free-form optimization report
```

No Optimization Agent/model call is executed by this skill helper. The
collector is strictly read-only; client-business automation remains outside
this skill. Its observed evidence may support a later hypothesis and expected
benefit, but weak evidence remains an observation gap, not an invented
benchmark. The dashboard uses reviewed outputs only.

## 11. Run workspace

Create only directories that contain an artifact. A minimal run may contain:

```text
run/
├── run_state.json
├── inputs/source_manifest.json
├── data_room/source_catalog.json
├── indexes/source_index.json
├── indexes/ontology_index.json
├── indexes/prepared_index.json
├── planning/portfolio_plan.json          # Requirement Mode only, if used
├── lem/enterprise_ontology.jsonl
├── lem/prepared_data_registry.jsonl
├── prepared/<asset-id>.<ext>              # loadable run-local assets
├── cache/                                 # current run only, when used
├── telemetry/events.jsonl                 # when events exist
├── questions/Q-001/item_state.json        # Question Mode
├── questions/Q-001/work/plan.json
├── questions/Q-001/work/source_map.json
├── questions/Q-001/work/findings.jsonl
├── questions/Q-001/draft.json             # only when materialized
├── questions/Q-001/accepted.json          # atomic immutable snapshot
├── requirements/R-001/...                 # Requirement Mode
├── products/...
└── optimizer/...                          # only after optimizer preconditions
```

`run_state.json`, item state, source/LEM records, registry records, and
telemetry use structured JSON or JSONL authority. Human-readable Markdown and
HTML are derived views. Do not create empty directories, empty stage
artifacts, or central/cross-run caches.

## 12. Constraints and release boundary

- Keep supplied Question Mode wording/order, one-at-a-time execution, and
  queue continuation. Keep Requirement Mode explicit-priority semantics.
- Use one Lead Analyst, one reviewer, and at most one targeted business
  repair per item. Execution recovery is a separate count and decision.
- Do not add a planner framework, mandatory Navigator, terminalizer, wall-time
  deadline, parallel question wave, business-term dictionary, domain recipe,
  central ontology, cross-run cache, production app, external call,
  compatibility wrapper, or a second repair.
- Do not present superseded v0.2.1 instructions as current. Do not claim that
  Benchmark A.1 has run.
- Do not mutate raw sources, prior runs, or accepted snapshots. Do not treat
  prose documents as lifecycle control.
- Do not auto-promote custom code or confuse the development-only evidence
  collector and later Optimization Agent with client business automation.

This v0.2.2 contract describes the minimal Agent Workbench + Durable
Execution path. It is an offline-friendly contract, not a claim of host-level
sandboxing, benchmark completion, or production hardening.

See the focused references for implementation detail:

- [Question and requirement playbook](references/QUESTION_ANALYSIS_PLAYBOOK.md)
- [Knowledge and reuse](references/KNOWLEDGE_AND_REUSE.md)
- [Review protocol](references/REVIEW_PROTOCOL.md)
- [Artifact and efficiency policy](references/ARTIFACT_AND_EFFICIENCY_POLICY.md)
- [Final product and optimizer](references/FINAL_PRODUCT_AND_AUTOMATION.md)
