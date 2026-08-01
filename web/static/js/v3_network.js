/**
 * Bernie v3 Redesign - Phase 7: Network Screen
 * Implementation using el() helper pattern.
 */

(function() {
  const $  = (sel, root = document) => root.querySelector(sel);
  const el = (...args) => window.el(...args);

  const api = window.api;

  // --- Constants ---
  const NET_STYLE = {
    unifi:  { bg: 'rgba(120,131,250,.16)', fg: '#9aa6ff',  label: 'unifi' },
    google: { bg: 'rgba(76,184,168,.16)',  fg: '#6cd0ba',  label: 'google' },
    both:   { bg: 'rgba(176,128,232,.18)', fg: '#c4a3f0',  label: 'both' },
  };

  const STATUS = {
    confirmed: { fg: '#4ea674', label: '✓ confirmed' },
    suspected: { fg: '#e08a3a', label: '? suspected' },
  };

  const maskMac = mac => (mac && String(mac).length >= 9) ? String(mac).slice(0, 8) + "\u00b7\u00b7:\u00b7\u00b7:\u00b7\u00b7" : String(mac || "");

  function kindIcon(kind, size = 16) {
    const c = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', "stroke-width": 1.6, "stroke-linecap": 'round', "stroke-linejoin": 'round' };
    let inner = '';
    switch (kind) {
      case 'laptop':  inner = '<rect x="3" y="5" width="18" height="11" rx="1.5"/><path d="M2 19h20"/>'; break;
      case 'phone':   inner = '<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/>'; break;
      case 'tablet':  inner = '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M11 18h2"/>'; break;
      case 'speaker': inner = '<rect x="6" y="3" width="12" height="18" rx="2"/><circle cx="12" cy="14" r="3"/><circle cx="12" cy="7" r="1"/>'; break;
      case 'server':  inner = '<rect x="3" y="4" width="18" height="6" rx="1"/><rect x="3" y="14" width="18" height="6" rx="1"/><circle cx="7" cy="7" r=".5" fill="currentColor"/><circle cx="7" cy="17" r=".5" fill="currentColor"/>'; break;
      case 'hub':     inner = '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/>'; break;
      case 'router':  inner = '<path d="M5 16h14v4H5z"/><path d="M12 12V8M8 8l4-4 4 4"/><circle cx="9" cy="18" r=".5" fill="currentColor"/><circle cx="15" cy="18" r=".5" fill="currentColor"/>'; break;
      case 'bulb':    inner = '<path d="M9 18h6M10 21h4M9 14a5 5 0 1 1 6 0c-1 1-1 2-1 3h-4c0-1 0-2-1-3z"/>'; break;
      case 'iot':     inner = '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9z"/>'; break;
      case 'console': inner = '<rect x="3" y="7" width="18" height="10" rx="3"/><circle cx="8" cy="12" r="1.2"/><circle cx="16" cy="12" r="1.2"/>'; break;
      default:        inner = '<circle cx="12" cy="12" r="9"/><path d="M9 10c0-2 1.5-3 3-3s3 1 3 2.5-3 1.5-3 3.5M12 17v.5"/>'; break;
    }
    return el('svg', { ...c, html: inner });
  }

  function plugIcon() {
    return el('svg', { 
      width: 11, height: 11, viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round",
      style: "color:var(--ink-3);flex-shrink:0;margin-right:4px",
      html: '<path d="M9 2v4M15 2v4M7 6h10v4a5 5 0 0 1-10 0z M12 15v5"/>'
    });
  }

  // --- State Management ---
  let state = {
    devices: [],
    q: '',
    editing: null,
    expanded: null,
    filter: 'online',
    showMacs: true,
    sortMode: 'online'
  };

  function setState(patch) {
    state = { ...state, ...patch };
    render();
  }

  async function updateDevice(mac, patch) {
    try {
      await api(`/api/network/devices/${encodeURIComponent(mac)}`, {
        method: 'PUT',
        body: patch
      });
      const newDevices = state.devices.map(d => {
        if (d.mac !== mac) return d;
        const updated = { ...d, ...patch };
        if ('name' in patch) {
          updated.custom_name  = patch.name;
          updated.display_name = patch.name || d.unifi_name || d.hostname || '';
        }
        return updated;
      });
      if (window.BernieData) window.BernieData.network = newDevices;
      setState({ devices: newDevices });
    } catch (err) {
      console.error("Failed to update device:", err);
      alert("Failed to update device: " + err.message);
    }
  }

  async function refreshDevices() {
    try {
      const data = await api('/api/network/devices');
      if (data) {
        if (window.BernieData) window.BernieData.network = data;
        localStorage.setItem("bernie-network-cache", JSON.stringify(data));
        setState({ devices: data });
      }
    } catch (err) {
      console.error("Refresh failed:", err);
    }
  }

  // --- UI Components ---

  const StatPill = (count, label, color) => el('div', {
    style: `display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px; background: var(--bg-pill); border: 1px solid var(--stroke); font-size: 12px; font-family: var(--font-mono)`
  },
    el('span', { style: `width: 7px; height: 7px; border-radius: 50%; background: ${color}` }),
    el('span', { style: `color: var(--ink); font-weight: 600` }, String(count)),
    el('span', { style: `color: var(--ink-3)` }, label)
  );

  const StatusCheck = (active, onClick, kind) => {
    const s = STATUS[kind];
    return el('button', {
      onclick: (e) => { e.stopPropagation(); onClick(); },
      style: `display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; background: ${active ? 'color-mix(in oklab, ' + s.fg + ' 18%, transparent)' : 'var(--bg-pill)'}; border: 1px solid ${active ? s.fg : 'var(--stroke)'}; color: ${active ? s.fg : 'var(--ink-3)'}; font-size: 11.5px; font-weight: 500; cursor: pointer; font-family: var(--font-sans)`
    },
      el('span', {
        style: `width: 12px; height: 12px; border-radius: 3px; border: 1.5px solid ${active ? s.fg : 'var(--ink-4)'}; background: ${active ? s.fg : 'transparent'}; display: grid; place-items: center; color: #1a1410; font-size: 9px; font-weight: 700; line-height: 1`,
        html: active ? '✓' : ''
      }),
      s.label
    );
  };

  const DeviceRow = (d, isLast) => {
    const isEditing = state.editing === d.mac;
    const isExpanded = state.expanded === d.mac;
    const unnamed = !d.display_name;
    const net = NET_STYLE[d.network] || { bg: 'rgba(255,255,255,.05)', fg: 'var(--ink-3)', label: d.network || 'unknown' };
    const macDisplay = state.showMacs ? d.mac : (unnamed ? d.mac : maskMac(d.mac));

    return el('div', {
      style: `border-bottom: ${isLast ? 'none' : '1px solid var(--stroke)'}; background: ${isExpanded ? 'rgba(255,255,255,.015)' : 'transparent'}; transition: background .12s`
    },
      el('div', {
        onclick: () => !isEditing && setState({ expanded: isExpanded ? null : d.mac }),
        style: `display: grid; grid-template-columns: 32px 1fr 110px 110px 95px; gap: 12px; padding: 9px 14px; align-items: center; opacity: ${unnamed ? 0.78 : 1}; cursor: pointer`
      },
        // Icon
        el('div', {
          style: `width: 30px; height: 30px; border-radius: 8px; background: ${unnamed ? 'rgba(255,255,255,.025)' : net.bg}; color: ${unnamed ? 'var(--ink-3)' : net.fg}; border: 1px solid ${unnamed ? 'var(--stroke)' : 'transparent'}; display: grid; place-items: center`
        }, kindIcon(d.kind)),

        // Name + secondary
        el('div', { style: 'display: flex; flex-direction: column; gap: 1px; min-width: 0' },
          isEditing ? el('input', {
            value: d.custom_name || '',
            autofocus: true,
            onclick: (e) => e.stopPropagation(),
            onkeydown: (e) => {
              if (e.key === 'Enter') { updateDevice(d.mac, { name: e.target.value.trim() }); setState({ editing: null }); }
              if (e.key === 'Escape') setState({ editing: null });
            },
            onblur: (e) => { updateDevice(d.mac, { name: e.target.value.trim() }); setState({ editing: null }); },
            placeholder: 'name this device',
            style: 'background: var(--bg-input); border: 1px solid var(--amber); border-radius: 6px; color: var(--ink); padding: 4px 8px; font-family: var(--font-sans); font-size: 13.5px; outline: none; width: 100%'
          }) : el('div', {
            style: `display: flex; align-items: center; gap: 6px; font-size: 13.5px; font-weight: ${unnamed ? 400 : 500}; font-style: ${unnamed ? 'italic' : 'normal'}; color: ${unnamed ? 'var(--ink-3)' : 'var(--ink)'}; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap`
          },
            d.is_wired && plugIcon(),
            d.status === 'suspected' && el('span', { style: 'color: var(--amber); font-size: 11px' }, '?'),
            el('span', { style: 'overflow: hidden; text-overflow: ellipsis' }, d.display_name || 'unnamed device'),
            unnamed && el('span', { style: 'color: var(--amber); font-size: 10px; font-style: normal; font-weight: 500' }, 'name?')
          ),
          el('div', {
            style: 'font-size: 11px; color: var(--ink-4); display: flex; gap: 6px; align-items: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap'
          },
            el('span', {}, d.vendor),
            el('span', { style: 'color: var(--ink-4)' }, '·'),
            el('span', { style: 'font-family: var(--font-mono); font-size: 10.5px' }, macDisplay)
          )
        ),

        // IP
        el('div', { style: 'font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-2)' }, d.ip),

        // Network badge
        el('div', {}, el('span', {
          style: `display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 999px; background: ${net.bg}; color: ${net.fg}; font-size: 10.5px; font-weight: 500; font-family: var(--font-mono)`
        },
          el('span', { style: `width: 5px; height: 5px; border-radius: 50%; background: ${net.fg}` }),
          net.label
        )),

        // Seen
        el('div', { 
          class: 't-mono', 
          style: `font-size: 11px; color: ${d.seen === 'now' ? 'var(--ok)' : 'var(--ink-3)'}; text-align: right` 
        }, d.seen)
      ),

      // Expanded panel
      isExpanded && !isEditing && el('div', {
        style: 'padding: 4px 14px 14px 56px; display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--stroke); background: rgba(255,255,255,.01)'
      },
        // Status check selector
        el('div', { class: 'row gap-2', style: 'align-items: center; margin-top: 10px' },
          el('span', { class: 't-eyebrow', style: 'width: 70px' }, 'Status'),
          StatusCheck(d.status === 'confirmed', () => updateDevice(d.mac, { status: d.status === 'confirmed' ? null : 'confirmed' }), 'confirmed'),
          StatusCheck(d.status === 'suspected', () => updateDevice(d.mac, { status: d.status === 'suspected' ? null : 'suspected' }), 'suspected'),
          el('span', { class: 't-meta t-muted', style: 'font-size: 11px; margin-left: 6px' },
            d.status === 'confirmed' ? "you've identified this device" :
            d.status === 'suspected' ? 'best guess — needs verification' :
            'unknown — please identify'
          )
        ),
        // Full MAC
        el('div', { class: 'row gap-2', style: 'align-items: center' },
          el('span', { class: 't-eyebrow', style: 'width: 70px' }, 'MAC'),
          el('span', { style: 'font-family: var(--font-mono); font-size: 12px; color: var(--ink-2)' }, d.mac),
          el('button', {
            class: 'btn btn-ghost',
            style: 'padding: 2px 8px; font-size: 10.5px',
            onclick: (e) => { 
              e.stopPropagation(); 
              navigator.clipboard.writeText(d.mac).then(() => {
                const b = e.target;
                const oldText = b.textContent;
                b.textContent = 'copied!';
                setTimeout(() => b.textContent = oldText, 2000);
              }); 
            }
          }, 'copy')
        ),
        el('div', { class: 'row gap-2', style: 'margin-top: 4px' },
          el('button', { class: 'btn', onclick: (e) => { e.stopPropagation(); setState({ editing: d.mac }); } }, 'Rename')
        )
      )
    );
  };

  // --- Main Render Function ---
  function renderList() {
    const listRoot = $("#network-list-root");
    if (!listRoot) return;

    const { devices, q, filter, showMacs } = state;

    const filtered = devices.filter(d => {
      if (filter === 'online'    && d.seen !== 'now')         return false;
      if (filter === 'unnamed'   && d.display_name)          return false;
      if (filter === 'suspected' && d.status !== 'suspected') return false;
      if (filter === 'unifi'     && d.network === 'google')  return false;
      if (filter === 'google'    && d.network === 'unifi')   return false;
      if (!q) return true;
      const s = q.toLowerCase();
      return (d.mac          || '').toLowerCase().includes(s)
          || (d.display_name || '').toLowerCase().includes(s)
          || (d.hostname     || '').toLowerCase().includes(s)
          || (d.vendor       || '').toLowerCase().includes(s)
          || (d.ip           || '').includes(s);
    });

    filtered.sort((a, b) => {
      if (state.sortMode === 'online') {
        if (a.seen === 'now' && b.seen !== 'now') return -1;
        if (a.seen !== 'now' && b.seen === 'now') return 1;
      } else if (state.sortMode === 'name') {
        const nA = a.display_name || a.hostname || a.mac || '';
        const nB = b.display_name || b.hostname || b.mac || '';
        return nA.localeCompare(nB);
      } else if (state.sortMode === 'ip') {
        const ipA = (a.ip || '').split('.').map(num => num.padStart(3, '0')).join('.');
        const ipB = (b.ip || '').split('.').map(num => num.padStart(3, '0')).join('.');
        return ipA.localeCompare(ipB);
      } else if (state.sortMode === 'mac') {
        return (a.mac || '').localeCompare(b.mac || '');
      }

      const getOrder = (d) => {
        if (!d.display_name) return 0;
        if (d.status === 'suspected') return 1;
        if (d.status === 'confirmed') return 2;
        return 3;
      };
      const oa = getOrder(a);
      const ob = getOrder(b);
      if (oa !== ob) return oa - ob;

      const ipA = (a.ip || '').split('.').map(num => num.padStart(3, '0')).join('.');
      const ipB = (b.ip || '').split('.').map(num => num.padStart(3, '0')).join('.');
      return ipA.localeCompare(ipB);
    });

    listRoot.innerHTML = '';
    if (filtered.length === 0) {
      listRoot.append(el('div', { style: 'padding: 32px; text-align: center; color: var(--ink-3)' }, 'No devices match.'));
    } else {
      filtered.forEach((d, i) => listRoot.append(DeviceRow(d, i === filtered.length - 1)));
    }
  }

  function render() {
    const root = $("#panel-network");
    if (!root) return;

    if (root.querySelector("#network-list-root")) {
       // Already initialized, just update counts and list
       const counts = {
         all: state.devices.length,
         online: state.devices.filter(d => d.seen === 'now').length,
         unnamed: state.devices.filter(d => !d.display_name).length,
         suspected: state.devices.filter(d => d.status === 'suspected').length,
         confirmed: state.devices.filter(d => d.status === 'confirmed').length,
       };
       // We can dynamically update counts if we add IDs to the labels, but surgical render of list is the main priority.
       // Re-rendering the whole toolbar loses focus. Let's just update the list.
       renderList();
       return;
    }

    const { devices, q, filter, showMacs } = state;

    const counts = {
      all: devices.length,
      online: devices.filter(d => d.seen === 'now').length,
      unnamed: devices.filter(d => !d.display_name).length,
      suspected: devices.filter(d => d.status === 'suspected').length,
      confirmed: devices.filter(d => d.status === 'confirmed').length,
    };

    root.innerHTML = '';
    root.className = 'page page-fade';
    root.style.maxWidth = '1080px';
    root.append(
      el('div', { class: 'row between', style: 'align-items: flex-end; margin-bottom: 24px' },
        el('div', {},
          el('div', { class: 't-eyebrow' }, 'Networks'),
          el('div', { class: 't-h1', style: 'font-size: 28px; font-family: var(--font-serif); font-weight: 400; margin-top: 4px' }, 'Devices')
        ),
        el('div', { class: 'row gap-2' },
          el('button', { 
            class: 'btn', 
            onclick: () => { state.showMacs = !state.showMacs; renderList(); },
            style: showMacs ? 'background: var(--bg-pill-hover); border-color: var(--stroke-strong)' : ''
          }, `MAC ${showMacs ? 'on' : 'off'}`),
          el('button', { 
            class: 'btn',
            onclick: () => refreshDevices()
          }, 'Refresh')
        )
      ),

      // Stat strip
      el('div', { id: 'network-stats', class: 'row gap-3', style: 'flex-wrap: wrap; margin-bottom: 24px' },
        StatPill(counts.confirmed, 'confirmed', 'var(--ok)'),
        StatPill(counts.suspected, 'suspected', 'var(--amber)'),
        StatPill(counts.unnamed, 'unnamed', 'var(--ink-3)')
      ),

      // Toolbar
      el('div', { class: 'card', style: 'padding: 10px 12px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 20px' },
        el('input', {
          class: 'input',
          placeholder: 'filter mac, name, vendor, ip…',
          value: q,
          oninput: (e) => { state.q = e.target.value; renderList(); },
          style: 'flex: 1; min-width: 220px'
        }),
        el('div', { class: 'row gap-2', style: 'flex-wrap: wrap' },
          [
            { id: 'all',       label: `All` },
            { id: 'online',    label: `Online` },
            { id: 'unnamed',   label: `Unnamed` },
            { id: 'suspected', label: `Suspected` },
            { id: 'unifi',     label: 'Unifi' },
            { id: 'google',    label: 'Google' },
          ].map(f => el('button', {
            class: 'btn filter-btn',
            'data-id': f.id,
            onclick: (e) => { 
              state.filter = f.id; 
              root.querySelectorAll('.filter-btn').forEach(b => {
                 b.style.background = ''; b.style.borderColor = ''; b.style.color = '';
              });
              e.target.style.background = 'var(--bg-pill-hover)';
              e.target.style.borderColor = 'var(--stroke-strong)';
              e.target.style.color = 'var(--ink)';
              renderList(); 
            },
            style: filter === f.id ? 'background: var(--bg-pill-hover); border-color: var(--stroke-strong); color: var(--ink)' : ''
          }, f.label))
        )
      ),

      // Rows
      el('div', { class: 'card', style: 'padding: 0; overflow: hidden' },
        el('div', { style: 'display: grid; grid-template-columns: 32px 1fr 110px 110px 95px; gap: 12px; padding: 12px 14px; align-items: center; border-bottom: 1px solid var(--stroke); background: var(--bg-sub); font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--ink-3);' },
          el('div', {}, 'Icon'),
          el('div', { style: 'cursor: pointer; user-select: none;', onclick: () => { state.sortMode = 'name'; renderList(); } }, `Name ${state.sortMode === 'name' ? '↓' : ''}`),
          el('div', { style: 'cursor: pointer; user-select: none;', onclick: () => { state.sortMode = 'ip'; renderList(); } }, `IP ${state.sortMode === 'ip' ? '↓' : ''}`),
          el('div', { style: 'cursor: pointer; user-select: none;', onclick: () => { state.sortMode = 'mac'; renderList(); } }, `Network ${state.sortMode === 'mac' ? '↓' : ''}`),
          el('div', { style: 'cursor: pointer; user-select: none; text-align: right', onclick: () => { state.sortMode = 'online'; renderList(); } }, `Seen ${state.sortMode === 'online' ? '↓' : ''}`)
        ),
        el('div', { id: 'network-list-root' })
      )
    );
    renderList();
  }

  // Global entry point
  window.renderNetwork = function() {
    state.devices = window.BernieData?.network || [];
    if (!state.devices.length) {
      refreshDevices();
      return;
    }
    render();
  };

})();
