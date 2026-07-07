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
    img = Image.new('PNG', (320, 180), color=(255, 235, 238))
    d = ImageDraw.Draw(img)
    error_message = f"바코드 생성 실패\nID: {account_id}"
    if isinstance(error, TimeoutError):
        error_message += "\n(페이지 로딩 시간 초과)"
    else:
        error_message += "\n(로그인 또는 페이지 오류)"
    d.multiline_text((20, 50), error_message, fill=(211, 47, 47), align="center")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    return Response(img_byte_arr.getvalue(), mimetype='image/png')

# get_barcode.py의 해당 라우터 부분을 아래와 같이 확실히 지정합니다.
@app.route('/api/get_barcode', methods=['GET'])
def handler():
    account_id_to_find = request.args.get('id')
    if not account_id_to_find:
        return "계정 ID를 지정해주세요.", 400

    try:
        ACCOUNTS = decrypt_accounts()
        target_account = next((acc for acc in ACCOUNTS if acc['id'] == account_id_to_find), None)

        if not target_account:
            return f"계정({account_id_to_find}) 정보를 찾을 수 없습니다.", 404

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("wss://chrome.browserless.io?token=YOUR-API-TOKEN")
            page = browser.new_page()
            
            page.goto("https://m.sktuniverse.co.kr/my", wait_until='domcontentloaded', timeout=20000)
            
            page.wait_for_selector('input#userId', timeout=15000)
            page.fill('input#userId', target_account['id'])
            page.fill('input#password', target_account['password'])
            page.click('button#loginBtn')

            barcode_button_selector = "button.btn_barcode"
            page.wait_for_selector(barcode_button_selector, timeout=15000)
            page.click(barcode_button_selector)

            barcode_popup_selector = "div.modal_pop_wrap.on div.barcode_box"
            page.wait_for_selector(barcode_popup_selector, timeout=10000)
            
            barcode_element = page.locator(barcode_popup_selector)
            screenshot_bytes = barcode_element.screenshot(type='png')
            
            browser.close()
            return Response(screenshot_bytes, mimetype='image/png')

    except Exception as e:
        print(f"Error processing {account_id_to_find}: {e}")
        return create_error_image(account_id_to_find, e)
