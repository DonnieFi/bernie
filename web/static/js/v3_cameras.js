// v3_cameras.js

(function () {
  const { $, $$, el, api } = window;

  let state = {
    config: { mode: "on", cameras_enabled: {} },
    events: [],
    cameras: {}, // Keyed by cam_id, val: label
    imageUrls: {}, // object URLs to release later
    refreshInterval: null,
    currentEventIndex: -1,
    modalKeyHandler: null,
    liveModalKeyHandler: null
  };

  async function fetchImageBlob(url) {
    const t = localStorage.getItem("bernie-token") || "";
    try {
      const res = await fetch(url, { headers: { "X-Bernie-Token": t } });
      if (!res.ok) return null;
      const blob = await res.blob();
      return URL.createObjectURL(blob);
    } catch {
      return null;
    }
  }

  function releaseObjectURLs() {
    Object.values(state.imageUrls).forEach(url => URL.revokeObjectURL(url));
    state.imageUrls = {};
  }

  async function loadData() {
    try {
      const [cfg, evts] = await Promise.all([
        api("/api/cameras/config"),
        api("/api/cameras/events")
      ]);
      if (cfg && !cfg.error) state.config = cfg;
      if (evts && !evts.error) state.events = evts;
      
      // Use cameras from D.cameras or config.cameras
      state.cameras = window.BernieData?.cameras || state.config.cameras || {};
    } catch (e) {
      console.error("Failed to load cameras data", e);
    }
  }

  async function setMode(mode) {
    try {
      const res = await api("/api/cameras/mode", { method: "POST", body: { mode } });
      if (res.status === "ok") {
        state.config.mode = mode;
        renderHeader();
      }
    } catch (e) {
      console.error("Failed to set mode", e);
    }
  }

  async function toggleCamera(camId, enabled) {
    try {
      const res = await api(`/api/cameras/${camId}/enable`, { method: "POST", body: { enabled } });
      if (res.status === "ok") {
        if (!state.config.cameras_enabled) state.config.cameras_enabled = {};
        state.config.cameras_enabled[camId] = enabled;
        // Don't need a full re-render, switch state handles itself
      }
    } catch (e) {
      console.error(`Failed to toggle camera ${camId}`, e);
      renderGrid(); // revert on failure
    }
  }

  function formatEventTime(timestamp) {
    const d = new Date(timestamp * 1000);
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) + ' · ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function renderHeader() {
    const header = $("#cameras-header");
    if (!header) return;
    header.innerHTML = "";
    
    const mode = state.config.mode || "on";
    
    const selector = el("div", { class: "mode-selector" },
      el("div", { 
        class: `mode-chip ${mode === 'on' ? 'active' : ''}`, 
        onclick: () => setMode('on') 
      }, "On"),
      el("div", { 
        class: `mode-chip ${mode === 'off' ? 'active' : ''}`, 
        onclick: () => setMode('off') 
      }, "Off"),
      el("div", { 
        class: `mode-chip ${mode === 'test' ? 'active' : ''}`, 
        onclick: () => setMode('test') 
      }, "Test")
    );

    header.append(
      el("h2", {}, "Cameras"),
      selector
    );
  }

  function refreshIconSvg() {
    return el("svg", {
      width: "16",
      height: "16",
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
      html: '<path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.8 1 6.5 2.5L21 8"/><path d="M21 3v5h-5"/>',
    });
  }

  function openLiveCameraModal(camId, label) {
    const modal = $("#live-camera-modal");
    if (!modal) return;

    const imgEl = modal.querySelector(".snapshot-modal-body img");
    const titleEl = modal.querySelector(".snapshot-modal-header h3");
    const counterEl = modal.querySelector(".snapshot-modal-counter");
    if (titleEl) titleEl.textContent = label;
    if (counterEl) counterEl.textContent = "Live snapshot";

    const loadSnap = async (forceRefresh = false) => {
      if (!imgEl) return;
      imgEl.style.opacity = "0.35";
      const oldUrl = state.imageUrls["live_modal"];
      if (oldUrl) URL.revokeObjectURL(oldUrl);
      const url = await fetchImageBlob(
        `/api/cameras/${camId}/snapshot?refresh=${forceRefresh}&_t=${Date.now()}`
      );
      if (url) {
        state.imageUrls["live_modal"] = url;
        imgEl.src = url;
        imgEl.style.opacity = "1";
      } else {
        imgEl.alt = "Failed to load snapshot";
        imgEl.style.opacity = "1";
      }
    };

    modal._loadSnap = loadSnap;
    modal.classList.add("open");
    loadSnap(false);

    if (state.liveModalKeyHandler) {
      document.removeEventListener("keydown", state.liveModalKeyHandler);
    }
    state.liveModalKeyHandler = (e) => {
      if (e.key === "Escape") closeLiveCameraModal();
    };
    document.addEventListener("keydown", state.liveModalKeyHandler);
  }

  function closeLiveCameraModal() {
    const modal = $("#live-camera-modal");
    if (modal) modal.classList.remove("open");
    if (state.liveModalKeyHandler) {
      document.removeEventListener("keydown", state.liveModalKeyHandler);
      state.liveModalKeyHandler = null;
    }
    if (state.imageUrls["live_modal"]) {
      URL.revokeObjectURL(state.imageUrls["live_modal"]);
      delete state.imageUrls["live_modal"];
    }
  }

  async function refreshCameraImage(camId, imgEl) {
    imgEl.style.opacity = "0.5";
    const oldUrl = state.imageUrls[camId];
    if (oldUrl) URL.revokeObjectURL(oldUrl);
    
    const url = await fetchImageBlob(`/api/cameras/${camId}/snapshot?refresh=true`);
    if (url) {
      state.imageUrls[camId] = url;
      imgEl.src = url;
    }
    imgEl.style.opacity = "1";
  }

  async function renderGrid() {
    const grid = $("#cameras-grid");
    if (!grid) return;
    grid.innerHTML = "";

    const camIds = Object.keys(state.cameras);
    if (camIds.length === 0) {
      grid.append(el("div", { style: "color: var(--ink-2); font-style: italic;" }, "No cameras configured."));
      return;
    }

    for (const camId of camIds) {
      const label = state.cameras[camId];
      const isEnabled = state.config.cameras_enabled?.[camId] !== false;

      const imgEl = el("img", { alt: label });
      imgEl.addEventListener("click", () => openLiveCameraModal(camId, label));

      const snapshotUrl = state.imageUrls[camId] || await fetchImageBlob(`/api/cameras/${camId}/snapshot`);
      if (snapshotUrl) {
        state.imageUrls[camId] = snapshotUrl;
        imgEl.src = snapshotUrl;
      }

      const refreshBtn = el("button", {
        class: "refresh-btn",
        type: "button",
        title: "Refresh snapshot",
        "aria-label": "Refresh snapshot",
        onclick: (e) => {
          e.stopPropagation();
          refreshCameraImage(camId, imgEl);
        },
      }, refreshIconSvg());

      const checkbox = el("input", { type: "checkbox", onchange: (e) => toggleCamera(camId, e.target.checked) });
      if (isEnabled) checkbox.checked = true;

      const card = el("div", { class: "camera-card" },
        el("div", { class: "camera-snapshot" }, imgEl, refreshBtn),
        el("div", { class: "camera-info" },
          el("div", { class: "camera-name" }, label),
          el("label", { class: "switch" },
            checkbox,
            el("span", { class: "slider" })
          )
        )
      );
      grid.append(card);
    }
  }

  function updateModalContent(index) {
    const modal = $("#snapshot-modal");
    if (!modal || index < 0 || index >= state.events.length) return;

    state.currentEventIndex = index;
    const event = state.events[index];
    const imgEl = modal.querySelector(".snapshot-modal-body img");
    const titleEl = modal.querySelector(".snapshot-modal-header h3");
    const counterEl = modal.querySelector(".snapshot-modal-counter");

    imgEl.style.opacity = "0.3";
    imgEl.src = ""; // clear previous

    if (titleEl) {
      titleEl.replaceChildren(
        el("span", { class: "event-badge" }, event.label),
        el("span", {}, state.cameras[event.camera] || event.camera)
      );
    }

    if (counterEl) {
      counterEl.textContent = `${index + 1} / ${state.events.length}`;
    }

    // Fetch full image
    fetchImageBlob(`/api/cameras/events/${event.id}/snapshot?crop=false`).then(url => {
      if (url) {
        if (state.imageUrls["modal"]) URL.revokeObjectURL(state.imageUrls["modal"]);
        state.imageUrls["modal"] = url;
        imgEl.src = url;
        imgEl.style.opacity = "1";
      } else {
        imgEl.alt = "Failed to load snapshot";
        imgEl.style.opacity = "1";
      }
    });
  }

  function navigateModal(delta) {
    if (state.currentEventIndex < 0 || state.events.length === 0) return;
    const newIndex = (state.currentEventIndex + delta + state.events.length) % state.events.length;
    updateModalContent(newIndex);
  }

  function openEventModal(index) {
    const modal = $("#snapshot-modal");
    if (!modal) return;

    modal.classList.add("open");
    updateModalContent(index);

    // Keyboard navigation
    if (state.modalKeyHandler) {
      document.removeEventListener("keydown", state.modalKeyHandler);
    }
    state.modalKeyHandler = (e) => {
      if (e.key === "Escape") {
        closeEventModal();
      } else if (e.key === "ArrowLeft") {
        navigateModal(-1);
      } else if (e.key === "ArrowRight") {
        navigateModal(1);
      }
    };
    document.addEventListener("keydown", state.modalKeyHandler);
  }

  function closeEventModal() {
    const modal = $("#snapshot-modal");
    if (modal) modal.classList.remove("open");

    if (state.modalKeyHandler) {
      document.removeEventListener("keydown", state.modalKeyHandler);
      state.modalKeyHandler = null;
    }
    if (state.imageUrls["modal"]) {
      URL.revokeObjectURL(state.imageUrls["modal"]);
      delete state.imageUrls["modal"];
    }
    state.currentEventIndex = -1;
  }

  async function renderEvents() {
    const list = $("#cameras-events-list");
    if (!list) return;
    list.innerHTML = "";

    if (state.events.length === 0) {
      list.append(el("div", { style: "color: var(--ink-2); font-style: italic;" }, "No recent motion events."));
      return;
    }

    for (const evt of state.events) {
      const evtId = evt.id;
      const imgEl = el("img", { class: "event-thumb placeholder", alt: evt.label });
      
      if (state.imageUrls[`evt_${evtId}`]) {
        imgEl.src = state.imageUrls[`evt_${evtId}`];
        imgEl.classList.remove("placeholder");
      } else {
        fetchImageBlob(`/api/cameras/events/${evtId}/snapshot?crop=true`).then(thumbUrl => {
          if (thumbUrl) {
            state.imageUrls[`evt_${evtId}`] = thumbUrl;
            imgEl.src = thumbUrl;
            imgEl.classList.remove("placeholder");
          }
        });
      }

      const durStr = evt.end_time ? Math.round(evt.end_time - evt.start_time) + "s" : "Ongoing";
      const score = Math.round((evt.data?.score || 0) * 100);

      const card = el("div", { class: "event-card", onclick: () => openEventModal(state.events.indexOf(evt)) },
        imgEl,
        el("div", { class: "event-details" },
          el("div", { class: "event-title" },
            el("span", { class: "event-badge" }, evt.label),
            el("span", {}, state.cameras[evt.camera] || evt.camera)
          ),
          el("div", { class: "event-meta" },
            el("span", {}, formatEventTime(evt.start_time)),
            el("span", {}, ` · ${durStr}`),
            score > 0 ? el("span", {}, ` · ${score}% confidence`) : null
          )
        )
      );

      list.append(card);
    }
  }

  function buildLayout() {
    const root = document.getElementById("panel-cameras");
    if (!root) return;
    root.innerHTML = "";

    const header = el("div", { id: "cameras-header", class: "cameras-header" });
    const grid = el("div", { id: "cameras-grid", class: "cameras-grid" });
    
    const eventsSec = el("div", { class: "events-section" },
      el("h3", {}, "Recent Events"),
      el("div", { id: "cameras-events-list", class: "events-list" })
    );

    const liveModal = el("div", { id: "live-camera-modal", class: "snapshot-modal" },
      el("div", { class: "snapshot-modal-content" },
        el("div", { class: "snapshot-modal-header" },
          el("h3", {}, ""),
          el("div", { class: "snapshot-modal-counter" }, ""),
          el("button", {
            class: "snapshot-modal-close",
            type: "button",
            title: "Close",
            "aria-label": "Close",
            onclick: () => closeLiveCameraModal(),
            html: `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>`,
          })
        ),
        el("div", { class: "snapshot-modal-body" },
          el("img", { alt: "Camera snapshot" })
        ),
        el("div", { class: "snapshot-modal-actions" },
          el("button", {
            class: "btn",
            type: "button",
            onclick: () => {
              const m = $("#live-camera-modal");
              if (m && m._loadSnap) m._loadSnap(true);
            },
          }, "Refresh"),
          el("button", { class: "btn", type: "button", onclick: () => closeLiveCameraModal() }, "Close")
        )
      )
    );
    liveModal.addEventListener("click", (e) => {
      if (e.target === liveModal) closeLiveCameraModal();
    });

    const modal = el("div", { id: "snapshot-modal", class: "snapshot-modal" },
      el("div", { class: "snapshot-modal-content" },
        el("div", { class: "snapshot-modal-header" },
          el("h3", {}, ""),
          el("div", { class: "snapshot-modal-counter", style: "font-family: var(--font-mono); font-size: 0.85rem; color: var(--ink-3);" }, ""),
          el("button", { class: "snapshot-modal-close", onclick: () => closeEventModal(), html: `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>` })
        ),
        el("div", { class: "snapshot-modal-body" },
          el("button", { 
            class: "modal-nav prev", 
            onclick: (e) => { e.stopImmediatePropagation(); navigateModal(-1); },
            html: `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>`
          }),
          el("img", { onclick: (e) => { /* clicking image goes to next for quick review */ navigateModal(1); } }),
          el("button", { 
            class: "modal-nav next", 
            onclick: (e) => { e.stopImmediatePropagation(); navigateModal(1); },
            html: `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>`
          })
        ),
        el("div", { class: "snapshot-modal-actions" },
          el("button", { class: "btn", onclick: () => closeEventModal() }, "Close")
        )
      )
    );

    // Close modal on click outside
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeEventModal();
    });

    closeLiveCameraModal();
    closeEventModal();
    const oldLiveModal = document.getElementById("live-camera-modal");
    if (oldLiveModal) oldLiveModal.remove();
    document.body.append(liveModal);

    const oldModal = document.getElementById("snapshot-modal");
    if (oldModal) oldModal.remove();
    document.body.append(modal);

    root.append(header, grid, eventsSec);
  }

  function isSecurityPanelActive() {
    const p = window._activePanel;
    return p === "security" || p === "cameras";
  }

  window.renderCameras = async function () {
    if (window.showPanel && !isSecurityPanelActive()) return;

    if (!document.getElementById("cameras-header")) {
      buildLayout();
    }

    await loadData();
    renderHeader();
    renderGrid();
    renderEvents();

    if (!state.refreshInterval) {
      state.refreshInterval = setInterval(async () => {
        if (!isSecurityPanelActive()) {
          clearInterval(state.refreshInterval);
          state.refreshInterval = null;
          releaseObjectURLs();
          return;
        }
        await loadData();
        renderHeader();
        // Skip re-rendering grid to avoid flicker, just update snapshots directly
        const camIds = Object.keys(state.cameras);
        for (const camId of camIds) {
          const img = document.querySelector(`.camera-card img[alt="${state.cameras[camId]}"]`);
          if (img) refreshCameraImage(camId, img);
        }
        renderEvents();
      }, 15000);
    }
  };

  // cleanup on navigation away
  const origShowPanel = window.showPanel;
  if (origShowPanel) {
    window.showPanel = function(id) {
      if (isSecurityPanelActive() && id !== "security" && id !== "cameras") {
        if (state.refreshInterval) {
          clearInterval(state.refreshInterval);
          state.refreshInterval = null;
        }
        releaseObjectURLs();
      }
      return origShowPanel.apply(this, arguments);
    };
  }

})();
