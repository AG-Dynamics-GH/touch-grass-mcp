"""Unified onboarding wizard: profile + city pack + API keys.

Two phases:

1. Profile — interactively collect city, interests, dislikes, vibes,
   neighborhoods. Auto-fills pulse defaults from the matching city pack.
2. API keys — opens each provider's signup URL, validates the pasted key
   against the live API, saves to ~/.config/touch-grass/.env.

Each provider key requires manual signup; full automation isn't possible
because Terms of Service must be accepted by a human.
"""

from __future__ import annotations

import contextlib
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from touch_grass.config import (
    config_exists,
    empty_config,
    get_config_dir,
    resolve_config_path,
    save_profile_dict,
)
from touch_grass.packs import PACKS, CityPack, resolve_pack

ValidateResult = tuple[bool, str]
Validator = Callable[[str], ValidateResult]


@dataclass(frozen=True)
class Provider:
    env_var: str
    name: str
    signup_url: str
    instructions: str
    validate: Validator
    optional: bool = False


# ---------------------------------------------------------------------------
# Validators — each returns (ok, message). Network errors map to ok=False.
# ---------------------------------------------------------------------------


def _validate_ticketmaster(key: str) -> ValidateResult:
    try:
        r = httpx.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={"apikey": key, "size": 1, "city": "New York"},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
    if r.status_code == 200:
        return True, "OK"
    if r.status_code == 401:
        return False, "Invalid API key"
    return False, f"HTTP {r.status_code}"


def _validate_eventbrite(key: str) -> ValidateResult:
    try:
        r = httpx.get(
            "https://www.eventbriteapi.com/v3/users/me/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
    if r.status_code == 200:
        return True, "OK"
    if r.status_code == 401:
        return False, "Invalid token"
    return False, f"HTTP {r.status_code}"


def _validate_yelp(key: str) -> ValidateResult:
    try:
        r = httpx.get(
            "https://api.yelp.com/v3/businesses/search",
            params={"location": "New York", "limit": 1},
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
    if r.status_code == 200:
        return True, "OK"
    if r.status_code in (401, 403):
        return False, "Invalid API key"
    return False, f"HTTP {r.status_code}"


def _validate_nyc_opendata(key: str) -> ValidateResult:
    """Socrata tokens bump rate limits; we just verify the token isn't rejected."""
    try:
        r = httpx.get(
            "https://data.cityofnewyork.us/resource/m3xk-t3ki.json",
            params={"$limit": 1},
            headers={"X-App-Token": key},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
    if r.status_code == 200:
        return True, "OK"
    if r.status_code == 403:
        return False, "Token rejected"
    return False, f"HTTP {r.status_code}"


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        env_var="TICKETMASTER_API_KEY",
        name="Ticketmaster",
        signup_url="https://developer.ticketmaster.com/",
        instructions=(
            "1. Sign in (or create an account)\n"
            "2. Go to 'My Apps' and create a new app\n"
            "3. Copy the 'Consumer Key'"
        ),
        validate=_validate_ticketmaster,
    ),
    Provider(
        env_var="EVENTBRITE_API_KEY",
        name="Eventbrite",
        signup_url="https://www.eventbrite.com/platform/api",
        instructions=(
            "1. Sign in (or create an account)\n"
            "2. Click 'Create API Key' or open an existing one\n"
            "3. Copy the 'Private Token'"
        ),
        validate=_validate_eventbrite,
    ),
    Provider(
        env_var="YELP_API_KEY",
        name="Yelp Fusion",
        signup_url="https://docs.developer.yelp.com/",
        instructions=(
            "1. Sign in (or create an account)\n"
            "2. Go to 'Manage App' and create a new app\n"
            "3. Copy the API Key from the app page"
        ),
        validate=_validate_yelp,
    ),
    Provider(
        env_var="NYC_OPENDATA_TOKEN",
        name="NYC Open Data (Socrata)",
        signup_url="https://data.cityofnewyork.us/profile/edit/developer_settings",
        instructions=(
            "1. Sign in to data.cityofnewyork.us\n"
            "2. Go to your profile -> Developer Settings\n"
            "3. Click 'Create New App Token'\n"
            "4. Copy the App Token"
        ),
        validate=_validate_nyc_opendata,
        optional=True,
    ),
)


# ---------------------------------------------------------------------------
# .env file IO
# ---------------------------------------------------------------------------


def _read_env(env_path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Returns empty dict if file is missing."""
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    """Write a single key=value to .env, preserving other lines and comments."""
    from dotenv import set_key

    env_path.parent.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.touch(mode=0o600)
    set_key(str(env_path), key, value, quote_mode="never")


# ---------------------------------------------------------------------------
# Profile collection
# ---------------------------------------------------------------------------


def _csv(s: str) -> list[str]:
    return [t.strip() for t in s.split(",") if t.strip()]


def collect_profile_interactive(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    city_hint: str | None = None,
) -> dict[str, Any]:
    """Interactively collect a full user profile.

    If ``city_hint`` matches a known city pack, location and pulse defaults
    are auto-filled and the user is not asked for state. Returns a config
    dict ready for ``save_profile_dict``.
    """
    pack: CityPack | None = None
    state = ""

    if city_hint:
        pack = resolve_pack(city_hint)
        if pack:
            output_fn(f"✓ {pack.name.upper()} pack matched for '{city_hint}'.")
            city = city_hint
            state = pack.state
        else:
            available = ", ".join(sorted(PACKS.keys()))
            output_fn(f"No starter pack for '{city_hint}'. Available: {available}")
            output_fn("Continuing with manual entry.")
            city = city_hint
    else:
        output_fn(f"Available city packs: {', '.join(sorted(PACKS.keys()))}")
        city = input_fn("Your city (e.g. New York, San Francisco, Austin): ").strip()
        pack = resolve_pack(city)
        if pack:
            state = pack.state
            output_fn(f"✓ {pack.name.upper()} pack detected — state {state} auto-filled.")

    if not state:
        state = input_fn("State (2-letter, e.g. CA): ").strip().upper()
    zip_code = input_fn("ZIP code (optional): ").strip()

    output_fn("\nWhat do you like? Comma-separated keywords; leave blank to skip.")
    music = input_fn("  Music genres (jazz, indie rock, electronic): ").strip()
    activities = input_fn("  Activities (running, yoga, art galleries): ").strip()
    food = input_fn("  Food / drink (rooftop bars, wine, ramen): ").strip()

    output_fn("\nDislikes — surfaces less of these (optional, comma-separated).")
    dislike_music = input_fn("  Music genres to skip: ").strip()
    dislike_activities = input_fn("  Activities to skip: ").strip()

    output_fn("\nVibe preferences (optional, e.g. intimate, chill, creative, upscale).")
    vibes = input_fn("  Vibes: ").strip()

    fav_neighborhoods = input_fn("\nFavorite neighborhoods (optional, comma-separated): ").strip()
    avoid_neighborhoods = input_fn("Neighborhoods to avoid (optional, comma-separated): ").strip()

    config = empty_config()
    config["location"]["city"] = city
    config["location"]["state"] = state
    config["location"]["zip"] = zip_code

    profile = config["user_profile"]
    profile["interests"]["music_genres"] = _csv(music)
    profile["interests"]["activities"] = _csv(activities)
    profile["interests"]["food_and_drink"] = _csv(food)
    profile["dislikes"]["music_genres"] = _csv(dislike_music)
    profile["dislikes"]["activities"] = _csv(dislike_activities)
    profile["vibe_preferences"] = _csv(vibes)
    profile["neighborhoods"]["favorites"] = _csv(fav_neighborhoods)
    profile["neighborhoods"]["avoid"] = _csv(avoid_neighborhoods)

    if pack:
        config["pulse"]["reddit_subs"] = list(pack.pulse_defaults.reddit_subs)
        config["pulse"]["rss_feeds"] = list(pack.pulse_defaults.rss_feeds)
        config["pulse"]["trends_geo"] = pack.pulse_defaults.trends_geo
        output_fn(f"\n✓ Pulse defaults auto-filled from {pack.name.upper()} pack.")

    return config


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


def _ask(prompt: str, default_yes: bool, input_fn: Callable[[str], str]) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    answer = input_fn(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def run_setup(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    browser_fn: Callable[[str], Any] = webbrowser.open,
    env_path: Path | None = None,
    providers: tuple[Provider, ...] = PROVIDERS,
    allow_unvalidated: bool = False,
    collect_profile: bool = False,
    city_hint: str | None = None,
    force_profile: bool = False,
) -> int:
    """Run the unified onboarding wizard.

    Phase 1 (when ``collect_profile=True``): collect profile interactively
    and save to ``config.json``. Skipped if a config already exists unless
    ``force_profile=True``.

    Phase 2 (always): run the API key wizard.

    Returns 0 on completion, 1 on Ctrl-C. All side-effecting callables are
    injected for testability.

    If ``allow_unvalidated=True``, keys that fail validation can be saved
    anyway after a confirmation prompt — useful when validation endpoints
    are unreachable from the user's network (corporate proxy, DNS, etc.).
    """
    if env_path is None:
        env_path = get_config_dir() / ".env"

    if collect_profile:
        if config_exists() and not force_profile:
            output_fn(f"Profile already exists at {resolve_config_path()}.")
            output_fn("Skipping profile setup. Use --force to redo, or edit the JSON directly.\n")
        else:
            output_fn("=== Step 1/2: Profile ===\n")
            try:
                profile = collect_profile_interactive(
                    input_fn=input_fn, output_fn=output_fn, city_hint=city_hint
                )
            except KeyboardInterrupt:
                output_fn("\n\nInterrupted during profile setup. Nothing saved.")
                return 1
            save_profile_dict(profile)
            output_fn(f"\n✓ Profile saved to {resolve_config_path()}\n")

    existing = _read_env(env_path)

    if collect_profile:
        output_fn("=== Step 2/2: API keys ===\n")
    else:
        output_fn("touch-grass setup — guided API key configuration\n")
    output_fn(f"Writing keys to: {env_path}\n")
    output_fn("Each provider requires manual signup; full automation isn't possible")
    output_fn("(Terms of Service must be accepted by a human). This wizard opens")
    output_fn("the signup page, validates the key you paste, and saves it.\n")

    summary: list[tuple[str, str, str]] = []

    try:
        return _run_setup_loop(
            providers=providers,
            existing=existing,
            env_path=env_path,
            input_fn=input_fn,
            output_fn=output_fn,
            browser_fn=browser_fn,
            allow_unvalidated=allow_unvalidated,
            summary=summary,
        )
    except KeyboardInterrupt:
        output_fn(
            "\n\nInterrupted. Keys saved so far are kept; re-run `touch-grass setup` to resume."
        )
        _print_summary(output_fn, env_path, summary)
        return 1


def _run_setup_loop(
    *,
    providers: tuple[Provider, ...],
    existing: dict[str, str],
    env_path: Path,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    browser_fn: Callable[[str], Any],
    allow_unvalidated: bool,
    summary: list[tuple[str, str, str]],
) -> int:
    for provider in providers:
        output_fn(f"\n--- {provider.name} ---")
        existing_key = existing.get(provider.env_var, "").strip()
        replacing_invalid = False

        if existing_key:
            output_fn(f"  Existing key found ({len(existing_key)} chars). Validating...")
            ok, msg = provider.validate(existing_key)
            if ok:
                output_fn("  ✓ Valid — keeping it.")
                summary.append((provider.name, "ok", "kept existing"))
                continue
            output_fn(f"  ✗ Invalid: {msg}")
            if not _ask("  Replace it?", default_yes=False, input_fn=input_fn):
                summary.append((provider.name, "skipped", "kept invalid key"))
                continue
            replacing_invalid = True

        if not replacing_invalid:
            opt_label = " (optional)" if provider.optional else ""
            if not _ask(
                f"  Configure {provider.name}?{opt_label}",
                default_yes=not provider.optional,
                input_fn=input_fn,
            ):
                summary.append((provider.name, "skipped", "user choice"))
                continue

        output_fn(f"\n  Signup: {provider.signup_url}")
        output_fn("  Steps:")
        for line in provider.instructions.splitlines():
            output_fn(f"    {line}")

        if _ask("  Open signup URL in your browser?", default_yes=True, input_fn=input_fn):
            # Browser open is best-effort; user already has the URL printed.
            with contextlib.suppress(Exception):
                browser_fn(provider.signup_url)

        key = input_fn(f"  Paste your {provider.name} key (or 'skip'): ").strip()
        if not key or key.lower() == "skip":
            summary.append((provider.name, "skipped", "no key provided"))
            continue

        output_fn("  Validating against live API...")
        ok, msg = provider.validate(key)
        if not ok:
            output_fn(f"  ✗ Validation failed: {msg}")
            if allow_unvalidated and _ask(
                "  Save anyway (validation may have failed due to network)?",
                default_yes=False,
                input_fn=input_fn,
            ):
                _write_env_key(env_path, provider.env_var, key)
                output_fn(f"  ⚠ Saved unvalidated key to {env_path}")
                summary.append((provider.name, "ok", "saved unvalidated"))
                continue
            output_fn("  Not saving. Re-run `touch-grass setup` to retry.")
            summary.append((provider.name, "failed", msg))
            continue

        _write_env_key(env_path, provider.env_var, key)
        output_fn(f"  ✓ Validated and saved to {env_path}")
        summary.append((provider.name, "ok", "saved"))

    _print_summary(output_fn, env_path, summary)
    return 0


def _print_summary(
    output_fn: Callable[[str], None],
    env_path: Path,
    summary: list[tuple[str, str, str]],
) -> None:
    output_fn("\n=== Setup summary ===")
    icons = {"ok": "✓", "skipped": "—", "failed": "✗"}
    for name, status, detail in summary:
        output_fn(f"  {icons.get(status, '?')} {name}: {detail}")

    output_fn(f"\nKeys file: {env_path}")
    output_fn("Run `touch-grass doctor` to confirm everything's wired up.")
