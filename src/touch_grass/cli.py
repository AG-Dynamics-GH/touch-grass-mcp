"""touch-grass CLI — bootstrap config, run server, clean cache."""

from __future__ import annotations

import argparse
import sys

from touch_grass import __version__
from touch_grass.config import (
    config_exists,
    get_config_dir,
    get_data_dir,
    resolve_config_path,
    save_profile_dict,
)
from touch_grass.packs import PACKS


def cmd_init(args) -> int:
    """Interactive profile bootstrap. Use --city to fast-launch a known city pack."""
    from touch_grass.setup_wizard import collect_profile_interactive

    if config_exists() and not args.force:
        path = resolve_config_path()
        print(f"Config already exists at {path}")
        print("Use --force to overwrite, or edit it directly.")
        return 1

    print("touch-grass init — let's set up your profile.\n")
    try:
        config = collect_profile_interactive(city_hint=args.city)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Nothing saved.")
        return 1
    save_profile_dict(config)
    path = resolve_config_path()
    print(f"\n✓ Config written to {path}")
    print()
    print("Next steps:")
    print("  1. Configure API keys: touch-grass setup --keys-only")
    print("  2. Wire the MCP server into Claude Desktop / Claude Code:")
    print("     command: touch-grass")
    print("     args: ['serve']")
    return 0


def cmd_list_cities(args) -> int:
    """Print available city starter packs and their aliases."""
    print("Available city packs (auto-fill pulse defaults on init/setup):\n")
    for pack in sorted(PACKS.values(), key=lambda p: p.name):
        aliases = ", ".join(pack.aliases)
        scrapers = (
            f" — deep coverage ({len(pack.client_modules)} local scrapers)"
            if pack.client_modules
            else " — starter pack (pulse defaults only)"
        )
        print(f"  {pack.name.upper()} ({pack.state}){scrapers}")
        print(f"    aliases: {aliases}")
    print("\nUse `touch-grass init --city <alias>` or `touch-grass setup --city <alias>`")
    print("to fast-launch with that pack's defaults applied.")
    return 0


def cmd_serve(args) -> int:
    """Run the MCP server (stdio by default)."""
    from touch_grass.server import mcp

    if args.http:
        print("HTTP transport not yet wired in v0.1 — run with stdio (default).", file=sys.stderr)
        return 2

    mcp.run()
    return 0


def cmd_clean(args) -> int:
    """Purge cache files older than --days days."""
    import time

    cutoff = time.time() - (args.days * 86400)
    cache_dir = get_data_dir() / "cache"
    if not cache_dir.exists():
        print("No cache directory yet.")
        return 0

    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    print(f"Removed {removed} cache files older than {args.days} days.")
    return 0


def cmd_setup(args) -> int:
    """Unified onboarding: profile + city pack + API keys."""
    from touch_grass.setup_wizard import run_setup

    return run_setup(
        allow_unvalidated=args.allow_unvalidated,
        collect_profile=not args.keys_only,
        city_hint=args.city,
        force_profile=args.force,
    )


def cmd_doctor(args) -> int:
    """Sanity check: config exists, env vars set, paths writable."""
    print(f"touch-grass-mcp {__version__}\n")
    print(f"Config dir:  {get_config_dir()}")
    print(f"Data dir:    {get_data_dir()}")
    print(f"Config file: {resolve_config_path()}")
    print(f"  exists: {config_exists()}")

    import os

    keys = [
        "TICKETMASTER_API_KEY",
        "EVENTBRITE_API_KEY",
        "YELP_API_KEY",
        "NYC_OPENDATA_TOKEN",
    ]
    print("\nAPI keys:")
    for k in keys:
        present = "✓" if os.environ.get(k) else "✗"
        print(f"  {present} {k}")

    print(f"\nNYC impersonation flag: {os.environ.get('TOUCH_GRASS_NYC_IMPERSONATE', 'unset')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="touch-grass", description="touch-grass MCP server")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="bootstrap a new profile (no API keys)")
    p_init.add_argument("--force", action="store_true", help="overwrite existing config")
    p_init.add_argument(
        "--city",
        metavar="NAME",
        help="city alias for fast launch (e.g. 'san francisco', 'sf', 'austin')",
    )
    p_init.set_defaults(func=cmd_init)

    p_list_cities = sub.add_parser("list-cities", help="show available city starter packs")
    p_list_cities.set_defaults(func=cmd_list_cities)

    p_serve = sub.add_parser("serve", help="run the MCP server")
    p_serve.add_argument("--http", action="store_true", help="HTTP transport (not in v0.1)")
    p_serve.set_defaults(func=cmd_serve)

    p_clean = sub.add_parser("clean", help="purge old cache files")
    p_clean.add_argument("--days", type=int, default=30, help="age threshold in days")
    p_clean.set_defaults(func=cmd_clean)

    p_setup = sub.add_parser(
        "setup",
        help="unified onboarding wizard (profile + city pack + API keys)",
    )
    p_setup.add_argument(
        "--city",
        metavar="NAME",
        help="city alias for fast launch (e.g. 'san francisco', 'sf', 'austin')",
    )
    p_setup.add_argument(
        "--keys-only",
        action="store_true",
        help="skip profile collection; only configure API keys",
    )
    p_setup.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing profile during setup",
    )
    p_setup.add_argument(
        "--allow-unvalidated",
        action="store_true",
        help="offer to save keys even if live validation fails (e.g. behind a proxy)",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_doctor = sub.add_parser("doctor", help="sanity check config + env")
    p_doctor.set_defaults(func=cmd_doctor)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
