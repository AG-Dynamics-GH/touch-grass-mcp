#!/usr/bin/env python3
"""City Pulse — NYC cultural trend tracker.

Fuses NYC trend signals from Reddit, Google Trends, and editorial RSS feeds
into a single normalized snapshot. Output JSON is consumed by the events_mcp
server for trend-aware re-ranking of restaurants/events, and by the IG
specialist for context-aware caption/venue choices.

Sources (Phase 1):
- Reddit (unauthenticated public JSON): r/nyc, r/AskNYC, r/FoodNYC, r/Brooklyn
- Google Trends (pytrends): rising NYC-region queries (geo=US-NY-501)
- Editorial RSS (feedparser): Eater NYC, Curbed NY, Time Out NY

Output:
    .tmp/city_pulse.json
    tools/mcp/events_mcp/data/city_pulse.json

Each entity record has:
    entity, category, sources, mention_count, first_seen, last_seen,
    confirmation (0..1), momentum (rising|peaked|fading|unknown),
    saturated (bool), evidence

Confirmation score: 3+ sources = 1.0, 2 = 0.6, 1 = 0.2.
Anti-trend: hits in mainstream tourist outlets flag a venue as "saturated"
so downstream consumers can demote it for a user who prefers local-only.

CLI:
    python tools/social/city_pulse.py            # human-readable
    python tools/social/city_pulse.py --json     # JSON to stdout
    python tools/social/city_pulse.py --source reddit --no-write
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from touch_grass.config import get_data_dir

logger = logging.getLogger("touch_grass.pulse")
logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomic JSON write — write to .tmp file then rename."""
    import json
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=str(path.parent))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def colorize(text: str, color: str = "") -> str:
    """No-op color helper for the public release (no ANSI styling)."""
    return str(text)


def _output_path() -> Path:
    return get_data_dir() / "cache" / "city_pulse.json"


_OUTPUT_TMP = None  # legacy alias — use _output_path()
_OUTPUT_MCP = None  # legacy alias — use _output_path()

REDDIT_SUBS: tuple[str, ...] = (
    "nyc",
    "AskNYC",
    "FoodNYC",
    "Brooklyn",
    "manhattan",
)
RSS_FEEDS: dict[str, str] = {
    "eater": "https://ny.eater.com/rss/index.xml",
    "curbed": "https://www.curbed.com/rss/index.xml",
    "timeout": "https://www.timeout.com/newyork/feed.rss",
}
TRENDS_KEYWORDS: tuple[str, ...] = (
    "restaurants nyc",
    "things to do nyc",
    "best bars nyc",
    "brooklyn",
    "williamsburg",
    "lower east side",
)
TRENDS_GEO = "US-NY-501"  # Nielsen DMA for NYC

# Anti-trend filter — outlets/domains that signal tourist saturation
SATURATION_DOMAINS: frozenset[str] = frozenset({"timeout.com", "thrillist.com", "tripadvisor.com"})

USER_AGENT = "city_pulse/0.1 (research; noreply@example.com)"

# Stopwords + non-entities. Lowercase comparison.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "to",
        "in",
        "of",
        "for",
        "on",
        "at",
        "and",
        "or",
        "but",
        "with",
        "this",
        "that",
        "i",
        "my",
        "we",
        "you",
        "they",
        "nyc",
        "york",
        "new",
        "what",
        "where",
        "how",
        "why",
        "when",
        "best",
        "top",
        "looking",
        "anyone",
        "thanks",
        "help",
        "today",
        "tonight",
        "weekend",
        "tomorrow",
    }
)

# Neighborhood vocabulary for category classification
_NEIGHBORHOODS: tuple[str, ...] = (
    "williamsburg",
    "bushwick",
    "soho",
    "tribeca",
    "lower east side",
    "east village",
    "west village",
    "harlem",
    "park slope",
    "dumbo",
    "chinatown",
    "flatiron",
    "midtown",
    "uptown",
    "fidi",
    "long island city",
    "greenpoint",
    "crown heights",
    "fort greene",
    "astoria",
)

# Regex extraction (fallback when spaCy is unavailable):
#   quoted names, markdown bold, "at/to/from <Capitalized>"
_ENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'"([^"]{3,60})"'),
    re.compile(r"\*\*([^*]{3,60})\*\*"),
    re.compile(r"(?:at|to|from)\s+([A-Z][\w'&]+(?:\s+[A-Z][\w'&]+){0,3})"),
)

# spaCy entity-type → our category vocabulary
_SPACY_TYPE_MAP: dict[str, str] = {
    "PERSON": "person",
    "ORG": "venue",
    "FAC": "venue",
    "GPE": "neighborhood",
    "LOC": "neighborhood",
    "EVENT": "event",
    "WORK_OF_ART": "topic",
    "PRODUCT": "topic",
}
# spaCy types we ignore: NORP, DATE, TIME, MONEY, ORDINAL, CARDINAL, PERCENT,
# QUANTITY, LANGUAGE, LAW.

_NLP: Any = None  # lazy-loaded spaCy pipeline; None if unavailable
_NLP_LOAD_ATTEMPTED: bool = False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    source: str
    title: str
    url: str
    seen_at: str


@dataclass
class TrendEntity:
    entity: str
    category: str
    sources: set[str] = field(default_factory=set)
    mention_count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    evidence: list[Evidence] = field(default_factory=list)

    def add(self, source: str, title: str, url: str, seen_at: datetime) -> None:
        iso = seen_at.astimezone(UTC).isoformat()
        self.sources.add(source)
        self.mention_count += 1
        if self.first_seen is None or iso < self.first_seen:
            self.first_seen = iso
        if self.last_seen is None or iso > self.last_seen:
            self.last_seen = iso
        if len(self.evidence) < 5:
            self.evidence.append(Evidence(source, title, url, iso))


# ---------------------------------------------------------------------------
# Source fetchers
#   All return list[dict] with: source, title, url, text, seen_at (datetime)
# ---------------------------------------------------------------------------


def fetch_reddit(
    subs: Iterable[str] = REDDIT_SUBS,
    limit: int = 50,
    http: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Fetch hot posts from NYC subreddits via unauthenticated JSON.

    Reddit's /r/<sub>/hot.json works without auth at low volume (~60 req/min
    per IP). For production density, set REDDIT_CLIENT_ID/SECRET in .env and
    swap to praw — left as a Phase 2 upgrade.
    """
    sess = http or requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})
    out: list[dict[str, Any]] = []
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
        try:
            resp = sess.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("reddit fetch failed for r/%s: %s", sub, exc)
            continue
        out.extend(_parse_reddit_listing(data, sub))
    return out


def _parse_reddit_listing(data: dict[str, Any], sub: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        out.append(
            {
                "source": "reddit",
                "subreddit": sub,
                "title": d.get("title", ""),
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "text": d.get("selftext", ""),
                "seen_at": datetime.fromtimestamp(d.get("created_utc") or 0, tz=UTC),
                "score": d.get("score", 0),
            }
        )
    return out


def fetch_trends(
    keywords: Iterable[str] = TRENDS_KEYWORDS,
    geo: str = TRENDS_GEO,
) -> list[dict[str, Any]]:
    """Pull rising NYC queries via pytrends. Optional dep — graceful fallback."""
    try:
        from pytrends.request import TrendReq  # type: ignore[import-untyped]
    except ImportError:
        logger.info("pytrends not installed — skipping trends source")
        return []

    out: list[dict[str, Any]] = []
    pytrend = TrendReq(hl="en-US", tz=300, timeout=(5, 15))
    now = datetime.now(UTC)
    for kw in keywords:
        rows = _trends_for_keyword(pytrend, kw, geo)
        for query, value in rows:
            out.append(
                {
                    "source": "trends",
                    "title": query,
                    "url": (f"https://trends.google.com/trends/explore?q={query}&geo={geo}"),
                    "text": "",
                    "seen_at": now,
                    "score": value,
                }
            )
    return out


def _trends_for_keyword(pytrend: Any, kw: str, geo: str) -> list[tuple[str, int]]:
    """Single-keyword pytrends call, returns (query, value) pairs from rising."""
    try:
        pytrend.build_payload([kw], timeframe="now 7-d", geo=geo)
        related = pytrend.related_queries().get(kw) or {}
        rising = related.get("rising")
    except Exception as exc:  # pytrends raises a wide variety
        logger.warning("pytrends fetch failed for %s: %s", kw, exc)
        return []
    if rising is None or getattr(rising, "empty", True):
        return []
    out: list[tuple[str, int]] = []
    for _, row in rising.head(10).iterrows():
        query = str(row.get("query", "")).strip()
        if not query:
            continue
        out.append((query, int(row.get("value") or 0)))
    return out


def fetch_rss(feeds: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Fetch editorial RSS items. feedparser handles malformed feeds gracefully."""
    feeds = feeds if feeds is not None else RSS_FEEDS
    try:
        import feedparser  # type: ignore[import-untyped]
    except ImportError:
        logger.info("feedparser not installed — skipping rss source")
        return []

    out: list[dict[str, Any]] = []
    for source, url in feeds.items():
        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
        except Exception as exc:
            logger.warning("rss fetch failed for %s: %s", source, exc)
            continue
        for entry in (parsed.entries or [])[:30]:
            out.append(
                {
                    "source": source,
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "text": entry.get("summary", ""),
                    "seen_at": _parse_rss_date(entry.get("published_parsed")),
                    "score": 1,
                }
            )
    return out


def _parse_rss_date(parsed_struct: Any) -> datetime:
    if parsed_struct is None:
        return datetime.now(UTC)
    try:
        epoch = time.mktime(parsed_struct)
    except (ValueError, TypeError, OverflowError):
        return datetime.now(UTC)
    return datetime.fromtimestamp(epoch, tz=UTC)


# ---------------------------------------------------------------------------
# Entity extraction & fusion
# ---------------------------------------------------------------------------


def _get_nlp() -> Any:
    """Lazy-load spaCy en_core_web_sm. Returns None if unavailable.

    Cached after first call (success or failure) to avoid repeated import
    attempts. Install with: .venv/bin/python -m spacy download en_core_web_sm
    """
    global _NLP, _NLP_LOAD_ATTEMPTED
    if _NLP_LOAD_ATTEMPTED:
        return _NLP
    _NLP_LOAD_ATTEMPTED = True
    try:
        import spacy  # type: ignore[import-untyped]
    except ImportError:
        logger.info("spaCy not installed — falling back to regex entity extraction")
        return None
    try:
        _NLP = spacy.load("en_core_web_sm")
    except OSError:
        logger.warning(
            "spaCy model en_core_web_sm not found — run: python -m spacy download en_core_web_sm"
        )
        _NLP = None
    return _NLP


def extract_entities(item: dict[str, Any]) -> list[tuple[str, str]]:
    """Return [(name, category), ...] for a fetched item.

    Uses spaCy NER when available (precise, type-aware). Falls back to the
    regex extractor when spaCy or its model are missing.
    """
    text = _clean_source_text(f"{item.get('title') or ''} {item.get('text') or ''}")
    nlp = _get_nlp()
    if nlp is not None:
        return _extract_entities_spacy(text, nlp)
    return _extract_entities_regex(text)


def _clean_source_text(text: str) -> str:
    """Decode HTML entities and strip tags from RSS-flavored payloads."""
    import html

    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_entities_spacy(text: str, nlp: Any) -> list[tuple[str, str]]:
    """spaCy-backed extractor. Filters by entity type, normalizes names."""
    if not text.strip():
        return []
    doc = nlp(text)
    seen: dict[str, str] = {}
    for ent in doc.ents:
        category = _SPACY_TYPE_MAP.get(ent.label_)
        if category is None:
            continue
        name = _normalize_entity(ent.text)
        if not _looks_like_entity(name):
            continue
        seen.setdefault(name, category)  # first label wins
    return sorted(seen.items())


def _normalize_entity(raw: str) -> str:
    """Lowercase, drop possessive 's, strip leading articles + punctuation."""
    name = raw.strip().strip(".,;:!?\"'`()[]{}").lower()
    if name.startswith(("the ", "a ", "an ")):
        name = name.split(" ", 1)[1]
    if name.endswith("'s") or name.endswith("’s"):
        name = name[:-2]
    return name.strip()


def _extract_entities_regex(text: str) -> list[tuple[str, str]]:
    """Regex fallback. Lower precision than spaCy — kept for envs without it."""
    candidates: set[str] = set()
    for pat in _ENTITY_PATTERNS:
        for match in pat.findall(text):
            cand = match.strip().lower()
            if _looks_like_entity(cand):
                candidates.add(cand)
    return [(name, _classify_by_keyword(name)) for name in sorted(candidates)]


def _looks_like_entity(s: str) -> bool:
    if len(s) < 3 or len(s) > 60:
        return False
    if s in _STOPWORDS:
        return False
    return any(c.isalpha() for c in s)


def fuse(items: list[dict[str, Any]]) -> dict[str, TrendEntity]:
    """Merge items into entity records keyed by normalized name."""
    entities: dict[str, TrendEntity] = {}
    for item in items:
        for ent_name, category in extract_entities(item):
            ent = entities.setdefault(
                ent_name,
                TrendEntity(entity=ent_name, category=category),
            )
            ent.add(
                source=item.get("source", "unknown"),
                title=item.get("title", ""),
                url=item.get("url", ""),
                seen_at=item.get("seen_at") or datetime.now(UTC),
            )
    return entities


def _classify_by_keyword(name: str) -> str:
    """Heuristic classify used by the regex fallback path only."""
    lower = name.lower()
    if any(n in lower for n in _NEIGHBORHOODS):
        return "neighborhood"
    return "venue"


# ---------------------------------------------------------------------------
# Scoring: confirmation, momentum, anti-trend
# ---------------------------------------------------------------------------


def confirmation_score(ent: TrendEntity) -> float:
    """0..1; >=3 sources = 1.0, 2 = 0.6, 1 = 0.2."""
    n = len(ent.sources)
    if n >= 3:
        return 1.0
    if n == 2:
        return 0.6
    return 0.2


def momentum(ent: TrendEntity, now: datetime | None = None) -> str:
    """Rising / peaked / fading based on first→last span and mention density."""
    if not ent.first_seen or not ent.last_seen:
        return "unknown"
    now = now or datetime.now(UTC)
    last = _parse_iso(ent.last_seen)
    first = _parse_iso(ent.first_seen)
    if last is None or first is None:
        return "unknown"
    if (now - last).days > 14:
        return "fading"
    span_days = max((last - first).total_seconds() / 86400.0, 1 / 24)
    rate = ent.mention_count / span_days
    if rate > 1.5:
        return "rising"
    return "peaked"


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_saturated(ent: TrendEntity) -> bool:
    """Anti-trend filter: hits in mainstream tourist outlets = saturated."""
    return any(any(d in (ev.url or "") for d in SATURATION_DOMAINS) for ev in ent.evidence)


# ---------------------------------------------------------------------------
# Render & orchestration
# ---------------------------------------------------------------------------


def render(entities: dict[str, TrendEntity]) -> dict[str, Any]:
    """JSON-safe payload, sorted by confirmation then mention count."""
    now = datetime.now(UTC)
    rendered: list[dict[str, Any]] = []
    for ent in entities.values():
        rendered.append(
            {
                "entity": ent.entity,
                "category": ent.category,
                "sources": sorted(ent.sources),
                "mention_count": ent.mention_count,
                "first_seen": ent.first_seen,
                "last_seen": ent.last_seen,
                "confirmation": round(confirmation_score(ent), 2),
                "momentum": momentum(ent, now),
                "saturated": is_saturated(ent),
                "evidence": [asdict(ev) for ev in ent.evidence[:5]],
            }
        )
    rendered.sort(key=lambda e: (e["confirmation"], e["mention_count"]), reverse=True)
    return {
        "generated_at": now.isoformat(),
        "city": "NYC",
        "entity_count": len(rendered),
        "entities": rendered,
    }


def collect(sources: Iterable[str] = ("reddit", "trends", "rss")) -> list[dict[str, Any]]:
    """Run requested source fetchers and concatenate results."""
    src_set = set(sources)
    items: list[dict[str, Any]] = []
    if "reddit" in src_set:
        items.extend(fetch_reddit())
    if "trends" in src_set:
        items.extend(fetch_trends())
    if "rss" in src_set:
        items.extend(fetch_rss())
    return items


def snapshot(
    sources: Iterable[str] = ("reddit", "trends", "rss"),
) -> dict[str, Any]:
    items = collect(sources=sources)
    entities = fuse(items)
    payload = render(entities)
    payload["source_counts"] = _source_counts(items)
    return payload


def _source_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for it in items:
        counts[it.get("source", "unknown")] += 1
    return dict(counts)


def write_snapshot(payload: dict[str, Any]) -> tuple[Path, Path]:
    """Write the pulse snapshot to the XDG data directory."""
    out_path = _output_path()
    atomic_write_json(out_path, payload)
    return out_path, out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="NYC city pulse trend tracker")
    p.add_argument("--json", action="store_true", help="emit JSON to stdout")
    p.add_argument("--no-write", action="store_true", help="skip writing snapshot files")
    p.add_argument(
        "--source",
        action="append",
        choices=["reddit", "trends", "rss"],
        help="restrict to specific source(s); repeatable",
    )
    p.add_argument("--top", type=int, default=25, help="rows to print in human view")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    sources = tuple(args.source) if args.source else ("reddit", "trends", "rss")
    payload = snapshot(sources=sources)
    if not args.no_write:
        write_snapshot(payload)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_human(payload, args.top)
    return 0


def _print_human(payload: dict[str, Any], top: int) -> None:
    counts = payload.get("source_counts", {})
    counts_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(colorize(f"NYC Pulse — {payload['generated_at']}", "cyan"))
    print(f"  {payload['entity_count']} entities  ({counts_str})")
    print()
    for ent in payload["entities"][:top]:
        marker = colorize("!", "yellow") if ent["saturated"] else " "
        print(
            f"  {marker} {ent['entity']:<35} "
            f"conf={ent['confirmation']:.1f} "
            f"mentions={ent['mention_count']:<3} "
            f"momentum={ent['momentum']:<7} "
            f"sources={','.join(ent['sources'])}"
        )


if __name__ == "__main__":
    sys.exit(main())
