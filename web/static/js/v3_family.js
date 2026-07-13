(function() {
  const $ = (sel, root = document) => root.querySelector(sel);

  const el = (...args) => window.el(...args);

  const PERSON_ACCENTS = {
    dad: 'oklch(0.72 0.14 50)',
    mom: 'oklch(0.72 0.13 200)',
    child1:  'oklch(0.7 0.16 320)',
    child2:  'oklch(0.72 0.13 140)',
  };

  let _openMemoryId = null;
  let _memoryCache  = {};

  function BatteryIcon({ pct, isAway }) {
    const lo  = pct < 30;
    const crit = pct < 15;
    const color = crit ? 'var(--err)' : (lo ? 'var(--warn)' : 'var(--ok)');
    
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width",   "20");
    svg.setAttribute("height",  "11");
    svg.setAttribute("viewBox", "0 0 20 11");
    svg.setAttribute("fill",    "none");
    
    if (isAway) {
      if (crit) svg.style.animation = "pulse-err 1.5s ease-in-out infinite";
      else if (lo) svg.style.animation = "pulse-warn 2s ease-in-out infinite";
    }

    const rect1 = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect1.setAttribute("x", "0.5"); rect1.setAttribute("y", "0.5");
    rect1.setAttribute("width", "17"); rect1.setAttribute("height", "10");
    rect1.setAttribute("rx", "2"); rect1.setAttribute("stroke", "var(--ink-3)");

    const rect2 = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect2.setAttribute("x", "18"); rect2.setAttribute("y", "3.5");
    rect2.setAttribute("width", "1.5"); rect2.setAttribute("height", "4");
    rect2.setAttribute("rx", "0.5"); rect2.setAttribute("fill", "var(--ink-3)");

    const rect3 = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect3.setAttribute("x", "2"); rect3.setAttribute("y", "2");
    rect3.setAttribute("width", Math.max(1, (pct / 100) * 14).toString());
    rect3.setAttribute("height", "7");
    rect3.setAttribute("rx", "1");
    rect3.setAttribute("fill", color);

    svg.append(rect1, rect2, rect3);
    return svg;
  }

  function openEmailModal(p) {
    const existing = document.getElementById("email-modal");
    if (existing) existing.remove();

    const modal = el("div", { id: "email-modal", style: "position: fixed; inset: 0; background: rgba(0,0,0,.6); display: flex; align-items: center; justify-content: center; z-index: 200;" });
    const box   = el("div", { class: "card card-pad", style: "width: 440px; max-width: 90vw; display: flex; flex-direction: column; gap: 12px;" });

    const toInput      = el("input",    { class: "input", value: p.email || "", placeholder: "To" });
    const subjectInput = el("input",    { class: "input", placeholder: "Subject" });
    const bodyInput    = el("textarea", { class: "input", placeholder: "Body…", rows: "5", style: "resize: vertical;" });
    const statusEl     = el("div",      { style: "font-size: 12px; color: var(--err)" });
    const sendBtn      = el("button",   { class: "btn btn-primary" }, "Send");
    const cancelBtn    = el("button",   { class: "btn btn-ghost"  }, "Cancel");

    sendBtn.addEventListener("click", async () => {
      const to      = toInput.value.trim();
      const subject = subjectInput.value.trim();
      const body    = bodyInput.value.trim();
      if (!to || !subject || !body) { statusEl.textContent = "All fields required."; return; }
      sendBtn.disabled = true;
      statusEl.textContent = "";
      try {
        await window.api("/api/email/send", { method: "POST", body: { to, subject, body } });
        if (window.flashBernie) window.flashBernie("Email sent to " + to);
        modal.remove();
      } catch (e) {
        statusEl.textContent = "Failed to send.";
        sendBtn.disabled = false;
      }
    });
    cancelBtn.addEventListener("click", () => modal.remove());
    modal.addEventListener("click", e => { if (e.target === modal) modal.remove(); });

    box.append(
      el("div", { class: "t-h2" }, `Email ${p.name}`),
      el("div", { class: "col", style: "gap: 8px" }, toInput, subjectInput, bodyInput),
      statusEl,
      el("div", { class: "row gap-2" }, sendBtn, cancelBtn)
    );
    modal.append(box);
    document.body.append(modal);
    subjectInput.focus();
  }

  function formatLastSeen(ts) {
    if (!ts) return "unknown";
    const d = new Date(ts);
    const now = new Date();
    const diff = now - d;
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));
    
    const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    
    if (days === 0 && d.getDate() === now.getDate()) {
      return `seen ${timeStr}`;
    } else if (days === 0 || (days === 1 && d.getDate() !== now.getDate())) {
      return `seen Yesterday ${timeStr}`;
    } else if (days < 7) {
      return `seen ${days}d ago`;
    } else {
      return `seen ${d.toLocaleDateString([], { month: "short", day: "numeric" })}`;
    }
  }

  function toDMS(lat, lon) {
    const convert = (val, pos, neg) => {
      const abs = Math.abs(val);
      const d = Math.floor(abs);
      const m = Math.floor((abs - d) * 60);
      const s = ((abs - d - m/60) * 3600).toFixed(1);
      return `${d}\u00B0${m}'${s}"${val >= 0 ? pos : neg}`;
    };
    return `${convert(lat, "N", "S")} ${convert(lon, "E", "W")}`;
  }

  async function refreshAndRedraw() {
    await window.api("/api/presence/refresh", { method: "POST" });
    const fresh = await window.api("/api/family");
    if (fresh && window.BernieData) window.BernieData.family = fresh;
    if (window.renderFamily) await window.renderFamily();
    if (window.renderShell)  window.renderShell();
  }

  async function renderFamily() {
    const root = $("#panel-family");
    if (!root) return;

    const D      = window.BernieData || {};
    const family = D.family || [];

    const draw = () => {
      root.innerHTML = "";
      root.className = "page page-fade";
      root.style.maxWidth = "1040px";

      const homeCount = family.filter(p => p.home).length;
      const awayCount = family.length - homeCount;

      root.append(
        el("div", { class: "row between", style: "align-items: flex-end; margin-bottom: 4px" },
          el("div", {},
            el("div", { class: "t-eyebrow" }, "Household"),
            el("div", { class: "t-h1", style: "font-size: 28px; font-family: var(--font-serif); font-weight: 400; margin-top: 4px" }, "Family"),
            el("div", { class: "t-meta t-muted", style: "margin-top: 6px" }, `${homeCount} home · ${awayCount} away`)
          ),
          el("div", { class: "row gap-2" },
            el("button", {
              class: "btn",
              onclick: async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true; btn.textContent = "Refreshing...";
                try {
                  await refreshAndRedraw();
                  if (window.flashBernie) window.flashBernie("Presence re-scanned.");
                } finally {
                  btn.disabled = false; btn.textContent = "Refresh all ↻";
                }
              }
            }, "Refresh all ↻")
          )
        ),
        el("div", { style: "display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px" },
          family.map(p => buildPersonCard(p))
        )
      );
    };

    const buildPersonCard = (p) => {
      const isHome = p.home;
      const accent = PERSON_ACCENTS[p.who?.toLowerCase()] || 'oklch(0.72 0.14 50)';
      const isOpen = _openMemoryId === p.who;

      return el("div", { class: "card", style: "overflow: hidden" },
        el("div", { style: `height: 3px; background: ${accent}` }),

        el("div", { style: "padding: 14px 16px 12px" },
          el("div", { class: "row gap-3", style: "align-items: center" },
            el("div", {
              style: `width: 38px; height: 38px; border-radius: 50%; display: grid; place-items: center; background: color-mix(in oklch, ${accent} 18%, var(--bg-card)); border: 1px solid color-mix(in oklch, ${accent} 40%, transparent); color: ${accent}; font-family: var(--font-serif); font-size: 18px;`
            }, p.initial || p.name[0]),
            el("div", { class: "col", style: "flex: 1; gap: 0" },
              el("div", { class: "t-h2" }, p.name),
              el("div", { class: "t-meta t-muted" }, p.role)
            ),
            el("div", { class: "col", style: "align-items: flex-end; gap: 4px" },
              p.departing ? el("div", { class: "chip", style: "background: var(--warn); color: white; border-color: transparent" },
                el("span", { class: "dot", style: "background: white" }),
                "Departing..."
              ) : el("div", { class: `chip ${isHome ? 'home' : 'away'}` },
                el("span", { class: "dot" }),
                p.status_label || (isHome ? 'home' : 'away')
              ),
              p.wifi ? el("div", { class: "chip", style: "background: var(--bg-card-2); color: var(--ink-2); border: 1px solid var(--stroke)" },
                el("span", { class: "dot", style: "background: var(--ok)" }),
                p.essid || "WiFi"
              ) : null
            )
          ),

          el("div", { class: "row between", style: "margin-top: 12px; align-items: center" },
            el("div", { class: "row gap-3", style: "font-size: 12px; color: var(--ink-3); align-items: center" },
              BatteryIcon({ pct: p.battery || 0, isAway: !p.home }),
              el("span", {}, `${p.battery || 0}%`),
              el("span", { style: "color: var(--ink-4)" }, "·"),
              el("span", {}, formatLastSeen(p.last_seen))
            ),
            el("button", {
              class: "btn btn-ghost",
              style: "font-size: 11px; padding: 2px 8px; height: auto; min-height: 0",
              onclick: async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true; btn.textContent = "Re-checking...";
                try {
                  await refreshAndRedraw();
                  if (window.flashBernie) window.flashBernie(`Household presence re-scanned.`);
                } finally {
                  btn.disabled = false; btn.textContent = "Refresh ↻";
                }
              }
            }, "Refresh ↻")
          ),

          p.gps ? (() => {
            const mapId = `map-${p.who}-${Date.now()}`;
            const wrapper = el("div", { style: "margin-top: 14px; background: var(--bg-card-2); border-radius: var(--r-md); border: 1px solid var(--stroke); overflow: hidden" },
              el("div", { class: "row between", style: "padding: 10px 12px 6px; align-items: center" },
                el("div", { class: "t-eyebrow", style: "font-size: 9px; opacity: 0.6" }, "LIVE LOCATION"),
                el("a", {
                  href: `https://www.google.com/maps/search/?api=1&query=${p.gps.lat},${p.gps.lon}`,
                  target: "_blank",
                  style: "font-size: 11px; color: var(--accent); text-decoration: none; font-weight: 500"
                }, "Open in Maps ↗")
              ),
              el("div", { id: mapId, style: "height: 160px; width: 100%; z-index: 0" }),
              el("div", { style: "padding: 8px 12px 10px" },
                el("div", { style: "font-size: 12px; color: var(--ink); font-family: var(--font-mono); letter-spacing: -0.02em" },
                  toDMS(p.gps.lat, p.gps.lon)
                ),
                p.address ? el("div", { style: "font-size: 11px; color: var(--ink-3); margin-top: 3px;" }, p.address) : null
              )
            );
            // Initialize map after DOM insertion
            requestAnimationFrame(() => {
              setTimeout(() => {
                const container = document.getElementById(mapId);
                if (!container || !window.L) return;
                const accent = PERSON_ACCENTS[p.who?.toLowerCase()] || 'oklch(0.72 0.14 50)';
                const map = L.map(container, {
                  zoomControl: false,
                  attributionControl: false,
                  dragging: false,
                  scrollWheelZoom: false,
                  doubleClickZoom: false,
                  touchZoom: false,
                  boxZoom: false,
                  keyboard: false
                }).setView([p.gps.lat, p.gps.lon], 15);
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
                  maxZoom: 19
                }).addTo(map);
                const pin = L.divIcon({
                  className: '',
                  html: `<div style="width:28px;height:28px;border-radius:50%;background:${accent};border:3px solid rgba(255,255,255,.9);box-shadow:0 2px 8px rgba(0,0,0,.4);display:grid;place-items:center;font-family:var(--font-serif);font-size:13px;color:#fff;font-weight:600">${(p.initial || p.name[0])}</div>`,
                  iconSize: [28, 28],
                  iconAnchor: [14, 14]
                });
                L.marker([p.gps.lat, p.gps.lon], { icon: pin }).addTo(map);
              }, 50);
            });
            return wrapper;
          })() : null,

          p.conflict_label ? el("div", { 
            style: "margin-top: 10px; font-size: 11px; color: var(--warn); font-weight: 600; display: flex; align-items: center; gap: 4px;" 
          }, 
            el("span", { style: "font-size: 14px" }, "⚠️"),
            p.conflict_label
          ) : null,

          el("div", { class: "row gap-2", style: "margin-top: 14px" },
            el("button", {
              class: "btn",
              style: "flex: 1",
              onclick: async () => {
                try {
                  await window.api(`/api/ping/${p.who}`, { method: "POST", body: { text: "Pinged from web UI" } });
                  if (window.flashBernie) window.flashBernie(`Pinged ${p.name} on Discord.`);
                } catch {
                  if (window.flashBernie) window.flashBernie(`Failed to ping: ${p.name}`);
                }
              }
            }, "Ping on Discord →"),
            el("button", {
              class: "btn",
              style: "flex: 1",
              onclick: () => {
                if (p.email) openEmailModal(p);
                else if (window.flashBernie) window.flashBernie(`No email for ${p.name}`);
              }
            }, "Email →")
          )
        ),

        el("button", {
          class: "row between",
          style: "width: 100%; padding: 10px 16px; background: transparent; border: 0; border-top: 1px solid var(--stroke); color: var(--ink-2); cursor: pointer; font-size: 12px; text-align: left;",
          onclick: () => toggleMemory(p.who)
        },
          el("span", {},
            "Memory · ",
            el("strong", { style: "color: var(--ink); fontWeight: 600" },
              (_memoryCache[p.who]
                ? ((_memoryCache[p.who].patterns?.length || 0) + (_memoryCache[p.who].recent_events?.length || 0))
                : (p.memory_count || 0)).toString()
            ),
            " notes"
          ),
          el("span", { style: `transform: ${isOpen ? 'rotate(180deg)' : 'none'}; transition: transform .2s; color: var(--ink-3)` }, "▾")
        ),

        isOpen ? buildMemoryBody(p) : null
      );
    };

    const buildMemoryBody = (p) => {
      const data = _memoryCache[p.who];
      if (!data) {
        return el("div", { style: "padding: 16px; border-top: 1px solid var(--stroke); background: var(--bg-card-2); font-size: 12.5px; color: var(--ink-3)" }, "Loading memory...");
      }

      const patterns = data.patterns      || [];
      const events   = data.recent_events || [];

      if (!patterns.length && !events.length) {
        return el("div", { style: "padding: 16px; border-top: 1px solid var(--stroke); background: var(--bg-card-2); font-size: 12.5px; color: var(--ink-3)" }, "No memory yet.");
      }

      const acked  = patterns.filter(x => x.type === "acknowledged").sort((a, b) => b.count - a.count).slice(0, 3);
      const missed = patterns.filter(x => x.type === "missed");
      const rows   = [];

      if (acked.length) {
        rows.push(el("div", { style: "font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-3); padding: 8px 0 4px" }, "Acknowledges"));
        acked.forEach(e => rows.push(
          el("div", { class: "row between", style: "padding: 6px 0; border-top: 1px solid var(--stroke); font-size: 12.5px; color: var(--ink-2)" },
            el("span", {}, e.title),
            el("span", { class: "t-mono", style: "color: var(--ink-3); font-size: 11px" }, `${e.count}×`)
          )
        ));
      }

      if (missed.length) {
        rows.push(el("div", { style: "font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-3); padding: 8px 0 4px" }, "Dropped balls"));
        missed.forEach(e => {
          const match  = events.find(r => r.type === "missed" && r.title === e.title);
          const delBtn = el("button", { class: "btn btn-ghost", style: "padding: 2px 8px; font-size: 10px; color: var(--err)" }, "×");
          delBtn.addEventListener("click", async () => {
            if (!match || !confirm(`Clear dropped ball for "${e.title}"?`)) return;
            try {
              await window.api(`/api/memory/${p.who}/event/${match.id}`, { method: "DELETE" });
              _memoryCache[p.who] = await window.api(`/api/memory/${p.who}`);
              draw();
            } catch {
              if (window.flashBernie) window.flashBernie("Failed to clear.");
            }
          });
          rows.push(
            el("div", { class: "row between", style: "padding: 6px 0; border-top: 1px solid var(--stroke); font-size: 12.5px; color: var(--ink-2); align-items: center" },
              el("span", {}, e.title),
              el("div", { class: "row gap-2" },
                el("span", { class: "t-mono", style: "color: var(--ink-3); font-size: 11px" }, `${e.count}×`),
                delBtn
              )
            )
          );
        });
      }

      const resetBtn = el("button", { class: "btn btn-ghost", style: "margin-top: 8px; font-size: 11px; color: var(--err)" }, "Reset all memory");
      resetBtn.addEventListener("click", async () => {
        if (!confirm(`Delete all memory entries for ${p.name}?`)) return;
        try {
          await window.api(`/api/memory/${p.who}`, { method: "DELETE" });
          delete _memoryCache[p.who];
          draw();
        } catch {
          if (window.flashBernie) window.flashBernie("Memory reset failed.");
        }
      });

      return el("div", { style: "padding: 4px 16px 16px; border-top: 1px solid var(--stroke); background: var(--bg-card-2)" },
        ...rows,
        resetBtn
      );
    };

    const toggleMemory = async (who) => {
      if (_openMemoryId === who) {
        _openMemoryId = null;
      } else {
        _openMemoryId = who;
        if (!_memoryCache[who]) {
          try {
            _memoryCache[who] = await window.api(`/api/memory/${who}`);
          } catch (e) {
            console.error("Failed to load memory:", e);
          }
        }
      }
      draw();
    };

    draw();
  }

  window.renderFamily = renderFamily;
})();
