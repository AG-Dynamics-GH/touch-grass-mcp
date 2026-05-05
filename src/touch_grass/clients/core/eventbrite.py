"""Eventbrite API client — local/community events, niche gatherings.

Uses the POST /v3/destination/search/ endpoint (the old GET /v3/events/search/
was deprecated and returns 404). Custom date_range is broken in this API —
we use presets (today, this_week, etc.) and post-filter by date.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

BASE_URL = "https://www.eventbriteapi.com/v3"

CITY_PLACE_IDS = {
    "new york": "85977539",
    "nyc": "85977539",
    "brooklyn": "85977539",
    "manhattan": "85977539",
    "los angeles": "85923517",
    "san francisco": "85922583",
    "chicago": "85940195",
    "miami": "85933541",
    "austin": "85929045",
}


def _token() -> str:
    token = os.environ.get("EVENTBRITE_API_KEY", "")
    if not token:
        raise RuntimeError("EVENTBRITE_API_KEY not set")
    return token


def _pick_date_preset(start_date: str, end_date: str) -> str:
    """Map a date range to the closest Eventbrite preset."""
    if not start_date or not end_date:
        return "current_future"
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return "current_future"
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    span = (end - start).days
    offset = (start - today).days
    if offset == 0 and span <= 1:
        return "today"
    if offset <= 1 and span <= 1:
        return "tomorrow"
    if offset <= 0 and span <= 7:
        return "this_week"
    if offset <= 7 and span <= 7:
        return "next_week"
    if offset <= 0 and span <= 31:
        return "this_month"
    return "current_future"


async def search_events(
    *,
    keyword: str = "",
    city: str = "New York",
    category: str = "",
    start_date: str = "",
    end_date: str = "",
    free_only: bool = False,
    size: int = 20,
) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }

    place_id = CITY_PLACE_IDS.get(city.lower(), "85977539")
    date_preset = _pick_date_preset(start_date, end_date)

    body: dict = {
        "event_search": {
            "dates": date_preset,
            "places": [place_id],
        },
        "expand.destination_event": ["primary_venue", "image", "ticket_availability"],
    }

    if keyword:
        body["event_search"]["q"] = keyword
    if free_only:
        body["event_search"]["price"] = "free"
    if category:
        cat_id = _category_id(category)
        if cat_id:
            body["event_search"]["tags"] = [cat_id]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE_URL}/destination/search/",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("events", {}).get("results", [])
    normalized = [_normalize(e) for e in results]

    if start_date and end_date:
        normalized = [e for e in normalized if start_date <= (e.get("date") or "") <= end_date]

    return normalized[:size]


def _category_id(category: str) -> str:
    mapping = {
        "concerts": "EventbriteCategory/103",
        "food_drink": "EventbriteCategory/110",
        "fitness": "EventbriteCategory/108",
        "social": "EventbriteCategory/113",
        "arts": "EventbriteCategory/105",
        "tech": "EventbriteCategory/102",
        "outdoor": "EventbriteCategory/108",
        "comedy": "EventbriteCategory/105",
        "sports": "EventbriteCategory/108",
    }
    return mapping.get(category, "")


# Categories worth fanning out for the personal-events sweep. Each call returns
# ~20 events; querying all of these in parallel yields ~120-150 unique events.
_FANOUT_CATEGORIES = ["concerts", "food_drink", "social", "arts", "tech", "comedy"]


async def search_events_fanout(
    *,
    city: str = "New York",
    start_date: str = "",
    end_date: str = "",
    size_per_category: int = 50,
) -> list[dict]:
    """Fan out across the major categories in parallel and dedupe by event id.

    Eventbrite's destination search returns ~20 events per call regardless of
    `size`. Fanning across 6 categories + 1 keyword-less default plus deduping
    gives substantially more useful coverage.
    """
    import asyncio

    coros = [
        search_events(
            city=city,
            start_date=start_date,
            end_date=end_date,
            size=size_per_category,
        )
    ]
    coros.extend(
        search_events(
            city=city,
            category=cat,
            start_date=start_date,
            end_date=end_date,
            size=size_per_category,
        )
        for cat in _FANOUT_CATEGORIES
    )
    results = await asyncio.gather(*coros, return_exceptions=True)

    seen: set[str] = set()
    merged: list[dict] = []
    for batch in results:
        if isinstance(batch, Exception) or not isinstance(batch, list):
            continue
        for ev in batch:
            ext_id = str(ev.get("id") or ev.get("external_id") or ev.get("url") or "")
            if not ext_id or ext_id in seen:
                continue
            seen.add(ext_id)
            merged.append(ev)
    return merged


def _normalize(event: dict) -> dict:
    venue = event.get("primary_venue", {})
    address = venue.get("address", {})
    image = event.get("image", {})

    start_date = event.get("start_date", "")
    start_time = event.get("start_time", "")

    ticket = event.get("ticket_availability", {})
    price_str = ""
    if ticket.get("is_free"):
        price_str = "Free"
    elif ticket.get("minimum_ticket_price"):
        p = ticket["minimum_ticket_price"]
        price_str = f"${p.get('major_value', '')} {p.get('currency', 'USD')}"

    name = event.get("name", "")
    if isinstance(name, dict):
        name = name.get("text", "")

    return {
        "provider": "eventbrite",
        "id": str(event.get("id", "")),
        "name": name,
        "date": start_date,
        "time": start_time[:5] if start_time else "",
        "genre": "",
        "price": price_str,
        "url": event.get("url", ""),
        "image": image.get("url", "") if isinstance(image, dict) else "",
        "venue_name": venue.get("name", ""),
        "address": address.get("localized_address_display", address.get("address_1", "")),
        "city": address.get("city", ""),
        "state": address.get("region", ""),
        "description": (event.get("summary", "") or "")[:300],
    }
