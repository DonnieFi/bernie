"""Tests for json_schema_compat — Codex/OpenRouter structured output."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_schema_compat import sanitize_json_schema_for_structured_output
from typed_outputs import ConsolidationOutput


class JsonSchemaCompatTest(unittest.TestCase):
    def test_consolidation_schema_has_no_defs_or_anyof(self):
        raw = ConsolidationOutput.model_json_schema()
        clean = sanitize_json_schema_for_structured_output(raw)
        self.assertNotIn("$defs", clean)
        self.assertNotIn("definitions", clean)
        dumped = str(clean)
        self.assertNotIn("anyOf", dumped)
        self.assertNotIn("maxLength", dumped)
        obs = clean["properties"]["observations"]["items"]["properties"]
        self.assertNotIn("expires_at", obs)

    def test_inlines_ref_properties(self):
        raw = ConsolidationOutput.model_json_schema()
        clean = sanitize_json_schema_for_structured_output(raw)
        props = clean.get("properties") or {}
        self.assertIn("new_routines", props)
        item = props["new_routines"]
        self.assertEqual(item.get("type"), "array")
        items = item.get("items") or {}
        self.assertIn("properties", items)
        self.assertIn("name", items["properties"])


if __name__ == "__main__":
    unittest.main()
