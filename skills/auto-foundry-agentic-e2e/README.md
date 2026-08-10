# Auto Foundry Agentic E2E Skill v0.2.3

`auto-foundry-agentic-e2e` is a natural, reviewed, offline-friendly workflow
for turning supplied enterprise evidence into bounded answers and a traceable
management dashboard prototype. The v0.2.3 contract is **Agent Workbench +
Durable Execution**: the program owns one data room/source catalog and one
durable item workspace before analysis, while the Lead Analyst remains free to
choose the useful analytical route.

## Run markers

Every run records:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.3
core_name: auto_foundry_core
core_version: 0.3.0
```

The normal program path is `RunContext` + `DataRoomWorkbench` +
`ItemWorkspace` + immutable `BoundAnalysisContext`; `CoreRuntime` remains
available for deterministic operations. The program owns lifecycle,
integration/product/optimizer transitions, strict freeze markers, and terminal
state. Agents never hand-edit terminal state.

## Choose one mode

- **Question Mode** preserves supplied wording and order, processes one
  question at a time, and never runs a parallel question wave. The program
  creates the item workspace, state, and `BoundAnalysisContext` before one Lead
  Analyst. The analyst writes plan/script/evidence/draft;
  `ControlledScriptRunner` performs compile/dependency preflight checks, then
  emits successful runtime receipts for `smoke` and `full`; a requested
  deterministic comparison adds a second `full` receipt, while a failed
  preflight emits its `compile` or `dependency_check` receipt. Coding errors
  return to the same attempt, while execution recovery requires a completed invocation receipt
  proving lane/provider/host/process loss. The program then routes one
  reviewer, allows at most one targeted business repair plus re-review, and
  writes immutable accepted answer bytes beside a separate acceptance envelope
  before continuing. It builds the dashboard after the complete queue and
  whole-run freeze.
- **Requirement Mode** is analytics-only and keeps user-owned records and
  explicit priority semantics. It records original text, objective, expected
  analytical/visual outputs, internal and foundation dependencies, data/
  ontology/prepared needs, definitions, limits, and status. Unprioritized
  records may be ordered one at a time for observed dependencies or safe reuse;
  there is no separate planner framework or keyword dictionary.

## Agent Workbench + Durable Execution

The program builds one physical source catalog from ZIP/archive and member
metadata. Entries include bounded columns, samples/values, hashes, and
workbook sheet information when available; the raw archive remains read-only.
For each item it creates:

```text
questions/<id>/item_state.json
questions/<id>/work/
requirements/<id>/item_state.json
requirements/<id>/work/
```

The Lead Analyst writes a plan, script, source map, evidence, and draft through
the program-owned workspace. `ControlledScriptRunner` performs compile/
dependency preflight checks and emits successful runtime receipts for `smoke`
and `full`; a requested deterministic comparison adds a second `full` receipt,
while a failed preflight emits only its `compile` or `dependency_check` receipt.
`draft` appears only when materialized. Accepted answer bytes are immutable and separate from the
program-owned acceptance envelope; `work` remains mutable. Filesystem
no-progress produces `await_runtime` or `materialization_guidance`, not
recovery. Only a completed invocation receipt proving lane/provider/host/
process loss authorizes execution recovery. There is no wall-time deadline,
terminalizer agent, or per-question freeze incident.

Compact source/LEM/prepared indexes remain searchable and the Lead Analyst
selects relevant IDs directly. Catalog capabilities may be recommended or
used internally; custom reproducible code is allowed. There is no Portfolio
Planner, Navigator, descriptor/typed-validation role, business-repair
finalizer, reviewer-of-reviewer, manual terminalizer, or per-item
catalog-compliance artifact.

When exact identity overlap is absent but same-object representations are
materially plausible, the run records candidates, evidence/coverage, a
semantic identity decision, and the reviewer check before declaring a combined
relationship unavailable (or explains why the route is inapplicable). Review
also performs a targeted source-catalog completeness search for material
absence claims without repeating the full analysis.

Run-local Prepared Data Registry entries point to loadable assets and record
hash, location, schema, grain, lineage/source IDs, scope, period, and limits.
Every accepted asset is registered in a canonical catalog immutable by source
hash/core/schema; scope/reuse eligibility controls visibility only, while
samples/categories are derived views. Structured `no_change` Knowledge Deltas
include a concrete reason, and the program—not custom question code—validates/
applies reviewed deltas.

After acceptance, exactly one Result Integration Agent incrementally consumes
small program APIs for claims, metrics, limitations, evidence, prepared
assets, ontology, relationships, and dashboard facts. It performs semantic
mapping; deterministic code validates types, paths, refs, hashes, stages, and
commits. There is no prose parser, giant mandatory JSON, Integration Reviewer,
or finalizer chain.

Passive attempt/artifact telemetry records invocation lane/role/route,
start/end/status/error when available, artifact before/after/counts, recovery
and business-repair counts, source/member reads, core/cache facts, and literal
provider/model/host/process identity (`unavailable` when unknown). Terminal
reason classifier output is exactly `same_attempt_feedback`,
`business_repair`, `execution_recovery`, `abort_and_new_clean_run`, or `null`;
raw terminal reasons remain specific facts. It contains no raw rows and never
controls routing.

## Install and verify

Install this folder as the single `auto-foundry-agentic-e2e` skill directory;
do not keep an older copy beside it. Start a fresh Codex task after changing a
skill so discovery is refreshed. Run the offline contract suite from the
repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/skill/test_contract.py
git diff --check -- skills/auto-foundry-agentic-e2e tests/skill/test_contract.py
```

The contract suite inspects text, local Markdown links, JSON templates, and the
deterministic protocol harness. It never imports the analytics core, calls a model, reads a dataset, or
performs a run. Do not run Benchmark A, network calls, package builds, commits,
or pushes as part of this offline check.

## Offline helper scripts

The existing local dashboard helper renders reviewed values only, and the
development-only optimizer collector remains a deterministic, strictly
read-only observer after the whole-run freeze. Neither helper calculates new
analytics or makes a model/network call. Their detailed contracts remain in
[Final product and optimizer](references/FINAL_PRODUCT_AND_AUTOMATION.md).

The supplied [test prompts](TEST_PROMPTS.md) exercise both modes without real
model calls or benchmark execution. The [run-state template](assets/RUN_STATE_TEMPLATE.json)
is an illustrative nine-field lifecycle shape; its placeholder
`manifest_hash` must be recomputed for a real run root. Queue, workbench, role,
product, optimizer, telemetry, and other artifacts are separate files, not
run-state fields. The [item-state template](assets/ITEM_STATE_TEMPLATE.json)
and [dashboard contract](assets/DASHBOARD_PROTOTYPE_TEMPLATE.md) are optional
starting points; do not create unused empty directories.

This is an offline-friendly contract, not a claim of host-level sandboxing,
Benchmark A.1 completion, or production hardening:

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully
> sandboxed by this Python package. True isolation requires a separate
> workspace/container or host allowlist.

## Reference map

- [Skill instructions](SKILL.md)
- [Question and requirement playbook](references/QUESTION_ANALYSIS_PLAYBOOK.md)
- [Knowledge and reuse](references/KNOWLEDGE_AND_REUSE.md)
- [Review protocol](references/REVIEW_PROTOCOL.md)
- [Artifact and efficiency policy](references/ARTIFACT_AND_EFFICIENCY_POLICY.md)
- [Final product and optimizer](references/FINAL_PRODUCT_AND_AUTOMATION.md)
- [Optimization follow-up template](assets/AUTOMATION_CANDIDATE_TEMPLATE.md)
- [Question result template](assets/QUESTION_RESULT_TEMPLATE.md)
- [Item state template](assets/ITEM_STATE_TEMPLATE.json)
- [Dashboard prototype contract](assets/DASHBOARD_PROTOTYPE_TEMPLATE.md)
- [Telemetry event template](assets/TELEMETRY_EVENT_TEMPLATE.json)
- [Optimizer evidence bundle template](assets/OPTIMIZER_EVIDENCE_BUNDLE_TEMPLATE.md)
- [Requirement record template](assets/REQUIREMENT_RECORD_TEMPLATE.json)
