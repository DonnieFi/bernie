/**
 * Bernie v3 Redesign - Phase 6: Config Screen
 * Standalone JS implementation using el() helper pattern.
 */

(function() {
  // --- Helpers (matching app.v6.js patterns) ---
  const $ = (...args) => window.$(...args);
  const el = (...args) => window.el(...args);


  function relativeTime(ts) {
    if (!ts) return '';
    const diff = (Date.now() - ts) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return new Date(ts).toLocaleDateString();
  }

  // --- State Management ---
  let state = {
    files: [],       // list of strings (filenames)
    active: null,    // string (filename)
    contents: {},    // name -> content
    draft: '',       // current editor text
    savedAt: null,   // timestamp of last save for active file
    loading: false
  };

  function setState(patch) {
    state = { ...state, ...patch };
    render();
  }

  async function loadFileList() {
    try {
      const files = await window.api('/api/config/files');
      setState({ files });
      if (files.length > 0 && !state.active) {
        switchFile(files[0]);
      }
    } catch (err) {
      console.error("Failed to load files:", err);
    }
  }

  async function switchFile(name) {
    const dirty = state.active && state.draft !== state.contents[state.active];
    if (dirty && !confirm(`Discard unsaved changes to ${state.active}?`)) return;

    if (state.contents[name] !== undefined) {
      setState({ active: name, draft: state.contents[name], savedAt: null });
      return;
    }

    setState({ loading: true });
    try {
      const res = await window.api(`/api/config/files/${encodeURIComponent(name)}`);
      setState({ 
        active: name, 
        contents: { ...state.contents, [name]: res.content },
        draft: res.content,
        savedAt: null,
        loading: false
      });
    } catch (err) {
      console.error("Failed to load file:", err);
      setState({ loading: false });
    }
  }

  async function save() {
    if (!state.active) return;
    if (!confirm(`Save changes to ${state.active}?`)) return;
    try {
      await window.api(`/api/config/files/${encodeURIComponent(state.active)}`, {
        method: 'PUT',
        body: { content: state.draft }
      });
      setState({ 
        contents: { ...state.contents, [state.active]: state.draft },
        savedAt: Date.now()
      });
    } catch (err) {
      console.error("Save failed:", err);
      alert("Save failed: " + err.message);
    }
  }

  function discard() {
    if (state.active) {
      setState({ draft: state.contents[state.active] });
    }
  }

  // --- UI Components ---
  const FileNavItem = (name) => {
    const isActive = state.active === name;
    const isDirty = isActive && state.draft !== state.contents[name];
    
    return el('div', {
      onclick: () => switchFile(name),
      style: `padding: 8px 14px; background: ${isActive ? 'var(--bg-card)' : 'transparent'}; border-left: 2px solid ${isActive ? 'var(--amber)' : 'transparent'}; font-family: var(--font-mono); font-size: 12px; color: ${isActive ? 'var(--ink)' : 'var(--ink-2)'}; display: flex; align-items: center; gap: 6px; cursor: default; transition: background .12s`
    },
      el('span', {}, name),
      isDirty && el('span', { style: 'color: var(--amber)' }, '•')
    );
  };

  // --- Main Render Function ---
  function render() {
    const root = $("#panel-config");
    if (!root) return;

    const { files, active, draft, contents, savedAt, loading } = state;
    const currentContent = contents[active];
    const dirty = active && draft !== currentContent;

    root.innerHTML = '';
    root.className = 'page page-fade';
    root.style.maxWidth = '1080px';
    const fileBlurb = (name) => {
      const map = {
        "soul.md": "Core identity / vibe",
        "bernie.md": "How Bernie talks and acts",
        "family.md": "Household setup notes",
        "context.md": "Hot family context",
        "capabilities.md": "Full capability reference",
        "capabilities_index.md": "Compact routing index (loaded each turn)",
        "USER_OVERRIDE.md": "Immutable human facts (edit carefully)",
      };
      if (map[name]) return map[name];
      if (name.startsWith("family/")) return "OSS family pack person note";
      if (name.endsWith(".md")) return "Person / household note";
      return "";
    };

    root.append(
      el('div', {},
        el('div', { class: 't-eyebrow' }, 'Personality'),
        el('div', { class: 't-h1', style: 'font-size: 28px; font-family: var(--font-serif); font-weight: 400; margin-top: 4px' }, 'Config'),
        el('div', { class: 't-body', style: 'color: var(--ink-3); margin-top: 8px; max-width: 42rem' },
          'Soul, Bernie, capabilities, and per-person notes. Deploy guides, ADRs, and config.json stay on disk — not in this panel.')
      ),

      el('div', { class: 'card', style: 'margin-top: 24px; overflow: hidden; display: grid; grid-template-columns: 240px 1fr; min-height: 500px' },
        // File list — personality allowlist (API-enforced)
        el('div', { style: 'border-right: 1px solid var(--stroke); background: var(--bg-card-2); overflow-y: auto; max-height: 70vh' },
          el('div', { class: 't-eyebrow', style: 'padding: 14px 14px 8px' }, 'Docs'),
          files.length
            ? files.map(f => FileNavItem(f))
            : el('div', { style: 'padding: 14px; color: var(--ink-3); font-size: 12px' }, 'No personality docs found under docs/'),
          el('div', { style: 'padding: 12px 14px; font-size: 11px; color: var(--ink-4); line-height: 1.4' },
            'soul · bernie · family · people · capabilities')
        ),

        // Editor
        el('div', { style: 'display: flex; flex-direction: column; background: var(--bg-card)' },
          active ? [
            el('div', { class: 'row between', style: 'padding: 12px 16px; border-bottom: 1px solid var(--stroke)' },
              el('div', { class: 'col', style: 'gap: 2px' },
                el('div', { class: 'row gap-2', style: 'align-items: baseline' },
                  el('span', { class: 't-mono', style: 'color: var(--ink); font-size: 13px' }, active),
                  el('span', { class: 't-meta', style: 'color: var(--ink-4)' },
                    dirty ? '· unsaved changes' : (savedAt ? `· saved ${relativeTime(savedAt)}` : '· saved')
                  )
                ),
                fileBlurb(active) && el('div', { style: 'font-size: 11.5px; color: var(--ink-3)' }, fileBlurb(active))
              ),
              el('div', { class: 'row gap-2' },
                el('button', {
                  class: 'btn',
                  disabled: !dirty,
                  onclick: discard,
                  style: `opacity: ${dirty ? 1 : 0.4}; cursor: ${dirty ? 'pointer' : 'default'}`
                }, 'Discard'),
                el('button', {
                  class: 'btn btn-primary',
                  disabled: !dirty,
                  onclick: save,
                  style: `opacity: ${dirty ? 1 : 0.5}; cursor: ${dirty ? 'pointer' : 'default'}`
                }, 'Save')
              )
            ),
            loading ? el('div', { style: 'padding: 40px; text-align: center; color: var(--ink-3)' }, 'Loading...') :
            (() => {
              const ta = el('textarea', {
                spellcheck: false,
                oninput: (e) => setState({ draft: e.target.value }),
                style: 'width: 100%; flex: 1; border: 0; outline: 0; resize: none; padding: 18px 20px; background: var(--bg-card); color: var(--ink); font-family: var(--font-mono); font-size: 13px; line-height: 1.7'
              });
              ta.value = draft; // must be set as a property, not an attribute
              return ta;
            })()
          ] : el('div', { style: 'padding: 40px; text-align: center; color: var(--ink-3)' }, 'Select a personality or person doc')
        )
      )
    );
  }

  // Global entry point
  window.renderConfig = function() {
    loadFileList();
  };

})();
