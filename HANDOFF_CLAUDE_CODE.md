# Claude Code Handoff - T Family Barcode

Date: 2026-07-15 KST
Repository: `https://github.com/a77ila2000/T`
Production URL: `https://preedgaonprime.vercel.app`
Main branch latest commit at this handoff: `7d73897 Consolidate refresh path to warm_tick, batch Redis reads, dedupe frontend polling`

## Goal

This project shows family barcodes for three T IDs:

- `a77ila2000` - 나
- `a77ila10004` - 어머니
- `min560728` - 아버지

Each account can show two barcode types:

- `universe`: T Universe barcode from `https://m.sktuniverse.co.kr/my` (T-ID/recaptcha login)
- `general`: T Membership barcode from `https://m.tworld.co.kr/v6/my?returnUrl=https://m.tworld.co.kr/v6/main` (direct tworld TID login, no recaptcha)

The desired UX:

- The page should usually show the latest successful barcode for all six slots.
- Only one barcode should refresh at a time (single global browser lock - see "Warm Scheduling").
- If one barcode is refreshing, all other already-generated barcodes should remain visible.
- If a barcode expires, keep showing the last successful barcode and mark it as expired/waiting/refreshing until the next one is ready.
- The page should clearly show which account+type is currently refreshing, distinguished from ones merely waiting their turn.
- **Barcodes must refresh as close to the moment of real expiry as possible** - both immediately when already expired, and (new this session) started slightly *before* expiry when it can shave real login/navigation time off the visible stale window. See "Warm Scheduling" for the hard constraint this operates under.

## Runtime / Hosting

- Frontend: static HTML/CSS/JS under `public/`. (Stale root-level `index.html`/`style.css` duplicates and an old `public/deploy-check.txt` artifact were deleted this session - `public/` was always the one actually served.)
- Backend: Flask-compatible Vercel Python functions in `api/get_barcode.py`.
- **Browser automation backend changed this session**: previously a paid Browserless.io cloud service; now a **self-hosted, open-source Browserless** (`browserless/chrome`, SSPL-1.0 license) Docker container on an Oracle Cloud "Always Free" ARM instance. See "Self-Hosted Browserless" below for full details and history.
- Vercel rewrites expose:
  - `/api/get_barcode`
  - `/api/warm_status`
  - `/api/warm_tick`
  - (`/api/warm_next` and `/api/warm_done` were **removed** this session - see "Refresh Path Consolidation" below)
- **GitHub Actions workflow removed this session** (`.github/workflows/warm-barcodes.yml` deleted, commit `1126119`). Its free-tier `on: schedule` cron was confirmed via `gh run list` to fire wildly irregularly (multi-hour gaps instead of the configured 3 minutes) and was fully redundant with cron-job.org below.
- **cron-job.org (external, free)**: hits `/api/warm_tick` every 1 minute. This is now the **sole external background driver**.
- The frontend also drives refreshes client-side while a tab is open (`runWarmWorker()`, every 3 minutes plus event-driven triggers) - see "Current Frontend Behavior".

## Required Environment Variables

Do not commit secret values. These names must exist in Vercel Production and Preview:

- `ENCRYPTION_KEY`
- `ENCRYPTED_ACCOUNTS`
- `BROWSERLESS_TOKEN`
- `BROWSERLESS_WS_URL` (defaults to `ws://168.138.194.2:3000` in code - the current self-hosted ARM server)
- `BROWSERLESS_WS_URL_UNIVERSE` / `BROWSERLESS_TOKEN_UNIVERSE` (default to the same values as the non-universe ones - kept as separate env vars only because a previous, since-abandoned architecture briefly needed two different servers; see "Self-Hosted Browserless")
- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`

Upstash Redis is the cache and scheduler state source (sole source of truth - see "Redis as source of truth" below).

## Self-Hosted Browserless (new this session)

**Why**: the previous Browserless.io paid cloud plan was being phased out; a self-hosted, open-source `browserless/chrome` Docker container on free-tier cloud VMs was set up instead.

**History** (in case anything about server capacity/behavior needs revisiting):

1. Two Oracle "Always Free" AMD `VM.Standard.E2.1.Micro` instances (1 OCPU/1GB each) were provisioned first - one per barcode type, since a single such box couldn't handle both without contention (confirmed: merging the two types onto one weak box made *both* start failing, not just the new one).
2. These AMD boxes were consistently too slow (~50-58s per universe login, dangerously close to Vercel's 60s hard kill).
3. Oracle's ARM "Always Free" tier (`VM.Standard.A1.Flex`, up to 2 OCPU/12GB total) was pursued instead via a community-known trick: upgrading to a Pay-As-You-Go (PAYG) billing account (with a $1 SGD budget alert as a safety net) improves free-tier ARM capacity availability without incurring charges, as long as usage stays within the Always Free limits.
4. A single ARM instance (2 OCPU/12GB, `168.138.194.2`, name `instance-20260714-1858`) was created and now runs **both** barcode types (`CONCURRENT=2` configured in the container) - no more need for two separate boxes.
5. **Important ARM64 gotcha**: the newer `ghcr.io/browserless/chromium` (v2) image has a documented bug (GitHub issue: hangs at "Launching ChromiumCDP Handler", ~30s timeout) on ARM64/Raspberry Pi 5-class hardware. The **older `browserless/chrome` (v1) image** is used instead and confirmed working.
6. The two original AMD instances (`193.123.162.16` "instance-20260713-0956" and `152.70.97.173` "instance-browserless-2") were **terminated** on 2026-07-15 now that everything runs on the single ARM box. No code references their IPs anymore (confirmed via grep).

**SSH access note**: the ARM server's key is `ssh-key-2026-07-14.key` in `C:\Users\MBJ\Desktop\서버키\` (confirmed working). A recovery keypair (`recovery-key` / `recovery-key.pub`) also exists in that folder from earlier troubleshooting of the (now-terminated) AMD server's lost SSH access - that issue is moot now that the AMD servers are gone.

**Capability note**: Claude Code's Bash/PowerShell tools run directly on the operator's own PC and can read/write files anywhere on it (e.g. the SSH key folder) - there is no separate sandboxed filesystem. SSH itself (actually connecting out to the Oracle servers) hasn't been exercised directly this way, but reading the key files to construct such a command is not blocked.

### SSO fast-path (tried, then fully reverted)

For a while this session, universe-type logins used a discovered shortcut: clicking the "구독중"/"인증대기" subscription badge (or the more stable "다음 결제일" text) on an already-logged-in tworld my-page triggers a popup that authenticates on `sktuniverse.co.kr` **without** the T-ID/recaptcha flow. This was fully implemented, iterated on (stealth-mode conflicts, server-contention issues), then **entirely reverted** (commit `26e1b9a`) once the new ARM server made the plain T-ID/recaptcha flow fast enough (~31-38s) to comfortably fit Vercel's 60s budget on its own. All related code (`login_state` helpers, the `warm_tick` 202/`pending_step2` branch, `/api/sso_debug`) was removed as dead code in commits `1126119` and `77a2bc9`. Mentioned here only so a future "why not just use the SSO shortcut again" idea has context on why it was abandoned (added complexity/fragility, not a functional dead end).

## Important Files

- `api/get_barcode.py`
  - All login, barcode scraping/generation, Redis cache, lock, warm scheduler logic.
  - `perform_barcode_request(account_id, barcode_type, debug_mode=False, cache_only=False, force_scrape=False)` holds the actual login/scrape/cache logic; the Flask route `handler()` is a thin arg-parsing wrapper around it.
  - `record_warm_result(target, token, success, http_code="")` records the outcome and releases the warm lock - called only by `warm_tick` now (see "Refresh Path Consolidation").
  - `poll_for_fresh_barcode(...)` - new this session, see "Early-Login-Lead" below.
- `api/warm_status.py`, `api/warm_tick.py`
  - Thin imports exposing the Flask app routes from `get_barcode.py`. Unchanged boilerplate. (`api/warm_next.py`/`api/warm_done.py` deleted this session along with the routes they exposed.)
- `public/index.html`
  - Frontend cards, type tabs, cache polling, page-triggered warm worker, per-person status detail panel, per-card active/queued status, tab-visibility catch-up.
- `public/style.css`
  - UI styling.
- Root-level `index.html` / `style.css` and `public/deploy-check.txt` - **deleted this session** (commit `77a2bc9`). They were stale, unserved leftovers; do not recreate them.

## Current Backend Behavior

### Cache keys

Barcode cache:

```text
barcode:{type}:{account_id}
```

Warm state:

```text
barcode:warm:{type}:{account_id}
```

Locks:

```text
barcode:warm-lock          (single global scheduler lock)
barcode:warm-current       (what warm_tick is currently working on, for status display)
barcode:browserless-lock   (single global browser-session lock)
```

Both locks are intentionally global (not per-account/type) - see "Warm Scheduling".

### Cache retention

`set_cached_barcode()` stores successful barcode data in Redis for 7 days (`LAST_BARCODE_RETENTION`). The barcode itself is only actually valid for its real ~20-minute countdown - the longer Redis TTL just keeps the last successful barcode displayable while the next refresh is pending or failing.

### Valid vs stale barcode

`get_cached_barcode(..., allow_stale=True)` returns expired cache as stale. `barcode_response()` sends:

```text
X-Barcode-Number
X-Barcode-Seconds-Left
X-Barcode-Stale: 0 or 1
X-Barcode-Status: valid or stale
X-Membership-Grade: only for general barcode when known
```

### Redis as source of truth

Redis is the sole source of truth for cache and scheduler state - there is no in-memory fallback (an in-memory `BARCODE_CACHE` dict existed but was write-only, never read; removed this session as dead code, commit `77a2bc9`).

## Current Frontend Behavior

On page load:

- Creates three account cards, defaults each to `universe`.
- Fetches visible universe cache with `cache_only=1` (`refreshVisibleCaches()`, called once).
- Starts `runWarmWorker()`.
- Polls warm status every 5s, re-renders the live countdown every 1s from the last polled snapshot.
- `visibilitychange`/`pageshow`/`focus` listeners re-run the cache/status/warm-worker checks when the tab becomes visible again, **debounced 350ms** (these three events can fire within milliseconds of each other for the same real "tab reactivated" moment - added this session after the redundant-request review below).
- `refreshWarmStatus()` tracks each target's `last_success_at` and immediately re-fetches the visible barcode image the moment it moves forward - this is now the **only** recurring re-fetch of the image (a prior session added this to close a lag bug; this session removed the then-redundant 30s `refreshVisibleCaches()` poll it made obsolete). Covers refreshes from this tab's own `runWarmWorker()` and ones triggered externally by cron-job.org equally.

### Redundant-request cleanup (new this session)

A review flagged several places the frontend was doing more network work than needed; verified and fixed:

- Page load used to call `fetchBarcode` for all 3 accounts **twice** (once directly, once via `refreshVisibleCaches()` right after) - the direct loop was removed.
- The recurring 30s `refreshVisibleCaches()` poll was removed entirely (see above) - it only ever mattered before the `last_success_at` watcher existed.
- `fetchBarcode()` now guards against firing a duplicate request for the same account+type while one is already in flight (a separate `inFlightFetches` Set, keyed `accountId:type` - distinct from the pre-existing `fetchingCards` Set, which is keyed by `accountId` alone and only used to avoid clobbering in-progress status text).

Status detail panel (`#warm-status-detail`):

- One row per person (나/어머니/아버지), 우주 and 일반 shown side by side.
- Each value: `갱신 중` (active warm-lock target), `대기 N번째` (due, queued behind N-1 others), a live `M:SS 남음` countdown ticking client-side between polls, or `만료됨` (briefly, only in the gap between the client's own countdown hitting 0 and the next server poll confirming it's due/queued).
- The old single-line `#warm-status-banner` above this panel was **removed** (commit `2b592eb`) as redundant - the per-person panel already conveys the same state, and the banner's real duty (triggering `runWarmWorker()` when something's due) is preserved silently inside `refreshWarmStatus()`.

Per-card status:

- The card matching the currently-active warm lock shows a distinct status line with its loading spinner visible.
- Other due-but-waiting cards show `대기열 N번째`.
- **Background cache polls no longer flash a fake "생성 중" state** (commit `26494d3`): every `fetchBarcode()` call used to unconditionally show the loader/disable buttons/set "실시간으로 생성 중입니다..." even for `cacheOnly` polls that never actually scrape anything (every current caller passes `cacheOnly: true`) - this made a perfectly healthy, non-due barcode look like it was being re-scraped every ~30s. Now gated on `preserveDisplay` (already false on first-load/empty-cache fetches, so those still show the loading state correctly).
- Dead frontend option plumbing (`auto`/`manual`/`quietMiss` fields threaded through `fetchBarcode()`/`enqueueBarcode()`) was removed this session (commit `77a2bc9`) - none of the current call sites ever set `auto`/`manual` to `true`, so those branches were unreachable, and `quietMiss` was never read at all.

## Warm Scheduling

Targets (`WARM_TARGETS` in `api/get_barcode.py`) are the 6 account×type combinations. Key constants:

```py
WARM_SUCCESS_INTERVAL = 20 * 60      # real barcode validity
WARM_FAIL_INTERVAL = 30              # 2nd+ consecutive failure backoff
WARM_LOCK_TTL = 75                   # self-heal window if Vercel hard-kills a request
WARM_CURRENT_TTL = 90
WARM_EARLY_LOGIN_LEAD_SECONDS = 20   # see "Early-Login-Lead" below
```

**Hard constraint (do not violate)**: the site will not renew a barcode's ~20-minute validity early - requesting before real expiry just returns the still-current barcode. `next_refresh_at` must always be computed from the real `seconds_left` the site actually reports, never pulled artificially earlier "for staggering purposes." **A fixed epoch-aligned stagger was tried and fully reverted this session** (commits `fe91ea3` then `79f1dcf`) after this was clarified - it scheduled attempts ~2 minutes before real expiry, which is pure wasted work since the site just returns the same code. Any future temptation to "spread out the 6 targets on a shared clock" should be resisted for this reason.

Staggering across **different people** instead falls out naturally from sequential processing (only one browser session at a time - see locks below): each real refresh lands at whatever wall-clock moment it's actually due, and since `next_refresh_at` is always "this target's own last real success + its own real ttl," that natural offset persists indefinitely.

**Universe and general for the *same* person do not stagger this way** - confirmed empirically (three separate accounts, repeated checks) that scraping them even ~40s apart in wall-clock time still produces expiry timestamps within 1-2 seconds of each other. The two site API endpoints (`/etc/barcode/data` for universe, `/common/my/tmembership` for general) appear to report time remaining on one shared per-account rotation, not independent 20-minute windows starting from whenever each is queried. A user cross-check on the real official app later suggested this might not be a literally-shared clock (saw both +20s and -20s differences when switching between the two screens), which is also fully consistent with "shared clock, but the two screens were checked at different real moments a few seconds/tens-of-seconds apart" - this was discussed at length but not conclusively resolved either way. **Practical takeaway regardless of root cause**: because they become due at nearly the same time, the queue naturally processes them back-to-back (universe then general, ~30-60s apart) rather than leaving one to rot - this is already close to the best achievable outcome without either (a) real per-type independence to exploit, or (b) running two Browserless sessions in parallel (see "Deferred: parallel same-account refresh" below).

Only one warm lock (`barcode:warm-lock`) and one browser lock (`barcode:browserless-lock`) exist, both global (not per-account/type) - deliberately, after contention on the old weak AMD hardware once caused *both* barcode types to start failing when merged onto one server. `select_warm_target()` always picks the most-overdue (or soonest-to-become-due, see below) target first.

### Early-Login-Lead (new this session, currently active)

Since universe/general logins spend ~20-38s on navigation/recaptcha *before* they can read the barcode value, and the real barcode has usually already expired by the time that login finishes if triggered exactly at the due moment, `select_warm_target(now, lead_seconds=WARM_EARLY_LOGIN_LEAD_SECONDS)` lets a target be picked up to 20 seconds **before** its real `next_refresh_at`. `warm_tick` detects this (`is_early = next_refresh_at > now`) and passes `force_scrape=True` into `perform_barcode_request()`, which:

1. Skips the normal "cache is still valid, just return it" short-circuit (only for this deliberately-early case - manual/`cache_only` requests are unaffected).
2. After reading the barcode value, calls `poll_for_fresh_barcode()`: if the value still looks like the tail end of the old cycle (`seconds_left < 300`), it waits 2s and re-polls the *same already-logged-in page* (no new login) up to the scrape budget, until the real rotation is observed or time runs out.

This does **not** increase scrape frequency (still one attempt per ~20min cycle, just started slightly sooner) and was confirmed live in production this session shaving the same-person universe+general pair down to completing within ~40s of real expiry rather than sitting stale for the whole login duration.

**This feature was briefly reverted and re-applied within the same session** (commits `88fdd92` → `74bcab5` revert → `68d494d` re-apply) after a "barcode never updates" bug report - the actual root cause turned out to be the separate frontend display-lag bug described above (status panel updating on a 5s cycle while the image lagged 30s behind), not this feature. Once that was fixed, the user confirmed the early-login-lead mechanism itself was working well and wanted it kept.

### Deferred: parallel same-account refresh

Explicitly considered and **deferred** (not implemented): running universe and general for the same person as two truly concurrent Browserless sessions (instead of sequentially through the single global lock) to eliminate the residual ~20-40s gap where the second-in-queue type is still waiting. Would require relaxing the global browser lock to a 2-slot semaphore and a way to fan out two concurrent triggers from a single per-minute external cron tick (nontrivial on Vercel's serverless model - a function ends when it responds). Given the current server (`CONCURRENT=2` configured) has the raw capacity, this is *possible* but was judged not worth the risk relative to the modest remaining benefit, especially given this exact area (scheduling/concurrency) has produced two real bugs this session already. Revisit only if the current sequential gap becomes an actual observed problem.

## Refresh Path Consolidation (new this session)

Previously there were **two** independently-implemented paths that did the same thing: cron-job.org hitting `/api/warm_tick` (single request, does everything server-side), and the frontend's `runWarmWorker()` doing a 3-step dance (`warm_next` → `get_barcode?...&warm=1` → `warm_done`). A follow-up review (external code analysis, verified against the actual code before acting on it) pointed out the client path's middle step returned a barcode image that was **immediately discarded** (`await response.blob()` then thrown away) and re-fetched via a separate `cache_only` call anyway - so the 3-step flow was strictly more round trips for the same outcome.

Fixed: `runWarmWorker()` now calls `/api/warm_tick` directly, same as cron-job.org. `warm_next()`/`warm_done()` routes, `api/warm_next.py`/`api/warm_done.py`, and their `vercel.json` entries are all removed. `record_warm_result()` is now called only by `warm_tick`. There is exactly one refresh code path now, used by both triggers.

Also batched in the same pass: `select_warm_target()` and `warm_status()` used to loop over the 6 `WARM_TARGETS` doing one Redis `GET` per target (up to 13 sequential round trips for `warm_status`, which the frontend polls every 5s per open tab) - both now do a single `MGET` instead.

## Known Current Issues / Watch Points

1. Vercel serverless: if a request gets hard-killed past its `maxDuration`, the `finally` block (lock release) never runs - self-heals via each lock's own TTL (`WARM_LOCK_TTL=75`, browser lock `90s`). This is a known, accepted tradeoff, not a bug.
2. A single transient warm-tick failure was observed and confirmed self-healing via the existing immediate-retry-on-first-failure logic (`compute_failure_retry_delay`) during this session's monitoring pass - working as designed.
3. Browserless connection/timeouts can still surface as 502/504/423 (423 = another refresh already running; 502 = login/extraction failed; 504 = timed out) - expected occasional failure modes, handled by the retry/backoff logic.
4. Deferred (reviewed, not applied): staging `submit_tid_credentials()`'s login-button submission more defensively (check between each attempt instead of firing several unconditionally once past the first early-exit check), and distinguishing required-vs-optional `goto_page()` navigations so a failed required hop fails fast instead of relying solely on the overall `mark()` time budget to catch it. Both touch the most fragile, most-tuned part of the codebase for a comparatively small payoff - revisit only if actually causing observed failures.
5. Bigger, deliberately-not-started idea: move the warm-refresh worker off Vercel entirely and run it as a persistent process directly on the Oracle ARM server (which already hosts Browserless) - would eliminate the CDP-over-network hop, Vercel's 60s budget pressure, the external cron dependency, and the Redis-based distributed locking, since a single local process naturally serializes without needing any of that. Real potential, but a separate architecture project, not a quick patch.

## Recent Commit Timeline (this multi-day session, newest first)

- `7d73897` - Consolidate refresh path to warm_tick, batch Redis reads, dedupe frontend polling
- `159ab21` - Bring HANDOFF_CLAUDE_CODE.md up to date with the full 2026-07-13/15 session
- `77a2bc9` - Remove dead code and stale files found during scheduled monitoring pass
- `26494d3` - Stop flashing the loading/generating status during quiet background cache polls
- `68d494d` - Reapply early-login-lead after confirming the real bug was elsewhere
- `f2648a1` - Sync visible barcode image as soon as warm_status shows a fresh refresh
- `74bcab5` - Revert early-login-lead (temporarily, pending investigation)
- `88fdd92` - Start warm refresh up to 20s before real expiry, poll in-place for rotation
- `2b592eb` - Remove redundant status banner - the per-person detail panel already covers it
- `1126119` - Remove unreliable GitHub Actions cron and dead SSO-era code
- `92586f5` - Group status panel by person, live-tick the countdown client-side
- `79f1dcf` - Revert to real-expiry-only scheduling - never attempt early refresh
- `65794e5` - Add a per-barcode status panel showing remaining time / refresh queue
- `fe91ea3` - Stagger the 6 targets onto fixed epoch-aligned slots (reverted above)
- `26e1b9a` - Revert to single-shot T-ID login now that the server has real headroom
- `9e8a70b` - Consolidate onto a single, more capable ARM server (2 OCPU/12GB)
- `71b34e1` through `81d7038` - SSO fast-path implementation and iteration (fully reverted)
- `176150a`, `aacaae7` - Two-server split / two-step universe login (superseded by the ARM consolidation)

Earlier history (2026-07-08 and before) is in git log; see previous handoff revisions if needed.

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

Manually run one full warm cycle in a single call (what cron-job.org hits every minute):

```powershell
$url='https://preedgaonprime.vercel.app/api/warm_tick?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
curl.exe -s -S --max-time 70 $url
```

## Local Verification Commands

Run before commit:

```powershell
python -m py_compile api\get_barcode.py api\warm_status.py api\warm_tick.py
node -e "const fs=require('fs');const html=fs.readFileSync('public/index.html','utf8');const m=html.match(/<script>([\s\S]*)<\/script>/); new Function(m[1]); console.log('JS_OK')"
if (Test-Path api\__pycache__) { Remove-Item -Recurse -Force api\__pycache__ }
git diff --check
```

## Last Observed Production State

Observed 2026-07-15, during a 2-hour scheduled monitoring/cleanup pass, and again right after the refresh-path consolidation deploy:

- All six slots healthy across multiple checks; no 5xx errors or timeouts seen across several minutes of live log watching.
- Watched the early-login-lead mechanism succeed live in production twice (어머니 pair and 아버지 pair), each completing within ~40s of real expiry.
- Watched one transient warm-tick failure self-heal via the immediate-retry logic within ~40s.
- Confirmed no remaining references to the two terminated Oracle AMD servers anywhere in code.
- After the consolidation deploy: confirmed `/api/warm_next` and `/api/warm_done` now 404, `/api/warm_status`/`/api/warm_tick` still respond correctly, and a real browser load only fires 3 `get_barcode` requests (one per account) instead of 6 - verified via network log inspection, no console errors, barcode image and status panel both rendering correctly.

Treat this as a snapshot, not a guarantee - re-run the diagnostics above to check current state.
