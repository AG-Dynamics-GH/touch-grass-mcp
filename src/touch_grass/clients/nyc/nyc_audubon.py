"""NYC Bird Alliance (formerly NYC Audubon) — bird walks, classes, lectures.

The org migrated from nycaudubon.org → nycbirdalliance.org and moved its event
calendar onto NeonCRM. The public event list lives at:

    https://nycbirdalliance.app.neoncrm.com/np/clients/nycbirdalliance/publicaccess/eventList.jsp

It returns server-rendered HTML — no JSON-LD, no public API. We scrape the
``<div class="neoncrm-event-list-event">`` blocks: each contains a name link,
``<div class="neoncrm-event-date">MM/DD/YYYY HH:MM AM-HH:MM AM ET</div>``, an
admission ``<ul><li>Free | $15 | ...</li></ul>``, and a register button URL
with the event id.

iCal feed (``?ical=1`` on the WP site) was not retained after the NeonCRM
migration; eventList.jsp is the only structured surface we found.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta

import httpx

LIST_URL = (
    "https://nycbirdalliance.app.neoncrm.com/np/clients/nycbirdalliance/publicaccess/eventList.jsp"
)
DETAIL_URL = "https://nycbirdalliance.app.neoncrm.com/np/clients/nycbirdalliance/publicaccess/event.jsp?event={eid}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_EVENT_BLOCK_RE = re.compile(
    r'<div class="neoncrm-event-list-event">(.*?)</div>\s*</div>',
    re.S,
)
_NAME_RE = re.compile(
    r'<a href="([^"]*event=(\d+))"[^>]*>([^<]+)</a>',
    re.S,
)
_DATE_RE = re.compile(r'<div class="neoncrm-event-date">([^<]+)</div>')
_ADMISSION_RE = re.compile(r'<div class="neoncrm-event-admission">.*?<li>(.*?)</li>', re.S)
_LOCATION_RE = re.compile(r'<div class="neoncrm-event-location">(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(text: str, max_len: int = 300) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub("", html.unescape(text)).replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def _parse_date(raw: str) -> tuple[str, str]:
    """'04/27/2026 05:30 PM - 07:00 PM ET' → ('2026-04-27', '17:30')."""
    if not raw:
        return "", ""
    raw = raw.strip()
    # Take everything before the first ' - '
    head = raw.split(" - ", 1)[0].rstrip(" ET").rstrip()
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M%p", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(head, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M") if "%I" in fmt else ""
        except ValueError:
            continue
    return "", ""


def _normalize(block_html: str) -> dict | None:
    name_m = _NAME_RE.search(block_html)
    if not name_m:
        return None
    detail_path = name_m.group(1)
    eid = name_m.group(2)
    title = _strip(name_m.group(3))

    date_m = _DATE_RE.search(block_html)
    raw_date = date_m.group(1).strip() if date_m else ""
    date_str, time_str = _parse_date(raw_date)

    adm_m = _ADMISSION_RE.search(block_html)
    price = _strip(adm_m.group(1), 60) if adm_m else ""

    loc_m = _LOCATION_RE.search(block_html)
    venue = _strip(loc_m.group(1), 100) if loc_m else ""

    detail_url = (
        f"https://nycbirdalliance.app.neoncrm.com{detail_path}"
        if detail_path.startswith("/")
        else DETAIL_URL.format(eid=eid)
    )

    return {
        "provider": "nyc_audubon",
        "id": eid,
        "name": title,
        "date": date_str,
        "time": time_str,
        "venue_name": venue or "NYC Bird Alliance",
        "address": "",
        "city": "New York",
        "state": "NY",
        "borough": "",
        "genre": "nature, birding",
        "price": price,
        "url": detail_url,
        "image": "",
        "description": f"NYC Bird Alliance program. {raw_date}".strip(),
    }


async def search_events(
    *,
    start_date: str = "",
    end_date: str = "",
    size: int = 60,
    **_unused,
) -> list[dict]:
    """Fetch NYC Bird Alliance (Audubon) upcoming events."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(LIST_URL)
            resp.raise_for_status()
            html_text = resp.text
    except (httpx.HTTPError, httpx.TimeoutException):
        return []

    out: list[dict] = []
    for m in _EVENT_BLOCK_RE.finditer(html_text):
        rec = _normalize(m.group(1))
        if not rec:
            continue
        if rec["date"] and (rec["date"] < start_date or rec["date"] > end_date):
            continue
        out.append(rec)
        if len(out) >= size:
            break
    return out
