"""MoMA PS1 client — scrapes the public /programs listing.

PS1's /programs page surfaces 5–8 currently-running programs at any time,
including the Greater New York surveys and Warm Up summer series. Each
program detail page exposes og:title and og:description like
'Exhibition. Ends Aug 17. ...' which we parse into name + end date.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta

import httpx

VENUE = {
    "name": "MoMA PS1",
    "neighborhood": "Long Island City",
    "city": "Long Island City",
    "borough": "Queens",
    "address": "22-25 Jackson Ave, Queens, NY 11101",
}
LISTING_URL = "https://www.momaps1.org/programs"
BASE = "https://www.momaps1.org"
PROVIDER = "momaps1"

LINK_RE = re.compile(r'href="(/en/programs/(?!category)[\w/-]+)"')
OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]+)"')
OG_DESC_RE = re.compile(r'property="og:description"\s+content="([^"]+)"')
DATE_PHRASE_RE = re.compile(
    r"(?P<verb>Ends|Through|Starts|Opens|Begins)\s+"
    r"(?P<m>[A-Za-z]+)\s+(?P<d>\d{1,2})(?:,\s*(?P<y>\d{4}))?",
    re.IGNORECASE,
)
GENRE_RE = re.compile(
    r"^(Exhibition|Performance|Music|Film|Talk|Workshop|Tour|Reading|Screening)",
    re.IGNORECASE,
)

_MONTHS: dict[str, int] = {}
for _i, _m in enumerate(
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
):
    _MONTHS[_m.lower()] = _i + 1
    _MONTHS[_m[:3].lower()] = _i + 1


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 events-mcp"})
    resp.raise_for_status()
    return resp.text


def _parse_date_phrase(text: str, today: date) -> tuple[str, str]:
    m = DATE_PHRASE_RE.search(text)
    if not m:
        return "", ""
    verb = m.group("verb").lower()
    month = _MONTHS.get(m.group("m").lower())
    if not month:
        return "", ""
    day = int(m.group("d"))
    year = int(m.group("y") or today.year)
    try:
        d = date(year, month, day)
    except ValueError:
        return "", ""
    if d < today and not m.group("y"):
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return "", ""
    if verb in ("ends", "through"):
        return today.isoformat(), d.isoformat()
    if verb in ("starts", "opens", "begins"):
        return d.isoformat(), ""
    return "", ""


def _classify_genre(description: str, title: str) -> str:
    m = GENRE_RE.search(description.strip())
    if m:
        return m.group(1).capitalize()
    if "warm up" in title.lower():
        return "Music"
    return "Exhibition"


async def _parse_program(
    client: httpx.AsyncClient, path: str, today: date, window_end: date
) -> dict | None:
    try:
        html = await _fetch(client, BASE + path)
    except Exception:
        return None
    title_m = OG_TITLE_RE.search(html)
    desc_m = OG_DESC_RE.search(html)
    if not title_m:
        return None
    title = title_m.group(1).replace(" - MoMA PS1", "").strip()
    description = desc_m.group(1).strip() if desc_m else ""
    start_iso, end_iso = _parse_date_phrase(description, today)
    if not start_iso:
        start_iso = today.isoformat()
    if end_iso and date.fromisoformat(end_iso) < today:
        return None
    if start_iso and date.fromisoformat(start_iso) > window_end:
        return None
    return {
        "name": title,
        "date": start_iso,
        "end_date": end_iso,
        "time": "",
        "venue_name": VENUE["name"],
        "neighborhood": VENUE["neighborhood"],
        "borough": VENUE["borough"],
        "city": VENUE["city"],
        "address": VENUE["address"],
        "state": "NY",
        "genre": _classify_genre(description, title),
        "url": BASE + path,
        "description": description[:500],
        "provider": PROVIDER,
        "external_id": path,
    }


async def search_events(start_date: str = "", end_date: str = "", limit: int = 50) -> list[dict]:
    today = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else date.today()
    window_end = (
        datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else today + timedelta(days=120)
    )

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        try:
            listing = await _fetch(client, LISTING_URL)
        except Exception:
            return []
        paths = sorted(set(LINK_RE.findall(listing)))
        if not paths:
            return []
        results = await asyncio.gather(
            *[_parse_program(client, p, today, window_end) for p in paths],
            return_exceptions=True,
        )

    events: list[dict] = []
    for r in results:
        if isinstance(r, dict):
            events.append(r)
            if len(events) >= limit:
                break
    return events
