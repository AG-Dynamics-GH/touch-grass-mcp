"""Profile-aware event scoring. The differentiator vs other event MCPs.

Pure data transformation: load a profile, score events, sort by relevance.
Zero MCP dependencies; usable from any Python context.
"""

from __future__ import annotations

from typing import Any


def _flatten_strings(obj: Any) -> list[str]:
    """Recursively pull all string leaves out of a nested dict/list."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_flatten_strings(v))
    return out


def profile_keywords(profile: dict) -> set[str]:
    """Extract positive keywords from a user profile."""
    user = profile.get("user_profile", profile) if isinstance(profile, dict) else {}
    keywords: set[str] = set()

    interests = user.get("interests", {}) or {}
    for v in _flatten_strings(interests):
        keywords.add(v.lower())

    keywords.update(s.lower() for s in user.get("vibe_preferences", []) or [])
    keywords.update(s.lower() for s in user.get("neighborhoods", {}).get("favorites", []) or [])
    keywords.update(s.lower() for s in user.get("preferred_groups", []) or [])
    keywords.update(s.lower() for s in user.get("bucket_list", []) or [])

    learned = user.get("learned_preferences", []) or []
    for entry in learned:
        if entry.get("signal") == "liked":
            for kw in entry.get("keywords", []) or []:
                keywords.add(kw.lower())

    return {k for k in keywords if k}


def profile_anti_keywords(profile: dict) -> set[str]:
    """Extract negative keywords from a user profile."""
    user = profile.get("user_profile", profile) if isinstance(profile, dict) else {}
    anti: set[str] = set()

    dislikes = user.get("dislikes", {}) or {}
    for v in _flatten_strings(dislikes):
        anti.add(v.lower())

    anti.update(s.lower() for s in user.get("neighborhoods", {}).get("avoid", []) or [])
    anti.update(s.lower() for s in user.get("avoid_groups", []) or [])

    learned = user.get("learned_preferences", []) or []
    for entry in learned:
        if entry.get("signal") == "disliked":
            for kw in entry.get("keywords", []) or []:
                anti.add(kw.lower())

    return {k for k in anti if k}


def _event_text(event: dict) -> str:
    parts = [
        event.get("name", ""),
        event.get("description", ""),
        event.get("genre", ""),
        event.get("group_name", ""),
        event.get("venue_name", ""),
        " ".join(
            event.get("categories", [])
            if isinstance(event.get("categories"), list)
            else [str(event.get("categories", ""))]
        ),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def score_event(event: dict, profile: dict) -> tuple[float, list[str]]:
    """Score an event against a profile. Returns (score 0..1, reasons)."""
    pos = profile_keywords(profile)
    neg = profile_anti_keywords(profile)
    return _score_against(event, pos, neg, profile)


def _score_against(
    event: dict, pos: set[str], neg: set[str], profile: dict
) -> tuple[float, list[str]]:
    text = _event_text(event)
    score = 0.5
    reasons = []

    pos_hits = [k for k in pos if k in text]
    if pos_hits:
        score += 0.12 * len(pos_hits)
        reasons.append(f"+matches: {', '.join(pos_hits[:3])}")

    neg_hits = [k for k in neg if k in text]
    if neg_hits:
        score -= 0.2 * len(neg_hits)
        reasons.append(f"-avoid: {', '.join(neg_hits[:3])}")

    user = profile.get("user_profile", profile) if isinstance(profile, dict) else {}
    group_name = (event.get("group_name") or "").lower()
    if group_name:
        if group_name in {g.lower() for g in user.get("preferred_groups", []) or []}:
            score += 0.3
            reasons.append("+preferred group")
        if group_name in {g.lower() for g in user.get("avoid_groups", []) or []}:
            score -= 0.3
            reasons.append("-avoid group")

    return max(0.0, min(1.0, score)), reasons


def rank_events(events: list[dict], profile: dict) -> list[dict]:
    """Score and sort events by relevance to the profile, then by date/time."""
    pos = profile_keywords(profile)
    neg = profile_anti_keywords(profile)
    scored = []
    for ev in events:
        score, reasons = _score_against(ev, pos, neg, profile)
        ev = dict(ev)
        ev["_relevance"] = score
        ev["_reasons"] = reasons
        scored.append(ev)
    scored.sort(key=lambda e: (-e["_relevance"], e.get("date", ""), e.get("time", "")))
    return scored
