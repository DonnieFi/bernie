(function() {
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (...args) => window.el(...args);

  const LVL_STYLES = {
    DEBUG:   { bg: 'rgba(139,130,117,.14)', fg: 'var(--ink-3)' },
    INFO:    { bg: 'rgba(107,155,198,.14)', fg: 'var(--info)'  },
    WARNING: { bg: 'rgba(217,152,83,.18)',  fg: 'var(--warn)'  },
    ERROR:   { bg: 'rgba(200,114,101,.2)',  fg: 'var(--err)'   },
  };
  const ALL_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];
  const LOG_CAP = 1000;

  let _logLines = [];
  let _active = (() => {
    try {
      const raw = JSON.parse(localStorage.getItem("bernie-logs-levels") || "null");
      if (raw && typeof raw === "object" && !Array.isArray(raw)) return raw;
    } catch {}
    return { DEBUG: false, INFO: true, WARNING: true, ERROR: true };
  })();
  let _q = "";
  let _loading = false;

  function parseLine(raw) {
    // Expected: "YYYY-MM-DD HH:MM:SS,mmm LEVEL module: message"
    const m = raw.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+([^:]+):\s*(.*)/);
    if (m) {
      const lvl = m[3] === "CRITICAL" ? "ERROR" : m[3];
      return { date: m[1], time: m[2], lvl, mod: m[4].trim(), msg: m[5], raw };
    }
    const lvl = ALL_LEVELS.find(l => raw.includes(l)) || "INFO";
    return { date: "", time: "", lvl, mod: "", msg: raw, raw };
  }

  function passes(line) {
    const p = parseLine(line);
    if (!_active[p.lvl]) return false;
    if (_q) {
      const s = _q.toLowerCase();
      return p.msg.toLowerCase().includes(s) || p.mod.toLowerCase().includes(s);
    }
    return true;
  }

  function buildRow(line, isFirst) {
    const p = parseLine(line);
    const s = LVL_STYLES[p.lvl] || LVL_STYLES.INFO;
    return el("div", {
      style: `display: grid; grid-template-columns: 92px 76px 200px 1fr; gap: 12px; padding: 6px 14px; font-family: var(--font-mono); font-size: 12.5px; line-height: 1.5; border-top: ${isFirst ? "none" : "1px solid rgba(255,255,255,.025)"};`
    },
      el("span", { style: "color: var(--ink-4)" }, p.date),
      el("span", { style: "color: var(--ink-3)" }, p.time),
      el("span", { style: "display: flex; align-items: center" },
        el("span", {
          style: `background: ${s.bg}; color: ${s.fg}; padding: 1px 7px; border-radius: 4px; font-size: 10px; letter-spacing: .06em; min-width: 60px; text-align: center; font-weight: 600; display: inline-block`
        }, p.lvl)
      ),
      el("span", { style: "color: var(--ink-2)" },
        p.mod ? el("span", { style: "color: var(--ink-3)" }, p.mod + ": ") : null,
        p.msg
      )
    );
  }

  function redrawRows() {
    const wrap = $("#v3-log-rows");
    if (!wrap) return;
    wrap.innerHTML = "";
    const visible = _logLines.filter(passes);
    visible.forEach((line, i) => wrap.append(buildRow(line, i === 0)));
  }

  function setStatus(text, ok) {
    const lbl = $("#v3-log-status-lbl");
    const dot = $("#v3-log-status-dot");
    if (lbl) lbl.textContent = text;
    if (dot) dot.style.background = ok ? "var(--ok)" : "var(--ink-4)";
  }

  async function loadLogs() {
    if (_loading) return;
    _loading = true;
    setStatus("LOADING…", false);
    try {
      const data = await window.api("/api/logs?n=300");
      const lines = Array.isArray(data.lines) ? data.lines : [];
      // newest first (file order is oldest→newest)
      _logLines = lines.slice().reverse().slice(0, LOG_CAP);
      redrawRows();
      setStatus(`${_logLines.length} lines`, true);
    } catch (e) {
      setStatus(e.message || "FAILED", false);
      if (window.flashBernie) window.flashBernie("Could not load logs.");
    } finally {
      _loading = false;
    }
  }

  // no-op cleanup (was disconnect WS)
  window.v3LogsCleanup = function () {};

  function renderLogs() {
    const root = $("#panel-logs");
    if (!root) return;

    root.innerHTML = "";
    root.className = "page page-fade";
    root.style.maxWidth = "1100px";

    const filterQ = el("input", {
      class: "input",
      style: "width: 220px",
      placeholder: "filter…",
      value: _q,
    });
    filterQ.addEventListener("input", e => { _q = e.target.value; redrawRows(); });

    const levelBtns = ALL_LEVELS.map(lvl => {
      const s = LVL_STYLES[lvl];
      const btn = el("button", { class: "btn" }, lvl);
      const refresh = () => {
        btn.style.background    = _active[lvl] ? s.bg : "transparent";
        btn.style.borderColor   = _active[lvl] ? "transparent" : "var(--stroke)";
        btn.style.color         = _active[lvl] ? s.fg : "var(--ink-3)";
        btn.style.fontFamily    = "var(--font-mono)";
        btn.style.fontSize      = "11px";
        btn.style.letterSpacing = ".06em";
        btn.style.padding       = "4px 10px";
      };
      refresh();
      btn.addEventListener("click", () => {
        _active[lvl] = !_active[lvl];
        localStorage.setItem("bernie-logs-levels", JSON.stringify(_active));
        refresh();
        redrawRows();
      });
      return btn;
    });

    const statusDot = el("span", {
      id: "v3-log-status-dot",
      style: "width: 6px; height: 6px; border-radius: 3px; background: var(--ink-4); display: inline-block"
    });
    const statusLbl = el("span", {
      id: "v3-log-status-lbl",
      class: "t-mono",
      style: "font-size: 11px; color: var(--ink-4)"
    }, "—");

    const refreshBtn = el("button", {
      class: "btn",
      onclick: () => loadLogs(),
    }, "Refresh");
    const clearBtn = el("button", {
      class: "btn btn-ghost",
      onclick: () => { _logLines = []; redrawRows(); setStatus("cleared", false); },
    }, "Clear");

    const toolbar = el("div", { class: "row between", style: "padding: 12px 14px; border-bottom: 1px solid var(--stroke); gap: 12px; flex-wrap: wrap;" },
      filterQ,
      el("div", { class: "row gap-2", style: "flex-wrap: wrap" }, ...levelBtns),
      el("div", { class: "row gap-2" },
        refreshBtn,
        clearBtn,
        el("div", { class: "row gap-2", style: "margin-left: 4px" }, statusDot, statusLbl)
      )
    );

    const rowWrap = el("div", {
      id: "v3-log-rows",
      style: "background: #100d0a; max-height: 520px; overflow-y: auto; overscroll-behavior: contain;"
    });

    root.append(
      el("div", {},
        el("div", { class: "t-eyebrow" }, "Diagnostics"),
        el("div", { class: "t-h1", style: "font-size: 28px; font-family: var(--font-serif); font-weight: 400; margin-top: 4px" }, "Logs")
      ),
      el("div", { class: "card", style: "overflow: hidden" },
        toolbar,
        rowWrap
      )
    );

    redrawRows();
    loadLogs();
  }

  window.renderLogs = renderLogs;
})();
