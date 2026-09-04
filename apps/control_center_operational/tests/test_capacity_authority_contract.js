"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const staticRoot = path.resolve(__dirname, "..", "static");
const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
const operational = fs.readFileSync(path.join(staticRoot, "operational.js"), "utf8");

// The base shell may consume the shared name, but it must not define another
// role split.  Operational.js is loaded second and owns the single authority.
assert.equal(/function\s+capacityForTotal\s*\(/.test(app), false);
assert.equal(/entityResolution:\s*Math\.min\(4,\s*total\)/.test(app), false);
assert.equal(/analyticalOwner:\s*Math\.min\(1,\s*total\)/.test(app), false);
assert.equal(/specialist:\s*Math\.min\(3,\s*total\)/.test(app), false);
assert.match(operational, /function\s+operationalCapacityForTotal\s*\(/);
assert.match(operational, /capacityForTotal\s*=\s*operationalCapacityForTotal/);
assert.match(app, /capacityForTotal\(Number\(slider\.value\)\)/);

process.stdout.write("capacity authority contract: OK\n");
