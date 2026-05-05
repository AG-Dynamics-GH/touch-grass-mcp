"""Dice.fm client — indie/electronic concerts and DJ events.

api.dice.fm endpoints have been retired. Now scrapes events from the
SSR'd __NEXT_DATA__ JSON on dice.fm/browse/{city-slug}. No auth needed.
City slugs are perm_name + city_id (e.g. new_york-5bbf4db0f06331478e9b2c59).
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import httpx

BROWSE_URL = "https://dice.fm/browse"

# City slugs from dice.fm/browse pageProps.city (perm_name-id)
CITY_SLUGS = {
    "new york": "new_york-5bbf4db0f06331478e9b2c59",
    "nyc": "new_york-5bbf4db0f06331478e9b2c59",
    "brooklyn": "new_york-5bbf4db0f06331478e9b2c59",
    "manhattan": "new_york-5bbf4db0f06331478e9b2c59",
}


def _city_slug(city: str) -> str:
    return CITY_SLUGS.get(city.lower(), CITY_SLUGS["new york"])


def _extract_next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _headers() -> dict:
    return {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    }


def _normalize_event(e: dict) -> dict:
    venue = e.get("venue", {}) or e.get("venues", [{}])[0] if e.get("venues") else {}
    if isinstance(e.get("venues"), list) and e["venues"]:
        venue = e["venues"][0]
    elif "venue" in e and isinstance(e["venue"], dict):
        venue = e["venue"]

    # New (2026): dates live under e['dates']['event_start_date'] (ISO with tz).
    # Legacy: e['date'] or e['event_date']. Fall back to date_unix epoch.
    dates_obj = e.get("dates") or {}
    date_raw = dates_obj.get("event_start_date") or e.get("date") or e.get("event_date") or ""
    date_str = ""
    time_str = ""
    if date_raw:
        try:
            dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            date_str = date_raw[:10]
    elif e.get("date_unix"):
        try:
            dt = datetime.fromtimestamp(e["date_unix"])
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError, OSError):
            pass

    artists = e.get("summary_lineup") or e.get("artists", []) or e.get("lineup", [])
    artist_names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in artists]

    return {
        "id": e.get("id") or e.get("perm_name", ""),
        "name": e.get("name", ""),
        "date": date_str,
        "time": time_str,
        "venue_name": venue.get("name", ""),
        "city": venue.get("city", {}).get("name", "")
        if isinstance(venue.get("city"), dict)
        else venue.get("city", ""),
        "address": venue.get("address", ""),
        "artists": [n for n in artist_names if n],
        "genre": ", ".join(e.get("genre_tags", []) or e.get("genres", [])),
        "url": e.get("url") or f"https://dice.fm/event/{e.get('perm_name', '')}",
        "image": (e.get("event_images", {}) or {}).get("landscape", "")
        or (e.get("featured_image") or ""),
        "price_min": e.get("price_min"),
        "currency": e.get("currency", ""),
        "sold_out": e.get("sold_out", False),
        "source": "dice",
    }


async def search_events(
    city: str = "new york",
    limit: int = 24,
) -> list[dict]:
    """Browse upcoming events in a city on Dice."""
    slug = _city_slug(city)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(
            f"{BROWSE_URL}/{slug}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = _extract_next_data(resp.text)

    events = data.get("props", {}).get("pageProps", {}).get("events", []) or []
    return [_normalize_event(e) for e in events[:limit]]


async def search_by_query(
    query: str,
    city: str = "new york",
    limit: int = 24,
) -> list[dict]:
    """Search events by artist/keyword. Falls back to filter-then-keyword on browse."""
    all_events = await search_events(city=city, limit=200)
    q = query.lower()
    filtered = [
        e
        for e in all_events
        if q in (e.get("name", "") or "").lower()
        or q in " ".join(e.get("artists", []) or []).lower()
    ]
    return filtered[:limit]
