"""City pulse — cultural trend tracker.

Fuses Reddit, Google Trends, and editorial RSS into a unified momentum signal.
Used by the server to re-rank events alongside profile-based scoring.
"""

from touch_grass.pulse.reader import boost_score, get_signal, is_saturated, load_pulse, rerank

__all__ = ["load_pulse", "get_signal", "boost_score", "is_saturated", "rerank"]
