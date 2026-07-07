from flask import Flask, request, Response
import os
import json
import base64
import io
import time
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, TimeoutError

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
    "Mozilla/5.0 (Linux; Android 13; SM-G981B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Mobile Safari/537.36"
)


def decrypt_accounts():
    key = base64.urlsafe_b64decode(ENCRYPTION_KEY_B64)
    encrypted_accounts = base64.urlsafe_b64decode(ENCRYPTED_ACCOUNTS_B64)
    return json.loads(Fernet(key).decrypt(encrypted_accounts).decode("utf-8"))


def create_error_image(account_id, error):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (760, 300), color=(255, 235, 238))
    draw = ImageDraw.Draw(img)
    text = str(error).replace("\n", " ")[:430]
    lines = [text[i:i + 76] for i in range(0, len(text), 76)]
    draw.multiline_text((18, 34), "\n".join(["Barcode failed", f"ID: {account_id}"] + lines[:5]), fill=(211, 47, 47))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")


def build_browserless_url():
    return f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=60000"


def safe_url(page):
    try:
        return page.url
    except Exception:
        return "closed"


def get_body_text(page, limit=260):
    try:
        return page.locator("body").inner_text(timeout=700).replace("\n", " | ")[:limit]
    except Exception:
        return ""


def goto_mobile_page(page, url, timeout=9000, referer=None):
    try:
        kwargs = {"wait_until": "domcontentloaded", "timeout": timeout}
        if referer:
            kwargs["referer"] = referer
        return page.goto(url, **kwargs)
    except TimeoutError as exc:
        print(f"debug goto timeout ignored url={url} current={safe_url(page)} error={exc}", flush=True)
        return None


def capture_screenshot_bytes(page):
    client = page.context.new_cdp_session(page)
    data = client.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
    return base64.b64decode(data["data"])


def screenshot_response(page):
    try:
        return Response(capture_screenshot_bytes(page), mimetype="image/png")
    except Exception as exc:
        return create_error_image("debug", f"screenshot failed: {exc}. url={safe_url(page)} body={get_body_text(page, 220)}")


def cookie_debug_lines(context):
    try:
        cookies = context.cookies()
    except Exception as exc:
        return [f"cookie read failed: {exc}"]
    interesting = [c for c in cookies if any(part in c.get("domain", "") for part in ["sktuniverse", "t-id.co.kr", "skt-id.co.kr"])]
    lines = [f"interesting cookies: {len(interesting)} / total {len(cookies)}"]
    for cookie in sorted(interesting, key=lambda c: (c.get("domain", ""), c.get("name", ""))):
        flags = []
        if cookie.get("httpOnly"):
            flags.append("HttpOnly")
        if cookie.get("secure"):
            flags.append("Secure")
        if cookie.get("sameSite"):
            flags.append(f"SameSite={cookie.get('sameSite')}")
        lines.append(f"{cookie.get('name')} @ {cookie.get('domain')} path={cookie.get('path')} len={len(cookie.get('value', ''))} {' '.join(flags)}")
    return lines


def wrap_line(text, width=82):
    text = str(text)
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def diagnostic_response(page, context, account_id, result, elapsed):
    from PIL import Image, ImageDraw
    try:
        screenshot = Image.open(io.BytesIO(capture_screenshot_bytes(page))).convert("RGB")
    except Exception:
        screenshot = Image.new("RGB", (412, 915), color=(245, 245, 245))
    img = Image.new("RGB", (screenshot.width + 720, max(screenshot.height, 980)), color=(255, 255, 255))
    img.paste(screenshot, (0, 0))
    draw = ImageDraw.Draw(img)
    x = screenshot.width + 18
    y = 18
    lines = [
        "T Universe login diagnostic",
        f"account={account_id}",
        f"elapsed={elapsed:.1f}s result={result}",
        f"url={safe_url(page)}",
        f"body={get_body_text(page, 520)}",
        "",
        "cookies:",
    ] + cookie_debug_lines(context)
    for line in lines:
        for piece in wrap_line(line):
            draw.text((x, y), piece, fill=(20, 20, 20))
            y += 18
        if y > img.height - 30:
            break
    out = io.BytesIO()
    img.save(out, format="PNG")
    return Response(out.getvalue(), mimetype="image/png")


def new_mobile_context(browser):
    return browser.new_context(
        viewport={"width": 412, "height": 915},
        user_agent=MOBILE_USER_AGENT,
        is_mobile=True,
        has_touch=True,
    )


def physical_tap_at(page, x, y):
    try:
        page.touchscreen.tap(x, y)
        print(f"debug touchscreen tap at {round(x)},{round(y)}", flush=True)
    except Exception as exc:
        print(f"touchscreen tap failed: {exc}", flush=True)
    try:
        page.mouse.click(x, y)
        print(f"debug mouse click at {round(x)},{round(y)}", flush=True)
    except Exception as exc:
        print(f"mouse click failed: {exc}", flush=True)


def tap_locator(locator, timeout=5000):
    locator.wait_for(state="visible", timeout=timeout)
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    box = locator.bounding_box(timeout=timeout)
    if not box:
        raise TimeoutError("Element has no bounding box")
    physical_tap_at(locator.page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    locator.page.wait_for_timeout(150)


def type_first_visible(page, selectors, value, timeout=6000):
    locator = page.locator(", ".join(selectors)).first
    locator.wait_for(state="visible", timeout=timeout)
    tap_locator(locator, timeout=timeout)
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


def force_submit_tid_login(page):
    try:
        page.locator("input[type='password']").first.press("Enter", timeout=1200)
        print("debug pressed Enter in password field", flush=True)
    except Exception as exc:
        print(f"debug password Enter failed: {exc}", flush=True)
    try:
        clicked = page.evaluate(
            """
            () => {
              const items = Array.from(document.querySelectorAll('button,input[type=submit],[role=button],a'))
                .map((el) => {
                  const r = el.getBoundingClientRect();
                  const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                  return { el, text, top: r.top, width: r.width, height: r.height, disabled: !!el.disabled };
                })
                .filter((item) => item.width > 100 && item.height > 20 && item.top > 330 && !item.disabled);
              const target = items.find((item) => /로그인|login/i.test(item.text)) || items[0];
              if (!target) return 'no-target';
              target.el.scrollIntoView({ block: 'center' });
              const opts = { bubbles: true, cancelable: true, view: window };
              target.el.dispatchEvent(new MouseEvent('mousedown', opts));
              target.el.dispatchEvent(new MouseEvent('mouseup', opts));
              target.el.dispatchEvent(new MouseEvent('click', opts));
              if (target.el.click) target.el.click();
              return `${target.text}|${target.top}|${target.width}x${target.height}`;
            }
            """
        )
        print(f"debug dom submit click target={clicked}", flush=True)
    except Exception as exc:
        print(f"debug dom submit click failed: {exc}", flush=True)


def wait_for_tid_result(page, timeout_ms=10000):
    end = time.monotonic() + timeout_ms / 1000
    last_url = ""
    while time.monotonic() < end:
        if page.is_closed():
            return "closed"
        last_url = safe_url(page)
        if "/member/login/channel/tid" in last_url or "code=" in last_url or "/my" in last_url:
            print(f"debug T ID callback reached: {last_url}", flush=True)
            page.wait_for_timeout(1000)
            return "callback"
        time.sleep(0.2)
    print(f"debug T ID result wait timed out at url={last_url} body={get_body_text(page, 200)}", flush=True)
    return "timeout"


def open_tid_from_my(page):
    goto_mobile_page(page, MY_PAGE_URL, timeout=7000)
    page.wait_for_timeout(700)
    referer_url = safe_url(page) if "sktuniverse" in safe_url(page) else MY_PAGE_URL
    print(f"debug my page seeded url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
    goto_mobile_page(page, TID_AUTHORIZE_URL, timeout=12000, referer=referer_url)
    page.wait_for_timeout(300)
    print(f"debug direct tid url={safe_url(page)} body={get_body_text(page, 180)}", flush=True)
    return page


@app.route("/api/get_barcode", methods=["GET"])
def handler():
    account_id = request.args.get("id")
    debug_mode = request.args.get("debug") == "1"
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
            page = context.new_page()
            page.set_default_timeout(6000)

            stage = "open_tid_from_my"
            mark(stage)
            tid_page = open_tid_from_my(page)
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
                tid_page.locator("button:has-text('로그인'), button:has-text('Login'), input[type='submit']").last.click(force=True, timeout=2500)
                print("debug locator force click submit", flush=True)
            except Exception as exc:
                print(f"login button locator failed: {exc}", flush=True)
            tid_page.wait_for_timeout(250)
            physical_tap_at(tid_page, 206, 470)
            tid_page.wait_for_timeout(250)
            force_submit_tid_login(tid_page)
            result = wait_for_tid_result(tid_page, timeout_ms=10000)
            print(f"debug tid submit result={result} url={safe_url(tid_page)}", flush=True)
            if debug_mode and result == "timeout":
                return diagnostic_response(tid_page, context, account_id, result, time.monotonic() - started)

            stage = "open_my_after_login"
            mark(stage)
            final_page = tid_page if not tid_page.is_closed() else page
            if final_page.is_closed():
                final_page = context.new_page()
                final_page.set_default_timeout(6000)
            goto_mobile_page(final_page, MY_PAGE_URL, timeout=9000)
            final_page.wait_for_timeout(2500)
            print(f"debug final my url={safe_url(final_page)} body={get_body_text(final_page, 260)}", flush=True)
            if debug_mode:
                return diagnostic_response(final_page, context, account_id, result, time.monotonic() - started)
            return screenshot_response(final_page)

    except Exception as e:
        print(f"Error processing {account_id} at {stage}: {type(e).__name__}: {e}", flush=True)
        return create_error_image(account_id, f"{stage}: {type(e).__name__}: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
