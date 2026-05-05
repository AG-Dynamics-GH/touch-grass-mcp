"""Park Avenue Armory client — uses curl_cffi to bypass Cloudflare-class blocking.

Each event detail page embeds a `pdpEvents = [{...}]` JS array with a full
list of performance dates, ticket types, and facility info. We fetch the
season-listing page to discover event URLs, then parse each detail page.
"""

from __future__ import annotations

import asyncio
import json
import os as _os
import re
from datetime import date, datetime, timedelta


def _impersonate_kw():
    """Return {} or {"impersonate": "chrome"} based on TOUCH_GRASS_NYC_IMPERSONATE."""
    if _os.environ.get("TOUCH_GRASS_NYC_IMPERSONATE", "").lower() in ("true", "1", "yes"):
        return {"impersonate": "chrome"}
    return {}


VENUE = {
    "name": "Park Avenue Armory",
    "neighborhood": "Upper East Side",
    "city": "New York",
    "address": "643 Park Ave, New York, NY 10065",
}
BASE = "https://www.armoryonpark.org"
SEASON_PATH = "/season-events/2026-season/"
PROVIDER = "park_avenue_armory"
PDPEVENTS_RE = re.compile(r"var\s+pdpEvents\s*=\s*(\[.*?\])\s*;", re.DOTALL)
EVENT_LINK_RE = re.compile(r'href="(/season-events/202[56]-season/[^"#?]+)"')


def _fetch_sync(url: str) -> str:
    from curl_cffi import requests

    resp = requests.get(url, **_impersonate_kw(), timeout=15)
    resp.raise_for_status()
    return resp.text


async def _fetch(url: str) -> str:
    return await asyncio.to_thread(_fetch_sync, url)


def _extract_pdp_events(html: str) -> list[dict]:
    m = PDPEVENTS_RE.search(html)
    if not m:
        return []
    raw = m.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_event(perf: dict, event_url: str) -> dict | None:
    raw_date = perf.get("date") or perf.get("doorsOpen") or ""
    if not raw_date:
        return None
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    show_title = perf.get("title", "").strip() or "Untitled"
    show_type = (perf.get("type") or {}).get("description", "").strip()
    name = (
        f"{show_title} — {show_type}"
        if show_type and show_type.lower() not in show_title.lower()
        else show_title
    )
    facility = (perf.get("facility") or {}).get("facilityDescription", "").strip()
    description = _strip_html(perf.get("description", ""))[:500] or None
    sold_out = (perf.get("status") or {}).get("description", "").lower() == "sold out"
    return {
        "name": name,
        "date": dt.date().isoformat(),
        "time": dt.strftime("%H:%M"),
        "venue_name": VENUE["name"],
        "neighborhood": VENUE["neighborhood"],
        "city": VENUE["city"],
        "address": VENUE["address"],
        "genre": "Performance",
        "url": event_url,
        "description": description,
        "facility": facility or None,
        "sold_out": sold_out,
        "provider": PROVIDER,
    }


async def search_events(start_date: str = "", end_date: str = "", limit: int = 200) -> list[dict]:
    today = date.today()
    start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else today
    end = (
        datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else today + timedelta(days=120)
    )

    try:
        season_html = await _fetch(BASE + SEASON_PATH)
    except Exception:
        return []

    paths = sorted(set(EVENT_LINK_RE.findall(season_html)))
    paths = [p for p in paths if p.count("/") >= 4 and not p.endswith("/overview/")]

    pages = await asyncio.gather(*[_fetch(BASE + p) for p in paths], return_exceptions=True)
    events: list[dict] = []
    for path, page in zip(paths, pages, strict=False):
        if isinstance(page, Exception) or not isinstance(page, str):
            continue
        for perf in _extract_pdp_events(page):
            entry = _normalize_event(perf, BASE + path)
            if not entry:
                continue
            event_date = date.fromisoformat(entry["date"])
            if event_date < start or event_date > end:
                continue
            events.append(entry)
            if len(events) >= limit:
                return events
    return events
