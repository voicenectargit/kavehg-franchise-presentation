/**
 * Shared dashboard logic — auth guard, list, create, edit, tags, preview
 */
(function () {
  "use strict";

  // ── Auth guard ──
  var token = localStorage.getItem("cp_token");
  var user;
  try { user = JSON.parse(localStorage.getItem("cp_user") || "null"); } catch (e) { user = null; }
  if (!token || !user) { window.location.href = "/login"; return; }

  // Populate nav user name
  var userNameEl = document.getElementById("userName");
  if (userNameEl) userNameEl.textContent = user.name || user.email;

  // API base injected via template data attribute
  var API = (document.body.dataset.api || "").replace(/\/$/, "");

  function authHeaders() {
    return { "Authorization": "Bearer " + token, "Content-Type": "application/json" };
  }

  function handleAuth(r) {
    if (r.status === 401) { logout(); throw new Error("Session expired"); }
    return r;
  }

  function esc(s) {
    var d = document.createElement("div"); d.textContent = s || "";
    return d.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Safely extract error detail from a non-OK response (handles non-JSON bodies)
  function parseErrorResponse(r, fallback) {
    return r.text().then(function (t) {
      try { var d = JSON.parse(t); return d.detail || fallback; }
      catch (e) { return fallback + " (HTTP " + r.status + ")"; }
    });
  }

  // ── Logout ──
  window.logout = function () {
    localStorage.removeItem("cp_token");
    localStorage.removeItem("cp_user");
    // Suppress cross-app SSO auto-adopt until the user manually logs in again.
    // The shared .ambivo.com cookie is still present (it belongs to all Ambivo
    // apps), so without this flag the login/root pages would immediately
    // re-adopt it and bounce the user back into the dashboard.
    localStorage.setItem("cp_sso_optout", "1");
    window.location.href = "/login";
  };

  // ══════════════════════════════════════════════════
  // Dashboard list page
  // ══════════════════════════════════════════════════
  var tbody = document.getElementById("tbody");
  if (tbody) {
    loadList();
  }

  function loadList() {
    var loadingEl = document.getElementById("loading");
    var tableEl = document.getElementById("table");
    var emptyEl = document.getElementById("empty");
    loadingEl.style.display = "";
    tableEl.style.display = "none";
    emptyEl.style.display = "none";

    fetch(API + "/api/presentations", { headers: authHeaders() })
      .then(handleAuth)
      .then(function (r) { if (!r.ok) throw new Error("Failed to load presentations"); return r.json(); })
      .then(function (list) {
        loadingEl.style.display = "none";
        if (!list.length) { emptyEl.style.display = ""; return; }
        tableEl.style.display = "";
        tbody.innerHTML = "";
        list.forEach(function (p) {
          var tr = document.createElement("tr");
          var protectedBadge = p.access_protected
            ? ' <span class="badge badge-amber">Protected</span>'
            : '';
          var typeBadge = p.content_type === "html"
            ? ' <span class="badge badge-blue">HTML</span>'
            : '';
          tr.innerHTML =
            '<td>' + esc(p.title) + protectedBadge + typeBadge + '</td>' +
            '<td><a class="link" href="/p/' + esc(p.slug) + '" target="_blank">/p/' + esc(p.slug) + '</a></td>' +
            '<td><span class="badge ' + (p.is_published ? 'badge-green' : 'badge-gray') + '">' +
              (p.is_published ? 'Published' : 'Draft') + '</span></td>' +
            '<td>' + (p.chat_enabled ? 'On' : 'Off') + '</td>' +
            '<td>' + (p.num_views || 0) + '</td>' +
            '<td><span class="link" style="cursor:pointer" data-queries="' + p.id + '">' + (p.total_chat_queries || 0) + '</span> <span style="color:#9ca3af;font-size:.8em">(' + (p.today_chat_queries || 0) + ')</span></td>' +
            '<td>' + (p.created_at ? new Date(p.created_at).toLocaleDateString() : "-") + '</td>' +
            '<td style="white-space:nowrap">' +
              '<a class="link" href="/dashboard/edit/' + p.id + '" style="margin-right:.5rem">Edit</a>' +
              '<button class="btn-danger-sm" data-toggle="' + p.id + '">Toggle</button> ' +
              '<button class="btn-danger-sm" data-delete="' + p.id + '">Delete</button>' +
            '</td>';
          tbody.appendChild(tr);
        });

        // Bind toggle / delete / queries via delegation
        tbody.onclick = function (e) {
          var tid = e.target.dataset.toggle;
          var did = e.target.dataset["delete"];
          var qid = e.target.dataset.queries;
          if (tid) togglePublish(tid);
          if (did) del(did);
          if (qid) openQueriesModal(qid);
        };
      })
      .catch(function (err) {
        loadingEl.textContent = "Error loading: " + err.message;
      });
  }

  function togglePublish(id) {
    fetch(API + "/api/presentations/" + id + "/publish", { method: "PATCH", headers: authHeaders() })
      .then(handleAuth)
      .then(function (r) { if (!r.ok) throw new Error("Failed to toggle publish status"); loadList(); })
      .catch(function (err) { alert(err.message); });
  }

  function del(id) {
    if (!confirm("Delete this presentation? The knowledge base will also be removed.")) return;
    fetch(API + "/api/presentations/" + id, { method: "DELETE", headers: authHeaders() })
      .then(handleAuth)
      .then(function (r) { if (!r.ok) throw new Error("Failed to delete presentation"); loadList(); })
      .catch(function (err) { alert(err.message); });
  }

  // ══════════════════════════════════════════════════
  // Tags input widget
  // ══════════════════════════════════════════════════
  window.initTagsInput = function (containerEl) {
    var tags = [];
    var input = containerEl.querySelector("input");

    function render() {
      // Remove old tag elements
      containerEl.querySelectorAll(".tag").forEach(function (el) { el.remove(); });
      tags.forEach(function (tag, i) {
        var span = document.createElement("span");
        span.className = "tag";
        span.innerHTML = esc(tag) + ' <span class="tag-remove" data-idx="' + i + '">&times;</span>';
        containerEl.insertBefore(span, input);
      });
    }

    function addTag(val) {
      val = val.trim().toLowerCase();
      if (val && tags.indexOf(val) === -1) {
        tags.push(val);
        render();
      }
    }

    input.addEventListener("keydown", function (e) {
      if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
        e.preventDefault();
        addTag(input.value.replace(/,/g, ""));
        input.value = "";
      }
      if (e.key === "Backspace" && !input.value && tags.length) {
        tags.pop();
        render();
      }
    });

    containerEl.addEventListener("click", function (e) {
      if (e.target.classList.contains("tag-remove")) {
        tags.splice(parseInt(e.target.dataset.idx, 10), 1);
        render();
      }
      input.focus();
    });

    return {
      getTags: function () { return tags.slice(); },
      setTags: function (arr) { tags = (arr || []).slice(); render(); }
    };
  };

  // ══════════════════════════════════════════════════
  // Content type toggle helper
  // ══════════════════════════════════════════════════
  window.initContentTypeToggle = function (btnMd, btnHtml, mdEditorEl, htmlEditorEl, hiddenInput, hintEl) {
    if (!btnMd || !btnHtml || !mdEditorEl || !htmlEditorEl || !hiddenInput) {
      return { activate: function () {} };
    }
    function activate(type) {
      hiddenInput.value = type;
      if (type === "html") {
        btnHtml.classList.add("active"); btnMd.classList.remove("active");
        mdEditorEl.style.display = "none"; htmlEditorEl.style.display = "";
      } else {
        btnMd.classList.add("active"); btnHtml.classList.remove("active");
        mdEditorEl.style.display = ""; htmlEditorEl.style.display = "none";
      }
    }
    btnMd.addEventListener("click", function () { activate("markdown"); });
    btnHtml.addEventListener("click", function () { activate("html"); });
    return { activate: activate };
  };

  // ══════════════════════════════════════════════════
  // HTML editor Code/Preview toggle
  // ══════════════════════════════════════════════════
  window.initHtmlPreview = function () {
    var btnWrite = document.getElementById("btnHtmlWrite");
    var btnPreview = document.getElementById("btnHtmlPreview");
    var textarea = document.getElementById("f_html");
    var frame = document.getElementById("htmlPreviewFrame");
    if (!btnWrite || !btnPreview || !textarea || !frame) return;

    btnWrite.addEventListener("click", function () {
      btnWrite.classList.add("active");
      btnPreview.classList.remove("active");
      textarea.style.display = "";
      frame.style.display = "none";
    });

    btnPreview.addEventListener("click", function () {
      btnPreview.classList.add("active");
      btnWrite.classList.remove("active");
      textarea.style.display = "none";
      frame.style.display = "block";
      frame.srcdoc = textarea.value || "<p style='color:#9ca3af;padding:2rem'>No HTML content to preview.</p>";
    });
  };

  // ══════════════════════════════════════════════════
  // Shared HTML detection + conversion helper
  // ══════════════════════════════════════════════════
  var HTML_RE = /^\s*<!DOCTYPE|^\s*<html|^\s*<head|^\s*<body|<\/(div|p|table|h[1-6]|ul|ol|section|article|body|html)>/i;

  function htmlToMarkdown(html) {
    if (typeof TurndownService === "undefined") return html;

    // Strip non-content elements before conversion
    html = html.replace(/<head[\s\S]*?<\/head>/gi, "");
    html = html.replace(/<style[\s\S]*?<\/style>/gi, "");
    html = html.replace(/<script[\s\S]*?<\/script>/gi, "");
    html = html.replace(/<!--[\s\S]*?-->/g, "");

    // Extract just <body> content if present
    var bodyMatch = html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
    if (bodyMatch) html = bodyMatch[1];

    // Strip remaining structural wrappers
    html = html.replace(/<!DOCTYPE[^>]*>/gi, "");
    html = html.replace(/<\/?html[^>]*>/gi, "");
    html = html.replace(/<\/?body[^>]*>/gi, "");

    var td = new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced", bulletListMarker: "-" });
    if (typeof turndownPluginGfm !== "undefined") td.use(turndownPluginGfm.gfm);
    td.addRule("images", {
      filter: "img",
      replacement: function (content, node) {
        var alt = node.getAttribute("alt") || "image";
        var src = node.getAttribute("src") || "";
        return src ? "![" + alt + "](" + src + ")" : "";
      }
    });
    return td.turndown(html).trim();
  }

  // ══════════════════════════════════════════════════
  // Markdown preview toggle
  // ══════════════════════════════════════════════════
  window.initPreview = function (editorEl, previewEl, btnWrite, btnPreview) {
    btnWrite.addEventListener("click", function () {
      btnWrite.classList.add("active");
      btnPreview.classList.remove("active");
      editorEl.style.display = "";
      previewEl.style.display = "none";
    });
    btnPreview.addEventListener("click", function () {
      btnPreview.classList.add("active");
      btnWrite.classList.remove("active");
      editorEl.style.display = "none";
      previewEl.style.display = "block";

      var content = editorEl.value || "";

      // If content looks like raw HTML, convert to markdown first
      if (HTML_RE.test(content)) {
        content = htmlToMarkdown(content);
        editorEl.value = content; // update textarea so submit sends markdown
      }

      if (typeof marked !== "undefined") {
        var rawHtml = marked.parse(content);
        // Sanitize: strip script tags and event handlers from preview
        var temp = document.createElement("div");
        temp.innerHTML = rawHtml;
        temp.querySelectorAll("script,iframe,object,embed").forEach(function(el) { el.remove(); });
        temp.querySelectorAll("*").forEach(function(el) {
          Array.from(el.attributes).forEach(function(attr) {
            if (attr.name.startsWith("on")) el.removeAttribute(attr.name);
          });
          if (el.tagName === "A" && el.href && /^javascript:/i.test(el.href)) el.removeAttribute("href");
        });
        previewEl.innerHTML = temp.innerHTML;
      } else {
        previewEl.textContent = content || "(preview unavailable — marked.js not loaded)";
      }
    });
  };

  // ══════════════════════════════════════════════════
  // HTML-to-Markdown paste handler (Turndown.js)
  // ══════════════════════════════════════════════════
  window.initPasteHandler = function (textareaEl) {
    if (typeof TurndownService === "undefined") return;

    textareaEl.addEventListener("paste", function (e) {
      var clipboardData = e.clipboardData || window.clipboardData;
      if (!clipboardData) return;

      var html = clipboardData.getData("text/html");

      // If no text/html, check if plain text looks like HTML source code
      if (!html) {
        var plain = clipboardData.getData("text/plain") || "";
        if (HTML_RE.test(plain)) {
          html = plain;
        } else {
          return; // truly plain text — let browser handle it
        }
      }

      e.preventDefault();
      var md = htmlToMarkdown(html);

      // Insert at cursor position
      var start = textareaEl.selectionStart;
      var end = textareaEl.selectionEnd;
      var before = textareaEl.value.substring(0, start);
      var after = textareaEl.value.substring(end);
      textareaEl.value = before + md + after;
      textareaEl.selectionStart = textareaEl.selectionEnd = start + md.length;
      textareaEl.dispatchEvent(new Event("input"));
    });
  };

  // ══════════════════════════════════════════════════
  // Section toggle helper
  // ══════════════════════════════════════════════════
  function initSectionToggle(checkboxEl, sectionEl) {
    if (!checkboxEl || !sectionEl) return;
    checkboxEl.addEventListener("change", function () {
      sectionEl.style.display = checkboxEl.checked ? "" : "none";
    });
  }
  // Keep old name for compatibility
  function initAccessToggle(c, s) { initSectionToggle(c, s); }

  // ══════════════════════════════════════════════════
  // Logo file preview helper
  // ══════════════════════════════════════════════════
  function initLogoFilePreview(fileInputEl, previewWrapEl, previewImgEl) {
    if (!fileInputEl || !previewWrapEl || !previewImgEl) return;
    fileInputEl.addEventListener("change", function () {
      var file = fileInputEl.files[0];
      if (!file) return;
      if (file.size > 1024 * 1024) { alert("Logo must be under 1 MB."); fileInputEl.value = ""; return; }
      var reader = new FileReader();
      reader.onload = function (e) {
        previewImgEl.src = e.target.result;
        previewWrapEl.style.display = "";
      };
      reader.readAsDataURL(file);
    });
  }

  var THEME_PRESETS = {
    modern: {primary_color:"#2563eb",secondary_color:"#4f46e5",accent_color:"#f59e0b",font_family:"Inter",dark_mode:false},
    minimal: {primary_color:"#475569",secondary_color:"#64748b",accent_color:"#94a3b8",font_family:"DM Sans",dark_mode:false},
    bold: {primary_color:"#7c3aed",secondary_color:"#ec4899",accent_color:"#f97316",font_family:"Space Grotesk",dark_mode:false},
    warm: {primary_color:"#ea580c",secondary_color:"#d97706",accent_color:"#dc2626",font_family:"Nunito",dark_mode:false},
    nature: {primary_color:"#059669",secondary_color:"#10b981",accent_color:"#0d9488",font_family:"Outfit",dark_mode:false},
    dark: {primary_color:"#06b6d4",secondary_color:"#8b5cf6",accent_color:"#f59e0b",font_family:"JetBrains Mono",dark_mode:true},
  };

  function initThemeSection() {
    var toggle = document.getElementById("f_theme_toggle");
    var section = document.getElementById("themeSection");
    if (!toggle || !section) return;
    initSectionToggle(toggle, section);

    // Color picker hex display sync
    ["primary","secondary","accent"].forEach(function(name) {
      var input = document.getElementById("f_theme_" + name);
      var hex = document.getElementById("f_theme_" + name + "_hex");
      if (input && hex) {
        input.addEventListener("input", function() { hex.textContent = input.value; });
      }
    });

    // Preset buttons
    document.querySelectorAll(".theme-preset-btn").forEach(function(btn) {
      btn.addEventListener("click", function() {
        var preset = btn.getAttribute("data-preset");
        var p = THEME_PRESETS[preset];
        if (!p) return;
        document.getElementById("f_theme_primary").value = p.primary_color;
        document.getElementById("f_theme_primary_hex").textContent = p.primary_color;
        document.getElementById("f_theme_secondary").value = p.secondary_color;
        document.getElementById("f_theme_secondary_hex").textContent = p.secondary_color;
        document.getElementById("f_theme_accent").value = p.accent_color;
        document.getElementById("f_theme_accent_hex").textContent = p.accent_color;
        document.getElementById("f_theme_font").value = p.font_family;
        document.getElementById("f_theme_dark").checked = p.dark_mode;
        // Highlight selected preset
        document.querySelectorAll(".theme-preset-btn").forEach(function(b) {
          b.style.borderColor = "#e5e7eb";
          b.style.boxShadow = "none";
        });
        btn.style.borderColor = p.primary_color;
        btn.style.boxShadow = "0 0 0 2px " + p.primary_color + "40";
        // Store preset name
        var hiddenPreset = document.getElementById("f_theme_preset_value");
        if (!hiddenPreset) {
          hiddenPreset = document.createElement("input");
          hiddenPreset.type = "hidden";
          hiddenPreset.id = "f_theme_preset_value";
          section.appendChild(hiddenPreset);
        }
        hiddenPreset.value = preset;
      });
    });
  }

  function populateThemeFields(theme) {
    if (!theme) return;
    var toggle = document.getElementById("f_theme_toggle");
    var section = document.getElementById("themeSection");
    if (!toggle || !section) return;
    // Enable theme section if any non-default values
    var hasTheme = theme.primary_color !== "#2563eb" || theme.font_family !== "System Default" || theme.dark_mode;
    if (hasTheme || theme.preset) {
      toggle.checked = true;
      section.style.display = "";
    }
    if (theme.primary_color) { document.getElementById("f_theme_primary").value = theme.primary_color; document.getElementById("f_theme_primary_hex").textContent = theme.primary_color; }
    if (theme.secondary_color) { document.getElementById("f_theme_secondary").value = theme.secondary_color; document.getElementById("f_theme_secondary_hex").textContent = theme.secondary_color; }
    if (theme.accent_color) { document.getElementById("f_theme_accent").value = theme.accent_color; document.getElementById("f_theme_accent_hex").textContent = theme.accent_color; }
    if (theme.font_family) document.getElementById("f_theme_font").value = theme.font_family;
    if (theme.dark_mode) document.getElementById("f_theme_dark").checked = true;
    if (theme.custom_css) document.getElementById("f_theme_css").value = theme.custom_css;
    // Highlight preset button
    if (theme.preset) {
      var btn = document.querySelector('.theme-preset-btn[data-preset="' + theme.preset + '"]');
      if (btn) {
        btn.style.borderColor = theme.primary_color;
        btn.style.boxShadow = "0 0 0 2px " + theme.primary_color + "40";
      }
    }
  }

  function getThemeFields() {
    var toggle = document.getElementById("f_theme_toggle");
    if (!toggle || !toggle.checked) return null;
    return {
      primary_color: (document.getElementById("f_theme_primary") || {}).value || "#2563eb",
      secondary_color: (document.getElementById("f_theme_secondary") || {}).value || "#4f46e5",
      accent_color: (document.getElementById("f_theme_accent") || {}).value || "#f59e0b",
      font_family: (document.getElementById("f_theme_font") || {}).value || "System Default",
      dark_mode: document.getElementById("f_theme_dark") ? document.getElementById("f_theme_dark").checked : false,
      custom_css: (document.getElementById("f_theme_css") || {}).value || "",
      preset: document.getElementById("f_theme_preset_value") ? document.getElementById("f_theme_preset_value").value : null,
    };
  }

  function getHeaderFields() {
    return {
      enabled: document.getElementById("f_header") ? document.getElementById("f_header").checked : false,
      link_url: (document.getElementById("f_link_url") || {}).value || null,
      link_text: (document.getElementById("f_link_text") || {}).value || null,
      email: (document.getElementById("f_header_email") || {}).value || null,
      phone: (document.getElementById("f_header_phone") || {}).value || null,
      text: (document.getElementById("f_header_text") || {}).value || null,
    };
  }

  function uploadLogo(presentationId, fileInput) {
    if (!fileInput || !fileInput.files || !fileInput.files[0]) return Promise.resolve(null);
    var formData = new FormData();
    formData.append("file", fileInput.files[0]);
    return fetch(API + "/api/presentations/" + presentationId + "/logo", {
      method: "POST",
      headers: { "Authorization": "Bearer " + token },
      body: formData,
    }).then(handleAuth).then(function (r) { return r.json(); });
  }

  // ══════════════════════════════════════════════════
  // Shared: Chat Queries Modal
  // ══════════════════════════════════════════════════
  var _qModal = document.getElementById("queriesModal");
  var _qCurrentPage = 1;
  var _qCurrentId = null;

  function openQueriesModal(presentationId) {
    if (!_qModal) return;
    _qCurrentId = presentationId;
    _qModal.style.display = "";
    _loadQueries(1);
  }

  function _loadQueries(page) {
    _qCurrentPage = page;
    var body = document.getElementById("queriesBody");
    body.innerHTML = '<div class="loading">Loading...</div>';

    fetch(API + "/api/presentations/" + _qCurrentId + "/queries?page=" + page + "&page_size=25", {
      headers: authHeaders(),
    })
      .then(handleAuth)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.items || !data.items.length) {
          body.innerHTML = '<p style="color:#9ca3af;text-align:center;padding:2rem 0">No queries recorded yet.</p>';
          document.getElementById("queriesFooter").style.display = "none";
          return;
        }
        var html = '<table class="query-table"><thead><tr>' +
          '<th>Question</th><th>Date / Time</th><th>IP</th><th>Code</th><th></th>' +
          '</tr></thead><tbody>';
        data.items.forEach(function (q) {
          var ts = q.created_at ? new Date(q.created_at).toLocaleString() : q.date;
          html += '<tr>' +
            '<td class="q-text" title="' + esc(q.question) + '">' + esc(q.question) + '</td>' +
            '<td class="q-meta">' + esc(ts) + '</td>' +
            '<td class="q-meta">' + esc(q.client_ip) + '</td>' +
            '<td class="q-meta">' + esc(q.access_code || '\u2014') + '</td>' +
            '<td><button class="btn-danger-sm" data-del-query="' + q.id + '" title="Delete">&times;</button></td>' +
            '</tr>';
        });
        html += '</tbody></table>';
        body.innerHTML = html;

        var totalPages = Math.ceil(data.total / data.page_size);
        var footer = document.getElementById("queriesFooter");
        footer.style.display = totalPages > 1 ? "" : "none";
        document.getElementById("queriesPageInfo").textContent = "Page " + data.page + " of " + totalPages;
        document.getElementById("btnQueriesPrev").disabled = data.page <= 1;
        document.getElementById("btnQueriesNext").disabled = data.page >= totalPages;
      })
      .catch(function (err) {
        body.innerHTML = '<p style="color:#b91c1c;text-align:center">Error: ' + esc(err.message) + '</p>';
      });
  }

  // Wire up modal controls (shared across pages)
  if (_qModal) {
    var _btnClose = document.getElementById("btnCloseQueries");
    if (_btnClose) _btnClose.addEventListener("click", function () { _qModal.style.display = "none"; });
    _qModal.addEventListener("click", function (e) {
      if (e.target === _qModal) _qModal.style.display = "none";
    });

    var _btnPrev = document.getElementById("btnQueriesPrev");
    var _btnNext = document.getElementById("btnQueriesNext");
    if (_btnPrev) _btnPrev.addEventListener("click", function () { _loadQueries(_qCurrentPage - 1); });
    if (_btnNext) _btnNext.addEventListener("click", function () { _loadQueries(_qCurrentPage + 1); });

    var _qBody = document.getElementById("queriesBody");
    if (_qBody) {
      _qBody.addEventListener("click", function (e) {
        var qid = e.target.dataset.delQuery;
        if (!qid) return;
        fetch(API + "/api/presentations/" + _qCurrentId + "/queries/" + qid, {
          method: "DELETE", headers: authHeaders(),
        }).then(handleAuth).then(function () { _loadQueries(_qCurrentPage); });
      });
    }

    var _btnClearAll = document.getElementById("btnClearAllQueries");
    if (_btnClearAll) {
      _btnClearAll.addEventListener("click", function () {
        if (!confirm("Delete ALL chat queries for this presentation? This cannot be undone.")) return;
        fetch(API + "/api/presentations/" + _qCurrentId + "/queries", {
          method: "DELETE", headers: authHeaders(),
        }).then(handleAuth).then(function () {
          _loadQueries(1);
          // Update stats if on edit page
          var sq = document.getElementById("statQueries");
          var st = document.getElementById("statToday");
          if (sq) sq.textContent = "0";
          if (st) st.textContent = "0";
        });
      });
    }
  }

  // ══════════════════════════════════════════════════
  // Create form
  // ══════════════════════════════════════════════════
  var createForm = document.getElementById("createForm");
  if (createForm) {
    var tagsWidget = window.initTagsInput(document.getElementById("tagsContainer"));
    window.initPreview(
      document.getElementById("f_md"),
      document.getElementById("mdPreview"),
      document.getElementById("btnWrite"),
      document.getElementById("btnPreview")
    );
    window.initPasteHandler(document.getElementById("f_md"));
    window.initContentTypeToggle(
      document.getElementById("btnTypeMarkdown"),
      document.getElementById("btnTypeHtml"),
      document.getElementById("markdownEditor"),
      document.getElementById("htmlEditor"),
      document.getElementById("f_content_type"),
      document.getElementById("contentTypeHint")
    );
    window.initHtmlPreview();
    initAccessToggle(document.getElementById("f_access"), document.getElementById("accessCodesSection"));
    initSectionToggle(document.getElementById("f_header"), document.getElementById("headerSection"));
    initThemeSection();
    initLogoFilePreview(document.getElementById("f_logo"), document.getElementById("logoPreviewWrap"), document.getElementById("logoPreview"));

    // Auto-generate slug from title
    var slugManuallyEdited = false;
    var slugEl = document.getElementById("f_slug");
    slugEl.addEventListener("input", function () { slugManuallyEdited = !!slugEl.value.trim(); });
    document.getElementById("f_title").addEventListener("input", function () {
      if (!slugManuallyEdited) {
        slugEl.value = this.value.trim().toLowerCase()
          .replace(/[^\w\s-]/g, "")
          .replace(/[\s_]+/g, "-")
          .replace(/^-+|-+$/g, "");
      }
    });

    // ── Bulk-upload zone (drop files = create as multi-chapter book)
    var createSelectedFiles = []; // array of File objects
    var createUploadZone = document.getElementById("createUploadZone");
    var createFileInput = document.getElementById("createFileInput");
    var createFolderInput = document.getElementById("createFolderInput");
    var createPickBtn = document.getElementById("btnCreatePick");
    var createPickFolderBtn = document.getElementById("btnCreatePickFolder");
    var createUploadPreview = document.getElementById("createUploadPreview");

    function fileRelativeName(file) {
      // Files picked via webkitdirectory carry the relative path; others fall back to plain name.
      return file.webkitRelativePath || file.name || "";
    }

    function isSupportedUploadFile(file) {
      var name = fileRelativeName(file);
      if (!name) return false;
      // Strip macOS metadata / dotfiles
      if (name.indexOf("__MACOSX") !== -1) return false;
      var base = name.split("/").pop();
      if (!base || base.charAt(0) === ".") return false;
      var ext = (base.split(".").pop() || "").toLowerCase();
      return [
        "md", "markdown", "txt", "html", "htm",
        "pdf", "docx", "pptx", "xlsx"
      ].indexOf(ext) !== -1;
    }

    function renderCreateUploadPreview() {
      if (!createUploadPreview) return;
      if (!createSelectedFiles.length) {
        createUploadPreview.style.display = "none";
        createUploadPreview.textContent = "";
        return;
      }
      var names = createSelectedFiles.map(fileRelativeName);
      createUploadPreview.style.display = "block";
      createUploadPreview.innerHTML =
        "<strong>" + createSelectedFiles.length + " file" +
        (createSelectedFiles.length === 1 ? "" : "s") + " selected:</strong> " +
        names.join(", ") +
        ' <a href="#" id="createUploadClear" style="margin-left:.5rem;color:#b91c1c">clear</a>';
      document.getElementById("createUploadClear").addEventListener("click", function (ev) {
        ev.preventDefault();
        createSelectedFiles = [];
        if (createFileInput) createFileInput.value = "";
        if (createFolderInput) createFolderInput.value = "";
        renderCreateUploadPreview();
      });
    }

    function adoptFiles(files) {
      var all = Array.prototype.slice.call(files || []);
      var filtered = all.filter(isSupportedUploadFile);
      var skipped = all.length - filtered.length;
      createSelectedFiles = filtered;
      renderCreateUploadPreview();
      if (skipped > 0 && createUploadPreview) {
        var note = document.createElement("div");
        note.style.cssText = "margin-top:.3rem;color:#9ca3af;font-size:.78rem";
        note.textContent = skipped + " unsupported file" +
          (skipped === 1 ? " was" : "s were") + " skipped.";
        createUploadPreview.appendChild(note);
      }
    }

    if (createPickBtn) {
      createPickBtn.addEventListener("click", function () {
        if (createFileInput) createFileInput.click();
      });
    }
    if (createPickFolderBtn) {
      createPickFolderBtn.addEventListener("click", function () {
        if (createFolderInput) createFolderInput.click();
      });
    }
    if (createFileInput) {
      createFileInput.addEventListener("change", function () {
        adoptFiles(createFileInput.files);
      });
    }
    if (createFolderInput) {
      createFolderInput.addEventListener("change", function () {
        adoptFiles(createFolderInput.files);
      });
    }
    if (createUploadZone) {
      ["dragenter", "dragover"].forEach(function (evt) {
        createUploadZone.addEventListener(evt, function (e) {
          e.preventDefault(); e.stopPropagation();
          createUploadZone.classList.add("is-dragover");
        });
      });
      ["dragleave", "drop"].forEach(function (evt) {
        createUploadZone.addEventListener(evt, function (e) {
          e.preventDefault(); e.stopPropagation();
          createUploadZone.classList.remove("is-dragover");
        });
      });
      createUploadZone.addEventListener("drop", function (e) {
        if (e.dataTransfer && e.dataTransfer.files) {
          adoptFiles(e.dataTransfer.files);
        }
      });
    }

    createForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var errEl = document.getElementById("formError");
      var btn = document.getElementById("submitBtn");
      errEl.classList.remove("show");
      btn.disabled = true;
      btn.textContent = "Creating\u2026";

      var contentType = document.getElementById("f_content_type").value;
      var hasFiles = createSelectedFiles && createSelectedFiles.length > 0;

      // Validate content is not empty — unless files will populate chapters
      var htmlEl = document.getElementById("f_html");
      var mdEl = document.getElementById("f_md");
      var contentVal = contentType === "html"
        ? (htmlEl ? htmlEl.value : "")
        : (mdEl ? mdEl.value : "");
      if (!hasFiles && (!contentVal || !contentVal.trim())) {
        errEl.textContent = contentType === "html"
          ? "HTML content cannot be empty (or upload files instead)."
          : "Markdown content cannot be empty (or upload files instead).";
        errEl.classList.add("show");
        btn.disabled = false;
        btn.textContent = "Create Presentation";
        return;
      }

      var body = {
        title: document.getElementById("f_title").value.trim(),
        content_type: contentType,
        description: document.getElementById("f_desc").value.trim() || null,
        tags: tagsWidget.getTags(),
        chat_enabled: document.getElementById("f_chat").checked,
        access_protected: document.getElementById("f_access").checked,
        header: getHeaderFields(),
      };
      var themeData = getThemeFields();
      if (themeData) body.theme = themeData;
      if (contentType === "html") {
        body.html_content = hasFiles && !contentVal.trim()
          ? "<p>Uploading chapters…</p>"
          : (htmlEl ? htmlEl.value : "");
      } else {
        body.markdown_content = hasFiles && !contentVal.trim()
          ? "# Uploading chapters…\n\nChapter content will appear here after upload completes."
          : (mdEl ? mdEl.value : "");
      }
      if (body.access_protected) {
        body.num_access_codes = parseInt(document.getElementById("f_num_codes").value, 10) || 3;
      }
      var slug = document.getElementById("f_slug").value.trim();
      if (slug) body.slug = slug;

      var logoInput = document.getElementById("f_logo");

      function uploadChapters(presentationId) {
        if (!hasFiles) return Promise.resolve(null);
        btn.textContent = "Uploading chapters…";
        var fd = new FormData();
        for (var i = 0; i < createSelectedFiles.length; i += 1) {
          var file = createSelectedFiles[i];
          // Use the relative path (folder/subfolder/file.md) as the
          // filename so server-side natural sort + slug derivation see
          // the full path. Falls back to bare name for normal pickers.
          fd.append("files", file, fileRelativeName(file));
        }
        var headers = authHeaders();
        delete headers["Content-Type"]; // let browser set multipart boundary
        return fetch(
          API + "/api/presentations/" + presentationId + "/chapters/bulk-import",
          { method: "POST", headers: headers, body: fd }
        ).then(function (r) {
          if (!r.ok) {
            return parseErrorResponse(r, "Bulk import failed").then(function (msg) {
              throw new Error(msg);
            });
          }
          return r.json();
        });
      }

      fetch(API + "/api/presentations", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(body),
      })
        .then(handleAuth)
        .then(function (r) {
          if (!r.ok) return parseErrorResponse(r, "Create failed").then(function (msg) { throw new Error(msg); });
          return r.json();
        })
        .then(function (result) {
          if (result.access_protected && result.access_codes && result.access_codes.length) {
            alert("Access codes generated:\n\n" + result.access_codes.join("\n") + "\n\nSave these codes — they are shown on the edit page too.");
          }
          var followups = Promise.resolve();
          if (logoInput && logoInput.files && logoInput.files[0]) {
            followups = followups.then(function () { return uploadLogo(result.id, logoInput); });
          }
          followups = followups.then(function () { return uploadChapters(result.id); });
          return followups.then(function (bulk) {
            if (hasFiles && bulk) {
              var msg = "Created " + (bulk.created || []).length +
                " chapter(s), updated " + (bulk.updated || []).length +
                (bulk.failed && bulk.failed.length
                  ? ", " + bulk.failed.length + " failed"
                  : "");
              try { console.log("Bulk import:", bulk); } catch (_) {}
              // Land on the edit page so author can review chapters
              window.location.href = "/dashboard/edit/" + result.id;
              return;
            }
            window.location.href = "/dashboard";
          });
        })
        .catch(function (err) {
          errEl.textContent = err.message;
          errEl.classList.add("show");
          btn.disabled = false;
          btn.textContent = "Create Presentation";
        });
    });
  }

  // ══════════════════════════════════════════════════
  // Edit form
  // ══════════════════════════════════════════════════
  var editForm = document.getElementById("editForm");
  if (editForm) {
    var editId = editForm.dataset.id;
    var tagsWidgetEdit = window.initTagsInput(document.getElementById("tagsContainer"));
    window.initPreview(
      document.getElementById("f_md"),
      document.getElementById("mdPreview"),
      document.getElementById("btnWrite"),
      document.getElementById("btnPreview")
    );
    window.initPasteHandler(document.getElementById("f_md"));
    var contentTypeToggle = window.initContentTypeToggle(
      document.getElementById("btnTypeMarkdown"),
      document.getElementById("btnTypeHtml"),
      document.getElementById("markdownEditor"),
      document.getElementById("htmlEditor"),
      document.getElementById("f_content_type"),
      document.getElementById("contentTypeHint")
    );
    window.initHtmlPreview();
    initAccessToggle(document.getElementById("f_access"), document.getElementById("accessCodesSection"));
    initSectionToggle(document.getElementById("f_header"), document.getElementById("headerSection"));
    initThemeSection();
    initLogoFilePreview(document.getElementById("f_logo"), document.getElementById("logoPreviewWrap"), document.getElementById("logoPreview"));

    var pendingLogoDelete = false;

    // Access mode select drives the visibility of access_codes section
    // and keeps the legacy hidden f_access checkbox in sync so other
    // bits of code that still read it continue to work.
    function applyAccessMode(mode) {
      var legacy = document.getElementById("f_access");
      var section = document.getElementById("accessCodesSection");
      var tenantOnly = document.getElementById("tenantOnlySection");
      var hint = document.getElementById("accessModeHint");
      var isCode = mode === "access_code";
      if (legacy) legacy.checked = isCode;
      if (section) section.style.display = isCode ? "" : "none";
      if (tenantOnly) tenantOnly.style.display = (mode === "ambivo_session") ? "" : "none";
      if (hint) {
        if (mode === "public") {
          hint.textContent = "No restrictions. The hosted link is the only secret.";
        } else if (mode === "ambivo_session") {
          hint.textContent = "Page validates an Ambivo JWT (Authorization header or ?token= query). Use this for embedding inside the Ambivo product.";
        } else {
          hint.textContent = "Visitors must enter a valid access code. Codes can be auto-generated or added below.";
        }
      }
    }
    var accessModeSelect = document.getElementById("f_access_mode");
    if (accessModeSelect) {
      accessModeSelect.addEventListener("change", function () {
        applyAccessMode(accessModeSelect.value);
      });
    }

    var accessCodes = [];
    var CODE_RE = /^[A-Z0-9]{3,12}$/;

    function renderAccessCodes() {
      var list = document.getElementById("accessCodesList");
      if (!list) return;
      if (!accessCodes.length) {
        list.innerHTML = '<p style="color:#9ca3af;font-size:.85rem">No codes yet. Add one or auto-generate.</p>';
        return;
      }
      list.innerHTML = accessCodes.map(function (c, i) {
        return '<span class="access-code-chip">' + esc(c) +
          ' <span class="code-action" data-edit="' + i + '" title="Edit" style="cursor:pointer;margin-left:.3rem">&#9998;</span>' +
          ' <span class="code-action" data-remove="' + i + '" title="Remove" style="cursor:pointer;color:#ef4444">&times;</span>' +
          '</span>';
      }).join("");
    }

    function setAccessCodes(codes) {
      accessCodes = (codes || []).slice();
      renderAccessCodes();
    }

    // Load existing data
    fetch(API + "/api/presentations/" + editId, { headers: authHeaders() })
      .then(handleAuth)
      .then(function (r) {
        if (!r.ok) throw new Error("Failed to load presentation");
        return r.json();
      })
      .then(function (p) {
        document.getElementById("f_title").value = p.title || "";
        document.getElementById("f_desc").value = p.description || "";
        document.getElementById("f_slug").value = p.slug || "";
        document.getElementById("f_md").value = p.markdown_content || "";
        document.getElementById("f_chat").checked = p.chat_enabled !== false;
        tagsWidgetEdit.setTags(p.tags || []);

        // Live URL — display the hosted_url returned by the API, fall
        // back to /p/{slug} on the current origin.
        var liveUrl = p.hosted_url
          || (window.location.origin + "/p/" + encodeURIComponent(p.slug || ""));
        var liveLink = document.getElementById("liveUrlLink");
        if (liveLink) {
          liveLink.href = liveUrl;
          liveLink.textContent = liveUrl;
        }
        var gateEl = document.getElementById("liveUrlGate");
        if (gateEl) {
          var mode = p.access_mode || (p.access_protected ? "access_code" : "public");
          if (mode === "ambivo_session") {
            gateEl.textContent = "Requires a logged-in Ambivo session (or a ?token=<JWT> URL parameter).";
            gateEl.style.display = "";
          } else if (mode === "access_code") {
            gateEl.textContent = "Visitors will be prompted for an access code.";
            gateEl.style.display = "";
          } else {
            gateEl.style.display = "none";
          }
        }

        // Content type
        var ct = p.content_type || "markdown";
        contentTypeToggle.activate(ct);
        if (ct === "html" && p.html_content) {
          document.getElementById("f_html").value = p.html_content;
        }

        // Access mode + codes
        var loadedMode = p.access_mode
          || (p.access_protected ? "access_code" : "public");
        if (accessModeSelect) accessModeSelect.value = loadedMode;
        applyAccessMode(loadedMode);
        setAccessCodes(p.access_codes);
        var tenantOnlyEl = document.getElementById("f_access_tenant_only");
        if (tenantOnlyEl) tenantOnlyEl.checked = !!p.access_tenant_only;

        // Header
        var h = p.header || {};
        var headerCheckbox = document.getElementById("f_header");
        if (headerCheckbox) {
          headerCheckbox.checked = !!h.enabled;
          if (h.enabled) document.getElementById("headerSection").style.display = "";
        }
        if (h.link_url) document.getElementById("f_link_url").value = h.link_url;
        if (h.link_text) document.getElementById("f_link_text").value = h.link_text;
        if (h.email) document.getElementById("f_header_email").value = h.email;
        if (h.phone) document.getElementById("f_header_phone").value = h.phone;
        if (h.text) document.getElementById("f_header_text").value = h.text;
        if (h.logo_url) {
          document.getElementById("logoPreview").src = h.logo_url;
          document.getElementById("logoPreviewWrap").style.display = "";
        }

        // Theme
        populateThemeFields(p.theme);

        // Stats bar
        var statsBar = document.getElementById("statsBar");
        if (statsBar) {
          document.getElementById("statViews").textContent = p.num_views || 0;
          document.getElementById("statQueries").textContent = p.total_chat_queries || 0;
          document.getElementById("statToday").textContent = p.today_chat_queries || 0;
          statsBar.style.display = "";
        }

        document.getElementById("editLoading").style.display = "none";
        document.getElementById("editContent").style.display = "";

        // Wire chapters editor if this is a book-mode presentation.
        if (typeof window.initChaptersEditor === "function") {
          try { window.initChaptersEditor(p); }
          catch (chapterErr) { console.error("initChaptersEditor failed", chapterErr); }
        }

        // Activate the sticky save bar's dirty-tracking only after the
        // form is fully populated, so the initial value-set events
        // don't flag the page as dirty.
        try { initStickySaveBar(); } catch (_) {}
      })
      .catch(function (err) {
        document.getElementById("editLoading").textContent = "Error: " + err.message;
      });

    // Code list event delegation (edit / remove)
    var codesList = document.getElementById("accessCodesList");
    if (codesList) {
      codesList.addEventListener("click", function (e) {
        var editIdx = e.target.dataset.edit;
        var removeIdx = e.target.dataset.remove;
        if (editIdx !== undefined) {
          var idx = parseInt(editIdx, 10);
          var newVal = prompt("Edit access code:", accessCodes[idx]);
          if (newVal === null) return;
          newVal = newVal.trim().toUpperCase();
          if (!CODE_RE.test(newVal)) { alert("Invalid code: 3-12 characters, A-Z and 0-9 only."); return; }
          if (accessCodes.indexOf(newVal) !== -1 && accessCodes[idx] !== newVal) { alert("Duplicate code."); return; }
          accessCodes[idx] = newVal;
          renderAccessCodes();
        }
        if (removeIdx !== undefined) {
          accessCodes.splice(parseInt(removeIdx, 10), 1);
          renderAccessCodes();
        }
      });
    }

    // Add custom code
    var btnAddCode = document.getElementById("btnAddCode");
    var newCodeInput = document.getElementById("newCodeInput");
    if (btnAddCode && newCodeInput) {
      btnAddCode.addEventListener("click", function () {
        var val = newCodeInput.value.trim().toUpperCase();
        if (!val) return;
        if (!CODE_RE.test(val)) { alert("Invalid code: 3-12 characters, A-Z and 0-9 only."); return; }
        if (accessCodes.indexOf(val) !== -1) { alert("Code already exists."); return; }
        accessCodes.push(val);
        renderAccessCodes();
        newCodeInput.value = "";
      });
      newCodeInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); btnAddCode.click(); }
      });
    }

    // Auto-generate one code
    var btnAutoGen = document.getElementById("btnAutoGen");
    if (btnAutoGen) {
      btnAutoGen.addEventListener("click", function () {
        var chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
        var code;
        do {
          code = "";
          for (var i = 0; i < 6; i++) code += chars.charAt(Math.floor(Math.random() * chars.length));
        } while (accessCodes.indexOf(code) !== -1);
        accessCodes.push(code);
        renderAccessCodes();
      });
    }

    // Regenerate all codes (immediate server call)
    var regenBtn = document.getElementById("btnRegenCodes");
    if (regenBtn) {
      regenBtn.addEventListener("click", function () {
        if (!confirm("Regenerate all access codes? Old codes will stop working.")) return;
        regenBtn.disabled = true;
        regenBtn.textContent = "Regenerating\u2026";

        fetch(API + "/api/presentations/" + editId, {
          method: "PUT",
          headers: authHeaders(),
          body: JSON.stringify({ regenerate_codes: 3 }),
        })
          .then(handleAuth)
          .then(function (r) { return r.json(); })
          .then(function (p) {
            setAccessCodes(p.access_codes);
            regenBtn.disabled = false;
            regenBtn.textContent = "Regenerate All";
          })
          .catch(function () {
            regenBtn.disabled = false;
            regenBtn.textContent = "Regenerate All";
          });
      });
    }

    // Remove logo button
    var btnRemoveLogo = document.getElementById("btnRemoveLogo");
    if (btnRemoveLogo) {
      btnRemoveLogo.addEventListener("click", function () {
        pendingLogoDelete = true;
        document.getElementById("logoPreview").src = "";
        document.getElementById("logoPreviewWrap").style.display = "none";
        document.getElementById("f_logo").value = "";
      });
    }

    // Open queries modal from edit page stat bar
    var statQueriesWrap = document.getElementById("statQueriesWrap");
    if (statQueriesWrap) {
      statQueriesWrap.addEventListener("click", function () { openQueriesModal(editId); });
    }

    // Sticky save bar — appears at the bottom of the viewport whenever
    // the form has unsaved changes. Submits via the button's `form`
    // attribute so it triggers the existing editForm submit handler.
    var _stickyInitialized = false;
    function initStickySaveBar() {
      if (_stickyInitialized) return;
      _stickyInitialized = true;
      var bar = document.getElementById("stickySaveBar");
      if (!bar || !editForm) return;
      function markDirty() {
        if (!bar.classList.contains("is-visible")) {
          bar.style.display = "";
          // Force a paint so the slide-up transition runs
          // eslint-disable-next-line no-unused-expressions
          bar.offsetHeight;
          bar.classList.add("is-visible");
          document.body.classList.add("has-sticky-save");
        }
      }
      // Most form mutations bubble input / change to the form itself.
      editForm.addEventListener("input", markDirty, true);
      editForm.addEventListener("change", markDirty, true);
      // Tags widget and access-codes edits happen on sibling DOM —
      // listen on document for the same event types so we don't miss
      // them. Filter to elements inside the edit content card to
      // avoid noise from other pages / overlays.
      var editContent = document.getElementById("editContent");
      if (editContent) {
        editContent.addEventListener("input", markDirty, true);
        editContent.addEventListener("change", markDirty, true);
        editContent.addEventListener("click", function (e) {
          // Toggle-style buttons in the access-codes / tags UI don't
          // fire input events; rely on data attributes or specific ids.
          var t = e.target;
          if (!t) return;
          if (t.matches && (
            t.matches("[data-remove]") ||
            t.matches("[data-edit]") ||
            t.id === "btnAddCode" ||
            t.id === "btnAutoGen" ||
            t.id === "btnRegenCodes" ||
            t.classList.contains("theme-preset-btn") ||
            t.classList.contains("tag-remove")
          )) {
            markDirty();
          }
        }, true);
      }
    }

    // Reset the sticky bar's dirty state on successful submit (handled
    // in the submit flow below by hiding before redirect).
    function hideStickySaveBar() {
      var bar = document.getElementById("stickySaveBar");
      if (!bar) return;
      bar.classList.remove("is-visible");
      document.body.classList.remove("has-sticky-save");
    }
    // Live-URL copy button
    var copyLiveBtn = document.getElementById("btnCopyLiveUrl");
    if (copyLiveBtn) {
      copyLiveBtn.addEventListener("click", function () {
        var url = (document.getElementById("liveUrlLink") || {}).href || "";
        if (!url) return;
        var done = function () {
          var orig = copyLiveBtn.textContent;
          copyLiveBtn.textContent = "Copied";
          setTimeout(function () { copyLiveBtn.textContent = orig; }, 1200);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(done, function () {
            window.prompt("Copy this URL:", url);
          });
        } else {
          window.prompt("Copy this URL:", url);
        }
      });
    }

    editForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var errEl = document.getElementById("formError");
      var btn = document.getElementById("submitBtn");
      var stickyBtn = document.getElementById("stickySaveBtn");
      var stickyStatus = document.getElementById("stickySaveStatus");
      errEl.classList.remove("show");
      btn.disabled = true;
      btn.textContent = "Updating\u2026";
      if (stickyBtn) { stickyBtn.disabled = true; stickyBtn.textContent = "Saving\u2026"; }
      if (stickyStatus) stickyStatus.textContent = "Saving changes\u2026";

      var accessMode = (accessModeSelect && accessModeSelect.value) || "public";
      var isProtected = accessMode === "access_code";
      var contentType = document.getElementById("f_content_type").value;

      // Validate content is not empty — except in book mode where the
      // server ignores the top-level content field entirely.
      var isBook = document.getElementById("chaptersSection") &&
                   document.getElementById("chaptersSection").style.display !== "none" &&
                   document.querySelectorAll("#chaptersList .chapter-row").length > 0;
      var contentVal = contentType === "html"
        ? document.getElementById("f_html").value
        : document.getElementById("f_md").value;
      if (!isBook && (!contentVal || !contentVal.trim())) {
        errEl.textContent = contentType === "html"
          ? "HTML content cannot be empty."
          : "Markdown content cannot be empty.";
        errEl.classList.add("show");
        btn.disabled = false;
        btn.textContent = "Update Presentation";
        if (stickyBtn) { stickyBtn.disabled = false; stickyBtn.textContent = "Save changes"; }
        if (stickyStatus) stickyStatus.textContent = "Unsaved changes";
        return;
      }

      var tenantOnlyInput = document.getElementById("f_access_tenant_only");
      var body = {
        title: document.getElementById("f_title").value.trim(),
        content_type: contentType,
        description: document.getElementById("f_desc").value.trim() || null,
        tags: tagsWidgetEdit.getTags(),
        chat_enabled: document.getElementById("f_chat").checked,
        access_mode: accessMode,
        access_protected: isProtected,
        access_tenant_only: !!(tenantOnlyInput && tenantOnlyInput.checked && accessMode === "ambivo_session"),
        header: getHeaderFields(),
      };
      var themeData = getThemeFields();
      if (themeData) body.theme = themeData;
      if (contentType === "html") {
        body.html_content = document.getElementById("f_html").value;
      } else {
        body.markdown_content = document.getElementById("f_md").value;
      }
      if (isProtected) {
        body.access_codes = accessCodes;
      } else {
        body.access_codes = [];
      }

      var logoInput = document.getElementById("f_logo");

      fetch(API + "/api/presentations/" + editId, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify(body),
      })
        .then(handleAuth)
        .then(function (r) {
          if (!r.ok) return parseErrorResponse(r, "Update failed").then(function (msg) { throw new Error(msg); });
          return r.json();
        })
        .then(function () {
          // Handle logo upload or delete
          if (pendingLogoDelete) {
            return fetch(API + "/api/presentations/" + editId + "/logo", {
              method: "DELETE",
              headers: authHeaders(),
            }).then(handleAuth);
          }
          if (logoInput && logoInput.files && logoInput.files[0]) {
            return uploadLogo(editId, logoInput);
          }
        })
        .then(function () {
          hideStickySaveBar();
          window.location.href = "/dashboard";
        })
        .catch(function (err) {
          errEl.textContent = err.message;
          errEl.classList.add("show");
          btn.disabled = false;
          btn.textContent = "Update Presentation";
          if (stickyBtn) { stickyBtn.disabled = false; stickyBtn.textContent = "Save changes"; }
          if (stickyStatus) stickyStatus.textContent = "Save failed — try again.";
        });
    });
  }
  // ══════════════════════════════════════════════════
  // AI Page Generator wizard
  // ══════════════════════════════════════════════════
  var aiModal = document.getElementById("aiModal");
  var btnAiGenerate = document.getElementById("btnAiGenerate");

  if (aiModal && btnAiGenerate) {
    // Hide button by default; show only if AI is available for this tenant
    btnAiGenerate.style.display = "none";
    fetch(API + "/api/ai/generate/available", { headers: authHeaders() })
      .then(handleAuth)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.available) {
          btnAiGenerate.style.display = "";
        }
        // If not available, button stays hidden — no error shown
      })
      .catch(function () {
        // On network error, keep button hidden
      });

    var aiSessionId = null;

    // DOM refs
    var aiStepPrompt = document.getElementById("aiStepPrompt");
    var aiStepInterview = document.getElementById("aiStepInterview");
    var aiStepLoading = document.getElementById("aiStepLoading");
    var aiStepPreview = document.getElementById("aiStepPreview");
    var aiPreviewFrame = document.getElementById("aiPreviewFrame");
    var aiPreviewCode = document.getElementById("aiPreviewCode");
    var btnAiPreviewTab = document.getElementById("btnAiPreviewTab");
    var btnAiCodeTab = document.getElementById("btnAiCodeTab");
    var btnAiUseHtml = document.getElementById("btnAiUseHtml");
    var btnAiDiscard = document.getElementById("btnAiDiscard");
    var aiChatLog = document.getElementById("aiChatLog");
    var aiQuestionsForm = document.getElementById("aiQuestionsForm");
    var btnAiStart = document.getElementById("btnAiStart");
    var btnAiDirect = document.getElementById("btnAiDirect");
    var btnAiAnswer = document.getElementById("btnAiAnswer");
    var btnAiClose = document.getElementById("btnAiClose");
    var aiPromptEl = document.getElementById("aiPrompt");
    var aiStatusEl = document.getElementById("aiStatus");
    var aiFooterError = document.getElementById("aiFooterError");

    function aiShowStep(step) {
      aiStepPrompt.style.display = step === "prompt" ? "" : "none";
      aiStepInterview.style.display = step === "interview" ? "" : "none";
      aiStepLoading.style.display = step === "loading" ? "" : "none";
      aiStepPreview.style.display = step === "preview" ? "flex" : "none";
      aiFooterError.style.display = "none";
    }

    function aiShowError(msg) {
      aiFooterError.textContent = msg;
      aiFooterError.style.display = "";
    }

    function aiAppendMsg(role, html) {
      var div = document.createElement("div");
      div.className = "ai-msg ai-msg-" + role;
      div.innerHTML = html;
      aiChatLog.appendChild(div);
      aiChatLog.scrollTop = aiChatLog.scrollHeight;
    }

    function aiRenderQuestions(questions) {
      if (!questions || !questions.length) return;
      var html = '<ol class="ai-question-list">';
      questions.forEach(function (q, i) {
        html += '<li><label>' + esc(q) + '</label>' +
          '<input type="text" data-ai-q="' + i + '" placeholder="Your answer..."></li>';
      });
      html += '</ol>';
      aiQuestionsForm.innerHTML = html;
      btnAiAnswer.disabled = false;

      // Focus first input
      var first = aiQuestionsForm.querySelector("input");
      if (first) first.focus();
    }

    var aiPendingHtml = "";

    function aiShowPreview(html) {
      aiPendingHtml = html;
      aiShowStep("preview");

      // Show live preview in iframe
      aiPreviewCode.value = html;
      aiPreviewFrame.srcdoc = html;
      aiPreviewFrame.style.display = "block";
      aiPreviewCode.style.display = "none";
      btnAiPreviewTab.className = "btn-sm btn-primary";
      btnAiCodeTab.className = "btn-sm btn-ghost";
    }

    // Preview / Code tab toggle
    btnAiPreviewTab.addEventListener("click", function () {
      // Sync any code edits back to preview
      aiPendingHtml = aiPreviewCode.value;
      aiPreviewFrame.srcdoc = aiPendingHtml;
      aiPreviewFrame.style.display = "block";
      aiPreviewCode.style.display = "none";
      btnAiPreviewTab.className = "btn-sm btn-primary";
      btnAiCodeTab.className = "btn-sm btn-ghost";
    });

    btnAiCodeTab.addEventListener("click", function () {
      aiPreviewFrame.style.display = "none";
      aiPreviewCode.style.display = "block";
      btnAiCodeTab.className = "btn-sm btn-primary";
      btnAiPreviewTab.className = "btn-sm btn-ghost";
    });

    // Use the HTML
    btnAiUseHtml.addEventListener("click", function () {
      var html = aiPreviewCode.value || aiPendingHtml;

      // Switch to HTML mode and insert
      var ctToggle = document.getElementById("f_content_type");
      var htmlTextarea = document.getElementById("f_html");
      if (ctToggle) ctToggle.value = "html";
      if (htmlTextarea) htmlTextarea.value = html;

      // Activate the HTML toggle visually
      var btnTypeMd = document.getElementById("btnTypeMarkdown");
      var btnTypeH = document.getElementById("btnTypeHtml");
      var mdEditor = document.getElementById("markdownEditor");
      var htmlEditorEl = document.getElementById("htmlEditor");
      if (btnTypeH) { btnTypeH.classList.add("active"); btnTypeMd.classList.remove("active"); }
      if (mdEditor) mdEditor.style.display = "none";
      if (htmlEditorEl) htmlEditorEl.style.display = "";

      // Close modal
      aiModal.style.display = "none";
    });

    // Discard — go back to prompt
    btnAiDiscard.addEventListener("click", function () {
      aiPendingHtml = "";
      aiSessionId = null;
      aiShowStep("prompt");
    });

    // Legacy name kept for all callers
    function aiInsertHtml(html) {
      aiShowPreview(html);
    }

    function aiCollectAnswers() {
      var inputs = aiQuestionsForm.querySelectorAll("input[data-ai-q]");
      var answers = [];
      inputs.forEach(function (inp) { answers.push(inp.value); });
      return answers;
    }

    // Safe error detail extractor (handles non-JSON error responses)
    function aiParseError(r, fallback) {
      return r.text().then(function (t) {
        try { var d = JSON.parse(t); return d.detail || fallback; }
        catch (e) { return fallback + " (HTTP " + r.status + ")"; }
      });
    }

    // Open modal
    btnAiGenerate.addEventListener("click", function () {
      aiSessionId = null;
      aiShowStep("prompt");
      aiChatLog.innerHTML = "";
      aiQuestionsForm.innerHTML = "";
      aiPromptEl.value = "";
      aiStatusEl.textContent = "";
      btnAiAnswer.disabled = true;
      aiModal.style.display = "";
      aiPromptEl.focus();
    });

    // Close only via X button — clicking outside does not dismiss
    btnAiClose.addEventListener("click", function () { aiModal.style.display = "none"; });

    // Start interview
    btnAiStart.addEventListener("click", function () {
      var prompt = aiPromptEl.value.trim();
      if (!prompt) { aiShowError("Please describe the page you want."); return; }

      btnAiStart.disabled = true;
      btnAiDirect.disabled = true;
      aiShowStep("loading");

      fetch(API + "/api/ai/generate/start", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ prompt: prompt }),
      })
        .then(handleAuth)
        .then(function (r) {
          if (!r.ok) return aiParseError(r, "Failed to start").then(function (msg) { throw new Error(msg); });
          return r.json();
        })
        .then(function (data) {
          aiSessionId = data.session_id;
          btnAiStart.disabled = false;
          btnAiDirect.disabled = false;

          if (data.status === "complete" && data.html) {
            aiInsertHtml(data.html);
            return;
          }

          aiShowStep("interview");
          aiAppendMsg("user", esc(prompt));
          if (data.questions && data.questions.length) {
            var qhtml = "<strong>Let me ask a few questions:</strong><br>" +
              data.questions.map(function (q) { return "• " + esc(q); }).join("<br>");
            aiAppendMsg("assistant", qhtml);
            aiRenderQuestions(data.questions);
          }
          aiStatusEl.textContent = "Round " + 1;
        })
        .catch(function (err) {
          btnAiStart.disabled = false;
          btnAiDirect.disabled = false;
          aiShowStep("prompt");
          aiShowError(err.message);
        });
    });

    // Generate directly (skip interview)
    btnAiDirect.addEventListener("click", function () {
      var prompt = aiPromptEl.value.trim();
      if (!prompt) { aiShowError("Please describe the page you want."); return; }

      btnAiStart.disabled = true;
      btnAiDirect.disabled = true;
      aiShowStep("loading");

      fetch(API + "/api/ai/generate/direct", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ prompt: prompt }),
      })
        .then(handleAuth)
        .then(function (r) {
          if (!r.ok) return aiParseError(r, "Generation failed").then(function (msg) { throw new Error(msg); });
          return r.json();
        })
        .then(function (data) {
          btnAiStart.disabled = false;
          btnAiDirect.disabled = false;
          if (data.html) {
            aiInsertHtml(data.html);
          } else {
            aiShowStep("prompt");
            aiShowError("No HTML was generated. Please try again with more detail.");
          }
        })
        .catch(function (err) {
          btnAiStart.disabled = false;
          btnAiDirect.disabled = false;
          aiShowStep("prompt");
          aiShowError(err.message);
        });
    });

    // Answer questions
    btnAiAnswer.addEventListener("click", function () {
      if (!aiSessionId) return;
      var answers = aiCollectAnswers();
      if (answers.every(function (a) { return !a.trim(); })) {
        aiShowError("Please answer at least one question."); return;
      }

      // Show user answers in chat
      var answerHtml = answers.filter(function (a) { return a.trim(); })
        .map(function (a) { return esc(a); }).join("<br>");
      aiAppendMsg("user", answerHtml);

      btnAiAnswer.disabled = true;
      aiQuestionsForm.innerHTML = '<div class="ai-spinner"></div>';
      aiStatusEl.textContent = "Thinking\u2026";

      fetch(API + "/api/ai/generate/" + aiSessionId + "/answer", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ answers: answers }),
      })
        .then(handleAuth)
        .then(function (r) {
          if (!r.ok) return aiParseError(r, "Failed to process answers").then(function (msg) { throw new Error(msg); });
          return r.json();
        })
        .then(function (data) {
          if (data.status === "complete" && data.html) {
            aiInsertHtml(data.html);
            return;
          }

          // More questions
          if (data.questions && data.questions.length) {
            var qhtml = data.questions.map(function (q) { return "• " + esc(q); }).join("<br>");
            aiAppendMsg("assistant", qhtml);
            aiRenderQuestions(data.questions);
          }
          aiStatusEl.textContent = "Round " + (data.round || "");
        })
        .catch(function (err) {
          aiShowError(err.message);
          btnAiAnswer.disabled = false;
          aiQuestionsForm.innerHTML = "";
          aiStatusEl.textContent = "";
        });
    });
  }
})();
