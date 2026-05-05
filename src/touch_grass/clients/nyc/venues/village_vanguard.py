"""Village Vanguard client — scrapes the WordPress RSS + event pages.

The Vanguard runs 6-day artist residencies (typically Tue–Sun). Each event page
has a `<h3>` date range like "April 28 - May 3". Each calendar date produces
two sets at 8pm and 10pm.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta

import feedparser
import httpx

VENUE = {
    "name": "Village Vanguard",
    "neighborhood": "West Village",
    "city": "New York",
    "address": "178 Seventh Avenue South, New York, NY 10014",
}
RSS_URL = "https://villagevanguard.com/feed/"
SET_TIMES = ["20:00", "22:00"]
PROVIDER = "village_vanguard"

_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
    )
}


async def _http_get(url: str, timeout: float = 12.0) -> str:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 events-mcp"})
        resp.raise_for_status()
        return resp.text


def _parse_date_range(text: str, today: date | None = None) -> list[date]:
    """Expand a string like 'April 28 - May 3' or 'May 5' into a list of dates.

    Year inferred: if the parsed date is more than 60 days in the past relative
    to today, advance the year by one (handles end-of-year residencies).
    """
    today = today or date.today()
    text = text.strip()
    pattern = re.compile(
        r"(?P<m1>[A-Za-z]+)\s+(?P<d1>\d{1,2})"
        r"(?:\s*[-–—]\s*(?:(?P<m2>[A-Za-z]+)\s+)?(?P<d2>\d{1,2}))?",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return []
    m1 = _MONTHS.get(m.group("m1").lower())
    if not m1:
        return []
    d1 = int(m.group("d1"))
    m2 = _MONTHS.get((m.group("m2") or m.group("m1")).lower())
    d2 = int(m.group("d2")) if m.group("d2") else d1
    if not m2:
        return []
    year = today.year
    try:
        start = date(year, m1, d1)
    except ValueError:
        return []
    if (today - start).days > 60:
        year += 1
        try:
            start = date(year, m1, d1)
        except ValueError:
            return []
    end_year = year + 1 if m2 < m1 else year
    try:
        end = date(end_year, m2, d2)
    except ValueError:
        return []
    if end < start:
        return []
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _parse_event_page(html: str, url: str) -> dict | None:
    name_match = re.search(r"<h2[^>]*>([^<]+)</h2>", html)
    h3_matches = re.findall(r"<h3[^>]*>([^<]+)</h3>", html)
    if not name_match or not h3_matches:
        return None
    name = name_match.group(1).strip()
    date_text = ""
    for h3 in h3_matches:
        if any(month.lower() in h3.lower() for month in _MONTHS):
            date_text = h3.strip()
            break
    if not date_text:
        return None
    lineup = re.findall(r"<h4[^>]*>\s*<strong>([^<]+)</strong>\s*[–-]\s*([^<]+)</h4>", html)
    lineup_str = (
        ", ".join(f"{name.strip()} ({role.strip()})" for name, role in lineup) if lineup else ""
    )
    return {
        "name": name,
        "date_text": date_text,
        "lineup": lineup_str,
        "url": url,
    }


async def search_events(start_date: str = "", end_date: str = "", limit: int = 50) -> list[dict]:
    """Fetch upcoming Vanguard residencies, expanded into per-date show entries."""
    today = date.today()
    start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else today
    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else today + timedelta(days=30)

    try:
        rss = await _http_get(RSS_URL)
    except Exception:
        return []
    feed = feedparser.parse(rss)
    entries = feed.get("entries", [])

    coros = []
    for entry in entries:
        link = entry.get("link", "")
        title = entry.get("title", "").strip()
        if not link or not title or title.lower() == "coming soon!":
            continue
        coros.append(_http_get(link))

    pages = await asyncio.gather(*coros, return_exceptions=True)
    events: list[dict] = []
    for entry, page in zip(entries, pages, strict=False):
        link = entry.get("link", "")
        if isinstance(page, Exception) or not isinstance(page, str):
            continue
        parsed = _parse_event_page(page, link)
        if not parsed:
            continue
        dates = _parse_date_range(parsed["date_text"], today=today)
        for d in dates:
            if d < start or d > end:
                continue
            for set_time in SET_TIMES:
                events.append(
                    {
                        "name": parsed["name"],
                        "date": d.isoformat(),
                        "time": set_time,
                        "venue_name": VENUE["name"],
                        "neighborhood": VENUE["neighborhood"],
                        "city": VENUE["city"],
                        "address": VENUE["address"],
                        "genre": "Jazz",
                        "url": parsed["url"],
                        "description": parsed["lineup"] or None,
                        "provider": PROVIDER,
                    }
                )
                if len(events) >= limit:
                    return events
    return events
