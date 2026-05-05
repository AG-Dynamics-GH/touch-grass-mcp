"""Editorial RSS client — NYC food/culture/news outlets.

Pulls RSS feeds from Eater NY, Gothamist, Time Out NY, and The Infatuation.
All feeds are public, no auth required. Use feedparser to handle RSS/Atom.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from email.utils import parsedate_to_datetime

import feedparser
import httpx

FEEDS = {
    "eater_ny": {
        "name": "Eater NY",
        "url": "https://ny.eater.com/rss/index.xml",
        "category": "food",
    },
    "gothamist": {
        "name": "Gothamist",
        "url": "https://gothamist.com/feed",
        "category": "news",
    },
    "timeout_ny": {
        "name": "Time Out New York",
        "url": "https://www.timeout.com/newyork/feed.rss",
        "category": "events",
    },
    "hyperallergic": {
        "name": "Hyperallergic",
        "url": "https://hyperallergic.com/feed/",
        "category": "arts",
    },
    "artforum": {
        "name": "Artforum",
        "url": "https://www.artforum.com/feed/",
        "category": "arts",
    },
}


def _parse_date(entry: dict) -> str:
    """Extract published date as ISO string, or empty."""
    for field in ("published", "updated", "pubDate"):
        raw = entry.get(field)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                return dt.strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                continue
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return datetime(*parsed[:6]).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    return ""


def _normalize_entry(entry: dict, feed_name: str, category: str) -> dict:
    summary = entry.get("summary", "") or entry.get("description", "")
    if len(summary) > 300:
        summary = summary[:297] + "..."
    return {
        "title": entry.get("title", "Untitled"),
        "url": entry.get("link", ""),
        "summary": summary,
        "date": _parse_date(entry),
        "source": feed_name,
        "category": category,
        "author": entry.get("author", ""),
    }


async def _fetch_feed(url: str) -> str:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 events-mcp"})
        resp.raise_for_status()
        return resp.text


async def fetch_feed(feed_id: str, limit: int = 15) -> list[dict]:
    """Fetch a single RSS feed by ID."""
    if feed_id not in FEEDS:
        raise ValueError(f"Unknown feed: {feed_id}. Options: {list(FEEDS.keys())}")
    meta = FEEDS[feed_id]
    raw = await _fetch_feed(meta["url"])
    parsed = feedparser.parse(raw)
    entries = parsed.get("entries", [])[:limit]
    return [_normalize_entry(e, meta["name"], meta["category"]) for e in entries]


async def fetch_all(
    category: str = "",
    limit_per_feed: int = 8,
) -> list[dict]:
    """Fetch all configured feeds (optionally filtered by category) in parallel."""
    targets = [
        (fid, meta) for fid, meta in FEEDS.items() if not category or meta["category"] == category
    ]
    coros = [fetch_feed(fid, limit_per_feed) for fid, _ in targets]
    results = await asyncio.gather(*coros, return_exceptions=True)
    items: list[dict] = []
    for r in results:
        if isinstance(r, list):
            items.extend(r)
    items.sort(key=lambda x: x.get("date", ""), reverse=True)
    return items
