(function () {
  "use strict";

  var POLL_INTERVAL_MS = 2500;
  var ACTIVE_STATUSES = new Set(["active", "dispatching", "running", "starting"]);
  var DONE_STATUSES = new Set(["complete", "completed", "accepted", "integrated", "committed", "verified"]);
  var FAILED_STATUSES = new Set(["failed", "error", "blocked"]);
  var ROLE_META = {
    analytical_owner: { label: "Analytical Owner", short: "AO", stage: "analyze" },
    entity_resolution_owner: { label: "Entity Resolution Owner", short: "ID", stage: "identities" },
    identity_owner: { label: "Identity Owner", short: "ID", stage: "identities" },
    identity_reviewer: { label: "Identity Reviewer", short: "RV", stage: "identities" },
    business_reviewer: { label: "Business Reviewer", short: "BR", stage: "review" },
    reviewer: { label: "Reviewer", short: "RV", stage: "review" },
    integration_agent: { label: "Integration Agent", short: "IN", stage: "integrate" },
    integration_fidelity_reviewer: { label: "Integration Fidelity Reviewer", short: "IF", stage: "integrate" }
  };
  var STAGE_META = {
    analyze: { label: "Analysis", short: "AO" },
    identities: { label: "Identities", short: "ID" },
    review: { label: "Review", short: "BR" },
    integrate: { label: "Integration", short: "IN" }
  };
  var state = {
    connected: false,
    config: null,
    snapshot: null,
    selectedRequirementId: "",
    refreshing: false
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function text(id, value) {
    var element = byId(id);
    if (element) element.textContent = String(value);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function normalizeRole(value) {
    return String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  }

  function normalizeStatus(value) {
    return String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  }

  function parseRequirement() {
    for (var i = 0; i < arguments.length; i += 1) {
      var value = String(arguments[i] || "");
      var match = value.match(/\bREQ[\s._:-]*0*(\d{1,3})\b/i);
      if (match) return "REQ-" + String(Number(match[1])).padStart(3, "0");
    }
    return "";
  }

  function requirementNumber(id) {
    var match = String(id || "").match(/(\d+)$/);
    return match ? Number(match[1]) : 0;
  }

  function displayRequirementName(id) {
    return "Requirement " + String(requirementNumber(id)).padStart(3, "0");
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, function (letter) { return letter.toUpperCase(); });
  }

  function prettyDomain(value) {
    var clean = String(value || "").replace(/-identities$/i, "").replace(/[_-]+/g, " ");
    return titleCase(clean || "Identity domain") + " Identities";
  }

  function timeValue(value) {
    var parsed = Date.parse(value || "");
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function formatTime(value) {
    var parsed = timeValue(value);
    if (!parsed) return "—";
    return new Date(parsed).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function formatRelative(value) {
    var parsed = timeValue(value);
    if (!parsed) return "freshness unavailable";
    var seconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
    if (seconds < 5) return "observed just now";
    if (seconds < 60) return "observed " + seconds + "s ago";
    if (seconds < 3600) return "observed " + Math.floor(seconds / 60) + "m ago";
    return "observed " + Math.floor(seconds / 3600) + "h ago";
  }

  function eventRequirement(event) {
    return parseRequirement(event && event.itemId, event && event.summary, event && event.artifact);
  }

  function nodeRequirement(node) {
    return parseRequirement(
      node && node.subjectId,
      node && node.objective,
      node && node.label,
      node && node.logicalOwner,
      node && node.taskName,
      node && node.domainId
    );
  }

  function eventObjective(event) {
    var parts = String(event && event.summary || "").split("·").map(function (part) { return part.trim(); });
    if (parts.length >= 3) return parts.slice(2).join(" · ");
    return titleCase(event && event.type || "Durable activity");
  }

  function stageFor(role, textValue) {
    var normalizedRole = normalizeRole(role);
    if (ROLE_META[normalizedRole]) return ROLE_META[normalizedRole].stage;
    var haystack = String(textValue || "").toLowerCase();
    if (haystack.indexOf("identity") >= 0) return "identities";
    if (haystack.indexOf("integration") >= 0 || haystack.indexOf("integrate") >= 0) return "integrate";
    if (haystack.indexOf("review") >= 0) return "review";
    if (haystack.indexOf("requirement") >= 0 || haystack.indexOf("analysis") >= 0) return "analyze";
    return "";
  }

  function isStartEvent(event) {
    var type = normalizeStatus(event && event.type);
    var status = normalizeStatus(event && event.status);
    return type === "dispatch_started" || (type.indexOf("dispatch") >= 0 && status === "dispatching");
  }

  function isEndEvent(event) {
    var type = normalizeStatus(event && event.type);
    var status = normalizeStatus(event && event.status);
    return type === "role_exit" || type.indexOf("completed") >= 0 || DONE_STATUSES.has(status);
  }

  function isFailureEvent(event) {
    var type = normalizeStatus(event && event.type);
    var status = normalizeStatus(event && event.status);
    return FAILED_STATUSES.has(status) || type.indexOf("fail") >= 0 || type.indexOf("error") >= 0;
  }

  function latestTimestamp(record) {
    return timeValue(record && (record.timestamp || record.startedAt || record.completedAt));
  }

  function sortedEvents(snapshot) {
    return (Array.isArray(snapshot.events) ? snapshot.events.slice() : [])
      .sort(function (a, b) { return latestTimestamp(a) - latestTimestamp(b); });
  }

  function deriveActiveWorkers(snapshot, activeCount) {
    if (activeCount <= 0) return [];
    var activeByKey = new Map();
    sortedEvents(snapshot).forEach(function (event) {
      var role = normalizeRole(event.role);
      var meta = ROLE_META[role];
      var requirementId = eventRequirement(event);
      if (!meta || !requirementId) return;
      var objective = eventObjective(event);
      var key = role + "|" + requirementId + "|" + objective.toLowerCase();
      if (isStartEvent(event)) activeByKey.set(key, {
        role: role,
        requirementId: requirementId,
        objective: objective,
        timestamp: event.timestamp
      });
      if (isEndEvent(event) || isFailureEvent(event)) activeByKey.delete(key);
    });

    var workers = Array.from(activeByKey.values()).sort(function (a, b) {
      return timeValue(b.timestamp) - timeValue(a.timestamp);
    });
    if (workers.length < activeCount) {
      var activeNodes = (snapshot.nodes || []).filter(function (node) {
        var role = normalizeRole(node.role);
        return ROLE_META[role] && (node.active === true || ACTIVE_STATUSES.has(normalizeStatus(node.status)));
      }).sort(function (a, b) { return latestTimestamp(b) - latestTimestamp(a); });
      activeNodes.forEach(function (node) {
        if (workers.length >= activeCount) return;
        var role = normalizeRole(node.role);
        var requirementId = nodeRequirement(node);
        var duplicate = workers.some(function (worker) {
          return worker.role === role && worker.requirementId === requirementId;
        });
        if (!duplicate) workers.push({
          role: role,
          requirementId: requirementId,
          objective: node.objective || node.taskName || ROLE_META[role].label,
          timestamp: node.startedAt || node.completedAt
        });
      });
    }
    while (workers.length < activeCount) {
      workers.push({
        role: "",
        requirementId: "",
        objective: "Executing role present in capacity projection",
        timestamp: snapshot.observedAt
      });
    }
    return workers.slice(0, activeCount);
  }

  function requirementIds(snapshot) {
    var context = snapshot.missionContext || {};
    var ids = Array.isArray(context.itemIds) && context.itemIds.length ? context.itemIds : context.requirementIds;
    var normalized = (Array.isArray(ids) ? ids : []).map(function (id) {
      return parseRequirement(id);
    }).filter(Boolean);
    if (!normalized.length) {
      var count = Number(snapshot.run && snapshot.run.requirementCount) || 0;
      for (var i = 1; i <= count; i += 1) normalized.push("REQ-" + String(i).padStart(3, "0"));
    }
    return Array.from(new Set(normalized)).sort(function (a, b) {
      return requirementNumber(a) - requirementNumber(b);
    });
  }

  function identityReferences(snapshot) {
    var nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
    var nodeById = new Map(nodes.map(function (node) { return [node.id, node]; }));
    var refs = new Map();
    (snapshot.edges || []).forEach(function (edge) {
      var source = nodeById.get(edge.source) || {};
      var target = nodeById.get(edge.target) || {};
      var sourceReq = nodeRequirement(source);
      var targetReq = nodeRequirement(target);
      if (sourceReq && normalizeRole(target.role) === "identity_domain") {
        refs.set(sourceReq, target);
      }
      if (targetReq && normalizeRole(source.role) === "identity_domain") {
        refs.set(targetReq, source);
      }
    });
    return refs;
  }

  function stageState(stage, nodes, event) {
    if (event) {
      if (isFailureEvent(event)) return "failed";
      if (isStartEvent(event)) return "live";
      if (isEndEvent(event)) return "done";
    }
    var statuses = nodes.map(function (node) { return normalizeStatus(node.status); });
    if (statuses.some(function (status) { return FAILED_STATUSES.has(status); })) return "failed";
    if (nodes.some(function (node) { return node.active === true || ACTIVE_STATUSES.has(normalizeStatus(node.status)); })) return "live";
    if (statuses.some(function (status) { return DONE_STATUSES.has(status); })) return "done";
    if (statuses.indexOf("historical") >= 0) return "recorded";
    return "empty";
  }

  function buildRequirementModels(snapshot) {
    var nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
    var events = sortedEvents(snapshot);
    var refs = identityReferences(snapshot);
    return requirementIds(snapshot).map(function (requirementId) {
      var relatedNodes = nodes.filter(function (node) { return nodeRequirement(node) === requirementId; });
      var relatedEvents = events.filter(function (event) { return eventRequirement(event) === requirementId; });
      var phases = {};
      ["analyze", "identities", "review", "integrate"].forEach(function (stage) {
        var stageNodes = relatedNodes.filter(function (node) {
          return stageFor(node.role, node.objective || node.taskName || node.label) === stage;
        });
        var stageEvents = relatedEvents.filter(function (event) {
          return stageFor(event.role, event.summary || event.type) === stage;
        });
        var latestEvent = stageEvents.length ? stageEvents[stageEvents.length - 1] : null;
        phases[stage] = {
          key: stage,
          state: stageState(stage, stageNodes, latestEvent),
          nodes: stageNodes,
          event: latestEvent,
          lastAt: latestEvent && latestEvent.timestamp || (stageNodes.slice().sort(function (a, b) {
            return latestTimestamp(b) - latestTimestamp(a);
          })[0] || {}).completedAt || ""
        };
      });

      if (refs.has(requirementId) && phases.identities.state === "empty") {
        phases.identities.state = "state";
        phases.identities.domain = refs.get(requirementId);
      }

      var substantiveStates = ["live", "done", "recorded", "failed", "state"];
      var hasWork = Object.keys(phases).some(function (key) {
        return substantiveStates.indexOf(phases[key].state) >= 0;
      });
      if (!hasWork) {
        phases.analyze.state = "queued";
      } else {
        if (phases.identities.state === "empty" && phases.analyze.state === "done") phases.identities.state = "neutral";
        if (phases.review.state === "empty" && ["done", "recorded"].indexOf(phases.analyze.state) >= 0) phases.review.state = "queued";
        if (phases.integrate.state === "empty" && ["done", "recorded"].indexOf(phases.review.state) >= 0) phases.integrate.state = "queued";
        if (phases.integrate.state === "empty" && phases.review.state === "live") phases.integrate.state = "queued";
      }

      var phaseValues = Object.keys(phases).map(function (key) { return phases[key]; });
      var outcome = "not-started";
      if (phaseValues.some(function (phase) { return phase.state === "failed"; })) outcome = "failed";
      else if (phases.integrate.state === "done") outcome = "accepted";
      else if (phaseValues.some(function (phase) { return phase.state === "live"; })) outcome = "working";
      else if (hasWork) outcome = "waiting";

      return {
        id: requirementId,
        name: displayRequirementName(requirementId),
        phases: phases,
        outcome: outcome,
        nodeCount: relatedNodes.length,
        events: relatedEvents,
        latestAt: relatedEvents.length ? relatedEvents[relatedEvents.length - 1].timestamp : ""
      };
    });
  }

  function phaseDetail(phase) {
    if (phase.state === "live") return "Running now";
    if (phase.state === "failed") return "Failed";
    if (phase.state === "done") return "Complete" + (phase.lastAt ? " " + formatTime(phase.lastAt).slice(0, 5) : "");
    if (phase.state === "recorded") return "Historical record";
    if (phase.state === "state") return "Current · not worker";
    if (phase.state === "neutral") return "Not observed";
    if (phase.state === "queued") return "Queued";
    return "";
  }

  function renderPhase(phase) {
    var meta = STAGE_META[phase.key];
    if (phase.state === "empty") return '<div class="phase empty"></div>';
    if (phase.state === "neutral") {
      return '<div class="phase neutral"><span>—</span><b>No identity record</b><small>' + escapeHtml(phaseDetail(phase)) + '</small></div>';
    }
    if (phase.state === "state") {
      var domain = phase.domain && (phase.domain.domainId || phase.domain.label);
      return '<div class="phase state-reference"><span>ID</span><b>' + escapeHtml(prettyDomain(domain)) + '</b><small>Current · not worker</small></div>';
    }
    var shortLabel = meta.short;
    if (phase.event && ROLE_META[normalizeRole(phase.event.role)]) shortLabel = ROLE_META[normalizeRole(phase.event.role)].short;
    var detail = phaseDetail(phase);
    var liveDot = phase.state === "live" ? "<i></i>" : "";
    return '<div class="phase ' + escapeHtml(phase.state) + '"><span>' + escapeHtml(shortLabel) + '</span><b>' +
      escapeHtml(meta.label) + '</b><small>' + liveDot + escapeHtml(detail) + '</small></div>';
  }

  function outcomeMarkup(model) {
    if (model.outcome === "accepted") return '<div class="outcome accepted"><i>✓</i><b>Accepted</b><small>Ready</small></div>';
    if (model.outcome === "failed") return '<div class="outcome failed"><i>!</i><b>Issue</b><small>Needs attention</small></div>';
    if (model.outcome === "working") return '<div class="outcome waiting"><i>···</i><b>Working</b><small>In progress</small></div>';
    if (model.outcome === "waiting") return '<div class="outcome waiting"><i>···</i><b>Waiting</b><small>Next dispatch</small></div>';
    return '<div class="outcome not-started"><b>Not started</b></div>';
  }

  function rowClass(model) {
    if (model.id === state.selectedRequirementId) return "selected-row";
    if (model.outcome === "accepted") return "complete-row";
    if (model.outcome === "not-started") return "queued-row";
    return "progress-row";
  }

  function renderBoard(models, snapshot) {
    var board = byId("requirement-board");
    if (!board) return;
    var previousScroll = board.scrollTop;
    board.querySelectorAll(".requirement-row").forEach(function (row) { row.remove(); });
    var html = models.map(function (model) {
      var statusLabel = model.outcome === "accepted" ? "Closed" :
        model.outcome === "not-started" ? "Queued" :
          model.outcome === "failed" ? "Issue" :
            model.outcome === "working" ? "Active" : "In progress";
      return '<div class="requirement-row phase-grid ' + rowClass(model) + '" data-requirement="' +
        escapeHtml(model.id) + '" role="button" tabindex="0" aria-pressed="' +
        String(model.id === state.selectedRequirementId) + '">' +
        '<div class="requirement-name"><b>' + escapeHtml(model.id) + '</b><strong>' +
        escapeHtml(model.name) + '</strong><small>' + escapeHtml(statusLabel) + " · " +
        escapeHtml(model.nodeCount) + ' records</small></div>' +
        renderPhase(model.phases.analyze) +
        renderPhase(model.phases.identities) +
        renderPhase(model.phases.review) +
        renderPhase(model.phases.integrate) +
        outcomeMarkup(model) +
        '</div>';
    }).join("");
    board.insertAdjacentHTML("beforeend", html);
    board.scrollTop = previousScroll;
    board.querySelectorAll(".requirement-row").forEach(function (row) {
      function select() {
        state.selectedRequirementId = row.getAttribute("data-requirement") || "";
        renderBoard(models, snapshot);
        renderFocus(models, snapshot);
      }
      row.addEventListener("click", select);
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          select();
        }
      });
    });
  }

  function activeWorkersForRequirement(snapshot, requirementId) {
    var capacity = snapshot.capacity || {};
    return deriveActiveWorkers(snapshot, Math.max(0, Number(capacity.active) || 0))
      .filter(function (worker) { return worker.requirementId === requirementId; });
  }

  function focusStageMarkup(phase, index) {
    var meta = STAGE_META[phase.key];
    var className = "chain-step";
    var icon = String(index).padStart(2, "0");
    var badge = "WAIT";
    var detail = "No durable activity recorded";
    if (phase.state === "done" || phase.state === "recorded") {
      className += " finished";
      icon = "✓";
      badge = "DONE";
      detail = (phase.event && ROLE_META[normalizeRole(phase.event.role)] || {}).label || meta.label;
      if (phase.lastAt) detail += " · " + formatTime(phase.lastAt);
    } else if (phase.state === "state") {
      className += " state-step";
      icon = "ID";
      badge = "STATE";
      detail = prettyDomain(phase.domain && (phase.domain.domainId || phase.domain.label)) + " · not a worker";
    } else if (phase.state === "neutral") {
      className += " state-step";
      icon = "—";
      badge = "NONE";
      detail = "No identity activity linked in the durable projection";
    } else if (phase.state === "live") {
      className += " running";
      var roleMeta = phase.event && ROLE_META[normalizeRole(phase.event.role)];
      icon = roleMeta ? roleMeta.short : meta.short;
      badge = "LIVE";
      detail = (roleMeta ? roleMeta.label : meta.label) + " · running now";
    } else if (phase.state === "failed") {
      className += " failed";
      icon = "!";
      badge = "ISSUE";
      detail = "Latest durable stage event failed";
    } else {
      className += " future";
      badge = phase.state === "queued" ? "NEXT" : "WAIT";
      detail = phase.state === "queued" ? "Ready for a future dispatch" : "No durable stage record yet";
    }
    return '<article class="' + className + '"><i>' + escapeHtml(icon) + '</i><div><b>' +
      escapeHtml(meta.label) + '</b><small>' + escapeHtml(detail) + '</small></div><em>' +
      escapeHtml(badge) + '</em></article>';
  }

  function eventLabel(event) {
    if (isStartEvent(event)) return "Dispatch started";
    if (isFailureEvent(event)) return "Stage reported an issue";
    if (isEndEvent(event)) return "Role completed";
    if (String(event.type || "").indexOf("data_room") >= 0) return "Evidence activity";
    return titleCase(event.type || "Durable activity");
  }

  function renderFocus(models, snapshot) {
    var panel = byId("focus-panel");
    if (!panel) return;
    var model = models.find(function (candidate) { return candidate.id === state.selectedRequirementId; }) || models[0];
    if (!model) return;
    var phaseList = ["analyze", "identities", "review", "integrate"].map(function (key) { return model.phases[key]; });
    var completedStages = phaseList.filter(function (phase) {
      return ["done", "recorded", "state", "neutral"].indexOf(phase.state) >= 0;
    }).length + (model.outcome === "accepted" ? 1 : 0);
    var livePhase = phaseList.find(function (phase) { return phase.state === "live"; });
    var statusText = livePhase ? STAGE_META[livePhase.key].label.toUpperCase() :
      model.outcome === "accepted" ? "ACCEPTED" :
        model.outcome === "failed" ? "ISSUE" :
          model.outcome === "not-started" ? "QUEUED" : "WAITING";
    var pillClass = model.outcome === "accepted" ? " done" : model.outcome === "failed" ? " failed" : "";
    var focusBar = "";
    for (var index = 0; index < 5; index += 1) {
      var barClasses = [];
      if (index < completedStages) barClasses.push("done");
      if (livePhase && index === phaseList.indexOf(livePhase)) barClasses.push("active");
      focusBar += '<i' + (barClasses.length ? ' class="' + barClasses.join(" ") + '"' : "") + '></i>';
    }
    var workers = activeWorkersForRequirement(snapshot, model.id);
    var identityPhase = model.phases.identities;
    var identityState = identityPhase.state === "state" ? prettyDomain(identityPhase.domain && (identityPhase.domain.domainId || identityPhase.domain.label)) :
      identityPhase.state === "neutral" ? "No linked identity state" : "Identity stage";
    var recentEvents = model.events.slice().sort(function (a, b) {
      return latestTimestamp(b) - latestTimestamp(a);
    }).slice(0, 4);
    var recentMarkup = recentEvents.length ? recentEvents.map(function (event) {
      var eventClass = isFailureEvent(event) ? "event-failed" : isStartEvent(event) ? "event-live" : "event-done";
      var roleMeta = ROLE_META[normalizeRole(event.role)];
      return '<article><time>' + escapeHtml(formatTime(event.timestamp)) + '</time><i class="' +
        eventClass + '"></i><p><b>' + escapeHtml(eventLabel(event)) + '</b><small>' +
        escapeHtml((roleMeta ? roleMeta.label : "Durable telemetry") + " · " + eventObjective(event)) +
        '</small></p></article>';
    }).join("") : '<article><time>—</time><i class="event-data"></i><p><b>No requirement events yet</b><small>Waiting for the first durable dispatch</small></p></article>';

    panel.innerHTML =
      '<header><div><span class="eyebrow">SELECTED REQUIREMENT</span><h2>' + escapeHtml(model.id) +
      '</h2><p>' + escapeHtml(model.name) + '</p></div><span class="review-pill' + pillClass +
      '"><i></i> ' + escapeHtml(statusText) + '</span></header>' +
      '<section class="focus-progress"><div><span>MISSION PROGRESS</span><strong>' +
      escapeHtml(completedStages) + ' / 5 stages</strong></div><div class="focus-bar">' + focusBar + '</div></section>' +
      '<section class="execution-chain"><span class="section-label">EXECUTION CHAIN</span>' +
      phaseList.map(function (phase, index) { return focusStageMarkup(phase, index + 1); }).join("") +
      '<article class="chain-step ' + (model.outcome === "accepted" ? "finished" : "future") +
      '"><i>' + (model.outcome === "accepted" ? "✓" : "05") + '</i><div><b>Business result</b><small>' +
      escapeHtml(model.outcome === "accepted" ? "Durable integration completed" : "Waiting for integration completion") +
      '</small></div><em>' + (model.outcome === "accepted" ? "DONE" : "WAIT") + '</em></article></section>' +
      '<section class="truth-explainer"><div class="actual-worker"><span>' +
      escapeHtml(workers[0] && ROLE_META[workers[0].role] ? ROLE_META[workers[0].role].short : "0") +
      '</span><p><b>' + escapeHtml(workers.length) + ' real worker' + (workers.length === 1 ? "" : "s") +
      ' here</b><small>Only executing roles count toward capacity.</small></p></div>' +
      '<div class="not-worker"><span>ID</span><p><b>' + escapeHtml(identityState) +
      '</b><small>System state is visible, never counted as a worker.</small></p></div></section>' +
      '<section class="recent-events"><span class="section-label">RECENT DURABLE ACTIVITY</span>' +
      recentMarkup + '</section>';
  }

  function renderWorkerCard(snapshot, activeCount) {
    var slot = byId("live-worker-slot");
    if (!slot) return;
    var workers = deriveActiveWorkers(snapshot, activeCount);
    if (!workers.length) {
      slot.classList.add("empty-worker");
      slot.innerHTML = '<span class="avatar">—</span><div><b>No executing role observed</b><small>Planner and system states remain separate</small></div><em>IDLE</em>';
      return;
    }
    slot.classList.remove("empty-worker");
    var worker = workers[0];
    var meta = ROLE_META[worker.role] || { label: "Executing role", short: "WK" };
    var extra = workers.length > 1 ? " +" + (workers.length - 1) + " more" : "";
    var detail = [worker.requirementId, worker.objective].filter(Boolean).join(" · ");
    slot.innerHTML = '<span class="avatar violet">' + escapeHtml(meta.short) + '</span><div><b>' +
      escapeHtml(meta.label + extra) + '</b><small>' + escapeHtml(detail || "Capacity projection") +
      '</small></div><em><i></i> LIVE</em>';
  }

  function renderSystemStates(snapshot) {
    var container = byId("system-state-rows");
    if (!container) return;
    var domains = (snapshot.nodes || []).filter(function (node) {
      return normalizeRole(node.role) === "identity_domain" && (node.active === true || ACTIVE_STATUSES.has(normalizeStatus(node.status)));
    });
    var domainLabel = domains.length ? prettyDomain(domains[0].domainId || domains[0].label) : "No current identity domain";
    var domainDetail = domains.length > 1 ? domains.length + " current domains" : domains.length ? "Current domain state" : "No current state observed";
    var eventCount = Array.isArray(snapshot.events) ? snapshot.events.length : 0;
    container.innerHTML =
      '<div class="state-row"><span class="state-symbol">ID</span><div><strong>' +
      escapeHtml(domainLabel) + '</strong><small>' + escapeHtml(domainDetail) +
      '</small></div><em>' + (domains.length ? "CURRENT" : "NONE") + '</em></div>' +
      '<div class="state-row"><span class="state-symbol">↯</span><div><strong>Durable projection</strong><small>' +
      escapeHtml(eventCount) + ' events in bounded view</small></div><em>CURRENT</em></div>';
  }

  function renderProgress(models) {
    var accepted = models.filter(function (model) { return model.outcome === "accepted"; }).length;
    var inProgress = models.filter(function (model) {
      return ["working", "waiting", "failed"].indexOf(model.outcome) >= 0;
    }).length;
    var queued = models.length - accepted - inProgress;
    text("mission-total", models.length + " TOTAL");
    text("accepted-count", accepted);
    text("progress-count", inProgress);
    text("queued-count", queued);
    var denominator = Math.max(1, models.length);
    byId("accepted-bar").style.width = (accepted / denominator * 100) + "%";
    byId("progress-bar").style.width = (inProgress / denominator * 100) + "%";
    byId("queued-bar").style.width = (queued / denominator * 100) + "%";
  }

  function renderFreshness() {
    if (!state.connected || !state.snapshot) return;
    text("connection-freshness", formatRelative(state.snapshot.observedAt));
  }

  function render(snapshot) {
    var models = buildRequirementModels(snapshot);
    var capacity = snapshot.capacity || snapshot.run && snapshot.run.capacity || {};
    var activeCount = Math.max(0, Number(capacity.active) || 0);
    var totalCapacity = Math.max(0, Number(capacity.total) || 0);
    var configRun = state.config && state.config.run || {};
    var runName = configRun.name || snapshot.run && snapshot.run.name || "Auto Foundry mission";
    var revision = snapshot.run && snapshot.run.dataRevision && snapshot.run.dataRevision.revisionId ||
      configRun.dataRevision && configRun.dataRevision.revisionId || "revision unavailable";

    if (!state.selectedRequirementId || !models.some(function (model) { return model.id === state.selectedRequirementId; })) {
      var currentWorker = deriveActiveWorkers(snapshot, activeCount)[0];
      var latestModel = models.slice().sort(function (a, b) { return timeValue(b.latestAt) - timeValue(a.latestAt); })[0];
      state.selectedRequirementId = currentWorker && currentWorker.requirementId ||
        latestModel && latestModel.id || models[0] && models[0].id || "";
    }

    document.title = "Auto Foundry · " + runName + " · Live Mission Board";
    text("run-title", runName);
    text("run-meta", "Filesystem projection · " + models.length + " requirements · " + revision);
    text("nav-requirement-count", models.length);
    text("concept-label", "LIVE MISSION BOARD");
    var connection = byId("connection-state");
    connection.classList.add("live-connection");
    connection.classList.remove("connection-error");
    connection.innerHTML = "<i></i> LIVE READ-ONLY";
    text("rail-status-label", "LIVE PROJECTION");
    text("rail-status-detail", "READ-ONLY · 2.5S REFRESH");
    byId("rail-status-dot").classList.add("connected");

    text("capacity-active", activeCount);
    text("capacity-total", "/ " + totalCapacity);
    text("capacity-caption", activeCount === 1 ? "worker now" : "workers now");
    renderWorkerCard(snapshot, activeCount);

    var planner = (snapshot.nodes || []).find(function (node) { return normalizeRole(node.role) === "planner"; });
    var plannerActive = !!planner && (planner.active === true || ACTIVE_STATUSES.has(normalizeStatus(planner.status)));
    text("planner-status", plannerActive ? "Coordinating the mission" : "Coordinator state recorded");
    text("planner-capacity-note", "Excluded from " + activeCount + " / " + totalCapacity + " capacity");
    byId("planner-pulse").classList.toggle("inactive", !plannerActive);

    renderSystemStates(snapshot);
    renderProgress(models);
    text("board-requirement-count", models.length);
    text("board-record-count", Math.max(0, (snapshot.nodes || []).filter(function (node) {
      return normalizeRole(node.role) !== "planner";
    }).length));
    text("board-relationship-count", (snapshot.edges || []).length);
    renderBoard(models, snapshot);
    renderFocus(models, snapshot);
    renderFreshness();
  }

  async function readJson(url) {
    var response = await fetch(url, { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  }

  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      var snapshot = await readJson("/api/snapshot");
      if (!snapshot || !Array.isArray(snapshot.nodes) || !Array.isArray(snapshot.events)) {
        throw new Error("Invalid snapshot");
      }
      state.snapshot = snapshot;
      state.connected = true;
      render(snapshot);
    } catch (error) {
      if (state.connected) {
        var connection = byId("connection-state");
        connection.classList.remove("live-connection");
        connection.classList.add("connection-error");
        connection.innerHTML = "<i></i> SNAPSHOT UNAVAILABLE";
        text("connection-freshness", "Last good projection retained");
      }
    } finally {
      state.refreshing = false;
    }
  }

  async function connect() {
    try {
      var config = await readJson("/api/config");
      if (!config || config.preview !== true || config.readOnly !== true) return;
      state.config = config;
      await refresh();
      window.setInterval(refresh, POLL_INTERVAL_MS);
      window.setInterval(renderFreshness, 1000);
    } catch (error) {
      /* Port 8778 intentionally remains a frozen, disconnected design reference. */
    }
  }

  connect();
}());
