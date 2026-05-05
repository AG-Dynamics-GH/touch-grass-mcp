"""Queens Public Library client — programs/events across QPL branches.

Hits the same internal Solr endpoint the queenslibrary.org calendar SPA uses:

    GET https://www.queenslibrary.org/search/call?searchField=*&category=calendar&pageParam=1&searchFilter=

Returns an HTML fragment (not JSON) containing one ``<div class="cardWrapper">``
per event plus per-event embedded JSON in inline scripts:

    arrJsonData_cal['014437-0426'] = '{&quot;jobID&quot;:&quot;...&quot;, ...}';

We extract the embedded JSON (HTML-entity decoded) for each event — it has
all the structured fields (jobID, title, descr, prgm_age, location, dates,
delivery_format, callUrl).

Pagination via &pageParam=N — caller can request more pages via ``pages=``.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime, timedelta

import httpx

BASE_URL = "https://www.queenslibrary.org"
SEARCH_PATH = "/search/call?searchField=*&category=calendar&pageParam={page}&searchFilter="
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://www.queenslibrary.org/calendar?searchField=%2A&category=calendar",
}

# arrJsonData_cal['014437-0426'] = '{ ... html-entity-encoded JSON ... }';
_JSON_BLOB_RE = re.compile(
    r"arrJsonData_cal\['([^']+)'\]\s*=\s*'(\{[^']*\})'\s*;",
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(text: str, max_len: int = 300) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub("", text).replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def _decode_blob(blob: str) -> dict:
    """The blob is HTML-entity-encoded JSON ('&quot;' instead of '\"')."""
    decoded = html.unescape(blob)
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        return {}


def _split_iso_or_unix(value) -> tuple[str, str]:
    if not value:
        return "", ""
    # Unix timestamp (int, float, or numeric string)
    try:
        ts = float(value)
        if ts > 1_000_000_000:  # plausible unix seconds since 2001
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except (TypeError, ValueError):
        pass
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19] if "T" in s else s, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            continue
    return s[:10], ""


def _normalize(record: dict) -> dict:
    job_id = record.get("jobID") or ""
    title = (record.get("title") or "").strip()
    descr = record.get("descr") or ""

    # Primary date is the unix timestamp of the next session.
    date_str, time_str = _split_iso_or_unix(record.get("date_show_timestamp"))

    branch = record.get("branch_name") or record.get("location") or ""
    age = record.get("prgm_age") or ""
    delivery = record.get("delivery_format") or ""
    prgm_type = record.get("prgm_type") or ""

    call_url = record.get("callUrl") or ""
    if call_url and not call_url.startswith("http"):
        call_url = f"{BASE_URL}{call_url}"

    image = (
        record.get("cal_image_large")
        or record.get("cal_image_small")
        or record.get("prgm_image")
        or ""
    )
    if image and not image.startswith("http"):
        image = f"https://image.queenslibrary.org/lamps/{image}"

    genre_bits = ["library"]
    if delivery:
        genre_bits.append(delivery.lower())
    if age:
        genre_bits.append(age)
    if prgm_type:
        genre_bits.append(prgm_type)

    return {
        "provider": "qpl",
        "id": job_id,
        "name": title,
        "date": date_str,
        "time": time_str,
        "venue_name": branch,
        "address": "",
        "city": "Queens",
        "state": "NY",
        "borough": "Queens",
        "neighborhood": branch,
        "genre": ", ".join(b for b in genre_bits if b),
        "price": "Free",
        "url": call_url,
        "image": image,
        "description": _strip(descr),
    }


async def _fetch_page(client: httpx.AsyncClient, page: int) -> list[dict]:
    url = BASE_URL + SEARCH_PATH.format(page=page)
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return []
    out: list[dict] = []
    for m in _JSON_BLOB_RE.finditer(resp.text):
        record = _decode_blob(m.group(2))
        if not record:
            continue
        # Ensure we have job id even if not in the JSON blob
        if not record.get("jobID"):
            record["jobID"] = m.group(1)
        out.append(_normalize(record))
    return out


async def search_events(
    *,
    start_date: str = "",
    end_date: str = "",
    size: int = 60,
    pages: int = 3,
    **_unused,
) -> list[dict]:
    """Fetch QPL events; pages 1..N (each page ~20 events)."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS) as client:
        results = await asyncio.gather(*[_fetch_page(client, p) for p in range(1, pages + 1)])

    seen: set[str] = set()
    out: list[dict] = []
    for page_recs in results:
        for rec in page_recs:
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            # If we have a parsed date, gate on the window — otherwise keep
            if rec["date"] and (rec["date"] < start_date or rec["date"] > end_date):
                continue
            out.append(rec)
            if len(out) >= size:
                return out
    return out
