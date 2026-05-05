"""TodayTix client — Broadway and off-Broadway tickets/shows.

Uses the undocumented mobile API at api.todaytix.com.
No user auth required for browsing shows. Returns real-time
pricing, rush/lottery flags, and showtimes.
"""

from __future__ import annotations

import httpx

BASE_URL = "https://api.todaytix.com/api/v2"

# TodayTix location IDs
LOCATIONS = {
    "new york": 1,
    "nyc": 1,
    "london": 2,
    "los angeles": 9,
    "washington dc": 12,
}


def _location_id(city: str) -> int:
    return LOCATIONS.get(city.lower(), 1)


def _headers() -> dict:
    return {
        "Accept": "application/json",
        "User-Agent": "TodayTix/4.0 (events-mcp)",
        "x-tt-app-version": "4.0.0",
    }


def _normalize_show(s: dict) -> dict:
    venue = s.get("venue", {})
    summary = s.get("summary", "") or s.get("description", "")
    if len(summary) > 250:
        summary = summary[:247] + "..."

    return {
        "id": s.get("id"),
        "name": s.get("displayName", "") or s.get("name", ""),
        "category": s.get("category", "") or s.get("type", ""),
        "venue_name": venue.get("name", "") if isinstance(venue, dict) else "",
        "address": venue.get("address1", "") if isinstance(venue, dict) else "",
        "neighborhood": venue.get("neighborhood", "") if isinstance(venue, dict) else "",
        "summary": summary,
        "rating": s.get("rating"),
        "is_rush": s.get("isRushAvailable", False),
        "is_lottery": s.get("isLotteryAvailable", False),
        "min_price": s.get("minPrice"),
        "max_price": s.get("maxPrice"),
        "currency": s.get("currency", "USD"),
        "url": f"https://www.todaytix.com/nyc/shows/{s.get('id')}" if s.get("id") else "",
        "image": s.get("imageUrl", "") or s.get("posterImageUrl", ""),
        "source": "todaytix",
    }


async def list_shows(
    city: str = "new york",
    limit: int = 30,
) -> list[dict]:
    """List currently available shows in a city."""
    loc = _location_id(city)
    params = {"location": loc, "limit": limit}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/shows",
            headers=_headers(),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    shows = data.get("data", []) or data.get("shows", [])
    return [_normalize_show(s) for s in shows[:limit]]


async def get_show_details(show_id: int) -> dict:
    """Get full details for a specific show."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/shows/{show_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    show = data.get("data", data)
    return _normalize_show(show)


async def get_showtimes(show_id: int) -> list[dict]:
    """Get available showtimes/performances for a show."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/shows/{show_id}/showtimes",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    times = data.get("data", []) or data.get("showtimes", [])
    return [
        {
            "id": t.get("id"),
            "datetime": t.get("datetime", "") or t.get("localDatetime", ""),
            "min_price": t.get("minPrice"),
            "max_price": t.get("maxPrice"),
            "available": t.get("hasAvailability", True),
        }
        for t in times
    ]
