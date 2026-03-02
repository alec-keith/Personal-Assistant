"""
Weather via Open-Meteo — completely free, no API key required.
Geocoding also via Open-Meteo's free geocoding API.
"""

import logging
import httpx

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}


async def get_weather(location: str, days: int = 3) -> str:
    """
    Return current conditions + N-day forecast for a location.
    Days must be 1-16.
    """
    days = max(1, min(days, 16))
    async with httpx.AsyncClient(timeout=10) as client:
        # Step 1: geocode the location
        geo = await client.get(GEOCODE_URL, params={"name": location, "count": 1, "language": "en"})
        geo.raise_for_status()
        results = geo.json().get("results")
        if not results:
            return f"Couldn't find location: {location}"

        place = results[0]
        lat, lon = place["latitude"], place["longitude"]
        city = place.get("name", location)
        country = place.get("country", "")

        # Step 2: fetch forecast
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": [
                "temperature_2m", "apparent_temperature", "weather_code",
                "wind_speed_10m", "relative_humidity_2m", "precipitation",
            ],
            "daily": [
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "precipitation_probability_max",
            ],
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto",
            "forecast_days": days,
        }
        wx = await client.get(FORECAST_URL, params=params)
        wx.raise_for_status()
        data = wx.json()

    cur = data["current"]
    daily = data["daily"]

    condition = WMO_CODES.get(cur["weather_code"], "Unknown")
    lines = [
        f"{city}, {country}" if country else city,
        f"Now: {cur['temperature_2m']}°F (feels {cur['apparent_temperature']}°F) — {condition}",
        f"Humidity {cur['relative_humidity_2m']}%  Wind {cur['wind_speed_10m']} mph",
    ]
    if cur.get("precipitation", 0) > 0:
        lines.append(f"Current precip: {cur['precipitation']}\"")

    lines.append("")
    lines.append("Forecast:")
    for i in range(len(daily["time"])):
        date = daily["time"][i]
        hi = daily["temperature_2m_max"][i]
        lo = daily["temperature_2m_min"][i]
        cond = WMO_CODES.get(daily["weather_code"][i], "?")
        precip = daily["precipitation_sum"][i]
        rain_chance = daily["precipitation_probability_max"][i]
        line = f"  {date}: {lo}–{hi}°F, {cond}"
        if rain_chance and rain_chance > 20:
            line += f", {rain_chance}% chance of rain"
        if precip and precip > 0:
            line += f" ({precip}\")"
        lines.append(line)

    return "\n".join(lines)
