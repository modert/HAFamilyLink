# Session Keepalive Design

## Problem

Google session cookies expire after a period of inactivity. The Family Link
integration relies on these cookies to query the Google Families API. Without
periodic refreshes the session dies and the user must re-authenticate via the
noVNC browser flow — a disruptive manual process.

## Approach

Instead of trying to call internal Google token-refresh endpoints (which are
undocumented, change without notice, and trigger bot-detection), we mimic a
real browser session:

1. **Save full browser state** after interactive authentication — cookies,
   `localStorage`, and `sessionStorage` via Playwright's `storage_state()`.
2. **Periodically launch a headless Chromium** instance with that saved state,
   navigate to `families.google.com`, and let the browser + Google's JS handle
   all cookie refresh / rotation naturally.
3. **Extract the refreshed cookies** and updated storage state, then persist
   them back to encrypted shared storage.

This is the same mechanism a human would trigger by simply opening the Family
Link page in a browser tab that hasn't been used in a while.

## Why Playwright?

- Playwright is already a dependency for the interactive noVNC auth flow.
- Headless Chromium executes Google's JavaScript, which handles APISID rotation,
  SID refresh, and consent cookie management transparently.
- No reverse-engineering of Google's internal APIs is required.

## Refresh Schedule

| Condition | Interval |
|-----------|----------|
| Normal (no failures) | **4 hours** |
| After 1st failure | 1 hour |
| After 2nd failure | 2 hours |
| After 3rd+ failure | 4 hours |
| After 5 consecutive failures | Stops retrying — manual re-auth required |

On startup, if saved browser state exists, an immediate refresh runs to verify
the session is still valid.

## Resource Usage (per refresh cycle)

Each refresh cycle launches a short-lived headless Chromium process. Typical
timings observed on a Proxmox VM (4 vCPU, 4 GB RAM):

| Phase | Duration |
|-------|----------|
| Playwright start + browser launch | ~2-4 s |
| Page navigation + JS settle | ~5-8 s |
| Cookie extraction + state save | < 1 s |
| **Total per refresh** | **~8-15 s** |

Between refreshes (the other 3 h 59 m 45 s) the keepalive uses zero CPU and
negligible memory — there is no persistent browser process.

Peak RAM during a refresh is approximately 200-300 MB (Chromium process).
On a Raspberry Pi 4 (4 GB) expect somewhat longer launch times (~5-8 s) but
the same brief duty cycle.

### Monitoring

Performance metrics are collected for every refresh and exposed via:

- **`GET /api/keepalive/status`** — current state, last refresh time, failure
  count, and last refresh timing breakdown.
- **`GET /api/keepalive/perf`** — aggregated stats over the last 20 refreshes:
  average/min/max total time, average browser launch time, average navigation
  time, plus the full history array.
- **`GET /api/health`** — includes a `keepalive` section when active.
- **`POST /api/keepalive/refresh`** — manually trigger a refresh (requires API
  key).

## Session Expiry Detection

During each refresh the keepalive checks:

1. **URL redirect** — if navigating to `families.google.com` redirects to
   `accounts.google.com`, the session has expired.
2. **HTTP status** — 4xx/5xx responses indicate server-side session rejection.
3. **Empty cookies** — if no Google cookies are present after navigation,
   something is wrong.

Any of these conditions increment the failure counter and trigger the backoff
schedule. After 5 consecutive failures a log message indicates manual
re-authentication is needed.

## Storage

Browser state and cookies are encrypted at rest using Fernet symmetric
encryption, stored under `/share/familylink/`:

| File | Contents |
|------|----------|
| `cookies.enc` | Google cookies (used by HA integration) |
| `browser_state.enc` | Full Playwright storage state (cookies + localStorage) |
| `.key` | Fernet encryption key (permissions `0600`) |

Both the production and dev add-on instances share this directory, so cookies
refreshed by one instance are available to the other.

## Interaction with the HA Integration

The HA custom integration (`familylink`) polls the add-on's `GET /api/cookies`
endpoint at its configured `update_interval` (default: 60 minutes). The
keepalive ensures those cookies remain valid between polls. The integration
does not need to know about the keepalive mechanism — it simply sees fresh
cookies every time it asks.
