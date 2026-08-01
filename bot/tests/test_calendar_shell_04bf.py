"""family-bot-04bf.1: Calendar navigation and empty panel shell."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TestCalendarShell(unittest.TestCase):
    def test_calendar_panel_is_wired(self):
        app = (ROOT / "web/static/js/app.v6.js").read_text()
        index = (ROOT / "web/index.html").read_text()
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()

        self.assertIn('{ id: "calendar", label: "Calendar"', app)
        self.assertIn("calendar:  window.renderCalendar", app)
        self.assertIn("v3_calendar.css", index)
        self.assertIn("v3_calendar.js", index)
        self.assertIn('document.getElementById("panel-calendar")', calendar)
        self.assertNotIn("PlanDrawer", calendar)

    def test_week_vertical_slice_uses_range_api(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()

        self.assertIn("/api/calendar?start=", calendar)
        self.assertIn("for (let offset = 0; offset < 7;", calendar)
        self.assertIn("event.is_garbage", calendar)
        self.assertIn("grid-template-columns: repeat(7", css)
        self.assertNotIn("border-left", css)

    def test_day_week_agenda_switch_persists_for_session(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()

        self.assertIn("function renderDay(", calendar)
        self.assertIn("function renderWeek(", calendar)
        self.assertIn("function renderAgenda(", calendar)
        self.assertIn('sessionStorage.setItem("bernie-calendar-view"', calendar)
        self.assertIn('["day", "week", "agenda"].map(viewButton)', calendar)

    def test_uniform_notes_render_in_all_views(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()

        self.assertIn("function uniformFor(", calendar)
        self.assertIn("state.data.uniform_by_date", calendar)
        self.assertGreaterEqual(calendar.count("uniformFor("), 3)

    def test_period_navigation_refetches_calendar_and_meals(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()

        self.assertIn("function movePeriod(", calendar)
        self.assertIn('id: "calendar-range"', calendar)
        self.assertIn('}, "Today")', calendar)
        self.assertIn("/api/meals?start=", calendar)
        self.assertIn("Promise.all(", calendar)

    def test_person_filter_chips_dim_mode(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()

        self.assertIn('personFilter: savedPerson || "all"', calendar)
        self.assertIn("calendar-person-filter", calendar)
        self.assertIn("calendar-filter-hint", calendar)
        self.assertIn("calendar-chip-dot", calendar)
        self.assertIn("calendar-homework-summary", calendar)
        self.assertIn("calendar-person-badge", calendar)
        self.assertIn("calendar-day-overview", calendar)
        self.assertIn("Me.person_colors", calendar)
        self.assertIn("${accent} 26%", calendar)
        self.assertIn("var(--event-accent, var(--ink-3)) 48%", css)
        self.assertIn("v3_calendar.css?v=100", (ROOT / "web/index.html").read_text())
        self.assertIn("is-dimmed", css)
        self.assertIn("opacity: 0.3", css)

    def test_now_marker_poll_and_refresh(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()

        self.assertIn("calendar-now-marker", calendar)
        self.assertIn("5 * 60 * 1000", calendar)
        self.assertIn("Refresh calendar", calendar)
        self.assertIn("v3CalendarLeave", calendar)

    def test_dinner_homework_footer_rows(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()

        self.assertIn("homework_by_date", calendar)
        self.assertIn("calendar-dinner", css)
        self.assertIn("calendar-homework", css)
        self.assertIn("calendar-footer", css)
        self.assertIn("Open Plan", calendar)
        self.assertIn('showPanel("plan")', calendar)
        self.assertIn("countMyOpenChores", calendar)

    def test_event_detail_modal(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()

        self.assertIn("openEventModal", calendar)
        self.assertIn("Read-only · Google Calendar", calendar)
        self.assertIn("Open in Google Calendar", calendar)
        self.assertIn("calendar-event-description", calendar)
        self.assertNotIn("Discord #calendar", calendar)

    def test_school_schedule_toggle(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()

        self.assertIn("calendar-school-toggle", calendar)
        self.assertIn("calendar-layers-row", calendar)
        self.assertIn("PERSON_ACCENTS_DEFAULT", calendar)
        self.assertIn("bernie-calendar-person", calendar)
        self.assertIn("/api/calendar/school-schedule", calendar)
        self.assertIn("show_school", calendar)
        self.assertIn("calendar-school-toggle", css)

    def test_day_timeline_and_subtitle(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()

        self.assertIn("renderDayTimeline", calendar)
        self.assertIn("calendar-day-timeline", css)
        self.assertIn("calendar-subtitle", calendar)
        self.assertIn("calendar-allday-events", calendar)

    def test_mobile_touch_targets(self):
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()

        self.assertIn("min-width: 760px", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("calendar-person-filter", css)

    def test_calendar_chores_and_editable_dinner(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        tasks = (ROOT / "web/static/js/v3_tasks.js").read_text()
        grocery = (ROOT / "web/static/js/v3_grocery.js").read_text()
        index = (ROOT / "web/index.html").read_text()

        self.assertIn("calendar-chore-chip", calendar)
        self.assertIn("openChoreTask", calendar)
        self.assertIn("openPlanTask", tasks)
        self.assertIn("saveDinner", calendar)
        self.assertIn("openGroceryPanel", grocery)
        self.assertIn("v3_grocery.js", index)

    def test_multi_day_and_filter_helpers(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()
        meals = (Path(__file__).resolve().parents[1] / "api/routes/meals.py").read_text()

        self.assertIn("function eventOnDay(", calendar)
        self.assertIn("function shouldDimEvent(", calendar)
        self.assertIn("closeEventModal", calendar)
        self.assertIn('if (e.key === "Escape")', calendar)
        self.assertIn("personDisplay(note.person_id)", calendar)
        self.assertIn("calendar-day-allday", calendar)
        self.assertIn("min-height: 44px", css)
        self.assertIn('"parent"', meals)
        # Agenda keeps days that only have dinner/HW/chores
        self.assertIn("hasMeal", calendar)
        self.assertIn("hasHw", calendar)
        self.assertIn("meal_type=dinner`).catch(() => [])", calendar)

    def test_ui_chrome_skylight_layout(self):
        calendar = (ROOT / "web/static/js/v3_calendar.js").read_text()
        css = (ROOT / "web/static/css/v3_calendar.css").read_text()
        app_css = (ROOT / "web/static/css/v3.css").read_text()

        self.assertIn("calendar-heading-left", calendar)
        self.assertIn("calendar-heading-right", calendar)
        self.assertIn("calendar-wall-pill", calendar)
        self.assertIn("calendar-today-jump", calendar)
        self.assertIn("calendar-person-filter-main", calendar)
        self.assertIn("font-family: var(--font-serif)", css)
        self.assertIn("calendar-view-switch button.active", css)
        self.assertNotIn("var(--font-display)", css)
        self.assertNotIn("border-left", css)
        self.assertIn(".page {\n  max-width: 920px;", app_css)


if __name__ == "__main__":
    unittest.main()
