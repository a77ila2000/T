from flask import Flask, request, Response
import os
import json
import base64
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, TimeoutError
import io
import time

app = Flask(__name__)

# Vercel 환경 변수에서 값 가져오기
ENCRYPTION_KEY_B64 = os.environ.get("ENCRYPTION_KEY")
ENCRYPTED_ACCOUNTS_B64 = os.environ.get("ENCRYPTED_ACCOUNTS")
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "2Uq9iBy84O6QGwO008597820ed94cb8fb02789f1092d91545")

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

def decrypt_accounts():
    key = base64.urlsafe_b64decode(ENCRYPTION_KEY_B64)
    encrypted_accounts = base64.urlsafe_b64decode(ENCRYPTED_ACCOUNTS_B64)
    f = Fernet(key)
    decrypted_json = f.decrypt(encrypted_accounts).decode('utf-8')
    return json.loads(decrypted_json)

def create_error_image(account_id, error):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (520, 260), color=(255, 235, 238))
    d = ImageDraw.Draw(img)
    error_text = str(error).replace("\n", " ")[:220]
    lines = [error_text[i:i + 52] for i in range(0, len(error_text), 52)]
    error_message = "\n".join(["Barcode failed", f"ID: {account_id}"] + lines[:5])
    d.multiline_text((18, 34), error_message, fill=(211, 47, 47), align="left")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    return Response(img_byte_arr.getvalue(), mimetype='image/png')

def seconds_left(deadline):
    return max(0.5, deadline - time.monotonic())

def assert_time_left(deadline, stage):
    if seconds_left(deadline) < 3:
        raise TimeoutError(f"deadline reached before {stage}")

def build_browserless_url():
    return f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}&stealth=true&timeout=55000"

def fill_first_visible(page, selectors, value, timeout=8000):
    joined = ", ".join(selectors)
    locator = page.locator(joined).first
    try:
        locator.wait_for(state='visible', timeout=timeout)
        locator.fill(value, timeout=timeout)
        return locator.evaluate("e => e.id || e.name || e.type || e.tagName")
    except Exception as e:
        body_text = get_body_text(page, 160)
        raise TimeoutError(f"No visible input after {timeout}ms. url={safe_url(page)} body={body_text}. selectors={selectors}. err={e}")

def wait_for_any(page, selectors, timeout=8000):
    joined = ", ".join(selectors)
    locator = page.locator(joined).first
    locator.wait_for(state='visible', timeout=timeout)
    return locator

def safe_url(page):
    try:
        return page.url
    except Exception:
        return "closed"

def get_body_text(page, limit=180):
    try:
        return page.locator('body').inner_text(timeout=1000).replace("\n", " | ")[:limit]
    except Exception:
        return ""

def wait_for_tid_login_page(parent_page, timeout_ms=16000):
    context = parent_page.context
    before_pages = set(context.pages)
    parent_page.click('#link-to-tid-login', timeout=5000)

    end_time = time.monotonic() + (timeout_ms / 1000)
    last_seen = []
    while time.monotonic() < end_time:
        pages = [p for p in context.pages if not p.is_closed()]
        last_seen = [safe_url(p) for p in pages]

        for candidate in pages:
            url = safe_url(candidate)
            if "auth.skt-id.co.kr" in url or "tapi.t-id.co.kr" in url:
                try:
                    candidate.wait_for_load_state('domcontentloaded', timeout=5000)
                except Exception:
                    pass
                return candidate

        if safe_url(parent_page).startswith("https://auth.skt-id.co.kr"):
            return parent_page

        new_pages = [p for p in pages if p not in before_pages]
        if new_pages:
            candidate = new_pages[-1]
            try:
                candidate.wait_for_load_state('domcontentloaded', timeout=5000)
            except Exception:
                pass
            if "T ID" in candidate.title() or candidate.locator('input#inputId, input[type="password"]').count() > 0:
                return candidate

        time.sleep(0.5)

    raise TimeoutError(f"T ID popup page not found. pages={last_seen}. parent_body={get_body_text(parent_page)}")

def wait_for_tid_inputs(login_page, timeout_ms=16000):
    end_time = time.monotonic() + (timeout_ms / 1000)
    last_url = ""
    last_body = ""
    while time.monotonic() < end_time:
        if login_page.is_closed():
            raise TimeoutError("T ID popup closed before inputs appeared")
        last_url = safe_url(login_page)
        try:
            if login_page.locator('input#inputId, input#userId, input[type="text"]').first.is_visible(timeout=1000):
                return
        except Exception:
            pass
        last_body = get_body_text(login_page, 160)
        time.sleep(0.5)
    raise TimeoutError(f"T ID inputs not visible. url={last_url}. body={last_body}")

def wait_for_popup_close_or_callback(login_page, timeout_ms=12000):
    end_time = time.monotonic() + (timeout_ms / 1000)
    last_url = ""
    while time.monotonic() < end_time:
        if login_page.is_closed():
            return "closed"
        try:
            last_url = login_page.url
            if "m.sktuniverse.co.kr/member/login/channel/tid" in last_url:
                return f"callback:{last_url}"
            if "m.sktuniverse.co.kr/my" in last_url:
                return f"my:{last_url}"
        except Exception:
            return "closed"
        time.sleep(0.5)
    body_text = get_body_text(login_page)
    raise TimeoutError(f"T ID popup did not close or reach callback. url={last_url}. body={body_text}")

@app.route('/api/get_barcode', methods=['GET'])
def handler():
    account_id_to_find = request.args.get('id')
    if not account_id_to_find:
        return "계정 ID를 지정해주세요.", 400

    browser = None
    stage = "start"
    deadline = time.monotonic() + 52

    try:
        stage = "decrypt_accounts"
        ACCOUNTS = decrypt_accounts()
        target_account = next((acc for acc in ACCOUNTS if acc['id'] == account_id_to_find), None)

        if not target_account:
            return f"계정({account_id_to_find}) 정보를 찾을 수 없습니다.", 404

        with sync_playwright() as p:
            stage = "connect_browserless"
            browser = p.chromium.connect_over_cdp(build_browserless_url(), timeout=10000)
            page = browser.new_page(viewport={"width": 1280, "height": 900}, user_agent=DESKTOP_USER_AGENT)
            page.set_default_timeout(8000)

            stage = "open_tuniverse_login"
            print(f"stage={stage}", flush=True)
            page.goto("https://m.sktuniverse.co.kr/member/login/view?loginRedirectUrl=%2Fmy", wait_until='domcontentloaded', timeout=25000)
            page.wait_for_selector('#link-to-tid-login', state='visible', timeout=15000)
            page.wait_for_timeout(1500)
            assert_time_left(deadline, stage)

            stage = "open_tid_popup"
            print(f"stage={stage} url={page.url}", flush=True)
            login_page = wait_for_tid_login_page(page, timeout_ms=min(16000, int(seconds_left(deadline) * 1000)))
            print(f"tid page url={safe_url(login_page)} title={login_page.title()}", flush=True)
            wait_for_tid_inputs(login_page, timeout_ms=min(16000, int(seconds_left(deadline) * 1000)))
            assert_time_left(deadline, stage)

            stage = "fill_tid_credentials"
            print(f"stage={stage} url={safe_url(login_page)}", flush=True)
            user_selector = fill_first_visible(login_page, [
                'input#inputId',
                'input#userId',
                'input[name="userId"]',
                'input[name="id"]',
                'input[type="email"]',
                'input[type="text"]',
            ], target_account['id'], timeout=8000)
            print(f"filled user selector: {user_selector}", flush=True)

            password_selector = fill_first_visible(login_page, [
                'input#inputPassword',
                'input#password',
                'input[name="password"]',
                'input[name="passwd"]',
                'input[type="password"]',
            ], target_account['password'], timeout=8000)
            print(f"filled password selector: {password_selector}", flush=True)
            assert_time_left(deadline, stage)

            stage = "submit_tid_login"
            print(f"stage={stage} url={safe_url(login_page)}", flush=True)
            login_button = wait_for_any(login_page, [
                'button[data-click-id="login"]',
                'button.btn-secondary:has-text("Login")',
                'button:has-text("Login")',
                'button:has-text("로그인")',
                'button#loginBtn',
            ], timeout=8000)
            login_button.click(timeout=8000)
            result = wait_for_popup_close_or_callback(login_page, timeout_ms=min(12000, int(seconds_left(deadline) * 1000)))
            print(f"tid login result={result}", flush=True)
            assert_time_left(deadline, stage)

            stage = "open_my_after_login"
            print(f"stage={stage} parent_url={safe_url(page)}", flush=True)
            if page.is_closed():
                page = browser.new_page(viewport={"width": 1280, "height": 900}, user_agent=DESKTOP_USER_AGENT)
            page.goto("https://m.sktuniverse.co.kr/my", wait_until='domcontentloaded', timeout=18000)
            page.wait_for_timeout(3500)
            assert_time_left(deadline, stage)

            stage = "wait_barcode_button"
            print(f"stage={stage} url={safe_url(page)}", flush=True)
            if '#go-login-btn' in page.locator('body').inner_html(timeout=2000):
                raise TimeoutError(f"Still logged out after T ID popup. body={get_body_text(page)}")
            barcode_button = wait_for_any(page, [
                'button.btn_barcode',
                'button:has-text("바코드")',
                '[aria-label*="바코드"]',
            ], timeout=min(12000, int(seconds_left(deadline) * 1000)))
            barcode_button.click(timeout=5000)
            assert_time_left(deadline, stage)

            stage = "wait_barcode_popup"
            print(f"stage={stage} url={safe_url(page)}", flush=True)
            barcode_popup = wait_for_any(page, [
                'div.modal_pop_wrap.on div.barcode_box',
                '.barcode_box',
                '[class*="barcode"]',
            ], timeout=min(8000, int(seconds_left(deadline) * 1000)))

            stage = "screenshot_barcode"
            screenshot_bytes = barcode_popup.screenshot(type='png')

            return Response(screenshot_bytes, mimetype='image/png')

    except Exception as e:
        print(f"Error processing {account_id_to_find} at {stage}: {type(e).__name__}: {e}", flush=True)
        return create_error_image(account_id_to_find, f"{stage}: {type(e).__name__}: {e}")

    finally:
        if browser:
            try:
                browser.close()
            except Exception as e:
                print(f"browser close failed: {e}", flush=True)
