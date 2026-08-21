/* Book reader — client-side chapter navigation, search, and prev/next.
 *
 * Chapters are bundled into the page as JSON in #bookChaptersData, so
 * navigation between chapters is instant and works offline once the
 * page has loaded.
 */
(function () {
  "use strict";

  const dataEl = document.getElementById("bookChaptersData");
  const script = document.currentScript;
  if (!dataEl || !script) return;

  let chapters = [];
  try {
    chapters = JSON.parse(dataEl.textContent || "[]");
  } catch (err) {
    console.error("book-reader: failed to parse chapters JSON", err);
    return;
  }
  if (!Array.isArray(chapters) || chapters.length === 0) return;

  const slug = script.getAttribute("data-slug") || "";
  let currentSlug = script.getAttribute("data-selected-slug") || chapters[0].slug;

  const content = document.getElementById("bookContent");
  const tocList = document.getElementById("bookTocList");
  const prev = document.getElementById("bookPrev");
  const next = document.getElementById("bookNext");
  const search = document.getElementById("bookSearch");
  const toggle = document.getElementById("bookSidebarToggle");
  const sidebar = document.getElementById("bookSidebar");

  function chapterIndex(s) {
    return chapters.findIndex((c) => c.slug === s);
  }

  function renderChapter(targetSlug, pushHistory) {
    const idx = chapterIndex(targetSlug);
    if (idx < 0) return false;
    const ch = chapters[idx];

    if (content) {
      content.innerHTML = ch.html || "";
      if (window.hljs && typeof window.hljs.highlightAll === "function") {
        try { window.hljs.highlightAll(); } catch (_) { /* noop */ }
      }
      // Mark broken images so styling can hide them, matching page.html
      content.querySelectorAll("img").forEach(function (img) {
        img.addEventListener("error", function () { img.classList.add("broken"); });
        if (img.complete && img.naturalWidth === 0 && img.src) {
          img.classList.add("broken");
        }
      });
    }

    if (tocList) {
      tocList.querySelectorAll(".book-toc-item").forEach(function (li) {
        li.classList.toggle("is-active", li.getAttribute("data-slug") === targetSlug);
      });
    }

    document.title = ch.title + " — " + (document.title.split(" — ").slice(-1)[0] || "");
    currentSlug = targetSlug;

    if (pushHistory) {
      const url = "/p/" + encodeURIComponent(slug) + "/c/" + encodeURIComponent(targetSlug);
      try {
        window.history.pushState({ chapter: targetSlug }, "", url);
      } catch (_) { /* noop */ }
    }

    // Prev/next visibility + targets
    if (prev) {
      if (idx > 0) {
        prev.hidden = false;
        prev.textContent = "← " + chapters[idx - 1].title;
        prev.href = "/p/" + encodeURIComponent(slug) + "/c/" + encodeURIComponent(chapters[idx - 1].slug);
      } else {
        prev.hidden = true;
      }
    }
    if (next) {
      if (idx < chapters.length - 1) {
        next.hidden = false;
        next.textContent = chapters[idx + 1].title + " →";
        next.href = "/p/" + encodeURIComponent(slug) + "/c/" + encodeURIComponent(chapters[idx + 1].slug);
      } else {
        next.hidden = true;
      }
    }

    // Scroll to top of chapter on switch
    if (content && typeof content.scrollIntoView === "function") {
      content.scrollIntoView({ behavior: "instant", block: "start" });
    }

    return true;
  }

  // Intercept TOC clicks for client-side nav
  if (tocList) {
    tocList.addEventListener("click", function (e) {
      const link = e.target.closest("a");
      if (!link) return;
      const li = link.closest(".book-toc-item");
      if (!li) return;
      e.preventDefault();
      const target = li.getAttribute("data-slug");
      if (target && target !== currentSlug) {
        renderChapter(target, true);
        // On mobile, close the sidebar after selection
        document.body.classList.remove("book-sidebar-open");
      }
    });
  }

  // Prev / next
  function navHandler(e) {
    if (!e.target.href) return;
    const m = e.target.href.match(/\/p\/[^/]+\/c\/([^/?#]+)/);
    if (!m) return;
    e.preventDefault();
    renderChapter(decodeURIComponent(m[1]), true);
  }
  if (prev) prev.addEventListener("click", navHandler);
  if (next) next.addEventListener("click", navHandler);

  // Back/forward
  window.addEventListener("popstate", function () {
    const m = window.location.pathname.match(/\/p\/[^/]+\/c\/([^/?#]+)/);
    if (m) {
      renderChapter(decodeURIComponent(m[1]), false);
    } else {
      renderChapter(chapters[0].slug, false);
    }
  });

  // Search filter — case-insensitive, matches title substring
  if (search && tocList) {
    search.addEventListener("input", function () {
      const q = (search.value || "").trim().toLowerCase();
      tocList.querySelectorAll(".book-toc-item").forEach(function (li) {
        const t = li.getAttribute("data-title") || "";
        const hit = !q || t.indexOf(q) !== -1;
        li.classList.toggle("is-hidden", !hit);
      });
    });
  }

  // Populate prev/next href + neighbor titles for the server-rendered chapter.
  renderChapter(currentSlug, false);

  // Highlight code blocks present in the server-rendered initial chapter.
  if (window.hljs && typeof window.hljs.highlightAll === "function") {
    try { window.hljs.highlightAll(); } catch (_) { /* noop */ }
  }

  // Mobile swipe navigation — swipe left = next chapter, right = prev.
  // Only active at the same ≤800px breakpoint that collapses the
  // sidebar; on desktop the prev/next buttons handle paging.
  if (content) {
    const SWIPE_MIN_DX = 60;          // horizontal distance to count as a swipe
    const SWIPE_MAX_DT = 500;         // milliseconds — slower than this is a scroll
    const SWIPE_AXIS_RATIO = 1.5;     // dx must beat dy by this factor (filters vertical scrolls)
    const isMobileViewport = () =>
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 800px)").matches;
    let touchStart = null;

    content.addEventListener("touchstart", function (e) {
      if (!isMobileViewport()) return;
      if (document.body.classList.contains("book-sidebar-open")) return;
      if (e.touches.length !== 1) { touchStart = null; return; }
      const t = e.touches[0];
      touchStart = { x: t.clientX, y: t.clientY, time: Date.now() };
    }, { passive: true });

    content.addEventListener("touchend", function (e) {
      const start = touchStart;
      touchStart = null;
      if (!start || !isMobileViewport()) return;
      const t = e.changedTouches && e.changedTouches[0];
      if (!t) return;
      const dx = t.clientX - start.x;
      const dy = t.clientY - start.y;
      const dt = Date.now() - start.time;
      if (dt > SWIPE_MAX_DT) return;
      if (Math.abs(dx) < SWIPE_MIN_DX) return;
      if (Math.abs(dx) < Math.abs(dy) * SWIPE_AXIS_RATIO) return;
      const idx = chapterIndex(currentSlug);
      if (dx < 0 && idx < chapters.length - 1) {
        renderChapter(chapters[idx + 1].slug, true);
      } else if (dx > 0 && idx > 0) {
        renderChapter(chapters[idx - 1].slug, true);
      }
    }, { passive: true });

    // Abort tracking if the gesture turns into multi-touch (pinch-zoom)
    // mid-swipe so we don't pop to a new chapter at the end.
    content.addEventListener("touchcancel", function () { touchStart = null; }, { passive: true });
  }

  // Mobile sidebar toggle
  if (toggle) {
    toggle.addEventListener("click", function () {
      document.body.classList.toggle("book-sidebar-open");
    });
    // Tap outside sidebar to close (mobile)
    document.addEventListener("click", function (e) {
      if (!document.body.classList.contains("book-sidebar-open")) return;
      if (sidebar && sidebar.contains(e.target)) return;
      if (toggle.contains(e.target)) return;
      document.body.classList.remove("book-sidebar-open");
    });
  }
})();
