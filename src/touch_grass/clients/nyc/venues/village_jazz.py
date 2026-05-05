"""Village/Times Square jazz triumvirate (and friends) — combined client.

Five NYC jazz venues, each with a distinct ingest pattern. Order of priority
matches the user's preference for the WV/EV trio first.

1. Village Vanguard (vv) — villagevanguard.com
   SSR HTML page with ``<div class="event-listing">`` cards. Each card has
   ``<h2>`` artist, ``<h3>`` date range (e.g. "April 28 — May 3"), and a
   ``squadup.com`` ticket button. Date ranges expand to a daily entry per
   day in the run. Genre always Jazz.

2. Smalls Jazz Club (smalls) — smallslive.com
   Their own home page only renders today + tomorrow shows; there is no
   public month-view URL. We pull the visible shows and date-stamp from the
   ``<div class="title5 sets">Wed Apr 29</div>`` line. ~16 events on any
   given pull. Acceptable as "what's playing right now" coverage.

3. Mezzrow (mezzrow) — mezzrow.com
   Mezzrow now redirects to the SmallsLIVE platform and uses identical
   markup. Same parser as #2; only the source URL and venue name differ.

4. Blue Note NYC (bluenote) — bluenotejazz.com
   WordPress mini_calendar at /nyc/shows/ paginated with
   ``?calendar_view&month=M&yr=YYYY``. One ``<td>`` per day with a
   ``<div class='day-wrap single-show'>`` that has artist, image, showtimes,
   and venue (Blue Note Jazz Club / Sorry Charlie's downstairs / etc.).

5. Birdland (birdland) — birdlandjazz.com
   WordPress + TicketWeb plugin. The /calendar/ page injects a JSON nonce
   in ``my_ajax_object``; we POST to /wp-admin/admin-ajax.php with action
   ``get_events_artist_calendar`` and a date range to retrieve all shows
   (Birdland Jazz Club + Birdland Theater) as JSON.

Each venue's fetcher is independent — a failure in one does not block the
others (we ``asyncio.gather`` with ``return_exceptions=True``).
"""

from __future__ import annotations

import asyncio
import calendar as cal_mod
import html as html_mod
import json
import logging
import re
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger("events.village_jazz")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

VENUES = {
    "vanguard": {
        "name": "Village Vanguard",
        "address": "178 7th Avenue South, New York, NY 10014",
        "neighborhood": "West Village",
    },
    "smalls": {
        "name": "Smalls Jazz Club",
        "address": "183 W 10th Street, New York, NY 10014",
        "neighborhood": "West Village",
    },
    "mezzrow": {
        "name": "Mezzrow",
        "address": "163 W 10th Street, New York, NY 10014",
        "neighborhood": "West Village",
    },
    "bluenote": {
        "name": "Blue Note Jazz Club",
        "address": "131 W 3rd Street, New York, NY 10012",
        "neighborhood": "Greenwich Village",
    },
    "birdland": {
        "name": "Birdland Jazz Club",
        "address": "315 W 44th Street, New York, NY 10036",
        "neighborhood": "Hell's Kitchen",
    },
}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip(s: str) -> str:
    return html_mod.unescape(_TAG_RE.sub("", s)).replace("\xa0", " ").strip()


def _parse_time(t: str) -> str:
    """'8:00 PM' / '8:00pm' / '8 PM' -> '20:00'. Empty on failure."""
    if not t:
        return ""
    raw = t.strip().upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return ""


def _within(date_str: str, start: str, end: str) -> bool:
    return start <= date_str <= end


def _shared_fields(venue_key: str) -> dict:
    v = VENUES[venue_key]
    return {
        "venue_name": v["name"],
        "address": v["address"],
        "neighborhood": v["neighborhood"],
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": "Jazz",
    }


# ---------------------------------------------------------------------------
# 1. Village Vanguard
# ---------------------------------------------------------------------------

_VV_URL = "https://villagevanguard.com/"
_VV_LISTING_RE = re.compile(
    r'<div class="event-listing">(.*?)</div>\s*</div>\s*</div>\s*</div>', re.S
)
_VV_TITLE_RE = re.compile(r"<h2>([^<]+)</h2>", re.S)
_VV_DATE_RE = re.compile(r"<h3>(.*?)</h3>", re.S)
_VV_HREF_RE = re.compile(r'href="([^"]+)"[^>]*role="button"')
_VV_IMG_RE = re.compile(r'<img src="([^"]+)"')
_VV_DESC_RE = re.compile(r'<div class="event-short-description">(.*?)</div>', re.S)


def _parse_vv_date_range(raw: str, base_year: int) -> list[str]:
    """'April 28 — May 3' / 'May 5 - May 10' -> list of YYYY-MM-DD covered.

    Both en-dashes (\\u2011) and ASCII '-' appear; tolerate either.
    Returns empty list on parse failure or single-day ranges with no clear date.
    """
    s = _strip(raw).replace("‑", "-").replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return []
    parts = re.split(r"\s*-\s*", s)
    if len(parts) == 1:
        # Single-day "May 5"
        try:
            dt = datetime.strptime(f"{parts[0]} {base_year}", "%B %d %Y")
            return [dt.strftime("%Y-%m-%d")]
        except ValueError:
            return []
    if len(parts) != 2:
        return []
    start_raw, end_raw = parts[0].strip(), parts[1].strip()
    # End may be just "May 3" or just "3" (same month).
    try:
        start_dt = datetime.strptime(f"{start_raw} {base_year}", "%B %d %Y")
    except ValueError:
        return []
    try:
        end_dt = datetime.strptime(f"{end_raw} {base_year}", "%B %d %Y")
    except ValueError:
        try:
            day_only = int(re.sub(r"\D", "", end_raw))
            end_dt = start_dt.replace(day=day_only)
        except (ValueError, OverflowError):
            return []
    if end_dt < start_dt:
        # Cross-month rollover — bump end to next year if needed.
        end_dt = end_dt.replace(year=end_dt.year + 1)
    out: list[str] = []
    cur = start_dt
    while cur <= end_dt:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def _vv_fetch_html() -> str:
    with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        r = client.get(_VV_URL)
        r.raise_for_status()
        return r.text


def _vv_block_meta(block: str, base_year: int) -> tuple[str, list[str], str, str, str] | None:
    """Extract (title, dates, url, img, desc) from one event-listing block. None on miss."""
    title_m = _VV_TITLE_RE.search(block)
    date_m = _VV_DATE_RE.search(block)
    if not title_m or not date_m:
        return None
    title = _strip(title_m.group(1))
    dates = _parse_vv_date_range(date_m.group(1), base_year)
    if not dates:
        return None
    href_m = _VV_HREF_RE.search(block)
    url = href_m.group(1) if href_m else _VV_URL
    img_m = _VV_IMG_RE.search(block)
    img = img_m.group(1) if img_m else ""
    desc_m = _VV_DESC_RE.search(block)
    desc = _strip(desc_m.group(1))[:600] if desc_m else ""
    return title, dates, url, img, desc


async def _fetch_vanguard(start_date: str, end_date: str) -> list[dict]:
    try:
        html = await asyncio.to_thread(_vv_fetch_html)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("Village Vanguard fetch failed: %s", e)
        return []
    base_year = datetime.strptime(start_date, "%Y-%m-%d").year
    base = _shared_fields("vanguard")
    events: list[dict] = []
    for m in _VV_LISTING_RE.finditer(html):
        meta = _vv_block_meta(m.group(1), base_year)
        if not meta:
            continue
        title, dates, url, img, desc = meta
        for d in dates:
            if not _within(d, start_date, end_date):
                continue
            events.append(
                {
                    "provider": "village_jazz",
                    "external_id": f"vanguard_{d}_{re.sub(r'[^a-z0-9]+', '-', title.lower())[:50]}",
                    "name": title,
                    "date": d,
                    "time": "20:00",  # Vanguard sets are 8 & 10 PM nightly; surface the early.
                    "url": url,
                    "image": img,
                    "description": desc,
                    "price": "",
                    **base,
                }
            )
    return events


# ---------------------------------------------------------------------------
# 2 + 3. Smalls + Mezzrow (same engine)
# ---------------------------------------------------------------------------

_SML_ARTICLE_RE = re.compile(
    r'<article class="event-display-today-and-tomorrow item[^"]*">(.*?)</article>',
    re.S,
)
_SML_HREF_RE = re.compile(r'<a href="(/events/[^"]+)"')
_SML_IMG_RE = re.compile(r'<img src="([^"]+)"')
_SML_TITLE_RE = re.compile(r'<p class="event-info-title">([^<]+)</p>')
_SML_VENUE_RE = re.compile(r'<div class="[^"]*venue">([^<]+)</div>')
_SML_DATE_RE = re.compile(
    r'<div class="title5 sets">([A-Z][a-z]{2}) ([A-Z][a-z]{2}) (\d{1,2})</div>'
)
_SML_TIMES_RE = re.compile(r'<div class="title5 sets">Sets at ([0-9: APMapm&;]+)</div>')


def _smalls_like_fetch(url: str) -> str:
    with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def _parse_smalls_like(
    html: str, base_url: str, venue_key: str, start_date: str, end_date: str, base_year: int
) -> list[dict]:
    base = _shared_fields(venue_key)
    events: list[dict] = []
    for m in _SML_ARTICLE_RE.finditer(html):
        block = m.group(1)
        href_m = _SML_HREF_RE.search(block)
        title_m = _SML_TITLE_RE.search(block)
        date_m = _SML_DATE_RE.search(block)
        if not (href_m and title_m and date_m):
            continue
        # date_m: "Wed Apr 29"
        try:
            dt = datetime.strptime(f"{date_m.group(2)} {date_m.group(3)} {base_year}", "%b %d %Y")
        except ValueError:
            continue
        date_str = dt.strftime("%Y-%m-%d")
        if not _within(date_str, start_date, end_date):
            continue
        title = _strip(title_m.group(1))
        url = base_url.rstrip("/") + href_m.group(1)
        img_m = _SML_IMG_RE.search(block)
        img = img_m.group(1) if img_m else ""
        times_m = _SML_TIMES_RE.search(block)
        # "6:00 PM & 7:30 PM" -> early-set 24h
        time_str = ""
        if times_m:
            first = re.split(r"&|;", times_m.group(1))[0]
            time_str = _parse_time(_strip(first))
        eid = f"{venue_key}_{date_str}_" + re.sub(r"[^a-z0-9]+", "-", title.lower())[:50]
        events.append(
            {
                "provider": "village_jazz",
                "external_id": eid,
                "name": title,
                "date": date_str,
                "time": time_str,
                "url": url,
                "image": img,
                "description": "",
                "price": "",
                **base,
            }
        )
    return events


async def _fetch_smalls(start_date: str, end_date: str) -> list[dict]:
    try:
        html = await asyncio.to_thread(_smalls_like_fetch, "https://www.smallslive.com/")
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("Smalls fetch failed: %s", e)
        return []
    base_year = datetime.strptime(start_date, "%Y-%m-%d").year
    return _parse_smalls_like(
        html, "https://www.smallslive.com", "smalls", start_date, end_date, base_year
    )


async def _fetch_mezzrow(start_date: str, end_date: str) -> list[dict]:
    try:
        html = await asyncio.to_thread(_smalls_like_fetch, "https://mezzrow.com/")
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("Mezzrow fetch failed: %s", e)
        return []
    base_year = datetime.strptime(start_date, "%Y-%m-%d").year
    return _parse_smalls_like(
        html, "https://mezzrow.com", "mezzrow", start_date, end_date, base_year
    )


# ---------------------------------------------------------------------------
# 4. Blue Note
# ---------------------------------------------------------------------------

_BN_DAY_RE = re.compile(
    r"<div class='day'>(\d+)</div><div class='day-wrap[^']*'>(.*?)</td>",
    re.S,
)
_BN_HREF_RE = re.compile(r"<h3><a href='([^']+)'>([^<]+)</a></h3>", re.S)
_BN_TIME_RE = re.compile(r"<time>([^<]+)</time>")
_BN_VENUE_RE = re.compile(r"<div class='venue'>([^<]+)</div>")
_BN_IMG_RE = re.compile(r"data-src='([^']+)'")


def _bn_fetch_month(month: int, year: int) -> str:
    url = f"https://www.bluenotejazz.com/nyc/shows/?calendar_view&month={month}&yr={year}"
    with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def _parse_bluenote_month(html: str, year: int, month: int) -> list[dict]:
    base = _shared_fields("bluenote")
    events: list[dict] = []
    for m in _BN_DAY_RE.finditer(html):
        try:
            day = int(m.group(1))
            date_str = f"{year:04d}-{month:02d}-{day:02d}"
            datetime.strptime(date_str, "%Y-%m-%d")  # validate
        except (ValueError, OverflowError):
            continue
        block = m.group(2)
        href_m = _BN_HREF_RE.search(block)
        if not href_m:
            continue
        url = href_m.group(1)
        title = _strip(href_m.group(2))
        time_m = _BN_TIME_RE.search(block)
        time_str = _parse_time(_strip(time_m.group(1))) if time_m else ""
        venue_m = _BN_VENUE_RE.search(block)
        venue_label = _strip(venue_m.group(1)) if venue_m else base["venue_name"]
        img_m = _BN_IMG_RE.search(block)
        img = img_m.group(1) if img_m else ""
        eid = f"bluenote_{date_str}_" + re.sub(r"[^a-z0-9]+", "-", title.lower())[:50]
        record = {
            "provider": "village_jazz",
            "external_id": eid,
            "name": title,
            "date": date_str,
            "time": time_str,
            "url": url,
            "image": img,
            "description": "",
            "price": "",
            **base,
        }
        record["venue_name"] = venue_label or base["venue_name"]
        events.append(record)
    return events


async def _fetch_bluenote(start_date: str, end_date: str) -> list[dict]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    months: list[tuple[int, int]] = []
    cur = start.replace(day=1)
    while cur <= end:
        months.append((cur.month, cur.year))
        # Advance one calendar month.
        last_day = cal_mod.monthrange(cur.year, cur.month)[1]
        cur = cur.replace(day=last_day) + timedelta(days=1)
    out: list[dict] = []
    for month, year in months:
        try:
            html = await asyncio.to_thread(_bn_fetch_month, month, year)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning("Blue Note %s/%s fetch failed: %s", year, month, e)
            continue
        for ev in _parse_bluenote_month(html, year, month):
            if _within(ev["date"], start_date, end_date):
                out.append(ev)
    return out


# ---------------------------------------------------------------------------
# 5. Birdland (TicketWeb plugin AJAX)
# ---------------------------------------------------------------------------

_BIRDLAND_CAL_URL = "https://www.birdlandjazz.com/calendar/"
_BIRDLAND_AJAX_URL = "https://www.birdlandjazz.com/wp-admin/admin-ajax.php"
_BIRDLAND_NONCE_RE = re.compile(r'"nonce":"([a-f0-9]+)"')


def _birdland_pull_nonce() -> tuple[str, httpx.Cookies]:
    with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        r = client.get(_BIRDLAND_CAL_URL)
        r.raise_for_status()
        nonce_m = _BIRDLAND_NONCE_RE.search(r.text)
        return (nonce_m.group(1) if nonce_m else ""), r.cookies


def _birdland_fetch(nonce: str, cookies: httpx.Cookies, start_date: str, end_date: str) -> dict:
    end = datetime.strptime(end_date, "%Y-%m-%d")
    params_obj = {
        "id": None,
        "type": "calendar3",
        "start": start_date,
        "end": end_date,
        "month": str(end.month),
        "year": str(end.year),
        "page": 0,
    }
    data = {
        "action": "get_events_artist_calendar",
        "nonce": nonce,
        "start": start_date,
        "end": end_date,
        "params": json.dumps(params_obj),
        "monthview": "standard",
        "calcount": "1",
    }
    headers = {**_HEADERS, "X-Requested-With": "XMLHttpRequest"}
    with httpx.Client(
        timeout=30, headers=headers, follow_redirects=True, cookies=cookies
    ) as client:
        r = client.post(_BIRDLAND_AJAX_URL, data=data)
        r.raise_for_status()
        return r.json()


def _birdland_time(raw: dict) -> str:
    """Prefer the precise sortkey time, fall back to the human displayTime label."""
    sort_key = raw.get("sortkey", "") or ""
    if " " in sort_key:
        try:
            return sort_key.split(" ", 1)[1][:5]
        except IndexError:
            pass
    return _parse_time(raw.get("displayTime", "") or "")


def _birdland_normalize(raw: dict, base: dict) -> dict | None:
    date_str = raw.get("start", "") or ""
    title = _strip(raw.get("title", "") or raw.get("id", ""))
    if not date_str or not title:
        return None
    time_str = _birdland_time(raw)
    venue_label = _strip(raw.get("venue", "") or "") or base["venue_name"]
    img_m = re.search(r'src="([^"]+)"', raw.get("imageUrl", "") or "")
    eid = f"birdland_{date_str}_{time_str}_" + re.sub(r"[^a-z0-9]+", "-", title.lower())[:50]
    record = {
        "provider": "village_jazz",
        "external_id": eid,
        "name": title,
        "date": date_str,
        "time": time_str,
        "url": raw.get("url", "") or _BIRDLAND_CAL_URL,
        "image": img_m.group(1) if img_m else "",
        "description": "",
        "price": "",
        **base,
    }
    record["venue_name"] = venue_label
    return record


def _parse_birdland(payload: dict, start_date: str, end_date: str) -> list[dict]:
    base = _shared_fields("birdland")
    events: list[dict] = []
    seen: set[str] = set()
    for raw in payload.get("events", []) or []:
        record = _birdland_normalize(raw, base)
        if record is None or not _within(record["date"], start_date, end_date):
            continue
        if record["external_id"] in seen:
            continue
        seen.add(record["external_id"])
        events.append(record)
    return events


async def _fetch_birdland(start_date: str, end_date: str) -> list[dict]:
    try:
        nonce, cookies = await asyncio.to_thread(_birdland_pull_nonce)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("Birdland nonce fetch failed: %s", e)
        return []
    if not nonce:
        logger.warning("Birdland: nonce not found in calendar page")
        return []
    try:
        payload = await asyncio.to_thread(_birdland_fetch, nonce, cookies, start_date, end_date)
    except (httpx.HTTPError, httpx.TimeoutException, json.JSONDecodeError) as e:
        logger.warning("Birdland AJAX failed: %s", e)
        return []
    return _parse_birdland(payload, start_date, end_date)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Fan-out to all five jazz venues. Independent failures don't block the others."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    coros = [
        _fetch_vanguard(start_date, end_date),
        _fetch_smalls(start_date, end_date),
        _fetch_mezzrow(start_date, end_date),
        _fetch_bluenote(start_date, end_date),
        _fetch_birdland(start_date, end_date),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[dict] = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("village_jazz sub-fetch raised: %s", r)
            continue
        out.extend(r)
    return out


async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
