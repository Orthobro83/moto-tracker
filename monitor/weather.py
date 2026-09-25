"""
Weather for the map inset: Open-Meteo, no account and no key.

Coordinates are rounded to two decimals (about a kilometre) before they leave the
Mini, so the rider's exact position is never sent to a weather service.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

URL = "https://api.open-meteo.com/v1/forecast"
FIELDS = ("temperature_2m,apparent_temperature,precipitation,rain,weather_code,"
          "cloud_cover,wind_speed_10m,wind_gusts_10m,is_day,relative_humidity_2m")

# Below this the model has a trace, and nothing a rider would call rain.
WET_MM = 0.2

# WMO weather codes → what a rider needs to know.
CODES = {
    0: ("Clear", "clear"), 1: ("Mainly clear", "clear"), 2: ("Partly cloudy", "cloud"),
    3: ("Overcast", "cloud"), 45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"), 80: ("Light showers", "rain"), 81: ("Showers", "rain"),
    82: ("Violent showers", "rain"), 85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm, hail", "storm"),
    99: ("Thunderstorm, hail", "storm"),
}


def describe(code: int, day: bool, precip_mm: float, cloud: int) -> tuple:
    """Label and icon. **Rain is only claimed when there is rain to claim** (Jack,
    2026-09-20): the pane said "Light drizzle" over a dry, partly sunny ride with
    0.1 mm behind it, and "Thunderstorm" over a dry one the day before. The code is
    the model's opinion; precipitation and cloud cover are quantities it carries.
    Fog is the exception — it is about seeing, not wetness."""
    if code in (45, 48):
        return CODES[code][0], "fog"
    if precip_mm >= WET_MM:
        label, icon = CODES.get(code, ("Rain", "rain"))
        return label, (icon if icon in ("rain", "snow", "storm") else "rain")
    if cloud < 0:
        return "—", ("clear" if day else "night")
    if cloud < 20:
        return ("Sunny", "clear") if day else ("Clear", "night")
    if cloud < 60:
        return ("Partly cloudy", "clear") if day else ("Partly cloudy", "night")
    if cloud < 88:
        return "Cloudy", "cloud"
    return "Overcast", "cloud"


def fetch(lat: float, lon: float, windy_kmh: float = 30, timeout: float = 10) -> Optional[dict]:
    """Current conditions at (roughly) this position, or None if unreachable."""
    query = urllib.parse.urlencode({
        "latitude": round(lat, 2), "longitude": round(lon, 2),
        "current": FIELDS, "wind_speed_unit": "kmh", "timezone": "auto"})
    try:
        with urllib.request.urlopen(f"{URL}?{query}", timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    cur = data.get("current") or {}
    code = int(cur.get("weather_code", -1))
    day = bool(cur.get("is_day", 1))
    wind = cur.get("wind_speed_10m") or 0
    gust = cur.get("wind_gusts_10m") or 0
    precip = cur.get("precipitation") or 0
    cloud = cur.get("cloud_cover")
    label, icon = describe(code, day, precip, -1 if cloud is None else int(cloud))
    # A rider feels wind before they read a label, so it outranks a clear sky.
    headline = label
    if icon in ("rain", "storm", "snow"):
        headline = label
    elif wind >= windy_kmh or gust >= windy_kmh * 1.6:
        headline = "Windy"
    return {
        "headline": headline, "label": label, "icon": icon, "cloud_pct": cloud,
        "temp_c": cur.get("temperature_2m"), "feels_c": cur.get("apparent_temperature"),
        "wind_kmh": wind, "gust_kmh": gust, "humidity": cur.get("relative_humidity_2m"),
        "precip_mm": cur.get("precipitation"), "is_day": day,
        "at": cur.get("time"), "code": code,
        # When this Mac actually got it, which is not the same as the model's
        # own timestamp — and is what says whether it is still worth showing.
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lat": round(lat, 2), "lon": round(lon, 2),
    }
