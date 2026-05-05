"""MoMA talks/programs client — calendar events at moma.org.

Strategy:
  1. Fetch /calendar pages (paginated via ?page=N) to harvest event ids.
  2. For each id, fetch /calendar/events/<id> and parse the inline
     <script type="application/ld+json"> Event blob.

JSON-LD gives us name, startDate, endDate, location.address, description,
image, url — everything we need with no HTML scraping fragility.

We deliberately exclude general "gallery admission" / "Museum admission"
listings; only programmed events appear under /calendar/events/<id>.
We further filter out the noisiest categories ("Family Story Time",
"Reading Group" cohorts, etc.) is left to the consumer because MoMA's
own page already only surfaces programmed events. Genre is inferred
from event-name keywords.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os as _os
import re
from datetime import datetime, timedelta

import httpx
from curl_cffi import requests as cf_requests


def _impersonate_kw():
    """Return {} or {"impersonate": "chrome"} based on TOUCH_GRASS_NYC_IMPERSONATE."""
    if _os.environ.get("TOUCH_GRASS_NYC_IMPERSONATE", "").lower() in ("true", "1", "yes"):
        return {"impersonate": "chrome"}
    return {}


logger = logging.getLogger(__name__)

CALENDAR_URL = "https://www.moma.org/calendar"
EVENT_DETAIL_URL = "https://www.moma.org/calendar/events/{eid}"
VENUE_NAME = "The Museum of Modern Art"
VENUE_ADDRESS = "11 West 53 Street, New York, NY 10019"
VENUE_NEIGHBORHOOD = "Midtown"

# How many calendar list pages to walk. MoMA programs roll about 14 days
# forward per page; 5 covers ~10 weeks which is plenty for a 14-day ingest.
_MAX_LIST_PAGES = 5

# Per-event detail concurrency. MoMA tolerates this; keep modest to be polite.
_DETAIL_CONCURRENCY = 6

_GENRE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bopen(?:ing)?\b", re.I), "Opening"),
    (re.compile(r"\blecture\b|\bsymposium\b", re.I), "Lecture"),
    (re.compile(r"\bconversation\b|\bin\s+conversation\b|\bdiscussion\b", re.I), "Talk"),
    (re.compile(r"\bgallery\s*(?:talk|tour)\b", re.I), "Gallery Talk"),
    (re.compile(r"\bfilm\b|\bscreening\b|\bcinema\b", re.I), "Film"),
    (re.compile(r"\bperformance\b|\bconcert\b|\brecital\b", re.I), "Performance"),
    (re.compile(r"\bworkshop\b|\bstudio\b|\bclass\b", re.I), "Workshop"),
    (re.compile(r"\btalk\b|\bartist\s+talk\b", re.I), "Talk"),
]


def _classify_genre(name: str, description: str) -> str:
    haystack = f"{name} {description[:300]}"
    for pat, label in _GENRE_RULES:
        if pat.search(haystack):
            return label
    return "Talk"


def _impersonate_get_text(url: str) -> str:
    """curl_cffi blocking GET. MoMA sometimes returns 403 to stock httpx UAs."""
    r = cf_requests.get(url, **_impersonate_kw(), timeout=20)
    r.raise_for_status()
    return r.text


def _harvest_event_ids(html: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"/calendar/events/(\d+)", html)))


def _extract_ld_json(html: str) -> dict | None:
    blocks = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.+?)</script>',
        html,
        re.DOTALL,
    )
    for blk in blocks:
        try:
            data = json.loads(blk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Event":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Event":
                    return item
    return None


def _split_iso(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    # MoMA emits "2026-04-29T10:30" (no seconds, no tz)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10], (value[11:16] if len(value) > 11 else "")
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _normalize(eid: str, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    date_str, time_str = _split_iso(data.get("startDate", ""))
    end_date, end_time = _split_iso(data.get("endDate", ""))
    loc = data.get("location") or {}
    address = ""
    venue_name = VENUE_NAME
    if isinstance(loc, dict):
        if isinstance(loc.get("address"), str):
            address = loc["address"]
        elif isinstance(loc.get("address"), dict):
            a = loc["address"]
            parts = [
                a.get("streetAddress", ""),
                a.get("addressLocality", ""),
                a.get("addressRegion", ""),
                a.get("postalCode", ""),
            ]
            address = ", ".join(p for p in parts if p)
        sub_name = loc.get("name", "")
        if sub_name and sub_name != venue_name:
            # e.g. "Education Center" — append for context
            venue_name = f"{VENUE_NAME} — {sub_name}"

    image = data.get("image") or ""
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")

    return {
        "provider": "moma_talks",
        "external_id": eid,
        "name": name,
        "date": date_str,
        "time": time_str,
        "end_date": end_date,
        "end_time": end_time,
        "venue_name": venue_name,
        "address": address or VENUE_ADDRESS,
        "neighborhood": VENUE_NEIGHBORHOOD,
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": _classify_genre(name, description),
        "price": "",
        "url": data.get("url") or EVENT_DETAIL_URL.format(eid=eid),
        "image": image,
        "description": description[:1200],
    }


async def _gather_event_ids() -> list[str]:
    ids: list[str] = []
    for page in range(1, _MAX_LIST_PAGES + 1):
        url = CALENDAR_URL if page == 1 else f"{CALENDAR_URL}?page={page}"
        try:
            html = await asyncio.to_thread(_impersonate_get_text, url)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("moma_talks: page %s fetch failed: %s", page, exc)
            continue
        page_ids = _harvest_event_ids(html)
        if not page_ids:
            break
        new_ids = [i for i in page_ids if i not in ids]
        ids.extend(new_ids)
        if not new_ids:
            # next page yielded only duplicates → calendar has likely ended
            break
    return ids


async def _fetch_detail(client: httpx.AsyncClient, eid: str, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        url = EVENT_DETAIL_URL.format(eid=eid)
        try:
            # MoMA detail pages aren't Cloudflare-shielded; httpx works fine.
            r = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            if r.status_code == 403:
                # fall back to curl_cffi for this id
                html = await asyncio.to_thread(_impersonate_get_text, url)
            else:
                r.raise_for_status()
                html = r.text
        except Exception as exc:  # noqa: BLE001
            logger.warning("moma_talks: detail %s failed: %s", eid, exc)
            return None
    data = _extract_ld_json(html)
    if not data:
        return None
    try:
        return _normalize(eid, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("moma_talks: normalize %s failed: %s", eid, exc)
        return None


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Fetch MoMA programmed events; defaults to a 14-day forward window."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    ids = await _gather_event_ids()
    if not ids:
        return []

    sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        tasks = [_fetch_detail(client, eid, sem) for eid in ids]
        results = await asyncio.gather(*tasks)

    events = [e for e in results if e and e.get("date")]
    return [e for e in events if start_date <= e["date"] <= end_date]


async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
