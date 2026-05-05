"""Carnegie Hall client — classical, jazz, world, and recital programs.

Carnegie's public ``/Calendar`` page is an Alpine.js shell that loads its event
list from Algolia at runtime. The Algolia application id, search-only API key,
and index name are all baked into the public ``FacetedEventSearch.js`` bundle —
they're frontend search keys, intended for browser use, so we use the same ones
to query the index directly. No auth, no scraping.

Algolia config (extracted from /scripts/Algolia/FacetedEventSearch.js):

    applicationId : "Q0TMLOPF1J"
    searchOnlyKey : "d2d2b382f2659c44ef8927aad7a24172"
    index         : "prod_Events"

Each hit has ``startdate`` (epoch ms), ``title``, ``url`` (relative), ``time``
(human string e.g. "7 PM"), ``date`` (e.g. "Friday, May 1, 2026"), ``facility``
(specific Carnegie hall — Stern, Zankel, Weill), ``webdisplayperformers``,
and ``genre`` (sometimes via faceted refinement; absent on the bare hit).

Events take place at one of three halls inside 881 7th Ave, Midtown West.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

ALGOLIA_APP_ID = "Q0TMLOPF1J"
ALGOLIA_SEARCH_TOKEN = "d2d2b382f2659c44ef8927aad7a24172"  # public search-only key, example placeholder — extracted from carnegiehall.org frontend JS
ALGOLIA_INDEX = "prod_Events"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

VENUE_PARENT = "Carnegie Hall"
VENUE_ADDRESS = "881 7th Avenue, New York, NY 10019"
NEIGHBORHOOD = "Midtown West"

_HEADERS = {
    "X-Algolia-API-Key": ALGOLIA_SEARCH_TOKEN,
    "X-Algolia-Application-Id": ALGOLIA_APP_ID,
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


def _to_epoch_ms(date_str: str) -> int:
    """YYYY-MM-DD → epoch milliseconds (UTC midnight)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _parse_human_time(t: str) -> str:
    """'7 PM' / '7:30 PM' → '19:00' / '19:30'. Returns '' on failure."""
    if not t:
        return ""
    raw = t.strip().upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return ""


def _classify_genre(title: str, performers: str) -> str:
    """Carnegie's Algolia hits don't expose ``genre`` on the bare record without
    a faceted refinement query. Heuristic from title + performers covers the
    common cases: classical (recitals/orchestras/chamber), jazz, world, vocal.
    """
    blob = f"{title} {performers}".lower()
    if any(k in blob for k in ("jazz", "blue note", "saxophone")):
        return "Jazz"
    if any(
        k in blob
        for k in (
            "recital",
            "quartet",
            "trio",
            "symphony",
            "orchestra",
            "philharmonic",
            "sonata",
            "chamber",
        )
    ):
        return "Classical"
    if any(
        k in blob for k in ("vocal", "soprano", "tenor", "baritone", "mezzo", "lieder", "songbook")
    ):
        return "Vocal"
    if any(k in blob for k in ("global", "world", "afro", "latin", "flamenco")):
        return "World"
    return "Concert"


def _hit_date(start_ms: object) -> str:
    """Convert Carnegie's epoch-ms ``startdate`` to YYYY-MM-DD; '' on failure."""
    if not start_ms:
        return ""
    try:
        dt_utc = datetime.fromtimestamp(int(start_ms) / 1000, tz=UTC)
        return dt_utc.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def _hit_url(rel: str) -> str:
    if rel.startswith("/"):
        return f"https://www.carnegiehall.org{rel}"
    return rel or "https://www.carnegiehall.org/Calendar"


def _hit_description(performers: str, facility: str) -> str:
    parts = [performers]
    if facility and facility != VENUE_PARENT:
        parts.append(f"at {facility}")
    return " — ".join(p for p in parts if p)


def _normalize_hit(hit: dict) -> dict | None:
    title = hit.get("title", "") or hit.get("_name", "")
    if not title:
        return None
    date_str = _hit_date(hit.get("startdate"))
    if not date_str:
        return None

    facility = hit.get("facility") or hit.get("facilityfacet") or VENUE_PARENT
    performers = hit.get("webdisplayperformers", "") or ""
    time_str = _parse_human_time(hit.get("time", ""))
    full_url = _hit_url(hit.get("url", "") or "")
    eid = hit.get("objectID") or hit.get("chronid") or hit.get("_id", "")
    description = _hit_description(performers, facility)

    return {
        "provider": "carnegie_hall",
        "external_id": str(eid),
        "name": title,
        "date": date_str,
        "time": time_str,
        "venue_name": facility,
        "address": VENUE_ADDRESS,
        "neighborhood": NEIGHBORHOOD,
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": _classify_genre(title, performers),
        "price": "",
        "url": full_url,
        "image": "",
        "description": description,
    }


def _fetch_page(start_ms: int, end_ms: int, page: int, hits_per_page: int) -> dict:
    body = {
        "query": "",
        "page": page,
        "hitsPerPage": hits_per_page,
        "numericFilters": [f"startdate >= {start_ms}", f"startdate < {end_ms}"],
    }
    with httpx.Client(timeout=20, headers=_HEADERS) as client:
        resp = client.post(ALGOLIA_URL, json=body)
        resp.raise_for_status()
        return resp.json()


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Fetch Carnegie Hall events whose ``startdate`` falls in [start_date, end_date)."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    start_ms = _to_epoch_ms(start_date)
    # Inclusive end: bump end_date by one day so events on end_date are included.
    end_ms = _to_epoch_ms(
        (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    )

    events: list[dict] = []
    page = 0
    hits_per_page = 100
    while True:
        try:
            data = await asyncio.to_thread(_fetch_page, start_ms, end_ms, page, hits_per_page)
        except (httpx.HTTPError, httpx.TimeoutException):
            break
        for hit in data.get("hits", []):
            normalized = _normalize_hit(hit)
            if normalized:
                events.append(normalized)
        nb_pages = int(data.get("nbPages", 1))
        page += 1
        if page >= nb_pages:
            break
    return events


# Convenience alias for the ingest harness.
async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
