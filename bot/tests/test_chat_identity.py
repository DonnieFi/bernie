import sys
from unittest.mock import MagicMock

# Mock out heavy/missing dependencies before anything else is imported
MOCK_MODULES = [
    'websockets', 'discord', 'discord.ext', 'discord.ext.commands',
    'croniter', 'anthropic', 'pytz', 'googleapiclient',
    'googleapiclient.discovery', 'google.oauth2', 'google.auth.transport.requests',
    'google.oauth2.credentials', 'google_auth_oauthlib.flow'
]
for mod in MOCK_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import unittest
from unittest.mock import Mock, patch, AsyncMock
import asyncio
import os
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient

# Ensure bot directory is in path
_HERE = os.path.dirname(__file__)
_BOT_DIR = os.path.abspath(os.path.join(_HERE, '..'))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

# Mock some things BEFORE importing modules that might trigger side effects
os.environ["DISCORD_TOKEN"] = "fake-token"
os.environ["ANTHROPIC_API_KEY"] = "fake-key"
os.environ["SPOON_API_KEY"] = "fake-key"
os.environ["BERNIE_API_TOKEN"] = "fake-api-token"

from constants import registry as person_registry
import api
import bot as bot_mod

class ChatIdentityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # create_api mounts WEB_ROOT/static — cognition image has no /web tree.
        cls._web_tmp = tempfile.TemporaryDirectory()
        web_root = cls._web_tmp.name
        Path(web_root, "static").mkdir()
        Path(web_root, "index.html").write_text("<html></html>", encoding="utf-8")

        cls.mock_bot = Mock()
        cls.mock_container = Mock()
        cls.mock_container.frigate = Mock()
        cls.mock_container.notification_orchestrator = Mock()
        cls.mock_container.calendar = Mock()
        cls.mock_container.weather = Mock()
        cls.mock_container.ha = Mock()
        cls.mock_container.summary_builder = Mock()
        cls.mock_container.connection_manager = Mock()
        cls.mock_container.supervisor = Mock()

        cls._web_patcher = patch("api.common.WEB_ROOT", web_root)
        cls._web_patcher.start()
        cls.app = api.create_api(bot=cls.mock_bot, container=cls.mock_container)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._web_patcher.stop()
        cls._web_tmp.cleanup()
        # Don't poison later modules in the same unittest run (e.g. test_executor_smol
        # load_all_domains → tools.email → real googleapiclient).
        for mod in MOCK_MODULES:
            sys.modules.pop(mod, None)
    def setUp(self):
        """Load a controlled person registry for tests."""
        self.test_config = {
            "family_members": {
                "Dad": {
                    "canonical_id": "dad",
                    "role": "admin",
                    "discord_id": "12345",
                    "email": "dad@example.com",
                },
                "Child1": {
                    "canonical_id": "child1",
                    "role": "kids",
                    "discord_id": "67890",
                    "email": "child1@example.com",
                }
            },
            "openwebui_users": {
                "dad.web@example.com": "dad"
            },
            "timezone": "America/Halifax",
            "bernie_api_token": "fake-api-token"
        }
        person_registry.load(self.test_config)
        
        # Patch BOTH the module-level config and the one used inside functions
        self.config_patcher1 = patch('api.common.config', self.test_config)
        self.config_patcher2 = patch('bot.config', self.test_config)
        self.config_patcher1.start()
        self.config_patcher2.start()

    def tearDown(self):
        self.config_patcher1.stop()
        self.config_patcher2.stop()
        person_registry.load({})
        self.app.dependency_overrides = {}

    @patch("bot.get_database")
    @patch("bot.frigate_service", new_callable=Mock)
    def test_discord_chat_identity(self, mock_frigate, mock_get_database):
        """Verify _handle_message resolves identity via person_registry for Discord messages."""
        mock_db = AsyncMock()
        mock_db.get_history.return_value = []
        mock_db.add_message.return_value = None
        mock_get_database.return_value = mock_db

        mock_chat_fn = AsyncMock(return_value="Test response")
        
        mock_author = Mock()
        mock_author.id = 12345 # Dad's discord ID
        mock_author.display_name = "Dad"

        mock_message = AsyncMock()
        mock_message.author = mock_author
        mock_message.content = "Hello from discord"
        
        # Manually construct the channel and typing mock to avoid AsyncMock issues
        mock_channel = Mock()
        mock_channel.id = 999
        mock_channel.send = AsyncMock(return_value=MagicMock())
        mock_typing = MagicMock()
        mock_typing.__aenter__ = AsyncMock()
        mock_typing.__aexit__ = AsyncMock()
        mock_channel.typing.return_value = mock_typing
        mock_message.channel = mock_channel
        
        # Run the handler
        asyncio.run(bot_mod._handle_message(mock_message, mock_chat_fn))

        # Assert chat function was called with correct identity
        mock_chat_fn.assert_called_once()
        _, kwargs = mock_chat_fn.call_args
        self.assertEqual(kwargs.get("actor_id"), "dad")
        self.assertEqual(kwargs.get("group"), "admin")
        self.assertEqual(kwargs.get("person_name"), "Dad") # Should be capitalized now
    
    @patch("llm.chat.chat_general", new_callable=AsyncMock)
    def test_web_ui_chat_identity(self, mock_chat_general):
        """Verify /api/chat resolves identity from the JWT for Web UI messages."""
        mock_chat_general.return_value = "Test reply"
        
        # Override the dependency that verifies the token
        async def override_verify():
            return api.Person(id="child1", role="kids")
            
        self.app.dependency_overrides[api.verify_token] = override_verify
        
        response = self.client.post(
            "/api/chat",
            headers={"X-Bernie-Token": "fake-api-token"},
            json={"message": "Hello from web UI"}
        )
        
        self.assertEqual(response.status_code, 200)
        
        # Assert that chat_general was called with the correct identity
        mock_chat_general.assert_called_once()
        _, kwargs = mock_chat_general.call_args
        self.assertEqual(kwargs.get("person_name"), "Child1")
        self.assertEqual(kwargs.get("actor_id"), "child1")
        self.assertEqual(kwargs.get("group"), "kids")

    @patch("llm.chat.chat_general", new_callable=AsyncMock)
    def test_openwebui_chat_identity(self, mock_chat_general):
        """Verify /v1/chat/completions resolves identity from payload email for OpenWebUI messages."""
        mock_chat_general.return_value = "Test reply"

        # Override the dependency that verifies the bearer token
        async def override_verify_bearer():
            return api.Person(id="api_user", role="admin")
            
        self.app.dependency_overrides[api.verify_bearer_token] = override_verify_bearer

        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer fake-api-token"},
            json={
                "model": "bernie",
                "messages": [{"role": "user", "content": "Hello from OpenWebUI"}],
                "user": "dad.web@example.com" # This email is in the test config
            }
        )

        self.assertEqual(response.status_code, 200)
        
        # Assert chat_general was called with correctly resolved identity
        mock_chat_general.assert_called_once()
        _, kwargs = mock_chat_general.call_args
        self.assertEqual(kwargs.get("person_name"), "Dad") # Resolved from email
        self.assertEqual(kwargs.get("actor_id"), "dad")
        self.assertEqual(kwargs.get("group"), "admin")

if __name__ == '__main__':
    unittest.main()
