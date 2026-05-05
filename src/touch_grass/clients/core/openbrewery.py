"""Open Brewery DB client — free brewery discovery, no API key needed.

Fully open API: https://api.openbrewerydb.org
No auth, no rate limit registration. Community-maintained dataset.
"""

from __future__ import annotations

import httpx

BASE_URL = "https://api.openbrewerydb.org/v1/breweries"

BREWERY_TYPE_LABELS = {
    "micro": "Microbrewery",
    "nano": "Nano Brewery",
    "regional": "Regional Brewery",
    "brewpub": "Brewpub",
    "large": "Large Brewery",
    "planning": "Planning",
    "bar": "Bar",
    "contract": "Contract Brewery",
    "proprietor": "Proprietor Brewery",
    "closed": "Closed",
}


async def search_breweries(
    *,
    city: str = "New York",
    state: str = "New York",
    brewery_type: str = "",
    size: int = 20,
) -> list[dict]:
    """Search breweries by city/state."""
    params: dict = {
        "by_city": city.replace(" ", "_"),
        "per_page": min(size, 50),
    }
    if state:
        params["by_state"] = state.replace(" ", "_")
    if brewery_type:
        params["by_type"] = brewery_type

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(BASE_URL, params=params)
        resp.raise_for_status()
        breweries = resp.json()

    return [_normalize(b) for b in breweries if b.get("brewery_type") != "closed"]


async def get_brewery(brewery_id: str) -> dict:
    """Get a single brewery by ID."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/{brewery_id}")
        resp.raise_for_status()
        return _normalize(resp.json())


def _normalize(brewery: dict) -> dict:
    btype = brewery.get("brewery_type", "")
    type_label = BREWERY_TYPE_LABELS.get(btype, btype.title())

    address_parts = [
        p for p in [brewery.get("street"), brewery.get("address_2"), brewery.get("address_3")] if p
    ]
    address = ", ".join(address_parts)

    return {
        "provider": "openbrewery",
        "id": brewery.get("id", ""),
        "name": brewery.get("name", ""),
        "brewery_type": type_label,
        "address": address,
        "city": brewery.get("city", ""),
        "state": brewery.get("state", ""),
        "postal_code": brewery.get("postal_code", ""),
        "phone": brewery.get("phone", ""),
        "url": brewery.get("website_url", ""),
        "venue_name": brewery.get("name", ""),
        "latitude": brewery.get("latitude"),
        "longitude": brewery.get("longitude"),
    }
