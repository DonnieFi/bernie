"""Oura Ring service tests — all HTTP calls mocked."""
import os
import sys
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import oura_service


def _mock_response(status: int, json_data: dict):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(responses: list):
    """Return a mock ClientSession whose get() returns responses in order."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(side_effect=responses)
    return session


def _mock_session_by_path(mapping: dict):
    """Route get() by URL path segment (safe under asyncio.gather)."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    def _get(url, *args, **kwargs):
        for key, resp in mapping.items():
            if key in str(url):
                return resp
        raise AssertionError(f"unexpected Oura URL: {url}")

    session.get = MagicMock(side_effect=_get)
    return session


_SLEEP_PAYLOAD = {
    "data": [{
        "day": "2026-05-12",
        "type": "long_sleep",
        "bedtime_start": "2026-05-12T22:30:00-03:00",
        "bedtime_end": "2026-05-13T06:30:00-03:00",
        "total_sleep_duration": 28800,
        "time_in_bed": 30000,
        "awake_time": 1200,
        "rem_sleep_duration": 5400,
        "light_sleep_duration": 9000,
        "deep_sleep_duration": 7200,
        "sleep_latency": 600,
        "efficiency": 92,
        "restless_periods": 3,
        "average_hrv": 45,
        "lowest_heart_rate": 52,
        "average_heart_rate": 58,
        "average_breath": 14.5,
        "average_spo2_percentage": 97.2,
        "lowest_spo2_percentage": 94.0,
        "average_skin_temperature": 0.3,
        "hrv": {"items": [40, 45, 50]},
        "heart_rate": {"items": [55, 58, 56]},
        "sleep_phase_5_min": "444333222",
        "movement_30_sec": "111222",
    }]
}
_DAILY_PAYLOAD = {
    "data": [{
        "score": 82,
        "contributors": {
            "deep_sleep": 80, "efficiency": 90, "latency": 85,
            "rem_sleep": 75, "restfulness": 88, "timing": 70, "total_sleep": 83,
        }
    }]
}
_READINESS_PAYLOAD = {
    "data": [{
        "score": 78,
        "contributors": {
            "activity_balance": 70, "body_temperature": 85, "hrv_balance": 72,
            "previous_day_activity": 80, "previous_night": 82, "recovery_index": 75,
            "resting_heart_rate": 80, "sleep_balance": 78,
        }
    }]
}


class TestOuraTokenMissing(unittest.IsolatedAsyncioTestCase):

    async def test_no_token_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OURA_TOKEN", None)
            result = await oura_service.get_sleep(date(2026, 5, 12))
            self.assertIsNone(result)


class TestOuraHTTPErrors(unittest.IsolatedAsyncioTestCase):

    async def test_401_returns_none(self):
        resp = _mock_response(401, {})
        session = _mock_session([resp])
        with patch.dict(os.environ, {"OURA_TOKEN": "fake"}), \
             patch("oura_service.get_http_session", return_value=session):
            result = await oura_service.get_sleep(date(2026, 5, 12))
        self.assertIsNone(result)

    async def test_non_200_returns_none(self):
        resp = _mock_response(500, {})
        session = _mock_session([resp])
        with patch.dict(os.environ, {"OURA_TOKEN": "fake"}), \
             patch("oura_service.get_http_session", return_value=session):
            result = await oura_service.get_sleep(date(2026, 5, 12))
        self.assertIsNone(result)


class TestOuraNoData(unittest.IsolatedAsyncioTestCase):

    async def test_no_sessions_returns_no_data(self):
        empty = _mock_response(200, {"data": []})
        # family-bot-1bf.5: single window fetch, not 14 sequential days
        session = _mock_session([empty])
        with patch.dict(os.environ, {"OURA_TOKEN": "fake"}), \
             patch("oura_service.get_http_session", return_value=session):
            result = await oura_service.get_sleep(date(2026, 5, 12))
        self.assertIsNotNone(result)
        self.assertTrue(result.get("no_data"))
        self.assertEqual(session.get.call_count, 1)


class TestOuraNormalResponse(unittest.IsolatedAsyncioTestCase):

    async def _run_happy_path(self):
        session = _mock_session_by_path({
            "/sleep": _mock_response(200, _SLEEP_PAYLOAD),
            "/daily_sleep": _mock_response(200, _DAILY_PAYLOAD),
            "/daily_readiness": _mock_response(200, _READINESS_PAYLOAD),
        })
        with patch.dict(os.environ, {"OURA_TOKEN": "fake"}), \
             patch("oura_service.get_http_session", return_value=session):
            return await oura_service.get_sleep(date(2026, 5, 12))

    async def test_returns_date(self):
        result = await self._run_happy_path()
        self.assertEqual(result["date"], "2026-05-12")

    async def test_sleep_durations_converted_to_minutes(self):
        result = await self._run_happy_path()
        self.assertEqual(result["total_sleep_minutes"], 480)   # 28800s
        self.assertEqual(result["rem_minutes"], 90)            # 5400s
        self.assertEqual(result["deep_minutes"], 120)          # 7200s

    async def test_daily_score_present(self):
        result = await self._run_happy_path()
        self.assertEqual(result["daily_score"], 82)
        self.assertIn("deep_sleep", result["score_contributors"])

    async def test_readiness_score_present(self):
        result = await self._run_happy_path()
        self.assertEqual(result["readiness_score"], 78)
        self.assertIn("hrv_balance", result["readiness_contributors"])

    async def test_hrv_and_hr_samples_present(self):
        result = await self._run_happy_path()
        self.assertEqual(result["hrv_5min_samples"], [40, 45, 50])
        self.assertEqual(result["heart_rate_5min_samples"], [55, 58, 56])

    async def test_efficiency_passthrough(self):
        result = await self._run_happy_path()
        self.assertEqual(result["sleep_efficiency"], 92)


class TestOuraFallbackToPreviousDay(unittest.IsolatedAsyncioTestCase):

    async def test_falls_back_when_first_day_empty(self):
        """If requested date has no data, get_sleep uses the most recent available."""
        yesterday_payload = {
            "data": [{**_SLEEP_PAYLOAD["data"][0], "day": "2026-05-11"}]
        }
        session = _mock_session_by_path({
            "/sleep": _mock_response(200, yesterday_payload),
            "/daily_sleep": _mock_response(200, _DAILY_PAYLOAD),
            "/daily_readiness": _mock_response(200, _READINESS_PAYLOAD),
        })
        with patch.dict(os.environ, {"OURA_TOKEN": "fake"}), \
             patch("oura_service.get_http_session", return_value=session):
            result = await oura_service.get_sleep(date(2026, 5, 12))
        self.assertEqual(result["date"], "2026-05-11")
        # one sleep window + two parallel daily endpoints
        self.assertEqual(session.get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
