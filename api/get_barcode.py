from flask import Flask, request, Response
import os, sys, json, base64, io, time, re, signal
from playwright.sync_api import sync_playwright

# Vercel invokes this file directly as the /api/get_barcode entrypoint, which does NOT
# automatically put this directory on sys.path for sibling imports (that's exactly why
# warm_status.py/warm_tick.py have to do this same append before importing get_barcode as a
# module) - without it, `from barcode_core import ...` below raises ModuleNotFoundError only
# when get_barcode.py itself is the direct entrypoint, which is why this broke /api/get_barcode
# specifically while /api/warm_status (which imports get_barcode, inheriting its sys.path
# fix) kept working.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from barcode_core import (
    RedisUnavailable, redis_command, mget_padded, normalize_barcode_type,
    warm_current_key, acquire_browser_lock, release_browser_lock,
    get_warm_state, set_warm_state, set_cached_barcode, select_warm_target,
    acquire_warm_lock, release_warm_lock, record_warm_result, WARM_TARGETS,
    WARM_SUCCESS_INTERVAL, decrypt_accounts, safe_url, get_body_text, goto_page,
    extract_barcode_number, extract_seconds_left, extract_membership_grade,
    fetch_barcode_data, fetch_tworld_membership_data, open_tid_from_my,
    wait_for_my_ready, submit_tid_credentials, wait_for_tid_login_form,
    wait_for_tid_result, wait_for_tworld_result, ensure_idpw_login_mode,
    open_barcode_view, poll_for_fresh_barcode, MY_PAGE_URL, TWORLD_MY_URL,
    TWORLD_LOGIN_URL, MOBILE_USER_AGENT, UPSTASH_REDIS_REST_URL,
    UPSTASH_REDIS_REST_TOKEN,
)

app = Flask(__name__)
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "")
# Moved to a single, much more capable self-hosted instance (Oracle ARM A1.Flex, 2 OCPU/12GB
# vs. the old 1 OCPU/1GB AMD micro boxes) - CONCURRENT=2 on this box means general and
# universe no longer need separate dedicated servers to avoid queueing each other out.
BROWSERLESS_WS_URL = os.environ.get("BROWSERLESS_WS_URL", "ws://168.138.194.2:3000")
BROWSERLESS_WS_URL_UNIVERSE = os.environ.get("BROWSERLESS_WS_URL_UNIVERSE", BROWSERLESS_WS_URL)
BROWSERLESS_TOKEN_UNIVERSE = os.environ.get("BROWSERLESS_TOKEN_UNIVERSE", BROWSERLESS_TOKEN)
# Vercel Runtime Timeout Error logs showed /api/warm_tick hitting the platform's own 60s
# maxDuration and getting killed outright - which skips our except/finally entirely, so
# release_browser_lock() and record_warm_result() never run. Checked between every stage
# transition (see mark() below) rather than relying on signal.alarm alone, since Vercel
# kept hitting its own 60s kill even with the alarm armed - most likely because the Python
# runtime here doesn't execute request handling on the main thread, where signal.alarm is a
# silent no-op. The self-hosted Browserless VM runs the full login flow in ~50-55s on its
# own, well above the old 35s budget - that guaranteed a ScrapeTimeout every run. Set closer
# to the platform ceiling to let real runs finish; the browser-lock's own 90s Redis TTL
# (acquire_browser_lock) is the backstop if a rare slow step still gets hard-killed past 60s
# instead of hitting this check cleanly.
# NOTE: this whole budget/signal.alarm mechanism exists ONLY to route around Vercel's 60s
# platform kill. The Oracle worker (oracle/worker_tick.py) has no such ceiling and uses a
# much simpler OS-level `timeout` wrapper instead - see that file and the migration plan.
SCRAPE_BUDGET_SECONDS = 50


class ScrapeTimeout(Exception):
    pass


def _scrape_alarm_handler(signum, frame):
    raise ScrapeTimeout(f"scrape exceeded {SCRAPE_BUDGET_SECONDS}s internal budget")

CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112"
]


def get_cached_barcode(account_id, barcode_type="universe", allow_stale=False):
    from barcode_core import cache_key
    barcode_type = normalize_barcode_type(barcode_type)
    now = time.time()

    raw = redis_command(["GET", cache_key(account_id, barcode_type)])
    if not raw and barcode_type == "universe":
        raw = redis_command(["GET", f"barcode:{account_id}"])
    if not raw:
        return None
    try:
        value = json.loads(raw)
        value["seconds_left"] = max(0, int(value.get("expires_at", 0) - now))
        value["stale"] = value.get("expires_at", 0) <= now + 5
        value["stale_seconds"] = max(0, int(now - value.get("expires_at", now)))
        if value["stale"] and not allow_stale:
            return None
        return value
    except Exception as exc:
        print(f"redis cache parse failed: {exc}", flush=True)
        return None


def resync_warm_schedule_from_cache(account_id, barcode_type, cached):
    # perform_barcode_request can return a still-valid cached barcode without ever calling
    # set_cached_barcode (no real scrape happened). If the scheduler's next_refresh_at for
    # this target was ever earlier than the cache's real remaining validity - from a debug
    # call, clock drift, or a stale value left over from before this fix - that "success"
    # would otherwise never advance next_refresh_at, freezing it "due" forever and starving
    # every other target, since select_warm_target always picks the most overdue first.
    target = next((item for item in WARM_TARGETS if item["id"] == account_id and item["type"] == barcode_type), None)
    if not target:
        return
    now = int(time.time())
    seconds_left = max(0, int(cached.get("seconds_left", 0)))
    correct_next_refresh_at = now + min(seconds_left, WARM_SUCCESS_INTERVAL)
    existing = get_warm_state(target)
    if int(existing.get("next_refresh_at") or 0) >= correct_next_refresh_at:
        return
    state = dict(existing)
    state["next_refresh_at"] = correct_next_refresh_at
    set_warm_state(target, state)


def json_response(payload, status=200):
    return Response(json.dumps(payload, ensure_ascii=False), status=status, mimetype="application/json")


def image_response(text, color=(255, 235, 238)):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (760, 300), color=color)
    draw = ImageDraw.Draw(img)
    msg = str(text).replace("\n", " ")[:430]
    draw.multiline_text((18, 34), "\n".join(msg[i:i + 76] for i in range(0, len(msg), 76)), fill=(211, 47, 47))
    out = io.BytesIO(); img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")


def screenshot_bytes(page):
    client = page.context.new_cdp_session(page)
    return base64.b64decode(client.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"])


def encode_code128_c(digits):
    # Code 128 Set C: pairs of digits per symbol (00-99) instead of one digit per symbol
    # (Set B, the previous implementation) - the standard, more compact encoding for
    # numeric-only data, and what the official T membership app's barcode uses. Both are
    # equally valid Code128 and should scan to the identical digit string either way (the
    # symbology is self-describing), so this is a format-parity change, not a correctness
    # fix. Verified against the `python-barcode` library: identical symbol sequence and
    # checksum for our actual 16-digit barcode numbers.
    if len(digits) % 2 == 1:
        # Odd length (not expected for real barcode numbers, which are consistently 16
        # digits, but handled defensively): Code C for all leading pairs, then switch to
        # Code B (code value 100) for the trailing single digit - matches how
        # python-barcode itself encodes an odd-length numeric string.
        codes = [105]
        for i in range(0, len(digits) - 1, 2):
            codes.append(int(digits[i:i + 2]))
        codes.append(100)
        codes.append(ord(digits[-1]) - 32)
    else:
        codes = [105]
        for i in range(0, len(digits), 2):
            codes.append(int(digits[i:i + 2]))
    return codes


_BARCODE_PNG_CACHE = {}
_BARCODE_PNG_CACHE_MAX = 32


def render_barcode_png(number):
    # The drawn pixels depend only on the digit string, not on seconds_left/grade/stale
    # (those only affect response headers) - so the same number always renders to the exact
    # same bytes. Vercel Fluid compute keeps the Python process warm across invocations, and
    # this function's hot path (every page view, tab refocus, 5s status poll) requests the
    # *same* still-valid number over and over between real refreshes (~20min apart), so
    # re-drawing it pixel-by-pixel every single time was pure repeated CPU work. Caching by
    # number alone (module-level dict, survives across requests within a warm instance) means
    # each number is drawn once until it actually changes.
    cached = _BARCODE_PNG_CACHE.get(number)
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw, ImageFont
    codes = encode_code128_c(number)
    checksum = codes[0] + sum(value * idx for idx, value in enumerate(codes[1:], 1))
    codes += [checksum % 103, 106]
    module, quiet, bar_height, text_height = 3, 30, 150, 46
    width = quiet * 2 + sum(sum(int(w) for w in CODE128_PATTERNS[code]) for code in codes) * module
    img = Image.new("RGB", (width, bar_height + text_height), "white")
    draw = ImageDraw.Draw(img)
    x = quiet
    for code in codes:
        for i, char in enumerate(CODE128_PATTERNS[code]):
            w = int(char) * module
            if i % 2 == 0:
                draw.rectangle([x, 18, x + w - 1, 18 + bar_height - 1], fill="black")
            x += w
    try: font = ImageFont.truetype("arial.ttf", 22)
    except Exception: font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), number, font=font)
    draw.text(((width - (bbox[2] - bbox[0])) / 2, bar_height + 20), number, fill="black", font=font)
    out = io.BytesIO(); img.save(out, format="PNG")
    png_bytes = out.getvalue()
    if len(_BARCODE_PNG_CACHE) >= _BARCODE_PNG_CACHE_MAX:
        _BARCODE_PNG_CACHE.pop(next(iter(_BARCODE_PNG_CACHE)))
    _BARCODE_PNG_CACHE[number] = png_bytes
    return png_bytes


def barcode_response(number, seconds_left=1200, grade="", stale=False, stale_seconds=0):
    number = re.sub(r"\D", "", str(number))
    if not number:
        return image_response("Barcode number is empty")
    resp = Response(render_barcode_png(number), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Barcode-Number"] = number
    resp.headers["X-Barcode-Seconds-Left"] = str(max(0, int(seconds_left or 0)))
    resp.headers["X-Barcode-Stale"] = "1" if stale else "0"
    resp.headers["X-Barcode-Status"] = "stale" if stale else "valid"
    resp.headers["X-Barcode-Stale-Seconds"] = str(max(0, int(stale_seconds or 0)))
    if grade:
        resp.headers["X-Membership-Grade"] = str(grade)
    return resp


def diagnostic_response(page, context, account_id, result, elapsed):
    from PIL import Image, ImageDraw
    try: shot = Image.open(io.BytesIO(screenshot_bytes(page))).convert("RGB")
    except Exception: shot = Image.new("RGB", (412, 915), color=(245, 245, 245))
    img = Image.new("RGB", (shot.width + 920, max(shot.height, 920)), color=(255, 255, 255))
    img.paste(shot, (0, 0))
    draw = ImageDraw.Draw(img)
    rows = ["T Universe login diagnostic", f"account={account_id}", f"elapsed={elapsed:.1f}s result={result}", f"url={safe_url(page)}", f"body={get_body_text(page, 620)}"]
    y = 18; x = shot.width + 18
    for row in rows:
        for i in range(0, len(str(row)), 106):
            draw.text((x, y), str(row)[i:i + 106], fill=(20, 20, 20)); y += 18
    out = io.BytesIO(); img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")


@app.route("/api/warm_tick", methods=["GET", "POST"])
def warm_tick():
    # Same-account sibling chaining (attempting universe then immediately general, or vice
    # versa, in one request) was tried and reverted here: live observation showed it rarely
    # actually engaged (universe and general's real due times aren't as tightly synced as
    # assumed - either can become due first) and its tighter per-call time budget contributed
    # to exposing a separate false-success bug (see the X-Barcode-Fallback check below) as a
    # retry storm. That bug fix alone brought the same-person pair gap down to a consistent
    # ~40-60s with no failures - back to the simple single-target-per-request version.
    now = int(time.time())
    lock_token = acquire_warm_lock()
    if not lock_token:
        return json_response({"status": "locked", "retry_after": 60})
    try:
        target, state = select_warm_target(now)
    except RedisUnavailable:
        # Don't guess a target when we can't actually read the schedule - the safe default
        # (select_warm_target failing open) would launch a real login scrape for whichever
        # target happens to sort first, purely because Redis hiccuped. Skip this tick; the
        # next cron-job.org call (or client retrigger) picks up normally once Redis recovers.
        release_warm_lock(lock_token)
        return json_response({"status": "redis_unavailable"}, status=503)
    if not target:
        release_warm_lock(lock_token)
        return json_response({"status": "no_due", "now": now})
    is_early = int(state.get("next_refresh_at") or 0) > now
    redis_command(["SET", warm_current_key(), json.dumps({
        "token": lock_token,
        "started_at": now,
        "id": target["id"],
        "type": target["type"],
        "name": target["name"],
    }), "EX", 90])
    try:
        result = perform_barcode_request(target["id"], target["type"], force_scrape=is_early)
        if isinstance(result, tuple):
            body, http_code = result[0], result[1]
        else:
            body, http_code = result, getattr(result, "status_code", 200)
        content_type = getattr(body, "mimetype", "") or ""
        is_fallback = bool(getattr(body, "headers", None) and body.headers.get("X-Barcode-Fallback"))
        # A lock-busy fallback (browser lock held by another request) re-serves the last
        # cached barcode instead of actually scraping - without this check it looked
        # identical to a genuine fresh success to the naive "200 + image" test below, which
        # never touched last_failure_at/consecutive_failures, so repeat contention could
        # retry every few seconds forever instead of backing off normally.
        success = http_code == 200 and content_type.startswith("image/") and not is_fallback
    except Exception as exc:
        print(f"warm_tick failed for {target['id']}/{target['type']}: {type(exc).__name__}: {exc}", flush=True)
        http_code = 500
        success = False
    state = record_warm_result(target, lock_token, success, str(http_code))
    return json_response({
        "status": "done",
        "id": target["id"],
        "type": target["type"],
        "name": target["name"],
        "success": success,
        "http_code": http_code,
        "next_refresh_at": state["next_refresh_at"],
    })


@app.route("/api/warm_status", methods=["GET"])
def warm_status():
    # Was up to 13 sequential Redis round trips (1 warm-current + 6 warm-state + up to
    # 12 barcode-cache incl. the legacy-key fallback) - the frontend polls this every 5s
    # per open tab, so that added up fast. One MGET covers all of it in a single round trip.
    from barcode_core import cache_key, warm_state_key
    now = int(time.time())
    state_keys = [warm_state_key(t["id"], t["type"]) for t in WARM_TARGETS]
    cache_keys = [cache_key(t["id"], t["type"]) for t in WARM_TARGETS]
    # WARM_TARGETS has 2 entries per account (universe + general), but the legacy fallback
    # key only depends on account id, not type - fetching it once per WARM_TARGETS entry
    # asked Redis for the same 3 keys twice on every single call to this 5s-polled endpoint.
    account_ids = list(dict.fromkeys(t["id"] for t in WARM_TARGETS))
    legacy_keys = [f"barcode:{aid}" for aid in account_ids]
    all_keys = [warm_current_key(), *state_keys, *cache_keys, *legacy_keys]
    try:
        raw_values = mget_padded(all_keys)
    except RedisUnavailable:
        # Fabricating an all-empty response here would show every card as expired/idle
        # instead of "we don't know right now" - a 503 makes the frontend's existing
        # `if (!response.ok) return;` skip this poll and keep showing its last good state
        # instead of flashing a false "만료됨" across every account.
        return json_response({"status": "redis_unavailable"}, status=503)
    n = len(WARM_TARGETS)
    current_raw = raw_values[0]
    state_raws = raw_values[1:1 + n]
    cache_raws = raw_values[1 + n:1 + 2 * n]
    legacy_by_id = dict(zip(account_ids, raw_values[1 + 2 * n:1 + 2 * n + len(account_ids)]))

    current = None
    if current_raw:
        try:
            current = json.loads(current_raw)
        except Exception:
            current = None

    targets = []
    for index, item in enumerate(WARM_TARGETS):
        state = {}
        if state_raws[index]:
            try:
                state = json.loads(state_raws[index])
            except Exception as exc:
                print(f"warm state parse failed: {exc}", flush=True)

        raw_cache = cache_raws[index]
        if not raw_cache and item["type"] == "universe":
            raw_cache = legacy_by_id.get(item["id"])
        cached = None
        if raw_cache:
            try:
                cached = json.loads(raw_cache)
                cached["seconds_left"] = max(0, int(cached.get("expires_at", 0) - now))
                cached["stale"] = cached.get("expires_at", 0) <= now + 5
                cached["stale_seconds"] = max(0, int(now - cached.get("expires_at", now)))
            except Exception as exc:
                print(f"redis cache parse failed: {exc}", flush=True)
                cached = None

        targets.append({
            "id": item["id"],
            "type": item["type"],
            "name": item["name"],
            "next_refresh_at": int(state.get("next_refresh_at") or 0),
            "last_success_at": int(state.get("last_success_at") or 0),
            "last_failure_at": int(state.get("last_failure_at") or 0),
            "has_cache": bool(cached),
            "stale": bool(cached and cached.get("stale")),
            "seconds_left": int(cached.get("seconds_left", 0)) if cached else 0,
            "stale_seconds": int(cached.get("stale_seconds", 0)) if cached else 0,
        })
    return json_response({"status": "ok", "now": now, "current": current, "targets": targets})


@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    if not account_id:
        return "Account ID is required.", 400
    debug_mode = request.args.get("debug") == "1"
    cache_only = request.args.get("cache_only") == "1"
    force_scrape = request.args.get("force_scrape") == "1"
    barcode_type = normalize_barcode_type(request.args.get("type"))
    return perform_barcode_request(account_id, barcode_type, debug_mode=debug_mode, cache_only=cache_only, force_scrape=force_scrape)


def perform_barcode_request(account_id, barcode_type, debug_mode=False, cache_only=False, force_scrape=False):
    browser = None
    lock_token = None
    stage = "start"
    started = time.monotonic()
    def mark(label):
        elapsed = time.monotonic() - started
        print(f"debug elapsed={elapsed:.1f}s stage={label}", flush=True)
        # signal.alarm is the belt; this is the suspenders. Vercel logs showed the alarm
        # alone wasn't enough to avoid the platform's own 60s kill (likely because the
        # Python runtime here doesn't run request handling on the main thread, where
        # signal.alarm silently has no effect) - so every stage transition also checks
        # elapsed wall-clock time directly and bails out via normal Python control flow,
        # which works regardless of threading/signal semantics.
        if elapsed > SCRAPE_BUDGET_SECONDS:
            raise ScrapeTimeout(f"scrape exceeded {SCRAPE_BUDGET_SECONDS}s internal budget at stage={label}")
    has_alarm = False
    if hasattr(signal, "alarm"):
        try:
            signal.signal(signal.SIGALRM, _scrape_alarm_handler)
            signal.alarm(SCRAPE_BUDGET_SECONDS)
            has_alarm = True
        except Exception as exc:
            print(f"debug scrape alarm unavailable: {type(exc).__name__}: {exc}", flush=True)
    try:
        # decrypt_accounts() (Fernet decrypt + JSON parse of all 3 accounts) used to run
        # unconditionally here, before either cache check below - meaning it ran on every
        # single cache-hit request too (by far the most frequent path: every page load, tab
        # refocus, and 5s status poll from every open tab), even though `target` is only
        # actually needed once we're past both cache checks and about to attempt a real
        # scrape. Deferring it to just before that point cuts real CPU work off the hot path
        # without changing behavior - an invalid account_id still ends up at the same 404
        # "Account not found", just reached after the (already-guaranteed-to-miss) cache
        # checks instead of before them.
        cached = get_cached_barcode(account_id, barcode_type)
        if not debug_mode and cached and not force_scrape:
            resync_warm_schedule_from_cache(account_id, barcode_type, cached)
            return barcode_response(cached["number"], cached.get("seconds_left", 0), cached.get("grade", ""), cached.get("stale", False), cached.get("stale_seconds", 0))
        if cache_only:
            cached = get_cached_barcode(account_id, barcode_type, allow_stale=True)
            if not debug_mode and cached:
                return barcode_response(cached["number"], cached.get("seconds_left", 0), cached.get("grade", ""), cached.get("stale", False), cached.get("stale_seconds", 0))
            return "No cached barcode", 404
        stage = "decrypt_accounts"; mark(stage)
        accounts = decrypt_accounts()
        target = next((acc for acc in accounts if acc["id"] == account_id), None)
        if not target:
            return f"Account not found: {account_id}", 404
        lock_token = acquire_browser_lock(account_id)
        if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN and not lock_token:
            cached = get_cached_barcode(account_id, barcode_type, allow_stale=True)
            if not debug_mode and cached:
                resync_warm_schedule_from_cache(account_id, barcode_type, cached)
                # X-Barcode-Fallback tells warm_tick this wasn't a real scrape attempt (the
                # browser lock was busy) - just re-serving whatever was cached. Without this
                # marker, warm_tick's naive "200 + image content-type = success" check
                # treated this the same as a genuine fresh refresh, which (a) never actually
                # renewed a stale barcode and (b) crucially never touched last_failure_at/
                # consecutive_failures, so nothing throttled the retry - observed causing a
                # sub-second-interval retry storm during a since-reverted experiment that
                # briefly made lock contention here common instead of rare.
                resp = barcode_response(cached["number"], cached.get("seconds_left", 0), cached.get("grade", ""), cached.get("stale", False), cached.get("stale_seconds", 0))
                resp.headers["X-Barcode-Fallback"] = "lock-busy"
                return resp
            resp = image_response(f"Another barcode refresh is already running\nID: {account_id}\nRetry shortly", color=(255, 248, 225))
            resp.status_code = 423
            resp.headers["Retry-After"] = "75"
            return resp
        with sync_playwright() as p:
            stage = "connect_browserless"; mark(stage)
            # 8s was too tight: launching a fresh Chromium process on the self-hosted VM's
            # single vCPU occasionally takes longer than that on its own (observed directly -
            # the server-side logs kept launching successfully while the client had already
            # given up), which was producing clean-looking but avoidable connection failures.
            # Reverted: merging universe onto the general server caused BOTH types to start
            # failing (contention for the single CONCURRENT=1 slot) - even general-only
            # requests started timing out. Splitting servers isn't about recaptcha isolation
            # after all, it's just about not having 6 accounts x 2 types queue for one slot.
            ws_url, ws_token = (BROWSERLESS_WS_URL_UNIVERSE, BROWSERLESS_TOKEN_UNIVERSE) if barcode_type == "universe" else (BROWSERLESS_WS_URL, BROWSERLESS_TOKEN)
            browser = p.chromium.connect_over_cdp(f"{ws_url}?token={ws_token}&stealth=true&timeout=60000", timeout=20000)
            mark("connected_browserless")
            context = browser.new_context(viewport={"width": 412, "height": 915}, user_agent=MOBILE_USER_AGENT, is_mobile=True, has_touch=True)
            page = context.new_page(); page.set_default_timeout(6000)
            if barcode_type == "general":
                stage = "open_tworld_login"; mark(stage)
                goto_page(page, TWORLD_LOGIN_URL, timeout=12000)
                page.wait_for_timeout(900)
                mark("after_goto_tworld_login")
                if "m.tworld.co.kr" not in safe_url(page):
                    wait_for_tid_login_form(page, 10000)
                    stage = "type_tworld_tid_credentials"; mark(stage)
                    submit_tid_credentials(page, target, "tworld")
                    mark("after_submit_tworld_tid_credentials")
                    result = wait_for_tworld_result(page, 12000)
                    # The idpw retry can itself run ~15-20s uninterrupted (no mark() check
                    # inside it), so skip it once there isn't enough budget headroom left to
                    # absorb that on top of the current elapsed time - better a clean early
                    # failure than running past Vercel's own 60s hard kill.
                    if result == "timeout" and "auth.skt-id.co.kr" in safe_url(page) and (time.monotonic() - started) < (SCRAPE_BUDGET_SECONDS - 15):
                        stage = "retry_tworld_idpw_login"; mark(stage)
                        ensure_idpw_login_mode(page)
                        submit_tid_credentials(page, target, "tworld-retry")
                        result = wait_for_tworld_result(page, 12000)
                        mark("after_retry_tworld_idpw_login")
                else:
                    result = "callback"
                print(f"debug tworld login result={result} url={safe_url(page)}", flush=True)
                if debug_mode and result == "timeout":
                    return diagnostic_response(page, context, account_id, result, time.monotonic() - started)
                stage = "open_tworld_my"; mark(stage)
                goto_page(page, TWORLD_MY_URL, timeout=12000)
                wait_for_my_ready(page, 6000)
                print(f"debug final tworld url={safe_url(page)} body={get_body_text(page, 260)}", flush=True)
                stage = "fetch_tworld_membership_data"; mark(stage)
                barcode_api = fetch_tworld_membership_data(page)
                if force_scrape:
                    barcode_api = poll_for_fresh_barcode(fetch_tworld_membership_data, page, mark, started, barcode_api, SCRAPE_BUDGET_SECONDS)
                print(f"debug tworld membership api={barcode_api}", flush=True)
                if barcode_api.get("number"):
                    grade = barcode_api.get("grade") or extract_membership_grade(page)
                    set_cached_barcode(account_id, barcode_api["number"], barcode_api.get("seconds_left", 20 * 60), barcode_type, grade)
                    return barcode_response(barcode_api["number"], barcode_api.get("seconds_left", 20 * 60), grade)
                visible_number = extract_barcode_number(page)
                visible_seconds = extract_seconds_left(page)
                visible_grade = extract_membership_grade(page)
                print(f"debug tworld visible barcode number={visible_number} seconds={visible_seconds} grade={visible_grade}", flush=True)
                if visible_number:
                    set_cached_barcode(account_id, visible_number, visible_seconds, barcode_type, visible_grade)
                    return barcode_response(visible_number, visible_seconds, visible_grade)
                return image_response(
                    f"Tworld membership barcode not found\nID: {account_id}\nstate={barcode_api.get('membership_state')}\nresp={barcode_api.get('resp_code')}\nmessage={barcode_api.get('message') or barcode_api.get('raw')}"
                ), 502
            else:
                # Direct T-ID/recaptcha login, in one shot. The self-hosted server now has real
                # (non-throttled) CPU with headroom to spare, so the full flow - previously only
                # reliable in ~50-58s on a weak 1 OCPU box, too close to Vercel's 60s ceiling -
                # should comfortably fit in a single request again.
                stage = "open_tid_from_my"; mark(stage)
                open_tid_from_my(page, mark)
                mark("after_open_tid_from_my")
                wait_for_tid_login_form(page, 8000)
                stage = "type_tid_credentials"; mark(stage)
                submit_tid_credentials(page, target)
                mark("after_submit_tid_credentials")
                result = wait_for_tid_result(page, 10000)
                if result == "timeout" and "auth.skt-id.co.kr" in safe_url(page) and (time.monotonic() - started) < (SCRAPE_BUDGET_SECONDS - 15):
                    stage = "retry_tid_idpw_login"; mark(stage)
                    ensure_idpw_login_mode(page)
                    submit_tid_credentials(page, target, "retry")
                    result = wait_for_tid_result(page, 10000)
                    mark("after_retry_tid_idpw_login")
                print(f"debug tid submit result={result} url={safe_url(page)}", flush=True)
                if debug_mode and result == "timeout":
                    return diagnostic_response(page, context, account_id, result, time.monotonic() - started)
                stage = "open_my_after_login"; mark(stage)
                goto_page(page, MY_PAGE_URL, timeout=9000)
                wait_for_my_ready(page, 5000)
                print(f"debug final my url={safe_url(page)} body={get_body_text(page, 260)}", flush=True)
                stage = "fetch_barcode_data"; mark(stage)
                barcode_api = fetch_barcode_data(page)
                if force_scrape:
                    barcode_api = poll_for_fresh_barcode(fetch_barcode_data, page, mark, started, barcode_api, SCRAPE_BUDGET_SECONDS)
                print(f"debug barcode api={barcode_api}", flush=True)
                if barcode_api.get("number"):
                    set_cached_barcode(account_id, barcode_api["number"], barcode_api.get("seconds_left", 20 * 60), barcode_type)
                    return barcode_response(barcode_api["number"], barcode_api.get("seconds_left", 20 * 60))
                if barcode_api.get("code") in ["MSG0115", "MSG0116", "MSG0118", "MSG0120"]:
                    return image_response(
                        f"T membership is required for barcode\nID: {account_id}\ncode={barcode_api.get('code')}\nmessage={barcode_api.get('message') or barcode_api.get('raw')}"
                    ), 409
                if barcode_api.get("code") == "MSG0998":
                    return image_response(
                        f"T membership card cancellation is in progress\nID: {account_id}\ncode={barcode_api.get('code')}\nmessage={barcode_api.get('message') or barcode_api.get('raw')}"
                    ), 409
            stage = "open_barcode_view"; mark(stage)
            if time.monotonic() - started > 52:
                return image_response(f"Barcode timed out before opening view\nID: {account_id}\nelapsed={time.monotonic() - started:.1f}s"), 504
            barcode_result = open_barcode_view(page, started + 52)
            barcode_number = ""
            for _ in range(10):
                barcode_number = extract_barcode_number(page)
                if barcode_number: break
                page.wait_for_timeout(300)
            seconds_left = extract_seconds_left(page)
            print(f"debug barcode open result={barcode_result} number={barcode_number} seconds={seconds_left} url={safe_url(page)} body={get_body_text(page, 260)}", flush=True)
            if debug_mode:
                return diagnostic_response(page, context, account_id, f"{result}; barcode={barcode_result}; number={barcode_number}; seconds={seconds_left}", time.monotonic() - started)
            if barcode_number:
                set_cached_barcode(account_id, barcode_number, seconds_left)
                return barcode_response(barcode_number, seconds_left)
            return image_response(f"Barcode number not found\nID: {account_id}\nbarcode_result={barcode_result}\nurl={safe_url(page)}\nbody={get_body_text(page, 260)}"), 502
    except ScrapeTimeout as exc:
        print(f"Scrape timed out for {account_id} at {stage}: elapsed={time.monotonic() - started:.1f}s", flush=True)
        return image_response(f"Barcode scrape exceeded {SCRAPE_BUDGET_SECONDS}s budget\nID: {account_id}\nstage={stage}"), 504
    except Exception as exc:
        print(f"Error processing {account_id} at {stage}: {type(exc).__name__}: {exc}", flush=True)
        return image_response(f"Barcode failed\nID: {account_id}\n{stage}: {type(exc).__name__}: {exc}"), 502
    finally:
        if has_alarm:
            signal.alarm(0)
        release_browser_lock(lock_token)
        if browser:
            try: browser.close()
            except Exception: pass
