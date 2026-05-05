"""User profile loading. Re-exports from touch_grass.config for convenience."""

from __future__ import annotations

from touch_grass.config import load_profile_dict


class Profile:
    """Thin wrapper around the profile dict.

    For v0.1 this is just a dict with attribute access. Future versions
    may convert to a pydantic BaseModel for stricter validation.
    """

    def __init__(self, data: dict | None = None):
        self._data = data or load_profile_dict()

    def __getitem__(self, key: str):
        return self._data[key]

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def to_dict(self) -> dict:
        return self._data

    @property
    def user_profile(self) -> dict:
        return self._data.get("user_profile", {})

    @property
    def location(self) -> dict:
        return self._data.get("location", {})

    @property
    def pulse_config(self) -> dict:
        return self._data.get("pulse", {})


def load_profile(config_path=None) -> Profile:
    """Load profile from XDG-resolved location (or explicit path).

    For v0.1 the config_path argument is ignored — uses XDG resolution always.
    """
    return Profile()
