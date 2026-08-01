(function () {
  const el = (...args) => window.el(...args);
  const api = (...args) => window.api(...args);

  async function renderGroceries(host) {
    if (!host) return;
    host.replaceChildren(el("p", { class: "calendar-status" }, "Loading groceries…"));
    try {
      const items = await api("/api/groceries");
      const grouped = {};
      for (const item of items || []) {
        const cat = item.category || "Other";
        grouped[cat] = grouped[cat] || [];
        grouped[cat].push(item);
      }
      const cats = Object.keys(grouped).sort();
      if (!cats.length) {
        host.replaceChildren(el("p", { class: "calendar-status" }, "Grocery list is empty."));
        return;
      }
      host.replaceChildren(el("div", { class: "grocery-checklist" },
        cats.map(cat => el("section", { class: "grocery-category" },
          el("h3", {}, cat),
          el("ul", { class: "grocery-items" },
            grouped[cat].map(item => el("li", { class: item.checked ? "is-checked" : "" },
              el("label", {},
                el("input", {
                  type: "checkbox",
                  checked: item.checked ? "checked" : null,
                  onchange: async e => {
                    await api(`/api/groceries/${item.id}`, {
                      method: "PATCH",
                      body: JSON.stringify({ checked: e.target.checked }),
                    });
                  },
                }),
                item.item
              ),
              el("button", {
                type: "button",
                class: "grocery-remove",
                "aria-label": `Remove ${item.item}`,
                onclick: async () => {
                  await api(`/api/groceries?item=${encodeURIComponent(item.item)}`, { method: "DELETE" });
                  renderGroceries(host);
                },
              }, "×")
            ))
          )
        ))
      ));
    } catch (error) {
      host.replaceChildren(el("p", { class: "calendar-status is-error" }, error.message || "Groceries unavailable"));
    }
  }

  async function openGroceryPanel() {
    let panel = document.getElementById("calendar-grocery-panel");
    if (!panel) {
      panel = el("section", {
        id: "calendar-grocery-panel",
        class: "calendar-grocery-panel",
        "aria-labelledby": "calendar-grocery-title",
      },
        el("header", { class: "calendar-grocery-header" },
          el("h2", { id: "calendar-grocery-title" }, "Groceries"),
          el("button", {
            type: "button",
            class: "calendar-grocery-close",
            onclick: () => panel.remove(),
          }, "Close")
        ),
        el("div", { id: "calendar-grocery-list" })
      );
      document.getElementById("panel-calendar")?.querySelector(".calendar-shell")?.append(panel);
    }
    await renderGroceries(document.getElementById("calendar-grocery-list"));
  }

  async function confirmSuggestGroceries(start, end) {
    const data = await api(`/api/meals/suggest-groceries?start=${start}&end=${end}&meal_type=dinner`);
    const picks = (data.suggestions || []).filter(Boolean);
    if (!picks.length) {
      window.flashBernie && window.flashBernie("No grocery suggestions for planned dinners");
      return;
    }
    const labels = picks.map(p => `${p.item} (${p.category})`).join("\n");
    if (!confirm(`Add these groceries?\n\n${labels}`)) return;
    await api("/api/groceries/bulk", {
      method: "POST",
      body: JSON.stringify({ items: picks.map(p => ({ item: p.item, category: p.category })) }),
    });
    window.flashBernie && window.flashBernie(`Added ${picks.length} grocery item${picks.length === 1 ? "" : "s"}`);
    openGroceryPanel();
  }

  window.openGroceryPanel = openGroceryPanel;
  window.confirmSuggestGroceries = confirmSuggestGroceries;
})();
