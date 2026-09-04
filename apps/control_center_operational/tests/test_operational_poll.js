"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticRoot = path.resolve(__dirname, "..", "static");
const context = {
  console,
  document: {
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  },
  window: {
    clearInterval() {},
    clearTimeout() {},
    setInterval() { return 1; },
    setTimeout() { return 1; },
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(staticRoot, "app.js"), "utf8"), context, { filename: "app.js" });
// Keep the base poller deterministic.  The production operational wrapper is
// loaded below and captures this function exactly as the browser does.
vm.runInContext(
  "__baseCalls = 0; __nextBase = null; pollEvents = async function testBasePollEvents() { __baseCalls += 1; if (__nextBase) await __nextBase(); }",
  context,
);
vm.runInContext(fs.readFileSync(path.join(staticRoot, "operational.js"), "utf8"), context, { filename: "operational.js" });

function configureState() {
  vm.runInContext(
    `state.config = { commandsEnabled: false };
state.runs = [{ id: "run-a", placeholder: false, status: "paused" }];
state.selectedRunId = "run-a";
state.selectionGeneration = 1;
state.snapshot = { run: { id: "run-a" }, events: [] };
state.snapshotRunId = "run-a";
state.snapshotGeneration = 1;
state.eventCursor = "cursor-1";
state.eventStream = "stream-1";
state.feedPaused = false;
operationalLastSnapshotRefreshAt = 1000;
__now = 1000;
Date.now = () => __now;
__snapshotCalls = 0;
api = async function testSnapshotApi() {
  __snapshotCalls += 1;
  return { run: { id: "run-a" }, events: [{ id: \`fresh-\${__snapshotCalls}\` }] };
};
renderMission = function testRenderMission() {};
operationalRefreshRunControl = async function testRefreshRunControl() {};`,
    context,
  );
}

async function run() {
  configureState();

  // A second interval tick must return while the first events request is in
  // flight, and the unchanged cursor must not trigger an immediate snapshot.
  vm.runInContext(
    "__nextBase = () => new Promise((resolve) => { __resolveBase = resolve; });",
    context,
  );
  const firstPoll = vm.runInContext("pollEvents()", context);
  await Promise.resolve();
  const secondPoll = vm.runInContext("pollEvents()", context);
  await secondPoll;
  assert.equal(vm.runInContext("__baseCalls", context), 1);
  assert.equal(vm.runInContext("__snapshotCalls", context), 0);
  vm.runInContext("__resolveBase()", context);
  await firstPoll;

  // A changed event cursor refreshes the complete snapshot after the base
  // poll finishes, and the guard is released for the next interval tick.
  vm.runInContext(
    "__nextBase = async () => { state.eventCursor = 'cursor-2'; }; __now = 2000;",
    context,
  );
  await vm.runInContext("pollEvents()", context);
  assert.equal(vm.runInContext("__snapshotCalls", context), 1);

  // Unchanged events remain incremental until the bounded ten-second
  // sidecar/status refresh cadence is due.
  vm.runInContext("__nextBase = null; __now = 9000;", context);
  await vm.runInContext("pollEvents()", context);
  assert.equal(vm.runInContext("__snapshotCalls", context), 1);

  vm.runInContext("__now = 13001;", context);
  await vm.runInContext("pollEvents()", context);
  assert.equal(vm.runInContext("__snapshotCalls", context), 2);
}

run().then(() => {
  process.stdout.write("operational poll guard: OK\n");
}).catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
