/* THEMIS pages. Plain JavaScript, no dependencies, no build step.
 *
 * One rule above the rest: anything the model or the project wrote reaches the page as
 * text (textContent), never as markup. The SQL under review can carry text addressed at
 * whatever reads it, and innerHTML would be the easiest way in.
 *
 * Every write carries X-Themis-UI: a header a cross-site form cannot add.
 */
(function () {
  "use strict";

  var UI_HEADER = { "X-Themis-UI": "1" };
  var SVG_NS = "http://www.w3.org/2000/svg";
  var SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"];

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function icon(name, cls) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon " + (cls || ""));
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function initials(name) {
    var parts = String(name || "?").replace(/[._]/g, " ").split(/\s+/).filter(Boolean);
    return (parts.slice(0, 2).map(function (p) { return p[0]; }).join("") || "?").toUpperCase();
  }

  function isTyping(target) {
    if (!target) return false;
    var tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
  }

  /* ---- toasts ------------------------------------------------------------------------ */

  function toast(text, bad) {
    var host = $("[data-toasts]");
    if (!host) return;
    var node = el("div", "toast" + (bad ? " bad" : ""));
    node.appendChild(icon(bad ? "alert" : "check-circle", "sm"));
    node.appendChild(el("span", "", text));
    host.appendChild(node);
    setTimeout(function () { node.remove(); }, bad ? 6000 : 3200);
  }

  /* ---- theme --------------------------------------------------------------------------- */

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    $$("[data-theme-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-set") === theme));
    });
  }

  function setTheme(theme) {
    applyTheme(theme);
    try { window.localStorage.setItem("themis-theme", theme); } catch (e) { /* not stored */ }
  }

  applyTheme(document.documentElement.getAttribute("data-theme") || "dark");
  $$("[data-theme-set]").forEach(function (b) {
    b.addEventListener("click", function () { setTheme(b.getAttribute("data-theme-set")); });
  });

  /* ---- navigation drawer (phones) ------------------------------------------------------ */

  var app = $("[data-app]");
  function closeNav() { if (app) app.classList.remove("nav-open"); }
  $$("[data-open-nav]").forEach(function (b) {
    b.addEventListener("click", function () { if (app) app.classList.add("nav-open"); });
  });
  $$("[data-close-nav]").forEach(function (b) { b.addEventListener("click", closeNav); });

  /* ---- overlays ------------------------------------------------------------------------ */

  var lastFocus = null;
  function openOverlay(node, focusTarget) {
    lastFocus = document.activeElement;
    node.classList.add("open");
    if (focusTarget) setTimeout(function () { focusTarget.focus(); }, 10);
  }
  function closeOverlay(node) {
    node.classList.remove("open");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  $$(".overlay").forEach(function (node) {
    node.addEventListener("mousedown", function (event) { if (event.target === node) closeOverlay(node); });
  });

  /* ---- model status -------------------------------------------------------------------- */

  function refreshStatus() {
    var pills = $$("[data-model-status]");
    if (!pills.length) return;
    fetch("/ui/api/status", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (s) {
        var state = "down";
        var label = "Model offline";
        var title = "The model host did not answer. The pages work; the assistant does not.";
        if (s && s.reachable && s.pulled) {
          state = "ready"; label = s.model; title = s.model + " is loaded and answering.";
        } else if (s && s.reachable) {
          state = "missing"; label = s.model + " not pulled";
          title = "The model host answers, but " + s.model + " is not pulled on it.";
        }
        pills.forEach(function (p) {
          p.setAttribute("data-state", state);
          p.title = title;
          var l = $("[data-model-label]", p);
          if (l) l.textContent = label;
        });
      });
  }
  refreshStatus();
  setInterval(refreshStatus, 60000);

  /* ---- command palette ----------------------------------------------------------------- */

  var palette = $("[data-palette]");
  var paletteInput = $("[data-palette-input]");
  var paletteResults = $("[data-palette-results]");
  var paletteIndex = null;
  var paletteItems = [];
  var paletteCursor = 0;

  var PAGES = [
    { kind: "page", label: "Overview", href: "/ui", icon: "grid" },
    { kind: "page", label: "Pull requests", href: "/ui/prs", icon: "pr" },
    { kind: "page", label: "Decision record", href: "/ui/decisions", icon: "shield" },
    { kind: "theme", label: "Switch to light theme", theme: "light", icon: "sun" },
    { kind: "theme", label: "Switch to dark theme", theme: "dark", icon: "moon" }
  ];

  function loadIndex() {
    if (paletteIndex) return Promise.resolve(paletteIndex);
    return fetch("/ui/api/prs", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : []; })
      .catch(function () { return []; })
      .then(function (rows) { paletteIndex = rows; return rows; });
  }

  function renderPalette() {
    var q = paletteInput.value.trim().toLowerCase();
    var theme = document.documentElement.getAttribute("data-theme");
    var pages = PAGES.filter(function (p) {
      if (p.kind === "theme" && p.theme === theme) return false;
      return !q || p.label.toLowerCase().indexOf(q) !== -1;
    });
    var prs = (paletteIndex || []).filter(function (r) {
      if (!q) return true;
      var hay = ((r.number ? "#" + r.number + " " : "") + r.title + " " + (r.author || "")).toLowerCase();
      return q.split(/\s+/).every(function (w) { return hay.indexOf(w.replace(/^#/, "")) !== -1 || hay.indexOf(w) !== -1; });
    }).slice(0, 8);

    paletteItems = [];
    paletteResults.textContent = "";
    prs.forEach(function (r) {
      var a = el("a", "palette-item");
      a.href = "/ui/pr/" + encodeURIComponent(r.key);
      a.setAttribute("role", "option");
      a.appendChild(el("span", "dot " + r.verdict));
      a.appendChild(el("span", "pr-no", r.number ? "#" + r.number : r.key.slice(0, 8)));
      a.appendChild(el("span", "t", r.title.replace(/`/g, "")));
      a.appendChild(el("span", "pill verdict-" + r.verdict + " verdict", r.label));
      paletteItems.push(a);
    });
    pages.forEach(function (p) {
      var a = el("a", "palette-item");
      a.href = p.href || "#";
      a.setAttribute("role", "option");
      a.appendChild(icon(p.icon, "sm"));
      a.appendChild(el("span", "t", p.label));
      a.appendChild(el("span", "faint small", p.kind === "theme" ? "Theme" : "Page"));
      if (p.kind === "theme") {
        a.addEventListener("click", function (event) {
          event.preventDefault(); setTheme(p.theme); closeOverlay(palette);
        });
      }
      paletteItems.push(a);
    });
    if (!paletteItems.length) {
      paletteResults.appendChild(el("div", "palette-empty", "Nothing matches “" + paletteInput.value.trim() + "”."));
      return;
    }
    paletteItems.forEach(function (item) { paletteResults.appendChild(item); });
    paletteCursor = 0;
    markCursor();
  }

  function markCursor() {
    paletteItems.forEach(function (item, i) {
      item.setAttribute("aria-selected", String(i === paletteCursor));
      if (i === paletteCursor) item.scrollIntoView({ block: "nearest" });
    });
  }

  function openPalette() {
    if (!palette) return;
    paletteInput.value = "";
    openOverlay(palette, paletteInput);
    renderPalette();
    loadIndex().then(renderPalette);
  }

  if (palette) {
    $$("[data-open-palette]").forEach(function (b) { b.addEventListener("click", openPalette); });
    paletteInput.addEventListener("input", renderPalette);
    paletteInput.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        paletteCursor = Math.min(paletteCursor + 1, paletteItems.length - 1); markCursor();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        paletteCursor = Math.max(paletteCursor - 1, 0); markCursor();
      } else if (event.key === "Enter" && paletteItems[paletteCursor]) {
        event.preventDefault();
        paletteItems[paletteCursor].click();
      }
    });
  }

  /* ---- keyboard ------------------------------------------------------------------------ */

  var shortcuts = $("[data-shortcuts]");
  if (shortcuts) {
    $("[data-close-dialog]", shortcuts).addEventListener("click", function () { closeOverlay(shortcuts); });
  }

  var goPending = false;
  var goTimer = null;
  var GO = { o: "/ui", p: "/ui/prs", d: "/ui/decisions" };

  function stepFinding(direction) {
    var cards = $$(".finding").filter(function (c) { return !c.hidden; });
    if (!cards.length) return;
    var current = cards.indexOf(document.activeElement && document.activeElement.closest ? document.activeElement.closest(".finding") : null);
    var next = cards[Math.min(Math.max(current + direction, 0), cards.length - 1)];
    if (current === -1) next = cards[direction > 0 ? 0 : cards.length - 1];
    showFindingsTab();
    var head = $(".finding-head", next);
    head.focus();
    next.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  document.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && (event.key === "k" || event.key === "K")) {
      event.preventDefault();
      if (palette && palette.classList.contains("open")) closeOverlay(palette); else openPalette();
      return;
    }
    if (event.key === "Escape") {
      var open = $(".overlay.open");
      if (open) closeOverlay(open);
      closeNav();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target) || $(".overlay.open")) return;

    if (goPending) {
      goPending = false;
      clearTimeout(goTimer);
      if (GO[event.key]) { event.preventDefault(); window.location.href = GO[event.key]; }
      return;
    }
    switch (event.key) {
      case "/": event.preventDefault(); openPalette(); break;
      case "?": event.preventDefault(); if (shortcuts) openOverlay(shortcuts); break;
      case "g": goPending = true; goTimer = setTimeout(function () { goPending = false; }, 1200); break;
      case "j": event.preventDefault(); stepFinding(1); break;
      case "k": event.preventDefault(); stepFinding(-1); break;
      case "o": {
        var card = document.activeElement && document.activeElement.closest ? document.activeElement.closest(".finding") : null;
        if (card) { event.preventDefault(); setExpanded(card, !card.classList.contains("expanded")); }
        break;
      }
      case "c": {
        var box = $("[data-chat-input]");
        if (box && !box.disabled) { event.preventDefault(); box.focus(); }
        break;
      }
      case "t": {
        var now = document.documentElement.getAttribute("data-theme");
        setTheme(now === "light" ? "dark" : "light");
        break;
      }
      default: break;
    }
  });

  /* ---- who is deciding (demo identity) ------------------------------------------------- */

  var nameDialog = $("[data-name-dialog]");
  var pendingName = null;

  function currentUser() { return document.body.getAttribute("data-user") || ""; }
  function trustedIdentity() { return document.body.getAttribute("data-identity") === "trusted"; }

  function askName() {
    return new Promise(function (resolve) {
      if (!nameDialog) { resolve(null); return; }
      pendingName = resolve;
      var input = $("[data-name-input]", nameDialog);
      input.value = currentUser();
      openOverlay(nameDialog, input);
    });
  }

  if (nameDialog) {
    $("[data-close-dialog]", nameDialog).addEventListener("click", function () {
      closeOverlay(nameDialog);
      if (pendingName) { pendingName(null); pendingName = null; }
    });
    $("[data-name-form]", nameDialog).addEventListener("submit", function (event) {
      event.preventDefault();
      var name = $("[data-name-input]", nameDialog).value.trim();
      if (!name) return;
      fetch("/ui/whoami", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, UI_HEADER),
        body: JSON.stringify({ name: name })
      }).then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      }).then(function (data) {
        document.body.setAttribute("data-user", data.name);
        $$("[data-me-name]").forEach(function (n) { n.textContent = data.name; });
        $$("[data-me-avatar]").forEach(function (n) { n.textContent = initials(data.name); });
        closeOverlay(nameDialog);
        toast("Decisions will be recorded as " + data.name);
        if (pendingName) { pendingName(data.name); pendingName = null; }
      }).catch(function () { toast("Could not save that name.", true); });
    });
  }
  $$("[data-set-name]").forEach(function (b) { b.addEventListener("click", askName); });

  /* ---- tabs ---------------------------------------------------------------------------- */

  $$("[role=tablist]").forEach(function (list) {
    var tabs = $$("[role=tab]", list);
    function select(tab) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { select(tab); });
      tab.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
        if (event.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
        if (next) { event.preventDefault(); select(next); next.focus(); }
      });
    });
    list.selectTab = select;
  });

  function showFindingsTab() {
    var tab = document.getElementById("tab-findings");
    if (tab && tab.parentNode.selectTab) tab.parentNode.selectTab(tab);
  }

  /* ---- findings: accordion and filters -------------------------------------------------- */

  function setExpanded(card, on) {
    card.classList.toggle("expanded", on);
    var head = $(".finding-head", card);
    if (head) head.setAttribute("aria-expanded", String(on));
  }

  $$(".finding").forEach(function (card) {
    $(".finding-head", card).addEventListener("click", function () {
      setExpanded(card, !card.classList.contains("expanded"));
    });
  });

  var expandAll = $("[data-expand-all]");
  if (expandAll) {
    expandAll.addEventListener("click", function () {
      var cards = $$(".finding").filter(function (c) { return !c.hidden; });
      var open = !cards.every(function (c) { return c.classList.contains("expanded"); });
      cards.forEach(function (c) { setExpanded(c, open); });
      expandAll.lastChild.textContent = open ? " Collapse all" : " Expand all";
    });
  }

  var findingFilter = "all";
  function applyFindingFilter() {
    var measuredOnly = $("[data-measured-only]");
    var onlyMeasured = measuredOnly && measuredOnly.checked;
    var shown = 0;
    $$(".finding").forEach(function (card) {
      var open = card.getAttribute("data-open") === "1";
      var ok = findingFilter === "all" || (findingFilter === "open" ? open : !open);
      if (onlyMeasured && card.getAttribute("data-measured") !== "1") ok = false;
      card.hidden = !ok;
      if (ok) shown += 1;
    });
    var empty = $("[data-findings-empty]");
    if (empty) empty.hidden = shown > 0;
  }
  $$("[data-finding-filter]").forEach(function (b) {
    b.addEventListener("click", function () {
      findingFilter = b.getAttribute("data-finding-filter");
      $$("[data-finding-filter]").forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
      applyFindingFilter();
    });
  });
  var measuredToggle = $("[data-measured-only]");
  if (measuredToggle) measuredToggle.addEventListener("change", applyFindingFilter);

  function jumpTo(id) {
    var card = document.getElementById("finding-" + id);
    if (!card) return;
    showFindingsTab();
    if (card.hidden) {
      findingFilter = "all";
      $$("[data-finding-filter]").forEach(function (o) { o.setAttribute("aria-pressed", String(o.getAttribute("data-finding-filter") === "all")); });
      if (measuredToggle) measuredToggle.checked = false;
      applyFindingFilter();
    }
    setExpanded(card, true);
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
  }
  $$("[data-jump]").forEach(function (a) {
    a.addEventListener("click", function (event) { event.preventDefault(); jumpTo(a.getAttribute("data-jump")); });
  });
  if (/^#finding-\d+$/.test(window.location.hash)) jumpTo(window.location.hash.slice(9));

  /* ---- decisions ----------------------------------------------------------------------- */

  function decisionPill(value) { return el("span", "pill d-" + (value || "open"), value || "open"); }

  /* The page's own recount after a decision, so the gate card is not stale until a reload.
   * The same rule as views.verdict_for; the server's answer replaces it on the next load. */
  function refreshGate() {
    var cards = $$(".finding");
    if (!cards.length) return;
    var threshold = document.body.getAttribute("data-threshold") || "high";
    var limit = SEVERITY_ORDER.indexOf(threshold);
    var open = cards.filter(function (c) { return c.getAttribute("data-open") === "1"; });
    var blocking = open.filter(function (c) { return SEVERITY_ORDER.indexOf(c.getAttribute("data-sev")) <= limit; });
    var key, label, reason;
    if (blocking.length) {
      var worst = blocking.map(function (c) { return c.getAttribute("data-sev"); })
        .sort(function (a, b) { return SEVERITY_ORDER.indexOf(a) - SEVERITY_ORDER.indexOf(b); })[0];
      key = "blocking"; label = "Blocking";
      reason = blocking.length + " open finding" + (blocking.length !== 1 ? "s" : "") + " at or above " + threshold + ", the worst " + worst;
    } else if (open.length) {
      key = "review"; label = "Needs review";
      reason = open.length + " open finding(s) below the " + threshold + " threshold";
    } else {
      key = "clear"; label = "Settled"; reason = "every finding has a decision";
    }
    var pill = $("[data-verdict]");
    if (pill) {
      pill.className = "pill verdict verdict-" + key;
      pill.textContent = "";
      pill.appendChild(icon(key === "blocking" ? "octagon" : key === "review" ? "clock" : "check-circle", "xs"));
      pill.appendChild(document.createTextNode(" " + label));
      pill.title = reason;
    }
    var reasonNode = $("[data-verdict-reason]");
    if (reasonNode) reasonNode.textContent = reason;
    var decided = cards.length - open.length;
    var progressLabel = $("[data-progress-label]");
    if (progressLabel) progressLabel.textContent = decided + " of " + cards.length;
    var fill = $("[data-progress] .meter-fill");
    if (fill) fill.setAttribute("width", String(100 * decided / cards.length));
    var openCount = $("[data-open-count]");
    if (openCount) openCount.textContent = String(open.length);
    var decidedCount = $("[data-decided-count]");
    if (decidedCount) decidedCount.textContent = String(decided);
  }

  function addEventToFeed(card, data) {
    var feed = $("[data-event-feed]");
    if (!feed) return;
    var empty = $("[data-event-empty]");
    if (empty) empty.remove();
    var rule = $(".f-meta .rule", card);
    var model = $(".f-meta .model", card);
    var a = el("a", "feed-item");
    a.href = "#finding-" + card.getAttribute("data-finding");
    var ic = el("span", "feed-icon t-" + data.disposition);
    ic.appendChild(icon("shield", "sm"));
    a.appendChild(ic);
    var text = el("div", "feed-text");
    var line = el("div");
    line.appendChild(el("strong", "", data.actor));
    line.appendChild(document.createTextNode(" " + data.disposition + " "));
    line.appendChild(el("span", "mono", rule ? rule.textContent : ""));
    line.appendChild(document.createTextNode(" on "));
    line.appendChild(el("span", "mono", model ? model.textContent : ""));
    text.appendChild(line);
    text.appendChild(el("div", "feed-detail", data.note || $(".f-title", card).textContent));
    a.appendChild(text);
    a.appendChild(el("span", "feed-when", "just now"));
    a.addEventListener("click", function (event) { event.preventDefault(); jumpTo(card.getAttribute("data-finding")); });
    feed.insertBefore(a, feed.firstChild);
    var count = $("[data-event-count]");
    if (count) count.textContent = String(parseInt(count.textContent, 10) + 1);
  }

  function record(card, disposition, note) {
    var id = card.getAttribute("data-finding");
    return fetch("/ui/findings/" + encodeURIComponent(id) + "/decision", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, UI_HEADER),
      body: JSON.stringify({ disposition: disposition, note: note || null })
    }).then(function (r) {
      if (r.status === 401 && !trustedIdentity()) {
        return askName().then(function (name) { return name ? record(card, disposition, note) : null; });
      }
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          throw new Error(body.detail || ("the server answered " + r.status));
        });
      }
      return r.json().then(function (data) {
        var status = $("[data-status]", card);
        status.textContent = "";
        status.appendChild(decisionPill(data.disposition));
        status.appendChild(document.createTextNode(" by "));
        status.appendChild(el("strong", "", data.actor));
        status.appendChild(document.createTextNode(" just now"));

        var pillHost = $("[data-decision-pill]", card);
        if (pillHost) { pillHost.textContent = ""; pillHost.appendChild(decisionPill(data.disposition)); }

        var item = el("div", "history-item");
        item.appendChild(el("span", "avatar sm", initials(data.actor)));
        var body = el("div");
        body.appendChild(el("strong", "", data.actor));
        body.appendChild(document.createTextNode(" "));
        body.appendChild(decisionPill(data.disposition));
        body.appendChild(el("span", "", " just now"));
        if (data.note) body.appendChild(el("div", "note", "“" + data.note + "”"));
        item.appendChild(body);
        var history = $("[data-history]", card);
        history.insertBefore(item, history.firstChild);

        card.setAttribute("data-open", data.disposition === "deferred" ? "1" : "0");
        refreshGate();
        addEventToFeed(card, data);
        toast("Recorded: " + data.disposition + " by " + data.actor);
        return data;
      });
    });
  }

  $$(".finding").forEach(function (card) {
    var form = $("[data-decision-form]", card);
    var note = $("[data-note]", card);
    var chosen = null;
    var buttons = $$("[data-decide]", card);

    function closeForm() {
      form.classList.remove("open");
      buttons.forEach(function (b) { b.setAttribute("aria-pressed", "false"); });
      chosen = null;
    }

    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var go = function () {
          chosen = button.getAttribute("data-decide");
          buttons.forEach(function (b) { b.setAttribute("aria-pressed", String(b === button)); });
          form.classList.add("open");
          note.focus();
        };
        if (!trustedIdentity() && !currentUser()) {
          askName().then(function (name) { if (name) go(); });
        } else {
          go();
        }
      });
    });
    $("[data-cancel]", card).addEventListener("click", closeForm);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!chosen) return;
      var submit = $("button[type=submit]", form);
      submit.disabled = true;
      record(card, chosen, note.value.trim())
        .then(function (data) { if (data) { note.value = ""; closeForm(); applyFindingFilter(); } })
        .catch(function (err) { toast("Not recorded: " + err.message, true); })
        .then(function () { submit.disabled = false; });
    });
  });

  /* ---- copy ---------------------------------------------------------------------------- */

  function copyText(text, what) {
    if (!navigator.clipboard) { toast("Copying needs a secure (https) page.", true); return; }
    navigator.clipboard.writeText(text).then(
      function () { toast((what || "Text") + " copied"); },
      function () { toast("The browser refused the copy.", true); }
    );
  }
  $$("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var src = $("[data-copy-src]", b.parentNode);
      copyText(src ? src.textContent : "", "SQL");
    });
  });

  /* ---- tables: filter, sort, export ----------------------------------------------------- */

  var table = $("[data-table]");
  if (table) {
    var tbody = table.tBodies[0];
    var filterInput = $("[data-filter-input]");
    var group = "";
    var shownNode = $("[data-shown]");

    var applyTableFilter = function () {
      var q = filterInput ? filterInput.value.trim().toLowerCase() : "";
      var shown = 0;
      $$("tr[data-row]", tbody).forEach(function (tr) {
        var ok = (!group || tr.getAttribute("data-group") === group) &&
          (!q || q.split(/\s+/).every(function (w) { return tr.getAttribute("data-search").indexOf(w) !== -1; }));
        tr.hidden = !ok;
        if (ok) shown += 1;
      });
      var empty = $("[data-empty-row]", tbody);
      if (empty) empty.hidden = shown > 0;
      if (shownNode) shownNode.textContent = shown + " shown";
    };
    if (filterInput) filterInput.addEventListener("input", applyTableFilter);
    $$("[data-filter-group]").forEach(function (b) {
      b.addEventListener("click", function () {
        group = b.getAttribute("data-filter-group");
        $$("[data-filter-group]").forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
        applyTableFilter();
      });
    });

    $$("th[data-sort]", table).forEach(function (th) {
      th.tabIndex = 0;
      var sort = function () {
        var index = Array.prototype.indexOf.call(th.parentNode.children, th);
        var numeric = th.getAttribute("data-sort") === "num";
        var ascending = th.getAttribute("aria-sort") !== "ascending";
        $$("th[data-sort]", table).forEach(function (o) { o.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
        var rows = $$("tr[data-row]", tbody);
        rows.sort(function (a, b) {
          var x = a.children[index].getAttribute("data-value") || a.children[index].textContent.trim();
          var y = b.children[index].getAttribute("data-value") || b.children[index].textContent.trim();
          var result = numeric ? (parseFloat(x) || 0) - (parseFloat(y) || 0) : x.localeCompare(y, undefined, { sensitivity: "base" });
          return ascending ? result : -result;
        });
        var empty = $("[data-empty-row]", tbody);
        rows.forEach(function (tr) { tbody.insertBefore(tr, empty || null); });
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); sort(); } });
    });
  }

  /* A cell that starts with = + - @ is a formula to a spreadsheet. A note typed on this page
   * must never become one on the auditor's machine. */
  function csvCell(value) {
    var text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
    if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  $$("[data-export-csv]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!table) return;
      var head = $$("thead th", table).map(function (th) { return csvCell(th.textContent); }).join(",");
      var lines = [head];
      $$("tr[data-row]", table.tBodies[0]).forEach(function (tr) {
        if (tr.hidden) return;
        lines.push(Array.prototype.map.call(tr.children, function (td) {
          return csvCell(td.hasAttribute("data-csv") ? td.getAttribute("data-csv") : td.textContent);
        }).join(","));
      });
      var blob = new Blob([lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" });
      var link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = button.getAttribute("data-export-csv") + "-" + new Date().toISOString().slice(0, 10) + ".csv";
      document.body.appendChild(link);
      link.click();
      setTimeout(function () { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
      toast((lines.length - 1) + " row" + (lines.length === 2 ? "" : "s") + " exported");
    });
  });

  /* ---- the assistant ------------------------------------------------------------------- */

  var chat = $("[data-chat]");
  if (chat) {
    var runKey = chat.getAttribute("data-chat");
    var thread = $("[data-thread]", chat);
    var form = $("[data-chat-form]", chat);
    var input = $("[data-chat-input]", chat);
    var send = $("[data-send]", chat);
    var controller = null;

    var scrollDown = function () { thread.scrollTop = thread.scrollHeight; };
    var grow = function () { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; };

    var setBusy = function (busy) {
      send.textContent = "";
      send.appendChild(icon(busy ? "stop" : "send", "sm"));
      send.setAttribute("aria-label", busy ? "Stop" : "Send");
      input.disabled = busy;
    };

    var argText = function (args) {
      if (!args || typeof args !== "object") return "";
      return Object.keys(args).map(function (k) { return k + "=" + JSON.stringify(args[k]); }).join(", ");
    };

    var ask = function (question) {
      if (controller || !question) return;
      var sugg = $(".suggestions", thread);
      if (sugg) sugg.remove();

      var mine = el("div", "msg user");
      mine.appendChild(el("div", "bubble", question));
      thread.appendChild(mine);

      var msg = el("div", "msg bot");
      var avatar = el("span", "bot-avatar sm");
      avatar.appendChild(icon("sparkles"));
      msg.appendChild(avatar);
      var bubble = el("div", "bubble");
      var steps = el("div", "steps");
      var thinking = el("div", "thinking");
      var dots = el("span", "typing");
      dots.appendChild(el("i")); dots.appendChild(el("i")); dots.appendChild(el("i"));
      thinking.appendChild(dots);
      var statusText = el("span", "", "Starting");
      thinking.appendChild(statusText);
      bubble.appendChild(steps);
      bubble.appendChild(thinking);
      msg.appendChild(bubble);
      thread.appendChild(msg);
      scrollDown();

      controller = new AbortController();
      setBusy(true);
      var finished = false;

      var finish = function () { finished = true; thinking.remove(); };

      var handle = function (evt) {
        if (evt.type === "status") {
          statusText.textContent = evt.text;
        } else if (evt.type === "step") {
          var step = el("div", "tool-step" + (evt.ok ? "" : " failed"));
          step.appendChild(icon(evt.ok ? "check-circle" : "alert", "xs"));
          var body = el("div");
          body.appendChild(el("code", "", evt.tool + "(" + argText(evt.arguments) + ")"));
          if (evt.preview) body.appendChild(el("span", "preview", evt.preview));
          step.appendChild(body);
          steps.appendChild(step);
          statusText.textContent = "Reading what " + evt.tool + " returned";
        } else if (evt.type === "answer") {
          finish();
          bubble.appendChild(el("div", "answer", evt.answer));
          if (evt.citations && evt.citations.length) {
            var cites = el("div", "cites");
            evt.citations.forEach(function (c) {
              var cite = el("div", "cite");
              var head = el("div", "cite-head");
              head.appendChild(el("span", "cite-n", c.n));
              head.appendChild(el("span", "cite-tool", c.tool));
              cite.appendChild(head);
              cite.appendChild(el("div", "cite-quote", "“" + c.quote + "”"));
              cites.appendChild(cite);
            });
            bubble.appendChild(cites);
          }
          var foot = el("div", "answer-foot");
          var grounded = el("span", "grounded");
          grounded.appendChild(icon("check-circle", "xs"));
          grounded.appendChild(document.createTextNode(" Every quote checked against a tool result"));
          foot.appendChild(grounded);
          foot.appendChild(el("span", "", "· " + evt.calls + " model call" + (evt.calls === 1 ? "" : "s")));
          var copy = el("button", "btn ghost sm");
          copy.type = "button";
          copy.appendChild(icon("copy", "xs"));
          copy.appendChild(document.createTextNode(" Copy"));
          copy.addEventListener("click", function () { copyText(evt.answer, "Answer"); });
          foot.appendChild(copy);
          bubble.appendChild(foot);
        } else if (evt.type === "refusal") {
          finish();
          msg.classList.add("refusal");
          bubble.appendChild(el("div", "answer", "I can't answer that from what THEMIS knows, so I won't guess. " + evt.reason));
        } else if (evt.type === "error") {
          finish();
          msg.classList.add("error");
          bubble.appendChild(el("div", "answer", evt.text));
        }
        scrollDown();
      };

      fetch("/ui/pr/" + encodeURIComponent(runKey) + "/chat", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json", Accept: "text/event-stream" }, UI_HEADER),
        body: JSON.stringify({ question: question }),
        signal: controller.signal
      }).then(function (response) {
        if (!response.ok || !response.body) {
          return response.json().catch(function () { return {}; }).then(function (body) {
            var detail = body.detail;
            if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg; }).join("; ");
            handle({ type: "error", text: "The question was not accepted: " + (detail || response.status) });
          });
        }
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var pump = function () {
          return reader.read().then(function (chunk) {
            if (chunk.done) return;
            buffer += decoder.decode(chunk.value, { stream: true });
            var frames = buffer.split("\n\n");
            buffer = frames.pop();
            frames.forEach(function (frame) {
              frame.split("\n").forEach(function (line) {
                if (line.indexOf("data: ") !== 0) return;
                try { handle(JSON.parse(line.slice(6))); } catch (e) { /* a malformed frame is skipped */ }
              });
            });
            return pump();
          });
        };
        return pump();
      }).catch(function (err) {
        if (err && err.name === "AbortError") {
          handle({ type: "error", text: "Stopped." });
        } else {
          handle({ type: "error", text: "The connection dropped before an answer arrived." });
        }
      }).then(function () {
        if (!finished) handle({ type: "error", text: "The stream ended without an answer." });
        controller = null;
        setBusy(false);
        input.focus();
      });
    };

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (controller) { controller.abort(); return; }
      var q = input.value.trim();
      if (q.length < 3) { toast("Ask a slightly longer question.", true); return; }
      input.value = "";
      grow();
      ask(q);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });
    input.addEventListener("input", grow);
    $$("[data-suggest]", chat).forEach(function (b) {
      b.addEventListener("click", function () { ask(b.textContent.trim()); });
    });
  }
})();
