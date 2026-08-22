# Auto Foundry Control Center

Local, read-only-first web interface for preparing and observing Auto Foundry missions.

The complete product contract lives in [`docs/AUTO_FOUNDRY_CONTROL_CENTER_REQUIREMENTS.md`](../../docs/AUTO_FOUNDRY_CONTROL_CENTER_REQUIREMENTS.md).

## What Layer 1 includes

- Palantir-inspired Mission Graph with Planner, shared Entity Resolution Owners, requirement-level Analytical Owners, AO specialists, and DAG dependencies;
- durable event feed, file/artifact cards, synchronized inspector, and technical trace;
- run discovery beneath explicit read-only roots;
- new/existing run launch form, local file/folder staging in the browser, requirement editor, and capacity control;
- launch **draft validation only** — no start, pause, resume, reopen, append, or dispatch command exists in this layer;
- deterministic fixture for safe UI development.

No third-party Python or frontend dependency is required.

## Run safely with the fixture

From the repository root:

```bash
python3 apps/control_center/server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

The default fixture never imports or invokes `auto_foundry_core` and never writes runtime state.

## Add read-only run discovery

Pass only the smallest explicit directory that should be discoverable:

```bash
python3 apps/control_center/server.py \
  --runs-root /absolute/path/to/approved/runs
```

The server searches that root for `run_state.json`, reads a bounded tail of nearby telemetry when present, and exposes only an explicit allow-list projection. Unknown event fields, prompts, messages, model responses, data values, and raw run state are not returned. A discovered run is always marked `readOnly` and `protected` in Layer 1.

Existing-run worker capacity is read from the nearest safe `entity_resolution/state.json` when available. The UI locks that value instead of promising a different capacity.

Do not pass the protected active run to automated tests. Use the built-in fixture or a temporary directory.

## Verify

```bash
python3 -m unittest discover -s apps/control_center/tests -v
python3 -m py_compile apps/control_center/server.py
node --check apps/control_center/static/app.js
python3 -m json.tool apps/control_center/fixtures/mission.json >/dev/null
```

## HTTP surface

Read-only:

- `GET /api/health`
- `GET /api/config`
- `GET /api/runs`
- `GET /api/snapshot?run_id=...`
- `GET /api/events?run_id=...&after=...&stream=...`

Non-mutating validation:

- `POST /api/launch/validate`

Every other `/api` POST returns an error. The server binds to loopback only.

The event endpoint uses a stream-generation identifier plus byte-offset cursor. It resets safely after rotation/truncation and serves bounded incremental pages; the browser drains successive pages without truncating them to the visible activity-card limit.

## Known Layer 1 limits

- Existing durable telemetry does not always contain exact agent spawn/finish lineage. The UI omits unavailable relationships rather than inventing them.
- Run projection currently shows a generic Planner node unless explicit relationship data is available.
- File and folder selection stages metadata in the browser; it does not upload or copy data.
- Remote URL ingestion and real runtime commands are deferred until their path, lifecycle, confirmation, and receipt contracts are implemented and tested in an isolated workspace.
