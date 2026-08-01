"""Tests for BackgroundTaskScheduler."""
import asyncio
import sys
import os
import unittest
from unittest.mock import MagicMock

# ── sys.path so 'bot/' modules resolve ───────────────────────────────────────
_HERE = os.path.dirname(__file__)
_BOT_DIR = os.path.abspath(os.path.join(_HERE, ".."))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

# ── Mock heavy deps before importing project modules ─────────────────────────
_MOCK_MODULES = [
    "discord", "discord.ext", "discord.ext.commands",
    "discord.ext.tasks",
    "anthropic", "aiohttp", "pytz",
    "googleapiclient", "googleapiclient.discovery",
    "google.oauth2", "google.auth.transport.requests",
    "google.oauth2.credentials", "google_auth_oauthlib.flow",
    "croniter", "websockets",
]
for _mod in _MOCK_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# discord.ext.tasks.loop must return a callable that produces a Loop-like obj
import discord.ext.tasks as _tasks_mock  # noqa: E402 — already mocked above

_fake_loop_obj = MagicMock()
_fake_loop_obj.__name__ = "wrapped"

def _fake_loop(**kwargs):
    def decorator(func):
        obj = MagicMock()
        obj._func = func
        obj._interval = kwargs
        obj.__name__ = func.__name__
        return obj
    return decorator

_tasks_mock.loop = _fake_loop

# `from discord.ext import tasks` in background_scheduler.py binds its own
# `_discord_tasks` to an auto-child of the discord.ext mock — a separate
# object from `_tasks_mock`. Rebind it post-import so _fake_loop is honoured.
import background_scheduler  # noqa: E402
background_scheduler._discord_tasks = _tasks_mock

from background_scheduler import BackgroundTaskScheduler, init_scheduler, get_scheduler  # noqa: E402


def _make_supervisor():
    sv = MagicMock()
    sv.register = MagicMock()
    sv.update_health = MagicMock()
    sv.get_status = MagicMock(return_value={
        "started": True,
        "tasks": {"foo": {"status": "running", "last_run": None, "errors": 0}},
    })
    return sv


class TestBTSRegister(unittest.TestCase):
    def setUp(self):
        self.sv = _make_supervisor()
        self.bts = BackgroundTaskScheduler(self.sv)

    def test_register_stores_meta(self):
        async def my_task():
            pass
        self.bts.register("foo", my_task, interval={"seconds": 60})
        self.assertIn("foo", self.bts._meta)
        self.assertEqual(self.bts._meta["foo"]["interval"], {"seconds": 60})
        self.assertEqual(self.bts._meta["foo"]["owner"], "discord")
        self.assertEqual(self.bts._meta["foo"]["tier"], "immediate")
        self.assertEqual(self.bts._meta["foo"]["restart_policy"], "always")

    def test_register_calls_sv_register(self):
        async def my_task():
            pass
        loop = self.bts.register("foo", my_task, interval={"minutes": 5})
        self.sv.register.assert_called_once_with("foo", loop)

    def test_register_returns_loop(self):
        async def my_task():
            pass
        loop = self.bts.register("foo", my_task, interval={"minutes": 5})
        self.assertIsNotNone(loop)

    def test_custom_owner_tier(self):
        async def my_task():
            pass
        self.bts.register(
            "bar", my_task, interval={"seconds": 10},
            owner="cognition", tier="overnight-only", restart_policy="on-failure",
        )
        self.assertEqual(self.bts._meta["bar"]["owner"], "cognition")
        self.assertEqual(self.bts._meta["bar"]["tier"], "overnight-only")
        self.assertEqual(self.bts._meta["bar"]["restart_policy"], "on-failure")

    def test_defaults_are_applied(self):
        async def some_task():
            pass
        self.bts.register("baz", some_task, interval={"minutes": 10})
        self.assertEqual(self.bts._meta["baz"]["owner"], "discord")
        self.assertEqual(self.bts._meta["baz"]["tier"], "immediate")
        self.assertEqual(self.bts._meta["baz"]["restart_policy"], "always")


class TestBTSGetStatus(unittest.TestCase):
    def test_get_status_merges_meta(self):
        sv = _make_supervisor()
        bts = BackgroundTaskScheduler(sv)
        bts._meta["foo"] = {
            "interval": {"minutes": 5},
            "owner": "discord",
            "tier": "immediate",
            "restart_policy": "always",
        }
        status = bts.get_status()
        self.assertTrue(status["started"])
        self.assertIn("foo", status["tasks"])
        self.assertEqual(status["tasks"]["foo"]["owner"], "discord")
        self.assertEqual(status["tasks"]["foo"]["tier"], "immediate")
        self.assertEqual(status["tasks"]["foo"]["status"], "running")

    def test_get_status_task_only_in_meta_not_merged(self):
        sv = _make_supervisor()
        sv.get_status.return_value = {
            "started": True,
            "tasks": {"only_in_sv": {"status": "running"}},
        }
        bts = BackgroundTaskScheduler(sv)
        # meta has "foo" but sv doesn't — should not crash, just skip
        bts._meta["foo"] = {
            "interval": {}, "owner": "discord", "tier": "immediate",
            "restart_policy": "always",
        }
        status = bts.get_status()
        self.assertNotIn("foo", status["tasks"])
        self.assertIn("only_in_sv", status["tasks"])


class TestBTSWrapperHealthCalls(unittest.TestCase):
    """The wrapper that bts.register creates is the contract surface for
    supervisor.update_health. Two properties matter:
      1. Success path calls update_health(name) exactly once, no error kwarg.
      2. A bug inside the success-path update_health call must NOT be
         re-classified as a task failure (otherwise a bug in the supervisor
         would trigger a spurious restart of a healthy task)."""

    def _wrapped_from(self, bts, name, func):
        loop = bts.register(name, func, interval={"seconds": 10})
        return loop._func  # _fake_loop stashes the wrapped coroutine here

    def test_success_calls_update_health_without_error(self):
        sv = _make_supervisor()
        bts = BackgroundTaskScheduler(sv)
        async def ok_task():
            pass
        wrapped = self._wrapped_from(bts, "ok", ok_task)
        asyncio.run(wrapped())
        sv.update_health.assert_called_once_with("ok")

    def test_failure_calls_update_health_with_error(self):
        sv = _make_supervisor()
        bts = BackgroundTaskScheduler(sv)
        async def bad_task():
            raise ValueError("boom")
        wrapped = self._wrapped_from(bts, "bad", bad_task)
        asyncio.run(wrapped())  # wrapper swallows
        sv.update_health.assert_called_once()
        call = sv.update_health.call_args
        self.assertEqual(call.args[0], "bad")
        self.assertIsInstance(call.kwargs.get("error"), ValueError)

    def test_success_path_update_health_bug_not_misattributed(self):
        """If update_health raises on the success path, the wrapper must NOT
        also call update_health(name, error=...) — that would mark a healthy
        task as failed and trigger a restart cascade."""
        sv = _make_supervisor()
        sv.update_health.side_effect = RuntimeError("supervisor bug")
        bts = BackgroundTaskScheduler(sv)
        async def ok_task():
            pass
        wrapped = self._wrapped_from(bts, "ok", ok_task)
        with self.assertRaises(RuntimeError):
            asyncio.run(wrapped())
        # Exactly one call, with no error kwarg — the success call that raised.
        self.assertEqual(sv.update_health.call_count, 1)
        for call in sv.update_health.call_args_list:
            self.assertNotIn("error", call.kwargs)


class TestBTSCognitionOwnerSurvives(unittest.TestCase):
    """Wave 2b's --role filter will consume bts._meta[name]['owner']. Make
    sure the value the caller passes survives all the way through to
    get_status."""

    def test_cognition_owner_appears_in_get_status(self):
        sv = _make_supervisor()
        sv.get_status.return_value = {
            "started": True,
            "tasks": {"nightly_eval": {"status": "running"}},
        }
        bts = BackgroundTaskScheduler(sv)
        async def t():
            pass
        bts.register(
            "nightly_eval", t,
            interval={"time": "02:30"},
            owner="cognition", tier="overnight-only",
        )
        status = bts.get_status()
        self.assertEqual(status["tasks"]["nightly_eval"]["owner"], "cognition")
        self.assertEqual(status["tasks"]["nightly_eval"]["tier"], "overnight-only")


class TestBTSChangeInterval(unittest.TestCase):
    def test_change_interval_updates_loop_and_meta(self):
        sv = _make_supervisor()
        loop = MagicMock()
        sv.tasks = {"daily_summary": loop}
        bts = BackgroundTaskScheduler(sv)
        bts._meta["daily_summary"] = {
            "interval": {"time": "old"},
            "owner": "discord",
            "tier": "immediate",
            "restart_policy": "always",
        }
        bts.change_interval("daily_summary", time="09:30")
        loop.change_interval.assert_called_once_with(time="09:30")
        self.assertEqual(bts._meta["daily_summary"]["interval"], {"time": "09:30"})

    def test_change_interval_unknown_task_raises(self):
        sv = _make_supervisor()
        sv.tasks = {}
        bts = BackgroundTaskScheduler(sv)
        with self.assertRaises(KeyError):
            bts.change_interval("missing", minutes=5)

    def test_sync_intervals_reschedules_changed_tasks(self):
        sv = _make_supervisor()
        loops = {name: MagicMock() for name in (
            "daily_summary", "weekly_summary", "reminders", "network_watchman",
        )}
        sv.tasks = loops
        bts = BackgroundTaskScheduler(sv)
        for name in loops:
            bts._meta[name] = {"interval": {}, "owner": "discord", "tier": "immediate", "restart_policy": "always"}

        prior = {
            "summary_hour": 7,
            "summary_minute": 0,
            "weekly_summary_hour": 20,
            "weekly_summary_minute": 0,
            "poll_interval_minutes": 5,
            "network_watchman_poll_minutes": 15,
        }
        with unittest.mock.patch.dict(
            "sys.modules",
            {},
            clear=False,
        ):
            import config as config_mod
            with unittest.mock.patch.object(config_mod, "config", {
                "summary_hour": 8,
                "summary_minute": 0,
                "weekly_summary_hour": 20,
                "weekly_summary_minute": 0,
                "poll_interval_minutes": 5,
                "network_watchman": {"poll_interval_minutes": 15},
                "timezone": "America/Halifax",
            }):
                updated = bts.sync_intervals_from_config(prior)
        self.assertEqual(updated, ["daily_summary"])
        loops["daily_summary"].change_interval.assert_called_once()


class TestBTSOwnerFilter(unittest.TestCase):
    """40A-2: start_all respects ROLE vs task owner metadata."""

    def test_discord_role_skips_cognition_tasks(self):
        sv = _make_supervisor()
        sv.start_all = unittest.mock.AsyncMock()
        sv.tasks = {"reminders": MagicMock(), "nightly_eval": MagicMock()}

        bts = BackgroundTaskScheduler(sv)
        bts._meta["reminders"] = {"owner": "discord", "tier": "immediate", "interval": {}, "restart_policy": "always"}
        bts._meta["nightly_eval"] = {"owner": "cognition", "tier": "overnight-only", "interval": {}, "restart_policy": "always"}

        with unittest.mock.patch.dict(os.environ, {"ROLE": "discord"}, clear=False):
            asyncio.run(bts.start_all())

        sv.start_all.assert_awaited_once()
        only = sv.start_all.call_args.kwargs.get("only_names")
        self.assertEqual(only, {"reminders"})

    def test_cognition_role_starts_cognition_tasks_only(self):
        sv = _make_supervisor()
        sv.start_all = unittest.mock.AsyncMock()
        sv.tasks = {"reminders": MagicMock(), "nightly_eval": MagicMock()}

        bts = BackgroundTaskScheduler(sv)
        bts._meta["reminders"] = {"owner": "discord", "tier": "immediate", "interval": {}, "restart_policy": "always"}
        bts._meta["nightly_eval"] = {"owner": "cognition", "tier": "overnight-only", "interval": {}, "restart_policy": "always"}

        with unittest.mock.patch.dict(os.environ, {"ROLE": "cognition"}, clear=False):
            asyncio.run(bts.start_all())

        only = sv.start_all.call_args.kwargs.get("only_names")
        self.assertEqual(only, {"nightly_eval"})

    def test_supervisor_smoke_discord_filter_skips_cognition_loop_start(self):
        """Integration-style: real supervisor honors only_names — cognition loop never starts."""
        from supervisor import TaskSupervisor

        bot = MagicMock()
        sv = TaskSupervisor(bot)
        discord_loop = MagicMock()
        discord_loop.is_running.return_value = False
        cognition_loop = MagicMock()
        cognition_loop.is_running.return_value = False
        sv.tasks = {"reminders": discord_loop, "nightly_eval": cognition_loop}
        sv._health = {
            "reminders": {"status": "registered", "run_count": 0, "errors": 0},
            "nightly_eval": {"status": "registered", "run_count": 0, "errors": 0},
        }
        sv._restart_counts = {"reminders": 0, "nightly_eval": 0}

        asyncio.run(sv.start_all(only_names={"reminders"}))

        discord_loop.start.assert_called_once()
        cognition_loop.start.assert_not_called()


class TestBTSSingleton(unittest.TestCase):
    def test_init_and_get(self):
        sv = _make_supervisor()
        bts = init_scheduler(sv)
        self.assertIs(get_scheduler(), bts)

    def test_get_before_init_raises(self):
        import background_scheduler as bs
        original = bs._scheduler
        bs._scheduler = None
        try:
            with self.assertRaises(RuntimeError):
                get_scheduler()
        finally:
            bs._scheduler = original


if __name__ == "__main__":
    unittest.main()
