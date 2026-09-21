/**
 * Task 4.2 — Call History Inspector client module.
 *
 * Drives the Call Desk dashboard against the live call-history API:
 *   - paginated, filtered call log queries
 *   - per-channel waveform rendering (left = caller, right = agent)
 *   - a seekable audio player backed by HTTP range requests
 *   - a transcript that follows playback and seeks on click
 *
 * No build step and no dependencies: attach it with a plain <script> tag and
 * use the global `CallInspector`.
 */
(function (global) {
  "use strict";

  var API = {
    list: "/api/calls",
    detail: "/api/calls/detail",
    waveform: "/api/calls/waveform",
    audio: "/api/calls/audio",
    stats: "/api/calls/stats",
    exportCsv: "/api/calls/export"
  };

  // ── HTTP ───────────────────────────────────────────────────────────────────
  function query(params) {
    return Object.keys(params || {})
      .filter(function (k) {
        return params[k] !== null && params[k] !== undefined && params[k] !== "";
      })
      .map(function (k) {
        return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]);
      })
      .join("&");
  }

  function getJSON(url) {
    return fetch(url).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok || data.status === "error") {
          throw new Error(data.error || ("HTTP " + res.status));
        }
        return data;
      });
    });
  }

  function fetchCalls(params) {
    return getJSON(API.list + "?" + query(params || {}));
  }

  function fetchDetail(callId) {
    return getJSON(API.detail + "?call_id=" + encodeURIComponent(callId));
  }

  function fetchWaveform(callId, buckets) {
    return getJSON(API.waveform + "?call_id=" + encodeURIComponent(callId) +
      "&buckets=" + (buckets || 240));
  }

  function fetchStats() {
    return getJSON(API.stats);
  }

  function audioUrl(callId) {
    return API.audio + "?call_id=" + encodeURIComponent(callId);
  }

  function exportUrl(params) {
    return API.exportCsv + "?" + query(params || {});
  }

  // ── Formatting ─────────────────────────────────────────────────────────────
  function formatDuration(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var mins = Math.floor(total / 60);
    var secs = total % 60;
    return mins + ":" + (secs < 10 ? "0" : "") + secs;
  }

  function formatClock(epochSeconds) {
    if (!epochSeconds) { return "--:--"; }
    var d = new Date(Number(epochSeconds) * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
  }

  var SENTIMENT_CLASS = { positive: "pos", neutral: "neu", negative: "neg", unknown: "neu" };

  function sentimentClass(sentiment) {
    return SENTIMENT_CLASS[sentiment] || "neu";
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  function renderTable(tbody, page, options) {
    var opts = options || {};
    if (!page.items.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No calls match these filters.</td></tr>';
      return;
    }
    tbody.innerHTML = page.items.map(function (call) {
      return '<tr data-call="' + escapeHtml(call.call_id) + '">' +
        '<td class="mono">' + formatClock(call.started_at) + "</td>" +
        '<td class="mono">' + escapeHtml(call.from_number || call.call_id.slice(0, 22)) + "</td>" +
        "<td>" + escapeHtml(call.agent_name) + "</td>" +
        '<td class="mono">' + formatDuration(call.duration_seconds) + "</td>" +
        '<td><span class="pill ' + sentimentClass(call.sentiment) + '">' +
          escapeHtml(call.sentiment) + "</span></td>" +
        "<td>" + escapeHtml(call.outcome) + "</td>" +
        '<td class="mono">' + call.turns + "</td>" +
        '<td class="mono">' + (call.has_audio ? "audio" : "—") + "</td>" +
        "</tr>";
    }).join("");

    if (opts.onSelect) {
      Array.prototype.forEach.call(tbody.querySelectorAll("tr[data-call]"), function (row) {
        row.addEventListener("click", function () { opts.onSelect(row.dataset.call); });
      });
    }
  }

  function renderPager(el, page, onPage) {
    el.innerHTML =
      '<button class="pg" data-page="' + (page.page - 1) + '"' + (page.has_prev ? "" : " disabled") + ">Prev</button>" +
      '<span class="pg-info">Page ' + page.page + " of " + page.pages +
      " &middot; " + page.total + " calls</span>" +
      '<button class="pg" data-page="' + (page.page + 1) + '"' + (page.has_next ? "" : " disabled") + ">Next</button>";
    if (onPage) {
      Array.prototype.forEach.call(el.querySelectorAll("button[data-page]"), function (btn) {
        btn.addEventListener("click", function () {
          if (!btn.disabled) { onPage(Number(btn.dataset.page)); }
        });
      });
    }
  }

  /**
   * Draws the stereo envelope into an SVG: caller above the axis, agent below,
   * with the played portion filled in and a playhead line.
   */
  function renderWaveform(svg, waveform, progress) {
    var width = 700, height = 96, mid = height / 2;
    var caller = waveform.caller || [];
    var agent = waveform.agent || [];
    var buckets = Math.max(caller.length, agent.length, 1);
    var step = width / buckets;
    var played = Math.round(buckets * Math.max(0, Math.min(1, progress || 0)));
    var parts = [];

    for (var i = 0; i < buckets; i++) {
      var x = (i * step).toFixed(2);
      var w = Math.max(step - 0.6, 0.6).toFixed(2);
      var fill = i < played ? "var(--accent)" : "var(--line, #444)";
      var up = Math.max((caller[i] || 0) * (mid - 4), 1);
      var down = Math.max((agent[i] || 0) * (mid - 4), 1);
      parts.push('<rect x="' + x + '" y="' + (mid - up).toFixed(2) + '" width="' + w +
        '" height="' + up.toFixed(2) + '" fill="' + fill + '" opacity="0.95"/>');
      parts.push('<rect x="' + x + '" y="' + mid.toFixed(2) + '" width="' + w +
        '" height="' + down.toFixed(2) + '" fill="' + fill + '" opacity="0.55"/>');
    }
    parts.push('<line x1="0" y1="' + mid + '" x2="' + width + '" y2="' + mid +
      '" stroke="var(--line, #444)" stroke-width="0.5"/>');
    var head = (played * step).toFixed(2);
    parts.push('<line x1="' + head + '" y1="0" x2="' + head + '" y2="' + height +
      '" stroke="var(--ink, #fff)" stroke-width="1.5"/>');

    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.innerHTML = parts.join("");
  }

  function renderTranscript(el, timeline, activeIndex, onSeek) {
    if (!timeline.length) {
      el.innerHTML = '<div class="empty">No transcript recorded for this call.</div>';
      return;
    }
    el.innerHTML = timeline.map(function (turn, index) {
      var flag = turn.is_frustrated ? ' <span class="pill neg">frustrated</span>' : "";
      return '<div class="turn ' + (turn.role === "agent" ? "agent" : "caller") +
        (index === activeIndex ? " active" : "") + '" data-seek="' + turn.start_s +
        '" data-index="' + index + '">' +
        '<span class="t">' + formatDuration(turn.start_s) + "</span>" +
        "<div><div class=\"who\">" + escapeHtml(turn.speaker) + flag + "</div>" +
        "<p>" + escapeHtml(turn.text) + "</p></div></div>";
    }).join("");

    if (onSeek) {
      Array.prototype.forEach.call(el.querySelectorAll("[data-seek]"), function (node) {
        node.addEventListener("click", function () { onSeek(Number(node.dataset.seek)); });
      });
    }
  }

  function activeTurnIndex(timeline, currentTime) {
    for (var i = 0; i < timeline.length; i++) {
      if (currentTime >= timeline[i].start_s && currentTime < timeline[i].end_s) { return i; }
    }
    return timeline.length && currentTime >= timeline[timeline.length - 1].end_s
      ? timeline.length - 1 : -1;
  }

  /**
   * Wires an <audio> element to a waveform, a clock and a transcript so all
   * three stay on the same moment of the call.
   */
  function createPlayer(refs) {
    var audio = refs.audio;
    var state = { callId: null, timeline: [], waveform: null, activeIndex: -1, duration: 0 };

    function paint() {
      var progress = state.duration ? audio.currentTime / state.duration : 0;
      if (state.waveform && refs.waveform) {
        renderWaveform(refs.waveform, state.waveform, progress);
      }
      if (refs.clock) {
        refs.clock.textContent = formatDuration(audio.currentTime) + " / " + formatDuration(state.duration);
      }
      var index = activeTurnIndex(state.timeline, audio.currentTime);
      if (index !== state.activeIndex && refs.transcript) {
        state.activeIndex = index;
        renderTranscript(refs.transcript, state.timeline, index, seek);
      }
    }

    function seek(seconds) {
      if (!state.callId) { return; }
      audio.currentTime = Math.max(0, Math.min(seconds, state.duration || seconds));
      paint();
    }

    // Recordings are dual-channel (caller left, agent right) so the waveform can show who spoke.
    // For listening, mix both voices into both ears; refs.split (a checkbox) restores the raw L/R.
    var mix = null;
    function ensureMix() {
      if (mix || !(window.AudioContext || window.webkitAudioContext)) { return; }
      try {
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        var src = ctx.createMediaElementSource(audio);
        var splitter = ctx.createChannelSplitter(2);
        var merger = ctx.createChannelMerger(2);
        var gL = ctx.createGain(), gR = ctx.createGain();   // caller / agent
        src.connect(splitter);
        splitter.connect(gL, 0); splitter.connect(gR, 1);
        gL.connect(merger, 0, 0); gL.connect(merger, 0, 1);
        gR.connect(merger, 0, 0); gR.connect(merger, 0, 1);
        merger.connect(ctx.destination);
        var raw = ctx.createGain(); src.connect(raw);         // untouched stereo path
        raw.gain.value = 0;
        raw.connect(ctx.destination);
        mix = { ctx: ctx, mixed: [gL, gR], raw: raw };
        applySplit();
      } catch (e) { console.warn("Recording downmix unavailable, playing raw stereo:", e); }
    }
    function applySplit() {
      if (!mix) { return; }
      var split = !!(refs.split && refs.split.checked);
      mix.mixed[0].gain.value = split ? 0 : 0.7;
      mix.mixed[1].gain.value = split ? 0 : 0.7;
      mix.raw.gain.value = split ? 1 : 0;
    }
    audio.addEventListener("play", function () { ensureMix(); if (mix && mix.ctx.state === "suspended") { mix.ctx.resume(); } });
    if (refs.split) { refs.split.addEventListener("change", applySplit); }

    audio.addEventListener("timeupdate", paint);
    audio.addEventListener("loadedmetadata", function () {
      if (isFinite(audio.duration) && audio.duration > 0) { state.duration = audio.duration; }
      paint();
    });

    if (refs.waveform) {
      refs.waveform.addEventListener("click", function (event) {
        var box = refs.waveform.getBoundingClientRect();
        seek(((event.clientX - box.left) / box.width) * (state.duration || 0));
      });
    }

    return {
      state: state,
      seek: seek,
      play: function () { return audio.play(); },
      pause: function () { audio.pause(); },
      toggle: function () { return audio.paused ? audio.play() : audio.pause(); },
      load: function (callId, detail) {
        state.callId = callId;
        state.timeline = detail.timeline || [];
        state.activeIndex = -1;
        state.duration = detail.call.duration_seconds || 0;
        audio.src = detail.audio && detail.audio.available ? audioUrl(callId) : "";
        if (refs.transcript) { renderTranscript(refs.transcript, state.timeline, -1, seek); }
        if (!detail.audio || !detail.audio.available) {
          state.waveform = null;
          if (refs.waveform) { refs.waveform.innerHTML = ""; }
          paint();
          return Promise.resolve(null);
        }
        var currentCallId = callId;
        return fetchWaveform(callId).then(function (data) {
          if (state.callId !== currentCallId) { return null; }
          state.waveform = (data && data.waveform) ? data.waveform : null;
          if (state.waveform && state.waveform.duration_seconds) { state.duration = state.waveform.duration_seconds; }
          paint();
          return state.waveform;
        }).catch(function () {
          if (state.callId !== currentCallId) { return null; }
          state.waveform = null;
          if (refs.waveform) { refs.waveform.innerHTML = ""; }
          paint();
          return null;
        });
      }
    };
  }

  // In-app confirmation dialog. Replaces the native confirm(), which browsers silently suppress
  // after a few dismissals ("don't let this page create more dialogs") — that made buttons like
  // "Remove number" look dead. Returns a Promise that resolves true (confirmed) / false (cancelled).
  // Self-contained: builds its own overlay + styles, so it works on any page and can't be blocked.
  global.uiConfirm = function (message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var prev = document.getElementById("uiConfirmOverlay");
      if (prev) { prev.remove(); }
      var ov = document.createElement("div");
      ov.id = "uiConfirmOverlay";
      ov.setAttribute("role", "dialog");
      ov.setAttribute("aria-modal", "true");
      ov.style.cssText = "position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;" +
        "justify-content:center;background:rgba(10,12,20,.55);padding:20px;";
      var danger = opts.danger !== false;
      var accent = danger ? "#A32F26" : "#0E7B6C";
      ov.innerHTML =
        '<div style="background:#fff;color:#131725;max-width:420px;width:100%;border-radius:10px;' +
        'box-shadow:0 12px 48px rgba(0,0,0,.35);font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;overflow:hidden">' +
        '<div style="padding:20px 22px 6px;font-size:16px;font-weight:700">' + (opts.title || "Please confirm") + '</div>' +
        '<div style="padding:0 22px 18px;font-size:13.5px;line-height:1.5;color:#4E556B" id="uiConfirmMsg"></div>' +
        '<div style="display:flex;gap:10px;justify-content:flex-end;padding:14px 22px;background:#F5F6F8;border-top:1px solid #E4E7EC">' +
          '<button type="button" id="uiConfirmCancel" style="padding:8px 16px;border-radius:6px;border:1px solid #D0D5DD;' +
          'background:#fff;color:#344054;font-size:13px;font-weight:600;cursor:pointer">' + (opts.cancelText || "Cancel") + '</button>' +
          '<button type="button" id="uiConfirmOk" style="padding:8px 16px;border-radius:6px;border:1px solid ' + accent + ';' +
          'background:' + accent + ';color:#fff;font-size:13px;font-weight:600;cursor:pointer">' + (opts.okText || "Confirm") + '</button>' +
        '</div>' +
      '</div>';
      document.body.appendChild(ov);
      ov.querySelector("#uiConfirmMsg").textContent = message;  // textContent = no HTML injection
      var okBtn = ov.querySelector("#uiConfirmOk");
      var done = function (val) { ov.remove(); document.removeEventListener("keydown", onKey); resolve(val); };
      function onKey(e) {
        if (e.key === "Escape") {
          done(false);
        } else if (e.key === "Enter" && document.activeElement === okBtn) {
          done(true);
        }
      }
      okBtn.addEventListener("click", function () { done(true); });
      ov.querySelector("#uiConfirmCancel").addEventListener("click", function () { done(false); });
      ov.addEventListener("click", function (e) { if (e.target === ov) { done(false); } });
      document.addEventListener("keydown", onKey);
      okBtn.focus();
    });
  };

  global.CallInspector = {
    api: API,
    fetchCalls: fetchCalls,
    fetchDetail: fetchDetail,
    fetchWaveform: fetchWaveform,
    fetchStats: fetchStats,
    audioUrl: audioUrl,
    exportUrl: exportUrl,
    formatDuration: formatDuration,
    formatClock: formatClock,
    escapeHtml: escapeHtml,
    sentimentClass: sentimentClass,
    renderTable: renderTable,
    renderPager: renderPager,
    renderWaveform: renderWaveform,
    renderTranscript: renderTranscript,
    activeTurnIndex: activeTurnIndex,
    createPlayer: createPlayer
  };
})(typeof window !== "undefined" ? window : this);
