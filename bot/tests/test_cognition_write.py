"""40A-1: cognition inbound write HTTP + db_client round-trip."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import database as db
    from cognition_write import create_internal_write_app
    from db_client import cognition_db_write
    import db_client
except ModuleNotFoundError:
    db = None
    create_internal_write_app = None
    cognition_db_write = None


def _asgi_client(app):
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@unittest.skipUnless(db is not None, "database module not available")
class TestCognitionWriteAuth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "cognition_write_auth.db")
        await db.init_db()
        self.app = create_internal_write_app()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def _post(self, headers=None, json_body=None):
        async with _asgi_client(self.app) as client:
            return await client.post(
                "/internal/db/write",
                json=json_body
                or {
                    "op": "add_message",
                    "kwargs": {"channel_id": 1, "role": "user", "content": "hi"},
                },
                headers=headers or {},
            )

    async def test_missing_secret_returns_503(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("INTERNAL_POST_SECRET", None)
            resp = await self._post(headers={"X-Internal-Auth": "anything"})
        self.assertEqual(resp.status_code, 503)

    async def test_wrong_secret_returns_403(self):
        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "good-secret"}, clear=False):
            resp = await self._post(headers={"X-Internal-Auth": "bad-secret"})
        self.assertEqual(resp.status_code, 403)

    async def test_missing_header_returns_403(self):
        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "good-secret"}, clear=False):
            resp = await self._post(headers={})
        self.assertEqual(resp.status_code, 403)

    async def test_unknown_op_returns_400(self):
        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "good-secret"}, clear=False):
            resp = await self._post(
                headers={"X-Internal-Auth": "good-secret"},
                json_body={"op": "drop_all_tables", "kwargs": {}},
            )
        self.assertEqual(resp.status_code, 400)


@unittest.skipUnless(db is not None, "database module not available")
class TestCognitionWriteRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "cognition_write_rt.db")
        await db.init_db()
        self.app = create_internal_write_app()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_health_endpoint(self):
        async with _asgi_client(self.app) as client:
            resp = await client.get("/internal/db/health")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))

    async def test_authenticated_server_write_persists_row(self):
        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "good-secret"}, clear=False):
            async with _asgi_client(self.app) as client:
                resp = await client.post(
                    "/internal/db/write",
                    json={
                        "op": "add_message",
                        "kwargs": {
                            "channel_id": 4242,
                            "role": "user",
                            "content": "server path",
                        },
                    },
                    headers={"X-Internal-Auth": "good-secret"},
                )
        self.assertEqual(resp.status_code, 200)
        history = await db.get_history(4242)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "server path")

    async def test_discord_role_client_round_trip(self):
        """discord role → cognition write HTTP → row in DB."""

        class _FakeResponse:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            async def json(self):
                return self._body

            async def text(self):
                return str(self._body)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _FakeSession:
            def __init__(self, app, **_kwargs):
                self._app = app
                self.closed = False

            async def close(self):
                self.closed = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, url, json=None, data=None, headers=None, **kwargs):
                import json as _json
                body = json if json is not None else (_json.loads(data) if data else None)
                return _ForwardingPost(self._app, body, headers)

            def get(self, url, **kwargs):
                return _ForwardingGet()

        class _ForwardingGet:
            async def __aenter__(self):
                return _FakeResponse(200, {"ok": True})

            async def __aexit__(self, *args):
                return False

        class _ForwardingPost:
            def __init__(self, app, json_body, headers):
                self._app = app
                self._json = json_body
                self._headers = headers

            async def __aenter__(self):
                async with _asgi_client(self._app) as client:
                    resp = await client.post(
                        "/internal/db/write", json=self._json, headers=self._headers
                    )
                self._resp = resp
                return _FakeResponse(resp.status_code, resp.json())

            async def __aexit__(self, *args):
                return False

        with patch.dict(
            os.environ,
            {"INTERNAL_POST_SECRET": "test-secret", "ROLE": "discord"},
            clear=False,
        ):
            with patch("db_client.aiohttp.ClientSession", lambda **kw: _FakeSession(self.app, **kw)):
                await db_client.close_rpc_session()
                await cognition_db_write(
                    "add_message",
                    channel_id=4242,
                    role="user",
                    content="round-trip test",
                )

        history = await db.get_history(4242)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "round-trip test")

        await cognition_db_write(
            "store_draft",
            draft_id="draft_rpc_test",
            draft={
                "summary": "Dentist",
                "start": "2026-07-03T19:00:00-03:00",
                "end": "2026-07-03T20:00:00-03:00",
                "attendees": ["Dad"],
                "location": "",
                "description": "",
                "remind_minutes": None,
            },
        )
        draft = await db.get_draft("draft_rpc_test")
        self.assertIsNotNone(draft)
        self.assertEqual(draft["summary"], "Dentist")

    async def test_cognition_role_writes_locally_without_http(self):
        with patch.dict(os.environ, {"ROLE": "cognition"}, clear=False):
            await cognition_db_write(
                "add_message",
                channel_id=99,
                role="assistant",
                content="local write",
            )
        history = await db.get_history(99)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "local write")


class TestWriteOpsRegistry(unittest.TestCase):
    """Every WRITE_OPS entry must resolve to a database handler."""

    def test_write_ops_keys_match_database_handlers(self):
        from cognition_write import WRITE_OPS, _WRITE_OP_NAMES
        import database

        self.assertEqual(set(WRITE_OPS.keys()), set(_WRITE_OP_NAMES))
        for op, handler in WRITE_OPS.items():
            self.assertIs(
                handler,
                getattr(database, op),
                f"WRITE_OPS[{op!r}] must point at database.{op}",
            )

    def test_db_writes_routed_ops_are_allowlisted(self):
        import inspect
        import database

        from cognition_write import WRITE_OPS

        sig = inspect.signature(database.add_message)
        params = list(sig.parameters.keys())
        # routed() positional binding smoke
        self.assertIn("add_message", WRITE_OPS)
        self.assertEqual(params[0], "channel_id")


class TestDiscordWriteRoutingSourceGate(unittest.TestCase):
    """40A-3: bot.py must not call scoped writes via get_database() directly."""

    _ROUTED = (
        "add_message",
        "mark_reminder_sent",
        "set_person_pref",
        "save_rsvp",
        "store_message_mapping",
    )

    def test_bot_py_uses_db_writes_for_routed_ops(self):
        bot_path = os.path.join(os.path.dirname(__file__), "..", "bot.py")
        with open(bot_path, encoding="utf-8") as f:
            src = f.read()
        for op in self._ROUTED:
            self.assertNotIn(
                f"get_database().{op}",
                src,
                f"bot.py must route {op} through db_writes, not get_database()",
            )

    def test_network_devices_store_routed_not_local_write(self):
        """family-bot-dgz: discord must not write network_devices.json on RO /data."""
        from cognition_write import WRITE_OPS

        self.assertIn("save_network_devices_store", WRITE_OPS)
        bot_path = os.path.join(os.path.dirname(__file__), "..", "bot.py")
        with open(bot_path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('save_network_devices_store', src)
        # network_monitor_task must not persist via STORE.write_text
        self.assertNotIn("STORE.write_text", src)
        ns_path = os.path.join(os.path.dirname(__file__), "..", "network_service.py")
        with open(ns_path, encoding="utf-8") as f:
            ns = f.read()
        self.assertIn("save_network_devices_store", ns)
        self.assertIn("writes_locally()", ns)


class TestApiWriteRoutingSourceGate(unittest.TestCase):
    """40A-4: api.py must not call scoped writes via container.db directly."""

    _ROUTED = (
        "create_automation",
        "set_automation_active",
        "delete_automation",
        "create_chat_thread",
        "update_chat_thread_title",
        "delete_chat_thread",
        "add_chat_message",
        "update_presence",
        "delete_memory_event",
        "delete_memory_events_for_person",
    )

    def test_api_py_uses_db_writes_for_routed_ops(self):
        api_dir = os.path.join(os.path.dirname(__file__), "..", "api")
        src_parts = []
        for root, _, files in os.walk(api_dir):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        src_parts.append(f.read())
        src = "\n".join(src_parts)
        for op in self._ROUTED:
            self.assertNotIn(
                f"await db.{op}",
                src,
                f"api package must route {op} through db_writes",
            )


if __name__ == "__main__":
    unittest.main()
