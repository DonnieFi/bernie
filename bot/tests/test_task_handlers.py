import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if "discord.ext.tasks" not in sys.modules:
    _discord_tasks = types.ModuleType("discord.ext.tasks")

    class _Loop:
        pass

    _discord_tasks.Loop = _Loop
    _discord_ext = types.ModuleType("discord.ext")
    _discord_ext.tasks = _discord_tasks
    sys.modules["discord"] = MagicMock()
    sys.modules["discord.ext"] = _discord_ext
    sys.modules["discord.ext.tasks"] = _discord_tasks

import worker  # noqa: F401 — registers handlers via cognitive_handlers.handlers
from cognitive_handlers.registry import HANDLERS, get_handler


class TestTaskHandlerRegistry(unittest.TestCase):
    def test_expected_handlers_registered(self):
        expected = {
            "discord_reply",
            "reflection",
            "consolidation",
            "study_guide",
            "study_guide_deliver",
            "research",
            "research_deliver",
        }
        self.assertTrue(expected.issubset(HANDLERS.keys()), msg=sorted(HANDLERS.keys()))

    def test_get_handler_returns_callable(self):
        fn = get_handler("reflection")
        self.assertIsNotNone(fn)
        self.assertTrue(callable(fn))

    def test_worker_reexports_registry(self):
        self.assertIs(worker._handlers, HANDLERS)
        self.assertIs(worker._call_ollama_topic, worker.worker_shared.call_ollama_topic)
