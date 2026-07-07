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
    # [수정] 'PNG' 오타를 'RGB'로 교정하여 500 에러를 방지합니다.
    img = Image.new('RGB', (320, 180), color=(255, 235, 238))
    d = ImageDraw.Draw(img)
    error_message = f"바코드 생성 실패\nID: {account_id}"
    if isinstance(error, TimeoutError):
        error_message += "\n(페이지 로딩 시간 초과)"
    else:
        error_message += f"\n({str(error)[:20]})"
    d.multiline_text((20, 50), error_message, fill=(211, 47, 47), align="center")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    return Response(img_byte_arr.getvalue(), mimetype='image/png')

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
            # [수정] Vercel 자체 크롬 대신, 외부의 Browserless 원격 크롬 브라우저에 연결합니다.
            # 아래 토큰 영역에 회원가입 후 발급받은 실제 토큰을 대입하거나 Vercel 환경변수로 관리하셔도 됩니다.
            browser = p.chromium.connect_over_cdp("wss://chrome.browserless.io?token=2Uq9iBy84O6QGwO008597820ed94cb8fb02789f1092d91545")
            page = browser.new_page()

            page.goto("https://m.sktuniverse.co.kr/my", wait_until='domcontentloaded', timeout=25000)

            # [보완] 로그인 수단 선택 화면 처리 및 전환 대기
            try:
                # "T아이디로 이용하기" 버튼이 나타날 때까지 최대 5초 대기
                page.wait_for_selector('text="T아이디로 이용하기"', timeout=5000)
                page.click('text="T아이디로 이용하기"')

                # ★ 중요: 버튼 클릭 후 실제 아이디 입력창('input#userId')이 화면에 로딩될 때까지 최대 5초간 명시적으로 대기합니다.
                page.wait_for_selector('input#userId', timeout=5000)
            except Exception as e:
                # 만약 이미 입력창이 바로 떠 있었거나 전환이 끝났다면 에러를 무시하고 진행합니다.
                print(f"로그인 진입 단계 우회 또는 확인: {e}")
                pass

            # 기존 로그인 입력 로직 (이제 안전하게 입력 가능)
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