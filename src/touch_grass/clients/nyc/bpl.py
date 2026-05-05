"""Brooklyn Public Library client — talks, kids, classes across BPL branches.

Hits the same Solr-backed search API the SPA at discover.bklynlibrary.org uses:

    GET https://discover.bklynlibrary.org/api/search/index.php?event=true

The API requires a Referer header (CORS gate) and returns ``grouped`` Solr
results — the actual events live under
``data['grouped']['ss_grouping']['groups'][i]['doclist']['docs'][0]``.

The endpoint hard-caps the response at 20 groups per call regardless of any
``rows``/``page``/``offset`` params we tried, so this returns up to 20 events
per ingest. The same call is rerun by the indexer and dedupes by event id.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx

API_URL = "https://discover.bklynlibrary.org/api/search/index.php?event=true"
EVENT_DETAIL_BASE = "https://discover.bklynlibrary.org/calendar"
_TAG_RE = re.compile(r"<[^>]+>")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://discover.bklynlibrary.org/?event=true",
    "Origin": "https://discover.bklynlibrary.org",
    "Accept": "application/json, text/plain, */*",
}


def _strip(text: str, max_len: int = 300) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub("", text).replace("\xa0", " ").replace("&nbsp;", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def _split_iso(iso: str) -> tuple[str, str]:
    """'2026-04-27T14:00:00Z' → ('2026-04-27', '14:00')."""
    if not iso:
        return "", ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return iso[:10], iso[11:16] if len(iso) > 11 else ""


def _normalize(doc: dict) -> dict:
    item_id = str(doc.get("item_id") or doc.get("id") or "")
    title = (doc.get("ts_title") or "").strip()
    body = doc.get("ts_body") or ""

    date_str, time_str = _split_iso(doc.get("ds_event_start_date", ""))
    end_date, end_time = _split_iso(doc.get("ds_event_end_date", ""))

    venue = (doc.get("ss_event_location") or doc.get("ss_event_location_master") or "").strip()
    is_virtual = bool(doc.get("is_virtual"))
    is_hybrid = bool(doc.get("is_hybrid"))

    image = (doc.get("ss_image_url") or "").strip()
    if image and image.startswith("//"):
        image = "https:" + image

    # Build a canonical detail URL — BPL routes /calendar/<slug>-<id>
    slug_title = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] if title else ""
    detail_url = (
        f"{EVENT_DETAIL_BASE}/{slug_title}-{item_id}" if slug_title else f"{EVENT_DETAIL_BASE}"
    )

    genre_bits = []
    if is_virtual:
        genre_bits.append("virtual")
    elif is_hybrid:
        genre_bits.append("hybrid")
    else:
        genre_bits.append("in-person")
    genre_bits.append("library")

    return {
        "provider": "bpl",
        "id": item_id,
        "name": title,
        "date": date_str,
        "time": time_str,
        "end_date": end_date,
        "end_time": end_time,
        "venue_name": venue,
        "address": "",
        "city": "Brooklyn",
        "state": "NY",
        "borough": "Brooklyn",
        "genre": ", ".join(genre_bits),
        "price": "Free",
        "url": detail_url,
        "image": image,
        "description": _strip(body),
    }


async def search_events(
    *,
    start_date: str = "",
    end_date: str = "",
    size: int = 50,
    **_unused,
) -> list[dict]:
    """Fetch upcoming Brooklyn Public Library events (up to ~20 per call)."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(API_URL)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError):
        return []

    groups = data.get("grouped", {}).get("ss_grouping", {}).get("groups", []) or []
    out: list[dict] = []
    for grp in groups:
        for doc in grp.get("doclist", {}).get("docs", []) or []:
            rec = _normalize(doc)
            if rec["date"] and (rec["date"] < start_date or rec["date"] > end_date):
                continue
            out.append(rec)
            if len(out) >= size:
                return out
    return out
