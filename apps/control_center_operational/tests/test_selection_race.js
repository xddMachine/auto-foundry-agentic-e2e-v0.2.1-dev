"use strict";

const assert = require("node:assert/strict");
const {
  state,
  selectRun,
  pollEvents,
  snapshotOwnershipIsCurrent,
  commitSnapshot,
} = require("../../control_center/static/app.js");

function deferred() {
  let resolve;
  const promise = new Promise((complete) => { resolve = complete; });
  return { promise, resolve };
}

function domElement(className = "") {
  const element = {
    className,
    hidden: false,
    textContent: "",
    children: [],
    parentNode: null,
    append(...nodes) {
      nodes.filter(Boolean).forEach((node) => {
        node.parentNode = element;
        element.children.push(node);
      });
    },
    replaceChildren(...nodes) {
      element.children = nodes.filter(Boolean);
      element.children.forEach((node) => { node.parentNode = element; });
    },
    remove() {
      element.removed = true;
      if (element.parentNode) element.parentNode.children = element.parentNode.children.filter((node) => node !== element);
    },
    querySelector(selector) {
      if (selector === "defs") return element.children.find((node) => node.tagName === "defs") || null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector !== ".lane-label") return [];
      return element.children.filter((node) => node.className === "lane-label");
    },
  };
  return element;
}

function installGraphDom() {
  const nodeLayer = domElement();
  const edgeLayer = domElement();
  const definitions = domElement();
  definitions.tagName = "defs";
  edgeLayer.replaceChildren(definitions);
  const graphStage = domElement();
  const staleLane = domElement("lane-label");
  graphStage.append(staleLane);
  const graphEmpty = domElement();
  const graphDataQuality = domElement();
  const elements = new Map([
    ["#nodeLayer", nodeLayer],
    ["#edgeLayer", edgeLayer],
    ["#graphStage", graphStage],
    ["#graphEmpty", graphEmpty],
    ["#graphDataQuality", graphDataQuality],
  ]);
  const priorDocument = global.document;
  global.document = { querySelector: (selector) => elements.get(selector) || null };
  return { priorDocument, staleLane };
}

async function run() {
  const oldSnapshot = { runId: "run-a", events: [{ id: "old-event" }] };
  state.selectedRunId = "run-a";
  state.selectionGeneration = 1;
  state.snapshot = oldSnapshot;
  state.snapshotRunId = "run-a";
  state.snapshotGeneration = 1;
  state.eventCursor = "old-cursor";
  state.eventStream = "old-stream";
  state.selectedNodeId = "old-node";
  state.recentEventNodes = new Set(["old-node"]);
  state.feedPaused = false;

  const runBResponse = deferred();
  const runCResponse = deferred();
  const eventResponse = deferred();
  const runDResponse = deferred();
  const requests = [];
  const responses = [runBResponse.promise, runCResponse.promise, eventResponse.promise, runDResponse.promise];
  const priorFetch = global.fetch;
  const { priorDocument, staleLane } = installGraphDom();
  global.fetch = (url) => {
    requests.push(String(url));
    const response = responses.shift();
    if (!response) throw new Error(`unexpected fetch ${url}`);
    return response;
  };

  try {
    const runBSelection = selectRun("run-b");
    assert.equal(state.selectedRunId, "run-b");
    assert.equal(state.snapshot, null);
    assert.equal(state.snapshotRunId, null);
    assert.equal(state.snapshotGeneration, null);
    assert.equal(state.selectedNodeId, null);
    assert.deepEqual([...state.recentEventNodes], []);
    assert.equal(staleLane.removed, true, "neutral selection graph must remove stale operational lane labels");

    await pollEvents();
    assert.equal(requests.length, 1, "pollEvents must not request events without an owned snapshot");
    assert.equal(state.snapshot, null);
    assert.equal(state.eventCursor, "0");
    assert.equal(state.eventStream, "");

    const runCSelection = selectRun("run-c");
    assert.equal(state.selectedRunId, "run-c");
    assert.equal(state.snapshot, null);
    assert.equal(state.snapshotRunId, null);
    assert.equal(state.snapshotGeneration, null);

    runBResponse.resolve({
      ok: true,
      json: async () => ({ run: { id: "run-b" }, events: [{ id: "stale" }] }),
    });
    await runBSelection;
    assert.equal(state.snapshot, null, "stale run-b response must not commit after run-c selection");
    assert.equal(state.snapshotRunId, null);
    assert.equal(state.snapshotGeneration, null);
    assert.equal(snapshotOwnershipIsCurrent("run-c", state.selectionGeneration), false);

    // Exercise the production poller with an owned run-c snapshot, then switch
    // runs while its event response is deferred.  The stale response must not
    // mutate cursor, events, or snapshot after selectRun invalidates ownership.
    assert.equal(commitSnapshot({ run: { id: "run-c" }, events: [] }, "run-c", state.selectionGeneration), true);
    state.eventCursor = "c-cursor";
    state.eventStream = "c-stream";
    const runCPoll = pollEvents();
    const runDSelection = selectRun("run-d");
    eventResponse.resolve({
      ok: true,
      json: async () => ({ streamId: "stale-stream", nextCursor: "stale-cursor", events: [{ id: "stale-event" }] }),
    });
    await runCPoll;
    assert.equal(state.snapshot, null, "stale poll response must not commit after run-d selection");
    assert.equal(state.snapshotRunId, null);
    assert.equal(state.snapshotGeneration, null);
    assert.equal(state.eventCursor, "0");
    assert.equal(state.eventStream, "");
    assert.equal(requests.length, 4);

    // Keep current run-d unresolved to avoid browser-only rendering; both
    // stale production paths have already completed their assertions.
    void runCSelection;
    void runDSelection;
  } finally {
    global.fetch = priorFetch;
    global.document = priorDocument;
  }
}

run().then(() => {
  process.stdout.write("production selection race: OK\n");
}).catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
