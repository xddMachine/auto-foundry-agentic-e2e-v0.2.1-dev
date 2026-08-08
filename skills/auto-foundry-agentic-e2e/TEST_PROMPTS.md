# Test Prompts

## Clean-room full run

```text
Use `$auto-foundry-agentic-e2e`.

Start a completely fresh clean-room run using only the attached dataset and the supplied questions. Do not read or reuse previous runs, scripts, dashboards, reports, ontologies, caches, or agent outputs.

At run start, report the loaded skill marker. It must be:

skill_name: auto-foundry-agentic-e2e
skill_version: 0.2.0

Process the supplied questions in exact order. Produce the strongest supported answer for each. Use clearly labelled source-local definitions or working proxies when official definitions are unavailable. Preserve supported partial answers and block only unsupported parts. Continue after limited, blocked, unsupported, or technical outcomes.

Save material scripts only when created and used. Build the final dashboard, audit view, and automation-candidate report after the complete queue.
```

## Minimal three-question regression

Use:

1. a simple one-table numerical question;
2. a question with a genuine cross-source linkage gap;
3. a question requiring small derived cleaning.

Acceptance:

- at least one reviewed numerical answer;
- partial answer on the linkage-gap question;
- one cleaning result or an honest finding that cleaning is unnecessary;
- queue completion;
- non-blocking knowledge updates;
- final dashboard and automation-candidate report.
