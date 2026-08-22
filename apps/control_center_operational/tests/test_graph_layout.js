"use strict";

const assert = require("node:assert/strict");
const { state, graphPositions } = require("../../control_center/static/app.js");

const nodes = [{ id: "planner", role: "planner", label: "Planner" }];
const edges = [];
for (let index = 1; index <= 8; index += 1) {
  const ownerId = `owner-${index}`;
  const reviewerId = `reviewer-${index}`;
  const finalId = `final-${index}`;
  const subjectId = `domain-${String(index).padStart(2, "0")}`;
  nodes.push(
    { id: ownerId, role: "entity_resolution_owner", label: `Domain ${index}`, subjectId },
    { id: reviewerId, role: "identity_reviewer", label: `Domain ${index} Reviewer`, subjectId },
    { id: finalId, role: index === 3 ? "entity_resolution_owner" : "identity_reviewer", label: `Domain ${index} Final`, subjectId },
  );
  edges.push(
    { source: "planner", target: ownerId, kind: "dispatch" },
    { source: ownerId, target: reviewerId, kind: "review" },
    { source: reviewerId, target: finalId, kind: index === 3 ? "repair" : "commit" },
  );
}

state.snapshot = { nodes, edges };
const positions = graphPositions(nodes);
const countsByColumn = new Map();
positions.forEach((position) => countsByColumn.set(position.depth, (countsByColumn.get(position.depth) || 0) + 1));
assert.ok([...countsByColumn.values()].every((count) => count <= 7), "a visual column may contain at most seven cards");

const ownerColumns = Array.from({ length: 8 }, (_, index) => positions.get(`owner-${index + 1}`).depth);
const reviewerColumns = Array.from({ length: 8 }, (_, index) => positions.get(`reviewer-${index + 1}`).depth);
assert.ok(Math.min(...reviewerColumns) > Math.max(...ownerColumns), "reviewer stage must start to the right of every owner overflow column");
for (let index = 1; index <= 8; index += 1) {
  assert.ok(positions.get(`reviewer-${index}`).x > positions.get(`owner-${index}`).x, "each reviewer must be right of its domain owner");
  assert.equal(positions.get(`reviewer-${index}`).y, positions.get(`owner-${index}`).y, "each domain workflow must stay on one horizontal row");
  assert.ok(positions.get(`final-${index}`).x > positions.get(`reviewer-${index}`).x, "each final identity step must be right of its reviewer");
  assert.equal(positions.get(`final-${index}`).y, positions.get(`owner-${index}`).y, "review, repair, and commit must preserve the domain row");
}
assert.deepEqual([...countsByColumn.values()], [1, 7, 1, 7, 1, 7, 1]);
assert.equal(state.graphColumns[2].continuation, true);
assert.equal(state.graphColumns[4].continuation, true);
assert.equal(state.graphColumns[6].continuation, true);
process.stdout.write("bounded staged graph layout: OK\n");
