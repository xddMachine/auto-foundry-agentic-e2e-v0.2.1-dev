"use strict";

const assert = require("node:assert/strict");
const {
  reviewedDashboardUrl,
  renderDashboardLink,
} = require("../../control_center_operational/static/app.js");

function linkElement() {
  const attributes = new Map();
  const classes = new Set(["primary-button", "is-disabled"]);
  return {
    href: "",
    classList: {
      add(value) { classes.add(value); },
      remove(value) { classes.delete(value); },
      contains(value) { return classes.has(value); },
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    removeAttribute(name) { attributes.delete(name); },
    getAttribute(name) { return attributes.get(name) ?? null; },
  };
}

const validSnapshot = {
  // A stale legacy field must not be consulted by the operational shell.
  run: { dashboardUrl: "/stale-dashboard/index.html" },
  productDashboard: { valid: true, dashboardUrl: "/api/product/dashboard/RUN-SIDECAR/index.html" },
};
const validLink = linkElement();
assert.equal(reviewedDashboardUrl(validSnapshot), "/api/product/dashboard/RUN-SIDECAR/index.html");
renderDashboardLink(validSnapshot, validLink);
assert.equal(validLink.href, "/api/product/dashboard/RUN-SIDECAR/index.html");
assert.equal(validLink.classList.contains("is-disabled"), false);
assert.equal(validLink.getAttribute("aria-disabled"), null);

for (const snapshot of [
  {
    run: { dashboardUrl: "/stale-dashboard/index.html" },
    productDashboard: { valid: false, dashboardUrl: "/tampered-dashboard/index.html" },
  },
  {
    run: { dashboardUrl: "/stale-dashboard/index.html" },
    productDashboard: { valid: true },
  },
]) {
  const invalidLink = linkElement();
  assert.equal(reviewedDashboardUrl(snapshot), null);
  renderDashboardLink(snapshot, invalidLink);
  assert.equal(invalidLink.getAttribute("href"), null, "invalid product sidecars must not create a link");
  assert.equal(invalidLink.classList.contains("is-disabled"), true);
  assert.equal(invalidLink.getAttribute("aria-disabled"), "true");
}

process.stdout.write("reviewed dashboard link contract: OK\n");
