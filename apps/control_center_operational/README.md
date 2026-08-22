# Auto Foundry Control Center — Operational

This is a separate loopback-only application layered on the existing light
Control Center prototype. It does not import or modify Planner routing,
analytics, ontology, entity-resolution policy, or the dashboard assembler.

## Start

From the repository root:

```bash
PYTHONPATH=src:. python3 -m apps.control_center_operational.server \
  --port 8768 \
  --runs-root /Users/fedorlebedev/Desktop/Dashboard \
  --source-root /Users/fedorlebedev/Desktop/Dashboard \
  --max-agents 64 \
  --enable-launch \
  --protected-run-id RUN-BENCHMARK-A-V070-ENTITY-RERUN-1 \
  --protected-run-root /Users/fedorlebedev/Desktop/Dashboard/benchmark_a_requirement_v070_entity_run_a_rerun_1
```

Open <http://127.0.0.1:8768>.

Omit `--enable-launch` for a locked observation-only server. Repeat
`--source-root`, `--protected-run-id`, and `--protected-run-root` when more
than one boundary is needed.

## Launch contract

- `Prepare launch` stages/uploads sources, validates the exact requirements and
  capacity, and writes a fingerprinted draft. It does not create or extend a
  run and does not call Codex or the network.
- `Start run` is a second explicit action. It creates a new isolated Requirement
  Mode run or appends a generation to a non-protected existing run through the
  public core APIs, then starts the local Planner process.
- Capacity is a ceiling from 1 through 64; Planner is excluded and host capacity
  may reduce physical concurrency.
- Browser files/folders, allow-listed local paths, and public unauthenticated
  HTTP(S) URLs are packaged into one immutable ZIP for a new run. Existing runs
  keep their authoritative immutable data room and reject new sources.
- Public URL downloads occur only after confirmation. DNS is resolved once and
  the connection is pinned to a validated public address while preserving the
  original Host header and HTTPS SNI/certificate verification.

## Live projection

The Mission Graph reads a bounded allow-list from durable run state, entity
resolution state, requirement workspaces, specialist records, telemetry, and
`control_center/lifecycle_events.jsonl`. Planner owns Identity and Analytical
Owner nodes; Analytical Owners own specialists; reviewers use a separate
`reviews` edge. Unknown event shapes and raw prompts/messages/model output are
never projected.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
  python3 -m unittest discover -s apps/control_center_operational/tests -v

node --check apps/control_center_operational/static/operational.js
```
