import sys, os, asyncio
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import AsyncMock, MagicMock

from executor import ToolContext, ServiceRefs


def _make_ctx(shadow=False, group="all", config=None, ha=None):
    """Build a ToolContext with the HA service optionally injected as a mock.

    `group="all"` matches the role_required="all" on read tools, and is
    elevated above the per-tool require_permission gate via the Final
    Guard test below — for write tests we use `group="parents"` so the
    HA permission check passes, since require_permission reads from a
    real permission_groups map.
    """
    refs = ServiceRefs(ha=ha, tz=None)
    return ToolContext(
        config=config or {},
        person_id=None,
        group=group,
        channel_id=None,
        shadow=shadow,
        executor="native",
        services=refs,
    )


def test_control_device_blocked_in_shadow():
    import tools.home  # noqa: F401
    from tools import get_registry
    fn = get_registry()["control_device"]["fn"]
    result = asyncio.run(
        fn({"entity_id": "light.kitchen", "action": "turn_on"}, _make_ctx(shadow=True))
    )
    assert "shadow" in result.lower()


def test_get_home_state_read_runs_in_shadow():
    """Read tools execute in shadow mode. Inject mock via ctx.services.ha
    rather than monkeypatching sys.modules."""
    import tools.home  # noqa: F401
    from tools import get_registry

    fake_ha = MagicMock()
    fake_ha.get_all_states = AsyncMock(return_value=[
        {"entity_id": "light.kitchen", "state": "off"},
    ])

    fn = get_registry()["get_home_state"]["fn"]
    result = asyncio.run(fn({}, _make_ctx(shadow=True, ha=fake_ha)))
    fake_ha.get_all_states.assert_called_once()
    assert "light.kitchen" in result


def test_home_tools_registered():
    import tools.home  # noqa: F401
    from tools import get_registry

    expected = {
        "control_device",
        "set_light",
        "trigger_automation",
        "get_home_state",
        "get_home_health",
        "get_network_devices",
    }
    missing = expected - get_registry().keys()
    assert not missing, f"Missing home tools: {missing}"


def test_control_device_is_write():
    import tools.home  # noqa: F401
    from tools import get_registry
    assert get_registry()["control_device"]["is_write"] is True
    assert get_registry()["set_light"]["is_write"] is True
    assert get_registry()["trigger_automation"]["is_write"] is True


def test_get_home_state_is_read():
    import tools.home  # noqa: F401
    from tools import get_registry
    assert get_registry()["get_home_state"]["is_write"] is False


# ── Toggle "dad lamp" off + on (control_device end-to-end) ──────────────────
# The handler resolves the entity, dispatches turn_on/turn_off via ha_service,
# and returns a success string. We mock ha_service to verify both branches.

def _admin_config():
    """Config map with admin/parents granted 'common_areas' so the
    require_permission gate inside control_device's handler passes."""
    return {
        "permission_groups": {
            "admin":   {"common_areas": True},
            "parents": {"common_areas": True},
            "kids":    {"common_areas": False},
        }
    }


def test_control_device_turn_off_dad_lamp():
    """Bernie tells dad lamp to turn off — via control_device tool."""
    import tools.home  # noqa: F401
    from tools import get_registry

    fake_ha = MagicMock()
    # entity already fully-qualified, so resolve_entity_id is not invoked,
    # but the handler does call it when there's no dot — set defensively.
    fake_ha.resolve_entity_id = MagicMock(return_value="light.dad_lamp")
    fake_ha.turn_off = AsyncMock(return_value=True)
    fake_ha.turn_on = AsyncMock(return_value=True)
    fake_ha.toggle = AsyncMock(return_value=True)

    fn = get_registry()["control_device"]["fn"]
    ctx = _make_ctx(
        shadow=False,
        group="admin",
        config=_admin_config(),
        ha=fake_ha,
    )
    result = asyncio.run(
        fn({"entity_id": "light.dad_lamp", "action": "turn_off"}, ctx)
    )
    fake_ha.turn_off.assert_awaited_once_with("light.dad_lamp")
    fake_ha.turn_on.assert_not_awaited()
    assert "turn_off" in result
    assert "light.dad_lamp" in result
    assert "successfully" in result.lower()


def test_control_device_turn_on_dad_lamp():
    """Bernie tells dad lamp to turn on — via control_device tool."""
    import tools.home  # noqa: F401
    from tools import get_registry

    fake_ha = MagicMock()
    fake_ha.resolve_entity_id = MagicMock(return_value="light.dad_lamp")
    fake_ha.turn_off = AsyncMock(return_value=True)
    fake_ha.turn_on = AsyncMock(return_value=True)
    fake_ha.toggle = AsyncMock(return_value=True)

    fn = get_registry()["control_device"]["fn"]
    ctx = _make_ctx(
        shadow=False,
        group="admin",
        config=_admin_config(),
        ha=fake_ha,
    )
    result = asyncio.run(
        fn({"entity_id": "light.dad_lamp", "action": "turn_on"}, ctx)
    )
    fake_ha.turn_on.assert_awaited_once_with("light.dad_lamp")
    fake_ha.turn_off.assert_not_awaited()
    assert "turn_on" in result
    assert "light.dad_lamp" in result
    assert "successfully" in result.lower()


def test_control_device_friendly_name_resolves_to_entity_id():
    """Calling control_device with 'dad lamp' (no dot) hits resolve_entity_id."""
    import tools.home  # noqa: F401
    from tools import get_registry

    fake_ha = MagicMock()
    fake_ha.resolve_entity_id = MagicMock(return_value="light.dad_lamp")
    fake_ha.turn_on = AsyncMock(return_value=True)

    fn = get_registry()["control_device"]["fn"]
    ctx = _make_ctx(group="admin", config=_admin_config(), ha=fake_ha)
    result = asyncio.run(fn({"entity_id": "dad lamp", "action": "turn_on"}, ctx))

    fake_ha.resolve_entity_id.assert_called_once_with("dad lamp")
    fake_ha.turn_on.assert_awaited_once_with("light.dad_lamp")
    assert "successfully" in result.lower()


def test_control_device_unresolvable_friendly_name():
    """If resolve_entity_id returns None, control_device reports it can't find the device."""
    import tools.home  # noqa: F401
    from tools import get_registry

    fake_ha = MagicMock()
    fake_ha.resolve_entity_id = MagicMock(return_value=None)
    fake_ha.turn_on = AsyncMock(return_value=True)

    fn = get_registry()["control_device"]["fn"]
    ctx = _make_ctx(group="admin", config=_admin_config(), ha=fake_ha)
    result = asyncio.run(fn({"entity_id": "nonexistent gadget", "action": "turn_on"}, ctx))

    fake_ha.turn_on.assert_not_awaited()
    assert "could not find" in result.lower()
