import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub dependencies
for _mod in ("discord", "aiomqtt", "frigate_service"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from llm.compat import execute_tool as _execute_tool


class TestFrigateHoursTool(unittest.IsolatedAsyncioTestCase):

    @patch("config.update_config", new_callable=AsyncMock)
    async def test_set_frigate_hours_success(self, mock_update):
        """Test that the frigate_set_hours tool successfully sets hours with valid formats."""
        ctx = MagicMock()
        ctx.shadow = False
        ctx.config = {"frigate": {"night_hours": {"start": "22:00", "end": "06:00"}}}

        res = await _execute_tool(
            "frigate_set_hours",
            {"start": "23:00", "end": "07:30"},
            config=ctx.config,
            cal_service=None,
            db_module=MagicMock(),
            tz=MagicMock(),
            session=MagicMock(),
            group="admin",
            hitl_approved=True,
        )

        self.assertIn("have been set to start at 23:00 and end at 07:30", res)
        mock_update.assert_called_once_with({"frigate": {"night_hours": {"start": "23:00", "end": "07:30"}}})

    @patch("config.update_config", new_callable=AsyncMock)
    async def test_set_frigate_hours_invalid_format(self, mock_update):
        """Test that the tool rejects invalid time formats."""
        ctx = MagicMock()
        ctx.shadow = False
        ctx.config = {"frigate": {"night_hours": {"start": "22:00", "end": "06:00"}}}

        res = await _execute_tool(
            "frigate_set_hours",
            {"start": "25:00", "end": "06:00"},
            config=ctx.config,
            cal_service=None,
            db_module=MagicMock(),
            tz=MagicMock(),
            session=MagicMock(),
            group="admin",
            hitl_approved=True,
        )

        self.assertIn("Invalid time format", res)
        mock_update.assert_not_called()

    async def test_set_frigate_hours_shadow(self):
        """Test that the tool does not perform writes in shadow mode."""
        from tools.admin import handle_frigate_set_hours
        
        ctx = MagicMock()
        ctx.shadow = True
        ctx.config = {"frigate": {"night_hours": {"start": "22:00", "end": "06:00"}}}

        res = await handle_frigate_set_hours({"start": "23:00", "end": "07:00"}, ctx)
        self.assertIn("[shadow: would have set frigate night hours", res)
