(function() {
  const LIST_FMT = new Intl.ListFormat("en", { style: "long", type: "conjunction" });
  const buildEl = (...args) => window.el(...args);

  function getGreeting() {
    const h = new Date().getHours();
    if (h < 5)  return "Up late,";
    if (h < 12) return "Good morning,";
    if (h < 17) return "Good afternoon,";
    if (h < 21) return "Good evening,";
    return "Evening,";
  }

  // Server mood/icon enums from /api/today — keep display mapping only, no re-classification.
  const MOOD_ICON = {
    stormy: "⛈", rainy: "🌧", foggy: "🌫", snowy: "❄️",
    sunny: "☀️", cloudy: "☁️", default: "🌤",
  };
  const HOURLY_ICON = {
    sun: "☀️", cloud: "☁️", drizzle: "🌧", fog: "🌫", storm: "⛈", snow: "❄️",
  };
  const MOOD_SCENE = {
    rainy:   "linear-gradient(180deg, #6b7f94 0%, #8fa3b5 45%, #9fb8c0 75%, #b0c4c8 100%)",
    stormy:  "linear-gradient(180deg, #4a5568 0%, #6b7f94 45%, #8fa3b5 100%)",
    cloudy:  "linear-gradient(180deg, #8a9aaa 0%, #a8b8c4 50%, #b8c8cc 100%)",
    foggy:   "linear-gradient(180deg, #aab8c0 0%, #c0ccd0 60%, #ccd4d0 100%)",
    snowy:   "linear-gradient(180deg, #9aa8b8 0%, #c8d0d8 50%, #e0e6ea 100%)",
    sunny:   "linear-gradient(180deg, #5b8db8 0%, #7aaacf 40%, #b8cdd8 75%, #c8d8c0 100%)",
    default: "linear-gradient(180deg, #5b8db8 0%, #7aaacf 40%, #b8cdd8 75%, #c8d8c0 100%)",
  };

  function deriveHumidity(temp, dewpoint) {
    if (dewpoint == null || temp == null) return null;
    return Math.round(100 * Math.exp(17.625 * dewpoint / (243.04 + dewpoint)) / Math.exp(17.625 * temp / (243.04 + temp)));
  }

  function parseTimeMins(timeStr) {
    if (!timeStr) return null;
    const m = timeStr.match(/(\d+):(\d+)\s*(am|pm)/i);
    if (!m) return null;
    let h = parseInt(m[1]);
    const mins = parseInt(m[2]);
    const ampm = m[3].toLowerCase();
    if (ampm === "pm" && h !== 12) h += 12;
    if (ampm === "am" && h === 12) h = 0;
    return h * 60 + mins;
  }

  function goPanel(id) {
    if (window.showPanel) window.showPanel(id);
    try { history.replaceState(null, "", `#${id}`); } catch (_) { location.hash = id; }
  }

  const _activeBlobUrls = [];
  window.addEventListener("beforeunload", () => {
    while (_activeBlobUrls.length) URL.revokeObjectURL(_activeBlobUrls.pop());
  });

  // Fetch a token-authenticated image URL and return a blob: URL
  async function fetchBlobUrl(path) {
    const t = localStorage.getItem("bernie-token") || "";
    const resp = await fetch(path, { headers: { "X-Bernie-Token": t } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    return URL.createObjectURL(blob);
  }

  // Full-screen lightbox for camera snapshots
  function openCameraLightbox(cameraId, label) {

    const overlay = buildEl("div", {
      style: "position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,.85); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px;"
    });

    const imgWrap = buildEl("div", {
      style: "position: relative; max-width: min(900px, 94vw); width: 100%;"
    });

    const img = buildEl("img", {
      style: "width: 100%; border-radius: 10px; display: block; opacity: 0; transition: opacity .3s;"
    });

    const statusLine = buildEl("div", {
      style: "color: rgba(255,255,255,.5); font-family: var(--font-mono); font-size: 12px; text-align: center;"
    }, "Loading…");

    const btnRow = buildEl("div", { style: "display: flex; gap: 12px; align-items: center;" },
      buildEl("button", {
        class: "btn btn-primary",
        onclick: () => loadSnap(true)
      }, "↺  Refresh"),
      buildEl("button", {
        class: "btn",
        style: "color: rgba(255,255,255,.6);",
        onclick: () => { URL.revokeObjectURL(img.src); overlay.remove(); }
      }, "Close")
    );

    const loadSnap = async (forceRefresh = false) => {
      statusLine.textContent = "Loading…";
      img.style.opacity = "0";
      try {
        const cacheBust = new Date().getTime();
        const url = await fetchBlobUrl(`/api/cameras/${cameraId}/snapshot?refresh=${forceRefresh}&_t=${cacheBust}`);
        if (img.src && img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
        img.src = url;
        img.onload = () => {
          img.style.opacity = "1";
          statusLine.textContent = `${label} · ${new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }).toLowerCase()}`;
        };
      } catch (e) {
        statusLine.textContent = "Snapshot unavailable.";
      }
    };

    imgWrap.append(img);
    overlay.append(
      buildEl("div", { style: "color: white; font-family: var(--font-sans); font-size: 15px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;" }, label),
      imgWrap,
      statusLine,
      btnRow
    );

    overlay.addEventListener("click", e => {
      if (e.target === overlay) { URL.revokeObjectURL(img.src); overlay.remove(); }
    });

    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { URL.revokeObjectURL(img.src); overlay.remove(); document.removeEventListener("keydown", esc); }
    });

    document.body.append(overlay);
    loadSnap();
  }


  // ── Weather card ────────────────────────────────────────────────────────────

  function buildWeatherCard(w) {
    const humidity = deriveHumidity(w.temp, w.dewpoint_c);
    const hourly = (w.hourly || []).slice(0, 5);
    const mood = w.mood || "default";
    const sceneBg = MOOD_SCENE[mood] || MOOD_SCENE.default;

    return buildEl("div", { class: "card card-pad", style: "display: flex; flex-direction: column; gap: 14px;" },
      buildEl("div", { class: "row between" },
        buildEl("div", { class: "card-tag" }, buildEl("span", { class: "pip info" }), "Weather"),
        buildEl("span", { style: "font-size: 18px; line-height: 1;" }, MOOD_ICON[mood] || MOOD_ICON.default)
      ),

      buildEl("div", {},
        buildEl("div", { style: "font-family: var(--font-serif); font-size: 22px; line-height: 1.15; font-weight: 400;" },
          `${w.condition || "Clear"} · ${w.temp}°C`
        ),
        buildEl("div", { class: "t-body", style: "margin-top: 5px; color: var(--ink-2);" }, w.recommendation || "Have a great day!")
      ),

      buildEl("div", { style: `border-radius: var(--r-md); height: 110px; background: ${sceneBg}; position: relative; overflow: hidden;` },
        buildEl("div", { style: "position: absolute; bottom: 0; left: 0; right: 0; height: 40px; background: linear-gradient(transparent, rgba(0,0,0,.15));" })
      ),

      buildEl("div", { style: "display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px;" },
        buildWeatherStat("FEELS LIKE", `${w.feelsLike}°`),
        buildWeatherStat("WIND", `${w.wind_kmh} km/h\n${w.wind_dir}`),
        buildWeatherStat("HUMIDITY", humidity != null ? `${humidity}%` : "—"),
        buildWeatherStat("SKY", (w.condition || "clear").toLowerCase())
      ),

      hourly.length > 0
        ? buildEl("div", { style: "border-top: 1px solid var(--stroke); padding-top: 12px;" },
            buildEl("div", { style: "display: flex; justify-content: space-between;" },
              ...hourly.map(h =>
                buildEl("div", { style: "display: flex; flex-direction: column; align-items: center; gap: 3px; flex: 1;" },
                  buildEl("div", { style: "font-size: 14px; line-height: 1;" }, HOURLY_ICON[h.icon] || "🌤"),
                  buildEl("div", { class: "t-mono", style: "font-size: 9.5px; color: var(--ink-3);" }, h.hr),
                  buildEl("div", { class: "t-mono", style: "font-size: 10px; color: var(--ink-2);" }, `${h.temp}°`)
                )
              )
            )
          )
        : null
    );
  }

  function buildWeatherStat(label, value) {
    return buildEl("div", { style: "display: flex; flex-direction: column; gap: 2px;" },
      buildEl("div", { class: "t-eyebrow", style: "font-size: 9px;" }, label),
      buildEl("div", { class: "t-mono", style: "font-size: 11px; color: var(--ink-2); white-space: pre-line;" }, value)
    );
  }

  // ── Bernie Says card ────────────────────────────────────────────────────────

  function buildBernieSaysCard(D) {
    return buildEl("div", { class: "card card-pad" },
      buildEl("div", { class: "card-tag", style: "margin-bottom: 10px;" },
        buildEl("span", { style: "font-size: 13px; line-height: 1; color: var(--ink-3);" }, "❝"),
        "Bernie says"
      ),
      buildEl("div", { 
        class: "t-body bernie-quote", 
        html: window.renderMarkdown(D.bernieNote || "Just keeping an eye on things.")
      })
    );
  }

  // ── Family card ─────────────────────────────────────────────────────────────

  function buildFamilyCard(D) {
    const COLORS = { dad: "var(--amber)", mom: "#6b9fd4", child1: "#6bbf8e", child2: "#b08eca" };
    const currentUser = D.user?.name?.toLowerCase() || "dad";

    const togglePresence = async (p, card) => {
      const next = !p.home;
      try {
        await window.api(`/api/presence/${p.id}/set`, {
          method: "POST",
          body: { home: next }
        });
        p.home = next;
        card.replaceWith(buildFamilyCard(D));
      } catch (e) {
        if (window.flashBernie) window.flashBernie(`Couldn't update ${p.name}'s presence.`);
      }
    };

    const card = buildEl("div", { class: "card card-pad" });

    const rows = (D.presence || []).map(p => {
      const color = COLORS[p.id?.toLowerCase()] || "var(--ink-3)";
      const isYou = p.name?.toLowerCase() === currentUser;

      // Use backend status_label (includes geofences like 'SacredHeart')
      const statusLabel = p.status_label || (p.home ? "Home" : "Away");
      const dotColor = p.tracked === false ? "var(--ink-4)" : (p.home ? (p.departing ? "var(--warn)" : "#6bbf8e") : "var(--amber)");

      const statusEl = buildEl("div", { style: "display: flex; flex-direction: column; align-items: flex-end; gap: 2px;" },
        buildEl("div", { style: "display: flex; align-items: center; gap: 6px;" },
          buildEl("div", {
            class: "t-meta",
            style: `font-size: 11.5px; color: ${p.tracked === false ? "var(--ink-3)" : "var(--ink-2)"}; ${p.tracked === false ? "cursor: pointer;" : ""}`,
            title: p.tracked === false ? "Tap to toggle — no device tracker" : ""
          }, statusLabel),
          buildEl("div", { style: `width: 7px; height: 7px; border-radius: 50%; background: ${dotColor};` })
        ),
        p.wifi ? buildEl("div", { style: "font-size: 9px; color: var(--ok); font-weight: 600; letter-spacing: 0.02em; text-transform: uppercase;" }, p.essid || "WIFI") : null
      );

      if (p.tracked === false) {
        statusEl.addEventListener("click", () => togglePresence(p, card));
      }

      const nameLabel = isYou ? `${p.name} (you)` : p.name;

      return buildEl("div", { style: "display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--stroke);" },
        buildEl("div", {
          style: `width: 26px; height: 26px; border-radius: 50%; background: ${color}22; border: 1.5px solid ${color}55; display: grid; place-items: center; font-size: 11px; font-weight: 600; color: ${color}; flex-shrink: 0;`
        }, p.initial || (p.name || "?")[0]),
        buildEl("div", { style: "flex: 1; font-size: 13px; font-weight: 500; color: var(--ink);" }, nameLabel),
        statusEl
      );
    });

    card.append(
      buildEl("div", { class: "row between", style: "margin-bottom: 8px;" },
        buildEl("div", { class: "card-tag" }, buildEl("span", { class: "pip" }), "Family"),
        buildEl("span", { class: "t-meta t-muted", style: "font-size: 11px;" }, `${(D.presence || []).filter(p => p.home).length} home`)
      ),
      ...rows,
      buildEl("button", {
        class: "btn",
        style: "width: 100%; margin-top: 10px; justify-content: space-between; color: var(--ink-3); font-size: 12px;",
        onclick: () => goPanel("people")
      },
        buildEl("span", {}, "View all"),
        buildEl("span", {}, "→")
      )
    );

    return card;
  }

  // ── Today schedule card ──────────────────────────────────────────────────────

  function buildTodayCard(D) {
    const allEvents = (D.schedule || []).flatMap(h => h.events);
    const now = D.now || { h: new Date().getHours(), m: new Date().getMinutes() };
    const nowMins = now.h * 60 + now.m;
    const scheduleLabel = D.schedule_label || "Today";
    const isTomorrow = scheduleLabel.toLowerCase() === "tomorrow";

    const rows = allEvents.map((e, i) => {
      const eMins = parseTimeMins(e.time);
      const isNow  = !isTomorrow && eMins != null && eMins <= nowMins && nowMins < eMins + 60;
      const isPast = !isTomorrow && eMins != null && eMins + 60 < nowMins;

      return buildEl("div", {
        style: `display: flex; align-items: center; gap: 8px; padding: 9px 0; ${i ? "border-top: 1px solid var(--stroke);" : ""} opacity: ${isPast ? 0.45 : 1}; ${isNow ? "border-left: 2px solid var(--amber); padding-left: 8px; margin-left: -8px;" : ""}`
      },
        buildEl("div", { class: "t-mono", style: `width: 56px; font-size: 10.5px; flex-shrink: 0; color: ${isNow ? "var(--amber)" : "var(--ink-3)"};` }, e.time || "—"),
        buildEl("div", { style: "flex: 1; font-size: 12.5px; color: var(--ink-2); line-height: 1.3;" }, e.title),
        isNow
          ? buildEl("div", { class: "chip", style: "background: var(--amber); color: white; font-size: 9.5px; padding: 2px 6px; border-color: transparent;" }, "Now")
          : (e.who && e.who !== "family"
              ? buildEl("div", { class: "chip", style: "font-size: 9.5px; padding: 2px 6px;" }, e.who)
              : null)
      );
    });

    return buildEl("div", { class: "card card-pad", style: "display: flex; flex-direction: column;" },
      buildEl("div", { class: "row between", style: "margin-bottom: 10px;" },
        buildEl("div", { class: "card-tag" }, buildEl("span", { class: "pip" }), scheduleLabel)
      ),

      allEvents.length === 0
        ? buildEl("div", { class: "t-body t-muted", style: "padding: 16px 0; font-style: italic; font-size: 13px;" }, `Nothing scheduled ${scheduleLabel.toLowerCase()}.`)
        : buildEl("div", {}, ...rows)
    );
  }

  // ── Quick actions bar ────────────────────────────────────────────────────────

  function buildQuickActions(D) {
    const tiles = [];

    // 1. Kitchen temp
    const temps = D.temps || [];
    let kitchenSensor = temps.find(s => s.label === "Kitchen" || s.name === "Kitchen");
    if (!kitchenSensor && temps.length > 0) {
      kitchenSensor = temps[0]; // fallback to first sensor
    }
    tiles.push(buildQATile(
      "🌡",
      kitchenSensor ? (kitchenSensor.label || kitchenSensor.name) : "Indoor Temp",
      (() => { const v = parseFloat(kitchenSensor?.current); return isNaN(v) ? "—" : `${v.toFixed(1)}°C`; })(),
      null,
      false
    ));

    // 2. Dad's lamp toggle
    const dadLight = (D.rooms || []).flatMap(r => r.lights).find(l => l.id === "dad-lamp");
    tiles.push(buildQATile(
      dadLight?.on ? "💡" : "🔦",
      "Dad's Lamp",
      dadLight ? (dadLight.on ? "On" : "Off") : "—",
      dadLight
        ? async (tile) => {
            try {
              const next = !dadLight.on;
              await window.api(`/api/lights/dad-lamp`, { method: "POST", body: { on: next } });
              dadLight.on = next;
              // Re-render quick actions
              const bar = document.getElementById("quick-actions-bar");
              if (bar) bar.replaceWith(buildQuickActions(D));
            } catch (e) {
              if (window.flashBernie) window.flashBernie("Couldn't toggle Dad's lamp.");
            }
          }
        : null,
      dadLight?.on
    ));

    // 3. Camera previews (tap lightbox; full grid on Security)
    const cams = D.cameras || {};
    const camEntries = Object.entries(cams);
    if (camEntries.length) {
      for (const [camId, label] of camEntries) {
        tiles.push(buildCamTile(camId, typeof label === "string" ? label : camId));
      }
    } else {
      tiles.push(buildQATile(
        "📷",
        "Cameras",
        "Open Security",
        () => goPanel("security"),
        false
      ));
    }

    // 4. Garbage / recycling
    const g = D.garbage;
    tiles.push(buildQATile(
      g?.icon || "🗑",
      g?.summary || "Garbage",
      g?.date_label || "—",
      null,
      false
    ));

    return buildEl("div", {
      id: "quick-actions-bar",
      class: "card today-actions",
      style: "padding: 0;"
    }, ...tiles);
  }

  function buildQATile(icon, label, value, onClick, isActive) {
    const tile = buildEl("div", {
      style: `display: flex; flex-direction: column; align-items: center; gap: 5px; padding: 16px 12px; cursor: ${onClick ? "pointer" : "default"}; border-right: 1px solid var(--stroke); user-select: none; transition: background .15s; ${isActive ? "background: color-mix(in oklab, var(--amber) 6%, var(--bg-card));" : ""}`,
    },
      buildEl("div", { style: "font-size: 20px; line-height: 1;" }, icon),
      buildEl("div", { class: "t-eyebrow", style: "font-size: 9px;" }, label),
      buildEl("div", { class: "t-mono", style: "font-size: 11px; color: var(--ink-2);" }, value)
    );

    if (onClick) {
      tile.style.cursor = "pointer";
      tile.addEventListener("mouseenter", () => tile.style.background = "var(--bg-card-2)");
      tile.addEventListener("mouseleave", () => tile.style.background = isActive ? "color-mix(in oklab, var(--amber) 6%, var(--bg-card))" : "");
      tile.addEventListener("click", () => onClick(tile));
    }

    return tile;
  }

  function buildCamTile(cameraId, label) {
    const shortLabel = String(label).replace(/\s*\(cam\s*\d+\)/i, "").trim() || label;
    const tile = buildEl("div", {
      class: "today-cam-tile",
      style: "display: flex; flex-direction: column; justify-content: flex-end; cursor: pointer; border-right: 1px solid var(--stroke); position: relative; overflow: hidden; min-height: 108px;",
    });

    const thumb = buildEl("img", {
      alt: shortLabel,
      style: "position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity .35s;",
    });
    const shade = buildEl("div", {
      style: "position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,.62) 0%, rgba(0,0,0,.08) 55%, transparent 100%); pointer-events: none;",
    });
    const lbl = buildEl("div", {
      class: "t-eyebrow",
      style: "position: relative; z-index: 1; padding: 0 10px; color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,.5);",
    }, shortLabel);
    const sub = buildEl("div", {
      class: "t-mono",
      style: "position: relative; z-index: 1; padding: 2px 10px 10px; font-size: 10px; color: rgba(255,255,255,.8);",
    }, "tap to enlarge");

    tile.append(thumb, shade, lbl, sub);

    fetchBlobUrl(`/api/cameras/${cameraId}/snapshot`).then(url => {
      _activeBlobUrls.push(url);
      thumb.src = url;
      thumb.onload = () => { thumb.style.opacity = "1"; };
      sub.textContent = "tap to enlarge";
    }).catch(e => {
      console.warn(`[Bernie] Camera thumbnail failed (${cameraId}):`, e.message);
      sub.textContent = "unavailable";
    });

    tile.addEventListener("click", () => openCameraLightbox(cameraId, label));

    return tile;
  }

  // ── Ask Bernie ───────────────────────────────────────────────────────────────

  function buildAskBernie() {
    const doAsk = async (input, replyEl, sendBtn) => {
      const text = input.value.trim();
      if (!text) return;
      sendBtn.disabled = true;
      input.disabled = true;

      replyEl.innerHTML = "";
      replyEl.style.display = "";
      const statusText = buildEl("div", { style: "font-style: italic; color: var(--ink-3); font-size: 14px" }, "thinking...");
      replyEl.append(statusText);

      try {
        const res = await window.api("/api/ask", { method: "POST", body: { question: text } });
        replyEl.innerHTML = "";

        const card = buildEl("div", { class: "card card-pad page-fade", style: "position: relative; margin-top: 10px; border-color: var(--info); background: color-mix(in oklab, var(--info) 4%, var(--bg-card))" },
          buildEl("button", {
            class: "btn btn-icon btn-ghost",
            style: "position: absolute; top: 8px; right: 8px; width: 24px; height: 24px; color: var(--ink-3)",
            onclick: () => { replyEl.style.display = "none"; replyEl.innerHTML = ""; },
            html: "✕"
          }),
          buildEl("div", { class: "card-tag", style: "margin-bottom: 12px" },
            buildEl("span", { class: "pip info" }),
            buildEl("span", { style: "color: var(--info)" }, "You asked · "),
            buildEl("span", { style: "text-transform: none; font-style: italic; opacity: 0.8" }, `"${text}"`)
          ),
          buildEl("div", { 
            class: "t-body", 
            style: "line-height: 1.6; color: var(--ink); padding-left: 2px; font-family: var(--font-serif); font-size: 16px;", 
            html: window.renderMarkdown(res?.answer ?? "No response.") 
          }),
          buildEl("div", { class: "row between", style: "margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--stroke)" },
            buildEl("div", { class: "t-mono", style: "color: var(--ink-4); font-size: 10px" },
              `via ${res?.model || "unknown"} · ${res?.latency_ms ? (res.latency_ms / 1000).toFixed(1) + "s" : "just now"}`
            ),
            buildEl("div", { class: "t-mono", style: "color: var(--ink-4); font-size: 10px" }, "Bernie")
          )
        );
        replyEl.append(card);
        window.hydrateAuthImages?.(card);
      } catch (err) {
        replyEl.innerHTML = "";
        replyEl.append(buildEl("div", { style: "color: var(--err); font-size: 14px" }, "Could not reach Bernie."));
      } finally {
        sendBtn.disabled = false;
        input.disabled = false;
        input.value = "";
        input.focus();
      }
    };

    const askInput = buildEl("input", {
      class: "input",
      style: "flex: 1; height: 38px;",
      placeholder: "do I need a jacket? · what's on tonight? · turn off the lamp",
    });
    const askSend  = buildEl("button", { class: "btn btn-primary btn-icon", style: "width: 38px; height: 38px; border-radius: 999px;", html: "→" });
    const askReply = buildEl("div", { style: "display: none; margin-top: 10px;" });

    askInput.addEventListener("keydown", e => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); doAsk(askInput, askReply, askSend); } });
    askSend.addEventListener("click", () => doAsk(askInput, askReply, askSend));

    const chips = ["jacket?", "tonight?", "garbage day?", "who's home?"].map(s =>
      buildEl("button", { class: "btn", onclick: () => { askInput.value = s; doAsk(askInput, askReply, askSend); } }, s)
    );

    return [
      buildEl("div", { class: "card card-pad", style: "display: flex; flex-direction: column; gap: 12px;" },
        buildEl("div", { class: "row between" },
          buildEl("div", { class: "card-tag" }, buildEl("span", { class: "pip" }), "Ask Bernie"),
          buildEl("div", { class: "t-mono", style: "color: var(--ink-4)" }, "⌘K")
        ),
        buildEl("div", { class: "row gap-2", style: "align-items: center;" }, askInput, askSend),
        askReply,
        buildEl("div", { class: "row gap-2", style: "flex-wrap: wrap;" }, ...chips)
      )
    ];
  }

  // ── Main render ──────────────────────────────────────────────────────────────

  window.renderToday = function renderToday() {
    const D = window.BernieData || {};
    const main = document.querySelector("#panel-today");
    if (!main) return;

    if (!D.weather) {
      return;
    }
    
    // Revoke any blob URLs from previous render before clearing the DOM
    // Revoke any blob URLs from previous render before clearing the DOM
    while (_activeBlobUrls.length) URL.revokeObjectURL(_activeBlobUrls.pop());

    let headerWrap = main.querySelector("#today-header-wrap");
    let askWrap    = main.querySelector("#today-ask-wrap");
    let gridWrap   = main.querySelector("#today-grid-wrap");
    let quickWrap  = main.querySelector("#today-quick-wrap");

    if (!headerWrap) {
      main.innerHTML = "";
      headerWrap = buildEl("div", { id: "today-header-wrap" });
      askWrap    = buildEl("div", { id: "today-ask-wrap" });
      gridWrap   = buildEl("div", { id: "today-grid-wrap" });
      quickWrap  = buildEl("div", { id: "today-quick-wrap" });
      main.append(headerWrap, askWrap, gridWrap, quickWrap);
      buildAskBernie().forEach(n => askWrap.append(n));
    }

    const w = D.weather;
    const d = new Date();
    const dateStr = d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }).replace(",", "");
    const timeStr = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }).toLowerCase();

    let outStr = "";
    if (D.presence) {
      const awayList = D.presence.filter(p => !p.home);
      if (awayList.length === 0) {
        outStr = "Everyone is home.";
      } else {
        const descriptions = awayList.map(p => {
          const zone = p.status_label && p.status_label !== "Away" ? `at ${p.status_label}` : "away";
          return `${p.name} is ${zone}`;
        });
        outStr = LIST_FMT.format(descriptions) + ".";
      }
    }

    // Header
    headerWrap.innerHTML = "";
    headerWrap.append(
      buildEl("div", { class: "row between", style: "align-items: flex-start; gap: 24px;" },
        buildEl("div", { style: "flex: 1;" },
          buildEl("div", { class: "t-eyebrow", style: "margin-bottom: 14px;" }, dateStr.toUpperCase()),
          buildEl("div", { class: "t-display" }, `${getGreeting()} ${D.user?.name || "Dad"}.`),
          outStr
            ? buildEl("div", { class: "t-body-lg t-muted", style: "margin-top: 8px; font-style: italic;" }, outStr)
            : null
        ),
        buildEl("div", { class: "col", style: "align-items: flex-end; gap: 4px; padding-top: 6px;" },
          buildEl("div", { style: "font-family: var(--font-serif); font-size: 28px; line-height: 1; letter-spacing: -0.01em;" },
            timeStr.split(" ")[0],
            buildEl("span", { class: "t-muted", style: "font-size: 18px;" }, " " + timeStr.split(" ")[1])
          ),
          buildEl("div", { class: "t-mono" }, `SUNSET ${w.sunset || "—"}`)
        )
      )
    );

    // 3-column grid
    gridWrap.innerHTML = "";
    gridWrap.append(
      buildEl("div", { class: "today-3col" },
        buildWeatherCard(w),
        buildEl("div", { style: "display: flex; flex-direction: column; gap: 14px;" },
          buildBernieSaysCard(D),
          buildFamilyCard(D)
        ),
        buildTodayCard(D)
      )
    );

    // Quick actions bar
    quickWrap.innerHTML = "";
    quickWrap.append(buildQuickActions(D));
    window.hydrateAuthImages?.(main);
  };
})();
