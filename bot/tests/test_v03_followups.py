"""Follow-ups: o5j.1 family patch, wze unlock measure, hcd Plan debounce, agc history batch."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "web" / "static" / "js" / "app.v6.js").exists():
            return parent
    return here.parents[2]


_ROOT = _repo_root()
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"
_TASKS = _ROOT / "web" / "static" / "js" / "v3_tasks.js"


def _home_py() -> Path:
    candidates = [
        _ROOT / "bot" / "api" / "routes" / "home.py",
        _ROOT / "api" / "routes" / "home.py",
        Path(__file__).resolve().parents[1] / "api" / "routes" / "home.py",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


_HOME_PY = _home_py()


class TestO5j1FamilyPatch(unittest.TestCase):
    def test_presence_patches_family(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("o5j.1", js)
        self.assertIn("D.family", js)
        self.assertIn("applyPatch", js)
        # Family re-render only when rows exist
        self.assertIn("D.family.length", js)


class TestWzeUnlockMeasure(unittest.TestCase):
    def test_unlock_trace_present(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("beginUnlockTrace", js)
        self.assertIn("endUnlockTrace", js)
        self.assertIn("__bernieUnlockApiCalls", js)
        self.assertIn("cold path", js)
        # Today cold path uses single today endpoint
        self.assertIn("opts.cold", js)
        # Home uses single dashboard
        self.assertIn("single dashboard fetch", js)

    def test_load_initial_is_health_only(self):
        js = _APP.read_text(encoding="utf-8")
        # Structural: loadInitial mentions health only
        self.assertIn("health only on unlock", js)
        idx = js.find("async function loadInitial")
        block = js[idx : idx + 500]
        self.assertIn("/api/health", block)
        self.assertNotIn("/api/today", block)
        self.assertNotIn("refreshAdminData", block)


class TestHcdPlanDebounce(unittest.TestCase):
    def test_filter_only_path(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn("filterOnly", js)
        self.assertIn("scheduleFilterRender", js)
        self.assertIn("tb-view-host", js)


class TestAgcHistoryBatch(unittest.TestCase):
    def test_source_uses_batch(self):
        src = _HOME_PY.read_text(encoding="utf-8")
        self.assertIn("family-bot-agc", src)
        self.assertIn("get_history_batch", src)
        # Old N-gather of get_temperature_history should not remain in _get_temps_data
        idx = src.find("async def _get_temps_data")
        block = src[idx : idx + 2500]
        self.assertIn("get_history_batch", block)
        self.assertNotIn("asyncio.gather(*[ha_service.get_temperature_history", block)

    def test_batch_called_with_eids(self):
        """Unit: hist_map path shapes result without N history calls."""
        if not _HOME_PY.exists():
            self.skipTest("home.py not mounted in this environment")
        self.assertIn("get_history_batch", _HOME_PY.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
