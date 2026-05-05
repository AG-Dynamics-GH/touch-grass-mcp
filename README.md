# touch-grass-mcp

> Preference-aware events discovery MCP server. Surfaces what you'd actually want to do — not what's loudest.

`touch-grass-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server that aggregates events, restaurants, museums, music, and trending venues across multiple sources, then **re-ranks results against your taste profile** so you stop scrolling generic event listings and start finding things you'd actually go to.

Self-hosted by design. Zero telemetry. All data stays on your machine.

## What you get

**Core (works in any US city):**
- Concerts (Ticketmaster, Resident Advisor, Dice)
- Restaurants + bars (Yelp)
- Local groups + meetups (Meetup, Eventbrite)
- Theater (TodayTix)
- Breweries (Open Brewery DB)
- Weather (NWS)
- Editorial picks (Eater, Gothamist, Time Out, Hyperallergic, Artforum)

**NYC pack (activates when your profile city is New York):**
- Public libraries (NYPL, BPL, QPL)
- Museums (MoMA, Met, Whitney, Frick, Carnegie Hall, 92Y, Park Avenue Armory, MoMA PS1)
- Venues (Lincoln Center, Brooklyn Steel, Metrograph, Village Vanguard, Village Jazz)
- NYC Open Data (city-permitted events)
- Audubon (birding walks)
- Curated community calendars

**The taste engine** — what makes this different from a flat aggregator:
- Profile-driven scoring: your interests, dislikes, vibe preferences, neighborhoods
- City pulse: live trend tracker (Reddit + Google Trends + RSS) that surfaces what's rising

## Install

```bash
pip install touch-grass-mcp
# or with optional pulse + NLP support:
pip install "touch-grass-mcp[pulse,nlp]"
```

## First-run setup

```bash
# 1. Bootstrap your profile
touch-grass init

# 2. (optional) Add API keys for richer results
cp .env.example ~/.config/touch-grass/.env
# edit and fill in: TICKETMASTER_API_KEY, EVENTBRITE_API_KEY, YELP_API_KEY, etc.

# 3. Sanity check
touch-grass doctor
```

## Wire it into Claude Desktop / Claude Code

Add to your MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` or `~/.claude.json`):

```json
{
  "mcpServers": {
    "touch-grass": {
      "command": "touch-grass",
      "args": ["serve"]
    }
  }
}
```

## Tools exposed

23 MCP tools across 5 categories:

**Event search:** `search_events`, `search_concerts`, `discover_niche_events`, `trending_events`, `get_event_details`, `search_community_calendars`, `search_ra_events`, `get_ra_event_details`

**Dining + venues:** `search_restaurants`, `search_breweries`, `get_restaurant_details`

**Arts + culture:** `search_broadway_shows`, `get_broadway_showtimes`, `get_museum_exhibitions`, `search_met_collection`, `get_editorial_picks`, `get_editorial_feed`

**Profile + recommendation:** `get_user_profile`, `update_user_preferences`, `get_recommendation_keywords`

**Calibration + utility:** `weekend_weather`, `log_flag_feedback`, `get_calibration_stats`

## Configuration

- **Profile:** `~/.config/touch-grass/config.json` (run `touch-grass init` to bootstrap)
- **API keys:** `~/.config/touch-grass/.env` (or shell env)
- **Cache + state:** `~/.local/share/touch-grass/` (XDG paths)

See [CONFIG.md](CONFIG.md) for the full profile schema.

## Privacy

Read [PRIVACY.md](PRIVACY.md) before installing. Short version: zero telemetry, no analytics, no phone-home, all data local.

## Architecture

- **`taste/`** — pure-Python preference engine: load profile, score events, rank by relevance. Zero MCP dependencies; importable from any context.
- **`pulse/`** — cultural trend tracker that re-ranks against current Reddit / Trends / RSS signal.
- **`packs.py`** — city-pack registry. NYC pack bundled. Adding a new city is a documented contribution.
- **`server.py`** — FastMCP server that wires it all together.

## Cities other than NYC

The core pack works anywhere in the US. NYC is the bundled deep-coverage city. Adding a new city = drop scrapers in `clients/<city>/`, register a `CityPack` in `packs.py`, supply pulse defaults. PRs welcome.

## License

[MIT](LICENSE).

## Status

v0.1 — alpha. Stable enough for personal use; expect breakage in fragile scrapers (museums, RSS).
