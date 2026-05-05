"""touch-grass CLI — bootstrap config, run server, clean cache."""

from __future__ import annotations

import argparse
import sys

from touch_grass import __version__
from touch_grass.config import (
    config_exists,
    get_config_dir,
    get_data_dir,
    resolve_config_path,
    save_profile_dict,
)
from touch_grass.packs import resolve_pack


def cmd_init(args) -> int:
    """Interactive bootstrap: ask city, interests, neighborhoods → write config.json."""
    if config_exists() and not args.force:
        path = resolve_config_path()
        print(f"Config already exists at {path}")
        print("Use --force to overwrite, or edit it directly.")
        return 1

    print("touch-grass init — let's set up your profile.\n")

    city = input("Your city (e.g. New York, Chicago, Austin): ").strip()
    state = input("State (2-letter, e.g. NY): ").strip().upper()
    zip_code = input("ZIP code (optional): ").strip()

    print("\nWhat do you like? Comma-separated keywords (e.g. jazz, indie rock).")
    music = input("  Music genres: ").strip()
    activities = input("  Activities (running, yoga, art galleries...): ").strip()
    food = input("  Food / drink (rooftop bars, wine, ramen...): ").strip()

    favorite_neighborhoods = input("\nFavorite neighborhoods (optional, comma-separated): ").strip()

    print()
    config = {
        "location": {
            "city": city,
            "state": state,
            "zip": zip_code,
            "radius_miles": 25,
        },
        "user_profile": {
            "name": "",
            "interests": {
                "music_genres": [s.strip() for s in music.split(",") if s.strip()],
                "activities": [s.strip() for s in activities.split(",") if s.strip()],
                "food_and_drink": [s.strip() for s in food.split(",") if s.strip()],
                "topics": [],
            },
            "dislikes": {"music_genres": [], "activities": [], "food_and_drink": []},
            "vibe_preferences": [],
            "neighborhoods": {
                "favorites": [s.strip() for s in favorite_neighborhoods.split(",") if s.strip()],
                "avoid": [],
            },
            "schedule": {
                "preferred_days": ["friday", "saturday", "sunday"],
                "preferred_times": ["evening"],
                "budget": "no_limit",
                "avoid_early_morning": True,
            },
            "social_context": {
                "typical_group_size": 2,
                "open_to_solo": True,
                "open_to_group_events": True,
            },
            "bucket_list": [],
            "preferred_groups": [],
            "avoid_groups": [],
        },
        "pulse": {
            "enabled": True,
            "reddit_subs": [],
            "rss_feeds": [],
            "trends_geo": None,
        },
        "community_calendars": [],
    }

    # Auto-fill pulse defaults if a city pack matches
    pack = resolve_pack(city)
    if pack:
        config["pulse"]["reddit_subs"] = list(pack.pulse_defaults.reddit_subs)
        config["pulse"]["rss_feeds"] = list(pack.pulse_defaults.rss_feeds)
        config["pulse"]["trends_geo"] = pack.pulse_defaults.trends_geo
        print(f"✓ {pack.name.upper()} pack detected — pulse defaults auto-filled.")

    save_profile_dict(config)
    path = resolve_config_path()
    print(f"✓ Config written to {path}")
    print()
    print("Next steps:")
    print("  1. Add API keys to ~/.config/touch-grass/.env (see .env.example in the repo)")
    print("  2. Add the MCP server to Claude Desktop / Claude Code config:")
    print("     command: touch-grass")
    print("     args: ['serve']")
    return 0


def cmd_serve(args) -> int:
    """Run the MCP server (stdio by default)."""
    from touch_grass.server import mcp

    if args.http:
        print("HTTP transport not yet wired in v0.1 — run with stdio (default).", file=sys.stderr)
        return 2

    mcp.run()
    return 0


def cmd_clean(args) -> int:
    """Purge cache files older than --days days."""
    import time

    cutoff = time.time() - (args.days * 86400)
    cache_dir = get_data_dir() / "cache"
    if not cache_dir.exists():
        print("No cache directory yet.")
        return 0

    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    print(f"Removed {removed} cache files older than {args.days} days.")
    return 0


def cmd_doctor(args) -> int:
    """Sanity check: config exists, env vars set, paths writable."""
    print(f"touch-grass-mcp {__version__}\n")
    print(f"Config dir:  {get_config_dir()}")
    print(f"Data dir:    {get_data_dir()}")
    print(f"Config file: {resolve_config_path()}")
    print(f"  exists: {config_exists()}")

    import os

    keys = [
        "TICKETMASTER_API_KEY",
        "EVENTBRITE_API_KEY",
        "YELP_API_KEY",
        "MEETUP_API_KEY",
        "NYC_OPENDATA_TOKEN",
    ]
    print("\nAPI keys:")
    for k in keys:
        present = "✓" if os.environ.get(k) else "✗"
        print(f"  {present} {k}")

    print(f"\nNYC impersonation flag: {os.environ.get('TOUCH_GRASS_NYC_IMPERSONATE', 'unset')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="touch-grass", description="touch-grass MCP server")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="bootstrap a new config")
    p_init.add_argument("--force", action="store_true", help="overwrite existing config")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="run the MCP server")
    p_serve.add_argument("--http", action="store_true", help="HTTP transport (not in v0.1)")
    p_serve.set_defaults(func=cmd_serve)

    p_clean = sub.add_parser("clean", help="purge old cache files")
    p_clean.add_argument("--days", type=int, default=30, help="age threshold in days")
    p_clean.set_defaults(func=cmd_clean)

    p_doctor = sub.add_parser("doctor", help="sanity check config + env")
    p_doctor.set_defaults(func=cmd_doctor)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
