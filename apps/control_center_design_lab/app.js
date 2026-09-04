(function () {
  "use strict";

  var requirements = [
    { id: "REQ-001", name: "Supplier reliability", state: "accepted", stage: 4, progress: 100 },
    { id: "REQ-002", name: "Customer margin", state: "accepted", stage: 4, progress: 100 },
    { id: "REQ-003", name: "Inventory risk", state: "accepted", stage: 4, progress: 100 },
    { id: "REQ-004", name: "Receivables", state: "accepted", stage: 4, progress: 100 },
    { id: "REQ-005", name: "Cross-domain operations", state: "accepted", stage: 4, progress: 100 },
    { id: "REQ-006", name: "Inbound concentration", state: "running", stage: 3, progress: 72 },
    { id: "REQ-007", name: "Service exceptions", state: "queued", stage: 0, progress: 0 },
    { id: "REQ-008", name: "Location resilience", state: "queued", stage: 0, progress: 0 },
    { id: "REQ-009", name: "Slow-moving stock", state: "queued", stage: 0, progress: 0 },
    { id: "REQ-010", name: "Payment behavior", state: "queued", stage: 0, progress: 0 },
    { id: "REQ-011", name: "Combined risk chains", state: "running", stage: 2, progress: 48 },
    { id: "REQ-012", name: "Route variability", state: "queued", stage: 0, progress: 0 },
    { id: "REQ-013", name: "Intervention portfolio", state: "queued", stage: 0, progress: 0 }
  ];

  var initialRequirements = JSON.stringify(requirements);
  var events = [
    { req: "REQ-006", role: "Integration Agent", code: "IN", action: "Integrate evidence bundle", stage: 3 },
    { req: "REQ-011", role: "Business Reviewer", code: "BR", action: "Review combined risk chains", stage: 2 },
    { req: "REQ-006", role: "Business Reviewer", code: "BR", action: "Review intervention ranking", stage: 2 },
    { req: "REQ-007", role: "Analytical Owner", code: "AO", action: "Analyze service exceptions", stage: 1 },
    { req: "REQ-011", role: "Integration Agent", code: "IN", action: "Integrate accepted findings", stage: 3 },
    { req: "REQ-008", role: "Identity Owner", code: "ID", action: "Resolve location identities", stage: 1 },
    { req: "REQ-006", role: "Integration Agent", code: "IN", action: "Commit durable result", stage: 4 },
    { req: "REQ-009", role: "Analytical Owner", code: "AO", action: "Analyze stock velocity", stage: 1 }
  ];

  /* The second-wave concepts share this small, deterministic activity fixture.
     It is deliberately richer than the requirement-only model, but contains
     no transport or run references. */
  var agentCatalog = [
    { id: "AG-AO-01", code: "AO", role: "Analytical Owner", hue: "violet" },
    { id: "AG-BR-01", code: "BR", role: "Business Reviewer", hue: "amber" },
    { id: "AG-IN-01", code: "IN", role: "Integration Agent", hue: "cyan" },
    { id: "AG-ID-01", code: "ID", role: "Identity Owner", hue: "blue" },
    { id: "AG-QA-01", code: "QA", role: "Evidence Steward", hue: "green" }
  ];

  var actionCatalog = [
    { id: "ACT-001", req: "REQ-006", agent: "AG-IN-01", label: "Integrate evidence bundle", stage: 3, duration: 19, start: 57, evidence: "EVD-101", critical: true },
    { id: "ACT-002", req: "REQ-011", agent: "AG-BR-01", label: "Review combined risk chains", stage: 2, duration: 14, start: 38, evidence: "EVD-102", critical: true },
    { id: "ACT-003", req: "REQ-006", agent: "AG-BR-01", label: "Review intervention ranking", stage: 2, duration: 12, start: 64, evidence: "EVD-103", critical: true },
    { id: "ACT-004", req: "REQ-007", agent: "AG-AO-01", label: "Analyze service exceptions", stage: 1, duration: 22, start: 19, evidence: "EVD-104", critical: false },
    { id: "ACT-005", req: "REQ-011", agent: "AG-IN-01", label: "Integrate accepted findings", stage: 3, duration: 15, start: 51, evidence: "EVD-105", critical: true },
    { id: "ACT-006", req: "REQ-008", agent: "AG-ID-01", label: "Resolve location identities", stage: 1, duration: 18, start: 27, evidence: "EVD-106", critical: false },
    { id: "ACT-007", req: "REQ-006", agent: "AG-IN-01", label: "Commit durable result", stage: 4, duration: 11, start: 77, evidence: "EVD-107", critical: true },
    { id: "ACT-008", req: "REQ-009", agent: "AG-AO-01", label: "Analyze stock velocity", stage: 1, duration: 17, start: 9, evidence: "EVD-108", critical: false },
    { id: "ACT-009", req: "REQ-010", agent: "AG-AO-01", label: "Model payment behavior", stage: 1, duration: 16, start: 34, evidence: "EVD-109", critical: false },
    { id: "ACT-010", req: "REQ-012", agent: "AG-ID-01", label: "Resolve route variability", stage: 1, duration: 20, start: 13, evidence: "EVD-110", critical: false },
    { id: "ACT-011", req: "REQ-013", agent: "AG-QA-01", label: "Validate intervention portfolio", stage: 2, duration: 13, start: 46, evidence: "EVD-111", critical: false },
    { id: "ACT-012", req: "REQ-005", agent: "AG-BR-01", label: "Accept cross-domain operations", stage: 4, duration: 10, start: 83, evidence: "EVD-112", critical: true },
    { id: "ACT-013", req: "REQ-001", agent: "AG-QA-01", label: "Check supplier evidence", stage: 4, duration: 9, start: 71, evidence: "EVD-113", critical: false },
    { id: "ACT-014", req: "REQ-002", agent: "AG-AO-01", label: "Compare customer margin", stage: 4, duration: 12, start: 61, evidence: "EVD-114", critical: false },
    { id: "ACT-015", req: "REQ-003", agent: "AG-QA-01", label: "Validate inventory risk", stage: 4, duration: 11, start: 68, evidence: "EVD-115", critical: false },
    { id: "ACT-016", req: "REQ-004", agent: "AG-IN-01", label: "Commit receivables result", stage: 4, duration: 8, start: 87, evidence: "EVD-116", critical: false }
  ];

  var evidenceCatalog = actionCatalog.map(function (action, index) {
    return { id: action.evidence, action: action.id, req: action.req, label: "Evidence " + String(index + 1).padStart(2, "0") };
  });

  var concepts = [
    {
      title: "Operational Command Deck",
      short: "Command Deck",
      subtitle: "Common operational picture",
      kicker: "CONCEPT 01 · SPATIAL OPERATIONS",
      description: "A mission-centric command surface: requirements orbit the mission core, live work pulses, and a compact event rail explains what changed without turning the screen into a log viewer.",
      scores: { "Demo impact": 5, "Clarity": 4, "Scale": 3 },
      rationale: "Creates an immediate hierarchy: mission first, active risks second, history last. It looks sophisticated while keeping one obvious focal point.",
      interaction: "Click any requirement to focus it. New work appears as a pulse from the mission core; the activity rail receives one finite animated entry per durable event.",
      reference: "Palantir Workshop common-operational-picture patterns, Palantir Workflow Lineage focus, and Datadog inspect mode.",
      render: renderCommandDeck
    },
    {
      title: "Swimlane Assembly Line",
      short: "Assembly Line",
      subtitle: "Requirements move through stations",
      kicker: "CONCEPT 02 · PROCESS CLARITY",
      description: "Every requirement owns a lane, and agents occupy explicit stations: Analyze, Resolve, Review, Integrate. The user always knows what came before, what is live, and what remains.",
      scores: { "Demo impact": 4, "Clarity": 5, "Scale": 5 },
      rationale: "Lifecycle is encoded spatially rather than inferred from tangled links. It remains legible with dozens of requirements and makes parallelism feel tangible.",
      interaction: "Worker capsules animate between stations. Select a lane for a focused work order, or filter to only active and blocked requirements.",
      reference: "Palantir Pipeline Builder, workflow lineage, manufacturing kanban, and deployment-pipeline UIs.",
      render: renderAssembly
    },
    {
      title: "Mission Constellation",
      short: "Constellation",
      subtitle: "Focus-driven service map",
      kicker: "CONCEPT 03 · CINEMATIC GRAPH",
      description: "A deliberately cinematic map: the mission is the star, requirements form a constellation, and directional activity travels along only the currently meaningful relationships.",
      scores: { "Demo impact": 5, "Clarity": 3, "Scale": 3 },
      rationale: "Strongest wow effect. The complexity is visible, but progressive disclosure prevents the user from seeing every edge at once.",
      interaction: "Hover highlights local dependencies. Click focuses a constellation and dims unrelated work; wheel zoom and a side inspector would reveal the next layer.",
      reference: "Datadog Service Map, Grafana Node Graph, topology explorers, and selective Palantir lineage expansion.",
      render: renderGalaxy
    },
    {
      title: "Trace Theater",
      short: "Trace Theater",
      subtitle: "Execution as a narrated timeline",
      kicker: "CONCEPT 04 · EXPLAINABILITY",
      description: "A chronological execution story instead of a graph. Parallel agent calls collapse into groups, active spans glow, and a persistent inspector keeps surrounding context visible.",
      scores: { "Demo impact": 4, "Clarity": 5, "Scale": 4 },
      rationale: "Best answer to “what exactly happened?” It preserves causality and makes starts, exits, retries, and reviews understandable to non-technical observers.",
      interaction: "Expand grouped parallel calls, scrub the time ruler, or click a span to inspect evidence counts, predecessor, result, duration, and status.",
      reference: "LangSmith Threads/Traces/Runs, distributed tracing, Temporal histories, and IDE execution timelines.",
      render: renderTrace
    },
    {
      title: "Agent City",
      short: "Agent City",
      subtitle: "A spatial control-room metaphor",
      kicker: "CONCEPT 05 · DEMO STORYTELLING",
      description: "Requirements become rooms in an operations complex. Workers visibly enter rooms, the Planner occupies a control tower, and room lighting communicates health at a glance.",
      scores: { "Demo impact": 5, "Clarity": 4, "Scale": 3 },
      rationale: "The spatial metaphor is instantly understandable in a live demo: you can point at who is working where without teaching graph semantics.",
      interaction: "Rooms light up when work starts, worker avatars move between rooms, and selecting a building reveals its work order and recent visitors.",
      reference: "Operations-center floor plans, multiplayer presence UIs, incident war rooms, and Palantir Workshop object interactions.",
      render: renderCity
    },
    {
      title: "Mission Matrix Radar",
      short: "Matrix Radar",
      subtitle: "Dense enterprise control plane",
      kicker: "CONCEPT 06 · OPERATOR DENSITY",
      description: "A high-density matrix treats every requirement × lifecycle stage as an observable cell. Micro-telemetry, health, ownership, and progress stay comparable without drawing a single crossing edge.",
      scores: { "Demo impact": 3, "Clarity": 5, "Scale": 5 },
      rationale: "Most scalable and least ambiguous. It sacrifices cinematic motion for excellent scanning, sorting, and exception management.",
      interaction: "Sort and filter rows, select any cell for drill-down, watch live cells pulse, and use the heat strip as an overview of the whole mission.",
      reference: "Palantir operational grids and inboxes, SRE status matrices, dense incident consoles, and observability heatmaps.",
      render: renderMatrix
    },
    {
      title: "TES · Total Execution Graph",
      short: "TES · Total Execution Graph",
      subtitle: "Action-level causal graph",
      kicker: "CONCEPT 07 · TES · TOTAL EXECUTION GRAPH",
      description: "A dense action-level graph where requirements, ephemeral agent sessions, concrete actions, evidence, and causal handoffs coexist. Everything mode is intentionally busy; Signal mode keeps only active and critical threads.",
      scores: { "Demo impact": 5, "Clarity": 3, "Scale": 4 },
      rationale: "Makes the work itself the object of attention. Causal edges and evidence nodes expose how a durable result is assembled instead of implying that a requirement is doing the work.",
      interaction: "Select any node to focus its neighborhood. Switch granularity between Everything and Signal, then advance the shared fixture to watch action and edge emphasis move.",
      reference: "Distributed tracing service graphs, causal DAG explorers, event-sourcing timelines, and graph-lens interfaces.",
      render: renderTotalExecutionGraph
    },
    {
      title: "Agent Spawn Wall",
      short: "Agent Spawn Wall",
      subtitle: "Ephemeral consoles in motion",
      kicker: "CONCEPT 08 · EPHEMERAL AGENTS",
      description: "The primary object is a live agent console. Sessions enter and leave the wall as the fixture advances; safe action lines make each active worker legible while completed agents collapse into a recent stack.",
      scores: { "Demo impact": 5, "Clarity": 4, "Scale": 3 },
      rationale: "Puts agent presence ahead of requirement bookkeeping. It demonstrates that a mission is a stream of short-lived, inspectable workers rather than a static checklist.",
      interaction: "Click an active console or a completed session in the recent stack. Advance the fixture to rotate workers between RUNNING and COMPLETED states.",
      reference: "Terminal multiplexers, incident responder walls, ephemeral Kubernetes workload views, and live operations consoles.",
      render: renderAgentSpawnWall
    },
    {
      title: "Temporal Braid",
      short: "Temporal Braid",
      subtitle: "Agent lifelines across time",
      kicker: "CONCEPT 09 · TEMPORAL CAUSALITY",
      description: "Horizontal time is primary. Agent lifelines weave across requirements and stages as action spans, ticks, parallel work, and explicit handoffs converge on a moving NOW line.",
      scores: { "Demo impact": 4, "Clarity": 5, "Scale": 4 },
      rationale: "Preserves ordering and concurrency in one read. The braid shows where agents overlap, where a handoff happens, and which chain is currently live.",
      interaction: "Click a strand or handoff to highlight its connected chain. Scroll horizontally for the full ruler and advance the fixture to move NOW.",
      reference: "Distributed tracing waterfalls, process-mining timelines, Temporal workflow histories, and lifeline diagrams.",
      render: renderTemporalBraid
    },
    {
      title: "Mission Flame Graph",
      short: "Mission Flame Graph",
      subtitle: "Nested effort and critical path",
      kicker: "CONCEPT 10 · NESTED EFFORT",
      description: "Mission → requirement → agent → action → evidence is rendered as stacked flame blocks. Width encodes simulated effort, depth encodes nesting, active blocks pulse, and a rail marks the critical path.",
      scores: { "Demo impact": 4, "Clarity": 4, "Scale": 5 },
      rationale: "Compresses a large execution tree into a familiar performance-analysis metaphor. Wide blocks expose effort concentration without pretending to report production latency.",
      interaction: "Click a block to focus its subtree. Everything mode shows the full nested fixture; Signal mode keeps active and critical branches in view.",
      reference: "CPU flame graphs, build profilers, call-tree explorers, and hierarchical workload analysis.",
      render: renderMissionFlameGraph
    },
    {
      title: "Semantic Zoom Atlas",
      short: "Semantic Zoom Atlas",
      subtitle: "One metaphor, five levels",
      kicker: "CONCEPT 11 · SEMANTIC ZOOM",
      description: "A nested circle atlas lets the user move from mission overview to requirement, agent, action, and evidence detail. Labels change with the semantic level so the same visual metaphor remains comprehensible.",
      scores: { "Demo impact": 5, "Clarity": 4, "Scale": 4 },
      rationale: "Balances overview and inspection without opening a second screen. Explicit levels make progressive disclosure a deliberate interaction instead of a hidden zoom gesture.",
      interaction: "Use the level controls or click a circle to zoom into its next semantic layer. The breadcrumb and inspector keep the selected chain visible.",
      reference: "Semantic zoom maps, nested containment diagrams, genome browsers, and progressive-disclosure topology explorers.",
      render: renderSemanticZoomAtlas
    },
    {
      title: "Event Storm",
      short: "Event Storm",
      subtitle: "Kinetic activity field",
      kicker: "CONCEPT 12 · KINETIC ACTIVITY",
      description: "A full-screen event field turns actions and agent sessions into particles and trails. Requirements are faint gravitational zones; new events spawn, move, and fade while the current worker remains identifiable.",
      scores: { "Demo impact": 5, "Clarity": 3, "Scale": 4 },
      rationale: "Best for communicating motion and emergence. It makes a busy mission feel alive while the selected-event inspector provides a grounded, safe explanation of what each particle means.",
      interaction: "Freeze the field for inspection or follow the newest event. Click any particle to inspect its action, requirement, agent, and evidence context.",
      reference: "Network traffic particle fields, observability event streams, physics-inspired dashboards, and incident activity maps.",
      render: renderEventStorm
    }
  ];

  var state = {
    concept: 0,
    selectedReq: "REQ-006",
    selectedAgent: "AG-IN-01",
    selectedAction: "ACT-001",
    selectedNode: "REQ-006",
    granularity: "everything",
    atlasLevel: 0,
    atlasFocus: "REQ-006",
    stormFrozen: false,
    stormFollow: true,
    eventIndex: 0,
    autoplay: true,
    recentEvents: [events[0], events[1], { req: "REQ-005", role: "Business Reviewer", code: "BR", action: "Finalize accepted review", stage: 4 }]
  };

  var conceptList = document.getElementById("concept-list");
  var stage = document.getElementById("concept-stage");
  var autoplayTimer = null;

  function esc(value) {
    return String(value).replace(/[&<>\"]/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[character];
    });
  }

  function reqById(id) {
    return requirements.filter(function (item) { return item.id === id; })[0] || requirements[0];
  }

  function activeEvent() {
    return events[state.eventIndex % events.length];
  }

  function stateDot(value) {
    return '<i class="state-dot ' + esc(value) + '"></i>';
  }

  function statusTag(req) {
    return '<span class="status-tag ' + esc(req.state) + '">' + stateDot(req.state) + esc(req.state) + '</span>';
  }

  function detailCard(req, label) {
    var current = activeEvent();
    var role = req.id === current.req ? current.role : (req.state === "accepted" ? "Durable result" : "No active worker");
    return '<div class="detail-card">' +
      '<span class="detail-label">' + esc(label || "SELECTED REQUIREMENT") + '</span>' +
      '<h3>' + esc(req.id) + '</h3>' +
      '<p>' + esc(req.name) + '</p>' +
      '<div class="detail-progress"><i style="width:' + req.progress + '%"></i></div>' +
      '<p><strong>' + esc(role) + '</strong><br>' + esc(req.progress) + '% durable progress · ' + esc(req.state) + '</p>' +
      '</div>';
  }

  function metricStrip() {
    var completed = requirements.filter(function (r) { return r.state === "accepted"; }).length;
    var live = requirements.filter(function (r) { return r.state === "running"; }).length;
    return '<div class="metric-strip">' +
      '<div class="metric"><span>LIVE WORKERS</span><strong>' + live + ' <small>now</small></strong></div>' +
      '<div class="metric"><span>WAITING</span><strong>0 <small>dependencies</small></strong></div>' +
      '<div class="metric"><span>ACCEPTED</span><strong>' + completed + ' <small>requirements</small></strong></div>' +
      '<div class="metric"><span>CAPACITY</span><strong>' + live + '/8 <small>Planner excluded</small></strong></div>' +
      '<div class="metric"><span>ACTIVE FOCUS</span><strong>' + esc(activeEvent().req) + ' <small>' + esc(activeEvent().code) + '</small></strong></div>' +
      '</div>';
  }

  function navMarkup() {
    conceptList.innerHTML = concepts.map(function (concept, index) {
      return '<button class="concept-button ' + (index === state.concept ? 'is-active' : '') + '" type="button" data-concept="' + index + '">' +
        '<span class="concept-index">0' + (index + 1) + '</span><span><strong>' + esc(concept.short) + '</strong><span>' + esc(concept.subtitle) + '</span></span></button>';
    }).join("");
  }

  function renderHeader() {
    var concept = concepts[state.concept];
    document.getElementById("concept-kicker").textContent = concept.kicker;
    document.getElementById("concept-title").textContent = concept.title;
    document.getElementById("concept-description").textContent = concept.description;
    document.getElementById("concept-rationale").textContent = concept.rationale;
    document.getElementById("concept-interaction").textContent = concept.interaction;
    document.getElementById("concept-reference").textContent = concept.reference;
    document.getElementById("score-row").innerHTML = Object.keys(concept.scores).map(function (key) {
      return '<span class="score-chip">' + esc(key.toUpperCase()) + '<b>' + concept.scores[key] + '/5</b></span>';
    }).join("");
  }

  function render() {
    navMarkup();
    renderHeader();
    stage.innerHTML = concepts[state.concept].render();
    document.getElementById("sim-clock").textContent = "Observed just now · event " + (state.eventIndex + 1);
  }

  function renderCommandDeck() {
    var positions = [
      [18,16],[37,8],[62,9],[79,18],[84,43],[78,69],[61,80],[38,84],[16,72],[9,47],[29,32],[67,32],[31,65]
    ];
    var nodes = requirements.map(function (req, index) {
      return '<button class="orbit-node ' + esc(req.state) + ' ' + (req.id === state.selectedReq ? 'selected' : '') + '" data-req="' + req.id + '" style="left:' + positions[index][0] + '%;top:' + positions[index][1] + '%">' +
        '<strong>' + esc(req.id) + '</strong><span>' + stateDot(req.state) + esc(req.name) + '</span></button>';
    }).join("");
    var activity = state.recentEvents.slice(0,5).map(function (event,index) {
      return '<div class="event-item ' + (index === 0 ? 'live' : '') + '"><strong>' + esc(event.req) + ' · ' + esc(event.role) + '</strong><span>' + esc(event.action) + '</span><time>' + (index === 0 ? 'NOW' : (index * 3 + 2) + 'm ago') + '</time></div>';
    }).join("");
    return '<div class="command-deck">' + metricStrip() + '<div class="command-body"><div class="command-map">' +
      '<div class="mission-core"><span>MISSION CORE</span><strong>13 / 13</strong><small>VISIBLE OBJECTS</small></div>' + nodes +
      '</div><aside class="command-rail"><div class="rail-title"><span>LIVE SIGNAL</span><b>● CURRENT</b></div>' + activity + '</aside></div></div>';
  }

  function renderAssembly() {
    var stages = ["ANALYZE","RESOLVE","REVIEW","INTEGRATE"];
    var current = activeEvent();
    var rows = requirements.map(function (req) {
      var cells = stages.map(function (_, index) {
        var step = index + 1;
        if (req.id === current.req && step === current.stage) {
          return '<div class="station"><span class="worker-capsule"><i></i>' + esc(current.code) + ' · ' + esc(current.role) + '</span></div>';
        }
        return '<div class="station ' + (req.stage > step || req.stage === 4 && step === 4 ? 'done' : '') + '"></div>';
      }).join("");
      return '<div class="assembly-row ' + (req.id === state.selectedReq ? 'selected' : '') + '" data-req="' + req.id + '"><div class="lane-name"><strong>' + esc(req.id) + '</strong><span>' + esc(req.name) + '</span></div>' + cells + '</div>';
    }).join("");
    var selected = reqById(state.selectedReq);
    return '<div class="assembly"><div class="assembly-board"><div class="assembly-head"><span>REQUIREMENT</span>' + stages.map(function (s) { return '<span>' + s + '</span>'; }).join("") + '</div>' + rows + '</div>' +
      '<aside class="assembly-side">' + detailCard(selected,"WORK ORDER") + '<div class="assembly-legend"><div>' + stateDot("running") + ' Worker present now</div><div>' + stateDot("accepted") + ' Durable stage complete</div><div>' + stateDot("queued") + ' Station not started</div></div></aside></div>';
  }

  function renderGalaxy() {
    var positions = [[16,22],[31,12],[54,11],[71,18],[82,34],[83,57],[72,75],[55,86],[34,84],[17,70],[12,48],[34,34],[65,48]];
    var nodes = requirements.map(function (req,index) {
      return '<button class="galaxy-node ' + esc(req.state) + ' ' + (req.id === state.selectedReq ? 'selected' : '') + '" data-req="' + req.id + '" style="--x:' + positions[index][0] + '%;--y:' + positions[index][1] + '%"><strong>' + esc(req.id.replace("REQ-","R")) + '</strong><span>' + esc(req.state) + '</span></button>';
    }).join("");
    var links = [
      [270,-152],[170,-125],[220,-49],[315,-23],[320,34],[245,81],[182,138],[260,153],[290,173],[310,196],[150,215],[200,248],[160,312]
    ].map(function (item,index) {
      return '<i class="galaxy-link ' + (requirements[index].state === "running" ? 'running' : '') + '" style="--len:' + item[0] + 'px;--angle:' + item[1] + 'deg"></i>';
    }).join("");
    return '<div class="galaxy">' + links + '<div class="galaxy-core"><strong>MISSION<br>CORE</strong><span>2 LIVE / 13</span></div>' + nodes +
      '<div class="galaxy-detail">' + detailCard(reqById(state.selectedReq),"FOCUSED CONSTELLATION") + '</div>' +
      '<div class="galaxy-key">SCROLL TO ZOOM · DRAG TO PAN<br>CLICK A NODE TO ISOLATE ITS LOCAL DEPENDENCIES</div></div>';
  }

  function renderTrace() {
    var current = activeEvent();
    var traceEvents = [
      { time:"04:12:08", req:current.req, code:current.code, role:current.role, action:current.action, duration:"LIVE", live:true },
      { time:"04:11:42", req:"REQ-011", code:"AO", role:"Analytical Owner", action:"Produced evidence-backed finding set", duration:"06:18" },
      { time:"04:10:33", req:"REQ-006", code:"ID", role:"Identity Owner", action:"Resolved supplier and route identities", duration:"03:44" },
      { time:"04:07:19", req:"REQ-005", code:"BR", role:"Business Reviewer", action:"Accepted cross-domain operations review", duration:"02:11" },
      { time:"04:03:55", req:"REQ-004", code:"IN", role:"Integration Agent", action:"Committed receivables result", duration:"01:08" }
    ];
    var rows = traceEvents.map(function (event,index) {
      return '<div class="trace-row ' + (event.live ? 'live ' : '') + (event.req === state.selectedReq ? 'selected' : '') + '" data-req="' + event.req + '"><time class="trace-time">' + event.time + '</time><span class="trace-icon">' + event.code + '</span><div class="trace-copy"><strong>' + esc(event.req) + ' · ' + esc(event.role) + '</strong><span>' + esc(event.action) + '</span></div><span class="trace-duration">' + event.duration + '</span></div>' +
      (index === 1 ? '<div class="parallel-group"><strong>＋ 4 PARALLEL CALLS</strong> Files, identity checks, analytical diagnostics, policy evidence</div>' : '');
    }).join("");
    var selected = reqById(state.selectedReq);
    return '<div class="trace-theater"><div class="trace-toolbar"><div class="trace-tabs"><span class="trace-tab active">MISSION</span><span class="trace-tab">REQUIREMENTS</span><span class="trace-tab">AGENT RUNS</span></div><span>0:00 ───── 5:00 ───── 10:00 ───── NOW</span></div>' +
      '<div class="trace-body"><div class="trace-list">' + rows + '</div><aside class="trace-inspector"><div class="inspector-head"><span>SPAN INSPECTOR</span><h3>' + esc(selected.id) + '</h3></div><div class="inspector-section"><span>EXECUTION</span><dl><dt>Status</dt><dd>' + esc(selected.state) + '</dd><dt>Progress</dt><dd>' + selected.progress + '%</dd><dt>Stage</dt><dd>' + selected.stage + ' / 4</dd><dt>Worker</dt><dd>' + esc(current.code) + '</dd></dl></div><div class="inspector-section"><span>SAFE EVIDENCE</span><dl><dt>Durable events</dt><dd>18</dd><dt>Files observed</dt><dd>7</dd><dt>Findings</dt><dd>4</dd><dt>Warnings</dt><dd>0</dd></dl></div><div class="inspector-section"><span>CONTEXT</span><p style="color:var(--muted);font-size:9px;line-height:1.5">Surrounding mission context remains visible while this span is selected.</p></div></aside></div></div>';
  }

  function renderCity() {
    var current = activeEvent();
    var rooms = requirements.map(function (req,index) {
      var worker = req.id === current.req ? '<span class="worker-person">' + esc(current.code) + '</span>' : '';
      var desks = '<div class="room-floor"><i class="desk"></i><i class="desk"></i><i class="desk"></i></div>';
      return '<button class="city-room ' + esc(req.state) + ' ' + (req.id === state.selectedReq ? 'selected' : '') + ' ' + (index === 0 ? 'planner-tower' : '') + '" data-req="' + req.id + '"><strong>' + (index === 0 ? 'PLANNER + ' : '') + esc(req.id) + '</strong><span>' + esc(req.name) + '</span>' + desks + worker + '</button>';
    }).join("");
    return '<div class="agent-city"><div class="city-hud"><span>13 ROOMS ONLINE</span><span>2 WORKERS PRESENT</span><span>0 INCIDENTS</span></div><div class="city-grid">' + rooms + '</div>' +
      '<aside class="city-side"><div class="detail-card"><span class="detail-label">OPERATIONS DIRECTORY</span><h3>' + esc(state.selectedReq) + '</h3><p>' + esc(reqById(state.selectedReq).name) + '</p><div class="worker-roster"><div><b>' + esc(current.code) + '</b><span><strong>' + esc(current.role) + '</strong>' + esc(current.action) + '</span></div><div><b>PL</b><span><strong>Planner · Control tower</strong>Schedules work; excluded from capacity</span></div><div><b>BR</b><span><strong>Business Reviewer</strong>Present in REQ-011 review room</span></div></div></div></aside></div>';
  }

  function renderMatrix() {
    var current = activeEvent();
    var stageNames = ["ANALYZE","RESOLVE","REVIEW","INTEGRATE"];
    var heat = requirements.map(function (req) { return '<i class="' + (req.state === 'accepted' ? 'done' : req.state === 'running' ? 'live' : '') + '"></i>'; }).join("");
    var rows = requirements.map(function (req,index) {
      var cells = stageNames.map(function (_,stageIndex) {
        var step = stageIndex + 1;
        var live = req.id === current.req && current.stage === step;
        var done = req.stage > step || (req.state === "accepted");
        return '<td data-req="' + req.id + '"><span class="cell-state ' + (live ? 'live' : done ? 'done' : '') + '">' + (live ? stateDot('running') + esc(current.code) + ' LIVE' : done ? '✓ DURABLE' : '—') + '</span></td>';
      }).join("");
      var bars = [4,9,6,14,11,17].map(function (height,barIndex) { return '<i style="height:' + (((height + index * 2 + barIndex) % 16) + 4) + 'px"></i>'; }).join("");
      return '<tr class="' + (req.id === state.selectedReq ? 'selected' : '') + '"><td data-req="' + req.id + '"><div class="matrix-req"><strong>' + esc(req.id) + '</strong><span>' + esc(req.name) + '</span></div></td>' + cells + '<td data-req="' + req.id + '"><div class="micro-bars">' + bars + '</div></td></tr>';
    }).join("");
    return '<div class="mission-matrix"><div class="matrix-summary"><div><strong>Mission completion radar</strong><br><span>5 accepted · 2 active · 6 queued</span></div><div class="heatbar">' + heat + '</div></div><div class="matrix-wrap"><table class="matrix-table"><thead><tr><th>REQUIREMENT</th>' + stageNames.map(function (s) { return '<th>' + s + '</th>'; }).join("") + '<th>ACTIVITY</th></tr></thead><tbody>' + rows + '</tbody></table></div><div class="matrix-foot">' + detailCard(reqById(state.selectedReq),"CELL INSPECTOR") + '</div></div>';
  }

  function agentById(id) {
    return agentCatalog.filter(function (agent) { return agent.id === id; })[0] || agentCatalog[0];
  }

  function actionById(id) {
    return actionCatalog.filter(function (action) { return action.id === id; })[0] || actionCatalog[0];
  }

  function evidenceById(id) {
    return evidenceCatalog.filter(function (evidence) { return evidence.id === id; })[0] || evidenceCatalog[0];
  }

  function agentForEvent(event) {
    return agentCatalog.filter(function (agent) { return agent.code === event.code; })[0] || agentCatalog[0];
  }

  function actionForEvent(event) {
    var exact = actionCatalog.filter(function (action) { return action.label === event.action; })[0];
    if (exact) { return exact; }
    var match = actionCatalog.filter(function (action) {
      return action.req === event.req && agentById(action.agent).code === event.code;
    })[0];
    return match || actionCatalog[state.eventIndex % actionCatalog.length];
  }

  function signalNode(node) {
    var current = activeEvent();
    var currentAgent = agentForEvent(current);
    return node.id === state.selectedNode || node.id === state.selectedReq || node.req === state.selectedReq ||
      node.req === current.req || node.id === current.req || node.agent === currentAgent.id || node.critical === true;
  }

  function tesNodeMarkup(node) {
    var hidden = state.granularity === "signal" && !signalNode(node) ? " signal-hidden" : "";
    var selected = node.id === state.selectedNode || (node.kind === "req" && node.id === state.selectedReq) ? " selected" : "";
    var live = node.req === activeEvent().req || node.id === activeEvent().req ? " live" : "";
    var attrs = 'data-node="' + esc(node.id) + '" data-req="' + esc(node.req || node.id) + '"';
    if (node.agent) { attrs += ' data-agent="' + esc(node.agent) + '"'; }
    if (node.action) { attrs += ' data-action="' + esc(node.action) + '"'; }
    if (node.evidence) { attrs += ' data-evidence="' + esc(node.evidence) + '"'; }
    return '<button class="tes-node ' + esc(node.kind) + (node.kind === "agent" ? " session" : "") + hidden + selected + live + '" ' + attrs + ' style="left:' + node.x + '%;top:' + node.y + '%" title="' + esc(node.label) + '"><strong>' + esc(node.short || node.id) + '</strong><small>' + esc(node.label) + '</small></button>';
  }

  function renderTotalExecutionGraph() {
    var reqPositions = [[11,19],[24,11],[40,10],[57,12],[72,22],[84,37],[83,59],[71,76],[53,86],[35,84],[19,74],[9,53],[28,53]];
    var agentPositions = [[38,30],[56,28],[69,43],[60,67],[38,68]];
    var actionPositions = [[24,31],[42,39],[58,38],[75,33],[79,51],[71,65],[57,76],[40,77],[22,65],[16,43],[36,52],[52,56],[65,54],[48,17],[31,17],[88,49]];
    var evidencePositions = [[17,28],[35,35],[49,33],[68,27],[88,31],[87,65],[64,84],[29,81],[10,67],[5,39],[33,59],[53,65],[73,56],[52,9],[26,8],[94,57]];
    var nodes = [];
    requirements.forEach(function (req, index) {
      nodes.push({ id:req.id, kind:"req", req:req.id, label:req.name, short:req.id.replace("REQ-","R"), x:reqPositions[index][0], y:reqPositions[index][1], critical:req.state === "running" });
    });
    agentCatalog.forEach(function (agent, index) {
      nodes.push({ id:agent.id, kind:"agent", agent:agent.id, label:agent.role, short:agent.code, x:agentPositions[index][0], y:agentPositions[index][1], critical:agent.id === agentForEvent(activeEvent()).id });
    });
    actionCatalog.forEach(function (action, index) {
      nodes.push({ id:action.id, kind:"action", req:action.req, agent:action.agent, action:action.id, label:action.label, short:action.id.replace("ACT-","A"), x:actionPositions[index][0], y:actionPositions[index][1], critical:action.critical });
    });
    evidenceCatalog.forEach(function (evidence, index) {
      nodes.push({ id:evidence.id, kind:"evidence", req:evidence.req, action:evidence.action, evidence:evidence.id, label:evidence.label, short:evidence.id.replace("EVD-","E"), x:evidencePositions[index][0], y:evidencePositions[index][1], critical:evidence.req === activeEvent().req });
    });
    var byId = {};
    nodes.forEach(function (node) { byId[node.id] = node; });
    var edges = [];
    actionCatalog.forEach(function (action, index) {
      edges.push([action.req, action.agent, ""]);
      edges.push([action.agent, action.id, "active"]);
      edges.push([action.id, action.evidence, ""]);
      if (index > 0) { edges.push([actionCatalog[index - 1].id, action.id, "causal"]); }
    });
    var currentAction = actionForEvent(activeEvent());
    var links = edges.map(function (edge) {
      var from = byId[edge[0]], to = byId[edge[1]];
      if (!from || !to) { return ""; }
      var active = edge[0] === currentAction.id || edge[1] === currentAction.id || from.req === activeEvent().req || to.req === activeEvent().req;
      var hidden = state.granularity === "signal" && !active && !signalNode(from) && !signalNode(to) ? " signal-hidden" : "";
      return '<line class="tes-link ' + (edge[2] === "causal" ? "causal " : "") + (active ? "active" : "") + hidden + '" x1="' + from.x + '%" y1="' + from.y + '%" x2="' + to.x + '%" y2="' + to.y + '%"></line>';
    }).join("");
    var focus = byId[state.selectedNode] || byId[state.selectedReq] || byId[currentAction.id];
    var minimap = nodes.slice(0, 25).map(function (node, index) {
      return '<i style="left:' + (8 + (node.x * .82)) + '%;top:' + (17 + (node.y * .7)) + '%"></i>';
    }).join("");
    return '<div class="tes-graph"><div class="tes-toolbar"><div><strong>TES / Total Execution Graph</strong><span>' + (state.granularity === "everything" ? "EVERYTHING · 50 nodes · causal + evidence edges" : "SIGNAL · active + critical neighborhood") + '</span></div><div class="tes-legend"><span><i class="tes-key req"></i>REQ</span><span><i class="tes-key agent"></i>AGENT / SESSION</span><span><i class="tes-key action"></i>ACTION</span><span><i class="tes-key evidence"></i>EVIDENCE</span></div></div><div class="tes-canvas"><svg class="tes-links" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">' + links + '</svg>' + nodes.map(tesNodeMarkup).join("") + '<aside class="tes-focus"><span class="detail-label">FOCUSED NODE</span><strong>' + esc(focus.id) + '</strong><p>' + esc(focus.label) + '<br>' + esc(focus.kind.toUpperCase()) + ' · ' + esc(focus.req || focus.agent || "MISSION") + '</p></aside><div class="tes-minimap"><span>OVERVIEW / LENS</span>' + minimap + '</div></div></div>';
  }

  function renderAgentSpawnWall() {
    var current = activeEvent();
    var currentAgent = agentForEvent(current);
    var activeAgents = agentCatalog.filter(function (agent, index) {
      if (state.granularity === "signal") { return agent.id === currentAgent.id || agent.id === state.selectedAgent; }
      return agent.id === currentAgent.id || ((index + state.eventIndex) % 3 !== 0);
    });
    var completedAgents = agentCatalog.filter(function (agent) { return activeAgents.indexOf(agent) === -1; });
    var consoles = activeAgents.map(function (agent) {
      var work = actionCatalog.filter(function (action) { return action.agent === agent.id; });
      var context = work[(state.eventIndex + work.length) % work.length];
      var lines = [context.label, work[(state.eventIndex + 1) % work.length].label, "Emit bounded evidence receipt"].map(function (line) { return '<li>' + esc(line) + '</li>'; }).join("");
      return '<article class="agent-console active ' + (agent.id === state.selectedAgent ? "selected" : "") + '" data-agent="' + esc(agent.id) + '" data-req="' + esc(context.req) + '"><div class="console-top"><span class="console-id"><i></i>' + esc(agent.id) + '</span><span class="console-status">RUNNING · LIVE</span></div><div class="console-body"><span class="console-context" data-req="' + esc(context.req) + '">' + esc(context.req) + ' · ' + esc(reqById(context.req).name) + '</span><ul class="console-lines">' + lines + '</ul></div></article>';
    }).join("");
    var recent = completedAgents.map(function (agent) {
      var work = actionCatalog.filter(function (action) { return action.agent === agent.id; });
      var context = work[(state.eventIndex + 1) % work.length];
      return '<button class="recent-agent" type="button" data-agent="' + esc(agent.id) + '" data-req="' + esc(context.req) + '"><i></i>' + esc(agent.code) + ' · ' + esc(agent.role) + '</button>';
    }).join("");
    var selected = agentById(state.selectedAgent);
    var selectedWork = actionCatalog.filter(function (action) { return action.agent === selected.id; })[state.eventIndex % actionCatalog.filter(function (action) { return action.agent === selected.id; }).length];
    return '<div class="spawn-wall"><div class="spawn-head"><div><strong>Agent Spawn Wall</strong><span>' + activeAgents.length + ' ephemeral sessions present · capacity ' + activeAgents.length + '/8 · Planner excluded</span></div><span class="spawn-signal">EVENT ' + String(state.eventIndex + 1).padStart(2,"0") + ' / 08</span></div><div class="spawn-grid">' + consoles + '<div class="recent-stack"><strong>RECENTLY COMPLETED</strong>' + (recent || '<span class="console-status">No completed sessions in this frame</span>') + '</div></div><aside class="spawn-inspector"><span>SESSION INSPECTOR</span><strong>' + esc(selected.id) + ' · ' + esc(selected.code) + '</strong><p>' + esc(selected.role) + '<br>Context: ' + esc(selectedWork.req) + ' · ' + esc(selectedWork.label) + '<br><span class="console-status">Ephemeral fixture session · safe action preview</span></p></aside></div>';
  }

  function renderTemporalBraid() {
    var currentAction = actionForEvent(activeEvent());
    var now = 23 + ((state.eventIndex * 9) % 57);
    var handoffs = [[27,0],[52,1],[74,2],[44,3],[67,4]].map(function (point, index) {
      return '<i class="braid-handoff" style="left:' + point[0] + '%;top:' + (43 + point[1] * 69) + 'px">↗</i>';
    }).join("");
    var rows = agentCatalog.map(function (agent) {
      var work = actionCatalog.filter(function (action) { return action.agent === agent.id; });
      if (state.granularity === "signal") { work = work.filter(function (action) { return action.critical || action.id === currentAction.id || action.id === state.selectedAction; }); }
      var spans = work.map(function (action, index) {
        var selected = action.id === state.selectedAction || action.req === state.selectedReq ? " selected" : "";
        var live = action.id === currentAction.id ? " live" : "";
        var type = action.stage >= 3 ? "" : action.stage === 2 ? " review" : " action";
        return '<button class="braid-span ' + type + (action.stage === 4 ? " done" : "") + selected + live + '" type="button" data-action="' + esc(action.id) + '" data-agent="' + esc(agent.id) + '" data-req="' + esc(action.req) + '" style="left:' + action.start + '%;width:' + action.duration + '%" title="' + esc(action.label) + '">' + esc(action.id.replace("ACT-","A")) + ' · ' + esc(action.req) + '</button>';
      }).join("");
      return '<div class="braid-row"><div class="braid-agent"><strong>' + esc(agent.id) + '</strong><span>' + esc(agent.role) + '</span></div><div class="braid-track">' + spans + '</div></div>';
    }).join("");
    var selected = actionById(state.selectedAction || currentAction.id);
    return '<div class="temporal-braid"><div class="braid-toolbar"><strong>Temporal Braid <span>agent lifelines · action spans · handoffs</span></strong><span>Signal follows event order · NOW moves with the fixture</span></div><div class="braid-body"><div class="braid-ruler"><i></i><span>T+00</span><span>T+10</span><span>T+20</span><span>T+30</span><span>T+40</span><span>T+50</span></div><div class="braid-lanes"><div class="braid-now" style="left:' + now + '%"></div><div class="braid-parallel">PARALLEL WORK · 4 ACTIONS OVERLAP</div>' + handoffs + rows + '</div><aside class="braid-inspector"><span>STRAND INSPECTOR</span><strong>' + esc(selected.id) + ' · ' + esc(selected.req) + '</strong><p>' + esc(selected.label) + '<br>' + esc(agentById(selected.agent).role) + ' · stage ' + selected.stage + '<br><span class="console-status">Causal predecessor → evidence receipt</span></p></aside></div></div>';
  }

  function flameBlock(className, id, req, label, width, meta, attrs, active, selected) {
    return '<button class="flame-block ' + className + (active ? " active" : "") + (selected ? " selected" : "") + '" type="button" ' + attrs + ' style="width:' + Math.max(17, Math.min(96, width)) + '%"><span>' + esc(id) + ' · ' + esc(label) + '</span><small>' + esc(meta) + '</small></button>';
  }

  function renderMissionFlameGraph() {
    var currentAction = actionForEvent(activeEvent());
    var criticalReqs = requirements.filter(function (req) { return req.state === "running" || req.id === activeEvent().req || req.id === state.selectedReq || req.id === "REQ-005" || req.id === "REQ-001"; });
    var shown = state.granularity === "signal" ? criticalReqs : requirements;
    var blocks = flameBlock("mission", "MISSION", "", "Night Operations", 94, "13 requirements", "data-node=\"MISSION\"", false, state.selectedNode === "MISSION");
    shown.forEach(function (req, reqIndex) {
      var reqActions = actionCatalog.filter(function (action) { return action.req === req.id; });
      if (state.granularity === "signal") { reqActions = reqActions.filter(function (action) { return action.critical || action.id === currentAction.id || action.id === state.selectedAction; }); }
      var reqWidth = 31 + (req.progress * .58) + ((reqIndex % 3) * 4);
      blocks += flameBlock("req", req.id, req.id, req.name, reqWidth, req.progress + "% durable", 'data-req="' + esc(req.id) + '" data-node="' + esc(req.id) + '"', req.id === activeEvent().req, req.id === state.selectedReq);
      var agentIds = [];
      reqActions.forEach(function (action) { if (agentIds.indexOf(action.agent) === -1) { agentIds.push(action.agent); } });
      agentIds.forEach(function (agentId, agentIndex) {
        var agent = agentById(agentId);
        var agentActions = reqActions.filter(function (action) { return action.agent === agentId; });
        blocks += flameBlock("agent", agent.id, req.id, agent.role, 25 + agentActions.length * 10 + agentIndex * 4, agent.code + " session", 'data-agent="' + esc(agent.id) + '" data-req="' + esc(req.id) + '" data-node="' + esc(agent.id) + '"', agent.id === agentForEvent(activeEvent()).id, agent.id === state.selectedAgent);
        agentActions.forEach(function (action) {
          blocks += flameBlock("action", action.id, req.id, action.label, 20 + action.duration * 2.5, action.duration + " ticks", 'data-action="' + esc(action.id) + '" data-agent="' + esc(agent.id) + '" data-req="' + esc(req.id) + '" data-node="' + esc(action.id) + '"', action.id === currentAction.id, action.id === state.selectedAction);
          blocks += flameBlock("evidence", action.evidence, req.id, evidenceById(action.evidence).label, 18 + action.duration * 1.5, "receipt", 'data-evidence="' + esc(action.evidence) + '" data-action="' + esc(action.id) + '" data-req="' + esc(req.id) + '" data-node="' + esc(action.evidence) + '"', false, action.evidence === actionById(state.selectedAction).evidence);
        });
      });
    });
    var selected = actionById(state.selectedAction || currentAction.id);
    return '<div class="flame-graph"><div class="flame-toolbar"><strong>Mission Flame Graph</strong><span>Width = simulated effort · depth = nesting</span><b class="flame-critical">CRITICAL PATH / ' + esc(currentAction.id) + '</b></div><div class="flame-root">' + blocks + '</div><div class="flame-rail"></div><aside class="flame-inspector"><span>SUBTREE FOCUS</span><strong>' + esc(selected.id) + ' · ' + esc(selected.req) + '</strong><p>' + esc(selected.label) + '<br>' + esc(agentById(selected.agent).role) + ' → ' + esc(selected.evidence) + '<br><span class="console-status">Selected blocks retain parent context</span></p></aside></div>';
  }

  function atlasNode(id, kind, label, short, x, y, req, attrs, hidden, selected) {
    return '<button class="atlas-node ' + kind + (hidden ? " signal-hidden" : "") + (selected ? " selected" : "") + '" type="button" data-node="' + esc(id) + '" data-req="' + esc(req || id) + '" ' + (attrs || "") + ' style="left:' + x + '%;top:' + y + '%"><strong>' + esc(short || id) + '</strong><small>' + esc(label) + '</small></button>';
  }

  function renderSemanticZoomAtlas() {
    var current = activeEvent();
    var currentAction = actionForEvent(current);
    var focusReq = reqById(state.atlasFocus && state.atlasFocus.indexOf("REQ-") === 0 ? state.atlasFocus : state.selectedReq);
    var reqPositions = [[9,22],[20,12],[34,10],[49,12],[64,17],[78,31],[83,50],[76,68],[62,81],[45,87],[28,82],[13,67],[10,47]];
    var html = '<div class="zoom-atlas"><div class="atlas-toolbar"><strong>Semantic Zoom Atlas</strong><span>LEVEL</span>' + ["MISSION","REQUIREMENT","AGENT","ACTION","EVIDENCE"].map(function (level, index) { return '<button class="atlas-level ' + (state.atlasLevel === index ? "active" : "") + '" type="button" data-atlas-level="' + index + '">' + level + '</button>'; }).join("") + '</div><div class="atlas-canvas"><i class="atlas-ring r1"></i><i class="atlas-ring r2"></i><i class="atlas-ring r3"></i><i class="atlas-ring r4"></i>';
    html += atlasNode("MISSION", "mission", "Night Operations Mission", "MISSION", 45, 50, "MISSION", "", false, state.atlasLevel === 0 && state.atlasFocus === "MISSION");
    requirements.forEach(function (req, index) {
      var hidden = state.granularity === "signal" && req.id !== focusReq.id && req.id !== current.req && req.state !== "running";
      var selected = req.id === focusReq.id;
      html += atlasNode(req.id, "requirement", req.name, req.id.replace("REQ-","R"), reqPositions[index][0], reqPositions[index][1], req.id, "", hidden, selected);
    });
    var focusActions = actionCatalog.filter(function (action) { return action.req === focusReq.id; });
    var focusAgents = agentCatalog.filter(function (agent) { return focusActions.some(function (action) { return action.agent === agent.id; }); });
    focusAgents.forEach(function (agent, index) {
      var hidden = state.atlasLevel < 1 || (state.granularity === "signal" && agent.id !== agentForEvent(current).id && agent.id !== state.selectedAgent);
      html += atlasNode(agent.id, "agent", agent.role, agent.code, [31,45,60,74,54][index], [31,20,29,43,67][index], focusReq.id, 'data-agent="' + esc(agent.id) + '"', hidden, agent.id === state.selectedAgent);
    });
    focusActions.forEach(function (action, index) {
      var hidden = state.atlasLevel < 2 || (state.granularity === "signal" && !action.critical && action.id !== currentAction.id && action.id !== state.selectedAction);
      html += atlasNode(action.id, "action", action.label, action.id.replace("ACT-","A"), [32,48,65,78][index], [60,73,63,74][index], action.req, 'data-agent="' + esc(action.agent) + '" data-action="' + esc(action.id) + '"', hidden, action.id === state.selectedAction);
    });
    focusActions.forEach(function (action, index) {
      var evidence = evidenceById(action.evidence);
      var hidden = state.atlasLevel < 3 || (state.granularity === "signal" && action.id !== currentAction.id && action.id !== state.selectedAction);
      html += atlasNode(evidence.id, "evidence", evidence.label, evidence.id.replace("EVD-","E"), [26,39,53,67][index], [76,84,83,87][index], action.req, 'data-action="' + esc(action.id) + '" data-evidence="' + esc(evidence.id) + '"', hidden, evidence.id === actionById(state.selectedAction).evidence);
    });
    var focusLabel = state.atlasLevel === 0 ? "Mission overview" : state.atlasLevel === 1 ? focusReq.name : state.atlasLevel === 2 ? agentById(state.selectedAgent).role : state.atlasLevel === 3 ? actionById(state.selectedAction).label : evidenceById(actionById(state.selectedAction).evidence).label;
    html += '<aside class="atlas-inspector"><span>SEMANTIC FOCUS · ' + ["MISSION","REQUIREMENT","AGENT","ACTION","EVIDENCE"][state.atlasLevel] + '</span><strong>' + esc(focusLabel) + '</strong><p>Click a circle to move deeper. Signal mode dims unrelated clusters while Everything keeps the full atlas.</p></aside><div class="atlas-breadcrumb">MISSION / ' + esc(focusReq.id) + ' / ' + esc(agentById(state.selectedAgent).code) + ' / ' + esc(actionById(state.selectedAction).id) + '</div></div></div>';
    return html;
  }

  function renderEventStorm() {
    var current = activeEvent();
    var currentAction = actionForEvent(current);
    var selected = actionById(state.stormFollow ? currentAction.id : state.selectedAction);
    var particles = actionCatalog.map(function (action, index) {
      var agent = agentById(action.agent);
      var x = 8 + ((index * 17 + 5) % 85), y = 11 + ((index * 29 + 7) % 75);
      var active = action.id === currentAction.id;
      var signalHidden = state.granularity === "signal" && !action.critical && !active && action.id !== state.selectedAction;
      var selectedClass = action.id === selected.id ? " selected" : "";
      return '<button class="storm-particle ' + (active ? "latest" : "") + (signalHidden ? " signal-hidden" : "") + selectedClass + '" type="button" aria-label="Action ' + esc(action.id) + ': ' + esc(action.label) + '" data-node="' + esc(action.id) + '" data-action="' + esc(action.id) + '" data-agent="' + esc(action.agent) + '" data-req="' + esc(action.req) + '" style="left:' + x + '%;top:' + y + '%;--dx:' + ((index % 4) * 12 - 18) + 'px;--dy:' + ((index % 5) * 9 - 18) + 'px;--drift:' + (4 + index % 4) + 's;--trail:' + (25 + index % 5 * 8) + 'px;--angle:' + (12 + index * 17) + 'deg" title="' + esc(action.label) + '"></button>';
    }).join("");
    var agentParticles = agentCatalog.map(function (agent, index) {
      var x = 16 + index * 17, y = 28 + ((index * 23) % 52);
      var signalHidden = state.granularity === "signal" && agent.id !== agentForEvent(current).id && agent.id !== state.selectedAgent;
      return '<button class="storm-particle agent ' + (signalHidden ? "signal-hidden " : "") + (agent.id === agentForEvent(current).id ? "selected" : "") + '" type="button" aria-label="Agent session ' + esc(agent.id) + ': ' + esc(agent.role) + '" data-node="' + esc(agent.id) + '" data-agent="' + esc(agent.id) + '" data-req="' + esc(current.req) + '" data-agent-code="' + esc(agent.code) + '" style="left:' + x + '%;top:' + y + '%;--dx:' + (10 - index * 3) + 'px;--dy:' + (index * 4 - 8) + 'px;--drift:' + (5 + index) + 's"></button>';
    }).join("");
    var evidenceParticles = evidenceCatalog.slice(0, 9).map(function (evidence, index) {
      var x = 12 + ((index * 21) % 82), y = 16 + ((index * 37) % 69);
      var signalHidden = state.granularity === "signal" && evidence.req !== current.req && evidence.action !== state.selectedAction;
      return '<button class="storm-particle evidence ' + (signalHidden ? "signal-hidden" : "") + '" type="button" aria-label="Evidence ' + esc(evidence.id) + '" data-node="' + esc(evidence.id) + '" data-evidence="' + esc(evidence.id) + '" data-action="' + esc(evidence.action) + '" data-req="' + esc(evidence.req) + '" style="left:' + x + '%;top:' + y + '%;--dx:' + (index % 3 * 9) + 'px;--dy:' + (index % 4 * 7) + 'px;--drift:' + (3 + index % 3) + 's"></button>';
    }).join("");
    return '<div class="event-storm ' + (state.stormFrozen ? "is-frozen" : "") + '"><div class="storm-toolbar"><strong>Event Storm</strong><span>' + (state.stormFrozen ? "FROZEN FOR INSPECTION" : "KINETIC FIELD · EVENTS FADE") + '</span><button class="storm-toggle ' + (state.stormFrozen ? "active" : "") + '" type="button" data-storm-control="freeze">' + (state.stormFrozen ? "Resume" : "Freeze") + '</button><button class="storm-toggle ' + (state.stormFollow ? "active" : "") + '" type="button" data-storm-control="follow">' + (state.stormFollow ? "Following" : "Follow latest") + '</button></div><div class="storm-field"><div class="storm-zone z1">REQ CLUSTER · SUPPLY</div><div class="storm-zone z2">REQ CLUSTER · RISK</div><div class="storm-zone z3">REQ CLUSTER · SERVICE</div>' + particles + agentParticles + evidenceParticles + '<aside class="storm-inspector"><span>SELECTED EVENT</span><strong>' + esc(selected.id) + ' · ' + esc(selected.req) + '</strong><p>' + esc(selected.label) + '<br>' + esc(agentById(selected.agent).code) + ' agent → ' + esc(selected.evidence) + '<br><span class="console-status">Fixture event ' + String(state.eventIndex + 1).padStart(2,"0") + ' · no run connection</span></p></aside><div class="storm-legend"><span><i></i>ACTION</span><span><i class="agent"></i>AGENT</span><span><i class="evidence"></i>EVIDENCE</span></div></div></div>';
  }

  function simulateNext() {
    var previous = activeEvent();
    state.eventIndex = (state.eventIndex + 1) % events.length;
    var current = activeEvent();
    var req = reqById(current.req);
    var liveSet = {};
    liveSet[current.req] = true;
    if (previous.req !== current.req && reqById(previous.req).state !== "accepted") {
      liveSet[previous.req] = true;
    }
    requirements.forEach(function (item) {
      if (item.state === "running" && !liveSet[item.id]) {
        item.state = item.stage >= 4 ? "accepted" : "queued";
      }
    });
    req.state = current.stage === 4 ? "accepted" : "running";
    req.stage = current.stage;
    req.progress = current.stage === 4 ? 100 : Math.min(92, current.stage * 23 + 8);
    state.selectedReq = current.req;
    state.selectedNode = current.req;
    if (state.concept !== 11 || state.stormFollow) { state.selectedAction = actionForEvent(current).id; }
    state.selectedAgent = agentForEvent(current).id;
    state.atlasFocus = current.req;
    state.recentEvents.unshift(current);
    state.recentEvents = state.recentEvents.slice(0,7);
    render();
  }

  function reset() {
    requirements = JSON.parse(initialRequirements);
    state.eventIndex = 0;
    state.selectedReq = "REQ-006";
    state.selectedAgent = "AG-IN-01";
    state.selectedAction = "ACT-001";
    state.selectedNode = "REQ-006";
    state.atlasLevel = 0;
    state.atlasFocus = "REQ-006";
    state.granularity = "everything";
    state.stormFrozen = false;
    state.stormFollow = true;
    state.recentEvents = [events[0],events[1],{ req:"REQ-005",role:"Business Reviewer",code:"BR",action:"Finalize accepted review",stage:4 }];
    document.getElementById("granularity-select").value = state.granularity;
    render();
  }

  function syncAutoplay() {
    var button = document.getElementById("autoplay-button");
    button.classList.toggle("is-active",state.autoplay);
    button.setAttribute("aria-pressed",String(state.autoplay));
    if (autoplayTimer) { window.clearInterval(autoplayTimer); }
    autoplayTimer = state.autoplay ? window.setInterval(simulateNext,3200) : null;
  }

  conceptList.addEventListener("click",function (event) {
    var target = event.target.closest("[data-concept]");
    if (!target) { return; }
    state.concept = Number(target.getAttribute("data-concept"));
    render();
  });

  stage.addEventListener("click",function (event) {
    var control = event.target.closest("[data-storm-control]");
    if (control) {
      var stormControl = control.getAttribute("data-storm-control");
      if (stormControl === "freeze") { state.stormFrozen = !state.stormFrozen; }
      if (stormControl === "follow") { state.stormFollow = !state.stormFollow; }
      render();
      return;
    }
    var level = event.target.closest("[data-atlas-level]");
    if (level) {
      state.atlasLevel = Number(level.getAttribute("data-atlas-level"));
      render();
      return;
    }
    var target = event.target.closest("[data-node],[data-req],[data-agent],[data-action],[data-evidence]");
    if (!target) { return; }
    var req = target.getAttribute("data-req");
    var agent = target.getAttribute("data-agent");
    var action = target.getAttribute("data-action");
    var node = target.getAttribute("data-node");
    if (req && req.indexOf("REQ-") === 0) { state.selectedReq = req; state.atlasFocus = req; }
    if (agent) { state.selectedAgent = agent; }
    if (action) { state.selectedAction = action; }
    if (node) { state.selectedNode = node; }
    if (target.closest(".storm-particle") && action) { state.stormFollow = false; }
    if (target.closest(".atlas-node")) {
      if (target.classList.contains("requirement")) { state.atlasLevel = Math.max(1, state.atlasLevel); }
      if (target.classList.contains("agent")) { state.atlasLevel = Math.max(2, state.atlasLevel); }
      if (target.classList.contains("action")) { state.atlasLevel = Math.max(3, state.atlasLevel); }
      if (target.classList.contains("evidence")) { state.atlasLevel = 4; }
    }
    render();
  });

  document.getElementById("granularity-select").addEventListener("change", function (event) {
    state.granularity = event.target.value === "signal" ? "signal" : "everything";
    render();
  });

  document.getElementById("next-button").addEventListener("click",simulateNext);
  document.getElementById("reset-button").addEventListener("click",reset);
  document.getElementById("autoplay-button").addEventListener("click",function () {
    state.autoplay = !state.autoplay;
    syncAutoplay();
  });

  render();
  syncAutoplay();
}());
