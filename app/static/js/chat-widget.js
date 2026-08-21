(function () {
  "use strict";

  var script = document.currentScript;
  var presentationId = script.getAttribute("data-presentation-id");
  var chatEnabled = script.getAttribute("data-chat-enabled") === "true";
  var apiBase = script.getAttribute("data-api-base") || "";

  if (!chatEnabled || !presentationId) return;

  var SESSION_KEY = "cp_session_" + presentationId;
  var sessionId = localStorage.getItem(SESSION_KEY) || null;
  var isStreaming = false;
  // Pending follow-up questions typed while an answer is still streaming.
  // Dispatched one at a time so we never run two inference requests at once.
  var queue = [];

  // ── Build DOM ──
  var bubble = el("button", { className: "chat-bubble", title: "Ask a question" });
  bubble.innerHTML =
    '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.2L4 17.2V4h16v12z"/></svg>';

  var panel = el("div", { className: "chat-panel" });

  var header = el("div", { className: "chat-header" });
  // Title is context-aware: "this guide" when rendered inside a
  // multi-chapter reader (presence of #bookChaptersData), otherwise
  // "this page".
  var _isBookPage = !!document.getElementById("bookChaptersData");
  var _chatTitle = _isBookPage ? "Ask about this guide" : "Ask about this page";
  header.innerHTML = '<span>' + _chatTitle + '</span>';
  var clearBtn = el("button", { className: "chat-clear-btn", title: "Clear conversation" });
  clearBtn.textContent = "\u21BB";
  var closeBtn = el("button", {});
  closeBtn.textContent = "\u2715";
  header.appendChild(clearBtn);
  header.appendChild(closeBtn);

  var messages = el("div", { className: "chat-messages" });
  // Thin, muted bar above the input for queue status ("N queued\u2026").
  var statusBar = el("div", { className: "chat-status-bar" });
  statusBar.style.display = "none";
  var inputArea = el("div", { className: "chat-input-area" });
  var input = el("input", { type: "text", placeholder: "Type your question\u2026" });
  var sendBtn = el("button", {});
  sendBtn.textContent = "Send";

  inputArea.appendChild(input);
  inputArea.appendChild(sendBtn);
  panel.appendChild(header);
  panel.appendChild(messages);
  panel.appendChild(statusBar);
  panel.appendChild(inputArea);

  document.body.appendChild(bubble);
  document.body.appendChild(panel);

  // ── Events ──
  bubble.addEventListener("click", function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) input.focus();
  });
  closeBtn.addEventListener("click", function () {
    panel.classList.remove("open");
  });
  clearBtn.addEventListener("click", clearMemory);
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  function send() {
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    appendMsg("user", text);
    // Always enqueue. If nothing is in flight, processQueue() dispatches
    // immediately; otherwise it waits its turn so we never fire two
    // inference requests concurrently.
    queue.push(text);
    if (isStreaming) {
      updateQueueHint();
    } else {
      processQueue();
    }
  }

  function processQueue() {
    if (isStreaming || !queue.length) return;
    var next = queue.shift();
    updateQueueHint();
    streamAnswer(next);
  }

  function updateQueueHint() {
    var n = queue.length;
    if (n > 0) {
      statusBar.textContent =
        n + (n === 1 ? " question" : " questions") +
        " queued · sending one at a time";
      statusBar.style.display = "block";
    } else {
      statusBar.style.display = "none";
      statusBar.textContent = "";
    }
  }

  function clearMemory() {
    sessionId = null;
    localStorage.removeItem(SESSION_KEY);
    messages.innerHTML = "";
    var notice = el("div", { className: "chat-msg assistant" });
    notice.textContent = "Conversation cleared.";
    messages.appendChild(notice);
  }

  function streamAnswer(question) {
    isStreaming = true;

    // Non-obtrusive waiting indicator: a small animated line plus muted
    // fine print. Kept until the first token actually arrives (not just
    // when the response opens) so the user isn't left staring at nothing
    // during inference latency.
    var waitEl = el("div", { className: "chat-waiting" });
    var waitDots = el("div", { className: "chat-waiting-dots" });
    waitDots.textContent = "Working on it";
    var waitNote = el("div", { className: "chat-waiting-note" });
    waitNote.textContent =
      "This can take a few seconds \u2014 you can keep typing, follow-ups are queued.";
    waitEl.appendChild(waitDots);
    waitEl.appendChild(waitNote);
    messages.appendChild(waitEl);
    scrollBottom();

    function removeWait() {
      if (waitEl.parentNode) waitEl.remove();
    }

    var url = apiBase + "/api/chat/" + presentationId;
    var body = JSON.stringify({ message: question, session_id: sessionId });

    var headers = { "Content-Type": "application/json" };
    try {
      var token = localStorage.getItem("cp_token");
      if (token) headers["Authorization"] = "Bearer " + token;
    } catch (_) { /* localStorage may throw in private mode */ }

    fetch(url, {
      method: "POST",
      headers: headers,
      body: body,
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (body) {
            var msg = body.detail || "HTTP " + resp.status;
            throw new Error(msg);
          });
        }
        // Defer creating the assistant bubble until the first token so the
        // waiting indicator stays visible through inference latency.
        var assistantEl = null;
        function ensureAssistant() {
          if (!assistantEl) {
            removeWait();
            assistantEl = appendMsg("assistant", "");
          }
          return assistantEl;
        }

        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var fullText = "";
        var currentEvent = "";

        function read() {
          reader.read().then(function (result) {
            if (result.done) {
              finish();
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            var lines = buffer.split("\n");
            buffer = lines.pop();

            for (var i = 0; i < lines.length; i++) {
              var line = lines[i];

              // SSE blank line = end of event block
              if (line.trim() === "") continue;

              // Track current SSE event type
              if (line.indexOf("event:") === 0) {
                currentEvent = line.substring(6).trim();
                if (currentEvent === "done") { finish(); return; }
                continue;
              }

              if (line.indexOf("data:") === 0) {
                // Strip "data:" prefix and exactly one optional space per SSE spec
                var data = line.substring(5);
                if (data.charAt(0) === " ") data = data.substring(1);

                if (currentEvent === "session") {
                  sessionId = data.trim();
                  localStorage.setItem(SESSION_KEY, sessionId);
                  currentEvent = "";
                  continue;
                }

                // Server is throttling — the request is waiting for a free
                // inference slot. Keep the waiting indicator, just refine
                // its wording so the user knows it's queued, not stalled.
                if (currentEvent === "queued") {
                  if (!assistantEl) {
                    waitDots.textContent = "Waiting in line";
                    waitNote.textContent =
                      "The assistant is busy — your question will start as soon as a slot frees up.";
                  }
                  currentEvent = "";
                  continue;
                }

                if (currentEvent === "error") {
                  fullText = data || "Sorry, an error occurred. Please try again.";
                  ensureAssistant().innerHTML = renderInlineMd(fullText);
                  currentEvent = "";
                  continue;
                }

                if (currentEvent === "sources") {
                  try {
                    var sources = JSON.parse(data);
                    if (Array.isArray(sources) && sources.length) {
                      renderSources(ensureAssistant(), sources);
                    }
                  } catch (parseErr) {
                    // Non-JSON sources payload — ignore silently
                  }
                  currentEvent = "";
                  continue;
                }

                // chunk or start — append text (preserving spaces)
                fullText += data;
                ensureAssistant().innerHTML = renderInlineMd(fullText);
                scrollBottom();
                currentEvent = "";
              }
            }
            read();
          });
        }
        read();

        var finished = false;
        function finish() {
          if (finished) return;
          finished = true;
          removeWait();
          isStreaming = false;
          // Dispatch the next queued question, if any; otherwise refocus.
          if (queue.length) {
            processQueue();
          } else {
            input.focus();
          }
        }
      })
      .catch(function (err) {
        removeWait();
        appendMsg("assistant", err.message || "Sorry, something went wrong. Please try again.");
        isStreaming = false;
        processQueue();
      });
  }

  // ── Helpers ──
  function appendMsg(role, text) {
    var d = el("div", { className: "chat-msg " + role });
    d.innerHTML = role === "assistant" ? renderInlineMd(text) : escapeHtml(text);
    messages.appendChild(d);
    scrollBottom();
    return d;
  }

  function scrollBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  // ── Citation chips ─────────────────────────────────────────────
  // Cache chapter mapping (title → slug) read from #bookChaptersData
  // if we're on a book reader page. null when not in book mode.
  var _chapterMap = (function () {
    try {
      var node = document.getElementById("bookChaptersData");
      if (!node || !node.textContent) return null;
      var arr = JSON.parse(node.textContent);
      if (!Array.isArray(arr)) return null;
      var map = {};
      for (var i = 0; i < arr.length; i += 1) {
        var ch = arr[i];
        if (ch && ch.title && ch.slug) {
          map[ch.title.toLowerCase()] = ch.slug;
        }
      }
      return map;
    } catch (_) {
      return null;
    }
  })();
  var _bookSlug = (function () {
    var match = window.location.pathname.match(/^\/p\/([^/]+)/);
    return match ? match[1] : null;
  })();

  function renderSources(assistantEl, sources) {
    var existing = assistantEl.nextElementSibling;
    if (existing && existing.classList.contains("chat-sources")) {
      existing.remove();
    }
    var box = document.createElement("div");
    box.className = "chat-sources";
    var label = document.createElement("span");
    label.className = "chat-sources-label";
    label.textContent = "Sources";
    box.appendChild(label);

    for (var i = 0; i < sources.length; i += 1) {
      var src = sources[i];
      var name = (src && src.display_file_name) || "";
      if (!name) continue;
      var excerpt = (src && src.excerpt) || "";
      var chapterSlug = _chapterMap ? _chapterMap[name.toLowerCase()] : null;

      var chip;
      if (chapterSlug && _bookSlug) {
        chip = document.createElement("a");
        chip.href = "/p/" + encodeURIComponent(_bookSlug) +
                    "/c/" + encodeURIComponent(chapterSlug);
      } else {
        chip = document.createElement("span");
      }
      chip.className = "chat-source-chip";
      chip.textContent = name;
      if (excerpt) chip.title = excerpt;
      box.appendChild(chip);
    }

    // Insert after the assistant message bubble
    assistantEl.parentNode.insertBefore(box, assistantEl.nextSibling);
    scrollBottom();
  }

  function el(tag, attrs) {
    var e = document.createElement(tag);
    for (var k in attrs) e[k] = attrs[k];
    return e;
  }

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function renderInlineMd(text) {
    if (typeof marked !== "undefined" && marked && typeof marked.parse === "function") {
      var rawHtml;
      try {
        rawHtml = marked.parse(text, { breaks: true, gfm: true });
      } catch (_) {
        return renderInlineFallback(text);
      }
      var temp = document.createElement("div");
      temp.innerHTML = rawHtml;
      temp.querySelectorAll("script,iframe,object,embed").forEach(function (n) { n.remove(); });
      temp.querySelectorAll("*").forEach(function (n) {
        Array.from(n.attributes).forEach(function (attr) {
          if (attr.name.indexOf("on") === 0) n.removeAttribute(attr.name);
        });
        if (n.tagName === "A") {
          if (n.href && /^javascript:/i.test(n.getAttribute("href") || "")) {
            n.removeAttribute("href");
          } else if (n.getAttribute("href")) {
            n.setAttribute("target", "_blank");
            n.setAttribute("rel", "noopener");
          }
        }
      });
      return temp.innerHTML;
    }
    return renderInlineFallback(text);
  }

  function renderInlineFallback(text) {
    var s = escapeHtml(text);
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*(.+?)\*/g, "<em>$1</em>");
    s = s.replace(/`(.+?)`/g, "<code>$1</code>");
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (m, text, url) {
      if (/^https?:\/\/|^\//.test(url)) return '<a href="' + url + '" target="_blank" rel="noopener">' + text + '</a>';
      return text;
    });
    s = s.replace(/\n/g, "<br>");
    return s;
  }
})();
