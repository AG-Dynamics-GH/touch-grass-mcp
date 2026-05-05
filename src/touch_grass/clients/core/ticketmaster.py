"""Ticketmaster Discovery API client — concerts, sports, theater, comedy.

Fan-out strategy
----------------
Ticketmaster's Discovery API has a 1000-element deep-paging cap (size*page < 1000).
When NYC's full 14-day market returns ~2,300 events, a single paginated sweep
filtered by `city=New York` is dominated by Arts & Theatre (Banksy Museum +
Broadway = ~1,600 totalElements) and squeezes out Music and Sports — Knicks
playoff games and marquee MSG concerts disappear.

Fix:
  1. Use `dmaId=345` (NYC designated market area) instead of `city/stateCode`.
     The city filter excludes the Bronx (Yankee Stadium), Brooklyn (Barclays),
     Queens (Citi Field/Forest Hills), Long Island, NJ, and CT venues.
  2. When no specific category is requested, fan out across all four TM segments
     (Music, Sports, Arts & Theatre, Miscellaneous) in parallel and merge —
     each segment gets its own 1000-deep-paging budget.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import httpx

BASE_URL = "https://app.ticketmaster.com/discovery/v2"

# NYC DMA (designated market area). Covers 5 boroughs + Long Island + NJ + CT.
NYC_DMA_ID = 345

# All four TM Discovery segments. When no `category` arg is given, we fan out
# across these so each gets its own 1000-deep-paging budget.
ALL_SEGMENTS: tuple[str, ...] = ("Music", "Sports", "Arts & Theatre", "Miscellaneous")

# TM Discovery: per-page max = 200, deep-paging cap = 1000th element
# (size*page must stay < 1000). 5 pages × 200 = 1000 hard cap.
PAGE_SIZE = 200
MAX_PAGES = 5


def _api_key() -> str:
    key = os.environ.get("TICKETMASTER_API_KEY", "")
    if not key:
        raise RuntimeError("TICKETMASTER_API_KEY not set")
    return key


def _category_to_segment(category: str) -> str:
    segment_map = {
        "concerts": "Music",
        "music": "Music",
        "sports": "Sports",
        "theater": "Arts & Theatre",
        "comedy": "Arts & Theatre",
        "arts": "Arts & Theatre",
        "misc": "Miscellaneous",
        "miscellaneous": "Miscellaneous",
    }
    return segment_map.get(category.lower(), category)


async def search_events(
    *,
    keyword: str = "",
    city: str = "",
    state_code: str = "",
    category: str = "",
    start_date: str = "",
    end_date: str = "",
    radius: int = 25,
    size: int = 200,
    dma_id: int | None = NYC_DMA_ID,
) -> list[dict]:
    """Search Ticketmaster.

    By default, queries the NYC DMA (id=345) and fans out across all four
    classification segments to bypass the 1000-deep-paging cap that would
    otherwise let Arts & Theatre crowd out Music and Sports.

    Pass `category="sports"` (or music/theater/etc.) to scope to one segment,
    or `keyword=` for a focused search; both bypass the fan-out.
    """
    base_params: dict = {
        "apikey": _api_key(),
        "locale": "*",
        "size": min(max(size, 1), PAGE_SIZE),
        "sort": "date,asc",
    }

    # DMA filter is preferred over city/state — DMA 345 covers all NYC boroughs,
    # Long Island, NJ, CT. City="New York" excludes Bronx/Brooklyn/Queens venues.
    if city or state_code:
        base_params["unit"] = "miles"
        base_params["radius"] = radius
        if city:
            base_params["city"] = city
        if state_code:
            base_params["stateCode"] = state_code
    elif dma_id is not None:
        base_params["dmaId"] = dma_id

    if keyword:
        base_params["keyword"] = keyword

    if start_date:
        base_params["startDateTime"] = _to_tm_datetime(start_date)
    else:
        base_params["startDateTime"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if end_date:
        base_params["endDateTime"] = _to_tm_datetime(end_date, end_of_day=True)

    # Decide fan-out vs single-segment. A specific category or keyword query
    # uses a single sweep; the default broad call splits across segments so
    # each gets its own 1000-deep-paging budget.
    if category:
        segments_to_query = [_category_to_segment(category)]
    elif keyword:
        segments_to_query = [""]  # no segment filter, single sweep
    else:
        segments_to_query = list(ALL_SEGMENTS)

    # When fanning out, give each segment its own deep-paging budget so a
    # high-volume segment (Arts & Theatre = 1500+ totalElements) doesn't
    # squeeze out low-volume ones (Sports ~100, Music ~250). The caller's
    # `size` becomes a final post-merge cap rather than a per-segment quota.
    if len(segments_to_query) > 1:
        per_segment_target = PAGE_SIZE * MAX_PAGES  # = 1000, the deep-paging cap
    else:
        per_segment_target = max(size, PAGE_SIZE)

    async with httpx.AsyncClient(timeout=20) as client:
        if len(segments_to_query) == 1:
            results = [
                await _paginate_segment(
                    client, base_params, segments_to_query[0], per_segment_target
                )
            ]
        else:
            results = await asyncio.gather(
                *(
                    _paginate_segment(client, base_params, seg, per_segment_target)
                    for seg in segments_to_query
                ),
                return_exceptions=True,
            )

    # Dedupe across segments by event id; flatten exceptions to empty.
    seen_ids: set[str] = set()
    collected: list[dict] = []
    for batch in results:
        if isinstance(batch, BaseException):
            continue
        for ev in batch:
            eid = ev.get("id", "")
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            collected.append(ev)

    # Sort merged result by date (segments returned in their own date order).
    collected.sort(key=lambda e: (e.get("date", ""), e.get("time", "")))

    # When fanning out, return everything we fetched — truncating by size
    # would drop late-window MSG events (date-sorted Arts & Theatre crowds
    # the front of the list). When the caller scoped to a single segment,
    # honor the size cap.
    if size and size > 0 and len(segments_to_query) == 1:
        return collected[:size]
    return collected


async def _paginate_segment(
    client: httpx.AsyncClient,
    base_params: dict,
    segment: str,
    target: int,
) -> list[dict]:
    """Page through one classification segment up to the deep-paging cap."""
    params = dict(base_params)
    if segment:
        params["classificationName"] = segment

    collected: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(MAX_PAGES):
        page_params = {**params, "page": page}
        resp = await client.get(f"{BASE_URL}/events.json", params=page_params)
        if resp.status_code != 200:
            # Deep-paging cap returns 400 with detail "Invalid page size & page combination".
            # Treat any non-2xx as end-of-stream rather than crash the whole fan-out.
            break
        data = resp.json()
        events = (data.get("_embedded") or {}).get("events", []) or []
        if not events:
            break
        for e in events:
            eid = e.get("id", "")
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            collected.append(_normalize(e))
            if target and len(collected) >= target:
                break
        if target and len(collected) >= target:
            break
        page_meta = data.get("page") or {}
        if page + 1 >= page_meta.get("totalPages", 0):
            break
    return collected


async def get_event_details(event_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/events/{event_id}.json",
            params={"apikey": _api_key()},
        )
        resp.raise_for_status()
        return _normalize(resp.json())


def _to_tm_datetime(date_str: str, *, end_of_day: bool = False) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(event: dict) -> dict:
    venue_data = {}
    venues = event.get("_embedded", {}).get("venues", [])
    if venues:
        v = venues[0]
        venue_data = {
            "venue_name": v.get("name", ""),
            "address": v.get("address", {}).get("line1", ""),
            "city": v.get("city", {}).get("name", ""),
            "state": v.get("state", {}).get("stateCode", ""),
        }

    dates = event.get("dates", {}).get("start", {})
    price_ranges = event.get("priceRanges", [])
    price_str = ""
    if price_ranges:
        low = price_ranges[0].get("min", "")
        high = price_ranges[0].get("max", "")
        currency = price_ranges[0].get("currency", "USD")
        price_str = f"${low}-${high} {currency}" if low and high else ""

    classifications = event.get("classifications", [{}])
    genre = classifications[0].get("genre", {}).get("name", "") if classifications else ""

    return {
        "provider": "ticketmaster",
        "id": event.get("id", ""),
        "name": event.get("name", ""),
        "date": dates.get("localDate", ""),
        "time": dates.get("localTime", ""),
        "genre": genre,
        "price": price_str,
        "url": event.get("url", ""),
        "image": (event.get("images", [{}])[0].get("url", "") if event.get("images") else ""),
        **venue_data,
    }
