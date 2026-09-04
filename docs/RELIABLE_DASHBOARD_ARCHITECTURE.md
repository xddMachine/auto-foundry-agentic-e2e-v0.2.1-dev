# Reliable analytics → dashboard

Baseline: d3f07e15762683bdf4752cf24e719a4891c5b7d7.
Development branch: feat/reliable-analytics-dashboard.
Paired implementation: core 0.9.0; skill 0.8.0.

## Decisions and implemented boundaries

An Analytical Owner retains end-to-end responsibility for interpreting each
requirement, selecting evidence, cleaning with context, checking identities/joins,
defining population/grain/period/units/denominator, calculating and writing its
answer. Optional specialists and independent Business Reviewer remain. Cleaning,
statistical methodology and semantic evidence are not replaced by chart selection.
The deterministic workbench, controlled calculations, evidence snapshots and
review contracts are retained, not rewritten as an untested universal workflow.

The accepted result is the handoff. Product does not reconstruct values from
prose and does not reopen raw data. Accepted typed visuals now retain KPI, pie,
donut, scatter, area, histogram, box-plot and waterfall geometry as well as the
existing chart types. ProductWorkspace exposes bounded inventory/detail and exact
predecessor-review feedback. An agent supplies only meaningful selection, order,
allowed recipes/layouts/renderers and concise copy. The host computes hash and
lineage bindings, initial/CAS plan admission, cumulative routes, immutable output
namespaces and candidate registration. Existing core stores remain authoritative.
This facade replaces the lengthy production prompt API sequence; it is not another
queue, state machine or database.

### Hybrid delivery, not a universal cube

A validated accepted subset can produce a non-final preview while other
requirements continue. The final candidate composes all terminal requirement
outcomes into coherent business domains. Requirements author fact bundles, not
independent HTML apps. There is no aggregation of incompatible grains into one
cube, and no reanalysis merely to redesign a chart. New source/requirement inputs
invalidate stale choices. A later generation derives routes from the actual
portfolio and immutable parent domain IDs and retains parent bytes.

Technical integration failure and semantic invalidity are different. An explicitly
exhausted technical projection does not erase a valid accepted answer. A reviewer
finding that undermines the analytical answer must stay unresolved/return to its
owner; it is not permission to display known-bad accepted facts. The historical
Test11 reviewer-detection gap is not claimed solved by renderer changes.

### Semantics and recovery

Same-ID ontology additions are exact repeats, compatible additions, or explicit
OntologyConflictError. Conflicting grain, unit, formula, scope and schema fields
cannot silently produce a first-wins hybrid. The merge is atomic. Cosmetic labels, evidence references and additive column-name lists have explicit
narrow policies; primary-key ordering is not unioned. Integration preflights
the prior shared LEM before fidelity review. Existing reviewed KnowledgeDelta /
successor mechanisms, not new IDs invented to dodge conflicts, handle meaning
changes. This is not a claim to solve every possible entity/relationship conflict.

IntegrationSession.persisted_identity is shared by coordinator recovery and
commit: the canonical validated snapshot can restore missing session/records
projections. Corrupt hashes and foreign accepted bindings fail closed.

Supervisor is now restricted by its role contract to public runtime recovery APIs;
it is no longer instructed to rewrite repository source or installed skills. It
continues in a fresh process and its host verifies artifact-bound progress or a
validated terminal transition. A changed diagnostic message or event hash alone
is not progress. This is a runtime ownership contract, not a claim of OS sandbox
isolation. Software repairs belong in development and a separate tested release.
Integrity and semantic blocks are not converted to synthetic success.

Portfolio `save` rejects stale revisions and binds changed requirement sets to
the exact parent plan hash under the generation lock. A concurrent old replan
cannot silently remove a newly appended requirement. Existing explicit portfolio
revision APIs still own intentional edits.

### Product and design

The uploaded index.html is a visual reference: light grey canvas, white cards,
legible business headings, teal/purple accents, responsive multi-column layout,
KPI row and diverse statistical charts. Its customer data and embedded application
runtime are not shipped. The renderer uses local existing assets, real scatter
coordinates and area geometry, distinct pie/donut, exact source tables and working
evidence/ontology navigation. Unsupported stacked-area conversion is rejected,
not drawn as an unrelated composition. Every chart needs legitimate data; there
is no quota or invented chart to create variety. Units, periods and caveats remain
part of the reviewed surface. The operational Control Center is separate from
the manager dashboard.

Independent Product Review remains required. The builder cannot accept its own
work or authorize publication. Preview, accepted candidate, complete run and
external publication policy are distinct. A repaired accepted candidate needs
an authorized immutable revision. The root parent lineage uses the actual root
run_state.json, not a nonexistent G-0001 generation_manifest.json.

## Installation for testing

Stop existing local servers/coordinators before changing the executing release.
Use a fresh checkout or clean worktree; do not overwrite dirty work. From this
branch's repository root, using your normal Python with project dependencies:

```sh
python3 -m pip install -e .
python3 scripts/package_release.py
python3 scripts/validate_release.py
python3 scripts/install_skill_release.py \
  --zip dist/auto-foundry-agentic-e2e-v0.8.0.zip \
  --skills-root "$HOME/.codex/skills" --dry-run
# After inspecting the dry-run, install the paired skill by omitting --dry-run.
```

The installer checks an exact deterministic package hash. Do not install only
core or keep multiple discoverable copies of the skill. Skill/code changes need
repackaging and an updated release hash, not a bypass of the validation. Stored
real runs are not migrated or replayed automatically. Test new runs first; use
existing explicit rebind/regeneration APIs for an old run after making a backup.
No old run state or accepted bytes should be manually edited to match this release.

To inspect without starting models:

```sh
mkdir -p /tmp/af-test-runs /tmp/af-test-sources
PYTHONPATH=src:. python3 -m apps.control_center_operational.server \
  --port 8768 --runs-root /tmp/af-test-runs --source-root /tmp/af-test-sources
```

This is observation-only. --enable-launch explicitly enables starting new local
agent runs; it was not used for development verification.

## Offline verification and visual demonstration

```sh
PYTHONPATH=src:. python3 -m pytest -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python3 -m unittest discover \
  -s apps/control_center_operational/tests -v
python3 scripts/build_dashboard_demo.py --output /tmp/af-synthetic-demo
```

The demo refuses an existing directory and uses explicitly synthetic recorded
agent/reviewer decisions. It invokes real accepted/integration/build/review
APIs, not hand-written final manifests. It does not start models, publish a
customer result or change a saved run. It demonstrates four KPI cards and nine
chart/table views. Opening its local index is visualization, not external
publication. Browser checks are separate from Python tests.

The original clean Ubuntu run produced 1346 passed, 7 failed, 7 skipped. Failures
included home-installed skill assumptions, filesystem ordering, absolute path
word checks and missing historical files outside the repository. Tests now
use isolated fixtures; actual absent historical canaries remain explicitly
skipped, not represented as passes. Release-specific results are recorded in
GitHub Actions artifacts. Package/import and browser checks supplement pytest.

## Remaining validation / intentional limits

No live Codex/API calls or customer runs were launched. Offline recorded decisions
prove mechanical integration, not semantic model quality, cost or run completion
under every provider failure. Validate with the user's normal installed Codex on
new unfamiliar data, including conflicts and interruption. The historical full
external run was inspected via a hash-verified selected diagnostic export; it was
not replayed. No claim of exhaustive correctness or a complete security audit.

There is no new distributed scheduler or SQLite migration in this release: adding
another persistence authority would increase risk before stable full-path tests.
Existing core responsibilities remain modularization opportunities, not reasons
to discard their safety contracts. Large-source performance and live repeated
agent repair require measured canaries, not more success-shaped test fixtures.

## Browser validation

```sh
python3 -m pip install pytest playwright
python3 -m playwright install chromium
python3 scripts/validate_dashboard_browser.py --output /tmp/af-browser-check
node --test apps/control_center_operational/tests/*.js
```

The browser test uses a real loopback-served site and follows every generated HTML
page. It checks KPI/chart counts, scatter geometry, area geometry, scoped legend
visibility, search/domain filtering, responsive overflow, JavaScript errors and
unchanged source bytes. `--inline --chromium /path/to/chromium` is a separately
reported DOM-only mode for restricted environments; it does not claim navigation
coverage. GitHub Actions runs the served-site version.
