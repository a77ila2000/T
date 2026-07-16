import os, json, base64, time, re
import urllib.request, urllib.error
from cryptography.fernet import Fernet

# Pure scheduling/Redis/scraping logic shared between the Vercel Flask app
# (api/get_barcode.py) and the Oracle worker (oracle/worker_tick.py). Nothing
# here imports Flask, Pillow, or Playwright itself - functions that touch a
# Playwright `page` take it as a parameter, they don't create the browser
# connection (that's each caller's own job, since Vercel connects to
# Browserless remotely while the Oracle worker connects to it over localhost).

ENCRYPTION_KEY_B64 = os.environ.get("ENCRYPTION_KEY")
ENCRYPTED_ACCOUNTS_B64 = os.environ.get("ENCRYPTED_ACCOUNTS")
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

MY_PAGE_URL = "https://m.sktuniverse.co.kr/my"
LOGIN_VIEW_URL = "https://m.sktuniverse.co.kr/member/login/view?loginRedirectUrl=%2Fmy"
TID_AUTHORIZE_URL = "https://tapi.t-id.co.kr/oidc/v20/authorize?client_id=a1c144a9-6ab3-49f3-b03f-4ce80d257f16&redirect_uri=https%3A%2F%2Fm.sktuniverse.co.kr%2Fmember%2Flogin%2Fchannel/tid"
TWORLD_MY_URL = "https://m.tworld.co.kr/v6/my?returnUrl=https://m.tworld.co.kr/v6/main"
TWORLD_LOGIN_URL = "https://m.tworld.co.kr/common/tid/login?target=/v6/my"
MOBILE_USER_AGENT = "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"

WARM_TARGETS = [
    {"id": "a77ila2000", "type": "universe", "name": "me-universe"},
    {"id": "a77ila10004", "type": "universe", "name": "mother-universe"},
    {"id": "min560728", "type": "universe", "name": "father-universe"},
    {"id": "a77ila2000", "type": "general", "name": "me-general"},
    {"id": "a77ila10004", "type": "general", "name": "mother-general"},
    {"id": "min560728", "type": "general", "name": "father-general"},
]
WARM_SUCCESS_INTERVAL = 20 * 60
# Bounded exponential backoff for consecutive failures: retry immediately on the first
# failure (so a single transient hiccup doesn't lose this target's place in the schedule),
# then back off progressively (30s/60s/120s/300s) instead of hammering a genuinely broken
# site/login every 30s indefinitely during a sustained outage. Capped at 300s rather than
# growing unbounded - still checks in every 5 minutes even in the worst case, not "less and
# less often forever".
FAILURE_BACKOFF_SCHEDULE = [0, 30, 60, 120, 300]
# The site itself won't renew a barcode's ~20min validity early - refreshing before the real
# expiry just gets the same barcode back, so next_refresh_at MUST be based on the real
# remaining seconds_left from the API response, never an artificially-pulled-earlier slot
# (a prior version tried a fixed epoch-aligned stagger to spread the 6 targets out, but that
# meant attempting a refresh ~2min before real expiry, which can't actually renew anything).
# Staggering instead falls out naturally: only one target can be scraped at a time (the
# global browser lock), so each real refresh lands at a different wall-clock moment, and
# since next_refresh_at is always "this target's own last real success + its own real ttl",
# that natural offset persists indefinitely rather than resyncing everyone to one shared clock.
# Both lock TTLs must comfortably exceed the slowest legitimate scrape on EITHER caller -
# Vercel (hard-killed at 60s by the platform) and the Oracle worker (internal budget ~90s,
# external `timeout` backstop 110s - see oracle/worker_tick.py). 75/90 only covered Vercel's
# side; once the Oracle worker could legitimately still be mid-scrape past 75-90s, its lock
# could silently expire and get re-acquired by another process (Vercel or another tick) while
# the original attempt was still genuinely running - a real double-scrape race, not just a
# slow-cleanup delay. Raised with margin above Oracle's 110s worst case.
WARM_LOCK_TTL = 130
WARM_CURRENT_TTL = 90
# Confirmed empirically (2026-07-14): universe and general share ONE rotation clock per
# account on the site's side - fetching either endpoint just reports how much time is left
# on the account's current cycle, not "20 fresh minutes from whenever we happened to ask".
# So a plain on-time scrape always spends its ~20-35s login/navigation AFTER the real expiry
# has already passed, meaning the barcode is genuinely stale for that whole login duration.
# To shrink that window, a scraper may pick a target before its real next_refresh_at (see
# select_warm_target) - the login/navigation happens early while the old barcode is still
# technically valid, and once on the my-page, if the value read back still looks like the
# tail end of the old cycle, poll_for_fresh_barcode() waits it out in place (no re-login
# needed) until the real rotation happens. The Oracle timer itself runs every 20s, so a 20s
# lead only started the job anywhere from 0-20s early; live login took 10-12s and could consume
# that entire margin. A 40s selection window makes the first eligible tick land roughly
# 20-40s before expiry without increasing tick/Redis frequency. This is still one scrape per
# ~20min cycle - the authenticated page simply waits for the actual rotation when necessary.
WARM_EARLY_LOGIN_LEAD_SECONDS = 40
LAST_BARCODE_RETENTION = 7 * 24 * 60 * 60


def decrypt_accounts():
    key = base64.urlsafe_b64decode(ENCRYPTION_KEY_B64)
    encrypted = base64.urlsafe_b64decode(ENCRYPTED_ACCOUNTS_B64)
    return json.loads(Fernet(key).decrypt(encrypted).decode("utf-8"))


def redis_command(command, timeout=4):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    req = urllib.request.Request(
        UPSTASH_REDIS_REST_URL,
        data=json.dumps(command).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("error"):
                print(f"redis error: {payload.get('error')}", flush=True)
                return None
            return payload.get("result")
    except Exception as exc:
        print(f"redis command failed: {type(exc).__name__}: {exc}", flush=True)
        return None


class RedisUnavailable(Exception):
    pass


def mget_padded(keys):
    # Guarantees a list of exactly len(keys) elements on success. redis_command returns None
    # on any error (network failure, Upstash error response, etc) - that case raises
    # RedisUnavailable instead of silently becoming an all-None list, because an all-None
    # list is indistinguishable from "every key genuinely has no value", and callers treat
    # those two situations very differently: select_warm_target() reads all-None state as
    # "next_refresh_at=0 for everyone", i.e. every target is overdue, which would launch a
    # real login scrape during a Redis outage - completely unnecessary work triggered by an
    # infra hiccup, not an actual due barcode. Callers must catch this explicitly and fail
    # closed (skip the scrape) instead of guessing.
    keys = list(keys)
    values = redis_command(["MGET", *keys])
    if values is None:
        raise RedisUnavailable("Redis MGET failed")
    if len(values) != len(keys):
        values = (list(values) + [None] * len(keys))[:len(keys)]
    return values


def normalize_barcode_type(value):
    return "general" if value in ("general", "normal", "tworld") else "universe"


def parse_seconds_left(raw, default=20 * 60):
    # `int(raw or default)` looks equivalent but isn't: Python treats 0 as falsy, so a
    # genuine `expireSeconds: 0` from the site (the barcode is right at the rotation
    # boundary) would silently become `default` (1200) instead of the real 0. That matters
    # here specifically because poll_for_fresh_barcode()'s early-lead polling loop keeps
    # polling only while seconds_left looks "not fresh yet" - if a 0 gets turned into 1200,
    # the loop sees an apparently-fresh 20-minute cycle and stops immediately, possibly
    # caching the same barcode number that's about to (or just did) rotate.
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def cache_key(account_id, barcode_type="universe"):
    return f"barcode:{normalize_barcode_type(barcode_type)}:{account_id}"


def warm_state_key(account_id, barcode_type):
    return f"barcode:warm:{normalize_barcode_type(barcode_type)}:{account_id}"


def warm_lock_key():
    return "barcode:warm-lock"


def warm_current_key():
    return "barcode:warm-current"


def browser_lock_key():
    return "barcode:browserless-lock"


# Default TTLs sized for the Oracle worker's worst case (see the WARM_LOCK_TTL comment
# above). Vercel passes its own shorter, platform-appropriate TTL explicitly at its call
# sites (get_barcode.py) - if Vercel used this 130s default too, a force-killed Vercel
# request (60s hard kill) would take up to 130s to self-heal instead of the ~90s that
# actually matches its own worst case, needlessly blocking other targets longer.
BROWSER_LOCK_TTL_DEFAULT = 130


def acquire_browser_lock(account_id, ttl=BROWSER_LOCK_TTL_DEFAULT):
    token = f"{account_id}:{time.time()}"
    result = redis_command(["SET", browser_lock_key(), token, "EX", ttl, "NX"])
    if result == "OK":
        return token
    return None


def release_browser_lock(token):
    if not token:
        return
    try:
        current = redis_command(["GET", browser_lock_key()])
        if current == token:
            redis_command(["DEL", browser_lock_key()])
    except Exception as exc:
        print(f"redis lock release failed: {exc}", flush=True)


def compute_failure_retry_delay(existing):
    # See FAILURE_BACKOFF_SCHEDULE above. consecutive_failures resets to 0 on the next real
    # success (record_warm_result's success path), so this only escalates during a genuine
    # sustained failure streak, not across normal day-to-day operation.
    consecutive_failures = int(existing.get("consecutive_failures", 0)) + 1
    index = min(consecutive_failures - 1, len(FAILURE_BACKOFF_SCHEDULE) - 1)
    delay = FAILURE_BACKOFF_SCHEDULE[index]
    return delay, consecutive_failures


def set_cached_barcode(account_id, number, seconds_left, barcode_type="universe", grade=""):
    barcode_type = normalize_barcode_type(barcode_type)
    ttl = max(1, parse_seconds_left(seconds_left))
    value = {
        "number": str(number),
        "expires_at": time.time() + ttl,
        "grade": str(grade or ""),
        "created_at": time.time(),
    }
    write_result = redis_command(["SET", cache_key(account_id, barcode_type), json.dumps(value), "EX", LAST_BARCODE_RETENTION])
    target = next((item for item in WARM_TARGETS if item["id"] == account_id and item["type"] == barcode_type), None)
    if target:
        now = int(time.time())
        existing = get_warm_state(target)
        if write_result == "OK":
            # Based on this fetch's own real remaining validity - never pulled earlier, since
            # the site won't renew before the real ~20min window is actually up. Staggering
            # across the 6 targets comes from natural sequential processing (see comment near
            # WARM_SUCCESS_INTERVAL above), not from forcing this schedule onto a shared clock.
            next_refresh_at = now + min(ttl, WARM_SUCCESS_INTERVAL)
            set_warm_state(target, {
                "next_refresh_at": next_refresh_at,
                "last_success_at": now,
                "last_failure_at": existing.get("last_failure_at", 0),
                "last_number": str(number),
                "last_grade": str(grade or ""),
            })
        else:
            delay, consecutive_failures = compute_failure_retry_delay(existing)
            print(f"redis SET failed for {cache_key(account_id, barcode_type)}; scheduling retry in {delay}s (consecutive_failures={consecutive_failures})", flush=True)
            set_warm_state(target, {
                "next_refresh_at": now + delay,
                "last_success_at": existing.get("last_success_at", 0),
                "last_failure_at": now,
                "last_number": existing.get("last_number", ""),
                "last_grade": existing.get("last_grade", ""),
                "consecutive_failures": consecutive_failures,
            })
    return value


def get_warm_state(target):
    raw = redis_command(["GET", warm_state_key(target["id"], target["type"])])
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as exc:
        print(f"warm state parse failed: {exc}", flush=True)
        return {}


def set_warm_state(target, state):
    redis_command(["SET", warm_state_key(target["id"], target["type"]), json.dumps(state), "EX", 7 * 24 * 60 * 60])


def select_warm_target(now=None, lead_seconds=WARM_EARLY_LOGIN_LEAD_SECONDS):
    # lead_seconds lets a target be picked up slightly before its real next_refresh_at (see
    # WARM_EARLY_LOGIN_LEAD_SECONDS above). Actually-overdue targets (due_at <= now) always
    # win over early-lead ones (due_at in (now, now+lead_seconds]) since they compare smaller.
    now = int(now or time.time())
    # One MGET for all 6 targets' warm state instead of 6 separate round trips - this runs
    # at the start of every tick, so it directly adds to how long a due target waits before
    # its scrape even begins.
    raw_states = mget_padded(warm_state_key(t["id"], t["type"]) for t in WARM_TARGETS)
    selected = None
    selected_state = None
    selected_due = None
    for index, target in enumerate(WARM_TARGETS):
        state = {}
        if raw_states[index]:
            try:
                state = json.loads(raw_states[index])
            except Exception as exc:
                print(f"warm state parse failed: {exc}", flush=True)
        due_at = int(state.get("next_refresh_at") or 0)
        # The early-lead window only makes sense against a real success schedule (the barcode
        # is about to expire on its predictable ~20min cycle, worth trying a bit early) - it
        # was never meant to apply on top of a FAILURE backoff, whose whole point is spacing
        # out retries (compute_failure_retry_delay sets next_refresh_at to now+0 or now+30
        # specifically to throttle how fast a failing target gets retried). Applying the same
        # 20s lead there let a 30s backoff be selected again after only ~10s - observed live
        # (2026-07-16) as a tworld_barcode_not_found target retrying every ~20s (this tick
        # interval) instead of the intended 30s, once the login flow started failing.
        effective_lead = 0 if int(state.get("consecutive_failures", 0)) > 0 else lead_seconds
        if due_at > now + effective_lead:
            continue
        if selected is None or due_at < selected_due or (due_at == selected_due and index < selected.get("index", 0)):
            selected = dict(target, index=index)
            selected_state = state
            selected_due = due_at
    return selected, selected_state or {}


def acquire_warm_lock(ttl=WARM_LOCK_TTL):
    token = f"warm:{time.time()}"
    result = redis_command(["SET", warm_lock_key(), token, "EX", ttl, "NX"])
    if result == "OK":
        return token
    return None


def release_warm_lock(token):
    if not token:
        return
    try:
        current = redis_command(["GET", warm_lock_key()])
        if current == token:
            redis_command(["DEL", warm_lock_key()])
            redis_command(["DEL", warm_current_key()])
    except Exception as exc:
        print(f"warm lock release failed: {exc}", flush=True)


def record_warm_result(target, token, success, http_code="", release_lock=True):
    now = int(time.time())
    existing = get_warm_state(target)
    if success:
        # set_cached_barcode already recorded the correct ttl-aware next_refresh_at for this
        # refresh as part of the scrape - don't clobber it with a flat +20m here. (No sibling
        # staggering happens here or anywhere else - that was tried and reverted, see the
        # comment near WARM_SUCCESS_INTERVAL above.)
        state = dict(existing)
        state.setdefault("next_refresh_at", now + WARM_SUCCESS_INTERVAL)
        state["last_http_code"] = http_code
        # Explicitly reset rather than leaving a stale value from a past failure streak sitting
        # in `existing` (dict(existing) above copies it forward otherwise) - a lingering
        # consecutive_failures both defeats compute_failure_retry_delay's "first failure after
        # a success retries immediately" grace period on the NEXT failure, and would make
        # select_warm_target's failure-vs-success early-lead check above misclassify this
        # target as still failing.
        state["consecutive_failures"] = 0
    else:
        delay, consecutive_failures = compute_failure_retry_delay(existing)
        state = {
            "next_refresh_at": now + delay,
            "last_success_at": existing.get("last_success_at", 0),
            "last_failure_at": now,
            "last_number": existing.get("last_number", ""),
            "last_grade": existing.get("last_grade", ""),
            "consecutive_failures": consecutive_failures,
            "last_http_code": http_code,
        }
    set_warm_state(target, state)
    if release_lock:
        release_warm_lock(token)
    return state


def safe_url(page):
    try: return page.url
    except Exception: return "closed"


def get_body_text(page, limit=260):
    try: return page.locator("body").inner_text(timeout=700).replace("\n", " | ")[:limit]
    except Exception: return ""


def goto_page(page, url, timeout=9000, referer=None):
    try:
        args = {"wait_until": "domcontentloaded", "timeout": timeout}
        if referer: args["referer"] = referer
        return page.goto(url, **args)
    except Exception as exc:
        print(f"debug goto ignored url={url} current={safe_url(page)} error={type(exc).__name__}: {exc}", flush=True)
        return None


def extract_barcode_number(page):
    try: text = page.locator("body").inner_text(timeout=1500)
    except Exception: text = ""
    for candidate in re.findall(r"(?<!\d)(\d[\d\s-]{10,22}\d)(?!\d)", text):
        number = re.sub(r"\D", "", candidate)
        if 12 <= len(number) <= 20:
            return number
    return ""


def extract_seconds_left(page):
    text = get_body_text(page, 1200)
    matches = re.findall(r"(?<!\d)(\d{1,2})\s*:\s*(\d{2})(?!\d)", text)
    if matches:
        minutes, seconds = matches[-1]
        return int(minutes) * 60 + int(seconds)
    return 20 * 60


def extract_membership_grade(page):
    try:
        text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        text = ""
    for grade in ["VIP", "GOLD", "SILVER"]:
        if re.search(rf"(?<![A-Z]){grade}(?![A-Z])", text, re.I):
            return grade
    return ""


def fetch_barcode_data(page):
    try:
        result = page.evaluate("""
        async () => {
          const response = await fetch('/etc/barcode/data', {
            method: 'GET',
            credentials: 'include',
            headers: {'Content-Type': 'application/json'}
          });
          const text = await response.text();
          try {
            return {ok: response.ok, status: response.status, body: JSON.parse(text)};
          } catch (error) {
            return {ok: response.ok, status: response.status, text};
          }
        }
        """)
        body = result.get("body") or {}
        data = body.get("data") or {}
        number = re.sub(r"\D", "", str(data.get("otbNum") or ""))
        seconds_left = parse_seconds_left(data.get("expireSeconds"))
        code = body.get("code") or ""
        message = body.get("message") or body.get("errorMessage") or ""
        return {
            "status": result.get("status"),
            "number": number,
            "seconds_left": seconds_left,
            "code": code,
            "message": message,
            "raw": str(body)[:500],
        }
    except Exception as exc:
        print(f"debug barcode data api failed: {type(exc).__name__}: {exc}", flush=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def fetch_tworld_membership_data(page):
    # `/common/my/tmembership` (no `/api/v6` prefix) always 404s - it's not a real endpoint.
    # T-World's own frontend calls `/api/v6/common/my/tmembership`, which additionally requires
    # session headers built from the `TWM`/`SessionUpdatedAt` cookies (without them it responds
    # 401 "Some headers are missing"). Verified live 2026-07-16: the old URL 404s every time,
    # the new URL without headers 401s, and the new URL with these headers returns a real
    # membership payload including otbNum/expireSeconds - this was the actual root cause of
    # every general-barcode refresh failing via this API path (the DOM-visible-extraction
    # fallback further down was masking it on the rare attempt that still found a number).
    try:
        result = page.evaluate("""
        async () => {
          function getCookie(name) {
            const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
            return match ? decodeURIComponent(match[2]) : '';
          }
          const twm = getCookie('TWM') || '';
          const updated = getCookie('SessionUpdatedAt') || '';
          const response = await fetch('/api/v6/common/my/tmembership', {
            method: 'GET',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              'Authorization': twm,
              'SessionUpdatedAt': updated,
              'x-session-key': twm,
              'x-session-updated': updated,
              'x-referrer': location.href,
            }
          });
          const text = await response.text();
          try {
            return {ok: response.ok, status: response.status, body: JSON.parse(text)};
          } catch (error) {
            return {ok: response.ok, status: response.status, text};
          }
        }
        """)
        body = result.get("body") or {}
        data = body.get("data") or {}
        number = re.sub(r"\D", "", str(data.get("otbNum") or data.get("barcode") or ""))
        seconds_left = parse_seconds_left(data.get("expireSeconds"))
        return {
            "status": result.get("status"),
            "resp_code": body.get("respCode"),
            "number": number,
            "seconds_left": seconds_left,
            "membership_state": data.get("mbrStCd") or data.get("mbrTypCd") or data.get("displayType") or "",
            "grade": data.get("grade") or data.get("gradeNm") or data.get("membershipGrade") or data.get("mbrGrdNm") or "",
            "message": body.get("respMsg") or body.get("message") or "",
            "raw": str(body)[:500],
        }
    except Exception as exc:
        print(f"debug tworld membership api failed: {type(exc).__name__}: {exc}", flush=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def physical_tap_at(page, x, y):
    try: page.touchscreen.tap(x, y)
    except Exception as exc: print(f"touchscreen tap failed: {exc}", flush=True)
    try: page.mouse.click(x, y)
    except Exception as exc: print(f"mouse click failed: {exc}", flush=True)


def type_first_visible(page, selectors, value, timeout=6000):
    locator = page.locator(", ".join(selectors)).first
    locator.wait_for(state="visible", timeout=timeout)
    try: locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception: pass
    locator.fill("", timeout=timeout)
    locator.type(value, delay=10, timeout=timeout)
    return locator.evaluate("e => e.id || e.name || e.type || e.tagName")


def ensure_idpw_login_mode(page):
    try:
        if page.locator("input[type='password']").first.is_visible(timeout=500):
            return
    except Exception:
        pass
    try:
        clicked = page.evaluate("""
        () => {
          const nodes = Array.from(document.querySelectorAll('button,a,[role=tab],[role=button],li,div,span'));
          const target = nodes.find((el) => {
            const text = (el.innerText || el.textContent || '').trim();
            const r = el.getBoundingClientRect();
            return /ID\\/?PW|아이디|비밀번호/i.test(text) && r.width > 20 && r.height > 10 && r.top < 260;
          });
          if (!target) return 'no-target';
          target.click();
          return (target.innerText || target.textContent || target.tagName || '').trim().slice(0, 80);
        }
        """)
        print(f"debug idpw tab click target={clicked}", flush=True)
        page.wait_for_timeout(600)
    except Exception as exc:
        print(f"debug idpw tab click failed: {exc}", flush=True)


def wait_for_tid_login_form(page, timeout_ms=8000):
    end = time.monotonic() + timeout_ms / 1000
    ensure_idpw_login_mode(page)
    while time.monotonic() < end:
        try:
            if page.locator("input#inputId, input#userId, input[type='text']").first.is_visible(timeout=400):
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"T ID login form not visible. url={safe_url(page)} body={get_body_text(page, 220)}")


def force_submit(page):
    try: page.locator("input[type='password']").first.press("Enter", timeout=1200)
    except Exception as exc: print(f"debug password Enter failed: {exc}", flush=True)
    # If that Enter press just succeeded and navigated us off the login page, the DOM click
    # below would fire against whatever's on the NEW page instead - see the state-check
    # comment in submit_tid_credentials() for the observed real failure this caused.
    if "auth.skt-id.co.kr" not in safe_url(page):
        return
    try:
        clicked = page.evaluate("""
        () => {
          const items = Array.from(document.querySelectorAll('button,input[type=submit],[role=button],a')).map((el) => {
            const r = el.getBoundingClientRect();
            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            return {el, text, top:r.top, width:r.width, height:r.height, disabled:!!el.disabled};
          }).filter((i) => i.width > 100 && i.height > 20 && i.top > 330 && !i.disabled);
          const target = items.find((i) => /로그인|login/i.test(i.text)) || items[0];
          if (!target) return 'no-target';
          target.el.click();
          return `${target.text}|${target.top}|${target.width}x${target.height}`;
        }
        """)
        print(f"debug dom submit click target={clicked}", flush=True)
    except Exception as exc:
        print(f"debug dom submit click failed: {exc}", flush=True)


def wait_for_tid_result(page, timeout_ms=10000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        url = safe_url(page)
        if page.is_closed(): return "closed"
        # A successful T ID callback can finish on the Universe home page instead of
        # preserving `/member/login/channel/tid` or `/my` in the final URL.  Treat only a
        # non-login Universe URL as success: the subsequent `/my` API read remains the
        # authoritative authentication/barcode check, while avoiding a guaranteed 10s
        # timeout after an already-completed login.
        universe_login_finished = (
            url.startswith("https://m.sktuniverse.co.kr")
            and "/member/login" not in url
            and "/netfunnel" not in url
        )
        if "/member/login/channel/tid" in url or "code=" in url or "/my" in url or universe_login_finished:
            print(f"debug T ID callback reached: {url}", flush=True)
            page.wait_for_timeout(800)
            return "callback"
        time.sleep(0.2)
    print(f"debug T ID result wait timed out at url={safe_url(page)} body={get_body_text(page, 200)}", flush=True)
    return "timeout"


def wait_for_tworld_result(page, timeout_ms=12000, settle_ms=800):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        if "m.tworld.co.kr" in safe_url(page):
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            return "callback"
        time.sleep(0.2)
    if "m.tworld.co.kr" in safe_url(page):
        if settle_ms:
            page.wait_for_timeout(settle_ms)
        return "callback"
    print(f"debug T world result wait timed out at url={safe_url(page)} body={get_body_text(page, 200)}", flush=True)
    return "timeout"


def open_tid_from_my(page, mark=None):
    # goto_page() swallows its own timeout exceptions and just carries on, so without a
    # checkpoint after each hop, a slow run can silently burn through all four navigations'
    # worst-case timeouts (7+8+12+12=39s) before the caller's own budget check ever gets a
    # chance to bail.
    def checkpoint(label):
        if mark:
            mark(label)
    goto_page(page, MY_PAGE_URL, timeout=7000)
    checkpoint("tid_my_page")
    page.wait_for_timeout(400)
    goto_page(page, LOGIN_VIEW_URL, timeout=8000, referer=MY_PAGE_URL)
    checkpoint("tid_login_view")
    page.wait_for_timeout(600)
    referer = safe_url(page) if "sktuniverse" in safe_url(page) else LOGIN_VIEW_URL
    print(f"debug direct authorize from login view url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
    goto_page(page, TID_AUTHORIZE_URL, timeout=12000, referer=referer)
    checkpoint("tid_authorize")
    page.wait_for_timeout(600)
    if "auth.skt-id.co.kr" not in safe_url(page) and "tapi.t-id.co.kr" not in safe_url(page):
        physical_tap_at(page, 206, 470)
        page.wait_for_timeout(900)
        print(f"debug direct authorize retry after coordinate tap url={safe_url(page)}", flush=True)
        goto_page(page, TID_AUTHORIZE_URL, timeout=12000, referer=referer)
        checkpoint("tid_authorize_retry")
        page.wait_for_timeout(600)
    print(f"debug tid entry url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)


def wait_for_my_ready(page, timeout_ms=5000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        body = get_body_text(page, 320)
        if len(body) > 80 and not body.lower().startswith("loading"):
            return True
        page.wait_for_timeout(250)
    return False


def submit_tid_credentials(page, target, label=""):
    prefix = f"{label} " if label else ""
    ensure_idpw_login_mode(page)
    print(prefix + "typed user selector:", type_first_visible(page, ["input#inputId", "input#userId", "input[name='userId']", "input[name='id']", "input[type='email']", "input[type='text']"], target["id"], 6000), flush=True)
    print(prefix + "typed password selector:", type_first_visible(page, ["input#inputPassword", "input#password", "input[name='password']", "input[name='passwd']", "input[type='password']"], target["password"], 6000), flush=True)
    try:
        page.locator("input[type='password']").first.press("Enter", timeout=1200)
        print(prefix + "debug password enter submit", flush=True)
    except Exception as exc:
        print(prefix + f"debug password enter failed: {exc}", flush=True)
    page.wait_for_timeout(700)
    # Everything below is a ladder of fallback submit attempts for when the Enter press above
    # didn't register. Each one is only safe to fire while we're STILL stuck on the T-ID login
    # page - once any earlier attempt actually succeeds and navigates us away, the remaining
    # ones fire against whatever happens to be on the NEW page instead of the login form.
    # Observed live (2026-07-16): the first DOM click below succeeded and moved to the tworld
    # my-page, but the code still fired the next 3 fallback actions unconditionally - the last
    # of which (force_submit's own loose "click the first big visible button" fallback) landed
    # on a "실시간 이용요금" (real-time usage fee) link on the new page, navigating away to a
    # bill-detail page instead and breaking the login that had actually already worked. This
    # was the root cause of general-barcode refresh failing on every single attempt.
    if "auth.skt-id.co.kr" not in safe_url(page):
        return
    try:
        clicked = page.evaluate("""
        () => {
          const password = document.querySelector('input[type=password]');
          const passBottom = password ? password.getBoundingClientRect().bottom : 300;
          const nodes = Array.from(document.querySelectorAll('button,input[type=submit],[role=button],a'));
          const visible = nodes.map((el) => {
            const r = el.getBoundingClientRect();
            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            return {el, text, top:r.top, width:r.width, height:r.height, disabled:!!el.disabled};
          }).filter((i) => i.width > 80 && i.height > 20 && i.top > passBottom && !i.disabled);
          const target = visible.find((i) => /로그인|login/i.test(i.text)) || visible[0];
          if (!target) return 'no-target';
          target.el.click();
          return `${target.text}|${target.top}|${target.width}x${target.height}`;
        }
        """)
        print(prefix + f"debug dom login click target={clicked}", flush=True)
    except Exception as exc:
        print(prefix + f"debug dom login click failed: {exc}", flush=True)
    page.wait_for_timeout(500)
    if "auth.skt-id.co.kr" not in safe_url(page):
        return
    try:
        page.locator("button:has-text('로그인'), button:has-text('Login'), input[type='submit'], button").last.click(force=True, timeout=2200)
        print(prefix + "debug locator force click submit", flush=True)
    except Exception as exc:
        print(prefix + f"login button locator failed: {exc}", flush=True)
    page.wait_for_timeout(200)
    if "auth.skt-id.co.kr" not in safe_url(page):
        return
    physical_tap_at(page, 206, 470)
    page.wait_for_timeout(200)
    if "auth.skt-id.co.kr" not in safe_url(page):
        return
    force_submit(page)


def open_barcode_view(page, deadline=None):
    before_url = safe_url(page)
    before_text = get_body_text(page, 220)
    try:
        clicked = page.evaluate("""
        () => {
          const nodes = Array.from(document.querySelectorAll('a,button,[role=button]'));
          const target = nodes.find((el) => /barcode|barCode/i.test(`${el.href || ''} ${el.id || ''} ${el.className || ''} ${el.getAttribute('aria-label') || ''} ${el.innerText || ''}`));
          if (!target) return 'no-dom-target';
          target.click();
          return `${target.tagName}:${target.href || target.id || target.className || target.innerText}`.slice(0, 120);
        }
        """)
        page.wait_for_timeout(900)
        # get_body_text() is a real page.locator().inner_text() CDP round-trip, not a local
        # read - fetch the page state once and reuse it for both the debug print and the
        # comparison below (same instant, same text) instead of asking the remote page twice.
        current_text = get_body_text(page, 220)
        print(f"debug barcode dom click={clicked} url={safe_url(page)} body={current_text[:180]}", flush=True)
        if safe_url(page) != before_url or current_text != before_text:
            return f"dom:{clicked}"
    except Exception as exc:
        print(f"debug barcode dom click failed: {exc}", flush=True)
    for x, y in [(300, 32), (320, 32), (340, 32), (380, 32), (292, 104), (332, 104), (372, 104)]:
        if deadline and time.monotonic() > deadline:
            return "deadline-before-barcode-tap"
        physical_tap_at(page, x, y)
        page.wait_for_timeout(1300)
        # Same dedup as the dom-click branch above - one CDP round-trip reused for both the
        # print and the comparison instead of two, across up to 7 loop iterations.
        current_text = get_body_text(page, 220)
        print(f"debug barcode coordinate tapped {x},{y} url={safe_url(page)} body={current_text[:180]}", flush=True)
        if extract_barcode_number(page) or safe_url(page) != before_url or current_text != before_text:
            return f"tap({x},{y})"
    return "not-opened"


def poll_for_fresh_barcode(fetch_fn, page, mark, started, initial, budget_seconds, min_fresh_seconds=300, poll_interval_ms=2000):
    # Only called for a deliberately early attempt (force_scrape=True, see
    # WARM_EARLY_LOGIN_LEAD_SECONDS): the login/navigation already happened before the real
    # expiry, so `initial` may still be the tail end of the OLD cycle (small seconds_left).
    # Poll the same already-authenticated page in place - no new login needed - until the
    # site's shared per-account clock actually rotates (seconds_left jumps back up) or the
    # caller's own scrape budget runs out, whichever comes first. Falls back to whatever was
    # last read if the budget runs out - no correctness regression either way.
    # budget_seconds is a parameter (not a module constant) because the two callers use very
    # different budgets: Vercel is racing a hard 60s platform kill (SCRAPE_BUDGET_SECONDS=50),
    # while the Oracle worker has no such ceiling and can afford a larger one.
    result = initial
    deadline = started + budget_seconds - 6
    while result.get("number") and int(result.get("seconds_left") or 0) < min_fresh_seconds and time.monotonic() < deadline:
        page.wait_for_timeout(poll_interval_ms)
        mark("poll_for_rotation")
        result = fetch_fn(page)
    return result
