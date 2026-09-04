/* Deterministic, offline dashboard interactions. All state comes from the
 * already-rendered DOM; this file does not create or load data. */
(function () {
  "use strict";

  function text(value) {
    return String(value == null ? "" : value).toLowerCase();
  }

  function matchingDataElements(root, attribute, expected) {
    /* Attribute names are static; values are compared as strings instead of
     * interpolated into a CSS selector.  Reviewed labels may contain quotes,
     * brackets, or other selector metacharacters. */
    var elements = root.querySelectorAll("[" + attribute + "]");
    var matches = [];
    for (var i = 0; i < elements.length; i += 1) {
      if (elements[i].getAttribute(attribute) === expected) matches.push(elements[i]);
    }
    return matches;
  }

  function update(root) {
    var search = root.querySelector("[data-runtime-search]");
    var domain = root.querySelector("[data-runtime-domain]");
    var query = text(search && search.value).trim();
    var domainValue = text(domain && domain.value).trim();
    var cards = root.querySelectorAll("[data-runtime-card]");
    var visible = 0;
    for (var i = 0; i < cards.length; i += 1) {
      var card = cards[i];
      var matchesText = !query || text(card.textContent).indexOf(query) !== -1;
      var matchesDomain = !domainValue || text(card.getAttribute("data-runtime-domain")) === domainValue;
      var shown = matchesText && matchesDomain;
      card.hidden = !shown;
      if (shown) visible += 1;
    }
    var sections = root.querySelectorAll("[data-runtime-section]");
    for (var s = 0; s < sections.length; s += 1) {
      var sectionCards = sections[s].querySelectorAll("[data-runtime-card]");
      var sectionVisible = false;
      for (var c = 0; c < sectionCards.length; c += 1) {
        if (!sectionCards[c].hidden) { sectionVisible = true; break; }
      }
      sections[s].hidden = (query || domainValue) && !sectionVisible;
    }
    var count = root.querySelector("[data-runtime-count]");
    if (count) count.textContent = visible + " visible view" + (visible === 1 ? "" : "s");
  }

  function init(root) {
    var search = root.querySelector("[data-runtime-search]");
    var domain = root.querySelector("[data-runtime-domain]");
    var clear = root.querySelector("[data-runtime-clear]");
    if (search) search.addEventListener("input", function () { update(root); });
    if (domain) domain.addEventListener("change", function () { update(root); });
    if (clear) clear.addEventListener("click", function () {
      if (search) search.value = "";
      if (domain) domain.value = "";
      update(root);
    });
    var toggles = root.querySelectorAll("[data-series-toggle]");
    for (var i = 0; i < toggles.length; i += 1) {
      toggles[i].addEventListener("click", function (event) {
        var button = event.currentTarget;
        var key = button.getAttribute("data-series-toggle");
        var marks = matchingDataElements(root, "data-series-key", key);
        var hidden = !button.classList.contains("is-off");
        for (var m = 0; m < marks.length; m += 1) marks[m].hidden = hidden;
        button.classList.toggle("is-off", hidden);
        button.setAttribute("aria-pressed", hidden ? "false" : "true");
      });
    }
    var drilldowns = root.querySelectorAll("[data-runtime-drilldown]");
    for (var d = 0; d < drilldowns.length; d += 1) {
      drilldowns[d].addEventListener("click", function (event) {
        var button = event.currentTarget;
        var targetId = button.getAttribute("data-runtime-drilldown");
        var targets = targetId ? matchingDataElements(root, "data-runtime-detail", targetId) : [];
        var target = targets.length ? targets[0] : null;
        if (!target) return;
        target.hidden = !target.hidden;
        button.setAttribute("aria-expanded", target.hidden ? "false" : "true");
      });
    }
    update(root);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var roots = document.querySelectorAll("[data-dashboard-runtime]");
    for (var i = 0; i < roots.length; i += 1) init(roots[i]);
  });
}());
