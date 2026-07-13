import sys
import os
import asyncio
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo


def _make_services():
    from executor import ServiceRefs

    db = MagicMock()
    db.get_tomorrow_context = AsyncMock(return_value=None)
    db.get_routines = AsyncMock(return_value=[])
    db.get_semantic_observations = AsyncMock(return_value=[])
    return ServiceRefs(
        calendar=None,
        ha=None,
        db=db,
        session=None,
        orchestrator=None,
        identity=None,
        tz=ZoneInfo("America/Halifax"),
    )


def _blocks_text(blocks) -> str:
    parts = []
    for b in blocks or []:
        if isinstance(b, dict):
            parts.append(b.get("text", "") or "")
        else:
            parts.append(str(b))
    return " ".join(parts)


class BernieContextTests(unittest.TestCase):
    def test_bernie_context_builds(self):
        from context import BernieContext

        config = {"timezone": "America/Halifax", "family_members": []}
        services = _make_services()

        async def _run():
            ctx = await BernieContext.build(
                config=config,
                person_id="person.red",
                channel_id="111111111111111111",
                tz=ZoneInfo("America/Halifax"),
                services=services,
            )
            self.assertIsInstance(ctx, BernieContext)
            blocks = ctx.render_blocks()
            self.assertIsInstance(blocks, list)
            self.assertGreater(len(_blocks_text(blocks)), 0)

        asyncio.run(_run())

    def test_render_includes_tomorrow_context(self):
        from context import BernieContext

        config = {"timezone": "America/Halifax", "family_members": []}
        services = _make_services()
        services.db.get_tomorrow_context = AsyncMock(return_value="Tomorrow: dentist at 9am")

        async def _run():
            ctx = await BernieContext.build(
                config=config,
                person_id=None,
                channel_id=None,
                tz=ZoneInfo("America/Halifax"),
                services=services,
            )
            text = _blocks_text(ctx.render_blocks())
            self.assertIn("dentist", text)

        asyncio.run(_run())
