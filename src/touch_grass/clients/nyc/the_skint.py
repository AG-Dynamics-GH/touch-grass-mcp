"""The Skint client — NYC free/cheap events newsletter (RSS).

Each post in the RSS feed (https://www.theskint.com/feed/) is either a daily
roundup ("FRI-MON, 4/24-27: SKINT WEEKEND") or a single sponsored event. We
treat every post as one event row pointing back at the original post — the
posts are dense prose ("free outdoor jazz at Lincoln Center, 7pm; cheap art
opening in Bushwick, 8pm; ..."), and a clean per-line parser would mis-merge
or drop entries more than half the time. Downstream curation can extract
specific items from the description if needed; the value here is keeping the
roundup pointer in the index.

Fields normalized:
- date: pubDate (the date the roundup was posted, which is also the date the
  events run for daily posts)
- name: post title
- description: text-stripped summary (≤300 chars)
- venue_name / borough / time: empty (post-level pointer, not a single event)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import feedparser
import httpx

FEED_URL = "https://www.theskint.com/feed/"
_TAG_RE = re.compile(r"<[^>]+>")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


def _parse_pub(raw: str) -> str:
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _strip_html(text: str, max_len: int = 300) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub("", text).replace("\xa0", " ").replace("&nbsp;", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def _is_sponsored(title: str) -> bool:
    return "(SPONSORED)" in title.upper() or "[SPONSORED]" in title.upper()


def _normalize(entry: dict) -> dict:
    title = (entry.get("title") or "").strip()
    link = entry.get("link") or ""
    pub = entry.get("published") or entry.get("updated") or ""
    date_str = _parse_pub(pub)
    summary = entry.get("summary") or entry.get("description") or ""

    return {
        "provider": "the_skint",
        "id": entry.get("id") or link or title,
        "name": title,
        "date": date_str,
        "time": "",
        "venue_name": "Various (see post)",
        "address": "",
        "city": "New York",
        "state": "NY",
        "borough": "",
        "genre": "sponsored" if _is_sponsored(title) else "roundup",
        "price": "Free / cheap",
        "url": link,
        "image": "",
        "description": _strip_html(summary),
    }


async def search_events(
    *,
    start_date: str = "",
    end_date: str = "",
    size: int = 20,
    **_unused,
) -> list[dict]:
    """Fetch the latest Skint posts and filter to [start_date, end_date]."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(FEED_URL)
            resp.raise_for_status()
            raw = resp.text
    except (httpx.HTTPError, httpx.TimeoutException):
        return []

    parsed = feedparser.parse(raw)
    out: list[dict] = []
    for entry in parsed.get("entries", []):
        rec = _normalize(entry)
        if start_date <= (rec["date"] or "9999-99-99") <= end_date:
            out.append(rec)
        if len(out) >= size:
            break
    return out
