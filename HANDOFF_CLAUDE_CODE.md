# Claude Code Handoff - T Family Barcode

Date: 2026-07-08 KST
Repository: `https://github.com/a77ila2000/T`
Production URL: `https://preedgaonprime.vercel.app`
Main branch latest commit at this handoff: `0d680d7 Point the affiliate brand shortcut at the mobile T world page`

## Goal

This project shows family barcodes for three T IDs:

- `a77ila2000` - 나
- `a77ila10004` - 어머니
- `min560728` - 아버지

Each account can show two barcode types:

- `universe`: T Universe barcode from `https://m.sktuniverse.co.kr/my`
- `general`: T Membership barcode from `https://m.tworld.co.kr/v6/my?returnUrl=https://m.tworld.co.kr/v6/main`

The desired UX is:

- The page should usually show the latest successful barcode for all six slots.
- Only one barcode should refresh at a time.
- If one barcode is refreshing, all other already generated barcodes should remain visible.
- If a barcode expires after 20 minutes, keep showing the last successful barcode and mark it as expired / waiting / refreshing.
- The page should clearly show which account and barcode type is currently refreshing, and distinguish that from ones merely waiting their turn.
- Universe and general refreshes for the same account should not land at the exact same moment (kept at least 3 minutes apart).
- Barcodes should get refreshed within a minute or two of expiring even when nobody has the page open.

## Runtime / Hosting

- Frontend: static HTML/CSS/JS under `public/`.
- Backend: Flask-compatible Vercel Python functions in `api/get_barcode.py`.
- Vercel rewrites expose:
  - `/api/get_barcode`
  - `/api/warm_next`
  - `/api/warm_done`
  - `/api/warm_status`
  - `/api/warm_tick` (new - see "External Scheduling" below)
- GitHub Actions workflow: `.github/workflows/warm-barcodes.yml`
  - Cron: every 3 minutes, but this is unreliable (see Known Issues) - kept only as a backup.
  - Calls `/api/warm_next`, then `/api/get_barcode?...&warm=1`, then `/api/warm_done`.
- **cron-job.org (external, free)**: hits `/api/warm_tick` every 1 minute. This is now the primary background driver - see "External Scheduling".

## Required Environment Variables

Do not commit secret values. These names must exist in Vercel Production and Preview:

- `ENCRYPTION_KEY`
- `ENCRYPTED_ACCOUNTS`
- `BROWSERLESS_TOKEN`
- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`

Upstash Redis is the cache and scheduler state source.

## Important Files

- `api/get_barcode.py`
  - All login, barcode scraping/generation, Redis cache, lock, warm scheduler logic.
  - `perform_barcode_request(account_id, barcode_type, debug_mode=False, cache_only=False)` holds the actual login/scrape/cache logic; the Flask route `handler()` is now just a thin arg-parsing wrapper around it.
  - `record_warm_result(target, token, success, http_code="")` is the shared state-recording logic used by both `/api/warm_done` and `/api/warm_tick`.
- `api/warm_next.py`, `api/warm_done.py`, `api/warm_status.py`, `api/warm_tick.py`
  - Thin imports exposing the Flask app routes from `get_barcode.py`.
- `public/index.html`
  - Frontend cards, type tabs, cache polling, page-triggered warm worker, status banner, per-card active/queued status, tab-visibility catch-up.
- `public/style.css`
  - UI styling.
- `.github/workflows/warm-barcodes.yml`
  - Background warming from GitHub Actions (backup driver only, see Known Issues).
- Root-level `index.html` / `style.css` (repo root, **not** under `public/`) are stale leftovers from an earlier version (no type tabs, old shortcut links). Not served by Vercel (`public/` is), but do not edit them by mistake - they should probably be deleted in a future cleanup.

## Current Backend Behavior

### Cache keys

Barcode cache:

```text
barcode:{type}:{account_id}
```

Examples:

```text
barcode:universe:a77ila2000
barcode:general:min560728
```

Warm state:

```text
barcode:warm:{type}:{account_id}
```

Locks:

```text
barcode:warm-lock
barcode:warm-current
barcode:browserless-lock
```

### Cache retention

`set_cached_barcode()` stores successful barcode data in Redis for 7 days:

```py
LAST_BARCODE_RETENTION = 7 * 24 * 60 * 60
```

The barcode itself is still valid only for its service-provided countdown, usually 20 minutes. The longer Redis TTL is only to keep the last successful barcode visible while the next refresh is waiting or failing.

`set_cached_barcode()` now also:

- Checks the actual Redis `SET` result (`"OK"`) before recording a warm-state success. A silently failed write (timeout, transient error) is treated as a failure and scheduled for a quick retry (`WARM_FAIL_INTERVAL`, 3 minutes) instead of being wrongly marked successful for a full 20-minute cycle.
- Caps `next_refresh_at` at `now + min(actual_ttl, WARM_SUCCESS_INTERVAL)` rather than always `+20m`, so a barcode whose real countdown is shorter than 20 minutes gets refreshed before it goes stale instead of after.
- Staggers sibling barcode types (universe vs general for the same account) so they never land within `WARM_STAGGER_INTERVAL` (3 minutes) of each other. This **only ever pulls a refresh earlier**, never later - delaying past the barcode's own real expiry was a real bug we hit and fixed (see Known Issues history below).

### Valid vs stale barcode

`get_cached_barcode(..., allow_stale=True)` returns expired cache as stale.

`barcode_response()` sends these headers:

```text
X-Barcode-Number
X-Barcode-Seconds-Left
X-Barcode-Stale: 0 or 1
X-Barcode-Status: valid or stale
X-Membership-Grade: only for general barcode when known
```

### Redis as source of truth

Vercel serverless instances can have inconsistent in-memory globals. `warm_status` previously saw old in-memory cache while `/api/get_barcode?cache_only=1` hit another instance and returned 404. This was fixed in commit `ecdb44a` by removing in-memory cache reads from `get_cached_barcode()`. Redis is the source of truth for status and cache responses.

## Current Frontend Behavior

On page load:

- Creates three account cards.
- Defaults each card to `universe`.
- Fetches visible universe cache with `cache_only=1`.
- Starts `runWarmWorker()`.
- Polls visible caches every 30 seconds, warm status every 15 seconds.
- **New**: `visibilitychange` / `pageshow` / `focus` listeners immediately re-run the cache/status/warm-worker checks as soon as the tab becomes visible again, instead of waiting for the next (browser-throttled) interval tick. Background tabs get their `setInterval` timers throttled or paused by the browser, so without this a barcode could finish refreshing server-side minutes before the tab visibly catches up.

Status banner (`#warm-status-banner`):

- Shows current refresh target as `{name} {barcode type} 갱신 중`.
- If due targets exist but no current lock, shows `{target} 갱신 필요 - 바로 시작합니다` and immediately calls `runWarmWorker()`.

Per-card status (added this session):

- The card matching the currently-active warm lock shows a distinct, bold/red status line: `{barcode type}를 지금 갱신하고 있습니다...` with its loading spinner visible.
- Other cards that are due but waiting their turn show `대기열 N번째 - 순서를 기다리는 중입니다.`, where N is computed the same way the backend picks the next target (earliest `next_refresh_at` first).
- This applies both when a card has no cached image yet **and** when a card is showing a stale (expired) barcode image - the image itself is left untouched in the stale case, only the status line updates.
- Cards mid-fetch, or in a 423 lock-retry backoff window, are skipped so this polling doesn't clobber their own in-flight status text.

Stale barcode display:

- If `X-Barcode-Stale: 1`, the barcode image remains visible.
- Timer shows `만료됨`.
- Status line shows active/queued state per the previous section.

## Warm Scheduling

Targets are defined in `api/get_barcode.py`:

```py
WARM_TARGETS = [
    {"id": "a77ila2000", "type": "universe", "name": "me-universe"},
    {"id": "a77ila10004", "type": "universe", "name": "mother-universe"},
    {"id": "min560728", "type": "universe", "name": "father-universe"},
    {"id": "a77ila2000", "type": "general", "name": "me-general"},
    {"id": "a77ila10004", "type": "general", "name": "mother-general"},
    {"id": "min560728", "type": "general", "name": "father-general"},
]
WARM_SUCCESS_INTERVAL = 20 * 60
WARM_FAIL_INTERVAL = 3 * 60
WARM_STAGGER_INTERVAL = 3 * 60
```

After a successful refresh:

- `next_refresh_at = now + min(actual_ttl, WARM_SUCCESS_INTERVAL)`, then possibly pulled earlier (never later) to keep 3 minutes of separation from the sibling barcode type of the same account.

After a failed refresh (including a silently-failed Redis write):

- `next_refresh_at = now + WARM_FAIL_INTERVAL` (3 minutes).

`select_warm_target()` always picks whichever due target has the smallest `next_refresh_at` (most overdue / soonest-expiring first), tie-broken by `WARM_TARGETS` index.

The system is intentionally sequential:

- Only one warm lock at a time (`barcode:warm-lock`).
- Only one Browserless lock at a time (`barcode:browserless-lock`).
- This avoids multiple T ID login/browser sessions fighting each other. It also means if several barcodes become due around the same time, they're processed one at a time (each real scrape can take up to ~60s), so a multi-item backlog can take a few minutes to fully clear - this is expected, not a bug.

### `warm_done` no longer overwrites the schedule

`record_warm_result()` (shared by `/api/warm_done` and `/api/warm_tick`) used to unconditionally set `next_refresh_at = now + WARM_SUCCESS_INTERVAL` on every success. Since `warm_done` always runs *after* `set_cached_barcode()` in the same cycle, it was silently clobbering the ttl-aware, staggered value `set_cached_barcode()` had just computed. Fixed: on success it now only merges in `last_http_code` and leaves the rest of the warm state exactly as `set_cached_barcode()` left it.

## External Scheduling (new)

GitHub Actions' native `on: schedule` cron is documented by GitHub as best-effort, and was observed here firing only a handful of times across several hours instead of every 3 minutes as configured - so whenever nobody had the page open, expired barcodes could sit unrefreshed for hours.

Added `/api/warm_tick` (`GET`/`POST`, no params) which performs the *entire* cycle in one request/response - acquire the warm lock, pick the most-overdue due target via `select_warm_target()`, call `perform_barcode_request()` directly (in-process, no extra HTTP hop), then `record_warm_result()` to release the lock and record success/failure. Response shapes:

```json
{"status": "locked", "retry_after": 60}
{"status": "no_due", "now": 1234567890}
{"status": "done", "id": "...", "type": "...", "name": "...", "success": true, "http_code": 200, "next_refresh_at": 1234567890}
```

This lets a simple external URL-pinger (no scripting needed, no GitHub token to hand out) drive the whole system reliably. Currently configured: **cron-job.org**, free account, hitting `https://preedgaonprime.vercel.app/api/warm_tick` every 1 minute. Verified in production (2026-07-08): cron-job.org's history shows consistent per-minute `200 OK` hits, and `warm_status` showed real automatic refreshes happening with no browser tab open and without GitHub Actions involved.

`vercel.json` sets `api/warm_tick.py` `maxDuration: 60` (same as `get_barcode.py`, since it does the same amount of real scraping work per call).

GitHub Actions is left in place as a redundant backup driver; no need to remove it.

## Discount Display Rules

Universe benefits:

- 나: 파리바게뜨 30% 할인 / 월 3만원 한도
- 어머니:
  - 세븐일레븐 30% 할인 / 월 3만원 한도
  - 투썸플레이스 30% 할인 / 월 3만원 한도
  - CU 편의점 20% 할인 / 월 3만원 한도
- 아버지:
  - 파리바게뜨 30% 할인 / 월 3만원 한도
  - CU 편의점 20% 할인 / 월 3만원 한도

General barcode:

- Show T Membership grade from `X-Membership-Grade`, for example `VIP`, `GOLD`, `SILVER`.

Shortcut link:

- Only one shortcut remains:
  - Label: `제휴 브랜드 확인`
  - URL: `https://m.tworld.co.kr/membership/benefit/brand` (mobile page - the old URL was a PC-only `sktmembership.tworld.co.kr/mps/pc-bff/...` page, inconsistent with the rest of this mobile-only app).

## Known Current Issues / Watch Points

1. Root-level `index.html` / `style.css` (repo root, not `public/`) are stale duplicates from an old version. Not served in production, but confusing - candidate for deletion.
2. Browserless connection/timeouts have caused 502/504/423 errors.
   - 423 means another refresh/browser lock is active.
   - 502 often means login/barcode extraction failed.
   - 504 means request timed out.
3. GitHub Actions scheduled workflows do not reliably run every 3 minutes (see "External Scheduling" above) - this is now mitigated by cron-job.org hitting `/api/warm_tick` every minute, but GH Actions itself is still unreliable if that external cron is ever removed.
4. Do not rely on Python module globals for persistent state on Vercel.
   - Redis must remain the source of truth.
5. When multiple targets become due in a burst, they clear one at a time (single sequential worker) - can take a few minutes to fully settle. Expected, not a bug.

### Resolved this session (2026-07-08)

For context on what "the past" looked like, in case anything regresses:

- Silent Redis write failures being recorded as warm-success (fixed in `86ab4d4`).
- No per-card way to tell which of several waiting barcodes was actively refreshing (fixed in `820deb9`, extended to stale-image cards in `cd7b116`).
- Background/inactive tabs not catching up promptly on refocus (fixed in `9b24cd5`).
- Universe/general for the same account syncing to within seconds of each other, with no separation mechanism (added in `f4a95b4`).
- That same stagger logic could push a refresh *later* than the barcode's real expiry, leaving it stuck stale while `warm_status` said nothing was due (fixed in `b8421b0`).
- `warm_done` clobbering `set_cached_barcode`'s carefully computed schedule on every single successful refresh, silently undoing the stagger fix (fixed in `679228a`).
- GitHub Actions' unreliable cron leaving barcodes unrefreshed for hours with no page open (mitigated in `939e435` + external cron-job.org setup).
- Affiliate-brand shortcut linking to a PC-only page (fixed in `0d680d7`).

## Useful Diagnostics

Check warm state:

```powershell
$url='https://preedgaonprime.vercel.app/api/warm_status?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
curl.exe -s -S --max-time 30 $url
```

Check cache for all six slots:

```powershell
$ids=@('a77ila2000','a77ila10004','min560728')
$types=@('universe','general')
foreach($type in $types){
  foreach($id in $ids){
    $url='https://preedgaonprime.vercel.app/api/get_barcode?id=' + $id + '&type=' + $type + '&cache_only=1&t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    Write-Output "--- $id $type"
    curl.exe -L -s -S --max-time 20 -D - -o NUL $url | Select-String -Pattern 'HTTP/|X-Barcode|X-Membership'
  }
}
```

Manually ask scheduler for next target (does not perform the scrape itself):

```powershell
$url='https://preedgaonprime.vercel.app/api/warm_next?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
curl.exe -s -S --max-time 30 $url
```

If `/api/warm_next` returns `ok`, do not abandon the token. Either call the returned target or release via `/api/warm_done`.

Manually run one full warm cycle in a single call (what cron-job.org hits every minute):

```powershell
$url='https://preedgaonprime.vercel.app/api/warm_tick?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
curl.exe -s -S --max-time 70 $url
```

## Local Verification Commands

Run before commit:

```powershell
python -m py_compile api\get_barcode.py api\warm_next.py api\warm_done.py api\warm_status.py api\warm_tick.py
node -e "const fs=require('fs');const html=fs.readFileSync('public/index.html','utf8');const m=html.match(/<script>([\s\S]*)<\/script>/); new Function(m[1]); console.log('JS_OK')"
if (Test-Path api\__pycache__) { Remove-Item -Recurse -Force api\__pycache__ }
git diff --check
```

For backend logic changes, it's useful to unit-test `set_cached_barcode` / `record_warm_result` / `warm_tick` in isolation without needing real Flask/Playwright/cryptography installed, by stubbing those three modules in `sys.modules` before `import get_barcode`, then monkeypatching `get_barcode.redis_command` and `get_barcode.time.time` to fake values. See this session's conversation for worked examples (near-synced siblings, push-past-expiry regression check, warm_tick success/failure/no_due/locked paths).

## Recent Commit Timeline

- `0d680d7` - Point the affiliate brand shortcut at the mobile T world page
- `939e435` - Add a single-request warm_tick endpoint for reliable external cron
- `679228a` - Stop warm_done from clobbering set_cached_barcode's schedule
- `b8421b0` - Never let sibling staggering delay a refresh past real expiry
- `f4a95b4` - Stagger universe/general refresh schedules by at least 3 minutes
- `9b24cd5` - Re-sync immediately when the tab becomes visible again
- `cd7b116` - Distinguish active vs queued refresh on stale barcode cards too
- `820deb9` - Show per-card refresh state so waiting barcodes are distinguishable
- `86ab4d4` - Confirm Redis write before marking barcode warm success
- `787d2bd` - Add Claude Code handoff notes
- `ecdb44a` - Use Redis as barcode cache source of truth
- `1ff990a` - Clarify active refresh status
- `db82919` - Wake refresh worker when barcodes are due
- `eabdf7f` - Keep last barcodes visible during refresh
- `1e190a9` - Show warmup progress and refresh visible cache
- `409e8e7` - Start expiry warmup from page load
- `a4e79d6` - Fix warm endpoint imports
- `d480519` - Expose warm scheduler endpoints
- `f904cef` - Schedule barcode refreshes by expiry state
- `ec2e71a` - Warm universe barcodes every fifteen minutes
- `110d6ab` - Stabilize T Universe login refresh
- `c761848` - Show membership grade for general barcodes

## Last Observed Production State

Observed 2026-07-08 late evening, after all fixes above had deployed and cron-job.org had been running for a while:

- All six slots (`me`/`mother`/`father` x `universe`/`general`) had valid, non-stale cache.
- Universe/general `next_refresh_at` for every account was staggered by exactly 180s (3 minutes), as designed.
- cron-job.org history showed consecutive per-minute `200 OK` executions against `/api/warm_tick`.
- An automatic refresh (`me-general`) was observed happening with no browser tab open and without manually calling anything, confirming the external-cron path works end-to-end.

Treat this as a snapshot, not a guarantee - re-run the diagnostics above to check current state.
