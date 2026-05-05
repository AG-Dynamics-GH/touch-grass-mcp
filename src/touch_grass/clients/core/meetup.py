"""Meetup client — PUBLIC SCRAPER (no API key required).

Meetup deprecated their public REST API and the GraphQL endpoint requires OAuth.
This client scrapes events from the SSR'd ``__APOLLO_STATE__`` JSON embedded in
``meetup.com/find`` (Next.js page). MEETUP_API_KEY is intentionally not used.

Coverage strategy: a default location-only sweep returns ~16 events. Keyword
sweeps return 30-80 each. To match the row counts of the paid providers, this
module fans out across a vetted keyword list and dedupes by event id, yielding
hundreds of unique events per call.

Failure mode: if the find page layout changes and apollo state is empty, the
client returns ``[]`` rather than crashing the ingest run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

FIND_URL = "https://www.meetup.com/find/"

CITY_SLUGS = {
    "new york": "us--ny--new_york",
    "nyc": "us--ny--new_york",
    "brooklyn": "us--ny--new_york",  # Meetup groups NYC by metro
    "manhattan": "us--ny--new_york",
}

# Vetted fan-out keywords — chosen to broaden coverage without pulling junk.
# Mirrors the canonical KEYWORD_SWEEP from server.py minus low-yield terms.
_FANOUT_KEYWORDS = [
    "",  # default location-only sweep
    "tech",
    "run",
    "yoga",
    "art",
    "music",
    "book",
    "food",
    "startup",
    "hike",
    "comedy",
    "film",
    "wellness",
    "social",
    "networking",
]


def _city_slug(city: str) -> str:
    return CITY_SLUGS.get(city.lower(), CITY_SLUGS["new york"])


def _headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    }


def _extract_apollo_state(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {}) or {}


def _resolve_ref(ref: dict | None, apollo: dict) -> dict:
    if not isinstance(ref, dict):
        return {}
    key = ref.get("__ref")
    if not key:
        return ref
    return apollo.get(key, {}) or {}


def _normalize_event(e: dict, apollo: dict) -> dict:
    group = _resolve_ref(e.get("group"), apollo)
    photo = _resolve_ref(e.get("featuredEventPhoto"), apollo)
    venue_obj = _resolve_ref(e.get("venue"), apollo) if isinstance(e.get("venue"), dict) else {}
    fee = e.get("feeSettings") or {}

    dt_str = e.get("dateTime", "") or ""
    date = ""
    time = ""
    if dt_str:
        try:
            dt = datetime.fromisoformat(dt_str)
            date = dt.strftime("%Y-%m-%d")
            time = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            date = dt_str[:10]
            time = dt_str[11:16] if len(dt_str) > 11 else ""

    price_str = ""
    if isinstance(fee, dict) and fee.get("amount"):
        price_str = f"${fee['amount']} {fee.get('currency', 'USD')}"
    elif not fee:
        price_str = "Free"

    # Prefer real venue when present, fall back to group name as venue proxy.
    venue_name = ""
    address = ""
    city = ""
    state = ""
    if venue_obj:
        venue_name = venue_obj.get("name", "") or ""
        address = venue_obj.get("address", "") or ""
        city = venue_obj.get("city", "") or ""
        state = venue_obj.get("state", "") or ""
    if not venue_name:
        venue_name = group.get("name", "")

    return {
        "provider": "meetup",
        "id": str(e.get("id", "")),
        "name": e.get("title", ""),
        "date": date,
        "time": time,
        "genre": "",
        "price": price_str,
        "url": e.get("eventUrl", ""),
        "image": photo.get("highResUrl") or photo.get("source") or "",
        "venue_name": venue_name,
        "address": address,
        "city": city or "New York",
        "state": state or "NY",
        "group_name": group.get("name", ""),
        "description": (e.get("description", "") or "")[:300],
    }


async def _fetch_keyword(client: httpx.AsyncClient, slug: str, keyword: str) -> list[dict]:
    """Fetch a single keyword sweep and return normalized events."""
    params = {"location": slug, "source": "EVENTS"}
    if keyword:
        params["keywords"] = keyword
    try:
        resp = await client.get(FIND_URL, headers=_headers(), params=params)
        resp.raise_for_status()
        apollo = _extract_apollo_state(resp.text)
    except httpx.HTTPError as e:
        logger.warning("meetup fetch failed (kw=%r): %s", keyword, e)
        return []
    events_raw = [v for k, v in apollo.items() if k.startswith("Event:")]
    return [_normalize_event(e, apollo) for e in events_raw]


async def search_events(
    *,
    keyword: str = "",
    city: str = "New York",
    category: str = "",  # noqa: ARG001 — kept for interface compat
    start_date: str = "",
    end_date: str = "",
    radius: int = 25,  # noqa: ARG001
    size: int = 20,
) -> list[dict]:
    """Scrape Meetup find page for events near `city`.

    No API key needed — this is a public scraper. ``MEETUP_API_KEY`` env var is
    ignored. If a keyword is given, runs a single targeted sweep. Otherwise fans
    out across a vetted keyword list to broaden coverage beyond the ~16 events
    returned by a location-only sweep.
    """
    # If env explicitly set, log once that we're ignoring it (helps debug).
    if os.environ.get("MEETUP_API_KEY"):
        logger.debug("MEETUP_API_KEY is set but ignored — meetup.py uses a public scraper.")

    slug = _city_slug(city)

    keywords = [keyword] if keyword else _FANOUT_KEYWORDS

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # Run keyword sweeps in parallel (Meetup's CDN handles this fine; if it
        # rate-limits we'll see HTTPError logs and just get fewer rows).
        results = await asyncio.gather(
            *[_fetch_keyword(client, slug, kw) for kw in keywords],
            return_exceptions=True,
        )

    seen_ids: set[str] = set()
    out: list[dict] = []
    for batch in results:
        if not isinstance(batch, list):
            continue
        for e in batch:
            eid = e.get("id", "")
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            d = e.get("date", "")
            if start_date and d and d < start_date:
                continue
            if end_date and d and d > end_date:
                continue
            out.append(e)
            if len(out) >= size:
                return out
    return out
