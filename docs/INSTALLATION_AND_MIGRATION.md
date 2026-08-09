# Installation and migration

These instructions describe a local replacement. The package/validator smoke
uses a temporary offline install target only; it never installs into this
repository's Python environment or the Codex runtime automatically.

## Normal runtime path

The supported integration path is deliberately small:

```python
from auto_foundry_core import DataRoomWorkbench, ItemWorkspace, CoreRuntime, OperationSpec, RunContext

context = RunContext("RUN-example", run_root, (input_root,))
workbench = DataRoomWorkbench(context, archive_path)
item = ItemWorkspace.create(context, "Q-001", original_text="Count supplied rows")
execution = CoreRuntime(context).execute(
    OperationSpec("sources.preview", parameters={"path": "rows.json", "limit": 20})
)
```

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

## Skill replacement (same name)

1. Build/validate `dist/auto-foundry-agentic-e2e-v0.2.2.zip` locally.
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
unzip -q dist/auto-foundry-agentic-e2e-v0.2.2.zip -d "$SKILLS_DIR"
```

4. Verify the installed `SKILL.md` frontmatter and markers are `0.2.2`, then
   start a **fresh Codex task**. Skill discovery is refreshed at task start;
   do not assume the current task sees a changed skill.

Rollback is a replacement, not a merge. Resolve and validate both exact
same-name paths first, send only the replacement directory to the macOS Trash,
then restore the backup:

```bash
SKILLS_DIR="$(cd "${CODEX_HOME:-$HOME/.codex}/skills" && pwd -P)"
REPLACEMENT="${SKILLS_DIR}/auto-foundry-agentic-e2e"
BACKUP_DIR="${SKILLS_DIR}/auto-foundry-agentic-e2e.backup"
test -d "$SKILLS_DIR"
test -d "$REPLACEMENT"
test -d "$BACKUP_DIR"
case "$REPLACEMENT" in
  "$SKILLS_DIR/auto-foundry-agentic-e2e") ;;
  *) printf '%s\n' "unexpected replacement path: $REPLACEMENT" >&2; exit 1 ;;
esac
test "$REPLACEMENT" != "$SKILLS_DIR"
/usr/bin/trash "$REPLACEMENT"
mv "$BACKUP_DIR" "$REPLACEMENT"
```

Keep the backup until the fresh-task discovery check succeeds. If
`/usr/bin/trash` is unavailable, stop and use a scoped, recoverable macOS
Trash operation; do not use broad recursive deletion.

## Core wheel replacement (same package name)

The validated wheel is `dist/auto_foundry_core-0.2.0-*.whl`. Install into an
explicit target or environment selected by the operator; do not install into
the repository or a user runtime as part of this deliverable:

```bash
TARGET="$(mktemp -d)"
python3 -m pip install --no-index --no-deps --target "$TARGET" dist/auto_foundry_core-0.2.0-*.whl
PYTHONPATH="$TARGET" python3 -c 'import auto_foundry_core; print(auto_foundry_core.__version__)'
PYTHONPATH="$TARGET" python3 -m auto_foundry_core catalog list
```

For a same-environment migration, record the existing wheel/package version,
install the new wheel with `--no-index --no-deps`, and run the import/catalog
smoke before removing the old package. Do not add compatibility wrappers or
mix two same-name skill trees.

Rollback is similarly explicit: uninstall/restore the previously recorded
`auto_foundry_core` wheel in the chosen target/environment, then rerun the
version and catalog checks. If any check fails, stop and restore the backup;
do not fetch packages or use a remote index.

## Release candidate status

After the complete offline vertical proofs and full suite pass, use the status
label **v0.2.2 — offline acceptance ready for later Benchmark A**. Benchmark A
is prepared but not run by this repository task. This remains an experimental
release candidate, not a production-hardened sandbox.

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully sandboxed by this Python package. True isolation requires a separate workspace/container or host allowlist.

## Scope and publication boundary

No skill installation, core installation, external network access, push, pull
request, or remote publication is performed by this repository task. Benchmark
A is prepared only; V3/xddMachine/autofoundary artifacts are untouched.
