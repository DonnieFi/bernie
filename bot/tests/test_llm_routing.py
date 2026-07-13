import unittest
from unittest.mock import MagicMock, patch

from service_container import ServiceContainer

class TestLLMRouting(unittest.TestCase):
    def setUp(self):
        self.mock_anthropic = MagicMock()
        self.mock_litellm = MagicMock()
        self.mock_ollama_url = "http://fake-ollama:11434"
        
        self.container = ServiceContainer(
            anthropic=self.mock_anthropic,
            litellm=self.mock_litellm,
            ollama=self.mock_ollama_url,
        )

    def test_llm_for_anthropic(self):
        with patch.dict("config.config", {"ollama_models": []}):
            result = self.container.llm_for("claude-sonnet-4-6")
            self.assertIs(result, self.mock_anthropic)

    def test_llm_for_litellm(self):
        with patch.dict("config.config", {"ollama_models": ["fake-local"]}):
            result = self.container.llm_for("or-deepseek-v3")
            self.assertIs(result, self.mock_litellm)

    def test_llm_for_ollama(self):
        with patch.dict("config.config", {"ollama_models": ["fake-local"]}):
            result = self.container.llm_for("fake-local")
            self.assertEqual(result, self.mock_ollama_url)

if __name__ == "__main__":
    unittest.main()
