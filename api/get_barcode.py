from flask import Flask, request, Response
import os, json, base64, io, time
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright

app = Flask(__name__)
ENCRYPTION_KEY_B64 = os.environ.get("ENCRYPTION_KEY")
ENCRYPTED_ACCOUNTS_B64 = os.environ.get("ENCRYPTED_ACCOUNTS")
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "2Uq9iBy84O6QGwO008597820ed94cb8fb02789f1092d91545")
MY_PAGE_URL = "https://m.sktuniverse.co.kr/my"
LOGIN_VIEW_URL = "https://m.sktuniverse.co.kr/member/login/view?loginRedirectUrl=%2Fmy"
TID_AUTHORIZE_URL = "https://tapi.t-id.co.kr/oidc/v20/authorize?client_id=a1c144a9-6ab3-49f3-b03f-4ce80d257f16&redirect_uri=https%3A%2F%2Fm.sktuniverse.co.kr%2Fmember%2Flogin%2Fchannel/tid"
MOBILE_USER_AGENT = "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"


def decrypt_accounts():
    key = base64.urlsafe_b64decode(ENCRYPTION_KEY_B64)
    encrypted = base64.urlsafe_b64decode(ENCRYPTED_ACCOUNTS_B64)
    return json.loads(Fernet(key).decrypt(encrypted).decode("utf-8"))


def image_response(text, color=(255, 235, 238)):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (760, 300), color=color); draw = ImageDraw.Draw(img)
    msg = str(text).replace("\n", " ")[:430]
    draw.multiline_text((18, 34), "\n".join(msg[i:i + 76] for i in range(0, len(msg), 76)), fill=(211, 47, 47))
    out = io.BytesIO(); img.save(out, format="PNG"); return Response(out.getvalue(), mimetype="image/png")


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
        print(f"debug goto ignored url={url} current={safe_url(page)} error={type(exc).__name__}: {exc}", flush=True); return None


def screenshot_bytes(page):
    client = page.context.new_cdp_session(page)
    return base64.b64decode(client.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"])


def screenshot_response(page):
    try: return Response(screenshot_bytes(page), mimetype="image/png")
    except Exception as exc: return image_response(f"screenshot failed: {exc}. url={safe_url(page)} body={get_body_text(page)}")


def cookie_lines(context):
    try: cookies = context.cookies()
    except Exception as exc: return [f"cookie read failed: {exc}"]
    interesting = [c for c in cookies if any(x in c.get("domain", "") for x in ["sktuniverse", "t-id.co.kr", "skt-id.co.kr"])]
    lines = [f"interesting cookies: {len(interesting)} / total {len(cookies)}"]
    for c in sorted(interesting, key=lambda v: (v.get("domain", ""), v.get("name", ""))):
        flags = []
        if c.get("httpOnly"): flags.append("HttpOnly")
        if c.get("secure"): flags.append("Secure")
        if c.get("sameSite"): flags.append(f"SameSite={c.get('sameSite')}")
        lines.append(f"{c.get('name')} @ {c.get('domain')} path={c.get('path')} len={len(c.get('value', ''))} {' '.join(flags)}")
    return lines


def install_network_debug(page, events):
    skip = ["google", "analytics", "doubleclick", "facebook", "criteo", "clarity", ".css", ".js", ".woff", ".png", ".jpg", ".gif"]
    focus = ["skt", "t-id", "tid", "member", "login", "my", "user", "User", "auth", "barcode", "coupon", "benefit", "subscription"]
    def keep(url, rtype):
        lower = url.lower()
        if any(s in lower for s in skip): return False
        if rtype in ["xhr", "fetch", "document"]: return True
        return any(f.lower() in lower for f in focus)
    def trim(url): return url.replace("https://", "")[:180]
    def on_response(resp):
        try:
            req = resp.request; url = resp.url
            if keep(url, req.resource_type):
                events.append(f"{resp.status} {req.resource_type} {req.method} {trim(url)}")
                if len(events) > 70: del events[:-70]
        except Exception as exc: events.append(f"response-log-error {exc}")
    def on_failed(req):
        try:
            if keep(req.url, req.resource_type):
                events.append(f"FAILED {req.resource_type} {req.method} {trim(req.url)} {req.failure}")
                if len(events) > 70: del events[:-70]
        except Exception as exc: events.append(f"requestfailed-log-error {exc}")
    page.on("response", on_response); page.on("requestfailed", on_failed)


def diagnostic_response(page, context, account_id, result, elapsed, network_events=None):
    from PIL import Image, ImageDraw
    try: shot = Image.open(io.BytesIO(screenshot_bytes(page))).convert("RGB")
    except Exception: shot = Image.new("RGB", (412, 915), color=(245, 245, 245))
    img = Image.new("RGB", (shot.width + 920, max(shot.height, 1260)), color=(255, 255, 255))
    img.paste(shot, (0, 0)); draw = ImageDraw.Draw(img)
    x = shot.width + 18; y = 18
    rows = ["T Universe login diagnostic", f"account={account_id}", f"elapsed={elapsed:.1f}s result={result}", f"url={safe_url(page)}", f"body={get_body_text(page, 520)}", "", "cookies:"] + cookie_lines(context)
    if network_events is not None: rows += ["", "network:"] + network_events[-45:]
    for row in rows:
        row = str(row)
        for i in range(0, len(row), 106): draw.text((x, y), row[i:i + 106], fill=(20, 20, 20)); y += 18
        if y > img.height - 30: break
    out = io.BytesIO(); img.save(out, format="PNG"); return Response(out.getvalue(), mimetype="image/png")


def physical_tap_at(page, x, y):
    try: page.touchscreen.tap(x, y)
    except Exception as exc: print(f"touchscreen tap failed: {exc}", flush=True)
    try: page.mouse.click(x, y)
    except Exception as exc: print(f"mouse click failed: {exc}", flush=True)


def tap_locator(locator, timeout=5000):
    locator.wait_for(state="visible", timeout=timeout)
    try: locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception: pass
    box = locator.bounding_box(timeout=timeout)
    if not box: raise RuntimeError("no bounding box")
    physical_tap_at(locator.page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2); locator.page.wait_for_timeout(120)


def type_first_visible(page, selectors, value, timeout=6000):
    locator = page.locator(", ".join(selectors)).first; locator.wait_for(state="visible", timeout=timeout)
    tap_locator(locator, timeout); locator.fill("", timeout=timeout); locator.type(value, delay=10, timeout=timeout)
    return locator.evaluate("e => e.id || e.name || e.type || e.tagName")


def wait_for_tid_login_form(page, timeout_ms=8000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        try:
            if page.locator("input#inputId, input#userId, input[type='text']").first.is_visible(timeout=400): return
        except Exception: pass
        time.sleep(0.2)
    raise RuntimeError(f"T ID login form not visible. url={safe_url(page)} body={get_body_text(page, 220)}")


def force_submit(page):
    try: page.locator("input[type='password']").first.press("Enter", timeout=1200)
    except Exception as exc: print(f"debug password Enter failed: {exc}", flush=True)
    try:
        clicked = page.evaluate("""
        () => {
          const items = Array.from(document.querySelectorAll('button,input[type=submit],[role=button],a')).map((el) => {
            const r = el.getBoundingClientRect(); const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            return {el, text, top:r.top, width:r.width, height:r.height, disabled:!!el.disabled};
          }).filter((i) => i.width > 100 && i.height > 20 && i.top > 330 && !i.disabled);
          const target = items.find((i) => /로그인|login/i.test(i.text)) || items[0]; if (!target) return 'no-target';
          target.el.scrollIntoView({block:'center'}); const opts={bubbles:true,cancelable:true,view:window};
          target.el.dispatchEvent(new MouseEvent('mousedown', opts)); target.el.dispatchEvent(new MouseEvent('mouseup', opts)); target.el.dispatchEvent(new MouseEvent('click', opts)); target.el.click();
          return `${target.text}|${target.top}|${target.width}x${target.height}`;
        }
        """); print(f"debug dom submit click target={clicked}", flush=True)
    except Exception as exc: print(f"debug dom submit click failed: {exc}", flush=True)


def wait_for_tid_result(page, timeout_ms=10000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        url = safe_url(page)
        if page.is_closed(): return "closed"
        if "/member/login/channel/tid" in url or "code=" in url or "/my" in url:
            print(f"debug T ID callback reached: {url}", flush=True); page.wait_for_timeout(1000); return "callback"
        time.sleep(0.2)
    print(f"debug T ID result wait timed out at url={safe_url(page)} body={get_body_text(page, 200)}", flush=True); return "timeout"


def open_tid_from_my(page):
    goto_page(page, MY_PAGE_URL, timeout=7000); page.wait_for_timeout(500)
    goto_page(page, LOGIN_VIEW_URL, timeout=8000, referer=MY_PAGE_URL); page.wait_for_timeout(700)
    print(f"debug login view seeded url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
    before = safe_url(page)
    try:
        btn = page.locator("#link-to-tid-login, button:has-text('T 아이디'), button:has-text('T아이디'), text=T 아이디로 이용하기, text=T아이디로 이용하기").first
        tap_locator(btn, timeout=2500); print("debug tapped real T ID button", flush=True)
    except Exception as exc: print(f"debug real T ID button tap failed: {exc}", flush=True)
    page.wait_for_timeout(1200)
    if "auth.skt-id.co.kr" not in safe_url(page) and "tapi.t-id.co.kr" not in safe_url(page):
        referer = safe_url(page) if "sktuniverse" in safe_url(page) else LOGIN_VIEW_URL
        print(f"debug fallback authorize after button attempt url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
        goto_page(page, TID_AUTHORIZE_URL, timeout=12000, referer=referer); page.wait_for_timeout(300)
    print(f"debug tid entry url={safe_url(page)} from={before} body={get_body_text(page, 180)}", flush=True); return page


@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id"); debug_mode = request.args.get("debug") == "1"
    if not account_id: return "Account ID is required.", 400
    browser = None; stage = "start"; started = time.monotonic(); network_events = []
    def mark(label): print(f"debug elapsed={time.monotonic() - started:.1f}s stage={label}", flush=True)
    try:
        stage = "decrypt_accounts"; mark(stage)
        accounts = decrypt_accounts(); target = next((acc for acc in accounts if acc["id"] == account_id), None)
        if not target: return f"Account not found: {account_id}", 404
        with sync_playwright() as p:
            stage = "connect_browserless"; mark(stage)
            browser = p.chromium.connect_over_cdp(f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=60000", timeout=8000)
            context = browser.new_context(viewport={"width":412,"height":915}, user_agent=MOBILE_USER_AGENT, is_mobile=True, has_touch=True)
            page = context.new_page(); page.set_default_timeout(6000); install_network_debug(page, network_events)
            stage = "open_tid_from_my"; mark(stage); open_tid_from_my(page); wait_for_tid_login_form(page, 8000)
            stage = "type_tid_credentials"; mark(stage)
            user_selector = type_first_visible(page, ["input#inputId", "input#userId", "input[name='userId']", "input[name='id']", "input[type='email']", "input[type='text']"], target["id"], 6000); print(f"typed user selector: {user_selector}", flush=True)
            password_selector = type_first_visible(page, ["input#inputPassword", "input#password", "input[name='password']", "input[name='passwd']", "input[type='password']"], target["password"], 6000); print(f"typed password selector: {password_selector}", flush=True)
            stage = "submit_tid_login"; mark(stage)
            try:
                page.locator("button:has-text('로그인'), button:has-text('Login'), input[type='submit']").last.click(force=True, timeout=2500); print("debug locator force click submit", flush=True)
            except Exception as exc: print(f"login button locator failed: {exc}", flush=True)
            page.wait_for_timeout(250); physical_tap_at(page, 206, 470); page.wait_for_timeout(250); force_submit(page)
            result = wait_for_tid_result(page, 10000); print(f"debug tid submit result={result} url={safe_url(page)}", flush=True)
            if debug_mode and result == "timeout": return diagnostic_response(page, context, account_id, result, time.monotonic() - started, network_events)
            stage = "open_my_after_login"; mark(stage)
            goto_page(page, MY_PAGE_URL, timeout=9000); page.wait_for_timeout(4500)
            print(f"debug final my url={safe_url(page)} body={get_body_text(page, 260)}", flush=True)
            if debug_mode: return diagnostic_response(page, context, account_id, result, time.monotonic() - started, network_events)
            return screenshot_response(page)
    except Exception as exc:
        print(f"Error processing {account_id} at {stage}: {type(exc).__name__}: {exc}", flush=True)
        return image_response(f"Barcode failed\nID: {account_id}\n{stage}: {type(exc).__name__}: {exc}")
    finally:
        if browser:
            try: browser.close()
            except Exception: pass
