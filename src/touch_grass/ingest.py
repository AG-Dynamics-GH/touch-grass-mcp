"""Background ingest of upcoming events into a local SQLite cache.

The MCP server fetches live data on each tool call. The cache layer is optional —
it speeds up repeated queries by warming a local index ahead of time.

For v0.1 this module is a stub. Run `touch-grass clean` to manage the cache;
proper bulk ingest will land in v0.2.

Schedule via cron, launchd, or systemd timer per OS.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub: bulk ingest is not yet implemented in v0.1."""
    print(
        "touch-grass ingest is not yet implemented in v0.1.\n"
        "The MCP server fetches data live on each tool call — no warm-up needed.\n"
        "v0.2 will add a local SQLite cache + scheduled refresh.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
