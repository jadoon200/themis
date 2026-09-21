/* THEMIS pages: record a decision, stream an answer. No libraries.
 *
 * Every string that came from the model or the project is inserted with textContent,
 * never innerHTML. The SQL under review can carry text written at whatever reads it, and a
 * page that rendered it as markup would be the easiest way into this one.
 */
(function () {
  "use strict";

  var HEADERS = { "Content-Type": "application/json", "X-Themis-UI": "1" };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function post(url, body) {
    return fetch(url, { method: "POST", headers: HEADERS, body: JSON.stringify(body) });
  }

  // ---- who am I (demo only) ------------------------------------------------------------

  function askName() {
    var name = window.prompt("Record decisions under which name? (demo — sign-in replaces this)");
    if (!name || !name.trim()) return Promise.resolve(false);
    return post("/ui/whoami", { name: name.trim() }).then(function (r) {
      if (r.ok) {
        document.querySelectorAll("[data-set-name]").forEach(function (b) {
          b.textContent = name.trim();
        });
      }
      return r.ok;
    });
  }

  document.querySelectorAll("[data-set-name]").forEach(function (button) {
    button.addEventListener("click", askName);
  });

  // ---- decisions -----------------------------------------------------------------------

  function decisionPill(value) {
    return el("span", "pill decision-" + value, value);
  }

  document.querySelectorAll("[data-finding]").forEach(function (card) {
    var id = card.getAttribute("data-finding");
    var form = card.querySelector("[data-decision-form]");
    var note = card.querySelector("[data-note]");
    var status = card.querySelector("[data-status]");
    var history = card.querySelector("[data-history]");
    var list = card.querySelector("[data-history-list]");
    var chosen = null;

    card.querySelectorAll("[data-decide]").forEach(function (button) {
      button.addEventListener("click", function () {
        chosen = button.getAttribute("data-decide");
        form.classList.add("open");
        note.focus();
      });
    });
    card.querySelector("[data-cancel]").addEventListener("click", function () {
      form.classList.remove("open");
      chosen = null;
    });

    function send() {
      return post("/ui/findings/" + id + "/decision", { disposition: chosen, note: note.value });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!chosen) return;
      var confirm = card.querySelector("[data-confirm]");
      confirm.disabled = true;
      send()
        .then(function (r) {
          if (r.status === 401) {
            return askName().then(function (ok) { return ok ? send() : r; });
          }
          return r;
        })
        .then(function (r) {
          return r.json().then(function (body) { return { ok: r.ok, body: body }; });
        })
        .then(function (result) {
          confirm.disabled = false;
          if (!result.ok) {
            window.alert(result.body.detail || "The decision could not be recorded.");
            return;
          }
          var d = result.body;
          status.textContent = "";
          status.appendChild(decisionPill(d.disposition));
          status.appendChild(document.createTextNode(" by "));
          status.appendChild(el("strong", "", d.actor));
          status.appendChild(document.createTextNode(" just now" + (d.note ? " — “" + d.note + "”" : "")));
          var item = el("li");
          item.appendChild(el("strong", "", d.actor));
          item.appendChild(document.createTextNode(" — " + d.disposition + ", just now" + (d.note ? ": “" + d.note + "”" : "")));
          list.insertBefore(item, list.firstChild);
          history.hidden = false;
          var summary = history.querySelector("summary");
          var count = list.children.length;
          summary.textContent = count + " decision" + (count === 1 ? "" : "s") + " on record";
          note.value = "";
          form.classList.remove("open");
          chosen = null;
        })
        .catch(function () {
          confirm.disabled = false;
          window.alert("The decision could not be recorded.");
        });
    });
  });

  // ---- chat ----------------------------------------------------------------------------

  var panel = document.querySelector("[data-chat]");
  if (!panel) return;

  var runKey = panel.getAttribute("data-chat");
  var log = panel.querySelector("[data-chat-log]");
  var form = panel.querySelector("[data-chat-form]");
  var input = panel.querySelector("[data-chat-input]");
  var submit = form.querySelector("button[type=submit]");
  var busy = false;

  function scroll() { log.scrollTop = log.scrollHeight; }

  function citeNode(c) {
    var node = el("div", "cite");
    node.appendChild(el("span", "cite-tool", "[" + c.n + "] " + c.tool));
    node.appendChild(el("span", "cite-quote", c.quote));
    return node;
  }

  function ask(question) {
    if (busy || !question.trim()) return;
    busy = true;
    submit.disabled = true;
    input.value = "";
    var empty = log.querySelector(".chat-empty");
    if (empty) empty.remove();

    log.appendChild(el("div", "msg user", question));
    var bot = el("div", "msg bot");
    var trace = el("div", "trace");
    var working = el("div", "working");
    working.appendChild(el("span", "dot"));
    var workingText = el("span", "", "Starting");
    working.appendChild(workingText);
    bot.appendChild(trace);
    bot.appendChild(working);
    log.appendChild(bot);
    scroll();

    function finish() {
      working.remove();
      busy = false;
      submit.disabled = false;
      input.focus();
      scroll();
    }

    function handle(event) {
      if (event.type === "status") {
        workingText.textContent = event.text;
      } else if (event.type === "step") {
        var line = el("div", "trace-line");
        line.appendChild(el("span", "trace-n", event.n));
        var args = Object.keys(event.arguments || {})
          .map(function (k) { return k + "=" + event.arguments[k]; })
          .join(", ");
        var body = el("span");
        body.appendChild(el("code", "", event.tool + "(" + args + ")"));
        if (event.preview) body.appendChild(document.createTextNode(" — " + event.preview));
        line.appendChild(body);
        trace.appendChild(line);
        workingText.textContent = "Reading what came back";
      } else if (event.type === "answer") {
        bot.appendChild(el("div", "answer-text", event.answer));
        var cites = el("div", "cites");
        (event.citations || []).forEach(function (c) { cites.appendChild(citeNode(c)); });
        bot.appendChild(cites);
        finish();
      } else if (event.type === "refusal") {
        bot.classList.add("refusal");
        bot.appendChild(el("div", "answer-text", "Could not answer: " + event.reason));
        finish();
      } else if (event.type === "error") {
        bot.classList.add("error");
        bot.appendChild(el("div", "answer-text", event.text));
        finish();
      }
      scroll();
    }

    fetch("/ui/pr/" + encodeURIComponent(runKey) + "/chat", {
      method: "POST",
      headers: HEADERS,
      body: JSON.stringify({ question: question }),
    })
      .then(function (response) {
        if (!response.ok || !response.body) throw new Error("HTTP " + response.status);
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) { if (busy) finish(); return; }
            buffer += decoder.decode(chunk.value, { stream: true });
            var frames = buffer.split("\n\n");
            buffer = frames.pop();
            frames.forEach(function (frame) {
              var data = frame.split("\n").filter(function (l) { return l.indexOf("data: ") === 0; })
                .map(function (l) { return l.slice(6); }).join("");
              if (data) { try { handle(JSON.parse(data)); } catch (e) { /* ignore a torn frame */ } }
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        handle({ type: "error", text: "The chat could not be reached: " + err.message });
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    ask(input.value);
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask(input.value);
    }
  });
  log.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-suggest]");
    if (chip) ask(chip.textContent);
  });
})();
