# Auto Foundry — core 0.9.0 / skill 0.8.0

Autonomous, evidence-backed business analysis with a reviewed, local dashboard.
This release keeps the Analytical Owner and independent Business Reviewer, while
simplifying the handoff between accepted analytical results and the program that
integrates and presents them.

**Start here:** [architecture, installation and validation](docs/RELIABLE_DASHBOARD_ARCHITECTURE.md).
Development branch: `feat/reliable-analytics-dashboard`. The original branch and
stored business runs are not modified or automatically restarted by this release.

## Execution model

```text
RunContext + DataRoomWorkbench
  -> bounded evidence and analytical context
  -> Analytical Owner: investigate, clean, reconcile, calculate, interpret
  -> independent Business Reviewer and bounded repairs
  -> immutable accepted result
       -> ProductWorkspace -> partial preview / final candidate -> Product Review
       -> typed integration + fidelity review -> reusable enterprise model
```

The agent owns business meaning and presentation choices. The program owns exact
serialization, source bindings, calculations, recovery, generation/revision
routing and writes. No intermediate human approval is added to the analytical
pipeline; unresolved evidence and semantic conflicts remain explicit outcomes.

Each requirement produces reusable fact bundles, not a separate hand-written
HTML application. Accepted results can appear in an incremental non-final preview
while other requirements continue. Final assembly composes the terminal portfolio
into decision domains. It does not force incompatible grains or currencies into
a universal cube or recompute analysis simply to redesign a chart.

An exhausted technical integration step does not erase a valid accepted answer.
A semantic error in an answer is different: it must return to its owner and cannot
be hidden by a presentation fallback. Product construction never approves its
own output or grants publication authority.

## ProductWorkspace

The current Product Agent interface is
[`product_workspace.py`](skills/auto-foundry-agentic-e2e/scripts/product_workspace.py):
`inventory()`, `detail(widget_id)`, `feedback()` and `build(choices, presentation=...)`.
The agent selects source-bound views, eligible chart recipes, order and readable
copy. The workspace handles preflight, hashes, plan admission/CAS, immutable output
paths, later generations and candidate registration through the existing stores.

The dashboard has responsive KPI/chart layouts, source tables and evidence pages.
Pie and donut are distinct, scatter uses real coordinates, area charts use the
accepted series, and legends and search/domain filters act on the rendered views.
Chart variety is evidence-driven, not a quota. An unsupported stacked-area
conversion is rejected rather than shown as an unrelated composition.

## Install the paired release

Stop old local coordinators/servers first. Use a fresh checkout or clean worktree;
do not overwrite existing dirty work. Run from this branch's repository root:

```sh
python3 -m pip install -e .
python3 scripts/package_release.py
python3 scripts/validate_release.py
python3 scripts/install_skill_release.py \
  --zip dist/auto-foundry-agentic-e2e-v0.8.0.zip \
  --skills-root "$HOME/.codex/skills" --dry-run
```

Inspect the installer dry-run, then repeat its command without `--dry-run` to
install. **Update both core and skill**, not one alone. The installer verifies the
exact package and stages/archives outside skill discovery roots. Restart the local
agent environment after installation; do not leave duplicate discoverable skills.

Test new runs first. Historical runs may require their existing explicit
rebind/regeneration APIs; take a backup and never hand-edit state/hashes to force
an old run onto a new release.

## Inspect the dashboard without model calls

```sh
python3 scripts/build_dashboard_demo.py --output /tmp/af-synthetic-demo
```

Open the `html` path printed by the command. The output directory must not already
exist. This demonstration uses **synthetic recorded agent/reviewer decisions** and
real accepted-bundle, integration, assembly and product-review storage APIs. It
creates four KPI cards and nine chart/table views. It is not a live-agent benchmark,
a customer result, or external publication.

The operational Control Center is separate from the manager dashboard:

```sh
mkdir -p /tmp/af-test-runs /tmp/af-test-sources
PYTHONPATH=src:. python3 -m apps.control_center_operational.server \
  --port 8768 --runs-root /tmp/af-test-runs --source-root /tmp/af-test-sources
```

This invocation is observation-only. Add `--enable-launch` only when deliberately
starting new local agent runs with the normal installed Codex environment.

## Verification

```sh
python3 -m pip install pytest playwright
PYTHONPATH=src:. python3 -m pytest -q
PYTHONPATH=src:. python3 -m unittest discover \
  -s apps/control_center_operational/tests -p 'test_*.py'
node --test apps/control_center_operational/tests/test_*.js
python3 -m playwright install chromium
python3 scripts/validate_dashboard_browser.py --output /tmp/af-browser-check
```

GitHub Actions checks the exact branch/PR head, builds and verifies the paired
release, runs the Python and JavaScript suites and opens the **HTTP-served**
dashboard in Chromium. Browser checks cover generated pages, chart geometry,
legend isolation, search/domain filtering, mobile overflow, JavaScript errors and
unchanged artifact bytes. Artifacts contain source identity, test reports, release
packages and the synthetic dashboard/screenshots, not stored business runs.

A separate `--inline --chromium /path/to/chromium` browser mode supports restricted
sandboxes and labels its result `inline_dom`; it does not establish HTTP navigation
coverage. Optional canaries depending on unshipped historical datasets are
explicitly skipped, not reported as passed.

## Boundaries and further reading

Snapshot recovery validates canonical bindings; it does not excuse corruption.
Conflicting ontology definitions fail atomically instead of silently choosing the
first scalar. Semantic changes use the existing reviewed KnowledgeDelta/successor
path. Stale portfolio revisions cannot silently remove concurrently added work.
Supervisor is instructed to perform public runtime recovery, not rewrite executing
source. This role contract is not an OS sandbox.

No live model calls or customer runs were launched for development verification.
Mechanical integration tests do not prove semantic accuracy on arbitrary data,
cost, or reliability under every provider failure. The historical Test11 Business
Review detection gap is not claimed solved by dashboard work. The complete
external archived run was not replayed.

See [current architecture and limits](docs/RELIABLE_DASHBOARD_ARCHITECTURE.md),
[Product Agent contract](skills/auto-foundry-agentic-e2e/references/PRODUCT_AGENT_ASSEMBLER_CONTRACT.md),
[skill entry point](skills/auto-foundry-agentic-e2e/SKILL.md), and
[core architecture](docs/CORE_ARCHITECTURE.md). Historical implementation and
benchmark documents describe their own checkpoints, not the status of this release.
