from flask import Flask, request, Response
import os, json, base64, io, time, re, signal, urllib.request, urllib.error
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright

app = Flask(__name__)
ENCRYPTION_KEY_B64 = os.environ.get("ENCRYPTION_KEY")
ENCRYPTED_ACCOUNTS_B64 = os.environ.get("ENCRYPTED_ACCOUNTS")
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "2Uq9iBy84O6QGwO008597820ed94cb8fb02789f1092d91545")
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
MY_PAGE_URL = "https://m.sktuniverse.co.kr/my"
LOGIN_VIEW_URL = "https://m.sktuniverse.co.kr/member/login/view?loginRedirectUrl=%2Fmy"
TID_AUTHORIZE_URL = "https://tapi.t-id.co.kr/oidc/v20/authorize?client_id=a1c144a9-6ab3-49f3-b03f-4ce80d257f16&redirect_uri=https%3A%2F%2Fm.sktuniverse.co.kr%2Fmember%2Flogin%2Fchannel/tid"
TWORLD_MY_URL = "https://m.tworld.co.kr/v6/my?returnUrl=https://m.tworld.co.kr/v6/main"
TWORLD_LOGIN_URL = "https://m.tworld.co.kr/common/tid/login?target=/v6/my"
MOBILE_USER_AGENT = "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
BARCODE_CACHE = {}
WARM_TARGETS = [
    {"id": "a77ila2000", "type": "universe", "name": "me-universe"},
    {"id": "a77ila10004", "type": "universe", "name": "mother-universe"},
    {"id": "min560728", "type": "universe", "name": "father-universe"},
    {"id": "a77ila2000", "type": "general", "name": "me-general"},
    {"id": "a77ila10004", "type": "general", "name": "mother-general"},
    {"id": "min560728", "type": "general", "name": "father-general"},
]
WARM_SUCCESS_INTERVAL = 20 * 60
WARM_FAIL_INTERVAL = 30
WARM_STAGGER_INTERVAL = 3 * 60
# Only pull a schedule earlier for sibling separation when it's already due soon - otherwise
# a barcode with plenty of real validity left gets force-refreshed early just to dodge a
# sibling collision that's still many minutes away and may not even matter by then.
WARM_STAGGER_APPLY_WINDOW = 5 * 60
# Must stay close to get_barcode.py/warm_tick.py's vercel.json maxDuration (60s): if Vercel
# kills a scrape for running too long, nothing releases the lock, so it can only self-heal
# once its TTL expires. A lock TTL far beyond maxDuration (the old value was 170s) means a
# single killed attempt can block every other target for minutes.
WARM_LOCK_TTL = 75
WARM_CURRENT_TTL = 90
LAST_BARCODE_RETENTION = 7 * 24 * 60 * 60
# Vercel Runtime Timeout Error logs showed /api/warm_tick hitting the platform's own 60s
# maxDuration and getting killed outright - which skips our except/finally entirely, so
# release_browser_lock() and record_warm_result() never run. Raise our own timeout a few
# seconds earlier so a slow scrape always fails through normal Python control flow instead.
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


def normalize_barcode_type(value):
    return "general" if value in ("general", "normal", "tworld") else "universe"


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


def acquire_browser_lock(account_id):
    token = f"{account_id}:{time.time()}"
    result = redis_command(["SET", browser_lock_key(), token, "EX", 90, "NX"])
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


def get_cached_barcode(account_id, barcode_type="universe", allow_stale=False):
    barcode_type = normalize_barcode_type(barcode_type)
    memory_key = f"{barcode_type}:{account_id}"
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
        if value["stale"] and not allow_stale:
            return None
        BARCODE_CACHE[memory_key] = value
        return value
    except Exception as exc:
        print(f"redis cache parse failed: {exc}", flush=True)
        return None


def compute_failure_retry_delay(existing):
    # First failure after a success (or a fresh target) retries immediately so it doesn't
    # lose its place behind targets on the normal ~20 minute cycle. A second consecutive
    # failure backs off by WARM_FAIL_INTERVAL (30s) instead of retrying instantly again,
    # so any other already-due target gets a turn first; if nothing else is due, this
    # target simply comes back around after the 30s cool-down.
    consecutive_failures = int(existing.get("consecutive_failures", 0)) + 1
    delay = 0 if consecutive_failures == 1 else WARM_FAIL_INTERVAL
    return delay, consecutive_failures


def set_cached_barcode(account_id, number, seconds_left, barcode_type="universe", grade=""):
    barcode_type = normalize_barcode_type(barcode_type)
    ttl = max(1, int(seconds_left or 1200))
    value = {
        "number": str(number),
        "expires_at": time.time() + ttl,
        "grade": str(grade or ""),
        "created_at": time.time(),
    }
    BARCODE_CACHE[f"{barcode_type}:{account_id}"] = value
    write_result = redis_command(["SET", cache_key(account_id, barcode_type), json.dumps(value), "EX", LAST_BARCODE_RETENTION])
    target = next((item for item in WARM_TARGETS if item["id"] == account_id and item["type"] == barcode_type), None)
    if target:
        now = int(time.time())
        existing = get_warm_state(target)
        if write_result == "OK":
            next_refresh_at = now + min(ttl, WARM_SUCCESS_INTERVAL)
            sibling_type = "general" if barcode_type == "universe" else "universe"
            sibling = next((item for item in WARM_TARGETS if item["id"] == account_id and item["type"] == sibling_type), None)
            if sibling and next_refresh_at - now <= WARM_STAGGER_APPLY_WINDOW:
                sibling_next = int(get_warm_state(sibling).get("next_refresh_at") or 0)
                if sibling_next and abs(next_refresh_at - sibling_next) < WARM_STAGGER_INTERVAL:
                    # Only ever pull the schedule earlier for separation, never push it later -
                    # delaying past this barcode's own real expiry would leave it stuck stale
                    # while the scheduler still thinks nothing is due. Also only do this when
                    # the natural refresh is already due soon (see WARM_STAGGER_APPLY_WINDOW) -
                    # otherwise a barcode with plenty of real validity left gets force-refreshed
                    # early just to dodge a collision that's still many minutes away.
                    next_refresh_at = max(now, min(next_refresh_at, sibling_next - WARM_STAGGER_INTERVAL))
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


def select_warm_target(now=None):
    now = int(now or time.time())
    selected = None
    selected_state = None
    selected_due = None
    for index, target in enumerate(WARM_TARGETS):
        state = get_warm_state(target)
        due_at = int(state.get("next_refresh_at") or 0)
        if due_at > now:
            continue
        if selected is None or due_at < selected_due or (due_at == selected_due and index < selected.get("index", 0)):
            selected = dict(target, index=index)
            selected_state = state
            selected_due = due_at
    return selected, selected_state or {}


def json_response(payload, status=200):
    return Response(json.dumps(payload, ensure_ascii=False), status=status, mimetype="application/json")


def acquire_warm_lock():
    token = f"warm:{time.time()}"
    result = redis_command(["SET", warm_lock_key(), token, "EX", WARM_LOCK_TTL, "NX"])
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


def image_response(text, color=(255, 235, 238)):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (760, 300), color=color)
    draw = ImageDraw.Draw(img)
    msg = str(text).replace("\n", " ")[:430]
    draw.multiline_text((18, 34), "\n".join(msg[i:i + 76] for i in range(0, len(msg), 76)), fill=(211, 47, 47))
    out = io.BytesIO(); img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")


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


def screenshot_bytes(page):
    client = page.context.new_cdp_session(page)
    return base64.b64decode(client.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"])


def screenshot_response(page):
    try: return Response(screenshot_bytes(page), mimetype="image/png")
    except Exception as exc: return image_response(f"screenshot failed: {exc}. url={safe_url(page)} body={get_body_text(page)}")


def barcode_response(number, seconds_left=1200, grade="", stale=False):
    from PIL import Image, ImageDraw, ImageFont
    number = re.sub(r"\D", "", str(number))
    if not number:
        return image_response("Barcode number is empty")
    codes = [104] + [ord(ch) - 32 for ch in number]
    checksum = 104 + sum(value * idx for idx, value in enumerate(codes[1:], 1))
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
    resp = Response(out.getvalue(), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Barcode-Number"] = number
    resp.headers["X-Barcode-Seconds-Left"] = str(max(0, int(seconds_left or 0)))
    resp.headers["X-Barcode-Stale"] = "1" if stale else "0"
    resp.headers["X-Barcode-Status"] = "stale" if stale else "valid"
    if grade:
        resp.headers["X-Membership-Grade"] = str(grade)
    return resp


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
        seconds_left = int(data.get("expireSeconds") or 20 * 60)
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
    try:
        result = page.evaluate("""
        async () => {
          const response = await fetch('/common/my/tmembership', {
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
        number = re.sub(r"\D", "", str(data.get("otbNum") or data.get("barcode") or ""))
        seconds_left = int(data.get("expireSeconds") or 20 * 60)
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
        if "/member/login/channel/tid" in url or "code=" in url or "/my" in url:
            print(f"debug T ID callback reached: {url}", flush=True)
            page.wait_for_timeout(800)
            return "callback"
        time.sleep(0.2)
    print(f"debug T ID result wait timed out at url={safe_url(page)} body={get_body_text(page, 200)}", flush=True)
    return "timeout"


def wait_for_tworld_result(page, timeout_ms=12000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        if "m.tworld.co.kr" in safe_url(page):
            page.wait_for_timeout(800)
            return "callback"
        time.sleep(0.2)
    if "m.tworld.co.kr" in safe_url(page):
        page.wait_for_timeout(800)
        return "callback"
    print(f"debug T world result wait timed out at url={safe_url(page)} body={get_body_text(page, 200)}", flush=True)
    return "timeout"


def open_tid_from_my(page):
    goto_page(page, MY_PAGE_URL, timeout=7000)
    page.wait_for_timeout(400)
    goto_page(page, LOGIN_VIEW_URL, timeout=8000, referer=MY_PAGE_URL)
    page.wait_for_timeout(600)
    referer = safe_url(page) if "sktuniverse" in safe_url(page) else LOGIN_VIEW_URL
    print(f"debug direct authorize from login view url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
    goto_page(page, TID_AUTHORIZE_URL, timeout=12000, referer=referer)
    page.wait_for_timeout(600)
    if "auth.skt-id.co.kr" not in safe_url(page) and "tapi.t-id.co.kr" not in safe_url(page):
        physical_tap_at(page, 206, 470)
        page.wait_for_timeout(900)
        print(f"debug direct authorize retry after coordinate tap url={safe_url(page)}", flush=True)
        goto_page(page, TID_AUTHORIZE_URL, timeout=12000, referer=referer)
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


def open_tworld_after_tid_login(page, target):
    goto_page(page, TWORLD_LOGIN_URL, timeout=12000)
    page.wait_for_timeout(1800)
    if "auth.skt-id.co.kr" in safe_url(page):
        try:
            wait_for_tid_login_form(page, 2500)
            print("debug tworld requested T ID login again", flush=True)
            print("typed user selector:", type_first_visible(page, ["input#inputId", "input#userId", "input[name='userId']", "input[name='id']", "input[type='email']", "input[type='text']"], target["id"], 6000), flush=True)
            print("typed password selector:", type_first_visible(page, ["input#inputPassword", "input#password", "input[name='password']", "input[name='passwd']", "input[type='password']"], target["password"], 6000), flush=True)
            try:
                page.locator("button:has-text('Login'), input[type='submit'], button").last.click(force=True, timeout=2200)
            except Exception as exc:
                print(f"tworld login button locator failed: {exc}", flush=True)
            page.wait_for_timeout(200)
            physical_tap_at(page, 206, 470)
            page.wait_for_timeout(200)
            force_submit(page)
        except Exception as exc:
            print(f"debug tworld T ID relogin skipped: {type(exc).__name__}: {exc}", flush=True)
    end = time.monotonic() + 12
    while time.monotonic() < end:
        if "m.tworld.co.kr" in safe_url(page):
            break
        page.wait_for_timeout(300)
    if "m.tworld.co.kr" not in safe_url(page):
        goto_page(page, TWORLD_MY_URL, timeout=12000)
    page.wait_for_timeout(1200)
    wait_for_my_ready(page, 6000)


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
    try:
        page.locator("button:has-text('로그인'), button:has-text('Login'), input[type='submit'], button").last.click(force=True, timeout=2200)
        print(prefix + "debug locator force click submit", flush=True)
    except Exception as exc:
        print(prefix + f"login button locator failed: {exc}", flush=True)
    page.wait_for_timeout(200)
    physical_tap_at(page, 206, 470)
    page.wait_for_timeout(200)
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
        print(f"debug barcode dom click={clicked} url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
        if safe_url(page) != before_url or get_body_text(page, 220) != before_text:
            return f"dom:{clicked}"
    except Exception as exc:
        print(f"debug barcode dom click failed: {exc}", flush=True)
    for x, y in [(300, 32), (320, 32), (340, 32), (380, 32), (292, 104), (332, 104), (372, 104)]:
        if deadline and time.monotonic() > deadline:
            return "deadline-before-barcode-tap"
        physical_tap_at(page, x, y)
        page.wait_for_timeout(1300)
        print(f"debug barcode coordinate tapped {x},{y} url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
        if extract_barcode_number(page) or safe_url(page) != before_url or get_body_text(page, 220) != before_text:
            return f"tap({x},{y})"
    return "not-opened"


@app.route("/api/warm_next", methods=["GET"])
def warm_next():
    now = int(time.time())
    lock_token = acquire_warm_lock()
    if not lock_token:
        return json_response({"status": "locked", "retry_after": 60})
    target, state = select_warm_target(now)
    if not target:
        release_warm_lock(lock_token)
        next_due = None
        states = []
        for item in WARM_TARGETS:
            item_state = get_warm_state(item)
            due_at = int(item_state.get("next_refresh_at") or 0)
            if due_at:
                states.append(due_at)
        if states:
            next_due = min(states)
        return json_response({"status": "no_due", "now": now, "next_due_at": next_due})
    redis_command(["SET", warm_current_key(), json.dumps({
        "token": lock_token,
        "started_at": now,
        "id": target["id"],
        "type": target["type"],
        "name": target["name"],
    }), "EX", WARM_CURRENT_TTL])
    return json_response({
        "status": "ok",
        "now": now,
        "token": lock_token,
        "id": target["id"],
        "type": target["type"],
        "name": target["name"],
        "next_refresh_at": int(state.get("next_refresh_at") or 0),
    })


def record_warm_result(target, token, success, http_code=""):
    now = int(time.time())
    existing = get_warm_state(target)
    if success:
        # set_cached_barcode already recorded the correct next_refresh_at for this
        # refresh (ttl-aware and staggered against the sibling barcode type) as part
        # of the get_barcode call - don't clobber it with a flat +20m here.
        state = dict(existing)
        state.setdefault("next_refresh_at", now + WARM_SUCCESS_INTERVAL)
        state["last_http_code"] = http_code
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
    release_warm_lock(token)
    return state


@app.route("/api/warm_done", methods=["GET", "POST"])
def warm_done():
    account_id = request.values.get("id")
    barcode_type = normalize_barcode_type(request.values.get("type"))
    token = request.values.get("token")
    success = request.values.get("success") == "1"
    http_code = request.values.get("http_code", "")
    target = next((item for item in WARM_TARGETS if item["id"] == account_id and item["type"] == barcode_type), None)
    if not target:
        release_warm_lock(token)
        return json_response({"status": "unknown_target"}, 404)
    state = record_warm_result(target, token, success, http_code)
    return json_response({"status": "recorded", "success": success, "next_refresh_at": state["next_refresh_at"]})


@app.route("/api/warm_tick", methods=["GET", "POST"])
def warm_tick():
    now = int(time.time())
    lock_token = acquire_warm_lock()
    if not lock_token:
        return json_response({"status": "locked", "retry_after": 60})
    target, _ = select_warm_target(now)
    if not target:
        release_warm_lock(lock_token)
        return json_response({"status": "no_due", "now": now})
    redis_command(["SET", warm_current_key(), json.dumps({
        "token": lock_token,
        "started_at": now,
        "id": target["id"],
        "type": target["type"],
        "name": target["name"],
    }), "EX", WARM_CURRENT_TTL])
    try:
        result = perform_barcode_request(target["id"], target["type"])
        if isinstance(result, tuple):
            body, http_code = result[0], result[1]
        else:
            body, http_code = result, getattr(result, "status_code", 200)
        content_type = getattr(body, "mimetype", "") or ""
        success = http_code == 200 and content_type.startswith("image/")
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
    now = int(time.time())
    current_raw = redis_command(["GET", warm_current_key()])
    current = None
    if current_raw:
        try:
            current = json.loads(current_raw)
        except Exception:
            current = None
    targets = []
    for item in WARM_TARGETS:
        state = get_warm_state(item)
        cached = get_cached_barcode(item["id"], item["type"], allow_stale=True)
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
        })
    return json_response({"status": "ok", "now": now, "current": current, "targets": targets})


@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    if not account_id:
        return "Account ID is required.", 400
    debug_mode = request.args.get("debug") == "1"
    cache_only = request.args.get("cache_only") == "1"
    barcode_type = normalize_barcode_type(request.args.get("type"))
    return perform_barcode_request(account_id, barcode_type, debug_mode=debug_mode, cache_only=cache_only)


def perform_barcode_request(account_id, barcode_type, debug_mode=False, cache_only=False):
    browser = None
    lock_token = None
    stage = "start"
    started = time.monotonic()
    def mark(label): print(f"debug elapsed={time.monotonic() - started:.1f}s stage={label}", flush=True)
    has_alarm = False
    if hasattr(signal, "alarm"):
        try:
            signal.signal(signal.SIGALRM, _scrape_alarm_handler)
            signal.alarm(SCRAPE_BUDGET_SECONDS)
            has_alarm = True
        except Exception as exc:
            print(f"debug scrape alarm unavailable: {type(exc).__name__}: {exc}", flush=True)
    try:
        stage = "decrypt_accounts"; mark(stage)
        accounts = decrypt_accounts()
        target = next((acc for acc in accounts if acc["id"] == account_id), None)
        if not target:
            return f"Account not found: {account_id}", 404
        cached = get_cached_barcode(account_id, barcode_type)
        if not debug_mode and cached:
            return barcode_response(cached["number"], cached.get("seconds_left", 0), cached.get("grade", ""), cached.get("stale", False))
        if cache_only:
            cached = get_cached_barcode(account_id, barcode_type, allow_stale=True)
            if not debug_mode and cached:
                return barcode_response(cached["number"], cached.get("seconds_left", 0), cached.get("grade", ""), cached.get("stale", False))
            return "No cached barcode", 404
        lock_token = acquire_browser_lock(account_id)
        if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN and not lock_token:
            cached = get_cached_barcode(account_id, barcode_type, allow_stale=True)
            if not debug_mode and cached:
                return barcode_response(cached["number"], cached.get("seconds_left", 0), cached.get("grade", ""), cached.get("stale", False))
            resp = image_response(f"Another barcode refresh is already running\nID: {account_id}\nRetry shortly", color=(255, 248, 225))
            resp.status_code = 423
            resp.headers["Retry-After"] = "75"
            return resp
        with sync_playwright() as p:
            stage = "connect_browserless"; mark(stage)
            browser = p.chromium.connect_over_cdp(f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=60000", timeout=8000)
            context = browser.new_context(viewport={"width": 412, "height": 915}, user_agent=MOBILE_USER_AGENT, is_mobile=True, has_touch=True)
            page = context.new_page(); page.set_default_timeout(6000)
            if barcode_type == "general":
                stage = "open_tworld_login"; mark(stage)
                goto_page(page, TWORLD_LOGIN_URL, timeout=12000)
                page.wait_for_timeout(900)
                if "m.tworld.co.kr" not in safe_url(page):
                    wait_for_tid_login_form(page, 10000)
                    stage = "type_tworld_tid_credentials"; mark(stage)
                    submit_tid_credentials(page, target, "tworld")
                    result = wait_for_tworld_result(page, 12000)
                    if result == "timeout" and "auth.skt-id.co.kr" in safe_url(page):
                        stage = "retry_tworld_idpw_login"; mark(stage)
                        ensure_idpw_login_mode(page)
                        submit_tid_credentials(page, target, "tworld-retry")
                        result = wait_for_tworld_result(page, 12000)
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
                stage = "open_tid_from_my"; mark(stage)
                open_tid_from_my(page)
                wait_for_tid_login_form(page, 8000)
                stage = "type_tid_credentials"; mark(stage)
                submit_tid_credentials(page, target)
                result = wait_for_tid_result(page, 10000)
                if result == "timeout" and "auth.skt-id.co.kr" in safe_url(page):
                    stage = "retry_tid_idpw_login"; mark(stage)
                    ensure_idpw_login_mode(page)
                    submit_tid_credentials(page, target, "retry")
                    result = wait_for_tid_result(page, 10000)
                print(f"debug tid submit result={result} url={safe_url(page)}", flush=True)
                if debug_mode and result == "timeout":
                    return diagnostic_response(page, context, account_id, result, time.monotonic() - started)
                stage = "open_my_after_login"; mark(stage)
                goto_page(page, MY_PAGE_URL, timeout=9000)
                wait_for_my_ready(page, 5000)
                print(f"debug final my url={safe_url(page)} body={get_body_text(page, 260)}", flush=True)
                stage = "fetch_barcode_data"; mark(stage)
                barcode_api = fetch_barcode_data(page)
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
