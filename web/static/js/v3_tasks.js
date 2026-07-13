/* Bernie · Tasks board (Phase 32) — faithful port of mockup.html, fed by the
 * live API and embedded in the dashboard's #panel-tasks.
 *  - Views: Board / Month / Roster
 *  - Search + filter row (person · type · month) + clear
 *  - Compact cards (no inline notes — notes/actions live in the drawer)
 *  - Detail drawer: GET /api/tasks/{id}/detail (runs · events · links) + comment
 *  - Axis 1 appearance (light/dark) → body[data-theme] (shared w/ app.v6.js)
 *  - Axis 2 mode (calm/hud) → #panel-tasks[data-mode] (localStorage)
 * Reuses the dashboard's global api()/window.BernieData/window.Me/window.flashBernie.
 */
(function () {
  const D = () => window.BernieData || {};
  const me = () => window.Me || {};
  const qEl = id => document.getElementById(id);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ── dates / months ────────────────────────────────────────────────────────
  const MNAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const NOW = new Date();
  const YEAR = NOW.getFullYear();
  const ym = d => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
  const CUR_MONTH = ym(NOW);
  const MONTHS = Array.from({ length: 8 }, (_, i) => ym(new Date(NOW.getFullYear(), NOW.getMonth() + i, 1)));
  function monthLabel(m, withYear) {
    if (!m || m === "someday") return "someday";
    const [y, mo] = m.split("-"), lbl = MNAMES[+mo] || m;
    return withYear ? `${lbl} ${y}` : (y === String(YEAR) ? lbl : `${lbl} '${y.slice(2)}`);
  }
  const fmtTime = s => !s ? "" : `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  function fmtDue(iso) {
    if (!iso) return null;
    const d = new Date(iso), diff = Math.ceil((d - NOW) / 86400000);
    if (diff >= 0 && diff <= 6) return d.toLocaleDateString(undefined, { weekday: "short" });
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function fmtAbs(iso) {
    if (!iso) return null;
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  // ── roster (real family + static agents) ────────────────────────────────────
  const TONE = { mom: "var(--p-mom)", dad: "var(--p-dad)", child1: "var(--p-child1)", child2: "var(--p-child2)" };
  const FALLBACK_FAMILY = [
    { id: "dad", label: "Dad", short: "D", kind: "person", role: "parent" },
    { id: "mom", label: "Mom", short: "B", kind: "person", role: "parent" },
    { id: "child1", label: "Child1", short: "C", kind: "person", role: "kid" },
    { id: "child2", label: "Child2", short: "P", kind: "person", role: "kid" },
  ];
  const AGENTS = [
    { id: "agent:bernie", label: "Bernie", short: "✦", kind: "agent", role: "household ai", tone: "var(--a-bernie)" },
    { id: "agent:research-worker", label: "Research Worker", short: "RW", kind: "agent", role: "background scheduler", tone: "var(--a-worker)" },
  ];
  function family() {
    const fam = D().family || [];
    const list = fam.length
      ? fam.filter(p => p.who && p.role !== "friend").map(p => ({
          id: p.who, label: p.name || p.who,
          short: ((p.initial || p.name || "?")[0] || "?").toUpperCase(),
          kind: "person", role: p.role || "", tone: TONE[p.who] || "var(--slate)",
        }))
      : FALLBACK_FAMILY.map(p => ({ ...p, tone: TONE[p.id] || "var(--slate)" }));
    return list;
  }
  function byId() {
    const m = {};
    for (const a of [...family(), ...AGENTS]) m[a.id] = a;
    return m;
  }

  const TYPES = {
    chore: { glyph: "▤", label: "chore" }, research: { glyph: "◉", label: "research" },
    bernie: { glyph: "✦", label: "bernie" }, code: { glyph: "⌘", label: "code" }, system: { glyph: "⚙", label: "system" },
  };
  const STATUSES = [
    { id: "triage", label: "Triage", cap: "rough ideas", hudOnly: true },
    { id: "todo", label: "To do", cap: "planned", hudOnly: false },
    { id: "ready", label: "Ready", cap: "deps met", hudOnly: true },
    { id: "running", label: "In progress", cap: "live", hudOnly: false },
    { id: "blocked", label: "Blocked", cap: "needs help", hudOnly: false },
    { id: "done", label: "Done", cap: "shipped", hudOnly: false },
  ];

  // ── map an API row (_row_to_task) → the card/drawer shape ────────────────────
  function normPersonId(id) {
    if (!id) return id;
    return id.startsWith("person:") ? id.slice(7) : id;
  }
  function personMatches(stored, userId) {
    if (!stored || !userId) return false;
    if (stored === userId) return true;
    if (stored === "person:" + userId || userId === "person:" + stored) return true;
    return false;
  }
  function mapTask(r) {
    const norm = normPersonId;
    return {
      id: r.id, type: r.type || "chore", status: r.kanban_status || r.status || "todo",
      title: r.title || "", details: r.details || "",
      assigned_to: norm(r.assigned_to) || null,
      acceptable_assignees: (r.acceptable_assignees || []).map(norm),
      month: r.horizon || "someday", priority: r.priority || "normal",
      visibility: r.visibility || "family",
      due: fmtDue(r.due_at), recurring: r.is_recurring ? "recurring" : null,
      completion_note: r.completion_note || "", completed: fmtAbs(r.completed_at),
      created: fmtAbs(r.created_at),
      requires_approval: !!r.requires_approval, approved_at: r.approved_at || null,
      category: r.category || "Task",
      run_id: r.current_run_id || null,
      blocked_reason: (r.kanban_status === "blocked") ? (r.error || null) : null,
    };
  }
  const TASKLIST = () => (D().tasks || []).map(mapTask);

  // ── state ───────────────────────────────────────────────────────────────────
  const MODE_KEY = "bernie.tasks.mode";
  const S = {
    view: "board", search: "", fPerson: null, fType: null, fMonth: "all",
    quickOpen: false, openId: null, mMenu: false, focus: null, detail: null,
    qa: { title: "", type: "chore", assignee: null, month: CUR_MONTH, priority: "normal" },
  };
  const getMode = () => localStorage.getItem(MODE_KEY) === "hud" ? "hud" : "calm";
  let _searchDebounce = null;

  function canManage(t) {
    const m = me();
    const role = m.role || "";
    if (role === "admin" || role === "parents") return true;
    return personMatches(t.assigned_to, m.id);
  }
  function isAdminUser() {
    const role = (me().role || "");
    return role === "admin" || role === "parents";
  }
  function canConvertType() {
    return isAdminUser();
  }
  function showCodeInUi() {
    return getMode() === "hud" && canConvertType();
  }
  function uiTaskTypes() {
    return showCodeInUi() ? ["chore", "research", "bernie", "code"] : ["chore", "research", "bernie"];
  }
  function assigneesForType(type) {
    if (type === "chore") return family().map(p => p.id);
    if (type === "research") return ["agent:bernie", "agent:research-worker"];
    if (type === "bernie") return ["agent:bernie"];
    if (type === "code") return []; // legacy rows only — no nanobot UI surface
    return [];
  }
  function defaultAssignee(type) {
    const ids = assigneesForType(type);
    if (type === "chore") return me().id && ids.includes(me().id) ? me().id : ids[0];
    return ids[0] || null;
  }
  function pickAssignee(type, current) {
    const ids = assigneesForType(type);
    if (current && ids.includes(current)) return current;
    return defaultAssignee(type);
  }
  function editStatuses() {
    const hud = getMode() === "hud";
    return STATUSES.filter(s => !s.hudOnly || hud).concat([{ id: "archived", label: "Archived" }]);
  }
  function editSelect(label, field, value, options) {
    const opts = options.map(o => {
      const id = typeof o === "string" ? o : o.id;
      const lbl = typeof o === "string" ? o : o.label;
      return `<option value="${esc(id)}"${id === value ? " selected" : ""}>${esc(lbl)}</option>`;
    }).join("");
    return `<label class="edit-field"><span>${esc(label)}</span><select data-edit="${field}">${opts}</select></label>`;
  }
  function editSection(t) {
    if (!canManage(t) || t.type === "system") return "";
    const typeOpts = (t.type === "code" && showCodeInUi()
      ? ["chore", "research", "bernie", "code"]
      : uiTaskTypes()).map(k => ({ id: k, label: TYPES[k].label }));
    const statusOpts = editStatuses();
    const monthOpts = [{ id: "someday", label: "Someday" }].concat(MONTHS.map(m => ({ id: m, label: monthLabel(m, true) })));
    const assigneeOpts = assigneesForType(t.type).map(id => ({ id, label: nameOf(id) }));
    const curAssignee = (t.assigned_to && assigneesForType(t.type).includes(t.assigned_to))
      ? t.assigned_to : pickAssignee(t.type, t.assigned_to);
    let html = `<div class="sec"><div class="lbl">edit</div><div class="edit-grid">`;
    if (canConvertType()) html += editSelect("Type", "type", t.type, typeOpts);
    html += editSelect("Status", "status", t.status, statusOpts);
    html += editSelect("Horizon", "horizon", t.month || "someday", monthOpts);
    html += `<label class="edit-field"><span>Category</span><input data-edit="category" value="${esc(t.category || "Task")}"></label>`;
    html += editSelect("Assignee", "assignee", curAssignee, assigneeOpts);
    html += `</div></div>`;
    return html;
  }

  function visibleInCalm(t) {
    if (t.type === "code" && !showCodeInUi()) return false;
    return getMode() === "hud" || t.visibility !== "internal";
  }

  // ── small render helpers ─────────────────────────────────────────────────────
  function avatar(id, size = 22) {
    if (!id) return `<span class="av ghost" style="width:${size}px;height:${size}px">?</span>`;
    const lookup = normPersonId(id);
    const a = byId()[lookup] || byId()[id];
    const tone = a ? a.tone : "var(--slate)";
    const short = a ? a.short : (id.startsWith("agent:") ? id.slice(6, 8).toUpperCase() : lookup[0].toUpperCase());
    const isAgent = a ? a.kind === "agent" : id.startsWith("agent:");
    const sp = isAgent ? `<span class="spark">✦</span>` : "";
    const title = a ? `${a.label} · ${a.role}` : id;
    return `<span class="av ${isAgent ? "agent" : ""}" title="${esc(title)}" style="width:${size}px;height:${size}px;background:${tone}">${esc(short)}${sp}</span>`;
  }
  function badge(type, compact) { const t = TYPES[type] || TYPES.system; return `<span class="badge b-${type} ${compact ? "compact" : ""}"><span class="g">${t.glyph}</span>${t.label}</span>`; }
  function nameOf(id) { const a = byId()[normPersonId(id)] || byId()[id]; return a ? a.label : (id || ""); }
  function assigneeChip(t, size = 22) {
    if (t.assigned_to) return `<span class="who">${avatar(t.assigned_to, size)}<span class="lbl">${esc(nameOf(t.assigned_to))}</span></span>`;
    const ids = t.acceptable_assignees || [];
    if (!ids.length) return `<span class="who">${avatar(null, size)}<span class="faint">unassigned</span></span>`;
    const shown = ids.slice(0, 3), extra = ids.length - shown.length;
    return `<span class="who"><span class="stack">${shown.map(id => avatar(id, size)).join("")}${extra > 0 ? `<span class="more">+${extra}</span>` : ""}</span><span class="faint">any can claim</span></span>`;
  }
  function hudLine(t) {
    const p = [];
    if (t.run_id) p.push(`run=${t.run_id}`);
    if (t.recurring) p.push(`recurring`);
    if (!t.assigned_to && (t.acceptable_assignees || []).length > 1) p.push(`claimable=[${t.acceptable_assignees.length}]`);
    if (t.visibility === "internal") p.push("internal");
    return p.length ? p.join(" · ") : `id=${t.id} · type=${t.type}`;
  }

  // ── filtering ─────────────────────────────────────────────────────────────────
  function filtered() {
    const q = S.search.toLowerCase();
    return TASKLIST().filter(t => {
      if (t.status === "archived") return false;
      if (q && !t.title.toLowerCase().includes(q)) return false;
      if (S.fPerson && t.assigned_to !== S.fPerson && !(t.acceptable_assignees || []).includes(S.fPerson)) return false;
      if (S.fType && t.type !== S.fType) return false;
      if (S.fMonth !== "all" && (t.month || "someday") !== S.fMonth) return false;
      return true;
    });
  }

  // ── card ────────────────────────────────────────────────────────────────────
  function card(t, opts) {
    opts = opts || {};
    const isHud = getMode() === "hud", internal = t.visibility === "internal";
    if (!visibleInCalm(t)) return "";
    const claimable = !t.assigned_to && (t.acceptable_assignees || []).length > 1;
    let crow = badge(t.type);
    if (isHud && internal) crow += `<span class="pill">internal</span>`;
    if (t.recurring) crow += `<span class="pill sage">↻ ${esc(t.recurring)}</span>`;
    crow += `<span class="spc"></span>`;
    if (t.priority === "high") crow += `<span class="pri" title="high"></span>`;
    if (t.due) crow += `<span class="due">due ${esc(t.due)}</span>`;
    if (t.status === "done") crow += `<span style="color:var(--ok)">✓</span>`;
    if (t.status === "blocked") crow += `<span style="color:var(--err)">⚠</span>`;
    let mid = "";
    if (t.status === "blocked" && t.blocked_reason) mid = `<div class="blocked-note">⚠ <span>${esc(t.blocked_reason)}</span></div>`;
    if (t.status === "done" && t.completion_note) mid = `<div class="done-note">${esc(t.completion_note)}</div>`;
    const bottom = `<div class="bottom">${assigneeChip(t)}<span class="spc"></span>` +
      (S.view !== "month" ? `<span class="pill ${t.month === CUR_MONTH ? "accent" : ""}">${monthLabel(t.month)}</span>` : "") + `</div>`;
    const hud = isHud ? `<div class="hud-line">${esc(hudLine(t))}</div>` : "";
    const dragAttr = opts.draggable ? ` draggable="true" data-task-id="${t.id}"` : "";
    return `<article class="card t-${t.type} ${t.status === "done" ? "done" : ""} ${claimable ? "claimable" : ""}" data-open="${t.id}"${dragAttr}>
      <div class="crow">${crow}</div>
      <div class="ctitle">${esc(t.title)}</div>${mid}${bottom}${hud}</article>`;
  }

  // ── columns + views ───────────────────────────────────────────────────────────
  function column(title, sub, count, accent, body, laneId) {
    const laneAttr = laneId ? ` data-lane="${laneId}"` : "";
    return `<section class="col"><div class="col-h">${accent ? `<span class="dot" style="background:${accent}"></span>` : ""}
      <span class="nm">${title}${sub ? `<span class="cap">${esc(sub)}</span>` : ""}</span><span class="ct">${count}</span></div>
      <div class="col-body"${laneAttr}>${count ? body : `<div class="empty">— none —</div>`}</div></section>`;
  }
  function viewBoard(ts) {
    const isHud = getMode() === "hud";
    const vis = ts.filter(visibleInCalm);
    const cols = STATUSES.filter(s => isHud || !s.hudOnly);
    let html = cols.map(s => {
      const ct = vis.filter(t => t.status === s.id && t.type !== "system");
      const ac = s.id === "running" ? "var(--accent)" : s.id === "blocked" ? "var(--err)" : s.id === "done" ? "var(--ok)" : null;
      return column(s.label, isHud ? s.cap : null, ct.length, ac, ct.map(t => card(t, { draggable: canManage(t) })).join(""), s.id);
    }).join("");
    if (isHud) {
      const sys = vis.filter(t => t.type === "system");
      html += column("System", "cognitive_tasks · read-only", sys.length, null, sys.map(t => card(t)).join(""), null);
    }
    return `<div class="board">${html}</div>`;
  }
  function viewMonth(ts) {
    const isHud = getMode() === "hud", vis = ts.filter(t => isHud || t.visibility !== "internal");
    const buckets = [...MONTHS, "someday"].filter(m => m === CUR_MONTH || vis.some(t => (t.month || "someday") === m));
    return `<div class="board">${buckets.map(m => {
      const ct = vis.filter(t => (t.month || "someday") === m);
      return column(monthLabel(m, true), m === "someday" ? "no date" : "", ct.length, m === CUR_MONTH ? "var(--accent)" : null, ct.map(card).join(""));
    }).join("")}</div>`;
  }
  function viewRoster(ts) {
    const isHud = getMode() === "hud", vis = ts.filter(t => isHud || t.visibility !== "internal");
    const agents = isHud ? AGENTS : AGENTS.filter(a => a.id === "agent:bernie");
    const cols = [...family(), ...agents, { id: "__open", label: "Open to claim", role: "no one has picked these up" }];
    return `<div class="board">${cols.map(p => {
      const ct = p.id === "__open" ? vis.filter(t => !t.assigned_to) : vis.filter(t => t.assigned_to === p.id);
      const title = `<span style="display:inline-flex;align-items:center;gap:8px">${avatar(p.id === "__open" ? null : p.id, 20)}<span>${esc(p.label)}</span></span>`;
      const ac = p.kind === "agent" ? "var(--accent)" : p.id === "__open" ? "var(--line-strong)" : null;
      return column(title, p.role, ct.length, ac, ct.map(card).join(""));
    }).join("")}</div>`;
  }

  // ── quick add ────────────────────────────────────────────────────────────────
  function validAssignees(type) {
    if (type === "research") return AGENTS;
    if (type === "bernie") return AGENTS.filter(a => a.id === "agent:bernie");
    if (type === "code") return [];
    return family();
  }
  function quickAdd() {
    if (!S.quickOpen) return "";
    const q = S.qa, va = validAssignees(q.type);
    if (!va.find(a => a.id === q.assignee)) q.assignee = (va[0] || {}).id || null;
    return `<div class="qa">
      <input class="title-in" id="tb-qa-title" placeholder="Add a task… (e.g. “Child2's permission slip — needs signing tonight”)" value="${esc(q.title)}">
      <div class="ctrls">
        <span class="flabel">type</span>
        ${uiTaskTypes().map(k => `<button class="chip ${q.type === k ? "on" : ""}" data-qa-type="${k}">${badge(k, true)}</button>`).join("")}
        <span class="vline"></span>
        <span class="flabel">assign</span>
        ${va.map(a => `<button class="chip ${q.assignee === a.id ? "on" : ""}" data-qa-assignee="${a.id}">${avatar(a.id, 16)} ${esc(a.label)}</button>`).join("")}
        <span class="vline"></span>
        <span class="flabel">month</span>
        <select id="tb-qa-month">${MONTHS.map(m => `<option value="${m}" ${q.month === m ? "selected" : ""}>${monthLabel(m, true)}</option>`).join("")}<option value="someday" ${q.month === "someday" ? "selected" : ""}>Someday</option></select>
        <select id="tb-qa-priority">${["low", "normal", "high"].map(p => `<option value="${p}" ${q.priority === p ? "selected" : ""}>${p} priority</option>`).join("")}</select>
        <span class="spc"></span>
        <button class="ghost" data-qa="cancel">cancel</button>
        <button class="add" data-qa="add">Add task ↵</button>
      </div>
      <div class="qa-err" id="tb-qa-err"></div>
      <div class="tool-note">type → assignee: chore = family · research = Bernie / worker · bernie = Bernie</div>
    </div>`;
  }

  // ── top bar ──────────────────────────────────────────────────────────────────
  function topRow1() {
    const isHud = getMode() === "hud", all = TASKLIST().filter(visibleInCalm);
    const running = all.filter(t => t.status === "running").length, blocked = all.filter(t => t.status === "blocked").length;
    const calmCount = all.filter(t => t.status !== "archived").length;
    const hudTitle = '<span class="mono" style="font-size:18px;color:var(--accent)">$</span> tasks.live';
    return `<div class="ttl"><h1>${isHud ? hudTitle : "Plan"}</h1>
      <div class="sub">${isHud ? `system hud · <span style="color:var(--accent)">●</span> ${running} running · ${blocked} blocked` : `calm chores · ${calmCount} active`}</div></div>
      <div class="search">🔍<input id="tb-search" placeholder="search tasks" value="${esc(S.search)}"><span class="kbd">/</span></div>
      <span class="spacer"></span>
      <div class="seg">${[["board", "Board"], ["month", "Month"], ["roster", "Roster"]].map(([v, l]) => `<button class="${S.view === v ? "on" : ""}" data-view="${v}">${l}</button>`).join("")}</div>
      <div class="seg" title="mode (h)">${isAdminUser()
        ? [["calm", "◑ Calm"], ["hud", "▦ HUD"]].map(([v, l]) => `<button class="${getMode() === v ? "on" : ""}" data-mode="${v}">${l}${v === "hud" ? '<span class="kbd">h</span>' : ""}</button>`).join("")
        : `<button class="on" data-mode="calm">◑ Calm</button>`}</div>
      <button class="newbtn" data-qa="open">+ New task</button>`;
  }
  function topRow2() {
    const chip = (on, attr, inner) => `<button class="chip ${on ? "on" : ""}" ${attr}>${inner}</button>`;
    const people = family().map(p => chip(S.fPerson === p.id, `data-person="${p.id}"`, `${avatar(p.id, 16)} ${esc(p.label)}`)).join("")
      + `<span class="vline"></span>` + AGENTS.map(a => chip(S.fPerson === a.id, `data-person="${a.id}"`, `${avatar(a.id, 16)} ${esc(a.label)}`)).join("");
    const types = uiTaskTypes().map(k => chip(S.fType === k, `data-type="${k}"`, badge(k, true))).join("");
    const mLabel = S.fMonth === "all" ? "All months" : monthLabel(S.fMonth, true);
    const any = S.fPerson || S.fType || S.fMonth !== "all" || S.search;
    return `<span class="flabel">filter</span><div class="frow">${people}</div><span class="spacer"></span>
      <div class="frow">${types}</div>
      <div class="dd"><button data-month-toggle>${mLabel} <span class="mono" style="color:var(--ink-faint);font-size:10.5px">▾</span></button>
        <div class="dd-menu ${S.mMenu ? "open" : ""}">
          <button class="${S.fMonth === "all" ? "sel" : ""}" data-month="all">All months</button><div class="div"></div>
          ${MONTHS.map(m => `<button class="${S.fMonth === m ? "sel" : ""}" data-month="${m}">${monthLabel(m, true)}</button>`).join("")}
          <div class="div"></div><button class="${S.fMonth === "someday" ? "sel" : ""}" data-month="someday">Someday</button>
        </div></div>
      ${any ? `<button class="clearbtn" data-clear>clear ×</button>` : ""}`;
  }

  // ── drawer ─────────────────────────────────────────────────────────────────
  function statusTone(s) { return ({ running: "accent", done: "ok", blocked: "err", ready: "ok" }[s]) || ""; }
  function drawerHtml(t) {
    if (!t) return "";
    const det = S.detail && S.detail.id === t.id ? S.detail : null;
    const isAgentType = ["research", "code", "system", "bernie"].includes(t.type);
    let body = t.details ? `<p class="det">${esc(t.details)}</p>` : "";

    body += `<div class="sec"><div class="lbl">assigned</div><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${assigneeChip(t, 30)}</div>
      <div class="metrics" style="margin-top:6px">status ${t.status} · ${monthLabel(t.month, true)}${t.visibility === "internal" ? " · internal" : ""}</div></div>`;

    body += editSection(t);

    if (t.type === "chore") {
      const ex = []; if (t.due) ex.push(`due ${t.due}`); if (t.recurring) ex.push(`↻ ${t.recurring}`); if (t.completed) ex.push(`completed ${t.completed}`);
      if (ex.length) body += `<div class="sec"><div class="lbl">chore</div><div style="display:flex;gap:6px;flex-wrap:wrap">${ex.map(e => `<span class="pill">${esc(e)}</span>`).join("")}</div></div>`;
    }
    if (t.status === "blocked" && t.blocked_reason) body += `<div class="sec"><div class="lbl">blocked</div><div class="blocked-note">⚠ <span>${esc(t.blocked_reason)}</span></div></div>`;
    if (t.status === "done" && t.completion_note) body += `<div class="sec"><div class="lbl">outcome</div><div class="done-note">${esc(t.completion_note)}</div></div>`;

    // run history (task_executions, via /detail)
    const runs = det ? det.runs || [] : null;
    if (runs && runs.length) {
      body += `<div class="sec"><div class="lbl">Run history</div>` + runs.map(r => {
        const cls = r.status === "active" ? "active" : (r.status === "completed" ? "" : "bad");
        return `<div class="run-row ${cls}"><span class="o">${esc(r.status)}</span><span>${esc(r.execution_id || "")}</span><span class="d">${esc(fmtAbs(r.started_at) || "")}</span></div>`;
      }).join("") + `</div>`;
    } else if (det) {
      body += `<div class="sec"><div class="lbl">run history</div><div class="metrics">no runs yet</div></div>`;
    }

    // dependencies (links, via /detail)
    if (det && ((det.parents || []).length || (det.children || []).length)) {
      const line = (lbl, ids) => ids.length ? `<div class="dep"><span>${lbl}</span><span class="st">${ids.map(i => "#" + i).join(", ")}</span></div>` : "";
      body += `<div class="sec"><div class="lbl">dependencies</div>${line("blocked by", det.parents || [])}${line("blocks", det.children || [])}</div>`;
    }

    // activity / comments
    const events = det ? det.events || [] : null;
    let evHtml = "";
    if (events && events.length) {
      evHtml = events.slice().reverse().slice(0, 12).map(ev => {
        let meta = ev.metadata; if (typeof meta === "string") { try { meta = JSON.parse(meta); } catch { meta = {}; } }
        const txt = meta && (meta.text || meta.reason || meta.note || meta.summary || "") || "";
        const who = ev.actor_person_id ? nameOf(ev.actor_person_id) : "system";
        const actor = ev.actor_person_id ? normPersonId(ev.actor_person_id) : null;
        return `<div class="cmt">${avatar(actor && byId()[actor] ? actor : "agent:bernie", 22)}<div class="body"><div class="w2">${esc(who)} · ${esc(fmtAbs(ev.created_at) || "")} · ${esc(ev.event_type)}</div>${esc(txt)}</div></div>`;
      }).join("");
    }
    body += `<div class="sec"><div class="lbl">activity</div>${evHtml || `<div class="metrics">${det ? "no activity yet" : "loading…"}</div>`}
      <div class="cmt-in"><input id="tb-cmt-in" placeholder="Add a note…"><button class="act" data-cmt="${t.id}">Send</button></div></div>`;

    if (det && getMode() === "hud") body += `<div class="sec hud-only"><div class="lbl">metrics</div><div class="metrics">${esc(hudLine(t))}</div></div>`;

    // type-aware actions (only endpoints that exist)
    let acts = "";
    if (t.type === "chore") {
      acts = `<button class="act primary" data-act="done">✓ Mark done</button>` +
        (t.requires_approval && !t.approved_at ? `<button class="act" data-act="approve">Approve</button>` : "") +
        `<button class="act" data-act="snooze">Snooze 1d</button><button class="act danger" data-act="archive">Archive</button>`;
    } else if (t.type === "system") {
      acts = `<button class="act ghost" disabled>read-only · cognitive_tasks</button>`;
    } else {
      acts = (t.status === "blocked"
        ? `<button class="act" data-act="todo">Unblock</button>`
        : `<button class="act" data-act="blocked">Block</button>`) +
        (t.status !== "done" ? `<button class="act primary" data-act="done">Mark done</button>` : "") +
        `<button class="act danger" data-act="archive">Archive</button>` +
        `<div class="tool-note">agent actions map to kanban_* tools</div>`;
    }
    return `<div class="d-head"><div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${badge(t.type)}<span class="pill ${statusTone(t.status)}">${t.status}</span>${t.visibility === "internal" ? `<span class="pill">internal</span>` : ""}<span class="mono" style="font-size:10.5px;color:var(--ink-faint)">task #${t.id} · ${monthLabel(t.month, true)}</span></div>
        <h2 class="d-title">${esc(t.title)}</h2></div>
        <button class="x" data-close>✕</button></div>
      <div class="d-body" data-task="${t.id}">${body}<div class="acts">${acts}</div></div>`;
  }

  // ── main render ────────────────────────────────────────────────────────────
  window.renderTasks = function () {
    const root = qEl("panel-tasks");
    if (!root) return;
    root.dataset.mode = getMode();
    const main = qEl("main-content");
    if (main) main.classList.toggle("tasks-active", root.style.display !== "none");
    const ts = filtered();
    const viewHtml = S.view === "board" ? viewBoard(ts) : S.view === "month" ? viewMonth(ts) : viewRoster(ts);
    const openTask = S.openId ? TASKLIST().find(x => x.id === S.openId) : null;
    root.innerHTML =
      `<div class="tb-top"><div class="row1">${topRow1()}</div><div class="row2">${topRow2()}</div></div>` +
      `<div class="tb-qa-host">${quickAdd()}</div>` +
      `<div class="tb-view-host">${viewHtml}</div>` +
      `<div class="scrim ${openTask ? "open" : ""}" id="tb-scrim"></div>` +
      `<aside class="drawer ${openTask ? "open" : ""}" id="tb-drawer">${drawerHtml(openTask)}</aside>`;
    if (S.focus) { const el = qEl(S.focus); if (el) { el.focus(); const v = el.value; el.value = ""; el.value = v; } }
    bindBoardDrag(root);
  };

  function bindBoardDrag(root) {
    if (S.view !== "board") return;
    root.querySelectorAll(".card[draggable]").forEach(card => {
      card.addEventListener("dragstart", e => {
        e.dataTransfer.setData("task_id", card.getAttribute("data-task-id"));
        e.dataTransfer.effectAllowed = "move";
        card.classList.add("dragging");
      });
      card.addEventListener("dragend", () => card.classList.remove("dragging"));
    });
    root.querySelectorAll(".col-body[data-lane]").forEach(zone => {
      let enterCount = 0;
      const col = zone.closest(".col");
      zone.addEventListener("dragover", e => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; });
      zone.addEventListener("dragenter", e => {
        e.preventDefault();
        if (++enterCount === 1 && col) col.classList.add("drag-over");
      });
      zone.addEventListener("dragleave", () => {
        if (--enterCount <= 0) { enterCount = 0; if (col) col.classList.remove("drag-over"); }
      });
      zone.addEventListener("drop", e => {
        e.preventDefault();
        enterCount = 0;
        if (col) col.classList.remove("drag-over");
        const taskId = e.dataTransfer.getData("task_id");
        if (taskId) handleDrop(taskId, zone.getAttribute("data-lane"));
      });
    });
  }

  async function handleDrop(taskId, laneStatus) {
    const t = TASKLIST().find(x => String(x.id) === String(taskId));
    if (t && t.status === laneStatus) return;
    try {
      if (laneStatus === "done" && t && t.type === "chore") {
        await api(`/api/tasks/${taskId}/complete`, { method: "POST", body: {} });
      } else {
        await api(`/api/tasks/${taskId}/move`, { method: "POST", body: { status: laneStatus } });
      }
      await refresh();
    } catch (err) {
      if (window.flashBernie) window.flashBernie((err && err.message) || "Failed to move task");
    }
  }

  // ── data refresh ──────────────────────────────────────────────────────────
  async function refresh() {
    try {
      const m = me();
      const role = m.role || "";
      const allPeople = role === "admin" || role === "parents";
      const rows = await api(`/api/tasks?all_people=${allPeople ? "true" : "false"}`);
      if (Array.isArray(rows)) window.BernieData.tasks = rows;
    } catch (e) { /* keep stale list */ }
    window.renderTasks();
  }
  async function openTask(id) {
    S.openId = id; S.detail = null; window.renderTasks();
    try {
      const d = await api(`/api/tasks/${id}/detail`);
      S.detail = { id, ...d };
    } catch (e) {
      if (window.flashBernie) window.flashBernie((e && e.message) || "Cannot open task");
      S.openId = null;
      S.detail = null;
    }
    if (S.openId === id) window.renderTasks();
  }
  async function addTask() {
    const title = (qEl("tb-qa-title")?.value || "").trim();
    const errEl = qEl("tb-qa-err");
    if (!title) { qEl("tb-qa-title")?.focus(); return; }
    const q = S.qa;
    try {
      if (q.type === "chore") {
        await api("/api/tasks", { method: "POST", body: { title, type: "chore", assigned_to: q.assignee, horizon: q.month, priority: q.priority } });
      } else {
        await api("/api/tasks/agent", { method: "POST", body: { type: q.type, title, assigned_to: q.assignee, horizon: q.month, priority: q.priority } });
      }
      S.quickOpen = false; S.qa.title = ""; S.focus = null;
      await refresh();
    } catch (err) { if (errEl) errEl.textContent = (err && err.message) || "Failed to add task"; }
  }
  async function applyEdit(id, field, value) {
    const t = TASKLIST().find(x => x.id === id);
    if (!t || !canManage(t)) return;
    try {
      if (field === "type") {
        if (!canConvertType() || value === t.type) return;
        const assignee = pickAssignee(value, t.assigned_to);
        await api(`/api/tasks/${id}/convert`, {
          method: "POST",
          body: { type: value, assigned_to: assignee, enqueue: value === "research" },
        });
      } else if (field === "status") {
        if (value === t.status) return;
        if (value === "done" && t.type === "chore") await api(`/api/tasks/${id}/complete`, { method: "POST", body: {} });
        else await api(`/api/tasks/${id}/move`, { method: "POST", body: { status: value } });
      } else if (field === "horizon") {
        if (value === (t.month || "someday")) return;
        await api(`/api/tasks/${id}`, { method: "PATCH", body: { horizon: value } });
      } else if (field === "category") {
        const cat = (value || "").trim() || "Task";
        if (cat === (t.category || "Task")) return;
        await api(`/api/tasks/${id}`, { method: "PATCH", body: { category: cat } });
      } else if (field === "assignee") {
        if (value === t.assigned_to) return;
        await api(`/api/tasks/${id}`, { method: "PATCH", body: { assigned_to: value } });
      } else return;
      await refresh();
      if (S.openId === id) await openTask(id);
    } catch (err) {
      if (window.flashBernie) window.flashBernie((err && err.message) || "Update failed");
      window.renderTasks();
    }
  }
  async function doAction(id, act) {
    const t = TASKLIST().find(x => x.id === id);
    try {
      if (act === "approve") await api(`/api/tasks/${id}/approve`, { method: "POST", body: {} });
      else if (act === "snooze") { const until = new Date(Date.now() + 864e5).toISOString(); await api(`/api/tasks/${id}/snooze`, { method: "POST", body: { snooze_until: until } }); }
      else if (act === "done" && t && t.type === "chore") await api(`/api/tasks/${id}/complete`, { method: "POST", body: {} });
      else await api(`/api/tasks/${id}/move`, { method: "POST", body: { status: act === "archive" ? "archived" : act } });
      S.openId = null; S.detail = null; await refresh();
    } catch (err) { if (window.flashBernie) window.flashBernie((err && err.message) || "Action failed"); }
  }
  async function sendComment(id) {
    const inp = qEl("tb-cmt-in"); const text = (inp?.value || "").trim(); if (!text) return;
    try {
      await api(`/api/tasks/${id}/comment`, { method: "POST", body: { text } });
      if (inp) inp.value = "";
      await openTask(id);
    }
    catch (err) { if (window.flashBernie) window.flashBernie((err && err.message) || "Comment failed"); }
  }
  function setMode(v) { try { localStorage.setItem(MODE_KEY, v); } catch (e) {} window.renderTasks(); }

  // ── events (delegated; attached once, scoped to the tasks panel) ──────────────
  const inPanel = e => e.target.closest && e.target.closest("#panel-tasks");
  document.addEventListener("click", e => {
    if (!inPanel(e)) return;
    const el = e.target.closest("[data-view],[data-mode],[data-person],[data-type],[data-month],[data-month-toggle],[data-clear],[data-open],[data-close],[data-qa],[data-qa-type],[data-qa-assignee],[data-act],[data-cmt]");
    const ddArea = e.target.closest(".dd");
    if (!ddArea && S.mMenu) { S.mMenu = false; window.renderTasks(); }
    if (e.target.id === "tb-scrim") { S.openId = null; S.detail = null; window.renderTasks(); return; }
    if (!el) return;
    const d = el.dataset;
    if (d.view != null) { S.view = d.view; S.focus = null; window.renderTasks(); }
    else if (d.mode != null) { setMode(d.mode); }
    else if (d.person != null) { S.fPerson = S.fPerson === d.person ? null : d.person; S.focus = null; window.renderTasks(); }
    else if (d.type != null && d.qaType == null) { S.fType = S.fType === d.type ? null : d.type; S.focus = null; window.renderTasks(); }
    else if (el.hasAttribute("data-month-toggle")) { S.mMenu = !S.mMenu; window.renderTasks(); }
    else if (d.month != null) { S.fMonth = d.month; S.mMenu = false; S.focus = null; window.renderTasks(); }
    else if (el.hasAttribute("data-clear")) { S.fPerson = S.fType = null; S.fMonth = "all"; S.search = ""; S.focus = null; window.renderTasks(); }
    else if (d.open != null) { openTask(Number(d.open)); }
    else if (el.hasAttribute("data-close")) { S.openId = null; S.detail = null; window.renderTasks(); }
    else if (d.cmt != null) { sendComment(Number(d.cmt)); }
    else if (d.act != null) { doAction(S.openId, d.act); }
    else if (d.qaType != null) { S.qa.title = qEl("tb-qa-title")?.value || S.qa.title; S.qa.type = d.qaType; S.focus = "tb-qa-title"; window.renderTasks(); }
    else if (d.qaAssignee != null) { S.qa.title = qEl("tb-qa-title")?.value || S.qa.title; S.qa.assignee = d.qaAssignee; S.focus = "tb-qa-title"; window.renderTasks(); }
    else if (d.qa === "open") { S.quickOpen = true; S.focus = "tb-qa-title"; window.renderTasks(); }
    else if (d.qa === "cancel") { S.quickOpen = false; S.qa.title = ""; S.focus = null; window.renderTasks(); }
    else if (d.qa === "add") { addTask(); }
  });
  document.addEventListener("input", e => {
    if (!inPanel(e)) return;
    if (e.target.id === "tb-search") {
      S.search = e.target.value;
      S.focus = "tb-search";
      clearTimeout(_searchDebounce);
      _searchDebounce = setTimeout(() => window.renderTasks(), 150);
    }
    else if (e.target.id === "tb-qa-title") { S.qa.title = e.target.value; }
  });
  document.addEventListener("change", e => {
    if (!inPanel(e)) return;
    if (e.target.id === "tb-qa-month") S.qa.month = e.target.value;
    else if (e.target.id === "tb-qa-priority") S.qa.priority = e.target.value;
    else if (e.target.dataset.edit && S.openId) applyEdit(S.openId, e.target.dataset.edit, e.target.value);
  });
  document.addEventListener("blur", e => {
    if (!inPanel(e)) return;
    if (e.target.dataset && e.target.dataset.edit === "category" && S.openId) applyEdit(S.openId, "category", e.target.value);
  }, true);
  document.addEventListener("keydown", e => {
    const panel = qEl("panel-tasks");
    if (!panel || panel.offsetParent === null) return;   // only when tasks panel is visible
    if (e.target.id === "tb-cmt-in" && e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (S.openId) sendComment(S.openId);
      return;
    }
    if (e.key === "Escape") { S.openId = null; S.detail = null; S.quickOpen = false; S.mMenu = false; S.focus = null; window.renderTasks(); return; }
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.key === "/") { e.preventDefault(); S.focus = "tb-search"; window.renderTasks(); }
    else if (e.key === "h") { setMode(getMode() === "hud" ? "calm" : "hud"); }
    else if (e.key === "1") { S.view = "board"; window.renderTasks(); }
    else if (e.key === "2") { S.view = "month"; window.renderTasks(); }
    else if (e.key === "3") { S.view = "roster"; window.renderTasks(); }
  });
})();
