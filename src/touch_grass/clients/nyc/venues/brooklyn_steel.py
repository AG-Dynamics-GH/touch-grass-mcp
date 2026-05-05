"""Brooklyn Steel client — parses Bowery Presents venue page microdata.

Each event uses Schema.org Event microdata with startDate, name, performer,
description, ticket URL, and venue Place. We pull the `/venues/brooklyn-steel`
listing page and extract one event per Event itemtype block.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import unescape

import httpx

VENUE = {
    "name": "Brooklyn Steel",
    "neighborhood": "East Williamsburg",
    "city": "Brooklyn",
    "address": "319 Frost St, Brooklyn, NY 11222",
}
URL = "https://www.bowerypresents.com/venues/brooklyn-steel"
PROVIDER = "brooklyn_steel"

EVENT_BLOCK_RE = re.compile(
    r'(itemtype="http://schema\.org/Event".*?)(?=itemtype="http://schema\.org/Event"|</body>|</html>)',
    re.DOTALL,
)
START_DATE_RE = re.compile(r'startDate"\s+content="([^"]+)"')
NAME_RE = re.compile(r'<span itemprop="name">([^<]+)</span>')
SHOW_LINK_RE = re.compile(r'href="(/shows/detail/[^"]+)"')
PERFORMER_RE = re.compile(r'<span itemprop="performer" content="([^"]+)"')
DESC_RE = re.compile(r'<meta itemprop="description" content="([^"]+)"')
PRESENTED_RE = re.compile(r'<span class="presented-by">\s*<a[^>]*>([^<]+)</a>', re.DOTALL)


async def _fetch(url: str, timeout: float = 12.0) -> str:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 events-mcp"})
        resp.raise_for_status()
        return resp.text


def _parse_block(block: str) -> dict | None:
    m = START_DATE_RE.search(block)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1))
    except ValueError:
        return None
    name_match = NAME_RE.search(block)
    name = unescape(name_match.group(1).strip()) if name_match else "Untitled"
    link_match = SHOW_LINK_RE.search(block)
    url = "https://www.bowerypresents.com" + link_match.group(1) if link_match else URL
    performer_match = PERFORMER_RE.search(block)
    supporting = unescape(performer_match.group(1).strip()) if performer_match else ""
    presented_match = PRESENTED_RE.search(block)
    presenter = unescape(presented_match.group(1).strip()) if presented_match else ""
    description_parts = []
    if supporting:
        description_parts.append(f"with {supporting}")
    if presenter:
        description_parts.append(f"presented by {presenter}")
    description = "; ".join(description_parts) or None
    return {
        "name": name,
        "date": dt.date().isoformat(),
        "time": dt.strftime("%H:%M"),
        "venue_name": VENUE["name"],
        "neighborhood": VENUE["neighborhood"],
        "city": VENUE["city"],
        "address": VENUE["address"],
        "genre": "Concert",
        "url": url,
        "description": description,
        "lineup": supporting or None,
        "provider": PROVIDER,
    }


async def search_events(start_date: str = "", end_date: str = "", limit: int = 100) -> list[dict]:
    today = date.today()
    start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else today
    end = (
        datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else today + timedelta(days=180)
    )

    try:
        html = await _fetch(URL)
    except Exception:
        return []

    events: list[dict] = []
    for block in EVENT_BLOCK_RE.findall(html):
        entry = _parse_block(block)
        if not entry:
            continue
        d = date.fromisoformat(entry["date"])
        if d < start or d > end:
            continue
        events.append(entry)
        if len(events) >= limit:
            break
    return events
