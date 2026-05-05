# Privacy

`touch-grass-mcp` is built around five hard commitments. They are enforced by code review, audit gates, and the absence of certain dependencies.

## 1. Zero telemetry. Ever.

No analytics, no usage stats, no error reporting service, no opt-in instrumentation. The codebase contains no Sentry, no PostHog, no Amplitude, no Mixpanel, no Segment, no Datadog, no Google Analytics, no anything. You can verify this with:

```bash
rg -i "sentry|posthog|amplitude|segment|mixpanel|datadog|@sentry|google-analytics|track\(" src/
```

That command should return zero matches. If it ever returns hits, file a security issue.

## 2. All data stays local.

Your profile, the events DB, the pulse cache, calibration logs — every byte the server reads or writes lives in:

- `~/.config/touch-grass/` — profile, API keys
- `~/.local/share/touch-grass/` — cache, state

Nothing syncs anywhere. Nothing phones home. Nothing leaves your machine unless you explicitly call a third-party API for source data (and even then, only the search query goes out).

## 3. No phone-home.

Outbound network calls are limited to the source APIs you explicitly enable. The full domain list:

- `app.ticketmaster.com` (events)
- `www.eventbriteapi.com` (events)
- `api.yelp.com` (restaurants)
- `api.meetup.com` (groups, optional)
- `api.openbrewerydb.org` (breweries)
- `api.weather.gov` (NWS forecast)
- `gist.ra.co` (Resident Advisor scrape)
- `dice.fm` (Dice scrape)
- `data.cityofnewyork.us` (NYC Open Data)
- Various NYC museum / library / venue sites (NYC pack only)
- `reddit.com` (pulse, optional)
- `trends.google.com` (pulse, optional)
- Editorial RSS feeds (pulse, optional)

That's it. The package will not contact any other host.

## 4. API keys never leave your machine.

Stored in `~/.config/touch-grass/.env` or shell env. Never committed. Never logged. Never transmitted to anyone but the API the key authenticates to.

## 5. Self-hosted by design.

No SaaS dependency. No managed service. No cloud feature. No "free tier" that becomes a paywall. The package runs entirely on your own machine.

## What we cannot promise

- **Source APIs may have their own privacy policies.** When you call Ticketmaster, you're sending Ticketmaster a query. We don't track what you search; they might. Read their terms.
- **Local files are subject to local risks.** If your laptop gets stolen and your home directory isn't encrypted, the profile leaks. Use FileVault / dm-crypt / BitLocker.
- **The package itself can change.** A future version could in theory add telemetry. The way to keep us honest is: read the diff before updating, and pin to a specific version.

## Threat model

`touch-grass-mcp` is designed to behave like a local CLI tool, not a web service. The trust we ask for is the trust you'd give to any pip-installed package: that we're not exfiltrating data. Verify it by reading the code; the codebase is intentionally small enough to audit.

If you find evidence of any of the above commitments being violated, please open a security issue immediately (see [SECURITY.md](SECURITY.md)).
