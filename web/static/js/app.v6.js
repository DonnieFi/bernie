// ==========================================================================
// Bernie — App shell: render + navigation + data loading
// ==========================================================================

(function () {
  const DEFAULT_OPENWEBUI_URL = "https://ai.lan/";

  function getOpenWebUIUrl() {
    const fromMe = window.Me && typeof window.Me.openwebui_url === "string"
      ? window.Me.openwebui_url.trim()
      : "";
    return fromMe || DEFAULT_OPENWEBUI_URL;
  }

  // Bumped after successful mutations so subsequent GETs can opt-in to a shared bust key.
  // Avoids Date.now() on every GET (which killed browser caching of API responses).
  let _apiMutateEpoch = 0;

  const api = (path, opts = {}) => {
    const t = localStorage.getItem("bernie-token") || "";
    let body = opts.body;
    let autoHeaders = {};

    // Only stringify if it's a plain object and not a string/formdata/blob
    if (body !== undefined && body !== null && typeof body === "object" && !(body instanceof FormData) && !(body instanceof Blob)) {
      body = JSON.stringify(body);
      autoHeaders["Content-Type"] = "application/json";
    }

    const mergedHeaders = {
      "X-Bernie-Token": t,
      ...autoHeaders,
      ...(opts.headers || {})
    };

    const method = (opts.method || "GET").toUpperCase();
    const isGet = method === "GET";
    // No per-request Date.now bust. After a mutation, append a stable epoch so GETs
    // see fresh data without making every GET uncacheable forever.
    let url = path;
    if (isGet && _apiMutateEpoch) {
      url += (path.includes("?") ? "&" : "?") + "_=" + _apiMutateEpoch;
    }

    return fetch(url, { ...opts, body, headers: mergedHeaders }).then(async r => {
      if ((r.status === 403 || r.status === 401) && !path.includes("/api/auth/") && !path.includes("/api/me")) {
        console.warn("Auth failure for " + path + ", status: " + r.status);
        localStorage.removeItem("bernie-token");
        location.reload();
      }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        const err = new Error(body.detail || body.error || `HTTP ${r.status}`);
        err.status = r.status;
        throw err;
      }
      if (!isGet) _apiMutateEpoch = Date.now();
      return r.json().catch(() => ({ error: "Failed to parse response" }));
    });
  };

  const D  = window.BernieData || {};
  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else if (v !== false && v != null) n.setAttribute(k, v);
    }
    for (const k of kids.flat()) {
      if (k == null || k === false) continue;
      n.append(k.nodeType ? k : document.createTextNode(k));
    }
    return n;
  };

  // Export shared helpers for v3 modules.
  window.api = api;
  window.$   = $;
  window.$$  = $$;
  window.el  = el;

  /** UI gate only — server guards API routes via verify_token + role checks. */
  function isAdminUser() {
    const role = (window.Me && window.Me.role) || "";
    return role === "admin" || role === "parents";
  }

  // Nav ids (plan/people/security) → legacy panel DOM ids (tasks/family/cameras).
  const PANEL_DOM_ID = {
    plan: "tasks",
    people: "family",
    security: "cameras",
  };
  const PANEL_NAV_ID = Object.fromEntries(
    Object.entries(PANEL_DOM_ID).map(([nav, dom]) => [dom, nav])
  );
  const LEGACY_PANEL = {
    tasks: "plan",
    family: "people",
    cameras: "security",
    chat: "today",
    activity: "admin",
    network: "admin",
    settings: "admin",
    logs: "admin",
    config: "admin",
    cognition: "today",
  };

  function normalizePanelId(id) {
    if (!id) return "today";
    return LEGACY_PANEL[id] || id;
  }

  function domPanelId(navId) {
    return PANEL_DOM_ID[navId] || navId;
  }

  const NAV = [
    { id: "today",    label: "Today",    icon: "sun"      },
    { id: "home",     label: "Home",     icon: "home"     },
    { id: "plan",     label: "Plan",     icon: "tasks"    },
    { id: "people",   label: "People",   icon: "family"   },
    { id: "security", label: "Security", icon: "camera"   },
    { id: "admin",    label: "Admin",    icon: "settings", adminOnly: true },
  ];

  function visibleNav() {
    return NAV.filter(n => !n.adminOnly || isAdminUser());
  }

  /** Mobile ≤768px: five family tabs + optional More (Admin). */
  function shellNavGroups() {
    const all = visibleNav();
    const mobile = typeof window !== "undefined" && window.matchMedia("(max-width: 768px)").matches;
    if (!mobile || all.length <= 5) return { primary: all, overflow: [] };
    const overflow = all.filter(n => n.id === "admin");
    const primary = all.filter(n => n.id !== "admin");
    return { primary, overflow };
  }

  function renderNavItem(n, activePanel, collapsed, counts) {
    return el("button", {
      type: "button",
      class: `nav-item ${n.id === activePanel ? "active" : ""}`,
      "data-panel": n.id,
      "aria-current": n.id === activePanel ? "page" : "false",
      onclick: () => showPanel(n.id),
      title: collapsed ? n.label : "",
    },
      el("div", { html: ICONS[n.icon] || "" }),
      !collapsed && el("span", {}, n.label),
      !collapsed && el("span", { class: "num" }, counts[n.id] || "")
    );
  }

  const ICONS = {
    sun:      `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/></svg>`,
    chat:     `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`,
    home:     `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11 12 4l9 7"/><path d="M5 10v9h14v-9"/></svg>`,
    family:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2.2"/><path d="M3 19c0-3.3 2.7-6 6-6s6 2.7 6 6"/><path d="M14.5 19c0-2.3 1.6-4 3.5-4s3 1.4 3 3.5"/></svg>`,
    tasks:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><path d="m9 12 2 2 4-4"/></svg>`,
    activity: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2-6 4 12 2-6h6"/></svg>`,
    network:  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="16" width="4" height="4" rx="1"/><rect x="10" y="16" width="4" height="4" rx="1"/><rect x="18" y="16" width="4" height="4" rx="1"/><rect x="10" y="4" width="4" height="4" rx="1"/><path d="M12 8v4M4 16v-2a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2"/></svg>`,
    settings: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a7.97 7.97 0 0 0 .1-6l1.7-1-2-3.5-2 .8a8 8 0 0 0-5-2.9L12 .5h-4l-.2 1.9a8 8 0 0 0-5 2.9l-2-.8-2 3.5 1.7 1a7.97 7.97 0 0 0 .1 6L-1.2 16l2 3.5 2-.8a8 8 0 0 0 5 2.9L8 23.5h4l.2-1.9a8 8 0 0 0 5-2.9l2 .8 2-3.5z" transform="translate(2 0) scale(.85)"/></svg>`,
    logs:     `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 10h12M4 14h16M4 18h10"/></svg>`,
    config:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h11l3 3v15H5z"/><path d="M16 3v3h3"/><path d="M9 12h6M9 16h6"/></svg>`,
    camera:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>`,
  };

  function renderShell() {
    const savedTheme = localStorage.getItem("bernie-theme") || "dark";
    document.body.dataset.theme = savedTheme;

    const activePanel = normalizePanelId(_activePanel || localStorage.getItem("bernie-panel") || "today");
    const collapsed = localStorage.getItem("bernie-sidebar-collapsed") === "true";
    const appRoot = document.getElementById("app");
    if (!appRoot) return;

    const isLight = document.body.dataset.theme === "light";
    const themeIcon = isLight
      ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`
      : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>`;

    const toggleTheme = () => {
      const next = document.body.dataset.theme === "dark" ? "light" : "dark";
      document.body.dataset.theme = next;
      localStorage.setItem("bernie-theme", next);
      renderShell();
    };

    const counts = { today: "", home: "", network: "" };
    if (D.rooms) {
      try {
        const allLights = D.rooms.flatMap(r => r.lights || []);
        counts.home = String(allLights.filter(l => l.on).length || "");
      } catch {}
    }
    if (D.network) {
      try {
        counts.network = String((D.network || []).filter(d => !d.display_name).length || "");
      } catch {}
    }

    // Surgical update: only create if missing
    let sidebar = appRoot.querySelector(".sidebar");
    let main = appRoot.querySelector("#main-content");

    if (!sidebar || !main) {
      appRoot.innerHTML = "";
      sidebar = el("aside", { class: `sidebar${collapsed ? " collapsed" : ""}` });
      main = el("main", { class: "main", id: "main-content" });
      appRoot.append(sidebar, main);
    } else {
      sidebar.classList.toggle("collapsed", collapsed);
    }

    // Adjust main grid column for collapsed state
    appRoot.classList.toggle("sidebar-collapsed", collapsed);

    // Sidebar: Identity
    sidebar.innerHTML = "";
    const toggleSidebar = () => {
      const next = !collapsed;
      localStorage.setItem("bernie-sidebar-collapsed", next ? "true" : "false");
      renderShell();
    };

    sidebar.append(
      el("div", { class: "identity" },
        el("div", { class: "avatar" }, el("img", { src: "/static/bernie-avatar.png?v=80", alt: "Bernie" })),
        el("div", { class: "col" },
          el("div", { class: "name" }, "bernie"),
          el("div", { class: "loc" }, D.weather?.location_label || "Halifax")
        ),
        el("div", { class: "sidebar-controls" },
          el("button", { class: "theme-toggle", onclick: themeToggle, html: themeIcon }),
          el("button", {
            class: `collapse-toggle ${collapsed ? "is-collapsed" : ""}`,
            onclick: toggleSidebar,
            title: collapsed ? "Expand sidebar" : "Collapse sidebar",
            html: collapsed 
              ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>`
              : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>`
          })
        )
      ),
      el("nav", { class: "nav", "aria-label": "Main" },
        (() => {
          const { primary, overflow } = shellNavGroups();
          const items = primary.map(n => renderNavItem(n, activePanel, collapsed, counts));
          if (overflow.length) {
            const moreActive = overflow.some(n => n.id === activePanel);
            items.push(el("button", {
              type: "button",
              class: `nav-item nav-more ${moreActive ? "active" : ""}`,
              "aria-label": "More",
              "aria-current": moreActive ? "page" : "false",
              onclick: () => showPanel(overflow[0].id),
              title: overflow.map(n => n.label).join(", "),
            },
              el("div", { html: ICONS.settings || "" }),
              !collapsed && el("span", {}, "More")
            ));
          }
          return items;
        })()
      ),
      !collapsed && el("div", { class: "bot-status" },
        el("div", { class: "row" },
          el("span", { class: "dot" }),
          el("span", {}, `bernie · up ${D.health?.uptime || "—"}`)
        ),
        el("div", { class: "meta" }, D.health?.bot_connected ? "DISCORD · CONNECTED" : "DISCORD · OFFLINE"),
        el("div", { class: "meta", style: "margin-top: 6px;" }, (D.health?.model || "model · unknown").toUpperCase())
      )
    );

    function themeToggle() { toggleTheme(); }

    // Panels: ensure mounts exist; visibility owned by showPanel() only.
     visibleNav().forEach(n => {
       const domId = domPanelId(n.id);
       const panelId = n.id === "admin" ? "panel-admin" : `panel-${domId}`;
       let panel = document.getElementById(panelId);
       if (!panel) {
         panel = document.createElement("div");
         panel.id = panelId;
         panel.classList.add("page", "page-fade");
         panel.style.display = "none";
         main.appendChild(panel);
       }
     });
  }
  window.renderShell = renderShell;

  function flashBernie(msg) {
    const n = el("div", { style: "position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: var(--bg-card-2); border: 1px solid var(--amber); color: var(--ink); padding: 10px 18px; border-radius: 999px; box-shadow: 0 4px 24px rgba(0,0,0,.4); font-family: var(--font-serif); font-style: italic; z-index: 500;" }, msg);
    document.body.append(n);
    setTimeout(() => n.remove(), 2200);
  }
  window.flashBernie = flashBernie;

  function _skelPanel(...rows) {
    const wrap = document.createElement("div");
    wrap.className = "skel-panel";
    for (const row of rows) wrap.appendChild(row);
    return wrap;
  }
  function _skelBar(w = "60%") {
    const d = document.createElement("div");
    d.className = "skel skel-bar";
    d.style.width = w;
    return d;
  }
  function _skelBlock() {
    const d = document.createElement("div");
    d.className = "skel skel-block";
    return d;
  }
  function _skelRow(...tiles) {
    const r = document.createElement("div");
    r.className = "skel-row";
    tiles.forEach(() => { const d = document.createElement("div"); d.className = "skel skel-tile"; r.appendChild(d); });
    return r;
  }

  function showSkeletons() {
    const panels = {
      "panel-today":    _skelPanel(_skelBlock(), _skelBar("40%"), _skelBar("70%"), _skelBar("50%")),
      "panel-home":     _skelPanel(_skelBlock(), _skelRow(1,2,3,4), _skelRow(1,2,3,4), _skelBar("30%"), _skelBlock()),
      "panel-family":   _skelPanel(_skelRow(1,2,3,4), _skelBar("40%"), _skelBlock()),
      "panel-activity": _skelPanel(_skelBar("30%"), ...[1,2,3,4,5,6].map(() => _skelBar("90%"))),
      "panel-network":  _skelPanel(_skelBar("25%"), _skelBar("100%"), ...[1,2,3,4,5,6,7,8].map(() => _skelBar("90%"))),
    };
    for (const [id, content] of Object.entries(panels)) {
      const el = document.getElementById(id);
      if (el && !el.children.length) { el.innerHTML = ""; el.appendChild(content); }
    }
  }

  /** 40b-style family prefetch on unlock (Today QA + Home dashboard). Admin stays panel-scoped. */
  async function loadFamilyBootstrap() {
    const safe = (p) => p.catch(e => { console.error("API call failed:", e); return null; });
    const [today, temps, rooms, dash, network, tasks] = await Promise.all([
      safe(api("/api/today")),
      safe(api("/api/temperatures")),
      safe(api("/api/rooms")),
      safe(api("/api/home/dashboard")),
      safe(api("/api/network/devices")),
      safe(api("/api/tasks?all_people=true")),
    ]);
    if (today) Object.assign(D, today);
    if (dash) Object.assign(D, dash);
    // Dedicated endpoints win over dashboard embeds (40b fetched both).
    if (temps) D.temps = temps;
    if (rooms) D.rooms = rooms;
    if (network) D.network = network;
    if (tasks) D.tasks = tasks;
    D._homeLoaded = true;
    D._homeLoadError = (dash || rooms)
      ? null
      : "Could not load Home Assistant data. Check that HA is up and bernie-api can reach it.";
  }

  async function refreshHomeData() {
    const safe = (p) => p.catch(e => { console.error("Home API failed:", e); return null; });
    const [dash, temps, rooms] = await Promise.all([
      safe(api("/api/home/dashboard")),
      safe(api("/api/temperatures")),
      safe(api("/api/rooms")),
    ]);
    let anyOk = false;
    if (dash) { Object.assign(D, dash); anyOk = true; }
    if (temps) { D.temps = temps; anyOk = true; }
    // Dedicated /api/rooms wins over dashboard.rooms (fresher floor map).
    if (rooms) { D.rooms = rooms; anyOk = true; }
    D._homeLoaded = true;
    D._homeLoadError = anyOk
      ? null
      : "Could not load Home Assistant data. Check that HA is up and bernie-api can reach it.";
    if (window.renderHome) window.renderHome(true);
    updateSidebarCounts();
    return anyOk;
  }
  window.refreshHomeData = refreshHomeData;

  async function refreshTodayData() {
    const safe = (p) => p.catch(e => { console.error("API call failed:", e); return null; });
    const [today, temps, rooms] = await Promise.all([
      safe(api("/api/today")),
      safe(api("/api/temperatures")),
      safe(api("/api/rooms")),
    ]);
    if (today) Object.assign(D, today);
    if (temps) D.temps = temps;
    if (rooms) D.rooms = rooms;
  }

  async function refreshAdminData() {
    const safe = (p) => p.catch(e => { console.error("API call failed:", e); return null; });
    const [settings, network, models, keys, activity, usage, notifLog] = await Promise.all([
      safe(api("/api/settings")),
      safe(api("/api/network/devices")),
      safe(api("/api/config/models")),
      safe(api("/api/keys/status")),
      safe(api("/api/activity?period=30d")),
      safe(api("/api/usage?days=30")),
      safe(api("/api/activity/notifications?limit=20")),
    ]);
    if (settings) D.settings = settings;
    if (network) D.network = network;
    if (models) D.models = models;
    if (keys) D.keys = keys;
    if (activity) D.activity = activity;
    if (usage) D.usage = usage;
    if (notifLog) D.notifLog = notifLog;
  }

  function syncPanelVisibility(activeId) {
    const main = document.getElementById("main-content");
    if (!main) return;
    visibleNav().forEach(n => {
      const domId = domPanelId(n.id);
      const panelId = n.id === "admin" ? "panel-admin" : `panel-${domId}`;
      const panel = document.getElementById(panelId);
      if (panel) panel.style.display = n.id === activeId ? "" : "none";
    });
    [...main.children].forEach(child => {
      if (!child.id || !child.id.startsWith("panel-")) return;
      const isNav = visibleNav().some(n => {
        const domId = domPanelId(n.id);
        return child.id === `panel-${domId}` || (n.id === "admin" && child.id === "panel-admin");
      });
      if (!isNav) child.style.display = "none";
    });
  }

  async function invokePanelRender(id) {
    if (id === "home" && window.renderHome) {
      await window.renderHome(true);
    } else {
      const renderFn = {
        today:     window.renderToday,
        plan:      window.renderTasks,
        people:    window.renderFamily,
        security:  window.renderCameras,
        admin:     window.renderAdmin,
      }[id];
      if (renderFn) await Promise.resolve(renderFn());
    }
    updateSidebarCounts();
  }

  function updateSidebarCounts() {
    const homeNav = document.querySelector('.nav-item[data-panel="home"] .num');
    if (homeNav && D.rooms) {
      try {
        const on = D.rooms.flatMap(r => r.lights || []).filter(l => l.on).length;
        homeNav.textContent = on ? String(on) : "";
      } catch (_) {}
    }
  }

  function connectWS() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const token = localStorage.getItem("bernie-token") || "";
    const ws = new WebSocket(`${protocol}//${location.host}/ws`);
    ws.onopen = () => ws.send(JSON.stringify({ token }));
    ws.onmessage = ({ data }) => {
      const e = JSON.parse(data);
      if (e.type === "presence.update" && D.presence) {
        const p = D.presence.find(x => x.id === e.id);
        if (p) {
          p.home = e.home; p.sub = e.sub; if (e.last_seen_ts) p.last_seen_ts = e.last_seen_ts;
          renderShell();
          updateSidebarCounts();
          if (window.renderToday)  window.renderToday();
          if (window.renderFamily) window.renderFamily();
        }
      }
      // ha_service + /api/lights both emit light.state {id, on, last}
      // Accept legacy light.update {entity_id, on} for older API processes.
      if ((e.type === "light.state" || e.type === "light.update") && D.rooms) {
        const lid = e.id || (e.entity_id
          ? String(e.entity_id).split(".").pop().replace(/_/g, "-")
          : null);
        if (lid) {
          const light = D.rooms.flatMap(r => r.lights || []).find(l => l.id === lid);
          if (light) {
            light.on = e.on;
            if (e.last) light.last = e.last;
            if (_activePanel === "home" && window.renderHome) window.renderHome(true);
            if (_activePanel === "today" && window.renderToday) window.renderToday();
            updateSidebarCounts();
          }
        }
      }
      if (e.type === "task.update") {
        // Re-fetch the full task list so all clients stay in sync
        api("/api/tasks?all_people=true").then(d => {
          if (d) { D.tasks = d; if (window.renderTasks) window.renderTasks(); }
        }).catch(() => {});
      }
    };
    ws.onclose = () => setTimeout(connectWS, 3000);
  }

  function applyBranding() {
    const fam = (window.Me && window.Me.family_name) || "Example";
    const label = String(fam).includes("Family") ? fam : `${fam} Family`;
    document.title = `Bernie · ${label} Home Agent`;
    const sub = document.querySelector("#login-overlay .brand-sub");
    if (sub) sub.textContent = label;
  }

  async function checkAuth() {
    const overlay = $("#login-overlay");
    const grid    = $("#login-avatar-grid");
    const form    = $("#login-form");
    const input   = $("#login-token");
    const err     = $("#login-err");
    const selUser = $("#login-selected-user");
    const backBtn = $("#login-back");
    let selectedPerson = null;

    const validateToken = async (t) => {
      try {
        const res = await fetch("/api/me", { headers: { "X-Bernie-Token": t } });
        if (!res.ok) return false;
        window.Me = await res.json();
        applyBranding();
        return true;
      } catch (e) { return false; }
    };

    const unlock = async () => {
      overlay.style.display = "none";
      await loadInitial();
      const hashPanel = normalizePanelId((location.hash || "").replace(/^#/, ""));
      let startPanel = normalizePanelId(localStorage.getItem("bernie-panel") || "today");
      if (hashPanel && hashPanel !== "today") startPanel = hashPanel;
      if (startPanel === "admin" && !isAdminUser()) {
        await showPanel("today");
      } else {
        await showPanel(startPanel);
      }
      connectWS();
    };

    const stored = localStorage.getItem("bernie-token");
    if (stored && await validateToken(stored)) return unlock();

    overlay.style.display = "flex";
    
    // Fetch users for grid
    try {
      const res = await fetch("/api/auth/users");
      if (res.ok) {
        const users = await res.json();
        grid.innerHTML = "";
        for (const u of users) {
          const btn = el("button", {
            style: `width: 80px; height: 80px; border-radius: 20px; border: none; background: var(--bg-card-2); color: ${u.color}; font-family: var(--font-serif); font-size: 32px; font-weight: 600; cursor: pointer; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 12px rgba(0,0,0,.2); transition: transform 0.1s;`,
            onclick: () => {
              selectedPerson = u.name;
              selUser.textContent = `Log in as ${u.name}`;
              grid.style.display = "none";
              form.style.display = "flex";
              input.value = "";
              input.focus();
            }
          }, u.name[0].toUpperCase());
          
          btn.onmousedown = () => btn.style.transform = "scale(0.95)";
          btn.onmouseup = btn.onmouseleave = () => btn.style.transform = "";
          
          const wrapper = el("div", { style: "display: flex; flex-direction: column; align-items: center; gap: 8px;" },
            btn,
            el("span", { style: "font-size: 14px; color: var(--ink-2); font-weight: 500;" }, u.name)
          );
          grid.append(wrapper);
        }
      }
    } catch (e) {
      console.error("Failed to load auth users");
    }

    backBtn.onclick = () => {
      form.style.display = "none";
      grid.style.display = "flex";
      err.textContent = "";
      selectedPerson = null;
    };

    form.onsubmit = async (e) => {
      e.preventDefault();
      const pin = input.value.trim();
      if (!selectedPerson || pin.length === 0 || pin.length > 64) return;
      
      const submitBtn = form.querySelector("button[type=submit]");
      submitBtn.disabled = true; submitBtn.textContent = "Verifying...";
      err.textContent = "";
      
      try {
        const res = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ person: selectedPerson, pin })
        });
        
        if (res.ok) {
          const data = await res.json();
          localStorage.setItem("bernie-token", data.token);
          await validateToken(data.token);
          unlock();
        } else {
          const data = await res.json().catch(() => ({}));
          err.textContent = data.detail || "Invalid PIN";
          submitBtn.disabled = false; submitBtn.textContent = "Unlock";
          input.value = ""; input.focus();
        }
      } catch (e) {
        err.textContent = "Connection error";
        submitBtn.disabled = false; submitBtn.textContent = "Unlock";
      }
    };
  }

  // Markdown renderer — bold, italic, inline code, code blocks, links, images, line breaks
  function _escAttr(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // family-bot-x46.4: only http(s) or same-origin relative URLs in href/src
  function _safeUrl(url) {
    if (!url || typeof url !== "string") return "";
    const u = url.trim();
    if (u.startsWith("/") && !u.startsWith("//")) return u;
    try {
      const parsed = new URL(u, location.origin);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.href;
    } catch (_) {}
    return "";
  }

  function renderMarkdown(text) {
    if (!text) return "";
    let s = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    
    // 1. Stash code blocks to prevent formatting leakage
    const blocks = [];
    s = s.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, c) => {
      const id = `__BLOCK_${blocks.length}__`;
      blocks.push(`<pre><code>${c.trim()}</code></pre>`);
      return id;
    });

    // 2. Images (before links, to avoid double-matching)
    s = s.replace(/!\[(.*?)\]\((.*?)\)/g, (_, alt, url) => {
      const safe = _safeUrl(url);
      if (!safe) return _escAttr(alt || "");
      const isSameOrigin =
        safe.startsWith("/") ||
        safe === location.origin ||
        safe.startsWith(location.origin + "/");
      // Relative /api/… or absolute same-origin http(s)://host/api/…
      const needsAuth =
        isSameOrigin &&
        (safe.startsWith("/api/") ||
          safe.startsWith(location.origin + "/api/"));
      if (needsAuth) {
        // Prefer path-only for fetch so token header works without query leaks
        const authSrc = safe.startsWith(location.origin)
          ? safe.slice(location.origin.length) || "/"
          : safe;
        return `<img data-bern-auth-src="${_escAttr(authSrc)}" alt="${_escAttr(alt)}" style="max-width:100%; border-radius:var(--r-md); margin-top:8px; display:block; cursor:zoom-in" onclick="window.open(this.src,'_blank')">`;
      }
      return `<img src="${_escAttr(safe)}" alt="${_escAttr(alt)}" style="max-width:100%; border-radius:var(--r-md); margin-top:8px; display:block; cursor:zoom-in" onclick="window.open(this.src,'_blank')">`;
    });

    // 3. Links
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
      const safe = _safeUrl(url);
      if (!safe) return label;
      return `<a href="${_escAttr(safe)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });

    // 4. Other formatting
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    s = s.replace(/\n/g, "<br>");

    // 5. Restore stashed blocks
    blocks.forEach((html, i) => {
      s = s.replace(`__BLOCK_${i}__`, html);
    });

    return s;
  }

  // Blob URLs for auth images — revoke on panel switch to avoid leaks
  const _authBlobUrls = new Set();

  function revokeAuthImageBlobs() {
    for (const u of _authBlobUrls) {
      try { URL.revokeObjectURL(u); } catch (_) {}
    }
    _authBlobUrls.clear();
  }

  async function hydrateAuthImages(root) {
    const scope = root || document;
    const t = localStorage.getItem("bernie-token") || "";
    const imgs = scope.querySelectorAll ? scope.querySelectorAll("img[data-bern-auth-src]") : [];
    for (const img of imgs) {
      const url = img.getAttribute("data-bern-auth-src");
      if (!url) continue;
      try {
        const res = await fetch(url, { headers: { "X-Bernie-Token": t } });
        if (res.ok) {
          const blobUrl = URL.createObjectURL(await res.blob());
          // Revoke prior blob if we re-hydrate the same element
          if (img.src && img.src.startsWith("blob:")) {
            try {
              URL.revokeObjectURL(img.src);
              _authBlobUrls.delete(img.src);
            } catch (_) {}
          }
          _authBlobUrls.add(blobUrl);
          img.src = blobUrl;
        }
      } catch (_) {}
      img.removeAttribute("data-bern-auth-src");
    }
  }

  window.renderMarkdown = renderMarkdown;
  window.hydrateAuthImages = hydrateAuthImages;
  window.revokeAuthImageBlobs = revokeAuthImageBlobs;

  function getPanels() {
    return visibleNav().map(n => n.id);
  }
  let _activePanel = null;

  async function showPanel(id) {
    window.showPanel = showPanel;
    id = normalizePanelId(id);
    if (id === "chat") {
      const url = getOpenWebUIUrl();
      window.open(url, "_blank", "noopener,noreferrer");
      flashBernie("Opening OpenWebUI");
      return;
    }
    if (id === "admin" && !isAdminUser()) {
      flashBernie("Admin access required");
      return;
    }

    const domId = domPanelId(id);
    const prevPanel = _activePanel;

    // Drop auth-image blob URLs when leaving a panel (DOM nodes go away)
    if (prevPanel && prevPanel !== id) {
      revokeAuthImageBlobs();
    }

    if (prevPanel === "admin" && id !== "admin" && typeof window.v3AdminLeave === "function") {
      window.v3AdminLeave();
    }

    const main = document.getElementById("main-content");
    if (!main) return;

    // Prevent scroll lock class (tasks-active) from leaking to other tabs
    main.classList.toggle("tasks-active", domId === "tasks");

    syncPanelVisibility(id);

    let root = document.getElementById(id === "admin" ? "panel-admin" : `panel-${domId}`);
    if (!root) {
      root = document.createElement("div");
      root.classList.add("page", "page-fade");
      root.id = id === "admin" ? "panel-admin" : `panel-${domId}`;
      main.appendChild(root);
    }
    root.style.display = "";

    document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
    const activeNav = document.querySelector(`.nav-item[data-panel="${id}"]`);
    if (activeNav) activeNav.classList.add("active");
    if (id === "admin") {
      const more = document.querySelector(".nav-item.nav-more");
      if (more) more.classList.add("active");
    }

    localStorage.setItem("bernie-panel", id);
    if (location.hash !== `#${id}`) {
      try { history.replaceState(null, "", `#${id}`); } catch (_) { location.hash = id; }
    }
    _activePanel = id;
    window._activePanel = id;
    window.scrollTo({ top: 0, behavior: "instant" });

    if (prevPanel !== id) {
      await loadPanelData(id);
      if (_activePanel !== id) return;
    }
    await invokePanelRender(id);
    if (_activePanel !== id) return;
    renderShell();
    syncPanelVisibility(id);
  }

  window.openBernieChat = function () {
    const url = getOpenWebUIUrl();
    window.open(url, "_blank", "noopener,noreferrer");
    flashBernie("Opening OpenWebUI");
  };

  async function loadPanelData(navId) {
    navId = normalizePanelId(navId || _activePanel || "today");
    const safe = (p) => p.catch(e => { console.error("API call failed:", e); return null; });

    if (navId === "today") {
      await refreshTodayData();
      return;
    }
    if (navId === "home") {
      await refreshHomeData();
      return;
    }
    if (navId === "plan") {
      const tasks = await safe(api("/api/tasks?all_people=true"));
      if (tasks) D.tasks = tasks;
      return;
    }
    if (navId === "people") {
      const family = await safe(api("/api/family"));
      if (family) D.family = family;
      return;
    }
    if (navId === "security") {
      return;
    }
    if (navId === "admin") {
      await refreshAdminData();
      return;
    }
  }

  async function loadInitial() {
    renderShell();
    showSkeletons();
    const safe = (p) => p.catch(e => { console.error("API call failed:", e); return null; });
    const [health] = await Promise.all([
      safe(api("/api/health")),
      loadFamilyBootstrap(),
      isAdminUser() ? refreshAdminData() : Promise.resolve(),
    ]);
    if (health) D.health = health;
    renderShell();
    if (!_intervalsStarted) {
      _intervalsStarted = true;
      setInterval(() => { if (window.refreshTemps) window.refreshTemps(); }, 300_000);
      setInterval(() => {
        if (_activePanel !== "plan") return;
        api("/api/tasks?all_people=true").then(d => {
          if (d) { D.tasks = d; if (window.renderTasks) window.renderTasks(); }
        }).catch(() => {});
      }, 60_000);
    }
  }
  let _intervalsStarted = false;
  window.showPanel = showPanel;

    document.addEventListener("DOMContentLoaded", () => {
    checkAuth();
    window.addEventListener("hashchange", () => {
      const id = normalizePanelId((location.hash || "").replace(/^#/, ""));
      if (!id || !localStorage.getItem("bernie-token")) return;
      if (id === "admin" && !isAdminUser()) return;
      showPanel(id);
    });
    document.addEventListener("keydown", e => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.key >= "1" && e.key <= "9") {
        const idx = parseInt(e.key) - 1;
        const panels = getPanels();
        if (panels[idx]) showPanel(panels[idx]);
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") renderShell();
    });
    setInterval(() => { if (D.presence && window.renderToday) window.renderToday(); }, 60_000);
  });
})();
