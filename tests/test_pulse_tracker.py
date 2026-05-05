"""Tests for touch_grass.pulse.tracker — pure helpers + fused pipeline.

All network sources are mocked. Verifies extraction, scoring, fusion, and
the end-to-end snapshot shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from touch_grass.pulse import tracker as cp

# ---------------------------------------------------------------------------
# Entity extraction — regex fallback path (spaCy forced unavailable)
# ---------------------------------------------------------------------------


@pytest.fixture
def force_regex_path(monkeypatch):
    """Force _get_nlp to return None so the regex extractor runs."""
    monkeypatch.setattr(cp, "_get_nlp", lambda: None)


def test_regex_extracts_quoted_names(force_regex_path):
    item = {"title": 'New review: "Carbone" is the spot'}
    found = cp.extract_entities(item)
    assert ("carbone", "venue") in found


def test_regex_extracts_markdown_bold(force_regex_path):
    item = {"title": "", "text": "Loved **Carbone** for date night"}
    found = cp.extract_entities(item)
    assert ("carbone", "venue") in found


def test_regex_skips_stopwords(force_regex_path):
    item = {"title": '"the" "york"', "text": ""}
    assert cp.extract_entities(item) == []


def test_regex_rejects_too_short_or_long(force_regex_path):
    item = {"title": '"ab" "' + ("x" * 80) + '"', "text": ""}
    assert cp.extract_entities(item) == []


def test_regex_at_pattern_extracts_capitalized_target(force_regex_path):
    item = {"title": "Heading to Lilia tonight", "text": ""}
    found = cp.extract_entities(item)
    assert any(name == "lilia" for name, _ in found)


def test_regex_classifies_neighborhood(force_regex_path):
    item = {"title": "", "text": "**Williamsburg** is hot rn"}
    found = cp.extract_entities(item)
    assert ("williamsburg", "neighborhood") in found


# ---------------------------------------------------------------------------
# Entity extraction — spaCy path (mocked nlp)
# ---------------------------------------------------------------------------


def _spacy_doc_stub(ents):
    """Build a minimal stand-in for nlp(text) returning .ents iterable."""
    fake_ents = [MagicMock(text=text, label_=label) for text, label in ents]
    doc = MagicMock()
    doc.ents = fake_ents
    return doc


def test_spacy_path_uses_native_entity_types(monkeypatch):
    fake_nlp = MagicMock(
        side_effect=lambda text: _spacy_doc_stub(
            [("Wallace Shawn", "PERSON"), ("Williamsburg", "GPE"), ("Carbone", "ORG")]
        )
    )
    monkeypatch.setattr(cp, "_get_nlp", lambda: fake_nlp)
    item = {"title": "Wallace Shawn at Carbone in Williamsburg", "text": ""}
    found = dict(cp.extract_entities(item))
    assert found.get("wallace shawn") == "person"
    assert found.get("williamsburg") == "neighborhood"
    assert found.get("carbone") == "venue"


def test_spacy_path_drops_uninteresting_types(monkeypatch):
    fake_nlp = MagicMock(
        side_effect=lambda text: _spacy_doc_stub(
            [("$50", "MONEY"), ("Tuesday", "DATE"), ("Carbone", "ORG")]
        )
    )
    monkeypatch.setattr(cp, "_get_nlp", lambda: fake_nlp)
    item = {"title": "$50 fixed-price on Tuesday at Carbone", "text": ""}
    found = dict(cp.extract_entities(item))
    assert "$50" not in found
    assert "tuesday" not in found
    assert found.get("carbone") == "venue"


# ---------------------------------------------------------------------------
# Text + name normalization
# ---------------------------------------------------------------------------


def test_clean_source_text_strips_html_and_entities():
    raw = "<p>The strip <em>comes</em> &amp; goes &#8230;</p>"
    out = cp._clean_source_text(raw)
    assert "<" not in out
    assert "&amp;" not in out
    assert "&#8230;" not in out
    assert "strip" in out.lower()


def test_normalize_entity_strips_possessive_and_articles():
    assert cp._normalize_entity("Bonnie's") == "bonnie"
    assert cp._normalize_entity("The West Village") == "west village"
    assert cp._normalize_entity("  Carbone, ") == "carbone"


def test_get_nlp_returns_none_when_spacy_missing(monkeypatch):
    cp._NLP_LOAD_ATTEMPTED = False
    cp._NLP = None
    import builtins

    real_import = builtins.__import__

    def fail_import(name, *a, **kw):
        if name == "spacy":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fail_import)
    assert cp._get_nlp() is None
    # cleanup so other tests using the real model don't get cached None
    cp._NLP_LOAD_ATTEMPTED = False
    cp._NLP = None


# ---------------------------------------------------------------------------
# TrendEntity.add — first/last/source bookkeeping
# ---------------------------------------------------------------------------


def test_trend_entity_add_tracks_first_and_last_seen():
    ent = cp.TrendEntity(entity="bonnie's", category="venue")
    t1 = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ent.add("reddit", "post1", "u1", t2)
    ent.add("eater", "post2", "u2", t1)
    assert ent.first_seen == t1.isoformat()
    assert ent.last_seen == t2.isoformat()
    assert ent.sources == {"reddit", "eater"}
    assert ent.mention_count == 2


def test_trend_entity_caps_evidence_at_five():
    ent = cp.TrendEntity(entity="x", category="venue")
    now = datetime.now(UTC)
    for i in range(10):
        ent.add(f"src{i}", f"t{i}", f"u{i}", now)
    assert len(ent.evidence) == 5
    assert ent.mention_count == 10


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_confirmation_score_thresholds():
    ent = cp.TrendEntity(entity="x", category="venue")
    ent.sources = {"reddit"}
    assert cp.confirmation_score(ent) == 0.2
    ent.sources = {"reddit", "eater"}
    assert cp.confirmation_score(ent) == 0.6
    ent.sources = {"reddit", "eater", "curbed"}
    assert cp.confirmation_score(ent) == 1.0


def test_momentum_rising_when_dense():
    ent = cp.TrendEntity(entity="x", category="venue")
    now = datetime(2026, 4, 25, tzinfo=UTC)
    # 5 mentions over 1 day -> 5/day -> rising
    for i in range(5):
        ent.add("reddit", "t", "u", now - timedelta(hours=i * 4))
    assert cp.momentum(ent, now) == "rising"


def test_momentum_fading_when_old():
    ent = cp.TrendEntity(entity="x", category="venue")
    now = datetime(2026, 4, 25, tzinfo=UTC)
    old = now - timedelta(days=30)
    ent.add("reddit", "t", "u", old)
    assert cp.momentum(ent, now) == "fading"


def test_momentum_unknown_when_empty():
    ent = cp.TrendEntity(entity="x", category="venue")
    assert cp.momentum(ent) == "unknown"


def test_is_saturated_flags_tourist_outlets():
    ent = cp.TrendEntity(entity="joe's pizza", category="venue")
    ent.evidence.append(
        cp.Evidence(
            source="timeout",
            title="Best slices",
            url="https://timeout.com/newyork/pizza",
            seen_at="2026-04-25T00:00:00+00:00",
        )
    )
    assert cp.is_saturated(ent) is True


def test_is_saturated_clean_for_local_only():
    ent = cp.TrendEntity(entity="bonnie's", category="venue")
    ent.evidence.append(
        cp.Evidence(
            source="reddit",
            title="great spot",
            url="https://reddit.com/r/nyc/abc",
            seen_at="2026-04-25T00:00:00+00:00",
        )
    )
    assert cp.is_saturated(ent) is False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classify_by_keyword_neighborhood():
    assert cp._classify_by_keyword("williamsburg") == "neighborhood"
    assert cp._classify_by_keyword("east village") == "neighborhood"


def test_classify_by_keyword_venue_default():
    assert cp._classify_by_keyword("bonnies") == "venue"


# ---------------------------------------------------------------------------
# Fuse — fold items into entity dict
# ---------------------------------------------------------------------------


def test_fuse_merges_same_entity_across_sources(monkeypatch):
    """Force the regex extractor and verify fuse merges by normalized name."""
    monkeypatch.setattr(cp, "_get_nlp", lambda: None)
    now = datetime(2026, 4, 25, tzinfo=UTC)
    items = [
        {
            "source": "reddit",
            "title": 'Tried "Carbone" last night',
            "text": "",
            "url": "u1",
            "seen_at": now,
        },
        {
            "source": "eater",
            "title": 'Eater review: "Carbone" still the move',
            "text": "",
            "url": "u2",
            "seen_at": now,
        },
    ]
    entities = cp.fuse(items)
    assert "carbone" in entities
    ent = entities["carbone"]
    assert ent.sources == {"reddit", "eater"}
    assert ent.mention_count == 2


# ---------------------------------------------------------------------------
# Reddit fetcher — mocked HTTP
# ---------------------------------------------------------------------------


def test_fetch_reddit_parses_listing_payload():
    fake_payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "What's everyone eating tonight?",
                        "permalink": "/r/nyc/comments/abc",
                        "selftext": "Heading to Lilia",
                        "created_utc": 1714000000,
                        "score": 42,
                    }
                }
            ]
        }
    }
    sess = MagicMock()
    sess.headers = {}
    resp = MagicMock()
    resp.json.return_value = fake_payload
    resp.raise_for_status = MagicMock()
    sess.get.return_value = resp

    items = cp.fetch_reddit(subs=("nyc",), limit=5, http=sess)
    assert len(items) == 1
    assert items[0]["source"] == "reddit"
    assert items[0]["title"] == "What's everyone eating tonight?"
    assert items[0]["seen_at"].tzinfo is not None


def test_fetch_reddit_continues_on_failure():
    sess = MagicMock()
    sess.headers = {}
    sess.get.side_effect = [
        # First sub fails
        type(
            "R",
            (),
            {
                "json": lambda self: (_ for _ in ()).throw(ValueError("bad")),
                "raise_for_status": lambda self: None,
            },
        )(),
        # Second succeeds
        type(
            "R",
            (),
            {
                "json": lambda self: {"data": {"children": []}},
                "raise_for_status": lambda self: None,
            },
        )(),
    ]
    items = cp.fetch_reddit(subs=("nyc", "AskNYC"), limit=5, http=sess)
    assert items == []


# ---------------------------------------------------------------------------
# Trends fetcher — graceful fallback when pytrends absent
# ---------------------------------------------------------------------------


def test_fetch_trends_returns_empty_when_pytrends_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fail_import(name, *a, **kw):
        if name.startswith("pytrends"):
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fail_import)
    assert cp.fetch_trends(keywords=("brooklyn",)) == []


# ---------------------------------------------------------------------------
# RSS fetcher — graceful fallback when feedparser absent
# ---------------------------------------------------------------------------


def test_fetch_rss_returns_empty_when_feedparser_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fail_import(name, *a, **kw):
        if name == "feedparser":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fail_import)
    assert cp.fetch_rss(feeds={"x": "http://x"}) == []


# ---------------------------------------------------------------------------
# End-to-end pipeline shape
# ---------------------------------------------------------------------------


def test_snapshot_pipeline_with_all_mocked():
    now = datetime(2026, 4, 25, tzinfo=UTC)
    fake_items = [
        {
            "source": "reddit",
            "title": 'Tried "Bonnie\'s" last night',
            "text": "",
            "url": "u1",
            "seen_at": now,
        },
        {
            "source": "eater",
            "title": 'Eater: "Bonnie\'s" still the move',
            "text": "",
            "url": "https://ny.eater.com/x",
            "seen_at": now,
        },
        {
            "source": "trends",
            "title": "williamsburg",
            "text": "",
            "url": "u3",
            "seen_at": now,
        },
    ]
    with (
        patch.object(cp, "fetch_reddit", return_value=fake_items[:1]),
        patch.object(cp, "fetch_trends", return_value=fake_items[2:3]),
        patch.object(cp, "fetch_rss", return_value=fake_items[1:2]),
    ):
        payload = cp.snapshot()

    assert payload["city"] == "NYC"
    assert "generated_at" in payload
    assert payload["entity_count"] >= 1
    assert payload["source_counts"] == {"reddit": 1, "trends": 1, "eater": 1}
    # Entities sorted by confirmation desc
    confs = [e["confirmation"] for e in payload["entities"]]
    assert confs == sorted(confs, reverse=True)


def test_collect_filters_by_source():
    with (
        patch.object(cp, "fetch_reddit", return_value=[{"source": "reddit"}]),
        patch.object(cp, "fetch_trends", return_value=[{"source": "trends"}]),
        patch.object(cp, "fetch_rss", return_value=[{"source": "rss"}]),
    ):
        only_reddit = cp.collect(sources=("reddit",))
        assert all(i["source"] == "reddit" for i in only_reddit)
        assert len(only_reddit) == 1


# ---------------------------------------------------------------------------
# CLI smoke (argparse only — no actual fetches)
# ---------------------------------------------------------------------------


def test_main_runs_with_no_write_and_json(capsys):
    with patch.object(cp, "snapshot", return_value={"city": "NYC", "entities": []}):
        rc = cp.main(["--no-write", "--json"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert '"city": "NYC"' in captured


def test_main_human_view(capsys):
    fake = {
        "generated_at": "2026-04-25T00:00:00+00:00",
        "city": "NYC",
        "entity_count": 1,
        "source_counts": {"reddit": 1},
        "entities": [
            {
                "entity": "bonnie's",
                "category": "venue",
                "sources": ["reddit"],
                "mention_count": 1,
                "confirmation": 0.2,
                "momentum": "peaked",
                "saturated": False,
                "evidence": [],
            }
        ],
    }
    with patch.object(cp, "snapshot", return_value=fake):
        rc = cp.main(["--no-write"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "bonnie's" in out
    assert "NYC Pulse" in out
