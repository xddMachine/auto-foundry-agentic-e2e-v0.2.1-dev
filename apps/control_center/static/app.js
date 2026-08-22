const state = {
  config: null,
  runs: [],
  snapshot: null,
  selectedRunId: null,
  eventFilter: "all",
  feedPaused: false,
  eventCursor: 0,
  eventStream: "",
  recentEventNodes: new Set(),
  files: [],
  graph: { x: 10, y: 8, scale: 0.82, dragging: false, startX: 0, startY: 0 },
  selectedNodeId: null,
  selectionGeneration: 0,
  snapshotRunId: null,
  snapshotGeneration: null,
};

function selectionRequestIsCurrent(
  requestedRunId,
  requestedGeneration,
  currentRunId = state.selectedRunId,
  currentGeneration = state.selectionGeneration,
) {
  return currentRunId === requestedRunId && currentGeneration === requestedGeneration;
}

function snapshotOwnershipIsCurrent(requestedRunId = state.selectedRunId, requestedGeneration = state.selectionGeneration) {
  return Boolean(state.snapshot)
    && selectionRequestIsCurrent(requestedRunId, requestedGeneration)
    && state.snapshotRunId === requestedRunId
    && state.snapshotGeneration === requestedGeneration;
}

function commitSnapshot(snapshot, requestedRunId, requestedGeneration) {
  if (!selectionRequestIsCurrent(requestedRunId, requestedGeneration)) return false;
  state.snapshot = snapshot;
  state.snapshotRunId = requestedRunId;
  state.snapshotGeneration = requestedGeneration;
  return true;
}

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const svgNS = "http://www.w3.org/2000/svg";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS(svgNS, tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function humanStatus(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function displayRunStatus(run) {
  const status = String(run?.status || "unknown");
  if (run?.placeholder && ["prepared", "starting"].includes(status)) return "Interpreting requirements";
  return humanStatus(status);
}

function roleLabel(role) {
  const labels = {
    planner: "Planner",
    entity_resolution_owner: "Entity Resolution",
    identity_reviewer: "Identity Reviewer",
    analytical_owner: "Analytical Owner",
    specialist: "Specialist",
    work_item: "Work item",
  };
  return labels[role] || humanStatus(role || "agent");
}

function roleGlyph(role) {
  return { planner: "PL", entity_resolution_owner: "ID", identity_reviewer: "RV", analytical_owner: "AO", specialist: "SP" }[role] || "WK";
}

function eventGlyph(category) {
  return { file: "▤", review: "◇", error: "!", dependency: "⌁", artifact: "✓", lifecycle: "→" }[category] || "·";
}

function formatTime(value) {
  if (!value) return "TIME —";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function relativeTime(value) {
  if (!value) return "no timestamp";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return String(value);
  const seconds = Math.round((Date.now() - time) / 1000);
  if (Math.abs(seconds) < 5) return "just now";
  if (seconds < 60) return `${Math.max(0, seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(time));
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "unknown size";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  }).then(async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || payload.message || `Request failed (${response.status})`);
      if (payload.errors && typeof payload.errors === "object") {
        error.errors = payload.errors;
        const details = Object.entries(payload.errors)
          .map(([key, value]) => `${key}: ${value}`)
          .filter(Boolean);
        if (details.length && !String(error.message).includes(details.join("; "))) {
          error.message = `${error.message}: ${details.join("; ")}`;
        }
      }
      throw error;
    }
    return payload;
  });
}

async function bootstrap() {
  bindNavigation();
  bindRunPicker();
  bindMissionControls();
  bindGraphControls();
  bindLaunchForm();
  bindInspector();
  try {
    [state.config, { runs: state.runs }] = await Promise.all([api("/api/config"), api("/api/runs")]);
    $("#connectionDot").classList.add("is-online");
    renderRunMenu();
    renderRunsTable();
    populateExistingRuns();
    if (!state.runs.length) throw new Error("No fixture or discoverable run is configured.");
    const fixture = state.runs.find((run) => run.source === "fixture");
    await selectRun((fixture || state.runs[0]).id);
    route();
    window.setInterval(updateFreshness, 1000);
    window.setInterval(pollEvents, 1000);
  } catch (error) {
    showFatal(error.message || String(error));
  }
}

function bindNavigation() {
  window.addEventListener("hashchange", route);
  $("#runSearch").addEventListener("input", renderRunsTable);
}

function route() {
  const requested = window.location.hash.replace("#", "") || "mission";
  const view = ["mission", "runs", "launch", "evidence"].includes(requested) ? requested : "mission";
  $$(".view").forEach((node) => node.classList.toggle("is-visible", node.dataset.view === view));
  $$(".rail-link").forEach((node) => node.classList.toggle("is-active", node.dataset.route === view));
  $("#viewCrumb").textContent = view.toUpperCase();
  if (view === "mission") window.setTimeout(fitGraph, 50);
  if (view === "evidence") renderEvidence();
}

function bindRunPicker() {
  const button = $("#runPickerButton");
  const menu = $("#runMenu");
  button.addEventListener("click", () => {
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    button.setAttribute("aria-expanded", String(willOpen));
  });
  document.addEventListener("click", (event) => {
    if (!button.contains(event.target) && !menu.contains(event.target)) {
      menu.hidden = true;
      button.setAttribute("aria-expanded", "false");
    }
  });
}

function renderRunMenu() {
  const menu = $("#runMenu");
  menu.replaceChildren();
  state.runs.forEach((run) => {
    const button = element("button", "run-option");
    button.type = "button";
    button.setAttribute("role", "option");
    button.classList.toggle("is-selected", run.id === state.selectedRunId);
    const glyph = element("span", "run-glyph", run.source === "fixture" ? "FX" : "AF");
    const copy = element("span");
    copy.append(element("strong", "", run.name), element("small", "", `${displayRunStatus(run)} · ${run.requirementCount || 0} requirements`));
    button.append(glyph, copy);
    button.addEventListener("click", async () => {
      menu.hidden = true;
      await selectRun(run.id);
      window.location.hash = "mission";
    });
    menu.append(button);
  });
}

async function selectRun(runId) {
  const requestedRunId = runId;
  const requestedGeneration = state.selectionGeneration + 1;
  state.selectionGeneration = requestedGeneration;
  state.selectedRunId = requestedRunId;
  state.snapshot = null;
  state.snapshotRunId = null;
  state.snapshotGeneration = null;
  state.eventCursor = "0";
  state.eventStream = "";
  state.recentEventNodes = new Set();
  state.selectedNodeId = null;
  renderNeutralGraph();
  let snapshot;
  try {
    snapshot = await api(`/api/snapshot?run_id=${encodeURIComponent(requestedRunId)}`);
  } catch (error) {
    if (!selectionRequestIsCurrent(requestedRunId, requestedGeneration)) return;
    throw error;
  }
  if (!commitSnapshot(snapshot, requestedRunId, requestedGeneration)) return;
  const snapshotCursor = state.snapshot.telemetry?.nextCursor;
  state.eventCursor = snapshotCursor !== undefined && snapshotCursor !== null
    ? String(snapshotCursor)
    : String(Math.max(0, ...(state.snapshot.events || []).map((event) => Number(event.cursor) || 0)));
  state.eventStream = state.snapshot.telemetry?.streamId || "";
  renderRunMenu();
  renderRunContext();
  renderMission();
  renderRunsTable();
  renderEvidence();
}

function renderRunContext() {
  const run = state.snapshot?.run || {};
  $("#selectedRunName").textContent = run.name || "Unnamed run";
  const stage = run.placeholder && run.observedStage ? ` · ${humanStatus(run.observedStage)}` : "";
  $("#selectedRunMeta").textContent = `${run.source === "fixture" ? "Deterministic fixture" : "Filesystem projection"} · ${run.requirementCount || 0} requirements${stage}`;
  const status = $("#runStatus");
  status.className = `status-chip status-${run.status || "unknown"}`;
  status.replaceChildren(element("i"), document.createTextNode(` ${displayRunStatus(run).toUpperCase()}`));
  updateFreshness();
  const dashboard = $("#openDashboard");
  if (run.dashboardUrl) {
    dashboard.href = run.dashboardUrl;
    dashboard.classList.remove("is-disabled");
    dashboard.removeAttribute("aria-disabled");
  } else {
    dashboard.removeAttribute("href");
    dashboard.classList.add("is-disabled");
    dashboard.setAttribute("aria-disabled", "true");
  }
}

function updateFreshness() {
  const observed = state.snapshot?.observedAt || state.snapshot?.run?.updatedAt;
  $("#freshness").textContent = observed ? `Observed ${relativeTime(observed)}` : "No observation";
}

function renderMission() {
  if (!state.snapshot) return;
  renderMetrics();
  renderGraph();
  renderActivity();
  renderTrace();
  renderLimitations();
}

function renderMetrics() {
  const nodes = state.snapshot.nodes || [];
  const active = nodes.filter((node) => node.active && node.id !== "planner").length;
  const waiting = nodes.filter((node) => node.status === "waiting").length;
  const complete = nodes.filter((node) => ["completed", "committed"].includes(node.status)).length;
  const capacity = state.snapshot.capacity || {};
  const events = state.snapshot.events || [];
  const latest = events.reduce(
    (current, event) => !current || Number(event.cursor || 0) > Number(current.cursor || 0) ? event : current,
    null,
  );
  // The graph projection is the authority for currently running workers.
  // Capacity leases can outlive a role attempt across pause/recovery and must
  // not be presented as live agents.
  $("#metricActive").textContent = String(active);
  $("#metricWaiting").textContent = String(waiting);
  $("#metricComplete").textContent = String(complete);
  $("#metricCapacity").textContent = capacity.total ? `${active}/${capacity.total}` : `${active}/—`;
  $("#metricActivity").textContent = latest?.summary || "No durable event";
  $("#metricActivityDetail").textContent = latest ? `${formatTime(latest.timestamp)} · ${roleLabel(latest.role)}` : "Telemetry unavailable";
}

function renderNeutralGraph() {
  if (typeof document === "undefined") return;
  const nodeLayer = $("#nodeLayer");
  const edgeLayer = $("#edgeLayer");
  if (!nodeLayer || !edgeLayer) return;
  const definitions = $("defs", edgeLayer);
  nodeLayer.replaceChildren();
  edgeLayer.replaceChildren(definitions);
  const empty = $("#graphEmpty");
  if (empty) empty.hidden = false;
  const quality = $("#graphDataQuality");
  if (quality) quality.textContent = "Projection: loading selected run";
  const stage = $("#graphStage");
  if (stage) $$(".lane-label", stage).forEach((label) => label.remove());
}

function graphPositions(nodes) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const parents = new Map(nodes.map((node) => [node.id, []]));
  (state.snapshot?.edges || []).forEach((edge) => {
    if (!nodeById.has(edge.source) || !nodeById.has(edge.target) || edge.source === edge.target) return;
    parents.get(edge.target).push(edge.source);
  });
  const depthMemo = new Map();
  const visiting = new Set();
  const depthOf = (nodeId) => {
    if (depthMemo.has(nodeId)) return depthMemo.get(nodeId);
    if (visiting.has(nodeId)) return 0; // malformed cycle: keep the node at a safe root depth
    visiting.add(nodeId);
    const depth = Math.max(0, ...(parents.get(nodeId) || []).map((parentId) => depthOf(parentId) + 1));
    visiting.delete(nodeId);
    depthMemo.set(nodeId, depth);
    return depth;
  };
  const columns = new Map();
  nodes.forEach((node) => {
    const depth = depthOf(node.id);
    if (!columns.has(depth)) columns.set(depth, []);
    columns.get(depth).push(node);
  });
  const rolePriority = {
    planner: 0,
    entity_resolution_owner: 1,
    identity_reviewer: 2,
    analytical_owner: 3,
    specialist: 4,
  };
  const identityRoles = new Set(["entity_resolution_owner", "identity_reviewer"]);
  columns.forEach((items) => items.sort((left, right) => {
    const leftIdentity = identityRoles.has(left.role);
    const rightIdentity = identityRoles.has(right.role);
    if (leftIdentity !== rightIdentity) return leftIdentity ? -1 : 1;
    if (leftIdentity && rightIdentity) {
      const subjectOrder = String(left.subjectId || left.label || left.id)
        .localeCompare(String(right.subjectId || right.label || right.id));
      if (subjectOrder) return subjectOrder;
    }
    const roleOrder = (rolePriority[left.role] ?? 99) - (rolePriority[right.role] ?? 99);
    return roleOrder || String(left.label || left.id).localeCompare(String(right.label || right.id));
  }));
  const positions = new Map();
  const maximumNodesPerColumn = 7;
  const columnGap = 286;
  const rowGap = 132;
  const marginX = 42;
  const marginY = 48;
  const visualColumns = [];
  Array.from(columns.keys()).sort((left, right) => left - right).forEach((logicalDepth) => {
    const items = columns.get(logicalDepth) || [];
    for (let offset = 0; offset < items.length; offset += maximumNodesPerColumn) {
      visualColumns.push({
        logicalDepth,
        continuation: offset > 0,
        items: items.slice(offset, offset + maximumNodesPerColumn),
      });
    }
  });
  const maximumRows = Math.max(1, ...visualColumns.map((column) => column.items.length));
  const maximumDepth = Math.max(0, visualColumns.length - 1);
  state.graphLayout = {
    width: Math.max(900, marginX * 2 + maximumDepth * columnGap + 240),
    height: Math.max(620, marginY * 2 + maximumRows * rowGap),
  };
  state.graphColumns = visualColumns.map((column, depth) => ({
    depth,
    logicalDepth: column.logicalDepth,
    continuation: column.continuation,
  }));
  visualColumns.forEach((column, depth) => {
    const { items, logicalDepth, continuation } = column;
    const contentHeight = Math.max(0, (items.length - 1) * rowGap);
    const start = logicalDepth === 0 && items.length === 1
      ? Math.max(marginY, (state.graphLayout.height - contentHeight - 90) / 2)
      : marginY;
    items.forEach((node, index) => positions.set(node.id, {
      x: marginX + depth * columnGap,
      y: start + index * rowGap,
      depth,
      logicalDepth,
      continuation,
    }));
  });
  return positions;
}

function edgePath(source, target) {
  const sx = source.x + 218;
  const sy = source.y + 45;
  const tx = target.x;
  const ty = target.y + 45;
  const bend = Math.max(50, (tx - sx) * 0.5);
  return `M ${sx} ${sy} C ${sx + bend} ${sy}, ${tx - bend} ${ty}, ${tx} ${ty}`;
}

function renderGraph() {
  const nodes = state.snapshot.nodes || [];
  const edges = state.snapshot.edges || [];
  const nodeLayer = $("#nodeLayer");
  const edgeLayer = $("#edgeLayer");
  const definitions = $("defs", edgeLayer);
  nodeLayer.replaceChildren();
  edgeLayer.replaceChildren(definitions);
  $("#graphEmpty").hidden = nodes.length > 0 || edges.length > 0;
  const positions = graphPositions(nodes);
  const latestActiveNodeIds = state.recentEventNodes;

  const depths = Array.from(new Set(Array.from(positions.values()).map((position) => position.depth))).sort((a, b) => a - b);
  depths.forEach((depth) => {
    const lane = element("span", "lane-label", `DEPTH ${depth + 1}`);
    lane.style.left = `${42 + depth * 286}px`;
    lane.style.top = `${state.graphLayout.height - 34}px`;
    nodeLayer.append(lane);
  });
  const stage = $("#graphStage");
  if (stage) {
    stage.style.width = `${state.graphLayout.width}px`;
    stage.style.height = `${state.graphLayout.height}px`;
  }

  edges.forEach((edge) => {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) return;
    const pathData = edgePath(source, target);
    const path = svgElement("path", { d: pathData, class: `edge ${edge.kind === "dependency" ? "is-dependency" : ""} ${latestActiveNodeIds.has(edge.target) ? "is-active" : ""}` });
    const hit = svgElement("path", { d: pathData, class: "edge-hit" });
    hit.addEventListener("click", () => openInspector("edge", edge));
    edgeLayer.append(path, hit);
    if (edge.label) {
      const label = svgElement("text", { x: (source.x + target.x + 218) / 2, y: (source.y + target.y) / 2 + 37, class: "edge-label", "text-anchor": "middle" });
      label.textContent = edge.label;
      edgeLayer.append(label);
    }
    if (latestActiveNodeIds.has(edge.target)) {
      const pulse = svgElement("circle", { r: 2.5, class: "edge-pulse" });
      const motion = svgElement("animateMotion", { dur: "1.8s", repeatCount: "1", path: pathData });
      pulse.append(motion);
      edgeLayer.append(pulse);
    }
  });

  nodes.forEach((node) => {
    const position = positions.get(node.id);
    if (!position) return;
    const card = element("button", `agent-node status-${node.status || "unknown"}`);
    card.type = "button";
    card.dataset.role = node.role || "agent";
    card.style.left = `${position.x}px`;
    card.style.top = `${position.y}px`;
    card.classList.toggle("is-selected", node.id === state.selectedNodeId);
    card.setAttribute("aria-label", `${node.label}, ${humanStatus(node.status)}`);
    const top = element("div", "node-top");
    const icon = element("span", "role-icon", roleGlyph(node.role));
    const title = element("span", "node-title");
    title.append(element("strong", "", node.label || roleLabel(node.role)), element("small", "", roleLabel(node.role)));
    top.append(icon, title, element("i", "node-status"));
    const fallbackObjective = [node.taskName && humanStatus(node.taskName), node.subjectId]
      .filter(Boolean)
      .join(" · ");
    const objective = element("p", "node-objective", node.objective || fallbackObjective || "No recorded objective");
    const foot = element("div", "node-foot");
    const progressValue = node.progress;
    if (typeof progressValue === "number" && Number.isFinite(progressValue)) {
      const progress = element("span", "node-progress");
      const progressFill = element("i");
      progressFill.style.width = `${Math.max(0, Math.min(100, progressValue))}%`;
      progress.append(progressFill);
      foot.append(progress);
    }
    foot.append(element("span", "", node.requirementId || node.domain || node.subjectId || humanStatus(node.status).toUpperCase()));
    card.append(top, objective, foot);
    card.addEventListener("click", () => {
      state.selectedNodeId = node.id;
      renderGraph();
      renderTrace();
      openInspector("node", node);
    });
    nodeLayer.append(card);
  });

  const isFixture = state.snapshot.run?.source === "fixture";
  $("#graphDataQuality").textContent = isFixture
    ? "Projection: deterministic UI fixture"
    : edges.length
      ? "Projection: recorded durable relationships"
      : "Projection: lineage unavailable · events remain factual";
  applyGraphTransform();
}

function applyGraphTransform() {
  const { x, y, scale } = state.graph;
  $("#graphStage").style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
}

function fitGraph() {
  const viewport = $("#graphViewport");
  if (!viewport.offsetWidth) return;
  const layout = state.graphLayout || { width: 1200, height: 700 };
  const scale = Math.min(0.9, (viewport.offsetWidth - 36) / layout.width, (viewport.offsetHeight - 28) / layout.height);
  state.graph.scale = Math.max(0.62, scale);
  state.graph.x = Math.max(8, (viewport.offsetWidth - layout.width * state.graph.scale) / 2);
  state.graph.y = Math.max(6, (viewport.offsetHeight - layout.height * state.graph.scale) / 2);
  applyGraphTransform();
}

function bindGraphControls() {
  const viewport = $("#graphViewport");
  $("#fitGraph").addEventListener("click", fitGraph);
  viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    const previous = state.graph.scale;
    const next = Math.min(1.25, Math.max(0.55, previous - event.deltaY * 0.0008));
    const rect = viewport.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const pointerY = event.clientY - rect.top;
    state.graph.x = pointerX - ((pointerX - state.graph.x) * next) / previous;
    state.graph.y = pointerY - ((pointerY - state.graph.y) * next) / previous;
    state.graph.scale = next;
    applyGraphTransform();
  }, { passive: false });
  viewport.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".agent-node")) return;
    state.graph.dragging = true;
    state.graph.startX = event.clientX - state.graph.x;
    state.graph.startY = event.clientY - state.graph.y;
    viewport.classList.add("is-dragging");
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!state.graph.dragging) return;
    state.graph.x = event.clientX - state.graph.startX;
    state.graph.y = event.clientY - state.graph.startY;
    applyGraphTransform();
  });
  const endDrag = () => { state.graph.dragging = false; viewport.classList.remove("is-dragging"); };
  viewport.addEventListener("pointerup", endDrag);
  viewport.addEventListener("pointercancel", endDrag);
}

function renderActivity() {
  const list = $("#activityList");
  list.replaceChildren();
  const events = [...(state.snapshot.events || [])]
    .filter((event) => state.eventFilter === "all" || event.category === state.eventFilter)
    .sort((a, b) => (b.cursor || 0) - (a.cursor || 0));
  if (!events.length) {
    const empty = element("div", "empty-state");
    empty.append(element("span", "", "·"), element("strong", "", "No matching durable events"));
    list.append(empty);
    return;
  }
  events.slice(0, 80).forEach((event) => {
    const item = element("article", "activity-item");
    item.dataset.category = event.category || "system";
    item.tabIndex = 0;
    const icon = element("span", "event-icon", eventGlyph(event.category));
    const copy = element("div", "event-copy");
    const meta = element("div", "event-meta");
    meta.append(element("span", "", roleLabel(event.role)), element("time", "", formatTime(event.timestamp)));
    copy.append(meta, element("strong", "", event.summary || humanStatus(event.type)));
    if (event.path || event.artifact) copy.append(element("code", "event-path", event.path || event.artifact));
    item.append(icon, copy);
    item.addEventListener("click", () => selectEvent(event));
    item.addEventListener("keydown", (keyboardEvent) => {
      if (["Enter", " "].includes(keyboardEvent.key)) selectEvent(event);
    });
    list.append(item);
  });
}

function selectEvent(event) {
  if (event.nodeId) {
    state.selectedNodeId = event.nodeId;
    renderGraph();
    renderTrace();
  }
  openInspector("event", event);
}

function bindMissionControls() {
  $$("[data-event-filter]").forEach((button) => button.addEventListener("click", () => {
    state.eventFilter = button.dataset.eventFilter;
    $$("[data-event-filter]").forEach((candidate) => candidate.classList.toggle("is-active", candidate === button));
    renderActivity();
  }));
  $("#pauseFeed").addEventListener("click", () => {
    state.feedPaused = !state.feedPaused;
    $("#pauseFeed").textContent = state.feedPaused ? "▶" : "Ⅱ";
    $("#pauseFeed").setAttribute("aria-label", state.feedPaused ? "Resume feed" : "Pause feed");
    $("#feedState").textContent = state.feedPaused ? "Feed paused in browser" : "Polling durable telemetry";
  });
  const toggleTrace = () => {
    const panel = $("#tracePanel");
    panel.hidden = !panel.hidden;
    if (!panel.hidden) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $("#toggleTrace").addEventListener("click", toggleTrace);
  $("#showTraceFromSegment").addEventListener("click", toggleTrace);
}

async function pollEvents() {
  if (!state.selectedRunId || state.feedPaused) return;
  const requestedRunId = state.selectedRunId;
  const requestedGeneration = state.selectionGeneration;
  try {
    const received = [];
    let payload = null;
    for (let page = 0; page < 6; page += 1) {
      if (!snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration)) return;
      const stream = encodeURIComponent(state.eventStream || "");
      payload = await api(`/api/events?run_id=${encodeURIComponent(requestedRunId)}&after=${state.eventCursor}&stream=${stream}`);
      if (!snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration)) return;
      state.eventStream = payload.streamId || "";
      state.eventCursor = String(payload.nextCursor ?? "0");
      const known = new Set((state.snapshot.events || []).map((event) => event.id));
      (payload.events || []).forEach((event) => {
        if (!known.has(event.id)) {
          state.snapshot.events.push(event);
          known.add(event.id);
          received.push(event);
        }
      });
      if (!payload.hasMore) break;
    }
    if (!snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration)) return;
    if (received.length) {
      state.recentEventNodes = new Set(received.map((event) => event.nodeId).filter(Boolean));
      state.snapshot.observedAt = payload.observedAt;
      renderMetrics();
      renderActivity();
      renderGraph();
      window.setTimeout(() => {
        if (!snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration)) return;
        state.recentEventNodes = new Set();
        if (state.snapshot) renderGraph();
      }, 2100);
    } else {
      state.snapshot.observedAt = payload.observedAt;
    }
    $("#connectionDot").classList.add("is-online");
    $("#feedState").textContent = "Durable telemetry current";
  } catch (error) {
    if (!snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration)) return;
    $("#connectionDot").classList.remove("is-online");
    $("#feedState").textContent = "Projection temporarily unavailable";
  }
}

function renderTrace() {
  const body = $("#traceBody");
  body.replaceChildren();
  const trace = state.snapshot.trace || [];
  if (!trace.length) {
    const message = element("div", "empty-state");
    message.append(element("strong", "", "No recorded invocation spans"), element("p", "", "Technical events remain visible in Live Activity."));
    body.append(message);
    return;
  }
  const maximum = Math.max(...trace.map((span) => Number(span.startMs || 0) + Number(span.durationMs || 0)), 1);
  trace.forEach((span) => {
    const row = element("div", "trace-row");
    row.classList.toggle("is-selected", span.nodeId === state.selectedNodeId);
    row.classList.toggle("is-dimmed", Boolean(state.selectedNodeId) && span.nodeId !== state.selectedNodeId);
    const label = element("div", "trace-label");
    label.style.paddingLeft = `${Number(span.depth || 0) * 14}px`;
    label.append(element("span", "", `${String(Number(span.depth || 0) + 1).padStart(2, "0")}`), document.createTextNode(span.label || "Operation"));
    const track = element("div", "trace-track");
    const bar = element("button", `trace-bar status-${span.status || "unknown"}`, span.role || span.label);
    bar.type = "button";
    bar.style.left = `${(Number(span.startMs || 0) / maximum) * 100}%`;
    bar.style.width = `${Math.max(0.7, (Number(span.durationMs || 0) / maximum) * 100)}%`;
    bar.addEventListener("click", () => {
      state.selectedNodeId = span.nodeId || null;
      renderGraph();
      renderTrace();
      openInspector("trace", span);
    });
    track.append(bar);
    row.append(label, track);
    body.append(row);
  });
}

function renderLimitations() {
  const container = $("#limitations");
  const limitations = state.snapshot.limitations || [];
  container.hidden = !limitations.length;
  container.replaceChildren(...limitations.map((text) => element("span", "", `△ ${text}`)));
}

function renderRunsTable() {
  const body = $("#runsTableBody");
  if (!body) return;
  const search = ($("#runSearch")?.value || "").trim().toLowerCase();
  const runs = state.runs.filter((run) => !search || `${run.name} ${run.status} ${run.id}`.toLowerCase().includes(search));
  body.replaceChildren();
  runs.forEach((run) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const runCell = element("div", "run-cell");
    runCell.append(element("span", "run-glyph", run.source === "fixture" ? "FX" : "AF"));
    const copy = element("span");
    copy.append(element("strong", "", run.name), element("small", "", run.id));
    runCell.append(copy);
    nameCell.append(runCell);
    const statusCell = document.createElement("td");
    const status = element("span", `table-status ${run.status || "unknown"}`);
    status.append(element("i"), document.createTextNode(displayRunStatus(run)));
    statusCell.append(status);
    const requirements = element("td", "", String(run.requirementCount || 0));
    const updated = element("td", "", relativeTime(run.updatedAt));
    const mode = element("td", "", run.placeholder ? "Semantic intake" : (run.source === "fixture" ? "Fixture" : "Read-only"));
    const actionCell = document.createElement("td");
    const action = element("button", "table-button", run.id === state.selectedRunId ? "Viewing" : "Open");
    action.type = "button";
    action.disabled = run.id === state.selectedRunId;
    action.addEventListener("click", async () => { await selectRun(run.id); window.location.hash = "mission"; });
    actionCell.append(action);
    row.append(nameCell, statusCell, requirements, updated, mode, actionCell);
    body.append(row);
  });
  $("#runSummary").textContent = `${runs.length} of ${state.runs.length} runs`;
}

function renderEvidence() {
  const grid = $("#evidenceGrid");
  if (!grid || !state.snapshot) return;
  const events = (state.snapshot.events || []).filter((event) => event.path || event.artifact);
  grid.replaceChildren();
  if (!events.length) {
    const message = element("div", "fatal-message", "No safe artifact or source paths are recorded in this projection.");
    grid.append(message);
    return;
  }
  events.slice().reverse().forEach((event) => {
    const card = element("article", "evidence-card panel");
    card.tabIndex = 0;
    const header = document.createElement("header");
    header.append(element("span", "event-icon", eventGlyph(event.category)), element("span", "section-kicker", event.category.toUpperCase()));
    const path = event.artifact || event.path;
    card.append(header, element("h2", "", path.split("/").pop()), element("p", "", path));
    const footer = document.createElement("footer");
    footer.append(element("span", "", roleLabel(event.role)), element("time", "", formatTime(event.timestamp)));
    card.append(footer);
    card.addEventListener("click", () => selectEvent(event));
    grid.append(card);
  });
}

function openInspector(kind, value) {
  const inspector = $("#inspector");
  const body = $("#inspectorBody");
  body.replaceChildren();
  $("#inspectorTitle").textContent = { node: "Agent details", event: "Event details", edge: "Dependency details", trace: "Trace span" }[kind] || "Details";
  const hero = element("section", "inspector-hero");
  hero.append(element("span", "role-icon", kind === "node" ? roleGlyph(value.role) : eventGlyph(value.category)));
  hero.append(element("h3", "", value.label || value.summary || value.type || "Recorded detail"));
  hero.append(element("p", "", value.objective || value.path || value.artifact || value.role || "Durable projection"));
  hero.append(element("span", "inspector-status", humanStatus(value.status || value.kind || kind)));
  body.append(hero);
  const list = element("dl", "detail-list");
  const detailKeys = ["id", "role", "requirementId", "itemId", "domain", "source", "target", "kind", "timestamp", "path", "artifact", "rows", "progress", "startMs", "durationMs"];
  detailKeys.forEach((key) => {
    if (value[key] === undefined || value[key] === null || value[key] === "") return;
    const row = element("div", "detail-row");
    row.append(element("dt", "", humanStatus(key)), element("dd", "", Array.isArray(value[key]) ? value[key].join(", ") : String(value[key])));
    list.append(row);
  });
  body.append(list);
  if (value.details) {
    body.append(element("span", "section-kicker", "RECORDED ATTRIBUTES"));
    const pre = element("pre", "json-block", JSON.stringify(value.details, null, 2));
    body.append(pre);
  }
  inspector.classList.add("is-open");
  inspector.setAttribute("aria-hidden", "false");
}

function bindInspector() {
  $("#closeInspector").addEventListener("click", closeInspector);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeInspector(); });
}

function closeInspector() {
  $("#inspector").classList.remove("is-open");
  $("#inspector").setAttribute("aria-hidden", "true");
  state.selectedNodeId = null;
  if (state.snapshot) {
    renderGraph();
    renderTrace();
  }
}

function populateExistingRuns() {
  const select = $("#existingRun");
  select.replaceChildren();
  state.runs.forEach((run) => {
    const option = document.createElement("option");
    option.value = run.id;
    option.textContent = `${run.name} · ${displayRunStatus(run)}`;
    select.append(option);
  });
}

function bindLaunchForm() {
  $$("input[name='mode']").forEach((input) => input.addEventListener("change", () => {
    const continuing = input.checked && input.value === "continue";
    $("#projectNameField").hidden = continuing;
    $("#existingRunField").hidden = !continuing;
    updateCapacityForMode();
    updatePreflight();
  }));
  $("#addRequirement").addEventListener("click", addRequirementField);
  $("#requirementFields").addEventListener("input", updatePreflight);
  $("#projectName").addEventListener("input", updatePreflight);
  $("#existingRun").addEventListener("change", () => { updateCapacityForMode(); updatePreflight(); });
  $("#sourcePath").addEventListener("input", updatePreflight);
  $("#maxAgents").addEventListener("input", () => { renderCapacityBreakdown(currentCapacity()); updatePreflight(); });
  const dropZone = $("#dropZone");
  $("#fileInput").addEventListener("change", (event) => addFiles(event.target.files));
  $("#folderInput").addEventListener("change", (event) => addFiles(event.target.files));
  ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("is-dragging"); }));
  dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  $("#launchForm").addEventListener("submit", validateDraft);
  updateCapacityForMode();
  updatePreflight();
}

function capacityForTotal(total) {
  return {
    total,
    entityResolution: Math.min(4, total),
    analyticalOwner: Math.min(1, total),
    specialist: Math.min(3, total),
  };
}

function authoritativeCapacityForSelectedRun() {
  const run = state.runs.find((candidate) => candidate.id === $("#existingRun").value);
  const capacity = run?.capacity;
  if (!capacity || !Number.isInteger(capacity.total)) return null;
  const keys = ["total", "entityResolution", "analyticalOwner", "specialist"];
  return keys.every((key) => Number.isInteger(capacity[key]))
    ? Object.fromEntries(keys.map((key) => [key, capacity[key]]))
    : null;
}

function currentCapacity() {
  const continuing = $("input[name='mode']:checked")?.value === "continue";
  return continuing ? authoritativeCapacityForSelectedRun() : capacityForTotal(Number($("#maxAgents").value));
}

function renderCapacityBreakdown(capacity) {
  const slider = $("#maxAgents");
  const values = $$(".capacity-breakdown strong");
  $("#capacityValue").textContent = capacity ? String(capacity.total) : "—";
  values[0].textContent = capacity ? String(capacity.entityResolution) : "—";
  values[1].textContent = capacity ? String(capacity.analyticalOwner) : "—";
  values[2].textContent = capacity ? String(capacity.specialist) : "—";
  slider.setAttribute("aria-valuetext", capacity ? `${capacity.total} total active workers` : "Authoritative capacity unavailable");
}

function updateCapacityForMode() {
  const slider = $("#maxAgents");
  const continuing = $("input[name='mode']:checked")?.value === "continue";
  const authoritative = continuing ? authoritativeCapacityForSelectedRun() : null;
  slider.disabled = continuing;
  if (authoritative) slider.value = String(authoritative.total);
  renderCapacityBreakdown(continuing ? authoritative : capacityForTotal(Number(slider.value)));
}

function addRequirementField() {
  const count = $$(".requirement-field").length + 1;
  const field = element("label", "field requirement-field");
  field.append(element("span", "", `Input block ${count}`));
  const textarea = document.createElement("textarea");
  textarea.name = "intake-block";
  textarea.rows = 4;
  textarea.placeholder = "Add more context, questions, notes, or pasted document text";
  const remove = element("button", "remove-requirement", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", `Remove input block ${count}`);
  remove.addEventListener("click", () => { field.remove(); renumberRequirements(); updatePreflight(); });
  field.append(textarea, element("small", "field-error"), remove);
  $("#requirementFields").append(field);
  textarea.focus();
}

function renumberRequirements() {
  $$(".requirement-field").forEach((field, index) => { $("span", field).textContent = `Input block ${index + 1}`; });
}

function addFiles(fileList) {
  const allowed = new Set(["csv", "tsv", "json", "jsonl", "ndjson", "xlsx", "parquet", "zip", "txt", "text", "md", "markdown", "rst", "pdf", "docx", "odt"]);
  Array.from(fileList || []).forEach((file) => {
    const extension = file.name.split(".").pop().toLowerCase();
    const descriptor = { name: file.name, size: file.size, type: file.type || extension, valid: allowed.has(extension) };
    if (!state.files.some((current) => current.name === descriptor.name && current.size === descriptor.size)) state.files.push(descriptor);
  });
  renderFileManifest();
  updatePreflight();
}

function renderFileManifest() {
  const manifest = $("#fileManifest");
  manifest.replaceChildren();
  state.files.forEach((file, index) => {
    const row = element("div", "file-row");
    row.append(element("span", "file-type", file.name.split(".").pop()));
    const copy = element("span");
    copy.append(element("strong", "", file.name), element("small", "", `${formatBytes(file.size)} · ${file.valid ? "ready" : "unsupported format"}`));
    const remove = element("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.addEventListener("click", () => { state.files.splice(index, 1); renderFileManifest(); updatePreflight(); });
    row.append(copy, remove);
    manifest.append(row);
  });
}

function launchPayload() {
  const mode = $("input[name='mode']:checked").value;
  const intakeBlocks = $$("textarea[name='intake-block']").map((input) => input.value).filter((value) => value.trim());
  const sources = state.files.map(({ name, size, type, valid }) => ({ name, size, type, valid }));
  const sourcePath = $("#sourcePath").value.trim();
  if (sourcePath) sources.push({ path: sourcePath, type: "local_path" });
  return {
    mode,
    projectName: $("#projectName").value.trim(),
    runId: $("#existingRun").value,
    intakeBlocks,
    sources,
    sourceUrl: $("#sourceUrl").value.trim(),
    maxAgents: Number($("#maxAgents").value),
    capacity: currentCapacity(),
  };
}

function updatePreflight() {
  const payload = launchPayload();
  const values = $$("#preflightSummary dd");
  values[0].textContent = payload.mode === "new" ? (payload.projectName || "New run") : ($("#existingRun option:checked")?.textContent || "Existing run");
  values[1].textContent = String(payload.intakeBlocks.length);
  values[2].textContent = String(payload.sources.length + (payload.sourceUrl ? 1 : 0));
  values[3].textContent = payload.capacity ? `${payload.capacity.total} workers` : "Authoritative capacity unavailable";
}

function clearFieldErrors() {
  $$(".field").forEach((field) => { field.classList.remove("has-error"); const output = $(".field-error", field); if (output) output.textContent = ""; });
}

function showFieldError(selector, message) {
  const field = $(selector);
  if (!field) return;
  field.classList.add("has-error");
  const output = $(".field-error", field);
  if (output) output.textContent = message;
}

async function validateDraft(event) {
  event.preventDefault();
  clearFieldErrors();
  const output = $("#validationResult");
  const button = $("#validateDraft");
  button.disabled = true;
  button.textContent = "Validating…";
  try {
    const result = await api("/api/launch/validate", { method: "POST", body: JSON.stringify(launchPayload()) });
    output.hidden = false;
    output.className = `validation-result ${result.valid ? "is-valid" : "is-invalid"}`;
    output.textContent = result.message;
    if (result.errors.projectName) showFieldError("#projectNameField", result.errors.projectName);
    if (result.errors.runId) showFieldError("#existingRunField", result.errors.runId);
    if (result.errors.intakeBlocks) showFieldError(".requirement-field", result.errors.intakeBlocks);
    if (result.errors.sourceUrl) showFieldError("#sourceUrlField", result.errors.sourceUrl);
  } catch (error) {
    output.hidden = false;
    output.className = "validation-result is-invalid";
    output.textContent = error.message || String(error);
  } finally {
    button.disabled = false;
    button.textContent = "Validate draft";
  }
}

function showFatal(message) {
  $("#connectionDot").classList.remove("is-online");
  const visible = $(".view.is-visible") || $("#missionView");
  visible.replaceChildren(element("div", "fatal-message", `Control Center could not initialize: ${message}`));
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", bootstrap);
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    state,
    graphPositions,
    selectRun,
    pollEvents,
    selectionRequestIsCurrent,
    snapshotOwnershipIsCurrent,
    commitSnapshot,
  };
}
