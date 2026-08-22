# Canned requirement-mode replay

This directory contains a small synthetic, non-PII milk-lot fixture. The
requirement replay consumes it as readable input and never treats the fixture
as model output.

The exact parent requirement is:

> Dashboard should show the ratio of milk fat content to the procurement price of the raw material for that milk.

Each cycle creates one `R-001` requirement workspace, persists one immutable
semantic plan with three dependent semantic tasks, runs one deterministic
controlled script, records one parent answer, accepts it with explicit limits,
and commits one metric plus one dashboard fact through `IntegrationSession`.
The replay performs an item-only fidelity acceptance and makes zero agent,
model, or network calls.

Run from the repository root:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src \
  python3 -B scripts/run_canned_requirement_replay.py --cycles 3
```

The CLI prints one JSON summary. Use `--output-root PATH` only when retained
cycle evidence is intentional.
