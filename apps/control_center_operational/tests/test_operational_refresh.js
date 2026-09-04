"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticRoot = path.resolve(__dirname, "..", "static");
const clearedIntervals = [];
let nextIntervalId = 0;
const context = {
  console,
  document: { addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; } },
  window: {
    clearInterval(id) { clearedIntervals.push(id); },
    clearTimeout() {},
    setInterval() { nextIntervalId += 1; return nextIntervalId; },
    setTimeout() { return 1; },
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(staticRoot, "app.js"), "utf8"), context, { filename: "app.js" });
// The production wrapper is exercised with a narrow base selector stub; this
// keeps the regression focused on refresh ownership rather than the snapshot
// rendering DOM.
vm.runInContext(
  "selectRun = function testBaseSelectRun(runId) { state.selectedRunId = runId; state.selectionGeneration += 1; return new Promise((resolve) => { __resolveBaseSelection = () => resolve({ run: { id: runId } }); }); }",
  context,
);
vm.runInContext(fs.readFileSync(path.join(staticRoot, "operational.js"), "utf8"), context, { filename: "operational.js" });

function refreshNeeded(runs, selectedRunId) {
  context.__testRuns = runs;
  context.__testSelectedRunId = selectedRunId;
  return vm.runInContext(
    "state.runs = __testRuns; state.selectedRunId = __testSelectedRunId; operationalRefreshIsNeeded()",
    context,
  );
}

async function run() {
  assert.equal(
    refreshNeeded([{ id: "run-stale", placeholder: true, status: "failed" }], "run-stale"),
    true,
    "a selected failed placeholder must keep the run refresh alive",
  );
  assert.equal(
    refreshNeeded([{ id: "run-stale", placeholder: false, status: "paused" }], "run-stale"),
    false,
    "refresh must stop after the durable successor replaces the placeholder",
  );

  context.__testRefreshCalls = 0;
  vm.runInContext(
    "operationalRefreshRuns = async function testRefreshRuns() { __testRefreshCalls += 1; }",
    context,
  );
  vm.runInContext(
    "state.runs = [{ id: 'run-stale', placeholder: true, status: 'failed' }]; state.selectedRunId = null; operationalRunsTimer = null;",
    context,
  );
  const pendingSelection = vm.runInContext("selectRun('run-stale')", context);
  assert.equal(context.__testRefreshCalls, 1, "selecting a failed placeholder must restart the existing refresh loop");
  assert.equal(vm.runInContext("operationalRunsTimer !== null", context), true);
  vm.runInContext("__resolveBaseSelection()", context);
  await pendingSelection;

  vm.runInContext(
    "state.runs = [{ id: 'run-stale', placeholder: false, status: 'paused' }]; operationalMaybeStopRunsRefresh()",
    context,
  );
  assert.equal(clearedIntervals.length, 1, "durable replacement must stop the restarted refresh loop");
  assert.equal(vm.runInContext("operationalRunsTimer === null", context), true);
}

run().then(() => {
  process.stdout.write("operational placeholder refresh: OK\n");
}).catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
