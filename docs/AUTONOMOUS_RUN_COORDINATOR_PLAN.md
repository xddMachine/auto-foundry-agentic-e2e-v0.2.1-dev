# Autonomous Run Coordinator

The coordinator is a small event-driven launcher, not another planner or
reviewer. `RequirementPlannerProvider.next_actions()` is the authoritative,
ordered ready set. The coordinator preserves that order, launches every
nonconflicting action in a bounded worker pool, and asks Planner for a fresh
ready set whenever a role exits.

```text
ordered Planner ready set
        |
        v
ordered dispatch records (one per role/subject/action slot)
        |
        +--> role process A (public APIs, outside the lock)
        +--> role process B (public APIs, outside the lock)
        |
        v
first completion -> transport diagnostic + fresh Planner read
```

A waiting Analytical Owner requirement does not occupy the resolver lane. Thus
identity-domain actions and the next Analytical Owner action can overlap, and a
newly released action is launched while unrelated long work is still running.

## Public role transport

Every role receives a plain-text prompt containing one `PlannerAction`, the run
root, and a deterministic idempotency key. Role stdout, stderr, exit code, and
timeout are transport diagnostics only. The coordinator never parses a result
envelope and never treats a role claim as phase success.

Roles use the existing public APIs:

- Analytical Owners bind `ItemWorkspace` and `AnalystWorkspace`, persist the
  semantic plan/scope/evidence, submit an answer or bounded insufficiency
  conclusion, and finish the attempt after its draft is durable.
- Business reviewers record an independent review; finalization uses the public
  item finalizer.
- Entity-resolution owners/reviewers use their public reservation, result, and
  review APIs.
- Integration, product, and reporting roles use their existing typed sessions
  and finalizers. The coordinator does not synthesize phase transitions.

## Durable state

The run root contains exactly one coordinator lock:
`control_plane/.coordinator.lock`. It protects atomic state/checkpoint and
append-only event writes only; role processes never run while it is held.

`coordinator_state.json` stores the run and generation IDs, Planner binding,
status, diagnostics, the last action, and an `active_dispatches` list. Each
entry contains the public action, its slot key, idempotency key, and a minimal
`runner_id`/`runner_pid` claim. Claims are made under the same control-plane
lock: an unclaimed entry or one whose PID is provably dead can be adopted, but
a live PID is skipped. If a process crashes, a later coordinator can safely
redispatch the entry with the same key; public role APIs own idempotent commit
behavior. There is no TTL or heartbeat.

Events retain the small hash-chain/replay substrate:

- `run_started`
- `dispatch_started` and `dispatch_claimed`
- `dispatch_claimed` and `dispatch_claims_cleared`
- `role_exit`
- `planner_advanced` or `wait`
- `reopen`
- `legacy_imported`
- `run_completed`

No scheduler journal, second decision-maker, admission lock, or role-result
authority is part of the control plane.

## Reconciliation

`run()` and `step()` read the complete ready set, register all non-running
slots in Planner order, release the lock, and wait for `FIRST_COMPLETED`.

After each completed role:

- the active entry is removed and ordinary transport diagnostics are recorded;
- Planner is read again immediately;
- actions that are no longer offered are ordinary Planner progress;
- an unchanged role/subject/action slot is `no_progress` (or
  `role_transport_failure`) and remains retryable;
- newly offered, nonconflicting actions are launched immediately, even while
  older futures continue.

Duplicate ready-set entries for the same `(role, subject, action)` slot are
ignored. One failed or unchanged action never erases another completed action.

An empty ready set is terminal only when the public phase snapshot reports a
valid `complete` or `complete_with_limits` run. Empty but incomplete, paused, or
invalid state is `waiting`.

## Lifecycle and CLI

`start` accepts the initial spec. `step`, `run`, `resume`, `status`, and
`reopen` reconstruct the canonical persisted spec automatically. A canonical
G5 control plane with the exact legacy wrapper is surfaced as
`legacy_import_required`; `reopen` archives the legacy specification/state/event
bytes under `control_plane/legacy_import/`, but first audits the known
newline-canonical event/state hashes, contiguous previous-hash chain, and
checkpoint tail. Any mismatch aborts before archive or current-state writes.
On success it records the archived hashes in a new current-format event, writes
a flat current spec, and rebuilds an empty ready dispatch map. The legacy event
log is never replayed. Repeating `reopen` after import is idempotent. For a
current-format run, `reopen` remains a simple
event/state reset for an idle waiting or terminal coordinator and clears only
dead dispatch claims.

CLI exit codes are stable:

- `0`: terminal `complete` or `complete_with_limits`;
- `2`: true failed/blocked/rethink result;
- `3`: waiting, ready/dispatching, or bounded work that needs another call.

Use:

```text
auto_foundry_core coordinator start --spec SPEC
auto_foundry_core coordinator step|run|resume|status|watchdog
auto_foundry_core coordinator reopen --reason "why"
```
