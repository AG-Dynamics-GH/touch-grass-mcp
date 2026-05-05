"""NYC Open Data (Socrata) client — free city events from data.cityofnewyork.us.

No API key required. Optional app token for higher rate limits (1k→10k/hour).
Dataset: NYC Events Permits & related event feeds.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import httpx

BASE_URL = "https://data.cityofnewyork.us/resource"

EVENTS_DATASET = "tvst-q49a"
PARKS_EVENTS = "6v4b-5gp4"


def _app_token() -> str:
    return os.environ.get("NYC_OPENDATA_TOKEN", "")


def _headers() -> dict:
    token = _app_token()
    if token:
        return {"X-App-Token": token}
    return {}


async def search_events(
    *,
    keyword: str = "",
    start_date: str = "",
    end_date: str = "",
    borough: str = "",
    size: int = 20,
) -> list[dict]:
    """Search NYC events from the city's open data portal."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

    params: dict = {
        "$limit": min(size, 50),
        "$order": "start_date_time ASC",
        "$where": f"start_date_time >= '{start_date}T00:00:00' AND start_date_time <= '{end_date}T23:59:59'",
    }

    if keyword:
        params["$where"] += f" AND upper(event_name) like '%{keyword.upper()}%'"

    if borough:
        params["$where"] += f" AND upper(event_borough) = '{borough.upper()}'"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/{EVENTS_DATASET}.json",
            params=params,
            headers=_headers(),
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        records = resp.json()

    return [_normalize_event(r) for r in records]


async def search_parks_events(
    *,
    keyword: str = "",
    start_date: str = "",
    end_date: str = "",
    size: int = 20,
) -> list[dict]:
    """Search NYC Parks Department events."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

    params: dict = {
        "$limit": min(size, 50),
        "$order": "startdate ASC",
        "$where": f"startdate >= '{start_date}T00:00:00' AND startdate <= '{end_date}T23:59:59'",
    }

    if keyword:
        params["$where"] += f" AND upper(title) like '%{keyword.upper()}%'"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/{PARKS_EVENTS}.json",
            params=params,
            headers=_headers(),
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        records = resp.json()

    return [_normalize_parks(r) for r in records]


def _normalize_event(record: dict) -> dict:
    start = record.get("start_date_time", "")
    date = start[:10] if start else ""
    time = start[11:16] if len(start) > 11 else ""

    return {
        "provider": "nyc_opendata",
        "id": record.get("event_id", record.get(":id", "")),
        "name": record.get("event_name", ""),
        "date": date,
        "time": time,
        "genre": record.get("event_type", ""),
        "price": "Free"
        if record.get("event_type", "").lower() in ("free", "street fair", "plaza program")
        else "",
        "url": "",
        "image": "",
        "venue_name": record.get("event_location", ""),
        "address": record.get("event_street_side", ""),
        "city": "New York",
        "state": "NY",
        "borough": record.get("event_borough", ""),
        "description": record.get("event_name", ""),
    }


def _normalize_parks(record: dict) -> dict:
    start = record.get("startdate", "")
    date = start[:10] if start else ""
    time = start[11:16] if len(start) > 11 else ""

    return {
        "provider": "nyc_parks",
        "id": record.get("uid", record.get(":id", "")),
        "name": record.get("title", ""),
        "date": date,
        "time": time,
        "genre": record.get("categories", ""),
        "price": "Free",
        "url": record.get("link", ""),
        "image": record.get("image", ""),
        "venue_name": record.get("location", ""),
        "address": "",
        "city": "New York",
        "state": "NY",
        "description": (record.get("snippet", "") or "")[:300],
    }
