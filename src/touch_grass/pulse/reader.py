#!/usr/bin/env python3
"""Pulse reader — events_mcp integration library for city_pulse snapshots.

Reads `tools/mcp/events_mcp/data/city_pulse.json` (written by
`tools/social/city_pulse.py`) and exposes a small API the events MCP server
can use to re-rank restaurant/event recommendations against current NYC
trend signal.

Public API:
    load_pulse(path=None) -> dict
        Load the snapshot. Returns {} when missing.

    get_signal(name) -> dict | None
        Look up a venue/topic. Case-insensitive, with substring fallback.
        Returns the entity record or None.

    boost_score(name) -> float
        Single 0..1 score combining confirmation, momentum, and the
        anti-saturation flag. Returns 0.0 if entity unknown.

    is_saturated(name) -> bool
        True if the entity's evidence trips the tourist-outlet filter.

    rerank(items, name_key="name") -> list
        Return a copy of `items` sorted by boost_score descending. Each item
        gets `pulse_signal` (None if no match) attached. Saturated venues
        are pushed to the bottom.

CLI for inspection:
    python tools/mcp/events_mcp/pulse_reader.py --venue "bonnie's"
    python tools/mcp/events_mcp/pulse_reader.py --top 10
    python tools/mcp/events_mcp/pulse_reader.py --rerank-stdin <input.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from touch_grass.config import get_data_dir


def _default_pulse_path() -> Path:
    return get_data_dir() / "cache" / "city_pulse.json"


DEFAULT_PULSE_PATH = None  # legacy alias — use _default_pulse_path() at call time

_MOMENTUM_WEIGHT: dict[str, float] = {
    "rising": 1.0,
    "peaked": 0.6,
    "fading": 0.2,
    "unknown": 0.4,
}


# ---------------------------------------------------------------------------
# Loading + lookup
# ---------------------------------------------------------------------------


def load_pulse(path: Path | str | None = None) -> dict[str, Any]:
    """Load city_pulse snapshot. Returns {} on missing file or parse failure."""
    p = Path(path) if path else _default_pulse_path()
    if not p.is_file():
        return {}
    try:
        with p.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _normalize(name: str) -> str:
    """Match the normalization city_pulse applies to entity keys."""
    cleaned = name.strip().strip(".,;:!?\"'`()[]{}").lower()
    if cleaned.startswith(("the ", "a ", "an ")):
        cleaned = cleaned.split(" ", 1)[1]
    if cleaned.endswith("'s") or cleaned.endswith("’s"):
        cleaned = cleaned[:-2]
    return cleaned.strip()


def _index_entities(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a lookup keyed by normalized entity name."""
    return {ent["entity"]: ent for ent in payload.get("entities", [])}


def get_signal(name: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Look up a venue by name with case-insensitive + substring fallback."""
    payload = payload if payload is not None else load_pulse()
    if not payload:
        return None
    idx = _index_entities(payload)
    needle = _normalize(name)
    if needle in idx:
        return idx[needle]
    # Substring fallback — covers "Bonnie's Williamsburg" → "bonnie's"
    for key, ent in idx.items():
        if needle and (needle in key or key in needle):
            return ent
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def boost_score(name: str, payload: dict[str, Any] | None = None) -> float:
    """0..1 trendiness score. Returns 0.0 when the entity is unknown.

    Formula: confirmation * momentum_weight * (saturated ? 0.3 : 1.0)
    Saturated venues are heavily discounted, not zeroed, so very-strong
    signal still surfaces them — but ranked below clean local picks.
    """
    sig = get_signal(name, payload)
    if sig is None:
        return 0.0
    momentum_w = _MOMENTUM_WEIGHT.get(sig.get("momentum", "unknown"), 0.4)
    confirmation = float(sig.get("confirmation") or 0.0)
    saturation_factor = 0.3 if sig.get("saturated") else 1.0
    return round(confirmation * momentum_w * saturation_factor, 3)


def is_saturated(name: str, payload: dict[str, Any] | None = None) -> bool:
    sig = get_signal(name, payload)
    return bool(sig and sig.get("saturated"))


def rerank(
    items: Iterable[dict[str, Any]],
    name_key: str = "name",
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Sort items by boost_score desc, attaching `pulse_signal` to each.

    `name_key` is the dict key to read for the venue/topic name. Items
    without that key keep boost=0.0 and are stable-sorted to the bottom.
    """
    payload = payload if payload is not None else load_pulse()
    annotated: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        name = str(item.get(name_key, "") or "")
        sig = get_signal(name, payload) if name else None
        score = boost_score(name, payload) if name else 0.0
        copy = dict(item)
        copy["pulse_signal"] = (
            None
            if sig is None
            else {
                "score": score,
                "confirmation": sig.get("confirmation"),
                "momentum": sig.get("momentum"),
                "saturated": sig.get("saturated"),
                "sources": sig.get("sources"),
                "mention_count": sig.get("mention_count"),
            }
        )
        annotated.append((score, copy))
    annotated.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in annotated]


# ---------------------------------------------------------------------------
# Snapshot age helper (informational only — never auto-refresh)
# ---------------------------------------------------------------------------


def snapshot_age_hours(payload: dict[str, Any] | None = None) -> float | None:
    """Age of the loaded snapshot in hours. None when no snapshot loaded."""
    payload = payload if payload is not None else load_pulse()
    ts = payload.get("generated_at")
    if not ts:
        return None
    try:
        gen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = datetime.now(UTC) - gen
    return round(delta.total_seconds() / 3600.0, 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Inspect city_pulse snapshots")
    p.add_argument(
        "--path", help="snapshot path (default: tools/mcp/events_mcp/data/city_pulse.json)"
    )
    p.add_argument("--venue", help="look up a single venue/topic and print its signal")
    p.add_argument("--top", type=int, help="show top N entities by boost_score")
    p.add_argument(
        "--rerank-stdin",
        action="store_true",
        help="read JSON list from stdin, rerank by boost, print result",
    )
    p.add_argument("--name-key", default="name", help="dict key used by --rerank-stdin")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = load_pulse(args.path)
    if not payload:
        print("No snapshot found. Run: python tools/social/city_pulse.py", file=sys.stderr)
        return 1

    age = snapshot_age_hours(payload)
    if age is not None:
        print(f"# snapshot age: {age:.1f}h", file=sys.stderr)

    if args.rerank_stdin:
        return _cli_rerank_stdin(payload, args.name_key)
    if args.venue:
        return _cli_show_venue(payload, args.venue)
    if args.top:
        return _cli_show_top(payload, args.top)

    print(json.dumps({"entity_count": payload.get("entity_count", 0)}, indent=2))
    return 0


def _cli_show_venue(payload: dict[str, Any], venue: str) -> int:
    sig = get_signal(venue, payload)
    if sig is None:
        print(json.dumps({"venue": venue, "found": False}, indent=2))
        return 0
    out = {
        "venue": venue,
        "found": True,
        "matched_entity": sig.get("entity"),
        "boost_score": boost_score(venue, payload),
        "saturated": sig.get("saturated"),
        "signal": sig,
    }
    print(json.dumps(out, indent=2))
    return 0


def _cli_show_top(payload: dict[str, Any], top: int) -> int:
    entities = payload.get("entities", [])
    scored = [(boost_score(e["entity"], payload), e) for e in entities]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    rows = [
        {
            "entity": ent["entity"],
            "category": ent.get("category"),
            "boost": score,
            "confirmation": ent.get("confirmation"),
            "momentum": ent.get("momentum"),
            "saturated": ent.get("saturated"),
            "sources": ent.get("sources"),
        }
        for score, ent in scored[:top]
    ]
    print(json.dumps(rows, indent=2))
    return 0


def _cli_rerank_stdin(payload: dict[str, Any], name_key: str) -> int:
    try:
        items = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print("stdin must be a JSON array of objects", file=sys.stderr)
        return 2
    print(json.dumps(rerank(items, name_key=name_key, payload=payload), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
