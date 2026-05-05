# Security

## Reporting a vulnerability

If you find a security issue — anything that breaks the privacy commitments in [PRIVACY.md](PRIVACY.md), exposes credentials, or could harm a self-hoster — please report it privately.

**Email:** open a private security advisory via GitHub: https://github.com/AG-Dynamics-GH/touch-grass-mcp/security/advisories/new

Please don't open a public issue for security findings.

## Scope

In scope:
- The package itself (`src/touch_grass/`)
- The CLI (`touch-grass init`, `serve`, etc.)
- The MCP tool surface

Not in scope:
- Bugs in third-party APIs we wrap
- Issues with `pip` itself
- Vulnerabilities in optional NLP / pulse dependencies (file upstream with `spacy`, `pytrends`, `praw`)

## What we'll do

- Acknowledge within 72 hours
- Coordinate disclosure timeline
- Credit you in the changelog (if you want)

## Common gotchas

- **Don't share your `~/.config/touch-grass/.env`**. Treat it like any other secret file.
- **`TOUCH_GRASS_NYC_IMPERSONATE=true` is opt-in and ToS-questionable.** Some museum sites' terms forbid browser fingerprint forgery. Off by default.
- **API keys belong in env, not the JSON profile.** The JSON profile is your taste data; the env is your secrets. Keep them separated.
