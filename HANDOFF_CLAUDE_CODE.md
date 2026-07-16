# Claude Code Handoff - T Family Barcode

Date: 2026-07-16 KST
Repository: `https://github.com/a77ila2000/T`
Production URL: `https://preedgaonprime.vercel.app`
Oracle fallback URL: `https://168-138-194-2.sslip.io`
Main branch latest commit at this handoff: `e52262e Fix the real root cause of 100% general-barcode failure: wrong T-World API endpoint`

**Biggest change this session: both the login/scrape work and the read API now run on the Oracle VM, not Vercel.** Vercel serves only static HTML/CSS/JS. The browser calls `https://168-138-194-2.sslip.io` directly, so page views do not invoke Vercel Functions. See "Oracle Worker Migration" and "Oracle Read API" below before changing the hosting split.

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

- Frontend: static HTML/CSS/JS under `public/`, served by both Vercel and Caddy on Oracle. The frontend uses the absolute Oracle API base URL; it does not call same-origin Vercel API routes.
- Read backend: `oracle/read_api.py`, a small Flask/Gunicorn service bound to `127.0.0.1:8080` on Oracle and exposed through Caddy HTTPS. It reads Upstash with one `MGET`, renders a cached SVG, and never imports Playwright or performs a login.
- Vercel: static files only. `.vercelignore` excludes `api/`, `oracle/`, and tests, while `vercel.json` contains only static response headers. This also makes stale cron-job.org calls to `/api/warm_tick` hit no Vercel Function.
- Legacy/fallback application code: `api/get_barcode.py` remains in the repository for reference and shared behavior, but it is no longer included in Vercel deployments.
- Shared pure logic: `api/barcode_core.py` is used by the Oracle refresh worker and read API.
- **Browser automation backend changed this session (2026-07-15)**: previously a paid Browserless.io cloud service; now a **self-hosted, open-source Browserless** (`browserless/chrome`, SSPL-1.0 license) Docker container on an Oracle Cloud "Always Free" ARM instance. See "Self-Hosted Browserless" below for full details and history.
- **Who performs login/scrape**: only `oracle/worker_tick.py`, run by a systemd timer every 20 seconds. The old Vercel scrape files remain in Git history/repository context but `.vercelignore` keeps them out of deployments.
- Oracle Caddy exposes `/api/get_barcode`, `/api/warm_status`, `/healthz`, and the static fallback page. There is deliberately no public refresh-trigger endpoint.
- **GitHub Actions workflow removed in the 2026-07-15 session** (`.github/workflows/warm-barcodes.yml` deleted, commit `1126119`). Its free-tier `on: schedule` cron was confirmed via `gh run list` to fire wildly irregularly (multi-hour gaps instead of the configured 3 minutes) and was fully redundant with cron-job.org below.
- **cron-job.org**: the old job may still request Vercel `/api/warm_tick`, but that route is no longer deployed and therefore cannot invoke a Vercel Function. It can be disabled at any time.
- The frontend never drives refreshes. It reads status/cache only; the Oracle timer owns all scheduling and refresh execution.

## Required Environment Variables

Do not commit secret values. These names must exist in `/opt/tworld-worker/.env` on Oracle:

- `ENCRYPTION_KEY`
- `ENCRYPTED_ACCOUNTS`
- `BROWSERLESS_TOKEN`
- `BROWSERLESS_WS_URL` (defaults to `ws://168.138.194.2:3000` in code - the current self-hosted ARM server)
- `BROWSERLESS_WS_URL_UNIVERSE` / `BROWSERLESS_TOKEN_UNIVERSE` (default to the same values as the non-universe ones - kept as separate env vars only because a previous, since-abandoned architecture briefly needed two different servers; see "Self-Hosted Browserless")
- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`

Upstash Redis is the cache and scheduler state source (sole source of truth - see "Redis as source of truth" below). Vercel no longer needs these variables because it deploys no Python Functions. On Oracle, `BROWSERLESS_WS_URL` is overridden to `ws://localhost:3000`.

## Oracle Read API (2026-07-16)

- `oracle/read_api.py` has no scrape path. `/api/get_barcode` performs one Redis `MGET`, returns the valid or last stale barcode as SVG, and exposes the existing `X-Barcode-*` headers for the unchanged frontend display logic.
- `/api/warm_status` reads current state, six scheduler states, six cache keys, and three legacy keys in one `MGET`, matching the optimized Vercel status response shape.
- Cross-origin response and exposed-header settings allow the Vercel static page to read Oracle directly without a proxy.
- `oracle/tworld-api.service` runs one Gunicorn worker with two threads on `127.0.0.1:8080`.
- `oracle/Caddyfile` terminates HTTPS for `168-138-194-2.sslip.io`, proxies `/api/*` and `/healthz`, and serves `public/` as the Vercel-independent fallback page.
- Oracle Cloud's security list and the Ubuntu host `iptables` rules must both allow inbound TCP 80 and 443 for Caddy certificate issuance and HTTPS traffic. Save host rules with `netfilter-persistent save` so they survive reboot. Browserless remains on port 3000 for the refresh worker.
- Deploy the unit to `/etc/systemd/system/tworld-api.service`, the Caddyfile to `/etc/caddy/Caddyfile`, then run `systemctl daemon-reload`, `systemctl enable --now tworld-api caddy`.

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

**Capability note**: Claude Code's Bash/PowerShell tools run directly on the operator's own PC and can read/write files anywhere on it (e.g. the SSH key folder) - there is no separate sandboxed filesystem. **Confirmed 2026-07-16**: SSH to the Oracle server (`ssh -i "C:\Users\MBJ\Desktop\서버키\ssh-key-2026-07-14.key" ubuntu@168.138.194.2`) works fine from this environment, including passwordless `sudo`, and was used extensively to deploy the Oracle worker - see "Oracle Worker Migration".

### SSO fast-path (tried, then fully reverted)

For a while this session, universe-type logins used a discovered shortcut: clicking the "구독중"/"인증대기" subscription badge (or the more stable "다음 결제일" text) on an already-logged-in tworld my-page triggers a popup that authenticates on `sktuniverse.co.kr` **without** the T-ID/recaptcha flow. This was fully implemented, iterated on (stealth-mode conflicts, server-contention issues), then **entirely reverted** (commit `26e1b9a`) once the new ARM server made the plain T-ID/recaptcha flow fast enough (~31-38s) to comfortably fit Vercel's 60s budget on its own. All related code (`login_state` helpers, the `warm_tick` 202/`pending_step2` branch, `/api/sso_debug`) was removed as dead code in commits `1126119` and `77a2bc9`. Mentioned here only so a future "why not just use the SSO shortcut again" idea has context on why it was abandoned (added complexity/fragility, not a functional dead end).

## Oracle Worker Migration (2026-07-16 session)

**Why**: Vercel Hobby's Fluid Active CPU free tier is 4 hours/month. The usage dashboard showed a daily-CPU chart climbing to 16-38 min/day, on pace to hit 5-8 hours/month - roughly double the free limit. The dominant remaining cost (after the cache-hit-path fixes below) is believed to be the actual Playwright login/scrape itself, which runs on Vercel ~432 times/day (6 targets x ~3 refreshes/hour, each up to 50-60s of Vercel wall time). The fix: run that scrape loop on the Oracle VM instead, which already hosts Browserless, has spare capacity, and isn't billed per-CPU-second.

**What changed**:

1. **`api/barcode_core.py`** (new) - all the pure Redis/locking/scheduling/scraping logic that used to live only in `get_barcode.py` was extracted here verbatim (no behavior change, verified via a full regression pass comparing outputs before/after). `get_barcode.py` now imports from it and keeps only the Flask routes, image rendering, and the Vercel-specific `SCRAPE_BUDGET_SECONDS`/`signal.alarm`/`mark()` 60s-kill workaround (which has no equivalent need on Oracle - see point 3).
   - **Gotcha hit during this split**: Vercel invokes `api/get_barcode.py` directly as the `/api/get_barcode` entrypoint, which does **not** put its own directory on `sys.path` for sibling imports - that's exactly why `warm_status.py`/`warm_tick.py` already had `sys.path.append(os.path.dirname(__file__))` before importing `get_barcode` as a module. `get_barcode.py`'s own new `from barcode_core import ...` needed the same fix (commit `f4679f5`) after a live 500 `FUNCTION_INVOCATION_FAILED` on `/api/get_barcode` right after the split deployed (`/api/warm_status` kept working the whole time since it inherits the fix via importing `get_barcode`).
2. **`oracle/worker_tick.py`** (new) - a standalone script, **not** deployed to Vercel (no `vercel.json` route), that does exactly what one `warm_tick()` call does: `acquire_warm_lock` -> `select_warm_target` (unchanged scheduling logic, same function from `barcode_core`) -> `acquire_browser_lock` -> scrape -> `set_cached_barcode`/`record_warm_result` -> exit. `perform_scrape()` mirrors `get_barcode.py`'s `perform_barcode_request()` scrape body step-for-step but connects to Browserless via `ws://localhost:3000` (no internet hop) instead of the public URL, and returns plain values instead of Flask responses.
   - **Deliberate design choice**: one-shot script re-run by a systemd **timer**, not a persistent `while True` process. A hung cycle is killed cleanly by an OS-level `timeout` wrapper rather than needing an in-process watchdog thread (unreliable for interrupting a stuck synchronous Playwright call from another thread), and a fresh process each tick avoids any long-running-process resource leak. This mirrors today's serverless "spin up, do one thing, exit" model, just relocated - deliberately **not** the "sleep until the next computed due time" shape some early proposals suggested, per this project's repeated bad experience with scheduling cleverness (see commit history around same-account chaining, below).
3. **Oracle server deployment** (`168.138.194.2`, SSH key `C:\Users\MBJ\Desktop\서버키\ssh-key-2026-07-14.key`, user `ubuntu` with passwordless `sudo`):
   - Dedicated non-login system user `tworld` (not `ubuntu`) owns everything under `/opt/tworld-worker/`.
   - `/opt/tworld-worker/repo` is a `git clone` of this same repo (so `barcode_core.py`/`worker_tick.py` update via `git pull`, not manual file edits) - as `tworld`: `git -C /opt/tworld-worker/repo pull --ff-only`.
   - `/opt/tworld-worker/venv` - Python venv with `cryptography` + `playwright` (client library only; no `playwright install chromium` needed since it connects to the already-running Browserless container, doesn't launch its own browser).
   - `/opt/tworld-worker/.env` (mode 600, owned by `tworld`) holds the 6 env vars listed above. **`vercel env pull` could not retrieve real values for the custom "Encrypted" vars** (system vars like `VERCEL_ENV` pulled fine, but `ENCRYPTION_KEY`/`ENCRYPTED_ACCOUNTS`/etc. consistently came back empty via CLI pull, cause not root-caused - possibly a permissions/team-scope quirk) - the user ended up providing `ENCRYPTION_KEY`/`ENCRYPTED_ACCOUNTS` directly, `BROWSERLESS_TOKEN` was recovered from the Browserless Docker container's own env (`sudo docker inspect browserless --format '{{range .Config.Env}}{{println .}}{{end}}'`), and `UPSTASH_REDIS_REST_URL`/`TOKEN` came from the Upstash console's "Connect > REST" tab. If this needs redoing, try that combination of sources rather than `vercel env pull` first.
   - `/etc/systemd/system/tworld-worker.service` (`Type=oneshot`, `ExecStart=timeout 100 /opt/tworld-worker/venv/bin/python3 /opt/tworld-worker/repo/oracle/worker_tick.py`, `EnvironmentFile=/opt/tworld-worker/.env`) + `tworld-worker.timer` (`OnUnitActiveSec=20s`) - both copied from `oracle/tworld-worker.service`/`oracle/tworld-worker.timer` in this repo, then `sudo systemctl daemon-reload && sudo systemctl enable --now tworld-worker.timer`.
   - Check it's alive: `sudo systemctl status tworld-worker.timer` and `sudo journalctl -u tworld-worker -f` (or `-n 40` for recent history).
4. **Verified live 2026-07-16**: manual single-cycle run succeeded end-to-end (real T-ID login, real barcode fetched, Redis updated, ~2.7s CPU consumed per `systemd`'s own accounting). After enabling the timer, it automatically caught and successfully refreshed multiple real targets with zero manual triggering (including one case of 3 consecutive `tworld_barcode_not_found` failures on `mother-general` self-healing via the pre-existing immediate-retry logic, then succeeding - normal transient-failure handling, not a new bug). Confirmed via `warm_status` that results written by the Oracle worker are immediately visible through Vercel's existing read path with zero frontend changes needed.
5. **Current state / what's NOT done yet**: cron-job.org (every 1 min) and Vercel's own `warm_tick` are **deliberately left running unchanged** as a parallel safety net - the existing global Redis locks (`warm_lock`/`browser_lock`) already make dual-writer operation safe (whichever fires first wins, no code change needed), confirmed live via repeated "warm lock busy - another tick (Vercel or this worker) is running, skipping" log lines showing both systems actively contending. **Remaining steps** (not yet done, needs a few days of parallel observation first): confirm via the Vercel usage dashboard that CPU hours actually dropped, then reduce cron-job.org's frequency (e.g. 1min -> 15min, kept as a reduced-but-present backstop) - do **not** strip any code from `get_barcode.py`/`vercel.json`, the savings come from invoking `warm_tick` less often, not from deleting the fallback path.

## Oracle Worker Bugfixes (2026-07-16, found live after the migration)

Within hours of the Oracle worker going live, the user reported a card visibly stuck on
"만료됨 (5:05 경과)" - general-barcode refresh had been failing on **every single attempt**
(46 failures, 0 successes via the API path, per a live journal review). Root-caused and fixed
via an external review + live verification against the actual running system (commit `e2431e0`
and a follow-up commit for the endpoint fix - see the commit timeline):

1. **The real root cause: wrong T-World membership API endpoint.** `fetch_tworld_membership_data()`
   called `/common/my/tmembership`, which **always 404s** - it isn't a real endpoint. T-World's
   own frontend calls `/api/v6/common/my/tmembership`, which additionally requires session
   headers (`Authorization`/`x-session-key` from the `TWM` cookie, `SessionUpdatedAt`/
   `x-session-updated` from the `SessionUpdatedAt` cookie, `x-referrer`) - without them it
   responds 401 "Some headers are missing". **Verified live 2026-07-16** by running all three
   variants inside a real authenticated session: old URL -> 404, new URL without headers ->
   401, new URL with the session headers -> 200 with a real `otbNum`/`expireSeconds` payload.
   This explains why general-barcode refresh had been failing 100% of the time via the API path
   since the Oracle worker went live - the one earlier "success" seen in testing came from the
   DOM-visible-extraction fallback, not the API, which masked how broken the API call was.
2. **Login fallback ladder fired blindly regardless of whether an earlier step already
   succeeded.** `submit_tid_credentials()`/`force_submit()` fire a sequence of fallback submit
   actions (DOM click, locator click, coordinate tap, force_submit's own click) with no check
   for "did we already navigate away". Confirmed live: the first DOM click succeeded and moved
   to the tworld my-page, but the code kept firing - force_submit's own loose "click the first
   big visible button" fallback landed on an unrelated "실시간 이용요금" link on the new page,
   breaking the already-successful login. Fixed by checking `"auth.skt-id.co.kr" not in
   safe_url(page)` before each subsequent fallback step, in both functions.
3. **Failure backoff was defeated by the early-lead window.** `select_warm_target()`'s 20s
   early-lead (meant for real ~20min success schedules) was also applied on top of failure
   backoff delays - a 30s backoff plus a 20s lead meant a failing target was retried after only
   ~10s, not 30s. Confirmed live as ~20s-interval retry loops once bug #2 started failing
   repeatedly. Early-lead now only applies when `consecutive_failures` is 0.
4. **Failure backoff is now bounded-exponential, not flat.** `FAILURE_BACKOFF_SCHEDULE = [0, 30,
   60, 120, 300]` (was: 0 then flat 30s forever) - still checks in every 5 minutes at worst
   during a sustained outage, instead of hammering a broken login every 30s indefinitely.
   `record_warm_result()`'s success path now explicitly resets `consecutive_failures` to 0
   (previously only implicitly cleared via `set_cached_barcode`'s own write, which doesn't
   always run before a recorded success - e.g. a cache-hit-without-rescrape success path).
5. **Oracle's idle-tick Redis usage cut from 5 commands to 1.** `oracle/worker_tick.py`'s
   `main()` used to acquire+release the global warm lock on every ~20s tick even when nothing
   was due, projected to exceed Upstash's free-tier monthly command quota at that polling
   frequency. Reordered to peek via `select_warm_target()` (1 MGET) *before* touching the lock;
   only acquires it (and re-verifies due-ness, guarding against the lock-free peek going stale)
   once something is actually due. The same peek-before-lock reorder was mirrored into Vercel's
   `warm_tick()` too, since cron-job.org's 1-minute polling has the identical (smaller-scale)
   waste.
6. **Lock TTLs now differ per caller.** `WARM_LOCK_TTL`/browser-lock TTL had been raised to a
   shared 130s to stop the Oracle worker's up-to-110s runs from outliving their own lock (a real
   double-scrape race risk) - but reusing that same 130s for Vercel meant a force-killed Vercel
   request (60s hard kill) would now take up to 130s to self-heal instead of its old, more
   appropriate ~75-90s. `acquire_warm_lock()`/`acquire_browser_lock()` now take an explicit `ttl`
   parameter: Vercel's call sites in `get_barcode.py` pass 75/90, Oracle's in
   `oracle/worker_tick.py` pass 130 (matching its own `WORKER_SCRAPE_BUDGET_SECONDS=90` +
   `timeout 110` systemd wrapper, with margin).
7. **Added `tests/` with real pytest tests** (`test_barcode_core.py`, `test_worker_tick.py`, 15
   tests, all mocked - no network/Redis/browser needed) covering every fix above by name, plus
   the earlier session's fixes (parse_seconds_left, RedisUnavailable, MGET dedup). Previously
   all verification this session was ad-hoc (typed inline during the session, not saved) - a
   fair gap the external review pointed out. Run with `pip install pytest && pytest tests/ -v`.
8. **Added `.github/workflows/test.yml`** - runs the pytest suite on every push/PR (not a
   scheduled cron, unlike the unreliable one removed earlier this project's history - see
   "Runtime / Hosting"). Cheap (a few seconds), catches an obvious regression before it reaches
   either Vercel or the Oracle server.

**Deliberately NOT applied this pass** (real points, lower urgency/higher risk, revisit only if
actually causing an observed problem):
- Atomic Redis "compare-and-delete" (Lua `EVAL`) for lock release, replacing the current
  GET-then-DEL (non-atomic - a TTL-expiry-timing coincidence could let a release delete a
  *different* process's newly-acquired lock). Real but low-probability now that TTLs have
  generous margin; adds real complexity (Upstash REST `EVAL`) to the most fragile subsystem.
- Replacing `submit_tid_credentials()`'s fixed 500-700ms waits with active polling for the
  login form's disappearance. The state-check fix above (item 2) already closed the *observed*
  failure; this would only shrink an already-small residual timing window further.
- Unifying Oracle's `perform_scrape()` and Vercel's `perform_barcode_request()` into one shared
  function - both still independently orchestrate the same `barcode_core` calls in the same
  order. Valid long-term cleanup, but a large structural change; the urgent fixes above should
  land and stabilize first.
- Full docs reorg (splitting this file into README/operations/history docs). Fixed the factual
  staleness this pass instead (date/commit/TTL values/polling-interval claims below) rather than
  restructuring.

## Session Fixes (2026-07-16, before the Oracle migration)

1. **Per-card timer/status freezing during active refresh** (commit `27cc7ac`): the card's own "만료됨 (M:SS 경과)" timer and status text were gated on `img.dataset.stale`, a DOM flag only set once an actual `fetchBarcode` response came back - with no tab-refocus event to force one, that meant the card froze at whatever text it had at the moment of local expiry for the entire refresh duration, while the status panel above (driven by the same `warm_status` poll) correctly ticked through "갱신 중 (M:SS 경과)" live. Fixed by deriving "is this due" from the live poll data (`isActive`/`dueTargets`/`remaining`) instead of the lagging DOM flag, in both `refreshWarmStatus()`'s per-card block and `updateVisibleCardTimers()`.
2. **Vercel CPU reduction, cache-hit path** (commits `83242b9`, `7acae8a`): `decrypt_accounts()` used to run unconditionally before the cache-hit fast path, `barcode_response()` re-rendered the Code128 PNG pixel-by-pixel on every single call even for an unchanged still-valid number, `open_barcode_view()`'s fallback loop called `get_body_text()` (a real CDP round-trip) twice per iteration for the same instant, and `warm_status()`'s legacy-key MGET fetched the same 3 account ids twice (WARM_TARGETS has 2 entries per account). All four fixed: decrypt deferred past the cache checks, PNG bytes cached in a module-level dict keyed by number (safe - pixels depend only on the digit string), `get_body_text()` calls deduped, legacy MGET deduped (19 -> 16 keys).
3. **`expireSeconds: 0` truthiness bug** (commit `822e09b`): `int(x or 20*60)` treats a real `0` (barcode exactly at its rotation boundary) the same as a missing field, silently turning it into 1200 - could make the early-lead poll (`poll_for_fresh_barcode`) think a stale cycle was already fresh and stop polling early. Added `parse_seconds_left()` (explicit `is None` check) in `barcode_core.py`, used everywhere `expireSeconds`/`seconds_left` gets parsed.
4. **Redis MGET failure indistinguishable from "no data"** (commit `822e09b`): `mget_padded()` used to turn a Redis outage into an all-None list, identical to "every key genuinely empty" - `select_warm_target()` would then see every target as overdue and launch a real login scrape purely because Redis hiccuped. Added `RedisUnavailable` (raised on failure), `warm_tick`/`warm_status` now fail closed (503, no guessing) instead.
5. **Frontend adaptive polling** (commit `822e09b`): `warm_status` was polled on a flat 5s interval around the clock. Replaced with a self-rescheduling chain (`nextStatusDelay()`/`scheduleNextStatusPoll()`): 3s while a refresh is active/queued, 5s when something's due within 60s, 25s otherwise, paused entirely while the tab is hidden (resumes via the existing `catchUpAfterHidden` refocus handler). Verified live: steady-state ~25.4s gaps between polls in a real deployed tab.
6. All of the above (items 2-5) were originally raised by an external ("Codex") code review the user pasted in as screenshots; each claim was independently re-verified against the actual current code (not taken on faith) before being adopted - two other suggestions from that same review (matching Browserless's `CONCURRENT` env var to actual usage, adding a unit-test/CI suite) were deliberately **not** applied as low-value or out-of-scope for that pass.

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
- Polls warm status on an **adaptive** schedule (not a flat 5s anymore, since 2026-07-16 - see "Session Fixes" below): 3s while active/queued, 5s when something's due within 60s, 25s otherwise, paused while the tab is hidden. Re-renders the live countdown every 1s from the last polled snapshot regardless.
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

Targets (`WARM_TARGETS`, now in `api/barcode_core.py` - see "Oracle Worker Migration") are the 6
account×type combinations. Key constants (updated 2026-07-16, see "Oracle Worker Bugfixes"):

```py
WARM_SUCCESS_INTERVAL = 20 * 60                    # real barcode validity
FAILURE_BACKOFF_SCHEDULE = [0, 30, 60, 120, 300]   # bounded exponential, indexed by consecutive_failures-1
WARM_LOCK_TTL = 130                                # Oracle-oriented default; Vercel passes ttl=75 explicitly at its call sites
BROWSER_LOCK_TTL_DEFAULT = 130                      # same story; Vercel passes ttl=90 explicitly
WARM_CURRENT_TTL = 90                               # Vercel's own "in progress" marker TTL; Oracle uses 130 inline in worker_tick.py
WARM_EARLY_LOGIN_LEAD_SECONDS = 20   # see "Early-Login-Lead" below - only applies when consecutive_failures==0
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

1. Vercel serverless: if a request gets hard-killed past its `maxDuration`, the `finally` block (lock release) never runs - self-heals via each lock's own TTL (Vercel passes `ttl=75`/`ttl=90` explicitly at its call sites; Oracle's worker uses the longer 130s default - see "Oracle Worker Bugfixes"). This is a known, accepted tradeoff, not a bug.
2. A single transient warm-tick failure was observed and confirmed self-healing via the existing immediate-retry-on-first-failure logic (`compute_failure_retry_delay`) during this session's monitoring pass - working as designed.
3. Browserless connection/timeouts can still surface as 502/504/423 (423 = another refresh already running; 502 = login/extraction failed; 504 = timed out) - expected occasional failure modes, handled by the retry/backoff logic.
4. Deferred (reviewed, agreed on scope, not yet implemented as of 2026-07-16 - see project memory `project_goto_page_hard_failure`): `goto_page()` in `barcode_core.py` currently swallows every navigation exception uniformly (timeouts, benign `net::ERR_ABORTED` redirect races, and genuine unrecoverable network failures all get the same "log and return None" treatment) - the only safety net is `mark()`'s overall elapsed-time budget, not per-navigation failure detection. Agreed direction: add an opt-in `required=True` param, classify hard failures via an **allowlist** (not denylist) of specific net error substrings (`ERR_NAME_NOT_RESOLVED`, `ERR_CONNECTION_REFUSED`, `ERR_CONNECTION_RESET`, `ERR_CONNECTION_CLOSED`, `ERR_CONNECTION_TIMED_OUT`, `ERR_INTERNET_DISCONNECTED`, `ERR_ADDRESS_UNREACHABLE`, `ERR_EMPTY_RESPONSE` - deliberately excluding `ERR_ABORTED`/`TimeoutError`, which are common and often benign in this OAuth redirect chain), and raise (not swallow) only for the two safest call sites: the very first `goto_page()` call right after a fresh CDP connect in each login path (`open_tid_from_my()`'s first hop to `MY_PAGE_URL`, and the `TWORLD_LOGIN_URL` goto in the general/tworld path) - a hard failure there strongly signals a systemic Browserless/network issue, not something a middle-hop retry would fix. Middle hops were deliberately excluded from scope since each is an independent top-level navigation that doesn't strictly depend on the prior hop succeeding.
5. ~~Bigger, deliberately-not-started idea: move the warm-refresh worker off Vercel entirely and run it as a persistent process directly on the Oracle ARM server~~ **Done 2026-07-16 - see "Oracle Worker Migration" above.** Kept here as a pointer since this bullet was the origin of that work.
6. **New watch point (2026-07-16)**: the Oracle worker and Vercel's `warm_tick` now run in parallel indefinitely until the observation period above concludes - if debugging a scheduling oddity, check `sudo journalctl -u tworld-worker` on the Oracle box in addition to `vercel logs`, since either system could have handled any given refresh.

## Recent Commit Timeline (this multi-day session, newest first)

- `e52262e` - Fix the real root cause of 100% general-barcode failure: wrong T-World API endpoint
- `e2431e0` - Fix general-barcode login corruption, failure-retry early-lead bypass, and Oracle Redis quota
- `85cae64` - Update HANDOFF_CLAUDE_CODE.md for the 2026-07-16 session
- `50a5b8d` - Add systemd unit files for the Oracle worker
- `c60a5d1` - Add oracle/worker_tick.py - single-tick scraper for the Oracle worker
- `f4679f5` - Fix ModuleNotFoundError breaking /api/get_barcode after the barcode_core split
- `98ee988` - Extract reusable scraping/scheduling logic into api/barcode_core.py
- `822e09b` - Fix expireSeconds=0 truthiness bug, fail closed on Redis outage, adaptive status polling
- `7acae8a` - Trim redundant CDP round-trips and MGET keys
- `83242b9` - Cut Vercel Fluid Active CPU on the hot cache-hit path
- `27cc7ac` - Fix per-card timer/status freezing during active refresh
- `420cebe` - Revert same-account chaining, keep the false-success bug fix
- `7d77fae` - Add per-request tagged logging to diagnose the chaining feature's real behavior
- `2712f9c` - Fix false-success bug that turned lock contention into a retry storm
- `c144df7` - Loosen the chain budget threshold and log the chain decision
- `a1e7129` - Chain the same-account sibling scrape into the same warm_tick request (reverted above)
- `a633aef` - Sync the card timer's stale-elapsed display with the status panel's
- `8f4d9d7` - Center the name column and widen it by ~5 characters for more breathing room
- `a0b6dd7` - Widen the status panel's name column so it doesn't crowd the 우주 column
- `c43e5ce` - Switch barcode to Code128 Set C, keep status-panel time flowing during refresh, grid-align columns
- `eaeae89` - Show live elapsed time next to expired barcodes, not just "만료됨"
- `1dd4e6b` - Remove dead 423-retry-queue code, harden MGET against short responses
- `3874889` - Update HANDOFF_CLAUDE_CODE.md for the warm_tick consolidation + MGET batching
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
python -m py_compile api\get_barcode.py api\barcode_core.py api\warm_status.py api\warm_tick.py oracle\worker_tick.py
node -e "const fs=require('fs');const html=fs.readFileSync('public/index.html','utf8');const m=html.match(/<script>([\s\S]*)<\/script>/); new Function(m[1]); console.log('JS_OK')"
pip install pytest ; pytest tests\ -v
if (Test-Path api\__pycache__) { Remove-Item -Recurse -Force api\__pycache__ }
git diff --check
```

(`tests/` added 2026-07-16 - see "Oracle Worker Bugfixes". Also runs automatically on push via `.github/workflows/test.yml`.)

## Last Observed Production State

**2026-07-16, later the same day** - after the "Oracle Worker Bugfixes" pass (see that section):
the earlier same-day entry below claiming the `mother-general` 3x-failure-then-success was
"existing retry logic working as designed, not a new bug" turned out to be **incomplete** - a
live journal review shortly after showed general-barcode refresh had actually been failing
**100% of the time via the API path** (46 failures, 0 API successes) due to the wrong-endpoint
bug described in "Oracle Worker Bugfixes" #1; the one success seen earlier came from the
DOM-visible-extraction fallback, which happened to mask how broken the API call was. After
deploying the endpoint fix + the login-fallback state-check fix:
- Live-tested the corrected endpoint directly (see "Oracle Worker Bugfixes" #1) - real 200
  response with a real `otbNum`/`expireSeconds` payload.
- Watched several real ticks post-fix: no more misdirected clicks (no "실시간 이용요금" or
  similar wrong-page navigations), general-barcode successes recorded via the real API path.
- All six slots showed `stale: false` with recent `last_success_at` timestamps after the fix.
- 15 new pytest tests added (`tests/`) covering every fix in this pass by name, all passing.

**2026-07-16, right after the Oracle worker first went live** (see the caveat above - the
general-barcode assessment in this entry was later found to be incomplete):

- All six slots healthy; `barcode_core.py` split verified via regression tests, then live-verified via a real successful scrape immediately after the sys.path hotfix deployed.
- Oracle worker (`tworld-worker.timer`, every 20s) confirmed automatically catching and successfully refreshing multiple real targets with zero manual triggering, including one self-healing transient-failure case (`mother-general`, 3x `tworld_barcode_not_found` then success - later found to actually be the wrong-endpoint bug, not ordinary transient-failure handling; see the entry above).
- Confirmed via `systemd`'s own accounting: ~2-3s CPU per successful scrape on the Oracle side.
- Confirmed via `warm_status` that Vercel's read path sees Oracle-worker-written results immediately, with zero frontend changes.
- Confirmed via repeated "warm lock busy" log lines on both sides that Vercel's `warm_tick` (still driven by cron-job.org every 1 min) and the Oracle worker coexist safely on the shared Redis locks, as designed.
- Not yet confirmed: actual Vercel CPU-hour reduction on the usage dashboard (needs a few days of data before/after to compare) - check this before reducing cron-job.org's frequency.

Earlier snapshot, 2026-07-15, during a 2-hour scheduled monitoring/cleanup pass, and again right after the refresh-path consolidation deploy:

- All six slots healthy across multiple checks; no 5xx errors or timeouts seen across several minutes of live log watching.
- Watched the early-login-lead mechanism succeed live in production twice (어머니 pair and 아버지 pair), each completing within ~40s of real expiry.
- Watched one transient warm-tick failure self-heal via the immediate-retry logic within ~40s.
- Confirmed no remaining references to the two terminated Oracle AMD servers anywhere in code.
- After the consolidation deploy: confirmed `/api/warm_next` and `/api/warm_done` now 404, `/api/warm_status`/`/api/warm_tick` still respond correctly, and a real browser load only fires 3 `get_barcode` requests (one per account) instead of 6 - verified via network log inspection, no console errors, barcode image and status panel both rendering correctly.

Treat this as a snapshot, not a guarantee - re-run the diagnostics above to check current state.
