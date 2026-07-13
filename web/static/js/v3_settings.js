/**
 * Bernie v3 Settings Screen
 * Standalone implementation for Phase 4.
 * Updated for v3.11 with Surgical Rendering.
 */

(function() {
  const el = (...args) => window.el(...args);

  const api = window.api;

  let _entities = [];
  let _filter = "all";
  let _search = "";
  let _isFetching = false;
  let _lastEntityFetch = 0;
  let _isSaving = false;

  function updateEntityList() {
    const listContainer = document.querySelector("#settings-entity-list");
    if (!listContainer) return;

    const filtered = _entities.filter(e => {
      const s = _search.toLowerCase();
      const matchesSearch = e.entity_id.toLowerCase().includes(s) || (e.name || "").toLowerCase().includes(s);
      if (_filter === "all") return matchesSearch;
      return matchesSearch && e.domain === _filter;
    });

    listContainer.innerHTML = "";
    if (filtered.length > 0) {
      filtered.forEach((e, i) => {
         const row = el("div", {
           class: "row gap-3",
           style: `padding: 8px 12px; border-bottom: ${i < filtered.length - 1 ? '1px solid var(--stroke)' : 'none'}; align-items: center ${e.domain === 'light' ? '; cursor: pointer' : ''}`,
           onclick: e.domain === 'light' ? (ev) => openLightPopover(e.entity_id, ev.currentTarget) : null
         },
           el("span", {
             class: "t-mono",
             style: `width: 56px; font-size: 10px; padding: 2px 6px; background: var(--bg-pill); border-radius: 4px; text-align: center; color: ${e.domain === 'light' ? 'var(--amber)' : 'var(--info)'}`
           }, (e.domain || "??").toUpperCase()),
           el("span", { class: "t-mono", style: "flex: 1; font-size: 12px; color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis" }, e.entity_id),
           el("span", { class: "t-meta", style: "color: var(--ink-3)" }, e.name || "")
         );
         listContainer.append(row);
      });
    } else {
      listContainer.append(el("div", { style: "padding: 20px; text-align: center; color: var(--ink-4)" }, "No entities found"));
    }

    // Update count and refresh string if they exist
    const countEl = document.querySelector("#settings-entity-count");
    if (countEl) countEl.textContent = `${_entities.length} entities · live`;
    
    const refreshEl = document.querySelector("#settings-entity-refreshed");
    if (refreshEl) {
      refreshEl.textContent = _lastEntityFetch > 0 
        ? `refreshed ${Math.floor((Date.now() - _lastEntityFetch)/60000)}m ago` 
        : "loading...";
    }
  }

  async function fetchEntities() {
    if (_isFetching) return;
    _isFetching = true;
    
    const btn = document.querySelector("#settings-entity-refresh-btn");
    if (btn) btn.classList.add("loading");

    try {
      const data = await api("/api/ha/entities");
      if (data) {
        _entities = data;
        _lastEntityFetch = Date.now();
      }
    } catch (e) {
      console.error("Failed to fetch entities:", e);
    } finally {
      _isFetching = false;
      if (btn) btn.classList.remove("loading");
      updateEntityList();
    }
  }

  const openLightPopover = async (entityId, targetEl) => {
    const existing = document.querySelector('.light-popover');
    if (existing) existing.remove();

    const id = entityId.split('.')[1];
    const lights = (window.BernieData?.rooms || []).flatMap(r => r.lights || []);
    const l = lights.find(l => l.id === id.replace(/_/g, '-'));
    if (!l) return;

    const pop = el("div", {
      class: "light-popover",
      style: `position: fixed; z-index: 9999; background: var(--bg-card); border: 1px solid var(--stroke); border-radius: var(--r-lg); padding: 16px; min-width: 240px; box-shadow: var(--shadow-lg);`,
      onclick: (e) => e.stopPropagation()
    });

    let curOn = l.on;
    let curBrightness = l.brightness != null ? l.brightness : 255;
    let curColorTemp = l.color_temp != null ? l.color_temp : 4000;
    let curRgb = l.rgb_color || [255, 255, 255];
    let debounceTimer = null;

    const send = (attrs = {}) => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(async () => {
        try {
          await api(`/api/lights/${l.id}`, { method: "POST", body: { on: curOn, ...attrs } });
          Object.assign(l, { on: curOn, ...attrs });
        } catch (e) {
          if (window.flashBernie) window.flashBernie('Light update failed');
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

    if (targetEl) {
      const rect = targetEl.getBoundingClientRect();
      const popRect = pop.getBoundingClientRect();
      let top = rect.bottom + 8;
      let left = rect.left;
      if (top + popRect.height > window.innerHeight) top = rect.top - popRect.height - 8;
      if (left + popRect.width > window.innerWidth) left = window.innerWidth - popRect.width - 16;
      pop.style.top = `${top}px`;
      pop.style.left = `${left}px`;
    }

    const close = () => { if (pop.parentNode) pop.remove(); document.removeEventListener('mousedown', check); };
    const check = (e) => { if (!pop.contains(e.target) && !targetEl.contains(e.target)) close(); };
    setTimeout(() => document.addEventListener('mousedown', check), 0);
  };

  async function renderSettings() {
    const root = document.querySelector("#panel-settings");
    if (!root) return;

    const D = window.BernieData || {};
    const config = D.settings || { members: [], channels: [], summarySchedule: "07:00 · #smithy" };

    if (root.querySelector("#settings-grid")) {
      updateEntityList();
      updateModelsSection();
      return;
    }

    const draw = () => {
      root.innerHTML = "";
      root.className = "page page-fade";
      root.style.maxWidth = "960px";

      // Header
      root.append(el("div", { style: "margin-bottom: 32px" },
        el("div", { class: "t-eyebrow" }, "System Settings"),
        el("div", { class: "t-h1", style: "font-size: 32px; font-family: var(--font-serif); font-weight: 400; margin-top: 4px" }, "Configuration")
      ));

      // Grid Container
      const grid = el("div", { 
        id: "settings-grid",
        style: "display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 20px; align-items: start;" 
      });
      
      const col1 = el("div", { class: "col", style: "gap: 20px" },
        buildFamilySection(config.members),
        buildChannelsSection(config.channels),
        buildModelsCard()
      );

      const col2 = el("div", { class: "col", style: "gap: 20px" },
        buildSummarySection(config),
        buildEntitiesSection(),
        buildSystemSection()
      );
      
      grid.append(col1, col2);
      root.append(grid);
    };

    function buildModelsCard() {
      const wrap = el("div", { id: "settings-models-wrap" });
      _renderModelsInto(wrap);
      return el("div", { class: "card card-pad" }, wrap);
    }

    function _renderModelsInto(wrap) {
      const models = (window.BernieData || {}).models;
      wrap.innerHTML = "";
      if (!models) {
        wrap.append(
          el("div", { class: "t-h2", style: "margin-bottom: 8px" }, "Model"),
          el("div", { class: "t-meta", style: "color: var(--ink-4)" }, "Loading…")
        );
        return;
      }

      const anthropicOnly = (models.models || []).filter(m => m.source === "anthropic");
      const litellmOnly = (models.models || []).filter(m => m.source === "litellm");

      const allModels = [...(models.models || []), ...(models.ollama_models || [])];

      const activeFor = key => ({
        webui: models.webui_model, discord: models.current,
        openwebui: models.openwebui_model, fallback: models.fallback_model,
        digest: models.digest_model, shadow: models.shadow_model,
        worker: models.worker_model, research: models.research_model,
        research_upgrade: models.research_upgrade_model,
        study_guide: models.study_guide_model,
        audit: models.audit_model,
        eval: models.eval_model,
        judge_fallback: models.judge_fallback_model,
        judge_ollama: models.judge_ollama_fallback,
        vision: models.vision_model,
        primary_reliable: models.primary_reliable_model,
        reflection: models.reflection_model,
        consolidation: models.consolidation_model,
      })[key];

      const setActive = (key, val) => {
        if (key === "webui") models.webui_model = val;
        else if (key === "discord") models.current = val;
        else if (key === "openwebui") models.openwebui_model = val;
        else if (key === "fallback") models.fallback_model = val;
        else if (key === "digest") models.digest_model = val;
        else if (key === "shadow") models.shadow_model = val;
        else if (key === "worker") models.worker_model = val;
        else if (key === "research") models.research_model = val;
        else if (key === "research_upgrade") models.research_upgrade_model = val;
        else if (key === "study_guide") models.study_guide_model = val;
        else if (key === "audit") models.audit_model = val;
        else if (key === "eval") models.eval_model = val;
        else if (key === "judge_fallback") models.judge_fallback_model = val;
        else if (key === "judge_ollama") models.judge_ollama_fallback = val;
        else if (key === "vision") models.vision_model = val;
        else if (key === "primary_reliable") models.primary_reliable_model = val;
        else if (key === "reflection") models.reflection_model = val;
        else if (key === "consolidation") models.consolidation_model = val;
      };

      const targets = [
        { key: "discord",   label: "Discord",             pool: models.models     },
        { key: "webui",     label: "Web UI / Ask Bernie", pool: models.models     },
        { key: "openwebui", label: "OpenWebUI",           pool: models.models     },
        { key: "digest",    label: "Nightly digest",      pool: anthropicOnly     },
        { key: "fallback",  label: "Ollama fallback",     pool: models.ollama_models },
        { key: "shadow",    label: "Shadow eval",         pool: allModels         },
        { key: "worker",    label: "Background worker",   pool: allModels         },
        { key: "research",  label: "Research worker",     pool: allModels         },
        { key: "research_upgrade", label: "Research upgrade (escalation)", pool: allModels },
        { key: "study_guide", label: "Study guide worker", pool: allModels        },
        { key: "audit",    label: "Watchman audit",       pool: allModels         },
        { key: "eval",     label: "Nightly judge (eval)", pool: allModels         },
        { key: "judge_fallback", label: "Judge LiteLLM fallback", pool: litellmOnly },
        { key: "judge_ollama", label: "Judge Ollama fallback", pool: models.ollama_models },
        { key: "vision",   label: "Vision",               pool: models.ollama_models },
        { key: "primary_reliable", label: "Primary reliable (native escalation)", pool: models.models },
        { key: "reflection", label: "Reflection worker",  pool: allModels         },
        { key: "consolidation", label: "Consolidation worker", pool: allModels    },
      ];

      wrap.append(
        el("div", { class: "t-h2", style: "margin-bottom: 4px" }, "Model"),
        el("div", { class: "t-meta", style: "color: var(--ink-4); margin-bottom: 16px" }, "Each surface uses an independent model."),
        ...targets.map(({ key, label, pool }) => {
          const active = activeFor(key);
          const sel = el("select", {
            class: "input",
            style: "width: 100%; margin-top: 6px; font-size: 13px; font-family: var(--font-mono)",
          });
          (pool || []).forEach(m => {
            const suffix = m.source === "litellm" ? " (LiteLLM)" : m.source === "ollama" ? " (Ollama)" : "";
            const opt = el("option", { value: m.id }, m.id + suffix);
            if (m.id === active) opt.selected = true;
            sel.appendChild(opt);
          });
          if (!active) {
            const placeholder = el("option", { value: "", disabled: true, selected: true }, "— not set —");
            sel.insertBefore(placeholder, sel.firstChild);
          }
          sel.addEventListener("change", async () => {
            const model_id = sel.value;
            if (!model_id) return;
            try {
              await window.api("/api/config/models", { method: "PATCH", body: { model: model_id, target: key } });
              setActive(key, model_id);
              if (window.flashBernie) window.flashBernie("Model switched.");
            } catch (e) {
              if (window.flashBernie) window.flashBernie("Model switch failed.");
              sel.value = activeFor(key) || "";
            }
          });
          return el("div", { style: "margin-bottom: 14px" },
            el("div", { class: "t-eyebrow" }, label),
            sel
          );
        })
      );
    }

    function updateModelsSection() {
      const wrap = document.querySelector("#settings-models-wrap");
      if (wrap) _renderModelsInto(wrap);
    }

    const PERSON_ACCENTS = {
      dad:    'oklch(0.72 0.14 50)',
      dad: 'oklch(0.72 0.14 50)',
      mom:    'oklch(0.72 0.13 200)',
      mom: 'oklch(0.72 0.13 200)',
      child1:  'oklch(0.7 0.16 320)',
      child2:  'oklch(0.72 0.13 140)',
    };

    function buildFamilySection(members) {
      return el("div", { class: "card card-pad", style: "margin-bottom: 20px" },
        el("div", { class: "t-h2", style: "margin-bottom: 4px" }, "Family (read-only)"),
        el("p", { class: "sub", style: "margin: 0 0 16px" }, "Edit members in People"),
        el("div", { class: "col", style: "gap: 8px" },
          members.map(m => {
            const accent = PERSON_ACCENTS[m.name.toLowerCase()] || 'oklch(0.72 0.14 50)';
            return el("div", {
            class: "row gap-3",
            style: "padding: 12px; background: var(--bg-sub); border-radius: var(--r-md); align-items: center"
          },
            el("div", {
              class: "pip",
              style: `width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center; font-weight: 600; font-size: 14px; background: color-mix(in oklch, ${accent} 18%, var(--bg-card)); border: 1px solid color-mix(in oklch, ${accent} 40%, transparent); color: ${accent}`
            }, m.name[0]),
            el("div", { style: "flex: 1" },
              el("div", { style: "font-weight: 500; font-size: 14px" }, m.name),
              el("div", { class: "t-mono", style: "font-size: 10px; color: var(--ink-4); margin-top: 2px" }, m.mac || "No MAC configured")
            ),
            el("div", { class: "t-meta", style: "color: var(--ink-3); text-align: right" },
              el("div", { style: "font-size: 11px" }, "Reminders"),
              el("div", { style: "font-size: 10px; color: var(--ink-4)" }, m.reminders_enabled ? `Every ${m.reminder_minutes}m` : "Off")
            )
          );
          })
        )
      );
    }

    function buildSummarySection(cfg) {
      const saveBtn = el("button", { 
        class: "btn btn-primary", 
        onclick: async () => {
          const h = parseInt(document.querySelector("#sum-hour").value);
          const m = parseInt(document.querySelector("#sum-min").value);
          saveBtn.disabled = true;
          saveBtn.textContent = "Saving...";
          try {
            await api("/api/settings", { 
              method: "PUT", 
              body: { schedule: { summary_hour: h, summary_minute: m } } 
            });
            if (window.flashBernie) window.flashBernie("Summary schedule updated.");
          } catch (e) {
            alert("Failed to save: " + e.message);
          } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = "Save";
          }
        }
      }, "Save");

      return el("div", { class: "card card-pad", style: "margin-bottom: 20px" },
        el("div", { class: "row between", style: "margin-bottom: 4px" },
          el("div", { class: "t-h2" }, "Morning summary"),
          saveBtn
        ),
        el("div", { class: "t-meta t-muted", style: "margin-bottom: 16px" }, `Currently ${cfg.summarySchedule}`),
        el("div", { class: "row gap-3" },
          el("div", { class: "col", style: "flex: 1" },
            el("label", { class: "t-eyebrow", style: "margin-bottom: 6px" }, "Hour (0-23)"),
            el("input", { 
              id: "sum-hour", 
              type: "number", 
              class: "input", 
              value: cfg.summaryHour,
              min: 0, max: 23
            })
          ),
          el("div", { class: "col", style: "flex: 1" },
            el("label", { class: "t-eyebrow", style: "margin-bottom: 6px" }, "Minute (0-59)"),
            el("input", { 
              id: "sum-min", 
              type: "number", 
              class: "input", 
              value: cfg.summaryMinute,
              min: 0, max: 59
            })
          )
        )
      );
    }

    function buildChannelsSection(channels) {
      return el("div", { class: "card card-pad", style: "margin-bottom: 20px" },
        el("div", { class: "t-h2", style: "margin-bottom: 16px" }, "Notification channels"),
        el("div", { class: "col", style: "gap: 12px" },
          channels.map(c => el("div", { 
            class: "row gap-3",
            style: "align-items: center"
          },
            el("div", { 
              class: "pip", 
              style: "width: 36px; height: 36px; border-radius: 8px; background: var(--bg-sub); display: grid; place-items: center; font-family: var(--font-mono); font-size: 11px; font-weight: 600; color: var(--ink-3)" 
            }, c.ico),
            el("div", { style: "flex: 1" },
              el("div", { style: "font-weight: 500; font-size: 14px" }, c.name),
              el("div", { class: "t-meta", style: "font-size: 11px; color: var(--ink-4)" }, c.meta)
            ),
            el("div", { class: `chip ${c.state}` }, c.label)
          ))
        )
      );
    }

    function buildEntitiesSection() {
      if (_entities.length === 0 && !_isFetching && (Date.now() - _lastEntityFetch > 300000)) {
        fetchEntities();
      }

      const listContainer = el("div", { 
        id: "settings-entity-list",
        class: "col", 
        style: "max-height: 400px; overflow-y: auto; border: 1px solid var(--stroke); border-radius: var(--r-md); background: var(--bg-sub)" 
      });
      
      const refreshedStr = _lastEntityFetch > 0 
        ? `refreshed ${Math.floor((Date.now() - _lastEntityFetch)/60000)}m ago` 
        : "loading...";

      const section = el("div", { class: "card card-pad" },
        el("div", { class: "row between", style: "align-items: flex-start" },
          el("div", {},
            el("div", { class: "t-h2" }, "Home Assistant entities"),
            el("div", { id: "settings-entity-count", class: "t-meta t-muted", style: "margin-top: 4px" }, `${_entities.length} entities · live`)
          ),
          el("div", { class: "col", style: "align-items: flex-end" },
            el("div", { class: "row gap-2" },
              el("button", { 
                id: "settings-entity-refresh-btn",
                class: `btn ${_isFetching ? 'loading' : ''}`, 
                onclick: () => fetchEntities() 
              }, "Refresh"),
              el("select", { 
                class: "input", 
                style: "padding: 4px 8px; font-size: 12px",
                onchange: (e) => { _filter = e.target.value; updateEntityList(); }
              },
                ["all", "light", "sensor", "media_player", "automation"].map(d => el("option", { 
                  value: d, 
                  selected: _filter === d 
                }, d.toUpperCase()))
              )
            ),
            el("div", { id: "settings-entity-refreshed", class: "t-mono", style: "font-size: 10px; color: var(--ink-4); margin-top: 6px" }, refreshedStr)
          )
        ),
        el("input", {
          id: "settings-entity-search",
          class: "input",
          style: "width: 100%; margin: 16px 0; background: var(--bg-sub)",
          placeholder: "Search entities...",
          value: _search,
          oninput: (e) => { _search = e.target.value; updateEntityList(); }
        }),
        listContainer
      );

      // We need to wait for listContainer to be in DOM to update it
      setTimeout(updateEntityList, 0);

      return section;
    }

    function buildSystemSection() {
      return el("div", { class: "col", style: "margin-top: 32px; gap: 12px" },
        el("div", { class: "t-eyebrow" }, "Maintenance"),
        el("div", { class: "row gap-2" },
          el("button", { 
            class: "btn",
            style: "color: var(--err)",
            onclick: async () => {
              if (confirm("Restart the bot container?")) {
                await api("/api/bot/restart", { method: "POST" });
                if (window.flashBernie) window.flashBernie("Restart command sent.");
              }
            }
          }, "Restart bot"),
          el("button", { 
            class: "btn",
            onclick: async () => {
              await api("/api/config/reload", { method: "POST" });
              if (window.flashBernie) window.flashBernie("Config reloaded.");
            }
          }, "Reload config")
        )
      );
    }

    draw();
  }

  // Export to window
  window.renderSettings = renderSettings;

})();
