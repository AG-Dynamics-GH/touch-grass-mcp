"""
Event Discovery MCP Server
===========================
Unified event/venue search across Ticketmaster, Eventbrite, NYC Open Data,
NYPL, Google Calendar community feeds, Yelp Fusion, Open Brewery DB,
and NWS weather forecasts.
Run: .venv/bin/python -m tools.mcp.events_mcp.server
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from touch_grass.clients.core import (
    editorial,
    eventbrite,
    nws_weather,
    openbrewery,
    resident_advisor,
    ticketmaster,
    todaytix,
    yelp,
)
from touch_grass.clients.nyc import gcal_public, nyc_opendata, nypl
from touch_grass.clients.nyc.museums import museums
from touch_grass.config import get_data_dir, load_profile_dict

# Runtime configuration (after all imports per E402)
load_dotenv()  # find .env in CWD or env vars
logger = logging.getLogger("events-discovery")
logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

# CONFIG_PATH no longer hardcoded — load_profile_dict() resolves via XDG
# VENUE_CACHE_PATH removed — Resy integration cut from public release


# ---------------------------------------------------------------------------
# Profile-to-queries mapping — vetted translations from profile interests
# to actual event-search keywords. Avoids the "lexical mismatch" trap where
# raw profile terms (e.g. "rooftop", "AI") return junky lexical matches.
# ---------------------------------------------------------------------------


_PROFILE_QUERY_MAP = {
    "jazz": ["jazz"],
    "live music": ["concert", "live music"],
    "indie": ["indie"],
    "electronic": ["electronic", "techno", "house"],
    "running": ["run club", "5k"],
    "yoga": ["yoga"],
    "art galleries": ["gallery", "exhibition", "opening"],
    "creative events": ["workshop", "studio"],
    "rooftop bars": ["rooftop"],
    "diverse cuisines": ["chef's table", "tasting menu"],
    "craft cocktails": ["cocktail"],
    "upscale dining": ["chef's table", "supper club"],
    "wine bars": ["wine bar"],
    "ai": ["AI workshop", "machine learning"],
    "tech": ["tech meetup", "demo day"],
    "startups": ["startup", "founder"],
    "intimate": [],
    "chill": [],
    "creative": ["workshop"],
    "outdoor": ["outdoor"],
    "rooftop": ["rooftop"],
}


# Canonical keyword sweep — minimum floor for any "what's happening" question.
# Profile-specific keywords are added via _profile_to_queries.
_CANONICAL_KEYWORDS = [
    "jazz",
    "classical",
    "opera",
    "recital",
    "indie",
    "electronic",
    "concert",
    "chamber",
    "choir",
    "theater",
    "matinee",
    "film",
    "screening",
    "festival",
    "premiere",
    "reading",
    "talk",
    "yoga",
    "run club",
    "pilates",
    "meditation",
    "hike",
    "rooftop",
    "market",
    "brunch",
    "tasting",
    "omakase",
    "chef",
    "pop-up",
    "supper club",
    "gallery",
    "opening",
    "exhibition",
    "art fair",
    "book",
    "AI",
    "tech meetup",
    "demo",
    "lecture",
    "comedy",
    "stand-up",
]


def _profile_to_queries(profile: dict) -> list[str]:
    """Map profile interests to vetted search keywords (deduped, ordered)."""
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        ql = q.lower()
        if ql not in seen:
            seen.add(ql)
            queries.append(q)

    interests = profile.get("interests", {})
    for category in interests.values():
        if isinstance(category, list):
            for term in category:
                term_l = term.lower()
                if term_l in _PROFILE_QUERY_MAP:
                    for q in _PROFILE_QUERY_MAP[term_l]:
                        _add(q)
    return queries


# ---------------------------------------------------------------------------
# User profile helpers
# ---------------------------------------------------------------------------


def _load_user_profile() -> dict:
    """Load user profile via XDG-resolved config."""
    config = load_profile_dict()
    return config.get("user_profile", {})


def _profile_keywords(profile: dict) -> set[str]:
    """Extract all positive-signal keywords from the user profile (lowercased)."""
    keywords: set[str] = set()
    interests = profile.get("interests", {})
    for category in interests.values():
        if isinstance(category, list):
            keywords.update(k.lower() for k in category)
    for v in profile.get("vibe_preferences", []):
        keywords.add(v.lower())
    for n in profile.get("neighborhoods", {}).get("favorites", []):
        keywords.add(n.lower())
    for lp in profile.get("learned_preferences", []):
        if lp.get("signal") in ("liked", "loved") and lp.get("keywords"):
            keywords.update(k.lower() for k in lp["keywords"])
    return keywords


def _profile_anti_keywords(profile: dict) -> set[str]:
    """Extract negative-signal keywords from dislikes + learned dislikes."""
    anti: set[str] = set()
    dislikes = profile.get("dislikes", {})
    for category in dislikes.values():
        if isinstance(category, list):
            anti.update(k.lower() for k in category)
    for n in profile.get("neighborhoods", {}).get("avoid", []):
        anti.add(n.lower())
    for lp in profile.get("learned_preferences", []):
        if lp.get("signal") in ("disliked", "not_interested") and lp.get("keywords"):
            anti.update(k.lower() for k in lp["keywords"])
    return anti


def _score_event(
    event: dict, pos: set[str], neg: set[str], profile: dict
) -> tuple[float, list[str]]:
    """Score an event 0.0-1.0 plus reason fragments explaining the score."""
    searchable = " ".join(
        str(event.get(f, "")).lower()
        for f in ("name", "genre", "venue_name", "city", "group_name", "categories")
    )
    reasons: list[str] = []

    pos_hits = sorted({k for k in pos if k in searchable})
    neg_hits = sorted({k for k in neg if k in searchable})
    score = 0.5 + (len(pos_hits) * 0.12) - (len(neg_hits) * 0.2)
    if pos_hits:
        reasons.append("+matches: " + ", ".join(pos_hits[:4]))
    if neg_hits:
        reasons.append("-conflicts: " + ", ".join(neg_hits[:4]))

    group_name = str(event.get("group_name", "")).strip()
    if group_name:
        group_lower = group_name.lower()
        if any(g.lower() == group_lower for g in profile.get("preferred_groups", [])):
            score += 0.3
            reasons.append(f"+preferred group: {group_name}")
        elif any(g.lower() == group_lower for g in profile.get("avoid_groups", [])):
            score -= 0.3
            reasons.append(f"-avoid group: {group_name}")

    crowd = profile.get("crowd_signals", {})
    crowd_text = f"{group_name} {event.get('name', '')}".lower()
    crowd_pos = sorted({k for k in crowd.get("positive", []) if k.lower() in crowd_text})
    crowd_neg = sorted({k for k in crowd.get("negative", []) if k.lower() in crowd_text})
    score += len(crowd_pos) * 0.1
    score -= len(crowd_neg) * 0.15
    if crowd_pos:
        reasons.append("+crowd: " + ", ".join(crowd_pos[:3]))
    if crowd_neg:
        reasons.append("-crowd: " + ", ".join(crowd_neg[:3]))

    return max(0.0, min(1.0, score)), reasons


def _confidence_band(score: float) -> str:
    """Map score magnitude to a confidence label.

    Score far from 0.5 neutral → high; score near a tag threshold → borderline.
    """
    if score >= 0.85 or score <= 0.15:
        return "high"
    if score >= 0.78 or score <= 0.22 or 0.62 <= score <= 0.68 or 0.27 <= score <= 0.33:
        return "borderline"
    return "high"


def _rank_events(events: list[dict]) -> list[dict]:
    """Sort events by profile relevance (descending), then by date."""
    profile = _load_user_profile()
    pos = _profile_keywords(profile)
    neg = _profile_anti_keywords(profile)
    has_groups = profile.get("preferred_groups") or profile.get("avoid_groups")
    has_crowd = profile.get("crowd_signals", {}).get("positive") or profile.get(
        "crowd_signals", {}
    ).get("negative")
    if not pos and not neg and not has_groups and not has_crowd:
        return events
    for e in events:
        score, reasons = _score_event(e, pos, neg, profile)
        e["_relevance"] = score
        e["_relevance_reasons"] = reasons
    events.sort(key=lambda e: (-e.get("_relevance", 0.5), e.get("date", ""), e.get("time", "")))
    return events


mcp = FastMCP(
    "events-discovery",
    instructions=(
        "Event discovery for NYC and other cities. Search concerts, local events, "
        "meetups, run clubs, rooftop parties, and more across multiple providers. "
        "Also discover restaurants, bars, breweries, and NYPL library events. "
        "Includes NWS weather forecasts for outdoor planning. Community calendars "
        "pull from 19 curated Google Calendar/Meetup iCal feeds. "
        "Tip: after finding events, use Google Calendar MCP to add them to your calendar.\n\n"
        "DEFAULT BEHAVIOR — DEEPEST fan out across sources: When the user asks an open-ended "
        "question ('what's happening', 'interesting events', 'things to do', 'recommend'), "
        "OR a bare/terse prompt ('what's good?', 'recs?', 'anything happening?', 'got anything?', "
        "'tonight?', 'this weekend?'), call MULTIPLE tools IN PARALLEL rather than picking one "
        "category. The user expects the DEEPEST possible sweep by default — do not stop at a "
        "shallow single-pass.\n\n"
        "BARE PROMPTS — DO NOT ASK FOR CLARIFICATION FIRST: If the prompt has no explicit time "
        "or location, default to today + the configured city + the loaded user_profile, then "
        "run the comprehensive sweep below. Offer to refine AFTER returning results — never "
        "respond with only a clarifying question on a bare event query.\n\n"
        "A complete sweep MUST include ALL of the following in parallel:\n"
        "  • search_concerts + search_ra_events (music — explicit date range)\n"
        "  • discover_niche_events across ALL categories (arts, social, fitness, food_drink, "
        "outdoor, tech — one call per category)\n"
        "  • search_community_calendars with NO query (pull everything) AND additional "
        "targeted keyword passes (e.g. 'run', 'brunch', 'yoga', 'book', 'film', 'jazz') — "
        "keyword-specific events (like 'run + brunch') often only surface via targeted query\n"
        "  • search_events with broad keywords (jazz, comedy, rooftop, film, market, pop-up)\n"
        "  • get_editorial_picks (and get_editorial_feed per source if picks look thin)\n"
        "  • trending_events, weekend_weather\n"
        "  • search_broadway_shows, get_museum_exhibitions (retry per-museum if 'all' returns empty)\n"
        "  • search_breweries\n"
        "  • For hyperlocal depth: multiple discover_niche_events passes with `neighborhood` "
        "set (Brooklyn, Williamsburg, East Village, LES)\n\n"
        "Only narrow to a single category when the user explicitly specifies one ('just "
        "concerts', 'only run clubs'). Filter/curate AFTER fetching, not before — pull broad, "
        "then select. If you miss an event the user knew about, you queried too shallow.\n\n"
        "HORIZON-AWARE FAN-OUT — adjust the sweep based on time horizon, since 'tonight' and "
        "'next month' need different priorities:\n"
        "  • TONIGHT / NEXT FEW HOURS — prioritize walk-in friendly: search_resy_restaurants for "
        "the date, search_ra_events tonight only, "
        "search_concerts tonight only, weekend_weather (rain pushes to indoor), search_breweries / "
        "Yelp cocktail bars near the user's location. SKIP weekly-recurring community calendar items "
        "with start times already in the past today, SKIP ticketed events that require advance "
        "purchase windows. Tag walk-in / no-rez bars explicitly.\n"
        "  • THIS WEEKEND (Fri-Sun) — fan out wide: Editorial picks for 'best of weekend' angle.\n"
        "  • NEXT 1–2 WEEKS — scarcity-driven: prioritize get_editorial_picks (Frieze, gallery "
        "openings, restaurant openings), trending_events, ticketed concerts that may sell out, "
        "Broadway shows with limited windows. \n"
        "  • THIS MONTH / FUTURE PLANNING — editorial and exhibitions lead: get_museum_exhibitions, "
        "get_editorial_picks (especially Hyperallergic / Artforum for arts cycles), trending events, "
        "Broadway runs. Skip walk-in / live-tonight tools — wrong horizon.\n\n"
        "PRICING — always surface cost when the response contains it: Ticketmaster price ranges, "
        "Eventbrite ticket prices, TodayTix min/max, RA cost, Yelp $ tier. "
        "Say 'Free' when explicitly free, and simply omit pricing when the provider didn't supply it — "
        "don't guess or infer. Users pick events partly on price, so never drop this field when it's present."
    ),
)


def _load_config() -> dict:
    """Load full config via XDG-resolved path."""
    return load_profile_dict()


def _default_city() -> str:
    return _load_config().get("location", {}).get("city", "New York")


def _default_state() -> str:
    return _load_config().get("location", {}).get("state", "NY")


def _default_radius() -> int:
    return _load_config().get("location", {}).get("radius_miles", 25)


def _weekend_range() -> tuple[str, str]:
    today = datetime.now()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0 and today.hour >= 18:
        days_until_friday = 7
    friday = today + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)
    return friday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


async def _gather_with_fallback(*coros) -> list[dict]:
    """Run multiple provider queries, skip failures gracefully."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    events = []
    for r in results:
        if isinstance(r, list):
            events.extend(r)
        elif isinstance(r, Exception):
            logger.warning("Provider failed: %s: %s", type(r).__name__, r)
    return events


def _dedup(events: list[dict]) -> list[dict]:
    """Remove near-duplicate events by name+date+venue similarity."""
    seen: set[str] = set()
    unique = []
    for e in events:
        key = f"{e.get('name', '').lower().strip()[:40]}|{e.get('date', '')}|{e.get('venue_name', '').lower().strip()[:20]}"
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def _format_events(events: list[dict], limit: int = 20) -> str:
    if not events:
        return "No events found matching your criteria."

    events = _rank_events(events)
    lines = []
    for i, e in enumerate(events[:limit], 1):
        relevance = e.pop("_relevance", None)
        reasons = e.pop("_relevance_reasons", []) or []
        tag = ""
        if relevance is not None:
            confidence = _confidence_band(relevance)
            if relevance >= 0.8:
                tag = f" [Great match — {confidence}]"
            elif relevance >= 0.65:
                tag = f" [Good match — {confidence}]"
            elif relevance <= 0.25:
                tag = f" [Not your vibe — {confidence}]"
        parts = [f"**{i}. {e['name']}**{tag}"]
        if tag and reasons:
            parts.append(f"   _why: {'; '.join(reasons[:3])}_")
        if e.get("date"):
            dt = e["date"]
            if e.get("time"):
                dt += f" at {e['time']}"
            parts.append(f"   Date: {dt}")
        if e.get("venue_name"):
            venue = e["venue_name"]
            if e.get("city"):
                venue += f", {e['city']}"
            parts.append(f"   Venue: {venue}")
        if e.get("price"):
            parts.append(f"   Price: {e['price']}")
        if e.get("genre"):
            parts.append(f"   Genre: {e['genre']}")
        if e.get("group_name"):
            parts.append(f"   Group: {e['group_name']}")
        if e.get("duration"):
            parts.append(f"   Duration: {e['duration']}")
        if e.get("attending"):
            parts.append(f"   Attending: {e['attending']}")
        if e.get("url"):
            parts.append(f"   Link: {e['url']}")
        parts.append(f"   Provider: {e.get('provider', 'unknown')}")
        lines.append("\n".join(parts))

    return "\n\n".join(lines)


def _format_venues(venues: list[dict], limit: int = 15) -> str:
    if not venues:
        return "No venues found matching your criteria."

    lines = []
    for i, v in enumerate(venues[:limit], 1):
        parts = [f"**{i}. {v['name']}**"]
        if v.get("rating"):
            stars = f"{v['rating']}/5"
            if v.get("review_count"):
                stars += f" ({v['review_count']} reviews)"
            parts.append(f"   Rating: {stars}")
        if v.get("categories"):
            parts.append(f"   Type: {v['categories']}")
        if v.get("brewery_type"):
            parts.append(f"   Type: {v['brewery_type']}")
        if v.get("price"):
            parts.append(f"   Price: {v['price']}")
        if v.get("address"):
            addr = v["address"]
            if v.get("city"):
                addr += f", {v['city']}"
            parts.append(f"   Address: {addr}")
        if v.get("phone"):
            parts.append(f"   Phone: {v['phone']}")
        if v.get("hours"):
            parts.append(f"   Hours: {v['hours']}")
        if v.get("url"):
            parts.append(f"   Link: {v['url']}")
        parts.append(f"   Provider: {v.get('provider', 'unknown')}")
        lines.append("\n".join(parts))

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Event search tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_events(
    query: str = "",
    city: str = "",
    category: str = "",
    start_date: str = "",
    end_date: str = "",
    include_weather: bool = False,
    limit: int = 20,
) -> str:
    """Search for events across Ticketmaster, Eventbrite, NYC Open Data, NYPL, and community calendars.

    Args:
        query: Search keywords (e.g. "jazz", "run club", "comedy", "rooftop")
        city: City name (default: from config, usually "New York")
        category: Filter by category: concerts, sports, theater, comedy, food_drink, fitness, social, arts, tech, outdoor
        start_date: Start date YYYY-MM-DD (default: today)
        end_date: End date YYYY-MM-DD (default: 7 days from start)
        include_weather: Prepend weather forecast for the date range (default False)
        limit: Max results to return (default 20)
    """
    city = city or _default_city()
    state = _default_state() if city.lower() in ("new york", "nyc", "brooklyn", "manhattan") else ""
    is_nyc = city.lower() in ("new york", "nyc", "brooklyn", "manhattan")

    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

    coros = [
        ticketmaster.search_events(
            keyword=query,
            city=city,
            state_code=state,
            category=category,
            start_date=start_date,
            end_date=end_date,
            radius=_default_radius(),
            size=limit,
        ),
        eventbrite.search_events(
            keyword=query,
            city=city,
            category=category,
            start_date=start_date,
            end_date=end_date,
            size=limit,
        ),
        gcal_public.search_all_calendars(
            keyword=query,
            category=category,
            start_date=start_date,
            end_date=end_date,
            size=limit,
        ),
    ]

    if is_nyc:
        borough = ""
        if city.lower() == "brooklyn":
            borough = "Brooklyn"
        elif city.lower() == "manhattan":
            borough = "Manhattan"
        coros.append(
            nyc_opendata.search_events(
                keyword=query,
                start_date=start_date,
                end_date=end_date,
                borough=borough,
                size=limit,
            )
        )
        coros.append(
            nyc_opendata.search_parks_events(
                keyword=query,
                start_date=start_date,
                end_date=end_date,
                size=limit // 2,
            )
        )
        coros.append(
            nypl.search_events(
                keyword=query,
                start_date=start_date,
                end_date=end_date,
                borough=borough,
                size=limit // 2,
            )
        )

    events = await _gather_with_fallback(*coros)
    events = _dedup(events)
    result = _format_events(events, limit)

    if include_weather and is_nyc:
        try:
            forecast = await nws_weather.get_nyc_forecast(days=3)
            result = nws_weather.format_forecast(forecast) + "\n\n---\n\n" + result
        except Exception:
            pass

    return result


@mcp.tool()
async def search_concerts(
    artist: str = "",
    city: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 20,
) -> str:
    """Search for concerts and live music events. Optionally filter by artist.

    Args:
        artist: Artist or band name (optional — if omitted, searches all concerts)
        city: City name (default: from config)
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        limit: Max results
    """
    city = city or _default_city()
    state = _default_state() if city.lower() in ("new york", "nyc", "brooklyn", "manhattan") else ""

    events = await _gather_with_fallback(
        ticketmaster.search_events(
            keyword=artist,
            city=city,
            state_code=state,
            category="concerts",
            start_date=start_date,
            end_date=end_date,
            radius=_default_radius(),
            size=limit,
        ),
        eventbrite.search_events(
            keyword=artist,
            city=city,
            category="concerts",
            start_date=start_date,
            end_date=end_date,
            size=limit,
        ),
    )
    events = _dedup(events)

    if city:
        city_lower = city.lower()
        events = [e for e in events if not e.get("city") or city_lower in e.get("city", "").lower()]

    return _format_events(events, limit)


@mcp.tool()
async def discover_niche_events(
    category: str = "social",
    neighborhood: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 15,
) -> str:
    """Discover niche/hyperlocal events — run clubs, rooftop parties, pop-ups, social meetups, street fairs.

    Searches Eventbrite, NYC Open Data, and community Google Calendar feeds.

    Args:
        category: fitness, social, food_drink, arts, tech, outdoor
        neighborhood: Specific area (e.g. "Brooklyn", "East Village", "Williamsburg")
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        limit: Max results
    """
    city = neighborhood or _default_city()

    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

    borough = ""
    if neighborhood and neighborhood.lower() in (
        "brooklyn",
        "manhattan",
        "queens",
        "bronx",
        "staten island",
    ):
        borough = neighborhood.title()

    coros = [
        eventbrite.search_events(
            keyword="",
            city=city,
            category=category,
            start_date=start_date,
            end_date=end_date,
            size=limit,
        ),
        nyc_opendata.search_events(
            keyword=category,
            start_date=start_date,
            end_date=end_date,
            borough=borough,
            size=limit,
        ),
        nyc_opendata.search_parks_events(
            keyword=category if category in ("fitness", "outdoor", "arts") else "",
            start_date=start_date,
            end_date=end_date,
            size=limit // 2,
        ),
        gcal_public.search_all_calendars(
            keyword=category,
            category=category,
            start_date=start_date,
            end_date=end_date,
            size=limit,
        ),
        nypl.search_events(
            keyword=category,
            start_date=start_date,
            end_date=end_date,
            size=limit // 2,
        ),
    ]

    events = await _gather_with_fallback(*coros)
    events = _dedup(events)
    return _format_events(events, limit)


@mcp.tool()
async def trending_events(
    city: str = "",
    include_weather: bool = True,
    limit: int = 10,
) -> str:
    """Get trending/popular events this weekend in your city. Includes weather forecast by default.

    Args:
        city: City name (default: from config)
        include_weather: Prepend weekend weather forecast (default True)
        limit: Max results
    """
    city = city or _default_city()
    start, end = _weekend_range()
    is_nyc = city.lower() in ("new york", "nyc", "brooklyn", "manhattan")

    coros = [
        ticketmaster.search_events(
            city=city,
            state_code=_default_state(),
            start_date=start,
            end_date=end,
            size=limit,
        ),
        eventbrite.search_events(
            city=city,
            start_date=start,
            end_date=end,
            size=limit,
        ),
        gcal_public.search_all_calendars(
            start_date=start,
            end_date=end,
            size=limit,
        ),
    ]

    if is_nyc:
        coros.append(
            nyc_opendata.search_events(
                start_date=start,
                end_date=end,
                size=limit,
            )
        )
        coros.append(
            nypl.search_events(
                start_date=start,
                end_date=end,
                size=limit // 2,
            )
        )

    events = await _gather_with_fallback(*coros)
    events = _dedup(events)
    result = _format_events(events, limit)

    if include_weather and is_nyc:
        try:
            forecast = await nws_weather.get_nyc_forecast(days=3)
            result = nws_weather.format_forecast(forecast) + "\n\n---\n\n" + result
        except Exception:
            pass

    return result


@mcp.tool()
async def get_event_details(
    event_id: str,
    provider: str = "ticketmaster",
) -> str:
    """Get full details for a specific event by ID.

    Args:
        event_id: The event ID from a previous search result
        provider: Which provider the event is from (ticketmaster, eventbrite)
    """
    if provider == "ticketmaster":
        event = await ticketmaster.get_event_details(event_id)
        return _format_events([event], 1)

    return f"Detail lookup not yet supported for provider '{provider}'. Use the event URL from the search result."


# ---------------------------------------------------------------------------
# Weather tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def weekend_weather() -> str:
    """Get the NYC weather forecast for the next few days to help plan outdoor vs indoor events.

    Returns temperature, conditions, precipitation chance, and an outdoor-friendly flag for each period.
    """
    forecast = await nws_weather.get_nyc_forecast(days=4)
    return nws_weather.format_forecast(forecast)


# ---------------------------------------------------------------------------
# Community calendar tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_community_calendars(
    query: str = "",
    category: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 15,
) -> str:
    """Search events from curated community Google Calendar feeds (run clubs, social groups, etc.).

    Calendars are configured in config/social_agent_config.json under "google_calendars".
    Each calendar has a name, iCal URL, and category. No API key needed.

    Args:
        query: Search keywords to filter events
        category: Filter by calendar category (e.g. "fitness", "social", "arts")
        start_date: Start date YYYY-MM-DD (default: today)
        end_date: End date YYYY-MM-DD (default: 7 days from start)
        limit: Max results (default 15)
    """
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

    events = await gcal_public.search_all_calendars(
        keyword=query,
        category=category,
        start_date=start_date,
        end_date=end_date,
        size=limit,
    )

    if not events:
        config = _load_config()
        cals = config.get("google_calendars", [])
        if not cals:
            return (
                "No community calendars configured. Add Google Calendar iCal URLs to "
                'config/social_agent_config.json under "google_calendars" as:\n'
                '[{"name": "My Run Club", "url": "https://calendar.google.com/calendar/ical/.../basic.ics", "category": "fitness"}]'
            )

    return _format_events(events, limit)


# ---------------------------------------------------------------------------
# Restaurant & venue discovery tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_restaurants(
    cuisine: str = "",
    city: str = "",
    neighborhood: str = "",
    price: str = "",
    sort_by: str = "best_match",
    limit: int = 15,
) -> str:
    """Search for restaurants, bars, and cafes via Yelp Fusion.

    Args:
        cuisine: Cuisine type or keyword (e.g. "sushi", "italian", "brunch", "rooftop bar", "date night")
        city: City name (default: from config)
        neighborhood: Specific area to search (e.g. "East Village", "Williamsburg", "SoHo")
        price: Price filter — "1" ($), "2" ($$), "3" ($$$), "4" ($$$$), or "1,2" for range
        sort_by: Sort order — best_match, rating, review_count, distance
        limit: Max results (default 15)
    """
    location = neighborhood or city or _default_city()
    category = "restaurants"

    if cuisine and cuisine.lower() in ("bar", "bars", "cocktail", "cocktails", "drinks"):
        category = "bars"
    elif cuisine and cuisine.lower() in ("cafe", "coffee", "cafes"):
        category = "cafes"
    elif cuisine and cuisine.lower() in ("brunch",):
        category = "brunch"
    elif cuisine and cuisine.lower() in ("dessert", "ice cream", "bakery"):
        category = "dessert"

    term = (
        cuisine
        if cuisine and cuisine.lower() not in ("bar", "bars", "cafe", "coffee", "brunch", "dessert")
        else ""
    )

    venues = await _gather_with_fallback(
        yelp.search_businesses(
            term=term,
            city=location,
            category=category,
            price=price,
            sort_by=sort_by,
            size=limit,
        ),
    )
    return _format_venues(venues, limit)


@mcp.tool()
async def search_breweries(
    city: str = "",
    brewery_type: str = "",
    limit: int = 15,
) -> str:
    """Search for breweries and taprooms via Open Brewery DB (free, no API key needed).

    Args:
        city: City name (default: from config)
        brewery_type: Filter by type — micro, nano, regional, brewpub, large, bar, contract, proprietor
        limit: Max results (default 15)
    """
    city = city or _default_city()
    state = "New York" if city.lower() in ("new york", "nyc", "brooklyn", "manhattan") else ""

    venues = await _gather_with_fallback(
        openbrewery.search_breweries(
            city=city,
            state=state,
            brewery_type=brewery_type,
            size=limit,
        ),
    )
    return _format_venues(venues, limit)


@mcp.tool()
async def get_restaurant_details(
    business_id: str,
) -> str:
    """Get detailed info and reviews for a restaurant/bar from Yelp.

    Args:
        business_id: The Yelp business ID from a search result
    """
    detail_coro = yelp.get_business_details(business_id)
    reviews_coro = yelp.get_reviews(business_id)

    results = await asyncio.gather(detail_coro, reviews_coro, return_exceptions=True)

    detail = results[0] if isinstance(results[0], dict) else {}
    reviews = results[1] if isinstance(results[1], list) else []

    if not detail:
        return "Could not fetch restaurant details. Check the business ID."

    parts = [f"**{detail.get('name', 'Unknown')}**"]
    if detail.get("rating"):
        parts.append(f"Rating: {detail['rating']}/5 ({detail.get('review_count', 0)} reviews)")
    if detail.get("categories"):
        parts.append(f"Cuisine: {detail['categories']}")
    if detail.get("price"):
        parts.append(f"Price: {detail['price']}")
    if detail.get("address"):
        parts.append(f"Address: {detail['address']}")
    if detail.get("phone"):
        parts.append(f"Phone: {detail['phone']}")
    if detail.get("hours"):
        parts.append(f"Hours: {detail['hours']}")
    if detail.get("transactions"):
        parts.append(f"Services: {', '.join(detail['transactions'])}")
    if detail.get("url"):
        parts.append(f"Yelp: {detail['url']}")

    if reviews:
        parts.append("\n**Recent Reviews:**")
        for r in reviews:
            stars = "*" * r.get("rating", 0)
            parts.append(f"  [{stars}] {r.get('text', '')[:200]}")
            parts.append(f"  — {r.get('user', 'Anonymous')}, {r.get('time_created', '')[:10]}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Resy reservation tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_recommendation_keywords() -> str:
    """Return the canonical keyword sweep + profile-driven keyword expansion.

    Use at the start of any open-ended recommendation request ('what's happening',
    'recommend events', 'this weekend'). Returns the full keyword list to drive
    parallel search_events calls. Floor + profile expansion ensures we don't miss
    yoga/classical/gallery/run-club categories the user actually likes.
    """
    profile = _load_user_profile()
    profile_qs = _profile_to_queries(profile)

    canonical = list(_CANONICAL_KEYWORDS)
    extra = [q for q in profile_qs if q.lower() not in {c.lower() for c in canonical}]

    parts = [
        "**Recommendation keyword sweep**\n",
        f"Canonical floor ({len(canonical)} keywords) — run all per day:",
        "  " + ", ".join(canonical),
    ]
    if extra:
        parts.append(f"\nProfile-driven additions ({len(extra)}) — based on user interests:")
        parts.append("  " + ", ".join(extra))

    parts.append(
        "\n**Usage:** For multi-day requests, run a SEPARATE parallel sweep PER DAY. "
        "Pass each keyword to `search_events` with the single-day range. Day-specific "
        "events (Sunday yoga, weekly jams, matinees) get drowned out across multi-day windows."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# User profile tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_user_profile() -> str:
    """Get the user's event/dining preference profile.

    Returns interests, dislikes, vibe preferences, favorite neighborhoods,
    schedule preferences, and learned preferences from past feedback.
    Use this before searching to tailor queries to the user's taste.
    """
    profile = _load_user_profile()
    if not profile or not profile.get("name"):
        return (
            "No user profile configured yet. The profile lives in "
            'config/social_agent_config.json under "user_profile". '
            "Ask the user about their interests and use update_user_preferences to populate it."
        )

    sections = [f"**Profile: {profile.get('name', 'User')}**"]

    interests = profile.get("interests", {})
    for cat, items in interests.items():
        if items:
            label = cat.replace("_", " ").title()
            sections.append(f"  Likes ({label}): {', '.join(items)}")

    dislikes = profile.get("dislikes", {})
    for cat, items in dislikes.items():
        if items:
            label = cat.replace("_", " ").title()
            sections.append(f"  Dislikes ({label}): {', '.join(items)}")

    if profile.get("vibe_preferences"):
        sections.append(f"  Vibe: {', '.join(profile['vibe_preferences'])}")

    neighborhoods = profile.get("neighborhoods", {})
    if neighborhoods.get("favorites"):
        sections.append(f"  Favorite neighborhoods: {', '.join(neighborhoods['favorites'])}")
    if neighborhoods.get("avoid"):
        sections.append(f"  Avoids neighborhoods: {', '.join(neighborhoods['avoid'])}")

    schedule = profile.get("schedule", {})
    if schedule.get("preferred_days"):
        sections.append(f"  Preferred days: {', '.join(schedule['preferred_days'])}")
    if schedule.get("preferred_times"):
        sections.append(f"  Preferred times: {', '.join(schedule['preferred_times'])}")

    social = profile.get("social_context", {})
    if social:
        parts = []
        if social.get("typical_group_size"):
            parts.append(f"group size ~{social['typical_group_size']}")
        if social.get("open_to_solo"):
            parts.append("open to solo")
        if social.get("open_to_group_events"):
            parts.append("open to group events")
        if parts:
            sections.append(f"  Social: {', '.join(parts)}")

    learned = profile.get("learned_preferences", [])
    if learned:
        recent = learned[-5:]
        sections.append(f"  Learned ({len(learned)} total, showing last {len(recent)}):")
        for lp in recent:
            signal = lp.get("signal", "?")
            note = lp.get("note", "")
            kw = ", ".join(lp.get("keywords", []))
            sections.append(f"    {signal}: {note}" + (f" [{kw}]" if kw else ""))

    return "\n".join(sections)


@mcp.tool()
async def update_user_preferences(
    field: str,
    action: str = "add",
    values: str = "",
    signal: str = "",
    note: str = "",
    keywords: str = "",
) -> str:
    """Update the user's preference profile based on conversation feedback.

    For structured updates (interests, dislikes, vibes, neighborhoods):
        field: The profile field to update. One of:
            interests.music_genres, interests.activities, interests.food_and_drink,
            interests.sports, interests.topics,
            dislikes.music_genres, dislikes.activities, dislikes.food_and_drink,
            vibe_preferences,
            neighborhoods.favorites, neighborhoods.avoid,
            schedule.preferred_days, schedule.preferred_times,
            name
        action: "add" to append values, "remove" to delete values, "set" to replace entirely
        values: Comma-separated values (e.g. "jazz, indie rock, electronic")

    For conversational learning (recording event feedback):
        field: "learned"
        signal: One of: liked, loved, disliked, not_interested
        note: Short description (e.g. "loved the jazz at Blue Note")
        keywords: Comma-separated tags for future matching (e.g. "jazz, live music, intimate venue")
    """
    config = _load_config()
    profile = config.get("user_profile", {})

    if field == "learned":
        if not signal:
            return "Error: 'signal' is required for learned preferences (liked, loved, disliked, not_interested)"
        entry = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "signal": signal,
            "note": note,
            "keywords": [k.strip() for k in keywords.split(",") if k.strip()] if keywords else [],
        }
        profile.setdefault("learned_preferences", []).append(entry)
        config["user_profile"] = profile
        from touch_grass.config import save_profile_dict

        save_profile_dict(config)
        return f"Recorded: {signal} — {note}"

    if field == "name":
        profile["name"] = values.strip()
        config["user_profile"] = profile
        from touch_grass.config import save_profile_dict

        save_profile_dict(config)
        return f"Name set to: {values.strip()}"

    parts = field.split(".")
    target = profile
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    leaf = parts[-1]

    current = target.get(leaf, [])
    if not isinstance(current, list):
        return f"Error: field '{field}' is not a list — use 'name' field for scalar values"

    new_values = [v.strip() for v in values.split(",") if v.strip()]

    if action == "add":
        existing_lower = {v.lower() for v in current}
        for v in new_values:
            if v.lower() not in existing_lower:
                current.append(v)
                existing_lower.add(v.lower())
        target[leaf] = current
    elif action == "remove":
        remove_lower = {v.lower() for v in new_values}
        target[leaf] = [v for v in current if v.lower() not in remove_lower]
    elif action == "set":
        target[leaf] = new_values
    else:
        return f"Error: unknown action '{action}' — use add, remove, or set"

    config["user_profile"] = profile
    from touch_grass.config import save_profile_dict

    save_profile_dict(config)
    result_list = target[leaf]
    return f"Updated {field}: {', '.join(result_list)}"


# ---------------------------------------------------------------------------
# Coveted restaurant watchlist tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Editorial RSS — Eater, Gothamist, Time Out NY, Hyperallergic, Artforum
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_editorial_picks(
    category: str = "",
    limit: int = 25,
) -> str:
    """Get recent articles from NYC editorial sources (Eater, Gothamist, Time Out, Hyperallergic, Artforum).

    Useful for "what's new" curated picks rather than "what's available tonight."

    Args:
        category: Optional filter — food, news, arts, events. Empty = all.
        limit: Max items across all feeds (default 25)
    """
    try:
        items = await editorial.fetch_all(category=category, limit_per_feed=8)
    except Exception as e:
        return f"Editorial fetch failed: {e}"

    if not items:
        return "No editorial items found."

    items = items[:limit]
    label = f" — {category}" if category else ""
    parts = [f"**Editorial picks{label}** ({len(items)} items)\n"]
    for item in items:
        line = f"- **{item.get('title', 'Untitled')}**"
        if item.get("date"):
            line += f"  ({item['date']})"
        line += f"  *{item.get('source', '')}*"
        if item.get("summary"):
            line += f"\n  {item['summary']}"
        if item.get("url"):
            line += f"\n  {item['url']}"
        parts.append(line)

    return "\n".join(parts)


@mcp.tool()
async def get_editorial_feed(
    feed_id: str,
    limit: int = 15,
) -> str:
    """Get a single editorial feed by ID.

    Args:
        feed_id: One of: eater_ny, gothamist, timeout_ny, hyperallergic, artforum
        limit: Max items (default 15)
    """
    try:
        items = await editorial.fetch_feed(feed_id, limit)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Feed fetch failed: {e}"

    if not items:
        return f"No items found in feed '{feed_id}'."

    parts = [f"**{items[0].get('source', feed_id)}** ({len(items)} items)\n"]
    for item in items:
        line = f"- **{item.get('title', 'Untitled')}**"
        if item.get("date"):
            line += f"  ({item['date']})"
        if item.get("summary"):
            line += f"\n  {item['summary']}"
        if item.get("url"):
            line += f"\n  {item['url']}"
        parts.append(line)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# TodayTix — Broadway and off-Broadway tickets
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_broadway_shows(
    city: str = "new york",
    limit: int = 30,
) -> str:
    """Browse Broadway and off-Broadway shows on TodayTix with real-time pricing.

    Includes rush ticket and lottery availability flags.

    Args:
        city: City (default: new york). Also: london, los angeles, washington dc
        limit: Max shows (default 30)
    """
    try:
        shows = await todaytix.list_shows(city, limit)
    except Exception as e:
        return f"TodayTix search failed: {e}"

    if not shows:
        return f"No shows found on TodayTix for {city}."

    parts = [f"**TodayTix — {city.title()}** ({len(shows)} shows)\n"]
    for s in shows:
        line = f"- **{s.get('name', 'Untitled')}**"
        if s.get("category"):
            line += f"  [{s['category']}]"
        if s.get("venue_name"):
            line += f" @ {s['venue_name']}"
        if s.get("min_price") is not None:
            price = f"${s['min_price']}"
            if s.get("max_price") and s["max_price"] != s["min_price"]:
                price += f"-${s['max_price']}"
            line += f"  {price}"
        flags = []
        if s.get("is_rush"):
            flags.append("RUSH")
        if s.get("is_lottery"):
            flags.append("LOTTERY")
        if flags:
            line += f"  [{', '.join(flags)}]"
        if s.get("summary"):
            line += f"\n  {s['summary']}"
        line += f"  `id: {s.get('id')}`"
        parts.append(line)

    parts.append("\nUse `get_broadway_showtimes` with a show ID for performance times.")
    return "\n".join(parts)


@mcp.tool()
async def get_broadway_showtimes(show_id: int) -> str:
    """Get available performances/showtimes for a TodayTix show.

    Args:
        show_id: TodayTix show ID (from search_broadway_shows results)
    """
    try:
        times = await todaytix.get_showtimes(show_id)
    except Exception as e:
        return f"Showtimes fetch failed: {e}"

    if not times:
        return f"No showtimes available for show {show_id}."

    parts = [f"**Showtimes for show {show_id}** ({len(times)} performances)\n"]
    for t in times:
        line = f"- {t.get('datetime', '?')}"
        if t.get("min_price") is not None:
            price = f"${t['min_price']}"
            if t.get("max_price") and t["max_price"] != t["min_price"]:
                price += f"-${t['max_price']}"
            line += f"  {price}"
        if not t.get("available", True):
            line += "  [SOLD OUT]"
        parts.append(line)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Museums — Met + MoMA
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_museum_exhibitions(
    museum: str = "all",
    limit: int = 10,
) -> str:
    """Get current and upcoming exhibitions at NYC museums.

    Args:
        museum: One of: met, moma, whitney, cooperhewitt, newmuseum, all (default: all)
        limit: Max exhibitions per museum (default 10)
    """
    try:
        if museum == "met":
            items = await museums.get_met_exhibitions(limit)
        elif museum == "moma":
            items = await museums.get_moma_exhibitions(limit)
        elif museum == "whitney":
            items = await museums.get_whitney_exhibitions(limit)
        elif museum == "cooperhewitt":
            items = await museums.get_cooperhewitt_exhibitions(limit)
        elif museum == "newmuseum":
            items = await museums.get_newmuseum_exhibitions(limit)
        else:
            items = await museums.get_all_exhibitions(limit_per_museum=limit)
    except Exception as e:
        return f"Museum exhibitions fetch failed: {e}"

    if not items:
        return f"No exhibitions found for '{museum}'."

    parts = [f"**Museum exhibitions** ({len(items)} found)\n"]
    for e in items:
        line = f"- **{e.get('title', 'Untitled')}**"
        line += f"  *{e.get('venue', '')}*"
        if e.get("dates"):
            line += f"\n  {e['dates']}"
        if e.get("url"):
            line += f"\n  {e['url']}"
        parts.append(line)

    return "\n".join(parts)


@mcp.tool()
async def search_met_collection(
    query: str,
    limit: int = 10,
) -> str:
    """Search the Metropolitan Museum's collection by keyword.

    Useful for "what works of [artist] are at the Met" or "find Egyptian sculpture."
    Includes gallery numbers for items currently on view.

    Args:
        query: Search term (artist name, medium, period, etc.)
        limit: Max results (default 10)
    """
    try:
        objs = await museums.search_met_collection(query, has_images=True, limit=limit)
    except Exception as e:
        return f"Met collection search failed: {e}"

    if not objs:
        return f"No Met collection results for '{query}'."

    parts = [f"**Met Collection — '{query}'** ({len(objs)} results)\n"]
    for o in objs:
        line = f"- **{o.get('title', 'Untitled')}**"
        if o.get("artist"):
            line += f"  by {o['artist']}"
        if o.get("date"):
            line += f"  ({o['date']})"
        if o.get("medium"):
            line += f"\n  {o['medium']}"
        if o.get("department"):
            line += f"  [{o['department']}]"
        if o.get("is_on_view") and o.get("gallery"):
            line += f"  *On view: Gallery {o['gallery']}*"
        if o.get("url"):
            line += f"\n  {o['url']}"
        parts.append(line)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Resident Advisor — electronic music events and clubs
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_ra_events(
    city: str = "new york",
    start_date: str = "",
    end_date: str = "",
    limit: int = 20,
) -> str:
    """Search electronic music events on Resident Advisor.

    Best coverage globally for warehouse parties, club nights, DJ sets,
    and underground electronic events. Captures venues like Nowadays,
    Public Records, Mansions, Basement, House of Yes, Bossa Nova that
    Ticketmaster typically misses.

    Args:
        city: City name (default: new york). Supported: NYC, LA, SF, Chicago,
            Berlin, London, Paris, Amsterdam, Tokyo
        start_date: ISO date YYYY-MM-DD (default: today)
        end_date: ISO date YYYY-MM-DD (default: 14 days from today)
        limit: Max results (default 20)
    """
    try:
        events = await resident_advisor.search_events(city, start_date, end_date, limit)
    except Exception as e:
        return f"Resident Advisor search failed: {e}"

    if not events:
        return f"No RA events found for {city} between {start_date or 'today'} and {end_date or '+14d'}."

    parts = [f"**Resident Advisor — {city.title()}** ({len(events)} events)\n"]
    for e in events:
        line = f"- **{e.get('name', 'Untitled')}**"
        if e.get("date"):
            line += f"  {e['date']}"
        if e.get("time"):
            line += f" {e['time']}"
        if e.get("venue_name"):
            line += f"  @ {e['venue_name']}"
        if e.get("artists"):
            artists = ", ".join(e["artists"][:5])
            line += f"\n  Lineup: {artists}"
            if len(e["artists"]) > 5:
                line += f" (+{len(e['artists']) - 5} more)"
        if e.get("cost"):
            line += f"\n  Cost: {e['cost']}"
        if e.get("url"):
            line += f"\n  {e['url']}"
        line += f"  `id: {e.get('id')}`"
        parts.append(line)

    parts.append(
        "\nUse `get_ra_event_details` with an event ID for description, cost, and full lineup."
    )
    return "\n".join(parts)


@mcp.tool()
async def get_ra_event_details(event_id: str) -> str:
    """Get full details for a Resident Advisor event (description, cost, lineup, genres).

    Args:
        event_id: RA event ID (from search_ra_events results)
    """
    try:
        e = await resident_advisor.get_event_details(event_id)
    except Exception as e:
        return f"RA event details failed: {e}"

    if not e:
        return f"Event {event_id} not found."

    parts = [f"**{e.get('name', 'Untitled')}**"]
    if e.get("date"):
        line = f"{e['date']}"
        if e.get("start_time"):
            line += f" — {e['start_time'][11:16]}"
        if e.get("end_time"):
            line += f" to {e['end_time'][11:16]}"
        parts.append(line)
    if e.get("venue_name"):
        venue_line = f"Venue: {e['venue_name']}"
        if e.get("venue_address"):
            venue_line += f", {e['venue_address']}"
        parts.append(venue_line)
    if e.get("city"):
        parts.append(f"City: {e['city']}" + (f", {e['country']}" if e.get("country") else ""))
    if e.get("genres"):
        parts.append(f"Genres: {', '.join(e['genres'])}")
    if e.get("artists"):
        parts.append(f"Lineup: {', '.join(e['artists'])}")
    if e.get("promoters"):
        parts.append(f"Promoters: {', '.join(e['promoters'])}")
    if e.get("cost"):
        parts.append(f"Cost: {e['cost']}")
    if e.get("min_age") is not None:
        parts.append(f"Min age: {e['min_age']}")
    if e.get("description"):
        desc = e["description"]
        if len(desc) > 500:
            desc = desc[:497] + "..."
        parts.append(f"\n{desc}")
    if e.get("url"):
        parts.append(f"\n{e['url']}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Flag calibration loop — log when system flag disagrees with user action
# ---------------------------------------------------------------------------


CALIBRATION_LOG_PATH = get_data_dir() / "events_index" / "flag_calibration.jsonl"


def _append_calibration(entry: dict) -> None:
    CALIBRATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CALIBRATION_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


@mcp.tool()
async def log_flag_feedback(
    event_name: str,
    system_flag: str,
    user_action: str,
    event_date: str = "",
    note: str = "",
) -> str:
    """Log a disagreement between the system's profile flag and what the user actually did.

    Call this whenever the user accepts a "[Not your vibe]" event or skips a "[Great match]" —
    the disagreement is the training signal for flag-quality auditing.

    Args:
        event_name: Event name (free-form, used for matching against past flags)
        system_flag: One of "Great match", "Good match", "Not your vibe", "(no flag)"
        user_action: One of "accepted", "skipped", "ignored", "saved_for_later", "rejected_strongly"
        event_date: Optional YYYY-MM-DD
        note: Optional one-line explanation (e.g. "rooftop matched but it's a tourist promoter")
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event_name": event_name.strip(),
        "event_date": event_date.strip(),
        "system_flag": system_flag.strip(),
        "user_action": user_action.strip(),
        "note": note.strip(),
    }
    try:
        _append_calibration(entry)
    except Exception as e:
        return f"Failed to log feedback: {e}"
    return f"Logged: '{event_name[:60]}' — system said {system_flag!r}, user {user_action!r}"


@mcp.tool()
async def get_calibration_stats(limit: int = 50) -> str:
    """Summarize flag-vs-action disagreements logged via log_flag_feedback.

    Reports false positives (Great match → skipped), false negatives
    (Not your vibe → accepted), and the raw recent log.

    Args:
        limit: Max recent entries to display (default 50)
    """
    if not CALIBRATION_LOG_PATH.exists():
        return "No calibration data yet. Use log_flag_feedback when a flag disagrees with what you actually wanted."

    entries: list[dict] = []
    with CALIBRATION_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not entries:
        return "Calibration log exists but is empty."

    total = len(entries)
    false_pos = [
        e
        for e in entries
        if "match" in e.get("system_flag", "").lower()
        and e.get("user_action") in ("skipped", "ignored", "rejected_strongly")
    ]
    false_neg = [
        e
        for e in entries
        if "not your vibe" in e.get("system_flag", "").lower()
        and e.get("user_action") in ("accepted", "saved_for_later")
    ]
    fp_rate = (len(false_pos) / total * 100) if total else 0
    fn_rate = (len(false_neg) / total * 100) if total else 0

    lines = [
        "**Flag calibration summary**",
        f"  Total feedback entries: {total}",
        f"  False positives (match → skipped): {len(false_pos)} ({fp_rate:.0f}%)",
        f"  False negatives (not vibe → accepted): {len(false_neg)} ({fn_rate:.0f}%)",
        "",
    ]

    if false_pos:
        lines.append("**Recent false positives** (system was too generous):")
        for e in false_pos[-5:]:
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(f"  - [{e.get('system_flag', '?')}] {e.get('event_name', '?')[:80]}{note}")
        lines.append("")

    if false_neg:
        lines.append("**Recent false negatives** (system was too strict):")
        for e in false_neg[-5:]:
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(f"  - [{e.get('system_flag', '?')}] {e.get('event_name', '?')[:80]}{note}")
        lines.append("")

    lines.append(f"**Last {min(limit, total)} entries**:")
    for e in entries[-limit:]:
        note = f" — {e['note']}" if e.get("note") else ""
        lines.append(
            f"  {e.get('ts', '?')[:16]}  [{e.get('system_flag', '?')}] → {e.get('user_action', '?')}: {e.get('event_name', '?')[:60]}{note}"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
