"""Phase 34 — email send policy and hygiene."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from email_service import (
    _apply_bernie_subject_prefix,
    _strip_quoted_blocks,
    check_send_policy,
    family_email_set,
    normalize_email,
    resolve_family_cc_email,
)


class _FakeConfig:
    family_members = {
        "Dad": {
            "canonical_id": "dad",
            "email": "dad@example.com",
            "role": "admin",
        },
        "Kid": {
            "canonical_id": "child1",
            "email": "kid@school.ca",
            "role": "kids",
        },
    }


class TestEmailPolicy(unittest.TestCase):
    def setUp(self):
        self.cfg = {"family_members": _FakeConfig.family_members}

    def test_family_email_set_from_members(self):
        emails = family_email_set(self.cfg)
        self.assertIn("dad@example.com", emails)
        self.assertIn("kid@school.ca", emails)

    def test_parent_to_family_allowed(self):
        action, err = check_send_policy("dad@example.com", None, "parents", self.cfg)
        self.assertEqual(action, "allow")
        self.assertIsNone(err)

    def test_parent_to_non_family_blocked(self):
        action, err = check_send_policy("coach@school.ca", None, "parents", self.cfg)
        self.assertEqual(action, "block")
        self.assertIn("coach@school.ca", err or "")

    def test_kid_to_family_needs_approval(self):
        action, err = check_send_policy("dad@example.com", None, "kids", self.cfg)
        self.assertEqual(action, "approve")
        self.assertIsNone(err)

    def test_cc_non_family_blocked(self):
        action, err = check_send_policy(
            "dad@example.com", ["coach@school.ca"], "parents", self.cfg
        )
        self.assertEqual(action, "block")
        self.assertIn("coach@school.ca", err or "")

    def test_system_to_family_allowed(self):
        action, _ = check_send_policy("kid@school.ca", None, "system", self.cfg)
        self.assertEqual(action, "allow")

    def test_bernie_subject_prefix(self):
        self.assertTrue(_apply_bernie_subject_prefix("Hello").startswith("[Bernie]"))
        self.assertEqual(_apply_bernie_subject_prefix("[Bernie] Hi"), "[Bernie] Hi")

    def test_strip_quoted_blocks(self):
        body = "Hi mom\n\nOn Tue, x wrote:\n> quoted"
        self.assertEqual(_strip_quoted_blocks(body), "Hi mom")


class TestEmailReplyRouting(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_prefers_forwarder_from_signal(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from email_service import _resolve_reply_recipient

        fake_db = MagicMock()
        fake_db.get_email_signal_by_thread_id = AsyncMock(return_value=None)
        fake_db.get_email_signal_by_gmail_id = AsyncMock(
            return_value={
                "gmail_id": "g1",
                "forwarder_email": "dad@example.com",
                "thread_id": "t1",
            }
        )
        cfg = {"family_members": _FakeConfig.family_members}

        with patch("db_binding.get_database", return_value=fake_db):
            resolved = await _resolve_reply_recipient(
                "external@evil.com",
                reply_to_gmail_id="g1",
                thread_id=None,
                config=cfg,
            )
        self.assertEqual(resolved, "dad@example.com")

    async def test_resolve_thread_id_lookup(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from email_service import _resolve_reply_recipient

        fake_db = MagicMock()
        fake_db.get_email_signal_by_thread_id = AsyncMock(
            return_value={
                "gmail_id": "g2",
                "forwarder_email": "dad@example.com",
                "thread_id": "t9",
            }
        )
        fake_db.get_email_signal_by_gmail_id = AsyncMock(
            return_value={
                "gmail_id": "g2",
                "forwarder_email": "dad@example.com",
            }
        )
        cfg = {"family_members": _FakeConfig.family_members}

        with patch("db_binding.get_database", return_value=fake_db):
            resolved = await _resolve_reply_recipient(
                "external@evil.com",
                reply_to_gmail_id=None,
                thread_id="t9",
                config=cfg,
            )
        self.assertEqual(resolved, "dad@example.com")


class TestResolveFamilyCc(unittest.TestCase):
    def test_prefers_config_when_in_family(self):
        cfg = {
            "study_guide_cc_email": "dad@example.com",
            "family_members": _FakeConfig.family_members,
        }
        self.assertEqual(resolve_family_cc_email(cfg, "study_guide_cc_email"), "dad@example.com")

    def test_gmail_alias_matches_family(self):
        self.assertEqual(
            normalize_email("d.ad+tag@gmail.com"),
            normalize_email("dad@gmail.com"),
        )