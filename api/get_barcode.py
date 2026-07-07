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
        return create_error_image("debug", f"debug screenshot failed: {screenshot_error}. url={safe_url(page)} body={get_body_text(page, 220)}")

def seconds_left(deadline):
    return max(0.5, deadline - time.monotonic())

def assert_time_left(deadline, stage):
    if seconds_left(deadline) < 3:
        raise TimeoutError(f"deadline reached before {stage}")

def build_browserless_url():
    return f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=55000"

def safe_url(page):
    try:
        return page.url
    except Exception:
        return "closed"

def get_body_text(page, limit=180):
    try:
        return page.locator("body").inner_text(timeout=1000).replace("\n", " | ")[:limit]
    except Exception:
        return ""

def wait_for_any(page, selectors, timeout=8000):
    joined = ", ".join(selectors)
    locator = page.locator(joined).first
    locator.wait_for(state="visible", timeout=timeout)
    return locator

def physical_tap(locator, timeout=8000):
    locator.wait_for(state="visible", timeout=timeout)
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    box = locator.bounding_box(timeout=timeout)
    if not box:
        raise TimeoutError("Element has no bounding box")
    page = locator.page
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    physical_tap_at(page, x, y)
    page.wait_for_timeout(700)
    return f"{round(x)},{round(y)}"

def physical_tap_at(page, x, y):
    try:
        page.touchscreen.tap(x, y)
        print(f"debug touchscreen tap at {round(x)},{round(y)}", flush=True)
    except Exception as tap_error:
        print(f"touchscreen tap failed, using mouse click: {tap_error}", flush=True)
        page.mouse.click(x, y)
    try:
        client = page.context.new_cdp_session(page)
        client.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [{"x": x, "y": y, "radiusX": 2, "radiusY": 2, "force": 1}],
        })
        client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        print(f"debug cdp touch at {round(x)},{round(y)}", flush=True)
    except Exception as cdp_error:
        print(f"cdp touch failed: {cdp_error}", flush=True)

def type_first_visible(page, selectors, value, timeout=8000):
    locator = wait_for_any(page, selectors, timeout=timeout)
    try:
        physical_tap(locator, timeout=timeout)
        locator.fill("", timeout=timeout)
        locator.type(value, delay=45, timeout=timeout)
        return locator.evaluate("e => e.id || e.name || e.type || e.tagName")
    except Exception as e:
        body_text = get_body_text(page, 160)
        raise TimeoutError(f"Could not type into input. url={safe_url(page)} body={body_text}. selectors={selectors}. err={e}")

def wait_for_tid_inputs(page, timeout_ms=16000):
    end_time = time.monotonic() + (timeout_ms / 1000)
    last_url = ""
    last_body = ""
    while time.monotonic() < end_time:
        last_url = safe_url(page)
        try:
            if page.locator("input#inputId, input#userId, input[type='text']").first.is_visible(timeout=1000):
                return
        except Exception:
            pass
        last_body = get_body_text(page, 160)
        time.sleep(0.5)
    raise TimeoutError(f"T ID inputs not visible. url={last_url}. body={last_body}")

def open_t_id_from_t_universe(main_page):
    main_page.goto(MY_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
    main_page.wait_for_timeout(5000)
    print(f"debug my page before login: {safe_url(main_page)} body={get_body_text(main_page, 180)}", flush=True)

    physical_tap_at(main_page, 112, 96)
    main_page.wait_for_timeout(5000)
    print(f"debug login entry: {safe_url(main_page)} body={get_body_text(main_page, 220)}", flush=True)

    before_pages = list(main_page.context.pages)
    physical_tap_at(main_page, 195, 400)
    main_page.wait_for_timeout(6000)
    after_pages = list(main_page.context.pages)
    new_pages = [p for p in after_pages if p not in before_pages]
    tid_page = new_pages[-1] if new_pages else main_page
    tid_page.set_default_timeout(8000)
    try:
        tid_page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception as load_error:
        print(f"T ID page load wait failed: {load_error}", flush=True)
    print(f"debug T ID page chosen: pages_before={len(before_pages)} pages_after={len(after_pages)} url={safe_url(tid_page)} body={get_body_text(tid_page, 220)}", flush=True)
    return tid_page

def finish_tid_login(tid_page, final_page):
    physical_tap_at(tid_page, 195, 470)
    print("debug tapped lower blue T ID login button", flush=True)
    try:
        tid_page.wait_for_url("**/member/login/channel/tid?code=**", timeout=15000)
        print(f"debug reached T Universe callback: {safe_url(tid_page)}", flush=True)
    except Exception as callback_error:
        print(f"callback wait failed: {callback_error}. current={safe_url(tid_page)}", flush=True)
    try:
        tid_page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception as load_error:
        print(f"callback domcontentloaded wait failed: {load_error}", flush=True)
    tid_page.wait_for_timeout(9000)
    final_page.goto(MY_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
    final_page.wait_for_timeout(8000)

@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    if not account_id:
        return "Account ID is required.", 400

    browser = None
    stage = "start"
    deadline = time.monotonic() + 58

    try:
        stage = "decrypt_accounts"
        accounts = decrypt_accounts()
        target_account = next((acc for acc in accounts if acc["id"] == account_id), None)
        if not target_account:
            return f"Account not found: {account_id}", 404

        with sync_playwright() as p:
            stage = "connect_browserless"
            browser = p.chromium.connect_over_cdp(build_browserless_url(), timeout=10000)
            main_page = browser.new_page(
                viewport={"width": 390, "height": 844},
                user_agent=MOBILE_USER_AGENT,
                is_mobile=True,
                has_touch=True,
            )
            main_page.set_default_timeout(8000)

            stage = "open_t_id_from_t_universe"
            tid_page = open_t_id_from_t_universe(main_page)
            wait_for_tid_inputs(tid_page, timeout_ms=min(16000, int(seconds_left(deadline) * 1000)))
            assert_time_left(deadline, stage)

            stage = "type_tid_credentials"
            print(f"stage={stage} url={safe_url(tid_page)}", flush=True)
            user_selector = type_first_visible(tid_page, [
                "input#inputId",
                "input#userId",
                "input[name='userId']",
                "input[name='id']",
                "input[type='email']",
                "input[type='text']",
            ], target_account["id"], timeout=8000)
            print(f"typed user selector: {user_selector}", flush=True)
            password_selector = type_first_visible(tid_page, [
                "input#inputPassword",
                "input#password",
                "input[name='password']",
                "input[name='passwd']",
                "input[type='password']",
            ], target_account["password"], timeout=8000)
            print(f"typed password selector: {password_selector}", flush=True)
            assert_time_left(deadline, stage)

            stage = "finish_tid_login"
            finish_tid_login(tid_page, main_page)
            print(f"debug final my page: {safe_url(main_page)} body={get_body_text(main_page, 220)}", flush=True)
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
