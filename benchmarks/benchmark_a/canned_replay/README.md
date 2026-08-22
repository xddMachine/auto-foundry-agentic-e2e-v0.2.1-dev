# Canned analytical-role replay

This directory contains a synthetic replay cassette, not model output and not
an agent response schema. agent_outputs.json is consumed only by the
deterministic harness. Its role objects are readable conclusions; the harness
translates them through real program APIs and owns all internal JSON, paths,
pointers, hashes, receipts, lifecycle state, review packets, and integration
persistence.

The cassette has no PII, benchmark data, network result, or production claim.
Every role record is labelled deterministic_fixture_replay with unavailable
provider/model identity and zero agent, model, and network calls.

The replay exercises:

- Q-001: one Analytical Owner, no specialist, same-attempt NameError
  correction, deterministic controlled execution, business
  accept_with_limits, and fidelity acceptance;
- Q-002: one Analytical Owner, one metric/method specialist memo, two distinct
  semantic business repair_once rounds with a fresh current-draft baseline for
  the second round, and a final targeted acceptance_with_limits;
- Q-003: one Analytical Owner, two independent specialist memos, business
  acceptance, typed integration, one fidelity record repair, and targeted
  fidelity acceptance;
- the real dashboard renderer, optimizer evidence collector, lifecycle,
  reporting projector, and finalizer;
- fail-closed program probes for invalid external calls, review scope,
  fidelity checked IDs, commit ordering, duplicate invocation, and stale
  reporting.

Run from the repository root:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
      python3 -B scripts/run_canned_agent_replay.py --cycles 5

Each cycle creates a fresh temporary input and run root. The CLI prints one JSON
summary and requires a stable semantic digest across cycles. Use --output-root
PATH only when you intentionally want retained evidence.
