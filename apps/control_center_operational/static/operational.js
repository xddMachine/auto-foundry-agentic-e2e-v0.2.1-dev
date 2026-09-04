/* Operational Control Center enhancements.
 *
 * This script is loaded after the read-only Control Center application. It
 * replaces only presentation/controller functions and talks to the separate
 * operational API. The analytical runtime and dashboard assembler are never
 * imported into the browser.
 */

const operationalFiles = new Map();
const operationalNodeCache = new Map();
let operationalDraft = null;
let operationalStatusTimer = null;
let operationalAttachedRunId = null;
let operationalRunsTimer = null;
let operationalRunsRefreshInFlight = false;
let operationalPendingLaunchTimer = null;
let operationalLaunchRequestPending = false;
let operationalStatusPollGeneration = 0;
let operationalStatusPollInFlight = false;
let operationalEventsPollInFlight = false;
let operationalLastSnapshotRefreshAt = 0;
let operationalCacheRunId = null;
let operationalGraphWidth = 1460;
let operationalGraphHeight = 720;
let operationalRunControlInFlight = false;
let operationalProductRegenerationInFlight = false;
let operationalRunControlState = null;
let operationalPreparationKey = null;
const OPERATIONAL_PREPARATION_STORAGE_KEY = "auto-foundry.operational.preparation-key";

function operationalPreparationIdentity() {
  if (operationalPreparationKey) return operationalPreparationKey;
  try {
    operationalPreparationKey = window.localStorage.getItem(OPERATIONAL_PREPARATION_STORAGE_KEY);
  } catch (_error) {
    operationalPreparationKey = null;
  }
  if (!operationalPreparationKey) {
    const random = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    operationalPreparationKey = `prep-${random}`;
    try {
      window.localStorage.setItem(OPERATIONAL_PREPARATION_STORAGE_KEY, operationalPreparationKey);
    } catch (_error) {
      // Private browsing/storage-disabled contexts still get a per-tab key;
      // the server remains the durable authority when storage is available.
    }
  }
  return operationalPreparationKey;
}

function operationalForgetPreparationIdentity() {
  operationalPreparationKey = null;
  try {
    window.localStorage.removeItem(OPERATIONAL_PREPARATION_STORAGE_KEY);
  } catch (_error) {
    // Storage is optional; the next request will create a fresh in-memory key.
  }
}

const layerOneRoleLabel = roleLabel;
roleLabel = function operationalRoleLabel(value) {
  return value === "reviewer" ? "Reviewer" : layerOneRoleLabel(value);
};

const layerOneRoleGlyph = roleGlyph;
roleGlyph = function operationalRoleGlyph(value) {
  return value === "reviewer" ? "RV" : layerOneRoleGlyph(value);
};

const layerOneApi = api;
function operationalMutationPath(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  return !["GET", "HEAD"].includes(method)
    && (path.startsWith("/api/launch/") || path.startsWith("/api/run/"));
}

api = async function operationalApi(path, options = {}) {
  const mutation = operationalMutationPath(path, options);
  if (mutation) {
    // LaunchSettings issues a fresh process-local token on every server
    // start.  Refresh it immediately before a command so an already-open
    // tab cannot submit the token captured before a restart.
    const config = await layerOneApi("/api/config");
    const launchToken = config?.launchToken;
    if (typeof launchToken !== "string" || !launchToken) {
      throw new Error("Operational control token is unavailable");
    }
    state.config = config;
  }
  const token = state.config?.launchToken;
  const headers = { ...(options.headers || {}) };
  if (token && (path.startsWith("/api/launch/") || path.startsWith("/api/run/"))) {
    headers["X-Control-Center-Token"] = token;
  }
  return layerOneApi(path, { ...options, headers });
};

function operationalRenderRunControl(value = operationalRunControlState) {
  const container = $("#runControl");
  const button = $("#runControlButton");
  const regenerationButton = $("#regenerateProductButton");
  const message = $("#runControlMessage");
  if (!container || !button || !message) return;
  const selected = state.runs.find((run) => run.id === state.selectedRunId);
  const action = value?.action;
  const available = Boolean(
    state.config?.commandsEnabled
    && selected
    && !selected.placeholder
    && ["pause", "resume"].includes(action),
  );
  const regenerationAvailable = Boolean(
    regenerationButton
    && state.config?.commandsEnabled
    && selected
    && !selected.placeholder
    && (value?.canRegenerateProduct || value?.productRegenerationPending),
  );
  const regenerationPending = Boolean(value?.productRegenerationPending);
  container.hidden = !(available || regenerationAvailable);
  if (available) {
    button.dataset.action = action;
    button.hidden = false;
    button.textContent = operationalRunControlInFlight
      ? (action === "pause" ? "Pausing…" : "Resuming…")
      : (action === "pause" ? "Pause run" : "Resume run");
    button.disabled = operationalRunControlInFlight || operationalProductRegenerationInFlight;
  } else {
    button.hidden = true;
    button.disabled = true;
  }
  if (regenerationButton) {
    regenerationButton.hidden = !regenerationAvailable;
    regenerationButton.disabled = regenerationPending || operationalProductRegenerationInFlight || operationalRunControlInFlight;
    regenerationButton.textContent = regenerationPending
      ? "Regeneration pending…"
      : operationalProductRegenerationInFlight
      ? "Requesting…"
      : "Regenerate dashboard";
  }
  if (available) {
    message.textContent = value.message || "Durable progress and graph history are preserved.";
  } else if (regenerationAvailable) {
    message.textContent = value.productRegenerationMessage || "Request one Product Agent dashboard regeneration from accepted business outputs.";
  } else {
    message.textContent = "Run control is unavailable for this lifecycle state.";
  }
}

async function operationalRefreshRunControl(runId = state.selectedRunId, generation = state.selectionGeneration) {
  if (!state.config?.commandsEnabled || !runId) {
    operationalRunControlState = null;
    operationalRenderRunControl();
    return;
  }
  const selected = state.runs.find((run) => run.id === runId);
  if (!selected || selected.placeholder) {
    operationalRunControlState = null;
    operationalRenderRunControl();
    return;
  }
  try {
    const value = await api(`/api/run/status?run_id=${encodeURIComponent(runId)}`);
    if (!selectionRequestIsCurrent(runId, generation)) return;
    operationalRunControlState = value;
  } catch (_error) {
    if (!selectionRequestIsCurrent(runId, generation)) return;
    operationalRunControlState = null;
  }
  operationalRenderRunControl();
}

async function operationalApplyRunControl() {
  const action = operationalRunControlState?.action;
  const runId = state.selectedRunId;
  if (operationalRunControlInFlight || !runId || !["pause", "resume"].includes(action)) return;
  const confirmation = action === "pause"
    ? "Pause this run? All committed progress and the recorded graph will be preserved. An unfinished in-flight attempt may retry after resume."
    : "Resume this run from its durable Coordinator checkpoint?";
  if (!window.confirm(confirmation)) return;
  operationalRunControlInFlight = true;
  operationalRenderRunControl();
  try {
    operationalRunControlState = await api(`/api/run/${action}`, {
      method: "POST",
      body: JSON.stringify({ runId, confirmed: true }),
    });
    await operationalRefreshRuns();
    await selectRun(runId);
  } catch (error) {
    const message = error?.message || String(error);
    operationalRunControlState = { ...operationalRunControlState, message };
  } finally {
    operationalRunControlInFlight = false;
    operationalRenderRunControl();
  }
}

async function operationalApplyProductRegeneration() {
  const value = operationalRunControlState;
  const runId = state.selectedRunId;
  if (operationalProductRegenerationInFlight || operationalRunControlInFlight || !runId || !value?.canRegenerateProduct || value?.productRegenerationPending) return;
  if (!window.confirm("Request one Product Agent dashboard regeneration from accepted business outputs? The existing candidate and review remain preserved.")) return;
  // The server derives this key from the durable Coordinator projection and
  // binds it to the current accepted Product revision/spec.  Never generate
  // a browser-local timestamp/random key: a refresh or double-click must
  // address the same one-shot request.
  const idempotencyKey = value.productRegenerationIdempotencyKey;
  operationalProductRegenerationInFlight = true;
  operationalRenderRunControl();
  try {
    operationalRunControlState = await api("/api/run/regenerate-product", {
      method: "POST",
      body: JSON.stringify({
        runId,
        confirmed: true,
        ...(typeof idempotencyKey === "string" && idempotencyKey ? { idempotencyKey } : {}),
        reason: "operator requested Product dashboard regeneration",
      }),
    });
    await operationalRefreshRuns();
    await selectRun(runId);
  } catch (error) {
    const message = error?.message || String(error);
    operationalRunControlState = { ...operationalRunControlState, message };
  } finally {
    operationalProductRegenerationInFlight = false;
    operationalRenderRunControl();
  }
}

function operationalFileKey(file) {
  const name = String(file.name || "unnamed-file");
  const relativePath = file.webkitRelativePath || name;
  return `${relativePath}:${file.size}:${file.lastModified}`;
}

function operationalCapacityForTotal(value) {
  const total = Math.max(1, Number(value) || 1);
  const analyticalOwner = Math.max(1, Math.ceil(total / 8));
  const specialist = Math.min(analyticalOwner * 3, Math.floor((total * 3) / 8));
  const entityResolution = Math.max(0, total - analyticalOwner - specialist);
  return { total, entityResolution, analyticalOwner, specialist };
}

capacityForTotal = operationalCapacityForTotal;

addFiles = function operationalAddFiles(fileList) {
  Array.from(fileList || []).forEach((file) => {
    const name = String(file.name || "unnamed-file");
    const extension = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    const key = operationalFileKey(file);
    operationalFiles.set(key, file);
    const descriptor = {
      key,
      name,
      relativePath: file.webkitRelativePath || name,
      size: file.size,
      type: file.type || extension || "application/octet-stream",
      // Local admission is based on the regular-file/path safety checks in
      // the launch boundary, not a browser extension allowlist.  Keep this
      // flag for the base application contract, but every selected file is
      // eligible for staging here (including extensionless/unknown files).
      valid: true,
      uploadState: "selected",
    };
    if (!state.files.some((current) => current.key === key)) state.files.push(descriptor);
  });
  operationalResetPreparedDraft();
  renderFileManifest();
  updatePreflight();
};

renderFileManifest = function operationalRenderFileManifest() {
  const manifest = $("#fileManifest");
  manifest.replaceChildren();
  state.files.forEach((file, index) => {
    const row = element("div", `file-row upload-${file.uploadState || "selected"}`);
    row.dataset.sourceIndex = String(index);
    if (file.validationError) row.classList.add("has-error");
    const suffix = file.name.includes(".") ? file.name.split(".").pop() : "file";
    row.append(element("span", "file-type", suffix));
    const copy = element("span");
    const status = file.validationError
      ? file.validationError
      : file.uploadState === "uploaded"
        ? "staged and hash-bound"
        : file.uploadState === "uploading"
          ? "staging…"
          : file.uploadState === "failed"
            ? "staging failed"
            : "selected locally";
    copy.append(
      element("strong", "", file.relativePath || file.name),
      element("small", "", `${formatBytes(file.size)} · ${status}`),
    );
    const remove = element("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.addEventListener("click", () => {
      operationalFiles.delete(file.key);
      state.files.splice(index, 1);
      operationalResetPreparedDraft();
      renderFileManifest();
      updatePreflight();
    });
    row.append(copy, remove);
    manifest.append(row);
  });
};

launchPayload = function operationalLaunchPayload() {
  const mode = $("input[name='mode']:checked").value;
  const intakeBlocks = $$("textarea[name='intake-block']")
    .map((input) => input.value)
    .filter((value) => value.trim().length > 0);
  const sources = state.files.map((file) => ({
    kind: "upload",
    uploadId: file.uploadId || null,
    name: file.name,
    relativePath: file.relativePath || file.name,
    size: file.size,
    sha256: file.sha256 || null,
  }));
  const sourcePath = $("#sourcePath").value.trim();
  if (sourcePath) sources.push({ kind: "local_path", path: sourcePath });
  const sourceUrl = $("#sourceUrl").value.trim();
  if (sourceUrl) sources.push({ kind: "remote_url", url: sourceUrl });
  return {
    idempotencyKey: operationalPreparationIdentity(),
    mode,
    projectName: $("#projectName").value.trim(),
    runId: $("#existingRun").value,
    intakeBlocks,
    sources,
    maxAgents: Number($("#maxAgents").value),
    capacity: currentCapacity(),
  };
};

function operationalResetPreparedDraft() {
  operationalForgetPreparationIdentity();
  if (!operationalDraft) return;
  operationalDraft = null;
  const button = $("#validateDraft");
  if (button) button.textContent = "Prepare launch";
  const confirmation = $("#launchConfirmation");
  if (confirmation) confirmation.remove();
}

function operationalValidationDetails(errors = {}) {
  return Object.entries(errors || {})
    .map(([key, value]) => `${key}: ${value}`)
    .filter((value) => !value.endsWith(": undefined") && !value.endsWith(": null"));
}

async function operationalUploadFile(descriptor) {
  if (descriptor.uploadId || !descriptor.valid) return;
  const file = operationalFiles.get(descriptor.key);
  if (!file) throw new Error(`Local file handle expired: ${descriptor.relativePath || descriptor.name}`);
  descriptor.uploadState = "uploading";
  renderFileManifest();
  const params = new URLSearchParams({
    filename: descriptor.name,
    relative_path: descriptor.relativePath || descriptor.name,
  });
  let result;
  try {
    result = await api(`/api/launch/upload?${params}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
  } catch (error) {
    descriptor.uploadState = "failed";
    renderFileManifest();
    throw error;
  }
  descriptor.uploadId = result.uploadId;
  descriptor.sha256 = result.sha256;
  descriptor.relativePath = result.relativePath;
  descriptor.uploadState = "uploaded";
  renderFileManifest();
}

async function operationalUploadSelectedFiles() {
  const pending = state.files.filter((file) => file.valid && !file.uploadId);
  for (let offset = 0; offset < pending.length; offset += 3) {
    await Promise.all(pending.slice(offset, offset + 3).map(operationalUploadFile));
  }
}

function operationalShowErrors(errors = {}) {
  state.files.forEach((file) => { delete file.validationError; });
  const details = operationalValidationDetails(errors);
  const sourcePath = $("#sourcePath").value.trim();
  const sourceUrl = $("#sourceUrl").value.trim();
  const localPathIndex = state.files.length;
  const remoteUrlIndex = localPathIndex + (sourcePath ? 1 : 0);
  if (errors.projectName) showFieldError("#projectNameField", errors.projectName);
  if (errors.runId) showFieldError("#existingRunField", errors.runId);
  if (errors.intakeBlocks) showFieldError(".requirement-field", errors.intakeBlocks);
  if (errors.sourceUrl) showFieldError("#sourceUrlField", errors.sourceUrl);
  Object.entries(errors).forEach(([key, value]) => {
    const match = /^sources\[(\d+)\]$/.exec(key);
    if (!match) return;
    const index = Number(match[1]);
    if (state.files[index]) state.files[index].validationError = String(value);
    else if (sourcePath && index === localPathIndex) showFieldError("#sourcePathField", value);
    else if (sourceUrl && index === remoteUrlIndex) showFieldError("#sourceUrlField", value);
  });
  renderFileManifest();
  if (errors.maxAgents) {
    const output = $("#validationResult");
    output.hidden = false;
    output.textContent = errors.maxAgents;
  }
  if (details.length) {
    const output = $("#validationResult");
    output.hidden = false;
    output.className = "validation-result is-invalid";
    output.textContent = details.join("; ");
  }
  return details.join("; ");
}

function operationalConfirmationCard(result) {
  $("#launchConfirmation")?.remove();
  const card = element("div", "launch-confirmation");
  card.id = "launchConfirmation";
  const heading = element("div", "launch-confirmation-heading");
  heading.append(element("span", "confirmation-glyph", "✓"));
  const copy = element("div");
  const continuation = $("input[name='mode']:checked")?.value === "continue";
  copy.append(
    element("strong", "", "Launch package is ready"),
    element(
      "small",
      "",
      continuation
        ? "Sources create a new immutable data revision; the active attempt is never rewritten."
        : "Review the immutable fingerprint, then start the Foundry Supervisor.",
    ),
  );
  heading.append(copy);
  const facts = element("dl", "confirmation-facts");
  const entries = [
    ["Run", result.runId],
    ["Input blocks", String(result.summary?.inputBlocks ?? 0)],
    ["Sources", String(result.summary?.sources ?? 0)],
    ["Capacity", `${result.effectiveCapacity?.total ?? "—"} workers`],
  ];
  if (continuation && result.dataRevision?.revisionId) {
    entries.push(["Current data", `${result.dataRevision.revisionId} · immutable`]);
  }
  entries.forEach(([label, value]) => {
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", value));
    facts.append(row);
  });
  const fingerprint = element("code", "launch-fingerprint", result.fingerprint || "fingerprint unavailable");
  const cancel = element("button", "text-button", "Cancel preparation");
  cancel.type = "button";
  cancel.addEventListener("click", () => operationalCancelPreparation().catch((error) => {
    operationalRenderStatus({ status: "failed", message: error.message || String(error) });
  }));
  card.append(heading, facts, fingerprint, cancel);
  $("#validateDraft").before(card);
}

async function operationalCancelPreparation() {
  if (!operationalDraft) return;
  const result = await api("/api/launch/cancel", {
    method: "POST",
    body: JSON.stringify({
      draftId: operationalDraft.draftId,
      fingerprint: operationalDraft.fingerprint,
      confirmed: true,
    }),
  });
  operationalRenderStatus(result);
  operationalResetPreparedDraft();
  await operationalRefreshRuns();
}

async function operationalPrepare() {
  await operationalUploadSelectedFiles();
  const result = await api("/api/launch/prepare", {
    method: "POST",
    body: JSON.stringify(launchPayload()),
  });
  const details = operationalShowErrors(result.errors);
  if (!result.valid || !result.prepared) {
    const error = new Error(details || result.message || "Launch package needs attention.");
    if (result.errors) error.errors = result.errors;
    throw error;
  }
  operationalDraft = result;
  operationalConfirmationCard(result);
  return result;
}

async function operationalExecute() {
  if (!operationalDraft) throw new Error("Prepare the launch package first.");
  return api("/api/launch/execute", {
    method: "POST",
    body: JSON.stringify({
      draftId: operationalDraft.draftId,
      fingerprint: operationalDraft.fingerprint,
      confirmed: true,
    }),
  });
}

function operationalRenderStatus(result) {
  const output = $("#validationResult");
  output.hidden = false;
  // The operational API only promotes a launch to ``running`` after the
  // current-checkout Supervisor writes its hash-bound readiness receipt.  Be
  // defensive at the browser boundary as well: a legacy/partial payload must
  // remain visibly starting rather than claiming a live run.
  const requestedStatus = result.status || "starting";
  const status = requestedStatus === "running" && result.ready !== true ? "starting" : requestedStatus;
  output.className = `validation-result launch-status status-${status}`;
  const message = result.message || (
    status === "starting"
      ? result.startupTimedOut === true
        ? "Foundry Supervisor is still starting; its live child was retained while readiness is pending."
        : "Preparing the run before the Foundry Supervisor starts."
      : `Run ${status}.`
  );
  const pending = result.pendingDataRefresh === true || status === "queued";
  const revisionId = result.dataRevisionId || result.dataRevision?.revisionId;
  output.textContent = pending && revisionId
    ? `${message} Data revision ${revisionId} is pending the next safe scheduler boundary.`
    : message;
}

function operationalPlaceholderIsOpen(run) {
  return Boolean(run?.placeholder) && !["failed", "completed", "cancelled"].includes(String(run.status || "").toLowerCase());
}

function operationalRunForStatus(result, runs) {
  return runs.find((run) => (
    (result.runRoot && (run.runRoot === result.runRoot || run.authoritativeRunRoot === result.runRoot))
    || (result.runId && run.authoritativeRunId === result.runId)
    || (result.runId && run.id === result.runId)
  ));
}

async function operationalRefreshRuns() {
  if (operationalRunsRefreshInFlight) return;
  operationalRunsRefreshInFlight = true;
  try {
    const response = await api("/api/runs");
    const nextRuns = Array.isArray(response.runs) ? response.runs : state.runs;
    const previous = state.runs.find((run) => run.id === state.selectedRunId);
    state.runs = nextRuns;
    renderRunMenu();
    renderRunsTable();
    populateExistingRuns();
    const selected = state.runs.find((run) => run.id === state.selectedRunId);
    if (!selected) return;
    // A placeholder and its durable successor share the same path-derived id.
    // Reload the snapshot once the run_state projection wins the merge.
    if (previous?.placeholder && !selected.placeholder) {
      await selectRun(selected.id);
      return;
    }
    if (state.snapshot && snapshotOwnershipIsCurrent(state.selectedRunId, state.selectionGeneration)) {
      state.snapshot.run = selected;
      renderRunContext();
      renderMission();
    }
  } catch (error) {
    // The base application owns connection/error presentation.  A transient
    // refresh failure must not erase an already-rendered durable list.
  } finally {
    operationalRunsRefreshInFlight = false;
    operationalMaybeStopRunsRefresh();
  }
}

function operationalRefreshIsNeeded() {
  const selected = state.runs.find((run) => run.id === state.selectedRunId);
  // Keep a stale tab attached to a selected placeholder until its durable
  // successor replaces it, even when the placeholder has already failed.
  // Otherwise terminal intake failures stop the refresh loop before the
  // replacement run can become selectable/control-ready.
  return operationalLaunchRequestPending
    || Boolean(selected?.placeholder)
    || state.runs.some(operationalPlaceholderIsOpen);
}

function operationalMaybeStopRunsRefresh() {
  if (operationalRefreshIsNeeded()) return;
  if (operationalRunsTimer) window.clearInterval(operationalRunsTimer);
  operationalRunsTimer = null;
}

function operationalStartRunsRefresh() {
  if (operationalRunsTimer) window.clearInterval(operationalRunsTimer);
  operationalRunsTimer = window.setInterval(() => {
    // Keep the Runs view live while intake is materialising.  The launch
    // status poll also calls this immediately, so a reload does not depend on
    // the in-memory draft object.
    if (operationalRefreshIsNeeded()) operationalRefreshRuns();
    else operationalMaybeStopRunsRefresh();
  }, 2500);
  operationalRefreshRuns();
}

function operationalBeginPendingLaunchRefresh() {
  operationalLaunchRequestPending = true;
  operationalStartRunsRefresh();
  if (operationalPendingLaunchTimer) window.clearInterval(operationalPendingLaunchTimer);
  // The status file is written synchronously before intake planning begins;
  // poll independently of the execute response so the first placeholder can
  // enter Runs while that request is still awaiting its response.
  operationalPendingLaunchTimer = window.setInterval(() => {
    if (operationalLaunchRequestPending) operationalRefreshRuns();
  }, 250);
}

async function operationalEndPendingLaunchRefresh() {
  operationalLaunchRequestPending = false;
  if (operationalPendingLaunchTimer) window.clearInterval(operationalPendingLaunchTimer);
  operationalPendingLaunchTimer = null;
  await operationalRefreshRuns();
}

async function operationalPollLaunchStatus(draftId) {
  if (operationalStatusPollInFlight) return;
  if (operationalStatusTimer) window.clearTimeout(operationalStatusTimer);
  operationalStatusTimer = null;
  const generation = ++operationalStatusPollGeneration;
  let terminal = false;
  const poll = async () => {
    if (generation !== operationalStatusPollGeneration || operationalStatusPollInFlight) return;
    operationalStatusPollInFlight = true;
    try {
      const result = await api(`/api/launch/status?draft_id=${encodeURIComponent(draftId)}`);
      operationalRenderStatus(result);
      await operationalRefreshRuns();
      if (["completed", "failed", "cancelled"].includes(result.status)) {
        terminal = true;
      }
      const target = operationalRunForStatus(result, state.runs);
      if (target && operationalAttachedRunId !== target.id) {
        await selectRun(target.id);
        operationalAttachedRunId = target.id;
        window.location.hash = "mission";
      }
      if (!operationalLaunchRequestPending && !state.runs.some(operationalPlaceholderIsOpen)) {
        terminal = true;
      }
    } catch (error) {
      operationalRenderStatus({ status: "unknown", message: error.message || String(error) });
    } finally {
      operationalStatusPollInFlight = false;
      if (generation === operationalStatusPollGeneration && !terminal) {
        operationalStatusTimer = window.setTimeout(poll, 3000);
      } else if (generation === operationalStatusPollGeneration) {
        operationalStatusTimer = null;
      }
    }
  };
  await poll();
}

validateDraft = async function operationalSubmit(event) {
  event.preventDefault();
  clearFieldErrors();
  const output = $("#validationResult");
  const button = $("#validateDraft");
  button.disabled = true;
  try {
    if (!operationalDraft) {
      button.textContent = "Preparing…";
      const result = await operationalPrepare();
      output.hidden = false;
      output.className = "validation-result is-valid";
      output.textContent = result.message;
      button.textContent = "Start run";
      button.classList.add("is-armed");
    } else {
      button.textContent = "Starting…";
      operationalBeginPendingLaunchRefresh();
      let result;
      try {
        result = await operationalExecute();
      } finally {
        await operationalEndPendingLaunchRefresh();
      }
      operationalRenderStatus(result);
      button.textContent = "Run requested";
      button.classList.remove("is-armed");
      button.disabled = true;
      await operationalPollLaunchStatus(operationalDraft.draftId);
    }
  } catch (error) {
    output.hidden = false;
    output.className = "validation-result is-invalid";
    if (error.errors) operationalShowErrors(error.errors);
    output.textContent = error.message || String(error);
    if (!operationalDraft) button.textContent = "Prepare launch";
    else button.textContent = "Start run";
    button.disabled = false;
  } finally {
    if (!button.textContent.includes("requested")) button.disabled = false;
  }
};

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    operationalStartRunsRefresh();
  }, { once: true });
}

const layerOneGraphPositions = graphPositions;
graphPositions = function operationalGraphPositions(nodes) {
  const positions = layerOneGraphPositions(nodes);
  operationalGraphWidth = state.graphLayout?.width || 1200;
  operationalGraphHeight = state.graphLayout?.height || 700;
  return positions;
};

function operationalResetNodeCacheForRun(runId = state.selectedRunId) {
  const normalizedRunId = runId === undefined || runId === null ? "" : String(runId);
  if (operationalCacheRunId === normalizedRunId) return;
  operationalNodeCache.clear();
  operationalCacheRunId = normalizedRunId;
}

const layerOneSelectRun = selectRun;
selectRun = async function operationalSelectRun(runId) {
  // Clear synchronously before the base selector awaits the new snapshot, so
  // no previous run's cached agents can be reintroduced on the next frame.
  operationalResetNodeCacheForRun(runId);
  // Async selectors execute their state reset before the first await.  Start
  // the existing refresh loop immediately after that reset so a slow snapshot
  // cannot leave a selected terminal placeholder stale.
  const selection = layerOneSelectRun(runId);
  const selected = state.runs.find((run) => run.id === state.selectedRunId);
  if (selected?.placeholder && !operationalRunsTimer) {
    // A terminal placeholder may have stopped the initial refresh loop before
    // the operator selected it.  Restart that same loop so its durable
    // successor can replace it without requiring a page reload.
    operationalStartRunsRefresh();
  }
  const result = await selection;
  if (snapshotOwnershipIsCurrent(runId, state.selectionGeneration)) {
    operationalLastSnapshotRefreshAt = Date.now();
  }
  await operationalRefreshRunControl(runId, state.selectionGeneration);
  return result;
};

function operationalVisibleNodes(nodes) {
  if (!snapshotOwnershipIsCurrent(state.selectedRunId, state.selectionGeneration)) {
    operationalNodeCache.clear();
    return [];
  }
  operationalResetNodeCacheForRun(state.selectedRunId);
  const now = Date.now();
  nodes.forEach((node) => operationalNodeCache.set(node.id, { node, lastSeen: now }));
  const currentIds = new Set(nodes.map((node) => node.id));
  for (const id of operationalNodeCache.keys()) {
    if (!currentIds.has(id)) operationalNodeCache.delete(id);
  }
  return Array.from(operationalNodeCache.values())
    .map(({ node, lastSeen }) => ({ ...node, stale: now - lastSeen > 7000 }));
}

const layerOneRenderGraph = renderGraph;
renderGraph = function operationalRenderGraph() {
  if (!snapshotOwnershipIsCurrent(state.selectedRunId, state.selectionGeneration)) {
    operationalNodeCache.clear();
    renderNeutralGraph();
    return;
  }
  const allNodes = state.snapshot.nodes || [];
  const visibleNodes = operationalVisibleNodes(allNodes);
  state.snapshot.nodes = visibleNodes;
  layerOneRenderGraph();
  state.snapshot.nodes = allNodes;
  const visiblePositions = graphPositions(visibleNodes);
  const renderedEdges = (state.snapshot.edges || []).filter(
    (edge) => visiblePositions.has(edge.source) && visiblePositions.has(edge.target),
  );
  $$("#edgeLayer path.edge").forEach((path, index) => {
    const edge = renderedEdges[index];
    path.classList.toggle("is-review", edge?.kind === "review" || edge?.relation === "reviews");
  });
  const stage = $("#graphStage");
  if (stage) {
    stage.style.width = `${operationalGraphWidth}px`;
    stage.style.height = `${operationalGraphHeight}px`;
  }
  $$(".lane-label", stage).forEach((label) => label.remove());
  const depthLabels = Array.from(new Set(visibleNodes.map((node) => visiblePositions.get(node.id)?.depth).filter((depth) => Number.isInteger(depth)))).sort((a, b) => a - b);
  depthLabels.forEach((depth) => {
    const column = state.graphColumns?.find((item) => item.depth === depth);
    const stageNumber = Number.isInteger(column?.logicalDepth) ? column.logicalDepth + 1 : depth + 1;
    const label = column?.continuation ? `STAGE ${stageNumber} · CONT.` : `STAGE ${stageNumber}`;
    const marker = element("span", "lane-label", label);
    marker.style.left = `${42 + depth * 286}px`;
    marker.style.top = `${operationalGraphHeight - 34}px`;
    stage.append(marker);
  });
  visibleNodes.forEach((node) => {
    const card = $(`.agent-node[aria-label^="${CSS.escape(node.label)},"]`);
    if (!card) return;
    card.dataset.nodeId = node.id;
    card.classList.toggle("is-derived", node.source === "durable_projection");
    card.classList.toggle("is-stale", Boolean(node.stale));
  });
};

fitGraph = function operationalFitGraph() {
  const viewport = $("#graphViewport");
  if (!viewport?.offsetWidth) return;
  const scale = Math.min(
    0.92,
    (viewport.offsetWidth - 36) / operationalGraphWidth,
    (viewport.offsetHeight - 28) / operationalGraphHeight,
  );
  state.graph.scale = Math.max(0.42, scale);
  state.graph.x = Math.max(8, (viewport.offsetWidth - operationalGraphWidth * state.graph.scale) / 2);
  state.graph.y = Math.max(6, (viewport.offsetHeight - operationalGraphHeight * state.graph.scale) / 2);
  applyGraphTransform();
};

const layerOnePollEvents = pollEvents;
pollEvents = async function operationalPollEvents() {
  if (operationalEventsPollInFlight) return;
  operationalEventsPollInFlight = true;
  const requestedRunId = state.selectedRunId;
  const requestedGeneration = state.selectionGeneration;
  try {
    const priorCursor = String(state.eventCursor ?? "0");
    const priorStream = String(state.eventStream || "");
    await layerOnePollEvents();
    if (!snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration) || !state.selectedRunId || state.feedPaused) return;
    const cursorChanged = String(state.eventCursor ?? "0") !== priorCursor;
    const streamChanged = String(state.eventStream || "") !== priorStream;
    const refreshDue = Date.now() - operationalLastSnapshotRefreshAt >= 10000;
    if (!cursorChanged && !streamChanged && !refreshDue) return;
    const fresh = await api(`/api/snapshot?run_id=${encodeURIComponent(requestedRunId)}`);
    if (!snapshotOwnershipIsCurrent(requestedRunId, requestedGeneration)) return;
    const priorEvents = state.snapshot?.events || [];
    const eventMap = new Map(priorEvents.map((event) => [event.id, event]));
    (fresh.events || []).forEach((event) => eventMap.set(event.id, event));
    fresh.events = Array.from(eventMap.values()).slice(-600);
    if (!commitSnapshot(fresh, requestedRunId, requestedGeneration)) return;
    operationalLastSnapshotRefreshAt = Date.now();
    renderMission();
    await operationalRefreshRunControl(requestedRunId, requestedGeneration);
  } catch (_error) {
    // The base poller already exposes connection state; retain the last safe snapshot.
  } finally {
    operationalEventsPollInFlight = false;
  }
};

const layerOneRenderLimitations = renderLimitations;
renderLimitations = function operationalRenderLimitations() {
  const limitations = state.snapshot?.limitations;
  if (!Array.isArray(limitations)) {
    layerOneRenderLimitations();
    return;
  }
  state.snapshot.limitations = limitations.map((value) => {
    if (value !== "Launch controls validate a draft only; command execution is disabled in Layer 1.") return value;
    return state.config?.commandsEnabled
      ? "Launch requires a separate fingerprint-bound confirmation; the fixture itself never invokes the runtime."
      : "Launch execution is locked on this server; preparation remains non-mutating.";
  });
  layerOneRenderLimitations();
  state.snapshot.limitations = limitations;
};

const layerOneRenderRunContext = renderRunContext;
renderRunContext = function operationalRenderRunContext() {
  layerOneRenderRunContext();
  const run = state.snapshot?.run || {};
  const meta = $("#selectedRunMeta");
  if (!meta) return;
  const stage = run.placeholder && run.observedStage ? ` · ${humanStatus(run.observedStage)}` : "";
  const source = run.source === "fixture" ? "Deterministic fixture" : "Filesystem projection";
  const base = `${source} · ${run.requirementCount || 0} requirements${stage}`;
  const revision = run.dataRevision;
  const pending = run.pendingDataRefresh;
  if (pending?.revisionId) {
    meta.textContent = `${base} · ${pending.revisionId} pending next safe scheduler boundary`;
  } else if (revision?.revisionId) {
    meta.textContent = `${base} · ${revision.revisionId} active`;
  } else {
    meta.textContent = base;
  }
};

const layerOneRenderRunsTable = renderRunsTable;
renderRunsTable = function operationalRenderRunsTable() {
  layerOneRenderRunsTable();
  $$("#runsTableBody tr").forEach((row) => {
    const runId = row.querySelector(".run-cell small")?.textContent;
    const run = state.runs.find((candidate) => candidate.id === runId);
    const mode = row.children[4];
    if (!run || !mode) return;
    const pending = run.pendingDataRefresh;
    const revision = pending?.revisionId || run.dataRevision?.revisionId;
    if (!revision) return;
    mode.textContent = pending?.revisionId
      ? `D ${revision} · pending next safe scheduler boundary`
      : `D ${revision} · active`;
  });
};

async function operationalConfigure() {
  const config = await api("/api/config");
  state.config = config;
  const slider = $("#maxAgents");
  const maximum = Number(config.maxAgents) || 64;
  slider.max = String(maximum);
  if (Number(slider.value) > maximum) slider.value = String(maximum);
  const labels = $$(".range-labels span");
  if (labels[1]) labels[1].textContent = `${maximum} workers`;
  const pill = $(".readonly-pill");
  if (pill) pill.textContent = config.commandsEnabled ? "OBSERVE + LAUNCH" : "LAUNCH LOCKED";
  const capacityNote = $(".capacity-card .form-card-heading p");
  if (capacityNote) capacityNote.textContent = "Planner excluded · concurrency ceiling, subject to host capacity.";
  renderCapacityBreakdown(currentCapacity());
  updatePreflight();
  await operationalRefreshRunControl();
}

document.addEventListener("DOMContentLoaded", () => {
  const form = $("#launchForm");
  form.addEventListener("input", (event) => {
    if (!event.target.closest("#validateDraft")) operationalResetPreparedDraft();
  }, true);
  form.addEventListener("change", operationalResetPreparedDraft, true);
  $("#runControlButton")?.addEventListener("click", operationalApplyRunControl);
  $("#regenerateProductButton")?.addEventListener("click", operationalApplyProductRegeneration);
  operationalConfigure().catch((error) => operationalRenderStatus({ status: "unknown", message: error.message }));
});
