"""92Y client — Lexington Ave + 92nd, Upper East Side.

92ny.org is fronted by Imperva/Incapsula bot protection that returns a JS
challenge page (the Incapsula iframe redirect) to plain ``httpx`` *and* to
``curl_cffi`` chrome impersonation. The challenge can only be cleared by a
real JS-executing browser session, which is out of scope for the events
ingest pipeline (no headless browser dependency).

What IS reachable without solving the challenge:

* ``GET https://www.92ny.org/sitemap.xml`` returns 1.1 MB of structured XML
  with every public URL on the site, *including* ~117 ``/event/<slug>``
  detail pages. The sitemap has no ``<lastmod>`` and no event date metadata,
  so we cannot filter to a date window — every URL is just a slug.

Given the date window cannot be honored, this client returns an empty list
and logs a single info-level message, so the orchestrator records the
attempt without crashing. The interface matches the other clients so that
once a date-bearing feed surfaces (sitemap-news, an officially exposed JSON
API, or a curated ICS), only this file changes.

Possible upgrades (each non-trivial):

1. Render the challenge page with Playwright/Puppeteer once per session,
   cache the ``visid_incap_*`` and ``incap_ses_*`` cookies, replay them with
   ``curl_cffi`` chrome impersonation. ~1-2 hours of work.
2. Use the public 92Y RSS-on-Eventbrite feed if/when they migrate ticketing
   off the in-house Tessitura wrapper.
3. Pay for an Incapsula-aware scraping proxy (ScrapingBee, Bright Data).

For now, classical/jazz programming at 92Y can usually be cross-found through
Carnegie Hall partnerships or through hand-curated entries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("events.ninety_second_y")

VENUE_NAME = "92Y"
VENUE_ADDRESS = "1395 Lexington Avenue, New York, NY 10128"
NEIGHBORHOOD = "Upper East Side"

_BLOCKED_NOTICE = (
    "92Y (92ny.org) is behind Imperva/Incapsula bot protection — "
    "returning empty result. Upgrade path requires a JS-rendering scraper."
)


async def fetch_events(start_date: str = "", end_date: str = "") -> list[dict]:
    """Stub — see module docstring. Returns []."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=14)).strftime(
            "%Y-%m-%d"
        )
    logger.info(_BLOCKED_NOTICE)
    return []


async def search_events(start_date: str = "", end_date: str = "", **_: object) -> list[dict]:
    return await fetch_events(start_date, end_date)
