"""Lincoln Center client — full-complex calendar (Geffen, Tully, Vivian Beaumont, etc.).

The Lincoln Center site exposes a server-side AJAX endpoint that powers its
month view of the consolidated complex calendar:

    GET https://www.lincolncenter.org/ajaxCalendar/<Month> <Year>

It returns JSON whose ``calRender`` field is server-rendered HTML — one
``<div class="calendar-day" data-date="...">`` per day, each containing many
``<div class="cal-day-show ...">`` cards. The card's class list also encodes
the resident organization (``new-york-philharmonic``, ``film-at-lincoln-center``,
``the-juilliard-school``, ``jazz-at-lincoln-center``...) and venue
(``david-geffen-hall``, ``alice-tully-hall``, ``wu-tsai-theater``,
``vivian-beaumont``, ``walter-reade-theater``, ``dizzys-club``...).

Inside each card:
    <a href="..."><h2 class="show-name">Title</h2></a>
    <h2 class="show-org"><a ...>Resident Org</a></h2>
    <div class="show-time-price">
        <span class="show-time">7:30 pm</span>
        <span class="show-price">$25</span>
    </div>

This is an aggregator — events are presented by the constituent organizations
(NY Phil, Film at Lincoln Center, Juilliard, etc.). We surface the SPECIFIC
hall as venue_name when classifiable; fall back to "Lincoln Center" otherwise.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
from datetime import datetime, timedelta

import httpx

AJAX_URL = "https://www.lincolncenter.org/ajaxCalendar"
ADDRESS = "10 Lincoln Center Plaza, New York, NY 10023"
NEIGHBORHOOD = "Upper West Side"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html",
    "X-Requested-With": "XMLHttpRequest",
}

# Specific hall classes -> human venue name. Order matters (more specific first).
_VENUE_CLASS_MAP: list[tuple[str, str]] = [
    ("wu-tsai-theater,-david-geffen-hall", "Wu Tsai Theater (David Geffen Hall)"),
    ("wu-tsai-theater", "Wu Tsai Theater"),
    ("david-geffen-hall", "David Geffen Hall"),
    ("alice-tully-hall", "Alice Tully Hall"),
    ("walter-reade-theater", "Walter Reade Theater"),
    ("vivian-beaumont-theater", "Vivian Beaumont Theater"),
    ("vivian-beaumont", "Vivian Beaumont Theater"),
    ("mitzi-e.-newhouse-theater", "Mitzi E. Newhouse Theater"),
    ("rose-theater", "Rose Theater"),
    ("appel-room", "The Appel Room"),
    ("dizzys-club", "Dizzy's Club"),
    ("dizzy's-club", "Dizzy's Club"),
    ("damrosch-park", "Damrosch Park"),
    ("hearst-plaza", "Hearst Plaza"),
    ("clark-studio-theater", "Clark Studio Theater"),
    ("paul-hall", "Paul Hall (Juilliard)"),
    ("kaplan-penthouse", "Kaplan Penthouse"),
    ("metropolitan-opera-house", "Metropolitan Opera House"),
    ("juilliard", "Juilliard School"),
]

# Org class -> classification helpers
_ORG_GENRE_MAP: list[tuple[str, str]] = [
    ("new-york-philharmonic", "Classical"),
    ("metropolitan-opera", "Opera"),
    ("chamber-music-society", "Classical"),
    ("jazz-at-lincoln-center", "Jazz"),
    ("film-at-lincoln-center", "Film"),
    ("the-juilliard-school", "Classical"),
    ("juilliard", "Classical"),
    ("school-of-american-ballet", "Dance"),
    ("new-york-city-ballet", "Dance"),
    ("lincoln-center-theater", "Theater"),
]

# Genre class hints, second priority after org hits.
_GENRE_CLASS_HINTS: list[tuple[str, str]] = [
    ("classical-music", "Classical"),
    ("jazz", "Jazz"),
    ("popular-music", "Popular Music"),
    ("dance", "Dance"),
    ("theater", "Theater"),
    ("film", "Film"),
    ("comedy", "Comedy"),
    ("kids-and-family", "Family"),
    ("opera", "Opera"),
]

_CARD_RE = re.compile(
    r'<div class="cal-day-show ([^"]*?)">.*?' r"</div>\s*</div>\s*</div>",
    re.S,
)
_DAY_HEADER_RE = re.compile(r'<div class="calendar-day" data-date="([^"]+)"')
_HREF_RE = re.compile(r'<a href="([^"]+)"', re.S)
_NAME_RE = re.compile(r'<h2 class="show-name">(.*?)</h2>', re.S)
_ORG_RE = re.compile(r'<h2 class="show-org"><a [^>]*>(.*?)</a></h2>', re.S)
_TIME_RE = re.compile(r'<span class="show-time">([^<]*)</span>')
_PRICE_RE = re.compile(r'<span class="show-price">([^<]*)</span>')
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _strip(s: str) -> str:
    s = _TAG_STRIP_RE.sub("", s)
    return html_mod.unescape(s).replace("\xa0", " ").strip()


def _classify_venue(class_blob: str) -> str:
    for needle, label in _VENUE_CLASS_MAP:
        if needle in class_blob:
            return label
    return "Lincoln Center"


# Org-name fallback table — applied after class-blob hits when class is generic.
_ORG_NAME_FALLBACK: list[tuple[tuple[str, ...], str]] = [
    (("philharmonic", "chamber"), "Classical"),
    (("jazz",), "Jazz"),
    (("film",), "Film"),
    (("ballet", "dance"), "Dance"),
]


def _genre_from_class(class_blob: str) -> str:
    for needle, label in _ORG_GENRE_MAP:
        if needle in class_blob:
            return label
    for needle, label in _GENRE_CLASS_HINTS:
        if needle in class_blob:
            return label
    return ""


def _genre_from_org(org: str) -> str:
    org_lower = org.lower()
    for keywords, label in _ORG_NAME_FALLBACK:
        if any(k in org_lower for k in keywords):
            return label
    return ""


def _classify_genre(class_blob: str, org: str) -> str:
    return _genre_from_class(class_blob) or _genre_from_org(org) or "Concert"


def _parse_time(t: str) -> str:
    """'7:30 pm' / '12:45 pm' -> '19:30' / '12:45'. Empty for 'Multiple Times'."""
    if not t or "multiple" in t.lower() or "time" not in t.lower() and not re.search(r"\d", t):
        return ""
    raw = t.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return ""


def _human_date_to_iso(human: str) -> str:
    """'Friday, May 1, 2026' -> '2026-05-01'."""
    try:
        return datetime.strptime(human.strip(), "%A, %B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _extract_card_block(html: str, start: int) -> tuple[str, int]:
    """Return (card_html, end_index) by counting <div> open/close from cal-day-show start."""
    # Each card is bounded by <div class="cal-day-show-cont">...</div> at the same depth.
    # The simplest robust approach: split body on '<div class="cal-day-show-cont">'.
    return html, start  # unused fallback


def _iter_day_blocks(body: str) -> list[tuple[str, str]]:
    """Yield (iso_date, day_html) tuples by splitting on calendar-day markers."""
    # Capture every "calendar-day" div together with what follows, until the next.
    matches = list(_DAY_HEADER_RE.finditer(body))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        date_iso = _human_date_to_iso(m.group(1))
        if not date_iso:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((date_iso, body[m.end() : end]))
    return out


def _card_url(card_html: str) -> str:
    href_m = _HREF_RE.search(card_html)
    url = href_m.group(1) if href_m else "https://www.lincolncenter.org/calendar"
    if url.startswith("/"):
        return f"https://www.lincolncenter.org{url}"
    return url


def _card_class_blob(card_html: str) -> str:
    m = re.search(r'<div class="cal-day-show ([^"]+)"', card_html)
    return m.group(1) if m else ""


def _card_description(org: str, raw_time: str, time_str: str) -> str:
    desc = f"Presented by {org}" if org else ""
    if raw_time and not time_str:
        # e.g. "Multiple Times" — surface in description
        text = f"{desc} — {raw_time}"
        # Trim leading/trailing " — " separator if present
        if text.startswith(" — "):
            text = text[3:]
        if text.endswith(" — "):
            text = text[:-3]
        desc = text
    return desc


def _parse_card(card_html: str, date_iso: str, source_org: str = "") -> dict | None:
    name_m = _NAME_RE.search(card_html)
    if not name_m:
        return None
    name = _strip(name_m.group(1))

    org_m = _ORG_RE.search(card_html)
    org = _strip(org_m.group(1)) if org_m else source_org

    time_m = _TIME_RE.search(card_html)
    raw_time = _strip(time_m.group(1)) if time_m else ""
    time_str = _parse_time(raw_time)

    price_m = _PRICE_RE.search(card_html)
    price = _strip(price_m.group(1)) if price_m else ""

    class_blob = _card_class_blob(card_html)
    eid = f"{date_iso}_{re.sub(r'[^a-z0-9]+', '-', name.lower())[:60]}"

    return {
        "provider": "lincoln_center",
        "external_id": eid,
        "name": name,
        "date": date_iso,
        "time": time_str,
        "venue_name": _classify_venue(class_blob),
        "address": ADDRESS,
        "neighborhood": NEIGHBORHOOD,
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": _classify_genre(class_blob, org),
        "price": price,
        "url": _card_url(card_html),
        "image": "",
        "description": _card_description(org, raw_time, time_str),
    }


def _split_cards(day_html: str) -> list[str]:
    """Split a day's HTML into individual card blocks. Each card starts with
    ``<div class="cal-day-show-cont">`` and ends just before the next."""
    parts = re.split(r'(?=<div class="cal-day-show-cont">)', day_html)
    return [p for p in parts if 'class="cal-day-show "' in p or 'class="cal-day-show ' in p]


def _fetch_month(month_label: str) -> str:
    """Sync HTTP fetch for one ``<Month> <Year>`` label, returning the rendered HTML body."""
    with httpx.Client(timeout=30, headers=_HEADERS, follow_redirects=True) as client:
        resp = client.get(f"{AJAX_URL}/{month_label}")
        resp.raise_for_status()
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return ""
    return data.get("calRender", "") or ""


def _months_in_window(start_date: str, end_date: str) -> list[str]:
    """Generate the unique 'Month YYYY' labels covering [start_date, end_date]."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    out: list[str] = []
    cur = start.replace(day=1)
    while cur <= end:
        out.append(cur.strftime("%B %Y"))
        # Advance to first of next month.
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return out


def _events_from_body(body: str, start_date: str, end_date: str) -> list[dict]:
    out: list[dict] = []
    if not body:
        return out
    for date_iso, day_html in _iter_day_blocks(body):
        if date_iso < start_date or date_iso > end_date:
            continue
        for card in _split_cards(day_html):
            parsed = _parse_card(card, date_iso)
            if parsed:
                out.append(parsed)
    return out


def _dedupe_events(events: list[dict]) -> list[dict]:
    """Adjacent month payloads can repeat the same event_id."""
    seen: set[str] = set()
    out: list[dict] = []
    for e in events:
        if e["external_id"] in seen:
            continue
        seen.add(e["external_id"])
        out.append(e)
    return out


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Fetch Lincoln Center complex calendar events in [start_date, end_date]."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    months = _months_in_window(start_date, end_date)
    bodies = await asyncio.gather(*(asyncio.to_thread(_fetch_month, m) for m in months))

    events: list[dict] = []
    for body in bodies:
        events.extend(_events_from_body(body, start_date, end_date))
    return _dedupe_events(events)


async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
