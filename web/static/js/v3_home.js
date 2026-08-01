/**
 * Bernie v3 - Home Screen Redesign (v2 Layout)
 * Living (rooms/comfort) for family; Plant (HA ops) for admin/parents under More.
 */

(function() {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const el = (...args) => window.el(...args);

  const api = window.api;

  /** family-bot-ngo: true admin — full Plant/HA workshop */
  function isHomePlantAdmin() {
    const role = ((window.Me && window.Me.role) || "").toLowerCase();
    return role === "admin";
  }
  /** Parents + admin can open Plant/More; kids stay Living-only */
  function canSeePlantLayer() {
    if (typeof window.isKidUser === "function" && window.isKidUser()) return false;
    return typeof window.isAdminUser === "function" && window.isAdminUser();
  }

  const TEMP_PALETTE = [
    'oklch(0.72 0.13 30)', 'oklch(0.78 0.12 90)', 'oklch(0.78 0.13 140)',
    'oklch(0.72 0.13 200)', 'oklch(0.72 0.13 260)', 'oklch(0.7 0.16 320)',
    'oklch(0.78 0.12 60)'
  ];

  // ── Temperature chart ─────────────────────────────────────────────────────
  const buildTempMultiChart = (sensors) => {
    const ns = "http://www.w3.org/2000/svg";
    const W = 1000, H = 100, padX = 0, padY = 8;
    let gMin = Infinity, gMax = -Infinity;
    const series = sensors.map((s, i) => {
      const vals = (s.history || []).map(h => parseFloat(h.v || h.state)).filter(v => !isNaN(v));
      if (vals.length) { gMin = Math.min(gMin, ...vals); gMax = Math.max(gMax, ...vals); }
      return { vals, color: TEMP_PALETTE[i % TEMP_PALETTE.length] };
    });
    if (gMin === Infinity) return el("div", { style: "height: 40px" });
    const range = gMax - gMin || 1;
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", "100%"); svg.setAttribute("height", H); svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("preserveAspectRatio", "none");
    series.forEach(({ vals, color }) => {
      if (vals.length < 2) return;
      const pts = vals.map((v, i) => ({ x: padX + (i / (vals.length - 1)) * (W - padX * 2), y: H - padY - ((v - gMin) / range) * (H - padY * 2) }));
      const line = document.createElementNS(ns, "polyline");
      line.setAttribute("fill", "none"); line.setAttribute("stroke", color); line.setAttribute("stroke-width", "2"); line.setAttribute("points", pts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" "));
      svg.appendChild(line);
    });
    return svg;
  };

  // ── Layout Components ─────────────────────────────────────────────────────

  const buildSnapshotStrip = (D) => {
    const lightsOn = (D.rooms || []).flatMap(r => r.lights || []).filter(l => l.on).length;
    const lightsTotal = (D.rooms || []).flatMap(r => r.lights || []).length;
    const switchesOn = (D.switches || []).filter(sw => sw.on).length;
    const switchesTotal = (D.switches || []).length;
    const playingCount = (D.media || []).filter(m => m.is_playing).length;
    const playingTotal = (D.media || []).length;
    const autosOn = (D.automations || []).filter(a => a.enabled).length;
    const autosTotal = (D.automations || []).length;

    let alerts = 0;
    (D.system || []).forEach(e => {
      const isUpdate = e.entity_id.startsWith("update.");
      const isBinary = e.entity_id.startsWith("binary_sensor.");
      const hasUpdate = isUpdate && e.latest_version && e.installed_version && e.latest_version !== e.installed_version;
      if (!e.available || (isBinary && e.state === "off") || hasUpdate) alerts++;
    });

    const kid = typeof window.isKidUser === "function" && window.isKidUser();
    // family-bot-ngo / 0ty: Living snapshot = comfort; Automations/System only on Plant (admin)
    const livingTiles = kid
      ? [
          { label: "Lights", num: lightsOn, den: lightsTotal, sub: "on", color: "var(--amber)" },
          { label: "Music", num: playingCount, den: playingTotal, sub: "playing", color: "var(--ok)" },
        ]
      : [
          { label: "Lights", num: lightsOn, den: lightsTotal, sub: "on", color: "var(--amber)" },
          { label: "Switches", num: switchesOn, den: switchesTotal, sub: "", color: "var(--info)" },
          { label: "Playing", num: playingCount, den: playingTotal, sub: "", color: "var(--ok)" },
        ];
    const plantTiles = isHomePlantAdmin()
      ? [
          { label: "Automations", num: autosOn, den: autosTotal, sub: "", color: "var(--ok)" },
          { label: "System", num: alerts > 0 ? alerts : "ok", den: null, sub: alerts > 0 ? "alerts" : "", color: alerts > 0 ? "var(--amber)" : "var(--ink-3)" },
        ]
      : [];
    const tiles = livingTiles.concat(plantTiles);

    return el("div", { class: "hv2-snap" },
      tiles.map(t => el("div", { class: "hv2-snap-card" },
        el("div", { class: "hv2-snap-label" }, t.label),
        el("div", { class: "hv2-snap-val" },
          el("span", { style: `color: ${t.color}` }, String(t.num)),
          t.den != null ? el("span", { style: "color: var(--ink-4); font-size: 20px;" }, ` /${t.den}`) : null,
          el("span", { style: "font-size: 14px; color: var(--ink-4); margin-left: 6px; font-family: var(--font-sans); font-weight: 400;" }, t.sub)
        )
      ))
    );
  };

  let _tempRange = '24h', _pinnedSensors = [], _hoverSensor = null, _isFetchingTemps = false;

  const buildTemperatureRibbon = (sensors) => {
    if (!sensors.length) return null;
    return el("div", { class: "hv2-temp-ribbon" },
      el("div", { class: "row between", style: "margin-bottom: 16px; align-items: center;" },
        el("div", { class: "hv2-snap-label" }, "Temperatures"),
        el("div", { class: "row gap-2" }, ['1h', '24h', '7d'].map(r =>
          el("button", { class: `btn ${_tempRange === r ? 'btn-primary' : ''}`, style: "padding: 4px 10px; font-size: 11px; border-radius: 999px;", onclick: () => { _tempRange = r; fetchTemps(); } }, r)
        ))
      ),
      el("div", { class: "hv2-temp-grid" },
        sensors.map((s, i) => {
          const color = TEMP_PALETTE[i % TEMP_PALETTE.length];
          const isPinned = _pinnedSensors.includes(s.entity_id);
          const isFocused = _hoverSensor === s.entity_id || (_hoverSensor == null && isPinned);
          const val = parseFloat(s.current);
          const valStr = isNaN(val) ? "—" : val.toFixed(1);
          // family-bot-hcd: hover is CSS-only; pin click debounced full paint (not every mousemove)
          return el("div", {
            class: `hv2-temp-cell ${isFocused ? 'active' : ''}`,
            role: "button",
            tabindex: "0",
            "data-entity-id": s.entity_id,
            style: isFocused ? `border-color: ${color};` : "",
            onmouseenter: (ev) => {
              _hoverSensor = s.entity_id;
              const root = document.getElementById("panel-home");
              if (!root) return;
              root.querySelectorAll(".hv2-temp-cell").forEach(c => {
                const on = c.getAttribute("data-entity-id") === s.entity_id || _pinnedSensors.includes(c.getAttribute("data-entity-id"));
                c.classList.toggle("active", on);
              });
            },
            onmouseleave: () => {
              _hoverSensor = null;
              const root = document.getElementById("panel-home");
              if (!root) return;
              root.querySelectorAll(".hv2-temp-cell").forEach(c => {
                c.classList.toggle("active", _pinnedSensors.includes(c.getAttribute("data-entity-id")));
              });
            },
            onclick: () => {
              _pinnedSensors = isPinned ? _pinnedSensors.filter(id => id !== s.entity_id) : [..._pinnedSensors, s.entity_id];
              if (window.renderHome) window.renderHome(true);
            },
            onkeydown: (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                _pinnedSensors = isPinned ? _pinnedSensors.filter(id => id !== s.entity_id) : [..._pinnedSensors, s.entity_id];
                if (window.renderHome) window.renderHome(true);
              }
            }
          },
            el("div", { style: `font-family: var(--font-serif); font-size: 24px; color: ${color}` }, valStr, el("span", { style: "font-size: 14px; margin-left: 2px" }, "°")),
            el("div", { style: "font-size: 12px; font-weight: 500; color: var(--ink); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" }, s.name || s.label),
            el("div", { class: "t-mono", style: "font-size: 10px; color: var(--ink-4); margin-top: 2px" }, `${s.min?.toFixed(1) || '—'}–${s.max?.toFixed(1) || '—'}`)
          );
        })
      ),
      buildTempMultiChart(sensors)
    );
  };

  const buildSystemHealthStrip = (system) => {
    // family-bot-ngo: system/HA updates stay on Plant (admin), not Living family Home
    if (!isHomePlantAdmin()) return null;
    if (!system.length) return null;
    return el("div", { class: "hv2-sys" },
      system.map(e => {
        const isUpdate = e.entity_id.startsWith("update.");
        const isBinary = e.entity_id.startsWith("binary_sensor.");
        const hasUpdate = isUpdate && e.latest_version && e.installed_version && e.latest_version !== e.installed_version;
        const isWarn = !e.available || (isBinary && e.state === "off") || hasUpdate;

        let val = e.state;
        if (isUpdate) val = hasUpdate ? `ready` : "up to date";
        else if (isBinary) val = e.state === "on" ? "online" : "offline";
        else if (e.unit) val = `${e.state}${e.unit}`;

        return el("div", { class: `hv2-sys-pill ${isWarn ? 'warn' : ''}` },
          el("div", { class: "dot" }),
          el("span", { style: "opacity: 0.6" }, e.name),
          el("span", { class: "val", style: "font-weight: 600" }, val)
        );
      }),
      el("button", { 
        class: "btn", 
        style: "height: 24px; font-size: 10px; padding: 0 10px; margin-left: auto;",
        onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true; btn.textContent = "Checking…";
          try {
            const r = await api("/api/system/check-updates", { method: "POST" });
            if (window.flashBernie) window.flashBernie(`Refreshed ${r.refreshed ?? "?"} entities`);
            const D = window.BernieData = window.BernieData || {};
            D.system = await api("/api/system");
            if (window.renderHome) window.renderHome(true);
          } catch (_) {}
          btn.disabled = false; btn.textContent = "Check for updates";
        }
      }, "Check for updates")
    );
  };

  const openLightPopover = async (lightId, targetEl) => {
    const existing = document.querySelector('.light-popover');
    if (existing) existing.remove();

    const lights = (window.BernieData?.rooms || []).flatMap(r => r.lights || []);
    const l = lights.find(l => l.id === lightId);
    if (!l) return;

    const pop = el("div", {
      class: "light-popover",
      style: `position: fixed; z-index: 9999; background: var(--bg-card); border: 1px solid var(--stroke); border-radius: var(--r-lg); padding: 16px; min-width: 240px; box-shadow: var(--shadow-lg);`,
      onclick: (e) => e.stopPropagation()
    });

    let curOn = l.on, curBrightness = l.brightness ?? 255, curColorTemp = l.color_temp ?? 4000, curRgb = l.rgb_color || [255, 255, 255];
    let debounceTimer = null;

    const send = (attrs = {}) => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(async () => {
        try {
          await api(`/api/lights/${l.id}`, { method: "POST", body: { on: curOn, ...attrs } });
          Object.assign(l, { on: curOn, ...attrs });
          if (window.renderHome) window.renderHome(true);
          // Soft re-fetch after mutations so dashboard cache + other clients stay honest.
          if (typeof window.refreshHomeData === "function") {
            clearTimeout(window._homeRefreshTimer);
            window._homeRefreshTimer = setTimeout(() => window.refreshHomeData(), 400);
          }
        } catch (e) {
          if (window.flashBernie) window.flashBernie("Light update failed");
        }
      }, 150);
    };

    const toggle = el("button", {
      class: `btn ${curOn ? 'btn-primary' : ''}`,
      style: "width: 100%; margin-bottom: 12px",
      onclick: () => { curOn = !curOn; send({}); toggle.textContent = curOn ? "Turn Off" : "Turn On"; toggle.className = `btn ${curOn ? 'btn-primary' : ''}`; }
    }, curOn ? "Turn Off" : "Turn On");
    pop.append(el("div", { style: "font-weight: 600; margin-bottom: 12px; font-size: 14px; color: var(--ink)" }, l.name), toggle);

    if (l.supports_brightness) {
      const slider = el("input", { type: "range", min: "0", max: "255", value: curBrightness, style: "width: 100%",
        oninput: (e) => { curBrightness = parseInt(e.target.value); send({ brightness: curBrightness }); }
      });
      pop.append(el("div", { style: "margin-bottom: 12px" }, el("label", { style: "display: block; margin-bottom: 4px; font-size: 12px; color: var(--ink-3)" }, "Brightness"), slider));
    }
    if (l.supports_color_temp) {
      const slider = el("input", { type: "range", min: "2700", max: "6500", value: curColorTemp, style: "width: 100%",
        oninput: (e) => { curColorTemp = parseInt(e.target.value); send({ color_temp: curColorTemp }); }
      });
      pop.append(el("div", { style: "margin-bottom: 12px" }, el("label", { style: "display: block; margin-bottom: 4px; font-size: 12px; color: var(--ink-3)" }, "Color Temp"), slider));
    }
    if (l.supports_rgb) {
      const toHex = (rgb) => "#" + rgb.map(v => v.toString(16).padStart(2, '0')).join("");
      const picker = el("input", { type: "color", value: toHex(curRgb), style: "width: 100%; height: 32px; border: none; background: transparent; cursor: pointer",
        oninput: (e) => {
          const hex = e.target.value;
          curRgb = [parseInt(hex.slice(1,3),16), parseInt(hex.slice(3,5),16), parseInt(hex.slice(5,7),16)];
          send({ rgb: curRgb });
        }
      });
      pop.append(el("div", {}, el("label", { style: "display: block; margin-bottom: 4px; font-size: 12px; color: var(--ink-3)" }, "Color"), picker));
    }

    document.body.append(pop);
    const isMobile = window.innerWidth < 600;
    if (targetEl && !isMobile) {
      const rect = targetEl.getBoundingClientRect();
      const popRect = pop.getBoundingClientRect();
      let top = rect.bottom + 8, left = rect.left;
      if (top + popRect.height > window.innerHeight - 8) top = rect.top - popRect.height - 8;
      if (left + popRect.width > window.innerWidth - 16) left = window.innerWidth - popRect.width - 16;
      if (left < 8) left = 8;
      pop.style.top = `${Math.max(8, top)}px`; pop.style.left = `${left}px`;
    } else {
      pop.style.top = '50%'; pop.style.left = '50%'; pop.style.transform = 'translate(-50%, -50%)';
    }
    const close = () => { if (pop.parentNode) pop.remove(); document.removeEventListener('mousedown', check); };
    const check = (e) => { if (!pop.contains(e.target) && !targetEl?.contains(e.target)) close(); };
    setTimeout(() => document.addEventListener('mousedown', check), 0);
  };
  window.openLightPopover = openLightPopover;

  const buildLightTile = (l) => {
    const isOn = !!l.on;
    const orb = isOn
      ? el("div", { style: "width: 28px; height: 28px; border-radius: 50%; background: radial-gradient(circle at 35% 35%, #f0c084, #c97f2c); box-shadow: 0 0 14px rgba(217,152,83,.5);" })
      : el("div", { style: "width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--stroke-strong); background: transparent;" });
    
    // Convert 'last_changed' ISO string to a friendly time if available
    let timeStr = "—";
    if (l.last_changed) {
      const d = new Date(l.last_changed);
      const isToday = d.toDateString() === new Date().toDateString();
      timeStr = isToday ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : `yesterday ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`;
    }

    // family-bot-hcd: data-light-id enables surgical WS patch without full innerHTML
    return el("div", {
      class: "card hv2-light-tile",
      "data-light-id": l.id,
      "data-on": isOn ? "1" : "0",
      role: "button",
      tabindex: "0",
      style: `display: flex; flex-direction: column; justify-content: space-between; height: 110px; padding: 14px; background: ${isOn ? 'linear-gradient(180deg, rgba(217,152,83,.05), transparent 60%), var(--bg-card)' : 'var(--bg-card)'}; border-color: ${isOn ? 'rgba(217,152,83,.15)' : 'var(--stroke)'}; cursor: pointer;`,
      onclick: (e) => window.openLightPopover ? window.openLightPopover(l.id, e.currentTarget) : null,
      onkeydown: (e) => { if ((e.key === "Enter" || e.key === " ") && window.openLightPopover) { e.preventDefault(); window.openLightPopover(l.id, e.currentTarget); } }
    },
      el("div", { class: "row between", style: "align-items: flex-start" },
        orb,
        el("div", { style: `width: 6px; height: 6px; border-radius: 50%; background: ${isOn ? 'var(--ink-4)' : 'var(--stroke-strong)'};` })
      ),
      el("div", { style: "margin-top: auto;" },
        el("div", { class: "t-h3", style: "font-size: 14px; font-weight: 500; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 2px;" }, l.name || l.id),
        el("div", { class: "t-mono", style: "font-size: 10px; color: var(--ink-4);" }, l.last_changed && !timeStr.startsWith('—') ? (timeStr.includes('yesterday') ? `↻ ${timeStr}` : timeStr) : " ")
      )
    );
  };

  const buildMediaRow = (mp) => {
    const isPlaying = mp.is_playing;
    const volPct = mp.volume != null ? Math.round(mp.volume * 100) : null;
    const cmd = (c, extra = {}) => async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/media/${mp.id}`, { method: "POST", body: { command: c, ...extra } });
        if (window.refreshHomeData) await window.refreshHomeData();
        else {
          const DD = window.BernieData = window.BernieData || {};
          DD.media = await api("/api/media");
          if (window.renderHome) window.renderHome(true);
        }
      } catch (err) {
        if (window.flashBernie) window.flashBernie("Media command failed");
      }
    };

    return el("div", { class: "hv2-media-row", style: `flex-direction: row; align-items: center; justify-content: space-between; padding: 12px 14px; opacity: ${mp.available ? 1 : 0.4}` },
      el("div", { class: "row gap-3", style: "align-items: center; flex: 1; min-width: 0;" },
        el("button", { 
          class: "btn btn-icon", 
          style: `width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0; background: ${isPlaying ? 'oklch(0.72 0.13 140)' : 'var(--bg-pill)'}; border: 1px solid ${isPlaying ? 'transparent' : 'var(--stroke)'}; color: ${isPlaying ? '#fff' : 'var(--ink-2)'};`,
          onclick: cmd(isPlaying ? "pause" : "play")
        }, isPlaying ? "⏸" : "▶"),
        el("div", { class: "col", style: "flex: 1; min-width: 0" },
          el("div", { class: "t-h3", style: "font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" }, mp.name),
          el("div", { style: "font-size: 11px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" }, 
            mp.media_title ? (mp.media_artist ? `${mp.media_artist} — ${mp.media_title}` : mp.media_title) : (mp.state || "idle")
          )
        )
      ),
      volPct != null && el("div", { class: "row gap-2", style: "align-items: center; width: 100px; flex-shrink: 0; margin-left: 12px;" },
        el("input", {
          type: "range", class: "hv2-vol", min: "0", max: "100", value: volPct, style: `flex: 1; accent-color: ${isPlaying ? 'oklch(0.72 0.13 140)' : 'var(--ink-4)'};`,
          onchange: (e) => api(`/api/media/${mp.id}`, { method: "POST", body: { command: "volume_set", volume: e.target.value / 100 } })
        }),
        el("div", { class: "t-mono", style: "font-size: 10px; color: var(--ink-4); width: 18px; text-align: right;" }, volPct)
      )
    );
  };

  const buildAirQualityMatrix = (climate) => {
    if (!climate.length) return null;
    const byRoom = {};
    climate.forEach(s => { (byRoom[s.room_label] = byRoom[s.room_label] || {})[s.icon] = s; });
    
    // family-bot-co8: rooms from config/API; empty filter → all climate rooms
    const targetRooms =
      (window.Me && Array.isArray(window.Me.air_quality_rooms) && window.Me.air_quality_rooms.length)
        ? window.Me.air_quality_rooms
        : (window.BernieData && Array.isArray(window.BernieData.air_quality_rooms) && window.BernieData.air_quality_rooms.length)
          ? window.BernieData.air_quality_rooms
          : null;
    const filteredRooms = targetRooms
      ? Object.entries(byRoom).filter(([room]) => targetRooms.includes(room))
      : Object.entries(byRoom);
    if (!filteredRooms.length) return null;

    const cHum = "oklch(0.72 0.13 200)", cCo2 = "oklch(0.72 0.13 140)", cPm = "oklch(0.72 0.13 30)", cVoc = "oklch(0.7 0.16 320)";
    const dot = (color) => el("span", { class: "aq-dot", style: `background: ${color};` });

    return el("div", { class: "card", style: "margin-top: 48px; padding: 20px 24px;" },
      el("div", { class: "row between", style: "margin-bottom: 20px; align-items: baseline;" },
        el("div", { class: "t-eyebrow" }, "Air Quality"),
        el("div", { class: "t-mono", style: "font-size: 10px; color: var(--ink-4);" }, "humidity · CO₂ · PM2.5 · VOC")
      ),
      el("table", { class: "hv2-climate" },
        el("thead", {}, el("tr", {}, 
          el("th", {}, ""), 
          el("th", {}, dot(cHum), "Humidity %"), 
          el("th", {}, dot(cCo2), "CO₂ ppm"), 
          el("th", {}, dot(cPm), "PM2.5 µg"), 
          el("th", {}, dot(cVoc), "VOC ppb")
        )),
        el("tbody", {}, filteredRooms.map(([room, sensors]) => {
          const getVal = (key, color) => {
            const s = sensors[key];
            if (!s || s.value == null || s.value === "unavailable") return el("span", { style: "color: var(--ink-4);" }, "—");
            const v = parseFloat(s.value);
            const isWarn = (key === 'co2' && v > 1000) || (key === 'pm25' && v > 15) || (key === 'humidity' && (v > 65 || v < 30));
            return el("span", { style: `color: ${color};` }, v.toFixed(0), isWarn ? el("span", { style: "font-size: 10px; margin-left: 4px;" }, "△") : null);
          };
          return el("tr", {},
            el("td", { style: "font-weight: 500; color: var(--ink); font-family: var(--font-sans); font-size: 13px;" }, room),
            el("td", {}, getVal("humidity", cHum)),
            el("td", {}, getVal("co2", cCo2)),
            el("td", {}, getVal("pm25", cPm)),
            el("td", {}, getVal("voc", cVoc))
          );
        }))
      )
    );
  };

  // ── Main Render ───────────────────────────────────────────────────────────

  /** family-bot-hcd: patch one light tile + snap counts without full Home rebuild */
  function patchHomeLight(id, on) {
    const root = $("#panel-home");
    if (!root) return false;
    const tile = root.querySelector(`.hv2-light-tile[data-light-id="${CSS.escape(id)}"]`);
    if (!tile) return false;
    tile.setAttribute("data-on", on ? "1" : "0");
    tile.style.background = on
      ? "linear-gradient(180deg, rgba(217,152,83,.05), transparent 60%), var(--bg-card)"
      : "var(--bg-card)";
    tile.style.borderColor = on ? "rgba(217,152,83,.15)" : "var(--stroke)";
    const orb = tile.querySelector(":scope > div > div");
    if (orb) {
      if (on) {
        orb.style.cssText = "width: 28px; height: 28px; border-radius: 50%; background: radial-gradient(circle at 35% 35%, #f0c084, #c97f2c); box-shadow: 0 0 14px rgba(217,152,83,.5);";
      } else {
        orb.style.cssText = "width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--stroke-strong); background: transparent;";
      }
    }
    // Update Lights snap card if present
    const snap = root.querySelector(".hv2-snap");
    if (snap && window.BernieData?.rooms) {
      const lights = window.BernieData.rooms.flatMap(r => r.lights || []);
      const onN = lights.filter(l => l.on).length;
      const first = snap.querySelector(".hv2-snap-card .hv2-snap-val span");
      if (first) first.textContent = String(onN);
    }
    return true;
  }
  window.patchHomeLight = patchHomeLight;

  async function renderHome(force = false) {
    const root = $("#panel-home");
    if (!root) return;
    const D = window.BernieData || {};

    // Surgical Update for background refreshes only — user interactions pass force=true
    // family-bot-hcd: default path avoids full innerHTML wipe
    if (!force && root.querySelector('.hv2-snap')) {
      const climateContainer = root.querySelector(".hv2-climate-container");
      if (climateContainer) {
        const newClimate = buildAirQualityMatrix(D.climate || []);
        if (newClimate) climateContainer.replaceWith(el("div", { class: "hv2-climate-container" }, newClimate));
      }
      return;
    }

    root.innerHTML = "";
    root.className = "page page-fade";
    root.style.maxWidth = "1000px";

    // family-bot-ngo: family eyebrow is "Home" — never "Home Assistant" on Living path
    const eyebrow = isHomePlantAdmin() ? "Plant · control" : "Around the house";
    root.append(el("div", { class: "row between", style: "align-items: flex-end; margin-bottom: 24px" },
      el("div", {},
        el("div", { class: "t-eyebrow", "data-home-eyebrow": "1" }, eyebrow),
        el("div", { class: "t-h1", style: "font-size: 32px; font-family: var(--font-serif); font-weight: 400; margin-top: 4px" }, "Home")
      ),
      el("div", { class: "row gap-2" },
        el("button", { class: "btn", style: "padding: 8px 16px; min-width: 60px; line-height: 1.3; white-space: pre-line; text-align: left;", onclick: async () => {
          try { await api("/api/automations/lights-out", { method: "POST" }); if (window.flashBernie) window.flashBernie("Sent 'All Off' command."); }
          catch (e) {}
        }}, "All\noff"),
        el("button", { class: "btn", style: "padding: 8px 16px; min-width: 80px; line-height: 1.3; white-space: pre-line; text-align: left;", onclick: async () => {
          try { await api("/api/automations/sleep-mode", { method: "POST" }); if (window.flashBernie) window.flashBernie("Sleep mode activated."); }
          catch (e) {}
        }}, "Sleep\nmode")
      )
    ));

    // HA load error / empty spine — never silent blank grids for OSS operators
    if (D._homeLoadError) {
      root.append(el("div", {
        class: "card",
        style: "padding: 20px; border-color: var(--warn); margin-bottom: 24px;",
      },
        el("div", { style: "font-weight: 600; color: var(--warn); margin-bottom: 8px;" },
          isHomePlantAdmin() ? "Home Assistant unreachable" : "House controls are taking a break"),
        el("div", { class: "t-body", style: "color: var(--ink-2); margin-bottom: 12px;" },
          isHomePlantAdmin() ? D._homeLoadError : "Try again in a moment, or ask Bernie."),
        el("button", {
          class: "btn btn-primary",
          type: "button",
          onclick: async () => {
            if (window.refreshHomeData) await window.refreshHomeData();
          },
        }, "Retry")
      ));
    } else if (D._homeLoaded && !(D.rooms && D.rooms.length) && !(D.switches && D.switches.length)) {
      const kid = typeof window.isKidUser === "function" && window.isKidUser();
      root.append(el("div", {
        class: "card",
        style: "padding: 20px; margin-bottom: 24px;",
      },
        el("div", { style: "font-weight: 600; margin-bottom: 8px;" },
          kid ? "Nothing to control right now" : "Nothing showing yet"),
        el("div", { class: "t-body", style: "color: var(--ink-2);" },
          kid
            ? "When the house lights and rooms are ready, they’ll show up here. Ask Bernie if you need something."
            // family-bot-q69: operator config.json only for admin; parents get Bernie voice
            : (typeof window.isAdminUser === "function" && window.isAdminUser() && (window.Me?.role === "admin")
              ? "Configure home_assistant in config.json (URL + token + entities). Lights, switches, media, and climate show up here once Home Assistant is connected."
              : "The house is quiet on this screen. When lights and rooms are connected, they'll appear here."))
      ));
    }

    // Snapshot
    root.append(buildSnapshotStrip(D));

    // Temperature Ribbon
    const tempRibbon = buildTemperatureRibbon(D.temps || []);
    if (tempRibbon) root.append(tempRibbon);

    // System Health Strip
    const sysStrip = buildSystemHealthStrip(D.system || []);
    if (sysStrip) root.append(sysStrip);

    // Working Area (2 Cols)
    const leftCol = el("div", { class: "col", style: "gap: 24px" });
    
    if (D.rooms?.length) {
      D.rooms.forEach(room => {
        const lights = room.lights || [];
        const onCount = lights.filter(l => l.on).length;
        const floorLabel = room.name === "MAIN" ? "MAIN FLOOR" : room.name;
        leftCol.append(el("div", { class: "col", style: "gap: 12px" },
          el("div", { class: "row between", style: "align-items: baseline" },
            el("div", { style: "font-size: 13px; font-weight: 600; color: var(--ink); letter-spacing: 0.5px;" }, floorLabel),
            el("div", { class: "t-mono", style: "color: var(--ink-4); font-size: 10px" }, `${onCount}/${lights.length} on`)
          ),
          el("div", { class: "hv2-light-grid" }, lights.map(l => buildLightTile(l)))
        ));
      });
    }

    const rightCol = el("div", { class: "col", style: "gap: 32px" });
    
    // Switches
    if (D.switches?.length) {
      rightCol.append(el("div", { class: "col", style: "gap: 12px" },
        el("div", { style: "font-size: 16px; font-weight: 600; color: var(--ink); border-bottom: 1px solid var(--stroke); padding-bottom: 8px;" }, "Switches"),
        el("div", { class: "hv2-switch-list" }, D.switches.map(sw => el("div", { 
          class: "hv2-switch-row",
          role: "button",
          tabindex: "0",
          onclick: async () => {
            try {
              await api(`/api/switches/${sw.id}`, { method: "POST", body: { on: !sw.on } });
              if (window.refreshHomeData) await window.refreshHomeData();
              else {
                const DD = window.BernieData = window.BernieData || {};
                DD.switches = await api("/api/switches");
                renderHome(true);
              }
            } catch (_) {
              if (window.flashBernie) window.flashBernie("Switch update failed");
            }
          },
          onkeydown: async (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              try {
                await api(`/api/switches/${sw.id}`, { method: "POST", body: { on: !sw.on } });
                if (window.refreshHomeData) await window.refreshHomeData();
                else {
                  const DD = window.BernieData = window.BernieData || {};
                  DD.switches = await api("/api/switches");
                  renderHome(true);
                }
              } catch (_) {
                if (window.flashBernie) window.flashBernie("Switch update failed");
              }
            }
          }
        },
          el("div", { class: "row gap-3" }, 
            el("div", { style: `width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; background: ${sw.on ? 'rgba(217,152,83,.1)' : 'var(--bg-pill)'}; color: ${sw.on ? 'var(--amber)' : 'var(--ink-4)'};` }, "⚡"),
            el("span", { style: "font-size: 13px; line-height: 1.3;" }, sw.name)
          ),
          el("div", { class: `toggle`, style: "flex-shrink: 0; margin-left: 8px;", "data-on": sw.on ? "1" : "0" }, el("i"))
        )))
      ));
    }

    // Media
    if (D.media?.length) {
      rightCol.append(el("div", { class: "col", style: "gap: 12px" },
        el("div", { style: "font-size: 16px; font-weight: 600; color: var(--ink); border-bottom: 1px solid var(--stroke); padding-bottom: 8px;" }, "Media"),
        D.media.map(mp => buildMediaRow(mp))
      ));
    }

    // family-bot-ngo: automations live under Plant (admin) or collapsible More for parents
    const autoBlock = () => {
      if (!D.automations?.length) return null;
      return el("div", { class: "col", style: "gap: 12px" },
        el("div", { style: "font-size: 16px; font-weight: 600; color: var(--ink); border-bottom: 1px solid var(--stroke); padding-bottom: 8px;" }, "Automations"),
        D.automations.map(a => el("div", {
          class: "hv2-auto-row",
          style: "display: flex; align-items: center; justify-content: space-between;"
        },
          el("div", {
            style: "flex: 1; display: flex; align-items: center; cursor: pointer;",
            title: "Toggle automation",
            onclick: async () => {
              try {
                await api(`/api/ha/automations/${a.id}/toggle`, { method: "POST" });
                a.enabled = !a.enabled;
                renderHome(true);
                if (window.refreshHomeData) {
                  clearTimeout(window._homeRefreshTimer);
                  window._homeRefreshTimer = setTimeout(() => window.refreshHomeData(), 400);
                }
              } catch (_) {
                if (window.flashBernie) window.flashBernie("Automation toggle failed");
              }
            }
          },
            el("span", { style: "font-size: 13px" }, a.name),
            el("div", { class: `toggle`, style: "margin-left: 12px;", "data-on": a.enabled ? "1" : "0" }, el("i"))
          )
        ))
      );
    };

    if (isHomePlantAdmin()) {
      const ab = autoBlock();
      if (ab) rightCol.append(ab);
    }

    root.append(el("div", { class: "hv2-cols" }, leftCol, rightCol));

    // Parents: Plant ops under progressive More (not kids)
    if (canSeePlantLayer() && !isHomePlantAdmin()) {
      const more = el("details", { class: "card", style: "margin-top: 24px; padding: 16px;", "data-home-plant-more": "1" },
        el("summary", { style: "cursor: pointer; font-weight: 600; color: var(--ink-2);" }, "More · house plant"),
        el("div", { class: "col", style: "gap: 16px; margin-top: 16px;" },
          autoBlock() || el("div", { class: "t-body", style: "color: var(--ink-3);" }, "No automations right now.")
        )
      );
      root.append(more);
    }

    // Air Quality Matrix
    const climateTable = buildAirQualityMatrix(D.climate || []);
    if (climateTable) {
      root.append(el("div", { class: "hv2-climate-container" }, climateTable));
    }

    root.append(el("div", { style: "font-family: var(--font-mono); font-size: 10px; color: var(--ink-4); text-align: right; margin-top: 32px; text-transform: uppercase;" },
      "Last updated " + new Date().toLocaleTimeString())
    );
  }

  const fetchTemps = async () => {
    if (_isFetchingTemps) return;
    _isFetchingTemps = true;
    try {
      const hours = _tempRange === '1h' ? 1 : (_tempRange === '7d' ? 168 : 24);
      const data = await api(`/api/temperatures?hours=${hours}`);
      if (data) {
        (window.BernieData = window.BernieData || {}).temps = data;
        // family-bot-hcd/rgy: only paint Home when active; debounce poll rebuilds
        if (window._activePanel === "home" && window.renderHome) {
          clearTimeout(window._tempHomePaint);
          window._tempHomePaint = setTimeout(() => window.renderHome(true), 400);
        }
      }
    } catch (e) {} finally { _isFetchingTemps = false; }
  };

  window.renderHome = renderHome;
  window.refreshTemps = fetchTemps;
})();
