(function () {
  const el = (...args) => window.el(...args);
  const api = (...args) => window.api(...args);
  const POLL_MS = 5 * 60 * 1000;
  const PERSON_ACCENTS_DEFAULT = {
    dad: "oklch(0.72 0.14 50)",
    mom: "oklch(0.72 0.13 200)",
    child1: "oklch(0.7 0.16 320)",
    child2: "oklch(0.72 0.13 140)",
  };

  const savedView = sessionStorage.getItem("bernie-calendar-view");
  const savedPerson = sessionStorage.getItem("bernie-calendar-person");
  const state = {
    start: null,
    focus: new Date(),
    data: null,
    meals: [],
    chores: [],
    loading: false,
    view: savedView || (matchMedia("(max-width: 600px)").matches ? "day" : "week"),
    personFilter: savedPerson || "all",
    showSchool: true,
    pollTimer: null,
  };

  const dayKey = date => [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");

  function normPersonId(id) {
    if (!id) return "";
    const s = String(id);
    return s.startsWith("person:") ? s.slice(7) : s.toLowerCase();
  }

  function personAccent(who) {
    const key = normPersonId(who);
    const fromMe = (window.Me && window.Me.person_colors) || {};
    if (fromMe[key]) return fromMe[key];
    for (const [k, v] of Object.entries(fromMe)) {
      if (normPersonId(k) === key) return v;
    }
    return PERSON_ACCENTS_DEFAULT[key] || "var(--ink-3)";
  }

  function familyMembers() {
    const colors = (window.Me && window.Me.person_colors) || {};
    const names = new Map(
      ((window.D && window.D.family) || []).map(p => [normPersonId(p.who || p.id), p.name || p.who])
    );
    let ids = Object.keys(colors).filter(Boolean);
    if (!ids.length) ids = [...names.keys()].filter(Boolean);
    if (!ids.length) ids = Object.keys(PERSON_ACCENTS_DEFAULT);
    return ids.map(id => ({
      id: normPersonId(id),
      name: names.get(normPersonId(id)) || String(id).replace(/^./, c => c.toUpperCase()),
    }));
  }

  function setPersonFilter(id) {
    state.personFilter = id;
    sessionStorage.setItem("bernie-calendar-person", id);
    renderCalendar();
  }

  function isParentLike() {
    const role = ((window.Me && window.Me.role) || "").toLowerCase();
    // Match app.v6 isParent + singular "parent" used in some configs/tests.
    return role === "admin" || role === "parents" || role === "parent";
  }

  function isFamilyEvent(event) {
    return !(event.person_ids || []).length || event.color_key === "family";
  }

  function shouldDim(value) {
    if (state.personFilter === "all") return false;
    if (!value) return false;
    return normPersonId(value) !== normPersonId(state.personFilter);
  }

  /** Dim when filtered person is not among owners (family/shared always stay bright). */
  function shouldDimEvent(event) {
    if (state.personFilter === "all") return false;
    if (isFamilyEvent(event)) return false;
    const ids = (event.person_ids || []).map(normPersonId).filter(Boolean);
    if (!ids.length) return false;
    return !ids.includes(normPersonId(state.personFilter));
  }

  /** All-day GCal end is exclusive; timed events only on their start day. */
  function eventOnDay(event, key) {
    const start = String(event.start || "").slice(0, 10);
    if (!start) return false;
    if (!event.all_day) return start === key;
    let end = String(event.end || "").slice(0, 10);
    if (!end || end <= start) {
      const fallback = new Date(`${start}T12:00:00`);
      fallback.setDate(fallback.getDate() + 1);
      end = dayKey(fallback);
    }
    return start <= key && key < end;
  }

  function dayVisibleInAgenda(key) {
    const events = (state.data.events || []).filter(event => eventOnDay(event, key));
    const hasMeal = !!(mealFor(key) && mealFor(key).dish);
    const hasHw = schoolLayersVisible() && homeworkItems(key).length > 0;
    const hasChores = choresFor(key).length > 0;
    if (state.personFilter === "all") {
      return events.length > 0 || hasMeal || hasHw || hasChores;
    }
    const personHit = events.some(event => !shouldDimEvent(event));
    const hwHit = homeworkItems(key).some(item =>
      (item.person_ids || []).some(id => !shouldDim(id))
    );
    const choreHit = choresFor(key).some(task => !shouldDim(task.assigned_to || ""));
    return personHit || hasMeal || (schoolLayersVisible() && hwHit) || choreHit;
  }

  function homeworkDueCount() {
    if (!schoolLayersVisible()) return 0;
    const rows = state.data && state.data.homework_by_date;
    if (!rows) return 0;
    return Object.values(rows).reduce((n, items) => n + (items || []).length, 0);
  }

  function personDisplay(id) {
    const member = familyMembers().find(m => normPersonId(m.id) === normPersonId(id));
    return member ? member.name : (id ? String(id) : "Family");
  }

  function personInitial(id) {
    return personDisplay(id).slice(0, 1).toUpperCase();
  }

  function homeworkItems(key) {
    return ((state.data && state.data.homework_by_date) || {})[key] || [];
  }

  function homeworkSummary(key) {
    const items = homeworkItems(key);
    return items.length ? items.map(item => item.title).join(", ") : "None due";
  }

  function mealFor(key) {
    return (state.meals || []).find(meal => String(meal.date) === key);
  }

  function canMealEdit() {
    return isParentLike();
  }

  function canSeeAllChores() {
    return isParentLike();
  }

  function openChores() {
    const tasks = state.chores.length ? state.chores : ((window.D && window.D.tasks) || []);
    return tasks.filter(task => {
      if ((task.type || "chore") !== "chore") return false;
      const status = String(task.kanban_status || task.status || "").toLowerCase();
      if (status === "done" || status === "archived" || status === "complete") return false;
      if (task.completed_at) return false;
      return !!task.due_at;
    });
  }

  function choresFor(key) {
    return openChores().filter(task => String(task.due_at).slice(0, 10) === key);
  }

  async function openChoreTask(taskId) {
    if (window.showPanel) await window.showPanel("plan");
    if (window.openPlanTask) await window.openPlanTask(taskId);
  }

  async function saveDinner(key) {
    const dish = prompt("Dinner for this day:", mealFor(key)?.dish || "");
    if (dish === null) return;
    const trimmed = dish.trim();
    if (!trimmed) {
      await api(`/api/meals?date=${key}&meal_type=dinner`, { method: "DELETE" });
    } else {
      await api("/api/meals", {
        method: "PUT",
        body: { date: key, meal_type: "dinner", dish: trimmed },
      });
    }
    await loadRange({ quiet: true });
  }

  function weekStart(base = new Date()) {
    const date = new Date(base);
    date.setHours(0, 0, 0, 0);
    date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
    return date;
  }

  function eventStyle(event) {
    if (event.is_garbage) {
      return {
        background: "color-mix(in oklab, #22c55e 9%, var(--bg-card))",
        dot: "#22c55e",
      };
    }
    if (isFamilyEvent(event)) {
      return {
        background: "color-mix(in oklab, var(--ink-3) 10%, var(--bg-card))",
        dot: "var(--ink-3)",
      };
    }
    const accent = personAccent((event.person_ids || [])[0] || event.color_key);
    return {
      background: `linear-gradient(135deg, color-mix(in oklab, ${accent} 26%, var(--bg-card)), color-mix(in oklab, ${accent} 12%, var(--bg-card)))`,
      dot: accent,
    };
  }

  function canToggleSchool() {
    return canMealEdit();
  }

  function schoolLayersVisible() {
    return state.showSchool !== false;
  }

  function splitEvents(events) {
    const allDay = [];
    const timed = [];
    for (const event of events) {
      (event.all_day ? allDay : timed).push(event);
    }
    return { allDay, timed };
  }

  function formatEventWhen(event) {
    const start = new Date(event.start);
    const end = new Date(event.end);
    if (event.all_day) {
      const startLabel = start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
      const endInclusive = new Date(end);
      endInclusive.setDate(endInclusive.getDate() - 1);
      if (dayKey(start) === dayKey(endInclusive)) return `${startLabel} · All day`;
      return `${startLabel} – ${endInclusive.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} · All day`;
    }
    const sameDay = dayKey(start) === dayKey(end);
    const datePart = sameDay
      ? start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
      : `${start.toLocaleDateString([], { month: "short", day: "numeric" })} – ${end.toLocaleDateString([], { month: "short", day: "numeric" })}`;
    const timePart = `${start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} – ${end.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    return `${datePart} · ${timePart}`;
  }

  function rsvpLabel(status) {
    const map = {
      accepted: "Yes",
      tentative: "Maybe",
      declined: "No",
      needsaction: "Awaiting",
    };
    return map[String(status || "").toLowerCase()] || "Invited";
  }

  function closeEventModal() {
    const existing = document.getElementById("calendar-event-modal");
    if (existing) existing.remove();
    if (window._calendarModalEsc) {
      document.removeEventListener("keydown", window._calendarModalEsc);
      window._calendarModalEsc = null;
    }
  }

  function openEventModal(event) {
    closeEventModal();
    const people = (event.person_ids || []).map(id => personDisplay(id));
    const guestRows = (event.attendees || []).map(attendee => {
      const name = attendee.name || attendee.email || "Guest";
      return el("div", { class: "calendar-event-guest" },
        el("span", {}, name),
        el("span", { class: "calendar-event-rsvp" }, rsvpLabel(attendee.rsvp))
      );
    });
    const bodyRows = [
      el("div", { class: "calendar-event-modal-row" }, el("span", {}, "When"), el("strong", {}, formatEventWhen(event))),
      event.location && el("div", { class: "calendar-event-modal-row" }, el("span", {}, "Where"), el("strong", {}, event.location)),
      people.length && el("div", { class: "calendar-event-modal-row" }, el("span", {}, "Family"), el("strong", {}, people.join(", "))),
      event.organizer && el("div", { class: "calendar-event-modal-row" }, el("span", {}, "Organizer"), el("strong", {}, event.organizer)),
      guestRows.length && el("div", { class: "calendar-event-modal-row is-stack" },
        el("span", {}, "Guests"),
        el("div", { class: "calendar-event-guest-list" }, guestRows)
      ),
      event.description && el("div", { class: "calendar-event-modal-row is-stack" },
        el("span", {}, "Details"),
        el("div", { class: "calendar-event-description" }, event.description)
      ),
      event.status && event.status !== "confirmed" && el("div", { class: "calendar-event-modal-row" }, el("span", {}, "Status"), el("strong", {}, event.status)),
    ].filter(Boolean);
    const modal = el("div", {
      id: "calendar-event-modal",
      class: "calendar-event-modal open",
      onclick: e => { if (e.target === modal) closeEventModal(); },
    },
      el("div", { class: "calendar-event-modal-content", role: "dialog", "aria-modal": "true", "aria-labelledby": "calendar-event-title" },
        el("div", { class: "calendar-event-modal-header" },
          el("h3", { id: "calendar-event-title" }, event.title),
          el("button", {
            type: "button",
            class: "calendar-event-modal-close",
            "aria-label": "Close",
            onclick: () => closeEventModal(),
          }, "×")
        ),
        el("div", { class: "calendar-event-modal-body" }, bodyRows),
        el("div", { class: "calendar-event-modal-footer" },
          el("span", {}, "Read-only · Google Calendar"),
          event.html_link && el("a", {
            class: "calendar-event-open-link",
            href: event.html_link,
            target: "_blank",
            rel: "noopener noreferrer",
          }, "Open in Google Calendar")
        )
      )
    );
    document.body.append(modal);
    window._calendarModalEsc = e => { if (e.key === "Escape") closeEventModal(); };
    document.addEventListener("keydown", window._calendarModalEsc);
    const closeBtn = modal.querySelector(".calendar-event-modal-close");
    if (closeBtn) closeBtn.focus();
  }

  function nowMarker() {
    const now = new Date();
    return el("div", {
      class: "calendar-now-marker",
      id: "calendar-now-marker",
      "aria-label": `Current time ${now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`,
    },
      el("span", {}, "Now"),
      el("time", {}, now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }))
    );
  }

  function insertNowMarker(events) {
    const now = Date.now();
    const out = [];
    let placed = false;
    for (const event of events) {
      if (!placed && !event.all_day && new Date(event.start).getTime() > now) {
        out.push(nowMarker());
        placed = true;
      }
      out.push(eventCard(event));
    }
    if (!placed && state.view === "day" && dayKey(state.focus) === dayKey(new Date())) {
      out.push(nowMarker());
    }
    return out;
  }

  function eventCard(event) {
    const style = eventStyle(event);
    const dim = shouldDimEvent(event);
    const start = new Date(event.start);
    const end = new Date(event.end);
    const time = event.all_day
      ? "All day"
      : `${start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}–${end.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    const owner = (event.person_ids || [])[0];
    const badgeLabel = event.is_garbage ? "Trash" : (isFamilyEvent(event) ? "Family" : personDisplay(owner));
    const badge = el("span", {
      class: `calendar-person-badge${isFamilyEvent(event) ? " is-family" : ""}${event.is_garbage ? " is-garbage" : ""}`,
      style: isFamilyEvent(event) || event.is_garbage ? "" : `--badge-accent:${style.dot}`,
    }, badgeLabel);
    return el("article", {
      class: `calendar-event${event.is_garbage ? " is-garbage" : ""}${dim ? " is-dimmed" : ""}`,
      style: `--event-accent:${style.dot};--event-bg:${style.background}`,
      tabindex: "0",
      role: "button",
      onclick: () => openEventModal(event),
      onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openEventModal(event); } },
    },
      el("div", { class: "calendar-event-meta" },
        el("span", { class: "calendar-event-time" },
          el("span", {
            class: "calendar-person-dot",
            style: `background:${event.is_garbage ? "#22c55e" : style.dot}`,
            "aria-hidden": "true",
          }),
          el("time", {}, time)
        ),
        badge
      ),
      el("div", { class: "calendar-event-title" }, event.title),
      event.location && el("div", { class: "calendar-event-location" },
        el("span", { class: "calendar-event-loc-icon", "aria-hidden": "true" }, "⌖"),
        event.location
      )
    );
  }

  function homeworkRow(key) {
    if (!schoolLayersVisible()) return null;
    const items = homeworkItems(key);
    const summary = homeworkSummary(key);
    const people = [...new Set(items.flatMap(item => item.person_ids || []).filter(Boolean))];
    const lonePerson = people.length === 1 ? people[0] : null;
    const dimRow = items.length > 0
      && state.personFilter !== "all"
      && items.every(item => {
        const ids = item.person_ids || [];
        return ids.length > 0 && ids.every(id => shouldDim(id));
      });
    return el("div", { class: `calendar-homework${items.length ? "" : " is-clear"}${dimRow ? " is-dimmed" : ""}` },
      el("div", { class: "calendar-row-main" },
        el("span", { class: "calendar-row-icon", "aria-hidden": "true" }, "📚"),
        el("div", {},
          el("span", { class: "calendar-row-label" }, "Homework"),
          el("div", { class: "calendar-homework-summary" }, summary)
        )
      ),
      lonePerson && el("span", {
        class: `calendar-row-person${shouldDim(lonePerson) ? " is-dimmed" : ""}`,
        style: `--badge-accent:${personAccent(lonePerson)}`,
      }, personDisplay(lonePerson))
    );
  }

  function choreChip(task) {
    const assignee = task.assigned_to || "";
    const dim = shouldDim(assignee);
    return el("button", {
      type: "button",
      class: `calendar-chore-chip${dim ? " is-dimmed" : ""}`,
      style: `--chip-accent:${personAccent(assignee)}`,
      onclick: () => openChoreTask(task.id),
    }, task.title || "Chore");
  }

  function choreRow(key) {
    const chores = choresFor(key);
    if (!chores.length) return null;
    return el("div", { class: "calendar-chores" },
      el("span", { class: "calendar-row-label" }, "Chores"),
      el("div", { class: "calendar-chore-items" }, chores.map(choreChip))
    );
  }

  function dinnerRow(key, opts = {}) {
    const meal = mealFor(key);
    const dish = meal && meal.dish ? meal.dish : null;
    const label = opts.label || "Dinner";
    const editable = canMealEdit();
    const row = el(editable ? "button" : "div", {
      type: editable ? "button" : undefined,
      class: `calendar-dinner${dish ? "" : " is-tbd"}${editable ? " is-editable" : ""}`,
      title: editable ? "Tap to edit dinner" : undefined,
      onclick: editable ? () => saveDinner(key) : undefined,
    },
      el("div", { class: "calendar-row-main" },
        el("span", { class: "calendar-row-icon", "aria-hidden": "true" }, "🍽"),
        el("div", {},
          el("span", { class: "calendar-row-label" }, label),
          el("span", { class: "calendar-dinner-dish" }, dish || "TBD")
        )
      )
    );
    return row;
  }

  function dayFooter(key, opts = {}) {
    const rows = [
      choreRow(key),
      opts.skipHomework ? null : homeworkRow(key),
      opts.skipDinner ? null : dinnerRow(key),
    ].filter(Boolean);
    if (!rows.length) return null;
    return el("div", { class: "calendar-day-footer" }, rows);
  }

  function renderWeek(host) {
    const today = dayKey(new Date());
    const events = state.data.events || [];
    const grid = el("div", { class: "calendar-week", role: "grid", "aria-label": "Week calendar" });
    for (let offset = 0; offset < 7; offset += 1) {
      const date = new Date(state.start);
      date.setDate(date.getDate() + offset);
      const key = dayKey(date);
      const dayEvents = events
        .filter(event => eventOnDay(event, key))
        .sort((a, b) => String(a.start).localeCompare(String(b.start)));
      const { allDay, timed } = splitEvents(dayEvents);
      grid.append(el("section", {
        class: `calendar-day${key === today ? " is-today" : ""}`,
        role: "gridcell",
      },
        el("header", { class: "calendar-day-header" },
          el("div", { class: "calendar-day-header-top" },
            el("span", { class: "calendar-weekday" }, date.toLocaleDateString([], { weekday: "short" }).toUpperCase()),
            key === today && el("span", { class: "calendar-today-badge" }, "TODAY")
          ),
          el("strong", { class: "calendar-day-number" }, String(date.getDate())),
          uniformFor(key)
        ),
        allDay.length && el("div", { class: "calendar-allday-events" }, allDay.map(eventCard)),
        el("div", { class: "calendar-day-events" },
          timed.length
            ? timed.map(eventCard)
            : !allDay.length && el("span", { class: "calendar-empty" }, "No events scheduled")
        ),
        dayFooter(key)
      ));
    }
    host.replaceChildren(grid);
  }

  function eventsFor(key) {
    return (state.data.events || [])
      .filter(event => eventOnDay(event, key))
      .sort((a, b) => String(a.start).localeCompare(String(b.start)));
  }

  function uniformFor(key) {
    if (!schoolLayersVisible()) return null;
    const notes = (state.data.uniform_by_date || {})[key] || [];
    if (!notes.length) return null;
    return el("div", { class: "calendar-uniform" },
      el("span", { "aria-hidden": "true" }, "👕"),
      notes.map(note => el("span", {
        class: shouldDim(note.person_id) ? "is-dimmed" : "",
      }, `${note.text}${note.person_id ? ` (${personDisplay(note.person_id)})` : ""}`))
    );
  }

  function dayOverview(date) {
    const key = dayKey(date);
    const isToday = key === dayKey(new Date());
    return el("section", { class: "calendar-day-overview" },
      el("div", { class: "calendar-day-overview-main" },
        isToday && el("span", { class: "calendar-overview-kicker" }, "Today's Wall"),
        el("h2", {}, date.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" })),
        uniformFor(key)
      ),
      el("div", { class: "calendar-day-overview-side" },
        dinnerRow(key, { label: "Tonight's dinner" }),
        el("div", { class: "calendar-overview-card is-homework" },
          el("span", { class: "calendar-row-icon", "aria-hidden": "true" }, "📚"),
          el("div", {},
            el("span", { class: "calendar-row-label" }, "Homework row"),
            el("strong", {}, schoolLayersVisible() ? homeworkSummary(key) : "School hidden")
          )
        )
      )
    );
  }

  function daySection(date, events, className) {
    const key = dayKey(date);
    const cards = state.view === "day" && dayKey(date) === dayKey(new Date())
      ? insertNowMarker(events)
      : (events.length ? events.map(eventCard) : [el("span", { class: "calendar-empty" }, "No events scheduled")]);
    return el("section", { class: className },
      el("header", { class: "calendar-list-header" },
        el("div", {},
          el("span", {}, date.toLocaleDateString([], { weekday: "long" })),
          date.toDateString() === new Date().toDateString() && el("span", { class: "calendar-today-badge" }, "Today"),
          uniformFor(key)
        ),
        el("strong", {}, date.toLocaleDateString([], { month: "short", day: "numeric" }))
      ),
      el("div", { class: "calendar-list-events" }, cards),
      dayFooter(key)
    );
  }

  function renderDayTimeline(events) {
    const focusKey = dayKey(state.focus);
    const todayKey = dayKey(new Date());
    const isToday = focusKey === todayKey;
    const now = new Date();
    const hours = [];
    for (let hour = 7; hour <= 21; hour += 1) hours.push(hour);
    const grid = el("div", { class: "calendar-timeline-grid" },
      hours.map(hour => {
        const row = el("div", { class: "calendar-timeline-hour" },
          el("span", { class: "calendar-timeline-label" }, `${String(hour).padStart(2, "0")}:00`),
          el("div", { class: "calendar-timeline-line" })
        );
        if (isToday && now.getHours() === hour) {
          const topPct = (now.getMinutes() / 60) * 100;
          row.append(el("div", {
            class: "calendar-now-line",
            style: `top:${topPct}%`,
            "aria-label": `Current time ${now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`,
          },
            el("span", { class: "calendar-now-dot" }),
            el("span", { class: "calendar-now-bar" }),
            el("span", { class: "calendar-now-label" }, `${now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} NOW`)
          ));
        }
        return row;
      })
    );
    const cards = events.length
      ? (isToday ? insertNowMarker(events) : events.map(eventCard))
      : [el("span", { class: "calendar-empty" }, "No events scheduled")];
    return el("section", { class: "calendar-day-timeline" },
      el("header", { class: "calendar-timeline-header" },
        el("span", {}, "Timeline (07:00 – 21:00)"),
        isToday && el("span", { class: "calendar-timeline-now-kicker" },
          el("span", { class: "calendar-now-pulse", "aria-hidden": "true" }),
          `NOW · ${now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
        )
      ),
      grid,
      el("div", { class: "calendar-timeline-events" }, cards)
    );
  }

  function renderDay(host) {
    const focus = state.focus;
    const key = dayKey(focus);
    const events = eventsFor(key);
    const { allDay, timed } = splitEvents(events);
    const parts = [dayOverview(focus)];
    if (allDay.length) {
      parts.push(el("div", { class: "calendar-allday-events calendar-day-allday" }, allDay.map(eventCard)));
    }
    // Dinner is editable in the overview card; skip duplicate dinner strip here.
    parts.push(renderDayTimeline(timed), dayFooter(key, { skipDinner: true }));
    host.replaceChildren(...parts);
    const marker = document.getElementById("calendar-now-marker");
    if (marker) marker.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function renderAgenda(host) {
    const agenda = el("div", { class: "calendar-agenda" },
      el("header", { class: "calendar-agenda-header" },
        el("div", { class: "calendar-agenda-header-copy" },
          el("span", { class: "calendar-overview-kicker" }, "Chronological agenda"),
          el("h2", {},
            state.personFilter === "all"
              ? "Family master schedule"
              : `Schedule for ${personDisplay(state.personFilter)}`
          )
        ),
        el("div", { class: "calendar-agenda-range-wrap" },
          el("span", { id: "calendar-agenda-range", class: "calendar-agenda-range" }),
          el("span", { class: "calendar-agenda-range-sub" }, "7-day overview")
        )
      )
    );
    for (let offset = 0; offset < 7; offset += 1) {
      const date = new Date(state.start);
      date.setDate(date.getDate() + offset);
      const key = dayKey(date);
      if (!dayVisibleInAgenda(key)) continue;
      const events = eventsFor(key);
      const meal = mealFor(key);
      agenda.append(el("section", { class: `calendar-agenda-day${key === dayKey(new Date()) ? " is-today" : ""}` },
        el("header", { class: "calendar-list-header" },
          el("div", {},
            el("span", {}, date.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })),
            key === dayKey(new Date()) && el("span", { class: "calendar-today-badge" }, "Today"),
            uniformFor(key)
          ),
          el("div", { class: "calendar-agenda-badges" },
            homeworkItems(key).length > 0 && schoolLayersVisible() && el("span", { class: "calendar-agenda-badge is-hw" }, `HW: ${homeworkSummary(key)}`),
            el("span", { class: "calendar-agenda-badge is-dinner" }, `Dinner: ${meal?.dish || "TBD"}`)
          )
        ),
        el("div", { class: "calendar-list-events" },
          events.length
            ? events.map(eventCard)
            : el("span", { class: "calendar-empty" }, "No scheduled activities")
        ),
        dayFooter(key)
      ));
    }
    if (agenda.querySelectorAll(".calendar-agenda-day").length === 0) {
      agenda.append(el("p", { class: "calendar-status" }, "Nothing scheduled this week."));
    }
    host.replaceChildren(agenda);
  }

  function apiErrorMessage(error) {
    const detail = error && error.message;
    if (Array.isArray(detail)) {
      return detail.map(item => item.msg || item.message || JSON.stringify(item)).join("; ");
    }
    return detail || "Request failed";
  }

  async function toggleSchoolSchedule() {
    if (!canToggleSchool()) return;
    const next = !state.showSchool;
    try {
      const res = await api("/api/calendar/school-schedule", {
        method: "PATCH",
        body: { enabled: next },
      });
      state.showSchool = res.show_school !== false;
      await loadRange({ quiet: true });
    } catch (error) {
      if (error.status === 404) {
        alert("School toggle is not on the server yet — rebuild/restart bernie-api, then hard-refresh.");
        return;
      }
      alert(apiErrorMessage(error));
    }
  }

  function renderPersonFilter(root) {
    if (!root) return;
    const heading = root.querySelector(".calendar-heading");
    if (!heading) return;
    let wrap = document.getElementById("calendar-person-filter-wrap");
    if (!wrap) {
      wrap = el("div", { id: "calendar-person-filter-wrap", class: "calendar-person-filter-wrap" });
      heading.after(wrap);
    }
    let bar = document.getElementById("calendar-person-filter");
    if (!bar) {
      bar = el("div", {
        id: "calendar-person-filter",
        class: "calendar-person-filter",
        role: "group",
        "aria-label": "Filter by person",
      });
      wrap.append(bar);
    }
    const members = familyMembers();
    const chips = el("div", { class: "calendar-person-filter-chips" },
      el("button", {
        type: "button",
        class: `calendar-person-chip${state.personFilter === "all" ? " active" : ""}`,
        style: "--chip-accent: var(--amber)",
        "aria-pressed": String(state.personFilter === "all"),
        onclick: () => setPersonFilter("all"),
      },
        el("span", { class: "calendar-chip-dot", style: "background: var(--amber)" }),
        "All"
      ),
      ...members.map(member => el("button", {
        type: "button",
        class: `calendar-person-chip${state.personFilter === member.id ? " active" : ""}`,
        style: `--chip-accent:${personAccent(member.id)}`,
        "aria-pressed": String(state.personFilter === member.id),
        onclick: () => setPersonFilter(member.id),
      },
        el("span", { class: "calendar-chip-dot", style: `background:${personAccent(member.id)}` }),
        el("span", { class: "calendar-chip-name" }, member.name),
        el("span", { class: "calendar-chip-initial" }, personInitial(member.id))
      ))
    );
    const chipRow = el("div", { class: "calendar-person-filter-row" },
      el("span", { class: "calendar-person-filter-label" },
        el("span", { "aria-hidden": "true" }, "◎"),
        "People"
      ),
      chips
    );
    const layersRow = canToggleSchool() && el("div", {
      class: "calendar-layers-row",
      role: "group",
      "aria-label": "School schedule layers",
    },
      el("span", { class: "calendar-person-filter-label" }, "School"),
      el("button", {
        type: "button",
        class: `calendar-school-toggle${state.showSchool ? " is-on" : ""}`,
        "aria-pressed": String(!!state.showSchool),
        onclick: () => toggleSchoolSchedule(),
      }, state.showSchool ? "On · homework & uniform" : "Off · summer mode")
    );
    const hint = state.personFilter === "all"
      ? el("div", { class: "calendar-filter-hint is-all" },
          el("span", { class: "calendar-filter-hint-main" },
            el("span", { class: "calendar-filter-live-dot", "aria-hidden": "true" }),
            schoolLayersVisible()
              ? "All schedule layers active"
              : "School homework & uniform hidden"
          )
        )
      : el("div", { class: "calendar-filter-hint" },
          el("span", {}, "Highlighting ", el("strong", {}, personDisplay(state.personFilter)), " · others dimmed"),
          el("button", {
            type: "button",
            class: "calendar-person-reset",
            onclick: () => setPersonFilter("all"),
          }, "Reset")
        );
    bar.replaceChildren(
      el("div", { class: "calendar-person-filter-main" }, chipRow, hint),
      layersRow
    );
    bar.dataset.filter = state.personFilter;
  }

  function renderFooter(root) {
    if (!root) return;
    let footer = document.getElementById("calendar-footer");
    if (!footer) {
      footer = el("footer", { id: "calendar-footer", class: "calendar-footer" });
      root.append(footer);
    }
    const chores = typeof window.countMyOpenChores === "function" ? window.countMyOpenChores() : 0;
    const hwCount = homeworkDueCount();
    const range = rangeBounds();
    const startKey = dayKey(range.start);
    const endKey = dayKey(range.end);
    footer.replaceChildren(
      el("div", { class: "calendar-footer-left" },
        el("button", {
          type: "button",
          class: "calendar-footer-btn is-primary",
          onclick: () => window.showPanel && window.showPanel("plan"),
        },
          el("span", { class: "calendar-footer-ico", "aria-hidden": "true" }, "☰"),
          "Open Plan"
        ),
        chores > 0 && el("button", {
          type: "button",
          class: "calendar-footer-badge is-chore",
          onclick: () => window.showPanel && window.showPanel("plan"),
          title: "Open Plan chores",
        },
          el("span", { class: "calendar-footer-ico", "aria-hidden": "true" }, "☑"),
          `${chores} chore${chores === 1 ? "" : "s"}`
        ),
        hwCount > 0 && el("span", {
          class: "calendar-footer-badge calendar-footer-hw",
          title: "Homework due this range",
        },
          el("span", { class: "calendar-footer-ico", "aria-hidden": "true" }, "📚"),
          `${hwCount} HW due`
        ),
        el("button", {
          type: "button",
          class: "calendar-footer-btn",
          onclick: () => window.openGroceryPanel && window.openGroceryPanel(),
        }, "Groceries"),
        canMealEdit() && el("button", {
          type: "button",
          class: "calendar-footer-btn is-quiet",
          onclick: () => window.confirmSuggestGroceries && window.confirmSuggestGroceries(startKey, endKey),
        }, "Suggest groceries")
      ),
      el("div", { class: "calendar-footer-right" },
        el("span", { class: "calendar-footer-ico", "aria-hidden": "true" }, "💬"),
        el("span", {}, "Plan dinner in #furnace")
      )
    );
  }

  function renderHeadingSubtitle() {
    const sub = document.getElementById("calendar-subtitle");
    if (!sub) return;
    const range = rangeBounds();
    const today = new Date();
    const rangeText = range.start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
      + (dayKey(range.start) === dayKey(range.end)
        ? ""
        : ` – ${range.end.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}`);
    const todayKey = dayKey(today);
    const inRange = todayKey >= dayKey(range.start) && todayKey <= dayKey(range.end);
    sub.replaceChildren(
      el("span", {}, rangeText),
      inRange && el("span", { class: "calendar-subtitle-today" },
        " · ",
        today.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }),
        " (Today)"
      )
    );
  }

  function renderActive(host) {
    document.querySelectorAll("[data-calendar-view]").forEach(button => {
      const active = button.dataset.calendarView === state.view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const range = rangeBounds();
    const label = document.getElementById("calendar-range");
    if (label) {
      label.textContent = range.start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
        + (dayKey(range.start) === dayKey(range.end)
          ? ""
          : ` – ${range.end.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}`);
    }
    const shell = document.getElementById("panel-calendar")?.querySelector(".calendar-shell");
    if (shell) {
      renderPersonFilter(shell);
      renderFooter(shell);
    }
    renderHeadingSubtitle();
    const agendaRange = document.getElementById("calendar-agenda-range");
    if (agendaRange) {
      agendaRange.textContent = range.start.toLocaleDateString([], { month: "short", day: "numeric" })
        + " – "
        + range.end.toLocaleDateString([], { month: "short", day: "numeric" });
    }
    ({ day: renderDay, week: renderWeek, agenda: renderAgenda })[state.view](host);
  }

  function rangeBounds() {
    if (state.view === "day") {
      return { start: new Date(state.focus), end: new Date(state.focus) };
    }
    const start = weekStart(state.focus);
    const end = new Date(start);
    end.setDate(end.getDate() + 6);
    return { start, end };
  }

  async function loadRange(opts = {}) {
    const host = document.getElementById("calendar-view");
    const range = rangeBounds();
    state.start = weekStart(state.focus);
    state.loading = true;
    if (!opts.quiet) {
      host.replaceChildren(el("p", { class: "calendar-status" }, "Loading calendar…"));
    }
    try {
      const taskPath = canSeeAllChores() ? "/api/tasks?all_people=true" : "/api/tasks";
      [state.data, state.meals, state.chores] = await Promise.all([
        api(`/api/calendar?start=${dayKey(range.start)}&end=${dayKey(range.end)}`),
        api(`/api/meals?start=${dayKey(range.start)}&end=${dayKey(range.end)}&meal_type=dinner`).catch(() => []),
        api(taskPath).catch(() => []),
      ]);
      if (Array.isArray(state.chores)) {
        window.D = window.D || {};
        window.D.tasks = state.chores;
      }
      state.showSchool = state.data.show_school !== false;
      renderActive(host);
    } catch (error) {
      host.replaceChildren(el("p", { class: "calendar-status is-error" }, error.message || "Calendar unavailable"));
    } finally {
      state.loading = false;
    }
  }

  function movePeriod(direction) {
    state.focus.setDate(state.focus.getDate() + direction * (state.view === "day" ? 1 : 7));
    loadRange();
  }

  function viewButton(view) {
    return el("button", {
      type: "button",
      class: state.view === view ? "active" : "",
      "data-calendar-view": view,
      "aria-pressed": String(state.view === view),
      onclick: () => {
        state.view = view;
        sessionStorage.setItem("bernie-calendar-view", view);
        loadRange();
      },
    }, view[0].toUpperCase() + view.slice(1));
  }

  function startPoll() {
    stopPoll();
    state.pollTimer = setInterval(() => {
      if (window._activePanel !== "calendar") return;
      if (document.visibilityState !== "visible") return;
      loadRange({ quiet: true });
    }, POLL_MS);
  }

  function stopPoll() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
    closeEventModal();
  }

  async function renderCalendar() {
    const root = document.getElementById("panel-calendar");
    if (!root) return;
    if (!root.dataset.ready) {
      root.dataset.ready = "true";
      root.replaceChildren(el("section", { class: "calendar-shell", "aria-labelledby": "calendar-title" },
        el("header", { class: "calendar-heading" },
          el("div", { class: "calendar-heading-left" },
            el("div", { class: "calendar-heading-copy" },
              el("div", { class: "calendar-title-row" },
                el("h1", { id: "calendar-title" }, "Calendar"),
                el("span", { class: "calendar-wall-pill" }, "Family wall")
              ),
              el("p", { id: "calendar-subtitle", class: "calendar-subtitle" })
            ),
            el("div", { class: "calendar-navigation", role: "group", "aria-label": "Period navigation" },
              el("button", { type: "button", class: "calendar-nav-btn", "aria-label": "Previous period", onclick: () => movePeriod(-1) }, "‹"),
              el("span", { id: "calendar-range", class: "calendar-range-label" }),
              el("button", { type: "button", class: "calendar-nav-btn", "aria-label": "Next period", onclick: () => movePeriod(1) }, "›")
            )
          ),
          el("div", { class: "calendar-heading-right" },
            el("button", {
              type: "button",
              class: "calendar-today-jump",
              onclick: () => {
                state.focus = new Date();
                loadRange();
              },
            }, "Today"),
            el("button", {
              type: "button",
              class: "calendar-refresh-btn",
              "aria-label": "Refresh calendar",
              onclick: () => loadRange(),
            }, "↺"),
            el("div", { class: "calendar-view-switch", role: "group", "aria-label": "Calendar view" },
              ["day", "week", "agenda"].map(viewButton)
            )
          )
        ),
        el("div", { id: "calendar-view", class: "calendar-view-host", "aria-live": "polite" })
      ));
      startPoll();
    }
    if (root.dataset.ready) {
      startPoll();
      const host = document.getElementById("calendar-view");
      if (state.data) return renderActive(host);
      if (state.loading) return;
      await loadRange();
    }
  }

  window.renderCalendar = renderCalendar;
  window.v3CalendarLeave = stopPoll;
})();
