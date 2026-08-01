import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from executor import ExecutorConfig, ServiceRefs, ToolContext


class TestExecutor(unittest.TestCase):
    def test_executor_config_defaults(self):
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6")
        self.assertFalse(cfg.shadow)
        self.assertEqual(cfg.group, "family")
        self.assertIsNone(cfg.person_id)

    def test_tool_context_fields(self):
        refs = ServiceRefs(
            calendar=None, ha=None, db=None, session=None,
            orchestrator=None, identity=None, tz=None,
        )
        ctx = ToolContext(
            config={},
            person_id="person.red",
            group="admin",
            channel_id="111111111111111111",
            shadow=False,
            executor="native",
            services=refs,
        )
        self.assertEqual(ctx.group, "admin")
        self.assertFalse(ctx.shadow)
