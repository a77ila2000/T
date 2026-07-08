# Claude Code Handoff - T Family Barcode

Date: 2026-07-08 KST
Repository: `https://github.com/a77ila2000/T`
Production URL: `https://preedgaonprime.vercel.app`
Main branch latest commit at handoff: `ecdb44a Use Redis as barcode cache source of truth`

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
- The page should clearly show which account and barcode type is currently refreshing.

## Runtime / Hosting

- Frontend: static HTML/CSS/JS under `public/`.
- Backend: Flask-compatible Vercel Python functions in `api/get_barcode.py`.
- Vercel rewrites expose:
  - `/api/get_barcode`
  - `/api/warm_next`
  - `/api/warm_done`
  - `/api/warm_status`
- GitHub Actions workflow: `.github/workflows/warm-barcodes.yml`
  - Cron: every 3 minutes.
  - Calls `/api/warm_next`, then `/api/get_barcode?...&warm=1`, then `/api/warm_done`.

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
- `api/warm_next.py`, `api/warm_done.py`, `api/warm_status.py`
  - Thin imports exposing the Flask app routes from `get_barcode.py`.
- `public/index.html`
  - Frontend cards, type tabs, cache polling, page-triggered warm worker, status banner.
- `public/style.css`
  - UI styling.
- `.github/workflows/warm-barcodes.yml`
  - Background warming from GitHub Actions.

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

Important recent fix:

Vercel serverless instances can have inconsistent in-memory globals. `warm_status` previously saw old in-memory cache while `/api/get_barcode?cache_only=1` hit another instance and returned 404. This was fixed in commit `ecdb44a` by removing in-memory cache reads from `get_cached_barcode()`. Redis should now be the source of truth for status and cache responses.

## Current Frontend Behavior

On page load:

- Creates three account cards.
- Defaults each card to `universe`.
- Fetches visible universe cache with `cache_only=1`.
- Starts `runWarmWorker()`.
- Polls visible caches every 30 seconds.
- Polls `/api/warm_status` every 15 seconds.

Status banner:

- Shows current refresh target as `{name} {barcode type} 갱신 중`.
- If due targets exist but no current lock, shows `{target} 갱신 필요 - 바로 시작합니다` and immediately calls `runWarmWorker()`.
- This replaced the confusing old text `자동 갱신 대기 1개`.

Stale barcode display:

- If `X-Barcode-Stale: 1`, the barcode image remains visible.
- Timer shows `만료됨`.
- Status shows last barcode is being displayed and sequential refresh is waiting.

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
```

After a successful refresh:

- `next_refresh_at = now + 20 minutes`

After a failed refresh:

- `next_refresh_at = now + 3 minutes`

The system is intentionally sequential:

- Only one warm lock at a time.
- Only one Browserless lock at a time.
- This avoids multiple T ID login/browser sessions fighting each other.

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
  - URL: `https://sktmembership.tworld.co.kr/mps/pc-bff/benefitbrand/list-tab1.do`

## Known Current Issues / Watch Points

1. Old cache entries saved before the 7-day retention fix may already be gone from Redis.
   - A barcode must succeed once after commit `eabdf7f` before it can remain visible after expiry.

2. Father universe barcode has been the persistent problem.
   - It repeatedly failed or had no stored cache in prior checks.
   - If this still fails, debug `/api/get_barcode?id=min560728&type=universe&debug=1`.

3. Browserless connection/timeouts have caused 502/504/423 errors.
   - 423 means another refresh/browser lock is active.
   - 502 often means login/barcode extraction failed.
   - 504 means request timed out.

4. GitHub Actions scheduled workflows may not run exactly every 3 minutes.
   - Frontend page worker also wakes warm refresh when the page is open.
   - If the page is not open, GitHub Actions is the background driver.

5. Do not rely on Python module globals for persistent state on Vercel.
   - Redis must remain the source of truth.

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

Manually ask scheduler for next target:

```powershell
$url='https://preedgaonprime.vercel.app/api/warm_next?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
curl.exe -s -S --max-time 30 $url
```

If `/api/warm_next` returns `ok`, do not abandon the token. Either call the returned target or release via `/api/warm_done`.

## Local Verification Commands

Run before commit:

```powershell
python -m py_compile api\get_barcode.py api\warm_next.py api\warm_done.py api\warm_status.py
node -e "const fs=require('fs');const html=fs.readFileSync('public/index.html','utf8');const m=html.match(/<script>([\s\S]*)<\/script>/); new Function(m[1]); console.log('JS_OK')"
if (Test-Path api\__pycache__) { Remove-Item -Recurse -Force api\__pycache__ }
git diff --check
```

## Recent Commit Timeline

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

## Last Observed Production State Before This Handoff

This was observed before `ecdb44a` had fully redeployed, so treat it as a snapshot, not final truth:

- Current refresh: `a77ila2000 general`
- `a77ila10004 universe`: cache existed and valid.
- `a77ila10004 general`: cache existed and valid, grade `GOLD`.
- `min560728 general`: cache existed and valid, grade `SILVER`.
- `min560728 universe`: no cache; still the main failing slot.
- `a77ila2000 universe/general`: warm status showed stale memory, but direct cache requests returned 404. This mismatch is the reason for commit `ecdb44a`.

After `ecdb44a` deploys, re-run the diagnostics above. Status and cache-only responses should agree because both use Redis.

