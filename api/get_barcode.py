from flask import Flask, request, Response
import os
import json
import base64
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, TimeoutError
import io
import time
from urllib.parse import quote

app = Flask(__name__)

ENCRYPTION_KEY_B64 = os.environ.get("ENCRYPTION_KEY")
ENCRYPTED_ACCOUNTS_B64 = os.environ.get("ENCRYPTED_ACCOUNTS")
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "2Uq9iBy84O6QGwO008597820ed94cb8fb02789f1092d91545")

MY_PAGE_URL = "https://m.sktuniverse.co.kr/my"
LOGIN_VIEW_URL = "https://m.sktuniverse.co.kr/member/login/view?loginRedirectUrl=%2Fmy"
TID_AUTHORIZE_URL = (
    "https://tapi.t-id.co.kr/oidc/v20/authorize"
    "?client_id=a1c144a9-6ab3-49f3-b03f-4ce80d257f16"
    "&redirect_uri=https%3A%2F%2Fm.sktuniverse.co.kr%2Fmember%2Flogin%2Fchannel%2Ftid"
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
    return f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=55000"

def safe_url(page):
    try:
        return page.url
    except Exception:
        return "closed"

def get_body_text(page, limit=260):
    try:
        return page.locator("body").inner_text(timeout=1000).replace("\n", " | ")[:limit]
    except Exception:
        return ""

def wait_for_any(page, selectors, timeout=10000):
    locator = page.locator(", ".join(selectors)).first
    locator.wait_for(state="visible", timeout=timeout)
    return locator

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

def physical_tap(locator, timeout=10000):
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
    locator.page.wait_for_timeout(500)
    return f"{round(x)},{round(y)}"

def type_first_visible(page, selectors, value, timeout=10000):
    locator = wait_for_any(page, selectors, timeout=timeout)
    physical_tap(locator, timeout=timeout)
    locator.fill("", timeout=timeout)
    locator.type(value, delay=45, timeout=timeout)
    return locator.evaluate("e => e.id || e.name || e.type || e.tagName")

def wait_for_tid_login_form(page, timeout_ms=18000):
    end = time.monotonic() + timeout_ms / 1000
    last_url = ""
    last_body = ""
    while time.monotonic() < end:
        last_url = safe_url(page)
        try:
            if page.locator("input#inputId, input#userId, input[type='text']").first.is_visible(timeout=1000):
                return
        except Exception:
            pass
        last_body = get_body_text(page, 220)
        time.sleep(0.5)
    raise TimeoutError(f"T ID login form not visible. url={last_url}. body={last_body}")

def wait_for_logged_in_my(page, timeout_ms=12000):
    end = time.monotonic() + timeout_ms / 1000
    last_body = ""
    while time.monotonic() < end:
        last_body = get_body_text(page, 300)
        if "로그인" not in last_body and "회원가입" not in last_body:
            return True
        if "패밀리 혜택 관리" in last_body or "쿠폰" in last_body:
            return True
        time.sleep(0.7)
    print(f"debug my page still appears logged out/body={last_body}", flush=True)
    return False

@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    if not account_id:
        return "Account ID is required.", 400

    browser = None
    stage = "start"
    try:
        stage = "decrypt_accounts"
        accounts = decrypt_accounts()
        target_account = next((acc for acc in accounts if acc["id"] == account_id), None)
        if not target_account:
            return f"Account not found: {account_id}", 404

        with sync_playwright() as p:
            stage = "connect_browserless"
            browser = p.chromium.connect_over_cdp(build_browserless_url(), timeout=10000)
            page = browser.new_page(
                viewport={"width": 390, "height": 844},
                user_agent=MOBILE_USER_AGENT,
                is_mobile=True,
                has_touch=True,
            )
            page.set_default_timeout(10000)

            stage = "prime_t_universe_login_session"
            print(f"stage={stage}", flush=True)
            page.goto(LOGIN_VIEW_URL, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(4500)
            print(f"debug login view url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)

            stage = "open_tid_authorize_with_t_universe_context"
            page.goto(TID_AUTHORIZE_URL, wait_until="domcontentloaded", timeout=25000, referer=LOGIN_VIEW_URL)
            page.wait_for_timeout(3000)
            wait_for_tid_login_form(page, timeout_ms=18000)
            print(f"debug tid form url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)

            stage = "type_tid_credentials"
            user_selector = type_first_visible(page, [
                "input#inputId",
                "input#userId",
                "input[name='userId']",
                "input[name='id']",
                "input[type='email']",
                "input[type='text']",
            ], target_account["id"], timeout=10000)
            print(f"typed user selector: {user_selector}", flush=True)
            password_selector = type_first_visible(page, [
                "input#inputPassword",
                "input#password",
                "input[name='password']",
                "input[name='passwd']",
                "input[type='password']",
            ], target_account["password"], timeout=10000)
            print(f"typed password selector: {password_selector}", flush=True)

            stage = "submit_tid_login"
            try:
                login_button = page.locator("button:has-text('로그인'), button:has-text('Login'), input[type='submit']").last
                physical_tap(login_button, timeout=8000)
            except Exception as button_error:
                print(f"login button locator failed, coordinate tap: {button_error}", flush=True)
                physical_tap_at(page, 195, 330)
                physical_tap_at(page, 195, 470)
            page.wait_for_timeout(9000)
            print(f"debug after tid submit url={safe_url(page)} body={get_body_text(page, 220)}", flush=True)

            stage = "open_my_after_login"
            page.goto(MY_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(7000)
            wait_for_logged_in_my(page, timeout_ms=8000)
            print(f"debug final my url={safe_url(page)} body={get_body_text(page, 260)}", flush=True)
            return screenshot_response(page)

    except Exception as e:
        print(f"Error processing {account_id} at {stage}: {type(e).__name__}: {e}", flush=True)
        return create_error_image(account_id, f"{stage}: {type(e).__name__}: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
