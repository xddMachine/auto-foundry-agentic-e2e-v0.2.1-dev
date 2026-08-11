# Installation and migration

These instructions describe a local replacement. The package/validator smoke
uses a temporary offline install target only; it never installs into this
repository's Python environment or the Codex runtime automatically.

The current contract is skill `0.2.8` with core `0.3.5`. Benchmark A remains
prepared but unexecuted; this document does not authorize a run, installation,
or remote operation.

## Normal runtime path

The supported integration path is deliberately small:

```python
from auto_foundry_core import (
    BoundAnalysisContext,
    ControlledScriptRunner,
    CoreRuntime,
    DataRoomWorkbench,
    ItemWorkspace,
    OperationSpec,
    RunContext,
)

context = RunContext("RUN-example", run_root, (input_root,))
workbench = DataRoomWorkbench(context, archive_path)
item = ItemWorkspace.create(context, "Q-001", original_text="Count supplied rows")
execution = CoreRuntime(context).execute(
    OperationSpec("sources.preview", parameters={"path": "rows.json", "limit": 20})
)
```

For the current item runtime, create one immutable bound context after the
canonical catalog exists.  The program writes its manifest under the item
workspace; scripts load it through `AUTO_FOUNDRY_ANALYSIS_CONTEXT` and run via
`ControlledScriptRunner`.  Coding, timeout, and dependency failures are
same-attempt feedback.  This is a path/process bound, offline runner rather
than a hostile-code sandbox; use an OS/container boundary when executing
untrusted code.

The same context bounds source reads, products, cache, telemetry, and
optimizer evidence. Public exports include `RunContext`, `DataRoom`,
`DataRoomWorkbench`, `DataRoomMember`, `DataRoomCatalogEntry`, `PreparedAsset`,
`ItemWorkspace`, `ArtifactProgress`, `ProgressDecision`, `ExecutionAttempt`,
`AcceptedSnapshot`, `CoreRuntime`, `CoreExecutionResult`, `LEMRef`, and the
immutable contracts. The old mutable `Workspace` export is removed; callers
should not keep a second root-plumbing layer. The workbench owns physical
source access and durable execution, while the Lead Analyst owns semantic
judgment. A normal run begins from the explicit task and does not require
manual authorization or an extra confirmation step.

For prepared data, the Lead Analyst writes a candidate through the bound item
context; the accepted registry stays empty until Result Integration commits the
accepted item:

```python
bound = BoundAnalysisContext.create(context, archive_path, item, workbench=workbench)
candidate = bound.save_prepared_candidate(
    "orders-prepared", rows, scope="reusable",
    transformations=("bounded_csv_read",),
)
assert bound.prepared_assets.search() == ()
# After review/acceptance, one IntegrationSession stages `candidate` and its
# accepted commit registers it exactly once, retaining its scope.
```

The candidate descriptor and bytes remain under the current item's
`work/prepared/` directory. Rejected items and technical-failure integrations
do not create accepted registry entries. Registry registration validates exact
path, hash, byte/row counts, scope, and provenance; these mechanical checks
cannot prove semantic completeness. After mechanical validation, exactly one
fresh item-only Integration Fidelity Reviewer checks the current item before
commit. The packet excludes sibling, cumulative, prior-memory, and
broad-workspace context; the same Result Integration Agent may make one
targeted repair and receives one targeted recheck.
Repair scope honors explicit dependent artifact roots and JSON fragments by
authorizing their owning artifact paths; unrelated artifact mutations remain
fail-closed.
An invalid reviewer-scope packet may be recovered only with
`ItemWorkspace.discard_business_review(...)` and an inadmissible, item-bound
`reviewer_scope` incident. The append-only hash-bound audit and atomic discard
preserve work/draft bytes, reset review and repair state, and require a new
full review; no semantic reinterpretation or compatibility fallback is used.
For an implementation change, use only
`BoundAnalysisContext.rebind_implementation(...)`: a contiguous transition
ledger and durable intent/audit/head preserve the same source, catalog, stat,
and inventory under journal → run → item lock order. It rejects active
attempt/review/terminal/accepted state (discard an invalid review first) and
does not read ZIP members, rebuild catalogs, increment counters, emit false
telemetry, create analysis, or reread raw sources.

For later or multi-hop items use only
`BoundAnalysisContext.create_from_transitioned_catalog(...)`. It performs
immutable source inheritance, not a synthetic transition: original
source/catalog/stat/inventory identity is reused with no ZIP/member discovery,
catalog rebuild, inventory counters, or raw reads. Recursive upstream
provenance uses inherited journals oldest first → target journal → run →
lexical source/target item locks. Target intent/manifest/record/state recover
idempotently after a crash and retries write no synthetic transition audit;
`earliest_affected_item` is a lower bound, so an earlier target is rejected.

The run-level physical inventory is bound once and exposed through passive
counter operations (`archive_full_hash`, `member_content_hash`,
`selected_member_read`, `catalog_created`, `catalog_reused`, and
`catalog_loaded`). Bound child contexts reuse it; call `verify_source_full()`
explicitly before final publication to detect mutation. Opaque members are
safe to copy only with explicit materialization and are not semantically
parsed. `ControlledScriptRunner` has a configurable 3600-second process guard
by default; it is not an agent reasoning or workflow wall-time deadline.
Recovery accepts only a canonical persisted `receipt_ref` whose receipt has the
active attempt and lane; unpersisted or mismatched references fail closed.

## Skill replacement (same name)

1. Build/validate `dist/auto-foundry-agentic-e2e-v0.2.8.zip` locally.
2. Inspect that the ZIP has exactly one top-level directory named
   `auto-foundry-agentic-e2e/`.
3. Back up the existing same-name directory, then replace it atomically in the
   local skills directory (normally `$CODEX_HOME/skills`, if configured):

```bash
SKILLS_DIR="$(cd "${CODEX_HOME:-$HOME/.codex}/skills" && pwd -P)"
BACKUP_DIR="${SKILLS_DIR}/auto-foundry-agentic-e2e.backup"
test -d "$SKILLS_DIR"
test -d "${SKILLS_DIR}/auto-foundry-agentic-e2e"
test ! -e "$BACKUP_DIR"
mv "${SKILLS_DIR}/auto-foundry-agentic-e2e" "$BACKUP_DIR"
unzip -q dist/auto-foundry-agentic-e2e-v0.2.8.zip -d "$SKILLS_DIR"
```

4. Verify the installed `SKILL.md` frontmatter and markers are `0.2.8` with
   core version `0.3.5`, then
   start a **fresh Codex task**. Skill discovery is refreshed at task start;
   do not assume the current task sees a changed skill.

Rollback is a replacement, not a merge. The active v0.2.8 tree must leave the
skills discovery root before the previous entrypoint is restored; otherwise a
recursive discovery scan can see two same-name skills. Keep the replacement
tree in a timestamped retained directory outside `$CODEX_HOME/skills`:

```bash
SKILLS_DIR="$(cd "${CODEX_HOME:-$HOME/.codex}/skills" && pwd -P)"
ACTIVE="${SKILLS_DIR}/auto-foundry-agentic-e2e"
BACKUP_DIR="${SKILLS_DIR}/auto-foundry-agentic-e2e.previous-backup"
ROLLBACK_ROOT="${CODEX_HOME:-$HOME/.codex}/skill-rollback-replacements"
REPLACEMENT="${ROLLBACK_ROOT}/auto-foundry-agentic-e2e-v0.2.8-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$ROLLBACK_ROOT"
test -d "$SKILLS_DIR"
test -d "$ACTIVE"
test -d "$BACKUP_DIR"
test ! -e "$REPLACEMENT"
test ! -e "$BACKUP_DIR/SKILL.md"
test -f "$BACKUP_DIR/SKILL.md.rollback-previous"
mv "$ACTIVE" "$REPLACEMENT"
mv "$BACKUP_DIR/SKILL.md.rollback-previous" "$BACKUP_DIR/SKILL.md"
mv "$BACKUP_DIR" "$ACTIVE"
```

After the final move, the only discoverable `auto-foundry-agentic-e2e` entrypoint
under `SKILLS_DIR` is the restored active path; the retained replacement is
outside that root. Keep the replacement until the fresh-task discovery check
succeeds, then remove only that explicitly named retained directory if desired.

## Core wheel replacement (same package name)

The validated wheel is `dist/auto_foundry_core-0.3.5-*.whl`. Install into an
explicit target or environment selected by the operator; do not install into
the repository or a user runtime as part of this deliverable:

```bash
TARGET="$(mktemp -d)"
python3 -m pip install --no-index --no-deps --target "$TARGET" dist/auto_foundry_core-0.3.5-*.whl
PYTHONPATH="$TARGET" python3 -c 'import auto_foundry_core; print(auto_foundry_core.__version__)'
PYTHONPATH="$TARGET" python3 -m auto_foundry_core catalog list
```

For a same-environment migration, record the existing wheel/package version,
install the new wheel with `--no-index --no-deps`, and run the import/catalog
smoke before removing the old package. Do not add fallback wrappers or mix two
same-name skill trees.

Rollback is similarly explicit: uninstall/restore the previously recorded
`auto_foundry_core` wheel in the chosen target/environment, then rerun the
version and catalog checks. If any check fails, stop and restore the backup;
do not fetch packages or use a remote index.

## Release candidate status

After the complete offline vertical proofs and full suite pass, use the status
label **v0.2.8 / core 0.3.5 — offline program validation complete for later
Benchmark A**. Benchmark A is prepared but not run by this repository task. This remains an experimental
release candidate, not a production-hardened sandbox.

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully sandboxed by this Python package. True isolation requires a separate workspace/container or host allowlist.

## Scope and publication boundary

No skill installation, core installation, external network access, push, pull
request, or remote publication is performed by this repository task. Benchmark
A is prepared only; V3/xddMachine/autofoundary artifacts are untouched.
