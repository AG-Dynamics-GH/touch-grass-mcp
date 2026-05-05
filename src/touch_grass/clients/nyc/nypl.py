"""New York Public Library events client — free talks, readings, film, workshops.

Uses the NYPL Refinery API (public, no key required).
Endpoint: https://refinery.nypl.org/api/nypl/ndo/v0.1/site-data/events
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx

BASE_URL = "https://refinery.nypl.org/api/nypl/ndo/v0.1/site-data/events"

_TAG_RE = re.compile(r"<[^>]+>")


async def search_events(
    *,
    keyword: str = "",
    start_date: str = "",
    end_date: str = "",
    borough: str = "",
    size: int = 20,
) -> list[dict]:
    """Search NYPL public events across all branch libraries."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

    params: dict[str, str | int] = {
        "limit": min(size, 50),
        "sort": "start-date",
        "direction": "ASC",
    }
    if keyword:
        params["keyword"] = keyword

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(BASE_URL, params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()

    events_raw = data.get("data", [])

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    filtered = []
    for e in events_raw:
        attrs = e.get("attributes", {})
        sd = attrs.get("start-date", "")
        if not sd:
            continue
        event_date = datetime.fromisoformat(sd.replace("Z", "+00:00")).replace(tzinfo=None)
        if start_dt <= event_date <= end_dt.replace(hour=23, minute=59, second=59):
            filtered.append(attrs)

    if borough:
        bl = borough.lower()
        filtered = [a for a in filtered if bl in (a.get("location-name", "") or "").lower()]

    return [_normalize(a) for a in filtered[:size]]


def _normalize(attrs: dict) -> dict:
    start = attrs.get("start-date", "")
    date = ""
    time = ""
    if start:
        if "T" in start:
            date = start[:10]
            time = start[11:16]
        else:
            date = start[:10]

    name = (attrs.get("name", "") or "").strip()
    name = _TAG_RE.sub("", name).replace("\xa0", " ").strip()

    desc_html = attrs.get("description-full", "") or attrs.get("description-short", "") or ""
    desc = _TAG_RE.sub("", desc_html).replace("\xa0", " ").strip()[:300]

    location = attrs.get("location-name", "") or ""
    event_url = attrs.get("permalink", "") or ""
    if event_url and not event_url.startswith("http"):
        event_url = f"https://www.nypl.org{event_url}"

    return {
        "provider": "nypl",
        "id": str(attrs.get("event-id", "")),
        "name": name,
        "date": date,
        "time": time,
        "genre": "library",
        "price": "Free",
        "url": event_url,
        "image": "",
        "venue_name": location,
        "address": "",
        "city": "New York",
        "state": "NY",
        "description": desc,
    }
