"""Tests for touch_grass.pulse.reader.

Uses a tmp_path fixture to write fake city_pulse.json snapshots and verifies
load, lookup, scoring, rerank, and CLI behavior.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from touch_grass.pulse import reader as pr


def _make_payload(entities):
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "city": "NYC",
        "entity_count": len(entities),
        "entities": entities,
    }


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    """Write a minimal snapshot to a temp path and return that path."""
    payload = _make_payload(
        [
            {
                "entity": "carbone",
                "category": "venue",
                "sources": ["reddit", "eater", "curbed"],
                "mention_count": 5,
                "first_seen": "2026-04-20T00:00:00+00:00",
                "last_seen": "2026-04-25T00:00:00+00:00",
                "confirmation": 1.0,
                "momentum": "rising",
                "saturated": False,
                "evidence": [],
            },
            {
                "entity": "bonnie",
                "category": "venue",
                "sources": ["reddit"],
                "mention_count": 1,
                "first_seen": "2026-04-25T00:00:00+00:00",
                "last_seen": "2026-04-25T00:00:00+00:00",
                "confirmation": 0.2,
                "momentum": "peaked",
                "saturated": False,
                "evidence": [],
            },
            {
                "entity": "joe's pizza",
                "category": "venue",
                "sources": ["reddit", "timeout"],
                "mention_count": 4,
                "first_seen": "2026-04-20T00:00:00+00:00",
                "last_seen": "2026-04-25T00:00:00+00:00",
                "confirmation": 0.6,
                "momentum": "rising",
                "saturated": True,
                "evidence": [],
            },
        ]
    )
    p = tmp_path / "city_pulse.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# load_pulse
# ---------------------------------------------------------------------------


def test_load_pulse_returns_empty_when_missing(tmp_path):
    assert pr.load_pulse(tmp_path / "missing.json") == {}


def test_load_pulse_reads_valid_json(snapshot):
    payload = pr.load_pulse(snapshot)
    assert payload["city"] == "NYC"
    assert len(payload["entities"]) == 3


def test_load_pulse_returns_empty_on_corrupt_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert pr.load_pulse(bad) == {}


# ---------------------------------------------------------------------------
# get_signal — exact + substring lookup
# ---------------------------------------------------------------------------


def test_get_signal_exact_match(snapshot):
    payload = pr.load_pulse(snapshot)
    sig = pr.get_signal("carbone", payload=payload)
    assert sig is not None
    assert sig["entity"] == "carbone"


def test_get_signal_normalizes_possessive_and_articles(snapshot):
    payload = pr.load_pulse(snapshot)
    # "Bonnie's" should normalize to "bonnie" and find the entity
    assert pr.get_signal("Bonnie's", payload=payload) is not None
    # "The Carbone" should strip "the" and match
    assert pr.get_signal("The Carbone", payload=payload) is not None


def test_get_signal_substring_fallback(snapshot):
    payload = pr.load_pulse(snapshot)
    # "Bonnie's Williamsburg" should match "bonnie" via substring
    sig = pr.get_signal("Bonnie's Williamsburg", payload=payload)
    assert sig is not None
    assert sig["entity"] == "bonnie"


def test_get_signal_none_when_unknown(snapshot):
    payload = pr.load_pulse(snapshot)
    assert pr.get_signal("zzz unknown spot zzz", payload=payload) is None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_boost_score_high_for_clean_rising(snapshot):
    payload = pr.load_pulse(snapshot)
    # Carbone: confirmation=1.0, momentum=rising(1.0), not saturated → 1.0
    assert pr.boost_score("carbone", payload=payload) == 1.0


def test_boost_score_discounts_saturated(snapshot):
    payload = pr.load_pulse(snapshot)
    # Joe's: confirmation=0.6, rising(1.0), saturated(0.3) → 0.18
    score = pr.boost_score("joe's pizza", payload=payload)
    assert score == pytest.approx(0.18, abs=0.01)


def test_boost_score_zero_for_unknown(snapshot):
    payload = pr.load_pulse(snapshot)
    assert pr.boost_score("does not exist", payload=payload) == 0.0


def test_is_saturated(snapshot):
    payload = pr.load_pulse(snapshot)
    assert pr.is_saturated("joe's pizza", payload=payload) is True
    assert pr.is_saturated("carbone", payload=payload) is False
    assert pr.is_saturated("unknown spot", payload=payload) is False


# ---------------------------------------------------------------------------
# Rerank — list re-ordering with pulse_signal annotation
# ---------------------------------------------------------------------------


def test_rerank_orders_by_boost(snapshot):
    payload = pr.load_pulse(snapshot)
    items = [
        {"name": "Bonnie's", "id": 1},
        {"name": "Carbone", "id": 2},
        {"name": "Unknown Spot", "id": 3},
        {"name": "Joe's Pizza", "id": 4},
    ]
    out = pr.rerank(items, name_key="name", payload=payload)
    ids = [it["id"] for it in out]
    # Carbone (1.0) > Bonnie (0.12) > Joe's saturated (0.18 ... actually higher)
    # Actual order: carbone=1.0, joe's=0.18, bonnie=0.12, unknown=0.0
    assert ids[0] == 2  # Carbone first
    assert ids[-1] == 3  # Unknown last


def test_rerank_attaches_pulse_signal(snapshot):
    payload = pr.load_pulse(snapshot)
    out = pr.rerank([{"name": "Carbone"}], payload=payload)
    sig = out[0]["pulse_signal"]
    assert sig is not None
    assert sig["confirmation"] == 1.0
    assert sig["momentum"] == "rising"
    assert sig["saturated"] is False


def test_rerank_handles_missing_name_key(snapshot):
    payload = pr.load_pulse(snapshot)
    out = pr.rerank([{"name": "Carbone"}, {"other": "no match"}], payload=payload)
    # The item without a name should sort to the bottom with pulse_signal=None
    assert out[-1].get("pulse_signal") is None


# ---------------------------------------------------------------------------
# Snapshot age
# ---------------------------------------------------------------------------


def test_snapshot_age_hours_recent(snapshot):
    payload = pr.load_pulse(snapshot)
    age = pr.snapshot_age_hours(payload)
    assert age is not None
    assert age < 1.0


def test_snapshot_age_hours_none_when_no_timestamp():
    assert pr.snapshot_age_hours({"entities": []}) is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_main_no_snapshot(tmp_path, capsys):
    rc = pr.main(["--path", str(tmp_path / "nope.json")])
    assert rc == 1


def test_cli_venue_lookup_found(snapshot, capsys):
    rc = pr.main(["--path", str(snapshot), "--venue", "Carbone"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["found"] is True
    assert parsed["matched_entity"] == "carbone"


def test_cli_venue_lookup_not_found(snapshot, capsys):
    rc = pr.main(["--path", str(snapshot), "--venue", "zzz nothing"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["found"] is False


def test_cli_top_returns_sorted_rows(snapshot, capsys):
    rc = pr.main(["--path", str(snapshot), "--top", "3"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3
    boosts = [r["boost"] for r in rows]
    assert boosts == sorted(boosts, reverse=True)


def test_cli_rerank_stdin(snapshot, capsys, monkeypatch):
    items = [{"name": "Carbone"}, {"name": "Unknown"}]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(items)))
    rc = pr.main(["--path", str(snapshot), "--rerank-stdin"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["name"] == "Carbone"
    assert out[0]["pulse_signal"]["confirmation"] == 1.0


def test_cli_rerank_stdin_invalid(snapshot, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    rc = pr.main(["--path", str(snapshot), "--rerank-stdin"])
    assert rc == 2
