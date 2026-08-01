
import asyncio
import aiohttp
import os
import sys

async def main():
    api_key = os.getenv("TOMORROW_WEATHER_API")
    if not api_key:
        print("TOMORROW_WEATHER_API not set")
        return
    
    lat, lon = 44.6476, -63.5728
    url = f"https://api.tomorrow.io/v4/weather/realtime?location={lat},{lon}&apikey={api_key}&units=metric"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            print(data)

if __name__ == "__main__":
    asyncio.run(main())
