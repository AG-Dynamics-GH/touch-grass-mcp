# Configuration reference

The profile config lives at `~/.config/touch-grass/config.json` (XDG-aware).

To create one: run `touch-grass init`. To switch profiles: set `TOUCH_GRASS_PROFILE=date_night` to load `~/.config/touch-grass/profiles/date_night.json` instead.

## Schema

```json
{
  "location": {
    "city": "New York",      // required for many tools; activates city pack if matched
    "state": "NY",           // 2-letter US code
    "zip": "10001",          // optional
    "radius_miles": 25       // search radius for local results
  },
  "user_profile": {
    "name": "",
    "interests": {
      "music_genres": ["jazz", "indie rock", "electronic"],   // example values — replace
      "activities": ["running", "yoga", "art galleries"],
      "food_and_drink": ["wine bars", "ramen", "rooftops"],
      "topics": ["AI", "design"]
    },
    "dislikes": {
      "music_genres": [],
      "activities": ["bus tours"],
      "food_and_drink": []
    },
    "vibe_preferences": ["chill", "intimate", "creative"],
    "neighborhoods": {
      "favorites": ["Greenpoint", "Williamsburg"],
      "avoid": []
    },
    "schedule": {
      "preferred_days": ["friday", "saturday", "sunday"],
      "preferred_times": ["evening", "afternoon"],
      "budget": "no_limit",                                    // or "low" / "medium" / "high"
      "avoid_early_morning": true
    },
    "social_context": {
      "typical_group_size": 2,
      "open_to_solo": true,
      "open_to_group_events": true
    },
    "bucket_list": ["see a show at Carnegie Hall"],
    "preferred_groups": ["My Neighborhood Run Club"],          // exact group names from Meetup etc.
    "avoid_groups": []
  },
  "pulse": {
    "enabled": true,
    "reddit_subs": ["nyc", "AskNYC"],                          // pulled in for trend signal
    "rss_feeds": ["https://ny.eater.com/rss/index.xml"],
    "trends_geo": "US-NY-501"                                  // Nielsen DMA code; null disables
  },
  "community_calendars": [
    { "name": "Local Run Club", "url": "https://...ical", "category": "fitness" }
  ]
}
```

## Environment variables

```
TOUCH_GRASS_CONFIG=/path/to/config.json     # explicit path (overrides default)
TOUCH_GRASS_PROFILE=date_night              # profile name in profiles/
TOUCH_GRASS_NYC_IMPERSONATE=false           # opt-in browser fingerprinting for NYC museum scrapers
```

API keys (from `~/.config/touch-grass/.env` or shell env):

```
TICKETMASTER_API_KEY=
EVENTBRITE_API_KEY=
YELP_API_KEY=
MEETUP_API_KEY=
NYC_OPENDATA_TOKEN=
```

## How the taste engine reads your profile

- Strings under `interests.*`, `vibe_preferences`, `neighborhoods.favorites`, `preferred_groups`, `bucket_list` become **positive keywords**: events matching them get +0.12 score per hit.
- Strings under `dislikes.*`, `neighborhoods.avoid`, `avoid_groups` become **negative keywords**: -0.2 score per hit.
- An event in a `preferred_groups` group gets +0.3.
- An event in an `avoid_groups` group gets -0.3.
- Final score is clamped to 0..1; events sort by score descending, then by date.

The empty default scores everything 0.5 and sorts purely by date. Fill in fields to feel the difference.
