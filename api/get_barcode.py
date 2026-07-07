from flask import Flask, request, Response
import os
import json
import base64
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, TimeoutError
import io
import time

app = Flask(__name__)

ENCRYPTION_KEY_B64 = os.environ.get("ENCRYPTION_KEY")
ENCRYPTED_ACCOUNTS_B64 = os.environ.get("ENCRYPTED_ACCOUNTS")
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "2Uq9iBy84O6QGwO008597820ed94cb8fb02789f1092d91545")

MY_PAGE_URL = "https://m.sktuniverse.co.kr/my"
TID_AUTHORIZE_URL = (
    "https://tapi.t-id.co.kr/oidc/v20/authorize"
    "?client_id=a1c144a9-6ab3-49f3-b03f-4ce80d257f16"
    "&redirect_uri=https%3A%2F%2Fm.sktuniverse.co.kr%2Fmember%2Flogin%2Fchannel/tid"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

def decrypt_accounts():
    key = base64.urlsafe_b64decode(ENCRYPTION_KEY_B64)
    encrypted_accounts = base64.urlsafe_b64decode(ENCRYPTED_ACCOUNTS_B64)
    f = Fernet(key)
    return json.loads(f.decrypt(encrypted_accounts).decode("utf-8"))

def create_error_image(account_id, error):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (760, 300), color=(255, 235, 238))
    d = ImageDraw.Draw(img)
    text = str(error).replace("\n", " ")[:430]
    lines = [text[i:i + 76] for i in range(0, len(text), 76)]
    d.multiline_text((18, 34), "\n".join(["Barcode failed", f"ID: {account_id}"] + lines[:5]), fill=(211, 47, 47))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")

def screenshot_response(page):
    try:
        client = page.context.new_cdp_session(page)
        data = client.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        return Response(base64.b64decode(data["data"]), mimetype="image/png")
    except Exception as screenshot_error:
        return create_error_image("debug", f"screenshot failed: {screenshot_error}. url={safe_url(page)} body={get_body_text(page, 220)}")

def build_browserless_url():
    return f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=60000"

def safe_url(page):
    try:
        return page.url
    except Exception:
        return "closed"

def get_body_text(page, limit=260):
    try:
        return page.locator("body").inner_text(timeout=800).replace("\n", " | ")[:limit]
    except Exception:
        return ""

def wait_for_any(page, selectors, timeout=6000):
    locator = page.locator(", ".join(selectors)).first
    locator.wait_for(state="visible", timeout=timeout)
    return locator

def wait_for_text_contains(page, expected, timeout_ms=7000):
    end = time.monotonic() + timeout_ms / 1000
    last_body = ""
    while time.monotonic() < end:
        last_body = get_body_text(page, 400)
        if expected in last_body:
            return
        time.sleep(0.2)
    raise TimeoutError(f"text not visible: {expected}. url={safe_url(page)} body={last_body}")

def new_mobile_context(browser):
    return browser.new_context(
        viewport={"width": 390, "height": 844},
        user_agent=MOBILE_USER_AGENT,
        is_mobile=True,
        has_touch=True,
    )

def physical_tap_at(page, x, y):
    try:
        page.touchscreen.tap(x, y)
        print(f"debug touchscreen tap at {round(x)},{round(y)}", flush=True)
    except Exception as tap_error:
        print(f"touchscreen tap failed: {tap_error}", flush=True)
    try:
        page.mouse.click(x, y)
        print(f"debug mouse click at {round(x)},{round(y)}", flush=True)
    except Exception as mouse_error:
        print(f"mouse click failed: {mouse_error}", flush=True)

def physical_tap(locator, timeout=6000):
    locator.wait_for(state="visible", timeout=timeout)
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    box = locator.bounding_box(timeout=timeout)
    if not box:
        raise TimeoutError("Element has no bounding box")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    physical_tap_at(locator.page, x, y)
    locator.page.wait_for_timeout(150)
    return f"{round(x)},{round(y)}"

def type_first_visible(page, selectors, value, timeout=6000):
    locator = wait_for_any(page, selectors, timeout=timeout)
    physical_tap(locator, timeout=timeout)
    locator.fill("", timeout=timeout)
    locator.type(value, delay=10, timeout=timeout)
    return locator.evaluate("e => e.id || e.name || e.type || e.tagName")

def wait_for_tid_login_form(page, timeout_ms=8000):
    end = time.monotonic() + timeout_ms / 1000
    last_url = ""
    last_body = ""
    while time.monotonic() < end:
        last_url = safe_url(page)
        try:
            if page.locator("input#inputId, input#userId, input[type='text']").first.is_visible(timeout=400):
                return
        except Exception:
            pass
        last_body = get_body_text(page, 220)
        time.sleep(0.2)
    raise TimeoutError(f"T ID login form not visible. url={last_url}. body={last_body}")

def wait_for_tid_result(tid_page, timeout_ms=3200):
    end = time.monotonic() + timeout_ms / 1000
    last_url = ""
    while time.monotonic() < end:
        if tid_page.is_closed():
            print("debug T ID page closed after submit", flush=True)
            return "closed"
        last_url = safe_url(tid_page)
        if "/member/login/channel/tid" in last_url or "code=" in last_url:
            print(f"debug T ID callback reached: {last_url}", flush=True)
            tid_page.wait_for_timeout(300)
            return "callback"
        time.sleep(0.2)
    print(f"debug T ID result wait timed out at url={last_url} body={get_body_text(tid_page, 200)}", flush=True)
    return "timeout"

def open_authorize_fallback(main_page, chooser_url):
    print("debug opening authorize fallback in same context", flush=True)
    tid_page = main_page.context.new_page()
    tid_page.set_default_timeout(6000)
    tid_page.goto(TID_AUTHORIZE_URL, wait_until="domcontentloaded", timeout=12000, referer=chooser_url)
    tid_page.wait_for_timeout(300)
    print(f"debug fallback tid url={safe_url(tid_page)} body={get_body_text(tid_page, 180)}", flush=True)
    return tid_page

def open_tid_from_my(main_page):
    main_page.goto(MY_PAGE_URL, wait_until="domcontentloaded", timeout=12000)
    main_page.wait_for_timeout(500)
    print(f"debug my page before login url={safe_url(main_page)} body={get_body_text(main_page, 180)}", flush=True)

    try:
        login_entry = main_page.locator("text=로그인·회원가입").first
        physical_tap(login_entry, timeout=3500)
    except Exception as login_click_error:
        print(f"login text click failed, using coordinate: {login_click_error}", flush=True)
        body = get_body_text(main_page, 260)
        y = 156 if "보러가기" in body else 96
        physical_tap_at(main_page, 105, y)

    main_page.wait_for_timeout(600)
    chooser_url = safe_url(main_page)
    print(f"debug login chooser url={chooser_url} body={get_body_text(main_page, 220)}", flush=True)

    before_pages = list(main_page.context.pages)
    try:
        tid_button = main_page.locator("#link-to-tid-login").first
        physical_tap(tid_button, timeout=3500)
    except Exception as tid_button_error:
        print(f"T ID button not visible after login entry: {tid_button_error}", flush=True)
        return open_authorize_fallback(main_page, chooser_url)

    main_page.wait_for_timeout(800)
    after_pages = list(main_page.context.pages)
    new_pages = [candidate for candidate in after_pages if candidate not in before_pages]
    tid_page = new_pages[-1] if new_pages else main_page
    tid_page.set_default_timeout(6000)
    print(f"debug after T button pages_before={len(before_pages)} pages_after={len(after_pages)} tid_url={safe_url(tid_page)} body={get_body_text(tid_page, 180)}", flush=True)

    if "auth.skt-id.co.kr" not in safe_url(tid_page) and "tapi.t-id.co.kr" not in safe_url(tid_page):
        print("debug T button did not navigate in Browserless", flush=True)
        tid_page = open_authorize_fallback(main_page, chooser_url)
    return tid_page

@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    if not account_id:
        return "Account ID is required.", 400

    browser = None
    stage = "start"
    started = time.monotonic()

    def mark(label):
        print(f"debug elapsed={time.monotonic() - started:.1f}s stage={label}", flush=True)

    try:
        stage = "decrypt_accounts"
        mark(stage)
        accounts = decrypt_accounts()
        target_account = next((acc for acc in accounts if acc["id"] == account_id), None)
        if not target_account:
            return f"Account not found: {account_id}", 404

        with sync_playwright() as p:
            stage = "connect_browserless"
            mark(stage)
            browser = p.chromium.connect_over_cdp(build_browserless_url(), timeout=8000)
            context = new_mobile_context(browser)
            main_page = context.new_page()
            main_page.set_default_timeout(6000)

            stage = "open_tid_from_my"
            mark(stage)
            tid_page = open_tid_from_my(main_page)
            wait_for_tid_login_form(tid_page, timeout_ms=8000)

            stage = "type_tid_credentials"
            mark(stage)
            user_selector = type_first_visible(tid_page, [
                "input#inputId",
                "input#userId",
                "input[name='userId']",
                "input[name='id']",
                "input[type='email']",
                "input[type='text']",
            ], target_account["id"], timeout=6000)
            print(f"typed user selector: {user_selector}", flush=True)
            password_selector = type_first_visible(tid_page, [
                "input#inputPassword",
                "input#password",
                "input[name='password']",
                "input[name='passwd']",
                "input[type='password']",
            ], target_account["password"], timeout=6000)
            print(f"typed password selector: {password_selector}", flush=True)

            stage = "submit_tid_login"
            mark(stage)
            try:
                login_button = tid_page.locator("button:has-text('로그인'), button:has-text('Login'), input[type='submit']").last
                physical_tap(login_button, timeout=4500)
            except Exception as button_error:
                print(f"login button locator failed, coordinate tap: {button_error}", flush=True)
                physical_tap_at(tid_page, 195, 330)
                physical_tap_at(tid_page, 195, 470)
            result = wait_for_tid_result(tid_page, timeout_ms=3200)
            print(f"debug tid submit result={result} url={safe_url(tid_page)}", flush=True)

            stage = "open_my_after_login"
            mark(stage)
            if main_page.is_closed():
                main_page = context.new_page()
                main_page.set_default_timeout(6000)
            main_page.goto(MY_PAGE_URL, wait_until="domcontentloaded", timeout=11000)
            main_page.wait_for_timeout(800)
            print(f"debug final my url={safe_url(main_page)} body={get_body_text(main_page, 260)}", flush=True)
            return screenshot_response(main_page)

    except Exception as e:
        print(f"Error processing {account_id} at {stage}: {type(e).__name__}: {e}", flush=True)
        return create_error_image(account_id, f"{stage}: {type(e).__name__}: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
