"""Chelsea / major-NYC gallery exhibition openings client.

Surfaces gallery exhibitions (and Thursday-night opening receptions) from a
hand-picked roster of blue-chip NYC dealers. Each gallery exposes its data
differently — there is no shared standard — so this module is a small farm of
per-site fetchers aggregated under one ``fetch_events`` entry point.

Reception fallback (documented per spec)
----------------------------------------
Most galleries publish exhibition START dates but not the opening reception
date/time. When a reception time isn't explicitly provided we emit a single
event per exhibition with:

* ``date``      = exhibition ``start_date``
* ``time``      = ``"18:00"`` (typical opening reception time)
* ``genre``     = ``"Gallery Opening"`` if today is within ±3 days of the
  start_date (i.e. that's the opening week), else ``"Exhibition"``

David Zwirner is the one gallery that publishes a structured ``reception``
field — when present we parse it and use the exact day + time.

Sources surveyed (priority order)
---------------------------------
* **David Zwirner** — ``davidzwirner.com/exhibitions``, Cloudflare-fronted
  Next.js. We fetch with ``curl_cffi`` and parse ``__NEXT_DATA__``. Includes
  ``reception`` strings like ``"Thursday, April 23, 6–8 PM"``.
* **Pace Gallery** — ``pacegallery.com/exhibitions/``. Static HTML index with
  ``<h3 class=index-grid__text-title>`` titles, ``index-grid__text-date``
  ranges, ``index-grid__text-location`` city tags. Filtered to ``New York``.
* **Hauser & Wirth** — ``hauserwirth.com/locations/<slug>/``. Three NY slugs
  scraped (18th Street, 22nd Street, Wooster Street) via curl_cffi +
  ``__NEXT_DATA__`` ``relatedData.exhibitions``. Cleanly per-location.
* **Gagosian** — ``gagosian.com/exhibitions/`` Next.js, plain httpx works,
  ``__NEXT_DATA__.props.pageProps.exhibitions`` carries ``dates_display`` like
  ``"April 25–June 27, 2026"``. Filtered to NY locations via ``location_str``.
* **Marian Goodman** — ``mariangoodman.com/exhibitions/new-york/`` static HTML
  Artlogic-style cards (``.entry`` blocks).
* **Lehmann Maupin** — ``lehmannmaupin.com/exhibitions`` static HTML, parses
  ``.entry`` cards filtered to ``New York`` location subtitles.
* **Sean Kelly** — ``skny.com/exhibitions`` static HTML, similar Artlogic
  ``.entry`` layout filtered to ``Sean Kelly, New York``.

Skipped (couldn't justify the maintenance cost): Cheim & Read (sparse 2024
inventory, may be effectively closed), Drawing Center, New Museum (these are
museums, not galleries, and their event/exhibition programs are already
adjacent to the museum-talks clients).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os as _os
import re
from datetime import datetime, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests


def _impersonate_kw():
    """Return {} or {"impersonate": "chrome"} based on TOUCH_GRASS_NYC_IMPERSONATE."""
    if _os.environ.get("TOUCH_GRASS_NYC_IMPERSONATE", "").lower() in ("true", "1", "yes"):
        return {"impersonate": "chrome"}
    return {}


logger = logging.getLogger(__name__)

PROVIDER = "chelsea_galleries"
DEFAULT_TIMEOUT = 20

# ---------------------------------------------------------------------------
# Constants & venue metadata
# ---------------------------------------------------------------------------
DEFAULT_RECEPTION_TIME = "18:00"  # typical Thursday-night opening
OPENING_WINDOW_DAYS = 3  # within ±3 days of start_date → "Gallery Opening"

# Per-gallery venue metadata. ``url`` is the canonical exhibitions index URL.
GALLERY_VENUES: dict[str, dict[str, str]] = {
    "david_zwirner": {
        "venue_name": "David Zwirner",
        "address": "525 & 533 West 19th Street, New York, NY 10011",
        "neighborhood": "Chelsea",
    },
    "pace": {
        "venue_name": "Pace Gallery",
        "address": "540 West 25th Street, New York, NY 10001",
        "neighborhood": "Chelsea",
    },
    "hauser_wirth_22nd": {
        "venue_name": "Hauser & Wirth — 22nd Street",
        "address": "542 West 22nd Street, New York, NY 10011",
        "neighborhood": "Chelsea",
    },
    "hauser_wirth_18th": {
        "venue_name": "Hauser & Wirth — 18th Street",
        "address": "443 West 18th Street, New York, NY 10011",
        "neighborhood": "Chelsea",
    },
    "hauser_wirth_wooster": {
        "venue_name": "Hauser & Wirth — Wooster Street",
        "address": "134 Wooster Street, New York, NY 10012",
        "neighborhood": "SoHo",
    },
    "gagosian": {
        "venue_name": "Gagosian",
        "address": "555 West 24th Street, New York, NY 10011",
        "neighborhood": "Chelsea",
    },
    "marian_goodman": {
        "venue_name": "Marian Goodman Gallery",
        "address": "385 Broadway, New York, NY 10013",
        "neighborhood": "TriBeCa",
    },
    "lehmann_maupin": {
        "venue_name": "Lehmann Maupin",
        "address": "501 West 24th Street, New York, NY 10011",
        "neighborhood": "Chelsea",
    },
    "sean_kelly": {
        "venue_name": "Sean Kelly Gallery",
        "address": "475 Tenth Avenue, New York, NY 10018",
        "neighborhood": "Hell's Kitchen",
    },
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

# "April 25 – June 27, 2026" / "Apr 10, 2025 – Dec 31, 2026" / "March 26–May 2, 2026"
_DATE_RANGE_MONTH_FIRST = re.compile(
    r"(?P<m1>[A-Za-z]+)\s*(?P<d1>\d{1,2})"
    r"(?:,\s*(?P<y1>\d{4}))?"
    r"\s*[–\-—]\s*"
    r"(?:(?P<m2>[A-Za-z]+)\s*)?(?P<d2>\d{1,2})"
    r"(?:,\s*(?P<y2>\d{4}))?",
)

# Format B: "14 April - 6 June 2026" / "20 - 23 May 2026" (UK day-first style)
_DATE_RANGE_DAY_FIRST = re.compile(
    r"(?P<d1>\d{1,2})\s*(?:(?P<m1>[A-Za-z]+))?"
    r"(?:,?\s*(?P<y1>\d{4}))?"
    r"\s*[–\-—]\s*"
    r"(?P<d2>\d{1,2})\s*(?P<m2>[A-Za-z]+)"
    r"(?:,?\s*(?P<y2>\d{4}))?",
)


def _parse_date_range(s: str) -> tuple[str, str]:
    """Parse ``"April 25 – June 27, 2026"`` → (``2026-04-25``, ``2026-06-27``).

    Falls back to ('', '') on unrecognized strings.
    """
    if not s:
        return "", ""
    text = s.replace(" ", " ").replace("\xa0", " ").strip()
    # Month-first first (more specific anchor: leading alphabetic month).
    m = _DATE_RANGE_MONTH_FIRST.match(text)
    if m and m.group("m1") and m.group("m1").lower() in _MONTHS:
        parts = m.groupdict()
        m2 = parts["m2"] or parts["m1"]
        y2 = parts["y2"] or parts["y1"]
        y1 = parts["y1"] or y2
        if y1 and y2:
            try:
                start = datetime(int(y1), _MONTHS[parts["m1"].lower()], int(parts["d1"]))
                end = datetime(int(y2), _MONTHS[m2.lower()], int(parts["d2"]))
                return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            except (KeyError, ValueError):
                pass

    # Day-first fallback.
    m = _DATE_RANGE_DAY_FIRST.search(text)
    if m:
        parts = m.groupdict()
        m1 = parts["m1"] or parts["m2"]
        y2 = parts["y2"] or parts["y1"]
        y1 = parts["y1"] or y2
        if m1 and m1.lower() in _MONTHS and y1 and y2:
            try:
                start = datetime(int(y1), _MONTHS[m1.lower()], int(parts["d1"]))
                end = datetime(int(y2), _MONTHS[parts["m2"].lower()], int(parts["d2"]))
                return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            except (KeyError, ValueError):
                pass

    return "", ""


def _classify_genre(start_date: str, today: datetime | None = None) -> str:
    """Within ±OPENING_WINDOW_DAYS of start_date → 'Gallery Opening', else 'Exhibition'."""
    if not start_date:
        return "Exhibition"
    if today is None:
        today = datetime.now()
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return "Exhibition"
    return "Gallery Opening" if abs((sd - today).days) <= OPENING_WINDOW_DAYS else "Exhibition"


def _parse_reception(reception: str, fallback_date: str) -> tuple[str, str]:
    """Parse a reception string to (date, time).

    Supports both Zwirner US-style ("Thursday, April 23, 6–8 PM") and Marian
    Goodman UK-style ("Friday, 9 May 2025, 6 - 8 pm"). Returns
    (fallback_date, DEFAULT_RECEPTION_TIME) if the string is empty or can't
    be parsed.
    """
    if not reception:
        return fallback_date, DEFAULT_RECEPTION_TIME
    date_str = fallback_date
    fallback_year = int(fallback_date[:4]) if fallback_date else None

    # Try US "Month Day[, Year]" — anchored to a month name *not* immediately
    # preceded by a digit (which would be the UK day-first form).
    us = re.search(
        r"(?<!\d\s)(?<!\d)([A-Za-z]+)\s+(\d{1,2})(?:[a-z]{0,2})?(?:,\s*(\d{4}))?", reception
    )
    if us and us.group(1).lower() in _MONTHS:
        month = _MONTHS[us.group(1).lower()]
        day = int(us.group(2))
        year = int(us.group(3)) if us.group(3) else fallback_year
        if year:
            try:
                date_str = datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                date_str = fallback_date
    else:
        # UK "Day Month [Year]"
        uk = re.search(r"(\d{1,2})(?:[a-z]{0,2})?\s+([A-Za-z]+)(?:\s+(\d{4}))?", reception)
        if uk and uk.group(2).lower() in _MONTHS:
            month = _MONTHS[uk.group(2).lower()]
            day = int(uk.group(1))
            year = int(uk.group(3)) if uk.group(3) else fallback_year
            if year:
                try:
                    date_str = datetime(year, month, day).strftime("%Y-%m-%d")
                except ValueError:
                    date_str = fallback_date

    # time: "6–8 PM" / "6 PM" / "6:30 PM" / "6 - 8 pm"
    tm = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*(?:[–\-—]\s*\d{1,2}(?::\d{2})?)?\s*(AM|PM|am|pm)", reception
    )
    time_str = DEFAULT_RECEPTION_TIME
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2)) if tm.group(2) else 0
        meridiem = tm.group(3).upper()
        if meridiem == "PM" and hour < 12:
            hour += 12
        if meridiem == "AM" and hour == 12:
            hour = 0
        time_str = f"{hour:02d}:{minute:02d}"
    return date_str, time_str


def _extract_next_data(html: str) -> dict[str, Any]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Per-gallery fetchers
# ---------------------------------------------------------------------------
def _build_event(
    *,
    gallery_key: str,
    name: str,
    date: str,
    time: str,
    genre: str,
    url: str,
    image: str = "",
    description: str = "",
    external_id: str = "",
) -> dict[str, Any]:
    venue = GALLERY_VENUES[gallery_key]
    return {
        "provider": PROVIDER,
        "external_id": external_id or f"{gallery_key}::{name}::{date}",
        "name": name,
        "date": date,
        "time": time,
        "venue_name": venue["venue_name"],
        "address": venue["address"],
        "neighborhood": venue["neighborhood"],
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": genre,
        "price": "",
        "url": url,
        "image": image,
        "description": description[:1200],
    }


def _fetch_zwirner_sync() -> list[dict[str, Any]]:
    """David Zwirner — curl_cffi + __NEXT_DATA__ (Cloudflare-fronted)."""
    r = cf_requests.get(
        "https://www.davidzwirner.com/exhibitions",
        **_impersonate_kw(),
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    data = _extract_next_data(r.text)
    pp_data = data.get("props", {}).get("pageProps", {}).get("data", {})
    pool: list[dict[str, Any]] = []
    pool.extend(pp_data.get("nowOpen") or [])
    pool.extend(pp_data.get("upcoming") or [])

    events: list[dict[str, Any]] = []
    for item in pool:
        # Filter to NY locations
        locations = item.get("locations") or []
        in_ny = any(
            (loc.get("address") or {}).get("city", "").lower().startswith("new york")
            or (loc.get("name") or "").lower().startswith("new york")
            for loc in locations
            if isinstance(loc, dict)
        )
        if not in_ny:
            continue
        title = item.get("title") or ""
        if not title:
            continue
        start_date = (item.get("startDate") or "")[:10]
        if not start_date:
            continue
        reception_str = item.get("reception") or ""
        date_str, time_str = _parse_reception(reception_str, start_date)
        genre = "Gallery Opening" if reception_str else _classify_genre(start_date)
        slug = (item.get("slug") or {}).get("current", "")
        url = f"https://www.davidzwirner.com{slug}" if slug.startswith("/") else slug
        subtitle = item.get("subtitle") or ""
        description = subtitle if subtitle else (item.get("summary") or "")
        events.append(
            _build_event(
                gallery_key="david_zwirner",
                name=title,
                date=date_str,
                time=time_str,
                genre=genre,
                url=url or "https://www.davidzwirner.com/exhibitions",
                description=description or "",
                external_id=f"zwirner::{item.get('_id') or slug}",
            )
        )
    return events


def _fetch_pace_sync() -> list[dict[str, Any]]:
    """Pace Gallery — static HTML, ``index-grid__link`` cards in ``New York`` group."""
    r = httpx.get(
        "https://www.pacegallery.com/exhibitions/", headers=_HEADERS, timeout=DEFAULT_TIMEOUT
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    events: list[dict[str, Any]] = []
    for card in soup.select("a.index-grid__link"):
        title_el = card.select_one(".index-grid__text-title")
        date_el = card.select_one(".index-grid__text-date")
        loc_el = card.select_one(".index-grid__text-location")
        if not (title_el and date_el and loc_el):
            continue
        location = loc_el.get_text(strip=True)
        if location.strip().lower() != "new york":
            continue
        title = title_el.get_text(strip=True)
        start_date, _ = _parse_date_range(date_el.get_text(strip=True))
        if not (title and start_date):
            continue
        href = card.get("href") or ""
        url = f"https://www.pacegallery.com{href}" if href.startswith("/") else href
        img_el = card.select_one("img")
        image = img_el.get("src") if img_el else ""
        events.append(
            _build_event(
                gallery_key="pace",
                name=title,
                date=start_date,
                time=DEFAULT_RECEPTION_TIME,
                genre=_classify_genre(start_date),
                url=url or "https://www.pacegallery.com/exhibitions/",
                image=image or "",
                description=date_el.get_text(strip=True),
                external_id=f"pace::{href.strip('/')}",
            )
        )
    return events


_HW_NY_LOCATIONS = [
    ("hauser_wirth_22nd", "/locations/10073-hauser-wirth-new-york-22nd-street/"),
    ("hauser_wirth_18th", "/locations/41189-hauser-wirth-18th-street/"),
    ("hauser_wirth_wooster", "/locations/new-york-wooster-street/"),
]


def _fetch_hauser_wirth_sync() -> list[dict[str, Any]]:
    """Hauser & Wirth — three NY location pages, curl_cffi + __NEXT_DATA__."""
    events: list[dict[str, Any]] = []
    for gallery_key, path in _HW_NY_LOCATIONS:
        try:
            r = cf_requests.get(
                f"https://www.hauserwirth.com{path}",
                **_impersonate_kw(),
                timeout=DEFAULT_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as exc:  # pragma: no cover — network
            logger.warning("hauser_wirth %s fetch failed: %s", gallery_key, exc)
            continue
        data = _extract_next_data(r.text)
        rel = data.get("props", {}).get("pageProps", {}).get("relatedData", {})
        for item in rel.get("exhibitions", []) or []:
            title = item.get("title") or ""
            start = (item.get("startDate") or "")[:10]
            if not (title and start):
                continue
            slug = item.get("slug") or ""
            url = (
                f"https://www.hauserwirth.com/exhibitions/{slug}/"
                if slug
                else "https://www.hauserwirth.com/exhibitions/"
            )
            events.append(
                _build_event(
                    gallery_key=gallery_key,
                    name=title,
                    date=start,
                    time=DEFAULT_RECEPTION_TIME,
                    genre=_classify_genre(start),
                    url=url,
                    description=item.get("dateOverride") or "",
                    external_id=f"hw::{gallery_key}::{slug}",
                )
            )
    return events


_GAGOSIAN_NY_HINTS = ("new york", "madison", "park", "west 24", "west 21", "980 madison")


def _fetch_gagosian_sync() -> list[dict[str, Any]]:
    """Gagosian — Next.js, plain httpx works."""
    r = httpx.get("https://gagosian.com/exhibitions/", headers=_HEADERS, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    data = _extract_next_data(r.text)
    items = data.get("props", {}).get("pageProps", {}).get("exhibitions", []) or []
    events: list[dict[str, Any]] = []
    for item in items:
        location = (item.get("location_str") or "").lower()
        if not any(h in location for h in _GAGOSIAN_NY_HINTS):
            continue
        title = item.get("title") or ""
        dates_display = item.get("dates_display") or ""
        start, _ = _parse_date_range(dates_display)
        if not (title and start):
            continue
        rel_url = item.get("absolute_url") or ""
        url = f"https://gagosian.com{rel_url}" if rel_url.startswith("/") else rel_url
        # thumbnail.sizes is a dict of size→url; pick any url-bearing entry.
        image = ""
        thumb = item.get("thumbnail") or {}
        sizes = thumb.get("sizes") if isinstance(thumb, dict) else None
        if isinstance(sizes, dict):
            for v in sizes.values():
                if isinstance(v, str) and v.startswith("http"):
                    image = v
                    break
        subtitle = item.get("subtitle") or item.get("subtitle_2") or ""
        description = " — ".join(p for p in (subtitle, dates_display) if p)
        events.append(
            _build_event(
                gallery_key="gagosian",
                name=title,
                date=start,
                time=DEFAULT_RECEPTION_TIME,
                genre=_classify_genre(start),
                url=url or "https://gagosian.com/exhibitions/",
                image=image,
                description=description,
                external_id=f"gagosian::{rel_url.strip('/')}",
            )
        )
    return events


def _fetch_marian_goodman_sync() -> list[dict[str, Any]]:
    """Marian Goodman — bespoke HTML.

    Cards are ``div.area > a[href^="/exhibitions/"]`` with ``.location``,
    ``.heading_title``, ``.subheading`` plus a sibling ``div.bottom`` carrying
    the date range and an optional ``div.bottom.additional_date`` carrying
    "Opening Reception: ...". When the reception line is present we use its
    date/time exactly; otherwise the show start date with 18:00 fallback.
    """
    r = httpx.get(
        "https://www.mariangoodman.com/exhibitions/new-york/",
        headers=_HEADERS,
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Each "item" wraps a card and its bottom block.
    for item in soup.select("div.item"):
        link = item.select_one("div.area a[href^='/exhibitions/']")
        if not link:
            continue
        href = link.get("href") or ""
        # Skip nav/index links like /exhibitions/new-york/past/2024/
        if "/past/" in href or href.count("/") < 3:
            continue
        if href in seen:
            continue
        seen.add(href)
        title_el = link.select_one(".heading_title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        loc_el = link.select_one(".location")
        location = loc_el.get_text(strip=True).lower() if loc_el else ""
        if "new york" not in location:
            continue
        sub_el = link.select_one(".subheading")
        subtitle = sub_el.get_text(strip=True) if sub_el else ""
        bottoms = item.select("div.bottom")
        date_text = bottoms[0].get_text(" ", strip=True) if bottoms else ""
        reception_text = ""
        for b in bottoms[1:]:
            text = b.get_text(" ", strip=True)
            if "opening reception" in text.lower() or "reception" in text.lower():
                reception_text = text
                break
        start, _ = _parse_date_range(date_text)
        if not (title and start):
            continue
        if reception_text:
            event_date, event_time = _parse_reception(reception_text, start)
            genre = "Gallery Opening"
        else:
            event_date, event_time = start, DEFAULT_RECEPTION_TIME
            genre = _classify_genre(start)
        url = f"https://www.mariangoodman.com{href}"
        img_el = item.select_one("img")
        image = (img_el.get("src") or img_el.get("data-src") or "") if img_el else ""
        description = " — ".join(p for p in (subtitle, date_text, reception_text) if p)
        events.append(
            _build_event(
                gallery_key="marian_goodman",
                name=title,
                date=event_date,
                time=event_time,
                genre=genre,
                url=url,
                image=image,
                description=description,
                external_id=f"marian_goodman::{href.strip('/')}",
            )
        )
    return events


def _fetch_lehmann_maupin_sync() -> list[dict[str, Any]]:
    """Lehmann Maupin — ``lehmannmaupin.com/exhibitions``, filter to NY."""
    r = httpx.get(
        "https://www.lehmannmaupin.com/exhibitions",
        headers=_HEADERS,
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return _parse_artlogic_entries(
        r.text,
        gallery_key="lehmann_maupin",
        base="https://www.lehmannmaupin.com",
        location_filter="new york",
    )


def _fetch_sean_kelly_sync() -> list[dict[str, Any]]:
    """Sean Kelly — ``skny.com/exhibitions``, filter to NY."""
    r = httpx.get(
        "https://www.skny.com/exhibitions",
        headers=_HEADERS,
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return _parse_artlogic_entries(
        r.text,
        gallery_key="sean_kelly",
        base="https://www.skny.com",
        location_filter="new york",
    )


def _parse_artlogic_entries(
    html: str,
    *,
    gallery_key: str,
    base: str,
    location_filter: str = "",
) -> list[dict[str, Any]]:
    """Parse Artlogic-style ``<div class="entry">`` exhibition cards.

    Card layout:
        <div class="entry"><a href="/exhibitions/<slug>">...
            <h1>{title}</h1>
            <h2>{subtitle}</h2>?
            <h2 class="subtitle2">{location}</h2>
            <h3>{date range}</h3>
        </a></div>
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in soup.select("div.entry"):
        link = entry.find("a", href=True)
        if not link:
            continue
        href = link["href"]
        if not href.startswith("/exhibitions/") or href in seen:
            continue
        seen.add(href)
        title_el = entry.find("h1")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        # Locate the location and date headers
        h2_tags = entry.find_all("h2")
        h3_tags = entry.find_all("h3")
        location = ""
        for h2 in h2_tags:
            txt = h2.get_text(strip=True)
            cls = " ".join(h2.get("class") or [])
            if "subtitle2" in cls or any(
                c.lower() in txt.lower() for c in ("new york", "london", "los angeles", "seoul")
            ):
                location = txt
                break
        if location_filter and location_filter not in location.lower():
            continue
        date_text = h3_tags[0].get_text(strip=True) if h3_tags else ""
        start, _ = _parse_date_range(date_text)
        if not (title and start):
            continue
        url = f"{base}{href}" if href.startswith("/") else href
        img = entry.find("img")
        image = ""
        if img:
            image = img.get("src") or ""
        subtitle_el = next(
            (h for h in h2_tags if "subtitle2" not in " ".join(h.get("class") or [])),
            None,
        )
        subtitle = subtitle_el.get_text(strip=True) if subtitle_el else ""
        description = " — ".join(p for p in (subtitle, location, date_text) if p)
        events.append(
            _build_event(
                gallery_key=gallery_key,
                name=title,
                date=start,
                time=DEFAULT_RECEPTION_TIME,
                genre=_classify_genre(start),
                url=url,
                image=image,
                description=description,
                external_id=f"{gallery_key}::{href.strip('/')}",
            )
        )
    return events


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------
_FETCHERS: list[tuple[str, Any]] = [
    ("david_zwirner", _fetch_zwirner_sync),
    ("pace", _fetch_pace_sync),
    ("hauser_wirth", _fetch_hauser_wirth_sync),
    ("gagosian", _fetch_gagosian_sync),
    ("marian_goodman", _fetch_marian_goodman_sync),
    ("lehmann_maupin", _fetch_lehmann_maupin_sync),
    ("sean_kelly", _fetch_sean_kelly_sync),
]


def _filter_window(
    events: list[dict[str, Any]], start_date: str, end_date: str
) -> list[dict[str, Any]]:
    if not start_date and not end_date:
        return events
    return [
        e
        for e in events
        if (not start_date or e["date"] >= start_date) and (not end_date or e["date"] <= end_date)
    ]


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
    """Fetch Chelsea/major-NYC gallery exhibitions; defaults to next 14 days.

    Per-gallery failures are logged at WARNING and ignored; one broken site
    never blocks the rest.
    """
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    async def _run(name: str, fn: Any) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            logger.warning("chelsea_galleries %s failed: %s", name, exc)
            return []

    results = await asyncio.gather(
        *[_run(name, fn) for name, fn in _FETCHERS], return_exceptions=False
    )

    all_events: list[dict[str, Any]] = []
    for (name, _fn), out in zip(_FETCHERS, results, strict=False):
        logger.info("chelsea_galleries %s: %d raw events", name, len(out))
        all_events.extend(out)

    # Dedup by external_id (Hauser & Wirth's three locations could in theory
    # surface duplicates if the site changes; this guards future drift).
    deduped: dict[str, dict[str, Any]] = {}
    for e in all_events:
        deduped[e["external_id"]] = e

    return _filter_window(list(deduped.values()), start_date, end_date)


# Convenience alias for the ingest harness.
async def search_events(
    start_date: str = "", end_date: str = "", **_: object
) -> list[dict[str, Any]]:
    return await fetch_events(start_date, end_date)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    out = asyncio.run(fetch_events(today, end))
    print(f"\n{len(out)} events {today} → {end}\n")
    for e in out:
        print(f"  {e['date']} {e['time']} | {e['venue_name']:40s} | {e['genre']:16s} | {e['name']}")
    sys.exit(0)
