---
name: auto-foundry-agentic-e2e
description: Runs a natural, reviewed, end-to-end enterprise analysis workflow for supplied questions or analytics-only manager requirements. It reuses a run-local Living Enterprise Model, selects bounded evidence semantically, keeps custom work reproducible, and builds a traceable offline dashboard after the queue.
metadata:
  author: auto-foundry
  version: "0.2.1"
  core_name: auto_foundry_core
  core_version: "0.1.0"
  architecture: natural-analysis-with-question-and-requirement-modes
  release: progressive-reuse-and-traceable-products
---

# Auto Foundry Agentic E2E — v0.2.1

## 0. Run identity and authority

At the beginning of every run, write these exact markers to structured run
state and repeat them in the final run report:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.1
core_name: auto_foundry_core
core_version: 0.1.0
```

`run_state.json` is the lifecycle authority. It records the run identity,
mode, allowed roots, queue/portfolio records, active item, outcomes, review
status, product status, and optimizer status. Markdown is a human-readable
view only; never infer lifecycle state, completion, or the next item from
prose. Keep raw sources read-only and put derived work below the current run
root.

## 1. Mission and modes

The skill turns supplied enterprise evidence into useful, bounded analysis,
reviewed answers, reusable run-local knowledge, and a local management-facing
prototype. Analysis stays natural: choose the smallest route that answers the
active item, and do not manufacture stages or artifacts merely to satisfy a
checklist.

Use exactly one mode per run:

### Question Mode

Use when the user supplies business questions. Treat each supplied question as
a user-owned record. Preserve its original wording and order, activate one
question at a time, and do not discover extra questions. For every question:

1. load only relevant reusable knowledge and prepared views;
2. let one Lead Analyst choose and perform the useful work;
3. run one concise Lead Analyst self-check;
4. send the complete draft to one routed Independent Reviewer;
5. make at most one targeted repair, then perform a short recheck when needed;
6. record the final outcome and continue to the next supplied question.

Continue after `answered_with_limits`, `partial_answer`, `null_finding`,
`blocked_by_evidence`, `unsupported`, or `technical_failure`. Stop the queue
only for a global infrastructure failure that makes every remaining item
impossible. Build the final dashboard after the complete supplied queue.

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

The Portfolio Planner is one semantic planning pass over the full portfolio.
It uses meaning, dependencies, evidence availability, and reusable knowledge;
it never uses a fixed business-term dictionary or a keyword router. Explicit
user priority is honored first. Among unprioritized records it may order work
to satisfy dependencies or maximize safe reuse. It records the rationale and
does a short replan between completed items. Requirement items execute one at
a time, in the planned order. Shared foundation work is traceable and
reusable, but is never silently promoted to a user requirement.

## 2. Roles and item workflow

The activating agent is the Run Director. The Run Director initializes state,
keeps sources read-only, invokes the Portfolio Planner in Requirement Mode,
and keeps the queue moving.

- **Navigator** semantically selects a bounded bundle of relevant ontology and
  prepared-data IDs from compact indexes.
- **Lead Analyst** owns one active question or requirement, chooses the natural
  route, records decisions, and writes the draft answer.
- **Specialists** may be called for bounded semantic, relationship, document,
  process, quality, cleaning, calculation, or visualization work. They advise
  the active item and do not create a second item lifecycle.
- **Independent Reviewer** checks the complete answer at the business-result
  boundary.
- **Product Builder** assembles final products only from reviewed outputs.
- **Evidence Collector** is a post-run deterministic observer of Auto Foundry
  workflow and substrate evidence. A separate fresh Optimization Agent may
  later reason from its bounded bundle.

For each Requirement Mode item, use this exact, progressive sequence:

```text
portfolio record
  → Navigator selects bounded IDs from compact indexes
  → deterministic exact-ID bundle validation
  → inspect Capability Catalog
  → concise plan
  → natural analysis
  → optional specialists, core operations, or custom reproducible code
  → one concise Lead Analyst self-check
  → one routed Independent Reviewer
  → at most one targeted repair
  → final answer and outcome
  → reviewed Knowledge Delta applied atomically by code
```

There is no capability-by-capability approval tree or separate ontology
closing role. An item may finish early when its evidence is sufficient.
Preserve supported parts when another part is blocked.

Every item plan and answer should distinguish: direct answer, expected output
shape, scope and period, working definitions, population and denominator,
method, evidence references, supported components, unsupported components,
limitations, and next evidence needed. Use `technical_failure` for workflow or
tool defects; never turn it into a claim about the data.

## 3. Progressive Living Enterprise Model

The Living Enterprise Model (LEM) is run-local and progressive. It has two
linked, separately addressable layers:

1. **Enterprise Ontology** — an extensible map of business objects, fields,
   grains, relationships, rules, processes, metrics, conflicts, and known
   limitations. It is reusable business understanding, not a transaction copy
   and not a dump of raw rows.
2. **Prepared Data Registry** — reusable derived assets, profiles, mappings,
   normalized values, relationship measurements, and prepared views with exact
   source/evidence references. A prepared asset is reusable only within its
   recorded scope and effective period.

Keep reusable preparation distinct from a requirement-scoped view. Record
source scope, effective periods, evidence, limits, transformations, and
conflicts; preserve conflicting definitions instead of overwriting them. A
review may produce `promoted`, `promoted_with_limits`, or `no_change`.
`no_change` is a valid terminal update and never blocks the queue.

The Navigator receives compact indexes first and returns exact IDs. The
Run Director validates that every returned ID exists, belongs to the current
run, is allowed for the item, and has the expected layer/type before reading
the bundle. Do not silently broaden a bundle or invent IDs. In clean-room mode
both layers start empty.

## 4. Core Capability Catalog and custom work

Before choosing a deterministic operation, inspect the local Capability Catalog
and record the catalog lookup. Use a catalog capability when it fits the task;
never distort an analytical question to fit a capability. Generic discovery and
execution forms are illustrative and intentionally do not assume unverified
implementation IDs:

```bash
python -m auto_foundry_core catalog ...
python -m auto_foundry_core run ...
```

Core operations are optional. Custom Python, SQL, shell, notebook, spreadsheet
formula, or chart code is allowed when it is the clearest route. Preserve the
code, inputs, outputs, assumptions, and a reproduction command in the current
run. Record a capability gap when the catalog lacks a needed operation; do not
silently substitute an unrelated operation or auto-promote custom code.

## 5. Clean-room and path controls

For a fresh or clean-room run, create an empty run root, empty LEM layers, and
an empty run cache before reading supplied evidence. Declare explicit allowed
roots (current run root, supplied input roots, and approved core/tool roots) in
structured state. The Run Director, Navigator, core, and custom tools enforce
those paths.

Do not read or reuse sibling runs, previous-run caches, ontologies, scripts,
reports, dashboards, hidden prompts, or prior agent outputs. Do not copy raw
data into products when a reference or derived summary is sufficient. If a
worker or specialist reads or writes outside its allowlist, discard that lane's
outputs, record a `clean_room_incident` with the attempted path and disposition,
and continue only with a clean replacement when safe. Prose assertions alone
are not evidence of host-level sandboxing.

## 6. Review routing and disclosure

Route one review per item. Prefer an independent reviewer in a fresh context;
if unavailable, try an alternate independent route; if that is unavailable,
use a fresh same-family context. Do not hardcode model or provider names. Where
the host supports it, release reviewer sessions after the verdict.

If no reviewer can be obtained, continue with:

```json
{"review_status":"unavailable","review_strength":"none","verdict":"not_reviewed"}
```

Disclose this limitation in the item result and final report. The item may
still finish as `answered_with_limits` from the Lead Analyst result; do not
claim a reviewer verdict when no reviewer was invoked. An available reviewer
may return `accept`, `accept_with_limits`, `repair_once`, or
`block_specific_claims`; only the single permitted repair may follow.

## 7. Final products and dashboard prototype

After every supplied question or requirement has a terminal outcome, freeze
the reviewed answer references, LEM snapshot, prepared registry, and telemetry
for product construction. The Product Builder creates a local static dashboard
prototype (not a production application) and an audit/trace view. It must:

- use only reviewed outputs and their evidence links;
- add no new analytics or unreviewed calculations;
- organize views by business domain and decision flow, not input order;
- include multiple KPI cards, charts, and tables where supported;
- show periods, populations, units, proxy labels, limitations, blocked
  components, and evidence-readiness gaps visibly;
- use offline local assets only and validate internal links/anchors;
- expose traceability from every displayed metric or claim to its reviewed
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
limitations and non-empty ordered domain/decision-flow metadata assigning every
widget exactly once. Every path is validated before a probe/read/mkdir/write;
the helper never reads raw sources or calculates a new metric. Its CLI accepts
`--run-root` and `--run-id`, with the fixture path relative to the run and both
outputs relative to `run_root/products`.

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

## 8. Passive telemetry and the optimizer

Record append-only, passive telemetry for material workflow events (item ID,
operation/category, timestamps, status, artifact references, errors, and
reviewer availability). Do not put raw business rows, secrets, or unnecessary
personal data in telemetry. Telemetry observes the run; it does not choose
routes, create gates, or change answers.

Only after reviewed answers and outcomes are frozen, the LEM and prepared
registry are frozen, the dashboard prototype is complete, and telemetry is
closed may the collector inspect workflow/substrate evidence:

```text
frozen run
  → deterministic optimizer evidence bundle
  → one fresh Optimization Agent
  → grounded free-form optimization report
```

No Optimization Agent/model call is executed by this skill helper. The
collector is strictly read-only: it cannot edit source, run state, LEM,
prepared data, products, code, or configuration. If either the collector or
the later agent fails, record `optimizer_status: technical_failure` and retain
the analytical completion state. Keep Auto Foundry substrate/workflow
optimization separate from client business automation; weak evidence remains
an observation gap rather than an invented benchmark.

## 9. Run workspace

Create only directories that contain an artifact. A minimal run may contain:

```text
run/
├── run_state.json
├── inputs/source_manifest.json
├── indexes/ontology_index.json
├── indexes/prepared_index.json
├── planning/portfolio_plan.json          # Requirement Mode only
├── lem/enterprise_ontology.jsonl
├── lem/prepared_data_registry.jsonl
├── cache/                                 # current run only, when used
├── telemetry/events.jsonl                 # when events exist
├── questions/Q-001/...                    # Question Mode, optional
├── requirements/R-001/...                 # Requirement Mode, optional
├── products/...
└── optimizer/...                          # only after optimizer preconditions
```

`run_state.json`, indexes, LEM records, registry records, and telemetry use
structured JSON or JSONL authority. Human-readable Markdown and HTML are
derived views. Do not create empty directories, empty stage artifacts, or
central/cross-run caches.

## 10. Constraints

- No real model calls, benchmark runs, or external publication are required by
  this skill; use offline/fake tests for contract verification.
- Do not copy raw data into the skill, embed current datasets, or add
  domain-specific code, metrics, recipes, cloud/security/product
  implementations, or deployment instructions.
- Do not create a central ontology, cross-run cache, business-term dictionary,
  mandatory helper use, wall-time deadline, extra review layer, a second review
  of a review, or a second repair.
- Do not mutate source files or prior runs, and do not treat prose documents as
  lifecycle control.
- Do not add compatibility wrappers or deprecated v0.2.0 instructions.
- Do not auto-promote custom code or confuse client business automation with
  the development-only evidence collector and later Optimization Agent.

See the focused references for implementation detail:

- [Question and requirement playbook](references/QUESTION_ANALYSIS_PLAYBOOK.md)
- [Knowledge and reuse](references/KNOWLEDGE_AND_REUSE.md)
- [Review protocol](references/REVIEW_PROTOCOL.md)
- [Artifact and efficiency policy](references/ARTIFACT_AND_EFFICIENCY_POLICY.md)
- [Final product and optimizer](references/FINAL_PRODUCT_AND_AUTOMATION.md)
