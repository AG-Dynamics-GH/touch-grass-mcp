"""Google Calendar public iCal feed client — community events, run clubs, etc.

Parses public .ics feeds from Google Calendar. No API key needed.
Feed URL format: https://calendar.google.com/calendar/ical/{id}/public/basic.ics

Calendars are configured in config/social_agent_config.json under
"google_calendars" as a list of {name, url, category} objects.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "social_agent_config.json"

_ET = ZoneInfo("America/New_York")


def _load_calendars() -> list[dict]:
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        return config.get("google_calendars", [])
    return []


def _parse_dt(dt_val) -> datetime | None:
    """Convert icalendar date/datetime to timezone-aware datetime."""
    if dt_val is None:
        return None
    dt = dt_val.dt if hasattr(dt_val, "dt") else dt_val
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_ET)
        return dt
    # date without time — treat as midnight ET
    return datetime(dt.year, dt.month, dt.day, tzinfo=_ET)


async def fetch_calendar_events(
    *,
    calendar_url: str,
    calendar_name: str = "",
    category: str = "",
    start_date: str = "",
    end_date: str = "",
    size: int = 20,
) -> list[dict]:
    """Fetch events from a single public Google Calendar iCal feed."""
    if not start_date:
        start_date = datetime.now(_ET).strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=_ET)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=_ET
    )

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(calendar_url)
        resp.raise_for_status()
        ical_text = resp.text

    cal = Calendar.from_ical(ical_text)
    events = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        dtstart = _parse_dt(component.get("dtstart"))
        if dtstart is None:
            continue

        if dtstart < start_dt or dtstart > end_dt:
            continue

        events.append(_normalize(component, calendar_name, category))

    events.sort(key=lambda e: (e.get("date", ""), e.get("time", "")))
    return events[:size]


async def search_all_calendars(
    *,
    keyword: str = "",
    category: str = "",
    start_date: str = "",
    end_date: str = "",
    size: int = 20,
) -> list[dict]:
    """Search across all configured Google Calendar feeds."""
    calendars = _load_calendars()
    if not calendars:
        return []

    if category:
        calendars = [
            c for c in calendars if c.get("category", "") == category or not c.get("category")
        ]

    all_events: list[dict] = []
    for cal_config in calendars:
        try:
            events = await fetch_calendar_events(
                calendar_url=cal_config["url"],
                calendar_name=cal_config.get("name", ""),
                category=cal_config.get("category", category),
                start_date=start_date,
                end_date=end_date,
                size=size,
            )
            if keyword:
                kw_lower = keyword.lower()
                events = [
                    e
                    for e in events
                    if kw_lower in e.get("name", "").lower()
                    or kw_lower in e.get("description", "").lower()
                ]
            all_events.extend(events)
        except Exception:
            continue

    all_events.sort(key=lambda e: (e.get("date", ""), e.get("time", "")))
    return all_events[:size]


def _normalize(component, calendar_name: str, category: str) -> dict:
    dtstart = _parse_dt(component.get("dtstart"))
    dtend = _parse_dt(component.get("dtend"))

    date_str = dtstart.strftime("%Y-%m-%d") if dtstart else ""
    time_str = dtstart.strftime("%H:%M") if dtstart and dtstart.hour != 0 else ""

    summary = str(component.get("summary", ""))
    location = str(component.get("location", ""))
    description = str(component.get("description", ""))[:300]
    uid = str(component.get("uid", ""))

    duration = ""
    if dtstart and dtend:
        diff = dtend - dtstart
        hours = diff.seconds // 3600
        mins = (diff.seconds % 3600) // 60
        if hours > 0:
            duration = f"{hours}h" + (f"{mins}m" if mins else "")
        elif mins > 0:
            duration = f"{mins}m"

    return {
        "provider": "gcal",
        "id": uid,
        "name": summary,
        "date": date_str,
        "time": time_str,
        "genre": category,
        "price": "",
        "url": "",
        "image": "",
        "venue_name": location.split(",")[0] if location else "",
        "address": location if location else "",
        "city": "",
        "state": "",
        "group_name": calendar_name,
        "duration": duration,
        "description": description,
    }
