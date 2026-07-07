from flask import Flask, request, Response
import os
import json
import base64
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
import io

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
    img = Image.new("RGB", (700, 280), color=(255, 235, 238))
    d = ImageDraw.Draw(img)
    text = str(error).replace("\n", " ")[:360]
    lines = [text[i:i + 70] for i in range(0, len(text), 70)]
    d.multiline_text((18, 34), "\n".join(["Barcode failed", f"ID: {account_id}"] + lines[:5]), fill=(211, 47, 47))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")

def screenshot_response(page):
    client = page.context.new_cdp_session(page)
    data = client.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
    return Response(base64.b64decode(data["data"]), mimetype="image/png")

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

@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    if not account_id:
        return "Account ID is required.", 400
    browser = None
    stage = "start"
    try:
        accounts = decrypt_accounts()
        if not next((acc for acc in accounts if acc["id"] == account_id), None):
            return f"Account not found: {account_id}", 404
        with sync_playwright() as p:
            stage = "connect_browserless"
            browser = p.chromium.connect_over_cdp(build_browserless_url(), timeout=10000)
            page = browser.new_page(viewport={"width": 390, "height": 844}, user_agent=MOBILE_USER_AGENT, is_mobile=True, has_touch=True)
            page.set_default_timeout(8000)
            stage = "open_my"
            page.goto(MY_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(5000)
            print(f"before login tap url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
            physical_tap_at(page, 112, 96)
            page.wait_for_timeout(5000)
            print(f"before T tap url={safe_url(page)} body={get_body_text(page, 220)}", flush=True)
            physical_tap_at(page, 195, 400)
            page.wait_for_timeout(7000)
            print(f"after T tap url={safe_url(page)} body={get_body_text(page, 220)}", flush=True)
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
