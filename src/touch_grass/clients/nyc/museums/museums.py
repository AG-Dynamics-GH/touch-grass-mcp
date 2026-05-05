"""Museum client — exhibitions and collection data across NYC museums.

Met: official Collection API at collectionapi.metmuseum.org (no auth).
MoMA: scrapes /calendar/exhibitions (Cloudflare-protected; often empty).
Whitney, Cooper Hewitt, New Museum: scraped/API'd directly.
"""

from __future__ import annotations

import contextlib
import html as html_lib
import re
from datetime import UTC, date, datetime

import httpx

_FULL_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
_MONTH_NAMES: dict[str, int] = {}
for _i, _m in enumerate(_FULL_MONTHS):
    _MONTH_NAMES[_m.lower()] = _i + 1
    _MONTH_NAMES[_m[:3].lower()] = _i + 1

MET_API = "https://collectionapi.metmuseum.org/public/collection/v1"
MET_EXHIBITIONS_URL = "https://www.metmuseum.org/exhibitions"
MOMA_EXHIBITIONS_URL = "https://www.moma.org/calendar/exhibitions"
WHITNEY_EXHIBITIONS_URL = "https://whitney.org/exhibitions"
COOPERHEWITT_EVENTS_API = "https://www.cooperhewitt.org/wp-json/wp/v2/ch_events"
NEWMUSEUM_EXHIBITIONS_URL = "https://www.newmuseum.org/exhibitions/"


def _headers() -> dict:
    return {
        "Accept": "text/html,application/json",
        "User-Agent": "Mozilla/5.0 events-mcp",
    }


# ---------------------------------------------------------------------------
# Met Museum — Collection API + exhibitions scrape
# ---------------------------------------------------------------------------


async def search_met_collection(
    query: str,
    has_images: bool = True,
    limit: int = 10,
) -> list[dict]:
    """Search the Met collection by keyword. Returns object summaries."""
    params = {"q": query, "hasImages": "true" if has_images else "false"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{MET_API}/search", params=params)
        resp.raise_for_status()
        ids = resp.json().get("objectIDs", []) or []
        ids = ids[:limit]
        if not ids:
            return []

        objs = []
        for oid in ids:
            try:
                r = await client.get(f"{MET_API}/objects/{oid}")
                r.raise_for_status()
                objs.append(r.json())
            except httpx.HTTPError:
                continue

    return [
        {
            "id": o.get("objectID"),
            "title": o.get("title", ""),
            "artist": o.get("artistDisplayName", ""),
            "date": o.get("objectDate", ""),
            "medium": o.get("medium", ""),
            "department": o.get("department", ""),
            "gallery": o.get("GalleryNumber", ""),
            "image": o.get("primaryImageSmall", ""),
            "url": o.get("objectURL", ""),
            "is_on_view": bool(o.get("GalleryNumber")),
        }
        for o in objs
    ]


async def get_met_exhibitions(limit: int = 15) -> list[dict]:
    """Scrape current and upcoming Met exhibitions from the website."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(MET_EXHIBITIONS_URL, headers=_headers())
        resp.raise_for_status()
        html = resp.text

    return _parse_met_exhibitions(html, limit)


def _parse_met_exhibitions(html: str, limit: int) -> list[dict]:
    """Extract exhibition titles, dates, URLs from Met HTML.

    Met's site dropped <h*>/<time> tags — titles and dates now appear as bare
    text nodes inside exhibition cards. Strip tags and split on common date
    prefixes like "Through", "Opens", "Opening", or date ranges.
    """
    # Each exhibition card has a title <a> then a sibling meta <div> with the
    # date. Scan <a> tags that directly wrap title text, then peek forward ~800
    # chars for the first date-like string ("Through June 28", "Opens May 5",
    # "On view Apr 1 – Jun 30").
    by_url: dict[str, dict] = {}
    link_pattern = re.compile(
        r'<a[^>]+href="(/exhibitions/[^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    date_pattern = re.compile(
        r"(Through\s+[A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?"
        r"|Opens?\s+[A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?"
        r"|Opening\s+[A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?"
        r"|On view[^<\"]{0,80}"
        r"|[A-Z][a-z]+\s+\d{1,2}\s*[-–]\s*[A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?"
        r"|[A-Z][a-z]+\s+\d{1,2},\s*\d{4}\s*[-–]\s*[A-Z][a-z]+\s+\d{1,2},\s*\d{4})"
    )

    for m in link_pattern.finditer(html):
        url_path = m.group(1)
        title = re.sub(r"<[^>]+>", " ", m.group(2))
        title = html_lib.unescape(re.sub(r"\s+", " ", title).strip())
        if not title or len(title) < 3 or len(title) > 200:
            continue
        tail = html[m.end() : m.end() + 1200]
        dm = date_pattern.search(tail)
        dates = dm.group(1).strip() if dm else ""
        prior = by_url.get(url_path)
        if prior and prior.get("dates") and not dates:
            continue
        by_url[url_path] = {
            "title": title,
            "dates": dates,
            "url": f"https://www.metmuseum.org{url_path}",
            "venue": "The Met",
            "source": "met",
        }

    return list(by_url.values())[:limit]


# ---------------------------------------------------------------------------
# MoMA — exhibitions scrape from Next.js __NEXT_DATA__
# ---------------------------------------------------------------------------


async def get_moma_exhibitions(limit: int = 15) -> list[dict]:
    """Scrape current and upcoming MoMA exhibitions.

    MoMA actively blocks server-side scraping with 403 (Cloudflare/Akamai bot
    protection) regardless of User-Agent. Use a realistic browser UA + referer
    and best-effort continue; caller should tolerate empty results.
    """
    browser_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    headers = {
        "User-Agent": browser_ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.moma.org/",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(MOMA_EXHIBITIONS_URL, headers=headers)
        if resp.status_code == 403:
            return []
        resp.raise_for_status()
        html = resp.text

    return _parse_moma_exhibitions(html, limit)


def _parse_moma_exhibitions(html: str, limit: int) -> list[dict]:
    """Extract MoMA exhibition entries from page HTML."""
    exhibitions = []
    pattern = re.compile(
        r'<a[^>]+href="(/calendar/exhibitions/\d+[^"]*)"[^>]*>.*?'
        r"<h\d[^>]*>([^<]+)</h\d>.*?"
        r"(?:<time[^>]*>([^<]+)</time>|<p[^>]*>([^<]+)</p>)",
        re.DOTALL,
    )
    seen_urls = set()
    for m in pattern.finditer(html):
        url_path = m.group(1)
        if url_path in seen_urls:
            continue
        seen_urls.add(url_path)
        title = m.group(2).strip()
        dates = (m.group(3) or m.group(4) or "").strip()
        exhibitions.append(
            {
                "title": title,
                "dates": dates,
                "url": f"https://www.moma.org{url_path}",
                "venue": "MoMA",
                "source": "moma",
            }
        )
        if len(exhibitions) >= limit:
            break
    return exhibitions


# ---------------------------------------------------------------------------
# Whitney Museum — scrape exhibitions page
# ---------------------------------------------------------------------------


async def get_whitney_exhibitions(limit: int = 15) -> list[dict]:
    """Scrape current and upcoming Whitney exhibitions."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(WHITNEY_EXHIBITIONS_URL, headers=_headers())
        resp.raise_for_status()
        html = resp.text

    return _parse_whitney_exhibitions(html, limit)


# Skip-list of hub pages that aren't actual exhibitions.
_WHITNEY_HUB_SLUGS = {"performance", "archive", "virtual", "past", "upcoming", "current", ""}
_DATE_CUE = re.compile(r"Through|Opens?|Opening|On view|\d{4}|[A-Z][a-z]+\s+\d", re.I)


def _parse_whitney_exhibitions(html: str, limit: int) -> list[dict]:
    """Title and dates are both inside the <a>, pipe-separated after tag-strip."""
    by_slug: dict[str, dict] = {}
    for m in re.finditer(
        r'<a[^>]+href="/exhibitions/([^"#?/]+)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    ):
        slug = m.group(1)
        if slug in _WHITNEY_HUB_SLUGS:
            continue
        inner = re.sub(r"<[^>]+>", " | ", m.group(2))
        inner = html_lib.unescape(re.sub(r"\s+", " ", inner).strip(" |"))
        parts = [p.strip() for p in inner.split("|") if p.strip()]
        if not parts:
            continue

        # Heuristic: drop leading eyebrow labels ("Upcoming", "Last chance", "Online"),
        # then title is the first substantive part, date is the first date-matching part.
        eyebrows = {"upcoming", "last chance", "online", "on view"}
        while parts and parts[0].lower() in eyebrows:
            parts.pop(0)
        if not parts:
            continue
        title = parts[0]
        if len(title) < 3 or len(title) > 200:
            continue
        dates = ""
        for p in parts[1:]:
            if _DATE_CUE.search(p) and len(p) < 60:
                dates = p
                break

        prior = by_slug.get(slug)
        if prior and prior.get("dates") and not dates:
            continue
        by_slug[slug] = {
            "title": title,
            "dates": dates,
            "url": f"https://whitney.org/exhibitions/{slug}",
            "venue": "Whitney Museum",
            "source": "whitney",
        }

    return list(by_slug.values())[:limit]


# ---------------------------------------------------------------------------
# Cooper Hewitt — WordPress REST API (custom post type `ch_events`)
# ---------------------------------------------------------------------------


async def get_cooperhewitt_exhibitions(limit: int = 15) -> list[dict]:
    """Fetch Cooper Hewitt programs/exhibitions from their public WP REST API."""
    params = {"per_page": min(max(limit, 1), 50), "orderby": "date", "order": "desc"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(COOPERHEWITT_EVENTS_API, headers=_headers(), params=params)
        resp.raise_for_status()
        items = resp.json()

    out: list[dict] = []
    for it in items:
        title_html = (it.get("title") or {}).get("rendered", "")
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        if not title:
            continue
        # Event dates live in WP post_meta as Unix timestamps (seconds).
        meta = it.get("meta") or {}
        start_ts = meta.get("_ch_exhibition_on_view_date")
        end_ts = meta.get("_ch_exhibition_to_date")

        def _fmt(ts: object) -> str:
            try:
                return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%b %d, %Y")
            except (TypeError, ValueError):
                return ""

        start = _fmt(start_ts)
        end = _fmt(end_ts)
        if start and end and start != end:
            dates = f"{start} – {end}"
        elif start:
            dates = start
        else:
            dates = ""
        out.append(
            {
                "title": title,
                "dates": dates,
                "url": it.get("link") or meta.get("_ch_event_link", ""),
                "ticket_url": meta.get("_ch_event_link", ""),
                "venue": "Cooper Hewitt",
                "source": "cooperhewitt",
            }
        )
    return out[:limit]


# ---------------------------------------------------------------------------
# New Museum — parse __NEXT_DATA__ Apollo state
# ---------------------------------------------------------------------------


async def get_newmuseum_exhibitions(limit: int = 15) -> list[dict]:
    """Extract current + upcoming New Museum exhibitions from embedded Apollo state."""
    import json as _json

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(NEWMUSEUM_EXHIBITIONS_URL, headers=_headers())
        resp.raise_for_status()
        html = resp.text

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return []
    try:
        data = _json.loads(m.group(1))
    except _json.JSONDecodeError:
        return []

    # Exhibitions live at pageProps.__TEMPLATE_QUERY_DATA__.page.blocks[*].exhibitions[*]
    # with __typename == "Exhibition" (WPGraphQL/Faust).
    template = data.get("props", {}).get("pageProps", {}).get("__TEMPLATE_QUERY_DATA__", {})
    blocks = (template.get("page") or {}).get("blocks") or []
    exhibitions: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for ex in block.get("exhibitions") or []:
            if not isinstance(ex, dict) or ex.get("__typename") != "Exhibition":
                continue
            title_raw = ex.get("title") or ex.get("name") or ""
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", title_raw)).strip()
            if not title:
                continue
            url = ex.get("link") or NEWMUSEUM_EXHIBITIONS_URL
            # Prefer the museum's prose date string when provided — it handles
            # "Ongoing", multi-phase runs, etc. Fall back to start/end ISO dates.
            date_override = (ex.get("dateTextOverride") or "").strip()
            start = (ex.get("startDate") or "")[:10]
            end = (ex.get("endDate") or "")[:10]
            if date_override:
                dates = date_override
            elif start and end:
                dates = f"{start} – {end}"
            elif start:
                dates = f"Opens {start}"
            elif end:
                dates = f"Through {end}"
            else:
                dates = ""
            exhibitions.append(
                {
                    "title": title,
                    "dates": dates,
                    "url": url,
                    "venue": "New Museum",
                    "source": "newmuseum",
                }
            )

    seen: set[str] = set()
    unique: list[dict] = []
    for e in exhibitions:
        if e["title"] in seen:
            continue
        seen.add(e["title"])
        unique.append(e)
    return unique[:limit]


# ---------------------------------------------------------------------------
# Aggregator + events_index normalizer
# ---------------------------------------------------------------------------


_VENUE_META = {
    "met": ("The Met", "Museum Mile", "Manhattan", "1000 5th Ave, New York, NY 10028"),
    "moma": (
        "Museum of Modern Art",
        "Midtown West",
        "Manhattan",
        "11 W 53rd St, New York, NY 10019",
    ),
    "whitney": (
        "Whitney Museum of American Art",
        "Meatpacking District",
        "Manhattan",
        "99 Gansevoort St, New York, NY 10014",
    ),
    "cooperhewitt": (
        "Cooper Hewitt, Smithsonian Design Museum",
        "Carnegie Hill",
        "Manhattan",
        "2 E 91st St, New York, NY 10128",
    ),
    "newmuseum": ("New Museum", "Bowery", "Manhattan", "235 Bowery, New York, NY 10002"),
}


def _parse_exhibition_dates(text: str, today: date | None = None) -> tuple[str, str]:
    """Parse free-form 'dates' text from exhibition pages into (start_iso, end_iso).

    Handles: 'Through June 28', 'Through May 31, 2026', 'April 14 - July 27',
    'April 14 - July 27, 2026', 'Opens June 5'. Returns ('', '') if unparseable.
    """
    today = today or date.today()
    text = text.strip()
    if not text:
        return "", ""

    range_re = re.compile(
        r"(?P<m1>[A-Za-z]+)\s+(?P<d1>\d{1,2})"
        r"(?:\s*[-–—]\s*(?:(?P<m2>[A-Za-z]+)\s+)?(?P<d2>\d{1,2}))?"
        r"(?:,\s*(?P<y>\d{4}))?",
        re.IGNORECASE,
    )

    if re.search(r"\b(through|until|until|ends?|closes?)\b", text, re.IGNORECASE):
        m = range_re.search(text)
        if not m:
            return today.isoformat(), ""
        end_month = _MONTH_NAMES.get(m.group("m1").lower())
        if not end_month:
            return today.isoformat(), ""
        end_day = int(m.group("d1"))
        year = int(m.group("y") or today.year)
        try:
            end = date(year, end_month, end_day)
        except ValueError:
            return today.isoformat(), ""
        if end < today:
            with contextlib.suppress(ValueError):
                end = date(year + 1, end_month, end_day)
        return today.isoformat(), end.isoformat()

    if re.search(r"\b(opens?|begins?|starts?)\b", text, re.IGNORECASE):
        m = range_re.search(text)
        if m:
            month = _MONTH_NAMES.get(m.group("m1").lower())
            if month:
                year = int(m.group("y") or today.year)
                try:
                    start = date(year, month, int(m.group("d1")))
                except ValueError:
                    return "", ""
                if (today - start).days > 60:
                    try:
                        start = date(year + 1, month, int(m.group("d1")))
                    except ValueError:
                        return "", ""
                return start.isoformat(), ""

    m = range_re.search(text)
    if not m:
        return "", ""
    m1 = _MONTH_NAMES.get(m.group("m1").lower())
    if not m1:
        return "", ""
    d1 = int(m.group("d1"))
    if not m.group("d2"):
        year = int(m.group("y") or today.year)
        try:
            start = date(year, m1, d1)
        except ValueError:
            return "", ""
        return start.isoformat(), ""
    m2 = _MONTH_NAMES.get((m.group("m2") or m.group("m1")).lower()) or m1
    d2 = int(m.group("d2"))
    year = int(m.group("y") or today.year)
    try:
        start = date(year, m1, d1)
    except ValueError:
        return "", ""
    end_year = year + 1 if m2 < m1 else year
    try:
        end = date(end_year, m2, d2)
    except ValueError:
        return start.isoformat(), ""
    if end < today:
        return "", ""
    return start.isoformat(), end.isoformat()


def _normalize_for_index(item: dict) -> dict | None:
    title = (item.get("title") or "").strip()
    if not title:
        return None
    src = (item.get("source") or "").lower()
    venue_name, neighborhood, borough, address = _VENUE_META.get(
        src, (item.get("venue", ""), "", "", "")
    )
    start_iso, end_iso = _parse_exhibition_dates(item.get("dates") or "")
    if not start_iso:
        start_iso = date.today().isoformat()
    return {
        "name": title,
        "date": start_iso,
        "end_date": end_iso,
        "time": "",
        "venue_name": venue_name,
        "neighborhood": neighborhood,
        "borough": borough,
        "city": "New York",
        "state": "NY",
        "address": address,
        "genre": "Exhibition",
        "url": item.get("url", ""),
        "description": item.get("dates", ""),
        "provider": "museums",
        "external_id": item.get("url", "") or f"{src}:{title}",
    }


async def get_all_exhibitions(limit_per_museum: int = 10) -> list[dict]:
    """Fetch exhibitions from all configured museums in parallel.

    Output is normalized to the events_index schema (name/date/end_date/venue_name)
    so exhibitions actually persist into the events DB instead of being silently
    dropped at insert time.
    """
    import asyncio

    results = await asyncio.gather(
        get_met_exhibitions(limit_per_museum),
        get_moma_exhibitions(limit_per_museum),
        get_whitney_exhibitions(limit_per_museum),
        get_cooperhewitt_exhibitions(limit_per_museum),
        get_newmuseum_exhibitions(limit_per_museum),
        return_exceptions=True,
    )
    items: list[dict] = []
    for r in results:
        if isinstance(r, list):
            for raw in r:
                normalized = _normalize_for_index(raw)
                if normalized:
                    items.append(normalized)
    return items
