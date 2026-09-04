# Auto Foundry Control Center — Operational

This is the single supported loopback-only Operational Control Center runtime.
It does not import or modify Planner routing, analytics, ontology,
entity-resolution policy, or the dashboard assembler.

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
  public core APIs, then starts the local Foundry Supervisor through the current
  all-path routing.
- Capacity is a ceiling from 1 through 64; Planner is excluded and host capacity
  may reduce physical concurrency.
- Browser files/folders and allow-listed local paths are packaged into one
  immutable ZIP for a new run. On an existing run, supplied sources are merged
  into a new immutable data revision (D-N): the current archive is never
  rewritten, and a source member at the same normalized path replaces it only
  in the new revision. A data-only continuation may reuse the existing plan.
  If the active attempt is not at a safe scheduler boundary, the revision is
  durable immediately and the requirement refresh is recorded as pending rather
  than starting a second supervisor.
- Public URL downloads occur only after confirmation. DNS is resolved once and
  the connection is pinned to a validated public address while preserving the
  original Host header and HTTPS SNI/certificate verification.
- The semantic intake Planner returns a schema-versioned MissionContext
  sidecar alongside analytical RequirementRecords. Product brief fields,
  source/operational context, technical constraints, and additional context
  retain exact intake/document provenance. Document-backed items must use a
  trusted catalog source binding (document hash plus normalized page/sheet/
  section locator); Planner receives bounded excerpts only. Bounded PDF, DOCX,
  ODT, CSV, and XLSX excerpts are catalogued without replacing the immutable
  raw data room. `mission_context.json`, `mission_plan.json`, and
  `document_catalog.json` are hash-bound from the launch manifest and intake
  plan, with `mission_context_active.json` as the cumulative retry pointer.

## Live projection

The Mission Graph reads a bounded allow-list from durable run state, telemetry,
coordinator/lifecycle events, and strict hash-bound sidecars. Mission context,
document catalog, role sessions, identity domains, data revisions, and reviewed
product/dashboard outputs are exposed as separate metadata projections. Raw
prompts, messages, model output, analytical data, and filesystem work paths are
never projected.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
  python3 -m unittest discover -s apps/control_center_operational/tests -v

node --check apps/control_center_operational/static/operational.js
```
