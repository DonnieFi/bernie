import sys, os, asyncio, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestJudgeTriplet(unittest.TestCase):

    def test_judge_triplet_exists(self):
        from eval_service import judge_triplet
        self.assertTrue(callable(judge_triplet))

    def test_judge_triplet_returns_none_without_api_key(self):
        """judge_triplet must return None (not raise) when the API key is absent."""
        import eval_service
        from unittest.mock import patch
        row = {
            "primary_response": "primary",
            "shadow_response": "model shadow",
            "harness_shadow_response": "harness shadow",
            "user_message": "hello",
        }
        with patch.object(eval_service, "ANTHROPIC_KEY", ""):
            result = asyncio.run(eval_service.judge_triplet(row, "claude-haiku-4-5-20251001"))
        self.assertEqual(result, {'winner': None, 'reasoning': 'ANTHROPIC_API_KEY not set'})


if __name__ == "__main__":
    unittest.main()
