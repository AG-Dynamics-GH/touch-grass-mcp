"""Preference-aware re-ranking — the differentiator.

Pure data transformation: load a Profile, score events against it.
Zero MCP dependencies; usable from any context.
"""

from touch_grass.taste.profile import Profile, load_profile
from touch_grass.taste.ranking import (
    profile_anti_keywords,
    profile_keywords,
    rank_events,
    score_event,
)

__all__ = [
    "Profile",
    "load_profile",
    "profile_keywords",
    "profile_anti_keywords",
    "score_event",
    "rank_events",
]
