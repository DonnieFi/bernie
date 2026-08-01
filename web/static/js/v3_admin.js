/* Bernie · Admin shell (phase-41) — thin mount; subtabs expanded in 7kw */
(function () {
  const SUBTABS = [
    { id: "usage", label: "Usage", legacyPanel: "panel-activity" },
    { id: "network", label: "Network", legacyPanel: "panel-network" },
    { id: "models", label: "Models", legacyPanel: "panel-settings" },
    { id: "config", label: "Config", legacyPanel: "panel-config" },
    { id: "logs", label: "Logs", legacyPanel: "panel-logs" },
    { id: "system", label: "System", legacyPanel: null },
  ];

  const RENDERERS = {
    usage: () => window.renderActivity && window.renderActivity(),
    network: () => window.renderNetwork && window.renderNetwork(),
    models: () => window.renderSettings && window.renderSettings(),
    config: () => window.renderConfig && window.renderConfig(),
    logs: () => window.renderLogs && window.renderLogs(),
    system: () => renderSystem(),
  };

  let _tab = localStorage.getItem("bernie-admin-tab") || "usage";

  // Reparents legacy panel nodes into Admin subtabs at runtime. Renderers must
  // re-query #panel-* on each render — do not cache root refs at init time.
  function ensureLegacyHost(subtabId, legacyPanelId) {
    const root = document.getElementById("panel-admin");
    const sub = root && root.querySelector(`.admin-subpanel[data-subtab="${subtabId}"]`);
    if (!sub || !legacyPanelId) return null;
    let legacy = document.getElementById(legacyPanelId);
    if (!legacy) {
      legacy = document.createElement("div");
      legacy.id = legacyPanelId;
      legacy.className = "page page-fade";
      sub.appendChild(legacy);
    } else if (!sub.contains(legacy)) {
      sub.appendChild(legacy);
    }
    legacy.style.display = "";
    return legacy;
  }

  function renderSystem() {
    const host = document.querySelector("#panel-admin .admin-subpanel[data-subtab='system']");
    if (!host) return;
    host.innerHTML = `<div class="page-hdr"><h2>System</h2><p class="sub">Background scheduler status</p></div><div id="admin-system-body" class="metrics">Loading…</div>`;
    const body = host.querySelector("#admin-system-body");
    if (!window.api) return;
    window.api("/api/scheduler").then(d => {
      if (!body) return;
      const tasks = (d && d.tasks) || d || {};
      const rows = Object.entries(tasks).map(([k, v]) => {
        const st = v && (v.running ? "running" : v.enabled === false ? "off" : "idle");
        return `<div class="run-row"><span class="o">${k}</span><span>${st || "—"}</span></div>`;
      }).join("");
      body.innerHTML = rows || "<div class='metrics'>No scheduler tasks reported.</div>";
    }).catch(() => { if (body) body.textContent = "Could not load scheduler status."; });
  }

  function showSubtab(id) {
    const prev = _tab;
    if (prev === "logs" && id !== "logs" && typeof window.v3LogsCleanup === "function") {
      window.v3LogsCleanup();
    }
    _tab = id;
    localStorage.setItem("bernie-admin-tab", id);
    const root = document.getElementById("panel-admin");
    if (!root) return;
    root.querySelectorAll(".admin-tab").forEach(btn => {
      btn.classList.toggle("on", btn.dataset.subtab === id);
    });
    root.querySelectorAll(".admin-subpanel").forEach(p => {
      p.style.display = p.dataset.subtab === id ? "" : "none";
    });
    const meta = SUBTABS.find(t => t.id === id);
    if (meta && meta.legacyPanel) ensureLegacyHost(id, meta.legacyPanel);
    const fn = RENDERERS[id];
    if (fn) fn();
  }

  function render() {
    const root = document.getElementById("panel-admin");
    if (!root) return;
    if (!root.dataset.wired) {
      root.innerHTML = `
        <div class="page-hdr" style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
          <div><h2>Admin</h2><p class="sub">Operator tools · admin/parents only</p></div>
          <button type="button" class="admin-tab" id="admin-openwebui">Chat (OpenWebUI) ↗</button>
        </div>
        <div class="admin-tabs">${SUBTABS.map(t =>
          `<button type="button" class="admin-tab" data-subtab="${t.id}">${t.label}</button>`
        ).join("")}</div>
        ${SUBTABS.map(t =>
          `<div class="admin-subpanel page-fade" data-subtab="${t.id}" style="display:none"></div>`
        ).join("")}`;
      root.querySelectorAll(".admin-tab[data-subtab]").forEach(btn => {
        btn.addEventListener("click", () => showSubtab(btn.dataset.subtab));
      });
      const ow = root.querySelector("#admin-openwebui");
      if (ow) ow.addEventListener("click", () => { if (window.openBernieChat) window.openBernieChat(); });
      root.dataset.wired = "1";
    }
    showSubtab(_tab);
  }

  window.renderAdmin = render;
  window.v3AdminLeave = function () {
    if (_tab === "logs" && typeof window.v3LogsCleanup === "function") {
      window.v3LogsCleanup();
    }
  };
})();
