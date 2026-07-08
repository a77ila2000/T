from flask import Flask, request, Response
import os, json, base64, io, time, re, urllib.request, urllib.error
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


def get_cached_barcode(account_id, barcode_type="universe"):
    barcode_type = normalize_barcode_type(barcode_type)
    memory_key = f"{barcode_type}:{account_id}"
    cached = BARCODE_CACHE.get(memory_key)
    now = time.time()
    if cached and cached.get("expires_at", 0) > now + 5:
        return cached

    raw = redis_command(["GET", cache_key(account_id, barcode_type)])
    if not raw and barcode_type == "universe":
        raw = redis_command(["GET", f"barcode:{account_id}"])
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if value.get("expires_at", 0) <= now + 5:
            return None
        BARCODE_CACHE[memory_key] = value
        return value
    except Exception as exc:
        print(f"redis cache parse failed: {exc}", flush=True)
        return None


def set_cached_barcode(account_id, number, seconds_left, barcode_type="universe"):
    barcode_type = normalize_barcode_type(barcode_type)
    ttl = max(1, int(seconds_left or 1200))
    value = {"number": str(number), "expires_at": time.time() + ttl}
    BARCODE_CACHE[f"{barcode_type}:{account_id}"] = value
    redis_command(["SET", cache_key(account_id, barcode_type), json.dumps(value), "EX", ttl])
    return value


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


def barcode_response(number, seconds_left=1200):
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
    resp.headers["X-Barcode-Seconds-Left"] = str(max(1, int(seconds_left or 1200)))
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


def wait_for_tid_login_form(page, timeout_ms=8000):
    end = time.monotonic() + timeout_ms / 1000
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
          const target = items.find((i) => /login/i.test(i.text)) || items[0];
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


@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    debug_mode = request.args.get("debug") == "1"
    cache_only = request.args.get("cache_only") == "1"
    barcode_type = normalize_barcode_type(request.args.get("type"))
    if not account_id:
        return "Account ID is required.", 400
    browser = None
    lock_token = None
    stage = "start"
    started = time.monotonic()
    def mark(label): print(f"debug elapsed={time.monotonic() - started:.1f}s stage={label}", flush=True)
    try:
        stage = "decrypt_accounts"; mark(stage)
        accounts = decrypt_accounts()
        target = next((acc for acc in accounts if acc["id"] == account_id), None)
        if not target:
            return f"Account not found: {account_id}", 404
        cached = get_cached_barcode(account_id, barcode_type)
        if not debug_mode and cached:
            return barcode_response(cached["number"], int(cached["expires_at"] - time.time()))
        if cache_only:
            return "No cached barcode", 404
        lock_token = acquire_browser_lock(account_id)
        if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN and not lock_token:
            cached = get_cached_barcode(account_id, barcode_type)
            if not debug_mode and cached:
                return barcode_response(cached["number"], int(cached["expires_at"] - time.time()))
            resp = image_response(f"Another barcode refresh is already running\nID: {account_id}\nRetry shortly", color=(255, 248, 225))
            resp.status_code = 423
            resp.headers["Retry-After"] = "20"
            return resp
        with sync_playwright() as p:
            stage = "connect_browserless"; mark(stage)
            browser = p.chromium.connect_over_cdp(f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=60000", timeout=8000)
            context = browser.new_context(viewport={"width": 412, "height": 915}, user_agent=MOBILE_USER_AGENT, is_mobile=True, has_touch=True)
            page = context.new_page(); page.set_default_timeout(6000)
            stage = "open_tid_from_my"; mark(stage)
            open_tid_from_my(page)
            wait_for_tid_login_form(page, 8000)
            stage = "type_tid_credentials"; mark(stage)
            print("typed user selector:", type_first_visible(page, ["input#inputId", "input#userId", "input[name='userId']", "input[name='id']", "input[type='email']", "input[type='text']"], target["id"], 6000), flush=True)
            print("typed password selector:", type_first_visible(page, ["input#inputPassword", "input#password", "input[name='password']", "input[name='passwd']", "input[type='password']"], target["password"], 6000), flush=True)
            stage = "submit_tid_login"; mark(stage)
            try:
                page.locator("button:has-text('Login'), input[type='submit'], button").last.click(force=True, timeout=2200)
                print("debug locator force click submit", flush=True)
            except Exception as exc:
                print(f"login button locator failed: {exc}", flush=True)
            page.wait_for_timeout(200); physical_tap_at(page, 206, 470); page.wait_for_timeout(200); force_submit(page)
            result = wait_for_tid_result(page, 10000)
            print(f"debug tid submit result={result} url={safe_url(page)}", flush=True)
            if debug_mode and result == "timeout":
                return diagnostic_response(page, context, account_id, result, time.monotonic() - started)
            if barcode_type == "general":
                stage = "open_tworld_after_login"; mark(stage)
                open_tworld_after_tid_login(page, target)
                print(f"debug final tworld url={safe_url(page)} body={get_body_text(page, 260)}", flush=True)
                stage = "fetch_tworld_membership_data"; mark(stage)
                barcode_api = fetch_tworld_membership_data(page)
                print(f"debug tworld membership api={barcode_api}", flush=True)
                if barcode_api.get("number"):
                    set_cached_barcode(account_id, barcode_api["number"], barcode_api.get("seconds_left", 20 * 60), barcode_type)
                    return barcode_response(barcode_api["number"], barcode_api.get("seconds_left", 20 * 60))
                return image_response(
                    f"Tworld membership barcode not found\nID: {account_id}\nstate={barcode_api.get('membership_state')}\nresp={barcode_api.get('resp_code')}\nmessage={barcode_api.get('message') or barcode_api.get('raw')}"
                ), 502
            else:
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
    except Exception as exc:
        print(f"Error processing {account_id} at {stage}: {type(exc).__name__}: {exc}", flush=True)
        return image_response(f"Barcode failed\nID: {account_id}\n{stage}: {type(exc).__name__}: {exc}"), 502
    finally:
        release_browser_lock(lock_token)
        if browser:
            try: browser.close()
            except Exception: pass
