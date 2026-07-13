"""openrouter_direct alias persistence for /model-add."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestRegisterOpenrouterAlias(unittest.TestCase):
    def test_register_persists_and_resolves(self):
        import openrouter_models as om

        with tempfile.TemporaryDirectory() as tmp:
            prices = Path(tmp) / "model_prices.json"
            prices.write_text(
                json.dumps({"_litellm_aliases": {}, "models": []}),
                encoding="utf-8",
            )
            with patch.object(om, "_prices_path", return_value=prices):
                om.invalidate_alias_table()
                om.register_openrouter_alias("or-grok-45", "x-ai/grok-4.5")
                data = json.loads(prices.read_text(encoding="utf-8"))
                self.assertEqual(
                    data["_litellm_aliases"]["or-grok-45"],
                    "x-ai/grok-4.5",
                )
                self.assertEqual(
                    om.resolve_openrouter_slug("or-grok-45"),
                    "x-ai/grok-4.5",
                )
                om.invalidate_alias_table()

    def test_extras_include_grok_45(self):
        import openrouter_models as om

        om.invalidate_alias_table()
        self.assertEqual(
            om.resolve_openrouter_slug("or-grok-45"),
            "x-ai/grok-4.5",
        )


if __name__ == "__main__":
    unittest.main()
