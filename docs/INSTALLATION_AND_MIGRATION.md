# Installation and migration

These instructions describe a local replacement. The package/validator smoke
uses a temporary offline install target only; it never installs into this
repository's Python environment or the Codex runtime automatically.

The current contract is skill `0.7.1` with core `0.8.0`. Benchmark A remains
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
`AcceptedSnapshot`, `AnalystWorkspace`, `AnalystAnswer`, `EvidenceNote`,
`SpecialistTask`, `SpecialistMemo`, `ReviewFinding`, `BusinessReviewAdapter`,
`CoreRuntime`, `CoreExecutionResult`, `LEMRef`, and the
immutable contracts. The old mutable `Workspace` export is removed; callers
should not keep a second root-plumbing layer. The workbench owns physical
source access and durable execution, while one Analytical Owner owns the full
semantic loop and final answer. Analytical roles use `AnalystWorkspace`; they
do not author internal JSON, paths, hashes, or state.

Before analysis, the owner calls `brief()` and searches compact accepted
ontology/prepared descriptors with `search_ontology()` and
`search_prepared_assets()`. Useful exact IDs are selected with
`select_ontology()` or `select_prepared_assets()` and a purpose; the program
records the item-bound trace in `work/semantic_selections.jsonl`. Prepared rows
are loaded only after selection and exact registry content-hash validation via
`load_prepared_asset()`.

This is an explicit readiness/scouting pass: search/select applicable ontology,
identity mappings, relationships, and prepared semantics, or record why none
applies. If a needed identity domain is `resolving`, the Analytical Owner
reports `waiting_on_resolution` and releases its lane. The Planner skips to the
next original-order runnable item and marks the earliest paused item
`ready_to_resume` when the domain is `ready`; if all runnable items wait, the owner lane sleeps while
active resolvers progress. Block only when nothing is runnable and no resolver
progresses.

Call `RequirementSupervisorWorkspace.scheduling_tick()` after each wait,
resume, or terminal transition; it returns the host-ready next requirement and
keeps `run_state.json` reconciled with item state.

Requirement Mode uses one run-level **Planner**, an event-driven control plane
and cognitive scheduler. It receives exact `RequirementRecord` values, compact
physical catalog metadata, and current item/resolution outcomes. Initial order
and grouping are advisory and preserve explicit user priority/order; the
Planner never declares runtime semantic dependencies. The Analytical Owner
discovers those after understanding the requirement, and the runtime
`waiting_on_resolution`/`ready_to_resume` ledger is the sole semantic block.
may suggest zero to three owner specialists but does not calculate or write
answers and is not a deterministic ID/hash or lifecycle authority.
`RequirementExecutionPlan` and `RequirementExecutionGroup` are revisionable
scheduling recommendations, separate from catalog hashes and lifecycle state.
A technical failure does not create Planner dependency blocks; independent groups
remain eligible and runtime resolution state controls waiting and resume.

The default capacity is four entity-resolution workers, one Analytical Owner,
up to three owner specialists, and eight active workers total; the Planner is
not counted. Hosts may configure lower or higher limits but must never
oversubscribe actual host capacity. Requested Run A and Run B executions remain
sequential.

Every requirement binds directly to the same `RunContext` and shared Data Room.
Each item creates an item-local `RequirementAnalysisPlan` and follows the
ordinary loop: analysis, review, iterative material repairs, accept or
`technical_failure`, then integration. Within a group one Analytical Owner
remains per requirement; bounded shared investigation may be reused and
independent groups may run when host capacity permits. No previous-item
context or internal paths/hashes are exposed to analytical roles.

Entity-resolution jobs are parallel external jobs, not extra specialists and do
not answer requirements. When an Analytical Owner proposes a new arbitrary
real-world identity domain during scouting, reserve that exact owner-bound
proposal as `resolving` and launch an Entity Resolution Owner; the Planner does
not pre-reserve it. Do not hardcode Supplier/Factory/Order
scope; strongly coupled classes may share a domain. The owner scans the full
shared Data Room, owns methodology, may inspect manually or write
Python/SQL/scripts/use helpers, infer and bulk-apply justified patterns, test
samples/coverage/exceptions/population differences, and revise the method.
Row-by-row review, an authoritative crosswalk, and a fixed matching script are
not required; pattern rules remain run knowledge and future helper-library
audit is deferred.

The review decision is binary per proposed mapping (accepted or not accepted).
Each accepted `CanonicalMapping` may contain one or many source identities or
representations, including bulk pattern-derived populations. Unresolved or
ambiguous records remain source-local/outside canonical mappings with coverage
and exceptions preserved, without downgrading proven mappings. Ready snapshots
publish the canonical class, source-account representation classes, reviewed
`IdentityDecision`/`CanonicalMapping`, identity `represents` relationships, and
a versioned mapping asset/coverage where available. Ready is exposed only after
an atomic reviewed commit; owners never see partial snapshots.

For prepared data, the Analytical Owner asks the bound facade to materialize a
candidate; the accepted registry stays empty until Result Integration commits the
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
targeted repair and receives a targeted recheck.
Review findings identify an answer section and business category as semantic
repair provenance. The current or a replacement owner may update any
item-local work and coherent answer section; cross-item writes remain
fail-closed and internal paths/hashes stay program-owned.
The current business verdicts are `accept`, `accept_with_limits`, `repair_once`,
and `confirm_data_insufficiency`. Material repairs may repeat and each receives
a targeted recheck. Only an owner-originated
`DataInsufficiencyConclusion` confirmed by the reviewer can publish
`blocked_by_evidence`; other defects repair or technical-fail. Result
Integration may reuse committed semantics, relationships, conclusions, and
prepared assets selected through the analytical facade. Persisted analysis
contexts load across implementation revisions without a transition/rebind
handoff.

Use `accept` when the core requested decision is answered and only normal
disclosed limits remain. Use `accept_with_limits` only when a material requested
component is missing or unreliable; source-local, currency, or no-causality
caveats alone do not force with-limits. Semantic fidelity is independent, and
resolution review decisions are binary per mapping while each accepted mapping
may contain one or many source identities and coverage/exceptions are job-level
evidence.

Result Integration publishes material reusable business objects/table mappings,
grain, key fields/normalization, relationship/cardinality/coverage/date
authority/limits, and truly reusable prepared descriptors. It does not publish
every merge, result row, metric observation, Japan/Spain filter, or
question-specific aggregation. `no_change` is valid only when no reusable
semantic understanding or asset was established, with a concrete reason.
Q2/Q9 can search, select, and load Q1's exact order-fulfillment semantics, then
compute their own requirement-specific measures.

Analytical Owners establish actual joins/relationships with
`source_id`/`target_id`, `join_keys`, grain, cardinality, `matched_pairs` (the
number of unique tested edge pairs), `source_population`/`target_population`,
`matched_source_count`/`matched_target_count` (distinct matched endpoints),
and `source_coverage`/`target_coverage` (each matched endpoint count divided
by its population; zero when that population is zero), plus `as_of`/date
authority, limitations, and evidence. Integration publishes only reviewed tested
relationships and canonical identity mappings; it never completes a theoretical
graph or infers joins from prose.

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

Build and validate `dist/auto-foundry-agentic-e2e-v0.7.1.zip` locally. The
replacement must be staged and validated before it enters a discovery root;
never unzip directly over the active directory. The repository provides a
stdlib-only atomic installer with a dry-run mode:

```bash
python3 scripts/package_release.py
python3 scripts/validate_release.py
python3 scripts/install_skill_release.py \
  --zip dist/auto-foundry-agentic-e2e-v0.7.1.zip \
  --skills-root "${CODEX_HOME:-$HOME/.codex}/skills" \
  --dry-run
```

For the authorized replacement, omit `--dry-run`. The installer verifies CRCs,
member count and paths, frontmatter/runtime markers, the deterministic package
hash, and every staged file before guarded directory renames. A stable
cross-process lock and fsynced swap journal live in a sibling transaction
directory outside `skills/`; if the process dies between renames, the next
invocation recovers the recorded transaction before doing anything else.
The previous active tree and any old `*.backup`/`*.previous-backup` trees are
moved to a timestamped archive root outside `skills/`; existing active or
archive paths are never overwritten. The CLI accepts no hash/count/version
override: the v0.7.1 production manifest is authoritative. The installer never
changes the real user runtime as part of this repository task; use a temporary
`--skills-root` in tests.

After a successful replacement, verify the installed `SKILL.md` markers are
`0.7.1` with core `0.8.0`, then start a **fresh Codex task**. Skill discovery is
refreshed at task start; do not assume the current task sees a changed skill.
Keep exactly one discoverable `auto-foundry-agentic-e2e` entrypoint: retained
archives belong outside the discovery root.

## Core wheel replacement (same package name)

`scripts/package_release.py` would create
`dist/auto_foundry_core-0.8.0-*.whl`; run `scripts/validate_release.py` and
confirm validation passes before installing it. This repository task does not
claim that a wheel already exists or has been validated; current evidence is
offline tests and static checks only. After that conditional validation,
install into an explicit target or environment selected by the operator; do
not install into the repository or a user runtime as part of this deliverable:

```bash
TARGET="$(mktemp -d)"
python3 -m pip install --no-index --no-deps --target "$TARGET" dist/auto_foundry_core-0.8.0-*.whl
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
label **v0.7.1 / core 0.8.0 — offline program validation complete for later
Benchmark A**. Benchmark A is prepared but not run by this repository task. This remains an experimental
release candidate, not a production-hardened sandbox.

> A Coding Agent with unrestricted host shell/filesystem access cannot be fully sandboxed by this Python package. True isolation requires a separate workspace/container or host allowlist.

## Scope and publication boundary

No skill installation, core installation, external network access, push, pull
request, or remote publication is performed by this repository task. Benchmark
A is prepared only; V3/xddMachine/autofoundary artifacts are untouched.
