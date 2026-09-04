"use strict";

const POLL_INTERVAL_MS = 2500;
const LANE_HEIGHT = 146;
const NODE_WIDTH = 214;
const NODE_HEIGHT = 112;
const NODE_GAP = 22;
const NODE_START_X = 205;
const SVG_NS = "http://www.w3.org/2000/svg";

const ROLE_NAMES = {
  planner: "Mission Planner",
  analytical_owner: "Analytical Owner",
  business_reviewer: "Business Reviewer",
  integration_agent: "Integration Agent",
  integration_fidelity_reviewer: "Integration Fidelity Reviewer",
  entity_resolution_owner: "Identity Resolver",
  identity_reviewer: "Identity Reviewer",
  identity_domain: "Identity Domain",
  identity_owner: "Identity Owner",
  reviewer: "Reviewer",
  subject: "Requirement Record",
};

const ROLE_ICONS = {
  planner: "PL",
  analytical_owner: "AO",
  business_reviewer: "BR",
  integration_agent: "IN",
  integration_fidelity_reviewer: "FR",
  entity_resolution_owner: "ID",
  identity_reviewer: "RV",
  identity_domain: "DM",
  identity_owner: "IO",
  reviewer: "RV",
  subject: "WK",
};

const ACTION_NAMES = {
  "analyze requirement": "Analysis",
  "repair requirement": "Repair",
  "resume requirement analysis": "Resumed analysis",
  "review requirement": "Business review",
  "finalize requirement review": "Review finalization",
  "integrate requirement": "Integration",
  "review integration fidelity": "Integration review",
  "repair integration fidelity": "Integration repair",
  "commit integration requirement": "Integration commit",
  "resolve identity": "Identity resolution",
  "repair identity result": "Identity repair",
  "review identity result": "Identity review",
  "commit identity result": "Identity commit",
};

const state = {
  config: null,
  snapshot: null,
  nodes: [],
  edges: [],
  requirements: [],
  lanes: [],
  layout: new Map(),
  nodeById: new Map(),
  firstSeen: new Map(),
  nextSeen: 1,
  nodeElements: new Map(),
  edgeElements: new Map(),
  selectedId: null,
  currentLane: null,
  focus: "all",
  eventFilter: "all",
  search: "",
  feedPaused: false,
  followActive: true,
  initializedView: false,
  activeSignature: "",
  stageWidth: 1800,
  stageHeight: 1200,
  view: { x: 0, y: 0, scale: 0.82 },
  drag: null,
  observedDate: null,
  failures: 0,
};

const dom = Object.fromEntries(
  [
    "requirementNav", "runTitle", "runMeta", "runStatus", "observedAt",
    "metricActive", "metricWaiting", "metricNodes", "metricComplete",
    "metricCapacity", "metricActivity", "metricActivityDetail", "searchInput",
    "followActive", "zoomOut", "zoomIn", "fitMap", "showActive", "viewport",
    "stage", "edgeLayer", "laneLayer", "nodeLayer", "nodeSummary", "offscreen",
    "offscreenText", "offscreenShow", "minimap", "activityList", "pauseFeed",
    "feedState", "inspector", "inspectorTitle", "inspectorBody", "closeInspector",
    "fatal", "fatalMessage", "viewContext", "viewContextTitle", "viewContextDetail",
  ].map((id) => [id, document.getElementById(id)]),
);

function text(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function compact(value, limit = 20) {
  const raw = text(value);
  if (raw.length <= limit) return raw;
  return `${raw.slice(0, Math.max(6, limit - 7))}…${raw.slice(-6)}`;
}

function titleCase(value) {
  return text(value)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isHash(value) {
  return /^[a-f0-9]{24,}$/i.test(text(value).replace(/^.*:/, ""));
}

function isTechnicalLabel(value) {
  const raw = text(value);
  return isHash(raw) || /^[a-z][a-z0-9_]+:[^ ]+$/i.test(raw);
}

function readableLabel(value, role) {
  const raw = text(value).trim();
  if (!raw) return roleName(role);
  if (isHash(raw)) return `${roleName(role)} session`;
  if (raw.includes(":")) {
    const [prefix, ...rest] = raw.split(":");
    if (Object.prototype.hasOwnProperty.call(ROLE_NAMES, prefix.toLowerCase()) && rest.length) {
      return `${titleCase(rest.join(":"))} · ${roleName(role)}`;
    }
  }
  if (/^[a-z0-9_-]+$/.test(raw) && /[-_]/.test(raw)) return titleCase(raw);
  return raw;
}

function requirementId(...values) {
  for (const value of values) {
    const match = text(value).match(/\bREQ[\s_.:-]*0*(\d{1,3})\b/i);
    if (match) return `REQ-${String(Number(match[1])).padStart(3, "0")}`;
  }
  return null;
}

function nodeRequirement(node) {
  return requirementId(
    node.subjectId,
    node.objective,
    node.label,
    node.logicalOwner,
    node.taskName,
    node.canonicalIdentity,
    node.domainId,
    node.ownerRef,
    node.reviewerRef,
  );
}

function normalizedStatus(status) {
  const value = text(status).toLowerCase();
  if (["complete", "completed", "accepted", "success", "succeeded"].includes(value)) return "completed";
  if (["active", "running"].includes(value)) return "active";
  if (["dispatching", "starting", "started"].includes(value)) return "dispatching";
  if (["review", "reviewing", "in_review"].includes(value)) return "review";
  if (["waiting", "blocked", "pending"].includes(value)) return "waiting";
  if (["failed", "failure", "error", "rejected"].includes(value)) return "failed";
  if (["historical", "history"].includes(value)) return "historical";
  return value || "historical";
}

function isLive(node) {
  return ["active", "dispatching"].includes(normalizedStatus(node.status));
}

function roleName(role) {
  return ROLE_NAMES[text(role).toLowerCase()] || titleCase(role || "Agent");
}

function objectiveAction(objective) {
  const action = text(objective).split(" · ")[0].trim();
  return ACTION_NAMES[action.toLowerCase()] || action || "Work item";
}

function cardTitle(node, req) {
  if (node.role === "planner") return "Mission Planner";
  const label = text(node.label).trim();
  const action = objectiveAction(node.objective);
  if (req && requirementId(label) === req) return `${req} · ${action}`;
  if (!label || isTechnicalLabel(label)) return readableLabel(label, node.role);
  return readableLabel(label, node.role);
}

function cardTechnicalRef(node) {
  const candidates = [node.logicalOwner, node.taskName, node.subjectId, node.domainId, node.canonicalIdentity];
  const useful = candidates.find((candidate) => candidate && !requirementId(candidate));
  if (useful) return compact(useful, 25);
  if (isHash(node.label)) return compact(node.label, 22);
  if (node.id !== "planner") return compact(text(node.id).replace(/^invocation:/, "inv:"), 22);
  return "mission control";
}

function statusLabel(status) {
  return {
    active: "ACTIVE",
    dispatching: "STARTING",
    completed: "COMPLETE",
    waiting: "WAITING",
    review: "REVIEW",
    failed: "FAILED",
    historical: "HISTORICAL",
  }[normalizedStatus(status)] || text(status).toUpperCase();
}

function dateValue(value) {
  const parsed = Date.parse(text(value));
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}

function formatTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(parsed);
}

function formatDateTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(parsed);
}

function relativeTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - parsed.getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

function make(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}

function svgElement(tag, attributes = {}) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, text(value));
  return element;
}

function snapshotRequirements(snapshot) {
  const context = snapshot.missionContext || {};
  const supplied = Array.isArray(context.itemIds) && context.itemIds.length
    ? context.itemIds
    : Array.isArray(context.requirementIds) ? context.requirementIds : [];
  const normalized = [...new Set(supplied.map((item) => requirementId(item)).filter(Boolean))];
  if (normalized.length) return normalized.sort();
  const count = Number(snapshot.run && snapshot.run.requirementCount) || 0;
  return Array.from({ length: count }, (_, index) => `REQ-${String(index + 1).padStart(3, "0")}`);
}

function assignLanes(nodes, edges, requirements) {
  const result = new Map();
  const inferred = new Set();
  const valid = new Set(requirements);
  for (const node of nodes) {
    if (node.role === "planner" || node.id === "planner") {
      result.set(node.id, "GLOBAL");
      continue;
    }
    const req = nodeRequirement(node);
    if (req && valid.has(req)) result.set(node.id, req);
  }

  const neighbors = new Map();
  const addNeighbor = (left, right) => {
    if (!neighbors.has(left)) neighbors.set(left, new Set());
    neighbors.get(left).add(right);
  };
  for (const edge of edges) {
    addNeighbor(edge.source, edge.target);
    addNeighbor(edge.target, edge.source);
  }

  for (let pass = 0; pass < 3; pass += 1) {
    let changed = false;
    for (const node of nodes) {
      if (result.has(node.id)) continue;
      const linked = new Set();
      for (const neighbor of neighbors.get(node.id) || []) {
        const lane = result.get(neighbor);
        if (valid.has(lane)) linked.add(lane);
      }
      if (linked.size === 1) {
        result.set(node.id, [...linked][0]);
        inferred.add(node.id);
        changed = true;
      }
    }
    if (!changed) break;
  }

  for (const node of nodes) if (!result.has(node.id)) result.set(node.id, "SHARED");
  return { result, inferred };
}

function prepareSnapshot(snapshot) {
  const nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes.filter((node) => node && node.id && node.visible !== false) : [];
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = Array.isArray(snapshot.edges)
    ? snapshot.edges.filter((edge) => edge && nodeIds.has(edge.source) && nodeIds.has(edge.target))
    : [];
  const requirements = snapshotRequirements(snapshot);
  const assignments = assignLanes(nodes, edges, requirements);
  for (const node of nodes) {
    if (!state.firstSeen.has(node.id)) state.firstSeen.set(node.id, state.nextSeen++);
    node._lane = assignments.result.get(node.id);
    node._laneInferred = assignments.inferred.has(node.id);
  }
  state.nodes = nodes;
  state.edges = edges;
  state.requirements = requirements;
  state.nodeById = new Map(nodes.map((node) => [node.id, node]));
  state.lanes = [
    { id: "GLOBAL", title: "MISSION CONTROL", subtitle: "Planner and global coordination" },
    ...requirements.map((id) => ({ id, title: id, subtitle: "Requirement execution chain" })),
    { id: "SHARED", title: "SHARED SERVICES", subtitle: "Cross-requirement identities and sessions" },
  ];
}

function nodeSort(left, right) {
  const leftTime = Math.min(dateValue(left.startedAt), dateValue(left.completedAt));
  const rightTime = Math.min(dateValue(right.startedAt), dateValue(right.completedAt));
  if (leftTime !== rightTime) return leftTime - rightTime;
  return (state.firstSeen.get(left.id) || 0) - (state.firstSeen.get(right.id) || 0);
}

function buildLayout() {
  const laneNodes = new Map(state.lanes.map((lane) => [lane.id, []]));
  for (const node of state.nodes) {
    const lane = laneNodes.has(node._lane) ? node._lane : "SHARED";
    laneNodes.get(lane).push(node);
  }
  for (const nodes of laneNodes.values()) nodes.sort(nodeSort);
  const maxCount = Math.max(4, ...[...laneNodes.values()].map((nodes) => nodes.length));
  state.stageWidth = NODE_START_X + maxCount * (NODE_WIDTH + NODE_GAP) + 70;
  state.stageHeight = state.lanes.length * LANE_HEIGHT;
  state.layout.clear();
  state.lanes.forEach((lane, laneIndex) => {
    laneNodes.get(lane.id).forEach((node, nodeIndex) => {
      state.layout.set(node.id, {
        x: NODE_START_X + nodeIndex * (NODE_WIDTH + NODE_GAP),
        y: laneIndex * LANE_HEIGHT + 17,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        lane: lane.id,
        laneIndex,
        nodeIndex,
      });
    });
  });
  return laneNodes;
}

function laneState(nodes) {
  const statuses = nodes.map((node) => normalizedStatus(node.status));
  if (statuses.some((status) => ["active", "dispatching"].includes(status))) return "active";
  if (statuses.some((status) => status === "failed")) return "failed";
  if (statuses.some((status) => ["waiting", "review"].includes(status))) return "waiting";
  if (nodes.length && statuses.every((status) => ["completed", "historical"].includes(status))) return "complete";
  return "recorded";
}

function renderRequirementNav(laneNodes) {
  dom.requirementNav.replaceChildren();
  dom.requirementNav.append(make("span", "", "REQUIREMENTS"));
  for (const req of state.requirements) {
    const nodes = laneNodes.get(req) || [];
    const button = make("button", `req-nav-item${state.currentLane === req ? " is-current" : ""}`);
    button.type = "button";
    const indicator = make("i", laneState(nodes));
    const label = make("b", "", req);
    const count = make("small", "", nodes.length ? `${nodes.length} nodes` : "not started");
    button.append(indicator, label, count);
    button.addEventListener("click", () => focusLane(req));
    dom.requirementNav.append(button);
  }
}

function renderLanes(laneNodes) {
  dom.laneLayer.replaceChildren();
  dom.laneLayer.style.width = `${state.stageWidth}px`;
  dom.laneLayer.style.height = `${state.stageHeight}px`;
  state.lanes.forEach((lane, index) => {
    const nodes = laneNodes.get(lane.id) || [];
    const row = make("div", `lane ${nodes.some(isLive) ? "is-active" : ""} ${nodes.length ? "" : "is-empty"}`.trim());
    row.dataset.lane = lane.id;
    row.style.top = `${index * LANE_HEIGHT}px`;
    row.style.width = `${state.stageWidth}px`;
    row.style.height = `${LANE_HEIGHT}px`;
    const label = make("div", "lane-label");
    label.append(make("b", "", lane.title), make("small", "", `${lane.subtitle} · ${nodes.length} node${nodes.length === 1 ? "" : "s"}`));
    row.append(label);
    if (!nodes.length) row.append(make("div", "empty-lane", "No durable agent activity recorded yet"));
    dom.laneLayer.append(row);
  });
}

function renderNodes() {
  dom.nodeLayer.replaceChildren();
  dom.nodeLayer.style.width = `${state.stageWidth}px`;
  dom.nodeLayer.style.height = `${state.stageHeight}px`;
  state.nodeElements.clear();
  for (const node of state.nodes) {
    const position = state.layout.get(node.id);
    if (!position) continue;
    const req = node._lane && node._lane.startsWith("REQ-") ? node._lane : nodeRequirement(node);
    const status = normalizedStatus(node.status);
    const card = make("button", `node-card status-${status}${state.selectedId === node.id ? " is-selected" : ""}`);
    card.type = "button";
    card.dataset.nodeId = node.id;
    card.style.left = `${position.x}px`;
    card.style.top = `${position.y}px`;
    card.title = `${cardTitle(node, req)} — ${roleName(node.role)} — ${statusLabel(status)}`;

    const head = make("div", "node-head");
    const icon = make("span", `role-icon ${["analytical_owner", "business_reviewer", "reviewer"].includes(node.role) ? "purple" : ""}`, ROLE_ICONS[node.role] || "AG");
    const title = make("span", "node-title");
    title.append(make("b", "", cardTitle(node, req)), make("small", "", roleName(node.role)));
    head.append(icon, title, make("i", "status-dot"));
    const objective = make("p", "node-objective", text(node.objective) || (node.role === "subject" ? "Durable requirement subject record" : "Recorded agent session"));
    const foot = make("div", "node-foot");
    foot.append(make("b", "", statusLabel(status)), make("span", "", cardTechnicalRef(node)));
    card.append(head, objective, foot);
    card.addEventListener("click", (event) => {
      event.stopPropagation();
      selectNode(node.id);
    });
    dom.nodeLayer.append(card);
    state.nodeElements.set(node.id, card);
  }
}

function edgePath(source, target) {
  const sx = source.x + source.width;
  const sy = source.y + source.height / 2;
  const tx = target.x;
  const ty = target.y + target.height / 2;
  if (tx >= sx) {
    const bend = Math.max(45, (tx - sx) * 0.48);
    return `M ${sx} ${sy} C ${sx + bend} ${sy}, ${tx - bend} ${ty}, ${tx} ${ty}`;
  }
  const bend = Math.max(55, Math.abs(tx - sx) * 0.22);
  const detour = Math.max(source.x, target.x) + source.width + bend;
  return `M ${sx} ${sy} C ${detour} ${sy}, ${detour} ${ty}, ${tx} ${ty}`;
}

function renderEdges() {
  dom.edgeLayer.replaceChildren();
  dom.edgeLayer.setAttribute("width", state.stageWidth);
  dom.edgeLayer.setAttribute("height", state.stageHeight);
  dom.edgeLayer.setAttribute("viewBox", `0 0 ${state.stageWidth} ${state.stageHeight}`);
  state.edgeElements.clear();
  for (const edge of state.edges) {
    const source = state.layout.get(edge.source);
    const target = state.layout.get(edge.target);
    if (!source || !target) continue;
    const path = svgElement("path", { class: "edge", d: edgePath(source, target) });
    path.dataset.edgeId = edge.id;
    dom.edgeLayer.append(path);
    state.edgeElements.set(edge.id, path);
  }
}

function applyFilters() {
  const query = state.search.trim().toLowerCase();
  const selected = state.selectedId;
  const connected = new Set(selected ? [selected] : []);
  if (selected) {
    for (const edge of state.edges) {
      if (edge.source === selected) connected.add(edge.target);
      if (edge.target === selected) connected.add(edge.source);
    }
  }
  for (const node of state.nodes) {
    const element = state.nodeElements.get(node.id);
    if (!element) continue;
    const status = normalizedStatus(node.status);
    const matchesFocus = state.focus === "all"
      || (state.focus === "active" && ["active", "dispatching"].includes(status))
      || (state.focus === "waiting" && ["waiting", "review"].includes(status))
      || (state.focus === "issues" && ["failed", "error"].includes(status));
    const haystack = [node.label, node.role, node.objective, node.logicalOwner, node.subjectId, node._lane].map(text).join(" ").toLowerCase();
    const matchesSearch = !query || haystack.includes(query);
    const matchesTrace = !selected || connected.has(node.id);
    element.classList.toggle("is-dim", !matchesFocus || !matchesSearch || !matchesTrace);
    element.classList.toggle("is-connected", Boolean(selected && connected.has(node.id)));
    element.classList.toggle("is-selected", selected === node.id);
  }
  for (const edge of state.edges) {
    const element = state.edgeElements.get(edge.id);
    if (!element) continue;
    const incident = selected && (edge.source === selected || edge.target === selected);
    element.classList.toggle("is-selected", Boolean(incident));
    element.classList.toggle("is-dim", Boolean(selected && !incident));
  }
}

function renderSelectedEdgeLabels() {
  for (const existing of dom.edgeLayer.querySelectorAll(".edge-label")) existing.remove();
  if (!state.selectedId) return;
  for (const edge of state.edges) {
    if (edge.source !== state.selectedId && edge.target !== state.selectedId) continue;
    const source = state.layout.get(edge.source);
    const target = state.layout.get(edge.target);
    if (!source || !target) continue;
    const label = svgElement("text", {
      class: "edge-label",
      x: (source.x + source.width + target.x) / 2,
      y: (source.y + target.y) / 2 + NODE_HEIGHT / 2 - 5,
    });
    label.textContent = text(edge.label || edge.kind || "link");
    dom.edgeLayer.append(label);
  }
}

function updateMetrics(snapshot) {
  const capacity = snapshot.capacity || {};
  const liveNodes = state.nodes.filter((node) => node.role !== "planner" && isLive(node));
  const waiting = state.nodes.filter((node) => ["waiting", "review"].includes(normalizedStatus(node.status))).length;
  const completed = state.nodes.filter((node) => normalizedStatus(node.status) === "completed").length;
  const active = Number.isFinite(Number(capacity.active)) ? Number(capacity.active) : liveNodes.length;
  const total = Number(capacity.total) || 8;
  dom.metricActive.textContent = active;
  dom.metricWaiting.textContent = waiting;
  dom.metricNodes.textContent = state.nodes.length;
  dom.metricComplete.textContent = completed;
  dom.metricCapacity.textContent = `${active}/${total}`;
  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  const latest = [...events].sort((a, b) => dateValue(b.timestamp) - dateValue(a.timestamp))[0];
  dom.metricActivity.textContent = latest ? text(latest.summary || titleCase(latest.type)) : "No durable event recorded";
  dom.metricActivityDetail.textContent = latest ? `${formatTime(latest.timestamp)} · ${roleName(latest.role || "agent")}` : "Waiting for durable projection";
  dom.nodeSummary.textContent = `${state.nodes.length} nodes · ${state.edges.length} links · ${state.requirements.length}/${state.requirements.length} requirements visible`;
}

function eventKind(event) {
  const content = [event.type, event.category, event.role, event.status, event.summary].map(text).join(" ").toLowerCase();
  if (/error|fail|reject|blocked/.test(content)) return "error";
  if (/review/.test(content)) return "review";
  if (/file|data room|member read|artifact/.test(content)) return "file";
  return "work";
}

function eventIcon(event) {
  const kind = eventKind(event);
  if (kind === "file") return "▤";
  if (kind === "review") return "✓";
  if (kind === "error") return "!";
  if (text(event.type).includes("exit")) return "↳";
  return "→";
}

function safeEventDetail(event) {
  const candidate = text(event.path || event.artifact);
  if (!candidate) return text(event.itemId || event.status || "durable telemetry");
  return candidate.split(/[\\/]/).filter(Boolean).pop() || "durable telemetry";
}

function renderEvents(snapshot) {
  if (state.feedPaused) return;
  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  const selected = [...events]
    .sort((a, b) => dateValue(b.timestamp) - dateValue(a.timestamp))
    .filter((event) => state.eventFilter === "all" || eventKind(event) === state.eventFilter)
    .slice(0, 36);
  dom.activityList.replaceChildren();
  if (!selected.length) dom.activityList.append(make("div", "event", "No events in this filter"));
  for (const event of selected) {
    const kind = eventKind(event);
    const row = make("article", `event ${kind}`);
    row.append(make("span", "event-icon", eventIcon(event)));
    const top = make("div", "event-top");
    top.append(make("b", "", roleName(event.role || "agent")), make("time", "", formatTime(event.timestamp)));
    row.append(top, make("p", "", text(event.summary || titleCase(event.type) || "Durable event")), make("small", "", safeEventDetail(event)));
    dom.activityList.append(row);
  }
  dom.feedState.textContent = `Live read-only projection · ${events.length} events in bounded stream`;
}

function renderRunHeader(snapshot) {
  const run = snapshot.run || {};
  const configured = state.config && state.config.run ? state.config.run : {};
  const title = configured.name || run.name || run.authoritativeRunId || "Auto Foundry run";
  dom.runTitle.textContent = title;
  dom.runMeta.textContent = `${run.authoritativeRunId || configured.authoritativeRunId || run.id || "run"} · ${state.requirements.length} requirements · live filesystem projection`;
  const rawStatus = text(run.status || configured.status || "running").toLowerCase();
  const status = normalizedStatus(rawStatus);
  dom.runStatus.className = `status-pill ${rawStatus === "running" ? "running" : status === "completed" ? "complete" : status}`;
  dom.runStatus.replaceChildren(make("i"), document.createTextNode(rawStatus === "running" ? "RUNNING" : statusLabel(status)));
  state.observedDate = new Date(snapshot.observedAt || Date.now());
  updateObservedLabel();
}

function updateObservedLabel() {
  if (!state.observedDate) return;
  dom.observedAt.textContent = `Observed ${relativeTime(state.observedDate)}`;
}

function renderInspector(node) {
  if (!node) {
    dom.inspector.classList.remove("is-open");
    dom.inspector.setAttribute("aria-hidden", "true");
    return;
  }
  const req = node._lane && node._lane.startsWith("REQ-") ? node._lane : nodeRequirement(node);
  dom.inspectorTitle.textContent = cardTitle(node, req);
  dom.inspectorBody.replaceChildren();
  const objective = make("section", "inspector-section");
  objective.append(make("span", "", "RECORDED OBJECTIVE"), make("p", "", text(node.objective) || "No durable objective was recorded for this node."));
  dom.inspectorBody.append(objective);
  const details = make("dl", "detail-grid");
  const relationships = state.edges.filter((edge) => edge.source === node.id || edge.target === node.id).length;
  const rows = [
    ["Role", roleName(node.role)],
    ["Status", statusLabel(node.status)],
    ["Lane", node._lane || "Shared services"],
    ["Lane source", node._laneInferred ? "Inferred from recorded link" : "Recorded node metadata"],
    ["Started", formatDateTime(node.startedAt)],
    ["Completed", formatDateTime(node.completedAt)],
    ["Relationships", String(relationships)],
    ["Technical node", compact(node.id, 52)],
    ["Session", compact(node.sessionId, 40) || "—"],
  ];
  for (const [key, value] of rows) details.append(make("dt", "", key), make("dd", "", value));
  dom.inspectorBody.append(details);
  const privacy = make("section", "inspector-section");
  privacy.append(make("span", "", "PREVIEW BOUNDARY"), make("p", "", "Only allowlisted durable metadata is shown. Prompts, model responses, raw data, credentials, and secrets are not loaded by this page."));
  dom.inspectorBody.append(privacy);
  dom.inspector.classList.add("is-open");
  dom.inspector.setAttribute("aria-hidden", "false");
}

function selectNode(nodeId) {
  state.selectedId = state.selectedId === nodeId ? null : nodeId;
  applyFilters();
  renderSelectedEdgeLabels();
  renderInspector(state.selectedId ? state.nodeById.get(state.selectedId) : null);
}

function renderMinimap() {
  dom.minimap.replaceChildren();
  const sx = 200 / state.stageWidth;
  const sy = 122 / state.stageHeight;
  state.lanes.forEach((lane, index) => {
    dom.minimap.append(svgElement("line", {
      class: "mini-lane", x1: 0, x2: 200, y1: index * LANE_HEIGHT * sy, y2: index * LANE_HEIGHT * sy,
    }));
  });
  for (const node of state.nodes) {
    const position = state.layout.get(node.id);
    if (!position) continue;
    const status = normalizedStatus(node.status);
    dom.minimap.append(svgElement("rect", {
      class: `mini-node ${isLive(node) ? "active" : status === "completed" ? "complete" : ""}`,
      x: position.x * sx,
      y: position.y * sy,
      width: Math.max(2, NODE_WIDTH * sx),
      height: Math.max(2, NODE_HEIGHT * sy),
      rx: 1,
    }));
  }
  const visibleX = Math.max(0, -state.view.x / state.view.scale);
  const visibleY = Math.max(0, -state.view.y / state.view.scale);
  const visibleW = dom.viewport.clientWidth / state.view.scale;
  const visibleH = dom.viewport.clientHeight / state.view.scale;
  dom.minimap.append(svgElement("rect", {
    class: "mini-window",
    x: visibleX * sx,
    y: visibleY * sy,
    width: Math.min(200, visibleW * sx),
    height: Math.min(122, visibleH * sy),
    rx: 1,
  }));
}

function clampScale(value) {
  return Math.min(1.35, Math.max(0.22, value));
}

function applyTransform() {
  state.view.scale = clampScale(state.view.scale);
  dom.stage.style.width = `${state.stageWidth}px`;
  dom.stage.style.height = `${state.stageHeight}px`;
  dom.stage.style.transform = `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;
  renderMinimap();
  updateOffscreenActive();
}

function updateViewContext(title, detail) {
  dom.viewContextTitle.textContent = title;
  dom.viewContextDetail.textContent = detail;
}

function centerOnBounds(bounds, requestedScale) {
  const width = dom.viewport.clientWidth;
  const height = dom.viewport.clientHeight;
  const scale = clampScale(requestedScale || state.view.scale);
  const centerX = bounds.x + bounds.width / 2;
  const centerY = bounds.y + bounds.height / 2;
  state.view = { x: width / 2 - centerX * scale, y: height / 2 - centerY * scale, scale };
  applyTransform();
}

function focusLane(laneId) {
  const index = state.lanes.findIndex((lane) => lane.id === laneId);
  if (index < 0) return;
  state.currentLane = laneId;
  state.followActive = false;
  dom.followActive.classList.remove("is-on");
  const nodes = state.nodes.filter((node) => node._lane === laneId);
  const lane = state.lanes[index];
  updateViewContext(lane.title, `${nodes.length} recorded node${nodes.length === 1 ? "" : "s"} · focused lane`);
  const right = nodes.length
    ? Math.max(...nodes.map((node) => state.layout.get(node.id).x + NODE_WIDTH))
    : NODE_START_X + 320;
  const desiredScale = Math.min(1, (dom.viewport.clientWidth - 80) / Math.max(700, right));
  centerOnBounds({ x: 0, y: index * LANE_HEIGHT, width: Math.max(700, right + 35), height: LANE_HEIGHT }, desiredScale);
  const laneNodes = new Map(state.lanes.map((lane) => [lane.id, state.nodes.filter((node) => node._lane === lane.id)]));
  renderRequirementNav(laneNodes);
}

function fitAll() {
  state.followActive = false;
  dom.followActive.classList.remove("is-on");
  updateViewContext("ALL REQUIREMENTS", `${state.requirements.length} lanes · every recorded node remains available`);
  const scale = Math.min(
    1,
    (dom.viewport.clientWidth - 26) / state.stageWidth,
    (dom.viewport.clientHeight - 26) / state.stageHeight,
  );
  centerOnBounds({ x: 0, y: 0, width: state.stageWidth, height: state.stageHeight }, scale);
}

function showActive() {
  const active = state.nodes.filter((node) => node.role !== "planner" && isLive(node));
  if (!active.length) {
    updateViewContext("MISSION CONTROL", "No live work item recorded in the current snapshot");
    const planner = state.nodeById.get("planner");
    if (planner) centerOnBounds(state.layout.get(planner.id), 1);
    return;
  }
  const liveTimestamp = (node) => {
    const parsed = Date.parse(text(node.startedAt || node.completedAt));
    return Number.isFinite(parsed) ? parsed : -1;
  };
  const primary = [...active].sort((left, right) => liveTimestamp(right) - liveTimestamp(left))[0];
  const primaryLane = primary._lane || "SHARED";
  const laneActive = active.filter((node) => (node._lane || "SHARED") === primaryLane);
  const contextTitle = primaryLane === "SHARED" ? "SHARED SERVICES" : primaryLane;
  updateViewContext(
    contextTitle,
    active.length === laneActive.length
      ? `${active.length} live node${active.length === 1 ? "" : "s"} · following durable state`
      : `${laneActive.length} live here · ${active.length} live across the mission`,
  );
  const positions = laneActive.map((node) => state.layout.get(node.id)).filter(Boolean);
  const left = Math.min(...positions.map((position) => position.x));
  const top = Math.min(...positions.map((position) => position.y));
  const right = Math.max(...positions.map((position) => position.x + position.width));
  const bottom = Math.max(...positions.map((position) => position.y + position.height));
  const scale = Math.min(1.05, (dom.viewport.clientWidth - 120) / Math.max(500, right - left + 120), (dom.viewport.clientHeight - 100) / Math.max(260, bottom - top + 80));
  centerOnBounds({ x: left - 60, y: top - 40, width: right - left + 120, height: bottom - top + 80 }, scale);
}

function updateOffscreenActive() {
  const active = state.nodes.filter((node) => node.role !== "planner" && isLive(node));
  if (!active.length) {
    dom.offscreen.hidden = true;
    return;
  }
  const vw = dom.viewport.clientWidth;
  const vh = dom.viewport.clientHeight;
  const visible = active.some((node) => {
    const position = state.layout.get(node.id);
    if (!position) return false;
    const left = state.view.x + position.x * state.view.scale;
    const top = state.view.y + position.y * state.view.scale;
    const right = left + position.width * state.view.scale;
    const bottom = top + position.height * state.view.scale;
    return right > 0 && bottom > 0 && left < vw && top < vh;
  });
  dom.offscreen.hidden = visible;
  dom.offscreenText.textContent = `${active.length} live node${active.length === 1 ? " is" : "s are"} outside this view`;
}

function zoomAt(centerX, centerY, factor) {
  const oldScale = state.view.scale;
  const nextScale = clampScale(oldScale * factor);
  const stageX = (centerX - state.view.x) / oldScale;
  const stageY = (centerY - state.view.y) / oldScale;
  state.view.x = centerX - stageX * nextScale;
  state.view.y = centerY - stageY * nextScale;
  state.view.scale = nextScale;
  applyTransform();
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  prepareSnapshot(snapshot);
  const laneNodes = buildLayout();
  renderRunHeader(snapshot);
  renderRequirementNav(laneNodes);
  renderLanes(laneNodes);
  renderNodes();
  renderEdges();
  updateMetrics(snapshot);
  renderEvents(snapshot);
  applyFilters();
  renderSelectedEdgeLabels();
  if (state.selectedId) renderInspector(state.nodeById.get(state.selectedId));

  const liveIds = state.nodes.filter((node) => node.role !== "planner" && isLive(node)).map((node) => node.id).sort();
  const signature = liveIds.join("|");
  if (!state.initializedView) {
    state.initializedView = true;
    requestAnimationFrame(() => {
      if (liveIds.length) showActive();
      else focusLane(state.requirements.find((req) => (laneNodes.get(req) || []).length) || "GLOBAL");
    });
  } else if (state.followActive && signature && signature !== state.activeSignature) {
    requestAnimationFrame(showActive);
  } else {
    applyTransform();
  }
  state.activeSignature = signature;
  dom.fatal.hidden = true;
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function loadConfig() {
  state.config = await fetchJson("/api/config");
  if (!state.config.readOnly) throw new Error("Preview boundary is not read-only");
}

async function poll() {
  try {
    if (!state.config) await loadConfig();
    const snapshot = await fetchJson("/api/snapshot");
    state.failures = 0;
    renderSnapshot(snapshot);
  } catch (error) {
    state.failures += 1;
    dom.feedState.textContent = `Snapshot unavailable · retrying (${state.failures})`;
    if (!state.snapshot || state.failures >= 3) {
      dom.fatal.hidden = false;
      dom.fatalMessage.textContent = `The separate preview could not read the allowlisted snapshot: ${error.message}`;
    }
  }
}

function bindControls() {
  document.querySelectorAll("[data-focus]").forEach((button) => {
    button.addEventListener("click", () => {
      state.focus = button.dataset.focus;
      document.querySelectorAll("[data-focus]").forEach((candidate) => candidate.classList.toggle("is-active", candidate === button));
      applyFilters();
    });
  });
  document.querySelectorAll("[data-event]").forEach((button) => {
    button.addEventListener("click", () => {
      state.eventFilter = button.dataset.event;
      document.querySelectorAll("[data-event]").forEach((candidate) => candidate.classList.toggle("is-active", candidate === button));
      if (state.snapshot) renderEvents(state.snapshot);
    });
  });
  dom.searchInput.addEventListener("input", () => { state.search = dom.searchInput.value; applyFilters(); });
  dom.followActive.addEventListener("click", () => {
    state.followActive = !state.followActive;
    dom.followActive.classList.toggle("is-on", state.followActive);
    if (state.followActive) showActive();
  });
  dom.zoomIn.addEventListener("click", () => zoomAt(dom.viewport.clientWidth / 2, dom.viewport.clientHeight / 2, 1.18));
  dom.zoomOut.addEventListener("click", () => zoomAt(dom.viewport.clientWidth / 2, dom.viewport.clientHeight / 2, 1 / 1.18));
  dom.fitMap.addEventListener("click", fitAll);
  dom.showActive.addEventListener("click", showActive);
  dom.offscreenShow.addEventListener("click", showActive);
  dom.pauseFeed.addEventListener("click", () => {
    state.feedPaused = !state.feedPaused;
    dom.pauseFeed.textContent = state.feedPaused ? "▶" : "Ⅱ";
    dom.pauseFeed.title = state.feedPaused ? "Resume feed" : "Pause feed";
    dom.feedState.parentElement.classList.toggle("paused", state.feedPaused);
    dom.feedState.textContent = state.feedPaused ? "Activity display paused · graph still live" : "Polling read-only snapshot";
    if (!state.feedPaused && state.snapshot) renderEvents(state.snapshot);
  });
  dom.closeInspector.addEventListener("click", () => selectNode(state.selectedId));
  dom.viewport.addEventListener("click", (event) => {
    if (event.target === dom.viewport && state.selectedId) selectNode(state.selectedId);
  });

  dom.viewport.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".node-card") || event.target.closest(".minimap-wrap") || event.target.closest(".offscreen")) return;
    state.drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, startX: state.view.x, startY: state.view.y };
    dom.viewport.setPointerCapture(event.pointerId);
    dom.viewport.classList.add("is-dragging");
    state.followActive = false;
    dom.followActive.classList.remove("is-on");
  });
  dom.viewport.addEventListener("pointermove", (event) => {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    state.view.x = state.drag.startX + event.clientX - state.drag.x;
    state.view.y = state.drag.startY + event.clientY - state.drag.y;
    applyTransform();
  });
  const endDrag = (event) => {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    state.drag = null;
    dom.viewport.classList.remove("is-dragging");
  };
  dom.viewport.addEventListener("pointerup", endDrag);
  dom.viewport.addEventListener("pointercancel", endDrag);
  dom.viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = dom.viewport.getBoundingClientRect();
    zoomAt(event.clientX - rect.left, event.clientY - rect.top, event.deltaY < 0 ? 1.12 : 1 / 1.12);
    state.followActive = false;
    dom.followActive.classList.remove("is-on");
  }, { passive: false });

  dom.minimap.addEventListener("click", (event) => {
    event.stopPropagation();
    const rect = dom.minimap.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * state.stageWidth;
    const y = ((event.clientY - rect.top) / rect.height) * state.stageHeight;
    centerOnBounds({ x: x - 1, y: y - 1, width: 2, height: 2 }, state.view.scale);
    state.followActive = false;
    dom.followActive.classList.remove("is-on");
  });

  window.addEventListener("resize", () => applyTransform());
}

bindControls();
poll();
setInterval(poll, POLL_INTERVAL_MS);
setInterval(updateObservedLabel, 1000);
