(function() {
  const el = (...args) => window.el(...args);

  /* ── State ── */
  let currentPeriod = "30d";
  let chartMetric = "cost"; // cost | tokens | requests
  let stackBy = "provider"; // provider | model
  let sessionSortKey = "cost";
  let sessionSortDir = "desc"; // asc | desc

  const SESSION_SORT_COLS = [
    { key: "title", label: "Conversation", sortable: true },
    { key: "modelId", label: "Model", sortable: true },
    { key: "msgs", label: "Msgs", sortable: true, align: "right" },
    { key: "tokens", label: "Tokens", sortable: true, align: "right" },
    { key: "cost", label: "Cost", sortable: true, align: "right" },
    { key: null, label: "Share", sortable: false },
    { key: "lastActivityTs", label: "Last activity", sortable: true, align: "right" },
  ];

  function sessionSortValue(s, key) {
    if (key === "lastActivityTs") {
      const ts = s.lastActivityTs;
      if (typeof ts === "number" && ts > 0) return ts;
      if (s.lastActivityAt) {
        const parsed = Date.parse(s.lastActivityAt);
        if (!Number.isNaN(parsed)) return parsed;
      }
      return 0;
    }
    if (key === "cost" || key === "msgs" || key === "tokens") {
      return Number(s[key]) || 0;
    }
    return s[key] ?? "";
  }

  function sortSessions(sessions) {
    const key = sessionSortKey;
    const dir = sessionSortDir === "asc" ? 1 : -1;
    const numericKeys = new Set(["cost", "msgs", "tokens", "lastActivityTs"]);
    return [...(sessions || [])].sort((a, b) => {
      const av = sessionSortValue(a, key);
      const bv = sessionSortValue(b, key);
      if (numericKeys.has(key)) return (av - bv) * dir;
      return String(av).localeCompare(String(bv), undefined, { sensitivity: "base" }) * dir;
    });
  }

  function toggleSessionSort(key) {
    if (sessionSortKey === key) {
      sessionSortDir = sessionSortDir === "asc" ? "desc" : "asc";
    } else {
      sessionSortKey = key;
      sessionSortDir = key === "title" || key === "modelId" ? "asc" : "desc";
    }
    renderActivity();
  }

  /* ── Formatters ── */
  const fmt$ = (n, p = 2) => '$' + (n || 0).toLocaleString('en-US', { minimumFractionDigits: p, maximumFractionDigits: p });
  const fmt$tight = (n) => n < 1 ? fmt$(n, 4) : fmt$(n, 2);
  const fmtTok = (n) => n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(Math.round(n));
  const fmtInt = (n) => (n || 0).toLocaleString('en-US');
  const fmtDate = (iso) => {
    const d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  };
  const fmtMetricValue = (metric, value) => metric === 'cost' ? fmt$tight(value) : metric === 'requests' ? fmtInt(value) : fmtTok(value);

  function modelShortLabel(id) {
    id = id || '';
    if (id.startsWith('or-')) id = id.slice(3);
    return id.split('/').pop() || id || 'unknown';
  }

  /* ── Model Colors ── */
  const MODEL_COLORS = {
    // ColorBrewer-inspired qualitative palette (Set2 / Dark2 family)
    'claude-3-5-sonnet-20241022': '#1b9e77',
    'claude-3-5-sonnet-latest': '#1b9e77',
    'claude-3-7-sonnet-20250219': '#1b9e77',
    'claude-3-7-sonnet-latest': '#1b9e77',
    'claude-3-5-haiku-20241022': '#66c2a5',
    'claude-3-opus-20240229': '#0b6e4f',
    'gpt-4o': '#d95f02',
    'gpt-4o-mini': '#fc8d62',
    'deepseek-v3': '#8da0cb',
    'o1-mini': '#7570b3',
    'o3-mini': '#6a3d9a',
    'grok-2': '#e6ab02',
    'grok-3': '#a6761d',
    'anthropic': '#1b9e77',
    'openrouter': '#d95f02',
  };

  function getModelColor(id) {
    id = id || '';
    if (MODEL_COLORS[id]) return MODEL_COLORS[id];
    if (id === 'anthropic') return '#1b9e77';
    if (id === 'openrouter') return '#d95f02';
    // Strip or- prefix for pattern matching
    const norm = id.startsWith('or-') ? id.slice(3) : id;
    if (norm.includes("opus")) return "#0b6e4f";
    if (norm.includes("sonnet")) return "#1b9e77";
    if (norm.includes("haiku")) return "#66c2a5";
    if (norm.includes("claude")) return "#1b9e77";
    if (norm.includes("gpt-4") || norm.includes("gpt-5")) return "#d95f02";
    if (norm.includes("o1") || norm.includes("o3")) return "#7570b3";
    if (norm.includes("deepseek")) return "#8da0cb";
    if (norm.includes("llama") || norm.includes("hermes") || norm.includes("qwen") || norm.includes("gemma") || norm.includes("granite")) return "#e7298a";
    if (norm.includes("grok")) return "#e6ab02";
    if (norm.includes("kimi") || norm.includes("minimax") || norm.includes("nemotron")) return "#b8860b";
    if (norm.includes("gemini")) return "#4285f4";
    return "#a6761d";
  }

  function ensureTooltip() {
    let tip = document.getElementById("activity-tooltip");
    if (!tip) {
      tip = el("div", { id: "activity-tooltip", class: "act-tooltip" });
      document.body.appendChild(tip);
    }
    return tip;
  }

  function hideTooltip() {
    const tip = document.getElementById("activity-tooltip");
    if (tip) tip.classList.remove("show");
  }

  function showTooltip(evt, lines) {
    const tip = ensureTooltip();
    tip.innerHTML = "";
    lines.forEach((line, i) => {
      tip.append(el("div", { class: i === 0 ? "act-tooltip-title" : "act-tooltip-line" }, line));
    });
    tip.classList.add("show");
    const x = Math.min(window.innerWidth - 220, evt.clientX + 14);
    const y = Math.max(12, evt.clientY - 12);
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  /* ── Aggregation ── */
  function aggregate(days) {
    const totalsByModel = {};
    let inTok = 0, outTok = 0, cacheTok = 0, cacheCreateTok = 0, requests = 0, cost = 0;

    days.forEach(d => {
      Object.entries(d.models).forEach(([mid, m]) => {
        if (!totalsByModel[mid]) totalsByModel[mid] = { inTok: 0, outTok: 0, cacheTok: 0, cacheCreateTok: 0, requests: 0, cost: 0 };
        const t = totalsByModel[mid];
        t.inTok += m.inTok || 0;
        t.outTok += m.outTok || 0;
        t.cacheTok += m.cacheTok || 0;
        t.cacheCreateTok += m.cacheCreateTok || 0;
        t.requests += m.requests || 0;
        t.cost += m.cost || 0;

        inTok += m.inTok || 0;
        outTok += m.outTok || 0;
        cacheTok += m.cacheTok || 0;
        cacheCreateTok += m.cacheCreateTok || 0;
        requests += m.requests || 0;
        cost += m.cost || 0;
      });
    });

    return { totalsByModel, inTok, outTok, cacheTok, cacheCreateTok, requests, cost };
  }

  /* ── Primitives ── */
  function Sparkline(data, { stroke = 'var(--slate)', fill = 'rgba(77,122,154,0.18)', height = 28, width = 120 } = {}) {
    const max = Math.max(...data, 0.0001);
    const min = Math.min(...data, 0);
    const span = max - min || 1;
    const stepX = width / (data.length - 1 || 1);
    const pts = data.map((v, i) => [i * stepX, height - ((v - min) / span) * (height - 2) - 1]);
    const path = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const area = path + ` L ${width.toFixed(1)},${height} L 0,${height} Z`;
    
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.display = "block";
    
    const pArea = document.createElementNS("http://www.w3.org/2000/svg", "path");
    pArea.setAttribute("d", area);
    pArea.setAttribute("fill", fill);
    
    const pLine = document.createElementNS("http://www.w3.org/2000/svg", "path");
    pLine.setAttribute("d", path);
    pLine.setAttribute("stroke", stroke);
    pLine.setAttribute("stroke-width", "1.5");
    pLine.setAttribute("fill", "none");
    pLine.setAttribute("stroke-linejoin", "round");
    pLine.setAttribute("stroke-linecap", "round");
    
    svg.append(pArea, pLine);
    return svg;
  }

  function Delta(pct) {
    if (Math.abs(pct) < 0.05) return null;
    const up = pct >= 0;
    return el("span", {
      class: "act-num",
      style: `font-size: 11px; font-weight: 600; color: ${up ? 'var(--rust)' : 'var(--forest)'}; letter-spacing: 0;`
    }, (up ? '▲ ' : '▼ ') + Math.abs(pct).toFixed(1) + '%');
  }

  function StatusPill(state, label) {
    const isWarn = state === 'warn' || state === 'depleted';
    return el("span", { class: "pill " + (isWarn ? "warn" : "active") }, label);
  }

  /* ── Components ── */
  function buildHeader(period, lastSync) {
    const setP = (p) => {
      currentPeriod = p;
      const url = new URL(window.location);
      url.searchParams.set("period", p);
      window.history.replaceState({}, "", url);
      renderActivity();
    };

    return el("header", { style: "display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 32px;" },
      el("div", {},
        el("div", { class: "act-serif xl" }, "Activity"),
        el("div", { class: "act-micro", style: "margin-top: 8px; display: flex; gap: 14px;" },
          el("span", {}, "cost & provider insight"),
          el("span", { style: "color: var(--ink-4);" }, "·"),
          el("span", {}, "last sync ", el("span", { class: "act-num" }, lastSync))
        )
      ),
      el("div", { style: "display: flex; gap: 10px; align-items: center;" },
        el("div", { class: "seg" },
          ["7d", "30d", "90d"].map(p => el("button", { 
            class: period === p ? "on" : "",
            onclick: () => setP(p)
          }, p))
        ),
        el("button", { 
          class: "btn",
          onclick: async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            btn.classList.add("act-skel");
            try {
              const res = await window.api(`/api/activity/refresh?period=${currentPeriod}`, { method: "POST" });
              window.BernieData.activity = res;
              renderActivity();
            } finally {
              btn.disabled = false;
              btn.classList.remove("act-skel");
            }
          }
        }, "Refresh")
      )
    );
  }

  function buildKpiCard(label, value, sub, spark, stroke, fill, delta, suffix) {
    return el("div", { class: "card tight", style: "display: flex; flex-direction: column; gap: 8px; min-height: 118px;" },
      el("div", { class: "act-eyebrow" }, label),
      el("div", { style: "display: flex; align-items: baseline; gap: 8px;" },
        el("div", { class: "act-serif big" }, value),
        suffix ? el("span", { class: "act-micro", style: "padding-bottom: 2px;" }, suffix) : null
      ),
      el("div", { style: "display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: auto;" },
        el("div", { class: "act-micro", style: "display: flex; align-items: center; gap: 8px;" },
          delta !== undefined ? Delta(delta) : null,
          el("span", {}, sub)
        ),
        spark ? Sparkline(spark, { stroke, fill, width: 96, height: 26 }) : null
      )
    );
  }

  function buildKpiStrip(days, periodDays) {
    const cur = aggregate(days);
    // Previous period comes from the backend prevSummary (backend loads 2× data)
    const prev = window.BernieData?.activity?.prevSummary || {};

    const dailyCost = days.map(d => Object.values(d.models).reduce((s, m) => s + (m.cost || 0), 0));
    const dailyReq = days.map(d => Object.values(d.models).reduce((s, m) => s + (m.requests || 0), 0));

    const avgDay = cur.cost / Math.max(1, periodDays);
    const projectedMonth = avgDay * 30;
    const deltaCost = prev.cost ? ((cur.cost - prev.cost) / prev.cost) * 100 : 0;
    const deltaReq = prev.requests ? ((cur.requests - prev.requests) / prev.requests) * 100 : 0;

    return el("div", { style: "display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; margin-bottom: 18px;" },
      buildKpiCard("Period spend", fmt$(cur.cost), `vs prev period`, dailyCost, "var(--slate)", "rgba(77,122,154,0.18)", deltaCost),
      buildKpiCard("Daily average", fmt$(avgDay), "per day", dailyCost, "var(--terra)", "rgba(201,124,58,0.18)"),
      buildKpiCard("Projected · 30d", fmt$(projectedMonth), "at current rate", dailyCost, "var(--forest)", "rgba(58,107,78,0.18)"),
      buildKpiCard("Requests", fmtInt(cur.requests), `${fmt$tight(cur.cost / Math.max(1, cur.requests))} / req`, dailyReq, "var(--plum)", "rgba(107,74,110,0.18)", deltaReq)
    );
  }

  function buildDailyChart(days) {
    const modelIds = Array.from(new Set(days.flatMap(d => Object.keys(d.models))));
    const totalsByModel = aggregate(days).totalsByModel;
    
    let stacks;
    if (stackBy === 'provider') {
      stacks = [
        { key: 'anthropic', label: 'Anthropic', color: getModelColor('anthropic'), ids: modelIds.filter(id => id.includes("claude")) },
        { key: 'openrouter', label: 'OpenRouter', color: getModelColor('openrouter'), ids: modelIds.filter(id => !id.includes("claude")) }
      ];
    } else {
      stacks = modelIds
        .map(id => ({
          key: id,
          label: modelShortLabel(id),
          color: getModelColor(id),
          ids: [id],
          totalCost: totalsByModel[id]?.cost || 0,
        }))
        .sort((a, b) => b.totalCost - a.totalCost || a.label.localeCompare(b.label));
    }

    const seriesOf = (d, s) => {
      let v = 0;
      s.ids.forEach(id => {
        const m = d.models[id];
        if (!m) return;
        if (chartMetric === 'cost') v += m.cost;
        else if (chartMetric === 'tokens') v += (m.inTok + m.outTok);
        else v += m.requests;
      });
      return v;
    };

    const totals = days.map(d => stacks.reduce((s, st) => s + seriesOf(d, st), 0));
    const yMax = Math.max(...totals, 0.0001);

    const W = 800, H = 240;
    const padL = 38, padR = days.length <= 7 ? 20 : 8, padT = 14, padB = 26;
    const chartW = W - padL - padR;
    const chartH = H - padT - padB;
    const step = chartW / days.length;
    const bw = step * 0.74;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "100%");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.classList.add("act-chart");

    // Grid
    const gGrid = document.createElementNS("http://www.w3.org/2000/svg", "g");
    gGrid.classList.add("grid");
    [0, 0.25, 0.5, 0.75, 1].forEach(p => {
      const y = padT + chartH - p * chartH;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", padL);
      line.setAttribute("x2", W - padR);
      line.setAttribute("y1", y);
      line.setAttribute("y2", y);
      gGrid.append(line);
    });
    
    // Axis Labels
    const gAxisY = document.createElementNS("http://www.w3.org/2000/svg", "g");
    gAxisY.classList.add("axis");
    [0, 0.25, 0.5, 0.75, 1].forEach(p => {
      const v = p * yMax;
      const y = padT + chartH - p * chartH;
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", padL - 6);
      text.setAttribute("y", y + 3);
      text.setAttribute("text-anchor", "end");
      text.textContent = chartMetric === 'cost' ? '$' + (v < 1 ? v.toFixed(2) : Math.round(v)) : fmtTok(v);
      gAxisY.append(text);
    });

    // Bars
    const gBars = document.createElementNS("http://www.w3.org/2000/svg", "g");
    days.forEach((d, i) => {
      const x = padL + i * step + (step - bw) / 2;
      let yAcc = padT + chartH;
      stacks.forEach((s, si) => {
        const v = seriesOf(d, s);
        if (v <= 0) return;
        const h = (v / yMax) * chartH;
        yAcc -= h;
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", x);
        rect.setAttribute("y", yAcc);
        rect.setAttribute("width", bw);
        rect.setAttribute("height", h);
        rect.setAttribute("fill", s.color);
        if (si === stacks.length - 1) {
            rect.setAttribute("rx", "2");
        }
        rect.style.cursor = "pointer";
        rect.addEventListener("mousemove", (evt) => {
          const label = stackBy === 'provider' ? s.label : modelShortLabel(s.key);
          const modelData = s.ids
            .map(id => [id, d.models[id]])
            .filter(([, m]) => m);
          const reqs = modelData.reduce((sum, [, m]) => sum + (m.requests || 0), 0);
          const toks = modelData.reduce((sum, [, m]) => sum + ((m.inTok || 0) + (m.outTok || 0)), 0);
          showTooltip(evt, [
            `${fmtDate(d.date)} · ${label}`,
            `${chartMetric}: ${fmtMetricValue(chartMetric, v)}`,
            `cost: ${fmt$tight(modelData.reduce((sum, [, m]) => sum + (m.cost || 0), 0))}`,
            `${fmtInt(reqs)} req · ${fmtTok(toks)} tok`,
          ]);
        });
        rect.addEventListener("mouseleave", hideTooltip);
        gBars.append(rect);
      });
    });

    const shouldShowXTick = (i, len) => {
      if (len <= 7) return true;
      if (i === 0 || i === len - 1) return true;
      const every = len > 30 ? 7 : 3;
      return i % every === 0;
    };
    const gAxisX = document.createElementNS("http://www.w3.org/2000/svg", "g");
    gAxisX.classList.add("axis");
    days.forEach((d, i) => {
      if (!shouldShowXTick(i, days.length)) return;
      const x = padL + i * step + step / 2;
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", x);
      text.setAttribute("y", H - 8);
      text.setAttribute("text-anchor", "middle");
      text.textContent = fmtDate(d.date);
      gAxisX.append(text);
    });

    svg.append(gGrid, gAxisY, gBars, gAxisX);

    const setM = (m) => { 
      chartMetric = m; 
      const url = new URL(window.location);
      url.searchParams.set("metric", m);
      window.history.replaceState({}, "", url);
      renderActivity(); 
    };
    const setS = (s) => { 
      stackBy = s; 
      const url = new URL(window.location);
      url.searchParams.set("stack", s);
      window.history.replaceState({}, "", url);
      renderActivity(); 
    };

    return el("div", { class: "card card-pad", style: "flex: 1.95; min-height: 420px; display: flex; flex-direction: column;" },
      el("div", { style: "display: flex; justify-content: space-between; margin-bottom: 14px;" },
        el("div", {},
          el("div", { class: "act-eyebrow" }, el("span", { class: "act-dot terra" }), `Daily ${chartMetric}`),
          el("div", { class: "act-serif med", style: "margin-top: 8px;" }, 
            chartMetric === 'cost' ? fmt$(totals.reduce((a,b)=>a+b,0)) : fmtTok(totals.reduce((a,b)=>a+b,0)) + " " + chartMetric
          )
        ),
        el("div", { style: "display: flex; flex-direction: column; align-items: flex-end; gap: 8px;" },
          el("div", { class: "seg" }, 
            [["cost", "$"], ["tokens", "tokens"], ["requests", "reqs"]].map(([k, l]) => el("button", { 
              class: chartMetric === k ? "on" : "", 
              onclick: () => setM(k) 
            }, l))
          ),
          el("div", { class: "seg" }, 
            [["provider", "by provider"], ["model", "by model"]].map(([k, l]) => el("button", { 
              class: stackBy === k ? "on" : "", 
              onclick: () => setS(k) 
            }, l))
          )
        )
      ),
      el("div", { style: "flex: 1; min-height: 260px;" }, svg),
      el("div", { style: "display: flex; flex-wrap: wrap; gap: 14px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line);" },
        stacks.map(s => el("div", { style: "display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--ink-2);" },
          el("span", { style: `width: 10px; height: 10px; border-radius: 2px; background: ${s.color};` }),
          el("span", { class: "act-mono" }, s.label)
        ))
      )
    );
  }

  function buildModelBreakdown(days) {
    const agg = aggregate(days);
    const rows = Object.entries(agg.totalsByModel)
      .map(([id, stats]) => ({ id, label: modelShortLabel(id), color: getModelColor(id), ...stats }))
      .filter(r => r.cost > 0.0001)
      .sort((a, b) => b.cost - a.cost);
      
    const total = rows.reduce((s, r) => s + r.cost, 0);
    const max = Math.max(...rows.map(r => r.cost), 0.0001);

    return el("div", { class: "card card-pad", style: "flex: 0.95; min-height: 420px; max-height: 420px; display: flex; flex-direction: column;" },
      el("div", { style: "display: flex; align-items: center; margin-bottom: 18px;" },
        el("span", { class: "act-dot slate", style: "margin-right: 8px;" }),
        el("span", { class: "act-eyebrow" }, "Spend by model"),
        el("span", { class: "act-micro", style: "margin-left: auto;" }, `${rows.length} active`)
      ),
      el("div", { class: "act-model-scroll", style: "display: flex; flex-direction: column; gap: 12px; overflow-y: auto; padding-right: 6px;" },
        rows.map(r => {
          const w = (r.cost / max) * 100;
          const pct = (r.cost / total) * 100;
          return el("div", {
            onmousemove: (evt) => showTooltip(evt, [
              r.label,
              `cost: ${fmt$tight(r.cost)}`,
              `${fmtInt(r.requests)} req · ${fmtTok(r.inTok + r.outTok)} tok`,
              `${pct.toFixed(1)}% of spend`,
            ]),
            onmouseleave: hideTooltip,
            style: "cursor: default;"
          },
            el("div", { style: "display: flex; justify-content: space-between; margin-bottom: 4px;" },
              el("span", { class: "act-mono", style: "font-size: 12px;" }, r.label),
              el("span", { class: "act-num", style: "font-size: 12.5px; font-weight: 600;" }, fmt$tight(r.cost))
            ),
            el("div", { class: "bar-mini", style: "width: 100%; height: 8px; position: relative;" },
              el("div", { style: `position: absolute; left: 0; top: 0; bottom: 0; width: ${w}%; background: ${r.color}; border-radius: 4px;` })
            ),
            el("div", { style: "display: flex; justify-content: space-between; margin-top: 4px;" },
              el("span", { class: "act-micro" }, `${fmtInt(r.requests)} req · ${fmtTok(r.inTok + r.outTok)} tok`),
              el("span", { class: "act-micro act-num" }, `${pct.toFixed(1)}%`)
            )
          );
        })
      )
    );
  }

  function buildProviderCard(pData, days) {
    const isAnth = pData.provider === 'anthropic';
    const accent = isAnth ? 'var(--slate)' : 'var(--forest)';
    const modelIds = Object.keys(days[0]?.models || {}).filter(id => isAnth ? id.includes("claude") : !id.includes("claude"));
    
    const dailyCost = days.map(d => modelIds.reduce((s, id) => s + (d.models[id]?.cost || 0), 0));
    const totalCost = dailyCost.reduce((a, b) => a + b, 0);
    const totalReq = days.reduce((s, d) => s + modelIds.reduce((sum, id) => sum + (d.models[id]?.requests || 0), 0), 0);
    const totalTok = days.reduce((s, d) => s + modelIds.reduce((sum, id) => sum + (d.models[id]?.inTok || 0) + (d.models[id]?.outTok || 0), 0), 0);
    const avg = totalCost / Math.max(1, days.length);

    const balance = pData.balanceRemaining || 0;
    const budget = pData.budget || balance || 0;
    const used = Math.max(0, budget - balance);
    const pctUsed = budget > 0 ? (used / budget) * 100 : 0;
    const runway = avg > 0 ? balance / avg : 0;

    const state = balance <= 0 ? 'depleted' : (runway < 3 ? 'warn' : 'active');

    return el("div", { class: "card card-pad" },
      el("div", { style: "display: flex; align-items: center; gap: 10px; margin-bottom: 14px;" },
        el("span", { class: `act-dot ${isAnth ? 'slate' : 'forest'}` }),
        el("span", { class: "act-eyebrow" }, pData.provider),
        StatusPill(state, balance <= 0 ? 'depleted' : (runway < 3 ? runway.toFixed(1) + "d left" : 'active')),
        el("span", { class: "act-micro act-num", style: "margin-left: auto;" }, "live")
      ),
      el("div", { style: "display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 20px;" },
        el("div", {},
          el("div", { class: "act-micro" }, "Period spend"),
          el("div", { class: "act-serif big", style: "margin-top: 6px;" }, fmt$(totalCost)),
          el("div", { class: "act-micro", style: "margin-top: 4px;" }, 
            `${fmtInt(totalReq)} req · ${fmtTok(totalTok)} tok · avg `,
            el("span", { class: "act-num" }, fmt$tight(avg) + "/d")
          )
        ),
        Sparkline(dailyCost, { stroke: accent, fill: isAnth ? 'rgba(77,122,154,0.18)' : 'rgba(58,107,78,0.18)', width: 160, height: 56 })
      ),
      el("div", { style: "margin-bottom: 20px;" },
        el("div", { style: "display: flex; justify-content: space-between; margin-bottom: 6px;" },
          el("span", { class: "act-micro", style: "font-weight: 600;" }, `Budget · ${fmt$(budget, 0)}`),
          el("span", { class: "act-num", style: `font-size: 12.5px; font-weight: 600; color: ${balance <= 0 ? 'var(--rust)' : 'var(--ink)'};` }, fmt$(balance) + " left")
        ),
        el("div", { class: "bar-mini", style: "width: 100%; height: 10px; position: relative; border: 1px solid var(--line);" },
          el("div", { style: `position: absolute; left: 0; top: 0; bottom: 0; width: ${pctUsed}%; background: ${accent}; border-radius: 5px;` })
        ),
        el("div", { class: "act-micro", style: "display: flex; justify-content: space-between; margin-top: 6px;" },
          el("span", {}, Math.round(pctUsed) + "% used"),
          el("span", {}, balance <= 0 ? el("span", { style: "color: var(--rust); font-weight: 600;" }, "top-up needed") : 
            ["runway ", el("span", { class: "act-num", style: `font-weight: 600; color: ${runway < 3 ? 'var(--rust)' : 'var(--ink-2)'};` }, runway.toFixed(1) + "d"), " at current rate"]
          )
        )
      ),
      el("div", { style: "display: grid; grid-template-columns: 1fr auto; row-gap: 8px; border-top: 1px solid var(--line); padding-top: 12px;" },
        el("div", { class: "act-micro" }, "Active model"),
        el("div", { class: "act-mono", style: "font-size: 12.5px;" }, pData.activeModel),
        el("div", { class: "act-micro" }, "Last used"),
        el("div", { class: "act-num", style: "font-size: 12.5px;" }, pData.lastUsedAt ? pData.lastUsedAt.split('T')[0] : "—")
      )
    );
  }

  function buildHeatmap(data) {
    const cells = data.cells || [];
    const max = Math.max(...cells.flat(), 1);
    const dowLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "100%");
    svg.setAttribute("viewBox", "0 0 680 220");
    svg.setAttribute("preserveAspectRatio", "xMinYMid meet");

    // Hour labels
    const gHours = document.createElementNS("http://www.w3.org/2000/svg", "g");
    gHours.classList.add("axis");
    for (let h = 0; h < 24; h += 3) {
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", 56 + h * 25 + 11);
      text.setAttribute("y", 14);
      text.setAttribute("text-anchor", "middle");
      text.textContent = h === 0 ? "12a" : (h === 12 ? "12p" : (h > 12 ? (h - 12) + "p" : h + "a"));
      gHours.append(text);
    }
    svg.append(gHours);

    cells.forEach((row, dow) => {
      const gRow = document.createElementNS("http://www.w3.org/2000/svg", "g");
      gRow.setAttribute("transform", `translate(0, ${28 + dow * 25})`);
      
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", 48);
      text.setAttribute("y", 15);
      text.setAttribute("text-anchor", "end");
      text.setAttribute("fill", "var(--ink-3)");
      text.setAttribute("font-size", "10.5");
      text.setAttribute("font-family", "var(--font-mono)");
      text.textContent = dowLabels[dow];
      gRow.append(text);

      row.forEach((v, h) => {
        const t = v / max;
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", 56 + h * 25);
        rect.setAttribute("y", 0);
        rect.setAttribute("width", 22);
        rect.setAttribute("height", 22);
        rect.setAttribute("rx", 3);
        rect.setAttribute("fill", `oklch(${(0.95 - t * 0.55).toFixed(3)} ${(0.02 + t * 0.10).toFixed(3)} 55)`);
        gRow.append(rect);
      });
      svg.append(gRow);
    });

    return el("div", { class: "card card-pad", style: "flex: 2;" },
      el("div", { style: "display: flex; align-items: center; margin-bottom: 14px;" },
        el("span", { class: "act-dot terra", style: "margin-right: 8px;" }),
        el("span", { class: "act-eyebrow" }, "When you use it")
      ),
      svg,
      el("div", { style: "display: flex; align-items: center; gap: 8px; margin-top: 6px;" },
        el("span", { class: "act-micro" }, "less"),
        el("div", { style: "display: flex; gap: 2px;" },
          [0.1, 0.3, 0.5, 0.7, 0.9].map(t => el("span", { style: `width: 14px; height: 10px; border-radius: 2px; background: oklch(${(0.95 - t * 0.55).toFixed(3)} ${(0.02 + t * 0.10).toFixed(3)} 55);` }))
        ),
        el("span", { class: "act-micro" }, "more")
      )
    );
  }

  function pricingFor(id, pricing) {
    // Normalize: strip or- prefix, lowercase, try both raw and stripped forms
    const norm = (id.startsWith('or-') ? id.slice(3) : id).toLowerCase();
    return pricing[id] || pricing[norm] || { inputPerMTok: 3.0, outputPerMTok: 15.0 };
  }

  function buildEfficiency(days, pricing) {
    const agg = aggregate(days);
    // Cache hit rate: fraction of total context served from cache
    const totalContext = agg.inTok + agg.cacheTok + agg.cacheCreateTok;
    const cacheRatio = totalContext > 0 ? agg.cacheTok / totalContext : 0;

    // Cache savings: cache-read tokens at 90% discount vs full input price
    let savedCost = 0;
    Object.entries(agg.totalsByModel).forEach(([id, stats]) => {
        const p = pricingFor(id, pricing);
        savedCost += (stats.cacheTok * p.inputPerMTok * 0.9) / 1e6;
    });

    const outToIn = agg.outTok / Math.max(1, agg.inTok);
    const costPerK = agg.cost / ((agg.inTok + agg.outTok) / 1000 || 1);

    const Tile = (label, val, sub, color) => el("div", { style: "padding: 12px 14px; border-radius: 10px; background: var(--card-2); border: 1px solid var(--line);" },
      el("div", { class: "act-micro" }, label),
      el("div", { class: "act-serif sm act-num", style: `color: ${color}; margin-top: 6px;` }, val),
      el("div", { class: "act-micro", style: "margin-top: 4px;" }, sub)
    );

    return el("div", { class: "card card-pad", style: "flex: 1;" },
      el("div", { style: "display: flex; align-items: center; margin-bottom: 14px;" },
        el("span", { class: "act-dot slate", style: "margin-right: 8px;" }),
        el("span", { class: "act-eyebrow" }, "Efficiency")
      ),
      el("div", { style: "display: grid; grid-template-columns: 1fr 1fr; gap: 10px;" },
        Tile("Cache hit rate", (cacheRatio * 100).toFixed(1) + "%", "of input tokens", "var(--slate)"),
        Tile("Cache savings", fmt$(savedCost), "vs uncached", "var(--forest)"),
        Tile("Output : Input", (outToIn * 100).toFixed(1) + "%", "token ratio", "var(--terra)"),
        Tile("Cost per 1k tok", fmt$tight(costPerK), "blended avg", "var(--plum)")
      )
    );
  }

  function buildTopSessions(sessions) {
    if (!sessions?.length) {
      return el("div", { class: "card card-pad" },
        el("div", { style: "display: flex; align-items: center; margin-bottom: 14px;" },
          el("span", { class: "act-dot plum", style: "margin-right: 8px;" }),
          el("span", { class: "act-eyebrow" }, "Costliest conversations")
        ),
        el("div", { class: "t-body t-muted", style: "padding: 12px 0; font-style: italic;" }, "No sessions in this period.")
      );
    }
    const sorted = sortSessions(sessions);
    const max = Math.max(...sorted.map(s => s.cost), 0.0001);
    const sortMark = (key) =>
      sessionSortKey === key ? (sessionSortDir === "asc" ? " ↑" : " ↓") : "";

    const headerCell = (col) => {
      const cls = [col.align === "right" ? "right" : "", col.sortable ? "act-th-sort" : ""]
        .filter(Boolean)
        .join(" ");
      const kids = [col.label];
      if (col.sortable && col.key) {
        kids.push(el("span", { class: "act-sort-ind" }, sortMark(col.key)));
      }
      return el(
        "th",
        {
          class: cls || undefined,
          onclick: col.sortable && col.key ? () => toggleSessionSort(col.key) : undefined,
        },
        ...kids
      );
    };

    return el("div", { class: "card card-pad" },
      el("div", { style: "display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px;" },
        el("div", { style: "display: flex; align-items: center;" },
          el("span", { class: "act-dot plum", style: "margin-right: 8px;" }),
          el("span", { class: "act-eyebrow" }, "Costliest conversations")
        ),
        el("span", { class: "act-micro" }, `${sorted.length} in period`)
      ),
      el("div", { class: "tbl-wrap" },
        el("table", { class: "tbl" },
          el("thead", {},
            el("tr", {}, ...SESSION_SORT_COLS.map(headerCell))
          ),
          el("tbody", {},
            sorted.map(s => {
              const color = getModelColor(s.modelId);
              return el("tr", {
                onmousemove: (evt) => showTooltip(evt, [
                  s.title,
                  `cost: ${fmt$tight(s.cost)}`,
                  `${modelShortLabel(s.modelId)} · ${fmtInt(s.msgs)} req`,
                  `${fmtTok(s.tokens)} tok · ${s.lastActivityAt}`,
                ]),
                onmouseleave: hideTooltip,
              },
                el("td", {}, el("div", { style: "font-weight: 500;" }, s.title), el("div", { class: "act-micro act-mono" }, s.id)),
                el("td", {}, el("span", { class: "act-mono", style: "color: var(--ink-2);" }, modelShortLabel(s.modelId))),
                el("td", { class: "right act-num" }, fmtInt(s.msgs)),
                el("td", { class: "right act-num" }, fmtTok(s.tokens)),
                el("td", { class: "right act-num", style: "font-weight: 600;" }, fmt$tight(s.cost)),
                el("td", {}, el("span", { class: "bar-mini", style: `width: ${(s.cost / max) * 120}px; background: ${color};` })),
                el("td", { class: "right act-num act-micro" }, s.lastActivityAt)
              );
            })
          )
        )
      )
    );
  }

  /* ── Main Render ── */
  async function renderActivity() {
    const root = document.querySelector("#panel-activity");
    if (!root) return;

    // Load URL state if present
    const params = new URLSearchParams(window.location.search);
    if (params.has("period")) currentPeriod = params.get("period");
    if (params.has("metric")) chartMetric = params.get("metric");
    if (params.has("stack")) stackBy = params.get("stack");

    const D = window.BernieData || {};
    const now = Date.now();
    const isStale = !window._lastActivityFetch || (now - window._lastActivityFetch > 15000);

    if (!D.activity || D.activity.period !== currentPeriod || isStale) {
        if (!D.activity || D.activity.period !== currentPeriod) {
            root.innerHTML = '<div style="padding: 40px; text-align: center;" class="act-skel act-serif med">Loading activity...</div>';
        }
        window._lastActivityFetch = now;
        window.api(`/api/activity?period=${currentPeriod}`).then(res => {
            window.BernieData.activity = res;
            window._lastActivityFetch = Date.now();
            renderActivity();
        }).catch(e => {
            console.error("Failed to background refresh activity:", e);
            if (!D.activity) {
                root.innerHTML = '<div style="padding: 40px; text-align: center; color: var(--err);">Failed to load activity.</div>';
            }
        });

        if (!D.activity || D.activity.period !== currentPeriod) {
            return;
        }
    }

    const data = D.activity;
    root.innerHTML = "";
    root.classList.add("page", "page-fade", "page-usage");

    try {
      root.append(
        buildHeader(currentPeriod, data.lastSync),
        buildKpiStrip(data.daily, currentPeriod === "7d" ? 7 : currentPeriod === "90d" ? 90 : 30),
        el("div", { class: "activity-row" },
          buildDailyChart(data.daily),
          buildModelBreakdown(data.daily)
        ),
        el("div", { class: "activity-provider-grid" },
          data.accounts.map(acc => buildProviderCard(acc, data.daily))
        ),
        el("div", { class: "activity-row" },
          buildHeatmap(data.heatmap),
          buildEfficiency(data.daily, data.pricing)
        ),
        buildTopSessions(data.topSessions)
      );
    } catch (e) {
      console.error("renderActivity render error:", e);
      root.innerHTML = `<div style="padding: 40px; text-align: center; color: var(--rust);">Render error: ${e.message}</div>`;
    }
  }

  window.renderActivity = renderActivity;
})();
