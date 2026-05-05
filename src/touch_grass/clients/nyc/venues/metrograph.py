"""Metrograph client — Lower East Side art-house cinema showtimes.

Scrapes showtimes from https://metrograph.com/nyc/ — the SSR'd "All Showtimes"
page, which contains one ``<div id="calendar-list-day-YYYY-MM-DD">`` per day,
with one ``<div class="item film-thumbnail homepage-in-theater-movie">`` per
film and a ``<div class="showtimes">`` block of anchors per session.

No JSON-LD / NEXT_DATA on this page — pure HTML scrape via regex (the markup
is templated and consistent enough that bs4 isn't worth the dependency hit).
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta

import httpx

SHOWTIMES_URL = "https://metrograph.com/nyc/"
VENUE_NAME = "Metrograph"
VENUE_ADDRESS = "7 Ludlow St, New York, NY 10002"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_DAY_RE = re.compile(
    r'<div class="calendar-list-day movies-grid" id="calendar-list-day-(\d{4}-\d{2}-\d{2})">(.*?)(?=<div class="calendar-list-day movies-grid" id="calendar-list-day-|</div></div></div>\s*</section>)',
    re.S,
)
_ITEM_RE = re.compile(
    r'<div class="item film-thumbnail homepage-in-theater-movie">(.*?)</div></div>',
    re.S,
)
_TITLE_RE = re.compile(
    r'<h4><a href="(?P<url>[^"]+)" class="title">(?P<title>[^<]+)</a></h4>',
    re.S,
)
_META_RE = re.compile(r'<div class="film-metadata">([^<]*)</div>')
_DESC_RE = re.compile(r'<div class="film-description">(.*?)</div>', re.S)
_IMG_RE = re.compile(r'<a href="[^"]+" class="image"><img[^>]+src="([^"]+)"')
_SHOWTIME_RE = re.compile(
    r"<a(?P<attrs>[^>]*)>(?P<time>\d{1,2}:\d{2}\s?[ap]m)</a>",
    re.I,
)
_HREF_RE = re.compile(r'href="([^"]+)"')
_CLASS_RE = re.compile(r'class="([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(s: str) -> str:
    return _TAG_RE.sub("", html.unescape(s)).replace("\xa0", " ").strip()


def _parse_time(t: str, date_str: str) -> str:
    """'7:30pm' → '19:30' (24h)."""
    try:
        dt = datetime.strptime(t.strip().lower().replace(" ", ""), "%I:%M%p")
        return dt.strftime("%H:%M")
    except ValueError:
        return ""


def _normalize_film(
    item_html: str,
    date_str: str,
    showtime_attrs: str,
    raw_time: str,
) -> dict:
    title_m = _TITLE_RE.search(item_html)
    title = _strip(title_m.group("title")) if title_m else ""
    rel_url = title_m.group("url") if title_m else ""
    if rel_url and not rel_url.startswith("http"):
        rel_url = f"https://metrograph.com{rel_url}"

    meta_m = _META_RE.search(item_html)
    meta = _strip(meta_m.group(1)) if meta_m else ""
    director = meta.split("/")[0].strip() if "/" in meta else meta

    desc_m = _DESC_RE.search(item_html)
    description = _strip(desc_m.group(1))[:300] if desc_m else meta

    img_m = _IMG_RE.search(item_html)
    image = img_m.group(1) if img_m else ""

    sold_out = "sold_out" in showtime_attrs
    href_m = _HREF_RE.search(showtime_attrs)
    ticket_url = href_m.group(1) if href_m else rel_url

    time_24h = _parse_time(raw_time, date_str)

    # External id is film vista_id + date + time so the same film on different
    # showtimes dedupes correctly.
    vista = ""
    vm = re.search(r"vista_film_id=(\d+)", rel_url)
    if vm:
        vista = vm.group(1)
    eid = f"{vista or title[:30]}_{date_str}_{time_24h}"

    return {
        "provider": "metrograph",
        "id": eid,
        "name": title,
        "date": date_str,
        "time": time_24h,
        "venue_name": VENUE_NAME,
        "address": VENUE_ADDRESS,
        "city": "New York",
        "state": "NY",
        "borough": "Manhattan",
        "neighborhood": "Lower East Side",
        "genre": "film",
        "price": "Sold Out" if sold_out else "",
        "url": ticket_url,
        "image": image,
        "description": f"Dir. {director}. {description}".strip(". ") if director else description,
    }


async def search_events(
    *,
    start_date: str = "",
    end_date: str = "",
    size: int = 200,
    **_unused,
) -> list[dict]:
    """Fetch upcoming Metrograph showtimes within [start_date, end_date]."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(SHOWTIMES_URL)
            resp.raise_for_status()
            html_text = resp.text
    except (httpx.HTTPError, httpx.TimeoutException):
        return []

    events: list[dict] = []
    for day_match in _DAY_RE.finditer(html_text):
        day = day_match.group(1)
        if day < start_date or day > end_date:
            continue
        day_html = day_match.group(2)
        for item_match in _ITEM_RE.finditer(day_html):
            item_html = item_match.group(1) + "</div>"  # restore inner closer
            for st in _SHOWTIME_RE.finditer(item_html):
                events.append(
                    _normalize_film(
                        item_html=item_html,
                        date_str=day,
                        showtime_attrs=st.group("attrs"),
                        raw_time=st.group("time"),
                    )
                )
                if len(events) >= size:
                    return events
    return events
