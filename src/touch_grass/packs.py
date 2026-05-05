"""City-pack registry — declarative grouping of city-specific clients + pulse defaults.

A pack activates when the user's profile city matches one of its aliases.
Core clients always run regardless of pack. Adding a new city = register a CityPack
with its clients and pulse defaults.

This is a thin abstraction in v0.1; the server's existing dispatch still uses the
NYC pack directly. v0.2 will route all city-conditional logic through this registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PulseDefaults:
    reddit_subs: list[str] = field(default_factory=list)
    rss_feeds: list[str] = field(default_factory=list)
    trends_geo: str | None = None


@dataclass
class CityPack:
    name: str
    aliases: tuple[str, ...]
    state: str
    client_modules: list[str]  # dotted paths e.g. "touch_grass.clients.nyc.museums.met_talks"
    pulse_defaults: PulseDefaults


PACKS: dict[str, CityPack] = {
    "nyc": CityPack(
        name="nyc",
        aliases=("new york", "nyc", "brooklyn", "manhattan", "queens", "bronx"),
        state="NY",
        client_modules=[
            "touch_grass.clients.nyc.bpl",
            "touch_grass.clients.nyc.chelsea_galleries",
            "touch_grass.clients.nyc.gcal_public",
            "touch_grass.clients.nyc.nyc_audubon",
            "touch_grass.clients.nyc.nyc_opendata",
            "touch_grass.clients.nyc.nypl",
            "touch_grass.clients.nyc.qpl",
            "touch_grass.clients.nyc.the_skint",
            "touch_grass.clients.nyc.museums.carnegie_hall",
            "touch_grass.clients.nyc.museums.frick",
            "touch_grass.clients.nyc.museums.met_talks",
            "touch_grass.clients.nyc.museums.moma_talks",
            "touch_grass.clients.nyc.museums.momaps1",
            "touch_grass.clients.nyc.museums.museums",
            "touch_grass.clients.nyc.museums.ninety_second_y",
            "touch_grass.clients.nyc.museums.park_avenue_armory",
            "touch_grass.clients.nyc.museums.whitney_talks",
            "touch_grass.clients.nyc.venues.brooklyn_steel",
            "touch_grass.clients.nyc.venues.lincoln_center",
            "touch_grass.clients.nyc.venues.metrograph",
            "touch_grass.clients.nyc.venues.village_jazz",
            "touch_grass.clients.nyc.venues.village_vanguard",
        ],
        pulse_defaults=PulseDefaults(
            reddit_subs=["nyc", "AskNYC", "FoodNYC", "Brooklyn", "manhattan"],
            rss_feeds=[
                "https://ny.eater.com/rss/index.xml",
                "https://gothamist.com/feed",
                "https://www.timeout.com/newyork/feed.rss",
            ],
            trends_geo="US-NY-501",
        ),
    ),
    # Starter packs — pulse defaults only, no city-specific scrapers yet.
    # Core keyless aggregators (Ticketmaster, Eventbrite, Yelp, Resident Advisor,
    # Dice, OpenBrewery, NWS, editorial) work everywhere. PRs welcome to upgrade
    # any of these to a deep-coverage pack with local museum/library/venue scrapers.
    "sf": CityPack(
        name="sf",
        aliases=("san francisco", "sf", "san fran", "bay area"),
        state="CA",
        client_modules=[],
        pulse_defaults=PulseDefaults(
            reddit_subs=["sanfrancisco", "AskSF", "bayarea", "FoodSF"],
            rss_feeds=[
                "https://sf.eater.com/rss/index.xml",
                "https://www.timeout.com/san-francisco/feed.rss",
            ],
            trends_geo="US-CA-807",
        ),
    ),
    "la": CityPack(
        name="la",
        aliases=("los angeles", "la", "l.a.", "lax"),
        state="CA",
        client_modules=[],
        pulse_defaults=PulseDefaults(
            reddit_subs=["LosAngeles", "AskLosAngeles", "FoodLosAngeles"],
            rss_feeds=[
                "https://la.eater.com/rss/index.xml",
                "https://www.timeout.com/los-angeles/feed.rss",
            ],
            trends_geo="US-CA-803",
        ),
    ),
    "chicago": CityPack(
        name="chicago",
        aliases=("chicago", "chi", "chitown"),
        state="IL",
        client_modules=[],
        pulse_defaults=PulseDefaults(
            reddit_subs=["chicago", "AskChicago", "chicagofood"],
            rss_feeds=[
                "https://chicago.eater.com/rss/index.xml",
                "https://www.timeout.com/chicago/feed.rss",
            ],
            trends_geo="US-IL-602",
        ),
    ),
    "austin": CityPack(
        name="austin",
        aliases=("austin", "atx"),
        state="TX",
        client_modules=[],
        pulse_defaults=PulseDefaults(
            reddit_subs=["Austin", "AskAnAustinian", "austinfood"],
            rss_feeds=[
                "https://austin.eater.com/rss/index.xml",
            ],
            trends_geo="US-TX-635",
        ),
    ),
    "boston": CityPack(
        name="boston",
        aliases=("boston", "bos", "cambridge"),
        state="MA",
        client_modules=[],
        pulse_defaults=PulseDefaults(
            reddit_subs=["boston", "AskBoston", "bostonfood"],
            rss_feeds=[
                "https://boston.eater.com/rss/index.xml",
            ],
            trends_geo="US-MA-506",
        ),
    ),
    "seattle": CityPack(
        name="seattle",
        aliases=("seattle", "sea"),
        state="WA",
        client_modules=[],
        pulse_defaults=PulseDefaults(
            reddit_subs=["Seattle", "AskSeattle", "SeattleFood"],
            rss_feeds=[
                "https://seattle.eater.com/rss/index.xml",
            ],
            trends_geo="US-WA-819",
        ),
    ),
    "dc": CityPack(
        name="dc",
        aliases=("washington dc", "dc", "washington", "d.c."),
        state="DC",
        client_modules=[],
        pulse_defaults=PulseDefaults(
            reddit_subs=["washingtondc", "AskDC", "dcfood"],
            rss_feeds=[
                "https://dc.eater.com/rss/index.xml",
            ],
            trends_geo="US-DC-511",
        ),
    ),
}


def resolve_pack(city: str) -> CityPack | None:
    """Return the city pack matching this city name, or None if no pack registered."""
    if not city:
        return None
    city_lc = city.lower().strip()
    for pack in PACKS.values():
        if city_lc in pack.aliases:
            return pack
    return None


def all_pack_names() -> list[str]:
    return list(PACKS.keys())
