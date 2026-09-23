/* Find in footage: search the library by what was said.

   The transcripts are already timed segments, so a hit is a MOMENT, not just
   a file. This panel lists them and opens the clip at that second.

   Standalone on purpose, like zerko-motion.js: the React bundle is compiled
   and has no source on this machine, so a feature that can live beside it
   does. Everything is wrapped so a failure here cannot take the app down.

   Open: Ctrl+Shift+F (Cmd+Shift+F on a Mac), or the button on the library. */
(function () {
  "use strict";
  try {
    var PORTAL = /^\/(s|f)\//;
    if (PORTAL.test(location.pathname)) return;

    var token = function () {
      try {
        var t = localStorage.getItem("token");
        return t && t !== "null" && t !== "undefined" ? t : null;
      } catch (e) { return null; }
    };

    var css = [
      "[data-zk-spoken-btn]{position:fixed;right:16px;bottom:16px;z-index:40;display:flex;align-items:center;gap:6px;",
      "padding:8px 12px;border-radius:999px;border:1px solid rgb(63 63 70);background:rgb(24 24 27 / .92);color:rgb(212 212 216);",
      "font:500 12px/1 system-ui,sans-serif;cursor:pointer;box-shadow:0 6px 20px rgb(0 0 0 / .35);backdrop-filter:blur(6px)}",
      "[data-zk-spoken-btn]:hover{color:#fff;border-color:rgb(var(--accent-rgb,255 92 31) / .6)}",
      "[data-zk-spoken-btn] svg{width:14px;height:14px}",
      "@media (max-width:640px){[data-zk-spoken-btn] span{display:none}[data-zk-spoken-btn]{padding:10px}}",
      "[data-zk-spoken]{position:fixed;inset:0;z-index:70;display:flex;justify-content:center;align-items:flex-start;",
      "padding:8vh 16px 16px;background:rgb(0 0 0 / .7)}",
      "[data-zk-spoken] .zk-panel{width:100%;max-width:720px;max-height:80vh;display:flex;flex-direction:column;",
      "background:rgb(24 24 27);border:1px solid rgb(63 63 70);border-radius:12px;box-shadow:0 20px 60px rgb(0 0 0 / .5);overflow:hidden;",
      "color:rgb(228 228 231);font:14px/1.4 system-ui,sans-serif}",
      "[data-zk-spoken] .zk-head{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid rgb(39 39 42)}",
      "[data-zk-spoken] input{flex:1;min-width:0;background:transparent;border:0;outline:0;color:#fff;font-size:16px}",
      "[data-zk-spoken] .zk-hint{padding:8px 14px;color:rgb(113 113 122);font-size:12px;border-bottom:1px solid rgb(39 39 42)}",
      "[data-zk-spoken] .zk-list{overflow-y:auto;padding:6px}",
      "[data-zk-spoken] .zk-clip{display:flex;gap:10px;padding:8px;border-radius:8px}",
      "[data-zk-spoken] .zk-clip img{width:96px;height:54px;object-fit:cover;border-radius:6px;background:rgb(39 39 42);flex-shrink:0}",
      "[data-zk-spoken] .zk-body{flex:1;min-width:0}",
      "[data-zk-spoken] .zk-name{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      "[data-zk-spoken] .zk-folder{color:rgb(113 113 122);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      "[data-zk-spoken] .zk-moment{display:flex;gap:8px;width:100%;text-align:left;margin-top:4px;padding:4px 6px;border-radius:6px;",
      "border:0;background:transparent;color:rgb(212 212 216);font-size:12px;cursor:pointer}",
      "[data-zk-spoken] .zk-moment:hover,[data-zk-spoken] .zk-moment:focus-visible{background:rgb(39 39 42);outline:0}",
      "[data-zk-spoken] .zk-moment[data-active]{background:rgb(var(--accent-rgb,255 92 31) / .18)}",
      "[data-zk-spoken] .zk-tc{font:11px ui-monospace,monospace;color:rgb(161 161 170);padding-top:1px;flex-shrink:0;font-variant-numeric:tabular-nums}",
      "[data-zk-spoken] mark{background:rgb(250 204 21 / .28);color:rgb(254 249 195);border-radius:2px;padding:0 1px}",
      "[data-zk-spoken] .zk-empty{padding:24px;text-align:center;color:rgb(113 113 122)}",
      "[data-zk-spoken] .zk-x{border:0;background:transparent;color:rgb(161 161 170);font-size:20px;cursor:pointer;line-height:1}"
    ].join("");
    var style = document.createElement("style");
    style.setAttribute("data-zk-spoken-style", "");
    style.textContent = css;
    document.head.appendChild(style);

    var ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>' +
      '<path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>';

    var esc = function (s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    };
    var tc = function (s) {
      s = Math.max(0, Number(s) || 0);
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = Math.floor(s % 60);
      var mm = (h ? String(m).padStart(2, "0") : String(m)), ss = String(x).padStart(2, "0");
      return (h ? h + ":" : "") + mm + ":" + ss;
    };
    var terms = function (q) {
      var out = [];
      q.replace(/"([^"]+)"/g, function (_, p) { out.push(p.trim()); return " "; })
       .split(/[^\w']+/).forEach(function (w) { if (w) out.push(w); });
      return out.filter(Boolean).sort(function (a, b) { return b.length - a.length; });
    };
    var highlight = function (text, ts) {
      var safe = esc(text);
      if (!ts.length) return safe;
      var re = new RegExp("(" + ts.map(function (t) {
        return esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      }).join("|") + ")", "gi");
      return safe.replace(re, "<mark>$1</mark>");
    };

    var open = function (videoId, t) {
      var url = "/browse?video=" + encodeURIComponent(videoId) + "&t=" + encodeURIComponent(Math.max(0, t - 0.2).toFixed(2));
      close();
      if (location.pathname === "/browse") {
        history.pushState({}, "", url);
        window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
      } else {
        location.assign(url);
      }
    };

    var root = null, input, list, hint, timer = null, seq = 0, lastFocus = null;

    var render = function (data, q) {
      var ts = terms(q);
      if (!data.results || !data.results.length) {
        list.innerHTML = '<div class="zk-empty">Nobody says that in any transcribed clip.</div>';
        hint.textContent = "Only clips that have been transcribed can be searched.";
        return;
      }
      hint.textContent = data.moments + " moment" + (data.moments === 1 ? "" : "s") + " in " +
        data.results.length + " clip" + (data.results.length === 1 ? "" : "s") + " - Enter or click opens the clip there.";
      list.innerHTML = data.results.map(function (r) {
        var thumb = r.thumbnail_path ? '<img alt="" loading="lazy" src="' + esc(r.thumbnail_path) + '">' : "<img alt=\"\">";
        return '<div class="zk-clip">' + thumb + '<div class="zk-body"><div class="zk-name" title="' + esc(r.filename) + '">' +
          esc(r.filename) + '</div><div class="zk-folder">' + esc(r.folder || "") +
          (r.match_count > r.moments.length ? " - " + r.match_count + " mentions" : "") + "</div>" +
          r.moments.map(function (m) {
            return '<button type="button" class="zk-moment" data-v="' + r.video_id + '" data-t="' + m.start + '">' +
              '<span class="zk-tc">' + tc(m.start) + "</span><span>" + highlight(m.text, ts) + "</span></button>";
          }).join("") + "</div></div>";
      }).join("");
    };

    var run = function () {
      var q = input.value.trim();
      var mine = ++seq;
      if (q.length < 2) {
        list.innerHTML = "";
        hint.textContent = 'Every word, any order. Put "a phrase" in quotes to match it exactly.';
        return;
      }
      hint.textContent = "Searching…";
      var tok = token();
      fetch("/api/search/spoken?q=" + encodeURIComponent(q), {
        headers: tok ? { Authorization: "Bearer " + tok } : {}
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status === 401 ? "Sign in again to search." : "Search failed (" + r.status + ")");
        return r.json();
      }).then(function (data) {
        if (mine === seq) render(data, q);
      }).catch(function (e) {
        if (mine === seq) { list.innerHTML = ""; hint.textContent = e.message || "Search failed"; }
      });
    };

    var build = function () {
      root = document.createElement("div");
      root.setAttribute("data-zk-spoken", "");
      root.setAttribute("role", "dialog");
      root.setAttribute("aria-modal", "true");
      root.setAttribute("aria-label", "Find in footage");
      root.innerHTML = '<div class="zk-panel"><div class="zk-head">' + ICON.replace("<svg", '<svg width="18" height="18"') +
        '<input type="search" placeholder="Find in footage - what was said…" aria-label="Search transcripts" autocomplete="off">' +
        '<button type="button" class="zk-x" aria-label="Close">×</button></div>' +
        '<div class="zk-hint"></div><div class="zk-list"></div></div>';
      input = root.querySelector("input");
      list = root.querySelector(".zk-list");
      hint = root.querySelector(".zk-hint");
      root.addEventListener("mousedown", function (e) { if (e.target === root) close(); });
      root.querySelector(".zk-x").addEventListener("click", close);
      input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 250); });
      list.addEventListener("click", function (e) {
        var b = e.target.closest && e.target.closest(".zk-moment");
        if (b) open(b.getAttribute("data-v"), Number(b.getAttribute("data-t")));
      });
      root.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
        var items = Array.prototype.slice.call(list.querySelectorAll(".zk-moment"));
        if (!items.length) return;
        var i = items.indexOf(document.activeElement);
        if (e.key === "ArrowDown") { e.preventDefault(); items[Math.min(items.length - 1, i + 1)].focus(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); i <= 0 ? input.focus() : items[i - 1].focus(); }
        else if (e.key === "Enter" && document.activeElement === input) {
          e.preventDefault(); items[0].click();
        }
      });
      // Keep the app's own shortcuts (I/O, J/K/L, ratings) from firing while typing here.
      root.addEventListener("keydown", function (e) { e.stopPropagation(); });
      document.body.appendChild(root);
    };

    var isOpen = function () { return root && root.parentNode && root.style.display !== "none"; };
    var show = function () {
      if (!token()) return;
      lastFocus = document.activeElement;
      if (!root) build();
      root.style.display = "";
      if (!root.parentNode) document.body.appendChild(root);
      run();
      setTimeout(function () { input.focus(); input.select(); }, 0);
    };
    function close() {
      if (!root) return;
      root.style.display = "none";
      if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
    }

    window.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "F" || e.key === "f")) {
        if (PORTAL.test(location.pathname)) return;
        e.preventDefault();
        isOpen() ? close() : show();
      }
    }, true);
    window.addEventListener("zerko-find-in-footage", function () { show(); });

    // The button: only on the library, only when signed in.
    var btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("data-zk-spoken-btn", "");
    btn.title = "Find in footage (Ctrl+Shift+F)";
    btn.innerHTML = ICON + "<span>Find in footage</span>";
    btn.addEventListener("click", show);
    var sync = function () {
      var want = location.pathname === "/browse" && !!token();
      if (want && !btn.parentNode) document.body.appendChild(btn);
      if (!want && btn.parentNode) btn.parentNode.removeChild(btn);
    };
    var wrap = function (name) {
      var orig = history[name];
      history[name] = function () { var r = orig.apply(this, arguments); setTimeout(sync, 0); return r; };
    };
    wrap("pushState"); wrap("replaceState");
    window.addEventListener("popstate", function () { setTimeout(sync, 0); });
    window.addEventListener("storage", sync);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync);
    else sync();
    setInterval(sync, 3000);   // sign-in/out does not always change the URL
  } catch (e) {
    if (window.console) console.warn("zerko-spoken disabled:", e);
  }
})();
