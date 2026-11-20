/**
 * Export Clip — scene player UI (DOM injection)
 *
 * React ScenePlayer patch.after is easy to miss (lazy load / layout). This
 * mirrors CommunityScripts VideoScrollWheel: wait for #VideoJsPlayer on
 * /scenes/<id>, then insert Set Begin / Set End / Export under .VideoPlayer.
 */
(function () {
  "use strict";

  var BAR_ID = "export-clip-bar";
  var STYLE_ID = "export-clip-bar-style-fallback";

  function log() {
    var args = ["[export-clip]"].concat([].slice.call(arguments));
    try {
      console.log.apply(console, args);
    } catch (_) {}
  }

  function formatTime(seconds) {
    if (seconds == null || isNaN(seconds)) return "--:--";
    var s = Math.max(0, Number(seconds));
    var whole = Math.floor(s);
    var ms = Math.round((s - whole) * 1000);
    var h = Math.floor(whole / 3600);
    var m = Math.floor((whole % 3600) / 60);
    var sec = whole % 60;
    var base =
      h > 0
        ? h + ":" + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0")
        : m + ":" + String(sec).padStart(2, "0");
    return ms ? base + "." + String(ms).padStart(3, "0") : base;
  }

  function sceneIdFromPath(pathname) {
    var m = (pathname || window.location.pathname).match(/^\/scenes\/(\d+)/);
    return m ? m[1] : null;
  }

  function getPlayer() {
    try {
      var interactive = window.PluginApi && PluginApi.utils && PluginApi.utils.InteractiveUtils;
      if (interactive && typeof interactive.getPlayer === "function") {
        var p = interactive.getPlayer();
        if (p) return p;
      }
    } catch (_) {}
    var el = document.getElementById("VideoJsPlayer");
    if (el && el.player) return el.player;
    // video.js sometimes parks the player on a child element
    var vjs = document.querySelector("#VideoJsPlayer video-js, video-js#VideoJsPlayer, .video-js");
    if (vjs && vjs.player) return vjs.player;
    return null;
  }

  function getCurrentTime() {
    var player = getPlayer();
    if (!player || typeof player.currentTime !== "function") return null;
    var t = player.currentTime();
    return typeof t === "number" && !isNaN(t) ? t : null;
  }

  function callGQL(query, variables) {
    return fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ query: query, variables: variables }),
    }).then(function (res) {
      return res.json();
    }).then(function (json) {
      if (json.errors && json.errors.length) {
        throw new Error(json.errors.map(function (e) { return e.message; }).join("; "));
      }
      return json.data;
    });
  }

  function runExport(sceneId, begin, end) {
    return callGQL(
      "mutation RunPluginOperation($plugin_id: ID!, $args: Map) {\n" +
        "  runPluginOperation(plugin_id: $plugin_id, args: $args)\n" +
        "}",
      {
        plugin_id: "export-clip",
        args: {
          action: "export-clip",
          scene_id: String(sceneId),
          begin: begin,
          end: end,
        },
      }
    ).then(function (data) {
      return data.runPluginOperation;
    });
  }

  function toast(message, isError) {
    try {
      // Best-effort: some Stash builds expose toast via PluginApi hooks only inside React
      console[isError ? "error" : "log"]("[export-clip]", message);
    } catch (_) {}
    var status = document.querySelector("#" + BAR_ID + " .export-clip-bar__status");
    if (status) {
      status.textContent = message;
      status.classList.toggle("export-clip-bar__status--error", !!isError);
    }
  }

  function ensureFallbackCss() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent =
      "#" + BAR_ID + "{" +
      "display:flex;flex-direction:column;gap:.35rem;margin:.45rem 0 .65rem;" +
      "padding:.5rem .65rem;background:rgba(0,0,0,.45);" +
      "border:1px solid rgba(255,255,255,.18);border-radius:.25rem;z-index:5;position:relative;" +
      "}" +
      "#" + BAR_ID + " .export-clip-bar__controls{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem .5rem}" +
      "#" + BAR_ID + " .export-clip-bar__time{min-width:4.5rem;font-variant-numeric:tabular-nums;" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85rem}" +
      "#" + BAR_ID + " .export-clip-bar__status{font-size:.8rem;opacity:.9;word-break:break-all}" +
      "#" + BAR_ID + " .export-clip-bar__status--error{color:#f88}" +
      "#" + BAR_ID + " button{cursor:pointer}";
    document.head.appendChild(style);
  }

  function makeButton(label, className, onClick) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = className || "btn btn-sm btn-secondary";
    btn.textContent = label;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      onClick(e);
    });
    return btn;
  }

  function mountBar(anchor, sceneId) {
    ensureFallbackCss();

    var existing = document.getElementById(BAR_ID);
    if (existing) {
      if (existing.getAttribute("data-scene-id") === String(sceneId)) return existing;
      existing.remove();
    }

    var state = { begin: null, end: null, busy: false };

    var bar = document.createElement("div");
    bar.id = BAR_ID;
    bar.className = "export-clip-bar";
    bar.setAttribute("data-scene-id", String(sceneId));

    var controls = document.createElement("div");
    controls.className = "export-clip-bar__controls";

    var beginLabel = document.createElement("span");
    beginLabel.className = "export-clip-bar__time";
    beginLabel.title = "Begin time";
    beginLabel.textContent = "--:--";

    var endLabel = document.createElement("span");
    endLabel.className = "export-clip-bar__time";
    endLabel.title = "End time";
    endLabel.textContent = "--:--";

    var status = document.createElement("div");
    status.className = "export-clip-bar__status";
    status.textContent = "Set begin/end from the player, then export.";

    function refreshTimes() {
      beginLabel.textContent = formatTime(state.begin);
      endLabel.textContent = formatTime(state.end);
    }

    var setBeginBtn = makeButton("Set Begin", "btn btn-sm btn-secondary", function () {
      var t = getCurrentTime();
      if (t == null) {
        toast("Player time unavailable — is the video loaded?", true);
        return;
      }
      state.begin = t;
      refreshTimes();
      toast("Begin set to " + formatTime(t), false);
    });
    setBeginBtn.title = "Set clip begin to current player time";

    var setEndBtn = makeButton("Set End", "btn btn-sm btn-secondary", function () {
      var t = getCurrentTime();
      if (t == null) {
        toast("Player time unavailable — is the video loaded?", true);
        return;
      }
      state.end = t;
      refreshTimes();
      toast("End set to " + formatTime(t), false);
    });
    setEndBtn.title = "Set clip end to current player time";

    var exportBtn = makeButton("Export Clip", "btn btn-sm btn-primary", function () {
      if (state.busy) return;
      if (state.begin == null || state.end == null) {
        toast("Set both Begin and End first", true);
        return;
      }
      if (!(state.begin < state.end)) {
        toast(
          "Begin (" + formatTime(state.begin) + ") must be before end (" + formatTime(state.end) + ")",
          true
        );
        return;
      }
      state.busy = true;
      exportBtn.disabled = true;
      exportBtn.textContent = "Exporting…";
      toast("Exporting…", false);
      runExport(sceneId, state.begin, state.end)
        .then(function (result) {
          var path = null;
          if (typeof result === "string") path = result;
          else if (result && typeof result === "object") {
            if (result.error) throw new Error(String(result.error));
            path = result.output || result.path || result.file || JSON.stringify(result);
          }
          toast(path ? "Exported: " + path : "Export finished (see Tasks / plugin logs)", false);
        })
        .catch(function (err) {
          toast("Export failed: " + (err && err.message ? err.message : err), true);
        })
        .then(function () {
          state.busy = false;
          exportBtn.disabled = false;
          exportBtn.textContent = "Export Clip";
        });
    });
    exportBtn.title = "Export MP4 for the selected range";

    var clearBtn = makeButton("Clear", "btn btn-sm btn-outline-secondary", function () {
      state.begin = null;
      state.end = null;
      refreshTimes();
      toast("Cleared begin/end", false);
    });

    controls.appendChild(setBeginBtn);
    controls.appendChild(beginLabel);
    controls.appendChild(setEndBtn);
    controls.appendChild(endLabel);
    controls.appendChild(exportBtn);
    controls.appendChild(clearBtn);
    bar.appendChild(controls);
    bar.appendChild(status);

    // Prefer immediately after .VideoPlayer; else after the video-js host
    if (anchor && anchor.parentNode) {
      if (anchor.nextSibling) {
        anchor.parentNode.insertBefore(bar, anchor.nextSibling);
      } else {
        anchor.parentNode.appendChild(bar);
      }
    } else {
      document.body.appendChild(bar);
    }

    log("mounted bar for scene", sceneId, "after", anchor && anchor.className);
    return bar;
  }

  function findAnchor() {
    return (
      document.querySelector(".scene-player-container .VideoPlayer") ||
      document.querySelector(".VideoPlayer") ||
      document.querySelector("#VideoJsPlayer") ||
      document.querySelector(".scene-player-container")
    );
  }

  function tryMount() {
    var sceneId = sceneIdFromPath(window.location.pathname);
    if (!sceneId) {
      var stale = document.getElementById(BAR_ID);
      if (stale) stale.remove();
      return false;
    }
    // Need the player (or at least the scene player container) present
    var playerEl = document.getElementById("VideoJsPlayer");
    var anchor = findAnchor();
    if (!anchor && !playerEl) return false;
    mountBar(anchor || playerEl, sceneId);
    return true;
  }

  function waitForElement(selector, timeoutMs) {
    return new Promise(function (resolve) {
      var existing = document.querySelector(selector);
      if (existing) {
        resolve(existing);
        return;
      }
      var obs = new MutationObserver(function () {
        var el = document.querySelector(selector);
        if (el) {
          obs.disconnect();
          resolve(el);
        }
      });
      obs.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(function () {
        obs.disconnect();
        resolve(document.querySelector(selector));
      }, timeoutMs || 15000);
    });
  }

  function onSceneRoute() {
    var path = window.location.pathname || "";
    if (!/^\/scenes\/\d+/.test(path)) {
      var stale = document.getElementById(BAR_ID);
      if (stale) stale.remove();
      return;
    }
    log("scene route", path);
    // Player mounts async; retry a few times
    waitForElement("#VideoJsPlayer, .VideoPlayer, .scene-player-container", 20000).then(function () {
      var attempts = 0;
      var timer = setInterval(function () {
        attempts += 1;
        if (tryMount() || attempts >= 40) clearInterval(timer);
      }, 250);
    });
  }

  // Initial + SPA navigations (same helper VideoScrollWheel / cs-ui-lib use)
  if (/^\/scenes\/\d+/.test(window.location.pathname || "")) {
    onSceneRoute();
  }
  if (window.PluginApi && PluginApi.Event && PluginApi.Event.addEventListener) {
    PluginApi.Event.addEventListener("stash:location", function (e) {
      var pathname =
        (e && e.detail && e.detail.data && e.detail.data.location && e.detail.data.location.pathname) ||
        window.location.pathname;
      if (/^\/scenes\/\d+/.test(pathname || "")) onSceneRoute();
      else {
        var stale = document.getElementById(BAR_ID);
        if (stale) stale.remove();
      }
    });
  } else {
    // Fallback if PluginApi.Event missing
    window.addEventListener("popstate", onSceneRoute);
  }

  // Do NOT use PluginApi.patch on ScenePlayer. React may call the component with
  // (props, secondArg) so an after-hook typed as (props, result) can capture an
  // empty object as "result" and return it → React error #31 and a dead Scene page.

  log("UI script loaded");
})();
