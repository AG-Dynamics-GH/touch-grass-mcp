"""Yelp Fusion API client — restaurant and business discovery.

Free tier: 500 API calls/day, no credit card required.
Sign up: https://www.yelp.com/developers
"""

from __future__ import annotations

import os

import httpx

BASE_URL = "https://api.yelp.com/v3"

CATEGORY_MAP = {
    "restaurants": "restaurants",
    "food_drink": "restaurants,bars,cafes",
    "bars": "bars",
    "cafes": "coffee,cafes",
    "dessert": "desserts,bakeries,icecream",
    "brunch": "breakfast_brunch",
    "italian": "italian",
    "japanese": "japanese,sushi",
    "mexican": "mexican",
    "indian": "indpak",
    "thai": "thai",
    "chinese": "chinese",
    "korean": "korean",
    "mediterranean": "mediterranean",
    "vegan": "vegan,vegetarian",
}


def _api_key() -> str:
    key = os.environ.get("YELP_API_KEY", "")
    if not key:
        raise RuntimeError("YELP_API_KEY not set — get one at https://www.yelp.com/developers")
    return key


async def search_businesses(
    *,
    term: str = "",
    city: str = "New York",
    category: str = "restaurants",
    price: str = "",
    sort_by: str = "best_match",
    radius_meters: int = 8000,
    size: int = 20,
) -> list[dict]:
    """Search Yelp businesses (restaurants, bars, cafes, etc.).

    price: "1" (cheap) to "4" (expensive), or "1,2" for range
    sort_by: best_match, rating, review_count, distance
    """
    params: dict = {
        "location": city,
        "limit": min(size, 50),
        "sort_by": sort_by,
        "radius": min(radius_meters, 40000),
    }
    if term:
        params["term"] = term

    yelp_cat = CATEGORY_MAP.get(category, category)
    if yelp_cat:
        params["categories"] = yelp_cat

    if price:
        params["price"] = price

    headers = {"Authorization": f"Bearer {_api_key()}"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/businesses/search", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    businesses = data.get("businesses", [])[:size]
    return [_normalize(b) for b in businesses]


async def get_business_details(business_id: str) -> dict:
    """Get detailed info for a single business."""
    headers = {"Authorization": f"Bearer {_api_key()}"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/businesses/{business_id}", headers=headers)
        resp.raise_for_status()
        return _normalize_detail(resp.json())


async def get_reviews(business_id: str) -> list[dict]:
    """Get up to 3 reviews for a business (free tier limit)."""
    headers = {"Authorization": f"Bearer {_api_key()}"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/businesses/{business_id}/reviews", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "text": r.get("text", ""),
            "rating": r.get("rating", 0),
            "user": r.get("user", {}).get("name", ""),
            "time_created": r.get("time_created", ""),
        }
        for r in data.get("reviews", [])
    ]


def _normalize(biz: dict) -> dict:
    location = biz.get("location", {})
    categories = biz.get("categories", [])
    cat_names = ", ".join(c.get("title", "") for c in categories[:3])

    price_str = biz.get("price", "")

    return {
        "provider": "yelp",
        "id": biz.get("id", ""),
        "name": biz.get("name", ""),
        "rating": biz.get("rating", 0),
        "review_count": biz.get("review_count", 0),
        "price": price_str,
        "categories": cat_names,
        "phone": biz.get("display_phone", ""),
        "url": biz.get("url", ""),
        "image": biz.get("image_url", ""),
        "address": ", ".join(location.get("display_address", [])),
        "city": location.get("city", ""),
        "state": location.get("state", ""),
        "venue_name": biz.get("name", ""),
        "is_closed": biz.get("is_closed", False),
        "distance_meters": round(biz.get("distance", 0)),
    }


def _normalize_detail(biz: dict) -> dict:
    base = _normalize(biz)
    base["hours"] = _format_hours(biz.get("hours", []))
    base["photos"] = biz.get("photos", [])[:5]
    base["transactions"] = biz.get("transactions", [])
    return base


def _format_hours(hours_data: list) -> str:
    if not hours_data:
        return ""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    opens = hours_data[0].get("open", [])
    parts = []
    for entry in opens:
        day = days[entry.get("day", 0)]
        start = entry.get("start", "")
        end = entry.get("end", "")
        if start and end:
            parts.append(f"{day}: {start[:2]}:{start[2:]}-{end[:2]}:{end[2:]}")
    return "; ".join(parts)
