"""Guided API key setup wizard.

Each provider requires manual signup and ToS acceptance, so full automation
isn't possible. This wizard opens the signup URL, prompts for the resulting
key, validates it against the live API, and saves it to the user's .env.
"""

from __future__ import annotations

import contextlib
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from touch_grass.config import get_config_dir

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
) -> int:
    """Run the interactive setup wizard.

    Returns 0 on completion (even if user skipped everything), 1 on Ctrl-C.
    All side-effecting callables are injected for testability.

    If ``allow_unvalidated=True``, keys that fail validation can be saved
    anyway after a confirmation prompt — useful when validation endpoints
    are unreachable from the user's network (corporate proxy, DNS, etc.).
    """
    if env_path is None:
        env_path = get_config_dir() / ".env"

    existing = _read_env(env_path)

    output_fn("touch-grass setup — guided API key configuration")
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
