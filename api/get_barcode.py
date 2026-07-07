from flask import Flask, request, Response
import os
import json
import base64
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
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

def create_text_image(lines, width=1150):
    from PIL import Image, ImageDraw
    wrapped = []
    for line in lines:
        text = str(line).replace("\n", " | ")
        while len(text) > 125:
            wrapped.append(text[:125])
            text = text[125:]
        wrapped.append(text)
    height = max(320, 28 + len(wrapped) * 18)
    img = Image.new("RGB", (width, height), color=(245, 247, 250))
    d = ImageDraw.Draw(img)
    y = 14
    for line in wrapped:
        d.text((16, y), line, fill=(24, 31, 42))
        y += 18
    out = io.BytesIO()
    img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")

def create_error_image(account_id, error):
    return create_text_image(["Barcode failed", f"ID: {account_id}", str(error)], width=900)

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

def wait_for_text(page, text, timeout_ms=20000):
    end = time.monotonic() + timeout_ms / 1000
    last_url = ""
    last_body = ""
    while time.monotonic() < end:
        last_url = safe_url(page)
        last_body = get_body_text(page, 500)
        if text in last_body:
            return True
        time.sleep(0.5)
    raise TimeoutError(f"text not visible: {text}. url={last_url}. body={last_body}")

def physical_tap_at(page, x, y):
    try:
        page.touchscreen.tap(x, y)
        print(f"debug touchscreen tap at {round(x)},{round(y)}", flush=True)
    except Exception as tap_error:
        print(f"touchscreen tap failed: {tap_error}", flush=True)
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
    try:
        page.mouse.click(x, y)
        print(f"debug mouse click at {round(x)},{round(y)}", flush=True)
    except Exception as mouse_error:
        print(f"mouse click failed: {mouse_error}", flush=True)

def dom_click_at(page, x, y):
    try:
        return page.evaluate("""
            ([x, y]) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return 'no element';
                const clickable = el.closest('button,a,[role="button"],[onclick]') || el;
                const before = location.href;
                const label = (clickable.innerText || clickable.value || clickable.getAttribute('aria-label') || '').trim();
                clickable.click();
                return `${clickable.tagName}#${clickable.id || ''}.${String(clickable.className || '').slice(0, 80)} text=${label} href=${clickable.href || ''} before=${before} after=${location.href}`;
            }
        """, [x, y])
    except Exception as js_error:
        return f"dom click failed: {js_error}"

def element_at(page, x, y):
    try:
        return page.evaluate("""
            ([x, y]) => {
                const chain = [];
                let el = document.elementFromPoint(x, y);
                while (el && chain.length < 6) {
                    const r = el.getBoundingClientRect();
                    chain.push(`${el.tagName}#${el.id || ''}.${String(el.className || '').slice(0,80)} role=${el.getAttribute('role') || ''} href=${el.href || ''} rect=${Math.round(r.x)},${Math.round(r.y)},${Math.round(r.width)},${Math.round(r.height)} text=${String(el.innerText || el.value || '').trim().slice(0,90)}`);
                    el = el.parentElement;
                }
                return chain;
            }
        """, [x, y])
    except Exception as e:
        return [f"element_at failed: {e}"]

@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    if not account_id:
        return "Account ID is required.", 400
    browser = None
    stage = "start"
    events = []
    try:
        stage = "decrypt_accounts"
        accounts = decrypt_accounts()
        if not next((acc for acc in accounts if acc["id"] == account_id), None):
            return f"Account not found: {account_id}", 404

        with sync_playwright() as p:
            stage = "connect_browserless"
            browser = p.chromium.connect_over_cdp(build_browserless_url(), timeout=10000)
            page = browser.new_page(viewport={"width": 390, "height": 844}, user_agent=MOBILE_USER_AGENT, is_mobile=True, has_touch=True)
            page.set_default_timeout(8000)
            page.on("request", lambda req: events.append(f"REQ {req.method} {req.url[:210]}"))
            page.on("response", lambda res: events.append(f"RES {res.status} {res.url[:210]}"))
            page.on("requestfailed", lambda req: events.append(f"FAIL {req.failure or ''} {req.url[:210]}"))
            page.context.on("page", lambda new_page: events.append(f"NEW_PAGE {safe_url(new_page)}"))

            stage = "open_my"
            page.goto(MY_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(5000)
            url_before_login = safe_url(page)
            body_before_login = get_body_text(page)

            stage = "tap_login_join"
            physical_tap_at(page, 112, 96)
            wait_for_text(page, "T아이디", timeout_ms=25000)
            page.wait_for_timeout(1500)
            url_before_t = safe_url(page)
            body_before_t = get_body_text(page)
            before_pages = len(page.context.pages)
            point_chain = element_at(page, 195, 400)

            events.clear()
            stage = "tap_t_provider"
            physical_tap_at(page, 195, 400)
            dom_click_result = dom_click_at(page, 195, 400)
            page.wait_for_timeout(9000)
            after_pages = len(page.context.pages)
            url_after_t = safe_url(page)
            body_after_t = get_body_text(page)
            network_after_t = list(events)

            lines = [
                "T PROVIDER TAP NETWORK DIAGNOSTIC - WAITED FOR BUTTON",
                f"stage: {stage}",
                f"url_before_login: {url_before_login}",
                f"body_before_login: {body_before_login}",
                f"url_before_t: {url_before_t}",
                f"body_before_t: {body_before_t}",
                f"pages_before_t: {before_pages}",
                f"pages_after_t: {after_pages}",
                f"url_after_t: {url_after_t}",
                f"body_after_t: {body_after_t}",
                f"dom_click_result: {dom_click_result}",
                "",
                "elementFromPoint chain at 195,400 before tap:",
            ]
            lines.extend([f"- {item}" for item in point_chain])
            lines.append("")
            lines.append(f"network events after T tap only: count={len(network_after_t)}")
            if network_after_t:
                lines.extend([f"- {event}" for event in network_after_t[-80:]])
            else:
                lines.append("- none")
            lines.append("")
            lines.append("all pages:")
            for idx, p2 in enumerate(page.context.pages):
                lines.append(f"- page[{idx}] {safe_url(p2)} body={get_body_text(p2, 120)}")
            return create_text_image(lines)

    except Exception as e:
        print(f"Error processing {account_id} at {stage}: {type(e).__name__}: {e}", flush=True)
        return create_error_image(account_id, f"{stage}: {type(e).__name__}: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
