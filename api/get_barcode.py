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

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

def decrypt_accounts():
    """환경 변수에서 암호화된 정보를 가져와 복호화합니다."""
    key = base64.urlsafe_b64decode(ENCRYPTION_KEY_B64)
    encrypted_accounts = base64.urlsafe_b64decode(ENCRYPTED_ACCOUNTS_B64)
    f = Fernet(key)
    decrypted_json = f.decrypt(encrypted_accounts).decode('utf-8')
    return json.loads(decrypted_json)

def create_error_image(account_id, error):
    """오류 발생 시 보여줄 이미지 생성"""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (420, 210), color=(255, 235, 238))
    d = ImageDraw.Draw(img)
    error_text = str(error).replace("\n", " ")[:150]
    lines = [error_text[i:i + 42] for i in range(0, len(error_text), 42)]
    error_message = "\n".join(["Barcode failed", f"ID: {account_id}"] + lines[:4])
    d.multiline_text((18, 34), error_message, fill=(211, 47, 47), align="left")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    return Response(img_byte_arr.getvalue(), mimetype='image/png')

def seconds_left(deadline):
    return max(0.5, deadline - time.monotonic())

def assert_time_left(deadline, stage):
    if seconds_left(deadline) < 3:
        raise TimeoutError(f"deadline reached before {stage}")

def click_if_visible(page, selector, timeout=4000):
    try:
        page.wait_for_selector(selector, state='visible', timeout=timeout)
        page.click(selector, timeout=timeout)
        return True
    except Exception as e:
        print(f"skip selector {selector}: {e}", flush=True)
        return False

def fill_first_visible(page, selectors, value, timeout=8000):
    joined = ", ".join(selectors)
    locator = page.locator(joined).first
    try:
        locator.wait_for(state='visible', timeout=timeout)
        locator.fill(value, timeout=timeout)
        return locator.evaluate("e => e.id || e.name || e.type || e.tagName")
    except Exception as e:
        body_text = ""
        try:
            body_text = page.locator('body').inner_text(timeout=1000).replace("\n", " | ")[:120]
        except Exception:
            pass
        raise TimeoutError(f"No visible input matched after {timeout}ms. selectors={selectors}. url={page.url}. body={body_text}. err={e}")

def wait_for_any(page, selectors, timeout=8000):
    joined = ", ".join(selectors)
    locator = page.locator(joined).first
    locator.wait_for(state='visible', timeout=timeout)
    return locator

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
            browser = p.chromium.connect_over_cdp(
                f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}",
                timeout=10000,
            )
            page = browser.new_page(
                viewport={"width": 390, "height": 844},
                user_agent=MOBILE_USER_AGENT,
            )
            page.set_default_timeout(8000)

            stage = "open_my_page"
            print(f"stage={stage}", flush=True)
            page.goto("https://m.sktuniverse.co.kr/my", wait_until='domcontentloaded', timeout=18000)
            page.wait_for_timeout(2500)
            assert_time_left(deadline, stage)

            stage = "open_login_page"
            print(f"stage={stage} url={page.url}", flush=True)
            if click_if_visible(page, '#go-login-btn', timeout=5000):
                page.wait_for_timeout(2500)
            assert_time_left(deadline, stage)

            stage = "select_tid_login"
            print(f"stage={stage} url={page.url}", flush=True)
            if click_if_visible(page, '#link-to-tid-login', timeout=7000):
                page.wait_for_timeout(3000)
            assert_time_left(deadline, stage)

            stage = "fill_credentials"
            print(f"stage={stage} url={page.url}", flush=True)
            user_selector = fill_first_visible(page, [
                'input#userId',
                'input[name="userId"]',
                'input[name="id"]',
                'input[type="email"]',
                'input[type="text"]:not(#add-slot-search-proxy)',
            ], target_account['id'], timeout=8000)
            print(f"filled user selector: {user_selector}", flush=True)

            password_selector = fill_first_visible(page, [
                'input#password',
                'input[name="password"]',
                'input[name="passwd"]',
                'input[type="password"]',
            ], target_account['password'], timeout=8000)
            print(f"filled password selector: {password_selector}", flush=True)
            assert_time_left(deadline, stage)

            stage = "submit_login"
            print(f"stage={stage} url={page.url}", flush=True)
            login_button = wait_for_any(page, [
                'button#loginBtn',
                'button[type="submit"]:has-text("로그인")',
                'button:has-text("로그인")',
                'input[type="submit"]',
            ], timeout=8000)
            login_button.click(timeout=8000)
            assert_time_left(deadline, stage)

            stage = "wait_barcode_button"
            print(f"stage={stage} url={page.url}", flush=True)
            barcode_button = wait_for_any(page, [
                'button.btn_barcode',
                'button:has-text("바코드")',
                '[aria-label*="바코드"]',
            ], timeout=min(12000, int(seconds_left(deadline) * 1000)))
            barcode_button.click(timeout=5000)
            assert_time_left(deadline, stage)

            stage = "wait_barcode_popup"
            print(f"stage={stage} url={page.url}", flush=True)
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
