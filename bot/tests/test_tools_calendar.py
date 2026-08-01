import sys, os, asyncio
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from executor import ToolContext, ServiceRefs


def _make_ctx(shadow=False, calendar=None):
    refs = ServiceRefs(
        calendar=calendar,
        ha=None, db=None, session=None,
        orchestrator=None, identity=None,
        tz=ZoneInfo("America/Halifax"),
    )
    return ToolContext(
        config={},
        person_id=None,
        group="family",
        channel_id=None,
        shadow=shadow,
        executor="native",
        services=refs,
    )


def test_get_todays_events_returns_text():
    import tools.calendar  # noqa: F401 — triggers registration
    from tools import get_registry

    cal = MagicMock()
    cal.get_todays_events = AsyncMock(return_value=[])
    cal.events_to_text = MagicMock(return_value="No events today")

    fn = get_registry()["get_todays_events"]["fn"]
    result = asyncio.run(fn({}, _make_ctx(calendar=cal)))
    assert result == "No events today"


def test_get_todays_events_no_calendar_service():
    import tools.calendar  # noqa: F401
    from tools import get_registry

    fn = get_registry()["get_todays_events"]["fn"]
    result = asyncio.run(fn({}, _make_ctx(calendar=None)))
    assert "not available" in result.lower()


def test_create_event_blocked_in_shadow():
    import tools.calendar  # noqa: F401
    from tools import get_registry

    fn = get_registry()["create_event"]["fn"]
    result = asyncio.run(
        fn(
            {"summary": "Test", "date": "2026-06-01", "time": "10:00"},
            _make_ctx(shadow=True, calendar=MagicMock()),
        )
    )
    assert "shadow" in result.lower()


def test_create_event_is_write():
    import tools.calendar  # noqa: F401
    from tools import get_registry

    assert get_registry()["create_event"]["is_write"] is True


def test_calendar_tools_registered():
    import tools.calendar  # noqa: F401
    from tools import get_registry

    reg = get_registry()
    expected = {
        "get_todays_events",
        "get_week_events",
        "get_month_events",
        "get_historical_events",
        "create_event",
        "get_events_range",
        "get_rsvps",
        "get_school_schedule",
        "get_homework",
        "get_highlights",
    }
    missing = expected - reg.keys()
    assert not missing, f"Missing calendar tools: {missing}"
