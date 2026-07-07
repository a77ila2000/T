from flask import Flask, request, Response
import os
import json
import base64
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, TimeoutError
import io

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
    img = Image.new('RGB', (320, 180), color=(255, 235, 238))
    d = ImageDraw.Draw(img)
    error_text = str(error).replace("\n", " ")[:95]
    error_message = f"Barcode failed\nID: {account_id}\n{error_text}"
    d.multiline_text((20, 45), error_message, fill=(211, 47, 47), align="left")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    return Response(img_byte_arr.getvalue(), mimetype='image/png')

def click_if_visible(page, selector, timeout=5000):
    try:
        page.wait_for_selector(selector, state='visible', timeout=timeout)
        page.click(selector)
        return True
    except Exception as e:
        print(f"skip selector {selector}: {e}")
        return False

def wait_and_fill(page, selectors, value, timeout=15000):
    last_error = None
    for selector in selectors:
        try:
            page.wait_for_selector(selector, state='visible', timeout=timeout)
            page.fill(selector, value)
            return selector
        except Exception as e:
            last_error = e
    raise TimeoutError(f"No visible input matched {selectors}: {last_error}")

@app.route('/api/get_barcode', methods=['GET'])
def handler():
    account_id_to_find = request.args.get('id')
    if not account_id_to_find:
        return "계정 ID를 지정해주세요.", 400

    browser = None
    stage = "start"

    try:
        stage = "decrypt_accounts"
        ACCOUNTS = decrypt_accounts()
        target_account = next((acc for acc in ACCOUNTS if acc['id'] == account_id_to_find), None)

        if not target_account:
            return f"계정({account_id_to_find}) 정보를 찾을 수 없습니다.", 404

        with sync_playwright() as p:
            stage = "connect_browserless"
            browser = p.chromium.connect_over_cdp(f"wss://chrome.browserless.io?token={BROWSERLESS_TOKEN}")
            page = browser.new_page(
                viewport={"width": 390, "height": 844},
                user_agent=MOBILE_USER_AGENT,
            )
            page.set_default_timeout(15000)

            stage = "open_my_page"
            page.goto("https://m.sktuniverse.co.kr/my", wait_until='domcontentloaded', timeout=25000)
            page.wait_for_timeout(5000)

            stage = "open_login_page"
            if click_if_visible(page, '#go-login-btn', timeout=8000):
                page.wait_for_timeout(3000)

            stage = "select_tid_login"
            if click_if_visible(page, '#link-to-tid-login', timeout=12000):
                page.wait_for_timeout(5000)

            stage = "fill_credentials"
            user_selector = wait_and_fill(page, [
                'input#userId',
                'input[name="userId"]',
                'input[name="id"]',
                'input[type="text"]:visible',
                'input[type="email"]:visible',
            ], target_account['id'])
            print(f"filled user selector: {user_selector}")

            password_selector = wait_and_fill(page, [
                'input#password',
                'input[name="password"]',
                'input[name="passwd"]',
                'input[type="password"]:visible',
            ], target_account['password'])
            print(f"filled password selector: {password_selector}")

            stage = "submit_login"
            login_clicked = False
            for selector in [
                'button#loginBtn',
                'button[type="submit"]:has-text("로그인")',
                'button:has-text("로그인")',
                'input[type="submit"]',
            ]:
                if click_if_visible(page, selector, timeout=5000):
                    login_clicked = True
                    break
            if not login_clicked:
                raise TimeoutError("No login submit button matched")

            stage = "wait_barcode_button"
            barcode_button_selector = "button.btn_barcode"
            page.wait_for_selector(barcode_button_selector, state='visible', timeout=20000)
            page.click(barcode_button_selector)

            stage = "wait_barcode_popup"
            barcode_popup_selector = "div.modal_pop_wrap.on div.barcode_box"
            page.wait_for_selector(barcode_popup_selector, state='visible', timeout=10000)

            stage = "screenshot_barcode"
            barcode_element = page.locator(barcode_popup_selector)
            screenshot_bytes = barcode_element.screenshot(type='png')

            return Response(screenshot_bytes, mimetype='image/png')

    except Exception as e:
        print(f"Error processing {account_id_to_find} at {stage}: {type(e).__name__}: {e}")
        return create_error_image(account_id_to_find, f"{stage}: {type(e).__name__}: {e}")

    finally:
        if browser:
            try:
                browser.close()
            except Exception as e:
                print(f"browser close failed: {e}")
