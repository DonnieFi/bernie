"""Initial tests for the Phase 28 Wave 2c mode system.

These tests validate the data layer (the .md definition files) even before
the loader and resolver are implemented.

Critical goals:
- All 8 expected mode files exist and have required frontmatter keys.
- No deprecated fields (ask:, sticky:) are present.
- Concierge regression snapshot exists so we can detect silent shrinkage
  of the default tool surface the moment the loader is wired.
"""

from pathlib import Path
import unittest
import json
import yaml

from database import init_db, search_activity_log, _db_conn

MODES_DIR = Path(__file__).parent.parent / "modes"

EXPECTED_SLUGS = {
    "concierge",
    "tutor",
    "chef",
    "wind-down",
    "debug",
    "security",
    "home_automation",
    "ops",
    "chat-openwebui",
}

# Snapshot of the exact allow-list concierge must expose (non-admin tools).
# This is the regression fixture. Update only when intentionally changing
# the default tool surface.
CONCIERGE_ALLOW_SNAPSHOT = {
    "calendar",
    "cognitive",
    "email",
    "flights",
    "home",
    "identity",
    "meals",
    "media",
    "memory",
    "network",
    "notify",
    "presence",
    "search",
    "snapshots",
    "tasks",
    "transit",
    "weather",
}


class TestModeDefinitions(unittest.TestCase):

    def test_all_eight_mode_files_exist(self):
        actual = {p.stem for p in MODES_DIR.glob("*.md")}
        self.assertEqual(EXPECTED_SLUGS, actual, f"Mode file mismatch. Expected {EXPECTED_SLUGS}, got {actual}")

    def test_no_deprecated_fields_and_required_keys_present(self):
        """Reject ask: and sticky: (per Phase 28 v1 decisions).
        Require the minimum keys the loader will need.
        """
        # family-bot-19l: model_preference deleted (never wired into routing)
        required_top_level = {"slug", "name", "visibility", "domains"}
        required_domains = {"allow", "deny"}

        for md_file in MODES_DIR.glob("*.md"):
            text = md_file.read_text()
            self.assertTrue(text.startswith("---"), f"{md_file.name} must start with YAML frontmatter")

            _, frontmatter, _ = text.split("---", 2)
            data = yaml.safe_load(frontmatter) or {}

            # Check for deprecated fields (both top-level and nested under domains)
            self.assertNotIn("ask", data, f"{md_file.name} still contains deprecated 'ask:' field")
            domains = data.get("domains") or {}
            self.assertNotIn("ask", domains, f"{md_file.name} still contains deprecated 'domains.ask'")
            self.assertNotIn("sticky", data, f"{md_file.name} still contains deferred 'sticky:' field")
            self.assertNotIn("model_preference", data, f"{md_file.name} still has dead model_preference")

            # Required top-level keys
            missing = required_top_level - data.keys()
            self.assertFalse(missing, f"{md_file.name} missing required keys: {missing}")

            # domains structure
            domains = data.get("domains", {})
            missing_domains = required_domains - domains.keys()
            self.assertFalse(missing_domains, f"{md_file.name} domains missing: {missing_domains}")

            # Basic sanity on slug
            slug = data.get("slug")
            self.assertIsInstance(slug, str)
            self.assertTrue(slug and " " not in slug and slug.islower(),
                            f"{md_file.name} has invalid slug: {slug}")





class TestConciergeSnapshot(unittest.TestCase):

    def test_concierge_regression_snapshot(self):
        """Hardcoded snapshot test for concierge allow-list.

        This is intentionally a data snapshot (not yet loaded from the file)
        so the test can be un-xfailed the moment the loader + get_allowed_tools
        integration lands. It prevents silent regression of the default surface.
        """
        concierge_path = MODES_DIR / "concierge.md"
        text = concierge_path.read_text()
        _, frontmatter, _ = text.split("---", 2)
        data = yaml.safe_load(frontmatter) or {}

        actual_allow = set(data.get("domains", {}).get("allow", []))
        self.assertEqual(
            actual_allow,
            CONCIERGE_ALLOW_SNAPSHOT,
            f"Concierge allow-list changed!\nExpected: {CONCIERGE_ALLOW_SNAPSHOT}\nGot:      {actual_allow}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
