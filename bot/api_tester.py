import aiohttp
import logging
import os

log = logging.getLogger(__name__)

async def run_api_tests(session: aiohttp.ClientSession, config: dict) -> list[str]:
    """
    Run structural and content tests against live APIs to catch upstream breaking changes.
    Returns a list of error strings. Empty list means all tests passed.
    """
    errors = []

    # 1. Open-Meteo Geocoding
    url_geo = "https://geocoding-api.open-meteo.com/v1/search?name=Toronto&count=1&format=json&language=en"
    try:
        async with session.get(url_geo, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                errors.append(f"Open-Meteo Geocoding HTTP {resp.status}")
            else:
                data = await resp.json()
                results = data.get("results", [])
                if not results or not isinstance(results, list):
                    errors.append("Open-Meteo Geocoding: 'results' list missing or empty.")
                else:
                    first = results[0]
                    if "latitude" not in first or "longitude" not in first or "timezone" not in first:
                        errors.append("Open-Meteo Geocoding: 'latitude', 'longitude', or 'timezone' missing from result.")
    except Exception as e:
        errors.append(f"Open-Meteo Geocoding exception: {e}")

    # 2. Tomorrow.io APIs
    tomorrow_key = os.getenv("TOMORROW_WEATHER_API")
    lat, lon = config.get("location", {}).get("lat", 44.6476), config.get("location", {}).get("lon", -63.5728)
    
    if tomorrow_key:
        # Realtime
        url_rt = f"https://api.tomorrow.io/v4/weather/realtime?location={lat},{lon}&apikey={tomorrow_key}&units=metric"
        try:
            async with session.get(url_rt, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    errors.append(f"Tomorrow.io Realtime HTTP {resp.status}")
                else:
                    data = await resp.json()
                    payload = data.get("data") or data
                    values = payload.get("values", {})
                    if "temperature" not in values or "weatherCode" not in values:
                        errors.append("Tomorrow.io Realtime: 'temperature' or 'weatherCode' missing from 'values'.")
        except Exception as e:
            errors.append(f"Tomorrow.io Realtime exception: {e}")

        # Forecast
        url_fcst = f"https://api.tomorrow.io/v4/weather/forecast?location={lat},{lon}&timesteps=1d&apikey={tomorrow_key}&units=metric"
        try:
            async with session.get(url_fcst, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    errors.append(f"Tomorrow.io Forecast HTTP {resp.status}")
                else:
                    data = await resp.json()
                    payload = data.get("data") or data
                    timelines = payload.get("timelines") or data.get("timelines") or {}
                    
                    intervals = []
                    if isinstance(timelines, dict) and "daily" in timelines:
                        intervals = timelines["daily"]
                    elif isinstance(timelines, list):
                        timeline = next((t for t in timelines if isinstance(t, dict) and t.get("timestep") == "1d"), None)
                        if not timeline and timelines and isinstance(timelines[0], dict):
                            timeline = timelines[0]
                        intervals = timeline.get("intervals", []) if timeline else []
                    
                    if not intervals:
                        errors.append("Tomorrow.io Forecast: Timelines 'daily' list or 'intervals' missing.")
                    else:
                        first = intervals[0]
                        vals = first.get("values", {})
                        if "temperatureMax" not in vals or "temperatureMin" not in vals:
                            errors.append("Tomorrow.io Forecast: 'temperatureMax' or 'temperatureMin' missing from daily interval values.")
                        
                        start_time = first.get("time") or first.get("startTime")
                        if not start_time:
                            errors.append("Tomorrow.io Forecast: 'time' or 'startTime' missing from daily interval.")

        except Exception as e:
            errors.append(f"Tomorrow.io Forecast exception: {e}")

    # 3. Environment Canada
    bbox = f"{lon - 1.5},{lat - 1.5},{lon + 1.5},{lat + 1.5}"
    
    # SWOB
    url_swob = f"https://api.weather.gc.ca/collections/swob-realtime/items?f=json&bbox={bbox}&limit=5"
    try:
        async with session.get(url_swob, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                errors.append(f"EC SWOB HTTP {resp.status}")
            else:
                data = await resp.json()
                features = data.get("features", [])
                if not features:
                    errors.append("EC SWOB: 'features' list missing or empty.")
                else:
                    props = features[0].get("properties", {})
                    # Just verify basic structure
                    if "observation" not in props and "air_temp" not in props:
                        errors.append("EC SWOB: 'properties' missing expected observation data.")
    except Exception as e:
        errors.append(f"EC SWOB exception: {e}")

    # CityPageWeather
    url_city = f"https://api.weather.gc.ca/collections/citypageweather-realtime/items?f=json&bbox={bbox}&limit=5"
    try:
        async with session.get(url_city, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                errors.append(f"EC CityPage HTTP {resp.status}")
            else:
                data = await resp.json()
                features = data.get("features", [])
                if not features:
                    errors.append("EC CityPage: 'features' list missing or empty.")
                else:
                    props = features[0].get("properties", {})
                    if "site" not in props and "forecastGroup" not in props:
                        errors.append("EC CityPage: 'properties' missing expected forecast data.")
    except Exception as e:
        errors.append(f"EC CityPage exception: {e}")

    # 4. ReCollect
    recollect_url = config.get("recollect_ics_url")
    if recollect_url:
        try:
            async with session.get(recollect_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    errors.append(f"ReCollect ICS HTTP {resp.status}")
                else:
                    text = await resp.text()
                    if "BEGIN:VCALENDAR" not in text:
                        errors.append("ReCollect ICS: Missing 'BEGIN:VCALENDAR' marker.")
        except Exception as e:
            errors.append(f"ReCollect ICS exception: {e}")

    return errors
