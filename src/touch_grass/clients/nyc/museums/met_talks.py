"""Metropolitan Museum talks/programs client — events at metmuseum.org/events.

The Met's listing page is a Next.js server component that streams its events
via `self.__next_f.push([1, "<escaped-json-chunk>"])` script tags. Each chunk,
when concatenated and JS-string-decoded, contains the event search results
including `_source` objects with title, startDate, endDate, location,
teaserText, ticketPricing, primaryProgramTitle, etc.

Strategy:
  1. Fetch /events?page=N pages.
  2. Concatenate all `__next_f.push` payload strings, JS-unescape.
  3. Pull every `_source: {...}` object (brace-balanced) and parse as JSON.
  4. Filter to programmed events (skip "Museum admission" / pure visit categories)
     and to the requested window.

This avoids fragile per-event detail fetches and gives us the entire calendar
in a handful of HTTP calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os as _os
import re
from datetime import datetime, timedelta

from curl_cffi import requests as cf_requests


def _impersonate_kw():
    """Return {} or {"impersonate": "chrome"} based on TOUCH_GRASS_NYC_IMPERSONATE."""
    if _os.environ.get("TOUCH_GRASS_NYC_IMPERSONATE", "").lower() in ("true", "1", "yes"):
        return {"impersonate": "chrome"}
    return {}


logger = logging.getLogger(__name__)

LISTING_URL = "https://www.metmuseum.org/events"
VENUE_NAME = "The Metropolitan Museum of Art"
VENUE_ADDRESS = "1000 Fifth Avenue, New York, NY 10028"
VENUE_NEIGHBORHOOD_FIFTH = "Upper East Side"
VENUE_NEIGHBORHOOD_CLOISTERS = "Washington Heights"
CLOISTERS_ADDRESS = "99 Margaret Corbin Drive, New York, NY 10040"

_MAX_PAGES = 12  # 12 * 20 = 240 events; the Met currently lists ~226 total

# Categories we want — anything in these maps to a clear genre.
_CATEGORY_GENRE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"opening", re.I), "Opening"),
    (re.compile(r"lectures?", re.I), "Lecture"),
    (re.compile(r"symposi", re.I), "Symposium"),
    (re.compile(r"concerts?|performance|recital|met live arts", re.I), "Performance"),
    (re.compile(r"film|screening", re.I), "Film"),
    (re.compile(r"workshops?|classes?", re.I), "Workshop"),
    (re.compile(r"tours?|gallery talk", re.I), "Gallery Talk"),
    (re.compile(r"talks?|conversations?|met speaks", re.I), "Talk"),
]

# Categories we explicitly drop — these are not "programs" in the lecture/talk sense.
_DROP_CATEGORY_PATTERNS = [
    re.compile(r"^Member(?:ship)?$", re.I),  # plain membership benefits
]


def _impersonate_get_text(url: str) -> str:
    r = cf_requests.get(url, **_impersonate_kw(), timeout=20)
    r.raise_for_status()
    return r.text


def _decode_next_payload(html: str) -> str:
    """Concatenate every `self.__next_f.push([N, "..."]` string and JS-unescape."""
    pushes = re.findall(r'self\.__next_f\.push\(\[\d+,\s*"(.+?)"\]\)', html, re.DOTALL)
    full = "".join(pushes)
    if not full:
        return ""
    # The payload is a JS string literal: \" → ", \\ → \, \n → newline,
    # and \uXXXX for non-ASCII. We can't naively round-trip via the
    # unicode_escape codec because it interprets each Unicode char as
    # latin-1 and corrupts already-encoded UTF-8. Instead, leverage JSON's
    # parser, which understands the same escapes and preserves Unicode.
    try:
        return json.loads(f'"{full}"')
    except json.JSONDecodeError:
        return full.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")


def _grab_balanced_object(text: str, brace_start: int) -> str | None:
    depth = 0
    in_str = False
    escape = False
    for i in range(brace_start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : i + 1]
    return None


def _extract_sources(decoded: str) -> list[dict]:
    """Pull every `"_source": { ... }` block and parse as JSON."""
    out: list[dict] = []
    pos = 0
    needle = '"_source":'
    while True:
        idx = decoded.find(needle, pos)
        if idx < 0:
            break
        brace = decoded.find("{", idx + len(needle))
        if brace < 0:
            break
        obj_str = _grab_balanced_object(decoded, brace)
        if not obj_str:
            break
        try:
            obj = json.loads(obj_str)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            pass
        pos = brace + len(obj_str) if obj_str else brace + 1
    return out


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = (
        s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", s).strip()


def _split_iso(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value[:10], (value[11:16] if len(value) > 11 else "")
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _classify_genre(categories: list[str], program_title: str, name: str) -> str:
    haystack = " ".join(categories) + " " + program_title + " " + name
    for pat, label in _CATEGORY_GENRE_RULES:
        if pat.search(haystack):
            return label
    return "Talk"


def _is_dropworthy(categories: list[str]) -> bool:
    if not categories:
        return False
    for cat in categories:
        for pat in _DROP_CATEGORY_PATTERNS:
            if pat.search(cat):
                return True
    return False


def _normalize(src: dict) -> dict | None:
    title = _strip_html((src.get("title") or "").strip())
    if not title:
        return None
    categories = src.get("searchCategories") or []
    if _is_dropworthy(categories):
        return None
    program_title = src.get("primaryProgramTitle") or ""
    description = _strip_html(src.get("teaserText") or "")
    date_str, time_str = _split_iso(src.get("startDate", ""))
    end_date, end_time = _split_iso(src.get("endDate", ""))
    if not date_str:
        return None

    location_html = _strip_html(src.get("location") or "")
    building_name = src.get("buildingName") or ""
    is_cloisters = "cloisters" in (building_name + " " + location_html).lower()

    if is_cloisters:
        venue_name = "The Met Cloisters"
        address = CLOISTERS_ADDRESS
        neighborhood = VENUE_NEIGHBORHOOD_CLOISTERS
    elif building_name and building_name.lower() != "the met fifth avenue":
        venue_name = f"{VENUE_NAME} — {building_name}"
        address = VENUE_ADDRESS
        neighborhood = VENUE_NEIGHBORHOOD_FIFTH
    else:
        venue_name = VENUE_NAME
        address = VENUE_ADDRESS
        neighborhood = VENUE_NEIGHBORHOOD_FIFTH

    if "Online" in (building_name or ""):
        venue_name = f"{VENUE_NAME} (Online)"

    price_raw = (src.get("ticketPricing") or "").strip()
    price = (
        price_raw.lstrip("$").strip() if price_raw else ("Free" if not src.get("isPaid") else "")
    )
    if price and not price.startswith("$"):
        price = f"${price}" if any(c.isdigit() for c in price) else price

    url = src.get("url") or LISTING_URL
    image = src.get("teaserImageUrl") or ""

    ace_id = src.get("aceId") or ""

    return {
        "provider": "met_talks",
        "external_id": str(ace_id or url),
        "name": title,
        "date": date_str,
        "time": time_str,
        "end_date": end_date,
        "end_time": end_time,
        "venue_name": venue_name,
        "address": address,
        "neighborhood": neighborhood,
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": _classify_genre(categories, program_title, title),
        "price": price,
        "url": url,
        "image": image,
        "description": description[:1200],
    }


async def _fetch_page(page: int) -> list[dict]:
    url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
    try:
        html = await asyncio.to_thread(_impersonate_get_text, url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("met_talks: page %s fetch failed: %s", page, exc)
        return []
    decoded = _decode_next_payload(html)
    if not decoded:
        return []
    sources = _extract_sources(decoded)
    return [s for s in sources if isinstance(s, dict) and s.get("title")]


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Fetch Met programmed events for a date window. Defaults to next 14 days."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    pages = await asyncio.gather(*[_fetch_page(p) for p in range(1, _MAX_PAGES + 1)])

    seen_ids: set[str] = set()
    events: list[dict] = []
    for page_records in pages:
        for src in page_records:
            ev = _normalize(src)
            if not ev:
                continue
            if ev["external_id"] in seen_ids:
                continue
            seen_ids.add(ev["external_id"])
            if start_date <= ev["date"] <= end_date:
                events.append(ev)
    events.sort(key=lambda e: (e["date"], e["time"]))
    return events


async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
