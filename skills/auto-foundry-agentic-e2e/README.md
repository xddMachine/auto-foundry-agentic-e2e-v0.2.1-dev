# Auto Foundry Agentic E2E Skill v0.2.0

This release replaces the over-governed v0.1 workflow with a natural-analysis-first operating model.

## Important installation note

The skill name is unchanged:

```text
auto-foundry-agentic-e2e
```

Replace the previously installed folder rather than installing this package beside it. Two folders with the same skill name can cause the old version to remain active.

The new skill records this marker in every run:

```text
skill_version: 0.2.0
```

## Main changes

- analytical capabilities are adaptive, not mandatory stage gates;
- one Lead Analyst owns each question;
- one independent review per question instead of per-stage review;
- maximum one repair;
- source-local definitions and working proxies are allowed when explicit;
- partial answers are required;
- blockers affect only unsupported components;
- technical workflow defects are not data conclusions;
- question queue continues after blockers or failures;
- ontology updates are lightweight and non-blocking;
- Markdown is never parsed as authoritative lifecycle state;
- final dashboard is built after the complete queue;
- automation candidates are derived from actual observed agent work.

## Install

Replace the existing `auto-foundry-agentic-e2e` skill directory with the folder in this ZIP.

## Verify

At run start, confirm that the agent records:

```text
skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.0
```
