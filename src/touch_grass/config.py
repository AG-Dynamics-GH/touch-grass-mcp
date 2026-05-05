"""Configuration resolution: XDG paths, pydantic-validated profile, env vars.

The user's profile lives at $XDG_CONFIG_HOME/touch-grass/config.json by default.
Override via $TOUCH_GRASS_CONFIG (explicit file path) or $TOUCH_GRASS_PROFILE
(profile name in $XDG_CONFIG_HOME/touch-grass/profiles/<name>.json).

All cached/state data lives at $XDG_DATA_HOME/touch-grass/.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def get_config_dir() -> Path:
    """Return $XDG_CONFIG_HOME/touch-grass/, creating if absent."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    path = base / "touch-grass"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_dir() -> Path:
    """Return $XDG_DATA_HOME/touch-grass/, creating if absent."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    path = base / "touch-grass"
    path.mkdir(parents=True, exist_ok=True)
    (path / "cache").mkdir(exist_ok=True)
    (path / "state").mkdir(exist_ok=True)
    return path


def resolve_config_path() -> Path:
    """Resolve which config file to load.

    Order:
        1. $TOUCH_GRASS_CONFIG (explicit path)
        2. $XDG_CONFIG_HOME/touch-grass/profiles/$TOUCH_GRASS_PROFILE.json
        3. $XDG_CONFIG_HOME/touch-grass/config.json
    """
    explicit = os.environ.get("TOUCH_GRASS_CONFIG")
    if explicit:
        return Path(explicit)

    profile = os.environ.get("TOUCH_GRASS_PROFILE")
    if profile:
        return get_config_dir() / "profiles" / f"{profile}.json"

    return get_config_dir() / "config.json"


def load_profile_dict() -> dict[str, Any]:
    """Load the user profile as a dict. Returns empty config skeleton if missing."""
    path = resolve_config_path()
    if not path.exists():
        return empty_config()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"Failed to load config at {path}: {e}\n"
            f"Run `touch-grass init` to create a fresh config."
        ) from e


def save_profile_dict(config: dict[str, Any]) -> None:
    """Persist the user profile back to disk."""
    path = resolve_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def config_exists() -> bool:
    return resolve_config_path().exists()


def empty_config() -> dict[str, Any]:
    """Skeleton config used when no file exists yet."""
    return {
        "location": {"city": "", "state": "", "zip": "", "radius_miles": 25},
        "user_profile": {
            "name": "",
            "interests": {"music_genres": [], "activities": [], "food_and_drink": [], "topics": []},
            "dislikes": {"music_genres": [], "activities": [], "food_and_drink": []},
            "vibe_preferences": [],
            "neighborhoods": {"favorites": [], "avoid": []},
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
        "pulse": {"enabled": True, "reddit_subs": [], "rss_feeds": [], "trends_geo": None},
        "community_calendars": [],
    }


def is_nyc_impersonate_enabled() -> bool:
    """Check the env flag for browser impersonation in NYC scrapers (default off)."""
    return os.environ.get("TOUCH_GRASS_NYC_IMPERSONATE", "").lower() in ("true", "1", "yes")
