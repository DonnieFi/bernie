import os
import unittest
import aiohttp
import aiomqtt
from config import config


@unittest.skipUnless(os.environ.get("INTEGRATION_TESTS"), "requires live network (set INTEGRATION_TESTS=1)")
class TestFrigateHealth(unittest.IsolatedAsyncioTestCase):

    async def test_frigate_api_reachable(self):
        frigate_host = config.get("frigate", {}).get("host", "http://frigate.lan:5000")
        url = f"{frigate_host}/api/version"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    self.assertEqual(resp.status, 200, f"Frigate API returned {resp.status}")
                    text = await resp.text()
                    self.assertNotEqual(text, "", "Frigate API returned empty response")
            except aiohttp.ClientError as e:
                self.fail(f"Could not reach Frigate API at {url}: {e}")

    async def test_mqtt_broker_reachable(self):
        mqtt_cfg = config.get("mqtt", {})
        host = mqtt_cfg.get("host", "192.168.1.X")
        port = int(mqtt_cfg.get("port", 1883))
        user = os.environ.get("MQTT_USER")
        password = os.environ.get("MQTT_PASSWORD")

        self.assertIsNotNone(user, "MQTT_USER environment variable not set")
        self.assertIsNotNone(password, "MQTT_PASSWORD environment variable not set")

        try:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=user,
                password=password,
                identifier="bernie-health-test",
                timeout=5,
            ):
                pass
        except Exception as e:
            self.fail(f"Could not connect to MQTT broker at {host}:{port}: {e}")


if __name__ == "__main__":
    unittest.main()
