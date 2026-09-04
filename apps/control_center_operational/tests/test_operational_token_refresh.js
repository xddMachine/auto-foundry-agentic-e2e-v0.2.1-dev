"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticRoot = path.resolve(__dirname, "..", "static");
const controls = new Map([
  ["#runControl", { hidden: true }],
  ["#runControlButton", { dataset: {}, disabled: false, textContent: "" }],
  ["#runControlMessage", { textContent: "" }],
]);
const context = {
  console,
  document: {
    addEventListener() {},
    querySelector(selector) { return controls.get(selector) || null; },
    querySelectorAll() { return []; },
  },
  window: {
    clearInterval() {},
    clearTimeout() {},
    setInterval() { return 1; },
    setTimeout() { return 1; },
    confirm() { return true; },
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(staticRoot, "app.js"), "utf8"), context, { filename: "app.js" });
vm.runInContext(fs.readFileSync(path.join(staticRoot, "operational.js"), "utf8"), context, { filename: "operational.js" });

function response(payload, ok = true, status = 200) {
  return { ok, status, json: async () => payload };
}

const calls = [];
let mode = "resume";
context.fetch = async (url, options = {}) => {
  const request = {
    url: String(url),
    method: String(options.method || "GET").toUpperCase(),
    headers: { ...(options.headers || {}) },
  };
  calls.push(request);
  if (mode === "resume" && request.url === "/api/config") {
    return response({ commandsEnabled: true, launchToken: "fresh-token" });
  }
  if (mode === "resume" && request.url === "/api/run/resume") {
    return response({ lifecycleStatus: "running", action: "pause" });
  }
  if (mode === "status" && request.url.startsWith("/api/run/status")) {
    return response({ lifecycleStatus: "paused", action: "resume" });
  }
  if (mode === "config-failure" && request.url === "/api/config") {
    return response({ error: "config unavailable" }, false, 503);
  }
  throw new Error(`unexpected request: ${request.method} ${request.url}`);
};

async function run() {
  vm.runInContext(
    `state.config = { commandsEnabled: true, launchToken: "stale-token" };
state.runs = [{ id: "run-a", placeholder: false, status: "paused" }];
state.selectedRunId = "run-a";
state.selectionGeneration = 1;
operationalRunControlState = { action: "resume" };
operationalRefreshRuns = async function testRefreshRuns() {};
selectRun = async function testSelectRun() {};`,
    context,
  );

  await vm.runInContext("operationalApplyRunControl()", context);
  assert.deepEqual(calls.map(({ url }) => url), ["/api/config", "/api/run/resume"]);
  assert.equal(calls.filter(({ url }) => url === "/api/run/resume").length, 1);
  assert.equal(calls[1].method, "POST");
  assert.equal(calls[1].headers["X-Control-Center-Token"], "fresh-token");
  assert.equal(vm.runInContext("state.config.launchToken", context), "fresh-token");

  // Read-only status polling keeps using the already-loaded config and does
  // not add another config fetch.
  calls.length = 0;
  mode = "status";
  await vm.runInContext("api('/api/run/status?run_id=run-a')", context);
  assert.deepEqual(calls.map(({ url }) => url), ["/api/run/status?run_id=run-a"]);

  // If the fresh config cannot be read, the stale token is never submitted.
  calls.length = 0;
  mode = "config-failure";
  vm.runInContext("state.config = { commandsEnabled: true, launchToken: 'stale-token' }", context);
  await assert.rejects(
    () => vm.runInContext("api('/api/run/resume', { method: 'POST', body: '{}' })", context),
    /config unavailable/,
  );
  assert.deepEqual(calls.map(({ url }) => url), ["/api/config"]);
}

run().then(() => {
  process.stdout.write("operational mutation token refresh: OK\n");
}).catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
