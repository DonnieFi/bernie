from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.network import _fetch_speedtest_history


class _Response:
    status = 401

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def get(self, *_args, **_kwargs):
        return _Response()


class TestNetworkTools(unittest.IsolatedAsyncioTestCase):
    async def test_speedtest_auth_failure_is_not_reported_as_empty_history(self):
        with patch("tools.network.get_http_session", return_value=_Session()):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                await _fetch_speedtest_history("https://unifi.test", "bad-key")


if __name__ == "__main__":
    unittest.main()
