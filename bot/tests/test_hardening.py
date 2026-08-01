"""Hardening regression suite (family-bot-1ov.2).

Prefer behavioral checks. Keep critical security string/AST guards
(CR-01 calendar await, CR-02 hv2-snap) — they catch real regressions.
"""
import unittest
import ast
import re
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tests.utils import _bot, _web

API_PATH = _bot('api')  # package (8lx.2)
V3_HOME_PATH = _web('static', 'js', 'v3_home.js')
APP_JS_PATH = _web('static', 'js', 'app.v6.js')
INDEX_HTML_PATH = _web('index.html')


class TestCR01AsyncCalendarInvocation(unittest.TestCase):
    """Regression guard for CR-01: async calendar methods must not be wrapped in run_in_executor."""

    @classmethod
    def setUpClass(cls):
        # family-bot-8lx.2: api is a package
        if API_PATH.is_dir():
            cls.api = "\n".join(p.read_text() for p in sorted(API_PATH.rglob("*.py")))
        else:
            with open(API_PATH) as f:
                cls.api = f.read()

    def test_no_run_in_executor_for_calendar(self):
        """run_in_executor must not be used to call calendar_service methods."""
        # Match run_in_executor( ... calendar_service on the same logical line
        matches = re.findall(r'run_in_executor\([^)]*calendar_service[^)]*\)', self.api)
        self.assertEqual(
            matches, [],
            f"run_in_executor wrapping calendar_service found — CR-01 regression: {matches}"
        )

    def test_todays_events_is_awaited(self):
        """get_todays_events() must be called with await, not passed as a callable."""
        self.assertIn('await calendar_service.get_todays_events()', self.api)

    def test_tomorrows_events_is_awaited(self):
        """get_tomorrows_events() must be called with await, not passed as a callable."""
        self.assertIn('await calendar_service.get_tomorrows_events()', self.api)

    def test_today_calendar_fetch_has_exception_handler(self):
        """get_todays_events() fetch must be wrapped in try/except so errors return partial data."""
        self.assertIn('Calendar fetch error (today)', self.api)

    def test_tomorrow_calendar_fetch_has_exception_handler(self):
        """get_tomorrows_events() fetch must be wrapped in try/except so errors return partial data."""
        self.assertIn('Calendar fetch error (tomorrow)', self.api)

    def test_highlights_build_has_exception_handler(self):
        """_build_formatted_highlights must be wrapped in try/except to avoid 500 on SSL errors."""
        self.assertIn('Highlights build failed', self.api)


class TestCR02RenderHomeGuard(unittest.TestCase):
    """Regression guard for CR-02: renderHome surgical-update guard must use .hv2-snap sentinel."""

    @classmethod
    def setUpClass(cls):
        with open(V3_HOME_PATH) as f:
            cls.js = f.read()

    def test_render_home_guard_uses_hv2_snap(self):
        """Early-return guard must check for .hv2-snap, not root.children.length."""
        self.assertIn("root.querySelector('.hv2-snap')", self.js,
                      "renderHome guard must use .hv2-snap to detect real content")

    def test_render_home_guard_not_using_children_length(self):
        """root.children.length must not be the early-return guard (truthy on skeleton divs)."""
        # The old broken guard — should not appear as the force check
        self.assertNotIn('!force && root.children.length', self.js,
                         "CR-02 regression: guard is using children.length instead of .hv2-snap")


class TestHttpxClientLifecycle(unittest.TestCase):
    """family-bot-1ov.2: behavioral client lifecycle (prefer import/call over source strings)."""

    def test_make_client_requires_key(self):
        from llm.clients import make_client
        from unittest.mock import patch
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                make_client()
            self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))

    def test_make_client_with_base_url_stashes_owned_httpx(self):
        from llm.clients import make_client
        from unittest.mock import patch
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            client = make_client(base_url="http://127.0.0.1:9")
            try:
                self.assertTrue(hasattr(client, "_owned_http_client"))
                self.assertIsNotNone(client._owned_http_client)
            finally:
                owned = getattr(client, "_owned_http_client", None)
                if owned is not None:
                    try:
                        import asyncio
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(owned.aclose())
                        finally:
                            loop.close()
                    except Exception:
                        pass

    def test_close_client_is_async(self):
        import inspect
        from llm.clients import close_client, llm_client_is_ephemeral
        self.assertTrue(inspect.iscoroutinefunction(close_client))
        self.assertTrue(callable(llm_client_is_ephemeral))

    def test_close_client_skips_container_singleton(self):
        """Shared container clients must not be closed (replaces string guard)."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        import asyncio
        from llm.clients import close_client

        shared = MagicMock()
        shared.close = AsyncMock()
        container = SimpleNamespace(anthropic=shared, litellm=None, openrouter=None)

        async def _run():
            await close_client(shared, container)
            shared.close.assert_not_awaited()

        asyncio.run(_run())

    def test_close_client_closes_ephemeral(self):
        from unittest.mock import AsyncMock, MagicMock
        import asyncio
        from llm.clients import close_client

        ephemeral = MagicMock()
        ephemeral.close = AsyncMock()
        owned = MagicMock()
        owned.aclose = AsyncMock()
        ephemeral._owned_http_client = owned

        async def _run():
            await close_client(ephemeral, container=None)
            ephemeral.close.assert_awaited_once()
            owned.aclose.assert_awaited_once()

        asyncio.run(_run())

    def test_llm_client_is_ephemeral_container_vs_owned(self):
        from types import SimpleNamespace
        from llm.clients import llm_client_is_ephemeral

        shared = object()
        container = SimpleNamespace(anthropic=shared, litellm=None, openrouter=None)
        self.assertFalse(llm_client_is_ephemeral(shared, container))
        self.assertTrue(llm_client_is_ephemeral(object(), container))
        self.assertTrue(llm_client_is_ephemeral(object(), None))

    def test_call_for_audit_uses_ephemeral_guard(self):
        """call_for_audit must consult llm_client_is_ephemeral before close."""
        import inspect
        from llm import audit
        src = inspect.getsource(audit.call_for_audit)
        self.assertIn("llm_client_is_ephemeral", src)
        self.assertIn("close_client", src)

    def test_facade_exports_client_helpers(self):
        import llm.clients as c
        self.assertTrue(hasattr(c, "make_client"))
        self.assertTrue(hasattr(c, "close_client"))
        self.assertTrue(hasattr(c, "llm_client_is_ephemeral"))

    def test_claude_init_wires_model_state_container(self):
        """claude_service._init must propagate container to llm.model_state."""
        from types import SimpleNamespace
        import claude_service
        from llm import model_state

        previous = getattr(claude_service, "_container", None)
        try:
            fake_client = SimpleNamespace(base_url="https://litellm.example.local")
            fake_container = SimpleNamespace(
                db=object(),
                llm_for=lambda _model: fake_client,
            )
            claude_service._init(fake_container)
            self.assertEqual(
                model_state._base_url_for_model("or-test"),
                "https://litellm.example.local",
            )
        finally:
            claude_service._container = previous
            model_state._init(previous)


class TestBernieNoteClosure(unittest.TestCase):
    """Regression guard for bernieNote closure bug — events/cond/temp must be captured by value."""

    @classmethod
    def setUpClass(cls):
        # family-bot-8lx.2: api is a package
        if API_PATH.is_dir():
            cls.api = "\n".join(p.read_text() for p in sorted(API_PATH.rglob("*.py")))
        else:
            with open(API_PATH) as f:
                cls.api = f.read()

    def test_refresh_note_captures_events_by_value(self):
        """_refresh_note must capture events as a default arg to avoid cross-request contamination."""
        self.assertRegex(self.api, r'async def _refresh_note\([^)]*events=events[^)]*\)',
                         "_refresh_note must have events=events in its signature")

    def test_refresh_note_captures_cond_by_value(self):
        """_refresh_note must capture cond as a default arg."""
        self.assertRegex(self.api, r'async def _refresh_note\([^)]*cond=cond[^)]*\)',
                         "_refresh_note must have cond=cond in its signature")

    def test_refresh_note_captures_temp_by_value(self):
        """_refresh_note must capture temp as a default arg."""
        self.assertRegex(self.api, r'async def _refresh_note\([^)]*temp=temp[^)]*\)',
                         "_refresh_note must have temp=temp in its signature")

    def test_refresh_note_uses_schedule_events_text(self):
        """bernieNote must pass real calendar text, not just an event count."""
        self.assertIn("calendar_service.events_to_text(events)", self.api)
        self.assertIn("live_context_override=note_live_context", self.api)

    def test_note_cache_key_includes_schedule_label(self):
        """Cache key must distinguish Today vs Tomorrow evening views."""
        self.assertIn('note_key = f"{now.strftime', self.api)
        self.assertIn("schedule_label.lower()", self.api)


class TestPhase23Hardening(unittest.TestCase):
    """Regression guards for Phase 23 reliability and security fixes."""

    @classmethod
    def setUpClass(cls):
        # family-bot-8lx.2: api is a package
        if API_PATH.is_dir():
            cls.api = "\n".join(p.read_text() for p in sorted(API_PATH.rglob("*.py")))
        else:
            with open(API_PATH) as f:
                cls.api = f.read()
        with open(_bot('nightly_digest.py')) as f:
            cls.digest = f.read()
        db_pkg = _bot('database')
        if db_pkg.is_dir():
            cls.db = "\n".join(
                p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(db_pkg.rglob("*.py"))
                if "__pycache__" not in p.parts
            )
        else:
            with open(_bot('database.py')) as f:
                cls.db = f.read()
        with open(_bot('ha_service.py')) as f:
            cls.ha = f.read()
        with open(_web('static', 'js', 'app.v6.js')) as f:
            cls.app_js = f.read()
        with open(_web('static', 'js', 'v3_logs.js')) as f:
            cls.logs_js = f.read()

    def test_ws_token_not_in_query_param(self):
        """WebSocket endpoints must not accept token via query param (proxy log exposure)."""
        self.assertNotIn('token: str = Query(None)', self.api,
                         "WS token must not be a query param — use first-message handshake")

    def test_ws_uses_first_message_auth(self):
        """Main /ws must validate token from first received message."""
        self.assertGreaterEqual(self.api.count('receive_json()'), 1,
                                "/ws must read token from first message")

    def test_app_js_ws_no_query_token(self):
        """/ws connection in app.v6.js must not embed token in URL."""
        self.assertNotIn('/ws?token=', self.app_js,
                         "app.v6.js must not pass WS token as query param")

    def test_app_js_markdown_images_no_query_token(self):
        """Markdown images must not append auth token to src URLs."""
        self.assertNotIn('token=" + encodeURIComponent', self.app_js)
        self.assertIn('data-bern-auth-src', self.app_js)

    def test_logs_uses_http_snapshot_not_ws(self):
        """Logs panel uses GET /api/logs (no live WS tail)."""
        self.assertIn('"/api/logs"', self.api)
        self.assertNotIn('"/ws/logs"', self.api)
        self.assertIn('/api/logs', self.logs_js)
        self.assertNotIn('ws/logs', self.logs_js)

    def test_fallback_model_checks_http_status(self):
        """_call_fallback_model must check HTTP status before parsing JSON.

        Accept either guard spelling — the current code uses
        `if resp.status == 200:` (parse on success); the original asserted the
        inverse `resp.status != 200`. Both satisfy the requirement."""
        self.assertTrue(
            'resp.status == 200' in self.digest or 'resp.status != 200' in self.digest,
            "_call_fallback_model must guard on resp.status before calling .json()")

    def test_prune_logs_function_exists(self):
        """database.py must have a prune_logs() function for log table maintenance."""
        self.assertIn('async def prune_logs(', self.db)

    def test_prune_logs_covers_activity_log(self):
        """prune_logs must delete from activity_log."""
        self.assertIn('activity_log', self.db[self.db.find('async def prune_logs('):])

    def test_prune_logs_covers_notification_log(self):
        """prune_logs must delete from notification_log."""
        self.assertIn('notification_log', self.db[self.db.find('async def prune_logs('):])

    def test_prune_logs_covers_token_usage(self):
        """prune_logs must delete from token_usage."""
        self.assertIn('token_usage', self.db[self.db.find('async def prune_logs('):])

    def test_db_indices_created(self):
        """init_db must create indices on timestamp columns for log tables."""
        self.assertIn('CREATE INDEX IF NOT EXISTS idx_activity_log_logged_at', self.db)
        self.assertIn('CREATE INDEX IF NOT EXISTS idx_notification_log_sent_at', self.db)
        self.assertIn('CREATE INDEX IF NOT EXISTS idx_token_usage_logged_at', self.db)

    def test_ha_timeout_under_10s(self):
        """HA refresh_entities request timeout must be under 10s to avoid blocking the dashboard."""
        import re
        # Isolate just the refresh_entities function body
        fn_start = self.ha.find('async def refresh_entities(')
        fn_end   = self.ha.find('\n    async def ', fn_start + 1)
        fn_body  = self.ha[fn_start:fn_end] if fn_end != -1 else self.ha[fn_start:]
        match = re.search(r'ClientTimeout\(total=(\d+)\)', fn_body)
        self.assertIsNotNone(match, "refresh_entities must use explicit ClientTimeout for its request")
        self.assertLess(int(match.group(1)), 10,
                        "refresh_entities request timeout must be < 10s to avoid blocking the dashboard")


class TestOpenWebUIChatLauncher(unittest.TestCase):
    """Regression guards for Chat tab launcher behavior after removing in-app chat UI."""

    @classmethod
    def setUpClass(cls):
        with open(APP_JS_PATH) as f:
            cls.app_js = f.read()
        with open(INDEX_HTML_PATH) as f:
            cls.index_html = f.read()

    def test_chat_click_opens_openwebui_new_tab(self):
        """Chat nav click must launch OpenWebUI in a new tab."""
        self.assertIn('const DEFAULT_OPENWEBUI_URL = "https://ai.lan/";', self.app_js)
        self.assertIn('function getOpenWebUIUrl()', self.app_js)
        self.assertIn('window.Me.openwebui_url', self.app_js)
        self.assertIn('if (id === "chat") {', self.app_js)
        self.assertIn('const url = getOpenWebUIUrl();', self.app_js)
        self.assertIn('window.open(url, "_blank", "noopener,noreferrer")', self.app_js)

    def test_chat_not_in_panel_routing(self):
        """In-app panel hotkeys/routing should not include a chat panel anymore."""
        self.assertIn("function getPanels()", self.app_js)
        self.assertNotIn('id: "chat"', self.app_js)
        self.assertNotIn("{ id: \"chat\"", self.app_js)

    def test_legacy_chat_bundle_removed(self):
        """Old chat frontend bundle should not be loaded."""
        self.assertNotIn('v3_chat.js', self.index_html)
        self.assertFalse(
            os.path.exists(_web('static', 'js', 'v3_chat.js')),
            "web/static/js/v3_chat.js should be removed",
        )


class TestProxyAwareLoginRateLimit(unittest.TestCase):
    """Regression guards for trusted-proxy-aware client IP resolution in login rate limiting."""

    @classmethod
    def setUpClass(cls):
        # family-bot-8lx.2: api is a package
        if API_PATH.is_dir():
            cls.api = "\n".join(p.read_text() for p in sorted(API_PATH.rglob("*.py")))
        else:
            with open(API_PATH) as f:
                cls.api = f.read()

    def test_trusted_proxy_cidrs_supported(self):
        """Rate limiting should support configured trusted proxy CIDRs."""
        self.assertTrue(
            'trusted_proxy_cidrs = config.get("trusted_proxy_cidrs"' in self.api
            or 'trusted_proxy_cidrs = _ac.config.get("trusted_proxy_cidrs"' in self.api,
            "login rate-limit must read trusted_proxy_cidrs from config",
        )
        self.assertIn('ipaddress.ip_network(cidr, strict=False)', self.api)

    def test_xff_only_used_for_trusted_proxies(self):
        """X-Forwarded-For must be ignored unless peer IP is trusted."""
        self.assertIn('if not _is_trusted_proxy_ip(peer_ip):', self.api)
        self.assertIn('forwarded_ip = forwarded.split(",")[0].strip()', self.api)


class TestPhase13TaskAutomationBackend(unittest.TestCase):
    """Regression guards for Phase 13 backend task/reminder/automation primitives."""

    @classmethod
    def setUpClass(cls):
        # 8lx.1: database is a package — join domain sources for schema string guards
        db_pkg = _bot('database')
        if db_pkg.is_dir():
            cls.db = "\n".join(
                p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(db_pkg.rglob("*.py"))
                if p.name != "__pycache__"
            )
        else:
            with open(_bot('database.py')) as f:
                cls.db = f.read()
        with open(_bot('bot.py')) as f:
            cls.bot_py = f.read()
        # Peel: slash command bodies live under bot/slash/ (not only bot.py)
        slash_root = _bot('slash')
        if slash_root.is_dir():
            slash_src = "\n".join(
                p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(slash_root.rglob("*.py"))
                if "__pycache__" not in p.parts
            )
            cls.bot_and_slash = cls.bot_py + "\n" + slash_src
        else:
            cls.bot_and_slash = cls.bot_py
        api_root = _bot('api')
        if api_root.is_dir():
            cls.api = "\n".join(p.read_text() for p in sorted(api_root.rglob("*.py")))
        else:
            with open(api_root) as f:
                cls.api = f.read()

    def test_tasks_schema_has_snooze_and_visibility(self):
        self.assertIn('snooze_until', self.db)
        self.assertIn('snooze_count', self.db)
        self.assertIn('requires_approval', self.db)
        self.assertIn('remind_visibility', self.db)

    def test_automations_schema_has_schedule_kind_and_payload(self):
        self.assertIn('schedule_kind', self.db)
        self.assertIn('schedule_payload', self.db)
        self.assertIn('audience_scope', self.db)
        self.assertIn('next_run_at', self.db)

    def test_discord_commands_exist_for_tasks_and_automations(self):
        for command_name in [
            'name="task_add"',
            'name="task_list"',
            'name="task_done"',
            'name="task_snooze"',
            'name="task_approve"',
            'name="automation_add"',
            'name="automation_list"',
            'name="automation_toggle"',
            'name="automation_delete"',
        ]:
            self.assertIn(command_name, self.bot_and_slash)

    def test_personal_scheduler_loop_exists(self):
        self.assertIn('async def personal_tasks_task()', self.bot_py)
        # Registration moved to jobs/bts_registration.py (peel from bot.py)
        bts_reg = _bot('jobs', 'bts_registration.py')
        reg_src = bts_reg.read_text(encoding="utf-8") if bts_reg.is_file() else self.bot_py
        self.assertTrue(
            'bts.register("personal_tasks", personal_tasks_task,' in self.bot_py
            or 'bts.register(\n        "personal_tasks",' in reg_src
            or '"personal_tasks"' in reg_src and 'personal_tasks_task' in reg_src,
            "personal_tasks must be registered on BTS (bot.py or jobs/bts_registration.py)",
        )

    def test_api_validates_automation_schedule_on_create(self):
        self.assertIn('computed_next_run = next_automation_run(schedule_kind, schedule_payload, tz_name)', self.api)
        self.assertIn('raise HTTPException(status_code=400, detail="Schedule does not produce a future run time")', self.api)


class TestFrontendVersionSync(unittest.TestCase):
    """Regression guard for cache-busting: all assets in index.html must share the same version."""

    def test_js_css_versions_synchronized(self):
        """All static assets in index.html must have matching ?v=X versions."""
        if not os.path.exists(INDEX_HTML_PATH):
            self.skipTest("index.html not found")

        with open(INDEX_HTML_PATH) as f:
            html = f.read()

        # Find all ?v=NUMBER patterns in src or href
        versions = re.findall(r'(?:src|href)="[^"]*\.[a-z0-9]+\?v=(\d+)"', html)
        
        if not versions:
            self.fail("No versioned assets found in index.html")

        first_v = versions[0]
        for v in versions:
            self.assertEqual(
                v, first_v,
                f"Version mismatch in index.html: expected v={first_v}, found v={v}. "
                "Ensure all static assets are bumped together to prevent caching issues."
            )

class TestArchitecturalHardeningBaseline(unittest.TestCase):
    """Phase 0 architectural baseline guards.

    These assert current violation counts do not increase. As each phase removes
    a category, lower the matching baseline until it reaches zero.
    """

    # Ratchet: lower only when violations decrease.
    # 8lx.1 package: domain modules live under database/ and are excluded from bypass count.
    RAW_DB_BYPASS_BASELINE = 2
    BOT_IMPORT_BASELINE = 0
    NOTIFICATION_IMPORT_BASELINE = 0
    TOOL_CONFIG_IMPORT_BASELINE = 3

    @classmethod
    def setUpClass(cls):
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.python_files = [
            p for p in cls.project_root.rglob('*.py')
            if '__pycache__' not in p.parts
        ]

    def _tree_for(self, path: Path):
        return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))

    def _is_test_file(self, path: Path) -> bool:
        return 'tests' in path.parts

    def test_raw_db_bypass_count_does_not_increase(self):
        """Raw database access must only decrease from the Phase 0 baseline."""
        count = 0
        for path in self.python_files:
            if path.name in {
                'database.py', 'db_binding.py', 'db_migrations.py', 'migrate_tasks_v32.py',
                'task_store.py', 'cognition_write.py', 'db_writes.py', 'main.py', 'bot.py',
            } or self._is_test_file(path):
                continue
            # 8lx.1: domain package is the legitimate write/read layer
            if 'database' in path.parts:
                continue
            tree = self._tree_for(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == 'database' for alias in node.names):
                        count += 1
                elif isinstance(node, ast.ImportFrom):
                    if node.module == 'database' or (node.module or '').startswith('database.'):
                        count += 1
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id in {'db_conn', '_db_conn'}:
                        count += 1
                    elif isinstance(func, ast.Attribute) and func.attr in {'db_conn', '_db_conn'}:
                        count += 1

        self.assertLessEqual(
            count,
            self.RAW_DB_BYPASS_BASELINE,
            f'Raw DB bypass count increased: {count} > {self.RAW_DB_BYPASS_BASELINE}',
        )

    def test_bot_private_import_count_does_not_increase(self):
        """Imports from bot.py singletons/private helpers must only decrease."""
        count = 0
        for path in self.python_files:
            if path.name == 'bot.py' or self._is_test_file(path):
                continue
            tree = self._tree_for(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == 'bot':
                    count += len(node.names)

        self.assertLessEqual(
            count,
            self.BOT_IMPORT_BASELINE,
            f'bot.py import count increased: {count} > {self.BOT_IMPORT_BASELINE}',
        )

    def test_notification_import_count_does_not_increase(self):
        """Direct Notification dataclass imports must only decrease."""
        count = 0
        for path in self.python_files:
            if path.name in {'notification_router.py'} or self._is_test_file(path):
                continue
            tree = self._tree_for(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == 'notification_router':
                    count += sum(1 for alias in node.names if alias.name == 'Notification')

        self.assertLessEqual(
            count,
            self.NOTIFICATION_IMPORT_BASELINE,
            f'Notification import count increased: {count} > {self.NOTIFICATION_IMPORT_BASELINE}',
        )

    def test_tool_config_import_count_does_not_increase(self):
        """Tool handlers should move from direct config imports to ctx.config."""
        count = 0
        # project_root is bot/ (parents[1] of this file), not repo root
        tools_dir = self.project_root / 'tools'
        self.assertTrue(tools_dir.is_dir(), f'expected bot/tools at {tools_dir}')
        for path in tools_dir.rglob('*.py'):
            if self._is_test_file(path):
                continue
            tree = self._tree_for(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == 'config':
                    count += sum(1 for alias in node.names if alias.name == 'config')

        self.assertLessEqual(
            count,
            self.TOOL_CONFIG_IMPORT_BASELINE,
            f'Tool config import count increased: {count} > {self.TOOL_CONFIG_IMPORT_BASELINE}',
        )


if __name__ == '__main__':
    unittest.main()

