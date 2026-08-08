# Auto Foundry Agentic E2E Skill v0.2.1

`auto-foundry-agentic-e2e` is a natural, reviewed, offline-friendly workflow
for turning supplied enterprise evidence into useful answers and a traceable
management dashboard prototype.

## Run markers

Every run records:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.1
core_name: auto_foundry_core
core_version: 0.1.0
```

## Choose one mode

- **Question Mode** keeps supplied wording and order, processes one question at
  a time, continues after limited or failed outcomes, gives each item one Lead
  Analyst self-check and one routed review, allows at most one repair, and
  builds the dashboard after the queue.
- **Requirement Mode** is analytics-only. A semantic Portfolio Planner sees
  the full set of manager requirements, honors explicit priority, records
  dependencies and missing evidence, can reorder only unprioritized work for
  safe reuse, replans briefly between items, and executes sequentially.

Requirement records remain user-owned. They retain original text, priority,
objective, expected analytical/visual outputs, internal tasks, dependencies,
foundation dependencies, data/ontology/prepared needs, definitions, limits,
and status. Scope is classified semantically as
`analytics_in_scope`, `analytics_requires_missing_data`, or
`out_of_analytics_scope`; no fixed keyword dictionary is used.

## What is new in 0.2.1

- progressive run-local Living Enterprise Model with separate Enterprise
  Ontology and Prepared Data Registry layers;
- semantic Navigator bundles with exact-ID validation;
- catalog-first, fit-driven use of `auto_foundry_core`, with reproducible custom
  code and capability-gap records;
- independent-review routing with explicit unavailable disclosure;
- clean-room path allowlists and discarded-lane incident records;
- reviewed-output-only local static dashboard prototype guidance and a reusable
  offline asset;
- passive workflow telemetry;
- a strictly read-only post-run optimizer report that studies Auto Foundry
  substrate/workflow evidence, not client business automation.

## Install and verify

Install this folder as the single `auto-foundry-agentic-e2e` skill directory;
do not keep an older copy beside it. Start a fresh Codex task after changing a
skill so discovery is refreshed. Run the offline contract suite from the
repository root:

```bash
python3 -m pytest -q tests/skill
git diff --check
```

The supplied [test prompts](TEST_PROMPTS.md) exercise both modes without real
model calls or benchmark execution. The [run-state template](assets/RUN_STATE_TEMPLATE.json)
and [dashboard contract](assets/DASHBOARD_PROTOTYPE_TEMPLATE.md) are optional
starting points; do not create unused empty directories.

## Reference map

- [Skill instructions](SKILL.md)
- [Question and requirement playbook](references/QUESTION_ANALYSIS_PLAYBOOK.md)
- [Knowledge and reuse](references/KNOWLEDGE_AND_REUSE.md)
- [Review protocol](references/REVIEW_PROTOCOL.md)
- [Artifact and efficiency policy](references/ARTIFACT_AND_EFFICIENCY_POLICY.md)
- [Final product and optimizer](references/FINAL_PRODUCT_AND_AUTOMATION.md)
- [Automation candidate template](assets/AUTOMATION_CANDIDATE_TEMPLATE.md)
- [Question result template](assets/QUESTION_RESULT_TEMPLATE.md)
- [Dashboard prototype contract](assets/DASHBOARD_PROTOTYPE_TEMPLATE.md)
- [Telemetry event template](assets/TELEMETRY_EVENT_TEMPLATE.json)
- [Optimizer report template](assets/EXPERIMENTAL_OPTIMIZER_REPORT_TEMPLATE.md)
- [Requirement record template](assets/REQUIREMENT_RECORD_TEMPLATE.json)
