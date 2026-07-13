"""Manual Ollama integration smoke checks.

These are NOT unit tests — they hit a live Ollama server and print to stdout.
Run them directly: `python bot/tests/test_ollama.py`.

The functions are intentionally named `check_*` (not `test_*`) so neither
pytest nor unittest auto-discovers them. The file lives in `bot/tests/` for
historical reasons; do not rename it without checking that no docs reference
this path.
"""

import asyncio
import aiohttp
import logging
import sys
import os

# Add /app to path to import local modules
sys.path.append(os.getcwd())

try:
    from claude_service import _call_ollama
    from config import config
except ImportError:
    _call_ollama = None
    config = {}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test-ollama")

async def check_ollama_direct():
    print("\n--- Testing Direct Ollama Connection ---")
    fallback_cfg = config.get("llm_fallback")
    if not fallback_cfg:
        print("Error: No llm_fallback configured in config.json")
        return

    url = f"{fallback_cfg['url'].rstrip('/')}/api/chat"
    print(f"Target URL: {url}")
    print(f"Target Model: {fallback_cfg['model']}")

    async with aiohttp.ClientSession() as session:
        payload = {
            "model": fallback_cfg["model"],
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say 'Ollama is alive!'"}
            ],
            "stream": False
        }
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                print(f"HTTP Status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("message", {}).get("content", "")
                    print(f"Response: {content}")
                    if "alive" in content.lower():
                        print("✅ Direct connection successful!")
                    else:
                        print("⚠️ Connection worked but response was unexpected.")
                else:
                    print(f"❌ Failed: {await resp.text()}")
        except Exception as e:
            print(f"❌ Error connecting to Ollama: {e}")

async def check_fallback_logic():
    print("\n--- Testing Fallback Logic in claude_service ---")
    system = "You are Bernie, a helpful family assistant."
    messages = [{"role": "user", "content": "Who are you?"}]

    async with aiohttp.ClientSession() as session:
        try:
            response = await _call_ollama(system, messages, config, session)
            print(f"Bernie (via Ollama): {response}")
            if "Bernie" in response:
                print("✅ Fallback logic correctly passed system prompt and identity.")
            else:
                print("⚠️ Fallback worked but Bernie might have forgotten his name.")
        except Exception as e:
            print(f"❌ Fallback execution failed: {e}")

if __name__ == "__main__":
    asyncio.run(check_ollama_direct())
    asyncio.run(check_fallback_logic())
