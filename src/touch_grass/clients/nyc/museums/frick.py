"""Frick Collection client — lectures, concerts, gallery talks via Trumba iCal feed.

Frick.org is Cloudflare-protected (returns HTTP 418 to plain httpx). Their public
events feed is hosted by Trumba at:

    https://www.trumba.com/calendars/frick.ics

Standards-compliant iCalendar — every program (concerts, lectures, gallery
talks, fashion week events, member events) is a VEVENT with TZID=America/New_York
DTSTART, location, and HTML description. We pull events, filter to the requested
window, and tag genre based on Trumba's "Event Type" custom field where present
or simple keyword heuristics on SUMMARY otherwise.

All Frick events take place at the Frick Collection at 1 East 70th Street
(Manhattan, Upper East Side / Lenox Hill).
"""

from __future__ import annotations

import os as _os
import re
from datetime import datetime, timedelta

from curl_cffi import requests as cf_requests


def _impersonate_kw():
    """Return {} or {"impersonate": "chrome"} based on TOUCH_GRASS_NYC_IMPERSONATE."""
    if _os.environ.get("TOUCH_GRASS_NYC_IMPERSONATE", "").lower() in ("true", "1", "yes"):
        return {"impersonate": "chrome"}
    return {}


ICS_URL = "https://www.trumba.com/calendars/frick.ics"
VENUE_NAME = "The Frick Collection"
VENUE_ADDRESS = "1 East 70th Street, New York, NY 10021"
VENUE_NEIGHBORHOOD = "Lenox Hill"

# Trumba "Event Type" → our genre labels.
_EVENT_TYPE_GENRE = {
    "lectures": "Lecture",
    "lecture": "Lecture",
    "concerts": "Concert",
    "concert": "Concert",
    "gallery talks": "Gallery Talk",
    "gallery talk": "Gallery Talk",
    "symposia": "Symposium",
    "symposium": "Symposium",
    "openings": "Opening",
    "opening": "Opening",
    "members": "Members Event",
    "member": "Members Event",
    "tours": "Tour",
    "tour": "Tour",
    "screenings": "Film",
    "film": "Film",
}

# Fall-back keywords on SUMMARY when Event Type is absent.
_SUMMARY_GENRE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bopen(?:ing)?\b", re.I), "Opening"),
    (re.compile(r"\bconcert\b", re.I), "Concert"),
    (re.compile(r"\blecture\b", re.I), "Lecture"),
    (re.compile(r"\bsymposium\b|\bsymposia\b", re.I), "Symposium"),
    (re.compile(r"\bgallery\s*talk\b", re.I), "Gallery Talk"),
    (re.compile(r"\btalk\b|\bconversation\b|\bdiscussion\b", re.I), "Talk"),
    (re.compile(r"\bfilm\b|\bscreening\b", re.I), "Film"),
    (re.compile(r"\btour\b", re.I), "Tour"),
]


def _ics_unfold(text: str) -> str:
    """RFC 5545: lines beginning with space/tab are continuations of the previous line."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_unescape(value: str) -> str:
    """Reverse iCal text-value escaping (\\, \\;, \\n, ,)."""
    return (
        value.replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
    )


def _strip_html(s: str) -> str:
    """Quick HTML strip with entity decoding for &#xx; numeric refs Trumba emits."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = (
        s.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", s).strip()


def _split_vevents(text: str) -> list[str]:
    return re.findall(r"BEGIN:VEVENT.*?END:VEVENT", text, re.DOTALL)


def _parse_dtstart(line: str) -> tuple[str, str]:
    """Return (YYYY-MM-DD, HH:MM) from a DTSTART line. Tolerant of TZID and DATE-only forms."""
    # e.g. "DTSTART;TZID=America/New_York:20260429T150000" or "DTSTART:20260429"
    val = line.split(":", 1)[1].strip() if ":" in line else ""
    if not val:
        return "", ""
    if "T" in val:
        d, t = val.split("T", 1)
        try:
            dt_obj = datetime.strptime(d, "%Y%m%d")
            t_clean = t.rstrip("Z")[:6]  # HHMMSS
            tt = datetime.strptime(t_clean, "%H%M%S") if len(t_clean) == 6 else None
            return dt_obj.strftime("%Y-%m-%d"), tt.strftime("%H:%M") if tt else ""
        except ValueError:
            return val[:10], ""
    # date-only
    try:
        return datetime.strptime(val, "%Y%m%d").strftime("%Y-%m-%d"), ""
    except ValueError:
        return val[:10], ""


def _extract_property(block: str, name: str) -> str:
    """Return the raw value (after the first colon) of property `name` (no params)."""
    pattern = re.compile(rf"^{re.escape(name)}(?:;[^:\r\n]*)?:(.*)$", re.MULTILINE)
    m = pattern.search(block)
    return m.group(1).rstrip("\r") if m else ""


def _extract_event_type(block: str) -> str:
    """Pull the Trumba 'Event Type' custom field if present."""
    m = re.search(
        r'X-TRUMBA-CUSTOMFIELD;NAME="Event Type"[^:]*:([^\r\n]+)',
        block,
    )
    return m.group(1).strip() if m else ""


def _classify_genre(event_type: str, summary: str) -> str:
    et = event_type.strip().lower()
    if et:
        for key, label in _EVENT_TYPE_GENRE.items():
            if key in et:
                return label
    for pat, label in _SUMMARY_GENRE_RULES:
        if pat.search(summary):
            return label
    return "Talk"


def _parse_vevent(block: str) -> dict | None:
    summary_raw = _extract_property(block, "SUMMARY")
    if not summary_raw:
        return None
    summary = _strip_html(_ics_unescape(summary_raw))
    date_str, time_str = _parse_dtstart(
        next((ln for ln in block.splitlines() if ln.startswith("DTSTART")), "")
    )
    if not date_str:
        return None
    end_block = next((ln for ln in block.splitlines() if ln.startswith("DTEND")), "")
    end_date, end_time = _parse_dtstart(end_block) if end_block else ("", "")
    _ics_unescape(_extract_property(block, "LOCATION"))
    description = _strip_html(_ics_unescape(_extract_property(block, "DESCRIPTION")))
    uid = _extract_property(block, "UID") or _extract_property(block, "URL")
    url = _extract_property(block, "URL")
    if not url:
        # Trumba descriptions sometimes embed the canonical event URL.
        m = re.search(r"https://www\.frick\.org/[^\"'\s<]+", description)
        url = m.group(0) if m else "https://www.frick.org/calendar"
    event_type = _extract_event_type(block)
    genre = _classify_genre(event_type, summary)

    return {
        "provider": "frick",
        "external_id": uid or f"{date_str}-{summary[:60]}",
        "name": summary,
        "date": date_str,
        "time": time_str,
        "end_date": end_date,
        "end_time": end_time,
        "venue_name": VENUE_NAME,
        "address": VENUE_ADDRESS,
        "neighborhood": VENUE_NEIGHBORHOOD,
        "borough": "Manhattan",
        "city": "New York",
        "state": "NY",
        "genre": genre,
        "price": "",  # Trumba "Join Us" custom field has pricing but is free-text
        "url": url,
        "image": "",
        "description": description[:1200],
    }


def _filter_window(events: list[dict], start_date: str, end_date: str) -> list[dict]:
    if not start_date and not end_date:
        return events
    return [
        e
        for e in events
        if (not start_date or e["date"] >= start_date) and (not end_date or e["date"] <= end_date)
    ]


def _fetch_ics_text() -> str:
    """Synchronous fetch (curl_cffi has no native async)."""
    r = cf_requests.get(ICS_URL, **_impersonate_kw(), timeout=20)
    r.raise_for_status()
    return r.text


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Fetch Frick events; defaults to the next 14 days."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )

    import asyncio

    text = await asyncio.to_thread(_fetch_ics_text)
    text = _ics_unfold(text)
    blocks = _split_vevents(text)
    events: list[dict] = []
    for blk in blocks:
        e = _parse_vevent(blk)
        if e:
            events.append(e)
    return _filter_window(events, start_date, end_date)


# Convenience alias for ingest harness.
async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
