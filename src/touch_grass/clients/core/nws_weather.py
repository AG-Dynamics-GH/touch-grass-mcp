"""National Weather Service forecast client — 100% free, no API key.

Two-step: resolve grid coordinates for NYC once, then fetch 7-day forecast.
Docs: https://www.weather.gov/documentation/services-web-api
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

_ET = ZoneInfo("America/New_York")
_HEADERS = {
    "User-Agent": "(touch-grass-mcp, noreply@example.com)",
    "Accept": "application/geo+json",
}

_NYC_LAT, _NYC_LON = 40.7128, -74.0060
_GRID_CACHE: dict[str, str] = {}

_BAD_WEATHER = re.compile(
    r"rain|snow|thunderstorm|sleet|ice|freezing|hail|tornado|hurricane", re.IGNORECASE
)


async def _resolve_grid() -> str:
    if "forecast_url" in _GRID_CACHE:
        return _GRID_CACHE["forecast_url"]

    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
        resp = await client.get(f"https://api.weather.gov/points/{_NYC_LAT},{_NYC_LON}")
        resp.raise_for_status()
        data = resp.json()

    url = data["properties"]["forecast"]
    _GRID_CACHE["forecast_url"] = url
    return url


async def get_nyc_forecast(days: int = 7) -> list[dict]:
    """Fetch NWS 7-day forecast for NYC, returning one entry per period."""
    forecast_url = await _resolve_grid()

    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
        resp = await client.get(forecast_url)
        resp.raise_for_status()
        data = resp.json()

    periods = data.get("properties", {}).get("periods", [])
    results = []

    for p in periods:
        start = datetime.fromisoformat(p["startTime"])
        precip = p.get("probabilityOfPrecipitation", {}).get("value")
        precip_pct = precip if precip is not None else 0
        conditions = p.get("shortForecast", "")
        outdoor = not _BAD_WEATHER.search(conditions) and precip_pct < 40

        results.append(
            {
                "date": start.astimezone(_ET).strftime("%Y-%m-%d"),
                "day_name": p.get("name", start.strftime("%A")),
                "temperature": f"{p['temperature']}°{p.get('temperatureUnit', 'F')}",
                "conditions": conditions,
                "detailed": p.get("detailedForecast", ""),
                "precip_chance": f"{precip_pct}%",
                "wind": f"{p.get('windDirection', '')} {p.get('windSpeed', '')}".strip(),
                "outdoor_friendly": outdoor,
                "is_daytime": p.get("isDaytime", True),
            }
        )

    if days < 7:
        seen_dates: set[str] = set()
        trimmed = []
        for r in results:
            seen_dates.add(r["date"])
            if len(seen_dates) > days:
                break
            trimmed.append(r)
        return trimmed

    return results


def format_forecast(forecast: list[dict]) -> str:
    if not forecast:
        return "Weather forecast unavailable."

    lines = ["**NYC Weather Forecast**", ""]
    current_date = ""
    for f in forecast:
        if f["date"] != current_date:
            current_date = f["date"]
            lines.append(f"**{f['day_name']}** ({f['date']})")

        icon = "☀️" if f["outdoor_friendly"] else "🌧️"
        if not f["is_daytime"]:
            icon = "🌙"
        lines.append(
            f"  {icon} {f['temperature']} — {f['conditions']} (precip {f['precip_chance']}, wind {f['wind']})"
        )

    return "\n".join(lines)
