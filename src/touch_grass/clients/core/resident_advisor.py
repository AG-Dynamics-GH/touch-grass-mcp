"""Resident Advisor (RA) client — electronic music events and clubs.

Uses the public ra.co/graphql endpoint that powers the RA website.
No auth required. Best coverage for electronic/club events globally —
warehouse parties, DJ sets, club nights at venues like Nowadays,
Public Records, Mansions, Basement, House of Yes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

GRAPHQL_URL = "https://ra.co/graphql"

# RA area IDs for major cities (from ra.co/events/<country>/<city> URLs)
AREA_IDS = {
    "new york": 51,
    "nyc": 51,
    "brooklyn": 51,
    "manhattan": 51,
    "los angeles": 23,
    "san francisco": 26,
    "chicago": 117,
    "miami": 13,
    "london": 13,  # NOTE: London is also 13 in UK; resolved by country context
    "berlin": 34,
    "paris": 92,
    "amsterdam": 8,
    "tokyo": 49,
}


def _area_id(city: str) -> int:
    return AREA_IDS.get(city.lower(), 51)


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://ra.co/events",
        "Origin": "https://ra.co",
    }


EVENT_LISTINGS_QUERY = """
query GET_EVENT_LISTINGS(
  $filters: FilterInputDtoInput,
  $filterOptions: FilterOptionsInputDtoInput,
  $page: Int,
  $pageSize: Int
) {
  eventListings(
    filters: $filters,
    filterOptions: $filterOptions,
    pageSize: $pageSize,
    page: $page
  ) {
    data {
      id
      event {
        id
        title
        date
        startTime
        endTime
        contentUrl
        flyerFront
        venue {
          id
          name
          contentUrl
          area { name country { name } }
        }
        artists { id name }
      }
    }
    totalResults
  }
}
""".strip()


def _normalize_event(item: dict) -> dict:
    e = item.get("event", {})
    venue = e.get("venue") or {}
    area = venue.get("area") or {}
    country = area.get("country") or {}

    date_raw = e.get("date", "")
    start_raw = e.get("startTime", "")
    date_str = date_raw[:10] if date_raw else ""
    time_str = ""
    if start_raw:
        try:
            dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            time_str = start_raw[11:16] if len(start_raw) >= 16 else ""

    artists = e.get("artists") or []
    artist_names = [a.get("name", "") for a in artists if a.get("name")]

    content_url = e.get("contentUrl", "")
    event_url = f"https://ra.co{content_url}" if content_url.startswith("/") else content_url

    venue_url = venue.get("contentUrl", "") if venue else ""
    if venue_url.startswith("/"):
        venue_url = f"https://ra.co{venue_url}"

    return {
        "id": e.get("id", ""),
        "name": e.get("title", ""),
        "date": date_str,
        "time": time_str,
        "venue_name": venue.get("name", ""),
        "venue_url": venue_url,
        "city": area.get("name", ""),
        "country": country.get("name", ""),
        "artists": artist_names,
        "url": event_url,
        "image": e.get("flyerFront", ""),
        "source": "resident_advisor",
    }


async def search_events(
    city: str = "new york",
    start_date: str = "",
    end_date: str = "",
    limit: int = 20,
) -> list[dict]:
    """Browse electronic music events in a city for a date range.

    Args:
        city: City name (default: new york)
        start_date: ISO date YYYY-MM-DD (default: today)
        end_date: ISO date YYYY-MM-DD (default: 14 days from today)
        limit: Max results (default 20)
    """
    today = datetime.utcnow()
    start = start_date or today.strftime("%Y-%m-%d")
    end = end_date or (today + timedelta(days=14)).strftime("%Y-%m-%d")

    variables = {
        "filters": {
            "areas": {"eq": _area_id(city)},
            "listingDate": {
                "gte": f"{start}T00:00:00.000Z",
                "lte": f"{end}T23:59:59.000Z",
            },
        },
        "filterOptions": {"genre": True},
        "pageSize": limit,
        "page": 1,
    }

    payload = {
        "operationName": "GET_EVENT_LISTINGS",
        "variables": variables,
        "query": EVENT_LISTINGS_QUERY,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(GRAPHQL_URL, headers=_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()

    if "errors" in data:
        raise RuntimeError(f"RA GraphQL error: {data['errors']}")

    listings = (data.get("data") or {}).get("eventListings") or {}
    items = listings.get("data") or []
    return [_normalize_event(item) for item in items]


EVENT_DETAILS_QUERY = """
query GET_EVENT($id: ID!) {
  event(id: $id) {
    id
    title
    date
    startTime
    endTime
    contentUrl
    flyerFront
    content
    cost
    minimumAge
    venue {
      id
      name
      contentUrl
      address
      area { name country { name } }
    }
    artists { id name contentUrl }
    promoters { id name }
    genres { id name }
  }
}
""".strip()


async def get_event_details(event_id: str) -> dict:
    """Get full details for a specific RA event."""
    payload = {
        "operationName": "GET_EVENT",
        "variables": {"id": str(event_id)},
        "query": EVENT_DETAILS_QUERY,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(GRAPHQL_URL, headers=_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()

    if "errors" in data:
        raise RuntimeError(f"RA GraphQL error: {data['errors']}")

    e = (data.get("data") or {}).get("event")
    if not e:
        return {}

    venue = e.get("venue") or {}
    area = venue.get("area") or {}
    country = area.get("country") or {}

    return {
        "id": e.get("id", ""),
        "name": e.get("title", ""),
        "date": (e.get("date") or "")[:10],
        "start_time": e.get("startTime", ""),
        "end_time": e.get("endTime", ""),
        "venue_name": venue.get("name", ""),
        "venue_address": venue.get("address", ""),
        "city": area.get("name", ""),
        "country": country.get("name", ""),
        "description": e.get("content", ""),
        "cost": e.get("cost", ""),
        "min_age": e.get("minimumAge"),
        "artists": [a.get("name", "") for a in (e.get("artists") or [])],
        "promoters": [p.get("name", "") for p in (e.get("promoters") or [])],
        "genres": [g.get("name", "") for g in (e.get("genres") or [])],
        "image": e.get("flyerFront", ""),
        "url": f"https://ra.co{e.get('contentUrl', '')}"
        if e.get("contentUrl", "").startswith("/")
        else e.get("contentUrl", ""),
        "source": "resident_advisor",
    }
