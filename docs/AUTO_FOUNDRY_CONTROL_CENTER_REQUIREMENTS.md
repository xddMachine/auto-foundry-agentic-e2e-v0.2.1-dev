# Auto Foundry Control Center — Product and Interface Requirements

Status: implementation baseline
Last updated: 2026-08-18
Owner: project-level product contract

This document is the durable source of truth for the local Auto Foundry web interface discussed in the product-design conversation. Implementation decisions, reviews, and acceptance checks must refer back to it.

## 1. Product intent

Build a local-first web control center on top of the existing Auto Foundry runtime. The interface must make the existing system easier to start, continue, and observe. It must not replace or reinterpret the Planner, analytical execution, entity-resolution, ontology, review, integration, or dashboard-generation logic.

The product has two connected surfaces:

1. **Launch and Control** — configure a new run or extend an existing one.
2. **Live Mission View** — understand the current multi-agent mission and inspect technical activity without exposing private chain-of-thought.

The generated analytical dashboard remains a separate output. The Control Center links to it but does not duplicate it.

## 2. Non-negotiable boundaries

### 2.1 Existing runtime remains authoritative

- The current core runtime, lifecycle, Planner, analytical ownership, entity resolution, ontology construction, review, integration, and dashboard assembly remain the source of truth.
- The web layer calls or observes existing public runtime boundaries; it must not create a second orchestration engine.
- UI labels are projections of recorded state and events, not a new state machine.
- No synthetic activity may be presented as if it occurred.
- Historical relationships that cannot be recovered from durable telemetry must be labelled as unavailable, inferred only when explicitly marked as an inference, or omitted.

### 2.2 Current active run is a protected read-only target

At the time this interface is being developed, an existing run is active. Development and verification must not interrupt or mutate it.

Forbidden against that run:

- start, pause, resume, reopen, extend, cancel, retry, or dispatch actions;
- changes to requirements, run state, entity-resolution state, leases, receipts, telemetry, artifacts, dashboards, or lock files;
- test writes, replay writes, cleanup, migration, or repair;
- process signals or commands intended to influence the active runtime.

Allowed against that run:

- bounded read-only discovery of existing state and telemetry;
- read-only rendering in the Control Center;
- honest display of missing or coarse-grained telemetry.

All write-path development and tests must use an isolated temporary workspace, synthetic fixture, or canned replay.

### 2.3 Reasoning privacy

- Never display hidden chain-of-thought, internal deliberation, or private model reasoning.
- Display factual work summaries: role, assigned objective, lifecycle state, files/resources accessed, tool or script class, produced artifacts, timings, counts, review decisions, and errors.
- Prompts, responses, data values, and file contents must remain collapsed or redacted by default. Their future display requires an explicit security and permission design.

## 3. Target users and primary jobs

The initial user is a local operator who needs to:

- create a new Auto Foundry project/run;
- select and continue an existing project/run;
- enter one or more natural-language requirements;
- attach local datasets by drag-and-drop or file/folder selection;
- optionally supply a dataset location by path; remote URL ingestion is a later capability unless already supported safely by the core;
- set the maximum active-agent capacity within runtime limits;
- start the mission and see whether it was accepted;
- add requirements to an existing mutable run;
- pause, resume, or reopen only when the underlying lifecycle permits it;
- watch active and completed work in real time;
- understand dependencies, waiting, failures, review, and integration;
- open the generated dashboard and evidence artifacts.

## 4. Information architecture

### 4.1 Persistent application shell

- Compact left rail for Runs, Launch, Mission, Evidence, and Settings.
- Top context bar with selected project/run, lifecycle state, connection freshness, active-agent count, and global alerts.
- Main content area with responsive desktop-first layout.
- Inspector drawer on the right for the selected agent, event, file, artifact, or dependency.

### 4.2 Launch and Control view

Required controls:

- mode: **New project/run** or **Continue existing**;
- project/run selector with search, lifecycle state, last update, requirement count, and dashboard availability;
- project/run name for a new target;
- multiline requirements editor;
- add another requirement without losing prior entries;
- drag-and-drop dataset zone;
- local file picker;
- local folder picker where the browser/runtime supports it;
- explicit local path entry for already-available data;
- future remote URL field, visibly marked unsupported until a safe ingestion contract exists;
- file manifest showing name, type, size, validation state, and remove action;
- maximum active agents control constrained by authoritative runtime capacity;
- preflight summary showing what will happen and which target will be changed;
- primary action: Start, Add requirements, Resume, or Reopen, depending on mode and lifecycle;
- validation errors tied to the exact control that caused them;
- action receipt with immutable run/requirement identifiers after success.

The interface must require an explicit final confirmation before any operation that mutates an existing run.

### 4.3 Runs view

- List or table of discoverable projects/runs.
- Search and filters by lifecycle, type, age, and health.
- Clear distinction among running, paused, terminal, failed, incomplete, and read-only/protected runs.
- Last durable activity timestamp, requirement progress, active/waiting agent counts, incident count, and dashboard link.
- Selecting a run opens its Mission View without mutation.

### 4.4 Mission View

The default operational view combines:

- **Mission Graph** in the main canvas;
- **Live Activity rail** with human-readable recent events;
- **Inspector** for detail;
- compact metrics for active, waiting, completed, failed, and capacity usage.

The graph must support pan, zoom, fit-to-view, selection, focus on active nodes, and a calm reduced-motion mode.

### 4.5 Technical Trace view

A synchronized trace view shows:

- nested operations/spans;
- horizontal duration bars on a shared time axis;
- parent-child relationships;
- status, start time, duration, and source role;
- event filters by role, requirement, domain, status, file/resource type, and severity;
- file reads, structured data observations, tool/script activity, artifact writes, review/integration events, and errors;
- an inspector with recorded attributes and evidence paths.

Selecting an agent in Mission Graph filters or highlights its trace. Selecting a trace event highlights its graph node.

## 5. Authoritative agent and dependency model

The visualization is a directed acyclic mission graph, not a linear list and not necessarily a pure tree.

### 5.1 Planner

- Planner is the persistent root/control-plane node.
- Planner is visually distinct from worker capacity and is not counted as an active worker slot.
- Planner owns global mission progression, requirement routing, entity-resolution dispatch, review/integration progression, and terminal publication decisions according to the existing runtime.

### 5.2 Analytical Owner

- An Analytical Owner (AO) owns the investigation of a requirement.
- Initial capacity is one active AO slot.
- Each AO appears under its requirement/Planner route and may own up to three specialist agents.
- Specialists A/B/C are children of and controlled by that AO.
- An AO may discover that identity resolution is required, but it does not own the Entity Resolution Owner.
- When an AO waits on identity resolution, the UI must show the dependency explicitly. If the runtime releases the AO slot, the waiting AO is not counted as active and Planner may advance another requirement.

### 5.3 Entity Resolution Owner

- Identity-domain agents such as Customer or Product are global run-level resources owned and dispatched by Planner.
- They are shown directly under or beside Planner, not as children of the AO that first exposed the need.
- One resolved identity domain may unblock multiple requirements/AOs; the graph therefore supports multiple incoming/outgoing dependency edges.
- Initial entity-resolution capacity is four active workers.

### 5.4 Specialist agents

- Up to three specialists may be active under the current AO.
- The UI shows their bounded assignment and parent AO.
- A specialist node must not imply Planner ownership when the AO owns it.

### 5.5 Initial capacity contract

- total active workers: 8;
- Entity Resolution Owners: up to 4;
- Analytical Owners: up to 1;
- AO specialists: up to 3;
- Planner: excluded from worker total.

The backend must read the effective capacity from authoritative run state where available. Controls must not promise a capacity that the runtime cannot honor.

## 6. Mission Graph behavior

### 6.1 Nodes

Every node includes, when recorded:

- stable role icon and role name;
- concise objective or assigned item;
- lifecycle state;
- elapsed time or completion duration;
- requirement/domain identifier;
- small activity indicator based on recent real events;
- expandable details.

Node lifecycle states include queued, active, waiting, reviewing, integrating, completed, failed, and unknown/stale.

Completed agents should visually settle and compact, not abruptly vanish. They may collapse into a completed group after a short transition. Failed nodes remain visible until acknowledged or filtered.

### 6.2 Edges

Use distinct semantic edges:

- solid ownership/dispatch edge;
- dashed dependency/wait edge;
- accent return/result edge where recorded;
- shared-domain edge from one Entity Resolution Owner to every dependent requirement.

Animated pulses may travel only along an edge associated with a newly recorded event. No ambient fake traffic.

### 6.3 Large missions

- Collapse completed subtrees/groups.
- Cluster specialists beneath their AO.
- Keep Planner and active/waiting dependencies visible.
- Provide filters and focus mode.
- Preserve spatial stability so nodes do not constantly jump when events arrive.

## 7. Activity and file telemetry

The interface should make technical work feel alive while remaining truthful.

Supported activity classes:

- agent/attempt started, heartbeat observed, waiting, completed, failed;
- requirement or domain claimed/released;
- file opened/read;
- table or structured dataset observed, including row count when recorded;
- artifact created/updated;
- catalog or semantic resource reused;
- script/tool invoked, with safe tool-class label;
- review requested/accepted/rejected;
- integration/publish step;
- warning, incident, or error.

File activity cards show:

- basename prominently and full path in the inspector;
- format/type icon;
- action such as read, inspect, validate, or create;
- recorded row count/size when present;
- timestamp and responsible agent/item when attributable;
- link to local evidence only when safe and resolvable.

Repeated high-volume reads should aggregate into a truthful summary such as “37 dataset members read in 12 s,” with drill-down. The feed must not become an unreadable terminal dump.

## 8. Visual direction

The visual identity is inspired by the operational clarity of Palantir AIP/Foundry, not a pixel copy.

### 8.1 Principles

- dense but calm operational surface;
- hierarchy before decoration;
- crisp one-pixel borders, restrained radii, compact typography;
- status communicated through color plus icon/text, never color alone;
- motion explains state transition or event flow;
- details are progressively disclosed to prevent information overload;
- every decision/action has clear attribution.

### 8.2 Palette

- graphite/navy application background;
- slightly lighter panels and cards;
- cyan: active/executing;
- amber: waiting/blocked on dependency;
- violet: review/semantic work;
- green: completed/committed;
- red: error/failed;
- neutral steel: queued/inactive/unknown.

The implementation should use its own design tokens and accessible contrast rather than copying Palantir brand assets.

### 8.3 Motion

- node enter/settle transitions;
- edge pulse only on real activity;
- subtle active-node halo tied to freshness;
- trace bars extend with actual elapsed time;
- completion becomes a stable resolved state;
- `prefers-reduced-motion` disables non-essential animation.

## 9. Palantir research translated into product decisions

The following official patterns informed this specification:

- [AIP observability overview](https://www.palantir.com/docs/foundry/aip-observability/overview): metrics, run history, distributed tracing, logging, and drill-down belong in one connected observability system.
- [Trace view](https://www.palantir.com/docs/foundry/aip-observability/trace-view): nested spans, shared time axis, duration bars, resource-type color, and per-span details are the basis of Technical Trace.
- [DevCon Agent Stack and AIP Evolve](https://www.palantir.com/devcon/): durable orchestration, actionable telemetry, and turning raw agent telemetry into a readable narrative are core product principles.
- DevCon's **Design Patterns for Human-Agent Collaboration**: show reasoning outcomes without overload and make decision attribution explicit.
- [Kirkland Fund Formation Engine demo](https://www.kirkland.com/publications/video/2026/kirkland-and-palantir-partnership): domain-specific workflows should be presented as an integrated operational product, not as a generic chat wrapper.

Concrete adaptation for Auto Foundry:

- Mission Graph provides the legible business/agent narrative.
- Technical Trace provides the exact low-level timeline.
- Inspector provides recorded evidence and attribution.
- The interface defaults to summary and lets the operator drill down.
- Ontology/domain nodes and shared dependencies are first-class, because they are reusable across requirements.

## 10. Data and event projection

### 10.1 Read model

The Control Center maintains a read-only projection built from existing durable artifacts such as:

- run lifecycle state;
- requirement/item state;
- entity-resolution state and leases;
- telemetry JSONL;
- invocation receipts when present;
- review/integration/product state;
- generated dashboard and evidence paths.

The projection must tolerate partial, stale, malformed, or missing optional files. It reports freshness and limitations instead of converting absence into success, zero activity, or a fabricated relationship.

### 10.2 Normalized UI events

Each projected event should expose only recorded or safely derived fields:

- event id or deterministic cursor;
- timestamp;
- source file/stream;
- event class and subtype;
- run, requirement/item, attempt, role, domain, and invocation identifiers when present;
- parent/owner/dependency identifiers when present;
- path/resource/tool/artifact metadata;
- safe human-readable summary;
- severity/status;
- allow-listed diagnostic attributes for inspection; unrestricted raw records, prompts, messages, model responses, and data values are not exposed in Layer 1.

### 10.3 Live transport

Initial local transport may use bounded polling with an append-only cursor. The contract should allow later replacement with Server-Sent Events or WebSocket streaming without changing UI semantics.

- Updates must be incremental.
- File reads must use stable cursors/offsets.
- Rotation/truncation and malformed final JSONL lines must be handled safely.
- The frontend displays last successful refresh and stale/disconnected state.
- Attaching to a run is read-only.

## 11. Backend/API boundary

Keep observation and mutation explicitly separated.

Suggested read endpoints:

- list discoverable runs;
- get run summary;
- get mission graph projection;
- get events after cursor;
- get trace projection;
- get safe artifact/evidence manifest.

Suggested command endpoints, enabled only after isolated verification:

- validate launch draft;
- create new run;
- append requirements;
- pause/resume/reopen through existing lifecycle services;
- resolve explicit operator confirmation.

Every command returns an authoritative receipt. The server validates paths, lifecycle, capacity, and operation scope; browser-provided paths are never trusted directly.

## 12. Security and local-path rules

- Bind to loopback by default.
- Do not expose arbitrary filesystem browsing.
- Restrict selectable/readable paths to explicitly configured workspace/input roots.
- Reject traversal and paths outside allowed roots.
- Uploaded files go only to a newly created, isolated staging area until validated.
- Never serve secrets, environment variables, hidden prompts, or unrestricted raw logs.
- Escape all displayed names, paths, summaries, and event fields.
- Mutating endpoints require anti-CSRF protection or an equivalent local command token.
- The protected active run remains read-only even if general mutation endpoints are later enabled.

## 13. Delivery layers

### Layer 1 — Safe observability MVP

- separate local Control Center module;
- synthetic/canned replay data source;
- run discovery and read-only attach;
- Mission Graph, Live Activity, Inspector, and Technical Trace;
- launch form with validation and a clearly non-mutating preflight/draft path;
- zero mutation of the protected current run.

### Layer 2 — New-run launch

- isolated uploads/staging;
- validated new-run creation through existing core APIs;
- capacity configuration within authoritative limits;
- start receipt and automatic attach to Mission View;
- integration tests only in temporary workspaces.

### Layer 3 — Existing-run commands

- add requirements;
- lifecycle-aware pause/resume/reopen;
- explicit confirmation and receipts;
- protected-run policy and command audit.

### Layer 4 — Rich live instrumentation

- precise invocation/ownership/dependency events at existing wrapper boundaries;
- SSE or WebSocket transport if polling is insufficient;
- richer aggregation, performance analysis, and saved operator views;
- instrumentation remains factual and does not expose chain-of-thought.

## 14. Acceptance criteria

The feature is acceptable when:

- it starts as a separate local web application;
- it can render a deterministic fixture end to end with no core runtime calls;
- it can discover and observe a real run without writing to it;
- Planner, AO, specialists, and Entity Resolution Owners have the correct ownership structure;
- shared identity-domain dependencies render as a DAG;
- capacity totals and per-role limits match authoritative state;
- recent real file/artifact activity appears with correct attribution when recorded;
- repeated activity aggregates without data loss and can be expanded;
- graph and trace selection remain synchronized;
- missing telemetry is labelled honestly;
- the launch form supports all required inputs and validates them;
- no command can target the protected current run during development;
- all automated tests use fixtures or temporary workspaces;
- the existing analytical dashboard can be opened from a completed run;
- the interface remains usable with reduced motion and keyboard navigation.

## 15. Explicitly deferred questions

These are not grounds to block Layer 1:

- remote URL ingestion and its authentication/security model;
- exact cross-platform semantics of folder picking;
- long-term authentication for non-local deployment;
- retention policy for UI-specific event projections;
- whether exact agent spawn/finish instrumentation requires small additions at runtime wrapper boundaries;
- final command authorization policy after the protected run completes.

## 16. Operational prototype decisions (2026-08-18)

The operational prototype is a third, separate application. It does not replace
the dark Control Center or the light dashboard-theme prototype, and it does not
modify Planner, analytics, ontology, entity-resolution policy, result
integration, or the final dashboard assembler.

### 16.1 Launch boundary

- Launch is deliberately two-step: `Prepare launch` writes only an immutable
  draft under the Control Center state root; `Start run` requires a second
  explicit browser action bound to the exact draft fingerprint.
- Preparing a launch must not create a run root, alter an existing run, make a
  model call, or download a remote URL.
- Execution is disabled unless the loopback server is started with an explicit
  launch-enable flag. Tests use a fake runner and never call Codex or the
  network.
- After the cognitive Planner has interpreted the immutable intake, a new run
  is initialized only through the existing public core contracts:
  `RunContext`, `RunLifecycle`, `ResolutionCapacity`,
  `EntityResolutionWorkspace`, `ItemWorkspace`, and
  `RequirementSupervisorWorkspace`.
- Launch execution first gives the cognitive Planner the exact raw input blocks,
  optional document references, the current production skill binding, and the
  immutable data-room reference. The program validates source coverage and the
  returned portfolio before creating lifecycle state or `RequirementRecord`s.
- The Planner prompt uses the exact production `auto-foundry-agentic-e2e`
  binding in analytics-only Requirement Mode and forbids edits to the runtime
  checkout.
- The operational layer may start a Planner process, but it must never become
  an analytical decision-maker or change analytical routing.

### 16.2 Capacity

- The browser supports a requested ceiling from 1 through 64 active workers;
  Planner remains excluded.
- The chosen total and per-role allocation are persisted in the launch
  fingerprint and materialized as the run's authoritative
  `ResolutionCapacity`.
- The requested value is a ceiling, not a promise that the host can physically
  supply that many concurrent agents. Runtime/host availability may reduce
  actual concurrency, and the UI must state that honestly.
- Existing-run capacity is immutable and always comes from the run's durable
  entity-resolution state.

### 16.3 Requirements and sources

- The form accepts arbitrary natural-language briefs, questions, notes, or
  requirement documents. UI fields are input blocks, never requirement
  boundaries. No regex, numbered-heading parser, or required template decides
  the split.
- Planner decides which independent business decisions become requirements,
  their dependencies, grouping, and order. The program assigns stable
  non-colliding `REQ-NNN` identifiers only after validating the interpretation.
- Every non-whitespace character in text intake must be covered by an exact
  Planner-selected source span or explicitly classified as unassigned context;
  document-only requirements must bind to an immutable data-room document.
- Browser files and folders upload their bytes into isolated staging; folder
  relative paths are preserved and traversal/symlink aliases are rejected.
- Explicit local paths are accepted only inside administrator-configured source
  roots.
- A public unauthenticated HTTP(S) URL may be staged by the external launch
  adapter only after execution confirmation. Redirects and resolved addresses
  must remain public; credentials, localhost, private/link-local/reserved
  addresses, unbounded downloads, and unsupported formats are rejected.
- All accepted new-run sources are packaged into the core's single immutable
  local ZIP data room. Remote authentication remains deferred.
- Adding a new data room to an existing run is not supported because the shared
  room is immutable. Existing-run Launch may add requirements against the
  already-authoritative room but rejects new source inputs.

### 16.4 Dynamic mission graph

- The operational read model combines only allow-listed facts from durable run
  state, item state, entity-resolution state, specialist task/memo records,
  invocation receipts, core telemetry, and safe Codex tool-lifecycle metadata.
- Planner controls Analytical Owners and Entity Resolution agents. Analytical
  Owners control their specialists. A reviewer has a separate review relation
  to the reviewed target; `controller`, `requester`, and `reviews` are never
  collapsed into one edge.
- Nodes appear when observed, transition through active/waiting/review/terminal
  states, and fade from the live graph after completion while remaining in the
  technical trace.
- Unknown Codex event shapes are ignored. Prompts, agent messages, model
  responses, raw commands, raw data values, and chain-of-thought are never
  projected to the browser.
- If exact lineage is absent, durable-artifact projection is labelled as such;
  the UI must not invent traffic or relationships.
