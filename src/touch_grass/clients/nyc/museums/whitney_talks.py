"""Whitney Museum events client — talks, openings, performances at whitney.org.

Strategy mirrors moma_talks: walk paginated /events listing for slugs, then
fetch each event detail page and parse the inline application/ld+json Event.

Whitney's JSON-LD is rich (name, startDate, endDate, address, organizer,
description, etc.).  General "Whitney Biennial 15-min FFN" listings are
short-form gallery encounters — we keep them; downstream ranking handles
event-level filtering.
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

LISTING_URL = "https://whitney.org/events"
EVENT_DETAIL_URL = "https://whitney.org/events/{slug}"
VENUE_NAME = "Whitney Museum of American Art"
VENUE_ADDRESS = "99 Gansevoort Street, New York, NY 10014"
VENUE_NEIGHBORHOOD = "Meatpacking District"

_MAX_LIST_PAGES = 6
_DETAIL_CONCURRENCY = 6

_GENRE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"open(?:ing)?", re.I), "Opening"),
    (re.compile(r"lecture|symposium", re.I), "Lecture"),
    (re.compile(r"performance|concert|dj\b|recital|music", re.I), "Performance"),
    (re.compile(r"film|screening", re.I), "Film"),
    (re.compile(r"workshop|studio|class\b|drawing", re.I), "Workshop"),
    (re.compile(r"tour|gallery talk", re.I), "Gallery Talk"),
    (re.compile(r"talk|conversation|discussion|reading", re.I), "Talk"),
]


def _classify_genre(name: str, description: str) -> str:
    haystack = f"{name} {description[:300]}"
    for pat, label in _GENRE_RULES:
        if pat.search(haystack):
            return label
    return "Talk"


def _impersonate_get_text(url: str) -> str:
    r = cf_requests.get(url, **_impersonate_kw(), timeout=20)
    r.raise_for_status()
    return r.text


_EVENT_HREF_RE = re.compile(r'href="(/events/[^"#?]+)"')


def _harvest_slugs(html: str) -> list[str]:
    slugs: list[str] = []
    for match in _EVENT_HREF_RE.finditer(html):
        path = match.group(1)
        slug = path.rsplit("/", 1)[-1]
        if not slug or slug == "events":
            continue
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def _extract_ld_event(html: str) -> dict | None:
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
        candidates: list[dict] = []
        if isinstance(data, list):
            candidates = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            candidates = [data]
        for d in candidates:
            t = d.get("@type")
            if t == "Event" or (isinstance(t, list) and "Event" in t):
                return d
    return None


def _split_iso(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10], (value[11:16] if len(value) > 11 else "")
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = (
        s.replace("&amp;", "&")
        .replace("&amp;amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", s).strip()


def _normalize(slug: str, data: dict) -> dict | None:
    name = _strip_html((data.get("name") or "").strip())
    if not name:
        return None
    description = _strip_html((data.get("description") or "").strip())
    date_str, time_str = _split_iso(data.get("startDate", ""))
    if not date_str:
        return None
    end_date, end_time = _split_iso(data.get("endDate", ""))

    loc = data.get("location") or {}
    venue_name = VENUE_NAME
    address = VENUE_ADDRESS
    if isinstance(loc, dict):
        sub_name = loc.get("name", "")
        if sub_name and sub_name != VENUE_NAME and "whitney" not in sub_name.lower():
            venue_name = f"{VENUE_NAME} — {sub_name}"
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress", ""),
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("postalCode", ""),
            ]
            joined = ", ".join(p for p in parts if p)
            if joined:
                address = joined
        elif isinstance(addr, str) and addr:
            address = addr

    image = data.get("image") or ""
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")

    return {
        "provider": "whitney_talks",
        "external_id": data.get("@id") or slug,
        "name": name,
        "date": date_str,
        "time": time_str,
        "end_date": end_date,
        "end_time": end_time,
        "venue_name": venue_name,
        "address": address,
        "neighborhood": VENUE_NEIGHBORHOOD,
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": _classify_genre(name, description),
        "price": "",
        "url": data.get("@id") or EVENT_DETAIL_URL.format(slug=slug),
        "image": image,
        "description": description[:1200],
    }


async def _gather_slugs() -> list[str]:
    slugs: list[str] = []
    for page in range(1, _MAX_LIST_PAGES + 1):
        url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
        try:
            html = await asyncio.to_thread(_impersonate_get_text, url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("whitney_talks: list page %s failed: %s", page, exc)
            continue
        page_slugs = _harvest_slugs(html)
        new = [s for s in page_slugs if s not in slugs]
        if not new and page > 1:
            break
        slugs.extend(new)
    return slugs


async def _fetch_detail(
    client: httpx.AsyncClient, slug: str, sem: asyncio.Semaphore
) -> dict | None:
    async with sem:
        url = EVENT_DETAIL_URL.format(slug=slug)
        try:
            r = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
            )
            if r.status_code in (403, 418):
                html = await asyncio.to_thread(_impersonate_get_text, url)
            else:
                r.raise_for_status()
                html = r.text
        except Exception as exc:  # noqa: BLE001
            logger.warning("whitney_talks: detail %s failed: %s", slug, exc)
            return None
    data = _extract_ld_event(html)
    if not data:
        return None
    try:
        return _normalize(slug, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("whitney_talks: normalize %s failed: %s", slug, exc)
        return None


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Fetch Whitney programmed events for a date window. Defaults to next 14 days."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    slugs = await _gather_slugs()
    if not slugs:
        return []

    sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        results = await asyncio.gather(*[_fetch_detail(client, s, sem) for s in slugs])

    events = [e for e in results if e and e.get("date")]
    events = [e for e in events if start_date <= e["date"] <= end_date]
    events.sort(key=lambda e: (e["date"], e["time"]))
    return events


async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
